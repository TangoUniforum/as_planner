"""Measure tank continuity of the MILP solution (temporary)."""
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
by_week = defaultdict(dict); wl_of = {}
for row in r.batch_standing:
    if getattr(row, "in_purge", False) or row.biomass_kg <= 1e-9: continue
    by_week[row.week][row.batch_id] = (row.biomass_kg, row.feed_kg_day, row.avg_wt_g)
    wl_of[row.week] = row.week_label

weeks = sorted(by_week)
occ = defaultdict(list)      # (batch,tank) -> [week indices occupied]
prev_tb = {}; swaps=[]
for wi, w in enumerate(weeks):
    q,_,_,_,_ = milp.solve_week(by_week[w], set((b,t) for (b,t) in prev_tb.items()) if False else
                                {(b,t) for t,b in prev_tb.items()},
                                og_tanks, tank_vol, wl_of[w], system_limits, control,
                                np, linprog, time_limit=45.0, mip_rel_gap=0.002)
    tb={}
    for (b,t),kg in q.items():
        occ[(b,t)].append(wi); tb[t]=b
    for t,b in tb.items():
        if t in prev_tb and prev_tb[t]!=b: swaps.append((wl_of[w],t,prev_tb[t],b))
    prev_tb=tb

# fragmentation: per (batch,tank), count contiguous intervals; >1 = left & re-entered
frag=0; multi=[]
for (b,t),wks in occ.items():
    wks=sorted(wks); intervals=1
    for i in range(1,len(wks)):
        if wks[i]!=wks[i-1]+1: intervals+=1
    if intervals>1:
        frag += intervals-1; multi.append((b,t,intervals))
# per-batch tank churn: distinct tanks over life vs peak-concurrent
print(f"same-week A->B swaps: {len(swaps)}  {swaps}")
print(f"fragmented (batch,tank) occupancies (left & re-entered SAME tank): {len(multi)}; extra re-entries {frag}")
for x in multi[:10]: print(f"   batch={x[0]} tank={x[1]} intervals={x[2]}")
