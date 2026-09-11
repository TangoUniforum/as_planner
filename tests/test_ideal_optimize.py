"""forecast.ideal_optimize — the best rhythm within EVERY limit.

Behavioural invariants, never pinned numbers. Ranking, tie-break and the
closest cell on synthetic cells; bad grids refused before anything runs; an
engine error recorded on its cell and never feasible; the strict gates on a
real run's YearRead (each limit alone takes a plan out of the limits, the FW
note alone does not); and ONE small real grid (2 cells) end to end, whose
cells are judged exactly as ideal_engine.plausible(gates) and which leaves
the live config/ and scenario/ byte-identical.

The two display-only reads: `ignoring_limits` (the cost of the limits) and
the stability check, whose decision (`judge_stability`) is tested on
synthetic cells and whose extra wave is tested with an injected runner (a grid
cell is reused, never re-run). The pool cancels, and does not wait, when the
progress callback raises (a Streamlit rerun is a BaseException).

The cap dimension: caps=None is the one cap it always was; every (cadence,
size, cap) runs once AT its own cap and is judged at it; a cap above the
operator's is refused before anything runs; ties go to the higher cap; a
candidate's neighbours keep its cap; the wave is at most 2 x 10 runs.
"""
import dataclasses
import hashlib
import math
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from types import SimpleNamespace

import pytest

from forecast import ideal
from forecast import ideal_engine as ie
from forecast import ideal_optimize as io
from forecast import scenario_io as sio
from forecast.config_io import load_config

ROOT = Path(__file__).resolve().parents[1]


def _tree_hash(d: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(d.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(d)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def _repo_hashes() -> dict:
    return {d: _tree_hash(ROOT / d) for d in ("config", "scenario")}


CAP = 3_800_000.0


def _cell(cad, size, *, ok=True, revenue=0.0, hog=0.0, gain=0.0, total=0,
          error=None, cap=CAP):
    return io.Cell(cadence_days=cad, batch_size=size,
                   fish_per_week=size * 7.0 / cad, cap_kg=float(cap), year=2029,
                   revenue=revenue, hog_t=hog, gain_t=gain,
                   breaches={k: 0 for k in io.BREACH_KEYS}, total=total,
                   within_limits=ok and error is None, error=error)


# --- ranking -----------------------------------------------------------------

def test_best_is_the_highest_objective_among_cells_within_the_limits():
    cells = [_cell(49, 200_000, revenue=89e6, hog=4_383),
             _cell(49, 280_000, ok=False, revenue=137e6, hog=6_583, total=115),
             _cell(56, 220_000, revenue=95e6, hog=4_300)]
    assert io.best(cells, "revenue").batch_size == 220_000
    assert io.best(cells, "hog").batch_size == 200_000      # objective decides
    assert io.best(cells, "revenue").within_limits


def test_ties_break_to_the_smaller_batch_then_the_longer_cadence():
    a = _cell(49, 240_000, revenue=100e6)
    b = _cell(56, 220_000, revenue=100e6)
    c = _cell(63, 220_000, revenue=100e6)
    for order in ([a, b, c], [c, b, a], [b, a, c]):
        assert io.best(order, "revenue") == c               # never grid order


def test_after_every_other_term_the_higher_cap_wins():
    """Same objective, batch and cadence: the cap closest to the operator's
    setting (the higher one) wins in best / ignoring_limits / closest /
    table_order, whatever the input order."""
    lo, mid, hi = (_cell(49, 210_000, revenue=100e6, cap=c)
                   for c in (3_000_000, 3_200_000, 3_800_000))
    bad = [dataclasses.replace(c, within_limits=False, total=3)
           for c in (lo, mid, hi)]
    for order in ([lo, mid, hi], [hi, lo, mid], [mid, hi, lo]):
        assert io.best(order, "revenue") == hi
        assert io.ignoring_limits(order, "revenue") == hi
        assert io.stability_candidates(order, "revenue") == [hi, mid, lo]
        assert io.table_order(order, "revenue") == [hi, mid, lo]
    for order in (bad, bad[::-1]):
        assert io.closest(order, "revenue") == bad[2]
        assert io.table_order(order, "revenue") == bad[::-1]
    # The cap is the LAST term: more revenue at a lower cap still wins.
    rich = _cell(49, 210_000, revenue=101e6, cap=3_000_000)
    assert io.best([hi, rich], "revenue") == rich


def test_the_label_names_the_cap():
    c = _cell(49, 210_000, cap=3_200_000)
    assert c.label == "49 d × 210,000 @ 3,200 t"
    assert c.rhythm == "49 d × 210,000" and c.cap_t == 3_200.0
    assert c.key == (49, 210_000, 3_200_000.0)


def test_closest_is_the_fewest_breaches_then_the_objective():
    cells = [_cell(49, 280_000, ok=False, revenue=137e6, total=115),
             _cell(56, 300_000, ok=False, revenue=120e6, total=12),
             _cell(63, 300_000, ok=False, revenue=125e6, total=12),
             _cell(70, 200_000, error="RuntimeError: boom")]
    assert io.best(cells, "revenue") is None
    got = io.closest(cells, "revenue")
    assert (got.cadence_days, got.total) == (63, 12)
    assert io.closest([_cell(49, 200_000, error="X: y")], "revenue") is None


def test_table_order_is_within_first_by_objective_then_breaches_errors_last():
    cells = [_cell(49, 280_000, ok=False, revenue=137e6, total=115),
             _cell(70, 200_000, error="RuntimeError: boom"),
             _cell(49, 200_000, revenue=89e6),
             _cell(56, 300_000, ok=False, revenue=120e6, total=12),
             _cell(56, 220_000, revenue=95e6)]
    got = [(c.cadence_days, c.batch_size) for c in io.table_order(cells, "revenue")]
    assert got == [(56, 220_000), (49, 200_000), (56, 300_000), (49, 280_000),
                   (70, 200_000)]


def _failing(cad, size, gate_names, *, total=0, revenue=0.0):
    """A ran cell outside the limits that FAILs the named gates."""
    return dataclasses.replace(
        _cell(cad, size, ok=False, revenue=revenue, total=total),
        gates=tuple(ie.Gate(n, "FAIL", "x") for n in gate_names))


def test_a_non_limit_fail_ranks_after_cells_that_only_break_limits():
    audit = _failing(49, 200_000, ["Conservation audits"], revenue=99e6)
    limits = _failing(56, 300_000, ["Tank density (R8)"], total=50,
                      revenue=80e6)
    within = _cell(63, 200_000, revenue=70e6)
    boom = _cell(70, 200_000, error="RuntimeError: boom")
    assert io.other_failed_checks(audit) == ("Conservation audits",)
    assert io.other_failed_checks(limits) == ()     # a limit gate is not "other"
    # 0 breaches but a failed audit is further from a plan than 50 breaches.
    assert io.closest([audit, limits, boom], "revenue") == limits
    assert io.closest([audit, boom], "revenue") == audit
    got = io.table_order([boom, audit, within, limits], "revenue")
    assert got == [within, limits, audit, boom]


def test_breach_words_are_plain_and_count_aware():
    c = dataclasses.replace(
        _cell(49, 280_000, ok=False, total=115),
        breaches=dict({k: 0 for k in io.BREACH_KEYS}, density=76, sys_feed=37,
                      sys_biomass=1, floor=1))
    assert io.breach_words(c) == ["76 tank-weeks over density",
                                  "1 system-week over biomass",
                                  "37 system-weeks over feed",
                                  "1 week under the floor"]
    assert io.breach_words(_cell(49, 200_000)) == []
    assert io.breach_words(_cell(49, 200_000, error="X: y")) == []


# --- the cost of the limits (display only) ------------------------------------

def test_ignoring_limits_picks_the_top_earner_whatever_it_breaks():
    within = _cell(49, 210_000, revenue=103.1e6)
    breach = _cell(49, 280_000, ok=False, revenue=136.8e6, total=115)
    assert io.best([within, breach], "revenue") == within
    assert io.ignoring_limits([within, breach], "revenue") == breach
    assert io.ignoring_limits([breach, within], "revenue") == breach


def test_ignoring_limits_is_best_when_the_top_earner_is_within():
    cells = [_cell(49, 210_000, revenue=103.1e6),
             _cell(49, 203_000, ok=False, revenue=96.9e6, total=1),
             _cell(42, 180_000, revenue=96.6e6)]
    assert io.ignoring_limits(cells, "revenue") == io.best(cells, "revenue")
    # Same tie-break as best: the smaller batch, then the longer cadence.
    a, b = _cell(49, 240_000, revenue=1e8), _cell(56, 220_000, revenue=1e8)
    assert io.ignoring_limits([a, b], "revenue") == b


def test_an_errored_cell_never_counts_for_ignoring_limits():
    boom = _cell(49, 400_000, revenue=999e6, error="RuntimeError: boom")
    breach = _cell(49, 280_000, ok=False, revenue=136.8e6, total=115)
    assert io.ignoring_limits([boom, breach], "revenue") == breach
    assert io.ignoring_limits([boom], "revenue") is None
    assert io.ignoring_limits([], "revenue") is None


# --- the stability decision, on synthetic cells (no engine) -------------------

def _ran(*cells):
    return {c.key: c for c in cells}


def test_the_candidates_are_the_within_cells_best_first():
    cells = [_cell(49, 200_000, revenue=89e6),
             _cell(49, 280_000, ok=False, revenue=137e6, total=115),
             _cell(49, 210_000, revenue=103e6),
             _cell(42, 180_000, revenue=96.6e6),
             _cell(56, 232_000, revenue=91.4e6),
             _cell(70, 300_000, revenue=200e6, error="RuntimeError: boom")]
    got = io.stability_candidates(cells, "revenue")
    assert [c.batch_size for c in got] == [210_000, 180_000, 232_000, 200_000]
    assert got[0] == io.best(cells, "revenue")
    assert io.stability_candidates(cells[1:2], "revenue") == []


def test_at_most_ten_candidates():
    assert io.MAX_STABILITY_CANDIDATES == 10
    cells = [_cell(49, 100_000 + 10_000 * i, revenue=float(i))
             for i in range(12)]
    got = io.stability_candidates(cells, "revenue")
    assert [c.revenue for c in got] == [float(i) for i in range(11, 1, -1)]


def test_a_stable_winner():
    w = _cell(49, 210_000, revenue=103e6)
    stab, first = io.judge_stability(
        [w], _ran(_cell(49, 205_000), _cell(49, 215_000)))
    (cand, ns, ok), = stab
    assert cand == w and ok and first == w
    assert [n.batch_size for n in ns] == [205_000, 215_000]


def test_a_fragile_winner_with_a_stable_second():
    w = _cell(49, 210_000, revenue=103e6)
    s = _cell(42, 180_000, revenue=96.6e6)
    ran = _ran(_cell(49, 205_000),
               _cell(49, 215_000, ok=False, total=17),     # breaks a limit
               _cell(42, 175_000), _cell(42, 185_000))
    stab, first = io.judge_stability([w, s], ran)
    assert [ok for _c, _n, ok in stab] == [False, True]
    assert first == s


def test_none_of_three_is_stable():
    cands = [_cell(49, 210_000), _cell(42, 180_000), _cell(56, 232_000)]
    ran = _ran(*(_cell(c.cadence_days, c.batch_size + d, ok=(d < 0), total=1)
                 for c in cands for d in (-5_000, 5_000)))
    stab, first = io.judge_stability(cands, ran)
    assert [ok for *_x, ok in stab] == [False, False, False]
    assert first is None and len(stab) == 3


def test_a_neighbour_keeps_its_candidates_cap():
    c = _cell(49, 210_000, cap=3_200_000)
    assert io.neighbour_rhythms(c) == [(49, 205_000, 3_200_000.0),
                                       (49, 215_000, 3_200_000.0)]
    # The same rhythm at ANOTHER cap is not a neighbour: judged on the cap
    # the candidate ran at, a missing one raises.
    other = _ran(_cell(49, 205_000), _cell(49, 215_000))       # at 3,800 t
    with pytest.raises(KeyError):
        io.judge_stability([c], other)
    stab, first = io.judge_stability([c], _ran(
        _cell(49, 205_000, cap=3_200_000), _cell(49, 215_000, cap=3_200_000)))
    assert stab[0][2] and first == c
    assert {n.cap_kg for n in stab[0][1]} == {3_200_000.0}


def test_a_neighbour_below_the_smallest_batch_is_dropped():
    small = _cell(49, io.MIN_SIZE + 2_000)
    assert io.neighbour_rhythms(small) == [(49, io.MIN_SIZE + 7_000, CAP)]
    assert io.neighbour_rhythms(_cell(49, io.MIN_SIZE + 5_000)) == [
        (49, io.MIN_SIZE, CAP), (49, io.MIN_SIZE + 10_000, CAP)]
    stab, first = io.judge_stability([small],
                                     _ran(_cell(49, io.MIN_SIZE + 7_000)))
    assert len(stab[0][1]) == 1 and stab[0][2] and first == small


def test_an_errored_neighbour_is_not_stable_and_a_missing_one_raises():
    w = _cell(49, 210_000)
    stab, first = io.judge_stability(
        [w], _ran(_cell(49, 205_000),
                  _cell(49, 215_000, error="RuntimeError: boom")))
    assert stab[0][2] is False and first is None
    with pytest.raises(KeyError):                   # detect, don't coerce
        io.judge_stability([w], _ran(_cell(49, 205_000)))


# --- the stability wave, with an injected runner ------------------------------

def _fake_runner(calls, within=lambda size: True, args=None, with_cap=False):
    def fake(rhythm, cap_kg, project_dir, **kw):
        cad, size = rhythm
        calls.append((cad, size, cap_kg) if with_cap else (cad, size))
        if args is not None:
            args.append((cap_kg, project_dir, kw))
        ok = within(size)
        return _cell(cad, size, ok=ok, revenue=float(size), total=0 if ok else 1,
                     cap=cap_kg)
    return fake


def test_the_stability_wave_reuses_grid_cells_and_runs_the_rest_once(
        monkeypatch):
    calls, seen, args = [], [], []
    monkeypatch.setattr(io, "run_cell",
                        _fake_runner(calls, lambda s: s <= 210_000, args))
    # Non-default everything, so a wave run with other arguments shows.
    ctl, tmpl = object(), object()
    run = dict(template=tmpl,
               overrides={"min_harvest_weight_g": 3_600.0,
                          "max_transfers_per_week": 18},
               method="controller",
               method_overrides={"chronic_pressure_weeks": 6},
               density_overrides={"OG3N": 95.0},
               system_overrides={"OG3N": {"biomass": 450_000.0}},
               horizon_weeks=120)
    res = io.optimize([49], [200_000, 205_000, 210_000], 3_800_000, ROOT,
                      workers=1, control=ctl,
                      progress=lambda d, n, c: seen.append((d, n, c.batch_size)),
                      **run)
    # The grid, then ONLY the neighbours the grid lacks, each once.
    assert calls == [(49, 200_000), (49, 205_000), (49, 210_000),
                     (49, 215_000), (49, 195_000)]
    # The stability wave runs with EXACTLY the grid's arguments.
    assert len(args) == 5
    assert all(a == (3_800_000, str(ROOT), dict(run, control=ctl))
               for a in args), args
    assert all(a[2]["control"] is ctl and a[2]["template"] is tmpl
               for a in args)
    assert seen == [(1, 3, 200_000), (2, 3, 205_000), (3, 3, 210_000),
                    (4, 5, 215_000), (5, 5, 195_000)]
    assert [c.batch_size for c in res.cells] == [200_000, 205_000, 210_000]
    assert res.best.batch_size == 210_000
    assert [(c.batch_size, ok) for c, _n, ok in res.stability] == [
        (210_000, False), (205_000, True), (200_000, True)]
    assert res.stability[1][1] == (res.cells[0], res.cells[2])   # reused cells
    assert res.best_stable == res.cells[1]
    assert res.unconstrained == res.best             # the top earner is within
    assert res.note is None


def test_caps_none_is_the_one_cap_it_always_was(monkeypatch):
    """caps=None runs at cap_kg alone: the same calls, cells and ranking as
    caps=[cap_kg], every cell at that cap, one run per rhythm."""
    calls, args = [], []
    monkeypatch.setattr(io, "run_cell",
                        _fake_runner(calls, lambda s: s <= 210_000, args))
    a = io.optimize([49], [200_000, 205_000, 210_000], 3_800_000, ROOT,
                    workers=1, control=object())
    first = list(calls)
    calls.clear()
    b = io.optimize([49], [200_000, 205_000, 210_000], 3_800_000, ROOT,
                    caps=[3_800_000], workers=1, control=object())
    assert first == calls == [(49, 200_000), (49, 205_000), (49, 210_000),
                              (49, 215_000), (49, 195_000)]
    assert a == b
    assert {c.cap_kg for c in a.cells} == {3_800_000.0}
    assert {x[0] for x in args} == {3_800_000.0}
    assert [c.batch_size for c in io.table_order(a.cells, "revenue")] == [
        210_000, 205_000, 200_000]
    assert a.best.label == "49 d × 210,000 @ 3,800 t"


def _fake_engine(log, within=lambda cad, size, cap: True):
    """ie.ideal_run / ie.gates stand-ins: the REAL run_cell path, no engine.
    Revenue rises as the cap falls, so a lower cap can win."""
    def ideal_run(cad, size, cap, root, **kw):
        log.append(("run", cad, size, cap, dict(kw["overrides"])))
        y = SimpleNamespace(
            r8_over_tank_weeks=0, sys_bio_over_weeks=0, sys_feed_over_weeks=0,
            weeks_over_move_budget=0, under_floor_weeks=0, zero_weeks=0,
            over_cap_weeks=0, revenue=float(size) + (4e6 - cap),
            hog_t=4_000.0, gain_t=4_000.0, avg_gross_kg=4.2,
            peak_pct_of_cap=0.8)
        return SimpleNamespace(years={2029: y}, key=(cad, size, cap))

    def gates(run, year, control):
        log.append(("judge",) + run.key + (control.max_biomass_kg,))
        ok = within(*run.key)
        return [ie.Gate("Biomass cap", "PASS" if ok else "FAIL", "x")]
    return ideal_run, gates


def test_a_three_cap_grid_runs_and_judges_every_cell_at_its_own_cap(
        monkeypatch):
    log = []
    run, judge = _fake_engine(log)
    monkeypatch.setattr(ie, "ideal_run", run)
    monkeypatch.setattr(ie, "gates", judge)
    ctl = load_config(str(ROOT / "config"))[0]
    caps = [3_000_000, 3_800_000, 3_200_000]           # any order in
    res = io.optimize([49], [200_000, 210_000], 3_800_000, ROOT, caps=caps,
                      workers=1, control=ctl,
                      overrides={"min_harvest_per_week": 26_000.0})
    grid = [(49, s, float(p)) for s in (200_000, 210_000)
            for p in (3_800_000, 3_200_000, 3_000_000)]  # cap high -> low
    assert [c.key for c in res.cells] == grid
    runs = [x[1:4] for x in log if x[0] == "run"]
    judged = [x for x in log if x[0] == "judge"]
    assert runs[:6] == grid                            # the grid, in order
    assert len(runs) == len(set(runs))                 # nothing runs twice
    # Every run is judged at the cap it ran at; the engine takes the cap on
    # its own (never also in `overrides`, where ideal_run would refuse it).
    assert [j[1:4] for j in judged] == runs
    assert all(j[3] == j[4] for j in judged)
    assert all("max_biomass_kg" not in x[4] for x in log if x[0] == "run")
    assert ctl.max_biomass_kg == 3_800_000.0           # the input untouched
    # The wave: each candidate's neighbours at ITS cap (205k is shared).
    assert sorted(runs[6:]) == sorted(
        (49, s, float(p)) for s in (195_000, 205_000, 215_000)
        for p in (3_800_000, 3_200_000, 3_000_000))
    assert res.best.key == (49, 210_000, 3_000_000.0)  # the lower cap earns more
    assert all(n.cap_kg == cand.cap_kg for cand, ns, _ok in res.stability
               for n in ns)


def test_a_cap_above_the_operators_is_refused_before_anything_runs(
        monkeypatch):
    ran = []
    monkeypatch.setattr(io, "run_cell", lambda *a, **k: ran.append(a))
    with pytest.raises(ValueError, match="never searches above"):
        io.optimize([49], [200_000], 3_800_000, ROOT, workers=1,
                    control=object(), caps=[3_800_000, 3_900_000])
    assert not ran


def test_a_cap_in_overrides_that_differs_from_a_cap_to_try_is_refused(
        monkeypatch):
    """The cap goes in cap_kg / caps. ideal_run refuses a cap given twice, so
    an overrides max_biomass_kg that differs from a cap to try would fail
    every such cell: the grid is refused before anything runs. The same
    value as the one cap still runs, exactly as before."""
    ran = []
    monkeypatch.setattr(io, "run_cell", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(ie, "ideal_run", lambda *a, **k: ran.append(a))
    for caps, ov_cap in (([3_800_000, 3_200_000], 3_800_000.0),
                         (None, 3_200_000)):
        with pytest.raises(ValueError, match="cap_kg / caps"):
            io.optimize([49], [200_000], 3_800_000, ROOT, workers=1,
                        control=object(), caps=caps,
                        overrides={"max_biomass_kg": ov_cap})
    assert not ran
    monkeypatch.undo()
    log = []
    run, judge = _fake_engine(log)
    monkeypatch.setattr(ie, "ideal_run", run)
    monkeypatch.setattr(ie, "gates", judge)
    ctl = load_config(str(ROOT / "config"))[0]
    res = io.optimize([49], [200_000], 3_800_000, ROOT, workers=1, control=ctl,
                      overrides={"max_biomass_kg": 3_800_000.0})
    assert [c.key for c in res.cells] == [(49, 200_000, 3_800_000.0)]
    assert all(c.error is None for c in res.cells) and res.best is not None


def test_neighbours_keep_their_cap_and_reuse_grid_cells(monkeypatch):
    calls, args = [], []
    monkeypatch.setattr(io, "run_cell",
                        _fake_runner(calls, args=args, with_cap=True))
    ctl, tmpl = object(), object()
    run = dict(template=tmpl, overrides={"min_harvest_weight_g": 3_600.0},
               method="controller",
               method_overrides={"chronic_pressure_weeks": 6},
               density_overrides={"OG3N": 95.0},
               system_overrides={"OG3N": {"biomass": 450_000.0}},
               horizon_weeks=120)
    res = io.optimize([49], [200_000, 205_000, 210_000], 3_800_000, ROOT,
                      caps=[3_200_000, 3_800_000], workers=1, control=ctl,
                      **run)
    hi, lo = 3_800_000.0, 3_200_000.0
    assert calls[:6] == [(49, s, p) for s in (200_000, 205_000, 210_000)
                         for p in (hi, lo)]
    # Only the neighbours the grid lacks, at the candidate's cap, each once.
    assert calls[6:] == [(49, 215_000, hi), (49, 215_000, lo),
                         (49, 195_000, hi), (49, 195_000, lo)]
    # The wave runs with EXACTLY the grid's arguments — its cap included.
    assert all(a[1:] == (str(ROOT), dict(run, control=ctl)) for a in args)
    assert [a[0] for a in args] == [c[2] for c in calls]
    by_key = {c.key: c for c in res.cells}
    for cand, ns, _ok in res.stability:
        assert [n.key for n in ns] == io.neighbour_rhythms(cand)
        for n in ns:
            if n.key in by_key:
                assert n is by_key[n.key]                  # reused, not re-run
    assert res.best.key == (49, 210_000, hi)           # the tie -> higher cap


def test_ten_candidates_make_a_wave_of_at_most_twenty(monkeypatch):
    calls = []
    monkeypatch.setattr(io, "run_cell", _fake_runner(calls, with_cap=True))
    cads = [42, 49, 56, 63, 70, 77]
    res = io.optimize(cads, [200_000, 300_000], 3_800_000, ROOT, workers=1,
                      control=object())
    assert len(res.cells) == 12
    wave = calls[12:]
    assert len(res.stability) == io.MAX_STABILITY_CANDIDATES == 10
    assert len(wave) == 2 * io.MAX_STABILITY_CANDIDATES == 20
    assert len(set(wave)) == 20
    assert set(wave) == {r for cand, _ns, _ok in res.stability
                         for r in io.neighbour_rhythms(cand)}


def test_no_stability_runs_without_a_winner(monkeypatch):
    calls = []
    monkeypatch.setattr(io, "run_cell", _fake_runner(calls, lambda s: False))
    res = io.optimize([49], [200_000, 220_000], 3_800_000, ROOT, workers=1,
                      control=object())
    assert calls == [(49, 200_000), (49, 220_000)]
    assert res.best is None and res.stability == () and res.best_stable is None
    assert res.unconstrained.batch_size == 220_000   # display only
    assert res.closest is not None


# --- the pool never blocks the page -------------------------------------------

class _Rerun(BaseException):
    """Stands in for Streamlit's rerun/stop (a BaseException, not Exception)."""


def _fake_pool(log, broken=False):
    class Pool:
        def __init__(self, max_workers):
            log.append(("start", max_workers))

        def submit(self, fn, *a, **k):
            f = Future()
            if broken:
                f.set_exception(BrokenProcessPool("a worker died"))
            else:
                f.set_result(fn(*a, **k))
            return f

        def shutdown(self, wait=True, cancel_futures=False):
            log.append(("shutdown", wait, cancel_futures))
    return Pool


@pytest.mark.parametrize("exc", [_Rerun, RuntimeError])
def test_the_pool_cancels_without_waiting_when_progress_raises(monkeypatch,
                                                               exc):
    log = []
    monkeypatch.setattr(io, "ProcessPoolExecutor", _fake_pool(log))
    monkeypatch.setattr(io, "run_cell", _fake_runner([]))

    def progress(done, total, cell):
        raise exc("the page reran")
    with pytest.raises(exc):
        io.optimize([49], [200_000, 220_000, 240_000], 3_800_000, ROOT,
                    workers=2, control=object(), progress=progress)
    assert log == [("start", 2), ("shutdown", False, True)]


def test_a_dead_pool_still_falls_back_one_at_a_time(monkeypatch):
    log, calls = [], []
    monkeypatch.setattr(io, "ProcessPoolExecutor", _fake_pool(log, broken=True))
    monkeypatch.setattr(io, "run_cell", _fake_runner(calls, lambda s: False))
    res = io.optimize([49], [200_000, 220_000], 3_800_000, ROOT, workers=4,
                      control=object())
    assert log == [("start", 2), ("shutdown", True, True)]
    assert calls == [(49, 200_000), (49, 220_000)]   # each run once, in order
    assert "Parallel run unavailable" in res.note
    assert [c.batch_size for c in res.cells] == [200_000, 220_000]


def test_optimize_runs_both_waves_through_run_cell_the_default(monkeypatch):
    """Step 2 passes no runner: _run_wave's default, this module's run_cell,
    runs the grid and the stability wave (transition_optimize passes its
    own)."""
    waves, fns, real = [], [], io._run_wave

    def spy(*a, **k):
        waves.append((len(a), k))
        return real(*a, **k)
    monkeypatch.setattr(io, "_run_wave", spy)
    pool = _fake_pool([])

    class Recording(pool):
        def submit(self, fn, *a, **k):
            fns.append(fn)
            return super().submit(fn, *a, **k)
    monkeypatch.setattr(io, "ProcessPoolExecutor", Recording)
    monkeypatch.setattr(io, "run_cell", _fake_runner([]))
    res = io.optimize([49], [200_000, 220_000], 3_800_000, ROOT, workers=2,
                      control=object())
    assert res.best is not None and res.stability
    assert waves == [(6, {}), (6, {})]        # grid + stability, no runner
    assert len(fns) > len(res.cells)          # both waves went to the pool
    assert all(f is io.run_cell for f in fns)


def test_an_unknown_objective_is_refused():
    with pytest.raises(ValueError):
        io.objective_value(_cell(49, 200_000), "profit")
    with pytest.raises(ValueError):
        io.optimize([49], [200_000], 3_800_000, ROOT, objective="profit")


# --- refusals, before anything runs -------------------------------------------

@pytest.mark.parametrize("cads, sizes", [
    ([], [200_000]),
    ([49], []),
    ([49], [999]),                                   # not a batch
    ([6], [200_000]),                                # more than one a week
    ([49.0], [200_000]),                             # a float is not whole days
    ([True], [200_000]),
    ([49], [200_000.5]),
    (list(range(7, 7 + 151)), [200_000]),            # 151 cells
    (list(range(7, 13)), list(range(100_000, 351_000, 10_000))),   # 6 x 26 = 156
])
def test_bad_grids_are_refused_before_anything_runs(monkeypatch, cads, sizes):
    ran = []
    monkeypatch.setattr(io, "run_cell", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(ie, "ideal_run", lambda *a, **k: ran.append(a))
    with pytest.raises(ValueError):
        io.optimize(cads, sizes, 3_800_000, ROOT, workers=1)
    assert not ran


@pytest.mark.parametrize("cap_kg, caps", [
    (3_800_000, []),                                 # no cap to try
    (3_800_000, [0]),
    (3_800_000, [-3_200_000]),
    (3_800_000, [math.nan]),
    (3_800_000, [math.inf]),
    (3_800_000, [True]),
    (3_800_000, ["3200000"]),
    (3_800_000, [3_200_000, None]),
    (3_800_000, [3_800_000 - 100_000 * i for i in range(6)]),   # 30 x 6 = 180
    (3_800_000, [3_900_000]),                        # above the operator's cap
    (math.nan, None),                                # the ceiling itself
    (0, None),
])
def test_bad_caps_are_refused_before_anything_runs(monkeypatch, cap_kg, caps):
    ran = []
    monkeypatch.setattr(io, "run_cell", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(ie, "ideal_run", lambda *a, **k: ran.append(a))
    with pytest.raises(ValueError):
        io.optimize([49], list(range(100_000, 400_000, 10_000)), cap_kg, ROOT,
                    caps=caps, workers=1, control=object())
    assert not ran


def test_the_grid_is_cadence_then_size_then_cap_high_first_duplicates_collapse():
    assert io.check_grid([56, 49, 49], [220_000, 200_000],
                         [3_200_000, 3_800_000, 3_200_000.0]) == [
        (49, 200_000, 3_800_000.0), (49, 200_000, 3_200_000.0),
        (49, 220_000, 3_800_000.0), (49, 220_000, 3_200_000.0),
        (56, 200_000, 3_800_000.0), (56, 200_000, 3_200_000.0),
        (56, 220_000, 3_800_000.0), (56, 220_000, 3_200_000.0)]
    assert len(io.check_grid(range(7, 12), range(100_000, 200_000, 10_000),
                             [3e6, 3.2e6, 3.4e6])) == 150    # the most allowed
    with pytest.raises(ValueError, match="one cap"):
        io.check_grid([49], [200_000], [])


# --- an engine error is recorded, never feasible ------------------------------

def test_an_engine_error_is_recorded_on_its_cell_and_never_within(monkeypatch):
    def boom(cad, size, cap, root, **kw):
        if size == 220_000:
            raise RuntimeError("the engine wrote no workbook")
        raise ValueError("unknown system id")
    monkeypatch.setattr(ie, "ideal_run", boom)
    seen = []
    res = io.optimize([49], [200_000, 220_000], 3_800_000, ROOT, workers=1,
                      progress=lambda d, n, c: seen.append((d, n, c.batch_size)))
    assert [c.batch_size for c in res.cells] == [200_000, 220_000]
    assert res.cells[0].error.startswith("ValueError: unknown system id")
    assert res.cells[1].error.startswith("RuntimeError: the engine wrote")
    assert not any(c.within_limits for c in res.cells)
    assert res.best is None and res.closest is None
    assert seen == [(1, 2, 200_000), (2, 2, 220_000)]
    assert res.note is None


def test_run_cell_passes_every_limit_to_the_engine(monkeypatch):
    got = {}

    def fake(cad, size, cap, root, **kw):
        got.update(kw, cad=cad, size=size, cap=cap)
        raise RuntimeError("stop here")
    monkeypatch.setattr(ie, "ideal_run", fake)
    ctl = load_config(str(ROOT / "config"))[0]
    cell = io.run_cell((56, 240_000), 4_200_000.0, ROOT,
                       overrides={"min_harvest_weight_g": 3_600.0,
                                  "max_transfers_per_week": 18},
                       method="controller",
                       method_overrides={"chronic_pressure_weeks": 6},
                       density_overrides={"OG3N": 95.0},
                       system_overrides={"OG3N": {"biomass": 450_000.0}},
                       control=ctl)
    assert (got["cad"], got["size"], got["cap"]) == (56, 240_000, 4_200_000.0)
    assert got["overrides"] == {"min_harvest_weight_g": 3_600.0,
                                "max_transfers_per_week": 18}
    assert got["method"] == "controller"
    assert got["method_overrides"] == {"chronic_pressure_weeks": 6}
    assert got["density_overrides"] == {"OG3N": 95.0}
    assert got["system_overrides"] == {"OG3N": {"biomass": 450_000.0}}
    assert cell.error and not cell.within_limits
    assert cell.fish_per_week == pytest.approx(240_000 * 7 / 56)
    assert cell.cap_kg == 4_200_000.0                # an errored cell keeps its cap
    assert cell.label == "56 d × 240,000 @ 4,200 t"


def test_the_judging_control_carries_the_knobs_then_the_runs_limits():
    ctl = load_config(str(ROOT / "config"))[0]
    before = dataclasses.asdict(ctl) if dataclasses.is_dataclass(ctl) else None
    j = io.judging_control(
        ctl, {"global_buffer_pct": 0.25, "max_transfers_per_week": 12},
        {"max_transfers_per_week": 18, "min_harvest_weight_g": 3_600.0,
         "scenario_name": "not a limit", "sixn_production_start": "2026-01-01"})
    assert j.global_buffer_pct == 0.25                    # promoted knob
    assert j.max_transfers_per_week == 18                 # the run's limit wins
    assert j.min_harvest_weight_g == 3_600.0
    assert j.sixn_production_start == ctl.sixn_production_start   # not judged
    if before is not None:
        assert dataclasses.asdict(ctl) == before          # input untouched


# --- ONE small real grid, end to end -------------------------------------------

@pytest.fixture(scope="module")
def real_grid():
    """Two real engine cells through the process pool (~20-40 s)."""
    ctl = load_config(str(ROOT / "config"))[0]
    before = _repo_hashes()
    seen = []
    res = io.optimize([56], [200_000, 240_000], 3_800_000.0, ROOT,
                      objective="hog", workers=2,
                      progress=lambda d, n, c: seen.append(d), control=ctl)
    after = _repo_hashes()
    return dict(res=res, before=before, after=after, seen=seen, control=ctl)


def test_a_real_grid_judges_every_cell_as_plausible_gates(real_grid):
    res = real_grid["res"]
    assert real_grid["after"] == real_grid["before"]    # config/ scenario/ only read
    assert [c.key for c in res.cells] == [(56, 200_000, 3_800_000.0),
                                          (56, 240_000, 3_800_000.0)]
    # The stability wave: every neighbour of every candidate, grid cells
    # reused, the rest run once through the same pool path.
    grid = {c.key for c in res.cells}
    extra = {r for cand, _ns, _ok in res.stability
             for r in io.neighbour_rhythms(cand)} - grid
    assert sorted(real_grid["seen"]) == list(range(1, 2 + len(extra) + 1))
    assert res.unconstrained == io.ignoring_limits(res.cells, "hog")
    if res.best is not None:
        assert res.stability[0][0] == res.best
        for cand, ns, ok in res.stability:
            assert [n.key for n in ns] == io.neighbour_rhythms(cand)
            for n in ns:
                assert n.error is None, n.error
                assert n.within_limits == ie.plausible(n.gates)
            assert ok == all(n.within_limits for n in ns)
        assert res.best_stable == next(
            (c for c, _ns, ok in res.stability if ok), None)
    else:
        assert res.stability == () and res.best_stable is None
    for c in res.cells:
        assert c.error is None, c.error
        assert c.year == ie.last_complete_year(ideal.STEADY_START,
                                               ideal.HORIZON_WEEKS)
        assert c.gates and c.within_limits == ie.plausible(c.gates)
        assert set(c.breaches) == set(io.BREACH_KEYS)
        assert c.total == sum(c.breaches.values())
        assert c.within_limits == (c.total == 0 and all(
            g.status != "FAIL" for g in c.gates))
        assert c.hog_t > 0 and c.revenue > 0 and c.gain_t > 0
    if res.best is not None:
        assert res.best.within_limits and res.closest is None
        assert res.best == io.best(res.cells, "hog")
    else:
        assert res.closest == io.closest(res.cells, "hog")


# --- the strict gates on a real run's YearRead --------------------------------

_ONE_LIMIT = {
    "Harvest every week": dict(zero_weeks=1),
    "Harvest floor": dict(under_floor_weeks=1),
    "Biomass cap": dict(peak_pct_of_cap=1.0 + 1e-6, over_cap_weeks=1),
    "Tank density (R8)": dict(
        r8_over_tank_weeks=1,
        r8_worst=dict(week="x", tank=1, batch="b", density=86.0, cap=85.0,
                      system="OG1N", stage="SW")),
    "System limits": dict(
        sys_bio_over_weeks=1,
        sys_worst=dict(system="OG2S", week="x", kind="biomass", value=1.2e6,
                       cap=1.0e6, unit="kg", ratio=1.2)),
    "Handling budget": dict(weeks_over_move_budget=1),
}


def _clean(cell_run_year):
    return dataclasses.replace(
        cell_run_year, zero_weeks=0, under_floor_weeks=0, peak_pct_of_cap=0.9,
        over_cap_weeks=0, r8_over_tank_weeks=0, r8_worst=None,
        sys_bio_over_weeks=0, sys_feed_over_weeks=0, sys_worst=None,
        weeks_over_move_budget=0)


@pytest.fixture(scope="module")
def one_run():
    """A short real run (60 weeks) whose YearRead the strict tests edit."""
    t = ideal.default_template(sio.load_batches(str(ROOT / "scenario")))
    stream = ideal.synthetic_stream(t, 56, 200_000, horizon_weeks=60,
                                    start=ideal.STEADY_START)
    run = ie.run_schedule(stream, ROOT, start=ideal.STEADY_START,
                          horizon_weeks=60,
                          overrides={"sixn_production_start": "2026-01-01"})
    (year, y), = run.years.items()
    a = dict(run.audits, reconciliation_flags=0, tank_continuity_flags=0,
             input_conservation=[], input_status={})
    return dataclasses.replace(run, rc=0, audits=a,
                               years={year: _clean(y)}), year


@pytest.mark.parametrize("gate_name", sorted(_ONE_LIMIT))
def test_each_limit_alone_is_not_within_the_limits(one_run, gate_name):
    run, year = one_run
    ctl = load_config(str(ROOT / "config"))[0]
    assert ie.plausible(ie.gates(run, year, ctl))
    bad = dataclasses.replace(run, years={year: dataclasses.replace(
        run.years[year], **_ONE_LIMIT[gate_name])})
    g = ie.gates(bad, year, ctl)
    assert {x.name: x.status for x in g}[gate_name] == "FAIL"
    assert not ie.plausible(g)


def test_limit_gates_are_exactly_the_engines_limit_checks(one_run):
    """LIMIT_GATES names real ideal_engine.gates; every other gate is a check
    on the run itself (or the FW note, which never FAILs)."""
    run, year = one_run
    names = [g.name for g in ie.gates(run, year,
                                      load_config(str(ROOT / "config"))[0])]
    assert set(io.LIMIT_GATES) <= set(names)
    assert set(names) - set(io.LIMIT_GATES) == {
        "Engine finished", "Conservation audits", "Input conservation",
        "FW mass balance"}


def test_the_fw_note_alone_keeps_the_plan_within_the_limits(one_run):
    run, year = one_run
    ctl = load_config(str(ROOT / "config"))[0]
    fw = dataclasses.replace(run, audits=dict(run.audits, input_conservation=[
        "*** FW MASS-BALANCE BREACH: 1 batch(es) where the FW phase does not "
        "conserve: S001 (+3.1%). ***"]))
    g = ie.gates(fw, year, ctl)
    assert {x.name: x.status for x in g}["FW mass balance"] == "WARN"
    assert ie.plausible(g)
