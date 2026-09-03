"""The contract floor is judged against the floor THAT WEEK actually had.

`min_harvest_per_week` in config/control.yaml is only the default. The operator
sets the real commitment week by week in scenario/limits.yaml, and until
2026-09-02 `_harvest_extras` compared every week to the scalar and never read
those rows.

MEASURED on the 2026-08-31 PR: the checklist reported "3 planner weeks below the
30,000-fish contract floor" while SEVEN planner weeks were below the floors the
operator had written — 119,311 fish short, worst 2026-W47 by 25,675 — all of it
in the November-December trough the operator was trying to diagnose. The gate
was hiding the exact thing it exists to surface, and the operator's own
December floors of 50,000 read as satisfied by weeks delivering 30,030.

The floors come from the workbook's OWN RunConfig snapshot, so a reused
workbook can never be judged against someone else's limits.
"""
import pytest

from forecast.analysis import _gate_harvest_floor


def _ctx(**kw):
    base = {"weeks_below_floor": 0, "min_week": 50_000.0,
            "min_harvest": 30_000.0}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# The gate's reading
# --------------------------------------------------------------------------- #
def test_names_the_per_week_basis_not_the_default():
    """Quoting '30,000' beside a per-week verdict is the conflation being
    fixed — the text must not imply one number when floors vary."""
    st, txt = _gate_harvest_floor(_ctx(
        weeks_below_floor=7, min_week=27_325.0, floors_from="workbook",
        floor_shortfall_fish=119_311.0, worst_floor_week="2026-W47",
        worst_floor_gap=25_675.0))
    assert st == "WARN"
    assert "its week's contract floor" in txt
    assert "30,000-fish" not in txt
    assert "each week's OWN floor" in txt


def test_reports_the_shortfall_and_the_worst_week():
    """'7 weeks below' is not actionable; '119,311 fish short, worst W47 by
    25,675' tells the operator where to look."""
    _st, txt = _gate_harvest_floor(_ctx(
        weeks_below_floor=7, min_week=27_325.0, floors_from="workbook",
        floor_shortfall_fish=119_311.0, worst_floor_week="2026-W47",
        worst_floor_gap=25_675.0))
    assert "119,311 fish short" in txt
    assert "2026-W47" in txt and "25,675" in txt


def test_falls_back_to_the_flat_default_and_names_it():
    """Workbooks with no snapshot (older outputs, Global's stamp-only sheet)
    keep the pre-fix behaviour — no worse than it was, and honest about which
    basis produced the number."""
    _st, txt = _gate_harvest_floor(_ctx(weeks_below_floor=2, min_week=27_000.0))
    assert "30,000-fish contract floor" in txt
    assert "each week's OWN floor" not in txt


def test_a_clean_plan_still_passes():
    st, txt = _gate_harvest_floor(_ctx(
        weeks_below_floor=0, floors_from="workbook", floor_shortfall_fish=0.0))
    assert st == "PASS"
    assert "every planner week meets" in txt


def test_no_floor_configured_is_na_not_a_pass():
    st, _txt = _gate_harvest_floor(_ctx(min_harvest=0))
    assert st == "N/A"


def test_zero_shortfall_does_not_print_a_shortfall_clause():
    _st, txt = _gate_harvest_floor(_ctx(
        weeks_below_floor=0, floors_from="workbook", floor_shortfall_fish=0.0))
    assert "fish short in total" not in txt


# --------------------------------------------------------------------------- #
# The measurement underneath it
# --------------------------------------------------------------------------- #
def test_per_week_floors_parse_from_an_embedded_snapshot():
    from tools.run_compare import _per_week_floors

    class _WB:
        sheetnames = ["RunConfig"]

    import tools.run_compare as rc
    real = rc.read_config_snapshot if hasattr(rc, "read_config_snapshot") else None
    import forecast.config_snapshot as cs
    orig = cs.read_config_snapshot
    cs.read_config_snapshot = lambda wb, **kw: {
        "scenario/limits.yaml": (
            "facility:\n"
            "- week: 2026-W50\n  metric: min_harvest_per_week\n  value: 50000.0\n"
            "- week: 2026-W51\n  metric: min_harvest_per_week\n  value: 52000.0\n"
            "- week: 2026-W50\n  metric: biomass\n  value: 3650000.0\n")}
    try:
        got = _per_week_floors(_WB())
    finally:
        cs.read_config_snapshot = orig
    assert got == {"2026-W50": 50_000.0, "2026-W51": 52_000.0}
    assert real is None or True          # keep the import meaningful


def test_a_workbook_without_a_snapshot_yields_no_floors():
    """No snapshot must degrade to the flat default, never to an exception."""
    from tools.run_compare import _per_week_floors

    class _WB:
        sheetnames = []

    import forecast.config_snapshot as cs
    orig = cs.read_config_snapshot
    cs.read_config_snapshot = lambda wb, **kw: {}
    try:
        assert _per_week_floors(_WB()) == {}
    finally:
        cs.read_config_snapshot = orig
