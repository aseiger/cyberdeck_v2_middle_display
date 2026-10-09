#!/usr/bin/env python
"""Test: does reducing the NUMBER of SPI transfers per frame fix the
'works ~2 frames then stuck white' behavior?

The service's ShowImage writes each full frame as 38 chunks of 4096 bytes
(38 SPI/DMA transfers per frame). This test writes each full frame as a
SINGLE SPI transfer (1 per frame).

If 8 single-transfer frames stay clean (whereas 2 chunked frames went
white), the corruption is tied to the number of SPI/DMA transfers -> fix is
fewer, larger transfers. If it still goes white after ~2 frames, it's not a
transfer-count issue.

8 frames @ 50 MHz, ~10 s each, after a 12 s READY.
"""
import sys, time
sys.path.insert(0, "/home/alex/DisplayControl")
import spidev
from PIL import Image, ImageDraw, ImageFont
from lib import LCD_2inch4

disp = LCD_2inch4.LCD_2inch4(
    spi=spidev.SpiDev(0, 0),
    spi_freq=50_000_000,
    spi_init_freq=1_000_000,
    rst=27, dc=25, bl=18, bl_freq=5000,
)
disp.Init()
disp.bl_DutyCycle(100)
spi = disp.SPI
spi.mode = 0
W, H = disp.width, disp.height

try:
    big   = ImageFont.truetype("/home/alex/DisplayControl/Font/Font02.ttf", 90)
    small = ImageFont.truetype("/home/alex/DisplayControl/Font/Font02.ttf", 28)
except Exception:
    big = small = ImageFont.load_default()

def make_frame(num):
    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle([(0, 0), (W - 1, H - 1)], outline=(0, 255, 255), width=3)
    d.text((70, 90), str(num), fill=(0, 255, 0), font=big)
    d.text((16, 220), "single transfer", fill=(0, 200, 255), font=small)
    d.text((16, 255), "how far does it stay clean?", fill=(255, 255, 0), font=small)
    return img

def show_single(img):
    """Write the whole frame in ONE spi.transfer (no 4096-byte chunking)."""
    rgb = disp.np.asarray(img.convert("RGB"))
    pix = disp.np.zeros((H, W, 2), dtype=disp.np.uint8)
    pix[..., [0]] = disp.np.add(disp.np.bitwise_and(rgb[..., [0]], 0xF8),
                                disp.np.right_shift(rgb[..., [1]], 5))
    pix[..., [1]] = disp.np.add(disp.np.bitwise_and(disp.np.left_shift(rgb[..., [1]], 3), 0xE0),
                                disp.np.right_shift(rgb[..., [2]], 3))
    buf = pix.flatten().tolist()
    disp.command(0x36)
    disp.data(0x08)
    disp.SetWindows(0, 0, W, H)
    disp.digital_write(disp.DC_PIN, True)
    # xfer3 (full-duplex) has no 4096-byte cap, unlike writebytes. RX side is
    # unconnected (MISO not wired) so the returned data is ignored.
    spi.xfer3(buf)               # <-- ONE transfer for the whole frame

# READY
show_single(make_frame(0))
print("=== READY (12 s) — look at the display ===", flush=True)
time.sleep(12)

for i in range(1, 9):
    show_single(make_frame(i))
    print(f"*** FRAME {i}/8 (single transfer) ***", flush=True)
    time.sleep(10)

print("=== DONE ===", flush=True)
disp.bl_DutyCycle(0)
