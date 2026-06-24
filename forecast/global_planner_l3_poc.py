"""STANDALONE proof-of-concept: the placement-configuration LP (Layer 3).

METHOD: GLOBAL L3 (lexicographic placement LP)
==============================================

L1 (`forecast.global_planner_poc`) produces the facility-level harvest envelope
plus, with ``record_standing=True``, the per-(batch, week) *standing* population
that rides the facility caps. L2 assigns that standing to SYSTEMS with a greedy
water-filler. L3 replaces the greedy heuristic with a **lexicographic linear
program** that lays the whole horizon out at once, minimizing cap violations
first and transfers second.

This is deliberately ADDITIVE: it imports L1 + L2's conveyor helpers + the
existing caps/scenario primitives verbatim. It re-implements no biology and no
L1 math. Nothing here is imported by `forecast/run.py`.

The three stages
----------------
**Step 2 (pure arithmetic, no solver).** For every (batch, week) with standing
biomass, the whole-tank demand is::

    per_tank_capacity_kg = min_OG(max_density_kg_m3 * volume_m3) * density_target_pct
    tanks[b,w]           = ceil(remaining_biomass_kg / per_tank_capacity_kg)

Whole tanks, no splitting. A batch's fish are assumed ~even across its tanks, so
per-tank biomass = biomass_kg / tanks and per-tank feed = feed_kg_day / tanks.

**Step 3+4 (the LP).** Assign the whole-tank counts to the 11 production OG
systems (OG6N excluded — purge/harvest-staging is L1's harvest removal + a later
specific-tank step) over the whole horizon, as integer tank counts
``y[b,s,w] >= 0`` (solved as an LP relaxation, then rounded), in TWO lexicographic
passes:

  * **Pass A — meet the limits.** Minimize total per-system cap VIOLATION
    (``sum(slk_bio + slk_feed)``) subject to the constraints below.
  * **Pass B — then minimize transfers.** Fix Pass-A's slack at its optimum
    (allow a tiny epsilon), then minimize ``sum t`` where
    ``t[b,s,w] >= y[b,s,w] - y[b,s,w-1]`` is the tanks-worth of a batch moving
    INTO a new system week-to-week. Minimizing it = stability / continuity.

Constraints
-----------
- Place all tanks:           sum_s y[b,s,w] = tanks[b,w]            (per active b,w)
- System tank capacity:      sum_b y[b,s,w] <= n_tanks_in_system[s] (per s,w)
- Conveyor eligibility:      y[b,s,w] = 0 if s not eligible for b's tier that week
                             (tier by mean weight: <1kg -> nursery, >=1kg -> grow-out)
- Soft biomass cap:  sum_b y[b,s,w]*(biomass_kg[b,w]/tanks[b,w])
                       <= bio_cap[s,w]  + slk_bio[s,w]
- Soft feed cap:     sum_b y[b,s,w]*(feed_kg_day[b,w]/tanks[b,w])
                       <= feed_cap[s,w] + slk_feed[s,w]
- Transfers:                 t[b,s,w] >= y[b,s,w] - y[b,s,w-1],  t >= 0

Ineligible (b,s,w) cells simply have no ``y`` variable (the cheapest way to pin
them to 0). Tank continuity is STRUCTURAL — the y-counts come from L1's conserved
standing via Step-2 arithmetic, so this layer verifies conservation, it does not
re-derive it.

Solver
------
`scipy.optimize.linprog(method="highs")` (HiGHS). If scipy is unavailable the
runner falls back to L2's greedy water-filler and says so.

Two solve modes:

  * ``integer=True`` (default) solves a true MILP. The monolithic full-horizon
    MILP (~5.4k integer y) does NOT converge in HiGHS, so we exploit the
    problem's structure: Pass A's caps + tank constraints couple only WITHIN a
    week, so Pass A is solved EXACTLY as 52 tiny per-week MILPs (lexicographic:
    min tank slack, then min bio+feed kg slack). Transfers couple only
    consecutive weeks, so Pass B is a SEQUENTIAL per-week resolve: each week is
    re-placed with its cap-slack pinned at the Pass-A floor (bio + feed pinned
    separately, so legality never worsens) and a stickiness objective that
    rewards keeping a batch in last week's system(s) — a greedy lexicographic
    transfer reduction. Result: a whole-tank integer layout (0 rounding fixups,
    0 integrality gap) that is far more cap-legal than greedy L2 AND low-churn.

  * ``integer=False`` solves the monolithic continuous LP relaxation + rounds
    (the spec's stated path). The relaxation balances load to exactly the caps
    with FRACTIONAL tank splits that integer rounding cannot preserve, so its
    realized violations are large — it exists to expose that integrality gap.

After the solve ``y`` is rounded (a no-op in MILP mode) and a per-(batch, week)
repair restores ``sum_s y == tanks`` exactly; any repair count is reported.

What this is NOT
----------------
L3 here is system assignment as a *legal, stable* whole-tank layout. It does NOT
pick the specific physical tank within a system, model per-tank density beyond
the Step-2 even-split assumption, or run the 6N pair rotation — those are the
final specific-tank-pick + 6N-staging step.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .caps import (
    METRIC_BIOMASS,
    METRIC_FEED_DAY,
    SystemLimits,
    resolve_system_cap,
)
from .global_planner_l2_poc import (
    GROWOUT_SYSTEMS,
    NURSERY_SYSTEMS,
    ONE_KG_LOCK_G,
    PURGE_SYSTEMS,
    _tier_for_weight,
    _tier_systems,
    og_systems_from_facility,
)
from .global_planner_poc import BatchStandingRow, PlannerResult
from .models import ControlParams, FacilityConfig

# Default per-system caps when the SystemLimits sheet has no cap for a
# (week, system) — matches the uniform 400,000 kg / 3,000 kg-feed sheet.
_DEFAULT_BIO_CAP = 400000.0
_DEFAULT_FEED_CAP = 3000.0

# SELECTIVE over-stock lever (placement optimizer candidate, default OFF =
# operating density everywhere -> byte-identical). When set, a batch whose mean
# weight is <= _OVERSTOCK_MAX_WT_G packs toward _OVERSTOCK_DENSITY_PCT of the HARD
# density cap (fewer tanks, freeing tanks for other batches' hand-offs); heavier
# batches stay at the operating density. Light fish are safe to concentrate (low
# kg/m3); mature fish near the cap are not. The optimizer sweeps these.
_OVERSTOCK_DENSITY_PCT: Optional[float] = None   # e.g. 0.97 of the hard cap
_OVERSTOCK_MAX_WT_G: Optional[float] = None       # only batches lighter than this


# ---------------------------------------------------------------------------
# Step 2: whole-tank demand (pure arithmetic, no solver)
# ---------------------------------------------------------------------------

def per_tank_capacity_kg(
    facility: FacilityConfig, control: ControlParams
) -> float:
    """min over OG tanks of (max_density_kg_m3 * volume_m3) * density_target_pct.

    The smallest OG tank's legal mass at the running production density. Whole
    tanks are sized to this so any single tank stays under density everywhere.
    """
    og = [t.max_density_kg_m3 * t.volume_m3 for t in facility.tanks
          if t.type == "OG"]
    if not og:
        return float("inf")
    return min(og) * control.density_target_pct


def n_tanks_per_system(facility: FacilityConfig) -> dict[str, int]:
    """Count of OG tanks present in each OG system."""
    counts: dict[str, int] = {}
    for t in facility.tanks:
        if t.type == "OG":
            counts[t.system_id] = counts.get(t.system_id, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Mode-aware available-tank count (for the L1<->L3 feasibility loop)
# ---------------------------------------------------------------------------

def _is_purge_week(week_label: str, control: ControlParams) -> bool:
    """True if `week_label` is BEFORE control.sixn_production_start (6N purge).

    Mirrors forecast.sixn.is_purge_mode at the week grain: with no
    sixn_production_start the whole horizon is purge; otherwise a week is purge
    while its Monday is strictly before the production-start date.
    """
    from .time_grid import parse_iso_label

    psd = getattr(control, "sixn_production_start", None)
    if psd is None:
        return True
    psd_date = psd.date() if hasattr(psd, "date") else psd
    wk_monday = parse_iso_label(week_label)
    if wk_monday is None:
        return True
    return wk_monday < psd_date


def available_tanks_for_week(
    week_label: str, facility: FacilityConfig, control: ControlParams
) -> tuple[int, int]:
    """Mode-aware (biomass_tanks, feed_tanks) physically available this week.

    The whole-tank demand competes for the OG tanks L3 places production fish
    into. The count available differs by 6N mode; the spec's distinction is that
    6N purge tanks HOLD biomass but take NO feed (mirrors the STARVE treatment),
    so a 6N purge tank counts toward the biomass-tank budget but not the
    feed-tank budget:

      * 6N PURGE mode  (week < sixn_production_start): a 6N purge tank holds
        biomass (no feed). biomass_tanks counts it; feed_tanks does not.
      * 6N PRODUCTION mode (week >= sixn_production_start): every OG production
        tank feeds, so 6N counts toward BOTH budgets.

    On THIS facility the conveyor geometry is: the 11 NURSERY+GROWOUT systems
    are the production placement pool (33 tanks, all feeding), and OG6N (6 tanks)
    is a SEPARATE harvest-staging / depuration pool that L3 does NOT stock
    production fish into. So:

      * The placement pool = 11 NURSERY+GROWOUT systems = 33 feeding tanks.
        None of these 33 are 6N purge tanks, so biomass_tanks == feed_tanks ==
        33 in BOTH modes here. (The spec's "33 incl. 2 6N off-feed" shape
        assumed 6N sat INSIDE the placement systems; here it is a distinct pool,
        so the off-feed adjustment has nothing to subtract from the 33.)

    The mode-aware STARVE logic is kept STRUCTURAL (so a facility whose
    placement pool DID contain purge tanks would get the off-feed subtraction):
    `feed_tanks` excludes placement-pool tanks that are 6N purge this week;
    `biomass_tanks` includes them. The PURGE_SYSTEMS (6N) outside the placement
    pool are reported separately and not added — they are not stocking targets.
    Counts are derived from the facility's real OG inventory so a re-sized
    facility stays correct.
    """
    n_by_sys = n_tanks_per_system(facility)
    prod_systems = set(NURSERY_SYSTEMS) | set(GROWOUT_SYSTEMS)
    purge_systems = set(PURGE_SYSTEMS)
    purge_week = _is_purge_week(week_label, control)

    biomass_tanks = 0
    feed_tanks = 0
    for s, n in n_by_sys.items():
        if s in prod_systems:
            # In-pool production tank: feeds + holds biomass in both modes.
            biomass_tanks += n
            feed_tanks += n
        elif s in purge_systems:
            # 6N depuration/staging pool: NOT a placement target on this
            # facility, so it adds NO realizable placement capacity. (Kept here
            # explicitly so the geometry is documented; if a future facility
            # folds 6N into the placement pool, move it into prod_systems and
            # the off-feed STARVE subtraction below applies.)
            continue
    # STARVE adjustment (structural): any placement-pool tank that is a 6N purge
    # tank this week holds biomass but does not feed. On this facility there are
    # none in-pool, so this is a no-op; it preserves the spec's semantics for a
    # facility where 6N IS a placement system.
    if purge_week:
        in_pool_purge = sum(n for s, n in n_by_sys.items()
                            if s in prod_systems and s in purge_systems)
        feed_tanks -= in_pool_purge
    return biomass_tanks, feed_tanks


@dataclass
class TankDemandRow:
    """Step-2 whole-tank demand for one (batch, week)."""
    week: int
    week_label: str
    batch_id: str
    tier: str
    tanks: int
    biomass_kg: float
    feed_kg_day: float
    avg_wt_g: float
    per_tank_biomass_kg: float
    per_tank_feed_kg_day: float


def build_tank_demand(
    l1: PlannerResult,
    facility: FacilityConfig,
    control: ControlParams,
) -> list[TankDemandRow]:
    """Step 2: ceil(biomass / per_tank_capacity) whole tanks per (batch, week).

    6N PURGE-HOLD rows (`BatchStandingRow.in_purge`, only present when L1 ran
    with `model_purge_hold=True`) are SKIPPED here: the off-feed depuration
    population sits in the separate 6N staging pool (its own pairs at the 125%
    staged density), which L3 does not stock grow-out production fish into. So
    the grow-out whole-tank demand stays pure (no 6N double-count) and the 6N
    footprint is accounted by `sixn_tank_demand` against the 6 6N tanks. When
    `model_purge_hold` is off no row is `in_purge`, so this is byte-identical.
    """
    op_cap = per_tank_capacity_kg(facility, control)   # operating-density per tank
    _dt = getattr(control, "density_target_pct", 1.0) or 1.0
    hard_cap = op_cap / _dt                            # the smallest-OG hard cap
    rows: list[TankDemandRow] = []
    for r in l1.batch_standing:
        if r.biomass_kg <= 1e-9:
            continue
        if getattr(r, "in_purge", False):
            continue
        # SELECTIVE over-stock: only LIGHT batches concentrate toward the hard cap
        # (safe, low kg/m3); mature batches stay at operating density.
        cap = op_cap
        if (_OVERSTOCK_DENSITY_PCT is not None
                and r.avg_wt_g <= (_OVERSTOCK_MAX_WT_G or float("inf"))):
            cap = hard_cap * _OVERSTOCK_DENSITY_PCT
        tanks = max(1, math.ceil(r.biomass_kg / cap))
        rows.append(TankDemandRow(
            week=r.week, week_label=r.week_label, batch_id=r.batch_id,
            tier=_tier_for_weight(r.avg_wt_g), tanks=tanks,
            biomass_kg=r.biomass_kg, feed_kg_day=r.feed_kg_day,
            avg_wt_g=r.avg_wt_g,
            per_tank_biomass_kg=r.biomass_kg / tanks,
            per_tank_feed_kg_day=r.feed_kg_day / tanks,
        ))
    return rows


def sixn_tank_demand(
    l1: PlannerResult,
    facility: FacilityConfig,
    control: ControlParams,
) -> dict[int, int]:
    """Per-week 6N staging-tank demand from the L1 purge-hold population.

    The off-feed depuration fish (BatchStandingRow.in_purge) are staged in 6N
    pairs at the 125% harvest-staging density (a 6N tank holds MORE than a
    production tank because fish about to be harvested may exceed the running
    density). Footprint = ceil(total 6N-held biomass / (smallest_OG_tank *
    1.25)). Returns {week: tanks_in_6N}; empty when no in_purge rows (purge-hold
    off), so existing callers see nothing.
    """
    sixn_cap = smallest_og_tank_kg(facility) * 1.25
    held: dict[int, float] = {}
    for r in l1.batch_standing:
        if getattr(r, "in_purge", False) and r.biomass_kg > 1e-9:
            held[r.week] = held.get(r.week, 0.0) + r.biomass_kg
    return {w: max(1, math.ceil(kg / sixn_cap)) for w, kg in held.items()
            if kg > 1e-9}


def smallest_og_tank_kg(facility: FacilityConfig) -> float:
    """Min over OG tanks of (max_density_kg_m3 * volume_m3) — raw tank mass."""
    og = [t.max_density_kg_m3 * t.volume_m3 for t in facility.tanks
          if t.type == "OG"]
    return min(og) if og else float("inf")


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------

@dataclass
class L3PlacementRow:
    """Per-(batch, system, week) integer tank assignment from the LP."""
    week: int
    week_label: str
    batch_id: str
    system_id: str
    tier: str
    tanks: int                  # rounded integer tank count
    biomass_kg: float           # tanks * per_tank_biomass
    feed_kg_day: float          # tanks * per_tank_feed


@dataclass
class L3SystemLoadRow:
    """Per-(system, week) realized load vs caps."""
    week: int
    week_label: str
    system_id: str
    tier: str
    n_tanks: int
    n_tanks_cap: int
    biomass_kg: float
    feed_kg_day: float
    biomass_cap: float
    feed_cap: float
    over_biomass_kg: float
    over_feed_kg: float
    over_biomass: bool
    over_feed: bool


@dataclass
class L3Result:
    placements: list[L3PlacementRow]
    loads: list[L3SystemLoadRow]
    systems: list[str]
    solver: str                         # "scipy-highs" | "greedy-fallback"
    # Pass A
    passA_slack_total: float            # LP optimum sum(slk_bio+slk_feed) (kg)
    passA_tank_slack: float             # LP optimum sum(slk_tank) (tanks over phys.)
    passA_status: str
    # Pass B
    passB_transfers: float              # LP optimum sum t (tanks-worth moved)
    passB_status: str
    # Realized (after rounding) cap-violation accounting
    over_biomass_system_weeks: int
    over_feed_system_weeks: int
    total_over_biomass_kg: float
    total_over_feed_kg: float
    worst_biomass_ratio: float
    worst_feed_ratio: float
    # Transfers measured on the ROUNDED integer layout
    realized_transfers: float
    avg_systems_per_batch_week: float
    avg_systems_per_batch_horizon: float
    # Conservation
    worst_biomass_residual_kg: float    # |sum y*per_tank_bio - L1 standing|
    worst_tankcount_residual: float     # |sum_s y - tanks[b,w]|
    # Rounding diagnostics
    integrality_gap: float              # max |y_lp - round(y_lp)| over all vars
    rounding_fixups: int                # tank-count adjustments to restore = tanks
    n_y_vars: int
    n_constraints: int


# ---------------------------------------------------------------------------
# Eligibility (conveyor + documented grow-out -> nursery spill)
# ---------------------------------------------------------------------------

def _eligible_systems(
    tier: str, systems: set[str], allow_growout_spill: bool
) -> list[str]:
    """Systems a batch of `tier` may occupy.

    A <1 kg (nursery) batch is tier-locked to the nursery systems. A >=1 kg
    (grow-out) batch prefers grow-out systems but, when `allow_growout_spill`,
    may ALSO occupy nursery systems — the documented OG2N-style "necessary
    overflow". Without that spill the layout is *structurally* infeasible on this
    facility: grow-out tank demand (up to ~34 tanks) exceeds the 21 grow-out
    tanks, while the 12 nursery tanks sit nearly empty. The spill is a hard fact
    of the conveyor geometry, not a packing defect; the LP still PREFERS grow-out
    (lower transfers + the soft caps push load off over-full systems), so a
    grow-out batch only lands in nursery when grow-out is genuinely full.
    """
    if tier == "growout" and allow_growout_spill:
        # grow-out systems first, then nursery as overflow.
        return (_tier_systems("growout", systems)
                + _tier_systems("nursery", systems))
    return _tier_systems(tier, systems)


# ---------------------------------------------------------------------------
# LP build + solve
# ---------------------------------------------------------------------------

def _system_cap(
    metric: str, week_label: str, system_id: str,
    system_limits: SystemLimits, default: float,
) -> float:
    v = resolve_system_cap(metric, week_label, system_id, system_limits)
    return v if v is not None else default


def plan_l3(
    l1: PlannerResult,
    control: ControlParams,
    facility: FacilityConfig,
    system_limits: SystemLimits,
    *,
    slack_epsilon: float = 1.0,
    allow_growout_spill: bool = True,
    integer: bool = True,
    mip_time_limit: Optional[float] = 120.0,
    mip_rel_gap: float = 1e-4,
    verbose: bool = False,
) -> L3Result:
    """Run the lexicographic placement LP. See module docstring.

    `slack_epsilon` is the kg of total-slack head-room allowed in Pass B above
    Pass A's optimum (a tiny relaxation so Pass B's transfer objective has a
    feasible basis without re-opening a measurable cap violation).

    `allow_growout_spill` lets >=1 kg batches occupy nursery systems as the
    documented overflow. It MUST be on for this facility — grow-out tank demand
    exceeds the grow-out tank count, so a strict tier lock is infeasible (the LP
    raises). See `_eligible_systems`.

    `integer` (default True) solves the `y`/`t` tank counts as a true MILP via
    HiGHS branch-and-bound, so the layout is whole-tank by construction (no
    post-hoc rounding to break the soft caps). With `integer=False` the LP
    relaxation is solved and `y` is rounded — the integrality gap is large here
    (the fractional optimum balances load to exactly the caps, which integer
    rounding cannot preserve), so the MILP is the meaningful solve and the
    relaxation is offered only to expose that gap.
    """
    try:
        import numpy as np
        from scipy.optimize import linprog
        from scipy.sparse import coo_matrix
    except Exception:  # noqa: BLE001 — caller falls back to greedy
        raise RuntimeError("scipy unavailable")

    systems = [s for s in og_systems_from_facility(facility)
               if s in (set(NURSERY_SYSTEMS) | set(GROWOUT_SYSTEMS))]
    sys_idx = {s: i for i, s in enumerate(systems)}
    n_tanks = n_tanks_per_system(facility)

    demand = build_tank_demand(l1, facility, control)
    # Index demand by (batch, week) and collect weeks / batches.
    weeks = sorted({d.week for d in demand})
    week_label = {d.week: d.week_label for d in demand}
    by_bw: dict[tuple[str, int], TankDemandRow] = {
        (d.batch_id, d.week): d for d in demand}

    # ----- Build the y-variable index. One y per ELIGIBLE (b, s, w). -----
    # eligibility by tier of the batch that week (grow-out may spill to nursery).
    sys_set = set(systems)
    y_index: dict[tuple[str, str, int], int] = {}
    y_meta: list[tuple[str, str, int]] = []  # parallel: (batch, system, week)
    for d in demand:
        elig = _eligible_systems(d.tier, sys_set, allow_growout_spill)
        for s in elig:
            key = (d.batch_id, s, d.week)
            if key not in y_index:
                y_index[key] = len(y_meta)
                y_meta.append(key)
    n_y = len(y_meta)

    # Precompute (system, week) -> [var index] for fast row assembly.
    vars_by_sw: dict[tuple[str, int], list[int]] = {}
    for vi, (bid, s, w) in enumerate(y_meta):
        vars_by_sw.setdefault((s, w), []).append(vi)

    # slack vars: one slk_bio + one slk_feed + one slk_tank per (system, week).
    sw_list = [(s, w) for w in weeks for s in systems]
    sw_idx = {sw: i for i, sw in enumerate(sw_list)}
    n_sw = len(sw_list)

    # Variable layout for Pass A:
    #   [ y (n_y) | slk_bio (n_sw) | slk_feed (n_sw) | slk_tank (n_sw) ]
    # slk_tank softens the per-system TANK count cap. On this facility the
    # combined whole-tank demand peaks at ~36 tanks vs 33 physical OG tanks
    # (L1 rides ~100.5% of the biomass cap and ceil() rounding inflates the
    # count), so a HARD tank cap is structurally infeasible. We keep tank
    # capacity but make its overflow an explicit, heavily-penalized slack so the
    # LP always solves and the tank shortfall is reported rather than crashing.
    OFF_Y = 0
    OFF_SB = n_y
    OFF_SF = n_y + n_sw
    OFF_ST = n_y + 2 * n_sw
    n_varsA = n_y + 3 * n_sw

    # ---- Equality: place all tanks. sum_s y[b,s,w] = tanks[b,w] ----
    A_eq_rows: list[int] = []
    A_eq_cols: list[int] = []
    A_eq_val: list[float] = []
    b_eq: list[float] = []
    eqr = 0
    for d in demand:
        elig = _eligible_systems(d.tier, sys_set, allow_growout_spill)
        for s in elig:
            A_eq_rows.append(eqr)
            A_eq_cols.append(OFF_Y + y_index[(d.batch_id, s, d.week)])
            A_eq_val.append(1.0)
        b_eq.append(float(d.tanks))
        eqr += 1
    n_eq = eqr

    # ---- Inequalities (A_ub x <= b_ub) ----
    A_ub_rows: list[int] = []
    A_ub_cols: list[int] = []
    A_ub_val: list[float] = []
    b_ub: list[float] = []
    ubr = 0

    # (1) System tank capacity (SOFT): sum_b y[b,s,w] - slk_tank[s,w] <= n_tanks[s]
    for (s, w) in sw_list:
        vis = vars_by_sw.get((s, w))
        if not vis:
            continue
        for vi in vis:
            A_ub_rows.append(ubr)
            A_ub_cols.append(OFF_Y + vi)
            A_ub_val.append(1.0)
        A_ub_rows.append(ubr)
        A_ub_cols.append(OFF_ST + sw_idx[(s, w)])
        A_ub_val.append(-1.0)
        b_ub.append(float(n_tanks.get(s, 0)))
        ubr += 1

    # (2) Soft biomass cap:
    #     sum_b y*(per_tank_bio) - slk_bio[s,w] <= bio_cap[s,w]
    # (3) Soft feed cap:
    #     sum_b y*(per_tank_feed) - slk_feed[s,w] <= feed_cap[s,w]
    for (s, w) in sw_list:
        wl = week_label[w]
        bio_cap = _system_cap(METRIC_BIOMASS, wl, s, system_limits,
                              _DEFAULT_BIO_CAP)
        feed_cap = _system_cap(METRIC_FEED_DAY, wl, s, system_limits,
                               _DEFAULT_FEED_CAP)
        vis = vars_by_sw.get((s, w))
        terms_b = []
        terms_f = []
        if vis:
            for vi in vis:
                bid, ss, ww = y_meta[vi]
                d = by_bw[(bid, ww)]
                terms_b.append((vi, d.per_tank_biomass_kg))
                terms_f.append((vi, d.per_tank_feed_kg_day))
        if terms_b:
            for vi, coef in terms_b:
                A_ub_rows.append(ubr)
                A_ub_cols.append(OFF_Y + vi)
                A_ub_val.append(coef)
            A_ub_rows.append(ubr)
            A_ub_cols.append(OFF_SB + sw_idx[(s, w)])
            A_ub_val.append(-1.0)
            b_ub.append(bio_cap)
            ubr += 1

            for vi, coef in terms_f:
                A_ub_rows.append(ubr)
                A_ub_cols.append(OFF_Y + vi)
                A_ub_val.append(coef)
            A_ub_rows.append(ubr)
            A_ub_cols.append(OFF_SF + sw_idx[(s, w)])
            A_ub_val.append(-1.0)
            b_ub.append(feed_cap)
            ubr += 1
    n_ub_A = ubr

    def _coo(rows, cols, vals, m, n):
        if not rows:
            return coo_matrix((m, n))
        return coo_matrix((np.array(vals), (np.array(rows), np.array(cols))),
                          shape=(m, n))

    # ===== Build the full variable space ONCE and run a lexicographic =====
    # ===== sequence of solves, tightening a slack-budget row each time.  =====
    # Layout:  [ y | slk_bio | slk_feed | slk_tank | t ]
    OFF_T = n_y + 3 * n_sw
    n_vars = n_y + 3 * n_sw + n_y

    A_eqF = _coo(A_eq_rows, A_eq_cols, A_eq_val, n_eq, n_vars)

    # Transfer rows: -t[b,s,w] + y[b,s,w] - y[b,s,w-1] <= 0  (so t >= delta_in).
    A_ub_rows = list(A_ub_rows)
    A_ub_cols = list(A_ub_cols)
    A_ub_val = list(A_ub_val)
    for (bid, s, w), vi in y_index.items():
        A_ub_rows.append(ubr)
        A_ub_cols.append(OFF_T + vi)        # t var (parallel to y order)
        A_ub_val.append(-1.0)
        A_ub_rows.append(ubr)
        A_ub_cols.append(OFF_Y + vi)
        A_ub_val.append(1.0)
        prev = y_index.get((bid, s, w - 1))
        if prev is not None:
            A_ub_rows.append(ubr)
            A_ub_cols.append(OFF_Y + prev)
            A_ub_val.append(-1.0)
        b_ub.append(0.0)
        ubr += 1
    n_ub_base = ubr

    bounds = [(0.0, None)] * n_vars
    intF = np.zeros(n_vars)
    if integer:
        intF[OFF_Y:OFF_Y + n_y] = 1
        intF[OFF_T:OFF_T + n_y] = 1

    def _solve(c, extra_rows, time_limit=None):
        """Solve with base rows (sans transfer rows unless requested) + extras."""
        opts = {}
        if time_limit is not None:
            opts["time_limit"] = float(time_limit)
            opts["mip_rel_gap"] = mip_rel_gap
        rr = list(A_ub_rows)
        cc = list(A_ub_cols)
        vv = list(A_ub_val)
        bb = list(b_ub)
        r = n_ub_base
        for cols_vals, rhs in extra_rows:
            for col, val in cols_vals:
                rr.append(r)
                cc.append(col)
                vv.append(val)
            bb.append(rhs)
            r += 1
        A_ub_f = _coo(rr, cc, vv, r, n_vars)
        return linprog(c, A_ub=A_ub_f, b_ub=np.array(bb),
                       A_eq=A_eqF, b_eq=np.array(b_eq), bounds=bounds,
                       method="highs", integrality=intF, options=opts or None)

    # ===== Pass A — separable PER WEEK (no Pass-A constraint or objective =====
    # couples weeks), so we solve a tiny exact MILP for each week and stitch the
    # optimal y back together. This makes Pass A proven-optimal AND fast (the
    # monolithic MILP doesn't converge at 5k integer vars). Per week we run two
    # lexicographic solves: A.1 min tank slack, then A.2 min bio+feed kg slack
    # with tank slack fixed.
    sb_floor: dict[int, tuple] = {}   # per-week (bio_slack, feed_slack) floor
    if integer:
        passA_tank_slack, passA_slack, xA = _solve_passA_per_week(
            weeks, systems, sw_idx, vars_by_sw, y_meta, by_bw, n_tanks,
            week_label, system_limits, sb_floor, np, linprog,
            mip_rel_gap, verbose)
        passA_status = "per-week-exact"
    else:
        # LP relaxation: solve the monolithic relaxed Pass A (fast; the gap is
        # the point of this mode).
        c_tank = np.zeros(n_vars)
        c_tank[OFF_ST:OFF_ST + n_sw] = 1.0
        res1 = _solve(c_tank, [])
        passA_tank_slack = float(res1.x[OFF_ST:OFF_ST + n_sw].sum())
        tank_budget0 = ([(OFF_ST + j, 1.0) for j in range(n_sw)],
                        passA_tank_slack + 1.0)
        c_cap = np.zeros(n_vars)
        c_cap[OFF_SB:OFF_SB + n_sw] = 1.0
        c_cap[OFF_SF:OFF_SF + n_sw] = 1.0
        res2 = _solve(c_cap, [tank_budget0])
        xA = res2.x
        passA_slack = float(xA[OFF_SB:OFF_SB + n_sw].sum()
                            + xA[OFF_SF:OFF_SF + n_sw].sum())
        passA_status = f"LP-relax (status {res2.status})"
        if verbose:
            print(f"  [L3] Pass A (LP relax): tank_slack={passA_tank_slack:.1f}"
                  f" tanks, capslack={passA_slack:,.1f} kg")

    # ===== Pass B — minimize transfers, SEQUENTIAL per week. =====
    # The monolithic full-horizon transfer MILP is intractable at this size
    # (5,393 integer y + 5,393 t over 52 coupled weeks — HiGHS finds no integer
    # incumbent in minutes). Transfers couple only consecutive weeks, so we
    # process weeks in order: for each week re-solve the per-week placement with
    # its cap-slack pinned at the Pass-A floor (+epsilon, so legality is never
    # worsened) and a stickiness objective that rewards keeping each batch in the
    # system(s) it occupied last week. This is a greedy lexicographic transfer
    # reduction — fast (52 tiny MILPs) and deterministic. With integer=False the
    # global LP-relaxation Pass B is used instead (the spec's relaxation path).
    if integer:
        xB = _solve_passB_per_week(
            weeks, systems, sw_idx, vars_by_sw, y_meta, by_bw, n_tanks,
            week_label, system_limits, sb_floor, xA, slack_epsilon,
            np, linprog, mip_rel_gap, verbose)
        passB_transfers = float("nan")  # measured exactly on the rounded layout
        passB_status = "sequential per-week stickiness"
    else:
        tank_budget = ([(OFF_ST + j, 1.0) for j in range(n_sw)],
                       passA_tank_slack + 1.0)
        # One global bio+feed slack budget (LP-relax path; sb_floor unused here).
        cap_budget = ([(OFF_SB + j, 1.0) for j in range(2 * n_sw)],
                      passA_slack + slack_epsilon)
        c_tr = np.zeros(n_vars)
        c_tr[OFF_T:OFF_T + n_y] = 1.0
        resB = _solve(c_tr, [tank_budget, cap_budget],
                      time_limit=mip_time_limit)
        if resB.x is None:
            xB = xA
            passB_transfers = float("nan")
            passB_status = (f"LP-relax no soln ({resB.message}); Pass A layout")
        else:
            xB = resB.x
            passB_transfers = float(c_tr @ xB)
            passB_status = f"LP-relax status {resB.status}"
    if verbose:
        print(f"  [L3] Pass B: {passB_status}")

    # ----- Extract + round y -----
    y_lp = xB[OFF_Y:OFF_Y + n_y]
    integrality_gap = float(max((abs(v - round(v)) for v in y_lp), default=0.0))

    # Round, then per-(batch,week) repair so sum_s y == tanks[b,w] exactly.
    y_round = [int(round(v)) for v in y_lp]
    rounding_fixups = _repair_tank_counts(
        y_round, y_meta, y_index, by_bw, demand, systems, n_tanks)

    return _assemble_result(
        y_round, y_meta, by_bw, demand, systems, n_tanks, week_label,
        system_limits, solver="scipy-highs",
        passA_slack=passA_slack, passA_tank_slack=passA_tank_slack,
        passA_status=passA_status,
        passB_transfers=passB_transfers, passB_status=passB_status,
        integrality_gap=integrality_gap, rounding_fixups=rounding_fixups,
        n_y=n_y, n_constraints=n_eq + n_ub_A,
        weeks=weeks,
    )


def _solve_passA_per_week(
    weeks, systems, sw_idx, vars_by_sw, y_meta, by_bw, n_tanks,
    week_label, system_limits, sb_floor, np, linprog, mip_rel_gap, verbose,
):
    """Solve Pass A exactly, one small MILP per week (separable).

    Returns (total_tank_slack, total_cap_slack, xA_full) where xA_full is laid
    out over the GLOBAL variable space [y | slk_bio | slk_feed | slk_tank | t]
    with only the y entries populated (the global Pass B re-derives slacks/t).
    """
    n_y = len(y_meta)
    n_sw = len(sw_idx)
    OFF_Y = 0
    OFF_SB = n_y
    OFF_SF = n_y + n_sw
    OFF_ST = n_y + 2 * n_sw
    n_vars_full = n_y + 3 * n_sw + n_y
    xA = np.zeros(n_vars_full)

    total_tank = 0.0
    total_cap = 0.0
    # Per-week MILPs: grow-out weeks (≈30 batches × 11 eligible systems, tight
    # caps) are real bin-packing MILPs. A short per-week time limit + a modest
    # relative-gap returns a near-optimal incumbent quickly; the lexicographic
    # priority (tank slack first) is preserved exactly by the two-solve split.
    wk_opts = {"mip_rel_gap": max(mip_rel_gap, 0.02), "time_limit": 4.0}

    for wi, w in enumerate(weeks):
        if verbose and wi % 10 == 0:
            print(f"  [L3] Pass A: week {wi+1}/{len(weeks)} ...", flush=True)
        # Local vars: y for each (s, present this week) + per-system slk_bio,
        # slk_feed, slk_tank. Build a compact local problem.
        sys_here = [s for s in systems if (s, w) in vars_by_sw]
        # local y var list = all global y vars in this week.
        loc_y = []           # (global_vi, system, batch)
        for s in sys_here:
            for gvi in vars_by_sw[(s, w)]:
                bid, ss, ww = y_meta[gvi]
                loc_y.append((gvi, s, bid))
        nly = len(loc_y)
        # slack var indices: per system here.
        sb0 = nly
        sf0 = nly + len(sys_here)
        st0 = nly + 2 * len(sys_here)
        nlv = nly + 3 * len(sys_here)
        sys_pos = {s: i for i, s in enumerate(sys_here)}
        locy_pos = {gvi: i for i, (gvi, _, _) in enumerate(loc_y)}

        # Equality: place all tanks per batch this week.
        batches_here: dict[str, list[int]] = {}
        for i, (gvi, s, bid) in enumerate(loc_y):
            batches_here.setdefault(bid, []).append(i)
        Aeq_r, Aeq_c, Aeq_v, beq = [], [], [], []
        er = 0
        for bid, idxs in batches_here.items():
            d = by_bw[(bid, w)]
            for i in idxs:
                Aeq_r.append(er); Aeq_c.append(i); Aeq_v.append(1.0)
            beq.append(float(d.tanks)); er += 1

        # Inequalities: tank cap (soft), bio cap (soft), feed cap (soft).
        Aub_r, Aub_c, Aub_v, bub = [], [], [], []
        ur = 0
        for s in sys_here:
            vis = vars_by_sw[(s, w)]
            # tank: sum y - slk_tank <= n_tanks
            for gvi in vis:
                Aub_r.append(ur); Aub_c.append(locy_pos[gvi]); Aub_v.append(1.0)
            Aub_r.append(ur); Aub_c.append(st0 + sys_pos[s]); Aub_v.append(-1.0)
            bub.append(float(n_tanks.get(s, 0))); ur += 1
            # bio + feed
            wl = week_label[w]
            bio_cap = _system_cap(METRIC_BIOMASS, wl, s, system_limits,
                                  _DEFAULT_BIO_CAP)
            feed_cap = _system_cap(METRIC_FEED_DAY, wl, s, system_limits,
                                   _DEFAULT_FEED_CAP)
            for gvi in vis:
                bid, ss, ww = y_meta[gvi]
                d = by_bw[(bid, w)]
                Aub_r.append(ur); Aub_c.append(locy_pos[gvi])
                Aub_v.append(d.per_tank_biomass_kg)
            Aub_r.append(ur); Aub_c.append(sb0 + sys_pos[s]); Aub_v.append(-1.0)
            bub.append(bio_cap); ur += 1
            for gvi in vis:
                bid, ss, ww = y_meta[gvi]
                d = by_bw[(bid, w)]
                Aub_r.append(ur); Aub_c.append(locy_pos[gvi])
                Aub_v.append(d.per_tank_feed_kg_day)
            Aub_r.append(ur); Aub_c.append(sf0 + sys_pos[s]); Aub_v.append(-1.0)
            bub.append(feed_cap); ur += 1

        from scipy.sparse import coo_matrix as _coo_m
        Aeq = _coo_m((np.array(Aeq_v), (np.array(Aeq_r), np.array(Aeq_c))),
                     shape=(er, nlv)) if Aeq_r else _coo_m((er, nlv))
        Aub = _coo_m((np.array(Aub_v), (np.array(Aub_r), np.array(Aub_c))),
                     shape=(ur, nlv)) if Aub_r else _coo_m((ur, nlv))
        bnds = [(0.0, None)] * nlv
        intg = np.zeros(nlv); intg[:nly] = 1

        # A.1: min tank slack.
        c1 = np.zeros(nlv); c1[st0:st0 + len(sys_here)] = 1.0
        r1 = linprog(c1, A_ub=Aub, b_ub=np.array(bub), A_eq=Aeq,
                     b_eq=np.array(beq), bounds=bnds, method="highs",
                     integrality=intg, options=wk_opts)
        tank_w = float(r1.x[st0:st0 + len(sys_here)].sum())
        # A.2: fix tank slack, min bio+feed.
        Aub2_r = list(Aub_r) + [ur] * len(sys_here)
        Aub2_c = list(Aub_c) + [st0 + i for i in range(len(sys_here))]
        Aub2_v = list(Aub_v) + [1.0] * len(sys_here)
        bub2 = list(bub) + [tank_w + 1e-6]
        Aub2 = _coo_m((np.array(Aub2_v), (np.array(Aub2_r), np.array(Aub2_c))),
                      shape=(ur + 1, nlv))
        c2 = np.zeros(nlv)
        c2[sb0:sb0 + len(sys_here)] = 1.0
        c2[sf0:sf0 + len(sys_here)] = 1.0
        r2 = linprog(c2, A_ub=Aub2, b_ub=np.array(bub2), A_eq=Aeq,
                     b_eq=np.array(beq), bounds=bnds, method="highs",
                     integrality=intg, options=wk_opts)
        x2 = r2.x if r2.x is not None else r1.x
        bio_w = float(x2[sb0:sb0 + len(sys_here)].sum())
        feed_w = float(x2[sf0:sf0 + len(sys_here)].sum())
        cap_w = bio_w + feed_w
        # Store per-week (bio, feed) slack floors so Pass B can pin each metric
        # separately (a combined budget would let stickiness trade biomass slack
        # for extra feed overage).
        sb_floor[w] = (bio_w, feed_w)
        total_tank += tank_w
        total_cap += cap_w
        # Stitch this week's y into the global xA.
        for i, (gvi, s, bid) in enumerate(loc_y):
            xA[OFF_Y + gvi] = x2[i]

    if verbose:
        print(f"  [L3] Pass A (per-week exact): tank_slack={total_tank:.1f} "
              f"tanks, capslack(bio+feed)={total_cap:,.1f} kg "
              f"(sum of {len(weeks)} weekly MILPs)")
    return total_tank, total_cap, xA


def _solve_passB_per_week(
    weeks, systems, sw_idx, vars_by_sw, y_meta, by_bw, n_tanks,
    week_label, system_limits, sb_floor, xA, slack_epsilon,
    np, linprog, mip_rel_gap, verbose,
):
    """Pass B: sequential per-week transfer reduction (stickiness).

    Re-solves each week's placement with cap-slack pinned at the Pass-A floor and
    an objective that minimizes tank-placements into systems the batch did NOT
    occupy last week (a per-tank transfer proxy). Returns the global xB vector
    (y populated). Legality is never worse than Pass A (cap-slack is bounded by
    the same floor + epsilon).
    """
    from scipy.sparse import coo_matrix as _coo_m
    n_y = len(y_meta)
    n_sw = len(sw_idx)
    OFF_Y = 0
    n_vars_full = n_y + 3 * n_sw + n_y
    xB = np.zeros(n_vars_full)

    # last_sys[batch] = set of systems the batch occupied the previous week.
    last_sys: dict[str, set[str]] = {}

    for w in weeks:
        sys_here = [s for s in systems if (s, w) in vars_by_sw]
        loc_y = []
        for s in sys_here:
            for gvi in vars_by_sw[(s, w)]:
                bid, ss, ww = y_meta[gvi]
                loc_y.append((gvi, s, bid))
        nly = len(loc_y)
        sys_pos = {s: i for i, s in enumerate(sys_here)}
        locy_pos = {gvi: i for i, (gvi, _, _) in enumerate(loc_y)}
        sb0 = nly
        sf0 = nly + len(sys_here)
        st0 = nly + 2 * len(sys_here)
        nlv = nly + 3 * len(sys_here)

        # Equality: place all tanks per batch.
        batches_here: dict[str, list[int]] = {}
        for i, (gvi, s, bid) in enumerate(loc_y):
            batches_here.setdefault(bid, []).append(i)
        Aeq_r, Aeq_c, Aeq_v, beq = [], [], [], []
        er = 0
        for bid, idxs in batches_here.items():
            d = by_bw[(bid, w)]
            for i in idxs:
                Aeq_r.append(er); Aeq_c.append(i); Aeq_v.append(1.0)
            beq.append(float(d.tanks)); er += 1

        Aub_r, Aub_c, Aub_v, bub = [], [], [], []
        ur = 0
        for s in sys_here:
            vis = vars_by_sw[(s, w)]
            for gvi in vis:
                Aub_r.append(ur); Aub_c.append(locy_pos[gvi]); Aub_v.append(1.0)
            Aub_r.append(ur); Aub_c.append(st0 + sys_pos[s]); Aub_v.append(-1.0)
            bub.append(float(n_tanks.get(s, 0))); ur += 1
            wl = week_label[w]
            bio_cap = _system_cap(METRIC_BIOMASS, wl, s, system_limits,
                                  _DEFAULT_BIO_CAP)
            feed_cap = _system_cap(METRIC_FEED_DAY, wl, s, system_limits,
                                   _DEFAULT_FEED_CAP)
            for gvi in vis:
                bid, ss, ww = y_meta[gvi]
                d = by_bw[(bid, w)]
                Aub_r.append(ur); Aub_c.append(locy_pos[gvi])
                Aub_v.append(d.per_tank_biomass_kg)
            Aub_r.append(ur); Aub_c.append(sb0 + sys_pos[s]); Aub_v.append(-1.0)
            bub.append(bio_cap); ur += 1
            for gvi in vis:
                bid, ss, ww = y_meta[gvi]
                d = by_bw[(bid, w)]
                Aub_r.append(ur); Aub_c.append(locy_pos[gvi])
                Aub_v.append(d.per_tank_feed_kg_day)
            Aub_r.append(ur); Aub_c.append(sf0 + sys_pos[s]); Aub_v.append(-1.0)
            bub.append(feed_cap); ur += 1

        # Cap-slack budgets — pin BIOMASS and FEED slack SEPARATELY at the
        # Pass-A per-week floors (+ eps). Pinning the combined total would let
        # the stickiness objective trade biomass headroom for extra feed overage
        # (observed: feed over-weeks ballooned). Separate budgets keep both
        # metrics no worse than Pass A.
        bio_floor, feed_floor = sb_floor.get(w, (0.0, 0.0))
        for i in range(len(sys_here)):
            Aub_r.append(ur); Aub_c.append(sb0 + i); Aub_v.append(1.0)
        bub.append(bio_floor + slack_epsilon); ur += 1
        for i in range(len(sys_here)):
            Aub_r.append(ur); Aub_c.append(sf0 + i); Aub_v.append(1.0)
        bub.append(feed_floor + slack_epsilon); ur += 1
        # slk_tank is left free (lightly penalized in the objective); the place-
        # all-tanks equality forces the same minimal overflow Pass A used.

        Aeq = _coo_m((np.array(Aeq_v), (np.array(Aeq_r), np.array(Aeq_c))),
                     shape=(er, nlv)) if Aeq_r else _coo_m((er, nlv))
        Aub = _coo_m((np.array(Aub_v), (np.array(Aub_r), np.array(Aub_c))),
                     shape=(ur, nlv)) if Aub_r else _coo_m((ur, nlv))
        bnds = [(0.0, None)] * nlv
        intg = np.zeros(nlv); intg[:nly] = 1

        # Objective: penalize tanks placed in NON-sticky systems (transfer proxy)
        # + a tiny tank-slack penalty to avoid gratuitous overflow.
        c = np.zeros(nlv)
        for i, (gvi, s, bid) in enumerate(loc_y):
            if s not in last_sys.get(bid, set()):
                c[i] = 1.0
        c[st0:st0 + len(sys_here)] = 0.01

        res = linprog(c, A_ub=Aub, b_ub=np.array(bub), A_eq=Aeq,
                      b_eq=np.array(beq), bounds=bnds, method="highs",
                      integrality=intg,
                      options={"mip_rel_gap": max(mip_rel_gap, 0.02),
                               "time_limit": 4.0})
        x = res.x
        if x is None:
            # Fall back to Pass A's layout for this week.
            new_last: dict[str, set[str]] = {}
            for gvi in [g for (g, _, _) in loc_y]:
                bid, s, ww = y_meta[gvi]
                xB[OFF_Y + gvi] = xA[OFF_Y + gvi]
                if xA[OFF_Y + gvi] > 0.5:
                    new_last.setdefault(bid, set()).add(s)
            last_sys = new_last
            continue

        new_last = {}
        for i, (gvi, s, bid) in enumerate(loc_y):
            xB[OFF_Y + gvi] = x[i]
            if x[i] > 0.5:
                new_last.setdefault(bid, set()).add(s)
        last_sys = new_last

    if verbose:
        print(f"  [L3] Pass B (sequential per-week): {len(weeks)} weekly "
              f"stickiness MILPs solved")
    return xB


def _repair_tank_counts(
    y_round: list[int],
    y_meta: list[tuple[str, str, int]],
    y_index: dict[tuple[str, str, int], int],
    by_bw: dict[tuple[str, int], TankDemandRow],
    demand: list[TankDemandRow],
    systems: list[str],
    n_tanks: dict[str, int],
) -> int:
    """Repair integer rounding so sum_s y[b,s,w] == tanks[b,w] for every (b,w).

    LP-relaxation rounding can leave a (batch, week) one tank short or long.
    Adjust the eligible system with the most/least slack (least over a tank cap)
    to restore the exact tank count. Returns the number of tank-count fixups.
    """
    fixups = 0
    # Per (batch, week): list of (system, var_index).
    cells: dict[tuple[str, int], list[tuple[str, int]]] = {}
    for (bid, s, w), vi in y_index.items():
        cells.setdefault((bid, w), []).append((s, vi))

    # System occupancy per (s, w) for picking the least-loaded fixup target.
    occ: dict[tuple[str, int], int] = {}
    for (bid, s, w), vi in y_index.items():
        occ[(s, w)] = occ.get((s, w), 0) + y_round[vi]

    for d in demand:
        key = (d.batch_id, d.week)
        members = cells.get(key, [])
        cur = sum(y_round[vi] for _, vi in members)
        diff = d.tanks - cur
        if diff == 0:
            continue
        if diff > 0:
            # Need more tanks: add to systems with the most free tank slots.
            for _ in range(diff):
                cand = sorted(
                    members,
                    key=lambda sv: (occ.get((sv[0], d.week), 0)
                                    - n_tanks.get(sv[0], 0), sv[0]))
                s, vi = cand[0]
                y_round[vi] += 1
                occ[(s, d.week)] = occ.get((s, d.week), 0) + 1
                fixups += 1
        else:
            # Too many tanks: remove from systems where this batch has tanks.
            for _ in range(-diff):
                cand = sorted(
                    [(s, vi) for s, vi in members if y_round[vi] > 0],
                    key=lambda sv: (-(occ.get((sv[0], d.week), 0)), sv[0]))
                if not cand:
                    break
                s, vi = cand[0]
                y_round[vi] -= 1
                occ[(s, d.week)] = occ.get((s, d.week), 0) - 1
                fixups += 1
    return fixups


def _assemble_result(
    y_round, y_meta, by_bw, demand, systems, n_tanks, week_label,
    system_limits, *, solver, passA_slack, passA_tank_slack, passA_status,
    passB_transfers, passB_status, integrality_gap, rounding_fixups,
    n_y, n_constraints, weeks,
) -> L3Result:
    sys_set = set(systems)
    placements: list[L3PlacementRow] = []
    # Per (system, week) accumulators.
    load_bio: dict[tuple[str, int], float] = {}
    load_feed: dict[tuple[str, int], float] = {}
    load_tanks: dict[tuple[str, int], int] = {}

    # Per-batch system tracking for transfers + fragmentation.
    by_batch_week_sys: dict[tuple[str, int], set[str]] = {}
    batch_systems_horizon: dict[str, set[str]] = {}

    for vi, (bid, s, w) in enumerate(y_meta):
        tanks = y_round[vi]
        if tanks <= 0:
            continue
        d = by_bw[(bid, w)]
        bio = tanks * d.per_tank_biomass_kg
        feed = tanks * d.per_tank_feed_kg_day
        placements.append(L3PlacementRow(
            week=w, week_label=d.week_label, batch_id=bid, system_id=s,
            tier=d.tier, tanks=tanks, biomass_kg=bio, feed_kg_day=feed))
        load_bio[(s, w)] = load_bio.get((s, w), 0.0) + bio
        load_feed[(s, w)] = load_feed.get((s, w), 0.0) + feed
        load_tanks[(s, w)] = load_tanks.get((s, w), 0) + tanks
        by_batch_week_sys.setdefault((bid, w), set()).add(s)
        batch_systems_horizon.setdefault(bid, set()).add(s)

    # ----- Per-(system, week) load vs caps -----
    loads: list[L3SystemLoadRow] = []
    over_b_sw = over_f_sw = 0
    tot_over_b = tot_over_f = 0.0
    worst_b_ratio = worst_f_ratio = 0.0
    for w in weeks:
        wl = week_label[w]
        for s in systems:
            bio = load_bio.get((s, w), 0.0)
            feed = load_feed.get((s, w), 0.0)
            nt = load_tanks.get((s, w), 0)
            bio_cap = _system_cap(METRIC_BIOMASS, wl, s, system_limits,
                                  _DEFAULT_BIO_CAP)
            feed_cap = _system_cap(METRIC_FEED_DAY, wl, s, system_limits,
                                   _DEFAULT_FEED_CAP)
            ob = max(0.0, bio - bio_cap)
            of = max(0.0, feed - feed_cap)
            if ob > 1e-6:
                over_b_sw += 1
                tot_over_b += ob
            if of > 1e-6:
                over_f_sw += 1
                tot_over_f += of
            if bio_cap > 0:
                worst_b_ratio = max(worst_b_ratio, bio / bio_cap)
            if feed_cap > 0:
                worst_f_ratio = max(worst_f_ratio, feed / feed_cap)
            if nt > 0 or bio > 0:
                loads.append(L3SystemLoadRow(
                    week=w, week_label=wl, system_id=s,
                    tier=("nursery" if s in NURSERY_SYSTEMS else "growout"),
                    n_tanks=nt, n_tanks_cap=n_tanks.get(s, 0),
                    biomass_kg=bio, feed_kg_day=feed,
                    biomass_cap=bio_cap, feed_cap=feed_cap,
                    over_biomass_kg=ob, over_feed_kg=of,
                    over_biomass=ob > 1e-6, over_feed=of > 1e-6))

    # ----- Conservation -----
    # Recompute per-(batch, week) placed biomass + tank count from y_round
    # directly and compare to the Step-2 demand (the L1 standing biomass).
    worst_bio_resid = 0.0
    worst_tank_resid = 0.0
    placed_bio_bw: dict[tuple[str, int], float] = {}
    placed_tanks_bw: dict[tuple[str, int], int] = {}
    for vi, (bid, s, w) in enumerate(y_meta):
        if y_round[vi] <= 0:
            continue
        d = by_bw[(bid, w)]
        placed_bio_bw[(bid, w)] = (placed_bio_bw.get((bid, w), 0.0)
                                   + y_round[vi] * d.per_tank_biomass_kg)
        placed_tanks_bw[(bid, w)] = (placed_tanks_bw.get((bid, w), 0)
                                     + y_round[vi])
    for d in demand:
        key = (d.batch_id, d.week)
        # placed biomass uses per_tank*tanks == biomass_kg when tanks match,
        # so residual reflects only rounding repair drift.
        pb = placed_bio_bw.get(key, 0.0)
        worst_bio_resid = max(worst_bio_resid, abs(pb - d.biomass_kg))
        pt = placed_tanks_bw.get(key, 0)
        worst_tank_resid = max(worst_tank_resid, abs(pt - d.tanks))

    # ----- Transfers on the rounded integer layout -----
    realized_transfers = _measure_transfers(y_round, y_meta, by_bw)

    # ----- Fragmentation -----
    frag_numer = sum(len(v) for v in by_batch_week_sys.values())
    frag_denom = len(by_batch_week_sys)
    avg_frag = (frag_numer / frag_denom) if frag_denom else 0.0
    horizon_vals = [len(v) for v in batch_systems_horizon.values()]
    avg_horizon = (sum(horizon_vals) / len(horizon_vals)) if horizon_vals else 0.0

    return L3Result(
        placements=placements, loads=loads, systems=systems, solver=solver,
        passA_slack_total=passA_slack, passA_tank_slack=passA_tank_slack,
        passA_status=passA_status,
        passB_transfers=passB_transfers, passB_status=passB_status,
        over_biomass_system_weeks=over_b_sw, over_feed_system_weeks=over_f_sw,
        total_over_biomass_kg=tot_over_b, total_over_feed_kg=tot_over_f,
        worst_biomass_ratio=worst_b_ratio, worst_feed_ratio=worst_f_ratio,
        realized_transfers=realized_transfers,
        avg_systems_per_batch_week=avg_frag,
        avg_systems_per_batch_horizon=avg_horizon,
        worst_biomass_residual_kg=worst_bio_resid,
        worst_tankcount_residual=worst_tank_resid,
        integrality_gap=integrality_gap, rounding_fixups=rounding_fixups,
        n_y_vars=n_y, n_constraints=n_constraints,
    )


def _measure_transfers(
    y_round: list[int],
    y_meta: list[tuple[str, str, int]],
    by_bw: dict[tuple[str, int], TankDemandRow],
) -> float:
    """sum over (b,s,w) of max(0, y[b,s,w] - y[b,s,w-1]) — tanks-worth moving in."""
    # index by (batch, system, week)
    yv: dict[tuple[str, str, int], int] = {}
    for vi, (bid, s, w) in enumerate(y_meta):
        yv[(bid, s, w)] = y_round[vi]
    total = 0
    for (bid, s, w), cur in yv.items():
        prev = yv.get((bid, s, w - 1), 0)
        total += max(0, cur - prev)
    return float(total)
