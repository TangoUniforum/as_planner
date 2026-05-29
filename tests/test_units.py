"""Fast unit tests for pure helpers (no pipeline / no workbook)."""
from __future__ import annotations

import math

from forecast.precalc import _fraction_above, _OG12, _OG36, _OG_ALL


class TestFractionAbove:
    """`_fraction_above` = fraction of a normal(avg, avg*cv) above a cut."""

    def test_at_mean_is_half(self):
        # avg_wt == cut → half the distribution is above.
        assert math.isclose(_fraction_above(1000, 1000, 16), 0.5, abs_tol=1e-9)

    def test_well_above_cut_is_near_one(self):
        assert _fraction_above(1000, 5000, 16) > 0.999

    def test_well_below_cut_is_near_zero(self):
        assert _fraction_above(1000, 200, 16) < 0.001

    def test_bounded_unit_interval(self):
        for avg in (0, 50, 500, 1000, 4000, 9000):
            f = _fraction_above(1000, avg, 16)
            assert 0.0 <= f <= 1.0

    def test_zero_cv_is_step(self):
        # No variance → step function at the cut.
        assert _fraction_above(1000, 1200, 0) == 1.0
        assert _fraction_above(1000, 800, 0) == 0.0

    def test_zero_avg_wt_is_zero(self):
        assert _fraction_above(1000, 0, 16) == 0.0


class TestSystemSets:
    """Eligibility-system partitions used by the exit-at-1kg rule."""

    def test_og12_and_og36_disjoint(self):
        assert _OG12.isdisjoint(_OG36)

    def test_union_is_all(self):
        assert _OG12 | _OG36 == _OG_ALL

    def test_og12_is_nursery_systems(self):
        assert _OG12 == {"OG1N", "OG1S", "OG2N", "OG2S"}

    def test_og6n_in_growout_set(self):
        # OG6N is part of the grow-out system set (pipeline-ownership is
        # handled separately by excluding it from the free pool).
        assert "OG6N" in _OG36
