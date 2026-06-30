"""Manual starting-state events (operator-authored).

The operator hand-authors a small set of operational events that adjust the
PR-hydrated **starting state** (week-0 truth) before the forecast runs forward.
This is the "define the starting point, then let the planner build on top"
feature.

Design (locked with the operator):
  * AUGMENT the ProductionReport — the PR still hydrates the initial
    FacilityState; these events are applied ON TOP to adjust it.
  * STARTING-STATE ONLY — events mutate the week-0 state, then the forecast
    runs forward normally. They are NOT future pins the planner must honor, so
    none of the closed-loop harvest / placement engine changes.
  * Engine computes biology by default; partial / graded / harvest-grade are
    explicit options (graded + harvest-grade land in a later phase).

Each event is applied through the existing `events.py` `.apply(state)` methods,
which already enforce conservation + the INV rules, so a refused event leaves
the source intact (no fish lost).

Phase 1 implements `og_transfer` (OG->OG move/split). `fw_to_og` and
`og_to_6n` are recognised but deferred (they touch the FW biology path / 6N
purge logic) — they warn-and-skip until their phase lands.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .events import Harvest, TankAllocation, TranOGEntry, Transfer
from .yaml_atomic import read_text_resilient, write_text_atomic

MANUAL_EVENTS_FILE = "manual_events.yaml"

# Event types recognised by this module. og_transfer + harvest are wired;
# fw_to_og / og_to_6n are recognised but deferred to a later phase.
TYPE_OG_TRANSFER = "og_transfer"
TYPE_HARVEST = "harvest"
TYPE_FW_TO_OG = "fw_to_og"
TYPE_OG_TO_6N = "og_to_6n"


@dataclass
class ManualDest:
    """One destination tank for a manual event.

    count   — fish to send here; None means "all remaining from the source"
              (single dest) or "even split of the remainder" (multiple).
    avg_wt_g — destination weight; None inherits the source weight (engine
              computes — an instantaneous start-state move carries the source
              avg weight forward).
    """
    tank: int
    count: Optional[float] = None
    avg_wt_g: Optional[float] = None


@dataclass
class ManualEvent:
    type: str
    week: int = 1                            # 1-based forecast week the event fires in (the override window)
    from_tank: Optional[int] = None          # source tank id (og_transfer / harvest / og_to_6n)
    destinations: list[ManualDest] = field(default_factory=list)
    count: Optional[float] = None            # harvest amount; None = harvest the whole tank
    batch: Optional[str] = None              # optional cross-check; inferred from source if absent
    mode: str = "transfer"                    # "transfer" | "graded" | "harvest_grade" (later phases)
    notes: str = ""


# ---------- (de)serialization ----------

def manual_events_to_list(events: list[ManualEvent]) -> list[dict]:
    out: list[dict] = []
    for e in events:
        out.append({
            "type": e.type,
            "week": e.week,
            "from_tank": e.from_tank,
            "destinations": [
                {"tank": d.tank, "count": d.count, "avg_wt_g": d.avg_wt_g}
                for d in e.destinations
            ],
            "count": e.count,
            "batch": e.batch,
            "mode": e.mode,
            "notes": e.notes,
        })
    return out


def manual_events_from_list(data: list[dict]) -> list[ManualEvent]:
    out: list[ManualEvent] = []
    for d in data or []:
        dests = [
            ManualDest(
                tank=int(x["tank"]),
                count=(float(x["count"]) if x.get("count") is not None else None),
                avg_wt_g=(float(x["avg_wt_g"]) if x.get("avg_wt_g") is not None else None),
            )
            for x in (d.get("destinations") or [])
        ]
        ft = d.get("from_tank")
        cnt = d.get("count")
        out.append(ManualEvent(
            type=str(d["type"]),
            week=int(d.get("week") or 1),
            from_tank=(int(ft) if ft is not None else None),
            destinations=dests,
            count=(float(cnt) if cnt is not None else None),
            batch=(str(d["batch"]) if d.get("batch") is not None else None),
            mode=str(d.get("mode") or "transfer"),
            notes=str(d.get("notes") or ""),
        ))
    return out


def load_manual_events(scenario_dir) -> list[ManualEvent]:
    path = Path(scenario_dir) / MANUAL_EVENTS_FILE
    if not path.exists():
        return []
    data = yaml.safe_load(read_text_resilient(path)) or {}
    return manual_events_from_list(data.get("events", []))


def dump_manual_events(scenario_dir, events: list[ManualEvent]) -> None:
    d = Path(scenario_dir)
    d.mkdir(parents=True, exist_ok=True)
    text = (
        "# Manual starting-state events (operator-authored). Applied to the\n"
        "# PR-hydrated week-0 state BEFORE the forecast runs forward. See\n"
        "# forecast/manual_events.py. Starting-state only (not future pins).\n"
        + yaml.safe_dump({"events": manual_events_to_list(events)},
                         sort_keys=False, allow_unicode=True,
                         default_flow_style=False)
    )
    write_text_atomic(d / MANUAL_EVENTS_FILE, text)


# ---------- application to the starting state ----------

def _apply_og_transfer(state, ev: ManualEvent, idx: int,
                       event_date=None, out_events=None) -> list[str]:
    """Apply one OG->OG transfer/split to the hydrated state.

    Reuses events.Transfer.apply(), which enforces INV-1/INV-4/reserved and is
    continuity-safe on refusal (the source is drained only by what was actually
    accepted). Pure relocation: destination inherits the source avg weight
    unless overridden (no handling-mortality applied to a start-state move).
    """
    warns: list[str] = []
    tag = f"MANUAL og_transfer #{idx}"
    src = state.tanks_by_id.get(ev.from_tank)
    if src is None:
        return [f"{tag}: unknown source tank #{ev.from_tank}"]
    if src.is_empty:
        return [f"{tag}: source tank {src.location_id} is empty (nothing to move)"]
    if not ev.destinations:
        return [f"{tag}: no destinations specified"]
    if ev.batch and src.batch_id != ev.batch:
        warns.append(
            f"{tag}: source {src.location_id} holds batch {src.batch_id}, "
            f"not the specified {ev.batch} — using actual batch {src.batch_id}")
    batch_id = src.batch_id

    # Resolve destination counts: explicit counts honoured; None-count dests
    # share the remainder evenly (single None dest => "all remaining").
    explicit = sum(d.count for d in ev.destinations if d.count is not None)
    null_dests = [d for d in ev.destinations if d.count is None]
    remaining = src.count - explicit
    if remaining < -0.5:
        return [f"{tag}: requested {explicit:,.0f} fish exceeds tank "
                f"{src.location_id} population {src.count:,.0f}"]
    per_null = (remaining / len(null_dests)) if null_dests else 0.0

    allocs = []
    for d in ev.destinations:
        cnt = d.count if d.count is not None else per_null
        wt = d.avg_wt_g if d.avg_wt_g is not None else src.avg_wt_g
        allocs.append(TankAllocation(
            tank_id=d.tank, count=cnt, avg_wt_g=wt, cv_pct=src.cv_pct))

    total = sum(a.count for a in allocs)
    leaves_empty = abs(total - src.count) < 0.5
    tr = Transfer(
        batch_id=batch_id, event_date=(event_date or state.today),
        source_tank_id=ev.from_tank,
        destinations=allocs, leaves_source_empty=leaves_empty)
    warns.extend(f"{tag}: {w}" for w in tr.apply(state))

    if tr.count_transferred <= 0:
        warns.append(f"{tag}: moved 0 fish (all destinations refused) — "
                     f"batch {batch_id} stays in tank #{ev.from_tank}")
    else:
        if out_events is not None:
            out_events.append(tr)
        dest_ids = [d.tank for d in ev.destinations]
        print(f"    {tag}: moved {tr.count_transferred:,.0f} fish of batch "
              f"{batch_id} from tank #{ev.from_tank} -> tanks {dest_ids}"
              f"{' (source emptied)' if src.is_empty else ''}")
    return warns


def _apply_harvest(state, ev: ManualEvent, idx: int,
                   event_date=None, out_events=None) -> list[str]:
    """Apply one direct harvest via events.Harvest.

    Removes `count` fish (None = the whole tank) from the source. In the
    override window this is recorded as a real harvest in the week it fires.
    """
    warns: list[str] = []
    tag = f"MANUAL harvest #{idx}"
    src = state.tanks_by_id.get(ev.from_tank)
    if src is None:
        return [f"{tag}: unknown source tank #{ev.from_tank}"]
    if src.is_empty:
        return [f"{tag}: source tank {src.location_id} is empty (nothing to harvest)"]
    if ev.batch and src.batch_id != ev.batch:
        warns.append(
            f"{tag}: source {src.location_id} holds batch {src.batch_id}, "
            f"not the specified {ev.batch} — using actual batch {src.batch_id}")
    batch_id = src.batch_id
    count = ev.count if ev.count is not None else src.count
    if count > src.count + 0.5:
        return [f"{tag}: requested {count:,.0f} fish exceeds tank "
                f"{src.location_id} population {src.count:,.0f}"]
    h = Harvest(batch_id=batch_id, event_date=(event_date or state.today),
                source_tank_id=ev.from_tank, count=count, avg_wt_g=src.avg_wt_g)
    warns.extend(f"{tag}: {w}" for w in h.apply(state))
    if h.count > 0 and out_events is not None:
        out_events.append(h)
    print(f"    {tag}: harvested {h.count:,.0f} fish of batch {batch_id} "
          f"from tank #{ev.from_tank}")
    return warns


def _apply_og_to_6n(state, ev: ManualEvent, idx: int,
                    event_date=None, out_events=None) -> list[str]:
    """Move OG fish into a 6N depuration tank (normal-transfer mode).

    Reuses events.Transfer (so the move is audited as Transfer_Out/In), then
    FREEZES each 6N destination to STAGE_STARVE — off-feed depuration: the daily
    biology loop applies mortality but no growth, and the feed reports exclude
    STARVE tank-weeks. Destinations must be OG6N tanks.
    """
    from .sixn import SIXN_ALL_TANKS
    from .state import STAGE_STARVE
    warns: list[str] = []
    tag = f"MANUAL og_to_6n #{idx}"
    src = state.tanks_by_id.get(ev.from_tank)
    if src is None:
        return [f"{tag}: unknown source tank #{ev.from_tank}"]
    if src.is_empty:
        return [f"{tag}: source tank {src.location_id} is empty (nothing to move)"]
    if not ev.destinations:
        return [f"{tag}: no 6N destination specified"]
    for d in ev.destinations:
        if d.tank not in SIXN_ALL_TANKS:
            return [f"{tag}: dest tank #{d.tank} is not a 6N depuration tank "
                    f"({sorted(SIXN_ALL_TANKS)})"]
    if ev.batch and src.batch_id != ev.batch:
        warns.append(f"{tag}: source {src.location_id} holds batch {src.batch_id}, "
                     f"not the specified {ev.batch} — using actual batch {src.batch_id}")
    batch_id = src.batch_id

    explicit = sum(d.count for d in ev.destinations if d.count is not None)
    null_dests = [d for d in ev.destinations if d.count is None]
    remaining = src.count - explicit
    if remaining < -0.5:
        return [f"{tag}: requested {explicit:,.0f} fish exceeds tank "
                f"{src.location_id} population {src.count:,.0f}"]
    per_null = (remaining / len(null_dests)) if null_dests else 0.0
    allocs = []
    for d in ev.destinations:
        cnt = d.count if d.count is not None else per_null
        wt = d.avg_wt_g if d.avg_wt_g is not None else src.avg_wt_g
        allocs.append(TankAllocation(
            tank_id=d.tank, count=cnt, avg_wt_g=wt, cv_pct=src.cv_pct))
    total = sum(a.count for a in allocs)
    leaves_empty = abs(total - src.count) < 0.5
    tr = Transfer(
        batch_id=batch_id, event_date=(event_date or state.today),
        source_tank_id=ev.from_tank, destinations=allocs,
        leaves_source_empty=leaves_empty)
    warns.extend(f"{tag}: {w}" for w in tr.apply(state))

    if tr.count_transferred <= 0:
        warns.append(f"{tag}: moved 0 fish (all destinations refused) — "
                     f"batch {batch_id} stays in tank #{ev.from_tank}")
    else:
        for d in ev.destinations:
            t = state.tanks_by_id.get(d.tank)
            if t is not None and not t.is_empty:
                t.stage = STAGE_STARVE  # freeze: off-feed depuration
        if out_events is not None:
            out_events.append(tr)
        print(f"    {tag}: moved {tr.count_transferred:,.0f} fish of batch "
              f"{batch_id} from tank #{ev.from_tank} -> 6N "
              f"{[d.tank for d in ev.destinations]} (frozen, off-feed)")
    return warns


def _apply_fw_to_og(state, ev: ManualEvent, idx: int, fw_count, fw_avg_wt_g,
                    fw_cv, handling_frac, event_date=None, out_tranog=None):
    """Manual FW->OG transfer (TranOG) into operator-chosen OG tanks.

    Applies the SAME logic as the auto pipeline: handling mortality, then a
    reconcile-to-target cull (ev.count = the target tran_og_count) via
    _apply_bottom_cull, then the size-class split; emits a TranOGEntry into the
    chosen OG tanks (big class to the first ceil(N/2) tanks, small to the rest,
    mirroring placement). `fw_*` is the batch's FW state at this week (supplied
    by the caller from the FW projection). Returns (warnings, culled_count).
    """
    from .biology import _apply_bottom_cull, compute_size_class_split
    warns: list[str] = []
    tag = f"MANUAL fw_to_og #{idx}"
    batch_id = ev.batch
    if not batch_id:
        return [f"{tag}: fw_to_og requires `batch` (the FW batch to transfer)"], 0.0
    if not ev.destinations:
        return [f"{tag}: no OG destination tanks specified"], 0.0
    for d in ev.destinations:
        t = state.tanks_by_id.get(d.tank)
        if t is None:
            return [f"{tag}: unknown dest tank #{d.tank}"], 0.0
        if t.type != "OG":
            return [f"{tag}: dest #{d.tank} ({t.location_id}) is not an OG tank"], 0.0
        if not t.is_empty:
            return [f"{tag}: dest {t.location_id} not empty (holds {t.batch_id})"], 0.0
    if fw_count <= 0:
        return [f"{tag}: FW batch {batch_id} has no fish at this week"], 0.0

    # 1. handling mortality, 2. reconcile-to-target cull (operator's tran_og_count)
    cnt = fw_count * (1.0 - handling_frac)
    culled = fw_count - cnt
    target = ev.count
    if target and cnt > target:
        cull_pct = 1.0 - target / cnt
        cnt, fw_avg_wt_g, _cn, _cb = _apply_bottom_cull(
            cnt, fw_avg_wt_g, fw_cv, cull_pct)
        culled += _cn

    # 3. size-class split, 4. allocate big/small across the chosen tanks
    split = compute_size_class_split(
        batch_id=batch_id, tran_og_date=event_date,
        post_cull_count=cnt, post_cull_avg_wt_g=fw_avg_wt_g, cv_pct=fw_cv)
    tanks = [d.tank for d in ev.destinations]
    n = len(tanks)
    allocs = []
    if n >= 2:
        big_n = (n + 1) // 2
        small_n = n - big_n
        per_big = (split.big_class_count / big_n) if big_n else 0.0
        per_small = (split.small_class_count / small_n) if small_n else 0.0
        for i in range(big_n):
            allocs.append(TankAllocation(
                tank_id=tanks[i], count=per_big,
                avg_wt_g=split.big_class_avg_wt_g,
                cv_pct=split.post_cull_cv_pct, size_class="big"))
        for i in range(small_n):
            allocs.append(TankAllocation(
                tank_id=tanks[big_n + i], count=per_small,
                avg_wt_g=split.small_class_avg_wt_g,
                cv_pct=split.post_cull_cv_pct, size_class="small"))
    else:
        allocs.append(TankAllocation(
            tank_id=tanks[0], count=split.post_cull_count,
            avg_wt_g=split.post_cull_avg_wt_g,
            cv_pct=split.post_cull_cv_pct, size_class="mixed"))

    entry = TranOGEntry(batch_id=batch_id, event_date=event_date, destinations=allocs)
    warns.extend(f"{tag}: {w}" for w in entry.apply(state))
    if out_tranog is not None:
        out_tranog.append(entry)
    # Traceability: surface the manual TranOG cull as a labelled audit entry
    # (the engine's own FW culls show in the biology cull columns; this manual
    # FW->OG cull happens outside that path, so record it explicitly).
    if culled > 0:
        warns.append(
            f"MANUAL CULL — fw_to_og {batch_id} week {ev.week}: culled "
            f"{culled:,.0f} fish (handling-mortality + reconcile to target "
            f"{target:,.0f}); placed {cnt:,.0f} into OG tanks {tanks}")
    print(f"    {tag}: TranOG {cnt:,.0f} fish of {batch_id} -> OG tanks {tanks} "
          f"(culled {culled:,.0f} to hit target {target})")
    return warns, culled


def apply_events_for_week(state, events, week, week_start, week_label=None,
                          handling_frac=0.0, fw_lookup=None):
    """Apply every manual event scheduled for `week` (1-based) at the start of
    that override-window week, dating each event at `week_start`.

    Returns (transfer_objs, harvest_objs, tranog_objs, warnings): the events.*
    objects that actually applied, so the window can stitch them into the report
    streams + continuity audit. `fw_lookup` maps (batch_id, week_label) ->
    (count, avg_wt_g, cv) for fw_to_og events (the chosen FW batch's state at
    this week). Mutates `state`.
    """
    transfers: list = []
    harvests: list = []
    tranogs: list = []
    warns: list[str] = []
    for i, ev in enumerate(events, 1):
        if (ev.week or 1) != week:
            continue
        if ev.type == TYPE_OG_TRANSFER:
            warns.extend(_apply_og_transfer(
                state, ev, i, event_date=week_start, out_events=transfers))
        elif ev.type == TYPE_HARVEST:
            warns.extend(_apply_harvest(
                state, ev, i, event_date=week_start, out_events=harvests))
        elif ev.type == TYPE_OG_TO_6N:
            warns.extend(_apply_og_to_6n(
                state, ev, i, event_date=week_start, out_events=transfers))
        elif ev.type == TYPE_FW_TO_OG:
            fw = (fw_lookup or {}).get((ev.batch, week_label))
            if fw is None:
                warns.append(f"MANUAL week {week} fw_to_og #{i}: no FW state for "
                             f"batch {ev.batch!r} at this week (must be an in-flight "
                             f"FW batch still in freshwater)")
            else:
                w, _culled = _apply_fw_to_og(
                    state, ev, i, fw[0], fw[1], fw[2], handling_frac,
                    event_date=week_start, out_tranog=tranogs)
                warns.extend(w)
        else:
            warns.append(f"MANUAL week {week} event #{i}: unknown type "
                         f"'{ev.type}' — skipped")
    return transfers, harvests, tranogs, warns


def validate_manual_events(state, events: list[ManualEvent]) -> list[tuple[int, bool, list[str]]]:
    """Dry-run each event against a COPY of the hydrated state for reject-at-entry.

    Applies events cumulatively to a deep copy (so event N sees the effect of
    1..N-1, matching the real run) without mutating the caller's state. Returns
    one (index, ok, messages) tuple per event; ok=False means the event was
    refused or produced a warning the UI should surface before saving.
    """
    scratch = copy.deepcopy(state)
    results: list[tuple[int, bool, list[str]]] = []
    for i, ev in enumerate(events, 1):
        if ev.type == TYPE_OG_TRANSFER:
            w = _apply_og_transfer(scratch, ev, i)
        elif ev.type == TYPE_HARVEST:
            w = _apply_harvest(scratch, ev, i)
        elif ev.type == TYPE_OG_TO_6N:
            w = _apply_og_to_6n(scratch, ev, i)
        elif ev.type == TYPE_FW_TO_OG:
            # Full feasibility needs the run-time FW projection; here just check
            # the destination tanks are empty OG tanks.
            w = []
            for d in ev.destinations:
                t = scratch.tanks_by_id.get(d.tank)
                if t is None:
                    w.append(f"unknown dest tank #{d.tank}")
                elif t.type != "OG":
                    w.append(f"dest #{d.tank} is not an OG tank")
                elif not t.is_empty:
                    w.append(f"dest {t.location_id} not empty")
        else:
            w = [f"unknown type '{ev.type}'"]
        results.append((i, not w, w))
    return results
