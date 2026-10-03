"""The floor beats the cap -- and its sibling fix (operator's October plan, 2026-10-03).

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
        refill=False, budget=budget, week_floor=floor)
    return harvests, warns, budget


# --------------------------------------------------------------------------
# 2. floor beats cap
# --------------------------------------------------------------------------

def _w52_state():
    """The October 2026-W52 shape: 33,157 already harvested, the pair's other
    tank holds 18,389 and only 18,156 fit under the (carry-reduced) limit."""
    s = _mk_state()
    s.tanks_by_id[69].assign("B47", 18389, 3800.0, 16.0, STAGE_STARVE)
    return s


def test_the_floor_wins_over_the_limit():
    s = _w52_state()
    h, warns, b = _week(s, [(63, 69)], TODAY, floor=51500.0, cap=51313.0,
                        used=33157.0)
    assert [x.source_tank_id for x in h] == [69]
    assert round(b.used) == 51546
    assert any("FLOOR OVER LIMIT" in w for w in warns)
    assert not any("HARVEST LIMIT" in w for w in warns)


def test_without_a_floor_the_limit_hold_is_unchanged():
    s = _w52_state()
    h, warns, b = _week(s, [(63, 69)], TODAY, floor=None, cap=51313.0,
                        used=33157.0)
    assert h == [] and any("HARVEST LIMIT" in w for w in warns)


def test_a_week_already_at_its_floor_keeps_the_hold():
    s = _w52_state()
    h, warns, b = _week(s, [(63, 69)], TODAY, floor=30000.0, cap=51313.0,
                        used=33157.0)
    assert h == [] and any("HARVEST LIMIT" in w for w in warns)


def test_a_later_tank_that_fits_rescues_the_floor_instead():
    """Two candidates: 63 (25,000) would breach, 69 (18,300) fits and still
    reaches the floor -- 63 is held, 69 drains, the limit holds."""
    s = _mk_state()
    s.tanks_by_id[63].assign("B47", 25000, 3800.0, 16.0, STAGE_STARVE)
    s.tanks_by_id[69].assign("B47", 18300, 3800.0, 16.0, STAGE_STARVE)
    h, warns, b = _week(s, [(63, 69)], TODAY, floor=51000.0, cap=51500.0,
                        used=33157.0)
    assert [x.source_tank_id for x in h] == [69]
    assert b.used <= 51500.0
    assert any("HARVEST LIMIT" in w and "OG6N-63" in w for w in warns)


def test_of_two_breaching_tanks_the_smaller_overshoot_drains():
    s = _mk_state()
    s.tanks_by_id[63].assign("B47", 25000, 3800.0, 16.0, STAGE_STARVE)
    s.tanks_by_id[69].assign("B47", 20000, 3800.0, 16.0, STAGE_STARVE)
    h, warns, b = _week(s, [(63, 69)], TODAY, floor=51500.0, cap=51500.0,
                        used=33157.0)
    assert [x.source_tank_id for x in h] == [69]       # 53,157 not 58,157
    assert round(b.used) == 53157

