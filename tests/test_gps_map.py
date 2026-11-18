#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_gps_map.py - standalone checks for gpsMap.py (no panel needed).

Covers the parts that are easy to get subtly wrong and impossible to debug
on a 2.4" screen:

  1. ROTATION CONVENTION. "Heading up" means a place ahead along your course
     appears ABOVE the centre marker. A feature due east must end up above
     centre when heading is 90, and a feature due north must end up to the
     LEFT when heading is 90. This pins PIL's rotate() direction down; if the
     sign is ever flipped the map would swing the wrong way on every turn.
  2. SMOOTH SCROLL. gpsd fixes land ~1 Hz. Dead reckoning must turn that into
     even per-frame motion (no 1 s stall then a jump) and still land on the
     right ground distance after a few seconds.
  3. HEADING HOLD. Below MIN_TRACK_SPEED, course over ground is noise. Heading
     must freeze (and report heading_held) instead of following it, then
     converge once the vehicle is genuinely moving.
  4. ZOOM HYSTERESIS. Speed dithering around a band boundary must not thrash
     the tile pyramid; zooming out must still be quick.
  5. TILE SOURCE OFFLINE. A tile on disk loads; a tile that is missing returns
     None immediately (the render thread must never wait on the network).
  6. FRAME COST + LOOK. Renders real cached tiles along a drive near Concord NH
     at several headings, prints ms/frame against the panel's ~123 ms full
     frame budget, and writes PNGs to /tmp for eyeballing.

Usage:  .venv/bin/python tests/test_gps_map.py [--online]
        (--online also fetches one real tile into the writable cache)
"""

import math
import os
import sys
import time

sys.path.insert(0, "/home/alex/DisplayControl")

import numpy as np
from PIL import Image, ImageDraw

import gpsMap

OUT_DIR = "/tmp/gpsmap-test"
LAT, LON = 43.1769, -71.1018          # live position seen from this deck
FAILURES = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


# ── synthetic tile source: white ground, coloured blobs at known places ──

class SyntheticTiles:
    """Duck-typed TileCache: renders pure-colour blobs at given lat/lon.

    Lets us place a landmark a known number of metres from the map centre and
    then measure where it lands on screen.
    """

    def __init__(self, blobs=None):
        self.blobs = blobs or []       # [(lat, lon, (r,g,b))]
        self.requested = set()

    def get(self, zoom, x, y):
        self.requested.add((zoom, x, y))
        img = Image.new("RGB", (256, 256), (235, 235, 235))
        # Tile-edge grid so rotation is visible in saved frames.
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 255, 255], outline=(90, 90, 90), width=2)
        n = 2 ** zoom * 256
        for lat, lon, color in self.blobs:
            wx, wy = gpsMap.lonlat_to_world_px(lat, lon, zoom)
            tx, ty = wx / 256.0, wy / 256.0
            if int(tx) != x or int(ty) != y:
                continue
            lx, ly = wx - x * 256, wy - y * 256
            d.ellipse([lx - 14, ly - 14, lx + 14, ly + 14], fill=color)
        return img

    def set_focus(self, zoom, x, y):
        pass


def offset_meters(lat, lon, north_m=0.0, east_m=0.0):
    """Destination a known offset away, for placing test landmarks."""
    lat2 = lat + north_m / gpsMap.M_PER_DEG_LAT
    lon2 = lon + east_m / (gpsMap.EQUATOR_M_PER_DEG_LON * math.cos(math.radians(lat)))
    return lat2, lon2


def blob_centroid(img, color, tol=70):
    """Screen centroid of pixels near `color`, or None if not visible."""
    arr = np.asarray(img).astype(int)
    dist = np.abs(arr - np.array(color)).sum(axis=2)
    mask = dist < tol
    if mask.sum() < 12:
        return None
    ys, xs = np.nonzero(mask)
    return float(xs.mean()), float(ys.mean())


# ── 1. rotation convention ──────────────────────────────────────────────

def test_rotation():
    print("\n1. rotation convention (heading up)")
    north = (255, 0, 0)
    east = (0, 0, 255)
    # 60 m out is ~69 px at z17: comfortably inside the 240x276 window in
    # every direction (the horizontal half-extent is only 120 px ~ 104 m).
    blobs = [(*offset_meters(LAT, LON, north_m=60), north),
             (*offset_meters(LAT, LON, east_m=60), east)]
    mmap = gpsMap.MovingMap(SyntheticTiles(blobs), 240, 276)

    cases = [
        # heading, blob, where it should land relative to the centre marker
        (0,   north, "up"),
        (90,  east,  "up"),      # travelling east: a place east is ahead
        (90,  north, "left"),    # ...and north is over your left shoulder
        (180, north, "down"),    # heading south: north is behind you
        (270, north, "right"),   # heading west: north is on your right
        (270, east,  "down"),    # heading west: east is behind you
    ]
    cx, cy = 120.0, 138.0
    for heading, color, expect in cases:
        img, _info = mmap.render(LAT, LON, heading, 17)
        pos = blob_centroid(img, color)
        if pos is None:
            check(f"heading {heading}: {expect}", False, "blob not visible")
            continue
        dx, dy = pos[0] - cx, pos[1] - cy
        if expect == "up":
            ok = dy < -25 and abs(dx) < 25
        elif expect == "down":
            ok = dy > 25 and abs(dx) < 25
        elif expect == "left":
            ok = dx < -25 and abs(dy) < 25
        else:
            ok = dx > 25 and abs(dy) < 25
        check(f"heading {heading:>3}: landmark {expect}",
              ok, f"offset from centre ({dx:+.0f}, {dy:+.0f}) px")


# ── 2. smooth scroll between 1 Hz fixes ─────────────────────────────────

def test_smooth_scroll():
    print("\n2. smooth scroll (dead reckoning between ~1 Hz fixes)")
    est = gpsMap.PositionEstimator()
    speed = 20.0                      # m/s (~72 km/h)
    t0 = 1000.0
    frames = []                       # (time, lat, lon)
    frame_dt = 0.1                    # 10 fps render loop
    for i in range(50):               # 5 s of driving north
        now = t0 + i * frame_dt
        fix_i = int(i // 10)          # a fresh fix once a second
        fix_now = t0 + fix_i * 1.0
        lat = LAT + (speed * (fix_now - t0)) / gpsMap.M_PER_DEG_LAT
        est.update(lat, LON, speed, 0.0, now=fix_now)
        s = est.sample(now=now)
        frames.append((now, s.lat, s.lon, s.extrapolated))

    # Ground distance travelled per frame should be near-even, not 1 s of
    # nothing followed by a 20 m hop.
    steps = [math.dist((frames[i][1], frames[i][2]),
                       (frames[i + 1][1], frames[i + 1][2])) * gpsMap.M_PER_DEG_LAT
             for i in range(10, len(frames) - 1)]
    spread = max(steps) / max(1e-9, min(steps))
    total = sum(steps)
    check("per-frame steps are even", spread < 1.6,
          f"max/min step ratio {spread:.2f} (steps {min(steps):.2f}-{max(steps):.2f} m)")
    check("ground distance is right", abs(total - speed * 3.9) < 12.0,
          f"{total:.1f} m over ~3.9 s at {speed} m/s (expect ~{speed * 3.9:.1f})")
    check("marks interpolated frames as extrapolated",
          all(f[3] for f in frames[12:19]), "frames between fixes flagged")

    # A fix that goes stale must stop flying the last vector.
    est2 = gpsMap.PositionEstimator()
    est2.update(LAT, LON, 25.0, 0.0, now=0.0)
    far = est2.sample(now=99.0)
    near = est2.sample(now=est2.MAX_EXTRAPOLATION + 1.0)
    check("stale fix stops extrapolating",
          abs(far.lat - LAT) < 0.01 and not far.extrapolated,
          f"held at {(far.lat - LAT) * gpsMap.M_PER_DEG_LAT:+.1f} m north")


# ── 3. heading hold when slow ───────────────────────────────────────────

def test_heading_hold():
    print("\n3. heading hold below MIN_TRACK_SPEED")
    est = gpsMap.PositionEstimator()
    est.update(LAT, LON, 12.0, 90.0, now=0.0)      # established eastbound
    est.sample(now=0.0)
    start = est.sample(now=0.1).heading

    # Stopped at a light: gpsd wanders 90 -> 0 -> 270 while crawling.
    for i, wander in enumerate((0.0, 45.0, 350.0, 270.0, 180.0)):
        now = 1.0 + i * 0.2
        est.update(LAT + i * 1e-6, LON, 0.3, wander, now=now)
    held = est.sample(now=2.0)
    drift = abs(gpsMap.angle_delta(held.heading, start))
    check("heading frozen while crawling", drift < 2.0,
          f"drift {drift:.1f} deg while track wandered 360 deg")
    check("reports heading_held", held.heading_held is True)

    # Moving again: converges on the new course within a couple of seconds.
    for i in range(20):
        now = 2.0 + i * 0.1
        est.update(LAT, LON + i * 1e-5, 14.0, 300.0, now=now)
    moved = est.sample(now=4.0)
    err = abs(gpsMap.angle_delta(moved.heading, 300.0))
    check("converges once moving", err < 12.0, f"{err:.1f} deg from new course")
    check("reports heading live again", moved.heading_held is False)

    # Wrap must take the short way round, not swing 350 degrees the other way.
    est2 = gpsMap.PositionEstimator()
    est2.update(LAT, LON, 15.0, 359.0, now=0.0)
    for i in range(6):
        est2.update(LAT, LON + i * 1e-5, 15.0, 2.0, now=0.2 + i * 0.2)
    wrap = est2.sample(now=1.4).heading
    check("wraps 359 -> 002 the short way", wrap > 300.0 or wrap < 20.0,
          f"heading {wrap:.1f} deg")


# ── 4. zoom hysteresis ──────────────────────────────────────────────────

def test_zoom():
    print("\n4. speed-adaptive zoom")
    zc = gpsMap.ZoomController(initial=17)
    # Dither around the 8 m/s boundary: must not thrash.
    t = 0.0
    seen = set()
    for i in range(60):                       # 6 s of dithering
        speed = 8.0 + (0.6 if i % 2 else -0.6)
        t += 0.1
        seen.add(zc.update(speed, now=t))
    check("no thrash while dithering at a boundary", seen == {17},
          f"zooms seen: {sorted(seen)}")

    # Real highway speed: zooms out promptly (within DWELL_OUT + slack).
    for i in range(40):
        t += 0.1
        z = zc.update(28.0, now=t)
        if z != 17:
            break
    check("zooms out at highway speed", z == 15, f"reached z{z} after {(i + 1) * 0.1:.1f} s")

    # Back to town: only after a decisive, sustained slowdown.
    for i in range(200):
        t += 0.1
        z = zc.update(3.0, now=t)
        if z == 17:
            break
    check("zooms back in when slow", z == 17, f"after {(i + 1) * 0.1:.1f} s below the band")


# ── 5. tile source, offline ─────────────────────────────────────────────

def test_tile_source():
    print("\n5. tile source (local-first, never blocks)")
    tmp = "/tmp/gpsmap-test/tiles"
    os.makedirs(tmp, exist_ok=True)
    # Seed the writable cache with one known tile.
    z, x, y = 17, 39648, 48072
    os.makedirs(f"{tmp}/TestStyle/{z}/{x}", exist_ok=True)
    Image.new("RGB", (256, 256), (12, 200, 34)).save(f"{tmp}/TestStyle/{z}/{x}/{y}.png")

    tc = gpsMap.TileCache(writable=tmp, readonly=(), style="TestStyle")
    img = tc.get(z, x, y)
    check("loads a cached tile", img is not None and img.size == (256, 256),
          f"{getattr(img, 'size', None)} {getattr(img, 'mode', None)}")

    t0 = time.perf_counter()
    miss = tc.get(z, x + 500, y)
    dt_ms = (time.perf_counter() - t0) * 1000
    check("missing tile returns None at once", miss is None and dt_ms < 20.0,
          f"{dt_ms:.2f} ms (fetcher threads not even started)")
    check("second read hits memory cache", tc.get(z, x, y) is not None,
          f"hits={tc.hits} misses={tc.misses}")

    # Real pre-downloaded set must be readable through the read-only path.
    live = gpsMap.TileCache(writable=tmp + "/empty",
                            readonly=gpsMap.READONLY_CACHES,
                            style="Standard-OSM")
    real = live.get(17, x, y)
    check("reads the existing offline cache", real is not None,
          f"z17/{x}/{y} from {gpsMap.READONLY_CACHES[0]}")

    # Coarser zooms are not in the offline cache at all (it is z17-19), but
    # speed-adaptive zoom needs them. They must be synthesised by downsampling
    # the finer tiles, with no network involved.
    fresh = gpsMap.TileCache(writable=tmp + "/empty2",
                             readonly=gpsMap.READONLY_CACHES, style="Standard-OSM")
    t0 = time.perf_counter()
    coarse16 = fresh.get(16, x >> 1, y >> 1)
    dt16 = (time.perf_counter() - t0) * 1000
    check("synthesises z16 from cached z17",
          coarse16 is not None and coarse16.size == (256, 256),
          f"{dt16:.1f} ms first time, {fresh.synthesized} synthesised")
    t0 = time.perf_counter()
    coarse15 = fresh.get(15, x >> 2, y >> 2)
    dt15 = (time.perf_counter() - t0) * 1000
    check("synthesises z15 from cached z17",
          coarse15 is not None and coarse15.size == (256, 256),
          f"{dt15:.1f} ms first time")
    if coarse15 is not None:
        arr = np.asarray(coarse15)
        check("synthesised tile carries real map content", float(arr.std()) > 3.0,
              f"pixel std {float(arr.std()):.1f} (blank ground would be ~0)")
    t0 = time.perf_counter()
    again = fresh.get(16, x >> 1, y >> 1)
    check("synthesised tile is memoised", again is not None
          and (time.perf_counter() - t0) * 1000 < 1.0,
          f"second read {(time.perf_counter() - t0) * 1000:.2f} ms")
    return live


def test_render_cost(live):
    print("\n6. render cost + look (real cached tiles, drive near Concord NH)")
    os.makedirs(OUT_DIR, exist_ok=True)
    est = gpsMap.PositionEstimator()
    speed = 24.0
    frames = 30
    t0 = time.monotonic()
    imgs = []
    for zoom in (17, 16, 15):
        times = []
        for i in range(frames):
            # Drive on a slowly curving heading so rotation and scroll both move.
            now = t0 + i * 0.1
            heading = (40 + i * 4) % 360
            lat = LAT + i * 2.0 / gpsMap.M_PER_DEG_LAT
            lon = LON + i * 1.5 / (gpsMap.EQUATOR_M_PER_DEG_LON
                                   * math.cos(math.radians(LAT)))
            est.update(lat, lon, speed, heading, now=now)
            s = est.sample(now=now)
            mmap = gpsMap.MovingMap(live, 240, 276)
            f0 = time.perf_counter()
            img, info = mmap.render(s.lat, s.lon, s.heading, zoom)
            times.append((time.perf_counter() - f0) * 1000)
            if i == frames - 1:
                imgs.append((zoom, heading, img, info))
        avg = sum(times) / len(times)
        check(f"z{zoom} render fits the frame budget", avg < 45.0,
              f"avg {avg:.1f} ms/frame (min {min(times):.1f}, max {max(times):.1f});"
              " panel full frame is ~123 ms at 10 MHz")

    # Contact sheet: one frame per zoom, marker drawn where the view puts it.
    sheet = Image.new("RGB", (240 * len(imgs) + 10 * (len(imgs) - 1), 276 + 24),
                      (0, 0, 0))
    for i, (zoom, heading, img, info) in enumerate(imgs):
        sheet.paste(img, (i * (240 + 10), 24))
        d = ImageDraw.Draw(sheet)
        cx, cy = i * (240 + 10) + 120, 24 + 138
        d.polygon([(cx, cy - 9), (cx - 7, cy + 8), (cx + 7, cy + 8)], fill=(0, 255, 255))
        d.text((i * (240 + 10) + 4, 4),
               f"z{zoom} hdg {heading} tiles {info['tiles_have']}/{info['tiles_want']}"
               f" {info['meters_per_pixel']:.1f} m/px", fill=(255, 255, 0))
    out = os.path.join(OUT_DIR, "contact_sheet.png")
    sheet.save(out)
    print(f"       contact sheet: {out}")


def test_online_fetch():
    print("\n7. online fetch (opt-in)")
    tmp = "/tmp/gpsmap-test/onlinetiles"
    tc = gpsMap.TileCache(writable=tmp, readonly=(), style="Standard-OSM",
                          workers=1, fetch_throttle=0.0, prefetch_radius=1)
    tc.start()
    # An ocean tile that is definitely not in the offline set.
    z, x, y = 8, 67, 105
    tc.set_focus(z, x, y)          # schedules the prefetch ring
    t0 = time.monotonic()
    got = None
    while got is None and time.monotonic() - t0 < 25.0:
        got = tc.get(z, x, y)      # needed now: must be fetched even before focus
        if got is None:
            time.sleep(0.3)
    check("fetched the tile we asked to draw", got is not None,
          f"{tc.fetched} fetched, {tc.failed} failed, online={tc.online}"
          f" after {time.monotonic() - t0:.1f} s")
    if got is not None:
        p = tc._local_path(z, x, y)
        check("stored in the downloader's {style}/{z}/{x}/{y}.png layout",
              os.path.isfile(p), p)

    # Prefetch: a neighbour we never asked to draw should arrive on its own.
    # (Check the file directly — calling get() would enqueue it as "needed" and
    # prove nothing.)
    ring_path = tc._local_path(z, x + 1, y)
    t1 = time.monotonic()
    while not os.path.isfile(ring_path) and time.monotonic() - t1 < 20.0:
        time.sleep(0.4)
    check("prefetches the surrounding ring", os.path.isfile(ring_path),
          f"neighbour z{z}/{x + 1}/{y} arrived without being drawn")

    # Driving away / changing zoom must cancel queued prefetch for the old view.
    def tiles_at_zoom(zz):
        root = os.path.join(tmp, "Standard-OSM", str(zz))
        return sum(len([f for f in files if f.endswith(".png")])
                   for _r, _d, files in os.walk(root)) if os.path.isdir(root) else 0
    before = tiles_at_zoom(z)
    tc.set_focus(z - 2, x // 4, y // 4)
    time.sleep(2.0)
    after = tiles_at_zoom(z)
    # At most one extra tile may still land: whatever a worker had already
    # popped and was mid-download when the focus moved. The rest of the ring
    # must be dropped, not fetched.
    check("zoom change cancels stale prefetch", after <= before + 1,
          f"{before} tiles at z{z} before, {after} after"
          f" ({tc.pending} queued for the new view)")
    tc.stop()


def main():
    print("=" * 72)
    print("gpsMap.py checks")
    print("=" * 72)
    test_rotation()
    test_smooth_scroll()
    test_heading_hold()
    test_zoom()
    live = test_tile_source()
    test_render_cost(live)
    if "--online" in sys.argv:
        test_online_fetch()
    else:
        print("\n7. online fetch skipped (pass --online to try it)")
    print("\n" + "=" * 72)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {', '.join(FAILURES)}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
