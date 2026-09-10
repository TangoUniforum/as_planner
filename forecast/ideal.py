"""The Ideal — the steady rhythm the facility can sustain at a given biomass cap.

Answers "if we started clean, what stocking rhythm would this facility run
forever?" by running L1 (the tankless envelope in `global_planner_poc`) on a
SYNTHETIC stocking stream — one batch every `cadence` days, all the same size,
built on a real batch as the template — and reading a steady year-3 window,
after the start-up transient has washed out.

It is a DERIVED answer, not a search: a small grid of (cadence, size) cells,
each one plain L1 run (~2 s), priced on the economics bands. If this module
ever reaches for a solver it has drifted back into the Global method that was
deleted on 2026-09-10.

The biomass cap is an INPUT (operator, 2026-09-10: the cap is a variable, not
a permit). Above roughly 5,000 t there is no balanced plan at all: the weekly
draw ceiling (`og_tank_ceiling_kg`, one smallest OG tank per week) caps HOG
output, so a bigger cap only builds backlog. Every Rhythm carries that ceiling
so the tab can say why.

⚠ L1 has no tanks, no density and no handling budget, so this is a
CARRYING-CAPACITY answer, not an operational plan. And the growth model runs
hot (see forecast/accuracy.py), so tonnage here is optimistic by a few percent.
"""
from __future__ import annotations

import copy
import datetime as dt
import statistics
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import yaml

from forecast import global_planner_poc as gpp
from forecast.models import BatchInput

EIGHT_LB_HOG_KG = 3.629          # the 8 lb price step, HOG kg
FW_LEAD_DAYS = 350               # egg input -> transfer to OG
SF_LEAD_DAYS = 81                # egg input -> transfer to SF
STEADY_START = dt.datetime(2027, 1, 4)
HORIZON_WEEKS = 156
WINDOW = (104, 156)              # year 3 — the start-up transient is gone
CADENCES = (49, 56, 63)
SIZES = (250_000, 280_000, 310_000, 340_000, 370_000, 400_000)
# A plan that lands less than this share of what it stocks is building a
# backlog, not producing — its "revenue" is inventory being priced.
MIN_BALANCE = 0.95
# L1 harvests one week late (harvested_kg[w] == required_kg[w-1]), which reads
# as a persistent 1.1-1.5% over-cap on a perfectly good plan. Judge a plan by
# its PEAK against the cap with this tolerance, never by illegal-week count.
PEAK_TOLERANCE = 0.02


@dataclass(frozen=True)
class Rhythm:
    """One (cadence, size) cell at one cap, measured over the steady window."""
    cadence_days: int
    batch_size: int
    cap_kg: float
    smolt_per_yr: float
    landed_per_yr: float
    balance: float                # landed / stocked
    hog_t_per_yr: float
    revenue_per_yr: float
    avg_gross_kg: float
    share_over_8lb: float
    peak_kg: float
    ceiling_hog_t_per_yr: float   # og_tank_ceiling_kg x 52 x HOG yield

    @property
    def label(self) -> str:
        return f"{self.cadence_days}d × {self.batch_size // 1000}k"

    @property
    def peak_pct_of_cap(self) -> float:
        return self.peak_kg / self.cap_kg if self.cap_kg else 0.0

    @property
    def balanced(self) -> bool:
        return (self.balance >= MIN_BALANCE
                and self.peak_kg <= self.cap_kg * (1.0 + PEAK_TOLERANCE))


def price_bands(economics: dict) -> list[tuple[float, float, float]]:
    return [(float(b["min_kg"]), float(b["max_kg"]), float(b["price_per_kg"]))
            for b in economics["price_bands"]]


def price_of(hog_kg: float, bands: Sequence[tuple[float, float, float]]) -> float:
    for lo, hi, p in bands:
        if lo <= hog_kg < hi:
            return p
    return bands[-1][2] if hog_kg >= bands[-1][1] else bands[0][2]


def value_rows(rows, bands, cv_pct: float, hog_yield: float):
    """Band-price harvest rows across a CV distribution.

    -> (revenue, hog_kg, fish, fish_over_8lb)
    """
    rev = hog = n = over = 0.0
    for r in rows:
        if r.count <= 0 or r.avg_wt_g <= 0:
            continue
        h = gpp.WeightHistogram.from_normal(r.count, r.avg_wt_g, cv_pct)
        for w_g, c in zip(h.weights, h.counts):
            hk = w_g / 1000.0 * hog_yield
            b = c * hk
            rev += b * price_of(hk, bands)
            hog += b
            n += c
            if hk >= EIGHT_LB_HOG_KG:
                over += c
    return rev, hog, n, over


def synthetic_stream(template: BatchInput, cadence_days: int, batch_size: int,
                     horizon_weeks: int = HORIZON_WEEKS,
                     start: dt.datetime = STEADY_START) -> list[BatchInput]:
    """One batch every `cadence_days`, `batch_size` fish to OG, on the template.

    The stream starts a full FW lead before `start` so fish are already
    arriving in OG on day one. FW survival is the template's own.
    """
    survival = template.input_count / float(template.tran_og_count)
    out, i = [], 0
    d = start - dt.timedelta(days=FW_LEAD_DAYS + 30)
    end = start + dt.timedelta(weeks=horizon_weeks)
    while d < end:
        out.append(BatchInput(
            batch_id="S%03d" % i, input_date=d,
            input_count=int(round(batch_size * survival)),
            tran_sf_date=d + dt.timedelta(days=SF_LEAD_DAYS),
            tran_og_date=d + dt.timedelta(days=FW_LEAD_DAYS),
            tran_og_count=int(batch_size),
            tran_og_avg_wt_g=template.tran_og_avg_wt_g,
            tran_og_cv=template.tran_og_cv,
            fcr_model=template.fcr_model,
            fw_correction=template.fw_correction,
            sgr_correction=template.sgr_correction))
        d += dt.timedelta(days=cadence_days)
        i += 1
    return out


def evaluate(cadence_days: int, batch_size: int, cap_kg: float, *,
             template, tables, control, facility, bands, cv_pct,
             horizon_weeks: int = HORIZON_WEEKS,
             window: tuple[int, int] = WINDOW) -> Rhythm:
    """Run L1 clean-slate on one (cadence, size) at one cap."""
    c = copy.deepcopy(control)
    c.forecast_start = STEADY_START
    c.horizon_weeks = horizon_weeks
    c.max_biomass_kg = float(cap_kg)
    l1 = gpp.plan(synthetic_stream(template, cadence_days, batch_size, horizon_weeks),
                  tables, c, facility, record_standing=False,
                  model_purge_hold=True, model_full_facility=True)
    lo, hi = window
    rows = [e for e in l1.envelope if lo <= e.week < hi]
    trace = [r for r in l1.trace if lo <= r.week < hi]
    hog_yield = float(control.default_hog_yield)
    rev, hog_kg, n, over = value_rows(rows, bands, cv_pct, hog_yield)
    years = (hi - lo) / 52.0
    stocked = batch_size * 365.0 / cadence_days
    landed = n / years
    return Rhythm(
        cadence_days=int(cadence_days), batch_size=int(batch_size),
        cap_kg=float(cap_kg), smolt_per_yr=stocked, landed_per_yr=landed,
        balance=landed / stocked if stocked else 0.0,
        hog_t_per_yr=hog_kg / 1000.0 / years, revenue_per_yr=rev / years,
        avg_gross_kg=(sum(r.biomass_kg for r in rows) / n) if n else 0.0,
        share_over_8lb=(over / n) if n else 0.0,
        peak_kg=max((r.standing_biomass_kg for r in trace), default=0.0),
        ceiling_hog_t_per_yr=l1.og_tank_ceiling_kg * 52.0 * hog_yield / 1000.0)


def _evaluate_job(args):
    cadence, size, cap_kg, ctx = args
    return evaluate(cadence, size, cap_kg, **ctx)


def frontier(cap_kg: float, ctx: dict, *, cadences: Sequence[int] = CADENCES,
             sizes: Sequence[int] = SIZES, workers: int = 1) -> list[Rhythm]:
    """Every (cadence, size) cell at one cap, in grid order.

    `ctx` is `load_context(...)`. Failures RAISE — a cell that silently
    vanished would make the best-of look better than it is.
    """
    jobs = [(c, s, cap_kg, ctx) for c in cadences for s in sizes]
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(_evaluate_job, jobs))
    return [_evaluate_job(j) for j in jobs]


def best(rhythms: Sequence[Rhythm]) -> Optional[Rhythm]:
    """Highest-revenue BALANCED rhythm, or None if the cap admits none.

    Ties break toward the longer cadence then the smaller batch (fewer
    handling events, fewer smolt) — a unique final sort term, so the pick
    never depends on grid order.
    """
    ok = [r for r in rhythms if r.balanced]
    if not ok:
        return None
    return max(ok, key=lambda r: (round(r.revenue_per_yr, 2),
                                  r.cadence_days, -r.batch_size))


def current_rhythm(batches: Sequence[BatchInput], last: int = 6) -> tuple[int, int]:
    """The rhythm the scenario is stocking at now: the median gap between the
    last `last` input dates and their median OG size. -> (cadence_days, size)"""
    recent = sorted((b for b in batches if b.tran_og_count),
                    key=lambda b: (b.input_date, b.batch_id))[-last:]
    if len(recent) < 2:
        raise ValueError("need at least two stocked batches to read a rhythm")
    gaps = [(b.input_date - a.input_date).days for a, b in zip(recent, recent[1:])]
    return (int(round(statistics.median(gaps))),
            int(round(statistics.median(b.tran_og_count for b in recent))))


def default_template(batches: Sequence[BatchInput]) -> BatchInput:
    """The most recently stocked batch with a real OG transfer — the best
    guide to what future batches look like."""
    real = [b for b in batches
            if b.tran_og_count and b.tran_og_avg_wt_g and b.input_count]
    if not real:
        raise ValueError("no batch with an OG transfer count, weight and input "
                         "count to use as the Ideal template")
    return max(real, key=lambda b: (b.input_date, b.batch_id))


def load_context(project_dir, template_id: Optional[str] = None) -> dict:
    """Everything `evaluate` needs, from the live config + scenario."""
    from forecast import scenario_io as sio
    from forecast.config_io import load_config

    root = Path(project_dir)
    control, tables, facility = load_config(str(root / "config"))
    batches = sio.load_batches(str(root / "scenario"))
    if template_id is None:
        template = default_template(batches)
    else:
        found = [b for b in batches if b.batch_id == template_id]
        if not found:
            raise ValueError(f"template batch {template_id!r} not in scenario")
        template = found[0]
    economics = yaml.safe_load((root / "config" / "economics.yaml").read_text())
    return dict(template=template, tables=tables, control=control,
                facility=facility, bands=price_bands(economics),
                cv_pct=float(economics.get("model_cv_pct", 18.0)))
