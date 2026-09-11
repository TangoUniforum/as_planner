"""The transition optimizer: the best FUTURE batch size and biomass cap from
today's fish, within the limits.

Steps 1-2's optimizer (forecast.ideal_optimize) answers the empty-facility
ideal. From today's fish the only levers are the size of future stockings
(forecast.transition.propose keeps their dates and cadence) and the biomass
cap. So every (size, cap) cell here is ONE unchanged run of the real engine on
the uploaded ProductionReport (`ideal_engine.run_schedule`, the same call step
3's proposal arm makes), and TODAY'S PLAN runs once beside them, at the current
limits, exactly as step 3's today arm runs.

THE RULE (operator-approved, 2026-09-11). Today's plan already breaks limits
in the near years from fish already stocked (8/31 PR, 2027-29: 195 tank-weeks
over density, 3 / 137 system-weeks over biomass / feed, 2 weeks over the move
budget), which re-sizing future stockings cannot change — so a flat zero-breach
rule would reject every proposal. Instead (`verdict`):

  * EFFECT YEAR E (`effect_year`): the first calendar year that starts on or
    after the first re-sized batch's TranOG date (transition.summarize's
    first_changed_tran_og_date): 2027-09-23 -> 2028, 2028-01-01 -> 2028.
    With no re-sized batch (a cap-only change), or when no re-sized batch has
    a TranOG date: the first run year with NO dated per-week biomass-cap row
    in scenario/limits.yaml, i.e. the first year the what-if cap applies to
    every week without a dated row winning (`undated_cap_year`; 2027 on the
    live limits, whose dated rows are all 2026). When that cannot be
    determined (no limits given, or every run year has a dated row): the
    second calendar year of the run.
  * EARLY YEARS (years both runs cover, before E): the proposal must be NO
    WORSE than today's plan, year by year and limit by limit (the seven
    ideal_optimize.BREACH_KEYS counts), and must FAIL no non-limit check
    (engine finished, conservation audits, input conservation).
  * JUDGED YEARS (E on, a partial last year included): ZERO breaches and no
    FAIL gate.
  A proposal is within the limits only if both hold.

The run's years are the engine's ISO label-years ("2027-W01"...), so a
calendar-year boundary can sit up to three days off the label-year one; E is
still the calendar rule the operator approved.

Everything that is not transition-specific is ideal_optimize's, reused, not
copied: `judging_control` (each run judged on the knobs and limits it ran
with, the cell's own cap included), the objectives, the ranking and its
tie-break (`best`, `closest`, `ignoring_limits`, `table_order` — TrCell
carries the attributes they read, with a constant cadence), LIMIT_GATES /
`other_failed_checks`, BREACH_KEYS / BREACH_WORDS, the stability decision
(`stability_candidates`, `neighbour_rhythms`, `judge_stability`: size
+/- NEIGHBOUR_STEP at the same cap), and the process-pool wave
(ideal_optimize._run_wave: pool start / death fallback and its note,
cancel-without-wait on any other exception, one record per job). Each job
here names its own runner — `run_today` or `run_cell`, both top level so a
process pool can pickle them by their qualified name — because the first wave
runs today's plan beside the grid cells.

Pure orchestration: no Streamlit, no planning logic, and nothing is written to
config/ or scenario/ (each run lives in its own temp copy, see ideal_engine).
An engine exception is recorded on its cell (`TrCell.error`), never swallowed
and never counted as within the limits.
"""
from __future__ import annotations

import datetime as dt
import numbers
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional, Sequence

from forecast import ideal_engine as ie
from forecast import ideal_optimize as io
from forecast import transition as tr

# The transition check's horizon: four years on the real PR, so a re-size's
# 2028-29 effect shows (app._TR_HORIZON_WEEKS).
TR_HORIZON_WEEKS = 208
# Each cell is one ~80-100 s engine run on the real PR.
TR_MAX_CELLS = 60
# The stability check re-runs the top candidates' neighbours: at most
# 2 x TR_MAX_STABILITY_CANDIDATES extra runs, in one wave after the grid.
TR_MAX_STABILITY_CANDIDATES = 5
# A transition keeps every batch's dates, so it has no cadence lever. The
# ranking reused from ideal_optimize reads a cadence; one constant for every
# cell means it never decides a tie.
TR_CADENCE = 0
# Each breach count (ideal_optimize.BREACH_KEYS) and the YearRead count it is
# (the same pairing as ideal_optimize.run_cell's per-cell breaches).
YEAR_COUNTS = (("density", "r8_over_tank_weeks"),
               ("sys_biomass", "sys_bio_over_weeks"),
               ("sys_feed", "sys_feed_over_weeks"),
               ("moves", "weeks_over_move_budget"),
               ("floor", "under_floor_weeks"),
               ("zero", "zero_weeks"),
               ("over_cap", "over_cap_weeks"))
if tuple(k for k, _a in YEAR_COUNTS) != tuple(io.BREACH_KEYS):
    raise ImportError("transition_optimize.YEAR_COUNTS no longer matches "
                      "ideal_optimize.BREACH_KEYS")


def _words(key: str, n) -> str:
    """'12 tank-weeks over density' — ideal_optimize's plain words."""
    return f"{n:,} {io.BREACH_WORDS[key][0 if n == 1 else 1]}"


def years_text(years) -> str:
    """'2026–2030', '2028' or 'no year' — a span of run years in words (the
    page's too)."""
    ys = sorted(years)
    if not ys:
        return "no year"
    return str(ys[0]) if len(ys) == 1 else f"{ys[0]}–{ys[-1]}"


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #
def undated_cap_year(facility_limits, years) -> Optional[int]:
    """The first of `years` with NO dated per-week biomass-cap row in
    `facility_limits` (scenario_io.load_limits(...)[0]): the first year a
    what-if cap applies to every week without a dated row winning (the
    engine's own precedence, caps.resolve_facility_cap). None when every
    year has one."""
    from forecast.caps import METRIC_BIOMASS
    dated = {int(str(w)[:4]) for (w, m) in facility_limits.overrides
             if m == METRIC_BIOMASS}
    return next((y for y in sorted({int(y) for y in years})
                 if y not in dated), None)


def effect_year(summary: dict, years: Sequence[int], *,
                undated_cap_year: Optional[int] = None,
                cap_changed: bool = True) -> tuple:
    """The effect year E of a proposal, and why, in plain words.

    `summary` is transition.summarize's dict (n_changed,
    first_changed_tran_og_date); `years` the run's years. E is the first
    calendar year that starts on or after the first re-sized batch's TranOG
    date — a date inside a year gives the next year, a 1 January the same
    year. With no re-sized batch (a cap-only change), or none with a TranOG
    date, E is `undated_cap_year` (the first run year a what-if cap applies
    to every week without a dated per-week row winning, see
    `undated_cap_year`), and when the caller could not determine it (None),
    the second calendar year of the run (the first run year + 1).

    The operator's rule defines the no-re-size case for a CAP change. A
    proposal that re-sizes nothing and leaves the cap alone (only other
    limits, or the tank/system table, change) gets the same E by the same
    rule; `cap_changed=False` only makes the reason say so instead of
    naming a what-if cap that was not set. -> (E, why)."""
    ys = sorted({int(y) for y in years})
    n = int(summary.get("n_changed") or 0)
    d = summary.get("first_changed_tran_og_date") if n else None
    if d is not None:
        d = tr._as_date(d, "first_changed_tran_og_date")
        e = d.year if (d.month, d.day) == (1, 1) else d.year + 1
        return e, (f"the first re-sized batch reaches seawater (TranOG) on "
                   f"{d.isoformat()}, and {e} is the first calendar year that "
                   f"starts on or after that date")
    lead = ("none of the re-sized batches has a TranOG date" if n
            else "no future batch is re-sized")
    if not cap_changed:
        lead += (" and the biomass cap is not changed, so the cap-only rule "
                 "places it")
    if undated_cap_year is not None:
        e = int(undated_cap_year)
        return e, (f"{lead}: the effect year is the first year with no dated "
                   f"per-week biomass-cap row, where a what-if cap would apply "
                   f"to every week ({e})"
                   if not cap_changed else
                   f"{lead}, so the effect year is the first year the what-if "
                   f"cap applies to every week with no dated per-week cap row "
                   f"winning ({e})")
    if not ys:
        raise ValueError("no run years: the effect year cannot be placed")
    e = ys[0] + 1
    return e, (f"{lead}: the first year with no dated per-week biomass-cap "
               f"row could not be determined, so the effect year is the "
               f"second calendar year of the run ({e})"
               if not cap_changed else
               f"{lead}, and the first year a what-if cap applies without a "
               f"dated per-week row could not be determined, so the effect "
               f"year is the second calendar year of the run ({e})")


@dataclass(frozen=True)
class Verdict:
    """The two-window rule applied to one proposal (see the module
    docstring). Counts are per constraint (BREACH_KEYS) and year."""
    effect_year: int
    years: tuple                    # the years both runs cover
    early_years: tuple              # < effect_year: no worse than today
    judged_years: tuple             # >= effect_year: zero breaches
    counts: dict                    # key -> {year: (today, proposal)}
    worse: dict                     # key -> {early year: (today, proposal)}
    breached: dict                  # key -> {judged year: proposal}
    failed_checks: dict             # non-limit gate name -> (years,)
    early_failures: tuple           # 'worse than today: 12 vs 9 ... in 2027'
    judged_failures: tuple          # '3 tank-weeks over density in 2029'
    check_failures: tuple           # 'fails Conservation audits in 2027, 2028'
    year_failures: dict             # year -> the failures that year
    within_limits: bool

    @property
    def failures(self) -> tuple:
        """Every failure, early first, then judged, then the checks."""
        return self.early_failures + self.judged_failures + self.check_failures

    def excess(self) -> dict:
        """key -> how far the proposal is worse than today in the early
        years, summed (0 when no worse)."""
        return {k: sum(p - t for t, p in self.worse[k].values())
                for k in io.BREACH_KEYS}

    def judged(self) -> dict:
        """key -> the proposal's breaches in the judged years, summed."""
        return {k: sum(self.breached[k].values()) for k in io.BREACH_KEYS}


def verdict(today_years: dict, prop_years: dict, prop_gates_by_year: dict,
            effect_year: int) -> Verdict:
    """The two-window rule, pure (module docstring): today's plan
    (`today_years`) and the proposal (`prop_years`), each {year: YearRead},
    over the years BOTH cover; `prop_gates_by_year` is {year: the
    ideal_engine.gates the proposal was judged with} for every such year.

    Early years (< effect_year): each count no worse than today's that year.
    Judged years: each count zero. Every compared year: no gate FAILs that is
    not a limit (ideal_optimize.LIMIT_GATES are judged by their counts). A
    count is read with a plain getattr and a year's gates by plain indexing:
    a missing one raises — detect, don't coerce. With no shared year there
    is nothing to judge and the proposal is not within the limits."""
    if isinstance(effect_year, bool) or not isinstance(effect_year,
                                                       numbers.Integral):
        raise ValueError(f"effect_year must be a whole year, got "
                         f"{effect_year!r}")
    e = int(effect_year)
    years = tuple(sorted(set(today_years) & set(prop_years)))
    early = tuple(y for y in years if y < e)
    judged = tuple(y for y in years if y >= e)
    counts = {k: {} for k in io.BREACH_KEYS}
    worse = {k: {} for k in io.BREACH_KEYS}
    breached = {k: {} for k in io.BREACH_KEYS}
    per_year = {y: [] for y in years}
    early_f, judged_f = [], []
    for k, attr in YEAR_COUNTS:
        for y in years:
            t = getattr(today_years[y], attr)
            p = getattr(prop_years[y], attr)
            counts[k][y] = (t, p)
            if y < e:
                if p > t:
                    worse[k][y] = (t, p)
                    msg = (f"worse than today: {p:,} vs {t:,} "
                           f"{io.BREACH_WORDS[k][0 if p == 1 else 1]} in {y}")
                    early_f.append(msg)
                    per_year[y].append(msg)
            elif p > 0:
                breached[k][y] = p
                msg = f"{_words(k, p)} in {y}"
                judged_f.append(msg)
                per_year[y].append(msg)
    failed: dict = {}
    for y in years:
        for g in prop_gates_by_year[y]:
            if g.status == "FAIL" and g.name not in io.LIMIT_GATES:
                failed.setdefault(g.name, []).append(y)
                per_year[y].append(f"fails {g.name}")
    checks = tuple(f"fails {name} in {', '.join(map(str, ys))}"
                   for name, ys in failed.items())
    if not years:
        checks = ("the two runs share no year: nothing to compare",)
    fails = tuple(early_f) + tuple(judged_f) + checks
    return Verdict(effect_year=e, years=years, early_years=early,
                   judged_years=judged, counts=counts, worse=worse,
                   breached=breached,
                   failed_checks={n: tuple(ys) for n, ys in failed.items()},
                   early_failures=tuple(early_f),
                   judged_failures=tuple(judged_f), check_failures=checks,
                   year_failures={y: tuple(v) for y, v in per_year.items()},
                   within_limits=bool(years) and not fails)


# --------------------------------------------------------------------------- #
# Cells
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TrCell:
    """One future batch size at one biomass cap on the real PR, run once (or
    today's plan, `today=True`). `judge_cell` fills the totals and the
    verdict against today's plan."""
    batch_size: int                 # fish to OG per future batch (0 = today)
    cap_kg: float                   # the biomass cap it ran AT
    today: bool = False
    reads: dict = field(default_factory=dict)        # {year: YearRead}
    gates_by_year: dict = field(default_factory=dict)   # {year: (Gate,)}
    n_changed: int = 0              # future batches re-sized
    first_tran_og: Optional[dt.date] = None   # first re-sized TranOG date
    smolt_removed: int = 0          # fish to OG removed (negative = added)
    manual_events_file: Optional[str] = None
    error: Optional[str] = None     # "Type: message" when the engine raised
    elapsed_s: float = 0.0
    # --- filled by judge_cell / judge_today ---
    years: tuple = ()               # the years compared with today's plan
    revenue: float = 0.0            # totals over `years`
    hog_t: float = 0.0
    gain_t: float = 0.0
    effect: Optional[int] = None    # the effect year E
    effect_why: str = ""
    verdict: Optional[Verdict] = None
    breaches: dict = field(default_factory=dict)   # key -> excess + judged
    total: int = 0
    gates: tuple = ()               # the non-limit FAIL gates, one per name
    within_limits: bool = False

    @property
    def cadence_days(self) -> int:
        """Constant (TR_CADENCE): the transition keeps every date, so the
        ranking reused from ideal_optimize never decides on it."""
        return TR_CADENCE

    @property
    def cap_t(self) -> float:
        return self.cap_kg / 1000.0

    @property
    def label(self) -> str:
        """'320,000 @ 3,600 t' (fish per future batch @ cap)."""
        if self.today:
            return f"today's plan @ {self.cap_t:,.0f} t"
        return f"{self.batch_size:,} @ {self.cap_t:,.0f} t"

    @property
    def key(self) -> tuple:
        """(TR_CADENCE, batch_size, cap_kg): ideal_optimize.neighbour_rhythms'
        shape, so its neighbours and judge_stability key on it."""
        return (TR_CADENCE, self.batch_size, float(self.cap_kg))

    def per_year(self) -> list:
        """[(year, revenue, hog_t, gain_t)] over `years`."""
        return [(y, self.reads[y].revenue, self.reads[y].hog_t,
                 self.reads[y].gain_t) for y in self.years]


@dataclass(frozen=True)
class TrResult:
    today: TrCell                   # today's plan, at the current limits
    cells: tuple                    # grid order: size, then cap (high first)
    objective: str
    best: Optional[TrCell]          # highest objective within the limits
    closest: Optional[TrCell]       # only when best is None
    note: Optional[str] = None      # the pool could not start / died
    # DISPLAY ONLY — never an accepted plan:
    unconstrained: Optional[TrCell] = None
    stability: tuple = ()           # (candidate, neighbour TrCells, stable)
    best_stable: Optional[TrCell] = None
    ceiling_kg: float = 0.0
    undated_cap_year: Optional[int] = None


def _arm(batches, project_dir, run_kw: dict, control, method_overrides,
         judge_ov: dict):
    """One engine run on the PR, read and judged per year on the knobs and
    limits it ran with. -> (reads, gates_by_year, manual_events_file)."""
    run = ie.run_schedule(batches, project_dir, **run_kw)
    jc = io.judging_control(control, method_overrides, judge_ov)
    gates = {y: tuple(ie.gates(run, y, jc)) for y in sorted(run.years)}
    return (dict(run.years), gates,
            (run.audits or {}).get("manual_events_file"))


def _load_control(project_dir):
    from forecast.config_io import load_config
    return load_config(str(Path(project_dir) / "config"))[0]


def run_today(live, project_dir, *, pr_path,
              method: str = ie.DEFAULT_METHOD,
              method_overrides: Optional[dict] = None, control=None,
              horizon_weeks: int = TR_HORIZON_WEEKS) -> TrCell:
    """Today's plan: the live batches at the CURRENT limits, exactly as step
    3's today arm (app._ideal_transition_runs) runs it — no overrides, no
    tank/system what-ifs. Judged on the promoted knobs and the Control
    limits. Top level and picklable. An engine exception is recorded as
    `error`."""
    t0 = time.perf_counter()
    cap = 0.0
    try:
        if control is None:
            control = _load_control(project_dir)
        cap = float(io.judging_control(control, method_overrides,
                                       {}).max_biomass_kg)
        reads, gates, ev = _arm(
            live, project_dir,
            dict(pr_path=str(pr_path), horizon_weeks=int(horizon_weeks),
                 include_manual_events=True, method=method,
                 method_overrides=dict(method_overrides or {})),
            control, method_overrides, {})
    except Exception as e:  # noqa: BLE001 — recorded, not hidden
        return TrCell(batch_size=0, cap_kg=cap, today=True,
                      error=f"{type(e).__name__}: {e}",
                      elapsed_s=time.perf_counter() - t0)
    return TrCell(batch_size=0, cap_kg=cap, today=True, reads=reads,
                  gates_by_year=gates, manual_events_file=ev,
                  elapsed_s=time.perf_counter() - t0)


def run_cell(cell, project_dir, *, live, forecast_start, cutoff, pr_path,
             overrides: Optional[dict] = None,
             method: str = ie.DEFAULT_METHOD,
             method_overrides: Optional[dict] = None,
             density_overrides: Optional[dict] = None,
             system_overrides: Optional[dict] = None,
             control=None,
             horizon_weeks: int = TR_HORIZON_WEEKS) -> TrCell:
    """One (size, cap_kg): transition.propose(live, forecast_start, cutoff,
    [size]), then the engine on the PR with the SAME call step 3's proposal
    arm makes (app._ideal_transition_runs: pr_path, the 208-week horizon,
    manual events, the method and promoted knobs, the what-if limits in
    `overrides` plus max_biomass_kg = this cell's cap, and the tank/system
    what-ifs only when given), judged per year with
    ideal_optimize.judging_control at the cell's cap. Top level and
    picklable. An exception is recorded on the cell as `error`; the cell is
    then never within the limits."""
    size, cap = int(cell[0]), float(cell[1])
    t0 = time.perf_counter()
    try:
        if control is None:
            control = _load_control(project_dir)
        proposed, changes = tr.propose(live, forecast_start, cutoff, [size])
        summ = tr.summarize(proposed, changes)
        ov = dict(overrides or {}, max_biomass_kg=cap)
        kw = dict(pr_path=str(pr_path), horizon_weeks=int(horizon_weeks),
                  include_manual_events=True, method=method,
                  method_overrides=dict(method_overrides or {}),
                  overrides=ov)
        # Only when changed, exactly as the proposal arm sends them.
        if density_overrides:
            kw["density_overrides"] = dict(density_overrides)
        if system_overrides:
            kw["system_overrides"] = {s: dict(v)
                                      for s, v in system_overrides.items()}
        reads, gates, ev = _arm(proposed, project_dir, kw, control,
                                method_overrides, ov)
    except Exception as e:  # noqa: BLE001 — recorded on the cell, not hidden
        return TrCell(batch_size=size, cap_kg=cap,
                      error=f"{type(e).__name__}: {e}",
                      elapsed_s=time.perf_counter() - t0)
    first = summ["first_changed_tran_og_date"]
    return TrCell(batch_size=size, cap_kg=cap, reads=reads,
                  gates_by_year=gates, n_changed=int(summ["n_changed"]),
                  first_tran_og=first, smolt_removed=int(summ["smolt_removed"]),
                  manual_events_file=ev, elapsed_s=time.perf_counter() - t0)


def _totals(cell: TrCell, years) -> dict:
    ys = tuple(sorted(years))
    return dict(years=ys,
                revenue=float(sum(cell.reads[y].revenue for y in ys)),
                hog_t=float(sum(cell.reads[y].hog_t for y in ys)),
                gain_t=float(sum(cell.reads[y].gain_t for y in ys)))


def judge_today(today: TrCell) -> TrCell:
    """Today's plan's totals over every year it ran (the same years every
    cell covers: one PR, one horizon)."""
    if today.error is not None:
        return today
    return replace(today, **_totals(today, today.reads))


def judge_cell(cell: TrCell, today: TrCell, *,
               undated: Optional[int] = None) -> TrCell:
    """The cell against today's plan: its effect year, the two-window
    `verdict`, the objective totals over every year both runs cover, and the
    fields ideal_optimize's ranking reads (`breaches` = how far worse than
    today in the early years + breaches in the judged years, per key;
    `total`; `gates` = the non-limit FAIL gates, one per name). An errored
    cell comes back as it is (never within the limits)."""
    if cell.error is not None:
        return cell
    if today.error is not None:
        raise ValueError(f"today's plan did not run ({today.error}): a cell "
                         f"cannot be judged against it")
    years = sorted(set(cell.reads) & set(today.reads))
    e, why = effect_year({"n_changed": cell.n_changed,
                          "first_changed_tran_og_date": cell.first_tran_og},
                         years or sorted(cell.reads),
                         undated_cap_year=undated,
                         cap_changed=float(cell.cap_kg) != float(today.cap_kg))
    v = verdict(today.reads, cell.reads, cell.gates_by_year, e)
    ex, jd = v.excess(), v.judged()
    b = {k: int(ex[k] + jd[k]) for k in io.BREACH_KEYS}
    seen, gates = set(), []
    for y in years:
        for g in cell.gates_by_year[y]:
            if (g.status == "FAIL" and g.name not in io.LIMIT_GATES
                    and g.name not in seen):
                seen.add(g.name)
                gates.append(g)
    return replace(cell, **_totals(cell, years), effect=e, effect_why=why,
                   verdict=v, breaches=b, total=int(sum(b.values())),
                   gates=tuple(gates), within_limits=v.within_limits)


# --------------------------------------------------------------------------- #
# The grid
# --------------------------------------------------------------------------- #
def check_grid(sizes: Sequence[int], caps: Sequence[float],
               ceiling_kg: float) -> list:
    """The (size, cap_kg) cells to run, duplicates removed, ordered size,
    then cap DESCENDING (the operator's own cap first). Refuses, before
    anything runs: an empty grid, a size that is not a whole number of at
    least ideal_optimize.MIN_SIZE fish, a cap that is not a positive finite
    number or is above `ceiling_kg` (the step-1 cap slider: never searched
    above), and more than TR_MAX_CELLS cells."""
    ceiling = io._cap(ceiling_kg)
    szs = sorted({io._whole(s, "batch sizes (fish)", io.MIN_SIZE)
                  for s in (sizes or ())})
    cps = sorted({io._cap(c) for c in (caps or ())}, reverse=True)
    if not szs or not cps:
        raise ValueError("the grid is empty: give at least one batch size and "
                         "one cap")
    above = [c for c in cps if c > ceiling]
    if above:
        raise ValueError(
            f"cap(s) {', '.join(f'{c / 1000:,.0f} t' for c in above)} above "
            f"the cap slider's {ceiling / 1000:,.0f} t: the optimizer never "
            f"searches above it")
    n = len(szs) * len(cps)
    if n > TR_MAX_CELLS:
        raise ValueError(f"{len(szs)} sizes x {len(cps)} caps = {n} cells; at "
                         f"most {TR_MAX_CELLS} (each is one ~90 s engine run "
                         f"on the PR)")
    return [(s, p) for s in szs for p in cps]


def optimize_transition(live, forecast_start, cutoff, sizes: Sequence[int],
                        caps: Sequence[float], pr_path, *, ceiling_kg: float,
                        project_dir, objective: str = "revenue",
                        workers: int = 1,
                        progress: Optional[Callable] = None,
                        overrides: Optional[dict] = None,
                        method: str = ie.DEFAULT_METHOD,
                        method_overrides: Optional[dict] = None,
                        density_overrides: Optional[dict] = None,
                        system_overrides: Optional[dict] = None,
                        control=None,
                        horizon_weeks: int = TR_HORIZON_WEEKS) -> TrResult:
    """Run today's plan ONCE (current limits) and every (size, cap) cell, and
    pick the best cell within the limits under the two-window rule.

    Every future stocking (transition.propose, input date strictly after
    max(forecast_start, cutoff)) is re-sized to the cell's size; each cell
    runs, and is judged, at its own cap, with `overrides` (the what-if limits
    EXCEPT the cap — a max_biomass_kg there is refused), the tank/system
    what-ifs, the method and the promoted knobs (`run_cell`). The objective
    (ideal_optimize.OBJECTIVES: revenue, HOG, biomass gain) is SUMMED over
    every year both the cell and today's plan cover (one PR, one horizon: the
    same years for every cell). Ranking, tie-break (the smaller batch, then
    the higher cap), closest, `unconstrained` and the stability decision are
    ideal_optimize's. Stability: the top TR_MAX_STABILITY_CANDIDATES
    within-limits cells, each re-judged at size +/- NEIGHBOUR_STEP at the
    same cap, in one extra wave of at most 2 x TR_MAX_STABILITY_CANDIDATES
    runs (a grid cell with that size and cap is reused, not re-run).

    Refused with ValueError before anything runs: an unknown objective, a bad
    grid (`check_grid`: empty, a size under MIN_SIZE, a cap <= 0 or above
    `ceiling_kg`, more than TR_MAX_CELLS cells), a cap in `overrides`, a
    workers value that is not a positive whole number, a PR that is not a
    file, and a size that transition.propose refuses. `progress(done, total,
    cell)` is called as each run finishes (today's plan counts as one; the
    total grows by the stability wave). Pool behaviour:
    ideal_optimize._run_wave, each job naming its runner. Today's
    plan failing raises RuntimeError: nothing can be judged without it.
    """
    if objective not in io.OBJECTIVES:
        raise ValueError(f"objective must be one of {sorted(io.OBJECTIVES)}, "
                         f"got {objective!r}")
    jobs = check_grid(sizes, caps, ceiling_kg)
    ov = dict(overrides or {})
    if "max_biomass_kg" in ov:
        raise ValueError(
            f"overrides carry max_biomass_kg={ov['max_biomass_kg']!r}: the cap "
            f"goes in `caps` (each cell runs at its own cap), never in "
            f"overrides")
    if (isinstance(workers, bool) or not isinstance(workers, numbers.Integral)
            or workers < 1):
        raise ValueError(f"workers must be a positive whole number, got "
                         f"{workers!r}")
    if not Path(pr_path).is_file():
        raise ValueError(f"ProductionReport not found: {pr_path}")
    for s in sorted({s for s, _c in jobs}):
        tr.propose(live, forecast_start, cutoff, [s])   # refuses a bad size
    if control is None:
        control = _load_control(project_dir)
    from forecast import scenario_io as sio
    flimits = sio.load_limits(str(Path(project_dir) / "scenario"), control)[0]

    pd_ = str(project_dir)
    today_kw = dict(pr_path=str(pr_path), method=method,
                    method_overrides=dict(method_overrides or {}),
                    control=control, horizon_weeks=horizon_weeks)
    cell_kw = dict(live=list(live), forecast_start=forecast_start,
                   cutoff=cutoff, pr_path=str(pr_path), overrides=ov,
                   method=method, method_overrides=dict(method_overrides or {}),
                   density_overrides=density_overrides,
                   system_overrides=system_overrides, control=control,
                   horizon_weeks=horizon_weeks)
    count = {"done": 0, "total": len(jobs) + 1}

    def _tick(cell):
        count["done"] += 1
        if progress is not None:
            progress(count["done"], count["total"], cell)

    first: dict = {}

    def _record(i, cell):
        first[i] = cell
        _tick(cell)

    wave = ([(run_today, (list(live), pd_), today_kw)]
            + [(run_cell, ((s, c), pd_), cell_kw) for s, c in jobs])
    note = io._run_wave(wave, pd_, {}, workers, _record, "the grid")
    today = first[0]
    if today.error is not None:
        raise RuntimeError(f"today's plan could not run — {today.error}: "
                           f"there is nothing to judge the proposals against")
    today = judge_today(today)
    undated = undated_cap_year(flimits, today.reads)
    cells = tuple(judge_cell(first[i + 1], today, undated=undated)
                  for i in range(len(jobs)))
    b = io.best(cells, objective)

    stability, best_stable = (), None
    if b is not None:
        cands = io.stability_candidates(
            cells, objective)[:TR_MAX_STABILITY_CANDIDATES]
        ran = {c.key: c for c in cells}
        extra = []                          # candidate order, then -/+ step
        for c in cands:
            for r in io.neighbour_rhythms(c):
                if r not in ran and r not in extra:
                    extra.append(r)
        if extra:
            count["total"] += len(extra)
            more: dict = {}

            def _record_more(i, cell):
                more[i] = cell
                _tick(cell)

            note2 = io._run_wave([(run_cell, ((r[1], r[2]), pd_), cell_kw)
                                  for r in extra], pd_, {}, workers,
                                 _record_more, "the stability runs")
            ran.update({r: judge_cell(more[i], today, undated=undated)
                        for i, r in enumerate(extra)})
            note = " ".join(x for x in (note, note2) if x) or None
        stability, best_stable = io.judge_stability(cands, ran)
    return TrResult(today=today, cells=cells, objective=objective, best=b,
                    closest=None if b is not None else io.closest(cells,
                                                                  objective),
                    note=note, unconstrained=io.ignoring_limits(cells,
                                                                objective),
                    stability=stability, best_stable=best_stable,
                    ceiling_kg=float(ceiling_kg), undated_cap_year=undated)
