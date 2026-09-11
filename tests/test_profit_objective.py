"""The Profit objective in step 2's Ideal optimizer, and cost / profit shown
(never ranked on) in step 3's (2026-09-11, additive; operator ruling the same
day: "Show profit, don't rank by it" for the transition).

EVERY PRICE HERE IS MADE UP. The operator's real costs never appear in a
committed file: the costs below are built in the test, keyed by the model
feed-type NAMES of config/biology.yaml (names are not prices), and
config/costs.yaml is never read.

Behaviour only, no engine (ideal_engine.ideal_run / gates and the transition
runners are faked):
  * "profit" is an objective, and it is refused with ValueError BEFORE
    anything runs when costs are not set or a feed type has no price;
  * run_cell / judge_cell price the year(s) through forecast.costs.year_cost:
    profit = revenue - cost, the parts add up;
  * ranking by profit picks the cheaper of two equal-revenue plans;
  * the other objectives pick exactly the same with or without costs, and a
    costs-free search sends run_cell exactly the arguments it always did;
  * every worker gets ONE validated snapshot;
  * a cell that ran but could not be priced is an error cell under Profit
    only, never ranked on a cost of 0;
  * step 3 (the transition) ranks by revenue / HOG / gain only: "profit" is
    refused before anything runs, costs or no costs, and says why; with
    costs every cell still carries cost and profit for display, and a cell
    that cannot be priced ranks as it ran.
"""
import dataclasses
import datetime as dt
import pickle
from pathlib import Path
from types import SimpleNamespace

import pytest

from forecast import costs as C
from forecast import ideal_engine as ie
from forecast import ideal_optimize as io
from forecast import transition_optimize as to
from forecast.config_io import load_config
from forecast.models import BatchInput

ROOT = Path(__file__).resolve().parents[1]
CAP = 3_800_000.0
NAMES = io.feed_type_names(ROOT)
LO, HI = NAMES[0], NAMES[-1]
COST_FIELDS = ("cost", "profit", "costs_applied", "cost_parts",
               "unpriced_feed_kg", "cost_error")


def _costs(drop=()):
    """Made-up costs pricing every model feed type except `drop`."""
    return {"schema": 1, "fixed_monthly": 1000.0, "oxygen_per_kg_feed": 0.5,
            "chemicals_per_kg_feed": 0.25, "feed_shipping_per_kg": 0.125,
            "egg_price": 0.01,
            "feed_prices": {n: {"item": f"Made-up item {i}",
                                "price_per_kg": 1.0 + i}
                            for i, n in enumerate(NAMES) if n not in drop}}


def _price(name):
    return 1.0 + NAMES.index(name)


def _first_week(year, weeks):
    """A part year's first ISO week the way a run reads it: the run's first
    year holds its LAST weeks, any later year its first ones (FIRST_YEAR)."""
    n = dt.date(year, 12, 28).isocalendar()[1]
    w = n - weeks + 1 if year == FIRST_YEAR else 1
    return f"{year}-W{w:02d}"


def _hand_cost(feed, eggs, year, weeks, costs=None):
    """The cash-view cost worked by hand from the made-up numbers. The fixed
    cost is counted day by day over the calendar days of the year's weeks
    (each day 1 / its month's length of a month)."""
    import calendar
    c = costs or _costs()
    kg = sum(feed.values())
    fw = _first_week(year, weeks)
    monday = dt.date.fromisocalendar(year, int(fw[6:]), 1)
    months = sum(1.0 / calendar.monthrange(d.year, d.month)[1]
                 for d in (monday + dt.timedelta(days=i)
                           for i in range(7 * weeks)))
    return (sum(v * _price(n) for n, v in feed.items())
            + kg * (c["feed_shipping_per_kg"] + c["oxygen_per_kg_feed"]
                    + c["chemicals_per_kg_feed"])
            + eggs * c["egg_price"]
            + c["fixed_monthly"] * months)


def _read(year=2029, weeks=52, revenue=1e8, feed=None, eggs=100_000.0,
          drivers=True, **counts):
    base = dict(r8_over_tank_weeks=0, sys_bio_over_weeks=0,
                sys_feed_over_weeks=0, weeks_over_move_budget=0,
                under_floor_weeks=0, zero_weeks=0, over_cap_weeks=0)
    base.update(counts)
    return SimpleNamespace(
        year=year, weeks=weeks, revenue=revenue, hog_t=4_000.0,
        gain_t=4_000.0, avg_gross_kg=4.2, peak_pct_of_cap=0.8,
        feed_kg_by_type=(dict(feed or {LO: 1_000.0, HI: 50_000.0})
                         if drivers else None),
        eggs=eggs if drivers else None,
        first_week=_first_week(year, weeks), **base)


def _engine(log, *, revenue=lambda size, cap: float(size),
            feed=lambda size: {LO: 1_000.0, HI: 50_000.0},
            drivers=lambda size: True, within=lambda cad, size, cap: True):
    """ie.ideal_run / ie.gates stand-ins: the REAL run_cell path, no engine."""
    def ideal_run(cad, size, cap, root, **kw):
        log.append(("run", cad, size, cap, kw))
        y = _read(revenue=revenue(size, cap), feed=feed(size),
                  eggs=float(size) * 1.5, drivers=drivers(size))
        return SimpleNamespace(years={2029: y}, key=(cad, size, cap))

    def gates(run, year, control):
        ok = within(*run.key)
        return [ie.Gate("Biomass cap", "PASS" if ok else "FAIL", "x")]
    return ideal_run, gates


def _optimize(monkeypatch, log, sizes, costs, objective="revenue", **eng):
    run, judge = _engine(log, **eng)
    monkeypatch.setattr(ie, "ideal_run", run)
    monkeypatch.setattr(ie, "gates", judge)
    return io.optimize([49], sizes, CAP, ROOT, workers=1, objective=objective,
                       control=SimpleNamespace(max_biomass_kg=CAP),
                       costs=costs)


def _no_cost(cell):
    return dataclasses.replace(cell, **{
        f.name: f.default if f.default is not dataclasses.MISSING
        else f.default_factory() for f in dataclasses.fields(cell)
        if f.name in COST_FIELDS})


# --- the objective -------------------------------------------------------------

def test_profit_is_an_objective_named_profit():
    assert io.OBJECTIVES["profit"] == ("profit", "Profit")
    c = io.Cell(cadence_days=49, batch_size=200_000, fish_per_week=1.0,
                cap_kg=CAP, profit=-5.0, costs_applied=True)
    assert io.objective_value(c, "profit") == -5.0


# --- refused before anything runs -----------------------------------------------

def _count_runs(monkeypatch):
    ran = []
    monkeypatch.setattr(io, "run_cell", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(ie, "ideal_run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(ie, "run_schedule", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(to, "run_cell", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(to, "run_today", lambda *a, **k: ran.append(a))
    return ran


@pytest.mark.parametrize("costs, words", [
    (None, "needs config/costs.yaml"),
    (_costs(drop=(HI,)), f"no price for {HI}"),
])
def test_step2_profit_is_refused_before_anything_runs(monkeypatch, costs,
                                                      words):
    ran = _count_runs(monkeypatch)
    with pytest.raises(ValueError, match=words):
        io.optimize([49], [200_000], CAP, ROOT, objective="profit",
                    workers=1, costs=costs,
                    control=SimpleNamespace(max_biomass_kg=CAP))
    assert not ran


def test_malformed_costs_are_refused_for_any_objective(monkeypatch):
    ran = _count_runs(monkeypatch)
    bad = dict(_costs(), egg_price=-1.0)
    for obj in ("revenue", "profit"):
        with pytest.raises(ValueError, match="egg_price"):
            io.optimize([49], [200_000], CAP, ROOT, objective=obj, workers=1,
                        costs=bad, control=SimpleNamespace(max_biomass_kg=CAP))
    assert not ran


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


FS = dt.date(2026, 9, 1)


@pytest.fixture
def pr(tmp_path):
    p = tmp_path / "pr.xlsm"
    p.write_bytes(b"not read: the runners are faked")
    return p


@pytest.mark.parametrize("costs", [None, _costs(drop=(LO,)), _costs()],
                         ids=["no costs", "a feed unpriced", "complete"])
def test_step3_never_ranks_by_profit_refused_before_anything_runs(
        monkeypatch, pr, costs):
    """Show profit, don't rank by it (operator, 2026-09-11): refused with
    complete costs too — the reason is the run-window cash view, not
    missing costs — and the message says why and where profit ranks."""
    ran = _count_runs(monkeypatch)
    with pytest.raises(ValueError, match="does not rank by profit") as e:
        to.optimize_transition(_live(), FS, FS, [300_000], [CAP], pr,
                               ceiling_kg=CAP, project_dir=ROOT, workers=1,
                               control=object(), objective="profit",
                               costs=costs)
    assert str(e.value) == to.PROFIT_NOT_RANKED
    assert "favours smaller future batches" in str(e.value)
    assert "step 2" in str(e.value)
    assert not ran


def test_step3_objectives_are_step2s_minus_profit():
    assert to.TR_OBJECTIVES == {k: v for k, v in io.OBJECTIVES.items()
                                if k != "profit"}
    assert list(to.TR_OBJECTIVES) == ["revenue", "hog", "gain"]
    assert io.OBJECTIVES["profit"] == ("profit", "Profit")   # step 2 keeps it


# --- step 2: a cell's cost ----------------------------------------------------

def test_run_cell_prices_its_year_through_year_cost(monkeypatch):
    log = []
    run, judge = _engine(log)
    monkeypatch.setattr(ie, "ideal_run", run)
    monkeypatch.setattr(ie, "gates", judge)
    snap = C.validate_costs(_costs())
    c = io.run_cell((49, 200_000), CAP, ROOT, costs=snap,
                    control=SimpleNamespace(max_biomass_kg=CAP))
    want = _hand_cost({LO: 1_000.0, HI: 50_000.0}, 300_000.0, 2029, 52)
    assert c.costs_applied and c.cost_error is None
    assert c.cost == pytest.approx(want)
    assert c.profit == pytest.approx(c.revenue - want)
    p = c.cost_parts
    assert p["total"] == pytest.approx(
        p["feed"] + p["shipping"] + p["oxygen"] + p["chemicals"] + p["eggs"]
        + p["fixed"])
    # 52 ISO weeks of 2029 = Mon 01-01 .. Sun 12-30: 11 months + 30/31.
    assert p["fixed"] == pytest.approx(1000.0 * (11 + 30 / 31))
    assert c.unpriced_feed_kg == 0.0
    # The same YearRead through forecast.costs directly: one cost function.
    y = run(49, 200_000, CAP, ROOT).years[2029]
    assert c.cost == pytest.approx(C.year_cost(y, snap)["total"])
    # Without costs nothing is priced and nothing is claimed.
    plain = io.run_cell((49, 200_000), CAP, ROOT,
                        control=SimpleNamespace(max_biomass_kg=CAP))
    assert not plain.costs_applied and plain.cost == 0.0
    assert plain.cost_error is None
    assert _no_cost(c) == dataclasses.replace(plain, elapsed_s=c.elapsed_s)


def test_a_read_without_drivers_records_why_never_a_zero_cost(monkeypatch):
    log = []
    run, judge = _engine(log, drivers=lambda size: False)
    monkeypatch.setattr(ie, "ideal_run", run)
    monkeypatch.setattr(ie, "gates", judge)
    c = io.run_cell((49, 200_000), CAP, ROOT, costs=C.validate_costs(_costs()),
                    control=SimpleNamespace(max_biomass_kg=CAP))
    assert not c.costs_applied and c.cost == 0.0
    assert "without cost drivers" in c.cost_error
    assert c.error is None and c.within_limits      # the RUN is fine


def test_a_priced_cell_pickles_for_the_process_pool(monkeypatch):
    log = []
    run, judge = _engine(log)
    monkeypatch.setattr(ie, "ideal_run", run)
    monkeypatch.setattr(ie, "gates", judge)
    c = io.run_cell((49, 200_000), CAP, ROOT, costs=C.validate_costs(_costs()),
                    control=SimpleNamespace(max_biomass_kg=CAP))
    assert pickle.loads(pickle.dumps(c)) == c


# --- step 2: ranking ------------------------------------------------------------

def test_profit_picks_the_cheaper_of_two_equal_revenue_rhythms(monkeypatch):
    # Equal revenue; the SMALLER batch feeds more (costs more). Revenue's
    # tie-break takes the smaller batch; profit takes the cheaper one.
    kw = dict(revenue=lambda size, cap: 5e7,
              feed=lambda size: ({LO: 1_000.0, HI: 90_000.0}
                                 if size == 200_000
                                 else {LO: 1_000.0, HI: 40_000.0}))
    rev = _optimize(monkeypatch, [], [200_000, 210_000], _costs(), **kw)
    pro = _optimize(monkeypatch, [], [200_000, 210_000], _costs(),
                    objective="profit", **kw)
    assert rev.best.batch_size == 200_000
    assert pro.best.batch_size == 210_000
    assert pro.best.profit > next(c for c in pro.cells
                                  if c.batch_size == 200_000).profit
    assert [c.batch_size for c in io.table_order(pro.cells, "profit")] == [
        210_000, 200_000]


@pytest.mark.parametrize("objective", ["revenue", "hog", "gain"])
def test_other_objectives_pick_the_same_with_or_without_costs(monkeypatch,
                                                             objective):
    kw = dict(revenue=lambda size, cap: float(size) + (4e6 - cap),
              within=lambda cad, size, cap: size <= 210_000,
              feed=lambda size: {LO: size / 100.0, HI: size / 4.0})
    sizes = [190_000, 200_000, 210_000, 220_000]
    log_a, log_b = [], []
    a = _optimize(monkeypatch, log_a, sizes, None, objective, **kw)
    b = _optimize(monkeypatch, log_b, sizes, _costs(), objective, **kw)
    assert b.best.key == a.best.key
    assert (b.closest and b.closest.key) == (a.closest and a.closest.key)
    assert b.unconstrained.key == a.unconstrained.key
    assert [c.key for c in io.table_order(b.cells, objective)] == [
        c.key for c in io.table_order(a.cells, objective)]
    assert [(c.key, [n.key for n in ns], ok) for c, ns, ok in b.stability] == [
        (c.key, [n.key for n in ns], ok) for c, ns, ok in a.stability]
    assert all(c.costs_applied for c in b.cells)
    assert not any(c.costs_applied for c in a.cells)
    # Every field but the cost fields is identical, cell by cell.
    strip = lambda c: dataclasses.replace(_no_cost(c), elapsed_s=0.0)
    assert [strip(c) for c in b.cells] == [strip(c) for c in a.cells]
    # A costs-free search sends run_cell exactly what it always did.
    assert all("costs" not in x[4] for x in log_a)


def test_every_worker_is_priced_from_one_validated_snapshot(monkeypatch):
    seen = []
    real = io._run_wave

    def spy(jobs, project_dir, kw, workers, record, what, runner=None):
        seen.append(kw)
        return real(jobs, project_dir, kw, workers, record, what, runner)
    monkeypatch.setattr(io, "_run_wave", spy)
    costs = _costs()
    res = _optimize(monkeypatch, [], [200_000, 210_000], costs,
                    objective="profit")
    assert len(seen) == 2                           # the grid, the wave
    assert seen[0]["costs"] is seen[1]["costs"]     # ONE snapshot
    assert seen[0]["costs"] == C.validate_costs(costs)
    assert seen[0]["costs"] is not costs            # a copy, not the caller's
    assert res.best is not None


def test_under_profit_an_unpriced_cell_is_an_error_never_a_zero(monkeypatch):
    # 210k's read has no cost drivers: it cannot be priced.
    kw = dict(drivers=lambda size: size != 210_000,
              revenue=lambda size, cap: float(size) * 1_000.0)
    pro = _optimize(monkeypatch, [], [200_000, 210_000], _costs(),
                    objective="profit", **kw)
    bad = next(c for c in pro.cells if c.batch_size == 210_000)
    assert io.is_cost_error(bad) and not bad.within_limits
    assert "without cost drivers" in bad.error
    assert pro.best.batch_size == 200_000
    assert io.table_order(pro.cells, "profit")[-1].batch_size == 210_000
    # Under revenue the same cell ranks as it ran, with its cost_error kept.
    rev = _optimize(monkeypatch, [], [200_000, 210_000], _costs(), **kw)
    r210 = next(c for c in rev.cells if c.batch_size == 210_000)
    assert r210.error is None and r210.cost_error and rev.best is r210


def test_an_unpriced_cell_keeps_its_limits_verdict_for_display(monkeypatch):
    """require_price makes an unpriced cell an error under Profit (never
    ranked), but the page shows the limits verdict it RAN with, and an
    engine error is never within the limits (ran_within_limits)."""
    kw = dict(drivers=lambda size: size != 210_000,
              within=lambda cad, size, cap: size == 210_000)
    pro = _optimize(monkeypatch, [], [200_000, 210_000], _costs(),
                    objective="profit", **kw)
    bad = next(c for c in pro.cells if c.batch_size == 210_000)
    ok = next(c for c in pro.cells if c.batch_size == 200_000)
    assert io.is_cost_error(bad) and not bad.within_limits
    assert io.ran_within_limits(bad)              # it ran within the limits
    assert not io.ran_within_limits(ok)           # breaks them, priced
    assert pro.best is None and pro.closest.key == ok.key
    engine = dataclasses.replace(ok, error="RuntimeError: boom")
    assert not io.ran_within_limits(engine)
    # A TrCell: its two-window verdict.
    snap = C.validate_costs(_costs())
    today = to.judge_today(to.TrCell(batch_size=0, cap_kg=CAP, today=True,
                                     reads=_reads(),
                                     gates_by_year=_gates_by_year()),
                           costs=snap)
    cell = to.judge_cell(to.TrCell(
        batch_size=300_000, cap_kg=CAP,
        reads={y: SimpleNamespace(**dict(vars(r), feed_kg_by_type=None))
               for y, r in _reads().items()},
        gates_by_year=_gates_by_year(), n_changed=3,
        first_tran_og=dt.date(2027, 9, 23)), today, costs=snap)
    tc = io.require_price(cell, "profit")
    assert io.is_cost_error(tc) and not tc.within_limits
    assert io.ran_within_limits(tc) == cell.verdict.within_limits is True


def test_require_price_names_unpriced_feed():
    c = io.Cell(cadence_days=49, batch_size=200_000, fish_per_week=1.0,
                cap_kg=CAP, within_limits=True, costs_applied=True,
                unpriced_feed_kg=1234.0,
                cost_parts={"unpriced_types": ["Mystery 3.0"]})
    got = io.require_price(c, "profit")
    assert got.error == ("CostError: 1,234 kg of feed has no price "
                         "(Mystery 3.0)") and not got.within_limits
    assert io.require_price(c, "revenue") is c
    err = dataclasses.replace(c, error="RuntimeError: boom")
    assert io.require_price(err, "profit") is err
    assert not io.is_cost_error(err)


# --- step 3: the transition -----------------------------------------------------

YEARS = (2026, 2027, 2028, 2029, 2030)
FIRST_YEAR = YEARS[0]              # a run's first year holds its last weeks
WEEKS = {2026: 17, 2027: 52, 2028: 52, 2029: 52, 2030: 35}


def _reads(feed_hi=50_000.0, revenue=1e8, years=YEARS, counts=None):
    """{year: a read with cost drivers}; `counts` = {year: {count: n}}."""
    counts = counts or {}
    return {y: _read(year=y, weeks=WEEKS.get(y, 52), revenue=revenue,
                     feed={LO: 1_000.0, HI: feed_hi}, eggs=50_000.0,
                     **counts.get(y, {}))
            for y in years}


def _gates_by_year(years=YEARS):
    return {y: () for y in years}


def test_judge_cell_prices_the_years_both_runs_cover():
    snap = C.validate_costs(_costs())
    today = to.judge_today(to.TrCell(batch_size=0, cap_kg=CAP, today=True,
                                     reads=_reads(),
                                     gates_by_year=_gates_by_year()),
                           costs=snap)
    cell = to.TrCell(batch_size=300_000, cap_kg=CAP,
                     reads=_reads(feed_hi=40_000.0,
                                  years=YEARS + (2031,)),
                     gates_by_year=_gates_by_year(YEARS + (2031,)),
                     n_changed=3, first_tran_og=dt.date(2027, 9, 23))
    c = to.judge_cell(cell, today, costs=snap)
    assert c.years == YEARS                          # 2031 is not compared
    want = sum(_hand_cost({LO: 1_000.0, HI: 40_000.0}, 50_000.0, y, WEEKS[y])
               for y in YEARS)
    assert c.costs_applied and c.cost == pytest.approx(want)
    assert c.profit == pytest.approx(c.revenue - want)
    assert c.cost == pytest.approx(sum(
        C.year_cost(cell.reads[y], snap)["total"] for y in YEARS))
    assert today.costs_applied and today.cost > c.cost   # it feeds more
    # Without costs the same judgement, nothing priced.
    plain = to.judge_cell(cell, to.judge_today(dataclasses.replace(
        today, cost=0.0, profit=0.0, costs_applied=False, cost_parts={},
        unpriced_feed_kg=0.0)))
    assert not plain.costs_applied
    assert _no_cost(c) == plain


def _runners(within=lambda size, cap: True, feed=lambda size: 50_000.0):
    def fake_today(live, project_dir, **kw):
        return to.TrCell(batch_size=0, cap_kg=CAP, today=True,
                         reads=_reads(), gates_by_year=_gates_by_year())

    def fake_cell(cell, project_dir, **kw):
        size, cap = cell
        ok = within(size, cap)
        return to.TrCell(
            batch_size=size, cap_kg=float(cap),
            reads=_reads(feed_hi=feed(size), revenue=1e8,
                         counts=None if ok else {2029: dict(
                             r8_over_tank_weeks=2)}),
            gates_by_year=_gates_by_year(), n_changed=3,
            first_tran_og=dt.date(2027, 9, 23))
    return fake_today, fake_cell


def _tr_optimize(monkeypatch, pr, sizes, costs, objective="revenue", **kw):
    ft, fc = _runners(**kw)
    monkeypatch.setattr(to, "run_today", ft)
    monkeypatch.setattr(to, "run_cell", fc)
    ctl = load_config(str(ROOT / "config"))[0]
    return to.optimize_transition(_live(), FS, FS, sizes, [CAP], pr,
                                  ceiling_kg=CAP, project_dir=ROOT, workers=1,
                                  control=ctl, objective=objective,
                                  costs=costs)


def test_step3_shows_cost_and_profit_on_every_cell_but_never_ranks_on_them(
        monkeypatch, pr):
    # Equal revenue everywhere; 280k feeds least, so it has the highest
    # profit — and is NOT picked: revenue's tie-break takes the smaller.
    # (the stability wave's +/-5,000 neighbours feed a middling amount)
    feed = lambda size: {260_000: 60_000.0, 280_000: 30_000.0,
                         300_000: 45_000.0}.get(size, 50_000.0)
    sizes = [260_000, 280_000, 300_000]
    res = _tr_optimize(monkeypatch, pr, sizes, _costs(), feed=feed)
    assert res.best.batch_size == 260_000          # the tie-break: smaller
    most = max(res.cells, key=lambda c: c.profit)
    assert most.batch_size == 280_000 and most.key != res.best.key
    for c in res.cells + (res.today,):             # display: every cell
        assert c.costs_applied and c.cost_error is None
        assert c.profit == pytest.approx(c.revenue - c.cost)
    assert all(n.costs_applied for _c, ns, _ok in res.stability for n in ns)
    # No costs: nothing priced and nothing claimed (the page shows blanks),
    # and the same pick.
    plain = _tr_optimize(monkeypatch, pr, sizes, None, feed=feed)
    assert plain.best.key == res.best.key
    assert not plain.today.costs_applied
    assert not any(c.costs_applied or c.cost_error for c in plain.cells)


@pytest.mark.parametrize("objective", ["revenue", "hog", "gain"])
def test_step3_other_objectives_pick_the_same_with_or_without_costs(
        monkeypatch, pr, objective):
    kw = dict(within=lambda size, cap: size <= 300_000,
              feed=lambda size: size / 5.0)
    sizes = [260_000, 280_000, 300_000, 320_000]
    a = _tr_optimize(monkeypatch, pr, sizes, None, objective, **kw)
    b = _tr_optimize(monkeypatch, pr, sizes, _costs(), objective, **kw)
    assert b.best.key == a.best.key
    assert b.unconstrained.key == a.unconstrained.key
    assert [c.key for c in io.table_order(b.cells, objective)] == [
        c.key for c in io.table_order(a.cells, objective)]
    assert [(c.key, ok) for c, _ns, ok in b.stability] == [
        (c.key, ok) for c, _ns, ok in a.stability]
    assert all(c.costs_applied for c in b.cells) and b.today.costs_applied
    assert not any(c.costs_applied for c in a.cells)
    assert [_no_cost(c) for c in b.cells] == [_no_cost(c) for c in a.cells]


def test_step3_a_cell_that_cannot_be_priced_ranks_as_it_ran(monkeypatch, pr):
    """No step-3 objective reads the cost fields: a cell whose reads carry
    no cost drivers keeps its cost_error for display and ranks as it ran
    (here it earns the most, so it wins) — never an error cell."""
    ft, fc = _runners()

    def cell_no_drivers(cell, project_dir, **kw):
        c = fc(cell, project_dir, **kw)
        if cell[0] == 300_000:        # no cost drivers, and earns the most
            c = dataclasses.replace(c, reads={
                y: SimpleNamespace(**dict(vars(r), feed_kg_by_type=None,
                                          revenue=2e8))
                for y, r in c.reads.items()})
        return c
    monkeypatch.setattr(to, "run_today", ft)
    monkeypatch.setattr(to, "run_cell", cell_no_drivers)
    ctl = load_config(str(ROOT / "config"))[0]
    res = to.optimize_transition(_live(), FS, FS, [280_000, 300_000], [CAP],
                                 pr, ceiling_kg=CAP, project_dir=ROOT,
                                 workers=1, control=ctl, objective="revenue",
                                 costs=_costs())
    bad = next(c for c in res.cells if c.batch_size == 300_000)
    assert bad.error is None and not io.is_cost_error(bad)
    assert not bad.costs_applied and "without cost drivers" in bad.cost_error
    assert bad.within_limits and res.best.key == bad.key
    ok = next(c for c in res.cells if c.batch_size == 280_000)
    assert ok.costs_applied and ok.cost_error is None


def test_cost_fields_sum_the_years_and_name_unpriced_types_once():
    snap = C.validate_costs(_costs(drop=(HI,)))
    reads = [_read(year=y, weeks=WEEKS[y]) for y in (2027, 2028)]
    f = io.cost_fields(reads, 3e6, snap)
    parts = [C.year_cost(y, snap) for y in reads]
    assert f["cost"] == pytest.approx(sum(p["total"] for p in parts))
    assert f["profit"] == pytest.approx(3e6 - f["cost"])
    assert f["unpriced_feed_kg"] == pytest.approx(100_000.0)
    assert f["cost_parts"]["unpriced_types"] == [HI]
    assert io.cost_fields(reads, 3e6, None) == {}
