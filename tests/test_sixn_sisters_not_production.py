"""6N SISTER tanks (67/69/71) are never PRODUCTION capacity.

DESIGN §5 / forecast/sixn.py: after the purge->production switch the 6N MAINS
(61/63/65) become ordinary grow-out, but the sisters stay harvest-staging tanks
and are dropped from availability. Every destination filter in the weekly walk
enforces that -- except, historically, the two PRODUCTION in-place-purge staging
sites (the entry forward-promotion and the graded-stage last resort), which
excluded only the entry tier. Measured consequence on the 7.29.26 PR: OG6N-67
took a promoted entry tank at 2028-W13 and graded-stage tails at 2028-W20 and
2028-W27, so the production facility silently ran with a 37th tank that does
not exist.

Both sites now go through `_free_production_stage_tank`; these tests pin its
three exclusions so the two call sites cannot drift apart again.
"""
from __future__ import annotations

from datetime import date

from forecast.placement import _free_production_stage_tank
from forecast.state import FacilityState, TankState

TODAY = date(2026, 8, 3)


def _mk_state():
    """Entry tier, two grow-out tanks, and the full 6N block."""
    return FacilityState(TODAY, [
        TankState("OG1N-11", 11, "OG1N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG2S-22", 22, "OG2S", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG3N-31", 31, "OG3N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG5S-55", 55, "OG5S", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG6N-61", 61, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-63", 63, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-65", 65, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-67", 67, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-69", 69, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
        TankState("OG6N-71", 71, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
    ])


def _fill(state, *tank_ids, batch="B50"):
    for tid in tank_ids:
        state.tanks_by_id[tid].assign(batch, 10000, 3800.0, 16.0, "SW")


class TestProductionStageDestination:
    def test_prefers_lowest_id_growout(self):
        s = _mk_state()
        assert _free_production_stage_tank(s).tank_id == 31

    def test_never_the_entry_tier(self):
        """R5: the two entry tanks are the LOWEST free ids, and a grow-out
        tank is also free -- the grow-out tank must win."""
        s = _mk_state()
        _fill(s, 31, 61, 63, 65)          # free: 11, 22 (entry), 55, sisters
        picked = _free_production_stage_tank(s)
        assert picked is not None
        assert picked.tank_id == 55
        assert picked.system_id not in ("OG1N", "OG2S")

    def test_entry_and_sisters_only_stages_nothing(self):
        """Entry tier + sisters free, nothing else: both exclusions bite."""
        s = _mk_state()
        _fill(s, 31, 55, 61, 63, 65)
        assert _free_production_stage_tank(s) is None

    def test_never_a_6n_sister(self):
        """THE REGRESSION: only the sisters are free -> stage nothing."""
        s = _mk_state()
        _fill(s, 11, 22, 31, 55, 61, 63, 65)
        assert _free_production_stage_tank(s) is None

    def test_6n_mains_are_legal_production_growout(self):
        """The mains are ordinary grow-out in production -- still eligible."""
        s = _mk_state()
        _fill(s, 11, 22, 31, 55)
        picked = _free_production_stage_tank(s)
        assert picked is not None and picked.tank_id == 61

    def test_sister_skipped_in_favour_of_a_higher_main(self):
        """67 sorts BEFORE 69/71 but after 65: a sister must never win even
        when it is the lowest free id."""
        s = _mk_state()
        _fill(s, 11, 22, 31, 55, 61, 63)
        assert _free_production_stage_tank(s).tank_id == 65

    def test_reserved_tanks_are_skipped(self):
        s = _mk_state()
        _fill(s, 11, 22)
        assert _free_production_stage_tank(s, reserved={31}).tank_id == 55

    def test_no_free_tank_returns_none(self):
        s = _mk_state()
        _fill(s, 11, 22, 31, 55, 61, 63, 65, 67, 69, 71)
        assert _free_production_stage_tank(s) is None
