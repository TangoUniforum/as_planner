"""Seed the app-side stable config from the current workbook.

Reads Tables (biology), FacilityConfig, and Control from Forecast.xlsm and
writes config/control.yaml, config/biology.yaml, config/facility.yaml — the
Phase 1 stable-config tier (docs/DATA_PATH_REDESIGN.md). Run once to migrate;
re-run to re-sync if the workbook's stable config changes.

    python scripts/export_config_to_yaml.py [WORKBOOK] [CONFIG_DIR]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forecast.excel_io import (
    load_workbook, read_biology_tables, read_control, read_facility_config,
)
from forecast.config_io import dump_config


def main(workbook: str, config_dir: str) -> int:
    wb = load_workbook(Path(workbook))
    control = read_control(wb)
    tables = read_biology_tables(wb)
    facility = read_facility_config(wb)
    wb.close()

    dump_config(config_dir, control=control, tables=tables, facility=facility)

    print(f"Wrote stable config to {config_dir}/")
    print(f"  control.yaml : horizon={control.horizon_weeks}w, "
          f"max_biomass={control.max_biomass_kg:,.0f} kg, "
          f"hog_yield={control.default_hog_yield}")
    print(f"  biology.yaml : {len(tables.sgr_size_g)} SGR rows, "
          f"FCR models {sorted(tables.fcr_by_model)}, "
          f"{len(tables.mortality_pct_weekly)} mortality rows, "
          f"{len(tables.feed_types)} feed types, {len(tables.culling)} cull events")
    print(f"  facility.yaml: {len(facility.tanks)} tanks")
    return 0


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    wb = sys.argv[1] if len(sys.argv) > 1 else str(root / "Forecast.xlsm")
    cfg = sys.argv[2] if len(sys.argv) > 2 else str(root / "config")
    raise SystemExit(main(wb, cfg))
