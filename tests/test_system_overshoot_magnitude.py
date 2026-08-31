"""system_overshoot must see HOW FAR over a system runs -- and its WORST cell.

Two defects, fixed in that order:

v4 -- the count-based form counted a system 2% over exactly the same as one 32%
over, so the score was blind to severity precisely where severity is the
question. Magnitude weighting fixed that.

v6 -- the MEAN alone is not monotone with the `system_feed` gate, and the two
disagreed on a real board. On the 8.23.26 PR the tuned winner cut TOTAL
over-cap feed 22% while spreading it across 5 more system-weeks and pushing the
worst system 1.318x -> 1.331x. The gate called that worse; the score called it
better, contributing -0.3962 of a -0.3085 winning margin -- so this one term
decided the tournament AGAINST the gate a human reads. The term now carries the
peak, so a plan cannot buy a lower total by driving one system further past a
physical delivery ceiling.
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
    # single cell -> mean == peak == its own excess, so the term is 2x it
    assert mild == pytest.approx(0.04, abs=1e-9)
    assert bad == pytest.approx(0.64, abs=1e-9)
    assert bad > mild * 10, "a 32% breach must outweigh a 2% one"


def test_equal_counts_can_differ_by_severity():
    """The exact case the count-based form could not distinguish: same number
    of cells over cap, very different plans."""
    a = optimize._system_overshoot(_wb([("OG1N", 100, 100, 102, 100),
                                        ("OG2N", 100, 100, 103, 100)]))
    b = optimize._system_overshoot(_wb([("OG1N", 100, 100, 130, 100),
                                        ("OG2N", 100, 100, 132, 100)]))
    # a: mean .025 + peak .03 = .055;  b: mean .31 + peak .32 = .63
    assert a == pytest.approx(0.055, abs=1e-9)
    assert b == pytest.approx(0.63, abs=1e-9)
    assert b / a > 10


def test_a_lower_TOTAL_cannot_buy_a_worse_PEAK():
    """THE v6 REGRESSION -- the 8.23.26 disagreement in miniature.

    `spread` carries more over-cap cells but a lower worst case; `spike` cuts
    the total while driving one system further over. The mean alone PREFERS
    spike, which is how the tuner beat its own gate. The scored term must not.
    """
    spread = _wb([("OG1N", 100, 100, 120, 100),    # excess .20
                  ("OG2N", 100, 100, 118, 100),    # excess .18
                  ("OG3N", 50, 100, 50, 100),
                  ("OG4N", 50, 100, 50, 100),
                  ("OG5N", 50, 100, 50, 100)])
    spike = _wb([("OG1N", 100, 100, 125, 100),     # excess .25, and alone
                 ("OG2N", 50, 100, 50, 100),
                 ("OG3N", 50, 100, 50, 100),
                 ("OG4N", 50, 100, 50, 100),
                 ("OG5N", 50, 100, 50, 100)])
    # the v5 term, for the record: spike's mean is genuinely LOWER
    assert 0.25 / 5 < (0.20 + 0.18) / 5

    a = optimize._system_overshoot(spread)   # .076 + .20 = .276
    b = optimize._system_overshoot(spike)    # .050 + .25 = .300
    assert a == pytest.approx(0.276, abs=1e-9)
    assert b == pytest.approx(0.300, abs=1e-9)
    assert b > a, "a worse worst-system must not score better on a lower total"


def test_the_peak_alone_does_not_decide_when_peaks_TIE():
    """Peak gates which plans are comparable; the mean separates the ties.
    Same worst cell, different totals -> the total still decides."""
    lean = optimize._system_overshoot(_wb([("OG1N", 100, 100, 120, 100),
                                           ("OG2N", 50, 100, 50, 100)]))
    heavy = optimize._system_overshoot(_wb([("OG1N", 100, 100, 120, 100),
                                            ("OG2N", 100, 100, 115, 100)]))
    assert lean < heavy


def test_compliant_cells_contribute_nothing():
    assert optimize._system_overshoot(
        _wb([("OG1N", 50, 100, 50, 100), ("OG2N", 99, 100, 99, 100)])) == 0.0


def test_a_cell_over_on_both_counts_its_WORSE_dimension_not_the_sum():
    """The binding constraint is the one you hit first; summing would
    double-count a single overloaded system."""
    both = optimize._system_overshoot(_wb([("OG1N", 110, 100, 130, 100)]))
    assert both == pytest.approx(0.60, abs=1e-9)   # 2 x 0.30, not 2 x 0.40


def test_only_the_MEAN_half_is_diluted_by_compliant_cells():
    """One system 32% over among four compliant ones is a smaller AVERAGE
    problem -- but that system is exactly as unable to feed its fish, and the
    peak half says so. Dilution must not erase a breach entirely (pre-v6 it
    scaled the whole term by 1/n)."""
    alone = optimize._system_overshoot(_wb([("OG1N", 100, 100, 132, 100)]))
    among = optimize._system_overshoot(_wb([("OG1N", 100, 100, 132, 100),
                                            ("OG2N", 50, 100, 50, 100),
                                            ("OG3N", 50, 100, 50, 100),
                                            ("OG4N", 50, 100, 50, 100)]))
    assert alone == pytest.approx(0.64, abs=1e-9)    # .32 + .32
    assert among == pytest.approx(0.40, abs=1e-9)    # .08 + .32
    assert among > alone / 4, "the peak survives dilution; only the mean thins"


def test_missing_sheet_scores_zero_not_a_crash():
    wb = openpyxl.Workbook()
    wb.active.title = "Something Else"
    assert optimize._system_overshoot(wb) == 0.0
