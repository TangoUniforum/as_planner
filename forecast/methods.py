"""The "lobby": a registry of interchangeable production-planning methods.

Every method consumes the SAME inputs — the PR workbook + the app's config
(control / biology / facility) + scenario (batches / limits / manual_events) —
and produces a full forecast workbook at a caller-chosen output path. Because
the methods share the config + scenario (including scenario/manual_events.yaml,
the manual override window that BOTH engines apply identically), the runs are
apples-to-apples on the INPUTS: the SAME "manual entries are law" starting
state and the SAME control rules. That is the point — it lets the operator run
several methods and compare the results to be confident the plan they select is
the best available, not just the first one produced.

THEY ARE NOT IDENTICAL MODELS, and since 2026-08-21 the gap is material. The
Controller family runs forecast/placement.py, which charges handling mortality
on every tank-to-tank deposit and carries the OG1/2 density relief,
consolidation and chronic-pressure work. The Global family runs its own
placement and none of that: its transfers are FREE and it has no density-relief
policy. Both families DO share the R8 density exemption (forecast/tiers.py) and the
core biology (forecast/biology.py) -- but NOT the imperfect grader:
`grade_efficiency` is read only in placement.py and manual_events.py, so a
Global arm grades perfectly at the cut line while a Controller arm does not.

So: compare harvest SHAPE and contract compliance across families, but compare
transfer counts and density-relief behaviour only WITHIN a family. A
transfer-heavy Global plan is not being taxed the way a Controller plan is.

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
#   * the operational RULES — max_harvest_per_week (THE weekly processing
#     limit), harvest_relief_pct (the exceptional-weeks relief band above it,
#     2026-08-09 semantics), min_harvest_per_week (contract floor),
#     max_transfers_per_week (handling budget). These are CONSTRAINTS the plan
#     must respect at their configured values; a search that "tunes" a rule is
#     just relaxing the rule. (harvest_target_per_week is DELETED from config
#     but stays listed so no space can ever resurrect it.)
# register() enforces this structurally — an illegal space cannot register.
#
# NOTE, because "the plan must respect them" is only true of one family: the
# Global engine reads max_harvest_per_week and min_harvest_per_week but NEVER
# reads harvest_relief_pct or max_transfers_per_week. So a Global plan carries
# no handling budget and no relief-band semantics at all — if a Global column
# fails one of those gates on the board, that is a MODELLING GAP in the Global
# path, not a knob the operator can turn.
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
    blurb="Reactive week-by-week planner: greedy placement, a multi-objective "
          "rebalancer, and the 2026-08-21 density policy — OG1/2 relief, "
          "chronic-pressure anticipation, and consolidation that frees a tank "
          "by packing a batch into fewer of its own. Handling mortality is "
          "charged on every deposit here; it is NOT on the Global arms. "
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
          "share -> lexicographic LP placement (L3) -> continuity tank pick. "
          "Runs its OWN placement — NOT forecast/placement.py — so none of the "
          "controller's 2026-08-21 density work applies here: no OG1/2 "
          "density relief, no consolidation-to-free-tanks, no chronic-"
          "pressure anticipation, and NO handling mortality on its "
          "transfers (its moves are FREE, so its transfer count is not "
          "comparable with a Controller arm's). It DOES share the R8 "
          "density exemption and the core biology, but NOT the "
          "imperfect grader: grade_efficiency is read only in "
          "placement.py and manual_events.py (off control/state), so a "
          "Global arm grades PERFECTLY at the cut line and its size "
          "splits are cleaner than any Controller arm's. "
          "Since 2026-08-21 it models the REAL 6N handover "
          "(global_assume_primed_6n=false): expect a genuine startup ramp "
          "over the first ~2 purge-hold weeks rather than a smooth week 1.",
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
    # BLURB CORRECTED 2026-08-14. It used to read "the whole-horizon grow-out
    # layout is placed by a CP-SAT optimal (0-swap) solver". Both halves were
    # wrong and the error actively misled a placement investigation into
    # treating this method as a foresight benchmark:
    #   * NOT whole-horizon. tools.run_global_forecast.run_cpsat calls
    #     global_placement_milp_poc.solve_cpsat_perweek, which builds and solves
    #     ONE model per week (`info["status"] == "per-week"`), seeded only by
    #     last week's occupancy. It is exactly as myopic as the controller; what
    #     differs is that its objective carries an explicit min-max BALANCE term
    #     (`100 * (zb + zf)`, the per-week hottest system biomass/feed
    #     fractions) that the controller has no equivalent of.
    #   * NOT 0-swap. Same-week swaps are a SOFT objective term (`+ 3 *
    #     sum(tr_swap)`) — the cheapest penalty in the whole objective, two
    #     orders of magnitude under the cap-slack and balance terms — so the
    #     solver buys swaps freely whenever they relieve a hot system, and the
    #     realised plan emits thousands of transfers. (The hard 0-swap
    #     formulation exists in this module as the full-horizon/rolling-window
    #     solvers, which the registered method does not use.)
    blurb="Same L1 cascade + facility share, but the grow-out layout is placed "
          "by a CP-SAT solve. Like the LP it plans ONE WEEK AT A TIME (seeded "
          "by last week's occupancy) — not the whole horizon — and its "
          "advantage is not foresight but an explicit min-max balance term in "
          "the objective, which holds the hottest system's biomass/feed down. "
          "Same-week tank swaps are only softly penalised, so it buys them "
          "freely: expect a transfer-heavy plan. "
          "Runs its OWN placement — NOT forecast/placement.py — so none of the "
          "controller's 2026-08-21 density work applies here: no OG1/2 "
          "density relief, no consolidation-to-free-tanks, no chronic-"
          "pressure anticipation, and NO handling mortality on its "
          "transfers (its moves are FREE, so its transfer count is not "
          "comparable with a Controller arm's). It DOES share the R8 "
          "density exemption and the core biology, but NOT the "
          "imperfect grader: grade_efficiency is read only in "
          "placement.py and manual_events.py (off control/state), so a "
          "Global arm grades PERFECTLY at the cut line and its size "
          "splits are cleaner than any Controller arm's. "
          "Since 2026-08-21 it models the REAL 6N handover "
          "(global_assume_primed_6n=false): expect a genuine startup ramp "
          "over the first ~2 purge-hold weeks rather than a smooth week 1.",
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
    blurb="The validated controller with the Global engine's L1 harvest "
          "envelope fed in as a per-week target band. "
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
          "Its L1 envelope comes from the same planner as the Global arms, so "
          "global_assume_primed_6n shapes this arm's first ~2 weeks too. "
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
# The GLOBAL family is deliberately NOT in the default roster (2026-08-27).
#
# A method that hard-fails a gate can never be promoted, so running it in a
# routine sweep spends hours to reach a foregone conclusion. Measured on the
# 2026-08-25 tuned tournament:
#
#     all 3 controller arms, TUNED (58 variants)   ~19 min
#     global-lp,   stock only                      10,948 s  (3.0 h)
#     global-milp, stock only                      16,750 s  (4.7 h)
#
# Global consumed ~90% of an 8h35m tournament to produce two arms that
# hard-fail sixn_one_way AND handling_budget. It also cannot tune -- its knob
# space is empty by design (see GLOBAL_KNOB_* below) -- so it contributes one
# fixed point, not a search.
#
# It remains fully available: pass --methods global-lp,global-milp (CLI) or
# select it on the board. Run it deliberately as a REFERENCE when you want the
# achievable bound -- it is the only engine that holds the facility biomass cap
# (96.7% peak vs the controller's 105.7%, 0 weeks over vs 11).
#
# READMISSION CONDITION, so this is not a permanent exile: put it back in the
# default roster when it passes every HARD gate. See
# docs/GLOBAL_TANK_LIFECYCLE_DESIGN.md for what that needs.
DEFAULT_ROSTER = ["controller", "controller-hybrid", "controller-lns"]

# Everything registered, for callers that want the full sweep including the
# gate-bound reference arms.
FULL_ROSTER = ["controller", "controller-hybrid", "controller-lns",
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
