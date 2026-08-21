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
    carry_forward_cap_lookup,
    predictive_move_in_count,
    resolve_facility_cap,
    resolve_system_cap,
    system_cap_with_buffer,
)
from .biology import (
    _fcr_model_key, _feed_type_for_size, _interp, count_split_means,
    realized_feed_kg_day, sgr_pct_per_day, upper_truncated_split,
)
from .events import Grade, GradedHarvest, Harvest, OG12_SYSTEMS, OG12_MOVE_LOCK_WT_G, TankAllocation, Transfer, TranOGEntry
from .tiers import move_allowed, sixn_exit_allowed
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
    pair_combined_count,
)
from .state import FacilityState, TankState, STAGE_STARVE
from .time_grid import (
    forecast_week_labels,
    iso_week_label,
    og_entry_week_start,
    week_range,
)


# Per the lock record §5: all six pipeline tanks (61/63/65 main,
# 67/69/71 sister) live in system OG6N. OG6S is a regular OG3-6
# grow-out system, NOT pipeline-owned, so it is a valid move-in source
# for the 6N purge pipeline (and is allocatable by Phase A/B/C).
_SIXN_SYSTEMS = frozenset({"OG6N"})

# 6N PURGE-mode move-in modeling. Operationally a cohort is TRANSFERRED into a
# 6N depuration tank mid-week (~Friday) and then held OFF-FEED for the rest of
# the ~2-week purge (no growth, no feed) before harvest. Two consequences:
#   1) The weight placed into 6N is the source tank's week-open avg grown by the
#      4 days (Mon→Fri) it kept growing in the source before the transfer.
#   2) On entry the 6N tank is FROZEN (stage=STARVE): the daily biology loop then
#      neither grows nor feeds it, so it is harvested at the frozen entry weight
#      and eats nothing during depuration.
# The 4 pre-transfer days of feed are still real and are recorded as an explicit
# move-in feed entry (see PlacementResult.sixn_move_in_feed) the feed reports add
# in; the STARVE purge weeks themselves are excluded from realized feed.
PURGE_TRANSFER_GROWTH_DAYS = 4

# DEPURATION HOLD (operator rule): fish moved into a 6N purge tank must sit the
# full ~2-week purge before that tank may be drained. Enforced in BOTH
# directions: fills AVOID tanks of the pair that drains NEXT week (the audited
# leak — the anticipatory/reactive make-room dumped into the empty sister of
# the front-of-queue pair, shipping 17-33k fish with 1 week of purge), and the
# rotation's drain HOLDS any tank filled after the PREVIOUS rotation step
# (fail-safe; a held tank drains on the pair's next rotation).
SIXN_MIN_RESIDENCY_DAYS = 14
def _sixn_fill_capacity_fish(state: FacilityState, tank_id: int,
                             avg_wt_g: float, purge: bool = False) -> float:
    """Fish this 6N tank can still take, judged at the given transfer weight.

    PURGE MODE HAS NO DENSITY CEILING (operator, 2026-08-20). What bounds a
    purge tank is the HARVEST SCHEDULE, not kg/m3: the fish are off-feed, not
    growing, and leave within the ~2-week rotation. `purge=True` therefore
    returns unbounded capacity, so ONE batch fills ONE tank however dense.

    This REVERSES the earlier "Rule-2 stage" ruling recorded here, which capped
    purge fills at the tank's density cap and spilled the overflow into the
    pair's sister. That is now a defect, not a fix: a 6N tank is 1,720 m3, so
    95 kg/m3 held it to 163,400 kg, and a ~211,000 kg purge cohort was split
    across BOTH tanks of a pair. The sister (67/69/71) exists ONLY so that a
    SECOND, DIFFERENT batch needing harvest the same week is not mixed into an
    occupied tank — mixing destroys per-batch count fidelity. Spending the
    sister on a single batch's overflow burns the slot that separation needs
    and exhausts the rotation. Measured: VBA splits a batch across a pair 0
    times; this engine did it 39-60 pair-weeks and used 6x the sister capacity.

    In PRODUCTION mode (`purge=False`) 6N is an ordinary production system and
    the tank's configured density cap applies exactly as anywhere else.

    The cap is the tank's OWN `max_density_kg_m3` from config/facility.yaml.
    It used to be a hardcoded 95.0 here, which overrode whatever the operator
    had configured and violated the rule caps.py states for the whole codebase
    ("No capacity figure lives in code"). A 6N tank is 1,720 m3, so 95 held it
    to ~40,800 fish at 4 kg against a 48,000/week harvest floor — the cap, not
    the fish, decided the drain.
    """
    t = state.tanks_by_id.get(tank_id)
    if t is None or avg_wt_g <= 0:
        return 0.0
    if purge:
        # No ceiling: the harvest schedule bounds a depuration tank.
        return float("inf")
    if t.max_density_kg_m3 <= 0:
        return 0.0
    cap_kg = t.volume_m3 * t.max_density_kg_m3
    held_kg = (t.count * t.avg_wt_g / 1000.0) if not t.is_empty else 0.0
    return max(0.0, (cap_kg - held_kg) * 1000.0 / avg_wt_g)


# Drain-guard threshold in EVENT-DATE days: a next-rotation drain is at most 7
# calendar days after its fill (week_starts are 7 apart; the ragged partial
# FIRST forecast week makes it shorter, never longer), while the legal
# 2nd-rotation drain — the standard Wed-fill -> Fri-harvest two-week purge —
# is at least 8 (7 + the >=1-day first week). 8 therefore separates the two
# in every calendar. A raw 14-day floor was measured to misfire on the first
# forecast week (fills dated at the mid-week forecast start reached their
# on-schedule 2nd-rotation drain at 9-12 event days) and put a ZERO-harvest
# week back on 2 of 3 July PRs — breaching the steady-harvest contract.
SIXN_DRAIN_GUARD_MIN_DAYS = 8


def _grow_weight_days(avg_wt_g: float, batch: Optional[BatchInput],
                      tables: BiologyTables, days: int,
                      week_label: Optional[str] = None) -> float:
    """Advance an avg weight by `days` of the batch's SW daily growth.

    Weight only — same daily SW SGR math as `advance_tank_one_day` (SGR-curve
    lookup × the batch's sgr_correction, compounded per day); NO mortality. Used
    to land the 6N move-in weight at the mid-week (Friday) transfer point.
    """
    if avg_wt_g <= 0 or batch is None or tables is None or days <= 0:
        return avg_wt_g
    w = float(avg_wt_g)
    for _ in range(days):
        sgr_eff = sgr_pct_per_day(w, "SW", batch, tables, week_label)
        w = w * (1.0 + sgr_eff / 100.0)
    return w


# Closed-loop harvest controller tuning (placement #2).
#   _SETPOINT_FRACTION — biomass setpoint as a fraction of the facility cap.
#     The predictive move-in + reactive supplement drive biomass toward this
#     level. 1.0 sits ON the cap (max utilisation, peaks ~+3%, over cap ~half
#     the weeks); 0.995 centres just under it (near-identical in-band count,
#     peaks held ~+2.6%, under cap a majority of weeks). Tuned empirically
#     against the live config; weekly growth (~136t) exceeds the ±1% band
#     width (~78t), so ~±2.6% swings are the physical floor.
_SETPOINT_FRACTION = 0.995
# ANTICIPATORY setpoint margin. Instead of a flat fraction, pre-position biomass
# below the cap by ~_SETPOINT_LOOKAHEAD_WEEKS of the facility's REALIZED weekly
# growth, so the upcoming growth fills the headroom up to (not over) the cap and
# the harvest pre-sheds each peak across the calm run-up weeks rather than
# spiking past the processing max in the peak week. Self-adapting and safe: the
# margin grows when the facility is actually growing fast toward a peak and
# shrinks when flat (full utilisation), and it is anchored in realized growth —
# NOT the decoupled projection that historically drove harvest oscillation. The
# margin is clamped to [_MARGIN_MIN_FRAC, _MARGIN_MAX_FRAC] of the cap so
# utilisation never drops below ~(1 - _MARGIN_MAX_FRAC).
# Anticipatory margin = this many weeks of the facility's REALIZED growth, held
# below the cap so growth fills the headroom up to (not over) the cap and the
# harvest pre-sheds each peak across the calm run-up weeks. Anchored in realized
# growth (NOT a forward projection: the Phase-A projection under-predicts realized
# peaks by ~3% and breaches the hard cap if trusted for tight anticipation).
#
# Tuning (config(7), measured; biomass-over-cap weeks / mean facility utilisation):
#   0.50 -> 3 wks over (worst +1.8%) / 96.1%
#   0.60 -> 2 wks over (worst +0.6%) / 96.2%
#   0.75 -> 1 wk  over (worst +0.4%) / 95.8%   <- DEFAULT (tolerance-aware: the
#           lone touch sits inside the R24 +-deviation band, ~1% more utilisation)
#   0.90 -> 0 wks over (strictly under the HARD cap) / 94.8%
# Larger = safer/lower utilisation; smaller = tighter/occasional touches. The
# residual gap to 100% is NATURAL cohort troughs (weeks with little mature
# biomass), not slack. Raise toward 0.90 for a strict zero-breach run.
#
# INACTIVE (audit L7): this lookahead-margin mechanism and its Control knob
# `harvest_setpoint_lookahead_weeks` were SUPERSEDED by the dual-limit setpoint
# (min(biomass_cap, feed-implied cap), one facility_biomass_deviation_pct band
# below). No harvest path reads the knob or the constants below anymore; they are
# retained only so configs that predate the redesign still load.
_SETPOINT_LOOKAHEAD_WEEKS = 0.75
_MARGIN_MIN_FRAC = 0.005
_MARGIN_MAX_FRAC = 0.04


@dataclass
class _HarvestBudget:
    """Shared per-week harvest ceiling (fish count) threaded through every
    harvest pass when level-loading is ON. When OFF the caller sets cap=inf, so
    `take()` is a pass-through and emissions are byte-identical to the legacy
    path. `record()` tracks the ACTUAL fish taken (INV-5 force-empty aware), so
    the budget never under-counts; a forced full-tank may push `used` slightly
    past `cap` (captured in `overdraw`, carried into next week by the caller)."""
    cap: float
    used: float = 0.0
    overdraw: float = 0.0

    def remaining(self) -> float:
        return max(0.0, self.cap - self.used)

    def take(self, want: float) -> float:
        if want <= 0:
            return 0.0
        return min(want, self.remaining())

    def record(self, emitted: float) -> None:
        self.used += emitted
        if self.used > self.cap:
            self.overdraw = self.used - self.cap

# Arrival feed-forward smoothing window (weeks). Each scheduled TranOG arrival
# is pre-harvested over this many weeks before it lands. Empirically W=1
# (pre-draw in the move-in whose drain coincides with the arrival week) holds
# biomass tightest — wider windows draw biomass down too early/deep ahead of
# each batch and widen the swing. Kept as a tunable for non-default stocking
# cadences.
_ARRIVAL_SMOOTH_WEEKS = 1

# Lookahead (weeks) for anticipating the FW biomass rise in the harvest setpoint.
# The FW curve is fully known forward, so the predictive harvest pre-positions OG
# drawdown for the upcoming FW peak over this window rather than reacting once the
# total has spiked. Sized to the move-in lead so the weekly harvest clip keeps the
# facility under the cap without falling behind the growing FW load.
_FW_ANTICIPATE_WEEKS = 8

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

# ---- END-OF-WEEK CAP REPAIR (`cap_repair_budget`) --------------------------
# Every realised repair pass above (`_rebalance_systems_realized`,
# `_even_out_density`, `_balance_loads`, `_variable_quantity_rebalance`,
# `_consolidate_remnants`) runs BEFORE the week's day-by-day biology
# (`advance_tank_one_day`, further down phase_d_emit_events) — but the
# BatchLocations snapshot the SystemLimitsAudit sums is taken AFTER it. So the
# whole stack aims at the START-of-week load while every published metric
# measures the END-of-week one, and a full week of growth (~+7% biomass, ~+11%
# feed at entry-tier weights) lands in between with nothing left to check it.
# MEASURED on the 7.29 PR: of 104 non-6N over-cap (system, week) cells, the
# balancer had ALREADY left 79 of them compliant at its own exit (0.94-0.99 of
# cap) with no further event touching the system — growth alone carried them
# over. That is why `_BALANCE_SYS_FILL = 0.90` was not enough: 0.90 x 1.11 ~ 1.0.
# The repair pass below closes the loop by running LAST — after biology, after
# the grade split, immediately before the snapshot — on the state that is
# actually measured. It is a REPAIR, not a leveller: it fires only on a system
# over its RAW cap (the audited definition), moves the minimum, and stops.
# Running last also makes it the only pass whose handling-budget arithmetic is
# exact: nothing essential follows it, so spending `_moves_left()` can never
# push the week over `max_transfers_per_week`.
_REPAIR_SRC_FILL = 0.98      # relieve the hot system to this fraction of cap
_REPAIR_DST_FILL = 0.95      # never fill a destination system past this
_REPAIR_DENS_FILL = 0.95     # never fill a destination TANK past this x density cap
# FOOTPRINT-NEUTRAL: the repair may only top up a tank the batch ALREADY holds,
# never claim an empty one. This is a measured restriction, not caution:
#   * REACH is barely affected. Of the 104 non-6N over-cap cells on the 7.29 PR,
#     103 had a legal same-batch destination in a system under 0.95 of cap and
#     only 82 had a legal EMPTY one — so same-batch-only is the LARGER of the
#     two candidate sets, not a subset.
#   * The empty-claim variant broke the harvest contract. Measured over 8
#     starting states, claiming empties (with the matching `_persist_system_add`
#     that keeps the new tank in the batch's forward plan) shrank the free-tank
#     pool the harvest controller relies on, and on 7.29-nowin the 2028-W22 week
#     went from 46,974 fish over 3 tanks to a single 83,869-fish dump of three
#     whole B57 tanks — straight through the 60,500 relief CEILING, which is
#     never legal. That is precisely the free-tank antagonism already recorded
#     for harvest_level_load (leveling spreads fish thinner -> fewer free whole
#     tanks -> more make-room dumps -> spikier harvest).
# Keeping the tank SET unchanged also means no forward plan edit and no diff
# churn — the same property `_variable_quantity_rebalance` was built around.

# Pad applied to the 6N fill FLOOR clamp (and the graded floor fills) so a
# fill sized to the weekly harvest floor still drains AT the floor after the
# ~2-week purge-residency mortality (~0.15%); without it every floor-clamped
# fill lands a permanent ~45 fish short (the 29,955-vs-30,000 miss class).
_SIXN_FILL_MORTALITY_PAD = 1.002


# Eligible system sets used by Phase A.
_OG_ALL_WITH_6N = ["OG1N", "OG1S", "OG2N", "OG2S", "OG3N", "OG3S",
                   "OG4N", "OG4S", "OG5N", "OG5S", "OG6N", "OG6S"]
# In purge mode OG6N is owned by the 6N pipeline (_run_sixn_purge_week
# handles harvests + move-ins), so Phase A/B/C must NOT allocate it
# — otherwise non-6N tanks end up overpacked while OG6N goes unused
# by the rebalancer. Use OG6N only when 6N is in production mode.
_OG_ALL = ["OG1N", "OG1S", "OG2N", "OG2S", "OG3N", "OG3S",
           "OG4N", "OG4S", "OG5N", "OG5S", "OG6S"]
_OG12 = sorted(OG12_SYSTEMS)  # entry tier (tiers.ENTRY_SYSTEMS, via events alias)


def _as_date(d):
    if isinstance(d, datetime):
        return d.date()
    return d


# ============================================================
# Remnant floor (INV-5 at the SOURCE)
# ============================================================
# Every operation that removes fish from a tank must obey "take all, or leave
# >= min_tank_control": a sub-min "remnant" ties up a whole tank + feed line
# for a rounding error of fish (min_tank_control is the operator's "a tank
# under this is not worth operating" floor). These two helpers implement the
# rule at the EMITTER, so remnants are never created rather than repaired.

# Mortality pad on the KEEP side of the floor. A residue left at exactly the
# floor erodes below it by weekly mortality (~0.05-0.1%/wk) before the weekly
# snapshot — measured: every "leave exactly 7,000" residue showed up as a
# 6,996-fish sub-min row the same week. Keeping floor x 1.02 (~30 weeks of
# erosion headroom) makes a floor-kept tank stay a legal tank for the
# remainder of its natural plan life; the weekly sweep catches the long tail.
_REMNANT_KEEP_PAD = 1.02
# The sweep's trigger mirrors one week of erosion ABOVE the floor, so a tank
# about to dip under the floor by mortality is folded the week BEFORE the
# sub-min snapshot row would appear, not the week after.
_REMNANT_SWEEP_PAD = 1.002


def _floored_take(src_count: float, want: float, min_keep: float) -> float:
    """Clamp a removal so the source ends EMPTY or with >= min_keep fish.

    Preference order: (1) an unaffected take when the residue is legal;
    (2) a REDUCED take that leaves exactly min_keep growing (no overshoot of
    the caller's target — later sources make up the difference); (3) TAKE-ALL
    when the source can't retain a workable population (src_count <= min_keep
    — including a pre-existing remnant, which this drains). min_keep <= 0
    disables the floor (returns `want` clamped to the tank).
    """
    want = min(want, src_count)
    if want >= src_count - 0.5:
        return src_count                       # full drain intended anyway
    keep = min_keep * _REMNANT_KEEP_PAD        # floor + mortality-erosion pad
    if min_keep <= 0 or (src_count - want) >= keep:
        return want                            # legal residue
    reduced = src_count - keep
    if reduced > 0.5:
        return reduced                         # leave the (padded) floor
    return src_count                           # can't retain a workable tank


def _floored_partial(src_count: float, want: float, min_keep: float) -> float:
    """`_floored_take` for passes that must NEVER escalate to take-all (the
    rebalancers, whose move size is capped by destination headroom): returns a
    possibly reduced take, 0.0 meaning "skip this move"."""
    want = min(want, src_count)
    if want >= src_count - 0.5:
        return want
    keep = min_keep * _REMNANT_KEEP_PAD        # floor + mortality-erosion pad
    if min_keep <= 0 or (src_count - want) >= keep:
        return want
    return max(0.0, src_count - keep)


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
    # 6N purge-mode pre-transfer feed: (batch_id, week_label, feed_type) -> kg.
    # The 4 feed-days each move-in cohort eats in the source tank before the
    # mid-week transfer into off-feed (STARVE) 6N depuration. The feed reports
    # add these in; the STARVE 6N tank-weeks are excluded from realized feed.
    sixn_move_in_feed: dict = field(default_factory=dict)
    # Realized biology per (tank_id, week_label, batch_id) -> [bio_delta_kg,
    # mort_count]: the growth-minus-mortality biomass change and mortality count
    # the daily walker actually applied. The continuity audit reconciles against
    # these (ground truth) instead of re-estimating growth from a coarse weekly
    # SGR, so split-off sub-populations no longer false-positive a BIO_DRIFT.
    realized_biology: dict = field(default_factory=dict)


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


def _tranog_tank_need(cohort_kg, facility, control, plan_n=0):
    """OG tanks a TranOG cohort needs: max(plan tanks, the R28 config floor,
    the density-driven need = ceil(cohort_kg / smallest-OG-tank-cap)).

    Single source for the THREE sites that must agree or the no-drop invariant
    can trip: the anticipatory purge pacing (which passes plan_n=0 — plan tanks
    are resolved per-week elsewhere), the reactive make-room, and the actual
    TranOG placement. Uses the raw (undiscounted) tank cap, matching all three
    prior copies. (NOTE: Phase A sizes with the density-discounted cap, so this
    floor can differ from Phase A's by the density_target_pct factor — a known,
    pre-existing behaviour preserved here, not introduced by the extraction.)
    """
    cap = _max_kg_per_og_tank(facility)
    if cap <= 0:
        # A density AND a tank volume used to be written here as a fallback.
        # Sizing the no-drop TranOG invariant against numbers the operator never
        # set means planning against a figure that appears on no sheet and in no
        # config — the exact thing caps.py forbids ("No capacity figure lives in
        # code"). Name the missing input instead, same contract as
        # caps.require_system_cap.
        raise ValueError(
            "Cannot size a TranOG cohort: no OG tank in config/facility.yaml "
            "carries a positive max_density_kg_m3, so there is no per-tank "
            "capacity to divide by. Set max_density_kg_m3 on the OG tanks.")
    density_n = max(1, math.ceil(cohort_kg / cap))
    cfg_floor = max(2, (control.tran_og_default_tanks or 2) if control else 2)
    return max(plan_n, cfg_floor, density_n)


# --------------------------------------------------------------------------- #
# ANTICIPATORY HANDLING BUDGET — the two policy decisions, as pure functions.
#
# The weekly handling budget (operator rule 4) is spent by two kinds of pass:
# DEFERRABLE quality leveling, which checks the budget before emitting, and
# ESSENTIAL work, which never does. The essential passes run LAST in the week,
# so quality used to spend the budget out from under them and the week ended
# over cap. Both functions below let a pass anticipate that instead. They only
# ever REDUCE the moves a deferrable pass may make — neither authorises a move,
# so neither can create a topology violation, a remnant or a multi-batch tank.
#
# BOTH LAYERS ARE OFF. They were built to make the plan meet the 15-move
# handling budget structurally, and they do — but a 4-arm ablation (both off /
# A only / B only / both on) x 3 PRs x 2 knob sets measured what they cost, and
# the operator's rule order (steady weekly harvest is HARD; on handling they
# said "we can move to 15 if we need to") puts the trade the wrong way round.
# Full evidence table in the RESERVE block inside phase_d_emit_events; the
# headline, on the operator's own PR (7.29.26 + their manual window) with their
# tuned hybrid knobs:
#
#                 wks over 15   worst wk   worst harvest   wks < 30k floor
#   both OFF            1          17         24,137          3  (9,721 short)
#   A only              0          15         23,513          5 (22,687 short)
#   B only              1          17         24,137          3  (9,721 short)
#   both ON             0          15         23,513          5 (22,687 short)
#
# Layer A buys the budget compliance AND pays for it out of the harvest floor —
# they are the same lever, not two. Layer B is inert on 2 of the 3 PRs (its
# plan is IDENTICAL to both-off) and net harmful on the third. And on 7.9.26
# with tuned knobs, Layer A produces a 69,677-fish harvest week — past the
# 60,500 relief ceiling, a hard-rule breach that neither OFF nor B ever commits.
#
# The switches STAY (rather than deleting the code) because they are the
# reproducibility handle for that measurement and the one-line lever if the
# operator ever re-orders the rules and makes <=15 moves hard. They are NOT
# operator config: deliberately no control.yaml key and no app control, because
# this is a settled engineering result, not a per-run decision.
_ANTICIPATE_ARRIVAL_RESERVE = False    # Layer A — reserve for arrival make-room
_ANTICIPATE_PACING_DEFER = False       # Layer B — purge pacing stands down


def _entry_makeroom_move_cost(need_tanks: int, empty_entry: int,
                              free_growout: int, vacatable_entry: int) -> int:
    """Transfer MOVES the arrival-week entry-tier make-room is going to need.

    A TranOG cohort may enter ONLY the entry tier (R1), so it needs
    `need_tanks` empty OG1/2 tanks. `empty_entry` are already free; each one
    short must be VACATED, and each vacate costs:

      * 1 move — the entry occupant goes FORWARD into a free grow-out tank
        (R2, legal at any weight; never backward, never harvested — R4/R5);
      * +1 move when no grow-out tank is standing free to receive it, because
        a grow-out slot is freed by moving its fish into 6N first.

    `vacatable_entry` caps it: the pass can only vacate as many entry tanks as
    it has (non-depurating) occupants to move, so we never reserve budget for
    work that cannot happen.

    Pure arithmetic — no state, no side effects. Returns 0 when the entry tier
    already has room, which is the no-congestion case (no reserve, quality
    leveling proceeds at full budget).
    """
    deficit = min(int(need_tanks) - int(empty_entry), int(vacatable_entry))
    free = int(free_growout)
    cost = 0
    for _ in range(max(0, deficit)):
        cost += 1                      # entry -> grow-out forward vacate (R2)
        if free > 0:
            free -= 1
        else:
            cost += 1                  # free that slot: a 6N purge move-in
    return cost


def _quality_moves_left(moves_left: int, reserve: int, move_cap: int) -> int:
    """LAYER A. The budget the DEFERRABLE quality passes may spend: the raw
    remaining budget minus the anticipated arrival-week make-room `reserve`.

    Clamped so the reserve can never exceed the whole budget: a week whose
    essential work alone is over the cap is a real capacity signal the handling
    gate must still report, not a reason to silently starve the leveling.

    With `_ANTICIPATE_ARRIVAL_RESERVE` off (the shipped default — see the
    evidence block above) this is the identity on `moves_left`, i.e. exactly
    the pre-reserve behaviour: quality spends the whole remaining budget.
    """
    if move_cap <= 0 or not _ANTICIPATE_ARRIVAL_RESERVE:
        return moves_left
    return max(0, int(moves_left) - min(int(reserve), int(move_cap)))


def _pacing_may_defer(weeks_out: int, moves_left: int) -> bool:
    """LAYER B. May the anticipatory purge pacing pass stand down this week?

    That pass pre-frees a grow-out tank for a TranOG arrival that is
    `weeks_out` weeks away, and it walks a multi-week lookahead — its own
    contract is that an unavailable week simply waits. So it may stand down
    when the week has no handling budget left (`moves_left` <= 0) AND the
    arrival is more than one week out, i.e. the lookahead still has a calmer
    week to do the work in.

    Never True for an arrival landing NEXT week (`weeks_out` <= 1): the last
    chance to pre-stage is always taken, so no arrival is left short a tank
    and the work is only ever moved EARLIER, never refused.

    With `_ANTICIPATE_PACING_DEFER` off (the shipped default) this is
    constantly False — the pass never stands down, which is the pre-layer
    behaviour.
    """
    if not _ANTICIPATE_PACING_DEFER:
        return False
    return int(weeks_out) > 1 and int(moves_left) <= 0


def phase_a_precalc(
    biology_states_by_batch: dict[str, list[BatchWeekState]],
    harvest_demands: list[HarvestDemand],
    splits: list[SizeClassSplit],
    facility: FacilityConfig,
    control: Optional[ControlParams] = None,
) -> list[BatchWeekLoad]:
    """Compute per-(batch, week) load + tank-count demand + eligibility."""
    max_kg = _max_kg_per_og_tank(facility)
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
        # OG arrival = the batch's FIRST SW week, but only when it crosses
        # TranOG *within* the horizon (has an earlier non-SW week). Batches
        # already SW at forecast_start (PR-hydrated) start SW at week 0 with
        # no preceding FW week and are NOT arrivals. Biology defers the
        # FW->SW flip to the OG-entry week (VBA `wS >= TranOGDate`), so the
        # first SW week IS the correct OG-entry week. See time_grid
        # .og_entry_week_start / BUG #1.
        _first_sw_idx = next(
            (i for i, s in enumerate(states_sorted) if s.stage == "SW"),
            None,
        )
        tranog_week_label = (
            states_sorted[_first_sw_idx].week_label
            if _first_sw_idx is not None and _first_sw_idx > 0
            else None
        )
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
            is_tranog = (s.week_label == tranog_week_label)
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
            and t.system_id not in OG12_SYSTEMS   # R5: no harvest/6N staging from entry tier
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
    reserved=frozenset(),
    tables: Optional[BiologyTables] = None,
    sixn_move_in_feed: Optional[dict] = None,
    retain_in_source: bool = False,
    max_count: Optional[float] = None,
) -> float:
    """Graded harvest fallback (DESIGN §5a) when no batch's avg_wt is
    above min_harvest_weight.

    retain_in_source=True (grade-to-min top-up): the small (< harvest-weight)
    tail STAYS in the source tank — no separate retention tank needed; only the
    big tail is peeled to the 6N pickup. Honors min_transfer_count (don't peel a
    sub-min group out) and min_tank_control (don't leave a sub-min dribble).

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
    # Candidate scan: largest avg first (closer to threshold => fatter tail),
    # but a tank whose PEELABLE tail is below the operator floors must be
    # SKIPPED, not allowed to end the search — a small nearly-ripe tank was
    # blocking big peelable tanks right behind it and returning 0 (the
    # 2026-W44 empty fill: a 6.4k tank at 3.43 kg shadowed 40k tanks at 3.38).
    _min_tr = getattr(control, "min_transfer_count", 0.0) or 0.0
    chosen = None
    for b in fifo:
        cands = [
            t for t in state.tanks_by_id.values()
            if t.batch_id == b.batch_id and not t.is_empty
            and t.system_id not in _SIXN_SYSTEMS
            and t.system_id not in OG12_SYSTEMS   # R5: no graded 6N staging from entry tier
            and t.avg_wt_g < min_hv  # not eligible for regular move-in
        ]
        # Prefer largest avg first (closer to threshold => fatter tail).
        cands.sort(key=lambda t: t.avg_wt_g, reverse=True)
        for t in cands:
            frac_t = frac_above(t.avg_wt_g, t.cv_pct or 16.0, min_hv)
            if frac_t < min_fraction:
                continue
            peel_t = t.count * frac_t
            if max_count is not None:
                peel_t = min(peel_t, max_count)
            if peel_t < max(1.0, _min_tr):
                continue   # tail under min_transfer — try the next tank
            # REMNANT FLOOR (both modes): the small tail becomes a standing
            # tank population — in the SOURCE (retain_in_source) or in the
            # retention tank. Either way a sub-min tail would strand a
            # remnant, so skip the candidate.
            if 0 < (t.count - peel_t) < (control.min_tank_control or 0):
                continue   # would leave a sub-min dribble behind
            chosen = t
            break
        if chosen:
            break

    if chosen is None:
        return 0.0

    cv = chosen.cv_pct or 16.0
    frac = frac_above(chosen.avg_wt_g, cv, min_hv)
    big_count = chosen.count * frac
    # Surgical cap: peel only what's needed to close the floor gap (not the whole
    # over-weight tail) — minimizes the yield/biomass given up and the rotation
    # disturbance. The remaining over-weight fish stay in source and harvest later.
    if max_count is not None and big_count > max_count:
        big_count = max(0.0, max_count)
    small_count = chosen.count - big_count
    # Take the two means from the fraction actually MOVED, not from the harvest
    # weight. The cap above is applied to the COUNT, so when it bites the pickup
    # is a smaller, heavier group than "everything over 3.5 kg" — and the heavy
    # fish left behind belong to the retention leg's mean. Pricing retention at
    # the full lower-tail mean instead loses that mass outright: measured on the
    # 8.13 PR, 24 graded splits swung -6,493 to +1,546 kg (net +5,150), and the
    # three worst losses were three of the four largest biomass-drift rows in
    # the run. `count_split_means` conserves by construction and is IDENTICAL to
    # the threshold split whenever the cap does not bite, so an uncapped graded
    # split is unchanged.
    big_avg, small_avg = count_split_means(
        chosen.avg_wt_g, cv,
        (big_count / chosen.count) if chosen.count > 0 else 0.0)

    # Retention destination. retain_in_source (grade-to-min top-up): the small tail
    # STAYS in the SOURCE tank — same batch, no extra tank needed. Otherwise (make-
    # room): a free OG3+ tank not in the 6N pipeline / OG1-2 / reserved.
    if retain_in_source:
        retention = chosen  # the small tail stays in the SOURCE tank (same batch)
    else:
        retention = next(
            (t for t in sorted(state.tanks_by_id.values(), key=lambda x: x.tank_id)
             if t.is_empty and t.type == "OG"
             and t.system_id not in _SIXN_SYSTEMS
             and t.system_id not in OG12_SYSTEMS
             and t.tank_id not in reserved),
            None,
        )
        if retention is None:
            warnings.append(
                f"{week_label}: graded move-in for {chosen.batch_id} "
                f"{chosen.location_id} declined (no free OG3+ retention tank)"
            )
            return 0.0

    # PURGE move-in: the big (pickup) portion lands in the 6N main tank at the
    # mid-week transfer weight (its upper-tail mean grown 4 SW days) and is
    # frozen below; the retention tail stays in production at its week-open
    # weight. _try_graded_move_in is only reached from the 6N purge pipeline,
    # so this path is always purge mode.
    _bm = batch_meta.get(chosen.batch_id)
    pickup_xfer_wt = (_grow_weight_days(big_avg, _bm, tables,
                                        PURGE_TRANSFER_GROWTH_DAYS, week_label)
                      if tables is not None else big_avg)

    ev = GradedHarvest(
        batch_id=chosen.batch_id,
        event_date=week_start_date,
        source_tank_id=chosen.tank_id,
        pickup_tank_id=pair[0],
        pickup_count=big_count,
        pickup_avg_wt_g=pickup_xfer_wt,
        pickup_source_avg_wt_g=big_avg,  # pre-growth: debit source at this wt
        retention_tank_id=retention.tank_id,
        retention_count=small_count,
        retention_avg_wt_g=small_avg,
        cv_pct=cv,
    )
    _pk = state.tanks_by_id.get(pair[0])
    _pk_pre = _pk.count if (_pk is not None and not _pk.is_empty) else 0.0
    warns = ev.apply(state)
    warnings.extend(warns)
    # Refusal-aware: GradedHarvest.apply refuses non-destructively (e.g. the
    # pickup already holds a DIFFERENT batch). Returning big_count anyway
    # would miscount the fill and book feed for fish that never moved.
    _pk_post = _pk.count if (_pk is not None and not _pk.is_empty) else 0.0
    if _pk is None or _pk_post - _pk_pre < big_count - 0.5:
        return 0.0
    # Freeze the 6N pickup tank (off-feed depuration) and book its 4 pre-
    # transfer feed-days; the retention tank keeps growing/feeding normally.
    _freeze_6n_dest(state, pair[0], fill_date=week_start_date)
    if tables is not None:
        _book_move_in_feed(sixn_move_in_feed, chosen.batch_id, week_label,
                           pickup_xfer_wt, big_count, tables, _bm)
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


def _transit_entry_to_pair(
    state: FacilityState,
    batch_meta: dict[str, BatchInput],
    control: ControlParams,
    week_label: str,
    week_start_date: date,
    fill_pair,
    goal: float,
    already_moved: float,
    transfer_events: list,
    grade_events,
    warnings: list[str],
    reserved,
    tables,
    sixn_move_in_feed,
    avoid=frozenset(),
) -> float:
    """Route market-ready ENTRY-tier fish into the 6N fill pair the legal way.

    R5 forbids staging fish into 6N FROM an entry tank, and R2 allows entry ->
    grow-out at any weight — so the physical route is a two-hop transit:
    entry -> free grow-out HOP tank -> pair (the second hop reuses
    _make_room_into_6n: purge transfer weight, freeze, feed booking).

    Two stages, readiest-first:
      1. WHOLE-TANK transit of ripe entry tanks (avg >= min_harvest_weight),
         pursued up to `goal` (the fill target);
      2. GRADED transit of NEAR-ripe entry tanks (avg < min but a meaningful
         upper tail >= min): a Grade event peels the ripe tail into the hop
         tank at its conditional mean (small tail stays in the source at its
         lower mean — count + biomass conserve exactly), pursued only up to
         the harvest FLOOR (min_harvest_per_week) — an exception that keeps
         the drain from going dark, never a bulk-grading rule.

    Hop tanks: empty grow-out tanks, unreserved first. A tank RESERVED for an
    imminent TranOG arrival may serve as a TRANSIENT hop (it is empty again the
    moment the second leg completes) but only when the pair verifiably has an
    open slot, so the fish can never strand in the held tank. The reservation
    is lifted around the two legs and restored afterwards.

    This is the marginal feedstock the entry-tier rules removed from the
    rotation's direct pool (pre-rules it pulled/graded straight from OG1/2);
    without it the pipeline runs dry for the 1-2 weeks a FIFO batch gap
    leaves only entry-tier fish at/near harvest weight — the measured
    empty-week regressions. Returns the count added to the pair.
    """
    min_hv = control.min_harvest_weight_g or 0.0
    # CONTINUITY-ONLY: the transit exists to keep the drain from going dark,
    # not to chase the controller's full demand target — entry-tier fish it
    # doesn't take keep growing (they are 1 hop further from harvest, so
    # taking them early costs more growth than a grow-out draw). Cap the
    # whole transit at the padded FLOOR; demand above the floor is served by
    # the grow-out cascade alone, exactly as it was pre-rules.
    goal = min(goal, float(control.min_harvest_per_week or goal)
               * _SIXN_FILL_MORTALITY_PAD)
    if min_hv <= 0 or goal <= already_moved:
        return 0.0
    from statistics import NormalDist as _ND
    _std = _ND()

    def _frac_above(avg_wt: float, cv_pct: float, t: float) -> float:
        if avg_wt <= 0 or cv_pct <= 0:
            return 1.0 if avg_wt >= t else 0.0
        z = (t - avg_wt) / (avg_wt * cv_pct / 100.0)
        return max(0.0, min(1.0, 1.0 - _std.cdf(z)))

    def _hop_tank():
        """(tank, was_reserved) — empty grow-out hop tank, unreserved first;
        a reserved tank only transiently and only if the pair can accept."""
        frees = [t for t in sorted(state.tanks_by_id.values(),
                                   key=lambda x: x.tank_id)
                 if t.is_empty and t.type == "OG"
                 and t.system_id not in _SIXN_SYSTEMS
                 and t.system_id not in OG12_SYSTEMS]
        for t in frees:
            if t.tank_id not in reserved:
                return t, False
        if frees and _free_6n_slots(state, fill_pair, avoid):
            return frees[0], True
        return None, False

    def _second_leg(hop, was_reserved, src_loc) -> bool:
        """Move the hop tank into the pair; restore a transient reservation."""
        if was_reserved:
            state.reserved_tanks.discard(hop.tank_id)
        ok = _make_room_into_6n(
            state, hop, week_start_date, fill_pair,
            transfer_events, warnings, week_label,
            reason=f"6N rotation fill via entry forward-transit (from {src_loc})",
            sixn_move_in_feed=sixn_move_in_feed, tables=tables,
            batch_meta=batch_meta, is_purge=True, avoid=avoid)
        if was_reserved:
            state.reserved_tanks.add(hop.tank_id)
        return ok

    added = 0.0
    # ---- Stage 1: whole-tank transit of ripe entry tanks --------------------
    _ripe = sorted(
        [t for t in state.tanks_by_id.values()
         if not t.is_empty and t.type == "OG"
         and t.system_id in OG12_SYSTEMS and t.stage == "SW"
         and t.avg_wt_g >= min_hv],
        key=lambda t: (-t.avg_wt_g, t.tank_id))
    for _es in _ripe:
        if already_moved + added >= goal:
            break
        hop, was_res = _hop_tank()
        if hop is None:
            break
        # REMNANT FLOOR: a partial transit must leave the entry source empty or
        # >= min_tank_control (reduced take preferred; take-all when the source
        # can't retain the floor — the fish route to harvest via the pair).
        _take = _floored_take(_es.count,
                              min(goal - (already_moved + added), _es.count),
                              control.min_tank_control or 0.0)
        if _take <= 0:
            continue
        _e_batch, _e_loc = _es.batch_id, _es.location_id
        if was_res:
            state.reserved_tanks.discard(hop.tank_id)
        _hop_mv = Transfer(
            batch_id=_e_batch, event_date=week_start_date,
            source_tank_id=_es.tank_id,
            destinations=[TankAllocation(
                tank_id=hop.tank_id, count=_take,
                avg_wt_g=_es.avg_wt_g, cv_pct=_es.cv_pct)],
            leaves_source_empty=False,
        )
        warnings.extend(_hop_mv.apply(state))
        transfer_events.append(_hop_mv)
        if was_res:
            state.reserved_tanks.add(hop.tank_id)
        if hop.is_empty:   # first leg refused
            break
        _hopped = hop.count
        if not _second_leg(hop, was_res, _e_loc):
            break          # 6N full — fish stay in the hop tank as grow-out
        added += _hopped

    # ---- Stage 2: GRADED transit of near-ripe entry tanks (floor only) -----
    _floor_goal = min(goal, float(control.min_harvest_per_week or goal)
                      * _SIXN_FILL_MORTALITY_PAD)
    _min_tr = getattr(control, "min_transfer_count", 0.0) or 0.0
    if already_moved + added < _floor_goal and grade_events is not None:
        _near = [t for t in state.tanks_by_id.values()
                 if not t.is_empty and t.type == "OG"
                 and t.system_id in OG12_SYSTEMS and t.stage == "SW"
                 and t.avg_wt_g < min_hv
                 and _frac_above(t.avg_wt_g, t.cv_pct or 16.0, min_hv) >= 0.10]
        _near.sort(key=lambda t: (-t.avg_wt_g, t.tank_id))
        for _es in _near:
            if already_moved + added >= _floor_goal:
                break
            hop, was_res = _hop_tank()
            if hop is None:
                break
            cv = _es.cv_pct or 16.0
            frac = _frac_above(_es.avg_wt_g, cv, min_hv)
            big = min(_es.count * frac,
                      _floor_goal - (already_moved + added))
            small = _es.count - big
            if big < max(1.0, _min_tr):
                continue
            if small < (control.min_tank_control or 0):
                continue   # never leave a sub-min dribble behind
            # `big` was capped by the floor goal above, so take the means from
            # the fraction actually moved — the threshold split would price the
            # retention leg as if the heavy fish it kept were not in it. See
            # biology.count_split_means; identical when the cap does not bite.
            big_avg, small_avg = count_split_means(
                _es.avg_wt_g, cv, (big / _es.count) if _es.count > 0 else 0.0)
            _e_batch, _e_loc = _es.batch_id, _es.location_id
            if was_res:
                state.reserved_tanks.discard(hop.tank_id)
            g = Grade(
                batch_id=_e_batch, event_date=week_start_date,
                source_tank_ids=[_es.tank_id],
                destinations=[
                    TankAllocation(tank_id=_es.tank_id, count=small,
                                   avg_wt_g=small_avg, cv_pct=cv),
                    TankAllocation(tank_id=hop.tank_id, count=big,
                                   avg_wt_g=big_avg, cv_pct=cv),
                ],
            )
            warnings.extend(g.apply(state))
            if was_res:
                state.reserved_tanks.add(hop.tank_id)
            if hop.is_empty:   # grade refused
                continue
            grade_events.append(g)
            _hopped = hop.count
            if not _second_leg(hop, was_res, _e_loc):
                break
            warnings.append(
                f"{week_label}: GRADED entry transit — peeled the ripe tail "
                f"({_hopped:,.0f} fish @ {big_avg / 1000:.2f}kg) of {_e_loc} "
                f"(batch {_e_batch}) forward into the 6N fill pair; "
                f"{small:,.0f} fish stay growing at {small_avg / 1000:.2f}kg")
            added += _hopped
    return added


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
    budget: Optional["_HarvestBudget"] = None,
    reserved=frozenset(),
    tables: Optional[BiologyTables] = None,
    sixn_move_in_feed: Optional[dict] = None,
    grade_events=None,
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
    # Determine this week's harvest + fill pairs.
    if not pair_queue:
        # BOOTSTRAP / CONTINUE the round-robin from an EMPTY 6N. The pipeline can start
        # empty — a fresh facility, or (the common case) the PR-hydrated 6N drained by
        # the operator's manual starting-window before the forecast opens. With no
        # stocked pair to harvest, run a SEED week: no harvest, just fill the fallow
        # (resting) pair with a market-ready growout cohort (the move-in below), enqueue
        # it, and it is harvested a purge-cycle later — so the round-robin RE-STARTS and
        # harvest resumes. Without this the rotation stays frozen (returns every week) ->
        # ZERO harvest in purge mode -> the facility fills and TranOG arrivals can't be
        # placed. Winddown (no refill) or no fallow slot: as before, nothing to seed.
        if not refill or resting_pair is None:
            warnings.append(f"{week_label}: 6N purge queue empty — no harvest this week")
            return resting_pair
        harvest_pair = ()               # seed week: nothing to harvest this week
        fill_pair = resting_pair
        new_resting = next((p for p in SIXN_PAIRS
                            if p != fill_pair and pair_combined_count(state, p) == 0),
                           None)
    else:
        harvest_pair = pair_queue.pop(0)
        # RE-ENTRY to the 3-pair fallow rotation. When the forecast opens with
        # every pair stocked (no fallow slot) `resting_pair` is None and the
        # handler degrades to refill-in-place — and, without this, that None was
        # STICKY: `new_resting` below only re-arms when resting_pair is already
        # non-None, so the degrade outlived the condition that caused it for the
        # whole run. The startup warning promises "until a pair empties"; this is
        # what makes that true. Refill weeks only — in winddown there is no fill
        # to place, so adopting a fallow pair there would only reshuffle the
        # drain order.
        if resting_pair is None and refill:
            resting_pair = next(
                (p for p in SIXN_PAIRS
                 if p != harvest_pair and pair_combined_count(state, p) == 0),
                None)
            if resting_pair is not None:
                warnings.append(
                    f"{week_label}: 6N rotation RE-ENTERED the 3-pair fallow "
                    f"cycle — pair {resting_pair} is empty, so the Wed-fill/"
                    f"Fri-harvest split resumes (was refill-in-place)")
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
        # DEPURATION-HOLD fail-safe: never drain a tank on the rotation
        # immediately after its recorded fill (< SIXN_DRAIN_GUARD_MIN_DAYS —
        # that would be a 1-week purge; the standard 2nd-rotation drain is the
        # legal 2-week purge and always measures >= the threshold, partial
        # first forecast week included). With the fill-side avoidance in
        # _free_6n_slots this should never fire; if it does (e.g. a same-batch
        # top-up into a non-resting pair), the tank is HELD — its pair rests
        # next, and it drains on the next rotation with the hold satisfied.
        # Tanks with no recorded fill (PR-hydrated fish already purging at
        # forecast start) are treated as old enough.
        _fill_d = getattr(state, "sixn_fill_date", {}).get(tank_id)
        if (_fill_d is not None
                and (week_start_date - _fill_d).days < SIXN_DRAIN_GUARD_MIN_DAYS):
            warnings.append(
                f"{week_label}: DEPURATION HOLD — 6N {tank.location_id} "
                f"(batch {tank.batch_id}, {tank.count:.0f} fish) was filled "
                f"{(week_start_date - _fill_d).days}d ago (the rotation right "
                f"after its fill — a 1-week purge); drain held until the "
                f"pair's next rotation")
            continue
        # HARVEST LIMIT (relief semantics): never drain a tank past the weekly
        # processing limit (max_harvest_per_week) — HOLD it for the pair's
        # next rotation instead (its fill date is old, so the depuration guard
        # won't block that later drain, and sixn_level_drains shrinks the
        # pair's next fill so the combined drain stays level). Fills are
        # demand-sized and CAPPED at the limit, so this fires only when an
        # out-of-rotation make-room dump stacked a pair past it — the audited
        # 86,956-fish weeks. Deferral requires something already harvested
        # this week: a whole-week deferral would make an EMPTY week, and the
        # steady-harvest contract outranks the limit — such a week lands in
        # the EXCEPTIONAL relief band (limit .. limit*(1+harvest_relief_pct))
        # and the harvest gate counts every use.
        if (budget is not None and math.isfinite(budget.cap)
                and (budget.used > 0 or pair_drain_count > 0)
                and tank.count > budget.remaining()):
            warnings.append(
                f"{week_label}: HARVEST LIMIT — holding 6N "
                f"{tank.location_id} (batch {tank.batch_id}, "
                f"{tank.count:.0f} fish): draining it would exceed the "
                f"weekly processing limit ({budget.remaining():.0f} fish "
                f"left of {budget.cap:.0f}); drains next rotation")
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
        # Physical drain — never clamped, but recorded so the supplemental pass
        # below (and the rest of the week) sees the reduced remaining budget.
        if budget is not None:
            budget.record(ev.count)

    # 1b. NO supplemental direct-production harvest in purge mode (operator rule:
    # never harvest a production tank without purging it through 6N first). If the
    # harvested pair drains short of this week's target, that is by design — the
    # shortfall lets biomass BUILD toward the caps, and the predictive move-in
    # (sized to the controller's demand, below) tops up the pairs so the rotation
    # delivers the demand a purge-cycle later. All harvest flows through the 6N
    # pair drain (path 1 above); production fish reach harvest only via the move-in.

    # Wind-down (transition): harvest the front pair but do NOT restock — 6N
    # drains over the rotation while production harvest takes over via in-place
    # starvation. The resting pair stays empty; rotation continues so all pairs
    # drain in turn.
    if not refill:
        pair_queue.append(fill_pair)
        return new_resting

    # 2. Move-in target — Layer 2 demand 2 weeks ahead, clamped to
    #    [min_harvest_per_week, max_harvest_per_week]. The min clamp
    #    guarantees pair drains never fall below the operational floor
    #    when sufficient production inventory exists. (Computed BEFORE the
    #    source-batch pick so the entry forward-transit below knows the goal
    #    even when the grow-out pool is empty.)
    min_h = control.min_harvest_per_week or 0
    # RELIEF SEMANTICS (operator correction 2026-08-09): the fill is DEMAND-
    # driven (move_in_target from the controller) and CAPPED at the weekly
    # processing limit — max_harvest_per_week is a constraint the harvest
    # respects, never a level to size up to. The same limit gates the DRAIN
    # (the hold above); the relief band exists only for exceptional drains.
    max_h = float(control.max_harvest_per_week or 0) or min_h
    # NOTE (measured 2026-08-01, DO NOT RETRY without re-measuring): the floor
    # is judged at the DRAIN but sized HERE, and the fish carry ~lead weeks of
    # mortality between the two — so 12 of 19 sub-floor weeks land at exactly
    # 29,970 = 30,000 x 0.9995^2, thirty fish short by arithmetic. Grossing the
    # fill up by that survival factor is arithmetically correct AND makes the
    # plan worse: weeks below floor 21->17, but the worst week collapsed
    # 19,070->1,607 and weeks over the processing cap went 4->9. Asking for
    # thirty more fish per week cascades through tank selection and make-room
    # into a materially worse plan. The shortfall is real; this is not its fix.
    # FLOOR MORTALITY PAD (re-measured 2026-08-07, superseding the 2026-08-01
    # "do not retry" for THIS narrow form): a fill sized to exactly min_h
    # drains min_h x survival^(purge weeks) ~ 45 fish short two weeks later —
    # a permanent 29,955-vs-30,000 floor-miss class (8 weeks on the 7.17.26
    # PR). The old backfire grossed the whole target and cascaded through
    # WHOLE-TANK selection; this pads only the FLOOR CLAMP by ~0.2% and the
    # draws are partial (count-exact) takes, so no extra tank is pulled.
    _min_fill = min_h * _SIXN_FILL_MORTALITY_PAD
    if move_in_target is not None and move_in_target > 0:
        target = max(_min_fill, min(max_h, move_in_target))
    else:
        target = _min_fill
    # LEVEL DRAINS (opt-in `sixn_level_drains`): cap the fill by the resting pair's
    # REMAINING headroom (one weekly harvest's worth, max_h) so fills don't ACCUMULATE
    # into one pair across its rotation residency — the root cause of the 90-113k drain
    # spikes that starve OTHER pairs into sub-min troughs. Surplus stays in grow-out and
    # becomes the move-in for the next thin pair, lifting its drain toward the floor —
    # so every week meets the harvest MIN.
    if getattr(control, "sixn_level_drains", False):
        _existing = pair_combined_count(state, fill_pair)
        # NOTE (measured 2026-08-01): forcing the floor through this clamp —
        # "the contract must win" — BACKFIRES. It re-creates the accumulation
        # this clamp exists to prevent: weeks over the processing cap 4->6,
        # worst week 19,070->9,782, peak density 102.8->154.7. The clamp is
        # load-bearing; a sub-floor target here is a SYMPTOM of a full pair,
        # not the cause of the shortfall. Fix the fill, not the clamp.
        target = min(target, max(0.0, max_h - _existing))
    if target <= 0:
        pair_queue.append(fill_pair)
        return new_resting

    # 3. Pick FIFO move-in source batches (cascade list).
    move_in_batches = _pick_fifo_move_in_batches(state, batch_meta, control)
    # (An empty cascade list no longer early-returns: the FIFO loop below
    # simply moves nothing and the last-resort continuity ladder — entry
    # forward-transit + graded floor fill — still runs. Pre-rules the
    # rotation pulled/graded straight from entry tanks; a FIFO batch-
    # ripeness gap then left the fill EMPTY and the drain went dark two
    # weeks later — the measured 2026-W47 / 2027-W21 empty weeks on the
    # 7.17.26 PR.)

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
            and t.system_id not in OG12_SYSTEMS   # R5: no 6N staging from entry tier
            and t.avg_wt_g >= control.min_harvest_weight_g
        ]
        # Biggest avg_wt first: prefer to move the largest fish into the
        # pair (they'll be harvested in 2 weeks; we want big fish out).
        # VACATE-AWARE DRAW (operator, 2026-08-21). The priority order the
        # operator specified, outermost first:
        #   1. BATCH FIFO      — the enclosing `for move_in_batch` loop, fed by
        #                        _pick_fifo_move_in_batches. Untouched here;
        #                        this sort only ever orders ONE batch's tanks.
        #   2. HEAVIEST FIRST  — big fish out first; feeds the harvest weight
        #                        profile and the sales contract. Not negotiable.
        #   3. VACATE          — among tanks holding EFFECTIVELY THE SAME fish
        #                        (one 100 g band), draw the SMALLEST tank first
        #                        so it empties completely instead of leaving two
        #                        part-full tanks.
        # FIFO is broken only to MEET HARVEST CONSTRAINTS, and only by the
        # enclosing loop, which cascades to the next FIFO batch when the
        # oldest cannot fill the target.
        #
        # The handling is already paid for: these fish are moving to purge
        # regardless, so vacating a growout tank on the way costs nothing and
        # creates the headroom an over-dense OG1/2 tank needs. Same fish, same
        # week, same harvest — one more free tank.
        _BAND_G = 100.0
        src_tanks.sort(key=lambda t: (-int(t.avg_wt_g // _BAND_G), t.count,
                                      t.tank_id))
        moved_this_batch = 0
        for src in src_tanks:
            if count_moved >= target:
                break
            # PURGE move-in weight first — the per-tank fill cap is judged at
            # the mid-week (Friday) transfer weight the fish actually land at.
            _bm = batch_meta.get(move_in_batch)
            _xfer_wt = _grow_weight_days(src.avg_wt_g, _bm, tables,
                                         PURGE_TRANSFER_GROWTH_DAYS, week_label)
            # SISTER-FIRST FILL (rule-2 stage): allocate MAIN-first but never
            # STOCK a pair tank past the structural 95 kg/m3 fill cap —
            # overflow continues into the pair's other tank (the idle sister)
            # instead of overloading the main (audit: mains rode 128-141
            # while sisters sat empty 80-90% of purge weeks). A tank held by
            # a FOREIGN batch (depuration-held rider) contributes 0 capacity
            # (INV-1). Later contributor batches keep the historical
            # sister-first order so they never collide with the first batch's
            # main.
            def _dest_ok(tid, _b=move_in_batch):
                tk = state.tanks_by_id.get(tid)
                return tk is not None and (tk.is_empty or tk.batch_id == _b)
            _order = ((fill_pair[1], main_tank_id)
                      if (moved_this_batch == 0 and contributing_batches)
                      else (main_tank_id, fill_pair[1]))
            # purge mode -> unbounded per-tank fill, so the FIRST tank in
            # _order absorbs the whole take and the pair's other tank stays
            # free for a genuinely DIFFERENT batch (count fidelity at harvest).
            _purge_fill = is_purge_mode(control, week_start_date)
            _caps = [(tid, _sixn_fill_capacity_fish(state, tid, _xfer_wt,
                                                    purge=_purge_fill))
                     for tid in _order if _dest_ok(tid)]
            _caps = [(tid, c) for tid, c in _caps if c > 0]
            _cap_total = sum(c for _, c in _caps)
            if _cap_total <= 0:
                break   # pair at the 95 fill cap — surplus waits in grow-out
            # REMNANT FLOOR: never leave 0 < residue < min_tank_control behind.
            # A partial draw is reduced so the floor stays growing in the source
            # (a later tank/batch covers the difference); a source that can't
            # retain the floor is taken WHOLE — but a whole-tank escalation must
            # not blow the pair's 95-capacity, so it degrades to the
            # never-escalating partial form when it would.
            _want = min(target - count_moved, src.count, _cap_total)
            take = _floored_take(src.count, _want,
                                 control.min_tank_control or 0.0)
            if take > _cap_total + 0.5:
                take = _floored_partial(src.count, _want,
                                        control.min_tank_control or 0.0)
            if take <= 0:
                continue
            # Split the take across the pair, capped per tank; FREEZE each
            # destination (STARVE, no feed/growth for the rest of the purge)
            # and book the 4 pre-transfer feed-days once for the whole take.
            _allocs = []
            _rem = take
            for _tid, _c in _caps:
                _a = min(_rem, _c)
                if _a <= 0:
                    continue
                _allocs.append(TankAllocation(
                    tank_id=_tid, count=_a, avg_wt_g=_xfer_wt,
                    cv_pct=src.cv_pct))
                _rem -= _a
                if _rem <= 0:
                    break
            # SLIVER-LEG consolidation (handling budget): a split leg below the
            # operator's min-transfer size is a whole extra pumping setup for a
            # handful of fish (measured: a 208-fish leg into the near-full main
            # while 2,233 went to the sister). Fold such a leg into the pair's
            # other tank when its 95-cap headroom absorbs it — same total
            # move-in, one fewer tank-move. Capacity-bound splits keep both
            # legs (never trade the fill target away).
            _min_leg = float(getattr(control, "min_transfer_count", 0.0) or 0.0)
            if _min_leg > 0 and len(_allocs) > 1:
                _cap_by_tid = dict(_caps)
                _kept_allocs = []
                for _al in sorted(_allocs, key=lambda a: -a.count):
                    if _al.count < _min_leg and _kept_allocs:
                        _spill = _al.count
                        for _kb in _kept_allocs:
                            _head = _cap_by_tid.get(_kb.tank_id, 0.0) - _kb.count
                            _add = min(_spill, max(0.0, _head))
                            _kb.count += _add
                            _spill -= _add
                            if _spill <= 0:
                                break
                        if _spill > 0.5:
                            # No headroom elsewhere — the split is capacity-
                            # bound; keep the small leg after all.
                            _al.count = _spill
                            _kept_allocs.append(_al)
                    else:
                        _kept_allocs.append(_al)
                _allocs = _kept_allocs
            take = sum(a.count for a in _allocs)
            if take <= 0:
                continue
            ev = Transfer(
                batch_id=move_in_batch,
                event_date=week_start_date,
                source_tank_id=src.tank_id,
                destinations=_allocs,
                source_avg_wt_g=src.avg_wt_g,  # debit source at week-open weight
            )
            warns = ev.apply(state)
            warnings.extend(warns)
            transfer_events.append(ev)
            for _a in _allocs:
                _freeze_6n_dest(state, _a.tank_id, fill_date=week_start_date)
            _book_move_in_feed(sixn_move_in_feed, move_in_batch, week_label,
                               _xfer_wt, take, tables, _bm)
            count_moved += take
            moved_this_batch += take
        if moved_this_batch > 0:
            contributing_batches.append(move_in_batch)

    # FORWARD-TRANSIT (R2/R5): the rotation may not pull from ENTRY tanks
    # directly (R5 — no 6N staging from OG1/2), so when the grow-out pool
    # leaves the fill short while market-ready (or near-ready) fish sit in
    # entry tanks, route them the physical way: entry -> grow-out hop ->
    # fill pair. See _transit_entry_to_pair (whole-tank ripe transits to the
    # target + graded ripe-tail peels up to the floor).
    if count_moved < target:
        # Front of the (already-popped) queue drains NEXT week — the transit's
        # second-leg fallthrough must not land fish there (depuration hold).
        _avoid_imminent = frozenset(pair_queue[0]) if pair_queue else frozenset()
        count_moved += _transit_entry_to_pair(
            state, batch_meta, control, week_label, week_start_date,
            fill_pair, target, count_moved, transfer_events, grade_events,
            warnings, reserved, tables, sixn_move_in_feed,
            avoid=_avoid_imminent)

    # GRADE-TO-MIN floor fill: when the whole-tank move-in + entry transit leave
    # the resting pair below the harvest FLOOR (min_h), peel just enough of the
    # over-weight tail from near-market GROW-OUT tanks to REACH THE FLOOR — not
    # the controller's full move-in target. The small tail stays in the source
    # (no extra tank). Each peel is capped at the exact remaining shortfall, so
    # it harvests the least, gives up the least yield/biomass, and disturbs the
    # rotation the least. An EXCEPTION (fires only when below the floor), never
    # a rule; routes the bigs through 6N purge.
    # NOW UNCONDITIONAL (subsumes the old opt-in `harvest_grade_to_min`, which
    # remains accepted but no longer gates this): with the entry tier barred
    # from the rotation's direct pool (R5), this designed floor-exception is
    # the remaining legal feedstock during a FIFO batch-ripeness gap — leaving
    # it off produces empty harvest weeks, which breaks the operator's HARD
    # steady-harvest contract (worth more than the handling it saves).
    # Floor = padded floor, but NEVER past a level-drains-clamped target: when
    # sixn_level_drains cut the target below the floor the pair is nearly full
    # — that clamp is load-bearing (measured 2026-08-01) and grading past it
    # would re-create the accumulation spike it exists to prevent.
    _floor = min(_min_fill, target)
    if count_moved < _floor:
        for ptank in fill_pair:
            pt = state.tanks_by_id.get(ptank)
            if pt is None:
                continue
            # Successive peels: each call takes one source tank's tail; loop
            # until the floor is met or no candidate can contribute (the
            # refusal-aware return breaks cleanly on a cross-batch pickup).
            while count_moved < _floor:
                moved = _try_graded_move_in(
                    state, batch_meta, control, week_label, week_start_date,
                    (ptank,), transfer_events, warnings, reserved=reserved,
                    tables=tables, sixn_move_in_feed=sixn_move_in_feed,
                    retain_in_source=True, max_count=_floor - count_moved,
                )
                if moved <= 0:
                    break
                count_moved += moved

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
    min_keep: float = 0.0,
    moves_left=None,
) -> None:
    """Rebalance a batch's fish across its new tank set via Transfer events.

    Compute target_count = (total fish in prev_tanks) / len(this_tanks).
    Tanks with count > target send surplus to tanks with count < target
    via Transfer events. Sources (dropped tanks) are fully drained;
    dests (new tanks) start at zero and are filled up to target. Result:
    each this_tank ends at ~target count, eliminating density spikes
    from earlier consolidations or uneven hydrations.

    `moves_left` (optional callable -> int): the weekly handling budget.
    Draining SOURCES is essential — those tanks leave the batch's plan and
    another batch may claim them, so those moves are never blocked. The
    kept-tank EVENING moves (topping up under-target/new tanks from tanks
    the batch keeps) are QUALITY moves: once the budget is spent they stop,
    leaving the remaining deficit for the budgeted quality passes (even-out
    / balancer) to finish in calmer weeks. This is what spreads a rotation-
    week consolidation burst (measured: 7 same-week top-ups into one freed
    tank) across the following weeks instead of blowing the weekly cap.

    min_keep (min_tank_control): two remnant guards. (1) The even-split target
    itself must not be sub-min — when total/len(this_tanks) < min_keep, planned
    NEW destinations are dropped (never a kept/source tank) until each tank's
    share is at least the floor, so the plan can't fan a batch into remnant
    tanks. (2) Over->under pairing is TIER-LEGALITY-AWARE (R3/R4 via
    move_allowed): a pair the rules would certainly refuse is skipped at
    PLANNING time. Before this, the emitter planned e.g. growout->entry refills
    that Transfer.apply refused, while the entry tank's own OUTBOUND leg
    succeeded — stranding a sub-min remnant in the entry tank (the OG1S-16 /
    OG1N-15 operator finding).
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

    # REMNANT FLOOR guard (1): never PLAN a per-tank share below the operating
    # floor. Drop planned NEW destinations (highest tank_id first — the plan's
    # least-preferred pick) while the even-split share is sub-min; kept/source
    # tanks are never dropped (they hold fish the diff must still reconcile).
    this_eff = set(this_tanks)
    if min_keep > 0:
        while (len(this_eff) > 1 and total_count / len(this_eff)
               < min_keep * _REMNANT_KEEP_PAD):
            new_dests = sorted(this_eff - prev_tanks)
            if not new_dests:
                break
            this_eff.discard(new_dests[-1])
        if this_eff != set(this_tanks):
            dests = sorted(this_eff - prev_tanks)
            kept = sorted(prev_tanks & this_eff)

    target_per_tank = total_count / len(this_eff)

    # Build over/under lists. Source surpluses are ESSENTIAL (the plan gave
    # those tanks away — they must drain this week or the incoming batch
    # collides, INV-1); kept-tank surpluses are QUALITY evening moves and are
    # handled separately below (budget-gated via `moves_left`).
    overs: list[list] = []        # essential: [tank_id, surplus, tank_obj]
    overs_kept: list[list] = []   # quality evening, deferrable
    unders: list[list] = []       # [tank_id, deficit, tank_obj]
    for tid in kept:
        tank = state.tanks_by_id.get(tid)
        if tank is None:
            continue
        cur = tank.count if not tank.is_empty else 0.0
        if cur > target_per_tank + 0.5:
            overs_kept.append([tid, cur - target_per_tank, tank])
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
    overs_kept.sort(key=lambda x: -x[1])
    unders.sort(key=lambda x: -x[1])

    def _pair_legal(src_tank, dst_id) -> bool:
        """R2-R4 at PLANNING time: don't emit a transfer the tier rules will
        certainly refuse (the refused leg strands fish; see docstring)."""
        dst = state.tanks_by_id.get(dst_id)
        if dst is None:
            return False
        return move_allowed(src_tank.system_id, dst.system_id,
                            src_tank.avg_wt_g)[0]

    def _pair_surpluses(over_list: list[list], budgeted: bool) -> None:
        i = 0
        while i < len(over_list):
            if over_list[i][1] <= 0.5:
                i += 1; continue
            # HANDLING BUDGET: quality evening moves (kept-tank surpluses)
            # yield once the weekly budget is spent — the deficit stays and
            # the budgeted passes (even-out / balancer) resume the leveling
            # in a calmer week. Essential source drains never yield.
            if budgeted and moves_left is not None and moves_left() <= 0:
                return
            src_id, _, src_tank = over_list[i]
            j = next((k for k, u in enumerate(unders)
                      if u[1] > 0.5 and _pair_legal(src_tank, u[0])), None)
            if j is None:
                i += 1; continue   # no legal deficit for this source; residual below
            take = min(over_list[i][1], unders[j][1])
            dst_id = unders[j][0]
            # REMNANT FLOOR guard (2): a partial drain must leave the source empty
            # or >= min_keep — the take is REDUCED so the padded floor stays (the
            # deficit stays open for another surplus tank). The min() deliberately
            # caps the take-all escalation at the deficit: in the rare corner where
            # the source can't retain the floor AND its legal deficit can't absorb
            # the whole tank, the sub-min tail is left for the residual router
            # below (whole-tank move) and, failing that, the weekly remnant sweep.
            # A stricter in-loop version (skip the pairing, route whole) was tried
            # and REVERTED (2026-08-08): it measurably reshaped trajectories and
            # put an EMPTY harvest week back on the 7.2.26 PR — the hard
            # steady-harvest contract outranks a transient the sweep repairs a
            # week later.
            cur = src_tank.count if not src_tank.is_empty else 0.0
            if min_keep > 0 and cur > 0:
                take = min(take, _floored_take(cur, take, min_keep))
            if take <= 0.5:
                # Nothing movable for this pairing (floor guard zeroed it) —
                # advance; a zero-count Transfer is not a move and emitting one
                # would only spin the pairing loop.
                i += 1; continue
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
            over_list[i][1] -= take
            unders[j][1] -= take
            if over_list[i][1] < 0.5:
                i += 1

    _pair_surpluses(overs, budgeted=False)       # essential: dropped tanks drain
    _pair_surpluses(overs_kept, budgeted=True)   # quality: evening, budget-gated

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
            state.tanks_by_id[t] for t in this_eff
            if t in state.tanks_by_id
            and state.tanks_by_id[t].batch_id == batch_id
            # R2-R4: only route the residual to destinations the tier rules
            # allow from THIS source tank (no backward into entry, no
            # intra-entry at >= 1 kg) — Transfer.apply is the final gate.
            and move_allowed(tank.system_id,
                             state.tanks_by_id[t].system_id,
                             tank.avg_wt_g)[0]
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


def _consolidate_growout_to_free_tanks(
    state: FacilityState,
    event_date: date,
    transfer_events: list,
    warnings: list[str],
    need_tanks: int,
    max_moves: int,
    target_pct: float = 0.80,
) -> int:
    """VACATE growout tanks by consolidating each batch into fewer of its OWN
    tanks. Returns the number of tanks freed.

    WHY THIS AND NOT A REALLOCATION. An over-dense OG1/2 tank has a legal
    forward move (R2, any weight) but nowhere durable to put the fish: the
    facility runs at ~90% TANK occupancy (35.1 of 39 tanks, only 3.9 free in an
    average week, minimum 1) while sitting at ~62% of its water. Five measured
    attempts to relieve OG1/2 by moving fish into existing or empty growout
    tanks all RELOCATED the crowding instead of clearing it -- non-6N breaches
    56 -> 53 / 57 / 63 / 81. Taking one of the scarce free tanks simply denies
    it to whoever needed it a few weeks later; it is zero-sum.

    Consolidation is not zero-sum: batches run 4.95 growout tanks each, and
    packing one batch's own fish into fewer of its own tanks CREATES a free
    tank. Measured on the 8.13.26 workbook, in the 31 weeks with an OG1/2
    breach, consolidating to 80% of cap frees 2-6 tanks in 29 of them.

    80%, not 90%: a tank filled to its cap is back over within one week of
    growth (~4%/wk at 2-4 kg) -- the same defect this whole pass exists to fix.
    80% grows to ~83% and holds.

    LEGALITY. Growout->growout is unrestricted (R2/tiers.move_allowed), the
    move is within ONE batch so INV-1 cannot be violated, 6N is excluded
    (purge pipeline owns it), and STARVE tanks are never touched. Every move
    is a real `Transfer` applied to state and appended to `transfer_events`,
    so Transfer_Out/Transfer_In book and TankContinuityAudit reconciles --
    fish never change tanks without a recorded transfer.

    Only whole-tank vacates count: a partial move leaves the tank occupied and
    frees nothing, so it is skipped rather than emitted (handling for nothing).
    """
    if need_tanks <= 0 or max_moves <= 0:
        return 0
    bybatch: dict[str, list] = {}
    for t in state.tanks_by_id.values():
        if (not t.is_empty and t.type == "OG"
                and t.system_id not in OG12_SYSTEMS
                and t.system_id != "OG6N"
                and t.stage != STAGE_STARVE
                and t.max_density_kg_m3 > 0 and t.avg_wt_g > 0):
            bybatch.setdefault(t.batch_id, []).append(t)

    def _cap_fish(tk, wt):
        return tk.max_density_kg_m3 * tk.volume_m3 * 1000.0 / wt

    freed = 0
    moves = int(max_moves)
    # Widest-spread batches first: they hold the most redundant tanks.
    for bid, tanks in sorted(bybatch.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if freed >= need_tanks or moves <= 0:
            break
        if len(tanks) < 2:
            continue
        tanks.sort(key=lambda t: (t.count, t.tank_id))
        src, others = tanks[0], tanks[1:]
        if src.tank_id in getattr(state, "reserved_tanks", ()):
            continue
        allocs, rem = [], src.count
        for d in others:
            if rem <= 0.5:
                break
            take = min(rem, max(0.0, _cap_fish(d, d.avg_wt_g) * target_pct - d.count))
            if take <= 0.5:
                continue
            allocs.append(TankAllocation(tank_id=d.tank_id, count=take,
                                         avg_wt_g=src.avg_wt_g,
                                         cv_pct=src.cv_pct))
            rem -= take
        if rem > 0.5 or not allocs or len(allocs) > moves:
            continue                     # cannot FULLY vacate -> frees nothing
        ev = Transfer(batch_id=bid, event_date=event_date,
                      source_tank_id=src.tank_id, destinations=allocs,
                      leaves_source_empty=True)
        warnings.extend(ev.apply(state))
        transfer_events.append(ev)
        if ev.count_transferred > 0:
            freed += 1
            moves -= len(allocs)
    return freed


def _even_out_density(
    state: FacilityState,
    batch_id: str,
    event_date: date,
    transfer_events: list,
    warnings: list[str],
    max_moves: Optional[int] = None,
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
    # HANDLING BUDGET (rule 4): this is a deferrable quality pass — stop
    # emitting once the week's move budget is spent (None = unlimited).
    _mv_left = [max_moves if max_moves is not None else 10 ** 9]
    if _mv_left[0] <= 0:
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
        while i < len(overs) and j < len(unders) and _mv_left[0] > 0:
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
            if ev.count_transferred > 0:
                _mv_left[0] -= 1
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
    # RELIEF MARGIN: shed to 90% of cap, not to exactly 100%. Relieving to the
    # cap left zero headroom and the tank re-breached within one week of growth
    # -- instrumented on the 8.13.26 workbook, this pass drove OG1S-12 /
    # OG1N-15 / OG2N-25 to exactly 95.0 and they reported 98.6-98.8 at week
    # close, every week for months (B48 over cap 2026-11 -> 2027-03 while
    # growing 1.92 -> 3.53 kg). Moving fish that are under the cap is
    # legitimate when it makes room for what is coming (operator, 2026-08-21).
    RELIEF_PCT = 0.90
    # A tank freed by _consolidate_growout_to_free_tanks is EMPTY, so it is
    # available to ANY batch and is INV-1-safe by construction. Offered after
    # same-batch tanks (consolidating into an existing tank is cheaper
    # handling than opening a new one).
    _empty_go = [t for t in state.tanks_by_id.values()
                 if t.is_empty and t.type == "OG"
                 and t.system_id not in OG12_SYSTEMS
                 and t.system_id != "OG6N"
                 and t.max_density_kg_m3 > 0
                 and t.tank_id not in getattr(state, "reserved_tanks", ())]
    for src in og12_over:
        if src.avg_wt_g <= 0:
            continue
        for dst in list(og36_under) + sorted(_empty_go, key=lambda t: t.tank_id):
            _dst_wt = dst.avg_wt_g if not dst.is_empty else src.avg_wt_g
            if _dst_wt <= 0:
                continue
            src_cap_fish = (src.max_density_kg_m3 * src.volume_m3
                            * 1000.0 / src.avg_wt_g)
            dst_cap_fish = (dst.max_density_kg_m3 * dst.volume_m3
                            * 1000.0 / _dst_wt)
            shed = src.count - src_cap_fish * RELIEF_PCT
            room = dst_cap_fish * HEADROOM_PCT - dst.count
            take = min(shed, room)
            if take <= 0.5:
                continue
            if _mv_left[0] <= 0:
                return                    # handling budget spent
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
            if ev.count_transferred > 0:
                _mv_left[0] -= 1
            if src.count <= src_cap_fish * RELIEF_PCT + 0.5:
                break


def _realized_facility_metrics(
    state: FacilityState,
    batch_meta: dict[str, BatchInput],
    tables: BiologyTables,
    min_harvest_weight_g: float,
    week_label: Optional[str] = None,
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
    fac_sw_bio = 0.0   # feeding (SW grow-out) biomass only; off-feed STARVE excluded
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
            fac_sw_bio += bio
            sgr_eff = sgr_pct_per_day(t.avg_wt_g, "SW", batch, tables,
                                      week_label)
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
    return fac_bio, fac_growth_kg, fac_feed_kg_day, oldest_mature_avg_wt, fac_sw_bio


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


def _persist_tank_reserve(y, wl, until_wl, sorted_weeks, week_index, ta_index,
                          week_tank_owner, tank_assignments):
    """Strip tank y from EVERY batch's tank_assignments over [wl, until_wl).

    Used when the anticipatory purge pacing physically MOVES a growout tank's
    population into 6N and then HOLDS the slot empty for an imminent TranOG
    arrival. The fish that were in y have left the production layer, so y must
    not be scheduled to ANY batch during the hold window — otherwise the weekly
    assignment diff (or a later split/swap that the plan persisted before the
    reservation) would route a batch back into the held slot, defeating the hold
    (the B51-refills-reserved-tank leak). We clear y from every (batch, week)
    plan entry in the window and from week_tank_owner; from the arrival week
    (`until_wl`) on the plan is left untouched so the TranOG cohort can take it.
    """
    start = week_index.get(wl)
    if start is None:
        return
    window = set(w for w in sorted_weeks[start:] if w < until_wl)
    for a in tank_assignments:
        if a.week_label in window and y in a.tank_ids:
            a.tank_ids = [x for x in a.tank_ids if x != y]
    for w in window:
        owner_w = week_tank_owner.get(w)
        if owner_w is not None:
            owner_w.pop(y, None)


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
    reserved=frozenset(),
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
            t.avg_wt_g, t.biomass_kg, batch_meta.get(t.batch_id), tables, wl)
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
            # Destination systems legal from the OVER-CAP source system big_s
            # (R2-R4 via tiers.move_allowed): non-entry sources may never go
            # back to entry; entry sources <1 kg may target entry or growout,
            # >=1 kg forward (growout) only.
            elig = {s for s in og_systems
                    if move_allowed(big_s, s, bwt.get(b, 0.0))[0]}
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
                if (tk is not None and tk.is_empty and cand not in this_assignment
                        and cand not in reserved):  # held for an imminent TranOG
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
            # The split's fish flow OUT of the batch's current tanks (Phase D
            # evens them into the new tank), so the destination must be legal
            # from EVERY system the batch currently occupies (R2-R4): any
            # non-entry source tank forbids an entry-tier destination.
            _src_syss = {_tank_to_system_of(tid, og_tanks_by_system)
                         for tid in planned.get(bb, [])}
            _src_syss.discard(None)
            elig = {s for s in og_systems
                    if all(move_allowed(ss, s, bwt.get(bb, 0.0))[0]
                           for ss in _src_syss)}
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
                    if (tk is not None and tk.is_empty
                            and candy not in this_assignment
                            and candy not in reserved):  # held for imminent TranOG
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
    budget, min_transfer=0.0, min_keep=0.0,
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
                    t.avg_wt_g, t.biomass_kg, batch_meta.get(t.batch_id), tables,
                    wl)
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
                src.avg_wt_g, src.biomass_kg, batch_meta.get(bid), tables, wl)
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
                # R2-R4: the move goes from system S at the source tank's avg
                # weight; skip destinations the tier rules forbid (backward
                # into entry, or intra-entry at >=1 kg).
                if not move_allowed(S, T, src.avg_wt_g)[0]:
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
        # REMNANT FLOOR: never leave 0 < residue < min_tank_control in the
        # source. Reduced take only (never take-all — the move is bounded by
        # destination headroom); a reduced move that then falls under the
        # min_transfer floor is skipped entirely.
        move_count = _floored_partial(src.count, move_count, min_keep)
        # MIN-TRANSFER floor: don't split a sub-group smaller than min_transfer out
        # of a tank — a tiny partial move costs handling for marginal relief. This
        # is a PARTIAL move (leaves the source non-empty), so the floor applies;
        # whole-tank consolidation moves are emitted elsewhere. 0 = no floor.
        if move_count < max(1.0, min_transfer):
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
    level=False, reserved=frozenset(), min_transfer=0.0, min_keep=0.0,
):
    """Multi-objective balancer: cut out-of-bounds across per-tank DENSITY,
    per-system FEED, and per-system BIOMASS *together*.

    For each over-dense tank (worst first), move just enough surplus fish
    (conserved Transfer) into the best destination — an under-cap tank of the
    SAME batch, or an empty eligible tank — chosen by headroom in ALL THREE
    dimensions (system biomass, system feed, destination density). The move is
    capped by every dimension's headroom, so relieving a hot tank can never push
    a destination over its feed/biomass/density cap (the trap the naive density
    split fell into). Eligibility follows tiers.move_allowed (R2-R4: non-entry
    never back to entry; entry <1 kg → any OG, >=1 kg → forward only);
    Transfer.apply is the final INV gate. New tanks are
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
                            t.avg_wt_g, t.biomass_kg, batch_meta.get(t.batch_id),
                            tables, wl)
        return sb, sf

    moves = 0
    stuck: set = set()
    for _ in range(budget):
        sb, sf = loads()
        # LEVEL mode: per-system utilization = max(biomass, feed) vs cap (the
        # binding constraint), so the balancer fires on a system over ANY of its
        # caps, not only on tank density. Off => density-only, byte-identical.
        sys_u = {}
        if level:
            for s in og_tanks_by_system:
                bc, fc = cap_lookup(wl, s)
                sys_u[s] = max((sb[s] / bc) if bc else 0.0,
                               (sf[s] / fc) if fc else 0.0)
        worst = None
        worst_key = None
        worst_sys = None
        for s, ids in og_tanks_by_system.items():
            su = sys_u.get(s, 0.0)
            for tid in ids:
                if tid in stuck:
                    continue
                t = state.tanks_by_id.get(tid)
                if (t is None or t.is_empty or t.max_density_kg_m3 <= 0
                        or t.stage == STAGE_STARVE):   # don't relieve purge tanks
                    continue
                ratio = t.density_kg_m3 / t.max_density_kg_m3
                if level:
                    # Relieve the hottest system (density OR system biomass/feed
                    # over cap); among its tanks prefer the biggest fish (most
                    # eligible to flow downstream). Deterministic via tank_id.
                    pressure = ratio if ratio >= su else su
                    if pressure <= _BALANCE_TRIGGER_FRAC:
                        continue
                    key = (pressure, t.avg_wt_g, t.tank_id)
                else:
                    if ratio <= _BALANCE_TRIGGER_FRAC:
                        continue
                    key = ratio
                if worst is None or key > worst_key:
                    worst = t
                    worst_key = key
                    worst_sys = s
        if worst is None:
            break
        src = worst
        b = src.batch_id
        if src.avg_wt_g <= 0:
            stuck.add(src.tank_id)
            continue
        intensity = (realized_feed_kg_day(
            src.avg_wt_g, src.biomass_kg, batch_meta.get(b), tables, wl)
            / src.biomass_kg) if src.biomass_kg > 0 else 0.0
        surplus_kg = src.biomass_kg - src.max_biomass_kg * _BALANCE_TARGET_FRAC
        if level:
            # Relieve the hot system's BINDING cap (biomass and/or feed) to
            # _BALANCE_SYS_FILL of cap — whichever is over.
            _bc, _fc = cap_lookup(wl, worst_sys)
            if _bc and sb[worst_sys] > _bc:
                surplus_kg = max(surplus_kg, sb[worst_sys] - _bc * _BALANCE_SYS_FILL)
            if _fc and intensity > 0 and sf[worst_sys] > _fc:
                surplus_kg = max(surplus_kg,
                                 (sf[worst_sys] - _fc * _BALANCE_SYS_FILL) / intensity)
        if surplus_kg <= 1.0:
            stuck.add(src.tank_id)
            continue
        # Candidate destinations with per-dimension headroom. Eligibility =
        # tiers.move_allowed from the SOURCE tank's system at its avg weight
        # (R2-R4): non-entry never back to entry; entry <1 kg anywhere, >=1 kg
        # forward only.
        cands = []
        for s2, ids in og_tanks_by_system.items():
            eligible = (s2 in og_systems
                        and move_allowed(src.system_id, s2, src.avg_wt_g)[0])
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
                elif t2.is_empty and tid2 not in reserved:  # held for imminent TranOG
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
        if level:
            # BALANCE, not concentrate: send the load to the COLDEST eligible
            # system (lowest utilization) so it spreads across the facility,
            # rather than filling the single most-headroom tank. c[3] = dest
            # system; tiebreak most-headroom then existing-tank.
            cands.sort(key=lambda c: (sys_u.get(c[3], 0.0), -c[0], c[1]))
        else:
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
        # REMNANT FLOOR (see _variable_quantity_rebalance): reduce the take so
        # the source keeps >= min_tank_control, or skip.
        move_count = _floored_partial(src.count, move_count, min_keep)
        # MIN-TRANSFER floor (see _variable_quantity_rebalance): skip a partial
        # split smaller than min_transfer fish. 0 = no floor.
        if move_count < max(1.0, min_transfer):
            stuck.add(src.tank_id)
            continue
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


def _repair_over_cap_systems(
    state, wl, event_date, transfer_events, warnings,
    cap_lookup, batch_meta, tables, og_systems, og_tanks_by_system, budget,
    reserved=frozenset(), min_transfer=0.0, min_keep=0.0,
):
    """END-OF-WEEK cap repair — see the `_REPAIR_*` block above for WHY.

    Runs on the post-growth state the SystemLimitsAudit actually measures.
    While any OG system is over its RAW per-system biomass or feed cap, move the
    MINIMUM mass out of the hottest one into the COLDEST legal system, one
    conserved partial Transfer per move, until the budget runs out or nothing
    legal is left.

    Every business rule is honoured, and none of them is relaxed here:
      * tiers.move_allowed (R2-R4) from the SOURCE tank's system at its avg
        weight — no backward move into the entry tier, no intra-entry split at
        >= 1 kg; R7 (6N depuration one-way) via the STARVE skips below and
        `Transfer.apply`'s own gate;
      * one batch per tank — the destination is a tank ALREADY holding the
        source's batch (see the footprint-neutral note above: an empty tank is
        never claimed either, so the tank SET is unchanged and no forward plan
        edit is needed);
      * per-tank density — a destination tank is filled only to
        _REPAIR_DENS_FILL of its density cap, and `Transfer.apply` is the final
        INV gate;
      * remnant floor (`min_keep` = min_tank_control) via `_floored_partial`,
        and the min-transfer floor (`min_transfer`) — this pass only ever emits
        PARTIAL moves, so it can never strand a sub-floor residue or empty a
        source;
      * TranOG holds (`reserved`) and 6N-pipeline tanks are never touched;
      * the handling budget — `budget` is what the caller has left, and this
        pass is deferrable by construction (it emits nothing when budget <= 0).

    Returns the number of moves made. Never raises; a refused Transfer just
    marks that source stuck and the loop moves on.
    """
    if budget <= 0:
        return 0
    sixn = SIXN_MAIN_TANKS | SIXN_SISTER_TANKS

    def loads():
        sb = collections.defaultdict(float)
        sf = collections.defaultdict(float)
        for s, ids in og_tanks_by_system.items():
            for tid in ids:
                t = state.tanks_by_id.get(tid)
                if t is None or t.is_empty:
                    continue
                sb[s] += t.biomass_kg            # STARVE biomass counts to caps
                if t.stage != STAGE_STARVE:      # but STARVE fish eat nothing
                    sf[s] += realized_feed_kg_day(
                        t.avg_wt_g, t.biomass_kg, batch_meta.get(t.batch_id),
                        tables, wl)
        return sb, sf

    moves = 0
    stuck: set = set()                            # source tank ids that can't move
    for _ in range(int(budget)):
        sb, sf = loads()
        ratio = {}
        for s in og_tanks_by_system:
            bc, fc = cap_lookup(wl, s)
            ratio[s] = max((sb[s] / bc) if bc else 0.0,
                           (sf[s] / fc) if fc else 0.0)
        # Hottest OVER-CAP system first (deterministic tiebreak by system id).
        over = sorted(((r, s) for s, r in ratio.items()
                       if r > 1.0 and s in og_systems),
                      key=lambda x: (-x[0], x[1]))
        if not over:
            break
        placed = False
        for _r, S in over:
            bc, fc = cap_lookup(wl, S)
            # Mass to shed to reach _REPAIR_SRC_FILL of the BINDING cap.
            need_bio = (sb[S] - bc * _REPAIR_SRC_FILL) if bc else 0.0
            over_feed = (sf[S] - fc * _REPAIR_SRC_FILL) if fc else 0.0
            best = None
            for tid in sorted(og_tanks_by_system.get(S, [])):
                if tid in stuck:
                    continue
                src = state.tanks_by_id.get(tid)
                if (src is None or src.is_empty or src.stage == STAGE_STARVE
                        or src.avg_wt_g <= 0 or src.biomass_kg <= 1.0):
                    continue
                b = src.batch_id
                src_feed = realized_feed_kg_day(
                    src.avg_wt_g, src.biomass_kg, batch_meta.get(b), tables, wl)
                intensity = (src_feed / src.biomass_kg
                             if src.biomass_kg > 0 else 0.0)
                want = need_bio
                if over_feed > 0 and intensity > 0:
                    want = max(want, over_feed / intensity)
                # Lift a too-small shave up to the min-transfer floor (a move
                # below it would be refused outright) — still bounded by every
                # headroom below, so this can never overshoot a destination.
                if min_transfer > 0:
                    want = max(want, min_transfer * src.avg_wt_g / 1000.0)
                if want <= 1.0:
                    continue
                for T in sorted(og_tanks_by_system):
                    if T == S or T not in og_systems:
                        continue
                    if not move_allowed(S, T, src.avg_wt_g)[0]:
                        continue
                    tbc, tfc = cap_lookup(wl, T)
                    bio_head = (tbc * _REPAIR_DST_FILL - sb[T]) if tbc else 1e18
                    feed_head = (tfc * _REPAIR_DST_FILL - sf[T]) if tfc else 1e18
                    if bio_head <= 0 or feed_head <= 0:
                        continue
                    for tid2 in sorted(og_tanks_by_system.get(T, [])):
                        if tid2 in sixn or tid2 in reserved:
                            continue
                        t2 = state.tanks_by_id.get(tid2)
                        if t2 is None or t2.stage == STAGE_STARVE:
                            continue
                        # FOOTPRINT-NEUTRAL (see _REPAIR_* above): top up a tank
                        # the batch already holds. An empty tank is never
                        # claimed (that starves the harvest controller's free
                        # pool) and a tank holding ANOTHER batch never can be
                        # (one batch per tank).
                        if t2.is_empty or t2.batch_id != b:
                            continue
                        cap_kg = (t2.max_biomass_kg * _REPAIR_DENS_FILL
                                  if t2.max_density_kg_m3 > 0 else 1e18)
                        dens_head = cap_kg - t2.biomass_kg
                        if dens_head <= 0:
                            continue
                        move_kg = min(want, bio_head, dens_head,
                                      src.biomass_kg * 0.95)
                        if intensity > 0:
                            move_kg = min(move_kg, feed_head / intensity)
                        if move_kg <= 1.0:
                            continue
                        # COLDEST legal system first (spread, don't concentrate);
                        # then the BIGGER relief; then tank id for determinism.
                        key = (round(ratio.get(T, 0.0), 6), -move_kg, tid2)
                        if best is None or key < best[0]:
                            best = (key, src, t2, move_kg, T)
            if best is None:
                # Nothing legal for this system this round — don't retry it
                # while its tanks are unchanged; try the next-hottest.
                continue
            _key, src, dst, move_kg, T = best
            move_count = move_kg / (src.avg_wt_g / 1000.0)
            move_count = _floored_partial(src.count, move_count, min_keep)
            if move_count < max(1.0, min_transfer):
                stuck.add(src.tank_id)
                continue
            before = src.biomass_kg
            ev = Transfer(
                batch_id=src.batch_id, event_date=event_date,
                source_tank_id=src.tank_id,
                destinations=[TankAllocation(
                    tank_id=dst.tank_id, count=move_count,
                    avg_wt_g=src.avg_wt_g, cv_pct=src.cv_pct,
                )],
                leaves_source_empty=False,
            )
            warnings.extend(ev.apply(state))
            transfer_events.append(ev)
            if src.biomass_kg >= before - 1.0:     # refused by the INV gate
                stuck.add(src.tank_id)
                continue
            moves += 1
            placed = True
            break
        if not placed:
            break                                  # no legal move anywhere
    return moves


def _consolidate_remnants(
    state: FacilityState,
    event_date: date,
    week_label: str,
    transfer_events: list,
    warnings: list[str],
    min_keep: float,
    max_moves: Optional[int] = None,
) -> int:
    """Weekly remnant sweep (INV-5 repair): fold any occupied grow-out/entry
    tank holding 0 < count < min_keep into its OWN batch's other tanks.

    The emitter-side floors (_floored_take/_floored_partial + the diff
    emitter's guards) stop remnants being CREATED; this sweep is the systemic
    net for the two remaining sources — mortality attrition eroding a tank
    below the floor over time, and a refused transfer stranding a tail. Rules:

      - SAME-BATCH TOP-UP only (INV-1: one batch per tank; relocating the
        remnant to an empty tank would just move the problem).
      - Tier-legal destinations only (R2-R4 via move_allowed; entry sources
        prefer FORWARD grow-out destinations, and intra-entry only below the
        1 kg lock — move_allowed enforces).
      - 6N depuration + STARVE (in-place purge) tanks are pipeline-owned:
        neither swept as sources (their small counts are transient staging,
        drained at harvest) nor topped up as destinations.
      - Fold only when the batch's other tanks can absorb the WHOLE remnant
        within density headroom — a partial fold would leave a smaller
        remnant, defeating the point.

    A remnant whose batch has no absorbing tank (e.g. the batch's total
    remainder is itself < min_keep, living alone) legitimately stays; the
    abort-time _consolidate_entry_forward remains the last-resort backstop.
    Returns the number of tanks folded (emptied).
    """
    if min_keep <= 0:
        return 0
    # HANDLING BUDGET (rule 4): deferrable pass — a remnant left unswept this
    # week is swept on a calmer one (the emitters stop new ones forming).
    _mv_left = max_moves if max_moves is not None else 10 ** 9
    folds = 0
    remnants = sorted(
        [t for t in state.tanks_by_id.values()
         if not t.is_empty and t.type == "OG"
         and t.system_id not in _SIXN_SYSTEMS
         and t.stage != STAGE_STARVE
         and t.avg_wt_g > 0
         and 0 < t.count < min_keep * _REMNANT_SWEEP_PAD],
        key=lambda t: (t.count, t.tank_id))
    for src in remnants:
        if src.is_empty:
            continue
        if _mv_left <= 0:
            break                        # handling budget spent — defer
        cands = []
        for d in state.tanks_by_id.values():
            if (d.tank_id == src.tank_id or d.is_empty
                    or d.batch_id != src.batch_id or d.type != "OG"
                    or d.system_id in _SIXN_SYSTEMS
                    or d.stage == STAGE_STARVE
                    or d.max_density_kg_m3 <= 0):
                continue
            if not move_allowed(src.system_id, d.system_id, src.avg_wt_g)[0]:
                continue
            head_kg = d.max_biomass_kg * 0.98 - d.biomass_kg
            if head_kg <= 0:
                continue
            cands.append((d, head_kg / (src.avg_wt_g / 1000.0)))
        # Forward-first (out of the entry tier), then most headroom (fewest
        # destination hops), tank_id for determinism.
        cands.sort(key=lambda c: (c[0].system_id in OG12_SYSTEMS, -c[1],
                                  c[0].tank_id))
        if not cands or sum(r for _, r in cands) < src.count:
            continue   # can't absorb the whole remnant — leave it intact
        allocs, left = [], src.count
        for d, room in cands:
            take = min(left, room)
            if take <= 0.5:
                break
            allocs.append(TankAllocation(
                tank_id=d.tank_id, count=take,
                avg_wt_g=src.avg_wt_g, cv_pct=src.cv_pct))
            left -= take
        _sb, _sl, _sn = src.batch_id, src.location_id, src.count
        mv = Transfer(
            batch_id=_sb, event_date=event_date,
            source_tank_id=src.tank_id, destinations=allocs,
            leaves_source_empty=True)
        warnings.extend(mv.apply(state))
        transfer_events.append(mv)
        if mv.count_transferred > 0:
            _mv_left -= 1
        if state.tanks_by_id[src.tank_id].is_empty:
            folds += 1
            warnings.append(
                f"{week_label}: REMNANT SWEEP — folded {_sl} (batch {_sb}, "
                f"{_sn:.0f} fish < min_tank_control {min_keep:.0f}) into its "
                f"own batch's tank(s) "
                f"{[a.tank_id for a in allocs]} (same-batch top-up; frees the "
                f"tank + feed line)")
    return folds


def _free_production_stage_tank(state: FacilityState, reserved=frozenset()):
    """Lowest-id empty OG tank a PRODUCTION-mode pass may stage for in-place
    purge (the forward-promotion and graded-stage destinations).

    Three exclusions, all of which the sibling destination filters in the
    weekly walk already apply — this helper exists so the two production
    staging sites cannot drift apart again:

      * R5 — never the ENTRY tier (OG1/2): entry fish route forward first.
      * DESIGN §5 / sixn.py — never a 6N SISTER (67/69/71). In production
        mode ONLY the mains 61/63/65 become ordinary grow-out; the sisters
        are harvest-staging tanks that are not production capacity. Staging
        into one silently gave the production facility a tank that does not
        exist (measured on the 7.29.26 PR: OG6N-67 took a promoted entry
        tank at 2028-W13 and graded-stage tails at 2028-W20/W27).
      * A tank RESERVED for an imminent TranOG arrival.

    Returns the TankState, or None when nothing is free.
    """
    return next(
        (t for t in sorted(state.tanks_by_id.values(), key=lambda x: x.tank_id)
         if t.is_empty and t.type == "OG"
         and t.system_id not in OG12_SYSTEMS
         and t.tank_id not in SIXN_SISTER_TANKS
         and t.tank_id not in reserved),
        None)


def _free_6n_slots(state: FacilityState, resting_pair,
                   avoid=frozenset(), same_batch=None,
                   fill_date=None) -> list[int]:
    """6N tank ids that can ACCEPT a make-room move-in right now.

    A 6N tank is available if it is empty (any pair). The resting pair's
    main/sister come first (the Wed-fill slot the rotation refills), then
    any other empty 6N tank — so a make-room move-in prefers the slot the
    pipeline is about to fill anyway, and only spills onto extra empties
    when that one is taken.

    `avoid` — tank ids whose pair DRAINS next rotation: offered LAST, never
    first. Pre-fix these were the first fallthrough slots (the audited
    1-week-residency leak: 33,206 fish into the front pair's empty sister),
    so a dump landed there even when a legal slot existed. Demoting instead
    of excluding keeps make-room's success/failure identical to the old
    behaviour (minimal trajectory divergence — the PR_CORRECTION trial
    evaluator re-runs whole placements, so a refusal here reshapes entire
    plans); when the last-resort slot IS used, the rotation's drain guard
    holds that tank through its full purge, so the hold still cannot leak.
    """
    # TOP-UP FIRST (operator, 2026-08-20): one batch belongs in ONE tank. A
    # 6N tank already holding THIS batch, filled THIS SAME event date, is
    # offered ahead of any empty slot, so a second source tank of the same
    # batch moving in the same week tops that tank up instead of consuming a
    # fresh slot -- which was spending the pair's sister on a single batch and
    # leaving nowhere to separate a genuinely different batch at harvest.
    #
    # SAME fill_date is required, never merely the same batch: a tank filled in
    # an EARLIER week is partway through its ~2-week purge, and adding fish to
    # it would hand the newcomers a short clock when that tank drains. Same-day
    # top-up gives both tranches the identical purge window.
    topup: list[int] = []
    if same_batch is not None and fill_date is not None:
        _fd = getattr(state, "sixn_fill_date", {}) or {}
        for tid in sorted(SIXN_MAIN_TANKS | SIXN_SISTER_TANKS):
            t = state.tanks_by_id.get(tid)
            if (t is not None and not t.is_empty and tid not in avoid
                    and t.batch_id == same_batch
                    and _fd.get(tid) == fill_date):
                topup.append(tid)
    pref: list[int] = []
    last: list[int] = []
    for tid in list(resting_pair or ()):
        t = state.tanks_by_id.get(tid)
        if t is not None and t.is_empty and tid not in pref and tid not in last:
            (last if tid in avoid else pref).append(tid)
    for tid in sorted(SIXN_MAIN_TANKS | SIXN_SISTER_TANKS):
        t = state.tanks_by_id.get(tid)
        if t is not None and t.is_empty and tid not in pref and tid not in last:
            (last if tid in avoid else pref).append(tid)
    return topup + pref + last


def _freeze_6n_dest(state: FacilityState, dest_tank_id: int,
                    fill_date=None) -> None:
    """Freeze a 6N depuration destination after a purge-mode move-in.

    Sets the tank to STARVE so the daily biology loop neither grows nor feeds
    it for the rest of the ~2-week purge; it is harvested at the frozen entry
    weight (the mid-week transfer weight the move-in event already placed —
    source week-open avg grown PURGE_TRANSFER_GROWTH_DAYS). Only the 6N pipeline
    tanks are touched. No-op if the tank is empty (a fully refused transfer) so
    we never flag an empty tank as starving.

    `fill_date` (when given) is recorded in state.sixn_fill_date — the
    depuration-hold ledger the rotation's drain guard reads, so every fill
    path that freezes a 6N tank stamps its residency clock at this single
    chokepoint.
    """
    t = state.tanks_by_id.get(dest_tank_id)
    if t is not None and not t.is_empty and t.system_id in _SIXN_SYSTEMS:
        t.stage = STAGE_STARVE
        if fill_date is not None:
            getattr(state, "sixn_fill_date", {})[dest_tank_id] = fill_date


def _book_move_in_feed(accum: dict, batch_id: str, week_label: str,
                       transfer_avg_wt_g: float, moved_count: float,
                       tables: BiologyTables, batch: Optional[BatchInput]) -> None:
    """Record the 4 pre-transfer feed-days for a 6N purge move-in cohort.

    The cohort fed normally for PURGE_TRANSFER_GROWTH_DAYS in the source tank
    before the mid-week transfer, then goes off-feed (STARVE) in 6N. The feed
    reports exclude the STARVE 6N tank-weeks, so those 4 days would otherwise be
    lost; book them here keyed by (batch, move-in week, feed-type-at-transfer-wt)
    for the feed writers to add back in. moved_biomass uses the TRANSFER weight.
    """
    if accum is None or moved_count <= 0 or transfer_avg_wt_g <= 0:
        return
    moved_kg = moved_count * transfer_avg_wt_g / 1000.0
    feed_kg = (realized_feed_kg_day(transfer_avg_wt_g, moved_kg, batch, tables,
                                    week_label)
               * PURGE_TRANSFER_GROWTH_DAYS)
    if feed_kg <= 0:
        return
    ftype = _feed_type_for_size(tables, transfer_avg_wt_g)
    accum[(batch_id, week_label, ftype)] = (
        accum.get((batch_id, week_label, ftype), 0.0) + feed_kg)


def _make_room_into_6n(
    state: FacilityState,
    src,
    event_date: date,
    resting_pair,
    transfer_events: list,
    warnings: list[str],
    week_label: str,
    reason: str,
    sixn_move_in_feed: Optional[dict] = None,
    tables: Optional[BiologyTables] = None,
    batch_meta: Optional[dict] = None,
    is_purge: bool = False,
    avoid=frozenset(),
) -> bool:
    """PURGE-mode make-room: MOVE one growout tank's fish into a free 6N tank.

    The operator rule is absolute in purge mode — never harvest a production
    tank directly. To free a growout tank we move its whole population into a
    6N depuration tank, which both vacates the growout slot AND routes the fish
    to harvest via the rolling pair rotation. Returns True if the move was made,
    False if no 6N slot is free (a real 6N-capacity signal, never a bypass).

    Used by BOTH the anticipatory pacing pass (run in the weeks BEFORE a known
    TranOG arrival) and the reactive arrival-week make-room (the backstop).

    `avoid` — 6N tanks of the pair draining next rotation; used only as the
    LAST-RESORT destination (when no other slot is free), in which case the
    rotation's drain guard holds the tank through its full purge.
    """
    _usable = []
    for _tid in _free_6n_slots(state, resting_pair, avoid,
                               same_batch=(src.batch_id if is_purge else None),
                               fill_date=(event_date if is_purge else None)):
        _tk = state.tanks_by_id.get(_tid)
        # Empty slot, OR a 6N tank already holding this same batch (top-up,
        # INV-1-safe). Empty is the common case here.
        if _tk is not None and (_tk.is_empty or _tk.batch_id == src.batch_id):
            _usable.append(_tk)
    if not _usable:
        return False
    # PURGE move-in: land at the mid-week (Friday) transfer weight (week-open
    # avg grown 4 SW days) and FREEZE each 6N destination so it neither grows
    # nor feeds for the rest of the purge. Book the 4 pre-transfer feed-days.
    # In production mode (is_purge False) keep the legacy week-open weight and
    # SW stage — unchanged behaviour.
    _bm = (batch_meta or {}).get(src.batch_id)
    _xfer_wt = (_grow_weight_days(src.avg_wt_g, _bm, tables,
                                  PURGE_TRANSFER_GROWTH_DAYS, week_label)
                if (is_purge and tables is not None) else src.avg_wt_g)
    # Capture BEFORE apply: leaves_source_empty drains src, so reading
    # src.batch_id/count afterwards logs "batch None, 0 fish".
    _src_batch, _src_loc, _src_count = src.batch_id, src.location_id, src.count
    # SISTER-FIRST FILL (rule-2 stage): a whole-tank dump is SPLIT across the
    # usable slots so no 6N tank is stocked past the structural 95 kg/m3 fill
    # cap — the overflow lands in the next slot (the pair's idle sister)
    # instead of riding one main to 128-141. The move must fully vacate the
    # source (no-drop), so when total capacity is short the LAST slot takes
    # the remainder anyway — an overloaded purge tank beats a dropped arrival.
    _allocs = []
    _rem = _src_count
    for _i, _tk in enumerate(_usable):
        if _rem <= 0:
            break
        _cap = _sixn_fill_capacity_fish(state, _tk.tank_id, _xfer_wt,
                                        purge=is_purge)
        _a = _rem if _i == len(_usable) - 1 else min(_rem, _cap)
        if _a <= 0:
            continue
        _allocs.append(TankAllocation(tank_id=_tk.tank_id, count=_a,
                                      avg_wt_g=_xfer_wt, cv_pct=src.cv_pct))
        _rem -= _a
    if _rem > 0:                    # every slot at 0 capacity and none last?
        _allocs.append(TankAllocation(tank_id=_usable[-1].tank_id, count=_rem,
                                      avg_wt_g=_xfer_wt, cv_pct=src.cv_pct))
        _rem = 0
    _mv = Transfer(
        batch_id=_src_batch, event_date=event_date,
        source_tank_id=src.tank_id,
        destinations=_allocs,
        leaves_source_empty=True,
        # In purge mode debit the source at its week-open weight (the
        # dest carries the grown transfer weight); in production mode
        # both are week-open so this is the same value (no-op).
        source_avg_wt_g=(src.avg_wt_g if is_purge else None),
    )
    warnings.extend(_mv.apply(state))
    transfer_events.append(_mv)
    if is_purge:
        for _a in _allocs:
            _freeze_6n_dest(state, _a.tank_id, fill_date=event_date)
        if sixn_move_in_feed is not None and tables is not None:
            _book_move_in_feed(sixn_move_in_feed, _src_batch,
                               week_label, _xfer_wt, _src_count,
                               tables, _bm)
    _dest_desc = " + ".join(
        f"{state.tanks_by_id[_a.tank_id].location_id} ({_a.count:.0f})"
        for _a in _allocs)
    warnings.append(
        f"{week_label}: {reason} — MOVED {_src_loc} "
        f"(batch {_src_batch}, {_src_count:.0f} fish) into 6N "
        f"{_dest_desc} to purge (no direct harvest in purge mode)")
    return True


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
    fw_biomass_by_week: Optional[dict] = None,
    fw_feed_by_week: Optional[dict] = None,
    harvest_guide=None,
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
    # 6N purge-mode pre-transfer feed accumulator (see PlacementResult).
    sixn_move_in_feed: dict[tuple[str, str, str], float] = {}
    # Realized biology per (tank_id, week_label, batch_id) -> [bio_delta_kg,
    # mort_count]: the actual growth-minus-mortality biomass change and the
    # mortality count the daily walker applied, so the continuity audit can
    # reconcile against ground truth (see write_tank_continuity_audit).
    from collections import defaultdict as _dd
    realized_biology: dict[tuple[int, str, str], list] = _dd(lambda: [0.0, 0.0])
    if facility_limits is None:
        facility_limits = FacilityLimits()

    # Quick lookups.
    load_by_bw: dict[tuple[str, str], BatchWeekLoad] = {
        (l.batch_id, l.week_label): l for l in load_table
    }
    tank_by_bw: dict[tuple[str, str], list[int]] = {
        (a.batch_id, a.week_label): a.tank_ids for a in tank_assignments
    }
    # NOTE (audit H4): the realized harvest is driven CLOSED-LOOP off realized tank
    # biomass below (the dual-limit setpoint), NOT off the Layer-2 HarvestDemand
    # list. The scheduler's per-week demand is advisory/diagnostic only — it shaped
    # the Phase-A precalc load footprint upstream, but is not consumed here. A
    # former per-week demand aggregation lived here and was dead code; removed.
    splits_by_batch = {s.batch_id: s for s in splits}

    # TranOG arrival schedule (kg of biomass entering OG per ISO week). Each
    # split's post-cull population lands in the facility on its OG-entry week
    # (the first week boundary on/after TranOG_Date — NOT the raw date, which
    # falls in the prior transit week; see og_entry_week_start) — a KNOWN
    # disturbance the closed-loop harvest controller feeds forward so it can
    # pre-draw biomass down before the batch arrives (see
    # caps.predictive_move_in_count). Approximate; the predictive feedback
    # re-corrects against realized state each week.
    arrivals_by_week: dict[str, float] = {}
    for s in splits:
        if s.tran_og_date is None or s.post_cull_count <= 0:
            continue
        wk = iso_week_label(
            og_entry_week_start(_as_date(s.tran_og_date), initial_state.today))
        arrivals_by_week[wk] = arrivals_by_week.get(wk, 0.0) + (
            s.post_cull_count * s.post_cull_avg_wt_g / 1000.0)

    # ANTICIPATORY PURGE PACING — per-arrival-week growout-tank demand. The
    # TranOG entry schedule is KNOWN up front, so we can pre-drain (purge)
    # enough growout tanks BEFORE each arrival rather than scrambling the week
    # it lands (the reactive make-room, which silently dropped batches when 6N
    # had no free slot that exact week — B56/B67 on May V7.2). For each arrival
    # week we record how many growout (OG, non-6N) tanks the cohort needs:
    # max(plan tanks, the R28 config floor, density need). The per-week pacing
    # pass below frees these tanks gradually across the lookahead window,
    # spread across systems and paced to the biomass cap. Mirrors the
    # _need calculation in the reactive make-room so the two agree.
    arrival_tank_need: dict[str, int] = {}
    for s in splits:
        if s.tran_og_date is None or s.post_cull_count <= 0:
            continue
        wk = iso_week_label(
            og_entry_week_start(_as_date(s.tran_og_date), initial_state.today))
        _cohort_kg = s.post_cull_count * (s.post_cull_avg_wt_g / 1000.0)
        # plan tanks resolved per-week below (tank_assignments); the config +
        # density floors are the schedule-time lower bound and are enough to pace.
        arrival_tank_need[wk] = arrival_tank_need.get(wk, 0) + _tranog_tank_need(
            _cohort_kg, facility, control)

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
    # Same cap lookup the SystemLimitsAudit reports and the LNS optimizer
    # scores against (caps.carry_forward_cap_lookup) — the rebalancer must
    # be working against the number that will be judged.
    _cap_lookup = (carry_forward_cap_lookup(system_limits)
                   if system_limits is not None else None)

    def _sys_cap(wl_, sysid):
        if _cap_lookup is None:
            return (None, None)
        return (_cap_lookup(wl_, sysid, METRIC_BIOMASS),
                _cap_lookup(wl_, sysid, METRIC_FEED_DAY))

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
    if sixn_phase == "purge" and sixn_pair_queue and sixn_resting_pair is None:
        # The degrade is principled (nothing else is possible with all 3 pairs
        # stocked) but must be VISIBLE: refill-in-place was the 65/71-idle bug
        # (9b8aa17) and the operator should know the run STARTS in that shape.
        warnings.append(
            "6N: all 3 purge pairs are stocked at forecast start — no fallow "
            "pair, so the Wed-fill/Fri-harvest rotation degrades to "
            "refill-in-place until a pair empties")

    # Compute forecast_start once for day-by-day biology.
    forecast_start = initial_state.today

    # OG-entry day per crossing batch: the first forecast-week start on/after
    # TranOG_Date (VBA `wS >= TranOGDate`). The realized TranOG entry + the
    # proactive make-room harvest both fire on THIS day, not the raw
    # TranOG_Date — so the entry lands in the same week the plan reserved
    # tanks for (one week later than the cull). Keeps the realized layer in
    # lockstep with phase_a/precalc and stops the one-week-early entry that
    # was dropping forward batches. See time_grid.og_entry_week_start / BUG #1.
    og_entry_day: dict[str, date] = {
        s.batch_id: og_entry_week_start(_as_date(s.tran_og_date), forecast_start)
        for s in splits if s.tran_og_date
    }

    # Map week_label → (start, end) date range.
    week_ranges: dict[str, tuple[date, date]] = {}
    for label in sorted_weeks:
        wload = next((l for l in load_table if l.week_label == label), None)
        if wload is None:
            continue
        week_ranges[label] = (wload.week_start, wload.week_start + timedelta(days=7))

    # Level-load: fish over the hard weekly harvest cap that a single make-room
    # week was forced to take (conservation > cap) are borrowed from next week's
    # ceiling, so the multi-week total stays <= cap x weeks. 0.0 unless ON.
    _level_load = bool(getattr(control, "harvest_level_load", False))
    _carry_debt = 0.0

    # ANTICIPATORY PURGE PACING state. `_reserved_og` is the set of growout OG
    # tank ids the anticipatory pass has emptied EARLY (by purging them into 6N)
    # and is HOLDING empty until a known TranOG arrival can land in them. Without
    # this hold the rebalancer / balancer / grade-split would immediately reclaim
    # the freed tank (the facility is tank-tight near peak), so freeing it early
    # would be futile — the reserved set makes those fill passes skip it. Each
    # entry maps tank_id -> the arrival week_label it is held for, so the hold is
    # RELEASED once that week passes (or the arrival consumes it). Stays empty for
    # the committed config (no anticipatory free fires there) → no-op there.
    _reserved_for: dict[int, str] = {}
    _reserved_og: set[int] = set()

    # HANDLING BUDGET (operator rule 4): weekly cap on transfer MOVES. The
    # deferrable quality passes check remaining budget before emitting;
    # essential moves never do (see ControlParams.max_transfers_per_week).
    _move_cap = int(getattr(control, "max_transfers_per_week", 0) or 0)

    for week_label in sorted_weeks:
        # Moves emitted so far THIS week = applied Transfers appended since the
        # week opened (refused events have count_transferred 0 and don't count).
        _wk_ev0 = len(transfer_events)

        def _moves_left() -> int:
            """Remaining weekly move budget. Counts what the handling gate
            counts: DISTINCT applied (source, dest) tank pairs this week —
            one physical src->dst pumping event each (a multi-destination
            Transfer is as many moves as it has destinations; two same-pair
            legs are one move; TranOG/Grade rows are not moves). Counting
            EVENTS here (the old unit) under-counted multi-destination
            purge fills and let the deferrable passes overshoot the very
            budget they were clamping to."""
            if _move_cap <= 0:
                return 10 ** 9
            used_pairs = set()
            for e in transfer_events[_wk_ev0:]:
                if isinstance(e, Transfer) and e.count_transferred > 0:
                    for a in e.destinations:
                        if a.count >= 0.5:
                            used_pairs.add((e.source_tank_id, a.tank_id))
            return max(0, _move_cap - len(used_pairs))

        # Release reservations whose arrival week has arrived/passed (the arrival
        # itself consumes the tank below; anything still held past its week is
        # stale and freed back to the rebalancer). Also drop any reservation
        # whose tank is no longer empty (already consumed/claimed).
        def _stale_reservation(t, w):
            # Hold THROUGH the arrival week (w == week_label) so the rebalancer
            # this week can't reclaim the slot before the cohort lands; release
            # only once the arrival week has passed (w < week_label) or the tank
            # is no longer empty (already consumed/claimed).
            if w < week_label:
                return True
            tk = state.tanks_by_id.get(t)
            return tk is None or not tk.is_empty   # consumed / claimed
        for _tid in [t for t, w in list(_reserved_for.items())
                     if _stale_reservation(t, w)]:
            _reserved_for.pop(_tid, None)
        _reserved_og = set(_reserved_for)
        # Mirror into the FacilityState so Transfer.apply enforces the hold at
        # the physical chokepoint (the rebalancing paths diverge from the plan in
        # purge mode, so plan-level exclusion alone can't hold a slot).
        state.reserved_tanks = _reserved_og

        # Build this week's tank → batch map.
        this_assignment: dict[int, str] = {}
        for a in [a for a in tank_assignments if a.week_label == week_label]:
            for tid in a.tank_ids:
                this_assignment[tid] = a.batch_id

        # Don't let the Phase-C plan re-occupy a RESERVED held tank: drop any
        # reserved tank from this week's planned assignment so the diff below
        # won't fill it. The displaced batch consolidates into its other tanks
        # (the diff evens it). This is the plan-side companion to the reserved
        # exclusion in the rebalancer/balancer/grade passes — together they keep
        # an anticipatory-freed slot empty through every fill path until the
        # TranOG cohort lands. No-op when nothing is reserved (committed config).
        for _rtid in _reserved_og:
            this_assignment.pop(_rtid, None)

        ws_we = week_ranges.get(week_label)
        week_start_date = ws_we[0] if ws_we else _as_date(control.forecast_start)

        # ---------------- ANTICIPATORY HANDLING RESERVE ------------------
        # `_moves_left` above answers "what is left of the budget RIGHT NOW".
        # That is blind in one direction: the week's ESSENTIAL passes (the
        # arrival entry-vacate make-room, the anticipatory purge pacing) run
        # AFTER the deferrable quality passes, so quality spends the budget
        # down to exactly the cap and the essential work that follows pushes
        # the week OVER it.
        #
        # MEASURED — per-move attribution of ALL 5 over-budget weeks on 6 PRs
        # (moves = ESSENTIAL + quality):
        #   7.29  2026-W43  17 = 7 (3 rotation fills, 2 source drains,
        #                            2 entry vacates)      + 10 quality
        #   7.17  2026-W39  16 = 5 (4 rotation fills, 1 pacing) + 11 quality
        #   7.17  2027-W28  16 = 4 (3 rotation fills, 1 pacing) + 12 quality
        #   7.9   2026-W43  17 = 6 (1 fill, 3 drains, 2 vacates) + 11 quality
        #   7.2   2026-W43  16 = 6 (1 fill, 3 drains, 2 vacates) + 10 quality
        # EVERY over-budget week was quality-saturated while carrying only 4-7
        # essential moves. The pattern is identical in each: the passes that
        # run BEFORE quality use k moves, quality then spends the budget out to
        # exactly the cap (15 - k), and the essential passes that run AFTER add
        # their 1-2 moves on top — which is the entire overrun. The budget was
        # never short; the planner simply never looked ahead INSIDE the week.
        # A sequencing blindness, not a capacity limit — so nothing here has to
        # relax a business rule, and nothing here does.
        #
        # Everything the later passes need is knowable at the top of the week:
        # the TranOG entry schedule comes from the batch registry
        # (og_entry_day), the tanks a cohort needs from `_tranog_tank_need`,
        # and the 6N rotation is a fixed cadence. Two layers use that:
        #
        #   A. RESERVE (here). Price the work that CANNOT be deferred — the
        #      arrival-week entry-tier make-room — and let the deferrable
        #      passes spend only what is left over (`_moves_left_quality`).
        #      The essential passes keep the raw `_moves_left` and are never
        #      blocked. Deferred quality work is not lost: the leveling
        #      resumes in the next calmer week, exactly the contract the
        #      handling budget already had.
        #   B. DEFER (at the anticipatory purge pacing pass below). That pass
        #      CAN be deferred by construction — it walks a multi-week
        #      lookahead — so instead of reserving for it we let it stand down
        #      in a week that is already at budget and pre-free its tank in a
        #      calmer week inside the window.
        #
        # Layer B is not redundant with A: A is computed BEFORE the quality
        # passes run, and those passes themselves fill empty grow-out tanks —
        # so at reserve time the pacing pass's own demand still reads as
        # satisfied. Reserve what you can predict; defer what you could not.
        #
        # ------------------------ BOTH LAYERS ARE OFF ---------------------
        # Everything above is why they WORK. This is why they are not on.
        #
        # 4-ARM ABLATION (both off / A only / B only / both on) x 3 PRs x 2
        # knob sets = 24 plans, every cell re-run and reproduced, and the
        # both-off arm verified against a physically pre-layer placement.py
        # (identical on every metric). Weeks over the 15-move budget | worst
        # week's moves | weeks under the 30,000 contract floor (fish short):
        #
        #   PR / knobs        both OFF        A only        B only       both ON
        #   7.29 tuned      1|17|3 (9.7k)  0|15|5 (22.7k) 1|17|3 (9.7k) 0|15|5 (22.7k)
        #   7.29 stock      1|17|3 (11.5k) 0|15|4 (22.5k) 1|17|3 (11.5k) 0|15|4 (22.5k)
        #   7.17 tuned      1|16|4 (36.3k) 0|15|9 (56.7k) 0|15|5 (42.0k) 0|15|6 (36.9k)
        #   7.17 stock      2|16|6 (35.9k) 2|16|6 (35.9k) 1|17|7 (45.1k) 0|15|5 (35.0k)
        #   7.9  tuned      1|17|3 (17.8k) 0|15|3 (20.4k) 1|17|3 (17.8k) 0|15|3 (20.4k)
        #   7.9  stock      1|17|6 (28.4k) 0|15|4 (18.2k) 1|17|6 (28.4k) 0|15|4 (18.2k)
        #
        # Read down the columns and the hoped-for split is not there:
        #
        #   * The layers are ONE lever, not two. Layer A buys essentially all
        #     the budget compliance (5 of 6 cells alone) and pays for all of
        #     it out of the harvest floor. On the operator's own PR under both
        #     knob sets, A moves weeks-under-floor 3 -> 5 / 3 -> 4 and more
        #     than doubles the cumulative shortfall.
        #   * Layer B is mostly INERT: on 7.29 and 7.9 its TransferPlan +
        #     HarvestPlan are byte-identical to both-off under both knob sets.
        #     It still logs "pacing DEFERRED" there — it stands down only in
        #     weeks where the pass had no tank to free anyway. Where it does
        #     bite (7.17) it is net harmful alone: stock goes 2 weeks over at
        #     16 moves to 1 week over at 17.
        #   * Layer A breaks a HARD rule. On 7.9 with tuned knobs it produces
        #     a 69,677-fish week (2028-W22) — 26% over the 55,000 processing
        #     limit and past the 60,500 relief ceiling, which is never legal.
        #     Both-off and B-only peak at 54,945 there. Starving the quality
        #     rebalancer concentrates the facility (worst tank density rises
        #     102.5 -> 106.3 on 7.29 tuned), and the harvest controller clears
        #     that concentration with a make-room dump.
        #
        # The operator's rule order decides it: steady weekly harvest is HARD
        # (a 30,000/week contract), and on handling they said "we can move to
        # 15 if we need to". Off costs one 17-move week in 130 on their PR;
        # on costs two more weeks under the contract floor plus a ceiling
        # breach. So both layers are off, and the flags at the top of this
        # module are the lever if that ordering ever changes.
        # ------------------------------------------------------------------
        _arr_wk = ([s for s in splits
                    if s.batch_id in og_entry_day
                    and ws_we[0] <= og_entry_day[s.batch_id] < ws_we[1]]
                   if ws_we is not None else [])

        def _arrival_makeroom_reserve() -> int:
            """Moves the arrival-week entry-tier make-room will need.

            That pass MUST run this week — R1 says a TranOG cohort may enter
            only OG1/2, and a cohort with nowhere to land is a conservation
            breach (the hard no-drop abort). Each entry tank the arrival is
            short costs one forward vacate (R2, entry -> grow-out), plus one
            more move whenever no grow-out slot is standing free to receive
            it (that slot is freed by moving a grow-out tank into 6N).

            Read from live state on every call, so the reserve shrinks as the
            week's earlier passes happen to free entry tanks.

            PURGE ERA ONLY, and that scoping is structural rather than fitted:
            the congestion this reserve relieves is the 6N ROTATION FILL (3-4
            mandatory moves every week) colliding with an arrival, and in
            production mode there is no rotation — 61/63/65 are ordinary
            grow-out — so the collision cannot arise. Consistent with that,
            all 5 measured over-budget weeks across 6 PRs are purge-era, and
            reserving in production mode was measured to buy no budget at all
            while perturbing the plan into a 61,240-fish week over the harvest
            relief ceiling on 7.17 (a hard-gate FAIL). Scoped, not relaxed.
            """
            if not _arr_wk or not purge_this_week:
                return 0
            _need = 0
            for _s in _arr_wk:
                _ta = ta_index.get((_s.batch_id, week_label))
                _pn = len(_ta.tank_ids) if _ta and _ta.tank_ids else 0
                _need += _tranog_tank_need(
                    _s.post_cull_count * (_s.post_cull_avg_wt_g / 1000.0),
                    facility, control, _pn)
            _empty_entry = _free_grow = _vacatable = 0
            for _t in state.tanks_by_id.values():
                if _t.type != "OG" or _t.system_id in _SIXN_SYSTEMS:
                    continue
                if _t.system_id in OG12_SYSTEMS:
                    if _t.is_empty:
                        _empty_entry += 1
                    elif _t.stage != STAGE_STARVE:
                        _vacatable += 1
                elif _t.is_empty:
                    _free_grow += 1
            return _entry_makeroom_move_cost(
                _need, _empty_entry, _free_grow, _vacatable)

        def _moves_left_quality() -> int:
            """`_moves_left` minus the anticipated essential work — the budget
            the DEFERRABLE passes may spend. Policy lives in the pure
            `_quality_moves_left`; this closure only supplies the live numbers.
            """
            # Short-circuit when the layer is off (or the budget is disabled):
            # skips the facility scan `_arrival_makeroom_reserve` would do, and
            # `_moves_left()` already returns the unbounded sentinel for a
            # disabled budget. Same answer as the call below, no work.
            if _move_cap <= 0 or not _ANTICIPATE_ARRIVAL_RESERVE:
                return _moves_left()
            return _quality_moves_left(
                _moves_left(), _arrival_makeroom_reserve(), _move_cap)

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
                reserved=_reserved_og,
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
        fac_bio, fac_growth_kg, _fac_feed_kg_day, oldest_wt, _fac_sw_bio = (
            _realized_facility_metrics(
                state, batch_meta, tables, control.min_harvest_weight_g,
                week_label)
        )
        _fw_now = 0.0   # this week's FW standing biomass (set below if available)
        # FW-INCLUSIVE cap basis (audit H1): add this week's pre-feed (EGG/FW)
        # standing biomass + feed so the setpoint and the feed-implied cap below
        # measure TOTAL facility biomass against the 3.8M cap, not OG-only.
        if fw_biomass_by_week is not None:
            _fw_now = fw_biomass_by_week.get(week_label, 0.0)
            fac_bio += _fw_now
            _fac_feed_kg_day += fw_feed_by_week.get(week_label, 0.0)
            # ANTICIPATE the FW load: the FW biomass curve is fully known, so add its
            # projected RISE over the next _FW_ANTICIPATE_WEEKS into the growth term.
            # The predictive harvest then pre-positions OG drawdown AHEAD of the
            # growing FW instead of reacting after the total has already spiked, so
            # the weekly harvest clip keeps the facility under the cap rather than
            # falling behind. Only the rise is added: a FW DROP is a TranOG departure
            # whose OG arrival the arrivals feed-forward already covers, so
            # subtracting it would double-count. FW is never harvested — this only
            # sheds OG sooner to make room for it.
            _ci = sorted_weeks.index(week_label)
            _fw_ahead = max(
                (fw_biomass_by_week.get(sorted_weeks[_ci + k], 0.0)
                 for k in range(1, _FW_ANTICIPATE_WEEKS + 1)
                 if _ci + k < len(sorted_weeks)),
                default=_fw_now)
            fac_growth_kg += max(0.0, _fw_ahead - _fw_now)
        bio_cap = resolve_facility_cap(METRIC_BIOMASS, week_label, facility_limits, control)
        feed_cap = resolve_facility_cap(METRIC_FEED_DAY, week_label, facility_limits, control)
        max_hv = resolve_facility_cap(METRIC_MAX_HARVEST, week_label, facility_limits, control)
        min_hv = resolve_facility_cap(METRIC_MIN_HARVEST, week_label, facility_limits, control)
        weekly_max = max_hv if max_hv else float("inf")
        if bio_cap is not None:
            # DUAL-LIMIT, control-driven setpoint (no hardcoded margin). The harvest
            # controller works the facility toward BOTH facility caps at once: the
            # effective biomass ceiling is the LOWER of (a) the biomass cap and (b) the
            # biomass at which facility FEED would reach its cap (feed scales ~linearly
            # with biomass at the current size mix), so whichever limit binds drives
            # harvest. The setpoint sits one R24 deviation-band below that ceiling —
            # facility_biomass_deviation_pct is the operator's Control knob for how
            # close to the cap to run (no hidden margin). Below the band the predictive
            # move-in floors to min_harvest, so biomass + feed BUILD toward the caps;
            # near it, harvest ramps between min and max to MAINTAIN them.
            eff_cap = bio_cap
            if feed_cap and _fac_feed_kg_day > 0:
                # Feed-implied biomass ceiling, UN-BIASED (audit M3): only the FEEDING
                # biomass (SW grow-out + FW) produces facility feed; off-feed 6N/STARVE
                # purge biomass eats nothing, so it must NOT inflate the biomass-per-
                # feed ratio (the old proxy divided TOTAL fac_bio by SW feed, which
                # overstated the ceiling whenever depuration biomass was large).
                # Convert only the feeding biomass at the feed cap, then add the
                # non-feeding biomass back so the result is a TOTAL-biomass ceiling
                # comparable to fac_bio. Both caps are HARD; the lower one binds; the
                # deviation band below is the only soft margin.
                _feeding_bio = _fac_sw_bio + _fw_now
                _nonfeed_bio = max(0.0, fac_bio - _feeding_bio)
                if _feeding_bio > 0:
                    eff_cap = min(
                        bio_cap,
                        feed_cap * _feeding_bio / _fac_feed_kg_day + _nonfeed_bio)
            _dev = control.facility_biomass_deviation_pct or 0.0
            setpoint = eff_cap * (1.0 - _dev)
        else:
            setpoint = None

        # Realized biomass above the HARD cap (not merely above the setpoint):
        # the controller's own shed must never be clamped by a guide ceiling.
        _over_cap = (bio_cap is not None and fac_bio > bio_cap)

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

        # HYBRID lever 1 — PURGE weeks only. Fish moved in NOW drain at
        # `drain_idx`, so this week's move-in must deliver the guide's quantity
        # THERE (reusing the drain index the arrivals feed-forward computed —
        # no new offset arithmetic). Phase-aware at RUNTIME: `sixn_phase` is
        # only known during the walk, and the two engines' 6N clocks differ.
        if harvest_guide is not None and harvest_guide.purge_lever:
            if purge_this_week and sixn_refill:
                _dl = (sorted_weeks[drain_idx]
                       if drain_idx < len(sorted_weeks) else None)
                move_in_target = harvest_guide.target(
                    _dl, move_in_target, min_hv or 0.0, weekly_max,
                    # Ceiling only when L1 also calls the DRAIN week a purge
                    # week. If L1 has flipped to production by calendar while
                    # we are still purging, its number describes a different
                    # machine — take it as a floor, never as a clamp.
                    allow_ceiling=(harvest_guide.mode_for(_dl) == "purge"))
            elif purge_this_week:
                # Winddown: refills are off, so there is no move-in to steer,
                # and harvest_target is inert on this path. Say so rather than
                # letting the guide believe it is in control.
                harvest_guide.note(
                    f"{week_label}: winddown — no lever (6N refill off, harvest "
                    f"is whatever the draining pair holds)")

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

        # LEVEL-LOAD anticipation (opt-in): pre-harvest cohorts EARLIER so weekly
        # throughput is leveled under the hard cap and biomass never piles into a
        # one-week dump. Project REALIZED facility growth K weeks out (the Phase-A
        # projection under-predicts peaks ~3% and is unsafe), estimate the biomass
        # that must be shed over that window, and raise the harvest floor to 1/K of
        # it — so the controller starts shedding a coming peak across the calm
        # run-up weeks rather than reacting in the peak week. Composes with the
        # anticipatory setpoint (which sets HOW FAR below cap to sit; this sets HOW
        # EARLY to start). The weekly_max clamp below still caps each week, and the
        # _HarvestBudget enforces it hard across all passes.
        if (_level_load and setpoint is not None and oldest_wt > 0
                and math.isfinite(weekly_max)):
            _K = max(1, int(getattr(control, "harvest_smooth_lookahead_weeks", 6) or 1))
            _proj_bio_K = fac_bio + _K * max(0.0, fac_growth_kg)
            _shed_K = max(0.0, _proj_bio_K - setpoint)
            _level_floor = (_shed_K * 1000.0 / oldest_wt) / _K
            _lt = getattr(control, "harvest_level_target", None)
            if _lt not in (None, "auto"):
                _level_floor = max(_level_floor, float(_lt))
            harvest_target = max(harvest_target, _level_floor)

        # HYBRID lever 2 — PRODUCTION weeks (incl. the 6N-empty window). Its OWN
        # gate, deliberately not the level-load gate above: piggy-backing there
        # would make the hybrid silently inert whenever harvest_level_load is
        # off, weekly_max is infinite, or no mature fish exist.
        if (harvest_guide is not None and harvest_guide.production_lever
                and not purge_this_week):
            harvest_target = harvest_guide.target(
                week_label, harvest_target, min_hv or 0.0, weekly_max,
                # Ceiling needs BOTH: L1 agrees this is a production week (its
                # calendar clock runs ahead of our phase machine through
                # winddown/empty), AND we are not over the hard cap, where the
                # controller's own shed has to win.
                allow_ceiling=((harvest_guide.mode_for(week_label) == "production")
                               and not _over_cap))

        # In-place purge length + this week's harvest target (shared by the
        # winddown pre-stage and the production harvest below).
        purge_days = int(getattr(control, "starvation_period_days", 0) or 0)
        weekly_target = min(weekly_max, max(min_hv or 0.0, harvest_target or 0.0))

        # STARVE ENTRY target — DECOUPLED from the harvest cap. A tank entered
        # this week is harvestable `_starve_weeks` later, so it must be sized by
        # what the guide asks for at the week it will SERVE, not by this week's
        # harvest cap. Both halves matter: without the shift a raised cap cannot
        # be met (the backlog was sized last week), and with a shared number we
        # would freeze more fish off-feed than the pipeline needs.
        _starve_weeks = max(1, math.ceil((purge_days or 7) / 7.0))
        _entry_target = weekly_target
        if harvest_guide is not None and harvest_guide.production_lever:
            _si = cur_idx + _starve_weeks
            if _si < len(sorted_weeks):
                _sl = sorted_weeks[_si]
                _entry_target = harvest_guide.target(
                    _sl, weekly_target, min_hv or 0.0, weekly_max,
                    allow_ceiling=((harvest_guide.mode_for(_sl) == "production")
                                   and not _over_cap))

        # Shared per-week harvest budget. ON: a HARD ceiling at weekly_max less any
        # carried overdraw, threaded through every harvest pass below. OFF: inf, so
        # every take() is a pass-through and emissions are byte-identical to legacy.
        if _level_load and math.isfinite(weekly_max):
            _budget = _HarvestBudget(cap=max(0.0, weekly_max - _carry_debt))
        else:
            _budget = _HarvestBudget(cap=float("inf"))
        _carry_debt = 0.0

        # Harvest engine — 6N purge pipeline when in purge mode, else Layer-2 FIFO.
        _sixn_avoid: frozenset = frozenset()  # imminent-drain 6N tanks (set below)
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
                budget=_budget,
                reserved=_reserved_og,
                tables=tables,
                sixn_move_in_feed=sixn_move_in_feed,
                grade_events=grade_events,
            )
            # DEPURATION HOLD, fill side: after this week's rotation the front
            # of the queue drains NEXT week — any make-room dump into its tanks
            # would ship fish with 1 week of purge (the audited leak: 33,206
            # fish into OG6N-71 at 2026-W39, drained W40). Every downstream
            # 6N fill this week must avoid those tanks.
            _sixn_avoid = (frozenset(sixn_pair_queue[0])
                           if sixn_pair_queue else frozenset())
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
            # PRIMING IS FLOOR-ONLY. A guide ceiling must never shrink this: the
            # pre-stage is what fills the in-place purge pipeline BEFORE
            # production opens, and under-filling it empties the first
            # production weeks with nothing left to harvest. Measured: clamping
            # here put a 4,191-fish crater at 2028-W02, the week after the 6N
            # switch, on a baseline that had no craters at all. The guide may
            # raise priming, never lower it.
            _prime_target = max(weekly_target, _entry_target)
            if not sixn_refill and purge_days > 0 and _prime_target > 0:
                # Depuration takes ceil(purge_days/7) weekly steps to complete, so
                # the pipeline must hold that many staggered cohorts of ~target to
                # deliver target EVERY week from production's first week. Fill up to
                # that depth, entering ~target/week (the steady production rate).
                _stage_cap = math.ceil(purge_days / 7.0) * _prime_target
                _in_pipe = sum(t.count for t in state.tanks_by_id.values()
                               if t.stage == STAGE_STARVE and not t.is_empty)
                _entered = 0.0
                for bid in _pick_fifo_move_in_batches(state, batch_meta, control):
                    if _in_pipe >= _stage_cap or _entered >= _prime_target:
                        break
                    src_tanks = sorted(
                        [t for t in state.tanks_by_id.values()
                         if t.batch_id == bid and not t.is_empty and t.stage == "SW"
                         and t.system_id not in OG12_SYSTEMS   # R5: never purge-stage entry-tier tanks
                         and t.avg_wt_g >= control.min_harvest_weight_g],
                        key=lambda t: (-t.avg_wt_g, t.tank_id))
                    for src in src_tanks:
                        if _in_pipe >= _stage_cap or _entered >= _prime_target:
                            break
                        src.stage = STAGE_STARVE
                        src.starvation_days_remaining = purge_days
                        _in_pipe += src.count
                        _entered += src.count
                        warnings.append(
                            f"{week_label}: PRE-STAGE in-place purge {src.location_id} "
                            f"(batch {src.batch_id}, {src.count:.0f} fish) — readying "
                            f"the 6N->production harvest handoff (no gap)")
                # FORWARD-PROMOTION (R2/R5): if the grow-out pool alone can't
                # fill the priming target, mature fish parked in ENTRY tanks
                # (which R5 forbids staging in place) are MOVED forward into a
                # free grow-out tank — legal at any weight — and THAT tank is
                # staged. Keeps the handoff pipeline full without entry-tier
                # harvest; no-op when no grow-out slot is free.
                if _in_pipe < _stage_cap and _entered < _prime_target:
                    _prime_entry = sorted(
                        [t for t in state.tanks_by_id.values()
                         if not t.is_empty and t.type == "OG"
                         and t.system_id in OG12_SYSTEMS
                         and t.stage == "SW"
                         and t.avg_wt_g >= control.min_harvest_weight_g],
                        key=lambda t: (-t.avg_wt_g, t.tank_id))
                    for _esrc in _prime_entry:
                        if _in_pipe >= _stage_cap or _entered >= _prime_target:
                            break
                        _fg = next(
                            (t for t in sorted(state.tanks_by_id.values(),
                                               key=lambda x: x.tank_id)
                             if t.is_empty and t.type == "OG"
                             and t.system_id not in _SIXN_SYSTEMS
                             and t.system_id not in OG12_SYSTEMS
                             and t.tank_id not in _reserved_og),
                            None)
                        if _fg is None:
                            break  # no grow-out slot to promote into
                        _e_batch, _e_loc = _esrc.batch_id, _esrc.location_id
                        _mv = Transfer(
                            batch_id=_e_batch, event_date=week_start_date,
                            source_tank_id=_esrc.tank_id,
                            destinations=[TankAllocation(
                                tank_id=_fg.tank_id, count=_esrc.count,
                                avg_wt_g=_esrc.avg_wt_g, cv_pct=_esrc.cv_pct)],
                            leaves_source_empty=True,
                        )
                        warnings.extend(_mv.apply(state))
                        transfer_events.append(_mv)
                        if _fg.is_empty:   # transfer refused
                            break
                        _fg.stage = STAGE_STARVE
                        _fg.starvation_days_remaining = purge_days
                        _in_pipe += _fg.count
                        _entered += _fg.count
                        warnings.append(
                            f"{week_label}: PRE-STAGE promoted mature entry tank "
                            f"{_e_loc} (batch {_e_batch}) forward into "
                            f"{_fg.location_id} and staged it (R2/R5 — entry-tier "
                            f"fish route forward before harvest)")
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
                        # R5 guard: an entry-tier STARVE tank shouldn't exist
                        # (entry never enters the purge pipeline post-rules);
                        # if one does, never harvest it from the entry tier.
                        if (t.starvation_days_remaining <= 0
                                and t.system_id not in OG12_SYSTEMS):
                            _ready.append(t)
                _ready.sort(key=lambda t: (-t.avg_wt_g, t.tank_id))
                _hv = 0.0
                for t in _ready:
                    if _hv >= target:
                        break
                    # Partial-tank harvest: take exactly the remaining target from
                    # the last tank so the week lands on target instead of
                    # overshooting by up to a whole tank (which both spikes harvest
                    # past the processing max AND drags biomass below the setpoint).
                    # The remnant stays STARVE (frozen weight), front of next week.
                    take = min(t.count, target - _hv)
                    if _budget.remaining() <= 0:
                        break              # HARD weekly ceiling reached
                    take = _budget.take(take)
                    # INV-5-aware sizing: a take leaving 0 < residue < floor
                    # would force-empty the tank — up to min_tank_control fish
                    # PAST the hard ceiling (the measured 66,907 week = 60,000
                    # + a 6,907 force-empty). Take the whole tank only when it
                    # fits the budget; otherwise take LESS, leaving the floor
                    # (the frozen remnant fronts next week's drain).
                    _resid = t.count - take
                    _floor_ = control.min_tank_control or 0.0
                    if 0 < _resid < _floor_:
                        if _budget.take(t.count) >= t.count - 0.5:
                            take = t.count
                        else:
                            take = _budget.take(max(0.0, t.count - _floor_))
                    if take <= 0:
                        continue
                    ev = Harvest(
                        batch_id=t.batch_id, event_date=week_start_date,
                        source_tank_id=t.tank_id, count=take,
                        avg_wt_g=t.avg_wt_g,
                        min_tank_control=control.min_tank_control,
                    )
                    warnings.extend(ev.apply(state))
                    harvest_events.append(ev)
                    _budget.record(ev.count)
                    _hv += ev.count
                # (b) Enter ~target/week of fresh tanks into purge to keep the
                # staircase going (in == out == target ⇒ the pipeline stays
                # bounded at ~ceil(purge_days/7) cohorts; the step-(a) cap drains
                # any transient backlog smoothly). Don't enter while a ripe backlog
                # already covers next week's target — avoids freezing extra fish.
                _backlog = sum(t.count for t in state.tanks_by_id.values()
                               if t.stage == STAGE_STARVE and not t.is_empty
                               and t.starvation_days_remaining <= 0)
                _entered = 0.0
                # ENTRY is sized by _entry_target (the guide's demand at the week
                # these fish will actually serve), not by `target` (THIS week's
                # harvest cap). Identical to `target` unless the hybrid steers.
                if _entry_target > 0 and _backlog < _entry_target:
                    for bid in _pick_fifo_move_in_batches(state, batch_meta, control):
                        if _entered >= _entry_target:
                            break
                        src_tanks = [t for t in state.tanks_by_id.values()
                                     if t.batch_id == bid and not t.is_empty
                                     and t.stage == "SW"
                                     and t.system_id not in OG12_SYSTEMS  # R5
                                     and t.avg_wt_g >= control.min_harvest_weight_g]
                        src_tanks.sort(key=lambda t: (-t.avg_wt_g, t.tank_id))
                        for src in src_tanks:
                            if _entered >= _entry_target:
                                break
                            src.stage = STAGE_STARVE
                            src.starvation_days_remaining = purge_days
                            _entered += src.count
                    # FORWARD-PROMOTION (R2/R5): market-ready fish parked in
                    # ENTRY tanks can't be purged in place (R5 forbids harvest
                    # / staging from OG1/2), so when the grow-out pool alone
                    # can't fill the entry target, MOVE the biggest entry tank
                    # forward into a free grow-out tank (legal at any weight —
                    # R2; step (a)'s harvest just freed one) and stage THAT
                    # tank. Without this the pipeline starves while supply
                    # sits in OG1/2 (the 2028-W29 zero-harvest crater).
                    if _entered < _entry_target:
                        _entry_mature = sorted(
                            [t for t in state.tanks_by_id.values()
                             if not t.is_empty and t.type == "OG"
                             and t.system_id in OG12_SYSTEMS
                             and t.stage == "SW"
                             and t.avg_wt_g >= control.min_harvest_weight_g],
                            key=lambda t: (-t.avg_wt_g, t.tank_id))
                        for _esrc in _entry_mature:
                            if _entered >= _entry_target:
                                break
                            _fg = _free_production_stage_tank(
                                state, _reserved_og)
                            if _fg is None:
                                break  # no grow-out slot to promote into
                            _e_batch, _e_loc = _esrc.batch_id, _esrc.location_id
                            _mv = Transfer(
                                batch_id=_e_batch, event_date=week_start_date,
                                source_tank_id=_esrc.tank_id,
                                destinations=[TankAllocation(
                                    tank_id=_fg.tank_id, count=_esrc.count,
                                    avg_wt_g=_esrc.avg_wt_g, cv_pct=_esrc.cv_pct)],
                                leaves_source_empty=True,
                            )
                            warnings.extend(_mv.apply(state))
                            transfer_events.append(_mv)
                            if _fg.is_empty:   # transfer refused
                                break
                            _fg.stage = STAGE_STARVE
                            _fg.starvation_days_remaining = purge_days
                            _entered += _fg.count
                            warnings.append(
                                f"{week_label}: PROMOTED mature entry tank "
                                f"{_e_loc} (batch {_e_batch}) forward into "
                                f"{_fg.location_id} and staged it for in-place "
                                f"purge (R2/R5 — entry-tier fish route forward "
                                f"before harvest)")
                    # GRADED STAGE (last resort, floor only): during a FIFO
                    # batch-ripeness gap NO whole tank is at min harvest
                    # weight anywhere, but the ripest tank's upper TAIL is.
                    # Peel that tail via a Grade into a free grow-out tank
                    # (entry sources grade FORWARD — R2-legal at any weight;
                    # never into entry — R4) and stage THAT tank for in-place
                    # purge. Without this the staircase skips a week and the
                    # harvest goes dark purge_days later (measured: the
                    # 2028-W43 zero on the 7.9.26 PR — every ripe fish sat in
                    # tanks averaging 3.33-3.48 kg vs the 3.5 kg gate).
                    _floor_p = min(_entry_target,
                                   float(control.min_harvest_per_week
                                         or _entry_target)
                                   * _SIXN_FILL_MORTALITY_PAD)
                    if _entered < _floor_p:
                        from statistics import NormalDist as _ND_ps
                        _std_ps = _ND_ps()
                        _min_hv_p = control.min_harvest_weight_g or 0.0
                        _min_tr_p = getattr(control, "min_transfer_count",
                                            0.0) or 0.0

                        def _frac_above_p(avg, cvp, thr):
                            if avg <= 0 or cvp <= 0:
                                return 1.0 if avg >= thr else 0.0
                            z = (thr - avg) / (avg * cvp / 100.0)
                            return max(0.0, min(1.0, 1.0 - _std_ps.cdf(z)))

                        while _entered < _floor_p and _min_hv_p > 0:
                            _near = [
                                t for t in state.tanks_by_id.values()
                                if not t.is_empty and t.type == "OG"
                                and t.stage == "SW"
                                and t.avg_wt_g < _min_hv_p
                                and _frac_above_p(t.avg_wt_g, t.cv_pct or 16.0,
                                                  _min_hv_p) >= 0.10]
                            _near.sort(key=lambda t: (-t.avg_wt_g, t.tank_id))
                            _staged_one = False
                            for _gs in _near:
                                _dst = _free_production_stage_tank(
                                    state, _reserved_og)
                                if _dst is None:
                                    break
                                _cvg = _gs.cv_pct or 16.0
                                _frac = _frac_above_p(_gs.avg_wt_g, _cvg,
                                                      _min_hv_p)
                                _big = min(_gs.count * _frac,
                                           _floor_p - _entered)
                                _small = _gs.count - _big
                                if _big < max(1.0, _min_tr_p):
                                    continue
                                if _small < (control.min_tank_control or 0):
                                    continue
                                # Capped by the floor above -> means must come
                                # from the moved fraction, not the threshold.
                                _bavg, _savg = count_split_means(
                                    _gs.avg_wt_g, _cvg,
                                    (_big / _gs.count) if _gs.count > 0 else 0.0)
                                _g_batch, _g_loc = _gs.batch_id, _gs.location_id
                                _gev = Grade(
                                    batch_id=_g_batch,
                                    event_date=week_start_date,
                                    source_tank_ids=[_gs.tank_id],
                                    destinations=[
                                        TankAllocation(
                                            tank_id=_gs.tank_id, count=_small,
                                            avg_wt_g=_savg, cv_pct=_cvg),
                                        TankAllocation(
                                            tank_id=_dst.tank_id, count=_big,
                                            avg_wt_g=_bavg, cv_pct=_cvg),
                                    ],
                                )
                                warnings.extend(_gev.apply(state))
                                if _dst.is_empty:   # grade refused
                                    continue
                                grade_events.append(_gev)
                                _dst.stage = STAGE_STARVE
                                _dst.starvation_days_remaining = purge_days
                                _entered += _dst.count
                                warnings.append(
                                    f"{week_label}: GRADED STAGE — peeled the "
                                    f"ripe tail ({_dst.count:,.0f} fish @ "
                                    f"{_bavg / 1000:.2f}kg) of {_g_loc} (batch "
                                    f"{_g_batch}) into {_dst.location_id} and "
                                    f"staged it for in-place purge (floor "
                                    f"continuity; {_small:,.0f} stay growing)")
                                _staged_one = True
                                break
                            if not _staged_one:
                                break
            elif target > 0:
                # Immediate harvest (empty-phase tail, or purge_days unset).
                harvested = 0.0
                for bid in _pick_fifo_move_in_batches(state, batch_meta, control):
                    if harvested >= target:
                        break
                    src_tanks = [t for t in state.tanks_by_id.values()
                                 if t.batch_id == bid and not t.is_empty
                                 and t.system_id not in OG12_SYSTEMS  # R5: no entry-tier harvest
                                 and t.avg_wt_g >= control.min_harvest_weight_g]
                    src_tanks.sort(key=lambda t: t.avg_wt_g, reverse=True)
                    for src in src_tanks:
                        if harvested >= target:
                            break
                        take = min(target - harvested, src.count)
                        if _budget.remaining() <= 0:
                            break          # HARD weekly ceiling reached
                        take = _budget.take(take)
                        # INV-5-aware sizing (see the STARVE loop above): never
                        # let a force-empty escalate past the hard ceiling.
                        _resid = src.count - take
                        _floor_ = control.min_tank_control or 0.0
                        if 0 < _resid < _floor_:
                            if _budget.take(src.count) >= src.count - 0.5:
                                take = src.count
                            else:
                                take = _budget.take(
                                    max(0.0, src.count - _floor_))
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
                        _budget.record(ev.count)
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
                    min_keep=control.min_tank_control or 0.0,
                    # Only the EVENING half consults this; source drains are
                    # essential and never blocked (see the function's docstring).
                    moves_left=_moves_left_quality,
                )
            # Even-out pass: fix PR/residual over-concentration by
            # leveling fish across each batch's tanks where a tank is
            # over density cap and the moves are legal. Runs for ALL
            # active batches (including unchanged sets the diff skipped).
            # FUND THE RELIEF FIRST. An over-cap OG1/2 tank has a legal
            # forward move but nowhere durable to put the fish at ~90% tank
            # occupancy. Consolidating some batch's own growout tanks CREATES
            # a free tank rather than competing for one of the ~3.9 free in an
            # average week. Fires only when an entry tank is actually over cap,
            # and frees at most as many tanks as there are over-cap tanks.
            _over_entry = [t for t in state.tanks_by_id.values()
                           if not t.is_empty and t.type == "OG"
                           and t.system_id in OG12_SYSTEMS
                           and t.stage != STAGE_STARVE
                           and t.max_density_kg_m3 > 0
                           and t.density_kg_m3 > t.max_density_kg_m3]
            if _over_entry and _moves_left_quality() > 0:
                _consolidate_growout_to_free_tanks(
                    state, transfer_date, transfer_events, warnings,
                    need_tanks=len(_over_entry),
                    max_moves=_moves_left_quality(),
                )
            for b in sorted(set(prev_by_batch) | set(this_by_batch)):
                if _moves_left_quality() <= 0:
                    break             # handling budget spent — deferrable pass
                _even_out_density(
                    state, b, transfer_date, transfer_events, warnings,
                    max_moves=_moves_left_quality(),
                )

            # Multi-objective balancer: relieve any tank still over density cap
            # into the destination with the most headroom across density + system
            # feed + system biomass — cutting out-of-bounds on all three at once
            # without trading one for another. Continuity-safe (conserved
            # Transfers; new tanks forward-persisted).
            _bal_budget = min(
                int(getattr(control, "rebalance_balance_budget", 0) or 0),
                _moves_left_quality())    # handling budget: deferrable pass
            if _rebal_on and _bal_budget > 0:
                _balance_loads(
                    state, week_label, transfer_date, transfer_events, warnings,
                    ta_index, week_tank_owner, sorted_weeks, week_index_r,
                    _sys_cap, _rebal_buf, batch_meta, tables, _og_sys,
                    _grow_sys, _og_tanks, _bal_budget,
                    level=bool(getattr(control, "rebalance_level", False)),
                    reserved=_reserved_og,
                    min_transfer=getattr(control, "min_transfer_count", 0.0) or 0.0,
                    min_keep=control.min_tank_control or 0.0,
                )

            # Variable-quantity pass: with the week's placement realized, shave
            # any system still over its biomass/feed cap by moving a PRECISE
            # count of fish between a batch's existing tanks into a system with
            # headroom — exactly enough, no overshoot. Continuity-safe.
            _vq_budget = min(
                int(getattr(control, "rebalance_varqty_budget",
                            _REBALANCE_VARQTY_BUDGET) or 0),
                _moves_left_quality())    # handling budget: deferrable pass
            if _rebal_on and _vq_budget > 0:
                _variable_quantity_rebalance(
                    state, week_label, transfer_date, transfer_events, warnings,
                    _sys_cap, _rebal_buf, batch_meta, tables, og_systems_set,
                    og_tanks_by_system_r, _vq_budget,
                    min_transfer=getattr(control, "min_transfer_count", 0.0) or 0.0,
                    min_keep=control.min_tank_control or 0.0,
                )

            # REMNANT SWEEP (runs with the rebalancer every week): fold any
            # occupied tank still under min_tank_control — mortality attrition
            # or a refused transfer's stranded tail — into its own batch's
            # other tanks (same-batch top-up, tier-legal, whole-remnant only).
            # This makes the abort-time _consolidate_entry_forward (the
            # arrival-week backstop below) nearly unreachable.
            _consolidate_remnants(
                state, transfer_date, week_label, transfer_events, warnings,
                control.min_tank_control or 0.0,
                # handling budget: deferrable pass
                max_moves=_moves_left_quality(),
            )

        # ANTICIPATORY PURGE PACING (purge mode only). The TranOG arrival
        # schedule is known up front, but the reactive make-room below only acts
        # the WEEK an arrival lands — and by then the growout tanks may hold only
        # sub-market fish that can't be purged into 6N (the B56/B67 silent drops
        # on May V7.2: at the arrival week harvestable-growout count was 0, so
        # nothing could be moved out to free a slot, even though 6N HAD free
        # slots). In the calmer run-up weeks there ARE market-ready growout tanks
        # AND free 6N slots; we use them here: purge a readiest growout tank into
        # 6N now (freeing the slot immediately) and HOLD that slot empty (reserve
        # it) until the arrival lands in it. The reserve makes the rebalancer /
        # balancer / grade-split skip the held tank so it isn't reclaimed before
        # the cohort arrives. We target only the NEXT arrival inside the lookahead
        # and only its deficit (need minus tanks already empty-or-reserved for
        # it), one tank per pass, spread across the most-loaded systems. If no 6N
        # slot or no market-ready growout tank is free this week we simply wait —
        # there is a multi-week lookahead to catch up. Production mode unchanged.
        ws_we = week_ranges.get(week_label)
        if purge_this_week and sixn_refill and ws_we is not None:
            _aw_start = ws_we[0]
            _cur_i = sorted_weeks.index(week_label)
            # Lookahead = the purge lead (rotation depth) + 1, so a tank freed now
            # is reliably still empty (held) by the arrival; min 4 to give the
            # dwindling market-ready window time to be harvested before it shrinks.
            _look = max(4, len(sixn_pair_queue) + 1)
            _min_hv_wt = control.min_harvest_weight_g or 0
            # Walk upcoming arrival weeks nearest-first; pre-stage each in turn.
            for _j in range(1, _look + 1):
                _wi = _cur_i + _j
                if _wi >= len(sorted_weeks):
                    break
                _wk = sorted_weeks[_wi]
                _need_wk = arrival_tank_need.get(_wk, 0)
                if _need_wk <= 0:
                    continue
                # HANDLING BUDGET — LAYER B of the anticipatory reserve (see the
                # block at the top of the week loop). Of everything that runs
                # after the deferrable quality passes, THIS pass is the one that
                # can legitimately wait: it walks a multi-week lookahead and its
                # own contract already says so ("if no 6N slot or no market-ready
                # growout tank is free this week we simply wait"). So in a week
                # already at the handling budget it stands down and a calmer week
                # inside the window pre-frees the same tank — the work moves
                # EARLIER/LATER, it is never refused.
                #
                # Deferral stops one week out: an arrival landing NEXT week
                # (_j == 1) pre-stages regardless, so the last chance is never
                # skipped and no arrival is ever left short a tank. The reactive
                # arrival-week make-room below remains the final backstop.
                #
                # OFF BY DEFAULT — `_pacing_may_defer` is constantly False
                # unless `_ANTICIPATE_PACING_DEFER` is set. The 4-arm ablation
                # (table at the top of the week loop) found this branch inert
                # on 2 of 3 PRs — plan byte-identical to not having it, while
                # still logging the DEFERRED warning below, because it only
                # ever stood down in weeks where the pass had no tank to free
                # anyway — and net harmful on the third. Kept, gated, measured.
                if _pacing_may_defer(_j, _moves_left()):
                    warnings.append(
                        f"{week_label}: anticipatory purge pacing DEFERRED for "
                        f"the TranOG arrival in {_wk} — the week is at its "
                        f"handling budget ({_move_cap} moves); a calmer week "
                        f"inside the lookahead pre-frees the tank instead")
                    continue
                # Tanks already lined up for THIS arrival: currently-empty growout
                # tanks that are EITHER unreserved OR reserved for this same week
                # (don't double-count tanks held for a LATER arrival). Reserve the
                # shortfall now.
                _avail = sum(
                    1 for t in state.tanks_by_id.values()
                    if t.is_empty and t.type == "OG"
                    and t.system_id not in _SIXN_SYSTEMS
                    and _reserved_for.get(t.tank_id, _wk) == _wk
                )
                _deficit_wk = _need_wk - _avail
                while _deficit_wk > 0:
                    _cands = [
                        t for t in state.tanks_by_id.values()
                        if not t.is_empty and t.type == "OG"
                        and t.system_id not in _SIXN_SYSTEMS
                        and t.system_id not in OG12_SYSTEMS  # R5: never purge entry-tier tanks into 6N
                        and t.stage != STAGE_STARVE
                        and t.avg_wt_g >= _min_hv_wt
                    ]
                    if not _cands:
                        break  # nothing market-ready to purge yet; wait
                    # Spread: drain the system with the MOST occupied growout
                    # tanks first, readiest (nearest-market) tank within it.
                    _by_sys: dict[str, int] = {}
                    for t in _cands:
                        _by_sys[t.system_id] = _by_sys.get(t.system_id, 0) + 1
                    _cands.sort(key=lambda t: (
                        -_by_sys[t.system_id], -t.avg_wt_g, t.tank_id))
                    _src = _cands[0]
                    _src_tid = _src.tank_id
                    _src_batch = _src.batch_id
                    if not _make_room_into_6n(
                            state, _src, _aw_start, sixn_resting_pair,
                            transfer_events, warnings, week_label,
                            reason=(f"anticipatory purge pacing — holding a tank "
                                    f"for TranOG arrival in {_wk} (needs "
                                    f"{_need_wk})"),
                            sixn_move_in_feed=sixn_move_in_feed, tables=tables,
                            batch_meta=batch_meta, is_purge=True,
                            avoid=_sixn_avoid):
                        break  # 6N full this week — rotation frees a slot soon
                    # The fish have left the production layer (into 6N), so strip
                    # this tank from EVERY batch's PLAN until the arrival — else the
                    # weekly assignment diff (or a previously-persisted split/swap)
                    # routes a batch back into the held slot, defeating the hold.
                    _persist_tank_reserve(
                        _src_tid, week_label, _wk,
                        sorted_weeks, week_index_r, ta_index, week_tank_owner,
                        tank_assignments)
                    # HOLD the just-freed growout slot for this arrival week.
                    _reserved_for[_src_tid] = _wk
                    _reserved_og.add(_src_tid)
                    _deficit_wk -= 1

        # ANTICIPATORY ARRIVAL FREEING — PRODUCTION mode (the ceiling's last
        # gap). The purge-era pass above pre-frees tanks by purging into 6N;
        # in production the only escape is a DIRECT harvest, so the reactive
        # make-room below had to whole-tank-dump PAST the weekly limit in the
        # arrival week itself (conservation > cap — the measured 81,460-fish
        # week: two whole tanks at once). Pre-free here instead, WITHIN this
        # week's remaining limit budget only (budget.take — never a borrow):
        # the same fish are harvested a few weeks earlier UNDER the limit —
        # the pull-forward the operator prefers over touching the relief
        # band — and the freed tank is RESERVED for the arrival
        # exactly like the purge-era pass. Purge-complete STARVE tanks first
        # (harvest-ready anyway — gentler than the reactive pass, which
        # prefers un-starved SW), then the smallest dump.
        if (not purge_this_week) and ws_we is not None:
            _aw_start = ws_we[0]
            _cur_i = sorted_weeks.index(week_label)
            _min_hv_wt = control.min_harvest_weight_g or 0
            for _j in range(1, 6 + 1):
                _wi = _cur_i + _j
                if _wi >= len(sorted_weeks):
                    break
                _wk = sorted_weeks[_wi]
                _need_wk = arrival_tank_need.get(_wk, 0)
                if _need_wk <= 0:
                    continue
                # Availability is PESSIMISTIC for NEAR arrivals: an unreserved
                # empty tank counted today is routinely consumed by the
                # rebalancer/diff before the arrival (measured: a 79,288-fish
                # borrow fired although empties existed 4 weeks earlier). For
                # arrivals <= 3 weeks out, RESERVE the empties we count —
                # locking them costs nothing (no harvest) and the consumption
                # window is short. Reserving for DISTANT arrivals was measured
                # to over-hoard: it starved a production week into a
                # 7,062-fish crater on the regression fixture, so far weeks
                # keep the optimistic count + the budgeted pre-free below.
                _held = sum(1 for t, w in _reserved_for.items() if w == _wk)
                if _j <= 3:
                    for t in sorted(state.tanks_by_id.values(),
                                    key=lambda x: x.tank_id):
                        if _held >= _need_wk:
                            break
                        if (t.is_empty and t.type == "OG"
                                and t.system_id not in _SIXN_SYSTEMS
                                and t.tank_id not in _reserved_for):
                            _persist_tank_reserve(
                                t.tank_id, week_label, _wk,
                                sorted_weeks, week_index_r, ta_index,
                                week_tank_owner, tank_assignments)
                            _reserved_for[t.tank_id] = _wk
                            _reserved_og.add(t.tank_id)
                            _held += 1
                else:
                    _held += sum(
                        1 for t in state.tanks_by_id.values()
                        if t.is_empty and t.type == "OG"
                        and t.system_id not in _SIXN_SYSTEMS
                        and t.tank_id not in _reserved_for)
                _deficit_wk = _need_wk - _held
                while _deficit_wk > 0:
                    _cands = [t for t in state.tanks_by_id.values()
                              if not t.is_empty and t.type == "OG"
                              and t.system_id not in _SIXN_SYSTEMS
                              and t.system_id not in OG12_SYSTEMS
                              and t.avg_wt_g >= _min_hv_wt
                              and (t.stage != STAGE_STARVE
                                   or t.starvation_days_remaining <= 0)
                              # affordable inside THIS week's ceiling budget
                              and _budget.take(t.count) >= t.count - 0.5]
                    if not _cands:
                        break        # no headroom this week — later weeks retry
                    _cands.sort(key=lambda t: (
                        0 if t.stage == STAGE_STARVE else 1, t.count, t.tank_id))
                    _src = _cands[0]
                    _src_tid, _src_batch = _src.tank_id, _src.batch_id
                    _src_cnt, _src_loc = _src.count, _src.location_id
                    _ev = Harvest(
                        batch_id=_src_batch, event_date=_aw_start,
                        source_tank_id=_src_tid, count=_src_cnt,
                        avg_wt_g=_src.avg_wt_g, min_tank_control=0)
                    warnings.extend(_ev.apply(state))
                    harvest_events.append(_ev)
                    _budget.record(_ev.count)
                    _persist_tank_reserve(
                        _src_tid, week_label, _wk,
                        sorted_weeks, week_index_r, ta_index, week_tank_owner,
                        tank_assignments)
                    _reserved_for[_src_tid] = _wk
                    _reserved_og.add(_src_tid)
                    warnings.append(
                        f"{week_label}: anticipatory arrival freeing — "
                        f"harvested {_src_loc} (batch {_src_batch}, "
                        f"{_src_cnt:.0f} fish) within the weekly ceiling and "
                        f"RESERVED it for the TranOG arrival in {_wk} "
                        f"(needs {_need_wk})")
                    _deficit_wk -= 1

        # Day-by-day biology + TranOG entries within this week.
        ws_we = week_ranges.get(week_label)
        if ws_we is not None:
            ws_date, we_date = ws_we

            # PROACTIVE MAKE-ROOM (pre-biology). The facility is a conveyor
            # (OG1/2 -> ... -> OG6 -> harvest); harvest is biomass-driven, so
            # when biomass is under cap it lets near-market fish keep growing —
            # which OCCUPIES TANKS and can box out a TranOG arrival scheduled
            # THIS week, forcing the arrival to be DROPPED (losing its entire
            # stocked population — an input-fish-conservation breach; see
            # InputConservationAudit). Before the week's biology runs, ensure
            # enough empty ENTRY-TIER (OG1/2) tanks for this week's arrivals
            # (R1): vacate entry occupants FORWARD into free grow-out tanks
            # (R2), freeing grow-out slots first via harvest / 6N purge of the
            # readiest grow-out tanks where needed (entry tanks themselves are
            # never harvested — R5). Done PRE-biology at week-open weight so it
            # matches the continuity audit's event order (harvest before
            # growth) and stays drift-free. Conserved via Harvest/Transfer
            # events.
            _arrivals = [s for s in splits
                         if s.batch_id in og_entry_day
                         and ws_date <= og_entry_day[s.batch_id] < we_date]
            if _arrivals:
                _need = 0
                for s in _arrivals:
                    _ta = next((a for a in tank_assignments
                                if a.week_label == week_label
                                and a.batch_id == s.batch_id), None)
                    _plan_n = len(_ta.tank_ids) if _ta and _ta.tank_ids else 0
                    _cohort_kg = s.post_cull_count * (s.post_cull_avg_wt_g / 1000.0)
                    _need += _tranog_tank_need(_cohort_kg, facility, control, _plan_n)
                # R1: arrivals may land ONLY in the entry tier (OG1/2), so the
                # deficit is measured against EMPTY ENTRY tanks, and make-room
                # means VACATING an entry tank: move its fish FORWARD (R2 —
                # legal at any weight) into a free grow-out tank — preferring
                # the tank(s) the anticipatory pass reserved for this very
                # arrival. If no grow-out tank is free, first free one via the
                # existing grow-out make-room (purge: move into 6N; production:
                # whole-tank harvest — entry sources excluded per R5), then
                # forward-move the entry occupant into it.
                _empty_entry = [t for t in state.tanks_by_id.values()
                                if t.is_empty and t.type == "OG"
                                and t.system_id in OG12_SYSTEMS]
                _deficit = _need - len(_empty_entry)
                _min_hv_wt = control.min_harvest_weight_g or 0

                def _consolidate_entry_forward() -> bool:
                    """Vacate ONE entry tank by topping up SAME-BATCH grow-out
                    tanks with density headroom — a forward move (R2, legal at
                    any weight), INV-1-safe, and a WHOLE-TANK move so the
                    min-transfer split floor does not apply.

                    ABORT-PREVENTION ONLY: fires exclusively when the entry
                    tier is COMPLETELY full — i.e. the arrival would otherwise
                    hit the hard no-drop abort. A mere tank-count deficit with
                    at least one free entry tank keeps the historical
                    behaviour (the cohort crams into fewer tanks), because
                    firing on non-fatal deficits measurably reshaped no-window
                    trajectories through the PR_CORRECTION trial evaluator.

                    The measured case (2026-W50 abort, 7.29.26 PR + operator
                    window): OG1N-15 held 765 fish of B48 and OG1S-16 held 474
                    of B47 while both batches had grow-out headroom — the two
                    tanks the arrival needed. Smallest occupant first (least
                    handling, likeliest to fit). Returns True if an entry
                    tank was freed."""
                    if any(t.is_empty and t.type == "OG"
                           and t.system_id in OG12_SYSTEMS
                           for t in state.tanks_by_id.values()):
                        return False   # not abort-bound — keep legacy behaviour
                    _occ = sorted(
                        [t for t in state.tanks_by_id.values()
                         if not t.is_empty and t.type == "OG"
                         and t.system_id in OG12_SYSTEMS
                         and t.stage != STAGE_STARVE],
                        key=lambda t: (t.count, t.tank_id))
                    for _se in _occ:
                        if _se.avg_wt_g <= 0:
                            continue
                        _room = []
                        for _d in sorted(state.tanks_by_id.values(),
                                         key=lambda x: x.tank_id):
                            if (_d.is_empty or _d.batch_id != _se.batch_id
                                    or _d.type != "OG"
                                    or _d.system_id in _SIXN_SYSTEMS
                                    or _d.system_id in OG12_SYSTEMS
                                    or _d.stage == STAGE_STARVE
                                    or _d.max_density_kg_m3 <= 0):
                                continue
                            _head_kg = _d.max_biomass_kg * 0.98 - _d.biomass_kg
                            if _head_kg <= 0:
                                continue
                            _room.append(
                                (_d, _head_kg / (_se.avg_wt_g / 1000.0)))
                        if sum(r for _, r in _room) < _se.count:
                            continue  # this batch can't absorb the whole tank
                        _allocs, _left = [], _se.count
                        for _d, _r in _room:
                            _take = min(_left, _r)
                            if _take <= 0:
                                break
                            _allocs.append(TankAllocation(
                                tank_id=_d.tank_id, count=_take,
                                avg_wt_g=_se.avg_wt_g, cv_pct=_se.cv_pct))
                            _left -= _take
                        _sb, _sl, _sn = _se.batch_id, _se.location_id, _se.count
                        _cmv = Transfer(
                            batch_id=_sb, event_date=ws_date,
                            source_tank_id=_se.tank_id,
                            destinations=_allocs, leaves_source_empty=True)
                        warnings.extend(_cmv.apply(state))
                        transfer_events.append(_cmv)
                        if not state.tanks_by_id[_se.tank_id].is_empty:
                            continue  # refused — try the next occupant
                        warnings.append(
                            f"{week_label}: VACATED entry tank {_sl} (batch "
                            f"{_sb}, {_sn:.0f}-fish remnant absorbed) by "
                            f"CONSOLIDATING forward into its own grow-out "
                            f"tanks (R2) — freeing the entry tier for "
                            f"{len(_arrivals)} TranOG arrival(s) this week")
                        return True
                    return False

                while _deficit > 0:
                    # Entry occupants that can be vacated (readiest first —
                    # biggest fish are nearest their forward move anyway).
                    _entry_occ = sorted(
                        [t for t in state.tanks_by_id.values()
                         if not t.is_empty and t.type == "OG"
                         and t.system_id in OG12_SYSTEMS
                         and t.stage != STAGE_STARVE],
                        key=lambda t: (-t.avg_wt_g, t.tank_id))
                    if not _entry_occ:
                        break  # nothing to vacate — arrival shortfall handled below
                    # Forward-move destination: a free grow-out tank; reserved
                    # (anticipatory-freed) tanks first — this arrival is what
                    # they were held for.
                    _free_grow = sorted(
                        [t for t in state.tanks_by_id.values()
                         if t.is_empty and t.type == "OG"
                         and t.system_id not in _SIXN_SYSTEMS
                         and t.system_id not in OG12_SYSTEMS],
                        key=lambda t: (t.tank_id not in _reserved_og, t.tank_id))
                    if not _free_grow:
                        # No grow-out slot free — free one first (grow-out
                        # sources only; R5 keeps entry tanks out of this pool).
                        _cands = [t for t in state.tanks_by_id.values()
                                  if not t.is_empty and t.type == "OG"
                                  and t.system_id not in _SIXN_SYSTEMS
                                  and t.system_id not in OG12_SYSTEMS
                                  and t.stage != STAGE_STARVE  # purge-pipeline-owned
                                  and t.avg_wt_g >= _min_hv_wt]
                        if not _cands:
                            # LAST RESORT: the purge pipeline's own (STARVE)
                            # tanks — conservation (never drop an arrival)
                            # outranks pipeline ownership. Prefer READY tanks
                            # (purge complete, harvested next anyway); an
                            # unfinished purge tank is taken only when nothing
                            # else can free a slot.
                            _cands = [t for t in state.tanks_by_id.values()
                                      if not t.is_empty and t.type == "OG"
                                      and t.system_id not in _SIXN_SYSTEMS
                                      and t.system_id not in OG12_SYSTEMS
                                      and t.stage == STAGE_STARVE
                                      and t.avg_wt_g >= _min_hv_wt]
                            if not _cands:
                                # FORWARD-CONSOLIDATION VACATE: nothing is
                                # harvest-ripe and no tank is empty, but an
                                # entry tank whose batch has grow-out headroom
                                # can still vacate by same-batch top-up
                                # (strictly additive — only tried where the
                                # ladder previously gave up and aborted).
                                if _consolidate_entry_forward():
                                    _deficit -= 1
                                    continue
                                break  # genuinely saturated — abort handled below
                            _cands.sort(key=lambda t: (
                                t.starvation_days_remaining, -t.avg_wt_g, t.tank_id))
                        elif _level_load:
                            # Under level-load, free the SMALLEST harvestable tank first
                            # so the whole-tank make-room dump (which exceeds the cap) is
                            # as SMALL as possible — i.e. drain the cheap remnant tanks
                            # as the escape valve rather than the readiest/fullest tank.
                            # This is the dominant spike lever on a tank-tight facility:
                            # the make-room dump size IS the spike, so minimizing it
                            # flattens harvest (config(8): CV 0.215->0.157, max 86k->67k).
                            # Default (no level-load) keeps harvesting the readiest
                            # (nearest-market) tank, so OFF behaviour is unchanged.
                            _cands.sort(key=lambda t: (t.count, t.avg_wt_g, t.tank_id))
                        else:
                            _cands.sort(key=lambda t: (-t.avg_wt_g, t.tank_id))
                        _src = _cands[0]
                        # PURGE MODE — the operator rule is absolute: never harvest a
                        # production tank directly. MOVE its fish into 6N to purge, which
                        # both frees the tank AND routes the fish to harvest via the
                        # rotation. The anticipatory pacing pass above should have
                        # already freed a slot; this is the reactive backstop. If 6N is
                        # full this exact week, leave the tank and warn — a real 6N
                        # capacity signal; the hard no-drop invariant below catches a
                        # genuine loss.
                        if purge_this_week:
                            if not _make_room_into_6n(
                                    state, _src, ws_date, sixn_resting_pair,
                                    transfer_events, warnings, week_label,
                                    reason="reactive make-room for a TranOG arrival",
                                    sixn_move_in_feed=sixn_move_in_feed, tables=tables,
                                    batch_meta=batch_meta, is_purge=True,
                                    avoid=_sixn_avoid):
                                # 6N full — last try: forward-consolidation
                                # vacate (same-batch top-up needs no empty
                                # tank and no 6N slot).
                                if _consolidate_entry_forward():
                                    _deficit -= 1
                                    continue
                                warnings.append(
                                    f"{week_label}: make-room CANNOT free "
                                    f"{_src.location_id} for a TranOG arrival — 6N is full "
                                    f"and direct production harvest is forbidden in purge "
                                    f"mode (6N capacity signal)")
                                break
                            continue  # freed a grow-out slot; forward-move next pass
                        # PRODUCTION MODE — harvest the WHOLE tank directly (a partial
                        # wouldn't free it). This pass may exceed the weekly ceiling:
                        # dropping a stocked TranOG batch is worse and unrecoverable; the
                        # overage is borrowed from next week's ceiling (_carry_debt).
                        _over = max(0.0, _budget.used + _src.count - _budget.cap)
                        _ev = Harvest(
                            batch_id=_src.batch_id, event_date=ws_date,
                            source_tank_id=_src.tank_id, count=_src.count,
                            avg_wt_g=_src.avg_wt_g, min_tank_control=0,
                        )
                        warnings.extend(_ev.apply(state))
                        harvest_events.append(_ev)
                        _budget.record(_ev.count)
                        warnings.append(
                            f"{week_label}: proactive MAKE-ROOM harvest of "
                            f"{_src.location_id} (batch {_src.batch_id}, "
                            f"{_src.count:.0f} fish @ {_src.avg_wt_g / 1000:.2f}kg) — "
                            f"freeing a grow-out tank so an entry tank can be vacated "
                            f"for {len(_arrivals)} TranOG arrival(s) this week"
                        )
                        if _level_load and _over > 0:
                            warnings.append(
                                f"{week_label}: make-room exceeded the weekly harvest "
                                f"cap by {_over:.0f} fish to free a tank for a TranOG "
                                f"arrival — conservation takes priority; the overage is "
                                f"borrowed from next week's ceiling"
                            )
                        continue  # freed a grow-out slot; forward-move next pass
                    _dst = _free_grow[0]
                    # A reserved destination is consumed for its held purpose —
                    # release it so Transfer.apply accepts the stock.
                    if _dst.tank_id in _reserved_og:
                        _reserved_for.pop(_dst.tank_id, None)
                        _reserved_og.discard(_dst.tank_id)
                        state.reserved_tanks = _reserved_og
                    _src_e = _entry_occ[0]
                    _se_batch, _se_loc = _src_e.batch_id, _src_e.location_id
                    _mv = Transfer(
                        batch_id=_se_batch, event_date=ws_date,
                        source_tank_id=_src_e.tank_id,
                        destinations=[TankAllocation(
                            tank_id=_dst.tank_id, count=_src_e.count,
                            avg_wt_g=_src_e.avg_wt_g, cv_pct=_src_e.cv_pct)],
                        leaves_source_empty=True,
                    )
                    warnings.extend(_mv.apply(state))
                    transfer_events.append(_mv)
                    if not state.tanks_by_id[_src_e.tank_id].is_empty:
                        warnings.append(
                            f"{week_label}: entry-tank vacate {_se_loc} -> "
                            f"{_dst.location_id} was refused; cannot free the entry "
                            f"tier for this week's TranOG arrival(s)")
                        break
                    warnings.append(
                        f"{week_label}: VACATED entry tank {_se_loc} "
                        f"(batch {_se_batch}) forward into {_dst.location_id} "
                        f"(R1/R2) — freeing the entry tier for {len(_arrivals)} "
                        f"TranOG arrival(s) this week")
                    _deficit -= 1

            day = ws_date
            while day < we_date:
                # Apply TranOG entries scheduled for this day (the OG-entry
                # week start, not the raw TranOG_Date — see og_entry_day).
                for split in splits:
                    if og_entry_day.get(split.batch_id) != day:
                        continue
                    # Find this batch's Phase C tank assignment for THIS week.
                    ta = next(
                        (a for a in tank_assignments
                         if a.week_label == week_label and a.batch_id == split.batch_id),
                        None,
                    )
                    if ta is None:
                        # No Phase-C tank-assignment row for this arrival's
                        # entry week: the batch cannot be placed here. The
                        # input-conservation audit will report it as dropped;
                        # name the CAUSE at the site instead of leaving only
                        # the downstream symptom.
                        warnings.append(
                            f"{week_label}: TranOG arrival {split.batch_id} on "
                            f"{day} has NO Phase-C tank assignment this week — "
                            f"entry skipped; will surface as an "
                            f"input-conservation failure")
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
                    n_needed = _tranog_tank_need(cohort_kg, facility, control, plan_n)
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
                        # NO OG3+ overflow (R1): TranOG arrivals may enter ONLY
                        # the entry tier (OG1/2) — placing an arrival directly
                        # in OG3+ is physically impossible. If the entry tier is
                        # exhausted even after the pre-emptive eviction pass and
                        # the make-room passes, fall through to the loud no-drop
                        # abort below (an unrunnable scenario, not a forecast).
                    if not tanks_obj:
                        # HARD NO-DROP INVARIANT. A stocked, in-horizon batch is
                        # input fish; silently dropping it (the old `continue`)
                        # produced a forecast that LOST FISH without failing the
                        # continuity audit (a never-placed batch has no tank rows
                        # to drift). The anticipatory purge pacing + reactive
                        # make-room above should always free a slot in time; if we
                        # genuinely reach here the facility is saturated AND no fish
                        # are harvestable to purge, which is an unrunnable scenario,
                        # not a forecast — so we ABORT loudly rather than ship a
                        # silent loss. Names batch / arrival day / required vs free.
                        _free_entry = sum(
                            1 for t in state.tanks_by_id.values()
                            if t.is_empty and t.type == "OG"
                            and t.system_id in OG12_SYSTEMS)
                        raise RuntimeError(
                            f"{week_label}: TranOG arrival {split.batch_id} on "
                            f"{day} CANNOT be placed — needs {n_needed} entry-tier "
                            f"(OG1/2) tank(s), {_free_entry} free, and arrivals may "
                            f"enter ONLY the entry tier (rule R1); the eviction, "
                            f"anticipatory pacing and entry-vacate make-room passes "
                            f"could not free one (6N saturated / no harvestable "
                            f"fish to purge / no grow-out slot to move entry fish "
                            f"forward into). Refusing to drop "
                            f"{split.post_cull_count:.0f} stocked fish silently. "
                            f"Operator action required: add 6N depuration capacity, "
                            f"raise the facility biomass cap, or re-time the "
                            f"TranOG arrival schedule."
                        )
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
                        # REMNANT FLOOR on the arrival split: never CREATE a
                        # tank occupancy under min_tank_control when avoidable.
                        # Shrink each class's tank count until its per-tank
                        # share clears the floor (a final short split goes into
                        # the previous destination instead of its own tank).
                        # A class whose WHOLE population is under the floor
                        # keeps 1 tank — unavoidable, INV-1 forbids mixing.
                        _mtc = control.min_tank_control or 0.0
                        if _mtc > 0:
                            while (big_n > 1
                                   and split.big_class_count / big_n < _mtc):
                                big_n -= 1
                            while (small_n > 1
                                   and split.small_class_count / small_n < _mtc):
                                small_n -= 1
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
                # Apply continuous biology. Record the REALIZED biomass delta
                # (growth - mortality) and mortality count per (tank, week, batch)
                # so the continuity audit can reconcile against ground truth
                # instead of independently re-estimating growth from a coarse
                # weekly SGR (which false-positives for split-off sub-populations).
                for tank in state.tanks_by_id.values():
                    if tank.is_empty:
                        continue
                    bm = batch_meta.get(tank.batch_id)
                    if bm is None:
                        continue
                    _bid = tank.batch_id
                    _c0, _b0 = tank.count, tank.count * tank.avg_wt_g / 1000.0
                    advance_tank_one_day(tank, bm, tables, day)
                    _rb = realized_biology[(tank.tank_id, week_label, _bid)]
                    _rb[0] += (tank.count * tank.avg_wt_g / 1000.0) - _b0  # bio delta
                    _rb[1] += _c0 - tank.count                            # mort count
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
             and t.system_id != "OG6N"
             and t.tank_id not in _reserved_og],  # held for imminent TranOG
            key=lambda t: t.tank_id,
        )
        for tank in sorted(state.tanks_by_id.values(),
                           key=lambda t: t.density_kg_m3, reverse=True):
            if tank.is_empty:
                continue
            # R7: never grade-split a 6N DEPURATION tank (STARVE) — fish
            # committed to 6N may only be harvested. Pre-R7 this pass would
            # quietly pull half of an over-dense purge tank back into
            # grow-out (STARVE stage leaking with it — frozen, unfed fish in
            # a production tank); its density is governed by the sister-first
            # fill cap, with the no-drop make-room overflow as the one
            # documented exception (reported by the density gate, not
            # "relieved" by breaking the commitment).
            if not sixn_exit_allowed(tank.system_id, tank.stage):
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
            # REMNANT FLOOR: a half/half grade of a tank under 2x the floor
            # would CREATE two sub-min tanks — worse than the density it
            # relieves (a tank below min_tank_control is not worth operating).
            if (control.min_tank_control
                    and tank.count < 2 * control.min_tank_control):
                warnings.append(
                    f"{week_label}: tank {tank.location_id} over density "
                    f"trigger but grade-split declined — {tank.count:.0f} fish "
                    f"would split into two tanks under min_tank_control "
                    f"{control.min_tank_control:.0f}")
                continue
            # Pick a destination the tier rules allow from this source tank
            # (R2-R4 via tiers.move_allowed): no backward split into entry
            # from a non-entry source, no intra-entry split at >= 1 kg.
            tank_sys = tank.system_id
            candidate_dest = None
            for i, d in enumerate(grade_dest_pool):
                if not move_allowed(tank_sys, d.system_id, tank.avg_wt_g)[0]:
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

        # ---- END-OF-WEEK CAP REPAIR (opt-in: control.cap_repair_budget) ----
        # LAST pass of the week, on the post-growth / post-grade state the
        # snapshot below records — the only point where "is this system over
        # cap?" is asked of the state the audit will actually measure. See the
        # _REPAIR_* block near the top of this module for the measurement that
        # motivates it. Deferrable: it spends only what is left of the weekly
        # handling budget, and because nothing follows it that budget is exact.
        _repair_budget = min(
            int(getattr(control, "cap_repair_budget", 0) or 0), _moves_left())
        if _rebal_on and _repair_budget > 0 and ws_we is not None:
            _repair_over_cap_systems(
                state, week_label, ws_we[0], transfer_events, warnings,
                _sys_cap, batch_meta, tables, _og_sys, _og_tanks,
                _repair_budget,
                reserved=_reserved_og,
                min_transfer=getattr(control, "min_transfer_count", 0.0) or 0.0,
                min_keep=control.min_tank_control or 0.0,
            )

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

        # Borrow any over-cap fish this week took (make-room exception / INV-5
        # force-empty) from next week's ceiling, so the multi-week harvest total
        # stays within cap x weeks. 0.0 in the common case.
        _carry_debt = _budget.overdraw

    return (state, tranog_events, transfer_events, harvest_events,
            grade_events, locations, warnings, sixn_move_in_feed,
            dict(realized_biology))


# ============================================================
# Orchestrator
# ============================================================

def fw_addends_by_week(
    biology_states_by_batch: dict[str, list[BatchWeekState]],
) -> tuple[dict[str, float], dict[str, float]]:
    """Per-week-label FW/EGG biomass (kg) + feed (kg/day).

    FW-INCLUSIVE cap basis (audit H1/M5): pre-feed batches (EGG/FW stages) are
    real facility biomass but are NEVER stocked into tanks, so the realized
    harvest controller's tank-only biomass omits them. Summing them here lets
    the setpoint + the feed-implied cap measure TOTAL facility biomass (OG+FW),
    not OG-only — else true biomass rides over the 3.8M cap by the FW load
    (~4-7%) at setpoint.

    Extracted so the hybrid guide's L1 pre-pass can measure the SAME FW load
    rather than recomputing it under a manual-window-shifted forecast start.
    """
    fw_biomass_by_week: dict[str, float] = {}
    fw_feed_by_week: dict[str, float] = {}
    for _sts in biology_states_by_batch.values():
        for _s in _sts:
            if getattr(_s, "stage", None) in ("FW", "EGG"):
                fw_biomass_by_week[_s.week_label] = (
                    fw_biomass_by_week.get(_s.week_label, 0.0) + _s.biomass_kg)
                fw_feed_by_week[_s.week_label] = (
                    fw_feed_by_week.get(_s.week_label, 0.0)
                    + getattr(_s, "feed_kg_day", 0.0))
    return fw_biomass_by_week, fw_feed_by_week


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
    harvest_guide=None,
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

    fw_biomass_by_week, fw_feed_by_week = fw_addends_by_week(
        biology_states_by_batch)

    (final_state, tranog, transfers, harvests, grades, locs, d_warns,
     sixn_feed, realized_bio) = phase_d_emit_events(
        result.load_table, result.tank_assignments, harvest_demands,
        splits, initial_state, facility, control, batch_meta, tables,
        facility_limits=facility_limits,
        system_limits=system_limits,
        fw_biomass_by_week=fw_biomass_by_week,
        fw_feed_by_week=fw_feed_by_week,
        harvest_guide=harvest_guide,
    )
    result.tranog_events = tranog
    result.transfer_events = transfers
    result.harvest_events = harvests
    result.grade_events = grades
    result.batch_locations = locs
    result.sixn_move_in_feed = sixn_feed
    result.realized_biology = realized_bio
    result.warnings.extend(f"[D] {w}" for w in d_warns)

    # NOTE: opt-in LNS placement refinement (placement_method=="lns") is applied
    # ONE level up, in run.py, AFTER this greedy plan is realized — it relocates
    # realized grow-out occupancy IN PLACE (no second placement run), re-keying the
    # batch_locations, every event stream AND realized_biology together, gated on the
    # continuity audit (greedy is the warm start + fallback). run_placement itself
    # stays a pure realizer of whatever plan it is given. See lns_placement.
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
