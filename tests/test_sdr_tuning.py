#!/usr/bin/python3
# -*- coding: UTF-8 -*-
"""
test_sdr_tuning.py - offline test for the SDR tuning API (SdrWaterfall.tune /
adjust_gain), i.e. what the applet's tuning buttons drive through the IPC
server's "sdr" events.

No RTL-SDR hardware is needed: the inactive path only touches state, and the
live path is verified against a stubbed librtlsdr (the ctypes module) that
records every call.  Run with system python3 — numpy is all it imports.

Usage:  python3 tests/test_sdr_tuning.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sdr_waterfall as sw


class FakeLib:
    """Records the librtlsdr calls that tuning makes on an open dongle."""

    def __init__(self):
        self.center_freqs = []     # Hz values passed to rtlsdr_set_center_freq
        self.gains = []            # 0.1 dB units passed to rtlsdr_set_tuner_gain
        self.sample_rates = []     # S/s passed to rtlsdr_set_sample_rate
        self.gain_modes = []       # modes passed to rtlsdr_set_tuner_gain_mode
        self.closed = False
        # What the device reports back for get_sample_rate (simulated clamp).
        self.effective_rate = 2_000_000

    def rtlsdr_set_center_freq(self, dev, freq):
        self.center_freqs.append(freq)
        return 0

    def rtlsdr_set_tuner_gain(self, dev, gain_x10):
        self.gains.append(gain_x10)
        return 0

    def rtlsdr_set_sample_rate(self, dev, rate):
        self.sample_rates.append(rate)
        return 0

    def rtlsdr_get_sample_rate(self, dev, eff):
        # Production passes a one-element c_uint32 array (auto-converts to a
        # pointer with the real lib); it arrives here as the plain object.
        eff[0] = self.effective_rate
        return 0

    def rtlsdr_set_tuner_gain_mode(self, dev, mode):
        # void in the real lib; recorded here for assertions
        self.gain_modes.append(mode)

    def rtlsdr_close(self, dev):
        self.closed = True

    def rtlsdr_read_sync(self, dev, buf, n, nread):
        # Persistent failure: a reader thread started against this stub gives
        # up after MAX_READ_FAILURES (~0.5 s) and releases the device cleanly.
        return -1


def make_wf(active=False, lib=None):
    """A waterfall with (optionally) a fake-opened dongle behind `lib`.

    The caller owns swapping/restore of sw._LIB — this just wires state."""
    wf = sw.SdrWaterfall(width=240, height=252)
    if active:
        assert lib is not None
        wf._dev = object()      # opaque handle; the stub accepts anything
        wf.active = True
    return wf


def test_inactive_tune_updates_state_only():
    wf = make_wf(active=False)
    start = sw.SDR_CENTER_HZ

    assert wf.tune(25_000) == start + 25_000
    assert wf.tune(-100_000) == start - 75_000
    print("PASS inactive tune updates state")


def test_tune_clamps_to_limits():
    wf = make_wf(active=False)

    # Far below the minimum -> clamped, and further negative steps stick.
    assert wf.tune(-1e12) == sw.TUNE_MIN_HZ
    assert wf.tune(-50_000) == sw.TUNE_MIN_HZ
    assert wf.center_hz == sw.TUNE_MIN_HZ

    # Far above the maximum -> clamped, and further positive steps stick.
    wf.tune(1e12)
    assert wf.center_hz == sw.TUNE_MAX_HZ
    assert wf.tune(+50_000) == sw.TUNE_MAX_HZ
    print("PASS tune clamps to [TUNE_MIN_HZ, TUNE_MAX_HZ]")


def test_gain_clamps_to_limits():
    wf = make_wf(active=False)

    assert wf.adjust_gain(-100.0) == sw.GAIN_MIN_DB
    assert wf.gain_db == 0.0
    assert wf.adjust_gain(-5.0) == 0.0          # stays at the floor

    wf.gain_db = 48.5
    assert wf.adjust_gain(+10.0) == sw.GAIN_MAX_DB
    assert wf.adjust_gain(+5.0) == sw.GAIN_MAX_DB   # stays at the ceiling
    print("PASS gain clamps to [GAIN_MIN_DB, GAIN_MAX_DB]")


def test_live_tune_applies_with_ppm_correction():
    lib = FakeLib()
    old_lib, old_ppm = sw._LIB, sw.SDR_PPM
    sw._LIB = lib
    sw.SDR_PPM = -10                          # typical dongle calibration offset
    try:
        wf = make_wf(active=True, lib=lib)
        center = sw.SDR_CENTER_HZ

        new_center = wf.tune(+25_000)
        expected_hz = int((center + 25_000) * (1.0 - 10 / 1e6))
        assert new_center == center + 25_000, new_center
        assert lib.center_freqs == [expected_hz], \
            f"live retune not applied correctly: {lib.center_freqs} != {[expected_hz]}"

        wf.tune(-100_000)
        expected2 = int((center - 75_000) * (1.0 - 10 / 1e6))
        assert lib.center_freqs[-1] == expected2, \
            f"{lib.center_freqs} vs {expected2}"
    finally:
        sw._LIB, sw.SDR_PPM = old_lib, old_ppm
    print("PASS live tune re-points the dongle (PPM-corrected)")


def test_live_gain_applies_in_tenths():
    lib = FakeLib()
    old_lib = sw._LIB
    sw._LIB = lib
    try:
        wf = make_wf(active=True, lib=lib)

        g1 = wf.adjust_gain(+2.5)             # default 25.0 -> 27.5 dB
        assert abs(g1 - (sw.SDR_GAIN_DB + 2.5)) < 1e-9, g1
        expected_x10 = int(round((sw.SDR_GAIN_DB + 2.5) * 10))
        assert lib.gains == [expected_x10], \
            f"expected [{expected_x10}] (0.1 dB units), got {lib.gains}"

        wf.adjust_gain(-1.0)
        expected_x10 = int(round((sw.SDR_GAIN_DB + 1.5) * 10))
        assert lib.gains[-1] == expected_x10, lib.gains
    finally:
        sw._LIB = old_lib
    print("PASS live gain applied in 0.1 dB steps")


def test_set_center_freq_inactive_and_live():
    wf = make_wf(active=False)
    assert wf.set_center_freq(433_920_000.0) == 433_920_000.0
    # clamped like tune()
    assert wf.set_center_freq(-1e12) == sw.TUNE_MIN_HZ
    assert wf.center_hz == sw.TUNE_MIN_HZ

    lib = FakeLib()
    old_lib, old_ppm = sw._LIB, sw.SDR_PPM
    sw._LIB = lib
    sw.SDR_PPM = 0
    try:
        wf2 = make_wf(active=True, lib=lib)
        wf2.set_center_freq(915_000_000.0)
        assert lib.center_freqs == [915_000_000], lib.center_freqs
    finally:
        sw._LIB, sw.SDR_PPM = old_lib, old_ppm
    print("PASS set_center_freq (absolute) clamps and applies live")


def test_set_sample_rate_clamps_and_reads_back():
    # Inactive: clamped to the software window before it is remembered.
    wf = make_wf(active=False)
    assert wf.set_sample_rate(10_000) == sw.BANDWIDTH_MIN_RATE
    assert wf.sample_rate == sw.BANDWIDTH_MIN_RATE
    assert wf.set_sample_rate(9e6) == sw.BANDWIDTH_MAX_RATE
    assert wf.sample_rate == sw.BANDWIDTH_MAX_RATE

    # Live: the device's clamped read-back wins over what we asked for.
    lib = FakeLib()
    lib.effective_rate = 2_500_000          # dongle refuses the full 3.2 MS/s
    old_lib = sw._LIB
    sw._LIB = lib
    try:
        wf2 = make_wf(active=True, lib=lib)
        got = wf2.set_sample_rate(sw.BANDWIDTH_MAX_RATE)
        assert lib.sample_rates == [sw.BANDWIDTH_MAX_RATE], lib.sample_rates
        assert got == 2_500_000.0, got
        assert wf2.sample_rate == 2_500_000     # effective value is stored
    finally:
        sw._LIB = old_lib
    print("PASS set_sample_rate clamps and stores the device read-back")


def test_set_agc_live_and_on_open():
    lib = FakeLib()
    old_lib = sw._LIB
    sw._LIB = lib
    try:
        wf = make_wf(active=True, lib=lib)
        assert wf.set_agc(True) is True
        assert wf.agc_enabled is True
        # 0 = auto (AGC on), 1 = manual — matches the open path's convention.
        assert lib.gain_modes == [0], lib.gain_modes

        wf.set_agc(False)
        assert wf.agc_enabled is False
        assert lib.gain_modes[-1] == 1, lib.gain_modes
    finally:
        sw._LIB = old_lib

    # While inactive it only flips state; the next open must use it.
    opened_with = {}
    old_open = sw._open_dongle
    def fake_open(freq_hz, sample_rate, gain_db, agc=False):
        opened_with["agc"] = agc
        return object(), 2_000_000
    sw._LIB = lib
    sw._open_dongle = fake_open
    try:
        wf2 = make_wf(active=False)
        assert wf2.set_agc(True) is True
        ok = wf2.start()
        assert ok, wf2.error
        assert opened_with["agc"] is True, opened_with
        wf2.stop()
    finally:
        sw._LIB = old_lib
        sw._open_dongle = old_open
    print("PASS set_agc toggles live and applies on next open")


def test_open_uses_instance_tuning_state():
    """A runtime tune/gain made while inactive must apply on the NEXT open,
    not be reverted to the env-var defaults."""
    lib = FakeLib()
    old_lib, old_open = sw._LIB, sw._open_dongle
    opened_with = {}

    def fake_open(freq_hz, sample_rate, gain_db, agc=False):
        opened_with.update(center=freq_hz, gain=gain_db)
        return object(), 2_000_000            # dev handle + effective rate

    sw._LIB = lib
    sw._open_dongle = fake_open
    try:
        wf = make_wf(active=False)
        assert wf.tune(123_456) == sw.SDR_CENTER_HZ + 123_456
        assert wf.adjust_gain(-12.0) == sw.SDR_GAIN_DB - 12.0

        # start() now: the reader thread runs against the stub, fails its
        # reads (persistent -1), and releases the device on its own.
        ok = wf.start()
        assert ok, wf.error
        assert opened_with["center"] == sw.SDR_CENTER_HZ + 123_456, opened_with
        assert abs(opened_with["gain"] - (sw.SDR_GAIN_DB - 12.0)) < 1e-9, \
            opened_with

        wf.stop()                              # joins the reader, closes nothing twice
        assert not wf.active
    finally:
        sw._LIB = old_lib
        sw._open_dongle = old_open
    print("PASS next open uses runtime tuning state")


def main():
    tests = [
        test_inactive_tune_updates_state_only,
        test_tune_clamps_to_limits,
        test_gain_clamps_to_limits,
        test_live_tune_applies_with_ppm_correction,
        test_live_gain_applies_in_tenths,
        test_set_center_freq_inactive_and_live,
        test_set_sample_rate_clamps_and_reads_back,
        test_set_agc_live_and_on_open,
        test_open_uses_instance_tuning_state,
    ]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001 — report any crash as failure
            import traceback
            failures += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()

    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
