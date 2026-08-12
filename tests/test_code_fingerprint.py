"""A cached result's identity includes the CODE that produced it.

Negative controls for the 2026-08-12 stale-board incident: the Global engines
were rebuilt over two days while config/ and scenario/ stayed byte-identical,
so the input fingerprint never moved and four of five board legs replayed
pre-repair plans as current. Inputs alone are not a result's identity.
"""
from pathlib import Path

from forecast.analysis import code_fingerprint


def _engine(tmp_path: Path) -> Path:
    d = tmp_path / "engine"
    (d / "sub").mkdir(parents=True)
    (d / "planner.py").write_text("def plan():\n    return 1\n")
    (d / "sub" / "pick.py").write_text("CAP = 95\n")
    return d


def test_same_source_same_fingerprint(tmp_path):
    d = _engine(tmp_path)
    assert code_fingerprint((d,)) == code_fingerprint((d,))


def test_changed_source_changes_the_fingerprint(tmp_path):
    """THE control: the incident in one assertion — an engine edit with no
    input change must make cached results non-reusable."""
    d = _engine(tmp_path)
    before = code_fingerprint((d,))
    (d / "planner.py").write_text("def plan():\n    return 2\n")
    assert code_fingerprint((d,)) != before


def test_a_nested_module_counts(tmp_path):
    d = _engine(tmp_path)
    before = code_fingerprint((d,))
    (d / "sub" / "pick.py").write_text("CAP = 100\n")
    assert code_fingerprint((d,)) != before


def test_a_new_module_counts(tmp_path):
    d = _engine(tmp_path)
    before = code_fingerprint((d,))
    (d / "extra.py").write_text("X = 1\n")
    assert code_fingerprint((d,)) != before


def test_pycache_is_ignored(tmp_path):
    """.pyc content carries build metadata; including it would churn the
    fingerprint every run and force needless re-runs."""
    d = _engine(tmp_path)
    before = code_fingerprint((d,))
    pyc = d / "__pycache__"
    pyc.mkdir()
    (pyc / "planner.cpython-311.pyc").write_bytes(b"\x00\x01compiled")
    (pyc / "planner.py").write_text("this file is inside __pycache__\n")
    assert code_fingerprint((d,)) == before


def test_non_python_files_are_ignored(tmp_path):
    """Config content has its own fingerprint; this one is about code."""
    d = _engine(tmp_path)
    before = code_fingerprint((d,))
    (d / "notes.txt").write_text("hello\n")
    assert code_fingerprint((d,)) == before


def test_missing_directory_is_tolerated(tmp_path):
    d = _engine(tmp_path)
    assert code_fingerprint((d, tmp_path / "nope")) == code_fingerprint((d,))


def test_real_engine_is_hashable_and_stable():
    """The live tree: non-empty, and stable across two reads."""
    root = Path(__file__).resolve().parent.parent
    a = code_fingerprint((root / "forecast", root / "tools"))
    b = code_fingerprint((root / "forecast", root / "tools"))
    assert a == b and len(a) == 32
