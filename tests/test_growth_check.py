"""Check a growth model against the data, as SGR %/day by fish size (operator
2026-10-20: "is there a table that shows the calculated result from the
historical relative to the 100% model ... as % sgr/day"; "make sure everything
is connected properly and updates as more data is added").

Each test can fail: fish grown on a KNOWN fraction of the 100% curve must read
that fraction; the model column must be the engine's own daily growth; adding a
later PR must grow the "after" table; a window whose reports were replaced must
say so. The whole module fails on the code before the feature (nothing to
import, no section, no app table).
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from forecast import growth_history as gh
from forecast.biology import _interp, sgr_pct_per_day
from forecast.config_io import load_biology_tables
from forecast.models_report import growth_check_rows
from test_growth_app import _app_copy, _synthetic_store

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def tables():
    return load_biology_tables(ROOT / "config")


def _iv(batch, w0, days, sgr_fn, d1="2026-09-30", tank=11):
    w1 = gh._predict(w0, days, sgr_fn)
    d0 = (date.fromisoformat(d1) - timedelta(days=days)).isoformat()
    return gh.Interval(batch, tank, d0, d1, days, 10000.0, 9990.0, w0, w1, False)


def _grown_on(tables, factor):
    """Tank-months of fish grown on `factor` x the 100% curve, 300 g to 4.5 kg."""
    fn = lambda w: factor * _interp(w, tables.sgr_size_g, tables.sgr_sw_pct_day)  # noqa: E731
    return [_iv("B1", w0, 30, fn) for w0 in range(300, 4500, 100)]


def test_fish_grown_on_a_known_fraction_of_the_curve_read_that_fraction(tables):
    curve = (tables.sgr_size_g, tables.sgr_sw_pct_day)
    on = gh.check_rows(*curve, _grown_on(tables, 1.0), *curve)
    assert on and all(r[5] == 100 and r[6] == 100 for r in on), on
    slow = gh.check_rows(*curve, _grown_on(tables, 0.8), *curve)
    # SGR x 0.8 every day reads ~80% of the curve (a slower fish is a little
    # smaller, so its curve SGR is a little higher: 81%)
    assert all(r[6] == 81 for r in slow if r[0] != "4,000+ g"), slow
    # 4 kg+: the company table's growth DIPS between 4 and 4.5 kg (20.8 ->
    # 17.6 g/day), so the 100% prediction from the same start weight runs into
    # the dip and the slower fish reads a little higher (83%) -- the table's
    # shape, not the check
    assert [r[6] for r in slow if r[0] == "4,000+ g"] == [83]
    assert all(r[5] == 100 for r in slow)              # the model here IS the curve
    # negative control: the measurement is not simply echoed back
    assert [r[6] for r in slow] != [r[6] for r in on]
    assert [r[0] for r in on][:2] == ["0-500 g", "500-1,000 g"] and on[-1][0] == "4,000+ g"
    assert sum(r[1] for r in on) == len(_grown_on(tables, 1.0))


def test_model_column_is_the_engines_own_growth(tables):
    """The model SGR in the table is what the engine grows a batch on that curve
    at (sgr_pct_per_day, daily steps), with no batch offset or week factor."""
    model = [v * 0.9 for v in tables.sgr_sw_pct_day]
    t2 = SimpleNamespace(**{**vars(tables), "sgr_sw_by_model": {"M": model}})
    batch = SimpleNamespace(batch_id="BX", growth_model="M", sgr_correction=1.0,
                            fw_correction=1.0)
    for w0, days in ((350, 31), (1800, 30), (3900, 18)):
        iv = _iv("B1", w0, days, lambda w: 1.0)
        row = gh.check_rows(tables.sgr_size_g, model, [iv], tables.sgr_size_g,
                            tables.sgr_sw_pct_day)[0]
        w = w0
        for _ in range(days):
            w *= 1 + sgr_pct_per_day(w, "SW", batch, t2) / 100
        assert row[3] == round(100 * math.log(w / w0) / days, 3)


def test_window_and_after_follow_the_fit_rule():
    fit = SimpleNamespace(window_start="2026-01-31", as_of="2026-09-30",
                          excluded_batches=["B9"])
    ivs = [gh.Interval(b, 1, "x", d1, 30, 1, 1, 100, 110, False) for b, d1 in (
        ("B1", "2026-01-31"),        # = window start: outside (start < d1)
        ("B1", "2026-02-28"), ("B1", "2026-09-30"),   # inside, as_of included
        ("B1", "2026-10-18"),        # after the fit
        ("B9", "2026-05-31"), ("B9", "2026-10-31"))]  # left out: in neither
    assert [i.d1 for i in gh.fit_intervals(fit, ivs)] == ["2026-02-28", "2026-09-30"]
    assert [i.d1 for i in gh.later_intervals(fit, ivs)] == ["2026-10-18"]


def _add_report(store_path, closing, kind=gh.FULL, grow_days=None):
    """One more report: every tank of the last snapshot grown on the synthetic
    AGR for the days since it."""
    store = gh.GrowthHistory(store_path)
    last = max(store.snapshots, key=lambda s: s.closing)
    days = grow_days or (date.fromisoformat(closing) - last.closing_date).days
    agr = lambda w: min(0.62 * w ** 0.40, 13.9)               # noqa: E731
    og = []
    for b, t, c, w in last.og:
        for _ in range(days):
            w += agr(w)
        og.append([b, t, c, w])
    store.add(gh.Snapshot(closing=closing, kind=kind, source=f"{closing}.xlsx",
                          sha1=f"s{closing}{kind}", added=f"{closing}T00:00:00", og=og))
    store.save()


def _measured_batch(tables, name):
    t2 = SimpleNamespace(**{**vars(tables)})
    return [SimpleNamespace(batch_id="B11", growth_model=name, sgr_correction=1.0)], t2


def test_after_table_grows_as_later_prs_are_added(tmp_path, tables):
    from forecast.config_io import load_config
    _c, _t, facility = load_config(ROOT / "config")
    path = tmp_path / "gh.json"
    _synthetic_store(path)
    store = gh.GrowthHistory(path)
    ivs = store.intervals(*gh.facility_tank_sets(facility))
    fit = gh.fit_actual(ivs, sizes=tables.sgr_size_g, base_sw=tables.sgr_sw_pct_day,
                        as_of=date(2026, 9, 30), window_months=24)
    store.add_fit(fit)
    store.save()
    batches, t2 = _measured_batch(tables, fit.name)
    t2.sgr_sw_by_model = {fit.name: fit.sgr_pct_day}
    rows = growth_check_rows(batches, t2, path, facility)
    after = [r for r in rows if r[1].startswith("After")]
    assert len(after) == 1 and after[0][2] == "no tank-months yet"
    assert not any("fitted on" in r[1] for r in rows)       # window reads as fitted
    assert sum(r[3] for r in rows if r[1].startswith("Fitted")) == fit.n
    # a later full monthly report arrives: the after table fills in, the
    # fitted-window table and the frozen fit do not move
    _add_report(path, "2026-10-31")
    rows2 = growth_check_rows(batches, t2, path, facility)
    after2 = [r for r in rows2 if r[1].startswith("After")]
    assert sum(r[3] for r in after2) == 30                   # 30 tanks x 1 month
    assert [r for r in rows2 if r[1].startswith("Fitted")] == \
        [r for r in rows if r[1].startswith("Fitted")]
    # fish still growing on the synthetic AGR: the model holds on unseen data
    assert all(abs(r[4] - r[5]) < 0.05 for r in after2), after2


def test_a_window_whose_reports_were_replaced_says_so(tmp_path, tables):
    """Fit on a mid-month PR; the next mid-month PR of the same month replaces
    it (one source per month), so the window now reads fewer tank-months than
    the fit used -- the sheet must say so, not pass the new count off as the
    fit's (seen live 2026-10-20: 'Actual 2026-10-15' fitted on 370, reads 347)."""
    from forecast.config_io import load_config
    _c, _t, facility = load_config(ROOT / "config")
    path = tmp_path / "gh.json"
    _synthetic_store(path)
    _add_report(path, "2026-10-15", kind=gh.MID)
    store = gh.GrowthHistory(path)
    ivs = store.intervals(*gh.facility_tank_sets(facility))
    fit = gh.fit_actual(ivs, sizes=tables.sgr_size_g, base_sw=tables.sgr_sw_pct_day,
                        as_of=date(2026, 10, 15), window_months=24)
    store.add_fit(fit)
    store.save()
    batches, t2 = _measured_batch(tables, fit.name)
    t2.sgr_sw_by_model = {fit.name: fit.sgr_pct_day}
    assert not any("fitted on" in r[1] for r in growth_check_rows(batches, t2, path, facility))
    _add_report(path, "2026-10-20", kind=gh.MID, grow_days=5)
    rows = growth_check_rows(batches, t2, path, facility)
    win = [r[1] for r in rows if r[1].startswith("Fitted")]
    assert win and f"fitted on {fit.n} tank-months; {fit.n - 30} as the history reads now" in win[0]
    assert sum(r[3] for r in rows if r[1].startswith("After")) == 30


def test_base_curve_only_run_says_no_measured_model(tables):
    batches = [SimpleNamespace(batch_id="B1", growth_model="", sgr_correction=0.86)]
    assert growth_check_rows(batches, tables, None, None)[0][0] == \
        "No measured growth model in this run"


_CLICK_CHECK = r"""
import json, sys
from streamlit.testing.v1 import AppTest
at = AppTest.from_file(sys.argv[1], default_timeout=300)
at.session_state["app_mode"] = "Configure (models & control)"
at.run()
out = {}
if sys.argv[2] == "fit":
    at.button(key="gh_fit").click().run()
    out["md_fit"] = [m.value for m in at.markdown if "Check against the data" in m.value]
    out["frames_fit"] = [list(d.value.columns) for d in at.dataframe]
    at.button(key="gh_save").click().run()
    out["success"] = [s.value for s in at.success]
else:                                   # just open the page on a saved version
    at.selectbox(key="gh_version").set_value(sys.argv[2]).run()
out["md_saved"] = [m.value for m in at.markdown if m.value.startswith("**Check '")]
out["captions"] = [c.value for c in at.caption]
out["exceptions"] = [str(e.value) for e in at.exception]
print("<<<RESULT>>>" + json.dumps(out))
"""


def _click(app, store, mode):
    env = dict(os.environ, AS_GROWTH_HISTORY=str(store))
    proc = subprocess.run([sys.executable, "-c", _CLICK_CHECK, str(app), mode],
                          capture_output=True, text=True, timeout=900,
                          cwd=str(app.parent), env=env)
    marker = [ln for ln in proc.stdout.splitlines() if ln.startswith("<<<RESULT>>>")]
    assert marker, f"no result: {proc.stdout[-1500:]} | {proc.stderr[-1500:]}"
    return json.loads(marker[-1][len("<<<RESULT>>>"):])


def test_check_tables_in_the_app_and_they_update_with_a_later_pr(tmp_path):
    pytest.importorskip("streamlit")
    store = tmp_path / "gh.json"
    _synthetic_store(store)
    app = _app_copy(tmp_path / "app")
    r = _click(app, store, "fit")
    assert not r["exceptions"], r["exceptions"]
    assert r["md_fit"], "no check table under the fit preview"
    assert gh.CHECK_COLUMNS in r["frames_fit"]
    assert "Saved 'Actual 2026-09-30'." in r["success"]       # survives the rerun
    assert r["md_saved"] and "Actual 2026-09-30" in r["md_saved"][0]
    assert any("No tank-months ending after 2026-09-30 yet" in c for c in r["captions"])
    # a later PR is added to the history: the saved version's check now shows
    # the data it has never seen
    _add_report(store, "2026-10-31")
    r2 = _click(app, store, "Actual 2026-09-30")
    assert not r2["exceptions"], r2["exceptions"]
    assert any("Tank-months ending after 2026-09-30" in c and "(30;" in c
               for c in r2["captions"]), r2["captions"]
