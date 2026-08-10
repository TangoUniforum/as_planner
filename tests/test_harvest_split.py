"""Harvest target/ceiling split (operator ruling 2026-08).

50,000 fish/week is the planning TARGET (fills/leveling size to it);
60,000 is the HARD processing ceiling — a week may stretch between the
two when biomass demands it, never past the ceiling. The purge drain
defers a pair tank to its next rotation rather than exceed the ceiling.
Gate: PASS <= target, WARN in the stretch band, FAIL over the ceiling.
"""
from __future__ import annotations

from datetime import date

from forecast.analysis import _gate_harvest_cap
from forecast.models import ControlParams
from forecast.placement import (
    _HarvestBudget,
    _harvest_target_fish,
    _run_sixn_purge_week,
)
from forecast.state import STAGE_STARVE, FacilityState, TankState

TODAY = date(2026, 8, 3)


def _mk_control(**over):
    kw = dict(
        forecast_start=TODAY, horizon_weeks=10, scenario_name="t",
        max_feed_per_day_kg=34000.0, max_biomass_kg=3.8e6,
        max_harvest_per_week=60000.0, min_harvest_weight_g=3500.0,
        min_harvest_per_week=30000.0, min_tank_control=7000.0,
        default_hog_yield=0.81, facility_biomass_deviation_pct=0.005,
        handling_mortality_pct=0.01, sixn_growth=False,
        harvest_target_per_week=50000.0,
    )
    kw.update(over)
    return ControlParams(**kw)


# ---------------------------------------------------------------------------
# The knob semantics
# ---------------------------------------------------------------------------

class TestTargetKnob:
    def test_split(self):
        c = _mk_control()
        assert _harvest_target_fish(c) == 50000.0
        assert c.max_harvest_per_week == 60000.0    # ceiling untouched

    def test_unset_target_falls_back_to_ceiling(self):
        """Pre-split configs (no harvest_target_per_week): the ceiling serves
        as both numbers — the historical single-number behaviour."""
        c = _mk_control(harvest_target_per_week=0.0)
        assert _harvest_target_fish(c) == 60000.0

    def test_target_clamped_to_ceiling(self):
        c = _mk_control(harvest_target_per_week=70000.0)
        assert _harvest_target_fish(c) == 60000.0

    def test_default_is_50k(self):
        # The dataclass default: an old YAML without the key loads at 50k.
        c = _mk_control()
        assert ControlParams.__dataclass_fields__[
            "harvest_target_per_week"].default == 50000.0
        assert c.harvest_target_per_week == 50000.0


# ---------------------------------------------------------------------------
# Gate truth table
# ---------------------------------------------------------------------------

class TestHarvestGate:
    def test_pass_at_or_below_target(self):
        s, d = _gate_harvest_cap({"weeks_over_harvest_cap": 0,
                                  "weeks_over_harvest_target": 0})
        assert s == "PASS"

    def test_warn_in_stretch_band(self):
        s, d = _gate_harvest_cap({"weeks_over_harvest_cap": 0,
                                  "weeks_over_harvest_target": 3})
        assert s == "WARN" and "stretch" in d

    def test_fail_over_ceiling(self):
        s, d = _gate_harvest_cap({"weeks_over_harvest_cap": 1,
                                  "weeks_over_harvest_target": 4})
        assert s == "FAIL" and "ceiling" in d

    def test_legacy_context_stays_warn_only(self):
        s, _ = _gate_harvest_cap({"weeks_over_harvest_cap": 2})
        assert s == "WARN"
        s, _ = _gate_harvest_cap({"weeks_over_harvest_cap": 0})
        assert s == "PASS"

    def test_na_without_series(self):
        assert _gate_harvest_cap({})[0] == "N/A"


# ---------------------------------------------------------------------------
# Ceiling drain-hold in the purge rotation
# ---------------------------------------------------------------------------

def _mk_state():
    return FacilityState(TODAY, [
        TankState("OG6N-65", 65, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-71", 71, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-63", 63, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-69", 69, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
    ])


def _run_week(state, budget):
    harvests, transfers, warns = [], [], []
    _run_sixn_purge_week(
        state=state, pair_queue=[(65, 71)], week_label="2026-W40",
        week_start_date=TODAY, batch_meta={}, control=_mk_control(),
        harvest_events=harvests, transfer_events=transfers, warnings=warns,
        resting_pair=(63, 69), refill=False, budget=budget,
    )
    return harvests, warns


class TestCeilingDrainHold:
    def test_second_tank_held_at_ceiling(self):
        """The audited 86,956-fish shape: a make-room dump stacked the pair
        past the ceiling. The drain takes what fits and HOLDS the rest for
        the next rotation — never a >60k week."""
        s = _mk_state()
        s.tanks_by_id[65].assign("B43", 33453, 3800.0, 16.0, STAGE_STARVE)
        s.tanks_by_id[71].assign("B44", 33189, 3740.0, 16.0, STAGE_STARVE)
        harvests, warns = _run_week(s, _HarvestBudget(cap=60000.0))
        assert {h.source_tank_id for h in harvests} == {65}
        assert sum(h.count for h in harvests) <= 60000
        assert not s.tanks_by_id[71].is_empty          # held, not lost
        assert any("HARVEST CEILING" in w for w in warns)

    def test_within_ceiling_drains_both(self):
        s = _mk_state()
        s.tanks_by_id[65].assign("B43", 30000, 3800.0, 16.0, STAGE_STARVE)
        s.tanks_by_id[71].assign("B44", 25000, 3740.0, 16.0, STAGE_STARVE)
        harvests, warns = _run_week(s, _HarvestBudget(cap=60000.0))
        assert {h.source_tank_id for h in harvests} == {65, 71}
        assert not any("HARVEST CEILING" in w for w in warns)

    def test_first_tank_never_held(self):
        """A whole-week deferral would make an EMPTY week — the contract
        outranks the ceiling, so the first occupied tank always drains."""
        s = _mk_state()
        s.tanks_by_id[65].assign("B43", 59000, 3800.0, 16.0, STAGE_STARVE)
        s.tanks_by_id[71].assign("B44", 33189, 3740.0, 16.0, STAGE_STARVE)
        harvests, warns = _run_week(s, _HarvestBudget(cap=60000.0))
        assert {h.source_tank_id for h in harvests} == {65}
        assert any("HARVEST CEILING" in w for w in warns)

    def test_no_budget_is_legacy_behaviour(self):
        s = _mk_state()
        s.tanks_by_id[65].assign("B43", 33453, 3800.0, 16.0, STAGE_STARVE)
        s.tanks_by_id[71].assign("B44", 33189, 3740.0, 16.0, STAGE_STARVE)
        harvests, warns = _run_week(s, None)
        assert {h.source_tank_id for h in harvests} == {65, 71}
