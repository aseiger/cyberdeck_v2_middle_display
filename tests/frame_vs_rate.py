#!/usr/bin/env python
"""Isolate: is it FRAME NUMBER (first works, rest fail) or RATE?

All frames are black with a big label.
  F1 = 50 MHz   (baseline, should work)
  F2 = 50 MHz   (same rate as F1 -> tests H1: 2nd frame at same rate)
  F3 = 50 MHz   (confirmation)
  F4 = 33 MHz   (rate change -> tests H2)
  F5 = 10 MHz   (much slower -> tests H2)

GET READY holds 12 s first.
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
    big   = ImageFont.truetype("/home/alex/DisplayControl/Font/Font02.ttf", 90)
    small = ImageFont.truetype("/home/alex/DisplayControl/Font/Font02.ttf", 30)
except Exception:
    big = small = ImageFont.load_default()

def frame(num, mhz, color=(0, 255, 0)):
    img = Image.new("RGB", (disp.width, disp.height), (0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle([(0, 0), (disp.width - 1, disp.height - 1)], outline=(0, 255, 255), width=3)
    d.text((60, 90), str(num), fill=color, font=big)
    d.text((16, 220), f"{mhz} MHz", fill=(0, 200, 255), font=small)
    d.text((16, 255), "clean or white?", fill=(255, 255, 0), font=small)
    return img

disp.set_spi_speed(10_000_000)
disp.ShowImage(frame("READY", "", (0, 255, 255)))
print("=== GET READY (12 s) — look at the display now ===", flush=True)
time.sleep(12)

plan = [(1, 50), (2, 50), (3, 50), (4, 33), (5, 10)]
for num, mhz in plan:
    disp.set_spi_speed(int(mhz * 1_000_000))
    disp.ShowImage(frame(num, mhz))
    print(f"*** FRAME {num} @ {mhz} MHz (10 s) ***", flush=True)
    time.sleep(10)

print("=== DONE ===", flush=True)
disp.bl_DutyCycle(0)
