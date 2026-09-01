#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
test_sdr_smoke.py - standalone smoke test for sdr_waterfall.SdrWaterfall.

Opens the real RTL-SDR dongle (needs nothing else from the daemon), collects
a few seconds of waterfall columns, saves a PNG snapshot for visual check,
stops the waterfall, and then verifies with rtl_test that the device is free
again — i.e. other programs can use it right after we're done.

Usage:  .venv/bin/python tests/test_sdr_smoke.py [seconds]
"""

import os
import subprocess
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image
from sdr_waterfall import SdrWaterfall


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0

    wf = SdrWaterfall(width=240, height=252)

    print("starting...")
    t0 = time.monotonic()
    ok = wf.start()
    print(f"start -> {ok} ({time.monotonic() - t0:.3f}s)" + (f", error: {wf.error}" if not ok else ""))
    if not ok:
        return 1

    time.sleep(seconds)

    cols = wf.column_count
    rate = cols / seconds
    print(f"columns produced: {cols} ({rate:.1f}/s over {seconds}s)")

    snap = wf.snapshot()
    nonzero_pct = float((snap > 0).mean()) * 100.0
    pegged_pct = float((snap >= 250).mean()) * 100.0
    print(f"frame: min={snap.min()} max={snap.max()} mean={float(snap.mean()):.1f} "
          f"| nonzero {nonzero_pct:.1f}% | pegged>=250 {pegged_pct:.1f}%")

    if cols < 3:
        print("FAIL: reader produced almost no columns")
        wf.stop()
        return 1
    if pegged_pct > 99.0:
        print("WARN: frame fully pegged — gain too high, lower SDR_GAIN_DB")
    elif nonzero_pct < 5.0:
        print("WARN: frame almost empty — gain too low or no signal, raise SDR_GAIN_DB")

    out = "/tmp/sdr_snapshot.png"
    Image.fromarray(snap, "L").save(out)
    print(f"snapshot saved -> {out}")

    t0 = time.monotonic()
    wf.stop()
    print(f"stop -> done ({time.monotonic() - t0:.3f}s)")
    if wf.active:
        print("FAIL: still active after stop")
        return 1

    # Proof of the core requirement: another program can grab the dongle now.
    probe = subprocess.run(["rtl_test", "-t"], capture_output=True, text=True, timeout=20)
    head = " | ".join(probe.stdout.splitlines()[:3])
    print(f"post-stop rtl_test rc={probe.returncode}: {head}")
    if probe.returncode != 0:
        print("FAIL: device not free after stop (rtl_test could not open it)")
        return 1

    print("PASS sdr smoke test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
