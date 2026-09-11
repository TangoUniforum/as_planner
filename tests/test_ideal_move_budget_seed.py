"""Ideal → Tank & system limits: the weekly move budget box.

Control's `max_transfers_per_week` 0 means "budget off", but the box has
min_value=1 (the engine's override must be a positive whole number) and
Streamlit raises when a widget's value is under its min_value. The box used
to be seeded with the Control value as-is, so a budget-off Control broke the
Ideal page. Now it starts at max(1, Control) and — like every box on the page
— only a value MOVED from its seed becomes an override, so an untouched box
keeps Control's budget-off run.

`_ideal_limits_table` is lifted out of app.py and driven with a stand-in for
Streamlit that raises exactly where Streamlit would.
"""
import ast
import dataclasses
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"

pytestmark = pytest.mark.skipif(
    not (ROOT / "config" / "control.yaml").exists(),
    reason="needs a seeded config")


class _State(dict):
    pass


class _ColumnConfig:
    @staticmethod
    def Column(**kw):
        return kw

    @staticmethod
    def NumberColumn(**kw):
        return kw


class _FakeSt:
    """The calls _ideal_limits_table makes; number_input raises below its
    min_value, as Streamlit does (StreamlitValueBelowMinError)."""

    def __init__(self):
        self.session_state = _State()
        self.column_config = _ColumnConfig()
        self.captions, self.warnings, self.helps = [], [], {}

    def data_editor(self, df, **kw):
        return df

    def number_input(self, label, min_value=None, max_value=None, step=None,
                     key=None, help=None, value=None):
        self.helps[key] = help
        v = self.session_state[key] if key in self.session_state else value
        if v is None:
            v = min_value
        if min_value is not None and v < min_value:
            raise ValueError(f"{label}: value {v} is below min_value "
                             f"{min_value}")
        self.session_state[key] = v
        return v

    def caption(self, text, **kw):
        self.captions.append(text)

    def warning(self, text, **kw):
        self.warnings.append(text)


def _table(fake):
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    want = {"_ideal_limits_table", "_ideal_limit_seeds", "_ideal_default"}
    body = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in want]
    assert sorted(n.name for n in body) == sorted(want)
    ns = {"st": fake, "os": os, "_ROOT": ROOT}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(APP), "exec"), ns)
    return ns["_ideal_limits_table"]


@pytest.fixture(scope="module")
def live():
    from forecast.config_io import load_config
    control, _t, facility = load_config(str(ROOT / "config"))
    return control, facility


def _ctx(live, moves):
    control, facility = live
    return {"control": dataclasses.replace(control,
                                           max_transfers_per_week=moves),
            "facility": facility}


def test_the_stand_in_raises_where_streamlit_would():
    """Negative control: the old seed (Control's 0 under min_value=1) is
    refused by the stand-in, so the tests below would have caught it."""
    with pytest.raises(ValueError, match="below min_value"):
        _FakeSt().number_input("Weekly move budget (moves)", min_value=1,
                               key="k", value=0)


def test_a_budget_off_control_draws_the_box_and_keeps_the_budget_off(live):
    fake = _FakeSt()
    _dens, _sys, ctl_ov = _table(fake)(_ctx(live, 0), "t")
    assert fake.session_state["t_moves"] == 1
    assert ctl_ov == {}                    # untouched: Control's 0 stands
    assert any("0 = off" in c for c in fake.captions), fake.captions
    assert "Control: 0" in fake.helps["t_moves"]


def test_moving_the_box_from_a_budget_off_seed_is_an_override(live):
    fake = _FakeSt()
    table = _table(fake)
    table(_ctx(live, 0), "t")
    fake.session_state["t_moves"] = 5      # the operator types 5
    _dens, _sys, ctl_ov = table(_ctx(live, 0), "t")
    assert ctl_ov == {"max_transfers_per_week": 5}


def test_a_real_budget_is_the_seed_and_only_a_move_overrides(live):
    fake = _FakeSt()
    table = _table(fake)
    _d, _s, ctl_ov = table(_ctx(live, 15), "t")
    assert fake.session_state["t_moves"] == 15 and ctl_ov == {}
    assert not any("0 = off" in c for c in fake.captions)
    fake.session_state["t_moves"] = 20
    _d, _s, ctl_ov = table(_ctx(live, 15), "t")
    assert ctl_ov == {"max_transfers_per_week": 20}
