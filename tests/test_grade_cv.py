"""Grading narrows the size spread — and the plan must be able to see it.

Until 2026-09-18 every graded leg was stamped with the PARENT's CV, so the
distribution was modelled as unchanged by a sort. Measured on the 9.16 PR: 16%
on all 736 TransferPlan rows, including after all 178 grade events. Harvest
eligibility reads the CV (frac_above), so a graded leg was judged on twice its
real spread.
"""
from __future__ import annotations

import math
from statistics import NormalDist

import pytest

from forecast.biology import count_split_cvs, count_split_means

MU, CV = 4000.0, 16.0


def _total_sd(mu, cv, p, eff):
    """Re-assemble the two legs and measure the population's spread."""
    mu_u, mu_l = count_split_means(mu, cv, p, grade_efficiency=eff)
    cv_u, cv_l = count_split_cvs(mu, cv, p, grade_efficiency=eff)
    su, sl = mu_u * cv_u / 100.0, mu_l * cv_l / 100.0
    mean = p * mu_u + (1 - p) * mu_l
    var = (p * (su ** 2 + (mu_u - mean) ** 2)
           + (1 - p) * (sl ** 2 + (mu_l - mean) ** 2))
    return math.sqrt(var)


@pytest.mark.parametrize("p", [0.1, 0.25, 0.5, 0.75, 0.9])
@pytest.mark.parametrize("eff", [1.0, 0.85, 0.5])
def test_the_sort_never_creates_or_destroys_spread(p, eff):
    """Within + between variance still equals the parent's, at every
    efficiency — the same conservation promise count_split_means makes for
    mass. Without it a grader could invent or delete variance."""
    assert _total_sd(MU, CV, p, eff) == pytest.approx(MU * CV / 100.0, rel=1e-9)


@pytest.mark.parametrize("p", [0.1, 0.5, 0.9])
def test_both_legs_are_tighter_than_the_tank_they_came_from(p):
    cv_u, cv_l = count_split_cvs(MU, CV, p, grade_efficiency=0.85)
    assert cv_u < CV and cv_l < CV, (cv_u, cv_l)


def test_a_perfect_grader_gives_the_textbook_truncated_normal():
    """Median split of a standard normal: each half has sd sigma*sqrt(1-2/pi)
    and means mu +/- sigma*sqrt(2/pi)."""
    sigma = MU * CV / 100.0
    d = sigma * math.sqrt(2 / math.pi)
    sd_leg = sigma * math.sqrt(1 - 2 / math.pi)
    cv_u, cv_l = count_split_cvs(MU, CV, 0.5, grade_efficiency=1.0)
    assert cv_u == pytest.approx(100 * sd_leg / (MU + d), rel=1e-9)
    assert cv_l == pytest.approx(100 * sd_leg / (MU - d), rel=1e-9)


def test_a_grader_that_sorts_nothing_leaves_the_spread_alone():
    """efficiency -> 0 is 'the grader does not separate': both legs must carry
    the parent's spread, not a narrowed one."""
    cv_u, cv_l = count_split_cvs(MU, CV, 0.5, grade_efficiency=1e-6)
    assert cv_u == pytest.approx(CV, abs=0.05)
    assert cv_l == pytest.approx(CV, abs=0.05)


@pytest.mark.parametrize("bad", [(0.0, CV, 0.5), (MU, 0.0, 0.5),
                                 (MU, CV, 0.0), (MU, CV, 1.0)])
def test_degenerate_inputs_return_the_parent_cv(bad):
    """Same contract as count_split_means: nothing to split -> unchanged."""
    assert count_split_cvs(*bad) == (bad[1], bad[1])


def test_the_harvest_tail_it_was_getting_wrong():
    """The consequence, pinned: after a median grade of a 4,000 g tank, how
    much of the BIG leg does the planner think is under a 3,200 g gate?"""
    mu_u, _ = count_split_means(MU, CV, 0.5, grade_efficiency=1.0)
    cv_u, _ = count_split_cvs(MU, CV, 0.5, grade_efficiency=1.0)
    under = lambda cv: NormalDist().cdf((3200 - mu_u) / (mu_u * cv / 100.0))
    assert under(CV) > 0.03                  # the old answer: >3%
    assert under(cv_u) < 0.001               # the modelled truth: ~0.03%


def test_a_split_that_does_not_sort_is_not_this_function():
    """The density half-split (placement.py, DENSITY_TRIGGER path) divides a
    tank by COUNT without a grader: both halves keep the parent mean AND the
    parent spread. Pinned so nobody 'fixes' that site with count_split_cvs."""
    import inspect

    from forecast import placement
    src = inspect.getsource(placement)
    i = src.index("half_count = tank.count / 2")
    block = src[i:i + 1500]
    assert "count_split_cvs" not in block, \
        "the non-sorting density split must keep the parent CV"
    assert block.count("cv_pct=tank.cv_pct") == 2


# --------------------------------------------------------------------------- #
# Two different JOBS on one row type (operator 2026-09-18: "grading and splits
# are both options"). The plan has to say which one it is asking the crew for.
# --------------------------------------------------------------------------- #
def _transfer_plan_rows(*, grade_events=(), transfer_events=()):
    import openpyxl

    from forecast.excel_io import write_transfer_plan_output
    wb = openpyxl.Workbook()
    write_transfer_plan_output(wb, list(transfer_events), [],
                               grade_events=list(grade_events))
    ws = wb["TransferPlan"]
    rows = [[c.value for c in r] for r in ws.iter_rows()]
    hdr = next(r for r in rows if r and str(r[0]).strip() == "Week")
    i = rows.index(hdr)
    cols = {str(v).split(" (")[0]: j for j, v in enumerate(hdr) if v}
    return [{k: r[j] for k, j in cols.items()} for r in rows[i + 1:] if r[0]]


def _alloc(tank, count, wt, cv):
    from forecast.events import TankAllocation
    return TankAllocation(tank_id=tank, count=count, avg_wt_g=wt, cv_pct=cv)


def test_a_density_split_is_not_reported_as_a_grade():
    """Halving a tank by count uses no sorter, so both halves keep the parent
    weight AND spread. Reported as its own channel so the crew knows which job
    to rig — on the 9.16 PR this is 162 of 178 Grade rows."""
    from datetime import date

    from forecast.events import Grade
    ev = Grade(batch_id="B01", event_date=date(2026, 9, 7), kind="split",
               source_tank_ids=[31],
               destinations=[_alloc(31, 5000, 4000.0, 16.0),
                             _alloc(32, 5000, 4000.0, 16.0)])
    rows = _transfer_plan_rows(grade_events=[ev])
    assert {r["Channel"] for r in rows} == {"density_split"}
    assert {r["CV"] for r in rows} == {16.0}, "a split must not narrow the spread"
    assert {r["Avg_Weight"] for r in rows} == {4.0}


def test_a_size_grade_still_reports_as_one():
    from datetime import date

    from forecast.events import Grade
    ev = Grade(batch_id="B01", event_date=date(2026, 9, 7),
               source_tank_ids=[31],
               destinations=[_alloc(31, 5000, 3489.0, 11.1),
                             _alloc(32, 5000, 4511.0, 8.6)])
    rows = _transfer_plan_rows(grade_events=[ev])
    assert {r["Channel"] for r in rows} == {"grade_event"}
    assert sorted(r["CV"] for r in rows) == [8.6, 11.1]


def test_a_graded_harvest_writes_each_leg_its_own_cv():
    """The pickup and retention legs left the sorter with different spreads;
    the sheet used to print the parent's CV on both."""
    from datetime import date

    from forecast.events import GradedHarvest
    ev = GradedHarvest(batch_id="B01", event_date=date(2026, 9, 7),
                       source_tank_id=31, pickup_tank_id=40, pickup_count=1000,
                       pickup_avg_wt_g=4511.0, retention_tank_id=41,
                       retention_count=1000, retention_avg_wt_g=3489.0,
                       cv_pct=16.0, pickup_cv_pct=8.6, retention_cv_pct=11.1)
    rows = _transfer_plan_rows(transfer_events=[ev])
    by = {r["Grade"]: r for r in rows}
    assert by["pickup"]["CV"] == 8.6 and by["retention"]["CV"] == 11.1
    assert {r["Channel"] for r in rows} == {"graded_harvest"}


def test_an_event_that_says_nothing_still_reads_as_a_grade():
    """kind defaults to 'grade' and the CV fields default to the parent, so
    every caller written before 2026-09-18 behaves exactly as it did."""
    from datetime import date

    from forecast.events import Grade, GradedHarvest
    g = Grade(batch_id="B01", event_date=date(2026, 9, 7), source_tank_ids=[31],
              destinations=[_alloc(31, 5000, 3489.0, 16.0)])
    assert g.kind == "grade"
    h = GradedHarvest(batch_id="B01", event_date=date(2026, 9, 7),
                      source_tank_id=31, pickup_tank_id=40, pickup_count=1,
                      pickup_avg_wt_g=4000.0, retention_tank_id=41,
                      retention_count=1, retention_avg_wt_g=3000.0, cv_pct=16.0)
    assert h._pickup_cv == 16.0 and h._retention_cv == 16.0
