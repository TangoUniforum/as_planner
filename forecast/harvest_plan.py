"""Where the plan LANDS, what you WANT, and the lever that actually moves it.

WHY THIS EXISTS
---------------
Setting a monthly harvest target does not move a single fish. `config/targets.yaml`
is read by the grading layer and by nothing else -- an AST/text search finds it in
`forecast/analysis.py`, `app.py` and the measurement tools, and in NO planner
module. The targets gate says as much ("penalised, never disqualifying").

What DOES move tonnage is the per-week harvest band: `min_harvest_per_week` and
`max_harvest_per_week` overridden for a given week in `scenario/limits.yaml`.
Those resolve through `caps.resolve_facility_cap` into the controller
(`placement.py`), so capping February's weeks genuinely defers fish into
December. It is the same mechanism as the operator's FacilityLimits bands in the
VBA tool.

So an operator needs three things side by side, and previously had them on three
different screens with nothing connecting them:

    1. where the last run LANDED, by month        (the Harvest results tab)
    2. what they WANT                             (Configure -> Targets)
    3. the per-week band that steers it           (Configure -> Limits)

This module computes (1) and (3) beside (2), and turns "December is 224 t short"
into the specific weeks whose band would have to change.

TARGETS GRADE. LIMITS STEER. The UI must never blur that, or an operator sets a
target, sees nothing move, and concludes the tool is broken.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

#: metric keys as stored in scenario/limits.yaml facility overrides
METRIC_MIN = "min_harvest_per_week"
METRIC_MAX = "max_harvest_per_week"


@dataclass
class MonthRow:
    month: str                      # 'YYYY-MM'
    actual_kg: float                # what the last run landed
    target_kg: Optional[float]      # what you asked for, or None
    weeks: tuple                    # ISO week labels falling in this month
    min_override: Optional[float]   # per-week band currently set, if uniform
    max_override: Optional[float]

    @property
    def gap_kg(self) -> Optional[float]:
        if self.target_kg is None:
            return None
        return self.actual_kg - self.target_kg

    @property
    def status(self) -> str:
        """Plain words, not a colour: an operator reads this in a table."""
        if self.target_kg is None:
            return "no target"
        if self.target_kg <= 0:
            return "no target"
        pct = (self.actual_kg - self.target_kg) / self.target_kg * 100.0
        if abs(pct) <= 2.0:
            return "on target"
        return f"{'over' if pct > 0 else 'short'} {abs(pct):.0f}%"


def month_of(week_label: str) -> Optional[str]:
    """'2026-W44' -> '2026-11', by the ISO week's MONDAY.

    A week is attributed to the month its Monday falls in, matching
    analysis.week_to_month, so this view and the targets gate agree about which
    month a week belongs to. Disagreeing would put the gap in one month and the
    lever in another.
    """
    from .analysis import week_to_month
    return week_to_month(week_label)


def build_rows(monthly_actual: dict, targets: Optional[dict],
               week_labels, facility_overrides: dict) -> list[MonthRow]:
    """Join the three sources into one table.

    monthly_actual      {'YYYY-MM': kg} from analysis.harvest_by_period
    targets             analysis.load_targets() result, or None
    week_labels         every planner week in the run, in order
    facility_overrides  FacilityLimits.overrides — {(week, metric): value}
    """
    tmonthly = ((targets or {}).get("monthly") or {})
    weeks_by_month: dict[str, list] = {}
    for wl in week_labels or ():
        m = month_of(wl)
        if m:
            weeks_by_month.setdefault(m, []).append(wl)

    months = sorted(set(monthly_actual) | set(tmonthly) | set(weeks_by_month))
    rows = []
    for m in months:
        wks = tuple(weeks_by_month.get(m, ()))

        def _uniform(metric):
            """The band for this month, but ONLY when every week agrees.

            A month where some weeks are capped and others are not has no
            single number, and showing one would invite an edit that silently
            flattens the difference. None means 'mixed or unset' and the UI
            says so rather than guessing.
            """
            vals = [facility_overrides.get((w, metric)) for w in wks]
            present = [v for v in vals if v is not None]
            if not present or len(present) != len(wks):
                return None
            return present[0] if len(set(present)) == 1 else None

        rows.append(MonthRow(
            month=m,
            actual_kg=float(monthly_actual.get(m, 0.0)),
            target_kg=(float(tmonthly[m]) if m in tmonthly else None),
            weeks=wks,
            min_override=_uniform(METRIC_MIN),
            max_override=_uniform(METRIC_MAX),
        ))
    return rows


def partial_months(rows: list[MonthRow], weeks_per_full_month: int = 4) -> set:
    """Months the horizon only partly covers.

    The first and last month of a run are almost always short, and reading them
    as troughs is a real trap -- the 2026-08 and 2027-10 rows of an August run
    look like collapses and are just the horizon ending. Flagged, never hidden.
    """
    return {r.month for r in rows if 0 < len(r.weeks) < weeks_per_full_month}


def merge_overrides(existing: dict, edits: list[tuple]) -> tuple[dict, list]:
    """Apply month-level band edits to the per-week override map.

    `edits` is [(month, weeks, min_or_None, max_or_None)]. Returns the NEW
    override map and a list of human-readable changes.

    MERGE, NEVER REPLACE. The operator may have hand-tuned individual weeks
    (the VBA-era FacilityLimits bands are exactly that), and a screen that
    rebuilt the whole map from its own partial view would erase every week it
    does not show. Only weeks belonging to an edited month are touched; a
    cleared field removes that week's override rather than writing a zero,
    because 0 fish/week is a real and catastrophic instruction.
    """
    out = dict(existing)
    log = []
    for month, weeks, mn, mx in edits:
        for metric, val in ((METRIC_MIN, mn), (METRIC_MAX, mx)):
            for w in weeks:
                key = (w, metric)
                if val is None:
                    if key in out:
                        del out[key]
                elif out.get(key) != float(val):
                    out[key] = float(val)
            if val is None:
                log.append(f"{month}: cleared {metric} on {len(weeks)} week(s)")
            else:
                log.append(f"{month}: {metric} = {float(val):,.0f} "
                           f"on {len(weeks)} week(s)")
    return out, log


def suggest_band(row: MonthRow, avg_fish_kg: float) -> Optional[str]:
    """Turn a tonnage gap into the band change that would chase it.

    Advisory only, and deliberately phrased as arithmetic rather than a
    recommendation: the plan is chaos-sensitive (a neutral knob moves the worst
    harvest week by 8,629 fish), so the honest claim is "this is the size of the
    gap in fish/week", never "set this and it will work".
    """
    if row.target_kg is None or not row.weeks or avg_fish_kg <= 0:
        return None
    gap = row.gap_kg or 0.0
    if abs(gap) < 1000:
        return None
    fish = gap / avg_fish_kg / len(row.weeks)
    if gap < 0:
        return (f"short {abs(gap) / 1000:,.0f} t — about "
                f"{abs(fish):,.0f} more fish/week across {len(row.weeks)} "
                f"week(s). Fish must come from somewhere: RAISE this month's "
                f"floor only if an earlier month can spare them, or LOWER the "
                f"cap on the fat month you want to defer from.")
    return (f"over by {gap / 1000:,.0f} t — about {fish:,.0f} fish/week could "
            f"defer. LOWER this month's cap to push them later.")
