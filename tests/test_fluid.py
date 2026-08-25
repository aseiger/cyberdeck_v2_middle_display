#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""Headless test for fluidsim: renders frames to /tmp/fluid_test/,
exercises perturb()/reset(), and benchmarks step()/render()."""
import os, sys
# Make the project root importable so these scripts work from tests/.
_proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

import os
import time

from fluidsim import FluidSim

OUT = "/tmp/fluid_test"
os.makedirs(OUT, exist_ok=True)

sim = FluidSim()
sim._reset_fields()

t_start = time.perf_counter()
n_frames = 0
n_steps = 0
sim_ms = 0.0
render_ms = 0.0

def frame(name):
    global render_ms, n_frames
    t_r = time.perf_counter()
    img = sim.render()
    render_ms += (time.perf_counter() - t_r) * 1000
    n_frames += 1
    img.save(os.path.join(OUT, name))

# ── phase 1: fresh reset, let the pool fall (0-4s) ──
t0 = time.perf_counter()
i = 0
while time.perf_counter() - t0 < 4.0:
    t_s = time.perf_counter()
    sim.step(1.0 / 30.0)
    sim_ms += (time.perf_counter() - t_s) * 1000
    n_steps += 1
    i += 1
    if i % 15 == 0:  # every 0.5s
        frame(f"fall_{i:03d}.png")

# ── phase 2: splash perturbation in the pool at the bottom ──
sim.perturb(x=0.5, y=0.85, strength=140.0, radius=0.10, dye=0.6)
t0 = time.perf_counter()
i = 0
while time.perf_counter() - t0 < 1.5:
    sim.step(1.0 / 30.0)
    n_steps += 1
    i += 1
    if i % 15 == 0:
        frame(f"splash_{i:03d}.png")

# ── phase 3: directional jet from the side ──
sim.perturb(x=0.15, y=0.5, strength=200.0, radius=0.08, fx=1.0, fy=0.0, dye=0.8)
t0 = time.perf_counter()
i = 0
while time.perf_counter() - t0 < 1.5:
    sim.step(1.0 / 30.0)
    n_steps += 1
    i += 1
    if i % 15 == 0:
        frame(f"jet_{i:03d}.png")

# ── phase 4: reset() through the queue, let it fall again ──
sim.reset()
t0 = time.perf_counter()
i = 0
while time.perf_counter() - t0 < 2.5:
    sim.step(1.0 / 30.0)
    n_steps += 1
    i += 1
    if i % 15 == 0:
        frame(f"refall_{i:03d}.png")

wall = time.perf_counter() - t_start
print(f"steps: {n_steps}, frames saved: {n_frames}, wall: {wall:.2f}s")
print(f"avg step:   {sim_ms / n_steps:6.2f} ms")
print(f"avg render: {render_ms / max(1, n_frames):6.2f} ms")
print(f"frames in {OUT}")
