"""Targets GRADE, limits STEER — and the UI must never blur the two.

config/targets.yaml is read by the grading layer and by NO planner module, so a
monthly target moves no fish. The per-week min/max harvest band in
scenario/limits.yaml resolves through caps.resolve_facility_cap into the
controller, so it does. An operator who sets a target, sees nothing move, and
concludes the tool is broken has been misled by the interface, not the engine.
"""
import pytest

from forecast import harvest_plan as hp


WEEKS = ["2026-W40", "2026-W41", "2026-W42", "2026-W43", "2026-W44"]


def test_a_week_is_attributed_the_same_way_the_targets_gate_does_it():
    """If this view and the gate disagree about which month a week belongs to,
    the gap lands in one month and the lever in another."""
    from forecast.analysis import week_to_month
    for w in WEEKS:
        assert hp.month_of(w) == week_to_month(w)


def test_the_gap_is_actual_minus_target():
    rows = hp.build_rows({"2026-11": 376_000.0}, {"monthly": {"2026-11": 600_000.0}},
                         [], {})
    r = next(x for x in rows if x.month == "2026-11")
    assert r.gap_kg == pytest.approx(-224_000.0)
    assert "short" in r.status


def test_no_target_reads_as_no_target_not_as_a_miss():
    rows = hp.build_rows({"2026-11": 376_000.0}, None, [], {})
    assert rows[0].target_kg is None
    assert rows[0].gap_kg is None
    assert rows[0].status == "no target"


def test_a_month_with_a_mixed_band_reports_no_single_number():
    """Showing one number for a month whose weeks disagree invites an edit that
    silently flattens the difference."""
    # W41..W44 all fall in 2026-10, so this is one month with disagreeing weeks
    ov = {("2026-W41", hp.METRIC_MAX): 40_000.0,
          ("2026-W42", hp.METRIC_MAX): 55_000.0}
    rows = hp.build_rows({}, None, ["2026-W41", "2026-W42"], ov)
    assert rows[0].month == "2026-10"
    assert rows[0].max_override is None


def test_a_uniform_band_is_shown():
    ov = {(w, hp.METRIC_MAX): 44_000.0 for w in ("2026-W41", "2026-W42")}
    rows = hp.build_rows({}, None, ["2026-W41", "2026-W42"], ov)
    assert rows[0].month == "2026-10"
    assert rows[0].max_override == 44_000.0


def test_a_partially_covered_month_is_not_missing_a_band():
    """Half the weeks capped is not a month-level band."""
    ov = {("2026-W41", hp.METRIC_MAX): 44_000.0}
    rows = hp.build_rows({}, None, ["2026-W41", "2026-W42"], ov)
    assert rows[0].month == "2026-10"
    assert rows[0].max_override is None


def test_editing_one_month_never_touches_another_weeks_override():
    """THE SAFETY PROPERTY. Hand-tuned bands elsewhere must survive an edit
    made from this screen."""
    existing = {("2027-W05", hp.METRIC_MAX): 33_000.0,       # tuned elsewhere
                ("2026-W41", hp.METRIC_MAX): 55_000.0}
    new, log = hp.merge_overrides(
        existing, [("2026-10", ("2026-W41", "2026-W42"), None, 44_000.0)])
    assert new[("2027-W05", hp.METRIC_MAX)] == 33_000.0, "clobbered another month"
    assert new[("2026-W41", hp.METRIC_MAX)] == 44_000.0
    assert new[("2026-W42", hp.METRIC_MAX)] == 44_000.0
    assert log


def test_clearing_a_band_removes_it_rather_than_writing_zero():
    """0 fish/week is a real and catastrophic instruction, not 'unset'."""
    existing = {(w, hp.METRIC_MIN): 30_000.0 for w in ("2026-W41", "2026-W42")}
    new, _ = hp.merge_overrides(
        existing, [("2026-10", ("2026-W41", "2026-W42"), None, None)])
    assert new == {}
    assert not any(v == 0 for v in new.values())


def test_merge_is_pure_the_caller_still_holds_the_original():
    existing = {("2026-W41", hp.METRIC_MAX): 55_000.0}
    new, _ = hp.merge_overrides(
        existing, [("2026-10", ("2026-W41",), None, 44_000.0)])
    assert existing[("2026-W41", hp.METRIC_MAX)] == 55_000.0
    assert new[("2026-W41", hp.METRIC_MAX)] == 44_000.0


def test_partial_months_are_flagged_so_a_short_horizon_is_not_read_as_a_trough():
    rows = hp.build_rows({}, None, ["2026-W40"] + WEEKS[1:], {})
    rows.append(hp.MonthRow("2027-10", 217_000.0, None, ("2027-W40",), None, None))
    assert "2027-10" in hp.partial_months(rows)


def test_the_suggestion_is_arithmetic_not_a_promise():
    r = hp.MonthRow("2026-12", 376_000.0, 600_000.0,
                    tuple(f"2026-W{n}" for n in range(49, 53)), None, None)
    s = hp.suggest_band(r, 3.2)
    assert s and "short" in s
    # it must say where fish come from, not merely 'raise the floor'
    assert "somewhere" in s or "spare" in s
    assert "will work" not in s


def test_no_suggestion_for_a_trivial_gap():
    r = hp.MonthRow("2026-12", 600_100.0, 600_000.0, ("2026-W49",), None, None)
    assert hp.suggest_band(r, 3.2) is None


def test_no_suggestion_without_a_target():
    r = hp.MonthRow("2026-12", 376_000.0, None, ("2026-W49",), None, None)
    assert hp.suggest_band(r, 3.2) is None
