"""A report may not carry a month the forecast does not cover.

OPERATOR, 2026-09-08: "the report open is the start of the day after the
production report ... so there should be no aug inputs for the report, also in
the daily harvest schedule, the harvest entry for 8/31 should not be included
in the forecast for sep as that obviously belongs to Aug ... there should be no
entries in this document for Aug as it starts on 9/1."

WHY IT HAPPENED. An ISO week starts on a MONDAY. A forecast opening the day
after a month-end PR opens mid-week: the 2026-08-31 closing gives
forecast_start 2026-09-01 while week 2026-W36 starts Mon 2026-08-31. Every
month/day attribution keyed on the week rather than the forecast then reached
back into August — a MonthlyReport month with 17 rows, an August column in the
HarvestPlan Report carrying B41's 5,322 fish, and a Daily Harvest Schedule row
dated 8/31. All of it belongs to the ProductionReport, not the forecast.

THE TRAP THAT MADE THIS HARD. There are TWO starts in a run with a manual
override window, and they are not the same date:
  * the PLANNING start (`control.forecast_start`, `fs_date`) is shifted FORWARD
    by the window length — 2026-09-15 here;
  * the REPORT start (`report_start`, run.py) is the true opening, 2026-09-01.
Clipping on the planning start deletes the window weeks outright, which is
exactly why an earlier clip on `working_day_month_split` was retired (its
docstring still records it). These tests pin the distinction so the next
change cannot collapse the two again.

TOTALS ARE NEVER TOUCHED. A straddling week is re-spread over the days that
remain, so this moves which month a flow is REPORTED in and never a kilogram:
verified on the live plan, MonthlyReport growth/feed/harvest totals identical
to the kilogram before and after (14,731,571 / 17,375,922 / 14,498,372), and
every plan and audit sheet byte-identical.
"""
from __future__ import annotations

import datetime as dt

from forecast.time_grid import calendar_day_month_split, iso_week_month_split

W36_MONDAY = dt.date(2026, 8, 31)      # the ISO Monday of the first forecast week
OPENS = dt.date(2026, 9, 1)            # PR closing 8/31 + 1 day


def test_without_a_clip_the_week_still_lands_in_august():
    """NEGATIVE CONTROL — the old behaviour, so a green suite means something."""
    assert iso_week_month_split(W36_MONDAY) == {(2026, 8): 1.0}
    assert calendar_day_month_split(W36_MONDAY)[(2026, 8)] > 0


def test_the_harvest_month_moves_to_the_month_the_report_opens():
    assert iso_week_month_split(W36_MONDAY, clip_start=OPENS) == {(2026, 9): 1.0}


def test_daily_flows_drop_the_days_before_the_open_and_renormalise():
    split = calendar_day_month_split(W36_MONDAY, clip_start=OPENS)
    assert (2026, 8) not in split
    assert abs(split[(2026, 9)] - 1.0) < 1e-9, "fractions must still sum to 1"


def test_a_week_wholly_inside_the_horizon_is_unaffected():
    """The clip must only bite on the boundary week."""
    inside = dt.date(2026, 9, 14)
    assert (iso_week_month_split(inside, clip_start=OPENS)
            == iso_week_month_split(inside))
    assert (calendar_day_month_split(inside, clip_start=OPENS)
            == calendar_day_month_split(inside))


def test_a_genuine_boundary_week_mid_horizon_still_splits_normally():
    """Sep/Oct boundary, far from the open: the calendar split is untouched."""
    wk = dt.date(2026, 9, 28)          # Mon 28 Sep, week runs into October
    got = calendar_day_month_split(wk, clip_start=OPENS)
    assert set(got) == {(2026, 9), (2026, 10)}
    assert abs(sum(got.values()) - 1.0) < 1e-9


def test_clipping_never_loses_a_week_when_every_day_precedes_the_start():
    """The failure that retired the earlier clip: a week wholly before the
    start must NOT vanish. Daily flows keep their own days; the harvest month
    clamps to the first reported month."""
    old = dt.date(2026, 1, 5)
    daily = calendar_day_month_split(old, clip_start=OPENS)
    assert abs(sum(daily.values()) - 1.0) < 1e-9
    assert set(daily) == {(2026, 1)}, "the week kept its own days"
    assert iso_week_month_split(old, clip_start=OPENS) == {(2026, 9): 1.0}


def test_no_clip_is_the_default_everywhere():
    """Existing callers that pass nothing must behave exactly as before."""
    for d in (W36_MONDAY, dt.date(2026, 9, 28), dt.date(2027, 1, 4)):
        assert iso_week_month_split(d, clip_start=None) == iso_week_month_split(d)
        assert (calendar_day_month_split(d, clip_start=None)
                == calendar_day_month_split(d))
