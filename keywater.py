#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
keywater.py — cyberdeck keyboard -> fluid spout trigger.

Watches every /dev/input/event* device for key presses and maps each key to
a hand side (left / right) by its home position on a standard QWERTY layout.
Every press (and auto-repeat) sends a "burst" fluid event to the lcdstats
IPC socket, so the water spout on that side fires:

    left half of the keyboard  ->  spout on the left edge
    right half of the keyboard ->  spout on the right edge

Run as the `keywater` systemd service, or directly:

    python3 keywater.py             # forward bursts to the IPC socket
    python3 keywater.py --dry-run   # just print "<device>: <code> -> <side>"
"""

import json
import os
import select
import socket
import struct
import sys
import time

SOCK_PATH = "/tmp/lcdstats.sock"
INPUT_DIR = "/dev/input"
RESCAN_INTERVAL = 30.0        # seconds between /dev/input hotplug re-scans

# struct input_event { struct timeval; __u16 type; __u16 code; __s32 value; }
EVENT_FMT = "=llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FMT)

EV_KEY = 1
KEY_PRESS, KEY_RELEASE, KEY_REPEAT = 1, 0, 2

# ── key codes by hand side (Linux input-event-codes, QWERTY home position) ──

LEFT_KEYS = {
    1,              # Esc
    2, 3, 4, 5, 6,  # 1 2 3 4 5
    14,             # Backspace
    15,             # Tab
    16, 17, 18, 19, 20,   # q w e r t
    29,             # Left Ctrl
    30, 31, 32, 33, 34,   # a s d f g
    41,             # Grave `
    42,             # Left Shift
    44, 45, 46, 47, 48,   # z x c v b
    56,             # Left Alt
    57,             # Space
    58,             # Caps Lock
    59, 60, 61, 62, 63, 64,   # F1 - F6
    105,            # Left arrow
    125,            # Left Meta (Win)
}

RIGHT_KEYS = {
    7, 8, 9, 10, 11, 12, 13,   # 6 7 8 9 0 - =
    21, 22, 23, 24, 25,   # y u i o p
    26, 27,               # [ ]
    28,                   # Enter
    35, 36, 37, 38,       # h j k l
    39, 40,               # ; '
    43,                   # \
    49, 50, 51, 52, 53,   # n m , . /
    54,                   # Right Shift
    65, 66, 67, 68,       # F7 - F10
    87, 88,               # F11 F12
    97,                   # Right Ctrl
    100,                  # Right Alt
    106,                  # Right arrow
    126,                  # Right Meta (Win)
}


def classify(code):
    """Map a Linux key code to 'left'/'right', or None if unhandled."""
    if code in LEFT_KEYS:
        return "left"
    if code in RIGHT_KEYS:
        return "right"
    return None


def open_devices(fds):
    """Open any not-yet-opened /dev/input/event* devices into `fds`."""
    added = 0
    for name in sorted(os.listdir(INPUT_DIR)):
        if not name.startswith("event"):
            continue
        path = os.path.join(INPUT_DIR, name)
        if any(v == name for v in fds.values()):
            continue
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        fds[fd] = name
        added += 1
    return added


def send_burst(side):
    """Send a burst fluid event to the lcdstats IPC socket (best effort)."""
    msg = (json.dumps({"type": "fluid", "action": "burst", "side": side})
           + "\n").encode("utf-8")
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(SOCK_PATH)
        s.sendall(msg)
        s.close()
    except OSError:
        pass  # daemon not up / busy — drop the event


def main():
    dry = "--dry-run" in sys.argv
    fds = {}
    if open_devices(fds) == 0:
        print("keywater: no /dev/input devices accessible", file=sys.stderr)
        sys.exit(1)
    if dry:
        print("keywater: dry-run, watching: "
              + ", ".join(sorted(set(fds.values()))), flush=True)

    last_rescan = time.monotonic()
    while True:
        # periodic hotplug re-scan (new keyboards / event nodes)
        now = time.monotonic()
        if now - last_rescan > RESCAN_INTERVAL:
            last_rescan = now
            open_devices(fds)

        if not fds:
            time.sleep(0.5)
            continue

        try:
            ready, _, _ = select.select(list(fds.keys()), [], [], 0.5)
        except (ValueError, OSError):
            time.sleep(0.5)
            continue

        for fd in ready:
            try:
                data = os.read(fd, 64 * EVENT_SIZE)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                fds.pop(fd, None)
                continue

            device = fds.get(fd, "?")
            for i in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                _sec, _usec, etype, code, value = struct.unpack_from(
                    EVENT_FMT, data, i)
                if etype != EV_KEY or value not in (KEY_PRESS, KEY_REPEAT):
                    continue
                side = classify(code)
                if side is None:
                    continue
                if dry:
                    print(f"{device}: {code} -> {side}", flush=True)
                else:
                    send_burst(side)


if __name__ == "__main__":
    main()
