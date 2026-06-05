"""Read-only cross-sheet alignment check.

Detects the mismatches that show up when only *some* input sheets are
refreshed: Control forecast_start vs ProductionReport closing date,
batch IDs present in PR but missing from BatchRegistry (silently dropped
in-flight fish), PR tank IDs not in FacilityConfig, stale pins/limits
pointing at weeks outside the new horizon, and FCR models with no Tables
column.

Does NOT mutate the workbook.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forecast.excel_io import (
    load_workbook, read_batches, read_biology_tables, read_control,
    read_facility_config, read_pinned_harvests, read_pinned_transfers,
)
from forecast.production_report import read_production_report
from forecast.time_grid import forecast_week_labels


def main(path: str) -> int:
    wb = load_workbook(Path(path))
    control = read_control(wb)
    batches = read_batches(wb)
    tables = read_biology_tables(wb)
    facility = read_facility_config(wb)
    pr_closing, og_records, fw_records = read_production_report(wb)

    fs = control.forecast_start
    fs_date = fs.date() if hasattr(fs, "date") else fs
    print("=" * 70)
    print("CROSS-SHEET ALIGNMENT CHECK")
    print("=" * 70)
    print(f"Sheets present: {wb.sheetnames}")
    print()

    # ---- 1. Control forecast_start vs PR closing date ----
    # NOTE: run.py now DERIVES forecast_start from the PR closing date
    # (= closing + 1 day), mirroring the VBA DetectForecastStart(). So a
    # discrepancy here is auto-healed by the tool — it's informational,
    # not a blocker. This check still reports it so you can see what the
    # tool will actually use vs what the Control sheet currently stores.
    print("[1] Control.forecast_start  vs  ProductionReport closing date")
    print(f"    Control sheet value     = {fs_date}")
    print(f"    horizon                 = {control.horizon_weeks} weeks")
    print(f"    PR closing date         = {pr_closing}")
    if pr_closing is None:
        print("    >>> CHECK: PR closing date missing/unparseable. The tool will")
        print("        FALL BACK to the Control sheet value above. Make sure it's")
        print("        the intended start (run.py logs 'WARN - ForecastStart').")
    else:
        derived = pr_closing + timedelta(days=1)
        print(f"    tool will USE           = {derived}  (PR closing + 1 day, derived)")
        if derived != fs_date:
            print(f"    note: Control sheet ({fs_date}) is stale and will be IGNORED;")
            print(f"          the tool self-corrects to {derived}. No action needed.")
        else:
            print("    OK Control matches the derived start.")
    print()

    # ---- 2. PR batch IDs vs BatchRegistry ----
    reg_ids = {b.batch_id for b in batches}
    pr_og_ids = {r.batch_id for r in og_records}
    pr_fw_ids = {r.batch_id for r in fw_records}
    pr_ids = pr_og_ids | pr_fw_ids
    print("[2] ProductionReport batches  vs  BatchRegistry")
    print(f"    BatchRegistry batches   = {sorted(reg_ids)}")
    print(f"    PR OG-tank batches      = {sorted(pr_og_ids)}")
    print(f"    PR FW-unit batches      = {sorted(pr_fw_ids)}")
    missing = sorted(pr_ids - reg_ids)
    if missing:
        print(f"    >>> PROBLEM: in PR but NOT in BatchRegistry: {missing}")
        print(f"        These in-flight fish are SILENTLY DROPPED from the")
        print(f"        projection (run.py: b_meta is None -> continue).")
    else:
        print("    OK every PR batch has a BatchRegistry row.")
    only_reg = sorted(reg_ids - pr_ids)
    if only_reg:
        print(f"    note: in BatchRegistry only (treated as incoming/new): {only_reg}")
    print()

    # ---- 3. PR OG tank IDs vs FacilityConfig ----
    fac_tank_ids = {t.tank_id for t in facility.tanks}
    pr_tank_ids = {r.tank_id for r in og_records}
    print("[3] ProductionReport OG tanks  vs  FacilityConfig")
    unknown = sorted(pr_tank_ids - fac_tank_ids)
    if unknown:
        print(f"    >>> PROBLEM: PR references tank IDs not in FacilityConfig: {unknown}")
        print(f"        Those tanks' fish are dropped (hydration warning).")
    else:
        print(f"    OK all {len(pr_tank_ids)} PR tank IDs exist in FacilityConfig.")
    print()

    # ---- 4. FCR models vs Tables ----
    model_cols = set(tables.fcr_by_model.keys())
    print("[4] BatchRegistry FCR_Model  vs  Tables FCR columns")
    print(f"    Tables provides models  = {sorted(model_cols)}")
    bad = sorted({b.fcr_model for b in batches if b.fcr_model and b.fcr_model not in model_cols})
    if bad:
        print(f"    >>> PROBLEM: batches reference FCR models with no Tables column: {bad}")
    else:
        print("    OK all referenced FCR models resolve.")
    print()

    # ---- 5. Batch dates vs horizon window ----
    horizon_end = fs_date + timedelta(weeks=control.horizon_weeks)
    print("[5] BatchRegistry dates  vs  forecast window")
    print(f"    window = {fs_date} .. {horizon_end}")
    stale_tranog = []
    future_only = []
    for b in batches:
        tog = b.tran_og_date
        tog_d = tog.date() if hasattr(tog, "date") else tog
        if tog_d is None:
            continue
        if tog_d < fs_date and b.batch_id not in pr_ids:
            stale_tranog.append((b.batch_id, tog_d))
        if tog_d > horizon_end:
            future_only.append((b.batch_id, tog_d))
    if stale_tranog:
        print(f"    >>> CHECK: incoming batches with TranOG in the PAST and no PR")
        print(f"        presence (their entry week already elapsed):")
        for bid, d in stale_tranog:
            print(f"          {bid}: TranOG {d}")
    if future_only:
        print(f"    note: batches with TranOG beyond horizon (won't enter): "
              f"{[f'{b}@{d}' for b,d in future_only]}")
    if not stale_tranog and not future_only:
        print("    OK batch entry dates sit inside the window.")
    print()

    # ---- 6. Pins / limits week labels vs horizon ----
    labels = set(forecast_week_labels(fs_date, control.horizon_weeks))
    ph = read_pinned_harvests(wb, fs_date)
    pt = read_pinned_transfers(wb, fs_date)
    print("[6] Pins  vs  forecast horizon weeks")
    print(f"    pinned harvests = {len(ph)}, pinned transfers = {len(pt)}")
    for tag, pins in (("harvest", ph), ("transfer", pt)):
        for p in pins:
            if p.week_label is None:
                print(f"    >>> {tag} pin batch {p.batch_id}: week cell "
                      f"{p.raw_week_cell!r} did not parse")
            elif p.week_label not in labels:
                print(f"    >>> {tag} pin batch {p.batch_id}: week {p.week_label} "
                      f"is OUTSIDE the new horizon")
    if not ph and not pt:
        print("    (no pins set)")
    print()

    wb.close()
    print("=" * 70)
    print("Done. Lines marked '>>> PROBLEM' / '>>> CHECK' are the mismatches.")
    return 0


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else str(
        Path(__file__).resolve().parent.parent / "Forecast.xlsm"
    )
    raise SystemExit(main(p))
