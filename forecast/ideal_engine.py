"""The Ideal, priced by the REAL engine: tanks, density, handling and all.

`forecast.ideal` prices a stocking rhythm with L1, the TANKLESS envelope,
which has no tanks, no density cap and no handling budget. The operator's
ruling (2026-09-10) is "one tool": the Ideal shows the real engine's numbers
and the real engine's tank layout. So this module adds NO planning logic. It
builds inputs the engine already accepts, runs the unchanged, validated
engine (`forecast.methods.run_method`, controller-hybrid by default) in a
throwaway copy of config + scenario, and reads the workbook it writes.

What is read: on an empty start, the LAST COMPLETE (ISO label) year of the
horizon, computed by `last_complete_year`. Year 2 of a 104-week run is still
start-up (the engine lands only 0.94 / 0.88 of what it stocks there, and the
figure moves with the horizon), so `ideal_run` runs 156 weeks and reads year
3, the same steady window as L1's `ideal.WINDOW`.

MEASURED on that same steady window (156 w from 2027-01-04, year 2029;
L1 = `ideal.evaluate`), for two engines:

    cell                   controller (promoted*)   controller-hybrid      L1
    49d x 280k @ 3,800 t   6,583 t / $136.8M        6,548 t / $135.6M      7,625 t / $163.6M
    49d x 250k @ 4,200 t   6,582 t / $141.0M        6,317 t / $133.7M      7,642 t / $167.3M

    * the operator's promoted default on 2026-09-10: controller, hybrid off,
      chronic_pressure_weeks 6, facility_biomass_deviation_pct 0.01,
      harvest_smooth_lookahead_weeks 12, global_buffer_pct 0.1.

L1 runs ~16% high on tonnage against either engine. The RANKING depends on
the engine: the promoted controller agrees with L1 (250k @ 4,200 t wins), the
hybrid flips it. So an L1 figure is not a stand-in for an engine's, and the
app runs whichever method ▶ Run forecast uses (`method` + `method_overrides`).

Two ways in:
  * EMPTY START (the Ideal). A clean facility has no ProductionReport, but the
    engine's own input can say "empty": a ProductionReport sheet with a
    closing date and no rows hydrates to 0 tanks and 0 batches. Batches that
    entered OG on or before the start cannot be placed (the engine skips them,
    biology.py), so they are dropped here and REPORTED. 6N cannot bootstrap
    its purge pipeline from nothing (harvest freezes), so an empty start must
    run 6N in production mode. A control that says otherwise is refused, not
    patched. `ideal_run` sets sixn_production_start before the start, which
    is also the facility's own plan (production from 2028-01-01).
  * REAL PR (the transition). A proposed schedule on top of a real
    ProductionReport, optionally with that PR's manual events.

rc 0 and clean conservation audits do NOT mean a plan is feasible: an
overstocked run returned rc 0 at 9,329 kg/m3 and 1,685% of the cap. `gates`
is the plausibility check, and a plan is plausible iff no gate FAILs.

Hard limits (operator ruling 2026-09-10): a plan is within the limits iff no
gate FAILs. Every facility limit is a FAIL when broken, with zero breaches
allowed: every week harvests, the harvest floor, the biomass cap (no
tolerance in the engine: the 2% ideal.PEAK_TOLERANCE is the tankless quick
scan's one-week-lag allowance only), tank density (R8), the per-system
biomass and feed limits (flagged, as the engine's SystemLimitsAudit flags
them, above the limit x (1 + global_buffer_pct): the operator kept that
buffer), and the weekly move budget. "FW mass balance" stays a WARN: it is a
model-accounting note, not a facility limit, and an empty start trips it on
one early batch by construction.

Isolation: every run lives in tempfile.mkdtemp() and is removed afterwards
unless `keep_dir` is given. The project's config/ and scenario/ are only
read. run_method passes calib_log_path="", so the live FW calibration history
is never touched.

Revenue, HOG and the 8 lb share use `ideal.value_rows`, the SAME pricing the
L1 Ideal uses, so the two answers are comparable cell for cell.

Per-week limits in scenario/limits.yaml are ISO-week-labelled (all 2026
today), so a 2027+ start resolves every week to the Control defaults.
`limit_rows_in_horizon` says how many dated rows actually reach a run.

SYSTEM CONSTRAINTS as a what-if (operator, 2026-09-10): "what if the tanks
could hold more?" is answered by running the engine on different limits, not
by guessing. Three levers, each written ONLY into the run's temp copy:
  * `max_transfers_per_week` (an `overrides` key): the weekly move budget, a
    positive whole number (a float is refused, never truncated).
  * `density_overrides={system_id: kg/m3}`: every tank of that system gets
    that max_density_kg_m3 in the temp config/facility.yaml.
  * `system_overrides={system_id: {"biomass": kg, "feed_per_day": kg/day}}`:
    the system's standing limits in the temp scenario/limits.yaml
    system_defaults. The value lands where the system's PRODUCTION-mode
    weeks resolve it from: for OG6N, whose default has a `modes:` block,
    "biomass" replaces modes.production.biomass and the purge-mode biomass
    STAYS (it is an operator ruling about depuration, not a tank limit);
    "feed_per_day" replaces the system-level feed. Dated per-week `system`
    rows still win for their weeks (the engine's own precedence) and are
    not touched.
The engine reads facility.yaml and limits.yaml from the temp copy, and
`read_workbook` is handed the run's own copy, so R8 and the per-system
verdicts are judged against the OVERRIDDEN limits. Unknown systems / keys and
non-positive or non-finite values are refused before anything is written.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import math
import numbers
import os
import re
import shutil
import stat
import statistics
import sys
import tempfile
import time
import warnings
from collections import Counter, defaultdict
from dataclasses import dataclass, replace as _dc_replace
from pathlib import Path
from typing import Optional, Sequence

import openpyxl
import yaml

from forecast import ideal
from forecast import scenario_io as sio
from forecast.caps import (METRIC_BIOMASS, METRIC_MIN_HARVEST, MODE_PRODUCTION,
                           FacilityLimits, resolve_facility_cap)
from forecast.config_io import FACILITY_FILE, control_from_dict, load_config
from forecast.methods import REGISTRY, run_method
from forecast.models import BatchInput
from forecast.production_report import find_pr_sheet, parse_pr_worksheet
from forecast.sixn import is_purge_mode
from forecast.tiers import HARVEST_PREP_DENSITY_CAP, effective_density_cap

DEFAULT_METHOD = "controller-hybrid"
# The app's pseudo-method (app._AS_CONFIGURED): the controller engine on the
# given config EXACTLY as-is, with no registry pins. ▶ Run forecast runs it
# when the promoted plan is a tuned config rather than a registry arm, so the
# Ideal must be able to run it too — "one tool" means the SAME method.
AS_CONFIGURED = "as-configured"
ALLOWED_OVERRIDES = frozenset({
    "horizon_weeks", "max_biomass_kg", "max_harvest_per_week",
    "min_harvest_per_week", "min_harvest_weight_g", "max_feed_per_day_kg",
    "sixn_production_start", "scenario_name",
    # The weekly handling budget (moves/week). A positive whole number.
    "max_transfers_per_week",
})
# The per-system limits a what-if may set (scenario/limits.yaml
# system_defaults metrics; caps.METRIC_* names).
SYSTEM_OVERRIDE_KEYS = frozenset({"biomass", "feed_per_day"})
# The labels production_report._resolve_pr_columns looks for, in the columns
# (F-K) a real report puts them.
_PR_HEADER = ((6, "Opening Count"), (7, "Closing Count"),
              (8, "Opening Biomass [kg]"), (9, "Closing Biomass [kg]"),
              (10, "Opening Avg weight"), (11, "Closing Avg weight"))
_VALIDATION_TOP_N = 8
_LOG_TAIL_LINES = 40
# The biomass cap is a HARD limit in the engine (operator, 2026-09-10): a week
# is over it when standing / cap exceeds 1 by more than float noise.
CAP_EPSILON = 1e-9


@dataclass(frozen=True)
class Gate:
    name: str
    status: str                  # "PASS" | "WARN" | "FAIL"
    detail: str


@dataclass(frozen=True)
class YearRead:
    """One label-year (ISO "YYYY-Www" weeks) of an engine workbook.

    Totals cover the weeks the year actually has in the run (`weeks`); they
    are not annualised. `peak_pct_of_cap` is a fraction, as in ideal.Rhythm,
    over the weeks the engine caps (`capped_weeks`); 0.0 when none is capped.
    Caps and floors are resolved per week by `caps.resolve_facility_cap`, the
    engine's own rule, so an override of 0 means "no cap / no floor".
    `floor_min` / `floor_max` span the year's resolved floors (None when no
    week has one). Moves are the engine's handling unit: DISTINCT
    (From_Tank, To_Tank) pairs of Type=Transfer rows per week.
    """
    year: int
    weeks: int
    harvest_fish: float
    harvest_gross_t: float
    hog_t: float
    revenue: float
    avg_gross_kg: float
    share_over_8lb: float
    harvest_fish_wk_min: float
    harvest_fish_wk_max: float
    zero_weeks: int
    under_floor_weeks: int
    standing_peak_kg: float
    standing_mean_kg: float
    peak_pct_of_cap: float
    og_tanks_mean: float
    og_tanks_max: int
    og_tanks_total: int
    r8_over_tank_weeks: int
    r8_worst: Optional[dict]
    moves_mean: float
    moves_max: int
    weeks_over_move_budget: int
    per_system: dict
    floor_min: Optional[float] = None
    floor_max: Optional[float] = None
    capped_weeks: Optional[int] = None
    # Per-system limits, as the engine's own SystemLimitsAudit judges them:
    # (system, week) rows whose biomass / feed is above the system's limit
    # times (1 + global_buffer_pct); 6N is exempt while it purges. A plan
    # "within the system constraints" (operator, 2026-09-10) needs these too,
    # not only the facility cap and tank density.
    sys_bio_over_weeks: int = 0
    sys_feed_over_weeks: int = 0
    sys_worst: Optional[dict] = None
    # Weeks whose standing biomass (OG + FW, as `standing_peak_kg`) exceeds
    # that week's resolved cap by more than CAP_EPSILON; 0 when no week is
    # capped. > 0 exactly when peak_pct_of_cap > 1 + CAP_EPSILON.
    over_cap_weeks: int = 0
    # Biomass gain in the year, tonnes: the LIVE (gross, round) weight
    # harvested in the year (HarvestPlan "Gross_Biomass", the same source as
    # `harvest_gross_t` and `avg_gross_kg`) plus the change in standing
    # biomass (OG + FW) from the previous year's last week to this year's last
    # week (from this year's first week when the run has no previous year).
    # Fish that die are not gain: mortality lowers standing and is never
    # added back, so this is growth net of losses. New eggs entering FW are
    # counted as they arrive (a few kg against thousands of tonnes).
    gain_t: float = 0.0
    # Cost DRIVERS (2026-09-11), never dollars: read_workbook never reads
    # costs.yaml. None = not read (a workbook without FeedForecastWeekly, or
    # a caller that did not ask); forecast.costs.year_cost refuses None
    # loudly, so a missing driver is never priced as 0.
    feed_kg_by_type: Optional[dict] = None   # {feed name: kg} over the year's
                                             # weeks from the run's start
    eggs: Optional[float] = None             # eggs stocked in the year
    # The first ISO week read for the year ("YYYY-Www"). With `weeks` it
    # names the calendar days the year's figures cover, so
    # forecast.costs.year_cost charges the fixed monthly cost by those days
    # (a 53-week year is 371 days, not 12 months). None = not read.
    first_week: Optional[str] = None
    # Standing biomass (OG + FW, kg, as `standing_peak_kg`) in the year's
    # last week read: the fish still in the water when the year - or, for
    # the last year, the run - ends. None = not read.
    standing_end_kg: Optional[float] = None


@dataclass(frozen=True)
class EngineRun:
    rc: int
    elapsed_s: float
    method: str
    start: dt.date
    horizon_weeks: int
    overrides: dict
    dropped_batches: tuple
    years: dict
    layout_week: str
    layout: dict
    tank_sequence: dict
    audits: dict
    validation_top: dict
    limit_rows_in_horizon: int
    out_path: Optional[str] = None
    method_overrides: Optional[dict] = None
    # The validated system-constraint what-ifs the run was made with ({} when
    # none): {system: kg/m3} and {system: {"biomass"|"feed_per_day": value}}.
    density_overrides: Optional[dict] = None
    system_overrides: Optional[dict] = None


def _method(key: str):
    """The Method to run: a registry arm, or the as-configured pseudo-method."""
    if key == AS_CONFIGURED:
        from forecast.methods import Method
        return Method(key=AS_CONFIGURED, label="As configured",
                      family="Controller", blurb="", engine="controller")
    if key not in REGISTRY:
        raise ValueError(f"unknown method {key!r}; registered: "
                         f"{sorted(REGISTRY)} or {AS_CONFIGURED!r}")
    return REGISTRY[key]


@dataclass(frozen=True)
class _Harvest:
    """The two fields ideal.value_rows reads from a harvest row."""
    count: float
    avg_wt_g: float


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def _as_date(value, what: str) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value.strip()[:10])
        except ValueError:
            pass
    raise ValueError(f"{what} must be a date or an ISO 'YYYY-MM-DD' string, "
                     f"got {value!r}")


def _as_datetime(value, what: str) -> dt.datetime:
    d = _as_date(value, what)
    return dt.datetime(d.year, d.month, d.day)


def _week_label(d) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _horizon_labels(start: dt.date, weeks: int) -> list[str]:
    return [_week_label(start + dt.timedelta(weeks=i)) for i in range(weeks)]


def _label_year(label) -> int:
    return int(str(label)[:4])


def _copy_tree(src: Path, dst: Path) -> None:
    """Copy file CONTENTS only, never attributes.

    shutil.copytree also copies each directory's attributes, and OneDrive
    marks its folders ReadOnly. Windows then refuses to remove the temp copy,
    so rmtree(ignore_errors=True) silently left an empty config/ behind after
    every run (measured 2026-09-10; run_method's own as_cmp_* dirs too, since
    they copy from these).
    """
    dst.mkdir(parents=True)
    for p in sorted(src.rglob("*")):
        target = dst / p.relative_to(src)
        if p.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, target)


def _clear_readonly_and_retry(func, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _remove_tree(path) -> None:
    """Remove a temp run dir; warn if it survives, never fail silently."""
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_clear_readonly_and_retry)
        else:
            shutil.rmtree(path, onerror=_clear_readonly_and_retry)
    except OSError as exc:
        warnings.warn(f"could not remove the temp run dir {path}: {exc}",
                      RuntimeWarning, stacklevel=2)


def _check_overrides(overrides, horizon_weeks) -> dict:
    """Validate caller overrides -> the values to write, keys sorted."""
    if (isinstance(horizon_weeks, bool)
            or not isinstance(horizon_weeks, numbers.Integral)
            or horizon_weeks < 1):
        raise ValueError(f"horizon_weeks must be a positive whole number of "
                         f"weeks, got {horizon_weeks!r}")
    given = dict(overrides or {})
    unknown = sorted(set(given) - ALLOWED_OVERRIDES)
    if unknown:
        raise ValueError(f"control override(s) not allowed here: {unknown}. "
                         f"Allowed: {sorted(ALLOWED_OVERRIDES)}")
    out = {}
    for key in sorted(given):
        v = given[key]
        if key == "horizon_weeks":
            if v != horizon_weeks:
                raise ValueError(f"horizon_weeks given twice ({horizon_weeks} "
                                 f"and overrides {v!r}); pass it once")
            out[key] = int(horizon_weeks)
        elif key == "sixn_production_start":
            out[key] = None if v is None else _as_date(v, key).isoformat()
        elif key == "scenario_name":
            if not isinstance(v, str) or not v.strip():
                raise ValueError(f"scenario_name must be a non-empty string, "
                                 f"got {v!r}")
            out[key] = v
        elif key == "max_transfers_per_week":
            # The engine coerces with int(float(x)), so 15.5 would silently
            # become 15; a whole number is required instead.
            if (isinstance(v, bool) or not isinstance(v, numbers.Integral)
                    or v < 1):
                raise ValueError(f"max_transfers_per_week (the weekly move "
                                 f"budget) must be a positive whole number, "
                                 f"got {v!r}")
            out[key] = int(v)
        else:
            if (isinstance(v, bool) or not isinstance(v, numbers.Real)
                    or not math.isfinite(v) or v < 0):
                raise ValueError(f"control override {key!r} must be a "
                                 f"non-negative number, got {v!r}")
            out[key] = float(v)
    return out


def _positive(v, what: str) -> float:
    if (isinstance(v, bool) or not isinstance(v, numbers.Real)
            or not math.isfinite(v) or not v > 0):
        raise ValueError(f"{what} must be a finite number above 0, got {v!r}")
    return float(v)


def _read_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path} does not hold a YAML mapping")
    return doc


def _check_density_overrides(given, facility_doc: dict) -> dict:
    """{system_id: max_density_kg_m3} -> validated floats, keys sorted."""
    if given is None:
        return {}
    if not isinstance(given, dict):
        raise ValueError(f"density_overrides must be a dict {{system_id: "
                         f"kg/m3}}, got {type(given).__name__}")
    tanks = facility_doc.get("tanks")
    if not isinstance(tanks, list):
        raise ValueError("config/facility.yaml has no `tanks:` list")
    known = sorted({str(t.get("system_id")) for t in tanks
                    if isinstance(t, dict) and t.get("system_id") is not None})
    unknown = sorted(str(s) for s in given
                     if not isinstance(s, str) or s not in known)
    if unknown:
        raise ValueError(f"density_overrides: unknown system id(s) {unknown}; "
                         f"config/facility.yaml has {known}")
    return {s: _positive(given[s], f"density_overrides[{s!r}] "
                                   f"(max_density_kg_m3)")
            for s in sorted(given)}


def _check_system_overrides(given, limits_doc: dict) -> dict:
    """{system_id: {"biomass"|"feed_per_day": value}} -> validated floats."""
    if given is None:
        return {}
    if not isinstance(given, dict):
        raise ValueError(f"system_overrides must be a dict {{system_id: "
                         f"{{'biomass': kg, 'feed_per_day': kg/day}}}}, got "
                         f"{type(given).__name__}")
    defaults = limits_doc.get("system_defaults") or {}
    if not isinstance(defaults, dict):
        raise ValueError("scenario/limits.yaml system_defaults is not a mapping")
    known = sorted(str(s) for s in defaults)
    unknown = sorted(str(s) for s in given
                     if not isinstance(s, str) or s not in known)
    if unknown:
        raise ValueError(f"system_overrides: unknown system id(s) {unknown}; "
                         f"scenario/limits.yaml system_defaults has {known}")
    out = {}
    for s in sorted(given):
        limits = given[s]
        if not isinstance(limits, dict) or not limits:
            raise ValueError(f"system_overrides[{s!r}] must be a non-empty "
                             f"dict with 'biomass' and/or 'feed_per_day', got "
                             f"{limits!r}")
        bad = sorted(str(k) for k in limits
                     if not isinstance(k, str) or k not in SYSTEM_OVERRIDE_KEYS)
        if bad:
            raise ValueError(f"system_overrides[{s!r}]: unknown key(s) {bad}; "
                             f"allowed: {sorted(SYSTEM_OVERRIDE_KEYS)}")
        out[s] = {k: _positive(limits[k], f"system_overrides[{s!r}][{k!r}]")
                  for k in sorted(limits)}
    return out


def _apply_density(facility_doc: dict, dens: dict) -> dict:
    """The facility with each overridden system's new max_density_kg_m3.
    Every tank is COPIED before it is written and the input is never changed:
    with a YAML anchor two entries can be ONE dict object (a deep copy keeps
    that sharing), and writing one would silently move the other."""
    doc = dict(facility_doc)
    doc["tanks"] = [dict(t) if isinstance(t, dict) else t
                    for t in facility_doc["tanks"]]
    for t in doc["tanks"]:
        if isinstance(t, dict) and t.get("system_id") in dens:
            t["max_density_kg_m3"] = dens[t["system_id"]]
    return doc


def _apply_system(limits_doc: dict, sys_ov: dict) -> dict:
    """Write each value where the system's PRODUCTION-mode weeks resolve it
    from (caps.SystemLimits.resolve: mode default > system default). For a
    system with a `modes:` block, "biomass" ALWAYS goes to modes.production
    (created if missing), so its purge weeks can never move; any other metric
    goes to modes.production when that entry names it, else to the system
    level. Each touched block is copied first and the input is never changed
    — a YAML anchor can make two systems share ONE dict (see _apply_density)."""
    doc = dict(limits_doc)
    defaults = doc["system_defaults"] = dict(limits_doc["system_defaults"])
    for s, limits in sys_ov.items():
        old = defaults.get(s)
        block = defaults[s] = dict(old) if isinstance(old, dict) else {}
        modes = block.get(sio.MODES_KEY)
        prod = None
        if isinstance(modes, dict):
            modes = block[sio.MODES_KEY] = dict(modes)
            prev = modes.get(MODE_PRODUCTION)
            prod = dict(prev) if isinstance(prev, dict) else {}
        for k, v in limits.items():
            if prod is not None and (k == "biomass" or prod.get(k) is not None):
                prod[k] = v
                modes[MODE_PRODUCTION] = prod
            else:
                block[k] = v
    return doc


def write_empty_pr(path, closing_date):
    """An EMPTY ProductionReport: the closing banner and header, zero rows.

    Hydrates to 0 tanks and 0 batches, with forecast_start = closing + 1 day.
    """
    d = _as_date(closing_date, "closing_date")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ProductionReport"
    for col, label in _PR_HEADER:
        ws.cell(2, col, label)
    ws.cell(3, 1, f"Closing Month: {d.month}/{d.day}/{d.year}")
    wb.save(str(path))
    return path


def _pr_closing(pr_path) -> dt.date:
    wb = openpyxl.load_workbook(str(pr_path), read_only=True, data_only=True)
    try:
        ws = find_pr_sheet(wb)
        if ws is None:
            raise ValueError(f"{pr_path}: no sheet parses as a "
                             f"ProductionReport")
        closing, _og, _fw = parse_pr_worksheet(ws, quiet=True)
    finally:
        wb.close()
    if closing is None:
        raise ValueError(f"{pr_path}: the ProductionReport has no "
                         f"'Closing Month: m/d/yyyy' date")
    return closing


def prepare(work_dir, batches: Sequence[BatchInput], project_dir, *,
            start=None, horizon_weeks: int, overrides: Optional[dict] = None,
            pr_path=None, include_manual_events: bool = False,
            method_overrides: Optional[dict] = None,
            density_overrides: Optional[dict] = None,
            system_overrides: Optional[dict] = None) -> dict:
    """Lay out one engine run under `work_dir`; nothing is written elsewhere.

    Empty start when `pr_path` is None: `start` is required, the PR closes the
    day before, and batches with tran_og_date on/before the start are dropped
    and returned in `dropped`. Real PR otherwise: batches are used as-is and
    the start is the PR's closing + 1 day.

    `method_overrides` is a promoted plan's own knob set (app._effective_method),
    layered onto control.yaml the way ▶ Run forecast layers it
    (optimize.config_dir_with_overrides): promoted knobs first, then this
    run's `overrides` (the operator's per-run limits win), then run_method
    puts the method's registry pins on top. Any control key is allowed, but an
    unknown one is refused — a knob that silently did nothing would make the
    run look like the promoted plan when it is not.

    `density_overrides` ({system_id: max_density_kg_m3}) and
    `system_overrides` ({system_id: {"biomass": kg, "feed_per_day": kg/day}})
    are the system-constraint what-ifs (module docstring): written into the
    temp facility.yaml / limits.yaml only, the rest of both files kept as
    data. Without them both files are copied verbatim.

    Every check runs BEFORE the first file is written, so a refused call
    leaves `work_dir` untouched.
    -> dict(pr_path, config_dir, scenario_dir, dropped, start, overrides,
            manual_events_file, method_overrides, density_overrides,
            system_overrides)
    """
    ov = _check_overrides(overrides, horizon_weeks)
    root = Path(project_dir)
    for sub in ("config", "scenario"):
        if not (root / sub).is_dir():
            raise ValueError(f"{root} has no {sub}/ directory; is it the "
                             f"project folder?")
    limits_src = root / "scenario" / sio.LIMITS_FILE
    if not limits_src.is_file():
        raise ValueError(f"{limits_src} is missing")
    facility_src = root / "config" / FACILITY_FILE
    facility_doc = limits_doc = None
    if density_overrides is not None:
        facility_doc = _read_yaml(facility_src)
    dens = _check_density_overrides(density_overrides, facility_doc or {})
    if system_overrides is not None:
        limits_doc = _read_yaml(limits_src)
    sys_ov = _check_system_overrides(system_overrides, limits_doc or {})
    batches = list(batches)
    if not batches:
        raise ValueError("no batches to run")
    dup = sorted(k for k, n in Counter(b.batch_id for b in batches).items()
                 if n > 1)
    if dup:
        raise ValueError(f"duplicate batch id(s) {dup}: the engine keys "
                         f"batches by id, so one would replace the other")

    if pr_path is None:
        if start is None:
            raise ValueError("an empty start needs `start` (the forecast start "
                             "date); a real start reads it from pr_path")
        if include_manual_events:
            raise ValueError("manual events belong to one real "
                             "ProductionReport; an empty start has none")
        start_dt = _as_datetime(start, "start")
        dropped = tuple(b.batch_id for b in batches
                        if b.tran_og_date is not None
                        and _as_date(b.tran_og_date, b.batch_id) <= start_dt.date())
        gone = set(dropped)
        kept = [b for b in batches if b.batch_id not in gone]
    else:
        if not Path(pr_path).is_file():
            raise ValueError(f"ProductionReport not found: {pr_path}")
        closing = _pr_closing(pr_path)
        start_dt = _as_datetime(closing + dt.timedelta(days=1), "start")
        if start is not None and _as_date(start, "start") != start_dt.date():
            raise ValueError(
                f"start {start} disagrees with the ProductionReport, which "
                f"closes {closing} (forecast start = closing + 1 day = "
                f"{start_dt.date()}); omit start in real-PR mode")
        kept, dropped = batches, ()
    if not kept:
        raise ValueError(f"every batch entered OG on or before the start "
                         f"{start_dt.date()}; nothing is left to stock")

    with open(root / "config" / "control.yaml", encoding="utf-8") as f:
        control_doc = yaml.safe_load(f) or {}
    knobs = dict(method_overrides or {})
    if knobs:
        known = control_from_dict(dict(control_doc))
        unknown = sorted(k for k in knobs if not hasattr(known, k))
        if unknown:
            raise ValueError(f"promoted knob(s) {unknown} are not control "
                             f"settings; refusing rather than running a plan "
                             f"that only looks like the promoted one")
    control_doc.update(knobs)
    control_doc.update(ov)
    control_doc["horizon_weeks"] = int(horizon_weeks)
    # The engine's own coercion: a bad value fails here, not mid-run.
    control = control_from_dict(control_doc)
    if pr_path is None and is_purge_mode(control, start_dt.date()):
        raise ValueError(
            f"an empty facility cannot start with 6N in PURGE mode "
            f"(sixn_production_start={control.sixn_production_start}, start="
            f"{start_dt.date()}): the purge pipeline has nothing to bootstrap "
            f"from and harvest freezes. Pass overrides={{'sixn_production_"
            f"start': <a date on or before the start>}}, as ideal_run does")

    work = Path(work_dir)
    cfg, scn = work / "config", work / "scenario"
    if cfg.exists() or scn.exists():
        raise ValueError(f"{work} already holds a config/ or scenario/; "
                         f"prepare needs a fresh directory")

    work.mkdir(parents=True, exist_ok=True)
    _copy_tree(root / "config", cfg)
    with open(cfg / "control.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(control_doc, f, sort_keys=False)
    if dens:
        with open(cfg / FACILITY_FILE, "w", encoding="utf-8") as f:
            f.write(f"# What-if copy written by forecast.ideal_engine: "
                    f"max_density_kg_m3 overridden {dens}\n")
            yaml.safe_dump(_apply_density(facility_doc, dens), f,
                           sort_keys=False)
    scn.mkdir()
    with open(scn / sio.BATCHES_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump({"batches": sio.batches_to_list(kept)}, f,
                       sort_keys=False)
    if sys_ov:
        with open(scn / sio.LIMITS_FILE, "w", encoding="utf-8") as f:
            f.write(f"# What-if copy written by forecast.ideal_engine: "
                    f"system_defaults overridden {sys_ov}\n")
            yaml.safe_dump(_apply_system(limits_doc, sys_ov), f,
                           sort_keys=False)
    else:
        shutil.copyfile(limits_src, scn / sio.LIMITS_FILE)
    events_file = None
    if include_manual_events:
        src = root / "scenario" / "manual_events"
        if src.is_dir():
            _copy_tree(src, scn / "manual_events")
            mine = scn / "manual_events" / f"{closing:%Y-%m-%d}.yaml"
            events_file = mine.name if mine.is_file() else None
    if pr_path is None:
        pr = write_empty_pr(work / "pr_empty.xlsx",
                            start_dt.date() - dt.timedelta(days=1))
    else:
        pr = Path(pr_path).resolve()
    return dict(pr_path=str(pr), config_dir=str(cfg), scenario_dir=str(scn),
                dropped=dropped, start=start_dt.date(), overrides=ov,
                manual_events_file=events_file, method_overrides=knobs,
                density_overrides=dict(dens),
                system_overrides={s: dict(v) for s, v in sys_ov.items()})


# --------------------------------------------------------------------------- #
# Reading the workbook
# --------------------------------------------------------------------------- #
def _table(wb, name: str, first: str) -> tuple[list, list]:
    """(header, rows as dicts) of a sheet whose header row starts `first`."""
    if name not in wb.sheetnames:
        raise ValueError(f"workbook has no {name!r} sheet")
    hdr, out = None, []
    for r in wb[name].iter_rows(values_only=True):
        if hdr is None:
            if r and r[0] == first:
                hdr = [str(x) if x is not None else f"_{i}"
                       for i, x in enumerate(r)]
            continue
        if not r or all(x is None for x in r):
            continue
        out.append(dict(zip(hdr, r)))
    if hdr is None:
        raise ValueError(f"{name}: no header row starting {first!r}")
    return hdr, out


def _col(hdr: list, prefix: str, sheet: str) -> str:
    for h in hdr:
        if h.startswith(prefix):
            return h
    raise ValueError(f"{sheet}: no column starting {prefix!r} (have {hdr})")


def _input_conservation(wb) -> tuple[list, dict]:
    """The audit's status lines (between the title and the table) and the
    per-batch Status counts."""
    name = "InputConservationAudit"
    if name not in wb.sheetnames:
        raise ValueError(f"workbook has no {name!r} sheet")
    lines, status, hdr = [], Counter(), None
    for i, r in enumerate(wb[name].iter_rows(values_only=True)):
        first = r[0] if r else None
        if hdr is None:
            if first == "Batch":
                hdr = [str(x) if x is not None else f"_{j}"
                       for j, x in enumerate(r)]
            elif (i >= 1 and isinstance(first, str) and first.strip()
                  and not first.startswith("Generated:")):
                lines.append(first.strip())
            continue
        if not r or all(x is None for x in r):
            continue
        status[str(dict(zip(hdr, r)).get("Status"))] += 1
    if hdr is None:
        raise ValueError(f"{name}: no table header starting 'Batch'")
    return lines, dict(sorted(status.items()))


def _day(value) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    raise ValueError(f"BatchLocations: Week_Start {value!r} is not a date")


def _moves_per_week(rows, t_from: str, t_to: str, t_cnt: str) -> dict:
    """The engine's handling unit (placement._moves_left, the analysis
    handling gate): DISTINCT applied (source, dest) tank pairs per week. A
    multi-destination move is one pair per destination; two legs of one pair
    are one move; TranOG and Grade rows are not moves."""
    pairs = defaultdict(set)
    for r in rows:
        if r.get("Type") != "Transfer" or float(r[t_cnt] or 0.0) < 0.5:
            continue
        w = str(r["Week"])
        pairs[w].add((_tank_key(r[t_from], w), _tank_key(r[t_to], w)))
    return {w: len(p) for w, p in pairs.items()}


def _tank_key(value, week) -> str:
    """A TransferPlan tank cell as one comparable key: the writer puts the
    source as text ('11') and the destination as a number (13)."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"TransferPlan {week}: tank cell {value!r} is not a "
                         f"tank id")
    if isinstance(value, numbers.Integral):
        return str(int(value))
    if isinstance(value, numbers.Real):
        if not float(value).is_integer():
            raise ValueError(f"TransferPlan {week}: tank {value!r} is not a "
                             f"whole tank id")
        return str(int(value))
    text = str(value).strip()
    if not text:
        raise ValueError(f"TransferPlan {week}: a Transfer row has a blank tank")
    return text


def _iso_weeks_in(year: int) -> int:
    return dt.date(year, 12, 28).isocalendar()[1]


def last_complete_year(start, horizon_weeks: int) -> int:
    """The last ISO label-year the horizon covers in full.

    104 weeks from 2027-01-04 -> 2028; 156 weeks -> 2029. Raises when the
    horizon holds no complete year: a partial year would be read as if it
    were one.
    """
    d = _as_date(start, "start")
    if (isinstance(horizon_weeks, bool)
            or not isinstance(horizon_weeks, numbers.Integral)
            or horizon_weeks < 1):
        raise ValueError(f"horizon_weeks must be a positive whole number of "
                         f"weeks, got {horizon_weeks!r}")
    per_year = Counter(_label_year(w) for w in _horizon_labels(d, horizon_weeks))
    full = [y for y, n in per_year.items() if n == _iso_weeks_in(y)]
    if not full:
        raise ValueError(
            f"a {horizon_weeks}-week horizon from {d} holds no complete year "
            f"(weeks per year: {dict(sorted(per_year.items()))}); lengthen it "
            f"or pass years= explicitly")
    return max(full)


FEED_SHEET = "FeedForecastWeekly"
_WEEK_LABEL_RE = re.compile(r"^\d{4}-W\d{2}$")


def _feed_by_type_years(wb, years, from_label=None) -> Optional[dict]:
    """{year: {feed type: kg}} from the FeedForecastWeekly matrix, or None.

    The sheet (excel_io.write_feed_forecast_weekly): a header row
    `Feed Type, Max Size (g), <ISO week labels>`, a row of week starts (blank
    first cell), one row per feed type, then `Total (kg)`. A year's figure
    sums the week columns whose label-year is that year and whose label is
    on or after `from_label` (the run's first horizon week; None = every
    week). The cells are written rounded to whole kg, so a year carries up
    to 0.5 kg of rounding per type-week.

    TOLERANT, it never raises for the sheet's content: None when the sheet
    is missing or not the shape above, and a year with no week column in the
    sheet maps to None. None means "not read", which forecast.costs.year_cost
    refuses loudly; it is never a zero."""
    if FEED_SHEET not in wb.sheetnames:
        return None
    want = {int(y) for y in years}
    cols: dict = {}
    acc: dict = {}
    header = False
    for r in wb[FEED_SHEET].iter_rows(values_only=True):
        if not header:
            if r and r[0] == "Feed Type":
                header = True
                for i, lab in enumerate(r[2:], start=2):
                    if lab is None:
                        continue
                    s = str(lab)
                    if not _WEEK_LABEL_RE.match(s):
                        return None
                    y = _label_year(s)
                    if y in want and (from_label is None or s >= from_label):
                        cols[i] = y
            continue
        name = r[0] if r else None
        if (not isinstance(name, str) or not name.strip()
                or name.strip().startswith("Total")):
            continue                  # the week-start row and the Total row
        for i, y in cols.items():
            v = r[i] if i < len(r) else None
            if (isinstance(v, bool) or not isinstance(v, numbers.Real)
                    or not math.isfinite(v) or v < 0):
                return None
            d = acc.setdefault(y, {})
            d[name] = d.get(name, 0.0) + float(v)
    if not header:
        return None
    covered = set(cols.values())
    return {y: (acc.get(y, {}) if y in covered else None)
            for y in sorted(want)}


def eggs_by_year(batches, start, years, *, end=None) -> dict:
    """{year: eggs stocked} for each label-year in `years`.

    A batch counts its BatchInput.input_count (the egg count the EGG stage
    starts from) in the label-year of the ISO week its input_date falls in,
    when start <= input_date (< end when `end` is given). Batches stocked
    before the start are not the run's spend. A year with none is 0: that
    is a count, not a default."""
    s = _as_date(start, "start")
    e = _as_date(end, "end") if end is not None else None
    out = {y: 0.0 for y in sorted({int(y) for y in years})}
    for b in batches:
        d = _as_date(b.input_date, f"batch {b.batch_id} input_date")
        if d < s or (e is not None and d >= e):
            continue
        y = _label_year(_week_label(d))
        if y not in out:
            continue
        n = b.input_count
        if (isinstance(n, bool) or not isinstance(n, numbers.Real)
                or not math.isfinite(n) or n < 0):
            raise ValueError(f"batch {b.batch_id}: input_count {n!r} is not "
                             f"a number of eggs")
        out[y] += float(n)
    return out


def read_workbook(path, control, facility, bands, cv_pct: float,
                  hog_yield: float, years: Sequence[int], *,
                  facility_limits=None, start=None) -> dict:
    """Read an engine workbook into YearReads + layout + audits.

    `start` (the run's forecast start) limits the feed cost driver
    (YearRead.feed_kg_by_type, from FeedForecastWeekly) to weeks on or after
    the start's ISO week; None reads every week. Nothing else uses it.

    `facility_limits` (scenario_io.load_limits(...)[0]) feeds
    `caps.resolve_facility_cap`, the engine's own per-week rule for the
    biomass cap and the harvest floor; without it both come from Control. A
    resolved value of 0 means no cap / no floor that week, as in the engine.
    -> dict(years, layout_week, layout, tank_sequence, audits, validation_top,
            weekly); `weekly` maps each read week to its standing_kg,
            harvest_fish, moves, cap_kg and floor (None = none applies).
    """
    years = sorted({int(y) for y in years})
    if not years:
        raise ValueError("no years to read")
    limits = facility_limits if facility_limits is not None else FacilityLimits()
    from_label = (_week_label(_as_date(start, "start"))
                  if start is not None else None)
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        feed = _feed_by_type_years(wb, years, from_label)
        bh, bl = _table(wb, "BatchLocations", "Week")
        hh, hp = _table(wb, "HarvestPlan", "Week")
        th, tp = _table(wb, "TransferPlan", "Week")
        _p, bp = _table(wb, "BiologyProjection", "Batch")
        _r, rr = _table(wb, "ReconciliationReport", "Week")
        _c, tc = _table(wb, "TankContinuityAudit", "Week")
        _v, vl = _table(wb, "ValidationLog", "#")
        sh, sl = _table(wb, "SystemLimitsAudit", "Week")
        ic_lines, ic_status = _input_conservation(wb)
    finally:
        wb.close()
    # The engine's own per-system verdicts (excel_io's SystemLimitsAudit):
    # a flag = above the system limit x (1 + global_buffer_pct).
    s_cols = {c: _col(sh, c, "SystemLimitsAudit")
              for c in ("System", "Biomass_kg", "Biomass_cap", "Bio_flag",
                        "Feed_kg_day", "Feed_cap", "Feed_flag")}
    sys_over = {"biomass": Counter(), "feed": Counter()}
    sys_worst: dict = {}
    for r in sl:
        w = str(r["Week"])
        y = _label_year(w)
        for kind, flag, val_c, cap_c, unit in (
                ("biomass", "Bio_flag", "Biomass_kg", "Biomass_cap", "kg"),
                ("feed", "Feed_flag", "Feed_kg_day", "Feed_cap", "kg/day")):
            if not r.get(s_cols[flag]):
                continue
            sys_over[kind][y] += 1
            val = float(r.get(s_cols[val_c]) or 0.0)
            cap = float(r.get(s_cols[cap_c]) or 0.0)
            ratio = val / cap if cap > 0 else math.inf
            cur = sys_worst.get(y)
            if cur is None or (ratio, w, str(r[s_cols["System"]])) > (
                    cur["ratio"], cur["week"], cur["system"]):
                sys_worst[y] = dict(system=str(r[s_cols["System"]]), week=w,
                                    kind=kind, value=val, cap=cap, unit=unit,
                                    ratio=ratio)
    c_cnt = _col(bh, "Count", "BatchLocations")
    c_bio = _col(bh, "Biomass", "BatchLocations")
    c_den = _col(bh, "Density", "BatchLocations")
    h_cnt = _col(hh, "Count", "HarvestPlan")
    h_kg = _col(hh, "Gross_Biomass", "HarvestPlan")

    og = {t.tank_id: t for t in facility.tanks if t.type == "OG"}
    systems = sorted({t.system_id for t in og.values()})
    sys_n = Counter(t.system_id for t in og.values())

    located = sorted((r for r in bl if (r[c_cnt] or 0) > 0),
                     key=lambda r: (str(r["Week"]), str(r["System"]),
                                    r["Tank"], str(r["Batch"])))
    og_kg = defaultdict(float)
    occ = defaultdict(set)                   # (week, system) -> tank ids
    den_max = defaultdict(float)             # (year, system) -> raw density
    over = defaultdict(list)                 # year -> over-cap rows
    non_og = 0
    for r in located:
        t = og.get(r["Tank"])
        if t is None:
            non_og += 1
            continue
        w = str(r["Week"])
        dens = float(r[c_den] or 0.0)
        og_kg[w] += float(r[c_bio] or 0.0)
        occ[(w, t.system_id)].add(t.tank_id)
        key = (_label_year(w), t.system_id)
        den_max[key] = max(den_max[key], dens)
        stage = str(r["Stage"] or "")
        # The engine's R8 rule, judged the way the R8 audit judges it.
        cap = effective_density_cap(
            t.max_density_kg_m3, t.system_id, stage,
            is_purge_mode(control, _day(r["Week_Start"])),
            harvest_prep_cap=HARVEST_PREP_DENSITY_CAP)
        if dens > cap:
            over[_label_year(w)].append(
                (dens / cap, w, t.tank_id, str(r["Batch"]), dens, cap,
                 t.system_id, stage))

    fw_kg = defaultdict(float)
    for r in bp:
        if r.get("Stage") in ("FW", "EGG"):
            fw_kg[str(r["Week"])] += float(r.get("Biomass_kg") or 0.0)
    hv_n, hv_kg, harvests = defaultdict(float), defaultdict(float), []
    for r in hp:
        w, n, kg = str(r["Week"]), float(r[h_cnt] or 0.0), float(r[h_kg] or 0.0)
        hv_n[w] += n
        hv_kg[w] += kg
        if n > 0:
            harvests.append((w, _Harvest(n, kg * 1000.0 / n)))
    moves = _moves_per_week(tp, _col(th, "From_Tank", "TransferPlan"),
                            _col(th, "To_Tank", "TransferPlan"),
                            _col(th, "Count", "TransferPlan"))
    weeks_all = sorted({str(r["Week"]) for r in bl}
                       | {str(r["Week"]) for r in bp}
                       | {str(r["Week"]) for r in hp})
    if not weeks_all:
        raise ValueError(f"{path}: the workbook has no weeks at all")

    def _standing(w) -> float:
        return og_kg.get(w, 0.0) + fw_kg.get(w, 0.0)

    reads, weekly = {}, {}
    for y in years:
        W = [w for w in weeks_all if _label_year(w) == y]
        if not W:
            raise ValueError(f"year {y} has no weeks in this workbook (it "
                             f"covers {weeks_all[0]}..{weeks_all[-1]})")
        fish = [hv_n.get(w, 0.0) for w in W]
        # None = the engine applies no floor / no cap that week.
        floors = [resolve_facility_cap(METRIC_MIN_HARVEST, w, limits, control)
                  for w in W]
        standing = [_standing(w) for w in W]
        prev = [w for w in weeks_all if _label_year(w) == y - 1]
        standing_before = _standing(prev[-1]) if prev else standing[0]
        caps = [resolve_facility_cap(METRIC_BIOMASS, w, limits, control)
                for w in W]
        capped = [(s, float(c)) for s, c in zip(standing, caps) if c is not None]
        floored = [float(f) for f in floors if f is not None]
        for i, w in enumerate(W):
            weekly[w] = dict(standing_kg=standing[i], harvest_fish=fish[i],
                             moves=moves.get(w, 0), cap_kg=caps[i],
                             floor=floors[i])
        rev, hog_kg, n_priced, n_over = ideal.value_rows(
            [h for w, h in harvests if _label_year(w) == y],
            bands, cv_pct, hog_yield)
        fish_total = sum(fish)
        gross_kg = sum(hv_kg.get(w, 0.0) for w in W)
        occ_tot = [sum(len(occ.get((w, s), ())) for s in systems) for w in W]
        mv = [moves.get(w, 0) for w in W]
        ov = over[y]
        worst = max(ov) if ov else None
        reads[y] = YearRead(
            year=y, weeks=len(W),
            harvest_fish=fish_total,
            harvest_gross_t=gross_kg / 1000.0,
            hog_t=hog_kg / 1000.0,
            revenue=rev,
            avg_gross_kg=gross_kg / fish_total if fish_total else 0.0,
            share_over_8lb=n_over / n_priced if n_priced else 0.0,
            harvest_fish_wk_min=min(fish), harvest_fish_wk_max=max(fish),
            zero_weeks=sum(1 for x in fish if x <= 0),
            under_floor_weeks=sum(1 for x, f in zip(fish, floors)
                                  if f is not None and 0 < x < f),
            standing_peak_kg=max(standing),
            standing_mean_kg=statistics.mean(standing),
            peak_pct_of_cap=max((s / c for s, c in capped), default=0.0),
            og_tanks_mean=statistics.mean(occ_tot),
            og_tanks_max=max(occ_tot), og_tanks_total=len(og),
            r8_over_tank_weeks=len({(o[1], o[2]) for o in ov}),
            r8_worst=(dict(week=worst[1], tank=worst[2], batch=worst[3],
                           density=worst[4], cap=worst[5], system=worst[6],
                           stage=worst[7]) if worst else None),
            moves_mean=statistics.mean(mv), moves_max=max(mv),
            weeks_over_move_budget=sum(
                1 for x in mv if x > control.max_transfers_per_week),
            per_system={s: dict(
                mean_tanks=statistics.mean(len(occ.get((w, s), ())) for w in W),
                max_tanks=max(len(occ.get((w, s), ())) for w in W),
                n_tanks=sys_n[s],
                peak_density=den_max.get((y, s), 0.0)) for s in systems},
            floor_min=min(floored) if floored else None,
            floor_max=max(floored) if floored else None,
            capped_weeks=len(capped),
            sys_bio_over_weeks=sys_over["biomass"].get(y, 0),
            sys_feed_over_weeks=sys_over["feed"].get(y, 0),
            sys_worst=sys_worst.get(y),
            over_cap_weeks=sum(1 for s, c in capped
                               if s / c > 1.0 + CAP_EPSILON),
            gain_t=(gross_kg + standing[-1] - standing_before) / 1000.0,
            feed_kg_by_type=feed.get(y) if feed is not None else None,
            first_week=W[0],
            standing_end_kg=standing[-1],
        )

    # The layout: one mid-year week of the last read year, and every tank's
    # batch sequence across that year.
    last = years[-1]
    last_weeks = [w for w in weeks_all if _label_year(w) == last]
    layout_week = last_weeks[len(last_weeks) // 2]
    layout = {s: [] for s in systems}
    seq = {}
    for r in located:
        t = og.get(r["Tank"])
        w = str(r["Week"])
        if t is None or _label_year(w) != last:
            continue
        s = seq.setdefault((t.system_id, t.tank_id), [])
        if not s or s[-1] != str(r["Batch"]):
            s.append(str(r["Batch"]))
        if w == layout_week:
            layout[t.system_id].append(dict(
                tank=t.tank_id, batch=str(r["Batch"]),
                count=float(r[c_cnt] or 0.0),
                biomass_kg=float(r[c_bio] or 0.0),
                density=float(r[c_den] or 0.0), stage=str(r["Stage"] or "")))
    for rows in layout.values():
        rows.sort(key=lambda d: (d["tank"], d["batch"]))
    tank_sequence = {f"{s}-{t}": v for (s, t), v in sorted(seq.items())}

    vcat = defaultdict(Counter)
    for r in vl:
        m = re.search(r"(\d{4})-W\d{2}", str(r.get("Detail", "")))
        vcat[int(m.group(1)) if m else 0][str(r.get("Category"))] += 1
    validation_top = {
        y: sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[:_VALIDATION_TOP_N]
        for y, c in sorted(vcat.items())}

    audits = dict(
        reconciliation_flags=sum(1 for r in rr if r.get("Flag")),
        tank_continuity_flags=sum(1 for r in tc if r.get("Flag")),
        input_conservation=ic_lines,
        input_status=ic_status,
        non_og_location_rows=non_og,
    )
    return dict(years=reads, layout_week=layout_week, layout=layout,
                tank_sequence=tank_sequence, audits=audits,
                validation_top=validation_top, weekly=weekly)


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #
def _written_workbook(out_path) -> Optional[Path]:
    """The file the engine actually saved: run.py coerces the extension to
    match the workbook type (.xlsm when it kept VBA)."""
    base = Path(out_path)
    for p in (base.with_suffix(".xlsm"), base.with_suffix(".xlsx")):
        if p.is_file():
            return p
    return None


def run_schedule(batches: Sequence[BatchInput], project_dir, *, pr_path=None,
                 start=None, horizon_weeks: int = 104,
                 overrides: Optional[dict] = None,
                 method: str = DEFAULT_METHOD,
                 include_manual_events: bool = False,
                 years: Optional[Sequence[int]] = None,
                 keep_dir=None,
                 method_overrides: Optional[dict] = None,
                 density_overrides: Optional[dict] = None,
                 system_overrides: Optional[dict] = None) -> EngineRun:
    """Run the real engine on `batches` and read what it wrote.

    `years` defaults to the last complete year of the horizon on an empty
    start (`last_complete_year`: start.year + 1 at 104 weeks, + 2 at 156),
    or every year the horizon touches on a real PR. `keep_dir` (empty or
    absent) keeps the workbook, run.log and the exact config/ + scenario/ +
    PR it ran from. `audits['manual_events_file']` names the manual-events
    file the run used (None when there was none). `method` is a registry key
    or AS_CONFIGURED; `method_overrides` carries a promoted plan's knobs (see
    `prepare`), so the run is the same one ▶ Run forecast would make.
    `density_overrides` / `system_overrides` are the system-constraint
    what-ifs (see `prepare`); the read is judged against them, because it
    loads the run's own config/ and scenario/ copy.
    """
    m = _method(method)
    kd = Path(keep_dir) if keep_dir is not None else None
    if kd is not None and kd.exists() and any(kd.iterdir()):
        raise ValueError(f"keep_dir {kd} is not empty; give an empty or new "
                         f"directory so one run's files never mix with another's")
    batches = list(batches)          # read twice: prepare, then the egg count
    work = tempfile.mkdtemp(prefix="ideal_engine_")
    try:
        prep = prepare(work, batches, project_dir, start=start,
                       horizon_weeks=horizon_weeks, overrides=overrides,
                       pr_path=pr_path,
                       include_manual_events=include_manual_events,
                       method_overrides=method_overrides,
                       density_overrides=density_overrides,
                       system_overrides=system_overrides)
        start_d = prep["start"]
        horizon = _horizon_labels(start_d, horizon_weeks)
        span = sorted({_label_year(w) for w in horizon})
        if years is None:
            years = ([last_complete_year(start_d, horizon_weeks)]
                     if pr_path is None else span)
        years = sorted({int(y) for y in years})
        outside = [y for y in years if y not in span]
        if not years or outside:
            raise ValueError(f"year(s) {outside or years} are outside the run "
                             f"horizon {horizon[0]}..{horizon[-1]}")

        control, _tables, facility = load_config(prep["config_dir"])
        flimits, slimits = sio.load_limits(prep["scenario_dir"], control)
        in_h = set(horizon)
        limit_rows = (sum(1 for (w, _m) in flimits.overrides if w in in_h)
                      + sum(1 for (w, _s, _m) in slimits.caps if w in in_h))
        with open(Path(prep["config_dir"]) / "economics.yaml",
                  encoding="utf-8") as f:
            economics = yaml.safe_load(f)

        out = Path(work) / "out.xlsx"
        log = io.StringIO()
        t0 = time.perf_counter()
        with contextlib.redirect_stdout(log):
            rc, _el = run_method(m, prep["pr_path"], str(out),
                                 prep["config_dir"], prep["scenario_dir"],
                                 quiet=False)
        elapsed = time.perf_counter() - t0
        written = _written_workbook(out)
        if written is None:
            tail = "\n".join(log.getvalue().splitlines()[-_LOG_TAIL_LINES:])
            raise RuntimeError(f"the engine ({method}) returned rc={rc} and "
                               f"wrote no workbook. Last log lines:\n{tail}")
        got = read_workbook(written, control, facility,
                            ideal.price_bands(economics),
                            float(economics.get("model_cv_pct", 18.0)),
                            float(control.default_hog_yield), years,
                            facility_limits=flimits, start=start_d)
        # Cost DRIVER: eggs stocked, from the batches the run actually kept
        # (prepare drops those already in OG at an empty start), inside the
        # horizon. The workbook does not carry it, so it is added here.
        gone = set(prep["dropped"])
        eggs = eggs_by_year(
            [b for b in batches if b.batch_id not in gone], start_d, years,
            end=start_d + dt.timedelta(weeks=int(horizon_weeks)))
        years_read = {y: _dc_replace(r, eggs=eggs[y])
                      for y, r in got["years"].items()}
        audits = dict(got["audits"],
                      manual_events_included=bool(include_manual_events),
                      manual_events_file=prep["manual_events_file"],
                      # the budget weeks_over_move_budget was COUNTED against:
                      # the run's own kept control, promoted knobs included
                      move_budget=int(control.max_transfers_per_week))

        out_path = None
        if kd is not None:
            kd.mkdir(parents=True, exist_ok=True)
            shutil.copy2(written, kd / written.name)
            _copy_tree(Path(prep["config_dir"]), kd / "config")
            _copy_tree(Path(prep["scenario_dir"]), kd / "scenario")
            shutil.copy2(prep["pr_path"], kd / Path(prep["pr_path"]).name)
            (kd / "run.log").write_text(log.getvalue(), encoding="utf-8")
            out_path = str(kd / written.name)
        return EngineRun(
            rc=rc, elapsed_s=elapsed, method=method, start=start_d,
            horizon_weeks=int(horizon_weeks), overrides=prep["overrides"],
            dropped_batches=tuple(prep["dropped"]), years=years_read,
            layout_week=got["layout_week"], layout=got["layout"],
            tank_sequence=got["tank_sequence"], audits=audits,
            validation_top=got["validation_top"],
            limit_rows_in_horizon=limit_rows, out_path=out_path,
            method_overrides=dict(prep["method_overrides"]),
            density_overrides=dict(prep["density_overrides"]),
            system_overrides={s: dict(v) for s, v
                              in prep["system_overrides"].items()})
    finally:
        _remove_tree(work)


def ideal_run(cadence_days: int, batch_size: int, cap_kg: float, project_dir,
              *, template=None, horizon_weeks: int = ideal.HORIZON_WEEKS,
              overrides: Optional[dict] = None, method: str = DEFAULT_METHOD,
              keep_dir=None,
              years: Optional[Sequence[int]] = None,
              method_overrides: Optional[dict] = None,
              density_overrides: Optional[dict] = None,
              system_overrides: Optional[dict] = None) -> EngineRun:
    """One Ideal cell through the real engine, from an empty facility.

    Same synthetic stream as `ideal.evaluate`, 6N in production from Jan 1 of
    the year before the start, and `cap_kg` as the facility biomass cap.
    `template` is a BatchInput, a batch id from the scenario, or None for
    `ideal.default_template`. The default 156 weeks put the read year
    (`years` None -> the last complete year, `last_complete_year`) on the
    same steady year-3 window L1 is judged on.
    """
    for name, v in (("cadence_days", cadence_days),
                    ("batch_size", batch_size)):
        if (isinstance(v, bool) or not isinstance(v, numbers.Integral)
                or not v > 0):
            raise ValueError(f"{name} must be a positive whole number, got "
                             f"{v!r}")
    if (isinstance(cap_kg, bool) or not isinstance(cap_kg, numbers.Real)
            or not math.isfinite(cap_kg) or not cap_kg > 0):
        raise ValueError(f"cap_kg must be a positive number, got {cap_kg!r}")
    if template is None or isinstance(template, str):
        batches = sio.load_batches(str(Path(project_dir) / "scenario"))
        if template is None:
            template = ideal.default_template(batches)
        else:
            found = [b for b in batches if b.batch_id == template]
            if not found:
                raise ValueError(f"template batch {template!r} not in scenario")
            template = found[0]
    extra = dict(overrides or {})
    if "max_biomass_kg" in extra and extra["max_biomass_kg"] != cap_kg:
        raise ValueError(f"the cap is given twice (cap_kg={cap_kg} and "
                         f"overrides max_biomass_kg={extra['max_biomass_kg']})")
    start = ideal.STEADY_START
    ov = {"sixn_production_start": dt.date(start.year - 1, 1, 1).isoformat(),
          "max_biomass_kg": float(cap_kg),
          "scenario_name": (f"Ideal {int(cadence_days)}d x "
                            f"{int(batch_size) // 1000}k at "
                            f"{cap_kg / 1000:,.0f} t")}
    ov.update(extra)
    stream = ideal.synthetic_stream(template, int(cadence_days),
                                    int(batch_size),
                                    horizon_weeks=horizon_weeks, start=start)
    return run_schedule(stream, project_dir, start=start,
                        horizon_weeks=horizon_weeks, overrides=ov,
                        method=method, years=years, keep_dir=keep_dir,
                        method_overrides=method_overrides,
                        density_overrides=density_overrides,
                        system_overrides=system_overrides)


# --------------------------------------------------------------------------- #
# Plausibility
# --------------------------------------------------------------------------- #
def _gate(name: str, bad: bool, status: str, bad_detail: str,
          ok_detail: str) -> Gate:
    return Gate(name, status if bad else "PASS", bad_detail if bad else ok_detail)


def gates(run: EngineRun, year: int, control) -> list[Gate]:
    """The plausibility checks for one read year, in a fixed order.

    Hard limits (operator ruling 2026-09-10): a plan is within the limits iff
    no gate FAILs (see `plausible`). Every facility limit FAILs when broken,
    zero breaches allowed: "Harvest every week", "Harvest floor", "Biomass
    cap" (peak standing above the resolved cap by more than CAP_EPSILON; no
    tolerance here, ideal.PEAK_TOLERANCE belongs to the tankless quick scan),
    "Tank density (R8)", "System limits" (flagged above the limit x (1 +
    global_buffer_pct), exactly as the engine's SystemLimitsAudit flags them)
    and "Handling budget". The engine and audit gates FAIL too. Only "FW mass
    balance" is a WARN: a model-accounting note, not a facility limit (an
    empty start trips it on one early batch by construction).
    """
    if year not in run.years:
        raise ValueError(f"year {year} was not read in this run (read: "
                         f"{sorted(run.years)})")
    y = run.years[year]
    a = run.audits
    w = y.r8_worst
    lines = list(a.get("input_conservation", ()))
    breach = [ln for ln in lines if ln.startswith("***")
              and ("DROPPED" in ln or "OVER-PRODUCED" in ln)]
    n_dropped = a.get("input_status", {}).get("*** DROPPED ***", 0)
    fw = [ln for ln in lines if "FW MASS-BALANCE BREACH" in ln]
    rec, cont = a["reconciliation_flags"], a["tank_continuity_flags"]
    if y.floor_min is None:
        floor_s = "no harvest floor applies"
    elif y.floor_min == y.floor_max:
        floor_s = f"the resolved floor is {y.floor_min:,.0f} fish/week"
    else:
        floor_s = (f"the resolved floor runs {y.floor_min:,.0f}-"
                   f"{y.floor_max:,.0f} fish/week")
    sw = y.sys_worst
    sys_buf = float(getattr(control, "global_buffer_pct", 0.0) or 0.0)
    # The budget weeks_over_move_budget was COUNTED against (recorded by
    # run_schedule from the run's own control); a run without that record
    # falls back to its what-if override, then to the caller's control.
    budget = (run.audits or {}).get(
        "move_budget", (run.overrides or {}).get(
            "max_transfers_per_week", control.max_transfers_per_week))
    no_cap = y.capped_weeks == 0
    cap_ok = (f"no biomass cap applies in any of the {y.weeks} weeks (peak "
              f"standing {y.standing_peak_kg / 1000:,.0f} t)" if no_cap else
              f"peak standing {y.standing_peak_kg / 1000:,.0f} t is "
              f"{y.peak_pct_of_cap:.1%} of the cap")
    return [
        _gate("Engine finished", run.rc != 0, "FAIL",
              f"the engine returned rc {run.rc}", "rc 0"),
        _gate("Harvest every week", y.zero_weeks > 0, "FAIL",
              f"{y.zero_weeks} of {y.weeks} weeks harvest nothing",
              f"all {y.weeks} weeks harvest (min "
              f"{y.harvest_fish_wk_min:,.0f} fish)"),
        _gate("Harvest floor", y.under_floor_weeks > 0, "FAIL",
              f"{y.under_floor_weeks} weeks under the floor; {floor_s} (min "
              f"harvest {y.harvest_fish_wk_min:,.0f})",
              f"every week at or above the floor; {floor_s} (min harvest "
              f"{y.harvest_fish_wk_min:,.0f})"),
        _gate("Biomass cap", y.peak_pct_of_cap > 1.0 + CAP_EPSILON, "FAIL",
              f"peak standing {y.standing_peak_kg / 1000:,.0f} t is "
              f"{y.peak_pct_of_cap:.1%} of the cap; {y.over_cap_weeks} "
              f"week(s) over it (a hard limit: no tolerance in the engine)",
              cap_ok),
        _gate("Tank density (R8)", y.r8_over_tank_weeks > 0, "FAIL",
              (f"{y.r8_over_tank_weeks} tank-weeks over their cap; worst tank "
               f"{w['tank']} ({w['system']}) {w['week']}: {w['density']:.1f} vs "
               f"{w['cap']:.0f} kg/m3 ({w['stage']})") if w else "",
              "no tank over its density cap"),
        _gate("System limits",
              y.sys_bio_over_weeks + y.sys_feed_over_weeks > 0, "FAIL",
              (f"{y.sys_bio_over_weeks} system-weeks over their biomass limit "
               f"and {y.sys_feed_over_weeks} over their feed limit (flagged "
               f"above the limit +{sys_buf:.0%}); worst {sw['system']} "
               f"{sw['week']}: {sw['kind']} {sw['value']:,.0f} vs "
               f"{sw['cap']:,.0f} {sw['unit']}") if sw else "",
              f"every system within its biomass and feed limits (flagged "
              f"above the limit +{sys_buf:.0%})"),
        _gate("Handling budget", y.weeks_over_move_budget > 0, "FAIL",
              f"{y.weeks_over_move_budget} weeks over "
              f"{budget} moves (max {y.moves_max})",
              f"at most {y.moves_max} moves a week (budget {budget})"),
        _gate("Conservation audits", rec > 0 or cont > 0, "FAIL",
              f"ReconciliationReport {rec} flag(s), TankContinuityAudit "
              f"{cont} flag(s)", "0 reconciliation, 0 tank-continuity flags"),
        _gate("Input conservation", bool(breach) or n_dropped > 0, "FAIL",
              " | ".join(breach) or f"{n_dropped} batch(es) DROPPED",
              "every in-horizon batch placed, none over-produced"),
        # A model-accounting note, not a facility limit: stays a WARN.
        _gate("FW mass balance", bool(fw), "WARN", " | ".join(fw),
              "FW phase conserves for every batch"),
    ]


def plausible(gate_list: Sequence[Gate]) -> bool:
    """Hard limits (operator ruling 2026-09-10): a plan is within the limits
    iff no gate FAILs. A WARN (only "FW mass balance") never decides."""
    return not any(g.status == "FAIL" for g in gate_list)
