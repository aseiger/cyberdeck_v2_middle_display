#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""Buoyancy probe: does heavy-dye sinking give a clean 'falls and pools
at the bottom' behavior?"""
import os, sys
# Make the project root importable so these scripts work from tests/.
_proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

import os

from PIL import Image, ImageDraw

from fluidsim import FluidSim

OUT = "/tmp/fluid_buoy"
os.makedirs(OUT, exist_ok=True)
DT = 1.0 / 30.0

VARIANTS = {
    "sink_only": dict(
        width=128, height=170, gravity=0, buoyancy=3.0, damping=1.0,
        vorticity=2.0, dissipation=0.35, source_dye_rate=0.0,
        source_velocity=0.0, spout_width=1.0, pressure_iters=20, gamma=1.4),
    "sink_flow": dict(
        width=128, height=170, gravity=0, buoyancy=2.5, damping=1.0,
        vorticity=1.8, dissipation=0.50, source_dye_rate=3.0,
        source_velocity=60, spout_width=1.0, pressure_iters=20, gamma=1.4),
    "sink_strong": dict(
        width=128, height=170, gravity=0, buoyancy=6.0, damping=1.2,
        vorticity=1.5, dissipation=0.30, source_dye_rate=0.0,
        source_velocity=0.0, spout_width=1.0, pressure_iters=20, gamma=1.4),
}

GRAB_AT_S = [0.2, 0.5, 0.8, 1.2, 1.8, 2.5]


def run(name, kwargs):
    sim = FluidSim(target_size=(240, 320), **kwargs)
    sim._reset_fields()
    total = int(2.5 / DT)
    frames = {}
    for i in range(1, total + 1):
        sim.step(DT)
        t = i * DT
        for g in GRAB_AT_S:
            if abs(t - g) < DT / 2:
                frames[g] = sim.render()
    # poke the settled pool and grab the aftermath
    sim.perturb(x=0.5, y=0.8, strength=120, radius=0.12, dye=0.5)
    for i in range(int(0.8 / DT)):
        sim.step(DT)
    frames[99.0] = sim.render()
    return frames


cell_w, cell_h = 110, 147
n_cols = len(GRAB_AT_S) + 1
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
            f"{name}  t={GRAB_AT_S[0]}s..{GRAB_AT_S[-1]}s +poke",
            fill=(255, 255, 0))
    keys = GRAB_AT_S + [99.0]
    for c, g in enumerate(keys):
        img = frames[g].resize((cell_w, cell_h))
        x = pad + c * (cell_w + pad)
        y = pad + r * (cell_h + pad + label_h) + label_h
        M.paste(img, (x, y))
M.save(os.path.join(OUT, "buoy.png"))
print("saved", os.path.join(OUT, "buoy.png"))
