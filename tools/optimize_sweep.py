"""Multi-objective optimizer sweep (CLI) — thin wrapper over forecast.optimize.

Sweeps Control knobs and ranks variants on a SELECTABLE weighted objective
(walk the line: biomass + harvest near their limits and flat, feed + handling
down), gated on conservation. The same forecast.optimize.sweep() powers the app's
"Optimize" window.

Usage (from the repo root):
  python -m tools.optimize_sweep
  python -m tools.optimize_sweep --emphasis "Minimize handling" --quick
  python -m tools.optimize_sweep --weights biomass_var=3,harvest_var=3,feed_load=1
  python -m tools.optimize_sweep --config-template "C:\\path\\config_template (7).xlsx"
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

from forecast import optimize
from tools.tune_sweep import _base_config


def _parse_weights(s):
    if not s:
        return None
    out = {}
    for part in s.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = float(v)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default="Forecast.xlsm")
    ap.add_argument("--config-template", default=None,
                    help="Optional config_template.xlsx to seed config/scenario from.")
    ap.add_argument("--emphasis", default=optimize.DEFAULT_EMPHASIS,
                    help=f"One of: {', '.join(optimize.EMPHASIS_PRESETS)}")
    ap.add_argument("--weights", default=None,
                    help="Custom weights 'comp=val,comp=val' (overrides --emphasis).")
    ap.add_argument("--quick", action="store_true",
                    help="Quick sweep (4 variants) instead of full (~11).")
    args = ap.parse_args(argv)

    if not os.path.exists(args.input):
        sys.exit(f"input workbook not found: {args.input}")

    seed = tempfile.mkdtemp()
    cdir0, sdir0 = _base_config(args, seed)
    grid = optimize.opt_grid_for(args.quick)
    weights = _parse_weights(args.weights)
    emphasis = "custom" if weights else args.emphasis

    print(f"input={args.input}  emphasis={emphasis}  "
          f"sweep={'quick' if args.quick else 'full'} ({len(grid)} runs)")

    def progress(i, n, label):
        print(f"  [{i+1}/{n}] running {label} ...", file=sys.stderr, flush=True)

    results = optimize.sweep(args.input, cdir0, sdir0, grid=grid, progress=progress)
    rec = optimize.recommend(results, emphasis=args.emphasis, weights=weights)

    print(f"\n{'variant':<22} {'score':>7} {'bover':>6} {'bvar':>6} {'ugap':>6} "
          f"{'hvar':>6} {'hover':>6} {'feed':>8} {'tpf':>5} {'wk>cap':>6} | cons")
    print("-" * 104)
    for v in sorted(results, key=lambda v: (not v.conservation_ok, v.score)):
        m = v.metrics
        cons = "OK" if v.conservation_ok else f"FAIL d={v.dropped} o={v.overprod}"
        mark = " *" if v.label == rec.best_label else "  "
        print(f"{v.label:<20}{mark} {v.score:>7.3f} {m.biomass_overshoot:>6.3f} "
              f"{m.biomass_var:>6.3f} {m.biomass_util_gap:>6.3f} {m.harvest_var:>6.3f} "
              f"{m.harvest_overshoot:>6.3f} {m.feed_load:>8,.0f} {m.transfers_per_fish:>5.2f} "
              f"{m.weeks_over_harvest_cap:>6} | {cons}")
    print(f"\n{rec.text}")


if __name__ == "__main__":
    main()
