"""PR hydration survived being moved out of the Global tooling.

`_hydrate_pr` used to live in tools/run_full_facility_poc.py, surrounded by
Global-method harnesses. It is shared infrastructure — the L1 envelope and the
capacity/transition study both need it to learn where the facility is today —
so it moved to forecast/pr_state.py when the Global method was dropped.

These tests guard the move itself: the old import paths still resolve to the
SAME object, and the aggregation logic is unchanged. They use synthetic PR
records rather than the operator's workbook so they run anywhere.
"""
import os
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forecast import pr_state                                     # noqa: E402
from forecast.sixn import SIXN_ALL_TANKS                          # noqa: E402


def _og(batch, tank, count, bio):
    return SimpleNamespace(batch_id=batch, tank_id=tank,
                           closing_count=count, closing_biomass_kg=bio)


def _fw(batch, count, bio):
    return SimpleNamespace(batch_id=batch, closing_count=count,
                           closing_biomass_kg=bio)


class TestTheOldImportPathsStillWork:
    """Every historical spelling must resolve to the one function."""

    def test_the_tool_reexports_the_same_object(self):
        from tools.run_full_facility_poc import _hydrate_pr
        assert _hydrate_pr is pr_state.hydrate_pr

    def test_l1_probe_uses_the_same_object(self):
        from tools.l1_probe import _hydrate_pr
        assert _hydrate_pr is pr_state.hydrate_pr

    def test_the_private_alias_survives_in_the_new_home(self):
        assert pr_state._hydrate_pr is pr_state.hydrate_pr


class TestHydrationLogicIsUnchanged:
    """The OG/6N/FW split is the part that must not drift."""

    @pytest.fixture
    def hydrate(self, monkeypatch, tmp_path):
        """Drive hydrate_pr off synthetic records, not a real workbook."""
        def run(og_records, fw_records, closing=datetime(2026, 8, 31)):
            wb = SimpleNamespace(close=lambda: None)
            monkeypatch.setattr("forecast.excel_io.load_workbook",
                                lambda *a, **k: wb, raising=False)
            monkeypatch.setattr("forecast.production_report.read_production_report",
                                lambda _wb: (closing, og_records, fw_records),
                                raising=False)
            path = tmp_path / "PR.xlsm"
            path.write_bytes(b"")          # must exist; contents never read
            batches = [SimpleNamespace(batch_id="B1", tran_og_cv=14.0)]
            return pr_state.hydrate_pr(path, batches)
        return run

    def test_grow_out_fish_seed_og_with_the_batch_cv(self, hydrate):
        og, fw, start, purge = hydrate([_og("B1", 1, 1000.0, 2000.0)], [])
        assert og == {"B1": (1000.0, 2000.0, 14.0)}
        assert purge == {}

    def test_a_batch_with_no_cv_on_file_falls_back_to_sixteen(self, hydrate):
        og, _, _, _ = hydrate([_og("B9", 1, 100.0, 200.0)], [])
        assert og["B9"][2] == 16.0

    def test_six_n_resident_fish_go_to_purge_not_grow_out(self, hydrate):
        tank = sorted(SIXN_ALL_TANKS)[0]
        og, _, _, purge = hydrate([_og("B1", tank, 500.0, 1500.0)], [])
        assert og == {}, "6N fish must not seed grow-out"
        assert purge == {"B1": (500.0, 3000.0)}

    def test_tanks_of_the_same_batch_aggregate(self, hydrate):
        og, _, _, _ = hydrate(
            [_og("B1", 1, 100.0, 200.0), _og("B1", 2, 300.0, 400.0)], [])
        assert og["B1"][0] == 400.0
        assert og["B1"][1] == pytest.approx(600.0 * 1000.0 / 400.0)

    def test_fw_fish_are_reported_when_the_batch_is_not_already_in_og(self, hydrate):
        _, fw, _, _ = hydrate([], [_fw("B1", 200.0, 100.0)])
        assert fw["B1"][0] == 200.0
        assert fw["B1"][2] == datetime(2026, 8, 31)

    def test_fw_is_suppressed_once_the_batch_has_og_fish(self, hydrate):
        _, fw, _, _ = hydrate([_og("B1", 1, 10.0, 20.0)], [_fw("B1", 200.0, 100.0)])
        assert fw == {}, "a batch already in OG must not double-count its FW row"

    def test_zero_count_rows_are_dropped_everywhere(self, hydrate):
        og, fw, _, purge = hydrate([_og("B1", 1, 0.0, 0.0)], [_fw("B2", 0.0, 0.0)])
        assert (og, fw, purge) == ({}, {}, {})

    def test_the_start_date_is_the_day_after_pr_closing(self, hydrate):
        _, _, start, _ = hydrate([], [], closing=datetime(2026, 8, 31))
        assert start == datetime(2026, 9, 1)


class TestKnownWartsArePreserved:
    """Relocation was a refactor. These are pre-existing and deliberately kept."""

    def test_a_missing_workbook_still_returns_the_short_tuple(self, tmp_path):
        # Documented defect: callers unpack FOUR values, so this raises for them.
        # Asserted so that fixing it is a deliberate, caller-aware change.
        out = pr_state.hydrate_pr(tmp_path / "absent.xlsm", [])
        assert out == ({}, {}, None)
        assert len(out) == 3, "still the 3-vs-4 tuple asymmetry"
