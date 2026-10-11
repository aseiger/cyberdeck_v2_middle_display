# LCD backlight sag under SPI load — diagnosis & fix

**Status (2026-10-11):** working around it in software (GRAM at 10 MHz).
Permanent fix planned: run the backlight from a **5 V** supply.

## Symptom

The 2.8" Waveshare LCD (ST7789) looks dimmer during active display and
recovers to full brightness when the display goes quiet. Perceived as the
screen "fluctuating" or being "dimmer than it used to be."

## Ruled out (all tested)

| Suspect | Result |
|---|---|
| Backlight PWM duty | 100% (duty==period on pwmchip0/pwm2) |
| Applet brightness | holding 100 (`journalctl -u lcdstats \| grep "LCD brightness"`) |
| Panel NVCOM/gamma/init sequence | A/B of init @ xfer3/1 MHz vs writebytes/10 MHz → identical brightness |
| CPU load / thermal (delamination) | static frame under 4-core load to 71 °C → no change, no slow recovery; frame spatially uniform (no spots) |
| SPI clock rate being "wrong" | measured SCLK matches requested grid rate |
| White-screen fixes (xfer3, 1 MHz boot frames) | unrelated to brightness |

## Root cause

The module is **all 3.3 V**. The backlight white LEDs have Vf ≈ 3.0–3.2 V,
leaving only ~0.1–0.2 V of headroom on the 3.3 V rail. Every SPI transfer
bursts current (SSI switching at the SCLK rate + panel GRAM-write current),
dipping the shared 3.3 V; with almost no headroom, that dip cuts the LED
current a lot → visible dimming. It recovers as soon as the SPI goes idle.

## Measured sag (tests/camera_sag_test.py)

Locked-exposure USB microscope on the panel, static mid-gray frame,
idle vs back-to-back full-frame pushes:

| GRAM rate | idle cam | busy cam | sag |
|---|---|---|---|
| 10 MHz | 84.4 | 82.1 | **2.7 %** |
| 20 MHz | 84.5 | 80.1 | 5.2 % |
| 25 MHz | 86.0 | 78.7 | 8.5 % |
| 33 MHz | 87.5 | 80.3 | 8.2 % |
| 50 MHz | 89.1 | 76.9 | **13.7 %** |

Sag depth scales with the clock rate (burst current rises with rate).
The 2026-09-06 bump from 28 → 50 MHz is what made the dimming noticeable.

## Fixes

1. **Planned (best): backlight on 5 V.** Restores ~1.8 V of headroom, so
   3.3 V-side dips stop mattering. Then re-run `tests/camera_sag_test.py`
   at candidate rates and pick the fastest with acceptable sag
   (expect 50+ MHz to be usable again).
2. **Alternative: bulk decoupling cap** across the backlight 3.3 V/GND at
   the LCD connector, as close to the LED/FET as possible (a few hundred
   µF low-ESR). Stiffens the rail against the burst dips at any rate.
3. **Current software workaround:** `SPI_FREQ_HZ = 10 MHz`
   (sag ≈ 2.7 %, negligible). Cost: full frame ~123 ms (~8 fps worst case)
   — fluid/waterfall views are choppier.

## Re-testing after hardware changes

```
sudo systemctl stop lcdstats.service
.venv/bin/python tests/camera_sag_test.py   # idle vs busy sag at SPI_FREQ_HZ
sudo systemctl start lcdstats.service
```

Camera exposure must be locked before measuring (manual mode, this
microscope: `v4l2-ctl -d /dev/video0 -c auto_exposure=1` then
`-c exposure_time_absolute=<1..500>,gain=0`).
