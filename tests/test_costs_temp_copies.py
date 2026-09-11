"""No test's temp project copy may hold the operator's real config/costs.yaml.

2026-09-11: hundreds of pytest temp project copies were found under %TEMP%,
some holding the live costs file — the numbers are commercially sensitive,
and a pipeline run on such a copy also writes a CostsAndProfit sheet priced
with them. tests/conftest.py now provides `copy_config` (copytree without any
costs.yaml) and `project_without_costs` (a project dir to hand to code that
copies it, e.g. forecast.ideal_engine.prepare), and fails the session when
any file under the basetemp matches the live costs file by md5.

These tests prove the helpers do what they say and that a representative
Ideal run setup leaves no copy behind. Every number written here is made up;
the live file is only ever hashed, never read into a message.
"""
import hashlib
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "config" / "costs.yaml"


def _md5(p) -> str:
    return hashlib.md5(Path(p).read_bytes()).hexdigest()


def test_copy_config_leaves_out_every_costs_file(tmp_path, copy_config):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "control.yaml").write_text("horizon_weeks: 1\n", encoding="utf-8")
    (src / "costs.yaml").write_text("schema: 1\n", encoding="utf-8")
    (src / "costs.yaml.tmp-123").write_text("schema: 1\n", encoding="utf-8")
    (src / "sub" / "costs.yaml").write_text("schema: 1\n", encoding="utf-8")
    (src / "sub" / "keep.yaml").write_text("a: 1\n", encoding="utf-8")
    (src / "skip.txt").write_text("x", encoding="utf-8")
    copy_config(src, tmp_path / "dst", ignore_patterns=("skip.txt",))
    got = sorted(str(p.relative_to(tmp_path / "dst")).replace("\\", "/")
                 for p in (tmp_path / "dst").rglob("*") if p.is_file())
    assert got == ["control.yaml", "sub/keep.yaml"]


def test_the_scan_finds_a_copy_by_md5_and_only_a_copy(tmp_path,
                                                      live_costs_scan):
    """Negative control: the scan must fire on a byte-identical copy of the
    watched file (a made-up one here) and on nothing else."""
    ref = tmp_path / "ref" / "costs.yaml"
    ref.parent.mkdir()
    ref.write_text("schema: 1\nfixed_monthly: 12.5\n", encoding="utf-8")
    hit = tmp_path / "a" / "config" / "costs.yaml"
    hit.parent.mkdir(parents=True)
    hit.write_bytes(ref.read_bytes())
    other = tmp_path / "b" / "config" / "costs.yaml"
    other.parent.mkdir(parents=True)
    other.write_text("schema: 1\nfixed_monthly: 99.0\n", encoding="utf-8")
    found = live_costs_scan(tmp_path / "a", live=ref) + \
        live_costs_scan(tmp_path / "b", live=ref)
    assert found == [str(hit)]
    # No watched file at all: nothing can match.
    assert live_costs_scan(tmp_path, live=tmp_path / "missing.yaml") == []


def test_the_project_copy_is_the_project_minus_costs(project_without_costs):
    for sub in ("config", "scenario"):
        want = sorted(str(p.relative_to(ROOT / sub)).replace("\\", "/")
                      for p in (ROOT / sub).rglob("*")
                      if p.is_file() and not (p.name == "costs.yaml"
                                              or p.name.startswith("costs.yaml.")))
        got = sorted(str(p.relative_to(project_without_costs / sub))
                     .replace("\\", "/")
                     for p in (project_without_costs / sub).rglob("*")
                     if p.is_file())
        assert got == want, sub
        for rel in got:
            assert ((project_without_costs / sub / rel).read_bytes()
                    == (ROOT / sub / rel).read_bytes()), rel
    assert not list(project_without_costs.rglob("costs.yaml*"))


def test_a_representative_run_setup_leaves_no_live_costs_copy(
        tmp_path, copy_config, project_without_costs, live_costs_scan):
    """What the suite's Ideal and pipeline tests do — lay out an engine run
    (ideal_engine.prepare copies config/ whole) and copy the config — then
    look: no costs.yaml at all in the copies, and nothing under tmp_path or
    the shared project copy matches the live file's md5."""
    from forecast import ideal, ideal_engine as ie, scenario_io as sio
    t = ideal.default_template(
        sio.load_batches(str(project_without_costs / "scenario")))
    stream = ideal.synthetic_stream(t, 49, 280_000, horizon_weeks=60,
                                    start=ideal.STEADY_START)
    prep = ie.prepare(tmp_path / "w", stream, project_without_costs,
                      start=ideal.STEADY_START, horizon_weeks=60,
                      overrides={"sixn_production_start": "2026-01-01"})
    assert (Path(prep["config_dir"]) / "control.yaml").is_file()
    copy_config(ROOT / "config", tmp_path / "cfg")
    assert (tmp_path / "cfg" / "control.yaml").is_file()
    assert not list(tmp_path.rglob("costs.yaml*"))
    assert live_costs_scan(tmp_path) == []
    assert live_costs_scan(project_without_costs) == []
    if LIVE.is_file():
        live = _md5(LIVE)
        assert all(_md5(p) != live for p in tmp_path.rglob("*")
                   if p.is_file() and p.stat().st_size == LIVE.stat().st_size)


def test_no_temp_copy_under_the_basetemp_holds_the_live_costs(
        tmp_path_factory, live_costs_scan):
    """Everything the session has left so far (the session-end guard in
    conftest.py repeats this after the last test)."""
    leaks = live_costs_scan(tmp_path_factory.getbasetemp())
    assert leaks == [], (f"{len(leaks)} temp copies hold the live costs "
                         f"file, e.g. {os.path.dirname(leaks[0])}")
