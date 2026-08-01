"""The recommendation must identify the winner by its KNOBS, not its label.

coordinate_descent names each candidate for the single knob it changed that
step, so the same label recurs across rounds carrying different accumulated
overrides. A by-label lookup returns the earliest match — a round-1 partial
set — which then gets run and (with auto-save on) written to control.yaml in
place of the winning combination.
"""
from __future__ import annotations

from dataclasses import fields as _f

import forecast.optimize as opt


def _metrics(scale=0.0):
    kw = {}
    for fld in _f(opt.Metrics):
        if fld.name in ("transfers_by_type", "per_system"):
            continue
        kw[fld.name] = 0 if fld.name == "weeks_over_harvest_cap" else scale
    return opt.Metrics(**kw)


def _duplicate_label_results():
    """What a 2-round coordinate descent produces: round 1 tries the knob
    alone, round 2 re-tries it on top of an adopted knob — SAME label."""
    round1 = opt.OptVariant("tran_og_default_tanks=2",
                            {"tran_og_default_tanks": 2}, _metrics(0.9), 0, 0)
    adopted = opt.OptVariant("density_target_pct=0.9",
                             {"density_target_pct": 0.9}, _metrics(0.5), 0, 0)
    round2 = opt.OptVariant("tran_og_default_tanks=2",
                            {"density_target_pct": 0.9,
                             "tran_og_default_tanks": 2}, _metrics(0.1), 0, 0)
    return [round1, adopted, round2]


class TestRecommendationCarriesOverrides:

    def test_recommend_returns_the_winner_knobs(self):
        results = _duplicate_label_results()
        rec = opt.recommend(results, emphasis=opt.DEFAULT_EMPHASIS)
        # The round-2 combination wins on score...
        assert rec.best_label == "tran_og_default_tanks=2"
        # ...and the recommendation carries ITS knobs, not the round-1 subset.
        assert rec.overrides == {"density_target_pct": 0.9,
                                 "tran_og_default_tanks": 2}

    def test_by_label_lookup_would_have_picked_the_partial_set(self):
        """Pin the old failure mode so the regression can't creep back."""
        results = _duplicate_label_results()
        rec = opt.recommend(results, emphasis=opt.DEFAULT_EMPHASIS)
        stale = next(v for v in results if v.label == rec.best_label)
        assert stale.overrides == {"tran_og_default_tanks": 2}
        assert stale.overrides != rec.overrides

    def test_seeded_search_without_a_baseline_variant_does_not_raise(self):
        """coordinate_descent labels a non-empty seed "seed", so a recommendation
        built from descent-only results has no "baseline" to compare against."""
        rec = opt.recommend(_duplicate_label_results(),
                            emphasis=opt.DEFAULT_EMPHASIS)
        assert "baseline" not in rec.text
        assert rec.overrides

    def test_no_feasible_variant_yields_empty_overrides(self):
        bad = opt.OptVariant("hot", {"density_target_pct": 0.95},
                             opt._infeasible_metrics(), 0, 0,
                             failed="CANNOT be placed")
        rec = opt.recommend([bad], emphasis=opt.DEFAULT_EMPHASIS)
        assert rec.best_label == "(none)"
        assert rec.overrides == {}          # nothing to apply or save

    def test_baseline_winner_has_no_overrides(self):
        base = opt.OptVariant("baseline", {}, _metrics(0.0), 0, 0)
        hot = opt.OptVariant("hot", {"density_target_pct": 0.95},
                             _metrics(1.0), 0, 0)
        rec = opt.recommend([base, hot], emphasis=opt.DEFAULT_EMPHASIS)
        assert rec.best_label == "baseline"
        assert rec.overrides == {}
