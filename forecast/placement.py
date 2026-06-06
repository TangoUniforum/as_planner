"""Layer 3: precalc-driven placement (DESIGN §7).

Forward-deterministic precalc over the full forecast horizon. Four
phases run in order; each is a pure function consuming precalc data
and producing more precalc data. No reactive walking — the entire
batch-to-tank assignment is computed up-front, then events are
emitted as the diff between consecutive weeks.

Phase A — per-batch trajectory (independent)
    Per (batch, week): biomass / feed / count post-harvest, tank-count
    demand under density cap, eligible-systems set.

Phase B — system assignment (global greedy, FIFO-ordered)
    Per (batch, week): a dict { system_id: tank_count } summing to
    tanks_needed. Hard: system feed + biomass caps with R29 buffer,
    physical tank count, OG1/2 constraint at TranOG entry. Soft:
    spread across systems, load smoothing, sticky to prior week.

Phase C — tank assignment (sticky + rotation)
    Per (batch, week): specific tank_id list. Tanks stay with a batch
    week-over-week when possible; new slots fill from most-recently-
    emptied tank in the chosen system.

Phase D — event emission
    Diff successive weeks' tank assignment + apply harvest demand →
    TranOGEntry / Transfer / Harvest events. Applied to a fresh
    FacilityState clone for invariant verification.

The 1 kg rule is enforced by Phase D: any same-batch Transfer between
two OG1/2 tanks where the source avg_wt >= 1 kg is rejected by
events.Transfer.apply (INV-4), and the warning surfaces in
PlacementResult.warnings. Phase B/C are not yet 1 kg-aware — they
assume tanks can be re-arranged freely; the resulting INV-4 rejections
are visible as warnings until a follow-up cut routes such moves
through OG3/4/5/6.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from .biology import advance_tank_one_day
from .caps import (
    METRIC_BIOMASS,
    METRIC_FEED_DAY,
    METRIC_MAX_HARVEST,
    METRIC_MIN_HARVEST,
    FacilityLimits,
    SystemLimits,
    predictive_move_in_count,
    resolve_facility_cap,
    resolve_system_cap,
    system_cap_with_buffer,
)
from .biology import _fcr_model_key, _interp, upper_truncated_split
from .events import Grade, GradedHarvest, Harvest, OG12_SYSTEMS, OG12_MOVE_LOCK_WT_G, TankAllocation, Transfer, TranOGEntry
from .harvest_scheduler import HarvestDemand
from .models import (
    BatchInput,
    BatchWeekState,
    BiologyTables,
    ControlParams,
    FacilityConfig,
    SizeClassSplit,
    TankConfig,
)
from .sixn import (
    SIXN_MAIN_TANKS,
    SIXN_SISTER_TANKS,
    SIXN_PAIRS,
    initial_purge_pair_queue,
    is_purge_mode,
)
from .state import FacilityState, TankState
from .time_grid import forecast_week_labels, week_range


# Per the lock record §5: all six pipeline tanks (61/63/65 main,
# 67/69/71 sister) live in system OG6N. OG6S is a regular OG3-6
# grow-out system, NOT pipeline-owned, so it is a valid move-in source
# for the 6N purge pipeline (and is allocatable by Phase A/B/C).
_SIXN_SYSTEMS = frozenset({"OG6N"})

# Closed-loop harvest controller tuning (placement #2).
#   _SETPOINT_FRACTION — biomass setpoint as a fraction of the facility cap.
#     The predictive move-in + reactive supplement drive biomass toward this
#     level. 1.0 sits ON the cap (max utilisation, peaks ~+3%, over cap ~half
#     the weeks); 0.995 centres just under it (near-identical in-band count,
#     peaks held ~+2.6%, under cap a majority of weeks). Tuned empirically
#     against the live config; weekly growth (~136t) exceeds the ±1% band
#     width (~78t), so ~±2.6% swings are the physical floor.
_SETPOINT_FRACTION = 0.995


# Eligible system sets used by Phase A.
_OG_ALL_WITH_6N = ["OG1N", "OG1S", "OG2N", "OG2S", "OG3N", "OG3S",
                   "OG4N", "OG4S", "OG5N", "OG5S", "OG6N", "OG6S"]
# In purge mode OG6N is owned by the 6N pipeline (_run_sixn_purge_week
# handles harvests + move-ins), so Phase A/B/C must NOT allocate it
# — otherwise non-6N tanks end up overpacked while OG6N goes unused
# by the rebalancer. Use OG6N only when 6N is in production mode.
_OG_ALL = ["OG1N", "OG1S", "OG2N", "OG2S", "OG3N", "OG3S",
           "OG4N", "OG4S", "OG5N", "OG5S", "OG6S"]
_OG12 = ["OG1N", "OG1S", "OG2N", "OG2S"]


def _as_date(d):
    if isinstance(d, datetime):
        return d.date()
    return d


# ============================================================
# Outputs
# ============================================================

@dataclass
class BatchWeekLoad:
    """Phase A output: per-(batch, week) load + demand + eligibility."""
    batch_id: str
    week_label: str
    week_start: date
    count: float                  # post-harvest fish count
    avg_wt_g: float
    biomass_kg: float             # post-harvest
    feed_kg_day: float            # post-harvest
    tanks_needed: int             # ceil(biomass / max_per_tank_at_density_cap)
    eligible_systems: list[str]
    stage: str                    # "EGG" | "FW" | "SW" | "STARVE"
    is_tranog_week: bool          # this week contains the batch's TranOG_Date


@dataclass
class SystemAssignment:
    """Phase B output: per-(batch, week) → per-system tank count."""
    batch_id: str
    week_label: str
    per_system: dict[str, int]    # sum of values == tanks_needed at Phase A


@dataclass
class TankAssignment:
    """Phase C output: per-(batch, week) → specific tank_ids."""
    batch_id: str
    week_label: str
    tank_ids: list[int]


@dataclass
class BatchLocationRow:
    """Per-(week, batch, tank) occupancy row for BatchLocations output."""
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


@dataclass
class PlacementResult:
    """End-to-end Phase A..D outputs + warnings."""
    load_table: list[BatchWeekLoad] = field(default_factory=list)
    system_assignments: list[SystemAssignment] = field(default_factory=list)
    tank_assignments: list[TankAssignment] = field(default_factory=list)
    tranog_events: list[TranOGEntry] = field(default_factory=list)
    transfer_events: list[Transfer] = field(default_factory=list)
    harvest_events: list[Harvest] = field(default_factory=list)
    grade_events: list = field(default_factory=list)
    batch_locations: list[BatchLocationRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ============================================================
# Phase A — per-batch trajectory
# ============================================================

def _max_kg_per_og_tank(facility: FacilityConfig) -> float:
    """Most-restrictive (smallest) per-tank biomass cap across OG tanks.

    Different OG tanks may have different density caps (e.g. OG6N has 120
    kg/m³ while OG1-5 have 85), so using the average over-sizes capacity.
    Use the minimum so tanks_needed is correct for the most-restrictive
    tank a batch might land in.
    """
    og = [t for t in facility.tanks if t.type == "OG"]
    if not og:
        return 0.0
    caps = [t.max_density_kg_m3 * t.volume_m3 for t in og]
    return min(caps)


def phase_a_precalc(
    biology_states_by_batch: dict[str, list[BatchWeekState]],
    harvest_demands: list[HarvestDemand],
    splits: list[SizeClassSplit],
    facility: FacilityConfig,
    control: Optional[ControlParams] = None,
) -> list[BatchWeekLoad]:
    """Compute per-(batch, week) load + tank-count demand + eligibility."""
    max_kg = _max_kg_per_og_tank(facility)
    tranog_dates = {s.batch_id: _as_date(s.tran_og_date) for s in splits}
    tranog_default_tanks = (control.tran_og_default_tanks if control else 3) or 3

    # Harvest demands by batch, summed per week.
    harvest_by_batch_week: dict[tuple[str, str], float] = {}
    for d in harvest_demands:
        key = (d.batch_id, d.week_label)
        harvest_by_batch_week[key] = harvest_by_batch_week.get(key, 0.0) + d.count

    # Density-aware sizing: Control R31 `density_target_pct` (default
    # 0.85). Matches the precalc per-week sizing and the Phase D Grade
    # trigger so all three agree on "tank near cap".
    effective_max_kg = max_kg * control.density_target_pct

    out: list[BatchWeekLoad] = []
    for batch_id, states in biology_states_by_batch.items():
        states_sorted = sorted(states, key=lambda s: s.week_label)
        cum_harvested = 0.0
        tranog_date = tranog_dates.get(batch_id)
        for s in states_sorted:
            cum_harvested += harvest_by_batch_week.get((batch_id, s.week_label), 0.0)
            if s.count <= 0:
                continue
            # Placement is OG-only. Skip FW/EGG rows — those batches live
            # in the FW pool (Postsmolt / Smolt / Parr / etc.) which is
            # not tracked at the tank level.
            if s.stage != "SW":
                continue
            survive = max(0.0, 1.0 - cum_harvested / s.count)
            if survive <= 0:
                continue
            post_count = s.count * survive
            post_biomass = s.biomass_kg * survive
            post_feed = s.feed_kg_day * survive

            tanks_needed = (
                max(1, math.ceil(post_biomass / effective_max_kg))
                if effective_max_kg > 0 else 1
            )

            ws_date = (s.week_start.date()
                       if hasattr(s.week_start, "date") else s.week_start)
            is_tranog = (tranog_date is not None
                         and ws_date <= tranog_date < ws_date + timedelta(days=7))
            # TranOG arrival weeks need >=4 OG1/2 tanks so the SizeClassSplit
            # (big + small) can each get >=2 tanks. This keeps initial
            # density well under cap AND lets the big-first harvest pattern
            # work (drain big tanks → small migrates in via grade-split as
            # they grow). Control R28 (default 3) is a lower bound;
            # min(R28, 4) is the size-class working minimum.
            if is_tranog:
                tanks_needed = max(tanks_needed, max(4, tranog_default_tanks))
            # ----- The 1 kg rule in OG1/2 (two parts) -----
            # PART 1 (this block): fish MUST exit OG1/2 when avg_wt >= 1 kg.
            #   OG1/2 is for sub-1-kg fish only. A batch at avg_wt >= 1 kg
            #   is ineligible for OG1/2 systems (except in the TranOG
            #   arrival week itself — entries land in OG1 or OG2). This
            #   keeps OG1/2 vacant for incoming TranOGs.
            # PART 2 (enforced separately in Phase C narrow freeze +
            #   events.Transfer.apply via INV-4): above 1 kg, fish cannot
            #   be SPLIT internally between OG1/2 tanks (no intra-OG12
            #   Transfer with source >= 1 kg). The OG1/2 -> OG3+ exit
            #   move is allowed (cross-system).
            if is_tranog:
                eligible = _OG12[:]
            elif s.avg_weight_g >= OG12_MOVE_LOCK_WT_G:
                eligible = [sys for sys in _OG_ALL if sys not in _OG12]
            else:
                eligible = _OG_ALL[:]

            out.append(BatchWeekLoad(
                batch_id=batch_id,
                week_label=s.week_label,
                week_start=ws_date,
                count=post_count,
                avg_wt_g=s.avg_weight_g,
                biomass_kg=post_biomass,
                feed_kg_day=post_feed,
                tanks_needed=tanks_needed,
                eligible_systems=eligible,
                stage=s.stage,
                is_tranog_week=is_tranog,
            ))
    return out


# ============================================================
# Phase B — system assignment (greedy)
# ============================================================

def _og_system_tank_counts(facility: FacilityConfig) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in facility.tanks:
        if t.type == "OG":
            out[t.system_id] = out.get(t.system_id, 0) + 1
    return out


def phase_b_assign_systems(
    load_table: list[BatchWeekLoad],
    initial_state: FacilityState,
    batch_meta: dict[str, BatchInput],
    system_limits: SystemLimits,
    control: ControlParams,
    facility: FacilityConfig,
    migration_plan: Optional[dict] = None,
) -> tuple[list[SystemAssignment], list[str]]:
    """Global per-(batch, week) system assignment.

    Two modes:
      - Plan-driven (when `migration_plan` is non-empty): for each
        (batch, week) present in the plan, derive per_system from the
        plan's keep+add tank IDs. The plan is the source of truth.
      - Greedy fallback (legacy): scores eligible systems by sticky +
        spread − load. Used for batch-weeks not in the plan.
    """
    warnings: list[str] = []
    sys_tank_count = _og_system_tank_counts(facility)
    # Tank -> system lookup for plan consumption.
    tank_to_system: dict[int, str] = {
        t.tank_id: t.system_id for t in facility.tanks if t.type == "OG"
    }

    # Group by week_label
    by_week: dict[str, list[BatchWeekLoad]] = {}
    for l in load_table:
        by_week.setdefault(l.week_label, []).append(l)
    sorted_weeks = sorted(by_week.keys())

    # FIFO sort key.
    def _fifo_key(batch_id: str):
        b = batch_meta.get(batch_id)
        if b is None or b.input_date is None:
            return date.max
        return b.input_date.date() if hasattr(b.input_date, "date") else b.input_date

    # Sticky baseline = current state (week before week 0).
    prev_per_system: dict[str, dict[str, int]] = {}
    for tank in initial_state.tanks_by_id.values():
        if tank.batch_id:
            d = prev_per_system.setdefault(tank.batch_id, {})
            d[tank.system_id] = d.get(tank.system_id, 0) + 1

    og36_systems = ["OG3N", "OG3S", "OG4N", "OG4S", "OG5N", "OG5S", "OG6N", "OG6S"]

    assignments: list[SystemAssignment] = []
    for week_label in sorted_weeks:
        # TranOG arrivals first, then FIFO by age.
        loads = sorted(
            by_week[week_label],
            key=lambda l: (0 if l.is_tranog_week else 1, _fifo_key(l.batch_id)),
        )

        # ------ PRE-EMPTIVE OG1/2 EVICTION ------
        # Honor user invariant: a TranOG arrival can never fail to find room.
        # If this week's TranOG arrivals' OG1/2 demand exceeds the room left
        # after sticky carry-overs, move the oldest non-arriving OG1/2
        # occupants out to OG3-6 by rewriting their sticky baseline before
        # the main greedy loop sees them.
        tranog_arrivers = {l.batch_id for l in loads if l.is_tranog_week}
        og12_arrival_demand = sum(l.tanks_needed for l in loads if l.is_tranog_week)
        if og12_arrival_demand > 0:
            og12_sticky_used = sum(
                count
                for bid, per_sys in prev_per_system.items() if bid not in tranog_arrivers
                for sys, count in per_sys.items() if sys in _OG12
            )
            og12_total = sum(sys_tank_count.get(s, 0) for s in _OG12)
            free_after_sticky = og12_total - og12_sticky_used
            if free_after_sticky < og12_arrival_demand:
                need = og12_arrival_demand - free_after_sticky
                # Oldest OG1/2 occupants first (those most ready to move on).
                evictable: list[tuple[str, int]] = []
                for bid, per_sys in prev_per_system.items():
                    if bid in tranog_arrivers:
                        continue
                    og12_n = sum(per_sys.get(s, 0) for s in _OG12)
                    if og12_n > 0:
                        evictable.append((bid, og12_n))
                evictable.sort(key=lambda x: _fifo_key(x[0]))
                for bid, og12_n in evictable:
                    if need <= 0:
                        break
                    take = min(og12_n, need)
                    new_per_sys = dict(prev_per_system[bid])
                    # Remove `take` tanks from OG1/2 (drain oldest systems first).
                    removed = 0
                    for sys in _OG12:
                        if removed >= take:
                            break
                        have = new_per_sys.get(sys, 0)
                        if have > 0:
                            cut = min(have, take - removed)
                            new_per_sys[sys] = have - cut
                            if new_per_sys[sys] == 0:
                                del new_per_sys[sys]
                            removed += cut
                    # Redistribute `removed` tanks to OG3-6 systems
                    # (round-robin so spread holds).
                    placed = 0
                    while placed < removed:
                        for sys in og36_systems:
                            if placed >= removed:
                                break
                            new_per_sys[sys] = new_per_sys.get(sys, 0) + 1
                            placed += 1
                    prev_per_system[bid] = new_per_sys
                    need -= take
                if need > 0:
                    warnings.append(
                        f"{week_label}: cannot evict enough OG1/2 capacity for "
                        f"TranOG arrivals; {need} tank-weeks of demand unmet"
                    )

        # ------ Plan-driven assignment for this week ------
        # Every (batch, week) is covered by migration_plan (built by the
        # coordinator). Phase B is now a pure projection of the plan into
        # per-system tank counts; the WHICH-system decision was made
        # deterministically upstream (Q-COORD.E: no scoring weights).
        sys_assigned: dict[str, int] = {s: 0 for s in sys_tank_count}
        sys_load_bio: dict[str, float] = {s: 0.0 for s in sys_tank_count}
        sys_load_feed: dict[str, float] = {s: 0.0 for s in sys_tank_count}

        for load in loads:
            per_sys: dict[str, int] = {}
            per_tank_bio = load.biomass_kg / load.tanks_needed if load.tanks_needed else 0.0
            per_tank_feed = load.feed_kg_day / load.tanks_needed if load.tanks_needed else 0.0

            if not (migration_plan and (load.batch_id, week_label) in migration_plan):
                raise RuntimeError(
                    f"Phase B: batch {load.batch_id} week {week_label} has no "
                    f"entry in migration_plan; coordinator coverage is required"
                )
            step = migration_plan[(load.batch_id, week_label)]
            for tid in step.keep_tanks + step.add_tanks:
                sys = tank_to_system.get(tid)
                if sys is None:
                    continue
                per_sys[sys] = per_sys.get(sys, 0) + 1
                sys_assigned[sys] = sys_assigned.get(sys, 0) + 1
                sys_load_bio[sys] = sys_load_bio.get(sys, 0.0) + per_tank_bio
                sys_load_feed[sys] = sys_load_feed.get(sys, 0.0) + per_tank_feed
            assignments.append(SystemAssignment(
                batch_id=load.batch_id,
                week_label=week_label,
                per_system=per_sys,
            ))
            prev_per_system[load.batch_id] = per_sys

    return assignments, warnings


# ============================================================
# Phase C — tank assignment (sticky + rotation)
# ============================================================

def phase_c_assign_tanks(
    system_assignments: list[SystemAssignment],
    initial_state: FacilityState,
    facility: FacilityConfig,
    biology_states_by_batch: Optional[dict[str, list[BatchWeekState]]] = None,
    migration_plan: Optional[dict] = None,
) -> tuple[list[TankAssignment], list[str]]:
    """Pick specific tank_ids per (batch, system, week).

    Sticky: same tank stays with the same batch week-over-week.
    Rotation: when picking a fresh tank in a system, prefer most-
    recently-emptied tank (DESIGN §7.3).
    Tanks freed when a batch reduces its footprint this week become
    available for other batches the same week — processed in
    FIFO-by-batch order from Phase B's assignment ordering.

    Tank-set freeze: a batch with avg_weight_g >= OG12_MOVE_LOCK_WT_G
    (1 kg) has its tank set frozen to last week's tanks until harvest
    depletes it. INV-4 forbids intra-OG1/2 transfers above 1 kg, and
    operationally fish at that size are "settled for harvest" — no
    Phase C reassignments are honored. Set shrinks only via Harvest
    events draining tanks from the inside.
    """
    warnings: list[str] = []

    # Build (batch_id, week_label) -> avg_weight_g lookup so the freeze
    # rule can be evaluated per (batch, week). Missing entries (e.g., a
    # batch with no per-week biology row in that week) default to <1 kg
    # and the normal logic applies.
    avg_wt_by_bw: dict[tuple[str, str], float] = {}
    if biology_states_by_batch:
        for batch_id, states in biology_states_by_batch.items():
            for s in states:
                avg_wt_by_bw[(batch_id, s.week_label)] = s.avg_weight_g

    # System → list of TankConfig (sorted by tank_id for deterministic picking)
    tanks_in_system: dict[str, list[TankConfig]] = {}
    for t in facility.tanks:
        if t.type == "OG":
            tanks_in_system.setdefault(t.system_id, []).append(t)
    for sys in tanks_in_system:
        tanks_in_system[sys].sort(key=lambda c: c.tank_id)

    # Build per-(batch, week) → per_system dict for quick lookup.
    by_batch_week: dict[tuple[str, str], dict[str, int]] = {}
    for a in system_assignments:
        by_batch_week[(a.batch_id, a.week_label)] = a.per_system

    # Sort weeks chronologically.
    sorted_weeks = sorted({a.week_label for a in system_assignments})

    # Initial tank → batch mapping (week before week 0).
    prev_tank_to_batch: dict[int, str] = {
        tid: t.batch_id for tid, t in initial_state.tanks_by_id.items()
        if t.batch_id
    }
    # Track most-recent-emptied per tank for rotation; bootstrap from initial state.
    last_emptied: dict[int, Optional[date]] = {
        tid: t.last_emptied_date for tid, t in initial_state.tanks_by_id.items()
    }

    # Process batches in FIFO order each week — derive from
    # system_assignments' existing order (they're added FIFO in Phase B).
    by_week_ordered: dict[str, list[SystemAssignment]] = {}
    for a in system_assignments:
        by_week_ordered.setdefault(a.week_label, []).append(a)

    assignments: list[TankAssignment] = []
    for week_label in sorted_weeks:
        # Pool of tanks per system available this week (excluding sticky carry-overs).
        # First, identify which tanks each batch will keep (sticky).
        sticky_keep: dict[str, set[int]] = {}  # batch_id → set of tank_ids kept
        above_1kg_this_week: set[str] = set()
        for a in by_week_ordered[week_label]:
            avg_wt = avg_wt_by_bw.get((a.batch_id, week_label), 0.0)
            if avg_wt >= OG12_MOVE_LOCK_WT_G:
                above_1kg_this_week.add(a.batch_id)
            counts = dict(a.per_system)
            keeps: set[int] = set()
            for tid, b in list(prev_tank_to_batch.items()):
                if b != a.batch_id:
                    continue
                tank_cfg = next((t for t in facility.tanks if t.tank_id == tid), None)
                if tank_cfg is None or tank_cfg.type != "OG":
                    continue
                if counts.get(tank_cfg.system_id, 0) > 0:
                    keeps.add(tid)
                    counts[tank_cfg.system_id] -= 1
            sticky_keep[a.batch_id] = keeps

        # Compute pool of free tanks per system: any tank not in sticky_keep
        # for the matching batch — i.e. tanks previously belonging to a batch
        # that's dropping them this week, plus tanks that were already empty.
        free_pool: dict[str, list[int]] = {sys: [] for sys in tanks_in_system}
        keep_all: set[int] = set().union(*sticky_keep.values()) if sticky_keep else set()
        for sys, tcs in tanks_in_system.items():
            for tc in tcs:
                if tc.tank_id not in keep_all:
                    free_pool[sys].append(tc.tank_id)
            # Rotation order: most-recently-emptied first; never-emptied last.
            free_pool[sys].sort(
                key=lambda tid: (last_emptied.get(tid) is None,
                                 last_emptied.get(tid) or date.min),
                reverse=True,
            )

        # Now allocate fresh tanks to batches that need more than their sticky keeps.
        # Narrow 1 kg rule (INV-4 surgical scope): batches at avg_wt >=
        # OG12_MOVE_LOCK_WT_G cannot ADD a new tank in any OG1/2 system,
        # because the only way to populate that tank is an intra-OG12
        # Transfer which INV-4 forbids. Drops and cross-system moves
        # (OG12 -> OG3+) remain allowed and are how a >=1 kg batch should
        # vacate OG1/2 to make room for the next TranOG entry.
        for a in by_week_ordered[week_label]:
            # Plan-driven path: when the migration_plan has a step for
            # this (batch, week), the tank IDs come directly from it.
            if migration_plan and (a.batch_id, week_label) in migration_plan:
                step = migration_plan[(a.batch_id, week_label)]
                assignments.append(TankAssignment(
                    batch_id=a.batch_id,
                    week_label=week_label,
                    tank_ids=sorted(set(step.keep_tanks) | set(step.add_tanks)),
                ))
                continue
            wanted = dict(a.per_system)
            kept_ids = sticky_keep[a.batch_id]
            for tid in kept_ids:
                # account for already-satisfied per-system count
                tc = next((t for t in facility.tanks if t.tank_id == tid), None)
                if tc and wanted.get(tc.system_id, 0) > 0:
                    wanted[tc.system_id] -= 1
            assigned_ids = list(kept_ids)
            for sys, need in wanted.items():
                if (
                    a.batch_id in above_1kg_this_week
                    and sys in OG12_SYSTEMS
                    and need > 0
                ):
                    warnings.append(
                        f"{week_label}: batch {a.batch_id} >=1 kg cannot add "
                        f"{need} new {sys} tank(s) (INV-4: intra-OG1/2 "
                        f"transfer forbidden); Phase B should route to OG3+"
                    )
                    continue
                for _ in range(need):
                    if not free_pool.get(sys):
                        warnings.append(
                            f"{week_label}: tank exhausted in {sys} for batch {a.batch_id} "
                            f"(needed {need} more)"
                        )
                        break
                    new_tid = free_pool[sys].pop(0)
                    assigned_ids.append(new_tid)
            assignments.append(TankAssignment(
                batch_id=a.batch_id,
                week_label=week_label,
                tank_ids=sorted(assigned_ids),
            ))

        # Update prev_tank_to_batch + last_emptied for next iteration.
        new_tank_to_batch: dict[int, str] = {}
        for ta in [x for x in assignments if x.week_label == week_label]:
            for tid in ta.tank_ids:
                new_tank_to_batch[tid] = ta.batch_id
        # Tanks that lost their batch this week → record emptied date.
        for tid, prev_b in prev_tank_to_batch.items():
            if tid not in new_tank_to_batch:
                # Find this week's start date for the emptied stamp
                # (use the BatchWeekLoad's week_start via inverse lookup;
                # for simplicity, leave None — rotation just sorts after
                # any tank that does have a date)
                last_emptied[tid] = last_emptied.get(tid)  # placeholder
        prev_tank_to_batch = new_tank_to_batch

    return assignments, warnings


# ============================================================
# Phase D — event emission + state population
# ============================================================

def _pick_fifo_move_in_batches(
    state: FacilityState,
    batch_meta: dict[str, BatchInput],
    control: ControlParams,
) -> list[str]:
    """Return all production batches with mature fish in FIFO order.

    The 6N pipeline pulls move-in fish from the oldest batch first; when
    that batch runs out, cascade to the next FIFO batch. This is what
    keeps pair drains at or above min_hv even when a single batch can't
    supply the full move-in target.
    """
    min_wt = control.min_harvest_weight_g
    out: list[tuple[date, float, str]] = []
    for b in batch_meta.values():
        if b.input_date is None:
            continue
        input_d = b.input_date.date() if hasattr(b.input_date, "date") else b.input_date
        prod_tanks = [
            t for t in state.tanks_by_id.values()
            if t.batch_id == b.batch_id and not t.is_empty
            and t.system_id not in _SIXN_SYSTEMS
            and t.avg_wt_g >= min_wt
        ]
        if not prod_tanks:
            continue
        max_wt = max(t.avg_wt_g for t in prod_tanks)
        out.append((input_d, -max_wt, b.batch_id))
    out.sort()
    return [bid for _, _, bid in out]


def _try_graded_move_in(
    state: FacilityState,
    batch_meta: dict[str, BatchInput],
    control: ControlParams,
    week_label: str,
    week_start_date: date,
    pair: tuple[int, int],
    transfer_events: list,
    warnings: list[str],
    min_fraction: float = 0.10,
) -> float:
    """Graded harvest fallback (DESIGN §5a) when no batch's avg_wt is
    above min_harvest_weight.

    Walks FIFO across batches; for each production tank where the
    average is below threshold but a fraction ≥ `min_fraction` of fish
    are at or above it, computes the upper/lower conditional means and
    emits a `GradedHarvest`: the big portion goes to the purge pair
    main tank (pickup), the small portion to a free OG3+ tank
    (retention). Returns the count moved into pickup. Fires for one
    tank max per week (single GradedHarvest event); the pair will be
    refilled normally on later weeks as biology grows the remaining
    fish past threshold.
    """
    from statistics import NormalDist as _ND
    min_hv = control.min_harvest_weight_g
    if min_hv <= 0:
        return 0.0
    _std = _ND()

    def frac_above(avg_wt: float, cv_pct: float, t: float) -> float:
        if avg_wt <= 0 or cv_pct <= 0:
            return 1.0 if avg_wt >= t else 0.0
        z = (t - avg_wt) / (avg_wt * cv_pct / 100.0)
        return max(0.0, min(1.0, 1.0 - _std.cdf(z)))

    fifo = sorted(
        batch_meta.values(),
        key=lambda b: b.input_date.date() if hasattr(b.input_date, "date")
        else (b.input_date or date.max),
    )
    chosen = None
    for b in fifo:
        cands = [
            t for t in state.tanks_by_id.values()
            if t.batch_id == b.batch_id and not t.is_empty
            and t.system_id not in _SIXN_SYSTEMS
            and t.avg_wt_g < min_hv  # not eligible for regular move-in
        ]
        # Prefer largest avg first (closer to threshold => fatter tail).
        cands.sort(key=lambda t: t.avg_wt_g, reverse=True)
        for t in cands:
            if frac_above(t.avg_wt_g, t.cv_pct or 16.0, min_hv) >= min_fraction:
                chosen = t
                break
        if chosen:
            break

    if chosen is None:
        return 0.0

    # Retention: lowest-id free OG3+ tank not in 6N pipeline.
    retention = next(
        (t for t in sorted(state.tanks_by_id.values(), key=lambda x: x.tank_id)
         if t.is_empty and t.type == "OG"
         and t.system_id not in _SIXN_SYSTEMS
         and t.system_id not in OG12_SYSTEMS),
        None,
    )
    if retention is None:
        warnings.append(
            f"{week_label}: graded move-in for {chosen.batch_id} "
            f"{chosen.location_id} declined (no free OG3+ retention tank)"
        )
        return 0.0

    cv = chosen.cv_pct or 16.0
    frac = frac_above(chosen.avg_wt_g, cv, min_hv)
    big_count = chosen.count * frac
    small_count = chosen.count - big_count
    big_avg, small_avg = upper_truncated_split(chosen.avg_wt_g, cv, min_hv)

    ev = GradedHarvest(
        batch_id=chosen.batch_id,
        event_date=week_start_date,
        source_tank_id=chosen.tank_id,
        pickup_tank_id=pair[0],
        pickup_count=big_count,
        pickup_avg_wt_g=big_avg,
        retention_tank_id=retention.tank_id,
        retention_count=small_count,
        retention_avg_wt_g=small_avg,
        cv_pct=cv,
    )
    warns = ev.apply(state)
    warnings.extend(warns)
    warnings.append(
        f"{week_label}: graded move-in for {chosen.batch_id} "
        f"{chosen.location_id} (avg {chosen.avg_wt_g:.0f}g, "
        f"{frac*100:.0f}% above {min_hv:.0f}g): "
        f"{big_count:.0f} fish ({big_avg:.0f}g) -> pickup, "
        f"{small_count:.0f} ({small_avg:.0f}g) -> retention "
        f"{retention.location_id}"
    )
    transfer_events.append(ev)
    return big_count


def _run_sixn_purge_week(
    state: FacilityState,
    pair_queue: list[tuple[int, int]],
    week_label: str,
    week_start_date: date,
    batch_meta: dict[str, BatchInput],
    control: ControlParams,
    harvest_events: list,
    transfer_events: list,
    warnings: list[str],
    move_in_target: Optional[float] = None,
    harvest_target: Optional[float] = None,
) -> None:
    """Run one week of the 6N purge pipeline.

    Mutates `pair_queue` in place: pops the front pair, harvests it,
    restocks it from FIFO production, pushes it to the back.

    Returns nothing — events are appended to `harvest_events` /
    `transfer_events` and `state` is mutated.
    """
    if not pair_queue:
        warnings.append(f"{week_label}: 6N purge queue empty — no harvest this week")
        return

    pair = pair_queue.pop(0)

    # 1. Harvest the pair's contents (both tanks if occupied).
    pair_drain_count = 0.0
    for tank_id in pair:
        tank = state.tanks_by_id.get(tank_id)
        if tank is None or tank.is_empty:
            continue
        pair_drain_count += tank.count
        ev = Harvest(
            batch_id=tank.batch_id,
            event_date=week_start_date,
            source_tank_id=tank_id,
            count=tank.count,
            avg_wt_g=tank.avg_wt_g,
            min_tank_control=0,  # 6N tanks are intentionally drained
        )
        warnings.extend(ev.apply(state))
        harvest_events.append(ev)

    # 1b. Supplemental harvest from FIFO production tanks to top the weekly
    # total up to the CLOSED-LOOP target. `harvest_target` is the realized-
    # biomass controller's decision for this week (clamped to
    # [min_hv, max_hv]); when biomass is over band it equals max_hv, so the
    # supplement pulls biomass down immediately rather than waiting two purge
    # cycles for the move-in to flow through. Falls back to the operational
    # floor (min_harvest_per_week) when no target is supplied. The 6N pair
    # drain may fall short (e.g. pair was thin from a small earlier move-in);
    # the deficit is harvested directly from the oldest mature production
    # batch's largest tank.
    floor = harvest_target if (harvest_target and harvest_target > 0) else 0.0
    min_h = max(control.min_harvest_per_week or 0, floor)
    if min_h > 0 and pair_drain_count < min_h:
        deficit = min_h - pair_drain_count
        supp_batches = _pick_fifo_move_in_batches(state, batch_meta, control)
        for supp_bid in supp_batches:
            if deficit <= 0:
                break
            supp_tanks = [
                t for t in state.tanks_by_id.values()
                if t.batch_id == supp_bid and not t.is_empty
                and t.system_id not in _SIXN_SYSTEMS
                and t.avg_wt_g >= control.min_harvest_weight_g
            ]
            # Biggest avg_wt first: harvest mature/big-class fish ahead of
            # smaller ones. Big-class tanks drain → free for size-class
            # migration (small class moves up via grade-split).
            supp_tanks.sort(key=lambda t: t.avg_wt_g, reverse=True)
            for src in supp_tanks:
                if deficit <= 0:
                    break
                take = min(deficit, src.count)
                if take <= 0:
                    continue
                ev = Harvest(
                    batch_id=supp_bid,
                    event_date=week_start_date,
                    source_tank_id=src.tank_id,
                    count=take,
                    avg_wt_g=src.avg_wt_g,
                    min_tank_control=control.min_tank_control,
                )
                warnings.extend(ev.apply(state))
                harvest_events.append(ev)
                deficit -= take

    # 2. Pick FIFO move-in source batches (cascade list).
    move_in_batches = _pick_fifo_move_in_batches(state, batch_meta, control)
    if not move_in_batches:
        # Last-resort: graded move-in (DESIGN §5a) — peel the
        # above-threshold tail from a tank whose average is below
        # threshold but has a meaningful upper portion.
        moved = _try_graded_move_in(
            state, batch_meta, control, week_label, week_start_date,
            pair, transfer_events, warnings,
        )
        if moved <= 0:
            warnings.append(
                f"{week_label}: 6N pair {pair} harvested but no production "
                "batch above min_harvest_weight available for move-in (pair "
                "stays in rotation, will be empty next harvest)"
            )
        pair_queue.append(pair)
        return

    # 3. Move-in target — Layer 2 demand 2 weeks ahead, clamped to
    #    [min_harvest_per_week, max_harvest_per_week]. The min clamp
    #    guarantees pair drains never fall below the operational floor
    #    when sufficient production inventory exists.
    min_h = control.min_harvest_per_week or 0
    max_h = control.max_harvest_per_week or min_h
    if move_in_target is not None and move_in_target > 0:
        target = max(min_h, min(max_h, move_in_target))
    else:
        target = min_h
    if target <= 0:
        pair_queue.append(pair)
        return

    # 4. Pull from FIFO batches in cascade. When the oldest batch's
    #    production tanks can't fill the target, fall through to the next
    #    FIFO batch. This keeps move-in size >= min_h whenever total
    #    mature inventory >= min_h, and the resulting pair drain 2 weeks
    #    later also >= min_h (minus mortality).
    main_tank_id = pair[0]  # prefer main (61/63/65) over sister (67/69/71)
    count_moved = 0
    contributing_batches: list[str] = []
    for move_in_batch in move_in_batches:
        if count_moved >= target:
            break
        src_tanks = [
            t for t in state.tanks_by_id.values()
            if t.batch_id == move_in_batch and not t.is_empty
            and t.system_id not in _SIXN_SYSTEMS
            and t.avg_wt_g >= control.min_harvest_weight_g
        ]
        # Biggest avg_wt first: prefer to move the largest fish into the
        # pair (they'll be harvested in 2 weeks; we want big fish out).
        src_tanks.sort(key=lambda t: t.avg_wt_g, reverse=True)
        moved_this_batch = 0
        for src in src_tanks:
            if count_moved >= target:
                break
            take = min(target - count_moved, src.count)
            if take <= 0:
                continue
            # First contributor goes to main tank; later contributors
            # would need a sister tank, but we keep the move into a
            # single physical tank (main) since INV-1 (one batch per
            # tank) forbids mixing batches. So if there are multiple
            # contributing batches, the LATER ones can't share main tank
            # (already taken by first batch). Use sister tank for the
            # second batch's contribution.
            if moved_this_batch == 0 and contributing_batches:
                dest_tank_id = pair[1]  # sister tank for second+ batch
            else:
                dest_tank_id = main_tank_id
            ev = Transfer(
                batch_id=move_in_batch,
                event_date=week_start_date,
                source_tank_id=src.tank_id,
                destinations=[TankAllocation(
                    tank_id=dest_tank_id, count=take,
                    avg_wt_g=src.avg_wt_g, cv_pct=src.cv_pct,
                )],
            )
            warns = ev.apply(state)
            warnings.extend(warns)
            transfer_events.append(ev)
            count_moved += take
            moved_this_batch += take
        if moved_this_batch > 0:
            contributing_batches.append(move_in_batch)

    if count_moved == 0:
        warnings.append(
            f"{week_label}: 6N pair {pair} move-in failed (no fish moved); "
            f"pair will be empty next harvest"
        )
    elif count_moved < min_h:
        warnings.append(
            f"{week_label}: 6N pair {pair} move-in short of min_hv "
            f"({count_moved:,.0f} < {min_h:,.0f}); insufficient mature inventory"
        )

    # Push pair back to queue (will be next harvested after the other pairs ahead).
    pair_queue.append(pair)


def _emit_transfers_for_batch_diff(
    state: FacilityState,
    batch_id: str,
    prev_tanks: set,
    this_tanks: set,
    event_date: date,
    transfer_events: list,
    warnings: list[str],
) -> None:
    """Rebalance a batch's fish across its new tank set via Transfer events.

    Compute target_count = (total fish in prev_tanks) / len(this_tanks).
    Tanks with count > target send surplus to tanks with count < target
    via Transfer events. Sources (dropped tanks) are fully drained;
    dests (new tanks) start at zero and are filled up to target. Result:
    each this_tank ends at ~target count, eliminating density spikes
    from earlier consolidations or uneven hydrations.
    """
    sources = sorted(prev_tanks - this_tanks)
    dests = sorted(this_tanks - prev_tanks)
    kept = sorted(prev_tanks & this_tanks)

    if not sources and not dests:
        return

    # Empty-batch edge case: no destinations and no kept tanks. Plan says the
    # batch has nowhere to live this week, but the source still holds fish.
    # Per the continuity rule, only Events drain tanks — keep the fish in
    # place and let next week's plan reconcile (or surface the gap as a
    # bottleneck if it persists).
    if not this_tanks:
        for s in sources:
            tank = state.tanks_by_id.get(s)
            if tank is not None and not tank.is_empty:
                warnings.append(
                    f"{event_date}: batch {batch_id} plan has no this-tank "
                    f"but source {tank.location_id} still holds {tank.count:.0f} "
                    f"fish; retained in place (no force-empty)"
                )
        return

    # Total fish currently held across prev_tanks (sources + kept).
    total_count = 0.0
    avg_wt_sum = 0.0
    cv_sum = 0.0
    n_filled = 0
    for tid in prev_tanks:
        tank = state.tanks_by_id.get(tid)
        if tank is not None and not tank.is_empty:
            total_count += tank.count
            avg_wt_sum += tank.avg_wt_g * tank.count
            cv_sum += tank.cv_pct
            n_filled += 1

    if total_count <= 0:
        # Nothing to move (batch already empty across all prev_tanks).
        # Tanks should already be is_empty via Event apply; if any source is
        # still marked non-empty something upstream is inconsistent.
        for s in sources:
            tank = state.tanks_by_id.get(s)
            if tank is not None and not tank.is_empty:
                warnings.append(
                    f"{event_date}: batch {batch_id} source {tank.location_id} "
                    f"holds {tank.count:.0f} fish but rebalance saw "
                    f"total_count=0; retained in place (no force-empty)"
                )
        return

    target_per_tank = total_count / len(this_tanks)

    # Build over/under lists.
    overs: list[list] = []   # [tank_id, surplus, tank_obj]
    unders: list[list] = []  # [tank_id, deficit, tank_obj]
    for tid in kept:
        tank = state.tanks_by_id.get(tid)
        if tank is None:
            continue
        cur = tank.count if not tank.is_empty else 0.0
        if cur > target_per_tank + 0.5:
            overs.append([tid, cur - target_per_tank, tank])
        elif cur < target_per_tank - 0.5:
            unders.append([tid, target_per_tank - cur, tank])
    for tid in sources:
        tank = state.tanks_by_id.get(tid)
        if tank is None or tank.is_empty:
            continue
        overs.append([tid, tank.count, tank])
    for tid in dests:
        # New tanks start at 0; they need the full target_per_tank.
        unders.append([tid, target_per_tank, None])

    # Largest surpluses → largest deficits.
    overs.sort(key=lambda x: -x[1])
    unders.sort(key=lambda x: -x[1])

    i = j = 0
    src_is_drop = {tid: True for tid in sources}
    while i < len(overs) and j < len(unders):
        if overs[i][1] <= 0.5:
            i += 1; continue
        if unders[j][1] <= 0.5:
            j += 1; continue
        take = min(overs[i][1], unders[j][1])
        src_id, _, src_tank = overs[i]
        dst_id = unders[j][0]
        # Never set leaves_source_empty=True at the rebalance level —
        # earlier transfers from the same source can be REJECTED by
        # Transfer.apply (INV-4 etc.), so the rebalance can't know the
        # source's actual remaining count. Setting True would empty the
        # source after a successful tail-end transfer, losing whatever
        # fish were rejected upstream. Transfer.apply self-empties when
        # the actual remaining count is ~0.
        ev = Transfer(
            batch_id=batch_id, event_date=event_date, source_tank_id=src_id,
            destinations=[TankAllocation(
                tank_id=dst_id, count=take,
                avg_wt_g=src_tank.avg_wt_g, cv_pct=src_tank.cv_pct,
            )],
            leaves_source_empty=False,
        )
        warnings.extend(ev.apply(state))
        transfer_events.append(ev)
        overs[i][1] -= take
        unders[j][1] -= take
        if overs[i][1] < 0.5:
            i += 1
        if unders[j][1] < 0.5:
            j += 1

    # If any source still has fish after the rebalance, ROUTE them to
    # this-tanks holding the same batch (via Transfer event). If routing is
    # refused (INV-4: cannot move >=1 kg fish between OG1/2 tanks) or no
    # this-tank can receive them, the fish stay in the source — only Events
    # may drain tanks. Next week's plan reconciles the residual.
    for s in sources:
        tank = state.tanks_by_id.get(s)
        if tank is None or tank.is_empty:
            continue
        candidates = [
            state.tanks_by_id[t] for t in this_tanks
            if t in state.tanks_by_id
            and state.tanks_by_id[t].batch_id == batch_id
        ]
        if not candidates:
            warnings.append(
                f"{event_date}: batch {batch_id} source {tank.location_id} "
                f"still holds {tank.count:.0f} fish but no this-tank holds "
                f"the same batch; retained in place (no force-empty)"
            )
            continue
        candidates.sort(key=lambda t: t.count)
        dest_id = candidates[0].tank_id
        ev = Transfer(
            batch_id=batch_id, event_date=event_date, source_tank_id=s,
            destinations=[TankAllocation(
                tank_id=dest_id, count=tank.count,
                avg_wt_g=tank.avg_wt_g, cv_pct=tank.cv_pct,
            )],
            leaves_source_empty=True,
        )
        ev_warns = ev.apply(state)
        warnings.extend(ev_warns)
        transfer_events.append(ev)
        # If the transfer was refused (INV-4 above 1 kg etc.), the fish
        # stay in the source. Continuity rule: only Events drain tanks.
        if not tank.is_empty:
            warnings.append(
                f"{event_date}: residual {tank.count:.0f} fish in "
                f"{tank.location_id} (batch {batch_id}) could not be routed "
                f"to this-tank #{dest_id} (likely INV-4); retained in place"
            )
    return  # All cases handled by rebalance.


def _even_out_density(
    state: FacilityState,
    batch_id: str,
    event_date: date,
    transfer_events: list,
    warnings: list[str],
) -> None:
    """Even fish across a batch's tanks when any tank is over density cap.

    Fixes ProductionReport over-concentration: PR can stock a batch
    unevenly (e.g. 195k in one tank + 95k in another). The set-diff
    rebalance only fires when the tank SET changes, so an unchanged but
    uneven set keeps a tank over 95 kg/m^3 forever. This pass evens the
    distribution via legal Transfers (the lesser of two evils: a few
    transfers to reach an under-cap config).

    Legality (enforced here + by Transfer.apply):
      - OG1/2 tanks: only when avg_wt < 1 kg (INV-4 forbids intra-OG1/2
        moves at/above 1 kg). Sub-1 kg moves between ANY OG1/2 tanks are
        allowed, so OG1 and OG2 are evened as one pool.
      - OG3-6 tanks: any weight (any-to-any transfer allowed). OG6N is
        pipeline-owned — excluded.
    Only triggers when a tank in the group exceeds its density cap, so
    transfers are emitted only where actually needed.
    """
    tanks = [t for t in state.tanks_by_id.values()
             if t.batch_id == batch_id and not t.is_empty]
    if len(tanks) < 2:
        return

    og12_sub = [t for t in tanks
                if t.system_id in OG12_SYSTEMS
                and t.avg_wt_g < OG12_MOVE_LOCK_WT_G]
    og12_all = [t for t in tanks if t.system_id in OG12_SYSTEMS]
    og36 = [t for t in tanks
            if t.system_id not in OG12_SYSTEMS and t.system_id != "OG6N"]

    def _equalize(group):
        """Move fish between tanks in `group` to equalize counts; emits
        Transfers for the source → dest pairs needed. Returns nothing."""
        if len(group) < 2:
            return
        if not any(t.max_density_kg_m3 > 0
                   and t.density_kg_m3 > t.max_density_kg_m3
                   for t in group):
            return
        total = sum(t.count for t in group)
        target = total / len(group)
        overs = sorted([[t.tank_id, t.count - target, t]
                        for t in group if t.count > target + 0.5],
                       key=lambda x: -x[1])
        unders = sorted([[t.tank_id, target - t.count, t]
                         for t in group if t.count < target - 0.5],
                        key=lambda x: -x[1])
        i = j = 0
        while i < len(overs) and j < len(unders):
            if overs[i][1] <= 0.5:
                i += 1; continue
            if unders[j][1] <= 0.5:
                j += 1; continue
            take = min(overs[i][1], unders[j][1])
            src_id, _, src_tank = overs[i]
            dst_id = unders[j][0]
            ev = Transfer(
                batch_id=batch_id, event_date=event_date,
                source_tank_id=src_id,
                destinations=[TankAllocation(
                    tank_id=dst_id, count=take,
                    avg_wt_g=src_tank.avg_wt_g, cv_pct=src_tank.cv_pct,
                )],
                leaves_source_empty=False,
            )
            warnings.extend(ev.apply(state))
            transfer_events.append(ev)
            overs[i][1] -= take
            unders[j][1] -= take
            if overs[i][1] < 0.5:
                i += 1
            if unders[j][1] < 0.5:
                j += 1

    # Pass 1: sub-1kg OG1/2 group (legal intra-OG1/2).
    _equalize(og12_sub)
    # Pass 2: OG3-6 group (any weight, any-to-any allowed).
    _equalize(og36)
    # Pass 3: cross-scope OG1/2 -> OG3-6 for over-cap OG1/2 tanks that
    # passes 1+2 couldn't fix. INV-4 forbids INTRA-OG1/2 moves at >=1 kg,
    # but the system-progression law (DESIGN §4) explicitly allows
    # OG1/2 -> OG3-6 transfer at any weight ("outbound to 3/4/5/6 allowed
    # any time"). When a batch has an over-cap OG1/2 tank AND an
    # under-cap OG3-6 tank, equalize across the boundary. Source pool is
    # restricted to OG1/2-over-cap tanks (so we never push OG3-6 fish
    # back into OG1/2 — that would be operationally backwards).
    og12_over = [t for t in og12_all
                 if t.max_density_kg_m3 > 0
                 and t.density_kg_m3 > t.max_density_kg_m3]
    og36_under = [t for t in og36
                  if t.max_density_kg_m3 > 0
                  and t.density_kg_m3 < t.max_density_kg_m3]
    # Headroom: leave the destination at <= 90% of cap so the Phase D
    # density-trigger Grade (also runs in this week) doesn't fire on the
    # new destination — that would spawn extra grade events and could
    # cascade-violate other tanks.
    HEADROOM_PCT = 0.90
    for src in og12_over:
        if src.avg_wt_g <= 0:
            continue
        for dst in og36_under:
            if dst.avg_wt_g <= 0:
                continue
            src_cap_fish = (src.max_density_kg_m3 * src.volume_m3
                            * 1000.0 / src.avg_wt_g)
            dst_cap_fish = (dst.max_density_kg_m3 * dst.volume_m3
                            * 1000.0 / dst.avg_wt_g)
            shed = src.count - src_cap_fish
            room = dst_cap_fish * HEADROOM_PCT - dst.count
            take = min(shed, room)
            if take <= 0.5:
                continue
            ev = Transfer(
                batch_id=batch_id, event_date=event_date,
                source_tank_id=src.tank_id,
                destinations=[TankAllocation(
                    tank_id=dst.tank_id, count=take,
                    avg_wt_g=src.avg_wt_g, cv_pct=src.cv_pct,
                )],
                leaves_source_empty=False,
            )
            warnings.extend(ev.apply(state))
            transfer_events.append(ev)
            if src.count <= src_cap_fish + 0.5:
                break


def _realized_facility_metrics(
    state: FacilityState,
    batch_meta: dict[str, BatchInput],
    tables: BiologyTables,
    min_harvest_weight_g: float,
) -> tuple[float, float, float, float]:
    """Realized facility totals from the LIVE state, for the closed-loop
    harvest controller.

    Returns ``(biomass_kg, growth_kg_this_week, feed_kg_day, oldest_mature_avg_wt_g)``.

    - ``biomass_kg`` = ``state.total_biomass()`` (matches the reported
      facility biomass the operator sees).
    - growth + feed mirror ``biology.project_in_flight_batch``'s per-day
      formulas (SGR/FCR interpolation, ×7 weekly) so the realized decision is
      consistent with the projection it replaces. Only SW-stage fish grow/feed.
    - ``oldest_mature_avg_wt`` = avg weight of the FIFO-oldest batch whose
      fish are at/above harvest weight — the weight used to convert a growth
      *mass* into a harvest *count* (same convention as the scheduler).
    """
    fac_bio = 0.0
    fac_growth_kg = 0.0
    fac_feed_kg_day = 0.0
    mature: list[tuple[date, float]] = []  # (input_date, avg_wt_g)
    for t in state.tanks_by_id.values():
        if t.is_empty:
            continue
        bio = t.biomass_kg
        fac_bio += bio
        batch = batch_meta.get(t.batch_id)
        if t.stage == "SW":
            sgr_base = _interp(t.avg_wt_g, tables.sgr_size_g, tables.sgr_sw_pct_day)
            sgr_eff = sgr_base * (batch.sgr_correction if batch else 1.0)
            fac_growth_kg += bio * (sgr_eff / 100.0) * 7.0
            fcr_curve = tables.fcr_by_model.get(
                _fcr_model_key(batch.fcr_model) if batch else "", [])
            if fcr_curve:
                fcr = _interp(t.avg_wt_g, tables.fcr_size_g, fcr_curve)
                fac_feed_kg_day += bio * (sgr_eff / 100.0) * fcr
        if t.avg_wt_g >= min_harvest_weight_g and batch is not None:
            mature.append((_as_date(batch.input_date), t.avg_wt_g))

    oldest_mature_avg_wt = 0.0
    if mature:
        mature.sort(key=lambda m: m[0])
        oldest_mature_avg_wt = mature[0][1]
    return fac_bio, fac_growth_kg, fac_feed_kg_day, oldest_mature_avg_wt


def phase_d_emit_events(
    load_table: list[BatchWeekLoad],
    tank_assignments: list[TankAssignment],
    harvest_demands: list[HarvestDemand],
    splits: list[SizeClassSplit],
    initial_state: FacilityState,
    facility: FacilityConfig,
    control: ControlParams,
    batch_meta: dict[str, BatchInput],
    tables: BiologyTables,
    facility_limits: Optional[FacilityLimits] = None,
) -> tuple[FacilityState, list[TranOGEntry], list[Transfer], list[Harvest],
           list[BatchLocationRow], list[str]]:
    """Walk the plan week by week; emit events from assignment diff +
    harvest demand; populate a fresh FacilityState with per-week
    tank states; record BatchLocationRows.
    """
    warnings: list[str] = []
    tranog_events: list[TranOGEntry] = []
    transfer_events: list[Transfer] = []
    harvest_events: list[Harvest] = []
    grade_events: list[Grade] = []
    locations: list[BatchLocationRow] = []
    if facility_limits is None:
        facility_limits = FacilityLimits()

    # Quick lookups.
    load_by_bw: dict[tuple[str, str], BatchWeekLoad] = {
        (l.batch_id, l.week_label): l for l in load_table
    }
    tank_by_bw: dict[tuple[str, str], list[int]] = {
        (a.batch_id, a.week_label): a.tank_ids for a in tank_assignments
    }
    demands_by_week: dict[str, list[HarvestDemand]] = {}
    for d in harvest_demands:
        demands_by_week.setdefault(d.week_label, []).append(d)
    # Aggregate Layer 2 demand per week as the 6N move-in target proxy.
    # The 6N pipeline harvests "two weeks of purge later", so a move-in
    # at week T should match what Layer 2 plans to harvest at week T + 2
    # (clamped to [min_harvest_per_week, max_harvest_per_week] in the
    # pipeline). This lets the dynamic harvest count (min while biomass
    # is building, ramping to max as facility cap approaches) carry
    # through to the pipeline's actual throughput.
    weekly_demand_count: dict[str, float] = {}
    for d in harvest_demands:
        weekly_demand_count[d.week_label] = weekly_demand_count.get(d.week_label, 0.0) + d.count
    splits_by_batch = {s.batch_id: s for s in splits}

    # Active batches per week (set of batch_ids).
    active_by_week: dict[str, set[str]] = {}
    for a in tank_assignments:
        active_by_week.setdefault(a.week_label, set()).add(a.batch_id)
    sorted_weeks = sorted(active_by_week.keys())

    # Tank config lookup.
    tank_cfg_by_id: dict[int, TankConfig] = {t.tank_id: t for t in facility.tanks}

    # Clone initial_state into a fresh FacilityState for execution.
    state = FacilityState.from_facility_config(facility, today=initial_state.today)
    for tid, t in initial_state.tanks_by_id.items():
        st = state.tanks_by_id.get(tid)
        if st is None:
            continue
        st.batch_id = t.batch_id
        st.count = t.count
        st.avg_wt_g = t.avg_wt_g
        st.cv_pct = t.cv_pct
        st.stage = t.stage
        st.last_emptied_date = t.last_emptied_date

    # Previous-week tank → batch map (initial state).
    prev_assignment: dict[int, str] = {
        tid: t.batch_id for tid, t in state.tanks_by_id.items() if t.batch_id
    }

    # 6N purge pipeline queue (only meaningful while in purge mode; ignored
    # if the forecast starts in production mode). Pairs are ordered with
    # the lowest-count pair first so W1 harvests it (user H10).
    try:
        sixn_pair_queue: list[tuple[int, int]] = list(initial_purge_pair_queue(state))
    except RuntimeError as e:
        warnings.append(str(e))
        sixn_pair_queue = []

    # Compute forecast_start once for day-by-day biology.
    forecast_start = initial_state.today

    # Map week_label → (start, end) date range.
    week_ranges: dict[str, tuple[date, date]] = {}
    for label in sorted_weeks:
        wload = next((l for l in load_table if l.week_label == label), None)
        if wload is None:
            continue
        week_ranges[label] = (wload.week_start, wload.week_start + timedelta(days=7))

    for week_label in sorted_weeks:
        # Build this week's tank → batch map.
        this_assignment: dict[int, str] = {}
        for a in [a for a in tank_assignments if a.week_label == week_label]:
            for tid in a.tank_ids:
                this_assignment[tid] = a.batch_id

        ws_we = week_ranges.get(week_label)
        week_start_date = ws_we[0] if ws_we else _as_date(control.forecast_start)
        purge_this_week = is_purge_mode(control, week_start_date)

        # Closed-loop harvest control (DESIGN: "close the loop" + forward-
        # looking #2). Decide harvest against the REALIZED state — not the
        # scheduler's decoupled projection — via TWO levers:
        #
        #   * move_in_target — PRIMARY, depuration-respecting. Fish moved into
        #     6N now drain after the purge lead time (the pair rotation), so we
        #     PROJECT biomass to that harvest week and pre-size the move-in to
        #     land on the setpoint, pre-empting the growth spike instead of
        #     chasing it (predictive_move_in_count).
        #   * harvest_target — the immediate supplemental harvest from
        #     production tanks (no lag, but bypasses purge). Held at the
        #     operational floor; raised to full capacity only when biomass is
        #     already over the upper band, as a safety net for an acute spike
        #     (e.g. an unmodelled TranOG arrival) the lagged channel can't catch.
        fac_bio, fac_growth_kg, fac_feed_kg_day, oldest_wt = (
            _realized_facility_metrics(
                state, batch_meta, tables, control.min_harvest_weight_g)
        )
        bio_cap = resolve_facility_cap(METRIC_BIOMASS, week_label, facility_limits, control)
        max_hv = resolve_facility_cap(METRIC_MAX_HARVEST, week_label, facility_limits, control)
        min_hv = resolve_facility_cap(METRIC_MIN_HARVEST, week_label, facility_limits, control)
        dev = control.facility_biomass_deviation_pct or 0.0
        weekly_max = max_hv if max_hv else float("inf")
        setpoint = bio_cap * _SETPOINT_FRACTION if bio_cap is not None else None

        # Committed harvest already locked into the purge pipeline: the pairs
        # in the queue drain over the next `lead` weeks, each at least the
        # operational floor (the supplemental top-up).
        lead = max(1, len(sixn_pair_queue))
        floor_mass_kg = (min_hv or 0.0) * oldest_wt / 1000.0
        committed_kg = 0.0
        for pair in sixn_pair_queue:
            pair_bio = sum(
                state.tanks_by_id[tid].biomass_kg
                for tid in pair
                if tid in state.tanks_by_id and not state.tanks_by_id[tid].is_empty
            )
            committed_kg += max(pair_bio, floor_mass_kg)

        move_in_target = predictive_move_in_count(
            total_biomass=fac_bio,
            growth_kg_week=fac_growth_kg,
            committed_harvest_kg=committed_kg,
            setpoint=setpoint,
            lead_weeks=lead,
            harvest_avg_wt_g=oldest_wt,
            weekly_min=min_hv or 0.0,
            weekly_max=weekly_max,
        )
        # Immediate supplement (reactive deadbeat): harvest this week's growth
        # plus the CURRENT deviation from setpoint, realised now with no lag.
        # The predictive move-in pre-positions the steady harvest; this catches
        # what prediction misses (rate-limited move-in, unmodelled TranOG
        # arrivals, growth/weight estimate error) so biomass doesn't spike
        # while the lagged channel catches up. Floor when at/under setpoint.
        if oldest_wt > 0 and setpoint is not None:
            shed_kg = fac_growth_kg + (fac_bio - setpoint)
            harvest_target = max(min_hv or 0.0,
                                 min(weekly_max, shed_kg * 1000.0 / oldest_wt))
        else:
            harvest_target = min_hv or 0.0

        # Harvest engine — 6N purge pipeline when in purge mode, else Layer-2 FIFO.
        if purge_this_week:
            _run_sixn_purge_week(
                state=state,
                pair_queue=sixn_pair_queue,
                week_label=week_label,
                week_start_date=week_start_date,
                batch_meta=batch_meta,
                control=control,
                harvest_events=harvest_events,
                transfer_events=transfer_events,
                warnings=warnings,
                move_in_target=move_in_target,
                harvest_target=harvest_target,
            )
        else:
            # Production mode: fall back to Layer-2 demand application.
            for hd in demands_by_week.get(week_label, []):
                remaining = hd.count
                while remaining > 0.5:
                    src_tanks = [t for t in state.tanks_by_id.values()
                                 if t.batch_id == hd.batch_id and not t.is_empty]
                    if not src_tanks:
                        warnings.append(
                            f"{week_label}: batch {hd.batch_id} has no occupied tanks; "
                            f"harvest demand of {remaining:.0f} fish dropped"
                        )
                        break
                    src_tanks.sort(key=lambda t: t.count)
                    src = src_tanks[0]
                    take = min(remaining, src.count)
                    ev = Harvest(
                        batch_id=hd.batch_id, event_date=week_start_date,
                        source_tank_id=src.tank_id, count=take,
                        avg_wt_g=src.avg_wt_g,
                        min_tank_control=control.min_tank_control,
                    )
                    warnings.extend(ev.apply(state))
                    harvest_events.append(ev)
                    remaining -= take
                    if take <= 0:
                        break

        # Emit Transfer events for assignment diff (after harvests).
        # In purge mode the 6N tanks are owned by the purge pipeline (see
        # _run_sixn_purge_week above) — exclude them from the diff so we
        # don't double-route or clobber pipeline-managed move-in fish.
        if ws_we is not None:
            transfer_date = ws_we[0]
            sixn_ids = SIXN_MAIN_TANKS | SIXN_SISTER_TANKS
            prev_by_batch: dict[str, set] = {}
            this_by_batch: dict[str, set] = {}
            for tid, b in prev_assignment.items():
                if purge_this_week and tid in sixn_ids:
                    continue
                prev_by_batch.setdefault(b, set()).add(tid)
            for tid, b in this_assignment.items():
                if purge_this_week and tid in sixn_ids:
                    continue
                this_by_batch.setdefault(b, set()).add(tid)
            # Process batches in order of net tank change: batches LOSING
            # tanks (negative net change) first — they vacate via
            # consolidation Transfers; batches GAINING tanks (positive)
            # second — they fill the just-vacated tanks. Avoids INV-1
            # collisions when two batches swap a tank in the same week
            # while preserving the rebalance's fish-consolidation
            # behavior (no force-empty losses).
            def _net_change(bid: str) -> int:
                p = len(prev_by_batch.get(bid, set()))
                n = len(this_by_batch.get(bid, set()))
                return n - p
            # Tiebreak by batch_id: a set of batch_id STRINGS iterates in
            # hash-randomized order, so sorting by net_change alone left
            # equal-net-change batches (e.g. two batches each gaining one
            # tank and competing for the same free tanks) in a
            # PYTHONHASHSEED-dependent order — making the whole forecast
            # nondeterministic across runs. The batch_id tiebreak pins it.
            batches_in_order = sorted(
                set(prev_by_batch) | set(this_by_batch),
                key=lambda bid: (_net_change(bid), bid),
            )
            for b in batches_in_order:
                p = prev_by_batch.get(b, set())
                n = this_by_batch.get(b, set())
                if p == n:
                    continue
                _emit_transfers_for_batch_diff(
                    state, b, p, n, transfer_date, transfer_events, warnings,
                )
            # Even-out pass: fix PR/residual over-concentration by
            # leveling fish across each batch's tanks where a tank is
            # over density cap and the moves are legal. Runs for ALL
            # active batches (including unchanged sets the diff skipped).
            for b in sorted(set(prev_by_batch) | set(this_by_batch)):
                _even_out_density(
                    state, b, transfer_date, transfer_events, warnings,
                )

        # Day-by-day biology + TranOG entries within this week.
        ws_we = week_ranges.get(week_label)
        if ws_we is not None:
            ws_date, we_date = ws_we
            day = ws_date
            while day < we_date:
                # Apply TranOG entries scheduled for this day.
                for split in splits:
                    if _as_date(split.tran_og_date) != day:
                        continue
                    # Find this batch's Phase C tank assignment for THIS week.
                    ta = next(
                        (a for a in tank_assignments
                         if a.week_label == week_label and a.batch_id == split.batch_id),
                        None,
                    )
                    if ta is None:
                        continue
                    tanks_obj = [state.tanks_by_id[tid] for tid in ta.tank_ids
                                 if tid in state.tanks_by_id and state.tanks_by_id[tid].is_empty]
                    # FW entries must NEVER be silently dropped — missing an
                    # arrival breaks the biomass model downstream. If the
                    # migration plan's assigned tanks are all occupied
                    # (precalc/runtime divergence in purge mode), fall back to
                    # any empty OG1/2 tank as a last resort. Plan is the hint;
                    # the FW->OG floor is the rule.
                    # TRANOG MINIMUM TANK FLOOR: cohort gets max(plan, 4,
                    # ceil(biomass / 95kg/m^3 / 1720m^3)) tanks. Plan can
                    # under-allocate when free_pool tracking diverges
                    # from actual tank state (purge mode); the operational
                    # rule "TranOG must use max(R28, 4) tanks" + density
                    # cap force a higher floor at runtime.
                    plan_n = len(ta.tank_ids) if ta.tank_ids else 0
                    cohort_kg = split.post_cull_count * (
                        split.post_cull_avg_wt_g / 1000.0
                    )
                    density_n = max(
                        1, math.ceil(cohort_kg / (95.0 * 1720.0))
                    )
                    n_needed = max(plan_n, 4, density_n)
                    if len(tanks_obj) < n_needed:
                        already_ids = {t.tank_id for t in tanks_obj}
                        # Stage 1 fallback: any empty OG1/2 tank.
                        og12_fallback = [
                            t for t in state.tanks_by_id.values()
                            if t.is_empty and t.system_id in OG12_SYSTEMS
                            and t.tank_id not in already_ids
                        ]
                        og12_fallback.sort(key=lambda t: t.tank_id)
                        while len(tanks_obj) < n_needed and og12_fallback:
                            picked = og12_fallback.pop(0)
                            tanks_obj.append(picked)
                            already_ids.add(picked.tank_id)
                            warnings.append(
                                f"{week_label}: TranOG {split.batch_id} "
                                f"fell back to OG1/2 tank #{picked.tank_id} "
                                f"({picked.location_id}); plan assigned {ta.tank_ids}"
                            )
                        # Stage 2 fallback (density-preservation): if OG1/2
                        # is genuinely exhausted, overflow into freshly-empty
                        # OG3+ tanks. The biology rule "TranOG entries land
                        # in OG1/2" gets degraded only when capacity forces
                        # it, but the cohort gets spread across enough tanks
                        # to keep density within cap. Better than cramming
                        # the full ~290k fish cohort into 1 OG1/2 tank where
                        # density grows to 200+ kg/m^3 over the lifecycle.
                        if len(tanks_obj) < n_needed:
                            og3plus_fallback = [
                                t for t in state.tanks_by_id.values()
                                if t.is_empty
                                and t.system_id not in OG12_SYSTEMS
                                and t.type == "OG"
                                and t.system_id != "OG6N"   # owned by 6N pipeline
                                and t.tank_id not in already_ids
                            ]
                            og3plus_fallback.sort(key=lambda t: t.tank_id)
                            while len(tanks_obj) < n_needed and og3plus_fallback:
                                picked = og3plus_fallback.pop(0)
                                tanks_obj.append(picked)
                                already_ids.add(picked.tank_id)
                                warnings.append(
                                    f"{week_label}: TranOG {split.batch_id} "
                                    f"OVERFLOW to OG3+ tank #{picked.tank_id} "
                                    f"({picked.location_id}) — OG1/2 exhausted; "
                                    f"density-preservation fallback"
                                )
                    if not tanks_obj:
                        warnings.append(
                            f"{week_label}: TranOG {split.batch_id} on {day}: "
                            f"no empty OG1/2 tanks ANYWHERE; arrival DROPPED "
                            f"(biomass model broken — operator action required)"
                        )
                        continue
                    N = len(tanks_obj)
                    # SIZE-CLASS PRESERVED split: keep big/small distinction
                    # so harvest can target big-class tanks first (they
                    # reach market weight earlier; small class fills the
                    # gap by growing). Requires N >= 2 with each class in
                    # >= 1 tank; Phase A enforces tanks_needed >= 4 at
                    # TranOG so each class typically gets 2+ tanks.
                    if N >= 2:
                        big_n = (N + 1) // 2
                        small_n = N - big_n
                        per_big = (split.big_class_count / big_n) if big_n else 0
                        per_small = (split.small_class_count / small_n) if small_n else 0
                        allocations = []
                        for i in range(big_n):
                            allocations.append(TankAllocation(
                                tank_id=tanks_obj[i].tank_id, count=per_big,
                                avg_wt_g=split.big_class_avg_wt_g,
                                cv_pct=split.post_cull_cv_pct, size_class="big",
                            ))
                        for i in range(small_n):
                            allocations.append(TankAllocation(
                                tank_id=tanks_obj[big_n + i].tank_id,
                                count=per_small,
                                avg_wt_g=split.small_class_avg_wt_g,
                                cv_pct=split.post_cull_cv_pct, size_class="small",
                            ))
                    else:
                        # N=1 fallback (OG capacity exhausted): mix into
                        # the single tank. Density will be flagged for
                        # grade-split later.
                        allocations = [TankAllocation(
                            tank_id=tanks_obj[0].tank_id,
                            count=split.post_cull_count,
                            avg_wt_g=split.post_cull_avg_wt_g,
                            cv_pct=split.post_cull_cv_pct, size_class="mixed",
                        )]
                    ev = TranOGEntry(
                        batch_id=split.batch_id,
                        event_date=day,
                        destinations=allocations,
                    )
                    warnings.extend(ev.apply(state))
                    tranog_events.append(ev)
                # Apply continuous biology.
                for tank in state.tanks_by_id.values():
                    if tank.is_empty:
                        continue
                    bm = batch_meta.get(tank.batch_id)
                    if bm is None:
                        continue
                    advance_tank_one_day(tank, bm, tables, day)
                day = day + timedelta(days=1)

        # (Harvest demands were already applied at the start of this week,
        # before the Transfer diff. See above.)

        # ---- Density-trigger Grade events ----
        # Split any high-density tank (density > 85% of cap). Reserve
        # TRANOG_RESERVE OG3+ tanks for upcoming TranOG arrivals (which
        # need N=4 tanks for size-class allocation). Grade only fires
        # while OG3+ free pool remains above the reserve threshold.
        DENSITY_TRIGGER_PCT = control.density_target_pct
        TRANOG_RESERVE = 4
        grade_dest_pool = sorted(
            [t for t in state.tanks_by_id.values()
             if t.is_empty and t.type == "OG"
             and t.system_id != "OG6N"],
            key=lambda t: t.tank_id,
        )
        for tank in sorted(state.tanks_by_id.values(),
                           key=lambda t: t.density_kg_m3, reverse=True):
            if tank.is_empty:
                continue
            cap = tank.max_density_kg_m3
            if cap <= 0:
                continue
            if tank.density_kg_m3 <= DENSITY_TRIGGER_PCT * cap:
                # In density order (desc); past trigger, done.
                break
            # Reserve OG3+ tanks for upcoming TranOG (N=4 minimum).
            if len(grade_dest_pool) <= TRANOG_RESERVE:
                break
            # Pick a destination, honoring INV-4 if >= 1 kg.
            high_wt = tank.avg_wt_g >= OG12_MOVE_LOCK_WT_G
            tank_sys = tank.system_id
            candidate_dest = None
            for i, d in enumerate(grade_dest_pool):
                if high_wt and tank_sys in OG12_SYSTEMS and d.system_id in OG12_SYSTEMS:
                    # Intra-OG12 split forbidden above 1 kg — skip.
                    continue
                candidate_dest = d
                grade_dest_pool.pop(i)
                break
            if candidate_dest is None:
                warnings.append(
                    f"{week_label}: tank {tank.location_id} density "
                    f"{tank.density_kg_m3:.1f} > {DENSITY_TRIGGER_PCT*100:.0f}% "
                    f"of cap {cap:.0f}; no free destination tank for grade-split"
                )
                continue
            half_count = tank.count / 2
            grade_date = ws_we[0] if ws_we is not None else _as_date(control.forecast_start)
            ev = Grade(
                batch_id=tank.batch_id,
                event_date=grade_date,
                source_tank_ids=[tank.tank_id],
                destinations=[
                    TankAllocation(
                        tank_id=tank.tank_id, count=half_count,
                        avg_wt_g=tank.avg_wt_g, cv_pct=tank.cv_pct,
                        size_class="",
                    ),
                    TankAllocation(
                        tank_id=candidate_dest.tank_id, count=half_count,
                        avg_wt_g=tank.avg_wt_g, cv_pct=tank.cv_pct,
                        size_class="",
                    ),
                ],
            )
            warnings.extend(ev.apply(state))
            grade_events.append(ev)

        # Snapshot BatchLocations for this week — iterate ALL non-empty
        # tanks (not just Phase C's planned assignments). Otherwise tanks
        # the 6N pipeline owns (OG6N main + sister) or PR-hydrated tanks
        # outside the plan would be invisible in the output.
        ws_date = ws_we[0] if ws_we is not None else _as_date(control.forecast_start)
        for tank in state.tanks_by_id.values():
            if tank.is_empty:
                continue
            locations.append(BatchLocationRow(
                week_label=week_label,
                week_start=ws_date,
                batch_id=tank.batch_id,
                tank_id=tank.tank_id,
                location_id=tank.location_id,
                system_id=tank.system_id,
                count=tank.count,
                avg_wt_g=tank.avg_wt_g,
                biomass_kg=tank.biomass_kg,
                density_kg_m3=tank.density_kg_m3,
            ))

        prev_assignment = this_assignment

    return state, tranog_events, transfer_events, harvest_events, grade_events, locations, warnings


# ============================================================
# Orchestrator
# ============================================================

def run_placement(
    initial_state: FacilityState,
    batch_meta: dict[str, BatchInput],
    biology_states_by_batch: dict[str, list[BatchWeekState]],
    harvest_demands: list[HarvestDemand],
    splits: list[SizeClassSplit],
    system_limits: SystemLimits,
    control: ControlParams,
    facility: FacilityConfig,
    tables: BiologyTables,
    migration_plan: Optional[dict] = None,
    facility_limits: Optional[FacilityLimits] = None,
) -> tuple[PlacementResult, FacilityState]:
    """End-to-end Phase A → B → C → D.

    When `migration_plan` is supplied (precalc canvas output), Phase B
    and Phase C consume it as the source of truth for per-(batch, week)
    tank assignments. Their internal greedy logic is bypassed for any
    batch-week present in the plan; otherwise the greedy fallback runs.
    """
    result = PlacementResult()
    result.load_table = phase_a_precalc(
        biology_states_by_batch, harvest_demands, splits, facility,
        control=control,
    )
    sys_assigns, b_warns = phase_b_assign_systems(
        result.load_table, initial_state, batch_meta, system_limits, control, facility,
        migration_plan=migration_plan,
    )
    result.system_assignments = sys_assigns
    result.warnings.extend(f"[B] {w}" for w in b_warns)

    tank_assigns, c_warns = phase_c_assign_tanks(
        result.system_assignments, initial_state, facility,
        biology_states_by_batch=biology_states_by_batch,
        migration_plan=migration_plan,
    )
    result.tank_assignments = tank_assigns
    result.warnings.extend(f"[C] {w}" for w in c_warns)

    final_state, tranog, transfers, harvests, grades, locs, d_warns = phase_d_emit_events(
        result.load_table, result.tank_assignments, harvest_demands,
        splits, initial_state, facility, control, batch_meta, tables,
        facility_limits=facility_limits,
    )
    result.tranog_events = tranog
    result.transfer_events = transfers
    result.harvest_events = harvests
    result.grade_events = grades
    result.batch_locations = locs
    result.warnings.extend(f"[D] {w}" for w in d_warns)
    return result, final_state


def summarize_placement(result: PlacementResult, state: FacilityState) -> dict:
    """Diagnostics summary across the 4 phases."""
    total_harvest_count = sum(ev.count for ev in result.harvest_events)
    total_harvest_kg = sum(ev.count * ev.avg_wt_g / 1000.0
                           for ev in result.harvest_events)
    total_tranog_count = sum(
        sum(d.count for d in ev.destinations) for ev in result.tranog_events
    )
    return {
        "load_rows": len(result.load_table),
        "system_assignments": len(result.system_assignments),
        "tank_assignments": len(result.tank_assignments),
        "tranog_events": len(result.tranog_events),
        "tranog_fish_placed": total_tranog_count,
        "transfer_events": len(result.transfer_events),
        "harvest_events": len(result.harvest_events),
        "harvest_count_total": total_harvest_count,
        "harvest_kg_total": total_harvest_kg,
        "location_rows": len(result.batch_locations),
        "warnings": len(result.warnings),
        "end_state_occupied_tanks": sum(
            1 for t in state.tanks_by_id.values() if not t.is_empty
        ),
        "end_state_biomass_kg": state.total_biomass(),
        "end_state_biomass_by_system": state.biomass_by_system(),
    }
