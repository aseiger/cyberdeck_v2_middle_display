#!/usr/bin/env python
"""Find the SPI rate at which GRAM (pixel) writes start corrupting the panel.

Init runs at a safe 1 MHz; the backlight is lit; a "GET READY" screen holds
12 s, then a BLACK frame with a big colored rate label is pushed at each
candidate GRAM rate and held ~10 s.

WATCH THE DISPLAY. For each rate, note whether it is:
  - clean (solid black + legible number)  -> usable
  - white / garbled / banded / flickering -> corruption
The highest clean rate is the new SPI_FREQ_HZ.

Usage: gram_rate_test.py [MHz ...]   (defaults: 50 33 25 20 16 10)
"""
import os, sys, time
sys.path.insert(0, "/home/alex/DisplayControl")
import spidev
from PIL import Image, ImageDraw, ImageFont
from lib import LCD_2inch4

disp = LCD_2inch4.LCD_2inch4(
    spi=spidev.SpiDev(0, 0),
    spi_freq=10_000_000,
    spi_init_freq=1_000_000,
    rst=27, dc=25, bl=18, bl_freq=5000,
)
disp.Init()
disp.clear_color(0x0000)
disp.bl_DutyCycle(100)

try:
    font  = ImageFont.truetype("/home/alex/DisplayControl/Font/Font02.ttf", 64)
    small = ImageFont.truetype("/home/alex/DisplayControl/Font/Font02.ttf", 28)
except Exception:
    font = small = ImageFont.load_default()

def frame(title, subtitle="", color=(0, 255, 0)):
    img = Image.new("RGB", (disp.width, disp.height), (0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle([(0, 0), (disp.width - 1, disp.height - 1)], outline=(0, 255, 255), width=3)
    d.text((16, 100), title, fill=color, font=font)
    if subtitle:
        d.text((16, 190), subtitle, fill=(255, 255, 0), font=small)
    return img

# ---- GET READY ----
disp.set_spi_speed(10_000_000)
disp.ShowImage(frame("GET READY", "watch for the rates...", (0, 255, 255)))
print("=== GET READY (12 s) — look at the display now ===", flush=True)
time.sleep(12)

rates = [int(round(float(a))) for a in sys.argv[1:]] or \
        [50, 33, 25, 20, 16, 10]

for i, mhz in enumerate(rates):
    rate = int(mhz * 1_000_000)
    disp.set_spi_speed(rate)
    disp.ShowImage(frame(f"{mhz} MHz", "clean or white?", (0, 255, 0)))
    print(f"*** [{i+1}/{len(rates)}] SHOWING {mhz} MHz  (10 s) ***", flush=True)
    time.sleep(10)

print("=== DONE — backlight off ===", flush=True)
disp.bl_DutyCycle(0)
