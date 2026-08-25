#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""Steady-state probe: run variants for 3s of sim time, save the fall
sequence and the settled look."""
import os, sys
# Make the project root importable so these scripts work from tests/.
_proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

import os

from PIL import Image, ImageDraw

from fluidsim import FluidSim

OUT = "/tmp/fluid_steady"
os.makedirs(OUT, exist_ok=True)
DT = 1.0 / 30.0

VARIANTS = {
    "pour_full": dict(
        width=128, height=170, gravity=380, damping=1.0, vorticity=1.6,
        dissipation=0.55, source_dye_rate=4.0, source_velocity=130,
        spout_width=1.0, spout_center=0.5, pressure_iters=20, gamma=1.5),
    "spout_35": dict(
        width=128, height=170, gravity=380, damping=1.0, vorticity=1.8,
        dissipation=0.55, source_dye_rate=6.0, source_velocity=170,
        spout_width=0.35, spout_center=0.35, pressure_iters=20, gamma=1.5),
    "spout_50": dict(
        width=128, height=170, gravity=380, damping=1.0, vorticity=1.8,
        dissipation=0.55, source_dye_rate=6.0, source_velocity=170,
        spout_width=0.35, spout_center=0.5, pressure_iters=20, gamma=1.5),
}

GRAB_AT_S = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0]

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
    return frames


cell_w, cell_h = 110, 147
n_cols = len(GRAB_AT_S)
n_rows = len(VARIANTS)
pad = 6
label_h = 16
M = Image.new("RGB",
             (n_cols * (cell_w + pad) + pad,
              n_rows * (cell_h + pad + label_h) + pad),
             (12, 12, 16))
dr = ImageDraw.Draw(M)
for r, (name, kwargs) in enumerate(VARIANTS.items()):
    frames = run(name, kwargs)
    dr.text((pad, pad + r * (cell_h + pad + label_h)),
            f"{name}  t={GRAB_AT_S[0]}s -> {GRAB_AT_S[-1]}s", fill=(255, 255, 0))
    for c, g in enumerate(GRAB_AT_S):
        img = frames[g].resize((cell_w, cell_h))
        x = pad + c * (cell_w + pad)
        y = pad + r * (cell_h + pad + label_h) + label_h
        M.paste(img, (x, y))
M.save(os.path.join(OUT, "steady.png"))
print("saved", os.path.join(OUT, "steady.png"))
