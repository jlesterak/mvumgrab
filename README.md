# mvumgrab

USFS Motor Vehicle Use Maps (MVUMs) on your phone, in **OsmAnd**, offline. MVUMs are the legal word on which
forest roads and trails are open, to which vehicles, and when.

A small server builds two things per national forest, and a phone app (a PWA that works offline) downloads them:

| File | From | In OsmAnd |
|---|---|---|
| **Roads & trails** `.gpx` | USFS national MVUM GIS data, every forest (110) | a track: lines colored by vehicle class; tap a line or its pin for the vehicle classes and dates open |
| **Official map** `.sqlitedb` | the forest's MVUM PDFs, converted on the server on request | an overlay map (Configure map → Overlay map) |

Only the PDF maps show the dispersed-camping corridors along roads; the GIS data has no corridors.

## Phone

1. Open the server's HTTPS address in Chrome on the phone (a service worker needs HTTPS; `tailscale serve` is the
   easy way). Menu → **Add to Home screen**.
2. **All forests** → search or **Near me** → **Download**. Files are kept inside the app.
3. **Open in OsmAnd** saves the file to Downloads; tap the download notification and pick OsmAnd. This works
   with no signal: the app and every downloaded forest are on the phone.
4. **Official map → Make on server** converts a forest's PDFs (about 3 min per map); download it when it is done.

Line colors: green road open to all vehicles, blue road for highway-legal vehicles only, orange trail open to all
vehicles, purple trail for vehicles 50 in or narrower, red motorcycle trail, brown special designation.

## Server

Container (DockSTARTer-style env, one `/config` volume):

```yaml
services:
  mvumgrab:
    image: ghcr.io/jlesterak/mvumgrab:0.1.0
    container_name: mvumgrab
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=America/Anchorage
    ports:
      - 8765:8765
    volumes:
      - ./mvumgrab:/config
    restart: unless-stopped
```

On first start it downloads the USFS national MVUM data (~270 MB), builds every forest's GPX (~6 min, ~420 MB
of GPX), then crawls the forest websites for PDF maps (slow, rate-limited). Both refresh when older than 30 days
(`MVUM_MAX_AGE_DAYS`). `/admin` is the desktop UI for the PDF catalog and batch conversions. `/healthz` for monitoring.

Without Docker: `./mvumgrab` (needs `python3` and Docker for GDAL), then http://127.0.0.1:8765.

## What it can't get

- About 4,000 MVUM trail records have no line anywhere in USFS public data. Most are in Tonto, Custer Gallatin
  and Beaverhead-Deerlodge. The app shows the count per forest. Roads are complete.
- Forests that publish their PDF MVUMs only through Avenza get no official map (their roads and trails still come
  from the GIS data).
- A few USFS PDFs are not georeferenced; those maps fail with "PDF has no georeferencing".
- Insets printed inside a map's frame sit at the main map's coordinates (same as Avenza).

## CLI

```
python3 -m mvum.vector [--force]              # refresh national vector data + every GPX
python3 -m mvum.catalog r10/chugach r06      # crawl part of the PDF catalog
python3 -m unittest discover tests            # fast tests (the GDAL one needs the GDAL image)
RUN_SLOW=1 python3 -m unittest discover tests # + one real PDF download/convert (~2 min)
<venv>/bin/python tests/ui_smoke.py           # Playwright: phone app offline + admin UI
```

Data: USFS FSGeodata (`Trans_MVUM_Road`, `Trans_MVUM_Trail`, `Trans_Trail_NFS_Publish`) and USFS MVUM PDFs,
public domain. Not a substitute for the official map: check closures with the ranger district.
