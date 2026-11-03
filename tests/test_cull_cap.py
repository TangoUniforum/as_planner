"""Scheduled FW culls never take a batch below its TranOG target.

Operator rule 2026-11-03: "the culling plan in the app should never cull below
the transfer to OG plan count for each batch". The culling plan (biology.yaml
`culling`, % per cull) used to fire its full % regardless: a batch stocked
short (or one already in FW at the PR with fewer fish) was culled 3 x 7% and
arrived in seawater below tran_og_count -- measured on the 10.31 PR with 7%
culls, B55 arrived at 289,299 against a 310,000 target.

Now each cull takes its % only while the count it leaves, after mortality to
TranOG_Date and the transfer handling loss, still reaches the target; else it
takes just the surplus, or nothing. A batch with a surplus is culled exactly
as before.
"""
from __future__ import annotations

import copy
import dataclasses
from datetime import date, datetime
from pathlib import Path

import pytest

from forecast.biology import (
    project_batch, project_in_flight_fw_batch, scheduled_cull_fractions,
    solve_fw_correction,
)
from forecast.config_io import load_config
from forecast.models import BatchInput

_CFG = Path(__file__).resolve().parents[1] / "config"
_CULLS_7 = [(170, 7.0), (215, 7.0), (260, 7.0)]


@pytest.fixture(scope="module")
def loaded():
    """Real config + biology tables, with the operator's 7% culling plan."""
    control, tables, _facility = load_config(str(_CFG))
    tables = copy.copy(tables)
    tables.culling = list(_CULLS_7)
    return control, tables


def _batch(input_count, target, **kw):
    return BatchInput(
        batch_id=kw.get("bid", "BX"), input_date=datetime(2026, 6, 1),
        input_count=input_count, tran_sf_date=datetime(2026, 8, 20),
        tran_og_date=datetime(2027, 5, 13), tran_og_count=target,
        tran_og_avg_wt_g=370.0, tran_og_cv=16.0,
        fcr_model="FCR_116_Quick", fw_correction=0.82, sgr_correction=1.0)


def _arrival(loaded, batch):
    control, tables = loaded
    _rows, _resid, split = project_batch(batch, tables, control, warnings=[])
    assert split is not None, "batch never reached seawater in the horizon"
    return split.post_cull_count


def _culled_pct_by_event(loaded, batch):
    control, tables = loaded
    rows, _r, _s = project_batch(batch, tables, control, warnings=[])
    return [r.cull_event_pct for r in rows if r.cull_event_pct > 0]


def test_a_short_batch_still_arrives_at_its_target(loaded):
    """The regression. 520k eggs culled 3 x 7% + mortality arrive well under
    330k on the parent commit; with the cap they land on the target."""
    b = _batch(520_000, 330_000)
    assert _arrival(loaded, b) >= 330_000 * 0.9995


def test_negative_control_the_uncapped_plan_would_arrive_short(loaded):
    """Proves the test above can fail: the same batch under the plain table %
    (what the parent commit applied) ends below target."""
    control, tables = loaded
    b = _batch(520_000, 330_000)
    plain = scheduled_cull_fractions(
        dataclasses.replace(b, tran_og_count=0), tables,
        control.handling_mortality_pct / 100.0, b.input_date, b.input_count,
        fw_from=b.tran_sf_date)
    assert all(abs(v - 0.07) < 1e-12 for v in plain.values())
    capped = scheduled_cull_fractions(
        b, tables, control.handling_mortality_pct / 100.0, b.input_date,
        b.input_count, fw_from=b.tran_sf_date)
    assert sum(capped.values()) < sum(plain.values()), "cap never bit"


def test_a_batch_with_surplus_is_culled_exactly_as_before(loaded):
    """570k eggs for a 290k target: every cull fires its full 7%."""
    b = _batch(570_000, 290_000)
    pcts = _culled_pct_by_event(loaded, b)
    assert len(pcts) == 3 and all(p == 7.0 for p in pcts)
    assert _arrival(loaded, b) == pytest.approx(290_000, rel=1e-3)  # transit-week mortality


def test_the_cap_takes_the_surplus_then_nothing(loaded):
    """Culls are capped in date order: early ones take what they can, a later
    one that would breach the target takes less or nothing -- never negative."""
    control, tables = loaded
    b = _batch(520_000, 330_000)
    f = scheduled_cull_fractions(
        b, tables, control.handling_mortality_pct / 100.0, b.input_date,
        b.input_count, fw_from=b.tran_sf_date)
    seq = [f[d] for d, _p in _CULLS_7]
    assert all(0.0 <= x <= 0.07 for x in seq)
    assert seq == sorted(seq, reverse=True)


def test_no_target_means_the_plain_plan(loaded):
    control, tables = loaded
    b = _batch(520_000, 0)
    f = scheduled_cull_fractions(
        b, tables, control.handling_mortality_pct / 100.0, b.input_date,
        b.input_count, fw_from=b.tran_sf_date)
    assert all(abs(v - 0.07) < 1e-12 for v in f.values())


def test_an_in_flight_fw_batch_is_capped_too(loaded):
    """A batch already in FW at the PR, with fewer fish than the plan needs
    (the B54/B55 case), still arrives at its target."""
    control, tables = loaded
    b = _batch(570_000, 330_000)
    ctl = dataclasses.replace(control, forecast_start=datetime(2026, 9, 1))
    _st, _r, splits = project_in_flight_fw_batch(
        b, tables, ctl, 500_000, 20.0, date(2026, 8, 31))
    assert splits, "no OG entry"
    assert splits[0].post_cull_count >= 330_000 * 0.9995


def test_calibration_uses_the_capped_culls(loaded):
    """The FW-correction solver must apply the SAME capped culls as the
    projection (a bottom cull lifts the mean weight), or the solved correction
    misses the TranOG weight target."""
    control, tables = loaded
    b = _batch(520_000, 330_000)
    s = solve_fw_correction(b, tables, control.handling_mortality_pct)
    assert s is not None
    _rows, resid, _split = project_batch(
        dataclasses.replace(b, fw_correction=s), tables, control, warnings=[])
    assert abs(resid.residual_pct) < 0.05
