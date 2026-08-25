#!/usr/bin/env python
"""Diagnostic: push ONE static frame, then drive the backlight pin as a
plain digital HIGH with the PWM engine fully disabled.

If the display still flickers with a frozen frame + DC backlight, the
flicker is electrical (power rail / backlight wiring) or panel-internal
(VCOM/oscillator) — NOT the backlight PWM and NOT the update loop.
"""
import os, sys
# Make the project root importable so these scripts work from tests/.
_proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

import sys
import time
import subprocess
import spidev

sys.path.insert(0, "/home/alex/DisplayControl")
from lib import LCD_2inch4
from PIL import Image, ImageDraw

disp = LCD_2inch4.LCD_2inch4(
    spi=spidev.SpiDev(0, 0), spi_freq=15000000, rst=27, dc=25, bl=18, bl_freq=5000
)
disp.Init()
disp.clear()

img = Image.new("RGB", (disp.width, disp.height), (10, 10, 30))
d = ImageDraw.Draw(img)
d.rectangle([(0, 0), (disp.width - 1, disp.height - 1)], outline=(0, 255, 255), width=4)
d.text((24, 130), "STATIC TEST", fill=(255, 255, 255))
disp.ShowImage(img)

# Take the backlight pin over as a plain digital output, no PWM at all.
base = "/sys/class/pwm/pwmchip0/pwm2"
with open(base + "/duty_cycle", "w") as f:
    f.write("0")
with open(base + "/enable", "w") as f:
    f.write("0")
with open("/sys/class/pwm/pwmchip0/unexport", "w") as f:
    f.write("2")
subprocess.check_call(["pinctrl", "set", "18", "oh"])

print("Static frame pushed. Backlight = plain digital HIGH (PWM engine off).", flush=True)
print("Holding 180 s — WATCH THE DISPLAY: does it still flicker?", flush=True)
time.sleep(180)
print("Done.", flush=True)
