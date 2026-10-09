#!/usr/bin/env python
"""Write a known pattern to ST7789 GRAM, read it back over SPI, compare.

NOTE: requires the panel's MISO line to be wired to SPI0_MISO. The 2.4"
panel in the cyberdeck is write-only (MISO not connected), so on that
hardware every read-back comes out 0x00 — that is NOT corruption.

Diagnoses whether the SPI TX path is delivering the bytes we intend:
  - readback == pattern  -> SPI TX + panel storage OK (white is display-side)
  - readback == 0xFFFF   -> SSI is clocking all-ones (TX corruption) -> white
  - readback == garbage  -> SSI TX corrupted another way

Does several write+read cycles per rate to reproduce the "works a few times
then breaks" behavior, and at two rates (5 MHz baseline, 50 MHz service).

Usage: gram_readback_test.py [MHz ...]   (defaults: 5 50)
"""
import sys, time
sys.path.insert(0, "/home/alex/DisplayControl")
import spidev
from lib import LCD_2inch4

W, H = 240, 320
RX_X, RX_Y, RX_N = 0, 160, 24          # read-back region: 24 px = 48 bytes

def px(x, y):
    return ((y * W + x) * 7 + 3) & 0xFFFF

disp = LCD_2inch4.LCD_2inch4(
    spi=spidev.SpiDev(0, 0),
    spi_freq=5_000_000,
    spi_init_freq=1_000_000,
    rst=27, dc=25, bl=18, bl_freq=5000,
)
disp.Init()
disp.bl_DutyCycle(100)
spi = disp.SPI
spi.mode = 0

def write_full(rate_hz):
    spi.max_speed_hz = rate_hz
    buf = []
    for y in range(H):
        for x in range(W):
            v = px(x, y)
            buf.append(v & 0xFF)
            buf.append((v >> 8) & 0xFF)
    disp.SetWindows(0, 0, W, H)
    disp.digital_write(disp.DC_PIN, True)
    for i in range(0, len(buf), 4096):
        spi.writebytes(buf[i:i+4096])

def read_region(rate_hz, x0, y0, n):
    spi.max_speed_hz = rate_hz
    def cmd(c):
        disp.digital_write(disp.DC_PIN, False)
        spi.writebytes([c])
    def dat(v):
        disp.digital_write(disp.DC_PIN, True)
        spi.writebytes([v & 0xFF])
    cmd(0x2A); dat(x0 >> 8); dat(x0 & 0xFF); dat((x0+n-1) >> 8); dat((x0+n-1) & 0xFF)
    cmd(0x2B); dat(y0 >> 8); dat(y0 & 0xFF); dat(y0 >> 8); dat(y0 & 0xFF)
    cmd(0x2E)                       # RAM read
    disp.digital_write(disp.DC_PIN, True)
    nread = n * 2 + 8               # extra bytes for the dummy-first-byte quirk
    rx = list(spi.xfer3([0x00] * nread))
    return rx

def expected_bytes():
    out = []
    for x in range(RX_X, RX_X + RX_N):
        v = px(x, RX_Y)
        out.append(v & 0xFF)
        out.append((v >> 8) & 0xFF)
    return out

exp = expected_bytes()
rates = [int(round(float(a))) for a in sys.argv[1:]] or [5, 50]

for mhz in rates:
    rate = int(mhz * 1e6)
    print(f"\n===== {mhz} MHz =====", flush=True)
    for i in range(6):
        write_full(rate)
        rx = read_region(rate, RX_X, RX_Y, RX_N)
        # search for exp at offsets 0..8
        match_at = None
        for off in range(0, 9):
            if rx[off:off+len(exp)] == exp:
                match_at = off
                break
        n_ff = sum(1 for b in rx[:len(exp)] if b == 0xFF)
        if match_at is not None:
            verdict = f"OK (pattern at offset {match_at})"
        elif n_ff == len(exp):
            verdict = "ALL-ONES  <- SSI TX corrupt (writes 0xFFFF)"
        else:
            verdict = f"GARBAGE ({n_ff}/{len(exp)} bytes are 0xFF)"
        print(f"  cycle {i+1}: {verdict}", flush=True)
        if match_at is None:
            print(f"      expected: {''.join(f'{b:02x}' for b in exp[:12])}...", flush=True)
            print(f"      readback: {''.join(f'{b:02x}' for b in rx[:12])}...", flush=True)
        time.sleep(0.2)

print("\nDone. Backlight off.", flush=True)
disp.bl_DutyCycle(0)
