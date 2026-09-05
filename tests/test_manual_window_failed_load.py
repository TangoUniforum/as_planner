"""A failed READ of the manual-event file must never become an empty SAVE.

DEFECT (2026-09-04). `_mw_events` seeds the working set from this PR's event
file. When the loader raises, `_read_or_explain` renders a red error naming the
risk exactly right -- "do not save it over the file ... or the stored operations
are lost" -- and the working set becomes []. Then:

  * `_ok` is DISCARDED, so nothing outlives that one render;
  * the seeding block is guarded by `"mw_events" not in st.session_state`, and
    Streamlit reruns the whole script on EVERY widget interaction -- so on the
    next click the block short-circuits and the error is never re-emitted;
  * `_mw_validate` returns {} early for an empty list, so `bad` is empty;
  * `_mw_save_bar` gates Save on `bad` alone -> `disabled=bool({})` -> ENABLED.

Net: after any single click the operator sees an empty editor, no error, and a
live Save button that overwrites their hand-built operations with an empty
window. The guard is a one-shot message, not a gate.

The fix must NOT be "disable Save when the window is empty": deliberately
clearing a window and saving it is a real operation (the 🧹 Clear button exists
for exactly that). The distinction is WHY the list is empty -- a failed read, or
the operator's choice. Both halves are pinned below; the negative control is
what stops the fix from being an over-correction.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = pytest.importorskip("app", reason="app.py not importable without Streamlit")


class _Col:
    def __init__(self, rec):
        self._rec = rec

    def button(self, label, **kw):
        self._rec.append({"label": label, **kw})
        return False


class _FakeSt:
    """Enough Streamlit to run _mw_save_bar and record what it rendered."""

    def __init__(self, session=None):
        self.session_state = dict(session or {})
        self.buttons: list[dict] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.successes: list[str] = []

    def columns(self, spec):
        n = len(spec) if isinstance(spec, (list, tuple)) else int(spec)
        return [_Col(self.buttons) for _ in range(n)]

    def error(self, msg, *a, **k):
        self.errors.append(str(msg))

    def warning(self, msg, *a, **k):
        self.warnings.append(str(msg))

    def success(self, msg, *a, **k):
        self.successes.append(str(msg))

    def info(self, msg, *a, **k):
        pass

    def caption(self, msg, *a, **k):
        pass

    def rerun(self):
        pass


def _save_button(fake):
    for b in fake.buttons:
        if "Save window" in b["label"]:
            return b
    raise AssertionError("no Save button was rendered: %r"
                         % [b["label"] for b in fake.buttons])


def _run_save_bar(monkeypatch, session):
    fake = _FakeSt(session)
    monkeypatch.setattr(app, "st", fake)
    monkeypatch.setattr(app, "_pr_closing", lambda: "2026-08-31")
    app._mw_save_bar([], {})
    return fake


def test_save_is_disabled_after_a_failed_load(monkeypatch):
    """THE DEFECT. The read failed, the working set is [] for that reason, and
    Save must not be able to write it over the file."""
    fake = _run_save_bar(monkeypatch, {"_mw_load_failed": True})
    assert _save_button(fake)["disabled"] is True, (
        "Save is ENABLED after a failed read — clicking it overwrites the "
        "operator's stored operations with an empty window")


def test_the_failure_is_named_on_every_rerun(monkeypatch):
    """The one-shot message is the other half. Whatever re-renders the bar must
    restate why the window is empty, because the seeding block does not."""
    fake = _run_save_bar(monkeypatch, {"_mw_load_failed": True})
    said = " ".join(fake.errors + fake.warnings).lower()
    assert said, "a failed load renders nothing at all on a rerun"
    assert "read" in said or "load" in said, (
        "the message must say the file could not be READ, not merely that the "
        "window is empty: %r" % (fake.errors + fake.warnings))


def test_a_deliberately_cleared_window_can_still_be_saved(monkeypatch):
    """NEGATIVE CONTROL. Clearing a window and saving it is a real operation.
    A fix that simply disables Save on an empty list breaks it, and this test
    fails if that is what was done."""
    fake = _run_save_bar(monkeypatch, {})
    assert _save_button(fake)["disabled"] is False, (
        "Save is disabled for a deliberately empty window — the 🧹 Clear "
        "operation can no longer be persisted")


def test_a_failed_load_is_recorded_rather_than_discarded(monkeypatch):
    """Upstream half: _mw_events must persist the failure, or the save bar has
    nothing to gate on."""
    import forecast.manual_events as _me

    def _boom(*a, **k):
        raise ValueError("malformed YAML")

    monkeypatch.setattr(_me, "load_manual_events", _boom)
    fake = _FakeSt({"_pr_key": "pr-1"})
    monkeypatch.setattr(app, "st", fake)
    monkeypatch.setattr(app, "_pr_closing", lambda: "2026-08-31")
    monkeypatch.setattr(app, "_mw_bump_grid", lambda: None)
    out = app._mw_events()
    assert out == [], "a failed load must not fabricate events"
    assert fake.session_state.get("_mw_load_failed") is True, (
        "_mw_events discarded the read failure — nothing downstream can know "
        "the empty window is an error rather than a choice")
