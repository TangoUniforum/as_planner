"""A batch the ProductionReport holds partly in freshwater, partly in seawater.

THE DEFECT (measured on 2026-08-31, B49: 47,743 fish in seawater tanks 14 + 24,
250,225 still in freshwater). PR hydration loads OG tanks only, and run.py
kept any batch with an OG tank out of the freshwater projection -- so a split
batch was planned from its seawater part alone and its freshwater part was
moved by NOTHING unless the operator scripted an fw_to_og. With no script those
fish sat in the ledger opening forever (Count_Check +250,225, "FW PART NOT
MODELLED"). On the four historical splits in the corpus (B36, B40, B41, B42)
the same gap either dropped the cohort from backtests or, where the entry tier
was full, hit the placement hard abort.

OPERATOR DECISIONS (2026-09-11) this module implements:

  WHEN   the scenario tran_og_date -- or the first forecast week when that
         date has already passed at the PR close:
         og_entry_week_start(max(tran_og_date, forecast_start)). The same rule
         every batch uses; nothing is invented.
  HOW    the PR's FW count run through FW biology (the batch's configured
  MANY   fw_correction -- no auto-calibration, no calibration-history record),
         then culled to the REMAINING target = tran_og_count - the SW part at
         the PR close (B49 on 8/31: 290,000 - 47,743 = 242,257).
         remaining >= the fish available -> no cull; remaining <= 0 -> NO cull
         and a loud "target already met by the SW part" (culling a batch on a
         registry figure it already met is the silent destruction the tool
         forbids).
  WHERE  a top-up of the batch's OWN entry-tier stage-SW tanks, heaviest first
         (the big class into the heavier tank); a class that would breach the
         density target spills into empty entry tanks; 6N, STARVE and OG3+
         tanks are never used (TranOGEntry.apply refuses them anyway).
  SWITCH control.split_batch_fw: auto (default) | off. `off` is the V1 engine;
         detection (the "Split batch at PR close" warning and the audit's
         FW PART NOT MODELLED) stays on in both.
  WINS   a manual fw_to_og ALWAYS wins -- the batch is then not auto-moved.
  ALSO   an OVERDUE wholly-FW batch (tran_og_date before the PR close, every
         fish still in freshwater) was never placed at all -- its projection
         started in seawater and emitted no arrival. It moves in the first
         forecast week, by the same rule and switch.

MECHANISM (design: scratchpad/ledger/split_batch_design.md). The automatic
arrival reuses the SCHEDULED TranOG path -- project_in_flight_fw_batch on a
copy of the batch with the effective date and the remaining target -- because
it can land in any planner week; the manual path stays the single source for
scripted ones. The FW track's seawater rows are merged with the OG track into
ONE SW row per week (merge_states); several consumers assume one row per
(batch, week). Its freshwater rows travel separately (the ledger, the
FW-inclusive caps and the FW feed reports read them explicitly).

Everything here is pure: the engine's call-outs are no-ops when the split and
overdue sets are empty, so a PR with no split -- and a split the operator
scripts -- runs byte-identical to V1.
"""
from __future__ import annotations

import dataclasses
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from types import SimpleNamespace
from typing import Optional

from .batch_order import batch_sort_key

MODES = ("auto", "off")
# Exactly the two documented words (any case). A YAML boolean is the one
# other form accepted -- an unquoted `off` / `no` loads as False, `on` / `yes`
# as True (YAML 1.1) -- because that is how the file itself spells them; a
# QUOTED 'true', '1', 'no' ... is a typo and is refused like any other.
_AUTO_WORDS = {"auto"}
_OFF_WORDS = {"off"}


def _as_date(d):
    if d is None:
        return None
    return d.date() if isinstance(d, datetime) else d


def sorted_ids(ids) -> list:
    """Batch ids (a set or dict of strings) in natural order: never iterate a
    set of batch-id strings without a unique final sort term."""
    return sorted(ids, key=batch_sort_key)


def mode(control) -> str:
    """The resolved `split_batch_fw` setting: "auto" or "off".

    A YAML `off` written without quotes loads as the boolean False (YAML 1.1),
    so booleans are read as their obvious meaning. Anything else is REFUSED,
    loudly: a typo must not silently pick an engine."""
    raw = getattr(control, "split_batch_fw", "auto")
    if raw is None:
        return "auto"
    if isinstance(raw, bool):
        return "auto" if raw else "off"
    s = str(raw).strip().lower()
    if s in _AUTO_WORDS:
        return "auto"
    if s in _OFF_WORDS:
        return "off"
    raise ValueError(
        f"control split_batch_fw = {raw!r} is not a valid setting: use 'auto' "
        f"(model a split batch's freshwater part automatically) or 'off' (the "
        f"V1 engine; the split is only warned about).")


@dataclass
class SplitClassification:
    """Which PR batches the automatic path models.

    split_ids    PR OG count > 0 AND PR FW count > 0 (a split batch).
    overdue_ids  PR FW count > 0, no OG fish, and tran_og_date before the
                 forecast start (= on/before the PR close).
    Both exclude a batch a manual fw_to_og moved (the manual event wins) and a
    batch with no registry row / tran_og_date (nothing says when it moves --
    the existing not-modelled warning names it). Both are in natural batch
    order. sw_count / fw_count / fw_kg are the PR's own figures."""
    split_ids: list = field(default_factory=list)
    overdue_ids: list = field(default_factory=list)
    sw_count: dict = field(default_factory=dict)
    fw_count: dict = field(default_factory=dict)
    fw_kg: dict = field(default_factory=dict)

    @property
    def auto_ids(self) -> list:
        return sorted(set(self.split_ids) | set(self.overdue_ids), key=batch_sort_key)

    def __bool__(self) -> bool:
        return bool(self.split_ids or self.overdue_ids)


def classify(og_records, fw_records, *, transferred_fw=(), batch_by_id,
             forecast_start) -> SplitClassification:
    """Classify from the ProductionReport's OWN records, never from the
    post-window state (which has already absorbed a manual fw_to_og). Zero-
    count rows are ignored. `forecast_start` is the report's opening day (the
    PR close + 1), not a window-shifted planning start."""
    fs = _as_date(forecast_start)
    sw: dict = defaultdict(float)
    for r in og_records or ():
        sw[r.batch_id] += float(r.closing_count or 0.0)
    fwc: dict = defaultdict(float)
    fwkg: dict = defaultdict(float)
    for r in fw_records or ():
        fwc[r.batch_id] += float(r.closing_count or 0.0)
        fwkg[r.batch_id] += float(r.closing_biomass_kg or 0.0)
    moved = set(transferred_fw or ())
    out = SplitClassification()
    for b in sorted(fwc, key=batch_sort_key):
        if fwc[b] <= 0 or b in moved:
            continue
        meta = (batch_by_id or {}).get(b)
        if meta is None or not meta.input_date or not meta.tran_og_date:
            continue
        if sw.get(b, 0.0) > 0:
            out.split_ids.append(b)
            out.sw_count[b] = sw[b]
        elif fs is not None and _as_date(meta.tran_og_date) < fs:
            out.overdue_ids.append(b)
        else:
            continue
        out.fw_count[b] = fwc[b]
        out.fw_kg[b] = fwkg[b]
    return out


def remaining_target(tran_og_count, sw_count) -> float:
    """The FW part's reconcile target: the whole-batch tran_og_count less the
    fish already in seawater at the PR close. 0 means NO cull -- the target is
    met (or absent), and culling to a registry figure the batch already met
    would destroy fish on paper."""
    t = float(tran_og_count or 0.0)
    r = t - float(sw_count or 0.0)
    return r if (t > 0 and r > 0) else 0.0


def effective_tran_og_date(tran_og_date, forecast_start) -> date:
    """max(tran_og_date, forecast_start): the scenario date, or the first
    forecast week once that date has passed."""
    return max(_as_date(tran_og_date), _as_date(forecast_start))


def fw_track_batch(b_meta, *, forecast_start, target):
    """A COPY of the batch for its freshwater track: the effective date and
    the remaining target. The registry object is untouched, and its
    fw_correction is carried as configured."""
    eff = effective_tran_og_date(b_meta.tran_og_date, forecast_start)
    new_date = (datetime.combine(eff, datetime.min.time())
                if isinstance(b_meta.tran_og_date, datetime) else eff)
    tgt = int(round(target)) if target and target > 0 else 0
    return dataclasses.replace(b_meta, tran_og_date=new_date, tran_og_count=tgt)


def project_fw_track(b_meta, tables, fw_control, fw_count, fw_kg, pr_closing, *,
                     target):
    """(states, residuals, splits) of the FW part, via the scheduled TranOG
    path (biology.project_in_flight_fw_batch) on fw_track_batch's copy.
    `fw_control` carries the ORIGINAL forecast start + horizon, exactly as for
    a wholly-FW in-flight batch."""
    from .biology import project_in_flight_fw_batch
    tb = fw_track_batch(b_meta, forecast_start=fw_control.forecast_start, target=target)
    avg = (float(fw_kg) * 1000.0 / float(fw_count)) if fw_count and fw_kg else 0.0
    return project_in_flight_fw_batch(tb, tables, fw_control, fw_count, avg, pr_closing)


# ---- one SW stream per batch -------------------------------------------------------

_ADD = ("count", "biomass_kg", "feed_kg_day", "feed_kg_week", "cull_event_pct",
        "cull_count_week", "cull_biomass_kg_week", "open_count", "open_biomass_kg",
        "close_count", "close_biomass_kg", "mort_count_week")


def _sum_rows(o, f):
    kw = {k: float(getattr(o, k, 0.0) or 0.0) + float(getattr(f, k, 0.0) or 0.0)
          for k in _ADD}
    wo, wf = float(o.biomass_kg or 0.0), float(f.biomass_kg or 0.0)
    wt = wo + wf

    def _bw(attr):
        if wt <= 0:
            return getattr(o, attr)
        return (getattr(o, attr) * wo + getattr(f, attr) * wf) / wt

    kw["avg_weight_g"] = (kw["biomass_kg"] * 1000.0 / kw["count"]
                          if kw["count"] > 0 else o.avg_weight_g)
    kw["open_avg_weight_g"] = (kw["open_biomass_kg"] * 1000.0 / kw["open_count"]
                               if kw["open_count"] > 0 else o.open_avg_weight_g)
    kw["close_avg_weight_g"] = (kw["close_biomass_kg"] * 1000.0 / kw["close_count"]
                                if kw["close_count"] > 0 else o.close_avg_weight_g)
    kw["sgr_pct_day"] = _bw("sgr_pct_day")
    kw["fcr"] = _bw("fcr")
    kw["stage"] = "SW"
    return dataclasses.replace(o, **kw)


def merge_states(og_rows, fw_rows):
    """(sw_rows, fw_phase_rows).

    Before the FW track's entry week every row is the OG-track row itself
    (unchanged -- a split PR's pre-entry weeks match V1 byte for byte). From
    the entry week on, ONE SW row per week sums both tracks: counts, biomass,
    feed, mortality, culls, open and close add; the weights are biomass over
    count; SGR and FCR are biomass-weighted. The FW track's crossing-week
    cull rides on that row. The FW track's FW/EGG rows come back separately:
    never two rows for one (batch, week) in states_by_batch."""
    fw_sorted = sorted(fw_rows or (), key=lambda s: s.week_label)
    fw_phase = [s for s in fw_sorted if s.stage in ("FW", "EGG")]
    entry = next((s.week_label for s in fw_sorted if s.stage == "SW"), None)
    if entry is None:
        return list(og_rows or ()), fw_phase
    fw_sw = {s.week_label: s for s in fw_sorted if s.stage == "SW"}
    og_by = {s.week_label: s for s in (og_rows or ())}
    out = []
    for wk in sorted(set(og_by) | set(fw_sw)):
        o, f = og_by.get(wk), fw_sw.get(wk)
        if f is None or wk < entry:
            if o is not None:
                out.append(o)
        elif o is None:
            out.append(f)
        else:
            out.append(_sum_rows(o, f))
    return out, fw_phase


# ---- where the arrival lands -------------------------------------------------------

def own_entry_tanks(state, batch_id) -> list:
    """The batch's own entry-tier (OG1/2), on-feed (stage SW) tanks, heaviest
    fish first (tank id breaks ties). 6N is outside the entry tier, STARVE is
    off feed, OG3+ is not an arrival tier -- none of them can take a top-up."""
    from .events import OG12_SYSTEMS
    from .state import STAGE_SW
    own = [t for t in state.tanks_by_id.values()
           if t.batch_id == batch_id and not t.is_empty and t.type == "OG"
           and t.system_id in OG12_SYSTEMS and t.stage == STAGE_SW]
    return sorted(own, key=lambda t: (-float(t.avg_wt_g or 0.0), t.tank_id))


@dataclass
class TopUp:
    allocations: list = field(default_factory=list)   # events.TankAllocation
    empty_tanks_used: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def _cap_kg(t, density_target_pct) -> float:
    return float(t.max_density_kg_m3 or 0.0) * float(t.volume_m3 or 0.0) * float(
        density_target_pct or 0.0)


def plan_topup(split, own, empties, *, density_target_pct, min_tank_control=0.0) -> TopUp:
    """Allocate the arrival: the big class to own[0] (the heaviest), the small
    class to own[1] (or own[0] when the batch holds one tank), each up to its
    density TARGET (the cap Phase A sizes with). What does not fit spills,
    class by class, into `empties` in order. A remainder with no empty tank
    left -- or one smaller than min_tank_control, which would open a tank
    under the force-empty floor -- stays in its own tank, past the target,
    and the returned warning says so. Every fish is allocated."""
    from .events import TankAllocation
    if not own:
        raise ValueError("plan_topup needs at least one own entry tank")
    load = {t.tank_id: float(t.count or 0.0) * float(t.avg_wt_g or 0.0) / 1000.0
            for t in own}
    home = {"big": own[0], "small": own[1] if len(own) > 1 else own[0]}
    pool = list(empties or ())
    alloc: dict = {}
    order: list = []
    used: list = []
    warns: list = []

    def _put(tank_id, cls, n, wt):
        key = (tank_id, cls)
        if key not in alloc:
            alloc[key] = [0.0, wt]
            order.append(key)
        alloc[key][0] += n

    for cls, n, wt in (("big", split.big_class_count, split.big_class_avg_wt_g),
                       ("small", split.small_class_count, split.small_class_avg_wt_g)):
        n = float(n or 0.0)
        if n <= 0:
            continue
        t = home[cls]
        room = max(0.0, _cap_kg(t, density_target_pct) - load[t.tank_id])
        fit = n if wt <= 0 else min(n, room * 1000.0 / wt)
        if fit > 0:
            _put(t.tank_id, cls, fit, wt)
            load[t.tank_id] += fit * wt / 1000.0
        rest = n - fit
        while rest > 0.5 and pool and rest >= float(min_tank_control or 0.0):
            e = pool.pop(0)
            used.append(e.tank_id)
            take = rest if wt <= 0 else min(rest, _cap_kg(e, density_target_pct) * 1000.0 / wt)
            _put(e.tank_id, cls, take, wt)
            rest -= take
        if rest > 0.5:
            _put(t.tank_id, cls, rest, wt)
            load[t.tank_id] += rest * wt / 1000.0
            why = ("no empty entry tank was left to spill into" if not pool else
                   f"fewer than min_tank_control ({float(min_tank_control):,.0f}) fish "
                   f"would have opened a tank under the force-empty floor")
            warns.append(
                f"SPLIT BATCH {split.batch_id}: the top-up of its own tank #{t.tank_id} "
                f"runs past the density target ({density_target_pct:.0%} of the tank "
                f"cap) by {rest:,.0f} {cls}-class fish - {why}. Every fish is "
                f"placed; the density audit judges the tank against its cap.")
        elif rest > 0:
            _put(t.tank_id, cls, rest, wt)        # sub-fish rounding stays home
    cv = float(getattr(split, "post_cull_cv_pct", 16.0) or 16.0)
    allocs = [TankAllocation(tank_id=tid, count=alloc[(tid, cls)][0],
                             avg_wt_g=alloc[(tid, cls)][1], cv_pct=cv, size_class=cls)
              for (tid, cls) in order]
    return TopUp(allocations=allocs, empty_tanks_used=used, warnings=warns)


def spill_tanks_needed(split, own, *, density_target_pct, empty_cap_kg,
                       min_tank_control=0.0) -> int:
    """Empty entry tanks the top-up would spill into, with every empty tank
    holding `empty_cap_kg` at the density target. The arrival make-room and
    the anticipatory pacing count the batch's own tanks as capacity through
    this, so they neither vacate nor pre-free tanks the top-up does not need
    (each is a move against the hard 15-move weekly budget)."""
    if not own:
        return 0
    if not empty_cap_kg or empty_cap_kg <= 0:
        return 0
    virtual = [SimpleNamespace(tank_id=-(i + 1), max_density_kg_m3=float(empty_cap_kg),
                               volume_m3=1.0 / float(density_target_pct or 1.0))
               for i in range(8)]
    tp = plan_topup(split, own, virtual, density_target_pct=density_target_pct,
                    min_tank_control=min_tank_control)
    return len(tp.empty_tanks_used)


# ---- the lines the operator reads ---------------------------------------------------

def _culled_to(target, arrival) -> bool:
    """Did the reconcile cull fire? The projection culls to `target` only
    when more fish than that are left after handling, so the arrival then
    equals the target; an arrival below it means no cull was made. An
    unknown arrival (None) keeps the line's old reading."""
    if target <= 0:
        return False
    return arrival is None or float(arrival) >= float(target) - 0.5


def auto_transfer_line(batch_id, *, fw_count, week_label, placed, tran_og_date,
                       pr_closing, tran_og_count, sw_count, overdue=False,
                       arrival=None) -> str:
    """ONE ValidationLog line per automatic transfer: the batch, the fish, the
    week and the rule -- and how to override it. `arrival` = the fish the
    projection sent to seawater (SizeClassSplit.post_cull_count): it says
    whether a reconcile cull was actually made, so the line never claims one
    that did not happen (B50 overdue on 8/31: 254,135 fish against
    tran_og_count 290,000 -- handling mortality only)."""
    tog, pc = _as_date(tran_og_date), _as_date(pr_closing)
    past = tog is not None and pc is not None and tog <= pc
    toc = float(tran_og_count or 0.0)
    if overdue:
        if toc <= 0:
            tail = (f"{placed:,.0f} enter seawater after freshwater and handling "
                    f"mortality (no tran_og_count to reconcile to)")
        elif _culled_to(toc, arrival):
            tail = (f"{placed:,.0f} enter seawater after freshwater mortality, "
                    f"handling mortality and the reconcile cull to tran_og_count "
                    f"{toc:,.0f}")
        else:
            tail = (f"{placed:,.0f} enter seawater after freshwater and handling "
                    f"mortality only - NO reconcile cull: fewer fish than "
                    f"tran_og_count {toc:,.0f} were left to cull from")
        return (f"OVERDUE FW BATCH {batch_id}: {fw_count:,.0f} FW fish auto-transferred "
                f"{week_label} (the first forecast week) - scenario tran_og_date {tog} "
                f"is before the PR close {pc}, and every fish was still in freshwater "
                f"- script an fw_to_og to override. {tail}.")
    when = (f"per scenario tran_og_date ({tog}, already past at the PR close {pc}, "
            f"so the first forecast week)" if past
            else f"per scenario tran_og_date ({tog})")
    rem = remaining_target(tran_og_count, sw_count)
    if toc <= 0:
        how = (f"{placed:,.0f} enter seawater after freshwater and handling mortality "
               f"only - no tran_og_count to reconcile to")
    elif rem <= 0:
        how = (f"{placed:,.0f} enter seawater after freshwater and handling mortality "
               f"only - NO reconcile cull: target already met by the SW part "
               f"(tran_og_count {toc:,.0f}, {float(sw_count):,.0f} already in "
               f"seawater at the PR close)")
    elif not _culled_to(rem, arrival):
        how = (f"{placed:,.0f} enter seawater after freshwater and handling mortality "
               f"only - NO reconcile cull: fewer fish than the remaining target "
               f"{rem:,.0f} (tran_og_count {toc:,.0f} - {float(sw_count):,.0f} "
               f"already in seawater at the PR close) were left to cull from")
    else:
        how = (f"{placed:,.0f} enter seawater after freshwater mortality, handling "
               f"mortality and the reconcile cull to the remaining target {rem:,.0f} "
               f"(tran_og_count {toc:,.0f} - {float(sw_count):,.0f} already in "
               f"seawater at the PR close)")
    return (f"SPLIT BATCH {batch_id}: {fw_count:,.0f} FW fish auto-transferred "
            f"{week_label} {when} - script an fw_to_og to override. {how}, topping up "
            f"the batch's own entry tanks.")


def not_placed_line(batch_id, *, expected, placed, week_label, overdue=False) -> str:
    head = "OVERDUE FW BATCH" if overdue else "SPLIT BATCH"
    return (f"{head} {batch_id}: FW PART NOT PLACED - {expected:,.0f} fish were due "
            f"to enter seawater {week_label} by the automatic transfer and "
            f"{placed:,.0f} were placed; {expected - placed:,.0f} fish are missing "
            f"from the plan (InputConservationAudit: FW PART DROPPED).")


def hybrid_guide_line(batch_id, count, week_label) -> str:
    return (f"HYBRID GUIDE - split batch {batch_id}: the automatic FW->SW transfer "
            f"({count:,.0f} fish, {week_label}) is not seeded into the L1 guide - "
            f"its freshwater biomass is counted, the arrival is not - so the "
            f"guide's harvest envelope under-reads {batch_id} from {week_label}. "
            f"The controller plans the arrival itself.")


def hybrid_guide_overdue_line(batch_id, count, week_label) -> str:
    """The overdue sibling of hybrid_guide_line. The L1 guide seeds the fish
    in seawater at the PR close and skips an incoming batch whose
    tran_og_date is on or before the forecast start
    (global_planner_poc: `tran_og <= fs`), and an overdue batch's track has
    no freshwater week -- so the guide sees none of it."""
    return (f"HYBRID GUIDE - overdue FW batch {batch_id}: the automatic FW->SW "
            f"transfer ({count:,.0f} fish, {week_label}) is not in the L1 guide - "
            f"the guide seeds only the fish in seawater at the PR close and skips "
            f"a batch whose tran_og_date is before the forecast start - so its "
            f"harvest envelope leaves {batch_id} out for the whole horizon. The "
            f"controller plans the arrival itself.")


def unplaced_split_parts(expected, tranog_events) -> dict:
    """{batch: fish} an automatic transfer was due to place but did not. Only
    batches in `expected` (the auto-modelled ones) are judged, so a manually
    handled split never produces a line."""
    placed: dict = defaultdict(float)
    for ev in tranog_events or ():
        p = getattr(ev, "count_placed", None)
        if p is None:
            p = sum(float(getattr(d, "count", 0.0) or 0.0)
                    for d in getattr(ev, "destinations", ()) or ())
        placed[ev.batch_id] += float(p or 0.0)
    out = {}
    for b in sorted(expected or {}, key=batch_sort_key):
        miss = float(expected[b] or 0.0) - placed.get(b, 0.0)
        if miss > 0.5:
            out[b] = miss
    return out
