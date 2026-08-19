"""Per-week OG SGR factor — the operator's "we only get 90% this week" input.

A facility row in `scenario/limits.yaml` (metric `sgr_correction_og`) that
LAYERS on top of the growth curve and each batch's own `sgr_correction`. It is
seawater-only: freshwater has `fw_correction`, and this is an OG-tank input.

Two properties carry the whole feature and both are pinned here:

  * it MULTIPLIES, it does not replace — a batch that is already calibrated to
    0.8 and a week set to 0.9 grows at 0.72 of curve, not 0.9;
  * FEED moves with it. Feed is biomass x SGR/100 x FCR, so applying the factor
    at `sgr_pct_per_day` — the single source for the growth rate — makes a 90%
    week eat 90% and grow 90%, leaving FCR unchanged (operator decision
    2026-08-19). Applying it anywhere else would silently change FCR instead.

The threading matters as much as the maths: the factor rides on BiologyTables
because `tables` already reaches every growth and feed call site, and a single
missed site would let the PROJECTION the scheduler plans against disagree with
the REALIZED walk. `test_projection_and_realized_agree` is the guard for that.
"""
from __future__ import annotations

from datetime import date

from forecast.biology import og_sgr_factor, realized_feed_kg_day, sgr_pct_per_day
from forecast.caps import FacilityLimits, METRIC_SGR_OG, og_sgr_factors
from forecast.models import BatchInput, BiologyTables

_WK = "2026-W40"


def _tables(**kw):
    t = BiologyTables(sgr_size_g=[100.0, 6000.0],
                      sgr_sw_pct_day=[1.0, 1.0], sgr_fw_pct_day=[2.0, 2.0],
                      fcr_size_g=[100.0, 6000.0],
                      fcr_by_model={"1.21": [1.2, 1.2]})
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def _batch(sgr_correction=1.0):
    return BatchInput(
        batch_id="B1", input_date=date(2026, 1, 1), input_count=1000,
        tran_sf_date=None, tran_og_date=None, tran_og_count=None,
        tran_og_avg_wt_g=None, tran_og_cv=16.0, fcr_model="1.21",
        fw_correction=1.0, sgr_correction=sgr_correction)


class TestItLayersRatherThanReplaces:
    def test_the_factor_multiplies_the_batch_correction(self):
        t = _tables(og_sgr_by_week={_WK: 0.9})
        got = sgr_pct_per_day(1000.0, "SW", _batch(0.8), t, _WK)
        assert abs(got - (1.0 * 0.8 * 0.9)) < 1e-9, got

    def test_an_unset_week_changes_nothing(self):
        t = _tables(og_sgr_by_week={_WK: 0.9})
        assert sgr_pct_per_day(1000.0, "SW", _batch(0.8), t, "2026-W41") == 0.8

    def test_no_config_at_all_is_the_old_behaviour(self):
        t = _tables()
        assert sgr_pct_per_day(1000.0, "SW", _batch(0.8), t, _WK) == 0.8

    def test_no_week_in_context_is_the_old_behaviour(self):
        """A caller with genuinely no week must not inherit someone else's."""
        t = _tables(og_sgr_by_week={_WK: 0.9})
        assert sgr_pct_per_day(1000.0, "SW", _batch(0.8), t) == 0.8

    def test_freshwater_is_untouched(self):
        """It is an OG-tank input; FW has fw_correction."""
        t = _tables(og_sgr_by_week={_WK: 0.5})
        assert sgr_pct_per_day(1000.0, "FW", _batch(), t, _WK) == 2.0


class TestFeedMovesWithIt:
    def test_a_90_percent_week_feeds_90_percent(self):
        base = _tables()
        cut = _tables(og_sgr_by_week={_WK: 0.9})
        b = _batch()
        f0 = realized_feed_kg_day(1000.0, 50_000.0, b, base, _WK)
        f1 = realized_feed_kg_day(1000.0, 50_000.0, b, cut, _WK)
        assert f0 > 0
        assert abs(f1 / f0 - 0.9) < 1e-9, (f0, f1)

    def test_fcr_is_unchanged_because_both_move_together(self):
        """growth and feed scale by the same factor, so feed/growth holds."""
        cut = _tables(og_sgr_by_week={_WK: 0.9})
        b = _batch()
        sgr = sgr_pct_per_day(1000.0, "SW", b, cut, _WK)
        feed = realized_feed_kg_day(1000.0, 50_000.0, b, cut, _WK)
        growth = 50_000.0 * sgr / 100.0
        assert abs(feed / growth - 1.2) < 1e-9


class TestTheOperatorInput:
    def test_it_reads_the_facility_rows(self):
        fl = FacilityLimits(overrides={
            ("2026-W40", METRIC_SGR_OG): 0.9,
            ("2026-W41", METRIC_SGR_OG): 0.8,
            ("2026-W40", "biomass"): 3_800_000.0,
        })
        assert og_sgr_factors(fl) == {"2026-W40": 0.9, "2026-W41": 0.8}

    def test_zero_is_a_real_answer_not_absence(self):
        """A cap treats 0 as unset; 'no growth this week' is legitimate here."""
        fl = FacilityLimits(overrides={("2026-W40", METRIC_SGR_OG): 0.0})
        assert og_sgr_factors(fl) == {"2026-W40": 0.0}
        t = _tables(og_sgr_by_week={"2026-W40": 0.0})
        assert sgr_pct_per_day(1000.0, "SW", _batch(), t, "2026-W40") == 0.0

    def test_a_negative_factor_is_dropped_not_applied(self):
        """Silently treating it as a shrink factor would invent a model."""
        fl = FacilityLimits(overrides={("2026-W40", METRIC_SGR_OG): -0.5})
        assert og_sgr_factors(fl) == {}

    def test_helper_defaults_to_one(self):
        assert og_sgr_factor(_tables(), None) == 1.0
        assert og_sgr_factor(_tables(), "2026-W40") == 1.0
        assert og_sgr_factor(_tables(og_sgr_by_week={_WK: 0.9}), "2026-W99") == 1.0


class TestProjectionAndRealizedAgree:
    def test_both_paths_read_the_same_factor(self):
        """The scheduler plans on the PROJECTION; the plan is executed on the
        REALIZED walk. If a call site were missed, one would grow at full rate
        while the other was reduced, and the plan would be built on numbers the
        run never produces. Both go through sgr_pct_per_day, so the guard is
        that neither can reach it without a week."""
        from forecast.biology import advance_tank_one_day, project_in_flight_batch
        import inspect
        for fn in (advance_tank_one_day, project_in_flight_batch):
            src = inspect.getsource(fn)
            if "sgr_pct_per_day(" in src:
                assert "iso_week_label(" in src or "week_label" in src, (
                    f"{fn.__name__} calls sgr_pct_per_day without a week — the "
                    f"projection and the realized walk would disagree")
