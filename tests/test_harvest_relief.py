"""Harvest limit + pressure relief (operator semantic correction 2026-08-09).

`max_harvest_per_week` (55,000) is THE weekly processing LIMIT — a
CONSTRAINT the demand-driven harvest respects, never a level to plan up
to. `harvest_relief_pct` (0.10) defines the derived absolute ceiling
(limit * 1.10 = 60,500) an EXCEPTIONAL week may reach. The 6N purge
drain defers a pair tank to its next rotation rather than exceed the
limit; only contract-outranking drains (first tank) and make-room
borrows land in the relief band. Gate: PASS = 0 weeks over the limit,
WARN = 1..3 relief weeks, FAIL = >3 relief weeks or any week above the
derived ceiling. The removed `harvest_target_per_week` knob is ignored
with a console note when an old YAML still carries it.
"""
from __future__ import annotations

from datetime import date

from forecast.analysis import _gate_harvest_cap
from forecast.config_io import control_from_dict
from forecast.models import ControlParams
from forecast.placement import (
    _HarvestBudget,
    _run_sixn_purge_week,
)
from forecast.state import STAGE_STARVE, FacilityState, TankState

TODAY = date(2026, 8, 3)

LIMIT = 55000.0


def _mk_control(**over):
    kw = dict(
        forecast_start=TODAY, horizon_weeks=10, scenario_name="t",
        max_feed_per_day_kg=34000.0, max_biomass_kg=3.8e6,
        max_harvest_per_week=LIMIT, min_harvest_weight_g=3500.0,
        min_harvest_per_week=30000.0, min_tank_control=7000.0,
        default_hog_yield=0.81, facility_biomass_deviation_pct=0.005,
        handling_mortality_pct=0.01, sixn_growth=False,
        harvest_relief_pct=0.10,
    )
    kw.update(over)
    return ControlParams(**kw)


# ---------------------------------------------------------------------------
# The knob semantics
# ---------------------------------------------------------------------------

class TestReliefKnob:
    def test_defaults(self):
        # The dataclass default: an old YAML without the key loads at 10%.
        c = _mk_control()
        assert ControlParams.__dataclass_fields__[
            "harvest_relief_pct"].default == 0.10
        assert c.harvest_relief_pct == 0.10
        assert c.max_harvest_per_week == LIMIT

    def test_derived_ceiling(self):
        c = _mk_control()
        ceiling = c.max_harvest_per_week * (1 + c.harvest_relief_pct)
        assert abs(ceiling - 60500.0) < 1e-6

    def test_target_knob_is_gone(self):
        assert "harvest_target_per_week" not in ControlParams.__dataclass_fields__

    def test_old_yaml_with_target_is_ignored_with_note(self, capsys):
        """Config migration: an old control.yaml still carrying the removed
        harvest_target_per_week knob loads fine — value ignored, note
        printed."""
        d = {
            "forecast_start": TODAY.isoformat(), "horizon_weeks": 10,
            "scenario_name": "t", "max_feed_per_day_kg": 34000.0,
            "max_biomass_kg": 3.8e6, "max_harvest_per_week": LIMIT,
            "min_harvest_weight_g": 3500.0, "min_harvest_per_week": 30000.0,
            "min_tank_control": 7000.0, "default_hog_yield": 0.81,
            "facility_biomass_deviation_pct": 0.005,
            "handling_mortality_pct": 0.01, "sixn_growth": False,
            "harvest_target_per_week": 50000,       # the removed knob
        }
        c = control_from_dict(d)
        assert not hasattr(c, "harvest_target_per_week")
        assert c.max_harvest_per_week == LIMIT
        out = capsys.readouterr().out
        assert "harvest_target_per_week" in out and "ignored" in out


# ---------------------------------------------------------------------------
# Gate truth table (PASS 0 / WARN 1..3 / FAIL >3 or ceiling breach)
# ---------------------------------------------------------------------------

class TestHarvestGate:
    def test_pass_at_or_below_limit(self):
        s, d = _gate_harvest_cap({"weeks_over_harvest_cap": 0,
                                  "weeks_over_relief_ceiling": 0})
        assert s == "PASS"

    def test_warn_one_to_three_relief_weeks(self):
        for n in (1, 2, 3):
            s, d = _gate_harvest_cap({"weeks_over_harvest_cap": n,
                                      "weeks_over_relief_ceiling": 0})
            assert s == "WARN" and f"relief used {n}x" in d

    def test_fail_more_than_three_relief_weeks(self):
        s, d = _gate_harvest_cap({"weeks_over_harvest_cap": 4,
                                  "weeks_over_relief_ceiling": 0})
        assert s == "FAIL" and "earlier" in d

    def test_fail_any_week_over_derived_ceiling(self):
        s, d = _gate_harvest_cap({"weeks_over_harvest_cap": 1,
                                  "weeks_over_relief_ceiling": 1})
        assert s == "FAIL" and "relief ceiling" in d

    def test_limit_only_context(self):
        # A context without the ceiling count still applies the relief rule.
        s, _ = _gate_harvest_cap({"weeks_over_harvest_cap": 2})
        assert s == "WARN"
        s, _ = _gate_harvest_cap({"weeks_over_harvest_cap": 5})
        assert s == "FAIL"
        s, _ = _gate_harvest_cap({"weeks_over_harvest_cap": 0})
        assert s == "PASS"

    def test_na_without_series(self):
        assert _gate_harvest_cap({})[0] == "N/A"


# ---------------------------------------------------------------------------
# Limit drain-hold in the purge rotation
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


class TestLimitDrainHold:
    def test_second_tank_held_at_limit(self):
        """The audited make-room-stacked pair: the drain takes what fits
        UNDER the processing limit and HOLDS the rest for the next rotation
        — the rotation never spends the relief band on its own."""
        s = _mk_state()
        s.tanks_by_id[65].assign("B43", 33453, 3800.0, 16.0, STAGE_STARVE)
        s.tanks_by_id[71].assign("B44", 33189, 3740.0, 16.0, STAGE_STARVE)
        harvests, warns = _run_week(s, _HarvestBudget(cap=LIMIT))
        assert {h.source_tank_id for h in harvests} == {65}
        assert sum(h.count for h in harvests) <= LIMIT
        assert not s.tanks_by_id[71].is_empty          # held, not lost
        assert any("HARVEST LIMIT" in w for w in warns)

    def test_within_limit_drains_both(self):
        s = _mk_state()
        s.tanks_by_id[65].assign("B43", 30000, 3800.0, 16.0, STAGE_STARVE)
        s.tanks_by_id[71].assign("B44", 24000, 3740.0, 16.0, STAGE_STARVE)
        harvests, warns = _run_week(s, _HarvestBudget(cap=LIMIT))
        assert {h.source_tank_id for h in harvests} == {65, 71}
        assert not any("HARVEST LIMIT" in w for w in warns)

    def test_first_tank_never_held(self):
        """A whole-week deferral would make an EMPTY week — the contract
        outranks the limit, so the first occupied tank always drains (this
        is one of the exceptional paths the relief band exists for)."""
        s = _mk_state()
        s.tanks_by_id[65].assign("B43", 54000, 3800.0, 16.0, STAGE_STARVE)
        s.tanks_by_id[71].assign("B44", 33189, 3740.0, 16.0, STAGE_STARVE)
        harvests, warns = _run_week(s, _HarvestBudget(cap=LIMIT))
        assert {h.source_tank_id for h in harvests} == {65}
        assert any("HARVEST LIMIT" in w for w in warns)

    def test_no_budget_is_legacy_behaviour(self):
        s = _mk_state()
        s.tanks_by_id[65].assign("B43", 33453, 3800.0, 16.0, STAGE_STARVE)
        s.tanks_by_id[71].assign("B44", 33189, 3740.0, 16.0, STAGE_STARVE)
        harvests, warns = _run_week(s, None)
        assert {h.source_tank_id for h in harvests} == {65, 71}
