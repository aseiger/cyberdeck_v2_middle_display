# LCD SPI Drive Strength Fix (Pi 5 / RP1)

## Symptom

The 2.4" ST7789 SPI LCD rendered fine on the dashboard view but went
**white** (corrupted GRAM) when switching to full-frame views — worst on
the SDR waterfall. Originally reproducible at 28 MHz; after this fix the
panel is verified clean at 50 MHz.

## Root cause

On the Raspberry Pi 5, the 40-pin header's SPI0 is **RP1**'s SPI
(`1f00050000.spi`, exposed as `/dev/spidev0.0`; the 2712's own SPI is
`spidev10.0` and unused). The five SPI pads are RP1 `gpio7..11`
(CS0 / SCLK / MOSI / MISO / CS1).

The firmware's built-in SPI0 pin config (nodes `rp1_spi0_gpio9` and
`rp1_spi0_cs_gpio7` in the module DTB) sets `drive-strength`/`slew-rate`
only on gpio9-11. **CS0 and SCLK (gpio7/8) were left at the RP1 pad
defaults: 4 mA drive, slow slew.** At ~20 MHz (50 ns SCK period) those
weak, slow clock edges fail signal integrity through the wiring, the
panel samples garbage, and GRAM ends up white.

## The fix

A device-tree overlay that:

1. Adds a pin-config node for all five SPI pads: `drive-strength = <12>`
   (RP1 maximum) and `slew-rate = <1>` (fast).
2. **Appends that node's phandle to the SPI controller's `pinctrl-0`
   state.** This second step is essential: the RP1 pinctrl driver only
   generates maps for nodes that a device references — an unreferenced
   child node of the pinctrl controller is parsed and then silently
   ignored. The original firmware nodes are kept (they carry the CS0
   pull-up / MOSI bias-disable settings).

Files:

- `lcd-spi0-drive.dts` — overlay source (this file is the source of truth)
- `lcd-spi0-drive.dtbo` — compiled binary (what the firmware loads)

## Install (on a Pi 5)

```sh
sudo cp lcd-spi0-drive.dtbo /boot/firmware/overlays/
grep -q lcd-spi0-drive /boot/firmware/config.txt || \
    echo 'dtoverlay=lcd-spi0-drive' | sudo tee -a /boot/firmware/config.txt
sudo reboot
```

Recompile from source if the `.dtbo` is not available:

```sh
dtc -@ -I dts -O dtb -o lcd-spi0-drive.dtbo lcd-spi0-drive.dts
```

## Verify after reboot

```sh
grep -E "pin (7|8|9|10|11) " \
  /sys/kernel/debug/pinctrl/1f000d0000.gpio-pinctrl-rp1/pinconf-pins
```

All five pins must read **`output drive strength (12 mA)`** and
**`slew rate (0x1)`**. Before the fix, gpio7 and gpio8 read
`4 mA` / `slew rate (0x0)`.

Also confirm the maps exist:

```sh
awk '/^device 1f00050000.spi/{f=1} f' /sys/kernel/debug/pinctrl/pinctrl-maps \
  | grep -c "drive"   # should be > 0
```

## Gotchas (learned the hard way)

1. **Unreferenced pinctrl nodes do nothing.** Adding the config node
   under the RP1 gpio pinctrl controller is not enough — the SPI device's
   `pinctrl-0` must reference it. Symptom of forgetting this: the node
   appears in `/sys/firmware/devicetree/base` but the pads never change
   and there are no kernel errors.

2. **Hardcoded phandles.** `fragment@1` references the firmware's
   original state nodes by literal phandle values (`0x2c 0x2d`). If a
   firmware update renumbers phandles, the SPI state will fail to
   resolve and the LCD will stop initializing. Recovery: read the current
   values and update the `.dts`:

   ```sh
   od -An -tu1 /sys/firmware/devicetree/base/axi/pcie@1000120000/rp1/spi@50000/pinctrl-0
   ```

   (big-endian 32-bit cells; they should be the phandles of
   `rp1_spi0_gpio9` / `rp1_spi0_cs_gpio7` under `.../rp1/gpio@d0000`.)

3. **RP1 SPI rate grid.** SCLK comes from the 200 MHz core clock through
   an integer divisor: achievable rates are 100 / 50 / 40 / 33.3 / 25 /
   20 MHz. Requests between grid points (e.g. 80 MHz) are silently
   **clamped down** (80 → 50). The 100 MHz maximum also exceeds the
   ST7789's 80 MHz spec and corrupts the panel on this wiring.
   Verified clean: 50 MHz (`SPI_FREQ_HZ` in `lcdstats.py`).

## Rollback

```sh
sudo sed -i '/dtoverlay=lcd-spi0-drive/d' /boot/firmware/config.txt
sudo reboot
```

(Only needed if the overlay misbehaves; the panel works at ≤20 MHz even
without it.)

## Related history

- 28 MHz used to corrupt the panel while SCLK ran at 4 mA drive — that
  was this bug, not a panel limitation.
- The per-update backlight dimming is a *separate* issue (5V rail sag
  under SPI load), not a rate/drive problem; the real fix is power
  decoupling on the LCD 5V rail.
