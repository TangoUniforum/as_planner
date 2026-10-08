"""Per-week harvest WEIGHT floor: limits.yaml facility metric `min_harvest_weight_g`.

Operator request 2026-10-08: keep heavier fish most of the year but let
December harvest lighter ones to hold 300,000-310,000 lb HOG a week, without
lowering Control's Min harvest weight for the whole run (which broke the size
limits in 25 other weeks). A week with a row uses that weight; a week without
one uses Control, so a limits file with no such rows plans exactly as before.
"""
from __future__ import annotations

import ast
import collections
import contextlib
import io
import shutil
from pathlib import Path

import openpyxl
import yaml

from forecast.caps import (METRIC_MIN_HARVEST_WT, FacilityLimits, control_for_week,
                           harvest_weight_g)
from forecast.scenario_io import facility_limits_from_list, facility_limits_to_list
from tools.knob_corpus import _table

ROOT = Path(__file__).resolve().parent.parent
REF = ROOT / "tests" / "fixtures" / "reference"


class _Ctl:
    min_harvest_weight_g = 3500.0
    max_harvest_per_week = 55000.0


def test_week_without_a_row_uses_control():
    fl = FacilityLimits({("2026-W50", METRIC_MIN_HARVEST_WT): 3300.0})
    assert harvest_weight_g("2026-W49", fl, _Ctl()) == 3500.0
    assert harvest_weight_g("2026-W50", fl, _Ctl()) == 3300.0
    assert harvest_weight_g("2026-W50", None, _Ctl()) == 3500.0


def test_a_zero_row_means_unset_not_zero_grams():
    fl = FacilityLimits({("2026-W50", METRIC_MIN_HARVEST_WT): 0.0})
    assert harvest_weight_g("2026-W50", fl, _Ctl()) == 3500.0


def test_control_for_week_is_the_same_object_without_a_row():
    c = _Ctl()
    fl = FacilityLimits({("2026-W50", METRIC_MIN_HARVEST_WT): 3500.0})  # == Control
    assert control_for_week(c, fl, "2026-W49") is c
    assert control_for_week(c, fl, "2026-W50") is c


def test_control_for_week_copies_and_never_mutates_the_run_control():
    c = _Ctl()
    fl = FacilityLimits({("2026-W50", METRIC_MIN_HARVEST_WT): 3300.0})
    w = control_for_week(c, fl, "2026-W50")
    assert w is not c and w.min_harvest_weight_g == 3300.0
    assert c.min_harvest_weight_g == 3500.0
    assert w.max_harvest_per_week == c.max_harvest_per_week


def test_the_row_survives_the_save_round_trip():
    rows = [{"week": "2026-W50", "metric": METRIC_MIN_HARVEST_WT, "value": 3300.0}]
    assert facility_limits_to_list(facility_limits_from_list(rows)) == rows


def test_the_limits_editor_lists_the_row():
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_edit_limits")
    lists = [n.value for n in ast.walk(fn) if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "fl_metrics" for t in n.targets)]
    assert lists, "fl_metrics not found in _edit_limits"
    names = {e.id for e in lists[0].elts if isinstance(e, ast.Name)}
    assert "METRIC_MIN_HARVEST_WT" in names


def _run(tmp: Path, rows: list) -> dict:
    """Weekly harvest (fish) of the reference run with `rows` as facility rows."""
    cfg, scn = tmp / "config", tmp / "scenario"
    shutil.copytree(REF / "config", cfg)
    shutil.copytree(REF / "scenario", scn)
    lp = scn / "limits.yaml"
    d = yaml.safe_load(lp.read_text(encoding="utf-8"))
    d["facility"] = rows
    lp.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
    from forecast.run import main
    out = tmp / "out.xlsx"
    with contextlib.redirect_stdout(io.StringIO()):
        main(str(REF / "production_report.xlsx"), str(out), config_dir=str(cfg),
             scenario_dir=str(scn), calib_log_path="")
    if not out.exists():
        out = out.with_suffix(".xlsm")
    wb = openpyxl.load_workbook(out, read_only=True, data_only=True)
    cols, body = _table(wb, "HarvestPlan")
    wk = collections.Counter()
    for x in body:
        w = x[cols["Week"]]
        if w and str(w).startswith("20"):
            wk[str(w)] += float(x[cols["Count"]] or 0)
    return wk


def _control_weight() -> float:
    return float(yaml.safe_load((REF / "config" / "control.yaml").read_text(
        encoding="utf-8"))["min_harvest_weight_g"])


def test_rows_equal_to_control_change_nothing(tmp_path):
    base = _run(tmp_path / "a", [])
    same = _run(tmp_path / "b", [{"week": f"2026-W{n}", "metric": METRIC_MIN_HARVEST_WT,
                                  "value": _control_weight()} for n in range(26, 34)])
    assert same == base


def test_a_heavy_week_stops_the_planner_harvesting_light_fish(tmp_path):
    # The reference rotation drains ~55k fish a week through 2026-W25..W32.
    # 9 kg live in W28..W32 (staging + harvest weeks): no tank qualifies, so
    # those weeks' harvest must collapse; the weeks before are untouched.
    base = _run(tmp_path / "a", [])
    heavy = _run(tmp_path / "b", [{"week": f"2026-W{n}", "metric": METRIC_MIN_HARVEST_WT,
                                   "value": 9000.0} for n in range(28, 33)])
    assert base["2026-W30"] > 50000.0, base["2026-W30"]
    assert heavy["2026-W30"] < 0.5 * base["2026-W30"], (heavy["2026-W30"], base["2026-W30"])
    assert heavy["2026-W26"] == base["2026-W26"]
