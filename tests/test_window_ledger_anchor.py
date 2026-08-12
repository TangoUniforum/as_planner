"""The batch ledger and the tank ledger must open the forecast from the SAME
PR-hydrated state — including under a manual override window.

THE DEFECT THIS PINS. A manual override window advances the FacilityState in
place to week N+1 (that is the state the planner plans from). `run.py` keeps a
pre-window deepcopy, `audit_initial_state`, precisely so the audits can anchor
the window's own weeks to the PR. TankContinuityAudit got that anchor;
write_reconciliation_report was still handed the live, already-advanced
`state`. So the batch ledger opened its FIRST row — the first window week —
from a state N weeks in the FUTURE, and every batch read as drift on that one
row: the count off by the window's mortality + harvest, the biomass off by the
window's growth. Nothing had actually moved; the per-tank event-stream audit
showed zero count drift over the same weeks.

A ledger that cries drift where there is none is worse than no ledger: it
trains the reader to scroll past a real alarm. These tests are the negative
control — on the parent commit `test_first_week_opening_agrees_across_ledgers`
fails with a per-batch opening gap equal to exactly the window's losses.

Needs the gitignored Forecast.xlsm + a seeded config/scenario (real biology),
so it skips cleanly when they are absent — it runs in the main checkout where
the data lives.
"""
from __future__ import annotations

import shutil
from collections import defaultdict
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent  # Python/
WORKBOOK = ROOT / "Forecast.xlsm"
CONFIG_DIR = ROOT / "config"
SCENARIO_DIR = ROOT / "scenario"

# Two pure-biology window weeks: enough that a wrong anchor is off by two
# weeks of mortality + growth, and no scripted events needed — the anchor bug
# fires on window length alone.
WINDOW_WEEKS = 2
HORIZON_WEEKS = 14          # short: the defect is at the FIRST week

pytestmark = pytest.mark.skipif(
    not (WORKBOOK.exists()
         and (CONFIG_DIR / "control.yaml").exists()
         and (SCENARIO_DIR / "limits.yaml").exists()),
    reason="needs Forecast.xlsm + seeded config/scenario (gitignored)",
)


@pytest.fixture(scope="module")
def windowed_run(tmp_path_factory):
    """Run the real pipeline with a manual override window, in an isolated
    copy of config/scenario so nothing touches the caller's dirs."""
    import contextlib
    import io

    import forecast.run as run_mod

    work = tmp_path_factory.mktemp("window_ledger")
    cdir = work / "config"
    sdir = work / "scenario"
    shutil.copytree(CONFIG_DIR, cdir)
    # No scripted events: the window is pure biology, so this test pins the
    # ANCHOR and nothing else. The per-PR `manual_events/` dir is left out of
    # the copy entirely (deleting it afterwards trips OneDrive on Windows).
    shutil.copytree(SCENARIO_DIR, sdir,
                    ignore=shutil.ignore_patterns("manual_events"))

    cy = cdir / "control.yaml"
    cfg = yaml.safe_load(cy.read_text())
    cfg["horizon_weeks"] = HORIZON_WEEKS
    cy.write_text(yaml.safe_dump(cfg, sort_keys=False))

    (sdir / "manual_events.yaml").write_text("events: []\n")

    inp = work / "Forecast.xlsm"
    out = work / "out.xlsm"
    shutil.copy(WORKBOOK, inp)
    with contextlib.redirect_stdout(io.StringIO()):
        rc = run_mod.main(str(inp), str(out), config_dir=str(cdir),
                          scenario_dir=str(sdir), advance_weeks=WINDOW_WEEKS)
    assert rc == 0, f"pipeline exited non-zero ({rc})"
    return out


def _rows(wb, sheet):
    """Data rows of an audit sheet (title + blurb + blank + header = 4)."""
    rows = list(wb[sheet].iter_rows(values_only=True))[4:]
    return [r for r in rows if r and r[0]]


@pytest.fixture(scope="module")
def ledgers(windowed_run):
    import openpyxl
    wb = openpyxl.load_workbook(windowed_run, keep_vba=True, data_only=True)
    return _rows(wb, "ReconciliationReport"), _rows(wb, "TankContinuityAudit")


def test_first_week_opening_agrees_across_ledgers(ledgers):
    """NEGATIVE CONTROL. Per batch, the batch ledger's Open_Count for the
    first window week must equal the tank ledger's — both are 'the PR'.

    On the parent commit the batch ledger opened from the post-window state,
    so every batch is short by its window mortality (and by the window's
    harvest for any batch that was harvested)."""
    recon, tank = ledgers
    week = recon[0][0]

    recon_open = {r[1]: (r[2] or 0.0) for r in recon if r[0] == week}
    tank_open: dict[str, float] = defaultdict(float)
    for r in tank:
        if r[0] == week:
            tank_open[r[2]] += r[3] or 0.0

    assert recon_open, "ReconciliationReport has no first-week rows"
    assert tank_open, "TankContinuityAudit has no first-week rows"

    mismatched = {
        b: (recon_open.get(b, 0.0), tank_open.get(b, 0.0))
        for b in set(recon_open) | set(tank_open)
        if abs(recon_open.get(b, 0.0) - tank_open.get(b, 0.0)) > 1.0
    }
    assert not mismatched, (
        f"{week}: batch ledger and tank ledger disagree on the opening "
        f"balance (batch, tank): {mismatched}. The two audits must anchor to "
        f"the same PR-hydrated state."
    )


def test_window_first_week_shows_no_phantom_drift(ledgers):
    """NEGATIVE CONTROL. The window's first week is ordinary biology on an
    untouched facility — the batch ledger must balance there, not flag."""
    recon, _ = ledgers
    week = recon[0][0]
    flagged = [r for r in recon if r[0] == week and r[19]]
    assert not flagged, (
        f"{week} (first manual-window week) flagged {len(flagged)} batch(es) "
        f"with no fish actually moving: {[(r[1], r[9], r[19]) for r in flagged]}"
    )


def test_no_count_drift_anywhere_in_the_batch_ledger(ledgers):
    """Across the whole windowed run, no batch-week may lose or gain fish
    without an event to explain it."""
    recon, _ = ledgers
    drift = [(r[0], r[1], r[9]) for r in recon if r[19] == "COUNT_DRIFT"]
    assert not drift, f"unexplained batch count drift: {drift}"


def test_tank_ledger_has_no_count_drift(ledgers):
    """The authoritative per-tank event-stream audit stays clean — the fix is
    a reporting-anchor fix and must not perturb it."""
    _, tank = ledgers
    drift = [(r[0], r[1], r[2], r[13]) for r in tank if r[14] == "TANK_DRIFT"]
    assert not drift, f"unexplained per-tank count drift: {drift}"
