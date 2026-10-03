"""Before/after previews for a manual-window event the operator is about to add.

WHY (operator, 2026-10-03): "we should add some information to the manual
transfers windows so I know how much I am moving into and what it will look
like once done." Their October script filled 6N tank 63 in W44 with
23,357 + 32,000 = 55,357 fish -- above the 51,500 weekly processing limit
pinned for W46, the week that tank comes out of its depuration hold -- and
nothing in the editor said so. A whole-tank 6N drain cannot be split, so the
run had no way to honour both the script and the limit.

READ-ONLY. Nothing here changes a plan. Every number comes from the functions
the run itself executes:

  * the state a new week-`wk` event acts on = `advance_facility_window` for
    weeks 1..wk-1 (events + biology, exactly as the run) + the window's
    week-start 6N depuration freeze + `apply_events_for_week` for the events
    already scripted in week `wk` (the new event is appended, so the run
    applies it after them -- same order here);
  * the state "once done" = `apply_events_for_week` of the new event alone on
    a copy of that state.

So "before" includes everything earlier in the same week (the second of two
og_to_6n into tank 63 shows 23,357 -> 55,357), and "after" is precisely what
the run will hold at that moment of the week (before the week's growth).

The harvest week of a 6N fill is the first later scripted harvest of that
tank, else the earliest week its depuration hold allows (fill week + the
engine's hold, the same arithmetic as manual_window.sixn_release_schedule).
The planner may take it later; it can never take it earlier.
"""
from __future__ import annotations

import contextlib
import copy
import io
from dataclasses import dataclass, field
from typing import Optional

HARVEST_TYPES = ("harvest", "graded_harvest")


@dataclass
class TankLine:
    tank_id: int
    location: str
    system_id: str
    role: str                      # "source" | "destination" | "6N" | ...
    before_batch: Optional[str]
    before_count: float
    before_avg_wt_g: float
    before_biomass_kg: float
    after_batch: Optional[str]
    after_count: float
    after_avg_wt_g: float
    after_biomass_kg: float
    volume_m3: float
    cap_kg_m3: float

    @property
    def before_density(self) -> float:
        return self.before_biomass_kg / self.volume_m3 if self.volume_m3 else 0.0

    @property
    def after_density(self) -> float:
        return self.after_biomass_kg / self.volume_m3 if self.volume_m3 else 0.0

    @property
    def after_pct_cap(self) -> Optional[float]:
        return (100.0 * self.after_density / self.cap_kg_m3
                if self.cap_kg_m3 else None)

    @property
    def over_cap(self) -> bool:
        return bool(self.cap_kg_m3) and self.after_density > self.cap_kg_m3 + 1e-9


@dataclass
class SixNLine:
    tank_id: int
    location: str
    count_after: float
    harvest_week: str              # ISO label
    harvest_week_index: int        # 1-based window week
    scripted: bool                 # True = a scripted harvest of this tank
    in_window: bool                # True = that week runs your script
    limit: Optional[float]         # max_harvest_per_week in force that week
    floor: Optional[float]         # min_harvest_per_week in force that week

    @property
    def over_limit(self) -> bool:
        return self.limit is not None and self.count_after > self.limit + 0.5


@dataclass
class HarvestCheck:
    week: str
    scripted_before: float         # this week's scripted harvest before the event
    this_event: float
    floor: Optional[float]
    limit: Optional[float]

    @property
    def total(self) -> float:
        return self.scripted_before + self.this_event


@dataclass
class EventPreview:
    week: str
    tanks: list = field(default_factory=list)       # [TankLine]
    harvested_count: float = 0.0
    harvested_biomass_kg: float = 0.0
    from_fw_count: float = 0.0                      # fw_to_og: fish taken out of FW
    culled_count: float = 0.0                       # fw_to_og bottom cull
    sixn: list = field(default_factory=list)        # [SixNLine]
    harvest: Optional[HarvestCheck] = None
    warnings: list = field(default_factory=list)    # the run's own messages
    refused: bool = False

    @property
    def fish_in(self) -> float:
        return sum(t.before_count for t in self.tanks) + self.from_fw_count

    @property
    def fish_out(self) -> float:
        return (sum(t.after_count for t in self.tanks) + self.harvested_count
                + self.culled_count)

    @property
    def d_fish(self) -> float:
        """Fish not accounted for (after + harvested + culled - before - from FW).
        Negative = handling mortality the run applies on the move."""
        return self.fish_out - self.fish_in

    @property
    def d_biomass_kg(self) -> float:
        """For moves within OG/6N only (no FW intake): kg after + harvested -
        kg before. FW intakes bring biomass in from freshwater, so the caller
        should not show this for them."""
        return (sum(t.after_biomass_kg for t in self.tanks)
                + self.harvested_biomass_kg
                - sum(t.before_biomass_kg for t in self.tanks))


def _quiet(fn, *a, **k):
    """The event handlers print progress lines for the run's console log; a
    preview must not spam the app's console on every rerun."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **k)


def state_before_event(state, ctx, events, wk):
    """(FacilityState, week_start_date, week_label, fw_lookup, consumed_fw) at
    the point in window week `wk` (1-based) where a NEW event would apply --
    the run's own replay. `state` is the PR-hydrated state; it is not mutated.
    `ctx` is the editor context (forecast_start, control, tables, batch_by_id,
    pr_closing, fw_records)."""
    from .manual_events import TYPE_FW_TO_OG, apply_events_for_week
    from .manual_window import (_build_fw_lookup, _freeze_purge_6n,
                                advance_facility_window)
    from .time_grid import week_label, week_start
    fs = ctx["forecast_start"]
    control = ctx.get("control")
    sc = copy.deepcopy(state)
    earlier = [e for e in events if (e.week or 1) < wk]
    if wk > 1:
        _quiet(advance_facility_window, sc, ctx["batch_by_id"], ctx["tables"],
               fs, wk - 1, events=earlier, control=control,
               pr_closing=ctx.get("pr_closing"), fw_records=ctx.get("fw_records"))
    ws = week_start(wk - 1, fs)
    lbl = week_label(wk - 1, fs)
    # The window freezes occupied 6N tanks at each week's start (purge mode);
    # the replay above restored them at its handoff, so re-apply week wk's.
    _freeze_purge_6n(sc, control, ws)
    fw_lookup = _build_fw_lookup(events, ctx.get("fw_records"), control,
                                 ctx.get("pr_closing"), ctx["tables"],
                                 ctx["batch_by_id"])
    consumed: dict = {}
    for i, e in enumerate(events, 1):
        if e.type == TYPE_FW_TO_OG and e.batch and (e.week or 1) < wk:
            consumed.setdefault(e.batch, (i, e.week or 1))
    hf = ((control.handling_mortality_pct / 100.0)
          if control is not None else 0.0)
    _quiet(apply_events_for_week, sc, events, wk, ws, week_label=lbl,
           handling_frac=hf, fw_lookup=fw_lookup, consumed_fw=consumed)
    return sc, ws, lbl, fw_lookup, consumed


def _snap(t):
    if t is None or t.is_empty:
        return (None, 0.0, 0.0, 0.0)
    return (t.batch_id, float(t.count), float(t.avg_wt_g), float(t.biomass_kg))


def _event_tanks(ev):
    """(tank_id, role) for every tank `ev` touches, source first."""
    out = []
    if ev.from_tank is not None:
        out.append((int(ev.from_tank), "source"))
    for i, d in enumerate(ev.destinations or []):
        role = "destination"
        if ev.type == "graded_harvest":
            role = "pickup (biggest)" if i == 0 else "smaller fish"
        elif ev.type == "fw_to_og":
            role = f"intake ({d.size_class})" if d.size_class else "intake"
        out.append((int(d.tank), role))
    seen, uniq = set(), []
    for tid, role in out:
        if tid not in seen:
            seen.add(tid)
            uniq.append((tid, role))
    return uniq


def harvest_week_of(tank_id, fill_wk, events, hold_weeks=None):
    """(1-based week, scripted?) the 6N tank filled in window week `fill_wk` is
    harvested: the first scripted harvest of that tank after the fill, else the
    earliest week the depuration hold allows (fill + hold, as
    manual_window.sixn_release_schedule times a scripted fill)."""
    if hold_weeks is None:
        from .global_planner_poc import _PURGE_HOLD_WEEKS as hold_weeks
    later = sorted((e.week or 1) for e in events
                   if e.type in HARVEST_TYPES and e.from_tank is not None
                   and int(e.from_tank) == int(tank_id)
                   and (e.week or 1) > fill_wk)
    if later:
        return later[0], True
    return fill_wk + int(hold_weeks), False


def resolve_limits(week_lbl, facility_limits, control):
    """(floor, limit) in force for a week: the per-week scenario row, else the
    Control default -- caps.resolve_facility_cap, the run's own resolver."""
    from .caps import METRIC_MAX_HARVEST, METRIC_MIN_HARVEST, resolve_facility_cap
    if control is None:
        return None, None
    if facility_limits is None:
        return (float(control.min_harvest_per_week or 0) or None,
                float(control.max_harvest_per_week or 0) or None)
    return (resolve_facility_cap(METRIC_MIN_HARVEST, week_lbl, facility_limits, control),
            resolve_facility_cap(METRIC_MAX_HARVEST, week_lbl, facility_limits, control))


def preview_event(base, ev, ctx, events, facility_limits=None,
                  scripted_harvest_by_week=None, window_weeks=None):
    """EventPreview for adding `ev` (week ev.week) on top of `events`.

    `base` = state_before_event(...) for ev.week. `scripted_harvest_by_week`
    maps ISO label -> fish the window already harvests that week (from the
    editor's projection); `window_weeks` = the scripted window length the run
    will use (last scripted week, counting `ev`)."""
    from .manual_events import is_staged_graded, apply_events_for_week
    from .sixn import SIXN_ALL_TANKS
    from .time_grid import week_label
    sc, ws, lbl, fw_lookup, consumed = base
    control = ctx.get("control")
    fs = ctx["forecast_start"]
    wk = ev.week or 1
    after = copy.deepcopy(sc)
    hf = ((control.handling_mortality_pct / 100.0)
          if control is not None else 0.0)
    if ev.type == "fw_to_og":
        # The lookup indexes only the FW batches the events name, so the
        # candidate intake must be in it (the run builds it from every event).
        from .manual_window import _build_fw_lookup
        fw_lookup = _build_fw_lookup(list(events) + [ev], ctx.get("fw_records"),
                                     control, ctx.get("pr_closing"),
                                     ctx["tables"], ctx["batch_by_id"])
    culls: list = []
    tr, hv, tn, warns, fwb = _quiet(
        apply_events_for_week, after, [ev], wk, ws, week_label=lbl,
        handling_frac=hf, fw_lookup=fw_lookup, out_week_culls=culls,
        consumed_fw=dict(consumed))
    p = EventPreview(week=lbl, warnings=list(warns))
    p.refused = any("REFUSED" in w for w in warns)
    for tid, role in _event_tanks(ev):
        b, a = sc.tanks_by_id.get(tid), after.tanks_by_id.get(tid)
        if b is None:
            continue
        bb, bn, bw, bkg = _snap(b)
        ab, an, aw, akg = _snap(a)
        p.tanks.append(TankLine(
            tank_id=tid, location=b.location_id, system_id=b.system_id,
            role=("6N" if tid in SIXN_ALL_TANKS and role == "destination" else role),
            before_batch=bb, before_count=bn, before_avg_wt_g=bw,
            before_biomass_kg=bkg, after_batch=ab, after_count=an,
            after_avg_wt_g=aw, after_biomass_kg=akg,
            volume_m3=float(b.volume_m3 or 0.0),
            cap_kg_m3=float(b.max_density_kg_m3 or 0.0)))
    p.harvested_count = sum(float(h.count) for h in hv)
    p.harvested_biomass_kg = sum(float(h.count) * float(h.avg_wt_g) / 1000.0
                                 for h in hv)
    for _b, (fw_n, culled) in (fwb or {}).items():
        p.from_fw_count += float(fw_n)
        p.culled_count += float(culled)

    sh = scripted_harvest_by_week or {}
    # 6N tanks this event leaves holding fish (not drained in-week).
    in_week_harvest = (ev.type == "graded_harvest" and not is_staged_graded(ev))
    if not in_week_harvest and ev.type != "harvest":
        for t in p.tanks:
            if (t.tank_id in SIXN_ALL_TANKS and t.role != "source"
                    and t.after_count > t.before_count + 0.5):
                hw, scripted = harvest_week_of(t.tank_id, wk, events + [ev])
                hl = week_label(hw - 1, fs)
                fl_, lim = resolve_limits(hl, facility_limits, control)
                p.sixn.append(SixNLine(
                    tank_id=t.tank_id, location=t.location,
                    count_after=t.after_count, harvest_week=hl,
                    harvest_week_index=hw, scripted=scripted,
                    in_window=(window_weeks is not None and hw <= window_weeks),
                    limit=lim, floor=fl_))
    if hv:
        fl_, lim = resolve_limits(lbl, facility_limits, control)
        p.harvest = HarvestCheck(week=lbl, scripted_before=float(sh.get(lbl, 0.0)),
                                 this_event=p.harvested_count, floor=fl_, limit=lim)
    return p


def staged_6n_summary(events, transfers, close_rows, labels, forecast_start,
                      facility_limits, control):
    """Every 6N fill the scripted window makes, with the count the tank holds
    at the end of its fill week and the limit of the week it comes out --
    [(fill_label, tank_id, count, harvest_label, scripted, limit)], fill order.

    `transfers` = the window projection's moves ({"week","src","dests",...});
    `close_rows` = its end-of-week BatchLocations snapshot."""
    from .sixn import SIXN_ALL_TANKS
    from .time_grid import week_label
    close = {(x.week_label, x.tank_id): x.count for x in close_rows
             if getattr(x, "count", 0) > 0}
    seen, out = set(), []
    for m in transfers:
        if m["week"] not in labels:
            continue
        wk = labels.index(m["week"]) + 1
        for d in m["dests"]:
            if d not in SIXN_ALL_TANKS or (m["week"], d) in seen:
                continue
            seen.add((m["week"], d))
            cnt = close.get((m["week"], d), 0.0)
            if cnt <= 0:
                continue     # drained in its own week (in-week graded harvest)
            hw, scripted = harvest_week_of(d, wk, events)
            hl = week_label(hw - 1, forecast_start)
            _fl, lim = resolve_limits(hl, facility_limits, control)
            out.append((m["week"], d, cnt, hl, scripted, lim))
    return out
