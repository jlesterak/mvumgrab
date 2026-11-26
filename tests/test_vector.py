import json
import os
import shutil
import subprocess
import tempfile
import unittest
import xml.dom.minidom

from mvum import convert, vector


def feat(props, coords=((-149.0, 60.0), (-149.1, 60.1))):
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "LineString", "coordinates": [list(c) for c in coords]} if coords else None}


ROAD = {"kind": "road", "id": "4460100", "name": "RESURRECTION CREEK RD", "symbol": "4", "seasonal": "seasonal",
        "passengervehicle": "open", "passengervehicle_datesopen": "05/01-10/31",
        "atv": "open", "atv_datesopen": "06/15-10/31", "districtname": "Seward Ranger District"}


class Gpx(unittest.TestCase):
    def test_routes_merge_and_rules_text(self):
        """Segments of one route with the same access become one trk; a different access splits it."""
        other = dict(ROAD, passengervehicle_datesopen="01/01-12/31")
        feats = [feat(ROAD), feat(ROAD, ((-149.1, 60.1), (-149.2, 60.2))), feat(other)]
        doc = xml.dom.minidom.parseString(vector.gpx("Chugach National Forest", feats))
        trks, wpts = doc.getElementsByTagName("trk"), doc.getElementsByTagName("wpt")
        self.assertEqual((len(trks), len(wpts)), (2, 2))
        self.assertEqual(len(trks[0].getElementsByTagName("trkseg")), 2)
        desc = trks[0].getElementsByTagName("desc")[0].firstChild.data
        self.assertIn("highway-legal only, seasonal", desc)
        self.assertIn("passenger car: 05/01-10/31", desc)
        self.assertIn("ATV: 06/15-10/31", desc)
        self.assertEqual(trks[0].getElementsByTagName("name")[0].firstChild.data, "FS 4460100 Resurrection Creek Rd")
        self.assertEqual(trks[0].getElementsByTagName("osmand:color")[0].firstChild.data, "#1f5fd1")


class Split(unittest.TestCase):
    def test_per_forest_files_counts_and_name_fallback(self):
        d = tempfile.mkdtemp()
        try:
            seq = os.path.join(d, "x.geojsonl")
            rows = [feat({"kind": "trail", "FORESTNAME": "A NF", "ID": "1", "NAME": None, "name_alt": "Bear Lake"}),
                    feat({"kind": "road", "FORESTNAME": "B NF", "ID": "2", "geom_source": None}),
                    feat({"kind": "road", "FORESTNAME": "B NF", "ID": "3"}, coords=None)]
            with open(seq, "w") as fh:
                fh.writelines("\x1e" + json.dumps(r) + "\n" for r in rows)
            index = {}
            vector.split(seq, d, index)
            self.assertEqual((index["A NF"]["trails"], index["B NF"]["roads"]), (1, 1))
            with open(os.path.join(d, "a-nf.geojsonl")) as fh:
                p = json.loads(fh.readline())["properties"]
            self.assertEqual(p["name"], "Bear Lake")
            self.assertNotIn("name_alt", p)
        finally:
            shutil.rmtree(d)


def have_gdal():
    if convert.LOCAL_GDAL:
        return True
    return shutil.which("docker") and subprocess.run(["docker", "image", "inspect", convert.IMAGE],
                                                     capture_output=True).returncode == 0


@unittest.skipUnless(have_gdal(), "needs GDAL (local or the Docker image)")
class GapFill(unittest.TestCase):
    """Trail records without a line: a duplicate of a lined record is dropped; an orphan gets the NFS line cut
    to its mileposts; one with no NFS match is counted missing."""

    def test_fill_and_missing(self):
        os.makedirs(convert.DATA, exist_ok=True)
        d = tempfile.mkdtemp(dir=convert.DATA)
        try:
            base = {"FORESTNAME": "T NF", "SYMBOL": "5", "SEASONAL": "yearlong"}
            trail = [dict(base, ID="1", RTE_CN="c1", BMP=0, EMP=1, NAME=None, _g=[(0, 0), (0, 1)]),
                     dict(base, ID="1", RTE_CN="c1", BMP=0, EMP=1, NAME="DUP NAME", _g=None),   # duplicate
                     dict(base, ID="2", RTE_CN="c2", BMP=1, EMP=2, NAME="ORPHAN", _g=None),     # fill from nfs
                     dict(base, ID="3", RTE_CN="c3", BMP=0, EMP=1, NAME="NOWHERE", _g=None)]    # missing
            nfs = [{"TRAIL_CN": "c2", "BMP": 0, "EMP": 4, "_g": [(1, 0), (1, 4)]}]
            road = [dict(base, ID="9", BMP=0, EMP=1, SURFACETYPE="NAT", OPERATIONALMAINTLEVEL="2", _g=[(2, 0), (2, 1)])]

            def write(name, rows):
                pad = {} if name == "nfs" else {f.upper(): "" for f in vector.FIELDS
                                                 if name == "road" or f not in vector.ROAD_ONLY}
                fc = {"type": "FeatureCollection", "features": [
                    {"type": "Feature", "properties": dict(pad, **{k: v for k, v in r.items() if k != "_g"}),
                     "geometry": {"type": "MultiLineString", "coordinates": [r["_g"]]} if r["_g"] else None}
                    for r in rows]}
                path = os.path.join(d, name + ".geojson")
                json.dump(fc, open(path, "w"))
                return path

            gpkg = os.path.join(d, "t.gpkg")
            for key, rows in [("road", road), ("trail", trail), ("nfs", nfs)]:
                convert.gdal("ogr2ogr", "-f", "GPKG", *(["-update"] if os.path.exists(gpkg) else []), gpkg,
                             write(key, rows), "-nln", key, "-nlt", "MULTILINESTRING", "-lco", "GEOMETRY_NAME=SHAPE")
            sqls, missing = vector.queries()
            got = {}
            for part, sql in sqls.items():
                out = os.path.join(d, part + ".json")
                open(os.path.join(d, part + ".sql"), "w").write(sql)
                convert.gdal("ogr2ogr", "-f", "GeoJSON", out, gpkg, "-sql", "@" + os.path.join(d, part + ".sql"))
                got[part] = json.load(open(out))["features"]
            self.assertEqual(len(got["road"]), 1)
            self.assertEqual([(f["properties"]["id"], f["properties"]["name_alt"]) for f in got["trail"]],
                             [("1", "DUP NAME")])
            [filled] = got["filled"]
            self.assertEqual(filled["properties"]["id"], "2")
            ys = [pt[1] for ln in vector.lines(filled["geometry"]) for pt in ln]  # mileposts 1-2 of a 0-4 line along y
            self.assertAlmostEqual(min(ys), 1, places=3)
            self.assertAlmostEqual(max(ys), 2, places=3)
            self.assertRegex(convert.gdal("ogrinfo", "-q", "-sql", missing, gpkg), r"n \(Integer\) = 1\b")
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
