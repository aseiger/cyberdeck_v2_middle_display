#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
sdr_waterfall.py - RTL-SDR spectrum waterfall for the SDR screen view.

Device lifecycle is strict by design (requirement from the deck owner): while
the SDR view is active this module owns the dongle exclusively; the moment
the view goes inactive, stop() closes the device so any other program
(gqrx, rtl_fm, SDR#, ...) can use it immediately.

Architecture: a background reader thread pulls complex samples from
librtlsdr (ctypes), turns each chunk into one spectrum column (Hann window +
full FFT, negative/positive frequency halves ordered around DC, max-pool to
display height, level-normalized against a reference that is frozen after a
short calibration window) and scrolls it into a shared uint8 frame
buffer.  The render loop just calls snapshot() when the
SDR view is active — no DDC/USB work on the main thread, ever.

If the dongle cannot be opened (another program holds it, or it was yanked),
start() records the reason in .error and returns False; the caller may retry
on a later view entry.  A reader that dies mid-stream closes the device too,
so the hardware is never left pinned by this module.

Tunables are environment-overridable: SDR_CENTER_HZ, SDR_GAIN_DB, SDR_PPM.
"""

import ctypes
import logging
import os
import threading
import time

import numpy as np

# --- Tunables -------------------------------------------------------------
# Center frequency for the waterfall view (±125 kHz at 250 kS/s).
# Override per-deck with SDR_CENTER_HZ.
SDR_CENTER_HZ = float(os.environ.get("SDR_CENTER_HZ", "910525000"))
SDR_GAIN_DB   = float(os.environ.get("SDR_GAIN_DB", "25.0"))       # manual tuner gain; AGC is never used
SDR_PPM       = int(float(os.environ.get("SDR_PPM", "0")))          # dongle calibration

# Per-column normalization percentiles: map this slice of each column's power
# distribution to black→white, with anything hotter clipping white.  Peak-
# referenced scaling washes out in wideband-dense bands (indoor RF), so use a
# percentile window that adapts to whatever the band actually contains.
NORM_LO_PCT   = 5.0
NORM_HI_PCT   = 99.0
# Columns used to CALIBRATE the display level reference after a device open.
# During that window live percentiles are tracked (so it looks right in any RF
# environment); afterwards the reference is FROZEN, so brightness changes on
# screen reflect real RF level changes instead of per-column auto-gain chasing.
CALIB_COLUMNS = 75        # ~3 s at the current scroll rate
# The RTL2832U's ADC has a static I/Q bias; in the FFT that concentrates into
# bin 0 (exactly center frequency) as an ever-present vertical line.  Subtracting
# each chunk's mean removes it without touching real signals, whose energy is
# spread over kHz of modulation around the carrier.
DC_REMOVAL    = True
# The R820T's local oscillator also leaks into its own mixer input; that energy
# lands at baseband zero for ANY tuning (verified by capturing two center
# frequencies — a real signal would shift with tuning, this did not), so no
# filtering can separate it from a true carrier.  A narrow notch around DC
# removes the line; modulated signals spread over kHz and stay visible except
# their pure-carrier core.
DC_NOTCH_HZ   = 150.0      # half-width of the DC notch in Hz (0 disables)

# 2 MSPS (confirmed stable on this dongle) → ±1 MHz span around center;
# each pixel row resolves ~8 kHz.
SAMPLE_RATE     = 2_000_000
CHUNK_SIZE      = 65536      # complex samples per column → ~30 cols/s at this rate
MAX_READ_FAILURES = 10       # consecutive read errors before we release the dongle


def _load_lib():
    """Load librtlsdr and declare the prototypes we use (None if absent).

    Signatures follow the canonical librtlsdr 0.6 header; read_sync's restype
    is c_int so an error (-1) can never be misread as a huge uint32 length."""
    for name in ("librtlsdr.so", "librtlsdr.so.0"):
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            continue

        c_uint32 = ctypes.c_uint32
        dev_p = ctypes.POINTER(ctypes.c_void_p)   # rtlsdr_device*

        lib.rtlsdr_open.restype = ctypes.c_int
        lib.rtlsdr_open.argtypes = [dev_p, c_uint32]
        lib.rtlsdr_close.restype = None            # void in 0.6git
        lib.rtlsdr_close.argtypes = [ctypes.c_void_p]
        lib.rtlsdr_set_center_freq.restype = ctypes.c_int
        lib.rtlsdr_set_center_freq.argtypes = [ctypes.c_void_p, c_uint32]
        lib.rtlsdr_set_sample_rate.restype = ctypes.c_int
        lib.rtlsdr_set_sample_rate.argtypes = [ctypes.c_void_p, c_uint32]
        lib.rtlsdr_set_tuner_gain_mode.restype = None   # void; best-effort
        lib.rtlsdr_set_tuner_gain_mode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.rtlsdr_set_agc_mode.restype = None          # void; best-effort
        lib.rtlsdr_set_agc_mode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.rtlsdr_set_tuner_gain.restype = ctypes.c_int  # gain in 0.1 dB steps
        lib.rtlsdr_set_tuner_gain.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.rtlsdr_get_sample_rate.restype = ctypes.c_int
        lib.rtlsdr_get_sample_rate.argtypes = [ctypes.c_void_p,
                                               ctypes.POINTER(c_uint32)]
        lib.rtlsdr_reset_buffer.restype = ctypes.c_int
        lib.rtlsdr_reset_buffer.argtypes = [ctypes.c_void_p]
        # This build exports the newer 4-argument read_sync:
        #   int rtlsdr_read_sync(dev, void *buf, uint32_t len_bytes, int *nread)
        # (verified by disassembly: x4 is forwarded to libusb_bulk_transfer's
        # actual_length).  len is BYTES, the return is a libusb status code,
        # and nread receives the bytes actually transferred.
        lib.rtlsdr_read_sync.restype = ctypes.c_int
        lib.rtlsdr_read_sync.argtypes = [ctypes.c_void_p,
                                         ctypes.POINTER(ctypes.c_int16), c_uint32,
                                         ctypes.POINTER(ctypes.c_int)]
        return lib

    logging.warning("[SDR] librtlsdr not found — SDR view disabled")
    return None


_LIB = _load_lib()
_HANN = np.hanning(CHUNK_SIZE).astype(np.float32)

# Complex baseband bin order around the carrier: for a complex I/Q FFT, bin 0
# IS the center frequency (DC), bins 1..N/2 are +offsets and bins N-Nyquist..
# N-1 are -offsets.  Ordered low→high this is [negative half][DC..+Nyquist],
# so row 0 = band bottom, middle row = center freq, last row = band top.
_N = CHUNK_SIZE
_BIN_ORDER = np.concatenate(
    [np.arange(_N - _N // 2, _N),        # -Nyquist ... just below DC
     np.arange(0, _N // 2 + 1)])         # DC ... +Nyquist


def _build_palette(n=256):
    """Classic SDR intensity ramp: black → blue → cyan → green → yellow → white.
    Bold, well-separated steps stay legible after the LCD's RGB→RGB565
    quantization (red/blue are only 3 bits there)."""
    anchors = [
        (0.00, (12,   4,  36)),
        (0.22, (16,  60, 190)),
        (0.42, ( 0, 175, 220)),
        (0.62, ( 0, 215,  90)),
        (0.80, (235, 225,   0)),
        (1.00, (255, 255, 255)),
    ]
    lut = np.zeros((n, 3), dtype=np.uint8)
    for i in range(n):
        p = i / (n - 1)
        j = next(k for k in range(len(anchors) - 1) if anchors[k][0] <= p <= anchors[k + 1][0])
        p0, c0 = anchors[j]
        p1, c1 = anchors[j + 1]
        t = (p - p0) / (p1 - p0)
        for ch in range(3):
            lut[i, ch] = int(round(c0[ch] + t * (c1[ch] - c0[ch])))
    return lut


_PALETTE = _build_palette()   # 256x3 uint8 lookup: grayscale level → RGB


def _open_dongle(freq_hz, sample_rate):
    """Open device 0 and configure it.  Returns (dev|None, effective_rate).

    The effective rate is queried back from the device: the dongle may clamp
    a requested sample rate, and sweep math must use what actually stuck."""
    if _LIB is None:
        return None, 0

    dev = ctypes.c_void_p()
    rc = _LIB.rtlsdr_open(ctypes.byref(dev), 0)
    if rc != 0 or not dev:
        logging.warning("[SDR] rtlsdr_open failed (rc=%d)", rc)
        return None, 0

    freq_hz = int(freq_hz * (1.0 + SDR_PPM / 1e6))
    _LIB.rtlsdr_set_center_freq(dev, freq_hz)
    if _LIB.rtlsdr_set_sample_rate(dev, sample_rate) != 0:
        logging.warning("[SDR] sample rate %d rejected; using device default", sample_rate)

    eff = ctypes.c_uint32(0)
    if _LIB.rtlsdr_get_sample_rate(dev, ctypes.byref(eff)) != 0 or not eff.value:
        eff.value = sample_rate   # query failed — assume what we asked for

    # Manual gain only — AGC is deliberately disabled (gain mode 0 would
    # enable it).  Automatic gain makes the waterfall brightness chase around
    # over time; a fixed manual level keeps the display stable and comparable.
    _LIB.rtlsdr_set_tuner_gain_mode(dev, 1)   # 1 = manual, AGC off
    if _LIB.rtlsdr_set_tuner_gain(dev, int(round(SDR_GAIN_DB * 10))) != 0:
        # Stay in manual mode at the tuner's own default rather than enabling AGC.
        logging.warning("[SDR] manual gain %.0f dB rejected; keeping tuner default", SDR_GAIN_DB)
    _LIB.rtlsdr_reset_buffer(dev)

    return dev, int(eff.value)


def _close_dongle(dev):
    if _LIB is not None and dev is not None:
        _LIB.rtlsdr_close(dev)


class SdrWaterfall:
    """Scrolling spectrum waterfall bound to one RTL-SDR device.

    Thread-safety: two small locks — _state_lock guards the device handle /
    active flag (so start/stop and a dying reader can't double-close the
    dongle), _frame_lock guards the pixel buffer.  Both critical sections are
    ~microseconds to one small memcpy; they never hold while USB I/O happens.
    """

    def __init__(self, width, height):
        self.width = int(width)
        self.height = int(height)
        self.center_hz = SDR_CENTER_HZ
        self.sample_rate = SAMPLE_RATE
        self.gain_db = SDR_GAIN_DB

        self._state_lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._frame = np.zeros((self.height, self.width), dtype=np.uint8)
        self._dev = None            # rtlsdev* handle (None = device closed)
        self._thread = None
        self._stop_event = threading.Event()
        self.active = False          # device open + reader running
        self.error = ""              # last start/read failure, for the UI
        self.column_count = 0        # columns produced (debug/tests)
        self._cols_since_open = 0    # columns this session (calibration window)
        self._ref_lo = None          # frozen normalization reference
        self._ref_hi = None          # (None pair = still calibrating)

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        """Open the dongle and start the reader thread.  Idempotent.

        Returns True if the waterfall is (now or already) running, else False
        with .error explaining why (e.g. device busy)."""
        with self._state_lock:
            if self.active and self._thread is not None and self._thread.is_alive():
                return True
            if _LIB is None:
                self.error = "librtlsdr missing"
                return False

        dev, rate = _open_dongle(self.center_hz, SAMPLE_RATE)
        if dev is None:
            self.error = "dongle busy or not found"
            return False

        self._stop_event.clear()
        buf = (ctypes.c_int16 * (CHUNK_SIZE * 2))()   # complex int16 pairs
        t = threading.Thread(target=self._reader, args=(buf,), name="sdr-waterfall", daemon=True)

        # Publish state BEFORE starting the thread so the reader can never
        # observe a half-opened device.
        with self._state_lock:
            self._dev = dev
            self._thread = t
            self.active = True
            self.error = ""
            self._cols_since_open = 0   # each device session recalibrates
            self._ref_lo = None
            self._ref_hi = None
        t.start()

        logging.info("[SDR] opened dongle @ %.1f MHz, %d S/s, gain %.0f dB",
                     int(self.center_hz) / 1e6, rate, self.gain_db)
        return True

    def stop(self):
        """Stop the reader and close the device.  Idempotent; safe to call
        when not active.  After this returns the dongle is free for other
        programs."""
        self._stop_event.set()
        with self._state_lock:
            t = self._thread
            dev = self._dev          # main thread takes ownership of close
            self._dev = None

        if t is not None and t.is_alive():
            t.join(timeout=2.0)      # in-flight read finishes within ~one chunk

        if dev is not None and _LIB is not None:
            _LIB.rtlsdr_close(dev)

        with self._state_lock:
            self.active = False
            self._thread = None

        logging.info("[SDR] closed dongle — device free for other programs")

    # -- render side -------------------------------------------------------

    def snapshot(self):
        """Latest waterfall frame as a grayscale uint8 numpy array
        (height, width), oldest columns on the left.  Safe from any thread."""
        with self._frame_lock:
            return self._frame.copy()

    def snapshot_rgb(self):
        """Same frame mapped through the SDR color palette: uint8 (h, w, 3)."""
        with self._frame_lock:
            return _PALETTE[self._frame.copy()]

    # -- internals ---------------------------------------------------------

    def _pool_column(self, spec_db):
        """Max-pool a spectrum down to one value per pixel ROW (one column of
        the waterfall is display-height tall).  spec_db arrives ordered low→
        high frequency around the carrier (see _BIN_ORDER)."""
        bins = spec_db                                # DC/center included now
        m = self.height * (len(bins) // self.height)  # largest height-aligned prefix
        pooled = bins[:m].reshape(self.height, -1).max(axis=1)
        return self._scale(pooled)

    def _scale(self, pooled):
        """Map pooled dB values to 0..255 against the level reference.

        For the first CALIB_COLUMNS after open we track live percentiles so
        the display looks right in any environment; at the end of that window
        the reference is frozen.  (Per-column rescaling forever made brightness
        chase around like AGC — a fixed reference makes brightness changes
        mean real level changes.)"""
        if self._ref_lo is None and self._cols_since_open >= CALIB_COLUMNS - 1:
            lo = float(np.percentile(pooled, NORM_LO_PCT))
            hi = float(np.percentile(pooled, NORM_HI_PCT))
            if hi - lo > 0.5:     # freeze only on a non-degenerate window
                self._ref_lo, self._ref_hi = lo, hi
        if self._ref_lo is None:  # still calibrating → live percentiles
            lo = float(np.percentile(pooled, NORM_LO_PCT))
            hi = float(np.percentile(pooled, NORM_HI_PCT))
        else:
            lo, hi = self._ref_lo, self._ref_hi
        span = max(hi - lo, 1e-6)     # flat column (no data yet) → black
        col = np.clip((pooled - lo) / span * 255.0, 0, 255)
        return col.astype(np.uint8)

    def _reader(self, buf):
        with self._state_lock:
            dev = self._dev          # published by start() before we ran
        if dev is None:              # stop() won the race — exit quietly
            return
        nread = ctypes.c_int(0)
        fails = 0
        try:
            while not self._stop_event.is_set():
                rc = _LIB.rtlsdr_read_sync(dev, buf, CHUNK_SIZE * 2,
                                           ctypes.byref(nread))
                n = nread.value
                if rc != 0 or n <= 0:
                    # Transient USB hiccups happen; give the bus a moment and
                    # retry before giving up on the device.
                    fails += 1
                    if fails >= MAX_READ_FAILURES:
                        logging.warning("[SDR] %d consecutive read errors (rc=%d) — releasing dongle",
                                        fails, rc)
                        break
                    time.sleep(0.05)
                    continue
                fails = 0

                # Zero-pad short reads so every column is full width.
                pairs = np.frombuffer(buf, dtype=np.int16).reshape(-1, 2)[:n // 2]
                re = pairs[:, 0].astype(np.float32) / 32768.0
                im = pairs[:, 1].astype(np.float32) / 32768.0
                if DC_REMOVAL:
                    # Kill the ADC's static I/Q bias — over the VALID samples
                    # only; averaging in the zero padding would bias the mean
                    # on short reads and re-create a fake center spike.
                    re -= re.mean()
                    im -= im.mean()
                samples = np.zeros(CHUNK_SIZE, dtype=np.complex64)
                samples[:len(pairs)] = re + 1j * im
                # Full complex FFT: bin 0 is the carrier (DC).  Reorder into
                # [negative half][DC..+Nyquist] so the column spans center-
                # rate/2 .. center+rate/2 with the middle row at center.
                power = np.abs(np.fft.fft(samples * _HANN)) ** 2
                if DC_NOTCH_HZ > 0:
                    # Mask ±DC_NOTCH_HZ around baseband zero (bin 0 and its
                    # wrap-around neighbours hold the negative offsets).
                    k = int(round(DC_NOTCH_HZ * CHUNK_SIZE / SAMPLE_RATE))
                    power[:k + 1] = 0.0
                    power[-k:] = 0.0
                spec_db_full = 10.0 * np.log10(power + 1e-12)
                spec_db = spec_db_full[_BIN_ORDER]

                col = self._pool_column(spec_db)
                with self._frame_lock:
                    if self.active and self._thread is threading.current_thread():
                        # In-place shift (np.roll returns a copy — don't use it
                        # here).  Overlapping-slice assignment uses memmove.
                        self._frame[:, :-1] = self._frame[:, 1:]
                        self._frame[:, -1] = col
                self.column_count += 1
                self._cols_since_open += 1

            # Unexpected death (read error / USB drop): release the device so
            # no other program is blocked by this module.  stop() from the
            # main thread races us safely — whoever grabs _dev first closes it.
            if not self._stop_event.is_set():
                with self._state_lock:
                    dev = self._dev
                    self._dev = None
                    self.active = False
                    self._thread = None
                self.error = "dongle read error"
                if _LIB is not None and dev is not None:
                    _LIB.rtlsdr_close(dev)
                    logging.warning("[SDR] released dongle after read error")
        except Exception as e:   # never let the daemon die over the SDR view
            logging.exception("[SDR] reader crashed: %s", e)
            self.error = "reader crash"
            with self._state_lock:
                dev = self._dev
                self._dev = None
                self.active = False
                self._thread = None
            if _LIB is not None and dev is not None:
                _LIB.rtlsdr_close(dev)
