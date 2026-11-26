"""MVUM as vector data: USFS national MVUM roads/trails -> per-forest GeoJSON lines -> OsmAnd GPX.

Source: the FSGeodata national downloads (refreshed by USFS weekly-ish), not the EDW map service. Both carry
the same records; the download is one request per file instead of thousands of pages.

Trail records: about half have no geometry. Most of those duplicate a record that has it (same forest, route,
mileposts) and only add the name; the rest get their line from the National Forest System trails dataset
(TRAIL_CN = RTE_CN, cut to the record's mileposts). What is left (Tonto, Custer Gallatin, Beaverhead-Deerlodge
mostly) has no line anywhere in USFS public data and is counted as `missing`.

One GPX per forest: one <trk> per route (segments merged by route id + identical access), coloured by MVUM
class, rules in <desc>, plus one waypoint per route so a tap always shows the rules (confirmed in OsmAnd
2026-11-25).
"""
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from xml.sax.saxutils import escape

from . import convert
from .catalog import UA

SOURCES = {
    "road": "https://data.fs.usda.gov/geodata/edw/edw_resources/fc/Trans_MVUM_Road.gdb.zip",
    "trail": "https://data.fs.usda.gov/geodata/edw/edw_resources/fc/Trans_MVUM_Trail.gdb.zip",
    "nfs": "https://data.fs.usda.gov/geodata/edw/edw_resources/fc/Trans_Trail_NFS_Publish.gdb.zip",
}
EDW_DIR = os.path.join(convert.DATA, "edw")
VECTOR = os.path.join(convert.DATA, "vector")
INDEX = os.path.join(VECTOR, "index.json")

# (vehicle field, label); dates come from <field>_datesopen except where the field name is abbreviated
CLASSES = [
    ("passengervehicle", "passenger car"), ("highclearancevehicle", "high-clearance"), ("truck", "truck"),
    ("bus", "bus"), ("motorhome", "motorhome"), ("fourwd_gt50inches", "4WD >50in"),
    ("twowd_gt50inches", "2WD >50in"), ("tracked_ohv_gt50inches", "tracked OHV >50in"),
    ("other_ohv_gt50inches", "other OHV >50in"), ("atv", "ATV"), ("motorcycle", "motorcycle"),
    ("otherwheeled_ohv", "other wheeled OHV"), ("tracked_ohv_lt50inches", "tracked OHV <50in"),
    ("other_ohv_lt50inches", "other OHV <50in"),
]
DATES = {"fourwd_gt50inches": "fourwd_gt50_datesopen", "twowd_gt50inches": "twowd_gt50_datesopen",
         "tracked_ohv_gt50inches": "tracked_ohv_gt50_datesopen", "other_ohv_gt50inches": "other_ohv_gt50_datesopen",
         "tracked_ohv_lt50inches": "tracked_ohv_lt50_datesopen", "other_ohv_lt50inches": "other_ohv_lt50_datesopen"}
ROAD_ONLY = ["surfacetype", "operationalmaintlevel"]
FIELDS = ["forestname", "id", "name", "symbol", "seasonal", "districtname"] + ROAD_ONLY
FIELDS += [c for c, _ in CLASSES] + [DATES.get(c, c + "_datesopen") for c, _ in CLASSES]

# MVUM symbol -> (short class, colour). Seasonal variants share the colour; "seasonal" goes in the text.
SYMBOLS = {
    "1": ("all vehicles", "#2e9e44"), "2": ("all vehicles", "#2e9e44"),
    "3": ("highway-legal only", "#1f5fd1"), "4": ("highway-legal only", "#1f5fd1"),
    "5": ("trail, all vehicles", "#e07b00"), "6": ("trail, all vehicles", "#e07b00"),
    "7": ("trail, 50in or less", "#8e3fc7"), "8": ("trail, 50in or less", "#8e3fc7"),
    "16": ("trail, wheeled OHV <50in", "#8e3fc7"), "17": ("trail, wheeled OHV <50in", "#8e3fc7"),
    "9": ("trail, motorcycles", "#d62728"), "10": ("trail, motorcycles", "#d62728"),
    "11": ("special designation", "#8c564b"), "12": ("special designation", "#8c564b"),
}
OTHER = ("see MVUM", "#555555")
SIMPLIFY = "0.00003"  # degrees, ~3 m: some forests ship survey-grade lines (Sierra: 800 points per road segment)
EMPTY = "(SHAPE IS NULL OR ST_IsEmpty(SHAPE))"
SAME = "b.FORESTNAME = t.FORESTNAME AND b.ID = t.ID AND abs(b.BMP - t.BMP) < 0.01 AND abs(b.EMP - t.EMP) < 0.01"


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ---------- download + build ----------

def download(progress=lambda *_: None):
    """Fetch the national zips when USFS has a newer copy. Returns True if any changed."""
    os.makedirs(EDW_DIR, exist_ok=True)
    meta_path = os.path.join(EDW_DIR, "sources.json")
    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
    changed = False
    for key, url in SOURCES.items():
        dest = os.path.join(EDW_DIR, key + ".gdb.zip")
        headers = {"User-Agent": UA}
        if os.path.exists(dest) and meta.get(key):
            headers["If-Modified-Since"] = meta[key]
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
                progress(f"downloading {key} ({int(r.headers.get('Content-Length', 0)) >> 20} MB)")
                with open(dest + ".part", "wb") as fh:
                    shutil.copyfileobj(r, fh, 1 << 20)
                os.replace(dest + ".part", dest)
                meta[key] = r.headers.get("Last-Modified")
                changed = True
        except urllib.error.HTTPError as e:
            if e.code != 304:
                raise
            progress(f"{key}: unchanged")
    json.dump(meta, open(meta_path, "w"), indent=1)
    return changed


def _cols(alias, kind):
    return ", ".join(f"{alias}.{f.upper()} AS {f}" if kind == "road" or f not in ROAD_ONLY else f"NULL AS {f}"
                     for f in FIELDS)


def queries():
    """SQL (GeoPackage + SpatiaLite) for the three exports: roads, trails with lines, trails filled from NFS."""
    road = f"SELECT 'road' AS kind, {_cols('t', 'road')}, t.SHAPE FROM road t WHERE NOT {EMPTY.replace('SHAPE', 't.SHAPE')}"
    trail = (f"SELECT 'trail' AS kind, {_cols('t', 'trail')}, (SELECT b.NAME FROM trail b WHERE {SAME} AND "
             f"b.NAME IS NOT NULL LIMIT 1) AS name_alt, t.SHAPE FROM trail t WHERE NOT {EMPTY.replace('SHAPE', 't.SHAPE')}")
    f0 = "MAX(0.0, MIN(1.0, (t.BMP - n.BMP) * 1.0 / (n.EMP - n.BMP)))"
    f1 = "MAX(0.0, MIN(1.0, (t.EMP - n.BMP) * 1.0 / (n.EMP - n.BMP)))"
    filled = (f"SELECT 'trail' AS kind, {_cols('t', 'trail')}, 'nfs' AS geom_source, "
              f"CASE WHEN {f0} < 0.001 AND {f1} > 0.999 THEN n.SHAPE "
              f"ELSE COALESCE(ST_Line_Substring(CastToLineString(n.SHAPE), {f0}, {f1}), n.SHAPE) END AS SHAPE "
              f"FROM trail t JOIN nfs n ON n.TRAIL_CN = t.RTE_CN AND n.EMP > n.BMP AND n.BMP < t.EMP AND n.EMP > t.BMP "
              f"AND NOT {EMPTY.replace('SHAPE', 'n.SHAPE')} "
              f"WHERE {EMPTY.replace('SHAPE', 't.SHAPE')} AND NOT EXISTS "
              f"(SELECT 1 FROM trail b WHERE {SAME} AND NOT {EMPTY.replace('SHAPE', 'b.SHAPE')})")
    missing = (f"SELECT t.FORESTNAME AS forestname, COUNT(*) AS n FROM trail t WHERE {EMPTY.replace('SHAPE', 't.SHAPE')} "
               f"AND NOT EXISTS (SELECT 1 FROM trail b WHERE {SAME} AND NOT {EMPTY.replace('SHAPE', 'b.SHAPE')}) "
               f"AND NOT EXISTS (SELECT 1 FROM nfs n WHERE n.TRAIL_CN = t.RTE_CN AND n.EMP > n.BMP AND n.BMP < t.EMP "
               f"AND n.EMP > t.BMP AND NOT {EMPTY.replace('SHAPE', 'n.SHAPE')}) GROUP BY t.FORESTNAME")
    return {"road": road, "trail": trail, "filled": filled}, missing


def build(progress=lambda *_: None):
    """National zips -> one GeoPackage -> GeoJSON lines per forest in data/vector + index.json. Cleans up after."""
    work = os.path.join(EDW_DIR, "work")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    gpkg = os.path.join(work, "mvum.gpkg")
    try:
        for key in SOURCES:
            progress(f"unpacking {key}")
            unpacked = os.path.join(work, key)
            with zipfile.ZipFile(os.path.join(EDW_DIR, key + ".gdb.zip")) as z:
                z.extractall(unpacked)
            gdb = next(os.path.join(unpacked, d) for d in os.listdir(unpacked) if d.endswith(".gdb"))
            convert.gdal("ogr2ogr", "-f", "GPKG", *(["-update"] if os.path.exists(gpkg) else []), gpkg, gdb,
                         "-nln", key, "-dim", "XY", "-t_srs", "EPSG:4326", "-nlt", "MULTILINESTRING")
            shutil.rmtree(unpacked)
        for sql in ["CREATE INDEX ix_trail ON trail(FORESTNAME, ID)", "CREATE INDEX ix_nfs ON nfs(TRAIL_CN)"]:
            convert.gdal("ogrinfo", "-q", "-sql", sql, gpkg)
        sqls, missing_sql = queries()
        tmp = os.path.join(VECTOR + ".new")
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        index = {}
        for part, sql in sqls.items():
            progress(f"exporting {part}")
            seq = os.path.join(work, part + ".geojsonl")
            sqlfile = os.path.join(work, part + ".sql")
            open(sqlfile, "w").write(sql)
            convert.gdal("ogr2ogr", "-f", "GeoJSONSeq", seq, gpkg, "-sql", "@" + sqlfile,
                         "-simplify", SIMPLIFY, "-lco", "COORDINATE_PRECISION=6")
            split(seq, tmp, index)
            os.remove(seq)
        out = convert.gdal("ogrinfo", "-q", "-sql", missing_sql, gpkg)
        for forest, n in re.findall(r"forestname \(String\) = (.+)\n\s+n \(Integer\) = (\d+)", out):
            index.setdefault(forest, _blank(forest))["missing"] = int(n)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        for v in index.values():
            v["updated"] = stamp
        json.dump(index, open(os.path.join(tmp, "index.json"), "w"), indent=1, sort_keys=True)
        old = VECTOR + ".old"
        shutil.rmtree(old, ignore_errors=True)
        if os.path.exists(VECTOR):
            os.replace(VECTOR, old)
        os.replace(tmp, VECTOR)
        shutil.rmtree(old, ignore_errors=True)
        return index
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _blank(forest):
    return {"slug": slug(forest), "segments": 0, "roads": 0, "trails": 0, "filled": 0, "missing": 0, "bbox": None}


def split(seq, dest, index):
    """Stream a GeoJSONSeq export into dest/<slug>.geojsonl, one file per forest; update counts and bbox."""
    handles = {}
    try:
        with open(seq) as fh:
            for line in fh:
                line = line.strip().lstrip("\x1e")
                if not line:
                    continue
                f = json.loads(line)
                p = {k.lower(): v for k, v in f["properties"].items() if v not in (None, "", "N/A")}
                forest = p.pop("forestname", None)
                if not forest or not f.get("geometry"):
                    continue
                if not p.get("name") and p.get("name_alt"):
                    p["name"] = p["name_alt"]
                p.pop("name_alt", None)
                f["properties"] = p
                ix = index.setdefault(forest, _blank(forest))
                ix["segments"] += 1
                ix["roads" if p["kind"] == "road" else "trails"] += 1
                ix["filled"] += p.get("geom_source") == "nfs"
                ix["bbox"] = _grow(ix["bbox"], f["geometry"])
                if forest not in handles:
                    handles[forest] = open(os.path.join(dest, ix["slug"] + ".geojsonl"), "a")
                handles[forest].write(json.dumps(f, separators=(",", ":")) + "\n")
    finally:
        for h in handles.values():
            h.close()


def lines(geom):
    if geom["type"] == "LineString":
        return [geom["coordinates"]]
    if geom["type"] == "MultiLineString":
        return geom["coordinates"]
    return []


def _grow(bb, geom):
    for ln in lines(geom):
        for x, y, *_ in ln:
            bb = [x, y, x, y] if bb is None else [min(bb[0], x), min(bb[1], y), max(bb[2], x), max(bb[3], y)]
    return [round(v, 4) for v in bb] if bb else bb


def load_index():
    try:
        return json.load(open(INDEX))
    except (OSError, ValueError):
        return {}


def features(forest):
    with open(os.path.join(VECTOR, slug(forest) + ".geojsonl")) as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ---------- GPX ----------

def access(p):
    """[(dates, [labels])] for the classes that are open, grouped by dates."""
    by = {}
    for field, label in CLASSES:
        if str(p.get(field, "")).lower() == "open":
            by.setdefault(p.get(DATES.get(field, field + "_datesopen")) or "dates not listed", []).append(label)
    return sorted(by.items(), key=lambda kv: kv[0])


def route_title(p):
    num, name = p.get("id", ""), (p.get("name") or "").title()
    prefix = "FS " if p["kind"] == "road" else "Trail "
    return " ".join(x for x in [prefix + num if num else "", name] if x).strip() or "Unnamed " + p["kind"]


def describe(p):
    cls, _ = SYMBOLS.get(str(p.get("symbol")), OTHER)
    head = f"{cls}, {p.get('seasonal', 'dates vary')}"
    rows = [f"{', '.join(labels)}: {dates}" for dates, labels in access(p)] or ["no vehicle classes listed open"]
    level = (p.get("operationalmaintlevel") or "").split(" - ")[0]
    extra = [x for x in [(p.get("surfacetype") or "").split(" - ")[-1].capitalize(),
                         level and "maint. level " + level, p.get("districtname")] if x]
    if p.get("geom_source") == "nfs":
        extra.append("line from USFS trails data")
    return "\n".join([head] + rows + ([" · ".join(extra)] if extra else []))


def routes(feats):
    """Merge segments that share route, symbol and access into one route."""
    out = {}
    for f in feats:
        p = f["properties"]
        key = (p["kind"], p.get("id"), p.get("name"), str(p.get("symbol")),
               tuple((d, tuple(labels)) for d, labels in access(p)))
        r = out.setdefault(key, {"props": p, "segs": []})
        r["segs"] += [ln for ln in lines(f["geometry"]) if len(ln) > 1]
    return [r for r in out.values() if r["segs"]]


def gpx(name, feats):
    rs = routes(feats)
    o = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<gpx version="1.1" creator="mvumgrab" xmlns="http://www.topografix.com/GPX/1/1" '
         'xmlns:osmand="https://osmand.net/docs/technical/osmand-file-formats/osmand-gpx">',
         f"<metadata><name>{escape(name)} MVUM</name><desc>USFS Motor Vehicle Use Map roads and trails "
         f"(FSGeodata national MVUM data). Not a substitute for the official map: check closures.</desc></metadata>"]
    for r in rs:
        p = r["props"]
        seg = max(r["segs"], key=len)
        lon, lat = seg[len(seg) // 2][:2]
        _, colour = SYMBOLS.get(str(p.get("symbol")), OTHER)
        o.append(f'<wpt lat="{lat}" lon="{lon}"><name>{escape(route_title(p))}</name>'
                 f"<desc>{escape(describe(p))}</desc><type>MVUM {p['kind']}s</type>"
                 f"<extensions><osmand:color>{colour}</osmand:color><osmand:icon>special_marker</osmand:icon>"
                 f"</extensions></wpt>")
    for r in rs:
        p = r["props"]
        _, colour = SYMBOLS.get(str(p.get("symbol")), OTHER)
        o.append(f"<trk><name>{escape(route_title(p))}</name><desc>{escape(describe(p))}</desc>"
                 f"<type>MVUM {p['kind']}s</type><extensions><osmand:color>{colour}</osmand:color>"
                 f"<osmand:width>{'bold' if p['kind'] == 'road' else 'medium'}</osmand:width></extensions>")
        for seg in r["segs"]:
            o.append("<trkseg>" + "".join(f'<trkpt lat="{pt[1]}" lon="{pt[0]}"/>' for pt in seg) + "</trkseg>")
        o.append("</trk>")
    o.append("</gpx>")
    return "\n".join(o) + "\n"


def gpx_name(forest):
    return f"{forest} MVUM.gpx"


def write_gpx(forest, out_dir=None):
    out_dir = out_dir or convert.OUT
    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, gpx_name(forest))
    with open(dest + ".tmp", "w") as fh:
        fh.write(gpx(forest, features(forest)))
    os.replace(dest + ".tmp", dest)
    return dest


def refresh(force=False, progress=lambda *_: None):
    """Download if newer, rebuild per-forest data and every GPX. Returns the index."""
    changed = download(progress)
    if changed or force or not os.path.exists(INDEX):
        build(progress)
    index = load_index()
    for forest, v in sorted(index.items()):
        if v["segments"]:
            write_gpx(forest)
    progress(f"vector: {len(index)} forests, {sum(v['segments'] for v in index.values())} segments")
    return index


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Refresh national MVUM vector data and write per-forest OsmAnd GPX")
    ap.add_argument("--force", action="store_true", help="rebuild even if USFS data is unchanged")
    a = ap.parse_args()
    for forest, v in sorted(refresh(a.force, progress=print).items()):
        print(f"{v['segments']:6} seg {v['filled']:5} filled {v['missing']:5} missing  {forest}")
