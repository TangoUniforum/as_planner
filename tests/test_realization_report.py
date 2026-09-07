"""The intent check: did what the planner decided actually happen?

Every other check verifies conservation (nothing lost) or an outcome (floors,
empty weeks, caps, handling budget). A move the planner emitted and the engine
refused passes all of them -- it is perfectly conservative, trips no gate, does
not consume the handling budget (which counts APPLIED pairs), and TransferPlan
deliberately drops it as "not the actionable plan". That is how 528 refusals on
the live config went unnoticed while every gate stayed green.

These tests pin the classification, the repeat-collapsing that turns 528 lines
into 73 facts, and that a refusal actually records WHY.
"""
from __future__ import annotations

from datetime import date

from openpyxl import Workbook

from forecast.events import Transfer
from forecast.excel_io import write_realization_report
from forecast.placement import TankAllocation
from forecast.state import FacilityState, TankState

D1 = date(2026, 11, 11)
D2 = date(2026, 11, 18)


def _mv(src, dst, count, batch="B45", when=D1, moved=None, reason=None, detail=None):
    ev = Transfer(
        batch_id=batch, event_date=when, source_tank_id=src,
        destinations=[TankAllocation(tank_id=dst, count=count,
                                     avg_wt_g=3100.0, cv_pct=16.0)],
    )
    ev.count_transferred = count if moved is None else moved
    ev.refusal_reason = reason
    ev.refusal_detail = detail
    return ev


def _sheet(events):
    wb = Workbook()
    write_realization_report(wb, events)
    return [[c.value for c in r] for r in wb["RealizationReport"].iter_rows()]


def _summary(rows):
    out = {}
    for r in rows:
        if r and r[0] and r[1] is not None and not str(r[0]).startswith("20"):
            out[str(r[0]).strip()] = r[1]
    return out


def _stuck(rows):
    i = next(k for k, r in enumerate(rows) if r and r[0] == "Batch")
    return [r for r in rows[i + 1:] if r and r[0] is not None]


def test_classifies_applied_partial_and_refused():
    s = _summary(_sheet([
        _mv(31, 41, 100.0),                                    # full
        _mv(32, 42, 100.0, moved=60.0),                        # partial
        _mv(33, 43, 100.0, moved=0.0, reason="source_holds_other_batch",
            detail="B47"),                                     # refused
    ]))
    assert s["Transfer events emitted"] == 3
    assert s["... applied in full"] == 1
    assert s["... applied in part"] == 1
    assert s["... refused whole"] == 1


def test_reports_the_share_of_intent_that_happened():
    s = _summary(_sheet([_mv(31, 41, 100.0), _mv(32, 42, 100.0, moved=0.0,
                                                 reason="source_holds_other_batch")]))
    assert s["Fish the planner planned to move"] == 200
    assert s["Fish that stayed put"] == 100
    assert s["Share of planned movement realized"] == "50.0%"


def test_repeats_collapse_to_one_fact_with_a_span():
    """528 lines are 73 facts. Without this the sheet is just the log again."""
    evs = [_mv(33, 43, 10.0, when=w, moved=0.0,
               reason="source_holds_other_batch", detail="B47")
           for w in (D1, D1, D2)]
    rows = _stuck(_sheet(evs))
    assert len(rows) == 1
    b, tank, reason, found, occ, first, last, fish = rows[0][:8]
    assert (tank, reason, found, occ) == (33, "source_holds_other_batch", "B47", 3)
    assert first == "2026-W46" and last == "2026-W47"
    assert fish == 30


def test_distinct_reasons_do_not_merge():
    rows = _stuck(_sheet([
        _mv(33, 43, 10.0, moved=0.0, reason="source_holds_other_batch", detail="B47"),
        _mv(33, 43, 10.0, moved=0.0, reason="r7_sixn_one_way", detail="OG6N"),
    ]))
    assert len(rows) == 2


def test_applied_moves_never_appear_as_stuck():
    assert _stuck(_sheet([_mv(31, 41, 100.0), _mv(32, 42, 100.0, moved=60.0)])) == []


def test_graded_harvest_events_are_skipped():
    class _GH:                    # rides in transfer_events without .destinations
        event_date = D1
        batch_id = "B45"
        pickup_tank_id = 61
    s = _summary(_sheet([_mv(31, 41, 100.0), _GH()]))
    assert s["Transfer events emitted"] == 1


def test_apply_records_why_it_refused():
    """The reason must come from the engine, not be inferred by the report."""
    st = FacilityState(D1, [
        TankState("OG3N-31", 31, "OG3N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG4N-41", 41, "OG4N", 1000.0, 95.0, 1000.0, "OG"),
    ])
    st.tanks_by_id[31].assign(batch_id="B47", count=5000.0,
                              avg_wt_g=3100.0, cv_pct=16.0, stage="SW")
    ev = Transfer(
        batch_id="B45", event_date=D1, source_tank_id=31,
        destinations=[TankAllocation(tank_id=41, count=100.0,
                                     avg_wt_g=3100.0, cv_pct=16.0)],
    )
    warns = ev.apply(st)
    assert warns and "expected B45" in warns[0]
    assert ev.refusal_reason == "source_holds_other_batch"
    assert ev.refusal_detail == "B47"
    assert ev.count_transferred == 0.0
