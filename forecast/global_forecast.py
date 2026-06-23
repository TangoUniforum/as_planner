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

What is FULLY wired vs SYSTEM-LEVEL / pending
---------------------------------------------
FULL (the L1/L3 layers support these without specific-tank detail):
  * HarvestPlan / HarvestReport / Batch Plan — from the L1 harvest envelope
    (round/live HOG; HOG-yield applied by the writer).
  * FeedForecastWeekly / FeedForecastMonthly — realized OG feed from the L3
    placement rows + projected FW/EGG feed (the validated biology), via the
    shared `_feed_by_type_week` helper.
  * Advisory — facility biomass + feed vs caps, scored on the TRUE total
    (FW + OG + purge), because L1 ran with `model_full_facility=True`.
  * FacilityMap — per-system tank-counts + biomass + feed from the L3 layout.
  * WeeklyReport / MonthlyReport open-close ledgers — chained from the L1
    facility standing trace (facility grain, see the stamp).
  * ReconciliationReport — the L1 per-batch conservation
    (seeded == harvested + standing + mort + cull, FW counted in the total).

SYSTEM-LEVEL / SPECIFIC-TANK ASSIGNMENT PENDING (clearly stamped on the sheet):
  * BatchLocations / FacilityMap rows / TransferPlan carry a DETERMINISTIC,
    PROVISIONAL within-system tank assignment (fill the system's tanks in
    tank-id order, one batch's tanks packed densely). The specific physical-tank
    pick + density-walk + 6N pair rotation is the DEFERRED next step (#2). Every
    such sheet is stamped "system-level; specific-tank assignment pending".
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from . import global_planner_poc as gpp
from .global_planner_l2_poc import NURSERY_SYSTEMS, GROWOUT_SYSTEMS, PURGE_SYSTEMS
from .global_planner_l3_poc import n_tanks_per_system, smallest_og_tank_kg
from .models import BatchInput, BiologyTables, ControlParams, FacilityConfig
from .time_grid import iso_week_label, parse_iso_label, week_range


METHOD_STAMP = "planning_method = GLOBAL (precalculated L1->L3)"
PENDING_STAMP = "system-level; specific-tank assignment pending (specific-tank pick deferred)"


# ---------------------------------------------------------------------------
# Lightweight stand-ins for the writer-consumed dataclasses (duck-typed).
# excel_io writers read attributes, never isinstance — so these adapters carry
# exactly the fields each writer reads. (We do NOT import placement.py /
# events.py types — those are production files; we only mirror their read shape.)
# ---------------------------------------------------------------------------

@dataclass
class _LocRow:
    """Mirror of placement.BatchLocationRow (the read shape excel_io expects)."""
    week_label: str
    week_start: date
    batch_id: str
    tank_id: int
    location_id: str
    system_id: str
    count: float
    avg_wt_g: float
    biomass_kg: float
    density_kg_m3: float
    stage: str = ""


@dataclass
class _HarvestEv:
    """Mirror of events.Harvest's read shape (HarvestPlan/Report/Advisory)."""
    batch_id: str
    event_date: date
    source_tank_id: int
    count: float
    avg_wt_g: float


# ---------------------------------------------------------------------------
# Build the writer-facing structures from the converged loop result.
# ---------------------------------------------------------------------------

def _label_to_week_start(label: str, forecast_start) -> date:
    """Monday of an ISO label, falling back to the forecast start if unparseable."""
    d = parse_iso_label(label)
    if d is not None:
        return d
    fs = forecast_start.date() if hasattr(forecast_start, "date") else forecast_start
    return fs


def _system_tank_ids(facility: FacilityConfig) -> dict[str, list[int]]:
    """Per OG system, the sorted physical tank ids (for the provisional pick)."""
    out: dict[str, list[int]] = {}
    for t in facility.tanks:
        if t.type == "OG":
            out.setdefault(t.system_id, []).append(t.tank_id)
    for s in out:
        out[s].sort()
    return out


@dataclass
class GlobalForecastTables:
    """Everything the runner needs to drive the excel_io writers."""
    batch_locations: list           # _LocRow (system-level provisional tanks)
    harvest_events: list            # _HarvestEv (from L1 envelope)
    fw_states: list                 # BatchWeekState (FW/EGG phase; biology)
    conservation: dict              # L1 per-batch conservation
    trace: list                     # L1 StandingTraceRow (facility standing)
    purge_trace: list               # L1 purge-hold accounting
    forecast_start: date
    n_pending_tank_rows: int        # rows carrying provisional tank ids


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

    * `harvest_events` come from the L1 harvest ENVELOPE (one event per
      (batch, week); event_date = the ISO week's Monday; source_tank_id is a
      provisional in-system tank).
    * `batch_locations` come from the L3 PLACEMENT (one row per
      (batch, system, week, tank), provisionally numbered within the system),
      plus the L1 6N purge-hold population as STARVE rows on OG6N tanks.
    * `fw_states` are the FW/EGG biology rows (the validated projectors), so the
      feed forecast + ledgers carry the freshwater phase.
    """
    l1 = loop_result.final_l1
    l3 = loop_result.final_l3
    fs = control.forecast_start
    fs_date = fs.date() if hasattr(fs, "date") else fs

    # ---- Harvest events from the L1 envelope ----
    harvest_events: list[_HarvestEv] = []
    # Deterministic provisional source-tank ids: reuse the facility's first OG
    # tank id as a stand-in (the specific-tank pick is deferred). We keep a
    # per-week rotating index so the HarvestReport doesn't collapse every
    # event onto one tank id visually.
    sys_tank_ids = _system_tank_ids(facility)
    purge_tanks = [tid for s in PURGE_SYSTEMS for tid in sys_tank_ids.get(s, [])]
    all_og_tanks = sorted(tid for ids in sys_tank_ids.values() for tid in ids)
    _ht_default = (purge_tanks or all_og_tanks or [0])
    for i, e in enumerate(sorted(l1.envelope, key=lambda r: (r.week_label, r.batch_id))):
        if e.count <= 0:
            continue
        ws = _label_to_week_start(e.week_label, fs_date)
        harvest_events.append(_HarvestEv(
            batch_id=e.batch_id, event_date=ws,
            source_tank_id=_ht_default[i % len(_ht_default)],
            count=e.count, avg_wt_g=e.avg_wt_g,
        ))

    # ---- Batch locations from the L3 placement (provisional tank pick) ----
    # Per (system, week) keep a cursor so each batch's tanks get distinct ids
    # within that system that week (densely packed, deterministic). When demand
    # exceeds the physical tank count (documented over-subscription on this
    # capacity-bound config) the ids wrap — flagged via the pending stamp.
    per_tank_cap = smallest_og_tank_kg(facility)  # raw mass for density calc
    tank_volume = {t.tank_id: t.volume_m3 for t in facility.tanks}
    batch_locations: list[_LocRow] = []
    cursor: dict[tuple[str, str], int] = {}
    n_pending = 0
    for p in sorted(l3.placements, key=lambda r: (r.week_label, r.system_id, r.batch_id)):
        if p.tanks <= 0:
            continue
        ids = sys_tank_ids.get(p.system_id, [])
        if not ids:
            continue
        ws = _label_to_week_start(p.week_label, fs_date)
        per_tank_bio = p.biomass_kg / p.tanks
        per_tank_count = 0.0
        avg_wt_g = (p.biomass_kg * 1000.0 / 1.0)  # placeholder; set below per row
        # Per-tank count from the placement's per-tank biomass + the batch mean
        # weight that week (from L1 standing). Use the placement avg weight.
        avg_wt_g = _avg_wt_for(l1, p.batch_id, p.week)
        if avg_wt_g <= 0:
            avg_wt_g = (per_tank_bio * 1000.0)  # degenerate fallback
        per_tank_count = per_tank_bio * 1000.0 / avg_wt_g if avg_wt_g > 0 else 0.0
        for k in range(p.tanks):
            key = (p.system_id, p.week_label)
            idx = cursor.get(key, 0)
            cursor[key] = idx + 1
            tank_id = ids[idx % len(ids)]
            if idx >= len(ids):
                n_pending += 1   # over-subscribed: provisional id wraps
            vol = tank_volume.get(tank_id, 0.0)
            dens = (per_tank_bio / vol) if vol > 0 else 0.0
            batch_locations.append(_LocRow(
                week_label=p.week_label, week_start=ws, batch_id=p.batch_id,
                tank_id=tank_id, location_id=f"{p.system_id}-{tank_id}",
                system_id=p.system_id, count=per_tank_count,
                avg_wt_g=avg_wt_g, biomass_kg=per_tank_bio,
                density_kg_m3=dens, stage="",
            ))

    # ---- 6N purge-hold population as STARVE rows on OG6N (off-feed) ----
    sixn_cap = smallest_og_tank_kg(facility) * 1.25
    purge_ids = purge_tanks or all_og_tanks
    pcursor: dict[str, int] = {}
    for r in l1.batch_standing:
        if not getattr(r, "in_purge", False) or r.biomass_kg <= 1e-9:
            continue
        n_tanks_held = max(1, math.ceil(r.biomass_kg / sixn_cap))
        ws = _label_to_week_start(r.week_label, fs_date)
        per_tank_bio = r.biomass_kg / n_tanks_held
        per_tank_count = r.count / n_tanks_held
        for k in range(n_tanks_held):
            idx = pcursor.get(r.week_label, 0)
            pcursor[r.week_label] = idx + 1
            tid = purge_ids[idx % len(purge_ids)] if purge_ids else 0
            vol = tank_volume.get(tid, 0.0)
            dens = (per_tank_bio / vol) if vol > 0 else 0.0
            sysid = next((s for s in PURGE_SYSTEMS
                          if tid in sys_tank_ids.get(s, [])), "OG6N")
            batch_locations.append(_LocRow(
                week_label=r.week_label, week_start=ws, batch_id=r.batch_id,
                tank_id=tid, location_id=f"{sysid}-{tid}", system_id=sysid,
                count=per_tank_count, avg_wt_g=r.avg_wt_g,
                biomass_kg=per_tank_bio, density_kg_m3=dens, stage="STARVE",
            ))
            n_pending += 1

    # ---- FW/EGG biology rows (the validated projectors) ----
    fw_states = _fw_biology_states(batches, tables, control, fw_inflight=fw_inflight)

    return GlobalForecastTables(
        batch_locations=batch_locations,
        harvest_events=harvest_events,
        fw_states=fw_states,
        conservation=l1.conservation,
        trace=l1.trace,
        purge_trace=l1.purge_trace,
        forecast_start=fs_date,
        n_pending_tank_rows=n_pending,
    )


def _avg_wt_for(l1, batch_id: str, week: int) -> float:
    """Mean weight (g) for a (batch, week) from L1 standing; 0 if not found."""
    for r in l1.batch_standing:
        if r.batch_id == batch_id and r.week == week and not getattr(r, "in_purge", False):
            return r.avg_wt_g
    return 0.0


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
