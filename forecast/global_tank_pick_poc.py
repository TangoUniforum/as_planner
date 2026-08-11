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
from .sixn import SIXN_PAIRS, is_purge_mode
from .tiers import SIXN_SYSTEM, is_entry, move_allowed
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
    unplaced_warnings: list = field(default_factory=list)   # LOUD never-drop misses
    topology_warnings: list = field(default_factory=list)   # R1-R7 breaches emitted


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
    initial_tank_state=None,
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
    # DIAGNOSTIC (6N-only-in-purge probe): total harvest count L1 demanded during
    # purge that could NOT be sourced from 6N tanks (the overflow that today leaks
    # to production tanks, violating the 6N-only rule).
    _purge_h_demand = 0.0
    _purge_h_from6n = 0.0
    _purge_h_shortfall = 0.0
    # DEPURATION RESIDENCY. The 6N pool is a BATCH process, not a pass-through
    # buffer: fish must sit off-feed for _PURGE_HOLD_WEEKS before harvest, which
    # is a product requirement, not a scheduling preference. L1's envelope
    # honours it (it releases fish `hold` weeks after the draw), but this pick
    # never did — it drew from whichever 6N tank had fish, including ones filled
    # the same week. Measured on a real PR: 96% of harvested fish (3.43M of
    # 3.56M) left 6N before completing the hold, 284 draws with ZERO residency.
    # That is what made the global plan look smoother than the controller's —
    # it was skipping the two-week pipeline lag the controller must live with.
    # `sixn_arrival[tank] = week index the current occupancy arrived`.
    sixn_arrival: dict[int, int] = {}
    # Same constant L1 plans against — imported, not restated, so the pick and
    # the envelope can never disagree about the hold length.
    from .global_planner_poc import _PURGE_HOLD_WEEKS as _HOLD
    _hold_weeks = int(_HOLD or 2)
    _hold_short_fish = _hold_total_fish = 0.0
    _hold_short_draws = _hold_total_draws = 0

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

    # Per-(batch, week) grow-out standing (NON-purge rows). A batch can carry TWO
    # non-purge rows in one week: its on-feed population AND its off-feed
    # in-place production hold (L1 step 8b, sixn=False). Both physically occupy
    # grow-out tanks, so SUM them (count + biomass) rather than overwrite — else
    # the pick places only one and the other vanishes from every tank (a large
    # production-mode TANK_DRIFT). avg_wt is the blended (biomass-weighted) mean.
    standing: dict[tuple[str, int], tuple[float, float, float]] = {}
    for r in l1.batch_standing:
        if getattr(r, "in_purge", False) or r.biomass_kg <= 1e-9:
            continue
        key = (r.batch_id, r.week)
        prev = standing.get(key)
        if prev is None:
            standing[key] = (r.count, r.biomass_kg, r.avg_wt_g)
        else:
            c = prev[0] + r.count
            b = prev[1] + r.biomass_kg
            standing[key] = (c, b, (b * 1000.0 / c if c > 1e-9 else r.avg_wt_g))

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
    if initial_tank_state is not None:
        # Seed the opening week's PRIOR occupancy from a handed-over tank state
        # (the manual override window's CLOSE). The first planned week then
        # CONTINUES those batches — the placement anchors their prior tanks and
        # emits transfers for any relocation — instead of re-stocking them from
        # empty via TranOG. This makes the manual->global hand-off reconcile in
        # the TankContinuityAudit rather than reading as vanish (manual tanks) +
        # restock (global TranOG). Empty run (no manual window) -> unchanged.
        for tid, tank in initial_tank_state.tanks_by_id.items():
            if getattr(tank, "is_empty", False) or tank.count <= 0:
                continue
            state[tid] = _Occ(
                batch_id=tank.batch_id, count=tank.count,
                biomass_kg=tank.biomass_kg,
                avg_wt_g=(tank.biomass_kg * 1000.0 / tank.count
                          if tank.count > 0 else 0.0))

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
    # Batches L1 seeded that the pick could not give ANY legal tank (Pass 1c).
    unplaced_warnings: list[str] = []
    # Emitted transfers that break the R1-R7 conveyor topology.
    topology_warnings: list[str] = []

    sixn_cap = smallest_og_tank_kg(facility) * 1.25  # 6N staged density

    # Tank geometry (for the CP-SAT injection's per-tank density flag).
    tank_vol_m3 = {t.tank_id: t.volume_m3 for t in facility.tanks}
    tank_maxd = {t.tank_id: t.max_density_kg_m3 for t in facility.tanks}

    from .sixn import SIXN_MAIN_TANKS, SIXN_SISTER_TANKS

    for w in weeks:
        wl = week_label[w]
        ws = _label_to_week_start(wl, fs_date)
        # 6N tanks that may NOT hold grow-out production fish this week.
        # PURGE mode: all six (mains are depuration staging, sisters are harvest
        # housing). PRODUCTION mode: only the 3 sisters — the 3 MAINS become
        # ordinary grow-out tanks (the facility's 33 -> 36). Previously this was
        # the unconditional `sixn_set` in both modes, so the pick could never use
        # the mains L3 had just been taught to allocate.
        _blocked6n = (set(SIXN_MAIN_TANKS) | set(SIXN_SISTER_TANKS)
                      if is_purge_mode(control, ws) else set(SIXN_SISTER_TANKS))

        def _growout_ids(system, _b=_blocked6n):
            """Tanks in `system` that may take grow-out fish this week."""
            return [t for t in sys_tank_ids.get(system, []) if t not in _b]

        # 6N tanks actually holding OFF-FEED DEPURATION fish this week (filled by
        # the purge round-robin below). STARVE is a STATE, not a tank id: in 6N
        # production mode the mains rear ordinary grow-out fish and must not be
        # stamped STARVE — that would hide them from every density/welfare metric
        # (which excludes purge rows) and misreport the depuration hold.
        _depurating: set[int] = set()

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
            b: [t for t in ids if t not in _blocked6n]
            for b, ids in prev_by_batch.items()
        }

        # ---- PASS 0: CONTINUITY ANCHOR (never-drop). Reserve each standing
        # batch's OWN prior non-6N tanks FIRST — before the per-system greedy below
        # can hand them to another batch and strand this one. That peak-week
        # starvation dropped whole batches for weeks (e.g. B62 unplaced W27-W34
        # while every physical tank was taken). Anchoring is conflict-free — a tank
        # has exactly one prior occupant — so it also PREVENTS the illegal same-week
        # A->B swap L3 implicitly asks for when it relocates a batch to a new
        # system. Anchors count toward the batch's L3 tank demand (only the
        # shortfall is filled in Pass 1) and register under each tank's ACTUAL
        # system so Pass 2's even-split covers them.
        chosen_by_ps: dict[tuple[str, str], list[int]] = {}
        actual_total: dict[str, int] = {}
        want_by_batch = dict(batch_total_tanks)
        for bid in sorted(want_by_batch):
            sc, _, _ = standing.get((bid, w), (0.0, 0.0, 0.0))
            if sc <= 1e-9:
                continue
            # Keep the FORWARD-most tanks first. A batch that straddles tiers
            # (common straight out of the operator's manual window, e.g. B45 held
            # OG1N/OG1S/OG2S and OG3S/OG6S at the W33 handoff) has to shrink its
            # footprint somewhere; retaining by bare tank id kept the low-numbered
            # ENTRY tanks and drained the grow-out ones, so the consolidation had
            # to run grow-out -> entry, which R4 forbids at any weight. Keeping
            # the non-entry tanks instead makes the same consolidation run
            # entry -> grow-out, which R2 allows at any weight. Same tank COUNT,
            # same biomass, legal direction.
            mine = sorted((t for t in prev_avail.get(bid, []) if t not in used_tanks),
                          key=lambda t: (is_entry(tank_sys.get(t, "")), t))
            keep = mine[:max(1, want_by_batch.get(bid, 1))]
            for tid in keep:
                used_tanks.add(tid)
                prev_avail[bid].remove(tid)
                chosen_by_ps.setdefault((bid, tank_sys.get(tid)), []).append(tid)
                actual_total[bid] = actual_total.get(bid, 0) + 1

        # ---- PASS 1: fill each batch UP TO its L3 tank demand with SWAP-FREE tanks
        # in L3's preferred system — EMPTY last week (free_clean) or the SAME batch's
        # (free_mine); a tank a DIFFERENT batch held last week is never reused (a
        # same-week swap cannot reconcile in the per-(tank, week) audit). Pass-0
        # anchors already count toward actual_total, so only the total SHORTFALL is
        # placed here — a batch that kept enough prior tanks takes none. A system
        # out of swap-free tanks packs denser (flagged) — never swap, never
        # overwrite another batch, never drop.
        for p in plist:
            batch_id = p.batch_id
            system = p.system_id
            sys_ids = _growout_ids(system)
            if not sys_ids or p.tanks <= 0:
                continue
            # R4 (never backward), MONOTONE: once any of this batch's fish sit
            # outside the entry tier, no part of it may be sent back into OG1/2.
            # L3 plans in system COUNTS and can legally hand a batch back to a
            # nursery system in a later week; realizing that would emit a
            # grow-out -> entry move, which rule R4 forbids at any weight. Skip
            # the entry destination and let Pass 1b/1c find it a forward home.
            if (is_entry(system)
                    and any(not is_entry(tank_sys.get(t, ""))
                            for t in prev_by_batch.get(batch_id, []))):
                continue
            need = want_by_batch.get(batch_id, p.tanks) - actual_total.get(batch_id, 0)
            if need <= 0:
                continue
            free = [t for t in sys_ids if t not in used_tanks]
            free_clean = [t for t in free if t not in prev_state]
            free_mine = [t for t in free
                         if t in prev_state and prev_state[t].batch_id == batch_id]
            add = (free_clean + free_mine)[:need]
            for tid in add:
                used_tanks.add(tid)
                if tid in prev_avail.get(batch_id, []):
                    prev_avail[batch_id].remove(tid)
            if add:
                chosen_by_ps.setdefault((batch_id, system), []).extend(add)
                actual_total[batch_id] = actual_total.get(batch_id, 0) + len(add)
            if actual_total.get(batch_id, 0) < want_by_batch.get(batch_id, p.tanks):
                week_oversub = True

        # ---- DENSITY-RELIEF SPREAD (CROSS-SYSTEM, minimize density). ----
        # After the base placement, an over-dense batch claims its next tank to
        # spread down toward the operating density target — in its OWN system if a
        # tank is free, ELSE in another ELIGIBLE system (same conveyor tier;
        # NO grow-out -> nursery spill, rule R4) that has a free tank AND cap headroom.
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
        if _growout_ids(SIXN_SYSTEM):
            grow_sys = grow_sys + [SIXN_SYSTEM]
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
                # R4 (never backward): >=1 kg batches may spread to grow-out
                # systems ONLY — no nursery (entry-tier) spill at any weight.
                # A <1 kg batch relieves into the entry tier first, then FORWARD
                # into grow-out: rule R2 allows an entry-tier cohort to move to
                # any OG3/4/5/6 tank "at ANY weight" (tiers.move_allowed returns
                # True for entry->grow-out regardless of weight). This is a
                # relief valve for an ALREADY-PLACED batch, not an entry rule:
                # R1 (arrivals enter the entry tier) is untouched, and R4 still
                # forbids ever coming back. Without the forward leg the entry
                # tier is a closed 12-tank box, and measurement showed that is
                # the whole density problem: 74 of 75 tank-weeks over the 95
                # cap were sub-1 kg fish stuck in OG1/OG2 at up to 187 kg/m3
                # while ~8 grow-out tanks sat free and eligible every one of
                # those weeks.
                elig = grow_sys if avg >= 1000.0 else (nurs_sys + grow_sys)
                # R4 (never backward): if this batch already holds a non-entry
                # tank, that tank can never send fish to an entry-tier tank, so
                # entry destinations are illegal for it however light the fish.
                if any(not is_entry(tank_sys.get(t, ""))
                       for t in prev_by_batch.get(bid, [])):
                    elig = [s2 for s2 in elig if not is_entry(s2)]
                cur_sys = {s for (b2, s) in chosen_by_ps if b2 == bid}
                order = ([s for s in elig if s in cur_sys]
                         + [s for s in elig if s not in cur_sys])
                for _ in range(extra):
                    placed = False
                    for sysm in order:
                        free = sorted(t for t in _growout_ids(sysm)
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

        # ---- PASS 1c: NEVER-DROP. A batch L1 seeded and L3 placed, but which
        # came out of Passes 0/1/1b with ZERO tanks, has no physical home: it
        # simply vanishes from BatchLocations while L1's batch-level
        # ReconciliationReport still reports it as standing and "conserving".
        # That is the dropped-batch class this project has hit before —
        # continuity audits are blind to a batch that was NEVER placed. Measured
        # here: B66 (570,000 fish) vanished at 2028-W49 while 13 of 39 tanks sat
        # empty and the facility had 400,000 kg of headroom.
        #
        # Claim ONE legal free tank per such batch, honouring the tier rules
        # (R1/R4: a <1 kg batch may only sit in the entry tier; a grow-out batch
        # may never move backward into it) and never taking a tank another batch
        # held last week (a same-week swap cannot reconcile in the per-tank
        # audit). If no legal tank exists the batch stays unplaced and we say so
        # LOUDLY — a genuine facility infeasibility is a finding to report, never
        # something to hide by over-stacking a tank past its density cap.
        for p in sorted(plist, key=lambda p: p.batch_id):
            bid = p.batch_id
            if actual_total.get(bid, 0) > 0:
                continue
            _, bio_u, avg_u = standing.get((bid, w), (0.0, 0.0, 0.0))
            if bio_u <= 1e-9:
                continue
            # Same tier model as L3 and the relief spread (R2 forward): a <1 kg
            # batch prefers the entry tier but may go FORWARD rather than have
            # nowhere to live. Leaving this one path nursery-only stranded B50
            # for 5 straight weeks while grow-out tanks stood empty.
            elig = (grow_sys if avg_u >= 1000.0 else (nurs_sys + grow_sys))
            # R4 (never backward) — same guard as the relief spread: a batch
            # with a non-entry tank last week may not be rescued INTO the entry
            # tier. Without this the never-drop pass emitted backward moves.
            if any(not is_entry(tank_sys.get(t, ""))
                   for t in prev_by_batch.get(bid, [])):
                elig = [s2 for s2 in elig if not is_entry(s2)]
                if not elig:
                    elig = grow_sys
            # Claim as many tanks as the DENSITY CAP requires, not just one — a
            # 118-tonne batch on a single 1720 m3 tank is 69 kg/m3 of honest
            # rescue turning into a cap breach the moment it grows.
            need_u = max(1, math.ceil(bio_u / op_per_tank)) if op_per_tank > 0 else 1
            free_u = [t for s in elig for t in _growout_ids(s)
                      if t not in used_tanks and t not in prev_state][:need_u]
            if not free_u:
                unplaced_warnings.append(
                    f"UNPLACED BATCH - {wl}: batch {bid} ({bio_u:,.0f} kg) has "
                    f"L1 standing but NO legal free tank in its tier "
                    f"({'nursery' if avg_u < 1000.0 else 'grow-out'}); it is "
                    f"absent from BatchLocations. This is a real placement "
                    f"infeasibility, not a rounding artifact.")
                continue
            if len(free_u) < need_u:
                unplaced_warnings.append(
                    f"UNPLACED BATCH - {wl}: batch {bid} ({bio_u:,.0f} kg) needed "
                    f"{need_u} tank(s) to stay under the density cap but only "
                    f"{len(free_u)} were legally free; it is placed DENSER than "
                    f"the cap rather than dropped.")
            for tid in free_u:
                used_tanks.add(tid)
                chosen_by_ps.setdefault((bid, tank_sys.get(tid)), []).append(tid)
            actual_total[bid] = len(free_u)

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
                # Split by the SOLVER's per-tank q (the BALANCED distribution it
                # optimized to hold the per-system caps), NOT evenly by volume. An
                # even split DISCARDS that balance: when a batch spans systems,
                # equal-per-tank piles biomass into a system the solver kept light,
                # pushing it over its cap — the pipeline's 107% residual even though
                # the solver's own layout had 0 over-cap slack. Scaling the solver's
                # q to the pick's exact `bio` keeps count + biomass conserved and the
                # per-tank density under the (solver-proved) cap.
                qsum = sum(qw.get((b, t), 0.0) for t in tankset) or 1.0
                for t in tankset:
                    qf = qw.get((b, t), 0.0) / qsum
                    pkg = bio * qf
                    volm = tank_vol_m3.get(t, 0.0)
                    over = (pkg / volm if volm > 0 else 0.0) > tank_maxd.get(t, 1e9)
                    new_state[t] = _Occ(batch_id=b, count=cnt * qf, biomass_kg=pkg,
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
                       if t not in used_tanks and t not in _blocked6n]
                tid = (pri[0] if pri else
                       next((t for t in sorted(tank_sys)
                             if t not in used_tanks and t not in _blocked6n
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
        # Claim order: DESCENDING biomass, so a large live depuration cohort is
        # seated before near-spent tails. A sub-1-fish tail claims NO 6N tank — it
        # is the tolerated near-harvest tail — so it cannot hog a whole tank via
        # continuity and strand a big cohort (the bug that dropped B51's ~60k fish
        # while three ZERO-count tails held tanks 61/69/71, mislabelling the loss
        # as mortality). n_need = ceil(biomass / sixn_cap) as before.
        claim_order = sorted(held, key=lambda r: -r.biomass_kg)
        sixn_assigned: dict[str, list[int]] = {}
        # First, honour continuity: re-seat live cohorts on their prior 6N tanks.
        for r in claim_order:
            if r.count < 1.0:
                sixn_assigned[r.batch_id] = []
                continue
            n_need = max(1, math.ceil(r.biomass_kg / sixn_cap))
            keep = [t for t in prev_sixn_by_batch.get(r.batch_id, [])
                    if t in sixn_free][:n_need]
            for t in keep:
                sixn_free.remove(t)
            sixn_assigned[r.batch_id] = keep
        # Then claim additional 6N tanks (main-then-sister) for any shortfall.
        for r in claim_order:
            if r.count < 1.0:
                continue
            n_need = max(1, math.ceil(r.biomass_kg / sixn_cap))
            cur = sixn_assigned.get(r.batch_id, [])
            while len(cur) < n_need and sixn_free:
                cur.append(sixn_free.pop(0))
            sixn_assigned[r.batch_id] = cur
            if len(cur) < n_need:
                week_oversub = True   # 6N pool genuinely over-subscribed (rare)

        for r in held:
            tanks = sixn_assigned.get(r.batch_id, [])
            if not tanks:
                continue
            _depurating.update(tanks)
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
            stage = "STARVE" if tid in _depurating else ""
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
        # Per PRIOR tank: the mortality COUNT the pick applied (prev count * mp).
        # The audit credits this (via realized_biology[..][1]); without it every
        # batch's weekly deaths read as negative drift.
        mort_ct_by_tank: dict[int, float] = {}

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

            # ---- (b) post-mortality survivors available per PRIOR tank. The
            # complement (prev * mp) is the deaths applied in that tank — record
            # it so the audit can credit mortality (else it reads as drift).
            avail = {t: prev_state[t].count * (1.0 - mp) for t in old_tanks}
            for t in old_tanks:
                mort_ct_by_tank[t] = prev_state[t].count * mp

            # ---- (c) HARVEST: draw from survivors (6N STARVE tanks first — the
            # just-released depuration pairs). Empties those tanks; the audit
            # zeroes them via harvest_out.
            if h_cnt > 1e-9 and old_tanks:
                _ws_date = ws.date() if hasattr(ws, "date") else ws
                _purge_wk = is_purge_mode(control, _ws_date)
                starve_first = ([t for t in old_tanks if t in sixn_set]
                                + [t for t in old_tanks if t not in sixn_set])
                drawn_c = 0.0
                _from6n = 0.0
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
                    if t in sixn_set:
                        _from6n += take_c
                        # AUDIT (not a gate): did these fish serve the purge?
                        _res = w - sixn_arrival.get(t, -10 ** 6)
                        if _res < _hold_weeks:
                            _hold_short_fish += take_c
                            _hold_short_draws += 1
                        _hold_total_fish += take_c
                        _hold_total_draws += 1
                if _purge_wk:
                    _purge_h_demand += drawn_c
                    _purge_h_from6n += _from6n
                    _purge_h_shortfall += max(0.0, drawn_c - _from6n)

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
            # The pairing itself must obey the conveyor topology. Choosing the
            # next supply tank by a bare running index emitted whatever pair fell
            # out of the ordering: 228 R4 backward moves (grow-out -> entry) and
            # 23 R3 intra-entry moves of >=1 kg fish on the operator's PR, while
            # the controller family emits ZERO. The tank SETS were legal — a
            # batch may legitimately hold a retained entry tank and a new
            # grow-out tank at once — but the SOURCE->DEST pairing between them
            # was not. Prefer a legal source for every destination, judged by the
            # SAME forecast.tiers module the controller uses so both families are
            # held to identical code.
            def _legal_src(s_entry, dest_tank):
                ok, _ = move_allowed(tank_sys.get(s_entry[0], ""),
                                     tank_sys.get(dest_tank, ""), s_entry[2])
                return ok

            for d in demand:
                dest = d[0]
                while d[1] > 1e-9:
                    avail_s = [s for s in supply if s[1] > 1e-9]
                    if not avail_s:
                        break
                    legal = [s for s in avail_s if _legal_src(s, dest)]
                    if legal:
                        s = legal[0]
                    else:
                        # No legal source for this destination. Conservation wins
                        # (fish must physically come from somewhere), so the move
                        # is still emitted — but it is a REAL topology breach and
                        # must be impossible to miss.
                        s = avail_s[0]
                        _ok, _why = move_allowed(tank_sys.get(s[0], ""),
                                                 tank_sys.get(dest, ""), s[2])
                        topology_warnings.append(
                            f"TOPOLOGY VIOLATION - {wl}: batch {batch_id} "
                            f"{tank_sys.get(s[0])}-{s[0]} -> "
                            f"{tank_sys.get(dest)}-{dest} at {s[2]:.0f} g. {_why}")
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
        # biomass balance EXACTLY. `open` is the prior TANK biomass REGARDLESS of
        # which batch held it: on an A->B swap the departing A leaves this tank via
        # h_out_kg / t_out_kg, so the basis MUST include A's open biomass. Zeroing
        # it on a batch change (the old rule) stranded A's out-debit and inflated
        # the arriving batch's net by A's WHOLE biomass — which the per-tank audit
        # then double-booked against its own prev_biomass (the swap-row BIO_DRIFT,
        # a -0.94 facility biomass ratio). prev_occ is None only for a genuinely
        # empty prior tank, where open = 0 is correct.
        for tid, occ in new_state.items():
            prev_occ = prev_state.get(tid)
            open_kg = prev_occ.biomass_kg if prev_occ is not None else 0.0
            base = (open_kg - h_out_kg.get(tid, 0.0) - t_out_kg.get(tid, 0.0)
                    + t_in_kg.get(tid, 0.0) + tn_in_kg.get(tid, 0.0))
            net = occ.biomass_kg - base
            realized_biology[(tid, wl, occ.batch_id)] = (
                net, mort_ct_by_tank.pop(tid, 0.0))
        # Tanks that EMPTIED this week were zeroed by mortality + harvest_out +
        # transfer_out. The audit still reconciles a zero-out row for the departed
        # batch, so credit the deaths applied in that tank (else they read as
        # negative drift). net biomass = 0 there (the tank closed empty). STARVE
        # (6N) tanks are off-feed: net ~0 (held biomass frozen week to week).
        for tid, mct in mort_ct_by_tank.items():
            if mct <= 1e-9:
                continue
            pocc = prev_state.get(tid)
            if pocc is None:
                continue
            prev = realized_biology.get((tid, wl, pocc.batch_id))
            realized_biology[(tid, wl, pocc.batch_id)] = (
                (prev[0] if prev else 0.0), (prev[1] if prev else 0.0) + mct)

        # Depuration clock: a 6N tank starts its hold when fish ARRIVE — i.e.
        # when it goes from empty/other-batch to holding this batch, or when its
        # count grows (a top-up restarts the hold for that tank, the
        # conservative reading). Emptying clears it.
        for tid in sixn_set:
            now = new_state.get(tid)
            before = prev_state.get(tid)
            if now is None:
                sixn_arrival.pop(tid, None)
            elif (before is None or before.batch_id != now.batch_id
                    or now.count > before.count + 1.0):
                sixn_arrival[tid] = w

        if week_oversub:
            oversub_weeks.append(wl)
        state = new_state

    if _hold_total_draws:
        _pct = 100.0 * _hold_short_fish / max(1.0, _hold_total_fish)
        print(f"  [DEPURATION AUDIT] {_hold_short_draws} of {_hold_total_draws} "
              f"6N draws harvested BEFORE the {_hold_weeks}-week purge hold "
              f"({_hold_short_fish:,.0f} of {_hold_total_fish:,.0f} fish, "
              f"{_pct:.0f}%). The 6N pool is a BATCH process — fish must sit "
              f"off-feed for the hold before harvest. L1's envelope honours it; "
              f"this tank pick does NOT stage fish into 6N ahead of the draw, so "
              f"it harvests from tanks it filled the same week. Any harvest-"
              f"smoothness advantage over the controller is inflated by that.")
    if unplaced_warnings:
        print(f"  !! UNPLACED BATCHES: {len(unplaced_warnings)} batch-week(s) "
              f"had L1 standing but no legal free tank - fish are ABSENT from "
              f"BatchLocations (see ValidationLog).")
        for _u in unplaced_warnings[:5]:
            print(f"     {_u}")
    if topology_warnings:
        print(f"  !! TOPOLOGY VIOLATIONS: {len(topology_warnings)} emitted "
              f"transfer(s) break the R1-R7 conveyor rules (see ValidationLog).")
        for _t in topology_warnings[:3]:
            print(f"     {_t}")
    if _purge_h_demand > 0:
        print(f"  [6N-RULE PROBE] purge-mode harvest: {_purge_h_demand:,.0f} fish "
              f"demanded; {_purge_h_from6n:,.0f} from 6N ({100*_purge_h_from6n/_purge_h_demand:.0f}%); "
              f"{_purge_h_shortfall:,.0f} would OVERFLOW to production tanks "
              f"({100*_purge_h_shortfall/_purge_h_demand:.0f}% from production tanks — "
              f"the 6N-only-rule violation; should be ~0).")

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
        unplaced_warnings=unplaced_warnings,
        topology_warnings=topology_warnings,
    )
