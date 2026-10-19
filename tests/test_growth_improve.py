"""Growth models step 4: expected improvement (ASSUMED versions) and the
per-batch factor from a batch's own history (forecast/growth_history.py).

Operator plan 2026-10-18: improvement both as "% per year" and as "blend to a
target", always labelled ASSUMED; per batch both fleet-only and fleet x own
history (pulled toward 1.0); "make it clear what's happening". Nothing here
changes a plan: an ASSUMED version is data until it is added to biology.yaml
and a batch picks it, and the per-batch factor is only shown.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from forecast import growth_history as gh
from forecast.config_io import load_biology_tables

ROOT = Path(__file__).resolve().parent.parent
SIZES = [50.0, 100.0, 500.0, 1000.0, 2000.0, 3000.0, 4000.0, 5000.0]
SGR = [3.0, 2.5, 1.5, 1.0, 0.65, 0.46, 0.35, 0.28]


def _fit(name="Actual 2026-10-15", as_of="2026-10-15", sgr=None):
    return gh.GrowthFit(
        name=name, as_of=as_of, window_months=24, window_start="2024-10-15",
        breakpoint_g=2000.0, a=0.62, b=0.40, cap_g_day=13.9, n=100,
        n_provisional=0, n_below=50, n_above=50, sizes_g=list(SIZES),
        sgr_pct_day=list(sgr or SGR))


def _agr(m, i):
    return gh._sgr_to_agr(m.sgr_pct_day[i], SIZES[i])


# --------------------------------------------------------------------------- #
# + % per year
# --------------------------------------------------------------------------- #
def test_zero_percent_is_the_measured_version():
    m = gh.improve_pct_per_year(_fit(), 0.0, "2027-10-15")
    assert m.sgr_pct_day == pytest.approx(SGR, abs=1e-4)
    assert m.factor == 1.0


def test_ten_percent_for_one_year_is_ten_percent_more_grams_per_day():
    f = _fit()
    m = gh.improve_pct_per_year(f, 10.0, "2027-10-15")
    assert m.factor == pytest.approx(1.10, abs=0.002)        # 365/365.25 of a year
    for i, w in enumerate(SIZES):
        if w >= gh.DEFAULT_SW_MIN_G:
            assert _agr(m, i) == pytest.approx(_agr(f, i) * m.factor, rel=1e-3)
        else:
            assert m.sgr_pct_day[i] == SGR[i]                 # not seawater fish
    # Two years compounds: more than one year, and exactly 1.1^2.
    m2 = gh.improve_pct_per_year(f, 10.0, "2028-10-15")
    assert m2.factor == pytest.approx(1.21, abs=0.003)
    assert m2.sgr_pct_day[6] > m.sgr_pct_day[6] > SGR[6]


def test_a_date_before_the_measurement_gives_no_improvement():
    # Negative control: without the clamp a date in the past would shrink the
    # curve for a positive % and the "improvement" would run backwards.
    m = gh.improve_pct_per_year(_fit(), 10.0, "2025-10-15")
    assert m.factor == 1.0
    assert m.sgr_pct_day == pytest.approx(SGR, abs=1e-4)


def test_every_assumed_version_says_so():
    m = gh.improve_pct_per_year(_fit(), 3.0, "2027-10-01")
    b = gh.blend_to_target(_fit(), "Base curve", SGR, "2028-10-15", "2027-10-15")
    for v in (m, b):
        assert gh.ASSUMED in v.name and v.explain.startswith(gh.ASSUMED)
        assert v.assumed is True and v.source == "Actual 2026-10-15"
    assert "+3%/yr" in m.name and "2027-10" in m.name
    with pytest.raises(ValueError):
        gh.improve_pct_per_year(_fit(), 60.0, "2027-10-01")


# --------------------------------------------------------------------------- #
# Blend toward a target
# --------------------------------------------------------------------------- #
def test_blend_is_a_straight_line_in_grams_per_day():
    f = _fit()
    target = [s * 1.5 for s in SGR]
    at0 = gh.blend_to_target(f, "T", target, "2028-10-15", "2026-10-15")
    half = gh.blend_to_target(f, "T", target, "2028-10-15", "2027-10-15")
    end = gh.blend_to_target(f, "T", target, "2028-10-15", "2028-10-15")
    after = gh.blend_to_target(f, "T", target, "2028-10-15", "2030-01-01")
    assert at0.factor == 0.0 and at0.sgr_pct_day == pytest.approx(SGR, abs=1e-4)
    assert half.factor == pytest.approx(0.5, abs=0.002)
    assert end.factor == 1.0 and after.factor == 1.0          # held after the date
    for i, w in enumerate(SIZES):
        if w < gh.DEFAULT_SW_MIN_G:
            assert half.sgr_pct_day[i] == SGR[i]
            continue
        a0, a1 = _agr(f, i), gh._sgr_to_agr(target[i], w)
        assert _agr(half, i) == pytest.approx(a0 + half.factor * (a1 - a0), rel=1e-3)
        assert end.sgr_pct_day[i] == pytest.approx(target[i], abs=1e-4)


def test_blend_refuses_a_target_date_before_the_measurement_or_other_sizes():
    with pytest.raises(ValueError, match="after"):
        gh.blend_to_target(_fit(), "T", SGR, "2026-01-01", "2027-01-01")
    with pytest.raises(ValueError, match="size rows"):
        gh.blend_to_target(_fit(), "T", SGR[:-1], "2028-10-15", "2027-10-15")


# --------------------------------------------------------------------------- #
# The store keeps ASSUMED versions frozen, next to the measured ones
# --------------------------------------------------------------------------- #
def test_assumed_versions_are_saved_frozen_and_kept_apart(tmp_path):
    p = tmp_path / "gh.json"
    s = gh.GrowthHistory(p)
    s.add_fit(_fit())
    s.save()
    assert "derived" not in json.loads(p.read_text())     # step 3 files unchanged
    m = gh.improve_pct_per_year(s.fits[0], 3.0, "2027-10-01")
    s.add_derived(m)
    s.add_derived(m)                                       # same values: no-op
    s.save()
    r = gh.GrowthHistory(p)
    assert [v.name for v in r.versions()] == ["Actual 2026-10-15", m.name]
    assert r.get_version(m.name).sgr_pct_day == m.sgr_pct_day
    other = gh.improve_pct_per_year(s.fits[0], 5.0, "2027-10-01", name=m.name)
    with pytest.raises(ValueError, match="frozen"):
        r.add_derived(other)
    orphan = gh.improve_pct_per_year(_fit(name="Not saved"), 3.0, "2027-10-01")
    with pytest.raises(KeyError):
        r.add_derived(orphan)                              # source must be saved
    with pytest.raises(ValueError, match="measured"):
        r.add_derived(gh.improve_pct_per_year(s.fits[0], 3.0, "2027-10-01",
                                              name="Actual 2026-10-15"))
    with pytest.raises(ValueError, match="ASSUMED"):
        r.add_fit(_fit(name=m.name))


def test_an_assumed_version_goes_into_biology_yaml_like_a_measured_one(tmp_path):
    import shutil
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg, ignore=shutil.ignore_patterns("costs.yaml*"))
    t = load_biology_tables(cfg)
    f = _fit(sgr=list(t.sgr_sw_pct_day))
    f.sizes_g = list(t.sgr_size_g)
    m = gh.improve_pct_per_year(f, 3.0, "2027-10-01")
    name, vals = gh.biology_entry(m, t.sgr_size_g)
    assert gh.add_growth_model_to_biology(cfg, name, vals) == "added"
    assert load_biology_tables(cfg).sgr_sw_by_model[name] == vals


# --------------------------------------------------------------------------- #
# Per batch: own history against the measured version
# --------------------------------------------------------------------------- #
def _intervals(batch, factor, months, w0=1000.0, d0="2026-01-31"):
    """`months` 30-day tank intervals of `batch` putting on `factor` times the
    weight (in log terms) the fit predicts."""
    from datetime import date, timedelta
    from forecast.biology import _interp
    f = _fit()
    out, d, w = [], date.fromisoformat(d0), w0
    for _ in range(months):
        pred = gh._predict(w, 30, lambda x: _interp(x, f.sizes_g, f.sgr_pct_day))
        w1 = w * (pred / w) ** factor
        out.append(gh.Interval(batch, 31, d.isoformat(),
                               (d + timedelta(days=30)).isoformat(), 30,
                               10000, 9990, w, w1, provisional=False))
        d, w = d + timedelta(days=30), w1
    return out


def test_a_batch_on_the_fleet_curve_measures_one():
    rows = gh.batch_factors(_intervals("B44", 1.0, 6), _fit())
    assert [r.batch for r in rows] == ["B44"]
    assert rows[0].measured == pytest.approx(1.0, abs=0.002)
    assert rows[0].suggested == pytest.approx(1.0, abs=0.002)


def test_more_history_moves_the_suggestion_closer_to_what_was_measured():
    few = gh.batch_factors(_intervals("B48", 1.10, 2), _fit())[0]
    many = gh.batch_factors(_intervals("B48", 1.10, 12, d0="2025-09-30"),
                            _fit())[0]   # all 12 inside the window
    assert few.measured == pytest.approx(1.10, abs=0.005)
    assert many.measured == pytest.approx(1.10, abs=0.005)
    # Pulled toward 1.0, less so with more evidence; half way at the prior.
    assert 1.0 < few.suggested < many.suggested < many.measured
    assert many.suggested == pytest.approx(1.05, abs=0.005)
    assert many.tank_months == pytest.approx(gh.BATCH_PRIOR_MONTHS)


def test_only_the_versions_window_counts_and_batches_come_in_number_order():
    ivs = (_intervals("B10", 1.2, 3, d0="2023-01-31")       # before the window
           + _intervals("B10", 1.0, 3) + _intervals("B9", 1.0, 3))
    rows = gh.batch_factors(ivs, _fit())
    assert [r.batch for r in rows] == ["B9", "B10"]
    # Negative control: counting the old block would lift B10 above 1.0.
    assert rows[1].n == 3 and rows[1].measured == pytest.approx(1.0, abs=0.002)


# --------------------------------------------------------------------------- #
# The app
# --------------------------------------------------------------------------- #
def _md5(p):
    return hashlib.md5(Path(p).read_bytes()).hexdigest()


_APP = r"""
import json, sys
from streamlit.testing.v1 import AppTest
at = AppTest.from_file(sys.argv[1], default_timeout=300)
at.session_state["app_mode"] = "Configure (models & control)"
at.run()
first = {"exceptions": [str(e.value) for e in at.exception],
         "warnings": [w.value for w in at.warning],
         "versions": [str(o) for o in at.selectbox(key="gh_version").options],
         "save_disabled": at.button(key="gh_imp_save").disabled,
         "tables": [list(d.value.columns) for d in at.dataframe]}
at.number_input(key="gh_imp_pct").set_value(3.0).run()
at.button(key="gh_imp_save").click().run()
print("<<<RESULT>>>" + json.dumps({
    "first": first,
    "exceptions": [str(e.value) for e in at.exception],
    "versions": [str(o) for o in at.selectbox(key="gh_version").options],
    "picked": at.selectbox(key="gh_version").value,
    "warnings": [w.value for w in at.warning],
}))
"""


def test_the_panel_builds_an_assumed_version_and_shows_the_batch_factors(tmp_path):
    pytest.importorskip("streamlit")
    from tests.test_growth_app import _synthetic_store
    store = tmp_path / "gh.json"
    _synthetic_store(store)
    s = gh.GrowthHistory(store)
    t = load_biology_tables(ROOT / "config")
    sixn, og = {61, 63, 65, 67, 69, 71}, None
    s.add_fit(gh.fit_actual(s.intervals(sixn, og), sizes=t.sgr_size_g,
                            base_sw=t.sgr_sw_pct_day, as_of="2026-09-30"))
    s.save()
    bio_before = _md5(ROOT / "config" / "biology.yaml")
    bat_before = _md5(ROOT / "scenario" / "batches.yaml")
    env = dict(os.environ, AS_GROWTH_HISTORY=str(store))
    proc = subprocess.run([sys.executable, "-c", _APP, str(ROOT / "app.py")],
                          capture_output=True, text=True, timeout=600, cwd=str(ROOT),
                          env=env)
    marker = [ln for ln in proc.stdout.splitlines() if ln.startswith("<<<RESULT>>>")]
    assert marker, f"no result: {proc.stdout[-1500:]} | {proc.stderr[-1500:]}"
    r = json.loads(marker[-1][len("<<<RESULT>>>"):])
    f = r["first"]
    assert not f["exceptions"] and not r["exceptions"], (f, r["exceptions"])
    assert f["versions"] == ["Actual 2026-09-30"]
    assert f["save_disabled"]                      # 0% = the measured version
    assert any("Measured" in c and "Suggested" in c and "sgr_correction now" in c
               for c in f["tables"])
    new = "Actual 2026-09-30 +3%/yr for 2027-09 (ASSUMED)"
    assert r["versions"] == ["Actual 2026-09-30", new]
    assert r["picked"] == new                     # the saved one is shown next
    assert any(w.startswith("ASSUMED") for w in r["warnings"])
    saved = gh.GrowthHistory(store)
    assert [m.name for m in saved.derived] == [new]
    assert _md5(ROOT / "config" / "biology.yaml") == bio_before
    assert _md5(ROOT / "scenario" / "batches.yaml") == bat_before
