"""Time grid for the forecast.

Week-of-year (ISO 8601) labels are the canonical "which week" identifier
throughout the project — e.g. `"2026-W20"`. Forecast-relative integer
week offsets are not stored anywhere; they're only used internally as
a temporary index while walking the horizon.

Week labels sort chronologically as strings (within a year and across
year boundaries: `"2026-W52" < "2027-W01"`).


Internal simulation grain is daily. External aggregation is weekly,
**ISO-Monday-aligned** with a possibly-partial first week when
forecast_start is not a Monday:

    week 0 = [forecast_start, next Monday)         length 0-7 days
    week i = [next Monday + (i-1)*7d, +7d)         length 7 days, i >= 1

If forecast_start IS a Monday, week 0 is itself a full 7-day ISO week
and the partial-first-week machinery is a no-op.

This matches the layout the operator's FacilityLimits / SystemLimits
sheets use (first column dated at forecast_start, subsequent columns
dated to the following Mondays).

The Daily Harvest Schedule splits weekly harvests across calendar
Mon-Fri. `mon_fri_in_week` returns the Mon-Fri dates falling inside
a given forecast week (0-5 days when the first week is partial; 5
days in any full week).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional


def _as_date(d) -> date:
    return d.date() if isinstance(d, datetime) else d


def _next_monday_on_or_after(d: date) -> date:
    """Smallest Monday >= d. If d is Monday, returns d."""
    days_to_mon = (7 - d.weekday()) % 7  # Mon=0
    return d + timedelta(days=days_to_mon)


def _partial_w0_length(forecast_start) -> int:
    """0 if forecast_start is Monday; else 1-6 days until next Monday."""
    fs = _as_date(forecast_start)
    return (_next_monday_on_or_after(fs) - fs).days


def week_start(i: int, forecast_start) -> date:
    """Inclusive start date of forecast week i."""
    fs = _as_date(forecast_start)
    partial = _partial_w0_length(fs)
    if partial == 0:
        return fs + timedelta(days=i * 7)
    if i == 0:
        return fs
    return fs + timedelta(days=partial + (i - 1) * 7)


def week_end(i: int, forecast_start) -> date:
    return week_start(i + 1, forecast_start)


def week_range(i: int, forecast_start) -> tuple[date, date]:
    return week_start(i, forecast_start), week_end(i, forecast_start)


def week_index(d, forecast_start) -> int:
    """Forecast week index for date d.

    Negative indices for dates before forecast_start (floor toward
    -infinity, with W0 being the partial-or-full window starting at
    forecast_start).
    """
    fs = _as_date(forecast_start)
    dd = _as_date(d)
    delta = (dd - fs).days
    if delta < 0:
        return -((-delta + 6) // 7)
    partial = _partial_w0_length(fs)
    if partial == 0:
        return delta // 7
    if delta < partial:
        return 0
    return 1 + (delta - partial) // 7


def iso_week_label(d) -> str:
    """ISO 8601 year-week label, e.g. '2026-W20'."""
    dd = _as_date(d)
    y, w, _ = dd.isocalendar()
    return f"{y}-W{w:02d}"


def week_label(i: int, forecast_start) -> str:
    """ISO label of the week containing forecast week i's start date."""
    return iso_week_label(week_start(i, forecast_start))


def day_offset(d, forecast_start) -> int:
    """Days since forecast_start (0 = forecast_start, may be negative)."""
    return (_as_date(d) - _as_date(forecast_start)).days


def forecast_week_labels(forecast_start, horizon_weeks: int) -> list[str]:
    """ISO labels for every forecast week, in chronological order.

    First label is the ISO week containing forecast_start (which may
    map to a partial week internally if forecast_start isn't Monday);
    subsequent labels are consecutive ISO weeks.
    """
    return [week_label(i, forecast_start) for i in range(horizon_weeks)]


def parse_iso_label(label: str) -> Optional[date]:
    """Parse 'YYYY-Www' or 'YYYY-W##' → date (Monday of that ISO week)."""
    m = re.match(r"^\s*(\d{4})-W(\d{1,2})\s*$", str(label))
    if not m:
        return None
    y, w = int(m.group(1)), int(m.group(2))
    try:
        return date.fromisocalendar(y, w, 1)
    except ValueError:
        return None


def label_for_date(d, forecast_start) -> str:
    """Canonical week label for a date inside the forecast (or before/after).

    Equivalent to `week_label(week_index(d, forecast_start), forecast_start)`.
    """
    return week_label(week_index(d, forecast_start), forecast_start)


def mon_fri_in_week(i: int, forecast_start) -> list[date]:
    """Calendar Mon-Fri dates that fall within forecast week i."""
    start, end = week_range(i, forecast_start)
    out: list[date] = []
    d = start
    while d < end:
        if d.weekday() <= 4:
            out.append(d)
        d += timedelta(days=1)
    return out
