#!/usr/bin/env python3
"""Build a country-grouped playlist from public IPTV-org feeds."""
import concurrent.futures
import csv
import io
import json
import gzip
import os
import xml.etree.ElementTree as ET
import re
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone

COUNTRIES = {"United States": "us", "Canada": "ca", "Australia": "au", "New Zealand": "nz", "Brazil": "br", "Scotland": "uk", "England": "uk"}
BASE = "https://iptv-org.github.io/iptv/countries/{}.m3u"
UA = "Jeremy-IPTV-Checker/1.0"
ISO = {"United States": "US", "Canada": "CA", "Australia": "AU", "New Zealand": "NZ", "Brazil": "BR",
       "Scotland": "GB", "England": "GB", "United Kingdom / Unclassified": "GB"}
DISPLAY = {"United Kingdom / Unclassified": "United Kingdom"}
EPG_SOURCES = [
    "https://epgshare01.online/epgshare01/epg_ripper_UK1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_US2.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_US_LOCALS1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_US_SPORTS1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_CA2.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_AU1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_NZ1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_BR1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_BR2.xml.gz",
]
EPG_DAYS = 4

def fetch_json(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.load(response)
    except Exception:
        return []

def title_of(ext):
    """Título do canal: o texto depois da primeira vírgula FORA de aspas (atributos como http-user-agent têm vírgula)."""
    inside = False
    for i, ch in enumerate(ext):
        if ch == '"':
            inside = not inside
        elif ch == "," and not inside:
            return ext[i + 1:].strip()
    return ext.rsplit(",", 1)[-1].strip()

def attr(ext, name):
    m = re.search(r'(?<![\w-])' + re.escape(name) + r'="([^"]*)"', ext)
    return m.group(1).strip() if m else ""

def set_attr(ext, name, value):
    value = value.replace('"', "")
    m = re.search(r'(?<![\w-])' + re.escape(name) + r'="[^"]*"', ext)
    if m:
        return ext[:m.start()] + name + '="' + value + '"' + ext[m.end():]
    return add_attribute(ext, name, value)

def clean_category(ext, meta):
    raw = attr(ext, "tvg-category")
    if not raw or raw.lower() == "undefined":
        cats = (meta or {}).get("categories") or []
        raw = cats[0] if cats else ""
    first = raw.split(";")[0].strip()
    if not first or first.lower() in ("undefined", "general"):
        return "General" if first.lower() == "general" else "Other"
    return first[:1].upper() + first[1:]
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
    title = title_of(ext).lower()
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

def norm(s):
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\([^)]*\)|\[[^]]*\]", "", (s or "").lower()))

def build_epg(results, now):
    """Une os guias de várias fontes e guarda só canais da playlist e os próximos EPG_DAYS dias."""
    index = {}
    for ext, _, _ in results:
        pid = attr(ext, "tvg-id")
        if not pid:
            continue
        for key in {pid, pid.split("@")[0], norm(title_of(ext)), norm(pid.split("@")[0].split(".")[0])}:
            if key and (key == pid or key == pid.split("@")[0] or len(key) >= 4):
                index.setdefault(key, set()).add(pid)
    matched = set()
    out_channels, out_programmes = [], []
    per_source = {}
    lo, hi = now - 24 * 3600, now + EPG_DAYS * 24 * 3600
    for url in EPG_SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=90) as response:
                data = response.read(80_000_000)
            stream = io.BytesIO(gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data)
            mapping = {}
            count = 0
            for event, el in ET.iterparse(stream, events=("end",)):
                if el.tag == "channel":
                    gid = el.get("id") or ""
                    names = [norm(n.text) for n in el.findall("display-name")]
                    pids = set(index.get(gid, set())) | set(index.get(gid.split("@")[0], set()))
                    if not pids:
                        for n in names:
                            options = index.get(n, set()) if len(n) >= 4 else set()
                            if options:
                                pids |= options
                    pids -= matched
                    if pids:
                        mapping[gid] = sorted(pids)
                        matched |= pids
                        for pid in sorted(pids):
                            node = ET.fromstring(ET.tostring(el))
                            node.set("id", pid)
                            out_channels.append(node)
                    el.clear()
                elif el.tag == "programme":
                    gid = el.get("channel") or ""
                    if gid in mapping:
                        try:
                            start = datetime.strptime((el.get("start") or "")[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()
                            stop = datetime.strptime((el.get("stop") or el.get("start") or "")[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()
                        except ValueError:
                            start = stop = now
                        if start <= hi and stop >= lo:
                            for pid in mapping[gid]:
                                node = ET.fromstring(ET.tostring(el))
                                node.set("channel", pid)
                                out_programmes.append(node)
                                count += 1
                    el.clear()
            per_source[url.rsplit("/", 1)[-1]] = {"channels": len(mapping), "programmes": count}
        except Exception as exc:
            per_source[url.rsplit("/", 1)[-1]] = {"error": str(exc)[:120]}
    return matched, out_channels, out_programmes, per_source

def main():
    now = datetime.now(timezone.utc).timestamp()
    channels_meta = {c.get("id"): c for c in fetch_json("https://iptv-org.github.io/api/channels.json") if c.get("id")}
    feeds_lang = {}
    for f in fetch_json("https://iptv-org.github.io/api/feeds.json"):
        feeds_lang[(f.get("channel"), f.get("id"))] = f.get("languages") or []
    lang_names = {l.get("code"): l.get("name") for l in fetch_json("https://iptv-org.github.io/api/languages.json")}

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
                assigned = region(ext) if code == "uk" else country
                if url in seen:
                    continue
                seen.add(url)
                pid = attr(ext, "tvg-id")
                meta = channels_meta.get(pid.split("@")[0])
                if (meta and meta.get("country") == "FR") or pid.split("@")[0].lower().endswith(".fr"):
                    errors["excluded_france"] += 1
                    continue
                if meta and (meta.get("is_nsfw") or meta.get("closed")):
                    errors["excluded_closed_or_nsfw"] += 1
                    continue
                candidates.append((ext, url, assigned))
        except Exception:
            errors["source_" + code] += 1
    results = []
    rejected = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
        for item, (valid, reason) in zip(candidates, pool.map(lambda c: check(c), candidates)):
            errors[reason] += 1
            if valid:
                results.append(valid)
            else:
                rejected.append((reason, item[2], title_of(item[0]), item[1]))
    order = list(COUNTRIES) + ["United Kingdom / Unclassified"]

    # ---- logos: confere o logo atual; se quebrado, tenta TODOS os candidatos do catálogo, só por ID exato ----
    catalog = {}
    for logo in fetch_json("https://iptv-org.github.io/api/logos.json"):
        cid, u = logo.get("channel"), logo.get("url")
        if cid and isinstance(u, str) and u.startswith("https://"):
            catalog.setdefault(cid, []).append((0 if logo.get("in_use") else 1, u))
    for cid, rec in channels_meta.items():
        u = rec.get("logo")
        if isinstance(u, str) and u.startswith("https://"):
            catalog.setdefault(cid, []).append((2, u))
    current = {attr(e, "tvg-logo") for e, _, _ in results if attr(e, "tvg-logo")}
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        valid_logo = dict(zip(current, pool.map(logo_works, current)))
    need = set()
    for ext, _, _ in results:
        if not valid_logo.get(attr(ext, "tvg-logo"), False):
            need.update(u for _, u in sorted(catalog.get(attr(ext, "tvg-id").split("@")[0], [])))
    need -= set(valid_logo)
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        valid_logo.update(zip(need, pool.map(logo_works, need)))

    rows = []
    logo_stats = Counter()
    missing_logos = []
    for ext, url, country in results:
        pid = attr(ext, "tvg-id")
        base_id, _, feed = pid.partition("@")
        meta = channels_meta.get(base_id)
        title = title_of(ext)
        # logo
        logo = attr(ext, "tvg-logo")
        if logo and valid_logo.get(logo, False):
            logo_stats["verified_working"] += 1
        else:
            fixed = next((u for _, u in sorted(catalog.get(base_id, [])) if valid_logo.get(u, False)), "")
            if fixed:
                ext = set_attr(ext, "tvg-logo", fixed)
                logo_stats["repaired_from_catalog"] += 1
            else:
                ext = set_attr(ext, "tvg-logo", "")
                logo_stats["missing_or_broken"] += 1
                missing_logos.append((country, title, pid))
        # metadados
        category = clean_category(ext, meta)
        ext = set_attr(ext, "tvg-category", category)
        ext = set_attr(ext, "tvg-country", ISO.get(country, ""))
        ext = set_attr(ext, "tvg-name", title)
        codes = feeds_lang.get((base_id, feed)) or feeds_lang.get((base_id, "SD")) or []
        names = [lang_names.get(c, c) for c in codes]
        if names:
            ext = set_attr(ext, "tvg-language", ";".join(names))
        group = DISPLAY.get(country, country)
        ext = set_attr(ext, "group-title", group)
        rows.append((order.index(country), category.lower(), title.lower(), ext, url, country))
    rows.sort(key=lambda r: r[:3])

    # ---- guia de programação ----
    ordered = [(r[3], r[4], r[5]) for r in rows]
    epg_url = "https://raw.githubusercontent.com/adailtononline-cmd/Jeremy/main/epg.xml"
    matched, ep_channels, ep_programmes, per_source = build_epg(ordered, now)
    epg_status = "matched" if matched else "no_matching_channel_ids"
    if matched:
        root = ET.Element("tv", {"generator-info-name": "Jeremy IPTV"})
        for node in ep_channels + ep_programmes:
            root.append(node)
        with open("epg.xml", "wb") as out:
            out.write(ET.tostring(root, encoding="utf-8", xml_declaration=True))
    header = '#EXTM3U' + (' url-tvg="' + epg_url + '" x-tvg-url="' + epg_url + '"' if matched else '')
    lines = [header]
    for ext, url, _ in ordered:
        lines += [ext, url]
    if rows:
        with open("jeremy.m3u", "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    os.makedirs("reports", exist_ok=True)
    with open("reports/rejected_links.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["reason", "country", "channel", "url"])
        w.writerows(sorted(rejected))
    with open("reports/missing_logos.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["country", "channel", "tvg_id"])
        w.writerows(sorted(missing_logos))

    with_epg = sum(1 for e, _, _ in ordered if attr(e, "tvg-id") in matched)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidates": len(candidates),
        "accepted": len(rows),
        "by_country": dict(Counter(r[5] for r in rows)),
        "by_category": dict(Counter(r[1] for r in rows).most_common()),
        "test_results": dict(errors),
        "notes": [
            "HTTP checks do not prove playback on Sparkle TV.",
            "Groups are countries only (the category stays in tvg-category). UK channels without explicit England/Scotland metadata stay under United Kingdom.",
            "France is excluded; closed or adult channels are excluded using the iptv-org catalog.",
            "Jeremy's previously approved channel numbers cannot be safely matched without his original snapshot.",
        ],
        "logos": dict(logo_stats),
        "epg": {"status": epg_status, "channels_with_guide": with_epg, "channels_total": len(rows),
                "programme_entries": len(ep_programmes), "window_days": EPG_DAYS, "sources": per_source},
    }
    with open("report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    if not rows:
        raise SystemExit("No valid channels: keeping any existing playlist unchanged")

if __name__ == "__main__":
    main()
