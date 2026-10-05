"""Weeks are SHOWN "W05 '27" in the app; the ISO label stays the key.

Operator 2026-10-05: week first, then the year, everywhere the app shows a
week -- the Excel export keeps "2027-W05". forecast/week_display wraps the
Streamlit drawing calls; these tests pin the formatter, the parser, and the
promise that only what is DRAWN changes (column keys, widget values and
selections stay ISO).
"""
from __future__ import annotations

import pandas as pd
import pytest

from forecast import week_display as W
from forecast.time_grid import (display_week, display_weeks_in_text,
                                parse_week_input)


@pytest.mark.parametrize("iso,shown", [
    ("2027-W05", "W05 '27"), ("2026-W53", "W53 '26"), ("2027-W01", "W01 '27"),
    ("2026-W5", "W05 '26"), ("B49", "B49"), ("2026-10", "2026-10"), ("", "")])
def test_display_week(iso, shown):
    assert display_week(iso) == shown


def test_text_rewrites_every_label_and_leaves_tokens_alone():
    s = ("rows cover 2026-W36..2026-W53; to 2026-W46's limit; "
         "file 2026-W40_planned.xlsx; month 2027-03")
    assert display_weeks_in_text(s) == (
        "rows cover W36 '26..W53 '26; to W46 '26's limit; "
        "file 2026-W40_planned.xlsx; month 2027-03")
    assert display_weeks_in_text(12) == 12


@pytest.mark.parametrize("typed,iso", [
    ("W05 '27", "2027-W05"), ("w5 27", "2027-W05"), ("W05 '2027", "2027-W05"),
    ("2027-W05", "2027-W05"), ("2027-W5", "2027-W05"), ("W53 '26", "2026-W53"),
    ("W53 '27", None),            # 2027 has 52 ISO weeks
    ("W00 '27", None), ("junk", None), ("", None)])
def test_parse_week_input_takes_either_form(typed, iso):
    assert parse_week_input(typed) == iso


def test_round_trip_every_week_of_two_years():
    from datetime import date, timedelta
    d = date(2026, 1, 1)
    from forecast.time_grid import iso_week_label
    while d < date(2028, 1, 1):
        iso = iso_week_label(d)
        assert parse_week_input(display_week(iso)) == iso
        d += timedelta(days=7)


def test_install_is_idempotent_and_covers_containers():
    import streamlit as st
    from streamlit.delta_generator import DeltaGenerator
    W.install(st)
    assert W.install(st) == 0                       # a rerun wraps nothing
    for name in ("markdown", "caption", "dataframe", "selectbox", "button"):
        assert getattr(getattr(DeltaGenerator, name), W._MARK, False)
        assert getattr(getattr(st, name), W._MARK, False)


def test_frame_keeps_iso_columns_and_labels_them():
    df = pd.DataFrame({"Tank": ["OG4N-45"], "2026-W53": ["B46"],
                       "2027-W01": ["B46"], "Week": ["2027-W01"]})
    tx = W._transform_for("dataframe")
    (out,), kw = tx((df,), {"column_config": {"2026-W53": {"width": "small"}}})
    # keys: never renamed (clicks + column_config + Save rely on them)
    assert list(out.columns) == list(df.columns)
    # cells: rewritten on the DRAWN copy only
    assert out["Week"].tolist() == ["W01 '27"] and df["Week"].tolist() == ["2027-W01"]
    # headers: labels, merged with the caller's own config
    cc = kw["column_config"]
    assert cc["2026-W53"] == {"width": "small", "label": "W53 '26"}
    assert cc["2027-W01"] == {"label": "W01 '27"}
    # row order is the ISO order -- the year boundary stays in place
    assert list(cc) == ["2026-W53", "2027-W01"]


def test_choice_widgets_return_iso_values():
    tx = W._transform_for("selectbox")
    args, kw = tx(("Week", ["2026-W53", "2027-W01"]), {})
    assert args[1] == ["2026-W53", "2027-W01"]       # options (= values) untouched
    assert [kw["format_func"](o) for o in args[1]] == ["W53 '26", "W01 '27"]
    args, kw = tx(("Week", [1, 2]), {"format_func": lambda o: f"2026-W4{o}"})
    assert kw["format_func"](1) == "W41 '26"          # composes with the caller's


def test_plotly_ticks_relabelled_in_iso_order():
    import plotly.graph_objects as go
    fig = go.Figure(go.Bar(x=["2026-W52", "2026-W53", "2027-W01"], y=[1, 2, 3]))
    (out,), _ = W._transform_for("plotly_chart")((fig,), {})
    assert list(out.data[0].x) == ["W52 '26", "W53 '26", "W01 '27"]
    assert list(fig.data[0].x) == ["2026-W52", "2026-W53", "2027-W01"]   # caller's fig


def test_editors_are_not_wrapped():
    """An editor returns what it shows: a rewritten cell would be SAVED back.
    The Limits grid relabels its week headers via column_config instead."""
    assert "data_editor" not in W.NAMES


def test_negative_control_without_the_layer_iso_shows():
    """The layer is what changes the text: the raw transform input is ISO."""
    args, kw = W._transform_for("caption")(("comes out 2026-W49",), {})
    assert args[0] == "comes out W49 '26"
    assert W._t("comes out 2026-W49") != "comes out 2026-W49"
