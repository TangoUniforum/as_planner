"""Cap resolution.

Three cap levels (DESIGN §6):

- **Tank**: density × volume per tank (from FacilityConfig). No buffer.
- **System**: per-system per-metric, stated ONCE as a default and
  overridden per week only where a genuine one-off applies (see
  "System cap precedence" below). Buffered by Control R29
  `Global buffer` (5% default).
- **Facility**: per-week per-metric. Default from Control; per-week
  override from FacilityLimits (blank cell → use Control default).
  Buffered by Control R24 `Target Biomass deviation` (±1% default) on
  biomass and feed. Harvest count caps + HOG yield are strict.

Buffers are symmetric (cap × (1 ± buffer_pct)).

System cap precedence
---------------------
A capacity is a FACT ABOUT THE FACILITY: it changes rarely, and when it
changes the operator wants to change ONE number. So it is stated once as
a default and only overridden per week where a genuine one-off applies.
`SystemLimits.resolve` walks exactly four steps, highest first:

  1. **per-week row**   `caps[(week, system, metric)]`
     — a genuine one-off week (maintenance, a trial, a shutdown).
  2. **system+mode default** `mode_defaults[(system, mode, metric)]`
     — the same system has a different capacity in a different operating
     MODE. Only 6N has one today: it holds more while it is a depuration
     station than it does once its 3 mains become grow-out. The mode of a
     week is DERIVED from Control (`sixn_growth` + `sixn_production_start`)
     by `mode_for_week`, so the split cannot drift away from the date that
     defines it.
  3. **system default** `defaults[(system, metric)]`
     — the ordinary answer, and the only one most systems ever need.
  4. **absent** → `None`. A cap nobody set stays unset; callers that
     require one (`require_system_cap`) raise naming the missing input
     rather than inventing a number. No capacity figure lives in code.

System-id naming:
- FacilityConfig identifies systems as `OG1N`, `OG1S`, ..., `OG6S`.
- The legacy Excel SystemLimits sheet labelled them `1N` ... `6S` (no `OG`
  prefix); `tools/vba_to_config.py` normalizes on import.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from .models import ControlParams
from .time_grid import iso_week_label, label_for_date


# Canonical metric tokens.
METRIC_BIOMASS = "biomass"
METRIC_FEED_DAY = "feed_per_day"
METRIC_MAX_HARVEST = "max_harvest_per_week"
METRIC_MIN_HARVEST = "min_harvest_per_week"
METRIC_HOG_YIELD = "hog_yield"
# Per-week OG (seawater) growth factor. NOT a cap — a multiplier the operator
# sets for weeks the site knows it cannot achieve the modelled growth (0.90 =
# "90% of expected this week"). Absent week = 1.0. Deliberately resolved by
# `og_sgr_factors` below rather than `resolve_facility_cap`: a cap treats 0 as
# "unset", but 0 growth is a legitimate thing to ask for here.
METRIC_SGR_OG = "sgr_correction_og"

# Operating modes a system-default may be qualified by. Today only 6N has
# two; the dimension is general so a future mode (a system taken off-line
# seasonally, say) is a data change and not a code change.
MODE_PURGE = "purge"
MODE_PRODUCTION = "production"
SYSTEM_MODES = (MODE_PURGE, MODE_PRODUCTION)


# ============================================================
# Data containers
# ============================================================

@dataclass
class FacilityLimits:
    """Per-week per-metric facility-level overrides.

    Keys are (week_label, metric) where week_label is the ISO label
    like "2026-W20". Blank cell on the sheet → use Control default
    (resolution below).
    """
    overrides: dict[tuple[str, str], float] = field(default_factory=dict)

    def get(self, week_label: str, metric: str, default: float) -> float:
        return self.overrides.get((week_label, metric), default)


def week_label_start(week_label: str) -> Optional[date]:
    """Monday of an absolute ISO week label ('2028-W01' → 2028-01-03).

    Returns None for anything that is not an absolute ISO label, so a
    caller can fall back rather than crash on a stray key.
    """
    s = (week_label or "").strip().upper()
    if "-W" not in s:
        return None
    y, _, w = s.partition("-W")
    try:
        return date.fromisocalendar(int(y), int(w), 1)
    except (ValueError, TypeError):
        return None


@dataclass
class SystemLimits:
    """Per-system capacity caps: defaults, mode defaults, per-week exceptions.

    Three layers, resolved by `resolve` in the order documented at the top
    of this module (per-week row > system+mode default > system default >
    absent):

    - `defaults[(system_id, metric)]` — the standing capacity. One entry
      per system per metric; this is what the operator edits.
    - `mode_defaults[(system_id, mode, metric)]` — the capacity while that
      system is in a particular operating mode (see `MODE_*`).
    - `caps[(week_label, system_id, metric)]` — a genuine one-off week.

    Mode binding
    ------------
    A mode default is meaningless without the Control fields that say WHICH
    mode a given week is in, so `mode_defaults` must be bound to Control
    (`bind_sixn_mode`) before any week can be resolved. Resolving an
    unbound object that carries mode defaults RAISES rather than guessing a
    mode — silently picking one would apply the wrong ceiling to half the
    horizon and look like a planner defect. Objects with no mode defaults
    need no binding.
    """
    caps: dict[tuple[str, str, str], float] = field(default_factory=dict)
    defaults: dict[tuple[str, str], float] = field(default_factory=dict)
    mode_defaults: dict[tuple[str, str, str], float] = field(default_factory=dict)
    # Control fields that decide a week's mode; set by `bind_sixn_mode`.
    sixn_growth: bool = False
    sixn_production_start: Optional[date] = None
    mode_bound: bool = False

    # ---- mode binding -------------------------------------------------

    def bind_sixn_mode(self, control) -> "SystemLimits":
        """Attach the Control fields that decide each week's 6N mode.

        Mutates and returns self so it composes with `load_limits`. Call
        this once, wherever Control is loaded next to the scenario.
        """
        self.sixn_growth = bool(getattr(control, "sixn_growth", False))
        self.sixn_production_start = getattr(control, "sixn_production_start", None)
        self.mode_bound = True
        return self

    def mode_for_week(self, week_label: str) -> str:
        """Operating mode of `week_label` — MODE_PURGE or MODE_PRODUCTION.

        Delegates the rule to `sixn.purge_mode_on` so this and the engine's
        own 6N phase machine can never disagree about where the boundary
        is. The week is represented by its ISO Monday: a week is in
        production mode once its first day is on/after
        `sixn_production_start`.
        """
        from .sixn import purge_mode_on
        d = week_label_start(week_label)
        if d is None:
            # Not an absolute ISO label — treat as the pre-transition mode
            # (what a horizon with no dates could only be).
            return MODE_PURGE
        return (MODE_PURGE
                if purge_mode_on(self.sixn_growth, self.sixn_production_start, d)
                else MODE_PRODUCTION)

    # ---- resolution ---------------------------------------------------

    def resolve(self, week_label: str, system_id: str,
                metric: str) -> Optional[float]:
        """The one place a system cap is decided. See module docstring."""
        v = self.caps.get((week_label, system_id, metric))
        if v is not None:
            return v
        if self.mode_defaults:
            if not self.mode_bound:
                systems = sorted({s for (s, _m, _k) in self.mode_defaults})
                raise ValueError(
                    "System caps carry mode-specific defaults "
                    f"({', '.join(systems)}) but this SystemLimits was never "
                    "bound to Control, so the operating mode of week "
                    f"{week_label} is unknown. Load it with "
                    "`load_limits(scenario_dir, control)` (or call "
                    "`.bind_sixn_mode(control)`) before resolving caps.")
            v = self.mode_defaults.get(
                (system_id, self.mode_for_week(week_label), metric))
            if v is not None:
                return v
        return self.defaults.get((system_id, metric))

    def get(self, week_label: str, system_id: str, metric: str) -> Optional[float]:
        """Alias of `resolve` (kept: callers say `.get`)."""
        return self.resolve(week_label, system_id, metric)

    def row(self, week_label: str, system_id: str, metric: str) -> Optional[float]:
        """The per-week EXCEPTION only, ignoring defaults. For editors."""
        return self.caps.get((week_label, system_id, metric))


# ============================================================
# Resolution helpers
# ============================================================

def resolve_facility_cap(
    metric: str,
    week_label: str,
    facility_limits: FacilityLimits,
    control: ControlParams,
) -> Optional[float]:
    """Return the resolved facility-cap value (override → Control default).

    Returns None if the metric isn't a facility cap or the default is 0.
    Buffer is NOT applied here — call `apply_facility_buffer` for the
    symmetric (lo, hi) tolerance band.
    """
    defaults = {
        METRIC_BIOMASS: control.max_biomass_kg,
        METRIC_FEED_DAY: control.max_feed_per_day_kg,
        METRIC_MAX_HARVEST: control.max_harvest_per_week,
        METRIC_MIN_HARVEST: control.min_harvest_per_week,
        METRIC_HOG_YIELD: control.default_hog_yield,
    }
    if metric not in defaults:
        return None
    val = facility_limits.get(week_label, metric, defaults[metric])
    return val if val > 0 else None


def override_coverage_gaps(facility_limits, control, week_labels):
    """Weeks in the horizon where a metric the operator IS steering per week
    has no row, so the week silently takes the Control default.

    DETECTION ONLY. This resolves nothing and changes no cap; it reports.
    `resolve_facility_cap` keeps falling back exactly as before, which is the
    correct behaviour -- an absent row genuinely means "use the default".

    The trap it names is narrower than "a week has no row". A metric with ZERO
    overrides is a deliberate choice: the default IS the operator's answer and
    there is nothing to warn about. A metric the operator has been setting week
    by week and then STOPS is the dangerous shape, because the weeks past the
    last row inherit a number nobody chose for them.

    Found on the 2026-08-31 PR: biomass and feed_per_day rows stopped at
    2026-W53 simply because that is where entry stopped, so everything from
    2027-W01 silently took the design/post-expansion defaults (34,000 kg/day
    against the 27,500 being entered). That was worth ~131 t of horizon
    production the plan did not have.

    Returns a list of dicts, one per affected metric, ordered by metric name:
    `metric`, `default`, `n_covered`, `n_missing`, `first_missing`,
    `first_covered`, `last_covered`, `missing_weeks` (capped at 12 for
    reporting), and the gap SHAPE -- `n_before` (horizon weeks earlier than the
    first row), `n_after` (later than the last) and `n_interior` (holes inside
    the covered span). The shape is the useful part and is what
    `coverage_gap_notes` reads: weeks AFTER the last row mean entry stopped,
    weeks BEFORE the first usually mean the rows start mid-horizon on purpose.
    """
    labels = [str(w) for w in (week_labels or [])]
    if not labels:
        return []
    in_horizon = set(labels)
    defaults = {
        METRIC_BIOMASS: control.max_biomass_kg,
        METRIC_FEED_DAY: control.max_feed_per_day_kg,
        METRIC_MAX_HARVEST: control.max_harvest_per_week,
        METRIC_MIN_HARVEST: control.min_harvest_per_week,
        METRIC_HOG_YIELD: control.default_hog_yield,
    }
    covered = {}
    for key in (getattr(facility_limits, "overrides", None) or {}):
        try:
            week, metric = key
        except (TypeError, ValueError):
            continue
        covered.setdefault(metric, set()).add(str(week))

    out = []
    for metric in sorted(covered):
        if metric not in defaults:
            continue                       # not a facility cap (e.g. the OG factor)
        have = covered[metric] & in_horizon
        if not have:
            continue                       # rows exist but none inside this horizon
        missing = [w for w in labels if w not in covered[metric]]
        if not missing:
            continue                       # fully covered -- the good case
        first_cov, last_cov = min(have), max(have)
        n_before = sum(1 for w in missing if w < first_cov)
        n_after = sum(1 for w in missing if w > last_cov)
        out.append({
            "metric": metric,
            "default": defaults[metric],
            "n_covered": len(have),
            "n_missing": len(missing),
            "first_missing": missing[0],
            "first_covered": first_cov,
            "last_covered": last_cov,
            "n_before": n_before,
            "n_after": n_after,
            "n_interior": len(missing) - n_before - n_after,
            "missing_weeks": missing[:12],
        })
    return out


def _fmt_cap(v):
    """A capacity an operator can read: `%g` renders 3800000.0 as `3.8e+06`,
    which is unreadable in a log line about kilograms of fish."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return format(f, ",.0f") if abs(f) >= 1000 else "%g" % f


def coverage_gap_notes(facility_limits, control, week_labels):
    """`override_coverage_gaps` rendered as ValidationLog lines.

    Empty list when every steered metric covers the horizon, so a clean run
    stays silent.
    """
    notes = []
    for g in override_coverage_gaps(facility_limits, control, week_labels):
        # WHERE the gap sits changes what it means, so say it. Weeks after the
        # last row is the dangerous shape (entry stopped); weeks before the
        # first row usually just means the rows start mid-horizon on purpose.
        shape = []
        if g["n_before"]:
            shape.append("%d before %s" % (g["n_before"], g["first_covered"]))
        if g["n_after"]:
            shape.append("%d after %s" % (g["n_after"], g["last_covered"]))
        if g["n_interior"]:
            shape.append("%d inside the covered span" % g["n_interior"])
        notes.append(
            "PER-WEEK COVERAGE - %s: rows cover %s..%s (%d of %d horizon "
            "week(s)); %s take the Control default %s. An absent row means "
            "\"use the default\", so this matters only if that default is not "
            "what you intend for those weeks - check it rather than assume it."
            % (g["metric"], g["first_covered"], g["last_covered"],
               g["n_covered"], g["n_covered"] + g["n_missing"],
               " and ".join(shape) or "%d week(s)" % g["n_missing"],
               _fmt_cap(g["default"])))
    return notes

def og_sgr_factors(facility_limits) -> dict[str, float]:
    """`{week_label: factor}` for the per-week OG growth factor.

    Layers on top of the growth curve and the batch's own `sgr_correction`
    (see `biology.sgr_pct_per_day`) — it does not replace either. Seawater
    only: freshwater has `fw_correction`, and this is an OG-tank input.

    Unlike a cap this is NOT buffered and 0 is NOT "unset": a week set to 0
    means no growth that week, which an operator may legitimately want.
    Negative values are dropped — there is no such thing as negative growth
    here, and silently treating one as a shrink factor would be inventing a
    model nobody asked for.
    """
    out: dict[str, float] = {}
    for key, val in (getattr(facility_limits, "overrides", None) or {}).items():
        try:
            week, metric = key
        except (TypeError, ValueError):
            continue
        if metric != METRIC_SGR_OG:
            continue
        try:
            f = float(val)
        except (TypeError, ValueError):
            continue
        if f < 0:
            continue
        out[str(week)] = f
    return out


def apply_facility_buffer(
    cap_value: float,
    metric: str,
    control: ControlParams,
) -> tuple[float, float]:
    """Symmetric ± tolerance band around a facility cap.

    Biomass and feed use Control R24 `Target Biomass deviation`.
    Harvest counts and HOG yield are strict (returned as (cap, cap)).
    """
    if metric in (METRIC_BIOMASS, METRIC_FEED_DAY):
        b = control.facility_biomass_deviation_pct  # R24 ± deviation
        return (cap_value * (1.0 - b), cap_value * (1.0 + b))
    return (cap_value, cap_value)


def predictive_move_in_count(
    total_biomass: float,
    growth_kg_week: float,
    setpoint: Optional[float],
    harvest_avg_wt_g: float,
    weekly_min: float,
    weekly_max: float,
    gain: float = 0.5,
    arrivals_kg: float = 0.0,
) -> float:
    """Forward-looking 6N purge move-in sizing (smooth proportional form).

    Fish moved into the 6N purge now are harvested after the purge lead time
    (the pair rotation). The move-in must keep biomass at the setpoint. In
    steady state that means replacing exactly one week's growth (which drains
    `lead` weeks later); deviations from setpoint are corrected by a *damped*
    proportional term::

        move_in_mass = growth_kg_week           # steady-state feed-forward
                       + gain * (biomass - setpoint)   # damped correction
                       + arrivals_kg            # known TranOG disturbance
        count        = move_in_mass * 1000 / harvest_avg_wt_g

    clipped to ``[weekly_min, weekly_max]``.

    Why `gain < 1` and NOT the instantaneous pipeline contents: an earlier
    form sized the move-in by subtracting the *current* pair biomass (a
    deadbeat projection to the drain week). Because the pipeline IS the thing
    being sized, that created a feedback loop — lumpy pairs → lumpy
    `committed` → lumpy move-in → lumpy pairs — that self-amplified into a
    30k↔55k bang-bang and cratered facility biomass on the big-drain weeks.
    Replacing the instantaneous pipeline term with its smooth steady-state
    expectation (one week's growth) and damping the correction breaks that
    loop, so the pipeline runs at a steady ~growth-replacement rate.

    `arrivals_kg` is the biomass scheduled to enter OG via TranOG at the drain
    week — a KNOWN disturbance fed forward so the move-in pre-draws biomass
    down before the batch lands. `harvest_avg_wt_g` converts the target mass
    to a fish count (purged fish do not grow, so their harvest weight ≈ now).
    """
    if harvest_avg_wt_g <= 0 or setpoint is None:
        # No biomass setpoint / no harvestable weight → operational floor.
        return weekly_min

    move_in_mass = (max(0.0, growth_kg_week)
                    + gain * (total_biomass - setpoint)
                    + max(0.0, arrivals_kg))
    count = move_in_mass * 1000.0 / harvest_avg_wt_g
    return max(weekly_min, min(weekly_max, count))


def resolve_system_cap(
    metric: str,
    week_label: str,
    system_id: str,
    system_limits: SystemLimits,
) -> Optional[float]:
    """Return the resolved system cap or None if no cap is set.

    Precedence: per-week row > system+mode default > system default >
    absent (module docstring).
    """
    return system_limits.resolve(week_label, system_id, metric)


def require_system_cap(
    metric: str,
    week_label: str,
    system_id: str,
    system_limits: SystemLimits,
) -> float:
    """`resolve_system_cap`, but a missing cap RAISES with its address.

    Capacities are operator inputs. A planner that substitutes an invented
    ceiling for one the operator never set plans against a number nobody
    chose and that appears nowhere in the output — so the engines that need
    a hard bound call this instead, and the error names the exact input to
    add. Shared by the MILP and L3 placement layers so the two cannot drift
    into different notions of "missing".
    """
    v = system_limits.resolve(week_label, system_id, metric)
    if v is None:
        raise ValueError(
            f"No {metric} cap configured for system {system_id} in week "
            f"{week_label}. Set it in scenario/limits.yaml — normally once, "
            f"under system_defaults:\n"
            f"  system_defaults:\n    {system_id}:\n      {metric}: <value>\n"
            f"or, for this week only, as an exception row "
            f"{{week: {week_label}, system: {system_id}, metric: {metric}, "
            f"value: <value>}}. Capacities are operator inputs — this code "
            f"will not invent one.")
    return v


def carry_forward_cap_lookup(system_limits: SystemLimits):
    """Cap lookup for the REPORTING sweeps over realized weeks.

    Returns `f(week_label, system_id, metric) -> Optional[float]`, the same
    precedence as `resolve_system_cap` plus one compatibility clause:

      per-week row > system+mode default > system default
        > nearest per-week row carried forward > absent

    The carry-forward tail exists only for a limits file written in the
    ROW-ONLY format (every week materialized, no defaults). Those files
    stop at their last week, and the audit sweeps whatever weeks the plan
    actually realized — which can run past it — so the last stated cap is
    carried. It sits BELOW the defaults deliberately: once a system has a
    default, a one-off exception week must not leak its value into every
    later week, which is exactly what an unconditional carry-forward would
    do.

    Previously hand-inlined three times (excel_io.write_system_limits_audit,
    lns_placement._carry_forward_caps, placement's rebalancer) with the same
    body; the audit and the optimizer must report the identical number, so
    there is one copy.
    """
    by_sys_metric: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for (wk, sysid, metric), val in system_limits.caps.items():
        by_sys_metric.setdefault((sysid, metric), []).append((wk, val))
    for k in by_sys_metric:
        by_sys_metric[k].sort()

    def lookup(week_label: str, system_id: str, metric: str) -> Optional[float]:
        v = system_limits.resolve(week_label, system_id, metric)
        if v is not None:
            return v
        lst = by_sys_metric.get((system_id, metric))
        if not lst:
            return None
        best = lst[0][1]
        for w, val in lst:
            if w <= week_label:
                best = val
            else:
                break
        return best

    return lookup


def system_cap_with_buffer(
    cap_value: float,
    control: ControlParams,
) -> float:
    """Upper-bound system cap after applying R29 global buffer.

    System caps are one-sided: a cap is a ceiling, so the buffer
    (Control R29 `Global buffer`, default 5%) gives the planner extra
    headroom above the raw cap before refusing to place more load.
    Returns `cap_value * (1 + R29)`. Unlike the symmetric R24 facility
    biomass band, there is no lower bound — being under a system cap is
    never a problem.
    """
    return cap_value * (1.0 + control.global_buffer_pct)
