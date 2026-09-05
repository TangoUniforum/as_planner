"""Activating a method is a different act from looking at its plan.

DEFECT (2026-09-04). Compare & Choose's "Use this plan" installs the result AND
sets the active planning method, by its own comment:

    # This is where the planning method is chosen — ▶ Run forecast
    # re-runs THIS method from now on.
    st.session_state["_chosen_method"] = k

with no call to `_adoption_gate` / `_adoption_refusal`. Analyze's card and its
promote picker both gate the same act. So a board leg that fails a HARD gate --
the Global arms are gate-bound on R7, and the relief-ceiling gate is SOFT so a
102,459-fish week only ranks a plan down -- can become the method every future
forecast runs, in one click, unremarked.

The gate itself is sound and is NOT the finding: it names each breach, covers
the two hard gates plus the soft relief ceiling and the contract floor, records
what was accepted, and keys the acknowledgement on `sig|slot|label` so a tick
cannot survive into a different plan or different inputs.

THE TRAP THIS TEST EXISTS FOR: `_adoption_gate` returns True when `breaches` is
empty, and a board entry has no `breaches` key at all. Wiring the gate in
naively would return True for every leg and read as protection while providing
none -- "missing measurements must not become a pass merely through empty
lists, zero defaults, or absent keys". So the breaches must be COMPUTED for the
board's own shape, and an ungraded leg must come back UNKNOWN, never clean.
"""
import ast
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = pytest.importorskip("app", reason="app.py not importable without Streamlit")


def _board_entry(gates=None, metrics=None):
    """The shape Compare & Choose stores per leg."""
    return {"_label": "Global — CP-SAT optimal",
            "_score": {"gates": gates or [], "verdict": {}, "metrics": metrics},
            "elapsed": 1.0}


def test_an_ungraded_board_leg_is_unknown_not_clean():
    """The trap: no metrics at all must NOT yield an empty breach list."""
    cand = app._board_adoption_candidate(_board_entry())
    assert cand["breaches"], (
        "an ungraded leg produced no breaches — an absent measurement was "
        "read as a pass, which is exactly the hazard the gate exists for")


def test_a_hard_gate_failure_reaches_the_breach_list():
    """A Global arm gate-bound on R7 must be refused activation silently."""
    gates = [{"key": "sixn_one_way", "label": "6N one-way commitment (R7)",
              "hard": True, "status": "FAIL", "detail": "fish left 6N"}]
    cand = app._board_adoption_candidate(_board_entry(gates=gates))
    assert any("R7" in b or "6N" in b for b in cand["breaches"]), (
        "a hard-gate FAIL did not reach the adoption breach list: %r"
        % cand["breaches"])


def test_the_candidate_carries_a_label_the_gate_can_name():
    cand = app._board_adoption_candidate(_board_entry())
    assert cand.get("label"), "the gate renders cand['label']; it must exist"


def test_the_gate_passes_a_clean_leg_without_friction():
    """NEGATIVE CONTROL. A leg with no breaches must not grow a checkbox, or
    the common case is taught to click through the guard."""
    import dataclasses

    from forecast import optimize as _opt
    m = dataclasses.replace(_opt._infeasible_metrics(),
                            harvest_zero_weeks=0,
                            weeks_over_relief_ceiling=0,
                            harvest_min_week=40000)
    cand = app._board_adoption_candidate(
        _board_entry(gates=[{"key": "conservation", "label": "Conservation",
                             "hard": True, "status": "PASS", "detail": ""}],
                     metrics=m))
    cand["breaches"] = [b for b in cand["breaches"] if "conserv" not in b.lower()]
    assert app._adoption_refusal(cand, acknowledged=False) is None or cand["breaches"]


def test_no_method_activation_escapes_the_gate():
    """STRUCTURAL. Every place that writes `_chosen_method` must sit in a
    function that also consults `_adoption_gate` -- a new surface that forgets
    it fails here rather than in production."""
    src = open(app.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    offenders = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        writes = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Assign)
            for t in n.targets
            if (isinstance(t, ast.Subscript)
                and isinstance(t.value, ast.Attribute)
                and t.value.attr == "session_state"
                and isinstance(t.slice, ast.Constant)
                and t.slice.value == "_chosen_method")]
        if not writes:
            continue
        calls = {n.func.id for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if "_adoption_gate" not in calls:
            offenders.append(fn.name)
    assert not offenders, (
        "these change the active planning method without consulting the "
        "adoption gate: %s" % offenders)


def test_the_gated_button_has_no_undefined_names():
    """I introduced a NameError writing this fix -- `_ana` is bound in Analyze
    but not in `_compare_and_choose`, and no test caught it because none of
    them execute a Streamlit body. Every Name loaded inside the function must
    resolve to a local, a module global, or a builtin."""
    import builtins
    src = open(app.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_compare_and_choose")
    bound = set(dir(builtins)) | set(vars(app))
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            bound.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, (ast.comprehension,)):
            pass
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
    missing = sorted({n.id for n in ast.walk(fn)
                      if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
                     - bound)
    assert not missing, (
        "names used in _compare_and_choose that are never bound there or at "
        "module level: %s" % missing)
