#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  gpsStats.py
#
#  Threaded GPS status collector that reads fix status and satellite
#  counts from a running gpsd daemon (default localhost:2947).
#

import json
import socket
import threading
import time


class GPSStatisticsCollector:
    """Reads GPS fix status and satellite counts from gpsd.

    Connects to gpsd over TCP, enables JSON watch mode, and continuously
    parses TPV (fix mode) and SKY (satellite) reports on a background
    thread. All access is non-blocking; if gpsd is unavailable the
    collector reports "no connection" and keeps retrying.
    """

    _MODE_TEXT = {0: "No Fix", 1: "No Fix", 2: "2D", 3: "3D"}

    def __init__(self, host="127.0.0.1", port=2947, reconnect_seconds=5.0,
                 pps_timeout_seconds=3.0):
        self._host = host
        self._port = port
        self._reconnect_seconds = reconnect_seconds
        self._pps_timeout_seconds = pps_timeout_seconds

        self._lock = threading.Lock()
        self._connected = False
        self._fix_mode = 0
        self._sats_used = 0
        self._sats_visible = 0
        self._pps_last_seen = 0.0
        self._last_update = 0.0
        # Additional TPV fields
        self._altitude = 0.0
        self._lat = 0.0
        self._lon = 0.0
        self._speed_mps = 0.0   # meters per second
        self._hdop = 0.0
        self._nmea_last_seen = 0.0

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def Connected(self):
        with self._lock:
            return self._connected

    @property
    def FixMode(self):
        with self._lock:
            return self._fix_mode

    @property
    def FixText(self):
        with self._lock:
            return self._MODE_TEXT.get(self._fix_mode, "No Fix")

    @property
    def SatsUsed(self):
        with self._lock:
            return self._sats_used

    @property
    def SatsVisible(self):
        with self._lock:
            return self._sats_visible

    @property
    def PPSActive(self):
        """True when gpsd has delivered a per-second time strobe recently.

        This covers both a hardware 1PPS edge (PPS report) and gpsd's TOFF
        report, which is the offset between the receiver's fix time and the
        host clock. TOFF arrives at ~1 Hz from many receivers with no 1PPS
        wiring at all, so True here means "gpsd has fresh per-second time
        from the receiver" - NOT "the system clock is disciplined to GPS".
        See TimeFromGPS.
        """
        with self._lock:
            if not self._connected or self._pps_last_seen == 0.0:
                return False
            return (time.monotonic() - self._pps_last_seen) < self._pps_timeout_seconds

    @property
    def TimeFromGPS(self):
        """True when gpsd has a valid fix AND fresh per-second GPS time.

        Requires a position lock (2D or 3D) plus a PPS/TOFF strobe within
        the last pps_timeout_seconds. Says nothing about whether the host
        clock is actually slewed to GPS — that needs a refclock consumer
        (e.g. gpsd SHM/DUMMY + chrony). Named to avoid implying discipline
        it does not measure.
        """
        with self._lock:
            if not self._connected:
                return False
            has_fix = self._fix_mode >= 2
            pps_alive = (self._pps_last_seen > 0.0) and \
                        (time.monotonic() - self._pps_last_seen) < self._pps_timeout_seconds
            return has_fix and pps_alive

    @property
    def Altitude(self):
        """Altitude in meters (MSL)."""
        with self._lock:
            return self._altitude

    @property
    def Latitude(self):
        with self._lock:
            return self._lat

    @property
    def Longitude(self):
        with self._lock:
            return self._lon

    @property
    def SpeedMPS(self):
        """Ground speed in meters per second."""
        with self._lock:
            return self._speed_mps

    @property
    def SpeedKPH(self):
        """Ground speed in kilometers per hour."""
        with self._lock:
            return self._speed_mps * 3.6

    @property
    def HDOP(self):
        """Horizontal dilution of precision (lower is better)."""
        with self._lock:
            return self._hdop

    @property
    def NMEAActive(self):
        """True when NMEA sentences are arriving regularly.

        We track this by watching for any TPV report from gpsd — a live
        fix stream means the receiver is outputting valid NMEA data.
        """
        with self._lock:
            if not self._connected or self._nmea_last_seen == 0.0:
                return False
            return (time.monotonic() - self._nmea_last_seen) < self._pps_timeout_seconds

    def stop(self):
        self._stop.set()

    def _reset_state(self):
        with self._lock:
            self._connected = False
            self._fix_mode = 0
            self._sats_used = 0
            self._sats_visible = 0
            self._pps_last_seen = 0.0
            self._altitude = 0.0
            self._lat = 0.0
            self._lon = 0.0
            self._speed_mps = 0.0
            self._hdop = 0.0
            self._nmea_last_seen = 0.0

    def _run(self):
        while not self._stop.is_set():
            sock = None
            try:
                sock = socket.create_connection(
                    (self._host, self._port), timeout=5.0
                )
                sock.settimeout(5.0)
                # "pps":true is REQUIRED for gpsd to forward PPS/TOFF reports
                # to this socket. Without it we never see a time strobe and
                # TimeFromGPS can never go True, no matter how good the fix.
                sock.sendall(
                    b'?WATCH={"enable":true,"json":true,"pps":true};\n'
                )
                with self._lock:
                    self._connected = True

                buffer = ""
                while not self._stop.is_set():
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break  # gpsd closed the connection
                    buffer += chunk.decode("utf-8", errors="ignore")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if line:
                            self._handle_report(line)
            except (OSError, socket.error):
                pass
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                self._reset_state()

            if self._stop.wait(self._reconnect_seconds):
                break

    def _handle_report(self, line):
        try:
            report = json.loads(line)
        except (ValueError, TypeError):
            return

        report_class = report.get("class")

        if report_class == "TPV":
            mode = report.get("mode", 0)
            try:
                mode = int(mode)
            except (ValueError, TypeError):
                mode = 0
            with self._lock:
                self._fix_mode = mode
                self._altitude = float(report.get("alt", 0.0) or 0.0)
                self._lat = float(report.get("lat", 0.0) or 0.0)
                self._lon = float(report.get("lon", 0.0) or 0.0)
                self._speed_mps = float(report.get("speed", 0.0) or 0.0)
                self._hdop = float(report.get("hdop", 0.0) or 0.0)
                self._last_update = time.monotonic()
                self._nmea_last_seen = time.monotonic()

        elif report_class == "SKY":
            satellites = report.get("satellites")
            if isinstance(satellites, list):
                visible = len(satellites)
                used = sum(1 for s in satellites if s.get("used"))
            else:
                # Newer gpsd versions expose aggregate counts directly.
                visible = report.get("nSat", 0)
                used = report.get("uSat", 0)
            with self._lock:
                self._sats_visible = int(visible)
                self._sats_used = int(used)
                self._last_update = time.monotonic()

        elif report_class in ("PPS", "TOFF"):
            # gpsd emits PPS when a hardware 1PPS pulse arrives, and TOFF
            # when it has a precise time offset for the current cycle.
            with self._lock:
                self._pps_last_seen = time.monotonic()
                self._last_update = self._pps_last_seen


def main(args):
    collector = GPSStatisticsCollector()
    while True:
        print(
            f"connected={collector.Connected} "
            f"fix={collector.FixText} "
            f"sats={collector.SatsUsed}/{collector.SatsVisible} "
            f"pps={collector.PPSActive} "
            f"time_from_gps={collector.TimeFromGPS}"
        )
        time.sleep(1)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv))
