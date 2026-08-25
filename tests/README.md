# tests/

Manual debug & tuning probes from the display bring‑up session. These are **not**
a pytest suite — they're standalone scripts that drive the real hardware (SPI
panel, backlight) or run the fluid sim headless. Run them by hand when you're
chasing a specific display/fluid issue.

Each script has a small `sys.path` bootstrap at the top so it can import the
project modules (`lib`, `fluidsim`, `pixfluid`) even though it lives in this
subfolder.

## Run

From the project root:

```bash
python tests/<name>.py
```

## SPI / backlight probes (drive the real 2.4" ST7789 panel)

| Script | Purpose |
| --- | --- |
| `black_loop_test.py` | Continuously rewrite a PURE‑BLACK full frame. Distinguishes backlight dimming (rail sag) from a panel/GRAM artifact. |
| `write_loop_test.py` | General full‑frame write‑loop probe at a target SPI rate. |
| `fluid_sweep_test.py` | Sweep the real fluid view at increasing SPI rates to find the corruption cliff. |
| `static_test.py` | Push ONE static frame, then drive the backlight as a plain digital HIGH (PWM off) to isolate electrical flicker. |

## Fluid‑sim tuning (headless, uses `fluidsim.FluidSim`)

| Script | Purpose |
| --- | --- |
| `dyn_tune.py` | Dynamic steady‑state tuning: more swirl, sharper filaments, coherent jet. |
| `final_tune.py` | Pick the best source shape, validate over 3 s + pokes. |
| `probe_buoy.py` | Buoyancy probe: does heavy‑dye sinking pool cleanly? |
| `probe_pool.py` | How bright the bottom pool stays over time, across variants. |
| `probe_steady.py` | Steady‑state probe: run variants for 3 s, save the fall sequence. |
| `ramp_tune.py` | Color‑ramp tuning: same dynamics, different LUT shapes. |
| `tune_fluid.py` | Parameter sweep: render a fixed‑time fall sequence for comparison. |

## Headless render test

| Script | Purpose |
| --- | --- |
| `test_fluid.py` | Headless test for fluidsim: renders frames to `/tmp/fluid_test/`, exercises `perturb()`/`reset()`, benchmarks `step()`/`render()`. |

> Heads‑up: the SPI/backlight probes write to the physical panel and touch the
> backlight pin. Don't run them blindly while the `lcdstats` service is the
> thing you want on screen.
