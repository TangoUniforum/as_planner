"""Full-horizon monolithic MILP: does global fallow planning kill the swaps?"""
import time
from collections import defaultdict
from pathlib import Path
import numpy as np
from scipy.optimize import linprog
from forecast.config_io import load_config
from forecast.scenario_io import load_batches, load_limits
from forecast import global_planner_poc as gpp
from forecast import global_placement_milp_poc as milp
from tools.run_full_facility_poc import _hydrate_pr

control, tables, facility = load_config("config")
batches = load_batches("scenario")
_fl, system_limits = load_limits("scenario", control)
inflight_og, fw_inflight, ds, purge_inflight = _hydrate_pr(Path("Forecast.xlsm"), batches)
if ds is not None: control.forecast_start = ds
r = gpp.plan(batches, tables, control, facility, inflight_og=inflight_og,
             record_standing=True, model_purge_hold=True, model_full_facility=True,
             fw_inflight=fw_inflight, purge_inflight=purge_inflight)
og_tanks = {t.tank_id: t.system_id for t in facility.tanks
            if t.type == "OG" and t.system_id != "OG6N"}
tank_vol = {t.tank_id: t.max_density_kg_m3 * t.volume_m3 for t in facility.tanks
            if t.type == "OG" and t.system_id != "OG6N"}
vol = {t.tank_id: t.volume_m3 for t in facility.tanks
       if t.type == "OG" and t.system_id != "OG6N"}
by_week = defaultdict(dict); wl_of = {}
for row in r.batch_standing:
    if getattr(row, "in_purge", False) or row.biomass_kg <= 1e-9: continue
    by_week[row.week][row.batch_id] = (row.biomass_kg, row.feed_kg_day, row.avg_wt_g)
    wl_of[row.week] = row.week_label

t0 = time.time()
q_by_w, info = milp.solve_full_horizon(
    by_week, og_tanks, tank_vol, vol, wl_of, system_limits, control,
    np, linprog, time_limit=900.0, mip_rel_gap=0.02)
dt = time.time() - t0
print(f"solved in {dt:.0f}s; status={info['status']} success={info['success']} obj={info.get('obj')}")
if q_by_w is None:
    print("  INFEASIBLE/none -> hard swap-free can't be met without L1 freeing tanks")
else:
    weeks = sorted(q_by_w); prev_tb = {}; swaps = 0; over95 = 0; worst = 0.0
    for w in weeks:
        tb = {}
        for (b, t), kg in q_by_w[w].items():
            tb[t] = b; d = kg / vol[t]; worst = max(worst, d)
            if d > 95: over95 += 1
        for t, b in tb.items():
            if t in prev_tb and prev_tb[t] != b: swaps += 1
        prev_tb = tb
    # conservation
    cons = 0.0
    for w in weeks:
        placed = defaultdict(float)
        for (b, t), kg in q_by_w[w].items(): placed[b] += kg
        for b, (bio, _, _) in by_week[w].items():
            if bio > 0: cons = max(cons, abs(placed.get(b, 0.0) - bio) / bio)
    print(f"  SAME-WEEK SWAPS: {swaps}   (hard swap-free target = 0)")
    print(f"  density>95 {over95}; worst {worst:.0f}; moved {info['moved_kfish']*1000:,.0f} fish;"
          f" over-cap {info['over_kg']:,.0f}kg; sys-slack {info['slack_kg']:,.0f}kg; conserv {cons*100:.4f}%")
