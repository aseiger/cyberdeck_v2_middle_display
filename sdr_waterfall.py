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
rFFT + max-pool to display width + adaptive dB scaling) and scrolls it into a
shared uint8 frame buffer.  The render loop just calls snapshot() when the
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
# 87.9 MHz: a local FM station sits ~105 kHz below center here, so the view
# shows a real signal out of the box.  Override per-deck with SDR_CENTER_HZ.
SDR_CENTER_HZ = float(os.environ.get("SDR_CENTER_HZ", "87900000"))
SDR_GAIN_DB   = float(os.environ.get("SDR_GAIN_DB", "25.0"))        # manual tuner gain
SDR_PPM       = int(float(os.environ.get("SDR_PPM", "0")))          # dongle calibration

# Per-column normalization percentiles: map this slice of each column's power
# distribution to black→white, with anything hotter clipping white.  Peak-
# referenced scaling washes out in wideband-dense bands (indoor RF), so use a
# percentile window that adapts to whatever the band actually contains.
NORM_LO_PCT   = 5.0
NORM_HI_PCT   = 99.0

SAMPLE_RATE     = 250_000    # samples/sec → ±125 kHz span around center frequency
CHUNK_SIZE      = 8192       # complex samples per waterfall column (~30 cols/s)
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

    # Manual gain for a stable display; fall back to AGC if the tuner refuses.
    _LIB.rtlsdr_set_tuner_gain_mode(dev, 1)
    if _LIB.rtlsdr_set_tuner_gain(dev, int(round(SDR_GAIN_DB * 10))) != 0:
        logging.warning("[SDR] manual gain %.0f dB rejected; using AGC", SDR_GAIN_DB)
        _LIB.rtlsdr_set_agc_mode(dev, 1)
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
        """Latest waterfall frame as a uint8 numpy array (height, width),
        oldest columns on the left.  Safe from any thread."""
        with self._frame_lock:
            return self._frame.copy()

    # -- internals ---------------------------------------------------------

    def _pool_column(self, spec_db):
        """Max-pool a spectrum down to one value per pixel ROW (one column of
        the waterfall is display-height tall)."""
        bins = spec_db[1:]                            # drop DC bin
        m = self.height * (len(bins) // self.height)  # largest height-aligned prefix
        pooled = bins[:m].reshape(self.height, -1).max(axis=1)

        lo = float(np.percentile(pooled, NORM_LO_PCT))
        hi = float(np.percentile(pooled, NORM_HI_PCT))
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
                samples = np.zeros(CHUNK_SIZE, dtype=np.complex64)
                pairs = np.frombuffer(buf, dtype=np.int16).reshape(-1, 2)[:n // 2]
                re = pairs[:, 0].astype(np.float32) / 32768.0
                im = pairs[:, 1].astype(np.float32) / 32768.0
                samples[:len(pairs)] = re + 1j * im
                # I/Q is already complex baseband: use a full FFT and keep the
                # positive-frequency half (DC..Nyquist).  rfft would cast away
                # the imaginary part.
                power = np.abs(np.fft.fft(samples * _HANN))[:CHUNK_SIZE // 2 + 1] ** 2
                spec_db = 10.0 * np.log10(power + 1e-12)

                col = self._pool_column(spec_db)
                with self._frame_lock:
                    if self.active and self._thread is threading.current_thread():
                        # In-place shift (np.roll returns a copy — don't use it
                        # here).  Overlapping-slice assignment uses memmove.
                        self._frame[:, :-1] = self._frame[:, 1:]
                        self._frame[:, -1] = col
                self.column_count += 1

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


class SdrPanorama:
    """Full-band log-frequency band map (SDR#'s "band scan" style).

    One synchronous frame can only hold ~2 MHz of spectrum (the dongle's max
    stable sample rate), so the whole 24 MHz–1760 MHz tuner range is assembled
    by sweeping: one wide snapshot per step, each pixel's power max-blended
    into a row.  When the sweep completes that row lands at the BOTTOM of the
    frame and everything scrolls up — persistent transmitters appear as bright
    vertical lines at their true frequencies.  One full pass takes ~20–60 s
    depending on the sample rate the device accepts.

    Same strict lifecycle as SdrWaterfall: owns the dongle only while active,
    closes it on exit / error / shutdown so other programs can use it."""

    FREQ_MIN_HZ = 24e6          # R820T usable range (approx.)
    FREQ_MAX_HZ = 1760e6
    RATE_REQUESTED = 2_000_000  # max stable; the device may clamp — we query back

    REF_DECAY_DB = 70.0         # display span below the slow-peak reference

    def __init__(self, width, height):
        self.width = int(width)
        self.height = int(height)
        self.freq_min_hz = self.FREQ_MIN_HZ
        self.freq_max_hz = self.FREQ_MAX_HZ
        self.gain_db = SDR_GAIN_DB

        # Log-frequency pixel grid: x-pixel i covers [_px_f[i], _px_f[i+1]) Hz.
        self._px_f = np.logspace(np.log10(self.FREQ_MIN_HZ),
                                 np.log10(self.FREQ_MAX_HZ), width + 1)

        self._state_lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._frame = np.zeros((self.height, self.width), dtype=np.uint8)
        self._dev = None            # rtlsdr device handle (None = closed)
        self._thread = None
        self._stop_event = threading.Event()
        self.active = False          # device open + sweeper running
        self.error = ""              # last failure, for the UI overlay
        self.row_count = 0           # completed sweeps committed to the frame
        self.sweep_seconds = None    # duration of the last full pass
        self._rate = 0               # effective sample rate (Hz)
        self._ref_db = None          # slow EMA of the brightest dB seen

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        """Open the dongle and start the sweep thread.  Idempotent."""
        with self._state_lock:
            if self.active and self._thread is not None and self._thread.is_alive():
                return True
            if _LIB is None:
                self.error = "librtlsdr missing"
                return False

        dev, rate = _open_dongle(self.FREQ_MIN_HZ, self.RATE_REQUESTED)
        if dev is None:
            self.error = "dongle busy or not found"
            return False

        buf = (ctypes.c_int16 * (CHUNK_SIZE * 2))()   # complex int16 pairs
        t = threading.Thread(target=self._sweeper, args=(buf,), name="sdr-panorama", daemon=True)

        with self._state_lock:      # publish before starting the thread
            self._dev = dev
            self._rate = rate
            self._thread = t
            self.active = True
            self.error = ""
        t.start()

        logging.info("[SDR-BAND] opened dongle, sweeping %.0f–%.2f GHz @ %d S/s",
                     self.FREQ_MIN_HZ / 1e6, self.FREQ_MAX_HZ / 1e9, rate)
        return True

    def stop(self):
        """Stop the sweeper and close the device.  Idempotent."""
        self._stop_event.set()
        with self._state_lock:
            t = self._thread
            dev = self._dev          # main thread takes ownership of close
            self._dev = None

        if t is not None and t.is_alive():
            t.join(timeout=2.0)      # in-flight step finishes quickly

        _close_dongle(dev)

        with self._state_lock:
            self.active = False
            self._thread = None

    # -- render side -------------------------------------------------------

    def snapshot(self):
        """Current band map as a uint8 numpy array (height, width), newest
        sweep at the bottom row.  Safe from any thread."""
        with self._frame_lock:
            return self._frame.copy()

    # -- internals ---------------------------------------------------------

    def _commit_row(self, row_acc, seconds):
        """Normalize one finished sweep and scroll it in at the bottom."""
        valid = row_acc > -1e29
        if not valid.any():
            return   # empty pass (all reads failed) — nothing to show

        rowmax = float(row_acc[valid].max())
        # Slow peak reference: what is white tracks the strongest signal seen
        # over recent passes, so the map stays readable whether the band is
        # full of strong TV carriers or nearly quiet.
        self._ref_db = (rowmax if self._ref_db is None
                        else self._ref_db + (rowmax - self._ref_db) * 0.2)
        floor = self._ref_db - self.REF_DECAY_DB
        vals = np.clip((row_acc - floor) / max(self._ref_db - floor, 1e-6) * 255.0,
                       0, 255).astype(np.uint8)

        with self._frame_lock:
            if self.active and self._thread is threading.current_thread():
                # In-place upward scroll (overlapping-slice assignment uses
                # memmove); newest sweep on the bottom row.
                self._frame[:-1] = self._frame[1:]
                self._frame[-1, :] = vals
                self.row_count += 1
        self.sweep_seconds = seconds

    def _sweeper(self, buf):
        with self._state_lock:
            dev = self._dev          # published by start() before we ran
            rate = self._rate
        if dev is None or not rate:  # stop() won the race — exit quietly
            return

        nread = ctypes.c_int(0)
        fails = 0
        N = CHUNK_SIZE
        d = float(rate) / N                          # Hz per FFT bin
        step = max(int(rate * 0.85), 100_000)        # ~15% snapshot overlap
        ppm_scale = (1.0 + SDR_PPM / 1e6)
        fmin = int(self.FREQ_MIN_HZ * ppm_scale)
        fmax = int(self.FREQ_MAX_HZ * ppm_scale)

        def new_row():
            return np.full(self.width, -1e30, dtype=np.float32)

        f = fmin
        row_acc = new_row()
        t_sweep = time.monotonic()

        try:
            while not self._stop_event.is_set():
                if f >= fmax:                       # full pass complete
                    self._commit_row(row_acc, time.monotonic() - t_sweep)
                    row_acc = new_row()
                    f = fmin
                    t_sweep = time.monotonic()

                _LIB.rtlsdr_set_center_freq(dev, int(f))
                rc = _LIB.rtlsdr_read_sync(dev, buf, N * 2, ctypes.byref(nread))
                n = nread.value
                if rc != 0 or n <= 0:               # transient USB hiccup?
                    fails += 1
                    if fails >= MAX_READ_FAILURES:
                        logging.warning("[SDR-BAND] %d consecutive read errors (rc=%d) — releasing dongle",
                                        fails, rc)
                        break
                    time.sleep(0.05)
                    continue
                fails = 0

                samples = np.zeros(N, dtype=np.complex64)
                pairs = np.frombuffer(buf, dtype=np.int16).reshape(-1, 2)[:n // 2]
                re = pairs[:, 0].astype(np.float32) / 32768.0
                im = pairs[:, 1].astype(np.float32) / 32768.0
                samples[:len(pairs)] = re + 1j * im
                db = 10.0 * np.log10(
                    np.abs(np.fft.fft(samples * _HANN))[:N // 2 + 1] ** 2 + 1e-12)

                # Blend this snapshot's power into the row: bin k sits at
                # f - rate/2 + k*d; x-pixel i spans [_px_f[i], _px_f[i+1]).
                a, b = f - float(rate) / 2.0, f + float(rate) / 2.0
                lo_px = max(0, int(np.searchsorted(self._px_f, a, side="right")) - 1)
                hi_px = min(self.width - 1, int(np.searchsorted(self._px_f, b, side="left")) - 1)
                for i in range(lo_px, hi_px + 1):
                    fl = max(a, float(self._px_f[i]))
                    fh = min(b, float(self._px_f[i + 1]))
                    k_lo = int(np.ceil((fl - a) / d))
                    k_hi = int(np.floor((fh - a) / d))
                    if 0 <= k_lo <= k_hi < len(db):
                        m = float(db[k_lo:k_hi + 1].max())
                        if m > row_acc[i]:
                            row_acc[i] = m

                f += step

            # Unexpected death: release the device (same race-safe pattern as
            # SdrWaterfall — whoever grabs _dev first closes it).
            if not self._stop_event.is_set():
                with self._state_lock:
                    dev = self._dev
                    self._dev = None
                    self.active = False
                    self._thread = None
                self.error = "dongle read error"
                _close_dongle(dev)
                logging.warning("[SDR-BAND] released dongle after read error")
        except Exception as e:   # never let the daemon die over this view
            logging.exception("[SDR-BAND] sweeper crashed: %s", e)
            self.error = "sweeper crash"
            with self._state_lock:
                dev = self._dev
                self._dev = None
                self.active = False
                self._thread = None
            _close_dongle(dev)
