"""Typed inputs and intermediate state for the forecast pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class ControlParams:
    forecast_start: datetime
    horizon_weeks: int
    scenario_name: str
    max_feed_per_day_kg: float
    max_biomass_kg: float
    max_harvest_per_week: float
    min_harvest_weight_g: float
    min_harvest_per_week: float
    min_tank_control: float
    max_tank_density_kg_m3: float
    default_hog_yield: float
    # R24: symmetric ± deviation around facility biomass + feed caps.
    # 0.01 means the planner accepts being within 1% above or below target.
    facility_biomass_deviation_pct: float
    # Control sheet "Handling Mortality" stores a percentage value (e.g. 0.01
    # means 0.01%).  Apply as fraction = handling_mortality_pct / 100.
    handling_mortality_pct: float
    sixn_growth: bool
    sixn_production_start: Optional[datetime] = None
    sixn_transition_weeks: Optional[int] = None
    # R28-R30 (added 2026-05).
    tran_og_default_tanks: int = 3
    global_buffer_pct: float = 0.05      # R29: system-limits symmetric buffer
    starvation_period_days: int = 10     # R30: in-place purge length (production mode)
    # R31: per-tank density target as a fraction of the cap. Drives
    # precalc `tanks_needed_at_density_cap` sizing, the Phase D Grade-
    # split trigger, and the PR-concentration advisory/action gate.
    # 0.85 leaves 15% headroom for growth between weekly checks.
    density_target_pct: float = 0.85


@dataclass
class BatchInput:
    batch_id: str
    input_date: datetime
    input_count: int
    tran_sf_date: Optional[datetime]
    tran_og_date: Optional[datetime]
    tran_og_count: Optional[int]
    tran_og_avg_wt_g: Optional[float]       # pre-cull target avg weight at TranOG
    tran_og_cv: float                       # CV of batch size distribution (%)
    fcr_model: str                          # "FCR_121_Quick" -> "1.21"
    fw_correction: float                    # multiplier on SGR_FW (FW calibration)
    sgr_correction: float                   # multiplier on SGR_SW (SW user adjustment)
    notes: str = ""


@dataclass
class CalibrationResidual:
    """Per-batch FW calibration residual at TranOG_Date."""
    batch_id: str
    tran_og_date: datetime
    target_avg_wt_g: float
    current_fw_correction: float
    projected_pre_cull_avg_wt_g: float
    residual_pct: float                     # (projected - target) / target * 100
    suggested_fw_correction: Optional[float] = None  # back-solved to land on target


@dataclass
class PinnedHarvest:
    """Operator-pinned harvest event read from the HarvestPlan input sheet.

    Treated as a hard constraint by the planner. `week_label` is the
    canonical ISO label ("2026-W20"); `raw_week_cell` preserves the
    original cell text so the planner can diagnose unparseable rows.
    """
    week_label: Optional[str]      # ISO "YYYY-Www" or None if unparseable
    raw_week_cell: str             # raw 'Week' cell text from the sheet
    batch_id: str
    tank_id: int
    count: float
    gross_avg_wt_kg: float
    gross_biomass_kg: float
    hog_yield: float
    hog_avg_wt_kg: float
    hog_biomass_kg: float


@dataclass
class PinnedTransfer:
    """Operator-pinned transfer event read from the TransferPlan input sheet.

    Treated as a hard constraint. `from_tank` may be the sentinel 'FW'
    for TranOG-entry rows; otherwise it is a stringified tank_id.
    """
    week_label: Optional[str]
    raw_week_cell: str
    batch_id: str
    from_tank: str
    to_tank: str
    count: float
    avg_weight_kg: float
    grade: str
    cv_pct: float


@dataclass
class SizeClassSplit:
    """Post-TranOG 2-class split metadata.

    Computed at the moment of TranOG handling-mortality + reconciliation
    cull. Placement consumes this to seed per-(batch, tank) sub-populations
    across N tanks. Median split of the post-cull distribution; the lower
    half goes to the 'small' class, upper half to the 'big' class.
    """
    batch_id: str
    tran_og_date: datetime
    post_cull_count: float
    post_cull_avg_wt_g: float       # mean of full distribution
    post_cull_cv_pct: float         # CV of distribution (typically batch.tran_og_cv)
    big_class_count: float          # = post_cull_count / 2
    big_class_avg_wt_g: float       # mean of upper half (above median)
    small_class_count: float        # = post_cull_count / 2
    small_class_avg_wt_g: float     # mean of lower half (below median)


@dataclass
class BiologyTables:
    """Lookup tables parsed from the Tables sheet.

    Each list is sorted ascending by the key column.
    """
    sgr_size_g: list[float] = field(default_factory=list)
    sgr_fw_pct_day: list[Optional[float]] = field(default_factory=list)
    sgr_sw_pct_day: list[Optional[float]] = field(default_factory=list)
    fcr_size_g: list[float] = field(default_factory=list)
    # Three FCR models keyed by their numeric tag "1.21", "1.18", "1.16".
    fcr_by_model: dict[str, list[float]] = field(default_factory=dict)
    mortality_week_from_input: list[int] = field(default_factory=list)
    mortality_pct_weekly: list[float] = field(default_factory=list)
    # Feed-type schedule: (max_size_g, feed_name), sorted ascending by max_size.
    feed_types: list[tuple[float, str]] = field(default_factory=list)
    # Culling schedule: (days_since_input, cull_pct), sorted ascending by day.
    culling: list[tuple[int, float]] = field(default_factory=list)


@dataclass
class TankConfig:
    location_id: str
    department: str
    stage: str               # "FW" or "SW"
    system_id: str
    tank_id: int
    volume_m3: float
    max_density_kg_m3: float
    max_feed_kg_day: float
    type: str                # "FW" or "OG"


@dataclass
class FacilityConfig:
    tanks: list[TankConfig]


@dataclass
class BatchWeekState:
    """Projected weekly biological state for one batch in one ISO week.

    `week_label` (ISO format like "2026-W20") is the canonical week
    identifier — chronological sort by string works correctly across
    year boundaries ("2026-W52" < "2027-W01").
    """
    batch_id: str
    week_label: str                # canonical week id, e.g. "2026-W20"
    week_start: datetime           # date of the week's start
    days_since_input: int
    week_from_input: int           # batch-local week count (mortality table lookup)
    count: float
    avg_weight_g: float
    biomass_kg: float
    feed_kg_day: float
    feed_kg_week: float
    sgr_pct_day: float
    fcr: float
    stage: str                     # "EGG" | "FW" | "SW"
    feed_type: str
    mortality_pct_weekly: float
    cull_event_pct: float = 0.0    # >0 if a culling event landed in this week
    cull_count_week: float = 0.0   # absolute fish removed by culls in this week
    cull_biomass_kg_week: float = 0.0  # biomass of fish removed by culls in this week
