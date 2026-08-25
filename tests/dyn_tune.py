#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""Dynamic steady-state tuning: more swirl, sharper filaments, coherent jet."""
import os, sys
# Make the project root importable so these scripts work from tests/.
_proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

import os

from PIL import Image, ImageDraw

from fluidsim import FluidSim

OUT = "/tmp/fluid_dyn"
os.makedirs(OUT, exist_ok=True)
DT = 1.0 / 30.0

CANDIDATES = {
    "dyn1": dict(width=128, height=170, gravity=120, buoyancy=0.0, damping=1.0,
                 vorticity=4.0, dissipation=0.15, source_dye_rate=6.0,
                 source_velocity=140, spout_width=0.35, spout_center=0.5,
                 pressure_iters=20, gamma=1.6, speed_influence=0.6, speed_ref=110),
    "dyn2": dict(width=128, height=170, gravity=140, buoyancy=0.0, damping=1.0,
                 vorticity=5.0, dissipation=0.12, source_dye_rate=7.0,
                 source_velocity=160, spout_width=0.30, spout_center=0.5,
                 pressure_iters=20, gamma=1.7, speed_influence=0.7, speed_ref=110),
    "dyn3": dict(width=128, height=170, gravity=110, buoyancy=0.0, damping=1.0,
                 vorticity=3.5, dissipation=0.18, source_dye_rate=5.0,
                 source_velocity=120, spout_width=0.45, spout_center=0.4,
                 pressure_iters=20, gamma=1.5, speed_influence=0.5, speed_ref=110),
}

GRAB_AT_S = [0.4, 0.8, 1.3, 2.0, 3.0, 4.5]


def run(name, kwargs):
    sim = FluidSim(target_size=(240, 320), **kwargs)
    sim._reset_fields()
    total = int(4.5 / DT)
    frames = {}
    for i in range(1, total + 1):
        sim.step(DT)
        t = i * DT
        for g in GRAB_AT_S:
            if abs(t - g) < DT / 2:
                frames[g] = sim.render()
    sim.perturb(x=0.5, y=0.65, strength=140, radius=0.13, dye=0.4)
    for _ in range(int(0.5 / DT)):
        sim.step(DT)
    frames[99.0] = sim.render()
    return frames


cell_w, cell_h = 100, 133
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
            f"{name}  t={GRAB_AT_S[0]}..{GRAB_AT_S[-1]}s +poke",
            fill=(255, 255, 0))
    keys = GRAB_AT_S + [99.0]
    for c, g in enumerate(keys):
        img = frames[g].resize((cell_w, cell_h))
        x = pad + c * (cell_w + pad)
        y = pad + r * (cell_h + pad + label_h) + label_h
        M.paste(img, (x, y))
M.save(os.path.join(OUT, "dyn.png"))
print("saved", os.path.join(OUT, "dyn.png"))
