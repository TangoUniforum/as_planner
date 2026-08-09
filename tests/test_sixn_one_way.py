"""R7 — 6N one-way commitment (operator rule 3).

Fish moved into a 6N depuration tank (stage STARVE) may NEVER transfer out;
only harvest empties the tank. Fail-safes in events.Transfer/Grade (engine),
a hard-block in the manual window's og_transfer, and a checklist gate.
Measured prevalence on 9 audit runs: ZERO depuration-era outbound moves —
this is enforcement against future passes and operator manual events, not a
behavior change.
"""
from __future__ import annotations

from datetime import date

from forecast.analysis import _gate_sixn_one_way
from forecast.events import Grade, Harvest, TankAllocation, Transfer
from forecast.manual_events import (
    ManualDest,
    ManualEvent,
    TYPE_OG_TRANSFER,
    _apply_og_transfer,
)
from forecast.state import STAGE_STARVE, FacilityState, TankState
from forecast.tiers import sixn_exit_allowed

TODAY = date(2026, 8, 3)


def _mk_state():
    return FacilityState(TODAY, [
        TankState("OG3N-31", 31, "OG3N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG5N-51", 51, "OG5N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG6N-61", 61, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-67", 67, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
    ])


class TestPredicate:
    def test_depurating_sixn_is_locked(self):
        assert not sixn_exit_allowed("OG6N", "STARVE")

    def test_production_mode_sixn_moves_freely(self):
        assert sixn_exit_allowed("OG6N", "SW")

    def test_non_sixn_starve_moves_freely(self):
        # production-mode in-place starvation on a grow-out tank is NOT the
        # 6N commitment — different rule, not blocked here
        assert sixn_exit_allowed("OG5N", "STARVE")


class TestTransferFailSafe:
    def test_refuses_transfer_out_of_depurating_sixn(self):
        s = _mk_state()
        s.tanks_by_id[61].assign("B50", 30000, 3800.0, 16.0, STAGE_STARVE)
        tr = Transfer(batch_id="B50", event_date=TODAY, source_tank_id=61,
                      destinations=[TankAllocation(31, 30000, 3800.0, 16.0)])
        warns = tr.apply(s)
        assert any("R7" in w for w in warns)
        assert tr.count_transferred == 0
        assert s.tanks_by_id[61].count == 30000       # state unchanged
        assert s.tanks_by_id[31].is_empty

    def test_production_mode_sixn_transfer_allowed(self):
        s = _mk_state()
        s.tanks_by_id[61].assign("B50", 30000, 3800.0, 16.0, "SW")
        tr = Transfer(batch_id="B50", event_date=TODAY, source_tank_id=61,
                      destinations=[TankAllocation(31, 10000, 3800.0, 16.0)])
        warns = tr.apply(s)
        assert not any("R7" in w for w in warns)
        assert tr.count_transferred == 10000

    def test_harvest_from_depurating_sixn_still_allowed(self):
        s = _mk_state()
        s.tanks_by_id[61].assign("B50", 30000, 3800.0, 16.0, STAGE_STARVE)
        hv = Harvest(batch_id="B50", event_date=TODAY, source_tank_id=61,
                     count=30000, avg_wt_g=3800.0)
        warns = hv.apply(s)
        assert not warns and s.tanks_by_id[61].is_empty

    def test_grade_from_depurating_sixn_refused(self):
        s = _mk_state()
        s.tanks_by_id[61].assign("B50", 30000, 3800.0, 16.0, STAGE_STARVE)
        g = Grade(batch_id="B50", event_date=TODAY, source_tank_ids=[61],
                  destinations=[TankAllocation(31, 15000, 3400.0, 16.0),
                                TankAllocation(51, 15000, 4200.0, 16.0)])
        warns = g.apply(s)
        assert any("R7" in w for w in warns)
        assert s.tanks_by_id[61].count == 30000


class TestManualWindowBlock:
    def test_og_transfer_from_sixn_hard_blocked_any_stage(self):
        """The manual window edits the PRE-forecast state, where 6N always
        means depuration — blocked regardless of the hydrated stage."""
        s = _mk_state()
        s.tanks_by_id[61].assign("B50", 30000, 3800.0, 16.0, "SW")
        ev = ManualEvent(type=TYPE_OG_TRANSFER, batch="B50", from_tank=61,
                         destinations=[ManualDest(tank=31, count=None)])
        warns = _apply_og_transfer(s, ev, 1)
        assert any("R7" in w for w in warns)
        assert s.tanks_by_id[61].count == 30000
        assert s.tanks_by_id[31].is_empty


class TestGate:
    def test_pass_warn_fail(self):
        assert _gate_sixn_one_way({"sixn_outbound_purge": 0})[0] == "PASS"
        st, d = _gate_sixn_one_way({"sixn_outbound_purge": 2})
        assert st == "FAIL" and "one-way" in d
        assert _gate_sixn_one_way({})[0] == "N/A"
