"""Guards for the 2026-08 silent-fallback audit's two behavioural fixes.

Both are the same defect class: a MISSING measurement defaulted to the
PASSING value, so a read failure looked like a clean result. The fixes make
absence read as "no data" (N/A / INVESTIGATE), never as a pass. Everything
else in that audit batch is log-lines only (no behaviour change) and is not
tested here.
"""
import math
import os
import sys

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forecast import tuning  # noqa: E402


def _wb_with_section_b(tmp_path, header, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "TransferTemplate"
    ws.append(header)
    for r in rows:
        ws.append(r)
    p = tmp_path / "out.xlsx"
    wb.save(p)
    return str(p)


def test_missing_peak_density_column_reads_as_no_data_not_all_clean(tmp_path):
    """DEFECT: with the Peak_Density column absent, every batch parsed as peak
    0.0 and the distribution reported "0 severe, all clean" — a missing
    MEASUREMENT presented as a compliant result. It must read as "no data"."""
    p = _wb_with_section_b(tmp_path, ["Batch", "Tanks"], [["B51", 3], ["B52", 2]])
    peaks, detail = tuning._peaks_and_detail(p)
    assert peaks == [] and detail == []
    # ...and the analysis-layer lens then reports N/A (None), never PASS.
    from forecast.analysis import density_review
    assert density_review(p) is None


def test_present_peak_density_column_still_measures(tmp_path):
    p = _wb_with_section_b(
        tmp_path,
        ["Batch", "Peak_Density_Ratio", "Peak_Wk", "Wks_from_Start"],
        [["B51", 1.42, 10, 4], ["B52", 0.95, 8, 2]])
    peaks, detail = tuning._peaks_and_detail(p)
    assert peaks == [1.42, 0.95]
    assert [d["Batch"] for d in detail] == ["B51"]   # >= DETAIL_RATIO only


def test_scan_audit_drift_missing_facility_row_cannot_read_as_pass():
    """DEFECT: the facility 'Count (fish)' totals initialized to 0.0, so a
    summary row that was missing (or unparseable) DEFAULTED to the passing
    value of the caller's `abs(fac_signed) < 1.0` cleanliness test. NaN fails
    that comparison, so the verdict reads INVESTIGATE."""
    from tools.run_global_forecast import _scan_audit_drift
    wb = openpyxl.Workbook()
    ws = wb.active   # no 'Count (fish)' row at all
    ws.append(["Some", "other", "row"])
    n_tank, n_bio, fac_signed, fac_abs = _scan_audit_drift(ws)
    assert n_tank == 0 and n_bio == 0
    assert math.isnan(fac_signed) and math.isnan(fac_abs)
    assert not (abs(fac_signed) < 1.0)      # the caller's clean test FAILS


def test_scan_audit_drift_parseable_row_unchanged():
    from tools.run_global_forecast import _scan_audit_drift
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Count (fish)", 0.4, 12.0])
    _, _, fac_signed, fac_abs = _scan_audit_drift(ws)
    assert fac_signed == 0.4 and fac_abs == 12.0
    assert abs(fac_signed) < 1.0            # genuinely clean still passes
