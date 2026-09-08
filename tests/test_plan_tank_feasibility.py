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
    # batch_id mirrors the real BatchWeekFact (precalc.py:131) and the first
    # half of the dict key. _relieve_tank_supply needs it: it is the unique
    # third term that makes the pick independent of input order (2026-09-08).
    def __init__(self, need, base, stage="SW", batch_id="B1"):
        self.tanks_needed_at_density_cap = need
        self.tanks_needed_base = base
        self.stage = stage
        self.batch_id = batch_id


class _BN:
    def __init__(self, week, deficit, kind="tank_supply"):
        self.week_label = week
        self.deficit = deficit
        self.kind = kind


def test_off_by_default():
    assert ControlParams.__dataclass_fields__["plan_tank_feasibility"].default is False


def test_releases_exactly_the_deficit():
    a, b = _Fact(5, 3, batch_id="B1"), _Fact(4, 2, batch_id="B2")
    n = _relieve_tank_supply({("B1", "W1"): a, ("B2", "W1"): b}, [_BN("W1", 2)])
    assert n == 2
    assert (a.tanks_needed_at_density_cap + b.tanks_needed_at_density_cap) == 7


def test_takes_the_deepest_reservation_first():
    deep = _Fact(6, 2, batch_id="B1")            # slack 4
    shallow = _Fact(4, 3, batch_id="B2")         # slack 1
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
    w1, w2 = _Fact(5, 1, batch_id="B1"), _Fact(5, 1, batch_id="B1")
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


# ---- Determinism (2026-09-08) ----
# This function was the landing site of the engine's reproducibility bug. Its
# key was two integers -- reservation depth, then claim size -- which tie
# constantly, and max() then returned whichever tied fact the pool happened to
# hold first. That order came from batch_week_facts, whose insertion order was
# hash-seed dependent, so ONE tied pick at 2026-W24 moved a free tank between
# B42 and B43 and diverged every downstream week (105 over-cap rows/170.0
# kg/m3 vs 90/162.9). The upstream set iteration is now sorted (run.py), and
# this pick no longer depends on input order at all.

def test_a_tie_is_broken_by_batch_id_not_by_input_order():
    """Same facts, opposite insertion order -> the same batch surrenders."""
    def _pick(order):
        facts = {(bid, "W1"): _Fact(7, 6, batch_id=bid) for bid in order}
        _relieve_tank_supply(facts, [_BN("W1", 1)])
        return {bid: f.tanks_needed_at_density_cap for (bid, _w), f in facts.items()}

    forward = _pick(["B42", "B43"])
    reverse = _pick(["B43", "B42"])
    assert forward == reverse, (
        f"pick depends on input order: {forward} vs {reverse}")
    # and it is the LOWEST batch_id that gives the reservation back
    assert forward["B42"] == 6 and forward["B43"] == 7


def test_deeper_reservation_still_outranks_the_batch_id_tiebreak():
    """The tiebreak is the LAST term, never a priority in its own right."""
    shallow_low = _Fact(4, 3, batch_id="B1")     # slack 1, lowest id
    deep_high = _Fact(6, 2, batch_id="B9")       # slack 4
    _relieve_tank_supply({("B1", "W1"): shallow_low, ("B9", "W1"): deep_high},
                         [_BN("W1", 1)])
    assert deep_high.tanks_needed_at_density_cap == 5   # depth wins
    assert shallow_low.tanks_needed_at_density_cap == 4
