"""Whole fish in TransferPlan tie to the event they split.

The engine carries fractional fish. A two-tank TranOG of 289,917.2 fish split
144,958.6 + 144,958.6 printed 144,959 + 144,959 = 289,918 on TransferPlan (each
leg rounded on its own) while BatchInputs, Batch Plan and the
InputConservationAudit said 289,917 for the same arrival (Oct Forecast V2,
B51-B61). Operator 2026-10-04: "fix the rounding errors". Every multi-leg
event now prints legs that SUM to round(event total) -- excel_io.whole_parts,
the rule the harvest sheets already use.
"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import openpyxl

from forecast.batch_inputs_report import COLUMNS, batch_input_rows
from forecast.events import TankAllocation, TranOGEntry, Transfer
from forecast.excel_io import write_transfer_plan_output
from forecast.models import BatchInput

COL = {name: i for i, (name, _m) in enumerate(COLUMNS)}
LEG = 289917.2 / 2          # 144,958.6 -- both legs round UP on their own


def _tranog(bid, when, *legs):
    return TranOGEntry(batch_id=bid, event_date=when, destinations=[
        TankAllocation(tank_id=t, count=n, avg_wt_g=400.0, cv_pct=16.0)
        for t, n in legs])


def _plan_rows(transfers, tranogs):
    wb = openpyxl.Workbook()
    write_transfer_plan_output(wb, transfers, tranogs)
    r = list(wb["TransferPlan"].iter_rows(values_only=True))
    hi = next(i for i, x in enumerate(r) if x and x[0] == "Week")
    h = {c: i for i, c in enumerate(r[hi])}
    return [(x[h["Batch"]], x[h["Type"]], x[h["To_Tank"]], x[h["Count (fish)"]])
            for x in r[hi + 1:] if x and x[0]]


def test_tranog_legs_sum_to_the_rounded_event_total():
    rows = _plan_rows([], [_tranog("B51", date(2026, 12, 7), (12, LEG), (16, LEG))])
    legs = [n for b, t, _tk, n in rows if b == "B51" and t == "TranOG"]
    assert len(legs) == 2
    assert sum(legs) == round(2 * LEG) == 289917
    assert sorted(legs) == [144958, 144959]


def test_transfer_legs_sum_to_the_rounded_event_total():
    tr = Transfer(batch_id="B46", event_date=date(2026, 11, 16), source_tank_id=45,
                  destinations=[TankAllocation(tank_id=t, count=10000.5,
                                               avg_wt_g=2700.0, cv_pct=16.0)
                                for t in (53, 56)],
                  leaves_source_empty=False)
    tr.count_transferred = 20001.0      # as apply() records it; 0 = refused, skipped
    rows = _plan_rows([tr], [])
    legs = [n for b, t, _tk, n in rows if t == "Transfer"]
    assert sum(legs) == round(20001.0) == 20001


def test_batch_inputs_ties_to_transferplan_fish_for_fish():
    ev = _tranog("B51", date(2026, 12, 7), (12, LEG), (16, LEG))
    plan = sum(n for b, t, _tk, n in _plan_rows([], [ev]) if t == "TranOG")
    b = BatchInput(batch_id="B51", input_date=datetime(2025, 12, 19),
                   input_count=550000, tran_sf_date=datetime(2026, 3, 1),
                   tran_og_date=datetime(2026, 12, 3), tran_og_count=290000,
                   tran_og_avg_wt_g=370.0, tran_og_cv=16.0,
                   fcr_model="FCR_116_Quick", fw_correction=0.9, sgr_correction=0.95)
    rows = batch_input_rows(
        batches_input=[b], batch_by_id_used={}, tranog_events=[ev],
        control=SimpleNamespace(auto_calibrate_fw=True, auto_calibrate_fw_min=0.5,
                                auto_calibrate_fw_max=1.5),
        tables=SimpleNamespace(fcr_by_model={"1.16": [1.0]}),
        forecast_start=date(2026, 10, 1), pr_fw={"B51"})
    assert rows[0][COL["Count into OG (run, fish)"]] == plan == 289917
