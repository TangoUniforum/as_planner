"""Opt-in LP-guided LNS placement refinement (the Tier-3 engine).

Design: docs/LP_GUIDED_LNS_PLACEMENT.md. Principle: ADD, never remove — the greedy
placement is BOTH the warm start and the fallback.

Why it operates on the REALIZED layout (not the canvas plan): measurement showed the
canvas plan's implied hot spot (~3.3x) is reshaped by `rebalance_level` down to the
realized ~1.5x — so the realized `batch_locations` is what carries the hot spots the
reports show. LNS therefore relocates *realized* grow-out tank occupancy off the
hottest systems onto cooler ones, emitting each move as a conserved `Transfer`.

How it stays continuity-safe (the operator's hard constraint): every candidate edit
is checked against the REAL `write_tank_continuity_audit` reconciliation (0 drift) and
input conservation (no batch dropped), and accepted only if it NEVER raises the
hot-spot peak and strictly improves the lexicographic (peak, over-cap area, balance
CV) objective — see `_better`. Anything else is reverted. The greedy plan is always
the floor — you cannot lose a fish, and turning LNS on can never make the forecast
worse.

Build phases (each keeps greedy byte-identical when off):
  A. (this) realized relocate + audit-gated accept, greedy hot-spot targeting.
  B. CP-SAT window repair (choose the best SET of relocations at once).
  C. LP guidance + rolling neighborhoods.
"""
from __future__ import annotations

import copy
import os
import sys
from collections import defaultdict
from datetime import timedelta

from .biology import realized_feed_kg_day
from .caps import carry_forward_cap_lookup
from .tiers import ENTRY_SYSTEMS as OG12_SYSTEMS  # entry tier — source of truth in tiers.py
from .time_grid import iso_week_label


# --------------------------------------------------------------------------- #
# Hot-spot metric — mirrors excel_io.write_system_limits_audit so the number we
# optimize against is IDENTICAL to the reported SystemLimitsAudit peak. Both
# now call the one lookup in caps, so "identical" is structural rather than a
# pair of hand-kept copies.
# --------------------------------------------------------------------------- #
def _carry_forward_caps(system_limits):
    return carry_forward_cap_lookup(system_limits)


def system_loads(batch_locations, batch_by_id, tables, system_limits):
    """Per-(week, system) realized biomass + daily feed, plus the cap lookup."""
    cap = _carry_forward_caps(system_limits)
    sb: dict = defaultdict(float)
    sf: dict = defaultdict(float)
    for r in batch_locations:
        if r.count <= 0:
            continue
        b = batch_by_id.get(r.batch_id)
        sb[(r.week_label, r.system_id)] += r.biomass_kg
        if getattr(r, "stage", "") != "STARVE":
            sf[(r.week_label, r.system_id)] += realized_feed_kg_day(
                r.avg_wt_g, r.biomass_kg, b, tables, r.week_label)
    return sb, sf, cap


def system_peak(batch_locations, batch_by_id, tables, system_limits):
    """The single hottest (system, week) biomass/feed load fraction (excl OG6N)."""
    sb, sf, cap = system_loads(batch_locations, batch_by_id, tables, system_limits)
    peak = 0.0
    for (wk, sysid), bio in sb.items():
        if sysid == "OG6N":
            continue
        bc = cap(wk, sysid, "biomass")
        if bc and bc > 0:
            peak = max(peak, bio / bc)
    for (wk, sysid), feed in sf.items():
        if sysid == "OG6N":
            continue
        fc = cap(wk, sysid, "feed_per_day")
        if fc and fc > 0:
            peak = max(peak, feed / fc)
    return peak


def system_score(batch_locations, batch_by_id, tables, system_limits, grow):
    """Lexicographic objective (lower is better, in priority order):
      peak  — the single hottest load fraction (biomass or feed, excl OG6N);
      area  — the TOTAL over-cap overage, Σ max(0, frac - 1) across every cell;
      cv    — mean between-grow-out-system load-fraction CV per week (balance).

    `peak` preserves the original single-objective behaviour. The `area` and `cv`
    tiers are what let LNS add value on a capacity-bound (tank-full) facility: a
    peak-neutral swap can relieve OTHER over-cap cells (area) and even the load
    system-to-system (cv) even when the single hottest cell can't be lowered."""
    sb, sf, cap = system_loads(batch_locations, batch_by_id, tables, system_limits)
    frac: dict = defaultdict(float)
    for (wk, sysid), bio in sb.items():
        if sysid == "OG6N":
            continue
        bc = cap(wk, sysid, "biomass")
        if bc and bc > 0:
            frac[(wk, sysid)] = max(frac[(wk, sysid)], bio / bc)
    for (wk, sysid), feed in sf.items():
        if sysid == "OG6N":
            continue
        fc = cap(wk, sysid, "feed_per_day")
        if fc and fc > 0:
            frac[(wk, sysid)] = max(frac[(wk, sysid)], feed / fc)
    peak = max(frac.values(), default=0.0)
    area = sum(max(0.0, f - 1.0) for f in frac.values())
    byweek: dict = defaultdict(list)
    for (wk, sysid), f in frac.items():
        if sysid in grow:
            byweek[wk].append(f)
    cvs = []
    for fs in byweek.values():
        if len(fs) >= 2:
            m = sum(fs) / len(fs)
            if m > 0:
                var = sum((x - m) ** 2 for x in fs) / len(fs)
                cvs.append((var ** 0.5) / m)
    cv = sum(cvs) / len(cvs) if cvs else 0.0
    return (peak, area, cv)


def _better(new, base, *, peak_eps=1e-9, area_eps=1e-3, cv_eps=5e-3):
    """Accept `new` over `base` iff it NEVER worsens the peak and strictly improves
    the lexicographic (peak, area, cv) objective by at least the per-tier epsilon.
    So a move can be taken to relieve total over-cap area or even the load even
    when the single hottest cell is pinned at the capacity floor — but a move that
    would raise the peak, or only churns CV by a trivial amount, is rejected."""
    if new[0] > base[0] + peak_eps:
        return False                       # hard rule: never raise the peak
    if new[0] < base[0] - peak_eps:
        # Strictly lower peak — good, but it may not be BOUGHT with more total
        # over-cap area (shaving the hottest cell while pushing other systems
        # over cap is a placement failure, not an improvement).
        return new[1] <= base[1] + area_eps
    if new[1] < base[1] - area_eps:
        return True                        # peak tied, less total over-cap area
    if new[1] > base[1] + area_eps:
        return False                       # peak tied, MORE area — reject
    return new[2] < base[2] - cv_eps       # peak + area tied, better balance


# --------------------------------------------------------------------------- #
# Conservation gate — reuse the REAL continuity audit so our reconciliation can
# never diverge from the one the regression test locks.
# --------------------------------------------------------------------------- #
def drift_count(placement, batch_week_states, initial_state, realized_biology=None):
    """Count TANK_DRIFT + BIO_DRIFT rows the real audit would flag for this
    (batch_locations, events) — 0 means continuity is intact.

    `realized_biology=None` runs the modelled (SGR/m_pct) reconciliation — the
    per-move accept gate, tank-agnostic so a relabel never perturbs it. Pass the
    plan's re-keyed realized_biology to audit the EXACT ground-truth reconciliation
    that ships (run.py), which catches any re-keying mistake before greedy is
    replaced. (Callers pass `dict or None`: an EMPTY dict would set the audit's
    _have_realized True with no data, zeroing every mortality — a false-drift trap.)"""
    import openpyxl

    from .excel_io import write_tank_continuity_audit
    wb = openpyxl.Workbook()
    write_tank_continuity_audit(
        wb, placement.batch_locations, batch_week_states,
        placement.harvest_events, placement.transfer_events,
        placement.grade_events, placement.tranog_events, initial_state,
        realized_biology=realized_biology)
    return _count_drift_rows(wb["TankContinuityAudit"])


def _count_drift_rows(ws):
    """TANK_DRIFT + BIO_DRIFT rows on a TankContinuityAudit sheet.

    Locates the flag columns BY HEADER NAME, not by hardcoded index: a column
    inserted into the audit layout would silently shift the flags and make this
    gate fail OPEN (0 drift reported for a drifting plan). No header found =
    fail CLOSED (a huge count, so every candidate is rejected and greedy stands)."""
    flag_i = bio_i = None
    n = 0
    for row in ws.iter_rows(values_only=True):
        if flag_i is None:
            if row and "Flag" in row and "Bio_Flag" in row:
                flag_i, bio_i = row.index("Flag"), row.index("Bio_Flag")
            continue
        if not row:
            continue
        if len(row) > flag_i and row[flag_i] == "TANK_DRIFT":
            n += 1
        if len(row) > bio_i and row[bio_i] == "BIO_DRIFT":
            n += 1
    if flag_i is None:
        return 10 ** 9        # header not found — fail closed, never fail open
    return n


# --------------------------------------------------------------------------- #
# Segments — a maximal contiguous run of weeks where ONE batch occupies ONE tank
# --------------------------------------------------------------------------- #
class _Segment:
    __slots__ = ("batch_id", "tank_id", "system", "rows", "week_labels")

    def __init__(self, rows):
        self.rows = rows
        self.batch_id = rows[0].batch_id
        self.tank_id = rows[0].tank_id
        self.system = rows[0].system_id
        self.week_labels = [r.week_label for r in rows]

    @property
    def ws(self):
        return self.week_labels[0]

    @property
    def we(self):
        return self.week_labels[-1]

    def bio(self, batch_meta, tables):
        """{week_label: (biomass_kg, feed_kg_day)} for this segment."""
        out = {}
        b = batch_meta.get(self.batch_id)
        for r in self.rows:
            feed = (0.0 if getattr(r, "stage", "") == "STARVE"
                    else realized_feed_kg_day(r.avg_wt_g, r.biomass_kg, b,
                                              tables, r.week_label))
            out[r.week_label] = (r.biomass_kg, feed, r.avg_wt_g)
        return out


def _segments(batch_locations, week_index):
    """All maximal (batch, tank) contiguous-week segments, deterministically ordered."""
    rows_by: dict = defaultdict(list)
    for r in batch_locations:
        if r.count > 0:
            rows_by[(r.batch_id, r.tank_id)].append(r)
    segs = []
    for (bid, tid), rows in rows_by.items():
        rows.sort(key=lambda r: week_index.get(r.week_label, 0))
        run = [rows[0]]
        for prev, cur in zip(rows, rows[1:]):
            if week_index[cur.week_label] == week_index[prev.week_label] + 1:
                run.append(cur)
            else:
                segs.append(_Segment(run))
                run = [cur]
        segs.append(_Segment(run))
    segs.sort(key=lambda s: (s.week_labels[0], s.system, s.batch_id, s.tank_id))
    return segs


# --------------------------------------------------------------------------- #
# Relabel — move a (batch, tank, weeks) occupancy to a new physical tank, in the
# batch_locations AND every event that references it. The continuity audit gates
# the result, so a mistake here is caught (reverted), never shipped.
# --------------------------------------------------------------------------- #
def _relabel(placement, relmap, tank_by_id):
    """Apply {(batch_id, old_tank, week_label) -> new_tank} to batch_locations and
    all event streams.

    batch_locations is relabeled exactly (by row week). Event tank refs sit on the
    BOUNDARY between occupancy and non-occupancy (an arrival at week w, a full-
    harvest / departure at the week AFTER the last occupied week), so we relabel an
    event's tank if the relmap touches (batch, tank) at the event week OR the week
    before. A relocation moves a whole contiguous segment to one target tank, so
    both weeks map to the same destination — the 2-week window catches the exit
    event without mis-routing. (And the continuity audit gates the result, so any
    timing miss is reverted, never shipped.)"""
    def relabel(batch_id, tank, ev_date):
        wk = iso_week_label(ev_date)
        pwk = iso_week_label(ev_date - timedelta(days=7))
        return (relmap.get((batch_id, tank, wk))
                or relmap.get((batch_id, tank, pwk)) or tank)

    for r in placement.batch_locations:
        nt = relmap.get((r.batch_id, r.tank_id, r.week_label))
        if nt is not None:
            tk = tank_by_id[nt]
            r.tank_id = nt
            r.location_id = tk.location_id
            r.system_id = tk.system_id
            r.density_kg_m3 = (r.biomass_kg / tk.volume_m3) if tk.volume_m3 else 0.0

    for ev in placement.tranog_events:
        for d in ev.destinations:
            d.tank_id = relabel(ev.batch_id, d.tank_id, ev.event_date)

    for ev in placement.harvest_events:
        ev.source_tank_id = relabel(ev.batch_id, ev.source_tank_id, ev.event_date)

    for ev in placement.transfer_events:
        if hasattr(ev, "pickup_tank_id"):              # GradedHarvest rides here
            ev.source_tank_id = relabel(ev.batch_id, ev.source_tank_id, ev.event_date)
            ev.pickup_tank_id = relabel(ev.batch_id, ev.pickup_tank_id, ev.event_date)
            ev.retention_tank_id = relabel(
                ev.batch_id, ev.retention_tank_id, ev.event_date)
            continue
        ev.source_tank_id = relabel(ev.batch_id, ev.source_tank_id, ev.event_date)
        for d in ev.destinations:
            d.tank_id = relabel(ev.batch_id, d.tank_id, ev.event_date)

    for ev in placement.grade_events:
        ev.source_tank_ids = [
            relabel(ev.batch_id, t, ev.event_date) for t in ev.source_tank_ids]
        for d in ev.destinations:
            d.tank_id = relabel(ev.batch_id, d.tank_id, ev.event_date)

    # Re-key the realized biology alongside the occupancy move. It is keyed by
    # (tank_id, week_label, batch_id) -> [bio_delta_kg, mort_count] and feeds the
    # SHIPPED TankContinuityAudit (run.py). If it kept the OLD tank keys after a
    # relocate/swap, that audit would reconcile every moved tank-week against a
    # missing key — mort defaults to 0 and biomass to the coarse SGR fallback —
    # and flag phantom TANK_DRIFT/BIO_DRIFT the (modelled) accept gate never sees.
    # Pop all sources first, then assign, so a two-way SWAP can't clobber: the
    # batch_id in the key already disambiguates the two directions, but staging
    # the writes keeps it order-independent. Symmetric under _invert (revert).
    rb = getattr(placement, "realized_biology", None)
    if rb:
        moves = {}
        for (bid, old_tank, wk), new_tank in relmap.items():
            k_old = (old_tank, wk, bid)
            if k_old in rb:
                moves[k_old] = (new_tank, wk, bid)
        vals = {k: rb.pop(k) for k in moves}
        for k_old, k_new in moves.items():
            rb[k_new] = vals[k_old]


def _invert(relmap):
    """Inverse map to undo a relabel: (batch, new_tank, week) -> old_tank."""
    return {(b, nt, w): t for (b, t, w), nt in relmap.items()}


def _density_legal(seg, tank_id, tank_by_id, batch_meta, tables):
    """True if `seg`'s biomass respects `tank_id`'s per-tank density cap on every
    week of the segment (a no-volume tank cannot be checked — treat as legal,
    matching _best_target)."""
    tk = tank_by_id[tank_id]
    if not tk.volume_m3:
        return True
    load = seg.bio(batch_meta, tables)
    return all(load[w][0] / tk.volume_m3 <= tk.max_density_kg_m3
               for w in seg.week_labels)


# --------------------------------------------------------------------------- #
# Target selection — coldest grow-out system with a free tank that keeps density
# legal and lands the segment below the current hot-spot ratio.
# --------------------------------------------------------------------------- #
def _best_target(seg, grow, hot_sys, hot_ratio, sys_tanks, occ, sb, sf, cap,
                 tank_by_id, batch_meta, tables):
    seg_load = seg.bio(batch_meta, tables)
    best_tank = None
    # Must STRICTLY beat the hot spot AND land legal: without the 1.0 floor a
    # relocation could push a legal system over its cap just because it ends
    # cooler than the hot spot (a system over cap while another has room is a
    # placement failure — see docs/GLOBAL placement design principle).
    best_worst = min(hot_ratio, 1.0)
    for s in grow:                                      # grow is sorted -> deterministic
        if s == hot_sys:
            continue
        # resulting load of s across the segment weeks if it absorbs the segment
        worst = 0.0
        for w in seg.week_labels:
            bkg, fkg, _ = seg_load[w]
            bc = cap(w, s, "biomass")
            if bc and bc > 0:
                worst = max(worst, (sb.get((w, s), 0.0) + bkg) / bc)
            fc = cap(w, s, "feed_per_day")
            if fc and fc > 0:
                worst = max(worst, (sf.get((w, s), 0.0) + fkg) / fc)
        if worst >= best_worst:
            continue
        # a tank free across ALL the segment's weeks, with density legal there
        for t in sys_tanks[s]:
            if any((t, w) in occ for w in seg.week_labels):
                continue
            tk = tank_by_id[t]
            if tk.volume_m3 and any(
                    seg_load[w][0] / tk.volume_m3 > tk.max_density_kg_m3
                    for w in seg.week_labels):
                continue
            best_tank, best_worst = t, worst
            break
    return best_tank


# --------------------------------------------------------------------------- #
# Entry point — greedy hot-spot relocation, audit-gated, accept-only-if-better
# --------------------------------------------------------------------------- #
def refine_realized(placement, *, initial_state, batch_week_states, control,
                    facility, system_limits, facility_limits, batch_meta, tables):
    """Relocate realized grow-out occupancy off the hottest systems; return the
    edited PlacementResult, or None to keep greedy. Every move is gated on 0 drift
    + no dropped batch + a strict `_better` improvement (never a higher peak);
    otherwise it is reverted."""
    grow = sorted({t.system_id for t in facility.tanks
                   if t.type == "OG" and t.system_id not in OG12_SYSTEMS
                   and t.system_id != "OG6N"})
    if len(grow) < 2:
        return None
    tank_by_id = {t.tank_id: t for t in facility.tanks}
    sys_tanks: dict = defaultdict(list)
    for t in facility.tanks:
        if t.type == "OG":
            sys_tanks[t.system_id].append(t.tank_id)
    for s in sys_tanks:
        sys_tanks[s].sort()
    cap = _carry_forward_caps(system_limits)

    g_batches = {r.batch_id for r in placement.batch_locations}
    work = copy.deepcopy(placement)                    # greedy stays untouched
    # Lexicographic objective (peak, over-cap area, balance CV): peak is the
    # original single-objective behaviour; the area + CV tiers let peak-neutral
    # swaps relieve TOTAL overage and even the load on a full facility, where the
    # single hottest cell can't be lowered by any move.
    start_score = system_score(placement.batch_locations, batch_meta, tables,
                               system_limits, grow)
    base_score = start_score

    weeks = sorted({r.week_label for r in work.batch_locations})
    week_index = {w: i for i, w in enumerate(weeks)}
    last_week = weeks[-1] if weeks else None
    budget = int(getattr(control, "lns_max_moves", 30) or 30)
    moves = 0

    for _ in range(budget):
        sb, sf, _c = system_loads(work.batch_locations, batch_meta, tables, system_limits)
        # hottest grow-out (system, week)
        hot_sys = hot_wk = None
        hot_ratio = 1e-9
        for (w, s), bio in sb.items():
            if s not in grow:
                continue
            bc = cap(w, s, "biomass")
            if bc and bc > 0 and bio / bc > hot_ratio:
                hot_ratio, hot_sys, hot_wk = bio / bc, s, w
        for (w, s), feed in sf.items():
            if s not in grow:
                continue
            fc = cap(w, s, "feed_per_day")
            if fc and fc > 0 and feed / fc > hot_ratio:
                hot_ratio, hot_sys, hot_wk = feed / fc, s, w
        if hot_sys is None or hot_ratio <= 1.0 + 1e-9:
            break                                       # nothing over cap to relieve

        occ = {(r.tank_id, r.week_label) for r in work.batch_locations if r.count > 0}
        if os.environ.get("LNS_DEBUG") and moves == 0:
            print(f"  [LNS_DEBUG] hottest = {hot_sys} {hot_wk} ratio={hot_ratio:.3f}",
                  file=sys.stderr)
            for s in grow:
                bc = cap(hot_wk, s, "biomass") or 0
                fc = cap(hot_wk, s, "feed_per_day") or 0
                br = (sb.get((hot_wk, s), 0.0) / bc) if bc else 0.0
                fr = (sf.get((hot_wk, s), 0.0) / fc) if fc else 0.0
                free = sum(1 for t in sys_tanks[s] if (t, hot_wk) not in occ)
                print(f"  [LNS_DEBUG]   {s}: bio={br:.2f} feed={fr:.2f} "
                      f"free_tanks@{hot_wk}={free}/{len(sys_tanks[s])}", file=sys.stderr)
            ncand = sum(1 for sg in _segments(work.batch_locations, week_index)
                        if sg.system == hot_sys and sg.ws <= hot_wk <= sg.we
                        and sg.we != last_week)
            print(f"  [LNS_DEBUG]   relocatable segments in {hot_sys}@{hot_wk}: "
                  f"{ncand}", file=sys.stderr)
        cands = [sg for sg in _segments(work.batch_locations, week_index)
                 if sg.system == hot_sys and sg.ws <= hot_wk <= sg.we
                 and sg.we != last_week]            # leave terminal cells (final_state)
        # biggest contributor to the hot week first
        cands.sort(key=lambda sg: (
            -dict((r.week_label, r.biomass_kg) for r in sg.rows).get(hot_wk, 0.0),
            sg.batch_id, sg.tank_id))

        did = False
        for seg in cands:
            target = _best_target(seg, grow, hot_sys, hot_ratio, sys_tanks, occ,
                                   sb, sf, cap, tank_by_id, batch_meta, tables)
            if target is None:
                continue
            relmap = {(seg.batch_id, seg.tank_id, w): target for w in seg.week_labels}
            _relabel(work, relmap, tank_by_id)
            new_score = system_score(work.batch_locations, batch_meta, tables,
                                     system_limits, grow)
            ok = (_better(new_score, base_score)
                  and {r.batch_id for r in work.batch_locations} >= g_batches
                  and drift_count(work, batch_week_states, initial_state) == 0)
            if ok:
                base_score = new_score
                moves += 1
                did = True
                break
            _relabel(work, _invert(relmap), tank_by_id)   # revert the bad trial

        # No free-tank target (facility full at the peak) — try a SWAP: exchange a
        # feed-heavy hot-system segment with a lighter cool-system segment of the
        # SAME week span (a clean 1:1 relabel, no empty tank needed). This is the
        # lever for a 100%-full facility with a per-system feed imbalance.
        if not did:
            all_segs = _segments(work.batch_locations, week_index)
            cool_by_span: dict = defaultdict(list)
            for sg in all_segs:
                if sg.system in grow and sg.system != hot_sys and sg.we != last_week:
                    cool_by_span[(sg.ws, sg.we)].append(sg)
            for seg in cands:
                sa_load = seg.bio(batch_meta, tables)
                sa_hot = sa_load.get(hot_wk, (0.0, 0.0, 0.0))
                for sb_seg in cool_by_span.get((seg.ws, seg.we), []):
                    sbl = sb_seg.bio(batch_meta, tables).get(hot_wk, (0.0, 0.0, 0.0))
                    if sbl[0] >= sa_hot[0] and sbl[1] >= sa_hot[1]:
                        continue                       # SB not lighter — swap won't relieve
                    # Per-tank density must stay legal on BOTH swapped tanks
                    # (the relocate path checks this in _best_target; a swap
                    # exchanges tanks, so check each segment against the tank
                    # it is moving INTO).
                    if not (_density_legal(seg, sb_seg.tank_id,
                                           tank_by_id, batch_meta, tables)
                            and _density_legal(sb_seg, seg.tank_id,
                                               tank_by_id, batch_meta, tables)):
                        continue
                    relmap = {(seg.batch_id, seg.tank_id, w): sb_seg.tank_id
                              for w in seg.week_labels}
                    relmap.update({(sb_seg.batch_id, sb_seg.tank_id, w): seg.tank_id
                                   for w in sb_seg.week_labels})
                    _relabel(work, relmap, tank_by_id)
                    new_score = system_score(work.batch_locations, batch_meta,
                                             tables, system_limits, grow)
                    ok = (_better(new_score, base_score)
                          and {r.batch_id for r in work.batch_locations} >= g_batches
                          and drift_count(work, batch_week_states, initial_state) == 0)
                    if ok:
                        base_score = new_score
                        moves += 1
                        did = True
                        break
                    _relabel(work, _invert(relmap), tank_by_id)
                if did:
                    break
        if not did:
            break

    if moves == 0:
        # stdout, not stderr: the app captures stdout only (_TeeIO), so a
        # stderr fallback line is invisible exactly where it matters — the
        # operator would see ACCEPTED runs narrated but never the fallbacks.
        print("  LNS placement: no beneficial relocation (greedy already near the "
              "capacity floor); greedy stands")
        return None
    # belt-and-suspenders final gate — check BOTH reconciliations: the modelled
    # one (the per-move gate) AND, when realized biology exists, the ground-truth
    # one that actually SHIPS (so a realized_biology re-keying mistake falls back
    # to greedy instead of shipping a falsified audit). `or None` avoids the
    # empty-dict false-drift trap (see drift_count).
    _rb = getattr(work, "realized_biology", None) or None
    if (drift_count(work, batch_week_states, initial_state) > 0
            or (_rb is not None
                and drift_count(work, batch_week_states, initial_state,
                                realized_biology=_rb) > 0)
            or {r.batch_id for r in work.batch_locations} < g_batches):
        print("  LNS placement: final safety gate failed; greedy stands")
        return None
    print(f"  LNS placement: ACCEPTED — {moves} move(s): peak "
          f"{start_score[0]:.3f}->{base_score[0]:.3f}, over-cap area "
          f"{start_score[1]:.2f}->{base_score[1]:.2f}, balance CV "
          f"{start_score[2]:.3f}->{base_score[2]:.3f}")
    return work
