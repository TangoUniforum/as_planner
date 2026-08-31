"""system_overshoot must see HOW FAR over a system runs, not just how often.

The count-based form counted a system 2% over exactly the same as one 32% over,
so the score was blind to severity precisely where severity is the question:
the per-system feed gate reports `worst 1.318x`, and the score could not see
that number at all. A plan squeaking past on many systems could then rank worse
than one badly over on a few.
"""
import openpyxl
import pytest

from forecast import optimize


def _wb(cells):
    """A minimal SystemLimitsAudit. `cells` = (system, biomass, bio_cap,
    feed, feed_cap) rows, all on distinct weeks so none is deduplicated."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "SystemLimitsAudit"
    ws.append(["SYSTEM LIMITS AUDIT"])
    ws.append(["Week", "System", "Biomass_kg", "Biomass_cap",
               "Bio_flag", "Feed_kg_day", "Feed_cap", "Feed_flag"])
    for i, (sysid, b, bc, f, fc) in enumerate(cells, start=1):
        ws.append([f"2026-W{i:02d}", sysid, b, bc, "", f, fc, ""])
    return wb


def test_severity_changes_the_value():
    mild = optimize._system_overshoot(_wb([("OG1N", 100, 100, 102, 100)]))
    bad = optimize._system_overshoot(_wb([("OG1N", 100, 100, 132, 100)]))
    assert mild == pytest.approx(0.02, abs=1e-9)
    assert bad == pytest.approx(0.32, abs=1e-9)
    assert bad > mild * 10, "a 32% breach must outweigh a 2% one"


def test_equal_counts_can_differ_by_severity():
    """The exact case the count-based form could not distinguish: same number
    of cells over cap, very different plans."""
    a = optimize._system_overshoot(_wb([("OG1N", 100, 100, 102, 100),
                                        ("OG2N", 100, 100, 103, 100)]))
    b = optimize._system_overshoot(_wb([("OG1N", 100, 100, 130, 100),
                                        ("OG2N", 100, 100, 132, 100)]))
    # mean(0.02, 0.03) = 0.025 vs mean(0.30, 0.32) = 0.31 -> 12.4x
    assert a < b
    assert b / a > 10


def test_compliant_cells_contribute_nothing():
    assert optimize._system_overshoot(
        _wb([("OG1N", 50, 100, 50, 100), ("OG2N", 99, 100, 99, 100)])) == 0.0


def test_a_cell_over_on_both_counts_its_WORSE_dimension_not_the_sum():
    """The binding constraint is the one you hit first; summing would
    double-count a single overloaded system."""
    both = optimize._system_overshoot(_wb([("OG1N", 110, 100, 130, 100)]))
    assert both == pytest.approx(0.30, abs=1e-9)   # not 0.40


def test_the_mean_is_over_ALL_cells_so_a_breach_is_diluted_by_compliance():
    """One system 32% over in a facility of four compliant ones is a smaller
    problem than the same breach in a facility of one."""
    alone = optimize._system_overshoot(_wb([("OG1N", 100, 100, 132, 100)]))
    among = optimize._system_overshoot(_wb([("OG1N", 100, 100, 132, 100),
                                            ("OG2N", 50, 100, 50, 100),
                                            ("OG3N", 50, 100, 50, 100),
                                            ("OG4N", 50, 100, 50, 100)]))
    assert among == pytest.approx(alone / 4, abs=1e-9)


def test_missing_sheet_scores_zero_not_a_crash():
    wb = openpyxl.Workbook()
    wb.active.title = "Something Else"
    assert optimize._system_overshoot(wb) == 0.0
