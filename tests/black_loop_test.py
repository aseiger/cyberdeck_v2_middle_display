#!/usr/bin/env python
"""Black-frame continuous rewrite probe.

Rewrites a PURE BLACK full frame continuously at 15 MHz.
A black frame passes no light regardless of backlight level, so:
  - stays solid black  -> the gray-frame flicker was BACKLIGHT dimming (rail sag)
  - flickers toward gray -> it's a PANEL/GRAM artifact (cells toggling)
"""
import os, sys
# Make the project root importable so these scripts work from tests/.
_proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

import sys, time
sys.path.insert(0, "/home/alex/DisplayControl")
import spidev
from lib import LCD_2inch4
from PIL import Image, ImageDraw

RATE_MHZ = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
DURATION = int(sys.argv[2]) if len(sys.argv) > 2 else 40

disp = LCD_2inch4.LCD_2inch4(
    spi=spidev.SpiDev(0, 0), spi_freq=15000000, rst=27, dc=25, bl=18, bl_freq=5000
)
disp.Init(); disp.clear(); disp.bl_DutyCycle(100)

img = Image.new("RGB", (disp.width, disp.height), (0, 0, 0))
d = ImageDraw.Draw(img)
# A 1px magenta frame so we can still see the edge / detect any gray bleed
d.rectangle([(0,0),(disp.width-1,disp.height-1)], outline=(255,0,255))
disp.ShowImage(img)

print(f"BLACK frame shown. {DURATION}s of continuous rewrites at {RATE_MHZ:g} MHz.", flush=True)
print("  solid black  -> backlight/rail-sag mechanism", flush=True)
print("  flickers gray -> panel/GRAM artifact", flush=True)
time.sleep(6)
disp.SPI.max_speed_hz = int(RATE_MHZ * 1e6)
time.sleep(0.2)

t0 = time.time(); n = 0
while time.time() - t0 < DURATION:
    disp.ShowImage(img); n += 1
print(f"Done: {n} writes.", flush=True)
