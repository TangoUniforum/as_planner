"""MOVE-SIZE RULES (Control `move_size_rules`, opt-in).

Operator 2026-10-08: "there are a number of transfers that dont meet the 7000
fish min transfer constraint ... isnt that wrong?" -- it was: the plan-carrying
step, the density leveller and the 6N fill moved partial groups below
min_transfer_count, and make-room could stack a pair past its week. Off = the
historical plan; on = a partial move is >= the minimum or does not happen (a
move that empties its source is always allowed), no move starts a new group
below the minimum in an empty tank, density relief from an over-cap tank still
wins, and each 6N fill is solved as whole tanks + at most one legal piece.
"""
from __future__ import annotations

import collections
import contextlib
import io
import shutil
from pathlib import Path

import openpyxl

from forecast.placement import _plan_sixn_draws, _small_move_blocked
from tools.knob_corpus import _table

REF = Path(__file__).resolve().parent / "fixtures" / "reference"


class _T:
    def __init__(self, count, empty=False):
        self.count, self.is_empty = count, empty


def test_small_partial_is_blocked_but_whole_tank_is_not():
    assert _small_move_blocked(3000, _T(20000), _T(30000), 7000)
    assert not _small_move_blocked(3000, _T(3000), _T(30000), 7000)   # empties source
    assert not _small_move_blocked(8000, _T(20000), _T(30000), 7000)
    assert not _small_move_blocked(3000, _T(20000), _T(30000), 0)      # rules off


def test_relief_wins_but_never_opens_a_dribble_tank():
    assert not _small_move_blocked(2000, _T(50000), _T(30000), 7000, relief=True)
    assert _small_move_blocked(2000, _T(50000), _T(0, empty=True), 7000, relief=True)


def test_fill_plan_whole_tanks_plus_one_legal_piece():
    plan = _plan_sixn_draws([(1, 30000, 0), (2, 25000, 0), (3, 40000, 0)], 52000, 7000, 7140)
    assert sum(plan.values()) == 52000
    pieces = [v for tid, v in plan.items() if v < dict([(1, 30000), (2, 25000), (3, 40000)])[tid]]
    assert len(pieces) <= 1 and all(v >= 7000 for v in pieces)


def test_fill_plan_uses_the_band_instead_of_a_small_piece():
    # exact 25,005 needs a 3,472 or 6,762 piece; the band allows 25,243 legally
    plan = _plan_sixn_draws([(31, 21533, 0), (36, 18243, 0)], 25005, 7000, 7140, lo=24505, hi=25505)
    assert plan is not None and 24505 <= sum(plan.values()) <= 25505
    assert all(v >= 7000 for v in plan.values())


def test_fill_plan_none_when_nothing_legal_fits():
    assert _plan_sixn_draws([(1, 40000, 0), (2, 10000, 0)], 45000, 7000, 7140) is None


def _run(tmp: Path, on: bool) -> dict:
    cfg, scn = tmp / "config", tmp / "scenario"
    shutil.copytree(REF / "config", cfg); shutil.copytree(REF / "scenario", scn)
    if on:
        with open(cfg / "control.yaml", "a", encoding="utf-8") as f:
            f.write("\nmove_size_rules: true\nmin_transfer_count: 7000\n")
    from forecast.run import main
    out = tmp / "out.xlsx"
    with contextlib.redirect_stdout(io.StringIO()):
        main(str(REF / "production_report.xlsx"), str(out), config_dir=str(cfg),
             scenario_dir=str(scn), calib_log_path="")
    if not out.exists():
        out = out.with_suffix(".xlsm")
    wb = openpyxl.load_workbook(out, read_only=True, data_only=True)
    c, tp = _table(wb, "TransferPlan"); cb, bl = _table(wb, "BatchLocations")
    occ = collections.defaultdict(float)
    for x in bl:
        occ[(str(x[cb["Week"]]), int(float(x[cb["Tank"]])), x[cb["Batch"]])] += float(x[cb["Count"]] or 0)
    grp = collections.defaultdict(float)
    for x in tp:
        if x[c["Week"]] and x[c["Type"]] != "TranOG":
            grp[(str(x[c["Week"]]), x[c["Batch"]], int(float(x[c["From_Tank"]])))] += float(x[c["Count"]] or 0)
    return sum(1 for (w, b, t), n in grp.items() if n < 7000 and occ.get((w, t, b), 0) > 0)


def test_rules_cut_small_partial_moves_on_the_reference_plan(tmp_path):
    off = _run(tmp_path / "off", on=False)
    on = _run(tmp_path / "on", on=True)
    assert off > 0, "fixture must show the defect"
    assert on < 0.5 * off, (off, on)
