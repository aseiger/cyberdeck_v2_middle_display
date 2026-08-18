#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
fluidsim.py — 2D incompressible fluid simulation for the cyberdeck LCD.

A numpy-vectorized implementation of Jos Stam's "Stable Fluids" (1999)
solver: semi-Lagrangian advection + Gauss-Seidel pressure projection,
with vorticity confinement to keep the motion lively.  It runs on a
coarse grid (default 96x128) and renders upscaled to the panel
resolution (240x320), so a full step costs only a few milliseconds on a
Pi 5.

Behavior:
  * `reset()` fills the top of the screen with fluid (a pool with a
    slightly wavy bottom edge) and clears everything else.
  * Each `step(dt)` applies gravity, a continuous "pour" source across
    the top edge, vorticity confinement, and the usual advect/project
    cycle, so the fluid falls from the top and pools at the bottom
    while a recirculating loop keeps it in motion.
  * `perturb(...)` / `reset()` are thread-safe: events are queued and
    applied atomically at the start of the next `step()`.  This is the
    hook for external perturbations (e.g. the GTK applet sending
    `{"type": "fluid", "action": "perturb", ...}` over the Unix socket).

Typical usage (from a render loop):

    sim = FluidSim(target_size=(240, 320))
    sim.reset()
    while active:
        sim.step(dt)            # dt = real elapsed seconds
        img = sim.render()      # PIL.Image, RGB, 240x320
        push_to_display(img)
"""

import math

import numpy as np
from PIL import Image


class FluidSim:
    """Stable-fluids solver with a top "pour" source and splash API."""

    def __init__(self, width=128, height=170, target_size=(240, 320),
                 gravity=100.0, buoyancy=0.0, damping=1.0, vorticity=3.0,
                 max_velocity=240.0, dissipation=0.20,
                 source_dye_rate=5.0, source_velocity=120.0,
                 spout_width=0.4, spout_center=0.45,
                 pressure_iters=20, gamma=1.4,
                 speed_influence=0.15, speed_ref=140.0,
                 background=(4, 6, 14),
                 ramp=((0.00, (4, 6, 14)),
                      (0.30, (8, 22, 64)),
                      (0.55, (0, 64, 160)),
                      (0.78, (0, 130, 240)),
                      (0.92, (60, 210, 255)),
                      (1.00, (210, 250, 255)))):
        self.W = int(width)
        self.H = int(height)
        self.target_size = (int(target_size[0]), int(target_size[1]))

        # physics (all velocities in grid-cells / second)
        self.gravity = float(gravity)
        self.buoyancy = float(buoyancy)
        self.damping = float(damping)
        self.vorticity = float(vorticity)
        self.max_velocity = float(max_velocity)
        self.dissipation = float(dissipation)
        self.source_dye_rate = float(source_dye_rate)
        self.source_velocity = float(source_velocity)
        self.spout_width = min(1.0, max(0.05, float(spout_width)))
        self.spout_center = min(1.0, max(0.0, float(spout_center)))
        self.pressure_iters = int(pressure_iters)
        self.gamma = float(gamma)
        self.speed_influence = float(speed_influence)
        self.speed_ref = float(speed_ref)

        H, W = self.H, self.W
        self.u = np.zeros((H, W), dtype=np.float32)    # x-velocity
        self.v = np.zeros((H, W), dtype=np.float32)    # y-velocity (down+)
        self.u0 = np.zeros_like(self.u)
        self.v0 = np.zeros_like(self.v)
        self.dens = np.zeros((H, W), dtype=np.float32)  # dye (0..~1.5)
        self.dens0 = np.zeros_like(self.dens)
        self.p = np.zeros((H, W), dtype=np.float32)     # scratch (pressure)
        self.div = np.zeros((H, W), dtype=np.float32)   # scratch (divergence)
        self._curl = np.zeros((H, W), dtype=np.float32)
        self._gx = np.zeros((H, W), dtype=np.float32)
        self._gy = np.zeros((H, W), dtype=np.float32)

        # cached advect indices (interior grid)
        self._ix = np.arange(1, W - 1, dtype=np.float32)[None, :]  # (1, W-2)
        self._iy = np.arange(1, H - 1, dtype=np.float32)[:, None]  # (H-2, 1)

        # cached bilinear-upsample indices for render()
        tw, th = self.target_size
        self._rx0, self._rx1, self._rdx = self._axis_coeffs(W - 1, tw)
        self._ry0, self._ry1, self._rdy = self._axis_coeffs(H - 1, th)

        self._lut = self._build_lut(background, ramp)

        # source window across the top edge
        if self.spout_width >= 0.98:
            self._source_weight = np.ones((1, W), dtype=np.float32)
        else:
            x = np.arange(W, dtype=np.float32) / (W - 1)
            half = self.spout_width / 2.0
            t = np.clip(1.0 - np.abs(x - self.spout_center) / half, 0.0, 1.0)
            t = 0.5 * (1.0 + np.cos(np.pi * t))  # smooth falloff
            self._source_weight = t[None, :].astype(np.float32)

        self._pending = []  # event queue: ("perturb", args) / ("reset",)

    # ── public API ────────────────────────────────────────────────────

    def reset(self):
        """Queue a reset: fluid restarts as a pool at the top of the screen."""
        self._pending.append(("reset",))

    def perturb(self, x=0.5, y=0.5, strength=80.0, radius=0.12,
                fx=0.0, fy=0.0, dye=0.0):
        """Queue a perturbation (splash / jet), applied on the next step.

        x, y      position, normalized [0, 1] (0,0 = top-left)
        strength  velocity impulse magnitude in grid-cells/second
        radius    blob radius, fraction of the shorter grid dimension
        fx, fy    direction vector; (0, 0) = radial burst
        dye       dye to add at the splash (0..~1)
        """
        self._pending.append(("perturb", (
            float(x), float(y), float(strength), float(radius),
            float(fx), float(fy), float(dye))))

    def step(self, dt):
        """Advance the simulation by dt seconds."""
        dt = min(max(float(dt), 1e-4), 0.05)
        self._apply_events()

        u, v = self.u, self.v

        # ── forces ──
        v += self.gravity * dt
        if self.buoyancy:
            # Boussinesq buoyancy: dye is "heavy" and drives its own
            # downward flow, so it sinks and stably pools at the bottom.
            v += self.buoyancy * self.dens * dt
        damp = max(0.0, 1.0 - self.damping * dt)
        u *= damp
        v *= damp

        # continuous pour from the top edge (full-width sheet or spout)
        band = 3
        w = self._source_weight  # (1, W) gaussian window, 1.0 outside the pour
        dye_add = np.minimum(1.0 - self.dens[:band, :],
                             self.source_dye_rate * dt * w)
        self.dens[:band, :] += dye_add
        v[:band, :] = np.minimum(
            self.max_velocity, v[:band, :] + self.source_velocity * dt * w)

        self._vorticity(dt)

        # ── velocity: project, advect, project ──
        self._project()
        np.copyto(self.u0, u)
        np.copyto(self.v0, v)
        self._advect(1, self.u, self.u0, self.u0, self.v0, dt)
        self._advect(2, self.v, self.v0, self.u0, self.v0, dt)
        self._project()
        np.clip(u, -self.max_velocity, self.max_velocity, out=u)
        np.clip(v, -self.max_velocity, self.max_velocity, out=v)

        # ── dye: advect, dissipate ──
        np.copyto(self.dens0, self.dens)
        self._advect(0, self.dens, self.dens0, u, v, dt)
        self.dens *= max(0.0, 1.0 - self.dissipation * dt)
        np.clip(self.dens, 0.0, 1.5, out=self.dens)

    def render(self):
        """Return the current state as a PIL RGB image at target_size."""
        # Base intensity is dye density; optionally brighten fast-moving
        # regions so the flow structure (jets, eddies) stays visible even
        # when the dye has mixed into a roughly uniform field.
        if self.speed_influence > 0.0:
            speed = np.sqrt(self.u * self.u + self.v * self.v)
            norm = np.clip(speed / self.speed_ref, 0.0, 1.0)
            f = self.dens * (1.0 + self.speed_influence * norm)
        else:
            f = self.dens

        tw, th = self.target_size
        if (self.W, self.H) == (tw, th):
            field = f
        else:
            # bilinear upscale of the scalar field (smooth dye edges)
            x0, x1 = self._rx0[None, :], self._rx1[None, :]
            dxi = self._rdx[None, :]
            y0, y1 = self._ry0[:, None], self._ry1[:, None]
            dyi = self._rdy[:, None]
            row0 = f[y0, x0] + (f[y0, x1] - f[y0, x0]) * dxi
            row1 = f[y1, x0] + (f[y1, x1] - f[y1, x0]) * dxi
            field = row0 + (row1 - row0) * dyi

        if self.gamma != 1.0:
            field = np.power(np.clip(field, 0.0, 1.0), self.gamma)
        idx = np.clip(field * 255.0, 0, 255).astype(np.int32)
        rgb = self._lut[idx]  # (th, tw, 3) uint8
        return Image.fromarray(rgb, "RGB")

    # ── event queue ───────────────────────────────────────────────────

    def _apply_events(self):
        if not self._pending:
            return
        events = self._pending[:]
        del self._pending[:]
        for ev in events:
            if ev[0] == "reset":
                self._reset_fields()
            elif ev[0] == "perturb":
                self._perturb(*ev[1])

    def _reset_fields(self):
        self.u.fill(0.0)
        self.v.fill(0.0)
        self.u0.fill(0.0)
        self.v0.fill(0.0)
        self.dens.fill(0.0)
        self.dens0.fill(0.0)
        H, W = self.H, self.W
        pool = int(H * 0.22)
        # wavy bottom edge so the pour breaks up organically
        x = np.arange(W, dtype=np.float32)
        depth = pool + 2.0 + 2.5 * np.sin(2.0 * math.pi * 1.5 * x / W + 0.7)
        for j in range(W):
            self.dens[:int(depth[j]), j] = 1.0
        fade = int(H * 0.04) + 1
        for i in range(fade):
            self.dens[pool + i, :] = max(0.0, 1.0 - (i + 1) / (fade + 1))
        self.v[:pool + fade, :] = 10.0  # nudge the pool downward

    def _perturb(self, x, y, strength, radius, fx, fy, dye):
        W, H = self.W, self.H
        ci = min(max(x, 0.0), 1.0) * (H - 1)
        cj = min(max(y, 0.0), 1.0) * (W - 1)
        r = max(1.5, radius * min(W, H))
        sx = r * 0.5
        i0 = max(0, int(ci - 3 * r))
        i1 = min(H - 1, int(ci + 3 * r))
        j0 = max(0, int(cj - 3 * r))
        j1 = min(W - 1, int(cj + 3 * r))
        if i1 < i0 or j1 < j0:
            return
        ii, jj = np.meshgrid(np.arange(i0, i1 + 1),
                             np.arange(j0, j1 + 1), indexing="ij")
        d2 = (ii - ci) ** 2 + (jj - cj) ** 2
        w = np.exp(-d2 / (2.0 * sx * sx)).astype(np.float32)
        if fx == 0.0 and fy == 0.0:
            dx = (jj - cj) / (r + 1e-6)
            dy = (ii - ci) / (r + 1e-6)
            n = np.sqrt(dx * dx + dy * dy) + 1e-9
            dx = dx / n
            dy = dy / n
        else:
            n = math.hypot(fx, fy) + 1e-9
            dx = np.full(w.shape, fx / n, dtype=np.float32)
            dy = np.full(w.shape, fy / n, dtype=np.float32)
        region = (slice(i0, i1 + 1), slice(j0, j1 + 1))
        self.u[region] += strength * w * dx
        self.v[region] += strength * w * dy
        if dye:
            self.dens[region] = np.minimum(
                1.5, self.dens[region] + dye * w)

    # ── solver core (Stam, 1999) ──────────────────────────────────────

    def _set_bnd(self, b, x):
        """Enforce wall boundary conditions.  b=0 scalar, 1 x-vel, 2 y-vel."""
        if b == 0:
            x[0, :] = x[1, :]
            x[-1, :] = x[-2, :]
            x[:, 0] = x[:, 1]
            x[:, -1] = x[:, -2]
        elif b == 1:
            x[0, :] = x[1, :]
            x[-1, :] = x[-2, :]
            x[:, 0] = -x[:, 1]
            x[:, -1] = -x[:, -2]
        elif b == 2:
            x[0, :] = -x[1, :]
            x[-1, :] = -x[-2, :]
            x[:, 0] = x[:, 1]
            x[:, -1] = x[:, -2]
        x[0, 0] = 0.5 * (x[1, 0] + x[0, 1])
        x[0, -1] = 0.5 * (x[1, -1] + x[0, -2])
        x[-1, 0] = 0.5 * (x[-2, 0] + x[-1, 1])
        x[-1, -1] = 0.5 * (x[-2, -1] + x[-1, -2])

    def _lin_solve(self, b, x, x0, a, c, iters):
        """Vectorized Jacobi relaxation of (x - a*lap(x)) = x0, c = 1+4a."""
        inv_c = 1.0 / c
        for _ in range(iters):
            x[1:-1, 1:-1] = (x0[1:-1, 1:-1] + a * (
                x[2:, 1:-1] + x[:-2, 1:-1] + x[1:-1, 2:] + x[1:-1, :-2]
            )) * inv_c
            self._set_bnd(b, x)

    def _advect(self, b, d, d0, u, v, dt):
        """Semi-Lagrangian advection of d0 by velocity field (u, v)."""
        W, H = self.W, self.H
        x = self._ix - dt * u[1:-1, 1:-1]
        y = self._iy - dt * v[1:-1, 1:-1]
        x0 = np.clip(np.floor(x).astype(np.int32), 0, W - 2)
        y0 = np.clip(np.floor(y).astype(np.int32), 0, H - 2)
        x1 = x0 + 1
        y1 = y0 + 1
        sx = (x - x0).astype(np.float32)
        sy = (y - y0).astype(np.float32)
        d[1:-1, 1:-1] = (
            (1.0 - sx) * (1.0 - sy) * d0[y0, x0]
            + sx * (1.0 - sy) * d0[y0, x1]
            + (1.0 - sx) * sy * d0[y1, x0]
            + sx * sy * d0[y1, x1]
        )
        self._set_bnd(b, d)

    def _project(self):
        """Helmholtz-Hodge projection: make (u, v) divergence-free."""
        u, v = self.u, self.v
        p, div = self.p, self.div
        div[1:-1, 1:-1] = -0.5 * (
            u[1:-1, 2:] - u[1:-1, :-2] + v[2:, 1:-1] - v[:-2, 1:-1])
        p[1:-1, 1:-1] = 0.0
        self._set_bnd(0, div)
        self._set_bnd(0, p)
        self._lin_solve(0, p, div, 1.0, 4.0, self.pressure_iters)
        u[1:-1, 1:-1] -= 0.5 * (p[1:-1, 2:] - p[1:-1, :-2])
        v[1:-1, 1:-1] -= 0.5 * (p[2:, 1:-1] - p[:-2, 1:-1])
        self._set_bnd(1, u)
        self._set_bnd(2, v)

    def _vorticity(self, dt):
        """Vorticity confinement: re-inject the small-scale swirls that
        the grid numerically dissipates."""
        u, v = self.u, self.v
        eps = self.vorticity
        curl, gx, gy = self._curl, self._gx, self._gy
        curl[1:-1, 1:-1] = 0.5 * (
            (v[2:, 1:-1] - v[:-2, 1:-1]) - (u[1:-1, 2:] - u[1:-1, :-2]))
        gx[1:-1, 1:-1] = np.abs(curl[1:-1, 2:]) - np.abs(curl[1:-1, :-2])
        gy[1:-1, 1:-1] = np.abs(curl[2:, 1:-1]) - np.abs(curl[:-2, 1:-1])
        norm = np.sqrt(gx * gx + gy * gy) + 1e-6
        gx /= norm
        gy /= norm
        u += dt * eps * gy * curl
        v -= dt * eps * gx * curl

    # ── rendering ─────────────────────────────────────────────────────

    @staticmethod
    def _axis_coeffs(span, n):
        """Bilinear sample positions for resampling [0..span] to n points."""
        t = np.linspace(0.0, span, n, dtype=np.float32)
        i0 = np.floor(t).astype(np.int32)
        i1 = np.minimum(i0 + 1, int(span))
        return i0, i1, (t - i0)

    @staticmethod
    def _build_lut(background, ramp):
        lut = np.zeros((256, 3), dtype=np.uint8)
        stops = sorted(ramp)
        for i in range(256):
            t = i / 255.0
            for k in range(len(stops) - 1):
                t0, c0 = stops[k]
                t1, c1 = stops[k + 1]
                if t <= t1 or k == len(stops) - 2:
                    f = 0.0 if t1 == t0 else max(0.0, min(1.0, (t - t0) / (t1 - t0)))
                    lut[i] = (
                        int(c0[0] + (c1[0] - c0[0]) * f),
                        int(c0[1] + (c1[1] - c0[1]) * f),
                        int(c0[2] + (c1[2] - c0[2]) * f),
                    )
                    break
        lut[0] = background
        return lut


if __name__ == "__main__":
    # quick self-test: render a few frames to /tmp/fluid_selftest/
    import os
    import time

    os.makedirs("/tmp/fluid_selftest", exist_ok=True)
    sim = FluidSim()
    sim._reset_fields()
    t0 = time.perf_counter()
    for i in range(45):
        sim.step(1.0 / 30.0)
        if i % 5 == 0:
            sim.render().save(f"/tmp/fluid_selftest/f{i:02d}.png")
    dt = (time.perf_counter() - t0) / 45 * 1000.0
    print(f"avg step: {dt:.1f} ms")
