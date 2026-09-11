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

import shutil
from pathlib import Path

import pytest
import yaml

pytest.importorskip("streamlit")

import app                                                    # noqa: E402
from forecast import analysis as _ana                         # noqa: E402
from forecast import optimize                                 # noqa: E402
from forecast.config_io import control_from_dict, control_to_dict  # noqa: E402


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


# --- the ⭐ Promote confirmation (2026-09-11) --------------------------------
# It said "▶ Run forecast and the ⚡ Quick run card now use this plan" every
# time, but Promote never clears a session pick and a session pick wins above.
# With a pick standing, the next ▶ Run forecast ran the PICK — the 2026-09-08
# incident's shape, with a message that said otherwise.

#
# THE SECOND DEFECT (verifier, 2026-09-11). The fix above compared the pick
# with the promoted knobs directly and warned whenever the promoted plan had
# knobs ("the same method, but without the promoted knobs ... reload the page
# to switch"). In the ✅ Adopt then ⭐ Promote flow that was FALSE: Adopt
# merges the winner's knobs into control.yaml and picks the same method, so
# the pick already runs the promoted plan and a reload switches nothing. The
# old grid never read control.yaml, so it could not see this. Ground truth now
# is the Control each run would load, built the way ▶ Run forecast builds it.

_NOW_USE = "now use this plan"


@pytest.fixture
def saved_config(tmp_path):
    """A throwaway config dir holding a copy of the app's control.yaml — the
    file ▶ Run forecast reads. Tests write into THIS copy, never config/."""
    d = tmp_path / "config"
    d.mkdir()
    shutil.copy(app.CONFIG_DIR / "control.yaml", d / "control.yaml")
    return d


def _adopt(cfg_dir, knobs):
    """✅ Adopt's own write: the winner's knobs merged into control.yaml."""
    optimize.save_overrides_to_config(str(cfg_dir), knobs)


def _control_run_forecast_loads(cfg_dir, key, overrides):
    """The Control ▶ Run forecast would load for (method key, overrides),
    through the SAME steps: optimize.config_dir_with_overrides for promoted
    knobs, then _run_with_workbook_bytes' pin layer, then the Control loader."""
    run_dir = (optimize.config_dir_with_overrides(str(cfg_dir), overrides)
               if overrides else str(cfg_dir))
    try:
        raw = yaml.safe_load(Path(run_dir, "control.yaml").read_text()) or {}
    finally:
        if overrides:
            shutil.rmtree(Path(run_dir).parent, ignore_errors=True)
    m = (app._AS_CONFIGURED if key == app._AS_CONFIGURED.key
         else app._METHODS.get(key) or app._METHODS["controller"])
    raw.update(m.overrides)
    return control_to_dict(control_from_dict(raw))


def test_promote_without_a_pick_says_run_forecast_uses_it(saved_config):
    lvl, txt = app._promote_message("Tuned A", "controller",
                                    {"chronic_pressure_weeks": 9}, None,
                                    config_dir=saved_config)
    assert lvl == "success" and _NOW_USE in txt and "Tuned A" in txt


@pytest.mark.parametrize("method", ["controller", "as-configured",
                                    "controller-hybrid"])
def test_adopt_then_promote_the_same_winner_says_run_forecast_uses_it(
        saved_config, method):
    """The verifier's flow: control.yaml already holds the knobs and the pick
    is the same method, so the pick IS the promoted plan."""
    knobs = {"chronic_pressure_weeks": 9, "plan_tank_feasibility": False}
    _adopt(saved_config, knobs)
    lvl, txt = app._promote_message("Tuned A", method, knobs, method,
                                    config_dir=saved_config)
    assert lvl == "success" and _NOW_USE in txt, txt
    assert "Reload" not in txt and "without the promoted knobs" not in txt


def test_one_knob_different_from_the_saved_config_is_named(saved_config):
    _adopt(saved_config, {"chronic_pressure_weeks": 9,
                          "plan_tank_feasibility": False})
    lvl, txt = app._promote_message(
        "Tuned A", "controller",
        {"chronic_pressure_weeks": 10, "plan_tank_feasibility": False},
        "controller", config_dir=saved_config)
    assert lvl == "warning" and _NOW_USE not in txt
    assert "`chronic_pressure_weeks` is `9` in this session's run" in txt
    assert "`10` in the promoted plan" in txt
    assert "plan_tank_feasibility" not in txt    # equal knobs are not named
    assert "still runs" in txt and "Reload the page" in txt


def test_a_different_picked_method_is_named(saved_config):
    knobs = {"chronic_pressure_weeks": 9}
    _adopt(saved_config, knobs)
    lvl, txt = app._promote_message("Tuned A", "controller", knobs,
                                    "controller-lns", config_dir=saved_config)
    assert lvl == "warning" and _NOW_USE not in txt
    assert app._method_obj("controller-lns").label in txt
    assert app._method_obj("controller").label in txt
    assert "the method" in txt
    assert "chronic_pressure_weeks" not in txt   # that knob is the same
    assert "still runs" in txt and "Reload the page" in txt


def test_a_promoted_knob_the_method_pins_is_not_a_difference(saved_config):
    """The plain controller pins hybrid_follow off over control.yaml AND over
    the promoted knobs, so both runs run it off — the same plan."""
    _adopt(saved_config, {"hybrid_follow": "off"})
    lvl, txt = app._promote_message("Tuned A", "controller",
                                    {"hybrid_follow": "full"}, "controller",
                                    config_dir=saved_config)
    assert lvl == "success" and _NOW_USE in txt, txt


def test_an_unreadable_control_is_said_not_guessed(tmp_path):
    lvl, txt = app._promote_message("Tuned A", "controller",
                                    {"chronic_pressure_weeks": 9}, "controller",
                                    config_dir=tmp_path / "no_such_config")
    assert lvl == "warning" and _NOW_USE not in txt
    assert "could not be read" in txt and "was not checked" in txt
    assert "`chronic_pressure_weeks`" in txt


def test_same_method_and_no_knobs_needs_no_control(tmp_path):
    lvl, txt = app._promote_message("Tuned A", "controller", {}, "controller",
                                    config_dir=tmp_path / "no_such_config")
    assert lvl == "success" and _NOW_USE in txt


@pytest.mark.parametrize("pick", [None, "controller", "controller-lns",
                                  "as-configured", "controller-hybrid"])
@pytest.mark.parametrize("adopted", [{}, {"chronic_pressure_weeks": 9}])
@pytest.mark.parametrize("method, knobs", [
    ("controller", {}), ("controller", {"chronic_pressure_weeks": 9}),
    ("controller", {"hybrid_follow": "full"}),
    ("controller-hybrid", {}), ("as-configured", {"hybrid_follow": "off"}),
    ("as-configured", {"chronic_pressure_weeks": 9})])
def test_the_promote_message_agrees_with_what_run_forecast_runs(
        clean_session, monkeypatch, saved_config, pick, adopted, method,
        knobs):
    """Ground truth: after the promote, _effective_method says which method
    and knobs ▶ Run forecast uses, and the Control it would load is built
    through the run's own steps on the saved control.yaml. The message may
    claim ▶ Run forecast uses the plan only when that run is the promoted
    plan's run: same method, same Control."""
    if adopted:
        _adopt(saved_config, adopted)
    _promote(monkeypatch, method, knobs)
    if pick:
        app.st.session_state["_chosen_method"] = pick
    key, overrides, _ = app._effective_method()
    runs_it = (key == method
               and _control_run_forecast_loads(saved_config, key, overrides)
               == _control_run_forecast_loads(saved_config, method, knobs))
    lvl, txt = app._promote_message("X", method, knobs, pick,
                                    config_dir=saved_config)
    assert (_NOW_USE in txt) == runs_it, (pick, adopted, method, knobs, txt)
    assert (lvl == "success") == runs_it


def test_promote_shows_the_message_built_from_the_session_pick():
    """The button path uses the helper with the live pick and the config dir
    ▶ Run forecast reads, not a fixed text."""
    import inspect
    src = inspect.getsource(app)
    body = src[src.index("        def _promote(cand, note_prefix):"):]
    body = body[:body.index("\n        if a2.button(")]
    assert "_promote_message(" in body
    assert 'st.session_state.get("_chosen_method")' in body
    assert "config_dir=CONFIG_DIR" in body
    assert _NOW_USE not in body               # the only copy is in the helper
