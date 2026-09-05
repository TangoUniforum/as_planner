"""The compare board's gates are a different shape, and I crashed the app on it.

DEFECT, mine, shipped 2026-09-04 in `_board_adoption_candidate` and hit by the
operator on 2026-09-05:

    AttributeError: 'str' object has no attribute 'get'
      app.py _compare_and_choose -> _board_adoption_candidate
      analysis.adoption_breaches:  for g in candidate.get("gates") ...  g.get("hard")

Analyze grades into a LIST OF DICTS (`key`/`status`/`hard`/`label`/`detail`).
The compare board builds something else entirely (`_board_score`):

    {"Conserves": bool, "Fully placed": bool, "No empty week": bool,
     "Under cap": bool}

A dict of four human labels to booleans. Iterating it yields the KEYS — strings
— so `g.get(...)` blows up. There is no `key`, `status` or `hard` in it, so the
Analyze shape cannot be recovered from it at all.

WHY NO TEST CAUGHT IT. My six tests for that change built the board entry from
my own assumption about its shape rather than from `_board_score`'s real
output, so they agreed with the bug. The render smoke test did not catch it
either: this code sits inside the per-leg loop, and the test renders Decide with
an EMPTY board, so the loop body never executes. State-dependent branches are
still uncovered — that limit is real and now demonstrated twice.

THE FIX must not become "pass an empty gate list and move on". Two of the four
board flags (`Conserves`, `No empty week`) map to hard gates, and `Fully placed`
means dropped fish. Silently dropping them is the exact "missing measurement
becomes a pass" hazard the gate exists to prevent. So a False flag becomes a
NAMED breach, and the unknown-shape case is treated as UNKNOWN, never clean.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = pytest.importorskip("app", reason="app.py not importable without Streamlit")

# EXACTLY what app._board_score returns for `gates` — copied from that function,
# not from what this test would find convenient.
CLEAN = {"Conserves": True, "Fully placed": True,
         "No empty week": True, "Under cap": True}


def _leg(gates=None, metrics=None, verdict=None):
    return {"_label": "Global — CP-SAT optimal",
            "_score": {"gates": dict(CLEAN if gates is None else gates),
                       "verdict": verdict or {}, "metrics": metrics},
            "elapsed": 1.0}


def test_the_real_board_shape_does_not_raise():
    """THE CRASH. This is the shape the operator's session actually holds."""
    cand = app._board_adoption_candidate(_leg())
    assert isinstance(cand.get("breaches"), list)


def test_a_false_board_flag_becomes_a_named_breach():
    """`Conserves` and `No empty week` are hard rules; `Fully placed` means
    fish were dropped. None may vanish because the shape is inconvenient."""
    for flag in ("Conserves", "Fully placed", "No empty week"):
        g = dict(CLEAN)
        g[flag] = False
        cand = app._board_adoption_candidate(_leg(gates=g))
        assert any(flag.lower() in b.lower() for b in cand["breaches"]), (
            "%s=False did not reach the breach list: %r" % (flag, cand["breaches"]))


def test_an_ungraded_leg_is_still_unknown_not_clean():
    """Unchanged contract from the original fix: no metrics at all must never
    read as a clean sweep."""
    cand = app._board_adoption_candidate(_leg())
    assert cand["breaches"], "an ungraded leg produced no breaches"


def test_an_unrecognised_gate_shape_is_not_silently_passed():
    """If the board's shape changes again, the failure must be loud."""
    cand = app._board_adoption_candidate(
        {"_label": "x", "_score": {"gates": "not-a-shape-we-know",
                                   "verdict": {}, "metrics": None}})
    assert cand["breaches"], "an unreadable gate shape produced no breaches"


def test_the_analyze_list_shape_still_works():
    """NEGATIVE CONTROL. The adapter must keep handling the list-of-dicts form,
    so a future caller passing an Analyze candidate is unaffected."""
    gates = [{"key": "sixn_one_way", "label": "6N one-way commitment (R7)",
              "hard": True, "status": "FAIL", "detail": "fish left 6N"}]
    cand = app._board_adoption_candidate(
        {"_label": "y", "_score": {"gates": gates, "verdict": {}, "metrics": None}})
    assert any("R7" in b or "6N" in b for b in cand["breaches"]), cand["breaches"]


def test_the_label_survives():
    assert app._board_adoption_candidate(_leg()).get("label")
