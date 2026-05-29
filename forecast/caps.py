"""Cap resolution.

Three cap levels (DESIGN §6):

- **Tank**: density × volume per tank (from FacilityConfig). No buffer.
- **System**: per-week per-system per-metric (from SystemLimits sheet).
  Blank cell → no cap for that (week, system, metric). Buffered by
  Control R29 `Global buffer` (5% default).
- **Facility**: per-week per-metric. Default from Control; per-week
  override from FacilityLimits (blank cell → use Control default).
  Buffered by Control R24 `Target Biomass deviation` (±1% default) on
  biomass and feed. Harvest count caps + HOG yield are strict.

Buffers are symmetric (cap × (1 ± buffer_pct)).

System-id naming:
- FacilityConfig identifies systems as `OG1N`, `OG1S`, ..., `OG6S`.
- SystemLimits sheet labels them `1N`, `1S`, ..., `6S` (no `OG` prefix).
- Internally we normalize to the FacilityConfig convention.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from .models import ControlParams
from .time_grid import iso_week_label, label_for_date


# Canonical metric tokens.
METRIC_BIOMASS = "biomass"
METRIC_FEED_DAY = "feed_per_day"
METRIC_MAX_HARVEST = "max_harvest_per_week"
METRIC_MIN_HARVEST = "min_harvest_per_week"
METRIC_HOG_YIELD = "hog_yield"


_FACILITY_METRIC_ALIASES = {
    "biomass (kg)": METRIC_BIOMASS,
    "biomass": METRIC_BIOMASS,
    "feed/day (kg/day)": METRIC_FEED_DAY,
    "feed/day": METRIC_FEED_DAY,
    "max harvest/week": METRIC_MAX_HARVEST,
    "min harvest/week": METRIC_MIN_HARVEST,
    "hog yield": METRIC_HOG_YIELD,
    "hog loss": METRIC_HOG_YIELD,
}

_SYSTEM_METRIC_ALIASES = {
    "biomass": METRIC_BIOMASS,
    "feed/day": METRIC_FEED_DAY,
}


def normalize_system(label: str) -> str:
    """SystemLimits-sheet label → FacilityConfig system_id.

    'OG1N' stays 'OG1N'; '1N' becomes 'OG1N'; '6N' becomes 'OG6N'.
    """
    s = (label or "").strip()
    if not s:
        return ""
    if s.upper().startswith("OG"):
        return s
    return f"OG{s}"


# ============================================================
# Data containers
# ============================================================

@dataclass
class FacilityLimits:
    """Per-week per-metric facility-level overrides.

    Keys are (week_label, metric) where week_label is the ISO label
    like "2026-W20". Blank cell on the sheet → use Control default
    (resolution below).
    """
    overrides: dict[tuple[str, str], float] = field(default_factory=dict)

    def get(self, week_label: str, metric: str, default: float) -> float:
        return self.overrides.get((week_label, metric), default)


@dataclass
class SystemLimits:
    """Per-week per-system per-metric system-level caps.

    Keys are (week_label, system_id, metric). Blank cell → no cap
    (None returned by get).
    """
    caps: dict[tuple[str, str, str], float] = field(default_factory=dict)

    def get(self, week_label: str, system_id: str, metric: str) -> Optional[float]:
        return self.caps.get((week_label, system_id, metric))


# ============================================================
# Sheet readers
# ============================================================

def _find_date_header_row(rows) -> Optional[int]:
    """Find the first row containing date/datetime cells from col 3 onward."""
    for i, row in enumerate(rows):
        if not row:
            continue
        for v in row[2:]:
            if isinstance(v, (date, datetime)):
                return i
    return None


def _build_col_to_label(date_row, forecast_start) -> dict[int, str]:
    """Map each date-cell column index → ISO week label.

    Uses `label_for_date` so the column's date is resolved against the
    forecast grid (so the first column dated at forecast_start gets the
    ISO label of forecast_start's containing week).
    """
    out: dict[int, str] = {}
    for c, v in enumerate(date_row):
        if c < 2:
            continue
        if isinstance(v, (date, datetime)):
            out[c] = label_for_date(v, forecast_start)
    return out


def read_facility_limits(wb, forecast_start) -> FacilityLimits:
    """Parse the FacilityLimits sheet."""
    if "FacilityLimits" not in wb.sheetnames:
        return FacilityLimits()
    ws = wb["FacilityLimits"]
    rows = list(ws.iter_rows(values_only=True))
    dr = _find_date_header_row(rows)
    if dr is None:
        return FacilityLimits()
    col_week = _build_col_to_label(rows[dr], forecast_start)

    overrides: dict[tuple[str, str], float] = {}
    for row in rows[dr + 1:]:
        if not row or len(row) < 2:
            continue
        metric_cell = row[1]
        if not isinstance(metric_cell, str):
            continue
        metric = _FACILITY_METRIC_ALIASES.get(metric_cell.strip().lower())
        if metric is None:
            continue
        for c, label in col_week.items():
            if c >= len(row):
                continue
            v = row[c]
            if isinstance(v, (int, float)) and v != 0:
                overrides[(label, metric)] = float(v)
    return FacilityLimits(overrides=overrides)


def read_system_limits(wb, forecast_start) -> SystemLimits:
    """Parse the SystemLimits sheet."""
    if "SystemLimits" not in wb.sheetnames:
        return SystemLimits()
    ws = wb["SystemLimits"]
    rows = list(ws.iter_rows(values_only=True))
    dr = _find_date_header_row(rows)
    if dr is None:
        return SystemLimits()
    col_week = _build_col_to_label(rows[dr], forecast_start)

    caps: dict[tuple[str, str, str], float] = {}
    for row in rows[dr + 1:]:
        if not row or len(row) < 2:
            continue
        system_cell = row[0]
        metric_cell = row[1]
        if not isinstance(system_cell, str) or not isinstance(metric_cell, str):
            continue
        sys_id = normalize_system(system_cell)
        if not sys_id:
            continue
        metric = _SYSTEM_METRIC_ALIASES.get(metric_cell.strip().lower())
        if metric is None:
            continue
        for c, label in col_week.items():
            if c >= len(row):
                continue
            v = row[c]
            if isinstance(v, (int, float)) and v != 0:
                caps[(label, sys_id, metric)] = float(v)
    return SystemLimits(caps=caps)


# ============================================================
# Resolution helpers
# ============================================================

def resolve_facility_cap(
    metric: str,
    week_label: str,
    facility_limits: FacilityLimits,
    control: ControlParams,
) -> Optional[float]:
    """Return the resolved facility-cap value (override → Control default).

    Returns None if the metric isn't a facility cap or the default is 0.
    Buffer is NOT applied here — call `apply_facility_buffer` for the
    symmetric (lo, hi) tolerance band.
    """
    defaults = {
        METRIC_BIOMASS: control.max_biomass_kg,
        METRIC_FEED_DAY: control.max_feed_per_day_kg,
        METRIC_MAX_HARVEST: control.max_harvest_per_week,
        METRIC_MIN_HARVEST: control.min_harvest_per_week,
        METRIC_HOG_YIELD: control.default_hog_yield,
    }
    if metric not in defaults:
        return None
    val = facility_limits.get(week_label, metric, defaults[metric])
    return val if val > 0 else None


def apply_facility_buffer(
    cap_value: float,
    metric: str,
    control: ControlParams,
) -> tuple[float, float]:
    """Symmetric ± tolerance band around a facility cap.

    Biomass and feed use Control R24 `Target Biomass deviation`.
    Harvest counts and HOG yield are strict (returned as (cap, cap)).
    """
    if metric in (METRIC_BIOMASS, METRIC_FEED_DAY):
        b = control.facility_biomass_deviation_pct  # R24 ± deviation
        return (cap_value * (1.0 - b), cap_value * (1.0 + b))
    return (cap_value, cap_value)


def resolve_system_cap(
    metric: str,
    week_label: str,
    system_id: str,
    system_limits: SystemLimits,
) -> Optional[float]:
    """Return the resolved system cap or None if no cap is set."""
    return system_limits.get(week_label, system_id, metric)


def apply_system_buffer(
    cap_value: float,
    control: ControlParams,
) -> tuple[float, float]:
    """Symmetric ± tolerance band around a system cap (Control R29 buffer)."""
    # ControlParams.target_biomass_pct holds R24 (facility); we want R29
    # global buffer. Caller passes whichever Control field is correct;
    # default Control reader puts R29 into a separate field — see run.py
    # wiring. For now this helper takes the buffer fraction directly via
    # the control param's value; we expose it as a free function below.
    raise NotImplementedError(
        "use apply_buffer_pct(value, buffer_pct) — Control.global_buffer_pct "
        "is the buffer fraction; reader will be wired in next step"
    )


def apply_buffer_pct(cap_value: float, buffer_pct: float) -> tuple[float, float]:
    """Symmetric ± tolerance band, buffer_pct as a fraction (0.05 = 5%)."""
    return (cap_value * (1.0 - buffer_pct), cap_value * (1.0 + buffer_pct))
