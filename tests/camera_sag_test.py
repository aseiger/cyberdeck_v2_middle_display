#!/usr/bin/env python
"""Quantify whether SPI activity dims the LCD backlight (power-sag test).

Locks the camera exposure (assumed set manually beforehand), shows a STATIC
mid-gray frame, warms everything up, VERIFIES the screen is bright, then
measures the panel's camera luminance:
  A  = SPI idle   (no pushes)
  B  = SPI busy   (same gray frame pushed back-to-back at 50 MHz)
  A2 = SPI idle   (again, to check for drift)

Each state is a fresh ffmpeg capture (no stale pipe buffer). If B < A (and
A2 ~ A), SPI activity dims the backlight; delta is the sag.

Usage: camera_sag_test.py
"""
import sys, time, threading, subprocess
sys.path.insert(0, "/home/alex/DisplayControl")
import numpy as np
import spidev
from PIL import Image
from lib import LCD_2inch4

CAM_W, CAM_H = 640, 480

def capture_n(n=30):
    """Fresh capture of n frames; return list of per-frame means."""
    p = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "v4l2", "-input_format", "mjpeg",
         "-video_size", f"{CAM_W}x{CAM_H}", "-i", "/dev/video0",
         "-frames:v", str(n), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    data = p.stdout
    out = []
    for i in range(n):
        sl = data[i * CAM_W * CAM_H:(i + 1) * CAM_W * CAM_H]
        if len(sl) < CAM_W * CAM_H:
            break
        out.append(np.frombuffer(sl, dtype=np.uint8).reshape(CAM_H, CAM_W).astype(float).mean())
    return out

spi = spidev.SpiDev(0, 0); spi.mode = 0
disp = LCD_2inch4.LCD_2inch4(spi=spi, spi_freq=50_000_000, spi_init_freq=1_000_000,
                             rst=27, dc=25, bl=18, bl_freq=5000)
disp.Init(); disp.bl_DutyCycle(100)
gray = Image.new("RGB", (disp.width, disp.height), (170, 170, 170))
disp.ShowImage(gray)
print("screen warm-up (4 s)...", flush=True)
time.sleep(4.0)

# VERIFY the screen is actually bright before measuring
v = capture_n(15)
vmean = float(np.mean(v))
print(f"verify: mean={vmean:.2f} (n={len(v)})", flush=True)
if vmean < 50:
    print("screen not bright yet — waiting 4 s and re-verifying", flush=True)
    time.sleep(4.0)
    v = capture_n(15)
    vmean = float(np.mean(v))
    print(f"verify: mean={vmean:.2f} (n={len(v)})", flush=True)
    if vmean < 50:
        print("ERROR: screen not bright; aborting", flush=True); sys.exit(1)

A = capture_n(40)
print(f"A  (SPI idle)  : n={len(A)} mean={np.mean(A):.2f}", flush=True)

stop = False
def push_loop():
    while not stop:
        disp.ShowImage(gray)
th = threading.Thread(target=push_loop, daemon=True); th.start()
time.sleep(0.4)
B = capture_n(60)
stop = True; th.join(timeout=3)
print(f"B  (SPI busy)  : n={len(B)} mean={np.mean(B):.2f}", flush=True)

time.sleep(0.4)
A2 = capture_n(40)
print(f"A2 (SPI idle)  : n={len(A2)} mean={np.mean(A2):.2f}", flush=True)

mA, mB, mA2 = float(np.mean(A)), float(np.mean(B)), float(np.mean(A2))
delta = mB - mA
print(f"\n=== RESULT ===")
print(f"idle A={mA:.2f}  busy B={mB:.2f}  idle A2={mA2:.2f}")
print(f"delta (busy-idle) = {delta:+.2f}  ({100*delta/mA:+.1f}%)")
print(f"idle drift A->A2 = {mA2-mA:+.2f}")
if delta < -0.5:
    print(">>> SPI activity DIMS the backlight (sag CONFIRMED).")
elif delta > 0.5:
    print(">>> SPI activity brightens it (unexpected).")
else:
    print(">>> No measurable change from SPI activity.")

disp.bl_DutyCycle(0)
