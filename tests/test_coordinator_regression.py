"""End-to-end regression guard for the greenfield coordinator.

Locks the empirical baseline of the incremental assignment coordinator
(exit-at-1kg + per-week top-up + forward-peak staggering + even-out
with cross-scope OG1/2 → OG3-6 pass + EVT_PR_CORRECTION 2-pass
evaluator + PR-anchored FW in-flight projection; see
docs/GREENFIELD_COORDINATOR_LOCKS.md Q-COORD.A-L):

    density violations <= 209,  worst <= 150 kg/m^3,
    0 count/biomass drift,  7/7 TranOG arrivals placed.

Runs the real pipeline on a COPY of Forecast.xlsm so the source
workbook is never mutated. Skips cleanly if the workbook is absent
(it is gitignored — a clean checkout won't have it).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent  # Python/
WORKBOOK = ROOT / "Forecast.xlsm"

# Baseline thresholds. Tests guard against REGRESSION: violations/worst
# must not exceed these; drift must stay zero.
# 2026-05-28: 212 / 185 (Q-COORD.A-I locks).
# 2026-05-30: 196 / 169.5 (Q-COORD.J cross-scope OG1/2 -> OG3-6 even-out
#   relieved B46/B48-class hotspots).
# 2026-06-01 (am): 243 / 193 (Q-COORD.L first cut: EVT_PR_CORRECTION
#   always-on for hard-cap projection. Semantic-over-metric trade.)
# 2026-06-01 (pm): 196 / 169.5 (Q-COORD.L 2-pass evaluator: planner
#   action applied only when strictly net-positive. On this workbook
#   all candidates regress, so advisory-only is chosen automatically.
#   Aligns with precalc-first: act when acting is better.)
# 2026-06-01 (later): 209 / 148.4 (PR-anchored FW in-flight projection:
#   FW batches with PR records project from PR-measured state instead
#   of biology projection from input_date. Worst density dropped 169.5
#   -> 148.4 (-12%); total count up 196 -> 209 (+7%) — pressure spread
#   across more tanks. The evaluator now ACCEPTS B46's PR_CORRECTION
#   (previously all candidates regressed).
# 2026-06-04: 245 / 216.5, TranOG 6 (forecast_start now DERIVED from the
#   ProductionReport closing date, = closing + 1 day, mirroring VBA
#   DetectForecastStart; previously trusted a stale Control B3). On the
#   refreshed workbook this moved the start 2026-05-15 -> 2026-06-01, so
#   week-0 hydrates from the heavier 5/31 snapshot instead of replaying
#   ~2.3 weeks of already-elapsed biology.
# 2026-06-05: 228 / 168.3, TranOG 6 (placement determinism fix). The
#   prior 245/216.5 was only the COMMON outcome of a nondeterministic
#   engine: phase_d sorted the per-week transfer-diff batch order by
#   net-tank-change with NO tiebreak, so equal-net-change batches were
#   ordered by hash-randomized set-of-strings iteration -> the forecast
#   changed run-to-run (245/216.5 vs 228/168.3 on the same workbook,
#   PYTHONHASHSEED-dependent). Adding a batch_id tiebreak pinned it; the
#   stable result is the BETTER plan (worst 216.5 -> 168.3). Now identical
#   across all hash seeds. See placement.py phase_d_emit_events.
MAX_VIOLATIONS = 228
MAX_WORST_DENSITY = 168.3
EXPECTED_TRANOG = 6

pytestmark = pytest.mark.skipif(
    not WORKBOOK.exists(),
    reason="Forecast.xlsm not present (gitignored); regression test needs it",
)


@pytest.fixture(scope="module")
def run_outputs(tmp_path_factory):
    """Run the full pipeline once on a temp copy; return its workbook path."""
    import forecast.run as run_mod

    tmp = tmp_path_factory.mktemp("wb") / "Forecast.xlsm"
    shutil.copy(WORKBOOK, tmp)
    rc = run_mod.main(str(tmp))
    assert rc == 0, f"pipeline exited non-zero ({rc})"
    return tmp


def _load(path):
    import openpyxl
    return openpyxl.load_workbook(path, keep_vba=True, data_only=True)


def test_zero_continuity_drift(run_outputs):
    wb = _load(run_outputs)
    ws = wb["TankContinuityAudit"]
    count_drift = bio_drift = 0
    for i, row in enumerate(ws.iter_rows(values_only=True), 1):
        if i < 5 or not row:
            continue
        if row[14] == "TANK_DRIFT":
            count_drift += 1
        if row[27] == "BIO_DRIFT":
            bio_drift += 1
    assert count_drift == 0, f"{count_drift} tank count-drift rows"
    assert bio_drift == 0, f"{bio_drift} tank biomass-drift rows"


def test_density_violations_within_baseline(run_outputs):
    wb = _load(run_outputs)
    ws = wb["BatchLocations"]
    viols = []
    for i, row in enumerate(ws.iter_rows(values_only=True), 1):
        if i < 5 or not row:
            continue
        d = row[8]
        if isinstance(d, (int, float)) and d > 95:
            viols.append(d)
    worst = max(viols, default=0.0)
    assert len(viols) <= MAX_VIOLATIONS, (
        f"density violations {len(viols)} > baseline {MAX_VIOLATIONS} "
        f"(regression)")
    assert worst <= MAX_WORST_DENSITY + 0.5, (
        f"worst density {worst:.1f} > baseline {MAX_WORST_DENSITY} "
        f"(regression)")


def test_all_tranog_placed(run_outputs):
    """All FW->OG arrivals must be placed (none silently dropped)."""
    wb = _load(run_outputs)
    ws = wb["TransferPlan"]
    tranog_batches = set()
    for i, row in enumerate(ws.iter_rows(values_only=True), 1):
        if i < 5 or not row:
            continue
        if row[6] == "TranOG" and str(row[8]).lower() == "applied":
            tranog_batches.add(row[1])
    assert len(tranog_batches) >= EXPECTED_TRANOG, (
        f"only {len(tranog_batches)} batches with applied TranOG, "
        f"expected >= {EXPECTED_TRANOG}")


# ---- YAML stable-config path (Phase 1 data-path inversion) ----
# The stable config (Control + biology + facility) can be loaded from YAML
# instead of the workbook. That path must reproduce the SAME forecast as the
# Excel path — otherwise the decoupling silently changed behavior.

@pytest.fixture(scope="module")
def run_outputs_yaml(tmp_path_factory):
    """Export stable config to YAML, then run the pipeline from it."""
    import forecast.run as run_mod
    from forecast.config_io import dump_config
    from forecast.excel_io import (
        load_workbook, read_control, read_biology_tables, read_facility_config,
    )

    cfg = tmp_path_factory.mktemp("cfg")
    wb = load_workbook(WORKBOOK)
    dump_config(cfg, control=read_control(wb), tables=read_biology_tables(wb),
                facility=read_facility_config(wb))
    wb.close()

    tmp = tmp_path_factory.mktemp("wb_yaml") / "Forecast.xlsm"
    shutil.copy(WORKBOOK, tmp)
    rc = run_mod.main(str(tmp), config_dir=str(cfg))
    assert rc == 0, f"YAML-config pipeline exited non-zero ({rc})"
    return tmp


def test_yaml_config_reproduces_baseline(run_outputs_yaml):
    """YAML stable-config run must match the Excel baseline (density + drift)."""
    wb = _load(run_outputs_yaml)
    ws = wb["BatchLocations"]
    viols = []
    for i, row in enumerate(ws.iter_rows(values_only=True), 1):
        if i < 5 or not row:
            continue
        d = row[8]
        if isinstance(d, (int, float)) and d > 95:
            viols.append(d)
    assert len(viols) <= MAX_VIOLATIONS, (
        f"YAML path: {len(viols)} viols > baseline {MAX_VIOLATIONS}")
    assert max(viols, default=0.0) <= MAX_WORST_DENSITY + 0.5, (
        f"YAML path: worst {max(viols, default=0.0):.1f} > {MAX_WORST_DENSITY}")

    wa = wb["TankContinuityAudit"]
    drift = 0
    for i, row in enumerate(wa.iter_rows(values_only=True), 1):
        if i < 5 or not row:
            continue
        if row[14] == "TANK_DRIFT" or row[27] == "BIO_DRIFT":
            drift += 1
    assert drift == 0, f"YAML path: {drift} drift rows"


# ---- Full app-managed path: config + scenario YAML (Phase 2) ----
# With both stable config AND the scenario (batches + limits) loaded from
# YAML, the workbook is read only for the ProductionReport. This full path
# must still reproduce the Excel baseline.

@pytest.fixture(scope="module")
def run_outputs_full_yaml(tmp_path_factory):
    """Export config + scenario to YAML, run from both."""
    import forecast.run as run_mod
    from forecast.config_io import dump_config
    from forecast.scenario_io import dump_scenario
    from forecast.caps import read_facility_limits, read_system_limits
    from forecast.production_report import read_production_report
    from forecast.excel_io import (
        load_workbook, read_control, read_biology_tables, read_facility_config,
        read_batches,
    )
    from datetime import datetime as _dt, timedelta as _td

    cfg = tmp_path_factory.mktemp("cfg2")
    scn = tmp_path_factory.mktemp("scn")
    wb = load_workbook(WORKBOOK)
    dump_config(cfg, control=read_control(wb), tables=read_biology_tables(wb),
                facility=read_facility_config(wb))
    pr_closing, _og, _fw = read_production_report(wb)
    fs = _dt(pr_closing.year, pr_closing.month, pr_closing.day) + _td(days=1)
    dump_scenario(scn, batches=read_batches(wb),
                  facility_limits=read_facility_limits(wb, fs.date()),
                  system_limits=read_system_limits(wb, fs.date()))
    wb.close()

    tmp = tmp_path_factory.mktemp("wb_full") / "Forecast.xlsm"
    shutil.copy(WORKBOOK, tmp)
    rc = run_mod.main(str(tmp), config_dir=str(cfg), scenario_dir=str(scn))
    assert rc == 0, f"full-YAML pipeline exited non-zero ({rc})"
    return tmp


def test_full_yaml_path_reproduces_baseline(run_outputs_full_yaml):
    """Config + scenario YAML (PR-only workbook read) must match the baseline."""
    wb = _load(run_outputs_full_yaml)
    ws = wb["BatchLocations"]
    viols = []
    for i, row in enumerate(ws.iter_rows(values_only=True), 1):
        if i < 5 or not row:
            continue
        d = row[8]
        if isinstance(d, (int, float)) and d > 95:
            viols.append(d)
    assert len(viols) <= MAX_VIOLATIONS, (
        f"full-YAML: {len(viols)} viols > baseline {MAX_VIOLATIONS}")
    assert max(viols, default=0.0) <= MAX_WORST_DENSITY + 0.5, (
        f"full-YAML: worst {max(viols, default=0.0):.1f} > {MAX_WORST_DENSITY}")

    wa = wb["TankContinuityAudit"]
    drift = sum(
        1 for i, row in enumerate(wa.iter_rows(values_only=True), 1)
        if i >= 5 and row and (row[14] == "TANK_DRIFT" or row[27] == "BIO_DRIFT")
    )
    assert drift == 0, f"full-YAML: {drift} drift rows"


# ---- Determinism guard (2026-06-05) ----
# The forecast must be identical regardless of PYTHONHASHSEED. A
# set-of-strings iteration in phase_d without a deterministic tiebreak
# made it vary run-to-run (245/216.5 vs 228/168.3 on the same workbook).
# Runs the pipeline in two subprocesses with different hash seeds and
# asserts the BatchLocations density signature matches.

def test_engine_deterministic_across_hash_seeds():
    import os
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent(
        """
        import shutil, tempfile, os, io, contextlib, openpyxl
        import forecast.run as r
        td = tempfile.gettempdir()
        t = os.path.join(td, "det%d.xlsm" % os.getpid())
        o = os.path.join(td, "deto%d.xlsm" % os.getpid())
        shutil.copy(os.environ["WB"], t)
        with contextlib.redirect_stdout(io.StringIO()):
            r.main(t, o)
        wb = openpyxl.load_workbook(o, data_only=True)
        ws = wb["BatchLocations"]
        v = []
        for i, row in enumerate(ws.iter_rows(values_only=True), 1):
            if i < 5 or not row:
                continue
            d = row[8]
            if isinstance(d, (int, float)) and d > 95:
                v.append(round(d, 2))
        print("%d|%.2f|%.2f" % (len(v), max(v, default=0.0), round(sum(v), 2)))
        """
    )

    def _run(seed):
        env = dict(os.environ, WB=str(WORKBOOK), PYTHONHASHSEED=str(seed))
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                             capture_output=True, text=True, env=env)
        assert out.returncode == 0, f"seed {seed} failed: {out.stderr[-500:]}"
        return out.stdout.strip().splitlines()[-1]

    sig0 = _run(0)
    sig1 = _run(1)
    assert sig0 == sig1, (
        f"non-deterministic across hash seeds: seed0={sig0} seed1={sig1}")
