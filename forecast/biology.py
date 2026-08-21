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
    og_entry_week_start as _og_entry_date,
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
    """Linear interp; flat at the ends; skip None/NaN ys.

    zip() rather than range(len(xs)): a ys shorter than xs used to raise a bare
    IndexError from inside the comprehension. That is how a MISSING FCR model
    surfaced — `fcr_by_model.get(key, [])` returns [], and the caller then
    interpolated an empty curve against a 62-point size axis (2026-08-20).
    Callers that need a missing curve to be an ERROR must say so themselves;
    see _require_fcr_models.
    """
    pairs = [(x_i, y_i) for x_i, y_i in zip(xs, ys)
             if y_i is not None and not (isinstance(y_i, float) and isnan(y_i))]
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


def _require_fcr_models(batches, tables: BiologyTables) -> None:
    """Fail fast, by name, when a batch asks for an FCR model the tables lack.

    Without this the missing curve is an empty list, `_interp` silently
    flat-lines it (or, before the zip fix, raised a bare IndexError from a
    comprehension), and the run either dies pointing at the wrong place or
    quietly feeds every affected batch on a curve that does not exist.
    Reproduced 2026-08-20: 37 of 45 batches were assigned FCR_115_Quick while
    the config carried only 1.21 / 1.18 / 1.16.
    """
    available = set(tables.fcr_by_model or {})
    missing: dict[str, list[str]] = {}
    for b in batches or []:
        model = getattr(b, "fcr_model", None)
        if not model:
            continue
        key = _fcr_model_key(model)
        if key not in available:
            missing.setdefault(key, []).append(getattr(b, "batch_id", "?"))
    if missing:
        detail = "; ".join(
            f"{key!r} wanted by {len(ids)} batch(es) ({', '.join(sorted(ids)[:5])}"
            f"{', ...' if len(ids) > 5 else ''})"
            for key, ids in sorted(missing.items())
        )
        raise ValueError(
            f"BiologyTables has no FCR curve for: {detail}. "
            f"Available models: {sorted(available) or '(none)'}. "
            f"Add the missing FCR_<model> column to the biology tables, or "
            f"correct the batch's FCR_Model."
        )


def og_sgr_factor(tables: BiologyTables, week_label: Optional[str]) -> float:
    """The operator's per-week OG growth factor for `week_label` (1.0 = none).

    Separate from the curve and from the batch correction on purpose: those two
    are the MODEL, this is the operator saying "we know we only get 90% of it
    these weeks". Unknown/absent week is 1.0, so an unset config changes
    nothing.
    """
    if not week_label or not tables.og_sgr_by_week:
        return 1.0
    try:
        return float(tables.og_sgr_by_week.get(week_label, 1.0))
    except (TypeError, ValueError):
        return 1.0


def sgr_pct_per_day(
    avg_wt_g: float, stage: str, batch: Optional[BatchInput],
    tables: BiologyTables, week_label: Optional[str] = None,
) -> float:
    """Effective SGR (%/day) at a weight: the size-interpolated FW or SW growth
    curve scaled by the batch's stage correction (`fw_correction` in FW, else
    `sgr_correction`; 1.0 when no batch), and in SEAWATER by the operator's
    per-week OG factor for `week_label`.

    Single source for the growth rate used by every daily projector,
    `advance_tank_one_day`, `realized_feed_kg_day`, and the placement
    growth/feed helpers — so the formula can't drift between copies. (The
    FW-correction SOLVER in `project_batch_fw_residual` uses a CANDIDATE
    correction, not the batch's, so it deliberately does not call this.)

    The three multipliers LAYER — curve x batch correction x week factor — and
    the week factor is SW-only: it is an OG-tank input, and the freshwater
    phase has its own `fw_correction`. `week_label=None` means "no week in
    context" and yields the pre-existing behaviour exactly, so a caller that
    genuinely has no week (a size-curve probe) is unchanged rather than
    silently taking someone else's week.
    """
    if stage == "FW":
        base = _interp(avg_wt_g, tables.sgr_size_g, tables.sgr_fw_pct_day)
        corr = batch.fw_correction if batch else 1.0
        return base * corr
    base = _interp(avg_wt_g, tables.sgr_size_g, tables.sgr_sw_pct_day)
    corr = batch.sgr_correction if batch else 1.0
    return base * corr * og_sgr_factor(tables, week_label)


def realized_feed_kg_day(
    avg_wt_g: float, biomass_kg: float, batch: Optional[BatchInput],
    tables: BiologyTables, week_label: Optional[str] = None,
) -> float:
    """Daily feed for a REALIZED tank state: biomass × SGR(weight)/100 × FCR.

    Mirrors the projection's feed model (`biomass × sgr_eff/100 × fcr`) but is
    evaluated at the tank's ACTUAL avg weight + the batch's SW SGR correction /
    FCR model — so per-system feed totals reflect what fish in the tanks really
    eat (no un-harvested 100kg projection fish; see the feed-projection fix).
    Returns 0 for empty / sub-feeding states.
    """
    if biomass_kg <= 0 or avg_wt_g <= 0:
        return 0.0
    # Feed follows the SAME rate growth does, week factor included: this is
    # biomass x SGR/100 x FCR, so an operator week at 90% feeds 90% and FCR is
    # unchanged (operator decision 2026-08-19 — "they ate less, so they grew
    # less", rather than normal ration against impaired growth).
    sgr_eff = sgr_pct_per_day(avg_wt_g, "SW", batch, tables, week_label)
    fcr_curve = tables.fcr_by_model.get(
        _fcr_model_key(batch.fcr_model) if batch else "", [])
    fcr = _interp(avg_wt_g, tables.fcr_size_g, fcr_curve) if fcr_curve else 1.2
    return biomass_kg * (sgr_eff / 100.0) * fcr


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


def apply_grade_efficiency(mean_g: float, upper_g: float, lower_g: float,
                           efficiency: float) -> tuple[float, float]:
    """Shrink a perfect size split toward the tank mean — an imperfect grader.

    A real grader does NOT cut the population cleanly at the threshold. Fish
    near the cut line go both ways, so the two resulting populations OVERLAP:
    the "big" side keeps some small fish and the "small" side keeps some big
    ones. The conditional means of a clean truncated-normal split are therefore
    further apart than reality (operator, 2026-08-21).

    First-moment model, matching the VBA verbatim
    (ForecastSim_V2.bas:705-709, ForecastEngine_V2.bas:3074-3075):

        big   = mean + (big_perfect   - mean) * efficiency
        small = mean + (small_perfect - mean) * efficiency

    efficiency = 1.0 is a perfect grader (no change); 0.5 halves the
    separation; 0.0 would collapse both to the mean.

    MASS IS CONSERVED for any efficiency, because the split FRACTIONS are
    untouched and the shrink is affine about the mean:
        p*(m + e*(U-m)) + (1-p)*(m + e*(L-m))
      = m + e*(p*U + (1-p)*L - m) = m,   since p*U + (1-p)*L = m.
    So this cannot introduce or destroy biomass; it only moves weight between
    the two legs.

    Values outside (0, 1) mean "perfect grader, do not shrink". That includes
    0.0, matching the VBA's `If gradeEfficiency > 0 Then` guard, where 0 reads
    as "feature off" rather than "collapse everything to the mean".
    """
    if not (0.0 < efficiency < 1.0):
        return upper_g, lower_g
    return (mean_g + (upper_g - mean_g) * efficiency,
            mean_g + (lower_g - mean_g) * efficiency)


def upper_truncated_split(
    avg_wt_g: float, cv_pct: float, threshold_g: float,
    grade_efficiency: float = 1.0,
) -> tuple[float, float]:
    """Split a normal distribution at `threshold_g`, return conditional means.

    For N(mu=avg_wt_g, sigma=avg_wt_g*cv_pct/100), returns
    (E[X | X >= threshold], E[X | X < threshold]) via the Mills ratio:

      E[X | X >= t] = mu + sigma * phi(z) / (1 - Phi(z))
      E[X | X <  t] = mu - sigma * phi(z) / Phi(z)

    where z = (t - mu) / sigma. Used by the graded-harvest path (DESIGN
    §5a) to size the >= harvest-weight portion (pickup) and < harvest-
    weight portion (retention) when a tank's average is below threshold
    but its upper tail crosses it.
    """
    if avg_wt_g <= 0 or cv_pct <= 0:
        return (avg_wt_g, avg_wt_g)
    sigma = avg_wt_g * cv_pct / 100.0
    z = (threshold_g - avg_wt_g) / sigma
    Phi_z = NormalDist().cdf(z)
    if Phi_z <= 1e-9 or Phi_z >= 1 - 1e-9:
        # Almost all fish on one side — collapse to avg_wt to avoid blow-up.
        return (avg_wt_g, avg_wt_g)
    phi_z = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
    upper_mean = avg_wt_g + sigma * phi_z / (1 - Phi_z)
    lower_mean = avg_wt_g - sigma * phi_z / Phi_z
    upper_mean, lower_mean = apply_grade_efficiency(
        avg_wt_g, upper_mean, lower_mean, grade_efficiency)
    return (max(0.0, upper_mean), max(0.0, lower_mean))


def count_split_means(
    avg_wt_g: float, cv_pct: float, upper_fraction: float,
    grade_efficiency: float = 1.0,
) -> tuple[float, float]:
    """Conditional means when the TOP `upper_fraction` of a tank is moved.

    Same distribution as `upper_truncated_split`, addressed by COUNT instead of
    by weight: given N(mu, sigma) and a decision to take the heaviest fraction
    p, returns (E[X | top p], E[X | bottom 1-p]).

    Why this exists. The graded path picks its pickup COUNT first — capped to
    exactly the floor shortfall so it peels the least it can — and then needs
    the two means. Taking them from `upper_truncated_split` at the harvest
    WEIGHT answers a different question: it is the split of the fish above
    3.5 kg, which is only the same partition when the cap happens not to bite.
    When it does bite, the pickup is smaller than the above-threshold group, so
    the heavy fish left behind sit in the retention leg while it is still
    priced at the full lower-tail mean — and the tank loses mass that never
    went anywhere. Measured on the 8.13 PR: 24 graded splits, swinging -6,493
    to +1,546 kg, net +5,150 kg, and the loss tail was 3 of the 4 worst
    biomass-drift rows in the whole run.

    Because p is the actual moved fraction, this conserves by construction:

        p * upper + (1 - p) * lower
          = p*mu + sigma*phi + (1-p)*mu - sigma*phi
          = mu

    Identical to `upper_truncated_split` whenever the pickup is NOT capped
    (z = inv_cdf(1-p) is the exact inverse of p = 1 - Phi(z)), so an uncapped
    graded split is unchanged.
    """
    if avg_wt_g <= 0 or cv_pct <= 0:
        return (avg_wt_g, avg_wt_g)
    if upper_fraction <= 1e-9 or upper_fraction >= 1 - 1e-9:
        # Everything on one side: there is no second group to hold a mean.
        return (avg_wt_g, avg_wt_g)
    sigma = avg_wt_g * cv_pct / 100.0
    nd = NormalDist()
    z = nd.inv_cdf(1.0 - upper_fraction)
    phi_z = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
    upper_mean = avg_wt_g + sigma * phi_z / upper_fraction
    lower_mean = avg_wt_g - sigma * phi_z / (1.0 - upper_fraction)
    # Imperfect grader: shrink the separation toward the mean. Conserves mass
    # (see apply_grade_efficiency) so this path stays "conserving by
    # construction" exactly as the docstring above promises.
    upper_mean, lower_mean = apply_grade_efficiency(
        avg_wt_g, upper_mean, lower_mean, grade_efficiency)
    return (max(0.0, upper_mean), max(0.0, lower_mean))


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
    # OG transfer lands on the first forecast-week boundary on/after
    # TranOG_Date; the reconciliation cull still fires on the date itself
    # (in the containing week). See _og_entry_date / BUG #1.
    og_entry_date = (_og_entry_date(tran_og_date, forecast_start)
                     if tran_og_date else None)

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
    crossed_tran_og = False     # reconciliation cull fired (containing week)
    og_transferred = False      # flipped to SW / entered OG (snapped week)
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

        # TranOG reconciliation cull — fires in the week CONTAINING
        # TranOG_Date (VBA `wE >= TranOGDate`). Handling mort, residual,
        # final cull to TranOG_Count, then SizeClassSplit metadata. The
        # fish stay in the FW pool here; the OG transfer (stage flip) is
        # deferred to og_entry_date below so it lands in the right week.
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
            crossed_tran_og = True

        # FW -> SW (OG transfer): flips to SW on the first forecast-week
        # boundary on/after TranOG_Date (VBA `wS >= TranOGDate`). Until
        # then the (already-culled) batch grows on the FW curve in the
        # FW pool, exactly as the legacy tool models the transit week. The
        # SizeClassSplit is emitted HERE (not at the cull) so its count +
        # weight reflect the state at OG entry — placement seeds the OG
        # tanks from it, so it must match the biology's first SW week or
        # the continuity audit sees a biomass drift the size of the
        # transit week's growth/mortality.
        if not og_transferred and og_entry_date and cur_date >= og_entry_date:
            split = compute_size_class_split(
                batch_id=batch.batch_id,
                tran_og_date=batch.tran_og_date,
                post_cull_count=cur_count,
                post_cull_avg_wt_g=cur_weight,
                cv_pct=batch.tran_og_cv,
            )
            stage = "SW"
            og_transferred = True

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
        _pre_mort = cur_count
        cur_count *= _daily_survival_factor(m_weekly)
        mort_count_today = _pre_mort - cur_count

        # ----- Daily growth -----
        if stage == "EGG":
            sgr_eff = 0.0
            fcr = 0.0
        elif stage == "FW":
            sgr_eff = sgr_pct_per_day(cur_weight, "FW", batch, tables,
                                      iso_week_label(cur_date))
            fcr = _interp(cur_weight, tables.fcr_size_g, fcr_curve)
            cur_weight = cur_weight * (1.0 + sgr_eff / 100.0)
        else:  # SW
            sgr_eff = sgr_pct_per_day(cur_weight, "SW", batch, tables,
                                      iso_week_label(cur_date))
            fcr = _interp(cur_weight, tables.fcr_size_g, fcr_curve)
            cur_weight = cur_weight * (1.0 + sgr_eff / 100.0)

        biomass_kg = cur_count * cur_weight / 1000.0 if stage != "EGG" else 0.0
        feed_kg_day = biomass_kg * (sgr_eff / 100.0) * fcr if stage != "EGG" else 0.0
        feed_type = _feed_type_for_size(tables, cur_weight) if stage != "EGG" else ""

        days.append((
            cur_date, dsi, stage, cur_count, cur_weight, sgr_eff, fcr,
            m_weekly, cull_pct_today, feed_kg_day, feed_type, biomass_kg,
            cull_count_today, cull_biomass_today, mort_count_today,
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
            open_count=days_in_w[0][3] + days_in_w[0][12] + days_in_w[0][14],
            open_avg_weight_g=days_in_w[0][4],
            open_biomass_kg=(days_in_w[0][3] + days_in_w[0][12]
                             + days_in_w[0][14]) * days_in_w[0][4] / 1000.0,
            close_count=last[3],
            close_avg_weight_g=last[4],
            close_biomass_kg=last[11],
            mort_count_week=sum(d[14] for d in days_in_w),
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


def _bisect_fw_correction(
    sim_fn, target: float,
    lo: float = 0.10, hi: float = 3.00, tol_rel: float = 1e-4, max_iter: int = 60,
) -> Optional[float]:
    """Bisect FW_Correction so `sim_fn(correction)` (the simulated pre-cull avg wt
    at TranOG) lands on `target`. Shared by the incoming and in-flight solvers."""
    if not target or target <= 0:
        return None
    wt_lo = sim_fn(lo)
    wt_hi = sim_fn(hi)
    if wt_lo is None or wt_hi is None:
        return None
    while wt_hi < target and hi < 10.0:
        hi *= 1.5
        wt_hi = sim_fn(hi)
        if wt_hi is None:
            return None
    while wt_lo > target and lo > 0.001:
        lo *= 0.5
        wt_lo = sim_fn(lo)
        if wt_lo is None:
            return None
    if wt_lo > target or wt_hi < target:
        return None
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        wt = sim_fn(mid)
        if wt is None:
            return None
        if abs(wt - target) / target < tol_rel:
            return mid
        if wt < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def solve_fw_correction(
    batch: BatchInput,
    tables: BiologyTables,
) -> Optional[float]:
    """FW_Correction landing pre-cull avg wt at TranOG on TranOG_AvgWt, for an
    INCOMING batch simulated from TranSF (egg-up)."""
    return _bisect_fw_correction(
        lambda c: _simulate_fw_avg_weight_at_tran_og(batch, tables, c),
        batch.tran_og_avg_wt_g)


def _simulate_inflight_fw_weight_at_tran_og(
    batch: BatchInput, tables: BiologyTables,
    start_date, start_weight: float, dsi_at_close: int, fw_correction: float,
) -> Optional[float]:
    """Pre-cull avg wt at TranOG for an IN-FLIGHT batch: walk the FW daily grid
    from `start_date`/`start_weight` (the PR-measured state) to TranOG under a
    candidate fw_correction, applying only the bottom culls NOT already fired by
    `dsi_at_close`. Mirrors project_in_flight_fw_batch's weight path so the
    back-solved correction lands the same projection on target."""
    if not batch.tran_og_date or not batch.input_date or start_weight <= 0:
        return None
    input_date = _as_date(batch.input_date)
    tran_og_date = _as_date(batch.tran_og_date)
    cur_weight = float(start_weight)
    cur_date = _as_date(start_date)
    fired = {int(d) for d, _p in tables.culling if int(d) <= dsi_at_close}
    while cur_date < tran_og_date:
        dsi = (cur_date - input_date).days
        for thresh, pct in tables.culling:
            if dsi >= thresh and thresh not in fired:
                _, cur_weight, _, _ = _apply_bottom_cull(
                    1.0, cur_weight, batch.tran_og_cv, pct / 100.0)
                fired.add(thresh)
        sgr_base = _interp(cur_weight, tables.sgr_size_g, tables.sgr_fw_pct_day)
        cur_weight = cur_weight * (1.0 + sgr_base * fw_correction / 100.0)
        cur_date = cur_date + timedelta(days=1)
    return cur_weight


def solve_inflight_fw_correction(
    batch: BatchInput, tables: BiologyTables,
    start_date, start_weight: float, dsi_at_close: int,
) -> Optional[float]:
    """FW_Correction for an IN-FLIGHT batch: the correction applied to its
    REMAINING FW growth (from its current PR weight/date to TranOG) that lands the
    pre-cull avg wt on TranOG_AvgWt. So a running batch gets a recalibration target
    just like a not-yet-input one."""
    return _bisect_fw_correction(
        lambda c: _simulate_inflight_fw_weight_at_tran_og(
            batch, tables, start_date, start_weight, dsi_at_close, c),
        batch.tran_og_avg_wt_g)


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
    # `today` is the day being simulated, so the OG week factor for THIS week
    # is unambiguous. FW days ignore it (sgr_pct_per_day applies it SW-only).
    sgr_eff = sgr_pct_per_day(tank.avg_wt_g, stage, batch, tables,
                              iso_week_label(today))
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
        _pre_mort = cur_count
        cur_count *= _daily_survival_factor(m_weekly)
        mort_count_today = _pre_mort - cur_count

        sgr_eff = sgr_pct_per_day(cur_weight, "SW", batch, tables,
                                  iso_week_label(cur_date))
        fcr = _interp(cur_weight, tables.fcr_size_g, fcr_curve)
        cur_weight = cur_weight * (1.0 + sgr_eff / 100.0)

        biomass_kg = cur_count * cur_weight / 1000.0
        feed_kg_day = biomass_kg * (sgr_eff / 100.0) * fcr
        feed_type = _feed_type_for_size(tables, cur_weight)

        days.append((
            cur_date, dsi, "SW", cur_count, cur_weight, sgr_eff, fcr,
            m_weekly, 0.0, feed_kg_day, feed_type, biomass_kg,
            0.0, 0.0, mort_count_today,
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
            open_count=days_in_w[0][3] + days_in_w[0][12] + days_in_w[0][14],
            open_avg_weight_g=days_in_w[0][4],
            open_biomass_kg=(days_in_w[0][3] + days_in_w[0][12]
                             + days_in_w[0][14]) * days_in_w[0][4] / 1000.0,
            close_count=last[3],
            close_avg_weight_g=last[4],
            close_biomass_kg=last[11],
            mort_count_week=sum(d[14] for d in days_in_w),
        ))
    return out


def project_in_flight_fw_batch(
    batch: BatchInput,
    tables: BiologyTables,
    control: ControlParams,
    initial_count: float,
    initial_avg_wt_g: float,
    pr_closing_date,
) -> tuple[list[BatchWeekState], list[CalibrationResidual], list[SizeClassSplit]]:
    """Project an in-flight FW batch forward from PR closing date.

    For batches the operator measured in FW physical units at PR
    closing (Postsmolt, Smolt, Parr, etc. — not yet in OG tanks), the
    starting state is the PR-measured count + biomass, NOT what the
    biology model would project from input_date. This matches what
    project_in_flight_batch already does for OG-in-flight batches.

    The biology MODELS (mortality table, SGR/FCR curves) are still
    referenced via days-since-input (input_date is the anchor for
    model lookups), so the growth curve is correct for the batch's
    actual age. Only the STATE (count, biomass, avg_wt) comes from PR.

    Stage transitions during the projection:
      - FW → SW at batch.tran_og_date: handling mortality + reconciliation
        cull (sized to land on tran_og_count) + size-class split metadata.
      - Scheduled bottom culls fire at their DSI thresholds if they fall
        after pr_closing_date.

    Returns (weekly_states, calibration_residuals, size_class_splits).
    The residual + split match project_batch's contract for the same
    TranOG crossing.
    """
    if not batch.input_date or initial_count <= 0:
        return [], [], []

    input_date = _as_date(batch.input_date)
    forecast_start = _as_date(control.forecast_start)
    forecast_end = forecast_start + timedelta(weeks=control.horizon_weeks)
    tran_og_date = _as_date(batch.tran_og_date) if batch.tran_og_date else None
    og_entry_date = (_og_entry_date(tran_og_date, forecast_start)
                     if tran_og_date else None)
    pr_close = _as_date(pr_closing_date)
    handling_frac = control.handling_mortality_pct / 100.0
    fcr_key = _fcr_model_key(batch.fcr_model)
    fcr_curve = tables.fcr_by_model.get(fcr_key, [])

    # Scheduled bottom culls that ALREADY fired before PR closing — mark
    # them so they don't re-fire during the projection. DSI thresholds
    # ≤ DSI at PR closing have already been applied to the operator's
    # measured count.
    cull_thresh_dsi = [(int(d), float(p)) for d, p in tables.culling]
    dsi_at_close = (pr_close - input_date).days
    fired_culls = {d for d, _p in cull_thresh_dsi if d <= dsi_at_close}

    # Starting state from PR.
    cur_count = float(initial_count)
    cur_weight = float(initial_avg_wt_g)
    # Batch is in FW at PR closing (caller filtered for FW PR records).
    # crossed_tran_og = True only if PR closing is already past TranOG
    # (operator's PR may straddle the date in edge cases).
    stage = "SW" if (tran_og_date and pr_close >= tran_og_date) else "FW"
    crossed_tran_og = (stage == "SW")
    og_transferred = (stage == "SW")

    residuals: list[CalibrationResidual] = []
    splits: list[SizeClassSplit] = []
    days: list[tuple] = []
    cur_date = forecast_start
    while cur_date < forecast_end:
        dsi = (cur_date - input_date).days
        cull_count_today = 0.0
        cull_biomass_today = 0.0
        cull_pct_today = 0.0

        # TranOG reconciliation cull — week CONTAINING TranOG_Date (VBA
        # `wE >= TranOGDate`). Fish stay in the FW pool; OG transfer is
        # deferred to og_entry_date below (see project_batch / BUG #1).
        if not crossed_tran_og and tran_og_date and cur_date >= tran_og_date:
            _pre = cur_count
            cur_count *= (1.0 - handling_frac)
            _hm = _pre - cur_count
            if _hm > 0:
                cull_count_today += _hm
                cull_biomass_today += _hm * cur_weight / 1000.0
            if batch.tran_og_avg_wt_g and batch.tran_og_avg_wt_g > 0:
                residuals.append(CalibrationResidual(
                    batch_id=batch.batch_id,
                    tran_og_date=batch.tran_og_date,
                    target_avg_wt_g=batch.tran_og_avg_wt_g,
                    current_fw_correction=batch.fw_correction,
                    projected_pre_cull_avg_wt_g=cur_weight,
                    residual_pct=(cur_weight - batch.tran_og_avg_wt_g)
                                 / batch.tran_og_avg_wt_g * 100.0,
                    # Back-solve the correction on the REMAINING FW growth (from
                    # the PR state at forecast_start to TranOG) so a running batch
                    # gets a recalibration target too, not just incoming ones.
                    suggested_fw_correction=solve_inflight_fw_correction(
                        batch, tables, forecast_start, initial_avg_wt_g, dsi_at_close),
                ))
            target_count = float(batch.tran_og_count or 0)
            if target_count > 0 and cur_count > target_count:
                final_cull = 1.0 - target_count / cur_count
                cur_count, cur_weight, _c_n, _c_b = _apply_bottom_cull(
                    cur_count, cur_weight, batch.tran_og_cv, final_cull,
                )
                cull_count_today += _c_n
                cull_biomass_today += _c_b
            crossed_tran_og = True

        # FW -> SW (OG transfer): first forecast-week boundary on/after
        # TranOG_Date (VBA `wS >= TranOGDate`). Split emitted here (not at
        # the cull) so its count + weight match the OG-entry state that
        # placement seeds from. See project_batch for the rationale.
        if not og_transferred and og_entry_date and cur_date >= og_entry_date:
            splits.append(compute_size_class_split(
                batch_id=batch.batch_id,
                tran_og_date=batch.tran_og_date,
                post_cull_count=cur_count,
                post_cull_avg_wt_g=cur_weight,
                cv_pct=batch.tran_og_cv,
            ))
            stage = "SW"
            og_transferred = True

        # Scheduled FW bottom culls — fire if DSI threshold reached AND
        # not already fired (incl. pre-PR threshold tracking).
        for thresh_dsi, pct in cull_thresh_dsi:
            if stage == "FW" and dsi >= thresh_dsi and thresh_dsi not in fired_culls:
                cur_count, cur_weight, _c_n, _c_b = _apply_bottom_cull(
                    cur_count, cur_weight, batch.tran_og_cv, pct / 100.0,
                )
                fired_culls.add(thresh_dsi)
                cull_pct_today += pct
                cull_count_today += _c_n
                cull_biomass_today += _c_b

        # Daily mortality.
        wfi = max(0, dsi // 7)
        m_weekly = _mortality_weekly_pct(tables, wfi)
        _pre_mort = cur_count
        cur_count *= _daily_survival_factor(m_weekly)
        mort_count_today = _pre_mort - cur_count

        # Daily growth — FW vs SW SGR curve (via the shared SGR helper).
        sgr_eff = sgr_pct_per_day(cur_weight, stage, batch, tables,
                                  iso_week_label(cur_date))
        fcr = _interp(cur_weight, tables.fcr_size_g, fcr_curve)
        cur_weight = cur_weight * (1.0 + sgr_eff / 100.0)

        biomass_kg = cur_count * cur_weight / 1000.0
        feed_kg_day = biomass_kg * (sgr_eff / 100.0) * fcr
        feed_type = _feed_type_for_size(tables, cur_weight)

        days.append((
            cur_date, dsi, stage, cur_count, cur_weight, sgr_eff, fcr,
            m_weekly, cull_pct_today, feed_kg_day, feed_type, biomass_kg,
            cull_count_today, cull_biomass_today, mort_count_today,
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
        cull_count_week = sum(d[12] for d in days_in_w)
        cull_biomass_week = sum(d[13] for d in days_in_w)
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
            stage=last[2],
            feed_type=last[10],
            mortality_pct_weekly=last[7],
            cull_event_pct=sum(d[8] for d in days_in_w),
            cull_count_week=cull_count_week,
            cull_biomass_kg_week=cull_biomass_week,
            open_count=days_in_w[0][3] + days_in_w[0][12] + days_in_w[0][14],
            open_avg_weight_g=days_in_w[0][4],
            open_biomass_kg=(days_in_w[0][3] + days_in_w[0][12]
                             + days_in_w[0][14]) * days_in_w[0][4] / 1000.0,
            close_count=last[3],
            close_avg_weight_g=last[4],
            close_biomass_kg=last[11],
            mort_count_week=sum(d[14] for d in days_in_w),
        ))
    return out, residuals, splits


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
    _require_fcr_models(batches, tables)
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
