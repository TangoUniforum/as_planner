"""Applying a solved target band must not delete limits it never saw.

DEFECT (2026-09-04). The "Apply these bands to Limits" button promises, in its
own help text: "Writes ONLY the capped weeks into scenario/limits.yaml, merging
with any bands you set by hand. Nothing else in the file is touched."

What it did (app.py, _target_solver_panel):

    _fl, _sl = _ll(SCENARIO_DIR, _ctl)      # read the CURRENT file
    _fl.overrides = dict(b["overrides"])    # ...then throw it away

`b["overrides"]` is `tools.solve_targets`' full override map SNAPSHOTTED AT
SOLVE TIME plus the caps it proposed (solve_targets.py:163/168/199). So the
assignment preserves everything that existed when you solved and silently
DESTROYS anything added between the solve and the apply -- from this editor or
any other surface. The disk read two lines above is dead code; its only
surviving use is the row count in the success message.

`solve()` also returns `base_overrides`, which is what makes the fix small: the
solve's OWN changes are (solved - base), and anything on disk that differs from
`base` is an edit the solve never saw.

The negative control matters as much as the defect: when the file has not moved
since the solve, the result must be exactly what the old code wrote, or this
becomes a behaviour change wearing a bug fix's clothes.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = pytest.importorskip("app", reason="app.py not importable without Streamlit")

A = ("2027-W01", "biomass")
B = ("2027-W02", "max_harvest_per_week")
C = ("2027-W03", "feed_per_day")


def test_an_override_added_after_the_solve_survives():
    """THE DEFECT. Solve, then set an unrelated band by hand, then apply."""
    base = {A: 3_650_000.0}
    solved = {A: 3_650_000.0, B: 40_000.0}        # the solver capped one week
    on_disk = {A: 3_650_000.0, C: 27_500.0}       # ...and you added one after
    merged, added, removed, stale = app._solver_apply_plan(on_disk, base, solved)
    assert C in merged, (
        "the hand-set band was deleted — the button's own help text promises "
        "'nothing else in the file is touched'")
    assert merged[C] == 27_500.0
    assert B in merged and merged[B] == 40_000.0, "the solver's cap must land"
    assert added == {B: 40_000.0}
    assert removed == set()
    assert stale == set()


def test_a_week_the_solver_dropped_is_dropped():
    """A merge that only ever ADDS would pin a cap the solve decided to lift."""
    base = {A: 3_650_000.0, B: 40_000.0}
    solved = {A: 3_650_000.0}                     # solver lifted B's cap
    merged, added, removed, stale = app._solver_apply_plan(dict(base), base, solved)
    assert B not in merged, "the lifted cap is still in the file"
    assert removed == {B}


def test_an_edit_to_a_week_the_solve_saw_is_reported_stale():
    """The solve reasoned about A=3.65M. The file now says something else, so
    the band is advice about a facility that no longer exists."""
    base = {A: 3_650_000.0}
    solved = {A: 3_650_000.0, B: 40_000.0}
    on_disk = {A: 3_800_000.0}                    # changed since the solve
    merged, added, removed, stale = app._solver_apply_plan(on_disk, base, solved)
    assert stale == {A}, "a changed input week was not reported"


def test_an_unmoved_file_applies_exactly_as_before():
    """NEGATIVE CONTROL. Nothing edited since the solve -> the result must equal
    what the old wholesale assignment produced, or this is a silent behaviour
    change rather than a fix."""
    base = {A: 3_650_000.0}
    solved = {A: 3_650_000.0, B: 40_000.0}
    merged, added, removed, stale = app._solver_apply_plan(dict(base), base, solved)
    assert merged == solved, "the ordinary path changed"
    assert stale == set()


def test_the_helper_does_not_mutate_its_inputs():
    """It is called to DECIDE; the caller writes. A helper that edits the live
    FacilityLimits in place would apply a refused plan anyway."""
    base = {A: 3_650_000.0}
    solved = {A: 3_650_000.0, B: 40_000.0}
    on_disk = {A: 3_650_000.0, C: 27_500.0}
    snap = (dict(base), dict(solved), dict(on_disk))
    app._solver_apply_plan(on_disk, base, solved)
    assert (base, solved, on_disk) == snap
