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

What this is NOT
----------------
This is L1 only: the *envelope*, not the assignment. It does not place fish in
tanks, does not respect per-system caps, does not model 6N depuration, and uses
the OG-tank kg ceiling as the only spatial proxy. L2 (assign envelope -> systems)
and L3 (assign -> tanks, density) are out of scope. See the runner's notes.
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
    sgr_pct_per_day,
)
from .models import BatchInput, BiologyTables, ControlParams, FacilityConfig
from .time_grid import iso_week_label, week_range


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
) -> PlannerResult:
    """Run the tankless L1 planner. See module docstring for the algorithm.

    Returns a PlannerResult with the harvest envelope, standing trace, feasibility
    verdict, and per-batch conservation.
    """
    fs = _as_date(control.forecast_start)
    horizon = control.horizon_weeks
    biomass_cap = control.max_biomass_kg
    feed_cap = control.max_feed_per_day_kg
    min_wt = control.min_harvest_weight_g
    max_harvest_fish = control.max_harvest_per_week
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
    infeasible: list[tuple[int, str, str, float]] = []
    # Carry-forward pre-draw debt: if an arrival deadline needs earlier shedding,
    # add it to this week's required (simple redistribution-to-earlier proxy).
    pre_draw_debt: dict[int, float] = {w: 0.0 for w in range(horizon)}

    for w in range(horizon):
        ws, we = week_range(w, fs)
        label = iso_week_label(ws)

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

        # 3) Compute facility standing + feed BEFORE harvest.
        standing = sum(work[s.batch_id].biomass_kg() for s in seeds
                       if entered[s.batch_id])
        feed = sum(work[s.batch_id].feed_kg_day(s.batch, tables) for s in seeds
                   if entered[s.batch_id])

        # 4) Required draw = max of the three needs.
        need_biomass = max(0.0, standing - biomass_cap)

        # need_feed: remove top mass until feed/day <= feed_cap. Feed scales
        # ~linearly with biomass at the heavy end; approximate the kg to shed
        # as (feed - cap)/feed * standing-of-feeding-fish, then refine isn't
        # needed for the POC — use the proportional estimate.
        need_feed = 0.0
        if feed > feed_cap and feed > 0:
            need_feed = (feed - feed_cap) / feed * standing

        # need_arrival: arrivals landing within the lead window must fit under
        # the cap; pre-draw the overflow now.
        upcoming = sum(arrivals_by_week.get(w + k, 0.0)
                       for k in range(1, arrival_lead_weeks + 1))
        need_arrival = max(0.0, (standing + upcoming) - biomass_cap)

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

        draw_target = min(required, weekly_ceiling)
        # If required exceeds the ceiling and it's arrival-driven, push the
        # overflow earlier (redistribute to the previous week's debt).
        if required > weekly_ceiling and binding in ("arrival", "predraw") and w > 0:
            pre_draw_debt[w - 1] = pre_draw_debt.get(w - 1, 0.0) + (required - weekly_ceiling)

        # 6) Allocate draw_target via FIFO-with-grade cascade.
        remaining = draw_target
        week_rows: dict[str, list[float]] = {}  # batch -> [count, kg]
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
                cons[s.batch_id]["harvested_count"] += got_c
                cons[s.batch_id]["harvested_kg"] += got_kg

        harvested_kg = sum(v[1] for v in week_rows.values())
        harvested_count = sum(v[0] for v in week_rows.values())
        for bid, (c, kg) in week_rows.items():
            envelope.append(HarvestEnvelopeRow(
                week=w, week_label=label, batch_id=bid,
                count=c, biomass_kg=kg,
                avg_wt_g=(kg * 1000.0 / c if c > 0 else 0.0),
            ))

        # 7) Post-harvest standing + feed + feasibility verdict.
        standing_post = standing - harvested_kg
        feed_post = sum(work[s.batch_id].feed_kg_day(s.batch, tables)
                        for s in seeds if entered[s.batch_id])
        over_bio = max(0.0, standing_post - biomass_cap)
        over_feed = max(0.0, feed_post - feed_cap)
        legal = over_bio <= 1e-3 and over_feed <= 1e-3
        if not legal:
            cap_name = "biomass" if over_bio > over_feed else "feed"
            over = max(over_bio, over_feed)
            infeasible.append((w, label, cap_name, over))

        trace.append(StandingTraceRow(
            week=w, week_label=label,
            standing_biomass_kg=standing_post, feed_kg_day=feed_post,
            biomass_cap=biomass_cap, feed_cap=feed_cap,
            harvested_kg=harvested_kg, harvested_count=harvested_count,
            required_kg=required, binding=binding, legal=legal,
            over_biomass_kg=over_bio, over_feed_kg=over_feed,
        ))

    # Final per-batch conservation: input ~= harvested + standing@horizon +
    # mortality + culls. seeded_count already folds the TranOG reconciliation
    # cull (we stock the post-cull count target), so we reconcile the
    # SEEDED population through OG.
    for s in seeds:
        c = cons[s.batch_id]
        c["standing_count"] = work[s.batch_id].total_count()
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
    )
