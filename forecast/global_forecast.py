"""ADAPTER: emit the STANDARD forecast workbook from the GLOBAL (L1->L3) method.

METHOD: GLOBAL (precalculated L1->L3)
====================================

This is the bridge between the tankless "global" planner POC
(`global_planner_poc` L1 + `global_planner_l3_poc` L3, converged by
`global_planner_loop_poc.run_loop`) and the SHARED `forecast.excel_io` writers
the production pipeline uses. It builds the data structures the writers consume
out of the converged L1 harvest envelope + L1 per-(batch,week) standing + L3
whole-tank placement, then `tools/run_global_forecast.py` calls the same writers
`forecast/run.py` calls.

It is deliberately ADDITIVE: it imports the POC layers + excel_io writers
verbatim and re-implements no biology, no L1/L3 math, and no writer. It is NOT
imported by `forecast/run.py`; the production pipeline stays byte-identical.

What is FULLY wired
-------------------
FULL (the L1/L3 layers support these without specific-tank detail):
  * HarvestPlan / HarvestReport / Batch Plan — from the L1 harvest envelope
    (round/live HOG; HOG-yield applied by the writer).
  * FeedForecastWeekly / FeedForecastMonthly — realized OG feed from the L3
    placement rows + projected FW/EGG feed (the validated biology), via the
    shared `_feed_by_type_week` helper.
  * Advisory — facility biomass + feed vs caps, scored on the TRUE total
    (FW + OG + purge), because L1 ran with `model_full_facility=True`.
  * WeeklyReport / MonthlyReport open-close ledgers — chained from the L1
    facility standing trace (facility grain, see the stamp).
  * ReconciliationReport — the L1 per-batch conservation
    (seeded == harvested + standing + mort + cull, FW counted in the total).

SPECIFIC-TANK PICK (step #2, NOW REAL — `forecast.global_tank_pick_poc`):
  * BatchLocations — REAL per-physical-tank occupancy (continuity-preserving
    pick: each batch stays on its tanks while L3 keeps it in the system; tanks
    claimed/released/relocated only when forced). One batch per tank, even-split.
  * FacilityMap — REAL physical per-tank grid (built from the same rows).
  * TransferPlan — REAL tank-to-tank moves: every physical relocation emitted as
    a Transfer (week, batch, source_tank, dest_tank, count). The 6N depuration
    flow uses the `forecast.sixn` pair round-robin (mains 61/63/65 preferred).
  * TankContinuityAudit — proves 0 TANK_DRIFT / 0 BIO_DRIFT over the emitted
    BatchLocations + TransferPlan + harvest events (every fish always in a tank;
    every move conserved) — the same invariant the production controller passes.
  The known structurally over-subscribed week (ceil rounding asks for >33
  grow-out tanks one week) is placed by double-stacking the overflow + FLAGGED
  (n_oversub_rows / oversub_weeks in the stamp), never dropped.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from . import global_tank_pick_poc as tankpick
from .models import BatchInput, BiologyTables, ControlParams, FacilityConfig


METHOD_STAMP = "planning_method = GLOBAL (precalculated L1->L3 + specific-tank pick)"
TANKPICK_STAMP = ("REAL specific-tank pick (continuity-preserving; 6N pair "
                  "round-robin; tank-to-tank transfers; TankContinuityAudit 0-drift)")


# ---------------------------------------------------------------------------
# Build the writer-facing structures from the converged loop result.
#
# BatchLocations / Transfers / Harvest events are all the REAL specific-tank
# objects from `global_tank_pick_poc` (which mirror the read shapes excel_io's
# writers + the TankContinuityAudit expect — placement.BatchLocationRow,
# events.Transfer, events.Harvest — without importing the production types).
# ---------------------------------------------------------------------------

@dataclass
class GlobalForecastTables:
    """Everything the runner needs to drive the excel_io writers."""
    batch_locations: list           # TankLocRow (REAL specific-tank occupancy)
    harvest_events: list            # TankHarvest (real source tank per draw)
    transfer_events: list           # TankTransfer (REAL tank-to-tank moves)
    tranog_events: list             # TankTranOG (first OG stocking, FW->tank)
    fw_states: list                 # BatchWeekState (FW/EGG phase; biology)
    conservation: dict              # L1 per-batch conservation
    trace: list                     # L1 StandingTraceRow (facility standing)
    purge_trace: list               # L1 purge-hold accounting
    forecast_start: date
    n_transfers: int                # count of tank-to-tank transfers emitted
    n_oversub_rows: int             # double-stacked rows (over-subscribed weeks)
    oversub_weeks: list             # [week_label] genuinely over-subscribed
    realized_biology: dict          # {(tank, wk, batch): (net_kg, mort)} for audit
    mort_states: list               # _MortState for the audit COUNT balance


def build_tables(
    loop_result,
    batches: list[BatchInput],
    tables: BiologyTables,
    control: ControlParams,
    facility: FacilityConfig,
    *,
    fw_inflight: Optional[dict] = None,
) -> GlobalForecastTables:
    """Convert a converged `run_loop` LoopResult into excel_io-ready structures.

    * `batch_locations`, `transfer_events` and `harvest_events` ALL come from the
      REAL specific-tank pick (`global_tank_pick_poc.pick_tanks`): a continuity-
      preserving physical-tank assignment of L3's system plan + the L1 6N
      purge-hold flow (sixn pair round-robin). Every physical move is a
      tank-to-tank Transfer and every harvest draw is debited from the specific
      tank the batch is released from (the 6N depuration tank in purge mode, the
      grow-out tank in production mode). The pick also emits `realized_biology`
      and `mort_states` so the TankContinuityAudit reconciles to 0 drift.
    * `fw_states` are the FW/EGG biology rows (the validated projectors), so the
      feed forecast + ledgers carry the freshwater phase.
    """
    fs = control.forecast_start
    fs_date = fs.date() if hasattr(fs, "date") else fs

    # ---- REAL specific-tank pick (step #2): physical tanks + transfers +
    #      harvest events + audit-closing realized biology / mortality. ----
    pick = tankpick.pick_tanks(loop_result, control, facility)

    # ---- FW/EGG biology rows (the validated projectors) ----
    fw_states = _fw_biology_states(batches, tables, control, fw_inflight=fw_inflight)

    return GlobalForecastTables(
        batch_locations=pick.batch_locations,
        harvest_events=pick.harvest_events,
        transfer_events=pick.transfers,
        tranog_events=pick.tranog_events,
        fw_states=fw_states,
        conservation=loop_result.final_l1.conservation,
        trace=loop_result.final_l1.trace,
        purge_trace=loop_result.final_l1.purge_trace,
        forecast_start=fs_date,
        n_transfers=pick.n_transfers,
        n_oversub_rows=pick.n_oversub_rows,
        oversub_weeks=pick.oversub_weeks,
        realized_biology=pick.realized_biology,
        mort_states=pick.mort_states,
    )


def _fw_biology_states(batches, tables, control, *, fw_inflight=None):
    """FW/EGG-stage BatchWeekState rows (the same projectors run.py drives).

    Only the FW/EGG rows are kept — the SW phase is the OG population L1 models
    (placed by L3), so the disjoint split mirrors `fw_phase_biomass_feed_by_week`.
    These feed the FeedForecast FW band + the ledger FW open/close.
    """
    from .biology import project_all_batches, project_in_flight_fw_batch
    fw_inflight = fw_inflight or {}
    out = []
    incoming = [b for b in batches if b.batch_id not in fw_inflight]
    states, _r, _s, _w = project_all_batches(incoming, tables, control)
    out.extend(s for s in states if s.stage in ("FW", "EGG"))
    batch_by_id = {b.batch_id: b for b in batches}
    for bid, (count, avg_wt, pr_close) in fw_inflight.items():
        b = batch_by_id.get(bid)
        if b is None or count <= 0:
            continue
        fw_states, _r2, _s2 = project_in_flight_fw_batch(
            b, tables, control, count, avg_wt, pr_close)
        out.extend(s for s in fw_states if s.stage in ("FW", "EGG"))
    return out


# ---------------------------------------------------------------------------
# Conservation check (for the runner's report + the workbook ReconciliationReport)
# ---------------------------------------------------------------------------

def conservation_summary(gft: GlobalForecastTables) -> dict:
    """Facility-level conservation: seeded == harvested + standing + mort + cull.

    Aggregates the L1 per-batch conservation. Standing@horizon already folds any
    fish still in the 6N purge hold (L1 does this), so FW-counted total holds.
    """
    tot = {"seeded": 0.0, "harvested": 0.0, "standing": 0.0,
           "mortality": 0.0, "cull": 0.0}
    worst = 0.0
    for c in gft.conservation.values():
        tot["seeded"] += c["seeded_count"]
        tot["harvested"] += c["harvested_count"]
        tot["standing"] += c["standing_count"]
        tot["mortality"] += c["mortality_count"]
        tot["cull"] += c["cull_count"]
        worst = max(worst, abs(c.get("residual_pct", 0.0)))
    accounted = (tot["harvested"] + tot["standing"]
                 + tot["mortality"] + tot["cull"])
    tot["accounted"] = accounted
    tot["residual"] = tot["seeded"] - accounted
    tot["residual_pct"] = (100.0 * tot["residual"] / tot["seeded"]
                           if tot["seeded"] > 0 else 0.0)
    tot["worst_batch_residual_pct"] = worst
    return tot
