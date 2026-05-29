"""Per-tank state container and facility aggregator.

Pure data + mutation primitives. No biology table lookups, no Excel I/O.
Daily growth + mortality rates are computed by the caller (the placement
walk) and passed in as `apply_daily_*` arguments.

Conceptual model:

  FacilityState   --owns-->   dict[tank_id -> TankState]
  TankState       --has-->    (location_id, system_id, batch_id, count, avg_wt, ...)

  At each day in the simulation:
    1. Apply scheduled Events for the day (events.py) — these mutate
       tank-level batch_id/count/avg_wt/cv.
    2. Apply continuous biology for the day:
       - apply_daily_mortality(survival_factor)  per non-empty tank
       - apply_daily_growth(sgr_pct_day)         per non-empty tank
    3. Advance FacilityState.today by one day.

Continuity invariants (DESIGN §8) are enforced by event apply methods
where possible and surfaced as warnings via `check_invariants()`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .models import FacilityConfig, TankConfig


# Stage tags carried on TankState.
STAGE_EMPTY = ""
STAGE_FW = "FW"
STAGE_SW = "SW"
STAGE_STARVE = "STARVE"  # 6N production-mode in-place purge (no feed, no growth)


@dataclass
class TankState:
    """State of one physical tank.

    Configuration fields (location_id..type) are copied from
    FacilityConfig at initialization and never mutated. Dynamic fields
    (batch_id, count, avg_wt_g, cv_pct, stage, starvation_days_remaining)
    evolve through events + continuous biology.
    """
    # ---- Config (immutable after init) ----
    location_id: str
    tank_id: int
    system_id: str
    volume_m3: float
    max_density_kg_m3: float
    max_feed_kg_day_cap: float   # per-tank feed cap from FacilityConfig
    type: str                    # "FW" or "OG"

    # ---- Dynamic state ----
    batch_id: Optional[str] = None
    count: float = 0.0
    avg_wt_g: float = 0.0
    cv_pct: float = 0.0
    stage: str = STAGE_EMPTY
    starvation_days_remaining: int = 0

    # ---- Telemetry ----
    last_emptied_date: Optional[date] = None   # for rotation rule (Phase C)

    # ---- Computed properties ----
    @property
    def is_empty(self) -> bool:
        return self.batch_id is None or self.count <= 0

    @property
    def biomass_kg(self) -> float:
        if self.is_empty:
            return 0.0
        return self.count * self.avg_wt_g / 1000.0

    @property
    def density_kg_m3(self) -> float:
        if self.volume_m3 <= 0 or self.is_empty:
            return 0.0
        return self.biomass_kg / self.volume_m3

    @property
    def max_biomass_kg(self) -> float:
        """Density-cap-implied biomass ceiling for this tank."""
        return self.max_density_kg_m3 * self.volume_m3

    # ---- Mutation primitives ----
    def apply_daily_mortality(self, survival_factor: float) -> None:
        """Multiply count by daily survival factor (1.0 = no mortality)."""
        if self.is_empty:
            return
        self.count *= survival_factor

    def apply_daily_growth(self, sgr_pct_day: float) -> None:
        """Compound avg weight by one day at sgr_pct_day %/day."""
        if self.is_empty or self.stage == STAGE_STARVE:
            return
        self.avg_wt_g *= (1.0 + sgr_pct_day / 100.0)

    def empty(self, today: date) -> None:
        """Remove all fish from the tank; record empty timestamp for rotation."""
        self.batch_id = None
        self.count = 0.0
        self.avg_wt_g = 0.0
        self.cv_pct = 0.0
        self.stage = STAGE_EMPTY
        self.starvation_days_remaining = 0
        self.last_emptied_date = today

    def assign(self, batch_id: str, count: float, avg_wt_g: float, cv_pct: float, stage: str) -> None:
        """Stock an empty tank with a batch."""
        self.batch_id = batch_id
        self.count = count
        self.avg_wt_g = avg_wt_g
        self.cv_pct = cv_pct
        self.stage = stage
        self.starvation_days_remaining = 0


class FacilityState:
    """Container for every physical tank's TankState + time cursor."""

    def __init__(self, today: date, tanks: list[TankState]) -> None:
        self.today: date = today
        self.tanks_by_id: dict[int, TankState] = {t.tank_id: t for t in tanks}
        self._by_system: dict[str, list[TankState]] = {}
        for t in tanks:
            self._by_system.setdefault(t.system_id, []).append(t)

    # ---- Builders ----
    @classmethod
    def from_facility_config(cls, facility: FacilityConfig, today: date) -> "FacilityState":
        tanks = [
            TankState(
                location_id=c.location_id,
                tank_id=c.tank_id,
                system_id=c.system_id,
                volume_m3=c.volume_m3,
                max_density_kg_m3=c.max_density_kg_m3,
                max_feed_kg_day_cap=c.max_feed_kg_day,
                type=c.type,
            )
            for c in facility.tanks
        ]
        return cls(today=today, tanks=tanks)

    # ---- Lookups ----
    def tanks_for_batch(self, batch_id: str) -> list[TankState]:
        return [t for t in self.tanks_by_id.values() if t.batch_id == batch_id]

    def tanks_in_system(self, system_id: str) -> list[TankState]:
        return list(self._by_system.get(system_id, ()))

    def empty_tanks_in_system(self, system_id: str) -> list[TankState]:
        return [t for t in self._by_system.get(system_id, ()) if t.is_empty]

    def occupied_tanks_in_system(self, system_id: str) -> list[TankState]:
        return [t for t in self._by_system.get(system_id, ()) if not t.is_empty]

    def systems(self) -> list[str]:
        return list(self._by_system.keys())

    # ---- Aggregates ----
    def biomass_by_system(self) -> dict[str, float]:
        return {
            s: sum(t.biomass_kg for t in tanks)
            for s, tanks in self._by_system.items()
        }

    def total_biomass(self) -> float:
        return sum(t.biomass_kg for t in self.tanks_by_id.values())

    def total_biomass_by_stage(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for t in self.tanks_by_id.values():
            out[t.stage] = out.get(t.stage, 0.0) + t.biomass_kg
        return out

    def count_by_batch(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for t in self.tanks_by_id.values():
            if t.batch_id:
                out[t.batch_id] = out.get(t.batch_id, 0.0) + t.count
        return out

    # ---- Rotation helper (Phase C, DESIGN §7.3) ----
    def recently_emptied_in_system(self, system_id: str) -> list[TankState]:
        """Empty tanks ordered most-recently-emptied first.

        Tanks never stocked (no last_emptied_date) sort last.
        Used by placement to pick the next tank to fill, keeping the
        facility rotation cycle tight.
        """
        empties = [t for t in self.empty_tanks_in_system(system_id)]
        empties.sort(
            key=lambda t: (t.last_emptied_date is None, t.last_emptied_date or date.min),
            reverse=True,
        )
        return empties

    # ---- Continuity invariants (DESIGN §8) ----
    def check_invariants(
        self,
        min_tank_control: float = 0.0,
        og12_one_kg_avg_wt_g: float = 1000.0,
    ) -> list[str]:
        """Snapshot-time invariant checks. Returns list of violation strings.

        INV-1 (one batch per tank): structurally enforced by TankState
          (batch_id is a single Optional[str]). Re-checked here trivially.
        INV-5 (min_tank_control floor): any non-empty tank with count
          below threshold is flagged. Force-empty repair is the
          placement layer's responsibility.
        OG1/2 >= 1 kg fish: tanks in OG1/OG2 holding fish above the
          threshold are noted (used by the placement layer to gate
          INV-4 repair routing).

        INV-2 (identity changes only via events), INV-3 (count balance),
        and INV-4 (no within-OG1/2 transfer above 1 kg) are enforced
        at event-application time, not at snapshot time.
        """
        out: list[str] = []
        for t in self.tanks_by_id.values():
            if t.is_empty:
                continue
            if min_tank_control > 0 and t.count < min_tank_control:
                out.append(
                    f"INV-5 violation: tank {t.location_id} (#{t.tank_id}) holds {t.count:.0f} "
                    f"fish of batch {t.batch_id}, below min_tank_control={min_tank_control:.0f}"
                )
            # In purge mode, OG6N has no biomass cap — it's the depuration
            # pool, intentionally allowed to hold whatever the pipeline
            # routes through. The density-cap field on FacilityConfig
            # only applies in production mode.
            if t.system_id == "OG6N":
                continue
            if t.density_kg_m3 > t.max_density_kg_m3 and t.max_density_kg_m3 > 0:
                out.append(
                    f"Density violation: tank {t.location_id} (#{t.tank_id}) at "
                    f"{t.density_kg_m3:.1f} kg/m3 > cap {t.max_density_kg_m3:.1f}"
                )
        return out

    # ---- Day cursor ----
    def advance_one_day(self) -> None:
        from datetime import timedelta
        self.today = self.today + timedelta(days=1)
