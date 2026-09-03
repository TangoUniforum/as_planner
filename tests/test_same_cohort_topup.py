"""A FW→OG intake may land in a tank that already holds ITS OWN cohort.

Operator rule, 2026-09-02: "we should be able to put the fish into the other
tanks that have the same cohort too… only the tanks with the same batch should
be available." Topping a tank up with its own batch is not mixing.

WHY IT MATTERED. On the 2026-08-31 ProductionReport, B49 is split — 47,743 fish
already in seawater (OG1S-14 and OG2S-24) and 250,225 still in freshwater — and
all twelve entry-tier tanks were occupied, two of them by B49 itself. TranOG
refused every occupied destination, so the cohort had nowhere legal to land and
the app's Add button could never enable.

THIS IS NOT A NEW RULE. `Transfer.apply` has always merged same-batch
destinations, with the same INV-1 refusal for a different batch and the same
count-weighted blend. TranOG was the one path that refused them all; these tests
pin the two paths to the same behaviour.

WHAT MUST NOT MOVE. 6N depuration tanks are type "OG", so the pre-existing
`tank.type != "OG"` check never excluded them — `not tank.is_empty` was the only
thing keeping a hand-written event out of an occupied 6N tank. The entry-tier
guard added alongside this change is what replaces it, and the last two tests
exist to keep it there.
"""
import datetime

import pytest

from forecast.events import TankAllocation, TranOGEntry
from forecast.state import FacilityState, TankState

STAGE_SW = "SW"
DAY = datetime.date(2026, 9, 1)


def _tank(tank_id, system_id, ttype="OG", batch=None, count=0.0, wt=0.0,
          stage="EMPTY", vol=1720.0):
    t = TankState(
        location_id=f"{system_id}-{tank_id}", tank_id=tank_id,
        system_id=system_id, volume_m3=vol, max_density_kg_m3=85.0,
        max_feed_kg_day_cap=3000.0, type=ttype)
    if batch:
        t.assign(batch_id=batch, count=count, avg_wt_g=wt, cv_pct=16.0,
                 stage=stage)
    return t


def _state(*tanks):
    return FacilityState(today=DAY, tanks=list(tanks))


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #
def test_lands_in_a_tank_holding_its_own_cohort():
    st = _state(_tank(14, "OG1S", batch="B49", count=25_173, wt=397.0,
                      stage=STAGE_SW))
    ev = TranOGEntry(batch_id="B49", event_date=DAY,
                     destinations=[TankAllocation(
                         tank_id=14, count=125_099, avg_wt_g=416.0,
                         cv_pct=16.0)])
    warns = ev.apply(st)
    assert warns == []
    assert ev.count_refused == 0.0
    assert ev.count_placed == pytest.approx(125_099)
    assert st.tanks_by_id[14].count == pytest.approx(25_173 + 125_099)


def test_the_blend_conserves_biomass():
    """The new mean times the new count must equal the two biomasses summed —
    a mean that drifts here would quietly create or destroy fish mass."""
    st = _state(_tank(14, "OG1S", batch="B49", count=25_173, wt=397.0,
                      stage=STAGE_SW))
    before_kg = st.tanks_by_id[14].biomass_kg
    arriving_kg = 125_099 * 416.0 / 1000.0
    TranOGEntry(batch_id="B49", event_date=DAY,
                destinations=[TankAllocation(tank_id=14, count=125_099,
                                             avg_wt_g=416.0, cv_pct=16.0)]
                ).apply(st)
    assert st.tanks_by_id[14].biomass_kg == pytest.approx(
        before_kg + arriving_kg, rel=1e-9)
    # and the mean sits between the two inputs, nearer the larger group
    assert 397.0 < st.tanks_by_id[14].avg_wt_g < 416.0


def test_an_empty_tank_still_behaves_exactly_as_before():
    """The empty branch is untouched, so no existing run may change."""
    st = _state(_tank(24, "OG2S"))
    TranOGEntry(batch_id="B49", event_date=DAY,
                destinations=[TankAllocation(tank_id=24, count=125_099,
                                             avg_wt_g=322.0, cv_pct=16.0)]
                ).apply(st)
    t = st.tanks_by_id[24]
    assert (t.batch_id, t.count, t.avg_wt_g, t.stage) == (
        "B49", 125_099, 322.0, STAGE_SW)


def test_the_split_cohort_can_use_both_of_its_own_tanks():
    """The case that motivated the change: two grades, two tanks, both already
    holding B49."""
    st = _state(_tank(14, "OG1S", batch="B49", count=25_173, wt=397.0,
                      stage=STAGE_SW),
                _tank(24, "OG2S", batch="B49", count=22_570, wt=491.0,
                      stage=STAGE_SW))
    ev = TranOGEntry(batch_id="B49", event_date=DAY, destinations=[
        TankAllocation(tank_id=14, count=125_099, avg_wt_g=416.0, cv_pct=16.0),
        TankAllocation(tank_id=24, count=125_099, avg_wt_g=322.0, cv_pct=16.0)])
    assert ev.apply(st) == []
    assert ev.count_refused == 0.0
    assert st.tanks_by_id[14].count == pytest.approx(150_272)
    assert st.tanks_by_id[24].count == pytest.approx(147_669)


# --------------------------------------------------------------------------- #
# What is still refused
# --------------------------------------------------------------------------- #
def test_a_different_batch_is_still_mixing_and_still_refused():
    st = _state(_tank(14, "OG1S", batch="B48", count=100_308, wt=842.0,
                      stage=STAGE_SW))
    ev = TranOGEntry(batch_id="B49", event_date=DAY,
                     destinations=[TankAllocation(tank_id=14, count=125_099,
                                                  avg_wt_g=416.0, cv_pct=16.0)])
    warns = ev.apply(st)
    assert any("DIFFERENT batch" in w and "INV-1" in w for w in warns)
    assert ev.count_placed == 0.0
    assert ev.count_refused == pytest.approx(125_099)
    assert st.tanks_by_id[14].batch_id == "B48"       # untouched
    assert st.tanks_by_id[14].count == pytest.approx(100_308)


def test_a_refused_destination_is_still_reported_as_short_stocked():
    """The accounting that exists because 1,200,000 fish were once planned,
    600,000 stocked, and 1,200,000 reported."""
    st = _state(_tank(14, "OG1S", batch="B48", count=1.0, wt=842.0,
                      stage=STAGE_SW),
                _tank(24, "OG2S"))
    ev = TranOGEntry(batch_id="B49", event_date=DAY, destinations=[
        TankAllocation(tank_id=14, count=600_000, avg_wt_g=416.0, cv_pct=16.0),
        TankAllocation(tank_id=24, count=600_000, avg_wt_g=322.0, cv_pct=16.0)])
    warns = ev.apply(st)
    assert ev.count_placed == pytest.approx(600_000)
    assert ev.count_refused == pytest.approx(600_000)
    assert any("SHORT-STOCKED" in w for w in warns)


# --------------------------------------------------------------------------- #
# 6N must be untouched — these are the guard, not decoration
# --------------------------------------------------------------------------- #
def test_an_occupied_6N_tank_is_refused_even_for_the_same_cohort():
    """6N tanks are type "OG". Before this change `not is_empty` was the only
    thing keeping a hand-written event out of one; the entry-tier guard is what
    replaces it. If this test fails, the 6N purge rule has a hole in it."""
    st = _state(_tank(61, "OG6N", batch="B49", count=50_000, wt=4200.0,
                      stage="STARVE"))
    ev = TranOGEntry(batch_id="B49", event_date=DAY,
                     destinations=[TankAllocation(tank_id=61, count=125_099,
                                                  avg_wt_g=416.0, cv_pct=16.0)])
    warns = ev.apply(st)
    assert any("not entry" in w or "R1" in w for w in warns)
    assert ev.count_placed == 0.0
    assert st.tanks_by_id[61].count == pytest.approx(50_000)


def test_an_occupied_grow_out_tank_is_refused_even_for_the_same_cohort():
    """R1 confines FW arrivals to OG1/OG2 whether the tank is empty or not."""
    st = _state(_tank(31, "OG3N", batch="B49", count=40_000, wt=2300.0,
                      stage=STAGE_SW))
    ev = TranOGEntry(batch_id="B49", event_date=DAY,
                     destinations=[TankAllocation(tank_id=31, count=125_099,
                                                  avg_wt_g=416.0, cv_pct=16.0)])
    assert any("R1" in w or "not entry" in w for w in ev.apply(st))
    assert ev.count_placed == 0.0


def test_an_off_feed_entry_tank_is_refused():
    """A tank in STARVE is not on feed; a FW intake must not land on it."""
    st = _state(_tank(14, "OG1S", batch="B49", count=25_173, wt=397.0,
                      stage="STARVE"))
    ev = TranOGEntry(batch_id="B49", event_date=DAY,
                     destinations=[TankAllocation(tank_id=14, count=125_099,
                                                  avg_wt_g=416.0, cv_pct=16.0)])
    assert any("not on feed" in w for w in ev.apply(st))
    assert ev.count_placed == 0.0


# --------------------------------------------------------------------------- #
# The editor must agree with the engine
# --------------------------------------------------------------------------- #
def _ev(batch, tank):
    from forecast.manual_events import ManualDest, ManualEvent
    return ManualEvent(type="fw_to_og", week=1, batch=batch,
                       destinations=[ManualDest(tank=tank)])


def test_validator_accepts_the_same_cohort():
    from forecast.manual_events import _validate_fw_to_og_structural
    st = _state(_tank(14, "OG1S", batch="B49", count=25_173, wt=397.0,
                      stage=STAGE_SW))
    assert _validate_fw_to_og_structural(st, _ev("B49", 14)) == []


def test_validator_refuses_a_different_cohort():
    from forecast.manual_events import _validate_fw_to_og_structural
    st = _state(_tank(14, "OG1S", batch="B48", count=100_308, wt=842.0,
                      stage=STAGE_SW))
    msgs = _validate_fw_to_og_structural(st, _ev("B49", 14))
    assert msgs and any("DIFFERENT batch" in m for m in msgs)


def test_validator_refuses_an_occupied_6N_tank():
    """The is_entry branch sits ahead of the emptiness branch in the elif
    chain; that ordering is the 6N guarantee at this site."""
    from forecast.manual_events import _validate_fw_to_og_structural
    st = _state(_tank(61, "OG6N", batch="B49", count=50_000, wt=4200.0,
                      stage="STARVE"))
    msgs = _validate_fw_to_og_structural(st, _ev("B49", 61))
    assert msgs and any("R1" in m or "entry tier" in m for m in msgs)
