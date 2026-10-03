"""Limit-matching helpers for the manual window (forecast/limit_match.py).

Operator, 2026-10-03 ("give it a try"): the editor should offer the count that
lands an entry ON the week's limit instead of leaving the arithmetic -- through
two weeks of purge mortality -- to them. With the October limits pinned at
min = max = 51,500 fish for 2026-W40..W52, their W44 fill of 6N tank 63
(23,357 already in + 32,000) holds 55,357 at W46.

Every proposed count is checked AGAINST THE RUN: the same events, with the
proposal written in, are executed by manual_window.advance_facility_window (the
window engine run.py uses), and the tank / week must land on the limit to the
fish -- and one fish more must go over it.

Uses the operator's October workbook like test_manual_preview; skipped when it
is not on this machine.
"""
from __future__ import annotations

import contextlib
import copy
import io
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_manual_preview import OCT_EVENTS, PR_0930

ROOT = Path(__file__).resolve().parent.parent


def build_env():
    """(state, ctx, events) -- as tests.test_manual_preview's fixture (and
    app._hydrate_state_from_upload), callable outside pytest for the AppTest
    driver below."""
    from datetime import datetime, timedelta
    from openpyxl import load_workbook
    from forecast.config_io import load_config
    from forecast.manual_events import manual_events_from_list
    from forecast.production_report import (hydrate_facility_state,
                                            read_production_report)
    from forecast.scenario_io import load_batches
    from forecast.state import FacilityState
    wb = load_workbook(PR_0930, data_only=True, read_only=False)
    control, tables, facility = load_config(ROOT / "config")
    batches = load_batches(ROOT / "scenario")
    pc, og, fw = read_production_report(wb)
    fs = datetime(pc.year, pc.month, pc.day) + timedelta(days=1)
    control.forecast_start = fs
    state = FacilityState.from_facility_config(facility, today=fs.date())
    hydrate_facility_state(state, og, batches)
    state.grade_efficiency = float(control.grade_efficiency or 1.0)
    ctx = {"batch_by_id": {b.batch_id: b for b in batches}, "tables": tables,
           "forecast_start": fs.date(), "control": control, "pr_closing": pc,
           "fw_records": fw}
    return state, ctx, manual_events_from_list(OCT_EVENTS)


@pytest.fixture(scope="module")
def env():
    return build_env()

pytestmark = pytest.mark.skipif(not PR_0930.exists(),
                                reason="the 2026-09-30 ProductionReport is not on this machine")


def _oct_limits():
    """The operator's per-week rows (scenario/limits.yaml, 2026-10-02):
    51,500 / 51,500 for W40..W52, the floor 51,000 in W43-W44."""
    from forecast.scenario_io import facility_limits_from_list
    rows = []
    for w in range(40, 53):
        lbl = f"2026-W{w}"
        rows.append({"week": lbl, "metric": "max_harvest_per_week", "value": 51500.0})
        rows.append({"week": lbl, "metric": "min_harvest_per_week",
                     "value": 51000.0 if w in (43, 44) else 51500.0})
    return facility_limits_from_list(rows)


def _window(env, events, n):
    from forecast.manual_window import advance_facility_window
    state, ctx, _ = env
    sc = copy.deepcopy(state)
    with contextlib.redirect_stdout(io.StringIO()):
        win = advance_facility_window(
            sc, ctx["batch_by_id"], ctx["tables"], ctx["forecast_start"], n,
            events=events, control=ctx["control"], pr_closing=ctx["pr_closing"],
            fw_records=ctx["fw_records"])
    return sc, win


def _week_total(env, win, wk):
    from forecast.time_grid import week_start
    d = week_start(wk - 1, env[1]["forecast_start"])
    return sum(float(h.count) for h in win["harvest_events"] if h.event_date == d)


def _tank_at_open(env, events, tank, wk):
    sc, _ = _window(env, events, wk - 1)
    t = sc.tanks_by_id[tank]
    return 0.0 if t.is_empty else float(t.count)


def _og_to_6n(wk, src, dst, n):
    from forecast.manual_events import ManualDest, ManualEvent
    return ManualEvent(type="og_to_6n", week=wk, from_tank=src, count=float(n),
                       destinations=[ManualDest(tank=dst, count=float(n))])


# ------------------------------------------------------- fill to limit ---

def test_fill_to_limit_lands_tank_63_on_w46s_limit(env):
    """The operator's case: W44 (window week 5) og_to_6n 53 -> 63. Unscripted,
    63 comes out at W46 (fill + the 2-week hold = the handoff week) and must
    hold no more than 51,500 then -- after two weeks of purge mortality."""
    from forecast import limit_match as lm
    state, ctx, evs = env
    i63 = 17
    assert (evs[i63].from_tank, evs[i63].destinations[0].tank) == (53, 63)
    rest = evs[:i63] + evs[i63 + 1:]
    src = lm._source_count_at(state, ctx, evs, evs[i63])
    s = lm.fill_to_limit(state, ctx, rest, lambda n: _og_to_6n(5, 53, 63, n),
                         63, 5, src, _oct_limits())
    assert s is not None and s.ok and not s.clamped
    assert (s.week_label, s.measure, s.target) == ("2026-W46", "tank", 51500.0)
    assert 27000 < s.count < 29500 < 32000        # what the operator typed
    # The run: the proposal lands on the limit; one more fish goes over it.
    at = _tank_at_open(env, rest + [_og_to_6n(5, 53, 63, s.count)], 63, 7)
    over = _tank_at_open(env, rest + [_og_to_6n(5, 53, 63, s.count + 1)], 63, 7)
    assert 51499.0 <= at <= 51500.0 + 1e-6, at
    assert over > 51500.0, over
    assert abs(at - s.after) < 1e-6
    # The operator's own 32,000 is the 3,8xx over the preview warned about.
    assert _tank_at_open(env, evs, 63, 7) > 55000


def test_fill_to_limit_targets_the_weeks_total_when_its_harvest_is_scripted(env):
    """W41 (week 2) og_to_6n 33 -> 69 is harvested by a SCRIPTED harvest in
    W43 (week 4) together with tank 63: the proposal matches the WEEK's total
    to the limit, not tank 69 alone."""
    from forecast import limit_match as lm
    state, ctx, evs = env
    i69 = 7
    assert (evs[i69].from_tank, evs[i69].destinations[0].tank) == (33, 69)
    rest = evs[:i69] + evs[i69 + 1:]
    src = lm._source_count_at(state, ctx, evs, evs[i69])
    s = lm.fill_to_limit(state, ctx, rest, lambda n: _og_to_6n(2, 33, 69, n),
                         69, 2, src, _oct_limits())
    assert s is not None and s.ok
    assert (s.week_label, s.measure) == ("2026-W43", "week")
    _sc, win = _window(env, rest + [_og_to_6n(2, 33, 69, s.count)], 4)
    tot = _week_total(env, win, 4)
    assert 51499.0 <= tot <= 51500.0 + 1e-6, tot
    _sc, win2 = _window(env, rest + [_og_to_6n(2, 33, 69, s.count + 1)], 4)
    assert _week_total(env, win2, 4) > 51500.0
    # ...and the tank alone would have been a different (larger) number.
    alone = lm.fill_to_limit(state, ctx, rest, lambda n: _og_to_6n(2, 33, 69, n),
                             69, 2, src, None)          # Control limit 55,000
    assert alone.target == ctx["control"].max_harvest_per_week


def test_fill_to_limit_clamps_to_what_the_source_holds(env):
    """A target the whole source can't reach: the proposal is all of it,
    flagged as clamped with how far short it stays."""
    from forecast import limit_match as lm
    from forecast.scenario_io import facility_limits_from_list
    state, ctx, evs = env
    rest = evs[:17] + evs[18:]
    src = lm._source_count_at(state, ctx, evs, evs[17])
    huge = facility_limits_from_list(
        [{"week": "2026-W46", "metric": "max_harvest_per_week", "value": 200000.0}])
    s = lm.fill_to_limit(state, ctx, rest, lambda n: _og_to_6n(5, 53, 63, n),
                         63, 5, src, huge)
    assert s.clamped and s.count == pytest.approx(int(src), abs=1)
    assert "short" in s.note


# ----------------------------------------------------------- suggestions ---

def test_window_suggestions_october(env):
    from forecast import limit_match as lm
    state, ctx, evs = env
    sugs = lm.window_suggestions(state, ctx, evs, _oct_limits())
    six = {(s.tank_id, s.week_label): s for s in sugs if s.row == "6N"}
    wk = {s.week_label: s for s in sugs if s.row == "week"}
    # 6N fills over their harvest week's limit, tank ALONE: 63 (W46) and 61
    # (W45). 63 and 69 harvested together in W43 fit alone -> no 6N row.
    assert set(six) == {(63, "2026-W46"), (61, "2026-W45")}
    assert six[(63, "2026-W46")].entry_index == 17          # the 32,000 entry
    assert 27000 < six[(63, "2026-W46")].proposed < 29500
    assert six[(61, "2026-W45")].entry_index == 14          # 52 -> 61, 13,000
    # Window weeks: over -> a fill of a tank harvested that week; under with
    # nothing to raise -> no proposal, a reason instead.
    assert wk["2026-W42"].entry_index == 4 and wk["2026-W42"].current is None
    assert wk["2026-W43"].entry_index == 7
    assert wk["2026-W45"].entry_index == 14 and wk["2026-W45"].same_as_6n
    for w in ("2026-W40", "2026-W41", "2026-W44"):
        assert wk[w].proposed is None and "no single scripted entry" in wk[w].message
    assert "ProductionReport" in wk["2026-W40"].message
    assert "already send all their source holds" in wk["2026-W44"].message


def test_applying_the_suggestions_top_down_lands_each_week_on_its_limit(env):
    """Apply as the strip offers it: the top suggestion, re-check, repeat. An
    edit can shift a LATER week (cutting 52 -> 61 in W43 leaves fish in 52
    that the W44 '52 -> 63 whole tank' carries on), which is why the list is
    re-solved after every Apply. It settles, and then every week the edits
    touched sits on 51,500 to the fish and tank 63 holds <= 51,500 at W46."""
    from forecast import limit_match as lm
    state, ctx, evs = env
    cur, rounds, edited = list(evs), 0, set()
    while rounds < 8:
        todo = [s for s in lm.window_suggestions(state, ctx, cur, _oct_limits())
                if s.proposed is not None]
        if not todo:
            break
        edited.add(todo[0].entry_index)
        cur = lm.apply_suggestion(cur, todo[0])
        rounds += 1
    assert not todo, [s.message for s in todo]
    assert {14, 17} <= edited
    assert all(evs[i].type in ("og_to_6n", "harvest", "graded_harvest") for i in edited)
    _sc, win = _window(env, cur, 6)
    for w in (3, 4, 6):                       # W42, W43, W45
        assert 51499.0 <= _week_total(env, win, w) <= 51500.0 + 1e-6, w
    assert 51499.0 <= _tank_at_open(env, cur, 63, 7) <= 51500.0 + 1e-6
    for i, (a, b) in enumerate(zip(evs, cur)):
        if i not in edited:
            assert (a.count, [(d.tank, d.count) for d in a.destinations]) ==                    (b.count, [(d.tank, d.count) for d in b.destinations])


def test_suggestions_come_earliest_entry_first(env):
    from forecast import limit_match as lm
    state, ctx, evs = env
    weeks = [evs[s.entry_index].week for s in
             lm.window_suggestions(state, ctx, evs, _oct_limits())
             if s.proposed is not None]
    assert weeks == sorted(weeks) and len(weeks) >= 4


def test_a_whole_tank_entry_gets_an_explicit_count(env):
    from forecast import limit_match as lm
    _state, _ctx, evs = env
    e = lm.with_count(evs[4], 6380.0, 67)       # og_to_6n 45 -> 67, whole tank
    assert (e.count, e.destinations[0].count) == (6380.0, 6380.0)
    assert evs[4].destinations[0].count is None   # the original is not mutated


# -------------------------------------------------------- room captions ---

def test_harvest_room():
    from forecast.limit_match import harvest_room
    assert harvest_room({"W": 40000.0}, "W", 51500.0, 51500.0, 5000.0) == (6500.0, 6500.0)
    assert harvest_room({"W": 40000.0}, "W", 51500.0, 51500.0, 15000.0) == (0.0, -3500.0)
    assert harvest_room({}, "W", None, 55000.0, 0.0) == (None, 55000.0)


def test_density_room_fish():
    from datetime import date
    from forecast.limit_match import density_room_fish
    from forecast.state import FacilityState, TankState
    s = FacilityState(date(2026, 10, 5), [TankState("OG3N-3", 3, "OG3N", 300.0, 95.0,
                                                    1000.0, "OG")])
    t = s.tanks_by_id[3]
    assert density_room_fish(t, 2000.0) == pytest.approx(300 * 95 * 1000 / 2000)
    t.assign("B50", 10000, 2000.0, 16.0, "SW")          # 20,000 kg in
    assert density_room_fish(t, 2000.0) == pytest.approx((28500 - 20000) * 1000 / 2000)


# ------------------------------------------------------ no plan change ---

def test_the_run_never_imports_the_helpers():
    """Read-only: nothing the pipeline executes imports limit_match."""
    code = ("import sys; sys.path.insert(0, %r); import forecast.run, "
            "forecast.manual_window, forecast.placement; "
            "print('forecast.limit_match' in sys.modules)" % str(ROOT))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, cwd=ROOT, timeout=300).stdout.strip()
    assert out.endswith("False"), out


# --------------------------------------------------------------- app ---

_DRIVER = r'''
import sys
sys.path.insert(0, %(root)r)
from streamlit.testing.v1 import AppTest

SCRIPT = r"""
import sys
sys.path.insert(0, %(root)r)
import streamlit as st
import app
from tests.test_limit_match import _oct_limits, build_env
from forecast.manual_events import manual_events_from_list
from tests.test_manual_preview import OCT_EVENTS
if "_env" not in st.session_state:
    st.session_state["_env"] = build_env()
state, ctx, _evs = st.session_state["_env"]
if "mw_events" not in st.session_state:
    evs = manual_events_from_list(OCT_EVENTS)
    if %(drop)r:
        evs = evs[:17] + evs[18:]
    st.session_state["mw_events"] = evs
st.session_state["_pr_key"] = "t"; st.session_state["_mw_events_pr"] = "t"
app._mw_facility_limits = _oct_limits
events = app._mw_events()
rows, labels, moves = app._mw_project(state, ctx, events, 8)
if %(strip)r:
    app._mw_window_strip(state, ctx, events, labels)
else:
    app._mw_action_panel(state, ctx, rows, labels, (53, labels[4], 5), {})
"""
'''

_PANEL = r'''
at = AppTest.from_string(SCRIPT, default_timeout=600)
at.run()
if at.exception:
    print("FAIL render: " + "; ".join(str(e)[:600] for e in at.exception)); raise SystemExit(1)
[r for r in at.radio if r.key == "mw_act"][0].set_value("Send to 6N depuration").run()
[m for m in at.multiselect if m.key == "mw_6_dest_53_5"][0].set_value([63]).run()
if at.exception:
    print("FAIL after pick: " + "; ".join(str(e)[:600] for e in at.exception)); raise SystemExit(1)
caps = [c.value for c in at.caption if "Room to" in c.value]
if not caps or "2026-W46" not in caps[0]:
    print("FAIL no room caption %r" % caps); raise SystemExit(1)
fill = [b for b in at.button if b.key == "mw_6_fill_53_5"]
if not fill:
    print("FAIL no fill button"); raise SystemExit(1)
lbl = fill[0].label
fill[0].click().run()
if at.exception:
    print("FAIL after fill: " + "; ".join(str(e)[:600] for e in at.exception)); raise SystemExit(1)
whole = [c for c in at.checkbox if c.key == "mw_6_whole_53_5"][0].value
tot = [n for n in at.number_input if n.key == "mw_6_total_53_5"]
if whole or not tot:
    print("FAIL fill did not set the count (whole=%r)" % whole); raise SystemExit(1)
n = tot[0].value
if not (27000 < n < 29500) or ("%s" % format(n, ",.0f")) not in lbl:
    print("FAIL count %r vs %r" % (n, lbl)); raise SystemExit(1)
warn = [w.value for w in at.warning if "over" in w.value and "6N" in w.value]
if warn:
    print("FAIL still warns over limit: %r" % warn); raise SystemExit(1)
[b for b in at.button if b.key == "mw_6_add"][0].click().run()
ev = at.session_state["mw_events"][-1]
if (ev.type, ev.from_tank, [(d.tank, d.count) for d in ev.destinations]) != ("og_to_6n", 53, [(63, n)]):
    print("FAIL added %r" % ((ev.type, ev.from_tank, [(d.tank, d.count) for d in ev.destinations]),)); raise SystemExit(1)
print("OK panel")
'''

_STRIP = r'''
at = AppTest.from_string(SCRIPT, default_timeout=600)
at.run()
if at.exception:
    print("FAIL render: " + "; ".join(str(e)[:600] for e in at.exception)); raise SystemExit(1)
apply = [b for b in at.button if (b.key or "").startswith("mw_sug_apply_")]
if len(apply) != 4:
    print("FAIL apply buttons %r" % [b.key for b in apply]); raise SystemExit(1)
def _snap(evs):
    return [(e.count, [(d.tank, d.count) for d in e.destinations]) for e in evs]
before = _snap(at.session_state["mw_events"])
apply[0].click().run()
if at.exception:
    print("FAIL after apply: " + "; ".join(str(e)[:600] for e in at.exception)); raise SystemExit(1)
after = _snap(at.session_state["mw_events"])
changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
if changed != [4] or after[4][1][0][1] is None or not (0 < after[4][1][0][1] < 9000):
    print("FAIL apply edited %r -> %r" % (changed, [after[i] for i in changed])); raise SystemExit(1)
left = [b for b in at.button if (b.key or "").startswith("mw_sug_apply_")]
if not left or set(b.key for b in left) & set(b.key for b in apply):
    print("FAIL strip did not re-evaluate: %r" % [b.key for b in left]); raise SystemExit(1)
print("OK strip")
'''


def _drive(body, drop, strip):
    src = (_DRIVER % {"root": str(ROOT), "drop": drop, "strip": strip}) + body
    import os
    p = subprocess.run([sys.executable, "-c", src], capture_output=True,
                       text=True, timeout=1200, cwd=ROOT, encoding="utf-8",
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    tail = [ln for ln in (p.stdout or "").splitlines()
            if ln.startswith(("OK", "FAIL"))]
    return p.returncode, (tail[-1] if tail else (p.stderr or "")[-2000:])


def test_app_fill_to_limit_sets_the_count_and_adds_it():
    rc, msg = _drive(_PANEL, True, False)
    assert rc == 0 and msg == "OK panel", msg


def test_app_strip_suggestion_applies_and_reevaluates():
    rc, msg = _drive(_STRIP, False, True)
    assert rc == 0 and msg == "OK strip", msg
