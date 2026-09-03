"""Measure the LEVELING/REPAIR trade across every rebalancer lever at once.

    python -m tools.measure_leveling --pr <PR.xlsx> --out run.jsonl --combos @grid.json

WHY THIS EXISTS
---------------
Per-system feed sits over cap in 67 of 720 system-weeks on the 8.23.26 PR and NO
controller variant in the tuned tournament clears it. The standing figure for the
fix -- "enabling the balancer halves breaches but costs $7.7M and five sub-floor
weeks" -- was measured under the OLD objective, ONE KNOB AT A TIME, before
harvest_floor_gap was scored and before metrics-v6. It cannot decide anything now.

This measures the whole lever FAMILY against the CONSTRAINTS, not the score:

    rebalance_balance_budget   the 3-dimension relief pass (0 = OFF)
    rebalance_level            hottest->coldest leveling; SHARES the budget
                               above, so budget 0 makes `level: true` INERT
    rebalance_varqty_budget    precise count off over-cap systems
    rebalance_split_budget     fan a crowded tank into free tanks
    cap_repair_budget          END-OF-WEEK repair -- the only pass that runs
                               AFTER biology, immediately before the snapshot the
                               SystemLimitsAudit measures. Every other pass aims
                               at START-of-week load while the metric reads
                               END-of-week, a full week of growth later (~+7%
                               biomass, ~+11% feed).

Operator inputs are never swept (methods.UNTUNABLE_KNOBS): max_transfers_per_week,
density_target_pct, min_harvest_per_week, tran_og_default_tanks. Where the handling
budget BINDS a pass we report it as a diagnosis, not as a lever to move.

READING IT
----------
Ranked on CONSTRAINTS first (feed breaches, floor weeks, hard gates), with score
reported alongside and never used to overrule them. A plan that clears the feed
gate and holds the contract floor beats a better-scoring plan that does not.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml                                                       # noqa: E402

from forecast import analysis as _ana                             # noqa: E402
from forecast import methods as _methods                          # noqa: E402
from forecast import optimize as _opt                             # noqa: E402
from tools.run_tuned_tournament import _grade                     # noqa: E402


def _base_cfg(config_dir: str) -> dict:
    with open(os.path.join(config_dir, "control.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def measure(pr, combos, config_dir, scenario_dir, out_dir, tag, targets=None,
            allow_operator_inputs=False):
    base = _base_cfg(config_dir)
    econ = _ana.load_economics(config_dir)
    ctl = _methods.REGISTRY["controller"]
    results = []
    for i, c in enumerate(combos):
        name = c.get("name") or ("combo%d" % i)
        ov = dict(c.get("overrides") or {})
        illegal = set(ov) & _methods.UNTUNABLE_KNOBS
        if illegal and not allow_operator_inputs:
            results.append({"name": name,
                            "error": "refuses operator inputs: %s" % sorted(illegal)})
            print("  [%s] %-34s REFUSED (operator input)" % (tag, name), flush=True)
            continue
        if illegal:
            # DELIBERATE, HUMAN-DIRECTED comparison only. UNTUNABLE_KNOBS exists
            # so a SEARCH cannot "win" by redefining the operation -- pinning
            # min_tank_control 7,000 -> 12,000 and claiming the gain. Measuring a
            # named knob's before/after on request is a different act, and for
            # some of these the app tells the operator to do exactly that
            # ("held out of every search as a safety guard, so this one you do
            # set by hand"). It stays OFF by default and says so loudly, because
            # the failure mode is someone reading a sweep that quietly moved a
            # contract and calling it an improvement.
            print("  [%s] %-34s OPERATOR INPUT overridden by explicit request: %s"
                  % (tag, name, sorted(illegal)), flush=True)
        m = _methods.Method(
            key="lev_%d" % i, label=name, family="Controller", blurb=name,
            engine=ctl.engine,
            overrides={**(ctl.overrides or {}), **ov},
            engine_kwargs=dict(ctl.engine_kwargs or {}),
            knob_grid=(), knob_space=())
        safe = "".join(ch if ch.isalnum() or ch in "-_=." else "-" for ch in name)
        out = os.path.join(out_dir, "%s_%d_%s.xlsm" % (tag, i, safe[:40]))
        t0 = time.time()
        try:
            rc, _el = _methods.run_method(m, pr, out, config_dir, scenario_dir)
        except Exception as e:                                    # noqa: BLE001
            results.append({"name": name, "overrides": ov,
                            "error": "%s: %s" % (type(e).__name__, e)})
            print("  [%s] %-34s ERROR %s" % (tag, name, type(e).__name__), flush=True)
            continue
        if rc != 0:
            results.append({"name": name, "overrides": ov,
                            "error": "engine rc=%d" % rc})
            print("  [%s] %-34s rc=%d" % (tag, name, rc), flush=True)
            continue
        cfg = {**base, **ov}
        g = _grade(out, cfg, targets, m.engine)
        mt, gates = g["metrics"], g["gates"]
        sf = _ana.system_feed_review(out) or {}
        cv = _ana.convergence_review(out) or {}
        rows = _ana.harvest_rows(out)
        rev = _ana.revenue_for(rows, econ) if econ else {}
        hv = g["harvest"]
        rec = {
            "name": name, "overrides": ov, "seconds": round(time.time() - t0, 1),
            "workbook": out,
            # --- CONSTRAINTS (these decide) ---
            "gates": {x["key"]: x["status"] for x in gates},
            "hard_fails": sum(1 for x in gates
                              if x["status"] == "FAIL" and x.get("hard")),
            "soft_fails": sum(1 for x in gates
                              if x["status"] == "FAIL" and not x.get("hard")),
            "warns": sum(1 for x in gates if x["status"] == "WARN"),
            "feed_over": sf.get("over"), "feed_worst": sf.get("worst"),
            "feed_systems": sf.get("systems_breaching"),
            "weeks_below_floor": hv.get("weeks_below_min"),
            "min_week": hv.get("min_week"),
            "zero_weeks": hv.get("zero_weeks"),
            "red_weeks": cv.get("weeks_red"),
            "red_avoidable": cv.get("weeks_red_avoidable"),
            "settled_week": cv.get("settled_week"),
            "moves_week_max": mt.moves_week_max,
            "weeks_moves_over_cap": mt.weeks_moves_over_cap,
            # --- OUTCOME ---
            "harvest_count": sum(float(r.get("count") or 0) for r in rows),
            "revenue": rev.get("revenue"), "margin": rev.get("margin"),
            "peak_pct_of_cap": (mt.overall_peak_biomass / mt.biomass_cap * 100.0
                                if mt.biomass_cap else None),
            # --- SCORE (reported, never decisive) ---
            "system_overshoot": mt.component("system_overshoot"),
            "transfers_per_fish": mt.component("transfers_per_fish"),
            "schema": _opt.METRICS_SCHEMA,
        }
        results.append(rec)
        print("  [%s] %-34s feed %3s worst %.3f  floor<%2s min %7.0f  mv/wk %3s  %5.1fs"
              % (tag, name, rec["feed_over"], float(rec["feed_worst"] or 0),
                 rec["weeks_below_floor"], float(rec["min_week"] or 0),
                 rec["moves_week_max"], rec["seconds"]), flush=True)
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pr", required=True)
    ap.add_argument("--combos", required=True,
                    help="JSON list of {name, overrides} objects, or @file.json")
    ap.add_argument("--out", required=True, help="JSONL results file (appended)")
    ap.add_argument("--config-dir", default=str(ROOT / "config"))
    ap.add_argument("--scenario-dir", default=str(ROOT / "scenario"))
    ap.add_argument("--tag", default="run")
    ap.add_argument("--workbook-dir", default=None)
    ap.add_argument("--allow-operator-inputs", action="store_true",
                    help="permit combos that set methods.UNTUNABLE_KNOBS. For a "
                         "DELIBERATE named before/after only -- never for a "
                         "search. Every such run is announced in the output.")
    a = ap.parse_args(argv)

    spec = a.combos
    if spec.startswith("@"):
        spec = Path(spec[1:]).read_text(encoding="utf-8")
    combos = json.loads(spec)
    wbdir = a.workbook_dir or tempfile.mkdtemp(prefix="as_lev_")
    os.makedirs(wbdir, exist_ok=True)
    targets = _ana.load_targets(a.config_dir)
    res = measure(a.pr, combos, a.config_dir, a.scenario_dir, wbdir, a.tag, targets,
                  allow_operator_inputs=a.allow_operator_inputs)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "a", encoding="utf-8") as f:
        for r in res:
            f.write(json.dumps(r, default=float) + "\n")
    ok = sum(1 for r in res if "error" not in r)
    print("[%s] %d/%d ok -> %s" % (a.tag, ok, len(res), a.out))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
