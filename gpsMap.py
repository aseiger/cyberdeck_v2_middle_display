#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  gpsMap.py
#
#  Heading-up moving map for the LCD "Map" view.
#
#  Three pieces, kept separate so each can be tested without the panel:
#
#    TileCache        Web-Mercator tile source. Local-first (a writable cache
#                     we fill, plus read-only caches such as the one built by
#                     ~/map-tile-downloader), falling back to an online fetch
#                     that is done OFF the render thread — a missing tile must
#                     never stall the SPI loop, it renders as blank ground and
#                     fills in when the download lands.
#
#    PositionEstimator  gpsd delivers TPV at ~1 Hz on this receiver, which is
#                     far too coarse for a map that should scroll smoothly.
#                     So we dead-reckon forward from the last fix using its
#                     own speed + course-over-ground, and low-pass the heading.
#                     Course over ground is meaningless below ~1.5 m/s (it
#                     wanders 90 degrees at a stoplight), so below that speed
#                     the heading is HELD at the last trustworthy value.
#
#    MovingMap        Builds a tile mosaic centred on the estimated position,
#                     rotates it so the current heading points up, and crops
#                     the panel-sized window out of the middle. The vehicle
#                     marker therefore stays parked in the middle of the
#                     screen while the map slides and swings beneath it.
#
# Tile layout on disk matches the {style}/{z}/{x}/{y}.png scheme used by
# ~/map-tile-downloader, so tiles fetched here show up in that tool's cache
# manager and vice versa. Our own writable cache is separate from the
# read-only ones: eviction only ever deletes files we downloaded, never the
# pre-downloaded set.
#
# Tile politeness (OSM tile usage policy): one request at a time per worker,
# a fixed delay between requests, a real User-Agent, and only the tiles that
# are actually on screen plus a small ring around them. When the network is
# down the failure is remembered and retried on a long backoff instead of
# hammering the server.
#

import collections
import heapq
import itertools
import logging
import math
import os
import random
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict

from PIL import Image

TILE_SIZE = 256

# Where tiles live. Writable first (we own it), then the big offline set.
WRITABLE_CACHE = "/home/alex/.cache/cyberdeck-map/tiles"
READONLY_CACHES = ("/home/alex/map-tile-downloader/tile-cache",)

OSM_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
# The tile policy wants a identifying agent; this is a personal device.
USER_AGENT = "lcdstats-cyberdeck/1.0 (personal moving map on a Pi cyberdeck)"

# Base colour for ground we have no tile for: dark blue-grey, distinct from
# both OSM's land beige and its water blue, so gaps read as "no data".
MISSING_TILE_RGB = (16, 20, 28)

M_PER_DEG_LAT = 111132.0            # metres per degree of latitude (good to ~0.3%)
EQUATOR_M_PER_DEG_LON = 111320.0    # metres per degree of longitude at the equator


# ── Web Mercator helpers ────────────────────────────────────────────────

def lonlat_to_world_px(lat, lon, zoom):
    """Lat/lon -> fractional pixel in the whole-world tile canvas at zoom."""
    n = 2 ** zoom * TILE_SIZE
    x = (lon + 180.0) / 360.0 * n
    lat = max(-85.05112878, min(85.05112878, lat))
    rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0 * n
    return x, y


def meters_per_pixel(lat, zoom):
    """Ground resolution of one screen pixel at this latitude/zoom."""
    return 156543.03392 * math.cos(math.radians(lat)) / (2 ** zoom)


def angle_delta(target, current):
    """Signed shortest rotation from current to target, in [-180, 180)."""
    return (target - current + 180.0) % 360.0 - 180.0


# ── Tile source ─────────────────────────────────────────────────────────

class TileCache:
    """Local-first map tile source with an asynchronous online fallback.

    get() never blocks on the network: a tile that exists (memory or disk) is
    returned immediately, anything else is queued for the fetcher thread and
    reported as missing (None) until it lands.
    """

    def __init__(self, writable=WRITABLE_CACHE, readonly=READONLY_CACHES,
                 style="Standard-OSM", url_template=OSM_URL,
                 user_agent=USER_AGENT, workers=2,
                 max_disk_bytes=300 * 1024 * 1024, mem_tiles=96,
                 fetch_throttle=0.25, request_timeout=6.0,
                 net_backoff=60.0, fail_backoff=180.0, prefetch_radius=2,
                 synth_depth=2, max_synth_zoom=19, synth_from_zoom=17):
        self._writable = os.path.join(writable, style)
        self._readonly = [os.path.join(p, style) for p in readonly]
        self._url = url_template
        self._ua = user_agent
        self._nworkers = int(workers)
        self._max_disk_bytes = int(max_disk_bytes)
        self._fetch_throttle = fetch_throttle
        self._request_timeout = request_timeout
        self._net_backoff = net_backoff
        self._fail_backoff = fail_backoff
        self._prefetch_radius = int(prefetch_radius)
        # Coarser-than-cached zooms are synthesised by downsampling the finer
        # tiles we do have (see _synthesize). synth_depth = how many levels up.
        self._synth_depth = int(synth_depth)
        self._max_synth_zoom = int(max_synth_zoom)
        # Zooms BELOW this are built locally from finer cached tiles rather than
        # fetched: the offline cache is dense here at z17, so a highway zoom of
        # z15/z16 needs no network at all.
        self._synth_from_zoom = int(synth_from_zoom)
        self._failed_until = {}            # (z,x,y) -> monotonic time to stop retrying
        self._synth_fail = {}              # (z,x,y) -> time to stop trying to synthesise

        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._mem = OrderedDict()            # (z,x,y) -> Image, most recent last
        self._mem_limit = int(mem_tiles)
        self._queue = []                     # heap of (distance, seq, z, x, y)
        self._queued = set()                 # dedupe: tiles already requested
        self._seq = itertools.count()

        # Focus: the tile the map is centred on. Used to rank prefetches and
        # to drop queued work that the vehicle has already driven past.
        self._focus = None                   # (z, x, y)
        self._focus_seq = 0

        self._stop = threading.Event()
        self._threads = []
        self._running = False

        # Stats / health, read by the view for its header line.
        self.hits = 0
        self.misses = 0
        self.fetched = 0
        self.failed = 0
        self.synthesized = 0               # tiles built by downsampling finer ones
        self._net_down_until = 0.0
        self._writes_since_sweep = 0

    # ── lifecycle ───────────────────────────────────────────────────────

    def start(self):
        """Start fetcher threads (idempotent). Called when the view opens."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop.clear()
            for i in range(self._nworkers):
                t = threading.Thread(target=self._worker, name=f"tilefetch{i}",
                                     daemon=True)
                self._threads.append(t)
                t.start()
        os.makedirs(self._writable, exist_ok=True)

    def stop(self):
        """Stop fetching (called when the view closes). Keeps caches."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._queue.clear()
            self._queued.clear()
            self._cv.notify_all()
        self._stop.set()
        threads, self._threads = self._threads, []
        for t in threads:
            t.join(timeout=2.0)

    @property
    def running(self):
        return self._running

    @property
    def online(self):
        """False while we are inside the backoff window after a network error."""
        return time.monotonic() >= self._net_down_until

    @property
    def pending(self):
        with self._lock:
            return len(self._queue)

    # ── read path (render thread) ───────────────────────────────────────

    def get(self, zoom, x, y):
        """Return tile as RGB Image, or None if not available yet.

        Never blocks on the network. Order tried: memory, disk (ours then the
        read-only caches), a tile synthesised by downsampling finer cached
        tiles, and only then a queued background fetch. A miss renders as
        blank ground until the download lands.
        """
        key = (zoom, x, y)
        img = self._lookup(zoom, x, y)
        if img is not None:
            with self._lock:
                self.hits += 1
            return img
        img = self._synthesize(zoom, x, y)
        if img is not None:
            self._remember(key, img)
            with self._lock:
                self.synthesized += 1
                self.hits += 1
            return img
        with self._lock:
            self.misses += 1
            self._enqueue_locked(zoom, x, y)
        return None

    def _lookup(self, zoom, x, y):
        """Memory then disk. No network, no synthesis, no enqueue."""
        key = (zoom, x, y)
        with self._lock:
            img = self._mem.get(key)
            if img is not None:
                self._mem.move_to_end(key)
                return img
        path = self._find_on_disk(zoom, x, y)
        if path is not None:
            img = self._decode(path)
            if img is not None:
                self._remember(key, img)
                return img
        return None

    def _synthesize(self, zoom, x, y):
        """Build a coarser tile from the finer tiles we have on disk.

        The offline cache is dense around here at z17-19 but has nothing at
        z15/z16, and speed-adaptive zoom wants exactly those when you get on
        the highway. Rather than depending on a network connection at 70 mph,
        paste the 2x2 (or 4x4) finer tiles together and downsample: one-off
        cost of a few milliseconds per tile, then it sits in the memory cache
        like any other. Partial coverage is still worth showing — blank
        quadrants read as "no data" the same way a whole missing tile does.
        """
        if self._synth_depth < 1:
            return None
        now = time.monotonic()
        if self._synth_fail.get((zoom, x, y), 0.0) > now:
            return None                     # already proved impossible recently
        for depth in range(1, self._synth_depth + 1):
            src_zoom = zoom + depth
            if src_zoom > self._max_synth_zoom:
                break
            span = 1 << depth                       # 2 or 4 tiles per side
            big = TILE_SIZE * span
            sheet = Image.new("RGB", (big, big), MISSING_TILE_RGB)
            got = 0
            for dy in range(span):
                for dx in range(span):
                    sub = self._lookup(src_zoom, (x << depth) + dx, (y << depth) + dy)
                    if sub is None:
                        continue
                    sheet.paste(sub, (dx * TILE_SIZE, dy * TILE_SIZE))
                    got += 1
            if got * 2 >= span * span:              # at least half covered
                return sheet.resize((TILE_SIZE, TILE_SIZE), Image.Resampling.BOX)
        if len(self._synth_fail) > 4096:
            self._synth_fail = {k: v for k, v in self._synth_fail.items() if v > now}
        self._synth_fail[(zoom, x, y)] = now + 60.0
        return None

    def set_focus(self, zoom, x, y):
        """Tell the cache what the map is centred on; schedules a prefetch ring.

        Cheap enough to call every frame: the ring is only re-scheduled when
        the centre tile (or zoom) actually changes.
        """
        with self._lock:
            if self._focus == (zoom, x, y):
                return
            self._focus = (zoom, x, y)
            self._focus_seq += 1
            r = self._prefetch_radius
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    self._enqueue_locked(zoom, x + dx, y + dy,
                                         dist=math.hypot(dx, dy), kind=1)

    # ── internals ───────────────────────────────────────────────────────

    def _enqueue_locked(self, zoom, x, y, dist=None, kind=0):
        """Queue a tile. kind 0 = wanted now for drawing, 1 = prefetch ring.

        Needed-now tiles are always fetched: get() only asks for tiles that
        are on screen this frame. Prefetch entries are dropped when the map
        has moved on or changed zoom, which is what keeps a drive from
        downloading the tile pyramid behind us.
        """
        key = (zoom, x, y)
        if key in self._queued or key in self._mem:
            return
        if self._failed_until.get(key, 0.0) > time.monotonic():
            return                          # in retry backoff after a failure
        if len(self._queue) >= 64:      # bounded: never build an unbounded backlog
            return
        if dist is None:
            if self._focus is not None and self._focus[0] == zoom:
                dist = math.hypot(x - self._focus[1], y - self._focus[2])
            else:
                dist = 0.0
        self._queued.add(key)
        # Heap order: needed-now before prefetch, then nearest first, then FIFO.
        heapq.heappush(self._queue, (kind, dist, next(self._seq), zoom, x, y))
        self._cv.notify()

    def _local_path(self, zoom, x, y):
        """Path a fetched tile is stored at. x is wrapped so the antimeridian
        gives one file per place rather than two."""
        n = 2 ** zoom
        return os.path.join(self._writable, str(zoom),
                            str(((x % n) + n) % n), f"{y}.png")

    def _find_on_disk(self, zoom, x, y):
        n = 2 ** zoom
        rel = f"{zoom}/{((x % n) + n) % n}/{y}.png"
        if os.path.isfile(os.path.join(self._writable, rel)):
            return os.path.join(self._writable, rel)
        for root in self._readonly:
            p = os.path.join(root, rel)
            if os.path.isfile(p):
                return p
        return None

    def _decode(self, path):
        try:
            with Image.open(path) as im:
                return im.convert("RGB")
        except (OSError, ValueError) as e:
            logging.debug("tile decode failed %s: %s", path, e)
            return None

    def _remember(self, key, img):
        with self._lock:
            self._mem[key] = img
            self._mem.move_to_end(key)
            while len(self._mem) > self._mem_limit:
                self._mem.popitem(last=False)

    def _worker(self):
        while not self._stop.is_set():
            with self._cv:
                while not self._queue and not self._stop.is_set():
                    self._cv.wait(1.0)
                if self._stop.is_set():
                    return
                _rank, dist, _seq, zoom, x, y = heapq.heappop(self._queue)
                self._queued.discard((zoom, x, y))
                if _rank == 1:
                    # Prefetch: worthless once the map has changed zoom or
                    # driven away from it. Needed-now tiles always proceed.
                    focus = self._focus
                    if focus is None or focus[0] != zoom:
                        continue
                    if math.hypot(x - focus[1], y - focus[2]) > self._prefetch_radius + 2:
                        continue
            if self._stop.wait(self._fetch_throttle):
                return
            # Coarse zooms: build them here rather than in render(), so the
            # one-off downsample cost lands on a background thread and the
            # first frames after a zoom change are not the slow ones. This is
            # also what keeps zooming out working with no network at all, so
            # it must come before the online check.
            if zoom < self._synth_from_zoom:
                synth = self._synthesize(zoom, x, y)
                if synth is not None:
                    self._remember((zoom, x, y), synth)
                    with self._lock:
                        self.synthesized += 1
                    continue
            if not self.online:
                continue
            try:
                ok = self._fetch(zoom, x, y)
            except Exception:               # never let one bad tile kill the fetcher
                logging.exception("tile fetch crashed for z%s/%s/%s", zoom, x, y)
                ok = False
            if ok:
                img = self._decode(self._local_path(zoom, x, y))
                if img is not None:
                    self._remember((zoom, x, y), img)
                with self._lock:
                    self.fetched += 1
                    self._writes_since_sweep += 1
                    sweep = self._writes_since_sweep >= 32
                    if sweep:
                        self._writes_since_sweep = 0
                if sweep:
                    self._sweep_disk()
            else:
                with self._lock:
                    self.failed += 1
                    self._failed_until[(zoom, x, y)] = (time.monotonic()
                                                        + self._fail_backoff)
                    if len(self._failed_until) > 512:
                        now = time.monotonic()
                        self._failed_until = {k: v for k, v in self._failed_until.items()
                                              if v > now}

    def _fetch(self, zoom, x, y):
        n = 2 ** zoom
        if not (0 <= y < n):
            return False                      # beyond the poles: nothing to get
        # {s} supports the a/b/c subdomain form used by OSM and friends.
        url = self._url.format(z=zoom, x=((x % n) + n) % n, y=y,
                               s=random.choice("abc"))
        dst = self._local_path(zoom, x, y)
        tmp = dst + ".part"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": self._ua})
            with urllib.request.urlopen(req, timeout=self._request_timeout) as r:
                data = r.read()
            if not data or data[:8] != b"\x89PNG\r\n\x1a\n":
                logging.debug("tile %s is not a PNG (%d bytes)", url, len(data))
                return False
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dst)              # atomic: render never sees a partial file
            return True
        except (urllib.error.URLError, OSError) as e:
            # One failure is treated as "no network": back off instead of
            # hammering the server while driving through a dead zone.
            self._net_down_until = time.monotonic() + self._net_backoff
            logging.debug("tile fetch failed %s: %s", url, e)
            return False

    def _sweep_disk(self):
        """Keep OUR writable cache under its size cap (never touches read-only)."""
        try:
            entries = []
            total = 0
            for root, _dirs, files in os.walk(self._writable):
                for name in files:
                    if not name.endswith(".png"):
                        continue
                    p = os.path.join(root, name)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    total += st.st_size
                    entries.append((st.st_mtime, st.st_atime, p))
            if total <= self._max_disk_bytes:
                return
            # Oldest-touched first; mtime as tie-break so a re-read tile survives.
            entries.sort(key=lambda e: (e[1], e[0]))
            for _atime, _mtime, p in entries:
                if total <= self._max_disk_bytes * 0.9:
                    break
                try:
                    size = os.path.getsize(p)
                    os.remove(p)
                    total -= size
                except OSError:
                    pass
        except OSError as e:
            logging.debug("tile cache sweep failed: %s", e)


# ── Position / heading estimation ───────────────────────────────────────

# One estimated position/heading, sampled for a single frame.
#   heading_held  -> course is being held because the vehicle is too slow for
#                    course-over-ground to mean anything
#   extrapolated  -> this position is dead-reckoned from an older fix
MapSample = collections.namedtuple(
    "MapSample",
    "lat lon heading speed_mps heading_held extrapolated fix_age")


class PositionEstimator:
    """Smooth, continuous position + heading from ~1 Hz GPS fixes.

    Between fixes the last known velocity is integrated forward (dead
    reckoning), so the map scrolls instead of stepping once a second. Heading
    is low-pass filtered and frozen below MIN_TRACK_SPEED, where course over
    ground stops meaning anything.
    """

    MIN_TRACK_SPEED = 1.5       # m/s (~5 km/h): below this COG is noise
    HEADING_TAU = 0.8           # s: heading filter time constant
    MAX_EXTRAPOLATION = 3.0     # s: stop flying the last vector past a dead fix

    def __init__(self):
        self._lat = None
        self._lon = None
        self._speed = 0.0
        self._track = None          # last trustworthy course over ground
        self._fix_time = 0.0
        self._heading = None        # filtered heading actually drawn
        self._held = True
        self._last_filter_time = 0.0

    def update(self, lat, lon, speed_mps, track_deg, now=None):
        """Feed one GPS fix in. Call whenever the collector has a fix."""
        if lat is None or lon is None:
            return
        if now is None:
            now = time.monotonic()
        with_fresh_position = (self._fix_time == 0.0
                               or lat != self._lat or lon != self._lon)
        self._lat, self._lon = lat, lon
        self._speed = float(speed_mps or 0.0)
        if with_fresh_position:
            self._fix_time = now

        moving = self._speed >= self.MIN_TRACK_SPEED
        valid_track = (track_deg is not None
                       and -360.0 <= float(track_deg) <= 360.0)
        if not (moving and valid_track):
            # Too slow, or no course reported: keep pointing the way we last
            # went and say so, rather than twitching at a stoplight.
            self._held = True
            self._last_filter_time = now
            return
        target = float(track_deg) % 360.0
        self._track = target
        if self._heading is None:
            self._heading = target          # first course: snap, nothing to filter
        else:
            dt_f = max(0.0, now - self._last_filter_time)
            alpha = min(1.0, dt_f / self.HEADING_TAU)
            # Exponential smoothing on the shortest angular path, so a
            # 359->001 wrap turns the corner instead of swinging round.
            self._heading = (self._heading
                             + alpha * angle_delta(target, self._heading)) % 360.0
        self._held = False
        self._last_filter_time = now

    def sample(self, now=None):
        """Current estimated position/heading, or None before the first fix."""
        if self._lat is None:
            return None
        if now is None:
            now = time.monotonic()
        age = now - self._fix_time
        heading = self._heading if self._heading is not None else 0.0
        if age <= 0.0 or age > self.MAX_EXTRAPOLATION or self._speed <= 0.0:
            # Fresh fix, or a fix so stale that guessing is worse than holding.
            return MapSample(self._lat, self._lon, heading, self._speed,
                             self._held, False, min(age, 99.0))
        dt = age
        rad = math.radians(heading)
        north = self._speed * dt * math.cos(rad)
        east = self._speed * dt * math.sin(rad)
        lat = self._lat + north / M_PER_DEG_LAT
        lon = self._lon + east / (EQUATOR_M_PER_DEG_LON * max(0.05, math.cos(math.radians(lat))))
        return MapSample(lat, lon, heading, self._speed, self._held, True, age)

    @property
    def heading_held(self):
        return self._held


class ZoomController:
    """Speed-adaptive zoom with hysteresis and a dwell time.

    Zooming out (wider view) reacts fast — you need the context the moment
    you get on the highway. Zooming in waits for the speed to clear the band
    boundary decisively and stay there, so crawling through a slow patch does
    not thrash the tile pyramid.
    """

    BANDS = ((8.0, 17), (20.0, 16), (float("inf"), 15))
    HYSTERESIS_MPS = 1.5
    DWELL_IN = 5.0      # s a zoom-in must be wanted before it happens
    DWELL_OUT = 1.0     # s for a zoom-out

    def __init__(self, initial=17):
        self._zoom = int(initial)
        self._wanted = None
        self._wanted_since = 0.0

    @property
    def zoom(self):
        return self._zoom

    def _band_for(self, speed):
        for bound, zoom in self.BANDS:
            if speed < bound:
                return zoom
        return self.BANDS[-1][1]

    def update(self, speed_mps, now=None):
        if now is None:
            now = time.monotonic()
        speed = float(speed_mps or 0.0)
        wanted = self._band_for(speed)
        if wanted > self._zoom and speed > self._upper_bound(wanted) - self.HYSTERESIS_MPS:
            # Would zoom in, but not decisively slower than the band below it.
            wanted = self._zoom
        if wanted == self._zoom:
            self._wanted = None
            return self._zoom
        if self._wanted != wanted:
            self._wanted = wanted
            self._wanted_since = now
            return self._zoom
        dwell = self.DWELL_IN if wanted > self._zoom else self.DWELL_OUT
        if now - self._wanted_since >= dwell:
            self._zoom = wanted
            self._wanted = None
        return self._zoom

    def _upper_bound(self, zoom):
        for bound, z in self.BANDS:
            if z == zoom:
                return bound
        return float("inf")


# ── Renderer ────────────────────────────────────────────────────────────

class MovingMap:
    """Renders a heading-up window of the world at a fixed screen centre.

    The mosaic is built one tile grid-aligned around the estimated position,
    then rotated by -heading... precisely: PIL rotates counter-clockwise, and
    we want the direction of travel (bearing `heading`, measured clockwise
    from north) to end up pointing up the screen, which is a counter-clockwise
    rotation of exactly `heading`. See tests/map_view_test.py for the check
    that pins this convention down.
    """

    def __init__(self, tiles, width, height, overscan=12):
        self.tiles = tiles
        self.width = int(width)
        self.height = int(height)
        # Rotation moves corners in; cover the panel diagonal plus a margin so
        # no unrotated edge ever shows inside the crop.
        radius = math.hypot(self.width, self.height) / 2.0 + overscan
        self.canvas = TILE_SIZE * max(1, math.ceil((2 * radius) / TILE_SIZE))

    def render(self, lat, lon, heading, zoom):
        """Return (image width x height, info dict). Missing tiles render blank.

        The mosaic is aligned to whole pixels around the position, so the
        centre is exact to within half a pixel (< 0.5 m at z17) with no
        sub-pixel bookkeeping.
        """
        cx, cy = lonlat_to_world_px(lat, lon, zoom)
        side = self.canvas
        left = int(round(cx - side / 2.0))
        top = int(round(cy - side / 2.0))

        canvas = Image.new("RGB", (side, side), MISSING_TILE_RGB)
        x0 = left // TILE_SIZE
        y0 = top // TILE_SIZE
        nx = ny = 2 ** zoom
        have = want = 0
        for ty in range(y0, y0 + side // TILE_SIZE + 1):
            if not (0 <= ty < ny):
                continue
            for tx in range(x0, x0 + side // TILE_SIZE + 1):
                want += 1
                img = self.tiles.get(zoom, ((tx % nx) + nx) % nx, ty)
                if img is None:
                    continue
                have += 1
                canvas.paste(img, (tx * TILE_SIZE - left, ty * TILE_SIZE - top))

        rotated = canvas.rotate(float(heading) % 360.0,
                                resample=Image.Resampling.BILINEAR,
                                fillcolor=MISSING_TILE_RGB)
        box = ((side - self.width) // 2,
               (side - self.height) // 2,
               (side - self.width) // 2 + self.width,
               (side - self.height) // 2 + self.height)
        window = rotated.crop(box)
        return window, {"tiles_have": have, "tiles_want": want,
                        "meters_per_pixel": meters_per_pixel(lat, zoom),
                        "zoom": zoom}


def scale_bar(meters_per_pixel, max_px=70):
    """Pick the largest round ground distance that fits in max_px.

    Returns (pixels, label). Falls back to the smallest round distance when
    even that does not fit (very coarse zoom), so there is always a bar.
    """
    choices = (10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000)
    chosen = choices[0]
    for dist in choices:
        if dist / meters_per_pixel <= max_px:
            chosen = dist
    px = max(1, int(round(chosen / meters_per_pixel)))
    label = f"{chosen} m" if chosen < 1000 else f"{chosen // 1000} km"
    return px, label


def _halo_text(draw, xy, text, font, fill, halo=(0, 0, 0)):
    """Text with a 1 px dark outline.

    OSM tiles are mostly pale (land is off-white), so plain white overlays
    vanish; a dark halo keeps them readable over both land and water.
    """
    x, y = xy
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx or dy:
                draw.text((x + dx, y + dy), text, font=font, fill=halo)
    draw.text((x, y), text, font=font, fill=fill)


def draw_map_overlay(draw, sample, info, rect, tiny_font,
                     marker_color=(0, 255, 255), text_color="WHITE",
                     north_color=(90, 90, 90)):
    """Draw the fixed screen furniture over a rendered map window.

    Shared by the dashboard view and tests/map_view_preview.py so the two can
    never disagree about where the marker, north tick and scale bar sit:

      * vehicle marker at the exact centre of `rect`, pointing up (up is the
        direction of travel, because the map was rotated by -heading... i.e.
        rotated so heading is up)
      * a north tick that swings around the marker as the map rotates
      * a scale bar bottom-left
      * a "no map data" note when every tile in the window was missing

    `rect` is (left, top, width, height) of the pasted map window; `info` is
    the dict returned by MovingMap.render(); `draw` is an ImageDraw on the
    full-panel image.
    """
    left, top, width, height = rect
    cx = left + width // 2
    cy = top + height // 2

    rad = math.radians(sample.heading)
    r = height // 2 - 18
    n_x = cx - r * math.sin(rad)      # north sits at screen bearing -heading
    n_y = cy - r * math.cos(rad)
    draw.line([(cx, cy), (n_x, n_y)], fill=north_color, width=1)
    _halo_text(draw, (n_x - 4, n_y - 16), "N", tiny_font, "WHITE")

    draw.polygon([(cx, cy - 11), (cx - 9, cy + 10), (cx + 9, cy + 10)],
                 fill=marker_color, outline=(10, 10, 10))

    bar_px, bar_label = scale_bar(info["meters_per_pixel"])
    bar_x = left + 8
    bar_y = top + height - 12
    # Dark underlay one pixel wider, so the bar survives pale tiles.
    draw.line([(bar_x - 1, bar_y), (bar_x + bar_px + 1, bar_y)], fill=(0, 0, 0), width=4)
    draw.line([(bar_x, bar_y), (bar_x + bar_px, bar_y)], fill=text_color, width=2)
    draw.line([(bar_x, bar_y - 3), (bar_x, bar_y + 3)], fill=text_color, width=2)
    _halo_text(draw, (bar_x + bar_px + 5, bar_y - 8), bar_label, tiny_font, "WHITE")

    if info["tiles_have"] == 0:
        # Off the cached area with no network: say so, rather than showing a
        # blank rectangle that reads as a broken display.
        _halo_text(draw, (left + 22, top + height // 2 - 9), "NO MAP DATA",
                   tiny_font, "YELLOW")


def map_header_lines(fix_text, speed_mps, heading, heading_held,
                     sats_used, sats_visible, pos_error_m, zoom,
                     online=True, pending=0):
    """The two status lines above the map, as plain strings.

    Shared by the dashboard view and tests/map_view_preview.py: the format is
    what the operator learns to read at a glance, so the preview must not be
    allowed to drift from what the panel actually shows.
    """
    if heading is None:
        hdg_text = "---"
    else:
        hdg_text = f"{heading % 360:03.0f}\u00b0" + ("H" if heading_held else "")
    line1 = f"{fix_text}  {speed_mps * 3.6:3.0f}km/h  {hdg_text}"
    if not online:
        src_text = "NO NET"
    elif pending:
        src_text = f"+{pending}"
    else:
        src_text = "MAP OK"
    line2 = (f"{sats_used}/{sats_visible} sat "
             f"+/-{pos_error_m:.0f}m z{zoom} {src_text}")
    return line1, line2
