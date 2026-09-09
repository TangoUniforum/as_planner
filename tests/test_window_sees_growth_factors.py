"""The manual override window must see the operator's per-week growth factors.

THE DEFECT (found 2026-09-08). `run.py` bound `tables.og_sgr_by_week` about
ninety lines BELOW the manual-override-window block. The window walks seven
days of real biology per week through `biology.advance_tank_one_day`, which
reads that dict — so it ran with an EMPTY one and `og_sgr_factor` returned 1.0
for every one of its days.

The operator had configured exactly those weeks in scenario/limits.yaml:

    - week: 2026-W36   metric: sgr_correction_og   value: 0.90
    - week: 2026-W37   metric: sgr_correction_og   value: 0.92

and every day the run ever simulates with an ISO label of W36 or W37 lies
inside the window. So both settings were **100% inert**: forcing them to 0.10 —
a ninefold cut — produced a byte-identical workbook across all 2,832
BatchLocations rows. A knob that cannot move the answer is worse than a wrong
default, because the operator reasonably believes it is working.

Cost on the live plan: the window grew ~8.5% too fast and handed the planner a
facility 29,541 kg (0.78%) too heavy at the handoff week. Small today, but the
error is whatever the operator dials in — the run at 0.10 and the run at 1.00
were the same file.

FIXED by hoisting the limits load and the factor binding above the window.
Safe: `load_limits` reads only `sixn_growth` and `sixn_production_start` from
control, neither of which the window changes, and `facility_limits` comes
straight from the file.

Guarded here by SOURCE ORDER, which is the property that actually broke. A
behavioural test would need a full windowed run; this catches the regression at
import time and names the reason.
"""
from __future__ import annotations

import re
from pathlib import Path

_RUN = Path(__file__).resolve().parent.parent / "forecast" / "run.py"
_SRC = _RUN.read_text(encoding="utf-8")


def _line_of(pattern: str) -> int:
    m = re.search(pattern, _SRC, re.MULTILINE)
    assert m, f"pattern not found in run.py — update this guard: {pattern}"
    return _SRC[:m.start()].count("\n") + 1


def test_the_growth_factor_is_bound_before_the_window_runs():
    bound = _line_of(r"tables\.og_sgr_by_week\s*=")
    window = _line_of(r"^    if window_n > 0:")
    assert bound < window, (
        f"tables.og_sgr_by_week is bound at line {bound}, AFTER the manual "
        f"override window at line {window}. The window walks real biology and "
        f"reads that dict, so it would run with factor 1.0 and the operator's "
        f"per-week sgr_correction_og for the window weeks would be inert.")


def test_the_limits_it_depends_on_are_loaded_before_the_window():
    loaded = _line_of(r"facility_limits, system_limits = load_limits\(")
    window = _line_of(r"^    if window_n > 0:")
    assert loaded < window, (
        f"load_limits runs at line {loaded}, after the window at {window} — "
        f"the factor binding depends on it and must precede the window too.")


def test_the_factor_is_bound_exactly_once():
    """Two bindings would let the window and the planner disagree."""
    n = len(re.findall(r"tables\.og_sgr_by_week\s*=", _SRC))
    assert n == 1, f"{n} bindings of tables.og_sgr_by_week; expected exactly 1"


def test_limits_are_loaded_exactly_once():
    n = len(re.findall(r"facility_limits, system_limits = load_limits\(", _SRC))
    assert n == 1, f"{n} load_limits calls; expected exactly 1"


def test_the_factor_reaches_the_biology_layer():
    """The dict is only useful if the growth path actually reads it."""
    from forecast import biology
    src = Path(biology.__file__).read_text(encoding="utf-8")
    assert "og_sgr_by_week" in src, (
        "biology no longer reads og_sgr_by_week — the binding above would be "
        "dead in a different way")
