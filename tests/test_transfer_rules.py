"""Facility transfer rules (tiers.py R1-R6) — unit truth table + the events.py
warn-and-refuse backbone + manual-events reject-at-entry.

The rules (operator-defined physical constraints, judged on TANK AVG weight):
  R1 arrivals enter ONLY the entry tier (OG1/2)
  R2 entry -> OG3-6 forward at any weight
  R3 entry -> entry only while the SOURCE tank avg < 1000 g
  R4 never backward (non-entry -> entry, any weight)
  R5 no harvest / 6N staging FROM entry-tier tanks
  R6 >= 1 kg fish MAY remain in entry tanks (no force-evict)

State fixtures follow the tiny-TankState pattern of tests/test_lns_placement.py
(no workbook needed — these must run everywhere).
"""
from __future__ import annotations

from datetime import date

from forecast.state import FacilityState, TankState
from forecast.tiers import (
    ENTRY_SPLIT_MAX_WT_G,
    ENTRY_SYSTEMS,
    harvest_allowed,
    is_entry,
    move_allowed,
)


TODAY = date(2026, 8, 3)


def _mk_state():
    """Tiny facility: 2 entry-tier tanks, 2 grow-out tanks, 1 6N tank."""
    return FacilityState(TODAY, [
        TankState("OG1N-1", 1, "OG1N", 200.0, 95.0, 1000.0, "OG"),
        TankState("OG1S-1", 2, "OG1S", 200.0, 95.0, 1000.0, "OG"),
        TankState("OG3N-1", 3, "OG3N", 200.0, 95.0, 1000.0, "OG"),
        TankState("OG4N-1", 4, "OG4N", 200.0, 95.0, 1000.0, "OG"),
        TankState("OG6N-61", 61, "OG6N", 200.0, 120.0, 1000.0, "OG"),
    ])


def _snapshot(state):
    return {tid: (t.batch_id, round(t.count, 3), round(t.avg_wt_g, 3))
            for tid, t in state.tanks_by_id.items()}


# ---------------------------------------------------------------------------
# tiers.py truth table
# ---------------------------------------------------------------------------

class TestTiersTruthTable:
    def test_entry_membership(self):
        assert ENTRY_SYSTEMS == {"OG1N", "OG1S", "OG2N", "OG2S"}
        for s in ENTRY_SYSTEMS:
            assert is_entry(s)
        for s in ("OG3N", "OG4S", "OG5N", "OG6N", "OG6S"):
            assert not is_entry(s)

    def test_entry_to_entry_below_1kg_ok(self):
        ok, why = move_allowed("OG1N", "OG2S", 999.0)
        assert ok and why == ""

    def test_entry_to_entry_at_or_above_1kg_refused(self):
        ok, why = move_allowed("OG1N", "OG1S", ENTRY_SPLIT_MAX_WT_G)
        assert not ok and "R3" in why
        ok, why = move_allowed("OG2N", "OG2S", 1500.0)
        assert not ok and "R3" in why

    def test_entry_to_growout_any_weight_ok(self):
        for wt in (200.0, 999.0, 1000.0, 4500.0):
            for dst in ("OG3N", "OG4S", "OG5N", "OG6S", "OG6N"):
                ok, why = move_allowed("OG1N", dst, wt)
                assert ok, (dst, wt, why)

    def test_growout_to_entry_refused_any_weight(self):
        # R4 includes sub-1kg fish — backward is NEVER legal.
        for wt in (200.0, 999.0, 2500.0):
            for src in ("OG3N", "OG5S", "OG6S"):
                ok, why = move_allowed(src, "OG1N", wt)
                assert not ok and "R4" in why, (src, wt)

    def test_growout_to_growout_ok(self):
        for wt in (500.0, 3000.0):
            ok, why = move_allowed("OG3N", "OG5S", wt)
            assert ok, why

    def test_harvest_allowed(self):
        for s in ENTRY_SYSTEMS:
            assert not harvest_allowed(s)          # R5
        for s in ("OG3N", "OG4S", "OG5N", "OG6N", "OG6S"):
            assert harvest_allowed(s)


# ---------------------------------------------------------------------------
# events.py warn-and-refuse backbone (state must be unchanged on refusal)
# ---------------------------------------------------------------------------

class TestEventRefusals:
    def test_backward_transfer_refused_state_unchanged(self):
        from forecast.events import TankAllocation, Transfer
        state = _mk_state()
        state.tanks_by_id[3].assign("B1", 30000, 800.0, 14.0, "SW")  # OG3N, sub-1kg
        before = _snapshot(state)
        ev = Transfer(batch_id="B1", event_date=TODAY, source_tank_id=3,
                      destinations=[TankAllocation(tank_id=1, count=10000,
                                                   avg_wt_g=800.0, cv_pct=14.0)])
        warns = ev.apply(state)
        assert any("R4" in w for w in warns)
        assert ev.count_transferred == 0
        assert _snapshot(state) == before

    def test_entry_transfer_over_limit_refused_state_unchanged(self):
        from forecast.events import TankAllocation, Transfer
        state = _mk_state()
        state.tanks_by_id[1].assign("B1", 30000, 1200.0, 14.0, "SW")  # OG1N >=1kg
        before = _snapshot(state)
        ev = Transfer(batch_id="B1", event_date=TODAY, source_tank_id=1,
                      destinations=[TankAllocation(tank_id=2, count=10000,
                                                   avg_wt_g=1200.0, cv_pct=14.0)])
        warns = ev.apply(state)
        assert any("INV-4" in w or "R3" in w for w in warns)
        assert ev.count_transferred == 0
        assert _snapshot(state) == before

    def test_entry_transfer_below_limit_applies(self):
        from forecast.events import TankAllocation, Transfer
        state = _mk_state()
        state.tanks_by_id[1].assign("B1", 30000, 800.0, 14.0, "SW")
        ev = Transfer(batch_id="B1", event_date=TODAY, source_tank_id=1,
                      destinations=[TankAllocation(tank_id=2, count=10000,
                                                   avg_wt_g=800.0, cv_pct=14.0)])
        warns = ev.apply(state)
        assert warns == []
        assert ev.count_transferred == 10000
        assert state.tanks_by_id[2].count == 10000

    def test_entry_forward_transfer_over_limit_applies(self):
        # R2: entry -> grow-out at ANY weight (the >=1kg exit move).
        from forecast.events import TankAllocation, Transfer
        state = _mk_state()
        state.tanks_by_id[1].assign("B1", 30000, 1200.0, 14.0, "SW")
        ev = Transfer(batch_id="B1", event_date=TODAY, source_tank_id=1,
                      destinations=[TankAllocation(tank_id=3, count=30000,
                                                   avg_wt_g=1200.0, cv_pct=14.0)],
                      leaves_source_empty=True)
        warns = ev.apply(state)
        assert warns == []
        assert state.tanks_by_id[1].is_empty
        assert state.tanks_by_id[3].count == 30000

    def test_entry_harvest_refused_state_unchanged(self):
        from forecast.events import Harvest
        state = _mk_state()
        state.tanks_by_id[1].assign("B1", 30000, 4500.0, 14.0, "SW")
        before = _snapshot(state)
        ev = Harvest(batch_id="B1", event_date=TODAY, source_tank_id=1,
                     count=30000, avg_wt_g=4500.0)
        warns = ev.apply(state)
        assert any("R5" in w for w in warns)
        assert ev.count == 0          # "did not apply" marker for callers
        assert _snapshot(state) == before

    def test_entry_graded_harvest_refused_state_unchanged(self):
        from forecast.events import GradedHarvest
        state = _mk_state()
        state.tanks_by_id[1].assign("B1", 30000, 4000.0, 14.0, "SW")
        before = _snapshot(state)
        ev = GradedHarvest(batch_id="B1", event_date=TODAY, source_tank_id=1,
                           pickup_tank_id=61, pickup_count=10000,
                           pickup_avg_wt_g=5000.0, retention_tank_id=3,
                           retention_count=20000, retention_avg_wt_g=3500.0,
                           cv_pct=14.0)
        warns = ev.apply(state)
        assert any("R5" in w for w in warns)
        assert _snapshot(state) == before

    def test_growout_harvest_still_applies(self):
        from forecast.events import Harvest
        state = _mk_state()
        state.tanks_by_id[3].assign("B1", 30000, 4500.0, 14.0, "SW")
        ev = Harvest(batch_id="B1", event_date=TODAY, source_tank_id=3,
                     count=30000, avg_wt_g=4500.0)
        warns = ev.apply(state)
        assert warns == []
        assert ev.count == 30000
        assert state.tanks_by_id[3].is_empty


# ---------------------------------------------------------------------------
# manual_events validation (legacy structural path — no workbook needed)
# ---------------------------------------------------------------------------

class TestManualEventValidation:
    def test_og_transfer_backward_invalid(self):
        from forecast.manual_events import ManualDest, ManualEvent, \
            validate_manual_events
        state = _mk_state()
        state.tanks_by_id[3].assign("B1", 30000, 800.0, 14.0, "SW")  # OG3N
        ev = ManualEvent(type="og_transfer", week=1, from_tank=3,
                         destinations=[ManualDest(tank=1)])          # -> OG1N
        (i, ok, msgs), = validate_manual_events(state, [ev])
        assert not ok
        assert any("R4" in m for m in msgs)

    def test_og_transfer_entry_over_limit_invalid(self):
        from forecast.manual_events import ManualDest, ManualEvent, \
            validate_manual_events
        state = _mk_state()
        state.tanks_by_id[1].assign("B1", 30000, 1200.0, 14.0, "SW")  # OG1N >=1kg
        ev = ManualEvent(type="og_transfer", week=1, from_tank=1,
                         destinations=[ManualDest(tank=2)])           # -> OG1S
        (i, ok, msgs), = validate_manual_events(state, [ev])
        assert not ok
        assert any("R3" in m for m in msgs)

    def test_harvest_from_entry_invalid(self):
        from forecast.manual_events import ManualEvent, validate_manual_events
        state = _mk_state()
        state.tanks_by_id[1].assign("B1", 30000, 4500.0, 14.0, "SW")
        ev = ManualEvent(type="harvest", week=1, from_tank=1, count=1000)
        (i, ok, msgs), = validate_manual_events(state, [ev])
        assert not ok
        assert any("R5" in m for m in msgs)

    def test_og_to_6n_from_entry_invalid(self):
        from forecast.manual_events import ManualDest, ManualEvent, \
            validate_manual_events
        state = _mk_state()
        state.tanks_by_id[1].assign("B1", 30000, 4500.0, 14.0, "SW")
        ev = ManualEvent(type="og_to_6n", week=1, from_tank=1,
                         destinations=[ManualDest(tank=61)])
        (i, ok, msgs), = validate_manual_events(state, [ev])
        assert not ok
        assert any("R5" in m for m in msgs)

    def test_graded_harvest_from_entry_invalid(self):
        from forecast.manual_events import ManualDest, ManualEvent, \
            validate_manual_events
        state = _mk_state()
        state.tanks_by_id[1].assign("B1", 30000, 4000.0, 14.0, "SW")
        ev = ManualEvent(type="graded_harvest", week=1, from_tank=1, count=5000,
                         destinations=[ManualDest(tank=61), ManualDest(tank=3)])
        (i, ok, msgs), = validate_manual_events(state, [ev])
        assert not ok
        assert any("R5" in m for m in msgs)

    def test_fw_to_og_dest_outside_entry_invalid(self):
        from forecast.manual_events import ManualDest, ManualEvent, \
            validate_manual_events
        state = _mk_state()
        ev = ManualEvent(type="fw_to_og", week=1, batch="BX", count=10000,
                         destinations=[ManualDest(tank=3)])           # OG3N
        (i, ok, msgs), = validate_manual_events(state, [ev])
        assert not ok
        assert any("R1" in m for m in msgs)

    def test_fw_to_og_dest_in_entry_valid_structurally(self):
        from forecast.manual_events import ManualDest, ManualEvent, \
            validate_manual_events
        state = _mk_state()
        ev = ManualEvent(type="fw_to_og", week=1, batch="BX", count=10000,
                         destinations=[ManualDest(tank=1)])           # OG1N
        (i, ok, msgs), = validate_manual_events(state, [ev])
        assert ok, msgs   # legacy path: FW feasibility deferred to run time

    def test_legal_moves_still_valid(self):
        from forecast.manual_events import ManualDest, ManualEvent, \
            validate_manual_events
        state = _mk_state()
        state.tanks_by_id[1].assign("B1", 30000, 1200.0, 14.0, "SW")  # entry >=1kg
        state.tanks_by_id[4].assign("B2", 20000, 4500.0, 14.0, "SW")  # growout
        evs = [
            # entry -> growout at >=1kg (R2 forward exit)
            ManualEvent(type="og_transfer", week=1, from_tank=1,
                        destinations=[ManualDest(tank=3)]),
            # growout harvest (allowed)
            ManualEvent(type="harvest", week=1, from_tank=4, count=1000),
        ]
        results = validate_manual_events(state, evs)
        assert all(ok for (_i, ok, _m) in results), results
