"""Layer 2: harvest scheduler.

Walks the forecast horizon week by week, generating per-(batch, week)
harvest demand to keep facility biomass within its R24 ± band.

This layer is **tank-agnostic** — it picks BATCH and AMOUNT only.
Layer 3 (placement.py) maps each `HarvestDemand` to specific source
tanks and emits the actual Harvest events (honoring 6N round-robin
when 6N=purge, direct-from-tank when 6N=production).

Rules (DESIGN §6 + user F2 + H10):
- FIFO over batches by `batch.input_date` (oldest first).
- `Min Harvest Weight (g)`: skip batches whose per-week avg wt is below
  this threshold.
- `Max Harvest/Week` / `Min Harvest/Week`: strict count bounds per
  facility-week (per-week override possible via FacilityLimits).
- `Target Biomass deviation` (R24): symmetric ± buffer on facility
  biomass cap. Harvests fire when biomass exceeds the upper band.
- Operator-pinned HarvestPlan rows are treated as truth (added to the
  demand list first; FIFO then fills the rest).

Output is a list of `HarvestDemand` rows. Inventory is debited per
batch as harvests are scheduled, so each successive week's effective
biomass reflects prior weeks' harvests.

Limitations of this first cut:
- Feed-cap enforcement is biomass-by-proxy only (feed scales with
  biomass, so reducing biomass usually fixes feed). A dedicated feed
  pass can be added later if cases arise where feed is binding but
  biomass is not.
- Mortality on the harvested portion is not back-credited to the
  unharvested fraction (approximation acceptable over a 52-week
  forecast; impact ≲ 1%).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from .caps import (
    FacilityLimits,
    METRIC_BIOMASS,
    METRIC_FEED_DAY,
    METRIC_MAX_HARVEST,
    METRIC_MIN_HARVEST,
    apply_facility_buffer,
    resolve_facility_cap,
)
from .models import BatchInput, BatchWeekState, ControlParams
from .time_grid import forecast_week_labels, iso_week_label


@dataclass
class HarvestDemand:
    """Per-(batch, week) harvest amount. Tank-agnostic; layer 3 assigns tanks.

    `week_label` is the canonical ISO label ("2026-W20").
    """
    week_label: str
    batch_id: str
    count: float
    avg_wt_g: float
    biomass_kg: float
    source: str   # "pinned" | "fifo_biomass" | "min_count_fill"


def _batch_age_key(batch: BatchInput) -> date:
    """FIFO sort key: older input_date sorts first."""
    d = batch.input_date
    if d is None:
        return date.max
    return d.date() if hasattr(d, "date") else d


def _effective_inventory(
    batch_id: str,
    week_state: BatchWeekState,
    cumulative_harvested: float,
) -> dict:
    """Inventory of a batch in one week net of prior cumulative harvests.

    Scales biomass + feed proportionally to the surviving count.
    """
    pre_count = week_state.count
    if pre_count <= 0:
        return {"count": 0.0, "avg_wt_g": 0.0, "biomass_kg": 0.0, "feed_kg_day": 0.0}
    surviving_ratio = max(0.0, 1.0 - cumulative_harvested / pre_count)
    return {
        "count": pre_count * surviving_ratio,
        "avg_wt_g": week_state.avg_weight_g,
        "biomass_kg": week_state.biomass_kg * surviving_ratio,
        "feed_kg_day": week_state.feed_kg_day * surviving_ratio,
    }


def project_biomass_under_min_only(
    states_by_batch: dict[str, list[BatchWeekState]],
    batches_by_id: dict[str, BatchInput],
    control: ControlParams,
    facility_limits: "FacilityLimits",
) -> dict[str, float]:
    """Forward-simulate per-week facility biomass assuming the operational
    floor (FIFO oldest-first at min_hv each week).

    Returns dict[week_label -> projected_biomass_kg]. This is the natural
    biomass trajectory the facility will follow under the lightest legal
    harvest — used by the scheduler as a per-week target ceiling and by
    the canvas as an early-warning of carrying-capacity gaps.
    """
    forecast_start = (control.forecast_start.date()
                      if hasattr(control.forecast_start, "date")
                      else control.forecast_start)
    horizon_labels = forecast_week_labels(forecast_start, control.horizon_weeks)
    fifo_order = sorted(
        states_by_batch.keys(),
        key=lambda b: _batch_age_key(batches_by_id[b]) if b in batches_by_id else date.max,
    )
    cum_harvest: dict[str, float] = {}
    projected: dict[str, float] = {}

    for label in horizon_labels:
        effective: dict[str, dict] = {}
        for b in fifo_order:
            ws_list = states_by_batch.get(b, [])
            ws = next((s for s in ws_list if s.week_label == label), None)
            if ws is None:
                continue
            inv = _effective_inventory(b, ws, cum_harvest.get(b, 0.0))
            if inv["count"] > 0:
                effective[b] = inv

        projected[label] = sum(e["biomass_kg"] for e in effective.values())

        # Apply FIFO min-only harvest for next-week projection.
        min_hv = resolve_facility_cap(METRIC_MIN_HARVEST, label, facility_limits, control) or 0.0
        need = float(min_hv)
        for b in fifo_order:
            if need <= 0:
                break
            e = effective.get(b)
            if e is None or e["avg_wt_g"] < control.min_harvest_weight_g:
                continue
            if e["count"] <= 0:
                continue
            cnt = min(need, e["count"])
            if cnt <= 0:
                continue
            cum_harvest[b] = cum_harvest.get(b, 0.0) + cnt
            need -= cnt

    return projected


def schedule_harvests(
    states_by_batch: dict[str, list[BatchWeekState]],
    batches_by_id: dict[str, BatchInput],
    control: ControlParams,
    facility_limits: FacilityLimits,
    projected_biomass: Optional[dict[str, float]] = None,
) -> tuple[list[HarvestDemand], list[str]]:
    """Plan harvests so facility biomass stays within its R24 ± band.

    `states_by_batch` should contain BOTH in-flight (PR-hydrated, projected
    forward via biology.project_in_flight_batch) and incoming
    (biology.project_all_batches) batches, keyed by batch_id.
    """
    warnings: list[str] = []
    demands: list[HarvestDemand] = []
    forecast_start = (control.forecast_start.date()
                      if hasattr(control.forecast_start, "date")
                      else control.forecast_start)
    horizon_labels = forecast_week_labels(forecast_start, control.horizon_weeks)

    # Running per-batch harvested total, accumulated as demands are scheduled.
    cumulative_harvest: dict[str, float] = {}

    # --- FIFO ordering. ---
    fifo_order = sorted(
        states_by_batch.keys(),
        key=lambda b: _batch_age_key(batches_by_id[b]) if b in batches_by_id else date.max,
    )

    # --- Walk weeks in chronological label order. ---
    for label in horizon_labels:
        # Effective inventory per batch in this week label.
        effective: dict[str, dict] = {}
        for b in fifo_order:
            ws_list = states_by_batch.get(b, [])
            week_state = next((s for s in ws_list if s.week_label == label), None)
            if week_state is None:
                continue
            harvested = cumulative_harvest.get(b, 0.0)
            inv = _effective_inventory(b, week_state, harvested)
            if inv["count"] > 0:
                effective[b] = inv

        # Facility totals.
        fac_biomass = sum(e["biomass_kg"] for e in effective.values())

        # Resolve caps + buffers.
        bio_cap = resolve_facility_cap(METRIC_BIOMASS, label, facility_limits, control)
        max_hv = resolve_facility_cap(METRIC_MAX_HARVEST, label, facility_limits, control)
        min_hv = resolve_facility_cap(METRIC_MIN_HARVEST, label, facility_limits, control)

        # Pinned harvests already account for some weekly count.
        weekly_count = sum(d.count for d in demands if d.week_label == label)
        weekly_max = max_hv if max_hv else float("inf")
        weekly_min = min_hv if min_hv else 0.0

        # ----- Clever maintenance controller --------------------------
        # Policy:
        #   1) If facility is BELOW both biomass band AND feed band:
        #      harvest = min_hv (operational floor; FIFO oldest first).
        #   2) Else: compute the harvest count needed to keep NEXT WEEK's
        #      projected biomass and feed at the respective cap edges.
        #      Take the MAX of the two (whichever limit is more binding).
        #   3) Hard clip to [min_hv, max_hv].
        # Project next-week growth in biomass.
        fac_growth_kg = 0.0
        fac_feed_kg_day = 0.0
        for b in fifo_order:
            ws_list = states_by_batch.get(b, [])
            ws_state = next((s for s in ws_list if s.week_label == label), None)
            if ws_state is None or ws_state.count <= 0 or b not in effective:
                continue
            harvested = cumulative_harvest.get(b, 0.0)
            survival = max(0.0, 1.0 - harvested / ws_state.count) if ws_state.count > 0 else 0.0
            eff_bio = ws_state.biomass_kg * survival
            fac_growth_kg += eff_bio * (ws_state.sgr_pct_day / 100.0) * 7.0
            fac_feed_kg_day += ws_state.feed_kg_day * survival

        # Resolve facility feed cap.
        feed_cap = resolve_facility_cap(METRIC_FEED_DAY, label, facility_limits, control)
        dev = control.facility_biomass_deviation_pct or 0.0
        bio_band_lo = bio_cap * (1.0 - dev) if bio_cap else None
        feed_band_lo = feed_cap * (1.0 - dev) if feed_cap else None

        below_bio = bio_band_lo is None or fac_biomass < bio_band_lo
        below_feed = feed_band_lo is None or fac_feed_kg_day < feed_band_lo

        # Context-aware 3-state controller. Maximize harvest ONLY when
        # current biomass is genuinely climbing past cap (not when
        # the projection alone exceeds — that triggers premature
        # over-harvest and dumps biomass below cap on average). The
        # projection trigger fires only as a safety when current biomass
        # is ALSO within striking distance of cap (>=95%).
        bio_band_hi = bio_cap * (1.0 + dev) if bio_cap else None
        proj_now = (projected_biomass.get(label) if projected_biomass else None)
        overflow_pressure = (
            (bio_band_hi is not None and fac_biomass > bio_band_hi)
            or (
                bio_cap is not None
                and proj_now is not None
                and proj_now > bio_cap * (1.0 + dev)
                and fac_biomass >= bio_cap * 0.99
            )
        )

        oldest_mature_avg_wt = 0.0
        for b in fifo_order:
            e = effective.get(b)
            if e and e["avg_wt_g"] >= control.min_harvest_weight_g:
                oldest_mature_avg_wt = e["avg_wt_g"]
                break

        if overflow_pressure:
            # Biomass over cap (or will be): use full harvest capacity.
            target_count = weekly_max
        elif below_bio and below_feed:
            # Genuine headroom: operational floor only.
            target_count = weekly_min
        else:
            # In band: harvest at growth rate to maintain position.
            if oldest_mature_avg_wt > 0:
                target_count = fac_growth_kg * 1000.0 / oldest_mature_avg_wt
            else:
                target_count = weekly_min

        # HARD CLIP to operational bounds from Control.
        target_count = max(weekly_min, min(weekly_max, target_count))

        # ----- Pull FIFO from mature batches to hit target_count ------
        need = target_count - weekly_count
        for b in fifo_order:
            if need <= 0:
                break
            if weekly_count >= weekly_max:
                break
            if b not in effective:
                continue
            e = effective[b]
            if e["avg_wt_g"] < control.min_harvest_weight_g:
                continue
            if e["count"] <= 0:
                continue
            cnt = min(need, e["count"], weekly_max - weekly_count)
            if cnt <= 0:
                continue
            kg = cnt * e["avg_wt_g"] / 1000.0
            # Source tag classifies the cause this week.
            if overflow_pressure:
                src = "overflow_max"          # over cap → max harvest
            elif below_bio and below_feed:
                src = "min_count_fill"        # below band → floor
            else:
                src = "growth_maintain"       # in band → growth-rate
            demands.append(HarvestDemand(
                week_label=label, batch_id=b,
                count=cnt, avg_wt_g=e["avg_wt_g"], biomass_kg=kg,
                source=src,
            ))
            cumulative_harvest[b] = cumulative_harvest.get(b, 0.0) + cnt
            weekly_count += cnt
            need -= cnt
            e["count"] -= cnt
            e["biomass_kg"] -= kg
            fac_biomass -= kg

        if weekly_count < weekly_min - 1.0:
            warnings.append(
                f"{label}: min harvest count {weekly_min:,.0f} not met "
                f"(actual {weekly_count:,.0f}); insufficient mature inventory"
            )

            if need > 1.0:
                warnings.append(
                    f"{label}: min harvest count {weekly_min:,.0f} not met "
                    f"(actual {weekly_count:,.0f}); no batch above min harvest weight available"
                )

    return demands, warnings


def summarize_demands(demands: list[HarvestDemand]) -> dict:
    """Aggregate demands for diagnostic display."""
    total_count = sum(d.count for d in demands)
    total_biomass = sum(d.biomass_kg for d in demands)
    by_source: dict[str, dict] = {}
    by_week: dict[str, dict] = {}
    by_batch: dict[str, dict] = {}
    for d in demands:
        for grp, key in ((by_source, d.source), (by_week, d.week_label), (by_batch, d.batch_id)):
            e = grp.setdefault(key, {"count": 0.0, "biomass_kg": 0.0, "rows": 0})
            e["count"] += d.count
            e["biomass_kg"] += d.biomass_kg
            e["rows"] += 1
    return {
        "total_count": total_count,
        "total_biomass_kg": total_biomass,
        "rows": len(demands),
        "by_source": by_source,
        "by_week": by_week,
        "by_batch": by_batch,
    }
