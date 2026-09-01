#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
test_sdr_band_smoke.py - standalone smoke test for sdr_waterfall.SdrPanorama.

Opens the real RTL-SDR dongle, runs the full-band sweep until at least two
complete scans are committed (each scan passes over 24 MHz–1.76 GHz, so this
takes ~1–2 minutes), saves a snapshot, stops, and verifies via rtl_test that
the device is free again for other programs.

Usage:  .venv/bin/python tests/test_sdr_band_smoke.py [timeout_seconds]
"""

import os
import subprocess
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image
from sdr_waterfall import SdrPanorama


def main():
    timeout = float(sys.argv[1]) if len(sys.argv) > 1 else 150.0

    p = SdrPanorama(width=240, height=252)

    print("starting...")
    ok = p.start()
    print(f"start -> {ok}" + (f", error: {p.error}" if not ok else ""))
    if not ok:
        return 1

    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end and p.row_count < 2:
        time.sleep(5.0)
        print(f"  rows={p.row_count} sweep_seconds="
              f"{p.sweep_seconds if p.sweep_seconds is not None else '--'}", flush=True)

    snap = p.snapshot()
    nonzero_pct = float((snap > 0).mean()) * 100.0
    print(f"frame: min={snap.min()} max={snap.max()} mean={float(snap.mean()):.1f} "
          f"| nonzero {nonzero_pct:.1f}% | rows committed: {p.row_count}")

    if p.row_count < 1:
        print("FAIL: no full sweep completed in time")
        p.stop()
        return 1
    if snap.max() == 0:
        print("FAIL: frame is empty despite committed sweeps")
        p.stop()
        return 1

    out = "/tmp/sdr_band_snapshot.png"
    Image.fromarray(snap, "L").save(out)
    print(f"snapshot saved -> {out}")

    if p.active:
        print("stopping...")
    t0 = time.monotonic()
    p.stop()
    print(f"stop -> done ({time.monotonic() - t0:.3f}s)")
    if p.active:
        print("FAIL: still active after stop")
        return 1

    # Proof of the core requirement: another program can grab the dongle now.
    probe = subprocess.run(["rtl_test", "-t"], capture_output=True, text=True, timeout=20)
    head = " | ".join(probe.stdout.splitlines()[:3])
    print(f"post-stop rtl_test rc={probe.returncode}: {head}")
    if probe.returncode != 0 or "Failed to open" in probe.stdout:
        print("FAIL: device not free after stop (rtl_test could not open it)")
        return 1

    print("PASS sdr band smoke test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
