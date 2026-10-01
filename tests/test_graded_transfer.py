"""Manual graded_transfer: OG->OG, whole tank or part of one, graded or split.

Operator ask (2026-10-01): "a graded transfer from tank OG_ to any two other
OG tanks, where we define the count per destination tank, and the app can
calculate the split, graded transfer percents and resulting biomasses, counts
and average weights ... making sure everything adds up before and after".
Both partial/whole and graded/not-graded are options.

Every test checks the facility's fish and biomass before vs after; every
refusal test checks the facility is left exactly as it was.
"""
from __future__ import annotations

import copy
from datetime import date

import pytest

from forecast import manual_events as me
from forecast.biology import count_split_classes
from forecast.manual_events import (
    ManualDest, ManualEvent, apply_events_for_week, dump_manual_events,
    load_manual_events, plan_graded_transfer, validate_manual_events)
from forecast.state import FacilityState, TankState

WEEK = date(2026, 10, 5)
EFF = 0.85


def _mk_state():
    tanks = [
        TankState("OG1N-1", 1, "OG1N", 200.0, 95.0, 1000.0, "OG"),
        TankState("OG1S-2", 2, "OG1S", 200.0, 95.0, 1000.0, "OG"),
        TankState("OG3N-3", 3, "OG3N", 300.0, 95.0, 1000.0, "OG"),
        TankState("OG4N-4", 4, "OG4N", 300.0, 95.0, 1000.0, "OG"),
        TankState("OG5N-5", 5, "OG5N", 300.0, 95.0, 1000.0, "OG"),
        TankState("OG5S-6", 6, "OG5S", 300.0, 95.0, 1000.0, "OG"),
        TankState("OG6N-61", 61, "OG6N", 300.0, 95.0, 1000.0, "OG"),
        TankState("FW-90", 90, "FW1", 100.0, 95.0, 1000.0, "FW"),
    ]
    st = FacilityState(WEEK, tanks)
    st.grade_efficiency = EFF
    st.tanks_by_id[3].assign("B50", 60_000, 2_400.0, 16.0, "SW")
    return st


def _snap(st):
    return {t.tank_id: (t.batch_id, round(t.count, 6), round(t.avg_wt_g, 6),
                        round(t.cv_pct, 6), t.stage)
            for t in st.tanks_by_id.values()}


def _totals(st):
    return (sum(t.count for t in st.tanks_by_id.values()),
            sum(t.biomass_kg for t in st.tanks_by_id.values()))


def _run(st, ev):
    tr, hv, tn, warns, _ = apply_events_for_week(st, [ev], 1, WEEK)
    return tr, warns


def _gt(dests, count=None, mode="grade", src=3):
    return ManualEvent(type="graded_transfer", week=1, from_tank=src,
                       destinations=[ManualDest(tank=t, count=c) for t, c in dests],
                       count=count, mode=mode)


def _assert_balanced(before, st):
    c, b = _totals(st)
    assert c == pytest.approx(before[0], abs=0.5)
    assert b == pytest.approx(before[1], abs=0.5)


# --------------------------------------------------------------------------
# What it does
# --------------------------------------------------------------------------

def test_whole_tank_graded_into_two_tanks():
    st = _mk_state()
    before = _totals(st)
    tr, warns = _run(st, _gt([(4, 20_000), (5, None)]))
    assert any(w.startswith("MANUAL EVENT OK") for w in warns), warns
    _assert_balanced(before, st)
    big, small = count_split_classes(2_400.0, 16.0, [1 / 3, 2 / 3], EFF)
    t4, t5 = st.tanks_by_id[4], st.tanks_by_id[5]
    assert (t4.count, t5.count) == (pytest.approx(20_000), pytest.approx(40_000))
    assert t4.avg_wt_g == pytest.approx(big[0]) and t4.cv_pct == pytest.approx(big[1])
    assert t5.avg_wt_g == pytest.approx(small[0]) and t5.cv_pct == pytest.approx(small[1])
    assert t4.avg_wt_g > 2_400.0 > t5.avg_wt_g
    assert t4.cv_pct < 16.0                        # grading narrows the spread
    assert st.tanks_by_id[3].is_empty
    assert len(tr) == 1 and tr[0].channel == "manual:graded_transfer:grade"
    # The audit debits the source at the destinations' count-weighted weight:
    # that must be the parent weight, or the continuity audit would drift.
    d = tr[0].destinations
    assert sum(a.count * a.avg_wt_g for a in d) / sum(a.count for a in d) \
        == pytest.approx(2_400.0)


def test_whole_tank_three_classes_source_keeps_the_middle():
    st = _mk_state()
    before = _totals(st)
    _tr, warns = _run(st, _gt([(4, 15_000), (3, 30_000), (5, 15_000)]))
    assert any(w.startswith("MANUAL EVENT OK") for w in warns), warns
    _assert_balanced(before, st)
    cls = count_split_classes(2_400.0, 16.0, [0.25, 0.5, 0.25], EFF)
    s = st.tanks_by_id[3]
    assert s.count == pytest.approx(30_000)
    assert s.avg_wt_g == pytest.approx(cls[1][0]) and s.cv_pct == pytest.approx(cls[1][1])
    assert st.tanks_by_id[4].avg_wt_g > s.avg_wt_g > st.tanks_by_id[5].avg_wt_g


def test_partial_grade_leaves_an_ungraded_share_at_the_parent_weight():
    st = _mk_state()
    before = _totals(st)
    _tr, warns = _run(st, _gt([(4, 10_000), (5, 15_000)], count=25_000))
    assert any(w.startswith("MANUAL EVENT OK") for w in warns), warns
    _assert_balanced(before, st)
    s = st.tanks_by_id[3]
    assert s.count == pytest.approx(35_000)
    assert s.avg_wt_g == pytest.approx(2_400.0) and s.cv_pct == pytest.approx(16.0)
    cls = count_split_classes(2_400.0, 16.0, [0.4, 0.6], EFF)
    assert st.tanks_by_id[4].avg_wt_g == pytest.approx(cls[0][0])
    assert st.tanks_by_id[5].avg_wt_g == pytest.approx(cls[1][0])


@pytest.mark.parametrize("count", [None, 30_000])
def test_split_mode_keeps_the_parent_weight_and_cv_on_every_leg(count):
    st = _mk_state()
    before = _totals(st)
    dests = [(4, 10_000), (5, None)] if count is None else [(4, 10_000), (5, 20_000)]
    tr, warns = _run(st, _gt(dests, count=count, mode="split"))
    assert any(w.startswith("MANUAL EVENT OK") for w in warns), warns
    _assert_balanced(before, st)
    for tid in (4, 5):
        t = st.tanks_by_id[tid]
        assert t.avg_wt_g == pytest.approx(2_400.0) and t.cv_pct == pytest.approx(16.0)
    assert tr[0].channel == "manual:graded_transfer:split"
    if count is not None:
        assert st.tanks_by_id[3].count == pytest.approx(30_000)


def test_same_batch_destination_is_topped_up_and_still_balances():
    st = _mk_state()
    st.tanks_by_id[4].assign("B50", 5_000, 2_600.0, 14.0, "SW")
    before = _totals(st)
    _tr, warns = _run(st, _gt([(4, 20_000), (5, None)]))
    assert any(w.startswith("MANUAL EVENT OK") for w in warns), warns
    _assert_balanced(before, st)
    assert st.tanks_by_id[4].count == pytest.approx(25_000)


def test_the_ok_line_states_before_and_after():
    st = _mk_state()
    _tr, warns = _run(st, _gt([(4, 20_000), (5, None)]))
    ok = next(w for w in warns if w.startswith("MANUAL EVENT OK"))
    assert "60,000 fish / 144,000.0 kg before" in ok
    assert "60,000 fish / 144,000.0 kg after" in ok
    assert "difference +0.0 fish, +0.0 kg" in ok


def test_plan_preview_is_what_the_run_applies():
    """The editor preview and the run call the same function."""
    st = _mk_state()
    plan = plan_graded_transfer(3, 60_000, 2_400.0, 16.0, [(4, 25_000), (5, None)],
                                count=None, mode="grade", grade_efficiency=EFF)
    assert not plan.errors
    _run(st, _gt([(4, 25_000), (5, None)]))
    for leg in plan.legs:
        t = st.tanks_by_id[leg.tank]
        assert (t.count, t.avg_wt_g, t.cv_pct) == (
            pytest.approx(leg.count), pytest.approx(leg.avg_wt_g), pytest.approx(leg.cv_pct))
    assert plan.after_biomass_kg == pytest.approx(plan.before_biomass_kg, abs=1e-6)


def test_blank_mode_reads_as_grade():
    st = _mk_state()
    _run(st, _gt([(4, 20_000), (5, None)], mode="transfer"))
    assert st.tanks_by_id[4].avg_wt_g > 2_400.0


def test_yaml_round_trip(tmp_path):
    ev = _gt([(4, 20_000), (3, None), (5, 10_000)], count=None, mode="split")
    dump_manual_events(tmp_path, [ev], pr_closing=date(2026, 9, 30))
    back = load_manual_events(tmp_path, pr_closing=date(2026, 9, 30))
    assert len(back) == 1
    b = back[0]
    assert (b.type, b.from_tank, b.count, b.mode) == ("graded_transfer", 3, None, "split")
    assert [(d.tank, d.count) for d in b.destinations] == [(4, 20_000), (3, None), (5, 10_000)]


def test_window_validation_accepts_a_good_event():
    st = _mk_state()
    res = validate_manual_events(st, [_gt([(4, 20_000), (5, None)])])
    assert res[0][1], res
    assert st.tanks_by_id[3].count == 60_000       # validation never mutates


# --------------------------------------------------------------------------
# Refusals — each leaves the facility exactly as it was
# --------------------------------------------------------------------------

def _assert_refused(st, ev, needle):
    snap = _snap(st)
    tr, warns = _run(st, ev)
    assert not tr
    refused = [w for w in warns if w.startswith("MANUAL EVENT REFUSED")]
    assert refused, warns
    assert needle in refused[0], refused[0]
    assert _snap(st) == snap
    # The dry-run validator says the same thing.
    st2 = copy.deepcopy(st)
    res = validate_manual_events(st2, [ev])
    assert not res[0][1]


def test_refused_when_counts_do_not_add_up():
    _assert_refused(_mk_state(), _gt([(4, 20_000), (5, 30_000)]), "add up to 50,000")


def test_refused_when_partial_counts_do_not_add_up():
    _assert_refused(_mk_state(), _gt([(4, 20_000), (5, 30_000)], count=40_000),
                    "add up to 50,000")


def test_refused_when_a_destination_holds_another_batch():
    st = _mk_state()
    st.tanks_by_id[5].assign("B77", 1_000, 1_000.0, 16.0, "SW")
    _assert_refused(st, _gt([(4, 20_000), (5, None)]), "DIFFERENT batch B77")


def test_refused_og12_move_between_entry_tanks_above_1kg():
    st = _mk_state()
    st.tanks_by_id[3].empty(WEEK)
    st.tanks_by_id[1].assign("B50", 60_000, 1_200.0, 16.0, "SW")
    _assert_refused(st, _gt([(2, 20_000), (4, None)], src=1), "R3")


def test_entry_tank_below_1kg_may_split_within_the_entry_tier():
    st = _mk_state()
    st.tanks_by_id[3].empty(WEEK)
    st.tanks_by_id[1].assign("B50", 60_000, 600.0, 16.0, "SW")
    before = _totals(st)
    _tr, warns = _run(st, _gt([(2, 20_000), (4, None)], src=1))
    assert any(w.startswith("MANUAL EVENT OK") for w in warns), warns
    _assert_balanced(before, st)


def test_refused_backward_move_into_the_entry_tier():
    _assert_refused(_mk_state(), _gt([(1, 20_000), (4, None)]), "R4")


def test_refused_from_a_6n_tank():
    st = _mk_state()
    st.tanks_by_id[3].empty(WEEK)
    st.tanks_by_id[61].assign("B50", 60_000, 3_000.0, 16.0, "STARVE")
    _assert_refused(st, _gt([(4, 20_000), (5, None)], src=61), "R7")


def test_refused_into_a_6n_tank():
    _assert_refused(_mk_state(), _gt([(61, 20_000), (5, None)]), "OG→OG only")


def test_refused_into_a_freshwater_tank():
    _assert_refused(_mk_state(), _gt([(90, 20_000), (5, None)]), "OG→OG only")


def test_refused_source_as_destination_in_a_partial():
    _assert_refused(_mk_state(), _gt([(4, 10_000), (3, 10_000)], count=20_000),
                    "PARTIAL transfer cannot send a class back")


def test_refused_two_blank_destinations():
    _assert_refused(_mk_state(), _gt([(4, None), (5, None)]), "only one destination")


def test_refused_single_destination():
    _assert_refused(_mk_state(), _gt([(4, None)]), "at least two")


def test_refused_partial_that_is_really_the_whole_tank():
    _assert_refused(_mk_state(), _gt([(4, 30_000), (5, 30_000)], count=60_000),
                    "whole tank")


def test_refused_unknown_mode():
    _assert_refused(_mk_state(), _gt([(4, 20_000), (5, None)], mode="sideways"),
                    "mode 'sideways'")


def test_refused_on_biomass_imbalance(monkeypatch):
    """A split that would invent or lose biomass is refused, not applied."""
    import forecast.biology as bio
    real = bio.count_split_classes

    def _leaky(mu, cv, fr, grade_efficiency=1.0):
        return [(m * 1.01, c) for m, c in real(mu, cv, fr, grade_efficiency)]
    monkeypatch.setattr(bio, "count_split_classes", _leaky)
    _assert_refused(_mk_state(), _gt([(4, 20_000), (5, None)]),
                    "biomass does not balance")


def test_biomass_guard_is_load_bearing(monkeypatch):
    """Negative control for the guard above: with the tolerance opened up the
    same leaky split DOES land and the facility gains biomass — so the
    refusal really is the guard's doing."""
    import forecast.biology as bio
    real = bio.count_split_classes

    def _leaky(mu, cv, fr, grade_efficiency=1.0):
        return [(m * 1.01, c) for m, c in real(mu, cv, fr, grade_efficiency)]
    monkeypatch.setattr(bio, "count_split_classes", _leaky)
    monkeypatch.setattr(me, "GT_BALANCE_TOL_KG", float("inf"))
    st = _mk_state()
    before = _totals(st)
    _run(st, _gt([(4, 20_000), (5, None)]))
    assert _totals(st)[1] > before[1] + 1_000


def test_refused_whole_event_when_one_leg_would_be_refused(monkeypatch):
    """Atomic: if the engine's Transfer refuses ANY leg (here a reserved
    tank), nothing moves — no half-graded tank."""
    st = _mk_state()
    st.reserved_tanks = {5}
    _assert_refused(st, _gt([(4, 20_000), (5, None)]), "would not accept")
