"""STANDALONE proof-of-concept: the L1<->L3 feasibility loop (tankless global).

METHOD: GLOBAL LOOP (L1 loading <-> L3 tank realizability)
==========================================================

L1 (`forecast.global_planner_poc`) harvests to hold the FACILITY biomass + feed
caps, treating the farm as one global pool. At 100% facility-cap loading L3
(`forecast.global_planner_l3_poc`) finds the whole-tank demand peaks ABOVE the
physical OG production-tank count: ceil(biomass / per_tank_capacity) summed over
batches exceeds the tanks that exist. So L1 over-loads vs what the tanks can
actually hold.

This module closes the loop. It drives L1's per-week biomass loading DOWN to the
tank-REALIZABLE envelope, then re-places, converging on the highest loading the
tanks can physically realize:

  1. Run L1 with a per-week biomass ceiling (initially = flat facility cap).
  2. Run L3 -> for each week, total whole-tank demand vs the mode-aware
     available BIOMASS tanks (33 in 6N purge mode, 36 in production mode).
  3. For every over-subscribed week, LOWER that week's L1 ceiling by the
     over-subscription converted to kg (tanks_over * per_tank_capacity) + a
     small margin, then re-run L1 + L3.
  4. Repeat until no week is over-subscribed (fully tank-realizable) or it
     converges / stops improving (cap ~10 iterations; the residual is reported).

The available-tank count is MODE-AWARE (see
`global_planner_l3_poc.available_tanks_for_week`): in 6N purge mode 33 tanks hold
biomass but only 31 feed (the 2 6N purge tanks are off-feed, mirroring STARVE);
in 6N production mode all 36 feed. The whole-tank demand is biomass-driven
(ceil of biomass / per-tank capacity), so the loop binds it against the
biomass-tank count.

6N flow-to-harvest (`model_purge_hold`)
---------------------------------------
When `model_purge_hold=True` L1 models the 2-week off-feed 6N purge hold (see
`global_planner_poc.plan`): the harvest-bound population sits in 6N pairs
(off-feed, still standing) before leaving round-robin. The loop then (a) places
only the GROW-OUT standing into the 33-tank pool (the in_purge 6N-held rows are
excluded by `build_tank_demand`), and (b) diagnoses the SEPARATE 6N staging pool
(`sixn_tank_demand` vs the 6 6N tanks) so 6N over-subscription is surfaced, not
hidden. Standing rides higher / feed lower than the instant-removal model, so the
realizable envelope and the tank-count picture both shift — this is the corrected,
trustworthy comparison.

This is deliberately ADDITIVE: it imports L1 + L3 verbatim, changes no existing
math, and is not imported by `forecast/run.py`. L3's lexicographic placement is
kept as-is this round (the loop only shapes L1's loading; it does not change the
transfer method).

What this is NOT
----------------
The loop converges the LOADING ENVELOPE to tank-realizable. It still does not
pick the specific physical tank within a system or run the 6N pair rotation —
that is the final specific-tank-pick + 6N-staging step.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import global_planner_l3_poc as l3
from . import global_planner_poc as gpp
from .caps import SystemLimits
from .global_planner_l2_poc import PURGE_SYSTEMS
from .global_planner_l3_poc import (
    L3Result,
    available_tanks_for_week,
    build_tank_demand,
    n_tanks_per_system,
    per_tank_capacity_kg,
    sixn_tank_demand,
)
from .global_planner_poc import PlannerResult
from .models import BatchInput, BiologyTables, ControlParams, FacilityConfig


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------

@dataclass
class WeekOverSub:
    """One week's tank over-subscription verdict at a given iteration."""
    week: int
    week_label: str
    tank_demand: int           # GROW-OUT whole-tank demand (in_purge excluded)
    avail_biomass_tanks: int   # mode-aware physical biomass tanks (33 grow-out)
    tanks_over: int            # max(0, demand - avail)
    standing_kg: float         # facility standing biomass this week
    ceiling_kg: float          # the L1 biomass ceiling in force this iteration
    sixn_tanks: int = 0        # 6N staging tanks the purge hold needs this week
    sixn_over: int = 0         # max(0, sixn_tanks - 6N physical tanks)


@dataclass
class LoopIteration:
    """A full L1->L3 pass + its over-subscription diagnosis."""
    iteration: int
    over_weeks: list[WeekOverSub]
    n_over_weeks: int
    total_tanks_over: int
    peak_biomass_kg: float
    mean_biomass_kg: float
    total_hog_kg: float
    l3_over_biomass_system_weeks: int
    l3_over_feed_system_weeks: int
    l3_realized_transfers: float
    peak_feed_kg_day: float = 0.0
    n_sixn_over_weeks: int = 0          # weeks the 6N pool over-subscribes
    peak_sixn_tanks: int = 0            # peak 6N staging tanks used


@dataclass
class LoopResult:
    iterations: list[LoopIteration]
    converged: bool                 # reached 0 over-subscribed weeks
    final_ceiling: dict[str, float] # per-week-label biomass ceiling at the end
    final_l1: PlannerResult
    final_l3: L3Result
    n_iterations: int
    # Converged-envelope summary (final iteration).
    peak_biomass_kg: float
    mean_biomass_kg: float
    facility_cap_kg: float
    pct_of_cap_peak: float
    pct_of_cap_mean: float
    total_hog_kg: float
    residual_over_weeks: int
    residual_total_tanks_over: int


# ---------------------------------------------------------------------------
# Diagnosis: whole-tank demand vs mode-aware available tanks
# ---------------------------------------------------------------------------

def diagnose_oversub(
    l1: PlannerResult,
    facility: FacilityConfig,
    control: ControlParams,
    ceiling: dict[str, float],
) -> list[WeekOverSub]:
    """Per-week whole-tank demand vs mode-aware available biomass tanks.

    The whole-tank demand is the SAME Step-2 arithmetic L3 uses
    (ceil(biomass / per_tank_capacity) per batch), summed over batches in a
    week. The available count is mode-aware (33 purge / 36 production for
    biomass). Returns one row per week with demand, availability and the
    over-subscription (>0 means the tanks cannot hold that week's loading).
    """
    demand = build_tank_demand(l1, facility, control)
    # standing biomass per week (from L1 trace) + week labels.
    standing_by_week = {r.week: r.standing_biomass_kg for r in l1.trace}
    label_by_week = {r.week: r.week_label for r in l1.trace}

    tanks_by_week: dict[int, int] = {}
    for d in demand:
        tanks_by_week[d.week] = tanks_by_week.get(d.week, 0) + d.tanks

    # 6N staging-tank demand from the purge hold (empty when purge-hold off).
    sixn_by_week = sixn_tank_demand(l1, facility, control)
    n_sixn_phys = sum(n for s, n in n_tanks_per_system(facility).items()
                      if s in set(PURGE_SYSTEMS))

    # Union of weeks that appear in grow-out demand or 6N demand (a week may have
    # only purge-held fish, e.g. transition tail).
    all_weeks = set(tanks_by_week) | set(sixn_by_week)
    out: list[WeekOverSub] = []
    for w in sorted(all_weeks):
        label = label_by_week.get(w, demand[0].week_label if demand else "")
        avail_bio, _avail_feed = available_tanks_for_week(label, facility, control)
        demand_tanks = tanks_by_week.get(w, 0)
        over = max(0, demand_tanks - avail_bio)
        sixn_t = sixn_by_week.get(w, 0)
        sixn_over = max(0, sixn_t - n_sixn_phys)
        out.append(WeekOverSub(
            week=w, week_label=label, tank_demand=demand_tanks,
            avail_biomass_tanks=avail_bio, tanks_over=over,
            standing_kg=standing_by_week.get(w, 0.0),
            ceiling_kg=ceiling.get(label, control.max_biomass_kg),
            sixn_tanks=sixn_t, sixn_over=sixn_over,
        ))
    return out


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def run_loop(
    batches: list[BatchInput],
    tables: BiologyTables,
    control: ControlParams,
    facility: FacilityConfig,
    system_limits: SystemLimits,
    *,
    inflight_og: Optional[dict[str, tuple[float, float, float]]] = None,
    harvest_tank_density_pct: float = 1.25,
    max_iterations: int = 10,
    margin_frac: float = 0.5,
    l3_kwargs: Optional[dict] = None,
    model_purge_hold: bool = True,
    model_full_facility: bool = True,
    fw_inflight: Optional[dict] = None,
    purge_inflight: Optional[dict] = None,
    verbose: bool = True,
) -> LoopResult:
    """Drive L1's per-week loading down to the tank-realizable envelope.

    `margin_frac` is the extra fraction of a per-tank capacity subtracted per
    over-subscribed tank, on top of the integral tanks_over * per_tank_capacity,
    so ceil() rounding does not immediately re-inflate the count. Each iteration
    LOWERS only the weeks that are still over-subscribed (monotone tightening),
    so the loop cannot thrash a week back up.

    Returns a LoopResult with every iteration's diagnosis + the converged
    envelope. L3 placement (`plan_l3`) is run as-is each iteration.

    `model_purge_hold` and `model_full_facility` DEFAULT True — the CORRECT
    whole-facility behavior for the tool (2-week off-feed 6N purge flow +
    counting FW + OG + purge against the cap). Because `model_full_facility`
    under-counts the FW phase unless the PR-measured in-flight FW units are
    supplied, callers MUST pass `fw_inflight` (the entry point
    `tools/run_global_forecast.py` always hydrates and passes it). Pass both
    False (e.g. via `tools/run_loop_poc --no-purge-hold`) to recover the old
    OG-only instant-removal comparison.
    """
    l3_kwargs = dict(l3_kwargs or {})
    l3_kwargs.setdefault("verbose", False)
    per_tank = per_tank_capacity_kg(facility, control)
    facility_cap = control.max_biomass_kg

    # Per-week-label ceiling. Start at the flat facility cap (== passing None to
    # L1, but we materialize it so we can lower individual weeks).
    ceiling: dict[str, float] = {}

    iterations: list[LoopIteration] = []
    last_l1: Optional[PlannerResult] = None
    last_l3: Optional[L3Result] = None
    converged = False

    for it in range(max_iterations):
        # First iteration uses None (byte-identical flat-cap L1); thereafter the
        # materialized per-week ceiling.
        bc = ceiling if ceiling else None
        l1 = gpp.plan(
            batches, tables, control, facility,
            inflight_og=inflight_og,
            harvest_tank_density_pct=harvest_tank_density_pct,
            record_standing=True,
            biomass_ceiling=bc,
            model_purge_hold=model_purge_hold,
            model_full_facility=model_full_facility,
            fw_inflight=fw_inflight,
            purge_inflight=purge_inflight,
        )
        res = l3.plan_l3(l1, control, facility, system_limits, **l3_kwargs)
        last_l1, last_l3 = l1, res

        over = diagnose_oversub(l1, facility, control, ceiling)
        over_weeks = [o for o in over if o.tanks_over > 0]
        total_over = sum(o.tanks_over for o in over_weeks)
        sixn_over_weeks = [o for o in over if o.sixn_over > 0]
        peak_sixn = max((o.sixn_tanks for o in over), default=0)
        standings = [r.standing_biomass_kg for r in l1.trace]
        peak = max(standings, default=0.0)
        mean = (sum(standings) / len(standings)) if standings else 0.0
        peak_feed = max((r.feed_kg_day for r in l1.trace), default=0.0)
        total_hog = sum(r.harvested_kg for r in l1.trace)

        iterations.append(LoopIteration(
            iteration=it, over_weeks=over_weeks, n_over_weeks=len(over_weeks),
            total_tanks_over=total_over, peak_biomass_kg=peak,
            mean_biomass_kg=mean, total_hog_kg=total_hog,
            l3_over_biomass_system_weeks=res.over_biomass_system_weeks,
            l3_over_feed_system_weeks=res.over_feed_system_weeks,
            l3_realized_transfers=res.realized_transfers,
            peak_feed_kg_day=peak_feed,
            n_sixn_over_weeks=len(sixn_over_weeks), peak_sixn_tanks=peak_sixn,
        ))

        if verbose:
            sixn_note = (f", 6N {peak_sixn}t peak ({len(sixn_over_weeks)} over)"
                         if model_purge_hold else "")
            print(f"  [loop] iter {it}: {len(over_weeks)} weeks over-subscribed "
                  f"({total_over} tanks over), peak {peak:,.0f} kg "
                  f"({100*peak/facility_cap:.1f}% cap), HOG {total_hog:,.0f} kg, "
                  f"L3 over bio/feed {res.over_biomass_system_weeks}/"
                  f"{res.over_feed_system_weeks}, "
                  f"transfers {res.realized_transfers:.0f}{sixn_note}")

        if not over_weeks:
            converged = True
            break

        # Lower each over-subscribed week's ceiling by the over-subscription in
        # kg (+ margin). Tighten relative to the week's CURRENT standing so the
        # ceiling actually bites (standing may sit below the prior ceiling).
        for o in over_weeks:
            cur = ceiling.get(o.week_label, facility_cap)
            # kg to shed = tanks_over whole tanks (+ margin) of capacity.
            shed = (o.tanks_over + margin_frac) * per_tank
            # Target the lower of (current ceiling - shed) and
            # (standing - shed): bind to whichever is active so a non-biting
            # ceiling still drops.
            target = min(cur, o.standing_kg) - shed
            new_ceiling = min(cur, target)
            ceiling[o.week_label] = max(0.0, new_ceiling)

    # Final summary from the last iteration.
    fin = iterations[-1]
    return LoopResult(
        iterations=iterations,
        converged=converged,
        final_ceiling=dict(ceiling),
        final_l1=last_l1,
        final_l3=last_l3,
        n_iterations=len(iterations),
        peak_biomass_kg=fin.peak_biomass_kg,
        mean_biomass_kg=fin.mean_biomass_kg,
        facility_cap_kg=facility_cap,
        pct_of_cap_peak=100.0 * fin.peak_biomass_kg / facility_cap if facility_cap else 0.0,
        pct_of_cap_mean=100.0 * fin.mean_biomass_kg / facility_cap if facility_cap else 0.0,
        total_hog_kg=fin.total_hog_kg,
        residual_over_weeks=fin.n_over_weeks,
        residual_total_tanks_over=fin.total_tanks_over,
    )
