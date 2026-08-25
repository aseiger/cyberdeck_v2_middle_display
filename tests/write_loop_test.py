#!/usr/bin/env python
"""General full-frame write-loop probe at a target SPI rate.

Usage:  write_loop_test.py [MHz] [seconds]
        (defaults: 24 MHz, 30 s)

Replicates the service init (15 MHz + clear), shows a gray sanity frame,
switches to the target rate, then continuously rewrites the SAME frozen
full frame. Because the content never changes:
   - stays gray  -> the rate is stable for full-frame writes
   - goes white  -> the rate exceeds the wiring/panel corruption limit
This is a clean probe for "highest stable SPI rate".
"""
import os, sys
# Make the project root importable so these scripts work from tests/.
_proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

import sys
import time

RATE_MHZ = float(sys.argv[1]) if len(sys.argv) > 1 else 24.0
DURATION = int(sys.argv[2]) if len(sys.argv) > 2 else 30

sys.path.insert(0, "/home/alex/DisplayControl")
import spidev
from lib import LCD_2inch4
from PIL import Image, ImageDraw

# Init at the conservative 15 MHz (known-clean), like the service does.
disp = LCD_2inch4.LCD_2inch4(
    spi=spidev.SpiDev(0, 0), spi_freq=15000000, rst=27, dc=25, bl=18, bl_freq=5000
)
disp.Init()
disp.clear()
disp.bl_DutyCycle(100)

img = Image.new("RGB", (disp.width, disp.height), (128, 128, 128))
d = ImageDraw.Draw(img)
d.rectangle([(0, 0), (disp.width - 1, disp.height - 1)], outline=(255, 0, 255), width=6)
d.text((20, 140), f"{RATE_MHZ:g} MHz LOOP", fill=(0, 0, 0))
disp.ShowImage(img)

try:
    base = "/sys/class/pwm/pwmchip0/pwm2"
    print("backlight:", open(base + "/duty_cycle").read().strip(),
          open(base + "/period").read().strip(),
          "enable=" + open(base + "/enable").read().strip(), flush=True)
except Exception as e:
    print("WARNING: backlight check failed:", e, flush=True)

print(f"SANITY: gray frame at 15 MHz (label says {RATE_MHZ:g}). Confirm visible.", flush=True)
time.sleep(8)

print(f"SWITCH to {RATE_MHZ:g} MHz — continuous full-frame rewrite for {DURATION} s.", flush=True)
disp.SPI.max_speed_hz = int(RATE_MHZ * 1e6)
time.sleep(0.2)

t0 = time.time()
n = 0
while time.time() - t0 < DURATION:
    disp.ShowImage(img)
    n += 1

print(f"Done: {n} full-frame writes at {RATE_MHZ:g} MHz. "
      f"(~{n / max(DURATION, 1):.1f} writes/s, ~{150000 * 8 / (RATE_MHZ * 1e6):.0f} ms/write)", flush=True)
