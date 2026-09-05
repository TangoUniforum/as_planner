"""The dashboard must describe the config the RUN used, not the global one.

DEFECT (2026-09-04). `_run_with_workbook_bytes` is careful about this
everywhere except one line. It builds `run_config_dir` -- the throwaway copy a
method's overrides were applied to -- and the comment beside `config_used`
records why: "Reading config_dir made an LNS run report placement_method=greedy
... the panel would describe a different plan than the one on screen."

`_parse_output_workbook(out_path)` takes only a path and reads the GLOBAL
CONFIG_DIR three times:

    load_facility_config(CONFIG_DIR)                       -> per-tank density caps
    load_control(CONFIG_DIR)                               -> the R8 purge/production
                                                              boundary for 6N
    load_control(CONFIG_DIR).density_welfare_threshold_kg_m3

Five of nine callers pass a config_dir that is NOT the global one --
`optimize.config_dir_with_overrides(...)` for the knob search, the tuned
tournament's winner verification and Quick run. Those runs are graded against a
config they did not use.

SCOPE, MEASURED: no knob in today's sweep grid changes any of the three values,
and config_dir_with_overrides edits only control.yaml, so no wrong number has
been demonstrated on the current grid. This is a correctness gap that is masked
by which knobs happen to be swept -- it starts lying the moment one of those
values joins a search, or a caller passes a config whose facility.yaml differs.

SECOND DEFECT, same function. Its degradation warnings are `print()`s, and
parsing happens AFTER the `with redirect_stdout(captured):` block closes (the
run ends at app.py ~6371, the parse is at ~6401). So they reach the server
terminal and never the operator. The worst of them is silent:

    except: ... "6N density judged as PURGE (exempt) for the whole horizon"

An unreadable control.yaml exempts every 6N tank from the density judgement for
the entire horizon and the screen shows an unqualified all-clear. The function
already has the right pattern for this -- `welfare_density_note` is RETURNED,
not printed -- so the other two degradations should be returned too.
"""
import os
import shutil
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = pytest.importorskip("app", reason="app.py not importable without Streamlit")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WB = os.path.join(
    os.environ.get("ASF_SCRATCH", r"C:\Users\julian.f\AppData\Local\Temp\claude"
                   r"\C--Users-julian-f-OneDrive---Atlantic-Sapphire-Production"
                   r"-Forecasts-Tool-Python\9713a940-2d28-4a9e-af3d-22a34efbec92"
                   r"\scratchpad"),
    "ref_main.xlsm")

pytestmark = pytest.mark.skipif(
    not (os.path.exists(WB) and os.path.exists(os.path.join(ROOT, "config",
                                                            "control.yaml"))),
    reason="needs a parsed output workbook + seeded config")


@pytest.fixture
def alt_config(tmp_path):
    """A config copy whose welfare line differs from the global one."""
    d = tmp_path / "cfg"
    shutil.copytree(os.path.join(ROOT, "config"), d)
    p = d / "control.yaml"
    cy = yaml.safe_load(p.read_text()) or {}
    cy["density_welfare_threshold_kg_m3"] = 42.0
    p.write_text(yaml.safe_dump(cy, sort_keys=False))
    return str(d)


def test_the_parser_grades_against_the_config_it_is_given(alt_config):
    """THE DEFECT: run with an overridden config, graded against the global."""
    from pathlib import Path
    out = app._parse_output_workbook(Path(WB), config_dir=alt_config)
    assert out["welfare_density"] == 42.0, (
        "the workbook was graded against the global config, not the one the "
        "run actually used")


def test_the_global_config_is_still_the_default(tmp_path):
    """NEGATIVE CONTROL. Callers that pass nothing must behave exactly as
    before, or every existing surface changes meaning at once."""
    from pathlib import Path
    from forecast.config_io import load_control
    expected = float(load_control(os.path.join(ROOT, "config"))
                     .density_welfare_threshold_kg_m3 or 0) or None
    out = app._parse_output_workbook(Path(WB))
    if expected:
        assert out["welfare_density"] == expected


def test_an_unreadable_config_is_reported_not_silently_assumed(tmp_path):
    """An empty config dir must not yield an unqualified all-clear: with
    control.yaml missing, every 6N tank is exempted from the density judgement
    for the whole horizon, and that has to be visible."""
    from pathlib import Path
    empty = tmp_path / "nothing"
    empty.mkdir()
    out = app._parse_output_workbook(Path(WB), config_dir=str(empty))
    notes = " ".join(str(v) for k, v in out.items()
                     if k.endswith("_note") or k == "config_notes"
                     or isinstance(v, str) and "unreadable" in str(v))
    assert notes.strip(), (
        "the parser degraded silently — no note reached the caller, and the "
        "warnings it prints land outside the stdout capture")
    assert "6N" in notes or "purge" in notes.lower(), (
        "the 6N density exemption is the degradation that produces a false "
        "all-clear; it must be named: %r" % notes)


def test_the_notes_are_rendered_not_just_returned():
    """A returned note nothing reads is the same silence in a new place."""
    import ast
    src = open(app.__file__, encoding="utf-8").read()
    assert 'r.get("config_notes")' in src or "r.get('config_notes')" in src, (
        "config_notes is returned but never read by the results page")
    tree = ast.parse(src)          # and the file still parses
    assert tree is not None


def test_the_call_site_passes_the_runs_config():
    """The whole point: _run_with_workbook_bytes must hand over run_config_dir,
    not config_dir, or the parameter changes nothing in practice."""
    import ast
    src = open(app.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_parse_output_workbook"):
            found.append({k.arg: getattr(k.value, "id", None)
                          for k in node.keywords})
    assert found, "_parse_output_workbook is never called"
    assert any(f.get("config_dir") == "run_config_dir" for f in found), (
        "the parser is still called without the run's own config: %r" % found)
