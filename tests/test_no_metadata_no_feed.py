"""A batch that is not in the scenario is not fed.

A ProductionReport batch with no Batches-sheet metadata (not in Configure ->
Batches) is loaded into its tanks, but the biology does not advance it: no
growth, no mortality. It was still FED -- realized_feed_kg_day used the curve
SGR x correction 1.0 x a made-up FCR of 1.2 -- so it ate with nothing to show
for it: 3.89 kt of feed on the 2025-07-31 PR run on today's scenario (B34,
B36), in the feed reports and in the planner's per-system feed loads.

Engine change 3 of 7 from the numbers audit (feed-05), taken one at a time:
such a batch now gets zero feed, matching its zero growth. Every caller looks
the batch up as batch_meta.get(batch_id), so None means exactly "not in the
scenario"; starving (6N purge) fish are filtered out before the call.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from forecast.biology import realized_feed_kg_day
from forecast.config_io import load_biology_tables
from forecast.models import BatchInput

ROOT = Path(__file__).resolve().parent.parent
TABLES = load_biology_tables(str(ROOT / "config"))

_BATCH = BatchInput(
    batch_id="B50", input_date=date(2025, 1, 6), input_count=300000,
    tran_sf_date=None, tran_og_date=None, tran_og_count=None,
    tran_og_avg_wt_g=None, tran_og_cv=16.0, fcr_model="1.21",
    fw_correction=1.0, sgr_correction=1.0)


def test_a_batch_not_in_the_scenario_is_not_fed():
    assert realized_feed_kg_day(3000.0, 30000.0, None, TABLES, "2026-W40") == 0.0


def test_a_batch_in_the_scenario_is_still_fed():
    """NEGATIVE CONTROL: the same fish with a scenario entry eat as before."""
    assert realized_feed_kg_day(3000.0, 30000.0, _BATCH, TABLES, "2026-W40") > 0.0


def test_the_hydration_warning_says_so():
    """The PR warning names all three things that do not happen."""
    src = (ROOT / "forecast" / "production_report.py").read_text(encoding="utf-8")
    assert "(no growth, no mortality and no feed)" in src
