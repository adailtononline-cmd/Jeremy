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

def logo_works(url):
    if not url or not url.startswith("https://"):
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Range": "bytes=0-2047"})
        with urllib.request.urlopen(req, timeout=8) as response:
            data = response.read(2048)
            ctype = response.headers.get("Content-Type", "").lower()
            return response.status in (200, 206) and (data.startswith((bytes.fromhex("89504e47"), bytes.fromhex("ffd8ff"), b"GIF87a", b"GIF89a", b"RIFF")) or b"<svg" in data[:512].lower() or (ctype.startswith("image/") and not data.lstrip().lower().startswith(b"<html")))
    except Exception:
        return False

def add_attribute(ext, name, value):
    prefix = re.match(r"^#EXTINF:-?\d+", ext)
    if not prefix:
        return ext
    pos = prefix.end()
    return ext[:pos] + ' ' + name + '="' + value.replace('"', "") + '"' + ext[pos:]

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
    epg_programmes = 0
    epg_error = None
    try:
        req = urllib.request.Request(epg_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as response:
            epg_data = response.read(30_000_000)
        root = ET.fromstring(gzip.decompress(epg_data) if epg_data[:2] == b"\x1f\x8b" else epg_data)
        if root.tag != "tv":
            raise ValueError("Not XMLTV")
        def norm(s):
            return re.sub(r"[^a-z0-9]", "", re.sub(r"\\([^)]*\\)", "", (s or "").lower()))
        by_name = {}
        for ch in root.findall("channel"):
            for name in ch.findall("display-name"):
                key = norm(name.text)
                if key and len(key) >= 4:
                    by_name.setdefault(key, set()).add(ch.get("id"))
        guide_ids = {ch.get("id") for ch in root.findall("channel")}
        playlist_ids = set()
        alias = {}
        for ext, _, _ in results:
            m = re.search(r'tvg-id="([^"]+)"', ext)
            if not m:
                continue
            pid = m.group(1)
            playlist_ids.add(pid)
            if pid in guide_ids:
                alias[pid] = pid
                continue
            title = ext.split(",", 1)[-1]
            keys = [norm(title), norm(pid.split("@")[0].split(".")[0])]
            for key in keys:
                options = by_name.get(key, set())
                if len(options) == 1:
                    alias[pid] = next(iter(options))
                    break
        epg_matches = len(alias)
        if epg_matches:
            reverse = {}
            for pid, gid in alias.items():
                reverse.setdefault(gid, []).append(pid)
            for node in list(root):
                if node.tag == "channel":
                    gid = node.get("id")
                    if gid not in reverse:
                        root.remove(node)
                    else:
                        node.set("id", reverse[gid][0])
                        for extra in reverse[gid][1:]:
                            duplicate = ET.fromstring(ET.tostring(node))
                            duplicate.set("id", extra)
                            root.append(duplicate)
                elif node.tag == "programme":
                    gid = node.get("channel")
                    if gid not in reverse:
                        root.remove(node)
                    else:
                        node.set("channel", reverse[gid][0])
                        for extra in reverse[gid][1:]:
                            duplicate = ET.fromstring(ET.tostring(node))
                            duplicate.set("channel", extra)
                            root.append(duplicate)
            epg_programmes = len(root.findall("programme"))
            with open("epg.xml", "wb") as out:
                out.write(ET.tostring(root, encoding="utf-8", xml_declaration=True))
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
    # Additional channel-specific logo sources from IPTV-org's channel catalog.
    # Use only an exact channel ID match, never a guessed logo.
    try:
        req = urllib.request.Request("https://iptv-org.github.io/api/channels.json", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as response:
            channel_records = json.load(response)
        for ch in channel_records:
            cid = ch.get("id")
            logo = ch.get("logo")
            if cid and isinstance(logo, str) and logo.startswith("https://"):
                logo_catalog.setdefault(cid, logo)
    except Exception:
        pass
    logo_stats = Counter()
    logo_urls = set()
    for ext, _, _ in results:
        m = re.search(r'tvg-logo="([^"]*)"', ext)
        if m and m.group(1).strip():
            logo_urls.add(m.group(1).strip())
    logo_urls.update(logo_catalog.values())
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        logo_valid = dict(zip(logo_urls, pool.map(logo_works, logo_urls)))

    lines = ['#EXTM3U' + (' url-tvg="' + epg_url + '" x-tvg-url="' + epg_url + '"' if epg_status == "matched" else '')]
    for ext, url, country in results:
        logo_match = re.search(r'tvg-logo="([^"]*)"', ext)
        channel_match = re.search(r'tvg-id="([^"]*)"', ext)
        existing_logo = logo_match.group(1).strip() if logo_match else ""
        channel_id = channel_match.group(1).strip() if channel_match else ""
        if existing_logo and logo_valid.get(existing_logo, False):
            logo_stats["verified_working"] += 1
        else:
            replacement = logo_catalog.get(channel_id)
            if replacement and logo_valid.get(replacement, False):
                if logo_match:
                    ext = ext[:logo_match.start()] + 'tvg-logo="' + replacement + '"' + ext[logo_match.end():]
                else:
                    ext = add_attribute(ext, "tvg-logo", replacement)
                logo_stats["repaired_from_catalog"] += 1
            else:
                if logo_match:
                    ext = ext[:logo_match.start()] + 'tvg-logo=""' + ext[logo_match.end():]
                logo_stats["missing_or_broken"] += 1
        original_group = re.search(r'group-title="([^"]*)"', ext)
        category = original_group.group(1).strip() if original_group else ""
        display_country = {"United Kingdom / Unclassified": "United Kingdom", "United States": "United States", "Canada": "Canada", "Australia": "Australia", "England": "England", "Scotland": "Scotland"}.get(country, country)
        combined_group = display_country
        if category and 'tvg-category=' not in ext:
            ext = add_attribute(ext, "tvg-category", category)
        ext = re.sub(r'group-title="[^"]*"', lambda _: 'group-title="' + combined_group + '"', ext)
        if 'group-title=' not in ext:
            ext = add_attribute(ext, "group-title", combined_group)
        lines += [ext, url]
    if results:
        with open("jeremy.m3u", "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "candidates": len(candidates), "accepted": len(results), "by_country": dict(Counter(x[2] for x in results)), "test_results": dict(errors), "notes": ["HTTP checks do not prove playback on Sparkle TV.", "UK channels without explicit England/Scotland metadata are preserved under United Kingdom / Unclassified rather than discarded.", "Jeremy's previously approved channel numbers cannot be safely matched without his original snapshot."]}
    report["logos"] = dict(logo_stats)
    report["epg"] = {"status": epg_status, "matching_channel_ids": epg_matches, "programme_entries": epg_programmes, "error": epg_error, "source": "https://epgshare01.online/epgshare01/epg_ripper_UK1.xml.gz"}
    with open("report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    if not results:
        raise SystemExit("No valid channels: keeping any existing playlist unchanged")

if __name__ == "__main__":
    main()
