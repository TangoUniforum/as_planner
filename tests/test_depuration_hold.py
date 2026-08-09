"""Depuration hold (operator rule): fish moved into a 6N purge tank sit the
full ~2-week purge (SIXN_MIN_RESIDENCY_DAYS) before that tank may drain.

The audited leak (July-2026 PRs, controller-hybrid): the anticipatory /
reactive make-room dumped whole grow-out tanks into the EMPTY SISTER of the
pair at the FRONT of the purge queue — drained the very next week, shipping
17-33k fish with 1 week of purge (e.g. 33,206 fish into OG6N-71 at 2026-W39,
drained 2026-W40, on the 7.17.26 PR).

Enforced at BOTH ends:
  * fill side  — _free_6n_slots takes an `avoid` set (the imminent-drain
    pair's tanks) threaded through _make_room_into_6n and the entry transit;
  * drain side — _run_sixn_purge_week refuses to drain a tank whose recorded
    fill (state.sixn_fill_date, stamped by _freeze_6n_dest) is younger than
    the hold; the tank rides to the pair's next rotation. Tanks with no
    recorded fill (PR-hydrated fish already purging at forecast start) are
    treated as old enough — their residency clock predates the horizon.
"""
from __future__ import annotations

from datetime import date, timedelta

from forecast.models import ControlParams
from forecast.placement import (
    SIXN_MIN_RESIDENCY_DAYS,
    _free_6n_slots,
    _freeze_6n_dest,
    _make_room_into_6n,
    _run_sixn_purge_week,
)
from forecast.state import STAGE_STARVE, FacilityState, TankState

TODAY = date(2026, 8, 3)


def _mk_state():
    """Grow-out tanks + the full 6N (3 main/sister pairs)."""
    return FacilityState(TODAY, [
        TankState("OG3N-31", 31, "OG3N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG4N-41", 41, "OG4N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG6N-61", 61, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-67", 67, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-63", 63, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-69", 69, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-65", 65, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-71", 71, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
    ])


def _mk_control(**over):
    kw = dict(
        forecast_start=TODAY, horizon_weeks=10, scenario_name="t",
        max_feed_per_day_kg=34000.0, max_biomass_kg=3.8e6,
        max_harvest_per_week=55000.0, min_harvest_weight_g=3500.0,
        min_harvest_per_week=30000.0, min_tank_control=7000.0,
        default_hog_yield=0.81, facility_biomass_deviation_pct=0.005,
        handling_mortality_pct=0.01, sixn_growth=False,
    )
    kw.update(over)
    return ControlParams(**kw)


# ---------------------------------------------------------------------------
# Fill side: avoid set
# ---------------------------------------------------------------------------

class TestFreeSlotsAvoid:
    def test_avoid_demotes_imminent_drain_tanks_to_last(self):
        s = _mk_state()
        # resting (61,67); front-of-queue pair (65,71) drains next week.
        slots = _free_6n_slots(s, (61, 67), avoid=frozenset((65, 71)))
        assert slots[:2] == [61, 67]        # resting pair still preferred
        assert slots[-2:] == [65, 71]       # imminent-drain pair only last

    def test_no_avoid_is_previous_behaviour(self):
        s = _mk_state()
        assert _free_6n_slots(s, (61, 67))[:2] == [61, 67]
        assert 71 in _free_6n_slots(s, (61, 67))

    def test_last_resort_dump_is_stamped_so_the_drain_guard_holds_it(self):
        """The audited W39 shape: resting pair already re-occupied, the only
        empty 6N tanks belong to the pair draining next week. Make-room still
        SUCCEEDS (refusing was measured to flip PR_CORRECTION trial verdicts
        and reshape whole plans) — but the fill is date-stamped, so next
        week's drain HOLDS the tank and the fish get their full purge."""
        s = _mk_state()
        # Resting pair tanks occupied (this week's rotation already refilled).
        s.tanks_by_id[63].assign("B43", 7000, 3800.0, 16.0, STAGE_STARVE)
        s.tanks_by_id[69].assign("B44", 26000, 3800.0, 16.0, STAGE_STARVE)
        # Mains of the other two pairs also mid-purge.
        s.tanks_by_id[61].assign("B42", 26000, 3800.0, 16.0, STAGE_STARVE)
        s.tanks_by_id[65].assign("B43", 33000, 3800.0, 16.0, STAGE_STARVE)
        # -> empty 6N tanks are exactly 67 and 71 (both in avoid).
        src = s.tanks_by_id[31]
        src.assign("B44", 33206, 3740.0, 16.0, "SW")
        warns: list[str] = []
        ok = _make_room_into_6n(
            s, src, TODAY, (63, 69), [], warns, "2026-W39",
            reason="test", is_purge=True, avoid=frozenset((65, 71, 61, 67)))
        assert ok is True                          # dump proceeds (last resort)
        dumped = 67 if not s.tanks_by_id[67].is_empty else 71
        assert s.sixn_fill_date.get(dumped) == TODAY
        # ... and the drain a week later holds exactly that tank.
        harvests, hold_warns, _ = _run_week(
            s, [(65, 71) if dumped == 71 else (61, 67)], (63, 69),
            TODAY + timedelta(days=7))
        assert dumped not in {h.source_tank_id for h in harvests}
        assert any("DEPURATION HOLD" in w for w in hold_warns)

    def test_make_room_prefers_legal_slot_over_avoided(self):
        s = _mk_state()
        src = s.tanks_by_id[31]
        src.assign("B44", 33206, 3740.0, 16.0, "SW")
        warns: list[str] = []
        ok = _make_room_into_6n(
            s, src, TODAY, (63, 69), [], warns, "2026-W39",
            reason="test", is_purge=True, avoid=frozenset((65, 71)))
        assert ok is True
        assert s.tanks_by_id[63].batch_id == "B44"     # resting main took it
        assert s.tanks_by_id[71].is_empty
        # fill date stamped for the drain guard
        assert s.sixn_fill_date.get(63) == TODAY


# ---------------------------------------------------------------------------
# Drain side: the fail-safe hold
# ---------------------------------------------------------------------------

def _run_week(state, pair_queue, resting, week_start, refill=False):
    harvests, transfers, warns = [], [], []
    new_resting = _run_sixn_purge_week(
        state=state, pair_queue=pair_queue, week_label="2026-W40",
        week_start_date=week_start, batch_meta={}, control=_mk_control(),
        harvest_events=harvests, transfer_events=transfers, warnings=warns,
        resting_pair=resting, refill=refill,
    )
    return harvests, warns, new_resting


class TestDrainGuard:
    def test_recent_fill_is_held_mature_tank_drains(self):
        s = _mk_state()
        wk = TODAY
        s.tanks_by_id[65].assign("B43", 33453, 3800.0, 16.0, STAGE_STARVE)
        s.tanks_by_id[71].assign("B44", 33189, 3740.0, 16.0, STAGE_STARVE)
        # 65 filled a full purge ago; 71 filled last week (the leak shape).
        _freeze_6n_dest(s, 65, fill_date=wk - timedelta(days=SIXN_MIN_RESIDENCY_DAYS))
        _freeze_6n_dest(s, 71, fill_date=wk - timedelta(days=7))
        harvests, warns, _ = _run_week(s, [(65, 71)], (63, 69), wk)
        harvested_tanks = {h.source_tank_id for h in harvests}
        assert 65 in harvested_tanks               # exactly-14d fill is legal
        assert 71 not in harvested_tanks           # held
        assert not s.tanks_by_id[71].is_empty
        assert s.tanks_by_id[71].count == 33189    # fish untouched
        assert any("DEPURATION HOLD" in w for w in warns)

    def test_ragged_first_week_second_rotation_drain_is_legal(self):
        """A rotation fill dated in the PARTIAL first forecast week reaches its
        on-schedule 2nd-rotation drain at 8-13 event days (physically the
        normal Wed-fill -> Fri-harvest two-week purge). Holding it was measured
        to put a ZERO-harvest week back (7.2 + 7.29 PRs) — it must drain."""
        s = _mk_state()
        wk = TODAY
        s.tanks_by_id[61].assign("B42", 31469, 4057.0, 16.0, STAGE_STARVE)
        _freeze_6n_dest(s, 61, fill_date=wk - timedelta(days=9))
        harvests, warns, _ = _run_week(s, [(61, 67)], (63, 69), wk)
        assert {h.source_tank_id for h in harvests} == {61}
        assert not any("DEPURATION HOLD" in w for w in warns)

    def test_unrecorded_fill_drains_normally(self):
        """PR-hydrated fish (no witnessed fill) must keep draining — their
        residency clock predates the horizon (the audited 'startup 1-week
        draws' were exactly these, a measurement artifact, not a leak)."""
        s = _mk_state()
        s.tanks_by_id[65].assign("B41", 26548, 3800.0, 16.0, STAGE_STARVE)
        harvests, warns, _ = _run_week(s, [(65, 71)], (63, 69), TODAY)
        assert {h.source_tank_id for h in harvests} == {65}
        assert not any("DEPURATION HOLD" in w for w in warns)

    def test_held_tank_drains_on_next_rotation(self):
        s = _mk_state()
        wk = TODAY
        s.tanks_by_id[71].assign("B44", 33189, 3740.0, 16.0, STAGE_STARVE)
        _freeze_6n_dest(s, 71, fill_date=wk - timedelta(days=7))
        q = [(65, 71)]
        harvests, warns, resting = _run_week(s, q, (63, 69), wk)
        assert harvests == [] and not s.tanks_by_id[71].is_empty
        # Pair rejoins the rotation (winddown path appends it) — next visit,
        # a week later, the hold is satisfied and the tank drains.
        harvests2, warns2, _ = _run_week(s, [(65, 71)], resting,
                                         wk + timedelta(days=7))
        assert {h.source_tank_id for h in harvests2} == {71}
        assert s.tanks_by_id[71].is_empty
