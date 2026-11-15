#!/usr/bin/env python3
"""Build a country-grouped playlist from public IPTV-org feeds."""
import concurrent.futures
import json
import re
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone

COUNTRIES = {"United States": "us", "Canada": "ca", "Australia": "au", "Scotland": "uk", "England": "uk"}
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
    lines = ["#EXTM3U"]
    for ext, url, country in results:
        original_group = re.search(r'group-title="([^"]*)"', ext)
        category = original_group.group(1).strip() if original_group else ""
        display_country = "UK" if country == "United Kingdom / Unclassified" else country
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
    with open("report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    if not results:
        raise SystemExit("No valid channels: keeping any existing playlist unchanged")

if __name__ == "__main__":
    main()
