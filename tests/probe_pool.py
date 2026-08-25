#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""Quantify how bright the bottom pool stays over time, for several
dissipation values (buoyancy-driven sinking)."""
import os, sys
# Make the project root importable so these scripts work from tests/.
_proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

import os
import numpy as np

from PIL import Image

from fluidsim import FluidSim

OUT = "/tmp/fluid_pool"
os.makedirs(OUT, exist_ok=True)
DT = 1.0 / 30.0

def run(dissipation, t_end=2.5):
    sim = FluidSim(
        target_size=(240, 320), width=128, height=170,
        gravity=0, buoyancy=3.0, damping=1.0, vorticity=1.5,
        dissipation=dissipation, source_dye_rate=0.0, source_velocity=0.0,
        spout_width=1.0, pressure_iters=20, gamma=1.0)
    sim._reset_fields()
    total = int(t_end / DT)
    bottom_frac = 0.20
    nb = int(sim.H * bottom_frac)
    samples = []
    for i in range(1, total + 1):
        sim.step(DT)
        if i % 30 == 0:
            bottom = sim.dens[-nb:, :]
            samples.append((i * DT, bottom.mean(), bottom.max()))
    img = sim.render()
    return img, samples

diss_list = [0.05, 0.10, 0.20]
imgs = []
for d in diss_list:
    img, samples = run(d)
    imgs.append(img.resize((120, 160)))
    print(f"--- dissipation={d}")
    for t, m, mx in samples:
        print(f"   t={t:4.2f}s  pool_mean={m:5.3f}  pool_max={mx:5.3f}")

from PIL import ImageDraw
M = Image.new("RGB", (3 * 126 + 6, 160 + 30), (12, 12, 16))
dr = ImageDraw.Draw(M)
for i, (d, img) in enumerate(zip(diss_list, imgs)):
    M.paste(img, (6 + i * 126, 24))
    dr.text((6 + i * 126, 4), f"diss={d}", fill=(255, 255, 0))
M.save(os.path.join(OUT, "pool.png"))
print("saved", os.path.join(OUT, "pool.png"))
