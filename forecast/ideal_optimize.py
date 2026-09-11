"""The Ideal optimizer: the best stocking rhythm that stays within EVERY limit.

The operator's spec (2026-09-10): "we have system limits, facility limits,
batch count, batch input frequency, batch growth model, and density limits and
we need the system to work to those, both for the individual run and
optimizer for count and frequency to meet those limits and meet the target
variable". Strictness: HARD LIMITS, zero breaches.

So this is a plain grid, not a solver: every (cadence, batch size) cell is ONE
unchanged run of the real engine from an empty facility
(`ideal_engine.ideal_run`, the same call as the Ideal's reference sheet), with
the SAME method, promoted knobs and limits the caller passes. A cell is
within the limits iff `ideal_engine.plausible(gates)`: no gate FAILs, judged
with a control carrying the knobs and limits the run used
(`judging_control`). Among those cells the best is the highest objective:
revenue, harvested tonnage (HOG) or biomass gain. When none is within the
limits, the closest is the cell with the fewest breaches.

Two DISPLAY-ONLY reads ride along (operator, 2026-09-10); neither is ever an
accepted plan. `ignoring_limits` is the best cell whatever it breaks — the
cost of the limits. The stability check re-runs the top within-limits cells'
neighbours (the same cadence, +/- NEIGHBOUR_STEP fish per batch) in one extra
wave: the planner is mode-discontinuous, so a zero between two zeros can still
break a limit (49 d x 203k did, between 200k and 210k).

Pure orchestration: no Streamlit, no planning logic, and nothing is written to
config/ or scenario/ (each run lives in its own temp copy, see ideal_engine).
An engine exception is recorded on its cell (`Cell.error`), never swallowed
and never counted as feasible.
"""
from __future__ import annotations

import copy
import numbers
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from pathlib import Path
from pickle import PicklingError
from typing import Callable, Optional, Sequence

from forecast import ideal
from forecast import ideal_engine as ie

# objective key -> (Cell attribute, label)
OBJECTIVES = {"revenue": ("revenue", "Revenue"),
              "hog": ("hog_t", "Harvest tonnage (HOG)"),
              "gain": ("gain_t", "Biomass gain")}
MAX_CELLS = 60
MIN_SIZE = 1000                 # fish: no real batch is smaller
MIN_CADENCE = 7                 # days: one stocking a week at most
# Per-limit breach counts, in the order the page shows them.
BREACH_KEYS = ("density", "sys_biomass", "sys_feed", "moves", "floor",
               "zero", "over_cap")
# The Control keys a run's limits can change; the judging control carries
# each one the run used (the rest of an `overrides` dict, e.g. the scenario
# name or the 6N production date, is not a limit and is not judged on).
JUDGED_KEYS = ("max_biomass_kg", "max_harvest_per_week",
               "min_harvest_per_week", "min_harvest_weight_g",
               "max_feed_per_day_kg", "max_transfers_per_week")
# The ideal_engine.gates that judge a facility LIMIT. Every other gate that
# can FAIL ("Engine finished", "Conservation audits", "Input conservation")
# checks the run itself: a cell failing one is further from a plan than a
# cell that only breaks limits, and is named by gate, not by a count.
LIMIT_GATES = ("Harvest every week", "Harvest floor", "Biomass cap",
               "Tank density (R8)", "System limits", "Handling budget")
# Each breach count in plain words: (one, many).
BREACH_WORDS = {
    "density": ("tank-week over density", "tank-weeks over density"),
    "sys_biomass": ("system-week over biomass", "system-weeks over biomass"),
    "sys_feed": ("system-week over feed", "system-weeks over feed"),
    "moves": ("week over the move budget", "weeks over the move budget"),
    "floor": ("week under the floor", "weeks under the floor"),
    "zero": ("week with no harvest", "weeks with no harvest"),
    "over_cap": ("week over the biomass cap", "weeks over the biomass cap")}
# The stability check: the top MAX_STABILITY_CANDIDATES within-limits cells,
# each re-judged at its batch size +/- NEIGHBOUR_STEP fish (same cadence) — at
# most 2 x 3 = 6 extra runs, in one wave after the grid.
NEIGHBOUR_STEP = 5_000
MAX_STABILITY_CANDIDATES = 3


@dataclass(frozen=True)
class Cell:
    """One rhythm, run once. Totals are for the one steady year read."""
    cadence_days: int
    batch_size: int
    fish_per_week: float            # batch_size x 7 / cadence_days
    year: Optional[int] = None
    revenue: float = 0.0
    hog_t: float = 0.0
    gain_t: float = 0.0
    avg_gross_kg: float = 0.0
    peak_pct_of_cap: float = 0.0
    breaches: dict = field(default_factory=dict)   # BREACH_KEYS -> count
    total: int = 0
    gates: tuple = ()
    within_limits: bool = False
    error: Optional[str] = None     # "Type: message" when the engine raised
    elapsed_s: float = 0.0

    @property
    def label(self) -> str:
        return f"{self.cadence_days} d × {self.batch_size:,}"


@dataclass(frozen=True)
class Result:
    cells: tuple                    # grid order: cadence-major, then size
    objective: str
    best: Optional[Cell]            # highest objective within every limit
    closest: Optional[Cell]         # only when best is None
    note: Optional[str] = None      # the pool could not start / died
    # DISPLAY ONLY — never an accepted plan:
    unconstrained: Optional[Cell] = None   # highest objective, limits ignored
    stability: tuple = ()           # (candidate, neighbour Cells, stable) each
    best_stable: Optional[Cell] = None     # first stable candidate, else None


def judging_control(control, method_overrides=None, overrides=None):
    """The control a run is JUDGED with: a copy of `control` carrying the
    promoted knobs the run used, then the run's own limits (JUDGED_KEYS in
    `overrides`) on top — the order the run's config is layered in
    (ideal_engine.prepare). `control` itself is never changed."""
    c = copy.deepcopy(control)
    for k, v in dict(method_overrides or {}).items():
        if hasattr(c, k):
            setattr(c, k, v)
    ov = dict(overrides or {})
    for k in JUDGED_KEYS:
        if k in ov and hasattr(c, k):
            setattr(c, k, ov[k])
    return c


def objective_value(cell: Cell, objective: str) -> float:
    if objective not in OBJECTIVES:
        raise ValueError(f"objective must be one of {sorted(OBJECTIVES)}, "
                         f"got {objective!r}")
    return float(getattr(cell, OBJECTIVES[objective][0]))


def _whole(v, what: str, least: int) -> int:
    if (isinstance(v, bool) or not isinstance(v, numbers.Integral)
            or v < least):
        raise ValueError(f"{what} must be whole numbers of at least {least}, "
                         f"got {v!r}")
    return int(v)


def check_grid(cadences: Sequence[int], sizes: Sequence[int]) -> list:
    """The (cadence, size) cells to run, cadence-major, duplicates removed.
    Refuses an empty grid, a cadence under MIN_CADENCE days, a batch under
    MIN_SIZE fish and more than MAX_CELLS cells — before anything runs."""
    cads = sorted({_whole(c, "cadences (days)", MIN_CADENCE)
                   for c in (cadences or ())})
    szs = sorted({_whole(s, "batch sizes (fish)", MIN_SIZE)
                  for s in (sizes or ())})
    if not cads or not szs:
        raise ValueError("the grid is empty: give at least one cadence and "
                         "one batch size")
    n = len(cads) * len(szs)
    if n > MAX_CELLS:
        raise ValueError(f"{len(cads)} cadences x {len(szs)} sizes = {n} "
                         f"cells; at most {MAX_CELLS} (each is one ~20 s "
                         f"engine run)")
    return [(c, s) for c in cads for s in szs]


def run_cell(rhythm, cap_kg: float, project_dir, *, template=None,
             overrides: Optional[dict] = None,
             method: str = ie.DEFAULT_METHOD,
             method_overrides: Optional[dict] = None,
             density_overrides: Optional[dict] = None,
             system_overrides: Optional[dict] = None,
             control=None,
             horizon_weeks: int = ideal.HORIZON_WEEKS) -> Cell:
    """One (cadence_days, batch_size) through `ideal_engine.ideal_run`, read
    and judged. Top level and picklable, so a process pool can run it.

    `overrides` are the run's facility limits (incl. min_harvest_weight_g and
    max_transfers_per_week); `control` is the live Control to judge from
    (loaded from `project_dir` when None). An exception from the engine is
    recorded on the cell as `error` and the cell is never within the limits.
    """
    cad, size = int(rhythm[0]), int(rhythm[1])
    fpw = size * 7.0 / cad
    t0 = time.perf_counter()
    try:
        if control is None:
            from forecast.config_io import load_config
            control = load_config(str(Path(project_dir) / "config"))[0]
        run = ie.ideal_run(cad, size, cap_kg, project_dir, template=template,
                           horizon_weeks=horizon_weeks,
                           overrides=dict(overrides or {}), method=method,
                           method_overrides=dict(method_overrides or {}),
                           density_overrides=density_overrides,
                           system_overrides=system_overrides)
        (year, y), = run.years.items()
        g = tuple(ie.gates(run, year, judging_control(
            control, method_overrides, overrides)))
    except Exception as e:  # noqa: BLE001 — recorded on the cell, not hidden
        return Cell(cadence_days=cad, batch_size=size, fish_per_week=fpw,
                    error=f"{type(e).__name__}: {e}",
                    elapsed_s=time.perf_counter() - t0)
    b = dict(density=y.r8_over_tank_weeks, sys_biomass=y.sys_bio_over_weeks,
             sys_feed=y.sys_feed_over_weeks, moves=y.weeks_over_move_budget,
             floor=y.under_floor_weeks, zero=y.zero_weeks,
             over_cap=y.over_cap_weeks)
    return Cell(cadence_days=cad, batch_size=size, fish_per_week=fpw,
                year=year, revenue=y.revenue, hog_t=y.hog_t, gain_t=y.gain_t,
                avg_gross_kg=y.avg_gross_kg, peak_pct_of_cap=y.peak_pct_of_cap,
                breaches=b, total=int(sum(b.values())), gates=g,
                within_limits=ie.plausible(g),
                elapsed_s=time.perf_counter() - t0)


def other_failed_checks(cell: Cell) -> tuple:
    """The names of the gates a cell FAILs that are NOT limits (LIMIT_GATES):
    'Engine finished', 'Conservation audits', 'Input conservation'."""
    return tuple(g.name for g in cell.gates
                 if g.status == "FAIL" and g.name not in LIMIT_GATES)


def breach_words(cell: Cell) -> list:
    """Each nonzero breach count in plain words, in BREACH_KEYS order:
    ['76 tank-weeks over density', '1 system-week over biomass']. A cell the
    engine failed on has no counts: []."""
    if cell.error is not None:
        return []
    out = []
    for k in BREACH_KEYS:
        n = int(cell.breaches[k])
        if n:
            out.append(f"{n:,} {BREACH_WORDS[k][0 if n == 1 else 1]}")
    return out


def _rank(c: Cell, objective: str) -> tuple:
    """The objective, then the smaller batch, then the longer cadence: a
    unique final sort term, so no pick depends on grid or finishing order."""
    return (round(objective_value(c, objective), 6), -c.batch_size,
            c.cadence_days)


def best(cells: Sequence[Cell], objective: str) -> Optional[Cell]:
    """Highest objective among cells within every limit (no error), or None.
    Ties break toward the smaller batch, then the longer cadence: a unique
    final sort term, so the pick never depends on grid or finishing order."""
    ok = [c for c in cells if c.within_limits and c.error is None]
    if not ok:
        return None
    return max(ok, key=lambda c: _rank(c, objective))


def ignoring_limits(cells: Sequence[Cell], objective: str) -> Optional[Cell]:
    """DISPLAY ONLY — the cost of the limits: the highest objective among the
    cells that RAN (no error), whatever they break; same tie-break as `best`.
    Never an accepted plan. None when no cell ran."""
    ran = [c for c in cells if c.error is None]
    if not ran:
        return None
    return max(ran, key=lambda c: _rank(c, objective))


def closest(cells: Sequence[Cell], objective: str) -> Optional[Cell]:
    """The ran cell with the fewest breaches, then the highest objective
    (same tie-break as `best`). A cell that fails a check that is not a limit
    (`other_failed_checks`) ranks after every cell that only breaks limits.
    Errored cells have no count: never closest."""
    ran = [c for c in cells if c.error is None]
    if not ran:
        return None
    return min(ran, key=lambda c: (bool(other_failed_checks(c)), c.total,
                                   -round(objective_value(c, objective), 6),
                                   c.batch_size, -c.cadence_days))


def table_order(cells: Sequence[Cell], objective: str) -> list:
    """Within-limits cells first by the objective, then the cells that only
    break limits by total breaches (then the objective), then the cells that
    fail a non-limit check, the same way; errored cells last. Deterministic."""
    def key(c):
        v = round(objective_value(c, objective), 6)
        if c.error is not None:
            return (3, 0, 0.0, c.cadence_days, c.batch_size)
        if c.within_limits:
            return (0, 0, -v, c.batch_size, -c.cadence_days)
        if other_failed_checks(c):
            return (2, c.total, -v, c.batch_size, -c.cadence_days)
        return (1, c.total, -v, c.batch_size, -c.cadence_days)
    return sorted(cells, key=key)


def neighbour_rhythms(cell: Cell) -> list:
    """The rhythms a candidate is re-judged at: the same cadence with the
    batch NEIGHBOUR_STEP fish smaller and larger, dropping one below
    MIN_SIZE."""
    return [(cell.cadence_days, s)
            for s in (cell.batch_size - NEIGHBOUR_STEP,
                      cell.batch_size + NEIGHBOUR_STEP) if s >= MIN_SIZE]


def stability_candidates(cells: Sequence[Cell], objective: str) -> list:
    """The within-limits, error-free cells best-first (the order of `best`:
    the first is `best`), at most MAX_STABILITY_CANDIDATES."""
    ok = [c for c in cells if c.within_limits and c.error is None]
    return sorted(ok, key=lambda c: _rank(c, objective),
                  reverse=True)[:MAX_STABILITY_CANDIDATES]


def judge_stability(candidates: Sequence[Cell], ran: dict) -> tuple:
    """The stability decision, on neighbours already run (no engine here).

    `ran` maps (cadence_days, batch_size) -> the Cell that ran that rhythm;
    every neighbour of every candidate must be in it (a missing one raises
    KeyError — detect, don't coerce). A candidate is stable iff every
    neighbour ran without error and is within the limits.
    -> (stability, best_stable): stability is a tuple of (candidate, tuple of
    neighbour Cells, stable) in candidate order; best_stable is the first
    stable candidate, else None."""
    out = []
    for c in candidates:
        ns = tuple(ran[r] for r in neighbour_rhythms(c))
        out.append((c, ns, all(n.error is None and n.within_limits
                               for n in ns)))
    return tuple(out), next((c for c, _ns, ok in out if ok), None)


def _run_wave(jobs: list, cap_kg: float, project_dir, kw: dict, workers: int,
              record: Callable, what: str) -> Optional[str]:
    """Run each (cadence, size) in `jobs`; `record(i, cell)` as each finishes.

    A process pool of `workers` when that is 2+ and there is more than one
    job. ONLY a pool that cannot start or dies falls back to one-at-a-time
    (the returned note says so). Any other exception — including a Streamlit
    rerun/stop (a BaseException) raised from the progress callback inside
    `record` — cancels the jobs not yet started and does NOT wait for the
    ones running, then re-raises: the page must never block for the rest of
    the grid."""
    done: set = set()
    note = None

    def _rec(i, cell):
        done.add(i)
        record(i, cell)

    if workers >= 2 and len(jobs) > 1:
        try:
            ex = ProcessPoolExecutor(max_workers=min(int(workers), len(jobs)))
        except OSError as e:
            note = (f"Parallel run unavailable ({type(e).__name__}: {e}) — "
                    f"ran {what} one at a time instead.")
        else:
            try:
                futs = {ex.submit(run_cell, j, cap_kg, str(project_dir), **kw): i
                        for i, j in enumerate(jobs)}
                for f in as_completed(futs):
                    _rec(futs[f], f.result())
            except (BrokenProcessPool, PicklingError) as e:
                note = (f"Parallel run unavailable ({type(e).__name__}: "
                        f"{e}) — ran the remaining {len(jobs) - len(done)} "
                        f"of {what} one at a time instead.")
                ex.shutdown(wait=True, cancel_futures=True)
            except BaseException:
                ex.shutdown(wait=False, cancel_futures=True)
                raise
            else:
                ex.shutdown(wait=True)
    for i, j in enumerate(jobs):
        if i not in done:
            _rec(i, run_cell(j, cap_kg, str(project_dir), **kw))
    return note


def optimize(cadences: Sequence[int], sizes: Sequence[int], cap_kg: float,
             project_dir, *, objective: str = "revenue", workers: int = 1,
             progress: Optional[Callable] = None, template=None,
             overrides: Optional[dict] = None,
             method: str = ie.DEFAULT_METHOD,
             method_overrides: Optional[dict] = None,
             density_overrides: Optional[dict] = None,
             system_overrides: Optional[dict] = None,
             control=None,
             horizon_weeks: int = ideal.HORIZON_WEEKS) -> Result:
    """Run every (cadence, size) cell and pick the best within every limit.

    Bad grids (empty, a cadence < MIN_CADENCE, a size < MIN_SIZE, more than
    MAX_CELLS cells) and an unknown objective are refused with ValueError
    before anything runs. Cells run in a process pool of `workers` when it
    is 2+ and there is more than one cell; ONLY a pool that cannot start or
    that dies falls back to one-at-a-time, and `Result.note` says so (an
    engine error is recorded on its cell, not re-run). Any other exception,
    a Streamlit rerun from `progress` included, cancels what has not started
    and re-raises without waiting (`_run_wave`). `progress(done, total,
    cell)` is called as each cell finishes. Cells come back in grid order
    whatever order they finish in.

    Display only, never an accepted plan: `Result.unconstrained` is
    `ignoring_limits(cells)`. When there is a best, the stability check runs
    the neighbours of `stability_candidates` that are not grid cells (a grid
    cell with exactly that rhythm is reused, not re-run) in ONE extra wave of
    at most 2 x MAX_STABILITY_CANDIDATES runs, through the same pool path
    with the same arguments; `progress` keeps counting (its total grows by
    the wave). `Result.stability` / `best_stable` come from `judge_stability`.
    """
    if objective not in OBJECTIVES:
        raise ValueError(f"objective must be one of {sorted(OBJECTIVES)}, "
                         f"got {objective!r}")
    jobs = check_grid(cadences, sizes)
    if (isinstance(workers, bool) or not isinstance(workers, numbers.Integral)
            or workers < 1):
        raise ValueError(f"workers must be a positive whole number, got "
                         f"{workers!r}")
    if control is None:
        from forecast.config_io import load_config
        control = load_config(str(Path(project_dir) / "config"))[0]
    kw = dict(template=template, overrides=dict(overrides or {}),
              method=method, method_overrides=dict(method_overrides or {}),
              density_overrides=density_overrides,
              system_overrides=system_overrides, control=control,
              horizon_weeks=horizon_weeks)
    count = {"done": 0, "total": len(jobs)}

    def _tick(cell):
        count["done"] += 1
        if progress is not None:
            progress(count["done"], count["total"], cell)

    done: dict = {}

    def _record(i, cell):
        done[i] = cell
        _tick(cell)

    note = _run_wave(jobs, cap_kg, project_dir, kw, workers, _record,
                     "the grid")
    cells = tuple(done[i] for i in range(len(jobs)))
    b = best(cells, objective)

    stability, best_stable = (), None
    if b is not None:
        cands = stability_candidates(cells, objective)
        ran = {(c.cadence_days, c.batch_size): c for c in cells}
        extra = []                          # candidate order, then -/+ step
        for c in cands:
            for r in neighbour_rhythms(c):
                if r not in ran and r not in extra:
                    extra.append(r)
        if extra:
            count["total"] += len(extra)
            more: dict = {}

            def _record_more(i, cell):
                more[i] = cell
                _tick(cell)

            note2 = _run_wave(extra, cap_kg, project_dir, kw, workers,
                              _record_more, "the stability runs")
            ran.update({r: more[i] for i, r in enumerate(extra)})
            note = " ".join(x for x in (note, note2) if x) or None
        stability, best_stable = judge_stability(cands, ran)
    return Result(cells=cells, objective=objective, best=b,
                  closest=None if b is not None else closest(cells, objective),
                  note=note, unconstrained=ignoring_limits(cells, objective),
                  stability=stability, best_stable=best_stable)
