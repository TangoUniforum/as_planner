"""THE NUMERIC BASELINE.

Runs the pipeline on the committed reference fixture and compares every pinned
number against `tests/fixtures/reference/golden.json`.

WHAT THIS CATCHES THAT THE REST OF THE SUITE DOES NOT
    The other tests prove the machinery works — events apply, gates fire,
    conservation holds structurally. None of them proves the ANSWERS are right.
    Demonstrated 2026-08-20: inverting the harvest scheduler's batch selection
    from FIFO to LIFO passed all 762 tests while the forecast moved 0.92% on
    fish and worst tank density went 100 -> 134 kg/m3. This test is what turns
    that into a failure.

WHEN IT FAILS
    Read the diff it prints — it names every metric that moved and by how much.
    Then decide. If the move is intended and understood, re-freeze with
    `python tests/fixtures/freeze_golden.py` IN THE SAME COMMIT as the change,
    and say in the commit message which numbers moved and why.
    NEVER re-freeze to make the test green. If you cannot explain a line of the
    diff, that line is the bug.

TOLERANCES (agreed with the operator 2026-08-20)
    counts and compliance   EXACT
    tonnage                 0.1%
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fixtures.freeze_golden import GOLDEN, REF, run_reference
from tests.fixtures.golden import compare, extract


@pytest.fixture(scope="module")
def actual(tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("reference_baseline")
    produced = run_reference(out_dir)
    return extract(produced)


def test_fixture_is_present():
    """The fixture must survive a clean clone — that is the whole point of it
    being 6 kB of synthetic data rather than a gitignored production workbook."""
    assert (REF / "production_report.xlsx").exists(), "reference PR missing"
    assert (REF / "config" / "control.yaml").exists(), "reference config missing"
    assert (REF / "scenario" / "batches.yaml").exists(), "reference scenario missing"
    assert GOLDEN.exists(), (
        "golden.json missing — run `python tests/fixtures/freeze_golden.py`")


def test_reference_run_matches_golden(actual):
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    diffs = compare(golden, actual)
    if diffs:
        head = "\n".join(f"  {d}" for d in diffs[:40])
        more = f"\n  ... and {len(diffs) - 40} more" if len(diffs) > 40 else ""
        pytest.fail(
            f"The reference forecast changed — {len(diffs)} metric(s) moved.\n"
            f"{head}{more}\n\n"
            f"If this change is intended, re-freeze in the SAME commit:\n"
            f"    python tests/fixtures/freeze_golden.py\n"
            f"and say in the commit message which numbers moved and why.")


def test_reference_plan_is_not_degenerate(actual):
    """Guard the BASELINE ITSELF against quietly becoming worthless.

    A baseline whose plan harvests almost nothing still passes an equality
    check while detecting nothing, because most pinned metrics are zero or
    constant. That is not hypothetical: the first draft of this fixture
    stocked ONE 6N pair and produced 2 harvest events in 26 weeks, because the
    depuration rotation cannot bootstrap from a single stocked pair — every
    drain lands exactly 7 days after its fill and trips the 8-day guard, so
    the pair is held forever.

    These bounds are deliberately loose. They are not a plan-quality
    assertion; they exist so that a fixture or engine change which collapses
    the plan fails HERE, naming the reason, instead of silently turning the
    baseline into a constant.
    """
    t = actual["totals"]
    assert t["harvest_events"] >= 20, (
        f"only {t['harvest_events']} harvest events over the horizon — the "
        f"reference plan has collapsed and the baseline no longer detects "
        f"anything. Check the 6N rotation (see the docstring).")
    assert t["fish"] > 500_000, f"only {t['fish']:,.0f} fish harvested"
    assert len(actual["weekly_fish"]) >= 15, (
        f"only {len(actual['weekly_fish'])} weeks harvested — too many empty "
        f"weeks for the baseline to be meaningful")
