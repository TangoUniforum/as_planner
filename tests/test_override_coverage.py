"""Per-week override COVERAGE: name the weeks that fall back, change nothing.

DEFECT (2026-09-03): `scenario/limits.yaml` carried biomass and feed_per_day
rows for 2026-W37..W53 and nothing after. Everything from 2027-W01 fell through
to the Control defaults, which are the DESIGN / post-expansion figures (34,000
kg/day and 3,800,000 kg) rather than the 27,500 / 3,650,000 derate the operator
had been entering week by week. The plan therefore assumed the expansion
capacity arrived on 2027-W01 -- a date nobody chose, worth ~131 t of horizon
production and ~228 t across Jan+Feb 2027. Nothing anywhere said a word.

The fix is DETECTION, not coercion: resolution still falls back exactly as
before, because an absent row genuinely does mean "use the default". What
changed is that a metric the operator IS steering per week, whose rows STOP
before the horizon ends, now says so in the ValidationLog.

These tests pin the three things that make it useful rather than noisy:
  1. it fires on the real shape (rows that stop mid-horizon);
  2. it stays SILENT when a metric has no rows at all, because there the
     default is the deliberate answer and a warning would be noise that gets
     the check switched off;
  3. it changes no resolved cap -- detection must not steer.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forecast import caps  # noqa: E402
from forecast.caps import (  # noqa: E402
    METRIC_BIOMASS, METRIC_FEED_DAY, METRIC_MIN_HARVEST, METRIC_SGR_OG,
    FacilityLimits, resolve_facility_cap,
)


WEEKS = ["2026-W%02d" % w for w in range(36, 54)] + \
        ["2027-W%02d" % w for w in range(1, 14)]


@pytest.fixture
def control():
    from forecast.config_io import load_control
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return load_control(os.path.join(root, "config"))


def _limits(pairs):
    return FacilityLimits(overrides=dict(pairs))


def test_rows_that_stop_mid_horizon_are_named(control):
    """The exact defect: 18 weeks covered, 13 silently on the default."""
    fl = _limits({(w, METRIC_FEED_DAY): 27500.0
                  for w in WEEKS if w.startswith("2026")})
    gaps = caps.override_coverage_gaps(fl, control, WEEKS)
    assert len(gaps) == 1, gaps
    g = gaps[0]
    assert g["metric"] == METRIC_FEED_DAY
    assert g["last_covered"] == "2026-W53"
    assert g["n_after"] == 13 and g["n_before"] == 0 and g["n_interior"] == 0
    assert g["default"] == control.max_feed_per_day_kg

    note = caps.coverage_gap_notes(fl, control, WEEKS)[0]
    assert METRIC_FEED_DAY in note
    assert "2026-W53" in note, "the note must name where the rows stop"
    assert str(int(control.max_feed_per_day_kg)) in note.replace(",", ""), \
        "the note must name the default the weeks will silently take"


def test_a_metric_with_no_rows_at_all_stays_silent(control):
    """Silence is the point: with no rows the default IS the operator's answer,
    and a warning there is noise that gets the whole check ignored."""
    fl = _limits({(w, METRIC_FEED_DAY): 27500.0 for w in WEEKS})
    assert caps.override_coverage_gaps(fl, control, WEEKS) == []
    assert caps.coverage_gap_notes(fl, control, WEEKS) == []
    assert caps.coverage_gap_notes(_limits({}), control, WEEKS) == []


def test_full_coverage_is_silent(control):
    """Every horizon week covered -> nothing to say."""
    fl = _limits({(w, m): 1.0 for w in WEEKS
                  for m in (METRIC_BIOMASS, METRIC_FEED_DAY)})
    assert caps.coverage_gap_notes(fl, control, WEEKS) == []


def test_a_leading_gap_is_reported_differently_from_a_trailing_one(control):
    """Weeks BEFORE the first row usually mean the rows start mid-horizon on
    purpose; weeks AFTER the last row mean entry stopped. Same count, different
    meaning, so the note must distinguish them."""
    fl = _limits({(w, METRIC_MIN_HARVEST): 50000.0 for w in WEEKS[2:]})
    g = caps.override_coverage_gaps(fl, control, WEEKS)[0]
    assert g["n_before"] == 2 and g["n_after"] == 0
    assert "before" in caps.coverage_gap_notes(fl, control, WEEKS)[0]


def test_the_og_growth_factor_is_not_a_facility_cap(control):
    """sgr_correction_og has no Control default to fall back to, so it is not
    a coverage question and must not be reported."""
    fl = _limits({(WEEKS[0], METRIC_SGR_OG): 0.95})
    assert caps.override_coverage_gaps(fl, control, WEEKS) == []


def test_detection_does_not_steer(control):
    """The load-bearing guarantee. Resolution must be byte-identical whether or
    not the coverage check has run -- it reports, it never substitutes."""
    fl = _limits({("2026-W40", METRIC_FEED_DAY): 27500.0})
    before = [resolve_facility_cap(METRIC_FEED_DAY, w, fl, control) for w in WEEKS]
    caps.override_coverage_gaps(fl, control, WEEKS)
    caps.coverage_gap_notes(fl, control, WEEKS)
    after = [resolve_facility_cap(METRIC_FEED_DAY, w, fl, control) for w in WEEKS]
    assert before == after
    # and the fallback itself is unchanged: covered week keeps its row, every
    # other week still gets the Control default.
    assert before[WEEKS.index("2026-W40")] == 27500.0
    assert before[0] == control.max_feed_per_day_kg


def test_an_empty_horizon_is_not_an_error(control):
    """Called before the week grid exists, it must return nothing rather than
    raise -- a diagnostic that can break a run is worse than no diagnostic."""
    fl = _limits({("2026-W40", METRIC_FEED_DAY): 27500.0})
    assert caps.override_coverage_gaps(fl, control, []) == []
    assert caps.override_coverage_gaps(fl, control, None) == []


def test_the_note_lands_in_its_own_validationlog_category():
    """DEFECT (found by execution, same day it shipped): the note text matched
    no branch of write_validation_log's category chain, so it fell into the
    catch-all `WARNING - Hydration` -- telling an operator scanning the log
    that their ProductionReport had read badly, when in fact a facility metric
    had simply run out of per-week rows. Exactly the mis-filing the
    FW-calibration branch above it was written to correct."""
    import openpyxl

    from forecast.excel_io import write_validation_log

    note = ("PER-WEEK COVERAGE - biomass: rows cover 2026-W36..2026-W53 "
            "(18 of 85 horizon week(s)); 67 after 2026-W53 take the Control "
            "default 3,800,000.")
    wb = openpyxl.Workbook()
    write_validation_log(wb, invariant_warnings=[note])
    cats = [row[1] for row in wb["ValidationLog"].iter_rows(values_only=True)
            if row and len(row) > 2 and row[2] and "PER-WEEK COVERAGE" in str(row[2])]
    assert cats, "the coverage note never reached the ValidationLog"
    assert "Per-week coverage" in str(cats[0]), (
        "coverage note filed as %r -- it must have its own category, not the "
        "hydration catch-all, which misreads as 'your PR is bad'" % cats[0])


def test_a_capacity_is_not_rendered_in_scientific_notation(control):
    """DEFECT: `%g` rendered 3800000.0 as `3.8e+06` in a log line about
    kilograms of fish. An operator cannot check a number they cannot read.

    Asserted on the RENDERED NOTE, not on the helper: a first draft of this
    test called `_fmt_cap` directly, and a negative control showed it still
    passed with the call site reverted to `%g`. A guard that does not fail on
    the defect it names is not a guard."""
    from forecast.caps import _fmt_cap
    assert _fmt_cap(3800000.0) == "3,800,000"
    assert _fmt_cap(34000.0) == "34,000"
    assert _fmt_cap(0.87) == "0.87"      # hog_yield stays readable too

    fl = _limits({(w, METRIC_BIOMASS): 3650000.0
                  for w in WEEKS if w.startswith("2026")})
    note = caps.coverage_gap_notes(fl, control, WEEKS)[0]
    assert "e+0" not in note, "capacity rendered in scientific notation: %s" % note
    assert format(control.max_biomass_kg, ",.0f") in note, (
        "the note must name the default in a form an operator can read: %s"
        % note)
