# Map screen (view 4)

A heading-up moving map: two lines of GPS status across the top, then the map
filling the panel, rotated so the direction of travel always points up and
scrolling underneath a marker that never moves.

```
┌────────────────────────┐
│ 3D  101km/h  337°      │  fix · ground speed · heading (H = held)
│ 8/20 sat +/-12m z15 OK │  sats · reported accuracy · zoom · tile source
├────────────────────────┤
│           N            │
│           │            │   north swings around the marker as the
│           ▲            │   map rotates the other way
│      (you are here)    │
│                        │
│ ▇▇▇▇▇▇▇ 200 m          │   scale bar
└────────────────────────┘
```

All of it lives in `gpsMap.py`; `lcdstats.py` only lays out the header, pastes
the window and gates the fetcher threads. Three pieces:

## 1. Tiles — `TileCache`

Lookup order for every tile, and this is the whole design:

1. **memory** (LRU, ~96 tiles)
2. **disk** — our own writable cache, then the read-only caches
3. **synthesised** by downsampling finer tiles we already have
4. **network**, on a background thread — never on the render thread

A tile that is not available renders as dark blue-grey ground and fills in
when the download lands. The SPI loop must never wait on a socket, so `get()`
returns `None` immediately (measured 0.1–4 ms) and queues the work.

**Why synthesis exists.** The offline set (`~/map-tile-downloader/tile-cache`,
2.1 M Standard-OSM tiles) is dense at **z17–19 and has nothing at z15/z16** —
and z15/z16 is exactly what you want at highway speed. So a coarse tile is
built by pasting the 2×2 (or 4×4) finer tiles and downsampling: 5–20 ms once
per tile, then it sits in the memory cache like any other tile. Zooming out to
z15 therefore needs **no network at all** inside the cached area. The fetcher
thread does this for the prefetch ring so the cost lands off the render thread
(worst frame at z15: 210 ms before that change, ~100 ms after).

**Cache layout** matches `map-tile-downloader` exactly — `{style}/{z}/{x}/{y}.png`
— so tiles fetched here show up in that tool's cache manager and vice versa.
Eviction (300 MB cap by default) only ever deletes files in *our* writable
cache, never the pre-downloaded set:

| Path | Role |
|---|---|
| `~/.cache/cyberdeck-map/tiles/Standard-OSM/` | ours: written by the fetcher, evictable |
| `~/map-tile-downloader/tile-cache/Standard-OSM/` | read-only: pre-downloaded, never touched |

Tile politeness (OSM tile usage policy): one request per worker with a fixed
250 ms spacing, a real `User-Agent`, only the visible tiles plus a ring of 2,
and a 60 s backoff after any network error instead of hammering through a dead
zone. Tiles that keep failing get a 3-minute retry backoff so driving outside
coverage does not spin.

## 2. Motion — `PositionEstimator`

gpsd delivers TPV about **1.1 Hz** on this receiver (`?WATCH` probe: 11 reports
in 10 s). One position per second on an 8 fps screen is a stall and a jump, not
a scroll — so between fixes the last velocity is integrated forward (dead
reckoning) using the fix's own speed and course over ground. Extrapolation stops
after 3 s (`MAX_EXTRAPOLATION`): a fix that went stale is held rather than
flown.

Heading is the other half. Course over ground is **noise below ~1.5 m/s** — at a
stoplight it wanders through the whole compass. So:

- above `MIN_TRACK_SPEED`: exponential smoothing on the *shortest angular path*
  (`HEADING_TAU` 0.8 s), so a 359°→001° turn cuts the corner instead of swinging
  358° the other way
- below it: heading is **frozen** at the last trustworthy value and the header
  shows a trailing `H`, e.g. `337°H`

## 3. Rendering — `MovingMap`

Build a tile mosaic aligned to whole pixels around the estimated position
(the centre is therefore exact to within half a pixel, < 0.5 m at z17), rotate
it counter-clockwise by the heading, and crop the panel-sized window out of the
middle. Rotating CCW by `heading` is what puts a bearing-`heading` place
straight up — pinned down by `tests/test_gps_map.py` §1 with landmarks placed at
known offsets:

| heading | landmark due east appears | landmark due north appears |
|---|---|---|
| 0° | up | up |
| 90° | **up** | left |
| 180° | behind | down |
| 270° | down | right |

The canvas is one tile multiple larger than the panel diagonal so no unrotated
edge ever enters the crop. Marker, north tick and scale bar are drawn by
`gpsMap.draw_map_overlay()`, shared with the preview tool so the two cannot
disagree; text gets a 1 px dark halo because OSM land is off-white and plain
white overlays vanish into it.

## Zoom

Speed-adaptive (`ZoomController`), with hysteresis and dwell, because switching
zoom re-requests every tile:

| ground speed | zoom | ≈ resolution across the panel |
|---|---|---|
| < 8 m/s (~29 km/h) | z17 | 0.87 m/px → ~200 m |
| < 20 m/s (~72 km/h) | z16 | 1.74 m/px → ~400 m |
| ≥ 20 m/s | z15 | 3.48 m/px → ~800 m |

Zooming **out** happens within ~1 s (you want the context the moment you merge
onto the highway). Zooming **in** waits for the speed to clear the band boundary
by 1.5 m/s *and* hold for 5 s, so crawling through a slow patch does not thrash
the tile pyramid. Measured: dithering ±0.6 m/s around the 8 m/s boundary for 6 s
changes nothing; a real slowdown re-zooms in 5.2 s.

## Cost

| | measured |
|---|---|
| render (z17 / z16 / z15) | 10.0 / 11.6 / 12.9 ms per frame average |
| rotation alone | 9.4 ms (PIL bilinear, 512² canvas) |
| panel full frame at 10 MHz SPI | ~123 ms → the map runs ~8 fps |
| process CPU, view 4 vs view 0 | 27.8% vs 10.4% |

A rotating map dirties nearly every tile, so the tile-diff optimisation buys
little here; the SPI write is the pacing (`FLUID_FRAME_INTERVAL_SECONDS`, no
artificial sleep).

## Tuning knobs

All in `gpsMap.py` at module level or on `TileCache.__init__`:
`WRITABLE_CACHE`, `READONLY_CACHES`, `OSM_URL`, `MISSING_TILE_RGB`,
`max_disk_bytes`, `prefetch_radius`, `synth_depth` / `max_synth_zoom` /
`synth_from_zoom`, and on `ZoomController` the `BANDS` / hysteresis / dwell.
Layout constants (`MAP_LEFT`, `MAP_TOP`) are in `lcdstats.py` next to where the
view is created.

To extend the offline coverage, pre-download with `~/map-tile-downloader` into
`tile-cache/Standard-OSM` (any zoom — coarser zooms simply skip synthesis).

## Checks

```bash
.venv/bin/python tests/test_gps_map.py            # rotation, motion, hold, zoom, tiles, cost
.venv/bin/python tests/test_gps_map.py --online   # + real fetch, prefetch ring, stale-cancel
.venv/bin/python tests/map_view_preview.py        # what the screen looks like → /tmp/gpsmap-test/
```
