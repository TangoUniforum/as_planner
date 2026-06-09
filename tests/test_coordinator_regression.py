"""Behavioral regression guard for the forecast pipeline.

Locks the model/pipeline BEHAVIOR, not specific output numbers. The exact
density-violation count / worst density / TranOG count legitimately change
with config (horizon, density target, limits, batches) — they are NOT the
thing under test. What must always hold, regardless of config, is that the
pipeline behaves correctly:

  1. COMPLETES + POPULATES — rc == 0 and the key sheets are written.
  2. MASS CONSERVATION — zero count-drift and zero biomass-drift rows in
     TankContinuityAudit: no fish/biomass created or lost unaccounted. (This
     also guarantees no TranOG arrival is silently dropped — a dropped
     arrival would break the count balance.)
  3. OUTPUT SANITY — every density is finite and >= 0 (no NaN / negative
     blow-ups); counts are non-negative.
  4. DETERMINISM — identical output regardless of PYTHONHASHSEED.

Runs the supported path (live config/ + scenario/ YAML, ProductionReport from
a COPY of Forecast.xlsm so the source is never mutated). Skips cleanly if the
workbook or config/scenario are absent.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent  # Python/
WORKBOOK = ROOT / "Forecast.xlsm"

pytestmark = pytest.mark.skipif(
    not WORKBOOK.exists(),
    reason="Forecast.xlsm not present (gitignored); regression test needs it",
)


CONFIG_DIR = ROOT / "config"
SCENARIO_DIR = ROOT / "scenario"


@pytest.fixture(scope="module")
def run_outputs(tmp_path_factory):
    """Run the SUPPORTED path and return the output workbook path.

    PR-only: the live config/ + scenario/ YAML (the single source of truth
    the app uses) plus the ProductionReport from the workbook. This is
    robust to Forecast.xlsm being a trimmed PR-only artifact — the limits +
    batches live in scenario/, not the workbook. Skips if config/scenario
    haven't been seeded (a clean checkout: run scripts/export_*_to_yaml.py).
    """
    import forecast.run as run_mod
    if not ((CONFIG_DIR / "control.yaml").exists()
            and (SCENARIO_DIR / "limits.yaml").exists()):
        pytest.skip("config/ + scenario/ not seeded "
                    "(run scripts/export_config_to_yaml.py + export_scenario_to_yaml.py)")
    tmp = tmp_path_factory.mktemp("wb") / "Forecast.xlsm"
    shutil.copy(WORKBOOK, tmp)
    rc = run_mod.main(str(tmp), config_dir=str(CONFIG_DIR),
                      scenario_dir=str(SCENARIO_DIR))
    assert rc == 0, f"pipeline exited non-zero ({rc})"
    return tmp


def _load(path):
    import openpyxl
    return openpyxl.load_workbook(path, keep_vba=True, data_only=True)


_REQUIRED_SHEETS = ["BatchLocations", "TankContinuityAudit", "HarvestPlan",
                    "TransferPlan", "BiologyProjection", "RunConfig"]


def test_run_completes_and_populates(run_outputs):
    """The run finishes and writes the key sheets with data."""
    wb = _load(run_outputs)
    for name in _REQUIRED_SHEETS:
        assert name in wb.sheetnames, f"missing output sheet {name}"
    rows = sum(1 for _ in wb["BatchLocations"].iter_rows())
    assert rows > 5, "BatchLocations has no data rows"


def test_mass_conservation(run_outputs):
    """No fish or biomass created/lost unaccounted (zero drift).

    This is the IN-FACILITY correctness invariant — independent of config.
    NOTE: zero drift does NOT prove no batch was dropped. A never-placed batch
    creates no tank-week rows, so it never touches per-tank continuity — it is
    invisible here, not unbalanced. Input-fish conservation (every stocked batch
    has a realized fate) is enforced separately by test_no_dropped_batches.
    """
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


def test_no_dropped_batches(run_outputs):
    """Input-fish conservation: no stocked batch is silently dropped.

    Closes the blind spot in test_mass_conservation. Every batch whose TranOG
    falls within the horizon must reach the realized facility; a batch the
    placement engine fails to place (no empty OG tank) vanishes from the plan
    with its full stocked population. The InputConservationAudit flags these as
    'DROPPED' with a Fish_At_Risk count, which must total zero.
    """
    wb = _load(run_outputs)
    assert "InputConservationAudit" in wb.sheetnames, "missing InputConservationAudit"
    ws = wb["InputConservationAudit"]
    dropped = []
    at_risk = 0.0
    for i, row in enumerate(ws.iter_rows(values_only=True), 1):
        if i < 5 or not row or not row[0]:
            continue
        if isinstance(row[7], str) and "DROPPED" in row[7]:
            dropped.append(row[0])
            if isinstance(row[8], (int, float)):
                at_risk += row[8]
    assert not dropped, (
        f"{len(dropped)} batch(es) dropped (never placed): {dropped} — "
        f"{at_risk:,.0f} stocked fish lost from the plan")


def test_output_sanity(run_outputs):
    """Densities are finite and non-negative; counts non-negative.

    We do NOT assert a specific violation count or worst density — those are
    config-dependent. We only guard against NaN/negative blow-ups.
    """
    wb = _load(run_outputs)
    ws = wb["BatchLocations"]
    bad_density = bad_count = 0
    for i, row in enumerate(ws.iter_rows(values_only=True), 1):
        if i < 5 or not row:
            continue
        count, density = row[5], row[8]
        if isinstance(density, (int, float)) and (density != density or density < 0):
            bad_density += 1
        if isinstance(count, (int, float)) and count < 0:
            bad_count += 1
    assert bad_density == 0, f"{bad_density} NaN/negative density rows"
    assert bad_count == 0, f"{bad_count} negative count rows"


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

    if not ((CONFIG_DIR / "control.yaml").exists()
            and (SCENARIO_DIR / "limits.yaml").exists()):
        pytest.skip("config/ + scenario/ not seeded")

    code = textwrap.dedent(
        """
        import shutil, tempfile, os, io, contextlib, openpyxl
        import forecast.run as r
        td = tempfile.gettempdir()
        t = os.path.join(td, "det%d.xlsm" % os.getpid())
        o = os.path.join(td, "deto%d.xlsm" % os.getpid())
        shutil.copy(os.environ["WB"], t)
        with contextlib.redirect_stdout(io.StringIO()):
            r.main(t, o, config_dir=os.environ["CFG"], scenario_dir=os.environ["SCN"])
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
        env = dict(os.environ, WB=str(WORKBOOK), CFG=str(CONFIG_DIR),
                   SCN=str(SCENARIO_DIR), PYTHONHASHSEED=str(seed))
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                             capture_output=True, text=True, env=env)
        assert out.returncode == 0, f"seed {seed} failed: {out.stderr[-500:]}"
        return out.stdout.strip().splitlines()[-1]

    sig0 = _run(0)
    sig1 = _run(1)
    assert sig0 == sig1, (
        f"non-deterministic across hash seeds: seed0={sig0} seed1={sig1}")
