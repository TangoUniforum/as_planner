"""The "lobby": a registry of interchangeable production-planning methods.

Every method consumes the SAME inputs — the PR workbook + the app's config
(control / biology / facility) + scenario (batches / limits / manual_events) —
and produces a full forecast workbook at a caller-chosen output path. Because
the methods share the config + scenario (including scenario/manual_events.yaml,
the manual override window every method applies identically), the runs are
apples-to-apples on the INPUTS: the SAME "manual entries are law" starting
state and the SAME control rules. That is the point — it lets the operator run
several methods and compare the results to be confident the plan they select is
the best available, not just the first one produced.

EVERY REGISTERED METHOD IS THE SAME ENGINE. All of them run forecast/run.py
over forecast/placement.py, which charges handling mortality on every
tank-to-tank deposit and carries the OG1/2 density relief, consolidation and
chronic-pressure work; they share the R8 density exemption (forecast/tiers.py),
the core biology (forecast/biology.py) and the imperfect grader
(`grade_efficiency`, read in placement.py and manual_events.py). What separates
them is the `overrides` block — an L1 harvest guide, an LNS placement pass, a
tank-feasibility pass — layered on the one engine. So every axis is comparable
across the roster, transfer counts and density-relief behaviour included.

This is the extension point: a newly-available method (a new placement backend,
a new solver) becomes comparable by adding ONE `register(...)` call here — the
compare driver (tools/run_compare.py) and the RunComparison sheet
(excel_io.write_run_comparison) need no change. A method that brings its OWN
engine, rather than an overrides patch on this one, also has to say what it
does and does not read: the handling-budget gate
(`analysis._gate_handling_budget`) grades on `max_transfers_per_week`, and a
plan produced without reading it is not comparable on move counts.

Nothing here mutates the caller's config / scenario dirs: each run executes in
an isolated temp copy (mirrors forecast.tuning._run_in_tempdir), so a method's
per-run control overrides (e.g. placement_method='lns') never leak between
methods or touch the user's files. The PR workbook is copied in too, so the
source is never written back.
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
#   * the operational RULES — max_harvest_per_week (THE weekly processing
#     limit), harvest_relief_pct (the exceptional-weeks relief band above it,
#     2026-08-09 semantics), min_harvest_per_week (contract floor),
#     max_transfers_per_week (handling budget). These are CONSTRAINTS the plan
#     must respect at their configured values; a search that "tunes" a rule is
#     just relaxing the rule. (harvest_target_per_week is DELETED from config
#     but stays listed so no space can ever resurrect it.)
# register() enforces this structurally — an illegal space cannot register.
#
UNTUNABLE_KNOBS = frozenset({
    # OPERATOR INPUTS, not levers (operator ruling 2026-08-22). A tuner may
    # change HOW the model plans; it may not change WHAT the facility is or
    # WHAT the business requires. These three describe the operation itself:
    #   min_tank_control      the minimum fish you will operate a tank with
    #   tran_og_default_tanks how many tanks a TranOG arrival needs
    #   density_target_pct    how full you are willing to run a tank
    # Moving them does not find a better plan for THIS facility, it finds a
    # plan for a different one -- and the 2026-08-22 tournament did exactly
    # that, pinning min_tank_control 7000 -> 12000 in every tuned winner and
    # claiming a 2.6% score gain partly bought by redefining the input.
    # density_target_pct is the direct expression of the operator's
    # minimise-density stance; a search free to raise it (it reached 0.95)
    # optimises against the very preference it is meant to serve.
    # ARM IDENTITY (2026-08-27). These two decide whether the L1 guide steers
    # ANYTHING, so they are what makes controller-hybrid a hybrid -- not policy
    # within it. While they were tunable the tuner switched them OFF on the
    # hybrid arm (measured 2026-08-25), leaving it byte-identical to the plain
    # controller on every metric across all 21 PRs while still appearing on the
    # board as a distinct method. A tournament then "agreed" three ways when it
    # had run one method three times.
    "hybrid_production_lever",
    "hybrid_purge_lever",
    "min_tank_control",
    "tran_og_default_tanks",
    "density_target_pct",
    "min_harvest_weight_g",
    "max_harvest_per_week",
    "harvest_relief_pct",
    "harvest_target_per_week",     # deleted knob — kept unresurrectable
    "min_harvest_per_week",
    "max_transfers_per_week",
    # PHYSICAL FACTS, not levers. A search is free to trade policy; it is not
    # free to redefine the equipment or the biology to improve its own score.
    # grade_efficiency describes how cleanly the GRADER separates sizes —
    # letting the optimizer push it to 1.0 would inflate the big leg of every
    # graded harvest and score better for it, which is fitting the model to the
    # objective. handling_mortality_pct is the same: a measured loss per
    # deposit, and a search that lowered it would "win" by not killing fish it
    # actually kills.
    "grade_efficiency",
    "handling_mortality_pct",
    # A MODELLING ASSUMPTION, not a lever. Letting a search turn the 6N prime
    # back on would let it buy a smoother harvest score with a plan the tank
    # picker cannot execute — choosing fiction because fiction scores better,
    # which is the same failure mode as tuning the grader.
    "global_assume_primed_6n",
    # A SAFETY GUARD, not a lever (2026-08-21). sixn_level_drains is what stops
    # a raised move-in accumulating into ONE 6N pair and starving the others —
    # the documented 90-113k drain-spike backfire — and hybrid_guide.py refuses
    # the purge lever outright while it is off rather than steering around it.
    # A search that could switch a guard off to score better is a search that
    # sells a rule to buy a number.
    "sixn_level_drains",
    # DEFINES WHICH ARM YOU ARE RUNNING, so it is not the search's to set: a
    # space containing it could turn `controller` into `controller-hybrid` and
    # have the Compare board unknowingly compare a method with itself. The
    # levers above ARE tunable — they are policy WITHIN an arm; this is the
    # arm's identity. (hybrid_follow_band stays tunable for the same reason:
    # band width is policy, not identity.)
    "hybrid_follow",
})

# Controller-family space: the existing full optimizer space — the broad grid
# (optimize.OPT_FULL_GRID) + the coordinate-descent axes (optimize.CD_KNOB_SPACE).
# Every knob in it is read by the controller engine (forecast/placement.py +
# forecast/run.py), which is what all Controller-family methods run.
CONTROLLER_KNOB_GRID = tuple((lbl, dict(ov)) for lbl, ov in OPT_FULL_GRID)
CONTROLLER_KNOB_SPACE = tuple((k, tuple(vs)) for k, vs in CD_KNOB_SPACE)


# --------------------------------------------------------------------------- #
# Method definition
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Method:
    """One interchangeable planner.

    key      stable id used on the command line and as the RunComparison column
    label    legible name shown on the sheet
    family   "Controller" — the one remaining family (groups the columns)
    blurb    one-line, human description of HOW this method plans
    engine   which callable runs it: "controller" (forecast.run.main), the only
             engine there is
    overrides   control.yaml patches applied in the temp copy before the run
                (e.g. {"placement_method": "lns"}); does NOT touch the user file
    engine_kwargs   extra keyword args passed to the engine callable; the
                    controller engine takes none, so this is empty today
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
        # calib_log_path="" — RECORD NOTHING. This harness isolates config and
        # scenario into a tempdir but used to leave the FW calibration log
        # pointing at the operational fw_calibration_history.jsonl, so every
        # Compare-board run, every tuning trial and every tournament arm
        # appended to it. That file exists to expose a STANDING model error
        # across months of real runs ("a correction the model has needed every
        # month for six months looked exactly like a one-off", accuracy.py);
        # measured 2026-08-21, 51% of its 30,712 records had been written that
        # single day by exploratory runs, and since every record carries
        # source="run.main" there is no way to filter them out afterwards.
        # tools/backtest.py and tests/fixtures/freeze_golden.py both already
        # guard this; the method runner is the one path that did not.
        return _run.main(inp, out, config_dir=cdir, scenario_dir=sdir,
                         calib_log_path="")
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
    ANYONE."""
    knobs = _space_knobs(method)
    illegal = knobs & UNTUNABLE_KNOBS
    if illegal:
        raise ValueError(
            f"method {method.key!r}: knob space contains untunable business/"
            f"rule knob(s) {sorted(illegal)} — these are fixed constraints "
            f"(see UNTUNABLE_KNOBS)")


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
    blurb="Reactive week-by-week planner: greedy placement, a multi-objective "
          "rebalancer, and the 2026-08-21 density policy — OG1/2 relief, "
          "chronic-pressure anticipation, and consolidation that frees a tank "
          "by packing a batch into fewer of its own. Handling mortality is "
          "charged on every deposit. "
          "The long-standing production engine and the greedy baseline. "
          "RE-MEASURED 2026-08-21 across all 21 PRs in pr_corpus (era "
          "registries, current code): it leaves at least one COMPLETELY EMPTY "
          "harvest week on 10 of 21 PRs, averaging 4.6 empty weeks per plan, "
          "with 25.0 weeks per plan under the contract floor and a worst "
          "non-empty week of 7,129 fish. So it still does not meet the "
          "steady-harvest contract rule.",
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
    blurb="Controller with a large-neighborhood-search pass that RE-LABELS "
          "which grow-out tank each occupancy segment sits in, moving load off "
          "the hottest systems (audit-gated). It emits no extra Transfers, so "
          "unlike a real move it costs neither handling mortality nor handling "
          "budget — its transfer count is comparable with the plain controller.",
))
register(Method(
    key="controller-feasible",
    label="Controller — plan-feasible tanks",
    family="Controller",
    engine="controller",
    # hybrid_follow pinned off for the same reason as `controller`: keep this
    # arm's ONE variable the feasibility pass, not the hybrid.
    overrides={"hybrid_follow": "off", "plan_tank_feasibility": True},
    knob_grid=CONTROLLER_KNOB_GRID,
    knob_space=CONTROLLER_KNOB_SPACE,
    blurb="Controller whose CANVAS plans within the tanks that exist. The "
          "canvas already detects, weeks ahead, that OG tank demand exceeds "
          "placeable supply, then plans past it — the detection is handed to "
          "the assignment planner and only ever appended to, never read. The "
          "excess is not need: each SW week's tank count is raised to that "
          "batch's own peak over the next 6 weeks, per batch, with nothing "
          "arbitrating the sum. This arm hands those forward reservations "
          "back — deepest slack first, never below a batch's need that week — "
          "until each week fits. MEASURED ON 3 PRs, IT DOES NOT WIN: the "
          "tank-supply shortfall goes to zero and transfer legs, ceiling "
          "breaches and worst density mostly improve (199 -> 110 kg/m3 on "
          "8/19), but tonnage slips on all three, refusals RISE, and 1-3 "
          "weeks go over the handling budget. Kept in the lobby because it "
          "isolates a real planning question on YOUR PR, not because it is "
          "the better plan.",
))
register(Method(
    key="controller-hybrid",
    label="Controller — hybrid (L1-guided harvest)",
    family="Controller",
    engine="controller",
    # band 0.05, not the 0.10 knob default: measured tighter on every axis that
    # matters (see blurb). The band is the CEILING half — a narrower band clamps
    # harvest less in the fat weeks, so fewer fish are held back for later.
    # NOTE (2026-08-26): pinning the LEVERS here to cure the inert stock arm was
    # tried and REVERTED. `overrides` are PINS -- tests/test_tuned_tournament.py
    # asserts every probe variant carries each override at its pinned value --
    # and the levers live in the TUNABLE knob space, so pinning them makes a
    # knob simultaneously fixed identity and tunable policy. The test caught it.
    # The inertness is real but only affects someone choosing the STOCK arm on
    # the board; a TUNED tournament already reaches the levers through
    # optimize.OPT_FULL_GRID (hybrid:prod-lever / both-levers / levers-off) and
    # on 2026-08-25 it searched them and chose OFF. Curing it properly means
    # deciding whether the levers are ARM IDENTITY (untunable) or POLICY
    # (tunable) -- an architecture call, not a default to flip.
    overrides={"hybrid_follow": "full", "hybrid_follow_band": 0.05,
               "hybrid_production_lever": True, "hybrid_purge_lever": True},
    knob_grid=CONTROLLER_KNOB_GRID,
    knob_space=CONTROLLER_KNOB_SPACE,
    blurb="The validated controller with the tankless L1 harvest envelope "
          "(forecast/global_planner_poc.py) fed in as a per-week target band. "
          "*** THIS ARM STEERS — the 'INERT' warning that stood here until "
          "2026-09-03 was FALSE and contradicted this Method's own overrides "
          "dict a few lines above. It pins hybrid_production_lever=True AND "
          "hybrid_purge_lever=True (added 2026-08-27), and config/control.yaml "
          "ships both `true` as well, so either route alone would be enough. "
          "The production half is live on every non-purge week. The PURGE half "
          "is refused at guide-build time while `sixn_level_drains: false` "
          "(hybrid_guide.py logs the refusal to the ValidationLog), so the "
          "LIVE configuration is the production-lever-alone arm — read that "
          "row of the measurements below, not the both-levers one. "
          "The 'BYTE-IDENTICAL to the plain controller across 21 PRs' result "
          "(2026-08-21: 4.6 empty harvest weeks per plan, 25.0 weeks under "
          "floor, worst week 7,129 fish) PREDATES the 2026-08-27 pins and no "
          "longer describes this arm. THE LEVERS HELP, on the "
          "basis that counts: on the REAL workbook with the LIVE scenario, "
          "weeks under the contract floor fall 20 -> 16 with both levers and "
          "20 -> 14 with the production lever alone, with ZERO empty harvest "
          "weeks throughout. (A corpus-wide run said the opposite — 4.6 -> 6.3 "
          "empty weeks — but that basis is contaminated: the reconstructed "
          "registries carry 7 of 19 batches with no real calibration and "
          "inferred arrival dates, and they produce 4.6 empty weeks per plan "
          "where the real workbook produces NONE. Do not compare methods on "
          "reconstructed corpus registries.) The levers ARE now reachable "
          "by the optimizer -- both sit in CONTROLLER_KNOB_SPACE and the "
          "grid carries hybrid:prod-lever / both-levers / levers-off -- so "
          "a TUNED tournament can switch them off as well as on. "
          "The 2026-08-03 figures below predate four changes and have not been "
          "reproduced. *** "
          "The figures that follow were measured across "
          "6 real July-2026 PRs on 2026-08-03 WITH THE LEVERS ON — BEFORE the "
          "2026-08-20/21 changes: handling mortality per deposit, "
          "grade_efficiency 0.85, purge move-in Thursday, 6N one-batch-one-"
          "tank. All four move the weekly harvest series, so re-measure before "
          "relying on these deltas): 0 zero-harvest weeks vs the controller's "
          "6, weeks under the contract floor 22.5 -> 9.0, worst week 0 -> "
          "16,148 fish. Costs peak biomass 102.6 -> 107.1% of cap: holding "
          "fish back for a lean week means they are still in the water. (A "
          "peak-density figure sat here too; it was measured BEFORE R8 removed "
          "the cap from purge and harvest-prep tanks — it counted tanks that no "
          "longer have one, so it has been withdrawn rather than restated.) "
          "Its L1 envelope is the one place global_assume_primed_6n is still "
          "read, so that knob shapes this arm's first ~2 weeks. "
          "Every HARVEST-side knob that shrinks that peak (wider deviation "
          "band, guide smoothing) puts empty weeks back — the spike IS the "
          "reserve; the 2026-08-21 DENSITY knobs are a different lever that "
          "sheds density by consolidating a batch into fewer of its own tanks "
          "and costs no harvest weeks. Chosen over band 0.10 and "
          "over deviation 0.025 by a 90-cell paired sweep: it is also the most "
          "STABLE arm, holding 0-1 blackout weeks under neutral perturbation "
          "where the alternatives drift to 3-4.",
))


# Default comparison roster. controller-hybrid is IN it as of 2026-08-03: with
# the zero-harvest-week blind spot fixed (34ecbaf) the plain controller was shown
# to breach the never-an-empty-week rule on 5 of 6 real PRs, while the hybrid
# breaches it on none. The old exclusion note here predated that measurement.
# `controller-feasible` joins the DEFAULT roster, not just the registry: a
# method the app's board and the tuned tournament never run is not "available",
# it is invisible. It is off-by-default at the KNOB level (plan_tank_feasibility
# false), so its presence here costs one more arm per tournament and changes no
# plan anyone adopts unless they pick it.
DEFAULT_ROSTER = ["controller", "controller-hybrid", "controller-lns",
                  "controller-feasible"]

# Everything registered. Kept as a distinct name from DEFAULT_ROSTER because
# callers reference it by name; it is equal to DEFAULT_ROSTER now that the
# Global family is gone.
FULL_ROSTER = ["controller", "controller-hybrid", "controller-lns",
               "controller-feasible"]


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
