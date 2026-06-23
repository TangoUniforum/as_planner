"""Verification runner: L1 instant-removal (PRE-FIX) vs L1 6N purge-hold (POST-FIX).

METHOD: GLOBAL (tankless L1 POC) — 6N flow-to-harvest correctness check

Loads repo config/ + scenario/ + PR-hydrated in-flight OG (same inputs as
tools.run_loop_poc), runs forecast.global_planner_poc.plan() twice:

  * PRE-FIX  : model_purge_hold=False — harvest removed instantly (the old POC).
  * POST-FIX : model_purge_hold=True  — harvest-bound fish enter a 2-week OFF-FEED
               6N purge hold (mirrors the production placement STARVE/move-in
               flow + forecast.sixn mode resolution) before leaving round-robin.

Prints the before/after standing-biomass + feed + 6N-occupancy delta and the
per-batch conservation for both. ADDITIVE; touches no production file.

Usage:
    python -m tools.run_purge_compare_poc
    python -m tools.run_purge_compare_poc --no-pr
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from forecast.config_io import load_config
from forecast.scenario_io import load_batches
from forecast import global_planner_poc as gpp
from tools.run_global_poc import _hydrate_inflight_og


def _summ(res, cap_bio, cap_feed):
    peak_bio = max((r.standing_biomass_kg for r in res.trace), default=0.0)
    mean_bio = (sum(r.standing_biomass_kg for r in res.trace) / len(res.trace)
                if res.trace else 0.0)
    peak_feed = max((r.feed_kg_day for r in res.trace), default=0.0)
    mean_feed = (sum(r.feed_kg_day for r in res.trace) / len(res.trace)
                 if res.trace else 0.0)
    hog = sum(r.harvested_kg for r in res.trace)
    worst = max((abs(c["residual_pct"]) for c in res.conservation.values()),
                default=0.0)
    return dict(peak_bio=peak_bio, mean_bio=mean_bio, peak_feed=peak_feed,
                mean_feed=mean_feed, hog=hog, worst_resid=worst)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config-dir", default=str(_ROOT / "config"))
    ap.add_argument("--scenario-dir", default=str(_ROOT / "scenario"))
    ap.add_argument("--workbook", default=str(_ROOT / "Forecast.xlsm"))
    ap.add_argument("--no-pr", action="store_true")
    ap.add_argument("--horizon", type=int, default=None,
                    help="override horizon_weeks (e.g. 120 to cross the 2028 "
                         "6N production transition)")
    args = ap.parse_args()

    control, tables, facility = load_config(args.config_dir)
    if args.horizon:
        control.horizon_weeks = args.horizon
    batches = load_batches(args.scenario_dir)
    inflight = {}
    if not args.no_pr:
        inflight, derived_start = _hydrate_inflight_og(Path(args.workbook), batches)
        if derived_start is not None:
            control.forecast_start = derived_start

    fs = control.forecast_start
    fs_date = fs.date() if hasattr(fs, "date") else fs
    print("=" * 72)
    print("  6N FLOW-TO-HARVEST: PRE-FIX (instant) vs POST-FIX (purge hold)")
    print("=" * 72)
    print(f"  forecast_start={fs_date}, horizon={control.horizon_weeks}w, "
          f"in-flight OG batches={len(inflight)}")
    psd = getattr(control, "sixn_production_start", None)
    print(f"  sixn_production_start={psd.date() if hasattr(psd,'date') else psd}, "
          f"sixn_transition_weeks={control.sixn_transition_weeks}, "
          f"starvation_period_days={control.starvation_period_days}")

    pre = gpp.plan(batches, tables, control, facility, inflight_og=inflight,
                   record_standing=True, model_purge_hold=False)
    post = gpp.plan(batches, tables, control, facility, inflight_og=inflight,
                    record_standing=True, model_purge_hold=True)

    cap_b, cap_f = control.max_biomass_kg, control.max_feed_per_day_kg
    sp, so = _summ(pre, cap_b, cap_f), _summ(post, cap_b, cap_f)

    def _row(name, a, b, unit=""):
        d = b - a
        print(f"  {name:<28} {a:>14,.0f} {b:>14,.0f} {d:>+14,.0f} {unit}")

    print(f"\n  {'metric':<28} {'PRE (instant)':>14} {'POST (hold)':>14} "
          f"{'delta':>14}")
    print(f"  {'-'*28} {'-'*14} {'-'*14} {'-'*14}")
    _row("peak standing biomass kg", sp["peak_bio"], so["peak_bio"])
    _row("mean standing biomass kg", sp["mean_bio"], so["mean_bio"])
    _row("peak feed kg/day", sp["peak_feed"], so["peak_feed"])
    _row("mean feed kg/day", sp["mean_feed"], so["mean_feed"])
    _row("total HOG kg", sp["hog"], so["hog"])
    print(f"  peak biomass % of cap        "
          f"{100*sp['peak_bio']/cap_b:>13.1f}% {100*so['peak_bio']/cap_b:>13.1f}%")
    print(f"  peak feed % of cap           "
          f"{100*sp['peak_feed']/cap_f:>13.1f}% {100*so['peak_feed']/cap_f:>13.1f}%")
    print(f"  worst |conservation residual| "
          f"{sp['worst_resid']:>12.4f}% {so['worst_resid']:>13.4f}%")

    # 6N purge-hold occupancy summary (POST only).
    pt = post.purge_trace
    if pt:
        modes = {}
        for r in pt:
            modes[r.mode] = modes.get(r.mode, 0) + 1
        peak_tanks = max((r.sixn_tanks_used for r in pt), default=0)
        peak_pairs = max((r.sixn_pairs_used for r in pt), default=0)
        peak_held = max((r.held_biomass_kg for r in pt), default=0.0)
        wks_held = sum(1 for r in pt if r.held_biomass_kg > 1e-6)
        print(f"\n  6N PURGE-HOLD (POST): weeks by mode = {modes}")
        print(f"    weeks with fish in 6N hold: {wks_held}/{len(pt)}")
        print(f"    peak 6N held biomass: {peak_held:,.0f} kg")
        print(f"    peak 6N tanks used:   {peak_tanks} (pairs {peak_pairs}); "
              f"6N has {gpp._N_SIXN_PAIRS} pairs / {2*gpp._N_SIXN_PAIRS} tanks")

    # in_purge batch_standing rows -> separate 6N pool demand for L3.
    n_purge_rows = sum(1 for r in post.batch_standing if r.in_purge)
    n_grow_rows = sum(1 for r in post.batch_standing if not r.in_purge)
    print(f"\n  POST batch_standing rows: {n_grow_rows} grow-out + "
          f"{n_purge_rows} in_purge (6N pool)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
