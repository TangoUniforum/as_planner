"""STANDALONE proof-of-concept: a "tankless global" harvest planner (Layer 1).

METHOD: GLOBAL (tankless L1 POC)
================================

This module is a *self-contained* proof of the core L1 math. It computes a
**harvest envelope** — how much to draw from each batch each week — that keeps
facility standing biomass AND feed/day legal across the whole horizon, *without
modelling any tanks*. The facility is treated as one global pool; the only
spatial constraint kept is a per-week harvest ceiling sized to one OG tank.

It is deliberately ADDITIVE: it imports the existing biology / config / scenario
primitives and re-uses them verbatim (no biology re-implemented). It does NOT
touch the existing pipeline, placement, or scheduler. Nothing here is imported
by `forecast/run.py`.

State representation
--------------------
Each batch is a **weight histogram** (a list of (avg_wt_g, count) bins), NOT a
single mean. The histogram is built at OG entry from (count, mean, cv) as a
truncated normal. Each simulated week, every batch:

  1. is thinned by the mortality survival factor (per `_mortality_weekly_pct` +
     `_daily_survival_factor`, compounded over 7 days);
  2. has any scheduled bottom cull due this week trimmed off the lowest bins
     (mirrors `_apply_bottom_cull`'s bottom-fraction removal, applied to the
     histogram so it is distribution-exact);
  3. grows every bin up by ITS OWN per-bin SGR (`sgr_pct_per_day(bin_wt, "SW")`
     compounded daily over the week) — fast small fish and slow big fish
     diverge, which a single-mean model cannot capture;
  4. (in pass 2) has harvest removed from the TOP bins (fish >= min size).

Two deterministic passes (no search)
-------------------------------------
Pass 1 — forward-project with NO harvest. Records per-week facility standing
biomass + feed/day (= sum over bins of bin_biomass * SGR/100 * FCR), and each
batch's OG-arrival week + arrival biomass.

Pass 2 — schedule harvest to hold the caps + meet arrival deadlines. Each week
compute the kg that MUST leave:

    required = max(
        need_biomass  = max(0, standing - biomass_cap),
        need_feed     = mass to remove so feed/day <= feed_cap,
        need_arrival  = pre-draw so standing + arrivals within lead time stay
                        under the biomass cap,
    )

Allocate `required` via the **FIFO-with-grade cascade**: draw from the oldest
batch first (take just `required` from its top bins if it has enough eligible
mass >= min_harvest_weight_g); if its fish are under min size, GRADE — take only
the >= min slice; if still short, move to the next-oldest batch's largest fish.
The weekly draw is capped at min(one OG tank's kg, max_harvest_per_week fish).
If an arrival deadline cannot be met even at the weekly ceiling, the draw is
redistributed to earlier weeks; if that still fails, the week is flagged
INFEASIBLE (over-stocked by X kg) — reported, never crashed.

Over-graze guard
----------------
Grading only fires when required > 0. A configurable `max_grade_fraction` caps
how much of a batch may be skimmed per week, and a `reserve_fraction` keeps at
least R% of each batch un-skimmed as a hard rail.

6N flow-to-harvest (opt-in `model_purge_hold`)
----------------------------------------------
By default harvest leaves the facility INSTANTLY in its draw week (the original
POC). With `model_purge_hold=True` the planner instead mirrors the production
pipeline's 6N depuration flow (see `forecast.placement` STARVE/move-in +
`forecast.sixn`), per-week resolved by `is_purge_mode` / `in_transition_window`:

  * PURGE mode: a drawn cohort is MOVED into a 6N pair ~2 weeks before harvest,
    held OFF-FEED (no feed, no growth; biomass STILL counts to standing), and
    released round-robin (~one pair/week). Standing runs higher (held, not shed
    early), feed runs lower (held fish don't eat), 6N pairs are in use.
  * PRODUCTION mode (>= sixn_production_start): off-feed IN PLACE for
    starvation_period_days, then removed; 6N main tanks join the placement pool.
  * TRANSITION window: 6N fallow — no new move-ins.

See `plan(..., model_purge_hold=...)` and `PurgeTraceRow`.

What this is NOT
----------------
This is L1 only: the *envelope*, not the assignment. It does not place fish in
specific tanks or respect per-system caps, and uses the OG-tank kg ceiling as
the spatial proxy (one pair/week in purge mode). The 6N flow is modeled at the
tankless / system-config grain (a pooled purge buffer + whole-tank 6N footprint),
NOT placement's per-tank state machine. L2 (assign envelope -> systems) and L3
(assign -> tanks, density) are out of scope. See the runner's notes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import NormalDist
from typing import Optional

from .biology import (
    _apply_bottom_cull,
    _daily_survival_factor,
    _fcr_model_key,
    _interp,
    _mortality_weekly_pct,
    project_all_batches,
    project_in_flight_fw_batch,
    sgr_pct_per_day,
)
from .models import BatchInput, BiologyTables, ControlParams, FacilityConfig
from .sixn import (
    SIXN_PAIRS,
    in_transition_window,
    is_purge_mode,
)
from .time_grid import iso_week_label, week_range


# 6N purge-hold (off-feed depuration) modeling, mirroring the production
# pipeline (`forecast.placement` STARVE / move-in flow + `forecast.sixn`):
#   * Purge mode: a harvest-bound cohort is MOVED into a 6N pair ~PURGE_HOLD_WEEKS
#     before its harvest week, held OFF-FEED (no feed, no growth; biomass still
#     counts to standing), and leaves round-robin (~one pair/week). 6N pairs ==
#     len(SIXN_PAIRS) (3) physical pairs (6 tanks), staged at 125% density.
#   * Production mode (week >= sixn_production_start): no 6N staging — harvest is
#     in-place off-feed for `starvation_period_days`, then removed; the 3 6N main
#     tanks join the production placement pool.
# The default `model_purge_hold=False` keeps every existing caller byte-identical
# (instant removal, no buffer); the L1<->L3 loop turns it ON.
_PURGE_HOLD_WEEKS = 2
_N_SIXN_PAIRS = len(SIXN_PAIRS)          # 3 depuration pairs (61/67, 63/69, 65/71)


# Number of histogram bins used to represent a batch's weight distribution.
_N_BINS = 15
# Truncate the normal at +/- this many sigma when building bins.
_TRUNC_SIGMA = 3.0


def _as_date(d) -> date:
    if isinstance(d, datetime):
        return d.date()
    return d


# ---------------------------------------------------------------------------
# Weight histogram
# ---------------------------------------------------------------------------

@dataclass
class WeightHistogram:
    """A batch's size distribution as parallel (weight, count) bins.

    `weights[i]` is the mean weight (g) of bin i; `counts[i]` is the fish count
    in that bin. Bins are kept sorted ascending by weight. All biology
    (mortality, cull, growth, harvest) operates bin-wise so the distribution —
    not just its mean — evolves correctly.
    """
    weights: list[float] = field(default_factory=list)
    counts: list[float] = field(default_factory=list)

    @classmethod
    def from_normal(cls, count: float, mean_g: float, cv_pct: float,
                    n_bins: int = _N_BINS) -> "WeightHistogram":
        """Truncated-normal histogram N(mean, mean*cv) over +/- _TRUNC_SIGMA."""
        if count <= 0 or mean_g <= 0:
            return cls([], [])
        sigma = mean_g * max(cv_pct, 0.0) / 100.0
        if sigma <= 0:
            return cls([mean_g], [count])
        lo = max(0.01, mean_g - _TRUNC_SIGMA * sigma)
        hi = mean_g + _TRUNC_SIGMA * sigma
        edges = [lo + (hi - lo) * i / n_bins for i in range(n_bins + 1)]
        nd = NormalDist(mean_g, sigma)
        weights: list[float] = []
        raw: list[float] = []
        for i in range(n_bins):
            a, b = edges[i], edges[i + 1]
            mass = nd.cdf(b) - nd.cdf(a)
            weights.append(0.5 * (a + b))
            raw.append(mass)
        total = sum(raw)
        if total <= 0:
            return cls([mean_g], [count])
        counts = [count * m / total for m in raw]
        return cls(weights, counts)

    def total_count(self) -> float:
        return sum(self.counts)

    def biomass_kg(self) -> float:
        return sum(w * c for w, c in zip(self.weights, self.counts)) / 1000.0

    def avg_wt_g(self) -> float:
        c = self.total_count()
        return (self.biomass_kg() * 1000.0 / c) if c > 0 else 0.0

    def is_empty(self) -> bool:
        return self.total_count() <= 1e-9

    def thin(self, survival_factor: float) -> None:
        """Apply a uniform survival multiplier (mortality) to every bin."""
        self.counts = [c * survival_factor for c in self.counts]

    def grow_week(self, batch: BatchInput, tables: BiologyTables) -> None:
        """Grow every bin up by its own per-bin SW SGR, compounded daily x7."""
        new_w: list[float] = []
        for w in self.weights:
            wt = w
            for _ in range(7):
                sgr = sgr_pct_per_day(wt, "SW", batch, tables)
                wt = wt * (1.0 + sgr / 100.0)
            new_w.append(wt)
        self.weights = new_w

    def feed_kg_day(self, batch: BatchInput, tables: BiologyTables) -> float:
        """Sum over bins of bin_biomass * SGR/100 * FCR — the daily feed."""
        fcr_curve = tables.fcr_by_model.get(_fcr_model_key(batch.fcr_model), [])
        total = 0.0
        for w, c in zip(self.weights, self.counts):
            if w <= 0 or c <= 0:
                continue
            bio = w * c / 1000.0
            sgr = sgr_pct_per_day(w, "SW", batch, tables)
            fcr = _interp(w, tables.fcr_size_g, fcr_curve) if fcr_curve else 1.2
            total += bio * (sgr / 100.0) * fcr
        return total

    def trim_bottom_fraction(self, frac: float) -> tuple[float, float]:
        """Remove the lowest `frac` of fish (by count) off the bottom bins.

        Mirrors `_apply_bottom_cull`'s "remove the bottom X%" semantics but
        applied bin-wise (distribution-exact). Returns (culled_count,
        culled_biomass_kg).
        """
        if frac <= 0 or self.is_empty():
            return 0.0, 0.0
        if frac >= 1.0:
            cc = self.total_count()
            cb = self.biomass_kg()
            self.counts = [0.0 for _ in self.counts]
            return cc, cb
        target = self.total_count() * frac
        removed = 0.0
        removed_bio = 0.0
        for i in range(len(self.counts)):  # ascending = smallest first
            if removed >= target:
                break
            take = min(self.counts[i], target - removed)
            removed += take
            removed_bio += take * self.weights[i] / 1000.0
            self.counts[i] -= take
        return removed, removed_bio

    def eligible_mass_kg(self, min_wt_g: float) -> float:
        """Biomass in bins at/above the harvest min weight."""
        return sum(w * c for w, c in zip(self.weights, self.counts)
                   if w >= min_wt_g) / 1000.0

    def harvest_top_kg(self, kg_wanted: float, min_wt_g: float
                       ) -> tuple[float, float]:
        """Remove up to `kg_wanted` from the TOP bins (>= min_wt_g, largest first).

        Returns (harvested_count, harvested_biomass_kg). Skips bins below the
        harvest min weight (those fish are graded out / retained).
        """
        if kg_wanted <= 0 or self.is_empty():
            return 0.0, 0.0
        got_kg = 0.0
        got_count = 0.0
        for i in range(len(self.counts) - 1, -1, -1):  # largest first
            if got_kg >= kg_wanted:
                break
            w = self.weights[i]
            if w < min_wt_g:
                continue
            c = self.counts[i]
            if c <= 0:
                continue
            bin_kg = w * c / 1000.0
            if bin_kg <= (kg_wanted - got_kg):
                take_c = c
            else:
                take_c = (kg_wanted - got_kg) * 1000.0 / w
            take_c = min(take_c, c)
            self.counts[i] -= take_c
            got_count += take_c
            got_kg += take_c * w / 1000.0
        return got_count, got_kg


# ---------------------------------------------------------------------------
# Per-batch OG-entry seeding
# ---------------------------------------------------------------------------

@dataclass
class BatchSeed:
    """An OG-entry seed for one batch: when it enters OG and its initial histogram."""
    batch_id: str
    og_entry_week: int          # forecast week index of OG entry (0 for in-flight)
    input_date: date            # for FIFO ordering (oldest = earliest input)
    hist: WeightHistogram
    input_count: float          # original stocked count (for conservation)
    batch: BatchInput


def _simulate_fw_avg_wt_to_og(batch: BatchInput, tables: BiologyTables
                              ) -> Optional[float]:
    """Pre-cull avg weight at TranOG for an INCOMING batch (FW daily sim).

    A POC approximation that mirrors biology._simulate_fw_avg_weight_at_tran_og:
    grow from FW start weight along the FW curve, firing scheduled culls, to get
    the realized OG-entry mean. Used only when the batch has no explicit
    tran_og_avg_wt_g target.
    """
    FW_START = 0.15
    if not batch.tran_sf_date or not batch.tran_og_date or not batch.input_date:
        return None
    input_date = _as_date(batch.input_date)
    tran_sf = _as_date(batch.tran_sf_date)
    tran_og = _as_date(batch.tran_og_date)
    wt = FW_START
    cur = tran_sf
    fired: set[int] = set()
    while cur < tran_og:
        dsi = (cur - input_date).days
        for thresh, pct in tables.culling:
            if dsi >= thresh and thresh not in fired:
                _, wt, _, _ = _apply_bottom_cull(1.0, wt, batch.tran_og_cv, pct / 100.0)
                fired.add(thresh)
        sgr = sgr_pct_per_day(wt, "FW", batch, tables)
        wt = wt * (1.0 + sgr / 100.0)
        cur = cur + timedelta(days=1)
    return wt


def build_seeds(
    batches: list[BatchInput],
    tables: BiologyTables,
    control: ControlParams,
    inflight_og: Optional[dict[str, tuple[float, float, float]]] = None,
) -> list[BatchSeed]:
    """Build OG-entry seeds for every batch that enters OG within the horizon.

    - In-flight OG batches (id in `inflight_og` mapping to (count, avg_wt_g,
      cv_pct)) seed at forecast week 0 from their PR-measured state.
    - Incoming batches seed at their OG-entry week from (tran_og_count,
      tran_og_avg_wt_g or a FW-simulated mean, tran_og_cv).

    POC approximations (documented):
      * OG entry week = first forecast week containing/after TranOG_Date.
      * The TranOG reconciliation cull to tran_og_count is applied at seed time
        by stocking exactly tran_og_count fish (the count target), at the target
        mean weight — equivalent to the post-cull state the real pipeline emits.
      * In-flight batches are assumed already in SW at week 0 (matches
        project_in_flight_batch).
    """
    inflight_og = inflight_og or {}
    fs = _as_date(control.forecast_start)
    horizon = control.horizon_weeks
    fs_end = fs + timedelta(weeks=horizon)
    seeds: list[BatchSeed] = []

    for b in batches:
        if b.input_date is None or b.input_count <= 0:
            continue
        input_date = _as_date(b.input_date)

        if b.batch_id in inflight_og:
            count, avg_wt, cv = inflight_og[b.batch_id]
            if count <= 0 or avg_wt <= 0:
                continue
            seeds.append(BatchSeed(
                batch_id=b.batch_id, og_entry_week=0, input_date=input_date,
                hist=WeightHistogram.from_normal(count, avg_wt, cv),
                input_count=float(b.input_count), batch=b,
            ))
            continue

        # Incoming: must have a TranOG date inside the horizon.
        if not b.tran_og_date:
            continue
        tran_og = _as_date(b.tran_og_date)
        if tran_og <= fs or tran_og >= fs_end:
            continue
        # OG entry week = first forecast week start on/after TranOG_Date.
        og_week = None
        for w in range(horizon):
            ws, we = week_range(w, fs)
            if ws >= tran_og or (ws <= tran_og < we):
                # land in the week whose start is >= tran_og (next boundary),
                # matching og_entry_week_start semantics for the POC.
                if ws >= tran_og:
                    og_week = w
                    break
        if og_week is None:
            # tran_og inside the last partial week — enter that week.
            og_week = horizon - 1
        count = float(b.tran_og_count or b.input_count)
        mean = b.tran_og_avg_wt_g
        if not mean or mean <= 0:
            mean = _simulate_fw_avg_wt_to_og(b, tables) or 0.0
        if count <= 0 or mean <= 0:
            continue
        seeds.append(BatchSeed(
            batch_id=b.batch_id, og_entry_week=og_week, input_date=input_date,
            hist=WeightHistogram.from_normal(count, mean, b.tran_og_cv),
            input_count=float(b.input_count), batch=b,
        ))

    return seeds


# ---------------------------------------------------------------------------
# FW-phase (freshwater / smolt / egg) standing — the whole-facility addend
# ---------------------------------------------------------------------------
#
# The production controller's facility biomass cap is enforced ONLY against OG
# (grow-out, seawater) biomass. But the real facility limit covers the ENTIRE
# farm: FW (freshwater/smolt/egg) standing + OG grow-out + 6N purge-hold. FW
# biomass is a GIVEN — fixed by the stocking cadence, the FW growth curve, and
# each batch's TranOG date — and it is NOT harvestable. So L1 cannot reduce it;
# it can only harvest OG harder/earlier so that (FW + OG + purge) stays under
# the facility cap each week.
#
# `fw_phase_biomass_feed_by_week` reuses the EXISTING validated biology — the
# same projectors `forecast/run.py` drives — to extract, per forecast week, the
# total FW-phase biomass (kg) and FW-phase feed (kg/day) summed across every
# batch that is still in its FW/EGG phase that week (i.e. not yet past TranOG /
# OG entry). It re-implements no biology: it calls `project_all_batches`
# (incoming batches) and `project_in_flight_fw_batch` (PR-measured FW units),
# then sums only the FW/EGG-stage `BatchWeekState` rows. The SW rows are exactly
# the population L1 already seeds at OG entry, so FW-phase and OG are DISJOINT
# (no double count): a batch contributes to FW until the forecast-week boundary
# on/after its TranOG, then to OG from that same boundary.

# Stages that count as "FW phase" (pre-OG, not yet seeded into L1's OG model).
_FW_PHASE_STAGES = ("EGG", "FW")


def fw_phase_biomass_feed_by_week(
    batches: list[BatchInput],
    tables: BiologyTables,
    control: ControlParams,
    *,
    fw_inflight: Optional[dict[str, tuple[float, float, "date"]]] = None,
) -> tuple[dict[str, float], dict[str, float]]:
    """Per-week FW-phase biomass (kg) and feed (kg/day), keyed by week LABEL.

    Reuses the production biology verbatim:
      * `project_all_batches` projects every INCOMING batch day-by-day from
        Input_Date (egg/FW growth, scheduled culls, TranOG reconciliation),
        emitting per-forecast-week `BatchWeekState` rows with a `stage`.
      * `project_in_flight_fw_batch` does the same for batches the operator
        measured in FW physical units at PR closing (anchored to PR state).

    We keep only the FW/EGG-stage rows (the pre-OG-entry phase) and sum their
    `biomass_kg` and `feed_kg_day` into per-week-label totals. The SW rows are
    the OG population L1 already models, so they are excluded here — FW-phase +
    OG are disjoint and the cap can be checked against their sum without double
    counting.

    `fw_inflight` (optional) maps batch_id -> (count, avg_wt_g, pr_closing_date)
    for FW-in-flight batches; when given those batches are projected from PR
    state instead of from Input_Date. Incoming batches present in `fw_inflight`
    are projected only via the in-flight path (no double count).
    """
    fw_inflight = fw_inflight or {}
    bio: dict[str, float] = {}
    feed: dict[str, float] = {}

    def _accumulate(rows) -> None:
        for s in rows:
            if s.stage in _FW_PHASE_STAGES:
                bio[s.week_label] = bio.get(s.week_label, 0.0) + s.biomass_kg
                feed[s.week_label] = feed.get(s.week_label, 0.0) + s.feed_kg_day

    # Incoming batches (exclude any that are FW-in-flight to avoid double count).
    incoming = [b for b in batches if b.batch_id not in fw_inflight]
    states, _resid, _splits, _warn = project_all_batches(incoming, tables, control)
    _accumulate(states)

    # FW-in-flight batches: project from PR-measured state.
    batch_by_id = {b.batch_id: b for b in batches}
    for bid, (count, avg_wt, pr_close) in fw_inflight.items():
        b = batch_by_id.get(bid)
        if b is None or count <= 0:
            continue
        fw_states, _r, _s = project_in_flight_fw_batch(
            b, tables, control, count, avg_wt, pr_close)
        _accumulate(fw_states)

    return bio, feed


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------

@dataclass
class HarvestEnvelopeRow:
    week: int
    week_label: str
    batch_id: str
    count: float
    biomass_kg: float
    avg_wt_g: float


@dataclass
class StandingTraceRow:
    week: int
    week_label: str
    standing_biomass_kg: float
    feed_kg_day: float
    biomass_cap: float
    feed_cap: float
    harvested_kg: float
    harvested_count: float
    required_kg: float
    binding: str                # which cap drove the draw
    legal: bool
    over_biomass_kg: float
    over_feed_kg: float
    # Whole-facility breakdown (only meaningful when model_full_facility=True;
    # 0.0 otherwise so existing callers are byte-identical). standing_biomass_kg
    # and feed_kg_day ABOVE are the TOTAL facility values (OG + purge + FW) when
    # full-facility is on; these split out the addends.
    fw_biomass_kg: float = 0.0       # FW/EGG-phase standing this week (given)
    fw_feed_kg_day: float = 0.0      # FW/EGG-phase feed this week (given)
    og_biomass_kg: float = 0.0       # OG grow-out standing (post-harvest)
    purge_biomass_kg: float = 0.0    # 6N purge-hold standing (off-feed)


@dataclass
class BatchStandingRow:
    """Per-(batch, week) POST-harvest standing state — exposed for L2.

    This is purely additive: it records, for each active batch each week, the
    standing biomass/count/mean-weight AFTER that week's harvest draw. It does
    not influence L1's harvest math; L2 (system assignment) consumes it.

    `in_purge` (default False) flags rows that are 6N PURGE-HOLD population —
    fish that have left grow-out into a 6N depuration pair, held off-feed for the
    rolling 2-week purge (see `model_purge_hold`). These rows carry biomass but
    ZERO feed (`feed_kg_day == 0`) and must be placed into the 6N staging pool,
    NOT the 33-tank grow-out placement pool. When `model_purge_hold` is off every
    row is grow-out (`in_purge=False`), so existing L2/L3 callers are unchanged.
    """
    week: int
    week_label: str
    batch_id: str
    count: float
    biomass_kg: float
    avg_wt_g: float
    feed_kg_day: float
    in_purge: bool = False


@dataclass
class PurgeTraceRow:
    """Per-week 6N PURGE-HOLD accounting (only when `model_purge_hold` is on).

    Mirrors the production pipeline's 6N depuration flow at the tankless grain:
    fish enter a 6N pair ~`purge_hold_weeks` before harvest, held OFF-FEED (no
    feed, no growth; biomass still counts), and leave round-robin (~one pair /
    week). See `forecast.sixn` for the reference structure being mirrored.
    """
    week: int
    week_label: str
    mode: str                    # "purge" | "production" | "transition"
    held_count: float            # fish parked in 6N this week (standing)
    held_biomass_kg: float       # their biomass (counts to standing, off-feed)
    moved_in_kg: float           # biomass that entered the hold this week
    released_kg: float           # biomass harvested OUT of the hold this week
    sixn_tanks_used: int         # 6N pair-tanks occupied (125% staged density)
    sixn_pairs_used: int         # 6N pairs occupied this week


@dataclass
class PlannerResult:
    envelope: list[HarvestEnvelopeRow]
    trace: list[StandingTraceRow]
    seeds: list[BatchSeed]
    og_tank_ceiling_kg: float
    max_harvest_per_week: float
    feasible: bool
    infeasible_weeks: list[tuple[int, str, str, float]]  # (wk, label, cap, over_kg)
    conservation: dict[str, dict]
    # Per-(batch, week) post-harvest standing, only populated when plan() is
    # called with record_standing=True. Empty otherwise (byte-identical default
    # behaviour for existing callers).
    batch_standing: list[BatchStandingRow] = field(default_factory=list)
    # Per-week 6N purge-hold accounting, only populated when model_purge_hold=True.
    # Empty otherwise (byte-identical default behaviour for existing callers).
    purge_trace: list[PurgeTraceRow] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Caps helpers
# ---------------------------------------------------------------------------

def smallest_og_tank_kg(facility: FacilityConfig) -> float:
    """Min over OG tanks of max_density_kg_m3 * volume_m3 — the weekly draw cap."""
    og = [t.max_density_kg_m3 * t.volume_m3 for t in facility.tanks
          if t.type == "OG"]
    return min(og) if og else float("inf")


# ---------------------------------------------------------------------------
# The two-pass planner
# ---------------------------------------------------------------------------

def plan(
    batches: list[BatchInput],
    tables: BiologyTables,
    control: ControlParams,
    facility: FacilityConfig,
    *,
    inflight_og: Optional[dict[str, tuple[float, float, float]]] = None,
    arrival_lead_weeks: int = 2,
    max_grade_fraction: float = 0.5,
    reserve_fraction: float = 0.05,
    harvest_tank_density_pct: float = 1.25,
    record_standing: bool = False,
    biomass_ceiling: Optional[dict[str, float]] = None,
    model_purge_hold: bool = True,
    model_full_facility: bool = False,
    fw_inflight: Optional[dict[str, tuple[float, float, "date"]]] = None,
    purge_inflight: Optional[dict[str, tuple[float, float]]] = None,
    purge_release_schedule: Optional[list[dict]] = None,
    manual_window_weeks: int = 0,
    fw_by_label: Optional[tuple[dict[str, float], dict[str, float]]] = None,
) -> PlannerResult:
    """Run the tankless L1 planner. See module docstring for the algorithm.

    Returns a PlannerResult with the harvest envelope, standing trace, feasibility
    verdict, and per-batch conservation.

    `biomass_ceiling` (ADDITIVE, opt-in) is an optional per-week-LABEL biomass
    ceiling override. When None (default) every week uses the flat
    `control.max_biomass_kg`, so existing callers are byte-identical. When given,
    week `w` harvests to hold `biomass_ceiling.get(label, control.max_biomass_kg)`
    instead of the flat facility cap — the L1<->L3 feasibility loop lowers a
    week's ceiling to what the tanks can physically realize and re-plans. The
    ceiling drives the biomass/arrival need + the legality verdict; the feed cap
    is untouched (the loop only constrains biomass).

    `model_purge_hold` (DEFAULT True — the CORRECT facility behavior) models the
    production pipeline's 6N flow-to-harvest instead of removing the harvest
    envelope instantly. It is data-dependency-free (no PR inputs needed beyond
    what L1 already consumes), so it is safe as the default. Pass False to
    recover the old byte-identical instant-removal POC (the `run_*_poc`
    diagnostics + `run_purge_compare_poc` do so explicitly):

      * PURGE mode (`forecast.sixn.is_purge_mode`): the kg drawn in a week are not
        removed at once. They are MOVED OUT of grow-out into a 6N PURGE HOLD
        (mirroring placement's STARVE move-in) and released (actually harvested)
        `_PURGE_HOLD_WEEKS` (==2) weeks later. While held they count to STANDING
        biomass but eat NOTHING (off-feed, frozen weight) and occupy 6N pairs at
        the 125% staged density. So standing biomass runs HIGHER (fish are held,
        not shed early) and feed runs LOWER (held fish don't eat). The
        `required` draw is sized against the standing that INCLUDES the hold, and
        the hold is round-robin throughput-capped at ~one pair (`_N_SIXN_PAIRS`)
        per week — at most one pair's worth of biomass releases per week.
      * PRODUCTION mode (`week >= sixn_production_start`): harvest-bound fish go
        off-feed IN PLACE for `control.starvation_period_days` then are removed;
        no separate 6N staging (the 3 6N main tanks join the placement pool — an
        L3 tank-count concern, surfaced via `available_tanks_for_week`). Modeled
        here as a short in-place off-feed hold (`starvation_period_days/7` weeks,
        rounded up) before removal.
      * TRANSITION window (`forecast.sixn.in_transition_window`): 6N is fallow;
        no new move-ins, the buffer is allowed to drain.

    The purge-hold population is recorded as `in_purge=True` BatchStandingRows
    (zero feed) so L3 places it into the 6N staging pool, not the 33-tank
    grow-out pool. Per-week 6N accounting lands in `PlannerResult.purge_trace`.

    `model_full_facility` (default False here at the `plan()` entry, but the
    CORRECT facility behavior — the loop / tool entry point MUST pass it True
    together with `fw_inflight`). It is left False as the `plan()` default ONLY
    because it is correct exclusively when `fw_inflight` (the PR-measured
    in-flight FW units) is ALSO passed; without `fw_inflight` it under-counts the
    FW phase. So `plan()`'s default stays the safe OG-only model, and the
    data-aware callers (`run_loop` below — DEFAULT True; the
    `tools/run_global_forecast.py` entry point) hydrate `fw_inflight` and pass
    `model_full_facility=True`. When True it makes L1 a TRUE whole-facility
    biomass/feed model. The production controller checks the facility cap against
    OG (grow-out) biomass ONLY, but the real limit covers the ENTIRE farm:

        facility standing = FW-phase (smolt/egg, pre-TranOG) + OG grow-out
                            + 6N purge-hold
        facility feed     = FW-phase feed + OG feed   (purge = 0, off-feed)

    FW biomass/feed is a GIVEN — fixed by the stocking cadence + FW growth +
    each batch's TranOG date — pulled from the EXISTING validated biology via
    `fw_phase_biomass_feed_by_week` (which calls `project_all_batches` +
    `project_in_flight_fw_batch`, summing only the FW/EGG-stage weeks). FW is
    NOT harvestable, so L1 cannot shrink it; instead the harvest cascade holds
    the TOTAL (FW + OG + purge) under the cap, which leaves OG an effective
    ceiling of (cap - FW(week) - purge(week)). L1 then draws OG harder/earlier
    to honor it. The biomass + feed caps, the arrival pre-draw, and the legality
    verdict are all evaluated against the whole-facility totals. When off
    (default) FW contributes nothing and the behaviour is the OG-only POC,
    byte-identical for every existing caller.

    `fw_inflight` maps batch_id -> (count, avg_wt_g, pr_closing_date) for
    FW-in-flight batches (measured in FW units at PR closing); passed through to
    the FW projector so those batches are anchored to PR state. Only consulted
    when `model_full_facility` is True.

    MANUAL OVERRIDE WINDOW semantics (`manual_window_weeks` +
    `purge_release_schedule`): when the plan CONTINUES from an operator-scripted
    manual window (forecast.manual_window), the pre-start weeks are operator
    TRUTH — only scripted events happened there. The planner must therefore not
    assume any unscripted pre-start 6N staging:

      * `manual_window_weeks > 0` DISABLES the steady-fill 6N prime below (the
        "assume a primed 6N" top-up that models fish staged during the weeks
        BEFORE forecast start — exactly the window weeks, where nothing was
        staged unless the operator scripted it). The plan's first possible
        staging action is week 0 (the handoff week), releasable after the
        purge hold — so the earliest UNSCRIPTED harvest is week
        `_PURGE_HOLD_WEEKS`.
      * `purge_release_schedule` (entries {batch_id, count, avg_wt_g,
        release_week}) carries the window-close 6N contents WITH their release
        timing: PR-start fish the window did not remove (hold already served —
        releasable from week 0, spread like `purge_inflight`), and scripted
        og_to_6n / graded-to-6N stagings (releasable `_PURGE_HOLD_WEEKS` after
        their scripted week). When given it REPLACES the `purge_inflight`
        spread (pass one or the other). Defaults (0 / None) keep every
        existing caller byte-identical.
    """
    fs = _as_date(control.forecast_start)
    horizon = control.horizon_weeks
    biomass_cap = control.max_biomass_kg

    # Whole-facility FW-phase addends: per-week-label biomass (kg) + feed
    # (kg/day) from the FW/EGG phase of every batch. Empty (zero) unless
    # model_full_facility is on, keeping the default path byte-identical.
    fw_bio_by_label: dict[str, float] = {}
    fw_feed_by_label: dict[str, float] = {}
    if model_full_facility:
        if fw_by_label is not None:
            # Caller supplied the curve (the hybrid guide passes the CONTROLLER's
            # own FW addends). project_in_flight_fw_batch starts its projection at
            # control.forecast_start with the raw PR state, so recomputing here
            # under a start that a manual window has shifted forward would
            # under-weight every FW batch — and bias the harvest envelope low.
            fw_bio_by_label, fw_feed_by_label = fw_by_label
        else:
            fw_bio_by_label, fw_feed_by_label = fw_phase_biomass_feed_by_week(
                batches, tables, control, fw_inflight=fw_inflight)

    def _bio_cap_for(label: str) -> float:
        """Per-week biomass ceiling: the override if present, else the flat cap."""
        if biomass_ceiling is None:
            return biomass_cap
        return biomass_ceiling.get(label, biomass_cap)
    feed_cap = control.max_feed_per_day_kg
    min_wt = control.min_harvest_weight_g
    # TARGET/CEILING split (operator ruling): L1 plans its weekly harvest
    # envelope at the planning TARGET (50k), not the hard processing ceiling
    # (60k) — the envelope is the SHAPE ordinary weeks should follow; the
    # realized controller may stretch toward the ceiling only when biomass
    # demands it. Unset/0 target falls back to the ceiling (pre-split configs).
    _tgt = float(getattr(control, "harvest_target_per_week", 0) or 0)
    max_harvest_fish = (min(_tgt, control.max_harvest_per_week)
                        if _tgt > 0 and control.max_harvest_per_week
                        else (_tgt or control.max_harvest_per_week))
    # Contract MIN weekly harvest (fish). The facility must never miss a weekly
    # harvest: draw at least this many fish every week even when biomass is under
    # cap (the controller applies the same clamp at placement.py:1139). 0 = off.
    min_harvest_fish = getattr(control, "min_harvest_per_week", 0) or 0
    # The staged harvest tank can be packed to harvest_tank_density_pct of the
    # normal control density (operator allowance: fish about to be harvested can
    # exceed the running production density). This sets the one-tank/week ceiling.
    og_ceiling = smallest_og_tank_kg(facility) * harvest_tank_density_pct

    seeds = build_seeds(batches, tables, control, inflight_og=inflight_og)
    # FIFO order: oldest batch (earliest input_date) drawn first.
    seeds_fifo = sorted(seeds, key=lambda s: (s.input_date, s.batch_id))
    seed_by_id = {s.batch_id: s for s in seeds}

    # Track per-batch conservation accumulators.
    cons = {s.batch_id: {
        "input_count": s.input_count,
        "seeded_count": s.hist.total_count(),
        "harvested_count": 0.0,
        "harvested_kg": 0.0,
        "mortality_count": 0.0,
        "cull_count": 0.0,
        "cull_kg": 0.0,
    } for s in seeds}

    # --- Pass 1: forward project with NO harvest, record arrivals + standing.
    # We need each batch's per-week biomass under no-harvest, plus arrival weeks.
    # We run a full no-harvest sim purely to know upcoming-arrival pressure; the
    # actual standing under harvest is computed live in pass 2.
    arrivals_by_week: dict[int, float] = {w: 0.0 for w in range(horizon)}
    for s in seeds:
        arrivals_by_week[s.og_entry_week] = (
            arrivals_by_week.get(s.og_entry_week, 0.0) + s.hist.biomass_kg())

    # --- Pass 2: live simulation with scheduled harvest.
    # Fresh working histograms (deep copies of the seed histograms).
    work: dict[str, WeightHistogram] = {
        s.batch_id: WeightHistogram(list(s.hist.weights), list(s.hist.counts))
        for s in seeds
    }
    # entered[id] flips True at the batch's OG-entry week.
    entered: dict[str, bool] = {s.batch_id: False for s in seeds}

    envelope: list[HarvestEnvelopeRow] = []
    trace: list[StandingTraceRow] = []
    batch_standing: list[BatchStandingRow] = []
    purge_trace: list[PurgeTraceRow] = []
    infeasible: list[tuple[int, str, str, float]] = []
    # Carry-forward pre-draw debt: if an arrival deadline needs earlier shedding,
    # add it to this week's required (simple redistribution-to-earlier proxy).
    pre_draw_debt: dict[int, float] = {w: 0.0 for w in range(horizon)}

    # 6N purge-hold buffer (only used when model_purge_hold). Each entry is one
    # cohort slice MOVED into 6N this week, frozen off-feed, due to be harvested
    # out `release_week` weeks later. Mirrors placement's STARVE move-in: weight
    # is frozen at move-in (no growth), feed is zero during the hold.
    #   buffer[w_release] -> list of dicts {batch_id, count, biomass_kg, avg_wt_g}
    purge_buffer: dict[int, list[dict]] = {}
    # In-place production-mode off-feed hold length in whole weeks (>=1).
    _starv_weeks = max(1, math.ceil((control.starvation_period_days or 7) / 7.0))

    # PRIME the purge pipeline with fish ALREADY in 6N at hand-over (mid-purge in
    # the PR snapshot). Mirrors the production pipeline (sixn.initial_purge_pair_
    # queue + placement's startup sixn_pair_queue, placement.py:2417): the stocked
    # 6N pairs are already in the purge queue and harvest out ~one pair/week in the
    # first weeks. Releasing them over the first _PURGE_HOLD_WEEKS gives the early
    # relief that stops L1 spinning a fresh backlog from EMPTY (the startup
    # overshoot). These fish are NOT in the grow-out seeds (the hydration split
    # them out), so they are added to seeded_count here for conservation; they
    # release as HOG in step 2b, which balances.
    if model_purge_hold and purge_release_schedule is not None:
        # MANUAL-WINDOW handoff: the window-close 6N contents arrive with
        # EXPLICIT release timing (built by the window applier from the scripted
        # events) — PR-start fish release from week 0, scripted stagings release
        # _PURGE_HOLD_WEEKS after their scripted week. This replaces the
        # purge_inflight spread below, so the hold is honored from the handoff.
        for entry in purge_release_schedule:
            _bid = entry["batch_id"]
            _c = float(entry["count"])
            if _c <= 0:
                continue
            _cc = cons.setdefault(_bid, {
                "input_count": 0.0, "seeded_count": 0.0,
                "harvested_count": 0.0, "harvested_kg": 0.0,
                "mortality_count": 0.0, "cull_count": 0.0, "cull_kg": 0.0})
            _cc["seeded_count"] += _c
            purge_buffer.setdefault(
                max(0, int(entry.get("release_week", 0))), []).append({
                    "batch_id": _bid, "count": _c,
                    "biomass_kg": float(entry["biomass_kg"]),
                    "avg_wt_g": float(entry["avg_wt_g"])})
    elif model_purge_hold and purge_inflight:
        _nrel = _PURGE_HOLD_WEEKS
        for _bid, (_c, _wt) in purge_inflight.items():
            _cc = cons.setdefault(_bid, {
                "input_count": 0.0, "seeded_count": 0.0,
                "harvested_count": 0.0, "harvested_kg": 0.0,
                "mortality_count": 0.0, "cull_count": 0.0, "cull_kg": 0.0})
            _cc["seeded_count"] += _c
            for _k in range(_nrel):
                purge_buffer.setdefault(_k, []).append({
                    "batch_id": _bid, "count": _c / _nrel,
                    "biomass_kg": _c / _nrel * _wt / 1000.0, "avg_wt_g": _wt})

    # ASSUME A PRIMED 6N at forecast start (operator directive: "what we have
    # today is not important — follow the constraints"). The handover 6N snapshot
    # can be UNDER-primed (e.g. the July PR handed over only ~145k kg), which
    # starves the first _PURGE_HOLD_WEEKS of harvest so biomass drifts OVER cap
    # while the depuration pipeline fills from empty. Model the 6N at its steady
    # operating fill instead: top up EACH of the first _PURGE_HOLD_WEEKS release
    # slots to ~one harvest-tank's throughput (og_ceiling) by DRAWING the shortfall
    # from the largest harvest-ready grow-out fish (top-down, >= min_wt, FIFO by
    # age). CONSERVING: those fish are already in the grow-out seeds, so this only
    # MOVES them into the buffer (reduces `work`) exactly like the in-week draw —
    # it never adds to seeded_count. Result: harvest is at the steady rate from
    # week 1 (>= the contract min), so the facility rides UP TO and UNDER the cap
    # with no startup ramp/overshoot.
    # WINDOW SEMANTICS: this steady-fill prime models staging that would have
    # happened in the weeks BEFORE forecast start. After a manual override
    # window those weeks are operator-scripted truth (only scripted events
    # happened), so the prime is an implicit unscripted staging — it must not
    # run. The first plannable staging is then week 0 (the handoff), and the
    # earliest unscripted release is week _PURGE_HOLD_WEEKS.
    if (model_purge_hold and manual_window_weeks == 0
            and is_purge_mode(control, fs)):
        _first_label = iso_week_label(fs)
        # Per-slot prime target = one harvest-tank (og_ceiling) PLUS a share of any
        # handover EXCESS-over-cap, so an over-cap starting state (e.g. from an
        # under-harvested manual window) is drawn back UNDER the cap from week 1
        # rather than riding over it while the L1's own draws lag. Bounded by the
        # max harvest rate (contract max fish * the eligible mean weight) so we
        # never prime beyond a physically/contractually harvestable week. An
        # under-cap handover has excess 0 -> primes to one tank (no over-harvest).
        _grow0 = sum(work[s.batch_id].biomass_kg()
                     for s in seeds if s.og_entry_week == 0)
        _held0 = sum(e["biomass_kg"] for rel in purge_buffer.values() for e in rel)
        _excess = max(0.0, (_grow0 + _held0
                            + fw_bio_by_label.get(_first_label, 0.0))
                      - _bio_cap_for(_first_label))
        _e_kg = sum(work[s.batch_id].eligible_mass_kg(min_wt)
                    for s in seeds if s.og_entry_week == 0)
        _e_ct = sum(sum(c for ww, c in zip(work[s.batch_id].weights,
                                           work[s.batch_id].counts) if ww >= min_wt)
                    for s in seeds if s.og_entry_week == 0)
        _mean_wt = (_e_kg * 1000.0 / _e_ct) if _e_ct > 0 else min_wt
        _prime_max = max_harvest_fish * _mean_wt / 1000.0
        # The peak sits at the HANDOVER week (first release slot), so shed the
        # excess EARLIEST-slot-first (fill buffer[0] up to the max harvest rate,
        # overflow to buffer[1], ...) rather than spreading it — spreading only
        # lands half the shed on the peak week. A small (0.5%) margin absorbs the
        # handover week's own growth so it lands strictly under, not exactly on.
        _remaining = (_excess + 0.005 * _bio_cap_for(_first_label)
                      if _excess > 0 else 0.0)
        for _k in range(_PURGE_HOLD_WEEKS):
            _slot_extra = min(_remaining, max(0.0, _prime_max - og_ceiling))
            _remaining -= _slot_extra
            _slot_target = og_ceiling + _slot_extra
            _have = sum(e["biomass_kg"] for e in purge_buffer.get(_k, []))
            _need = max(0.0, _slot_target - _have)
            for s in seeds_fifo:
                if _need <= 1e-6:
                    break
                got_c, got_kg = work[s.batch_id].harvest_top_kg(_need, min_wt)
                if got_kg > 1e-9:
                    purge_buffer.setdefault(_k, []).append({
                        "batch_id": s.batch_id, "count": got_c,
                        "biomass_kg": got_kg,
                        "avg_wt_g": (got_kg * 1000.0 / got_c if got_c > 0 else 0.0),
                        "sixn": True})
                    _need -= got_kg

    for w in range(horizon):
        ws, we = week_range(w, fs)
        label = iso_week_label(ws)
        # 6N mode for THIS week (mirrors forecast.sixn resolution at week grain).
        _purge = is_purge_mode(control, ws) if model_purge_hold else False
        _transition = (in_transition_window(control, ws)
                       if model_purge_hold else False)
        _hold_weeks = _PURGE_HOLD_WEEKS if _purge else _starv_weeks

        # 1) Activate batches entering OG this week.
        for s in seeds:
            if s.og_entry_week == w:
                entered[s.batch_id] = True

        # 2) Apply weekly biology (mortality, cull, growth) to active batches.
        for s in seeds:
            if not entered[s.batch_id]:
                continue
            h = work[s.batch_id]
            if h.is_empty():
                continue
            b = s.batch
            input_date = _as_date(b.input_date)
            dsi = (ws - input_date).days
            wfi = max(0, dsi // 7)
            m_weekly = _mortality_weekly_pct(tables, wfi)
            surv_daily = _daily_survival_factor(m_weekly)
            surv_week = surv_daily ** 7
            pre = h.total_count()
            h.thin(surv_week)
            cons[s.batch_id]["mortality_count"] += pre - h.total_count()
            # Scheduled bottom culls due this week (DSI in [dsi, dsi+7)).
            for thresh, pct in tables.culling:
                if dsi <= thresh < dsi + 7:
                    cc, cb = h.trim_bottom_fraction(pct / 100.0)
                    cons[s.batch_id]["cull_count"] += cc
                    cons[s.batch_id]["cull_kg"] += cb
            h.grow_week(b, tables)

        # 2b) PURGE-HOLD release: fish whose hold ends this week LEAVE 6N now —
        # this is the actual harvest (HOG) for the week. The held cohorts were
        # frozen off-feed since move-in, so they release at their move-in weight.
        released_rows: dict[str, list[float]] = {}  # batch -> [count, kg]
        released_kg = 0.0
        if model_purge_hold:
            for entry in purge_buffer.pop(w, []):
                bid = entry["batch_id"]
                acc = released_rows.setdefault(bid, [0.0, 0.0])
                acc[0] += entry["count"]
                acc[1] += entry["biomass_kg"]
                released_kg += entry["biomass_kg"]
                cons[bid]["harvested_count"] += entry["count"]
                cons[bid]["harvested_kg"] += entry["biomass_kg"]
            for bid, (c, kg) in released_rows.items():
                envelope.append(HarvestEnvelopeRow(
                    week=w, week_label=label, batch_id=bid,
                    count=c, biomass_kg=kg,
                    avg_wt_g=(kg * 1000.0 / c if c > 0 else 0.0),
                ))

        # Biomass STILL held in the purge buffer (not yet released): counts to
        # standing, eats nothing (off-feed). Frozen at move-in weight.
        held_biomass = sum(e["biomass_kg"]
                           for rel in purge_buffer.values() for e in rel)
        held_count = sum(e["count"]
                         for rel in purge_buffer.values() for e in rel)

        # 3) Compute facility standing + feed BEFORE this week's draw.
        # Standing INCLUDES the off-feed purge hold (fish are still on-farm);
        # feed EXCLUDES it (depuration fish eat nothing). With
        # model_full_facility, ALSO include the FW-phase (smolt/egg) standing +
        # feed — a GIVEN this week (not harvestable). The TOTAL is then what the
        # facility cap is checked against, so OG gets squeezed to (cap - FW -
        # purge). When off, fw_bio/fw_feed are 0 and this is the OG-only model.
        grow_biomass = sum(work[s.batch_id].biomass_kg() for s in seeds
                           if entered[s.batch_id])
        og_feed = sum(work[s.batch_id].feed_kg_day(s.batch, tables) for s in seeds
                      if entered[s.batch_id])
        fw_bio = fw_bio_by_label.get(label, 0.0)
        fw_feed = fw_feed_by_label.get(label, 0.0)
        standing = grow_biomass + held_biomass + fw_bio
        feed = og_feed + fw_feed

        # 4) Required draw = max of the three needs. The biomass ceiling is the
        # per-week override when given (else the flat facility cap).
        wk_bio_cap = _bio_cap_for(label)
        need_biomass = max(0.0, standing - wk_bio_cap)
        # ANTICIPATORY pacing (BOTH 6N modes): a drawn fish is held off-feed — in
        # the 6N pool (purge mode) or in-place (production-mode starvation) — and
        # keeps counting as standing until it RELEASES `_hold_weeks` later, so
        # drawing this week barely moves this week's standing and the relief LAGS
        # by the hold. Reactive drawing therefore overshoots the cap. This was
        # gated to purge mode only, which left PRODUCTION mode (2028+) with no
        # look-ahead — it rode ~5% OVER the cap. Both modes have the same lag, so
        # draw against the grow-out PROJECTED forward to the release week (frozen
        # FW + held excluded — off-feed fish don't grow) so the release lands
        # before the true total breaches. held_biomass is 0 in production mode.
        if model_purge_hold and grow_biomass > 0:
            _tb = _tg = 0.0
            for _s in seeds:
                if not entered[_s.batch_id]:
                    continue
                _st = work[_s.batch_id]
                _b = _st.biomass_kg()
                if _b <= 0:
                    continue
                _sgr = sgr_pct_per_day(_st.avg_wt_g(), "SW", _s.batch, tables)
                _tb += _b
                _tg += _b * (1.0 + _sgr / 100.0) ** 7
            _wk_factor = (_tg / _tb) if _tb > 0 else 1.0
            _gross = grow_biomass * (_wk_factor ** _hold_weeks)
            # Projecting the FULL current grow-out forward `_hold_weeks` assumes NO
            # intermediate harvest — but the on-feed pool is continuously drawn
            # down to hold the cap, so the gross projection over-estimates the
            # release-week standing and over-harvests, landing the plan ~3% UNDER
            # the cap. Discount by ~half the cap-holding drawdown (grow * (factor-1)
            # per week over the hold) so the plan TARGETS the cap (precalc should
            # sit as close to 100% as the math allows), not a margin below it.
            _draw_offset = 0.5 * grow_biomass * (_wk_factor - 1.0) * _hold_weeks
            _proj_grow = max(grow_biomass, _gross - _draw_offset)
            # The purge backlog (held, off-feed, ~steady) still occupies the cap
            # at the release week, so the grow-out must be drawn against
            # (cap - FW - held), not (cap - FW). Subtracting held is what brings
            # the true total to the cap instead of cap+held.
            # Hold ONE deviation band BELOW the cap (like the shipped controller).
            # The anticipation targets the flat cap exactly, but the 2-week purge-
            # hold lag lets grow-out regrow into a sawtooth that crests ~1% OVER
            # the cap. Aiming a band low turns the crest into ~100% instead of
            # ~101% — only ever harvests slightly more/earlier, never a zero week.
            _target_cap = wk_bio_cap * (1.0 - control.facility_biomass_deviation_pct)
            need_biomass = max(need_biomass,
                               (_proj_grow + fw_bio + held_biomass) - _target_cap)

        # need_feed: remove top mass until feed/day <= feed_cap. Feed scales
        # ~linearly with biomass at the heavy end; approximate the kg to shed
        # proportionally. Only OG (grow-out) fish are harvestable — FW-phase +
        # purge feed cannot be cut by harvesting — so under model_full_facility
        # the shed is scaled against the OG feed/biomass (shedding kg of OG cuts
        # og_feed/grow_biomass of feed per kg). When off (default), the original
        # whole-standing proportional estimate is preserved byte-identical.
        need_feed = 0.0
        if feed > feed_cap and feed > 0:
            if model_full_facility:
                if og_feed > 1e-9 and grow_biomass > 0:
                    need_feed = (feed - feed_cap) / og_feed * grow_biomass
            else:
                need_feed = (feed - feed_cap) / feed * standing

        # need_arrival: arrivals landing within the lead window must fit under
        # the cap; pre-draw the overflow now. Under model_full_facility the
        # FW->OG transfer is biomass-NEUTRAL at the facility level (the arriving
        # cohort is already counted as FW standing this week), so adding it again
        # would double-count — the biomass need already covers it; zero the
        # lookahead. When off, keep the original OG-arrival pre-draw.
        if model_full_facility:
            need_arrival = 0.0
        else:
            upcoming = sum(arrivals_by_week.get(w + k, 0.0)
                           for k in range(1, arrival_lead_weeks + 1))
            need_arrival = max(0.0, (standing + upcoming) - wk_bio_cap)

        required = max(need_biomass, need_feed, need_arrival,
                       pre_draw_debt.get(w, 0.0))

        binding = "none"
        if required > 0:
            binding = max(
                (("biomass", need_biomass), ("feed", need_feed),
                 ("arrival", need_arrival), ("predraw", pre_draw_debt.get(w, 0.0))),
                key=lambda kv: kv[1])[0]

        # 5) Weekly ceiling = min(one OG tank kg, max_harvest fish as kg).
        # Convert the fish ceiling to kg using the eligible mean weight.
        eligible_kg = sum(work[s.batch_id].eligible_mass_kg(min_wt)
                          for s in seeds if entered[s.batch_id])
        eligible_count = sum(
            sum(c for ww, c in zip(work[s.batch_id].weights,
                                   work[s.batch_id].counts) if ww >= min_wt)
            for s in seeds if entered[s.batch_id])
        elig_mean_wt = (eligible_kg * 1000.0 / eligible_count
                        if eligible_count > 0 else min_wt)
        fish_ceiling_kg = max_harvest_fish * elig_mean_wt / 1000.0
        weekly_ceiling = min(og_ceiling, fish_ceiling_kg)
        # In purge mode the 6N round-robin clears ~one PAIR (2 staged tanks) per
        # week, so the weekly MOVE-IN throughput is one pair's worth at the 125%
        # staged density (still bounded by the fish/wk processing cap). This is
        # the depuration pipeline's physical rate, replacing the single-tank OG
        # ceiling that modeled instant removal.
        if model_purge_hold and _purge:
            pair_ceiling = 2.0 * og_ceiling
            weekly_ceiling = min(pair_ceiling, fish_ceiling_kg)

        # MIN-HARVEST FLOOR (contract): never miss a weekly harvest — draw AT
        # LEAST min_harvest_per_week fish (as kg at the eligible mean weight) even
        # when biomass is under cap, so the facility maintains its steady contract
        # harvest AND primes the 6N pipeline from week 1 (which also keeps biomass
        # from drifting over cap while the pipeline ramps). Capped by the weekly
        # ceiling and by the mass actually eligible (>= min_wt) to harvest.
        min_floor_kg = min(min_harvest_fish * elig_mean_wt / 1000.0, eligible_kg)
        draw_target = min(max(required, min_floor_kg), weekly_ceiling)
        # If required exceeds the ceiling and it's arrival-driven, push the
        # overflow earlier (redistribute to the previous week's debt).
        if required > weekly_ceiling and binding in ("arrival", "predraw") and w > 0:
            pre_draw_debt[w - 1] = pre_draw_debt.get(w - 1, 0.0) + (required - weekly_ceiling)

        # In the TRANSITION window 6N is fallow (empty) — no new move-ins; let the
        # buffer drain and hold the draw at zero this week.
        if model_purge_hold and _transition:
            draw_target = 0.0

        # 6) Allocate draw_target via FIFO-with-grade cascade. With the purge-hold
        # model the drawn fish are MOVED into 6N (parked in the buffer, frozen
        # off-feed) and released `_hold_weeks` later; the week's HOG is the
        # buffer RELEASE computed in step 2b. Without it (default) the draw IS
        # the week's instant HOG (byte-identical legacy behaviour).
        remaining = draw_target
        week_rows: dict[str, list[float]] = {}  # batch -> [count, kg]
        moved_in_kg = 0.0
        for s in seeds_fifo:
            if remaining <= 1e-6:
                break
            if not entered[s.batch_id]:
                continue
            h = work[s.batch_id]
            if h.is_empty():
                continue
            # Over-graze guard: reserve + max grade fraction per batch/week.
            batch_count = h.total_count()
            reserve_kg = reserve_fraction * h.biomass_kg()
            max_grade_kg = max_grade_fraction * h.biomass_kg()
            elig = h.eligible_mass_kg(min_wt)
            allowable = max(0.0, min(elig - reserve_kg, max_grade_kg, remaining))
            if allowable <= 1e-6:
                continue
            got_c, got_kg = h.harvest_top_kg(allowable, min_wt)
            if got_kg > 0:
                week_rows[s.batch_id] = [got_c, got_kg]
                remaining -= got_kg
                if model_purge_hold:
                    # PURGE mode: MOVE into a 6N pair (staged, `sixn=True`).
                    # PRODUCTION mode: off-feed IN PLACE (`sixn=False` — it stays
                    # on a grow-out tank, NOT 6N staging). Release (=harvest)
                    # _hold_weeks later; frozen at the drawn slice's move-in mean.
                    rel = w + _hold_weeks
                    purge_buffer.setdefault(rel, []).append({
                        "batch_id": s.batch_id, "count": got_c,
                        "biomass_kg": got_kg,
                        "avg_wt_g": (got_kg * 1000.0 / got_c if got_c > 0 else 0.0),
                        "sixn": _purge,
                    })
                    moved_in_kg += got_kg
                    # conservation credited at RELEASE (step 2b), not here.
                else:
                    cons[s.batch_id]["harvested_count"] += got_c
                    cons[s.batch_id]["harvested_kg"] += got_kg

        if model_purge_hold:
            # The week's HOG is what was RELEASED from 6N (step 2b); the envelope
            # rows were already emitted there.
            drawn_kg = sum(v[1] for v in week_rows.values())
            harvested_kg = released_kg
            harvested_count = sum(v[0] for v in released_rows.values())
        else:
            drawn_kg = 0.0
            harvested_kg = sum(v[1] for v in week_rows.values())
            harvested_count = sum(v[0] for v in week_rows.values())
            for bid, (c, kg) in week_rows.items():
                envelope.append(HarvestEnvelopeRow(
                    week=w, week_label=label, batch_id=bid,
                    count=c, biomass_kg=kg,
                    avg_wt_g=(kg * 1000.0 / c if c > 0 else 0.0),
                ))

        # 7) Post-harvest standing + feed + feasibility verdict. The draw left
        # grow-out (it is now off-feed in 6N), so subtract this week's MOVE-IN
        # from the grow-out biomass; the held buffer (incl. this week's move-in,
        # minus this week's release) is added back as standing. Under
        # model_full_facility the FW-phase standing + feed (a given) are added to
        # the TOTAL the cap is checked against (FW is not touched by harvest).
        if model_purge_hold:
            grow_post = grow_biomass - drawn_kg
            held_post = held_biomass + moved_in_kg  # release already popped
            og_purge_post = grow_post + held_post
        else:
            grow_post = grow_biomass - harvested_kg
            held_post = held_biomass
            og_purge_post = standing - harvested_kg - fw_bio  # = grow_post (no FW)
        standing_post = og_purge_post + fw_bio
        og_feed_post = sum(work[s.batch_id].feed_kg_day(s.batch, tables)
                           for s in seeds if entered[s.batch_id])
        feed_post = og_feed_post + fw_feed
        over_bio = max(0.0, standing_post - wk_bio_cap)
        over_feed = max(0.0, feed_post - feed_cap)
        legal = over_bio <= 1e-3 and over_feed <= 1e-3
        if not legal:
            cap_name = "biomass" if over_bio > over_feed else "feed"
            over = max(over_bio, over_feed)
            infeasible.append((w, label, cap_name, over))

        trace.append(StandingTraceRow(
            week=w, week_label=label,
            standing_biomass_kg=standing_post, feed_kg_day=feed_post,
            biomass_cap=wk_bio_cap, feed_cap=feed_cap,
            harvested_kg=harvested_kg, harvested_count=harvested_count,
            required_kg=required, binding=binding, legal=legal,
            over_biomass_kg=over_bio, over_feed_kg=over_feed,
            fw_biomass_kg=fw_bio, fw_feed_kg_day=fw_feed,
            og_biomass_kg=grow_post, purge_biomass_kg=held_post,
        ))

        # 8) (additive, opt-in) record per-(batch, week) POST-harvest standing
        # so L2 can assign the standing population to systems. This reads the
        # already-evolved working histograms; it does not alter the harvest math.
        if record_standing:
            for s in seeds:
                if not entered[s.batch_id]:
                    continue
                h = work[s.batch_id]
                if h.is_empty():
                    continue
                batch_standing.append(BatchStandingRow(
                    week=w, week_label=label, batch_id=s.batch_id,
                    count=h.total_count(), biomass_kg=h.biomass_kg(),
                    avg_wt_g=h.avg_wt_g(),
                    feed_kg_day=h.feed_kg_day(s.batch, tables),
                ))
            # 8b) The off-feed HOLD population is ALSO standing. PURGE-mode holds
            # (sixn=True) occupy 6N pairs -> flagged in_purge so L3 routes them to
            # the 6N staging pool (not the 33-tank grow-out pool). PRODUCTION-mode
            # in-place starvation (sixn=False) stays on a GROW-OUT tank -> recorded
            # as ordinary (non-purge) standing so it competes for the 36-tank
            # production pool. Both are OFF-FEED (feed_kg_day=0).
            if model_purge_hold:
                held_6n: dict[str, list[float]] = {}     # bid -> [count, kg]
                held_inplace: dict[str, list[float]] = {}
                for rel in purge_buffer.values():
                    for e in rel:
                        tgt = held_6n if e.get("sixn") else held_inplace
                        acc = tgt.setdefault(e["batch_id"], [0.0, 0.0])
                        acc[0] += e["count"]
                        acc[1] += e["biomass_kg"]
                for bid, (c, kg) in held_6n.items():
                    if c <= 1e-9:
                        continue
                    batch_standing.append(BatchStandingRow(
                        week=w, week_label=label, batch_id=bid,
                        count=c, biomass_kg=kg,
                        avg_wt_g=(kg * 1000.0 / c if c > 0 else 0.0),
                        feed_kg_day=0.0, in_purge=True,
                    ))
                for bid, (c, kg) in held_inplace.items():
                    if c <= 1e-9:
                        continue
                    batch_standing.append(BatchStandingRow(
                        week=w, week_label=label, batch_id=bid,
                        count=c, biomass_kg=kg,
                        avg_wt_g=(kg * 1000.0 / c if c > 0 else 0.0),
                        feed_kg_day=0.0, in_purge=False,
                    ))

        # 8c) (additive, opt-in) per-week 6N purge-hold accounting trace. The
        # held population AFTER this week's release + move-in is the current
        # buffer contents; 6N tank/pair footprint is its whole-tank ceil at the
        # 125% staged (one-tank) density.
        if model_purge_hold:
            held_now_kg = sum(e["biomass_kg"]
                              for rel in purge_buffer.values() for e in rel)
            held_now_ct = sum(e["count"]
                              for rel in purge_buffer.values() for e in rel)
            # 6N staging footprint counts ONLY 6N-staged (purge) holds; the
            # production-mode in-place starvation occupies grow-out tanks instead.
            held_6n_kg = sum(e["biomass_kg"]
                             for rel in purge_buffer.values() for e in rel
                             if e.get("sixn"))
            sixn_tanks = (math.ceil(held_6n_kg / og_ceiling)
                          if held_6n_kg > 1e-6 else 0)
            sixn_pairs = math.ceil(sixn_tanks / 2.0) if sixn_tanks else 0
            mode = ("transition" if _transition else
                    "purge" if _purge else "production")
            purge_trace.append(PurgeTraceRow(
                week=w, week_label=label, mode=mode,
                held_count=held_now_ct, held_biomass_kg=held_now_kg,
                moved_in_kg=moved_in_kg, released_kg=released_kg,
                sixn_tanks_used=sixn_tanks, sixn_pairs_used=sixn_pairs,
            ))

    # Final per-batch conservation: input ~= harvested + standing@horizon +
    # mortality + culls. seeded_count already folds the TranOG reconciliation
    # cull (we stock the post-cull count target), so we reconcile the
    # SEEDED population through OG.
    # Any fish still in the 6N purge hold at the horizon end are on-farm standing
    # (never released): fold them into the per-batch standing so conservation
    # (seeded == harvested + standing + mort + cull) still closes exactly.
    held_at_end: dict[str, float] = {}
    if model_purge_hold:
        for rel in purge_buffer.values():
            for e in rel:
                held_at_end[e["batch_id"]] = (
                    held_at_end.get(e["batch_id"], 0.0) + e["count"])
    for s in seeds:
        c = cons[s.batch_id]
        c["standing_count"] = (work[s.batch_id].total_count()
                               + held_at_end.get(s.batch_id, 0.0))
        c["standing_kg"] = work[s.batch_id].biomass_kg()
        accounted = (c["harvested_count"] + c["standing_count"]
                     + c["mortality_count"] + c["cull_count"])
        c["accounted_count"] = accounted
        c["residual_count"] = c["seeded_count"] - accounted
        c["residual_pct"] = (100.0 * c["residual_count"] / c["seeded_count"]
                             if c["seeded_count"] > 0 else 0.0)

    return PlannerResult(
        envelope=envelope, trace=trace, seeds=seeds,
        og_tank_ceiling_kg=og_ceiling, max_harvest_per_week=max_harvest_fish,
        feasible=(len(infeasible) == 0), infeasible_weeks=infeasible,
        conservation=cons,
        batch_standing=batch_standing,
        purge_trace=purge_trace,
    )
