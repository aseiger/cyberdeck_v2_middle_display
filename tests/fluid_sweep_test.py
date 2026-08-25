#!/usr/bin/env python
"""Sweep the REAL fluid view at increasing SPI rates.

Replicates the service's fluid view exactly: dimmed cyberpunk background,
PixFluid sim (top spout), 32px tile-diff partial updates, ShowImageRegion
path. For each rate, runs the live fluid view for a fixed window so the
viewer can classify it:
   - rendering (may flicker)  -> stable at this rate
   - white / corrupted / garbled -> too high (corruption limit)
Finds the highest non-white rate for the actual fluid workload.

Usage: fluid_sweep_test.py [MHz ...] [window_seconds]
       (defaults: 24 26 28 30 31 32  MHz, 8 s window)
"""
import os, sys
# Make the project root importable so these scripts work from tests/.
_proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

import sys, time, os
sys.path.insert(0, "/home/alex/DisplayControl")
import spidev
from PIL import Image, ImageChops, ImageEnhance
from lib import LCD_2inch4
from pixfluid import PixFluid

# ---- exact copies of the service helpers (lcdstats.py) ----
def ChangedTileRegions(previous_image, current_image, tile_size=16):
    difference = ImageChops.difference(previous_image, current_image)
    width, height = current_image.size
    for top in range(0, height, tile_size):
        bottom = min(top + tile_size, height)
        run_left = None
        for left in range(0, width, tile_size):
            right = min(left + tile_size, width)
            changed = difference.crop((left, top, right, bottom)).getbbox() is not None
            if changed and run_left is None:
                run_left = left
            elif not changed and run_left is not None:
                yield (run_left, top, left, bottom)
                run_left = None
        if run_left is not None:
            yield (run_left, top, width, bottom)

def ShowImageRegion(display, image, x_start, y_start):
    width, height = image.size
    if width <= 0 or height <= 0:
        return
    rgb = display.np.asarray(image.convert("RGB"))
    pixels = display.np.zeros((height, width, 2), dtype=display.np.uint8)
    pixels[..., [0]] = display.np.add(
        display.np.bitwise_and(rgb[..., [0]], 0xF8),
        display.np.right_shift(rgb[..., [1]], 5),
    )
    pixels[..., [1]] = display.np.add(
        display.np.bitwise_and(display.np.left_shift(rgb[..., [1]], 3), 0xE0),
        display.np.right_shift(rgb[..., [2]], 3),
    )
    buffer = pixels.flatten().tolist()
    display.command(0x36)
    display.data(0x08)
    display.SetWindows(x_start, y_start, x_start + width, y_start + height)
    display.digital_write(display.DC_PIN, True)
    for offset in range(0, len(buffer), 4096):
        display.spi_writebyte(buffer[offset:offset + 4096])

# ---- setup exactly like the service ----
disp = LCD_2inch4.LCD_2inch4(
    spi=spidev.SpiDev(0, 0), spi_freq=15000000, rst=27, dc=25, bl=18, bl_freq=5000
)
disp.Init(); disp.clear(); disp.bl_DutyCycle(100)

background_path = os.path.join("/home/alex/DisplayControl", "pic", "cyberpunk_bg.png")
background_image = Image.open(background_path).convert("RGB")
background_image = ImageEnhance.Brightness(background_image).enhance(0.25)

fluid = PixFluid(target_size=(disp.width, disp.height))
fluid.reset()

# ---- parse args: rates then optional window ----
args = sys.argv[1:]
window_s = 8
if args and args[-1].replace('.', '').isdigit() and len(args) > 1:
    # last arg could be window only if it's small; keep simple: all args are rates
    pass
rates = [float(r) for r in args] or [24, 26, 28, 30, 31, 32]

displayed_image = None
for i, rate in enumerate(rates):
    disp.SPI.max_speed_hz = int(rate * 1e6)
    print(f"\n########## {rate:g} MHz — WINDOW {i+1}/{len(rates)} — WATCH NOW ##########", flush=True)
    time.sleep(0.3)
    t0 = time.time(); last = t0; n = 0
    while time.time() - t0 < window_s:
        now = time.time(); dt = now - last; last = now
        fluid.step(min(dt, 0.1))
        image1 = fluid.render(background_image)
        image1 = image1.rotate(0)
        if displayed_image is None:
            disp.ShowImage(image1)
        else:
            for region in ChangedTileRegions(displayed_image, image1, 32):
                left, top, right, bottom = region
                ShowImageRegion(disp, image1.crop(region), left, top)
        displayed_image = image1
        n += 1
    print(f"########## {rate:g} MHz — END ({n} frames) ##########", flush=True)
    time.sleep(2)

print("\nSweep complete. Report which windows rendered vs went white/garbled.", flush=True)
