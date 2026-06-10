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

import collections
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
from .biology import (
    _fcr_model_key, _interp, realized_feed_kg_day, upper_truncated_split,
)
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
from .state import FacilityState, TankState, STAGE_STARVE
from .time_grid import forecast_week_labels, iso_week_label, week_range


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

# Arrival feed-forward smoothing window (weeks). Each scheduled TranOG arrival
# is pre-harvested over this many weeks before it lands. Empirically W=1
# (pre-draw in the move-in whose drain coincides with the arrival week) holds
# biomass tightest — wider windows draw biomass down too early/deep ahead of
# each batch and widen the swing. Kept as a tunable for non-default stocking
# cadences.
_ARRIVAL_SMOOTH_WEEKS = 1

# Damping gain on the move-in's proportional biomass-deviation correction. The
# move-in is a lagged actuator (drains ~lead weeks later), so a full deadbeat
# (1.0) correction through it oscillates; a fraction spreads the correction
# across the delay and keeps the purge pipeline running at a steady rate
# instead of bang-banging floor↔cap. Tuned empirically.
_MOVE_IN_GAIN = 0.5

# --- Realized rebalancer: split controls -------------------------------------
# The base rebalancer relocates WHOLE tanks off over-cap systems but cannot help
# a batch that is over-DENSE because it is crammed into too few tanks (e.g. one
# tank at 3.8x). SPLIT lets it fan such a batch into free eligible tanks; Phase
# D's diff machinery then evens it out as a CONSERVED partial transfer. The
# budget caps splits/week so the pass stays deterministic and transfer-cheap.
_REBALANCE_SPLIT = True
_REBALANCE_SPLIT_BUDGET = 8

# --- Realized rebalancer: variable-quantity moves ----------------------------
# The swap/split passes move WHOLE tanks or even a batch across its tanks --
# coarse. VARQTY moves a PRECISE number of fish between a batch's EXISTING tanks
# in different systems: just enough to bring an over-cap system under its
# biomass/feed cap, capped by the destination's headroom (system caps + dest
# tank density). Biomass-per-tank is not fixed, so this shaves exactly the
# excess instead of overshooting. Continuity-safe (conserved Transfer events);
# no new tanks -> tank SET is unchanged -> no diff churn, no forward-persist.
_REBALANCE_VARQTY = True
_REBALANCE_VARQTY_BUDGET = 20
# Multi-objective balancer relieves an over-dense tank to THIS fraction of its
# density cap (not to the cap): ~10%/week growth then keeps it under cap through
# the week instead of pushing it straight back over (the carried/grown-over
# cycle). Applied to both the source target and the destination fill.
_BALANCE_TARGET_FRAC = 0.88
# Trigger the balancer on tanks already at this fraction of cap, not only those
# already over it: ~10%/week growth means a tank near ~0.92 of cap will cross by
# week-end, so relieving it now (into empty capacity like OG6S) pre-empts the
# spike instead of chasing it a week late (the W27-34 OG6S-empty case).
_BALANCE_TRIGGER_FRAC = 0.92
# Fill a destination SYSTEM only to this fraction of its feed/biomass cap (not
# the 1.05 buffer): same growth-margin logic as density, so a move can't push a
# destination system over its feed/biomass cap once the week's growth lands.
_BALANCE_SYS_FILL = 0.90
# Fill a variable-quantity move's destination only to this fraction of cap (NOT
# cap*buf), leaving headroom for the week's growth so a move can't relocate a
# violation into the destination.
_VARQTY_DST_FILL = 0.95


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
    stage: str = ""   # tank stage; "STARVE" = in-place purge (no feed/growth)


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
            # (big + small) — one tank per class is the working minimum, so 2.
            # The big-first harvest pattern still holds (drain big → small
            # migrates in as it grows) and the realized split rebalancer fans
            # batches out reactively. Control R28 raises the floor if set.
            # Lowered 4 -> 2 to free transition tanks (maximizes space use).
            if is_tranog:
                tanks_needed = max(tanks_needed, max(2, tranog_default_tanks))
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
    resting_pair: Optional[tuple[int, int]] = None,
    refill: bool = True,
) -> Optional[tuple[int, int]]:
    """Run one week of the 6N purge pipeline (3-pair fallow rotation).

    Operational rule: fish are TRANSFERRED into a pair mid-week (Wed) and the
    harvested pair is EMPTIED end-of-week (Fri) — so the move-in CANNOT go into
    the pair harvested this week. It fills the RESTING pair (emptied at a prior
    harvest, fallow since); the pair harvested this week becomes next week's
    resting pair. Three pairs therefore cycle: 2 purging + 1 fallow.

    Pops the front (lowest-count) pair and harvests it, fills the resting pair
    from FIFO production, pushes the filled pair to the back. Returns the new
    resting pair (the one just harvested) for the caller to carry forward.
    """
    if not pair_queue:
        warnings.append(f"{week_label}: 6N purge queue empty — no harvest this week")
        return resting_pair

    harvest_pair = pair_queue.pop(0)
    # Wed-fill / Fri-harvest: the move-in fills the RESTING pair, never the pair
    # harvested this week. Degenerate fallback (no resting pair — e.g. all pairs
    # stocked at start, no fallow slot) refills the harvested pair as before.
    fill_pair = resting_pair if resting_pair is not None else harvest_pair
    new_resting = harvest_pair if resting_pair is not None else None

    # 1. Harvest the harvested pair's contents (both tanks if occupied).
    pair_drain_count = 0.0
    for tank_id in harvest_pair:
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

    # Wind-down (transition): harvest the front pair but do NOT restock — 6N
    # drains over the rotation while production harvest takes over via in-place
    # starvation. The resting pair stays empty; rotation continues so all pairs
    # drain in turn.
    if not refill:
        pair_queue.append(fill_pair)
        return new_resting

    # 2. Pick FIFO move-in source batches (cascade list).
    move_in_batches = _pick_fifo_move_in_batches(state, batch_meta, control)
    if not move_in_batches:
        # Last-resort: graded move-in (DESIGN §5a) — peel the
        # above-threshold tail from a tank whose average is below
        # threshold but has a meaningful upper portion.
        moved = _try_graded_move_in(
            state, batch_meta, control, week_label, week_start_date,
            fill_pair, transfer_events, warnings,
        )
        if moved <= 0:
            warnings.append(
                f"{week_label}: 6N harvested {harvest_pair} but no production "
                "batch above min_harvest_weight to fill resting pair "
                f"{fill_pair} (stays in rotation, empty next harvest)"
            )
        pair_queue.append(fill_pair)
        return new_resting

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
        pair_queue.append(fill_pair)
        return new_resting

    # 4. Pull from FIFO batches in cascade. When the oldest batch's
    #    production tanks can't fill the target, fall through to the next
    #    FIFO batch. This keeps move-in size >= min_h whenever total
    #    mature inventory >= min_h, and the resulting pair drain 2 weeks
    #    later also >= min_h (minus mortality).
    main_tank_id = fill_pair[0]  # prefer main (61/63/65) over sister (67/69/71)
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
                dest_tank_id = fill_pair[1]  # sister tank for second+ batch
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
            f"{week_label}: 6N resting pair {fill_pair} move-in failed (no fish "
            f"moved); pair will be empty next harvest"
        )
    elif count_moved < min_h:
        warnings.append(
            f"{week_label}: 6N resting pair {fill_pair} move-in short of min_hv "
            f"({count_moved:,.0f} < {min_h:,.0f}); insufficient mature inventory"
        )

    # The refilled (resting) pair rejoins the rotation; the harvested pair is now
    # next week's resting pair.
    pair_queue.append(fill_pair)
    return new_resting


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
             if t.batch_id == batch_id and not t.is_empty
             and t.stage != STAGE_STARVE]   # STARVE tanks are purge-pipeline-owned
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


def _tank_to_system_of(tid, og_tanks_by_system):
    for s, ids in og_tanks_by_system.items():
        if tid in ids:
            return s
    return None


def _persist_system_move(b, src, y, wl, sorted_weeks, week_index, ta_index,
                         week_tank_owner):
    """Replace tank src -> y in batch b's tank_assignments from week wl forward,
    while b still holds src and y is free in the canvas plan (so the move
    persists as ONE transfer, not one per week)."""
    start = week_index.get(wl)
    if start is None:
        return
    for w in sorted_weeks[start:]:
        ta = ta_index.get((b, w))
        if ta is None or src not in ta.tank_ids:
            break
        owner_w = week_tank_owner.setdefault(w, {})
        if y in owner_w and owner_w[y] != b:
            break
        ta.tank_ids = sorted(y if x == src else x for x in ta.tank_ids)
        owner_w.pop(src, None)
        owner_w[y] = b


def _persist_system_add(b, y, wl, sorted_weeks, week_index, ta_index,
                        week_tank_owner):
    """Add free tank y to batch b's tank_assignments from week wl forward.

    Unlike `_persist_system_move`, b KEEPS its existing tanks — this is a SPLIT,
    not a swap. b spreads onto one more tank; Phase D's diff evens the fish
    across the larger set (a conserved partial transfer). Stops extending the
    split as soon as y is claimed by another batch downstream, or b's plan ends.
    """
    start = week_index.get(wl)
    if start is None:
        return
    for w in sorted_weeks[start:]:
        ta = ta_index.get((b, w))
        if ta is None:
            break
        owner_w = week_tank_owner.setdefault(w, {})
        if y in owner_w and owner_w[y] != b:
            break
        if y not in ta.tank_ids:
            ta.tank_ids = sorted(ta.tank_ids + [y])
        owner_w[y] = b


def _rebalance_systems_realized(
    state, this_assignment, ta_index, week_tank_owner, wl, sorted_weeks,
    week_index, cap_lookup, buf, batch_meta, tables,
    og_systems, growout_systems, og_tanks_by_system, split_budget=None,
):
    """Move batches off systems over their REALIZED biomass/feed cap onto
    eligible systems with headroom.

    Detection uses the realized end-of-last-week `state` (actual load, not the
    canvas projection). The move is an edit of `this_assignment` (swap one
    over-cap-system tank for an empty tank in the target) plus a forward edit
    of `tank_assignments` (via `ta_index`/`week_tank_owner`) so it persists —
    Phase D's diff machinery then emits the CONSERVED Transfer AND evens the
    batch across its new tank set (continuity-safe + de-concentrating). Targets
    keep the destination under BOTH caps; over-cap systems vacate their OLDEST
    batch first. Returns moves made.
    """
    planned: dict[str, list[int]] = {}
    for tid, b in this_assignment.items():
        planned.setdefault(b, []).append(tid)
    sb = collections.defaultdict(float)
    sf = collections.defaultdict(float)
    occ = collections.defaultdict(int)
    bbio: dict[str, float] = collections.defaultdict(float)
    bfeed: dict[str, float] = collections.defaultdict(float)
    bwt: dict[str, float] = {}
    for t in state.tanks_by_id.values():
        if t.is_empty or t.system_id not in og_systems:
            continue
        feed = realized_feed_kg_day(
            t.avg_wt_g, t.biomass_kg, batch_meta.get(t.batch_id), tables)
        sb[t.system_id] += t.biomass_kg
        sf[t.system_id] += feed
        occ[t.system_id] += 1
        bbio[t.batch_id] += t.biomass_kg
        bfeed[t.batch_id] += feed
        bwt[t.batch_id] = max(bwt.get(t.batch_id, 0.0), t.avg_wt_g)

    moves = 0
    for _guard in range(40):
        over = []
        for s in og_systems:
            bc, fc = cap_lookup(wl, s)
            if (bc and sb[s] > bc * buf) or (fc and sf[s] > fc * buf):
                over.append((max((sb[s] / bc) if bc else 0.0,
                                 (sf[s] / fc) if fc else 0.0), s))
        if not over:
            break
        over.sort(key=lambda x: (-x[0], x[1]))
        big_s = over[0][1]
        s_batches = sorted(
            {b for tid, b in this_assignment.items()
             if _tank_to_system_of(tid, og_tanks_by_system) == big_s},
            key=lambda b: (
                (batch_meta[b].input_date if b in batch_meta
                 and batch_meta[b].input_date else date.max), b),
        )
        moved = False
        for b in s_batches:
            ntanks = len(planned.get(b, []))
            if ntanks == 0:
                continue
            pb = bbio.get(b, 0.0) / ntanks
            pf = bfeed.get(b, 0.0) / ntanks
            elig = (growout_systems if bwt.get(b, 0.0) >= OG12_MOVE_LOCK_WT_G
                    else (og_systems - growout_systems))
            best = None
            for tgt in sorted(elig):
                if tgt == big_s or tgt not in og_systems:
                    continue
                if occ[tgt] >= len(og_tanks_by_system.get(tgt, [])):
                    continue
                bc, fc = cap_lookup(wl, tgt)
                if bc and sb[tgt] + pb > bc * buf:
                    continue
                if fc and sf[tgt] + pf > fc * buf:
                    continue
                head = min((bc * buf - sb[tgt] - pb) / bc if bc else 9.0,
                           (fc * buf - sf[tgt] - pf) / fc if fc else 9.0)
                if best is None or head > best[0]:
                    best = (head, tgt)
            if best is None:
                continue
            tgt = best[1]
            src = min((tid for tid in planned[b]
                       if _tank_to_system_of(tid, og_tanks_by_system) == big_s),
                      default=None)
            if src is None:
                continue
            y = None
            for cand in og_tanks_by_system.get(tgt, []):
                tk = state.tanks_by_id.get(cand)
                if tk is not None and tk.is_empty and cand not in this_assignment:
                    y = cand
                    break
            if y is None:
                continue
            del this_assignment[src]
            this_assignment[y] = b
            planned[b] = [y if x == src else x for x in planned[b]]
            _persist_system_move(b, src, y, wl, sorted_weeks, week_index,
                                 ta_index, week_tank_owner)
            sb[big_s] -= pb; sf[big_s] -= pf; occ[big_s] -= 1
            sb[tgt] += pb; sf[tgt] += pf; occ[tgt] += 1
            moves += 1
            moved = True
            break
        if not moved:
            break

    # --- Density-driven SPLIT pass ------------------------------------------
    # The swap pass above relocates whole tanks but can't de-concentrate a batch
    # that is over-dense because its fish are crammed into too few tanks (the
    # B49 "one tank at 3.8x for 24 weeks" failure). Here we SPLIT: hand the most
    # over-dense batch a free eligible tank and let Phase D even it out (conserved
    # partial transfer). Targets must have a full per-tank share of headroom under
    # BOTH caps, so a split never pushes a destination over its limits. We add the
    # target's load but don't credit the source's de-concentration — conservative,
    # so we may under-split but never over-fill a destination.
    _split_budget = (split_budget if split_budget is not None
                     else _REBALANCE_SPLIT_BUDGET)
    if _REBALANCE_SPLIT and _split_budget > 0:
        bcap: dict[str, float] = collections.defaultdict(float)
        for tid, bb in this_assignment.items():
            tk = state.tanks_by_id.get(tid)
            if tk is not None and tk.max_density_kg_m3 > 0:
                bcap[bb] += tk.max_biomass_kg
        for _g2 in range(_split_budget):
            cand = None
            for bb, bio in bbio.items():
                cp = bcap.get(bb, 0.0)
                if cp <= 0:
                    continue
                ratio = bio / cp
                if ratio > buf and (cand is None or ratio > cand[0]):
                    cand = (ratio, bb)
            if cand is None:
                break
            bb = cand[1]
            elig = (growout_systems if bwt.get(bb, 0.0) >= OG12_MOVE_LOCK_WT_G
                    else (og_systems - growout_systems))
            # Share reflects the FINAL spread, not n+1: a batch crammed in one
            # tank needs to fan across ceil(biomass / tank-capacity) tanks. Sizing
            # the share at n+1 would charge a target half the batch on the first
            # split (always over cap → never fires). avg_cap = the batch's
            # representative tank density-ceiling.
            have = max(1, len(planned.get(bb, [])))
            avg_cap = bcap.get(bb, 0.0) / have
            target_tanks = max(have + 1,
                               int(-(-bbio.get(bb, 0.0) // avg_cap)) if avg_cap else have + 1)
            share_b = bbio.get(bb, 0.0) / target_tanks
            share_f = bfeed.get(bb, 0.0) / target_tanks
            chosen = None
            for tgt in sorted(elig):
                if tgt not in og_systems:
                    continue
                if occ[tgt] >= len(og_tanks_by_system.get(tgt, [])):
                    continue
                bc, fc = cap_lookup(wl, tgt)
                if bc and sb[tgt] + share_b > bc * buf:
                    continue
                if fc and sf[tgt] + share_f > fc * buf:
                    continue
                for candy in og_tanks_by_system.get(tgt, []):
                    tk = state.tanks_by_id.get(candy)
                    if tk is not None and tk.is_empty and candy not in this_assignment:
                        chosen = (tgt, candy, tk)
                        break
                if chosen:
                    break
            if chosen is None:
                break
            tgt, y, tky = chosen
            this_assignment[y] = bb
            planned.setdefault(bb, []).append(y)
            _persist_system_add(bb, y, wl, sorted_weeks, week_index,
                                ta_index, week_tank_owner)
            bcap[bb] += tky.max_biomass_kg
            sb[tgt] += share_b
            sf[tgt] += share_f
            occ[tgt] += 1
            moves += 1

    return moves


def _variable_quantity_rebalance(
    state, wl, event_date, transfer_events, warnings,
    cap_lookup, buf, batch_meta, tables, og_systems, og_tanks_by_system,
    budget,
):
    """Shave over-cap systems by moving a PRECISE count of fish between a
    batch's EXISTING tanks in different systems.

    For each over-cap system (biomass or feed), pick a batch that also holds a
    tank in an under-cap system and transfer just enough fish from its over-cap
    tank to its under-cap tank to bring the system under cap — bounded by the
    destination system's headroom and the destination tank's density ceiling.
    Conserved Transfer events (continuity-safe); the tank SET never changes, so
    no diff churn and no forward-persist needed. Returns moves made.
    """
    sys_of = {tid: s for s, ids in og_tanks_by_system.items() for tid in ids}

    def sys_load(sysid):
        bio = feed = 0.0
        for tid in og_tanks_by_system.get(sysid, []):
            t = state.tanks_by_id.get(tid)
            if t is not None and not t.is_empty:
                bio += t.biomass_kg
                feed += realized_feed_kg_day(
                    t.avg_wt_g, t.biomass_kg, batch_meta.get(t.batch_id), tables)
        return bio, feed

    moves = 0
    stuck: set = set()
    for _ in range(budget):
        over = []
        for s in og_systems:
            if s in stuck:
                continue
            bc, fc = cap_lookup(wl, s)
            bio, feed = sys_load(s)
            r = max((bio / (bc * buf)) if bc else 0.0,
                    (feed / (fc * buf)) if fc else 0.0)
            if r > 1.0:
                over.append((r, s, bio, feed, bc, fc))
        if not over:
            break
        over.sort(key=lambda x: (-x[0], x[1]))
        _, S, sbio, sfeed, bc, fc = over[0]
        need_bio = (sbio - bc * buf) if bc else 0.0
        feed_over = (sfeed - fc * buf) if fc else 0.0

        # Source tanks: occupied tanks of S, biggest batch contribution first.
        src_by_batch: dict = collections.defaultdict(list)
        for tid in og_tanks_by_system.get(S, []):
            t = state.tanks_by_id.get(tid)
            if t is not None and not t.is_empty:
                src_by_batch[t.batch_id].append(t)

        best = None
        for bid in sorted(src_by_batch):
            src = max(src_by_batch[bid], key=lambda t: t.biomass_kg)
            if src.biomass_kg <= 1.0 or src.avg_wt_g <= 0:
                continue
            src_feed = realized_feed_kg_day(
                src.avg_wt_g, src.biomass_kg, batch_meta.get(bid), tables)
            intensity = src_feed / src.biomass_kg if src.biomass_kg > 0 else 0.0
            # Destinations: same batch, in an under-cap system with headroom.
            # Fill only to _VARQTY_DST_FILL of cap (NOT cap*buf) so a move never
            # pushes the destination to the violation line — leaving margin for
            # the week's growth. Sources are >cap*buf, dests <fill*cap, so each
            # move strictly narrows the spread instead of relocating violations.
            for tid2 in sorted(t.tank_id for t in state.tanks_by_id.values()
                               if not t.is_empty and t.batch_id == bid):
                t2 = state.tanks_by_id.get(tid2)
                T = sys_of.get(tid2)
                if T is None or T == S:
                    continue
                tbc, tfc = cap_lookup(wl, T)
                Tbio, Tfeed = sys_load(T)
                bio_head = (tbc * _VARQTY_DST_FILL - Tbio) if tbc else 9e18
                feed_head = (tfc * _VARQTY_DST_FILL - Tfeed) if tfc else 9e18
                if bio_head <= 0 or feed_head <= 0:
                    continue
                dens_head = ((t2.max_biomass_kg * 0.98 - t2.biomass_kg)
                             if t2.max_density_kg_m3 > 0 else 9e18)
                if dens_head <= 0:
                    continue
                move_bio = need_bio
                if feed_over > 0 and intensity > 0:
                    move_bio = max(move_bio, feed_over / intensity)
                feed_head_bio = (feed_head / intensity) if intensity > 0 else 9e18
                move_bio = min(move_bio, bio_head, feed_head_bio, dens_head,
                               src.biomass_kg * 0.9)
                if move_bio <= 1.0:
                    continue
                if best is None or move_bio > best[0]:
                    best = (move_bio, bid, src, t2)
        if best is None:
            stuck.add(S)
            continue
        move_bio, bid, src, dst = best
        move_count = move_bio / (src.avg_wt_g / 1000.0)
        if move_count < 1.0:
            stuck.add(S)
            continue
        before = src.biomass_kg
        ev = Transfer(
            batch_id=bid, event_date=event_date, source_tank_id=src.tank_id,
            destinations=[TankAllocation(
                tank_id=dst.tank_id, count=move_count,
                avg_wt_g=src.avg_wt_g, cv_pct=src.cv_pct,
            )],
            leaves_source_empty=False,
        )
        warnings.extend(ev.apply(state))
        transfer_events.append(ev)
        if src.biomass_kg >= before - 1.0:   # transfer refused (INV-4 etc.)
            stuck.add(S)
            continue
        moves += 1
    return moves


def _balance_loads(
    state, wl, event_date, transfer_events, warnings,
    ta_index, week_tank_owner, sorted_weeks, week_index,
    cap_lookup, buf, batch_meta, tables,
    og_systems, growout_systems, og_tanks_by_system, budget,
):
    """Multi-objective balancer: cut out-of-bounds across per-tank DENSITY,
    per-system FEED, and per-system BIOMASS *together*.

    For each over-dense tank (worst first), move just enough surplus fish
    (conserved Transfer) into the best destination — an under-cap tank of the
    SAME batch, or an empty eligible tank — chosen by headroom in ALL THREE
    dimensions (system biomass, system feed, destination density). The move is
    capped by every dimension's headroom, so relieving a hot tank can never push
    a destination over its feed/biomass/density cap (the trap the naive density
    split fell into). Eligibility honours the 1 kg lock (>=1 kg → growout only;
    <1 kg → any OG); Transfer.apply is the final INV gate. New tanks are
    forward-persisted so the set stays stable (no diff churn). Continuity-safe.
    """
    sys_of = {tid: s for s, ids in og_tanks_by_system.items() for tid in ids}

    def loads():
        sb = collections.defaultdict(float)
        sf = collections.defaultdict(float)
        for s, ids in og_tanks_by_system.items():
            for tid in ids:
                t = state.tanks_by_id.get(tid)
                if t is not None and not t.is_empty:
                    sb[s] += t.biomass_kg   # STARVE biomass still counts to caps
                    if t.stage != STAGE_STARVE:   # but STARVE fish eat nothing
                        sf[s] += realized_feed_kg_day(
                            t.avg_wt_g, t.biomass_kg, batch_meta.get(t.batch_id), tables)
        return sb, sf

    moves = 0
    stuck: set = set()
    for _ in range(budget):
        sb, sf = loads()
        worst = None
        for s, ids in og_tanks_by_system.items():
            for tid in ids:
                if tid in stuck:
                    continue
                t = state.tanks_by_id.get(tid)
                if (t is None or t.is_empty or t.max_density_kg_m3 <= 0
                        or t.stage == STAGE_STARVE):   # don't relieve purge tanks
                    continue
                ratio = t.density_kg_m3 / t.max_density_kg_m3
                if ratio > _BALANCE_TRIGGER_FRAC and (worst is None or ratio > worst[0]):
                    worst = (ratio, t)
        if worst is None:
            break
        src = worst[1]
        b = src.batch_id
        surplus_kg = src.biomass_kg - src.max_biomass_kg * _BALANCE_TARGET_FRAC
        if surplus_kg <= 1.0 or src.avg_wt_g <= 0:
            stuck.add(src.tank_id)
            continue
        growout = src.avg_wt_g >= OG12_MOVE_LOCK_WT_G
        intensity = (realized_feed_kg_day(
            src.avg_wt_g, src.biomass_kg, batch_meta.get(b), tables)
            / src.biomass_kg) if src.biomass_kg > 0 else 0.0
        # Candidate destinations with per-dimension headroom.
        cands = []
        for s2, ids in og_tanks_by_system.items():
            eligible = (s2 in growout_systems) if growout else (s2 in og_systems)
            if not eligible:
                continue
            tbc, tfc = cap_lookup(wl, s2)
            bio_head = (tbc * _BALANCE_SYS_FILL - sb[s2]) if tbc else 1e18
            feed_head = (tfc * _BALANCE_SYS_FILL - sf[s2]) if tfc else 1e18
            if bio_head <= 0 or feed_head <= 0:
                continue
            for tid2 in ids:
                if tid2 == src.tank_id:
                    continue
                t2 = state.tanks_by_id.get(tid2)
                if t2 is None or t2.stage == STAGE_STARVE:   # don't fill purge tanks
                    continue
                if (not t2.is_empty) and t2.batch_id == b:
                    dens_head = t2.max_biomass_kg * _BALANCE_TARGET_FRAC - t2.biomass_kg
                    is_new = False
                elif t2.is_empty:
                    # Claim any REALIZED-empty eligible tank, even one the canvas
                    # plan nominally reserves for another batch — those
                    # reservations frequently diverge from realized state and
                    # were stranding 174 fixable over-dense tanks beside empty
                    # capacity. _persist_system_add's downstream guard backs off
                    # if the reserving batch actually materialises later.
                    dens_head = t2.max_biomass_kg * _BALANCE_TARGET_FRAC
                    is_new = True
                else:
                    continue
                if dens_head <= 0:
                    continue
                score = min(bio_head, feed_head, dens_head)
                cands.append((score, is_new, t2, s2, tbc, tfc, bio_head, feed_head, dens_head))
        if not cands:
            stuck.add(src.tank_id)
            continue
        # Most headroom first; prefer reusing an existing tank over a new one.
        cands.sort(key=lambda c: (-c[0], c[1]))
        _sc, is_new, dst, s2, tbc, tfc, bio_head, feed_head, dens_head = cands[0]
        move_kg = min(surplus_kg, dens_head, bio_head, src.biomass_kg * 0.95)
        if tfc and intensity > 0:
            move_kg = min(move_kg, feed_head / intensity)
        if move_kg <= 1.0:
            stuck.add(src.tank_id)
            continue
        move_count = move_kg / (src.avg_wt_g / 1000.0)
        before = src.biomass_kg
        ev = Transfer(
            batch_id=b, event_date=event_date, source_tank_id=src.tank_id,
            destinations=[TankAllocation(
                tank_id=dst.tank_id, count=move_count,
                avg_wt_g=src.avg_wt_g, cv_pct=src.cv_pct,
            )],
            leaves_source_empty=False,
        )
        warnings.extend(ev.apply(state))
        transfer_events.append(ev)
        if src.biomass_kg >= before - 1.0:      # transfer refused (INV gate)
            stuck.add(src.tank_id)
            continue
        if is_new:
            _persist_system_add(b, dst.tank_id, wl, sorted_weeks, week_index,
                                ta_index, week_tank_owner)
        moves += 1
    return moves


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
    system_limits: Optional[SystemLimits] = None,
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

    # TranOG arrival schedule (kg of biomass entering OG per ISO week). Each
    # split's post-cull population lands in the facility at its tran_og_date —
    # a KNOWN disturbance the closed-loop harvest controller feeds forward so
    # it can pre-draw biomass down before the batch arrives (see
    # caps.predictive_move_in_count). Approximate; the predictive feedback
    # re-corrects against realized state each week.
    arrivals_by_week: dict[str, float] = {}
    for s in splits:
        if s.tran_og_date is None or s.post_cull_count <= 0:
            continue
        wk = iso_week_label(_as_date(s.tran_og_date))
        arrivals_by_week[wk] = arrivals_by_week.get(wk, 0.0) + (
            s.post_cull_count * s.post_cull_avg_wt_g / 1000.0)

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

    # ---- System rebalancer setup (realized; runs per week if caps given) ----
    _rebal_on = system_limits is not None
    og_systems_set: set[str] = set()
    growout_systems: set[str] = set()
    og_tanks_by_system_r: dict[str, list[int]] = {}
    for tcfg in facility.tanks:
        if tcfg.type == "OG" and tcfg.system_id != "OG6N":
            og_systems_set.add(tcfg.system_id)
            og_tanks_by_system_r.setdefault(tcfg.system_id, []).append(tcfg.tank_id)
            if tcfg.system_id not in OG12_SYSTEMS:
                growout_systems.add(tcfg.system_id)
    for _s in og_tanks_by_system_r:
        og_tanks_by_system_r[_s].sort()
    ta_index: dict[tuple[str, str], TankAssignment] = {
        (a.batch_id, a.week_label): a for a in tank_assignments
    }
    week_tank_owner: dict[str, dict[int, str]] = {}
    for a in tank_assignments:
        d = week_tank_owner.setdefault(a.week_label, {})
        for tid in a.tank_ids:
            d[tid] = a.batch_id
    week_index_r = {wl: i for i, wl in enumerate(sorted_weeks)}
    _rebal_buf = 1.0 + (control.global_buffer_pct or 0.0)
    _cap_weeks: dict[tuple[str, str], list] = {}
    if system_limits is not None:
        for (wk, sysid, metric), val in system_limits.caps.items():
            _cap_weeks.setdefault((sysid, metric), []).append((wk, val))
        for _k in _cap_weeks:
            _cap_weeks[_k].sort()

    def _sys_cap(wl_, sysid):
        def cf(metric):
            lst = _cap_weeks.get((sysid, metric))
            if not lst:
                return None
            best = lst[0][1]
            for w, v in lst:
                if w <= wl_:
                    best = v
                else:
                    break
            return best
        return (cf(METRIC_BIOMASS), cf(METRIC_FEED_DAY))

    # 6N purge pipeline queue (only meaningful while in purge mode; ignored
    # if the forecast starts in production mode). Pairs are ordered with
    # the lowest-count pair first so W1 harvests it (user H10).
    try:
        sixn_pair_queue: list[tuple[int, int]] = list(initial_purge_pair_queue(state))
    except RuntimeError as e:
        warnings.append(str(e))
        sixn_pair_queue = []
    # 3-pair fallow rotation: the resting (fallow) pair is the one NOT stocked at
    # forecast start — it takes the first move-in while the stocked pairs purge.
    # The Wed-fill/Fri-harvest rule needs exactly one fallow pair (2 purge + 1
    # rest). If none is empty the handler degrades to refill-in-place.
    _stocked_pairs = set(sixn_pair_queue)
    _empty_pairs = [p for p in SIXN_PAIRS if p not in _stocked_pairs]
    sixn_resting_pair: Optional[tuple[int, int]] = (
        _empty_pairs[0] if _empty_pairs else None)

    # 6N phase machine (operator spec). sixn_production_start is the STOP-REFILL
    # (transition) date, not "production begins". Phases advance:
    #   purge      — round-robin depuration (refilling 6N)
    #   winddown   — transition date reached: stop refills, 6N drains in rotation
    #   empty      — 6N drained: hold 61/63/65 empty for sixn_transition_weeks,
    #                drop 67/69/71 from availability
    #   production — empty window elapsed: 61/63/65 are normal growout tanks
    _psd = _as_date(control.sixn_production_start) if control.sixn_production_start else None
    if control.sixn_growth or (_psd is not None and _as_date(control.forecast_start) >= _psd):
        sixn_phase = "production"
    else:
        sixn_phase = "purge"
    sixn_empty_weeks = 0

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

        # Advance the 6N phase machine for this week (entry advancement; the
        # empty/production advancement happens AFTER the week's 6N harvest below,
        # once the drain state is known). purge/winddown still run the 6N
        # pipeline (winddown stops refills → drains); empty/production do not.
        if (sixn_phase == "purge" and _psd is not None and not control.sixn_growth
                and week_start_date >= _psd):
            sixn_phase = "winddown"
        purge_this_week = sixn_phase in ("purge", "winddown")
        sixn_refill = (sixn_phase == "purge")

        # In production, OG6N's main tanks (61/63/65) become a normal growout
        # system (sisters 67/69/71 stay unavailable). Augment the eligible system
        # sets so the rebalancer + balancer route and relieve density into OG6N.
        if sixn_phase == "production":
            _og_sys = og_systems_set | {"OG6N"}
            _grow_sys = growout_systems | {"OG6N"}
            _og_tanks = dict(og_tanks_by_system_r)
            _og_tanks["OG6N"] = [t for t in (61, 63, 65) if t in state.tanks_by_id]
        else:
            _og_sys, _grow_sys, _og_tanks = (
                og_systems_set, growout_systems, og_tanks_by_system_r)

        # System rebalancing: from the REALIZED end-of-last-week state, move
        # batches off systems over their biomass/feed cap onto eligible systems
        # with headroom — editing this_assignment + tank_assignments forward so
        # the diff machinery below conserves the fish (continuity-safe).
        if _rebal_on:
            _rebalance_systems_realized(
                state, this_assignment, ta_index, week_tank_owner, week_label,
                sorted_weeks, week_index_r, _sys_cap, _rebal_buf, batch_meta,
                tables, _og_sys, _grow_sys, _og_tanks,
                split_budget=int(getattr(control, "rebalance_split_budget",
                                         _REBALANCE_SPLIT_BUDGET) or 0),
            )

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
        fac_bio, fac_growth_kg, _fac_feed_kg_day, oldest_wt = (
            _realized_facility_metrics(
                state, batch_meta, tables, control.min_harvest_weight_g)
        )
        bio_cap = resolve_facility_cap(METRIC_BIOMASS, week_label, facility_limits, control)
        max_hv = resolve_facility_cap(METRIC_MAX_HARVEST, week_label, facility_limits, control)
        min_hv = resolve_facility_cap(METRIC_MIN_HARVEST, week_label, facility_limits, control)
        weekly_max = max_hv if max_hv else float("inf")
        setpoint = bio_cap * _SETPOINT_FRACTION if bio_cap is not None else None

        lead = max(1, len(sixn_pair_queue))

        # Feed-forward known TranOG arrivals, AMORTISED so each arrival is
        # pre-drawn gradually over the `_ARRIVAL_SMOOTH_WEEKS` harvest weeks
        # ending at the batch's arrival, rather than all at once (which would
        # saturate the weekly harvest cap and leave a residual spike). This
        # move-in drives harvest at week t+lead, so it carries that week's
        # share = (arrivals over [t+lead, t+lead+W)) / W. A single arrival of
        # size X thus contributes X/W to each of the W move-ins whose drains
        # precede it — summing to exactly X, no double-count.
        cur_idx = sorted_weeks.index(week_label)
        drain_idx = cur_idx + lead
        W = max(1, _ARRIVAL_SMOOTH_WEEKS)
        arrivals_kg = sum(
            arrivals_by_week.get(sorted_weeks[drain_idx + j], 0.0)
            for j in range(W) if drain_idx + j < len(sorted_weeks)
        ) / W

        move_in_target = predictive_move_in_count(
            total_biomass=fac_bio,
            growth_kg_week=fac_growth_kg,
            setpoint=setpoint,
            harvest_avg_wt_g=oldest_wt,
            weekly_min=min_hv or 0.0,
            weekly_max=weekly_max,
            gain=_MOVE_IN_GAIN,
            arrivals_kg=arrivals_kg,
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

        # In-place purge length + this week's harvest target (shared by the
        # winddown pre-stage and the production harvest below).
        purge_days = int(getattr(control, "starvation_period_days", 0) or 0)
        weekly_target = min(weekly_max, max(min_hv or 0.0, harvest_target or 0.0))

        # Harvest engine — 6N purge pipeline when in purge mode, else Layer-2 FIFO.
        if purge_this_week:
            sixn_resting_pair = _run_sixn_purge_week(
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
                resting_pair=sixn_resting_pair,
                refill=sixn_refill,
            )
            # PRE-STAGE the in-place purge during WINDDOWN (sixn_refill is False
            # only in winddown). Depuration takes purge_days, so if the first
            # STARVE tank only STARTS when production begins it isn't harvest-ready
            # for ~purge_days — leaving a harvest GAP exactly when the 6N purge
            # drain stops (the 0-harvest weeks). Here we enter mature tanks into
            # STARVE while the 6N drain still supplies THIS week's harvest, so they
            # finish purging by the time production begins and are ready to harvest
            # immediately — no break in harvest. We only ENTER + AGE them here
            # (don't harvest yet); the 6N drain covers winddown harvest. Bounded:
            # keep ~one weekly_target of fish in the pipeline, no more.
            if not sixn_refill and purge_days > 0 and weekly_target > 0:
                # Depuration takes ceil(purge_days/7) weekly steps to complete, so
                # the pipeline must hold that many staggered cohorts of ~target to
                # deliver target EVERY week from production's first week. Fill up to
                # that depth, entering ~target/week (the steady production rate).
                _stage_cap = math.ceil(purge_days / 7.0) * weekly_target
                _in_pipe = sum(t.count for t in state.tanks_by_id.values()
                               if t.stage == STAGE_STARVE and not t.is_empty)
                _entered = 0.0
                for bid in _pick_fifo_move_in_batches(state, batch_meta, control):
                    if _in_pipe >= _stage_cap or _entered >= weekly_target:
                        break
                    src_tanks = sorted(
                        [t for t in state.tanks_by_id.values()
                         if t.batch_id == bid and not t.is_empty and t.stage == "SW"
                         and t.avg_wt_g >= control.min_harvest_weight_g],
                        key=lambda t: (-t.avg_wt_g, t.tank_id))
                    for src in src_tanks:
                        if _in_pipe >= _stage_cap or _entered >= weekly_target:
                            break
                        src.stage = STAGE_STARVE
                        src.starvation_days_remaining = purge_days
                        _in_pipe += src.count
                        _entered += src.count
                        warnings.append(
                            f"{week_label}: PRE-STAGE in-place purge {src.location_id} "
                            f"(batch {src.batch_id}, {src.count:.0f} fish) — readying "
                            f"the 6N->production harvest handoff (no gap)")
                # Age the pre-staged tanks so they complete purge by production.
                for t in state.tanks_by_id.values():
                    if t.stage == STAGE_STARVE and not t.is_empty:
                        t.starvation_days_remaining -= 7
            # Wind-down drain check: once all 6N tanks are empty, enter the
            # fallow empty window.
            if sixn_phase == "winddown":
                _sixn_ids = SIXN_MAIN_TANKS | SIXN_SISTER_TANKS
                if all(state.tanks_by_id[t].is_empty for t in _sixn_ids
                       if t in state.tanks_by_id):
                    # sixn_transition_weeks=0 → no fallow window: 6N goes straight
                    # from drained to production (no empty-capacity dip).
                    if (control.sixn_transition_weeks or 0) <= 0:
                        sixn_phase = "production"
                    else:
                        sixn_phase = "empty"
                        sixn_empty_weeks = 0
        else:
            # empty / production. Count the fallow empty window, then flip to
            # full production once sixn_transition_weeks have elapsed.
            if sixn_phase == "empty":
                sixn_empty_weeks += 1
                if sixn_empty_weeks >= (control.sixn_transition_weeks or 0):
                    sixn_phase = "production"
            # Production harvest = IN-PLACE PURGE pipeline. A tank selected for
            # harvest enters STARVE (no feed, no growth — biomass holds, weight
            # frozen) and is harvested starvation_period_days later AT ITS ENTRY
            # weight, so facility biomass, feed, and the harvest avg weight are
            # correct (vs harvesting after ~2 more weeks of growth). STARVE tanks
            # are pipeline-owned: the rebalancing passes below skip them so their
            # fish aren't scrambled between purging and growing tanks.
            # purge_days=0 → harvest immediately (no in-place purge configured).
            target = weekly_target
            if sixn_phase == "production" and purge_days > 0:
                # (a) Age all in-place purge tanks; harvest the ones that have
                # completed purge, BUT only up to the weekly target (biggest
                # first). Ready tanks beyond the target stay STARVE (frozen) and
                # carry over to next week — so a backlog drains smoothly instead
                # of dumping as a surge (the post-handoff harvest spike).
                _ready = []
                for t in list(state.tanks_by_id.values()):
                    if t.stage == STAGE_STARVE and not t.is_empty:
                        t.starvation_days_remaining -= 7
                        if t.starvation_days_remaining <= 0:
                            _ready.append(t)
                _ready.sort(key=lambda t: (-t.avg_wt_g, t.tank_id))
                _hv = 0.0
                for t in _ready:
                    if _hv >= target:
                        break
                    ev = Harvest(
                        batch_id=t.batch_id, event_date=week_start_date,
                        source_tank_id=t.tank_id, count=t.count,
                        avg_wt_g=t.avg_wt_g, min_tank_control=0,
                    )
                    warnings.extend(ev.apply(state))
                    harvest_events.append(ev)
                    _hv += t.count
                # (b) Enter ~target/week of fresh tanks into purge to keep the
                # staircase going (in == out == target ⇒ the pipeline stays
                # bounded at ~ceil(purge_days/7) cohorts; the step-(a) cap drains
                # any transient backlog smoothly). Don't enter while a ripe backlog
                # already covers next week's target — avoids freezing extra fish.
                _backlog = sum(t.count for t in state.tanks_by_id.values()
                               if t.stage == STAGE_STARVE and not t.is_empty
                               and t.starvation_days_remaining <= 0)
                _entered = 0.0
                if target > 0 and _backlog < target:
                    for bid in _pick_fifo_move_in_batches(state, batch_meta, control):
                        if _entered >= target:
                            break
                        src_tanks = [t for t in state.tanks_by_id.values()
                                     if t.batch_id == bid and not t.is_empty
                                     and t.stage == "SW"
                                     and t.avg_wt_g >= control.min_harvest_weight_g]
                        src_tanks.sort(key=lambda t: (-t.avg_wt_g, t.tank_id))
                        for src in src_tanks:
                            if _entered >= target:
                                break
                            src.stage = STAGE_STARVE
                            src.starvation_days_remaining = purge_days
                            _entered += src.count
            elif target > 0:
                # Immediate harvest (empty-phase tail, or purge_days unset).
                harvested = 0.0
                for bid in _pick_fifo_move_in_batches(state, batch_meta, control):
                    if harvested >= target:
                        break
                    src_tanks = [t for t in state.tanks_by_id.values()
                                 if t.batch_id == bid and not t.is_empty
                                 and t.avg_wt_g >= control.min_harvest_weight_g]
                    src_tanks.sort(key=lambda t: t.avg_wt_g, reverse=True)
                    for src in src_tanks:
                        if harvested >= target:
                            break
                        take = min(target - harvested, src.count)
                        if take <= 0:
                            continue
                        ev = Harvest(
                            batch_id=bid, event_date=week_start_date,
                            source_tank_id=src.tank_id, count=take,
                            avg_wt_g=src.avg_wt_g,
                            min_tank_control=control.min_tank_control,
                        )
                        warnings.extend(ev.apply(state))
                        harvest_events.append(ev)
                        harvested += take

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

            # Multi-objective balancer: relieve any tank still over density cap
            # into the destination with the most headroom across density + system
            # feed + system biomass — cutting out-of-bounds on all three at once
            # without trading one for another. Continuity-safe (conserved
            # Transfers; new tanks forward-persisted).
            _bal_budget = int(getattr(control, "rebalance_balance_budget", 0) or 0)
            if _rebal_on and _bal_budget > 0:
                _balance_loads(
                    state, week_label, transfer_date, transfer_events, warnings,
                    ta_index, week_tank_owner, sorted_weeks, week_index_r,
                    _sys_cap, _rebal_buf, batch_meta, tables, _og_sys,
                    _grow_sys, _og_tanks, _bal_budget,
                )

            # Variable-quantity pass: with the week's placement realized, shave
            # any system still over its biomass/feed cap by moving a PRECISE
            # count of fish between a batch's existing tanks into a system with
            # headroom — exactly enough, no overshoot. Continuity-safe.
            _vq_budget = int(getattr(control, "rebalance_varqty_budget",
                                     _REBALANCE_VARQTY_BUDGET) or 0)
            if _rebal_on and _vq_budget > 0:
                _variable_quantity_rebalance(
                    state, week_label, transfer_date, transfer_events, warnings,
                    _sys_cap, _rebal_buf, batch_meta, tables, og_systems_set,
                    og_tanks_by_system_r, _vq_budget,
                )

        # Day-by-day biology + TranOG entries within this week.
        ws_we = week_ranges.get(week_label)
        if ws_we is not None:
            ws_date, we_date = ws_we

            # PROACTIVE MAKE-ROOM HARVEST (pre-biology). The facility is a
            # conveyor (OG1/2 -> ... -> OG6 -> harvest); harvest is biomass-
            # driven, so when biomass is under cap it lets near-market fish keep
            # growing — which OCCUPIES TANKS and can box out a TranOG arrival
            # scheduled THIS week, forcing the arrival to be DROPPED (losing its
            # entire stocked population — an input-fish-conservation breach; see
            # InputConservationAudit). Before the week's biology runs, ensure
            # enough empty OG growout tanks for this week's arrivals by harvesting
            # the readiest tanks (biggest fish first — nearest market, would be
            # harvested within days anyway). Done PRE-biology at week-open weight
            # so it matches the continuity audit's event order (harvest before
            # growth) and stays drift-free. Conserved via Harvest events.
            _arrivals = [s for s in splits
                         if ws_date <= _as_date(s.tran_og_date) < we_date]
            if _arrivals:
                _need = 0
                for s in _arrivals:
                    _ta = next((a for a in tank_assignments
                                if a.week_label == week_label
                                and a.batch_id == s.batch_id), None)
                    _plan_n = len(_ta.tank_ids) if _ta and _ta.tank_ids else 0
                    _cohort_kg = s.post_cull_count * (s.post_cull_avg_wt_g / 1000.0)
                    _og12_cap = _max_kg_per_og_tank(facility) or (95.0 * 1720.0)
                    _density_n = max(1, math.ceil(_cohort_kg / _og12_cap))
                    _cfg_floor = max(2, (control.tran_og_default_tanks or 2)
                                     if control else 2)
                    _need += max(_plan_n, _cfg_floor, _density_n)
                _empty_og = [t for t in state.tanks_by_id.values()
                             if t.is_empty and t.type == "OG"
                             and t.system_id not in _SIXN_SYSTEMS]
                _deficit = _need - len(_empty_og)
                _min_hv_wt = control.min_harvest_weight_g or 0
                while _deficit > 0:
                    _cands = [t for t in state.tanks_by_id.values()
                              if not t.is_empty and t.type == "OG"
                              and t.system_id not in _SIXN_SYSTEMS
                              and t.avg_wt_g >= _min_hv_wt]
                    if not _cands:
                        break  # genuinely saturated — arrival drop handled below
                    _cands.sort(key=lambda t: (-t.avg_wt_g, t.tank_id))
                    _src = _cands[0]
                    _ev = Harvest(
                        batch_id=_src.batch_id, event_date=ws_date,
                        source_tank_id=_src.tank_id, count=_src.count,
                        avg_wt_g=_src.avg_wt_g, min_tank_control=0,
                    )
                    warnings.extend(_ev.apply(state))
                    harvest_events.append(_ev)
                    warnings.append(
                        f"{week_label}: proactive MAKE-ROOM harvest of "
                        f"{_src.location_id} (batch {_src.batch_id}, "
                        f"{_src.count:.0f} fish @ {_src.avg_wt_g / 1000:.2f}kg) — "
                        f"freeing a tank for {len(_arrivals)} TranOG arrival(s) "
                        f"this week to avoid dropping them"
                    )
                    _deficit -= 1

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
                    # TRANOG MINIMUM TANK FLOOR: cohort gets max(plan, R28-floor,
                    # ceil(biomass / OG1/2-tank-capacity)) tanks. Plan can
                    # under-allocate when free_pool tracking diverges from actual
                    # tank state (purge mode); the config floor (Control R28,
                    # min 2 — one tank per size class) + the density cap force a
                    # higher floor at runtime. Must match the precalc/Phase-A
                    # floors (max(2, tran_og_default_tanks)) so the fallback path
                    # doesn't silently re-impose the old 4-tank reservation.
                    plan_n = len(ta.tank_ids) if ta.tank_ids else 0
                    cohort_kg = split.post_cull_count * (
                        split.post_cull_avg_wt_g / 1000.0
                    )
                    og12_cap_kg = _max_kg_per_og_tank(facility) or (95.0 * 1720.0)
                    density_n = max(1, math.ceil(cohort_kg / og12_cap_kg))
                    cfg_floor = max(2, (control.tran_og_default_tanks or 2)
                                    if control else 2)
                    n_needed = max(plan_n, cfg_floor, density_n)
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
                            f"no empty OG tank AND no harvestable fish ANYWHERE; "
                            f"arrival DROPPED (facility genuinely saturated — "
                            f"operator action required)"
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
                stage=tank.stage,
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
        system_limits=system_limits,
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
