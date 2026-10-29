"""Least-moves layout as a pipeline step (opt-in: control `placement_method: least_moves`).

The normal run decides WHAT happens to every batch (fish per week, weights, arrivals, 6N staging, harvest). This
step keeps all of that and re-lays the grow-out tanks (OG1-OG5 + OG6S) with as few fish moves as possible, using
the least-moves solver (forecast/least_moves.py) on the run's own endpoint, before any report is written. Every
report of the run then describes the re-laid plan.

    endpoint()   the run's endpoint + its own layout, from the placement result (no workbook needed)
    relayout()   solve -> check -> rebuild the placement (tank rows, transfers, TranOG tanks, 6N staging sources,
                 per-tank biology) -> accept gates. Any failed gate keeps the normal plan, with the reason logged.

WHAT STAYS EXACTLY AS THE NORMAL PLAN: fish and biomass per batch per week, arrivals, 6N staging per batch-week,
everything in 6N, harvest. Mortality per batch-week stays the normal plan's too (the handling mortality of its
moves included - fewer moves are not credited with fewer deaths, which keeps harvest identical).
WHAT CHANGES: which tank holds which fish (every OG1-5/6S row), the OG-to-OG transfers, which tanks a TranOG
arrival lands in, and which tank each 6N staging leaves from. Fish in a tank carry the batch's average weight that
week (the normal plan's tanks can differ by grading), so per-system feed can differ slightly. A graded harvest is
6N staging here (its pickup leaves from the new tank; a retention leg to another tank is a move like any other).

NOT HANDLED: harvest straight from a grow-out tank (in-place purge, 6N production mode), fish moving out of 6N back
into grow-out, grade events touching 6N. The step then re-plans only the weeks BEFORE the first such week, and pins
its last re-planned week to the normal plan's own tanks so the weeks after join on unchanged. When fewer than
MIN_WEEKS weeks are left it stands aside and the normal plan is kept; the log says why.
"""
from __future__ import annotations

import collections
import copy

from . import least_moves as lm
from .caps import carry_forward_cap_lookup, og_density_cap
from .events import TankAllocation, Transfer
from .placement import BatchLocationRow
from .time_grid import iso_week_label

SIXN_SYS = 'OG6N'
CHANNEL = 'least_moves'
MIN_WEEKS = 4


def _wk(ev) -> str:
    return iso_week_label(ev.event_date)


def _ct(ev) -> float:
    return float(getattr(ev, 'count_transferred', 0) or 0)


def _legs(ev):
    """(dest TankAllocation, fish actually moved) per destination of a Transfer (refusals pro rata, as the audit)."""
    tot = sum(d.count for d in ev.destinations) or 1.0
    return [(d, _ct(ev) * d.count / tot) for d in ev.destinations]


def _split(ev, sys_of: dict):
    """A move out of a grow-out tank, as (legs into 6N [(dest allocation, fish, source weight g or None)], legs to
    other grow-out tanks [(tank, fish)]). A graded harvest's pickup is a 6N leg."""
    if hasattr(ev, 'pickup_tank_id'):
        cv = ev.pickup_cv_pct if getattr(ev, 'pickup_cv_pct', None) is not None else ev.cv_pct
        six = [(TankAllocation(tank_id=ev.pickup_tank_id, count=ev.pickup_count, avg_wt_g=ev.pickup_avg_wt_g,
                               cv_pct=cv), ev.pickup_count,
                getattr(ev, 'pickup_source_avg_wt_g', None) or ev.pickup_avg_wt_g)]
        og = [(ev.retention_tank_id, ev.retention_count)] if ev.retention_tank_id != ev.source_tank_id else []
        return six, og
    six, og = [], []
    for d, q in _legs(ev):
        if q <= 0 or d.tank_id == ev.source_tank_id:
            continue
        if sys_of.get(d.tank_id) == SIXN_SYS:
            six.append((d, q, getattr(ev, 'source_avg_wt_g', None)))
        else:
            og.append((d.tank_id, q))
    return six, og


# ------------------------------------------------------------------------------------------------ guards
def unsupported(placement, sys_of: dict) -> dict:
    """week -> Counter of the reasons this step cannot re-lay that week (weeks not listed are fine)."""
    why = collections.defaultdict(collections.Counter)
    for h in placement.harvest_events:
        if sys_of.get(h.source_tank_id) != SIXN_SYS:
            why[_wk(h)]['harvest straight from a grow-out tank'] += 1
    for ev in placement.transfer_events:
        w = _wk(ev)
        if hasattr(ev, 'pickup_tank_id'):
            if (sys_of.get(ev.source_tank_id) == SIXN_SYS or sys_of.get(ev.pickup_tank_id) != SIXN_SYS
                    or sys_of.get(ev.retention_tank_id) == SIXN_SYS):
                why[w]['graded harvest not from grow-out into 6N'] += 1
            continue
        if _ct(ev) > 0 and sys_of.get(ev.source_tank_id) == SIXN_SYS and any(
                sys_of.get(d.tank_id) != SIXN_SYS for d in ev.destinations):
            why[w]['fish moving out of 6N into grow-out'] += 1
    for g in placement.grade_events:
        if any(sys_of.get(t) == SIXN_SYS for t in list(g.source_tank_ids) + [d.tank_id for d in g.destinations]):
            why[_wk(g)]['grade event touching 6N'] += 1
    for r in placement.batch_locations:
        if r.system_id != SIXN_SYS and getattr(r, 'stage', '') == 'STARVE':
            why[r.week_label]['in-place purge in a grow-out tank'] += 1
    return why


# ------------------------------------------------------------------------------------------------ endpoint
def endpoint(placement, *, start_state, control, facility, facility_limits, system_limits, batch_by_id,
             tables, weeks=None, pin_last=False) -> dict:
    """The least-moves endpoint (the same dict least_moves.read_endpoint builds from a workbook), from the run's
    own placement result. Weeks = `weeks`, default every week the placement wrote (the manual window is not in it
    yet). pin_last: the last week must end in the normal plan's own tanks (D['end_layout'])."""
    from .biology import realized_feed_kg_day
    og = [t for t in facility.tanks if t.type == 'OG' and t.system_id != SIXN_SYS]
    sys_of = {t.tank_id: t.system_id for t in facility.tanks}
    W = sorted(weeks or {r.week_label for r in placement.batch_locations if r.count > 0})
    Wset = set(W)
    rows = [r for r in placement.batch_locations if r.count > 0 and r.week_label in Wset]
    cap = carry_forward_cap_lookup(system_limits)
    systems = sorted({t.system_id for t in og})

    n = collections.defaultdict(float); kg = collections.defaultdict(float); feed = collections.defaultdict(float)
    layout = collections.defaultdict(dict)
    for r in rows:
        if r.system_id == SIXN_SYS:
            continue
        key = (r.batch_id, r.week_label)
        n[key] += r.count; kg[key] += r.biomass_kg
        feed[key] += realized_feed_kg_day(r.avg_wt_g, r.biomass_kg, batch_by_id.get(r.batch_id), tables, r.week_label)
        layout[r.week_label][r.tank_id] = (r.batch_id, r.count, r.density_kg_m3)

    arr = collections.defaultdict(float); arr_kg = collections.defaultdict(float)
    for e in placement.tranog_events:
        if _wk(e) in Wset:
            for d in e.destinations:
                arr[(e.batch_id, _wk(e))] += d.count; arr_kg[(e.batch_id, _wk(e))] += d.count * d.avg_wt_g / 1000
    stage = collections.defaultdict(float)
    flow = collections.defaultdict(lambda: [0.0, 0.0, 0.0])          # (w, b, t) -> [in, out, staged] (engine)
    for ev in placement.transfer_events:
        w = _wk(ev)
        s = ev.source_tank_id
        if w not in Wset or sys_of.get(s) == SIXN_SYS:
            continue
        six, og_legs = _split(ev, sys_of)
        for _, q, _ in six:
            stage[(ev.batch_id, w)] += q; flow[(w, ev.batch_id, s)][2] += q
        for t, q in og_legs:
            flow[(w, ev.batch_id, s)][1] += q; flow[(w, ev.batch_id, t)][0] += q
    for g in placement.grade_events:                                  # OG-to-OG grade legs count as moves too
        w = _wk(g)
        if w not in Wset:
            continue
        srcs = list(g.source_tank_ids)
        for d in g.destinations:
            if d.tank_id in srcs:
                continue
            flow[(w, g.batch_id, d.tank_id)][0] += d.count
            for s in srcs:
                flow[(w, g.batch_id, s)][1] += d.count / len(srcs)
    harvested = collections.defaultdict(float)
    for h in placement.harvest_events:
        harvested[_wk(h)] += h.count

    start = {}
    for tid, tk in start_state.tanks_by_id.items():
        if not tk.is_empty and tk.count > 0 and sys_of.get(tid) != SIXN_SYS and tk.type == 'OG':
            start[str(tid)] = [tk.batch_id, float(tk.count)]
    batches = sorted({b for (b, w) in n} | {b for (b, w) in arr} | {v[0] for v in start.values()})
    D = {'source': 'this run', 'weeks': W, 'start_from': None,
         'tanks': sorted((t.tank_id, t.system_id, t.volume_m3, t.max_density_kg_m3) for t in og),
         'sys_cap': {}, 'sys_cap_w': {w: {s: (cap(w, s, 'biomass') or 0, cap(w, s, 'feed_per_day') or 0)
                                          for s in systems} for w in W},
         'buffer': float(getattr(control, 'global_buffer_pct', 0.05) or 0),
         'min_tank': float(getattr(control, 'min_tank_control', 0) or 0),
         'min_move': float(getattr(control, 'min_transfer_count', 0) or 0),
         'move_rules': bool(getattr(control, 'move_size_rules', False)),
         'max_moves': int(getattr(control, 'max_transfers_per_week', 15) or 0),
         'dens_cap': {w: og_density_cap(w, facility_limits) for w in W},
         'start_layout': start, 'batch': {}, 'harvested': {w: harvested[w] for w in W},
         'engine_moved': {w: sum(f[0] for (ww, b, t), f in flow.items() if ww == w) for w in W},
         'engine_layout': {w: {str(t): [v[0], v[1]] for t, v in layout[w].items()} for w in W}}
    if pin_last:                    # entry tanks as planned throughout, so the pinned last week is reachable
        D['end_layout'] = D['engine_layout'][W[-1]]
        D['fix_entry'] = True
    for b in batches:
        D['batch'][b] = {w: {'n': n[(b, w)], 'kg': kg[(b, w)],
                             'wt': (kg[(b, w)] / n[(b, w)]) if n[(b, w)] > 0 else
                             (arr_kg[(b, w)] / arr[(b, w)] if arr[(b, w)] > 0 else 0.0),
                             'arr': arr[(b, w)], 'stage': stage[(b, w)],
                             'sfr': (feed[(b, w)] / kg[(b, w)]) if kg[(b, w)] > 0 else 0.0}
                         for w in W}
    vol = {t.tank_id: t.volume_m3 for t in og}
    D['engine_plan'] = [{'week': w, 'rows': [
        {'b': b, 't': t, 'n': c, 'inn': flow[(w, b, t)][0], 'out': flow[(w, b, t)][1], 'st': flow[(w, b, t)][2],
         'dens': dn} for t, (b, c, dn) in layout[w].items()] + [
        {'b': b, 't': t, 'n': 0.0, 'inn': f[0], 'out': f[1], 'st': f[2], 'dens': 0.0}
        for (ww, b, t), f in list(flow.items()) if ww == w and layout[w].get(t, (None,))[0] != b and t in vol]}
        for w in W]
    return D


# ------------------------------------------------------------------------------------------------ rebuild
def _rebuild(placement, D: dict, plan: list[dict], start_state, facility, pin_last=False) -> tuple:
    """A copy of `placement` with the re-laid grow-out tanks. Returns (new placement, biology check dict)."""
    P, close = _layout(placement, D, plan, start_state, facility, pin_last)
    return _biology(placement, P, D, [p['week'] for p in plan], close, start_state, facility)


def _layout(placement, D: dict, plan: list[dict], start_state, facility, pin_last=False) -> tuple:
    """The re-laid tank rows, transfers, TranOG tanks and 6N staging sources. Returns (placement copy, close) with
    close[(tank, week)] = (batch, fish, kg) at the end of each re-laid week."""
    tk = {t.tank_id: t for t in facility.tanks}
    sys_of = {t.tank_id: t.system_id for t in facility.tanks}
    W = [p['week'] for p in plan]; Wset = set(W)
    B = D['batch']
    P = copy.copy(placement)
    pinned = W[-1] if pin_last else None             # this week's tank rows stay the normal plan's own

    # fish per unit, after each week (the solver's own unit count -> the plan's exact batch total)
    U = collections.defaultdict(float)
    for p in plan:
        for r in p['rows']:
            U[(r['b'], p['week'])] += r['u']
    s_units = collections.Counter(); s_n = collections.defaultdict(float)
    for t, (b, c) in D['start_layout'].items():
        s_n[b] += c
    for b in s_n:
        s_units[b] = max(1, round(s_n[b] / lm.UNIT))

    def c_after(b, w):
        return B[b][w]['n'] / U[(b, w)] if U[(b, w)] > 0 else None

    def c_before(b, i):                                   # fish per unit when this week's moves happen
        if i > 0:
            c = c_after(b, W[i - 1])
            if c:
                return c
        elif s_n.get(b):
            return s_n[b] / s_units[b]
        return c_after(b, W[i]) or lm.UNIT

    def wt_before(b, i):                                  # kg per fish when this week's moves happen
        if i > 0 and B[b][W[i - 1]]['n'] > 0:
            return B[b][W[i - 1]]['wt']
        if i == 0 and s_n.get(b):
            kg = sum(start_state.tanks_by_id[int(t)].biomass_kg for t, v in D['start_layout'].items() if v[0] == b)
            return kg / s_n[b]
        return B[b][W[i]]['wt']

    # --- tank rows
    keep = [r for r in placement.batch_locations
            if not (r.week_label in Wset and r.system_id != SIXN_SYS and r.week_label != pinned)]
    meta = {}
    for r in placement.batch_locations:
        if r.week_label in Wset:
            meta.setdefault(r.week_label, (r.week_start, r.days))
    new_rows = []
    close = {}                                                                 # (t, w) -> (b, n, kg)
    for r in placement.batch_locations:
        if r.week_label == pinned and r.system_id != SIXN_SYS and r.count > 0:
            close[(r.tank_id, pinned)] = (r.batch_id, r.count, r.biomass_kg)
    for p in plan:
        w = p['week']
        for r in p['rows']:
            if r['u'] <= 0 or w == pinned:
                continue
            b, t = r['b'], int(r['t'])
            c = r['u'] * c_after(b, w); wt = B[b][w]['wt']
            close[(t, w)] = (b, c, c * wt)
            ws, days = meta.get(w, (None, 7.0))
            new_rows.append(BatchLocationRow(
                week_label=w, week_start=ws, batch_id=b, tank_id=t, location_id=tk[t].location_id,
                system_id=tk[t].system_id, count=c, avg_wt_g=wt * 1000.0, biomass_kg=c * wt,
                density_kg_m3=c * wt / tk[t].volume_m3, stage='SW', days=days))
    P.batch_locations = sorted(keep + new_rows, key=lambda r: (r.week_label, r.tank_id))

    # --- events
    date_of = {}
    for ev in placement.transfer_events:
        if _wk(ev) in Wset and sys_of.get(getattr(ev, 'source_tank_id', None)) != SIXN_SYS:
            date_of.setdefault((_wk(ev), ev.batch_id), ev.event_date)
    six_legs = collections.defaultdict(list)          # (w, b) -> [[dest alloc, fish, src event, src wt g], ...]
    kept_ev = []
    for ev in placement.transfer_events:
        w = _wk(ev)
        if w not in Wset or sys_of.get(ev.source_tank_id) == SIXN_SYS:
            kept_ev.append(ev)
            continue
        for d, q, swt in _split(ev, sys_of)[0]:
            six_legs[(w, ev.batch_id)].append([d, q, ev, swt])
    new_ev = []
    for i, p in enumerate(plan):
        w = p['week']
        senders = collections.defaultdict(list); receivers = collections.defaultdict(list)
        stagers = collections.defaultdict(list)
        for r in p['rows']:
            b, t = r['b'], int(r['t'])
            cb = c_before(b, i)
            if r['ou'] > 0: senders[b].append([t, r['ou'] * cb])
            if r['iu'] > 0: receivers[b].append([t, r['iu'] * cb])
            if r['su'] > 0: stagers[b].append([t, r['su']])
        for b in sorted(set(senders) | set(receivers)):
            snd = sorted(senders[b], key=lambda v: -v[1]); rcv = sorted(receivers[b], key=lambda v: -v[1])
            wtg = wt_before(b, i) * 1000.0
            ev_date = date_of.get((w, b)) or meta[w][0]
            a = c_ = 0
            legs = collections.defaultdict(list)
            while a < len(snd) and c_ < len(rcv):
                q = min(snd[a][1], rcv[c_][1])
                legs[snd[a][0]].append((rcv[c_][0], q))
                snd[a][1] -= q; rcv[c_][1] -= q
                if snd[a][1] < 1e-6: a += 1
                if rcv[c_][1] < 1e-6: c_ += 1
            for s, dl in legs.items():
                tot = sum(q for _, q in dl)
                new_ev.append(Transfer(batch_id=b, event_date=ev_date, source_tank_id=s,
                                       destinations=[TankAllocation(tank_id=d, count=q, avg_wt_g=wtg, cv_pct=0.0)
                                                     for d, q in dl],
                                       count_transferred=tot, channel=CHANNEL))
        # 6N staging: the normal plan's legs into 6N (same 6N tanks, fish, weights), now leaving from the new tanks
        for b, st in stagers.items():
            pool = six_legs.pop((w, b), [])
            tot_u = sum(u for _, u in st)
            want = sum(leg[1] for leg in pool)
            src = [[t, want * u / tot_u] for t, u in sorted(st, key=lambda v: -v[1])]
            j = 0
            for d, q, ev, swt in pool:
                while q > 1e-6 and j < len(src):
                    take = min(q, src[j][1])
                    graded = hasattr(ev, 'pickup_tank_id')
                    new_ev.append(Transfer(
                        batch_id=b, event_date=ev.event_date, source_tank_id=src[j][0],
                        destinations=[TankAllocation(tank_id=d.tank_id, count=take, avg_wt_g=d.avg_wt_g,
                                                     cv_pct=d.cv_pct, size_class=d.size_class)],
                        count_transferred=take, source_avg_wt_g=swt,
                        channel='graded_harvest' if graded else getattr(ev, 'channel', None)))
                    q -= take; src[j][1] -= take
                    if src[j][1] < 1e-6: j += 1
    if six_legs:
        raise RuntimeError(f'6N staging the new layout does not place: {sorted(six_legs)[:3]}')
    P.transfer_events = kept_ev + new_ev
    P.grade_events = [g for g in placement.grade_events if _wk(g) not in Wset]

    # TranOG: same fish per event, landing in the new entry tanks
    arr_t = collections.defaultdict(list)
    for p in plan:
        for r in p['rows']:
            if r['au'] > 0:
                arr_t[(p['week'], r['b'])].append((int(r['t']), r['au']))
    new_tog = []
    for e in placement.tranog_events:
        w = _wk(e)
        if w not in Wset:
            new_tog.append(e)
            continue
        tl = arr_t.get((w, e.batch_id))
        tot = sum(d.count for d in e.destinations)
        if not tl or tot <= 0:
            raise RuntimeError(f'TranOG {e.batch_id} {w}: no arrival tanks in the new layout')
        au = sum(u for _, u in tl)
        wtg = sum(d.count * d.avg_wt_g for d in e.destinations) / tot
        cv = sum(d.count * d.cv_pct for d in e.destinations) / tot
        e2 = copy.copy(e)
        e2.destinations = [TankAllocation(tank_id=t, count=tot * u / au, avg_wt_g=wtg, cv_pct=cv) for t, u in tl]
        new_tog.append(e2)
    P.tranog_events = new_tog
    return P, close


def _biology(placement, P, D: dict, W: list, close: dict, start_state, facility) -> tuple:
    """Per-tank realized biology of the re-laid weeks from their tank rows and events, and how it compares with the
    normal plan's per batch-week. Returns (P with realized_biology set, check dict)."""
    sys_of = {t.tank_id: t.system_id for t in facility.tanks}
    Wset = set(W)

    # --- per-tank biology: what each re-laid tank-week's count and biomass changes say happened in it, per batch
    # (a tank can change batch inside a week: the leaver's flows and the newcomer's are kept apart)
    t_in = collections.defaultdict(float); t_out = collections.defaultdict(float)     # (t, w, b)
    k_in = collections.defaultdict(float); k_out = collections.defaultdict(float)
    for ev in P.transfer_events:
        w = _wk(ev)
        if w not in Wset or _ct(ev) <= 0 or hasattr(ev, 'pickup_tank_id'):
            continue
        tot = sum(d.count for d in ev.destinations) or 1.0
        avg = sum(d.count * d.avg_wt_g for d in ev.destinations) / tot
        sw = getattr(ev, 'source_avg_wt_g', None) or avg
        key = (ev.source_tank_id, w, ev.batch_id)
        t_out[key] += _ct(ev); k_out[key] += _ct(ev) * sw / 1000
        for d, q in _legs(ev):
            t_in[(d.tank_id, w, ev.batch_id)] += q; k_in[(d.tank_id, w, ev.batch_id)] += q * d.avg_wt_g / 1000
    for e in P.tranog_events:
        w = _wk(e)
        if w in Wset:
            for d in e.destinations:
                t_in[(d.tank_id, w, e.batch_id)] += d.count; k_in[(d.tank_id, w, e.batch_id)] += d.count * d.avg_wt_g / 1000
    touched = collections.defaultdict(set)
    for (t, w, b_) in list(t_in) + list(t_out):
        touched[(t, w)].add(b_)
    rb = {k: v for k, v in placement.realized_biology.items() if not (k[1] in Wset and sys_of.get(k[0]) != SIXN_SYS)}
    old_mort = collections.defaultdict(float); old_bio = collections.defaultdict(float)
    for (t, w, b), v in placement.realized_biology.items():
        if w in Wset and sys_of.get(t) != SIXN_SYS:
            old_mort[(b, w)] += v[1]; old_bio[(b, w)] += v[0]
    new_mort = collections.defaultdict(float); new_bio = collections.defaultdict(float)
    worst_neg = 0.0
    og_ids = sorted(t[0] for t in D['tanks'])
    prev = {}
    for t in og_ids:
        s = start_state.tanks_by_id.get(t)
        prev[t] = ((s.batch_id, float(s.count), float(s.biomass_kg)) if s is not None and not s.is_empty
                   else (None, 0.0, 0.0))
    for w in W:
        for t in og_ids:
            pb, pc, pk = prev[t]
            cb, cc, ck = close.get((t, w), (None, 0.0, 0.0))
            for b in sorted({x for x in (pb, cb) if x} | touched.get((t, w), set())):
                o_n, o_k = (pc, pk) if b == pb else (0.0, 0.0)
                c_n, c_k = (cc, ck) if b == cb else (0.0, 0.0)
                key = (t, w, b)
                if not (o_n or c_n or t_in.get(key) or t_out.get(key)):
                    continue
                mort = o_n - t_out[key] + t_in[key] - c_n
                bio = c_k - (o_k - k_out[key] + k_in[key])
                rb[key] = [bio, mort]
                new_mort[(b, w)] += mort; new_bio[(b, w)] += bio
                worst_neg = min(worst_neg, mort)
            prev[t] = (cb, cc, ck) if cb else (None, 0.0, 0.0)
    P.realized_biology = rb
    keys = set(old_mort) | set(new_mort)
    bio_check = dict(
        worst_negative_tank_mortality=worst_neg,
        worst_batch_mortality_diff=max((abs(new_mort[k] - old_mort[k]) for k in keys), default=0.0),
        worst_batch_biomass_diff_kg=max((abs(new_bio[k] - old_bio[k]) for k in keys), default=0.0))
    return P, bio_check


# ------------------------------------------------------------------------------------------------ the step
HARD = ('density over cap (tank-weeks)', 'system biomass over cap x buffer', 'system feed over cap x buffer',
        'weeks over the move budget', 'two batches in a tank', 'entry split at >= 1 kg',
        '6N staging from an entry tank', 'tank under min fish', 'partial move under min',
        'partial 6N staging under min', 'received piece under min')


def relayout(placement, *, start_state, control, facility, facility_limits, system_limits, batch_by_id, tables,
             log=print):
    """Returns (placement to use, summary dict). The summary's 'accepted' says which one it is."""
    sys_of = {t.tank_id: t.system_id for t in facility.tanks}
    weeks = sorted({r.week_label for r in placement.batch_locations if r.count > 0})
    info = {'accepted': False}
    why = unsupported(placement, sys_of)
    bad = sorted(w for w in why if weeks and w >= weeks[0])
    W = [w for w in weeks if not bad or w < bad[0]]
    pin = bool(bad) and bool(W)
    if bad:
        info['stops_at'] = bad[0]
        info['stop_reason'] = '; '.join(f'{k} ({v})' for k, v in sorted(why[bad[0]].items()))
    if len(W) < MIN_WEEKS:
        info['reason'] = (f"only {len(W)} week(s) before {bad[0]} to re-plan ({info['stop_reason']})" if bad
                          else 'no weeks to re-plan')
        return placement, info
    try:
        D = endpoint(placement, start_state=start_state, control=control, facility=facility,
                     facility_limits=facility_limits, system_limits=system_limits, batch_by_id=batch_by_id,
                     tables=tables, weeks=W, pin_last=pin)
        R = lm.solve(D, horizon=int(getattr(control, 'least_moves_horizon', 10) or 10),
                     commit=int(getattr(control, 'least_moves_commit', 5) or 5),
                     secs=float(getattr(control, 'least_moves_secs', 60) or 60),
                     workers=int(getattr(control, 'least_moves_workers', 8) or 8), retries=2, log=log)
    except Exception as e:  # noqa: BLE001 - any failure keeps the normal plan
        info['reason'] = f'solver failed ({type(e).__name__}: {e})'
        return placement, info
    plan = R['plan']
    ce, cn = lm.check(D, D['engine_plan']), lm.check(D, plan)
    te, tn = lm.transfers_per_fish(D, D['engine_plan']), lm.transfers_per_fish(D, plan)
    info.update(tpf_before=te['all'], tpf_after=tn['all'], check_before=ce['issues'], check_after=cn['issues'],
                seconds=R['seconds'], weeks=len(plan))
    worse = [k for k in HARD if cn['issues'].get(k, 0) > ce['issues'].get(k, 0)]
    if cn['issues'].get('batch count differs from the plan'):
        worse.append('batch count differs from the plan')
    if worse:
        info['reason'] = 'rule check worse than the normal plan on: ' + ', '.join(
            f"{k} ({ce['issues'].get(k, 0)} -> {cn['issues'].get(k, 0)})" for k in worse)
        return placement, info
    if tn['all'] >= te['all']:
        info['reason'] = f"no fewer transfers ({te['all']:.2f} -> {tn['all']:.2f})"
        return placement, info
    try:
        P, bio = _rebuild(placement, D, plan, start_state, facility, pin_last=pin)
    except Exception as e:  # noqa: BLE001
        info['reason'] = f'rebuild failed ({type(e).__name__}: {e})'
        return placement, info
    info['biology'] = bio
    if (bio['worst_negative_tank_mortality'] < -250 or bio['worst_batch_mortality_diff'] > 5
            or bio['worst_batch_biomass_diff_kg'] > 500):
        info['reason'] = f'per-tank biology does not reconcile with the normal plan ({bio})'
        return placement, info
    info['accepted'] = True
    return P, info


def summary_line(info: dict) -> str:
    if not info.get('accepted'):
        return f"Least-moves layout NOT used - the normal plan stands: {info.get('reason')}"
    stop = (f"; weeks from {info['stops_at']} kept as planned ({info['stop_reason']})" if info.get('stops_at') else '')
    return (f"Least-moves layout used: transfers per harvested fish {info['tpf_before']:.2f} -> {info['tpf_after']:.2f} "
            f"over {info['weeks']} weeks ({info['seconds'] / 60:.0f} min){stop}; rule check before "
            f"{info['check_before'] or 'clean'}, after {info['check_after'] or 'clean'}")
