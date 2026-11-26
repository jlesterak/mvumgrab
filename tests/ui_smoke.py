"""Headless browser smoke test of the UI. Needs `playwright` (any venv) and a catalog that includes Chugach.

    <venv>/bin/python tests/ui_smoke.py          # browse/select checks only
    <venv>/bin/python tests/ui_smoke.py --convert  # also converts one map at zoom 13 (~1-2 min)
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PORT = 8799
URL = f"http://127.0.0.1:{PORT}/"


def main():
    convert = "--convert" in sys.argv
    srv = subprocess.Popen(["python3", "-m", "mvum.server", "--port", str(PORT)], cwd=ROOT)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(URL, timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        cat = json.load(urllib.request.urlopen(URL + "api/catalog"))
        total = sum(len(f.get("maps", [])) for r in cat["regions"] for f in r["forests"])
        with sync_playwright() as p:
            b = p.chromium.launch(args=["--mute-audio"])
            pg = b.new_page(viewport={"width": 1300, "height": 900})
            pg.on("dialog", lambda d: d.accept())
            pg.goto(URL)
            pg.wait_for_function("document.querySelector('#catinfo').textContent.includes('maps')")

            pg.fill("#filter", "chugach")
            forest = pg.locator(".forest > .row", has_text="Chugach").first
            forest.locator("input[type=checkbox]").check()
            assert "3 maps in 1 forest" in pg.text_content("#selsum"), pg.text_content("#selsum")
            print("ok  forest checkbox selects its 3 maps")

            pg.locator(".row.map input[type=checkbox]").first.uncheck()
            ind = pg.locator(".forest > .row", has_text="Chugach").first.locator("input").evaluate("e => e.indeterminate")
            assert ind, "forest checkbox should be indeterminate"
            assert "2 maps" in pg.text_content("#selsum")
            print("ok  partial selection shows indeterminate forest")

            pg.click("#clear")
            assert pg.text_content("#selsum") == "Nothing selected"
            pg.fill("#filter", "")
            pg.click("#selall")
            s = pg.text_content("#selsum")
            assert s.startswith(f"{total} maps"), (s, total)
            print(f"ok  select all shown -> {total} maps")
            pg.click("#clear")

            pg.fill("#filter", "kenai")
            names = pg.locator(".row.map .name").all_text_contents()
            assert names and all("kenai" in n.lower() for n in names), names
            print(f"ok  map-level filter: {len(names)} kenai maps")

            if convert:
                pg.fill("#filter", "copper river")
                pg.locator(".row.map input[type=checkbox]").first.check()
                pg.select_option("#maxzoom", "13")
                pg.select_option("#per", "map")
                pg.click("#go")
                pg.wait_for_selector("#job .g", timeout=10000)
                print("ok  job queued, progress shown")
                pg.wait_for_function("document.querySelector('#job').textContent.includes('Finished')", timeout=600000)
                job = pg.text_content("#job")
                assert "✗" not in job, job
                outs = pg.locator("#outs li .n").all_text_contents()
                made = [o for o in outs if "Copper" in o]
                assert made, outs
                print(f"ok  conversion finished -> {made[0]}")
                pg.screenshot(path=os.path.join(ROOT, "data", "ui_smoke.png"), full_page=False)
                urllib.request.urlopen(urllib.request.Request(URL + "api/delete-output", method="POST",
                                       data=json.dumps({"name": made[0]}).encode()))
            else:
                pg.screenshot(path=os.path.join(ROOT, "data", "ui_smoke.png"))
            b.close()
        print("UI SMOKE PASS")
    finally:
        srv.terminate()


if __name__ == "__main__":
    main()
