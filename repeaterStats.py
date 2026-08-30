#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  repeaterStats.py
#
#  Threaded status collector that polls the openhop-repeater HTTP API
#  (GET /api/stats, default localhost:8000) for live mesh statistics.
#

import json
import os
import threading
import urllib.error
import urllib.request


REPEATER_API_HOST = "127.0.0.1"
REPEATER_API_PORT = 8000
REPEATER_POLL_INTERVAL_SECONDS = 5.0
REPEATER_REQUEST_TIMEOUT_SECONDS = 3.0

# How many recent packets to pull from /api/recent_packets each poll. The
# display shows the newest ~12; a few extra give the feed some history so it
# doesn't look empty right after a restart (the endpoint reads SQLite, so the
# records survive repeater restarts).
REPEATER_PACKET_LIMIT = 24

# Mesh protocol payload types (pymc_core/protocol/constants.py). Used to label
# packets in the live feed; unknown values fall back to "T<n>".
PAYLOAD_TYPE_NAMES = {
    0x00: "REQ",
    0x01: "RESP",
    0x02: "TXT",
    0x03: "ACK",
    0x04: "ADVERT",
    0x05: "GTXT",
    0x06: "GDATA",
    0x07: "AREQ",
    0x08: "PATH",
    0x09: "TRACE",
    0x0A: "MULTI",
    0x0B: "CTRL",
    0x0F: "RAW",
}

# Mesh protocol route types (2-bit header field).
ROUTE_TYPE_NAMES = {
    0x00: "TFLOOD",
    0x01: "FLOOD",
    0x02: "DIRECT",
    0x03: "TDIRECT",
}


def payload_type_name(payload_type):
    """Short label for a mesh payload type, e.g. 4 -> 'ADVERT'."""
    try:
        return PAYLOAD_TYPE_NAMES.get(int(payload_type), f"T{int(payload_type)}")
    except (ValueError, TypeError):
        return "?"


def route_type_name(route_type):
    """Short label for a mesh route type, e.g. 1 -> 'FLOOD'."""
    try:
        return ROUTE_TYPE_NAMES.get(int(route_type), f"R{int(route_type)}")
    except (ValueError, TypeError):
        return "?"


# The deployed repeater build enforces authentication on all /api endpoints.
# An API token (created in the repeater web UI, Configuration page) is read
# from the REPEATER_API_KEY environment variable or, failing that, from a
# local file next to this script. Without it every poll gets HTTP 401 and
# the display reports "AUTH FAILED".
REPEATER_API_KEY_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "repeater_api_key"
)


def _load_api_key():
    """Return an API token from env or local file, or None if unavailable."""
    key = os.environ.get("REPEATER_API_KEY", "").strip()
    if not key:
        try:
            with open(REPEATER_API_KEY_FILE, "r") as f:
                key = f.read().strip()
        except OSError:
            key = ""
    return key or None


class RepeaterStatisticsCollector:
    """Polls the openhop-repeater /api/stats endpoint on a background thread.

    The repeater returns engine, airtime, radio and version state in one JSON
    document. All access is non-blocking; if the service is unavailable (or
    errors) the collector reports "not connected" and keeps retrying on the
    poll interval.

    Authentication: the deployed build requires an API token on every /api
    call (sent as the X-API-Key header). The token is taken from the
    REPEATER_API_KEY environment variable or the local file
    ``repeater_api_key`` next to this script; pass ``api_key=...`` explicitly
    to override. If no token is available and the service answers 401, the
    collector reports AuthFailed so the display can say "AUTH FAILED" instead
    of a misleading "SERVICE DOWN".
    """

    def __init__(self, host=REPEATER_API_HOST, port=REPEATER_API_PORT,
                 poll_interval_seconds=REPEATER_POLL_INTERVAL_SECONDS,
                 timeout_seconds=REPEATER_REQUEST_TIMEOUT_SECONDS,
                 api_key=None):
        self._host = host
        self._port = port
        self._poll_interval_seconds = poll_interval_seconds
        self._timeout_seconds = timeout_seconds
        # Explicit argument wins; otherwise fall back to env / local file.
        self._api_key = api_key if api_key is not None else _load_api_key()

        self._lock = threading.Lock()
        self._connected = False
        self._auth_failed = False
        self._node_name = ""
        self._local_hash = ""
        self._version = ""
        self._core_version = ""
        self._rx_count = 0
        self._forwarded_count = 0
        self._dropped_count = 0
        self._crc_error_count = 0
        self._rx_per_hour = 0.0
        self._forwarded_per_hour = 0.0
        self._uptime_seconds = 0.0
        self._noise_floor_dbm = None
        self._mode = ""
        self._radio_status = ""
        self._radio_error = ""
        self._utilization_percent = 0.0
        self._neighbor_count = 0
        # Newest-first list of compact packet records from /api/recent_packets.
        self._recent_packets = []

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def Connected(self):
        with self._lock:
            return self._connected

    @property
    def AuthFailed(self):
        """True when the service is reachable but rejects our credentials.

        Distinguishes "wrong/missing API token" (HTTP 401) from a plain
        connection failure so the display can show an actionable message.
        """
        with self._lock:
            return self._auth_failed

    @property
    def NodeName(self):
        """Configured node name (from config.node_name)."""
        with self._lock:
            return self._node_name

    @property
    def LocalHash(self):
        """Short local node hash identifying this repeater on the mesh."""
        with self._lock:
            return self._local_hash

    @property
    def Version(self):
        """Repeater application version string."""
        with self._lock:
            return self._version

    @property
    def CoreVersion(self):
        """openhope_core library version (or 'unknown')."""
        with self._lock:
            return self._core_version

    @property
    def RxCount(self):
        """Total packets received since start."""
        with self._lock:
            return self._rx_count

    @property
    def TxCount(self):
        """Total packets forwarded (retransmitted) since start."""
        with self._lock:
            return self._forwarded_count

    @property
    def DroppedCount(self):
        """Total packets dropped since start."""
        with self._lock:
            return self._dropped_count

    @property
    def CrcErrorCount(self):
        """Radio-level CRC error count (corrupt frames)."""
        with self._lock:
            return self._crc_error_count

    @property
    def RxPerHour(self):
        """Rolling receive rate, packets per hour."""
        with self._lock:
            return self._rx_per_hour

    @property
    def TxPerHour(self):
        """Rolling forward rate, packets per hour."""
        with self._lock:
            return self._forwarded_per_hour

    @property
    def UptimeSeconds(self):
        """Repeater engine uptime in seconds."""
        with self._lock:
            return self._uptime_seconds

    @property
    def NoiseFloorDbm(self):
        """Most recent noise-floor sample in dBm, or None if not yet sampled."""
        with self._lock:
            return self._noise_floor_dbm

    @property
    def Mode(self):
        """Repeater mode: 'forward', 'monitor' or 'no_tx' (empty if unknown)."""
        with self._lock:
            return self._mode

    @property
    def RadioStatus(self):
        """Radio health: 'ok', 'degraded' or 'disabled' (empty if unknown)."""
        with self._lock:
            return self._radio_status

    @property
    def RadioError(self):
        """Human-readable radio error string, empty when the radio is healthy."""
        with self._lock:
            return self._radio_error

    @property
    def AirtimeUtilizationPct(self):
        """Radio airtime utilization over the recent window, 0-100 percent."""
        with self._lock:
            return self._utilization_percent

    @property
    def NeighborCount(self):
        """Number of currently-known mesh neighbors."""
        with self._lock:
            return self._neighbor_count

    @property
    def RecentPackets(self):
        """Newest-first list of recent packet records (dicts).

        Each record has: timestamp, type_name, route_name, src_hash, dst_hash,
        rssi, snr, is_duplicate, drop_reason. Empty when the service is down
        or no packets have been seen yet.
        """
        with self._lock:
            return list(self._recent_packets)

    def stop(self):
        self._stop.set()

    def _reset_state(self, auth_failed=False):
        with self._lock:
            self._connected = False
            self._auth_failed = auth_failed
            self._node_name = ""
            self._local_hash = ""
            self._version = ""
            self._core_version = ""
            self._rx_count = 0
            self._forwarded_count = 0
            self._dropped_count = 0
            self._crc_error_count = 0
            self._rx_per_hour = 0.0
            self._forwarded_per_hour = 0.0
            self._uptime_seconds = 0.0
            self._noise_floor_dbm = None
            self._mode = ""
            self._radio_status = ""
            self._radio_error = ""
            self._utilization_percent = 0.0
            self._neighbor_count = 0
            self._recent_packets = []

    def _fetch_json(self, url, headers):
        """GET a URL and return the parsed JSON body (dict), or None on error.

        urllib.error.HTTPError is re-raised so callers can distinguish an
        auth rejection (401/403) from a plain failure; everything else
        (timeouts, connection refused, bad JSON) just returns None.
        """
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=self._timeout_seconds) as resp:
                body = resp.read()
        except urllib.error.HTTPError:
            raise
        except Exception:
            return None
        data = json.loads(body.decode("utf-8", errors="ignore"))
        return data if isinstance(data, dict) else None

    def _run(self):
        stats_url = f"http://{self._host}:{self._port}/api/stats"
        packets_url = (
            f"http://{self._host}:{self._port}"
            f"/api/recent_packets?limit={REPEATER_PACKET_LIMIT}"
        )
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key

        while not self._stop.is_set():
            ok = False
            auth_failed = False
            try:
                stats = self._fetch_json(stats_url, headers)
                if stats is not None:
                    self._apply_stats(stats)
                    # Second request in the same poll cycle; a failure here
                    # (e.g. transient 500) just leaves the feed stale for one
                    # interval rather than marking the whole service down.
                    packets_doc = self._fetch_json(packets_url, headers)
                    if packets_doc is not None:
                        self._apply_packets(
                            packets_doc.get("data") or []
                        )
                    ok = True
            except urllib.error.HTTPError as e:
                # 401/403 means the service is up but our token is missing or
                # invalid — report that distinctly from a plain outage.
                if e.code in (401, 403):
                    auth_failed = True

            if not ok:
                # Service down / timeout / bad JSON / rejected credentials —
                # clear stale data so the display reports "down" instead of
                # showing old numbers.
                self._reset_state(auth_failed=auth_failed)

            if self._stop.wait(self._poll_interval_seconds):
                break

    def _apply_stats(self, stats):
        config = stats.get("config")
        if not isinstance(config, dict):
            config = {}
        repeater_cfg = config.get("repeater")
        if not isinstance(repeater_cfg, dict):
            repeater_cfg = {}

        neighbors = stats.get("neighbors")
        neighbor_count = len(neighbors) if isinstance(neighbors, (dict, list)) else 0

        noise_floor = stats.get("noise_floor_dbm")
        try:
            noise_floor = float(noise_floor) if noise_floor is not None else None
        except (ValueError, TypeError):
            noise_floor = None

        with self._lock:
            self._connected = True
            self._auth_failed = False
            self._node_name = str(config.get("node_name", "") or "")
            self._local_hash = str(stats.get("local_hash", "") or "")
            self._version = str(stats.get("version", "") or "")
            self._core_version = str(stats.get("core_version", "unknown") or "unknown")
            self._rx_count = int(stats.get("rx_count", 0) or 0)
            self._forwarded_count = int(stats.get("forwarded_count", 0) or 0)
            self._dropped_count = int(stats.get("dropped_count", 0) or 0)
            self._crc_error_count = int(stats.get("crc_error_count", 0) or 0)
            self._rx_per_hour = float(stats.get("rx_per_hour", 0.0) or 0.0)
            self._forwarded_per_hour = float(stats.get("forwarded_per_hour", 0.0) or 0.0)
            self._uptime_seconds = float(stats.get("uptime_seconds", 0.0) or 0.0)
            self._noise_floor_dbm = noise_floor
            self._mode = str(repeater_cfg.get("mode", "") or "")
            self._radio_status = str(stats.get("radio_status", "") or "")
            self._radio_error = str(stats.get("radio_error", "") or "")
            self._utilization_percent = float(
                stats.get("utilization_percent", 0.0) or 0.0
            )
            self._neighbor_count = neighbor_count

    def _apply_packets(self, records):
        """Store compact packet records from /api/recent_packets, newest first."""
        packets = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            rssi = rec.get("rssi")
            try:
                rssi = int(rssi) if rssi is not None else None
            except (ValueError, TypeError):
                rssi = None
            packets.append({
                "timestamp": float(rec.get("timestamp", 0.0) or 0.0),
                "type_name": payload_type_name(rec.get("type")),
                "route_name": route_type_name(rec.get("route")),
                "src_hash": str(rec.get("src_hash", "") or ""),
                "dst_hash": str(rec.get("dst_hash", "") or ""),
                "rssi": rssi,
                "is_duplicate": bool(rec.get("is_duplicate", 0)),
                "drop_reason": rec.get("drop_reason") or "",
            })
        packets.sort(key=lambda p: p["timestamp"], reverse=True)

        with self._lock:
            self._recent_packets = packets
