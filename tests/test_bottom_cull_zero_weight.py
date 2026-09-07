"""A cull asked to remove 79,788 fish must not remove none and report success.

`_apply_bottom_cull` ranks fish by weight and trims the bottom. Its guard read
`if cull_pct <= 0 or count <= 0 or avg_wt <= 0: return count, avg_wt, 0.0, 0.0`
-- so a batch whose projected weight is zero got its cull SILENTLY SKIPPED,
returning "culled 0.0" with no warning.

That is not hypothetical. On the 2026-08-31 PR, B56's freshwater growth
projected an average weight of 0.00 g (Diagnostics: target 370 g, residual
-100%, "FW correction did not converge"). The TranOG reconcile then asked to
cull 79,788 fish to land on tran_og_count=330,000, culled none, and 409,671
fish entered seawater -- +24% over plan, surfacing only as an FW-divergence
NOTE that read like a survival-model calibration gap rather than a skipped cull.

The count reduction is well defined even when the size distribution is not, and
landing on a count is exactly what the caller asked for, so a zero weight now
culls PROPORTIONALLY. The zero weight itself stays loud via the FW-calibration
warning that fires for the same batch.
"""
from __future__ import annotations

import pytest

from forecast.biology import _apply_bottom_cull

_TARGET = 330_000.0
_START = 409_788.07
_PCT = 1.0 - _TARGET / _START


@pytest.mark.parametrize("avg_wt", [300.0, 50.0, 1.0, 0.0])
def test_the_cull_lands_on_the_target_count_at_any_weight(avg_wt):
    """The whole point of the reconcile: land on tran_og_count."""
    n, _w, culled, _b = _apply_bottom_cull(_START, avg_wt, 16.0, _PCT)
    assert n == pytest.approx(_TARGET, abs=1.0)
    assert culled == pytest.approx(_START - _TARGET, abs=1.0)


def test_zero_weight_no_longer_silently_culls_nobody():
    """The regression itself. On the parent commit this returned (409788, 0)."""
    n, _w, culled, _b = _apply_bottom_cull(_START, 0.0, 16.0, _PCT)
    assert culled > 0, "zero-weight cull removed nobody and reported success"
    assert n < _START


def test_zero_weight_culls_no_biomass():
    """There is no biomass to remove when the fish weigh nothing -- the count
    is corrected, the biomass stays 0, and the ledger still balances."""
    _n, _w, _c, bio = _apply_bottom_cull(_START, 0.0, 16.0, _PCT)
    assert bio == 0.0


def test_a_positive_weight_still_takes_the_SMALLEST_fish():
    """Unchanged behaviour where the distribution is usable: the survivors'
    mean must RISE, because the bottom of the distribution left."""
    _n, new_wt, _c, _b = _apply_bottom_cull(_START, 300.0, 16.0, _PCT)
    assert new_wt > 300.0


def test_the_degenerate_guards_still_short_circuit():
    assert _apply_bottom_cull(1000.0, 0.0, 16.0, 0.0)[2] == 0.0     # nothing asked
    assert _apply_bottom_cull(0.0, 0.0, 16.0, 0.5)[2] == 0.0        # nobody there
    assert _apply_bottom_cull(1000.0, 0.0, 16.0, 1.0)[0] == 0.0     # cull everyone
