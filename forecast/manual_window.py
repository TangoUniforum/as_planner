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
from .state import STAGE_STARVE
from .time_grid import forecast_week_labels


def _freeze_purge_6n(state, control, week_date) -> dict:
    """Depuration hold: in PURGE mode, freeze every occupied 6N tank to STARVE
    at the start of an override week, so the window honors the engine's 6N
    depuration rules — no growth, no feed (only mortality still applies).

    Why the window needs this: the shipped pipeline hydrates every OG-type tank
    (6N included) as a growing SW stage and relies on its purge ROTATION to
    harvest the 6N tanks out within a week or two, so their SW growth never
    accumulates. The manual window runs no rotation (operator events only), so
    without this hold a PR-hydrated 6N tank would grow like a grow-out tank for
    the whole window (the reported bug).

    Gated per week-DATE on `is_purge_mode`, so a 6N->production crossing that
    lands mid-window correctly stops freezing from that week on (weeks on/after
    the production-start date grow normally). Only ADDS the freeze — an operator
    og_to_6n / graded->6N destination is already STARVE and stays so; a tank is
    never un-frozen here.

    Returns {tank_id: (prev_stage, batch_id)} for tanks NEWLY frozen this call,
    so the caller can (a) surface them for traceability and (b) RESTORE their
    pre-freeze stage at the window->planner handoff — the hold is a manual-window
    concern only and must not carry the frozen stage downstream (the auto
    pipeline runs its own 6N rotation from a clean starting point).
    """
    if control is None:
        return {}
    from .sixn import SIXN_ALL_TANKS, is_purge_mode
    if not is_purge_mode(control, week_date):
        return {}
    frozen: dict = {}
    for tid in sorted(SIXN_ALL_TANKS):
        t = state.tanks_by_id.get(tid)
        if t is not None and not t.is_empty and t.stage != STAGE_STARVE:
            frozen[tid] = (t.stage, t.batch_id)
            t.stage = STAGE_STARVE
    return frozen


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


def sixn_release_schedule(state, transfer_events, window_start, window_n,
                          hold_weeks=None):
    """Release timing for the window-CLOSE 6N contents, honoring the purge hold
    from the handoff (window semantics — the planner continues from operator
    truth and must not release scripted stagings early).

    `state` is the post-window facility state; `transfer_events` the window's
    recorded transfers (events.Transfer / events.GradedHarvest — a 6N
    destination marks the tank's fill week); `window_start` the date of window
    week 1; `window_n` the window length in weeks. Returns L1's
    `purge_release_schedule` entries {batch_id, count, biomass_kg, avg_wt_g,
    release_week} where release_week is 0-based from the HANDOFF week:

      * a tank a scripted og_to_6n / graded-to-6N filled at window week k
        (1-based) releases `hold_weeks` after that week — global week
        k + hold - (window_n + 1), clamped >= 0;
      * a tank holding PR-start fish the window did not touch has already
        served its hold (it sat frozen through the window) — releasable from
        the handoff, spread over the first `hold_weeks` slots like the
        no-window purge_inflight (the pair-per-week rotation).

    A scripted top-up of a PR-start tank re-times the WHOLE tank to the
    scripted week (conservative: never releases held fish early).
    """
    from .sixn import SIXN_ALL_TANKS
    if hold_weeks is None:
        from .global_planner_poc import _PURGE_HOLD_WEEKS as hold_weeks
    entry_week: dict[int, int] = {}
    for tr in transfer_events or []:
        d0 = getattr(tr, "event_date", None)
        if d0 is None:
            continue
        wk = (d0 - window_start).days // 7 + 1     # 1-based window week
        dest_ids = ([tr.pickup_tank_id] if hasattr(tr, "pickup_tank_id")
                    else [a.tank_id
                          for a in getattr(tr, "destinations", []) or []])
        for tid in dest_ids:
            if tid in SIXN_ALL_TANKS:
                entry_week[tid] = max(wk, entry_week.get(tid, 0))
    out: list[dict] = []
    for t in state.tanks_by_id.values():
        if t.is_empty or t.tank_id not in SIXN_ALL_TANKS or t.count <= 0:
            continue
        wt = t.biomass_kg * 1000.0 / t.count
        if t.tank_id in entry_week:
            rel = max(0, entry_week[t.tank_id] + hold_weeks - (window_n + 1))
            out.append({"batch_id": t.batch_id, "count": t.count,
                        "biomass_kg": t.biomass_kg, "avg_wt_g": wt,
                        "release_week": rel})
        else:
            for k in range(hold_weeks):
                out.append({"batch_id": t.batch_id, "count": t.count / hold_weeks,
                            "biomass_kg": t.biomass_kg / hold_weeks,
                            "avg_wt_g": wt, "release_week": k})
    return out


def dark_handoff_weeks(sixn_start, events, window_weeks=None, hold_weeks=None):
    """PURE handoff-continuity check for a scripted manual window (no Streamlit,
    no biology): does the window drain the 6N depuration pipeline so that a
    handoff-era week has NOTHING harvestable under the purge hold?

    Inputs:
      sixn_start    {tank_id: fish_count} — the 6N tanks' contents in the
                    PR-hydrated starting state (week-1 open).
      events        the scripted ManualEvents (forecast.manual_events).
      window_weeks  N, the window length; default = the last scripted week.
      hold_weeks    the depuration hold (default: the engine's 2).

    Model (mirrors the engines' handoff semantics):
      * PR-start 6N fish are releasable from the handoff (their hold is served
        by sitting frozen through the window).
      * A scripted harvest / graded_harvest FROM a 6N tank removes fish.
      * A scripted og_to_6n / staged graded (is_staged_graded — the 6N-pickup
        default) at week k adds fish to the destination, releasable from week
        k + hold. A mode-'harvest' graded_harvest drains its pickup in its own
        week, so it stages nothing. An unknown staged count (count=None =
        "whole tank" — the source size isn't known here) counts as a positive
        presence, which is all the zero-check needs.

    Returns the list of 1-based ABSOLUTE week indices (window timeline, so the
    handoff is week N+1) among weeks N+1 .. N+hold with ZERO releasable 6N
    fish. Empty list = the handoff is covered.
    """
    from .manual_events import (
        TYPE_GRADED_HARVEST, TYPE_HARVEST, TYPE_OG_TO_6N, TYPE_OG_TRANSFER,
        is_staged_graded)
    from .sixn import SIXN_ALL_TANKS
    if hold_weeks is None:
        from .global_planner_poc import _PURGE_HOLD_WEEKS as hold_weeks
    if window_weeks:
        n = int(window_weeks)
    elif events:
        n = max(int(e.week or 1) for e in events)
    else:
        n = 0
    if n <= 0:
        return []
    # Per-tank pools: list of [count, releasable_from_week]. PR-start fish are
    # releasable from the handoff (week n+1).
    pools: dict[int, list[list[float]]] = {
        tid: [[float(c), float(n + 1)]]
        for tid, c in (sixn_start or {}).items()
        if tid in SIXN_ALL_TANKS and c and c > 0}
    for ev in sorted(events or [], key=lambda e: (e.week or 1)):
        wk = ev.week or 1
        if ev.type in (TYPE_HARVEST, TYPE_GRADED_HARVEST) \
                and ev.from_tank in SIXN_ALL_TANKS:
            pool = pools.get(ev.from_tank, [])
            take = (float(ev.count) if ev.count
                    else sum(p[0] for p in pool))       # None = whole tank
            for p in pool:                              # oldest-releasable first
                got = min(p[0], take)
                p[0] -= got
                take -= got
                if take <= 0:
                    break
        staged_to = []
        if ev.type == TYPE_GRADED_HARVEST and is_staged_graded(ev):
            # STAGED graded->6N (the 6N-pickup default): the biggest `count`
            # fish purge in the 6N pickup tank. (A mode-'harvest' graded event
            # drains its pickup in its own week — nothing staged for the
            # handoff.)
            staged_to = [(ev.destinations[0].tank, ev.count)]
        elif ev.type in (TYPE_OG_TO_6N, TYPE_OG_TRANSFER):
            staged_to = [(d.tank, d.count) for d in (ev.destinations or [])
                         if d.tank in SIXN_ALL_TANKS]
        for tid, cnt in staged_to:
            # Unknown count (None) = "whole source tank" — a positive presence.
            pools.setdefault(tid, []).append(
                [float(cnt) if cnt else 1.0, float(wk + hold_weeks)])
    dark = []
    for w in range(n + 1, n + hold_weeks + 1):
        releasable = sum(p[0] for pool in pools.values() for p in pool
                         if p[1] <= w and p[0] > 0)
        if releasable <= 0.5:
            dark.append(w)
    return dark


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
    purge_hold: dict = {}   # tank_id -> (pre-freeze stage, batch) for handoff restore
    week_start = forecast_start
    for i in range(n_weeks):
        # Depuration hold FIRST: in purge mode, freeze any pre-existing 6N tank
        # to STARVE (no growth / no feed) before anything else this week, so the
        # opening snapshot, heatmap + system rollup all show it depurating (feed
        # excluded) and the biology walk below doesn't grow it. Gated per
        # week-date so a mid-window 6N->production crossing stops freezing.
        # The hold is restored at the handoff below (it must not go downstream).
        newly_frozen = _freeze_purge_6n(state, control, week_start)
        for tid, info in newly_frozen.items():
            purge_hold.setdefault(tid, info)   # keep the FIRST-freeze original
        if newly_frozen:
            warnings.append(
                f"6N depuration (purge mode), week {labels[i]}: held tanks "
                f"{sorted(newly_frozen)} frozen — no growth, no feed (mortality "
                f"still applies); restored to the auto pipeline's starting state "
                f"at the window handoff (the planner runs its own 6N rotation)")
        # OPENING snapshot — the true start-of-week state, taken BEFORE this
        # week's operations AND before its growth/mortality. So week i's grid
        # column shows what the operator has to act ON when the week opens: for
        # week 1 that's the raw PR-hydrated facility (with 6N already held);
        # for later weeks it's the prior week's close. A harvest/move scripted
        # in week i therefore KEEPS showing the fish in week i (you still see
        # what you acted on) and only empties the tank from week i+1 onward.
        # (batch_locations below stays the end-of-week/closing snapshot the run
        # stitches into the output + audits — that ordering is unchanged.)
        opening_locations.extend(_snapshot_week(state, labels[i], week_start))
        # Operations next — they still date into THIS week for the continuity
        # audit — then biology, then the closing snapshot.
        hv: list = []
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
        # Steady-harvest contract lint: window weeks run ONLY scripted events,
        # so a window week without a scripted harvest is a ZERO-harvest week in
        # the plan. Say so loudly — never let the operator discover an empty
        # week in the output (operator-hit 2026-08: a graded staging silently
        # replaced the expected harvest and W33 shipped nothing).
        #
        # OUTSIDE the `if events` guard on purpose. The emptiest window of all —
        # `--advance-weeks N` with no scripted events at all (run.py takes
        # window_n = max(advance_weeks, last event week), so a window can exist
        # with an EMPTY event list) — is N consecutive zero-harvest weeks, and
        # is exactly the case this lint exists to catch. Inside the guard it
        # was the one case that could never fire.
        if not hv:
            warnings.append(
                f"MANUAL WINDOW — {labels[i]} schedules NO harvest: window "
                f"weeks execute only your scripted events, so this is a "
                f"zero-harvest week (steady-harvest contract). Script a "
                f"harvest or graded_harvest in this week if that is not "
                f"intended.")
        else:
            # The mirror lint: the window executes the script even past the
            # plant's weekly processing ceiling (operator law), but an
            # over-ceiling week is physically suspect — say so.
            _tot = sum(h.count for h in hv)
            _cap = float(getattr(control, "max_harvest_per_week", 0) or 0)
            _rel = float(getattr(control, "harvest_relief_pct", 0) or 0)
            _ceil = _cap * (1.0 + _rel)
            if _ceil > 0 and _tot > _ceil + 0.5:
                warnings.append(
                    f"MANUAL WINDOW — {labels[i]} scripts "
                    f"{_tot:,.0f} fish of harvest, above the plant ceiling "
                    f"{_ceil:,.0f} (max_harvest_per_week + relief). The "
                    f"window executed your script anyway — check the week "
                    f"is actually processable.")
        wk_realized = advance_facility_one_week(
            state, batch_by_id, tables, week_start, labels[i])
        realized.update(wk_realized)
        batch_locations.extend(_snapshot_week(state, labels[i], week_start))
        week_start = week_start + timedelta(days=7)
    # Handoff: undo the purge hold on the RETURNED state so the auto pipeline
    # starts from its expected condition and runs its OWN 6N rotation — the hold
    # is a manual-window concern only and must not propagate downstream. The
    # window's closing snapshots (batch_locations, taken inside the loop) keep
    # STARVE, so the window weeks stay correct in the output + audits; only the
    # live state the planner inherits is restored. Restore a tank ONLY if it is
    # still the same depurating batch we froze (an operator og_to_6n / graded->6N
    # that landed on it, or a harvest that emptied it, is left untouched).
    for tid, (orig_stage, orig_batch) in purge_hold.items():
        t = state.tanks_by_id.get(tid)
        if (t is not None and not t.is_empty
                and t.stage == STAGE_STARVE and t.batch_id == orig_batch):
            t.stage = orig_stage
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
