"""Controller tuning sweep (CLI) — thin wrapper over forecast.tuning.

Runs the forecast across a grid of Control knob values and prints, per variant,
the PER-BATCH PEAK-DENSITY DISTRIBUTION + conservation. Pick the row that
minimises severe (>1.3x) while conservation holds; if none beats baseline, the
severe peaks are a CAPACITY collision, not a tuning problem (USER_GUIDE sec 7.1).

The same forecast.tuning.sweep() powers the app's "Tune" window.

Usage (from the repo root):
  python -m tools.tune_sweep                         # repo config/ + scenario/ yaml
  python -m tools.tune_sweep --config-template "C:\\path\\config_template (7).xlsx"
  python -m tools.tune_sweep --input "C:\\path\\Forecast.xlsm"
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile

import openpyxl

from forecast import tuning


def _base_config(args, work):
    """Seed a (config_dir, scenario_dir) from a config_template workbook or the
    repo's config/ + scenario/ yaml."""
    cdir = os.path.join(work, "config")
    sdir = os.path.join(work, "scenario")
    os.makedirs(cdir)
    os.makedirs(sdir)
    for src, dst in (("config", cdir), ("scenario", sdir)):
        for f in os.listdir(src):
            if f.endswith(".yaml"):
                shutil.copy(os.path.join(src, f), dst)
    if args.config_template:
        import forecast.config_template as ct
        wb = openpyxl.load_workbook(args.config_template, data_only=True)
        ct.import_config_template(wb, cdir, sdir)
    return cdir, sdir


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default="Forecast.xlsm",
                    help="Input forecast workbook (PR source). Default: repo Forecast.xlsm")
    ap.add_argument("--config-template", default=None,
                    help="Optional config_template.xlsx to seed config/scenario from. "
                         "If omitted, the repo config/ + scenario/ yaml are used.")
    args = ap.parse_args(argv)

    if not os.path.exists(args.input):
        sys.exit(f"input workbook not found: {args.input}")

    seed = tempfile.mkdtemp()
    cdir0, sdir0 = _base_config(args, seed)

    print(f"input={args.input}  "
          f"config={'template:'+args.config_template if args.config_template else 'repo yaml'}")
    print(f"{'variant':<18} {'OVER':>5} {'sev>1.3':>7} {'worst':>6} {'median':>6} | "
          f"{'<=1.0':>5} {'1.0-1.1':>7} {'1.1-1.3':>7} {'>1.3':>5} | cons")
    print("-" * 96)

    def progress(i, n, label):
        print(f"  [{i+1}/{n}] running {label} ...", file=sys.stderr, flush=True)

    results = tuning.sweep(args.input, cdir0, sdir0, progress=progress)
    for r in results:
        d = r.dist
        cons = "OK" if r.conservation_ok else f"FAIL d={r.dropped} o={r.overprod}"
        print(f"{r.label:<18} {d.over:>2}/{d.n:<2} {d.severe:>7} {d.worst:>6.2f} "
              f"{d.median:>6.2f} | {d.buckets['<=1.0']:>5} {d.buckets['1.0-1.1']:>7} "
              f"{d.buckets['1.1-1.3']:>7} {d.buckets['>1.3']:>5} | {cons}")

    rec = tuning.recommend(results)
    print("\n" + rec.text)


if __name__ == "__main__":
    main()
