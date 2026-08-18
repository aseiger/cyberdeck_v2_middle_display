# GTK Applet — Handoff Instructions

## Overview

Build a small GTK (or GTK-like) system-tray / desktop applet that communicates with the LCD display daemon over a Unix domain socket. The applet provides sliders for main display brightness, SPI LCD backlight brightness, and volume, plus a button to cycle through screen views.

## IPC Protocol

**Socket path:** `/tmp/lcdstats.sock`

**Transport:** Unix domain socket, TCP-style (connect, send, recv).

**Message format:** JSON Lines — one JSON object per line, terminated by `\n`.

### Client → Server Messages

```json
{"type": "brightness", "value": 75}
```
Set the **main display brightness**. `value` is a float 0–100. This controls the primary screen's brightness (the display the applet itself lives on), NOT the SPI LCD.

```json
{"type": "lcd_brightness", "value": 75}
```
Set the **SPI LCD backlight** duty cycle. `value` is a float 0–100. This independently controls the Waveshare 2.4" SPI display's backlight, decoupled from the main display brightness.

```json
{"type": "volume", "value": 50}
```
Set system audio volume. `value` is a float 0–100. The server passes this through (the display daemon handles the actual volume change via amixer or similar).

```json
{"type": "view", "value": 0}
```
Switch the LCD to screen view `value`. Currently defined views:
- **0** — Full dashboard (time, GPS, network, CPU, memory, disk, fan, battery)
- **1** — Live fluid simulation (pour/pool, runs in real time)

```json
{"type": "fluid", "action": "perturb",
 "x": 0.5, "y": 0.7, "strength": 600, "radius": 40,
 "fx": 0.0, "fy": -1.0, "dye": 1.0}
```
Send a splash/jet into the fluid (view 1). `x`/`y` are normalized
position (0,0 = top-left). `strength` is the velocity impulse in
px/s — 300–900 makes a lively splash (default 500). `radius` is the
blast radius in px, 20–80 (default 40). `fx`/`fy` are the jet
direction (default `0,0` = radial burst). `dye` (0–1) adds a white
flash to the splash. All fields except `action` are optional.

```json
{"type": "fluid", "action": "reset"}
```
Reset the fluid so it falls from the top again.

```json
{"type": "get_status"}
```
Request current state from the server.

### Server → Client Response (to `get_status`)

```json
{"type": "status", "brightness": 75.0, "lcd_brightness": 75.0, "volume": 50.0, "view": 0}
```
- `brightness`: current main display brightness (0–100, or -1 if never set)
- `lcd_brightness`: current SPI LCD backlight level (0–100, or -1 if never set)
- `volume`: current volume level (0–100, or -1 if never set)
- `view`: current active view index

The server also sends an initial `status` response automatically when a new client connects.

## Applet UI Requirements

### Brightness Slider (Main Display)
- Horizontal slider, range 0–100
- Sends `{"type": "brightness", "value": <slider_value>}\n` on value change
- On connect, reads `brightness` from status and sets slider position
- Controls the primary screen brightness only — it does NOT affect the SPI LCD backlight

### LCD Brightness Slider (SPI Display)
- Horizontal slider, range 0–100
- Sends `{"type": "lcd_brightness", "value": <slider_value>}\n` on value change
- On connect, reads `lcd_brightness` from status and sets slider position
- Controls the Waveshare 2.4" SPI LCD backlight independently of the main display brightness
- Label it clearly (e.g. "LCD" or "SPI Display") so it is not confused with the main brightness slider

### Volume Slider
- Horizontal slider, range 0–100
- Sends `{"type": "volume", "value": <slider_value>}\n` on value change
- On connect, reads `volume` from status and sets slider position

### View Cycle Button
- A single button (icon or label like "Views" or "📺")
- Each click increments the view index: 0 → 1 → 0 → 1 ...
- Sends `{"type": "view", "value": <new_view>}\n` on each click
- Button label or icon should reflect the current view (e.g. "Dashboard" vs "Picture")
- On connect, reads `view` from status so the button state is correct

### Connection Handling
- Connect to `/tmp/lcdstats.sock` on startup
- If the socket is unavailable (daemon not running yet), retry every 5 seconds
- On disconnect, attempt to reconnect after 2 seconds
- Show a subtle indicator (red/green dot or tooltip) for connection status

## Technical Notes

- The server is multi-client — multiple applets can connect simultaneously
- Messages are fire-and-forget (brightness/volume/view) — no response expected
- Only `get_status` elicits a response
- The server runs as a systemd service (`lcdstats.service`)
- Python 3 environment with `.venv` in `/home/alex/DisplayControl/.venv/`

## Current Server Code Location

- Server implementation: `/home/alex/DisplayControl/display_server.py`
- Main display loop: `/home/alex/DisplayControl/lcdstats.py`
- Service file: `/home/alex/DisplayControl/lcdstats.service`

## Future Considerations

- More views will be added (view index > 1). The applet should handle an arbitrary number of views — don't hardcode to just 2. A dynamic approach (query `get_status` after each view switch to confirm the current view) is preferred.
- View 1 is a live 2-D particle fluid simulation (see `pixfluid.py`). A natural applet addition is a "poke" button (or touch the screen) that sends a `fluid` perturb message at the tapped location.
