#!/usr/bin/env python3
"""Build a country-grouped playlist from public IPTV-org feeds."""
import concurrent.futures
import csv
import io
import json
import gzip
import hashlib
import os
import xml.etree.ElementTree as ET
import re
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone

COUNTRIES = {"Brazil": "br", "United Kingdom": "uk", "Ukraine": "ua", "United States": "us", "Canada": "ca", "Australia": "au", "New Zealand": "nz"}
FREE_TV = {"Ukraine": "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_ukraine.m3u8",
           "United Kingdom": "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_uk.m3u8",
           "Brazil": "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_brazil.m3u8"}
KEEP_REGARDLESS = {"Brazil", "United Kingdom"}
DEAD = ("404", "410", "html", "unknown-media")
BASE = "https://iptv-org.github.io/iptv/countries/{}.m3u"
UA = "Jeremy-IPTV-Checker/1.0"
ISO = {"United States": "US", "Canada": "CA", "Australia": "AU", "New Zealand": "NZ", "Brazil": "BR", "United Kingdom": "GB", "Ukraine": "UA"}
DISPLAY = {}
EPG_SOURCES = [
    "https://i.mjh.nz/PlutoTV/all.xml",
    "https://epgshare01.online/epgshare01/epg_ripper_UK1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_US2.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_US_LOCALS1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_US_SPORTS1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_CA2.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_AU1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_NZ1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_UA1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_BR1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_BR2.xml.gz",
]
EPG_DAYS = 4
# Canais principais do Reino Unido: se o teste (feito fora do país) não alcança o servidor, mantém mesmo assim.
MAIN_UK = re.compile(r"^(BBC (One|Two|Three|Four|News|Parliament)|ITV ?[1-4X]?\b|ITVBe|Channel ?[45]\b|E4|More ?4|Film ?4|4Music|Dave|Sky News|Quest|Really|Yesterday)", re.I)

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


TVLOGOS_FOLDER = {"United States": "united-states", "Canada": "canada", "Australia": "australia", "New Zealand": "new-zealand", "Brazil": "brazil", "United Kingdom": "united-kingdom", "Ukraine": "ukraine"}
RAW_BASE = "https://raw.githubusercontent.com/adailtononline-cmd/Jeremy/main/"
FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]

PLUTO_URL = "https://i.mjh.nz/PlutoTV/all.xml"
_PLUTO = {}

def pluto_bytes():
    """Guia do Pluto TV (i.mjh.nz): baixado uma vez, casa pelo ID do canal que está no link da playlist."""
    if "data" not in _PLUTO:
        try:
            req = urllib.request.Request(PLUTO_URL, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as response:
                _PLUTO["data"] = response.read(120_000_000)
        except Exception:
            _PLUTO["data"] = b""
    return _PLUTO["data"]

def pluto_id(url):
    m = re.match(r"https://jmp2\.uk/plu-([0-9a-f]+)\.m3u8", url or "")
    return m.group(1) if m else ""

def pluto_icons():
    text = pluto_bytes().decode("utf-8", "ignore")
    return {m.group(1): m.group(2) for m in re.finditer(r'<channel id="([0-9a-f]+)">.*?<icon src="([^"]+)"', text, re.S)}


def slug_of(base_id, title):
    return re.sub(r"[^a-z0-9]+", "-", (base_id or "").lower()).strip("-") or hashlib.md5(title.encode()).hexdigest()[:10]

def localize_logo(job):
    """Baixa o logo, reduz para no máximo 256 px e guarda em logos/<slug>.png (o repositório serve; imgur e outros limitam acesso)."""
    slug, url = job
    try:
        from PIL import Image
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as response:
            raw = response.read(3_000_000)
        img = Image.open(io.BytesIO(raw))
        img.load()
        img = img.convert("RGBA")
        img.thumbnail((256, 256))
        os.makedirs("logos", exist_ok=True)
        img.save("logos/" + slug + ".png", optimize=True)
        return slug, True
    except Exception:
        return slug, False

def plain_title(title):
    return re.sub(r"\s*(\([^)]*\)|\[[^\]]*\]|[ⒼⓈⓎ])", "", title).strip()

def tvlogos_index():
    """Logos do projeto tv-logo/tv-logos, indexados por pasta de país + nome normalizado (só igualdade exata)."""
    data = fetch_json("https://api.github.com/repos/tv-logo/tv-logos/git/trees/main?recursive=1")
    index = {}
    for node in (data.get("tree", []) if isinstance(data, dict) else []):
        p = node.get("path", "")
        m = re.match(r"countries/([^/]+)/([^/]+)\.png$", p)
        if not m:
            continue
        folder, stem = m.groups()
        stem = re.sub(r"-(us|ca|au|nz|br|uk|gb)$", "", stem)
        stem = re.sub(r"-(hd|sd|4k)$", "", stem)
        index.setdefault((folder, norm(stem.replace("-", " "))), "https://raw.githubusercontent.com/tv-logo/tv-logos/main/" + p.replace(" ", "%20"))
    return index

def placeholder_logo(title, slug):
    """Logo gerado com o nome do canal, para o canal nunca ficar sem identificação."""
    from PIL import Image, ImageDraw, ImageFont
    digest = int(hashlib.md5(slug.encode()).hexdigest(), 16)
    palette = [(24, 54, 98), (92, 38, 110), (21, 94, 84), (122, 45, 45), (60, 70, 20), (30, 30, 30), (16, 76, 120), (110, 70, 15)]
    bg = palette[digest % len(palette)]
    size = 512
    img = Image.new("RGB", (size, size), bg)
    draw = ImageDraw.Draw(img)
    name = plain_title(title) or title
    font_path = next((f for f in FONTS if os.path.exists(f)), None)
    for px in (84, 72, 62, 52, 44, 36):
        font = ImageFont.truetype(font_path, px) if font_path else ImageFont.load_default()
        lines, line = [], ""
        for word in name.split():
            trial = (line + " " + word).strip()
            if draw.textlength(trial, font=font) <= size - 60:
                line = trial
            else:
                if line:
                    lines.append(line)
                line = word
        if line:
            lines.append(line)
        height = len(lines) * (px + 10)
        if height <= size - 80 and all(draw.textlength(l, font=font) <= size - 40 for l in lines):
            break
    y = (size - len(lines) * (px + 10)) // 2
    for l in lines:
        w = draw.textlength(l, font=font)
        draw.text(((size - w) / 2, y), l, font=font, fill=(255, 255, 255))
        y += px + 10
    os.makedirs("logos", exist_ok=True)
    img.save("logos/" + slug + ".png", optimize=True)
    return RAW_BASE + "logos/" + slug + ".png"

def norm(s):
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\([^)]*\)|\[[^]]*\]", "", (s or "").lower()))

def build_epg(results, now):
    """Une os guias de várias fontes e guarda só canais da playlist e os próximos EPG_DAYS dias."""
    index = {}
    for ext, link, _ in results:
        pid = attr(ext, "tvg-id")
        if not pid:
            continue
        if pluto_id(link):
            index.setdefault(pluto_id(link), set()).add(pid)
        for key in {pid, pid.split("@")[0], norm(title_of(ext)), norm(pid.split("@")[0].split(".")[0])}:
            if key and (key == pid or key == pid.split("@")[0] or len(key) >= 4):
                index.setdefault(key, set()).add(pid)
    matched = set()
    out_channels, out_programmes = [], []
    per_source = {}
    lo, hi = now - 24 * 3600, now + EPG_DAYS * 24 * 3600
    for url in EPG_SOURCES:
        try:
            if url == PLUTO_URL:
                data = pluto_bytes()
            else:
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
    curated = set()
    errors = Counter()
    seen = set()
    seen_titles = set()
    for country, furl in FREE_TV.items():
        try:
            req = urllib.request.Request(furl, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as response:
                source = response.read().decode("utf-8-sig")
            for ext, url in parse(source):
                if url in seen or "youtube.com" in url or "youtu.be" in url:
                    continue
                seen.add(url)
                seen_titles.add((country, norm(plain_title(title_of(ext)))))
                curated.add(url)
                candidates.append((ext, url, country))
        except Exception:
            errors["source_freetv_" + country] += 1
    for country, code in COUNTRIES.items():
        try:
            req = urllib.request.Request(BASE.format(code), headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as response:
                source = response.read().decode("utf-8-sig")
            for ext, url in parse(source):
                assigned = country
                if url in seen:
                    continue
                seen.add(url)
                key = (country, norm(plain_title(title_of(ext))))
                if country in FREE_TV and key in seen_titles and norm(plain_title(title_of(ext))) in {k[1] for k in seen_titles if k[0] == country}:
                    errors["duplicate_of_curated"] += 1
                    continue
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
            elif item[2] in KEEP_REGARDLESS and reason not in DEAD:
                # Brasil e Reino Unido: o teste roda fora desses países, então bloqueio regional ou falta de resposta
                # não prova link morto. Só sai o que é claramente morto (404/410) ou não é vídeo.
                results.append(item)
                errors["kept_br_uk_not_verified"] += 1
            else:
                rejected.append((reason, item[2], title_of(item[0]), item[1]))
    order = list(COUNTRIES)

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

    # segunda passada: canais ainda sem logo que funcione -> tenta o projeto tv-logos (nome exato no país)
    tv_index = tvlogos_index()
    extra_urls = {}
    icons = pluto_icons()
    for ext, link, country in results:
        base_id = attr(ext, "tvg-id").split("@")[0]
        if valid_logo.get(attr(ext, "tvg-logo"), False):
            continue
        if any(valid_logo.get(u, False) for _, u in catalog.get(base_id, [])):
            continue
        pico = icons.get(pluto_id(link))
        if pico and pico.startswith("https://"):
            extra_urls[base_id] = pico
            catalog.setdefault(base_id, []).append((0, pico))
            continue
        url = tv_index.get((TVLOGOS_FOLDER.get(country, ""), norm(plain_title(title_of(ext)))))
        if url:
            extra_urls[base_id] = url
            catalog.setdefault(base_id, []).append((3, url))
    todo = set(extra_urls.values()) - set(valid_logo)
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        valid_logo.update(zip(todo, pool.map(logo_works, todo)))

    plan = {}      # slug -> endereço externo do logo
    origin = {}    # índice do canal -> (slug, "original" | "repaired" | "none")
    for n, (ext, _, country) in enumerate(results):
        base_id = attr(ext, "tvg-id").split("@")[0]
        slug = slug_of(base_id, title_of(ext))
        logo = attr(ext, "tvg-logo")
        if logo and valid_logo.get(logo, False):
            plan.setdefault(slug, logo); origin[n] = (slug, "original")
        else:
            fixed = next((u for _, u in sorted(catalog.get(base_id, [])) if valid_logo.get(u, False)), "")
            if fixed:
                plan.setdefault(slug, fixed); origin[n] = (slug, "repaired")
            else:
                origin[n] = (slug, "none")
    if os.path.isdir("logos"):
        for old in os.listdir("logos"):
            os.remove(os.path.join("logos", old))
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        stored = dict(pool.map(localize_logo, plan.items()))

    rows = []
    logo_stats = Counter()
    missing_logos = []
    for n, (ext, url, country) in enumerate(results):
        pid = attr(ext, "tvg-id")
        base_id, _, feed = pid.partition("@")
        meta = channels_meta.get(base_id)
        title = plain_title(title_of(ext)) or title_of(ext)
        ext = ext[:len(ext) - len(title_of(ext))] + title
        slug, kind = origin[n]
        if kind != "none" and stored.get(slug, False):
            ext = set_attr(ext, "tvg-logo", RAW_BASE + "logos/" + slug + ".png")
            logo_stats["stored_" + kind] += 1
        else:
            ext = set_attr(ext, "tvg-logo", placeholder_logo(title, slug))
            logo_stats["placeholder_with_channel_name"] += 1
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
        rows.append((order.index(country), 0 if url in curated else 1, title.lower(), ext, url, country))
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
        "by_category": {},
        "test_results": dict(errors),
        "notes": [
            "HTTP checks do not prove playback on Sparkle TV.",
            "Groups are countries only, sorted by channel name (the category stays in tvg-category). Resolution and tags were removed from displayed names. England and Scotland are merged into United Kingdom.",
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
