"""Growth history + actual growth model (forecast/growth_history.py).

Operator plan 2026-10-18: the actual growth model builds on every PR added;
full monthly reports (with the Acc tab) supersede mid-month PRs; mid-month
intervals are provisional and count by their days; a 24-month window; each fit
is a frozen version. The tests pin each of those rules, and each rule has a
case where getting it wrong would change the answer (the negative controls).
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path

import pytest

from forecast import growth_history as gh
from forecast.biology import sgr_pct_per_day
from forecast.models import BatchInput, BiologyTables

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PR = ROOT / "tests" / "fixtures" / "pr_2026-09-30.xlsx"
SIXN = {61, 63, 65, 67, 69, 71}


def _snap(closing, og, kind=gh.FULL, source=None, added="2026-10-18T00:00:00", sha=None,
          modified=""):
    source = source or f"{closing} report.xlsx"
    return gh.Snapshot(closing=closing, kind=kind, source=source,
                       sha1=sha or f"{source}|{closing}|{kind}", added=added,
                       og=[list(r) for r in og], modified=modified)


# --------------------------------------------------------------------------- #
# Which report is used
# --------------------------------------------------------------------------- #
def test_full_report_beats_a_mid_month_pr_with_the_same_closing_date():
    mid = _snap("2026-09-30", [("B45", 31, 1000, 2000)], gh.MID, "10.1.26_PR.xlsx")
    full = _snap("2026-09-30", [("B45", 31, 1000, 2100)], gh.FULL, "Sep'26 Production Report.xlsx")
    for order in ([mid, full], [full, mid]):
        chain, sup = gh.choose_chain(order)
        assert [s.source for s in chain] == ["Sep'26 Production Report.xlsx"]
        assert [s.source for s, _ in sup] == ["10.1.26_PR.xlsx"]


def test_mid_month_prs_drop_out_once_their_months_full_report_is_in():
    s = [_snap("2026-08-31", []),
         _snap("2026-09-15", [], gh.MID, "9.15 PR.xlsx"),
         _snap("2026-09-30", [])]
    chain, sup = gh.choose_chain(s)
    assert [x.closing for x in chain] == ["2026-08-31", "2026-09-30"]
    assert "full report for its month" in sup[0][1]


def test_open_month_uses_only_its_latest_mid_month_pr():
    s = [_snap("2026-09-30", []),
         _snap("2026-10-08", [], gh.MID, "10.8.26_PR.xlsx"),
         _snap("2026-10-15", [], gh.MID, "10.15.26_PR.xlsx"),
         _snap("2026-10-11", [], gh.MID, "10.11.26_PR.xlsx")]
    chain, _ = gh.choose_chain(s)
    assert [x.source for x in chain][-1] == "10.15.26_PR.xlsx"
    assert len(chain) == 2


def test_final_beats_prelim_and_whole_report_beats_an_extract():
    prelim = _snap("2026-08-31", [("B1", 11, 1, 1)] * 30, source="Aug'26 Production Report Prelim.xls")
    final = _snap("2026-08-31", [("B1", 11, 1, 1)] * 30, source="Aug'26 Production Report.xlsx")
    assert gh.choose_chain([final, prelim])[0][0].source == final.source
    # An extract loses even when it is the newer file.
    extract = _snap("2025-05-31", [("B33", 31, 1, 1)] * 3, source="May'25 Production Report B33 B34.xlsx",
                    modified="2025-06-30T00:00:00")
    whole = _snap("2025-05-31", [("B33", 31, 1, 1)] * 24, source="May'25 Production Report.xlsx",
                  modified="2025-06-10T00:00:00")
    assert gh.choose_chain([whole, extract])[0][0].source == whole.source


def test_newest_version_wins_when_reports_are_otherwise_equal():
    rows = [("B33", 31, 1, 1)] * 31
    v2 = _snap("2024-11-30", rows, source="Nov'24 Production Report V2.xlsx",
               modified="2024-12-09T14:34:00", added="2026-10-18T00:00:02")
    v3 = _snap("2024-11-30", rows, source="Nov'24 Production Report V3.xlsx",
               modified="2024-12-09T14:37:00", added="2026-10-18T00:00:01")
    for order in ([v2, v3], [v3, v2]):
        assert gh.choose_chain(order)[0][0].source == v3.source


# --------------------------------------------------------------------------- #
# Clean intervals
# --------------------------------------------------------------------------- #
def test_only_clean_same_tank_intervals_count():
    s0 = _snap("2026-08-31", [("B45", 31, 10000, 2000), ("B46", 32, 10000, 2000),
                              ("B47", 33, 10000, 2000), ("B48", 61, 10000, 2000),
                              ("B49", 34, 10000, 2000), ("B50", 35, 10000, 2000)])
    s1 = _snap("2026-09-30", [("B45", 31, 9900, 2400),      # clean (1% mortality)
                              ("B46", 32, 9000, 2400),      # 10% lost: harvest/grade
                              ("B47", 33, 10500, 2400),     # fish added
                              ("B48", 61, 10000, 2400),     # 6N
                              ("B49", 44, 10000, 2400),     # moved tank
                              ("B50", 35, 9600, 2400)])     # 4% lost: still clean
    ivs = gh.clean_intervals([s0, s1], SIXN)
    assert sorted(i.batch for i in ivs) == ["B45", "B50"]
    i = next(x for x in ivs if x.batch == "B45")
    assert i.days == 30 and i.agr == pytest.approx(400 / 30)
    assert i.sgr == pytest.approx(100 * math.log(1.2) / 30)
    assert not i.provisional and i.weight == 1.0


def test_mid_month_interval_is_provisional_and_counts_by_its_days():
    s0 = _snap("2026-09-30", [("B45", 31, 1000, 2000)])
    s1 = _snap("2026-10-06", [("B45", 31, 1000, 2080)], gh.MID)
    (i,) = gh.clean_intervals([s0, s1], SIXN)
    assert i.provisional and i.weight == pytest.approx(6 / 30)


# --------------------------------------------------------------------------- #
# The fit
# --------------------------------------------------------------------------- #
A, B, CAP = 0.7, 0.39, 14.0
SIZES = [100.0, 150.0, 500.0, 1000.0, 2000.0, 3000.0, 4000.0, 5000.0]
BASE = [1.0, 1.77, 1.37, 1.05, 0.76, 0.62, 0.52, 0.368]


def _true_agr(w):
    return min(A * w ** B, CAP)


def _synthetic(end: date, months=24, scale=1.0, provisional_tail=False):
    """Tank-months that follow the true AGR exactly (x scale), sizes 300-4500 g."""
    ivs = []
    d1 = end
    for m in range(months):
        d0 = d1 - timedelta(days=30)
        for k, w0 in enumerate(range(300, 4600, 200)):
            w = float(w0)
            for _ in range(30):
                w += _true_agr(w) * scale
            ivs.append(gh.Interval(f"B{k}", 30 + k % 20, d0.isoformat(), d1.isoformat(),
                                   30, 1000, 995, float(w0), w,
                                   provisional=provisional_tail and m == 0))
        d1 = d0
    return ivs


def test_fit_recovers_a_known_growth_curve():
    end = date(2026, 10, 15)
    f = gh.fit_actual(_synthetic(end), sizes=SIZES, base_sw=BASE, as_of=end)
    assert f.cap_g_day == pytest.approx(CAP, rel=0.03)
    for w in (500, 1000, 1500):
        assert f.agr_g_day(w) == pytest.approx(_true_agr(w), rel=0.06)
    # Table rows: below the SW minimum keep the base value; the rest are the fit.
    assert f.sgr_pct_day[0] == BASE[0]
    i3 = SIZES.index(3000.0)
    assert f.sgr_pct_day[i3] == pytest.approx(100 * math.log(1 + CAP / 3000), rel=0.03)
    assert f.name == "Actual 2026-10-15" and f.n == 24 * 22


def test_window_leaves_out_growth_older_than_24_months():
    end = date(2026, 10, 15)
    recent = _synthetic(end, months=24)
    # Fast growth that ended before the window - more months of it than of the
    # recent growth, so it would dominate even the median if it got in.
    ancient = _synthetic(gh._months_before(end, 24), months=30, scale=2.0)
    f_recent = gh.fit_actual(recent, sizes=SIZES, base_sw=BASE, as_of=end)
    f_both = gh.fit_actual(recent + ancient, sizes=SIZES, base_sw=BASE, as_of=end)
    assert f_both.sgr_pct_day == f_recent.sgr_pct_day
    # Negative control: with a window long enough to reach it, the fast
    # ancient growth DOES move the fit - so the equality above is the window.
    f_long = gh.fit_actual(recent + ancient, sizes=SIZES, base_sw=BASE, as_of=end,
                           window_months=60)
    assert f_long.cap_g_day > f_recent.cap_g_day * 1.2


def test_short_provisional_intervals_weigh_less_than_full_months():
    end = date(2026, 10, 15)
    base = _synthetic(end)
    # A fast 5-day provisional sliver at every size above the breakpoint.
    fast = [gh.Interval("Bx", 40, (end - timedelta(days=5)).isoformat(), end.isoformat(),
                        5, 1000, 1000, float(w0), float(w0) + 5 * 30.0, True)
            for w0 in range(2100, 4600, 100)] * 4
    f = gh.fit_actual(base + fast, sizes=SIZES, base_sw=BASE, as_of=end)
    assert f.cap_g_day == pytest.approx(CAP, rel=0.05)
    assert f.n_provisional == len(fast)


def test_too_little_data_is_refused_not_guessed():
    end = date(2026, 10, 15)
    few = [i for i in _synthetic(end, months=1)]
    with pytest.raises(ValueError, match="not enough clean tank intervals"):
        gh.fit_actual(few, sizes=SIZES, base_sw=BASE, as_of=end)


def test_holdout_scores_the_fit_against_the_base_curve():
    end = date(2026, 10, 15)
    f = gh.fit_actual(_synthetic(end), sizes=SIZES, base_sw=BASE, as_of=end)
    h = f.holdout
    assert h and h["n"] > 0
    assert abs(h["fit"]["median_err_pct"]) < 1.0      # the truth is the fit's shape


def test_left_out_batches_do_not_shape_the_fit(tmp_path):
    """Operator 2026-10-20: leave chosen batches out of the actual model."""
    end = date(2026, 10, 15)
    base = _synthetic(end)
    # B99 grew at half speed everywhere (a disease episode), lots of it.
    sick = [gh.Interval("B99", i.tank, i.d0, i.d1, i.days, i.c0, i.c1, i.w0,
                        i.w0 + (i.w1 - i.w0) * 0.5, i.provisional) for i in base] * 2
    clean = gh.fit_actual(base, sizes=SIZES, base_sw=BASE, as_of=end)
    out = gh.fit_actual(base + sick, sizes=SIZES, base_sw=BASE, as_of=end,
                        exclude_batches=["B99", " "])
    assert out.sgr_pct_day == clean.sgr_pct_day and out.n == clean.n
    assert out.excluded_batches == ["B99"]
    # negative control: kept in, the slow batch drags the curve down
    kept = gh.fit_actual(base + sick, sizes=SIZES, base_sw=BASE, as_of=end)
    assert kept.cap_g_day < clean.cap_g_day * 0.9 and kept.excluded_batches == []
    # saved and read back; a store written before the field existed still loads
    store = gh.GrowthHistory(tmp_path / "gh.json")
    store.add_fit(out)
    store.save()
    assert gh.GrowthHistory(tmp_path / "gh.json").get_fit(out.name).excluded_batches == ["B99"]
    d = json.loads((tmp_path / "gh.json").read_text(encoding="utf-8"))
    del d["fits"][0]["excluded_batches"]
    (tmp_path / "old.json").write_text(json.dumps(d), encoding="utf-8")
    assert gh.GrowthHistory(tmp_path / "old.json").fits[0].excluded_batches == []


# --------------------------------------------------------------------------- #
# Frozen versions, the store, and the engine
# --------------------------------------------------------------------------- #
def test_versions_are_frozen_and_survive_new_reports(tmp_path):
    end = date(2026, 10, 15)
    store = gh.GrowthHistory(tmp_path / "gh.json")
    f1 = gh.fit_actual(_synthetic(end), sizes=SIZES, base_sw=BASE, as_of=end)
    store.add_fit(f1)
    store.add(_snap("2026-10-31", [("B45", 31, 1000, 2400)]))
    store.save()
    again = gh.GrowthHistory(tmp_path / "gh.json")
    assert again.get_fit("Actual 2026-10-15").sgr_pct_day == f1.sgr_pct_day
    assert [s.closing for s in again.snapshots] == ["2026-10-31"]
    f2 = gh.fit_actual(_synthetic(end, scale=0.8), sizes=SIZES, base_sw=BASE, as_of=end)
    with pytest.raises(ValueError, match="frozen"):
        again.add_fit(f2)
    again.add_fit(f1)                                  # the same values: no-op
    assert len(again.fits) == 1


def test_same_file_twice_is_a_duplicate(tmp_path):
    store = gh.GrowthHistory(tmp_path / "gh.json")
    s = _snap("2026-09-30", [], sha="abc")
    assert store.add(s) == "added"
    assert store.add(_snap("2026-09-30", [], source="copy.xlsx", sha="abc")) == "duplicate"


def test_store_has_no_default_path():
    with pytest.raises(TypeError):
        gh.GrowthHistory()                             # noqa


def test_a_fitted_version_runs_in_the_engine_as_a_growth_model():
    end = date(2026, 10, 15)
    f = gh.fit_actual(_synthetic(end), sizes=SIZES, base_sw=BASE, as_of=end)
    name, vals = gh.biology_entry(f, SIZES)
    t = BiologyTables(sgr_size_g=SIZES, sgr_fw_pct_day=[1.0] * len(SIZES),
                      sgr_sw_pct_day=BASE, sgr_sw_by_model={name: vals})
    b = BatchInput("B60", None, 0, None, None, None, None, 16.0, "FCR_116_Quick",
                   1.0, 1.0, growth_model=name)
    assert sgr_pct_per_day(3000.0, "SW", b, t) == pytest.approx(
        f.sgr_pct_day[SIZES.index(3000.0)])
    with pytest.raises(ValueError, match="different biology size rows"):
        gh.biology_entry(f, SIZES[:-1])


def test_report_names():
    yes = ["Sep'26 Production Report.xlsx", "10.15.26_PR.xlsx", "8.23.26 PR.xlsx"]
    no = ["4 30 2026 AS Monthly Production Report Template.xls",
          "Acc Exposure Close W41.xlsx", "~$Sep'26 Production Report.xlsx",
          "OG Transfer in Metrics.xlsx"]
    assert all(gh.is_report_name(n) for n in yes)
    assert not any(gh.is_report_name(n) for n in no)


@pytest.mark.skipif(not FIXTURE_PR.exists(), reason="fixture PR missing")
def test_reads_the_fixture_pr(tmp_path):
    s = gh.read_snapshot(FIXTURE_PR)
    assert s.closing == "2026-09-30" and s.kind in (gh.FULL, gh.MID)
    assert len(s.og) > 10 and all(r[3] > 0 for r in s.og)
    store = gh.GrowthHistory(tmp_path / "gh.json")
    assert store.add(s) == "added"
    store.save()
    assert json.loads((tmp_path / "gh.json").read_text())["format"] == gh.STORE_FORMAT
