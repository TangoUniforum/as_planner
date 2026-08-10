"""Weekly handling budget (operator rule 4): max 15 transfer moves/week.

Deferrable quality passes stop at the cap (their work waits for a calmer
week); essential moves are never blocked — an essential-only overrun is
reported by the handling gate, not silently truncated.
"""
from __future__ import annotations

from datetime import date

from forecast.analysis import _gate_handling_budget
from forecast.models import ControlParams
from forecast.placement import _consolidate_remnants, _even_out_density
from forecast.state import FacilityState, TankState

TODAY = date(2026, 8, 3)


def _mk_state():
    return FacilityState(TODAY, [
        TankState("OG3N-31", 31, "OG3N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG3N-32", 32, "OG3N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG3N-33", 33, "OG3N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG4N-41", 41, "OG4N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG4N-42", 42, "OG4N", 1000.0, 95.0, 1000.0, "OG"),
    ])


class TestGate:
    def test_truth_table(self):
        assert _gate_handling_budget({"weeks_moves_over_cap": 0,
                                      "weeks_moves_warn": 0})[0] == "PASS"
        s, d = _gate_handling_budget({"weeks_moves_over_cap": 0,
                                      "weeks_moves_warn": 3,
                                      "moves_week_max": 14})
        assert s == "WARN" and "14" in d
        s, d = _gate_handling_budget({"weeks_moves_over_cap": 2,
                                      "weeks_moves_warn": 5,
                                      "moves_week_max": 19})
        assert s == "FAIL" and "19" in d
        assert _gate_handling_budget({})[0] == "N/A"

    def test_knob_default(self):
        assert ControlParams.__dataclass_fields__[
            "max_transfers_per_week"].default == 15


class TestEvenOutBudget:
    def _crowded(self):
        s = _mk_state()
        # over-cap source (25,001 fish @ 3.8kg in 1000 m3 = 95.004) + two
        # near-empty same-batch destinations -> would emit 2 equalize moves
        s.tanks_by_id[31].assign("B50", 25100, 3800.0, 16.0, "SW")
        s.tanks_by_id[32].assign("B50", 8000, 3800.0, 16.0, "SW")
        s.tanks_by_id[33].assign("B50", 8000, 3800.0, 16.0, "SW")
        return s

    def test_unlimited_emits_all(self):
        s = self._crowded()
        evs, warns = [], []
        _even_out_density(s, "B50", TODAY, evs, warns)
        assert len([e for e in evs if e.count_transferred > 0]) == 2

    def test_budget_one_stops_after_one(self):
        s = self._crowded()
        evs, warns = [], []
        _even_out_density(s, "B50", TODAY, evs, warns, max_moves=1)
        assert len([e for e in evs if e.count_transferred > 0]) == 1

    def test_budget_zero_emits_none(self):
        s = self._crowded()
        evs, warns = [], []
        _even_out_density(s, "B50", TODAY, evs, warns, max_moves=0)
        assert evs == []


class TestRemnantSweepBudget:
    def _with_remnants(self):
        s = _mk_state()
        # two independent remnants, each with an absorbing same-batch tank
        s.tanks_by_id[31].assign("B50", 500, 3800.0, 16.0, "SW")
        s.tanks_by_id[32].assign("B50", 20000, 3800.0, 16.0, "SW")
        s.tanks_by_id[41].assign("B51", 600, 3800.0, 16.0, "SW")
        s.tanks_by_id[42].assign("B51", 20000, 3800.0, 16.0, "SW")
        return s

    def test_unlimited_folds_both(self):
        s = self._with_remnants()
        evs, warns = [], []
        folds = _consolidate_remnants(s, TODAY, "2026-W40", evs, warns, 7000.0)
        assert folds == 2

    def test_budget_one_folds_one_and_defers(self):
        s = self._with_remnants()
        evs, warns = [], []
        folds = _consolidate_remnants(s, TODAY, "2026-W40", evs, warns, 7000.0,
                                      max_moves=1)
        assert folds == 1
        # the deferred remnant is intact, not half-moved
        left = [t for t in (s.tanks_by_id[31], s.tanks_by_id[41])
                if not t.is_empty]
        assert len(left) == 1 and left[0].count in (500, 600)
