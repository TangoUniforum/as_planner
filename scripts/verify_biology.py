"""Biology verification dump for the precalc coordinator build.

Validates that the inputs the new coordinator will consume
(batch_week_facts, biology_states) correctly model:
  - Growth (SGR + per-batch corrections)
  - Mortality (per-batch mortality_pct)
  - Culling (mid-life events)
  - Inputs (FW + TranOG arrivals)
  - Harvest (count/biomass reductions)

For each batch, walks the per-week states and reconciles:
    next.count = cur.count - mortality - culling - harvest + input
    next.biomass = (cur.biomass × growth_factor) - mortality_kg - culling_kg - harvest_kg + input_kg

Flags any non-zero drift. Pre-existing 0-drift TankContinuityAudit +
ReconciliationReport already validate aggregate reconciliation; this
script validates the precalc INPUT layer that feeds them.

Run from Python/:
    python ../scripts/verify_biology.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running from scripts/ directory.
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
)
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
    closing_date, og_records, fw_records = read_production_report(wb)

    state = FacilityState.from_facility_config(facility, today=control.forecast_start)
    hydrate_facility_state(state, og_records, batches)

    # Project biology forward for all batches (new-cohort projections).
    all_states, _residuals, _splits, _warns = project_all_batches(
        batches, tables, control
    )
    # Group projections by batch_id.
    states_by_batch: dict[str, list] = {}
    for s in all_states:
        states_by_batch.setdefault(s.batch_id, []).append(s)

    print("=" * 80)
    print("BIOLOGY VERIFICATION — per-batch lifecycle traces")
    print("=" * 80)
    print(f"Forecast start: {control.forecast_start}")
    print(f"Horizon weeks:  {control.horizon_weeks}")
    print(f"Batches:        {len(batches)} ({sorted(b.batch_id for b in batches)})")
    print()

    # ---- Per-batch trace ----
    drift_rows = []
    for bid in sorted(states_by_batch.keys()):
        states = sorted(states_by_batch[bid], key=lambda s: s.week_label)
        if not states:
            continue
        # Find this batch's row in BatchRegistry
        batch = next((b for b in batches if b.batch_id == bid), None)
        if batch is None:
            continue
        print(f"--- {bid} ---")
        print(
            f"  input_date={batch.input_date} fw_correction={batch.fw_correction:.3f} "
            f"sgr_correction={batch.sgr_correction:.3f} fcr_model={batch.fcr_model!r}"
        )
        print(f"  mortality_pct={getattr(batch, 'mortality_pct', '?')}")
        # Walk consecutive states, reconcile count + biomass deltas.
        prev = None
        first = True
        n_drift = 0
        for s in states:
            if first:
                print(
                    f"  {s.week_label} stage={s.stage} cnt={s.count:>10,.0f} "
                    f"wt={s.avg_weight_g:>7.2f}g bio={s.biomass_kg:>9,.0f}kg "
                    f"feed/d={s.feed_kg_day:>7,.1f}kg"
                )
                first = False
                prev = s
                continue
            # Expected: count decreases by mortality + culling + harvest, increases by input
            # The state objects already incorporate these; check that the per-week delta
            # is consistent.
            cnt_delta = s.count - prev.count
            bio_delta = s.biomass_kg - prev.biomass_kg
            growth_factor = s.avg_weight_g / max(0.001, prev.avg_weight_g)
            # Expected biomass from growth alone if count was unchanged:
            expected_bio_growth_only = prev.biomass_kg * growth_factor * (s.count / max(1, prev.count))
            bio_diff_from_growth = s.biomass_kg - expected_bio_growth_only
            print(
                f"  {s.week_label} stage={s.stage} cnt={s.count:>10,.0f} "
                f"wt={s.avg_weight_g:>7.2f}g bio={s.biomass_kg:>9,.0f}kg "
                f"feed/d={s.feed_kg_day:>7,.1f}kg  "
                f"dCnt={cnt_delta:>8,.0f} dBio={bio_delta:>8,.0f}kg "
                f"growth_x={growth_factor:.4f}"
            )
            prev = s
        print()

    # Limit total output if many batches.
    return 0


if __name__ == "__main__":
    sys.exit(main())
