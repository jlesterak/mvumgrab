"""MVUM GeoPDF -> OsmAnd .sqlitedb overlay.

Per map:  download (cached) -> gdalinfo -> gdalwarp (render at the DPI the target zoom needs,
          clip to the map neatline, EPSG:3857 on the exact zoom grid) -> optional white->transparent
Per output (one map, or a whole forest): gdalwarp-merge the clipped tifs -> MBTiles + overviews
          (WebP tiles by default: ~4x smaller than PNG; Android decodes WebP with alpha)
          -> OsmAnd sqlitedb in OsmAnd's own writer layout (tilenumbering 'simple', XYZ rows).
GDAL runs in the osgeo/gdal ubuntu-full Docker image (the small image cannot read PDFs).
"""
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.request

from .catalog import UA

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA = os.path.join(ROOT, "data")
PDF_DIR = os.path.join(DATA, "pdf")
WORK = os.path.join(DATA, "work")
OUT = os.path.join(ROOT, "out")
IMAGE = os.environ.get("MVUM_GDAL_IMAGE", "ghcr.io/osgeo/gdal:ubuntu-full-latest")
WEB_MERC_RES0 = 156543.03392804097  # m/px at zoom 0
MIN_DPI, MAX_DPI = 72, 400


class ConvertError(Exception):
    pass


def gdal(*args, stdin=None):
    """Run a GDAL command in Docker with ROOT mounted at the same path, as the calling user."""
    cmd = ["docker", "run", "--rm", "-i", "--user", f"{os.getuid()}:{os.getgid()}",
           "-v", f"{ROOT}:{ROOT}", "-w", ROOT, "-e", "HOME=/tmp", IMAGE, *args]
    p = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if p.returncode != 0:
        raise ConvertError(f"{args[0]} failed: {(p.stderr or p.stdout).strip()[-800:]}")
    return p.stdout


# ---------- download ----------

def _head(url):
    """Size/date/type of a remote file. Box.com answers HEAD with 404, so fall back to a 1-byte ranged GET."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            return {"final_url": r.geturl(), "size": int(r.headers.get("Content-Length") or 0),
                    "modified": r.headers.get("Last-Modified") or "", "type": r.headers.get("Content-Type") or ""}
    except urllib.error.HTTPError:
        pass
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        total = (r.headers.get("Content-Range") or "").rpartition("/")[2]
        return {"final_url": r.geturl(), "size": int(total) if total.isdigit() else 0,
                "modified": r.headers.get("Last-Modified") or "", "type": r.headers.get("Content-Type") or ""}


def pdf_meta_path(map_id):
    return os.path.join(PDF_DIR, map_id + ".json")


def cached_meta(map_id):
    try:
        with open(pdf_meta_path(map_id)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def download(m, progress=lambda *_: None):
    """Fetch m['url'] to data/pdf/<id>.pdf unless the cached copy still matches size + Last-Modified."""
    os.makedirs(PDF_DIR, exist_ok=True)
    path = os.path.join(PDF_DIR, m["id"] + ".pdf")
    old = cached_meta(m["id"])
    try:
        head = _head(m["url"])
    except Exception as e:
        if old and os.path.exists(path):
            return path, old  # offline or blocked: use the cache
        raise ConvertError(f"cannot reach {m['url']}: {e}")
    if old and os.path.exists(path) and old.get("size") == head["size"] and old.get("modified") == head["modified"]:
        return path, old
    if head["type"] and "pdf" not in head["type"].lower() and "octet" not in head["type"].lower():
        raise ConvertError(f"not a PDF ({head['type']}): {m['url']}")
    tmp = path + ".part"
    req = urllib.request.Request(m["url"], headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        total, done = head["size"], 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            progress("download", done / total if total else 0)
    with open(tmp, "rb") as f:
        if f.read(5) != b"%PDF-":
            os.remove(tmp)
            raise ConvertError(f"download is not a PDF: {m['url']}")
    os.replace(tmp, path)
    head["fetched"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(pdf_meta_path(m["id"]), "w") as f:
        json.dump(head, f)
    return path, head


# ---------- per-map warp ----------

def zoom_res(z):
    return WEB_MERC_RES0 / (1 << z)


def inspect(pdf):
    """Return a list of georeferenced pages: {src, title, neatline, wkt, px72, lat}."""
    info = json.loads(gdal("gdalinfo", "-json", "-mdd", "all", pdf))
    subs = [v for k, v in info.get("metadata", {}).get("SUBDATASETS", {}).items() if k.endswith("_NAME")]
    infos = [(s, json.loads(gdal("gdalinfo", "-json", "-mdd", "all", s))) for s in subs] if subs else [(pdf, info)]
    pages = []
    for src, i in infos:
        wkt = (i.get("coordinateSystem") or {}).get("wkt")
        if not wkt or not i.get("geoTransform"):
            continue
        md = i.get("metadata", {}).get("", {})
        lat = i.get("wgs84Extent", {}).get("coordinates", [[[0, 45]]])[0]
        lat = sum(p[1] for p in lat) / len(lat)
        pages.append({"src": src, "title": md.get("TITLE", ""), "neatline": md.get("NEATLINE", ""),
                      "wkt": wkt, "px72": abs(i["geoTransform"][1]) * 72 / float(md.get("DPI", 72)), "lat": lat})
    if not pages:
        raise ConvertError("PDF has no georeferencing (not a GeoPDF)")
    return pages


def warp_map(pdf, map_id, maxzoom, white_transparent, progress=lambda *_: None, work=WORK):
    """Each georeferenced page -> work/<id>[_pN].tif in EPSG:3857 on the maxzoom grid. Returns (tifs, title)."""
    os.makedirs(work, exist_ok=True)
    pages = inspect(pdf)
    res = zoom_res(maxzoom)
    tifs = []
    for n, pg in enumerate(pages):
        ground = res * math.cos(math.radians(pg["lat"]))  # metres per target pixel on the ground
        dpi = int(min(MAX_DPI, max(MIN_DPI, 72 * pg["px72"] / ground)))
        out = os.path.join(work, f"{map_id}_p{n}.tif")
        args = ["gdalwarp", "-q", "-overwrite", "--config", "GDAL_PDF_DPI", str(dpi),
                "-t_srs", "EPSG:3857", "-tr", repr(res), repr(res), "-tap", "-r", "bilinear",
                "-dstalpha", "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES", "-wo", "NUM_THREADS=ALL_CPUS"]
        if pg["neatline"]:
            cut = os.path.join(work, f"{map_id}_p{n}_cut.csv")
            srs = os.path.join(work, f"{map_id}_p{n}.wkt")
            with open(cut, "w") as f:
                f.write(f'id,WKT\n1,"{pg["neatline"]}"\n')
            with open(srs, "w") as f:
                f.write(pg["wkt"])
            args += ["-cutline", cut, "-cutline_srs", srs, "-crop_to_cutline"]
        progress("render", (n + 0.1) / len(pages))
        gdal(*args, pg["src"], out)
        if white_transparent:
            gdal("python3", "-c", WHITE_TO_ALPHA, out)
        tifs.append(out)
    return tifs, pages[0]["title"]


# Runs inside the GDAL container (has numpy): alpha=0 where the pixel is near-white.
WHITE_TO_ALPHA = r"""
import sys
from osgeo import gdal
import numpy as np
ds = gdal.Open(sys.argv[1], gdal.GA_Update)
b = [ds.GetRasterBand(i) for i in (1, 2, 3, 4)]
bx, by = b[0].GetBlockSize()
for y in range(0, ds.RasterYSize, max(by, 512)):
    h = min(max(by, 512), ds.RasterYSize - y)
    r, g, bl, a = (x.ReadAsArray(0, y, ds.RasterXSize, h) for x in b)
    a[(r > 238) & (g > 238) & (bl > 238)] = 0
    b[3].WriteArray(a, 0, y)
ds.FlushCache()
"""


# ---------- per-output tiling ----------

def build_output(tifs, name, maxzoom, fmt="webp", progress=lambda *_: None, work=WORK):
    """Merge tifs -> MBTiles (zoom maxzoom-6..maxzoom) -> out/<name>.sqlitedb. Returns the path."""
    os.makedirs(OUT, exist_ok=True)
    safe = re.sub(r"[^\w .,()&-]+", "", name).strip() or "MVUM"
    stem = os.path.join(work, re.sub(r"\W+", "_", safe))
    merged, mbt = stem + "_merged.tif", stem + ".mbtiles"
    for p in (merged, mbt):
        if os.path.exists(p):
            os.remove(p)
    progress("merge", 0)
    res = zoom_res(maxzoom)
    gdal("gdalwarp", "-q", "-tr", repr(res), repr(res), "-tap", "-r", "near", "-co", "COMPRESS=DEFLATE",
         "-co", "TILED=YES", "-co", "BIGTIFF=IF_SAFER", *tifs, merged)
    progress("tiles", 0)
    tile_fmt = ["-co", "TILE_FORMAT=WEBP", "-co", "QUALITY=85"] if fmt == "webp" else ["-co", "TILE_FORMAT=PNG"]
    gdal("gdal_translate", "-q", "-of", "MBTiles", *tile_fmt, "-co", "ZOOM_LEVEL_STRATEGY=AUTO",
         "-co", f"NAME={safe}", merged, mbt)
    progress("overviews", 0)
    gdal("gdaladdo", "-q", "-r", "average", mbt, "2", "4", "8", "16", "32", "64")
    dest = os.path.join(OUT, safe + ".sqlitedb")
    mbtiles_to_sqlitedb(mbt, dest)
    for p in (merged, mbt):
        os.remove(p)
    return dest


def mbtiles_to_sqlitedb(mbtiles, dest):
    """Copy tiles into OsmAnd's sqlitedb layout, as OsmAnd's own SQLiteTileSource.createDataBase writes it."""
    src = sqlite3.connect(mbtiles)
    tmp = dest + ".part"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.execute("CREATE TABLE tiles (x int, y int, z int, s int, image blob, time long, PRIMARY KEY (x,y,z,s))")
    db.execute("CREATE TABLE info(tilenumbering,minzoom,maxzoom)")
    now = int(time.time() * 1000)
    zs = set()
    for z, x, tms_y, img in src.execute("SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"):
        zs.add(z)
        db.execute("INSERT OR REPLACE INTO tiles VALUES (?,?,?,0,?,?)", (x, (1 << z) - 1 - tms_y, z, img, now))
    if not zs:
        raise ConvertError("no tiles produced")
    db.execute("INSERT INTO info VALUES ('simple', ?, ?)", (min(zs), max(zs)))
    db.commit()
    db.close()
    src.close()
    os.replace(tmp, dest)
    return dest


# ---------- job ----------

def run_job(groups, maxzoom=14, white_transparent=True, fmt="webp", keep_work=False, progress=lambda *a, **k: None):
    """groups: [(output_name, [map, ...])]. progress(event, **kw). Returns [{name, path, size, errors}]."""
    os.makedirs(WORK, exist_ok=True)
    work = tempfile.mkdtemp(prefix="job-", dir=WORK)  # per job, so concurrent jobs never clean up each other
    results = []
    for gi, (name, maps) in enumerate(groups):
        tifs, errors = [], []
        for mi, m in enumerate(maps):
            def p(step, frac, m=m):
                progress("step", group=gi, name=name, map=m["id"], step=step, frac=frac)
            try:
                p("download", 0)
                pdf, meta = download(m, p)
                p("render", 0)
                t, title = warp_map(pdf, m["id"], maxzoom, white_transparent, p, work)
                tifs += t
                progress("map_done", group=gi, map=m["id"], title=title, size=meta.get("size"))
            except Exception as e:
                errors.append(f"{m.get('name', m['id'])}: {e}")
                progress("map_error", group=gi, map=m["id"], error=str(e))
        res = {"name": name, "path": None, "size": 0, "errors": errors}
        if tifs:
            try:
                res["path"] = build_output(tifs, name, maxzoom, fmt,
                                           lambda step, frac: progress("step", group=gi, name=name, map=None,
                                                                       step=step, frac=frac), work)
                res["size"] = os.path.getsize(res["path"])
            except Exception as e:
                res["errors"].append(str(e))
        if not keep_work:
            for t in tifs:
                os.remove(t)
        progress("group_done", group=gi, result=res)
        results.append(res)
    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)
    return results
