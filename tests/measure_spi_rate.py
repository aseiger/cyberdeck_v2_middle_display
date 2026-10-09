#!/usr/bin/env python
"""Measure the ACTUAL SPI0 bit rate produced for a given requested rate.

Sends one large timed full-duplex transfer (spidev xfer3) and back-calculates
the SCLK frequency from (bits sent) / (wall time). No oscilloscope needed.

The measured rate is a slight UNDER-estimate: the timing includes a small
fixed per-transfer overhead, so use a large buffer to keep the error small.
Treat the numbers as "at least this fast, and on-grid."

Useful after a firmware/kernel update to confirm the SSI clock grid is
unchanged (e.g. that a requested 50 MHz is really 50 MHz, and that requests
between the even-divisor grid points of the 200 MHz clk_sys clamp DOWN, not
up: 100/50/33.3/25/20/16.7/... MHz).

Safe: probe bytes are harmless to the ST7789. Re-run a normal Init()
afterwards to restore a known state.

Usage: measure_spi_rate.py [MHz ...]   (defaults: 10 25 50 80)
"""
import sys, time
sys.path.insert(0, "/home/alex/DisplayControl")
import spidev

NBYTES = 2_000_000                      # 2 MB -> 16 Mbit (overhead ~= negligible)
buf = bytes([0x55]) * NBYTES            # build once, OUTSIDE the timing
warm = bytes([0x00]) * 4096

reqs = [int(round(float(a) * 1e6)) for a in sys.argv[1:]] or \
       [10_000_000, 25_000_000, 50_000_000, 80_000_000]

spi = spidev.SpiDev(0, 0)
spi.mode = 0

print(f"buffer: {NBYTES} bytes ({8*NBYTES/1e6:.0f} Mbit); measured ~= at least this fast\n")
print(f"{'requested':>10} | {'measured':>10} | {'ms (2MB)':>9} | note")
print("-" * 56)
for freq in reqs:
    spi.max_speed_hz = freq
    spi.xfer3(warm)                     # warm-up / apply the new rate
    t0 = time.perf_counter()
    spi.xfer3(buf)
    dt = time.perf_counter() - t0
    measured = (8 * NBYTES) / dt / 1e6  # Mbit/s == MHz
    pct = 100.0 * measured / (freq / 1e6)
    note = f"~{pct:.0f}% of requested (overhead-limited)"
    print(f"{freq/1e6:9.1f} MHz | {measured:9.1f} MHz | {dt*1e3:8.1f} ms | {note}")
