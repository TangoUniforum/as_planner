"""forecast.ideal_engine — behavioural invariants, never pinned numbers.

The engine's figures move whenever biology, prices or the planner change, so
none are asserted. What is asserted: an empty ProductionReport really is
empty, the run is isolated (the project's config/ and scenario/ are only
read, overrides land in the temp copy and nowhere else), nothing is dropped
silently, bad inputs are refused before any file is written, one real Ideal
run is clean and plausible, reading a workbook is deterministic, and — the
part a blind reader cannot fake — what the reader reports AGREES with the
workbook's own independent sheets (Advisory, HarvestPlan, ValidationLog,
TransferPlan), parsed here with openpyxl and nothing from the module.

Exactly ONE engine run (~30 s at the 156-week default), shared through a
module-scoped fixture.
"""
import dataclasses
import datetime as dt
import hashlib
import inspect
import math
import re
import tempfile
from collections import defaultdict
from pathlib import Path

import openpyxl
import pytest
import yaml

from forecast import ideal
from forecast import ideal_engine as ie
from forecast import scenario_io as sio
from forecast.caps import METRIC_BIOMASS, METRIC_MIN_HARVEST, FacilityLimits
from forecast.config_io import load_config
from forecast.production_report import find_pr_sheet, parse_pr_worksheet

ROOT = Path(__file__).resolve().parents[1]
START = ideal.STEADY_START
PROD = {"sixn_production_start": "2026-01-01"}     # 6N production from day one
STEADY = START.year + 2          # year 3: what a 156-week Ideal run reads


def _tree_hash(d: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(d.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(d)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def _repo_hashes() -> dict:
    return {d: _tree_hash(ROOT / d) for d in ("config", "scenario")}


def _stale_temp_dirs() -> set:
    return set(Path(tempfile.gettempdir()).glob("ideal_engine_*"))


@pytest.fixture
def private_tempdir(monkeypatch, tmp_path):
    """tempfile's default dir, private to this test: a concurrent app or test
    run's own ideal_engine_* dir can never be mistaken for a leak here."""
    d = tmp_path / "tempdir"
    d.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    return d


@pytest.fixture(scope="module")
def stream():
    t = ideal.default_template(sio.load_batches(str(ROOT / "scenario")))
    return ideal.synthetic_stream(t, 49, 280_000, horizon_weeks=60, start=START)


@pytest.fixture(scope="module")
def control():
    return load_config(str(ROOT / "config"))[0]


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    """ONE real Ideal run at the defaults, kept so the workbook can be re-read."""
    keep = tmp_path_factory.mktemp("ideal_engine_kept")
    tdir = tmp_path_factory.mktemp("private_tempdir")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tempfile, "tempdir", str(tdir))
        before = _repo_hashes()
        run = ie.ideal_run(49, 280_000, 3_800_000, ROOT, keep_dir=keep)
        after = _repo_hashes()
        leaked = sorted(tdir.glob("ideal_engine_*"))
    return dict(run=run, before=before, after=after, leaked=leaked)


@pytest.fixture(scope="module")
def kept(engine):
    """The kept run's own config, pricing and limits, as read_workbook wants."""
    run = engine["run"]
    keep = Path(run.out_path).parent
    control, _t, facility = load_config(str(keep / "config"))
    with open(keep / "config" / "economics.yaml", encoding="utf-8") as f:
        econ = yaml.safe_load(f)
    flimits, _s = sio.load_limits(str(keep / "scenario"), control)
    args = (run.out_path, control, facility, ideal.price_bands(econ),
            float(econ.get("model_cv_pct", 18.0)), control.default_hog_yield)
    return dict(run=run, control=control, flimits=flimits, args=args)


# --- the empty ProductionReport --------------------------------------------

def test_empty_pr_parses_to_an_empty_facility_at_the_closing_date(tmp_path):
    closing = dt.date(2027, 1, 3)
    path = ie.write_empty_pr(tmp_path / "pr.xlsx", closing)
    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        ws = find_pr_sheet(wb)
        assert ws is not None
        got, og, fw = parse_pr_worksheet(ws, quiet=True)
    finally:
        wb.close()
    assert got == closing
    assert og == [] and fw == []


# --- prepare: isolation ------------------------------------------------------

def test_prepare_writes_only_under_work_dir(tmp_path, stream):
    before = _repo_hashes()
    work = tmp_path / "work"
    prep = ie.prepare(work, stream, ROOT, start=START, horizon_weeks=60,
                      overrides=PROD)
    for key in ("pr_path", "config_dir", "scenario_dir"):
        assert Path(prep[key]).resolve().is_relative_to(work.resolve())
    assert _repo_hashes() == before
    # limits.yaml and every config file except control.yaml are verbatim
    assert ((ROOT / "scenario" / "limits.yaml").read_bytes()
            == (Path(prep["scenario_dir"]) / "limits.yaml").read_bytes())
    for p in (ROOT / "config").iterdir():
        if p.is_file() and p.name != "control.yaml":
            assert (Path(prep["config_dir"]) / p.name).read_bytes() == p.read_bytes()


def test_unknown_override_is_refused_before_anything_is_written(tmp_path, stream):
    work = tmp_path / "work"
    with pytest.raises(ValueError, match="not allowed"):
        ie.prepare(work, stream, ROOT, start=START, horizon_weeks=60,
                   overrides={**PROD, "density_target_pct": 0.99})
    assert not work.exists()


def test_overrides_land_in_the_temp_control_and_nowhere_else(tmp_path, stream):
    live_bytes = (ROOT / "config" / "control.yaml").read_bytes()
    ov = {**PROD, "max_biomass_kg": 1_234_567.0, "scenario_name": "probe"}
    prep = ie.prepare(tmp_path / "w", stream, ROOT, start=START,
                      horizon_weeks=60, overrides=ov)
    live = yaml.safe_load(live_bytes)
    with open(Path(prep["config_dir"]) / "control.yaml", encoding="utf-8") as f:
        temp = yaml.safe_load(f)
    changed = {k for k in set(live) | set(temp) if live.get(k) != temp.get(k)}
    assert changed <= set(ov) | {"horizon_weeks"}
    assert temp["max_biomass_kg"] == 1_234_567.0
    assert temp["horizon_weeks"] == 60
    assert (ROOT / "config" / "control.yaml").read_bytes() == live_bytes
    assert b"1234567" not in (Path(prep["scenario_dir"]) / "limits.yaml").read_bytes()


# --- prepare: system-constraint what-ifs (density, system limits, moves) -----

def _yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _og_systems_at(cap):
    tanks = _yaml(ROOT / "config" / "facility.yaml")["tanks"]
    return sorted({t["system_id"] for t in tanks
                   if t["type"] == "OG" and t["max_density_kg_m3"] == cap})


def test_density_overrides_land_on_exactly_that_systems_tanks(tmp_path, stream):
    live_path = ROOT / "config" / "facility.yaml"
    live_bytes = live_path.read_bytes()
    before = _repo_hashes()
    live = yaml.safe_load(live_bytes)["tanks"]
    systems = sorted({t["system_id"] for t in live})
    a, b = systems[0], systems[-1]
    want = {a: 95, b: 101.5}
    prep = ie.prepare(tmp_path / "w", stream, ROOT, start=START,
                      horizon_weeks=60, overrides=PROD, density_overrides=want)
    temp = _yaml(Path(prep["config_dir"]) / "facility.yaml")["tanks"]
    assert len(temp) == len(live)
    hit = 0
    for lt, tt in zip(live, temp):
        assert {k: v for k, v in tt.items() if k != "max_density_kg_m3"} == \
               {k: v for k, v in lt.items() if k != "max_density_kg_m3"}
        if lt["system_id"] in want:
            assert tt["max_density_kg_m3"] == want[lt["system_id"]]
            hit += 1
        else:
            assert tt["max_density_kg_m3"] == lt["max_density_kg_m3"]
    assert hit == sum(1 for t in live if t["system_id"] in want) > 0
    # The engine's own reader sees the same thing.
    fac = load_config(prep["config_dir"])[2]
    for t in fac.tanks:
        if t.system_id in want:
            assert t.max_density_kg_m3 == want[t.system_id]
    assert prep["density_overrides"] == {a: 95.0, b: 101.5}
    assert prep["system_overrides"] == {}
    # No system override -> limits.yaml verbatim; the live files untouched.
    assert ((Path(prep["scenario_dir"]) / "limits.yaml").read_bytes()
            == (ROOT / "scenario" / "limits.yaml").read_bytes())
    assert live_path.read_bytes() == live_bytes
    assert _repo_hashes() == before


def test_system_overrides_land_in_system_defaults_and_6n_purge_stays(
        tmp_path, stream, control):
    live_path = ROOT / "scenario" / "limits.yaml"
    live_bytes = live_path.read_bytes()
    live = yaml.safe_load(live_bytes)
    plain = sorted(s for s, blk in live["system_defaults"].items()
                   if "modes" not in blk)
    moded = sorted(s for s, blk in live["system_defaults"].items()
                   if "modes" in blk)
    assert "OG6N" in moded and len(plain) >= 2
    s1, s2 = plain[0], plain[1]
    want = {s1: {"biomass": 450_000}, s2: {"feed_per_day": 3_300},
            "OG6N": {"biomass": 460_000, "feed_per_day": 3_200}}
    prep = ie.prepare(tmp_path / "w", stream, ROOT, start=START,
                      horizon_weeks=60, overrides=PROD, system_overrides=want)
    temp = _yaml(Path(prep["scenario_dir"]) / "limits.yaml")
    sd, ld = temp["system_defaults"], live["system_defaults"]
    assert set(sd) == set(ld)
    assert sd[s1] == {**ld[s1], "biomass": 450_000}
    assert sd[s2] == {**ld[s2], "feed_per_day": 3_300}
    six = sd["OG6N"]
    assert six["modes"]["production"]["biomass"] == 460_000
    assert six["modes"]["purge"] == ld["OG6N"]["modes"]["purge"]  # ruling stays
    assert six["feed_per_day"] == 3_200
    assert set(six) == set(ld["OG6N"])            # no system-level biomass added
    for s in set(ld) - {s1, s2, "OG6N"}:
        assert sd[s] == ld[s]
    # Per-week rows (system and facility) are data the engine resolves first.
    assert temp["system"] == live["system"]
    assert temp["facility"] == live["facility"]
    # Resolved by the engine's own loader, against a control whose 6N is in
    # purge before sixn_production_start and in production after it.
    _fl, sl = sio.load_limits(prep["scenario_dir"], control)
    prod_from = control.sixn_production_start
    purge_wk = ie._week_label(prod_from - dt.timedelta(weeks=10))
    prod_wk = ie._week_label(prod_from + dt.timedelta(weeks=10))
    assert sl.resolve(prod_wk, "OG6N", "biomass") == 460_000
    assert sl.resolve(purge_wk, "OG6N", "biomass") == \
        ld["OG6N"]["modes"]["purge"]["biomass"]
    assert sl.resolve(prod_wk, s1, "biomass") == 450_000
    assert sl.resolve(prod_wk, s2, "feed_per_day") == 3_300
    assert prep["system_overrides"] == {
        s1: {"biomass": 450_000.0}, s2: {"feed_per_day": 3_300.0},
        "OG6N": {"biomass": 460_000.0, "feed_per_day": 3_200.0}}
    # No density override -> facility.yaml verbatim; the live file untouched.
    assert ((Path(prep["config_dir"]) / "facility.yaml").read_bytes()
            == (ROOT / "config" / "facility.yaml").read_bytes())
    assert live_path.read_bytes() == live_bytes


def test_per_week_system_rows_survive_an_override_and_still_win(
        tmp_path, stream, control):
    """A dated per-week `system` row outranks the system default in the
    engine's own resolver, so an override must never touch those rows. The
    live file has none (`system: []`), which made the comparison above
    vacuous; so rows are injected into a PRIVATE copy of the project. They
    must come through unchanged, win in their week, and the override must
    apply in every other week — for a plain system and for OG6N."""
    proj = tmp_path / "proj"
    ie._copy_tree(ROOT / "config", proj / "config")
    ie._copy_tree(ROOT / "scenario", proj / "scenario")
    lp = proj / "scenario" / "limits.yaml"
    doc = _yaml(lp)
    prod_from = control.sixn_production_start
    wk = [ie._week_label(prod_from + dt.timedelta(weeks=n)) for n in (10, 11, 12)]
    other = ie._week_label(prod_from + dt.timedelta(weeks=20))
    plain = sorted(s for s, b in doc["system_defaults"].items() if "modes" not in b)
    s1, s2 = plain[0], plain[1]
    rows = [{"week": wk[0], "system": s1, "metric": "biomass", "value": 111_111.0},
            {"week": wk[1], "system": "OG6N", "metric": "biomass", "value": 222_222.0},
            {"week": wk[2], "system": s2, "metric": "feed_per_day", "value": 1_234.0}]
    doc["system"] = rows
    with open(lp, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, sort_keys=False)
    want = {s1: {"biomass": 450_000}, "OG6N": {"biomass": 460_000},
            s2: {"feed_per_day": 3_300}}
    prep = ie.prepare(tmp_path / "w", stream, proj, start=START,
                      horizon_weeks=60, overrides=PROD, system_overrides=want)
    assert _yaml(Path(prep["scenario_dir"]) / "limits.yaml")["system"] == rows
    _fl, sl = sio.load_limits(prep["scenario_dir"], control)
    assert sl.resolve(wk[0], s1, "biomass") == 111_111          # the row wins
    assert sl.resolve(other, s1, "biomass") == 450_000          # the override
    assert sl.resolve(wk[1], "OG6N", "biomass") == 222_222
    assert sl.resolve(other, "OG6N", "biomass") == 460_000
    assert sl.resolve(wk[2], s2, "feed_per_day") == 1_234
    assert sl.resolve(other, s2, "feed_per_day") == 3_300


def test_overrides_never_leak_through_shared_yaml_blocks():
    """A YAML anchor makes two systems ONE dict object; writing one must not
    move the other, and the parsed input must stay untouched. A moded system
    with no production entry still takes biomass into modes.production, so
    its purge weeks can never move."""
    shared = {"biomass": 400_000.0, "feed_per_day": 3000.0}
    doc = {"system_defaults": {
        "A": shared, "B": shared,
        "M": {"feed_per_day": 3000.0, "modes": {"purge": {"biomass": 700_000.0}}}},
        "system": [], "facility": []}
    out = ie._apply_system(doc, {"A": {"biomass": 450_000.0},
                                 "M": {"biomass": 460_000.0}})
    assert out["system_defaults"]["A"]["biomass"] == 450_000.0
    assert out["system_defaults"]["B"]["biomass"] == 400_000.0   # not dragged
    assert shared["biomass"] == 400_000.0                          # input intact
    m = out["system_defaults"]["M"]
    assert m["modes"]["production"]["biomass"] == 460_000.0
    assert m["modes"]["purge"]["biomass"] == 700_000.0 and "biomass" not in m
    assert "production" not in doc["system_defaults"]["M"]["modes"]
    tank = {"system_id": "A", "max_density_kg_m3": 85}
    fac = ie._apply_density({"tanks": [tank]}, {"A": 95.0})
    assert fac["tanks"][0]["max_density_kg_m3"] == 95.0
    assert tank["max_density_kg_m3"] == 85                         # input intact


def test_the_move_budget_is_a_control_override_and_a_whole_number(tmp_path,
                                                                  stream):
    prep = ie.prepare(tmp_path / "w", stream, ROOT, start=START,
                      horizon_weeks=60,
                      overrides={**PROD, "max_transfers_per_week": 18})
    doc = _yaml(Path(prep["config_dir"]) / "control.yaml")
    assert doc["max_transfers_per_week"] == 18
    assert isinstance(doc["max_transfers_per_week"], int)
    assert load_config(prep["config_dir"])[0].max_transfers_per_week == 18
    assert prep["overrides"]["max_transfers_per_week"] == 18


_NAN = float("nan")


@pytest.mark.parametrize("kw", [
    dict(density_overrides={"NO_SUCH_SYSTEM": 95}),
    dict(density_overrides={"OG1N": 0}),
    dict(density_overrides={"OG1N": -85}),
    dict(density_overrides={"OG1N": _NAN}),
    dict(density_overrides={"OG1N": float("inf")}),
    dict(density_overrides={"OG1N": True}),
    dict(density_overrides={"OG1N": "95"}),
    dict(density_overrides=[("OG1N", 95)]),
    dict(system_overrides={"NO_SUCH_SYSTEM": {"biomass": 450_000}}),
    dict(system_overrides={"OG1N": {"density": 95}}),
    dict(system_overrides={"OG1N": {"biomass": 0}}),
    dict(system_overrides={"OG1N": {"biomass": -1}}),
    dict(system_overrides={"OG1N": {"feed_per_day": _NAN}}),
    dict(system_overrides={"OG1N": {}}),
    dict(system_overrides={"OG1N": 450_000}),
    # a good one next to a bad one: nothing at all is applied
    dict(density_overrides={"OG1N": 95},
         system_overrides={"OG1N": {"biomass": 450_000, "feed": 1}}),
    dict(overrides={**PROD, "max_transfers_per_week": 15.5}),
    dict(overrides={**PROD, "max_transfers_per_week": 16.0}),
    dict(overrides={**PROD, "max_transfers_per_week": 0}),
    dict(overrides={**PROD, "max_transfers_per_week": -3}),
    dict(overrides={**PROD, "max_transfers_per_week": True}),
])
def test_bad_system_constraints_are_refused_before_anything_is_written(
        tmp_path, stream, kw):
    before = _repo_hashes()
    work = tmp_path / "w"
    kw = {"overrides": PROD, **kw}
    with pytest.raises(ValueError):
        ie.prepare(work, stream, ROOT, start=START, horizon_weeks=60, **kw)
    assert not work.exists()
    assert _repo_hashes() == before


def test_an_unknown_system_is_named_with_the_known_ones(tmp_path, stream):
    with pytest.raises(ValueError, match="OG1N"):
        ie.prepare(tmp_path / "w", stream, ROOT, start=START, horizon_weeks=60,
                   overrides=PROD, density_overrides={"OG9Z": 95})
    with pytest.raises(ValueError, match="OG1N"):
        ie.prepare(tmp_path / "w", stream, ROOT, start=START, horizon_weeks=60,
                   overrides=PROD, system_overrides={"OG9Z": {"biomass": 1.0}})


def test_the_what_if_arguments_are_keyword_only_and_default_to_none():
    for fn in (ie.prepare, ie.run_schedule, ie.ideal_run):
        ps = inspect.signature(fn).parameters
        for name in ("density_overrides", "system_overrides"):
            assert ps[name].kind is inspect.Parameter.KEYWORD_ONLY, (fn, name)
            assert ps[name].default is None, (fn, name)
    fields = [f.name for f in dataclasses.fields(ie.EngineRun)]
    assert fields[-2:] == ["density_overrides", "system_overrides"]
    assert all(f.default is None for f in dataclasses.fields(ie.EngineRun)
               if f.name in ("density_overrides", "system_overrides"))


# --- prepare: nothing dropped silently, bad inputs refused -------------------

def test_batches_entering_og_on_or_before_the_start_are_dropped_and_reported(
        tmp_path, stream):
    on_start = dataclasses.replace(stream[1], batch_id="ON_START",
                                   tran_og_date=START)
    batches = list(stream) + [on_start]
    prep = ie.prepare(tmp_path / "w", batches, ROOT, start=START,
                      horizon_weeks=60, overrides=PROD)
    want = {b.batch_id for b in batches if b.tran_og_date <= START}
    assert "ON_START" in want
    assert set(prep["dropped"]) == want
    written = {b.batch_id for b in sio.load_batches(prep["scenario_dir"])}
    assert written == {b.batch_id for b in batches} - want
    assert written


def test_empty_start_in_6n_purge_mode_is_refused_not_patched(tmp_path, stream):
    work = tmp_path / "w"
    with pytest.raises(ValueError, match="PURGE"):
        ie.prepare(work, stream, ROOT, start=START, horizon_weeks=60,
                   overrides={"sixn_production_start": "2031-01-01"})
    assert not work.exists()


def test_empty_start_needs_a_start_and_takes_no_manual_events(tmp_path, stream):
    with pytest.raises(ValueError):
        ie.prepare(tmp_path / "a", stream, ROOT, horizon_weeks=60, overrides=PROD)
    with pytest.raises(ValueError):
        ie.prepare(tmp_path / "b", stream, ROOT, start=START, horizon_weeks=60,
                   overrides=PROD, include_manual_events=True)


def test_real_pr_mode_reads_the_start_from_the_report_and_keeps_every_batch(
        tmp_path, stream):
    closing = dt.date(2026, 8, 31)
    pr = ie.write_empty_pr(tmp_path / "pr.xlsx", closing)
    prep = ie.prepare(tmp_path / "w", stream, ROOT, pr_path=pr,
                      horizon_weeks=60, include_manual_events=True)
    assert prep["start"] == closing + dt.timedelta(days=1)
    assert prep["dropped"] == ()
    written = {b.batch_id for b in sio.load_batches(prep["scenario_dir"])}
    assert written == {b.batch_id for b in stream}
    # The events this PR's closing names must exist AND be the file the run
    # reports; a missing directory is a failure here, never a skip.
    src = ROOT / "scenario" / "manual_events"
    assert src.is_dir()
    mine = f"{closing.isoformat()}.yaml"
    assert (src / mine).is_file()
    assert prep["manual_events_file"] == mine
    copied = Path(prep["scenario_dir"]) / "manual_events"
    assert ({p.name for p in copied.glob("*.yaml")}
            == {p.name for p in src.glob("*.yaml")})
    with pytest.raises(ValueError, match="disagrees"):
        ie.prepare(tmp_path / "w2", stream, ROOT, pr_path=pr, horizon_weeks=60,
                   start=dt.date(2026, 9, 7))


def test_run_schedule_refuses_before_the_engine_runs(tmp_path, stream,
                                                     private_tempdir):
    with pytest.raises(ValueError, match="unknown method"):
        ie.run_schedule(stream, ROOT, start=START, horizon_weeks=60,
                        overrides=PROD, method="no-such-method")
    with pytest.raises(ValueError, match="outside the run horizon"):
        ie.run_schedule(stream, ROOT, start=START, horizon_weeks=60,
                        overrides=PROD, years=[START.year + 5])
    with pytest.raises(ValueError, match="no complete year"):
        ie.run_schedule(stream, ROOT, start=START, horizon_weeks=30,
                        overrides=PROD)
    assert not _stale_temp_dirs()               # a refused run cleans up too
    keep = tmp_path / "keep"
    keep.mkdir()
    (keep / "old.txt").write_text("x")
    with pytest.raises(ValueError, match="not empty"):
        ie.run_schedule(stream, ROOT, start=START, horizon_weeks=60,
                        overrides=PROD, keep_dir=keep)


@pytest.mark.parametrize("cadence, size, cap", [
    (49.5, 280_000, 3_800_000),         # would have been truncated to 49
    (49, 280_000.7, 3_800_000),         # would have been truncated to 280,000
    (49.0, 280_000, 3_800_000),         # a float is not a whole number type
    (True, 280_000, 3_800_000),
    (49, 0, 3_800_000),
    (49, 280_000, float("nan")),
    (49, 280_000, 0),
])
def test_ideal_run_refuses_what_it_would_have_truncated(cadence, size, cap,
                                                        private_tempdir):
    with pytest.raises(ValueError):
        ie.ideal_run(cadence, size, cap, ROOT)
    assert not _stale_temp_dirs()               # refused before any run


# --- the read window ---------------------------------------------------------

def test_the_default_read_year_is_the_last_complete_year_of_the_horizon():
    assert ie.last_complete_year(START, 104) == START.year + 1
    assert ie.last_complete_year(START, 156) == START.year + 2
    # A mid-year start: the first and last years are partial.
    assert ie.last_complete_year(dt.date(START.year, 7, 5), 104) == START.year + 1
    # A 53-week ISO year counts as complete only with all 53 of its weeks.
    y53 = next(y for y in range(START.year, START.year + 12)
               if dt.date(y, 12, 28).isocalendar()[1] == 53)
    first = dt.date.fromisocalendar(y53, 1, 1)
    assert ie.last_complete_year(first, 53) == y53
    with pytest.raises(ValueError, match="no complete year"):
        ie.last_complete_year(first, 52)
    with pytest.raises(ValueError, match="no complete year"):
        ie.last_complete_year(START, 30)
    # ideal_run's default horizon is L1's steady window's horizon.
    default = inspect.signature(ie.ideal_run).parameters["horizon_weeks"].default
    assert default == ideal.HORIZON_WEEKS
    assert ie.last_complete_year(START, default) == START.year + 2


# --- ONE real engine run -----------------------------------------------------

def test_a_real_ideal_run_is_clean_plausible_and_isolated(engine, control):
    run = engine["run"]
    assert run.rc == 0
    assert run.horizon_weeks == ideal.HORIZON_WEEKS
    assert run.audits["reconciliation_flags"] == 0
    assert run.audits["tank_continuity_flags"] == 0
    assert run.audits["manual_events_included"] is False
    assert run.audits["manual_events_file"] is None
    assert set(run.years) == {STEADY}             # year 3, not year 2
    fails = [g for g in ie.gates(run, STEADY, control) if g.status == "FAIL"]
    assert not fails, fails
    assert run.years[STEADY].harvest_fish > 0
    assert any(run.layout.values())
    assert run.tank_sequence
    assert run.dropped_batches                    # the pre-start S000 batch
    assert run.overrides["max_biomass_kg"] == 3_800_000
    assert (ie._as_date(run.overrides["sixn_production_start"], "")
            < run.start)
    assert engine["after"] == engine["before"]    # project only read
    assert not engine["leaked"]                   # temp dir removed
    assert run.out_path and Path(run.out_path).is_file()


def test_a_what_if_run_is_judged_against_the_limits_it_ran_with(
        tmp_path, stream, private_tempdir):
    """ONE short real run (60 weeks, ~10 s) with raised OG density caps, a
    raised system limit and a different move budget. The engine must RUN on
    them (its own SystemLimitsAudit quotes the new caps) and the Ideal's
    verdict must be JUDGED on them: the run's YearRead is the read with the
    run's kept config, and the same workbook read against the live density
    caps can only find as many or more tank-weeks over their cap."""
    raised = _og_systems_at(85)
    assert raised and "OG6N" not in raised
    before = _repo_hashes()
    sys1 = raised[0]
    keep = tmp_path / "k"
    run = ie.run_schedule(
        stream, ROOT, start=START, horizon_weeks=60,
        overrides={**PROD, "max_transfers_per_week": 16},
        density_overrides={s: 95 for s in raised},
        system_overrides={sys1: {"biomass": 450_000, "feed_per_day": 3_300},
                          "OG6N": {"biomass": 450_000}},
        keep_dir=keep)
    assert run.rc == 0
    assert run.density_overrides == {s: 95.0 for s in raised}
    assert run.system_overrides[sys1] == {"biomass": 450_000.0,
                                          "feed_per_day": 3_300.0}
    assert _repo_hashes() == before
    assert not list(private_tempdir.glob("ideal_engine_*"))

    # The kept run's own inputs carry the what-ifs.
    kc, _t, kf = load_config(str(keep / "config"))
    lc, _t2, lf = load_config(str(ROOT / "config"))
    assert kc.max_transfers_per_week == 16 != lc.max_transfers_per_week
    for t in kf.tanks:
        live = next(x for x in lf.tanks if (x.system_id, x.tank_id)
                    == (t.system_id, t.tank_id))
        assert t.max_density_kg_m3 == (95 if t.system_id in raised
                                       else live.max_density_kg_m3)

    # The ENGINE ran on the new system limits: its own audit quotes them.
    sla = _sheet(run.out_path, "SystemLimitsAudit", "Week")
    live_sd = _yaml(ROOT / "scenario" / "limits.yaml")["system_defaults"]
    rows = defaultdict(list)
    for r in sla:
        rows[r["System"]].append(r)
    assert rows[sys1] and rows["OG6N"] and rows[raised[1]]
    assert all(r["Biomass_cap"] == 450_000 and r["Feed_cap"] == 3_300
               for r in rows[sys1])
    assert all(r["Biomass_cap"] == 450_000 for r in rows["OG6N"])  # production
    assert all(r["Biomass_cap"] == live_sd[raised[1]]["biomass"]
               for r in rows[raised[1]])

    # The verdict: the run's read IS the read on its kept config ...
    econ = _yaml(keep / "config" / "economics.yaml")
    fl, _s = sio.load_limits(str(keep / "scenario"), kc)
    years = sorted(run.years)
    rest = (ideal.price_bands(econ), float(econ.get("model_cv_pct", 18.0)),
            kc.default_hog_yield, years)
    mine = ie.read_workbook(run.out_path, kc, kf, *rest, facility_limits=fl)
    assert mine["years"] == run.years
    # ... and the live density caps (same control, so only the cap differs)
    # never count fewer tank-weeks over.
    livecap = ie.read_workbook(run.out_path, kc, lf, *rest, facility_limits=fl)
    for y in years:
        k, l = run.years[y], livecap["years"][y]
        assert l.r8_over_tank_weeks >= k.r8_over_tank_weeks, y
        w = k.r8_worst
        if w is not None and w["system"] in raised and w["stage"] == "SW":
            assert w["cap"] == 95
        # the move budget the verdict and its gate use is the run's own
        assert k.weeks_over_move_budget == sum(
            1 for wk, d in mine["weekly"].items()
            if wk.startswith(f"{y}-") and d["moves"] > 16)
        g = next(x for x in ie.gates(run, y, lc) if x.name == "Handling budget")
        assert "budget 16)" in g.detail or "over 16 moves" in g.detail, g.detail
        assert run.audits["move_budget"] == 16


def test_gates_keep_their_order_and_only_fail_rows_decide(engine, control):
    run = engine["run"]
    base = ie.gates(run, STEADY, control)
    names = [g.name for g in base]
    y = run.years[STEADY]

    def with_year(**kw):
        return dataclasses.replace(
            run, years={STEADY: dataclasses.replace(y, **kw)})

    warn_only = with_year(under_floor_weeks=2, weeks_over_move_budget=1,
                          r8_over_tank_weeks=3,
                          r8_worst=dict(week="x", tank=1, batch="b", density=99.0,
                                        cap=85.0, system="OG1N", stage="SW"),
                          sys_feed_over_weeks=2,
                          sys_worst=dict(system="OG2S", week="x", kind="feed",
                                         value=3400.0, cap=3000.0,
                                         unit="kg/day", ratio=3400 / 3000))
    g = ie.gates(warn_only, STEADY, control)
    assert [x.name for x in g] == names
    assert ie.plausible(g) and any(x.status == "WARN" for x in g)
    sysg = next(x for x in g if x.name == "System limits")
    assert sysg.status == "WARN" and "OG2S" in sysg.detail   # a WARN, never a FAIL

    for bad in (with_year(zero_weeks=1),
                with_year(peak_pct_of_cap=1.0 + ideal.PEAK_TOLERANCE + 0.01),
                dataclasses.replace(run, audits={**run.audits,
                                                 "reconciliation_flags": 1}),
                dataclasses.replace(run, audits={
                    **run.audits, "input_conservation": [
                        "*** 1 batch(es) DROPPED — 5 stocked fish ***"]}),
                dataclasses.replace(run, rc=1)):
        g = ie.gates(bad, STEADY, control)
        assert [x.name for x in g] == names
        assert not ie.plausible(g)
    with pytest.raises(ValueError):
        ie.gates(run, STEADY + 10, control)


def test_read_workbook_is_deterministic_and_matches_the_run(kept):
    run = kept["run"]
    a = ie.read_workbook(*kept["args"], [STEADY], facility_limits=kept["flimits"])
    b = ie.read_workbook(*kept["args"], [STEADY], facility_limits=kept["flimits"])
    assert a == b
    assert a["years"] == run.years
    assert a["layout"] == run.layout
    assert a["tank_sequence"] == run.tank_sequence


# --- the reader, checked against the workbook's OWN independent sheets -------

def _sheet(path, name, first):
    """Rows of one sheet as dicts, parsed here with openpyxl alone."""
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        hdr, rows = None, []
        for r in wb[name].iter_rows(values_only=True):
            if hdr is None:
                if r and r[0] == first:
                    hdr = list(r)
                continue
            if r and any(x is not None for x in r):
                rows.append(dict(zip(hdr, r)))
    finally:
        wb.close()
    assert hdr is not None, f"{name}: no header row starting {first!r}"
    return rows


_DENSITY = re.compile(r"^(\d{4}-W\d{2}): (\S+) \(batch [^)]*\) at "
                      r"([\d.]+) kg/m.? > cap ([\d.]+)")


def test_the_reader_agrees_with_the_workbooks_own_independent_sheets(kept):
    """Standing, harvest, zero weeks, R8 and moves, per week, against sheets
    the reader never opens (Advisory, ValidationLog) or parses its own way.

    Rounding is the only slack allowed: BatchLocations biomass is written to
    0 dp and BiologyProjection to 1 dp, Advisory to 0 dp.
    """
    path = kept["run"].out_path
    adv = _sheet(path, "Advisory", "Week")
    hp = _sheet(path, "HarvestPlan", "Week")
    vl = _sheet(path, "ValidationLog", "#")
    tp = _sheet(path, "TransferPlan", "Week")
    bl = _sheet(path, "BatchLocations", "Week")
    bp = _sheet(path, "BiologyProjection", "Batch")
    sla = _sheet(path, "SystemLimitsAudit", "Week")

    years = sorted({int(str(r["Week"])[:4]) for r in adv})
    got = ie.read_workbook(*kept["args"], years, facility_limits=kept["flimits"])
    weekly = got["weekly"]

    rows_in = defaultdict(int)                  # rounded rows behind a week
    for r in bl:
        rows_in[str(r["Week"])] += 1
    for r in bp:
        if r["Stage"] in ("FW", "EGG"):
            rows_in[str(r["Week"])] += 1
    fw_seen = any(r["Stage"] in ("FW", "EGG") and (r["Biomass_kg"] or 0) > 0
                  for r in bp)
    assert fw_seen                   # a stream always has fish in freshwater

    hv_rows = defaultdict(int)
    hv_weeks = set()
    for r in hp:
        hv_rows[str(r["Week"])] += 1
        if (r["Count (fish)"] or 0) > 0:
            hv_weeks.add(str(r["Week"]))

    pairs = defaultdict(set)
    for r in tp:
        if r["Type"] == "Transfer" and (r["Count (fish)"] or 0) >= 0.5:
            pairs[str(r["Week"])].add((str(r["From_Tank"]).strip(),
                                       str(r["To_Tank"]).strip()))

    dens = defaultdict(list)
    for r in vl:
        if r.get("Category") == "WARNING - Density":
            m = _DENSITY.match(str(r["Detail"]))
            assert m, f"unparsed Density row {r['Detail']!r}"
            dens[int(m[1][:4])].append((m[1], m[2], float(m[3]), float(m[4])))

    zero_total = 0
    for y in years:
        Y = got["years"][y]
        aw = {str(r["Week"]): r for r in adv if str(r["Week"]).startswith(f"{y}-")}
        assert set(w for w in weekly if w.startswith(f"{y}-")) == set(aw)
        assert Y.weeks == len(aw)

        # Standing = OG + FW, week by week, vs Advisory's Total_Biomass.
        for w, r in aw.items():
            tol = 0.5 * rows_in[w] + 0.5
            assert math.isclose(weekly[w]["standing_kg"],
                                float(r["Total_Biomass (kg)"]), abs_tol=tol), w
        assert math.isclose(Y.standing_peak_kg,
                            max(float(r["Total_Biomass (kg)"]) for r in aw.values()),
                            abs_tol=0.5 * max(rows_in[w] for w in aw) + 0.5)

        # Harvest count, week by week and for the year, vs Advisory.
        for w, r in aw.items():
            assert math.isclose(weekly[w]["harvest_fish"],
                                float(r["Harvest_Count"] or 0.0),
                                abs_tol=0.5 * hv_rows[w] + 0.5), w
        assert math.isclose(
            Y.harvest_fish, sum(float(r["Harvest_Count"] or 0) for r in aw.values()),
            abs_tol=0.5 * sum(hv_rows[w] for w in aw) + 0.5 * len(aw))

        # Zero weeks = weeks of the year with no HarvestPlan row at all.
        want_zero = sum(1 for w in aw if w not in hv_weeks)
        assert Y.zero_weeks == want_zero
        zero_total += want_zero

        # R8: never more tank-weeks than the engine's own Density warnings,
        # never fewer than the ones clearly over their cap after rounding
        # (BatchLocations density is written to 1 dp).
        hi = {(w, loc) for w, loc, _d, _c in dens[y]}
        lo = {(w, loc) for w, loc, d, c in dens[y] if d >= c + 0.2}
        assert len(lo) <= Y.r8_over_tank_weeks <= len(hi), (y, len(lo), len(hi))

        # Moves: distinct (From_Tank, To_Tank) Transfer pairs per week.
        for w in aw:
            assert weekly[w]["moves"] == len(pairs.get(w, ())), w
        assert Y.moves_max == max(len(pairs.get(w, ())) for w in aw)

        # Per-system limits: the engine's own SystemLimitsAudit flags,
        # counted here independently, system-week by system-week.
        yr = [r for r in sla if str(r["Week"]).startswith(f"{y}-")]
        assert Y.sys_bio_over_weeks == sum(1 for r in yr if r["Bio_flag"])
        assert Y.sys_feed_over_weeks == sum(1 for r in yr if r["Feed_flag"])
        assert (Y.sys_worst is None) == (
            Y.sys_bio_over_weeks + Y.sys_feed_over_weeks == 0)

    # An empty facility has nothing big enough to harvest in its first weeks,
    # so the zero-week check above had real zeros to find.
    assert zero_total > 0


def test_an_override_of_zero_means_no_cap_and_no_floor_as_in_the_engine(kept):
    ctl = kept["control"]
    base = ie.read_workbook(*kept["args"], [STEADY],
                            facility_limits=kept["flimits"])
    y0 = base["years"][STEADY]
    weeks = sorted(w for w in base["weekly"] if w.startswith(f"{STEADY}-"))
    assert y0.capped_weeks == len(weeks)          # the live config caps

    zero = FacilityLimits(overrides={
        **kept["flimits"].overrides,
        **{(w, METRIC_BIOMASS): 0.0 for w in weeks},
        **{(w, METRIC_MIN_HARVEST): 0.0 for w in weeks}})
    got = ie.read_workbook(*kept["args"], [STEADY], facility_limits=zero)
    y = got["years"][STEADY]
    assert y.capped_weeks == 0 and y.peak_pct_of_cap == 0.0
    assert y.floor_min is None and y.floor_max is None
    assert y.under_floor_weeks == 0
    run = dataclasses.replace(kept["run"], years={STEADY: y})
    g = {x.name: x for x in ie.gates(run, STEADY, ctl)}
    assert g["Biomass cap"].status == "PASS"
    assert "no biomass cap" in g["Biomass cap"].detail
    assert "no harvest floor" in g["Harvest floor"].detail

    # One week capped below what stands in it, one floored above what it
    # harvests: both are resolved PER WEEK, and the floor the gate quotes is
    # the resolved one, not control.min_harvest_per_week.
    w_cap, w_floor = weeks[len(weeks) // 2], weeks[-1]
    tight = base["weekly"][w_cap]["standing_kg"] / 2.0
    floor = base["weekly"][w_floor]["harvest_fish"] + 12_345.0
    one = FacilityLimits(overrides={
        **zero.overrides, (w_cap, METRIC_BIOMASS): tight,
        (w_floor, METRIC_MIN_HARVEST): floor})
    y1 = ie.read_workbook(*kept["args"], [STEADY],
                          facility_limits=one)["years"][STEADY]
    assert y1.capped_weeks == 1
    assert math.isclose(y1.peak_pct_of_cap, 2.0)
    assert y1.floor_min == y1.floor_max == floor
    assert y1.under_floor_weeks == 1
    g1 = {x.name: x for x in ie.gates(
        dataclasses.replace(run, years={STEADY: y1}), STEADY, ctl)}
    assert g1["Biomass cap"].status == "FAIL"
    assert g1["Harvest floor"].status == "WARN"
    assert f"{floor:,.0f}" in g1["Harvest floor"].detail
    assert floor != ctl.min_harvest_per_week


def test_moves_count_distinct_tank_pairs_not_rows():
    """The handling unit is a (source, dest) PAIR per week: two batches moved
    between the same two tanks in one week are ONE move (placement._moves_left).
    The real run's workbook never repeats a pair in a week, so the reader
    cross-check cannot tell pairs from rows; this pins the unit directly."""
    from forecast.ideal_engine import _moves_per_week
    rows = [
        {"Week": "2029-W10", "Type": "Transfer", "From_Tank": "11", "To_Tank": 31, "Count": 5000},
        {"Week": "2029-W10", "Type": "Transfer", "From_Tank": "11", "To_Tank": 31, "Count": 7000},
        {"Week": "2029-W10", "Type": "Transfer", "From_Tank": "11", "To_Tank": 33, "Count": 4000},
        {"Week": "2029-W10", "Type": "TranOG", "From_Tank": "FW", "To_Tank": 12, "Count": 280000},
        {"Week": "2029-W10", "Type": "Transfer", "From_Tank": "21", "To_Tank": 41, "Count": 0},
        {"Week": "2029-W11", "Type": "Transfer", "From_Tank": 11, "To_Tank": "31", "Count": 100},
    ]
    assert _moves_per_week(rows, "From_Tank", "To_Tank", "Count") == {
        "2029-W10": 2, "2029-W11": 1}


def _short_stream():
    from pathlib import Path
    from forecast import ideal as _im
    root = Path(__file__).resolve().parents[1]
    t = _im.load_context(root)["template"]
    return root, _im.synthetic_stream(t, 49, 280_000, horizon_weeks=60)


def test_promoted_knobs_land_in_the_run_config_and_the_runs_limits_win(tmp_path):
    """One tool: the Ideal must run what Run forecast runs, which layers the
    promoted plan's knobs onto control.yaml. The operator's per-run limits are
    the more specific statement, so they win a clash."""
    import yaml
    from pathlib import Path
    from forecast import ideal as _im
    from forecast.ideal_engine import prepare
    root, stream = _short_stream()
    prep = prepare(tmp_path / "w", stream, root, start=_im.STEADY_START,
                   horizon_weeks=60,
                   overrides={"sixn_production_start": "2026-01-01",
                              "max_biomass_kg": 4_000_000},
                   method_overrides={"chronic_pressure_weeks": 9,
                                     "max_biomass_kg": 1.0})
    doc = yaml.safe_load(open(Path(prep["config_dir"]) / "control.yaml",
                              encoding="utf-8"))
    assert doc["chronic_pressure_weeks"] == 9
    assert doc["max_biomass_kg"] == 4_000_000
    assert prep["method_overrides"] == {"chronic_pressure_weeks": 9,
                                        "max_biomass_kg": 1.0}


def test_an_unknown_promoted_knob_is_refused_before_anything_is_written(tmp_path):
    import pytest
    from forecast import ideal as _im
    from forecast.ideal_engine import prepare
    root, stream = _short_stream()
    with pytest.raises(ValueError):
        prepare(tmp_path / "w", stream, root, start=_im.STEADY_START,
                horizon_weeks=60,
                overrides={"sixn_production_start": "2026-01-01"},
                method_overrides={"no_such_knob": 1})
    assert not (tmp_path / "w").exists()


def test_as_configured_is_runnable_and_unknown_methods_are_refused():
    """Run forecast's as-configured pseudo-method (no registry pins) must be
    runnable here too, or a promoted tuned config could not be reproduced."""
    import pytest
    from forecast.ideal_engine import AS_CONFIGURED, _method
    m = _method(AS_CONFIGURED)
    assert m.key == AS_CONFIGURED and m.overrides == {} and m.engine == "controller"
    with pytest.raises(ValueError):
        _method("no-such-method")


def test_real_pr_mode_carries_the_what_if_limits(tmp_path):
    """Step 3's limits box: a cap override must reach the run's control.yaml
    on a REAL PR too. Dated per-week rows in limits.yaml still win for their
    weeks — the engine's own rule (caps.resolve_facility_cap) — and the run's
    limits.yaml is copied verbatim, so those rows are untouched."""
    import yaml
    from pathlib import Path
    from forecast import scenario_io as sio
    from forecast.ideal_engine import prepare
    root = Path(__file__).resolve().parents[1]
    pr = root / "tests" / "fixtures" / "reference" / "production_report.xlsx"
    assert pr.is_file(), pr
    batches = sio.load_batches(str(root / "scenario"))
    prep = prepare(tmp_path / "w", batches, root, horizon_weeks=60, pr_path=pr,
                   overrides={"max_biomass_kg": 4_200_000})
    doc = yaml.safe_load(open(Path(prep["config_dir"]) / "control.yaml",
                              encoding="utf-8"))
    assert doc["max_biomass_kg"] == 4_200_000
    live = (root / "scenario" / "limits.yaml").read_bytes()
    assert (Path(prep["scenario_dir"]) / "limits.yaml").read_bytes() == live
