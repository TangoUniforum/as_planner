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


def system_cap_with_buffer(
    cap_value: float,
    control: ControlParams,
) -> float:
    """Upper-bound system cap after applying R29 global buffer.

    System caps are one-sided: a cap is a ceiling, so the buffer
    (Control R29 `Global buffer`, default 5%) gives the planner extra
    headroom above the raw cap before refusing to place more load.
    Returns `cap_value * (1 + R29)`. Unlike the symmetric R24 facility
    biomass band, there is no lower bound — being under a system cap is
    never a problem.
    """
    return cap_value * (1.0 + control.global_buffer_pct)
