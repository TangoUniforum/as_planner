"""6N harvest fixes from the operator's October plan (2026-10-03).

1. THE WINDOW'S 6N FILLS CARRY A FILL DATE. A manual override window's
   og_to_6n / staged graded fill never stamped state.sixn_fill_date, and the
   planner's realized state was rebuilt without the ledger anyway. An unknown
   fill read as "purged 1000000d": overdue at once, so exempt from the weekly
   processing limit -- W46 drained OG6N-63 whole (55,301 into a 51,500 week)
   and W47 force-drained 65 + 71. Now the window stamps the scripted week, the
   planner's clone keeps the ledger, and a fill the run never witnessed (fish
   already in 6N at the PR close) counts from the original forecast start.

2. THE FLOOR BEATS THE CAP ("floor should win"). The limit hold held OG6N-69
   (18,389 fish, 18,156 of room) and the week landed at 33,157 against a
   51,500 floor. The hold now stands only if the week already meets its floor
   or another tank still to drain reaches it inside the limit.
"""
from __future__ import annotations

from datetime import date, timedelta

from forecast.manual_events import ManualDest, ManualEvent, _apply_og_to_6n
from forecast.models import ControlParams
from forecast.placement import (
    SIXN_DRAIN_GUARD_MIN_DAYS,
    _HarvestBudget,
    _freeze_6n_dest,
    _run_sixn_purge_week,
)
from forecast.state import STAGE_STARVE, FacilityState, TankState

TODAY = date(2026, 10, 1)


def _mk_state(today=TODAY):
    return FacilityState(today, [
        TankState("OG3N-31", 31, "OG3N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG4N-41", 41, "OG4N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG6N-61", 61, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-67", 67, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-63", 63, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-69", 69, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-65", 65, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-71", 71, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
    ])


def _mk_control(**over):
    kw = dict(
        forecast_start=TODAY, horizon_weeks=10, scenario_name="t",
        max_feed_per_day_kg=34000.0, max_biomass_kg=3.8e6,
        max_harvest_per_week=51500.0, min_harvest_weight_g=3200.0,
        min_harvest_per_week=51500.0, min_tank_control=7000.0,
        default_hog_yield=0.81, facility_biomass_deviation_pct=0.005,
        handling_mortality_pct=0.01, sixn_growth=False,
        sixn_overdue_drain_weeks=3,
    )
    kw.update(over)
    return ControlParams(**kw)


def _week(state, queue, week_start, *, floor=None, cap=51500.0, used=0.0,
          control=None):
    harvests, transfers, warns = [], [], []
    budget = _HarvestBudget(cap=cap, used=used)
    _run_sixn_purge_week(
        state=state, pair_queue=queue, week_label="W", week_start_date=week_start,
        batch_meta={}, control=control or _mk_control(), harvest_events=harvests,
        transfer_events=transfers, warnings=warns, resting_pair=(61, 67),
        refill=False, budget=budget)
    return harvests, warns, budget


# --------------------------------------------------------------------------
# 1. fill dates
# --------------------------------------------------------------------------

def test_window_og_to_6n_stamps_the_scripted_week():
    s = _mk_state()
    s.tanks_by_id[31].assign("B44", 60000, 3700.0, 16.0, "SW")
    ev = ManualEvent(type="og_to_6n", week=4, from_tank=31,
                     destinations=[ManualDest(tank=63, count=23357.0)])
    wk4 = TODAY + timedelta(days=21)
    _apply_og_to_6n(s, ev, 1, event_date=wk4)
    assert s.tanks_by_id[63].stage == STAGE_STARVE
    assert s.sixn_fill_date[63] == wk4


def test_a_window_top_up_moves_the_clock_forward():
    # Same rule as the planner's _freeze_6n_dest: the latest fill wins, so the
    # newcomers never ship on a short purge.
    s = _mk_state()
    s.tanks_by_id[31].assign("B44", 60000, 3700.0, 16.0, "SW")
    first, second = TODAY + timedelta(days=14), TODAY + timedelta(days=21)
    _apply_og_to_6n(s, ManualEvent(type="og_to_6n", from_tank=31,
                                   destinations=[ManualDest(tank=63, count=10000.0)]),
                    1, event_date=first)
    _apply_og_to_6n(s, ManualEvent(type="og_to_6n", from_tank=31,
                                   destinations=[ManualDest(tank=63, count=10000.0)]),
                    2, event_date=second)
    assert s.sixn_fill_date[63] == second


def test_a_refused_destination_gets_no_clock():
    s = _mk_state()
    s.tanks_by_id[31].assign("B44", 60000, 3700.0, 16.0, "SW")
    s.tanks_by_id[63].assign("B40", 1000, 4000.0, 16.0, STAGE_STARVE)  # other batch
    _apply_og_to_6n(s, ManualEvent(type="og_to_6n", from_tank=31,
                                   destinations=[ManualDest(tank=63, count=10000.0)]),
                    1, event_date=TODAY)
    assert 63 not in s.sixn_fill_date


def test_an_unwitnessed_fill_counts_from_the_forecast_start_not_forever():
    """PR-hydrated 6N fish: no fill date. Week 1 they are NOT overdue (they
    used to read 1,000,000 days and drain past the limit on sight)."""
    s = _mk_state()
    s.tanks_by_id[63].assign("B44", 40000, 3700.0, 16.0, STAGE_STARVE)
    s.tanks_by_id[69].assign("B44", 15000, 3700.0, 16.0, STAGE_STARVE)
    _h, warns, _b = _week(s, [(63, 69)], TODAY)
    assert not any("OVERDUE" in w for w in warns)
    assert not any("1000000" in w for w in warns)


def test_an_unwitnessed_fill_still_becomes_overdue_three_weeks_in():
    # The anti-livelock rule keeps working: from the origin, 3 weeks later.
    s = _mk_state()
    s.tanks_by_id[63].assign("B44", 40000, 3700.0, 16.0, STAGE_STARVE)
    _h, warns, _b = _week(s, [(63, 69)], TODAY + timedelta(days=21))
    assert any("OVERDUE DRAIN" in w and "has purged 21d" in w for w in warns)


def test_a_window_fill_one_week_old_is_held_by_the_depuration_guard():
    # Recorded fills now reach the guard: a W45 window fill is not drained W46.
    s = _mk_state()
    s.tanks_by_id[65].assign("B45", 35000, 3400.0, 16.0, STAGE_STARVE)
    wk = TODAY + timedelta(days=35)
    _freeze_6n_dest(s, 65, fill_date=wk - timedelta(days=7))  # stage already STARVE
    s.sixn_fill_date[65] = wk - timedelta(days=7)
    h, warns, _b = _week(s, [(65, 71)], wk)
    assert h == [] and any("DEPURATION HOLD" in w for w in warns)
    assert SIXN_DRAIN_GUARD_MIN_DAYS > 7


def test_the_planner_clone_keeps_the_window_purge_clock():
    """Phase D rebuilds a fresh state from the window's hand-off; it used to
    copy the tanks only and drop the 6N fill dates the window recorded."""
    from forecast.config_io import load_config
    from forecast.placement import _clone_for_execution
    from pathlib import Path
    _c, _t, facility = load_config(Path(__file__).resolve().parent.parent / "config")
    s = FacilityState.from_facility_config(facility, today=TODAY)
    s.tanks_by_id[63].assign("B44", 55357, 3700.0, 16.0, STAGE_STARVE)
    wk44 = TODAY + timedelta(days=21)
    s.sixn_fill_date[63] = wk44
    s.today = TODAY + timedelta(days=42)          # the window walked on
    c = _clone_for_execution(s, facility)
    assert c.sixn_fill_date == {63: wk44}
    assert c.origin_date == TODAY
    assert c.tanks_by_id[63].count == 55357
