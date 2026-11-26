"""Local web UI: browse the MVUM catalog, queue conversions, list outputs. stdlib only."""
import json
import os
import re
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote

from . import catalog, convert

HERE = os.path.dirname(__file__)
CATALOG = os.path.abspath(os.environ.get("MVUM_CATALOG") or os.path.join(convert.DATA, "catalog.json"))
STATE = {"crawl": None, "job": None}  # crawl: {running, line, started}; job: see start_job
LOCK = threading.Lock()


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


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            with open(os.path.join(HERE, "static", "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if p == "/api/catalog":
            return self.send(200, catalog_with_status())
        if p == "/api/status":
            return self.send(200, {"crawl": STATE["crawl"], "job": STATE["job"], "outputs": list_outputs(),
                                   "out_dir": convert.OUT})
        if p.startswith("/out/"):
            name = os.path.basename(unquote(p[5:]))
            path = os.path.join(convert.OUT, name)
            if name.endswith(".sqlitedb") and os.path.isfile(path):
                with open(path, "rb") as f:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                    self.send_header("Content-Length", str(os.path.getsize(path)))
                    self.end_headers()
                    while chunk := f.read(1 << 20):
                        self.wfile.write(chunk)
                return
        self.send(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.send(400, {"error": "bad json"})
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
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    os.makedirs(convert.OUT, exist_ok=True)
    srv = ThreadingHTTPServer((a.host, a.port), H)
    print(f"mvumgrab on http://{a.host}:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
