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
  * Engine computes biology by default; graded harvest (top-N-by-size) is an
    explicit option that reuses the engine's GradedHarvest split.

Each event is applied through the existing `events.py` `.apply(state)` methods,
which already enforce conservation + the INV rules, so a refused event leaves
the source intact (no fish lost).

Five event types are wired end-to-end: `og_transfer` (OG->OG move/split),
`harvest` (plain), `og_to_6n` (move into 6N depuration), `fw_to_og` (manual
TranOG with cull) and `graded_harvest` (size-sort a tank, harvest the biggest
N, retain the rest growing). Each conserves count + biomass and reconciles in
the tank-continuity + input-conservation audits.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .events import GradedHarvest, Harvest, TankAllocation, TranOGEntry, Transfer
from .yaml_atomic import read_text_resilient, write_text_atomic

MANUAL_EVENTS_FILE = "manual_events.yaml"

# Event types recognised by this module. All five are wired end-to-end.
TYPE_OG_TRANSFER = "og_transfer"
TYPE_HARVEST = "harvest"
TYPE_FW_TO_OG = "fw_to_og"
TYPE_OG_TO_6N = "og_to_6n"
TYPE_GRADED_HARVEST = "graded_harvest"


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

    # leaves_source_empty=False: let Transfer.apply empty the source ONLY when it
    # actually drains to ~0 (its own src.count<=0.5 check). Deriving it from the
    # PLANNED total would force-empty and DELETE the un-moved share if any
    # destination is refused (INV-1/INV-4/reserved) — a silent fish loss.
    tr = Transfer(
        batch_id=batch_id, event_date=(event_date or state.today),
        source_tank_id=ev.from_tank,
        destinations=allocs, leaves_source_empty=False)
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
    # leaves_source_empty=False (see _apply_og_transfer): avoid deleting the
    # un-moved share on a partial refusal; Transfer.apply auto-empties at ~0.
    tr = Transfer(
        batch_id=batch_id, event_date=(event_date or state.today),
        source_tank_id=ev.from_tank, destinations=allocs,
        leaves_source_empty=False)
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
        _tgt = f"{target:,.0f}" if target else "all FW (no target)"
        warns.append(
            f"MANUAL CULL — fw_to_og {batch_id} week {ev.week}: culled "
            f"{culled:,.0f} fish (handling-mortality + reconcile to target "
            f"{_tgt}); placed {cnt:,.0f} into OG tanks {tanks}")
    print(f"    {tag}: TranOG {cnt:,.0f} fish of {batch_id} -> OG tanks {tanks} "
          f"(culled {culled:,.0f} to hit target {target})")
    return warns, culled


def _apply_graded_harvest(state, ev: ManualEvent, idx: int, event_date=None,
                          out_transfers=None, out_harvests=None) -> list[str]:
    """Manual GRADED harvest: take the biggest `ev.count` fish from the source to
    processing, retain the smaller remainder growing (in the source, or a chosen
    retention tank).

    Size-sorts the source population at the count-implied cutoff so BOTH count and
    biomass conserve EXACTLY: with p = count/N the harvested fraction, the cutoff
    is mu + sigma*Phi^-1(1-p) and biology.upper_truncated_split returns the
    conditional means, giving big_count*big_avg + small_count*small_avg == N*mu
    (the E[X] = P*E[X|>=t] + (1-P)*E[X|<t] identity). Emits the engine's
    events.GradedHarvest (source -> pickup staging tank + retention) CHAINED with a
    plain events.Harvest that drains the pickup to processing — the SAME shape the
    auto-pipeline's 6N purge uses, so all three audits reconcile it with NO audit
    change. destinations[0] = pickup staging tank (empty OG); destinations[1]
    (optional) = retention tank, defaulting to the source (smalls stay in place).
    No handling mortality (a size sort is instantaneous, not a re-water transfer).
    """
    from statistics import NormalDist
    from .biology import upper_truncated_split
    warns: list[str] = []
    tag = f"MANUAL graded_harvest #{idx}"
    src = state.tanks_by_id.get(ev.from_tank)
    if src is None:
        return [f"{tag}: unknown source tank #{ev.from_tank}"]
    if src.is_empty:
        return [f"{tag}: source tank {src.location_id} is empty (nothing to grade)"]
    if not ev.destinations:
        return [f"{tag}: no pickup (harvest-staging) tank specified"]
    if ev.batch and src.batch_id != ev.batch:
        warns.append(
            f"{tag}: source {src.location_id} holds batch {src.batch_id}, "
            f"not the specified {ev.batch} — using actual batch {src.batch_id}")
    batch_id = src.batch_id

    pickup_id = ev.destinations[0].tank
    retention_id = (ev.destinations[1].tank if len(ev.destinations) >= 2
                    else src.tank_id)
    pickup = state.tanks_by_id.get(pickup_id)
    if pickup is None:
        return [f"{tag}: unknown pickup tank #{pickup_id}"]
    if pickup_id == src.tank_id:
        return [f"{tag}: pickup tank must differ from the source {src.location_id}"]
    if not pickup.is_empty and pickup.batch_id != batch_id:
        return [f"{tag}: pickup {pickup.location_id} not empty "
                f"(holds {pickup.batch_id})"]
    if retention_id != src.tank_id:
        ret = state.tanks_by_id.get(retention_id)
        if ret is None:
            return [f"{tag}: unknown retention tank #{retention_id}"]
        if not ret.is_empty and ret.batch_id != batch_id:
            return [f"{tag}: retention {ret.location_id} not empty "
                    f"(holds {ret.batch_id})"]

    K = ev.count
    if not K or K <= 0:
        return [f"{tag}: needs a positive harvest count (the number of biggest "
                f"fish to take)"]
    if K >= src.count - 0.5:
        return [f"{tag}: count {K:,.0f} >= tank {src.location_id} population "
                f"{src.count:,.0f} — use a plain harvest to take the whole tank"]

    # Top-K-by-size split at the count-implied cutoff (see docstring): count is
    # exact (big=K, small=N-K); biomass conserves via the conditional means.
    mu = src.avg_wt_g
    cv = src.cv_pct or 16.0
    n = src.count
    p = K / n
    sigma = mu * (cv / 100.0)
    if sigma <= 0:
        big_avg = small_avg = mu
    else:
        z = NormalDist().inv_cdf(1.0 - p)
        big_avg, small_avg = upper_truncated_split(mu, cv, mu + sigma * z)
    big_count = float(K)
    small_count = n - big_count

    gh = GradedHarvest(
        batch_id=batch_id, event_date=(event_date or state.today),
        source_tank_id=src.tank_id,
        pickup_tank_id=pickup_id, pickup_count=big_count,
        pickup_avg_wt_g=big_avg, pickup_source_avg_wt_g=big_avg,
        retention_tank_id=retention_id, retention_count=small_count,
        retention_avg_wt_g=small_avg, cv_pct=cv)
    pre = pickup.count
    warns.extend(f"{tag}: {w}" for w in gh.apply(state))
    # Reject-safety: emit ONLY if the split actually landed (GradedHarvest.apply
    # warns-and-returns without draining on a wrong-batch dest). Otherwise the
    # source keeps its fish and NO orphan harvest fires on an empty/foreign tank.
    landed = (not pickup.is_empty and pickup.batch_id == batch_id
              and pickup.count - pre >= big_count - 0.5)
    if not landed:
        warns.append(f"{tag}: graded split refused — source {src.location_id} "
                     f"unchanged, no harvest emitted")
        return warns
    if out_transfers is not None:
        out_transfers.append(gh)
    # Chain the pickup harvest: drain the >= cutoff portion at the PICKUP weight
    # (big_avg), NOT the source mean (would inject a biomass mismatch on the
    # pickup tank-week and can breach the continuity BIO tolerance).
    h = Harvest(batch_id=batch_id, event_date=(event_date or state.today),
                source_tank_id=pickup_id, count=big_count, avg_wt_g=big_avg)
    warns.extend(f"{tag}: {w}" for w in h.apply(state))
    if h.count > 0 and out_harvests is not None:
        out_harvests.append(h)
    _ret = "source" if retention_id == src.tank_id else f"#{retention_id}"
    print(f"    {tag}: graded {n:,.0f} of {batch_id} in {src.location_id} -> "
          f"harvested top {big_count:,.0f}@{big_avg / 1000:.2f}kg (via pickup "
          f"#{pickup_id}), retained {small_count:,.0f}@{small_avg / 1000:.2f}kg "
          f"in {_ret}")
    return warns


def apply_events_for_week(state, events, week, week_start, week_label=None,
                          handling_frac=0.0, fw_lookup=None):
    """Apply every manual event scheduled for `week` (1-based) at the start of
    that override-window week, dating each event at `week_start`.

    Returns (transfer_objs, harvest_objs, tranog_objs, warnings, fw_balance):
    the events.* objects that actually applied, so the window can stitch them
    into the report streams + continuity audit, plus `fw_balance` =
    {batch_id: [fw_count_at_transfer, culled]} for each fw_to_og — the FW-phase
    conservation leg (fw_count == placed + culled) that the InputConservation
    audit reconciles (the batch becomes OG-in-flight with no FW states, so this
    is the only record of its FW losses). `fw_lookup` maps (batch_id, week_label)
    -> (count, avg_wt_g, cv) for fw_to_og events (the chosen FW batch's state at
    this week). Mutates `state`.
    """
    transfers: list = []
    harvests: list = []
    tranogs: list = []
    warns: list[str] = []
    fw_balance: dict[str, list[float]] = {}
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
        elif ev.type == TYPE_GRADED_HARVEST:
            warns.extend(_apply_graded_harvest(
                state, ev, i, event_date=week_start,
                out_transfers=transfers, out_harvests=harvests))
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
                # Record the FW-phase conservation leg for the audit gate:
                # fw_count entering the transfer must equal placed + culled.
                rec = fw_balance.setdefault(ev.batch, [0.0, 0.0])
                rec[0] += fw[0]       # fw_count at the transfer week
                rec[1] += _culled     # handling mortality + reconcile-to-target cull
        else:
            warns.append(f"MANUAL week {week} event #{i}: unknown type "
                         f"'{ev.type}' — skipped")
    return transfers, harvests, tranogs, warns, fw_balance


def _validate_fw_to_og_structural(scratch, ev) -> list[str]:
    """Cheap structural checks for an fw_to_og (dest tanks empty OG + batch set)
    that don't need the FW projection. Shared by both validation paths."""
    w: list[str] = []
    for d in ev.destinations:
        t = scratch.tanks_by_id.get(d.tank)
        if t is None:
            w.append(f"unknown dest tank #{d.tank}")
        elif t.type != "OG":
            w.append(f"dest #{d.tank} is not an OG tank")
        elif not t.is_empty:
            w.append(f"dest {t.location_id} not empty (holds {t.batch_id})")
    if not ev.batch:
        w.append("fw_to_og requires a FW batch")
    return w


def validate_manual_events(state, events: list[ManualEvent], *,
                           batch_by_id=None, tables=None, forecast_start=None,
                           control=None, pr_closing=None, fw_records=None,
                           ) -> list[tuple[int, bool, list[str]]]:
    """Dry-run each event for reject-at-entry, FAITHFUL to the real override
    window when the run-time context is supplied.

    The run (manual_window.advance_facility_window) applies events PER WEEK with
    biology (growth/mortality) advancing between weeks, and an fw_to_og only
    fires if its batch is still in freshwater at that week (the FW projection).
    Validation must mirror that, or it accepts events that misbehave on the run:
      * events are sequenced by their `week` (NOT list order) with a biology
        advance between weeks, so a week-k event is checked against grown
        weights (matters for weight-gated rules like the 1 kg-lock);
      * fw_to_og is checked against the SAME FW projection — the batch must be
        in freshwater at its week and the target count must be feasible.

    Pass batch_by_id/tables/forecast_start/control (and pr_closing/fw_records
    for fw_to_og) for the faithful path. Without them it falls back to the
    legacy single-pass frozen-week-0 structural check (still catches dest/INV
    errors) and leaves fw_to_og feasibility to run time. Returns one
    (index, ok, messages) tuple per event in input order; applies to a COPY so
    the caller's state is never mutated.
    """
    from datetime import timedelta as _td
    scratch = copy.deepcopy(state)
    faithful = (batch_by_id is not None and tables is not None
                and forecast_start is not None and control is not None)
    handling_frac = ((control.handling_mortality_pct / 100.0)
                     if control is not None else 0.0)

    fw_lookup: dict = {}
    labels: list[str] = []
    if faithful:
        from .manual_window import _build_fw_lookup, advance_facility_one_week
        from .time_grid import forecast_week_labels
        if fw_records is not None:
            fw_lookup = _build_fw_lookup(
                events, fw_records, control, pr_closing, tables, batch_by_id)
        _max_week = max((e.week or 1) for e in events) if events else 0
        labels = forecast_week_labels(forecast_start, max(_max_week, 1))

    def _apply_one(ev, i, week_label, week_start) -> list[str]:
        if ev.type == TYPE_OG_TRANSFER:
            return _apply_og_transfer(scratch, ev, i, event_date=week_start)
        if ev.type == TYPE_HARVEST:
            return _apply_harvest(scratch, ev, i, event_date=week_start)
        if ev.type == TYPE_OG_TO_6N:
            return _apply_og_to_6n(scratch, ev, i, event_date=week_start)
        if ev.type == TYPE_GRADED_HARVEST:
            # Applies to scratch (mutates it so later weeks see the post-split
            # state) but emits nothing — faithful reject-at-entry, like fw_to_og.
            return _apply_graded_harvest(scratch, ev, i, event_date=week_start)
        if ev.type == TYPE_FW_TO_OG:
            w = _validate_fw_to_og_structural(scratch, ev)
            if not faithful:
                return w  # feasibility deferred to run (legacy behavior)
            if not ev.batch:
                return w
            fw = fw_lookup.get((ev.batch, week_label))
            if fw is None:
                w.append(f"batch {ev.batch} is not in freshwater at week "
                         f"{ev.week or 1} — no FW state to transfer (already past "
                         f"TranOG, or not an in-flight FW batch)")
                return w
            avail = fw[0] * (1.0 - handling_frac)
            if ev.count and ev.count > avail + 0.5:
                w.append(f"target {ev.count:,.0f} fish exceeds available FW "
                         f"{avail:,.0f} at week {ev.week or 1}")
            if w:
                return w
            # Apply so later weeks' events see the placed fish (faithful). The
            # "MANUAL CULL ..." note _apply_fw_to_og emits is informational
            # traceability (a cull to hit the target is EXPECTED on a valid
            # transfer), NOT a feasibility failure — drop it so it doesn't block.
            wa, _culled = _apply_fw_to_og(
                scratch, ev, i, fw[0], fw[1], fw[2], handling_frac,
                event_date=week_start, out_tranog=[])
            return [m for m in wa if not m.startswith("MANUAL CULL")]
        return [f"unknown type '{ev.type}'"]

    msgs_by_idx: dict[int, list[str]] = {}
    if faithful:
        _max_week = max((e.week or 1) for e in events) if events else 0
        week_start = forecast_start
        for wk in range(1, _max_week + 1):
            lbl = labels[wk - 1]
            for i, ev in enumerate(events, 1):
                if (ev.week or 1) != wk:
                    continue
                msgs_by_idx[i] = _apply_one(ev, i, lbl, week_start)
            # Advance biology one week so the next week's events see grown fish.
            advance_facility_one_week(scratch, batch_by_id, tables, week_start, lbl)
            week_start = week_start + _td(days=7)
    else:
        # Legacy single pass in list order on the frozen week-0 state.
        for i, ev in enumerate(events, 1):
            msgs_by_idx[i] = _apply_one(ev, i, None, state.today)

    return [(i, not msgs_by_idx.get(i, []), msgs_by_idx.get(i, []))
            for i, _ev in enumerate(events, 1)]
