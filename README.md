# mvumgrab

Browse every USFS Motor Vehicle Use Map (MVUM), pick the ones you want, and turn them into
offline overlays for **OsmAnd** (`.sqlitedb`). MVUMs are the legal word on which forest roads are open,
to which vehicles, when, and along which roads you may pull off to camp. Only the PDFs carry the
camping corridors; the USFS road GIS data does not.

## Run

```
./mvumgrab            # http://127.0.0.1:8765
```

Needs `python3` (stdlib only) and Docker. The first run pulls `ghcr.io/osgeo/gdal:ubuntu-full-latest`
(~4.5 GB; the small GDAL image cannot read PDFs).

1. **Refresh catalog** (first time, ~15 min): crawls every region's forest pages for MVUM PDFs.
2. Filter / expand Region → Forest → map; tick maps, whole forests, regions, or **Select all shown**.
3. Pick options and **Convert selected**. About 1–2 min per map.
4. Files land in `out/`. Copy them to the phone:
   `Android/media/net.osmand.plus/files/tiles/` (F-Droid build: whichever `net.osmand.*` folder exists).
5. OsmAnd → Configure map → **Overlay map** → pick the MVUM; use the transparency slider.

## Options

| Option | Default | Notes |
|---|---|---|
| Max zoom | 14 | 15 is sharper but ~4x the size |
| Output | one file per forest | OsmAnd shows one overlay at a time, so per-forest is usually what you want |
| Tiles | WebP | ~4x smaller than PNG. Pick PNG if WebP tiles don't show on your device |
| Make white transparent | on | the OSM base map and contours show through the map's white areas |

## What it can't get

- Forests that publish MVUMs only through Avenza or the national Digital Maps app show as
  **none found** / **Avenza only**, with a link to the forest's page.
- Insets and road tables printed inside a map's frame sit at the main map's coordinates (same as Avenza).

## CLI

```
python3 -m mvum.catalog r10/chugach r06      # crawl part of the catalog
python3 -m mvum.catalog --retry-blocked      # re-crawl forests the USFS firewall blocked
python3 -m unittest                           # fast tests
RUN_SLOW=1 python3 -m unittest                # + one real download/convert (~2 min)
```
