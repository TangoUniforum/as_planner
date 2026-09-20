"""Neighbourhood judging — pinned to the rhythms it was built from.

Measured 2026-09-19: none of the four zero-breach "winners" survived a +/-1 day,
+/-10,000 fish perturbation, and one (42 d x 225k @ 3,000 t) was legal at
exactly one point. The point verdict Ideal reports today cannot say that.
"""
from __future__ import annotations

import pytest

from forecast.ideal_robust import (Cell, INFEASIBLE, KNIFE_EDGE, MIXED, ROBUST,
                                   judge, neighbourhood, rank)

C = Cell(42, 225_000, 3.0e6)


def _res(legal_cells, hog=5000.0, centre_legal=True, centre=C):
    out = {centre: {"legal": centre_legal, "hog_t": hog}}
    for c in legal_cells:
        out[c] = {"legal": True, "hog_t": hog}
    return out


def test_the_neighbourhood_is_one_operational_step_in_each_axis():
    nb = neighbourhood(C)
    assert len(nb) == 8 and C not in nb
    assert {n.cadence_days for n in nb} == {41, 42, 43}
    assert {n.batch_size for n in nb} == {215_000, 225_000, 235_000}
    assert {n.cap_kg for n in nb} == {3.0e6}, "the cap is held, not perturbed"


def test_the_cap_is_not_perturbed():
    """The cap is a decision the operator makes and holds; cadence and batch
    size are what execution misses by."""
    assert all(n.cap_kg == C.cap_kg for n in neighbourhood(C))


def test_a_rhythm_legal_in_every_direction_is_robust():
    axis = [Cell(41, 225_000, 3.0e6), Cell(43, 225_000, 3.0e6),
            Cell(42, 215_000, 3.0e6), Cell(42, 235_000, 3.0e6)]
    v = judge(C, _res(axis))
    assert v.status == ROBUST and v.axis_legal == 4


def test_the_knife_edge_this_was_built_for():
    """42 d x 225k @ 3,000 t: legal at the point, every neighbour breaching."""
    v = judge(C, _res([]))
    assert v.status == KNIFE_EDGE
    assert v.axis_legal == 0 and v.neighbours_legal == 0
    assert v.robust_hog_t is None, "a knife-edge has no tonnage you can hold"


def test_partly_legal_is_mixed_not_robust():
    v = judge(C, _res([Cell(41, 225_000, 3.0e6), Cell(42, 215_000, 3.0e6)]))
    assert v.status == MIXED and v.axis_legal == 2


def test_an_illegal_centre_is_infeasible_whatever_its_neighbours_do():
    axis = [Cell(41, 225_000, 3.0e6), Cell(43, 225_000, 3.0e6),
            Cell(42, 215_000, 3.0e6), Cell(42, 235_000, 3.0e6)]
    v = judge(C, _res(axis, centre_legal=False))
    assert v.status == INFEASIBLE


def test_an_unrun_neighbour_is_never_evidence_of_safety():
    """A missing result must not be read as legal — that would turn an
    unfinished scan into a robust verdict."""
    v = judge(C, {C: {"legal": True, "hog_t": 5000.0}})
    assert v.status == KNIFE_EDGE


def test_robust_tonnage_is_the_worst_you_can_drift_into():
    """The number a planner should carry is what survives a one-step miss, not
    the centre's headline."""
    res = {C: {"legal": True, "hog_t": 6000.0}}
    for c, h in ((Cell(41, 225_000, 3.0e6), 5800.0),
                 (Cell(43, 225_000, 3.0e6), 5500.0),
                 (Cell(42, 215_000, 3.0e6), 5900.0),
                 (Cell(42, 235_000, 3.0e6), 6100.0)):
        res[c] = {"legal": True, "hog_t": h}
    v = judge(C, res)
    assert v.status == ROBUST
    assert v.centre_hog_t == 6000.0
    assert v.robust_hog_t == 5500.0


def test_a_diagonal_miss_does_not_condemn_a_rhythm():
    """Missing cadence AND size at once is a worse day than the rhythm should
    be judged on; it is reported, not held against it."""
    axis = [Cell(41, 225_000, 3.0e6), Cell(43, 225_000, 3.0e6),
            Cell(42, 215_000, 3.0e6), Cell(42, 235_000, 3.0e6)]
    v = judge(C, _res(axis))
    assert v.status == ROBUST
    assert v.neighbours_legal == 4 and v.detail["neighbours_total"] == 8


def test_ranking_puts_a_holdable_plan_above_a_bigger_fragile_one():
    """The whole point: a 6,040 t knife-edge must not outrank a 5,000 t rhythm
    the facility can actually hold."""
    big = Cell(28, 175_000, 3.0e6)
    fragile = judge(big, {big: {"legal": True, "hog_t": 6040.0}})
    axis = [Cell(41, 225_000, 3.0e6), Cell(43, 225_000, 3.0e6),
            Cell(42, 215_000, 3.0e6), Cell(42, 235_000, 3.0e6)]
    solid = judge(C, _res(axis, hog=5000.0))
    assert [v.centre.batch_size for v in rank([fragile, solid])] == [225_000, 175_000]


def test_the_neighbourhood_never_proposes_an_impossible_cell():
    v = neighbourhood(Cell(7, 1000, 3.0e6))
    assert all(n.cadence_days >= 7 and n.batch_size >= 1000 for n in v)
