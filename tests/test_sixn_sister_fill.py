"""Sister-first 6N fill stage (operator rule 2, stage 1).

No 6N purge tank is STOCKED past the structural 95 kg/m3 fill cap — the
overflow continues into the pair's other tank (the idle sister) instead of
overloading the main (audit: mains rode 128-141 kg/m3 while sisters sat
empty 80-90% of purge weeks). The no-drop make-room may still overflow the
LAST slot when total free 6N capacity is short — losing an arrival is worse.
"""
from __future__ import annotations

from datetime import date

from forecast.models import BatchInput, ControlParams
from forecast.placement import (
    SIXN_FILL_DENSITY_CAP_KG_M3,
    _make_room_into_6n,
    _run_sixn_purge_week,
    _sixn_fill_capacity_fish,
)
from forecast.state import FacilityState, TankState

TODAY = date(2026, 8, 3)
VOL = 1000.0                      # m3 -> 95t cap -> 25,000 fish at 3.8 kg


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
        harvest_target_per_week=50000.0,
    )
    kw.update(over)
    return ControlParams(**kw)


def _dens(state, tid):
    t = state.tanks_by_id[tid]
    return (t.count * t.avg_wt_g / 1000.0) / t.volume_m3 if not t.is_empty else 0.0


class TestCapacityHelper:
    def test_empty_tank(self):
        s = _mk_state()
        # 1000 m3 * 95 kg/m3 = 95,000 kg -> 25,000 fish at 3,800 g
        assert _sixn_fill_capacity_fish(s, 61, 3800.0) == 25000.0

    def test_partially_filled(self):
        s = _mk_state()
        s.tanks_by_id[61].assign("B50", 10000, 3800.0, 16.0, "SW")
        assert _sixn_fill_capacity_fish(s, 61, 3800.0) == 15000.0

    def test_at_cap(self):
        s = _mk_state()
        s.tanks_by_id[61].assign("B50", 26000, 3800.0, 16.0, "SW")
        assert _sixn_fill_capacity_fish(s, 61, 3800.0) == 0.0


class TestRotationFillSpills:
    def test_fill_spills_into_sister_at_95(self):
        """One batch, one big source: the fill lands main-first, tops the
        main at the 95 cap, and the overflow continues into the SISTER —
        neither tank over 95."""
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
        assert not m.is_empty and not sis.is_empty      # sister engaged
        assert _dens(s, 61) <= SIXN_FILL_DENSITY_CAP_KG_M3 + 0.01
        assert _dens(s, 67) <= SIXN_FILL_DENSITY_CAP_KG_M3 + 0.01
        # everything the fill moved is in the pair (nothing lost)
        assert m.count + sis.count + s.tanks_by_id[31].count == 40000

    def test_fill_stops_when_pair_at_cap(self):
        """Both pair tanks at the 95 cap: the fill moves nothing (surplus
        waits in grow-out) — never an overload."""
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
        assert _dens(s, 61) <= SIXN_FILL_DENSITY_CAP_KG_M3 + 0.01


class TestMakeRoomSplit:
    def test_dump_splits_across_pair(self):
        s = _mk_state()
        src = s.tanks_by_id[31]
        src.assign("B50", 33000, 3800.0, 16.0, "SW")
        warns: list[str] = []
        ok = _make_room_into_6n(
            s, src, TODAY, (61, 67), [], warns, "2026-W40",
            reason="test", is_purge=True)
        assert ok and src.is_empty
        assert not s.tanks_by_id[61].is_empty and not s.tanks_by_id[67].is_empty
        assert _dens(s, 61) <= SIXN_FILL_DENSITY_CAP_KG_M3 + 0.01
        assert _dens(s, 67) <= SIXN_FILL_DENSITY_CAP_KG_M3 + 0.01
        assert s.tanks_by_id[61].count + s.tanks_by_id[67].count == 33000
        # both destinations stamped for the depuration-hold guard
        assert s.sixn_fill_date.get(61) == TODAY
        assert s.sixn_fill_date.get(67) == TODAY

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
