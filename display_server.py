#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Unix domain socket server for bi-directional communication between
lcdstats.py and external clients (e.g. GTK brightness/volume applet).

Protocol: JSON Lines (one JSON object per line, terminated by \n).

Client -> Server messages:
  {"type": "brightness", "value": 75}   # LCD backlight duty cycle 0-100
  {"type": "volume",     "value": 50}   # system volume 0-100
  {"type": "get_status"}                 # request current state

Server -> Client messages (sent in response to get_status):
  {"type": "status", "brightness": 75, "volume": 50}
"""

import json
import logging
import os
import select
import socket
import threading

SOCKET_PATH = "/tmp/lcdstats.sock"

logger = logging.getLogger(__name__)


class DisplayControlServer:
    """Thread-safe Unix domain socket server for display control IPC."""

    def __init__(self, socket_path=SOCKET_PATH):
        self.socket_path = socket_path

        # Shared state (protected by lock)
        self._lock = threading.Lock()
        self._brightness = -1.0   # -1 means "no value received yet"
        self._volume = -1.0

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
    def volume(self):
        with self._lock:
            return self._volume

    @property
    def has_brightness(self):
        with self._lock:
            return self._brightness >= 0

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
                            self._handle_message(sock, line.decode("utf-8", errors="replace"))
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

        elif msg_type == "volume":
            value = float(msg.get("value", 0))
            with self._lock:
                self._volume = max(0.0, min(100.0, value))
            logger.debug("Volume -> %.1f", self._volume)

        elif msg_type == "get_status":
            self._send_status(client)

        else:
            logger.warning("Unknown message type: %s", msg_type)

    def _build_status(self):
        with self._lock:
            return json.dumps({
                "type": "status",
                "brightness": round(self._brightness, 1),
                "volume": round(self._volume, 1),
            }) + "\n"

    def _send_status(self, client):
        try:
            client.sendall(self._build_status().encode("utf-8"))
        except OSError:
            pass
