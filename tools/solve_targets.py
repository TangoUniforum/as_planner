"""Close the loop: adjust the weekly harvest band until the plan hits the targets.

    python -m tools.solve_targets --pr <PR.xlsx> [--iters 6] [--tolerance 5]

WHY AN ITERATIVE SOLVER AND NOT ARITHMETIC
------------------------------------------
The first attempt computed each capped month's band as `target / weeks` in one
shot. It moved the plan in the right direction -- December 376 t -> 587 t purely
by capping September and October -- but it overshot badly: October landed 499 t
against a 600 t target, because a FLAT cap does not distinguish "trim 84 t" from
"clip every week to the average".

The plan also REACTS. Deferred fish grow, change which tanks are free, and shift
later months. A one-shot calculation cannot see any of that, so the only honest
way to land inside tolerance is to run, measure, adjust, and run again.

    A run is ~30 s. Six iterations is three minutes. That is affordable, and it
    is the difference between "the plan moved" and "the plan hits the number".

THE THREE RULES THIS ENCODES
----------------------------
1. CAP, DO NOT FLOOR. Capping a fat month defers fish that already exist.
   Raising a floor cannot create fish -- on the 8.23.26 PR December is short
   while 400,000+ fish sit just under the 3,500 g minimum harvest weight -- and
   `min_harvest_per_week` is the sales contract the whole checklist protects.
   Writing an unreachable floor into it is writing a promise you cannot keep.

2. KEEP THE WEEK-TO-WEEK SHAPE. A flat monthly cap is unrealistic: harvest is
   never identical week to week. Each month's cap is distributed across its
   weeks in proportion to the plan's OWN weekly harvest, so the natural rhythm
   survives and only the level changes.

3. KEEP THE BEST, NOT THE LAST. This planner is chaos-sensitive -- a knob that
   should barely matter moves the worst harvest week by 8,629 fish -- so a later
   iteration is not automatically better. Every iteration is scored and the best
   one is returned, with the whole history so a human can see whether it
   converged or wandered.

NOTHING IS WRITTEN. The solver returns the bands it found; applying them is the
caller's decision, and the app makes it an explicit button press.
"""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forecast import analysis as _ana                            # noqa: E402
from forecast import harvest_plan as _hp                         # noqa: E402


def deviation(monthly_kg: dict, targets: dict) -> tuple[float, float, dict]:
    """(total abs deviation kg, worst |pct|, per-month pct) against the targets.

    Only months that HAVE a target count. Scoring a month nobody asked about
    would make the solver chase noise.
    """
    per = {}
    total = 0.0
    worst = 0.0
    for m, tgt in (targets or {}).items():
        if not tgt:
            continue
        act = float(monthly_kg.get(m, 0.0))
        total += abs(act - tgt)
        pct = (act - tgt) / tgt * 100.0
        per[m] = pct
        worst = max(worst, abs(pct))
    return total, worst, per


def weekly_shape(rows: list[dict], weeks: tuple) -> dict:
    """Each week's share of its month's harvest COUNT, from the plan itself.

    Falls back to an even split when the month harvested nothing (a shape
    cannot be inferred from zero), which is the only case where a flat band is
    the honest choice.
    """
    counts = {w: 0.0 for w in weeks}
    for r in rows:
        w = r.get("week")
        if w in counts:
            counts[w] += float(r.get("count") or 0.0)
    tot = sum(counts.values())
    if tot <= 0:
        return {w: 1.0 / len(weeks) for w in weeks} if weeks else {}
    return {w: c / tot for w, c in counts.items()}


def propose_bands(rows_h, monthly_kg, targets, weeks_by_month, avg_fish_kg,
                  current: dict, tolerance_pct: float, gain: float = 1.0):
    """One correction step -> {(week, metric): fish} for the capped months.

    Proportional control on the OBSERVED gap, not on the target alone: if the
    month came in 10% over, tighten its cap by ~10%. `gain` below 1 damps the
    step, which matters because the plan over-responds (a cap on one month also
    changes the months after it).
    """
    out = dict(current)
    touched = []
    # STEP LIMIT. The first version was bang-bang: cap a month that is over,
    # REMOVE the cap entirely when it comes under. That oscillated -- measured
    # 481 -> 295 -> 475 -> 295 t, flip-flopping between two states forever,
    # because removing a cap sends the month straight back over. A cap is now
    # RELAXED proportionally instead of deleted, and every step is clamped, so
    # the loop can settle instead of ringing.
    MAX_STEP = 0.25
    for m, tgt in (targets or {}).items():
        if not tgt:
            continue
        wks = tuple(weeks_by_month.get(m, ()))
        if not wks:
            continue
        act = float(monthly_kg.get(m, 0.0))
        err = (act - tgt) / tgt                       # + over, - under
        if abs(err) * 100.0 <= tolerance_pct:
            continue                                  # inside tolerance: leave it
        shape = weekly_shape(rows_h, wks)
        month_fish = tgt / avg_fish_kg
        capped_now = any((w, _hp.METRIC_MAX) in current for w in wks)
        if err < 0 and not capped_now:
            # Under target with no cap of ours to relax. Capping cannot help and
            # a floor cannot create fish -- this month is filled by deferrals
            # from the fat month ahead of it, or not at all.
            continue
        step = max(-MAX_STEP, min(MAX_STEP, gain * err))
        for w in wks:
            prev = current.get((w, _hp.METRIC_MAX))
            base = prev if prev else month_fish * shape.get(w, 1.0 / len(wks))
            out[(w, _hp.METRIC_MAX)] = max(1000.0, round(base * (1.0 - step)))
        touched.append(m)
    return out, touched


def solve(pr_path, config_dir, scenario_dir, out_dir, *, iters=6,
          tolerance_pct=5.0, gain=1.0, progress=None):
    """Run -> measure -> adjust, `iters` times. Returns the BEST attempt.

    Each iteration writes the candidate bands into a TEMP scenario copy, never
    the caller's: a solver that mutated the operator's config while searching
    would leave it holding whichever bands the last iteration happened to try.
    """
    import os
    import shutil
    import tempfile
    from forecast import methods as _methods
    from forecast.config_io import load_control
    from forecast.scenario_io import (load_limits, load_batches, dump_scenario)

    targets = (_ana.load_targets(config_dir) or {})
    tmonthly = targets.get("monthly") or {}
    if not tmonthly:
        return {"error": "no monthly targets set — nothing to solve for"}
    basis = targets.get("basis", "hog")

    ctl = load_control(config_dir)
    fl0, sl0 = load_limits(scenario_dir, ctl)
    base_overrides = dict(fl0.overrides)
    ctl_method = _methods.REGISTRY["controller"]

    os.makedirs(out_dir, exist_ok=True)
    history, best = [], None
    overrides = dict(base_overrides)

    for i in range(iters):
        work = tempfile.mkdtemp(prefix="as_solve_")
        try:
            sdir = os.path.join(work, "scenario")
            shutil.copytree(scenario_dir, sdir)
            fl, sl = load_limits(sdir, ctl)
            fl.overrides = dict(overrides)
            dump_scenario(sdir, batches=load_batches(sdir),
                          facility_limits=fl, system_limits=sl)
            wb = os.path.join(out_dir, "solve_%d.xlsm" % i)
            rc, _el = _methods.run_method(ctl_method, pr_path, wb,
                                          config_dir, sdir)
            if rc != 0:
                history.append({"iter": i, "error": "engine rc=%d" % rc})
                break
            rows_h = _ana.harvest_rows(wb)
            monthly, _y = _ana.harvest_by_period(rows_h, basis=basis)
            total, worst, per = deviation(monthly, tmonthly)
            hv = None
            try:
                from tools.run_tuned_tournament import _grade
                from forecast.config_io import control_to_dict
                g = _grade(wb, control_to_dict(ctl), targets,
                           ctl_method.engine)
                hv = g["harvest"]
            except Exception:                                    # noqa: BLE001
                pass
            rec = {"iter": i, "total_dev_kg": total, "worst_pct": worst,
                   "per_month_pct": per, "workbook": wb,
                   "overrides": dict(overrides), "monthly_kg": dict(monthly),
                   "weeks_below_floor": (hv or {}).get("weeks_below_min"),
                   "min_week": (hv or {}).get("min_week"),
                   "n_capped_weeks": sum(
                       1 for k in overrides if k[1] == _hp.METRIC_MAX)}
            history.append(rec)
            if progress:
                progress(i, iters, rec)
            # KEEP THE BEST, not the last: this planner is chaos-sensitive, so a
            # later iteration is not automatically an improvement.
            if best is None or total < best["total_dev_kg"]:
                best = rec
            if worst <= tolerance_pct:
                break
            weeks_by_month = {}
            for w in sorted({r.get("week") for r in rows_h if r.get("week")}):
                m = _hp.month_of(w)
                if m:
                    weeks_by_month.setdefault(m, []).append(w)
            tot_kg = sum(float(r.get("hog_kg") or 0) for r in rows_h)
            tot_f = sum(float(r.get("count") or 0) for r in rows_h)
            avg = (tot_kg / tot_f) if tot_f else 0.0
            if avg <= 0:
                break
            overrides, _touched = propose_bands(
                rows_h, monthly, tmonthly, weeks_by_month, avg, overrides,
                tolerance_pct, gain)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return {"best": best, "history": history, "targets": tmonthly,
            "base_overrides": base_overrides}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pr", required=True)
    ap.add_argument("--config-dir", default=str(ROOT / "config"))
    ap.add_argument("--scenario-dir", default=str(ROOT / "scenario"))
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--iters", type=int, default=6)
    ap.add_argument("--tolerance", type=float, default=None)
    ap.add_argument("--gain", type=float, default=1.0)
    a = ap.parse_args(argv)
    tol = a.tolerance
    if tol is None:
        tol = float((_ana.load_targets(a.config_dir) or {}).get(
            "tolerance_pct", 5.0))

    def _p(i, n, rec):
        print("  iter %d/%d  total dev %8.1f t  worst %6.1f%%  "
              "floor<%s  min_wk %s" % (
                  i + 1, n, rec["total_dev_kg"] / 1000.0, rec["worst_pct"],
                  rec.get("weeks_below_floor"),
                  format(int(rec.get("min_week") or 0), ",")), flush=True)

    res = solve(a.pr, a.config_dir, a.scenario_dir, a.out_dir,
                iters=a.iters, tolerance_pct=tol, gain=a.gain, progress=_p)
    if res.get("error"):
        print(res["error"])
        return 1
    b = res["best"]
    print("\nBEST: iteration %d — total deviation %.1f t, worst %.1f%%"
          % (b["iter"] + 1, b["total_dev_kg"] / 1000.0, b["worst_pct"]))
    for m, pct in sorted(b["per_month_pct"].items()):
        print("   %s  %8.1f t vs %8.1f t target  %+6.1f%%"
              % (m, b["monthly_kg"].get(m, 0) / 1000.0,
                 res["targets"][m] / 1000.0, pct))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
