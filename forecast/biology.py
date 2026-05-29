"""Biology projection: egg -> FW -> SW.

Simulation grain: **daily** internally. Aggregation: weekly on the
forecast grid anchored at `Control.forecast_start` (so each emitted
BatchWeekState corresponds to a 7-day block starting at
forecast_start + week_index * 7).

Stages (single-stream per batch through TranOG):

- EGG  : Input_Date <= today < TranSF_date.
        Weight = 0, biomass = 0; daily mortality applies to count.
- FW   : TranSF_date <= today < TranOG_Date.
        Daily growth with SGR_FW * FW_Correction; daily mortality;
        bottom-X% culls fire on the day they reach their DSI threshold.
- SW   : today >= TranOG_Date.
        Daily growth with SGR_SW * SGR_Correction; daily mortality.

Post-TranOG, biology continues as a single-stream count + avg-wt for
the batch as a whole. The per-(batch, tank) split is a placement
concern: at TranOG, biology emits a `SizeClassSplit` (median cut of
the post-cull distribution) for the placement layer to distribute
across N tanks.

Daily rates
-----------
- SGR is %/day from Tables (already daily); applied as
  w_next = w * (1 + sgr/100) each day.
- Mortality is %/week from Tables; converted to a daily survival
  factor as (1 - m_weekly/100)^(1/7) per day. Compounds exactly back
  to the weekly rate.
- Handling mortality fires once at each transfer event (TranSF, TranOG).
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from math import isnan
from statistics import NormalDist
from typing import Optional

from .models import (
    BatchInput,
    BatchWeekState,
    BiologyTables,
    CalibrationResidual,
    ControlParams,
    SizeClassSplit,
)
from .time_grid import (
    day_offset,
    iso_week_label,
    week_label as _week_label_for_index,
    week_range,
    week_start,
)


# Initial FW weight on transfer to startfeed (g).
FW_START_WEIGHT_G = 0.15

# Phi(0) = 1/sqrt(2*pi). Used for half-normal mean = sigma * Phi(0) / 0.5.
_PHI_AT_ZERO = 1.0 / math.sqrt(2.0 * math.pi)


def _as_date(d):
    if isinstance(d, datetime):
        return d.date()
    return d


# ---------- table helpers ----------

def _interp(x: float, xs: list[float], ys: list[Optional[float]]) -> float:
    """Linear interp; flat at the ends; skip None/NaN ys."""
    pairs = [(xs[i], ys[i]) for i in range(len(xs))
             if ys[i] is not None and not (isinstance(ys[i], float) and isnan(ys[i]))]
    if not pairs:
        return 0.0
    if x <= pairs[0][0]:
        return pairs[0][1]
    if x >= pairs[-1][0]:
        return pairs[-1][1]
    for i in range(1, len(pairs)):
        x0, y0 = pairs[i - 1]
        x1, y1 = pairs[i]
        if x0 <= x <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pairs[-1][1]


def _mortality_weekly_pct(tables: BiologyTables, week_from_input: int) -> float:
    """Step-function lookup: hold the last reference value forward."""
    wfi = max(0, week_from_input)
    if not tables.mortality_pct_weekly:
        return 0.0
    last = tables.mortality_pct_weekly[0]
    for w, m in zip(tables.mortality_week_from_input, tables.mortality_pct_weekly):
        if w <= wfi:
            last = m
        else:
            break
    return last


def _daily_survival_factor(weekly_pct: float) -> float:
    """Daily survival multiplier whose 7-day product equals (1 - weekly_pct/100)."""
    weekly_surv = max(0.0, 1.0 - weekly_pct / 100.0)
    return weekly_surv ** (1.0 / 7.0)


def _feed_type_for_size(tables: BiologyTables, avg_wt_g: float) -> str:
    """Smallest feed-type bracket whose Max Size >= avg weight."""
    for max_size, name in tables.feed_types:
        if avg_wt_g <= max_size:
            return name
    return tables.feed_types[-1][1] if tables.feed_types else ""


def _fcr_model_key(fcr_model_str: str) -> str:
    """'FCR_121_Quick' -> '1.21'."""
    m = re.search(r"(\d{2,3})", fcr_model_str or "")
    if not m:
        return "1.18"
    digits = m.group(1)
    if len(digits) == 3:
        return f"{digits[0]}.{digits[1:]}"
    return digits


# ---------- cull math ----------

def _apply_bottom_cull(
    count: float, avg_wt: float, cv_pct: float, cull_pct: float,
) -> tuple[float, float, float, float]:
    """Trim the bottom `cull_pct` of a normal distribution N(avg_wt, avg_wt*cv).

    Returns (new_count, new_avg_wt, culled_count, culled_biomass_kg).
    avg_wt rises because the smallest fish are removed; culled biomass
    uses the conditional mean of the removed (bottom) fraction.
    """
    if cull_pct <= 0 or count <= 0 or avg_wt <= 0:
        return count, avg_wt, 0.0, 0.0
    if cull_pct >= 1:
        return 0.0, avg_wt, count, count * avg_wt / 1000.0
    sigma = avg_wt * (cv_pct / 100.0)
    z = NormalDist().inv_cdf(cull_pct)
    # Conditional mean of N(0,1) below z, scaled to N(mu, sigma):
    # E[X | X < z*sigma + mu] = mu - sigma * phi(z) / Phi(z), Phi(z) = cull_pct.
    cond_mean = avg_wt - sigma * (math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)) / cull_pct
    if cond_mean < 0:
        cond_mean = 0.0
    new_avg = (avg_wt - cull_pct * cond_mean) / (1 - cull_pct)
    new_count = count * (1 - cull_pct)
    culled_count = count - new_count
    culled_biomass_kg = culled_count * cond_mean / 1000.0
    return new_count, new_avg, culled_count, culled_biomass_kg


def compute_size_class_split(
    batch_id: str,
    tran_og_date,
    post_cull_count: float,
    post_cull_avg_wt_g: float,
    cv_pct: float,
) -> SizeClassSplit:
    """Median split of post-TranOG distribution into big/small classes.

    Half-normal means about the median:
      upper_mean = mu + sigma * 2*phi(0) = mu + sigma * sqrt(2/pi)
      lower_mean = mu - sigma * sqrt(2/pi)
    """
    sigma = post_cull_avg_wt_g * (cv_pct / 100.0)
    delta = sigma * (2.0 * _PHI_AT_ZERO)  # sigma * sqrt(2/pi)
    half = post_cull_count / 2.0
    return SizeClassSplit(
        batch_id=batch_id,
        tran_og_date=tran_og_date,
        post_cull_count=post_cull_count,
        post_cull_avg_wt_g=post_cull_avg_wt_g,
        post_cull_cv_pct=cv_pct,
        big_class_count=half,
        big_class_avg_wt_g=post_cull_avg_wt_g + delta,
        small_class_count=half,
        small_class_avg_wt_g=max(0.0, post_cull_avg_wt_g - delta),
    )


# ---------- core daily projection ----------

def project_batch(
    batch: BatchInput,
    tables: BiologyTables,
    control: ControlParams,
    warnings: Optional[list[str]] = None,
) -> tuple[list[BatchWeekState], Optional[CalibrationResidual], Optional[SizeClassSplit]]:
    """Forward-simulate one batch day by day from Input_Date through forecast end.

    Returns:
      - Per-forecast-week aggregated rows that fall inside the horizon.
      - Optional FW calibration residual recorded at TranOG_Date.
      - Optional SizeClassSplit emitted at TranOG (None if the batch
        does not cross TranOG within the horizon).
    """
    if batch.input_date is None:
        return [], None, None

    forecast_start = _as_date(control.forecast_start)
    forecast_end = forecast_start + timedelta(weeks=control.horizon_weeks)
    input_date = _as_date(batch.input_date)
    tran_sf_date = _as_date(batch.tran_sf_date) if batch.tran_sf_date else None
    tran_og_date = _as_date(batch.tran_og_date) if batch.tran_og_date else None

    if input_date >= forecast_end:
        return [], None, None

    fcr_key = _fcr_model_key(batch.fcr_model)
    fcr_curve = tables.fcr_by_model.get(fcr_key, [])

    cull_thresh_dsi = [(dsi, pct) for dsi, pct in tables.culling]
    handling_frac = control.handling_mortality_pct / 100.0

    # State.
    cur_count = float(batch.input_count)
    cur_weight = 0.0
    stage = "EGG"
    transferred_to_fw = False
    crossed_tran_og = False
    residual: Optional[CalibrationResidual] = None
    split: Optional[SizeClassSplit] = None
    fired_culls: set[int] = set()

    # Daily series: one entry per simulated day (the day's *closing* state
    # after applying all that day's events + mortality + growth).
    # Tuple: (date, dsi, stage, count, avg_wt, sgr_eff_pct_day, fcr,
    #         m_weekly_pct, cull_pct_today, feed_kg_day, feed_type,
    #         biomass_kg, cull_count_today, cull_biomass_today)
    days: list[tuple] = []

    cur_date = input_date
    while cur_date < forecast_end:
        dsi = (cur_date - input_date).days

        # Per-day cull accumulators (count + biomass of fish removed today
        # by handling-mortality, scheduled bottom culls, or TranOG
        # reconciliation cull). Reset at the top of every day.
        cull_count_today = 0.0
        cull_biomass_today = 0.0
        cull_pct_today = 0.0

        # ----- Stage transitions at this day -----
        # Egg -> FW. Handling mortality removes a fixed fraction; the
        # culled biomass uses cur_weight (FW_START_WEIGHT_G is set after).
        if not transferred_to_fw and tran_sf_date and cur_date >= tran_sf_date:
            _pre = cur_count
            cur_count *= (1.0 - handling_frac)
            _hm = _pre - cur_count
            if _hm > 0:
                cull_count_today += _hm
                cull_biomass_today += _hm * cur_weight / 1000.0
            cur_weight = FW_START_WEIGHT_G
            stage = "FW"
            transferred_to_fw = True

        # FW -> SW (TranOG): handling mort, residual, reconciliation cull,
        # then emit SizeClassSplit metadata for placement.
        if not crossed_tran_og and tran_og_date and cur_date >= tran_og_date:
            _pre = cur_count
            cur_count *= (1.0 - handling_frac)
            _hm = _pre - cur_count
            if _hm > 0:
                cull_count_today += _hm
                cull_biomass_today += _hm * cur_weight / 1000.0
            if batch.tran_og_avg_wt_g and batch.tran_og_avg_wt_g > 0:
                residual = CalibrationResidual(
                    batch_id=batch.batch_id,
                    tran_og_date=batch.tran_og_date,
                    target_avg_wt_g=batch.tran_og_avg_wt_g,
                    current_fw_correction=batch.fw_correction,
                    projected_pre_cull_avg_wt_g=cur_weight,
                    residual_pct=(cur_weight - batch.tran_og_avg_wt_g) / batch.tran_og_avg_wt_g * 100.0,
                )
            target_count = float(batch.tran_og_count or 0)
            if target_count > 0:
                if cur_count <= target_count:
                    if warnings is not None:
                        warnings.append(
                            f"{batch.batch_id}: projected count at TranOG ({cur_count:.0f}) is "
                            f"below TranOG_Count target ({target_count:.0f}); no final cull applied"
                        )
                else:
                    final_cull = 1.0 - target_count / cur_count
                    cur_count, cur_weight, _c_n, _c_b = _apply_bottom_cull(
                        cur_count, cur_weight, batch.tran_og_cv, final_cull,
                    )
                    cull_count_today += _c_n
                    cull_biomass_today += _c_b
            split = compute_size_class_split(
                batch_id=batch.batch_id,
                tran_og_date=batch.tran_og_date,
                post_cull_count=cur_count,
                post_cull_avg_wt_g=cur_weight,
                cv_pct=batch.tran_og_cv,
            )
            stage = "SW"
            crossed_tran_og = True

        # ----- Scheduled bottom culls (FW only). Fire once per DSI threshold. -----
        for thresh_dsi, pct in cull_thresh_dsi:
            if stage == "FW" and dsi >= thresh_dsi and thresh_dsi not in fired_culls:
                cur_count, cur_weight, _c_n, _c_b = _apply_bottom_cull(
                    cur_count, cur_weight, batch.tran_og_cv, pct / 100.0,
                )
                fired_culls.add(thresh_dsi)
                cull_pct_today += pct
                cull_count_today += _c_n
                cull_biomass_today += _c_b

        # ----- Daily mortality (geometric, compounds to weekly) -----
        wfi = dsi // 7
        m_weekly = _mortality_weekly_pct(tables, wfi)
        cur_count *= _daily_survival_factor(m_weekly)

        # ----- Daily growth -----
        if stage == "EGG":
            sgr_eff = 0.0
            fcr = 0.0
        elif stage == "FW":
            sgr_base = _interp(cur_weight, tables.sgr_size_g, tables.sgr_fw_pct_day)
            sgr_eff = sgr_base * batch.fw_correction
            fcr = _interp(cur_weight, tables.fcr_size_g, fcr_curve)
            cur_weight = cur_weight * (1.0 + sgr_eff / 100.0)
        else:  # SW
            sgr_base = _interp(cur_weight, tables.sgr_size_g, tables.sgr_sw_pct_day)
            sgr_eff = sgr_base * batch.sgr_correction
            fcr = _interp(cur_weight, tables.fcr_size_g, fcr_curve)
            cur_weight = cur_weight * (1.0 + sgr_eff / 100.0)

        biomass_kg = cur_count * cur_weight / 1000.0 if stage != "EGG" else 0.0
        feed_kg_day = biomass_kg * (sgr_eff / 100.0) * fcr if stage != "EGG" else 0.0
        feed_type = _feed_type_for_size(tables, cur_weight) if stage != "EGG" else ""

        days.append((
            cur_date, dsi, stage, cur_count, cur_weight, sgr_eff, fcr,
            m_weekly, cull_pct_today, feed_kg_day, feed_type, biomass_kg,
            cull_count_today, cull_biomass_today,
        ))

        cur_date = cur_date + timedelta(days=1)

    # ----- Aggregate daily series to forecast weeks -----
    out: list[BatchWeekState] = []
    for w in range(control.horizon_weeks):
        ws, we = week_range(w, forecast_start)
        days_in_w = [d for d in days if ws <= d[0] < we]
        if not days_in_w:
            continue
        last = days_in_w[-1]
        sgr_end = last[5]
        fcr_end = last[6]
        m_weekly_end = last[7]
        stage_end = last[2]
        feed_type_end = last[10]

        mean_count = sum(d[3] for d in days_in_w) / len(days_in_w)
        mean_wt = sum(d[4] for d in days_in_w) / len(days_in_w)
        biomass_mean = mean_count * mean_wt / 1000.0 if stage_end != "EGG" else 0.0
        feed_kg_week = sum(d[9] for d in days_in_w)
        feed_kg_day_peak = max((d[9] for d in days_in_w), default=0.0)
        cull_pct_week = sum(d[8] for d in days_in_w)
        cull_count_week = sum(d[12] for d in days_in_w)
        cull_biomass_kg_week = sum(d[13] for d in days_in_w)
        wfi_end = last[1] // 7

        out.append(BatchWeekState(
            batch_id=batch.batch_id,
            week_label=iso_week_label(ws),
            week_start=datetime.combine(ws, datetime.min.time()),
            days_since_input=last[1],
            week_from_input=wfi_end,
            count=mean_count,
            avg_weight_g=mean_wt,
            biomass_kg=biomass_mean,
            feed_kg_day=feed_kg_day_peak,
            feed_kg_week=feed_kg_week,
            sgr_pct_day=sgr_end,
            fcr=fcr_end,
            stage=stage_end,
            feed_type=feed_type_end,
            mortality_pct_weekly=m_weekly_end,
            cull_event_pct=cull_pct_week,
            cull_count_week=cull_count_week,
            cull_biomass_kg_week=cull_biomass_kg_week,
        ))

    return out, residual, split


# ---------- FW back-solver (daily-step, mirrors project_batch) ----------

def _simulate_fw_avg_weight_at_tran_og(
    batch: BatchInput,
    tables: BiologyTables,
    fw_correction: float,
) -> Optional[float]:
    """Fast FW-only daily sim from TranSF -> TranOG (exclusive).

    Returns the pre-cull avg weight at TranOG_Date that would be
    produced by project_batch under the given fw_correction. Walks the
    same daily grid so the residual matches the full sim exactly.
    """
    if not batch.tran_sf_date or not batch.tran_og_date or not batch.input_date:
        return None
    input_date = _as_date(batch.input_date)
    tran_sf_date = _as_date(batch.tran_sf_date)
    tran_og_date = _as_date(batch.tran_og_date)

    cur_weight = FW_START_WEIGHT_G
    cur_date = tran_sf_date
    fired: set[int] = set()
    while cur_date < tran_og_date:
        dsi = (cur_date - input_date).days
        for thresh, pct in tables.culling:
            if dsi >= thresh and thresh not in fired:
                _, cur_weight, _, _ = _apply_bottom_cull(1.0, cur_weight, batch.tran_og_cv, pct / 100.0)
                fired.add(thresh)
        sgr_base = _interp(cur_weight, tables.sgr_size_g, tables.sgr_fw_pct_day)
        sgr_eff = sgr_base * fw_correction
        cur_weight = cur_weight * (1.0 + sgr_eff / 100.0)
        cur_date = cur_date + timedelta(days=1)
    return cur_weight


def solve_fw_correction(
    batch: BatchInput,
    tables: BiologyTables,
    lo: float = 0.10,
    hi: float = 3.00,
    tol_rel: float = 1e-4,
    max_iter: int = 60,
) -> Optional[float]:
    """Bisection: find FW_Correction landing pre-cull avg wt at TranOG on TranOG_AvgWt."""
    target = batch.tran_og_avg_wt_g
    if not target or target <= 0:
        return None
    wt_lo = _simulate_fw_avg_weight_at_tran_og(batch, tables, lo)
    wt_hi = _simulate_fw_avg_weight_at_tran_og(batch, tables, hi)
    if wt_lo is None or wt_hi is None:
        return None
    while wt_hi < target and hi < 10.0:
        hi *= 1.5
        wt_hi = _simulate_fw_avg_weight_at_tran_og(batch, tables, hi)
        if wt_hi is None:
            return None
    while wt_lo > target and lo > 0.001:
        lo *= 0.5
        wt_lo = _simulate_fw_avg_weight_at_tran_og(batch, tables, lo)
        if wt_lo is None:
            return None
    if wt_lo > target or wt_hi < target:
        return None
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        wt = _simulate_fw_avg_weight_at_tran_og(batch, tables, mid)
        if wt is None:
            return None
        if abs(wt - target) / target < tol_rel:
            return mid
        if wt < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# ---------- per-tank one-day biology step (used by placement walker) ----------

def advance_tank_one_day(tank, batch: BatchInput, tables: BiologyTables, today) -> None:
    """Apply one day of continuous biology (mortality + growth) to a tank.

    - Mortality: geometric daily survival factor derived from the batch's
      current week-from-input row in the mortality table.
    - Growth: daily SGR from the SGR table, multiplied by the batch's
      `fw_correction` (FW stage) or `sgr_correction` (SW stage).
      No growth during STARVE stage; EGG stage skipped.

    Tank state is mutated in place; no event row is emitted (continuous
    biology never produces logged events per the continuity invariants).
    """
    if tank.is_empty or batch is None or batch.input_date is None:
        return
    today = _as_date(today)
    input_date = _as_date(batch.input_date)
    dsi = (today - input_date).days
    wfi = max(0, dsi // 7)

    m_weekly = _mortality_weekly_pct(tables, wfi)
    tank.apply_daily_mortality(_daily_survival_factor(m_weekly))

    stage = tank.stage
    if stage in ("", "EGG", "STARVE"):
        return  # no growth for empty / pre-FW / starving tanks

    fcr_key = _fcr_model_key(batch.fcr_model)
    if stage == "FW":
        sgr_base = _interp(tank.avg_wt_g, tables.sgr_size_g, tables.sgr_fw_pct_day)
        sgr_eff = sgr_base * batch.fw_correction
    else:  # SW
        sgr_base = _interp(tank.avg_wt_g, tables.sgr_size_g, tables.sgr_sw_pct_day)
        sgr_eff = sgr_base * batch.sgr_correction
    tank.apply_daily_growth(sgr_eff)


# ---------- in-flight forward projection (PR-hydrated batches) ----------

def project_in_flight_batch(
    batch: BatchInput,
    tables: BiologyTables,
    control: ControlParams,
    initial_count: float,
    initial_avg_wt_g: float,
    initial_cv_pct: float,
) -> list[BatchWeekState]:
    """Project an OG batch forward from forecast_start.

    Starting state (count, avg_wt, cv) comes from ProductionReport
    hydration. The batch is assumed to be in SW phase at forecast_start
    (TranOG already crossed). Daily mortality + growth applied; no
    scheduled bottom culls (those fired in FW), no further handling
    mortality (no transfer events from biology — placement decides those).

    Days-since-input is computed from batch.input_date so the mortality
    table lookup tracks the batch's actual age.
    """
    if not batch.input_date or initial_count <= 0:
        return []

    forecast_start = _as_date(control.forecast_start)
    forecast_end = forecast_start + timedelta(weeks=control.horizon_weeks)
    input_date = _as_date(batch.input_date)

    fcr_key = _fcr_model_key(batch.fcr_model)
    fcr_curve = tables.fcr_by_model.get(fcr_key, [])

    cur_count = float(initial_count)
    cur_weight = float(initial_avg_wt_g)

    days: list[tuple] = []
    cur_date = forecast_start
    while cur_date < forecast_end:
        dsi = (cur_date - input_date).days
        wfi = max(0, dsi // 7)
        m_weekly = _mortality_weekly_pct(tables, wfi)
        cur_count *= _daily_survival_factor(m_weekly)

        sgr_base = _interp(cur_weight, tables.sgr_size_g, tables.sgr_sw_pct_day)
        sgr_eff = sgr_base * batch.sgr_correction
        fcr = _interp(cur_weight, tables.fcr_size_g, fcr_curve)
        cur_weight = cur_weight * (1.0 + sgr_eff / 100.0)

        biomass_kg = cur_count * cur_weight / 1000.0
        feed_kg_day = biomass_kg * (sgr_eff / 100.0) * fcr
        feed_type = _feed_type_for_size(tables, cur_weight)

        days.append((
            cur_date, dsi, "SW", cur_count, cur_weight, sgr_eff, fcr,
            m_weekly, 0.0, feed_kg_day, feed_type, biomass_kg,
            0.0, 0.0,
        ))
        cur_date = cur_date + timedelta(days=1)

    out: list[BatchWeekState] = []
    for w in range(control.horizon_weeks):
        ws, we = week_range(w, forecast_start)
        days_in_w = [d for d in days if ws <= d[0] < we]
        if not days_in_w:
            continue
        last = days_in_w[-1]
        mean_count = sum(d[3] for d in days_in_w) / len(days_in_w)
        mean_wt = sum(d[4] for d in days_in_w) / len(days_in_w)
        biomass_mean = mean_count * mean_wt / 1000.0
        feed_kg_week = sum(d[9] for d in days_in_w)
        feed_kg_day_peak = max((d[9] for d in days_in_w), default=0.0)
        wfi_end = last[1] // 7
        out.append(BatchWeekState(
            batch_id=batch.batch_id,
            week_label=iso_week_label(ws),
            week_start=datetime.combine(ws, datetime.min.time()),
            days_since_input=last[1],
            week_from_input=wfi_end,
            count=mean_count,
            avg_weight_g=mean_wt,
            biomass_kg=biomass_mean,
            feed_kg_day=feed_kg_day_peak,
            feed_kg_week=feed_kg_week,
            sgr_pct_day=last[5],
            fcr=last[6],
            stage="SW",
            feed_type=last[10],
            mortality_pct_weekly=last[7],
            cull_event_pct=0.0,
        ))
    return out


# ---------- orchestrator ----------

def project_all_batches(
    batches: list[BatchInput],
    tables: BiologyTables,
    control: ControlParams,
) -> tuple[list[BatchWeekState], list[CalibrationResidual], list[SizeClassSplit], list[str]]:
    """Project every eligible batch.

    Skips batches whose TranOG_Date is at/before forecast_start — those
    are sourced from ProductionReport in a later pipeline step.
    """
    states: list[BatchWeekState] = []
    residuals: list[CalibrationResidual] = []
    splits: list[SizeClassSplit] = []
    warnings: list[str] = []
    forecast_start = _as_date(control.forecast_start)
    for b in batches:
        if not b.input_date:
            continue
        if b.input_count <= 0:
            continue
        if not b.tran_og_date or _as_date(b.tran_og_date) <= forecast_start:
            continue
        rows, resid, split = project_batch(b, tables, control, warnings=warnings)
        if not rows:
            continue
        states.extend(rows)
        if resid is not None:
            resid.suggested_fw_correction = solve_fw_correction(b, tables)
            residuals.append(resid)
        if split is not None:
            splits.append(split)
    return states, residuals, splits, warnings
