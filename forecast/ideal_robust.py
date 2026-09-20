"""Judge a rhythm by its NEIGHBOURHOOD, not by one run.

WHY. Ideal reports a winner from a single deterministic run: zero breaches or
not. The planner is mode-discontinuous -- tiny knob nudges swing months by
100+ t -- so a cell can read LEGAL because its neighbours' breaches happened to
fall just outside it. Measured on 2026-09-19, perturbing each zero-breach
winner by +/-1 day and +/-10,000 fish:

    28 d x 175k @ 3,000 t    4 of 8 neighbours legal
    35 d x 200k @ 3,000 t    4 of 8
    42 d x 225k @ 3,000 t    0 of 8   <- legal at exactly one point
    49 d x 200k @ 3,800 t    5 of 8

Not one was robust, and the "best plan" on that list is legal only if the
facility hits its cadence to the day and its batch size to ten thousand fish.
That is not a plan, and the point verdict cannot tell you so.

The same scan found 49 d x 210k legal at 4,973 t -- 18% above the cell the
25k-step grid had named the winner. Searching coarsely and then judging the
neighbourhood finds what the grid steps over.

This module is the JUDGING half only: build the neighbourhood, and read a
verdict from results someone else ran. It never runs the engine, so it is
testable without one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

# One operational step in each axis: a day of cadence, ten thousand smolt.
# These are what a facility can miss by, not statistical choices.
CADENCE_STEP_DAYS = 1
SIZE_STEP_FISH = 10_000

ROBUST, MIXED, KNIFE_EDGE, INFEASIBLE = "ROBUST", "MIXED", "KNIFE-EDGE", "INFEASIBLE"


@dataclass(frozen=True)
class Cell:
    cadence_days: int
    batch_size: int
    cap_kg: float


@dataclass
class Verdict:
    centre: Cell
    status: str
    centre_hog_t: float = 0.0
    # HOG you still get if the facility drifts one step in any single axis and
    # stays legal. None when a drift makes it illegal -- that is the number a
    # planner should carry, not the centre's.
    robust_hog_t: Optional[float] = None
    axis_legal: int = 0                # of the 4 one-step-in-one-axis cells
    neighbours_legal: int = 0          # of all 8
    detail: dict = field(default_factory=dict)


def neighbourhood(cell: Cell, *, cadence_step: int = CADENCE_STEP_DAYS,
                  size_step: int = SIZE_STEP_FISH,
                  min_cadence: int = 7, min_size: int = 1000) -> list[Cell]:
    """The 8 cells around `cell` (cadence and size; the cap is held).

    The cap is deliberately NOT perturbed: it is a decision the operator makes
    and holds, while cadence and batch size are what execution misses by.
    """
    out = []
    for dc in (-cadence_step, 0, cadence_step):
        for ds in (-size_step, 0, size_step):
            if dc == 0 and ds == 0:
                continue
            c, s = cell.cadence_days + dc, cell.batch_size + ds
            if c < min_cadence or s < min_size:
                continue
            out.append(Cell(c, s, cell.cap_kg))
    return out


def _axis_cells(cell: Cell, nb: Iterable[Cell]) -> list[Cell]:
    """The 4 that differ in exactly ONE axis -- a miss in cadence OR in size,
    which is what actually happens. A simultaneous miss in both is a worse day
    and is reported in `neighbours_legal`, not held against the rhythm."""
    return [n for n in nb
            if (n.cadence_days == cell.cadence_days) != (n.batch_size == cell.batch_size)]


def judge(cell: Cell, results: dict) -> Verdict:
    """`results` maps Cell -> {"legal": bool, "hog_t": float}; a missing
    neighbour counts as not legal (unknown is never evidence of safety)."""
    centre = results.get(cell)
    if centre is None or not centre.get("legal"):
        return Verdict(cell, INFEASIBLE,
                       centre_hog_t=float((centre or {}).get("hog_t") or 0.0))
    nb = neighbourhood(cell)
    axis = _axis_cells(cell, nb)
    ax_ok = [n for n in axis if (results.get(n) or {}).get("legal")]
    all_ok = [n for n in nb if (results.get(n) or {}).get("legal")]
    if len(ax_ok) == len(axis) and axis:
        status = ROBUST
    elif not ax_ok:
        status = KNIFE_EDGE
    else:
        status = MIXED
    hog = float(centre.get("hog_t") or 0.0)
    robust = (min([hog] + [float(results[n]["hog_t"]) for n in ax_ok])
              if status == ROBUST else None)
    return Verdict(cell, status, centre_hog_t=hog, robust_hog_t=robust,
                   axis_legal=len(ax_ok), neighbours_legal=len(all_ok),
                   detail={"axis_total": len(axis), "neighbours_total": len(nb)})


def rank(verdicts: Iterable[Verdict]) -> list[Verdict]:
    """Best first: robust rhythms before fragile ones, and within ROBUST by the
    tonnage that SURVIVES a drift rather than the centre's headline."""
    order = {ROBUST: 0, MIXED: 1, KNIFE_EDGE: 2, INFEASIBLE: 3}
    return sorted(verdicts, key=lambda v: (
        order[v.status],
        -(v.robust_hog_t if v.robust_hog_t is not None else v.centre_hog_t)))
