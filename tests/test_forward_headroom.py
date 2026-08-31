"""The balancer must price the week's growth it can MEASURE, not a flat guess.

_BALANCE_TARGET_FRAC 0.88 / _BALANCE_TRIGGER_FRAC 0.92 / _BALANCE_SYS_FILL 0.90
are a flat ~10%/week growth allowance standing in for a projection nobody ran.
placement.py records why that cannot hold: "0.90 x 1.11 ~ 1.0" -- wherever real
growth beats the guess the margin evaporates and the move relocates the breach
instead of preventing it. `rebalance_headroom_days` replaces the guess with each
tank's own SGR.
"""
import math

import pytest

from forecast import placement
from forecast.models import ControlParams


class _Tables:
    """Minimal BiologyTables stand-in: a flat 1%/day SW curve, no week factor."""
    sgr_size_g = [0.0, 100000.0]
    sgr_sw_pct_day = [1.0, 1.0]
    sgr_fw_pct_day = [1.0, 1.0]
    og_sgr_by_week = {}
    fcr_by_model = {}


def test_zero_days_is_the_identity():
    """0 = OFF must be byte-identical, or the knob is not opt-in."""
    w, b = placement._grown_forward(1000.0, 5000.0, None, _Tables(), "2026-W30", 0)
    assert w == 1000.0
    assert b == 5000.0


def test_seven_days_compounds_at_the_tanks_own_rate():
    w, b = placement._grown_forward(1000.0, 5000.0, None, _Tables(), "2026-W30", 7)
    expected_w = 1000.0 * (1.01 ** 7)          # ~1072.1 g
    assert w == pytest.approx(expected_w, rel=1e-9)
    # biomass scales with weight: count is held constant by design
    assert b == pytest.approx(5000.0 * (expected_w / 1000.0), rel=1e-9)


def test_growth_beats_the_flat_margin_it_replaces():
    """The documented failure: a 0.90 fill assumes ~11% growth. At 1%/day the
    week is 7.2%, but the curve is not flat in practice -- the point is that the
    projection is MEASURED, so it can be larger OR smaller than the guess. Here
    it is smaller, which means real headroom the flat margin was throwing away."""
    _w, b = placement._grown_forward(1000.0, 100.0, None, _Tables(), "2026-W30", 7)
    growth = b / 100.0 - 1.0
    assert growth == pytest.approx(0.0721, abs=5e-4)
    flat_allowance = 1.0 - placement._BALANCE_SYS_FILL      # 0.10
    assert growth < flat_allowance, "here the flat margin over-reserves"


def test_the_projected_fill_does_not_double_count_the_margin():
    """Once growth is explicit the flat stand-in must not ALSO apply, or the
    balancer reserves for the same week twice and refuses legal moves."""
    assert placement._BALANCE_SYS_FILL_FWD > placement._BALANCE_SYS_FILL
    assert placement._BALANCE_SYS_FILL_FWD <= 1.0


def test_degenerate_tanks_project_to_themselves():
    for wt, bio in ((0.0, 100.0), (500.0, 0.0), (-1.0, 5.0)):
        w, b = placement._grown_forward(wt, bio, None, _Tables(), "2026-W30", 7)
        assert (w, b) == (wt, bio)


def test_the_knob_defaults_to_off():
    """OFF by default, so an existing config is byte-identical until someone
    opts in. Read the dataclass default -- ControlParams has 13 required fields
    and instantiating it here would test the fixture, not the knob."""
    import dataclasses
    fld = {f.name: f for f in dataclasses.fields(ControlParams)}
    assert fld["rebalance_headroom_days"].default == 0
