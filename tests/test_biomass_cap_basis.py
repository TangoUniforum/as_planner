"""The biomass gate must judge each week against the cap THAT WEEK had.

The facility cap is not a constant: on the live scenario it defaults to 3.80M
and the operator drops it to 3.65M from 2026-W37, which `scenario/limits.yaml`
expresses as one `facility` row per week and metric. Dividing the horizon PEAK
by a single cap asks the wrong question — the biggest week and the tightest week
need not be the same week.

Measured 2026-09-02 on the 2026-07-31 run: peak 4.222M in 2026-W36 against the
then-current 3.80M reads 111.1%, while the worst RATIO is 114.0% in a later week
measured against 3.65M. Same run, three points apart.

The verdict-changing case is the one below: a plan peaking at 3.75M after W37 is
over the 3.65M cap it must obey, and the flat comparison calls it a PASS.
"""
from forecast.analysis import _gate_biomass_cap


def _conv(worst_pct):
    """Minimal convergence_review shape — only the field the gate reads."""
    return {"worst_pct": worst_pct}


def test_per_week_series_is_preferred_over_the_flat_peak():
    st, txt = _gate_biomass_cap(
        {"peak_pct_of_cap": 111.1, "convergence": _conv(114.0)})
    assert st == "FAIL"
    assert "114.0%" in txt and "111.1" not in txt
    assert "week's cap" in txt          # the basis is named, never implied


def test_flat_peak_can_hide_a_breach_the_per_week_cap_catches():
    """3.75M after 2026-W37 is 102.7% of the 3.65M cap and 98.7% of 3.80M.

    Flat says PASS. The week's own cap says WARN. This is the inversion the
    change exists to prevent.
    """
    flat = _gate_biomass_cap({"peak_pct_of_cap": 98.7})
    assert flat[0] == "PASS"

    real = _gate_biomass_cap(
        {"peak_pct_of_cap": 98.7, "convergence": _conv(102.7)})
    assert real[0] == "WARN"


def test_falls_back_to_the_flat_peak_and_says_so():
    st, txt = _gate_biomass_cap({"peak_pct_of_cap": 105.0})
    assert st == "WARN"
    assert "flat" in txt                # never passed off as the per-week number


def test_no_series_and_no_peak_is_na_not_a_pass():
    st, _txt = _gate_biomass_cap({})
    assert st == "N/A"


def test_thresholds_are_unchanged():
    for pct, want in ((99.9, "PASS"), (100.0, "PASS"), (100.1, "WARN"),
                      (110.0, "WARN"), (110.1, "FAIL")):
        assert _gate_biomass_cap({"convergence": _conv(pct)})[0] == want, pct


def test_an_empty_convergence_dict_does_not_shadow_the_peak():
    """A review that ran but produced no worst_pct must not blank the gate."""
    st, txt = _gate_biomass_cap({"peak_pct_of_cap": 120.0, "convergence": {}})
    assert st == "FAIL" and "120.0%" in txt
