"""forecast.analysis — targets, economics, gates, ranking, defaults.

Pure-logic tests (no pipeline runs): period math, target judging incl. the
horizon boundary, price banding incl. the unpriced gap, gate statuses, the
operator-approved rank ordering, and the promoted-default round-trip that
guarantees the setting cannot be lost between sessions."""
import tempfile
from pathlib import Path

import pytest

from forecast import analysis as A


# --------------------------------------------------------------------------- #
# Period mapping
# --------------------------------------------------------------------------- #
def test_week_to_month_iso_monday_convention():
    # 2026-W31's Monday is 2026-07-27 -> July, even though the week spills
    # into August. Must match the app's Harvest-tab convention.
    assert A.week_to_month("2026-W31") == "2026-07"
    assert A.week_to_month("2026-W32") == "2026-08"


def test_week_to_month_garbage_is_none():
    assert A.week_to_month("not-a-week") is None
    assert A.week_to_month("") is None


def _rows(*triples):
    """(week, gross_kg, hog_kg) -> harvest_rows-shaped dicts."""
    return [{"week": w, "count": 1000.0, "gross_avg_kg": 4.0,
             "gross_kg": g, "hog_kg": h, "hog_avg_kg": h / 1000.0}
            for (w, g, h) in triples]


def test_harvest_by_period_totals_and_basis():
    rows = _rows(("2026-W31", 100.0, 85.0), ("2026-W32", 50.0, 42.0),
                 ("2026-W35", 10.0, 8.0))
    monthly, yearly = A.harvest_by_period(rows, basis="hog")
    assert monthly == {"2026-07": 85.0, "2026-08": 50.0}
    assert yearly == {"2026": 135.0}
    monthly_g, _ = A.harvest_by_period(rows, basis="gross")
    assert monthly_g["2026-07"] == 100.0


# --------------------------------------------------------------------------- #
# Target review
# --------------------------------------------------------------------------- #
def _targets(monthly=None, yearly=None, tol=5.0):
    return {"basis": "hog", "tolerance_pct": tol,
            "monthly": monthly or {}, "yearly": yearly or {}}


def test_review_targets_met_close_missed():
    monthly = {"2026-07": 100.0, "2026-08": 96.0, "2026-09": 50.0}
    tr = A.review_targets(monthly, {}, _targets(
        monthly={"2026-07": 100.0, "2026-08": 100.0, "2026-09": 100.0}))
    by = {r["period"]: r["status"] for r in tr["rows"]}
    assert by == {"2026-07": "MET", "2026-08": "CLOSE", "2026-09": "MISSED"}
    assert tr["met"] == 1 and tr["close"] == 1 and tr["missed"] == 1
    assert tr["worst_pct"] == pytest.approx(50.0)
    assert tr["total_shortfall_kg"] == pytest.approx(54.0)


def test_review_targets_beyond_horizon_is_na_not_missed():
    # Harvest ends in 2026-08; a 2026-12 target must be N/A, but an in-horizon
    # blackout month (2026-07, zero harvest recorded) must be judged MISSED.
    monthly = {"2026-06": 80.0, "2026-08": 90.0}
    tr = A.review_targets(monthly, {}, _targets(
        monthly={"2026-07": 100.0, "2026-12": 100.0}))
    by = {r["period"]: r["status"] for r in tr["rows"]}
    assert by["2026-07"] == "MISSED"
    assert by["2026-12"] == "N/A"
    assert tr["judged"] == 1


def test_review_targets_yearly():
    tr = A.review_targets({}, {"2026": 900.0},
                          _targets(yearly={"2026": 1000.0, "2027": 1000.0}))
    by = {r["period"]: r["status"] for r in tr["rows"]}
    assert by["2026"] == "MISSED" and by["2027"] == "N/A"


# --------------------------------------------------------------------------- #
# Revenue banding
# --------------------------------------------------------------------------- #
def _econ():
    return {"currency": "USD", "basis": "hog",
            "price_bands": [
                {"min_kg": 2.0, "max_kg": 3.0, "price_per_kg": 8.0},
                {"min_kg": 3.0, "max_kg": 5.0, "price_per_kg": 10.0},
            ]}


def test_revenue_bands_and_unpriced_gap():
    rows = [
        # hog_avg 2.5 -> band 1: 1000 kg * 8
        {"week": "2026-W31", "count": 400, "gross_avg_kg": 3.0,
         "gross_kg": 1200.0, "hog_kg": 1000.0, "hog_avg_kg": 2.5},
        # hog_avg 4.0 -> band 2: 2000 kg * 10
        {"week": "2026-W32", "count": 500, "gross_avg_kg": 4.7,
         "gross_kg": 2350.0, "hog_kg": 2000.0, "hog_avg_kg": 4.0},
        # hog_avg 6.0 -> NO band: unpriced, loud not silent
        {"week": "2026-W33", "count": 100, "gross_avg_kg": 7.0,
         "gross_kg": 700.0, "hog_kg": 600.0, "hog_avg_kg": 6.0},
    ]
    rev = A.revenue_for(rows, _econ())
    assert rev["total"] == pytest.approx(1000 * 8 + 2000 * 10)
    assert rev["priced_kg"] == pytest.approx(3000.0)
    assert rev["unpriced_kg"] == pytest.approx(600.0)
    assert rev["by_band"][1]["revenue"] == pytest.approx(20000.0)


def test_load_economics_rejects_empty_bands(tmp_path):
    (tmp_path / A.ECONOMICS_FILE).write_text("currency: USD\nprice_bands: []\n")
    assert A.load_economics(tmp_path) is None


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #
def _statuses(ctx):
    return {g["key"]: g["status"] for g in A.evaluate_gates(ctx)}


def test_gates_all_pass():
    st = _statuses({"dropped": 0, "overprod": 0, "zero_weeks": 0,
                    "peak_pct_of_cap": 99.0, "weeks_over_harvest_cap": 0,
                    "targets_review": None})
    assert st["conservation"] == "PASS"
    assert st["no_empty_week"] == "PASS"
    assert st["biomass_cap"] == "PASS"
    assert st["harvest_cap"] == "PASS"
    assert st["targets"] == "N/A"    # no targets configured -> never a FAIL


def test_gates_hard_failures():
    st = _statuses({"dropped": 5, "overprod": 0, "zero_weeks": 2,
                    "peak_pct_of_cap": 120.0, "weeks_over_harvest_cap": 3})
    assert st["conservation"] == "FAIL"
    assert st["no_empty_week"] == "FAIL"
    assert st["biomass_cap"] == "FAIL"     # >110% escalates to FAIL
    assert st["harvest_cap"] == "WARN"     # processing spikes warn, not fail


def test_targets_gate_is_penalized_not_disqualifying():
    tr = A.review_targets({"2026-07": 50.0}, {},
                          _targets(monthly={"2026-07": 100.0}))
    st = _statuses({"dropped": 0, "overprod": 0, "zero_weeks": 0,
                    "targets_review": tr})
    assert st["targets"] == "WARN"   # never FAIL — operator decision


def test_broken_gate_is_visible_not_silent():
    A.register_gate("boom", "always breaks", hard=False,
                    fn=lambda ctx: 1 / 0)
    try:
        res = {g["key"]: g for g in A.evaluate_gates({"dropped": 0})}
        assert res["boom"]["status"] == "FAIL"
        assert "gate error" in res["boom"]["detail"]
    finally:
        A.GATES[:] = [g for g in A.GATES if g.key != "boom"]


# --------------------------------------------------------------------------- #
# Ranking — hard gates dominate everything, then targets, then score
# --------------------------------------------------------------------------- #
def _cand(hard_fail=0, soft_fail=0, warn=0, shortfall=0.0, score=1.0):
    gates = ([{"hard": True, "status": "FAIL", "key": "x", "label": "x"}] * hard_fail
             + [{"hard": False, "status": "FAIL", "key": "y", "label": "y"}] * soft_fail
             + [{"hard": False, "status": "WARN", "key": "z", "label": "z"}] * warn)
    return {"gates": gates, "score": score,
            "targets_review": {"total_shortfall_kg": shortfall}}


def test_rank_hard_gate_dominates_score():
    good_score_but_fails = _cand(hard_fail=1, score=0.01)
    worse_score_but_clean = _cand(score=5.0)
    ranked = sorted([good_score_but_fails, worse_score_but_clean],
                    key=A.rank_key)
    assert ranked[0] is worse_score_but_clean


def test_rank_shortfall_beats_score_within_same_gates():
    misses_target = _cand(shortfall=50_000.0, score=0.01)
    meets_target = _cand(shortfall=0.0, score=5.0)
    assert sorted([misses_target, meets_target],
                  key=A.rank_key)[0] is meets_target


def test_rank_score_breaks_full_ties():
    a, b = _cand(score=2.0), _cand(score=1.0)
    assert sorted([a, b], key=A.rank_key)[0] is b


# --------------------------------------------------------------------------- #
# Promoted default — must survive a round-trip (cannot be lost)
# --------------------------------------------------------------------------- #
def test_promoted_default_roundtrip(tmp_path):
    assert A.load_promoted_default(tmp_path) is None
    A.save_promoted_default(
        tmp_path, "controller-hybrid",
        {"tran_og_default_tanks": 3, "rebalance_level": True},
        promoted_ts="2026-08-05T12:00:00", note="won on 3 PRs",
        evidence={"score": 1.23, "prs": ["7.29.26 PR"]})
    d = A.load_promoted_default(tmp_path)
    assert d["method"] == "controller-hybrid"
    assert d["overrides"] == {"tran_og_default_tanks": 3,
                              "rebalance_level": True}
    assert d["evidence"]["prs"] == ["7.29.26 PR"]
    # Overwrite = the new promotion replaces the old, no merge surprises.
    A.save_promoted_default(tmp_path, "controller", {}, "2026-08-06T09:00:00")
    assert A.load_promoted_default(tmp_path)["method"] == "controller"


def test_targets_roundtrip_and_empty_is_none(tmp_path):
    assert A.load_targets(tmp_path) is None
    A.save_targets(tmp_path, {"basis": "hog", "tolerance_pct": 5.0,
                              "monthly": {"2026-08": 250_000.0},
                              "yearly": {}})
    t = A.load_targets(tmp_path)
    assert t["monthly"] == {"2026-08": 250_000.0}
    A.save_targets(tmp_path, {"basis": "hog", "tolerance_pct": 5.0,
                              "monthly": {}, "yearly": {}})
    assert A.load_targets(tmp_path) is None   # all-empty = unset, gate N/A
