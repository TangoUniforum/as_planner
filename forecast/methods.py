"""The "lobby": a registry of interchangeable production-planning methods.

Every method consumes the SAME inputs — the PR workbook + the app's config
(control / biology / facility) + scenario (batches / limits / manual_events) —
and produces a full forecast workbook at a caller-chosen output path. Because
the methods share the config + scenario (including scenario/manual_events.yaml,
the manual override window that BOTH engines apply identically), the runs are
apples-to-apples: the SAME "manual entries are law" starting state and the SAME
control rules; only the PLANNING METHOD differs. That is the whole point — it
lets the operator run several methods and compare the results to be confident
the plan they select is the best available, not just the first one produced.

This is the extension point: a newly-available method (a new placement backend,
a new solver) becomes comparable by adding ONE `register(...)` call here — the
compare driver (tools/run_compare.py) and the RunComparison sheet
(excel_io.write_run_comparison) need no change.

Nothing here mutates the caller's config / scenario dirs: each run executes in
an isolated temp copy (mirrors forecast.tuning._run_in_tempdir), so a method's
per-run control overrides (e.g. placement_method='lns') never leak between
methods or touch the user's files. The PR workbook is copied in too, so the
source is never written back.

The rigid front-end (L1 tankless harvest + facility-share) is identical across
the Global methods; only the PLACEMENT layer differs (LP vs CP-SAT). A true
Global rigid-greedy (L2 water-filler, no LP) is not yet a wired mode — when it
is, it registers here beside `global-lp` / `global-milp` and joins the roster.
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional

import yaml

from .optimize import CD_KNOB_SPACE, OPT_FULL_GRID


# --------------------------------------------------------------------------- #
# Tunable knob spaces — what the TUNED tournament may search, per method
# --------------------------------------------------------------------------- #
# Knobs that are NEVER tunable, by ANY method's search (operator ruling):
#   * min_harvest_weight_g — a BUSINESS constant (the sellable size), not a
#     planner preference. (Stocking count/size are business inputs too, but
#     they live in scenario/batches.yaml, so a control-knob space cannot
#     reach them by construction.)
#   * the wave-3 operational RULES — max_harvest_per_week (hard processing
#     ceiling), harvest_target_per_week (planning target), min_harvest_per_week
#     (contract floor), max_transfers_per_week (handling budget). These are
#     CONSTRAINTS the plan must respect at their configured values; a search
#     that "tunes" a rule is just relaxing the rule.
# register() enforces this structurally — an illegal space cannot register.
UNTUNABLE_KNOBS = frozenset({
    "min_harvest_weight_g",
    "max_harvest_per_week",
    "harvest_target_per_week",
    "min_harvest_per_week",
    "max_transfers_per_week",
})

# Controller-family space: the existing full optimizer space — the broad grid
# (optimize.OPT_FULL_GRID) + the coordinate-descent axes (optimize.CD_KNOB_SPACE).
# Every knob in it is read by the controller engine (forecast/placement.py +
# forecast/run.py), which is what all Controller-family methods run.
CONTROLLER_KNOB_GRID = tuple((lbl, dict(ov)) for lbl, ov in OPT_FULL_GRID)
CONTROLLER_KNOB_SPACE = tuple((k, tuple(vs)) for k, vs in CD_KNOB_SPACE)

# Global-family space: EMPTY — verified 2026-08-09 by grepping the whole global
# path (global_planner_poc / _l2 / _l3 / _loop / global_tank_pick_poc /
# global_placement_milp_poc / global_forecast / tools.run_global_forecast) for
# control-knob reads:
#   * The only TUNABLE knobs the global path reads are
#     facility_biomass_deviation_pct (L1) and density_target_pct (L3) — and
#     overriding exactly those (plus tran_og) was experimentally shown to BREAK
#     Global's conservation proof (2026-08-07). Excluded.
#   * The proposed safe candidates are NOT consumed by the global path:
#     global_buffer_pct is read only by caps.system_cap_with_buffer, whose sole
#     caller is the CONTROLLER's placement.py; hybrid_guide_smooth_weeks is read
#     only by hybrid_guide.py, which only forecast/run.py (controller) calls.
#     A knob a method doesn't read must not be in its space.
#   * Everything else it reads is a fixed rule/constant (max_biomass_kg,
#     max_harvest_per_week, min_harvest_weight_g, horizon, 6N dates).
# So the honest Global space is empty: the Globals compete at stock config, and
# a hard-gate failure there is 'gate-bound' (no knob can fix it), not tunable.
GLOBAL_KNOB_GRID: tuple = ()
GLOBAL_KNOB_SPACE: tuple = ()

# Knobs a GLOBAL method's space may ever contain. Currently empty (see above);
# if a conservation-safe, actually-consumed global knob is found later, add it
# here WITH the grep + conservation evidence, and the registry check relaxes.
GLOBAL_CONSERVATION_SAFE_KNOBS = frozenset()


# --------------------------------------------------------------------------- #
# Method definition
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Method:
    """One interchangeable planner.

    key      stable id used on the command line and as the RunComparison column
    label    legible name shown on the sheet
    family   "Controller" | "Global" (groups the columns)
    blurb    one-line, human description of HOW this method plans
    engine   which callable runs it: "controller" (forecast.run.main) or
             "global" (tools.run_global_forecast.run_global)
    overrides   control.yaml patches applied in the temp copy before the run
                (e.g. {"placement_method": "lns"}); does NOT touch the user file
    engine_kwargs   extra keyword args passed to the engine callable
                    (e.g. {"optimal": True} to select CP-SAT placement)
    knob_grid   broad-sweep rows ((label, {knob: value}), ...) the tuned
                tournament may run for THIS method — every row is layered ON TOP
                of `overrides` (the method's pins stay pinned)
    knob_space  coordinate-descent axes ((knob, (values...)), ...) for the same
                search; also the single-pass probe used when the method fails a
                hard gate at stock config
    """
    key: str
    label: str
    family: str
    blurb: str
    engine: str
    overrides: dict = field(default_factory=dict)
    engine_kwargs: dict = field(default_factory=dict)
    knob_grid: tuple = ()
    knob_space: tuple = ()


# --------------------------------------------------------------------------- #
# The isolated run harness (engine-parametric generalization of
# forecast.tuning._run_in_tempdir).
# --------------------------------------------------------------------------- #
def _run_engine(engine: str, inp, out, cdir, sdir, engine_kwargs: dict) -> int:
    if engine == "controller":
        from forecast import run as _run
        return _run.main(inp, out, config_dir=cdir, scenario_dir=sdir)
    if engine == "global":
        from tools.run_global_forecast import run_global
        return run_global(inp, out, config_dir=cdir, scenario_dir=sdir,
                          **engine_kwargs)
    raise ValueError(f"unknown engine {engine!r}")


def run_method(method: Method, input_path, out_path,
               base_config_dir, base_scenario_dir, *, quiet: bool = True):
    """Run `method` in an isolated temp copy of config + scenario with its
    control overrides applied, writing the full forecast workbook to `out_path`
    (which lives OUTSIDE the temp dir, so it persists for drill-in). Returns
    (rc, elapsed_seconds). Never mutates the caller's dirs or the PR workbook.
    """
    work = tempfile.mkdtemp(prefix=f"as_cmp_{method.key}_")
    try:
        cdir = os.path.join(work, "config")
        sdir = os.path.join(work, "scenario")
        shutil.copytree(str(base_config_dir), cdir)
        shutil.copytree(str(base_scenario_dir), sdir)
        if method.overrides:
            cy = os.path.join(cdir, "control.yaml")
            with open(cy) as f:
                cfg = yaml.safe_load(f) or {}
            cfg.update(method.overrides)
            with open(cy, "w") as f:
                yaml.safe_dump(cfg, f)
        inp = os.path.join(work, os.path.basename(str(input_path)))
        shutil.copy(str(input_path), inp)

        t0 = time.time()
        cm = (contextlib.redirect_stdout(io.StringIO()) if quiet
              else contextlib.nullcontext())
        with cm:
            rc = _run_engine(method.engine, inp, str(out_path), cdir, sdir,
                             method.engine_kwargs)
        return rc, time.time() - t0
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #
REGISTRY: "dict[str, Method]" = {}


def _space_knobs(method: Method) -> set:
    """Every knob name the method's search space can touch."""
    knobs = {k for k, _ in method.knob_space}
    for _, ov in method.knob_grid:
        knobs.update(ov)
    return knobs


def _validate_knob_space(method: Method) -> None:
    """Structural guarantees on a method's tunable space (fail at register time,
    not mid-tournament): business constants / operational rules are untunable by
    ANYONE, and a Global method may only carry knobs proven conservation-safe
    AND consumed by the global path (currently: none)."""
    knobs = _space_knobs(method)
    illegal = knobs & UNTUNABLE_KNOBS
    if illegal:
        raise ValueError(
            f"method {method.key!r}: knob space contains untunable business/"
            f"rule knob(s) {sorted(illegal)} — these are fixed constraints "
            f"(see UNTUNABLE_KNOBS)")
    if method.engine == "global":
        unsafe = knobs - GLOBAL_CONSERVATION_SAFE_KNOBS
        if unsafe:
            raise ValueError(
                f"method {method.key!r}: global-engine knob space contains "
                f"{sorted(unsafe)} — not in GLOBAL_CONSERVATION_SAFE_KNOBS "
                f"(placement-side overrides broke Global conservation, "
                f"2026-08-07; a knob the global path doesn't read must not "
                f"be in its space)")


def register(method: Method) -> None:
    _validate_knob_space(method)
    REGISTRY[method.key] = method


register(Method(
    key="controller",
    label="Controller — reactive greedy",
    family="Controller",
    engine="controller",
    # MUST pin hybrid_follow off EXPLICITLY. control.yaml now ships the hybrid on
    # (see there), and run_method layers a method's overrides on top of that base
    # — so without this line the "controller" arm would silently BE the hybrid and
    # every controller-vs-hybrid comparison would run the same plan twice. That is
    # not hypothetical: an A/B whose override equalled the live config value is
    # exactly how grade-to-min was wrongly recorded as inert (2026-08-03).
    overrides={"hybrid_follow": "off"},
    knob_grid=CONTROLLER_KNOB_GRID,
    knob_space=CONTROLLER_KNOB_SPACE,
    blurb="Reactive week-by-week planner: greedy placement + multi-objective "
          "rebalancer. The long-standing production engine and the greedy "
          "baseline — but it leaves a totally empty harvest week on 5 of 6 real "
          "PRs, so it no longer meets the steady-harvest contract rule.",
))
register(Method(
    key="controller-lns",
    label="Controller — greedy + LNS",
    family="Controller",
    engine="controller",
    # Same reason as `controller` above: isolate the LNS variable from the hybrid.
    overrides={"placement_method": "lns", "hybrid_follow": "off"},
    knob_grid=CONTROLLER_KNOB_GRID,
    knob_space=CONTROLLER_KNOB_SPACE,
    blurb="Controller with a large-neighborhood-search pass that relocates / "
          "swaps grow-out occupancy off the hottest systems (audit-gated).",
))
register(Method(
    key="global-lp",
    label="Global — lexicographic LP",
    family="Global",
    engine="global",
    engine_kwargs={"optimal": False},
    # Knob space EMPTY (GLOBAL_KNOB_*): no conservation-safe knob the global
    # path actually reads exists — see the evidence block above GLOBAL_KNOB_GRID.
    knob_grid=GLOBAL_KNOB_GRID,
    knob_space=GLOBAL_KNOB_SPACE,
    blurb="Precalculated cascade: tankless harvest (L1) -> per-batch facility "
          "share -> lexicographic LP placement (L3) -> continuity tank pick.",
))
register(Method(
    key="global-milp",
    label="Global — CP-SAT optimal",
    family="Global",
    engine="global",
    engine_kwargs={"optimal": True},
    # Same empty space as global-lp (same L1/L3 knob consumption).
    knob_grid=GLOBAL_KNOB_GRID,
    knob_space=GLOBAL_KNOB_SPACE,
    blurb="Same L1 cascade + facility share, but the whole-horizon grow-out "
          "layout is placed by a CP-SAT optimal (0-swap) solver, not the LP.",
))
register(Method(
    key="controller-hybrid",
    label="Controller — hybrid (L1-guided harvest)",
    family="Controller",
    engine="controller",
    # band 0.05, not the 0.10 knob default: measured tighter on every axis that
    # matters (see blurb). The band is the CEILING half — a narrower band clamps
    # harvest less in the fat weeks, so fewer fish are held back for later.
    overrides={"hybrid_follow": "full", "hybrid_follow_band": 0.05},
    knob_grid=CONTROLLER_KNOB_GRID,
    knob_space=CONTROLLER_KNOB_SPACE,
    blurb="The validated controller with the Global engine's L1 harvest "
          "envelope fed in as a per-week target band. Meets the never-an-empty-"
          "week contract rule; the plain controller does NOT. Measured across "
          "6 real July-2026 PRs (2026-08-03, fixed harvest metric): 0 zero-"
          "harvest weeks vs the controller's 6, weeks under the contract floor "
          "22.5 -> 9.0, worst week 0 -> 16,148 fish. Costs peak biomass 102.6 "
          "-> 107.1% of cap and peak density 102 -> 124: holding fish back for "
          "a lean week means they are still in the water. Every knob that "
          "shrinks that peak (wider deviation band, guide smoothing) puts empty "
          "weeks back — the spike IS the reserve. Chosen over band 0.10 and "
          "over deviation 0.025 by a 90-cell paired sweep: it is also the most "
          "STABLE arm, holding 0-1 blackout weeks under neutral perturbation "
          "where the alternatives drift to 3-4.",
))


# Default comparison roster. controller-hybrid is IN it as of 2026-08-03: with
# the zero-harvest-week blind spot fixed (34ecbaf) the plain controller was shown
# to breach the never-an-empty-week rule on 5 of 6 real PRs, while the hybrid
# breaches it on none. The old exclusion note here predated that measurement.
DEFAULT_ROSTER = ["controller", "controller-hybrid", "controller-lns",
                  "global-lp", "global-milp"]


def get_roster(keys: "Optional[list[str]]" = None) -> "list[Method]":
    """Resolve method keys to Method objects (defaults to DEFAULT_ROSTER).
    Raises KeyError with the available keys if an unknown key is requested."""
    keys = list(keys) if keys else list(DEFAULT_ROSTER)
    out = []
    for k in keys:
        if k not in REGISTRY:
            raise KeyError(f"unknown method {k!r}; available: "
                           f"{', '.join(sorted(REGISTRY))}")
        out.append(REGISTRY[k])
    return out
