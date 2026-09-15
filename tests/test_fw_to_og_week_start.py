"""A scripted fw_to_og moves the fish as they are at the START of its week.

Deferred since 2026-09-08, fixed 2026-09-15. The manual window deposits an
fw_to_og cohort in seawater at the week's start and then grows it through that
week there. The lookup it read the cohort from (manual_window._build_fw_lookup,
and the editor's app._mw_fw_avail) indexed each freshwater week by its CLOSE --
the fish after a whole freshwater week of growth -- so that week was grown
twice: 8,431 kg on B49 on the 8/31 PR that never grew (the count unchanged),
its 2026-W36 ledger row reading Bio_FCR 12.70. Invisible to every
conservation surface: they book the arrival as it is handed over.

Both now read one rule, manual_window.fw_week_start_states: the week's
opening count and its weight before the first day's growth.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from forecast.models import BatchInput

ROOT = Path(__file__).resolve().parent.parent
PR_CLOSE = date(2026, 8, 31)
PR_COUNT, PR_KG = 250_225.0, 92_333.0            # B49's freshwater part on 8/31


def _b49():
    return BatchInput(
        batch_id="B49", input_date=datetime(2025, 9, 11), input_count=550_000,
        tran_sf_date=datetime(2025, 12, 8), tran_og_date=datetime(2026, 9, 14),
        tran_og_count=290_000, tran_og_avg_wt_g=370.0, tran_og_cv=16.0,
        fcr_model="FCR_116_Quick", fw_correction=0.9517, sgr_correction=0.95)


@pytest.fixture(scope="module")
def tables():
    from forecast.config_io import load_biology_tables
    return load_biology_tables(ROOT / "config")


@pytest.fixture(scope="module")
def control():
    from forecast.config_io import load_control
    c = load_control(ROOT / "config")
    c.forecast_start = datetime(2026, 9, 1)
    return c


def _fw_rec():
    return SimpleNamespace(batch_id="B49", unit_label="PostS.01", fw_system="PostS",
                           closing_count=PR_COUNT, closing_biomass_kg=PR_KG)


def _lookup(tables, control):
    from forecast.manual_events import ManualDest, ManualEvent
    from forecast.manual_window import _build_fw_lookup
    ev = [ManualEvent(type="fw_to_og", week=1, batch="B49",
                      destinations=[ManualDest(tank=24)])]
    return _build_fw_lookup(ev, [_fw_rec()], control, PR_CLOSE, tables, {"B49": _b49()})


def _states(tables, control):
    from forecast.biology import project_in_flight_fw_batch
    states, _, _ = project_in_flight_fw_batch(
        _b49(), tables, control, PR_COUNT, PR_KG * 1000.0 / PR_COUNT, PR_CLOSE)
    return {s.week_label: s for s in states if s.stage == "FW"}


def test_week_one_moves_the_fish_the_report_counted(tables, control):
    """The first forecast week opens at the PR close: an fw_to_og in it moves
    the report's own count and weight -- not the week's close."""
    lk = _lookup(tables, control)
    n, wt, cv = lk[("B49", "2026-W36")]
    assert n == pytest.approx(PR_COUNT, abs=1)
    assert wt == pytest.approx(PR_KG * 1000.0 / PR_COUNT, abs=0.01)
    assert cv == 16.0
    st = _states(tables, control)["2026-W36"]
    assert wt < st.close_avg_weight_g - 1.0, (
        f"week 1 hands over {wt:.1f} g, the week's close {st.close_avg_weight_g:.1f} g: "
        f"a whole freshwater week of growth before the fish even enter seawater")


def test_a_later_week_moves_the_previous_weeks_close(tables, control):
    """Each week starts where the one before ended."""
    lk = _lookup(tables, control)
    prev = _states(tables, control)["2026-W36"]
    n, wt, _cv = lk[("B49", "2026-W37")]
    assert n == pytest.approx(prev.close_count, rel=1e-9)
    assert wt == pytest.approx(prev.close_avg_weight_g, rel=1e-9)
