"""The weekly harvest floor in kg HOG (operator 2026-10-19: "a tonnes HOG
floor instead of a count floor").

The planner works in fish, so a HOG floor becomes, each week, the larger of
the fish floor and HOG kg / (harvest weight x HOG yield) -- capped at the
week's fish limit (caps.min_harvest_fish). Unset (the default) it must plan
exactly as before; set, it must lift the weeks that fell under it, and the run
must say which weeks still did.
"""
from __future__ import annotations

import contextlib
import io
import math
import shutil
from pathlib import Path

import openpyxl
import pytest
import yaml

from conftest import copy_config_without_costs
from forecast.caps import (METRIC_MAX_HARVEST, METRIC_MIN_HARVEST, METRIC_MIN_HARVEST_HOG,
                           METRIC_HOG_YIELD, METRIC_MIN_HARVEST_WT, FacilityLimits,
                           hog_floor_kg, min_harvest_fish, resolve_facility_cap)
from forecast.config_io import load_control

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PR = ROOT / "tests" / "fixtures" / "pr_2026-09-30.xlsx"
needs_pr = pytest.mark.skipif(not FIXTURE_PR.exists(), reason="fixture PR missing")
WK = "2027-W10"


def _ctrl(**kw):
    c = load_control(ROOT / "config")
    c.min_harvest_per_week = 26000.0
    c.max_harvest_per_week = 55000.0
    c.min_harvest_weight_g = 3500.0
    c.default_hog_yield = 0.81
    c.min_harvest_hog_kg_per_week = None
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _limits(rows=()):
    fl = FacilityLimits()
    for week, metric, value in rows:
        fl.overrides[(week, metric)] = float(value)
    return fl


# --------------------------------------------------------------------------- #
# The conversion
# --------------------------------------------------------------------------- #
def test_without_a_hog_floor_the_fish_floor_is_untouched():
    c, fl = _ctrl(), _limits([(WK, METRIC_MIN_HARVEST, 31000)])
    assert hog_floor_kg(WK, fl, c) is None
    for wt in (0.0, 3000.0, 4500.0):
        assert min_harvest_fish(WK, fl, c, wt) == resolve_facility_cap(
            METRIC_MIN_HARVEST, WK, fl, c) == 31000.0
    c0 = _ctrl(min_harvest_per_week=0.0)
    assert min_harvest_fish(WK, _limits(), c0, 4000.0) is None     # None stays None


def test_a_hog_floor_becomes_fish_at_the_harvest_weight():
    c = _ctrl(min_harvest_hog_kg_per_week=110000.0)
    # 110,000 kg HOG / (3.8 kg x 0.81) = 35,737.3 -> 35,738 fish, above 26,000.
    assert min_harvest_fish(WK, _limits(), c, 3800.0) == math.ceil(110000 / (3.8 * 0.81))
    # Heavier fish need fewer; lighter fish need more.
    assert min_harvest_fish(WK, _limits(), c, 4200.0) < min_harvest_fish(WK, _limits(), c, 3800.0)
    # The fish floor still wins when it is the larger.
    c2 = _ctrl(min_harvest_hog_kg_per_week=50000.0)
    assert min_harvest_fish(WK, _limits(), c2, 3800.0) == 26000.0


def test_the_week_row_wins_and_uses_the_weeks_yield_and_weight():
    c = _ctrl(min_harvest_hog_kg_per_week=110000.0)
    fl = _limits([(WK, METRIC_MIN_HARVEST_HOG, 90000), (WK, METRIC_HOG_YIELD, 0.80),
                  (WK, METRIC_MIN_HARVEST_WT, 3000)])
    assert hog_floor_kg(WK, fl, c) == 90000.0
    assert hog_floor_kg("2027-W11", fl, c) == 110000.0          # other weeks: Control
    assert min_harvest_fish(WK, fl, c, 3600.0) == math.ceil(90000 / (3.6 * 0.80))
    # Unknown weight: the week's minimum harvest weight (3,000 g here), the
    # lightest fish that may go, so the most fish.
    assert min_harvest_fish(WK, fl, c, 0.0) == math.ceil(90000 / (3.0 * 0.80))


def test_a_hog_floor_never_asks_for_more_fish_than_the_weeks_limit():
    c = _ctrl(min_harvest_hog_kg_per_week=200000.0)
    fl = _limits([(WK, METRIC_MAX_HARVEST, 42000)])
    # 200 t at 3.5 kg would be 70,547 fish; the plant takes 42,000 that week.
    assert min_harvest_fish(WK, fl, c, 3500.0) == 42000.0
    # Negative control: without the limit row the Control 55,000 binds instead.
    assert min_harvest_fish(WK, _limits(), c, 3500.0) == 55000.0


# --------------------------------------------------------------------------- #
# A real run
# --------------------------------------------------------------------------- #
def _run(tmp, hog_kg=None):
    cfg, scn = tmp / "config", tmp / "scenario"
    copy_config_without_costs(ROOT / "config", cfg)
    shutil.copytree(ROOT / "scenario", scn)
    if hog_kg is not None:
        p = cfg / "control.yaml"
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        d["min_harvest_hog_kg_per_week"] = float(hog_kg)
        p.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
    inp, out = tmp / "pr.xlsx", tmp / "out.xlsm"
    shutil.copy(FIXTURE_PR, inp)
    from forecast.run import main
    with contextlib.redirect_stdout(io.StringIO()):
        rc = main(str(inp), str(out), config_dir=str(cfg), scenario_dir=str(scn),
                  calib_log_path="", growth_history_path="")
    assert rc == 0
    wb = openpyxl.load_workbook(out, read_only=True, data_only=True)
    hog = {}
    for r in wb["HarvestReport"].iter_rows(min_row=5, values_only=True):
        if r and isinstance(r[0], int) and hasattr(r[3], "strftime"):
            hog[str(r[2])] = hog.get(str(r[2]), 0.0) + float(r[8] or 0)
    notes = [str(c) for row in wb["ValidationLog"].iter_rows(values_only=True)
             for c in row if isinstance(c, str) and "HOG FLOOR" in c]
    wb.close()
    return hog, notes


@needs_pr
def test_a_hog_floor_lifts_the_thin_weeks_and_the_run_names_the_rest(tmp_path):
    base, base_notes = _run(tmp_path / "base")
    assert not base_notes                                  # no floor set, nothing said
    weeks = sorted(w for w in base if w >= "2027-W01")[:40]
    vals = sorted(base[w] for w in weeks)
    floor = vals[len(vals) // 4]                           # a quarter of weeks sit under it
    under_before = [w for w in weeks if base[w] < floor - 1.0]
    assert len(under_before) >= 5                          # the negative control: it CAN fail
    hog, notes = _run(tmp_path / "floor", hog_kg=floor)
    under_after = [w for w in weeks if hog.get(w, 0.0) < floor - 1.0]
    assert len(under_after) < len(under_before), (under_before, under_after)
    assert any(" week(s) with a HOG floor met it" in n for n in notes), notes
    # Every week the run calls short really is short, and vice versa.
    named = {n.split(" - ")[1].split(":")[0] for n in notes if "shipped" in n}
    assert {w for w in weeks if w in named} == set(under_after)
