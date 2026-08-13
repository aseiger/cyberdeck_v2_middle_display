#!/usr/bin/env python3
"""Build a PWM→RPM lookup table for the aux fan.

Sweeps the fan PWM from 0% to 100% in steps, waits for the RPM to
settle at each step, and records the steady-state RPM.  Outputs a
JSON file (aux_fan_curve.json) that lcdstats.py loads at startup.

Usage:
    # Stop lcdstats first (it owns the PWM pin).
    sudo systemctl stop lcdstats.service
    python3 tune_aux_fan_curve.py

    # Custom step size and settle time:
    python3 tune_aux_fan_curve.py --steps 21 --settle 3
"""

import argparse
import json
import os
import signal
import sys
import time

sys.path.insert(0, ".")
from lib.lcdconfig import HardwarePWM
from gpiozero import Button

# ---------------------------------------------------------------------------
# Hardware constants (match lcdstats.py)
# ---------------------------------------------------------------------------
CASE_FAN_GPIO = 13
FAN_TACH_GPIO = 17
PULSES_PER_REV = 2
OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "aux_fan_curve.json")

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_STEPS = 11            # number of PWM steps (0..1 inclusive)
DEFAULT_SETTLE_S = 4.0        # seconds to wait at each step before reading
DEFAULT_READ_S = 2.0          # seconds to average RPM at each step


class TachReader:
    def __init__(self, pin, pulses_per_rev=2):
        self._pulses_per_rev = pulses_per_rev
        self._count = 0
        self._last_time = time.monotonic()
        self._btn = Button(pin, pull_up=True)
        self._btn.when_pressed = self._on_pulse

    def _on_pulse(self):
        self._count += 1

    def read_rpm(self):
        now = time.monotonic()
        dt = now - self._last_time
        pulses = self._count
        self._count = 0
        self._last_time = now
        if dt <= 0:
            return 0.0
        return pulses / self._pulses_per_rev / dt * 60.0

    def close(self):
        self._btn.close()


def average_rpm(tach, duration_s, sample_ms=100):
    """Read tach repeatedly and return the mean RPM."""
    total = 0.0
    n = 0
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        total += tach.read_rpm()
        n += 1
        time.sleep(sample_ms / 1000.0)
    return total / n if n else 0.0


def sweep(steps, settle_s, read_s):
    print("=" * 55)
    print("  Aux Fan PWM→RPM Curve Mapper")
    print("=" * 55)
    print(f"  Steps        : {steps}")
    print(f"  Settle time  : {settle_s:.1f} s")
    print(f"  Read time    : {read_s:.1f} s")
    print(f"  Output file  : {OUTPUT_FILE}")
    print("=" * 55)
    print()
    print("WARNING: This will drive the aux fan PWM from 0% to 100%.")
    print("Press Ctrl+C at any time to abort.")
    print()

    for i in range(3, 0, -1):
        print(f"Starting in {i}...  (Ctrl+C to cancel)  ")
        time.sleep(1)

    pwm = HardwarePWM(CASE_FAN_GPIO, frequency=1000)
    tach = TachReader(FAN_TACH_GPIO, pulses_per_rev=PULSES_PER_REV)

    def _cleanup(signum=None, frame=None):
        print("\nCleaning up...")
        pwm.value = 0.0
        pwm.close()
        tach.close()
        if signum is not None:
            sys.exit(0)

    signal.signal(signal.SIGINT, _cleanup)
    signal.signal(signal.SIGTERM, _cleanup)

    table = []

    print()
    print(f"  {'PWM %':>6}   {'Settle RPM':>12}   {'Avg RPM':>10}")
    print(f"  {'------':>6}   {'----------':>12}   {'-------':>10}")

    try:
        for step in range(steps):
            duty = step / (steps - 1)
            pwm_pct = duty * 100

            pwm.value = duty
            time.sleep(settle_s)

            rpm = average_rpm(tach, read_s)

            table.append({"pwm": round(duty, 4), "rpm": round(rpm, 1)})

            print(f"  {pwm_pct:6.1f}   {rpm:12.0f}   {rpm:10.0f}")

    finally:
        pwm.value = 0.0
        pwm.close()
        tach.close()

    # Write output
    data = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "steps": steps,
        "curve": table,
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(data, f, indent=2)

    print()
    print(f"  Table written to {OUTPUT_FILE}")
    print(f"  {len(table)} entries recorded.")
    print("=" * 55)


def main():
    parser = argparse.ArgumentParser(
        description="Map aux fan PWM duty to steady-state RPM."
    )
    parser.add_argument(
        "--steps", type=int, default=DEFAULT_STEPS,
        help=f"Number of PWM steps (default: {DEFAULT_STEPS})"
    )
    parser.add_argument(
        "--settle", type=float, default=DEFAULT_SETTLE_S,
        help=f"Seconds to wait before reading (default: {DEFAULT_SETTLE_S})"
    )
    parser.add_argument(
        "--read", type=float, default=DEFAULT_READ_S,
        help=f"Seconds to average RPM (default: {DEFAULT_READ_S})"
    )
    args = parser.parse_args()
    sweep(args.steps, args.settle, args.read)


if __name__ == "__main__":
    main()
