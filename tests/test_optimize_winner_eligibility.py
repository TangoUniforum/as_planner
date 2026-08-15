"""Optimize mode may not crown a plan that breaks a hard rule.

`forecast.optimize.recommend` used to select on the emphasis score with ONE
filter — conservation. Everything else the tuned tournament checks was absent,
and this winner is the one that gets APPLIED: `tools/auto_optimize.py
--save-config` and the app's Apply / Auto-optimize / Analyze-quick panels all
call `optimize.save_overrides_to_config`, which merges the winning knobs into
the operator's real control.yaml. An ineligible winner there becomes the
standing configuration for every later run.

Both missing guards were added because they caught real failures:

* the emphasis objective is statistically blind to the harvest floor
  (corr(worst week, score) = -0.03 over a 40-variant pool; the pool's
  best-floor plan ranked 36th of 40), so the search could sell the
  steady-harvest contract for density/biomass gains;
* the weekly processing limit was protected by a single objective term, and
  the "Product quality" preset sets that term's weight to 0 — its winners
  planned 82,000-83,500-fish weeks on 4 of 8 starting states, ~50% over the
  55,000 limit.

Every test here FAILS on the parent commit. The predicates are imported from
`forecast.tournament`, never re-implemented — `test_the_predicates_are_not_a
_second_copy` pins that, because two copies of a business rule drift.
"""
from __future__ import annotations

import dataclasses
import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import forecast.optimize as O          # noqa: E402
import forecast.tournament as T        # noqa: E402


def _variant(overrides, *, label="v", dropped=0, overprod=0, failed=None,
             zero_weeks=0, ceiling_weeks=0, min_week=20000.0, **components):
    """A measured variant. Defaults are ELIGIBLE on every guard, so each test
    breaks exactly one rule and nothing else."""
    m = O._infeasible_metrics()
    m = dataclasses.replace(m, **{c: 1.0 for c in O.COMPONENTS})
    m = dataclasses.replace(m, harvest_zero_weeks=zero_weeks,
                            weeks_over_relief_ceiling=ceiling_weeks,
                            harvest_min_week=min_week, **components)
    return O.OptVariant(label=label, overrides=dict(overrides), metrics=m,
                        dropped=dropped, overprod=overprod, failed=failed)


def _rec(variants):
    return O.recommend(variants, emphasis=O.DEFAULT_EMPHASIS,
                       weights={c: 1.0 for c in O.COMPONENTS})


# --------------------------------------------------------------------------- #
# The three guards, each as a negative control
# --------------------------------------------------------------------------- #
def test_a_relief_ceiling_breach_cannot_win_however_good_its_score():
    """The 4-of-8-states failure: an 82k-fish week is 50% over the processing
    limit, and under "Product quality" nothing in the objective could see it."""
    breach = _variant({"harvest_relief_pct": 0.5}, label="breach",
                      ceiling_weeks=4, harvest_var=0.0)      # best score
    clean = _variant({"harvest_relief_pct": 0.1}, label="clean",
                     ceiling_weeks=0, harvest_var=0.9)
    rec = _rec([breach, clean])
    assert rec.overrides == {"harvest_relief_pct": 0.1}
    assert rec.guards["ceiling"] == "applied"


def test_an_empty_harvest_week_cannot_win_however_good_its_score():
    """Never-an-empty-week is a HARD rule (the contract), not a preference."""
    empty = _variant({"a": 1}, label="empty", zero_weeks=3, harvest_var=0.0)
    clean = _variant({"a": 2}, label="clean", zero_weeks=0, harvest_var=0.9)
    rec = _rec([empty, clean])
    assert rec.overrides == {"a": 2}
    assert rec.guards["hard"] == "applied"


def test_a_variant_below_the_baseline_worst_week_cannot_win():
    """The measured 7.29 shape: the emphasis-best knob set cut the worst
    harvest week 20,526 -> 16,185 fish and the score never noticed."""
    baseline = _variant({}, label="baseline", min_week=20526.0, harvest_var=0.9)
    regress = _variant({"dev": 0.01}, label="regress", min_week=16185.0,
                       harvest_var=0.0)                       # best score
    holds = _variant({"dev": 0.005}, label="holds", min_week=21871.0,
                     harvest_var=0.5)
    rec = _rec([baseline, regress, holds])
    assert rec.overrides == {"dev": 0.005}
    assert rec.guards["floor"] == "applied"


# --------------------------------------------------------------------------- #
# UNKNOWN is never a pass
# --------------------------------------------------------------------------- #
def test_an_unmeasured_metric_is_not_treated_as_a_pass():
    """An old cache entry predating a metric field must not be crowned on the
    strength of the field being missing."""
    # empty-week count unknown (None) -> not a hard-gate pass
    unknown_z = _variant({"a": 1}, label="unknown-z", zero_weeks=None,
                         harvest_var=0.0)
    known = _variant({"a": 2}, label="known", harvest_var=0.9)
    assert _rec([unknown_z, known]).overrides == {"a": 2}

    # ceiling breaches unknown -> not a ceiling pass
    unknown_c = _variant({"a": 1}, label="unknown-c", ceiling_weeks=None,
                         harvest_var=0.0)
    assert _rec([unknown_c, known]).overrides == {"a": 2}

    # worst week unknown, with a baseline present -> not a floor pass
    base = _variant({}, label="baseline", min_week=20000.0, harvest_var=0.9)
    unknown_f = _variant({"a": 1}, label="unknown-f", min_week=None,
                         harvest_var=0.0)
    holds = _variant({"a": 2}, label="holds", min_week=25000.0,
                     harvest_var=0.5)
    assert _rec([base, unknown_f, holds]).overrides == {"a": 2}


# --------------------------------------------------------------------------- #
# Stand-down: a guard narrows the pool, it never empties it
# --------------------------------------------------------------------------- #
def test_all_ineligible_still_returns_a_winner_and_says_why():
    """A search must always return its best. When nothing clears a guard the
    guard STANDS DOWN — and the recommendation says so, because a silent
    stand-down is indistinguishable from a clean pass."""
    a = _variant({"a": 1}, label="a", ceiling_weeks=2, zero_weeks=1,
                 harvest_var=0.0)
    b = _variant({"a": 2}, label="b", ceiling_weeks=5, zero_weeks=4,
                 harvest_var=0.9)
    rec = _rec([a, b])
    assert rec.overrides == {"a": 1}                    # still recommends
    assert rec.guards["hard"] == "stood-down"
    assert rec.guards["ceiling"] == "stood-down"
    assert "STOOD DOWN" in rec.text
    assert "empty harvest week" in rec.text
    assert "relief ceiling" in rec.text


def test_the_floor_guard_is_off_without_a_baseline_variant():
    """A seeded coordinate descent labels its seed "seed", so there is no
    baseline to compare against. The guard must stay OFF, not invent one."""
    a = _variant({"a": 1}, label="seed", min_week=100.0, harvest_var=0.0)
    b = _variant({"a": 2}, label="d1", min_week=90000.0, harvest_var=0.9)
    rec = _rec([a, b])
    assert rec.guards["floor"] == "off"
    assert rec.overrides == {"a": 1}                    # emphasis-best stands


def test_an_infeasible_baseline_does_not_become_the_floor():
    """A failed baseline carries the _infeasible_metrics sentinel — a sentinel
    is not a measurement, so it cannot arm the floor guard."""
    base = O.OptVariant("baseline", {}, O._infeasible_metrics(), 0, 0,
                        failed="CANNOT be placed")
    a = _variant({"a": 1}, label="a", min_week=100.0, harvest_var=0.0)
    rec = _rec([base, a])
    assert rec.guards["floor"] == "off"
    assert rec.overrides == {"a": 1}


# --------------------------------------------------------------------------- #
# The operator must be told WHY a different plan won
# --------------------------------------------------------------------------- #
def test_the_excluded_top_candidate_is_named_with_its_reason():
    breach = _variant({"harvest_relief_pct": 0.5}, label="hot",
                      ceiling_weeks=4, harvest_var=0.0)
    clean = _variant({"harvest_relief_pct": 0.1}, label="clean",
                     harvest_var=0.9)
    rec = _rec([breach, clean])
    assert "hot" in rec.text and "EXCLUDED" in rec.text
    assert "relief ceiling in 4 week(s)" in rec.text
    assert rec.guard_notes                       # available un-formatted too


def test_the_floor_exclusion_quotes_both_numbers():
    baseline = _variant({}, label="baseline", min_week=20526.0, harvest_var=0.9)
    regress = _variant({"dev": 0.01}, label="regress", min_week=16185.0,
                       harvest_var=0.0)
    holds = _variant({"dev": 0.005}, label="holds", min_week=21871.0,
                     harvest_var=0.5)
    rec = _rec([baseline, regress, holds])
    assert "16,185" in rec.text and "20,526" in rec.text
    assert "regress" in rec.text


def test_an_ineligible_baseline_never_yields_baseline_stands():
    """`is_capacity_bound` prints "Baseline stands." If a guard threw the
    baseline out, that sentence would recommend an illegal plan."""
    baseline = _variant({}, label="baseline", ceiling_weeks=3, harvest_var=0.0)
    clean = _variant({"a": 1}, label="clean", harvest_var=0.9)
    rec = _rec([baseline, clean])
    assert rec.is_capacity_bound is False
    assert "Baseline stands" not in rec.text
    assert rec.overrides == {"a": 1}


def test_a_clean_search_reports_no_guard_noise():
    """The guards must be invisible when nothing breaks a rule — otherwise
    every normal run cries wolf."""
    baseline = _variant({}, label="baseline", harvest_var=0.9)
    better = _variant({"a": 1}, label="better", harvest_var=0.0)
    rec = _rec([baseline, better])
    assert rec.guard_notes == []
    assert rec.overrides == {"a": 1}
    assert set(rec.guards.values()) == {"applied"}


# --------------------------------------------------------------------------- #
# One implementation, not two
# --------------------------------------------------------------------------- #
def test_the_predicates_are_not_a_second_copy(monkeypatch):
    """Optimize must REUSE tournament's predicates. Disabling them at the
    tournament changes Optimize's answer — proof there is no private duplicate
    quietly enforcing (or one day not enforcing) its own version of the rule."""
    breach = _variant({"a": 1}, label="breach", ceiling_weeks=4,
                      harvest_var=0.0)
    clean = _variant({"a": 2}, label="clean", harvest_var=0.9)
    assert _rec([breach, clean]).overrides == {"a": 2}

    monkeypatch.setattr(T, "ceiling_eligible", lambda vs: list(vs))
    assert _rec([breach, clean]).overrides == {"a": 1}   # the guard came from T

    monkeypatch.setattr(T, "variant_hard_ok", lambda v: True)
    empty = _variant({"a": 3}, label="empty", zero_weeks=2, harvest_var=0.0)
    assert _rec([empty, clean]).overrides == {"a": 3}    # so did the hard gate


def test_optimize_and_the_tournament_agree_on_the_same_pool():
    """Same variants, same baseline: the two front doors must crown the same
    plan. A divergence here is the drift this reuse exists to prevent."""
    baseline = _variant({}, label="baseline", min_week=20000.0, harvest_var=0.9)
    breach = _variant({"a": 1}, label="breach", ceiling_weeks=2,
                      harvest_var=0.0)
    lean = _variant({"a": 2}, label="lean", min_week=9000.0, harvest_var=0.1)
    good = _variant({"a": 3}, label="good", min_week=26000.0, harvest_var=0.5)
    pool = [baseline, breach, lean, good]
    w = {c: 1.0 for c in O.COMPONENTS}
    assert O.recommend(list(pool), weights=w).overrides == {"a": 3}
    assert T.pick_winner(list(pool), w, stock_min_week=20000.0).overrides == {"a": 3}


# --------------------------------------------------------------------------- #
# The APPLY path — the reason any of this matters
# --------------------------------------------------------------------------- #
def _ineligible_search():
    """A search whose best-scoring plan breaches the ceiling AND regresses the
    floor; a slightly worse one is clean."""
    baseline = _variant({}, label="baseline", min_week=20526.0, harvest_var=0.9)
    bad = _variant({"density_target_pct": 0.99}, label="hot", ceiling_weeks=4,
                   min_week=16185.0, harvest_var=0.0)
    good = _variant({"density_target_pct": 0.90}, label="calm",
                    min_week=21871.0, harvest_var=0.5)
    return [baseline, bad, good]


def test_saving_the_winner_cannot_write_ineligible_knobs(tmp_path):
    """End to end: search -> recommend -> save_overrides_to_config. The knob
    that produced the illegal plan must not reach control.yaml."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "control.yaml").write_text(
        yaml.safe_dump({"density_target_pct": 0.85, "max_harvest_per_week": 55000}))

    rec = _rec(_ineligible_search())
    O.save_overrides_to_config(str(cfg), rec.overrides)

    saved = yaml.safe_load((cfg / "control.yaml").read_text())
    assert saved["density_target_pct"] == 0.90          # the eligible winner
    assert saved["density_target_pct"] != 0.99          # never the breach
    assert saved["max_harvest_per_week"] == 55000       # untouched


def test_the_app_apply_path_resolves_the_eligible_winner():
    """app._opt_winner re-finds the winning VARIANT from the recommendation
    (it is what Apply runs and Auto-optimize saves). It must land on the plan
    `recommend` actually chose, not the best-scoring ineligible one."""
    app = pytest.importorskip("app",
                              reason="app.py not importable without Streamlit")
    results = _ineligible_search()
    rec = _rec(results)
    won = app._opt_winner(results, rec)
    assert won.label == "calm"
    assert won.overrides == {"density_target_pct": 0.90}
    assert won.metrics.weeks_over_relief_ceiling == 0
