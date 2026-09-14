"""A SHALLOW 6N purge queue must re-seed its missing purging pair.

Operator topology: 2 pairs purging + 1 pair fallow, filled Wed / harvested Fri,
fish purge ~2 weeks (see test_sixn_fallow_reentry.py). When the PR hands over
only ONE stocked 6N pair and two empty ones (the 8/31 PR with no manual events:
63/69 stocked, 61/67 and 65/71 empty) the queue holds one pair where the
rotation needs two. Week 1 drains it and fills the resting pair; from then on
the pair at the front is ALWAYS the one filled the week before, the depuration
hold refuses it (it would be a 1-week purge) week after week, the third pair is
never used, and 6N harvest happens only on the 3-week overdue drains --
measured: 40 of 85 weeks with no harvest and the plan at ~4x the biomass cap.

Fix (engine patch biomass-01, operator-approved 2026-09-13): when the front
pair is too young to drain and a spare empty pair exists, run a SEED week --
fill the spare pair, harvest nothing (the front could not be drained anyway),
keep the resting pair -- so the queue regains its second purging pair and the
front drains at its full 2-week purge next week. The same shape as the
empty-queue bootstrap already in _run_sixn_purge_week.

These tests pin the seed, and pin that it does NOT fire where it must not.
"""
from __future__ import annotations

from datetime import date, timedelta

from forecast.models import BatchInput, ControlParams
from forecast.placement import _run_sixn_purge_week
from forecast.state import FacilityState, TankState

TODAY = date(2026, 8, 3)


def _mk_state():
    return FacilityState(TODAY, [
        TankState("OG3N-31", 31, "OG3N", 1000.0, 95.0, 1000.0, "OG"),
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
        max_harvest_per_week=60000.0, min_harvest_weight_g=3500.0,
        min_harvest_per_week=30000.0, min_tank_control=7000.0,
        default_hog_yield=0.81, facility_biomass_deviation_pct=0.005,
        handling_mortality_pct=0.01, sixn_growth=False,
    )
    kw.update(over)
    return ControlParams(**kw)


_META = {"B50": BatchInput(
    batch_id="B50", input_date=TODAY, input_count=400000,
    tran_sf_date=None, tran_og_date=None, tran_og_count=None,
    tran_og_avg_wt_g=None, tran_og_cv=16.0, fcr_model="1.21",
    fw_correction=1.0, sgr_correction=1.0)}


def _run(state, queue, resting, refill=True, target=30000.0):
    warns: list[str] = []
    harvests: list = []
    out = _run_sixn_purge_week(
        state=state, pair_queue=queue, week_label="2026-W40",
        week_start_date=TODAY, batch_meta=_META, control=_mk_control(),
        harvest_events=harvests, transfer_events=[], warnings=warns,
        move_in_target=target, resting_pair=resting, refill=refill,
    )
    return out, warns, harvests


def _shallow(front_filled_days_ago):
    """One purging pair (61/67, B49 in 61), resting 65/71, spare 63/69 empty,
    a market-ready cohort in 31 to fill from."""
    s = _mk_state()
    s.tanks_by_id[61].assign("B49", 20000, 3800.0, 16.0, "SW")
    s.tanks_by_id[31].assign("B50", 40000, 3800.0, 16.0, "SW")
    if front_filled_days_ago is not None:
        s.sixn_fill_date = {61: TODAY - timedelta(days=front_filled_days_ago)}
    return s


def _seeded(warns):
    return any("SHALLOW-QUEUE SEED" in w for w in warns)


class TestShallowQueueSeed:
    def test_a_young_front_pair_seeds_the_spare_pair(self):
        """The measured shape: the only purging pair was filled last week."""
        s = _shallow(front_filled_days_ago=3)
        queue = [(61, 67)]
        out, warns, harvests = _run(s, queue, (65, 71))

        assert _seeded(warns), warns
        assert not s.tanks_by_id[61].is_empty       # front NOT drained (too young)
        assert not harvests                         # a seed week harvests nothing
        # the SPARE pair was filled, not the resting pair
        assert not (s.tanks_by_id[63].is_empty and s.tanks_by_id[69].is_empty)
        assert s.tanks_by_id[65].is_empty and s.tanks_by_id[71].is_empty
        # the queue regains its second purging pair behind the front
        assert queue == [(61, 67), (63, 69)]
        assert out == (65, 71)                      # the resting pair stays fallow

    def test_an_old_enough_front_pair_rotates_normally(self):
        """NEGATIVE CONTROL: a front pair past the depuration hold drains as
        before -- no seed."""
        s = _shallow(front_filled_days_ago=14)
        queue = [(61, 67)]
        out, warns, harvests = _run(s, queue, (65, 71))

        assert not _seeded(warns)
        assert s.tanks_by_id[61].is_empty           # drained
        assert harvests
        assert not s.tanks_by_id[65].is_empty       # filled the resting pair
        assert out == (61, 67)                      # drained pair rests next

    def test_a_pr_hydrated_front_pair_is_old_enough(self):
        """No recorded fill date = fish already purging at the PR close: the
        depuration hold treats them as old enough, and so must the seed."""
        s = _shallow(front_filled_days_ago=None)
        queue = [(61, 67)]
        out, warns, harvests = _run(s, queue, (65, 71))

        assert not _seeded(warns)
        assert s.tanks_by_id[61].is_empty
        assert out == (61, 67)

    def test_no_seed_without_a_spare_empty_pair(self):
        """If the third pair is not empty there is nothing to seed: the
        previous behaviour (the depuration hold keeps the young pair) stands."""
        s = _shallow(front_filled_days_ago=3)
        s.tanks_by_id[63].assign("B49", 15000, 3800.0, 16.0, "SW")
        queue = [(61, 67)]
        out, warns, harvests = _run(s, queue, (65, 71))

        assert not _seeded(warns)
        assert any("DEPURATION HOLD" in w for w in warns)
        assert not s.tanks_by_id[61].is_empty       # still held, as before

    def test_no_seed_during_winddown(self):
        """refill=False: nothing is filled in winddown, so there is nothing to
        seed -- the fix must not move fish into 6N there."""
        s = _shallow(front_filled_days_ago=3)
        queue = [(61, 67)]
        out, warns, harvests = _run(s, queue, (65, 71), refill=False)

        assert not _seeded(warns)
        assert s.tanks_by_id[63].is_empty and s.tanks_by_id[69].is_empty

    def test_no_seed_without_a_resting_pair(self):
        """The degraded refill-in-place mode (no fallow pair tracked) is left
        to the fallow RE-ENTRY rule, not this one."""
        s = _shallow(front_filled_days_ago=3)
        queue = [(61, 67)]
        out, warns, harvests = _run(s, queue, None)

        assert not _seeded(warns)
