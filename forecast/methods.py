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
    """
    key: str
    label: str
    family: str
    blurb: str
    engine: str
    overrides: dict = field(default_factory=dict)
    engine_kwargs: dict = field(default_factory=dict)


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


def register(method: Method) -> None:
    REGISTRY[method.key] = method


register(Method(
    key="controller",
    label="Controller — reactive greedy",
    family="Controller",
    engine="controller",
    blurb="Reactive week-by-week planner: greedy placement + multi-objective "
          "rebalancer. The validated production engine, and the greedy baseline.",
))
register(Method(
    key="controller-lns",
    label="Controller — greedy + LNS",
    family="Controller",
    engine="controller",
    overrides={"placement_method": "lns"},
    blurb="Controller with a large-neighborhood-search pass that relocates / "
          "swaps grow-out occupancy off the hottest systems (audit-gated).",
))
register(Method(
    key="global-lp",
    label="Global — lexicographic LP",
    family="Global",
    engine="global",
    engine_kwargs={"optimal": False},
    blurb="Precalculated cascade: tankless harvest (L1) -> per-batch facility "
          "share -> lexicographic LP placement (L3) -> continuity tank pick.",
))
register(Method(
    key="global-milp",
    label="Global — CP-SAT optimal",
    family="Global",
    engine="global",
    engine_kwargs={"optimal": True},
    blurb="Same L1 cascade + facility share, but the whole-horizon grow-out "
          "layout is placed by a CP-SAT optimal (0-swap) solver, not the LP.",
))
register(Method(
    key="controller-hybrid",
    label="Controller — hybrid (L1-guided harvest)",
    family="Controller",
    engine="controller",
    overrides={"hybrid_follow": "full"},
    blurb="The validated controller, with the Global engine's L1 harvest "
          "envelope fed in as a per-week harvest target band — L1's steady "
          "weekly quantity, executed by the controller's realized machinery. "
          "Aimed at the weeks the reactive controller paces below the "
          "contract floor.",
))


# Default comparison roster (every currently-wired method).
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
