"""The Ideal optimizer's opt-in cell cache.

A cell is a 156-week engine run and a sweep repeats most of the previous one;
2026-09-19 re-ran the same cells across five sweeps. The cache is OPT-IN
(cache_path=None is exactly the old behaviour) and lives on the Ideal path
only -- the PR-driven Run forecast never touches it.

The dangerous half is staleness: the grading-CV and 6N commits of 2026-09-20
both moved plan numbers without touching cadence, size or cap, so an entry is
keyed by a fingerprint of the engine modules, the config/scenario files, the
knobs and the horizon.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from forecast import ideal_optimize as io
from forecast.ideal_engine import Gate

ROOT = Path(__file__).resolve().parent.parent


def _cell(cad=49, size=200_000, ok=True, cap=3_800_000.0, hog=4203.0):
    return io.Cell(cadence_days=cad, batch_size=size,
                   fish_per_week=size * 7.0 / cad, cap_kg=cap, year=2029,
                   revenue=42.8e6, hog_t=hog, gain_t=hog * 1.2,
                   avg_gross_kg=3.65, peak_pct_of_cap=0.58,
                   breaches={} if ok else {"density": 7}, total=0 if ok else 7,
                   gates=(Gate(name="density", status="PASS", detail="none"),
                          Gate(name="floor", status="WARN", detail="1 week")),
                   within_limits=ok, elapsed_s=21.0)


def _fake(calls):
    def fake(rhythm, cap_kg, project_dir, **kw):
        cad, size = rhythm
        calls.append((cad, size, cap_kg))
        return _cell(cad, size, ok=(size <= 210_000), cap=cap_kg,
                     hog=float(size) / 40.0)
    return fake


# --------------------------------------------------------------------------- #
# round-trip
# --------------------------------------------------------------------------- #
def test_a_cell_survives_the_round_trip_with_its_gates():
    """A cached cell that lost its gates would report a plan as ungated rather
    than as it ran."""
    c = _cell(ok=False)
    back = io._cell_from_record(json.loads(json.dumps(io._cell_to_record(c))))
    assert back == c
    assert [(g.name, g.status, g.detail) for g in back.gates] == \
           [("density", "PASS", "none"), ("floor", "WARN", "1 week")]
    assert back.within_limits is False and back.breaches == {"density": 7}


def test_the_what_if_knobs_are_part_of_the_key():
    """density_overrides and system_overrides are exactly what an Ideal what-if
    moves; leaving them out would let one what-if answer for another."""
    a = io._cache_knobs({"density_overrides": {"OG3N": 95.0}})
    b = io._cache_knobs({"density_overrides": {"OG3N": 120.0}})
    c = io._cache_knobs({"system_overrides": {"OG3N": {"biomass": 450_000.0}}})
    assert a != b and a != c and a != {}
    assert io._cache_knobs({}) == {}
    assert io._cache_knobs({"overrides": {}}) == {}


# --------------------------------------------------------------------------- #
# behaviour through optimize()
# --------------------------------------------------------------------------- #
def test_without_a_cache_path_nothing_changes(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(io, "run_cell", _fake(calls))
    res = io.optimize([49], [200_000, 220_000], 3_800_000, ROOT, workers=1)
    assert len(res.cells) == 2 and len(calls) >= 2
    assert not list(tmp_path.glob("*.jsonl"))


def test_a_warm_sweep_runs_nothing_and_returns_every_cell(monkeypatch, tmp_path):
    """The bug this pins: with every cell cached the job list is empty, and
    sizing the result from it returned ZERO cells."""
    cache = tmp_path / "cells.jsonl"
    calls = []
    monkeypatch.setattr(io, "run_cell", _fake(calls))
    first = io.optimize([49], [200_000, 220_000], 3_800_000, ROOT, workers=1,
                        cache_path=cache)
    n_cold = len(calls)
    calls.clear()
    warm = io.optimize([49], [200_000, 220_000], 3_800_000, ROOT, workers=1,
                       cache_path=cache)
    assert len(warm.cells) == len(first.cells) == 2, "a warm sweep lost cells"
    assert len(calls) < n_cold, "the warm sweep re-ran the grid"
    assert sorted((c.cadence_days, c.batch_size, c.hog_t) for c in warm.cells) \
        == sorted((c.cadence_days, c.batch_size, c.hog_t) for c in first.cells)


def test_the_progress_callback_still_counts_cached_cells(monkeypatch, tmp_path):
    """A cached cell must tick progress, or the app's bar stalls at 0."""
    cache = tmp_path / "cells.jsonl"
    monkeypatch.setattr(io, "run_cell", _fake([]))
    io.optimize([49], [200_000], 3_800_000, ROOT, workers=1, cache_path=cache)
    seen = []
    io.optimize([49], [200_000], 3_800_000, ROOT, workers=1, cache_path=cache,
                progress=lambda d, n, c: seen.append((d, n)))
    assert seen and seen[-1][0] == seen[-1][1], "progress never reached total"


def test_a_different_horizon_does_not_answer_from_the_cache(monkeypatch, tmp_path):
    """104-week screening and 156-week full runs are different measurements."""
    cache = tmp_path / "cells.jsonl"
    calls = []
    monkeypatch.setattr(io, "run_cell", _fake(calls))
    io.optimize([49], [200_000], 3_800_000, ROOT, workers=1, cache_path=cache)
    calls.clear()
    io.optimize([49], [200_000], 3_800_000, ROOT, workers=1, cache_path=cache,
                horizon_weeks=104)
    assert calls, "a 104-week sweep reused a 156-week verdict"


def test_different_knobs_do_not_answer_from_the_cache(monkeypatch, tmp_path):
    cache = tmp_path / "cells.jsonl"
    calls = []
    monkeypatch.setattr(io, "run_cell", _fake(calls))
    io.optimize([49], [200_000], 3_800_000, ROOT, workers=1, cache_path=cache,
                density_overrides={"OG3N": 95.0})
    calls.clear()
    io.optimize([49], [200_000], 3_800_000, ROOT, workers=1, cache_path=cache,
                density_overrides={"OG3N": 120.0})
    assert calls, "a different density what-if reused the first one's verdict"


def test_an_engine_error_is_never_cached(monkeypatch, tmp_path):
    """An error is about this run, not about the cell."""
    cache = tmp_path / "cells.jsonl"

    def boom(rhythm, cap_kg, project_dir, **kw):
        c = _cell(*rhythm, cap=cap_kg)
        return io.Cell(**{**c.__dict__, "error": "RuntimeError: nope"})
    monkeypatch.setattr(io, "run_cell", boom)
    io.optimize([49], [200_000], 3_800_000, ROOT, workers=1, cache_path=cache)
    from forecast.ideal_cache import CellCache
    assert len(CellCache(cache).load()) == 0
