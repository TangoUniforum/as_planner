"""A 6N fill is sized to the band of the week it is HARVESTED.

Fish moved into 6N in week t drain two weeks later. The controller used to
size that fill with week t's per-week floor/limit, so an operator's band bound
two weeks late: 2026's 51,500 rows sized the fills that drained in 2026-W53 /
2027-W01 (26,000/55,000 weeks), and a 36,500 March cap was overrun in March's
first weeks because those fills were sized at February's 42,500 (operator,
2026-10-05: "fix the fill week bug"). The fill's max also read only the
Control default, never the per-week row.

On the reference fixture (Control 30,000 / 55,000) the rotation fills drain
whole at 54,940 through 2026-W25..W32, so a one-week band is visible exactly
in the week it names.
"""
from __future__ import annotations

import collections
import contextlib
import io
import shutil
from pathlib import Path

import openpyxl
import yaml

from tools.knob_corpus import _table

REF = Path(__file__).resolve().parent / "fixtures" / "reference"


def _run(tmp: Path, rows: list) -> dict:
    """Weekly harvest (fish) of the reference run with `rows` as facility
    per-week overrides."""
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


def test_a_cap_binds_in_the_week_it_names(tmp_path):
    # Cap ONLY 2026-W30. Its fish were moved in at 2026-W28, which has no row.
    wk = _run(tmp_path, [
        {"week": "2026-W30", "metric": "max_harvest_per_week", "value": 40000.0}])
    assert wk["2026-W30"] <= 40000.0 + 1.0, wk["2026-W30"]
    # The fill week itself is untouched: its own harvest was staged earlier.
    assert wk["2026-W28"] > 50000.0


def test_a_band_does_not_leak_into_the_week_two_later(tmp_path):
    # Pin 2026-W28 (the FILL week of W30) at 45,000; W30 has no row and keeps
    # the Control band, so its harvest must not be pulled down to W28's number.
    wk = _run(tmp_path, [
        {"week": "2026-W28", "metric": "min_harvest_per_week", "value": 45000.0},
        {"week": "2026-W28", "metric": "max_harvest_per_week", "value": 45000.0}])
    assert wk["2026-W30"] > 50000.0, wk["2026-W30"]
