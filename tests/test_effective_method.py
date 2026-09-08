"""What ▶ Run forecast ACTUALLY runs — `app._effective_method`.

THE INCIDENT (2026-09-08). The operator picked "Controller — reactive greedy"
on the Compare engines board, restarted Streamlit, then pressed
"⭐ Promote as Quick-run default" on it. Both runs afterwards came out as
Controller — hybrid, and the workbook's own RunConfig stamp proved it:
`hybrid_follow: full` against a config that says `off`.

Neither was a bug in the engine. Two separate mechanisms, both working as
written:
  * a session pick lives in st.session_state and a RESTART clears it;
  * "Promote" deliberately only set what the ⚡ Quick run card replayed — its
    own tooltip says "Promoting changes nothing about the current run".
So the promoted default was durable but inert, and the session pick was live
but fragile. The operator had no way to express "run this from now on".

THE FIX. Promoting a default now decides the default, and it survives a
restart. Precedence: session pick > promoted default > _DEFAULT_METHOD.

AND ITS KNOBS TRAVEL WITH IT. A promoted plan is method + knobs; running the
method against today's config reproduces a DIFFERENT plan. That hazard is
called out twice in app.py's own Adopt/Promote comments, and this is the third
place it could have bitten. The overrides are layered into a throwaway config
dir at run time — config/control.yaml is never written.
"""
from __future__ import annotations

import pytest

pytest.importorskip("streamlit")

import app                                                    # noqa: E402
from forecast import analysis as _ana                         # noqa: E402


@pytest.fixture
def clean_session(monkeypatch):
    """A plain dict for session_state, and no promoted default by default."""
    monkeypatch.setattr(app.st, "session_state", {})
    monkeypatch.setattr(_ana, "load_promoted_default", lambda _cd: None)


def _promote(monkeypatch, method, overrides=None, ts="2026-09-08T13:40:48"):
    monkeypatch.setattr(_ana, "load_promoted_default", lambda _cd: {
        "method": method, "overrides": overrides or {}, "promoted_ts": ts})


def test_with_nothing_set_it_is_the_app_default(clean_session):
    key, overrides, source = app._effective_method()
    assert key == app._DEFAULT_METHOD
    assert overrides == {}
    assert "default" in source


def test_a_promoted_default_decides_the_run(clean_session, monkeypatch):
    """The incident: this used to return the hybrid and spend a run."""
    _promote(monkeypatch, "controller")
    key, _, source = app._effective_method()
    assert key == "controller"
    assert key != app._DEFAULT_METHOD           # the whole point
    assert "promoted" in source


def test_the_promoted_knobs_travel_with_the_method(clean_session, monkeypatch):
    """Method alone reproduces a DIFFERENT plan — the knobs are not optional."""
    _promote(monkeypatch, "controller",
             {"chronic_pressure_weeks": 6, "hybrid_follow": "off"})
    key, overrides, _ = app._effective_method()
    assert key == "controller"
    assert overrides == {"chronic_pressure_weeks": 6, "hybrid_follow": "off"}


def test_a_session_pick_beats_a_promoted_default(clean_session, monkeypatch):
    """Adopt / 'Use this plan' is the more recent, more specific intent."""
    _promote(monkeypatch, "controller")
    app.st.session_state["_chosen_method"] = "controller-lns"
    key, overrides, source = app._effective_method()
    assert key == "controller-lns"
    assert overrides == {}                      # the session pick brings none
    assert "session" in source


def test_the_source_is_reported_so_the_caption_can_say_why(clean_session,
                                                           monkeypatch):
    """The operator must be able to see WHERE the method came from."""
    _promote(monkeypatch, "controller", ts="2026-09-08T13:40:48")
    assert "2026-09-08T13:40:48" in app._effective_method()[2]


def test_a_promoted_record_without_a_method_is_ignored(clean_session,
                                                       monkeypatch):
    """A half-written defaults file must not silently pin something odd."""
    monkeypatch.setattr(_ana, "load_promoted_default",
                        lambda _cd: {"overrides": {"a": 1}})
    assert app._effective_method()[0] == app._DEFAULT_METHOD


def test_an_unreadable_defaults_file_falls_back_instead_of_crashing(
        clean_session, monkeypatch):
    def _boom(_cd):
        raise OSError("permission denied")
    monkeypatch.setattr(_ana, "load_promoted_default", _boom)
    key, overrides, _ = app._effective_method()
    assert key == app._DEFAULT_METHOD and overrides == {}


def test_the_returned_overrides_are_a_copy(clean_session, monkeypatch):
    """A caller mutating them must not corrupt the promoted record."""
    stored = {"chronic_pressure_weeks": 6}
    _promote(monkeypatch, "controller", stored)
    _, overrides, _ = app._effective_method()
    overrides["chronic_pressure_weeks"] = 99
    assert stored["chronic_pressure_weeks"] == 6
