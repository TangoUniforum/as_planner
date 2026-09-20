"""Screening runs rank; they never judge.

Measured on ten cells at both horizons: Spearman 0.988 on tonnage ranking, but
the LEGAL/not verdict agreed on only 4 of 10 — cells legal over 156 weeks read
as 1-7 breach-weeks over 104, because year 2 is still start-up.
"""
from __future__ import annotations

import pytest

from forecast.ideal_screen import (SCREEN_HORIZON_WEEKS, ScreenResult,
                                   describe, finalists, screening_saving)


def _r(cad, size, hog, cap=3.0e6):
    return ScreenResult(cad, size, cap, hog)


def test_a_screening_result_has_no_verdict_to_read():
    """Structural, not a convention: there is no `legal` attribute, so nobody
    can accidentally trust a cheap run's legality."""
    r = _r(42, 225_000, 5_000.0)
    assert not hasattr(r, "legal")
    assert not hasattr(r, "breaches")
    assert not hasattr(r, "within_limits")


def test_a_full_fidelity_run_cannot_masquerade_as_a_screen():
    with pytest.raises(ValueError, match="not a screening run"):
        ScreenResult(42, 225_000, 3.0e6, 5_000.0, horizon_weeks=156)


def test_finalists_are_the_best_ranked_cells():
    rs = [_r(42, 225_000, 5_400), _r(28, 175_000, 6_080),
          _r(49, 200_000, 4_085), _r(35, 200_000, 5_667)]
    assert [f.batch_size for f in finalists(rs, k=2)] == [175_000, 200_000]


def test_a_screen_is_reproducible_when_cells_tie():
    """Equal screened tonnage must not swap places between runs."""
    rs = [_r(49, 200_000, 5_000), _r(42, 225_000, 5_000), _r(42, 215_000, 5_000)]
    once = [(f.cadence_days, f.batch_size) for f in finalists(rs, k=3)]
    twice = [(f.cadence_days, f.batch_size) for f in finalists(list(reversed(rs)), k=3)]
    assert once == twice == [(42, 215_000), (42, 225_000), (49, 200_000)]


def test_k_must_be_at_least_one():
    with pytest.raises(ValueError):
        finalists([_r(42, 225_000, 1.0)], k=0)


def test_the_two_stage_plan_actually_saves():
    s = screening_saving(n_cells=150, k=5)
    assert s["two_stage_s"] < s["all_full_s"]
    assert 0.3 < s["saving"] < 0.7        # ~50% on the measured per-cell times


def test_screening_at_full_fidelity_saves_nothing():
    """A guard against 'screening' that quietly runs the expensive horizon."""
    s = screening_saving(n_cells=100, screen_seconds=55.0, full_seconds=55.0, k=5)
    assert s["saving"] < 0


def test_the_caveat_travels_with_the_numbers():
    d = describe()
    assert str(SCREEN_HORIZON_WEEKS) in d and "ranking only" in d
    assert "no verdict" in d
