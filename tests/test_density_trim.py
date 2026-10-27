"""END-OF-WEEK TANK DENSITY TRIM (`placement._trim_over_density_tanks`).

WHY THE PASS EXISTS: every density pass runs BEFORE the week's growth while the
audit measures the tank AFTER it, and the system repair (`cap_repair_budget`)
only sees system totals. Measured 2026-10-26 on the 10.18 PR live 4,200 t set
with no move limit: 189 tank-weeks over cap, 171 within 5 kg/m3, and 148 had a
legal same-batch tank with room after growth that same week.

WHAT THIS FILE GUARDS: the trim is a second pass on the measured state, so it
could relocate a breach instead of fixing one. Each test pins a rule it must
never break. The NEGATIVE CONTROL shows the fixture really is over cap with the
pass off (budget 0 = the parent engine), so a green file cannot come from a
no-op. Fixtures follow tests/test_cap_repair.py (tiny TankStates, no workbook).
"""
from __future__ import annotations

from datetime import date

import pytest

from forecast.models import BatchInput, BiologyTables
from forecast.placement import _trim_over_density_tanks
from forecast.state import STAGE_STARVE, FacilityState, TankState

TODAY = date(2027, 3, 1)
WL = "2027-W09"
TABLES = BiologyTables([100.0, 10000.0], [1.0, 1.0], [1.0, 1.0],
                       [100.0, 10000.0], {}, [], [], [], [])
NO_CAP = lambda _wl, _s: (None, None)                      # noqa: E731


def _tank(tid, system, *, batch=None, count=0.0, wt=0.0, vol=1000.0,
          dens=95.0, stage="SW"):
    t = TankState(f"{system}-{tid}", tid, system, vol, dens, 3000.0, "OG")
    if batch is not None:
        t.assign(batch, count, wt, 0.0, stage)
    return t


def _meta(state):
    ids = sorted({t.batch_id for t in state.tanks_by_id.values()
                  if not t.is_empty and t.batch_id})
    return {b: BatchInput(batch_id=b, input_date=date(2025, 1, 6),
                          input_count=500000, tran_sf_date=None,
                          tran_og_date=None, tran_og_count=None,
                          tran_og_avg_wt_g=None, tran_og_cv=10.0,
                          fcr_model="none", fw_correction=1.0,
                          sgr_correction=1.0) for b in ids}


def _run(state, by_sys, *, budget=4, cap_lookup=NO_CAP, purge=True,
         reserved=frozenset(), min_move=0.0, min_keep=0.0):
    events, warnings = [], []
    moves = _trim_over_density_tanks(
        state, WL, TODAY, events, warnings,
        cap_lookup=cap_lookup, batch_meta=_meta(state), tables=TABLES,
        og_systems=set(by_sys), og_tanks_by_system=by_sys, budget=budget,
        purge=purge, reserved=reserved, min_move=min_move, min_keep=min_keep)
    return moves, events


def _fish(state):
    return round(sum(t.count for t in state.tanks_by_id.values()), 3)


def _over_with_room():
    """Tank 1 (OG3N): 40,000 x 3 kg = 120 kg/m3 against 95. Tank 2 (OG4N) holds
    the SAME batch at 10 kg/m3. Tank 3 (OG4N) is empty and must stay empty."""
    tanks = [_tank(1, "OG3N", batch="B1", count=40000.0, wt=3000.0),
             _tank(2, "OG4N", batch="B1", count=3333.3333, wt=3000.0),
             _tank(3, "OG4N")]
    return FacilityState(TODAY, tanks), {"OG3N": [1], "OG4N": [2, 3]}


def test_negative_control_off_leaves_the_tank_over_cap():
    state, by_sys = _over_with_room()
    assert state.tanks_by_id[1].density_kg_m3 > 95.0
    moves, events = _run(state, by_sys, budget=0)
    assert moves == 0 and events == []
    assert state.tanks_by_id[1].density_kg_m3 > 95.0, "fixture not over cap"


def test_trims_the_tank_into_its_own_batch_and_conserves_fish():
    state, by_sys = _over_with_room()
    before = _fish(state)
    moves, events = _run(state, by_sys)
    assert moves >= 1 and events
    assert state.tanks_by_id[1].density_kg_m3 <= 95.0 + 1e-6
    assert state.tanks_by_id[2].density_kg_m3 <= 0.95 * 95.0 + 1e-6
    assert _fish(state) == pytest.approx(before)
    assert all(e.channel == "_trim_over_density_tanks" for e in events)


def test_moves_only_the_minimum():
    """It brings the source to 0.98 of its cap, not lower: the trim is a
    repair, not a leveller."""
    state, by_sys = _over_with_room()
    _run(state, by_sys)
    assert state.tanks_by_id[1].density_kg_m3 == pytest.approx(0.98 * 95.0, rel=1e-3)


def test_never_claims_an_empty_tank():
    tanks = [_tank(1, "OG3N", batch="B1", count=40000.0, wt=3000.0), _tank(3, "OG4N")]
    state = FacilityState(TODAY, tanks)
    moves, events = _run(state, {"OG3N": [1], "OG4N": [3]})
    assert moves == 0 and events == [] and state.tanks_by_id[3].is_empty


def test_never_mixes_two_batches():
    tanks = [_tank(1, "OG3N", batch="B1", count=40000.0, wt=3000.0),
             _tank(2, "OG4N", batch="B2", count=1000.0, wt=3000.0)]
    state = FacilityState(TODAY, tanks)
    moves, _e = _run(state, {"OG3N": [1], "OG4N": [2]})
    assert moves == 0 and state.tanks_by_id[2].count == pytest.approx(1000.0)


def test_never_fills_a_destination_past_095_of_its_cap():
    """Destination already at 90 of 95: only ~0.25 kg/m3 of room at 0.95 x cap,
    so the source stays over rather than move the breach."""
    tanks = [_tank(1, "OG3N", batch="B1", count=40000.0, wt=3000.0),
             _tank(2, "OG4N", batch="B1", count=30000.0, wt=3000.0)]
    state = FacilityState(TODAY, tanks)
    _run(state, {"OG3N": [1], "OG4N": [2]})
    assert state.tanks_by_id[2].density_kg_m3 <= 0.95 * 95.0 + 1e-6


def test_respects_the_destination_system_cap():
    tanks = [_tank(1, "OG3N", batch="B1", count=40000.0, wt=3000.0),
             _tank(2, "OG4N", batch="B1", count=3333.3333, wt=3000.0)]
    state = FacilityState(TODAY, tanks)
    caps = lambda _wl, s: (10500.0, None) if s == "OG4N" else (None, None)   # noqa: E731
    moves, _e = _run(state, {"OG3N": [1], "OG4N": [2]}, cap_lookup=caps)
    assert moves == 0, "OG4N is already over 0.95 of its 10.5 t system cap"


def test_never_moves_backward_into_the_entry_tier():
    tanks = [_tank(1, "OG3N", batch="B1", count=40000.0, wt=3000.0),
             _tank(2, "OG1N", batch="B1", count=1000.0, wt=3000.0)]
    state = FacilityState(TODAY, tanks)
    moves, _e = _run(state, {"OG3N": [1], "OG1N": [2]})
    assert moves == 0


def test_entry_tier_over_1kg_moves_forward_only():
    """R3: at >= 1 kg no split inside OG1/2; forward to OG3+ is fine."""
    tanks = [_tank(1, "OG1N", batch="B1", count=80000.0, wt=1500.0),
             _tank(2, "OG2N", batch="B1", count=1000.0, wt=1500.0)]
    state = FacilityState(TODAY, tanks)
    moves, _e = _run(state, {"OG1N": [1], "OG2N": [2]})
    assert moves == 0
    tanks = [_tank(1, "OG1N", batch="B1", count=80000.0, wt=1500.0),
             _tank(2, "OG3N", batch="B1", count=1000.0, wt=1500.0)]
    state = FacilityState(TODAY, tanks)
    moves, _e = _run(state, {"OG1N": [1], "OG3N": [2]})
    assert moves >= 1 and state.tanks_by_id[1].density_kg_m3 <= 95.0 + 1e-6


def test_harvest_prep_and_6n_are_left_alone():
    # A STARVE (harvest-prep) source is not trimmed (judged at 150 elsewhere).
    tanks = [_tank(1, "OG3N", batch="B1", count=40000.0, wt=3000.0, stage=STAGE_STARVE),
             _tank(2, "OG4N", batch="B1", count=1000.0, wt=3000.0)]
    state = FacilityState(TODAY, tanks)
    assert _run(state, {"OG3N": [1], "OG4N": [2]})[0] == 0
    # A 6N main in purge is never a destination, even for its own batch.
    tanks = [_tank(1, "OG3N", batch="B1", count=40000.0, wt=3000.0),
             _tank(61, "OG6N", batch="B1", count=1000.0, wt=3000.0)]
    state = FacilityState(TODAY, tanks)
    assert _run(state, {"OG3N": [1], "OG6N": [61]}, purge=True)[0] == 0


def test_never_touches_a_reserved_tank():
    state, by_sys = _over_with_room()
    moves, _e = _run(state, by_sys, reserved={2})
    assert moves == 0 and state.tanks_by_id[2].count == pytest.approx(3333.3333)


def test_uses_the_tanks_own_cap_including_a_per_week_row():
    """The planner writes a week's og_density_kg_m3 row onto the tank, so a
    tank at 90 is a breach under 85 and fine under 95."""
    tanks = [_tank(1, "OG3N", batch="B1", count=30000.0, wt=3000.0, dens=85.0),
             _tank(2, "OG4N", batch="B1", count=1000.0, wt=3000.0, dens=85.0)]
    state = FacilityState(TODAY, tanks)
    assert _run(state, {"OG3N": [1], "OG4N": [2]})[0] >= 1
    tanks = [_tank(1, "OG3N", batch="B1", count=30000.0, wt=3000.0, dens=95.0),
             _tank(2, "OG4N", batch="B1", count=1000.0, wt=3000.0, dens=95.0)]
    state = FacilityState(TODAY, tanks)
    assert _run(state, {"OG3N": [1], "OG4N": [2]})[0] == 0


def test_spends_no_more_than_its_budget():
    tanks = [_tank(1, "OG3N", batch="B1", count=40000.0, wt=3000.0),
             _tank(4, "OG3S", batch="B1", count=40000.0, wt=3000.0),
             _tank(2, "OG4N", batch="B1", count=1000.0, wt=3000.0),
             _tank(5, "OG4S", batch="B1", count=1000.0, wt=3000.0)]
    state = FacilityState(TODAY, tanks)
    moves, events = _run(state, {"OG3N": [1], "OG3S": [4], "OG4N": [2], "OG4S": [5]}, budget=1)
    assert moves == 1 and len(events) == 1


def test_respects_the_remnant_floor():
    """A trim never leaves a source under min_tank_control (it only moves part
    of an over-cap tank, so this binds on a small, very dense tank)."""
    tanks = [_tank(1, "OG3N", batch="B1", count=4000.0, wt=3000.0, vol=100.0),
             _tank(2, "OG4N", batch="B1", count=100.0, wt=3000.0)]
    state = FacilityState(TODAY, tanks)
    _run(state, {"OG3N": [1], "OG4N": [2]}, min_keep=3900.0)
    assert state.tanks_by_id[1].count >= 3900.0 - 1e-6 or state.tanks_by_id[1].is_empty


def test_is_deterministic():
    a, by = _over_with_room(); b, _ = _over_with_room()
    _, ea = _run(a, by); _, eb = _run(b, by)
    assert [(e.source_tank_id, [(d.tank_id, round(d.count, 3)) for d in e.destinations]) for e in ea] == \
           [(e.source_tank_id, [(d.tank_id, round(d.count, 3)) for d in e.destinations]) for e in eb]
