# mvumgrab

Local web app: crawl USFS MVUM PDFs, convert to OsmAnd `.sqlitedb` overlays. See README.md for use.

## Facts
- Python stdlib only; GDAL runs in Docker (`ghcr.io/osgeo/gdal:ubuntu-full-latest`, needs the *full* image for the
  PDF reader). `convert.gdal()` mounts the repo at its own path and runs as the calling uid.
- Catalog sources, in order: forest sitemap pages matching motor-vehicle-use/mvum → forest `/maps-guides` page
  → R4/R5 ArcGIS footprint layers (direct box.com PDFs). Forest list from `/rNN/forests-grasslands`.
- fs.usda.gov sits behind an Azure WAF that serves a JS challenge to scripts at times; `catalog.fetch` throttles
  (1 req/s, `MVUM_DELAY`) and backs off; a forest that never clears is "blocked", not "none".
- sqlitedb layout copies OsmAnd's own writer (`SQLiteTileSource.createDataBase`): `info(tilenumbering='simple',
  minzoom,maxzoom)`, `tiles(x,y,z,s,image,time)` with XYZ rows. Don't switch to BigPlanet (inverted z).
- Warp renders each PDF at the DPI the target zoom needs (capped 72–400), clips to the PDF NEATLINE, snaps to
  the Web Mercator zoom grid (`-tap`), so MBTiles AUTO zoom == chosen max zoom.
- `data/` (catalog, PDF cache) and `out/` are gitignored. `MVUM_CATALOG` env overrides the catalog path.
- UI smoke test needs Playwright from any venv: `<venv>/bin/python tests/ui_smoke.py [--convert]`.
- Remote: none yet; planned `icebox:git/mvumgrab.git` (private) once Jake OKs creating it.
