"""PLACEMENT OPTIMIZER (POC): sweep selective over-stock candidates, score each
against the L1 result, pick the best.

For a fixed within-limits L1 envelope, each candidate is a (over-stock density,
weight threshold) setting of the selective over-stock lever in
`global_planner_l3_poc`. Each is run through the full L1<->L3 loop + the swap-free
specific-tank pick, then SCORED lexicographically:

  1. density-over-cap tank-weeks (welfare: count over 95 kg/m3)  -> minimize
  2. worst per-tank density                                       -> minimize
  3. dense / double-stacked rows (crowding)                       -> minimize
  4. transfers (churn)                                            -> minimize

The swap-free pick guarantees 0 TANK_DRIFT for every candidate, so legality is a
constant; the score ranks the legal options by welfare + utilization + churn.
ADDITIVE, sandbox-only; touches no production file.
"""
from __future__ import annotations

from pathlib import Path

from forecast.config_io import load_config
from forecast.scenario_io import load_batches, load_limits
from forecast import global_planner_loop_poc as loop
from forecast import global_planner_l3_poc as l3
from forecast import global_tank_pick_poc as tp
from tools.run_full_facility_poc import _hydrate_pr

# (label, overstock_density_pct, max_wt_g, lookahead_expand_weeks)
# None,None = operating density; lookahead 0 = no anticipatory pre-expand.
CANDIDATES = [
    ("baseline", None, None, 0),
    ("overstock light<2.5kg@1.0", 1.00, 2500.0, 0),
    ("lookahead 1wk", None, None, 1),
    ("lookahead 2wk", None, None, 2),
    ("overstock + lookahead 2wk", 1.00, 2500.0, 2),
]

DENSITY_CAP = 95.0  # kg/m3 welfare cap


def _score(pick) -> tuple:
    dens = [r.density_kg_m3 for r in pick.batch_locations if r.density_kg_m3 > 0]
    over = sum(1 for d in dens if d > DENSITY_CAP)
    maxd = max(dens) if dens else 0.0
    return (over, round(maxd), pick.n_oversub_rows, pick.n_transfers)


def main() -> int:
    control, tables, facility = load_config("config")
    batches = load_batches("scenario")
    _fl, system_limits = load_limits("scenario")
    inflight_og, fw_inflight, ds, purge_inflight = _hydrate_pr(
        Path("Forecast.xlsm"), batches)
    if ds is not None:
        control.forecast_start = ds

    print(f"  {'candidate':<26} {'over95':>6} {'maxd':>5} {'dense':>6} "
          f"{'xfer':>5} {'peak%':>6}")
    rows = []
    for name, dens, wt, look in CANDIDATES:
        l3._OVERSTOCK_DENSITY_PCT = dens
        l3._OVERSTOCK_MAX_WT_G = wt
        l3._LOOKAHEAD_EXPAND_WEEKS = look
        res = loop.run_loop(
            batches, tables, control, facility, system_limits,
            inflight_og=inflight_og,
            l3_kwargs=dict(slack_epsilon=1000.0, mip_time_limit=120.0,
                           mip_rel_gap=0.01, verbose=False),
            model_purge_hold=True, model_full_facility=True,
            fw_inflight=fw_inflight, purge_inflight=purge_inflight, verbose=False)
        pick = tp.pick_tanks(res, control, facility)
        sc = _score(pick)
        rows.append((name, sc, res.pct_of_cap_peak))
        print(f"  {name:<26} {sc[0]:>6} {sc[1]:>5} {sc[2]:>6} {sc[3]:>5} "
              f"{res.pct_of_cap_peak:>5.1f}%")
    l3._OVERSTOCK_DENSITY_PCT = None
    l3._OVERSTOCK_MAX_WT_G = None
    l3._LOOKAHEAD_EXPAND_WEEKS = 0

    best = min(rows, key=lambda r: r[1])
    print(f"\n  BEST given the L1 result: {best[0]}")
    print(f"    score (over95, maxd, dense, xfer) = {best[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
