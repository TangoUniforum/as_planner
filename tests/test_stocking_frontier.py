"""Stocking-frontier scaling — WHICH batches count as 'future' stock.

DEFECT (2026-07-13 audit): the frontier filtered on `tran_og_date > forecast_start`,
so FW-in-flight batches — already stocked and swimming in freshwater, merely not
yet in seawater — were scaled/culled too, contradicting the module's own contract
('fish already in the facility are fixed'). The filter must key on the INPUT
(stocking) date: only a batch not yet stocked is a future stocking decision.
"""
from __future__ import annotations

from datetime import date, datetime

from forecast.models import BatchInput
from forecast.stocking_frontier import scale_future_batches


def _batch(bid, input_date, tran_og_date, n=100000, tran_og_count=90000):
    return BatchInput(
        batch_id=bid, input_date=input_date, input_count=n,
        tran_sf_date=None, tran_og_date=tran_og_date,
        tran_og_count=tran_og_count, tran_og_avg_wt_g=None,
        tran_og_cv=16.0, fcr_model="1.21", fw_correction=1.0,
        sgr_correction=1.0)


def test_fw_in_flight_batches_are_never_scaled():
    """A batch stocked BEFORE forecast_start but entering seawater AFTER it is
    in-facility (FW in-flight) — the old tran_og_date filter cut it."""
    fs = date(2026, 6, 1)
    inflight = _batch("B_INFLIGHT", datetime(2026, 3, 2), datetime(2026, 9, 7))
    future = _batch("B_FUTURE", datetime(2026, 8, 3), datetime(2027, 2, 1))
    past = _batch("B_PAST", datetime(2025, 10, 6), datetime(2026, 4, 6))

    scaled = scale_future_batches([inflight, future, past], fs, keep=0.9)

    assert scaled == 1
    assert inflight.input_count == 100000, "in-flight fish are fixed"
    assert inflight.tran_og_count == 90000
    assert past.input_count == 100000
    assert future.input_count == 90000, "only the un-stocked batch is reduced"
    assert future.tran_og_count == 81000


def test_missing_input_date_is_left_alone():
    b = _batch("B_NODATE", None, datetime(2027, 2, 1))
    assert scale_future_batches([b], date(2026, 6, 1), keep=0.5) == 0
    assert b.input_count == 100000
