"""The Limits grid is seeded ONCE and Save treats it as the whole truth.

DEFECT (2026-09-04, and it already cost the operator 402 rows). `_edit_limits`
builds `flim_wide` / `slim_wide` from `scenario/limits.yaml` only when a session
key is MISSING. After that the grid persists in session_state for the life of
the session, while Save rebuilds the file from it:

    fl_recs  = _preserved_facility_limits(fl_cur, weeks)   # weeks OUTSIDE the grid
    fl_recs += [... for r in _records(fdf) for wk in weeks
                if r.get(wk) not in (None, "")]            # the GRID, for weeks inside

`_limit_week_cols` returns the forecast HORIZON when a PR is uploaded, so
`weeks` covers every horizon week whether or not the file has rows for it. The
preserver therefore protects only weeks outside the horizon; every week inside
it is taken from the grid, and a blank cell means DELETE.

So any row written to limits.yaml AFTER the grid was seeded -- by a config
import, by the target-band solver, by another surface, by an edit on disk -- is
silently reverted by the next Save. Observed 2026-09-04 06:31: 402 deletions,
0 additions. The 2026-W36/W37 rows survived (outside the horizon, so the
preserver caught them); every row from 2027-W01 to 2028-W15 was inside the
horizon, blank in the stale grid, and went.

The fix is a THREE-WAY merge, not a re-seed: re-seeding would throw away the
operator's unsaved edits, which is the opposite failure. The grid knows what it
was seeded with, so a cell that still matches its seed was never touched and
must defer to what is on disk NOW; a cell that differs is a deliberate edit and
wins, INCLUDING a blank, which means delete.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = pytest.importorskip("app", reason="app.py not importable without Streamlit")

W26 = "2026-W40"
W27 = "2027-W10"
BIO = "biomass"
FEED = "feed_per_day"


def test_a_row_written_after_the_grid_was_seeded_survives_a_save():
    """THE DEFECT, in the exact shape that deleted 402 rows: the grid was
    seeded when the file had 2026 rows only, the file has since gained 2027
    rows, and the operator saves without touching anything."""
    seeded = {(W26, BIO): 3_650_000.0}
    grid = {(W26, BIO): 3_650_000.0, (W27, BIO): None}   # blank: never seen
    on_disk = {(W26, BIO): 3_650_000.0, (W27, BIO): 3_650_000.0}
    covered = {(W26, BIO), (W27, BIO)}
    merged, conflicts = app._limits_grid_merge(seeded, grid, on_disk, covered)
    assert (W27, BIO) in merged, (
        "a row added to limits.yaml after the grid was seeded was deleted by a "
        "save that did not touch it")
    assert merged[(W27, BIO)] == 3_650_000.0


def test_a_deliberate_edit_still_wins():
    seeded = {(W26, BIO): 3_650_000.0}
    grid = {(W26, BIO): 3_700_000.0}
    on_disk = {(W26, BIO): 3_650_000.0}
    merged, _ = app._limits_grid_merge(seeded, grid, on_disk, {(W26, BIO)})
    assert merged[(W26, BIO)] == 3_700_000.0


def test_a_deliberately_cleared_cell_still_deletes():
    """Blank means 'use the Control default'. Clearing must remain possible, or
    the merge becomes a one-way ratchet that can never remove a cap."""
    seeded = {(W26, BIO): 3_650_000.0}
    grid = {(W26, BIO): None}
    on_disk = {(W26, BIO): 3_650_000.0}
    merged, _ = app._limits_grid_merge(seeded, grid, on_disk, {(W26, BIO)})
    assert (W26, BIO) not in merged


def test_a_cell_changed_on_disk_and_in_the_grid_is_reported():
    """Both moved. The grid wins -- the operator is looking at it -- but the
    caller must be able to say so rather than resolve it silently."""
    seeded = {(W26, BIO): 3_650_000.0}
    grid = {(W26, BIO): 3_700_000.0}
    on_disk = {(W26, BIO): 3_800_000.0}
    merged, conflicts = app._limits_grid_merge(seeded, grid, on_disk, {(W26, BIO)})
    assert merged[(W26, BIO)] == 3_700_000.0
    assert (W26, BIO) in conflicts


def test_an_untouched_grid_over_an_unchanged_file_changes_nothing():
    """NEGATIVE CONTROL. The ordinary case must be a no-op, or every save
    starts rewriting the file for no reason."""
    on_disk = {(W26, BIO): 3_650_000.0, (W26, FEED): 27_500.0}
    merged, conflicts = app._limits_grid_merge(
        dict(on_disk), dict(on_disk), dict(on_disk),
        {(W26, BIO), (W26, FEED)})
    assert merged == on_disk
    assert not conflicts


def test_keys_outside_the_grid_are_left_alone():
    """Weeks outside the horizon are the existing preserver's job. The merge
    must not second-guess them."""
    seeded = {}
    grid = {}
    on_disk = {("2026-W36", BIO): 3_650_000.0}
    merged, _ = app._limits_grid_merge(seeded, grid, on_disk, set())
    assert merged == on_disk


def test_it_does_not_mutate_its_inputs():
    seeded = {(W26, BIO): 3_650_000.0}
    grid = {(W26, BIO): None}
    on_disk = {(W26, BIO): 3_650_000.0}
    snap = (dict(seeded), dict(grid), dict(on_disk))
    app._limits_grid_merge(seeded, grid, on_disk, {(W26, BIO)})
    assert (seeded, grid, on_disk) == snap


def test_system_limit_keys_work_the_same():
    """slim_wide is keyed (week, system, metric) and has the identical hazard."""
    k_old = (W26, "OG1N", BIO)
    k_new = (W27, "OG1N", BIO)
    seeded = {k_old: 400_000.0}
    grid = {k_old: 400_000.0, k_new: None}
    on_disk = {k_old: 400_000.0, k_new: 380_000.0}
    merged, _ = app._limits_grid_merge(seeded, grid, on_disk, {k_old, k_new})
    assert merged[k_new] == 380_000.0
