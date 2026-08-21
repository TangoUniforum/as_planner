"""6N purge fill: ONE BATCH, ONE TANK (operator, 2026-08-20).

PURGE mode has NO density ceiling — the harvest schedule bounds a depuration
tank, not kg/m3 (off-feed fish, not growing, gone within the ~2-week
rotation). So a purge fill lands ENTIRELY in one tank however dense it gets.

The sister (67/69/71) is NOT overflow capacity. It exists solely so a SECOND,
DIFFERENT batch needing harvest the same week is not mixed into an occupied
tank, because mixing destroys per-batch count fidelity at harvest. Spending
the sister on one batch's overflow burns the slot that separation needs.

This SUPERSEDES the earlier "sister-first / spill at the density cap" stage
these tests used to pin. In PRODUCTION mode 6N is an ordinary system and its
configured density cap applies normally — pinned below on both sides.
"""
from __future__ import annotations

from datetime import date

from forecast.models import BatchInput, ControlParams
from forecast.placement import (
    _make_room_into_6n,
    _run_sixn_purge_week,
    _sixn_fill_capacity_fish,
)
from forecast.state import FacilityState, TankState

TODAY = date(2026, 8, 3)
VOL = 1000.0                      # m3
TANK_CAP = 120.0                  # the fixture tanks' configured density


def _mk_state():
    return FacilityState(TODAY, [
        TankState("OG3N-31", 31, "OG3N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG6N-61", 61, "OG6N", VOL, 120.0, 1000.0, "OG"),
        TankState("OG6N-67", 67, "OG6N", VOL, 120.0, 1000.0, "OG"),
        TankState("OG6N-63", 63, "OG6N", VOL, 120.0, 1000.0, "OG"),
        TankState("OG6N-69", 69, "OG6N", VOL, 120.0, 1000.0, "OG"),
        TankState("OG6N-65", 65, "OG6N", VOL, 120.0, 1000.0, "OG"),
        TankState("OG6N-71", 71, "OG6N", VOL, 120.0, 1000.0, "OG"),
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


def _dens(state, tid):
    t = state.tanks_by_id[tid]
    return (t.count * t.avg_wt_g / 1000.0) / t.volume_m3 if not t.is_empty else 0.0


class TestCapacityHelper:
    def test_empty_tank(self):
        s = _mk_state()
        # 1000 m3 * 120 kg/m3 (the tank's CONFIG) = 120,000 kg -> 31,578 fish
        assert round(_sixn_fill_capacity_fish(s, 61, 3800.0)) == 31579

    def test_partially_filled(self):
        s = _mk_state()
        s.tanks_by_id[61].assign("B50", 10000, 3800.0, 16.0, "SW")
        assert round(_sixn_fill_capacity_fish(s, 61, 3800.0)) == 21579

    def test_at_cap(self):
        s = _mk_state()
        s.tanks_by_id[61].assign("B50", 31579, 3800.0, 16.0, "SW")
        assert _sixn_fill_capacity_fish(s, 61, 3800.0) == 0.0

    def test_purge_mode_has_no_ceiling(self):
        """purge=True -> unbounded, even on a tank already past its cap.

        The production-mode cases above keep the configured cap; this is the
        whole behavioural difference between the two modes in one assertion.
        """
        s = _mk_state()
        s.tanks_by_id[61].assign("B50", 31579, 3800.0, 16.0, "SW")
        assert _sixn_fill_capacity_fish(s, 61, 3800.0) == 0.0
        assert _sixn_fill_capacity_fish(s, 61, 3800.0, purge=True) == float("inf")


class TestRotationFillSpills:
    def test_fill_keeps_one_batch_in_one_tank(self):
        """One batch, one big source: the whole fill lands in the MAIN and
        the sister stays EMPTY, reserved for a different batch.

        40,000 fish at 3,800 g is 152,000 kg in a 1,000 m3 tank = 152 kg/m3,
        well past the fixture's 120 configured cap. That is CORRECT in purge:
        the cap does not apply, and splitting to hold 120 would spend the
        sister that a genuinely different batch needs at harvest.
        """
        s = _mk_state()
        s.tanks_by_id[31].assign("B50", 40000, 3800.0, 16.0, "SW")
        meta = {"B50": BatchInput(
            batch_id="B50", input_date=TODAY, input_count=400000,
            tran_sf_date=None, tran_og_date=None, tran_og_count=None,
            tran_og_avg_wt_g=None, tran_og_cv=16.0, fcr_model="1.21",
            fw_correction=1.0, sgr_correction=1.0)}
        harvests, transfers, warns = [], [], []
        _run_sixn_purge_week(
            state=s, pair_queue=[(63, 69)], week_label="2026-W40",
            week_start_date=TODAY, batch_meta=meta, control=_mk_control(),
            harvest_events=harvests, transfer_events=transfers,
            warnings=warns, move_in_target=40000.0, resting_pair=(61, 67),
            refill=True,
        )
        m, sis = s.tanks_by_id[61], s.tanks_by_id[67]
        assert not m.is_empty
        assert sis.is_empty, "sister must stay free for a DIFFERENT batch"
        assert _dens(s, 61) > TANK_CAP          # no ceiling in purge
        # nothing lost: every fish is in the main or still in grow-out
        assert m.count + s.tanks_by_id[31].count == 40000

    def test_fill_stops_when_pair_holds_a_foreign_batch(self):
        """Both pair tanks hold a DIFFERENT batch: the fill moves nothing.

        Renamed 2026-08-20 — this never tested the density cap. It passes
        because B49 occupies both tanks and a B50 fill may not mix into
        them, which is the count-fidelity rule, not a capacity limit. With
        purge uncapped the old name asserted a mechanism that no longer
        exists, while the behaviour it actually pins matters MORE now.
        """
        s = _mk_state()
        s.tanks_by_id[61].assign("B49", 25000, 3800.0, 16.0, "SW")
        s.tanks_by_id[67].assign("B49", 25000, 3800.0, 16.0, "SW")
        s.tanks_by_id[31].assign("B50", 40000, 3800.0, 16.0, "SW")
        meta = {"B50": BatchInput(
            batch_id="B50", input_date=TODAY, input_count=400000,
            tran_sf_date=None, tran_og_date=None, tran_og_count=None,
            tran_og_avg_wt_g=None, tran_og_cv=16.0, fcr_model="1.21",
            fw_correction=1.0, sgr_correction=1.0)}
        harvests, transfers, warns = [], [], []
        _run_sixn_purge_week(
            state=s, pair_queue=[(63, 69)], week_label="2026-W40",
            week_start_date=TODAY, batch_meta=meta, control=_mk_control(),
            harvest_events=harvests, transfer_events=transfers,
            warnings=warns, move_in_target=40000.0, resting_pair=(61, 67),
            refill=True,
        )
        assert s.tanks_by_id[31].count == 40000         # nothing moved
        assert _dens(s, 61) <= TANK_CAP + 0.01


class TestMakeRoomSplit:
    def test_dump_keeps_one_batch_in_one_tank(self):
        """A whole-tank make-room dump lands in ONE 6N tank, sister untouched.

        33,000 at 3,800 g = 125,400 kg in 1,000 m3 = 125 kg/m3, past the 120
        configured cap and correct in purge. Previously this split across the
        pair to stay under the cap, which is what burned sister capacity.
        """
        s = _mk_state()
        src = s.tanks_by_id[31]
        src.assign("B50", 33000, 3800.0, 16.0, "SW")
        warns: list[str] = []
        ok = _make_room_into_6n(
            s, src, TODAY, (61, 67), [], warns, "2026-W40",
            reason="test", is_purge=True)
        assert ok and src.is_empty
        assert not s.tanks_by_id[61].is_empty
        assert s.tanks_by_id[67].is_empty, "sister reserved for another batch"
        assert s.tanks_by_id[61].count == 33000
        assert _dens(s, 61) > TANK_CAP          # no ceiling in purge
        # the filled destination is stamped for the depuration-hold guard
        assert s.sixn_fill_date.get(61) == TODAY
        assert 67 not in s.sixn_fill_date

    def test_no_drop_overflow_when_capacity_short(self):
        """Only one usable slot and the dump exceeds its 95-capacity: the
        move still fully vacates the source (an overloaded purge tank beats
        a dropped arrival)."""
        s = _mk_state()
        # every 6N tank except 71 is held by foreign batches
        for tid, b in ((61, "B41"), (67, "B42"), (63, "B43"),
                       (69, "B44"), (65, "B45")):
            s.tanks_by_id[tid].assign(b, 20000, 3800.0, 16.0, "SW")
        src = s.tanks_by_id[31]
        src.assign("B50", 33000, 3800.0, 16.0, "SW")
        warns: list[str] = []
        ok = _make_room_into_6n(
            s, src, TODAY, (61, 67), [], warns, "2026-W40",
            reason="test", is_purge=True)
        assert ok and src.is_empty
        assert s.tanks_by_id[71].count == 33000         # over cap, by design
