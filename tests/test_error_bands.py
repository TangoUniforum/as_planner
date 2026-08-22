"""Confidence-band arithmetic — the inversion, and the refusals.

Two things here are worth a test rather than a comment.

THE INVERSION is the easy thing to get backwards, and getting it backwards is
invisible: the band still renders, still looks plausible, and is wrong in the
direction that flatters the plan. error = (predicted - actual) / actual, so
actual = predicted / (1 + err). A HIGH error means the model ran HOT, so the
p90 ERROR must produce the LOW tonnage edge. The bounds cross over.

THE REFUSALS matter as much as the numbers. A band that appears where the
measurement cannot support one is worse than no band, because it reads as
authoritative. So: no model, a horizon past the quotable window, a horizon the
model flags `weak` — all must return None, not a guess.
"""
from __future__ import annotations

import datetime as dt

from forecast.error_bands import (QUOTABLE_MAX_MONTHS, apply_band,
                                  band_for_horizon, describe, load_error_model,
                                  months_between)

# A model shaped like tools/error_model.py's output. Horizon 2 is deliberately
# ASYMMETRIC so a symmetric-band bug cannot pass, and horizon 3 is `weak`.
MODEL = {
    "batches_used": 500,
    "batches_excluded_exec_confounded": 40,
    "horizons_months": {
        "1": {"n": 100, "weak": False, "median_signed_pct": 0.0,
              "p10_pct": -10.0, "p90_pct": 10.0, "typical_abs_pct": 5.0},
        "2": {"n": 90, "weak": False, "median_signed_pct": -3.0,
              "p10_pct": -20.0, "p90_pct": 5.0, "typical_abs_pct": 8.0},
        "3": {"n": 2, "weak": True, "median_signed_pct": -5.0,
              "p10_pct": -30.0, "p90_pct": 5.0, "typical_abs_pct": 12.0},
    },
}


class TestInversion:
    def test_high_error_gives_the_LOW_edge(self):
        """p90 = +10% means the model ran hot, so actual lands BELOW plan."""
        lo, hi = apply_band(110.0, band_for_horizon(MODEL, 1))
        # 110 / 1.10 == 100 exactly; the +10% error is the LOW edge.
        assert round(lo, 6) == 100.0
        # 110 / 0.90 == 122.2; the -10% error is the HIGH edge.
        assert round(hi, 4) == round(110.0 / 0.9, 4)
        assert lo < 110.0 < hi

    def test_asymmetric_band_stays_asymmetric(self):
        """A symmetric-band bug would put the plan value at the midpoint."""
        v = 100.0
        lo, hi = apply_band(v, band_for_horizon(MODEL, 2))
        assert round(lo, 4) == round(v / 1.05, 4)     # p90 = +5%
        assert round(hi, 4) == round(v / 0.80, 4)     # p10 = -20%
        mid = (lo + hi) / 2.0
        assert abs(mid - v) > 5.0, "band collapsed to symmetric about the plan"

    def test_low_is_always_below_high(self):
        for h in (1, 2):
            lo, hi = apply_band(500.0, band_for_horizon(MODEL, h))
            assert lo < hi


class TestRefusals:
    def test_no_model_returns_none(self):
        assert band_for_horizon(None, 1) is None
        assert apply_band(100.0, None) is None

    def test_horizon_past_the_quotable_window_is_refused(self):
        far = QUOTABLE_MAX_MONTHS + 1
        assert band_for_horizon(MODEL, far) is None

    def test_weak_horizon_is_refused(self):
        """Horizon 3 has n=2 and weak=True: a spread from two samples is not a
        distribution, and showing it would look more certain than it is."""
        assert MODEL["horizons_months"]["3"]["weak"] is True
        assert band_for_horizon(MODEL, 3) is None

    def test_in_month_and_negative_horizons_are_refused(self):
        assert band_for_horizon(MODEL, 0) is None
        assert band_for_horizon(MODEL, -1) is None

    def test_missing_percentiles_are_refused(self):
        broken = {"horizons_months": {"1": {"n": 50, "weak": False,
                                            "p10_pct": None, "p90_pct": 5.0}}}
        assert band_for_horizon(broken, 1) is None


class TestHorizonClock:
    def test_months_between_counts_calendar_months(self):
        assert months_between(dt.date(2026, 8, 1), dt.date(2026, 9, 1)) == 1
        assert months_between(dt.date(2026, 8, 1), dt.date(2027, 2, 1)) == 6
        assert months_between(dt.date(2026, 8, 1), dt.date(2026, 8, 28)) == 0


class TestDescribe:
    def test_absence_is_stated_not_hidden(self):
        assert "No measured error model" in describe(None)

    def test_describe_names_the_evidence_and_the_exclusions(self):
        d = describe(MODEL)
        assert "500" in d and "40" in d


class TestRealModelIfPresent:
    def test_committed_model_loads_and_bands_sensibly(self):
        """The repo ships a measured model beside the corpus. If it is there it
        must load and produce a sane band; if it is not, that is legal (a clone
        that has never run a backtest) and must not fail."""
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        m = load_error_model(root)
        if m is None:
            return
        assert m.get("horizons_months")
        b = band_for_horizon(m, 1)
        if b is None:
            return
        lo, hi = apply_band(1000.0, b)
        assert 0 < lo < hi
        # A 1-month band wider than +/-50% would mean the model is not usable;
        # catch that here rather than on a slide in front of finance.
        assert lo > 500.0 and hi < 2000.0
