"""Fast unit tests for pure helpers (no pipeline / no workbook)."""
from __future__ import annotations

import math

from forecast.precalc import _fraction_above, _OG12, _OG36, _OG_ALL


class TestFractionAbove:
    """`_fraction_above` = fraction of a normal(avg, avg*cv) above a cut."""

    def test_at_mean_is_half(self):
        # avg_wt == cut → half the distribution is above.
        assert math.isclose(_fraction_above(1000, 1000, 16), 0.5, abs_tol=1e-9)

    def test_well_above_cut_is_near_one(self):
        assert _fraction_above(1000, 5000, 16) > 0.999

    def test_well_below_cut_is_near_zero(self):
        assert _fraction_above(1000, 200, 16) < 0.001

    def test_bounded_unit_interval(self):
        for avg in (0, 50, 500, 1000, 4000, 9000):
            f = _fraction_above(1000, avg, 16)
            assert 0.0 <= f <= 1.0

    def test_zero_cv_is_step(self):
        # No variance → step function at the cut.
        assert _fraction_above(1000, 1200, 0) == 1.0
        assert _fraction_above(1000, 800, 0) == 0.0

    def test_zero_avg_wt_is_zero(self):
        assert _fraction_above(1000, 0, 16) == 0.0


class TestSystemSets:
    """Eligibility-system partitions used by the exit-at-1kg rule."""

    def test_og12_and_og36_disjoint(self):
        assert _OG12.isdisjoint(_OG36)

    def test_union_is_all(self):
        assert _OG12 | _OG36 == _OG_ALL

    def test_og12_is_nursery_systems(self):
        assert _OG12 == {"OG1N", "OG1S", "OG2N", "OG2S"}

    def test_og6n_in_growout_set(self):
        # OG6N is part of the grow-out system set (pipeline-ownership is
        # handled separately by excluding it from the free pool).
        assert "OG6N" in _OG36


class TestUpperTruncatedSplit:
    """`biology.upper_truncated_split`: conditional means above/below a cut.

    Used by graded-harvest (DESIGN §5a) to peel the >= harvest-weight
    tail off a tank whose average sits just below threshold.
    """

    def test_at_mean_returns_symmetric_means(self):
        from forecast.biology import upper_truncated_split
        # avg == cut: upper and lower means are symmetric about avg
        # with separation sigma * sqrt(2/pi).
        avg, cv = 1000.0, 16.0
        up, lo = upper_truncated_split(avg, cv, avg)
        sigma = avg * cv / 100.0
        expected_gap = sigma * math.sqrt(2.0 / math.pi)
        assert math.isclose(up - avg, expected_gap, rel_tol=1e-6)
        assert math.isclose(avg - lo, expected_gap, rel_tol=1e-6)

    def test_threshold_well_above_avg(self):
        # Threshold far above the mean: upper conditional mean exceeds
        # threshold; lower stays near avg.
        from forecast.biology import upper_truncated_split
        up, lo = upper_truncated_split(800, 16, 1500)
        assert up >= 1500
        assert lo < 800

    def test_degenerate_cv_zero(self):
        from forecast.biology import upper_truncated_split
        up, lo = upper_truncated_split(900, 0, 1000)
        assert up == lo == 900


class TestGradedHarvestEvent:
    """`events.GradedHarvest.apply`: end-to-end peel of an above-threshold
    tail off one source tank into a pickup + retention pair."""

    def _make_state(self):
        from forecast.models import FacilityConfig, TankConfig
        from forecast.state import FacilityState
        cfg = FacilityConfig(tanks=[
            TankConfig(location_id="OG3N-1",
                       system_id="OG3N", tank_id=1, volume_m3=1720,
                       max_density_kg_m3=95, max_feed_kg_day=500, type="OG"),
            TankConfig(location_id="OG6N-61",
                       system_id="OG6N", tank_id=2, volume_m3=1720,
                       max_density_kg_m3=95, max_feed_kg_day=500, type="OG"),
            TankConfig(location_id="OG3S-1",
                       system_id="OG3S", tank_id=3, volume_m3=1720,
                       max_density_kg_m3=95, max_feed_kg_day=500, type="OG"),
        ])
        from datetime import date as _d
        s = FacilityState.from_facility_config(cfg, today=_d(2026, 6, 1))
        s.tanks_by_id[1].assign(
            batch_id="B99", count=100000, avg_wt_g=900.0,
            cv_pct=16.0, stage="SW",
        )
        return s

    def test_apply_drains_source_and_stocks_pickup_retention(self):
        from datetime import date as _d
        from forecast.events import GradedHarvest
        s = self._make_state()
        ev = GradedHarvest(
            batch_id="B99", event_date=_d(2026, 6, 1),
            source_tank_id=1,
            pickup_tank_id=2, pickup_count=30000, pickup_avg_wt_g=1100.0,
            retention_tank_id=3, retention_count=70000, retention_avg_wt_g=815.0,
            cv_pct=16.0,
        )
        warns = ev.apply(s)
        assert warns == []
        assert s.tanks_by_id[1].is_empty
        assert s.tanks_by_id[2].count == 30000
        assert s.tanks_by_id[2].avg_wt_g == 1100.0
        assert s.tanks_by_id[3].count == 70000
        assert s.tanks_by_id[3].avg_wt_g == 815.0
        # Count conserved (within float tolerance).
        assert math.isclose(
            s.tanks_by_id[2].count + s.tanks_by_id[3].count, 100000, abs_tol=1e-6,
        )


class TestPredictiveMoveIn:
    """`caps.predictive_move_in_count`: smooth proportional 6N move-in sizing.

        move_in_mass = growth + gain*(biomass - setpoint) + arrivals_kg
        count        = move_in_mass * 1000 / harvest_avg_wt
    clipped to [min, max]. The damped (gain<1) deviation term + steady-state
    growth feed-forward break the pipeline-feedback lumpiness."""

    SP = 3_900_000.0
    MIN_HV = 30_000.0
    MAX_HV = 55_000.0
    WT = 5_000.0          # 5 kg; kg = count*wt/1000
    GROWTH = 200_000.0    # 200 t/week -> 40k fish at 5 kg

    def _mi(self, biomass, growth=None, setpoint=None, wt=None, gain=0.5,
            arrivals=0.0):
        from forecast.caps import predictive_move_in_count
        return predictive_move_in_count(
            total_biomass=biomass,
            growth_kg_week=self.GROWTH if growth is None else growth,
            setpoint=self.SP if setpoint is None else setpoint,
            harvest_avg_wt_g=self.WT if wt is None else wt,
            weekly_min=self.MIN_HV, weekly_max=self.MAX_HV,
            gain=gain, arrivals_kg=arrivals,
        )

    def test_at_setpoint_replaces_growth(self):
        # biomass == setpoint → move-in replaces exactly one week's growth.
        # 200_000 kg * 1000 / 5_000 g = 40_000 fish.
        assert math.isclose(self._mi(self.SP), 40_000.0, rel_tol=1e-9)

    def test_above_setpoint_raises_move_in_by_gain(self):
        # +100t over setpoint, gain 0.5 → growth + 50t = 250t → 50_000.
        assert math.isclose(self._mi(self.SP + 100_000.0, gain=0.5),
                            50_000.0, rel_tol=1e-9)

    def test_gain_damps_the_correction(self):
        # Deadbeat gain 1.0 on the same +100t → growth + 100t = 300t -> 60_000,
        # clipped to max. A lower gain keeps it in range — that's the point.
        assert self._mi(self.SP + 100_000.0, gain=1.0) == self.MAX_HV
        assert math.isclose(self._mi(self.SP + 100_000.0, gain=0.3),
                            (200_000 + 0.3 * 100_000) * 1000 / 5_000, rel_tol=1e-9)

    def test_below_setpoint_clips_to_floor(self):
        # Far below setpoint → move-in asks for < growth (here negative) →
        # floor, letting biomass build back.
        assert self._mi(self.SP - 800_000.0) == self.MIN_HV

    def test_arrival_feedforward_raises_move_in(self):
        # A scheduled arrival pre-draws: +50t / 5kg = +10_000 fish.
        base = self._mi(self.SP, arrivals=0.0)
        with_arr = self._mi(self.SP, arrivals=50_000.0)
        assert math.isclose(base, 40_000.0, rel_tol=1e-9)
        assert math.isclose(with_arr - base, 10_000.0, rel_tol=1e-9)

    def test_no_setpoint_is_floor(self):
        from forecast.caps import predictive_move_in_count
        out = predictive_move_in_count(
            total_biomass=self.SP, growth_kg_week=self.GROWTH, setpoint=None,
            harvest_avg_wt_g=self.WT, weekly_min=self.MIN_HV,
            weekly_max=self.MAX_HV, gain=0.5, arrivals_kg=0.0)
        assert out == self.MIN_HV

    def test_no_harvest_weight_is_floor(self):
        assert self._mi(self.SP, wt=0.0) == self.MIN_HV


class TestISOWeekConsistency:
    """Reader-produced labels must equal writer-produced labels for the
    same date, regardless of whether forecast_start lands on Monday.
    Locks the property that the audit's R2 (forecast-week vs ISO-week
    mismatch) is now obsolete — all readers/writers route through ISO.
    """

    def test_label_for_date_matches_iso_week_label(self):
        from datetime import date
        from forecast.time_grid import (
            iso_week_label, label_for_date, week_label,
            forecast_week_labels,
        )
        # Non-Monday forecast start (Wednesday).
        fs = date(2026, 5, 13)
        # An event date inside the horizon.
        ev = date(2026, 7, 8)  # Wednesday — should be ISO 2026-W28
        # Writers use iso_week_label(date) directly.
        write_label = iso_week_label(ev)
        # Readers convert (date → forecast week index → label).
        read_label = label_for_date(ev, fs)
        assert write_label == read_label == "2026-W28"

    def test_forecast_week_labels_match_iso_weeks(self):
        from datetime import date
        from forecast.time_grid import (
            iso_week_label, week_start, forecast_week_labels,
        )
        # Non-Monday start: labels[i] must equal iso_week_label of the
        # ISO Monday of week i.
        fs = date(2026, 5, 13)  # Wednesday, ISO W20
        labels = forecast_week_labels(fs, 8)
        # First label is the ISO week containing fs.
        assert labels[0] == "2026-W20"
        # Subsequent labels are consecutive ISO weeks.
        for i in range(1, len(labels)):
            assert labels[i] == iso_week_label(week_start(i, fs))

    def test_partial_week_0_distinct_from_week_1(self):
        from datetime import date
        from forecast.time_grid import forecast_week_labels
        # Wednesday start → W0 is a partial Wed-Sun in ISO Wnn;
        # W1 is full Mon-Sun in ISO W(nn+1). They MUST differ to be
        # collision-free as dictionary keys for the per-week tables.
        fs = date(2026, 5, 13)
        labels = forecast_week_labels(fs, 2)
        assert labels[0] != labels[1]
        assert labels[0] == "2026-W20"
        assert labels[1] == "2026-W21"

    def test_monday_start_no_partial_week(self):
        from datetime import date
        from forecast.time_grid import forecast_week_labels
        fs = date(2026, 5, 11)  # Monday, ISO W20
        labels = forecast_week_labels(fs, 3)
        assert labels == ["2026-W20", "2026-W21", "2026-W22"]

    def test_year_end_iso_week(self):
        from datetime import date
        from forecast.time_grid import iso_week_label
        # 2026-12-29 (Tue) is in ISO 2026-W53.
        assert iso_week_label(date(2026, 12, 29)) == "2026-W53"
        # 2027-01-04 (Mon) is in ISO 2027-W01.
        assert iso_week_label(date(2027, 1, 4)) == "2027-W01"


class TestManualFwToOgConservation:
    """The manual FW->OG override (`_apply_fw_to_og`) must conserve fish: the
    FW count entering the transfer splits exactly into placed + culled, with no
    fish created or leaked. This identity is what the InputConservation FW
    mass-balance gate relies on for a manually-transferred batch (which has no
    FW biology states of its own)."""

    def _state_two_empty_og(self):
        from datetime import date as _d
        from forecast.models import FacilityConfig, TankConfig
        from forecast.state import FacilityState
        cfg = FacilityConfig(tanks=[
            TankConfig(location_id="OG2S-26", system_id="OG2S", tank_id=26,
                       volume_m3=1720, max_density_kg_m3=95,
                       max_feed_kg_day=3000, type="OG"),
            TankConfig(location_id="OG4S-44", system_id="OG4S", tank_id=44,
                       volume_m3=1720, max_density_kg_m3=95,
                       max_feed_kg_day=3000, type="OG"),
        ])
        return FacilityState.from_facility_config(cfg, today=_d(2026, 6, 1))

    def test_culled_plus_placed_equals_fw_count(self):
        from datetime import date as _d
        from forecast.manual_events import (
            ManualEvent, ManualDest, _apply_fw_to_og)
        s = self._state_two_empty_og()
        fw_count = 300000.0
        ev = ManualEvent(type="fw_to_og", week=1, batch="B49", count=250000,
                         destinations=[ManualDest(tank=26), ManualDest(tank=44)])
        tranogs = []
        warns, culled = _apply_fw_to_og(
            s, ev, 1, fw_count, 370.0, 20.0, 0.0001,
            event_date=_d(2026, 6, 1), out_tranog=tranogs)
        placed = sum(d.count for e in tranogs for d in e.destinations)
        # Conservation identity: nothing created or leaked.
        assert math.isclose(placed + culled, fw_count, abs_tol=1e-6)
        # Operator hit the target count exactly.
        assert math.isclose(placed, 250000, abs_tol=1e-6)
        # Tanks now hold exactly the placed fish, split across both dests.
        assert math.isclose(
            s.tanks_by_id[26].count + s.tanks_by_id[44].count, placed, abs_tol=1e-6)

    def _state_four_empty_og(self):
        from datetime import date as _d
        from forecast.models import FacilityConfig, TankConfig
        from forecast.state import FacilityState
        cfg = FacilityConfig(tanks=[
            TankConfig(location_id=f"OG-{t}", system_id="OG2S", tank_id=t,
                       volume_m3=1720, max_density_kg_m3=95,
                       max_feed_kg_day=3000, type="OG")
            for t in (26, 44, 46, 48)])
        return FacilityState.from_facility_config(cfg, today=_d(2026, 6, 1))

    def test_explicit_grade_routes_big_and_small_to_tagged_tanks(self):
        """size_class="big"/"small" tags route each entry grade ONLY to its own
        tanks (heavier fish to the big tanks, lighter to the small), still
        conserving count. The UI's two-picker flow relies on this."""
        from datetime import date as _d
        from forecast.manual_events import (
            ManualEvent, ManualDest, _apply_fw_to_og)
        s = self._state_four_empty_og()
        fw_count = 300000.0
        ev = ManualEvent(type="fw_to_og", week=1, batch="B49", count=250000,
                         destinations=[
                             ManualDest(tank=26, size_class="big"),
                             ManualDest(tank=44, size_class="big"),
                             ManualDest(tank=46, size_class="small"),
                             ManualDest(tank=48, size_class="small")])
        tranogs = []
        warns, culled = _apply_fw_to_og(
            s, ev, 1, fw_count, 370.0, 20.0, 0.0001,
            event_date=_d(2026, 6, 1), out_tranog=tranogs)
        placed = sum(d.count for e in tranogs for d in e.destinations)
        assert math.isclose(placed + culled, fw_count, abs_tol=1e-6)
        assert math.isclose(placed, 250000, abs_tol=1e-6)
        # Heavier fish in the big-tagged tanks than the small-tagged tanks.
        big_wt = min(s.tanks_by_id[26].avg_wt_g, s.tanks_by_id[44].avg_wt_g)
        small_wt = max(s.tanks_by_id[46].avg_wt_g, s.tanks_by_id[48].avg_wt_g)
        assert big_wt > small_wt
        # Each grade's count split evenly across its own tanks.
        assert math.isclose(s.tanks_by_id[26].count, s.tanks_by_id[44].count, abs_tol=1e-6)
        assert math.isclose(s.tanks_by_id[46].count, s.tanks_by_id[48].count, abs_tol=1e-6)

    def test_grade_with_no_assigned_tank_is_rejected(self):
        """If a grade that has fish gets no tank, the transfer is rejected (no
        fish placed) rather than silently dropped."""
        from datetime import date as _d
        from forecast.manual_events import (
            ManualEvent, ManualDest, _apply_fw_to_og)
        s = self._state_four_empty_og()
        ev = ManualEvent(type="fw_to_og", week=1, batch="B49", count=250000,
                         destinations=[ManualDest(tank=26, size_class="big")])
        tranogs = []
        warns, culled = _apply_fw_to_og(
            s, ev, 1, 300000.0, 370.0, 20.0, 0.0001,
            event_date=_d(2026, 6, 1), out_tranog=tranogs)
        assert any("smaller grade" in w and "no tank" in w for w in warns)
        assert not tranogs                       # nothing placed
        assert s.tanks_by_id[26].is_empty        # source tank untouched


class TestManualFwBalanceAudit:
    """The InputConservation FW mass-balance gate must reconcile a manual
    fw_to_og batch from the window-captured (fw_count, culled) — it has no FW
    biology states, so without this it was silently SKIPPED. It must also be
    labeled 'manual fw_to_og' (its target gap is intentional), NOT mislabeled as
    an 'FW UNDER/OVER plan' survival-calibration miss."""

    def _run_audit(self, manual_fw_balance):
        from datetime import datetime as _dt
        from types import SimpleNamespace
        import openpyxl
        from forecast.events import TranOGEntry, TankAllocation
        from forecast.models import BatchInput
        from forecast.excel_io import write_input_conservation_audit
        wb = openpyxl.Workbook()
        bt = BatchInput(
            batch_id="B49", input_date=_dt(2025, 12, 1), input_count=550000,
            tran_sf_date=None, tran_og_date=_dt(2026, 8, 27),
            tran_og_count=290000, tran_og_avg_wt_g=370.0, tran_og_cv=16.0,
            fcr_model="FCR_121_Quick", fw_correction=1.0, sgr_correction=1.0)
        # B49 placed into seawater at the operator's target (250k), below the
        # 290k plan — a -14% gap that is intentional, not a calibration miss.
        blr = SimpleNamespace(batch_id="B49", week_label="2026-W23", count=250000)
        tog = TranOGEntry(
            batch_id="B49", event_date=_dt(2026, 6, 1),
            destinations=[TankAllocation(tank_id=26, count=125000,
                                         avg_wt_g=455.0, cv_pct=16.0),
                          TankAllocation(tank_id=44, count=125000,
                                         avg_wt_g=330.0, cv_pct=16.0)])
        control = SimpleNamespace(forecast_start=_dt(2026, 6, 1), horizon_weeks=130)
        write_input_conservation_audit(
            wb, [bt], [blr], [], control, tranog_events=[tog],
            biology_states_by_batch=None, manual_fw_balance=manual_fw_balance)
        ws = wb["InputConservationAudit"]
        rows = list(ws.iter_rows(values_only=True))
        hdr = next(r for r in rows if r and r[0] == "Batch")
        b49 = next(r for r in rows if r and r[0] == "B49")
        summary = " ".join(str(c) for r in rows[:8] for c in r if c is not None)
        return dict(zip(hdr, b49)), summary

    def test_manual_cull_reconciled_and_relabeled(self):
        # fw_count(342,783) == placed(250,000) + culled(92,783) -> residual 0.
        row, summary = self._run_audit({"B49": [342783.0, 92783.0]})
        assert row["FW_Flag"] == "manual fw_to_og"
        assert row["FW_Cull (fish)"] == 92783
        assert row["FW_Bal_Residual (fish)"] == 0
        # Intentional target gap must NOT be reported as a calibration divergence.
        assert "B49" not in summary.split("calibration gap")[-1] \
            if "calibration gap" in summary else True
        assert "FW MASS-BALANCE BREACH" not in summary

    def test_wrong_manual_cull_breaches_the_gate(self):
        # If the cull were under-counted (fish leaked), the residual goes large
        # and the gate must FLAG it — the safety net the fix restores.
        row, summary = self._run_audit({"B49": [342783.0, 10000.0]})
        # residual = 342783 - 250000 - 10000 = 82,783 (>2% of base) -> breach.
        assert row["FW_Bal_Residual (fish)"] == 82783
        assert "FW MASS-BALANCE BREACH" in summary
