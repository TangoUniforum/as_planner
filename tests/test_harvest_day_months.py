"""Harvest is counted in the month and year of its HARVEST DAYS.

Operator, 2026-10-06: "harvest should be assigned to the month, year that the
harvest day happens ... we should plan on even harvests, the plan from the
harvest plan report, and assign appropriately ... everything should change and
be calculated that way as that is more correct."

So a week's harvest is spread evenly over its Mon-Fri harvest days -- exactly
the days the Daily Harvest Schedule lists (time_grid.harvest_schedule_days:
none before the report opens) -- and each day counts in its own month and year.
This replaces the 2026-09-02 rule that put the whole week in its ISO Monday's
month (tests/test_harvest_month_convention.py keeps that function's own tests).

Measured on the 9/30 PR's live plan before the change: the app showed 2027 as
434/328/699/534/708/547/546/673/538/569/660/484 t by Monday against a plan
levelled to 467/328/642/590/606/593/601/595/592/594/585/559 t by harvest day,
and 2026 read 2,047 t against 2,014 t. Same fish, same weeks; only which month
counts them.

Every sheet must agree, so most tests here build ONE set of harvest events and
check that HarvestPlan Report, HarvestReport, YearlySummary, MonthlyReport,
CostsAndProfit's month rows and the app's analysis helpers all book the same
split -- in whole fish that add up.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import openpyxl
import pytest

from forecast import analysis as A
from forecast import excel_io
from forecast.caps import FacilityLimits
from forecast.events import Harvest
from forecast.placement import BatchLocationRow
from forecast.time_grid import (harvest_month_split, harvest_schedule_days,
                                iso_week_month_split)

OPENS = date(2026, 10, 1)            # PR closing 9/30 + 1 day (a Thursday)
W40 = date(2026, 9, 28)              # Mon; Thu-Fri are the forecast's days
W44 = date(2026, 10, 26)             # Mon 26 - Fri 30 Oct: inside October
W49 = date(2026, 11, 30)             # Mon 30 Nov, Tue-Fri 1-4 Dec
W53 = date(2026, 12, 28)             # Mon 28 Dec - Fri 1 Jan 2027


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #
def test_a_week_inside_one_month_is_all_that_month():
    assert harvest_month_split(W44) == {(2026, 10): 1.0}


def test_a_month_end_week_splits_by_its_harvest_days():
    assert harvest_month_split(W49) == pytest.approx({(2026, 11): 0.2,
                                                      (2026, 12): 0.8})


def test_the_year_end_week_splits_between_years():
    """2026-W53: Mon 28 - Thu 31 Dec in 2026, Fri 1 Jan in 2027."""
    assert harvest_month_split(W53) == pytest.approx({(2026, 12): 0.8,
                                                      (2027, 1): 0.2})


def test_negative_control_the_old_monday_rule_kept_the_week_whole():
    """So a green suite means the reports really changed rule."""
    assert iso_week_month_split(W49) == {(2026, 11): 1.0}
    assert iso_week_month_split(W53) == {(2026, 12): 1.0}


def test_no_harvest_day_before_the_report_opens():
    """The first week of a 9/30 PR: only Thu 1 and Fri 2 Oct are forecast
    days, so the week is all October, never September."""
    assert harvest_month_split(W40, OPENS) == {(2026, 10): 1.0}
    assert harvest_month_split(W40) == pytest.approx({(2026, 9): 0.6,
                                                      (2026, 10): 0.4})


def test_a_weekend_opening_still_puts_the_week_somewhere():
    sat = date(2026, 10, 3)
    split = harvest_month_split(W40, sat)
    assert split == {(2026, 10): 1.0}


@pytest.mark.parametrize("d, rs", [(W40, OPENS), (W44, None), (W49, None),
                                   (W53, None), (W40, date(2026, 10, 3))])
def test_it_is_exactly_the_daily_harvest_schedule_days(d, rs):
    """One rule: the month split is the Daily Harvest Schedule's days."""
    days = harvest_schedule_days(d, rs)
    want = {}
    for x in days:
        want[(x.year, x.month)] = want.get((x.year, x.month), 0) + 1 / len(days)
    assert harvest_month_split(d, rs) == pytest.approx(want)
    assert sum(harvest_month_split(d, rs).values()) == pytest.approx(1.0)


def test_week_labels_split_the_same_way():
    assert A.week_month_split("2026-W53") == pytest.approx(
        {"2026-12": 0.8, "2027-01": 0.2})
    assert A.week_month_split("2026-W40", OPENS) == {"2026-10": 1.0}
    assert A.week_month_split("garbage") == {}


# --------------------------------------------------------------------------- #
# The app's helpers
# --------------------------------------------------------------------------- #
def _row(week, count, hog_kg):
    return {"week": week, "count": count, "gross_avg_kg": 4.0,
            "gross_kg": hog_kg / 0.81, "hog_kg": hog_kg,
            "hog_avg_kg": hog_kg / count}


def test_harvest_by_period_splits_months_and_years():
    rows = [_row("2026-W49", 10_000, 30_000.0), _row("2026-W53", 10_000, 50_000.0)]
    monthly, yearly = A.harvest_by_period(rows)
    assert monthly == pytest.approx({"2026-11": 6_000.0,
                                     "2026-12": 24_000.0 + 40_000.0,
                                     "2027-01": 10_000.0})
    assert yearly == pytest.approx({"2026": 70_000.0, "2027": 10_000.0})


def test_a_month_is_only_judged_when_all_its_harvest_weeks_are_planned():
    """December 2026 has harvest days in W49..W53: without W53 it is partial."""
    weeks = [f"2026-W{w}" for w in range(45, 53)]
    full_m, _ = A.full_periods(weeks)
    assert "2026-11" in full_m and "2026-12" not in full_m
    full_m, _ = A.full_periods(weeks + ["2026-W53"])
    assert "2026-12" in full_m


def test_revenue_prices_each_month_share_at_that_months_price():
    econ = {"basis": "hog", "currency": "USD", "model_cv_pct": 1.0,
            "price_bands": [{"min_kg": 0.0, "max_kg": 99.0,
                             "price_per_kg": 10.0,
                             "monthly": {"2027-01": 20.0}}]}
    got = A.revenue_for([_row("2026-W53", 10_000, 50_000.0)], econ)
    assert got["total"] == pytest.approx(40_000 * 10.0 + 10_000 * 20.0, rel=1e-6)


def test_a_row_already_booked_to_a_month_is_not_split_again():
    """CostsAndProfit hands revenue_for one month's share at a time."""
    from forecast.costs_report import harvest_by_month
    econ = {"basis": "hog", "currency": "USD", "model_cv_pct": 1.0,
            "price_bands": [{"min_kg": 0.0, "max_kg": 99.0,
                             "price_per_kg": 10.0, "monthly": {}}]}
    by_m = harvest_by_month([_row("2026-W53", 10_000, 50_000.0)], OPENS)
    assert sorted(by_m) == ["2026-12", "2027-01"]
    assert sum(r["hog_kg"] for r in by_m["2027-01"]) == pytest.approx(10_000.0)
    assert A.revenue_for(by_m["2027-01"], econ)["total"] == pytest.approx(
        100_000.0, rel=1e-6)


# --------------------------------------------------------------------------- #
# The workbook: every sheet books the same split, in whole fish that add up
# --------------------------------------------------------------------------- #
def _hv(d, batch, tank, count, wt=4000.0):
    return Harvest(batch_id=batch, event_date=d, source_tank_id=tank,
                   count=count, avg_wt_g=wt)


def _wk(d):
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _loc(d, batch, tank, count, wt=4000.0):
    return BatchLocationRow(
        week_label=_wk(d), week_start=d, batch_id=batch, tank_id=tank,
        location_id=f"OG6N-{tank}", system_id="OG6N", count=count,
        avg_wt_g=wt, biomass_kg=count * wt / 1000.0, density_kg_m3=50.0,
        stage="")


def _ctl():
    return SimpleNamespace(max_biomass_kg=3_800_000.0, max_feed_per_day_kg=30_000.0,
                           max_harvest_per_week=55_000.0,
                           min_harvest_per_week=26_000.0, default_hog_yield=0.81,
                           max_transfers_per_week=0)


def _events():
    return [_hv(W40, "B43", 63, 2_160.0),
            _hv(W44, "B44", 65, 51_500.0),
            _hv(W49, "B45", 61, 30_001.0), _hv(W49, "B46", 67, 29_002.0),
            _hv(W53, "B46", 71, 33_333.0), _hv(W53, "B47", 52, 26_667.0)]


def _rows(ws):
    return [tuple(r) for r in ws.iter_rows(values_only=True)]


def _table(ws, pred):
    rows = _rows(ws)
    i = next(k for k, r in enumerate(rows) if r and pred(r))
    hdr = [str(c) if c is not None else "" for c in rows[i]]
    out = []
    for r in rows[i + 1:]:
        if not r or all(c is None for c in r):
            break
        out.append(dict(zip(hdr, r)))
    return out


def _hpr_units(wb):
    """{(year, month): TOTAL Units} and {year: TOTAL <year>} from HarvestPlan Report."""
    by_m, by_y, y = {}, {}, None
    for r in _rows(wb["HarvestPlan Report"]):
        if r and isinstance(r[0], str) and r[0].startswith("S "):
            y = int(r[0][-4:])
        if r and r[0] == "TOTAL" and r[1] == "Units":
            for mo in range(1, 13):
                v = r[1 + mo]
                if isinstance(v, (int, float)) and v:
                    by_m[(y, mo)] = v
            by_y[y] = r[14]
    return by_m, by_y


@pytest.fixture
def wb():
    evs = _events()
    locs = [_loc(e.event_date, e.batch_id, e.source_tank_id, e.count) for e in evs]
    book = openpyxl.Workbook()
    excel_io.write_harvest_plan_output(book, evs, 0.81, {})
    excel_io.write_harvest_plan_report(book, evs, "S", 0.81, {}, report_start=OPENS)
    excel_io.write_harvest_report(book, evs, 0.81, {}, report_start=OPENS)
    excel_io.write_yearly_summary(book, [], evs, FacilityLimits(), _ctl(),
                                  default_hog_yield=0.81, report_start=OPENS)
    excel_io.write_monthly_report(book, locs, evs, [], report_start=OPENS)
    return book


def test_harvest_plan_report_books_months_by_harvest_day(wb):
    by_m, by_y = _hpr_units(wb)
    # W40 -> October whole (no September harvest day); W44 October.
    assert by_m[(2026, 10)] == 2_160 + 51_500
    # W49: 1/5 November, 4/5 December (whole fish per event).
    assert by_m[(2026, 11)] == 6_000 + 5_800            # 30,001/5 -> 6,000; 29,002/5 -> 5,800
    # W53: 4/5 December 2026, 1/5 January 2027.
    assert by_m[(2026, 12)] == (30_001 - 6_000) + (29_002 - 5_800) \
        + (33_333 - 6_667) + (26_667 - 5_333)
    assert by_m[(2027, 1)] == 6_667 + 5_333
    assert by_y[2026] + by_y[2027] == sum(e.count for e in _events())


def test_every_period_total_is_whole_fish(wb):
    by_m, by_y = _hpr_units(wb)
    for v in list(by_m.values()) + list(by_y.values()):
        assert float(v).is_integer()


def test_harvest_report_writes_one_row_per_month_a_week_harvests_in(wb):
    hr = _table(wb["HarvestReport"], lambda r: r[0] == "Year" and "Batch" in r)
    w53 = [r for r in hr if r["Week"] == "2026-W53" and r["Batch"] == "B46"]
    assert [(r["Year"], r["Date"].date() if hasattr(r["Date"], "date") else r["Date"])
            for r in w53] == [(2026, date(2026, 12, 28)), (2027, date(2027, 1, 1))]
    assert sum(r["Count (fish)"] for r in w53) == 33_333
    # a week inside one month keeps its single row, dated on its Monday
    w44 = [r for r in hr if r["Week"] == "2026-W44"]
    assert len(w44) == 1 and w44[0]["Count (fish)"] == 51_500
    # the first week is October: no September row, no harvest day before 10/1
    w40 = [r for r in hr if r["Week"] == "2026-W40"]
    assert len(w40) == 1 and w40[0]["Month"].month == 10


def test_harvest_report_yearly_summary_and_hpr_agree_on_each_year(wb):
    hr = _table(wb["HarvestReport"], lambda r: r[0] == "Year" and "Batch" in r)
    hr_y = {}
    for r in hr:
        hr_y[r["Year"]] = hr_y.get(r["Year"], 0) + r["Count (fish)"]
    ys = {r["Year"]: r["Harvest_Count (fish)"] for r in _table(
        wb["YearlySummary"], lambda r: r[0] == "Year")}
    _m, hpr_y = _hpr_units(wb)
    assert ys[2027] == hr_y[2027] == hpr_y[2027] == 6_667 + 5_333
    assert ys[2026] == hr_y[2026] == hpr_y[2026]


def test_harvest_report_months_tie_to_hpr_months(wb):
    hr = _table(wb["HarvestReport"], lambda r: r[0] == "Year" and "Batch" in r)
    hr_m = {}
    for r in hr:
        k = (r["Month"].year, r["Month"].month)
        hr_m[k] = hr_m.get(k, 0) + r["Count (fish)"]
    by_m, _y = _hpr_units(wb)
    assert hr_m == by_m


def test_monthly_report_harvest_ties_to_hpr_months(wb):
    mo = [x for x in _table(wb["MonthlyReport"], lambda r: r[0] == "Scenario")
          if x["Batch"] == "TOTAL"]
    by_m, _y = _hpr_units(wb)
    got = {}
    for x in mo:
        m = x["Month"]
        k = (m.year, m.month) if hasattr(m, "year") else (int(str(m)[:4]), int(str(m)[5:7]))
        got[k] = x["Harv_Count (fish)"]
    assert {k: v for k, v in got.items() if v} == by_m


def test_the_app_helpers_book_what_the_workbook_books(wb):
    """analysis.harvest_by_period over HarvestPlan == HarvestPlan Report HOG."""
    rows = A.harvest_rows_from_ws(wb["HarvestPlan"])
    monthly, _y = A.harvest_by_period(rows, clip_start=OPENS)
    hpr_hog, y, in_total = {}, None, False
    for r in _rows(wb["HarvestPlan Report"]):
        if r and isinstance(r[0], str) and r[0].startswith("S "):
            y = int(r[0][-4:])
        if r and r[0]:
            in_total = r[0] == "TOTAL"          # the TOTAL block's 3 rows
        if r and in_total and r[1] == "Biomass - Tons HOG":
            for mo in range(1, 13):
                v = r[1 + mo]
                if isinstance(v, (int, float)) and v:
                    hpr_hog["%04d-%02d" % (y, mo)] = v
    assert sorted(hpr_hog) == sorted(m for m, v in monthly.items() if v)
    for m, t in hpr_hog.items():
        assert monthly[m] / 1000.0 == pytest.approx(t, abs=0.6)
