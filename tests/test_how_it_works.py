"""The How-it-works page must tell the truth about THIS install.

The page is static prose, but its numbers (harvest floor/target/ceiling,
caps, the handling budget) are read live from config/control.yaml via
app._hiw_knobs — so retuning a knob can never leave the rulebook describing
a facility that no longer exists. These tests pin that contract.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = pytest.importorskip("app", reason="app.py not importable without Streamlit")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.skipif(
    not os.path.isdir(os.path.join(ROOT, "config")),
    reason="config/ not seeded",
)


def test_page_numbers_track_the_live_control_config():
    """DEFECT CLASS this locks out: a hardcoded rulebook. The page's numbers
    must equal whatever control.yaml currently says — the 55k processing
    limit, the relief fraction and its DERIVED ceiling, the 30k floor, the
    15-move handling budget, the caps."""
    from forecast.config_io import load_control
    c = load_control(os.path.join(ROOT, "config"))
    k = app._hiw_knobs()
    assert k["min_hv"] == float(c.min_harvest_per_week)
    assert k["max_hv"] == float(c.max_harvest_per_week)
    assert k["relief_pct"] == float(c.harvest_relief_pct)
    assert k["ceiling_hv"] == pytest.approx(
        float(c.max_harvest_per_week) * (1.0 + float(c.harvest_relief_pct)))
    assert k["moves"] == int(c.max_transfers_per_week)
    assert k["bio_cap"] == float(c.max_biomass_kg)
    assert k["feed_cap"] == float(c.max_feed_per_day_kg)
    assert k["min_tank"] == float(c.min_tank_control)
    assert k["min_wt"] == float(c.min_harvest_weight_g)


def test_floor_limit_ceiling_ordering_in_the_shipped_config():
    """The limit+relief model's own invariant, as the page states it: the
    contract floor <= the processing limit <= the derived relief ceiling
    (30,000 <= 55,000 <= 60,500 on the shipped config). The ceiling is
    strictly above the limit whenever a relief band exists at all."""
    k = app._hiw_knobs()
    assert k["min_hv"] <= k["max_hv"] <= k["ceiling_hv"]
    if k["relief_pct"] > 0:
        assert k["ceiling_hv"] > k["max_hv"]


def test_knobs_survive_a_missing_config(monkeypatch, tmp_path):
    """The page must render before any config is seeded (first-launch state):
    _hiw_knobs falls back to the documented defaults instead of raising."""
    monkeypatch.setattr(app, "CONFIG_DIR", tmp_path / "nonexistent")
    k = app._hiw_knobs()
    assert k["min_hv"] > 0 and k["ceiling_hv"] >= k["max_hv"] > 0
