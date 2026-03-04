# Cyberdeck V2 Middle Display

A Raspberry Pi system-stats dashboard for a Waveshare 2.4" SPI LCD (240×320), designed as the middle display of a cyberdeck build. The display shows real-time system information including clock, network status, CPU/memory/disk usage, and analog-style gauges.

## Features

- **Real-time clock** — large HH:MM:SS display with date
- **Network info** — IP address, Wi-Fi SSID, signal quality & RSSI
- **System stats** — CPU usage & temperature, memory usage, disk usage, uptime
- **Analog gauges** — half-circle needle gauges for volume, CPU, and memory
- **Brightness/volume bars** — visual indicators synced from an external applet
- **IPC server** — Unix domain socket server (`/tmp/lcdstats.sock`) allowing external clients (e.g. a GTK applet) to control LCD backlight brightness and report system volume
- **Case fan control** — mirrors the CPU fan PWM to a secondary case fan via GPIO
- **Hardware PWM backlight** — flicker-free backlight control via kernel sysfs, supporting both Pi 4 and Pi 5
- **systemd service** — runs automatically at boot

## Hardware

- Raspberry Pi (tested on Pi 5; also supports Pi 4)
- Waveshare 2.4" IPS LCD (240×320, SPI interface)
- SPI bus 0, device 0
- GPIO pins:
  | Function | BCM Pin |
  |---|---|
  | LCD Reset | 27 |
  | LCD DC | 25 |
  | LCD Backlight | 18 |
  | Case Fan PWM | 13 |

## Prerequisites

- Python 3 with a virtual environment
- SPI enabled (`sudo raspi-config` → Interface Options → SPI)
- System packages: `sysstat` (for `mpstat`)

## Installation

```bash
# Clone the repository
git clone git@github.com:aseiger/cyberdeck_v2_middle_display.git
cd cyberdeck_v2_middle_display

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install Python dependencies
pip install spidev Pillow gpiozero numpy
```

## Usage

### Run manually

```bash
source .venv/bin/activate
python lcdstats.py
```

### Install as a systemd service

```bash
sudo cp lcdstats.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lcdstats.service
```

Check status:

```bash
sudo systemctl status lcdstats.service
```

## IPC Protocol

External clients can connect to the Unix domain socket at `/tmp/lcdstats.sock` using JSON Lines (one JSON object per line).

**Client → Server:**

```json
{"type": "brightness", "value": 75}
{"type": "volume", "value": 50}
{"type": "get_status"}
```

**Server → Client:**

```json
{"type": "status", "brightness": 75.0, "volume": 50.0}
```

## Project Structure

| File | Description |
|---|---|
| `lcdstats.py` | Main application — renders the stats dashboard to the LCD |
| `display_server.py` | Unix domain socket IPC server for brightness/volume control |
| `systemStats.py` | Threaded system statistics collector (CPU, memory, disk, network, etc.) |
| `gaugeWidget.py` | Half-circle analog gauge drawing widget |
| `lcdstats.service` | systemd unit file for running at boot |
| `lib/` | Waveshare LCD driver library (SPI, GPIO, hardware PWM) |
| `Font/` | TrueType fonts used by the display |

## License

- `lib/` and `Font/` — MIT License (Waveshare)
- `systemStats.py` — GPL v2+
