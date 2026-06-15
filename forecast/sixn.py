"""6N twin-mode helpers (DESIGN §5).

Two regimes governed by Control R26 `6N Production Start Date` plus
Control "6N growth" flag:

- **Purge mode**: 6N is a depuration system. Sister pairs 61/67,
  63/69, 65/71 hold fish for a rolling 2-week purge before harvest.
  No feed, no biomass cap, harvest from 6N only via round-robin.
- **Production mode**: 6N is a normal production system. Tanks
  67/69/71 are unavailable; standard system caps apply; harvest
  direct from 3/4/5/6 after in-place starvation (Control R30
  `Starvation period (days)`).

This module exposes the static structure (pairs, main vs sister
tanks) and the mode-resolution rule. Round-robin sequencing,
move-in batch selection, and starvation-cycle scheduling are
consumed by `placement.py` in a follow-up cut.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from .models import ControlParams
from .state import FacilityState


# Sister pairs (main_tank_id, sister_tank_id). Single-batch harvest
# prefers the main tank (61/63/65); sister (67/69/71) is used only
# when both tanks of a pair are needed (two batches harvested same week).
SIXN_PAIRS: list[tuple[int, int]] = [
    (61, 67),
    (63, 69),
    (65, 71),
]

SIXN_MAIN_TANKS = frozenset(p[0] for p in SIXN_PAIRS)
SIXN_SISTER_TANKS = frozenset(p[1] for p in SIXN_PAIRS)
SIXN_ALL_TANKS = SIXN_MAIN_TANKS | SIXN_SISTER_TANKS


def _as_date(d):
    if isinstance(d, datetime):
        return d.date()
    return d


def is_purge_mode(control: ControlParams, today) -> bool:
    """True if 6N is operating in purge (depuration) mode on `today`.

    Resolution:
      - `sixn_growth=True` -> production mode immediately (False).
      - `sixn_growth=False`, no `sixn_production_start` -> purge.
      - `sixn_growth=False`, with `sixn_production_start`:
          purge while today < production_start; production thereafter.
    """
    if control.sixn_growth:
        return False
    if control.sixn_production_start is None:
        return True
    psd = _as_date(control.sixn_production_start)
    return _as_date(today) < psd


def in_transition_window(control: ControlParams, today) -> bool:
    """True if `today` falls in the empty-6N window between purge end
    and production start (R27 `6N Transition Window (weeks)`)."""
    if control.sixn_growth:
        return False
    if control.sixn_production_start is None:
        return False
    psd = _as_date(control.sixn_production_start)
    weeks = control.sixn_transition_weeks or 0
    if weeks <= 0:
        return False
    from datetime import timedelta
    start_of_window = psd - timedelta(weeks=weeks)
    return start_of_window <= _as_date(today) < psd


def pair_combined_count(state: FacilityState, pair: tuple[int, int]) -> float:
    """Sum of fish across both tanks of a 6N pair (empty tanks contribute 0)."""
    total = 0.0
    for tid in pair:
        t = state.tanks_by_id.get(tid)
        if t is not None and not t.is_empty:
            total += t.count
    return total


def initial_purge_pair_queue(state: FacilityState) -> list[tuple[int, int]]:
    """Forecast-startup ordering of stocked 6N pairs for the purge pipeline.

    FIXED cyclic harvest order 61 -> 63 -> 65 (the SIXN_PAIRS sequence), entered
    just AFTER the empty (resting/fallow) pair — the empty slot marks where the
    rotation sits, so no PR purge-age is needed. Examples (stocked -> harvest order):
      61,63 (65 empty) -> 61, 63   (next after 65 is 61)
      63,65 (61 empty) -> 63, 65   (next after 61 is 63)
      65,61 (63 empty) -> 65, 61   (next after 63 is 65)
    If every pair is stocked (no fallow slot), start at the first pair (61). Only
    stocked pairs (>=1 fish) are included; an empty pair joins when a move-in
    restocks it.
    """
    n = len(SIXN_PAIRS)
    stocked_flags = [pair_combined_count(state, p) > 0 for p in SIXN_PAIRS]
    if not any(stocked_flags):
        return []
    if all(stocked_flags):
        return list(SIXN_PAIRS)                 # no fallow slot -> fixed order from 61
    empty_idx = next(i for i, ok in enumerate(stocked_flags) if not ok)
    order = []
    for k in range(1, n + 1):                   # walk the cycle starting after the empty
        idx = (empty_idx + k) % n
        if stocked_flags[idx]:
            order.append(SIXN_PAIRS[idx])
    return order


# Kept for back-compat where a single initial pair is wanted.
def pick_initial_purge_pair(state: FacilityState) -> Optional[tuple[int, int]]:
    """First pair in the initial purge queue (lowest non-zero count)."""
    q = initial_purge_pair_queue(state)
    return q[0] if q else None


