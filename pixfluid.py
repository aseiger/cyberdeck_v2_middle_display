#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
pixfluid.py — fast 2D particle fluid ("pixel water") for the cyberdeck LCD.

Particle-based viscoelastic fluid after Clavet, Beaudoin & Poulin (2005),
"Particle-based Viscoelastic Fluid Simulation": pairwise viscosity +
double-density relaxation, fully vectorized in numpy (no scipy).  The spout
fires on demand via burst() (keywater: left/right keyboard halves), arcing
water into the pool; the oldest particles are recycled back to the spout, so
the scene runs forever at a fixed particle count.

The public API mirrors FluidSim (fluidsim.py) so lcdstats.py and the IPC
layer keep working unchanged:

    sim = PixFluid(target_size=(240, 320))
    sim.reset()                 # queue a fresh bottom pool
    sim.step(dt)                # real seconds; fixed substeps inside
    img = sim.render()          # PIL RGB image at target_size
    img = sim.render(bg)        # optionally composite over a background
    sim.burst(side='left', duration=0.6)  # fire the spout on that side
    sim.perturb(x=0.5, y=0.5, strength=400, radius=0.12,
                fx=0.0, fy=0.0, dye=0.5)

Units: positions and velocities are panel pixels / pixel-per-second.
perturb() strength is an impulse in px/s (a good splash is ~300-800).
"""

import math
from collections import deque

import numpy as np
from PIL import Image


class PixFluid:
    """Particle fluid with a burst spout, splash API and metaball rendering."""

    def __init__(self, target_size=(240, 320),
                 n_particles=650,
                 h=9.0,
                 gravity=1500.0,
                 k=1500.0,
                 k_near=1500.0,
                 rho0=0.7,
                 sigma=0.0,
                 beta=0.05,
                 xsph=0.08,
                 dt_sub=1.0 / 120.0,
                 max_substeps=12,
                 max_speed=700.0,
                 wall_bounce=0.2,
                 spout_mode='orbit',
                 spout_period=9.0,
                 spout_x=0.45,
                 spout_y=0.35,
                 spout_halfwidth=6.0,
                 spawn_rate=500.0,
                 spawn_speed=950.0,
                 start_pool=260,
                 kernel_radius=4.5,
                 deep=(4, 62, 175),
                 surface=(115, 210, 255)):
        self.W, self.H = int(target_size[0]), int(target_size[1])
        self.n = int(n_particles)
        self.h = float(h)
        self.gravity = float(gravity)
        self.k = float(k)
        self.k_near = float(k_near)
        self.rho0 = float(rho0)
        self.sigma = float(sigma)
        self.beta = float(beta)
        self.xsph = float(xsph)   # XSPH velocity smoothing (pool settling)
        self.dt_sub = float(dt_sub)
        self.max_substeps = int(max_substeps)
        self.max_speed = float(max_speed)
        self.wall_bounce = float(wall_bounce)
        self.spout_mode = spout_mode
        self.spout_period = max(1.0, float(spout_period))
        self.spout_x = float(spout_x)
        self.spout_y = float(spout_y)
        self.spout_halfwidth = float(spout_halfwidth)
        self.spawn_rate = float(spawn_rate)
        self.spawn_speed = float(spawn_speed)
        self.start_pool = int(start_pool)
        self.kernel_radius = float(kernel_radius)
        self.deep = np.array(deep, dtype=np.float32)
        self.surface = np.array(surface, dtype=np.float32)

        # cell grid for neighbor search
        self.nx = max(1, int(math.ceil(self.W / self.h)))
        self.ny = max(1, int(math.ceil(self.H / self.h)))
        self.n_cells = self.nx * self.ny

        # state
        self.pos = np.zeros((self.n, 2), dtype=np.float32)
        self.vel = np.zeros((self.n, 2), dtype=np.float32)
        self.prev = np.zeros((self.n, 2), dtype=np.float32)
        self.alive = np.zeros(self.n, dtype=bool)
        self._queue = deque()      # alive slots in spawn order (oldest first)
        self._dead = deque(range(self.n))
        self.flash = np.zeros((self.H, self.W), dtype=np.float32)
        self._xacc = np.zeros((2, self.n), dtype=np.float32)  # XSPH accumulator
        self._spawn_acc = 0.0
        self.spout_theta = 0.0     # orbit phase in [0,1): position on perimeter
        self._sim_t = 0.0                    # accumulated real time (burst windows)
        self._burst_until = {"left": 0.0, "right": 0.0}  # sim-time expiry per side
        self._burst_alt = False              # alternate sides when both armed
        self._acc = 0.0
        self._pending = []         # event queue: ("reset",) / ("perturb", args)

        # splat kernel: list of (dy, dx, weight) for the nonzero taps
        r = int(math.ceil(self.kernel_radius)) + 1
        taps = []
        for oy in range(-r, r + 1):
            for ox in range(-r, r + 1):
                d = math.hypot(ox, oy)
                w = (1.0 - d / (self.kernel_radius + 0.5)) ** 1.5 \
                    if d <= self.kernel_radius else 0.0
                if w > 0.0:
                    taps.append((oy, ox, float(w)))
        self._taps = taps
        # coarser taps for the speed field (soft highlight, precision not critical)
        r2 = max(1, int(math.ceil(self.kernel_radius * 0.55)))
        taps2 = []
        for oy in range(-r2, r2 + 1):
            for ox in range(-r2, r2 + 1):
                d = math.hypot(ox, oy)
                w = (1.0 - d / (self.kernel_radius * 0.55 + 0.5)) ** 1.5 \
                    if d <= self.kernel_radius * 0.55 else 0.0
                if w > 0.0:
                    taps2.append((oy, ox, float(w)))
        self._taps2 = taps2
        self._pad = r

        self._do_reset()

    # ── public API ────────────────────────────────────────────────────

    def reset(self):
        """Queue a reset: water restarts as a pool at the bottom of the screen."""
        self._pending.append(("reset",))

    def burst(self, side="left", duration=0.6):
        """Fire the spout on `side` ('left'/'right') for `duration` seconds.

        Re-arming while active extends the window (holding a key keeps the
        water flowing).  While no burst is armed the spout stays silent and
        the pool rests.
        """
        side = "left" if str(side) == "left" else "right"
        self._burst_until[side] = self._sim_t + max(0.0, float(duration))

    def perturb(self, x=0.5, y=0.5, strength=400.0, radius=0.12,
                fx=0.0, fy=0.0, dye=0.0):
        """Queue a perturbation (splash / jet), applied on the next step.

        x, y      position, normalized [0, 1] (0,0 = top-left)
        strength  impulse magnitude in px/s (300-800 is a big splash)
        radius    blob radius, fraction of the shorter panel dimension
        fx, fy    direction vector; (0, 0) = radial burst
        dye       white flash to add at the splash (0..~1.5)
        """
        self._pending.append(("perturb", (
            float(x), float(y), float(strength), float(radius),
            float(fx), float(fy), float(dye))))

    def step(self, dt):
        """Advance the simulation by dt seconds (real time)."""
        dt = min(max(float(dt), 0.0), 0.1)
        self._sim_t += dt
        self._apply_events()
        self._acc += dt
        n = min(int(self._acc / self.dt_sub), self.max_substeps)
        self._acc = min(self._acc, self.max_substeps * self.dt_sub)
        for _ in range(n):
            self._substep(self.dt_sub)

    def render(self, background=None):
        """Return the current state as a PIL RGB image at (W, H).

        If `background` (a PIL image of the same size) is given, the water
        is composited over it.
        """
        H, W = self.H, self.W
        pad = self._pad
        Fp = np.zeros((H + 2 * pad, W + 2 * pad), dtype=np.float32)
        Gp = np.zeros((H + 2 * pad, W + 2 * pad), dtype=np.float32)
        a = np.nonzero(self.alive)[0]
        if a.size:
            lo = pad
            hi_x = W + pad - 1
            hi_y = H + pad - 1
            cx = np.clip((self.pos[a, 0] + pad).astype(np.int32), lo, hi_x)
            cy = np.clip((self.pos[a, 1] + pad).astype(np.int32), lo, hi_y)
            vdt = self.vel[a] * 0.016
            sx = np.clip((self.pos[a, 0] + vdt[:, 0] + pad).astype(np.int32),
                         lo, hi_x)
            sy = np.clip((self.pos[a, 1] + vdt[:, 1] + pad).astype(np.int32),
                         lo, hi_y)
            w = np.clip(np.linalg.norm(self.vel[a], axis=1) / 700.0,
                        0.0, 1.0).astype(np.float32)
            for oy, ox, kv in self._taps:
                Fp[cy + oy, cx + ox] += kv
                Fp[sy + oy, sx + ox] += kv * 0.5
                Gp[cy + oy, cx + ox] += kv * w
            for oy, ox, kv in self._taps2:
                Gp[cy + oy, cx + ox] += kv * w * 0.5
        F = Fp[pad:pad + H, pad:pad + W]
        G = Gp[pad:pad + H, pad:pad + W]

        # Coverage is a *continuous* alpha (soft edge), not a hard threshold:
        # the boolean mask used to flip ~1,300 edge pixels in and out every
        # frame as the water sloshed, which made the whole image's brightness
        # pulse on and off with each update.  A smooth alpha keeps total
        # coverage — and hence global brightness — continuous, and
        # anti-aliases the water edge as a bonus.
        t0 = 0.30
        tspan = 0.18          # alpha ramp width above the t0 threshold
        alpha = np.clip((F - t0) / tspan, 0.0, 1.0)
        alpha = np.maximum(alpha, np.clip((self.flash - 0.03) / 0.25, 0.0, 1.0))
        alpha = np.maximum(
            alpha,
            np.clip((G - 0.5) / 0.5, 0.0, 1.0) * np.clip((F - 0.12) / 0.18, 0.0, 1.0),
        )

        depth = np.clip((F - t0) / 1.3, 0.0, 1.0)
        c = (self.surface[None, :] * (1.0 - depth)[..., None]
             + self.deep[None, :] * depth[..., None]).astype(np.float32)
        m_speed = np.clip((G - 0.5) / 0.5, 0.0, 1.0) * 0.6
        m_flash = np.clip(self.flash, 0.0, 1.0) * 0.85
        m = np.clip(np.maximum(m_speed, m_flash), 0.0, 1.0)
        c = c * (1.0 - m)[..., None] + 255.0 * m[..., None]

        if background is not None:
            base = np.asarray(background.convert("RGB"), dtype=np.float32)
            if base.shape == c.shape:
                out = alpha[..., None] * c + (1.0 - alpha)[..., None] * base
                return Image.fromarray(np.rint(out).astype(np.uint8), "RGB")
        out = (alpha[..., None] * c
               + (1.0 - alpha)[..., None] * np.array((4, 6, 14), np.float32))
        return Image.fromarray(np.rint(out).astype(np.uint8), "RGB")

    # ── event queue ───────────────────────────────────────────────────

    def _apply_events(self):
        if not self._pending:
            return
        events = self._pending[:]
        del self._pending[:]
        for ev in events:
            if ev[0] == "reset":
                self._do_reset()
            elif ev[0] == "perturb":
                self._perturb(*ev[1])

    def _do_reset(self):
        self.vel[:] = 0.0
        self.prev[:] = 0.0
        self.flash[:] = 0.0
        self._spawn_acc = 0.0
        self._acc = 0.0
        self._burst_until["left"] = 0.0
        self._burst_until["right"] = 0.0
        self._queue.clear()
        self._dead.clear()
        self.alive[:] = False
        W, H = self.W, self.H
        n0 = min(self.start_pool, self.n)
        # pool block across the bottom; spacing ~5.4 px keeps it cohesive
        spacing = 5.4
        cols = max(1, int((W - 16) / spacing))
        rows = max(1, int(math.ceil(n0 / cols)))
        idx = np.arange(self.n)
        self._dead.extend(idx[n0:].tolist())
        for i in range(n0):
            self.alive[i] = True
            self._queue.append(i)
        x = 8.0 + (np.arange(n0) % cols) * spacing + np.random.uniform(-1, 1, n0)
        y = (H - 4.0) - (np.arange(n0) // cols) * spacing + np.random.uniform(-0.5, 0.5, n0)
        self.pos[:n0, 0] = np.clip(x, 3, W - 3)
        self.pos[:n0, 1] = np.clip(y, 3, H - 3)

    def _spout_state(self, mode=None):
        """Spout point + inward normal for a spout mode.

        Returns (sx, sy, nvx, nvy) for 'left', 'right', 'top' or 'orbit'
        (the spout travels the panel perimeter top -> right -> bottom ->
        left, always pouring inward; on the bottom edge it sprays up as a
        fountain).  Defaults to self.spout_mode.
        """
        mode = mode or self.spout_mode
        W, H = self.W, self.H
        if mode == 'left':
            return (2.0, self.spout_y * H, 1.0, 0.0)
        if mode == 'right':
            return (W - 2.0, self.spout_y * H, -1.0, 0.0)
        if mode == 'top':
            return (self.spout_x * W, 2.0, 0.0, 1.0)
        P = 2.0 * (W + H)
        s = (self.spout_theta * P) % P
        if s < W:                              # top edge, moving right
            return (s, 2.0, 0.0, 1.0)
        s -= W
        if s < H:                              # right edge, moving down
            return (W - 2.0, s, -1.0, 0.0)
        s -= H
        if s < W:                              # bottom edge, moving left
            return (W - s, H - 2.0, 0.0, -1.0)
        s -= W
        return (2.0, H - s, 1.0, 0.0)          # left edge, moving up

    def _perturb(self, x, y, strength, radius, fx, fy, dye):
        x = min(max(x, 0.0), 1.0) * self.W
        y = min(max(y, 0.0), 1.0) * self.H
        r = max(8.0, radius * min(self.W, self.H) * 1.3)
        a = np.nonzero(self.alive)[0]
        if a.size == 0:
            return
        dx = self.pos[a, 0] - x
        dy = self.pos[a, 1] - y
        d2 = dx * dx + dy * dy
        sig2 = 2.0 * (r * 0.55) ** 2
        w = np.exp(-d2 / sig2)
        sel = w > 0.02
        a = a[sel]
        if a.size == 0:
            return
        w = w[sel]
        dx = dx[sel]
        dy = dy[sel]
        if fx == 0.0 and fy == 0.0:
            nrm = np.sqrt(dx * dx + dy * dy) + 1e-6
            dx = dx / nrm
            dy = dy / nrm
        else:
            nrm = math.hypot(fx, fy) + 1e-9
            dx = np.full(a.shape, fx / nrm, dtype=np.float32)
            dy = np.full(a.shape, fy / nrm, dtype=np.float32)
        self.vel[a, 0] += strength * w * dx
        self.vel[a, 1] += strength * w * dy
        if dye:
            # white flash at the splash
            px, py = int(round(x)), int(round(y))
            ext = int(math.ceil(r * 1.5))
            y0, y1 = max(0, py - ext), min(self.H, py + ext)
            x0, x1 = max(0, px - ext), min(self.W, px + ext)
            if y1 > y0 and x1 > x0:
                gy, gx = np.mgrid[y0:y1, x0:x1]
                gd2 = (gx - x) ** 2 + (gy - y) ** 2
                blob = dye * np.exp(-gd2 / (2.0 * (r * 0.6) ** 2))
                self.flash[y0:y1, x0:x1] = np.maximum(
                    self.flash[y0:y1, x0:x1], blob.astype(np.float32))

    # ── per-substep physics ───────────────────────────────────────────

    def _substep(self, dt):
        a = np.nonzero(self.alive)[0]
        if a.size == 0:
            return
        pos, vel = self.pos, self.vel

        # gravity
        vel[a, 1] += self.gravity * dt

        # advect
        self.prev[a] = pos[a]
        pos[a] += vel[a] * dt

        # neighbors (built once, shared by viscosity and relaxation)
        pairs = self._build_pairs(a)
        if pairs is not None:
            pi, pj, q, ux, uy = pairs

            # pairwise viscosity (approaching particles resist)
            if self.sigma or self.beta:
                u = (vel[pi, 0] - vel[pj, 0]) * ux \
                    + (vel[pi, 1] - vel[pj, 1]) * uy
                m = u > 0.0
                if m.any():
                    I = dt * q[m] * (self.sigma * u[m]
                                    + self.beta * u[m] * u[m]) * 0.5
                    ix = I * ux[m]
                    iy = I * uy[m]
                    np.add.at(vel[:, 0], pi[m], -ix)
                    np.add.at(vel[:, 1], pi[m], -iy)
                    np.add.at(vel[:, 0], pj[m], ix)
                    np.add.at(vel[:, 1], pj[m], iy)

            # double-density relaxation
            rho = np.zeros(self.n, dtype=np.float32)
            rho_n = np.zeros(self.n, dtype=np.float32)
            q2 = q * q
            q3 = q2 * q
            np.add.at(rho, pi, q2)
            np.add.at(rho, pj, q2)
            np.add.at(rho_n, pi, q3)
            np.add.at(rho_n, pj, q3)
            P = self.k * (rho - self.rho0)
            Pn = self.k_near * rho_n
            D = dt * dt * ((P[pi] + P[pj]) * q
                           + (Pn[pi] + Pn[pj]) * q2)
            dx = D * ux * 0.5
            dy = D * uy * 0.5
            pos[pi, 0] -= dx
            pos[pi, 1] -= dy
            pos[pj, 0] += dx
            pos[pj, 1] += dy

        # velocity from position change
        vel[a] = (pos[a] - self.prev[a]) / dt

        # XSPH velocity smoothing: each particle's velocity is pulled toward
        # its neighbors' average, which damps relative motion.  This kills the
        # micro-jitter that keeps a resting pool "boiling" without affecting
        # coherent motion (a falling jet moves together, so it barely slows).
        if self.xsph and pairs is not None:
            ax = self._xacc[0]
            ay = self._xacc[1]
            ax[a] = 0.0
            ay[a] = 0.0
            dvx = q * (vel[pj, 0] - vel[pi, 0])
            dvy = q * (vel[pj, 1] - vel[pi, 1])
            np.add.at(ax, pi, dvx)
            np.add.at(ay, pi, dvy)
            np.add.at(ax, pj, -dvx)
            np.add.at(ay, pj, -dvy)
            vel[a, 0] += self.xsph * ax[a]
            vel[a, 1] += self.xsph * ay[a]

        # speed clamp
        sp = np.linalg.norm(vel[a], axis=1)
        m = sp > self.max_speed
        if m.any():
            vel[a[m]] *= (self.max_speed / sp[m])[:, None]

        # walls (small restitution)
        m = pos[a, 0] < 3.0
        pos[a[m], 0] = 3.0
        vel[a[m], 0] = np.abs(vel[a[m], 0]) * self.wall_bounce
        m = pos[a, 0] > self.W - 3.0
        pos[a[m], 0] = self.W - 3.0
        vel[a[m], 0] = -np.abs(vel[a[m], 0]) * self.wall_bounce
        m = pos[a, 1] < 3.0
        pos[a[m], 1] = 3.0
        vel[a[m], 1] = np.abs(vel[a[m], 1]) * self.wall_bounce
        m = pos[a, 1] > self.H - 3.0
        pos[a[m], 1] = self.H - 3.0
        vel[a[m], 1] = -np.abs(vel[a[m], 1]) * self.wall_bounce

        # spout fires only while a burst window is armed (keywater / IPC)
        left_on = self._sim_t < self._burst_until["left"]
        right_on = self._sim_t < self._burst_until["right"]
        if left_on or right_on:
            self.spout_theta = (self.spout_theta + self.dt_sub / self.spout_period) % 1.0
            self._spawn_acc += self.spawn_rate * dt
            n_spawn = int(min(self._spawn_acc, 12))
            self._spawn_acc -= n_spawn
            if n_spawn:
                for _ in range(n_spawn):
                    if left_on and right_on:
                        self._burst_alt = not self._burst_alt
                        mode = "right" if self._burst_alt else "left"
                    else:
                        mode = "left" if left_on else "right"
                    sx, sy, nvx, nvy = self._spout_state(mode)
                    tx, ty = nvy, -nvx       # tangent = direction of travel
                    if len(self._dead) > 0:
                        idx = self._dead.popleft()
                        self.alive[idx] = True
                    else:
                        idx = self._queue.popleft()
                    self._queue.append(idx)
                    off = np.random.uniform(-self.spout_halfwidth,
                                            self.spout_halfwidth)
                    self.pos[idx, 0] = sx + tx * off
                    self.pos[idx, 1] = sy + ty * off
                    jet = self.spawn_speed * np.random.uniform(0.85, 1.15)
                    tang = np.random.uniform(-40.0, 40.0)
                    self.vel[idx, 0] = nvx * jet + tx * tang
                    self.vel[idx, 1] = nvy * jet + ty * tang
                    self.prev[idx] = self.pos[idx]

        # flash decay
        self.flash *= 0.87

    # ── neighbor search (uniform grid, fully vectorized) ──────────────

    def _build_pairs(self, a):
        """Return (pi, pj, q, ux, uy) for unique pairs with r < h, or None.

        pi/pj index into the full (n,) particle arrays. ux/uy is the unit
        vector from i toward j.
        """
        W, H = self.W, self.H
        h = self.h
        nx, ny, n_cells = self.nx, self.ny, self.n_cells
        x = self.pos[a, 0]
        y = self.pos[a, 1]
        cx = np.clip((x / h).astype(np.int32), 0, nx - 1)
        cy = np.clip((y / h).astype(np.int32), 0, ny - 1)
        cid = (cy * nx + cx).astype(np.int64)
        order = np.argsort(cid, kind="stable")
        counts = np.bincount(cid, minlength=n_cells)
        offsets = np.concatenate(([0], np.cumsum(counts))).astype(np.int64)

        pi_list = []
        pj_list = []

        # same-cell pairs (i < j)
        sizes = counts * counts
        total = int(sizes.sum())
        if total:
            c_all = np.arange(n_cells)
            starts = np.repeat(np.cumsum(sizes) - sizes, sizes)
            t = np.arange(total) - starts
            ma = np.repeat(counts, sizes)
            i0 = np.repeat(offsets[:-1], sizes)
            pi = i0 + (t // ma)
            pj = i0 + (t % ma)
            keep = pi < pj
            pi_list.append(pi[keep])
            pj_list.append(pj[keep])

        # neighboring-cell pairs, half-neighborhood (each pair once)
        for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
            c_all = np.arange(n_cells)
            bx = (c_all % nx) + dx
            by = (c_all // nx) + dy
            ok = (bx < nx) & (by < ny) & (counts > 0)
            b = np.clip((by * nx + bx).astype(np.int64), 0, n_cells - 1)
            ok &= (counts[b] > 0)
            ca = counts[c_all[ok]]
            cb = counts[b[ok]]
            sizes = ca * cb
            total = int(sizes.sum())
            if total == 0:
                continue
            starts = np.repeat(np.cumsum(sizes) - sizes, sizes)
            t = np.arange(total) - starts
            i0 = np.repeat(offsets[c_all[ok]], sizes)
            j0 = np.repeat(offsets[b[ok]], sizes)
            mb = np.repeat(cb, sizes)
            pi_list.append(i0 + (t // mb))
            pj_list.append(j0 + (t % mb))

        if not pi_list:
            return None
        pi = np.concatenate(pi_list).astype(np.int64)
        pj = np.concatenate(pj_list).astype(np.int64)
        # pi/pj are positions in the sorted `order` array; map to particle ids
        pi = order[pi]
        pj = order[pj]

        dxi = self.pos[pj, 0] - self.pos[pi, 0]
        dyi = self.pos[pj, 1] - self.pos[pi, 1]
        r2 = dxi * dxi + dyi * dyi
        m = r2 < h * h
        pi, pj = pi[m], pj[m]
        if pi.size == 0:
            return None
        r = np.sqrt(r2[m])
        inv = np.where(r > 1e-6, 1.0 / np.maximum(r, 1e-6), 0.0)
        ux = dxi[m] * inv
        uy = dyi[m] * inv
        q = 1.0 - r / h
        return pi, pj, q, ux, uy




if __name__ == "__main__":
    # quick self-test: render a few frames to /tmp/pixfluid_selftest/
    import os
    import time

    os.makedirs("/tmp/pixfluid_selftest", exist_ok=True)
    sim = PixFluid()
    t0 = time.perf_counter()
    for i in range(90):
        sim.step(1.0 / 30.0)
        if i % 6 == 0:
            sim.render().save(f"/tmp/pixfluid_selftest/f{i:02d}.png")
    wall = (time.perf_counter() - t0) / 90 * 1000.0
    print(f"avg frame (step+render): {wall:.1f} ms")
