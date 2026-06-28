"""Full-horizon placement via CP-SAT (OR-Tools): does it crack the full picture?"""
import time
from collections import defaultdict
from pathlib import Path
from ortools.sat.python import cp_model
from forecast.config_io import load_config
from forecast.scenario_io import load_batches, load_limits
from forecast import global_planner_poc as gpp
from forecast.global_planner_l2_poc import GROWOUT_SYSTEMS, NURSERY_SYSTEMS
from forecast.global_placement_milp_poc import _eligible_tanks, _scap, _DEFAULT_BIO_CAP, _DEFAULT_FEED_CAP
from forecast.caps import METRIC_BIOMASS, METRIC_FEED_DAY
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
gset, nset = set(GROWOUT_SYSTEMS), set(NURSERY_SYSTEMS)
weeks = sorted(by_week); wpos={w:i for i,w in enumerate(weeks)}
systems = sorted(set(og_tanks.values()))

m = cp_model.CpModel()
x={}; q={}; ov={}; tr={}
for w in weeks:
    for b,(bio,feed,avg) in by_week[w].items():
        if bio<=1e-9: continue
        for t in _eligible_tanks(avg,og_tanks,gset,nset):
            B=int(bio)
            x[b,t,w]=m.NewBoolVar(f"x{b}_{t}_{w}")
            q[b,t,w]=m.NewIntVar(0,B,f"q{b}_{t}_{w}")
            ov[b,t,w]=m.NewIntVar(0,B,f"o{b}_{t}_{w}")
            m.Add(q[b,t,w] <= B*x[b,t,w])
            m.Add(ov[b,t,w] >= q[b,t,w]-int(tank_vol[t]))
for (b,w) in {(b,w) for (b,t,w) in x}:
    m.Add(sum(q[b,t,w] for t in og_tanks if (b,t,w) in x)==int(by_week[w][b][0]))
for (t,w) in {(t,w) for (b,t,w) in x}:
    m.Add(sum(x[b,t,w] for b in by_week[w] if (b,t,w) in x)<=1)
# swap-free HARD: x[b,t,w] + sum_{a!=b} x[a,t,w-1] <= 1
for (b,t,w) in x:
    pw=w-1
    if pw in wpos:
        others=[x[a,t,pw] for a in by_week.get(pw,{}) if a!=b and (a,t,pw) in x]
        if others: m.Add(x[b,t,w]+sum(others)<=1)
    tr[b,t,w]=m.NewBoolVar(f"t{b}_{t}_{w}")
    if (b,t,pw) in x: m.Add(tr[b,t,w]>=x[b,t,w]-x[b,t,pw])
    else: m.Add(tr[b,t,w]>=x[b,t,w]) if pw not in wpos else m.Add(tr[b,t,w]>=x[b,t,w])
# system caps soft
sl=[]
for (s,w) in {(og_tanks[t],w) for (b,t,w) in x}:
    cells=[(b,t) for (b,t,ww) in x if ww==w and og_tanks[t]==s]
    sb=m.NewIntVar(0,10**7,f"sb{s}_{w}"); sl.append(sb)
    m.Add(sum(q[b,t,w] for (b,t) in cells)<=int(_scap(METRIC_BIOMASS,wl_of[w],s,system_limits,_DEFAULT_BIO_CAP))+sb)
    sf=m.NewIntVar(0,10**7,f"sf{s}_{w}"); sl.append(sf)
    fc=int(_scap(METRIC_FEED_DAY,wl_of[w],s,system_limits,_DEFAULT_FEED_CAP)*1000)
    m.Add(sum(q[b,t,w]*int(by_week[w][b][1]/by_week[w][b][0]*1000) for (b,t) in cells)<=fc+sf*1000)
m.Minimize(1000000*sum(sl)+1000*sum(ov.values())+sum(tr.values()))
solver=cp_model.CpSolver()
solver.parameters.max_time_in_seconds=300.0
solver.parameters.num_search_workers=8
print(f"vars: {len(x)} x, {len(x)} q; solving with CP-SAT (300s, 8 workers)...")
t0=time.time(); st=solver.Solve(m); dt=time.time()-t0
print(f"status={solver.StatusName(st)} in {dt:.0f}s; obj={solver.ObjectiveValue():.0f} bound={solver.BestObjectiveBound():.0f}")
if st in (cp_model.OPTIMAL,cp_model.FEASIBLE):
    prev={}; swaps=0; o95=0; worst=0.0
    for w in weeks:
        tb={}
        for (b,t,ww) in x:
            if ww!=w: continue
            qq=solver.Value(q[b,t,w])
            if qq>0:
                tb[t]=b; d=qq/vol[t]; worst=max(worst,d)
                if d>95: o95+=1
        for t,b in tb.items():
            if t in prev and prev[t]!=b: swaps+=1
        prev=tb
    print(f"  SAME-WEEK SWAPS: {swaps}  | density>95 {o95}; worst {worst:.0f}")
