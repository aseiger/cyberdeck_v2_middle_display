#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
map_view_preview.py - render what the LCD "Map" view (view 4) looks like,
to a PNG, without touching the panel.

Uses the same helpers the view uses (gpsMap.MovingMap for the tile mosaic and
rotation, gpsMap.draw_map_overlay for the marker/north/scale bar, and
gpsMap.map_header_lines for the status text), so the picture is what the panel
will show — only the background image and the SPI write are missing.

Position comes from live gpsd when it has a fix, otherwise from the arguments.
Tiles come from the local caches first; pass --online to let missing ones be
fetched (off by default so a preview run never hits a tile server).

Usage:
    .venv/bin/python tests/map_view_preview.py                 # live GPS, 4 headings
    .venv/bin/python tests/map_view_preview.py 43.209 -71.5   # a place you pick
    .venv/bin/python tests/map_view_preview.py --zoom 15
Writes /tmp/gpsmap-test/view-preview.png
"""

import math
import os
import sys
import time

sys.path.insert(0, "/home/alex/DisplayControl")

from PIL import Image, ImageDraw, ImageFont

import gpsMap

OUT = "/tmp/gpsmap-test/view-preview.png"
PANEL_W, PANEL_H = 240, 320
MAP_LEFT, MAP_TOP = 4, 46
MAP_W = PANEL_W - 2 * MAP_LEFT
MAP_H = PANEL_H - MAP_TOP - 6

FONT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "Font", "Font02.ttf")


def live_position():
    """(lat, lon, speed_mps, track) from gpsd, or None if there is no fix."""
    import json
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", 2947), timeout=3)
    except OSError:
        return None
    s.settimeout(4)
    s.sendall(b'?WATCH={"enable":true,"json":true};\n')
    buf, last = "", None
    t0 = time.time()
    while time.time() - t0 < 5:
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            break
        buf += chunk.decode("utf-8", "ignore")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            try:
                r = json.loads(line.strip())
            except ValueError:
                continue
            if r.get("class") == "TPV" and r.get("mode", 0) >= 2:
                last = (float(r.get("lat", 0)), float(r.get("lon", 0)),
                        float(r.get("speed", 0) or 0), r.get("track"))
    s.close()
    return last


def render_frame(tiles, lat, lon, heading, speed_mps, zoom, fonts,
                 sats=(8, 20), err_m=12.0, held=False):
    """One full-panel frame of view 4."""
    mmap = gpsMap.MovingMap(tiles, MAP_W, MAP_H)
    img, info = mmap.render(lat, lon, heading, zoom)

    frame = Image.new("RGB", (PANEL_W, PANEL_H), (0, 0, 0))
    frame.paste(img, (MAP_LEFT, MAP_TOP))
    draw = ImageDraw.Draw(frame)

    line1, line2 = gpsMap.map_header_lines(
        "3D", speed_mps, heading, held, sats[0], sats[1], err_m, zoom,
        online=tiles.online if hasattr(tiles, "online") else True,
        pending=tiles.pending if hasattr(tiles, "pending") else 0)
    draw.text((8, 6), line1, fill="LIME", font=fonts["small"])
    draw.text((8, 6 + 18 + 1), line2, fill=(170, 170, 170), font=fonts["tiny"])
    draw.line([(0, MAP_TOP - 4), (PANEL_W, MAP_TOP - 4)], fill=(50, 50, 50), width=2)

    sample = gpsMap.MapSample(lat, lon, heading, speed_mps, held, False, 0.0)
    gpsMap.draw_map_overlay(draw, sample, info,
                            (MAP_LEFT, MAP_TOP, MAP_W, MAP_H), fonts["tiny"])
    draw.rectangle([(0, 0), (PANEL_W, PANEL_H)], outline=(50, 50, 50), width=5)
    return frame, info


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    explicit_zoom = "--zoom" in sys.argv
    zoom = int(sys.argv[sys.argv.index("--zoom") + 1]) if explicit_zoom else None
    online = "--online" in sys.argv

    pos = None
    if len(args) >= 2:
        pos = (float(args[0]), float(args[1]), 12.0, 90.0)
    else:
        pos = live_position()
        if pos is None:
            pos = (43.1769, -71.1018, 12.0, 90.0)
            print("gpsd unavailable — using Concord NH default position")
    lat, lon, speed_mps, track = pos
    if zoom is None:
        # Let the real controller settle at this speed, so the preview shows
        # the zoom the view would actually be at (speed-adaptive bands).
        zc = gpsMap.ZoomController()
        t = 0.0
        for _ in range(300):
            t += 0.1
            zoom = zc.update(speed_mps, now=t)
    print(f"position {lat:.5f} {lon:.5f}, {speed_mps * 3.6:.0f} km/h, "
          f"track {track}, zoom z{zoom}, online={online}")

    tiles = gpsMap.TileCache()
    if online:
        tiles.start()
    fonts = {"small": ImageFont.truetype(FONT, 18),
             "tiny": ImageFont.truetype(FONT, 14)}

    headings = [0, 90, 180, 270]
    frames = []
    for h in headings:
        frame, info = render_frame(tiles, lat, lon, h, speed_mps, zoom, fonts)
        frames.append((h, frame, info))
        print(f"  heading {h:>3}: tiles {info['tiles_have']}/{info['tiles_want']},"
              f" {info['meters_per_pixel']:.2f} m/px")

    gap = 8
    sheet = Image.new("RGB", (PANEL_W * len(frames) + gap * (len(frames) - 1),
                              PANEL_H + 18), (0, 0, 0))
    d = ImageDraw.Draw(sheet)
    for i, (h, frame, info) in enumerate(frames):
        x = i * (PANEL_W + gap)
        sheet.paste(frame, (x, 18))
        d.text((x + 4, 3), f"heading {h}", fill=(255, 255, 0), font=fonts["tiny"])
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    sheet.save(OUT)
    if online:
        tiles.stop()
    print(f"saved: {OUT}")


if __name__ == "__main__":
    main()
