"""BiologyTables enforces its ascending-by-key invariant on construction.

The lookups in forecast.biology scan and clamp POSITIONALLY, so an
out-of-order row silently flattens a curve onto that row instead of raising.
These tests lock the invariant at the one choke point every entry path shares
(YAML load, Excel template import, the app's biology grid, VBA migration).
"""
from __future__ import annotations

import math

from forecast.biology import _feed_type_for_size, _interp, _mortality_weekly_pct
from forecast.models import BiologyTables


def _tables(**kw) -> BiologyTables:
    """A small well-formed table; kwargs override any field."""
    base = dict(
        sgr_size_g=[10.0, 100.0, 1000.0, 5000.0],
        sgr_fw_pct_day=[3.0, 2.0, 1.0, 0.5],
        sgr_sw_pct_day=[3.3, 2.2, 1.1, 0.55],
        fcr_size_g=[10.0, 100.0, 1000.0, 5000.0],
        fcr_by_model={"1.15": [0.8, 1.0, 1.2, 1.4]},
        mortality_week_from_input=[1, 10, 100],
        mortality_pct_weekly=[0.5, 0.2, 0.05],
        feed_types=[(5.0, "Fry"), (500.0, "Grower"), (9000.0, "Optimax")],
        culling=[(30, 2.0), (200, 1.0)],
    )
    base.update(kw)
    return BiologyTables(**base)


class TestGrowthCurveCollapse:
    """The shipped defect: one appended out-of-order row flattened the curve."""

    def test_out_of_order_growth_row_does_not_collapse_sw_sgr(self):
        # An operator adds a missing 50 g point at the BOTTOM of the grid.
        t = _tables(
            sgr_size_g=[10.0, 100.0, 1000.0, 5000.0, 50.0],
            sgr_fw_pct_day=[3.0, 2.0, 1.0, 0.5, 9.9],
            sgr_sw_pct_day=[3.3, 2.2, 1.1, 0.55, 9.9],
        )
        # Without the sort, every fish above 50 g read 9.9 (the appended row).
        assert math.isclose(_interp(5000.0, t.sgr_size_g, t.sgr_sw_pct_day), 0.55)
        assert math.isclose(_interp(1000.0, t.sgr_size_g, t.sgr_sw_pct_day), 1.1)

    def test_growth_payload_co_permutes(self):
        t = _tables(
            sgr_size_g=[1000.0, 10.0, 5000.0, 100.0],
            sgr_fw_pct_day=[1.0, 3.0, 0.5, 2.0],
            sgr_sw_pct_day=[1.1, 3.3, 0.55, 2.2],
        )
        assert t.sgr_size_g == [10.0, 100.0, 1000.0, 5000.0]
        assert t.sgr_fw_pct_day == [3.0, 2.0, 1.0, 0.5]
        assert t.sgr_sw_pct_day == [3.3, 2.2, 1.1, 0.55]

    def test_fcr_columns_co_permute(self):
        t = _tables(
            fcr_size_g=[1000.0, 10.0, 5000.0, 100.0],
            fcr_by_model={"1.15": [1.2, 0.8, 1.4, 1.0],
                          "1.21": [1.3, 0.9, 1.5, 1.1]},
        )
        assert t.fcr_size_g == [10.0, 100.0, 1000.0, 5000.0]
        assert t.fcr_by_model["1.15"] == [0.8, 1.0, 1.2, 1.4]
        assert t.fcr_by_model["1.21"] == [0.9, 1.1, 1.3, 1.5]

    def test_growth_and_fcr_keys_permute_independently(self):
        # The two key columns may diverge; sorting one must not touch the other.
        t = _tables(sgr_size_g=[100.0, 10.0, 1000.0, 5000.0],
                    sgr_fw_pct_day=[2.0, 3.0, 1.0, 0.5],
                    sgr_sw_pct_day=[2.2, 3.3, 1.1, 0.55])
        assert t.fcr_size_g == [10.0, 100.0, 1000.0, 5000.0]
        assert t.fcr_by_model["1.15"] == [0.8, 1.0, 1.2, 1.4]


class TestOtherKeyedTables:
    """Mortality scans, feed brackets and culls are order-sensitive too."""

    def test_mortality_pairs_co_permute(self):
        t = _tables(mortality_week_from_input=[10, 1, 100],
                    mortality_pct_weekly=[0.2, 0.5, 0.05])
        assert t.mortality_week_from_input == [1, 10, 100]
        # Beyond the last row the scan keeps the FINAL row's value.
        assert math.isclose(_mortality_weekly_pct(t, 200), 0.05)

    def test_feed_types_sorted(self):
        t = _tables(feed_types=[(9000.0, "Optimax"), (5.0, "Fry"),
                                (500.0, "Grower")])
        assert [s for s, _ in t.feed_types] == [5.0, 500.0, 9000.0]
        assert _feed_type_for_size(t, 3.0) == "Fry"

    def test_culling_sorted(self):
        t = _tables(culling=[(200, 1.0), (30, 2.0)])
        assert t.culling == [(30, 2.0), (200, 1.0)]


class TestInvariantSafety:
    """The sort must be a no-op on good input and never raise on bad input."""

    def test_already_sorted_is_identity(self):
        t = _tables()
        assert t.sgr_size_g == [10.0, 100.0, 1000.0, 5000.0]
        assert t.sgr_sw_pct_day == [3.3, 2.2, 1.1, 0.55]
        assert t.fcr_by_model["1.15"] == [0.8, 1.0, 1.2, 1.4]
        assert t.feed_types == [(5.0, "Fry"), (500.0, "Grower"),
                                (9000.0, "Optimax")]

    def test_stable_on_duplicate_keys(self):
        t = _tables(sgr_size_g=[100.0, 10.0, 100.0],
                    sgr_fw_pct_day=[2.0, 3.0, 2.5],
                    sgr_sw_pct_day=[2.2, 3.3, 2.6])
        # Both 100.0 rows kept, in their original relative order.
        assert t.sgr_size_g == [10.0, 100.0, 100.0]
        assert t.sgr_fw_pct_day == [3.0, 2.0, 2.5]

    def test_empty_and_ragged_do_not_raise(self):
        BiologyTables()
        # 9 positional args — the form tests/test_lns_placement.py uses.
        BiologyTables([1.0], [1.0], [1.0], [1.0], {}, [1], [1.0], [], [])
        # A short FCR column (the app's editor can round-trip one) is left be.
        t = _tables(fcr_size_g=[1000.0, 10.0, 5000.0, 100.0],
                    fcr_by_model={"1.15": [1.2, 0.8]})
        assert t.fcr_size_g == [10.0, 100.0, 1000.0, 5000.0]
        assert t.fcr_by_model["1.15"] == [1.2, 0.8]
