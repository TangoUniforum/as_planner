"""6N SOURCE LIMIT (Control `sixn_max_source_tanks`, opt-in).

Operator 2026-10-15: "never move more then two tanks into 6N to make a harvest
tank becasue there are logistical constraints" -- counted per harvest week,
i.e. per weekly fill of a 6N PAIR (main + sister together). 0 = no limit (the
historical plan); N = no pair draws from more than N OG tanks in one week.
"""
from __future__ import annotations

import collections
import contextlib
import io
import shutil
from datetime import date, timedelta
from pathlib import Path

import openpyxl

from forecast.placement import (
    _freeze_6n_dest,
    _make_room_into_6n,
    _plan_sixn_draws,
    _sixn_source_ok,
)
from forecast.state import STAGE_STARVE, FacilityState, TankState
from tools.knob_corpus import _table

REF = Path(__file__).resolve().parent / "fixtures" / "reference"
TODAY = date(2026, 11, 2)            # a Monday
PAIR_OF = {61: 61, 67: 61, 63: 63, 69: 63, 65: 65, 71: 65}


def _mk_state(cap=0):
    s = FacilityState(TODAY, [
        TankState("OG3N-31", 31, "OG3N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG3N-32", 32, "OG3N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG3N-33", 33, "OG3N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG4N-41", 41, "OG4N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG6N-61", 61, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-67", 67, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-63", 63, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-69", 69, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-65", 65, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-71", 71, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
    ])
    s.sixn_max_source_tanks = cap
    return s


# --- the fill solver ---------------------------------------------------------

CANDS = [(31, 12000, 0), (32, 12000, 0), (33, 12000, 0), (41, 12000, 0)]


def test_unlimited_fill_uses_more_than_two_tanks():
    """Negative control: the shape the limit exists for (4 small tanks)."""
    plan = _plan_sixn_draws(CANDS, 45000, 7000, 7140, lo=40000, hi=48000)
    assert plan is not None and len(plan) > 2


def test_limited_fill_uses_at_most_two_tanks_and_takes_the_biggest_legal_draw():
    plan = _plan_sixn_draws(CANDS, 45000, 7000, 7140, lo=40000, hi=48000, max_tanks=2)
    assert plan is not None and len(plan) <= 2
    # nothing with 2 tanks reaches the floor -> the biggest legal draw (two
    # whole tanks), never the unlimited draw
    assert sum(plan.values()) == 24000


def test_limited_fill_whole_pool_shortcut_respects_the_limit():
    plan = _plan_sixn_draws(CANDS, 60000, 7000, 7140, lo=40000, hi=60000, max_tanks=2)
    assert len(plan) <= 2
    assert len(_plan_sixn_draws(CANDS, 60000, 7000, 7140, lo=40000, hi=60000)) == 4


def test_no_room_left_means_no_plan():
    assert _plan_sixn_draws(CANDS, 45000, 7000, 7140, max_tanks=0) is None


# --- the ledger ----------------------------------------------------------------

def _fill(s, dest, src, when=TODAY):
    s.tanks_by_id[dest].assign("B45", 10000, 3500.0, 16.0, STAGE_STARVE)
    _freeze_6n_dest(s, dest, fill_date=when, source_tank_id=src)


def test_third_tank_refused_for_the_whole_pair_this_week_only():
    s = _mk_state(cap=2)
    assert _sixn_source_ok(s, 61, 41, TODAY)          # before any fill: allowed
    _fill(s, 61, 41)
    _fill(s, 67, 33)                                   # sister counts for the pair
    assert not _sixn_source_ok(s, 61, 31, TODAY)
    assert not _sixn_source_ok(s, 67, 31, TODAY)
    assert _sixn_source_ok(s, 61, 41, TODAY + timedelta(days=3))   # same tank, same week
    assert _sixn_source_ok(s, 63, 31, TODAY)                       # another pair
    assert _sixn_source_ok(s, 61, 31, TODAY + timedelta(days=7))   # next week


def test_no_limit_allows_everything():
    s = _mk_state(cap=0)
    _fill(s, 61, 41)
    _fill(s, 67, 33)
    assert _sixn_source_ok(s, 61, 31, TODAY)


def test_make_room_goes_to_another_pair_when_the_resting_pair_is_full_up():
    for cap, expect_resting in ((2, False), (0, True)):
        s = _mk_state(cap=cap)
        # resting pair (63, 69): 63 already filled from 41 and 33 this week,
        # 69 still empty -- the slot make-room would normally use.
        s.tanks_by_id[63].assign("B45", 30000, 3500.0, 16.0, STAGE_STARVE)
        _freeze_6n_dest(s, 63, fill_date=TODAY, source_tank_id=41)
        _freeze_6n_dest(s, 63, fill_date=TODAY, source_tank_id=33)
        src = s.tanks_by_id[31]
        src.assign("B46", 20000, 3300.0, 16.0, "SW")
        ok = _make_room_into_6n(s, src, TODAY, (63, 69), [], [], "2026-W45",
                                reason="test", is_purge=True)
        assert ok
        assert (not s.tanks_by_id[69].is_empty) is expect_resting, cap


# --- the whole plan ------------------------------------------------------------

def _max_pair_sources(tmp: Path, cap: int) -> int:
    cfg, scn = tmp / "config", tmp / "scenario"
    shutil.copytree(REF / "config", cfg); shutil.copytree(REF / "scenario", scn)
    with open(cfg / "control.yaml", "a", encoding="utf-8") as f:
        f.write(f"\nmove_size_rules: true\nmin_transfer_count: 7000\n"
                f"sixn_max_source_tanks: {cap}\n")
    from forecast.run import main
    out = tmp / "out.xlsx"
    with contextlib.redirect_stdout(io.StringIO()):
        main(str(REF / "production_report.xlsx"), str(out), config_dir=str(cfg),
             scenario_dir=str(scn), calib_log_path="")
    if not out.exists():
        out = out.with_suffix(".xlsm")
    wb = openpyxl.load_workbook(out, read_only=True, data_only=True)
    c, tp = _table(wb, "TransferPlan")
    src = collections.defaultdict(set)
    for x in tp:
        try:
            to, fr = int(float(x[c["To_Tank"]])), int(float(x[c["From_Tank"]]))
        except (TypeError, ValueError):
            continue
        if to in PAIR_OF and fr not in PAIR_OF and x[c["Type"]] in ("Transfer", "Grade"):
            src[(str(x[c["Week"]]), PAIR_OF[to])].add(fr)
    return max((len(v) for v in src.values()), default=0)


def test_limit_holds_on_the_reference_plan(tmp_path):
    off = _max_pair_sources(tmp_path / "off", 0)
    on = _max_pair_sources(tmp_path / "on", 2)
    assert off > 2, "fixture must show a pair filled from more than two tanks"
    assert on <= 2, (off, on)
