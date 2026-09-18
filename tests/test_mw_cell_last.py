"""The manual-window grid's THIRD number: density, or count in thousands.

Operator ask 2026-09-18: read stocking levels straight off the Starting-setup
grid without clicking each tank. The toggle changes only that number — the cell
colour still shades by density-vs-cap, so a full tank still reads red in Fill
mode whichever number is shown.
"""
from __future__ import annotations

import re
from datetime import date

import pytest

pytest.importorskip("streamlit")

from forecast.placement import BatchLocationRow          # noqa: E402
from forecast.state import FacilityState, TankState      # noqa: E402

import app                                               # noqa: E402

WK = "2026-W36"


def _state():
    return FacilityState(date(2026, 9, 1), [
        TankState("OG3N-31", 31, "OG3N", 1720.0, 85.0, 1000.0, "OG"),
        TankState("OG3S-36", 36, "OG3S", 1720.0, 85.0, 1000.0, "OG"),
    ])


def _row(tank, loc, sysid, count, wt_g, dens):
    return BatchLocationRow(
        week_label=WK, week_start=date(2026, 9, 1), batch_id="B45", tank_id=tank,
        location_id=loc, system_id=sysid, count=count, avg_wt_g=wt_g,
        biomass_kg=count * wt_g / 1000.0, density_kg_m3=dens, stage="SW")


def _rows():
    return [_row(31, "OG3N-31", "OG3N", 45_000.0, 3_100.0, 81.1),
            _row(36, "OG3S-36", "OG3S", 7_128.0, 5_300.0, 22.0)]


def _cells(last=None):
    kw = {} if last is None else {"last": last}
    styler, ylabels, _t = app._mw_grid(_state(), _rows(), [WK], color_by="fill", **kw)
    df = styler.data
    return {y: str(df.loc[y, WK]) for y in ylabels}, styler


def test_density_is_the_default_third_number():
    cells, _s = _cells()
    assert cells["OG3N-31"] == "B45 · 3.10kg · 81"
    assert cells["OG3S-36"] == "B45 · 5.30kg · 22"


def test_count_shows_thousands_and_no_density():
    cells, _s = _cells("count")
    assert cells["OG3N-31"] == "B45 · 3.10kg · 45k"        # 45,000 fish
    assert cells["OG3S-36"] == "B45 · 5.30kg · 7.1k"       # a small tank keeps a decimal
    assert "81" not in cells["OG3N-31"], "density must not be shown in count mode"


def test_density_mode_shows_no_count_token():
    cells, _s = _cells("density")
    assert not any("k ·" in c or c.endswith("k") for c in cells.values())


def test_the_colour_does_not_depend_on_the_toggle():
    """Fill mode still shades by density-vs-cap in either mode."""
    _c1, s1 = _cells("density")
    _c2, s2 = _cells("count")
    bg = lambda s: sorted(re.findall(r"background-color:\s*([^;\"]+)", s.to_html()))
    assert bg(s1) == bg(s2) and bg(s1), bg(s1)
