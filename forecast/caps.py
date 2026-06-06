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


def decide_week_harvest_count(
    fac_biomass: float,
    fac_growth_kg: float,
    fac_feed_kg_day: float,
    bio_cap: Optional[float],
    feed_cap: Optional[float],
    dev: float,
    weekly_min: float,
    weekly_max: float,
    oldest_mature_avg_wt: float,
) -> float:
    """Reactive 3-state facility maintenance controller.

    Given facility biomass + daily feed and this week's projected growth,
    return the facility-wide harvest *count* that keeps biomass inside the
    R24 ± band:

      * above the upper band            -> harvest at full capacity (pull down)
      * below BOTH biomass + feed bands -> operational floor only (let it build)
      * in band                         -> harvest exactly this week's growth
                                           (hold position)

    Result is hard-clipped to ``[weekly_min, weekly_max]``.

    This is the *pure* decision shared by two callers:

    - the open-loop scheduler (`harvest_scheduler.schedule_harvests`), fed a
      biology *projection*, and
    - the closed-loop placement pipeline (`placement.phase_d_emit_events`),
      fed the REALIZED facility state each week.

    The closed-loop caller passes realized biomass/feed/growth so the decision
    tracks what actually happened in the tanks rather than a decoupled forecast
    — that is what keeps facility biomass in band (the "close the loop" fix).
    """
    bio_band_lo = bio_cap * (1.0 - dev) if bio_cap else None
    bio_band_hi = bio_cap * (1.0 + dev) if bio_cap else None
    feed_band_lo = feed_cap * (1.0 - dev) if feed_cap else None

    below_bio = bio_band_lo is None or fac_biomass < bio_band_lo
    below_feed = feed_band_lo is None or fac_feed_kg_day < feed_band_lo
    overflow_pressure = bio_band_hi is not None and fac_biomass > bio_band_hi

    if overflow_pressure:
        target = weekly_max
    elif below_bio and below_feed:
        target = weekly_min
    elif oldest_mature_avg_wt > 0:
        target = fac_growth_kg * 1000.0 / oldest_mature_avg_wt
    else:
        target = weekly_min

    return max(weekly_min, min(weekly_max, target))


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
