"""Per-week grow-out density cap (operator 2026-10-26: "density limit of 85 till
we hit 4000 standing then bump it up to 95 to get to 4200").

A limits.yaml facility row `og_density_kg_m3` holds every OG1-5 / 6S tank to
that density in that week instead of its facility.yaml cap. 6N keeps its own
rule (no cap while it is the purge station), FW is untouched, and a limits
file with no such rows plans exactly as before (the plan-identity gate).
"""
from __future__ import annotations

import contextlib
import io
import shutil
from pathlib import Path

import pytest
import yaml

from conftest import copy_config_without_costs
from forecast.caps import (METRIC_MIN_HARVEST_HOG, METRIC_OG_DENSITY, FacilityLimits,
                           has_og_density_rows, og_density_applies, og_density_cap,
                           tank_density_cap_for_week)

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PR = ROOT / "tests" / "fixtures" / "pr_2026-09-30.xlsx"
needs_pr = pytest.mark.skipif(not FIXTURE_PR.exists(), reason="fixture PR missing")
WK = "2027-W13"


def _limits(rows=()):
    fl = FacilityLimits()
    for week, metric, value in rows:
        fl.overrides[(week, metric)] = float(value)
    return fl


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def test_no_row_keeps_the_facility_cap():
    fl = _limits([(WK, METRIC_MIN_HARVEST_HOG, 110000)])
    assert not has_og_density_rows(fl)
    assert og_density_cap(WK, fl) is None
    assert tank_density_cap_for_week(85.0, "OG3N", WK, fl) == 85.0
    assert tank_density_cap_for_week(85.0, "OG3N", WK, None) == 85.0


def test_a_row_governs_growout_og_only_and_only_its_week():
    fl = _limits([(WK, METRIC_OG_DENSITY, 95)])
    assert has_og_density_rows(fl)
    for sys_id in ("OG1N", "OG2S", "OG3N", "OG5S", "OG6S"):
        assert og_density_applies(sys_id)
        assert tank_density_cap_for_week(85.0, sys_id, WK, fl) == 95.0
        assert tank_density_cap_for_week(85.0, sys_id, "2027-W12", fl) == 85.0
    # 6N keeps its own cap in every mode; FW is not an OG tank.
    for sys_id in ("OG6N", "FW1", ""):
        assert not og_density_applies(sys_id)
        assert tank_density_cap_for_week(120.0, sys_id, WK, fl) == 120.0


def test_a_zero_or_negative_row_is_no_row():
    for v in (0, -5):
        fl = _limits([(WK, METRIC_OG_DENSITY, v)])
        assert og_density_cap(WK, fl) is None
        assert tank_density_cap_for_week(85.0, "OG4N", WK, fl) == 85.0


# --------------------------------------------------------------------------- #
# End to end: the planner holds the tanks to the row
# --------------------------------------------------------------------------- #
def _run(tmp, density=None):
    """Run the fixture PR; return (max growout density in the row weeks,
    {system: cap} of the final state's tanks)."""
    cfg, scn = tmp / "config", tmp / "scenario"
    copy_config_without_costs(ROOT / "config", cfg)
    shutil.copytree(ROOT / "scenario", scn)
    p = scn / "limits.yaml"
    d = yaml.safe_load(p.read_text(encoding="utf-8"))
    rows_from = "2027-W01"
    if density is not None:
        import datetime as _dt
        day = _dt.date(2027, 1, 4)
        while day < _dt.date(2029, 1, 1):
            y, w, _ = day.isocalendar()
            d.setdefault("facility", []).append(
                {"week": f"{y}-W{w:02d}", "metric": METRIC_OG_DENSITY, "value": float(density)})
            day += _dt.timedelta(days=7)
        p.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
    inp, out = tmp / "pr.xlsx", tmp / "out.xlsm"
    shutil.copy(FIXTURE_PR, inp)

    from forecast import run as _run_mod
    from forecast import tiers
    got = {}
    _orig = _run_mod.run_placement

    def _spy(*a, **kw):
        res = _orig(*a, **kw)
        got["p"], got["fs"] = res
        return res

    _run_mod.run_placement = _spy
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            rc = _run_mod.main(str(inp), str(out), config_dir=str(cfg), scenario_dir=str(scn),
                               calib_log_path="", growth_history_path="")
    finally:
        _run_mod.run_placement = _orig
    assert rc == 0
    peak = 0.0
    for r in got["p"].batch_locations:
        if (r.week_label >= rows_from and og_density_applies(r.system_id)
                and not tiers.density_exempt(r.system_id, getattr(r, "stage", ""), True)):
            peak = max(peak, float(r.density_kg_m3 or 0.0))
    caps = {}
    for t in got["fs"].tanks_by_id.values():
        caps.setdefault(t.system_id, set()).add(float(t.max_density_kg_m3))
    return peak, caps


@needs_pr
def test_the_planner_holds_growout_tanks_to_the_row_and_leaves_6n_alone(tmp_path):
    base_peak, base_caps = _run(tmp_path / "base")
    low_peak, low_caps = _run(tmp_path / "low", density=60.0)
    # The planner used the row: grow-out tanks run well below their 85 peak.
    assert low_peak < base_peak - 5.0, (low_peak, base_peak)
    # The final state carries the row's cap on grow-out tanks only; 6N and FW
    # keep exactly what facility.yaml gave them.
    for sys_id, caps in low_caps.items():
        if og_density_applies(sys_id):
            assert caps == {60.0}, (sys_id, caps)
        else:
            assert caps == base_caps[sys_id], (sys_id, caps)
