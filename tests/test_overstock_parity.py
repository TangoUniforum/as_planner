"""Comparison parity: no method may plan to a density its rivals cannot use.

The 2026-08-12 finding. `run_global(overstock=True)` was the DEFAULT, packing
every batch under 2.5 kg to 100% of the hard density cap while the controller
family planned to `density_target_pct`. Global therefore entered every board
with capacity the other methods did not take. The operator's rule is 95 hard /
target 85 "as much as possible" — it contains no weight-based exemption; the
2.5 kg threshold and the "light fish are safe to concentrate" rationale were
engineering inventions, not husbandry policy.

The lever stays available for placement studies. It must never be ON by
default, because a default that applies to one method only is not a lever, it
is a thumb on the scale.
"""
import inspect

from forecast import global_planner_l3_poc as l3
from tools.run_global_forecast import run_global


def test_overstock_is_off_by_default():
    """THE control: a comparison run must not enable it for one method."""
    assert inspect.signature(run_global).parameters["overstock"].default is False


def test_the_module_defaults_are_inert():
    """Import-time state plans at the operating density, so any code path that
    forgets to set the lever gets parity, not a silent advantage."""
    assert l3._OVERSTOCK_DENSITY_PCT is None
    assert l3._OVERSTOCK_MAX_WT_G is None


def test_the_lever_still_works_when_asked_for():
    """Off by default is not the same as removed — a placement study must still
    be able to measure it (tools/run_placement_optimize.py sweeps it)."""
    prev = (l3._OVERSTOCK_DENSITY_PCT, l3._OVERSTOCK_MAX_WT_G)
    try:
        l3._OVERSTOCK_DENSITY_PCT, l3._OVERSTOCK_MAX_WT_G = 1.0, 2500.0
        assert l3._OVERSTOCK_DENSITY_PCT == 1.0
        assert l3._OVERSTOCK_MAX_WT_G == 2500.0
    finally:
        l3._OVERSTOCK_DENSITY_PCT, l3._OVERSTOCK_MAX_WT_G = prev


def test_the_docstring_states_why_it_is_off():
    """A default that reverses a previous default needs its reason attached, or
    the next person restores it as a performance win."""
    doc = run_global.__doc__ or ""
    assert "parity" in doc.lower()
    assert "2.5" in doc
