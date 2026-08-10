"""Weekly handling budget (operator rule 4): max 15 transfer moves/week.

Deferrable quality passes stop at the cap (their work waits for a calmer
week); essential moves are never blocked — an essential-only overrun is
reported by the handling gate, not silently truncated.
"""
from __future__ import annotations

from datetime import date

import openpyxl

from forecast.analysis import _gate_handling_budget
from forecast.events import TankAllocation, Transfer
from forecast.excel_io import write_transfer_plan_output
from forecast.models import ControlParams
from forecast.optimize import _weekly_move_counts
from forecast.placement import (
    _consolidate_remnants,
    _emit_transfers_for_batch_diff,
    _even_out_density,
)
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


class TestDiffBudget:
    """Plan-diff: SOURCE drains are essential (never blocked); kept-tank
    EVENING top-ups are quality moves that yield to the handling budget."""

    def test_source_drain_never_blocked(self):
        s = _mk_state()
        s.tanks_by_id[31].assign("B50", 20000, 3800.0, 16.0, "SW")
        s.tanks_by_id[32].assign("B50", 20000, 3800.0, 16.0, "SW")
        evs, warns = [], []
        # 31 leaves the plan (source), 41 joins (new dest). Budget exhausted.
        _emit_transfers_for_batch_diff(
            s, "B50", {31, 32}, {32, 41}, TODAY, evs, warns,
            moves_left=lambda: 0)
        applied = [e for e in evs if e.count_transferred > 0]
        assert applied, "essential source drain was blocked by the budget"
        assert all(e.source_tank_id == 31 for e in applied)
        assert s.tanks_by_id[31].is_empty

    def test_kept_evening_yields_to_budget(self):
        s = _mk_state()
        for tid in (31, 32, 33):
            s.tanks_by_id[tid].assign("B50", 21000, 3800.0, 16.0, "SW")
        evs, warns = [], []
        # No sources; batch gains tank 41 -> the 3 kept tanks would each top
        # it up (quality evening). Budget spent -> nothing moves.
        _emit_transfers_for_batch_diff(
            s, "B50", {31, 32, 33}, {31, 32, 33, 41}, TODAY, evs, warns,
            moves_left=lambda: 0)
        assert [e for e in evs if e.count_transferred > 0] == []
        assert s.tanks_by_id[41].is_empty

    def test_kept_evening_partial_budget(self):
        s = _mk_state()
        for tid in (31, 32, 33):
            s.tanks_by_id[tid].assign("B50", 21000, 3800.0, 16.0, "SW")
        evs, warns = [], []
        _emit_transfers_for_batch_diff(
            s, "B50", {31, 32, 33}, {31, 32, 33, 41}, TODAY, evs, warns,
            moves_left=lambda: max(0, 1 - len(evs)))
        assert len([e for e in evs if e.count_transferred > 0]) == 1

    def test_no_budget_arg_is_unlimited(self):
        s = _mk_state()
        for tid in (31, 32, 33):
            s.tanks_by_id[tid].assign("B50", 21000, 3800.0, 16.0, "SW")
        evs, warns = [], []
        _emit_transfers_for_batch_diff(
            s, "B50", {31, 32, 33}, {31, 32, 33, 41}, TODAY, evs, warns)
        assert len([e for e in evs if e.count_transferred > 0]) == 3
        assert not s.tanks_by_id[41].is_empty


class TestTransferPlanRowHonesty:
    """A TransferPlan 'Transfer' row = one real src->dst tank move: 0-fish
    float-residue legs are dropped and same-week duplicate (batch, src, dst)
    legs merge into one row — the handling gate counts these rows."""

    def _events(self):
        e1 = Transfer(
            batch_id="B50", event_date=TODAY, source_tank_id=31,
            destinations=[
                TankAllocation(tank_id=41, count=0.3, avg_wt_g=3800.0,
                               cv_pct=16.0),
                TankAllocation(tank_id=42, count=5000, avg_wt_g=3800.0,
                               cv_pct=16.0),
            ])
        e1.count_transferred = 5000.3
        e2 = Transfer(
            batch_id="B50", event_date=TODAY, source_tank_id=32,
            destinations=[TankAllocation(tank_id=41, count=1000,
                                         avg_wt_g=3600.0, cv_pct=16.0)])
        e2.count_transferred = 1000
        e3 = Transfer(
            batch_id="B50", event_date=TODAY, source_tank_id=32,
            destinations=[TankAllocation(tank_id=41, count=2000,
                                         avg_wt_g=3900.0, cv_pct=16.0)])
        e3.count_transferred = 2000
        return [e1, e2, e3]

    def _rows(self, wb):
        ws = wb["TransferPlan"]
        rows = list(ws.iter_rows(values_only=True))
        hi = next(i for i, r in enumerate(rows) if r and r[0] == "Week")
        return [dict(zip(rows[hi], r)) for r in rows[hi + 1:]
                if r and r[0]]

    def test_zero_leg_dropped_and_duplicates_merged(self):
        wb = openpyxl.Workbook()
        write_transfer_plan_output(wb, self._events(), [], [])
        rows = self._rows(wb)
        assert len(rows) == 2
        moves = {(r["From_Tank"], r["To_Tank"]): r for r in rows}
        assert ("31", 42) in moves            # the real leg survives
        assert ("31", 41) not in moves        # 0.3-fish leg is not a move
        merged = moves[("32", 41)]
        assert merged["Count (fish)"] == 3000
        # fish-weighted avg weight: (1000*3.6 + 2000*3.9) / 3000 = 3.8
        assert abs(merged["Avg_Weight (kg)"] - 3.8) < 1e-6

    def test_gate_counter_matches_rows(self):
        wb = openpyxl.Workbook()
        write_transfer_plan_output(wb, self._events(), [], [])
        assert _weekly_move_counts(wb) == [2]

    def test_gate_counter_skips_zero_rows_in_legacy_workbooks(self):
        # A workbook written before the writer fix may carry 0-fish rows.
        wb = openpyxl.Workbook()
        ws = wb.create_sheet("TransferPlan")
        ws.append(["Week", "Batch", "Type", "From_Tank", "To_Tank",
                   "Count (fish)", "Avg_Weight (kg)", "Grade", "CV (%)"])
        ws.append(["2026-W40", "B50", "Transfer", "31", 41, 0, 3.8, None, 16])
        ws.append(["2026-W40", "B50", "Transfer", "31", 42, 5000, 3.8, None, 16])
        assert _weekly_move_counts(wb) == [1]
