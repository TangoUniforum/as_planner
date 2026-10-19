"""An egg batch stocked DURING a manual window is in the ledger from its stocking week.

Operator 2026-10-09: "does the Oct close and November open number tie?" -- it
did not. The window moves the forecast start to the first post-window week
before the incoming batches are projected, so B57 (570,000 eggs stocked
2026-10-08, inside the operator's W40-W46 window) first appeared at W47 already at 534,804
with no input ever booked: the facility fish total jumped 534,804 between the
W46 close and the W47 open (October close vs November open). The window weeks
are now projected on the original grid for the REPORTS only.
"""
from __future__ import annotations

import contextlib
import io
import shutil
from pathlib import Path

import openpyxl
import pytest

from conftest import copy_config_without_costs

ROOT = Path(__file__).resolve().parent.parent
PR = ROOT / "tests" / "fixtures" / "pr_2026-09-30.xlsx"

pytestmark = pytest.mark.skipif(
    not (PR.exists() and (ROOT / "config" / "control.yaml").exists()
         and (ROOT / "scenario" / "batches.yaml").exists()),
    reason="needs the 9/30 PR fixture + committed config/scenario")


@pytest.fixture(scope="module")
def wb(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("egg")
    copy_config_without_costs(ROOT / "config", tmp / "config")   # never the live costs
    shutil.copytree(ROOT / "scenario", tmp / "scenario")
    shutil.rmtree(tmp / "scenario" / "manual_events", ignore_errors=True)
    (tmp / "scenario" / "manual_events.yaml").unlink(missing_ok=True)
    from forecast.run import main
    out = tmp / "out.xlsm"
    with contextlib.redirect_stdout(io.StringIO()):
        main(str(PR), str(out), config_dir=str(tmp / "config"),
             scenario_dir=str(tmp / "scenario"), advance_weeks=2, calib_log_path="")
    return openpyxl.load_workbook(out, read_only=True, data_only=True)


def _weekly(wb):
    rows = list(wb["WeeklyReport"].iter_rows(values_only=True))
    h = next(i for i, r in enumerate(rows) if r and r[0] == "Scenario")
    c = {k: i for i, k in enumerate(rows[h])}
    return [r for r in rows[h + 1:] if r and r[c["Batch"]]], c


def test_the_egg_batch_is_booked_in_its_stocking_week(wb):
    rows, c = _weekly(wb)
    b57 = [r for r in rows if r[c["Batch"]] == "B57"]
    first = b57[0]
    assert first[c["Week"]] == "2026-W41"
    assert first[c["Open_Count (fish)"]] == 0
    assert abs(first[c["Input_Count (fish)"]] - 570000) <= 1


def test_the_facility_total_chains_week_to_week(wb):
    rows, c = _weekly(wb)
    tot = [r for r in rows if r[c["Batch"]] == "TOTAL"]
    for a, b in zip(tot, tot[1:]):
        assert abs(b[c["Open_Count (fish)"]] - a[c["Close_Count (fish)"]]) <= 1, (a[c["Week"]], b[c["Week"]])


def test_october_close_is_november_open(wb):
    rows = list(wb["MonthlyReport"].iter_rows(values_only=True))
    c = {k: i for i, k in enumerate(rows[4])}
    tot = {r[1]: r for r in rows[5:] if r and r[2] == "TOTAL"}
    assert abs(tot["2026-11"][c["Open_Count (fish)"]] - tot["2026-10"][c["Close_Count (fish)"]]) <= 1
    assert tot["2026-10"][c["Input_Count (fish)"]] >= 570000 - 1
