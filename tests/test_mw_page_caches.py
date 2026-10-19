"""The manual-window page's speed-ups must never show a stale answer.

Operator, 2026-10-04: "make the grid lighter". Measured on the October kit
(AppTest, server side): an idle rerun of the editor spent 0.7-1.0 s of ~1.1 s
re-hashing config/ + scenario/ (13-20 times per rerun), 0.25-0.34 s re-projecting
the freshwater cohorts, and every event edit ran limit_match.window_suggestions
with 21 of its 59 window projections exact repeats. Three changes:

  * app._MW_RUN -- ONE config fingerprint per editor run, cleared when the run
    ends (so the next run re-reads disk);
  * app._mw_fw_load -- cached per (PR, config fingerprint, window weeks);
  * limit_match._memo_scope -- inside one helper call, an identical projection
    (and manual_window's freshwater lookup) is computed once.

Each test below pins that the cached answer is the fresh one, and each has a
negative control showing the test would catch a stale cache.
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_manual_preview import PR_0930

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(not PR_0930.exists(),
                                reason="the 2026-09-30 ProductionReport is not on this machine")


# ----------------------------------------------------- app: config fingerprint

_DRIVER = r'''
import json, sys, shutil
from pathlib import Path
sys.path.insert(0, %(root)r)
from streamlit.testing.v1 import AppTest
TMP = Path(%(tmp)r)
for d in ("config", "scenario"):
    shutil.copytree(Path(%(root)r) / d, TMP / d,
                    ignore=shutil.ignore_patterns("costs.yaml*"))   # never the live costs

SCRIPT = r"""
import sys, datetime
from pathlib import Path
sys.path.insert(0, %(root)r)
import streamlit as st
import app
from forecast.manual_events import manual_events_from_list
from tests.test_manual_preview import OCT_EVENTS, PR_0930
TMP = Path(%(tmp)r)
app.CONFIG_DIR = TMP / "config"; app.SCENARIO_DIR = TMP / "scenario"
if %(sticky)r and not getattr(app, "_sticky_on", False):
    # NEGATIVE CONTROL: a fingerprint that never refreshes -- the stale
    # cache the per-run scope must never become.
    _first = app._config_fingerprint()
    app._mw_cfg_fp = lambda: _first
    app._sticky_on = True
app.pr = {"ok": True, "closing": datetime.date(2026, 9, 30)}
st.session_state["_pr_key"] = "t"; st.session_state["_mw_events_pr"] = "t"
if "mw_events" not in st.session_state:
    st.session_state["mw_events"] = manual_events_from_list(OCT_EVENTS)
class _Up:
    name = PR_0930.name
    def getvalue(self):
        return PR_0930.read_bytes()
app._manual_window_editor(_Up())
st.session_state["_after_fp"] = getattr(app._MW_RUN, "fp", "unset")
st.session_state["_disk_fp"] = app._config_fingerprint()
"""

def limits(at):
    for df in at.dataframe:
        v = df.value
        if "Scripted harvest (fish)" in v.columns:
            return [float(x) for x in v["Limit"]]
    return None

at = AppTest.from_string(SCRIPT, default_timeout=900)
at.run()
out = {"first": limits(at), "exc1": [str(e.value)[:200] for e in at.exception],
       "fp_cleared_1": at.session_state["_after_fp"] is None}
# An in-app config save between two reruns: the processing limit moves.
ctl = TMP / "config" / "control.yaml"
txt = ctl.read_text(encoding="utf-8")
assert "max_harvest_per_week: 55000.0" in txt
ctl.write_text(txt.replace("max_harvest_per_week: 55000.0",
                           "max_harvest_per_week: 54000.0"), encoding="utf-8")
at.run()
out["second"] = limits(at)
out["exc2"] = [str(e.value)[:200] for e in at.exception]
out["fp_cleared_2"] = at.session_state["_after_fp"] is None
out["fwload_fp_is_disk"] = (at.session_state["_mw_fwload_cache"]["key"][1]
                            == at.session_state["_disk_fp"])
print("RESULT " + json.dumps(out))
'''


def _drive(tmp_path, sticky):
    code = _DRIVER % {"root": str(ROOT), "tmp": str(tmp_path), "sticky": sticky}
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, cwd=ROOT, timeout=1200)
    line = [ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")]
    assert line, p.stdout[-2000:] + p.stderr[-3000:]
    return json.loads(line[-1][len("RESULT "):])


def test_a_config_save_reaches_the_window_strip_on_the_next_rerun(tmp_path):
    r = _drive(tmp_path, sticky=False)
    assert not r["exc1"] and not r["exc2"], r
    assert r["first"] and set(r["first"]) == {55000.0}, r
    assert set(r["second"]) == {54000.0}, r          # fresh, not cached
    # the per-run fingerprint never outlives its editor run
    assert r["fp_cleared_1"] and r["fp_cleared_2"], r
    # the FW-load cache is keyed on the fingerprint the disk has NOW
    assert r["fwload_fp_is_disk"], r


def test_negative_control_a_sticky_fingerprint_shows_the_stale_limit(tmp_path):
    """The test above is load-bearing: with a fingerprint that never
    refreshes, the second run shows the OLD limit."""
    r = _drive(tmp_path, sticky=True)
    assert r["first"] and set(r["first"]) == {55000.0}, r
    assert set(r["second"]) == {55000.0}, r


# --------------------------------------------- limit_match: projection memo

def _snap(x):
    if isinstance(x, (list, tuple)):
        return [_snap(y) for y in x]
    if dataclasses.is_dataclass(x):
        return repr(sorted((k, repr(v)) for k, v in dataclasses.asdict(x).items()))
    return repr(x)


@pytest.fixture(scope="module")
def env():
    from tests.test_limit_match import build_env
    return build_env()


def test_memoised_helpers_give_the_unmemoised_answers(env):
    from forecast import limit_match as lm
    from forecast import manual_window as mw
    from forecast.manual_events import ManualDest, ManualEvent
    from tests.test_limit_match import _oct_limits
    state, ctx, events = env
    lim = _oct_limits()
    assert (_snap(lm.window_suggestions(state, ctx, events, lim))
            == _snap(lm._window_suggestions(state, ctx, events, lim)))
    for tid, wk in [(63, 5), (61, 4), (69, 2)]:
        assert (_snap(lm.count_at_harvest(state, ctx, events, tid, wk))
                == _snap(lm._count_at_harvest(state, ctx, events, tid, wk)))

    def make(n):
        return ManualEvent(type="og_to_6n", week=8, from_tank=45, count=float(n),
                           destinations=[ManualDest(tank=61, count=float(n))])
    assert (_snap(lm.fill_to_limit(state, ctx, events, make, 61, 8, 60909.0, lim))
            == _snap(lm._fill_to_limit(state, ctx, events, make, 61, 8, 60909.0, lim)))
    # the memo never outlives the helper call
    assert getattr(lm._MEMO, "d", None) is None
    assert getattr(mw._FW_MEMO, "d", None) is None


def test_the_memo_is_off_after_an_exception_inside_the_scope():
    from forecast import limit_match as lm
    from forecast import manual_window as mw
    with pytest.raises(RuntimeError):
        with lm._memo_scope():
            assert lm._MEMO.d == {} and mw._FW_MEMO.d == {}
            raise RuntimeError("boom")
    assert getattr(lm._MEMO, "d", None) is None
    assert getattr(mw._FW_MEMO, "d", None) is None


def test_the_memo_tells_different_event_lists_apart(env):
    """Two event lists that differ in one count must not share a projection."""
    import copy
    from forecast import limit_match as lm
    state, ctx, events = env
    other = copy.deepcopy(events)
    other[17].count = 20000.0
    for d in other[17].destinations:
        d.count = 20000.0
    with lm._memo_scope():
        a = lm.project(state, ctx, events, 8)[1]
        b = lm.project(state, ctx, other, 8)[1]
    plain_b = lm.project(state, ctx, other, 8)[1]
    # entry 17 is the W5 fill 53 -> 63: tank 63's count at the open of week 7
    n63 = lambda w: lm._tank_at_open(ctx, w, 63, 7)  # noqa: E731
    assert n63(b) == n63(plain_b)
    assert n63(a) != n63(b)


def test_negative_control_a_memo_blind_to_events_returns_the_wrong_window(
        env, monkeypatch):
    """The test above is load-bearing: key the memo on anything but the
    events and the edited list gets the unedited list's projection."""
    import copy
    from forecast import limit_match as lm
    state, ctx, events = env
    monkeypatch.setattr(lm, "_events_key", lambda evs: "same")
    other = copy.deepcopy(events)
    other[17].count = 20000.0
    for d in other[17].destinations:
        d.count = 20000.0
    with lm._memo_scope():
        a = lm.project(state, ctx, events, 8)[1]
        b = lm.project(state, ctx, other, 8)[1]
    assert lm._tank_at_open(ctx, a, 63, 7) == lm._tank_at_open(ctx, b, 63, 7)


def _fw_events(batch):
    from forecast.manual_events import ManualDest, ManualEvent
    return [ManualEvent(type="fw_to_og", week=3, batch=batch,
                        destinations=[ManualDest(tank=22), ManualDest(tank=13)])]


def _lookups(state, ctx):
    from forecast import limit_match as lm
    from forecast import manual_window as mw
    args = (ctx["fw_records"], ctx["control"], ctx["pr_closing"], ctx["tables"],
            ctx["batch_by_id"])
    with lm._memo_scope():
        a = mw._build_fw_lookup(_fw_events("B50"), *args)
        b = mw._build_fw_lookup(_fw_events("B51"), *args)
    return a, b, mw._build_fw_lookup(_fw_events("B51"), *args)


def test_fw_lookup_memo_is_off_in_a_run_and_keyed_on_the_batches(env):
    from forecast import manual_window as mw
    state, ctx, _ = env
    assert getattr(mw._FW_MEMO, "d", None) is None        # a run never memoises
    a, b, plain_b = _lookups(state, ctx)
    assert a and b and a != b
    assert b == plain_b


def test_negative_control_a_key_without_the_batches_collides(env, monkeypatch):
    """The keyed-on-batches test is load-bearing: drop the batch set from the
    key and B51's lookup comes back as B50's."""
    from forecast import manual_window as mw
    state, ctx, _ = env
    monkeypatch.setattr(mw, "_fw_memo_key",
                        lambda fb, *rest: tuple(id(x) for x in rest[:1]))
    a, b, plain_b = _lookups(state, ctx)
    assert b != plain_b and b == a
