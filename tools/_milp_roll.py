"""Rolling-window MILP: does windowed fallow planning drive swaps to 0?"""
import sys, time
from collections import defaultdict
from pathlib import Path
import numpy as np
from scipy.optimize import linprog
from forecast.config_io import load_config
from forecast.scenario_io import load_batches, load_limits
from forecast import global_planner_poc as gpp
from forecast import global_placement_milp_poc as milp
from tools.run_full_facility_poc import _hydrate_pr

WINDOW = int(sys.argv[1]) if len(sys.argv) > 1 else 8
TL = int(sys.argv[3]) if len(sys.argv) > 3 else 60
STRIDE = int(sys.argv[2]) if len(sys.argv) > 2 else 4
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

print(f"WINDOW={WINDOW} STRIDE={STRIDE}")
t0 = time.time()
committed, info = milp.solve_rolling(
    by_week, og_tanks, tank_vol, vol, wl_of, system_limits, control,
    np, linprog, window=WINDOW, stride=STRIDE, time_limit=float(TL), mip_rel_gap=0.02,
    verbose=False)
dt = time.time() - t0
if committed is None:
    print(f"FAILED after {dt:.0f}s: {info}"); sys.exit()
weeks = sorted(committed); prev_tb = {}; swaps = 0; over95 = 0; worst = 0.0; cons = 0.0
for w in weeks:
    tb = {}; placed = defaultdict(float)
    for (b, t), kg in committed[w].items():
        tb[t] = b; placed[b] += kg; d = kg / vol[t]; worst = max(worst, d)
        if d > 95: over95 += 1
    for t, b in tb.items():
        if t in prev_tb and prev_tb[t] != b: swaps += 1
    for b, (bio, _, _) in by_week[w].items():
        if bio > 0: cons = max(cons, abs(placed.get(b, 0.0) - bio) / bio)
    prev_tb = tb
print(f"solved {len(weeks)} wks in {dt:.0f}s")
print(f"  SAME-WEEK SWAPS: {swaps}  | density>95 {over95}; worst {worst:.0f}; conserv {cons*100:.4f}%")
