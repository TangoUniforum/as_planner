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

from .events import Harvest, TankAllocation, Transfer
from .yaml_atomic import read_text_resilient, write_text_atomic

MANUAL_EVENTS_FILE = "manual_events.yaml"

# Event types recognised by this module. og_transfer + harvest are wired;
# fw_to_og / og_to_6n are recognised but deferred to a later phase.
TYPE_OG_TRANSFER = "og_transfer"
TYPE_HARVEST = "harvest"
TYPE_FW_TO_OG = "fw_to_og"
TYPE_OG_TO_6N = "og_to_6n"
_DEFERRED_TYPES = {TYPE_FW_TO_OG, TYPE_OG_TO_6N}


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

def _apply_og_transfer(state, ev: ManualEvent, idx: int) -> list[str]:
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
        batch_id=batch_id, event_date=state.today, source_tank_id=ev.from_tank,
        destinations=allocs, leaves_source_empty=leaves_empty)
    warns.extend(f"{tag}: {w}" for w in tr.apply(state))

    if tr.count_transferred <= 0:
        warns.append(f"{tag}: moved 0 fish (all destinations refused) — "
                     f"batch {batch_id} stays in tank #{ev.from_tank}")
    else:
        dest_ids = [d.tank for d in ev.destinations]
        print(f"    {tag}: moved {tr.count_transferred:,.0f} fish of batch "
              f"{batch_id} from tank #{ev.from_tank} -> tanks {dest_ids}"
              f"{' (source emptied)' if src.is_empty else ''}")
    return warns


def _apply_harvest(state, ev: ManualEvent, idx: int) -> list[str]:
    """Apply one direct starting-state harvest via events.Harvest.

    Removes `count` fish (None = the whole tank) from the source. The fish are
    taken OUT of the week-0 inventory before the forecast runs forward.
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
    h = Harvest(batch_id=batch_id, event_date=state.today,
                source_tank_id=ev.from_tank, count=count, avg_wt_g=src.avg_wt_g)
    warns.extend(f"{tag}: {w}" for w in h.apply(state))
    print(f"    {tag}: harvested {h.count:,.0f} fish of batch {batch_id} "
          f"from tank #{ev.from_tank}")
    return warns


def apply_manual_events(state, events: list[ManualEvent]) -> list[str]:
    """Apply all manual starting-state events to the hydrated FacilityState.

    Returns warnings (problems + refusals) for the ValidationLog. Successful
    applications print a one-line summary to the run log. Mutates `state`.
    """
    warns: list[str] = []
    for i, ev in enumerate(events, 1):
        if ev.type == TYPE_OG_TRANSFER:
            warns.extend(_apply_og_transfer(state, ev, i))
        elif ev.type == TYPE_HARVEST:
            warns.extend(_apply_harvest(state, ev, i))
        elif ev.type in _DEFERRED_TYPES:
            warns.append(f"MANUAL event #{i}: type '{ev.type}' not yet "
                         f"implemented (Phase 3) — skipped")
        else:
            warns.append(f"MANUAL event #{i}: unknown type '{ev.type}' — skipped")
    return warns


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
        elif ev.type in _DEFERRED_TYPES:
            w = [f"type '{ev.type}' not yet implemented"]
        else:
            w = [f"unknown type '{ev.type}'"]
        results.append((i, not w, w))
    return results
