"""MILP with the REAL fish-moved objective: continuity + density + movement."""
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
_fl, system_limits = load_limits("scenario")
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

prev_count = {}; prev_tb = {}
t0=time.time(); MOVED=0.0; over95=0; worst=0.0; swaps=0; nonopt=0; cons=0.0; entries=0
for w in sorted(by_week):
    q, moved, over, sslk, status = milp.solve_week(
        by_week[w], prev_count, og_tanks, tank_vol, wl_of[w], system_limits, control,
        np, linprog, time_limit=45.0, mip_rel_gap=0.002)
    if status != 0: nonopt += 1
    MOVED += moved
    placed=defaultdict(float); tb={}; nc={}
    for (b,t),kg in q.items():
        placed[b]+=kg; tb[t]=b; nc[(b,t)]=kg/(by_week[w][b][2]*1000.0)
        d=kg/vol[t]; worst=max(worst,d)
        if d>95: over95+=1
        if (b,t) not in prev_count: entries+=1
    for b,(bio,_,_) in by_week[w].items():
        if bio>0: cons=max(cons, abs(placed.get(b,0.0)-bio)/bio)
    for t,b in tb.items():
        if t in prev_tb and prev_tb[t]!=b: swaps+=1
    prev_tb=tb; prev_count=nc
print(f"[REAL fish-moved objective, 45s/wk] solved in {time.time()-t0:.0f}s; nonopt {nonopt}")
print(f"  fish MOVED total: {MOVED*1000:,.0f}  | tank-entries: {entries}")
print(f"  density>95 {over95}; worst {worst:.0f}; swaps {swaps}; sys-slack {sslk:.0f}; conservation {cons*100:.4f}%")
