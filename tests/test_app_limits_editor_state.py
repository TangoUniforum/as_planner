"""Guard for the Limits editor's session_state initialisation contract.

DEFECT (2026-09-03, hit by the operator mid-session): the Limits tab raised

    KeyError: st.session_state has no key "_lim_weeks"
    app.py -> _config_editor() -> _edit_limits() -> st.session_state["_lim_weeks"]

`_edit_limits` initialises SIX session keys together, then reads them. The
initialisation was guarded by ONE sentinel (`"sysdef_grid" not in
st.session_state`), while `_clear_all_editor_state` — which runs after a config
import — cleared only FOUR of the six and left `sysdef_grid` behind. So the next
render saw the sentinel, concluded "already initialised", skipped the rebuild,
and then read `_lim_weeks`, which had just been popped. Raw traceback, dead tab.

This is the "state outlives its inputs" shape that produced a whole batch of
app.py defects. It cannot be caught by `py_compile`, and calling the editor
needs a Streamlit runtime, so both halves of the contract are checked
STATICALLY here:

  1. every key the guard tests is a key the block actually initialises, and
     vice versa — no key may be initialised behind a sentinel that does not
     mention it;
  2. every one of those keys is dropped by `_clear_all_editor_state`, so no
     clear-path can leave the editor half-built.

Either half alone is insufficient: (1) without (2) lets a future clear-path
reintroduce the bug, and (2) without (1) lets a future key be added to the
block without joining the guard.
"""
import ast
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = pytest.importorskip("app", reason="app.py not importable without Streamlit")


def _tree():
    return ast.parse(open(app.__file__, encoding="utf-8").read())


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found in app.py")


def _session_key(node):
    """`st.session_state["k"]` -> "k", else None."""
    if not isinstance(node, ast.Subscript):
        return None
    v = node.value
    if not (isinstance(v, ast.Attribute) and v.attr == "session_state"):
        return None
    s = node.slice
    return s.value if isinstance(s, ast.Constant) and isinstance(s.value, str) else None


def _guard_block(fn):
    """The `if any(k not in st.session_state for k in (...)):` init block.

    Returns (keys named in the guard, keys assigned in its body).
    """
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        named = {c.value for c in ast.walk(node.test)
                 if isinstance(c, ast.Constant) and isinstance(c.value, str)}
        if not named:
            continue
        mentions_state = any(
            isinstance(a, ast.Attribute) and a.attr == "session_state"
            for a in ast.walk(node.test))
        if not mentions_state:
            continue
        assigned = set()
        for stmt in node.body:
            for t in ast.walk(stmt):
                if isinstance(t, ast.Assign):
                    for tgt in t.targets:
                        k = _session_key(tgt)
                        if k:
                            assigned.add(k)
        if assigned:
            return named, assigned
    raise AssertionError(
        "_edit_limits() no longer has a session_state init block guarded by a "
        "membership test — if the initialisation moved, move this guard too")


def test_limits_editor_guard_names_every_key_it_initialises():
    """A key initialised behind a sentinel that does not name it is a key that
    a partial clear can delete without triggering the rebuild."""
    named, assigned = _guard_block(_func(_tree(), "_edit_limits"))
    missing = assigned - named
    assert not missing, (
        "_edit_limits() initialises %s inside a block whose guard does not "
        "test %s. Add them to the guard tuple: a caller that drops one of them "
        "leaves the editor half-built and the next read raises KeyError."
        % (sorted(assigned), sorted(missing)))
    stale = named - assigned
    assert not stale, (
        "the _edit_limits() init guard tests %s but the block no longer "
        "initialises %s — a guard that names a key nobody sets rebuilds "
        "forever. Drop them from the guard." % (sorted(named), sorted(stale)))


def test_clearing_editor_state_drops_every_limits_editor_key():
    """The exact defect: `_clear_all_editor_state` cleared four of the six keys
    and left the sentinel behind, so the rebuild never fired."""
    tree = _tree()
    _named, assigned = _guard_block(_func(tree, "_edit_limits"))
    clear = _func(tree, "_clear_all_editor_state")
    cleared = {c.value for c in ast.walk(clear)
               if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    missing = assigned - cleared
    assert not missing, (
        "_clear_all_editor_state() does not drop %s, which _edit_limits() "
        "initialises. A config import would leave those keys stale (or, if a "
        "sibling key IS dropped, leave the editor half-built) — this is the "
        "KeyError on '_lim_weeks' that took the Limits tab down."
        % sorted(missing))


def test_lim_weeks_is_read_only_after_the_guarded_block():
    """Belt and braces: the crash was a READ of `_lim_weeks`. Whatever else
    changes, that read must stay inside `_edit_limits`, after the block that
    sets it — never in a helper that could be reached first."""
    tree = _tree()
    fn = _func(tree, "_edit_limits")
    reads = [n for n in ast.walk(fn) if _session_key(n) == "_lim_weeks"
             and isinstance(n.ctx, ast.Load)]
    assert reads, "_edit_limits() no longer reads _lim_weeks — retire this guard"
    _named, assigned = _guard_block(fn)
    assert "_lim_weeks" in assigned, (
        "_lim_weeks is read in _edit_limits() but no longer initialised in its "
        "guarded block — the read can now precede any write")
