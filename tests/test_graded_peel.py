"""The floor-fill graded peel: two conflated rules and a backwards test order.

`_try_graded_move_in` is the EXCEPTION the 6N rotation falls back on when whole
mature tanks cannot fill the week's harvest floor: peel the market-weight tail
out of a near-ripe grow-out tank, send it to the purge pickup, leave the small
tail behind. Two defects made it refuse to fire on exactly the weeks it exists
for (measured on the 2026-08-31 ProductionReport, B45/B46, 2026-W44..W53).

(A) CONFLATED MINIMUMS. Three different operator rules were spelled with the
    same 7,000:
      min_tank_control   how thin a tank may be LEFT           (a remnant rule)
      min_transfer_count how small a group is worth a PUMP     (the rebalancer)
      the graded peel    how small a ripe tail is worth a GRADER
    The peel borrowed the pump's number, so a tank holding a genuine 5,000-fish
    market-weight tail was refused. `min_grade_count` is the peel's own rule;
    left unset it inherits `min_transfer_count`, so the shipped default is
    unchanged.

(B) CAP BEFORE MINIMUM. `max_count` is the SURGICAL CAP -- how many fish this
    week's floor still needs. It was applied BEFORE the minimum test, so the
    week's own shortfall was being read as if it said something about the
    TANK: a 900-fish gap could never be closed by grading, no matter how much
    market-weight fish stood in the water. The tank's OWN tail decides whether
    it can be graded; the cap decides how much of it to take.

These are BEHAVIOURAL tests -- "does the peel fire, and how much does it take"
-- not pinned tonnages. The plan-level numbers live in the commit message.
"""
from __future__ import annotations

from datetime import date, datetime

from forecast.models import BatchInput, ControlParams
from forecast.placement import _try_graded_move_in
from forecast.state import FacilityState, TankState

TODAY = date(2026, 11, 2)
BATCH = "B45"


def _mk_state(count=40_000.0, avg_wt_g=3_380.0, cv_pct=16.0):
    """One near-ripe grow-out tank + a free 6N pair to peel into.

    3,380 g at CV 16% puts ~35% of the tank over a 3,500 g gate: a real
    market-weight tail in a tank whose MEAN is below the gate -- the exact
    shape the peel exists for.
    """
    st = FacilityState(TODAY, [
        TankState("OG4N-41", 41, "OG4N", 1000.0, 95.0, 3000.0, "OG"),
        TankState("OG6N-61", 61, "OG6N", 1000.0, 120.0, 3000.0, "OG"),
        TankState("OG6N-67", 67, "OG6N", 1000.0, 120.0, 3000.0, "OG"),
    ])
    st.tanks_by_id[41].assign(batch_id=BATCH, count=count, avg_wt_g=avg_wt_g,
                              cv_pct=cv_pct, stage="SW")
    return st


def _mk_control(**over):
    kw = dict(
        forecast_start=TODAY, horizon_weeks=10, scenario_name="t",
        max_feed_per_day_kg=34000.0, max_biomass_kg=3.8e6,
        max_harvest_per_week=55000.0, min_harvest_weight_g=3500.0,
        min_harvest_per_week=30000.0, min_tank_control=7000.0,
        default_hog_yield=0.81, facility_biomass_deviation_pct=0.005,
        handling_mortality_pct=0.0, sixn_growth=False,
        min_transfer_count=7000.0,
    )
    kw.update(over)
    return ControlParams(**kw)


def _meta():
    return {BATCH: BatchInput(
        batch_id=BATCH, input_date=datetime(2025, 6, 2), input_count=400_000,
        tran_sf_date=None, tran_og_date=None, tran_og_count=None,
        tran_og_avg_wt_g=None, tran_og_cv=16.0, fcr_model="FCR_121_Quick",
        fw_correction=1.0, sgr_correction=1.0)}


def _peel(state, control, max_count=None):
    """Run one peel; return (fish moved, warnings)."""
    warns: list[str] = []
    moved = _try_graded_move_in(
        state, _meta(), control, "2026-W44", TODAY, (61, 67), [], warns,
        tables=None, retain_in_source=True, max_count=max_count)
    return moved, warns


# ---------------------------------------------------------------------------
# (B) the cap decides how MUCH, never WHETHER
# ---------------------------------------------------------------------------

class TestCapOrder:
    def test_a_small_shortfall_is_closed_exactly(self):
        """The defect in one line: a 900-fish gap used to be unclosable
        because 900 < 7,000, even though the tank held a ~14,000-fish tail."""
        st = _mk_state()
        moved, _ = _peel(st, _mk_control(), max_count=900.0)
        assert moved > 0, "the peel refused a gap smaller than the peel minimum"
        assert abs(moved - 900.0) < 0.5, "took more than the floor asked for"

    def test_the_cap_never_takes_more_than_the_tail(self):
        """A cap larger than the tank's ripe tail must not invent ripe fish."""
        st = _mk_state()
        moved, _ = _peel(st, _mk_control(), max_count=1_000_000.0)
        assert 0 < moved < 40_000, moved

    def test_an_uncapped_peel_takes_the_whole_tail(self):
        st_capped = _mk_state()
        st_free = _mk_state()
        big, _ = _peel(st_free, _mk_control(), max_count=None)
        small, _ = _peel(st_capped, _mk_control(), max_count=5_000.0)
        assert small < big

    def test_the_small_take_leaves_a_fatter_remnant(self):
        """The operator's constraint: 'without leaving all the tanks with low
        counts of fish'. Taking only what the floor needs is what PROTECTS the
        source tank -- a fact the old order could not exploit."""
        st_small = _mk_state()
        st_big = _mk_state()
        _peel(st_small, _mk_control(), max_count=900.0)
        _peel(st_big, _mk_control(), max_count=None)
        assert st_small.tanks_by_id[41].count > st_big.tanks_by_id[41].count
        assert st_small.tanks_by_id[41].count >= 7000.0


# ---------------------------------------------------------------------------
# (A) the graded tail has its own minimum
# ---------------------------------------------------------------------------

class TestPeelMinimum:
    def test_unset_inherits_min_transfer_count(self):
        """The shipped default must not change any existing plan."""
        assert _mk_control().min_grade_count is None
        st_a, st_b = _mk_state(), _mk_state()
        a, _ = _peel(st_a, _mk_control(min_transfer_count=7000.0))
        b, _ = _peel(st_b, _mk_control(min_transfer_count=7000.0,
                                       min_grade_count=7000.0))
        assert a == b

    def test_a_tail_under_the_peel_minimum_is_refused(self):
        # 12,000-fish tank, ~35% tail => ~4,200 fish: under a 7,000 floor.
        st = _mk_state(count=12_000.0)
        moved, _ = _peel(st, _mk_control(min_grade_count=7000.0))
        assert moved == 0.0

    def test_the_same_tail_is_taken_once_the_peel_has_its_own_floor(self):
        """(A) exactly: nothing about the FISH changed, only which rule the
        planner measured them against."""
        st = _mk_state(count=12_000.0)
        moved, _ = _peel(st, _mk_control(min_grade_count=3000.0))
        assert moved > 0

    def test_zero_means_no_floor_not_inherit(self):
        """0 is a real setting ('no floor'), not an unset sentinel."""
        st = _mk_state(count=12_000.0)
        moved, _ = _peel(st, _mk_control(min_grade_count=0.0))
        assert moved > 0


# ---------------------------------------------------------------------------
# what must NOT move: the remnant floor still governs what is LEFT
# ---------------------------------------------------------------------------

class TestRemnantFloorStillHolds:
    def test_a_peel_that_would_strand_a_dribble_is_refused(self):
        """min_tank_control is a DIFFERENT rule from the peel minimum and the
        loosened peel must not be allowed to eat it. A 9,000-fish tank with a
        ~3,150-fish tail would leave ~5,850 behind -- under the 7,000 floor."""
        st = _mk_state(count=9_000.0)
        moved, _ = _peel(st, _mk_control(min_grade_count=0.0))
        left = st.tanks_by_id[41].count
        assert moved == 0.0 or left == 0.0 or left >= 7000.0

    def test_a_capped_peel_is_judged_on_what_it_actually_takes(self):
        """The remnant test must read the POST-cap amount: judging the full
        tail would refuse a small, harmless peel out of the same small tank
        the test above rightly refuses to strip."""
        st = _mk_state(count=9_000.0)
        moved, _ = _peel(st, _mk_control(min_grade_count=0.0), max_count=800.0)
        assert moved > 0
        assert st.tanks_by_id[41].count >= 7000.0
