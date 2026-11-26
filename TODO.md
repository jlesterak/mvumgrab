# mvumgrab TODO

- [IN PROGRESS] Phone app (plan 2026-11-25, tracking `.work/2026-11-25-phone-app.jsonl`). Built, validated, published: github.com/jlesterak/mvumgrab, `ghcr.io/jlesterak/mvumgrab:0.1.0`. Left, when icebox is up (Jake OK'd all four 2026-11-25):
  1. Create `icebox:git/mvumgrab.git`, add remote `icebox`, push.
  2. Paste `homelab/proposals/mvumgrab/override-block.yml` into icebox's override, copy the proposal to `~/dockstarter-template-proposals/mvumgrab/`, `docker compose up -d mvumgrab`, check `curl icebox:8765/healthz`.
  3. Jake runs (sudo): install Tailscale on icebox, `sudo tailscale up`, `sudo tailscale serve --bg --https=443 http://127.0.0.1:8765`. Phone: open the ts.net URL, Add to Home screen.
  4. Homepage entry (`picebox/homepage/services.yaml`, near Songarr) + ENVIRONMENT.md icebox line, one homelab commit; copy services.yaml to picebox.
- [PENDING] Trail records with no line anywhere in USFS public data (~4,070, mostly Tonto 1,497, Custer Gallatin 1,285, Beaverhead-Deerlodge 464): find per-forest sources only if Jake goes there. Counted as `missing` in `data/vector/index.json`.
- [PENDING] Jake: copy `out/MVUM Chugach.sqlitedb` to the phone, confirm it shows under Overlay map (WebP tiles). If blank, reconvert with Tiles = PNG and note it here.
- [PENDING] Forests with "none found" (~25, mostly R1/R3/R6/R8): their MVUMs are Avenza-only or on odd pages. Add per-forest overrides only for forests Jake actually visits.
- [COMPLETED] 2026-11-25 vector MVUM (`mvum/vector.py`): national FSGeodata roads/trails downloads -> per-forest GeoJSON + OsmAnd GPX for 110 forests (181,811 segments, 441 MB GPX at ~3 m simplification). Trail lines missing in the MVUM data are filled from the NFS trails dataset by TRAIL_CN + mileposts (9,040). GPX confirmed in OsmAnd on Jake's phone (colors, tap shows rules).
- [COMPLETED] 2026-11-25 validator fixes: Box.com HEAD 404 → ranged-GET fallback (169 R4/R5 maps), tests discoverable, duplicate per-map names kept apart, per-job work dirs.
- [COMPLETED] 2026-11-25 v1: crawler (sitemap, maps-guides, R4/R5 footprint fallbacks, WAF backoff), converter (neatline clip, zoom-grid warp, white→alpha, WebP/PNG, OsmAnd sqlitedb), web UI (tree, tri-state, select all, filter, queue, outputs).
