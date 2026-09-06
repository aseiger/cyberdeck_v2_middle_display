#!/usr/bin/env bash
#
# install_lcd_spi_drive.sh — install the RP1 SPI0 drive-strength overlay.
#
# Raises CS0/SCLK/MOSI/MISO/CS1 (RP1 gpio7-11) on a Raspberry Pi 5 to
# 12 mA drive + fast slew, fixing white-screen corruption on the 2.4"
# ST7789 LCD. See LCD_SPI_DRIVE.md for background and gotchas.
#
# Usage:  sudo ./install_lcd_spi_drive.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DTBO_SRC="$SCRIPT_DIR/lcd-spi0-drive.dtbo"
DTS_SRC="$SCRIPT_DIR/lcd-spi0-drive.dts"
OVERLAY_DIR="/boot/firmware/overlays"
CONFIG="/boot/firmware/config.txt"
OVERLAY_NAME="lcd-spi0-drive"

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo $0" >&2
    exit 1
fi

# Build the .dtbo from source if the binary isn't shipped/available.
if [[ ! -f "$DTBO_SRC" ]]; then
    if ! command -v dtc >/dev/null; then
        echo "error: $DTBO_SRC not found and dtc is not installed" >&2
        echo "       (apt install device-tree-compiler) or copy the .dtbo" >&2
        exit 1
    fi
    [[ -f "$DTS_SRC" ]] || { echo "error: $DTS_SRC not found" >&2; exit 1; }
    echo "Compiling overlay from source..."
    dtc -@ -I dts -O dtb -o "$DTBO_SRC" "$DTS_SRC"
fi

echo "Installing to $OVERLAY_DIR/$OVERLAY_NAME.dtbo"
cp "$DTBO_SRC" "$OVERLAY_DIR/"

if grep -q "^dtoverlay=$OVERLAY_NAME" "$CONFIG"; then
    echo "Already enabled in $CONFIG — nothing to do."
else
    cat >> "$CONFIG" <<EOF

# Max drive (12 mA) + fast slew on RP1 SPI0 pads — fixes white-screen
# corruption on the 2.4" LCD at higher SPI rates. See LCD_SPI_DRIVE.md.
dtoverlay=$OVERLAY_NAME
EOF
    echo "Enabled in $CONFIG"
fi

echo
echo "Reboot to apply, then verify all five pads read 12 mA / slew 0x1:"
echo "  grep -E 'pin (7|8|9|10|11) ' \\"
echo "    /sys/kernel/debug/pinctrl/1f000d0000.gpio-pinctrl-rp1/pinconf-pins"
