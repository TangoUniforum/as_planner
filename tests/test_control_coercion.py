"""Control knobs must arrive as their DECLARED types, whatever the YAML says.

Dataclasses do not validate, so a numeric knob supplied as a string was stored
as a string, written back to YAML quoted, and only failed much later inside
arithmetic — far from the cause. Two real routes in: the app's Control editor
picks its widget from the CURRENT value, so a knob that is presently null
renders as a text box; and control.yaml can be hand-edited.

Unlike test_app_helpers.py this covers the ENGINE path — control_from_dict runs
on every forecast, CLI or app.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forecast.config_io import (control_from_dict, control_to_dict,  # noqa: E402
                                load_control)

CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")


@pytest.fixture(scope="module")
def base():
    return control_to_dict(load_control(CONFIG_DIR))


def test_real_config_round_trips_unchanged(base):
    """The coercion must be behaviour-preserving on the shipped config —
    otherwise it is not a fix, it is a change."""
    assert control_to_dict(control_from_dict(base)) == base


def test_numeric_knob_given_as_string_becomes_a_number(base):
    """DEFECT: a null-valued numeric knob rendered as a text box, so typing in
    it saved "5" and blew up later in arithmetic."""
    c = control_from_dict({**base, "harvest_level_target": "5"})
    assert c.harvest_level_target == 5.0
    assert isinstance(c.harvest_level_target, float)


def test_optional_int_knob_given_as_string_becomes_an_int(base):
    c = control_from_dict({**base, "sixn_transition_weeks": "3"})
    assert c.sixn_transition_weeks == 3
    assert isinstance(c.sixn_transition_weeks, int)


def test_int_knob_tolerates_a_float_spelling(base):
    assert control_from_dict({**base, "horizon_weeks": "130.0"}).horizon_weeks == 130


@pytest.mark.parametrize("raw,expected",
                         [("true", True), ("False", False), ("yes", True),
                          ("0", False), (1, True)])
def test_bool_knob_accepts_yaml_ish_spellings(base, raw, expected):
    assert control_from_dict({**base, "harvest_level_load": raw}).harvest_level_load \
        is expected


def test_blank_on_an_optional_knob_means_unset(base):
    assert control_from_dict({**base, "harvest_level_target": ""}) \
        .harvest_level_target is None


def test_blank_on_a_required_knob_is_a_loud_error(base):
    """Silently coercing this to None would push the failure into the engine,
    which is the exact behaviour being fixed."""
    with pytest.raises(ValueError, match="max_biomass_kg"):
        control_from_dict({**base, "max_biomass_kg": ""})


def test_unparseable_number_names_the_knob_and_the_value(base):
    with pytest.raises(ValueError, match="max_biomass_kg"):
        control_from_dict({**base, "max_biomass_kg": "not a number"})


def test_string_knobs_are_left_alone(base):
    """hybrid_follow / placement_method are genuinely strings — coercion must
    not mangle them."""
    c = control_from_dict({**base, "hybrid_follow": "full",
                           "placement_method": "greedy"})
    assert c.hybrid_follow == "full" and c.placement_method == "greedy"


def test_every_registered_method_builds_a_valid_control(base):
    """control.yaml ships the hybrid ON and run_method layers overrides on top,
    so each method's effective config must still construct."""
    from forecast import methods as M
    for key, m in sorted(M.REGISTRY.items()):
        control_from_dict({**base, **(m.overrides or {})})


def test_comparison_arms_are_pinned_off_not_inheriting_the_hybrid(base):
    """REGRESSION GUARD: control.yaml turns the hybrid on globally. If the
    controller arms ever stop pinning hybrid_follow off they become the hybrid,
    and every controller-vs-hybrid comparison silently runs the same plan twice
    — reporting "no difference" for a real effect. That mistake has already
    been made once on this project (grade-to-min, 2026-08-03)."""
    from forecast import methods as M
    for key in ("controller", "controller-lns"):
        eff = control_from_dict({**base, **(M.REGISTRY[key].overrides or {})})
        assert eff.hybrid_follow == "off", (
            f"{key} must pin hybrid_follow off — control.yaml enables it globally"
        )
    assert control_from_dict(
        {**base, **M.REGISTRY["controller-hybrid"].overrides}).hybrid_follow == "full"
