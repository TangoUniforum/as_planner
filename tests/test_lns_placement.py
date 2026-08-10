"""LNS realized-placement engine — relocation correctness + the conservation gate.

The synthetic case proves the machinery works WHEN there is room: a hot system with
a free tank in a cooler system. The engine must relocate the offending batch, lower
the hot-spot peak, and keep the continuity audit at 0 drift. A separate real-config
gate (test_lns_real_config_safe) proves that on the live workbook LNS never breaks
conservation and never raises the peak — even when it (correctly) finds no move.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from forecast import lns_placement
from forecast.caps import SystemLimits
from forecast.events import Harvest, TankAllocation, TranOGEntry
from forecast.models import BatchWeekState, BiologyTables, TankConfig
from forecast.placement import BatchLocationRow, PlacementResult
from forecast.state import FacilityState, TankState
from forecast.time_grid import iso_week_label


class _Ctl:
    lns_max_moves = 30


class _Fac:
    def __init__(self, tanks):
        self.tanks = tanks


def _synthetic():
    """OG3N (1 tank) holds a 15 t batch -> 1.5x over its 10 t cap; OG4N has a free
    tank and a 40 t cap. Relocating the batch to OG4N drops the peak to ~0.4x."""
    start = date(2027, 1, 4)
    wd = [start + timedelta(weeks=i) for i in range(6)]
    wl = [iso_week_label(d) for d in wd]

    tanks = [
        TankConfig("T1", "OG3N", 1, 200.0, 95.0, 1000.0, "OG"),   # HOT
        TankConfig("T2", "OG4N", 2, 200.0, 95.0, 1000.0, "OG"),   # COOL, free
        TankConfig("T3", "OG4N", 3, 200.0, 95.0, 1000.0, "OG"),   # COOL, anchor
    ]
    facility = _Fac(tanks)

    bl = []
    for i in (0, 1, 2):   # B1 in T1/OG3N, 10000 @ 1500 g = 15 t
        bl.append(BatchLocationRow(wl[i], wd[i], "B1", 1, "T1", "OG3N",
                                   10000.0, 1500.0, 15000.0, 75.0, "SW"))
    for i in (0, 1, 2, 3, 4):   # B2 anchor in T3/OG4N (extends the horizon past B1)
        bl.append(BatchLocationRow(wl[i], wd[i], "B2", 3, "T3", "OG4N",
                                   1000.0, 1000.0, 1000.0, 5.0, "SW"))

    tranog = [TranOGEntry("B1", wd[0], [TankAllocation(1, 10000.0, 1500.0, 0.0)]),
              TranOGEntry("B2", wd[0], [TankAllocation(3, 1000.0, 1000.0, 0.0)])]
    harv = [Harvest("B1", wd[3], 1, 10000.0, 1500.0)]   # full harvest empties T1 at W4
    placement = PlacementResult(batch_locations=bl, tranog_events=tranog,
                                harvest_events=harv)
    # Realized biology keyed by (tank_id, week_label, batch_id) — static (sgr=0,
    # mortality=0) so it reconciles, but PRESENT so refine exercises the re-key +
    # the realized-keyed final gate (guards finding #1's shipped-audit path).
    placement.realized_biology = {}
    for bid, idxs, tid in [("B1", (0, 1, 2), 1), ("B2", (0, 1, 2, 3, 4), 3)]:
        for i in idxs:
            placement.realized_biology[(tid, wl[i], bid)] = [0.0, 0.0]

    states = []
    for bid, idxs, cnt, wt, bio in [("B1", (0, 1, 2, 3), 10000.0, 1500.0, 15000.0),
                                    ("B2", (0, 1, 2, 3, 4), 1000.0, 1000.0, 1000.0)]:
        for i in idxs:   # sgr=0, mortality=0 -> static, so the baseline reconciles
            states.append(BatchWeekState(bid, wl[i], wd[i], i * 7, i, cnt, wt, bio,
                                         0.0, 0.0, 0.0, 1.2, "SW", "", 0.0))

    initial = FacilityState(start, [
        TankState("T1", 1, "OG3N", 200.0, 95.0, 1000.0, "OG"),
        TankState("T2", 2, "OG4N", 200.0, 95.0, 1000.0, "OG"),
        TankState("T3", 3, "OG4N", 200.0, 95.0, 1000.0, "OG")])

    caps = {}
    for L in wl:
        caps[(L, "OG3N", "biomass")] = 10000.0
        caps[(L, "OG4N", "biomass")] = 40000.0
    sl = SystemLimits(caps=caps)
    tables = BiologyTables([100.0, 5000.0], [1.0, 1.0], [1.0, 1.0],
                           [100.0, 5000.0], {}, [], [], [], [])
    return placement, initial, states, facility, sl, tables


def test_relocates_to_free_tank_zero_drift():
    placement, initial, states, facility, sl, tables = _synthetic()
    base = lns_placement.system_peak(placement.batch_locations, {}, tables, sl)
    assert base > 1.4 and lns_placement.drift_count(placement, states, initial) == 0

    edited = lns_placement.refine_realized(
        placement, initial_state=initial, batch_week_states=states, control=_Ctl(),
        facility=facility, system_limits=sl, facility_limits=None,
        batch_meta={}, tables=tables)

    assert edited is not None, "engine should relocate B1 into the free OG4N tank"
    b1_sys = {r.system_id for r in edited.batch_locations if r.batch_id == "B1"}
    assert b1_sys == {"OG4N"}, f"B1 should have moved to OG4N, got {b1_sys}"
    new = lns_placement.system_peak(edited.batch_locations, {}, tables, sl)
    assert new < base - 1e-6, f"peak should drop ({base:.3f} -> {new:.3f})"
    assert lns_placement.drift_count(edited, states, initial) == 0, "no drift after move"
    # realized_biology re-keyed WITH the move (finding #1): B1's entries now live
    # under the destination tank (2), and the realized-keyed audit that actually
    # SHIPS (run.py) is clean — not just the modelled one.
    assert all(k[0] == 2 for k in edited.realized_biology if k[2] == "B1"), \
        "B1 realized biology must follow it to tank 2"
    assert lns_placement.drift_count(
        edited, states, initial, realized_biology=edited.realized_biology) == 0, \
        "shipped (realized-keyed) audit must be clean after the move"
    # greedy input is untouched (still 1.5x) — the engine works on a copy
    assert lns_placement.system_peak(placement.batch_locations, {}, tables, sl) == base
    assert placement.realized_biology[(1, iso_week_label(date(2027, 1, 4)), "B1")] \
        == [0.0, 0.0], "greedy's realized_biology must be untouched (kept at tank 1)"


def test_no_move_when_no_target():
    """With OG4N's cap as tight as OG3N's, relocating can't beat the hot spot, so the
    engine returns None (greedy stands) rather than churn for no gain."""
    placement, initial, states, facility, sl, tables = _synthetic()
    sl.caps.update({(L, "OG4N", "biomass"): 10000.0 for L in
                    {k[0] for k in sl.caps}})   # OG4N now as tight as OG3N
    edited = lns_placement.refine_realized(
        placement, initial_state=initial, batch_week_states=states, control=_Ctl(),
        facility=facility, system_limits=sl, facility_limits=None,
        batch_meta={}, tables=tables)
    assert edited is None


class _Tk:
    def __init__(self, tid, sysid):
        self.tank_id = tid
        self.location_id = f"L{tid}"
        self.system_id = sysid
        self.volume_m3 = 200.0
        self.max_density_kg_m3 = 95.0


def test_relabel_rekeys_realized_biology():
    """_relabel must carry realized_biology (keyed by tank_id) with the occupancy,
    or the SHIPPED TankContinuityAudit (run.py feeds realized_biology) reconciles a
    moved tank-week against a MISSING key -> mort defaults to 0 -> phantom drift the
    modelled accept gate never sees. Covers relocate, revert (audit-reject path), and
    a two-way swap. Guards audit finding #1 (2026-07-13)."""
    wl0, wl1 = "2027-W01", "2027-W02"
    tank_by_id = {1: _Tk(1, "OG3N"), 2: _Tk(2, "OG4N")}

    # --- RELOCATE B1: tank 1 -> tank 2 for both weeks ---
    bl = [BatchLocationRow(wl0, date(2027, 1, 4), "B1", 1, "L1", "OG3N",
                           10000.0, 1500.0, 15000.0, 75.0, "SW"),
          BatchLocationRow(wl1, date(2027, 1, 11), "B1", 1, "L1", "OG3N",
                           10000.0, 1500.0, 15000.0, 75.0, "SW")]
    p = PlacementResult(batch_locations=bl)
    p.realized_biology = {(1, wl0, "B1"): [12.0, 34.0], (1, wl1, "B1"): [56.0, 78.0]}
    relmap = {("B1", 1, wl0): 2, ("B1", 1, wl1): 2}
    lns_placement._relabel(p, relmap, tank_by_id)
    assert (1, wl0, "B1") not in p.realized_biology, "old tank key must be gone"
    assert p.realized_biology[(2, wl0, "B1")] == [12.0, 34.0]
    assert p.realized_biology[(2, wl1, "B1")] == [56.0, 78.0]
    assert all(r.tank_id == 2 and r.system_id == "OG4N" for r in p.batch_locations)

    # --- REVERT via _invert restores the original keys + values exactly ---
    lns_placement._relabel(p, lns_placement._invert(relmap), tank_by_id)
    assert p.realized_biology == {(1, wl0, "B1"): [12.0, 34.0],
                                  (1, wl1, "B1"): [56.0, 78.0]}
    assert all(r.tank_id == 1 for r in p.batch_locations)

    # --- SWAP B1@tank1 <-> B2@tank2 (same week): each batch's biology follows it ---
    q = PlacementResult(batch_locations=[
        BatchLocationRow(wl0, date(2027, 1, 4), "B1", 1, "L1", "OG3N",
                         10000.0, 1500.0, 15000.0, 75.0, "SW"),
        BatchLocationRow(wl0, date(2027, 1, 4), "B2", 2, "L2", "OG4N",
                         5000.0, 1000.0, 5000.0, 25.0, "SW")])
    q.realized_biology = {(1, wl0, "B1"): [1.0, 2.0], (2, wl0, "B2"): [3.0, 4.0]}
    swap = {("B1", 1, wl0): 2, ("B2", 2, wl0): 1}
    lns_placement._relabel(q, swap, tank_by_id)
    assert q.realized_biology == {(2, wl0, "B1"): [1.0, 2.0], (1, wl0, "B2"): [3.0, 4.0]}


# --------------------------------------------------------------------------- #
# 2026-07-13 audit findings: _better / _best_target over-cap discipline, swap
# density legality, drift_count fail-closed column location.
# --------------------------------------------------------------------------- #

def test_better_never_buys_a_peak_drop_with_more_overcap_area():
    """DEFECT (2026-07 audit): a strictly-lower peak was accepted UNCONDITIONALLY,
    so shaving the hottest cell while pushing other systems over cap (more total
    over-cap area) passed the gate. Lower peak may not be bought with more area."""
    base = (1.5, 0.5, 0.0)
    assert lns_placement._better((1.4, 0.5, 0.0), base) is True     # clean peak drop
    assert lns_placement._better((1.4, 0.9, 0.0), base) is False    # bought with area
    assert not lns_placement._better((1.6, 0.0, 0.0), base)         # never raise peak


def test_relocation_never_pushes_a_legal_system_over_cap():
    """DEFECT (2026-07 audit): _best_target only required the destination to end
    BELOW the hot ratio — with a tight destination cap a relocation could push a
    LEGAL system over its cap (1.33x here, 'better' than 1.5x). A system over cap
    while another has room is a placement failure, so the engine must refuse."""
    placement, initial, states, facility, sl, tables = _synthetic()
    # OG4N cap 40000 -> 12000: absorbing B1's 15 t would land OG4N at ~1.33x
    # (over cap, but still under OG3N's 1.5x hot ratio — the old acceptance).
    sl.caps.update({(L, "OG4N", "biomass"): 12000.0
                    for L in {k[0] for k in sl.caps}})
    edited = lns_placement.refine_realized(
        placement, initial_state=initial, batch_week_states=states, control=_Ctl(),
        facility=facility, system_limits=sl, facility_limits=None,
        batch_meta={}, tables=tables)
    assert edited is None, "must not push the legal OG4N over its cap"


def test_swap_respects_per_tank_density():
    """DEFECT (2026-07 audit): the swap path omitted the per-tank density check the
    relocate path does in _best_target — a heavy segment could be swapped into a
    small tank at an illegal density (150 kg/m3 here). The swap must be refused."""
    start = date(2027, 1, 4)
    wd = [start + timedelta(weeks=i) for i in range(6)]
    wl = [iso_week_label(d) for d in wd]

    tanks = [
        TankConfig("T1", "OG3N", 1, 200.0, 95.0, 1000.0, "OG"),   # HOT (B1, 15 t)
        TankConfig("T4", "OG3N", 4, 200.0, 95.0, 1000.0, "OG"),   # anchor (B3)
        TankConfig("T3", "OG4N", 3, 100.0, 95.0, 1000.0, "OG"),   # small COOL tank
    ]
    facility = _Fac(tanks)

    bl = []
    for i in (0, 1, 2):    # B1: 15 t in T1 -> OG3N ~1.5x over its 10 t cap
        bl.append(BatchLocationRow(wl[i], wd[i], "B1", 1, "T1", "OG3N",
                                   10000.0, 1500.0, 15000.0, 75.0, "SW"))
    for i in (0, 1, 2):    # B2: 1 t in the small T3 (same span as B1 -> swappable)
        bl.append(BatchLocationRow(wl[i], wd[i], "B2", 3, "T3", "OG4N",
                                   1000.0, 1000.0, 1000.0, 10.0, "SW"))
    for i in (0, 1, 2, 3, 4):   # B3 anchors the horizon (its segment ends last)
        bl.append(BatchLocationRow(wl[i], wd[i], "B3", 4, "T4", "OG3N",
                                   100.0, 1000.0, 100.0, 0.5, "SW"))

    tranog = [TranOGEntry("B1", wd[0], [TankAllocation(1, 10000.0, 1500.0, 0.0)]),
              TranOGEntry("B2", wd[0], [TankAllocation(3, 1000.0, 1000.0, 0.0)]),
              TranOGEntry("B3", wd[0], [TankAllocation(4, 100.0, 1000.0, 0.0)])]
    harv = [Harvest("B1", wd[3], 1, 10000.0, 1500.0),
            Harvest("B2", wd[3], 3, 1000.0, 1000.0)]
    placement = PlacementResult(batch_locations=bl, tranog_events=tranog,
                                harvest_events=harv)

    states = []
    for bid, idxs, cnt, wt, bio in [("B1", (0, 1, 2, 3), 10000.0, 1500.0, 15000.0),
                                    ("B2", (0, 1, 2, 3), 1000.0, 1000.0, 1000.0),
                                    ("B3", (0, 1, 2, 3, 4), 100.0, 1000.0, 100.0)]:
        for i in idxs:
            states.append(BatchWeekState(bid, wl[i], wd[i], i * 7, i, cnt, wt, bio,
                                         0.0, 0.0, 0.0, 1.2, "SW", "", 0.0))

    initial = FacilityState(start, [
        TankState("T1", 1, "OG3N", 200.0, 95.0, 1000.0, "OG"),
        TankState("T4", 4, "OG3N", 200.0, 95.0, 1000.0, "OG"),
        TankState("T3", 3, "OG4N", 100.0, 95.0, 1000.0, "OG")])

    caps = {}
    for L in wl:
        caps[(L, "OG3N", "biomass")] = 10000.0
        caps[(L, "OG4N", "biomass")] = 40000.0
    sl = SystemLimits(caps=caps)
    tables = BiologyTables([100.0, 5000.0], [1.0, 1.0], [1.0, 1.0],
                           [100.0, 5000.0], {}, [], [], [], [])

    # Sanity: baseline reconciles, so ONLY the density check can refuse the swap.
    assert lns_placement.drift_count(placement, states, initial) == 0
    edited = lns_placement.refine_realized(
        placement, initial_state=initial, batch_week_states=states, control=_Ctl(),
        facility=facility, system_limits=sl, facility_limits=None,
        batch_meta={}, tables=tables)
    assert edited is None, \
        "swap into the 100 m3 tank would be 150 kg/m3 — must be refused"


def test_drift_count_locates_flag_columns_by_header_and_fails_closed():
    """DEFECT (2026-07 audit): the flag columns were read at hardcoded indices
    14/27 — an inserted audit column silently shifts them and the gate fails OPEN
    (0 drift for a drifting plan). Columns are now located by header name, and a
    sheet with no recognisable header fails CLOSED."""
    import openpyxl

    def _sheet(header, rows):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["TANK CONTINUITY AUDIT"])
        ws.append(["blurb"])
        ws.append([])
        ws.append(header)
        for r in rows:
            ws.append(r)
        return ws

    # Flags in the CURRENT layout positions.
    hdr = ["Week"] + ["x"] * 13 + ["Flag"] + ["y"] * 12 + ["Bio_Flag"]
    row = ["2027-W01"] + [None] * 13 + ["TANK_DRIFT"] + [None] * 12 + ["BIO_DRIFT"]
    assert lns_placement._count_drift_rows(_sheet(hdr, [row])) == 2

    # A column INSERTED before the flags: same data, shifted one right — the old
    # index-14/27 read returned 0 here (fail-open); header location still sees 2.
    hdr2 = ["Week", "Inserted"] + ["x"] * 13 + ["Flag"] + ["y"] * 12 + ["Bio_Flag"]
    row2 = ["2027-W01", None] + [None] * 13 + ["TANK_DRIFT"] + [None] * 12 + ["BIO_DRIFT"]
    assert lns_placement._count_drift_rows(_sheet(hdr2, [row2])) == 2

    # No recognisable header at all -> fail CLOSED (gate rejects, greedy stands).
    ws = _sheet(["Week", "Whatever"], [["2027-W01", "TANK_DRIFT"]])
    assert lns_placement._count_drift_rows(ws) >= 10 ** 9
