#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""Color-ramp tuning: same dynamics, different LUT shapes."""
import os, sys
# Make the project root importable so these scripts work from tests/.
_proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

import os

from PIL import Image, ImageDraw

from fluidsim import FluidSim

OUT = "/tmp/fluid_ramp"
os.makedirs(OUT, exist_ok=True)
DT = 1.0 / 30.0

BASE = dict(
    width=128, height=170, gravity=100, buoyancy=0.0, damping=1.0,
    vorticity=3.0, dissipation=0.20, source_dye_rate=5.0,
    source_velocity=120, spout_width=0.4, spout_center=0.45,
    pressure_iters=20, gamma=1.4, speed_influence=0.15, speed_ref=140)

RAMPS = {
    "A_current": ((0.00, (5, 7, 14)),
                  (0.18, (10, 26, 70)),
                  (0.45, (0, 96, 210)),
                  (0.70, (0, 176, 255)),
                  (0.88, (70, 226, 255)),
                  (1.00, (222, 252, 255))),
    "B_deep":    ((0.00, (4, 6, 14)),
                  (0.30, (8, 22, 64)),
                  (0.55, (0, 64, 160)),
                  (0.78, (0, 130, 240)),
                  (0.92, (60, 210, 255)),
                  (1.00, (210, 250, 255))),
    "C_neon":    ((0.00, (4, 5, 12)),
                  (0.35, (6, 14, 44)),
                  (0.60, (0, 70, 190)),
                  (0.80, (0, 200, 255)),
                  (0.95, (140, 245, 255)),
                  (1.00, (255, 255, 255))),
}

GRAB_AT_S = [0.4, 0.8, 1.4, 2.2, 3.2, 4.5]


def run(ramp):
    sim = FluidSim(target_size=(240, 320), ramp=ramp, **BASE)
    sim._reset_fields()
    total = int(4.5 / DT)
    frames = {}
    for i in range(1, total + 1):
        sim.step(DT)
        t = i * DT
        for g in GRAB_AT_S:
            if abs(t - g) < DT / 2:
                frames[g] = sim.render()
    for (px, py, s) in [(0.5, 0.6, 130), (0.35, 0.45, 150)]:
        sim.perturb(x=px, y=py, strength=s, radius=0.12, dye=0.5)
        for _ in range(int(0.5 / DT)):
            sim.step(DT)
    frames[99.0] = sim.render()
    return frames


cell_w, cell_h = 100, 133
n_cols = len(GRAB_AT_S) + 1
n_rows = len(RAMPS)
pad = 6
label_h = 16
M = Image.new("RGB",
             (n_cols * (cell_w + pad) + pad,
              n_rows * (cell_h + pad + label_h) + pad),
             (12, 12, 16))
dr = ImageDraw.Draw(M)
for r, (name, ramp) in enumerate(RAMPS.items()):
    frames = run(ramp)
    dr.text((pad, pad + r * (cell_h + pad + label_h)),
            f"{name}  t={GRAB_AT_S[0]}..{GRAB_AT_S[-1]}s +pokes",
            fill=(255, 255, 0))
    keys = GRAB_AT_S + [99.0]
    for c, g in enumerate(keys):
        img = frames[g].resize((cell_w, cell_h))
        x = pad + c * (cell_w + pad)
        y = pad + r * (cell_h + pad + label_h) + label_h
        M.paste(img, (x, y))
M.save(os.path.join(OUT, "ramp.png"))
print("saved", os.path.join(OUT, "ramp.png"))
