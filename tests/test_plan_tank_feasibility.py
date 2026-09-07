"""The canvas may plan within the tanks that exist, instead of past them.

The canvas detects, weeks ahead, that OG tank demand exceeds placeable supply.
Before this knob that detection was handed to _build_facility_assignment_plan
and only ever APPENDED to -- `bottlenecks` appears there four times, every one
an .append(). The plan was built for 35 tanks in a facility with 33.

The excess is not need: `tanks_needed_at_density_cap` is raised to each batch's
own peak over the next _FOOTPRINT_LOOKAHEAD_WEEKS so a growing batch claims
grow-out early. That is per batch, with nothing arbitrating the sum. Only that
reservation may be handed back, never a batch's need for the week itself.

OFF BY DEFAULT and measured not to win -- see the method blurb. These tests pin
the mechanics, not a verdict.
"""
from __future__ import annotations

import pytest

from forecast.models import ControlParams
from forecast.precalc import _relieve_tank_supply


class _Fact:
    def __init__(self, need, base, stage="SW"):
        self.tanks_needed_at_density_cap = need
        self.tanks_needed_base = base
        self.stage = stage


class _BN:
    def __init__(self, week, deficit, kind="tank_supply"):
        self.week_label = week
        self.deficit = deficit
        self.kind = kind


def test_off_by_default():
    assert ControlParams.__dataclass_fields__["plan_tank_feasibility"].default is False


def test_releases_exactly_the_deficit():
    a, b = _Fact(5, 3), _Fact(4, 2)
    n = _relieve_tank_supply({("B1", "W1"): a, ("B2", "W1"): b}, [_BN("W1", 2)])
    assert n == 2
    assert (a.tanks_needed_at_density_cap + b.tanks_needed_at_density_cap) == 7


def test_takes_the_deepest_reservation_first():
    deep, shallow = _Fact(6, 2), _Fact(4, 3)     # slack 4 vs 1
    _relieve_tank_supply({("B1", "W1"): deep, ("B2", "W1"): shallow},
                         [_BN("W1", 2)])
    assert deep.tanks_needed_at_density_cap == 4     # both taken from the deep one
    assert shallow.tanks_needed_at_density_cap == 4


def test_never_goes_below_a_batchs_own_need():
    """The guard that stops this squeezing a batch into tipping its density."""
    f = _Fact(4, 3)                                  # only 1 tank of slack
    n = _relieve_tank_supply({("B1", "W1"): f}, [_BN("W1", 3)])
    assert n == 1
    assert f.tanks_needed_at_density_cap == 3        # == base, not 1


def test_a_week_with_no_reservation_is_left_alone():
    f = _Fact(3, 3)
    assert _relieve_tank_supply({("B1", "W1"): f}, [_BN("W1", 2)]) == 0
    assert f.tanks_needed_at_density_cap == 3


def test_only_sw_weeks_are_touched():
    fw = _Fact(5, 1, stage="FW")
    assert _relieve_tank_supply({("B1", "W1"): fw}, [_BN("W1", 2)]) == 0
    assert fw.tanks_needed_at_density_cap == 5


def test_other_bottleneck_kinds_are_ignored():
    f = _Fact(5, 1)
    assert _relieve_tank_supply({("B1", "W1"): f},
                                [_BN("W1", 2, kind="facility_feed")]) == 0


def test_only_the_named_week_is_relieved():
    w1, w2 = _Fact(5, 1), _Fact(5, 1)
    _relieve_tank_supply({("B1", "W1"): w1, ("B1", "W2"): w2}, [_BN("W1", 2)])
    assert w1.tanks_needed_at_density_cap == 3
    assert w2.tanks_needed_at_density_cap == 5


def test_registered_as_a_comparable_method():
    from forecast.methods import REGISTRY
    m = REGISTRY["controller-feasible"]
    assert m.overrides["plan_tank_feasibility"] is True
    # the ONE variable vs `controller` must be the feasibility pass
    assert m.overrides["hybrid_follow"] == "off"
    assert m.family == "Controller"
