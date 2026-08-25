#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""Parameter sweep for fluidsim: renders a fixed-time fall sequence for
several parameter sets side by side so we can pick the best-looking one."""
import os, sys
# Make the project root importable so these scripts work from tests/.
_proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

import os
import itertools

import numpy as np
from PIL import Image

from fluidsim import FluidSim

OUT = "/tmp/fluid_tune"
os.makedirs(OUT, exist_ok=True)

# Each candidate: dict of FluidSim kwargs
CANDIDATES = {
    "A_base":      dict(width=96,  height=128, gravity=160, damping=0.8,
                        vorticity=3.0, dissipation=0.18, source_dye_rate=2.0,
                        source_velocity=70, pressure_iters=16, gamma=1.0),
    "B_fast_dark": dict(width=96,  height=128, gravity=420, damping=1.2,
                        vorticity=1.2, dissipation=0.60, source_dye_rate=4.0,
                        source_velocity=140, pressure_iters=16, gamma=1.5),
    "C_fine":      dict(width=128, height=170, gravity=380, damping=1.0,
                        vorticity=1.6, dissipation=0.55, source_dye_rate=4.0,
                        source_velocity=130, pressure_iters=20, gamma=1.5),
    "D_fine_fast": dict(width=160, height=213, gravity=460, damping=1.1,
                        vorticity=1.4, dissipation=0.60, source_dye_rate=5.0,
                        source_velocity=150, pressure_iters=24, gamma=1.5),
}

# simulation times (seconds) at which to grab a frame
T_STEPS = 30          # steps per frame, dt fixed below
DT = 1.0 / 30.0
FRAME_AT = [1, 2, 4, 7, 11, 16]   # step multiples (0.03..0.53s of sim)


def run_candidate(name, kwargs):
    sim = FluidSim(target_size=(240, 320), **kwargs)
    sim._reset_fields()
    frames = []
    total_steps = FRAME_AT[-1]
    for step in range(1, total_steps + 1):
        sim.step(DT)
        if step in FRAME_AT:
            frames.append(sim.render())
    return frames


# Build a montage: columns = time, rows = candidate
cell_w, cell_h = 120, 160
n_cols = len(FRAME_AT)
n_rows = len(CANDIDATES)
pad = 6
label_h = 18
montage = Image.new("RGB",
                    (n_cols * (cell_w + pad) + pad,
                     n_rows * (cell_h + pad + label_h) + pad),
                    (12, 12, 16))
from PIL import ImageDraw
dr = ImageDraw.Draw(montage)

for r, (name, kwargs) in enumerate(CANDIDATES.items()):
    frames = run_candidate(name, kwargs)
    for c, img in enumerate(frames):
        img = img.resize((cell_w, cell_h))
        x = pad + c * (cell_w + pad)
        y = pad + r * (cell_h + pad + label_h) + label_h
        montage.paste(img, (x, y))
    dr.text((pad, pad + r * (cell_h + pad + label_h)), name, fill=(255, 255, 0))

montage.save(os.path.join(OUT, "montage.png"))
print("saved", os.path.join(OUT, "montage.png"))
print("grid perf note: see per-candidate step cost below")

# quick perf per candidate
for name, kwargs in CANDIDATES.items():
    import time
    sim = FluidSim(target_size=(240, 320), **kwargs)
    sim._reset_fields()
    t0 = time.perf_counter()
    for _ in range(30):
        sim.step(DT)
    step_ms = (time.perf_counter() - t0) / 30 * 1000
    t0 = time.perf_counter()
    sim.render()
    render_ms = (time.perf_counter() - t0) * 1000
    print(f"  {name:12s} grid={kwargs['width']}x{kwargs['height']:3d}  step={step_ms:6.2f}ms render={render_ms:5.2f}ms")
