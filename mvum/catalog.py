"""Build a catalog of USFS Motor Vehicle Use Map PDFs: region -> forest -> maps.

Sources, per forest:
  1. the forest sitemap -> pages whose URL says motor-vehicle-use / mvum -> PDF links on them
  2. fallback: the forest's /maps-guides page -> links whose URL or text says MVUM / motor vehicle
  3. fallback: the regional MVUM footprint layers (R4, R5 publish direct PDF links there)
A forest whose MVUMs are only in the Avenza store is flagged "avenza-only".

fs.usda.gov sits behind an Azure WAF that sometimes serves a JS challenge to scripts;
we throttle, back off, and flag the forest "blocked" rather than "none" if it never clears.
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://www.fs.usda.gov"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
REGIONS = {
    "r01": "Northern Region", "r02": "Rocky Mountain Region", "r03": "Southwestern Region",
    "r04": "Intermountain Region", "r05": "Pacific Southwest Region", "r06": "Pacific Northwest Region",
    "r08": "Southern Region", "r09": "Eastern Region", "r10": "Alaska Region",
}
# slugs on the forests-grasslands pages that are not forests
NOT_FORESTS = {"animals-plants", "educational", "events", "about-area", "alerts", "climate", "contact-us",
               "data-tools", "fire", "forest-products", "forests-grasslands", "leadership", "maps-guides",
               "multimedia", "natural-resources", "newsroom", "offices", "passes", "permits", "planning",
               "publications", "recreation", "state-private-tribal", "wilderness", "working-with-us"}
FOOTPRINTS = "https://services1.arcgis.com/gGHDlz6USftL5Pau/arcgis/rest/services/{}/FeatureServer/0/query"
FOOTPRINT_LAYERS = {"r04": "R4_MVUM_Footprints_PUBLIC_VIEW", "r05": "R5_MVUM_Footprints_Public"}
MVUM_URL = re.compile(r"motor-?vehicle-?use|mvum", re.I)
MVUM_TEXT = re.compile(r"\bmvum|motor vehicle use", re.I)

DELAY = float(os.environ.get("MVUM_DELAY", "1.0"))
_last = [0.0]


class Blocked(Exception):
    pass


def fetch(url, tries=4):
    """GET text with throttle; retries through the WAF challenge with backoff."""
    for attempt in range(tries):
        wait = DELAY - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            body = ""
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            body = ""
        if body and "Azure WAF" not in body[:2000]:
            return body
        time.sleep(10 * (attempt + 1))
    raise Blocked(url)


def text_of(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def pdf_links(page_html, page_url):
    """Return [(absolute_url, label)] for PDF / /media/ links, labelled by link text or nearest heading."""
    out, seen = [], set()
    # headings by position: (start, level, text). Links may sit inside a heading, so scan them separately.
    heads = [(h.start(), int(h.group(1)), text_of(h.group(2)))
             for h in re.finditer(r"<h([1-6])[^>]*>(.*?)</h\1>", page_html, re.S | re.I)]
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", page_html, re.S | re.I):
        before = [h for h in heads if h[0] < m.start() and h[2]]
        heading = before[-1][2] if before else ""
        section = next((h[2] for h in reversed(before) if h[1] <= 2), "")
        href = re.search(r'href="([^"]+)"', m.group(1) or "")
        if not href:
            continue
        url = urllib.parse.urljoin(page_url, html.unescape(href.group(1)))
        p = urllib.parse.urlparse(url)
        is_pdf = p.path.lower().endswith(".pdf") or re.fullmatch(r"/media/\d+", p.path)
        if not is_pdf or not (p.netloc.endswith("fs.usda.gov") or p.netloc.endswith("box.com")):
            continue
        if url in seen:
            continue
        seen.add(url)
        label = text_of(m.group(2))
        if re.search(r"\.(kmz|kml|zip|gpx|shp|gdb)\b|\b(kmz|kml|zip|gis data|shapefile)\b", label, re.I):
            continue
        if not label or re.fullmatch(r"(download|view|pdf|here|click here|map)( pdf)?", label, re.I):
            label = heading or os.path.basename(p.path)
        out.append([url, label, section])
    # same label twice (e.g. summer and winter maps per district): add the section heading
    counts = {}
    for _, label, _ in out:
        counts[label] = counts.get(label, 0) + 1
    return [(u, f"{l} ({s})" if counts[l] > 1 and s and s != l else l) for u, l, s in out]


def map_id(url):
    m = re.search(r"/media/(\d+)", url)
    if m:
        return "fs-" + m.group(1)
    return "u-" + re.sub(r"[^A-Za-z0-9]+", "-", urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])[-60:]


def list_forests(region):
    page = fetch(f"{BASE}/{region}/forests-grasslands") or ""
    forests = {}
    for m in re.finditer(rf'<a[^>]+href="(?:{re.escape(BASE)})?/{region}/([a-z0-9-]+)"[^>]*>(.*?)</a>', page, re.S):
        slug, name = m.group(1), text_of(m.group(2))
        if slug in NOT_FORESTS:
            continue
        if slug not in forests or len(name) > len(forests[slug]):
            forests[slug] = name or slug
    return [{"slug": s, "name": n or s} for s, n in sorted(forests.items())]


def footprint_maps(region):
    """Regional footprint layer rows -> {forest_fullname_lower: [(url, label)]}."""
    layer = FOOTPRINT_LAYERS.get(region)
    if not layer:
        return {}
    q = urllib.parse.urlencode({"where": "1=1", "outFields": "*", "returnGeometry": "false", "f": "json"})
    try:
        with urllib.request.urlopen(FOOTPRINTS.format(layer) + "?" + q, timeout=60) as r:
            rows = json.load(r).get("features", [])
    except Exception:
        return {}
    out = {}
    for f in rows:
        a = f["attributes"]
        url = a.get("Download_URL") or a.get("View_URL") or ""
        if not url.lower().endswith(".pdf"):
            continue
        name = (a.get("Forest_fullname") or a.get("FOREST_NAME") or "").strip()
        label = ", ".join(x.strip() for x in (a.get("P_Title"), a.get("D_Title")) if x and x.strip())
        out.setdefault(name.lower(), []).append((url, label or name))
    return out


def crawl_forest(region, forest, footprints):
    slug = forest["slug"]
    root = f"{BASE}/{region}/{slug}"
    forest.update({"url": root, "maps": [], "status": "none", "pages": []})
    try:
        sm = fetch(root + "/sitemap.xml") or ""
        locs = re.findall(r"<loc>([^<]+)</loc>", sm)
        pages = [u.replace("http://", "https://", 1) for u in locs
                 if MVUM_URL.search(u) and not re.search(r"/(alerts|newsroom)/", u)]
        links = []
        for p in pages:
            links += pdf_links(fetch(p) or "", p)
        avenza = False
        if not links:
            p = root + "/maps-guides"
            page = fetch(p) or ""
            pages.append(p)
            for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", page, re.S | re.I):
                href = re.search(r'href="([^"]+)"', m.group(1))
                if not href:
                    continue
                url, label = urllib.parse.urljoin(p, html.unescape(href.group(1))), text_of(m.group(2))
                if not (MVUM_URL.search(url) or MVUM_TEXT.search(label)):
                    continue
                if "store.avenza.com" in url:
                    avenza = True
                elif re.search(r"/media/\d+$|\.pdf$", url, re.I):
                    links.append((url, label))
                elif url.startswith(root + "/") and url not in pages:
                    pages.append(url)
                    links += pdf_links(fetch(url) or "", url)
    except Blocked:
        forest["status"] = "blocked"
        return forest
    if not links:
        name = forest["name"].lower()
        for k, v in footprints.items():
            if k and (k in name or name.split()[0] in k):
                links += v
    forest["pages"] = pages
    seen = set()
    for url, label in links:
        mid = map_id(url)
        if mid in seen:
            continue
        seen.add(mid)
        forest["maps"].append({"id": mid, "name": label, "url": url})
    forest["status"] = "ok" if forest["maps"] else ("avenza-only" if avenza else "none")
    return forest


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save(cat, path):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cat, f, indent=1)
    os.replace(tmp, path)


def crawl(path, only=None, retry_blocked=False, progress=print):
    """Crawl all regions (or `only`=['r10/chugach', 'r06']) and write the catalog incrementally."""
    cat = load(path) or {"updated": None, "regions": []}
    regions = {r["id"]: r for r in cat["regions"]}
    want = only or list(REGIONS)
    for rid in REGIONS:
        if not any(w == rid or w.startswith(rid + "/") for w in want):
            continue
        reg = regions.setdefault(rid, {"id": rid, "name": REGIONS[rid], "forests": []})
        known = {f["slug"]: f for f in reg["forests"]}
        slugs = [w.split("/", 1)[1] for w in want if w.startswith(rid + "/")]
        if retry_blocked:
            forests = [f for f in reg["forests"] if f.get("status") == "blocked"]
        elif slugs:
            forests = [known.get(s) or {"slug": s, "name": s} for s in slugs]
        else:
            try:
                forests = list_forests(rid)
            except Blocked:
                progress(f"{rid}: forest list blocked")
                continue
            for f in forests:  # keep the better name we may already have
                f["name"] = f["name"] if f["name"] != f["slug"] else known.get(f["slug"], f)["name"]
        fp = footprint_maps(rid)
        for i, f in enumerate(forests, 1):
            progress(f"{rid} {i}/{len(forests)} {f['name']}")
            known[f["slug"]] = crawl_forest(rid, dict(f), fp)
            reg["forests"] = sorted(known.values(), key=lambda x: x["name"].lower())
            cat["regions"] = [regions[r] for r in REGIONS if r in regions]
            cat["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            save(cat, path)
    return cat


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Crawl USFS MVUM PDFs into a catalog")
    ap.add_argument("only", nargs="*", help="limit to regions/forests, e.g. r10 or r10/chugach")
    ap.add_argument("--retry-blocked", action="store_true")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "data", "catalog.json"))
    a = ap.parse_args()
    c = crawl(os.path.abspath(a.out), a.only or None, a.retry_blocked)
    for r in c["regions"]:
        for f in r["forests"]:
            print(f"{r['id']} {f['status']:11} {len(f.get('maps', [])):3}  {f['name']}")
    sys.exit(0)
