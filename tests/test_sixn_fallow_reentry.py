"""The 3-pair fallow rotation must be able to RE-ENTER after a degrade.

Operator topology: 2 pairs purging + 1 pair fallow, filled Wed / harvested Fri,
so the move-in NEVER goes into the pair drained the same week. When the forecast
opens with all three pairs stocked (the common shape after a manual starting
window that stages into every pair) there is no fallow slot, and the handler
deliberately degrades to refill-in-place. The startup warning says that lasts
"until a pair empties".

It did not. `new_resting` only re-arms when `resting_pair` is already non-None,
so once None it stayed None: the degrade outlived its cause for the entire run.
Measured on the 7.29.26 PR + the operator's 2026-07-31 window: all 75 purge
weeks ran refill-in-place, resting_pair None throughout, fill pair == harvest
pair every single week.

These tests pin the re-entry, and pin that it does NOT fire where it must not
(no fallow pair available; winddown, where there is no fill to place).
"""
from __future__ import annotations

from datetime import date

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
    out = _run_sixn_purge_week(
        state=state, pair_queue=queue, week_label="2026-W40",
        week_start_date=TODAY, batch_meta=_META, control=_mk_control(),
        harvest_events=[], transfer_events=[], warnings=warns,
        move_in_target=target, resting_pair=resting, refill=refill,
    )
    return out, warns


class TestFallowReEntry:
    def test_reenters_when_a_pair_is_fallow(self):
        """resting_pair None (the sticky degrade) but 65/71 IS empty: the
        rotation must adopt it, fill THERE, and hand back the drained pair."""
        s = _mk_state()
        s.tanks_by_id[61].assign("B49", 20000, 3800.0, 16.0, "SW")
        s.tanks_by_id[63].assign("B49", 20000, 3800.0, 16.0, "SW")
        s.tanks_by_id[31].assign("B50", 40000, 3800.0, 16.0, "SW")
        queue = [(61, 67), (63, 69)]
        out, warns = _run(s, queue, None)

        assert out == (61, 67)                      # drained pair now rests
        assert s.tanks_by_id[61].is_empty           # harvested
        # the fill went to the FALLOW pair, not the pair drained this week
        assert not s.tanks_by_id[65].is_empty
        assert queue[-1] == (65, 71)                # filled pair re-queued
        assert any("RE-ENTERED" in w for w in warns)

    def test_no_reentry_when_every_pair_is_stocked(self):
        """The documented degrade is preserved when there is genuinely no
        fallow slot: fill in place, still no resting pair."""
        s = _mk_state()
        for tid in (61, 63, 65):
            s.tanks_by_id[tid].assign("B49", 20000, 3800.0, 16.0, "SW")
        s.tanks_by_id[31].assign("B50", 40000, 3800.0, 16.0, "SW")
        queue = [(61, 67), (63, 69), (65, 71)]
        out, warns = _run(s, queue, None)

        assert out is None                          # still degraded
        assert queue[-1] == (61, 67)                # refilled in place
        assert not any("RE-ENTERED" in w for w in warns)

    def test_no_reentry_during_winddown(self):
        """refill=False: nothing is filled, so adopting a fallow pair would
        only reshuffle the drain order. Must stay a no-op."""
        s = _mk_state()
        s.tanks_by_id[61].assign("B49", 20000, 3800.0, 16.0, "SW")
        s.tanks_by_id[63].assign("B49", 20000, 3800.0, 16.0, "SW")
        queue = [(61, 67), (63, 69)]
        out, warns = _run(s, queue, None, refill=False)

        assert out is None
        assert queue[-1] == (61, 67)                # harvested pair re-queued
        assert not any("RE-ENTERED" in w for w in warns)

    def test_healthy_rotation_is_unchanged(self):
        """With a resting pair already set the behaviour is exactly as before:
        fill the resting pair, hand back the drained one."""
        s = _mk_state()
        s.tanks_by_id[61].assign("B49", 20000, 3800.0, 16.0, "SW")
        s.tanks_by_id[63].assign("B49", 20000, 3800.0, 16.0, "SW")
        s.tanks_by_id[31].assign("B50", 40000, 3800.0, 16.0, "SW")
        queue = [(61, 67), (63, 69)]
        out, warns = _run(s, queue, (65, 71))

        assert out == (61, 67)
        assert not s.tanks_by_id[65].is_empty
        assert queue[-1] == (65, 71)
        assert not any("RE-ENTERED" in w for w in warns)
