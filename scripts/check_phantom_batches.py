"""Check whether stale registry batches (no PR presence, past TranOG)
get phantom-simulated as living fish inside the forecast window.

Projects the *incoming* batches (same exclusion logic as run.py) and
reports, per suspect batch, how many batch-week states land inside the
forecast window and the biomass they carry at the start of the horizon.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forecast.excel_io import (
    load_workbook, read_batches, read_biology_tables, read_control,
    read_facility_config,
)
from forecast.production_report import read_production_report
from forecast.biology import project_all_batches

SUSPECTS = {"B37", "B38", "B39", "B40"}


def main(path: str) -> int:
    wb = load_workbook(Path(path))
    control = read_control(wb)
    batches = read_batches(wb)
    tables = read_biology_tables(wb)
    _facility = read_facility_config(wb)
    pr_closing, og_records, fw_records = read_production_report(wb)
    wb.close()

    fs = control.forecast_start
    fs_date = fs.date() if hasattr(fs, "date") else fs
    horizon_end = fs_date + timedelta(weeks=control.horizon_weeks)
    pr_ids = {r.batch_id for r in og_records} | {r.batch_id for r in fw_records}

    # Same exclusion as run.py: PR-present batches are projected as in-flight.
    incoming = [b for b in batches if b.batch_id not in pr_ids]
    states, _resid, _splits, _warns = project_all_batches(incoming, tables, control)

    print(f"forecast window: {fs_date} .. {horizon_end}")
    print(f"PR-present (in-flight) batches: {sorted(pr_ids)}")
    print()
    print(f"{'Batch':<6} {'in-window wks':>13} {'first wk':>10} "
          f"{'open bio kg':>12} {'open avgwt g':>13} {'open count':>11}")
    print("-" * 70)

    by_batch: dict[str, list] = {}
    for s in states:
        by_batch.setdefault(s.batch_id, []).append(s)

    any_phantom = False
    for bid in sorted(SUSPECTS):
        ss = sorted(
            [s for s in by_batch.get(bid, [])
             if fs_date <= (s.week_start.date() if hasattr(s.week_start, "date")
                            else s.week_start) <= horizon_end],
            key=lambda s: s.week_start,
        )
        if not ss:
            print(f"{bid:<6} {'0':>13}   (no in-window states — correctly ignored)")
            continue
        any_phantom = True
        f = ss[0]
        print(f"{bid:<6} {len(ss):>13} {str(f.week_label):>10} "
              f"{f.biomass_kg:>12,.0f} {f.avg_weight_g:>13,.0f} {f.count:>11,.0f}")

    print()
    if any_phantom:
        print(">>> PHANTOM RISK: one or more stale batches carry biomass INTO the")
        print("    forecast window. They are not in PR, so this biomass is not part")
        print("    of the real current state — it inflates the front of the horizon.")
    else:
        print("OK: none of the suspect batches produce in-window states. The biology")
        print("    engine already drops them; no action needed beyond tidiness.")
    return 0


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else str(
        Path(__file__).resolve().parent.parent / "Forecast.xlsm"
    )
    raise SystemExit(main(p))
