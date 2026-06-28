"""Verify the solve_cpsat MODULE function: swaps, density, conservation."""
import time
from collections import defaultdict
from pathlib import Path
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
og_tanks = {t.tank_id: t.system_id for t in facility.tanks if t.type=="OG" and t.system_id!="OG6N"}
tank_vol = {t.tank_id: t.max_density_kg_m3*t.volume_m3 for t in facility.tanks if t.type=="OG" and t.system_id!="OG6N"}
vol = {t.tank_id: t.volume_m3 for t in facility.tanks if t.type=="OG" and t.system_id!="OG6N"}
by_week = defaultdict(dict); wl_of = {}
for row in r.batch_standing:
    if getattr(row,"in_purge",False) or row.biomass_kg<=1e-9: continue
    by_week[row.week][row.batch_id]=(row.biomass_kg,row.feed_kg_day,row.avg_wt_g); wl_of[row.week]=row.week_label

q_by_w, info = milp.solve_cpsat(by_week, og_tanks, tank_vol, vol, wl_of, system_limits,
                                control, time_limit=300.0, workers=8, verbose=True)
print(f"status={info['status']} obj={info.get('obj')} bound={info.get('bound')}")
if q_by_w is None:
    print("  no solution"); raise SystemExit
prev={}; swaps=0; o95=0; worst=0.0; cons=0.0
for w in sorted(q_by_w):
    tb={}; placed=defaultdict(float)
    for (b,t),kg in q_by_w[w].items():
        tb[t]=b; placed[b]+=kg; d=kg/vol[t]; worst=max(worst,d)
        if d>95: o95+=1
    for t,b in tb.items():
        if t in prev and prev[t]!=b: swaps+=1
    for b,(bio,_,_) in by_week[w].items():
        if bio>0: cons=max(cons, abs(placed.get(b,0.0)-bio)/bio)
    prev=tb
print(f"  SAME-WEEK SWAPS: {swaps}  | density>95 {o95}; worst {worst:.0f}")
print(f"  worst conservation residual (incl kg-rounding): {cons*100:.4f}%")
print(f"  slack_kg {info['slack_kg']}; over_kg {info['over_kg']}")
