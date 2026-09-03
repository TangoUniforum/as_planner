"""A week's harvest is counted in the month of its ISO MONDAY.

Operator decision, 2026-09-02, settling an audit finding: the sales-facing
sheets prorated a boundary week across its Mon-Fri days while the targets gate
and the app's monthly view attributed the whole week by ISO Monday. Both are
defensible descriptions of reality — fish do physically leave the plant across
the week — but only one can be the contract, and a plan graded on one convention
while committed on the other disagrees with itself.

Measured on the 2026-07-31 run before the change: the same 7,919 t landed in
different months by as much as 97 t, against monthly targets of 570-700 t.
2026-08 read 416.0 t by Monday and 328.6 t by working day.

The working-day split is KEPT — it still describes the physical Mon-Fri flow,
and the Daily Harvest Schedule is built on it. It is simply no longer the basis
for a monthly sales number.
"""
import datetime

from forecast.time_grid import (calendar_day_month_split,
                                iso_week_month_split,
                                working_day_month_split)


def test_a_boundary_week_is_not_split():
    """2026-08-31 is a Monday whose week runs into September."""
    monday = datetime.date(2026, 8, 31)
    assert iso_week_month_split(monday) == {(2026, 8): 1.0}


def test_any_day_of_the_week_lands_on_its_mondays_month():
    """The rule keys off the Monday, not the date handed in — a Tuesday in
    September still belongs to August if its week started there."""
    for offset in range(7):
        d = datetime.date(2026, 8, 31) + datetime.timedelta(days=offset)
        assert iso_week_month_split(d) == {(2026, 8): 1.0}, d


def test_it_crosses_a_year_boundary_by_the_same_rule():
    # 2026-12-28 is a Monday; its week runs into January 2027.
    assert iso_week_month_split(datetime.date(2026, 12, 28)) == {(2026, 12): 1.0}
    # 2027-01-04 is the following Monday.
    assert iso_week_month_split(datetime.date(2027, 1, 4)) == {(2027, 1): 1.0}


def test_it_always_sums_to_one_and_names_one_month():
    d = datetime.date(2026, 1, 1)
    for _ in range(120):
        split = iso_week_month_split(d)
        assert len(split) == 1
        assert abs(sum(split.values()) - 1.0) < 1e-12
        d += datetime.timedelta(days=3)


def test_it_differs_from_the_working_day_split_where_it_matters():
    """The whole reason the finding existed. A boundary week splits under the
    old rule and does not under this one."""
    monday = datetime.date(2026, 8, 31)          # Mon-Fri spans Aug 31 - Sep 4
    wd = working_day_month_split(monday)
    assert len(wd) == 2                          # it DID split
    assert set(wd) == {(2026, 8), (2026, 9)}
    assert len(iso_week_month_split(monday)) == 1


def test_the_daily_flow_split_is_untouched():
    """Feed, growth and mortality are consumed every day and still split by
    calendar day. Only the HARVEST month convention changed."""
    split = calendar_day_month_split(datetime.date(2026, 8, 31))
    assert set(split) == {(2026, 8), (2026, 9)}
    assert abs(sum(split.values()) - 1.0) < 1e-12


def test_a_wholly_interior_week_is_identical_under_both_rules():
    """No boundary, no disagreement — the change moves only boundary weeks."""
    monday = datetime.date(2026, 8, 10)
    assert iso_week_month_split(monday) == working_day_month_split(monday)
