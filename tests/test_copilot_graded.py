"""Co-pilot 6N staging semantics — 2026-07-13 audit findings.

DEFECT (MEDIUM): a controller Type='Grade' TransferPlan pickup (top-N-by-size
into 6N) was extracted as a plain 'to_6n' Move, so approval emitted og_to_6n —
moving `count` MEAN-weight fish, losing the size selection (wrong biomass to 6N,
wrong remainder left growing). Grade rows must surface as kind='grade_to_6n'
and approve into a `graded_harvest` manual event.

DEFECT (LOW): `is6n` was SIXN_ALL_TANKS regardless of mode — in PRODUCTION mode
(2028+) the 6N mains are grow-out tanks, and approving a 'to_6n' into one emits
an og_to_6n that freezes a production tank to STARVE.
"""
from __future__ import annotations

from datetime import date

import openpyxl

from forecast import copilot
from forecast.manual_events import TYPE_GRADED_HARVEST, TYPE_OG_TO_6N
from forecast.sixn import SIXN_ALL_TANKS, SIXN_SISTER_TANKS

WK = "2027-W10"


def _plan_wb(rows):
    """A workbook whose TransferPlan matches excel_io.write_transfer_plan_output:
    Week | Batch | Type | From_Tank | To_Tank | Count | Avg_Weight | Grade | CV."""
    wb = openpyxl.Workbook()
    ws = wb.create_sheet("TransferPlan")
    ws.append(["TRANSFER PLAN"])
    ws.append(["blurb"])
    ws.append([])
    ws.append(["Week", "Batch", "Type", "From_Tank", "To_Tank",
               "Count (fish)", "Avg_Weight (kg)", "Grade", "CV (%)"])
    for r in rows:
        ws.append(r)
    return wb


TANK_SYS = {31: "OG3N", 32: "OG3N", 61: "OG6N", 67: "OG6N"}
TANK_LOC = {31: "OG3N-1", 32: "OG3N-2", 61: "6N-61", 67: "6N-67"}


def test_grade_pickup_surfaces_as_grade_to_6n_with_retention():
    wb = _plan_wb([
        # Graded 6N staging: biggest 4000 fish -> 6N tank 61, remainder -> tank 32.
        [WK, "B7", "Grade", "31", 61, 4000, 5.2, "pickup", 12.0],
        [WK, "B7", "Grade", "31", 32, 6000, 4.1, "retention", 12.0],
        # A plain whole-tank staging move stays a plain to_6n.
        [WK, "B8", "Transfer", 32, 67, 9000, 4.8, None, None],
    ])
    moves = copilot._extract_transfers(
        wb, WK, TANK_SYS, TANK_LOC, set(SIXN_ALL_TANKS),
        only_to_6n=True, engine="controller")
    kinds = {m.kind for m in moves}
    assert kinds == {"grade_to_6n", "to_6n"}
    g = next(m for m in moves if m.kind == "grade_to_6n")
    assert (g.from_tank, g.to_tank, g.count) == (31, 61, 4000.0)
    assert g.retention_tank == 32, "retention leg must ride with the pickup"
    assert g.avg_wt_kg == 5.2, "pickup weight is the GRADED (top-N) weight"
    # The retention leg itself must not appear as a second move.
    assert len(moves) == 2


def test_grade_rows_never_leak_into_og_transfer_picks():
    """An approved retention leg as og_transfer would also move mean-weight fish."""
    wb = _plan_wb([
        [WK, "B7", "Grade", "31", 61, 4000, 5.2, "pickup", 12.0],
        [WK, "B7", "Grade", "31", 32, 6000, 4.1, "retention", 12.0],
        [WK, "B9", "Transfer", 31, 32, 500, 3.0, None, None],
    ])
    moves = copilot._extract_transfers(
        wb, WK, TANK_SYS, TANK_LOC, set(SIXN_ALL_TANKS),
        only_to_6n=False, engine="global-lp")
    assert [m.kind for m in moves] == ["og_transfer"]
    assert moves[0].batch == "B9"


def test_grade_to_6n_approves_into_a_graded_harvest_event():
    m = copilot.Move(kind="grade_to_6n", engine="controller", priority=2,
                     from_tank=31, to_tank=61, from_loc="OG3N-1", to_loc="6N-61",
                     batch="B7", count=4000.0, avg_wt_kg=5.2, note="",
                     retention_tank=32)
    ev, = copilot.to_manual_events([m], window_week=3)
    assert ev.type == TYPE_GRADED_HARVEST
    assert ev.from_tank == 31 and ev.count == 4000.0 and ev.week == 3
    assert [d.tank for d in ev.destinations] == [61, 32]

    # Remainder stays in the source -> destinations carry only the 6N pickup
    # (_apply_graded_harvest defaults retention to the source tank).
    m2 = copilot.Move(kind="grade_to_6n", engine="controller", priority=2,
                      from_tank=31, to_tank=61, from_loc="OG3N-1", to_loc="6N-61",
                      batch="B7", count=4000.0, avg_wt_kg=5.2, note="",
                      retention_tank=31)
    ev2, = copilot.to_manual_events([m2], window_week=3)
    assert ev2.type == TYPE_GRADED_HARVEST
    assert [d.tank for d in ev2.destinations] == [61]

    # Plain staging still approves as og_to_6n with the count on the DEST.
    m3 = copilot.Move(kind="to_6n", engine="controller", priority=2,
                      from_tank=32, to_tank=67, from_loc="OG3N-2", to_loc="6N-67",
                      batch="B8", count=9000.0, avg_wt_kg=4.8, note="")
    ev3, = copilot.to_manual_events([m3], window_week=3)
    assert ev3.type == TYPE_OG_TO_6N
    assert ev3.destinations[0].tank == 67 and ev3.destinations[0].count == 9000.0


class _Ctl:
    def __init__(self, growth=False, prod_start=None):
        self.sixn_growth = growth
        self.sixn_production_start = prod_start


def test_6n_staging_set_shrinks_to_sisters_in_production_mode():
    """DEFECT: is6n was ALL 6N tanks in every mode — in production mode a move
    into a 6N MAIN (a grow-out production tank, 2028+) was classed 'to_6n' and
    approval froze the tank to STARVE. Mains are staging only in purge mode."""
    ctl = _Ctl(growth=False, prod_start=date(2028, 1, 3))
    assert copilot._staging_6n_tanks(ctl, date(2027, 6, 1)) == set(SIXN_ALL_TANKS)
    assert copilot._staging_6n_tanks(ctl, date(2028, 6, 1)) == set(SIXN_SISTER_TANKS)
    # sixn_growth=True = production immediately (no purge model at all).
    assert (copilot._staging_6n_tanks(_Ctl(growth=True), date(2026, 1, 1))
            == set(SIXN_SISTER_TANKS))


def test_production_mode_move_into_a_main_is_not_to_6n():
    wb = _plan_wb([[WK, "B8", "Transfer", 32, 61, 9000, 4.8, None, None]])
    prod_is6n = copilot._staging_6n_tanks(
        _Ctl(growth=False, prod_start=date(2027, 1, 1)), date(2027, 3, 8))
    assert copilot._extract_transfers(
        wb, WK, TANK_SYS, TANK_LOC, prod_is6n,
        only_to_6n=True, engine="controller") == []
    # ...and it surfaces instead as a REGULAR relocation for the transfer lens.
    moves = copilot._extract_transfers(
        wb, WK, TANK_SYS, TANK_LOC, prod_is6n,
        only_to_6n=False, engine="global-lp")
    assert [m.kind for m in moves] == ["og_transfer"]
