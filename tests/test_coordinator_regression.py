"""End-to-end regression guard for the greenfield coordinator.

Locks the empirical baseline of the incremental assignment coordinator
(exit-at-1kg + per-week top-up + forward-peak staggering + even-out
with cross-scope OG1/2 → OG3-6 pass + EVT_PR_CORRECTION worst-first
1-tank claim; see docs/GREENFIELD_COORDINATOR_LOCKS.md Q-COORD.A-L):

    density violations <= 243,  worst <= 195 kg/m^3,
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
# 2026-06-01: 243 / 193.0 (Q-COORD.L ACCEPTED: EVT_PR_CORRECTION emits
#   the recommended PR split as a concrete W1 action. Semantic
#   correctness over simulation metric — see lock record for the
#   accepted trade-off rationale).
MAX_VIOLATIONS = 243
MAX_WORST_DENSITY = 195.0
EXPECTED_TRANOG = 7

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
