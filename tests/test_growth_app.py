"""Growth models in the app and the run (step 3 of the 2026-10-18 plan).

Pins: an operational run adds its PR to the growth history and a test/scratch
run never does (the calibration-log rule); a recording problem never fails a
run; adding a version to biology.yaml writes biology.yaml only and changes no
plan until a batch picks it; the before/after really runs the version; and the
Configure page renders the panel and the Batches column without writing the
operator's history.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

from conftest import copy_config_without_costs
from forecast import growth_history as gh
from forecast.config_io import load_biology_tables
from forecast.models import BatchInput

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PR = ROOT / "tests" / "fixtures" / "pr_2026-09-30.xlsx"
needs_pr = pytest.mark.skipif(not FIXTURE_PR.exists(), reason="fixture PR missing")


def _md5(p):
    return hashlib.md5(Path(p).read_bytes()).hexdigest() if Path(p).exists() else None


def _project(tmp):
    cfg, scn = tmp / "config", tmp / "scenario"
    copy_config_without_costs(ROOT / "config", cfg)
    shutil.copytree(ROOT / "scenario", scn)
    return cfg, scn


def _run(tmp, **kw):
    cfg, scn = _project(tmp)
    inp, out = tmp / "pr.xlsx", tmp / "out.xlsm"
    shutil.copy(FIXTURE_PR, inp)
    from forecast.run import main
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(str(inp), str(out), config_dir=str(cfg), scenario_dir=str(scn), **kw)
    return rc, buf.getvalue()


# --------------------------------------------------------------------------- #
# The run hook
# --------------------------------------------------------------------------- #
@needs_pr
def test_a_run_records_its_pr_once_and_a_test_run_records_nothing(tmp_path):
    live = gh.live_store_path()
    live_before = _md5(live)
    store = tmp_path / "gh.json"
    rc, log = _run(tmp_path / "a", calib_log_path="", growth_history_path=str(store))
    assert rc == 0 and "Growth history: added 2026-09-30" in log
    assert [s.closing for s in gh.GrowthHistory(store).snapshots] == ["2026-09-30"]
    rc, log = _run(tmp_path / "b", calib_log_path="", growth_history_path=str(store))
    assert rc == 0 and "Growth history: already in" in log
    assert len(gh.GrowthHistory(store).snapshots) == 1
    # The test/scratch convention: calib_log_path="" (or a path of its own)
    # and no growth_history_path -> nothing is recorded anywhere.
    for kw in ({"calib_log_path": ""},
               {"calib_log_path": str(tmp_path / "own_calib.jsonl")}):
        rc, log = _run(tmp_path / ("c" + str(len(kw["calib_log_path"]))), **kw)
        assert rc == 0 and "Growth history" not in log
    assert _md5(live) == live_before


@needs_pr
def test_a_broken_history_file_never_fails_the_run(tmp_path):
    store = tmp_path / "gh.json"
    store.write_text("{not json", encoding="utf-8")
    rc, log = _run(tmp_path / "r", calib_log_path="", growth_history_path=str(store))
    assert rc == 0 and "Growth history: NOT recorded" in log
    assert store.read_text(encoding="utf-8") == "{not json"     # left as it was


# --------------------------------------------------------------------------- #
# biology.yaml, the batch sentence, the folder scan
# --------------------------------------------------------------------------- #
def test_adding_a_version_writes_biology_yaml_only(tmp_path):
    cfg, _ = _project(tmp_path)
    others = {n: _md5(cfg / n) for n in ("control.yaml", "facility.yaml")}
    n = len(load_biology_tables(cfg).sgr_size_g)
    vals = [0.5] * n
    assert gh.add_growth_model_to_biology(cfg, "Actual 2026-10-15", vals) == "added"
    assert load_biology_tables(cfg).sgr_sw_by_model["Actual 2026-10-15"] == vals
    assert {k: _md5(cfg / k) for k in others} == others
    assert gh.add_growth_model_to_biology(cfg, "Actual 2026-10-15", vals) == "already there"
    with pytest.raises(ValueError, match="different growth model"):
        gh.add_growth_model_to_biology(cfg, "Actual 2026-10-15", [0.4] * n)
    with pytest.raises(ValueError, match="size rows"):
        gh.add_growth_model_to_biology(cfg, "Short", [0.4] * (n - 1))


def test_batch_sentence_says_curve_and_factor():
    t = load_biology_tables(ROOT / "config")
    t.sgr_sw_by_model = {"Actual 2026-10-15": [1.0] * len(t.sgr_size_g)}

    def b(**kw):
        d = dict(batch_id="B60", input_date=None, input_count=0, tran_sf_date=None,
                 tran_og_date=None, tran_og_count=None, tran_og_avg_wt_g=None,
                 tran_og_cv=16.0, fcr_model="FCR_116_Quick", fw_correction=1.0,
                 sgr_correction=1.0)
        d.update(kw)
        return BatchInput(**d)
    assert gh.describe_batch_growth(b(), t) == "Base curve"
    assert gh.describe_batch_growth(b(sgr_correction=0.95), t) == \
        "Base curve x 0.95 (sgr_correction)"
    assert gh.describe_batch_growth(b(growth_model="Actual 2026-10-15"), t) == \
        "Actual 2026-10-15"
    assert "NOT in biology.yaml" in gh.describe_batch_growth(b(growth_model="Typo"), t)


@needs_pr
def test_folder_scan_adds_reports_once_and_skips_the_rest(tmp_path):
    d = tmp_path / "Reports" / "2026" / "Sep"
    d.mkdir(parents=True)
    shutil.copy(FIXTURE_PR, d / "Sep'26 Production Report.xlsx")
    shutil.copy(FIXTURE_PR, d / "Acc Exposure Close W41.xlsx")       # not a report name
    store = gh.GrowthHistory(tmp_path / "gh.json")
    r1 = gh.scan_reports(store, [tmp_path / "Reports" / "2026"], since="2024-11-01")
    assert len(r1["added"]) == 1 and r1["already"] == 0
    r2 = gh.scan_reports(gh.GrowthHistory(tmp_path / "gh.json"),
                         [tmp_path / "Reports" / "2026"], since="2024-11-01")
    assert r2["added"] == [] and r2["already"] == 1
    r3 = gh.scan_reports(gh.GrowthHistory(tmp_path / "gh2.json"),
                         [tmp_path / "Reports" / "2026"], since="2026-10-01")
    assert r3["added"] == [] and r3["before_since"] == 1


# --------------------------------------------------------------------------- #
# Before / after
# --------------------------------------------------------------------------- #
@needs_pr
def test_before_after_runs_the_version_and_changes_nothing_live(tmp_path):
    cfg, scn = _project(tmp_path / "proj")
    before = {p.name: _md5(p) for p in list(cfg.iterdir()) + list(scn.glob("*.yaml"))}
    t = load_biology_tables(cfg)
    slow = [None if v is None else v * 0.8 for v in t.sgr_sw_pct_day]
    rows = gh.compare_plan(FIXTURE_PR, cfg, scn, "Slow", slow, tmp_path / "work")
    assert rows and all(len(r) == 5 for r in rows)
    hog_before = sum(r[2] for r in rows)
    hog_after = sum(r[4] for r in rows)
    assert hog_before > 0 and hog_after != hog_before      # the version really ran
    assert {p.name: _md5(p) for p in list(cfg.iterdir()) + list(scn.glob("*.yaml"))} == before
    # The same curve as the base one: before == after (negative control for
    # the inequality above - the difference is the curve, not the harness).
    same = gh.compare_plan(FIXTURE_PR, cfg, scn, "Same", list(t.sgr_sw_pct_day),
                           tmp_path / "work2")
    assert all(r[1] == r[3] and r[2] == r[4] for r in same)


# --------------------------------------------------------------------------- #
# The app
# --------------------------------------------------------------------------- #
_PROBE = r"""
import json, sys, os
from streamlit.testing.v1 import AppTest
at = AppTest.from_file(sys.argv[1], default_timeout=300)
at.session_state["app_mode"] = "Configure (models & control)"
at.run()
print("<<<RESULT>>>" + json.dumps({
    "exceptions": [str(e.value) for e in at.exception],
    "markdown": [m.value for m in at.markdown],
    "buttons": [b.label for b in at.button],
    "selects": {w.label: [str(o) for o in w.options] for w in at.selectbox},
    "captions": [c.value for c in at.caption],
}))
"""


def _render(store_path):
    env = dict(os.environ, AS_GROWTH_HISTORY=str(store_path))
    proc = subprocess.run([sys.executable, "-c", _PROBE, str(ROOT / "app.py")],
                          capture_output=True, text=True, timeout=600, cwd=str(ROOT),
                          env=env)
    marker = [ln for ln in proc.stdout.splitlines() if ln.startswith("<<<RESULT>>>")]
    assert marker, f"no result: {proc.stdout[-1500:]} | {proc.stderr[-1500:]}"
    return json.loads(marker[-1][len("<<<RESULT>>>"):])


def test_configure_page_renders_the_panel_with_an_empty_history(tmp_path):
    pytest.importorskip("streamlit")
    store = tmp_path / "gh.json"
    r = _render(store)
    assert not r["exceptions"], r["exceptions"]
    assert any("Actual growth model" in m for m in r["markdown"])
    assert "Scan the Reports folder (from Nov 2024)" in r["buttons"]
    assert not store.exists()                       # rendering writes nothing


def test_configure_page_shows_saved_versions(tmp_path):
    pytest.importorskip("streamlit")
    t = load_biology_tables(ROOT / "config")
    store = gh.GrowthHistory(tmp_path / "gh.json")
    store.fits.append(gh.GrowthFit(
        name="Actual 2026-10-15", as_of="2026-10-15", window_months=24,
        window_start="2024-10-15", breakpoint_g=2000.0, a=0.62, b=0.40,
        cap_g_day=13.9, n=370, n_provisional=23, n_below=161, n_above=209,
        sizes_g=list(t.sgr_size_g), sgr_pct_day=list(t.sgr_sw_pct_day)))
    store.save()
    before = _md5(tmp_path / "gh.json")
    r = _render(tmp_path / "gh.json")
    assert not r["exceptions"], r["exceptions"]
    assert r["selects"].get("Version") == ["Actual 2026-10-15"]
    assert "Add 'Actual 2026-10-15' to biology.yaml growth models" in r["buttons"]
    assert _md5(tmp_path / "gh.json") == before


_CLICK = r"""
import json, sys
from streamlit.testing.v1 import AppTest
at = AppTest.from_file(sys.argv[1], default_timeout=300)
at.session_state["app_mode"] = "Configure (models & control)"
at.run()
at.button(key="gh_fit").click().run()
preview = [m.value for m in at.markdown if m.value.startswith("Preview")]
at.button(key="gh_save").click().run()
print("<<<RESULT>>>" + json.dumps({
    "exceptions": [str(e.value) for e in at.exception],
    "preview": preview,
    "selects": {w.label: [str(o) for o in w.options] for w in at.selectbox},
}))
"""


def _synthetic_store(path):
    """Six month-end full reports: 40 tanks growing at a known AGR, half the
    tank-months above 2 kg - enough for a fit."""
    from datetime import timedelta
    agr = lambda w: min(0.62 * w ** 0.40, 13.9)             # noqa: E731
    store = gh.GrowthHistory(path)
    ends = [date(2026, 4, 30), date(2026, 5, 31), date(2026, 6, 30),
            date(2026, 7, 31), date(2026, 8, 31), date(2026, 9, 30)]
    wts = {t: 400.0 + 100.0 * k for k, t in enumerate(
        [11, 12, 13, 14, 15, 16, 21, 22, 23, 24, 25, 26, 31, 32, 33, 34, 35, 36,
         41, 42, 43, 44, 45, 46, 51, 52, 53, 54, 55, 56])}
    prev = None
    for d in ends:
        if prev is not None:
            for t in wts:
                for _ in range((d - prev).days):
                    wts[t] += agr(wts[t])
        store.add(gh.Snapshot(closing=d.isoformat(), kind=gh.FULL,
                              source=f"{d:%b} report.xlsx", sha1=f"s{d}",
                              added=f"{d}T00:00:00",
                              og=[[f"B{t}", t, 10000.0, w] for t, w in wts.items()]))
        prev = d
    store.save()


def test_fit_and_save_buttons_work_and_write_only_the_named_history(tmp_path):
    pytest.importorskip("streamlit")
    store = tmp_path / "gh.json"
    _synthetic_store(store)
    bio_before = _md5(ROOT / "config" / "biology.yaml")
    env = dict(os.environ, AS_GROWTH_HISTORY=str(store))
    proc = subprocess.run([sys.executable, "-c", _CLICK, str(ROOT / "app.py")],
                          capture_output=True, text=True, timeout=600, cwd=str(ROOT),
                          env=env)
    marker = [ln for ln in proc.stdout.splitlines() if ln.startswith("<<<RESULT>>>")]
    assert marker, f"no result: {proc.stdout[-1500:]} | {proc.stderr[-1500:]}"
    r = json.loads(marker[-1][len("<<<RESULT>>>"):])
    assert not r["exceptions"], r["exceptions"]
    assert r["preview"] and "Actual 2026-09-30" in r["preview"][0]
    saved = gh.GrowthHistory(store)
    assert [f.name for f in saved.fits] == ["Actual 2026-09-30"]
    assert saved.fits[0].cap_g_day == pytest.approx(13.9, rel=0.05)
    assert r["selects"].get("Version") == ["Actual 2026-09-30"]
    assert _md5(ROOT / "config" / "biology.yaml") == bio_before   # Save != adopt


# --------------------------------------------------------------------------- #
# Leave batches out of the fit, clicked through the app (operator 2026-10-20)
# --------------------------------------------------------------------------- #
_CLICK_EXCLUDE = r"""
import json, sys
from streamlit.testing.v1 import AppTest
at = AppTest.from_file(sys.argv[1], default_timeout=300)
at.session_state["app_mode"] = "Configure (models & control)"
at.run()
out = {"options": [str(o) for o in at.multiselect(key="gh_exclude").options]}
at.button(key="gh_fit").click().run()               # nothing left out first
out["preview_all"] = [m.value for m in at.markdown if m.value.startswith("Preview")]
at.multiselect(key="gh_exclude").set_value(["B11", "B12"]).run()
at.button(key="gh_fit").click().run()
out["preview_out"] = [m.value for m in at.markdown if m.value.startswith("Preview")]
at.button(key="gh_save").click().run()
out["after_save"] = [str(e.value) for e in at.exception]
at.button(key="gh_adopt").click().run()
out["exceptions"] = [str(e.value) for e in at.exception]
out["success"] = [s.value for s in at.success]
out["captions"] = [c.value for c in at.caption]
print("<<<RESULT>>>" + json.dumps(out))
"""


def _app_copy(dst):
    """The app, its code and a COPY of the config/scenario: the 'Add to
    biology.yaml' button writes the config next to app.py, so a click test
    must never run the operator's own tree."""
    ign = shutil.ignore_patterns("__pycache__", "costs.yaml")
    dst.mkdir(parents=True)
    shutil.copy(ROOT / "app.py", dst / "app.py")
    for d in ("forecast", "tools", "config", "scenario"):
        shutil.copytree(ROOT / d, dst / d, ignore=ign)
    return dst / "app.py"


def test_leave_out_fit_save_and_add_clicked_in_the_app(tmp_path):
    pytest.importorskip("streamlit")
    store = tmp_path / "gh.json"
    _synthetic_store(store)
    app = _app_copy(tmp_path / "app")
    live = {p: _md5(ROOT / "config" / p) for p in ("biology.yaml", "control.yaml")}
    env = dict(os.environ, AS_GROWTH_HISTORY=str(store))
    proc = subprocess.run([sys.executable, "-c", _CLICK_EXCLUDE, str(app)],
                          capture_output=True, text=True, timeout=900,
                          cwd=str(app.parent), env=env)
    marker = [ln for ln in proc.stdout.splitlines() if ln.startswith("<<<RESULT>>>")]
    assert marker, f"no result: {proc.stdout[-1500:]} | {proc.stderr[-1500:]}"
    r = json.loads(marker[-1][len("<<<RESULT>>>"):])
    assert not r["after_save"] and not r["exceptions"], (r["after_save"], r["exceptions"])
    # the picker offers the history's batches in batch order
    assert r["options"][:3] == ["B11", "B12", "B13"] and len(r["options"]) == 30
    # negative control: with nothing picked the preview names nothing left out
    assert r["preview_all"] and "left out" not in r["preview_all"][0]
    n_all = int(r["preview_all"][0].split("— ")[1].split(" tank-months")[0])
    assert r["preview_out"] and "left out: B11, B12" in r["preview_out"][0]
    n_out = int(r["preview_out"][0].split("— ")[1].split(" tank-months")[0])
    assert n_out == n_all - 2 * 5                    # 2 tanks x 5 month intervals
    # Save: the frozen version records the left-out batches
    fit = gh.GrowthHistory(store).get_fit("Actual 2026-09-30")
    assert fit.excluded_batches == ["B11", "B12"] and fit.n == n_out
    # Add to biology.yaml: the COPY's biology.yaml has it, with the saved values
    bio = yaml.safe_load((app.parent / "config" / "biology.yaml").read_text(encoding="utf-8"))
    assert bio["sgr_sw_by_model"]["Actual 2026-09-30"] == fit.sgr_pct_day
    # the page reruns after adding, so it shows the version as in biology.yaml
    assert any("In biology.yaml" in c for c in r["captions"])
    # the operator's own config is untouched
    assert {p: _md5(ROOT / "config" / p) for p in live} == live
