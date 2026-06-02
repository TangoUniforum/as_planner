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
            TankConfig(location_id="OG3N-1", department="OG", stage="SW",
                       system_id="OG3N", tank_id=1, volume_m3=1720,
                       max_density_kg_m3=95, max_feed_kg_day=500, type="OG"),
            TankConfig(location_id="OG6N-61", department="OG", stage="SW",
                       system_id="OG6N", tank_id=2, volume_m3=1720,
                       max_density_kg_m3=95, max_feed_kg_day=500, type="OG"),
            TankConfig(location_id="OG3S-1", department="OG", stage="SW",
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
