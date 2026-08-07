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
# Revenue banding — size-biased lognormal spread (the operator's Excel method)
# --------------------------------------------------------------------------- #
def _econ(cv_pct=18.0):
    return {"currency": "USD", "basis": "hog", "model_cv_pct": cv_pct,
            "price_bands": [
                {"min_kg": 2.0, "max_kg": 3.0, "price_per_kg": 8.0,
                 "monthly": {}},
                {"min_kg": 3.0, "max_kg": 5.0, "price_per_kg": 10.0,
                 "monthly": {}},
            ]}


def test_band_fraction_matches_monte_carlo():
    # Independent check: the analytic size-biased share must match a
    # biomass-weighted Monte Carlo of the SAME lognormal population.
    import math
    import random
    mean, cv, lo, hi = 4.0, 0.18, 3.629, 4.536
    s = math.sqrt(math.log(1 + cv * cv))
    mu = math.log(mean) - 0.5 * s * s
    rng = random.Random(42)
    draws = [rng.lognormvariate(mu, s) for _ in range(200_000)]
    mc = (sum(w for w in draws if lo <= w < hi) / sum(draws))
    assert A.biomass_band_fraction(mean, cv, lo, hi) == pytest.approx(mc, abs=0.01)


def test_band_fraction_degenerate_and_total():
    # cv=0 collapses to the old step function; wide band captures ~all kg.
    assert A.biomass_band_fraction(4.0, 0.0, 3.629, 4.536) == 1.0
    assert A.biomass_band_fraction(4.0, 0.0, 5.0, 6.0) == 0.0
    assert A.biomass_band_fraction(4.0, 0.18, 0.001, 1000.0) == pytest.approx(1.0, abs=1e-6)


def test_revenue_spreads_kg_across_bands():
    rows = [{"week": "2026-W32", "count": 500, "gross_avg_kg": 3.5,
             "gross_kg": 1750.0, "hog_kg": 1500.0, "hog_avg_kg": 3.0}]
    rev = A.revenue_for(rows, _econ())
    # Mean sits ON the 2-3/3-5 band edge: kg splits across BOTH bands
    # (roughly half each, size-bias tilting to the upper), none silently lost.
    assert rev["by_band"][0]["kg"] > 0 and rev["by_band"][1]["kg"] > 0
    assert rev["priced_kg"] + rev["unpriced_kg"] == pytest.approx(1500.0)
    assert rev["unpriced_kg"] < 150.0        # only the far tails are unpriced
    assert rev["total"] == pytest.approx(
        rev["by_band"][0]["kg"] * 8.0 + rev["by_band"][1]["kg"] * 10.0)


def test_revenue_tails_outside_ladder_are_unpriced():
    rows = [{"week": "2026-W33", "count": 100, "gross_avg_kg": 7.0,
             "gross_kg": 700.0, "hog_kg": 600.0, "hog_avg_kg": 6.0}]
    rev = A.revenue_for(rows, _econ())
    # Mean 6.0 kg is above every band: most kg unpriced, a little lower-tail
    # kg lands in the 3-5 band — loud gap, not an invented price.
    assert rev["unpriced_kg"] > 400.0
    assert rev["priced_kg"] == pytest.approx(600.0 - rev["unpriced_kg"])


def test_revenue_monthly_price_override():
    econ = _econ()
    econ["price_bands"][1]["monthly"] = {"2026-08": 12.0}   # W32 -> 2026-08
    rows = [{"week": "2026-W32", "count": 500, "gross_avg_kg": 4.7,
             "gross_kg": 2350.0, "hog_kg": 2000.0, "hog_avg_kg": 4.0}]
    base = A.revenue_for(rows, _econ())
    bumped = A.revenue_for(rows, econ)
    # Same kg distribution; the 3-5 band earns 12 instead of 10 in August.
    assert bumped["by_band"][1]["kg"] == pytest.approx(base["by_band"][1]["kg"])
    assert bumped["by_band"][1]["revenue"] == pytest.approx(
        base["by_band"][1]["kg"] * 12.0)


def test_load_economics_model_cv_and_monthly(tmp_path):
    (tmp_path / A.ECONOMICS_FILE).write_text(
        "currency: USD\nbasis: hog\nmodel_cv_pct: 22\n"
        "price_bands:\n"
        "  - min_kg: 2.0\n    max_kg: 3.0\n    price_per_kg: 8.0\n"
        "    monthly:\n      2026-09: 9.1\n")
    e = A.load_economics(tmp_path)
    assert e["model_cv_pct"] == 22.0
    assert e["price_bands"][0]["monthly"] == {"2026-09": 9.1}
    # Legacy schema (no model_cv_pct / monthly) still loads with defaults.
    (tmp_path / A.ECONOMICS_FILE).write_text(
        "currency: USD\nprice_bands:\n"
        "  - min_kg: 2.0\n    max_kg: 3.0\n    price_per_kg: 8.0\n")
    e2 = A.load_economics(tmp_path)
    assert e2["model_cv_pct"] == 18.0 and e2["price_bands"][0]["monthly"] == {}


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


# --------------------------------------------------------------------------- #
# Result cache — reload/frozen-tab resilience
# --------------------------------------------------------------------------- #
def test_cache_roundtrip_and_prefix(tmp_path):
    A.cache_save("board_controller", {"sig": "abc", "res": {"ok": True,
                 "output_bytes": b"\x00\x01" * 100}}, cache_dir=tmp_path)
    A.cache_save("ana_summary", {"sig": "abc", "emphasis": "Walk the line"},
                 cache_dir=tmp_path)
    board = A.cache_load_all(cache_dir=tmp_path, prefix="board_")
    assert list(board) == ["board_controller"]
    assert board["board_controller"]["res"]["output_bytes"][:2] == b"\x00\x01"
    everything = A.cache_load_all(cache_dir=tmp_path)
    assert set(everything) == {"board_controller", "ana_summary"}


def test_cache_overwrite_newest_wins(tmp_path):
    A.cache_save("board_x", {"sig": "old"}, cache_dir=tmp_path)
    A.cache_save("board_x", {"sig": "new"}, cache_dir=tmp_path)
    assert A.cache_load_all(cache_dir=tmp_path)["board_x"]["sig"] == "new"


def test_cache_eviction_keeps_newest(tmp_path):
    import os
    import time
    for i in range(6):
        A.cache_save(f"e{i}", {"i": i}, cache_dir=tmp_path, keep=6)
        # deterministic ordering: backdate earlier entries into the past
        t = time.time() - 100 + i
        os.utime(tmp_path / f"e{i}.pkl", (t, t))
    A.cache_save("e_final", {"i": 99}, cache_dir=tmp_path, keep=3)
    kept = A.cache_load_all(cache_dir=tmp_path)
    assert len(kept) == 3 and "e_final" in kept


def test_cache_corrupt_entry_is_skipped_not_fatal(tmp_path):
    A.cache_save("good", {"ok": 1}, cache_dir=tmp_path)
    (tmp_path / "bad.pkl").write_bytes(b"this is not a pickle")
    out = A.cache_load_all(cache_dir=tmp_path)
    assert "good" in out and "bad" not in out


# --------------------------------------------------------------------------- #
# Knob-search variant cache — crash/resume resilience
# --------------------------------------------------------------------------- #
def test_sweep_variant_cache_reuses_and_relabels(monkeypatch):
    from forecast import optimize as O
    calls = []

    def fake_run(label, ov, cdir, sdir, inp):
        calls.append(label)
        return O.OptVariant(label=label, overrides=dict(ov), metrics=None,
                            dropped=0, overprod=0)

    monkeypatch.setattr(O, "run_variant", fake_run)
    grid = [("baseline", {}), ("a", {"x": 1})]
    vc = {}
    r1 = O.sweep("in", "cfg", "scn", grid=grid, parallel=False,
                 variant_cache=vc)
    assert len(calls) == 2 and len(vc) == 2 and len(r1) == 2
    # Second search: everything reused, nothing re-run — this is the
    # crash-resume property (a mid-search crash keeps completed variants).
    r2 = O.sweep("in", "cfg", "scn", grid=grid, parallel=False,
                 variant_cache=vc)
    assert len(calls) == 2
    # Same overrides under a NEW label (descent reaching a grid point):
    # reused, and relabeled for the requester.
    r3 = O.sweep("in", "cfg", "scn", grid=[("renamed", {"x": 1})],
                 parallel=False, variant_cache=vc)
    assert len(calls) == 2 and r3[0].label == "renamed"
    assert r3[0].overrides == {"x": 1}


def test_sweep_without_cache_unchanged(monkeypatch):
    from forecast import optimize as O
    calls = []

    def fake_run(label, ov, cdir, sdir, inp):
        calls.append(label)
        return O.OptVariant(label=label, overrides=dict(ov), metrics=None,
                            dropped=0, overprod=0)

    monkeypatch.setattr(O, "run_variant", fake_run)
    grid = [("baseline", {}), ("a", {"x": 1})]
    O.sweep("in", "cfg", "scn", grid=grid, parallel=False)
    O.sweep("in", "cfg", "scn", grid=grid, parallel=False)
    assert len(calls) == 4    # no cache -> every variant runs every time


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


# --------------------------------------------------------------------------- #
# Density-quality gate (the old Tune readout as a checklist lens)
# --------------------------------------------------------------------------- #
def test_density_gate_pass_warn_na():
    dr_ok = {"n": 60, "over": 40, "severe": 0, "worst": 1.12, "median": 1.01,
             "buckets": {}, "severe_rows": []}
    st1 = {g["key"]: g for g in A.evaluate_gates(
        {"dropped": 0, "overprod": 0, "density_review": dr_ok})}
    assert st1["density_quality"]["status"] == "PASS"
    assert st1["density_quality"]["hard"] is False   # diagnostic, never blocks

    dr_bad = dict(dr_ok, severe=3, worst=1.42)
    st2 = {g["key"]: g["status"] for g in A.evaluate_gates(
        {"dropped": 0, "overprod": 0, "density_review": dr_bad})}
    assert st2["density_quality"] == "WARN"          # penalized, not FAIL

    st3 = {g["key"]: g["status"] for g in A.evaluate_gates({"dropped": 0})}
    assert st3["density_quality"] == "N/A"           # absent data never fails


def test_sweep_pickling_break_degrades_to_sequential(monkeypatch):
    # A hot-reload under a live run breaks pool serialization (PicklingError).
    # The sweep must finish sequentially — reusing everything the cache
    # already has — instead of dying (crashed a real run 2026-08-07).
    import concurrent.futures as cf
    from pickle import PicklingError
    from forecast import optimize as O

    class _BrokenPool:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def submit(self, *a, **k):
            raise PicklingError("not the same object as forecast.optimize.run_variant")

    calls = []

    def fake_run(label, ov, cdir, sdir, inp):
        calls.append(label)
        return O.OptVariant(label=label, overrides=dict(ov), metrics=None,
                            dropped=0, overprod=0)

    monkeypatch.setattr(O, "run_variant", fake_run)
    monkeypatch.setattr(cf, "ProcessPoolExecutor", _BrokenPool)
    grid = [("baseline", {}), ("a", {"x": 1}), ("b", {"x": 2})]
    vc = {}
    res = O.sweep("in", "cfg", "scn", grid=grid, parallel=True, max_workers=4,
                  variant_cache=vc)
    assert [v.label for v in res] == ["baseline", "a", "b"]
    assert len(calls) == 3 and len(vc) == 3


def test_variant_cache_stores_only_plain_data(monkeypatch):
    # The reload-proof invariant: cache VALUES must be plain dicts (a class
    # instance tied to a module generation becomes unpicklable after a
    # hot-reload — the 2026-08-07 disk-cache failures).
    import pickle
    from forecast import optimize as O

    def fake_run(label, ov, cdir, sdir, inp):
        return O.OptVariant(label=label, overrides=dict(ov),
                            metrics=O._infeasible_metrics(), dropped=0,
                            overprod=0)

    monkeypatch.setattr(O, "run_variant", fake_run)
    vc = {}
    res = O.sweep("in", "cfg", "scn", grid=[("a", {"x": 1})], parallel=False,
                  variant_cache=vc)
    assert all(isinstance(v, dict) for v in vc.values())
    pickle.dumps(vc)                       # must never raise
    # And the rebuild path returns a real OptVariant with a real Metrics.
    again = O.sweep("in", "cfg", "scn", grid=[("a", {"x": 1})], parallel=False,
                    variant_cache=vc)
    assert again[0].metrics is not None
    assert again[0].metrics.weeks_over_harvest_cap == res[0].metrics.weeks_over_harvest_cap
