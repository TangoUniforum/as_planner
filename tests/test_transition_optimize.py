"""forecast.transition_optimize — the best future batch size and cap from
today's fish, within the two-window rule (operator, 2026-09-11).

Behavioural invariants on synthetic reads, never pinned numbers, no engine:
the effect year (a mid-year TranOG -> the next year, 1 January -> that year,
no re-sized batch -> the first year with no dated cap row, else the second
year of the run); the verdict (early years no worse than today, judged years
zero, a non-limit FAIL anywhere blocks, a missing count raises); bad grids
refused before anything runs; today's plan run ONCE and every cell at its
own cap with identical arguments (injected runners); run_cell making the
same engine call as step 3's proposal arm (ideal_engine faked); and the
ranking / stability decision reused from ideal_optimize (a fragile winner
with a stable second).
"""
import dataclasses
import datetime as dt
import math
import os
import pickle
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from pickle import PicklingError
from types import SimpleNamespace

import pytest

from forecast import ideal_engine as ie
from forecast import ideal_optimize as io
from forecast import transition as tr
from forecast import transition_optimize as to
from forecast.caps import FacilityLimits
from forecast.config_io import load_config
from forecast.models import BatchInput

ROOT = Path(__file__).resolve().parents[1]
FS = dt.date(2026, 9, 1)             # the 8/31 PR's forecast start
YEARS = (2026, 2027, 2028, 2029, 2030)
E = 2028
CAP = 3_800_000.0
ATTRS = tuple(a for _k, a in to.YEAR_COUNTS)


def _yr(revenue=1e8, hog=1_000.0, gain=1_000.0, **counts):
    base = {a: 0 for a in ATTRS}
    base.update(counts)
    return SimpleNamespace(revenue=revenue, hog_t=hog, gain_t=gain, **base)


def _years(counts=None, revenue=1e8, years=YEARS):
    counts = counts or {}
    return {y: _yr(revenue=revenue, **counts.get(y, {})) for y in years}


def _gates(fails=None, years=YEARS):
    fails = fails or {}
    return {y: tuple(ie.Gate(n, "FAIL", "x") for n in fails.get(y, ()))
            for y in years}


def _batch(bid, input_date, og=340_000, inp=570_000):
    d = dt.datetime.combine(input_date, dt.time())
    return BatchInput(
        batch_id=bid, input_date=d, input_count=inp,
        tran_sf_date=d + dt.timedelta(days=81),
        tran_og_date=d + dt.timedelta(days=350),
        tran_og_count=og, tran_og_avg_wt_g=370.0, tran_og_cv=16.0,
        fcr_model="FCR_116_Quick", fw_correction=1.0, sgr_correction=1.0)


def _live():
    return [_batch(f"X{i:02d}", dt.date(2026, 8, 1) + dt.timedelta(days=49 * i))
            for i in range(8)]


@pytest.fixture
def pr(tmp_path):
    p = tmp_path / "pr.xlsm"
    p.write_bytes(b"not read: the runners are faked")
    return p


# --- the effect year ----------------------------------------------------------

def test_a_resized_batch_mid_year_takes_effect_the_next_year():
    e, why = to.effect_year({"n_changed": 3,
                             "first_changed_tran_og_date": dt.date(2027, 9, 23)},
                            YEARS)
    assert e == 2028
    assert "2027-09-23" in why and "first calendar year" in why
    for d, want in ((dt.date(2027, 12, 31), 2028), (dt.date(2028, 1, 2), 2029),
                    (dt.datetime(2027, 9, 23, 12), 2028)):
        assert to.effect_year({"n_changed": 1,
                               "first_changed_tran_og_date": d}, YEARS)[0] == want


def test_a_resized_batch_on_the_first_of_january_takes_effect_that_year():
    for d in (dt.date(2028, 1, 1), dt.datetime(2028, 1, 1)):
        assert to.effect_year({"n_changed": 2,
                               "first_changed_tran_og_date": d}, YEARS)[0] == 2028


def test_with_no_resized_batch_the_first_year_with_no_dated_cap_row():
    none = {"n_changed": 0, "first_changed_tran_og_date": None}
    e, why = to.effect_year(none, YEARS, undated_cap_year=2027)
    assert e == 2027 and "no future batch is re-sized" in why
    assert "dated per-week cap row" in why
    # Undetermined: the second calendar year of the run, said so.
    e, why = to.effect_year(none, YEARS)
    assert e == 2027 and "second calendar year of the run" in why
    assert to.effect_year(none, (2027, 2028))[0] == 2028
    # A date on a proposal that re-sizes nothing is not an effect.
    assert to.effect_year({"n_changed": 0,
                           "first_changed_tran_og_date": dt.date(2027, 3, 1)},
                          YEARS, undated_cap_year=2026)[0] == 2026
    # Re-sized, but no re-sized batch has a TranOG date: the same fallback.
    e, why = to.effect_year({"n_changed": 2,
                             "first_changed_tran_og_date": None}, YEARS)
    assert e == 2027 and "none of the re-sized batches has a TranOG date" in why
    with pytest.raises(ValueError):
        to.effect_year(none, ())


def test_a_change_that_leaves_the_cap_alone_gets_the_same_year_in_its_words():
    """No re-sized batch and the cap not moved (only other limits change):
    the cap-only rule still places E, and the reason does not name a what-if
    cap that was never set."""
    none = {"n_changed": 0, "first_changed_tran_og_date": None}
    e, why = to.effect_year(none, YEARS, undated_cap_year=2027,
                            cap_changed=False)
    assert e == to.effect_year(none, YEARS, undated_cap_year=2027)[0] == 2027
    assert "biomass cap is not changed" in why
    assert "what-if cap applies" not in why
    e, why = to.effect_year(none, YEARS, cap_changed=False)
    assert e == 2027 and "second calendar year of the run" in why
    assert "not changed" in why
    # A re-sized batch places E whatever the cap does.
    e, why = to.effect_year({"n_changed": 1,
                             "first_changed_tran_og_date": dt.date(2027, 9, 23)},
                            YEARS, cap_changed=False)
    assert e == 2028 and "not changed" not in why
    # A cell judged at today's cap says the cap is not changed; below it,
    # the what-if cap — the same year either way.
    today = _today()
    same = to.judge_cell(_cell(340_000, n=0, first=None), today, undated=2027)
    lower = to.judge_cell(_cell(340_000, cap=3.6e6, n=0, first=None), today,
                          undated=2027)
    assert same.effect == lower.effect == 2027
    assert "not changed" in same.effect_why
    assert "what-if cap applies" in lower.effect_why
    # The date is read by transition's own reader: a string is refused.
    with pytest.raises(ValueError, match="date or datetime"):
        to.effect_year({"n_changed": 1,
                        "first_changed_tran_og_date": "2027-09-23"}, YEARS)
    assert to.years_text([2030, 2026, 2028]) == "2026–2030"
    assert to.years_text([2028]) == "2028" and to.years_text(()) == "no year"


def test_the_undated_cap_year_reads_only_dated_biomass_rows():
    fl = FacilityLimits(overrides={("2026-W40", "biomass"): 3_650_000.0,
                                   ("2026-W41", "biomass"): 3_650_000.0,
                                   ("2027-W02", "feed_per_day"): 30_000.0})
    assert to.undated_cap_year(fl, YEARS) == 2027      # a feed row is not a cap
    fl2 = FacilityLimits(overrides={**fl.overrides,
                                    ("2027-W50", "biomass"): 1.0})
    assert to.undated_cap_year(fl2, YEARS) == 2028
    assert to.undated_cap_year(FacilityLimits(), YEARS) == 2026
    every = FacilityLimits(overrides={(f"{y}-W10", "biomass"): 1.0
                                      for y in YEARS})
    assert to.undated_cap_year(every, YEARS) is None


def test_the_live_limits_put_a_cap_only_change_in_the_second_year():
    """On the live limits file the per-week cap rows are all 2026, so a
    cap-only change takes effect in 2027 — the same year the fallback
    names. Read, never pinned: skipped if the file changes shape."""
    from forecast import scenario_io as sio
    ctl = load_config(str(ROOT / "config"))[0]
    fl = sio.load_limits(str(ROOT / "scenario"), ctl)[0]
    dated = {int(w[:4]) for (w, m) in fl.overrides if m == "biomass"}
    if dated != {2026}:
        pytest.skip(f"dated cap rows now cover {sorted(dated)}")
    assert to.undated_cap_year(fl, YEARS) == 2027


# --- the verdict -------------------------------------------------------------

def test_equal_to_today_early_and_zero_later_is_within():
    v = to.verdict(_years(), _years(), _gates(), E)
    assert v.within_limits and not v.failures
    assert v.years == YEARS
    assert v.early_years == (2026, 2027) and v.judged_years == (2028, 2029, 2030)
    # Today's breaches in the early years do not count against a proposal
    # that is no worse (equal in 2027, better in 2026).
    today = _years({2026: dict(r8_over_tank_weeks=50),
                    2027: dict(r8_over_tank_weeks=9, sys_feed_over_weeks=40)})
    prop = _years({2026: dict(r8_over_tank_weeks=40),
                   2027: dict(r8_over_tank_weeks=9, sys_feed_over_weeks=40)})
    v = to.verdict(today, prop, _gates(), E)
    assert v.within_limits and not v.failures
    assert v.counts["density"][2027] == (9, 9)
    assert v.excess() == {k: 0 for k in io.BREACH_KEYS}


def test_an_early_year_worse_than_today_on_one_constraint_is_not_within():
    today = _years({2027: dict(r8_over_tank_weeks=9)})
    prop = _years({2027: dict(r8_over_tank_weeks=12)})
    v = to.verdict(today, prop, _gates(), E)
    assert not v.within_limits
    assert v.early_failures == (
        "worse than today: 12 vs 9 tank-weeks over density in 2027",)
    assert v.failures == v.early_failures and not v.judged_failures
    assert v.worse["density"] == {2027: (9, 12)}
    assert v.excess()["density"] == 3
    assert v.year_failures[2027] == v.early_failures
    assert v.year_failures[2026] == ()
    # Year by year, not summed: better in 2026 does not buy 2027 back.
    today = _years({2026: dict(weeks_over_move_budget=5),
                    2027: dict(weeks_over_move_budget=0)})
    prop = _years({2026: dict(weeks_over_move_budget=0),
                   2027: dict(weeks_over_move_budget=1)})
    v = to.verdict(today, prop, _gates(), E)
    assert not v.within_limits
    assert v.early_failures == (
        "worse than today: 1 vs 0 week over the move budget in 2027",)


def test_a_judged_year_with_one_breach_is_not_within():
    # Today's plan breaks the same limit more that year: judged years are
    # zero, not "no worse".
    today = _years({2029: dict(r8_over_tank_weeks=37)})
    prop = _years({2029: dict(r8_over_tank_weeks=1)})
    v = to.verdict(today, prop, _gates(), E)
    assert not v.within_limits
    assert v.judged_failures == ("1 tank-week over density in 2029",)
    assert v.judged()["density"] == 1 and v.breached["density"] == {2029: 1}
    v = to.verdict(_years(), _years({2030: dict(sys_bio_over_weeks=3)}),
                   _gates(), E)
    assert v.judged_failures == ("3 system-weeks over biomass in 2030",)


def test_a_non_limit_fail_in_an_early_year_is_not_within():
    v = to.verdict(_years(), _years(),
                   _gates({2027: ("Conservation audits",)}), E)
    assert not v.within_limits
    assert v.check_failures == ("fails Conservation audits in 2027",)
    assert v.failed_checks == {"Conservation audits": (2027,)}
    v = to.verdict(_years(), _years(), _gates({y: ("Input conservation",)
                                               for y in (2026, 2029)}), E)
    assert v.check_failures == ("fails Input conservation in 2026, 2029",)
    # A LIMIT gate failing in an early year is judged by its count, which is
    # no worse than today's: still within.
    today = _years({2027: dict(r8_over_tank_weeks=9)})
    v = to.verdict(today, today, _gates({2027: ("Tank density (R8)",)}), E)
    assert v.within_limits


def test_a_missing_count_or_year_raises_and_no_shared_year_is_not_within():
    """Detect, don't coerce: a count missing from a read, or a compared year
    with no gates, is an error — never a silent 0 that reads as within."""
    broken = {y: SimpleNamespace(r8_over_tank_weeks=0) for y in YEARS}
    with pytest.raises(AttributeError):
        to.verdict(broken, broken, _gates(), E)
    with pytest.raises(KeyError):
        to.verdict(_years(), _years(), {2026: ()}, E)
    v = to.verdict({}, _years(), {}, E)
    assert v.years == () and not v.within_limits and v.failures
    for bad in (2028.0, True, "2028"):
        with pytest.raises(ValueError):
            to.verdict(_years(), _years(), _gates(), bad)


def test_the_windows_follow_the_effect_year():
    prop = _years({2027: dict(zero_weeks=1)})
    assert not to.verdict(_years(), prop, _gates(), 2027).within_limits
    assert not to.verdict(_years(), prop, _gates(), 2028).within_limits
    today = _years({2027: dict(zero_weeks=1)})
    assert to.verdict(today, prop, _gates(), 2028).within_limits    # early
    assert not to.verdict(today, prop, _gates(), 2027).within_limits  # judged
    v = to.verdict(today, prop, _gates(), 2031)            # nothing judged yet
    assert v.judged_years == () and v.within_limits
    v = to.verdict(today, prop, _gates(), 2020)            # everything judged
    assert v.early_years == () and not v.within_limits


# --- judging a cell against today's plan -------------------------------------

def _cell(size, cap=CAP, counts=None, fails=None, revenue=1e8, years=YEARS,
          first=dt.date(2027, 9, 23), n=3, **kw):
    return to.TrCell(batch_size=size, cap_kg=float(cap),
                     reads=_years(counts, revenue=revenue, years=years),
                     gates_by_year=_gates(fails, years=years), n_changed=n,
                     first_tran_og=first, **kw)


def _today(counts=None, years=YEARS):
    return to.judge_today(to.TrCell(batch_size=0, cap_kg=CAP, today=True,
                                    reads=_years(counts, years=years),
                                    gates_by_year=_gates(years=years)))


def test_a_cell_is_judged_on_the_years_both_runs_cover():
    today = _today({2027: dict(r8_over_tank_weeks=9)})
    c = to.judge_cell(_cell(300_000, revenue=2e8, years=YEARS + (2031,),
                            counts={2027: dict(r8_over_tank_weeks=12),
                                    2029: dict(sys_feed_over_weeks=2)},
                            fails={2028: ("Conservation audits",),
                                   2029: ("Conservation audits",
                                          "Harvest floor")}), today)
    assert c.years == YEARS                       # 2031 is not compared
    assert c.revenue == pytest.approx(2e8 * len(YEARS))
    assert c.hog_t == pytest.approx(1_000.0 * len(YEARS))
    assert c.effect == 2028 and "2027-09-23" in c.effect_why
    assert not c.within_limits and c.verdict.effect_year == 2028
    assert c.breaches["density"] == 3 and c.breaches["sys_feed"] == 2
    assert c.total == 5
    # One gate per non-limit name (a limit gate is judged by its count).
    assert io.other_failed_checks(c) == ("Conservation audits",)
    assert today.revenue == pytest.approx(1e8 * len(YEARS))
    # A cap-only cell (nothing re-sized) takes the undated cap year.
    c = to.judge_cell(_cell(340_000, n=0, first=None), today, undated=2027)
    assert c.effect == 2027 and c.within_limits
    # An errored cell comes back as it is, never within.
    err = to.TrCell(batch_size=300_000, cap_kg=CAP, error="RuntimeError: x")
    assert to.judge_cell(err, today) is err


def test_the_label_and_key_carry_the_size_and_cap():
    c = _cell(320_000, cap=3_600_000)
    assert c.label == "320,000 @ 3,600 t" and c.cap_t == 3_600.0
    assert c.key == (to.TR_CADENCE, 320_000, 3_600_000.0)
    assert io.neighbour_rhythms(c) == [(to.TR_CADENCE, 315_000, 3_600_000.0),
                                       (to.TR_CADENCE, 325_000, 3_600_000.0)]
    assert _today().label == "today's plan @ 3,800 t"


# --- bad grids, refused before anything runs ---------------------------------

def _no_run(monkeypatch):
    ran = []
    monkeypatch.setattr(to, "run_cell", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(to, "run_today", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(ie, "run_schedule", lambda *a, **k: ran.append(a))
    return ran


@pytest.mark.parametrize("sizes, caps, extra", [
    ([], [CAP], {}),                                   # no size
    ([300_000], [], {}),                               # no cap
    ([999], [CAP], {}),                                # not a batch
    ([300_000.0], [CAP], {}),                          # a float is not fish
    ([True], [CAP], {}),
    ([300_000], [0], {}),
    ([300_000], [-CAP], {}),
    ([300_000], [math.nan], {}),
    ([300_000], [math.inf], {}),
    ([300_000], ["3800000"], {}),
    ([300_000], [CAP + 100_000], {}),                  # above the slider
    ([300_000], [CAP], dict(ceiling_kg=0)),
    (list(range(200_000, 322_000, 2_000)), [CAP], {}),  # 61 cells
    (list(range(200_000, 231_000, 1_000)), [CAP, 3.6e6], {}),   # 31 x 2 = 62
    ([300_000], [CAP], dict(objective="profit")),
    ([300_000], [CAP], dict(overrides={"max_biomass_kg": CAP})),
    ([300_000], [CAP], dict(workers=0)),
    ([300_000], [CAP], dict(workers=2.0)),
])
def test_bad_grids_are_refused_before_anything_runs(monkeypatch, pr, sizes,
                                                    caps, extra):
    ran = _no_run(monkeypatch)
    kw = dict(ceiling_kg=CAP, project_dir=ROOT, workers=1, control=object())
    kw.update(extra)
    with pytest.raises(ValueError):
        to.optimize_transition(_live(), FS, FS, sizes, caps, pr, **kw)
    assert not ran


def test_a_missing_pr_and_a_size_propose_refuses_run_nothing(monkeypatch, pr,
                                                              tmp_path):
    ran = _no_run(monkeypatch)
    with pytest.raises(ValueError, match="ProductionReport not found"):
        to.optimize_transition(_live(), FS, FS, [300_000], [CAP],
                               tmp_path / "missing.xlsm", ceiling_kg=CAP,
                               project_dir=ROOT, control=object())
    no_input = [dataclasses.replace(b, input_count=0) for b in _live()]
    with pytest.raises(ValueError, match="no input count"):
        to.optimize_transition(no_input, FS, FS, [300_000], [CAP], pr,
                               ceiling_kg=CAP, project_dir=ROOT,
                               control=object())
    assert not ran


def test_the_grid_is_size_then_cap_high_first_and_at_most_sixty():
    assert to.check_grid([300_000, 240_000, 240_000], [3.4e6, CAP, 3.4e6],
                         CAP) == [(240_000, CAP), (240_000, 3.4e6),
                                  (300_000, CAP), (300_000, 3.4e6)]
    assert len(to.check_grid(range(200_000, 215_000, 1_000),
                             [CAP, 3.6e6, 3.4e6, 3.2e6], CAP)) == 60
    assert to.check_grid([300_000], [CAP], CAP) == [(300_000, CAP)]   # = slider


# --- today's plan once, every cell at its own cap -----------------------------

def _runners(calls, within=lambda size, cap: True):
    def fake_today(live, project_dir, **kw):
        calls.append(("today", None, None, project_dir, kw))
        return to.TrCell(batch_size=0, cap_kg=CAP, today=True,
                         reads=_years(), gates_by_year=_gates())

    def fake_cell(cell, project_dir, **kw):
        size, cap = cell
        calls.append(("cell", size, cap, project_dir, kw))
        ok = within(size, cap)
        # Revenue rises with the batch and as the cap falls.
        return to.TrCell(
            batch_size=size, cap_kg=float(cap),
            reads=_years(None if ok else {2029: dict(r8_over_tank_weeks=2)},
                         revenue=float(size) + (4e6 - cap)),
            gates_by_year=_gates(), n_changed=3,
            first_tran_og=dt.date(2027, 9, 23))
    return fake_today, fake_cell


def _optimize(monkeypatch, pr, calls, sizes, caps, within=lambda s, c: True,
              **kw):
    ft, fc = _runners(calls, within)
    monkeypatch.setattr(to, "run_today", ft)
    monkeypatch.setattr(to, "run_cell", fc)
    ctl = load_config(str(ROOT / "config"))[0]
    args = dict(ceiling_kg=CAP, project_dir=ROOT, workers=1, control=ctl)
    args.update(kw)
    return to.optimize_transition(_live(), FS, FS, sizes, caps, pr, **args), ctl


def test_todays_plan_runs_once_and_every_cell_at_its_own_cap(monkeypatch, pr):
    calls, seen = [], []
    run = dict(overrides={"min_harvest_weight_g": 3_600.0,
                          "max_transfers_per_week": 18},
               method="controller",
               method_overrides={"chronic_pressure_weeks": 6},
               density_overrides={"OG3N": 95.0},
               system_overrides={"OG3N": {"biomass": 450_000.0}})
    res, ctl = _optimize(monkeypatch, pr, calls, [300_000, 240_000],
                         [3.4e6, CAP],
                         progress=lambda d, n, c: seen.append((d, n)), **run)
    today = [c for c in calls if c[0] == "today"]
    cells = [c for c in calls if c[0] == "cell"]
    assert len(today) == 1 and calls[0][0] == "today"
    assert today[0][4] == dict(pr_path=str(pr), method="controller",
                               method_overrides={"chronic_pressure_weeks": 6},
                               control=ctl, horizon_weeks=208)
    grid = [(240_000, CAP), (240_000, 3.4e6), (300_000, CAP), (300_000, 3.4e6)]
    assert [(c[1], c[2]) for c in cells[:4]] == grid          # each cell once
    assert [c.key[1:] for c in res.cells] == grid
    # Identical arguments for every cell: the cap is the cell's own and never
    # in `overrides`; the what-if limits, engine and knobs are the same.
    kws = [c[4] for c in cells]
    assert all(k == kws[0] for k in kws)
    assert kws[0]["overrides"] == run["overrides"]
    assert "max_biomass_kg" not in kws[0]["overrides"]
    for key in ("method", "method_overrides", "density_overrides",
                "system_overrides"):
        assert kws[0][key] == run[key]
    assert kws[0]["pr_path"] == str(pr) and kws[0]["horizon_weeks"] == 208
    assert kws[0]["forecast_start"] == FS and kws[0]["cutoff"] == FS
    assert all(c[3] == str(ROOT) for c in calls)
    # Progress counts today's plan and never goes backwards.
    assert [d for d, _n in seen] == list(range(1, len(calls) + 1))
    assert seen[0][1] == 5
    # The objective is summed over every year; the top earner is the biggest
    # batch at the lowest cap, and ties never depend on the order in.
    assert res.best.key[1:] == (300_000, 3.4e6)
    assert res.best.revenue == pytest.approx(
        len(YEARS) * (300_000 + 4e6 - 3.4e6))
    assert res.today.revenue == pytest.approx(len(YEARS) * 1e8)
    assert res.closest is None and res.note is None
    assert res.ceiling_kg == CAP


def test_a_fragile_winner_with_a_stable_second(monkeypatch, pr):
    calls = []
    res, _ctl = _optimize(monkeypatch, pr, calls,
                          [240_000, 260_000, 280_000], [CAP],
                          within=lambda s, c: s <= 260_000 or s == 285_000)
    cells = [(c[1], c[2]) for c in calls if c[0] == "cell"]
    # The grid, then ONLY the neighbours it lacks, each once, at its cap.
    assert cells[:3] == [(240_000, CAP), (260_000, CAP), (280_000, CAP)]
    assert cells[3:] == [(255_000, CAP), (265_000, CAP), (235_000, CAP),
                         (245_000, CAP)]
    assert res.best.batch_size == 260_000                  # 280k breaks
    assert [(c.batch_size, ok) for c, _n, ok in res.stability] == [
        (260_000, False), (240_000, True)]
    assert res.best_stable.batch_size == 240_000
    assert res.unconstrained.batch_size == 280_000         # display only
    assert all(n.cap_kg == CAP for _c, ns, _ok in res.stability for n in ns)
    # Neighbours are judged against today's plan like any cell.
    assert all(n.verdict is not None for _c, ns, _ok in res.stability
               for n in ns)
    assert res.stability[0][1][1].verdict.judged_failures == (
        "2 tank-weeks over density in 2029",)


def test_at_most_five_candidates_and_a_wave_of_ten(monkeypatch, pr):
    calls = []
    sizes = [200_000 + 20_000 * i for i in range(7)]
    res, _ctl = _optimize(monkeypatch, pr, calls, sizes, [CAP])
    wave = [(c[1], c[2]) for c in calls if c[0] == "cell"][7:]
    assert len(res.stability) == to.TR_MAX_STABILITY_CANDIDATES == 5
    assert len(wave) == 2 * to.TR_MAX_STABILITY_CANDIDATES
    assert [c.batch_size for c, _n, _ok in res.stability] == sizes[::-1][:5]


def test_nothing_within_names_the_closest_and_runs_no_stability(monkeypatch,
                                                                 pr):
    calls = []
    res, _ctl = _optimize(monkeypatch, pr, calls, [240_000, 260_000], [CAP],
                          within=lambda s, c: False)
    assert len(calls) == 3                          # today + 2, no wave
    assert res.best is None and res.stability == () and res.best_stable is None
    assert res.closest == io.closest(res.cells, "revenue")
    assert res.closest.batch_size == 260_000        # fewest, then objective


def test_todays_plan_failing_raises(monkeypatch, pr):
    monkeypatch.setattr(to, "run_today", lambda *a, **k: to.TrCell(
        batch_size=0, cap_kg=CAP, today=True, error="RuntimeError: boom"))
    monkeypatch.setattr(to, "run_cell", _runners([])[1])
    with pytest.raises(RuntimeError, match="today's plan could not run"):
        to.optimize_transition(_live(), FS, FS, [300_000], [CAP], pr,
                               ceiling_kg=CAP, project_dir=ROOT,
                               control=load_config(str(ROOT / "config"))[0])


# --- run_cell: the same engine call as step 3's proposal arm ------------------

def _fake_engine(log, fail_size=None):
    def run_schedule(batches, project_dir, **kw):
        log.append(("run", batches, project_dir, kw))
        sizes = {b.tran_og_count for b in batches}
        if fail_size in sizes:
            raise RuntimeError("the engine wrote no workbook")
        return SimpleNamespace(years=_years(), audits={
            "manual_events_file": "2026-08-31.yaml"})

    def gates(run, year, control):
        log.append(("judge", year, control.max_biomass_kg,
                    control.min_harvest_weight_g,
                    control.chronic_pressure_weeks))
        return [ie.Gate("Biomass cap", "PASS", "x")]
    return run_schedule, gates


def test_run_cell_makes_the_proposal_arms_call_and_judges_at_its_cap(
        monkeypatch, pr):
    log = []
    rs, g = _fake_engine(log)
    monkeypatch.setattr(ie, "run_schedule", rs)
    monkeypatch.setattr(ie, "gates", g)
    ctl = load_config(str(ROOT / "config"))[0]
    cap_before = ctl.max_biomass_kg
    live = _live()
    c = to.run_cell((300_000, 3_400_000.0), ROOT, live=live, forecast_start=FS,
                    cutoff=FS, pr_path=pr,
                    overrides={"min_harvest_weight_g": 3_600.0},
                    method="controller",
                    method_overrides={"chronic_pressure_weeks": 6},
                    density_overrides={"OG3N": 95.0},
                    system_overrides={"OG3N": {"biomass": 450_000.0}},
                    control=ctl)
    (_r, batches, root, kw), *judged = log
    assert batches == tr.propose(live, FS, FS, [300_000])[0]
    assert root == ROOT
    assert kw == dict(pr_path=str(pr), horizon_weeks=208,
                      include_manual_events=True, method="controller",
                      method_overrides={"chronic_pressure_weeks": 6},
                      overrides={"min_harvest_weight_g": 3_600.0,
                                 "max_biomass_kg": 3_400_000.0},
                      density_overrides={"OG3N": 95.0},
                      system_overrides={"OG3N": {"biomass": 450_000.0}})
    # Judged every year on the knobs and the limits it ran with, its cap.
    assert [j[1] for j in judged] == list(YEARS)
    assert all(j[2:] == (3_400_000.0, 3_600.0, 6) for j in judged)
    assert c.error is None and c.n_changed == 7
    assert c.first_tran_og == tr.summarize(
        *tr.propose(live, FS, FS, [300_000]))["first_changed_tran_og_date"]
    assert c.manual_events_file == "2026-08-31.yaml"
    assert ctl.max_biomass_kg == cap_before            # the input untouched
    # Untouched tank/system tables send nothing (the run stays Run forecast's).
    log.clear()
    to.run_cell((300_000, CAP), ROOT, live=live, forecast_start=FS, cutoff=FS,
                pr_path=pr, control=ctl)
    kw = log[0][3]
    assert "density_overrides" not in kw and "system_overrides" not in kw
    assert kw["overrides"] == {"max_biomass_kg": CAP}


def test_run_today_is_step_3s_today_arm(monkeypatch, pr):
    log = []
    rs, g = _fake_engine(log)
    monkeypatch.setattr(ie, "run_schedule", rs)
    monkeypatch.setattr(ie, "gates", g)
    ctl = load_config(str(ROOT / "config"))[0]
    live = _live()
    t = to.run_today(live, ROOT, pr_path=pr, method="controller",
                     method_overrides={"chronic_pressure_weeks": 6},
                     control=ctl)
    (_r, batches, _root, kw), *judged = log
    assert batches == live                         # the live schedule, as is
    assert kw == dict(pr_path=str(pr), horizon_weeks=208,
                      include_manual_events=True, method="controller",
                      method_overrides={"chronic_pressure_weeks": 6})
    assert all(j[2] == ctl.max_biomass_kg for j in judged)   # current limits
    assert t.today and t.error is None and t.cap_kg == ctl.max_biomass_kg


def test_an_engine_error_is_recorded_on_its_cell_and_never_within(monkeypatch,
                                                                   pr):
    log = []
    rs, g = _fake_engine(log, fail_size=260_000)
    monkeypatch.setattr(ie, "run_schedule", rs)
    monkeypatch.setattr(ie, "gates", g)
    res = to.optimize_transition(_live(), FS, FS, [240_000, 260_000], [CAP],
                                 pr, ceiling_kg=CAP, project_dir=ROOT,
                                 workers=1,
                                 control=load_config(str(ROOT / "config"))[0])
    bad = [c for c in res.cells if c.batch_size == 260_000][0]
    assert bad.error.startswith("RuntimeError: the engine wrote no workbook")
    assert not bad.within_limits and bad.verdict is None
    assert res.best.batch_size == 240_000
    assert bad not in [c for c, _n, _ok in res.stability]


def test_the_runners_are_picklable_for_the_process_pool():
    for fn in (to.run_cell, to.run_today):
        assert pickle.loads(pickle.dumps(fn)) is fn
    assert pickle.loads(pickle.dumps(_cell(300_000))) == _cell(300_000)


# --- the pool wave: ONE implementation, every runner ---------------------------
# transition_optimize has no pool of its own: both its waves run through
# ideal_optimize._run_wave, each job naming its runner (today's plan beside the
# grid cells). Every runner shape is driven through the SAME pool faults and
# must behave identically — one writer, so a fix cannot miss a copy.

def test_both_transition_waves_run_through_ideal_optimizes_one_wave(
        monkeypatch, pr):
    assert not hasattr(to, "_run_wave")                  # the copy is gone
    waves, real = [], io._run_wave

    def spy(jobs, *a, **k):
        waves.append([j[0] for j in jobs])
        return real(jobs, *a, **k)
    monkeypatch.setattr(io, "_run_wave", spy)
    _optimize(monkeypatch, pr, [], [240_000, 260_000, 280_000], [CAP],
              within=lambda s, c: s <= 260_000 or s == 285_000)
    assert waves == [[to.run_today] + [to.run_cell] * 3,   # today + the grid
                     [to.run_cell] * 4]                     # the stability runs


def _pool_cell(cell, project_dir, **kw):
    """A TOP-LEVEL fake runner (transition_optimize.run_cell's shape): a real
    process pool pickles it by its qualified name."""
    return cell, project_dir, kw, os.getpid()


def _pool_step2(rhythm, cap_kg, project_dir, **kw):
    """A TOP-LEVEL fake runner in ideal_optimize.run_cell's shape."""
    return rhythm, cap_kg, project_dir, kw, os.getpid()


def test_a_real_two_worker_pool_runs_top_level_runners():
    sizes = (1_000, 2_000, 3_000)
    rec = {}
    note = io._run_wave([(_pool_cell, ((s, CAP), "pd"), {"x": 1})
                         for s in sizes], "unused", {}, 2, rec.__setitem__,
                        "the grid")
    assert note is None                                  # no fallback
    assert [rec[i][:3] for i in range(3)] == [((s, CAP), "pd", {"x": 1})
                                               for s in sizes]
    assert os.getpid() not in {r[3] for r in rec.values()}   # in the workers
    rec.clear()
    note = io._run_wave([(7, s, CAP) for s in sizes], "pd", {"x": 1}, 2,
                        rec.__setitem__, "the grid", runner=_pool_step2)
    assert note is None
    assert [rec[i][:4] for i in range(3)] == [((7, s), CAP, "pd", {"x": 1})
                                               for s in sizes]
    assert os.getpid() not in {r[4] for r in rec.values()}
    # The transition's REAL runners, mixed in one real pool as its first wave
    # is: each refused before any engine run (a control with no cap, a size
    # of 0), the error recorded on its cell in the worker.
    rec.clear()
    note = io._run_wave(
        [(to.run_today, ([], "pd"), dict(pr_path="x", control=object())),
         (to.run_cell, ((0, CAP), "pd"),
          dict(live=[], forecast_start=FS, cutoff=FS, pr_path="x",
               control=object()))],
        "unused", {}, 2, rec.__setitem__, "the grid")
    assert note is None
    assert rec[0].today and rec[0].error.startswith("AttributeError")
    assert rec[1].batch_size == 0 and rec[1].error.startswith("ValueError")


class _Stop(BaseException):
    """What a Streamlit rerun/stop raised from the progress callback is."""


def _fake_pool(log, fault):
    state = {"in_pool": False}

    class Pool:
        """Runs each job AT submit into a real Future and logs what the wave
        asks of it: start, each shutdown's (wait, cancel_futures)."""
        def __init__(self, max_workers):
            if fault.get("start"):
                raise OSError("no processes")
            log.append(("start", max_workers))
            self.n = 0

        def submit(self, fn, *a, **k):
            i, self.n = self.n, self.n + 1
            if fault.get("pickle") == i:
                raise PicklingError("cannot pickle it")
            f = Future()
            if fault.get("die") == i:
                f.set_exception(BrokenProcessPool("a worker died"))
                return f
            state["in_pool"] = True
            try:
                f.set_result(fn(*a, **k))
            finally:
                state["in_pool"] = False
            return f

        def shutdown(self, wait=True, cancel_futures=False):
            log.append(("shutdown", wait, cancel_futures))
    return Pool, state


# Each runner shape ideal_optimize._run_wave takes, and the runner each job
# must run through ("today" for the first job of the mixed wave).
RUNNERS = {"default": "step2",  # step 2's (cadence, size, cap) jobs: run_cell
           "runner": "step2",   # the same jobs with `runner` passed
           "cell": "cell",      # (fn, args, kwargs): transition run_cell
           "today": "today",    # (fn, args, kwargs): transition run_today
           "mixed": "cell"}     # today's plan first, then the grid cells


def _wave_trace(which, fault, workers, n, monkeypatch):
    """One wave of `n` jobs through ideal_optimize._run_wave with one of
    RUNNERS -> everything observable (each call tagged with its runner)."""
    log, calls, rec = [], [], []
    pool, state = _fake_pool(log, fault)
    monkeypatch.setattr(io, "ProcessPoolExecutor", pool)
    # Submit order, so which job the pool died on is deterministic.
    monkeypatch.setattr(io, "as_completed", lambda fs: list(fs))
    sizes = [1_000 * (i + 1) for i in range(n)]

    def record(i, cell):
        rec.append((i, cell))
        if fault.get("stop") == len(rec):
            raise _Stop()

    def ran(who, size, cap, project_dir, kw):
        calls.append(("pool" if state["in_pool"] else "inline", who, size,
                      cap, project_dir, kw))
        return size

    def step2(rhythm, cap, pd, **kw):         # ideal_optimize.run_cell's shape
        return ran("step2", rhythm[1], cap, pd, kw)

    def cell(c, pd, **kw):                    # transition run_cell's shape
        return ran("cell", c[0], c[1], pd, kw)

    def today(c, pd, **kw):                   # run_today's (live, project_dir)
        return ran("today", c[0], c[1], pd, kw)

    def never(*a, **k):
        raise AssertionError("run_cell ran, not the job's own runner")

    grid = [(7, s, CAP) for s in sizes]
    if which == "default":
        monkeypatch.setattr(io, "run_cell", step2)
        go = (lambda: io._run_wave(grid, "pd", {"x": 1}, workers, record,
                                   "the grid"))
    elif which == "runner":
        monkeypatch.setattr(io, "run_cell", never)       # the runner given wins
        go = (lambda: io._run_wave(grid, "pd", {"x": 1}, workers, record,
                                   "the grid", runner=step2))
    else:
        monkeypatch.setattr(io, "run_cell", never)
        fns = ([today] + [cell] * (n - 1) if which == "mixed"
               else [{"cell": cell, "today": today}[which]] * n)
        # project_dir, kw and runner are the job's own: the wave's go unused.
        go = (lambda: io._run_wave([(fn, ((s, CAP), "pd"), {"x": 1})
                                    for fn, s in zip(fns, sizes)], "unused",
                                   {"unused": True}, workers, record,
                                   "the grid", runner=never))
    try:
        out = ("note", go())
    except _Stop:
        out = ("stopped",)
    return out, log, calls, rec


@pytest.mark.parametrize("fault, workers, n, outcome, shutdowns", [
    ({}, 1, 3, ("note", None), []),                         # one at a time
    ({}, 4, 1, ("note", None), []),                         # one job
    ({}, 4, 3, ("note", None), [("shutdown", True, False)]),
    ({"start": True}, 4, 3, ("note", "Parallel run unavailable (OSError: no "
                             "processes) — ran the grid one at a time "
                             "instead."), []),
    ({"die": 1}, 4, 4, ("note", "Parallel run unavailable (BrokenProcessPool:"
                        " a worker died) — ran the remaining 3 of the grid one"
                        " at a time instead."), [("shutdown", True, True)]),
    ({"pickle": 0}, 4, 3, ("note", "Parallel run unavailable (PicklingError: "
                           "cannot pickle it) — ran the remaining 3 of the "
                           "grid one at a time instead."),
     [("shutdown", True, True)]),
    ({"stop": 2}, 4, 4, ("stopped",), [("shutdown", False, True)]),
    ({"stop": 1}, 1, 3, ("stopped",), []),
])
def test_one_pool_wave_runs_every_runner_through_the_same_faults(
        monkeypatch, fault, workers, n, outcome, shutdowns):
    traces = {w: _wave_trace(w, fault, workers, n, monkeypatch)
              for w in RUNNERS}

    def untagged(t):
        out, log, calls, rec = t
        return out, log, [c[:1] + c[2:] for c in calls], rec
    base = untagged(traces["default"])
    for which, (out, log, calls, rec) in traces.items():
        assert untagged((out, log, calls, rec)) == base   # identical behaviour
        # Each job ran through its own runner (today's plan is job 0).
        assert all(c[1] == ("today" if which == "mixed" and c[2] == 1_000
                            else RUNNERS[which]) for c in calls)
        assert out == outcome
        assert [e for e in log if e[0] == "shutdown"] == shutdowns
        # Every job gets the same cap, project dir and keywords, wherever it
        # ran.
        assert all(c[3:] == (CAP, "pd", {"x": 1}) for c in calls)
        if out[0] == "note":           # every job recorded exactly once
            assert sorted(i for i, _c in rec) == list(range(n))
            assert [c for _i, c in sorted(rec)] == [1_000 * (i + 1)
                                                    for i in range(n)]
        else:                          # stopped: nothing runs after the stop
            assert len(rec) == fault["stop"]
            assert not [c for c in calls if c[0] == "inline"] or workers < 2
