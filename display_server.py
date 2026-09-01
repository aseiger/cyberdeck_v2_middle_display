#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Unix domain socket server for bi-directional communication between
lcdstats.py and external clients (e.g. GTK brightness/volume applet).

Protocol: JSON Lines (one JSON object per line, terminated by \n).

Client -> Server messages:
  {"type": "brightness",     "value": 75}   # main display brightness 0-100
  {"type": "lcd_brightness", "value": 75}   # SPI LCD backlight duty cycle 0-100
  {"type": "volume",         "value": 50}   # system volume 0-100
  {"type": "view",           "value": 1}    # switch to screen view N (0=dashboard, 1=fluid, 2=repeater)
  {"type": "fluid", "action": "perturb",
   "x":0.5, "y":0.7, "strength":600, "radius":40,
   "fx":0.0, "fy":-1.0, "dye":1.0}            # splash/jet into the fluid (view 1)
                                              #   strength in px/s (300-900), radius in px (20-80)
  {"type": "fluid", "action": "reset"}       # reset the fluid (reforms as a pool)
  {"type": "fluid", "action": "burst",
   "side": "left", "duration": 0.6}          # fire the spout on that side for a fixed time
  {"type": "fluid", "action": "spout",
   "side": "left", "on": true, "row": 2}      # hold a spout on/off while a key is held (keywater);
                                              #   row 1-4 = keyboard row -> pour height,
                                              #   side "center" = space bar (top center)
  {"type": "get_status"}                     # request current state

Server -> Client messages (sent in response to get_status):
  {"type": "status", "brightness": 75, "lcd_brightness": 75, "volume": 50,
   "view": 0, "views": ["Dashboard", "Fluid", "Repeater", "SDR",
                        "SDR Band"]}

The server is the source of truth for the available screens: VIEWS below is
the single registry of screen names (position in the list == view index). It
is advertised to every client in the `views` field of each status message, so
clients never hardcode their own screen lists. New screens are added here
(and given a render branch in lcdstats.py).
"""

import json
import logging
import os
import select
import socket
import threading

# Canonical list of LCD screens. The position is the view index used by the
# "view" client messages and the `view` field of status responses.
VIEWS = [
    "Dashboard",  # 0: full dashboard (time, GPS, network, CPU, fan, battery)
    "Fluid",      # 1: live pixel fluid simulation
    "Repeater",   # 2: openhop repeater status + packet feed
    "SDR",        # 3: RTL-SDR spectrum waterfall (dongle open only while shown)
    "SDR Band",   # 4: full-band log-frequency band map, 24 MHz–1.76 GHz
]

SOCKET_PATH = "/tmp/lcdstats.sock"


logger = logging.getLogger(__name__)


class DisplayControlServer:
    """Thread-safe Unix domain socket server for display control IPC."""

    def __init__(self, socket_path=SOCKET_PATH):
        self.socket_path = socket_path

        # Shared state (protected by lock)
        self._lock = threading.Lock()
        self._brightness = -1.0   # -1 means "no value received yet"
        self._lcd_brightness = -1.0  # SPI LCD backlight, -1 means "no value received yet"
        self._volume = -1.0
        self._current_view = 0    # active screen view index
        self._fluid_events = []   # pending fluid-sim events, drained by the render thread

        self._server_sock = None
        self._clients = []        # list of connected client sockets
        self._running = False
        self._thread = None

    # ── Public state access (thread-safe) ────────────────────────────

    @property
    def brightness(self):
        with self._lock:
            return self._brightness

    @property
    def lcd_brightness(self):
        with self._lock:
            return self._lcd_brightness

    @property
    def volume(self):
        with self._lock:
            return self._volume

    @property
    def current_view(self):
        with self._lock:
            return self._current_view

    @property
    def view_count(self):
        """Number of registered screens (len of the VIEWS registry)."""
        return len(VIEWS)

    def set_view(self, view_index):
        # Clamp to the registered screen range — VIEWS is the source of truth,
        # so clients can never select a screen that does not exist.
        with self._lock:
            self._current_view = max(0, min(int(view_index), len(VIEWS) - 1))

    def set_lcd_brightness(self, value):
        """Seed the LCD backlight state (e.g. the daemon's initial hardware value)."""
        with self._lock:
            self._lcd_brightness = max(0.0, min(100.0, float(value)))

    def drain_fluid_events(self):
        """Pop and return all pending fluid-simulation events (view 1).

        Called by the fluid render thread before each step; returns a list
        of event dicts (each with an ``action`` key).
        """
        with self._lock:
            events = self._fluid_events
            self._fluid_events = []
        return events

    @property
    def has_brightness(self):
        with self._lock:
            return self._brightness >= 0

    @property
    def has_lcd_brightness(self):
        with self._lock:
            return self._lcd_brightness >= 0

    @property
    def has_volume(self):
        with self._lock:
            return self._volume >= 0

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self):
        """Start the server in a background daemon thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("DisplayControlServer started on %s", self.socket_path)

    def stop(self):
        """Shutdown the server and clean up."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        self._cleanup()

    # ── Internal ─────────────────────────────────────────────────────

    def _cleanup(self):
        for c in self._clients:
            try:
                c.close()
            except OSError:
                pass
        self._clients.clear()
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass
            self._server_sock = None
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

    def _run(self):
        # Remove stale socket file
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

        self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind(self.socket_path)
        # Make socket world-writable so any user can connect
        os.chmod(self.socket_path, 0o777)
        self._server_sock.listen(5)
        self._server_sock.setblocking(False)

        buffers = {}  # client socket -> byte buffer

        while self._running:
            readable = [self._server_sock] + self._clients
            try:
                ready, _, _ = select.select(readable, [], [], 0.1)
            except (ValueError, OSError):
                break

            for sock in ready:
                if sock is self._server_sock:
                    # Accept new connection
                    try:
                        client, _ = self._server_sock.accept()
                        client.setblocking(False)
                        self._clients.append(client)
                        buffers[client] = b""
                        logger.info("Client connected")
                        self._send_status(client)
                    except OSError:
                        pass
                else:
                    # Read from client
                    try:
                        data = sock.recv(4096)
                        if not data:
                            raise ConnectionResetError
                        buffers[sock] += data
                        # Process complete lines
                        while b"\n" in buffers[sock]:
                            line, buffers[sock] = buffers[sock].split(b"\n", 1)
                            try:
                                self._handle_message(sock, line.decode("utf-8", errors="replace"))
                            except Exception:
                                # Never let one bad message kill the server
                                # thread (bad types, unexpected shapes) — log and keep serving.
                                logger.warning(
                                    "Error handling message from client: %r",
                                    line[:200], exc_info=True,
                                )
                    except (OSError, ConnectionResetError):
                        logger.info("Client disconnected")
                        self._clients.remove(sock)
                        buffers.pop(sock, None)
                        try:
                            sock.close()
                        except OSError:
                            pass

        self._cleanup()

    def _handle_message(self, client, raw):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Bad JSON from client: %s", raw)
            return

        msg_type = msg.get("type")

        if msg_type == "brightness":
            value = float(msg.get("value", 0))
            with self._lock:
                self._brightness = max(0.0, min(100.0, value))
            logger.debug("Brightness -> %.1f", self._brightness)

        elif msg_type == "lcd_brightness":
            value = float(msg.get("value", 0))
            with self._lock:
                self._lcd_brightness = max(0.0, min(100.0, value))
            logger.debug("LCD brightness -> %.1f", self._lcd_brightness)

        elif msg_type == "volume":
            value = float(msg.get("value", 0))
            with self._lock:
                self._volume = max(0.0, min(100.0, value))
            logger.debug("Volume -> %.1f", self._volume)

        elif msg_type == "view":
            # Clamp through set_view(): the VIEWS registry is the source of
            # truth, so out-of-range values land on the nearest valid screen.
            value = int(float(msg.get("value", 0)))
            self.set_view(value)
            logger.debug("View -> %d", self.current_view)

        elif msg_type == "fluid":
            event = {k: v for k, v in msg.items() if k != "type"}
            with self._lock:
                self._fluid_events.append(event)
            logger.debug("Fluid event -> %s", event.get("action"))

        elif msg_type == "get_status":
            self._send_status(client)

        else:
            logger.warning("Unknown message type: %s", msg_type)

    def _build_status(self):
        with self._lock:
            return json.dumps({
                "type": "status",
                "brightness": round(self._brightness, 1),
                "lcd_brightness": round(self._lcd_brightness, 1),
                "volume": round(self._volume, 1),
                "view": self._current_view,
                # Advertised screen registry: position == view index.
                "views": list(VIEWS),
            }) + "\n"

    def _send_status(self, client):
        try:
            client.sendall(self._build_status().encode("utf-8"))
        except OSError:
            pass
