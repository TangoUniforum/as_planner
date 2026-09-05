"""Advice caches must name the inputs they were computed on.

DEFECT (2026-09-04). Two results that drive the Decide flow were stored in
session_state under FIXED keys, with no input identity at all:

    _key = "_monthly_check_res"          # step 1's "Keep your current settings"
    st.session_state["_slv_res"] = res   # the solved target bands

So both survive a new PR upload, a config edit, a scenario edit and a change of
targets, and keep rendering as though they described the current inputs. Step 1
is the first thing the operator reads, and it states a conclusion in bold.

The project already has the right identity function -- `_sweep_inputs_sig()`,
which folds the PR content hash, the config/scenario fingerprint, the engine
source fingerprint and the metrics-schema version, and exists because cached
board legs replayed pre-edit scenarios twice in August. What it does NOT fold is
TARGETS, correctly: targets regrade an existing forecast without rerunning it,
so they must not invalidate expensive engine results.

But targets are an INPUT to both of these tasks -- the monthly check measures
against them and the solver solves for them -- so they must participate in THESE
identities even though they stay out of the sweep's. That is the distinction the
review brief asked for: dependency-specific identities, not one blunt key.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = pytest.importorskip("app", reason="app.py not importable without Streamlit")


def test_the_task_signature_folds_in_the_sweep_identity(monkeypatch):
    monkeypatch.setattr(app, "_sweep_inputs_sig", lambda: "SIG-A")
    a = app._advice_sig("monthly", {"2026-12": 600.0})
    monkeypatch.setattr(app, "_sweep_inputs_sig", lambda: "SIG-B")
    b = app._advice_sig("monthly", {"2026-12": 600.0})
    assert a != b, "a new PR or config edit does not change the advice identity"


def test_targets_participate(monkeypatch):
    """The distinction the brief asked for: targets stay OUT of the sweep
    signature (they regrade, they do not re-run) but they are an INPUT here."""
    monkeypatch.setattr(app, "_sweep_inputs_sig", lambda: "SIG-A")
    a = app._advice_sig("monthly", {"2026-12": 600.0})
    b = app._advice_sig("monthly", {"2026-12": 650.0})
    assert a != b, "changing a target left the advice identity unchanged"


def test_two_tasks_do_not_share_an_identity(monkeypatch):
    monkeypatch.setattr(app, "_sweep_inputs_sig", lambda: "SIG-A")
    assert app._advice_sig("monthly", {}) != app._advice_sig("solver", {})


def test_identical_inputs_are_stable(monkeypatch):
    """NEGATIVE CONTROL. It must not churn, or the cache never hits and every
    rerender re-runs a multi-minute measurement."""
    monkeypatch.setattr(app, "_sweep_inputs_sig", lambda: "SIG-A")
    t = {"2026-12": 600.0, "2027-01": 500.0}
    assert app._advice_sig("monthly", t) == app._advice_sig("monthly", dict(t))


def test_target_key_order_does_not_matter(monkeypatch):
    monkeypatch.setattr(app, "_sweep_inputs_sig", lambda: "SIG-A")
    assert (app._advice_sig("monthly", {"a": 1.0, "b": 2.0})
            == app._advice_sig("monthly", {"b": 2.0, "a": 1.0}))


def test_unhashable_or_odd_targets_do_not_raise(monkeypatch):
    """A diagnostic that can break a render is worse than no diagnostic --
    the same rule _read_or_explain and override_coverage_gaps follow."""
    monkeypatch.setattr(app, "_sweep_inputs_sig", lambda: "SIG-A")
    for t in (None, [], {"x": object()}, "not-a-dict"):
        assert isinstance(app._advice_sig("monthly", t), str)


def test_the_caches_are_keyed_by_it():
    """Static half: neither result may be stored under a bare constant again."""
    import ast
    src = open(app.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if (isinstance(t, ast.Subscript)
                    and isinstance(t.value, ast.Attribute)
                    and t.value.attr == "session_state"
                    and isinstance(t.slice, ast.Constant)
                    and t.slice.value in ("_slv_res", "_monthly_check_res")):
                bad.append(t.slice.value)
    assert not bad, (
        "%s still stored under a fixed session key — the result cannot say "
        "which inputs it was computed on" % sorted(set(bad)))
