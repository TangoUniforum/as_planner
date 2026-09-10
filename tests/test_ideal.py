"""forecast.ideal — behavioural invariants, never pinned numbers.

The steady-state figures themselves (e.g. 49d x 280k -> $163.6M at 3,800 t)
move whenever biology or prices are re-calibrated, so they are not asserted.
What is asserted: the stream is built the way it claims, the pick obeys its
own gates and never depends on grid order, the caller's config is never
mutated, and a run is reproducible.
"""
import copy
import dataclasses
import datetime as dt
from pathlib import Path

import pytest

from forecast import ideal

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def ctx():
    return ideal.load_context(ROOT)


def _rhythm(**kw):
    base = dict(cadence_days=49, batch_size=280_000, cap_kg=3_800_000.0,
                smolt_per_yr=2.0e6, landed_per_yr=1.98e6, balance=0.99,
                hog_t_per_yr=7_600.0, revenue_per_yr=160e6, avg_gross_kg=4.6,
                share_over_8lb=0.5, peak_kg=3_800_000.0,
                ceiling_hog_t_per_yr=7_697.0)
    base.update(kw)
    return ideal.Rhythm(**base)


# --- the synthetic stream -------------------------------------------------

def test_stream_is_evenly_spaced_at_the_cadence(ctx):
    s = ideal.synthetic_stream(ctx["template"], 56, 300_000, horizon_weeks=60)
    gaps = {(b.input_date - a.input_date).days for a, b in zip(s, s[1:])}
    assert gaps == {56}


def test_stream_stocks_the_requested_size_at_the_template_survival(ctx):
    t = ctx["template"]
    s = ideal.synthetic_stream(t, 49, 250_000, horizon_weeks=60)
    assert {b.tran_og_count for b in s} == {250_000}
    want = t.input_count / float(t.tran_og_count)
    for b in s:
        assert b.input_count / b.tran_og_count == pytest.approx(want, rel=1e-5)


def test_stream_is_already_arriving_in_og_on_day_one(ctx):
    s = ideal.synthetic_stream(ctx["template"], 49, 280_000, horizon_weeks=60)
    assert min(b.tran_og_date for b in s) <= ideal.STEADY_START
    assert max(b.input_date for b in s) < (
        ideal.STEADY_START + dt.timedelta(weeks=60))


# --- pricing ---------------------------------------------------------------

def test_price_bands_are_half_open_and_clamp_at_both_ends():
    bands = [(0.0, 1.0, 1.0), (1.0, 2.0, 5.0)]
    assert ideal.price_of(0.999, bands) == 1.0
    assert ideal.price_of(1.0, bands) == 5.0           # lower edge belongs up
    assert ideal.price_of(9.0, bands) == 5.0           # above the top band
    assert ideal.price_of(-1.0, bands) == 1.0          # below the bottom band


# --- the pick --------------------------------------------------------------

def test_best_never_picks_a_backlog_plan():
    rich_backlog = _rhythm(revenue_per_yr=999e6, balance=0.80)
    honest = _rhythm(revenue_per_yr=150e6)
    assert ideal.best([rich_backlog, honest]) is honest


def test_best_never_picks_a_plan_over_the_cap_tolerance():
    over = _rhythm(revenue_per_yr=999e6,
                   peak_kg=3_800_000 * (1 + ideal.PEAK_TOLERANCE) + 1)
    at_edge = _rhythm(revenue_per_yr=150e6,
                      peak_kg=3_800_000 * (1 + ideal.PEAK_TOLERANCE))
    assert ideal.best([over, at_edge]) is at_edge


def test_best_is_none_when_the_cap_admits_no_balanced_plan():
    assert ideal.best([_rhythm(balance=0.5), _rhythm(peak_kg=9e9)]) is None
    assert ideal.best([]) is None


def test_best_does_not_depend_on_grid_order():
    tied = [_rhythm(cadence_days=c, batch_size=s)
            for c in (49, 56, 63) for s in (250_000, 280_000)]
    picks = {ideal.best(order) for order in (tied, tied[::-1],
                                             tied[1::2] + tied[::2])}
    assert len(picks) == 1


# --- today's rhythm ---------------------------------------------------------

def test_current_rhythm_reads_back_a_stream_it_was_given(ctx):
    s = ideal.synthetic_stream(ctx["template"], 56, 300_000, horizon_weeks=80)
    assert ideal.current_rhythm(s) == (56, 300_000)


def test_current_rhythm_refuses_a_single_batch(ctx):
    with pytest.raises(ValueError):
        ideal.current_rhythm([ctx["template"]])


# --- template --------------------------------------------------------------

def test_default_template_is_the_latest_real_batch(ctx):
    from forecast import scenario_io as sio
    batches = sio.load_batches(str(ROOT / "scenario"))
    t = ideal.default_template(batches)
    assert t.input_date == max(b.input_date for b in batches
                               if b.tran_og_count and b.tran_og_avg_wt_g)


def test_default_template_refuses_when_nothing_is_usable(ctx):
    blank = dataclasses.replace(ctx["template"], tran_og_count=0)
    with pytest.raises(ValueError):
        ideal.default_template([blank])


def test_unknown_template_id_is_refused_not_substituted():
    with pytest.raises(ValueError):
        ideal.load_context(ROOT, template_id="NO-SUCH-BATCH")


# --- a real L1 run (short horizon to keep it quick) -------------------------

def test_evaluate_is_reproducible_and_leaves_the_config_alone(ctx):
    before = copy.deepcopy(ctx["control"])
    kw = dict(horizon_weeks=60, window=(40, 60), **ctx)
    a = ideal.evaluate(49, 280_000, 4_200_000, **kw)
    b = ideal.evaluate(49, 280_000, 4_200_000, **kw)
    assert a == b
    assert a.cap_kg == 4_200_000
    assert ctx["control"].max_biomass_kg == before.max_biomass_kg
    assert ctx["control"].horizon_weeks == before.horizon_weeks
    assert ctx["control"].forecast_start == before.forecast_start
    assert a.smolt_per_yr == pytest.approx(280_000 * 365 / 49)
    assert a.ceiling_hog_t_per_yr > 0
