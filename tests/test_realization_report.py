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
    """The TRANSFER stuck table only — stop at the HARVEST section, which has
    its own Batch/Source_Tank header and would otherwise be swept in."""
    end = next((k for k, r in enumerate(rows)
                if r and r[0] and str(r[0]).startswith("HARVEST")), len(rows))
    head = next((k for k, r in enumerate(rows[:end]) if r and r[0] == "Batch"),
                None)
    if head is None:
        return []
    return [r for r in rows[head + 1:end] if r and r[0] is not None]


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
    assert s["... transfers refused whole"] == 1


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


# ---------------------------------------------------------------------------
# HARVEST + TranOG.
#
# Both read CLEAN on the two PR closings tested (110/110 and 101/101 harvests
# taken exactly as decided; 100.0% of planned TranOG entry realized). A clean
# number is worth nothing until the counter is shown to be reachable -- this
# project has already shipped a metric that could not report a non-zero by
# construction, and believed it. Every test below forces one of the paths.
# ---------------------------------------------------------------------------

from forecast.events import Harvest, TranOGEntry   # noqa: E402


def _state():
    st = FacilityState(D1, [
        TankState("OG1N-11", 11, "OG1N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG4N-41", 41, "OG4N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG5N-51", 51, "OG5N", 1000.0, 95.0, 1000.0, "OG"),
    ])
    return st


def _stock(st, tid, batch, n, wt=3100.0):
    st.tanks_by_id[tid].assign(batch_id=batch, count=n, avg_wt_g=wt,
                               cv_pct=16.0, stage="SW")


def _hsummary(harvests):
    wb = Workbook()
    write_realization_report(wb, [], harvest_events=harvests)
    rows = [[c.value for c in r] for r in wb["RealizationReport"].iter_rows()]
    return _summary(rows), rows


def test_harvest_keeps_what_was_asked_for_after_apply_overwrites_it():
    st = _state(); _stock(st, 41, "B45", 5000.0)
    ev = Harvest(batch_id="B45", event_date=D1, source_tank_id=41,
                 count=9999.0, avg_wt_g=3100.0)
    ev.apply(st)
    assert ev.count == 5000.0            # apply rewrote it to reality
    assert ev.requested_count == 9999.0  # ... and intent survived


def test_harvest_short_is_counted():
    st = _state(); _stock(st, 41, "B45", 5000.0)
    ev = Harvest(batch_id="B45", event_date=D1, source_tank_id=41,
                 count=9999.0, avg_wt_g=3100.0)
    ev.apply(st)
    s, _ = _hsummary([ev])
    assert s["... short (tank held fewer than asked)"] == 1
    assert s["Fish decided"] == 9999 and s["Fish taken"] == 5000


def test_harvest_source_mismatch_is_reported_as_refused():
    st = _state(); _stock(st, 41, "B47", 5000.0)
    ev = Harvest(batch_id="B45", event_date=D1, source_tank_id=41,
                 count=1000.0, avg_wt_g=3100.0)
    ev.apply(st)
    assert ev.refusal_reason == "source_holds_other_batch"
    assert ev.refusal_detail == "B47"
    s, rows = _hsummary([ev])
    assert s["... harvests refused whole"] == 1
    assert s["Fish taken"] == 0          # never credited as harvested
    assert any(r and r[2] == "source_holds_other_batch" for r in rows)


def test_harvest_r5_entry_tier_refusal_is_reachable():
    st = _state(); _stock(st, 11, "B45", 5000.0)      # OG1N = entry tier
    ev = Harvest(batch_id="B45", event_date=D1, source_tank_id=11,
                 count=1000.0, avg_wt_g=3100.0)
    ev.apply(st)
    assert ev.refusal_reason == "r5_entry_tier_harvest"
    assert _hsummary([ev])[0]["... harvests refused whole"] == 1


def test_inv5_force_empty_counts_as_over_realization_not_refusal():
    st = _state(); _stock(st, 41, "B45", 5000.0)
    ev = Harvest(batch_id="B45", event_date=D1, source_tank_id=41,
                 count=4000.0, avg_wt_g=3100.0, min_tank_control=2000.0)
    ev.apply(st)
    assert ev.forced_empty is True and ev.count == 5000.0
    s, _ = _hsummary([ev])
    assert s["... force-emptied (INV-5, took more)"] == 1
    assert s["... harvests refused whole"] == 0


def _tsummary(trans):
    wb = Workbook()
    write_realization_report(wb, [], tranog_events=trans)
    rows = [[c.value for c in r] for r in wb["RealizationReport"].iter_rows()]
    return _summary(rows), rows


def test_tranog_refused_destination_is_reachable_and_reported():
    st = _state(); _stock(st, 11, "B47", 100.0)       # occupied by another batch
    ev = TranOGEntry(batch_id="B45", event_date=D1, destinations=[
        TankAllocation(tank_id=11, count=600000.0, avg_wt_g=370.0, cv_pct=16.0),
    ])
    ev.apply(st)
    assert ("inv1_dest_holds_other_batch" in [r for _, r in ev.refusals])
    s, rows = _tsummary([ev])
    assert s["Fish that never entered"] == 600000
    assert s["Share of planned entry realized"] == "0.0%"
    assert any(r and r[2] == "inv1_dest_holds_other_batch" for r in rows)


def test_tranog_all_stocked_reads_100_percent():
    st = _state()
    ev = TranOGEntry(batch_id="B45", event_date=D1, destinations=[
        TankAllocation(tank_id=41, count=1000.0, avg_wt_g=370.0, cv_pct=16.0),
    ])
    ev.apply(st)
    s, _ = _tsummary([ev])
    assert s["Share of planned entry realized"] == "100.0%"
    assert s["Fish that never entered"] == 0


# ---------------------------------------------------------------------------
# GRADING: Grade (size split) and GradedHarvest (the peel).
#
# Also clean on both PRs (79/79 Grade, 7/7 GradedHarvest applied). Same rule as
# above: the zero only means something once each path is shown to be reachable.
#
# GradedHarvest matters more than its count suggests -- write_transfer_plan_output
# emits its pickup and retention rows WITHOUT checking whether it applied, so a
# refused peel prints on TransferPlan as a real move. This section is the only
# place that would show up.
# ---------------------------------------------------------------------------

from forecast.events import Grade, GradedHarvest   # noqa: E402


def _gsummary(grades=(), transfers=()):
    wb = Workbook()
    write_realization_report(wb, list(transfers), grade_events=list(grades))
    rows = [[c.value for c in r] for r in wb["RealizationReport"].iter_rows()]
    return _summary(rows), rows


def _gh(src, pickup, retention, batch="B45"):
    return GradedHarvest(
        batch_id=batch, event_date=D1, source_tank_id=src,
        pickup_tank_id=pickup, pickup_count=1000.0, pickup_avg_wt_g=3400.0,
        retention_tank_id=retention, retention_count=1000.0,
        retention_avg_wt_g=2600.0, cv_pct=16.0,
    )


def test_grade_count_not_conserved_is_reachable():
    st = _state(); _stock(st, 41, "B45", 5000.0)
    ev = Grade(batch_id="B45", event_date=D1, source_tank_ids=[41],
               destinations=[TankAllocation(tank_id=51, count=99.0,
                                            avg_wt_g=3100.0, cv_pct=16.0)])
    ev.apply(st)
    assert ev.refusal_reason == "count_not_conserved"
    s, rows = _gsummary(grades=[ev])
    assert s["Grade events emitted"] == 1
    assert s["... grades refused whole"] == 1
    assert any(r and r[2] == "count_not_conserved" for r in rows)


def test_grade_applied_is_not_counted_as_refused():
    st = _state(); _stock(st, 41, "B45", 5000.0)
    ev = Grade(batch_id="B45", event_date=D1, source_tank_ids=[41],
               destinations=[TankAllocation(tank_id=51, count=5000.0,
                                            avg_wt_g=3100.0, cv_pct=16.0)])
    ev.apply(st)
    assert ev.refusal_reason is None
    s, _ = _gsummary(grades=[ev])
    assert s["... grades refused whole"] == 0


def test_graded_harvest_source_mismatch_is_reachable():
    st = _state(); _stock(st, 41, "B47", 5000.0)
    ev = _gh(41, 51, 11)
    ev.apply(st)
    assert ev.refusal_reason == "source_holds_other_batch"
    assert ev.refusal_detail == "B47"
    s, rows = _gsummary(transfers=[ev])
    assert s["... peels refused whole"] == 1
    assert any(r and r[0] == "GradedHarvest" for r in rows)


def test_graded_harvest_r5_entry_tier_is_reachable():
    st = _state(); _stock(st, 11, "B45", 5000.0)      # OG1N source
    ev = _gh(11, 51, 41)
    ev.apply(st)
    assert ev.refusal_reason == "r5_entry_tier_harvest"
    assert _gsummary(transfers=[ev])[0]["... peels refused whole"] == 1


def test_graded_harvest_pickup_holding_another_batch_is_reachable():
    st = _state(); _stock(st, 41, "B45", 5000.0); _stock(st, 51, "B47", 10.0)
    ev = _gh(41, 51, 11)
    ev.apply(st)
    assert ev.refusal_reason == "pickup_holds_other_batch"
    assert ev.refusal_detail == "B47"


def test_graded_harvest_is_not_counted_as_a_transfer():
    """It rides in transfer_events but is not a tank-to-tank move; counting it
    there would inflate the transfer realization rate with the wrong unit."""
    st = _state(); _stock(st, 41, "B45", 5000.0)
    ev = _gh(41, 51, 11)
    ev.apply(st)
    s, _ = _gsummary(transfers=[ev])
    assert s["Transfer events emitted"] == 0
    assert s["GradedHarvest events emitted"] == 1
