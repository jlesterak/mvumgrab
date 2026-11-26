# mvumgrab

Server + offline phone app (PWA): per-forest MVUM roads/trails GPX (vector) and official-map `.sqlitedb`
overlays (PDF) for OsmAnd. See README.md for use.

## Facts
- Python stdlib only. GDAL: in the container it runs in-process (image built FROM `ghcr.io/osgeo/gdal:ubuntu-full-3.13.3`,
  pinned: `:latest` is a nightly); on a dev box `convert.gdal()` uses Docker, mounting the repo at its own path as the
  calling uid. Needs the *full* image for the PDF reader.
- Vector source: FSGeodata national zips (`Trans_MVUM_Road/Trail`, `Trans_Trail_NFS_Publish`), not the EDW
  MapServer (same records, thousands of pages). ~Half the MVUM trail records have no geometry: most duplicate a
  lined record (drop, take the name); orphans get the NFS line by TRAIL_CN = RTE_CN cut to mileposts; ~4k have
  none anywhere (Tonto, Custer Gallatin, Beaverhead-Deerlodge). Lines simplified ~3 m (Sierra ships 800 pts/segment).
- Forest join between the PDF catalog and vector data is `server.fkey()` (names differ: "& X Grassland", Mount/Mt.).
- PWA needs HTTPS for its service worker (localhost is exempt). Chrome's Web Share rejects .gpx/.sqlitedb, so
  "Open in OsmAnd" is a blob download from Cache Storage.
- Catalog sources, in order: forest sitemap pages matching motor-vehicle-use/mvum → forest `/maps-guides` page
  → R4/R5 ArcGIS footprint layers (direct box.com PDFs). Forest list from `/rNN/forests-grasslands`.
- fs.usda.gov sits behind an Azure WAF that serves a JS challenge to scripts at times; `catalog.fetch` throttles
  (1 req/s, `MVUM_DELAY`) and backs off; a forest that never clears is "blocked", not "none".
- sqlitedb layout copies OsmAnd's own writer (`SQLiteTileSource.createDataBase`): `info(tilenumbering='simple',
  minzoom,maxzoom)`, `tiles(x,y,z,s,image,time)` with XYZ rows. Don't switch to BigPlanet (inverted z).
- Warp renders each PDF at the DPI the target zoom needs (capped 72–400), clips to the PDF NEATLINE, snaps to
  the Web Mercator zoom grid (`-tap`), so MBTiles AUTO zoom == chosen max zoom.
- `data/` (catalog, PDF cache, `edw/` zips, `vector/` per-forest GeoJSON lines) and `out/` are gitignored.
  `MVUM_DATA`, `MVUM_OUT`, `MVUM_CATALOG` override paths; the container uses `/config/{data,out}`.
- UI smoke test: `~/.venvs/playwright/bin/python tests/ui_smoke.py [--convert]` (phone app offline + admin UI).
- Image: `ghcr.io/jlesterak/mvumgrab`, built by GitHub Actions on `v*` tags only; version in `mvum/__init__.py`.
- Remote: none yet; planned `icebox:git/mvumgrab.git` (private) once Jake OKs creating it.
