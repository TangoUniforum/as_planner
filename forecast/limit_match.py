"""Limit-matching helpers for the manual window: the count that lands a 6N
tank, or a week's scripted harvest, ON the per-week limit.

WHY (operator, 2026-10-03): with the October limits pinned at min = max =
51,500 fish a week, the before/after previews (forecast/manual_preview.py) said
WHEN a scripted entry missed -- tank 63 filled in W44 with 55,357 fish, 3,857
over W46's limit -- but the operator still had to work out the right count by
hand, through two weeks of purge mortality. "Give it a try": the editor offers
the number instead.

READ-ONLY, like manual_preview. Nothing here changes a plan; a helper only
PROPOSES a count, the operator applies it. Every number comes from the window
engine the run executes (manual_window.advance_facility_window) -- the count
"at harvest" is that engine's own projection of the tank through the weeks
between fill and harvest (frozen off-feed in 6N, mortality only), never a
hand-tuned survival constant.

How a count is solved. Fish move, die and are harvested in proportion to the
count sent, so the measured quantity (a tank's count at its harvest week, or a
week's scripted harvest) is linear in the count to within float noise. Two
projections fix the line (count 0 = the entry left out, count = all the source
holds), its root is the candidate, and further projections walk the candidate
down until the projected value is not above the target and then up while the
next fish still fits -- so a proposed count NEVER lands above the limit.

Which week. A 6N tank's harvest week is its first later scripted harvest
(manual_preview.harvest_week_of); unscripted, the planner can take it no
earlier than the end of the depuration hold AND no earlier than the handoff
week (the window harvests only what is scripted). When the harvest is
scripted, other tanks harvested in that same week share its limit, so the
target is the WEEK's total; otherwise the tank alone is matched to the limit.
"""
from __future__ import annotations

import contextlib
import copy
import io
import math
from dataclasses import dataclass, field
from typing import Callable, Optional

FILL_TYPES = ("og_to_6n", "graded_harvest")


def _quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **k)


def project(state, ctx, events, n_weeks):
    """The run's window over `events` for `n_weeks` on a COPY of `state`."""
    from .manual_window import advance_facility_window
    sc = copy.deepcopy(state)
    win = _quiet(advance_facility_window, sc, ctx["batch_by_id"], ctx["tables"],
                 ctx["forecast_start"], n_weeks, events=list(events),
                 control=ctx.get("control"), pr_closing=ctx.get("pr_closing"),
                 fw_records=ctx.get("fw_records"))
    return sc, win


def _refusals(win) -> int:
    return sum(1 for w in (win.get("warnings") or []) if "REFUSED" in str(w))


def week_harvest(ctx, win, wk) -> float:
    """Fish the projected window harvests in window week `wk` (1-based)."""
    from .time_grid import week_start
    d = week_start(wk - 1, ctx["forecast_start"])
    return sum(float(h.count) for h in (win.get("harvest_events") or [])
               if getattr(h, "event_date", None) == d)


def window_length(events) -> int:
    return max([e.week or 1 for e in events] or [0])


def harvest_week(tank_id, fill_wk, events, hold_weeks=None):
    """(1-based week, scripted?) the 6N tank filled in `fill_wk` comes out:
    the first later scripted harvest of it, else max(end of the depuration
    hold, the handoff week) -- see the module docstring."""
    from .manual_preview import harvest_week_of
    hw, scripted = harvest_week_of(tank_id, fill_wk, events, hold_weeks)
    if not scripted:
        hw = max(hw, window_length(events) + 1)
    return hw, scripted


def count_at_harvest(state, ctx, events, tank_id, fill_wk):
    """(fish, week index, week label, scripted?) -- what 6N `tank_id`, filled
    in window week `fill_wk`, holds at the OPEN of the week it comes out: the
    count the run drains, after purge mortality (and any later top-up) --
    not its count at the moment of the fill."""
    from .time_grid import week_label
    hw, scripted = harvest_week(tank_id, fill_wk, events)
    sc, _w = project(state, ctx, events, hw - 1)
    t = sc.tanks_by_id.get(int(tank_id))
    n = float(t.count) if (t is not None and not t.is_empty) else 0.0
    return n, hw, week_label(hw - 1, ctx["forecast_start"]), scripted


@dataclass
class Solve:
    """A proposed count and what it does."""
    count: float                 # the proposed count (whole fish)
    target: float                # the value aimed at (limit or floor)
    kind: str                    # "limit" | "floor"
    week_label: str              # the week the target applies to
    week_index: int              # 1-based
    measure: str                 # "tank" (tank alone at harvest) | "week" (week's total)
    tank_id: Optional[int]
    before: float                # measured value with the entry left out (count 0)
    with_current: Optional[float]  # measured value at the entry's current count
    after: float                 # measured value at the proposed count
    n_max: float                 # most the entry can carry (source holds)
    clamped: bool                # True = n_max reached before the target
    ok: bool = True
    note: str = ""
    warnings: list = field(default_factory=list)

    @property
    def gap(self) -> float:
        """Target minus what the proposal reaches (>0 = still short)."""
        return self.target - self.after


def _solve_linear(metric: Callable[[float], float], target: float, n_max: float,
                  refused: Callable[[float], bool] = lambda n: False,
                  max_iter: int = 12):
    """(count, value_at_count, value_at_0, clamped) -- the largest WHOLE count
    in [0, n_max] whose measured value is not above `target`, for a measured
    value that rises with the count. None when the count has no effect."""
    n_max = math.floor(n_max + 1e-6)
    m0 = metric(0)
    if n_max <= 0:
        return 0.0, m0, m0, True
    m1 = metric(n_max)
    slope = (m1 - m0) / n_max
    if slope <= 1e-9:
        return None
    if m1 <= target + 1e-6 and not refused(n_max):
        return float(n_max), m1, m0, m1 < target - 1.0
    n = max(0, min(n_max, math.floor((target - m0) / slope)))
    val = metric(n)
    it = 0
    while n > 0 and val > target + 1e-6 and it < max_iter:
        n = max(0, n - max(1, math.ceil((val - target) / slope)))
        val = metric(n)
        it += 1
    if n > 0 and refused(n):
        # The run refuses this count (e.g. it empties a source a LATER entry
        # still draws on): the largest accepted count below it. 0 = the entry
        # left out, accepted by definition.
        lo, hi = 0, n
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if refused(mid):
                hi = mid
            else:
                lo = mid
        n = lo
        val = metric(n)
    it = 0
    while n + 1 <= n_max and it < max_iter:
        v1 = metric(n + 1)
        if v1 > target + 1e-6 or refused(n + 1):
            break
        n, val = n + 1, v1
        it += 1
    # clamped = the entry can't reach the target within what it may carry.
    return float(n), val, m0, val < target - 1.0


def _metric_factory(state, ctx, build: Callable[[float], list], tank_id,
                    hw: int, measure: str):
    """metric(n) and refused(n) for the event list `build(n)`."""
    cache: dict = {}

    def run(n):
        n = float(n)
        if n not in cache:
            evs = build(n)
            if measure == "week":
                _sc, win = project(state, ctx, evs, hw)
                val = week_harvest(ctx, win, hw)
            else:
                sc, win = project(state, ctx, evs, hw - 1)
                t = sc.tanks_by_id.get(tank_id)
                val = float(t.count) if (t is not None and not t.is_empty) else 0.0
            cache[n] = (val, _refusals(win), win)
        return cache[n]

    base_ref = {}

    def metric(n):
        return run(n)[0]

    def refused(n):
        if "r" not in base_ref:
            base_ref["r"] = run(0)[1]
        return run(n)[1] > base_ref["r"]

    return metric, refused, run


def _limits_at(ctx, facility_limits, wk):
    from .manual_preview import resolve_limits
    from .time_grid import week_label
    lbl = week_label(wk - 1, ctx["forecast_start"])
    floor, lim = resolve_limits(lbl, facility_limits, ctx.get("control"))
    return lbl, floor, lim


# ---------------------------------------------------------------- events ---

def with_count(ev, n, tank_id=None):
    """A copy of `ev` carrying count `n` (into `tank_id` for a transfer)."""
    e = copy.deepcopy(ev)
    if e.type in ("og_to_6n", "og_transfer"):
        dests = e.destinations or []
        tgt = next((d for d in dests if tank_id is None or int(d.tank) == int(tank_id)),
                   dests[0] if dests else None)
        if tgt is not None:
            tgt.count = float(n)
        if len(dests) == 1:
            e.count = float(n)
        else:
            known = [d.count for d in dests]
            e.count = (float(sum(known)) if all(c is not None for c in known) else None)
    else:
        e.count = float(n)
    return e


def entry_count(ev, tank_id=None) -> Optional[float]:
    """The count an entry carries, None = 'whole tank / remainder'."""
    if ev.type in ("og_to_6n", "og_transfer"):
        for d in ev.destinations or []:
            if tank_id is None or int(d.tank) == int(tank_id):
                return d.count
        return None
    return ev.count


def _source_count_at(state, ctx, events, ev) -> float:
    """Fish in the entry's source tank at the moment it runs (the run's replay,
    earlier same-week entries included) -- the most the entry can move."""
    from .manual_preview import state_before_event
    rest = [e for e in events if e is not ev]
    same_before = []
    for e in events:
        if e is ev:
            break
        if (e.week or 1) == (ev.week or 1):
            same_before.append(e)
    others = [e for e in rest if (e.week or 1) != (ev.week or 1)] + same_before
    sc = state_before_event(state, ctx, others, ev.week or 1)[0]
    t = sc.tanks_by_id.get(int(ev.from_tank)) if ev.from_tank is not None else None
    if t is None or t.is_empty:
        return 0.0
    return float(t.count)


# --------------------------------------------------------------- helpers ---

def fill_to_limit(state, ctx, events, make_event: Callable[[float], object],
                  tank_id: int, wk: int, source_count: float,
                  facility_limits=None, graded: bool = False) -> Optional[Solve]:
    """The count a NEW 6N fill into `tank_id` in window week `wk` should send so
    the tank -- or, when its harvest is scripted, the week's harvest -- lands on
    the processing limit of the week it comes out.

    `make_event(n)` builds the candidate event for count n; `events` is the
    working set WITHOUT it. A graded pick (the biggest N) must leave fish
    behind, so its ceiling is one fish under the source."""
    probe = make_event(max(1.0, source_count))
    evs_probe = list(events) + [probe]
    hw, scripted = harvest_week(tank_id, wk, evs_probe)
    in_week = (getattr(probe, "type", "") == "graded_harvest"
               and str(getattr(probe, "mode", "")).lower() == "harvest")
    if in_week:
        hw, scripted = wk, True
    lbl, floor, lim = _limits_at(ctx, facility_limits, hw)
    if not lim:
        return None
    measure = "week" if scripted else "tank"
    n_max = max(0.0, source_count - (1.0 if graded else 0.0))

    def build(n):
        return list(events) if n <= 0 else list(events) + [make_event(n)]

    metric, refused, _run = _metric_factory(state, ctx, build, tank_id, hw, measure)
    res = _solve_linear(metric, lim, n_max, refused)
    if res is None:
        return Solve(0.0, lim, "limit", lbl, hw, measure, tank_id, metric(0), None,
                     metric(0), n_max, False, ok=False,
                     note="this entry does not reach that week's harvest")
    n, val, m0, clamped = res
    s = Solve(n, lim, "limit", lbl, hw, measure, tank_id, m0, None, val, n_max,
              clamped)
    s.ok = n > 0 and not refused(n)
    if m0 >= lim - 0.5:
        s.ok, s.note = False, (f"already at/over {lbl}'s {lim:,.0f} limit "
                               f"without this fill ({m0:,.0f})")
    elif clamped:
        s.note = (f"the source holds only {n_max:,.0f} — {lim - val:,.0f} short "
                  f"of the limit even sending all of it")
    return s


def room_to_limit(state, ctx, events, make_event, tank_id, wk, source_count,
                  facility_limits=None, graded=False) -> Optional[Solve]:
    """Alias kept for the editor's 'Room left' caption on a 6N fill: the same
    solve as fill_to_limit (room = the count it proposes)."""
    return fill_to_limit(state, ctx, events, make_event, tank_id, wk,
                         source_count, facility_limits, graded)


def harvest_room(harvest_by_week: dict, week_lbl: str, floor, limit,
                 this_count: float = 0.0):
    """(to_floor, to_limit) for a week's scripted harvest plus `this_count`:
    fish still missing to reach the floor (0 when met) and fish left under the
    limit (negative = over)."""
    tot = float(harvest_by_week.get(week_lbl, 0.0)) + float(this_count or 0.0)
    to_floor = max(0.0, (floor or 0.0) - tot) if floor else None
    to_limit = (limit - tot) if limit else None
    return to_floor, to_limit


def density_room_fish(tank, avg_wt_g: float) -> Optional[float]:
    """Fish of `avg_wt_g` the OG tank can still take before its density cap
    (negative = already over). None if the tank has no cap/volume."""
    cap = float(getattr(tank, "max_density_kg_m3", 0) or 0)
    vol = float(getattr(tank, "volume_m3", 0) or 0)
    if not cap or not vol or not avg_wt_g:
        return None
    have = 0.0 if tank.is_empty else float(tank.biomass_kg)
    return (cap * vol - have) * 1000.0 / float(avg_wt_g)


# ----------------------------------------------------------- suggestions ---

@dataclass
class Suggestion:
    row: str                     # "week" | "6N"
    week_label: str              # the week judged (harvest week)
    tank_id: Optional[int]       # the 6N tank (6N rows) / tank whose fill is changed
    entry_index: Optional[int]   # index into the events list of the entry to edit
    entry_label: str             # human name of that entry
    current: Optional[float]     # its current count (None = whole tank)
    proposed: Optional[float]    # proposed count; None = no single entry can fix it
    solve: Optional[Solve] = None
    message: str = ""
    same_as_6n: bool = False      # a week row whose fix is a 6N row's fix


def _describe(ev, i) -> str:
    from .time_grid import week_label  # noqa: F401  (label added by caller)
    dests = ",".join(str(d.tank) for d in (ev.destinations or []))
    if ev.type == "harvest":
        return f"harvest #{i + 1} of {ev.from_tank}"
    if ev.type == "graded_harvest":
        return f"graded→6N #{i + 1} {ev.from_tank}→{dests}"
    return f"{ev.type} #{i + 1} {ev.from_tank}→{dests}"


def _fills_of(events, tank_id, before_wk, after_wk=0):
    """Indices of entries that put fish in 6N `tank_id` in weeks
    (after_wk, before_wk) -- og_to_6n into it, or a staged graded pickup."""
    from .manual_events import is_staged_graded
    out = []
    for i, e in enumerate(events):
        w = e.week or 1
        if not (after_wk < w < before_wk):
            continue
        if e.type == "og_to_6n" and any(int(d.tank) == int(tank_id)
                                        for d in e.destinations or []):
            out.append(i)
        elif (e.type == "graded_harvest" and e.destinations
              and int(e.destinations[0].tank) == int(tank_id) and is_staged_graded(e)):
            out.append(i)
    return out


def _prev_harvest_wk(events, tank_id, before_wk):
    ws = [e.week or 1 for e in events
          if e.type in ("harvest", "graded_harvest") and e.from_tank is not None
          and int(e.from_tank) == int(tank_id) and (e.week or 1) < before_wk]
    return max(ws) if ws else 0


def solve_entry(state, ctx, events, idx, hw, measure, tank_id, target, kind,
                lbl) -> Optional[Solve]:
    """The count for entry `idx` that brings the measure (tank alone at the
    open of window week `hw`, or week `hw`'s total harvest) to `target` --
    never above it for kind 'limit', the least that reaches it for 'floor'."""
    ev = events[idx]
    fill_tank = None
    if ev.type == "og_to_6n":
        fill_tank = tank_id if any(int(d.tank) == int(tank_id)
                                   for d in ev.destinations or []) else \
            int(ev.destinations[0].tank)
    graded = ev.type == "graded_harvest"
    if ev.type == "harvest":
        # A harvest can take at most what its tank holds when it runs.
        n_max = _source_count_at(state, ctx, events, ev)
    else:
        n_max = _source_count_at(state, ctx, events, ev) - (1.0 if graded else 0.0)
    cur = entry_count(ev, fill_tank)

    def build(n):
        evs = list(events)
        if n <= 0:
            evs.pop(idx)
        else:
            evs[idx] = with_count(ev, n, fill_tank)
        return evs

    metric, refused, run = _metric_factory(state, ctx, build, tank_id, hw, measure)
    current_val = run(cur if cur is not None else n_max)[0]
    if kind == "limit":
        res = _solve_linear(metric, target, n_max, refused)
        if res is None:
            return None
        n, val, m0, clamped = res
    else:
        # floor: the LEAST count reaching the target = one past the largest
        # count staying below it.
        res = _solve_linear(metric, target - 1.0, n_max, refused)
        if res is None:
            return None
        n, val, m0, _c = res
        if (n + 1 <= math.floor(n_max + 1e-6) and not refused(n + 1)
                and metric(n + 1) <= target + 1.0):
            n, val = n + 1, metric(n + 1)
        clamped = val < target - 1.0
    s = Solve(n, target, kind, lbl, hw, measure, tank_id, m0, current_val, val,
              n_max, clamped)
    s.ok = not refused(n)
    return s


def window_suggestions(state, ctx, events, facility_limits=None):
    """For each scripted week whose harvest is off its floor/limit and each 6N
    fill that will hold more than its harvest week's limit: the one entry edit
    that lands it on target, or a reason no single entry can.

    Week rows prefer the FILL of a tank harvested that week (the operator's
    lever: how much goes into 6N), then a partial-count harvest; 6N rows edit
    the last fill into that tank. Never applied here -- the editor offers them."""
    from .sixn import SIXN_ALL_TANKS
    n = window_length(events)
    if not n:
        return []
    _sc, win = project(state, ctx, events, n)
    out: list[Suggestion] = []

    # ---- 6N fills vs the limit of the week they come out ----
    seen_tanks = set()
    for i in sorted(range(len(events)), key=lambda k: -(events[k].week or 1)):
        e = events[i]
        tanks = []
        if e.type == "og_to_6n":
            tanks = [int(d.tank) for d in e.destinations or []
                     if int(d.tank) in SIXN_ALL_TANKS]
        elif e.type == "graded_harvest" and e.destinations:
            from .manual_events import is_staged_graded
            if is_staged_graded(e):
                tanks = [int(e.destinations[0].tank)]
        for t in tanks:
            fw = e.week or 1
            key = (t, fw)
            if key in seen_tanks:
                continue
            seen_tanks.add(key)
            # The last fill of this tank in this week is the lever.
            last = max(k for k in _fills_of(events, t, fw + 1, fw - 1))
            hw, scripted = harvest_week(t, fw, events)
            lbl, floor, lim = _limits_at(ctx, facility_limits, hw)
            if not lim:
                continue
            # The TANK ALONE against its harvest week's limit (a 6N tank drains
            # whole). A scripted week shared with other tanks is the week
            # rows' business -- judging the fill on the week's total flagged
            # tanks that fit on their own.
            measure = "tank"
            s2, _w2 = project(state, ctx, events, hw - 1)
            tk = s2.tanks_by_id.get(t)
            val = float(tk.count) if (tk is not None and not tk.is_empty) else 0.0
            if val <= lim + 0.5:
                continue
            ev = events[last]
            name = _describe(ev, last)
            sol = solve_entry(state, ctx, events, last, hw, measure, t, lim,
                              "limit", lbl)
            sug = Suggestion("6N", lbl, t, last, name, entry_count(ev, t), None,
                             sol)
            if _improves(sol, "limit"):
                sug.proposed = sol.count
                sug.message = (f"{name} (W{ev.week}): {_fmt(entry_count(ev, t))} → "
                               f"{sol.count:,.0f} — tank {t} then holds "
                               f"{sol.after:,.0f} at {lbl} (limit {lim:,.0f})")
            else:
                sug.message = (f"{name}: no count of this entry brings tank {t} "
                               f"under {lbl}'s {lim:,.0f} limit — move fish to "
                               f"another 6N tank")
            out.append(sug)

    # ---- window weeks: scripted harvest vs floor / limit ----
    for wk in range(1, n + 1):
        lbl, floor, lim = _limits_at(ctx, facility_limits, wk)
        tot = week_harvest(ctx, win, wk)
        if lim and tot > lim + 0.5:
            kind, target = "limit", lim
        elif floor and tot < floor - 0.5:
            kind, target = "floor", floor
        else:
            continue
        hv = [i for i, e in enumerate(events)
              if (e.week or 1) == wk and e.type == "harvest" and e.from_tank is not None]
        cands = []
        for i in hv:
            t = int(events[i].from_tank)
            prev = _prev_harvest_wk(events, t, wk)
            fills = _fills_of(events, t, wk, prev)
            if fills:
                cands.append((max(fills), t))
        sug = Suggestion("week", lbl, None, None, "", None, None)
        sol = None
        for fi, t in sorted(cands, key=lambda c: -c[0]):
            s = solve_entry(state, ctx, events, fi, wk, "week", t, target, kind, lbl)
            if not _improves(s, kind):
                continue
            if kind == "floor" and s.after < target - 0.5:
                if sol is None or s.after > sol.after:
                    sol, sug.entry_index, sug.tank_id = s, fi, t
                continue
            sol, sug.entry_index, sug.tank_id = s, fi, t
            break
        if sol is None:
            # Fallback: a harvest with an explicit count, or a whole-tank
            # harvest over the limit can be cut to a partial count.
            for i in hv:
                e = events[i]
                if kind == "floor" and e.count is None:
                    continue
                s = solve_entry(state, ctx, events, i, wk, "week",
                                int(e.from_tank), target, kind, lbl)
                if _improves(s, kind) and (kind == "limit" or s.after >= target - 0.5):
                    sol, sug.entry_index, sug.tank_id = s, i, int(e.from_tank)
                    break
        sug.solve = sol
        dup = next((o for o in out if o.row == "6N" and sol is not None
                    and o.entry_index == sug.entry_index
                    and o.proposed is not None and abs(o.proposed - sol.count) < 0.5),
                   None)
        if sol is not None and sug.entry_index is not None and sol.count > 0:
            ev = events[sug.entry_index]
            sug.entry_label = _describe(ev, sug.entry_index)
            sug.current = entry_count(ev, sug.tank_id if ev.type == "og_to_6n" else None)
            sug.proposed = sol.count
            reach = (f"reaches {sol.after:,.0f}" if sol.after < target - 0.5
                     else f"= {sol.after:,.0f}")
            sug.message = (f"{sug.entry_label} (W{ev.week}): {_fmt(sug.current)} → "
                           f"{sol.count:,.0f} — {lbl} harvest {reach} "
                           f"({'floor' if kind == 'floor' else 'limit'} {target:,.0f})"
                           + (" — the most this entry can carry" if
                              (kind == "floor" and sol.after < target - 0.5) else "")
                           + (" (same change as the 6N row above)" if dup else ""))
            sug.same_as_6n = dup is not None
        else:
            short = (target - tot) if kind == "floor" else (tot - target)
            if kind == "floor":
                why = ("the window fills into its tanks already send all their "
                       "source holds" if cands else
                       "its tanks hold fish from the ProductionReport, not a "
                       "window fill")
                fix = "add a harvest of another ripe 6N tank"
            else:
                why, fix = "", "split the fill across two 6N tanks"
            sug.message = (f"{lbl}: {short:,.0f} "
                           f"{'under the floor' if kind == 'floor' else 'over the limit'}"
                           f" — no single scripted entry can change this week's "
                           f"harvest by that much" + (f" ({why})" if why else "")
                           + f"; {fix}")
        out.append(sug)
    # Earliest entry first: an edit can shift what LATER entries move (a fill
    # cut leaves fish in its source for a later whole-tank entry to carry), so
    # the list is meant to be applied from the top, re-checked after each.
    def _order(sg):
        wk_e = (events[sg.entry_index].week or 1) if sg.entry_index is not None else 99
        return (sg.proposed is None, wk_e, 0 if sg.row == "6N" else 1, sg.week_label)
    out.sort(key=_order)
    return out


def _improves(sol, kind) -> bool:
    """A proposal worth offering: run would accept it, and it moves the
    measure toward the target -- a limit breach ends at/under the limit; a
    floor miss ends closer to the floor (a whole fish at least)."""
    if sol is None or not sol.ok or sol.count <= 0:
        return False
    cur = sol.with_current
    if kind == "limit":
        return sol.after <= sol.target + 1e-6 and (cur is None or cur > sol.target + 0.5)
    return cur is None or sol.after > cur + 0.5


def _fmt(v) -> str:
    return "whole tank" if v is None else f"{v:,.0f}"


def apply_suggestion(events, sug: Suggestion):
    """A NEW events list with the suggestion's count written into its entry."""
    if sug.entry_index is None or sug.proposed is None:
        return list(events)
    evs = list(events)
    ev = evs[sug.entry_index]
    tank = sug.tank_id if ev.type in ("og_to_6n", "og_transfer") else None
    evs[sug.entry_index] = with_count(ev, sug.proposed, tank)
    return evs
