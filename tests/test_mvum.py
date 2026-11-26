import os
import sqlite3
import tempfile
import unittest

from mvum import catalog, convert

HERE = os.path.dirname(__file__)


class Scraper(unittest.TestCase):
    def test_chugach_page(self):
        with open(os.path.join(HERE, "fixtures", "chugach_mvum.html")) as f:
            links = catalog.pdf_links(f.read(), "https://www.fs.usda.gov/r10/chugach/maps-guides/motor-vehicle-use-maps")
        self.assertEqual(len(links), 3)
        self.assertIn(("https://www.fs.usda.gov/media/262762",
                       "Prince William Sound and Northern Kenai Peninsula MVUM"), links)

    def test_skips_gis_downloads(self):
        page = ('<h3>Map 1</h3><a href="/media/1">Download</a><a href="/media/2">2025 MVUM [.kmz]</a>'
                '<a href="/media/3">2025 GIS Zip File</a>')
        self.assertEqual(catalog.pdf_links(page, "https://www.fs.usda.gov/x"),
                         [("https://www.fs.usda.gov/media/1", "Map 1")])

    def test_link_inside_heading_and_duplicate_names(self):
        """White River 2026: links sit inside <h3>, summer and winter maps share district names."""
        page = ('<h2>Summer MVUMs</h2><h3><a href="/media/1">Dillon RD</a></h3>'
                '<h2>Winter MVUMs</h2><h3><a href="/media/2">Dillon RD</a></h3>')
        self.assertEqual(catalog.pdf_links(page, "https://www.fs.usda.gov/x"),
                         [("https://www.fs.usda.gov/media/1", "Dillon RD (Summer MVUMs)"),
                          ("https://www.fs.usda.gov/media/2", "Dillon RD (Winter MVUMs)")])


class Sqlitedb(unittest.TestCase):
    def test_osmand_layout(self):
        """MBTiles TMS rows must become XYZ rows; info says 'simple' numbering as OsmAnd's own writer does."""
        d = tempfile.mkdtemp()
        mbt, out = os.path.join(d, "a.mbtiles"), os.path.join(d, "a.sqlitedb")
        db = sqlite3.connect(mbt)
        db.execute("CREATE TABLE tiles (zoom_level int, tile_column int, tile_row int, tile_data blob)")
        db.execute("INSERT INTO tiles VALUES (14, 1393, 11698, x'89504e47')")  # TMS row of XYZ y=4685
        db.execute("INSERT INTO tiles VALUES (8, 21, 182, x'89504e47')")
        db.commit()
        convert.mbtiles_to_sqlitedb(mbt, out)
        c = sqlite3.connect(out)
        self.assertEqual(c.execute("SELECT tilenumbering, minzoom, maxzoom FROM info").fetchone(), ("simple", 8, 14))
        self.assertEqual(c.execute("SELECT x, y, s FROM tiles WHERE z=14").fetchone(), (1393, 4685, 0))
        self.assertEqual(c.execute("SELECT y FROM tiles WHERE z=8").fetchone(), (73,))


@unittest.skipUnless(os.environ.get("RUN_SLOW"), "set RUN_SLOW=1 (downloads + converts one real MVUM, ~2 min)")
class EndToEnd(unittest.TestCase):
    def test_chugach_map(self):
        m = {"id": "fs-262762", "name": "PWS", "url": "https://www.fs.usda.gov/media/262762"}
        [res] = convert.run_job([("MVUM e2e test", [m])], maxzoom=12)
        try:
            self.assertEqual(res["errors"], [])
            c = sqlite3.connect(res["path"])
            lo, hi = c.execute("SELECT minzoom, maxzoom FROM info").fetchone()
            self.assertEqual(hi, 12)
            # tile x at z12 for ~149W must be ~ (180-149)/360*4096 = 352
            xs = c.execute("SELECT min(x), max(x) FROM tiles WHERE z=12").fetchone()
            self.assertTrue(340 <= xs[0] <= xs[1] <= 360, xs)
            img = c.execute("SELECT image FROM tiles LIMIT 1").fetchone()[0]
            self.assertEqual(img[8:12], b"WEBP")
        finally:
            if res["path"]:
                os.remove(res["path"])


if __name__ == "__main__":
    unittest.main()
