"""Cheap screening runs: they may RANK cells, they may never JUDGE them.

MEASURED 2026-09-19, ten cells run at both horizons (104 weeks reading year 2,
156 weeks reading year 3):

    tonnage ranking    Spearman 0.988   -- the cheap run orders cells correctly
    tonnage level      ratio 0.95-1.01  -- and gets close on the number
    LEGAL / not        agreed on 4 of 10 -- and is useless as a verdict

Cells legal at full horizon read as 1-7 breach-weeks when screened: year 2 is
still start-up, and its transients breach limits that year 3 does not. The
error is not symmetric noise -- it is a start-up artefact, which is exactly why
`ideal_engine` runs 156 weeks in the first place.

So a screening stage buys about 2x per run, and the only safe use of it is
ORDER. This module makes that structural rather than a habit: a ScreenResult
has no `legal` field to read, and `finalists` returns cells to re-run, never a
verdict.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

# `ideal.HORIZON_WEEKS` is the full-fidelity horizon; this is the screening one.
# 104 weeks reads year 2 -- still start-up, which is the whole caveat above.
SCREEN_HORIZON_WEEKS = 104
# Measured agreement, kept next to the constant so nobody has to go looking.
MEASURED_RANK_CORRELATION = 0.988
MEASURED_VERDICT_AGREEMENT = 0.4


@dataclass(frozen=True)
class ScreenResult:
    """A cheap run's output. Deliberately carries NO verdict: there is no
    `legal` field, because a screening run's legality agreed with the truth on
    4 of 10 cells and a field would invite someone to read it."""
    cadence_days: int
    batch_size: int
    cap_kg: float
    hog_t: float
    horizon_weeks: int = SCREEN_HORIZON_WEEKS

    def __post_init__(self):
        if self.horizon_weeks >= 156:
            raise ValueError(
                f"horizon_weeks={self.horizon_weeks} is not a screening run; a "
                f"full-fidelity result carries a verdict and belongs in a Cell, "
                f"not a ScreenResult")


def finalists(results: Iterable[ScreenResult], k: int = 5) -> list[ScreenResult]:
    """The k best-RANKED cells, to be re-run at full fidelity.

    Ties break on cadence then size so a screen is reproducible: two cells with
    equal screened tonnage must not swap places between runs.
    """
    if k < 1:
        raise ValueError(f"k must be at least 1, got {k}")
    return sorted(results, key=lambda r: (-r.hog_t, r.cadence_days,
                                          r.batch_size, r.cap_kg))[:k]


def screening_saving(n_cells: int, screen_seconds: float = 26.0,
                     full_seconds: float = 55.0, k: int = 5) -> dict:
    """What the two-stage plan costs against running everything at full
    fidelity. Defaults are the measured per-cell times (single worker)."""
    two_stage = n_cells * screen_seconds + k * full_seconds
    all_full = n_cells * full_seconds
    return {"two_stage_s": two_stage, "all_full_s": all_full,
            "saving": 1.0 - two_stage / all_full if all_full else 0.0}


def describe() -> str:
    """One line for a report or a log, so the caveat travels with the numbers."""
    return (f"screened at {SCREEN_HORIZON_WEEKS} weeks: ranking only "
            f"(measured Spearman {MEASURED_RANK_CORRELATION:.3f}; "
            f"legal/not agreed on {MEASURED_VERDICT_AGREEMENT:.0%} of cells, "
            f"so no verdict is read from a screening run)")
