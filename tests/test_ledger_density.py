"""Weekly/MonthlyReport `Avg_Density` — the column that used to be a literal 0.

`_ledger_value_cells` once wrote the constant `0` into the density column of
EVERY row of both reports. A column of zeros does not read as "no data", it
reads as "density is fine" — the most consequential thing this project's
reports can say wrongly. That is still the first thing these tests pin.

WHAT CHANGED 2026-09-07. The column was the WORST tank the batch occupied (a
max). The operator asked for the AVERAGE instead, in the reports only: it is
now total biomass over total water. The max's virtue — one over-cap tank cannot
hide inside a roomy average — is real, and it has NOT been lost: it lives in
ValidationLog's per-tank density lines, SystemLimitsAudit, and Batch Plan's
Density_Status / Peak_Density (xcap). None of those read this column, and
nothing in the engine, the gates or the audits does either; it is
presentational. Verified before the change, not assumed.

These pin that the number is real, that it is biomass-weighted (not a mean OF
densities, which would weight a tiny tank like a big one), that it rolls up to
months the same way rather than as a prorated flow, and that the blank case
stays blank.
"""
from __future__ import annotations

from datetime import date

import openpyxl

from forecast import excel_io
from forecast.placement import BatchLocationRow

_MON = date(2026, 8, 3)          # 2026-W32
_MON2 = date(2026, 8, 10)        # 2026-W33
_DENS_COL = _LEDGER_IDX = excel_io._LEDGER_COLS.index("Avg_Density (kg/m³)")


def _loc(week, wkstart, batch, tank, count, wt_g, bio, dens):
    return BatchLocationRow(
        week_label=week, week_start=wkstart, batch_id=batch, tank_id=tank,
        location_id=f"OG3-{tank}", system_id="OG3", count=count,
        avg_wt_g=wt_g, biomass_kg=bio, density_kg_m3=dens)


_LOCS = [
    # B1, W32: three tanks — one of them well over the 95 cap.
    _loc("2026-W32", _MON, "B1", 40, 10_000, 4000.0, 40_000.0, 80.0),
    _loc("2026-W32", _MON, "B1", 41, 10_000, 4000.0, 40_000.0, 142.5),
    _loc("2026-W32", _MON, "B1", 42, 10_000, 4000.0, 40_000.0, 60.0),
    # B1, W33: back inside the cap.
    _loc("2026-W33", _MON2, "B1", 40, 15_000, 4200.0, 63_000.0, 88.0),
    _loc("2026-W33", _MON2, "B1", 41, 15_000, 4200.0, 63_000.0, 91.0),
]


def _rows():
    return excel_io._build_batch_week_ledger(_LOCS, [], [])


def _sheet_rows(ws):
    """(header row, data rows) — the ledger block starts at the header line."""
    all_rows = [r for r in ws.iter_rows(values_only=True)]
    hi = next(i for i, r in enumerate(all_rows)
              if r and r[0] == "Scenario")
    return all_rows[hi], [r for r in all_rows[hi + 1:] if r and r[0] is not None]


class TestAvgDensityIsReal:
    def test_the_column_is_no_longer_a_constant_zero(self):
        """Negative control: on the parent commit every value cell here is 0."""
        vals = [d["peak_density"] for d in _rows()]
        assert any(v for v in vals), "Avg_Density is still all zero/blank"

    def test_it_is_total_biomass_over_total_water(self):
        """W32 holds 3 x 40,000 kg at 80 / 142.5 / 60 kg/m3. Water is
        500 + 280.7 + 666.7 = 1447.4 m3, so the average is 120,000 / 1447.4 =
        82.9 -- NOT the arithmetic mean of the densities (94.2), which would
        weight the small dense tank like the big roomy one."""
        by_week = {(d["batch"], d["week"]): d["peak_density"] for d in _rows()}
        assert round(by_week[("B1", "2026-W32")], 1) == 82.9
        assert round(by_week[("B1", "2026-W33")], 1) == 89.5

    def test_the_average_sits_between_the_tanks_it_averages(self):
        """The invariant that makes it an average at all."""
        by_week = {(d["batch"], d["week"]): d["peak_density"] for d in _rows()}
        assert 60.0 < by_week[("B1", "2026-W32")] < 142.5
        assert 88.0 < by_week[("B1", "2026-W33")] < 91.0

    def test_a_batch_with_no_tank_rows_reads_blank_not_zero(self):
        """No tank that week (a freshwater week carried by the projection) must
        be an EMPTY cell. 0 would say "density is fine"."""
        from forecast.models import BatchWeekState
        s = BatchWeekState(
            batch_id="B9", week_label="2026-W32", week_start=_MON,
            days_since_input=7, week_from_input=1, count=5000.0,
            avg_weight_g=5.0, biomass_kg=25.0, feed_kg_day=1.0,
            feed_kg_week=7.0, sgr_pct_day=3.0, fcr=0.9, stage="FW",
            feed_type="FW", mortality_pct_weekly=0.1)
        rows = excel_io._build_batch_week_ledger(_LOCS, [], [s])
        fw = next(d for d in rows if d["batch"] == "B9")
        assert fw["peak_density"] is None
        assert excel_io._ledger_value_cells(fw)[_DENS_COL] is None


class TestItReachesBothSheets:
    def test_weekly_report_writes_the_average_and_labels_it(self):
        wb = openpyxl.Workbook()
        excel_io.write_weekly_report(wb, _LOCS, [], [])
        ws = wb["WeeklyReport"]
        header, data = _sheet_rows(ws)
        assert header[4 + _DENS_COL] == "Avg_Density (kg/m³)"
        w32 = next(r for r in data if r[1] == "2026-W32" and r[3] == "B1")
        assert w32[4 + _DENS_COL] == 82.9
        # The legend must be IN the sheet, and must say plainly this is NOT a
        # peak - otherwise a reader carries over the old meaning and concludes
        # no tank is over cap.
        blurb = chr(10).join(str(r[0]) for r in ws.iter_rows(values_only=True)
                          if r and r[0] is not None)
        assert "AVERAGE density" in blurb and "not a peak" in blurb

    def test_monthly_rolls_up_as_an_average_not_a_prorated_flow(self):
        wb = openpyxl.Workbook()
        excel_io.write_monthly_report(wb, _LOCS, [], [])
        ws = wb["MonthlyReport"]
        header, data = _sheet_rows(ws)
        assert header[3 + _DENS_COL] == "Avg_Density (kg/m³)"
        aug = next(r for r in data if r[1] == "2026-08" and r[2] == "B1")
        # 246,000 kg over 2,855.6 m3 across both weeks — NOT a sum (172.4),
        # NOT a mean of the weekly averages (86.2), NOT a prorated flow.
        assert aug[3 + _DENS_COL] == 86.1
