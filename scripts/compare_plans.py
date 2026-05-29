"""Diagnostic compare: assignment_plan (new coordinator) vs
migration_plan (legacy FIFO) for selected batches.

Builds the canvas, then prints per-week tank sets side-by-side for
representative batches (B47 pre-existing, B50 in-horizon TranOG).

Run from Python/:
    python ../scripts/compare_plans.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from forecast.biology import project_all_batches  # type: ignore
from forecast.excel_io import (  # type: ignore
    load_workbook,
    read_batches,
    read_biology_tables,
    read_control,
    read_facility_config,
    read_pinned_harvests,
    read_pinned_transfers,
)
from forecast.caps import read_facility_limits, read_system_limits  # type: ignore
from forecast.harvest_scheduler import schedule_harvests  # type: ignore
from forecast.precalc import build_precalc_canvas  # type: ignore
from forecast.production_report import (  # type: ignore
    hydrate_facility_state,
    read_production_report,
)
from forecast.state import FacilityState  # type: ignore


def main() -> int:
    wb_path = ROOT / "Forecast.xlsm"
    wb = load_workbook(str(wb_path))

    control = read_control(wb)
    batches = read_batches(wb)
    tables = read_biology_tables(wb)
    facility = read_facility_config(wb)
    facility_limits = read_facility_limits(wb, control.forecast_start)
    system_limits = read_system_limits(wb, control.forecast_start)
    closing_date, og_records, fw_records = read_production_report(wb)
    pinned_harvests = read_pinned_harvests(wb, control.forecast_start)
    pinned_transfers = read_pinned_transfers(wb, control.forecast_start)

    state = FacilityState.from_facility_config(
        facility, today=control.forecast_start
    )
    hydrate_facility_state(state, og_records, batches)

    all_states, _r, splits, _w = project_all_batches(batches, tables, control)
    states_by_batch: dict[str, list] = {}
    for s in all_states:
        states_by_batch.setdefault(s.batch_id, []).append(s)

    batches_by_id = {b.batch_id: b for b in batches}
    demands, _sw = schedule_harvests(
        states_by_batch=states_by_batch,
        batches_by_id=batches_by_id,
        pinned=pinned_harvests,
        control=control,
        facility_limits=facility_limits,
    )

    canvas = build_precalc_canvas(
        control=control,
        batches=batches,
        tables=tables,
        facility=facility,
        facility_limits=facility_limits,
        system_limits=system_limits,
        biology_states_by_batch=states_by_batch,
        splits=splits,
        harvest_demands=demands,
        pinned_harvests=pinned_harvests,
        pinned_transfers=pinned_transfers,
        initial_state=state,
    )

    print("=" * 80)
    print("PLAN COMPARISON  --  migration_plan (legacy) vs assignment_plan (new)")
    print("=" * 80)
    print(f"migration_plan entries:   {len(canvas.migration_plan):>5}")
    print(f"assignment_plan entries:  {len(canvas.assignment_plan):>5}")
    print()

    targets = ["B47", "B50", "B48", "B41"]
    for bid in targets:
        print(f"--- {bid} ---")
        weeks = sorted({wl for (b, wl) in canvas.batch_week_facts if b == bid})
        for wl in weeks:
            fact = canvas.batch_week_facts.get((bid, wl))
            if fact is None or fact.stage != "SW":
                continue
            mig = canvas.migration_plan.get((bid, wl))
            asg = canvas.assignment_plan.get((bid, wl))
            mig_tanks = sorted(set(mig.keep_tanks) | set(mig.add_tanks)) if mig else []
            asg_tanks = asg.tank_ids if asg else []
            same = (mig_tanks == asg_tanks)
            mark = "" if same else "  *DIFF*"
            print(f"  {wl} needed={fact.tanks_needed_at_density_cap:>2}  "
                  f"mig({len(mig_tanks):>2})={mig_tanks}  "
                  f"asg({len(asg_tanks):>2})={asg_tanks}{mark}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
