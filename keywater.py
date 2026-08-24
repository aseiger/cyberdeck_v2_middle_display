#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
keywater.py — cyberdeck keyboard -> fluid spout trigger.

Watches every /dev/input/event* device for key presses and maps each key to
a hand side (left / right) by its home position on a standard QWERTY layout.
Key down holds the spout on; key up turns it off, so the spout lasts
exactly as long as the key is pressed.  The pour height follows the key's
row (number row pours from the top, home row from the middle, bottom row
near the pool), and the space bar pours straight down from the top center:

    left half of the keyboard  ->  left spout, at the key's row height
    right half of the keyboard ->  right spout, at the key's row height
    space bar                  ->  center spout, straight down from the top

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
    58,             # Caps Lock
    59, 60, 61, 62, 63, 64,   # F1 - F6
    105,            # Left arrow
    125,            # Left Meta (Win)
}

SPACE = 57   # space bar: center spout (pours from the top center)

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


# Keyboard rows, top to bottom (1 = number/F-key row ... 4 = bottom row).
# The spout height follows the row; see SPOUT_ROW_Y in pixfluid.py.
ROW1_KEYS = frozenset({
    1,              # Esc
    2, 3, 4, 5, 6, 7, 8, 9, 10, 11,   # 1 2 3 4 5 6 7 8 9 0
    12, 13,         # - =
    14,             # Backspace
    59, 60, 61, 62, 63, 64, 65, 66, 67, 68,   # F1 - F10
    87, 88,         # F11 F12
})
ROW2_KEYS = frozenset({
    15,             # Tab
    16, 17, 18, 19, 20, 21, 22, 23, 24, 25,   # q w e r t y u i o p
    26, 27,         # [ ]
})
ROW3_KEYS = frozenset({
    58,             # Caps Lock
    30, 31, 32, 33, 34, 35, 36, 37, 38,   # a s d f g h j k l
    39, 40,         # ; '
    43,             # \\
    28,             # Enter
})
ROW4_KEYS = frozenset({
    41,             # Grave `
    42,             # Left Shift
    44, 45, 46, 47, 48,   # z x c v b
    49, 50, 51, 52, 53,   # n m , . /
    54,             # Right Shift
    29,             # Left Ctrl
    56,             # Left Alt
    97,             # Right Ctrl
    100,            # Right Alt
    125,            # Left Meta (Win)
    126,            # Right Meta (Win)
    105,            # Left arrow
    106,            # Right arrow
})


def row_for(code):
    """Map a Linux key code to keyboard row 1-4 (top to bottom), or None."""
    if code in ROW1_KEYS:
        return 1
    if code in ROW2_KEYS:
        return 2
    if code in ROW3_KEYS:
        return 3
    if code in ROW4_KEYS:
        return 4
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


def send_spout(side, on, row=None):
    """Hold a spout on/off via the lcdstats IPC socket (best effort)."""
    event = {"type": "fluid", "action": "spout",
             "side": side, "on": bool(on)}
    if row is not None:
        event["row"] = int(row)
    msg = (json.dumps(event) + "\n").encode("utf-8")
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

    # keys currently held, per side: code -> row.  While several keys are
    # held on a side the spout pours from the topmost of them, and a
    # release only turns the spout off when the last key goes up.
    held = {"left": {}, "right": {}}

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
                if etype != EV_KEY:
                    continue
                if code == SPACE:               # space bar -> center spout
                    side, row = "center", None
                else:
                    side = classify(code)
                    if side is None:
                        continue
                    row = row_for(code)
                if value == KEY_PRESS:          # key down -> spout on
                    eff = row
                    if side != "center":
                        if row is not None:
                            held[side][code] = row
                        eff = min(held[side].values()) if held[side] else None
                    if dry:
                        print(f"{device}: {code} down -> {side}"
                              + (f" (row {eff})" if eff else ""), flush=True)
                    else:
                        send_spout(side, True, eff)
                elif value == KEY_RELEASE:      # key up -> spout off (or
                    eff = None                  # still on if more keys held)
                    if side != "center":
                        held[side].pop(code, None)
                        if held[side]:
                            eff = min(held[side].values())
                    if dry:
                        if eff is not None:
                            print(f"{device}: {code} up -> {side} "
                                  f"(row {eff}, still on)", flush=True)
                        else:
                            print(f"{device}: {code} up -> {side} off",
                                  flush=True)
                    elif eff is not None:
                        send_spout(side, True, eff)
                    else:
                        send_spout(side, False)
                # KEY_REPEAT: key already down, spout already held on


if __name__ == "__main__":
    main()
