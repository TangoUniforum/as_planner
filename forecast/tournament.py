"""The TUNED tournament — per-method knob search, so the board compares
every method AT ITS BEST, not the live-config engine tuned vs the rest stock.

The operator's vision, final form: find the best parameters for EVERY method,
compare the tuned methods, promote the winner (method + knobs) as the default,
then use just that method fast afterward. Today's Analyze knob round tunes only
the live-config engine — an asymmetric tournament this module fixes.

Per method the flow is:

  stock run (the board leg, reused)  ->  hard-gate check
    PASSES + has a knob space  -> FULL search (optimize.sweep grid + coordinate
                                  descent, both restricted to the METHOD's own
                                  space, its pinned overrides merged under every
                                  candidate) -> tuned winner -> verification run
    FAILS  + has a knob space  -> cheap PROBE (single pass, one knob at a time):
                                  does ANY single knob fix the gate? none ->
                                  'gate-bound', skip the full search
    no knob space              -> competes at stock ('stock-only'); a hard-gate
                                  failure there is 'gate-bound' by definition

Search candidates are conservation-gated exactly like Optimize (a variant that
drops/over-produces fish can never win), and the winner must additionally pass
the never-an-empty-week hard rule when any candidate does.

This module holds the PURE decision logic (unit-testable, no IO) plus the
shared search driver used by both the app's Analyze mode and the headless
tools/run_tuned_tournament.py. Grading/present-layer concerns stay with the
callers (each already has its own lens stack).

ENGINE NOTE: the search harness (optimize.sweep / coordinate_descent) runs the
CONTROLLER engine. That is correct for every method with a non-empty space
today (the Global family's space is empty — forecast/methods.py has the
evidence); tune_method guards against a future global space silently being
measured on the wrong engine.
"""
from __future__ import annotations

from typing import Optional

# The hard rules a probe must fix (analysis.GATES keys with hard=True).
HARD_GATE_KEYS = ("conservation", "no_empty_week")


# --------------------------------------------------------------------------- #
# Pure decision logic
# --------------------------------------------------------------------------- #
def tuned_label(label: str, overrides: Optional[dict] = None,
                pins: Optional[dict] = None) -> str:
    """Board label for a method's tuned candidate. Shows only the knobs the
    SEARCH chose (winner minus the method's own pins) — the pins are part of
    the method's identity, not of the tuning."""
    chosen = {k: v for k, v in (overrides or {}).items()
              if (pins or {}).get(k) != v}
    knobs = ", ".join(f"{k}={v}" for k, v in sorted(chosen.items()))
    return f"{label} (tuned: {knobs})" if knobs else f"{label} (tuned)"


def hard_gate_fails(gates: list) -> list:
    """Keys of the HARD gates a graded candidate fails.
    `gates` = analysis.evaluate_gates output ([{key, hard, status, ...}])."""
    return [g["key"] for g in gates
            if g.get("hard") and g.get("status") == "FAIL"]


def plan_for(stock_hard_fails: list, knob_space) -> str:
    """What the tournament does with one method, given its stock-config
    hard-gate verdict and its knob space:

      'full-search'  passes the hard gates and has knobs to tune
      'stock-only'   passes but has NO tunable knobs (competes as-is)
      'probe'        fails a hard gate; a cheap probe decides if any knob helps
      'gate-bound'   fails and has no knobs — nothing to search, by definition
    """
    if list(stock_hard_fails):
        return "probe" if knob_space else "gate-bound"
    return "full-search" if knob_space else "stock-only"


def probe_grid(method) -> list:
    """The cheap targeted probe: ONE knob at a time, single pass over the
    method's knob_space, the method's pinned overrides merged under every
    candidate. Skips candidates identical to the stock config (a pin already
    at that value) — they would just re-measure the failing stock run."""
    def _key(ov):
        return tuple(sorted((str(k), str(x)) for k, x in ov.items()))

    rows, seen = [], {_key(method.overrides)}
    for knob, values in method.knob_space:
        for v in values:
            ov = {**method.overrides, knob: v}
            key = _key(ov)
            if key in seen:
                continue
            seen.add(key)
            rows.append((f"probe {knob}={v}", ov))
    return rows


def search_grid(method) -> list:
    """The method's broad-sweep grid with its pinned overrides merged under
    every row. The 'baseline' row (empty overrides) becomes the pins alone —
    i.e. the method exactly as registered."""
    return [(lbl, {**method.overrides, **ov}) for lbl, ov in method.knob_grid]


def variant_hard_ok(v) -> Optional[bool]:
    """Hard-gate verdict for one OptVariant: conservation AND never an empty
    harvest week. None = unknowable (metrics predate harvest_zero_weeks —
    an old cache entry); callers must treat None as NOT a fix, never as a
    pass — a gate is only ever cleared by a measurement."""
    if not v.conservation_ok:
        return False
    z = getattr(v.metrics, "harvest_zero_weeks", None)
    if z is None:
        return None
    return int(z) == 0


def probe_outcome(hard_oks: list) -> str:
    """'fixable' if ANY probe candidate cleared the hard gates, else
    'gate-bound' (no single knob fixes the method — skip the full search)."""
    return "fixable" if any(ok is True for ok in hard_oks) else "gate-bound"


def floor_eligible(variants: list, stock_min_week) -> list:
    """The variants that do NOT regress the steady-harvest contract relative
    to the method's own STOCK run — i.e. whose worst planner harvest week is
    at least `stock_min_week` fish.

    WHY THIS EXISTS (measured 2026-08-12, operator's 7.29 PR). The emphasis
    objective scores no floor term at all: its only harvest components are a
    CV (`harvest_var`) and an over-the-processing-limit fraction
    (`harvest_overshoot`). Over a 40-variant controller search the worst
    harvest week ranged 7,855..27,462 fish while corr(worst week, score) was
    -0.03 — statistically blind. The search therefore promoted a knob set
    that made the plain controller's worst week 21% WORSE (20,526 -> 16,185),
    bought almost entirely with biomass_overshoot (-0.28 of a -0.35 total
    score move) and density_overshoot (-0.19), while the harvest terms
    together contributed +0.004 — 1% of the decision. Worse, the pool's
    best-floor plan (27,462 fish, deviation 0.02) was evaluated and ranked
    36th of 40, largely because `biomass_util_gap` PENALISES the headroom
    that protects the floor.

    A weight cannot fix that safely — any finite weight is still tradeable,
    and max-normalisation shrinks a component's influence toward zero
    whenever one outlier variant inflates its denominator. So the floor is a
    RANK, not a term: tuning may buy any trade it likes among plans that
    hold the contract at least as well as doing nothing, and may never sell
    the contract for density/biomass/handling gains.

    Returns [] when the guard cannot apply (no baseline, or nothing measured
    it) — callers then fall back to the unguarded pool rather than pretending
    a gate was cleared. `harvest_min_week` None means UNKNOWN (an old cache
    entry), never a pass."""
    if stock_min_week is None:
        return []
    try:
        base = float(stock_min_week)
    except (TypeError, ValueError):
        return []
    out = []
    for v in variants:
        mw = getattr(v.metrics, "harvest_min_week", None)
        if mw is not None and float(mw) >= base:
            out.append(v)
    return out


def ceiling_eligible(variants: list) -> list:
    """The variants that never breach the RELIEF CEILING (the processing limit
    plus its relief band) in any planner week.

    WHY THIS EXISTS (measured 2026-08-13, 8 starting states, 717 plans). The
    weekly processing limit is enforced in the objective by exactly ONE term,
    `harvest_overshoot` — and the `harvest_cap` gate is SOFT, so `pick_winner`
    never filtered on it. That leaves the constraint protected by a single
    weight in a single preset family. The "Product quality" preset sets
    `harvest_overshoot` to weight 0 (every other preset gives it 0.5-2), so
    under it NOTHING in the search — objective or gate — could see a breach.
    Its winners planned 82,181 / 83,152 / 82,626 / 83,504-fish weeks on 4 of 8
    starting states: ~50% over the 55,000 limit and 36-38% over the 60,500
    ceiling the config itself calls never legal. Walk the line breached on
    none, purely because its `harvest_overshoot` weight happens to be 2.

    A constraint whose enforcement depends on one preset's weight is not
    enforced. So, exactly like `floor_eligible`, the ceiling becomes a RANK:
    tuning may buy any trade it likes among plans that stay inside the
    operator's stated ceiling, and may never sell the ceiling for welfare,
    density or biomass gains. This is emphasis-independent by construction.

    Returns [] when nothing measured it (old cache entries) so callers fall
    back to the unguarded pool rather than pretending a gate was cleared.
    `weeks_over_relief_ceiling` None means UNKNOWN, never a pass."""
    out = []
    for v in variants:
        n = getattr(v.metrics, "weeks_over_relief_ceiling", None)
        if n is not None and int(n) == 0:
            out.append(v)
    return out


def pick_winner(variants: list, weights: dict, stock_min_week=None):
    """The tuned winner among a method's evaluated variants: emphasis-best
    among those passing ALL hard gates; if none passes the empty-week rule,
    fall back to conservation-OK only (the board will still show the failure
    honestly — a search never hides a gate). None if nothing conserves.
    Deterministic tie-break by overrides key (parallel == sequential).

    Two emphasis-independent RANKS are then applied, in the operator's own
    priority order, each standing down if it would empty the pool (a search
    still returns its best, and `tune_method` reports which happened):
      1. `ceiling_eligible` — no winner may breach the relief ceiling. The
         objective protects that limit with a single term whose weight is 0
         in one shipped preset, so a weight cannot be the enforcement.
      2. `floor_eligible` — no winner may harvest less in its leanest week
         than the method's own stock run (`stock_min_week`).
    The ceiling is applied FIRST because it is the harder rule: a week over
    the processing ceiling cannot be executed at all, whereas a lean week is
    a shortfall. Applying it first also means the floor guard chooses among
    executable plans rather than ranking an illegal one to the top."""
    from . import optimize
    if not variants:
        return None
    optimize.score_variants(variants, weights)
    full = [v for v in variants if variant_hard_ok(v) is True]
    pool = full or [v for v in variants if v.conservation_ok]
    if not pool:
        return None
    pool = ceiling_eligible(pool) or pool
    pool = floor_eligible(pool, stock_min_week) or pool
    return min(pool, key=lambda v: (v.score, optimize._overrides_key(v.overrides)))


def estimate_budget(method, max_rounds: int = 3) -> dict:
    """Honest pre-run cost estimate for one method's tuned leg, in ENGINE RUNS
    (the unit the operator waits on). The probe only happens when the stock
    run fails a hard gate; descent cost is an upper bound (per-round axis
    sweep, early-stopped on no improvement)."""
    probe = len(probe_grid(method))
    grid = len(method.knob_grid)
    per_round = sum(len(vs) for _, vs in method.knob_space)
    return {
        "stock": 1,
        "probe_if_gate_fails": probe,
        "grid": grid,
        "descent_max": per_round * max_rounds,
        "verify": 1 if (grid or probe) else 0,
        "max_total": 1 + probe + grid + per_round * max_rounds
        + (1 if (grid or probe) else 0),
    }


def cached_count(variant_cache, grid) -> int:
    """How many of `grid`'s rows are already in the cross-run variant cache —
    the honest 'reuse makes this cheap' number shown before a search."""
    from . import optimize
    if variant_cache is None:
        return 0
    return sum(1 for _, ov in grid
               if optimize._overrides_key(ov) in variant_cache)


# --------------------------------------------------------------------------- #
# The shared per-method search driver
# --------------------------------------------------------------------------- #
def tune_method(method, input_path, config_dir, scenario_dir, *,
                emphasis, weights=None, stock_hard_fails=(),
                progress=None, max_workers=None, variant_cache=None,
                max_rounds: int = 3, stock_min_week=None) -> dict:
    """Run one method's tournament leg (search only — the caller owns the
    stock run, the grading, and the winner's verification run).

    `stock_min_week` = the method's own stock-config worst planner harvest
    week. Pass it: it arms the contract-floor no-regression guard (see
    `floor_eligible`) so a tuned winner can never be a knob set that harvests
    LESS in the leanest week than not tuning at all. Omitted, the guard is
    inert and selection is emphasis-score only (the pre-2026-08-12 behaviour).

    Returns {method, plan, status, winner_overrides, probe, variants,
    floor_guard}:
      status 'tuned'        winner_overrides = the method's best knob set
                            (its pins INCLUDED — apply as-is / promote as-is)
             'stock-only'   nothing to tune; competes at stock
             'gate-bound'   hard-gate failure no probed knob fixes
             'search-failed' no search variant conserved (engine refused all)
      floor_guard  'off' (no baseline given) | 'applied' (the winner came from
                   the no-regression pool) | 'stood-down' (nothing held the
                   floor — the winner is emphasis-best and REGRESSES it)
    """
    from . import optimize
    if (method.knob_space or method.knob_grid) and method.engine != "controller":
        # The search harness runs forecast.run.main. Tuning a non-controller
        # method through it would measure the WRONG engine and could promote
        # knobs the method never saw — refuse loudly instead.
        raise NotImplementedError(
            f"method {method.key!r}: engine {method.engine!r} has a knob "
            f"space, but the search harness only runs the controller engine")
    w = weights or optimize.weights_for(emphasis)
    plan = plan_for(list(stock_hard_fails), method.knob_space)
    out = {"method": method.key, "plan": plan, "status": plan,
           "winner_overrides": None, "probe": None, "variants": [],
           "floor_guard": "off", "stock_min_week": stock_min_week}
    if plan in ("gate-bound", "stock-only"):
        return out

    if plan == "probe":
        grid = probe_grid(method)
        pv = optimize.sweep(str(input_path), str(config_dir),
                            str(scenario_dir), grid=grid, progress=progress,
                            max_workers=max_workers,
                            variant_cache=variant_cache)
        out["probe"] = pv
        if probe_outcome([variant_hard_ok(v) for v in pv]) == "gate-bound":
            out["status"] = "gate-bound"
            out["variants"] = pv
            return out
        # a knob CAN fix the gate -> the method earns the full search
        # (the probe runs are already in the variant cache — near-free reuse)

    results = optimize.deep_search_combined(
        str(input_path), str(config_dir), str(scenario_dir),
        emphasis=emphasis, weights=w, grid=search_grid(method),
        knob_space=(list(method.knob_space) or None), max_rounds=max_rounds,
        progress=progress, max_workers=max_workers,
        variant_cache=variant_cache)
    out["variants"] = results
    best = pick_winner(results, w, stock_min_week=stock_min_week)
    if best is None:
        out["status"] = "search-failed"
        return out
    if stock_min_week is not None:
        out["floor_guard"] = ("applied" if floor_eligible(
            [v for v in results if v.conservation_ok], stock_min_week)
            else "stood-down")
    out["status"] = "tuned"
    out["winner_overrides"] = dict(best.overrides)
    out["winner_label"] = best.label
    return out
