"""GLOBAL-method constraint parity — negative controls for the placement repair.

The Global engines must respect every constraint the controller family does;
otherwise a compare board is not a comparison. This suite guards the repairs
made 2026-08-11, each one a constraint that was ABSENT rather than merely
mis-tuned. Policy is the project's: a check exists to DETECT a defect, and a
check that cannot fire is itself a defect — so every constraint here gets a
control proving it BINDS plus a positive control proving it stays quiet on
clean input.

Measured on the operator's 7.29 PR + scenario/manual_events/2026-07-31.yaml:
  * 33-vs-36: `production_tanks_per_system` / `production_systems_for_week`
    were CORRECT but had ZERO call sites — `plan_l3` used the mode-blind
    `n_tanks_per_system`, so production-era weeks were placed into a facility
    3 tanks smaller than the real one. Cost: B65+B66 = 1,140,000 fish dropped.
  * never-drop: a batch L1 seeded but the tank pick could not place simply
    VANISHED from BatchLocations while the batch-level ReconciliationReport
    still called it standing and conserving (continuity audits are blind to a
    batch that was never placed). 570,000 fish at 2028-W49, with 13 of 39
    tanks empty and 400,000 kg of headroom.
  * STARVE-is-a-state: the 6N stage stamp keyed off the TANK ID, so once the
    mains became production grow-out their fish would have been stamped
    off-feed depuration — hiding them from every density/welfare metric (all
    of which exclude purge rows) and corrupting the depuration-hold audit.
"""
import os
import sys
from datetime import datetime

import openpyxl
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forecast import excel_io, window_weeks                       # noqa: E402
from forecast.global_planner_l3_poc import (                      # noqa: E402
    n_tanks_per_system,
    production_systems_for_week,
    production_tanks_per_system,
)
from forecast.models import ControlParams, FacilityConfig, TankConfig  # noqa: E402
from forecast.sixn import SIXN_MAIN_TANKS, SIXN_SISTER_TANKS      # noqa: E402


# --------------------------------------------------------------------------- #
# A facility shaped like the real one: 11 production systems x 3 tanks, plus
# OG6N's 3 mains + 3 sisters.
# --------------------------------------------------------------------------- #
def _facility():
    tanks = []
    for grp, sysbase in enumerate(["OG1", "OG2", "OG3", "OG4", "OG5"], start=1):
        for i in range(6):
            tid = grp * 10 + 1 + i
            sysid = sysbase + ("N" if i % 2 == 0 else "S")
            tanks.append(TankConfig(f"{sysid}-{tid}", sysid, tid, 1720.0, 95.0,
                                    3000.0, "OG"))
    for tid in sorted(set(SIXN_MAIN_TANKS) | set(SIXN_SISTER_TANKS)):
        tanks.append(TankConfig(f"OG6N-{tid}", "OG6N", tid, 1720.0, 95.0,
                                3000.0, "OG"))
    # OG6S: the 7th grow-out system (3 tanks), as on the real facility.
    for i, tid in enumerate((66, 68, 70)):
        tanks.append(TankConfig(f"OG6S-{tid}", "OG6S", tid, 1720.0, 95.0,
                                3000.0, "OG"))
    return FacilityConfig(tanks=tanks)


def _control(prod_start="2028-01-01"):
    c = ControlParams(
        forecast_start=datetime(2026, 8, 3), horizon_weeks=130,
        scenario_name="t", max_feed_per_day_kg=34000.0, max_biomass_kg=3.8e6,
        max_harvest_per_week=55000.0, min_harvest_weight_g=3500.0,
        min_harvest_per_week=30000.0, min_tank_control=7000.0,
        default_hog_yield=0.81, facility_biomass_deviation_pct=0.005,
        handling_mortality_pct=0.01, sixn_growth=False)
    c.sixn_production_start = datetime.fromisoformat(prod_start)
    return c


PURGE_WK, PROD_WK = "2027-W20", "2028-W20"


class TestModeAwareTankCount:
    """33 tanks in purge era, 36 in production era — and OG6N's 3 SISTERS are
    never production in either mode (they are harvest staging)."""

    def test_purge_week_has_no_production_6n_tanks(self):
        n = production_tanks_per_system(_facility(), _control(), PURGE_WK)
        assert n.get("OG6N", 0) == 0
        assert sum(n.values()) == 33
        assert "OG6N" not in production_systems_for_week(
            _facility(), _control(), PURGE_WK)

    def test_production_week_adds_the_three_6n_mains(self):
        n = production_tanks_per_system(_facility(), _control(), PROD_WK)
        assert n.get("OG6N", 0) == 3, "the 3 MAINS become grow-out production"
        assert sum(n.values()) == 36
        assert "OG6N" in production_systems_for_week(
            _facility(), _control(), PROD_WK)

    def test_sisters_are_never_production_in_either_mode(self):
        """The 3 sisters exist so two batches can stage for harvest the same
        week without co-mingling; counting them as grow-out would invent
        capacity that physically is not there."""
        f = _facility()
        for wk in (PURGE_WK, PROD_WK):
            n = production_tanks_per_system(f, _control(), wk)
            assert n.get("OG6N", 0) <= len(SIXN_MAIN_TANKS)

    def test_the_mode_blind_count_really_is_different(self):
        """NEGATIVE CONTROL for the whole fix: if the mode-blind helper agreed
        with the mode-aware one, wiring it would be a no-op and this suite
        would be proving nothing. It does not — it over-counts by the 3
        sisters and is flat across the mode boundary."""
        f = _facility()
        raw = n_tanks_per_system(f)
        assert raw.get("OG6N", 0) == 6            # mains + sisters, no mode
        assert (production_tanks_per_system(f, _control(), PURGE_WK)
                != production_tanks_per_system(f, _control(), PROD_WK))


class TestPlanL3UsesTheModeAwareCount:
    """The helpers above were correct for months while having ZERO call sites.
    Correctness is worthless unwired, so guard the WIRING, not just the math."""

    def test_plan_l3_calls_production_tanks_per_system(self, monkeypatch):
        """Spy: plan_l3 must consult the mode-aware count. Patched to raise a
        sentinel, so reaching it proves it is on the placement path."""
        import forecast.global_planner_l3_poc as l3

        class _Reached(Exception):
            pass

        def _spy(*a, **k):
            raise _Reached()

        monkeypatch.setattr(l3, "production_tanks_per_system", _spy)

        class _L1:
            batch_standing = [
                l3.BatchStandingRow(week=0, week_label=PROD_WK, batch_id="B1",
                                    count=100000.0, biomass_kg=100000.0,
                                    avg_wt_g=1000.0, feed_kg_day=500.0),
            ]

        with pytest.raises(_Reached):
            l3.plan_l3(_L1(), _control(), _facility(), None)

    def test_the_spy_would_not_fire_without_the_wiring(self, monkeypatch):
        """Positive control for the spy itself: patching the OTHER (mode-blind)
        helper must NOT raise, so the test above is detecting the specific
        call it claims to."""
        import forecast.global_planner_l3_poc as l3

        class _Reached(Exception):
            pass

        monkeypatch.setattr(l3, "n_tanks_per_system",
                            lambda *a, **k: (_ for _ in ()).throw(_Reached()))

        class _L1:
            batch_standing = [
                l3.BatchStandingRow(week=0, week_label=PROD_WK, batch_id="B1",
                                    count=100000.0, biomass_kg=100000.0,
                                    avg_wt_g=1000.0, feed_kg_day=500.0),
            ]
        try:
            l3.plan_l3(_L1(), _control(), _facility(), None)
        except _Reached:
            pytest.fail("plan_l3 still routes its tank capacity through the "
                        "mode-blind n_tanks_per_system")
        except Exception:
            pass          # any other failure is fine; we only assert the route


class TestUnplacedBatchIsLoud:
    """Fish with L1 standing but no physical tank must be IMPOSSIBLE to miss.
    They previously vanished while conservation still reported them standing."""

    def test_unplaced_batch_files_as_an_error_not_a_hydration_note(self):
        wb = openpyxl.Workbook()
        excel_io.write_validation_log(wb, invariant_warnings=[
            "UNPLACED BATCH - 2028-W49: batch B66 (570,000 kg) has L1 standing "
            "but NO legal free tank in its tier (grow-out); it is absent from "
            "BatchLocations."])
        rows = [r for r in wb["ValidationLog"].iter_rows(values_only=True)
                if r and isinstance(r[0], int)]
        assert len(rows) == 1
        assert rows[0][1].startswith("ERROR"), \
            f"an unplaced batch filed as {rows[0][1]!r}"
        assert "Unplaced batch" in rows[0][1]

    def test_unplaced_warning_cannot_be_read_as_a_manual_window_week(self):
        """It carries an ISO week label, so it MUST NOT also carry the manual
        markers — a planner week wrongly tagged 'window' is excluded from the
        harvest-compliance gates, hiding breaches in the degraded run."""
        wb = openpyxl.Workbook()
        excel_io.write_validation_log(wb, invariant_warnings=[
            "UNPLACED BATCH - 2028-W49: batch B66 has L1 standing but no tank.",
            "MANUAL EVENT OK - 2026-W31: harvested 21,812 fish",
        ])
        assert window_weeks.manual_window_weeks(wb) == {"2026-W31"}

    def test_the_pick_exposes_its_unplaced_list(self):
        """The pick's result must CARRY the misses so the caller can record
        them; a stdout-only degrade is invisible to the graders."""
        from forecast.global_tank_pick_poc import TankPickResult
        r = TankPickResult(
            batch_locations=[], transfers=[], tranog_events=[],
            harvest_events=[], realized_biology={}, mort_states=[],
            n_transfers=0, n_oversub_rows=0, oversub_weeks=[], n_tank_weeks=0)
        assert r.unplaced_warnings == []       # clean run stays quiet
        r2 = TankPickResult(
            batch_locations=[], transfers=[], tranog_events=[],
            harvest_events=[], realized_biology={}, mort_states=[],
            n_transfers=0, n_oversub_rows=0, oversub_weeks=[], n_tank_weeks=0,
            unplaced_warnings=["UNPLACED BATCH - x"])
        assert r2.unplaced_warnings


class TestStarveIsAStateNotATankId:
    """Once the 6N mains carry production grow-out, stamping them STARVE would
    hide those fish from every density/welfare metric (they all exclude purge
    rows) and corrupt the depuration-hold audit."""

    def test_density_metrics_exclude_starve_rows(self):
        """The reason the stamp matters: a STARVE row is invisible to the
        density peak. If production fish were stamped STARVE, an over-packed
        6N main would read as a clean facility."""
        from forecast.optimize import _is_purge_row
        hdr_si, hdr_sti = 4, 9
        starve = ("2028-W20", None, "B1", 61, "OG6N", 1, 1.0, 1, 500.0, "STARVE")
        grow = ("2028-W20", None, "B1", 61, "OG6N", 1, 1.0, 1, 500.0, "SW")
        assert _is_purge_row(starve, hdr_si, hdr_sti) is True
        assert _is_purge_row(grow, hdr_si, hdr_sti) is False

    def test_stage_is_keyed_off_depuration_not_the_tank_id(self):
        """WIRING guard: the pick must decide STARVE from the set of tanks it
        actually filled with depuration fish, never from `tid in sixn_set`."""
        import inspect
        from forecast import global_tank_pick_poc as tp
        src = inspect.getsource(tp.pick_tanks)
        assert 'stage = "STARVE" if tid in _depurating else ""' in src
        assert 'stage = "STARVE" if tid in sixn_set else ""' not in src
