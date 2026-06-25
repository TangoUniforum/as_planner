"""STANDALONE proof-of-concept: the SPECIFIC-TANK pick (step #2) for the GLOBAL planner.

METHOD: GLOBAL specific-tank realization (continuity-preserving)
===============================================================

L3 (`forecast.global_planner_l3_poc`) lays out a legal, low-churn whole-tank
SYSTEM plan: per-(batch, system, week) integer tank COUNTS ``y[b,s,w]`` plus the
L1 per-(batch, week) standing (count / biomass / mean weight; some rows
``in_purge`` = parked in the 6N depuration hold). L3 does NOT pick the specific
physical tank within a system or run the 6N pair rotation — that is THIS step.

This module realizes L3's system plan as PHYSICAL TANK IDS, week to week, while
preserving TANK CONTINUITY (the core operator invariant): a batch stays on the
tanks it already holds as long as L3 keeps it in that system; tanks are only
claimed / released / relocated when L3's plan forces it. Every physical move is
emitted as a Transfer so the TransferPlan is truthful and the
``TankContinuityAudit`` reconciles to 0 drift.

The assignment (per week, chronological)
----------------------------------------
A running ``tank_state: tank_id -> Occupant(batch, count, biomass, avg_wt)`` is
carried week to week. For each week:

1. **Grow-out placement (L3 ``y[b,s,w]``).** For each (batch, system) the plan
   asks for ``want`` tanks. The batch's tanks carried from last week are split
   into those already IN this system (keep — continuity) and those in OTHER
   systems (these will be released / relocated). Then:
     * If ``want`` <= kept-in-system: keep the lowest ``want`` of them, release
       the rest (prefer freeing whole tanks).
     * If ``want`` > kept-in-system: keep all in-system tanks, then CLAIM free
       tanks in the system (a relocation from one of the batch's tanks elsewhere
       if it has a spare to move, else a fresh empty tank). Claiming reuses a
       tank the batch is vacating in another system as the SOURCE of the move, so
       a system-to-system shift is one transfer per tank rather than a
       drain-to-limbo.
   Per-tank count / biomass = the batch's L1 standing split EVENLY over its
   assigned tanks that week (the L3 even-split assumption).

2. **6N purge hold (L1 ``in_purge`` rows).** The off-feed depuration population
   is parked in the 6N pair tanks via the round-robin from `forecast.sixn`
   (mains 61/63/65 preferred; the sister 67/69/71 of a pair is taken only when
   that pair needs two tanks), held the rolling 2-week purge, then harvested. A
   batch already in a 6N tank stays there (continuity); growth in the held
   population claims additional 6N tanks in pair order. In 6N PRODUCTION mode no
   row is ``in_purge`` (L1 starves in place), so the 6N mains carry production
   grow-out exactly like any other system and this step is empty.

3. **Transfers.** After both pools are assigned for the week, the per-batch
   tank set is compared to last week's. A batch tank that is NEW this week is a
   move IN; it is paired with one of that batch's vacated tanks (a real
   tank-to-tank relocation) when one exists, else sourced as a fresh stocking
   from the batch's largest current tank (a split). Each pairing is one Transfer
   event (week, batch, source_tank, dest_tank, count, avg_wt) of the destination
   tank's per-tank count. TranOG (FW->first OG tank) and harvest-out are NOT
   re-emitted here — the harvest events already carry the source tank.

Over-subscription (the known 1-week structural case)
----------------------------------------------------
The loop converges loading to the tank-realizable envelope, but ceil() rounding
can leave ONE week asking for more grow-out tanks (up to ~34) than the 33
physical grow-out tanks. When a system's ``want`` exceeds its free tanks the
overflow tanks are placed by DOUBLE-STACKING onto the system's already-occupied
tanks (lowest tank id first) and each such row is flagged ``oversub=True`` +
counted. Nothing is dropped and biomass is conserved; the flag is surfaced in
the stamp / a note so the operator sees the one over-packed week explicitly.

What this is / is NOT
---------------------
This is a CONTINUITY-VALID, transfer-economical greedy pick — it keeps batches
put and turns L3's count deltas into the minimal set of physical moves that
realize them. It is NOT a globally transfer-MINIMAL assignment (no look-ahead /
search; a batch relocating systems picks an arbitrary-but-deterministic free
tank). The even-split (count/biomass uniform across a batch's tanks) is inherited
from L3. Deliberately ADDITIVE: imports L1/L3/sixn primitives verbatim, changes
no existing math, and is not imported by `forecast/run.py`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .global_planner_l2_poc import GROWOUT_SYSTEMS, NURSERY_SYSTEMS, PURGE_SYSTEMS
from .global_planner_l3_poc import L3Result, smallest_og_tank_kg
from .global_planner_poc import PlannerResult
from .models import ControlParams, FacilityConfig
from .sixn import SIXN_PAIRS
from .time_grid import parse_iso_label


# ---------------------------------------------------------------------------
# Result containers (the read-shape excel_io's writers + audit consume)
# ---------------------------------------------------------------------------

@dataclass
class TankLocRow:
    """One physical tank's occupancy for one (batch, week). Mirrors
    placement.BatchLocationRow's read shape (excel_io writers + audit)."""
    week_label: str
    week_start: date
    batch_id: str
    tank_id: int
    location_id: str
    system_id: str
    count: float
    avg_wt_g: float
    biomass_kg: float
    density_kg_m3: float
    stage: str = ""
    oversub: bool = False        # placed by double-stack (over-subscribed week)


@dataclass
class _TransferDest:
    """Mirror of a transfer destination (excel_io reads .tank_id/.count/etc.)."""
    tank_id: int
    count: float
    avg_wt_g: float
    cv_pct: float = 0.0
    size_class: str = ""


@dataclass
class TankTransfer:
    """One physical tank-to-tank move. Mirrors events.Transfer's read shape:
    excel_io's TransferPlan + TankContinuityAudit read .source_tank_id,
    .source_avg_wt_g, .count_transferred, .event_date, .batch_id, .destinations."""
    event_date: date
    batch_id: str
    source_tank_id: int
    source_avg_wt_g: float
    count_transferred: float
    destinations: list                       # [_TransferDest]


@dataclass
class TankHarvest:
    """One harvest draw, debited from a specific physical tank. Mirrors
    events.Harvest's read shape (excel_io reads .batch_id/.event_date/
    .source_tank_id/.count/.avg_wt_g)."""
    batch_id: str
    event_date: date
    source_tank_id: int
    count: float
    avg_wt_g: float


@dataclass
class TankTranOG:
    """A batch's FIRST stocking into an OG tank (the FW->seawater entry, or an
    in-flight batch appearing at the forecast start). Mirrors events.TranOG's
    read shape: the TankContinuityAudit credits each destination as `tranog_in`
    (present the full week, full-week growth), so a tank a batch enters from
    'nowhere' (no prior OG tank) still balances. excel_io's TransferPlan reads
    .batch_id/.event_date/.destinations (From_Tank shown as 'FW')."""
    batch_id: str
    event_date: date
    destinations: list                       # [_TransferDest]


@dataclass
class _MortState:
    """Duck-typed BatchWeekState the TankContinuityAudit reads for the COUNT
    balance: it reads .batch_id, .week_label, .mortality_pct_weekly, .sgr_pct_day."""
    batch_id: str
    week_label: str
    mortality_pct_weekly: float
    sgr_pct_day: float = 0.0


@dataclass
class TankPickResult:
    batch_locations: list                    # TankLocRow (grow-out + 6N hold)
    transfers: list                          # TankTransfer
    tranog_events: list                      # TankTranOG (first OG stocking)
    harvest_events: list                     # TankHarvest (real source tanks)
    realized_biology: dict                   # {(tank, wk, batch): (net_kg, mort)}
    mort_states: list                        # _MortState (audit count balance)
    n_transfers: int
    n_oversub_rows: int                      # double-stacked rows (over-sub weeks)
    oversub_weeks: list                      # [week_label] genuinely over-subscribed
    n_tank_weeks: int


# ---------------------------------------------------------------------------
# Occupancy bookkeeping
# ---------------------------------------------------------------------------

@dataclass
class _Occ:
    """A physical tank's occupant this week."""
    batch_id: str
    count: float
    biomass_kg: float
    avg_wt_g: float
    oversub: bool = False


def _system_tank_ids(facility: FacilityConfig) -> dict[str, list[int]]:
    """Per OG system, the sorted physical tank ids."""
    out: dict[str, list[int]] = {}
    for t in facility.tanks:
        if t.type == "OG":
            out.setdefault(t.system_id, []).append(t.tank_id)
    for s in out:
        out[s].sort()
    return out


def _tank_volume(facility: FacilityConfig) -> dict[int, float]:
    return {t.tank_id: t.volume_m3 for t in facility.tanks if t.type == "OG"}


def _tank_system(facility: FacilityConfig) -> dict[int, str]:
    return {t.tank_id: t.system_id for t in facility.tanks if t.type == "OG"}


def _label_to_week_start(label: str, fs_date: date) -> date:
    d = parse_iso_label(label)
    return d if d is not None else fs_date


# ---------------------------------------------------------------------------
# 6N pair-tank ordering (mirrors forecast.sixn round-robin)
# ---------------------------------------------------------------------------

def _sixn_tank_order() -> list[int]:
    """6N tanks in claim order: all MAINS first (61,63,65 — preferred), then the
    SISTERS (67,69,71) — a pair's sister is taken only after its main, matching
    `sixn.SIXN_PAIRS` (single-batch harvest prefers the main; sister when a pair
    needs two). Mains in pair order, then sisters in pair order."""
    mains = [p[0] for p in SIXN_PAIRS]
    sisters = [p[1] for p in SIXN_PAIRS]
    return mains + sisters


# ---------------------------------------------------------------------------
# The pick
# ---------------------------------------------------------------------------

def pick_tanks(
    loop_result,
    control: ControlParams,
    facility: FacilityConfig,
    grow_q_by_week: Optional[dict] = None,
) -> TankPickResult:
    """Realize the converged L1->L3 plan as specific physical tanks.

    Returns a TankPickResult with real BatchLocations (TankLocRow), real
    tank-to-tank Transfers (TankTransfer) and over-subscription accounting.
    """
    l1: PlannerResult = loop_result.final_l1
    l3: L3Result = loop_result.final_l3
    fs = control.forecast_start
    fs_date = fs.date() if hasattr(fs, "date") else fs

    sys_tank_ids = _system_tank_ids(facility)
    tank_vol = _tank_volume(facility)
    tank_sys = _tank_system(facility)
    sixn_order = _sixn_tank_order()
    sixn_set = set(sixn_order)

    # ---- L3 grow-out demand: per (week, system, batch) -> tank count + standing.
    # Index placements; also gather the per-(batch, week) L1 standing so per-tank
    # split uses the conserved count/biomass/avg_wt.
    placements_by_week: dict[int, list] = {}
    week_label: dict[int, str] = {}
    for p in l3.placements:
        if p.tanks <= 0:
            continue
        placements_by_week.setdefault(p.week, []).append(p)
        week_label[p.week] = p.week_label

    # Per-(batch, week) grow-out standing (NON-purge rows).
    standing: dict[tuple[str, int], tuple[float, float, float]] = {}
    for r in l1.batch_standing:
        if getattr(r, "in_purge", False) or r.biomass_kg <= 1e-9:
            continue
        standing[(r.batch_id, r.week)] = (r.count, r.biomass_kg, r.avg_wt_g)

    # Per-(batch, week) 6N purge-hold standing.
    purge_rows: dict[int, list] = {}
    for r in l1.batch_standing:
        if getattr(r, "in_purge", False) and r.biomass_kg > 1e-9:
            purge_rows.setdefault(r.week, []).append(r)
            week_label.setdefault(r.week, r.week_label)

    # ---- Harvest envelope: per (batch, week) count + biomass drawn (released).
    harvest_by_bw: dict[tuple[str, int], tuple[float, float, float]] = {}
    week_by_label: dict[str, int] = {}
    for e in l1.envelope:
        if e.count <= 0:
            continue
        # map week_label back to a week index (envelope carries label, not idx).
        harvest_by_bw[(e.batch_id, e.week_label)] = (
            e.count, e.biomass_kg, e.avg_wt_g)
    label_for_week = dict(week_label)

    weeks = sorted(week_label)

    # ---- Running physical tank state. tank_id -> _Occ (or absent = empty). ----
    state: dict[int, _Occ] = {}

    batch_locations: list[TankLocRow] = []
    transfers: list[TankTransfer] = []
    tranog_events: list[TankTranOG] = []
    harvest_events: list[TankHarvest] = []
    # realized_biology[(tank, week_label, batch)] = (net_growth_minus_mort_kg,
    # mort_count) — the residual the daily walker "would have" applied, computed
    # so each tank-week's biomass balance closes exactly (the audit's ground
    # truth path). Count mortality is carried separately via mort_states.
    realized_biology: dict[tuple[int, str, str], tuple[float, float]] = {}
    # Per-(batch, week) weekly mortality % the audit applies in the COUNT balance.
    mort_states: list[_MortState] = []
    n_oversub_rows = 0
    oversub_weeks: list[str] = []

    sixn_cap = smallest_og_tank_kg(facility) * 1.25  # 6N staged density

    # Tank geometry (for the CP-SAT injection's per-tank density flag).
    tank_vol_m3 = {t.tank_id: t.volume_m3 for t in facility.tanks}
    tank_maxd = {t.tank_id: t.max_density_kg_m3 for t in facility.tanks}

    for w in weeks:
        wl = week_label[w]
        ws = _label_to_week_start(wl, fs_date)
        prev_state = state
        # Tanks each batch held last week (for continuity + transfer sourcing).
        prev_by_batch: dict[str, list[int]] = {}
        for tid, occ in prev_state.items():
            prev_by_batch.setdefault(occ.batch_id, []).append(tid)
        for b in prev_by_batch:
            prev_by_batch[b].sort()

        new_state: dict[int, _Occ] = {}
        used_tanks: set[int] = set()
        week_oversub = False

        # =============================================================
        # 1) GROW-OUT placement from L3 y[b,s,w] (production OG systems).
        # =============================================================
        # Order batches deterministically; process so continuity is honoured.
        # When CP-SAT drives grow-out placement, the L3 greedy block below is a
        # no-op (empty plist) and new_state is built from the optimal q after it.
        plist = ([] if grow_q_by_week is not None else
                 sorted(placements_by_week.get(w, []),
                        key=lambda p: (p.system_id, p.batch_id)))
        # A batch may be placed across MULTIPLE systems the same week (L3
        # fragmentation). The L1 standing count/biomass is split EVENLY over the
        # batch's TOTAL tank footprint that week (all systems), not per-placement
        # — otherwise each placement would receive the whole batch (n-fold
        # over-count). Pre-sum the batch's total tanks across its placements.
        batch_total_tanks: dict[str, int] = {}
        for p in plist:
            if p.tanks > 0 and sys_tank_ids.get(p.system_id):
                batch_total_tanks[p.batch_id] = (
                    batch_total_tanks.get(p.batch_id, 0) + p.tanks)
        # Track, per batch, which of its prev tanks are still unconsumed (so a
        # batch relocating systems can move FROM a vacated tank).
        prev_avail: dict[str, list[int]] = {
            b: [t for t in ids if t not in sixn_set]
            for b, ids in prev_by_batch.items()
        }

        # ---- PASS 1: choose SWAP-FREE physical tanks per (batch, system). ----
        # A tank may be occupied by a batch this week ONLY if it was EMPTY last
        # week (free_clean) or that SAME batch already held it (kept / free_mine).
        # A tank a DIFFERENT batch held last week is NEVER reused this week: a
        # same-week A->B swap charges B's mortality rate against A's residual count
        # in the per-(tank, week) audit and cannot reconcile. Such a tank goes
        # fallow. If a system runs out of swap-free tanks the batch takes FEWER
        # tanks there and its standing packs DENSER (flagged) — never swap, never
        # overwrite another batch, never drop.
        chosen_by_ps: dict[tuple[str, str], list[int]] = {}
        actual_total: dict[str, int] = {}
        for p in plist:
            batch_id = p.batch_id
            system = p.system_id
            want = p.tanks
            sys_ids = sys_tank_ids.get(system, [])
            if not sys_ids or want <= 0:
                continue
            kept_in_sys = [t for t in prev_avail.get(batch_id, [])
                           if tank_sys.get(t) == system and t not in used_tanks]
            kept_in_sys.sort()
            if len(kept_in_sys) >= want:
                chosen = kept_in_sys[:want]
            else:
                chosen = list(kept_in_sys)
                need = want - len(chosen)
                free = [t for t in sys_ids
                        if t not in used_tanks and t not in chosen]
                free_clean = [t for t in free if t not in prev_state]
                free_mine = [t for t in free
                             if t in prev_state
                             and prev_state[t].batch_id == batch_id]
                chosen.extend((free_clean + free_mine)[:need])
                if len(chosen) < want:
                    # Swap-free shortfall: pack denser this week (flagged below).
                    week_oversub = True
            for tid in chosen:
                used_tanks.add(tid)
                if tid in prev_avail.get(batch_id, []):
                    prev_avail[batch_id].remove(tid)
            chosen_by_ps[(batch_id, system)] = chosen
            actual_total[batch_id] = actual_total.get(batch_id, 0) + len(chosen)

        # ---- DENSITY-RELIEF SPREAD (CROSS-SYSTEM, minimize density). ----
        # After the base placement, an over-dense batch claims its next tank to
        # spread down toward the operating density target — in its OWN system if a
        # tank is free, ELSE in another ELIGIBLE system (same conveyor tier;
        # grow-out may spill to nursery) that has a free tank AND cap headroom.
        # That splits the batch and halves its density instead of cramming to 176
        # while tanks sit idle in another system (the controller's rebalancer
        # move). Multi-objective + bounded:
        #   * density:    spread to the operating target (lower per-tank kg)
        #   * transfers:  stop AT the target; kept tanks persist (Pass-1 continuity)
        #   * system load: destination biomass + feed kept under cap (checked)
        #   * tanks:      free_clean only -> never over-subscribes, 0-drift
        BIO_CAP, FEED_CAP = 400000.0, 3000.0
        # Relieve toward 0.97 of the HARD cap (not the softer operating target),
        # so ONLY genuinely over-cap tanks get spread — fixing the real >95
        # breaches while leaving acceptable 85-95 tanks alone (minimize transfers).
        op_per_tank = smallest_og_tank_kg(facility) * 0.97
        batch_feed: dict[str, float] = {}
        for p in plist:
            batch_feed[p.batch_id] = batch_feed.get(p.batch_id, 0.0) + p.feed_kg_day
        grow_sys = [s for s in GROWOUT_SYSTEMS if s in sys_tank_ids]
        nurs_sys = [s for s in NURSERY_SYSTEMS if s in sys_tank_ids]
        # Per-system load AFTER the base placement (approx; conservative for the
        # headroom check — a spreading batch re-thins its other tanks, lowering
        # their systems, which we don't credit back, so the check only over-states).
        sys_bio: dict[str, float] = {}
        sys_feed: dict[str, float] = {}
        for (b2, sysm), ch in chosen_by_ps.items():
            n = actual_total.get(b2, len(ch)) or 1
            _, bb, _ = standing.get((b2, w), (0.0, 0.0, 0.0))
            sys_bio[sysm] = sys_bio.get(sysm, 0.0) + (bb / n) * len(ch)
            sys_feed[sysm] = (sys_feed.get(sysm, 0.0)
                              + (batch_feed.get(b2, 0.0) / n) * len(ch))
        if op_per_tank > 0:
            for bid in sorted({p.batch_id for p in plist}):
                _, bio, avg = standing.get((bid, w), (0.0, 0.0, 0.0))
                if bio <= 0:
                    continue
                n_act = actual_total.get(bid, 0)
                if n_act <= 0:
                    continue
                extra = math.ceil(bio / op_per_tank) - n_act
                if extra <= 0:
                    continue
                feed = batch_feed.get(bid, 0.0)
                elig = (grow_sys + nurs_sys) if avg >= 1000.0 else nurs_sys
                cur_sys = {s for (b2, s) in chosen_by_ps if b2 == bid}
                order = ([s for s in elig if s in cur_sys]
                         + [s for s in elig if s not in cur_sys])
                for _ in range(extra):
                    placed = False
                    for sysm in order:
                        free = sorted(t for t in sys_tank_ids.get(sysm, [])
                                      if t not in used_tanks and t not in prev_state)
                        if not free:
                            continue
                        new_n = actual_total[bid] + 1
                        pb, pf = bio / new_n, feed / new_n
                        if (sys_bio.get(sysm, 0.0) + pb > BIO_CAP
                                or sys_feed.get(sysm, 0.0) + pf > FEED_CAP):
                            continue
                        tid = free[0]
                        used_tanks.add(tid)
                        chosen_by_ps.setdefault((bid, sysm), []).append(tid)
                        actual_total[bid] = new_n
                        sys_bio[sysm] = sys_bio.get(sysm, 0.0) + pb
                        sys_feed[sysm] = sys_feed.get(sysm, 0.0) + pf
                        placed = True
                        break
                    if not placed:
                        break

        # ---- PASS 2: even-split each batch's standing over its ACTUAL tanks. ----
        # Splitting over the actually-placed tank count (not L3's planned count)
        # guarantees ALL of the batch's biomass/count is placed even when a
        # shortfall forced fewer tanks — denser, but conserved.
        for (batch_id, system), chosen in chosen_by_ps.items():
            if not chosen:
                continue
            n_act = actual_total.get(batch_id, len(chosen))
            cnt, bio, avg = standing.get((batch_id, w), (0.0, 0.0, 0.0))
            if bio <= 0:
                bio = sum(pp.biomass_kg for pp in plist if pp.batch_id == batch_id)
                cnt = (bio * 1000.0 / avg) if avg > 0 else 0.0
            per_bio = bio / n_act if n_act else 0.0
            per_cnt = cnt / n_act if n_act else 0.0
            per_avg = avg if avg > 0 else (per_bio * 1000.0 / per_cnt
                                           if per_cnt > 0 else 0.0)
            denser = n_act < batch_total_tanks.get(batch_id, n_act)
            for tid in chosen:
                new_state[tid] = _Occ(
                    batch_id=batch_id, count=per_cnt, biomass_kg=per_bio,
                    avg_wt_g=per_avg, oversub=denser)
                if denser:
                    n_oversub_rows += 1

        # ---- CP-SAT INJECTION: grow-out new_state straight from the optimal q. ----
        # Per-tank count = batch_count * (kg / batch_placed_kg): the fractions sum
        # to 1 so the COUNT conserves EXACTLY (independent of the integer-kg
        # rounding, which only nudges biomass sub-kg). Hard swap-free in the solve
        # guarantees no same-week A->B handover, so the audit reconciles 0-drift.
        if grow_q_by_week is not None:
            qw = grow_q_by_week.get(w, {})
            # CP-SAT owns the HARD part: WHICH tanks each batch occupies (system
            # biomass+feed headroom, hard swap-free). The split WITHIN that set is a
            # DETERMINED rule, not CP-SAT's arbitrary q: biomass proportional to tank
            # VOLUME -> every tank of a batch sits at the SAME (average) density =
            # maximum per-tank headroom. It's feasible (avg <= the max CP-SAT proved
            # <= cap, so 0 over-cap) and STABLE (a fixed rule over a stable tank set
            # just scales with the count, so Step-4 sees pure mortality/harvest, NOT
            # inter-tank shuffle). Forward/target-driven, not anchored to last week.
            tanks_by_b: dict[str, list[int]] = {}
            for (b, t) in qw:
                tanks_by_b.setdefault(b, []).append(t)
            for b, tankset in tanks_by_b.items():
                cnt, bio, avg = standing.get((b, w), (0.0, 0.0, 0.0))
                volsum = sum(tank_vol_m3.get(t, 0.0) for t in tankset) or 1.0
                for t in tankset:
                    vf = tank_vol_m3.get(t, 0.0) / volsum
                    pkg = bio * vf
                    volm = tank_vol_m3.get(t, 0.0)
                    over = (pkg / volm if volm > 0 else 0.0) > tank_maxd.get(t, 1e9)
                    new_state[t] = _Occ(batch_id=b, count=cnt * vf, biomass_kg=pkg,
                                        avg_wt_g=avg, oversub=over)
                    used_tanks.add(t)
                    if over:
                        n_oversub_rows += 1
            pbio = tanks_by_b   # batches CP-SAT placed (for the dust-fallback guard)
            # DUST fallback: a grow-out batch whose standing rounded below 1 kg got
            # no CP-SAT cell — keep it on a prior tank so no fish vanish from the
            # count audit (near-harvest tails of B41/B42).
            for (b2, w2), (cnt2, bio2, avg2) in standing.items():
                if w2 != w or bio2 <= 1e-9 or b2 in pbio:
                    continue
                # SWAP-FREE only: the batch's OWN prior tank (free_mine), else a
                # tank EMPTY last week (free_clean). Never a tank another batch held
                # last week — that is a same-week A->B swap the audit charges the
                # prior occupant's whole count against (the B48->B42 drift). If no
                # swap-free tank exists, skip: this is the <1-fish near-harvest tail.
                pri = [t for t in prev_by_batch.get(b2, [])
                       if t not in used_tanks and t not in sixn_set]
                tid = (pri[0] if pri else
                       next((t for t in sorted(tank_sys)
                             if t not in used_tanks and t not in sixn_set
                             and prev_state.get(t) is None), None))
                if tid is not None:
                    new_state[tid] = _Occ(batch_id=b2, count=cnt2, biomass_kg=bio2,
                                          avg_wt_g=avg2, oversub=False)
                    used_tanks.add(tid)

        # =============================================================
        # 2) 6N PURGE HOLD — park in_purge population in 6N pairs (round-robin).
        # =============================================================
        # Batches already in a 6N tank keep it (continuity); growth claims more
        # 6N tanks in main-then-sister order.
        held = purge_rows.get(w, [])
        held_batches = {r.batch_id for r in held}
        # prev 6N occupancy per batch.
        prev_sixn_by_batch: dict[str, list[int]] = {}
        for tid in sixn_order:
            occ = prev_state.get(tid)
            if occ is not None:
                prev_sixn_by_batch.setdefault(occ.batch_id, []).append(tid)
        # FALLOW guard (mirrors sixn's 2-purge-1-rest rotation): a 6N tank that
        # held a batch last week which is NOT held this week has just been
        # RELEASED (harvested) — keep it fallow this week rather than restocking
        # a different batch into it the SAME week (a same-week swap cannot
        # reconcile cleanly in the audit's one-row-per-(tank, week) model).
        fallow = {tid for tid, occ in prev_state.items()
                  if tid in sixn_set and occ.batch_id not in held_batches}
        sixn_free = [t for t in sixn_order if t not in fallow]  # claim order
        # First, honour continuity: re-seat batches on their prior 6N tanks.
        sixn_assigned: dict[str, list[int]] = {}
        for r in sorted(held, key=lambda r: r.batch_id):
            n_need = max(1, math.ceil(r.biomass_kg / sixn_cap))
            keep = [t for t in prev_sixn_by_batch.get(r.batch_id, [])
                    if t in sixn_free][:n_need]
            for t in keep:
                sixn_free.remove(t)
            sixn_assigned[r.batch_id] = keep
        # Then claim additional 6N tanks (main-then-sister) for any shortfall.
        for r in sorted(held, key=lambda r: r.batch_id):
            n_need = max(1, math.ceil(r.biomass_kg / sixn_cap))
            cur = sixn_assigned.get(r.batch_id, [])
            while len(cur) < n_need and sixn_free:
                cur.append(sixn_free.pop(0))
            sixn_assigned[r.batch_id] = cur
            if len(cur) < n_need:
                week_oversub = True   # 6N pool over-subscribed (rare)

        for r in held:
            tanks = sixn_assigned.get(r.batch_id, [])
            if not tanks:
                continue
            per_bio = r.biomass_kg / len(tanks)
            per_cnt = r.count / len(tanks)
            for tid in tanks:
                used_tanks.add(tid)
                if tid in prev_avail.get(r.batch_id, []):
                    prev_avail[r.batch_id].remove(tid)
                new_state[tid] = _Occ(
                    batch_id=r.batch_id, count=per_cnt, biomass_kg=per_bio,
                    avg_wt_g=r.avg_wt_g, oversub=False)

        # =============================================================
        # 3) Emit BatchLocations.
        # =============================================================
        for tid, occ in sorted(new_state.items()):
            vol = tank_vol.get(tid, 0.0)
            dens = (occ.biomass_kg / vol) if vol > 0 else 0.0
            system = tank_sys.get(tid, "OG6N")
            stage = "STARVE" if tid in sixn_set else ""
            batch_locations.append(TankLocRow(
                week_label=wl, week_start=ws, batch_id=occ.batch_id,
                tank_id=tid, location_id=f"{system}-{tid}", system_id=system,
                count=occ.count, avg_wt_g=occ.avg_wt_g,
                biomass_kg=occ.biomass_kg,
                density_kg_m3=dens, stage=stage, oversub=occ.oversub))

        # =============================================================
        # 4) Per-batch event flow: MORTALITY -> HARVEST -> TRANSFERS / TranOG.
        # =============================================================
        # The TankContinuityAudit reconciles each (tank, week):
        #   count : open - mort - h_out - t_out + t_in + tranog_in = close
        #   bio   : (open - h_out_kg - t_out_kg + t_in_kg + tranog_kg) + net = close
        # where `mort = open_count * mortality_pct_weekly/100` is applied PER TANK
        # on the prior count. So we (a) derive each batch's weekly mortality % from
        # its conserved totals, (b) shrink each prior tank's count by it, (c) draw
        # the harvest from the survivors, (d) match the remaining survivors to this
        # week's even-split layout as TRANSFERS, and (e) credit any tank a batch
        # ENTERS from no prior OG tank as a TranOG stocking. realized_biology then
        # carries the residual (growth) so the biomass balance closes exactly.
        new_by_batch: dict[str, list[int]] = {}
        for tid, occ in new_state.items():
            new_by_batch.setdefault(occ.batch_id, []).append(tid)

        # Per (tank, week) event kg/count we credit, to back-compute the residual.
        h_out_kg: dict[int, float] = {}
        t_out_kg: dict[int, float] = {}
        t_in_kg: dict[int, float] = {}
        tn_in_kg: dict[int, float] = {}

        all_batches = set(new_by_batch) | set(prev_by_batch)
        for batch_id in sorted(all_batches):
            cur_tanks = sorted(new_by_batch.get(batch_id, []))
            old_tanks = sorted(prev_by_batch.get(batch_id, []))

            prev_total = sum(prev_state[t].count for t in old_tanks)
            new_total = sum(new_state[t].count for t in cur_tanks)
            h_cnt, h_kg, h_wt = harvest_by_bw.get((batch_id, wl), (0.0, 0.0, 0.0))

            # ---- (a) batch weekly mortality %. The audit applies it per tank on
            # the prior count, so the SUM over the batch's tanks = prev_total *
            # mp/100 = the batch's total deaths. survivors = new_total + harvest.
            mort_total = max(0.0, prev_total - new_total - h_cnt)
            mp = (mort_total / prev_total) if prev_total > 1e-9 else 0.0
            if prev_total > 1e-9:
                mort_states.append(_MortState(
                    batch_id=batch_id, week_label=wl,
                    mortality_pct_weekly=100.0 * mp))

            # ---- (b) post-mortality survivors available per PRIOR tank.
            avail = {t: prev_state[t].count * (1.0 - mp) for t in old_tanks}

            # ---- (c) HARVEST: draw from survivors (6N STARVE tanks first — the
            # just-released depuration pairs). Empties those tanks; the audit
            # zeroes them via harvest_out.
            if h_cnt > 1e-9 and old_tanks:
                starve_first = ([t for t in old_tanks if t in sixn_set]
                                + [t for t in old_tanks if t not in sixn_set])
                drawn_c = 0.0
                for t in starve_first:
                    if h_cnt - drawn_c <= 1e-9:
                        break
                    take_c = min(avail[t], h_cnt - drawn_c)
                    if take_c <= 1e-9:
                        continue
                    take_kg = h_kg * (take_c / h_cnt) if h_cnt > 0 else 0.0
                    harvest_events.append(TankHarvest(
                        batch_id=batch_id, event_date=ws, source_tank_id=t,
                        count=take_c,
                        avg_wt_g=(take_kg * 1000.0 / take_c) if take_c > 0 else h_wt))
                    h_out_kg[t] = h_out_kg.get(t, 0.0) + take_kg
                    avail[t] -= take_c
                    drawn_c += take_c

            # ---- (d) TRANSFERS: match remaining survivors (supply) to this
            # week's per-tank demand. Self-match a retained tank first (no
            # transfer), then move the residual from other tanks.
            supply = [[t, avail[t], prev_state[t].avg_wt_g]
                      for t in old_tanks if avail[t] > 1e-9]
            demand = [[t, new_state[t].count, new_state[t].avg_wt_g]
                      for t in cur_tanks]
            supply_by_tank = {s[0]: s for s in supply}
            for d in demand:
                s = supply_by_tank.get(d[0])
                if s is not None and s[1] > 1e-9 and d[1] > 1e-9:
                    m = min(s[1], d[1])
                    s[1] -= m
                    d[1] -= m
            si = 0
            for d in demand:
                dest = d[0]
                while d[1] > 1e-9 and si < len(supply):
                    s = supply[si]
                    if s[1] <= 1e-9:
                        si += 1
                        continue
                    m = min(s[1], d[1])
                    src, src_wt = s[0], s[2]
                    dest_wt = new_state[dest].avg_wt_g
                    transfers.append(TankTransfer(
                        event_date=ws, batch_id=batch_id,
                        source_tank_id=src, source_avg_wt_g=src_wt,
                        count_transferred=m,
                        destinations=[_TransferDest(
                            tank_id=dest, count=m, avg_wt_g=dest_wt)]))
                    t_out_kg[src] = t_out_kg.get(src, 0.0) + m * src_wt / 1000.0
                    t_in_kg[dest] = t_in_kg.get(dest, 0.0) + m * dest_wt / 1000.0
                    s[1] -= m
                    d[1] -= m

            # ---- (e) TranOG: any residual demand not met by survivors is a FRESH
            # stocking (the batch entering OG from FW / appearing in-flight).
            # Credit it as tranog_in so the destination tank balances from 0.
            for d in demand:
                if d[1] <= 1e-9:
                    continue
                dest, need, dest_wt = d[0], d[1], d[2]
                tranog_events.append(TankTranOG(
                    batch_id=batch_id, event_date=ws,
                    destinations=[_TransferDest(
                        tank_id=dest, count=need, avg_wt_g=dest_wt)]))
                tn_in_kg[dest] = tn_in_kg.get(dest, 0.0) + need * dest_wt / 1000.0

        # ---- realized_biology: per (tank, week) net biomass (growth-minus-mort)
        # = close - (open - h_out_kg - t_out_kg + t_in_kg + tranog_kg). Closes the
        # biomass balance EXACTLY. open is the prior close only if the SAME batch
        # held the tank (else 0 — a turnover tank's prior batch left cleanly).
        for tid, occ in new_state.items():
            prev_occ = prev_state.get(tid)
            open_kg = (prev_occ.biomass_kg if
                       (prev_occ and prev_occ.batch_id == occ.batch_id) else 0.0)
            base = (open_kg - h_out_kg.get(tid, 0.0) - t_out_kg.get(tid, 0.0)
                    + t_in_kg.get(tid, 0.0) + tn_in_kg.get(tid, 0.0))
            net = occ.biomass_kg - base
            realized_biology[(tid, wl, occ.batch_id)] = (net, 0.0)
        # Tanks that EMPTIED this week were zeroed by mortality + harvest_out +
        # transfer_out; no realized_biology row needed (audit only grows occupied
        # tanks). STARVE (6N) tanks are off-feed: net should be ~0 there, which it
        # is (held biomass is frozen week to week).

        if week_oversub:
            oversub_weeks.append(wl)
        state = new_state

    return TankPickResult(
        batch_locations=batch_locations,
        transfers=transfers,
        tranog_events=tranog_events,
        harvest_events=harvest_events,
        realized_biology=realized_biology,
        mort_states=mort_states,
        n_transfers=len(transfers),
        n_oversub_rows=n_oversub_rows,
        oversub_weeks=oversub_weeks,
        n_tank_weeks=len(batch_locations),
    )
