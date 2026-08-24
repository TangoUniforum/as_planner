"""L1 ONLY, in ~2 seconds. The fast loop for Global planning questions.

    python tools/l1_probe.py --workbook <PR.xlsm>
    python tools/l1_probe.py --workbook <PR.xlsm> --sweep sixn_min_cohort_frac 0,0.05,0.15,0.25

WHY THIS EXISTS
---------------
A full global run takes 30-40 MINUTES: L1 plans, L3 solves, the tank pick
assigns physical tanks, and a workbook is written. But most planning questions
are decided entirely inside L1 -- how much is staged into 6N, how many distinct
cohorts a week creates, what the harvest envelope looks like. `plan()` alone
runs in about 2 SECONDS.

That 1000x gap is not a convenience, it is the difference between measuring and
guessing. On 2026-08-23 six consecutive fixes to the 6N depuration pipeline
failed, and the post-mortem was the same every time: the hypothesis was
reasoned rather than measured, because measuring cost 40 minutes. One of them
(-33.8% harvest, 27 empty weeks) was "validated" beforehand against the WRONG
QUANTITY -- harvested share, when the code constrained eligible mass -- and the
error was invisible until a full run. Two seconds would have caught it.

So: ask L1 first. Only spend a full run VERIFYING a candidate the fast loop has
already chosen.

WHAT IT REPORTS, and why these
------------------------------
    max_cohorts        distinct batches drawn in a purge week. Same-batch
                       cohorts SHARE a tank (operator rule 2026-08-24); two
                       batches in one week take a pair's main + sister.
    max_tanks_needed   the number to compare against the 6-TANK pool. Applies
                       the real seating rule above. Counting per COHORT instead
                       reports 8 where the pick needs 7 and the fish need 3 --
                       a metric that sends the reader chasing a capacity
                       problem that does not exist.
    harvest            total envelope fish. Any staging change must be judged
                       against this: a "fix" that relieves 6N pressure by
                       harvesting fewer fish has not fixed anything (one such
                       attempt cost 33.8% of harvest).

READ BOTH COLUMNS. Cohorts/week alone is optimised trivially by harvesting
less; harvest alone says nothing about legality.
"""
from __future__ import annotations

import argparse
import collections
import math
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import forecast.global_planner_poc as gpp                        # noqa: E402
import forecast.scenario_io as sio                               # noqa: E402
from forecast.config_io import load_config                       # noqa: E402
from tools.run_full_facility_poc import _hydrate_pr              # noqa: E402


def run_l1(workbook, config_dir, scenario_dir, **over):
    control, tables, facility = load_config(str(config_dir))
    batches = sio.load_batches(str(scenario_dir))
    og, fw, start, purge = _hydrate_pr(Path(workbook), batches)
    if start is not None:
        control.forecast_start = start
    kw = dict(inflight_og=og, record_standing=True, model_purge_hold=True,
              model_full_facility=True, fw_inflight=fw, purge_inflight=purge)
    kw.update(over)
    t0 = time.time()
    l1 = gpp.plan(batches, tables, control, facility, **kw)
    return l1, time.time() - t0, control, facility


def summarize(l1, control, facility):
    """cohorts/week + harvest + the per-cohort 6N footprint."""
    from forecast.global_planner_l3_poc import smallest_og_tank_kg
    og_ceiling = (smallest_og_tank_kg(facility)
                  * getattr(control, "harvest_tank_density_pct", 1.25))
    per_week = collections.Counter()
    harvest = 0.0
    for e in l1.envelope:
        if e.count > 0:
            harvest += e.count
            if str(e.week_label) < "2028":       # purge mode only
                per_week[e.week_label] += 1
    # Tank footprint under the SEATING RULE THE PICK ACTUALLY USES: same batch
    # shares a tank (operator, 2026-08-24), two batches in one week take a
    # pair's main + sister. Counting per COHORT instead reports 8 tanks where
    # the pick needs 7 and the fish need 3 -- a metric that would send the next
    # reader chasing a capacity problem that does not exist.
    by_week: dict = collections.defaultdict(lambda: collections.defaultdict(float))
    for r in l1.batch_standing:
        if getattr(r, "in_purge", False) and r.biomass_kg > 1e-9:
            by_week[r.week][r.batch_id] += r.biomass_kg
    need = collections.Counter()
    for wk, per_batch in by_week.items():
        if og_ceiling > 0:
            need[wk] = sum(math.ceil(kg / og_ceiling)
                           for kg in per_batch.values())
    return {
        "harvest": harvest,
        "max_cohorts": max(per_week.values()) if per_week else 0,
        "weeks_over_2": sum(1 for v in per_week.values() if v > 2),
        "weeks_over_3": sum(1 for v in per_week.values() if v > 3),
        "n_weeks": len(per_week),
        "max_tanks_needed": max(need.values()) if need else 0,
        "weeks_over_6_tanks": sum(1 for v in need.values() if v > 6),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workbook", required=True)
    ap.add_argument("--config-dir", default=str(_ROOT / "config"))
    ap.add_argument("--scenario-dir", default=str(_ROOT / "scenario"))
    ap.add_argument("--sweep", nargs=2, metavar=("KNOB", "VALUES"),
                    help="sweep one plan() kwarg, e.g. "
                         "--sweep sixn_min_cohort_frac 0,0.05,0.15")
    args = ap.parse_args()

    if not args.sweep:
        l1, dt, control, facility = run_l1(args.workbook, args.config_dir,
                                           args.scenario_dir)
        m = summarize(l1, control, facility)
        print(f"L1 in {dt:.2f}s")
        for k, v in m.items():
            print(f"  {k:>20}: {v:,.0f}" if isinstance(v, (int, float))
                  else f"  {k:>20}: {v}")
        print("  Compare max_tanks_needed against the 6-tank 6N pool.")
        print("  A week it cannot seat is harvested from production tanks"
              " instead -- UNPURGED fish, an R7 breach.")
        return 0

    knob, raw = args.sweep
    vals = [float(v) for v in raw.split(",")]
    print(f"{knob:>28}{'max/wk':>9}{'wks>2':>7}{'wks>3':>7}"
          f"{'harvest':>14}{'vs first':>10}")
    base = None
    for v in vals:
        l1, dt, control, facility = run_l1(args.workbook, args.config_dir,
                                           args.scenario_dir, **{knob: v})
        m = summarize(l1, control, facility)
        if base is None:
            base = m["harvest"]
        delta = 100.0 * (m["harvest"] - base) / base if base else 0.0
        print(f"{v:>28.3f}{m['max_cohorts']:>9}{m['weeks_over_2']:>7}"
              f"{m['weeks_over_3']:>7}{m['harvest']:>14,.0f}{delta:>9.2f}%")
    print("\n  Judge BOTH columns: fewer cohorts bought by less harvest is not")
    print("  a fix. Verify the chosen value with ONE full run before shipping.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
