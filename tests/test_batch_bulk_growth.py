"""Set growth for a run of batches in one go (operator 2026-10-20): the seawater
growth model, the SW offset (sgr_correction) and the FW offset (fw_correction).

Before this, the Batches tab took one dropdown per batch -- about 40 for the
10.18 PR -- which was not a usable step. Negative controls: the function is
new (the old code has nothing to import), and the app test fails on the old
code because the 'bulk_*' widgets do not exist there.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from forecast import growth_history as gh
from forecast.biology import BASE_GROWTH_MODEL
from forecast.config_io import load_biology_tables

ROOT = Path(__file__).resolve().parent.parent


def _rows():
    return [{"batch_id": b, "growth_model": "Old", "sgr_correction": 0.95,
             "fw_correction": 0.82, "notes": "keep me"}
            for b in ("B9", "B10", "B11", "B45", "B57", "B58", "B100")]


def test_range_is_batch_number_order_both_ends_included():
    # B9 < B10 < B100 by number; as strings "B100" < "B11" < "B9"
    out, changed = gh.set_batch_growth(_rows(), "B10", "B57", model="New")
    assert changed == ["B10", "B11", "B45", "B57"]
    got = {r["batch_id"]: r["growth_model"] for r in out}
    assert got == {"B9": "Old", "B10": "New", "B11": "New", "B45": "New",
                   "B57": "New", "B58": "Old", "B100": "Old"}


def test_none_leaves_a_field_and_base_curve_is_blank():
    rows = _rows()
    out, _ = gh.set_batch_growth(rows, "B58", "B100", model=BASE_GROWTH_MODEL,
                                 sw_adjustment=0.86)
    b58 = next(r for r in out if r["batch_id"] == "B58")
    assert b58["growth_model"] == ""             # batches.yaml: blank = Base curve
    assert b58["sgr_correction"] == 0.86
    assert b58["fw_correction"] == 0.82          # FW not asked for: unchanged
    assert b58["notes"] == "keep me"
    # the input rows are not modified
    assert rows[5]["growth_model"] == "Old" and rows[5]["sgr_correction"] == 0.95
    out2, _ = gh.set_batch_growth(rows, "B9", "B9", fw_adjustment=0.9)
    b9 = out2[0]
    assert (b9["growth_model"], b9["sgr_correction"], b9["fw_correction"]) \
        == ("Old", 0.95, 0.9)


def test_bad_range_and_bad_offset_are_refused():
    with pytest.raises(ValueError, match="comes after"):
        gh.set_batch_growth(_rows(), "B57", "B10", model="New")
    with pytest.raises(ValueError, match="above 0"):
        gh.set_batch_growth(_rows(), "B9", "B10", sw_adjustment=0)


_CLICK_BULK = r"""
import json, sys
from streamlit.testing.v1 import AppTest
first, mid, last = sys.argv[2:5]
at = AppTest.from_file(sys.argv[1], default_timeout=300)
at.session_state["app_mode"] = "Configure (models & control)"
at.run()
out = {"models": [str(o) for o in at.selectbox(key="bulk_model").options]}
at.button(key="bulk_apply").click().run()              # nothing chosen
out["nothing"] = [w.value for w in at.warning]
# in-flight batches: the measured model
at.selectbox(key="bulk_from").set_value(first)
at.selectbox(key="bulk_to").set_value(mid)
at.selectbox(key="bulk_model").set_value("Test model").run()
at.button(key="bulk_apply").click().run()
out["info1"] = [i.value for i in at.info]
# future batches: the base curve x 0.86, FW offset 0.85
nxt = sys.argv[5]
at.selectbox(key="bulk_from").set_value(nxt)
at.selectbox(key="bulk_to").set_value(last)
at.selectbox(key="bulk_model").set_value("Base curve")
at.checkbox(key="bulk_sw_on").check()
at.checkbox(key="bulk_fw_on").check().run()
at.number_input(key="bulk_sw").set_value(0.86)
at.number_input(key="bulk_fw").set_value(0.85).run()
out["captions"] = [c.value for c in at.caption]
at.button(key="bulk_apply").click().run()
out["info2"] = [i.value for i in at.info]
at.button(key="save_batch").click().run()
out["success"] = [s.value for s in at.success]
out["exceptions"] = [str(e.value) for e in at.exception]
print("<<<RESULT>>>" + json.dumps(out))
"""


def _md5(p):
    return hashlib.md5(Path(p).read_bytes()).hexdigest()


def _app_copy(dst):
    """The app and a COPY of config/scenario: Save Batches writes the scenario
    next to app.py, so a click test must never run the operator's own tree."""
    ign = shutil.ignore_patterns("__pycache__", "costs.yaml")
    dst.mkdir(parents=True)
    shutil.copy(ROOT / "app.py", dst / "app.py")
    for d in ("forecast", "tools", "config", "scenario"):
        shutil.copytree(ROOT / d, dst / d, ignore=ign)
    return dst / "app.py"


def test_set_several_batches_clicked_in_the_app(tmp_path):
    pytest.importorskip("streamlit")
    app = _app_copy(tmp_path / "app")
    cfg, scn = app.parent / "config", app.parent / "scenario"
    t = load_biology_tables(cfg)
    gh.add_growth_model_to_biology(cfg, "Test model", list(t.sgr_sw_pct_day))
    before = yaml.safe_load((scn / "batches.yaml").read_text(encoding="utf-8"))["batches"]
    ids = [b["batch_id"] for b in before]
    first, mid, nxt, last = ids[8], ids[20], ids[21], ids[-1]
    live = _md5(ROOT / "scenario" / "batches.yaml")
    env = dict(os.environ, AS_GROWTH_HISTORY=str(tmp_path / "gh.json"))
    proc = subprocess.run([sys.executable, "-c", _CLICK_BULK, str(app),
                           first, mid, last, nxt],
                          capture_output=True, text=True, timeout=900,
                          cwd=str(app.parent), env=env)
    marker = [ln for ln in proc.stdout.splitlines() if ln.startswith("<<<RESULT>>>")]
    assert marker, f"no result: {proc.stdout[-1500:]} | {proc.stderr[-1500:]}"
    r = json.loads(marker[-1][len("<<<RESULT>>>"):])
    assert not r["exceptions"], r["exceptions"]
    assert r["models"] == ["(leave as is)", "Base curve", "Test model"]
    # negative control: Apply with nothing chosen changes nothing and says so
    assert any("Nothing to change" in w for w in r["nothing"])
    assert any(f"13 batches ({first} to {mid}): growth model Test model" in i
               for i in r["info1"]), r["info1"]
    assert any("x0.86 - 14% slower than the seawater curve" in c for c in r["captions"])
    assert any("auto_calibrate_fw is ON" in c for c in r["captions"])
    n_future = len(ids) - 21
    assert any(f"{n_future} batches ({nxt} to {last}): growth model Base curve, "
               f"SW offset 0.86, FW offset 0.85" in i for i in r["info2"]), r["info2"]
    assert "Saved scenario/batches.yaml" in r["success"]      # survives the rerun
    after = {b["batch_id"]: b for b in yaml.safe_load(
        (scn / "batches.yaml").read_text(encoding="utf-8"))["batches"]}
    for i, b in enumerate(before):
        a = after[b["batch_id"]]
        if 8 <= i <= 20:
            assert a.get("growth_model") == "Test model"
            assert (a["sgr_correction"], a["fw_correction"]) \
                == (b["sgr_correction"], b["fw_correction"])
        elif i >= 21:
            assert not a.get("growth_model")                    # Base curve
            assert (a["sgr_correction"], a["fw_correction"]) == (0.86, 0.85)
        else:                                                   # outside: untouched
            assert not a.get("growth_model")
            assert (a["sgr_correction"], a["fw_correction"]) \
                == (b["sgr_correction"], b["fw_correction"])
        for k in ("input_date", "input_count", "tran_og_date", "tran_og_count",
                  "tran_og_avg_wt_g", "fcr_model", "notes"):
            assert a.get(k) == b.get(k), (b["batch_id"], k)
    # the operator's own scenario is untouched
    assert _md5(ROOT / "scenario" / "batches.yaml") == live
