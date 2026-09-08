"""A derived RATE is only meaningful when the thing it divides is real.

THE OPERATOR'S CATCH (2026-09-08). Reading the shipped Sep'26 workbook: an
FCR of 0.29 on B50 and 1.04 for October could not be right, and neither could
B43 showing more growth than feed. Two separate defects came out of it. The
first was a real bug (arrivals credited at 0 g — see test_coordinator_
regression). The second, this one, is not arithmetic but MEANING.

B43, 2026-W41: SGR 0.2973 %/day in a week when literally nothing grew. Its two
tanks were both off feed in depuration; the harvest took 53,539 fish from the
lighter tank (4.203 kg) and left 7,129 in the heavier one (4.304 kg). Tank 61
read 4.304 kg in BOTH weeks — flat. The batch mean rose purely because the
light half left.

The operator put the principle better than the code did: "if you take out the
top half into one tank but still have the small group in another tank then the
batch average should remain the same." Exactly — a SPLIT moves no mass. The
corollary is that a REMOVAL does move the mean, and SGR cannot tell the two
apart because it only sees the mean.

TWO RULES, for two different failures:

  SGR is log(close_wt/open_wt) — a PER-FISH rate, valid only while you follow
  the SAME fish. Harvest, culls and arrivals change which fish are in the
  average. 42 batch-weeks reported an SGR while >25% of the batch was
  harvested; 196 of 1,266 rows have any population change at all.

  The FCRs divide feed by MASS-BALANCE growth, which already handles harvest
  correctly — so a big harvest does NOT invalidate them. They go empty only
  when there is nothing being converted: B43's October is 304 kg of feed
  against a 255-tonne batch, 0.008 %/day.

Blank, never 0 — this file's own Avg_Density legend sets that convention: an
empty cell says "not applicable", a 0 asserts a result.
"""
from __future__ import annotations

from forecast.excel_io import (_FCR_MIN_SFR_PCT_DAY, _SGR_POP_CHANGE_TOL,
                               _rate_is_meaningful)


def _row(**kw):
    d = {"open_count": 100000.0, "harv_count": 0.0, "cull_count": 0.0,
         "input_count": 0.0, "sfr": 1.0}
    d.update(kw)
    return d


def test_an_ordinary_feeding_week_keeps_both():
    sgr_ok, fcr_ok = _rate_is_meaningful(_row())
    assert sgr_ok and fcr_ok


def test_the_b43_week_loses_its_sgr():
    """53,539 of 60,671 harvested — the mean moved because fish LEFT."""
    sgr_ok, _ = _rate_is_meaningful(
        _row(open_count=60671.0, harv_count=53539.0, sfr=0.0))
    assert not sgr_ok


def test_the_b43_month_loses_its_fcr_because_it_was_off_feed():
    """304 kg of feed against a 255-tonne batch is not feed conversion."""
    _, fcr_ok = _rate_is_meaningful(_row(sfr=0.008))
    assert not fcr_ok


def test_a_big_harvest_does_NOT_kill_the_fcr():
    """The FCR denominator is the mass balance, which handles harvest."""
    sgr_ok, fcr_ok = _rate_is_meaningful(
        _row(harv_count=40000.0, sfr=0.9))
    assert not sgr_ok, "SGR must go"
    assert fcr_ok, "FCR is still meaningful — its denominator is balance-derived"


def test_an_arrival_week_has_no_sgr():
    """Nothing to compare against: the batch had no opening position."""
    sgr_ok, _ = _rate_is_meaningful(_row(open_count=0.0, input_count=253240.0))
    assert not sgr_ok


def test_arrivals_change_the_mean_just_as_harvest_does():
    sgr_ok, _ = _rate_is_meaningful(_row(input_count=50000.0))
    assert not sgr_ok


def test_culls_count_as_a_population_change():
    sgr_ok, _ = _rate_is_meaningful(_row(cull_count=9000.0))
    assert not sgr_ok


def test_mortality_alone_does_not_suppress_the_sgr():
    """Mortality is roughly size-neutral and is NOT in the population test —
    otherwise every week in the horizon would blank."""
    sgr_ok, fcr_ok = _rate_is_meaningful(_row(mort_count=500.0))
    assert sgr_ok and fcr_ok


def test_the_thresholds_are_the_documented_ones():
    at = _rate_is_meaningful(
        _row(harv_count=100000.0 * _SGR_POP_CHANGE_TOL, sfr=_FCR_MIN_SFR_PCT_DAY))
    assert at == (True, True), "exactly at the tolerance is still meaningful"
    over = _rate_is_meaningful(
        _row(harv_count=100000.0 * _SGR_POP_CHANGE_TOL + 1.0,
             sfr=_FCR_MIN_SFR_PCT_DAY - 1e-6))
    assert over == (False, False)


def test_a_missing_field_does_not_crash_the_ledger():
    """Aggregates are built from several sources; absent keys must default."""
    assert _rate_is_meaningful({"open_count": 100.0}) == (True, False)
    assert _rate_is_meaningful({}) == (False, False)
