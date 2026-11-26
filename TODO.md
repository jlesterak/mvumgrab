# mvumgrab TODO

- [PENDING] Jake: copy `out/MVUM Chugach.sqlitedb` to the phone, confirm it shows under Overlay map (WebP tiles). If blank, reconvert with Tiles = PNG and note it here.
- [PENDING] Forests with "none found" (~25, mostly R1/R3/R6/R8): their MVUMs are Avenza-only or on odd pages. Add per-forest overrides only for forests Jake actually visits.
- [COMPLETED] 2026-11-25 v1: crawler (sitemap, maps-guides, R4/R5 footprint fallbacks, WAF backoff), converter (neatline clip, zoom-grid warp, white→alpha, WebP/PNG, OsmAnd sqlitedb), web UI (tree, tri-state, select all, filter, queue, outputs).
