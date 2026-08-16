"""END-OF-WEEK CAP REPAIR (`placement._repair_over_cap_systems`).

WHY THE PASS EXISTS (the measurement, so a future reader can re-derive it):
every other realised repair pass — `_rebalance_systems_realized`,
`_even_out_density`, `_balance_loads`, `_variable_quantity_rebalance`,
`_consolidate_remnants` — runs BEFORE the week's day-by-day biology inside
`phase_d_emit_events`, while the BatchLocations snapshot that the
SystemLimitsAudit sums is taken AFTER it. So the whole stack aims at the
START-of-week load and every published metric measures the END-of-week one.
Traced on the 7.29 PR: of 104 non-6N over-cap (system, week) cells, 79 were
ALREADY compliant when the balancer finished (0.94-0.99 of cap) with no later
event touching the system — a week of growth alone carried them over.

WHAT THIS FILE GUARDS. The pass is the FIRST cap check that sees the measured
state, so it is also the first that could relocate a violation instead of
repairing one. Each test below pins one rule it must never break, and the
NEGATIVE CONTROL (test_negative_control_*) proves the alarm can ring: the same
synthetic state with the pass switched off (budget 0 = the parent's behaviour)
STAYS over cap, so a green suite here cannot come from a no-op.

State fixtures follow the tiny-TankState pattern of tests/test_transfer_rules.py
and tests/test_lns_placement.py — no workbook needed.
"""
from __future__ import annotations

from datetime import date

import pytest

from forecast.models import BiologyTables
from forecast.placement import _repair_over_cap_systems
from forecast.state import STAGE_STARVE, FacilityState, TankState


TODAY = date(2027, 3, 1)
WL = "2027-W09"

# Flat 1 %/day SGR and no FCR model -> realized_feed_kg_day = biomass * 0.01 *
# 1.2, i.e. feed is a fixed multiple of biomass. That keeps the fixtures
# readable: a feed cap is just a biomass cap in disguise, and a test that wants
# to bind on FEED sets a low feed cap, not a different fish size.
TABLES = BiologyTables([100.0, 10000.0], [1.0, 1.0], [1.0, 1.0],
                       [100.0, 10000.0], {}, [], [], [], [])
FEED_PER_KG = 0.01 * 1.2


def _tank(tid, system, *, batch=None, count=0.0, wt=0.0, vol=1000.0,
          dens=95.0, stage="SW"):
    t = TankState(f"{system}-{tid}", tid, system, vol, dens, 3000.0, "OG")
    if batch is not None:
        t.assign(batch, count, wt, 0.0, stage)
    return t


def _caps(**per_system):
    """cap_lookup closure: {system: (biomass_cap, feed_cap)}."""
    def lookup(_wl, s):
        return per_system.get(s, (None, None))
    return lookup


def _run(state, cap_lookup, tanks_by_system, *, budget=4, systems=None,
         reserved=frozenset(), min_transfer=0.0, min_keep=0.0):
    events, warnings = [], []
    moves = _repair_over_cap_systems(
        state, WL, TODAY, events, warnings,
        cap_lookup=cap_lookup, batch_meta={}, tables=TABLES,
        og_systems=set(systems if systems is not None else tanks_by_system),
        og_tanks_by_system=tanks_by_system, budget=budget,
        reserved=reserved, min_transfer=min_transfer, min_keep=min_keep,
    )
    return moves, events, warnings


def _load(state, tanks_by_system, system):
    return sum(state.tanks_by_id[t].biomass_kg
               for t in tanks_by_system[system])


def _fish(state):
    return round(sum(t.count for t in state.tanks_by_id.values()), 3)


# --------------------------------------------------------------------------- #
# 1. The negative control + its positive twin
# --------------------------------------------------------------------------- #
def _over_cap_with_relief():
    """OG3N holds 120 t of B1 against a 100 t cap; OG4N holds 10 t of the SAME
    batch against 100 t. One legal partial move — B1's own OG4N tank — repairs
    it. (A free tank sits in OG4N too, and must stay free: the repair is
    footprint-neutral.)"""
    tanks = [_tank(1, "OG3N", batch="B1", count=40000.0, wt=3000.0),
             _tank(2, "OG4N", batch="B1", count=3333.3333, wt=3000.0),
             _tank(3, "OG4N")]
    state = FacilityState(TODAY, tanks)
    by_sys = {"OG3N": [1], "OG4N": [2, 3]}
    caps = _caps(OG3N=(100000.0, 1e9), OG4N=(100000.0, 1e9))
    return state, caps, by_sys


def test_negative_control_off_leaves_the_breach_in_place():
    """budget 0 == the parent engine. The state MUST stay over cap — otherwise
    every assertion below could be passing on a state that was never broken."""
    state, caps, by_sys = _over_cap_with_relief()
    assert _load(state, by_sys, "OG3N") > 100000.0
    moves, events, _ = _run(state, caps, by_sys, budget=0)
    assert moves == 0 and events == []
    assert _load(state, by_sys, "OG3N") > 100000.0, (
        "the fixture is not actually over cap — this control cannot fire")


def test_repairs_an_over_cap_system_into_the_cool_one():
    state, caps, by_sys = _over_cap_with_relief()
    before = _fish(state)
    moves, events, _ = _run(state, caps, by_sys, budget=4)
    assert moves >= 1 and events
    assert _load(state, by_sys, "OG3N") <= 100000.0
    assert _load(state, by_sys, "OG4N") <= 100000.0
    assert _fish(state) == before, "the repair must conserve fish exactly"


def test_is_footprint_neutral_and_never_claims_an_empty_tank():
    """The tank SET must come out unchanged: the repair tops up tanks the batch
    already holds and leaves the free pool — which the harvest controller needs
    for whole-tank moves — exactly as it found it. Claiming empties is what
    drove a 83,869-fish dump through the relief ceiling on 7.29-nowin."""
    state, caps, by_sys = _over_cap_with_relief()
    occupied_before = {t.tank_id for t in state.tanks_by_id.values()
                       if not t.is_empty}
    _m, events, _w = _run(state, caps, by_sys, budget=4)
    assert events
    occupied_after = {t.tank_id for t in state.tanks_by_id.values()
                      if not t.is_empty}
    assert occupied_after == occupied_before
    assert state.tanks_by_id[3].is_empty


def test_declines_when_the_only_cool_tank_is_empty():
    """Same fixture minus the batch's existing OG4N tank: an empty tank alone is
    NOT a destination, so the pass stands down rather than open a new tank."""
    tanks = [_tank(1, "OG3N", batch="B1", count=40000.0, wt=3000.0),
             _tank(3, "OG4N")]
    state = FacilityState(TODAY, tanks)
    by_sys = {"OG3N": [1], "OG4N": [3]}
    caps = _caps(OG3N=(100000.0, 1e9), OG4N=(100000.0, 1e9))
    moves, events, _ = _run(state, caps, by_sys, budget=4)
    assert moves == 0 and events == []
    assert state.tanks_by_id[3].is_empty


def test_repair_is_deterministic():
    def once():
        state, caps, by_sys = _over_cap_with_relief()
        _m, events, _w = _run(state, caps, by_sys, budget=4)
        return [(e.source_tank_id, e.destinations[0].tank_id,
                 round(e.destinations[0].count, 6)) for e in events]
    assert once() == once()


def test_feed_cap_binds_on_its_own():
    """A system under its BIOMASS cap but over its FEED cap is still repaired —
    96 of the 104 measured non-6N breaches were feed-only."""
    tanks = [_tank(1, "OG3N", batch="B1", count=40000.0, wt=3000.0),
             _tank(2, "OG4N", batch="B1", count=3333.3333, wt=3000.0)]
    state = FacilityState(TODAY, tanks)
    by_sys = {"OG3N": [1], "OG4N": [2]}
    feed_cap = 100000.0 * FEED_PER_KG          # == the 100 t biomass line
    caps = _caps(OG3N=(1e9, feed_cap), OG4N=(1e9, feed_cap))
    assert _load(state, by_sys, "OG3N") * FEED_PER_KG > feed_cap
    moves, _e, _w = _run(state, caps, by_sys, budget=4)
    assert moves >= 1
    assert _load(state, by_sys, "OG3N") * FEED_PER_KG <= feed_cap


# --------------------------------------------------------------------------- #
# 2. Business rules the repair may never relax
# --------------------------------------------------------------------------- #
def test_never_moves_backward_into_the_entry_tier():
    """R4: a non-entry source may never target OG1/2, however cold OG1/2 is."""
    tanks = [_tank(1, "OG3N", batch="B1", count=40000.0, wt=3000.0),
             _tank(2, "OG1N", batch="B1", count=1000.0, wt=3000.0),
             _tank(3, "OG1S", batch="B1", count=1000.0, wt=3000.0)]
    state = FacilityState(TODAY, tanks)
    by_sys = {"OG3N": [1], "OG1N": [2], "OG1S": [3]}
    caps = _caps(OG3N=(100000.0, 1e9), OG1N=(400000.0, 1e9),
                 OG1S=(400000.0, 1e9))
    moves, events, _ = _run(state, caps, by_sys, budget=4)
    assert moves == 0 and events == []
    assert state.tanks_by_id[2].count == pytest.approx(1000.0)
    assert state.tanks_by_id[3].count == pytest.approx(1000.0)


def test_entry_tier_at_or_over_1kg_may_only_move_forward():
    """R3: an over-cap entry system whose fish are >= 1 kg must NOT relieve
    sideways into another entry system, even a totally empty one."""
    tanks = [_tank(1, "OG2N", batch="B1", count=40000.0, wt=3000.0),
             _tank(2, "OG1N", batch="B1", count=1000.0, wt=3000.0)]
    state = FacilityState(TODAY, tanks)
    by_sys = {"OG2N": [1], "OG1N": [2]}
    caps = _caps(OG2N=(100000.0, 1e9), OG1N=(400000.0, 1e9))
    moves, events, _ = _run(state, caps, by_sys, budget=4)
    assert moves == 0 and events == []
    # ... but the SAME fixture under 1 kg is a legal intra-entry split.
    tanks = [_tank(1, "OG2N", batch="B1", count=200000.0, wt=600.0),
             _tank(2, "OG1N", batch="B1", count=1000.0, wt=600.0)]
    state = FacilityState(TODAY, tanks)
    moves, _e, _w = _run(state, caps, by_sys, budget=4)
    assert moves >= 1 and _load(state, by_sys, "OG2N") <= 100000.0


def test_never_mixes_two_batches_in_one_tank():
    """One batch per tank: the only cool tank holds a DIFFERENT batch, so there
    is no legal destination and the pass must decline rather than mix."""
    tanks = [_tank(1, "OG3N", batch="B1", count=40000.0, wt=3000.0),
             _tank(2, "OG4N", batch="B2", count=1000.0, wt=3000.0)]
    state = FacilityState(TODAY, tanks)
    by_sys = {"OG3N": [1], "OG4N": [2]}
    caps = _caps(OG3N=(100000.0, 1e9), OG4N=(100000.0, 1e9))
    moves, events, _ = _run(state, caps, by_sys, budget=4)
    assert moves == 0 and events == []
    assert state.tanks_by_id[2].batch_id == "B2"
    assert state.tanks_by_id[2].count == pytest.approx(1000.0)


def test_never_exceeds_the_destination_tank_density_cap():
    tanks = [_tank(1, "OG3N", batch="B1", count=40000.0, wt=3000.0),
             # 100 m3 at 95 kg/m3 -> 9.5 t ceiling; already holds 9 t.
             _tank(2, "OG4N", batch="B1", count=3000.0, wt=3000.0, vol=100.0)]
    state = FacilityState(TODAY, tanks)
    by_sys = {"OG3N": [1], "OG4N": [2]}
    caps = _caps(OG3N=(100000.0, 1e9), OG4N=(1e9, 1e9))
    _m, _e, _w = _run(state, caps, by_sys, budget=6)
    dst = state.tanks_by_id[2]
    assert dst.density_kg_m3 <= dst.max_density_kg_m3


def test_respects_the_remnant_floor_and_the_min_transfer_floor():
    """Never strand 0 < residue < min_tank_control in the source, and never
    emit a split smaller than min_transfer_count fish."""
    tanks = [_tank(1, "OG3N", batch="B1", count=40000.0, wt=3000.0),
             _tank(2, "OG4N", batch="B1", count=100.0, wt=3000.0)]
    state = FacilityState(TODAY, tanks)
    by_sys = {"OG3N": [1], "OG4N": [2]}
    caps = _caps(OG3N=(100000.0, 1e9), OG4N=(1e9, 1e9))
    _m, events, _w = _run(state, caps, by_sys, budget=6,
                          min_transfer=7000.0, min_keep=7000.0)
    for e in events:
        assert e.destinations[0].count >= 7000.0
    src = state.tanks_by_id[1]
    assert src.is_empty or src.count >= 7000.0


def test_a_sub_min_transfer_repair_is_declined_not_shrunk():
    """Only ~1 t is over cap — far under a 7,000-fish move. The pass may take
    MORE than the strict minimum (bounded by headroom) but must never emit a
    move below the floor."""
    tanks = [_tank(1, "OG3N", batch="B1", count=33700.0, wt=3000.0),
             _tank(2, "OG4N", batch="B1", count=100.0, wt=3000.0)]
    state = FacilityState(TODAY, tanks)
    by_sys = {"OG3N": [1], "OG4N": [2]}
    caps = _caps(OG3N=(100000.0, 1e9), OG4N=(1e9, 1e9))
    _m, events, _w = _run(state, caps, by_sys, budget=6, min_transfer=7000.0)
    for e in events:
        assert e.destinations[0].count >= 7000.0


def test_never_touches_a_reserved_tank():
    """A tank HELD for an imminent TranOG arrival is skipped even when it is an
    otherwise-perfect same-batch destination (defence in depth: the
    footprint-neutral rule already keeps the repair off EMPTY held tanks)."""
    tanks = [_tank(1, "OG3N", batch="B1", count=40000.0, wt=3000.0),
             _tank(2, "OG4N", batch="B1", count=100.0, wt=3000.0)]
    state = FacilityState(TODAY, tanks)
    state.reserved_tanks = {2}
    by_sys = {"OG3N": [1], "OG4N": [2]}
    caps = _caps(OG3N=(100000.0, 1e9), OG4N=(1e9, 1e9))
    moves, events, _ = _run(state, caps, by_sys, budget=4, reserved={2})
    assert moves == 0 and events == []
    assert state.tanks_by_id[2].count == pytest.approx(100.0)


def test_never_drains_or_fills_a_depuration_tank():
    """R7 (6N one-way): a STARVE tank is neither a source nor a destination."""
    # As a SOURCE: OG6N is over cap but its fish are committed to depuration.
    tanks = [_tank(61, "OG6N", batch="B1", count=40000.0, wt=3000.0,
                   stage=STAGE_STARVE),
             _tank(2, "OG4N", batch="B1", count=100.0, wt=3000.0)]
    state = FacilityState(TODAY, tanks)
    by_sys = {"OG6N": [61], "OG4N": [2]}
    caps = _caps(OG6N=(100000.0, 1e9), OG4N=(1e9, 1e9))
    moves, _e, _w = _run(state, caps, by_sys, budget=4)
    assert moves == 0 and state.tanks_by_id[61].count == pytest.approx(40000.0)

    # As a DESTINATION: a purging tank of the same batch must not be topped up.
    tanks = [_tank(1, "OG3N", batch="B1", count=40000.0, wt=3000.0),
             _tank(63, "OG6N", batch="B1", count=1000.0, wt=3000.0,
                   stage=STAGE_STARVE)]
    state = FacilityState(TODAY, tanks)
    by_sys = {"OG3N": [1], "OG6N": [63]}
    caps = _caps(OG3N=(100000.0, 1e9), OG6N=(1e9, 1e9))
    moves, _e, _w = _run(state, caps, by_sys, budget=4)
    assert moves == 0 and state.tanks_by_id[63].count == pytest.approx(1000.0)


def test_never_exceeds_its_move_budget():
    """The handling budget is a hard ceiling on this deferrable pass. Four
    over-cap systems, one move each at most."""
    tanks = []
    by_sys = {}
    for i, s in enumerate(("OG3N", "OG3S", "OG4N", "OG4S")):
        tanks.append(_tank(10 + i, s, batch=f"B{i}", count=40000.0, wt=3000.0))
        by_sys[s] = [10 + i]
    for i in range(4):    # each batch's own tank in a cool landing system
        tanks.append(_tank(20 + i, "OG5N", batch=f"B{i}", count=100.0,
                           wt=3000.0))
    by_sys["OG5N"] = [20, 21, 22, 23]
    state = FacilityState(TODAY, tanks)
    caps = _caps(OG3N=(100000.0, 1e9), OG3S=(100000.0, 1e9),
                 OG4N=(100000.0, 1e9), OG4S=(100000.0, 1e9),
                 OG5N=(1e9, 1e9))
    moves, events, _ = _run(state, caps, by_sys, budget=2)
    assert moves <= 2 and len(events) <= 2


def test_prefers_the_coldest_legal_system():
    """Balance, don't concentrate: two legal destinations, the colder one wins
    even though both have room."""
    tanks = [_tank(1, "OG3N", batch="B1", count=40000.0, wt=3000.0),
             _tank(2, "OG4N", batch="B1", count=25000.0, wt=3000.0),   # 75 % full
             _tank(3, "OG5N", batch="B1", count=5000.0, wt=3000.0)]    # 15 % full
    state = FacilityState(TODAY, tanks)
    by_sys = {"OG3N": [1], "OG4N": [2], "OG5N": [3]}
    caps = _caps(OG3N=(100000.0, 1e9), OG4N=(100000.0, 1e9),
                 OG5N=(100000.0, 1e9))
    _m, events, _w = _run(state, caps, by_sys, budget=1)
    assert events and events[0].destinations[0].tank_id == 3


def test_declines_when_every_destination_is_already_hot():
    """No relocation of a violation: if the only legal systems are themselves
    near cap, the pass does nothing rather than spread the breach."""
    tanks = [_tank(1, "OG3N", batch="B1", count=40000.0, wt=3000.0),
             _tank(2, "OG4N", batch="B1", count=33000.0, wt=3000.0)]
    state = FacilityState(TODAY, tanks)
    by_sys = {"OG3N": [1], "OG4N": [2]}
    caps = _caps(OG3N=(100000.0, 1e9), OG4N=(100000.0, 1e9))
    moves, events, _ = _run(state, caps, by_sys, budget=4)
    assert moves == 0 and events == []
    assert state.tanks_by_id[2].count == pytest.approx(33000.0)


def test_uncapped_systems_are_never_flagged_or_filled_past_reason():
    """A system with no cap row (cap_lookup -> None) is neither a breach nor an
    unlimited dump: it can receive, but it can never be the hot system."""
    tanks = [_tank(1, "OG3N", batch="B1", count=40000.0, wt=3000.0),
             _tank(2, "OG4N", batch="B1", count=100.0, wt=3000.0)]
    state = FacilityState(TODAY, tanks)
    by_sys = {"OG3N": [1], "OG4N": [2]}
    caps = _caps(OG4N=(1e9, 1e9))            # OG3N has NO cap at all
    moves, events, _ = _run(state, caps, by_sys, budget=4)
    assert moves == 0 and events == []


def test_the_shipped_config_value_is_off():
    """WITHDRAWN 2026-08-15, one day after adoption — the pass is OFF again.

    It was adopted at 8 on an 8-state study (better system balance on 8 of 8,
    floor cost "inside the noise band") plus the operator's own 7.29 PR check.
    Then it met a NINTH starting state, their 2026-08-13 PR, and on that one it
    is catastrophic. Same code, same config, same day, only this knob differs:

        7.29 PR   cap 0 -> 8 : worst week 19,630 -> 23,235   density 116.8 -> 102.2   GOOD
        8.13 PR   cap 0 -> 8 : worst week 23,259 ->  4,578   density 103.7 -> 124.2   BAD
                               and 0 -> 1 weeks past the relief ceiling

    An 80% collapse of the leanest harvest week, plus a ceiling breach, on the
    operator's hardest business rule. The system-balance gain IS robust
    (overshoot improves on both PRs, as the study found) — it is the FLOOR
    effect that is high-variance, and "inside the noise band" turned out to
    mean inside the noise OF THE EIGHT STATES SAMPLED, not small. A wide
    distribution sampled eight times reads as noise.

    So this is not a default. If the balance gain is wanted on a given month,
    run that PR both ways and compare — it costs forty seconds.

    The pin is kept (not deleted) so a future re-adoption has to come here and
    face the counter-evidence first."""
    import os
    from forecast.config_io import load_control
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert load_control(os.path.join(root, "config")).cap_repair_budget == 0


def test_zero_still_means_off():
    """Adoption must not weld the pass in: 0 remains a true bypass, so the
    operator can A/B it or revert without a code change."""
    import dataclasses
    from forecast.models import ControlParams
    fld = next(f for f in dataclasses.fields(ControlParams)
               if f.name == "cap_repair_budget")
    assert fld.default == 0
