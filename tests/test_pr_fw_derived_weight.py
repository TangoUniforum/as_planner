"""A PR batch with a count but no weight is seeded from its own lifecycle.

Growth is multiplicative, so seeding at 0 g keeps a batch at 0 g for life: it
never reaches min_harvest_weight_g, so it is never harvested, and it holds its
tanks until the horizon ends. Measured on the 2026-08-31 PR: B56 sat in tanks
14 and 21 from 2027-W32 to 2029-W05 at avg weight 0, with 0 fish harvested and
two tanks frozen for 80 weeks.

The batch's own lifecycle already says what it should weigh, and it is exactly
what a batch NOT yet in the PR is given: FW_START_WEIGHT_G at tran_sf_date,
then the FW curve under its fw_correction. Nothing is invented -- the same
model runs, from the same constant.

B56's case is the common one: at PR closing it has not hatched (tran_sf
2026-11-06 vs a 2026-08-31 close), which is WHY the report carries a count and
no weight. It is still eggs.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from forecast.biology import FW_START_WEIGHT_G, project_in_flight_fw_batch
from forecast.config_io import load_config
from forecast.models import BatchInput

_CFG = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture(scope="module")
def loaded():
    """The real config + biology tables -- this exercises the growth curves the
    plan actually uses, not a stub that could agree with a broken model."""
    control, tables, _facility = load_config(str(_CFG))
    return control, tables


def _as_day(x):
    return x.date() if hasattr(x, "date") else x


def _batch(tran_sf, **kw):
    return BatchInput(
        batch_id=kw.get("bid", "BX"), input_date=datetime(2026, 8, 20),
        input_count=570_000, tran_sf_date=tran_sf,
        tran_og_date=datetime(2027, 8, 5), tran_og_count=330_000,
        tran_og_avg_wt_g=370.0, tran_og_cv=16.0,
        fcr_model="FCR_116_Quick", fw_correction=0.82, sgr_correction=0.97)


def _run(loaded, batch, wt):
    control, tables = loaded
    states, _r, _s = project_in_flight_fw_batch(
        batch, tables, control, 563_234, wt, date(2026, 8, 31))
    return states


def test_a_zero_weight_batch_no_longer_stays_at_zero(loaded):
    """The regression. On the parent commit every state weighed 0 g."""
    st = _run(loaded, _batch(datetime(2026, 11, 6)), 0.0)
    assert st, "no states produced"
    assert max(s.avg_weight_g for s in st) > 0.0


def test_it_reaches_the_batchs_own_tranog_target(loaded):
    """The consequence that mattered: a 0 g batch never reaches ANY weight, so
    it is never harvestable. The milestone here is the FRESHWATER one this
    function is responsible for -- tran_og_avg_wt_g, 370 g -- not the seawater
    harvest weight, which is reached later by a different projector."""
    b = _batch(datetime(2026, 11, 6))
    st = _run(loaded, b, 0.0)
    assert max(s.avg_weight_g for s in st) > b.tran_og_avg_wt_g


def test_a_pre_hatch_batch_starts_as_EGG_and_weighs_nothing_until_it_hatches(loaded):
    """Matching project_batch day for day: eggs weigh 0 until tran_sf_date."""
    st = _run(loaded, _batch(datetime(2026, 11, 6)), 0.0)
    hatch = date(2026, 11, 6)
    # Strictly BEFORE the hatch week. The week that CONTAINS the hatch date is
    # legitimately FW -- the transition happens inside it -- so testing
    # week_start < hatch would fail on that boundary row and prove nothing.
    before = [s for s in st
              if s.week_start and _as_day(s.week_start) + timedelta(days=7) <= hatch]
    assert before, "fixture must span the pre-hatch period"
    assert all(s.stage == "EGG" for s in before)
    assert all(s.avg_weight_g == 0.0 for s in before)
    # ... and it does not stay an egg.
    assert any(s.stage == "FW" for s in st)


def test_an_already_hatched_batch_seeds_at_hatch_weight(loaded):
    """Start-feed already past at forecast start (config opens 2026-05-15).
    History cannot be reconstructed, so it takes a lower bound -- a growing
    fish rather than a frozen one."""
    st = _run(loaded, _batch(datetime(2026, 3, 1)), 0.0)
    assert st[0].stage != "EGG"
    assert st[0].avg_weight_g >= FW_START_WEIGHT_G
    assert max(s.avg_weight_g for s in st) > 10 * FW_START_WEIGHT_G


def test_a_batch_the_PR_DID_weigh_is_untouched(loaded):
    """The guard. Every batch with a real PR weight must take the identical
    path it always did -- this fix may only reach batches that were broken.
    A weighed batch is never EGG and never takes the hatch-weight seed, even
    when its tran_sf_date is still ahead."""
    st = _run(loaded, _batch(datetime(2026, 11, 6)), 0.207)
    assert all(s.stage != "EGG" for s in st)
    assert st[0].avg_weight_g > 0.207          # grew from the PR weight
    assert st[0].avg_weight_g < 0.207 * 1.5    # ... and did not jump to a seed
