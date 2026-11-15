#!/usr/bin/env python3
"""Build a country-grouped playlist from public IPTV-org feeds."""
import concurrent.futures
import json
import gzip
import xml.etree.ElementTree as ET
import re
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone

COUNTRIES = {"United States": "us", "Canada": "ca", "Australia": "au", "New Zealand": "nz", "Brazil": "br", "Scotland": "uk", "England": "uk"}
BASE = "https://iptv-org.github.io/iptv/countries/{}.m3u"
UA = "Jeremy-IPTV-Checker/1.0"
def get(url, timeout=12, method="GET"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-4095"}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        ctype = response.headers.get("Content-Type", "").lower()
        code = response.status
        data = response.read(4096)
    return code, ctype, data

def parse(text):
    items = []
    pending = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            pending = line
        elif pending and line and not line.startswith("#"):
            items.append((pending, line))
            pending = None
    return items

def region(ext):
    title = ext.split(",", 1)[-1].lower()
    group = re.search(r'group-title="([^"]*)"', ext)
    fields = (title + " " + (group.group(1).lower() if group else ""))
    if re.search(r'\b(scotland|scottish|glasgow|edinburgh|stv)\b', fields):
        return "Scotland"
    if re.search(r'\b(england|english|london|manchester|birmingham)\b', fields):
        return "England"
    return "United Kingdom / Unclassified"

def check(item):
    ext, url, country = item
    try:
        code, ctype, data = get(url)
        if code not in (200, 206):
            return None, str(code)
        if "text/html" in ctype or data.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            return None, "html"
        if not (url.lower().split("?")[0].endswith((".m3u8", ".mpd", ".mp4", ".ts")) or "mpegurl" in ctype or "video/" in ctype or "dash+xml" in ctype):
            return None, "unknown-media"
        return (ext, url, country), "ok"
    except urllib.error.HTTPError as e:
        return None, str(e.code)
    except Exception:
        return None, "network"

def main():
    candidates = []
    errors = Counter()
    seen = set()
    for country, code in COUNTRIES.items():
        if code == "uk" and country == "England":
            continue
        try:
            req = urllib.request.Request(BASE.format(code), headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as response:
                source = response.read().decode("utf-8-sig")
            for ext, url in parse(source):
                if code == "uk":
                    classified = region(ext)
                    country = classified
                if url in seen:
                    continue
                seen.add(url)
                candidates.append((ext, url, country))
        except Exception:
            errors["source_" + code] += 1
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
        for valid, reason in pool.map(check, candidates):
            errors[reason] += 1
            if valid:
                results.append(valid)
    order = list(COUNTRIES) + ["United Kingdom / Unclassified"]
    results.sort(key=lambda x: (order.index(x[2]), x[0].split(",", 1)[-1].lower()))
    epg_url = "https://epgshare01.online/epgshare01/epg_ripper_UK1.xml.gz"
    epg_status = "unavailable"
    epg_matches = 0
    epg_error = None
    try:
        req = urllib.request.Request(epg_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as response:
            epg_data = response.read(30_000_000)
        root = ET.fromstring(gzip.decompress(epg_data) if epg_data[:2] == b"\x1f\x8b" else epg_data)
        if root.tag != "tv":
            raise ValueError("Not XMLTV")
        guide_ids = {ch.get("id") for ch in root.findall("channel")}
        playlist_ids = set()
        for ext, _, _ in results:
            m = re.search(r'tvg-id="([^"]+)"', ext)
            if m:
                playlist_ids.add(m.group(1))
        epg_matches = len(guide_ids & playlist_ids)
        if epg_matches:
            with open("epg.xml", "wb") as out:
                out.write(gzip.decompress(epg_data) if epg_data[:2] == b"\x1f\x8b" else epg_data)
            epg_url = "https://raw.githubusercontent.com/adailtononline-cmd/Jeremy/main/epg.xml"
            epg_status = "matched"
        else:
            epg_status = "no_matching_channel_ids"
    except Exception as exc:
        epg_error = str(exc)[:300]
        epg_status = "invalid_or_unreachable"
    logo_catalog = {}
    try:
        req = urllib.request.Request("https://iptv-org.github.io/api/logos.json", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as response:
            logos = json.load(response)
        for logo in logos:
            channel_id = logo.get("channel")
            logo_url = logo.get("url")
            if channel_id and isinstance(logo_url, str) and logo_url.startswith("https://"):
                logo_catalog.setdefault(channel_id, logo_url)
    except Exception:
        pass
    logo_stats = Counter()
    lines = ['#EXTM3U' + (' url-tvg="' + epg_url + '" x-tvg-url="' + epg_url + '"' if epg_status == "matched" else '')]
    for ext, url, country in results:
        logo_match = re.search(r'tvg-logo="([^"]*)"', ext)
        channel_match = re.search(r'tvg-id="([^"]*)"', ext)
        existing_logo = logo_match.group(1).strip() if logo_match else ""
        channel_id = channel_match.group(1).strip() if channel_match else ""
        if not existing_logo:
            replacement = logo_catalog.get(channel_id)
            if replacement:
                if logo_match:
                    ext = ext[:logo_match.start()] + 'tvg-logo="' + replacement + '"' + ext[logo_match.end():]
                else:
                    ext = ext.replace(",", ' tvg-logo="' + replacement + '",', 1)
                logo_stats["repaired_from_catalog"] += 1
            else:
                logo_stats["still_missing"] += 1
        else:
            logo_stats["already_supplied"] += 1
        original_group = re.search(r'group-title="([^"]*)"', ext)
        category = original_group.group(1).strip() if original_group else ""
        display_country = {"United Kingdom / Unclassified": "United Kingdom", "United States": "United States", "Canada": "Canada", "Australia": "Australia", "England": "England", "Scotland": "Scotland"}.get(country, country)
        combined_group = display_country
        if category and 'tvg-category=' not in ext:
            ext = ext.replace(",", ' tvg-category="' + category.replace('"', "") + '",', 1)
        ext = re.sub(r'group-title="[^"]*"', lambda _: 'group-title="' + combined_group + '"', ext)
        if 'group-title=' not in ext:
            ext = ext.replace(",", ' group-title="' + combined_group + '",', 1)
        lines += [ext, url]
    if results:
        with open("jeremy.m3u", "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "candidates": len(candidates), "accepted": len(results), "by_country": dict(Counter(x[2] for x in results)), "test_results": dict(errors), "notes": ["HTTP checks do not prove playback on Sparkle TV.", "UK channels without explicit England/Scotland metadata are preserved under United Kingdom / Unclassified rather than discarded.", "Jeremy's previously approved channel numbers cannot be safely matched without his original snapshot."]}
    report["logos"] = dict(logo_stats)
    report["epg"] = {"status": epg_status, "matching_channel_ids": epg_matches, "error": epg_error, "source": "https://epgshare01.online/epgshare01/epg_ripper_UK1.xml.gz"}
    with open("report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    if not results:
        raise SystemExit("No valid channels: keeping any existing playlist unchanged")

if __name__ == "__main__":
    main()
