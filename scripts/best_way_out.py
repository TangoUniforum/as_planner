"""Precalc-first 'best way out' analysis.

Determines the THEORETICAL FLOOR of density violations from the known
biomass curves + operational rules, independent of the placement
heuristic. Separates:
  - UNAVOIDABLE (capacity-bound): weeks where an eligibility class
    (OG1/2 nursery or OG3-6 grow-out) needs more tanks at 95 kg/m^3
    than the class physically has -> violations forced by physics.
  - ADDRESSABLE (scheduling-bound): the gap between the actual run's
    violations and the floor -> what better scheduling could recover.

Relaxation validity: within OG1/2 (sub-1 kg) and within OG3-6 fish can
move freely between tanks (progression law), so a class-level capacity
check is a valid lower bound (it allows ideal within-class spread).

Run from Python/:
    python scripts/best_way_out.py
"""
from __future__ import annotations

import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from forecast.biology import project_all_batches, project_in_flight_batch  # type: ignore
from forecast.caps import read_facility_limits, read_system_limits  # type: ignore
from forecast.excel_io import (  # type: ignore
    load_workbook, read_batches, read_biology_tables, read_control,
    read_facility_config, read_pinned_harvests, read_pinned_transfers,
)
from forecast.harvest_scheduler import schedule_harvests  # type: ignore
from forecast.precalc import build_precalc_canvas  # type: ignore
from forecast.production_report import (  # type: ignore
    hydrate_facility_state, read_production_report,
)
from forecast.state import FacilityState  # type: ignore

KG_PER_TANK_AT_CAP = 95.0 * 1720.0  # 163,400 kg
OG12 = {"OG1N", "OG1S", "OG2N", "OG2S"}


def main() -> int:
    wb = load_workbook(str(ROOT / "Forecast.xlsm"))
    control = read_control(wb); batches = read_batches(wb)
    tables = read_biology_tables(wb); facility = read_facility_config(wb)
    fl = read_facility_limits(wb, control.forecast_start)
    sl = read_system_limits(wb, control.forecast_start)
    _cd, ogr, _fw = read_production_report(wb)
    ph = read_pinned_harvests(wb, control.forecast_start)
    pt = read_pinned_transfers(wb, control.forecast_start)

    state = FacilityState.from_facility_config(facility, today=control.forecast_start)
    hydrate_facility_state(state, ogr, batches)
    alls, _r, sp, _w = project_all_batches(batches, tables, control)
    sbb = defaultdict(list)
    for s in alls:
        sbb[s.batch_id].append(s)
    pr_agg = defaultdict(lambda: [0.0, 0.0])
    for r in ogr:
        pr_agg[r.batch_id][0] += r.closing_count
        pr_agg[r.batch_id][1] += r.closing_biomass_kg
    bbid = {b.batch_id: b for b in batches}
    for bid, (cnt, bio) in pr_agg.items():
        if bid in sbb or cnt <= 0 or bid not in bbid:
            continue
        st = project_in_flight_batch(bbid[bid], tables, control, cnt,
                                     bio * 1000 / cnt, 16.0)
        if st:
            sbb[bid] = st
    dem, _ = schedule_harvests(states_by_batch=sbb, batches_by_id=bbid,
                               pinned=ph, control=control, facility_limits=fl)
    canvas = build_precalc_canvas(
        control=control, batches=batches, tables=tables, facility=facility,
        facility_limits=fl, system_limits=sl, biology_states_by_batch=dict(sbb),
        splits=sp, harvest_demands=dem, pinned_harvests=ph,
        pinned_transfers=pt, initial_state=state)

    # Physical class capacities (OG6N pipeline-owned in purge -> exclude).
    sys_tanks = defaultdict(int)
    for t in facility.tanks:
        if t.type == "OG":
            sys_tanks[t.system_id] += 1
    og12_cap = sum(n for s, n in sys_tanks.items() if s in OG12)
    og36_cap = sum(n for s, n in sys_tanks.items()
                   if s not in OG12 and s != "OG6N")

    print("=" * 70)
    print("BEST-WAY-OUT: theoretical density-violation floor")
    print("=" * 70)
    print(f"Class capacity: OG1/2 nursery = {og12_cap} tanks, "
          f"OG3-6 grow-out = {og36_cap} tanks (OG6N excluded, purge)")
    print(f"Per-tank cap @ 95 kg/m3 = {KG_PER_TANK_AT_CAP:,.0f} kg\n")

    # Per week, per class: density-required tanks vs capacity.
    floor_total = 0
    binding = []
    for wl in canvas.horizon_labels:
        req12 = req36 = 0
        for (b, w), f in canvas.batch_week_facts.items():
            if w != wl or f.stage != "SW":
                continue
            bio = f.biomass_kg_after_harvest
            if bio <= 0:
                continue
            need = math.ceil(bio / KG_PER_TANK_AT_CAP)
            if f.avg_wt_g >= 1000:
                req36 += need
            else:
                req12 += need
        ex12 = max(0, req12 - og12_cap)
        ex36 = max(0, req36 - og36_cap)
        floor_total += ex12 + ex36
        if ex12 or ex36:
            binding.append((wl, req12, ex12, req36, ex36))

    # Current empirical baseline (locked in tests/test_coordinator_regression).
    ACTUAL = 196  # 2026-06-01, Q-COORD.L 2-pass evaluator
    print(f"THEORETICAL FLOOR (forced over-capacity tank-weeks): "
          f"{floor_total}")
    print(f"Actual run (incremental coordinator): {ACTUAL} violations")
    print(f"=> Addressable-by-scheduling gap: ~{ACTUAL - floor_total}\n")

    if binding:
        print("Binding weeks (class over physical capacity):")
        print(f"  {'week':>9}{'OG12req':>8}{'OG12ex':>7}"
              f"{'OG36req':>8}{'OG36ex':>7}")
        for wl, r12, e12, r36, e36 in binding:
            print(f"  {wl:>9}{r12:>8}{e12:>7}{r36:>8}{e36:>7}")
    else:
        print("No week exceeds class capacity -> floor is 0; ALL violations "
              "are scheduling/distribution-addressable, none forced.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
