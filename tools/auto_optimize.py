"""Auto-optimize (CLI): FIND the best Control knobs and USE them, in one shot.

Runs the multi-objective search, takes the conservation-validated best config, runs
the FULL forecast with it, and writes the optimized workbook — so the pipeline tunes
its own control parameters end-to-end. Default method is `combined` (Grid + Deep), so
the chosen knobs are validated TOGETHER as a set (this avoids stacking two separately-
measured single-knob recommendations, which can interact badly).

Usage (from the repo root):
  python -m tools.auto_optimize
  python -m tools.auto_optimize --emphasis "Minimize loads" --method combined
  python -m tools.auto_optimize --input Forecast.xlsm --output optimized.xlsm
  python -m tools.auto_optimize --emphasis "Respect caps" --save-config

By default config/control.yaml is NOT touched — you get an optimized output to inspect.
Pass --save-config to also persist the winning knobs so future normal runs use them.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile

from forecast import optimize
from tools.optimize_sweep import _parse_weights
from tools.tune_sweep import _base_config


def _run_search(method, input_path, cdir, sdir, emphasis, weights, progress):
    if method == "combined":
        return optimize.deep_search_combined(
            input_path, cdir, sdir, emphasis=emphasis, weights=weights, progress=progress)
    if method == "deep":
        return optimize.coordinate_descent(
            input_path, cdir, sdir, emphasis=emphasis, weights=weights, progress=progress)
    grid = optimize.opt_grid_for(method == "quick")   # quick | full
    return optimize.sweep(input_path, cdir, sdir, grid=grid, progress=progress)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default="Forecast.xlsm",
                    help="Input forecast workbook (PR source).")
    ap.add_argument("--output", default=None,
                    help="Output path for the optimized workbook "
                         "(default: <input>_optimized.xlsm).")
    ap.add_argument("--config-template", default=None,
                    help="Optional config_template.xlsx to seed config/scenario from.")
    ap.add_argument("--emphasis", default=optimize.DEFAULT_EMPHASIS,
                    help=f"One of: {', '.join(optimize.EMPHASIS_PRESETS)}")
    ap.add_argument("--method", default="combined",
                    choices=["quick", "full", "deep", "combined"],
                    help="Search method (default: combined = Grid + Deep, validates "
                         "knob COMBINATIONS).")
    ap.add_argument("--weights", default=None,
                    help="Custom weights 'comp=val,...' (overrides --emphasis).")
    ap.add_argument("--save-config", action="store_true",
                    help="Also merge the winning knobs into config/control.yaml so "
                         "future normal runs use them.")
    args = ap.parse_args(argv)

    if not os.path.exists(args.input):
        sys.exit(f"input workbook not found: {args.input}")

    seed = tempfile.mkdtemp()
    cdir0, sdir0 = _base_config(args, seed)
    weights = _parse_weights(args.weights)
    emphasis = args.emphasis

    def progress(i, n, label):
        tag = f"[{i}/{n}]" if n else f"[{i}]"
        print(f"  {tag} {label} ...", file=sys.stderr, flush=True)

    print(f"input={args.input}  method={args.method}  "
          f"emphasis={'custom' if weights else emphasis}", flush=True)
    print("FINDING best knobs ...", flush=True)
    results = _run_search(args.method, args.input, cdir0, sdir0, emphasis, weights, progress)
    rec = optimize.recommend(results, emphasis=emphasis, weights=weights)
    best = next((v for v in results if v.label == rec.best_label), None)

    print(f"\n{rec.text}")
    print("\nWinning knobs (validated together):")
    for line in optimize.overrides_yaml(best.overrides if best else {}).splitlines():
        print(f"  {line}")

    # USE them: run the full forecast with the winning config.
    print("\nUSING them — running the full forecast ...", flush=True)
    out = optimize.run_full_forecast(args.input, cdir0, sdir0, best.overrides if best else {})
    dest = args.output or (os.path.splitext(args.input)[0] + "_optimized.xlsm")
    shutil.copy(out, dest)
    print(f"optimized forecast written: {dest}")

    if args.save_config:
        optimize.save_overrides_to_config("config", best.overrides if best else {})
        print("saved the winning knobs into config/control.yaml "
              "(future normal runs now use them)")
    else:
        print("(config/control.yaml NOT changed — pass --save-config to persist)")


if __name__ == "__main__":
    main()
