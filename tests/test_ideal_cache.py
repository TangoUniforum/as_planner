"""The Ideal cell cache — and the staleness it exists to prevent.

A cell is a 156-week engine run, and 2026-09-19 re-ran the same cells across
five sweeps. The cost is the easy half. The dangerous half is a cached verdict
outliving the code that produced it: the grading-CV change measured that same
day flipped 42 d x 225k from "zero breach" to two breach-weeks without touching
cadence, size or cap.
"""
from __future__ import annotations

import json

import pytest

from forecast.ideal_cache import (CellCache, cell_key, engine_fingerprint,
                                  inputs_fingerprint)

BASE = dict(horizon_weeks=156, method="controller", knobs={"hybrid_follow": "off"},
            engine="eng1", inputs="in1")


def test_the_same_cell_is_the_same_key():
    a = cell_key(49, 200_000, 3.8e6, **BASE)
    b = cell_key(49, 200_000, 3.8e6, **BASE)
    assert a == b


@pytest.mark.parametrize("change", [
    {"cadence_days": 48}, {"batch_size": 210_000}, {"cap_kg": 3.0e6},
])
def test_a_different_cell_is_a_different_key(change):
    args = dict(cadence_days=49, batch_size=200_000, cap_kg=3.8e6)
    args.update(change)
    assert cell_key(**args, **BASE) != cell_key(49, 200_000, 3.8e6, **BASE)


def test_a_screening_run_never_answers_for_a_full_one():
    """104-week and 156-week runs are different measurements: the short run
    ranks cells well (Spearman 0.99) but agreed with the full run's LEGAL/not
    verdict on only 4 of 10 cells. They must never share an entry."""
    full = cell_key(49, 200_000, 3.8e6, **BASE)
    screen = cell_key(49, 200_000, 3.8e6, **{**BASE, "horizon_weeks": 104})
    assert full != screen


def test_new_engine_code_invalidates_the_verdict():
    """THE POINT OF THE FINGERPRINT. Same cell, changed engine -> a miss, not a
    stale 'legal'."""
    old = cell_key(42, 225_000, 3.0e6, **BASE)
    new = cell_key(42, 225_000, 3.0e6, **{**BASE, "engine": "eng2"})
    assert old != new


def test_changed_config_invalidates_the_verdict():
    old = cell_key(42, 225_000, 3.0e6, **BASE)
    new = cell_key(42, 225_000, 3.0e6, **{**BASE, "inputs": "in2"})
    assert old != new


def test_the_same_knobs_in_a_different_order_are_one_key():
    k1 = cell_key(49, 200_000, 3.8e6, **{**BASE, "knobs": {"a": 1, "b": 2}})
    k2 = cell_key(49, 200_000, 3.8e6, **{**BASE, "knobs": {"b": 2, "a": 1}})
    assert k1 == k2


def test_different_knobs_are_different_keys():
    k1 = cell_key(49, 200_000, 3.8e6, **{**BASE, "knobs": {"a": 1}})
    k2 = cell_key(49, 200_000, 3.8e6, **{**BASE, "knobs": {"a": 2}})
    assert k1 != k2


def test_it_remembers_across_processes(tmp_path):
    p = tmp_path / "cells.jsonl"
    CellCache(p).put("k1", {"hog_t": 4203.0, "legal": True})
    assert CellCache(p).get("k1") == {"hog_t": 4203.0, "legal": True}
    assert CellCache(p).get("never-seen") is None


def test_a_torn_line_never_breaks_a_search(tmp_path):
    """A half-written line (a killed run) must cost one entry, not the file."""
    p = tmp_path / "cells.jsonl"
    c = CellCache(p)
    c.put("k1", {"hog_t": 1.0})
    with open(p, "a", encoding="utf-8") as fh:
        fh.write('{"key": "k2", "value": {"ho\n')          # torn
    c.put("k3", {"hog_t": 3.0})
    fresh = CellCache(p).load()
    assert fresh.get("k1") == {"hog_t": 1.0}
    assert fresh.get("k3") == {"hog_t": 3.0}
    assert fresh.get("k2") is None


def test_compact_keeps_the_last_value_per_key(tmp_path):
    p = tmp_path / "cells.jsonl"
    c = CellCache(p)
    c.put("k1", {"v": 1})
    c.put("k1", {"v": 2})
    c.put("k2", {"v": 9})
    assert c.compact() == 2
    lines = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2
    assert CellCache(p).load().get("k1") == {"v": 2}


def test_the_engine_fingerprint_moves_when_engine_code_moves(tmp_path):
    from forecast import ideal_cache as ic
    d = tmp_path / "pkg"
    d.mkdir()
    for f in ic.ENGINE_FILES:
        (d / f).write_text("x = 1\n", encoding="utf-8")
    first = ic.engine_fingerprint(d)
    assert ic.engine_fingerprint(d) == first          # stable
    (d / "placement.py").write_text("x = 2\n", encoding="utf-8")
    assert ic.engine_fingerprint(d) != first          # and sensitive


def test_reporting_changes_do_not_invalidate_thousands_of_cells():
    """excel_io decides how a run is REPORTED, not what it plans. It is
    deliberately outside the fingerprint."""
    from forecast import ideal_cache as ic
    assert "excel_io.py" not in ic.ENGINE_FILES
    assert "placement.py" in ic.ENGINE_FILES


def test_the_cache_key_never_reads_costs():
    """No Ideal verdict depends on costs.yaml, and hashing it would put cost
    data into the cache key."""
    from forecast import ideal_cache as ic
    assert not any("cost" in f for f in ic.CONFIG_FILES + ic.SCENARIO_FILES)


def test_the_real_fingerprints_run_on_the_real_tree():
    """Smoke: both fingerprints compute against the shipped package/config."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    e = engine_fingerprint()
    i = inputs_fingerprint(root / "config", root / "scenario")
    assert len(e) == 16 and len(i) == 16 and e != i
