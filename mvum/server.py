"""mvumgrab server, stdlib only.

/        phone app (PWA, offline after first visit): pick forests, cache their files, hand them to OsmAnd
/admin   desktop UI: browse the PDF catalog, queue conversions, list outputs
A maintenance thread keeps the vector data and the PDF catalog under 30 days old and runs queued
on-demand raster conversions one at a time.
"""
import json
import os
import re
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote

from . import catalog, convert, vector

HERE = os.path.dirname(__file__)
CATALOG = os.path.abspath(os.environ.get("MVUM_CATALOG") or os.path.join(convert.DATA, "catalog.json"))
STATE = {"crawl": None, "job": None, "vector": None}  # crawl/vector: {running, line, started}; job: see start_job
LOCK = threading.Lock()
RASTER_JOBS = {}  # forest key -> the job dict started for it
RASTER = {}  # forest key -> "queued" | "running" | "error: ..."; on-demand official-map conversions
MAX_AGE = float(os.environ.get("MVUM_MAX_AGE_DAYS", 30)) * 86400
TYPES = {".gpx": "application/gpx+xml", ".sqlitedb": "application/octet-stream"}


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def catalog_with_status():
    cat = catalog.load(CATALOG) or {"updated": None, "regions": []}
    outputs = {f for f in os.listdir(convert.OUT)} if os.path.isdir(convert.OUT) else set()
    for r in cat["regions"]:
        for f in r["forests"]:
            for m in f.get("maps", []):
                meta = convert.cached_meta(m["id"])
                if meta:
                    m["cached"] = True
                    m["size"] = meta.get("size")
                    m["modified"] = meta.get("modified")
                    if meta.get("title"):
                        m["title"] = meta["title"]
            f["output"] = next((o for o in outputs if o == out_name(f["name"]) + ".sqlitedb"), None)
    return cat


def out_name(forest_name, map_name=None):
    base = "MVUM " + re.sub(r"\s+National (Forests?|Grasslands?)(\s+and\s+Grasslands?)?$", "", forest_name).strip()
    return f"{base} - {map_name}" if map_name else base


def fkey(name):
    """Join key for the PDF catalog (fs.usda.gov names) and the vector data (EDW names)."""
    n = (name or "").lower().split("&")[0]
    n = re.sub(r"\band national grasslands?\b.*", "", n).replace("mount ", "mt ")
    return re.sub(r"\b(national|forests?|grasslands?|and)\b|[^a-z ]|\s", "", n)


def _size(name):
    path = os.path.join(convert.OUT, name)
    return os.path.getsize(path) if name and os.path.isfile(path) else None


def forests():
    """Every forest from either source, with what the phone can download for it."""
    out = {}
    cat = catalog_with_status()
    for r in cat["regions"]:
        for f in r["forests"]:
            k = fkey(f["name"])
            raster = out_name(f["name"]) + ".sqlitedb"
            out[k] = {"key": k, "name": f["name"], "region": r["name"], "bbox": None, "pdf_maps": len(f.get("maps", [])),
                      "pdf_status": f.get("status"), "vector": None,
                      "raster": {"file": raster, "size": _size(raster), "state": RASTER.get(k)}}
    for name, v in vector.load_index().items():
        k = fkey(name)
        e = out.setdefault(k, {"key": k, "name": name, "region": None, "bbox": None, "pdf_maps": 0,
                               "pdf_status": None, "raster": None})
        e["bbox"] = v["bbox"]
        gpx = vector.gpx_name(name)
        e["vector"] = {"file": gpx, "size": _size(gpx), "roads": v["roads"], "trails": v["trails"],
                       "missing": v["missing"], "updated": v["updated"]}
    for e in out.values():
        if e["raster"] and not e["pdf_maps"] and not e["raster"]["size"]:
            e["raster"] = None  # nothing to convert
    return {"updated": cat.get("updated"), "forests": sorted(out.values(), key=lambda e: e["name"].lower())}


def queue_raster(key):
    """Queue an official-map conversion for a forest; the maintenance thread runs it."""
    if not any(e["key"] == key and e["raster"] for e in forests()["forests"]):
        return False
    if RASTER.get(key) not in ("queued", "running"):
        RASTER[key] = "queued"
    return True


def _run_next_raster():
    for k, job in list(RASTER_JOBS.items()):  # settle finished jobs against their own job object
        if job["running"]:
            return
        g = (job.get("groups") or [{}])[0]
        RASTER[k] = None if g.get("state") == "done" else "error: " + str((g.get("result") or {}).get("errors")
                                                                          or job.get("error"))
        del RASTER_JOBS[k]
    if STATE["job"] and STATE["job"]["running"]:
        return
    queued = [k for k, s in RASTER.items() if s == "queued"]
    if not queued:
        return
    cat = catalog.load(CATALOG) or {"regions": []}
    forest = next((f for r in cat["regions"] for f in r["forests"] if fkey(f["name"]) == queued[0]), None)
    ids = {m["id"] for m in (forest or {}).get("maps", [])}
    if ids and start_job(ids, 14, "forest", True, "webp"):
        RASTER[queued[0]] = "running"
        RASTER_JOBS[queued[0]] = STATE["job"]
    elif not ids:
        RASTER[queued[0]] = "error: no PDF maps"


def _stale(path):
    return not os.path.exists(path) or time.time() - os.path.getmtime(path) > MAX_AGE


def start_vector(force=False):
    with LOCK:
        if STATE["vector"] and STATE["vector"]["running"]:
            return False
        STATE["vector"] = {"running": True, "line": "starting", "started": now(), "error": None}
    try:
        vector.refresh(force, progress=lambda line: STATE["vector"].update(line=line))
        if os.path.exists(vector.INDEX):
            os.utime(vector.INDEX)  # checked now, even if USFS had nothing newer
        STATE["vector"]["line"] = "done"
    except Exception as e:
        STATE["vector"]["error"] = str(e)
    finally:
        STATE["vector"]["running"] = False
    return True


def maintain(tick=15):
    """Background loop: refresh stale data (vector first: it is what the phone needs), run queued rasters."""
    last_check = 0
    while True:
        try:
            if time.time() - last_check > 3600:
                last_check = time.time()
                if _stale(vector.INDEX) or any(_size(vector.gpx_name(n)) is None for n in vector.load_index()):
                    threading.Thread(target=start_vector, daemon=True).start()  # ~6 min; don't block rasters
                if _stale(CATALOG) and not (STATE["crawl"] and STATE["crawl"]["running"]):
                    start_crawl()
            _run_next_raster()
        except Exception as e:  # never let the loop die
            print("maintain:", e, flush=True)
        time.sleep(tick)


def list_outputs():
    if not os.path.isdir(convert.OUT):
        return []
    out = []
    for f in sorted(os.listdir(convert.OUT)):
        if f.endswith(".sqlitedb"):
            st = os.stat(os.path.join(convert.OUT, f))
            out.append({"name": f, "size": st.st_size, "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
    return out


def start_crawl(only=None, retry_blocked=False):
    with LOCK:
        if STATE["crawl"] and STATE["crawl"]["running"]:
            return False
        STATE["crawl"] = {"running": True, "line": "starting", "started": now(), "error": None}

    def run():
        try:
            catalog.crawl(CATALOG, only, retry_blocked, progress=lambda line: STATE["crawl"].update(line=line))
            STATE["crawl"]["line"] = "done"
        except Exception as e:
            STATE["crawl"]["error"] = str(e)
        finally:
            STATE["crawl"]["running"] = False
    threading.Thread(target=run, daemon=True).start()
    return True


def start_job(map_ids, maxzoom, per, white, fmt="webp"):
    """Resolve ids against the catalog, group them, run in a thread."""
    cat = catalog.load(CATALOG) or {"regions": []}
    groups = []
    for r in cat["regions"]:
        for f in r["forests"]:
            chosen = [m for m in f.get("maps", []) if m["id"] in map_ids]
            if not chosen:
                continue
            if per == "map":
                for m in chosen:
                    n = out_name(f["name"], m.get("title") or m["name"])
                    if any(g[0] == n for g in groups):  # two maps with the same title: keep both files
                        n += f" ({m['id']})"
                    groups.append((n, [m]))
            else:
                groups.append((out_name(f["name"]), chosen))
    if not groups:
        return None
    with LOCK:
        if STATE["job"] and STATE["job"]["running"]:
            return False
        job = STATE["job"] = {
            "running": True, "started": now(), "maxzoom": maxzoom, "white": white, "fmt": fmt,
            "groups": [{"name": n, "maps": [{"id": m["id"], "name": m["name"], "state": "queued", "step": "",
                                              "frac": 0, "error": None} for m in ms],
                        "state": "queued", "step": "", "result": None} for n, ms in groups],
        }

    def progress(event, group=None, map=None, **kw):
        g = job["groups"][group]
        mm = next((x for x in g["maps"] if x["id"] == map), None)
        if event == "step":
            g["state"] = "running"
            g["step"] = kw["step"]
            if mm:
                mm.update(state="running", step=kw["step"], frac=kw["frac"])
        elif event == "map_done":
            mm.update(state="done", step="", frac=1)
            if kw.get("title"):
                meta = convert.cached_meta(map) or {}
                meta["title"] = kw["title"]
                with open(convert.pdf_meta_path(map), "w") as fh:
                    json.dump(meta, fh)
        elif event == "map_error":
            mm.update(state="error", error=kw["error"])
        elif event == "group_done":
            g["result"] = kw["result"]
            g["state"] = "done" if kw["result"]["path"] else "error"
            g["step"] = ""

    def run():
        try:
            convert.run_job(groups, maxzoom, white, fmt, progress=progress)
        except Exception as e:
            job["error"] = str(e)
        finally:
            job["running"] = False
            job["finished"] = now()
    threading.Thread(target=run, daemon=True).start()
    return True


APP = {
    "/": ("app/index.html", "text/html; charset=utf-8"), "/index.html": ("app/index.html", "text/html; charset=utf-8"),
    "/app.js": ("app/app.js", "text/javascript"), "/sw.js": ("app/sw.js", "text/javascript"),
    "/manifest.webmanifest": ("app/manifest.webmanifest", "application/manifest+json"),
    "/icon.svg": ("app/icon.svg", "image/svg+xml"),
    "/admin": ("index.html", "text/html; charset=utf-8"),
}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):  # client went away (e.g. a service worker refresh)
            self.close_connection = True

    def send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, name, ctype):
        with open(path, "rb") as f:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(os.path.getsize(path)))
            self.end_headers()
            while chunk := f.read(1 << 20):
                self.wfile.write(chunk)

    def do_GET(self):
        p = urlparse(self.path).path
        if p in APP:
            name, ctype = APP[p]
            with open(os.path.join(HERE, "static", name), "rb") as f:
                return self.send(200, f.read(), ctype)
        if p == "/healthz":
            return self.send(200, {"ok": True})
        if p == "/api/forests":
            return self.send(200, dict(forests(), refresh={k: STATE[k] for k in ("vector", "crawl")}))
        if p.startswith("/files/"):
            name = os.path.basename(unquote(p[7:]))
            path = os.path.join(convert.OUT, name)
            ext = os.path.splitext(name)[1]
            if ext in TYPES and os.path.isfile(path):
                return self.send_file(path, name, TYPES[ext])
        if p == "/api/catalog":
            return self.send(200, catalog_with_status())
        if p == "/api/status":
            return self.send(200, {"crawl": STATE["crawl"], "job": STATE["job"], "outputs": list_outputs(),
                                   "out_dir": convert.OUT})
        if p.startswith("/out/"):
            name = os.path.basename(unquote(p[5:]))
            path = os.path.join(convert.OUT, name)
            if name.endswith(".sqlitedb") and os.path.isfile(path):
                return self.send_file(path, name, TYPES[".sqlitedb"])
        self.send(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.send(400, {"error": "bad json"})
        m = re.fullmatch(r"/api/forests/([a-z]+)/raster", p)
        if m:
            return self.send(200, {"ok": True}) if queue_raster(m.group(1)) else self.send(404, {"error": "no PDF maps"})
        if p == "/api/refresh":
            ok = start_crawl(body.get("only") or None, bool(body.get("retry_blocked")))
            return self.send(200 if ok else 409, {"ok": ok})
        if p == "/api/convert":
            ids = set(body.get("ids") or [])
            mz = int(body.get("maxzoom") or 14)
            if mz not in (12, 13, 14, 15, 16):
                return self.send(400, {"error": "maxzoom 12-16"})
            fmt = body.get("fmt", "webp")
            if fmt not in ("webp", "png") or body.get("per", "forest") not in ("forest", "map"):
                return self.send(400, {"error": "bad option"})
            ok = start_job(ids, mz, body.get("per", "forest"), bool(body.get("white", True)), fmt)
            if ok is None:
                return self.send(400, {"error": "no maps selected"})
            return self.send(200 if ok else 409, {"ok": ok})
        if p == "/api/delete-output":
            name = os.path.basename(body.get("name", ""))
            path = os.path.join(convert.OUT, name)
            if name.endswith(".sqlitedb") and os.path.isfile(path):
                os.remove(path)
                return self.send(200, {"ok": True})
            return self.send(404, {"error": "not found"})
        self.send(404, {"error": "not found"})


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("MVUM_PORT", 8765)))
    ap.add_argument("--host", default=os.environ.get("MVUM_HOST", "127.0.0.1"))
    ap.add_argument("--no-maintain", action="store_true", help="don't refresh data or run queued conversions")
    a = ap.parse_args()
    os.makedirs(convert.OUT, exist_ok=True)
    if not a.no_maintain:
        threading.Thread(target=maintain, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), H)
    print(f"mvumgrab on http://{a.host}:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
