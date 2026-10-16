"""6N TOP-UP after a make-room dump (2026-10-16).

Operator 2026-10-16, on the 580 t plan: "do the code change to top up the 6N
pair". The short weeks (2026-W52 on the 10.11 PR: 29,251 fish against a 41,948
floor) were a pair that already held a make-room dump (13,697 fish) being
topped up to max_h - 2 x dump: move-size rules net the dump out of max_h, and
the level-drains clamp then subtracted it a second time. Mature fish were
there (31,907 + 22,922 B45 in OG). The fill must top the pair up to the band.
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

from forecast.models import ControlParams
from forecast.placement import _freeze_6n_dest, _run_sixn_purge_week
from forecast.state import STAGE_STARVE, FacilityState, TankState

TODAY = date(2026, 12, 7)            # a Monday
DUMP = 13697.0


def _state():
    s = FacilityState(TODAY, [
        TankState("OG3N-31", 31, "OG3N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG3S-36", 36, "OG3S", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG6N-61", 61, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-67", 67, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-63", 63, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-69", 69, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-65", 65, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-71", 71, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
    ])
    s.tanks_by_id[31].assign("B45", 31907, 3683.0, 16.0, "SW")
    s.tanks_by_id[36].assign("B45", 38475, 3658.0, 16.0, "SW")
    # last week's make-room dump into the resting pair's main
    s.tanks_by_id[65].assign("B45", DUMP, 3543.0, 16.0, STAGE_STARVE)
    _freeze_6n_dest(s, 65, fill_date=TODAY - timedelta(days=5), source_tank_id=34)
    # the pair draining this week
    s.tanks_by_id[61].assign("B45", 44000, 3500.0, 16.0, STAGE_STARVE)
    _freeze_6n_dest(s, 61, fill_date=TODAY - timedelta(days=12), source_tank_id=32)
    return s


def _control(level_drains=True):
    return ControlParams(
        forecast_start=TODAY, horizon_weeks=10, scenario_name="t",
        max_feed_per_day_kg=34000.0, max_biomass_kg=3.8e6,
        max_harvest_per_week=42948.0, min_harvest_weight_g=3000.0,
        min_harvest_per_week=41948.0, min_tank_control=7000.0,
        default_hog_yield=0.81, facility_biomass_deviation_pct=0.005,
        handling_mortality_pct=0.01, sixn_growth=False,
        move_size_rules=True, min_transfer_count=7000.0,
        sixn_level_drains=level_drains, sixn_max_source_tanks=2)


def _pair_after_fill(level_drains=True):
    s = _state()
    s.sixn_max_source_tanks = 2
    meta = {"B45": SimpleNamespace(batch_id="B45", input_date=date(2024, 6, 1))}
    h, t, w = [], [], []
    _run_sixn_purge_week(
        state=s, pair_queue=[(61, 67), (63, 69)], week_label="2026-W50",
        week_start_date=TODAY, batch_meta=meta, control=_control(level_drains),
        harvest_events=h, transfer_events=t, warnings=w,
        move_in_target=42948.0, resting_pair=(65, 71), refill=True,
        weekly_min=41948.0, weekly_max=42948.0)
    return s.tanks_by_id[65].count + s.tanks_by_id[71].count, w


def test_pair_with_a_dump_is_topped_up_to_the_floor():
    held, w = _pair_after_fill()
    assert held >= 41948 * 0.999, (held, [x for x in w if "65" in x])
    assert held <= 42948 + 1, held


def test_without_level_drains_the_fill_already_reached_the_floor():
    """Negative control on the other branch: the move-size netting alone was
    always right; only the level-drains clamp counted the dump twice."""
    held, _ = _pair_after_fill(level_drains=False)
    assert held >= 41948 * 0.999, held
