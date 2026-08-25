#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""Final tuning: pick the best source shape, validate over 3s + pokes."""
import os, sys
# Make the project root importable so these scripts work from tests/.
_proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

import os
import numpy as np

from PIL import Image, ImageDraw

from fluidsim import FluidSim

OUT = "/tmp/fluid_final"
os.makedirs(OUT, exist_ok=True)
DT = 1.0 / 30.0

COMMON = dict(
    width=128, height=170, gravity=90, buoyancy=1.2, damping=1.0,
    vorticity=2.2, dissipation=0.25, source_dye_rate=4.0,
    source_velocity=100, pressure_iters=20, gamma=1.35)

CANDIDATES = {
    "wide_pour":     dict(COMMON, spout_width=1.0, spout_center=0.5),
    "stream":        dict(COMMON, spout_width=0.5, spout_center=0.5),
    "stream_off":    dict(COMMON, spout_width=0.4, spout_center=0.35),
}

GRAB_AT_S = [0.3, 0.6, 1.0, 1.5, 2.0, 3.0]


def run(name, kwargs):
    sim = FluidSim(target_size=(240, 320), **kwargs)
    sim._reset_fields()
    total = int(3.0 / DT)
    frames = {}
    for i in range(1, total + 1):
        sim.step(DT)
        t = i * DT
        for g in GRAB_AT_S:
            if abs(t - g) < DT / 2:
                frames[g] = sim.render()
    # a few pokes to show interactivity
    for (px, py, s) in [(0.5, 0.7, 120), (0.3, 0.5, 150), (0.7, 0.6, 130)]:
        sim.perturb(x=px, y=py, strength=s, radius=0.12, dye=0.3)
        for _ in range(int(0.5 / DT)):
            sim.step(DT)
    frames[99.0] = sim.render()
    return frames


cell_w, cell_h = 104, 139
n_cols = len(GRAB_AT_S) + 1
n_rows = len(CANDIDATES)
pad = 6
label_h = 16
M = Image.new("RGB",
             (n_cols * (cell_w + pad) + pad,
              n_rows * (cell_h + pad + label_h) + pad),
             (12, 12, 16))
dr = ImageDraw.Draw(M)
for r, (name, kwargs) in enumerate(CANDIDATES.items()):
    frames = run(name, kwargs)
    dr.text((pad, pad + r * (cell_h + pad + label_h)),
            f"{name}  t={GRAB_AT_S[0]}..{GRAB_AT_S[-1]}s +pokes",
            fill=(255, 255, 0))
    keys = GRAB_AT_S + [99.0]
    for c, g in enumerate(keys):
        img = frames[g].resize((cell_w, cell_h))
        x = pad + c * (cell_w + pad)
        y = pad + r * (cell_h + pad + label_h) + label_h
        M.paste(img, (x, y))
M.save(os.path.join(OUT, "final.png"))
print("saved", os.path.join(OUT, "final.png"))
