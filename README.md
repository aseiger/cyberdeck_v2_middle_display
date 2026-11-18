# Cyberdeck V2 Middle Display

A Raspberry Pi system-stats dashboard for a Waveshare 2.4" SPI LCD (240×320), designed as the middle display of a cyberdeck build. The display shows real-time system information including clock, network status, CPU/memory/disk usage, and analog-style gauges.

## Features

- **Real-time clock** — large HH:MM display with date
- **GPS status** — fix status (No Fix / 2D / 3D), satellite count, and 1PPS timing lock read from `gpsd`
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
- `gpsd` running for GPS status (optional; the indicator shows "No GPS" if unavailable)

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
{"type": "lcd_brightness", "value": 75}
{"type": "volume", "value": 50}
{"type": "view", "value": 4}
{"type": "get_status"}
```

- `brightness` — main display brightness (0–100)
- `lcd_brightness` — SPI LCD backlight duty cycle (0–100), independent of the main display brightness
- `view` — select a screen by index (see Screens below); out-of-range values clamp to the nearest valid screen

**Server → Client:**

```json
{"type": "status", "brightness": 75.0, "lcd_brightness": 75.0, "volume": 50.0, "view": 0,
 "views": ["Dashboard", "Fluid", "Repeater", "SDR", "Map"]}
```

## Screens

The screen list lives in one place — `VIEWS` in `display_server.py` — and is
advertised to every client in the `views` field of each status message, so a
client can cycle screens without hardcoding how many there are. Each entry has
a matching render branch in `lcdstats.py`.

| # | Name | What it shows |
|---|---|---|
| 0 | Dashboard | Clock, GPS (fix / signal / GPS-time), network, CPU, memory, disk, fans, battery |
| 1 | Fluid | Live 2-D pixel fluid sim; keyboard presses pour water (see `keywater.py`) |
| 2 | Repeater | MeshCore/openhop repeater status and a live packet feed |
| 3 | SDR | RTL-SDR spectrum waterfall — the dongle is opened only while this screen is up |
| 4 | Map | Heading-up moving map: GPS status header, then the map rotated so travel direction points up, scrolling under a fixed centre marker. See [docs/gps-map.md](docs/gps-map.md) |

```bash
# Switch screens from the shell:
python3 -c "import json,socket; s=socket.socket(socket.AF_UNIX); s.connect('/tmp/lcdstats.sock'); s.sendall(json.dumps({'type':'view','value':4}).encode()+b'\n')"
```

## Project Structure

| File | Description |
|---|---|
| `lcdstats.py` | Main application — renders the stats dashboard to the LCD |
| `display_server.py` | Unix domain socket IPC server for brightness/volume control |
| `systemStats.py` | Threaded system statistics collector (CPU, memory, disk, network, etc.) |
| `gpsStats.py` | Threaded GPS status collector (fix status, satellites, speed/course, accuracy via `gpsd`) |
| `gpsMap.py` | Heading-up moving map for the Map screen: tile cache (local-first + async online), position/heading estimator, mosaic + rotation renderer |
| `gaugeWidget.py` | Half-circle analog gauge drawing widget |
| `lcdstats.service` | systemd unit file for running at boot |
| `lib/` | Waveshare LCD driver library (SPI, GPIO, hardware PWM) |
| `Font/` | TrueType fonts used by the display |

## License

- `lib/` and `Font/` — MIT License (Waveshare)
- `systemStats.py` — GPL v2+
