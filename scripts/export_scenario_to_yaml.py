"""Seed the app-side scenario config from the current workbook.

Reads the forward batch schedule (BatchRegistry) and the facility/system
limits and writes scenario/batches.yaml + scenario/limits.yaml — the Phase 2
scenario tier (docs/DATA_PATH_REDESIGN.md). Limit week-labels are resolved
against the run's forecast_start (= ProductionReport closing + 1, the same
derivation run.py uses), so the seeded labels match a real run.

    python scripts/export_scenario_to_yaml.py [WORKBOOK] [SCENARIO_DIR]
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forecast.excel_io import load_workbook, read_batches, read_control
from forecast.caps import read_facility_limits, read_system_limits
from forecast.production_report import read_production_report
from forecast.scenario_io import dump_scenario


def _derive_forecast_start(wb):
    """Mirror run.py: forecast_start = PR closing + 1 day, else Control."""
    pr_closing, _og, _fw = read_production_report(wb)
    if pr_closing is not None:
        return datetime(pr_closing.year, pr_closing.month, pr_closing.day) + timedelta(days=1)
    return read_control(wb).forecast_start


def main(workbook: str, scenario_dir: str) -> int:
    wb = load_workbook(Path(workbook))
    batches = read_batches(wb)
    fs = _derive_forecast_start(wb)
    fs_date = fs.date() if hasattr(fs, "date") else fs
    facility_limits = read_facility_limits(wb, fs_date)
    system_limits = read_system_limits(wb, fs_date)
    wb.close()

    dump_scenario(scenario_dir, batches=batches,
                  facility_limits=facility_limits, system_limits=system_limits)

    print(f"Wrote scenario config to {scenario_dir}/  (forecast_start {fs_date})")
    print(f"  batches.yaml : {len(batches)} batches")
    print(f"  limits.yaml  : {len(facility_limits.overrides)} facility overrides, "
          f"{len(system_limits.caps)} system caps")
    return 0


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    wb = sys.argv[1] if len(sys.argv) > 1 else str(root / "Forecast.xlsm")
    sc = sys.argv[2] if len(sys.argv) > 2 else str(root / "scenario")
    raise SystemExit(main(wb, sc))
