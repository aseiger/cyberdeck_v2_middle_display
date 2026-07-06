#!/usr/bin/env python
"""One-shot helper: find which GPIO the aux fan tach is wired to.

Spins the case fan at full duty, enables internal pull-ups on candidate
pins, and counts falling edges for a few seconds. The tach pin will show
a steady pulse count proportional to fan speed; unconnected pins stay ~0.

Run with the lcdstats service stopped (it owns the fan PWM pin):
    sudo service lcdstats stop
    python detect_tach.py
    sudo service lcdstats start
"""
import time
from gpiozero import PWMOutputDevice, Button

CASE_FAN = 13
# Free BCM pins (excludes display RST/DC/BL, SPI, I2C, UART, fan PWM).
CANDIDATES = [4, 5, 6, 12, 16, 17, 19, 20, 21, 22, 23, 24, 26]
SAMPLE_SECONDS = 3.0
PULSES_PER_REV = 2

counts = {pin: 0 for pin in CANDIDATES}
inputs = []


def make_handler(pin):
    def handler():
        counts[pin] += 1
    return handler


fan = PWMOutputDevice(CASE_FAN)
fan.value = 1.0
time.sleep(1.0)  # let the fan spin up

for pin in CANDIDATES:
    try:
        dev = Button(pin, pull_up=True)
        dev.when_pressed = make_handler(pin)
        inputs.append(dev)
    except Exception as e:
        print(f"GPIO {pin}: skipped ({e})")

print(f"Fan at 100%, counting pulses for {SAMPLE_SECONDS:.0f}s...")
time.sleep(SAMPLE_SECONDS)

for pin in sorted(counts):
    pulses = counts[pin]
    rpm = pulses / PULSES_PER_REV / SAMPLE_SECONDS * 60
    marker = "  <-- likely tach" if pulses > 50 else ""
    print(f"GPIO {pin:2d}: {pulses:5d} pulses  (~{rpm:5.0f} RPM if tach){marker}")

fan.value = 0.0
fan.close()
for dev in inputs:
    dev.close()
