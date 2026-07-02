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
from .placement import BatchLocationRow
from .time_grid import forecast_week_labels


def _snapshot_week(state, week_label, week_start):
    """End-of-week per-tank BatchLocationRows for every occupied tank — the same
    shape Phase D emits (placement.py), so the prefix weeks stitch seamlessly
    into the BatchLocations output + the (event-stream-driven) continuity audit.
    """
    rows = []
    for tank in state.tanks_by_id.values():
        if tank.is_empty:
            continue
        rows.append(BatchLocationRow(
            week_label=week_label, week_start=week_start,
            batch_id=tank.batch_id, tank_id=tank.tank_id,
            location_id=tank.location_id, system_id=tank.system_id,
            count=tank.count, avg_wt_g=tank.avg_wt_g,
            biomass_kg=tank.biomass_kg, density_kg_m3=tank.density_kg_m3,
            stage=tank.stage,
        ))
    return rows


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


def _build_fw_lookup(events, fw_records, control, pr_closing, tables, batch_by_id):
    """Index (batch_id, week_label) -> (count, avg_wt_g, cv) for every FW batch
    referenced by an fw_to_og event, by projecting its in-flight FW trajectory.
    Only FW-stage (pre-TranOG) weeks are indexed — a manual FW->OG must happen
    while the batch is still in freshwater.
    """
    from collections import defaultdict
    from .manual_events import TYPE_FW_TO_OG
    fw_batches = {ev.batch for ev in (events or [])
                  if ev.type == TYPE_FW_TO_OG and ev.batch}
    if not fw_batches or not fw_records or control is None:
        return {}
    from .biology import project_in_flight_fw_batch
    agg = defaultdict(lambda: {"count": 0.0, "biomass_kg": 0.0})
    for r in fw_records:
        if r.batch_id in fw_batches:
            agg[r.batch_id]["count"] += r.closing_count
            agg[r.batch_id]["biomass_kg"] += r.closing_biomass_kg
    lookup: dict = {}
    for bid in fw_batches:
        a = agg.get(bid)
        b_meta = batch_by_id.get(bid)
        if not a or a["count"] <= 0 or b_meta is None:
            continue
        avg_wt = a["biomass_kg"] * 1000.0 / a["count"]
        states, _, _ = project_in_flight_fw_batch(
            b_meta, tables, control, a["count"], avg_wt, pr_closing)
        cv = b_meta.tran_og_cv or 16.0
        for s in states:
            if s.stage == "FW":
                lookup[(bid, s.week_label)] = (s.close_count, s.close_avg_weight_g, cv)
    return lookup


def advance_facility_window(state, batch_by_id, tables, forecast_start,
                            n_weeks, events=None, control=None,
                            pr_closing=None, fw_records=None):
    """Run the manual override window: `n_weeks` of (operator events + biology).

    For each week k in 1..N: apply the operator's events scheduled for week k
    (transfers / harvests / 6N-moves / FW->OG, recorded), advance 7 days of
    biology, snapshot BatchLocations. Returns a dict with realized_biology,
    batch_locations, transfer_events, harvest_events, tranog_events,
    transferred_fw_batches (FW batches manually moved to OG — the caller must
    exclude them from the auto FW supply), warnings, and new_start (the date
    that opens week N+1). All of it stitches into the output so the window weeks
    are visible + audited.
    """
    from .manual_events import apply_events_for_week
    handling_frac = ((control.handling_mortality_pct / 100.0)
                     if control is not None else 0.0)
    fw_lookup = _build_fw_lookup(
        events, fw_records, control, pr_closing, tables, batch_by_id)
    labels = forecast_week_labels(forecast_start, n_weeks)
    realized: dict[tuple[int, str, str], list[float]] = {}
    batch_locations: list = []
    opening_locations: list = []   # start-of-week (post-event, PRE-biology) snapshot
    transfer_events: list = []
    harvest_events: list = []
    tranog_events: list = []
    warnings: list[str] = []
    manual_fw_balance: dict[str, list[float]] = {}
    week_start = forecast_start
    for i in range(n_weeks):
        # OPENING snapshot FIRST — the true start-of-week state, taken BEFORE
        # this week's operations AND before its growth/mortality. So week i's
        # grid column shows what the operator has to act ON when the week opens:
        # for week 1 that's the raw PR-hydrated facility; for later weeks it's
        # the prior week's close. A harvest/move scripted in week i therefore
        # KEEPS showing the fish in week i (you still see what you acted on) and
        # only empties the tank from week i+1 onward. (batch_locations below
        # stays the end-of-week/closing snapshot the run stitches into the
        # output + audits — that ordering is unchanged.)
        opening_locations.extend(_snapshot_week(state, labels[i], week_start))
        # Operations next — they still date into THIS week for the continuity
        # audit — then biology, then the closing snapshot.
        if events:
            tr, hv, tn, w, fwb = apply_events_for_week(
                state, events, i + 1, week_start, week_label=labels[i],
                handling_frac=handling_frac, fw_lookup=fw_lookup)
            transfer_events.extend(tr)
            harvest_events.extend(hv)
            tranog_events.extend(tn)
            warnings.extend(w)
            for b, (cnt, culled) in fwb.items():
                rec = manual_fw_balance.setdefault(b, [0.0, 0.0])
                rec[0] += cnt
                rec[1] += culled
        wk_realized = advance_facility_one_week(
            state, batch_by_id, tables, week_start, labels[i])
        realized.update(wk_realized)
        batch_locations.extend(_snapshot_week(state, labels[i], week_start))
        week_start = week_start + timedelta(days=7)
    return {
        "realized_biology": realized,
        "batch_locations": batch_locations,
        "opening_locations": opening_locations,
        "transfer_events": transfer_events,
        "harvest_events": harvest_events,
        "tranog_events": tranog_events,
        "transferred_fw_batches": {e.batch_id for e in tranog_events},
        "manual_fw_balance": manual_fw_balance,
        "warnings": warnings,
        "new_start": week_start,
    }
