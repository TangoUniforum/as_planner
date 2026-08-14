"""Dump the RESOLVED system cap for every (week, system, metric) in a horizon.

The cap that matters is not what limits.yaml says, it is what the resolver
returns. This prints that table, so a change to the limits SCHEMA can be
proved to leave the ANSWERS alone: dump before, dump after, diff.

    python scripts/dump_resolved_system_caps.py --out before.csv
    # ... migrate ...
    python scripts/dump_resolved_system_caps.py --out after.csv
    diff before.csv after.csv

Weeks come from the forecast grid (`--start` + Control `horizon_weeks`), or
from `--weeks-from-limits` to sweep exactly the weeks the file mentions.
Columns: week,system,metric,cap   with cap empty meaning "no cap set".
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forecast.caps import METRIC_BIOMASS, METRIC_FEED_DAY, resolve_system_cap  # noqa: E402
from forecast.config_io import load_control, load_facility_config  # noqa: E402
from forecast.scenario_io import load_limits  # noqa: E402
from forecast.time_grid import forecast_week_labels  # noqa: E402

METRICS = (METRIC_BIOMASS, METRIC_FEED_DAY)


def _systems(facility_dir) -> list[str]:
    fac = load_facility_config(facility_dir)
    return sorted({t.system_id for t in fac.tanks if t.type == "OG"})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config-dir", default="config")
    ap.add_argument("--scenario-dir", default="scenario")
    ap.add_argument("--start", default=None,
                    help="forecast_start YYYY-MM-DD (default: Control's)")
    ap.add_argument("--horizon", type=int, default=None,
                    help="weeks (default: Control's horizon_weeks)")
    ap.add_argument("--weeks-from-limits", action="store_true",
                    help="sweep the weeks named in limits.yaml instead")
    ap.add_argument("--out", default=None, help="CSV path (default: stdout)")
    a = ap.parse_args(argv)

    control = load_control(a.config_dir)
    _fl, sl = load_limits(a.scenario_dir, control)
    systems = _systems(a.config_dir)

    if a.weeks_from_limits:
        weeks = sorted({wk for (wk, _s, _m) in sl.caps})
    else:
        start = (date.fromisoformat(a.start) if a.start
                 else (control.forecast_start.date()
                       if hasattr(control.forecast_start, "date")
                       else control.forecast_start))
        weeks = forecast_week_labels(start, int(a.horizon or control.horizon_weeks))

    lines = ["week,system,metric,cap"]
    for wk in weeks:
        for s in systems:
            for m in METRICS:
                v = resolve_system_cap(m, wk, s, sl)
                lines.append(f"{wk},{s},{m},{'' if v is None else repr(float(v))}")
    text = "\n".join(lines) + "\n"

    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"{len(lines) - 1} rows -> {a.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
