"""ProductionReport reader + FacilityState hydration.

ProductionReport is the canonical current-state snapshot. By
agreement (DESIGN §1, item 3), its closing date is the day BEFORE
forecast_start, so the PR closing values are exactly the tank states
at forecast_start opening — no bridging projection needed.

Sheet layout (row-tuple zero-indexed; cells refer to 1-indexed column):
  col 1: 'Closing Month: <m/d/yyyy>'   (top-level total row)
  col 2: 'Site: <name>'                 (site-level rollup)
  col 3: 'Fish group name: <id>'        (batch-level rollup)
  col 4: 'Unit: <tank_id>'              (per-tank row — the ones we want)
  col 7:  Closing Count   (fish)
  col 9:  Closing Biomass (kg)
  col 11: Closing Avg weight (g)

CV is not surfaced by PR; we default per-tank to the batch's
TranOG_CV from BatchRegistry (16% if the batch isn't found).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from .models import BatchInput
from .state import FacilityState, STAGE_FW, STAGE_SW


@dataclass
class PRTankRecord:
    """One OG (batch, tank) closing-state row from ProductionReport.

    Only emitted for OG-side rows where the Unit identifier is a bare
    integer matching a FacilityConfig OG tank_id (e.g. "Unit: 61").
    """
    batch_id: str
    tank_id: int
    closing_count: float
    closing_biomass_kg: float
    closing_avg_wt_g: float


@dataclass
class PRFWRecord:
    """One FW (batch, physical-unit) closing-state row.

    FW PR identifiers are stage-prefixed strings ("PostS.01", "Smolt.03").
    FacilityConfig models each FW system as a single logical tank but PR
    can place multiple batches in the same FW system simultaneously, so
    FW state needs a different representation than OG TankState.
    """
    batch_id: str
    unit_label: str          # raw Unit string, e.g. "PostS.01"
    fw_system: str           # parsed prefix, e.g. "PostS"
    closing_count: float
    closing_biomass_kg: float
    closing_avg_wt_g: float


# Map PR Unit-prefix -> FacilityConfig system_id.
_FW_PREFIX_TO_SYSTEM = {
    "HA1": "HA1",
    "HA2": "HA2",
    "SF": "SF",
    "Parr": "Par",
    "Pre": "PS",
    "PreS": "PS",
    "Smolt": "SM",
    "PostS": "PSM",
}


def _parse_unit_label(unit_str: str) -> tuple[Optional[int], Optional[str]]:
    """Classify a 'Unit:' value as OG (int) or FW (prefix string).

    Returns (og_tank_id, fw_prefix). Exactly one is non-None.
    "61"        -> (61, None)
    "PostS.01"  -> (None, "PostS")
    """
    s = unit_str.strip()
    if s.isdigit():
        return int(s), None
    # FW prefix: text before the dot (if any) else text up to first digit.
    prefix = re.split(r"[.\d]", s, maxsplit=1)[0]
    return None, prefix or None


def read_production_report(
    wb,
) -> tuple[Optional[date], list[PRTankRecord], list[PRFWRecord]]:
    """Parse ProductionReport into (closing_date, og_records, fw_records).

    OG records are emitted with closing_count > 0; same for FW.
    Empty tanks at closing are silently dropped.
    """
    ws = wb["ProductionReport"]
    closing_date: Optional[date] = None
    current_batch: Optional[str] = None
    og_records: list[PRTankRecord] = []
    fw_records: list[PRFWRecord] = []

    for row in ws.iter_rows(values_only=True):
        if not row:
            continue

        # Closing Month — col 1.
        c1 = row[0] if len(row) > 0 else None
        if isinstance(c1, str) and "Closing Month" in c1:
            m = re.search(r"(\d+/\d+/\d+)", c1)
            if m:
                try:
                    closing_date = datetime.strptime(m.group(1), "%m/%d/%Y").date()
                except ValueError:
                    pass
            continue

        # Fish group — col 3.
        c3 = row[2] if len(row) > 2 else None
        if isinstance(c3, str) and "Fish group" in c3:
            m = re.search(r"B\d+", c3)
            if m:
                current_batch = m.group(0)
            continue

        # Unit (per-tank) — col 4.
        c4 = row[3] if len(row) > 3 else None
        if not (isinstance(c4, str) and "Unit" in c4):
            continue
        if current_batch is None:
            continue
        unit_raw = c4.replace("Unit:", "").strip()
        og_id, fw_prefix = _parse_unit_label(unit_raw)

        closing_count = row[6] if len(row) > 6 else None       # col 7
        closing_biomass = row[8] if len(row) > 8 else None     # col 9
        closing_avg_wt = row[10] if len(row) > 10 else None    # col 11
        if not (isinstance(closing_count, (int, float)) and closing_count > 0):
            continue

        biomass = float(closing_biomass) if isinstance(closing_biomass, (int, float)) else 0.0
        avg_wt = float(closing_avg_wt) if isinstance(closing_avg_wt, (int, float)) else 0.0

        if og_id is not None:
            og_records.append(PRTankRecord(
                batch_id=current_batch,
                tank_id=og_id,
                closing_count=float(closing_count),
                closing_biomass_kg=biomass,
                closing_avg_wt_g=avg_wt,
            ))
        else:
            system = _FW_PREFIX_TO_SYSTEM.get(fw_prefix or "", fw_prefix or "?")
            fw_records.append(PRFWRecord(
                batch_id=current_batch,
                unit_label=unit_raw,
                fw_system=system,
                closing_count=float(closing_count),
                closing_biomass_kg=biomass,
                closing_avg_wt_g=avg_wt,
            ))

    return closing_date, og_records, fw_records


def summarize_fw_records(fw_records: list[PRFWRecord]) -> dict:
    """Per-(batch, system) aggregate of FW PR records for diagnostic display."""
    rolled: dict[tuple[str, str], dict] = {}
    for r in fw_records:
        key = (r.batch_id, r.fw_system)
        e = rolled.setdefault(key, {"count": 0.0, "biomass_kg": 0.0, "units": 0})
        e["count"] += r.closing_count
        e["biomass_kg"] += r.closing_biomass_kg
        e["units"] += 1
    return rolled


def hydrate_facility_state(
    state: FacilityState,
    records: list[PRTankRecord],
    batches: list[BatchInput],
) -> list[str]:
    """Stock each tank in `state` from the matching PR record.

    Stage is derived from TankConfig.type (FW tank -> FW stage, OG tank
    -> SW stage). CV defaults to the batch's TranOG_CV.

    Returns list of warning strings for tanks that couldn't be hydrated
    (unknown tank id, tank already stocked, etc.).
    """
    warns: list[str] = []
    batch_by_id = {b.batch_id: b for b in batches}
    for r in records:
        tank = state.tanks_by_id.get(r.tank_id)
        if tank is None:
            warns.append(
                f"PR: unknown tank #{r.tank_id} for batch {r.batch_id} "
                f"(count={r.closing_count:.0f}, biomass={r.closing_biomass_kg:.0f} kg)"
            )
            continue
        if not tank.is_empty:
            warns.append(
                f"PR: tank {tank.location_id} (#{r.tank_id}) already holds "
                f"batch {tank.batch_id}; cannot hydrate PR record for {r.batch_id}"
            )
            continue
        b = batch_by_id.get(r.batch_id)
        cv = b.tran_og_cv if b else 16.0
        stage = STAGE_FW if tank.type == "FW" else STAGE_SW
        tank.assign(
            batch_id=r.batch_id,
            count=r.closing_count,
            avg_wt_g=r.closing_avg_wt_g,
            cv_pct=cv,
            stage=stage,
        )
    return warns


def summarize_hydration(state: FacilityState) -> dict:
    """Snapshot summary for diagnostics: counts, biomass, per-system rollup."""
    occupied_tanks = [t for t in state.tanks_by_id.values() if not t.is_empty]
    by_system_biomass = state.biomass_by_system()
    by_system_occupied = {
        s: sum(1 for t in state.tanks_in_system(s) if not t.is_empty)
        for s in state.systems()
    }
    by_batch = state.count_by_batch()
    return {
        "occupied_tanks": len(occupied_tanks),
        "total_tanks": len(state.tanks_by_id),
        "total_biomass_kg": state.total_biomass(),
        "by_system_biomass": by_system_biomass,
        "by_system_occupied": by_system_occupied,
        "by_batch_count": by_batch,
        "num_batches_in_facility": len(by_batch),
    }
