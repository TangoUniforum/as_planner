"""STANDALONE proof-of-concept: a "tankless global" SYSTEM assigner (Layer 2).

METHOD: GLOBAL L2 (system assignment)
=====================================

L1 (`forecast.global_planner_poc`) produces the facility-level **harvest
envelope** + per-(batch, week) *standing* population (biomass / count / mean
weight) that rides the facility caps, treating the farm as one global pool.
L2 takes that standing population and **assigns each batch to a SYSTEM** each
week, honouring the conveyor flow direction and the per-system biomass + feed
caps — without modelling individual tanks (that is L3).

This is deliberately ADDITIVE: it imports L1 + the existing config / scenario /
caps primitives and re-uses them verbatim. It re-implements no biology. It does
not touch the production pipeline, placement, or scheduler. Nothing here is
imported by `forecast/run.py`.

The conveyor (flow forward only)
--------------------------------
Fish flow FW -> nursery -> grow-out -> 6N (purge / harvest staging), never
backward. Tiers (by mean weight, the "1 kg lock"):

  * NURSERY  = OG1N OG1S OG2N OG2S   (fish < 1 kg)
  * GROW-OUT = OG3N OG3S OG4N OG4S OG5N OG5S OG6S   (fish >= 1 kg)
  * PURGE    = OG6N   (harvest staging; handled by L1's harvest, not stocked here)

A batch whose mean weight crosses ~1 kg is promoted from nursery to grow-out
(the documented 1 kg lock). Because L1 already removes the harvest envelope, a
6N depuration tier is not stocked here — L2 places the *standing* fish that L1
keeps, and the 6N rotation is an L3 concern.

Per-system caps
---------------
Each (week, system) has a biomass cap and a feed/day cap (read from
`scenario/limits.yaml` via `forecast.caps.SystemLimits`; grow-out feed cap is
~3,000 kg/day/system here). A system is over-cap if assigned biomass or feed/day
exceeds its cap (the R29 global buffer is reported but the raw cap is the
headroom reference).

Distribute (the mix-and-match) — deterministic, greedy, no search
-----------------------------------------------------------------
Week by week, for each tier, sort the tier's batches largest-first (oldest /
closest-to-harvest get a seat first) and assign each to ONE system:

  1. Prefer the system the batch occupied LAST week if it still has headroom
     (don't thrash — full transfer-minimisation is L3's job, but L2 shouldn't
     churn needlessly).
  2. Otherwise pick the **least-loaded eligible** system with headroom, where
     "least-loaded" is measured by feed-fill ratio first (feed binds before
     biomass at this density), biomass-fill ratio as a tiebreak.

If NO system in the tier has headroom for a batch, it OVERFLOWS: a >= 1 kg
grow-out batch spills into the nursery tier (the documented OG2N-style
overflow); a nursery batch that cannot fit is recorded as a hard overflow. Each
overflow is an explicit OVERFLOW event (kg + tier + reason) — the per-system
feasibility signal, the analogue of L1's facility infeasibility verdict. The
batch is still PLACED (into its least-loaded system, accepting the over-cap) so
conservation holds and the over-cap is quantified rather than silently dropped.

Output
------
`tools/run_l2_poc.py` prints + writes a small .xlsx with the per-(system, week)
load trace (biomass + feed vs caps, OVER flag), the overflow report, and the
per-(batch, week) assignment, plus the conservation check
(Sum over systems of assigned biomass == L1's facility standing each week).

What this is NOT
----------------
L2 is system assignment only. It does NOT place fish in individual tanks, model
density per tank, model the 125%-staged harvest tank, or run the 6N pair
rotation — those are L3. It assigns one system per batch per week (no
mid-week splitting of a single batch across systems beyond the overflow case).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .caps import (
    METRIC_BIOMASS,
    METRIC_FEED_DAY,
    SystemLimits,
    resolve_system_cap,
)
from .global_planner_poc import BatchStandingRow, PlannerResult
from .models import ControlParams, FacilityConfig


# ---------------------------------------------------------------------------
# Conveyor tier definitions
# ---------------------------------------------------------------------------

NURSERY_SYSTEMS = ["OG1N", "OG1S", "OG2N", "OG2S"]
GROWOUT_SYSTEMS = ["OG3N", "OG3S", "OG4N", "OG4S", "OG5N", "OG5S", "OG6S"]
PURGE_SYSTEMS = ["OG6N"]

# The "1 kg lock": a batch is promoted nursery -> grow-out when its mean
# weight crosses this threshold (grams).
ONE_KG_LOCK_G = 1000.0

TIER_NURSERY = "nursery"
TIER_GROWOUT = "growout"


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------

@dataclass
class SystemLoadRow:
    """Per-(system, week) assigned load vs its caps."""
    week: int
    week_label: str
    system_id: str
    tier: str
    biomass_kg: float
    feed_kg_day: float
    biomass_cap: Optional[float]
    feed_cap: Optional[float]
    n_batches: int
    over_biomass: bool
    over_feed: bool
    biomass_ratio: float            # assigned / cap (0 if no cap)
    feed_ratio: float


@dataclass
class OverflowRow:
    """An explicit overflow event: a tier had no headroom for a batch."""
    week: int
    week_label: str
    batch_id: str
    from_tier: str
    biomass_kg: float
    spilled_to_system: str
    reason: str


@dataclass
class AssignmentRow:
    """Per-(batch, week): which system it occupies + biomass there."""
    week: int
    week_label: str
    batch_id: str
    tier: str
    system_id: str
    biomass_kg: float
    feed_kg_day: float
    avg_wt_g: float
    overflowed: bool


@dataclass
class L2Result:
    loads: list[SystemLoadRow]
    overflows: list[OverflowRow]
    assignments: list[AssignmentRow]
    conservation: list[dict]        # per-week Sum(systems) vs L1 standing
    systems: list[str]
    feasible: bool                  # no system-week over either cap
    over_system_weeks: int
    worst_biomass_ratio: float
    worst_feed_ratio: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def og_systems_from_facility(facility: FacilityConfig) -> list[str]:
    """OG system ids present in the facility, in canonical conveyor order."""
    present = {t.system_id for t in facility.tanks if t.type == "OG"}
    ordered = NURSERY_SYSTEMS + GROWOUT_SYSTEMS + PURGE_SYSTEMS
    return [s for s in ordered if s in present]


def _tier_for_weight(avg_wt_g: float) -> str:
    return TIER_GROWOUT if avg_wt_g >= ONE_KG_LOCK_G else TIER_NURSERY


def _tier_systems(tier: str, available: set[str]) -> list[str]:
    base = NURSERY_SYSTEMS if tier == TIER_NURSERY else GROWOUT_SYSTEMS
    return [s for s in base if s in available]


@dataclass
class _SysState:
    """Mutable per-system accumulator for one week."""
    system_id: str
    biomass: float = 0.0
    feed: float = 0.0
    n: int = 0
    bio_cap: Optional[float] = None
    feed_cap: Optional[float] = None

    def fits(self, add_bio: float, add_feed: float) -> bool:
        """True if adding (add_bio, add_feed) keeps BOTH caps satisfied.

        A None cap means no cap set -> always fits on that metric.
        """
        if self.bio_cap is not None and self.biomass + add_bio > self.bio_cap:
            return False
        if self.feed_cap is not None and self.feed + add_feed > self.feed_cap:
            return False
        return True

    def feed_fill(self) -> float:
        return (self.feed / self.feed_cap) if self.feed_cap else 0.0

    def bio_fill(self) -> float:
        return (self.biomass / self.bio_cap) if self.bio_cap else 0.0

    def add(self, bio: float, feed: float) -> None:
        self.biomass += bio
        self.feed += feed
        self.n += 1


# ---------------------------------------------------------------------------
# The assigner
# ---------------------------------------------------------------------------

def assign(
    l1: PlannerResult,
    control: ControlParams,
    facility: FacilityConfig,
    system_limits: SystemLimits,
) -> L2Result:
    """Assign L1's per-(batch, week) standing population to systems.

    Requires `l1` to have been produced by `plan(..., record_standing=True)`.
    Greedy least-loaded assignment, week by week, feed-balanced first, sticky to
    last week's system. See module docstring.
    """
    systems = og_systems_from_facility(facility)
    available = set(systems)

    # Group L1 standing rows by week.
    by_week: dict[int, list[BatchStandingRow]] = {}
    week_label: dict[int, str] = {}
    for r in l1.batch_standing:
        by_week.setdefault(r.week, []).append(r)
        week_label[r.week] = r.week_label

    loads: list[SystemLoadRow] = []
    overflows: list[OverflowRow] = []
    assignments: list[AssignmentRow] = []
    conservation: list[dict] = []

    # Last-week placement for stickiness: batch_id -> system_id.
    last_system: dict[str, str] = {}

    over_system_weeks = 0
    worst_bio_ratio = 0.0
    worst_feed_ratio = 0.0

    for w in sorted(by_week):
        label = week_label[w]
        rows = by_week[w]

        # Build fresh per-system accumulators with this week's caps.
        state: dict[str, _SysState] = {}
        for sid in systems:
            state[sid] = _SysState(
                system_id=sid,
                bio_cap=resolve_system_cap(METRIC_BIOMASS, label, sid, system_limits),
                feed_cap=resolve_system_cap(METRIC_FEED_DAY, label, sid, system_limits),
            )

        new_last: dict[str, str] = {}

        # Process largest batches first (closest to harvest get a seat first).
        rows_sorted = sorted(rows, key=lambda r: (-r.avg_wt_g, r.batch_id))
        for r in rows_sorted:
            tier = _tier_for_weight(r.avg_wt_g)
            tier_sys = _tier_systems(tier, available)
            chosen, overflowed, ov_reason = _choose_system(
                r, tier, tier_sys, state, available, last_system.get(r.batch_id))
            st = state[chosen]
            st.add(r.biomass_kg, r.feed_kg_day)
            new_last[r.batch_id] = chosen
            assignments.append(AssignmentRow(
                week=w, week_label=label, batch_id=r.batch_id, tier=tier,
                system_id=chosen, biomass_kg=r.biomass_kg,
                feed_kg_day=r.feed_kg_day, avg_wt_g=r.avg_wt_g,
                overflowed=overflowed,
            ))
            if overflowed:
                spill_tier = (TIER_NURSERY if tier == TIER_GROWOUT
                              else tier)
                overflows.append(OverflowRow(
                    week=w, week_label=label, batch_id=r.batch_id,
                    from_tier=tier, biomass_kg=r.biomass_kg,
                    spilled_to_system=chosen, reason=ov_reason,
                ))

        last_system = new_last

        # Emit the per-system load trace for this week.
        wk_over = False
        for sid in systems:
            st = state[sid]
            tier = (TIER_NURSERY if sid in NURSERY_SYSTEMS
                    else TIER_GROWOUT if sid in GROWOUT_SYSTEMS else "purge")
            over_b = st.bio_cap is not None and st.biomass > st.bio_cap + 1e-6
            over_f = st.feed_cap is not None and st.feed > st.feed_cap + 1e-6
            b_ratio = st.bio_fill()
            f_ratio = st.feed_fill()
            if over_b or over_f:
                wk_over = True
            worst_bio_ratio = max(worst_bio_ratio, b_ratio)
            worst_feed_ratio = max(worst_feed_ratio, f_ratio)
            loads.append(SystemLoadRow(
                week=w, week_label=label, system_id=sid, tier=tier,
                biomass_kg=st.biomass, feed_kg_day=st.feed,
                biomass_cap=st.bio_cap, feed_cap=st.feed_cap, n_batches=st.n,
                over_biomass=over_b, over_feed=over_f,
                biomass_ratio=b_ratio, feed_ratio=f_ratio,
            ))
        if wk_over:
            over_system_weeks += 1

        # Conservation: Sum over systems of assigned biomass == L1 facility standing.
        sys_total = sum(state[sid].biomass for sid in systems)
        l1_standing = sum(r.biomass_kg for r in rows)
        conservation.append({
            "week": w,
            "week_label": label,
            "system_total_kg": sys_total,
            "l1_standing_kg": l1_standing,
            "diff_kg": sys_total - l1_standing,
        })

    feasible = over_system_weeks == 0
    return L2Result(
        loads=loads, overflows=overflows, assignments=assignments,
        conservation=conservation, systems=systems, feasible=feasible,
        over_system_weeks=over_system_weeks,
        worst_biomass_ratio=worst_bio_ratio, worst_feed_ratio=worst_feed_ratio,
    )


def _choose_system(
    r: BatchStandingRow,
    tier: str,
    tier_sys: list[str],
    state: dict[str, _SysState],
    available: set[str],
    sticky: Optional[str],
) -> tuple[str, bool, str]:
    """Pick a system for batch row `r`. Returns (system_id, overflowed, reason).

    1. Stay in last week's system if it's in this tier and still fits (sticky).
    2. Else least-loaded eligible system (feed-fill first, biomass-fill tiebreak).
    3. Else OVERFLOW: spill a grow-out batch into nursery if a nursery system
       fits; otherwise place into the globally least-loaded system in the tier
       (or, as last resort, any system) accepting the over-cap.
    """
    add_b, add_f = r.biomass_kg, r.feed_kg_day

    # 1) Stickiness.
    if sticky and sticky in tier_sys and state[sticky].fits(add_b, add_f):
        return sticky, False, ""

    # 2) Least-loaded eligible in-tier system with headroom.
    fitting = [s for s in tier_sys if state[s].fits(add_b, add_f)]
    if fitting:
        best = min(fitting, key=lambda s: (state[s].feed_fill(),
                                           state[s].bio_fill(), s))
        return best, False, ""

    # 3) Overflow. A >= 1 kg grow-out batch spills into the nursery tier (the
    #    documented OG2N-style overflow) if a nursery system has headroom.
    if tier == TIER_GROWOUT:
        nursery_sys = _tier_systems(TIER_NURSERY, available)
        nfit = [s for s in nursery_sys if state[s].fits(add_b, add_f)]
        if nfit:
            best = min(nfit, key=lambda s: (state[s].feed_fill(),
                                            state[s].bio_fill(), s))
            return best, True, "growout full -> spilled into nursery"

    # 4) No tier (and no overflow target) has headroom. Place into the
    #    least-loaded in-tier system anyway and flag the over-cap, so the load
    #    is conserved and the over-stock is quantified (never dropped).
    pool = tier_sys or _tier_systems(TIER_NURSERY, available) or list(state)
    best = min(pool, key=lambda s: (state[s].feed_fill(),
                                     state[s].bio_fill(), s))
    return best, True, "all systems at cap -> placed over-cap"
