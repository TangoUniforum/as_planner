"""Stage 1: deterministic precalc canvas.

The full static landscape derivable from inputs, before any tank-level
placement decision. No "this batch goes in that tank" lives here — only
facts (configs), projections (biology), aggregates (per-week sums), and
detected bottlenecks (where demand exceeds supply at the canvas level).

The canvas is the single read-only input the Stage 2 optimizer consumes.
Anything the optimizer needs to know about the world should appear here.

Contents
--------
- Static: per-tank + per-system structural facts.
- Per-batch dynamic: biology projections, lifecycle markers,
  per-(batch, week) load + tank-count demand + eligible-systems set.
- Per-week aggregates: facility caps + bands, projected facility-wide
  biomass + feed, total tank demand, OG1/2 arrival demand, 6N mode.
- Operator pins: HarvestPlan / TransferPlan input rows.
- Diagnostics: bottleneck weeks, supply vs demand totals.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import NormalDist
from typing import Optional

from .caps import (
    FacilityLimits,
    METRIC_BIOMASS,
    METRIC_FEED_DAY,
    METRIC_MAX_HARVEST,
    METRIC_MIN_HARVEST,
    SystemLimits,
    apply_facility_buffer,
    resolve_facility_cap,
    resolve_system_cap,
)
from .harvest_scheduler import HarvestDemand
from .models import (
    BatchInput,
    BatchWeekState,
    BiologyTables,
    ControlParams,
    FacilityConfig,
    PinnedHarvest,
    PinnedTransfer,
    SizeClassSplit,
)
from .sixn import SIXN_PAIRS, in_transition_window, is_purge_mode
from .state import FacilityState
from .time_grid import forecast_week_labels, parse_iso_label, week_start


# Constant mirrored from events.py to avoid a circular import.
_OG12_MOVE_LOCK_WT_G = 1000.0


_OG12 = frozenset({"OG1N", "OG1S", "OG2N", "OG2S"})
_OG36 = frozenset({"OG3N", "OG3S", "OG4N", "OG4S",
                   "OG5N", "OG5S", "OG6N", "OG6S"})
_OG_ALL = _OG12 | _OG36

_STD_NORMAL = NormalDist()


def _as_date(d):
    if isinstance(d, datetime):
        return d.date()
    return d


# ============================================================
# Dataclasses — facts, projections, aggregates, diagnostics
# ============================================================

@dataclass
class TankFact:
    """Static per-tank info."""
    tank_id: int
    location_id: str
    system_id: str
    type: str                        # "FW" or "OG"
    volume_m3: float
    max_density_kg_m3: float
    max_biomass_kg: float            # = volume × max_density


@dataclass
class SystemFact:
    """Static per-system info."""
    system_id: str
    type: str                        # "FW" or "OG"
    tank_count: int
    total_max_biomass_kg: float
    tank_ids: list[int]


@dataclass
class BatchLifecycle:
    """Static lifecycle markers for one batch."""
    batch_id: str
    input_date: date
    tran_sf_date: Optional[date]
    tran_og_date: Optional[date]
    tran_og_count: Optional[int]
    tran_og_avg_wt_g: Optional[float]
    tran_og_cv_pct: float
    scheduled_cull_dates: list[date]
    fcr_model: str
    fw_correction: float
    sgr_correction: float


@dataclass
class BatchWeekFact:
    """Per-(batch, week) precalc record."""
    batch_id: str
    week_label: str
    week_start: date
    stage: str                           # "EGG" / "FW" / "SW"
    # Pre-harvest biology (Phase A inputs from the biology engine).
    count: float
    avg_wt_g: float
    biomass_kg: float
    feed_kg_day: float
    feed_kg_week: float
    cv_pct: float
    # Post-harvest projections (after applying Layer 2's per-batch demand).
    cumulative_harvested_count: float
    count_after_harvest: float
    biomass_kg_after_harvest: float
    feed_kg_day_after_harvest: float
    # Tank-count demand at the density cap.
    tanks_needed_at_density_cap: int
    # System eligibility this week (empty tuple for FW/EGG).
    eligible_systems: tuple[str, ...]
    # Lifecycle flags + harvest distribution math.
    is_tranog_week: bool
    fraction_above_min_harvest_weight: float


@dataclass
class WeeklyFacilityFact:
    """Per-week aggregated facility-wide precalc."""
    week_label: str
    week_start: date
    sixn_mode: str                       # "purge" / "transition" / "production"
    # Resolved caps + symmetric bands.
    facility_biomass_cap: Optional[float]
    facility_biomass_band: Optional[tuple[float, float]]
    facility_feed_cap: Optional[float]
    facility_feed_band: Optional[tuple[float, float]]
    max_harvest_count: Optional[float]
    min_harvest_count: Optional[float]
    # Projected facility-wide load from per-batch sums.
    projected_biomass_kg: float                  # pre-harvest sum across OG batches
    projected_biomass_kg_after_harvest: float    # post-Layer-2 demand (SW only)
    projected_biomass_kg_facility: float         # post-Layer-2, including FW pool
    projected_feed_kg_day: float                 # pre-harvest sum (SW only)
    projected_feed_kg_day_after_harvest: float   # post-Layer-2 (SW only)
    projected_feed_kg_day_facility: float        # post-Layer-2, including FW pool
    # Tank-demand aggregates.
    total_tank_demand: int
    og12_arrival_demand: int


@dataclass
class WeeklySystemFact:
    """Per-(week, system) cap snapshot."""
    week_label: str
    system_id: str
    tank_count: int                              # physical tanks (constant)
    feed_cap: Optional[float]                    # None = no cap that week
    biomass_cap: Optional[float]


@dataclass
class Bottleneck:
    """A canvas-visible supply-vs-demand gap (no placement needed to spot it)."""
    week_label: str
    system_id: Optional[str]                     # None for facility-wide
    kind: str                                    # see Bottleneck.KINDS below
    detail: str
    deficit: float

    KINDS = (
        "tank_supply",        # total OG tank demand > total OG tanks
        "og12_arrival",       # arrival demand > OG1/2 tank count
        "facility_biomass",   # projected post-harvest biomass > upper band
        "facility_feed",      # projected feed/day > upper band
        "tranog_unmet",       # TranOG arrival cannot allocate OG1/2 tanks
        "og12_residual",      # >=1 kg batch carries OG12 transit residual
        "biomass_below_band_window",  # natural carrying capacity below lower band
    )


@dataclass
class MigrationStep:
    """Precalculated per-(batch, week) tank assignment + cascade plan.

    Materializes the OG1/2 -> OG3+ migration as data instead of as a
    Phase B fallback. The tank set this week =
        keep_tanks + add_tanks  (drop_tanks were in prev_tanks, not here).

    `og12_transit_count` is the number of tanks in OG1/2 systems that
    the batch is holding as transit residual because OG3+ couldn't yet
    receive the migrants. These should shrink to 0 over subsequent weeks
    as harvest frees OG3+ capacity.
    """
    batch_id: str
    week_label: str
    keep_tanks: list[int]                # carried from prev week
    add_tanks: list[int]                 # newly allocated this week
    drop_tanks: list[int]                # released this week (fish go to add_tanks via cross-system Transfer)
    is_tranog: bool                      # week of TranOG arrival
    og12_transit_count: int = 0          # OG1/2 tanks held as transit residual
    notes: list[str] = field(default_factory=list)

    @property
    def tank_ids(self) -> list[int]:
        return sorted(set(self.keep_tanks) | set(self.add_tanks))


@dataclass
class TankAssignmentPlan:
    """Canonical per-(batch, week) tank assignment produced by the
    facility assignment coordinator.

    Sits one layer above MigrationStep: the coordinator decides WHICH
    tanks each batch occupies each week (with cross-batch coordination,
    system load balancing, and stickiness baked into the scoring), and
    MigrationStep derives the keep/add/drop diff by comparing two
    consecutive weeks' assignments. This separation lets a single
    canonical assignment drive both the migration cascade and any
    downstream consumer (Phase B/C, Phase D events, audits).

    Score breakdown is retained for diagnostics — the operator can see
    why a particular tank was assigned to a particular batch (e.g., "+800
    stickiness, -45 system feed load, +120 shortfall urgency").
    """
    batch_id: str
    week_label: str
    tank_ids: list[int]
    score_breakdown: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class PrecalcCanvas:
    """Complete Stage 1 output."""
    forecast_start: date
    horizon_weeks: int
    horizon_labels: list[str]

    tank_facts: dict[int, TankFact]
    system_facts: dict[str, SystemFact]

    batch_lifecycles: dict[str, BatchLifecycle]
    batch_week_facts: dict[tuple[str, str], BatchWeekFact]

    weekly_facility: dict[str, WeeklyFacilityFact]
    weekly_system: dict[tuple[str, str], WeeklySystemFact]

    pinned_harvests: list[PinnedHarvest]
    pinned_transfers: list[PinnedTransfer]

    bottlenecks: list[Bottleneck]
    total_og_tank_weeks_supply: int
    total_og_tank_weeks_demand: int

    # Per-(week, system) projected free OG tank counts after harvest +
    # 6N purge dynamics. Drives the migration plan below.
    tank_availability_by_week_system: dict[tuple[str, str], int] = field(default_factory=dict)

    # Per-(batch_id, week_label) cascade plan: keep/add/drop tank IDs.
    migration_plan: dict[tuple[str, str], MigrationStep] = field(default_factory=dict)

    # Per-(batch_id, week_label) canonical tank assignment produced by
    # the facility assignment coordinator. In session 1 this is built
    # but NOT consumed by _build_migration_plan — it's a parallel
    # computation for diagnostic comparison. Session 2 wires the
    # migration plan to derive its diffs from this canonical output.
    assignment_plan: dict[tuple[str, str], TankAssignmentPlan] = field(default_factory=dict)

    # Projected total facility biomass per week under FIFO min-only harvest.
    # The "natural" carrying-capacity curve — the scheduler's target ceiling.
    projected_biomass_by_week: dict[str, float] = field(default_factory=dict)

    warnings: list[str] = field(default_factory=list)


# ============================================================
# Builders
# ============================================================

def _max_kg_per_og_tank(facility: FacilityConfig) -> float:
    """Average OG-tank max biomass at density cap (kg). Used for tank-count math."""
    og = [t for t in facility.tanks if t.type == "OG"]
    if not og:
        return 0.0
    caps = [t.max_density_kg_m3 * t.volume_m3 for t in og]
    return sum(caps) / len(caps)


def _build_tank_facts(facility: FacilityConfig) -> dict[int, TankFact]:
    out: dict[int, TankFact] = {}
    for t in facility.tanks:
        out[t.tank_id] = TankFact(
            tank_id=t.tank_id,
            location_id=t.location_id,
            system_id=t.system_id,
            type=t.type,
            volume_m3=t.volume_m3,
            max_density_kg_m3=t.max_density_kg_m3,
            max_biomass_kg=t.volume_m3 * t.max_density_kg_m3,
        )
    return out


def _build_system_facts(facility: FacilityConfig) -> dict[str, SystemFact]:
    grouped: dict[str, list] = {}
    for t in facility.tanks:
        grouped.setdefault(t.system_id, []).append(t)
    out: dict[str, SystemFact] = {}
    for sys, tanks in grouped.items():
        total = sum(t.volume_m3 * t.max_density_kg_m3 for t in tanks)
        out[sys] = SystemFact(
            system_id=sys,
            type=tanks[0].type,
            tank_count=len(tanks),
            total_max_biomass_kg=total,
            tank_ids=sorted(t.tank_id for t in tanks),
        )
    return out


def _build_batch_lifecycles(
    batches: list[BatchInput],
    tables: BiologyTables,
) -> dict[str, BatchLifecycle]:
    out: dict[str, BatchLifecycle] = {}
    for b in batches:
        if b.input_date is None:
            continue
        input_d = _as_date(b.input_date)
        cull_dates = [
            input_d + timedelta(days=dsi)
            for dsi, pct in tables.culling if pct > 0
        ]
        out[b.batch_id] = BatchLifecycle(
            batch_id=b.batch_id,
            input_date=input_d,
            tran_sf_date=_as_date(b.tran_sf_date) if b.tran_sf_date else None,
            tran_og_date=_as_date(b.tran_og_date) if b.tran_og_date else None,
            tran_og_count=b.tran_og_count,
            tran_og_avg_wt_g=b.tran_og_avg_wt_g,
            tran_og_cv_pct=b.tran_og_cv,
            scheduled_cull_dates=cull_dates,
            fcr_model=b.fcr_model,
            fw_correction=b.fw_correction,
            sgr_correction=b.sgr_correction,
        )
    return out


def _fraction_above(min_weight_g: float, avg_wt_g: float, cv_pct: float) -> float:
    """Fraction of a normal(mu=avg_wt, sd=avg_wt*cv/100) distribution above
    `min_weight_g`. Bounded to [0, 1]."""
    if avg_wt_g <= 0:
        return 0.0
    if cv_pct <= 0:
        return 1.0 if avg_wt_g >= min_weight_g else 0.0
    sigma = avg_wt_g * cv_pct / 100.0
    z = (min_weight_g - avg_wt_g) / sigma
    return max(0.0, min(1.0, 1.0 - _STD_NORMAL.cdf(z)))


def _build_batch_week_facts(
    biology_states_by_batch: dict[str, list[BatchWeekState]],
    harvest_demands: list[HarvestDemand],
    splits: list[SizeClassSplit],
    batches: list[BatchInput],
    control: ControlParams,
    facility: FacilityConfig,
) -> dict[tuple[str, str], BatchWeekFact]:
    """Compute per-(batch, week) precalc facts."""
    cv_by_batch = {b.batch_id: b.tran_og_cv for b in batches}
    tranog_dates = {s.batch_id: _as_date(s.tran_og_date) for s in splits}

    harvest_by_bw: dict[tuple[str, str], float] = {}
    for d in harvest_demands:
        key = (d.batch_id, d.week_label)
        harvest_by_bw[key] = harvest_by_bw.get(key, 0.0) + d.count

    max_kg = _max_kg_per_og_tank(facility)
    min_h_wt = control.min_harvest_weight_g

    # Density-aware sizing: each tank packed to 85% of cap, leaving 15%
    # growth headroom. Same threshold as runtime Grade-split trigger so
    # precalc and Phase D agree.
    #
    # Per-week sizing is followed by a backward lifetime-max sweep so
    # each week's tanks_needed reflects the projected biomass peak over
    # the remaining lifecycle (until next harvest-driven shrink). This
    # is the precalc-first answer: the migration plan claims all
    # lifecycle-required tanks upfront and the sticky-floor in
    # _build_migration_plan keeps them allocated. No runtime lookahead
    # needed — the canonical tank trajectory is decided once at canvas
    # build time. Combined with the OG12 1 kg rule, this lets the
    # migration plan split cohorts BEFORE 1 kg (when intra-OG12 moves
    # are still legal) instead of waiting for density to cross the
    # 80.75 kg/m^3 trigger when the lock is already engaged.
    DENSITY_TARGET_PCT = 0.85
    effective_max_kg = max_kg * DENSITY_TARGET_PCT

    out: dict[tuple[str, str], BatchWeekFact] = {}
    for batch_id, states in biology_states_by_batch.items():
        states_sorted = sorted(states, key=lambda s: s.week_label)
        cv = cv_by_batch.get(batch_id, 16.0)
        tranog_date = tranog_dates.get(batch_id)
        cum = 0.0
        for s in states_sorted:
            cum += harvest_by_bw.get((batch_id, s.week_label), 0.0)
            survive_ratio = max(0.0, 1.0 - cum / s.count) if s.count > 0 else 0.0
            count_post = s.count * survive_ratio
            biomass_post = s.biomass_kg * survive_ratio
            feed_post = s.feed_kg_day * survive_ratio

            ws_date = _as_date(s.week_start)
            is_tranog = (
                tranog_date is not None
                and ws_date <= tranog_date < ws_date + timedelta(days=7)
            )
            if s.stage == "SW":
                # System-progression law: sub-1 kg fish live in the OG1/2
                # nursery; at 1 kg they MUST exit to the OG3-6 grow-out
                # systems (saved rule: "fish must exit OG1/2 at 1 kg").
                # Previously this used _OG_ALL for non-TranOG SW weeks,
                # which left over-1 kg fish eligible for OG1/2 — they then
                # overstayed (sticky) and clogged the 12-tank nursery,
                # tipping density. Eligibility now enforces the exit.
                if s.avg_weight_g >= _OG12_MOVE_LOCK_WT_G:
                    eligible = tuple(sorted(_OG36))   # grow-out
                else:
                    eligible = tuple(sorted(_OG12))   # nursery (incl. TranOG)
            else:
                eligible = ()  # FW / EGG live in the FW pool, not OG tanks

            if s.stage == "SW" and biomass_post > 0 and effective_max_kg > 0:
                tanks_needed = max(1, math.ceil(biomass_post / effective_max_kg))
            else:
                tanks_needed = 0
            # TranOG arrival weeks need >=4 OG1/2 tanks so the SizeClassSplit
            # (big + small) can each get >=2 tanks. Density math + size-class
            # operational management both require this minimum.
            if is_tranog and tanks_needed > 0:
                tanks_needed = max(
                    tanks_needed,
                    max(4, control.tran_og_default_tanks or 3),
                )

            frac_above = (
                _fraction_above(min_h_wt, s.avg_weight_g, cv)
                if s.stage == "SW" else 0.0
            )

            out[(batch_id, s.week_label)] = BatchWeekFact(
                batch_id=batch_id,
                week_label=s.week_label,
                week_start=ws_date,
                stage=s.stage,
                count=s.count,
                avg_wt_g=s.avg_weight_g,
                biomass_kg=s.biomass_kg,
                feed_kg_day=s.feed_kg_day,
                feed_kg_week=s.feed_kg_week,
                cv_pct=cv,
                cumulative_harvested_count=cum,
                count_after_harvest=count_post,
                biomass_kg_after_harvest=biomass_post,
                feed_kg_day_after_harvest=feed_post,
                tanks_needed_at_density_cap=tanks_needed,
                eligible_systems=eligible,
                is_tranog_week=is_tranog,
                fraction_above_min_harvest_weight=frac_above,
            )

        # NOTE: lifetime-max backward sweep REMOVED 2026-05-28. It was
        # added to claim OG1/2 tanks before the 1 kg lock engaged, but
        # the exit-at-1 kg enforcement (eligibility flips to OG3-6 at
        # 1 kg + EVT_MIGRATE) makes it unnecessary: fish leave OG1/2 at
        # 1 kg into OG3-6, where any-to-any transfer is allowed (no
        # lock, DESIGN §4). A grow-out batch therefore ADDS tanks
        # incrementally as biomass climbs (GROWTH_ADD), so per-week
        # sizing is correct. Lifetime-max caused every pre-existing
        # >1 kg batch to demand its peak tank count simultaneously at
        # W20, over-subscribing OG3-6 (4 batches x 8 tanks > 21) and
        # starving them to 2 tanks each -> 136k fish/tank violations.
        # Per-week sizing + incremental adds avoids that.

    return out


def _build_weekly_facility_facts(
    horizon_labels: list[str],
    forecast_start: date,
    control: ControlParams,
    facility_limits: FacilityLimits,
    batch_week_facts: dict[tuple[str, str], BatchWeekFact],
) -> dict[str, WeeklyFacilityFact]:
    out: dict[str, WeeklyFacilityFact] = {}
    # Group facts by week.
    facts_by_week: dict[str, list[BatchWeekFact]] = {}
    for (_, label), f in batch_week_facts.items():
        facts_by_week.setdefault(label, []).append(f)

    for label in horizon_labels:
        facts = facts_by_week.get(label, [])
        if facts:
            w_start = facts[0].week_start
        else:
            w_start = parse_iso_label(label) or forecast_start

        bio_cap = resolve_facility_cap(METRIC_BIOMASS, label, facility_limits, control)
        bio_band = apply_facility_buffer(bio_cap, METRIC_BIOMASS, control) if bio_cap else None
        feed_cap = resolve_facility_cap(METRIC_FEED_DAY, label, facility_limits, control)
        feed_band = apply_facility_buffer(feed_cap, METRIC_FEED_DAY, control) if feed_cap else None
        max_h = resolve_facility_cap(METRIC_MAX_HARVEST, label, facility_limits, control)
        min_h = resolve_facility_cap(METRIC_MIN_HARVEST, label, facility_limits, control)

        if is_purge_mode(control, w_start):
            sixn = "purge"
        elif in_transition_window(control, w_start):
            sixn = "transition"
        else:
            sixn = "production"

        og_facts = [f for f in facts if f.stage == "SW"]
        fw_facts = [f for f in facts if f.stage in ("FW", "EGG")]
        proj_bio = sum(f.biomass_kg for f in og_facts)
        proj_bio_post = sum(f.biomass_kg_after_harvest for f in og_facts)
        proj_feed = sum(f.feed_kg_day for f in og_facts)
        # Post-harvest feed scales with surviving count fraction.
        proj_feed_post = sum(f.feed_kg_day_after_harvest for f in og_facts)
        # Facility-wide totals fold in FW pool biomass + feed
        # (FW counts toward facility caps even though it's not in OG tanks).
        fw_bio = sum(f.biomass_kg for f in fw_facts)
        fw_feed = sum(f.feed_kg_day for f in fw_facts)
        tank_demand = sum(f.tanks_needed_at_density_cap for f in og_facts)
        og12_arrival = sum(
            f.tanks_needed_at_density_cap for f in og_facts if f.is_tranog_week
        )

        out[label] = WeeklyFacilityFact(
            week_label=label,
            week_start=w_start,
            sixn_mode=sixn,
            facility_biomass_cap=bio_cap,
            facility_biomass_band=bio_band,
            facility_feed_cap=feed_cap,
            facility_feed_band=feed_band,
            max_harvest_count=max_h,
            min_harvest_count=min_h,
            projected_biomass_kg=proj_bio,
            projected_biomass_kg_after_harvest=proj_bio_post,
            projected_biomass_kg_facility=proj_bio_post + fw_bio,
            projected_feed_kg_day=proj_feed,
            projected_feed_kg_day_after_harvest=proj_feed_post,
            projected_feed_kg_day_facility=proj_feed_post + fw_feed,
            total_tank_demand=tank_demand,
            og12_arrival_demand=og12_arrival,
        )
    return out


def _build_weekly_system_facts(
    horizon_labels: list[str],
    system_limits: SystemLimits,
    system_facts: dict[str, SystemFact],
) -> dict[tuple[str, str], WeeklySystemFact]:
    out: dict[tuple[str, str], WeeklySystemFact] = {}
    og_systems = [s for s, sf in system_facts.items() if sf.type == "OG"]
    for label in horizon_labels:
        for sys in og_systems:
            out[(label, sys)] = WeeklySystemFact(
                week_label=label,
                system_id=sys,
                tank_count=system_facts[sys].tank_count,
                feed_cap=resolve_system_cap(METRIC_FEED_DAY, label, sys, system_limits),
                biomass_cap=resolve_system_cap(METRIC_BIOMASS, label, sys, system_limits),
            )
    return out


def _detect_bottlenecks(
    weekly_facility: dict[str, WeeklyFacilityFact],
    system_facts: dict[str, SystemFact],
) -> list[Bottleneck]:
    """Spot supply-vs-demand gaps from the canvas alone (no placement run)."""
    out: list[Bottleneck] = []
    total_og_tanks = sum(sf.tank_count for sf in system_facts.values() if sf.type == "OG")
    og12_tanks = sum(
        sf.tank_count for sf in system_facts.values() if sf.system_id in _OG12
    )

    for label, wff in sorted(weekly_facility.items()):
        if wff.total_tank_demand > total_og_tanks:
            out.append(Bottleneck(
                week_label=label, system_id=None, kind="tank_supply",
                detail=(f"OG tank demand {wff.total_tank_demand} > "
                        f"facility supply {total_og_tanks}"),
                deficit=wff.total_tank_demand - total_og_tanks,
            ))
        if wff.og12_arrival_demand > og12_tanks:
            out.append(Bottleneck(
                week_label=label, system_id=None, kind="og12_arrival",
                detail=(f"OG1/2 arrival demand {wff.og12_arrival_demand} > "
                        f"OG1/2 capacity {og12_tanks}"),
                deficit=wff.og12_arrival_demand - og12_tanks,
            ))
        if (wff.facility_biomass_band
                and wff.projected_biomass_kg_facility > wff.facility_biomass_band[1]):
            out.append(Bottleneck(
                week_label=label, system_id=None, kind="facility_biomass",
                detail=(f"facility biomass (SW post-harvest + FW pool) "
                        f"{wff.projected_biomass_kg_facility:,.0f} kg > "
                        f"upper band {wff.facility_biomass_band[1]:,.0f} kg"),
                deficit=wff.projected_biomass_kg_facility
                        - wff.facility_biomass_band[1],
            ))
        if (wff.facility_feed_band
                and wff.projected_feed_kg_day_facility > wff.facility_feed_band[1]):
            out.append(Bottleneck(
                week_label=label, system_id=None, kind="facility_feed",
                detail=(f"facility feed/day "
                        f"{wff.projected_feed_kg_day_facility:,.0f} kg > "
                        f"upper band {wff.facility_feed_band[1]:,.0f} kg"),
                deficit=wff.projected_feed_kg_day_facility - wff.facility_feed_band[1],
            ))
    return out


def _build_facility_assignment_plan(
    horizon_labels: list[str],
    initial_state: FacilityState,
    facility: FacilityConfig,
    batch_week_facts: dict[tuple[str, str], BatchWeekFact],
    batch_lifecycles: dict[str, BatchLifecycle],
    harvest_demands: list[HarvestDemand],
    weekly_facility: dict[str, WeeklyFacilityFact],
    bottlenecks: list[Bottleneck],
    control: Optional[ControlParams] = None,
) -> dict[tuple[str, str], TankAssignmentPlan]:
    """Coordinated per-(batch, week) tank assignment via score-based greedy.

    THIS IS THE PROJECT'S PRECALC-FIRST FACILITY COORDINATOR.

    The original `_build_migration_plan` does per-week assignment with
    FIFO-oldest-first batch ordering — greedy on age, with no
    cross-batch demand balancing, no system load awareness, and no
    transfer-minimization objective. That worked when the facility was
    over-subscribed in only a narrow way, but on this workbook it
    leaves the youngest cohort (B47) with zero tank adds at W20
    because B41-B45 (older) consumed the free pool first.

    This coordinator replaces FIFO with a score-based greedy that
    explicitly weighs the four objectives the project lead has
    articulated:

      1. Each batch's per-week tank count is met (sized via lifetime-
         max in `_build_batch_week_facts`).
      2. Tanks are distributed across systems so per-system biomass +
         feed load is even (no system pinned at cap while others sit
         half-empty).
      3. Week-over-week tank stickiness is maximized (minimizes the
         number of Transfer events Phase D has to emit).
      4. Operational rules are honored as HARD filters: TranOG arrivals
         go to OG1/2 (with OG3+ overflow), ≥1 kg batches must end up
         in OG3+, 6N pipeline tanks are pipeline-owned, etc.

    Algorithm (deterministic single-pass forward walk):

      For each week W in chronological order:
        a. Settle biomass-driven drain: read harvest_demands and the
           harvest scheduler's per-batch biomass targets to determine
           which tanks fully empty this week. The actual drain logic
           lives in Phase D — here we only project the resulting tank
           state from biomass-level decisions (NOT picking specific
           tanks to drain). Per project lead: "we should do that
           based purely on biomass... not from an actual tank
           perspective because that will fall apart as the larger
           tanks get emptied and the smaller tanks get filled."
        b. Snapshot free_pool[sys] = OG tanks not occupied by any
           batch's prev assignment (excluding OG6N pipeline-owned).
        c. Build per-batch demand: needed = fact.tanks_needed_at_density_cap.
           Each batch has prev_tanks from W-1's assignment plus
           eligibility set (OG12 for TranOG, OG3+ for ≥1 kg, OG_ALL
           otherwise).
        d. Score-based assignment:
           For each (batch, candidate_tank) pair where:
             - tank is in batch's eligible_systems
             - tank is in prev OR free_pool
           compute score = sum of:
             + STICKY_BONUS (1000) if tank in prev_tanks (transfer
               minimization)
             + SHORTFALL_URGENCY * (needed - assigned_so_far) /
               needed (cross-batch fairness — under-allocated
               batches priced higher)
             - SYSTEM_LOAD_PENALTY * (current_system_biomass /
               system_biomass_cap) (system load balance)
             - SYSTEM_FEED_PENALTY * (current_system_feed /
               system_feed_cap) (system feed balance)
           Sort all (batch, tank) candidates by score desc, assign
           top-scoring pairs greedily, recompute scores when a
           batch's assigned count or a system's load changes.
        e. Emit TankAssignmentPlan per batch with the resulting
           tank set and score_breakdown for diagnostics.

    Why deterministic greedy not LP: the project's saved feedback
    rules ("precalc-first... leave as much as possible to algorithm
    and as little as possible to optimization") plus the need for the
    plan to be defendable per the Phase B2 dual-assert pattern. A
    deterministic algorithm produces the same plan every run and the
    score breakdown explains every assignment.

    SESSION 1 SCOPE (this file revision):
      - Data structure + function signature + algorithm spec.
      - Function builds an empty plan; PrecalcCanvas stores it but
        `_build_migration_plan` does NOT yet consume it. The existing
        migration plan + Phase D output are unchanged. Baseline
        density violation count = 324 (regression check).

    SESSION 2: implement the score function + greedy loop and produce
    a real assignment plan, still parallel to the existing migration
    plan (diagnostic only — both run, side-by-side comparison).

    SESSION 3: wire `_build_migration_plan` to derive its keep/add/
    drop diffs from the assignment plan. Migration plan becomes a
    thin diff layer.

    SESSION 4: 6N purge interaction + TranOG branch + edge cases.

    SESSION 5: regression suite + docs + golden-cell update.
    """
    plan: dict[tuple[str, str], TankAssignmentPlan] = {}

    # ---- Setup ----
    tank_to_system: dict[int, str] = {}
    for t in facility.tanks:
        if t.type == "OG":
            tank_to_system[t.tank_id] = t.system_id
    og12_systems = frozenset({"OG1N", "OG1S", "OG2N", "OG2S"})
    sixn_systems = frozenset({"OG6N"})
    all_og_tanks = sorted(tank_to_system.keys())

    # ---- Step 1: build per-batch tank-count timelines ----
    # For each batch, compress per-week tanks_needed into transitions
    # (week, count) where count changes from the previous week.
    timelines: dict[str, list[tuple[str, int]]] = {}
    for bid in batch_lifecycles:
        weeks_for_batch = [
            (wl, batch_week_facts[(bid, wl)])
            for (b, wl) in batch_week_facts
            if b == bid and batch_week_facts[(b, wl)].stage == "SW"
        ]
        weeks_for_batch.sort(key=lambda x: x[0])
        prev_count = None
        transitions: list[tuple[str, int]] = []
        for wl, fact in weeks_for_batch:
            cur_count = max(0, fact.tanks_needed_at_density_cap)
            if prev_count is None or cur_count != prev_count:
                transitions.append((wl, cur_count))
                prev_count = cur_count
        if transitions:
            timelines[bid] = transitions

    # ---- Step 2: initialize per-batch tank sets from PR ----
    # `current_tanks[bid]` = the set of tank_ids the batch occupies as of
    # the most recently processed event. Initial state comes from PR.
    current_tanks: dict[str, set[int]] = {}
    for tid, tank in initial_state.tanks_by_id.items():
        if tank.batch_id and tank.type == "OG":
            current_tanks.setdefault(tank.batch_id, set()).add(tid)

    # `tank_owner_by_week[wk][tid]` = the batch holding tid AT week wk in
    # the assignment plan. Each week starts as a copy of the previous
    # week's ownership, then events for that week mutate it.
    # Implementation: track ownership as a single dict that's mutated in
    # chronological order; each event commits state for its week.
    tank_owner: dict[int, Optional[str]] = {t: None for t in all_og_tanks}
    for bid, tanks in current_tanks.items():
        for tid in tanks:
            tank_owner[tid] = bid

    # ---- Step 3: build the event list ----
    # Event types and priorities (lower = processed first within a week):
    #   PR_INIT (0)         — pre-existing batch's starting tank set
    #   TRANOG_ARRIVAL (1)  — hard placement into OG12 (with OG3+ overflow)
    #   HARVEST_RELEASE (2) — batch shrinks; releases tanks BEFORE adds
    #   GROWTH_ADD (3)      — batch grows; claims newly-freed + free pool
    EVT_PR_INIT = 0
    EVT_PR_REBALANCE = 1   # release surplus tanks at forecast start so they
                            # rejoin the free pool for under-allocated batches
    EVT_MIGRATE = 2        # exit OG1/2 -> OG3-6 at the 1 kg crossing; frees
                            # nursery tanks BEFORE TranOG arrivals claim them
    EVT_TRANOG = 3
    EVT_RELEASE = 4
    EVT_ADD = 5

    events: list[tuple] = []
    # (week_label, type_priority, batch_id, delta, eligibility_tuple)

    # PR_INIT events — synthesized at first horizon week for each
    # pre-existing batch.
    first_week = horizon_labels[0] if horizon_labels else None
    if first_week is not None:
        for bid, tanks in sorted(current_tanks.items()):
            events.append((first_week, EVT_PR_INIT, bid, len(tanks),
                           tuple(sorted(tanks))))
            # PR_REBALANCE: if PR over-allocates a batch beyond its
            # precalc lifetime-peak need at the first SW week, release
            # the surplus tanks so they're available for under-allocated
            # batches' GROWTH_ADD events the same week. Operator-side
            # PR can be arbitrary; the forecast normalizes to the
            # canonical plan at horizon start (the only week where we
            # treat the tank set as malleable — sticky-floor applies
            # from week 2 onward).
            first_sw_fact = batch_week_facts.get((bid, first_week))
            if first_sw_fact is not None and first_sw_fact.stage == "SW":
                needed_now = max(0, first_sw_fact.tanks_needed_at_density_cap)
                surplus = len(tanks) - needed_now
                if surplus > 0:
                    events.append((first_week, EVT_PR_REBALANCE, bid,
                                   surplus, ()))

    # Per-week TARGET (EVT_ADD) + shrink (EVT_RELEASE) + TranOG events.
    # CONTENTION-RESILIENT: a TARGET event fires EVERY SW week carrying
    # the absolute needed tank count (not a one-shot delta at
    # transitions). The EVT_ADD handler tops the batch up TOWARD that
    # target each week — so a batch under-allocated because the eligible
    # pool was temporarily full keeps acquiring tanks in following weeks
    # as harvests/migrations free capacity (incremental catch-up). This
    # is still precalc-first: every week's target is the deterministic
    # per-week density requirement; the handler only ever ADDS toward it
    # (sticky — never shrinks here). Harvest-driven shrinks are explicit
    # EVT_RELEASE events when the per-week need drops.
    for bid in sorted(timelines.keys()):
        bw = sorted(
            ((wl, batch_week_facts[(bid, wl)]) for (b, wl) in batch_week_facts
             if b == bid and batch_week_facts[(b, wl)].stage == "SW"),
            key=lambda x: x[0],
        )
        prev_needed = len(current_tanks.get(bid, set()))
        for i, (wl, fact) in enumerate(bw):
            needed = max(0, fact.tanks_needed_at_density_cap)
            if fact.is_tranog_week and i == 0:
                # First SW week is TranOG arrival; hard placement.
                events.append((wl, EVT_TRANOG, bid, needed,
                               fact.eligible_systems))
                prev_needed = needed
                continue
            # Harvest-driven shrink: per-week need dropped.
            if needed < prev_needed:
                events.append((wl, EVT_RELEASE, bid, prev_needed - needed,
                               fact.eligible_systems))
            # Top-up toward the absolute target every week (catch-up).
            events.append((wl, EVT_ADD, bid, needed, fact.eligible_systems))
            prev_needed = needed

    # MIGRATE events: fire EVERY SW week a batch is at/above 1 kg, not
    # just the crossing. The handler does a 1:1 OG1/2 -> OG3-6 swap for
    # each OG1/2 tank the batch still holds (claim a free OG3-6 tank,
    # release the OG1/2 tank). Firing every week makes the drain
    # contention-resilient: if OG3-6 is full at the crossing week, the
    # batch keeps swapping in following weeks as grow-out tanks free up,
    # instead of being stuck in the nursery. Handler is a no-op once the
    # batch holds no OG1/2 tanks.
    for bid in sorted(timelines.keys()):
        bw = sorted(
            ((wl, batch_week_facts[(bid, wl)]) for (b, wl) in batch_week_facts
             if b == bid and batch_week_facts[(b, wl)].stage == "SW"),
            key=lambda x: x[0],
        )
        for wl, fact in bw:
            if fact.avg_wt_g >= _OG12_MOVE_LOCK_WT_G:
                events.append((wl, EVT_MIGRATE, bid,
                               max(1, fact.tanks_needed_at_density_cap),
                               fact.eligible_systems))

    # ---- Step 4: sort events chronologically by FIFO age ----
    # Rules:
    #   (1) Week ascending — natural time order.
    #   (2) Type priority ascending — PR_INIT, PR_REBALANCE, TRANOG,
    #       RELEASE, ADD (releases happen BEFORE adds in the same week
    #       so freed tanks rejoin the pool).
    #   (3) Within the same (week, type): FIFO by batch input_date
    #       ascending — oldest batch processed first. This is an
    #       operational fact (the operator stocked B41 before B47),
    #       not a tuning choice.
    #   (4) batch_id alphabetical as deterministic tiebreak.
    fifo_age: dict[str, int] = {}
    for i, bid in enumerate(sorted(
        batch_lifecycles.keys(),
        key=lambda b: (
            batch_lifecycles[b].input_date or date.max,
            b,
        ),
    )):
        fifo_age[bid] = i
    def _sort_key(e):
        wk, etype, bid, _delta, _elig = e
        return (wk, etype, fifo_age.get(bid, 1_000_000), bid)
    events.sort(key=_sort_key)

    # ---- Step 5: process events; record per-(batch, week) assignment ----
    # assignment[(bid, wl)] = tank_ids set at end of wl after all events
    # for wl. We track a running per-batch tank set and snapshot it on
    # every transition.
    assignment: dict[tuple[str, str], list[int]] = {}

    def _free_pool_in_systems(sys_set: set[str]) -> list[int]:
        return sorted(
            tid for tid in all_og_tanks
            if tank_owner[tid] is None
            and tank_to_system.get(tid) in sys_set
            and tank_to_system.get(tid) not in sixn_systems
        )

    notes_per_pair: dict[tuple[str, str], list[str]] = {}

    # Per-system tank ID lookup (sorted ascending) — used for the
    # deterministic tank-pick rule: lowest-numbered free tank in the
    # chosen system.
    og_tank_ids_by_system: dict[str, list[int]] = {}
    for tid, sys in tank_to_system.items():
        og_tank_ids_by_system.setdefault(sys, []).append(tid)
    for sys in og_tank_ids_by_system:
        og_tank_ids_by_system[sys].sort()

    # ---- Forward-peak system routing (batch staggering) ----
    # Per-cohort per-tank biomass curve over the horizon: the biomass
    # ONE tank of this batch carries at each week (= post-harvest
    # biomass / planned tank count). Used to route each grow-out
    # tank-pick to the system that minimizes the RESULTING peak system
    # biomass — so cohorts with overlapping peaks land in different
    # systems and each system's load stays level over time. This is the
    # staggering the facility wants, discovered arithmetically from the
    # known forward biology curves. Single operational criterion
    # (minimize peak system biomass), not a scoring weight.
    H = len(horizon_labels)
    week_idx = {wl: i for i, wl in enumerate(horizon_labels)}
    per_tank_curve: dict[str, list[float]] = {}
    for (b, wl), f in batch_week_facts.items():
        if f.stage != "SW" or wl not in week_idx:
            continue
        ptc = per_tank_curve.setdefault(b, [0.0] * H)
        ptc[week_idx[wl]] = (
            f.biomass_kg_after_harvest / max(1, f.tanks_needed_at_density_cap)
        )

    def _system_load_curve(sys: str) -> list[float]:
        curve = [0.0] * H
        for t in og_tank_ids_by_system.get(sys, []):
            owner = tank_owner[t]
            if owner is None:
                continue
            ptc = per_tank_curve.get(owner)
            if ptc is None:
                continue
            for w in range(H):
                curve[w] += ptc[w]
        return curve

    def _route_systems(candidate_systems, cohort_bid: str) -> list[str]:
        # Order eligible systems by the peak system biomass that WOULD
        # result from adding one tank of `cohort_bid` — lowest first.
        # Tiebreak by system_id for determinism. This produces
        # staggering: a cohort avoids systems already peaking when it
        # peaks. (Per-tank granularity naturally handles multi-system
        # splits — each successive tank re-evaluates the updated load.)
        cohort_ptc = per_tank_curve.get(cohort_bid, [0.0] * H)
        scored = []
        for s in candidate_systems:
            if s in sixn_systems:
                continue
            load = _system_load_curve(s)
            peak = max(load[w] + cohort_ptc[w] for w in range(H)) if H else 0.0
            scored.append((peak, s))
        scored.sort()
        return [s for _, s in scored]

    for ev in events:
        wl, etype, bid, delta, elig = ev
        eligible_set = set(elig) if elig else set()
        batch_set = current_tanks.setdefault(bid, set())
        notes = notes_per_pair.setdefault((bid, wl), [])

        if etype == EVT_PR_INIT:
            # Batch already populated in current_tanks from PR; nothing to
            # do beyond recording the assignment for the first week.
            pass

        elif etype == EVT_PR_REBALANCE:
            # Release `delta` surplus tanks from this batch's PR set.
            # Release from OG3+ first (cheapest to give back; OG1/2
            # tanks are the constrained resource for TranOG arrivals);
            # within a system, prefer highest tank_id (newer additions).
            candidates = sorted(
                batch_set,
                key=lambda tid: (
                    0 if tank_to_system.get(tid) not in og12_systems else 1,
                    -tid,
                ),
            )
            for tid in candidates[:delta]:
                tank_owner[tid] = None
                batch_set.discard(tid)
            notes.append(
                f"PR_REBALANCE: released {delta} surplus tank(s) "
                f"(PR had {len(candidates)}, precalc needs {len(candidates) - delta})"
            )

        elif etype == EVT_MIGRATE:
            # 1:1 swap OG1/2 -> OG3-6 for each OG1/2 tank the batch still
            # holds while at/above 1 kg. For each, claim a free OG3-6
            # tank and release the OG1/2 tank (total tank count constant,
            # so no transient over-concentration). If the OG3-6 pool is
            # short, stop — remaining OG1/2 tanks stay and the next
            # weekly MIGRATE retries. Phase D emits the cross-system
            # Transfer from the keep/add/drop diff. EVT_ADD (same week,
            # later priority) then tops the OG3-6 count up to `delta`
            # needed. Deterministic pick (alphabetical system, lowest
            # free tank_id) — no scoring.
            og12_held = sorted(t for t in batch_set
                               if tank_to_system.get(t) in og12_systems)
            swapped = 0
            for og12_tid in og12_held:
                claimed = None
                # Land exiting fish in the grow-out system that minimizes
                # resulting peak system biomass (staggering), not
                # alphabetically — so exit-at-1 kg levels OG3-6 load.
                for sys in _route_systems(eligible_set, bid):
                    if sys in sixn_systems:
                        continue
                    for t in og_tank_ids_by_system.get(sys, []):
                        if tank_owner[t] is None:
                            claimed = t
                            break
                    if claimed is not None:
                        break
                if claimed is None:
                    break  # OG3-6 pool short; retry next weekly MIGRATE
                tank_owner[claimed] = bid
                batch_set.add(claimed)
                tank_owner[og12_tid] = None
                batch_set.discard(og12_tid)
                swapped += 1
            if swapped:
                remaining = len(og12_held) - swapped
                notes.append(
                    f"MIGRATE: swapped {swapped} OG1/2->OG3-6 tank(s) at 1 kg"
                    + (f"; {remaining} OG1/2 left (pool short, retry next week)"
                       if remaining else "")
                )

        elif etype == EVT_TRANOG:
            # Hard placement: pick `delta` empty OG12 tanks; cascade to
            # OG3+ if OG12 insufficient (density-preservation overflow).
            og12_free = _free_pool_in_systems(set(og12_systems))
            picked = list(og12_free[:delta])
            if len(picked) < delta:
                og3_systems = {s for s in eligible_set
                               if s not in og12_systems
                               and s not in sixn_systems}
                if not og3_systems:
                    og3_systems = {"OG3N", "OG3S", "OG4N", "OG4S",
                                   "OG5N", "OG5S", "OG6S"}
                og3_free = _free_pool_in_systems(og3_systems)
                picked.extend(og3_free[:delta - len(picked)])
                if len(picked) > len(og12_free):
                    notes.append(
                        f"TranOG overflow to OG3+: {len(og12_free)}/{delta} "
                        f"in OG1/2, remainder in OG3+"
                    )
            if len(picked) < delta:
                bottlenecks.append(Bottleneck(
                    week_label=wl,
                    system_id=None,
                    kind="assignment_tranog_unmet",
                    detail=(f"TranOG {bid} needs {delta} tanks; "
                            f"only {len(picked)} free"),
                    deficit=delta - len(picked),
                ))
            for tid in picked:
                tank_owner[tid] = bid
                batch_set.add(tid)

        elif etype == EVT_RELEASE:
            # Shrink: pick `delta` tanks from batch's current set to
            # release. Pick from OG1/2 first (transit residuals), then
            # by largest tank_id (newest-added tanks shed first).
            candidates = sorted(
                batch_set,
                key=lambda tid: (
                    0 if tank_to_system.get(tid) in og12_systems else 1,
                    -tid,
                ),
            )
            for tid in candidates[:delta]:
                tank_owner[tid] = None
                batch_set.discard(tid)

        elif etype == EVT_ADD:
            # TOP-UP toward absolute target `delta` (= this week's needed
            # tank count). Counts only the batch's tanks in ELIGIBLE
            # systems toward the target — so a batch that hasn't fully
            # migrated (still holds OG1/2 tanks while eligible is OG3-6)
            # keeps acquiring grow-out tanks until it reaches target.
            # This is the contention catch-up: under-allocation in one
            # week is topped up in later weeks as the pool frees.
            # DETERMINISTIC RULES (no scoring):
            #   (1) prefer systems the batch already occupies (sticky);
            #   (2) other eligible systems alphabetically;
            #   (3) lowest-numbered free tank within a system.
            eligible_held = sum(
                1 for t in batch_set
                if tank_to_system.get(t) in eligible_set
            )
            shortfall = delta - eligible_held
            if shortfall > 0:
                current_systems = {tank_to_system.get(tid) for tid in batch_set}
                current_systems.discard(None)
                # Sticky first (own systems, no transfer); then NEW systems
                # ordered by forward-peak (staggering — level system load).
                preferred_systems = sorted(current_systems & eligible_set)
                other_systems = _route_systems(
                    eligible_set - set(preferred_systems), bid)
                picked = []
                for sys in preferred_systems + other_systems:
                    if len(picked) >= shortfall:
                        break
                    if sys in sixn_systems:
                        continue
                    for t in og_tank_ids_by_system.get(sys, []):
                        if len(picked) >= shortfall:
                            break
                        if tank_owner[t] is None:
                            picked.append(t)
                for tid in picked:
                    tank_owner[tid] = bid
                    batch_set.add(tid)
                if len(picked) < shortfall:
                    notes.append(
                        f"under target: need {delta} eligible tanks, have "
                        f"{eligible_held + len(picked)} (pool short this week)"
                    )

        # Record this batch's assignment AT this week (post-event).
        assignment[(bid, wl)] = sorted(batch_set)

    # ---- Step 6: forward-fill assignments for non-transition weeks ----
    # Between two transitions of the same batch, the assignment is
    # constant — propagate the most-recent assignment forward to each
    # horizon week where the batch is active.
    by_batch: dict[str, list[tuple[str, list[int]]]] = {}
    for (bid, wl), tids in assignment.items():
        by_batch.setdefault(bid, []).append((wl, tids))
    for bid in by_batch:
        by_batch[bid].sort(key=lambda x: x[0])

    horizon_index = {wl: i for i, wl in enumerate(horizon_labels)}
    for bid, snapshots in by_batch.items():
        next_idx = 0
        current_set: list[int] = []
        for wl in horizon_labels:
            if (next_idx < len(snapshots)
                    and snapshots[next_idx][0] == wl):
                current_set = snapshots[next_idx][1]
                next_idx += 1
            # Only emit if batch is active this week (has a SW fact).
            fact = batch_week_facts.get((bid, wl))
            if fact is None or fact.stage != "SW":
                continue
            plan_key = (bid, wl)
            plan[plan_key] = TankAssignmentPlan(
                batch_id=bid,
                week_label=wl,
                tank_ids=list(current_set),
                notes=notes_per_pair.get(plan_key, []),
            )

    return plan


def _build_reservation_plan(
    horizon_labels: list[str],
    initial_state: FacilityState,
    facility: FacilityConfig,
    batch_week_facts: dict[tuple[str, str], BatchWeekFact],
    batch_lifecycles: dict[str, BatchLifecycle],
    harvest_demands: list[HarvestDemand],
    bottlenecks: list[Bottleneck],
    control: Optional[ControlParams] = None,
) -> dict[tuple[str, str], TankAssignmentPlan]:
    """ANTICIPATORY reservation-grid scheduler (see
    docs/GREENFIELD_RESERVATION_SCHEDULER_DESIGN.md).

    Cohort-outer (not week-outer): processes cohorts in a fixed FIFO
    priority order and RESERVES each cohort's full forward (tank, week)
    trajectory before moving to the next cohort. Because a cohort's
    whole future is marked on the grid before later cohorts are
    processed, a big cohort's grow-out tanks are HELD for it rather than
    lost to a later cohort grabbing them first (the B46-class
    under-allocation the reactive coordinator suffers).

    Reserve-forward / release-forward semantics: claiming tank t at week
    w marks grid[t][w..end_of_cohort] = bid; a harvest-driven shrink
    un-marks the shed tank from that week forward. Cells are time-shared
    across cohorts — a tank a cohort needs at W40 can serve another at
    W20 — so this does NOT over-subscribe the way lifetime-max did.

    System choice uses forward-peak (minimize resulting peak system
    biomass) — the staggering rule. Tank choice within a system is
    lowest-free-id. Deterministic, no scoring weights.
    """
    H = len(horizon_labels)
    if H == 0:
        return {}
    week_idx = {wl: i for i, wl in enumerate(horizon_labels)}

    tank_to_system: dict[int, str] = {
        t.tank_id: t.system_id for t in facility.tanks if t.type == "OG"
    }
    og_tanks_by_system: dict[str, list[int]] = {}
    for tid, s in tank_to_system.items():
        og_tanks_by_system.setdefault(s, []).append(tid)
    for s in og_tanks_by_system:
        og_tanks_by_system[s].sort()
    sixn_systems = frozenset({"OG6N"})

    # grid[tid] = list over horizon weeks of owning batch_id or None.
    grid: dict[int, list] = {tid: [None] * H for tid in tank_to_system}

    # Per-cohort SW week facts (sorted by week index) + per-tank biomass.
    cohort_weeks: dict[str, list[tuple[int, BatchWeekFact]]] = {}
    per_tank_bio: dict[str, list[float]] = {}
    for (b, wl), f in batch_week_facts.items():
        if f.stage != "SW" or wl not in week_idx:
            continue
        cohort_weeks.setdefault(b, []).append((week_idx[wl], f))
    for b, lst in cohort_weeks.items():
        lst.sort()
        arr = [0.0] * H
        for w, f in lst:
            arr[w] = f.biomass_kg_after_harvest / max(
                1, f.tanks_needed_at_density_cap)
        per_tank_bio[b] = arr

    # PR seed: pre-existing batch tanks at forecast start.
    pr_tanks: dict[str, set] = {}
    for tid, tank in initial_state.tanks_by_id.items():
        if tank.batch_id and tank.type == "OG":
            pr_tanks.setdefault(tank.batch_id, set()).add(tid)

    # Priority order. RESERVATION_ORDER env selects:
    #   "fifo" (default) — by input_date (operational fact)
    #   "peak" — largest peak tank-demand first (packs better: the most
    #            constrained cohorts reserve before the facility fills)
    import os as _os
    _peak_demand = {
        b: max((f.tanks_needed_at_density_cap for _w, f in wks), default=0)
        for b, wks in cohort_weeks.items()
    }
    if _os.environ.get("RESERVATION_ORDER", "fifo") == "peak":
        order = sorted(cohort_weeks.keys(),
                       key=lambda b: (-_peak_demand[b], b))
    else:
        order = sorted(
            cohort_weeks.keys(),
            key=lambda b: (
                (batch_lifecycles[b].input_date or date.max)
                if b in batch_lifecycles else date.max,
                b,
            ),
        )

    def _sys_load(sys: str) -> list[float]:
        curve = [0.0] * H
        for tid in og_tanks_by_system.get(sys, []):
            row = grid[tid]
            for w in range(H):
                owner = row[w]
                if owner is not None:
                    curve[w] += per_tank_bio.get(owner, _ZERO)[w]
        return curve

    _ZERO = [0.0] * H

    def _free_for_span(tid: int, w0: int, w1: int) -> bool:
        row = grid[tid]
        for w in range(w0, w1 + 1):
            if row[w] is not None:
                return False
        return True

    def _reserve(tid: int, bid: str, w0: int, w1: int) -> None:
        row = grid[tid]
        for w in range(w0, w1 + 1):
            row[w] = bid

    def _release_from(tid: int, bid: str, w0: int) -> None:
        row = grid[tid]
        for w in range(w0, H):
            if row[w] == bid:
                row[w] = None

    notes_per_pair: dict[tuple[str, str], list[str]] = {}

    for bid in order:
        wks = cohort_weeks[bid]
        last_w = wks[-1][0]
        held: set = set()
        # Seed PR tanks as held (reserved from week 0 across the span;
        # they'll be released/migrated as eligibility + needs dictate).
        for tid in sorted(pr_tanks.get(bid, set())):
            if tid in grid:
                _reserve(tid, bid, wks[0][0], last_w)
                held.add(tid)

        for w, f in wks:
            needed = max(0, f.tanks_needed_at_density_cap)
            eligible = set(f.eligible_systems)
            notes = notes_per_pair.setdefault((bid, horizon_labels[w]), [])

            # Release held tanks no longer in eligible systems (e.g. the
            # OG1/2 tanks when the cohort crosses 1 kg → must exit to
            # OG3-6). Released from THIS week forward.
            for tid in sorted(held):
                if tank_to_system.get(tid) not in eligible:
                    _release_from(tid, bid, w)
                    held.discard(tid)

            held_in_elig = [t for t in held
                            if tank_to_system.get(t) in eligible]

            # Shrink (harvest): release surplus held tanks from w forward.
            if len(held_in_elig) > needed:
                # Release highest tank_id first (newest), OG1/2 before
                # OG3-6 when both present.
                shed = sorted(
                    held_in_elig,
                    key=lambda t: (
                        0 if tank_to_system.get(t) not in _OG12 else 1, -t),
                )[: len(held_in_elig) - needed]
                for tid in shed:
                    _release_from(tid, bid, w)
                    held.discard(tid)
            # Grow / catch up: reserve more eligible tanks for w..last_w.
            elif len(held_in_elig) < needed:
                shortfall = needed - len(held_in_elig)
                cand_systems = [s for s in eligible if s not in sixn_systems]
                cohort_ptc = per_tank_bio.get(bid, _ZERO)
                # Stickiness: systems the cohort ALREADY occupies come
                # first (no extra system spread / fragmentation), then
                # NEW systems ordered by forward-peak (staggering).
                own_systems = {tank_to_system.get(t) for t in held}
                own_systems.discard(None)
                scored = []
                for s in cand_systems:
                    load = _sys_load(s)
                    peak = max(load[x] + cohort_ptc[x] for x in range(H))
                    sticky = 0 if s in own_systems else 1
                    scored.append((sticky, peak, s))
                scored.sort()
                got = 0
                for _sticky, _peak, s in scored:
                    if got >= shortfall:
                        break
                    for tid in og_tanks_by_system.get(s, []):
                        if got >= shortfall:
                            break
                        if tid in held:
                            continue
                        if _free_for_span(tid, w, last_w):
                            _reserve(tid, bid, w, last_w)
                            held.add(tid)
                            got += 1
                if got < shortfall:
                    bottlenecks.append(Bottleneck(
                        week_label=horizon_labels[w],
                        system_id=None,
                        kind="reservation_unmet",
                        detail=(f"{bid} W={horizon_labels[w]} needs {needed} "
                                f"tanks; reserved {len(held_in_elig)+got}"),
                        deficit=shortfall - got,
                    ))
                    notes.append(
                        f"reservation short: need {needed}, got "
                        f"{len(held_in_elig)+got}")

    # Materialize plan from the grid.
    plan: dict[tuple[str, str], TankAssignmentPlan] = {}
    for bid, wks in cohort_weeks.items():
        for w, f in wks:
            wl = horizon_labels[w]
            tanks = [tid for tid in tank_to_system
                     if grid[tid][w] == bid]
            plan[(bid, wl)] = TankAssignmentPlan(
                batch_id=bid,
                week_label=wl,
                tank_ids=sorted(tanks),
                notes=notes_per_pair.get((bid, wl), []),
            )
    return plan


def _build_migration_plan(
    horizon_labels: list[str],
    initial_state: FacilityState,
    facility: FacilityConfig,
    batch_week_facts: dict[tuple[str, str], BatchWeekFact],
    batch_lifecycles: dict[str, BatchLifecycle],
    harvest_demands: list[HarvestDemand],
    bottlenecks: list[Bottleneck],
    control: Optional[ControlParams] = None,
    assignment_plan: Optional[dict[tuple[str, str], TankAssignmentPlan]] = None,
) -> tuple[dict[tuple[str, str], int], dict[tuple[str, str], MigrationStep]]:
    """Project per-(week, system) tank availability + per-(batch, week) cascade.

    Walks weeks chronologically. Each week:
      1. Apply tank-emptying dynamics matching Phase D's actual behavior:
         - Purge mode: drain the 6N head pair, then move-in fish from the
           FIFO oldest production batch into the pair's main tank (mirrors
           placement._run_sixn_purge_week).
         - Production mode (no purge): drain via Layer 2 harvest_demands
           (smallest-count tank first).
      2. Compute free tanks per system.
      3. Process batches FIFO: assign tanks honoring the 1 kg rule
         (>=1 kg cannot ADD an OG12 tank; OG12 transit residual allowed).
      4. Emit MigrationStep with keep/add/drop.
      5. Emit tranog_unmet Bottleneck if a TranOG week has insufficient OG12.

    Returns (tank_availability_by_week_system, migration_plan).

    If `assignment_plan` is provided (session 3+ wiring), this function
    derives MigrationStep entries from per-(batch, week) diffs of the
    canonical assignment plan rather than running its own FIFO greedy
    allocator. The migration plan becomes a thin diff layer: the
    coordinator owns the WHICH-tanks decision, this function owns the
    keep/add/drop bookkeeping that Phase B/C/D consume.
    """
    # System lookup per tank.
    tank_to_system: dict[int, str] = {}
    og_tank_ids_by_system: dict[str, list[int]] = {}
    for t in facility.tanks:
        if t.type == "OG":
            tank_to_system[t.tank_id] = t.system_id
            og_tank_ids_by_system.setdefault(t.system_id, []).append(t.tank_id)
    for sys in og_tank_ids_by_system:
        og_tank_ids_by_system[sys].sort()

    # ---- Diff-based path: derive MigrationStep from assignment_plan ----
    if assignment_plan:
        og12_systems_local = frozenset({"OG1N", "OG1S", "OG2N", "OG2S"})
        sixn_systems_local = frozenset({"OG6N"})
        plan_out: dict[tuple[str, str], MigrationStep] = {}
        availability_out: dict[tuple[str, str], int] = {}
        # Per-batch prev tanks (running, week-over-week).
        prev_by_batch: dict[str, set[int]] = {
            bid: set(tids) for bid, tids in batch_tanks.items()
        } if 'batch_tanks' in dir() else {}
        # Re-derive batch_tanks for the diff path (avoid relying on later
        # body initialization).
        prev_by_batch = {}
        for tid, tank in initial_state.tanks_by_id.items():
            if tank.batch_id and tank.type == "OG":
                prev_by_batch.setdefault(tank.batch_id, set()).add(tid)
        # Compute per-system free-tank availability per week from the
        # union of assigned tanks across all batches that week.
        for wl in horizon_labels:
            occupied: set[int] = set()
            for (bid, w), entry in assignment_plan.items():
                if w == wl:
                    occupied.update(entry.tank_ids)
            for sys, tids in og_tank_ids_by_system.items():
                availability_out[(wl, sys)] = sum(
                    1 for t in tids if t not in occupied
                )
        # Emit MigrationStep per (batch, week) from diffs.
        for (bid, wl), entry in assignment_plan.items():
            this_set = set(entry.tank_ids)
            prev_set = prev_by_batch.get(bid, set())
            fact = batch_week_facts.get((bid, wl))
            is_tranog = bool(fact and fact.is_tranog_week)
            keep = sorted(this_set & prev_set)
            add = sorted(this_set - prev_set)
            drop = sorted(prev_set - this_set)
            og12_transit = sum(
                1 for t in keep
                if tank_to_system.get(t) in og12_systems_local
                and not is_tranog
            )
            plan_out[(bid, wl)] = MigrationStep(
                batch_id=bid,
                week_label=wl,
                keep_tanks=keep,
                add_tanks=add,
                drop_tanks=drop,
                is_tranog=is_tranog,
                og12_transit_count=og12_transit,
                notes=list(entry.notes),
            )
            prev_by_batch[bid] = this_set
        return availability_out, plan_out
    # ---- End diff-based path ----

    # 6N pipeline state: pair queue (initialized only if relevant). The pairs
    # are ordered ascending by combined fish count at PR — same rule as
    # sixn.initial_purge_pair_queue (operator H10).
    sixn_pair_queue: list[tuple[int, int]] = []
    SIXN_PAIRS_LOCAL = [(61, 67), (63, 69), (65, 71)]
    if control is not None:
        # Determine if we start in purge mode (any week is purge).
        pair_counts = []
        for p in SIXN_PAIRS_LOCAL:
            total = sum(initial_state.tanks_by_id[t].count
                        for t in p if t in initial_state.tanks_by_id
                        and not initial_state.tanks_by_id[t].is_empty)
            if total > 0:
                pair_counts.append((p, total))
        pair_counts.sort(key=lambda x: x[1])
        sixn_pair_queue = [p for p, _ in pair_counts]

    # Initial per-batch tank occupancy from PR.
    batch_tanks: dict[str, list[int]] = {}
    tank_to_batch: dict[int, str] = {}
    tank_count_at_pr: dict[int, float] = {}
    for tid, tank in initial_state.tanks_by_id.items():
        if tank.batch_id and tank.type == "OG":
            batch_tanks.setdefault(tank.batch_id, []).append(tid)
            tank_to_batch[tid] = tank.batch_id
            tank_count_at_pr[tid] = tank.count
    for bid in batch_tanks:
        batch_tanks[bid].sort()

    # Harvest demands grouped by (week, batch).
    harvest_by_bw: dict[tuple[str, str], float] = {}
    for d in harvest_demands:
        key = (d.week_label, d.batch_id)
        harvest_by_bw[key] = harvest_by_bw.get(key, 0.0) + d.count

    # Layer-2 weekly demand totals (used by 6N pipeline to size move-ins).
    demand_count_by_week: dict[str, float] = {}
    for d in harvest_demands:
        demand_count_by_week[d.week_label] = (
            demand_count_by_week.get(d.week_label, 0.0) + d.count
        )

    # Track cumulative harvest per batch (to know when a batch is fully gone).
    cum_harvest: dict[str, float] = {bid: 0.0 for bid in batch_tanks}
    # Per-tank current count estimate (decays with harvest; updated each week).
    tank_count: dict[int, float] = dict(tank_count_at_pr)

    # FIFO order across all known batches (oldest input_date first).
    fifo_order = sorted(
        batch_lifecycles.keys(),
        key=lambda b: batch_lifecycles[b].input_date or date.max,
    )

    plan: dict[tuple[str, str], MigrationStep] = {}
    availability: dict[tuple[str, str], int] = {}

    og12_systems = {"OG1N", "OG1S", "OG2N", "OG2S"}
    sixn_systems = {"OG6N"}

    # Helpers from sixn module — imported lazily to avoid circular import.
    from .sixn import is_purge_mode as _is_purge

    for week_idx, week_label in enumerate(horizon_labels):
        # Determine purge mode this week from control + week_start.
        week_start_date = None
        bwf_any = next((f for (b, w), f in batch_week_facts.items() if w == week_label), None)
        if bwf_any is not None:
            week_start_date = bwf_any.week_start
        purge_this_week = (control is not None and week_start_date is not None
                           and _is_purge(control, week_start_date))

        # ---- 1. Apply tank dynamics for THIS week ----
        if purge_this_week and sixn_pair_queue:
            # 6N purge pipeline simulation (mirrors Phase D's
            # _run_sixn_purge_week so the migration plan's tank
            # availability matches reality).
            pair = sixn_pair_queue.pop(0)

            # a) Drain pair tanks (release whatever batch was there).
            for tid in pair:
                bid_at_tid = tank_to_batch.get(tid)
                if bid_at_tid is not None:
                    if tid in batch_tanks.get(bid_at_tid, []):
                        batch_tanks[bid_at_tid].remove(tid)
                    tank_to_batch.pop(tid, None)
                tank_count[tid] = 0.0

            # b) Pick FIFO oldest production batch with mature fish in
            #    non-6N tanks. (Mirrors _pick_fifo_move_in_batch.)
            move_in_batch: Optional[str] = None
            for bid in fifo_order:
                fact = batch_week_facts.get((bid, week_label))
                if fact is None or fact.stage != "SW":
                    continue
                if (control is not None
                        and fact.avg_wt_g < control.min_harvest_weight_g):
                    continue
                prod_tids = [
                    t for t in batch_tanks.get(bid, [])
                    if tank_to_system.get(t) not in sixn_systems
                    and tank_count.get(t, 0.0) > 0
                ]
                if prod_tids:
                    move_in_batch = bid
                    break

            # c) Move-in target: Layer 2 demand 2 weeks ahead, clamped
            #    to [min_h, max_h]. Same rule as the placement walk.
            if move_in_batch is not None and control is not None:
                min_h = control.min_harvest_per_week or 0.0
                max_h = control.max_harvest_per_week or min_h
                future_idx = week_idx + 2
                if future_idx < len(horizon_labels):
                    future_label = horizon_labels[future_idx]
                    target = demand_count_by_week.get(future_label, min_h)
                else:
                    target = min_h
                target = max(min_h, min(max_h, target))

                # d) Pull from move-in batch's production tanks
                #    (largest-count first, mirroring Phase D).
                src_tids = sorted(
                    [t for t in batch_tanks.get(move_in_batch, [])
                     if tank_to_system.get(t) not in sixn_systems],
                    key=lambda t: tank_count.get(t, 0.0),
                    reverse=True,
                )
                main_tid = pair[0]
                count_moved = 0.0
                for src_tid in src_tids:
                    if count_moved >= target:
                        break
                    src_cnt = tank_count.get(src_tid, 0.0)
                    take = min(target - count_moved, src_cnt)
                    if take <= 0:
                        continue
                    tank_count[src_tid] = src_cnt - take
                    count_moved += take
                    if tank_count[src_tid] <= 0.5:
                        if src_tid in batch_tanks[move_in_batch]:
                            batch_tanks[move_in_batch].remove(src_tid)
                        tank_to_batch.pop(src_tid, None)

                # e) Stock the pair's main tank with the moved fish.
                if count_moved > 0:
                    tank_count[main_tid] = count_moved
                    tank_to_batch[main_tid] = move_in_batch
                    batch_tanks.setdefault(move_in_batch, [])
                    if main_tid not in batch_tanks[move_in_batch]:
                        batch_tanks[move_in_batch].append(main_tid)

            # f) Push pair back to queue.
            sixn_pair_queue.append(pair)
        else:
            # Production-mode (or pre-purge): apply Layer 2 harvest demands.
            for (wl, bid), count_to_harvest in list(harvest_by_bw.items()):
                if wl != week_label:
                    continue
                if bid not in batch_tanks or not batch_tanks[bid]:
                    continue
                remaining = count_to_harvest
                cum_harvest[bid] = cum_harvest.get(bid, 0.0) + count_to_harvest
                # Drain smallest-count tank first to match Phase D's policy.
                tanks_sorted = sorted(batch_tanks[bid], key=lambda t: tank_count.get(t, 0.0))
                for tid in tanks_sorted:
                    if remaining <= 0:
                        break
                    t_cnt = tank_count.get(tid, 0.0)
                    take = min(remaining, t_cnt)
                    tank_count[tid] = t_cnt - take
                    remaining -= take
                    if tank_count[tid] <= 0.5:
                        if tid in batch_tanks[bid]:
                            batch_tanks[bid].remove(tid)
                        tank_to_batch.pop(tid, None)

        # ---- 2. Compute free tanks per OG system ----
        # OG6N is excluded from allocatable pool — the 6N pipeline owns
        # those tanks. Migration plan never picks an OG6N tank for a
        # batch's keep/add list.
        free_pool: dict[str, list[int]] = {}
        for sys, tids in og_tank_ids_by_system.items():
            free_pool[sys] = [t for t in tids if t not in tank_to_batch]
            availability[(week_label, sys)] = len(free_pool[sys])
        for sys in sixn_systems:
            free_pool[sys] = []

        # ---- 3. Process batches FIFO and build MigrationStep ----
        # Snapshot per-batch prev tanks (start-of-week state) for the diff.
        prev_tanks_snapshot: dict[str, list[int]] = {
            b: list(tids) for b, tids in batch_tanks.items()
        }
        # FIFO ordering (oldest batch first). Density-pressure priority
        # was tested empirically and made density violations WORSE on the
        # reference workbook: the violations are dominated by pre-existing
        # PR batches whose tank-set freezes at the OG12_MOVE_LOCK 1 kg
        # threshold — no allocation ordering can add tanks after freeze,
        # and the only relief is harvest-scheduler coverage.
        this_week_facts = [
            (bid, batch_week_facts[(bid, week_label)])
            for bid in fifo_order
            if (bid, week_label) in batch_week_facts
        ]

        for bid, fact in this_week_facts:
            if fact.stage != "SW":
                continue
            prev = list(prev_tanks_snapshot.get(bid, []))
            # OG6N tanks are owned by the 6N pipeline (added by move-in,
            # released by next drain). The migration plan must never add
            # or drop them — always keep them in the tank set as-is.
            prev_og6n = [t for t in prev if tank_to_system.get(t) in sixn_systems]
            prev_og12 = [t for t in prev if tank_to_system.get(t) in og12_systems]
            prev_og3plus = [t for t in prev if tank_to_system.get(t) is not None
                            and tank_to_system.get(t) not in og12_systems
                            and tank_to_system.get(t) not in sixn_systems]

            needed = max(1, fact.tanks_needed_at_density_cap)
            is_above_1kg = fact.avg_wt_g >= _OG12_MOVE_LOCK_WT_G
            is_tranog = fact.is_tranog_week
            eligible = set(fact.eligible_systems)

            # Sticky tank-set floor: total tank count per batch can only
            # shrink when a harvest reduces the cohort (and the remaining
            # biomass consolidates into fewer tanks). Outside of harvest
            # weeks, never drop tanks — per-week density sizing can
            # under-count tanks during low-biomass phases, and dropping
            # them creates concentration that becomes a density violation
            # when biomass grows back. TranOG weeks own their own floor
            # (max(4, density)) and skip this rule because prev is empty.
            harvest_this_week = harvest_by_bw.get((week_label, bid), 0.0)
            if not is_tranog and harvest_this_week <= 0 and len(prev) > 0:
                needed = max(needed, len(prev))

            keep_tanks: list[int] = []
            add_tanks: list[int] = []
            drop_tanks: list[int] = []
            notes: list[str] = []

            if is_tranog:
                # TranOG week: allocate `needed` tanks from OG1/2 first;
                # overflow into OG3+ when OG1/2 is exhausted (density-
                # preservation — better to spread fish across more tanks
                # than to cram a whole cohort into 1 OG1/2 tank).
                pickable = []
                for sys in ("OG1N", "OG1S", "OG2N", "OG2S"):
                    pickable.extend(free_pool.get(sys, []))
                og12_available = len(pickable)
                if og12_available < needed:
                    # Add OG3+ tanks (excluding OG6N pipeline-owned).
                    for sys in ("OG3N", "OG3S", "OG4N", "OG4S",
                                "OG5N", "OG5S", "OG6S"):
                        pickable.extend(free_pool.get(sys, []))
                if len(pickable) < needed:
                    bottlenecks.append(Bottleneck(
                        week_label=week_label,
                        system_id=None,
                        kind="tranog_unmet",
                        detail=(f"TranOG {bid} needs {needed} tanks; "
                                f"only {len(pickable)} free (OG1/2 + OG3+)"),
                        deficit=needed - len(pickable),
                    ))
                elif og12_available < needed:
                    notes.append(
                        f"TranOG arrival overflowed to OG3+: only "
                        f"{og12_available} of {needed} tanks in OG1/2"
                    )
                for tid in pickable[:needed]:
                    add_tanks.append(tid)
                    free_pool[tank_to_system[tid]].remove(tid)
                # All prev tanks are released (TranOG is the first OG entry).
                drop_tanks = list(prev)
            elif is_above_1kg:
                # >=1 kg: target tank set is `needed` OG3+ tanks.
                # Keep prev OG3+ tanks first (sticky). Add new OG3+ as
                # available. If needed > prev_og3plus + available_og3plus,
                # keep OG12 prev tanks as transit residual.
                kept_og3plus = list(prev_og3plus)[:needed]
                keep_tanks.extend(kept_og3plus)
                dropped_og3plus = [t for t in prev_og3plus if t not in kept_og3plus]
                drop_tanks.extend(dropped_og3plus)
                shortfall = needed - len(kept_og3plus)
                # Try to add fresh OG3+ tanks from free pool, eligible systems only.
                for sys in fact.eligible_systems:
                    if sys in og12_systems:
                        continue
                    while shortfall > 0 and free_pool.get(sys):
                        tid = free_pool[sys].pop(0)
                        add_tanks.append(tid)
                        shortfall -= 1
                # Remaining shortfall: hold prev OG12 as transit residual.
                transit = 0
                if shortfall > 0 and prev_og12:
                    held = prev_og12[:shortfall]
                    keep_tanks.extend(held)
                    transit = len(held)
                    notes.append(
                        f"transit residual: {transit} OG12 tank(s) "
                        f"held because OG3+ free pool insufficient"
                    )
                # Drop any prev OG12 tanks NOT held as transit (they migrate
                # to a newly-added OG3+ tank via cross-system Transfer).
                kept_og12_set = set(prev_og12[:transit])
                drop_tanks.extend(t for t in prev_og12 if t not in kept_og12_set)
                # Bottleneck if residual exists.
                if transit > 0:
                    bottlenecks.append(Bottleneck(
                        week_label=week_label,
                        system_id=None,
                        kind="og12_residual",
                        detail=(f"{bid} (avg_wt {fact.avg_wt_g:.0f}g) holds "
                                f"{transit} OG12 tank(s) as transit residual"),
                        deficit=transit,
                    ))
            else:
                # < 1 kg, not TranOG week: keep prev (sticky), add to fill needed.
                kept = list(prev)[:needed]
                keep_tanks.extend(kept)
                drop_tanks.extend(t for t in prev if t not in kept)
                shortfall = needed - len(kept)
                for sys in fact.eligible_systems:
                    while shortfall > 0 and free_pool.get(sys):
                        tid = free_pool[sys].pop(0)
                        add_tanks.append(tid)
                        shortfall -= 1
                if shortfall > 0:
                    notes.append(
                        f"under-tanked: needed {needed}, got "
                        f"{needed - shortfall}; no eligible system has free tanks"
                    )

            # Always carry forward any OG6N tanks the batch holds (these
            # are 6N-pipeline-managed; the migration plan must not add or
            # drop them).
            keep_tanks.extend(prev_og6n)

            plan[(bid, week_label)] = MigrationStep(
                batch_id=bid,
                week_label=week_label,
                keep_tanks=sorted(set(keep_tanks)),
                add_tanks=sorted(add_tanks),
                drop_tanks=sorted(drop_tanks),
                is_tranog=is_tranog,
                og12_transit_count=sum(
                    1 for t in keep_tanks
                    if tank_to_system.get(t) in og12_systems and not is_tranog
                ),
                notes=notes,
            )

            # Update batch_tanks + tank_to_batch for next iteration.
            new_set = set(keep_tanks) | set(add_tanks)
            for t in drop_tanks:
                if tank_to_batch.get(t) == bid:
                    tank_to_batch.pop(t, None)
            for t in new_set:
                tank_to_batch[t] = bid
                # Estimate tank count (split evenly across new tanks); rough.
                tank_count[t] = max(tank_count.get(t, 0.0), fact.count_after_harvest / max(1, len(new_set)))
            batch_tanks[bid] = sorted(new_set)

    return availability, plan


def build_precalc_canvas(
    control: ControlParams,
    batches: list[BatchInput],
    tables: BiologyTables,
    facility: FacilityConfig,
    facility_limits: FacilityLimits,
    system_limits: SystemLimits,
    biology_states_by_batch: dict[str, list[BatchWeekState]],
    splits: list[SizeClassSplit],
    harvest_demands: list[HarvestDemand],
    pinned_harvests: list[PinnedHarvest],
    pinned_transfers: list[PinnedTransfer],
    initial_state: Optional[FacilityState] = None,
    projected_biomass_by_week: Optional[dict[str, float]] = None,
) -> PrecalcCanvas:
    """Assemble the full Stage 1 precalc canvas."""
    forecast_start = _as_date(control.forecast_start)
    horizon_labels = forecast_week_labels(forecast_start, control.horizon_weeks)

    tank_facts = _build_tank_facts(facility)
    system_facts = _build_system_facts(facility)
    batch_lifecycles = _build_batch_lifecycles(batches, tables)
    batch_week_facts = _build_batch_week_facts(
        biology_states_by_batch, harvest_demands, splits, batches, control, facility,
    )
    weekly_facility = _build_weekly_facility_facts(
        horizon_labels, forecast_start, control, facility_limits, batch_week_facts,
    )
    weekly_system = _build_weekly_system_facts(
        horizon_labels, system_limits, system_facts,
    )
    bottlenecks = _detect_bottlenecks(weekly_facility, system_facts)

    total_og_supply = sum(
        sf.tank_count for sf in system_facts.values() if sf.type == "OG"
    ) * control.horizon_weeks
    total_og_demand = sum(f.total_tank_demand for f in weekly_facility.values())

    availability: dict[tuple[str, str], int] = {}
    migration: dict[tuple[str, str], MigrationStep] = {}
    assignment_plan: dict[tuple[str, str], TankAssignmentPlan] = {}
    if initial_state is not None:
        # Assignment coordinator. RESERVATION_PLAN env selects the new
        # anticipatory reservation-grid scheduler; default is the
        # incremental event-loop coordinator.
        import os as _os
        if _os.environ.get("RESERVATION_PLAN"):
            assignment_plan = _build_reservation_plan(
                horizon_labels=horizon_labels,
                initial_state=initial_state,
                facility=facility,
                batch_week_facts=batch_week_facts,
                batch_lifecycles=batch_lifecycles,
                harvest_demands=harvest_demands,
                bottlenecks=bottlenecks,
                control=control,
            )
        else:
            assignment_plan = _build_facility_assignment_plan(
                horizon_labels=horizon_labels,
                initial_state=initial_state,
                facility=facility,
                batch_week_facts=batch_week_facts,
                batch_lifecycles=batch_lifecycles,
                harvest_demands=harvest_demands,
                weekly_facility=weekly_facility,
                bottlenecks=bottlenecks,
                control=control,
            )
        availability, migration = _build_migration_plan(
            horizon_labels=horizon_labels,
            initial_state=initial_state,
            facility=facility,
            batch_week_facts=batch_week_facts,
            batch_lifecycles=batch_lifecycles,
            harvest_demands=harvest_demands,
            bottlenecks=bottlenecks,
            control=control,
            assignment_plan=assignment_plan,
        )

    # Detect contiguous windows where the projected biomass under min-only
    # harvest falls below the lower band — the facility's natural carrying
    # capacity gap. Operator sees this before the placement walk runs.
    projection = projected_biomass_by_week or {}
    if projection:
        bio_cap = control.max_biomass_kg
        dev = control.facility_biomass_deviation_pct or 0.0
        band_lo = bio_cap * (1.0 - dev) if bio_cap else None
        if band_lo is not None:
            in_window = False
            window_start = None
            window_min = float("inf")
            for label in horizon_labels:
                proj = projection.get(label)
                if proj is None:
                    continue
                if proj < band_lo:
                    if not in_window:
                        in_window = True
                        window_start = label
                        window_min = proj
                    else:
                        window_min = min(window_min, proj)
                else:
                    if in_window:
                        bottlenecks.append(Bottleneck(
                            week_label=window_start,
                            system_id=None,
                            kind="biomass_below_band_window",
                            detail=(
                                f"projected biomass under min-only harvest "
                                f"falls below band {band_lo/1000:,.0f} t "
                                f"from {window_start}; min in window "
                                f"{window_min/1000:,.0f} t"
                            ),
                            deficit=band_lo - window_min,
                        ))
                        in_window = False
                        window_min = float("inf")
            if in_window:
                bottlenecks.append(Bottleneck(
                    week_label=window_start,
                    system_id=None,
                    kind="biomass_below_band_window",
                    detail=(
                        f"projected biomass below band {band_lo/1000:,.0f} t "
                        f"from {window_start} to horizon end; min "
                        f"{window_min/1000:,.0f} t"
                    ),
                    deficit=band_lo - window_min,
                ))

    return PrecalcCanvas(
        forecast_start=forecast_start,
        horizon_weeks=control.horizon_weeks,
        horizon_labels=horizon_labels,
        tank_facts=tank_facts,
        system_facts=system_facts,
        batch_lifecycles=batch_lifecycles,
        batch_week_facts=batch_week_facts,
        weekly_facility=weekly_facility,
        weekly_system=weekly_system,
        pinned_harvests=pinned_harvests,
        pinned_transfers=pinned_transfers,
        bottlenecks=bottlenecks,
        total_og_tank_weeks_supply=total_og_supply,
        total_og_tank_weeks_demand=total_og_demand,
        tank_availability_by_week_system=availability,
        migration_plan=migration,
        assignment_plan=assignment_plan,
        projected_biomass_by_week=projection,
        warnings=[],
    )


# ============================================================
# Diagnostic printer
# ============================================================

def print_canvas_summary(canvas: PrecalcCanvas) -> None:
    """Operator-facing summary of the canvas. Read-only."""
    print()
    print("=" * 72)
    print(f"PRECALC CANVAS — forecast {canvas.forecast_start} + {canvas.horizon_weeks}w")
    print("=" * 72)

    og_tanks = sum(sf.tank_count for sf in canvas.system_facts.values() if sf.type == "OG")
    fw_tanks = sum(sf.tank_count for sf in canvas.system_facts.values() if sf.type == "FW")
    print(f"Facility: {og_tanks} OG tanks across "
          f"{sum(1 for sf in canvas.system_facts.values() if sf.type == 'OG')} OG systems  "
          f"+ {fw_tanks} FW (pool)")

    print(f"Batches:  {len(canvas.batch_lifecycles)} known to lifecycle, "
          f"{len({k[0] for k in canvas.batch_week_facts})} have per-week facts")

    # Tank-week budget.
    util_pct = (canvas.total_og_tank_weeks_demand
                / canvas.total_og_tank_weeks_supply * 100.0
                if canvas.total_og_tank_weeks_supply else 0.0)
    print(f"OG tank-week budget: demand {canvas.total_og_tank_weeks_demand} / "
          f"supply {canvas.total_og_tank_weeks_supply} "
          f"({util_pct:.1f}% utilization)")

    # 6N mode summary over horizon.
    modes = {"purge": 0, "transition": 0, "production": 0}
    for wff in canvas.weekly_facility.values():
        modes[wff.sixn_mode] = modes.get(wff.sixn_mode, 0) + 1
    print(f"6N modes: " + ", ".join(f"{k}={v}w" for k, v in modes.items() if v))

    # Peak weeks (tank demand).
    peaks = sorted(canvas.weekly_facility.values(),
                   key=lambda f: f.total_tank_demand, reverse=True)[:5]
    print(f"Peak tank-demand weeks (post-harvest, facility-wide incl. FW):")
    for wff in peaks:
        print(f"  {wff.week_label}: demand {wff.total_tank_demand} tanks, "
              f"biomass {wff.projected_biomass_kg_facility/1000:>7,.0f} t, "
              f"feed {wff.projected_feed_kg_day_facility:>7,.0f} kg/d "
              f"({wff.sixn_mode})")

    # Migration plan summary.
    if canvas.migration_plan:
        total_steps = len(canvas.migration_plan)
        tranog_steps = sum(1 for s in canvas.migration_plan.values() if s.is_tranog)
        transit_steps = sum(1 for s in canvas.migration_plan.values() if s.og12_transit_count > 0)
        total_adds = sum(len(s.add_tanks) for s in canvas.migration_plan.values())
        total_drops = sum(len(s.drop_tanks) for s in canvas.migration_plan.values())
        print(f"Migration plan: {total_steps} (batch, week) steps  "
              f"({tranog_steps} TranOG arrivals, {transit_steps} transit-residual)")
        print(f"  Tank moves: {total_adds} adds, {total_drops} drops over horizon")

    # Biomass projection under FIFO min-only harvest (the "carrying capacity"
    # trajectory the scheduler targets).
    if canvas.projected_biomass_by_week:
        proj = canvas.projected_biomass_by_week
        labels_sorted = sorted(proj.keys())
        peak_label = max(proj, key=proj.get)
        peak_val = proj[peak_label]
        first = proj[labels_sorted[0]]
        last = proj[labels_sorted[-1]]
        print(f"Biomass projection (min-only FIFO): "
              f"start {first/1000:,.0f} t -> peak {peak_val/1000:,.0f} t "
              f"@ {peak_label} -> end {last/1000:,.0f} t")

    # Bottlenecks.
    if canvas.bottlenecks:
        print(f"\nBOTTLENECKS ({len(canvas.bottlenecks)}):")
        by_kind: dict[str, list[Bottleneck]] = {}
        for b in canvas.bottlenecks:
            by_kind.setdefault(b.kind, []).append(b)
        for kind, items in by_kind.items():
            items.sort(key=lambda b: b.deficit, reverse=True)
            print(f"  {kind}: {len(items)} weeks")
            for b in items[:5]:
                print(f"    {b.week_label}: {b.detail}  (deficit {b.deficit:,.0f})")
            if len(items) > 5:
                print(f"    ... +{len(items) - 5} more")
    else:
        print(f"\nBottlenecks: none detected at canvas level")
    print("=" * 72)
