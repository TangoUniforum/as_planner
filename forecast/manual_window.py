"""Manual override window — prefix executor (Phase A).

Advances the facility forward through the operator's manual weeks 1..N BEFORE
the normal pipeline runs, then hands the resulting state + shifted forecast
start to whichever pipeline plans the remainder. See the design in
[[manual_starting_events]].

Biology FIDELITY is the whole game here: the manual weeks must grow / feed /
kill fish identically to how the normal engine would, or the state diverges at
the handoff. So this module REUSES the engine's own per-week biology — the exact
daily walk Phase D runs (placement.py: the `advance_tank_one_day` loop that
records `realized_biology`). It does not re-derive any growth/mortality math.

Phase A scope: pure biology advance (no operations, no TranOG inside the
window) + the realized-biology capture the continuity audit needs. Operations
(harvest/transfer/cull/6N/FW->OG) and TranOG-in-window land in later phases.
"""
from __future__ import annotations

from datetime import timedelta

from .biology import advance_tank_one_day
from .time_grid import forecast_week_labels


def advance_facility_one_week(state, batch_by_id, tables, week_start_date,
                              week_label):
    """Advance every occupied tank 7 days of continuous biology.

    Mirrors Phase D's per-week biology block exactly: for each of the 7 days,
    apply `advance_tank_one_day` (growth + mortality, no events) to each
    occupied tank, accumulating the REALIZED biomass delta and mortality count
    per (tank, batch) — the same ground-truth the continuity audit reconciles
    against. Mutates `state` in place. Returns the realized-biology dict keyed
    by (tank_id, week_label, batch_id) -> [bio_delta_kg, mort_count].
    """
    realized: dict[tuple[int, str, str], list[float]] = {}
    day = week_start_date
    for _ in range(7):
        for tank in state.tanks_by_id.values():
            if tank.is_empty:
                continue
            b_meta = batch_by_id.get(tank.batch_id)
            if b_meta is None:
                continue
            bid = tank.batch_id
            c0 = tank.count
            b0 = tank.count * tank.avg_wt_g / 1000.0
            advance_tank_one_day(tank, b_meta, tables, day)
            rb = realized.setdefault((tank.tank_id, week_label, bid), [0.0, 0.0])
            rb[0] += (tank.count * tank.avg_wt_g / 1000.0) - b0  # realized bio delta
            rb[1] += c0 - tank.count                              # mortality count
        day = day + timedelta(days=1)
    return realized


def advance_facility_window(state, batch_by_id, tables, forecast_start,
                            n_weeks):
    """Advance the facility `n_weeks` of pure biology from `forecast_start`.

    Returns (realized_biology, new_forecast_start) where new_forecast_start is
    the date that opens week N (i.e. forecast_start + n_weeks*7 days) — the
    point the forward pipeline takes over. `realized_biology` aggregates every
    advanced (tank, week, batch) so the prefix weeks can be stitched into the
    continuity audit later (Phase B).

    Phase A: pure biology only — assumes no TranOG arrival and no operations
    inside the window (callers must keep N small enough, validated upstream).
    """
    labels = forecast_week_labels(forecast_start, n_weeks)
    realized: dict[tuple[int, str, str], list[float]] = {}
    week_start = forecast_start
    for i in range(n_weeks):
        wk_realized = advance_facility_one_week(
            state, batch_by_id, tables, week_start, labels[i])
        realized.update(wk_realized)
        week_start = week_start + timedelta(days=7)
    return realized, week_start
