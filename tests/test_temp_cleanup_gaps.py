"""Gaps an independent mutation run found in tests/test_temp_cleanup.py.

Each test here is a promise of forecast.temp_cleanup that a single-behaviour
mutant broke while every test in test_temp_cleanup.py still passed
(2026-09-12):

- age is the folder's own MODIFIED time — not its access time;
- the prefix must START the name ("backup_as_forecast_x" is not the app's);
- a rename a virus scanner holds for a moment is retried, not skipped;
- a delete that leaves the folder behind is not counted, and remove_tree
  says so (False);
- a done-marker left by ANOTHER process does not stop this one's sweep;
- when the session's referenced folders cannot be read, nothing is removed
  (a mutant swept with nothing kept);
- a Path object, or a path inside a list or tuple, keeps its folder;
- app.py hands the clean-up the session's REAL state (a mutant passed {}):
  driven through the real app in AppTest on a fake temp dir;
- no temp delete of the app swallows its errors, however ignore_errors=True
  is spelled (a positional True slipped past the line scan).

Every test runs on a FAKE temp dir; the suite never sweeps the real one.
"""
import ast
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"
sys.path.insert(0, str(ROOT))
DAY = 86400.0
_REAL_TEMP = tempfile.gettempdir()   # read at import, before a test fakes it


@pytest.fixture
def short_tmp(tmp_path):
    """tmp_path — or, when it is long (a deep --basetemp), a fresh folder in
    the real temp dir: Windows refuses a path over 260 characters (WinError
    206), and 5 of these tests failed under a scratchpad basetemp (review
    2026-09-12). Its name carries no app prefix, so no sweep touches it."""
    if len(str(tmp_path)) <= 80 or len(_REAL_TEMP) >= len(str(tmp_path)):
        yield tmp_path
        return
    d = Path(tempfile.mkdtemp(prefix="tcl_", dir=_REAL_TEMP))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def tc(monkeypatch, short_tmp):
    """(the module, a fake temp dir that tempfile.gettempdir() returns)."""
    from forecast import temp_cleanup
    fake = short_tmp / "Temp"
    fake.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(fake))
    monkeypatch.delenv(temp_cleanup.OFF_ENV, raising=False)
    monkeypatch.delenv(temp_cleanup.DONE_ENV, raising=False)
    assert Path(tempfile.gettempdir()) == fake
    return temp_cleanup, fake


def _times(p, atime_days, mtime_days):
    now = time.time()
    os.utime(p, (now - atime_days * DAY, now - mtime_days * DAY))


def make(root, name, age_days, size=1000):
    d = Path(root) / name
    (d / "sub").mkdir(parents=True)
    (d / "a.txt").write_bytes(b"x" * size)
    (d / "sub" / "b.txt").write_bytes(b"y" * size)
    _times(d / "sub", age_days, age_days)
    _times(d, age_days, age_days)
    return d


def test_age_is_the_modified_time_not_the_access_time(tc):
    t, fake = tc
    old = make(fake, "as_forecast_old_mtime", 1)
    _times(old, 0, 5)                   # written 5 days ago, read just now
    young = make(fake, "as_forecast_young_mtime", 1)
    _times(young, 5, 0.5)               # written 12 h ago, atime 5 days ago
    n, _b = t.sweep()
    assert not old.exists(), "a folder written 5 days ago must go"
    assert young.exists(), "a folder written 12 h ago must stay"
    assert n == 1


def test_a_name_that_only_contains_a_prefix_stays(tc):
    t, fake = tc
    kept = [make(fake, "backup_as_forecast_x", 30),
            make(fake, "my.ideal_engine_1", 30),
            make(fake, "xas_run_2", 30)]
    assert t.sweep() == (0, 0)
    for d in kept:
        assert d.exists() and (d / "a.txt").exists(), d
        assert t.remove_if_old(d) is False and d.exists(), d


def test_a_brief_hold_on_the_rename_is_retried(tc, monkeypatch):
    t, fake = tc
    held = make(fake, "as_forecast_held", 5)
    real_rename, refused = os.rename, []

    def rename(src, dst, *a, **kw):
        if Path(src).name == held.name and len(refused) < 2:
            refused.append(src)         # a virus scanner, for ~2 tries
            raise PermissionError(13, "Access is denied", str(src))
        return real_rename(src, dst, *a, **kw)
    monkeypatch.setattr(os, "rename", rename)
    n, nbytes = t.sweep()
    assert len(refused) == 2
    assert not held.exists() and (n, nbytes) == (1, 2000)


def test_a_delete_that_leaves_the_folder_is_not_counted(tc, monkeypatch):
    t, fake = tc
    make(fake, "as_forecast_stuck", 5)
    monkeypatch.setattr(t, "remove_tree", lambda p: False)
    assert t.sweep() == (0, 0)
    lines = []
    t.run_once(log=lines.append)
    assert lines == ["temp clean-up: removed 0 folders (0.0 MB) older than "
                     "2 days"]


def test_remove_tree_says_false_when_the_folder_stays(tmp_path, monkeypatch):
    from forecast import temp_cleanup as t
    d = tmp_path / "stays"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "f.txt").write_text("x")
    if os.name == "nt":                 # a real one: a file still open
        fh = open(d / "sub" / "f.txt", "rb")
        try:
            assert t.remove_tree(d) is False
        finally:
            fh.close()
        assert d.exists()
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **kw: None)
    assert t.remove_tree(d) is False and d.exists()


def test_a_marker_left_by_another_process_does_not_stop_the_sweep(
        tc, monkeypatch):
    t, fake = tc
    d = make(fake, "as_forecast_a", 3)
    monkeypatch.setenv(t.DONE_ENV, str(os.getpid() + 1))   # inherited
    lines = []
    got = t.run_once(log=lines.append)
    assert got is not None and got[0] == 1 and not d.exists()
    assert os.environ[t.DONE_ENV] == str(os.getpid()) and len(lines) == 1


def test_when_the_referenced_folders_cannot_be_read_nothing_goes(tc):
    t, fake = tc
    a = make(fake, "as_forecast_a", 5)
    b = make(fake, "ideal_engine_b", 5)

    def keep():
        raise RuntimeError("session state unreadable")
    lines = []
    got = t.run_once(keep=keep, log=lines.append)       # must not raise
    assert a.exists() and b.exists(), (
        "with the session's folders unknown, a sweep could remove one it "
        "still reads")
    assert got == (0, 0) and lines == [
        "temp clean-up: removed 0 folders (0.0 MB) older than 2 days"]


def test_paths_as_path_objects_and_in_lists_keep_their_folders(tc):
    t, fake = tc
    by_path = make(fake, "as_forecast_by_path", 30)
    in_list = make(fake, "as_forecast_in_list", 30)
    in_tuple = make(fake, "as_forecast_in_tuple", 30)
    gone = make(fake, "as_forecast_gone", 30)
    state = {"a": {"output_path": by_path / "out_planned.xlsm"},
             "b": [1, str(in_list / "x.xlsm")],
             "c": {"runs": ({"work": str(in_tuple)},)}}
    keep = t.referenced_folders(state)
    assert keep == {by_path.name, in_list.name, in_tuple.name}
    assert t.sweep(keep=keep)[0] == 1
    assert by_path.exists() and in_list.exists() and in_tuple.exists()
    assert not gone.exists()


_APP_DRIVER = r'''
import sys
sys.path.insert(0, %(root)r)
try:
    from streamlit.testing.v1 import AppTest
except Exception as e:
    print("SKIP no AppTest: %%s" %% e)
    raise SystemExit(0)
import tempfile
assert tempfile.gettempdir() == %(fake)r, tempfile.gettempdir()
at = AppTest.from_file(%(app)r, default_timeout=600)
# A result this session still shows, the way the Run page keeps one.
at.session_state["zz_probe_result"] = {"ok": True,
                                       "output_path": %(out)r}
at.run()
print("RAN exception=%%r" %% bool(at.exception))
'''


def test_the_app_keeps_the_folders_its_session_points_into(short_tmp):
    """The real app, first page load, clean-up ON, on a fake temp dir: an
    old app folder the session's result points into stays, an old one
    nothing points into goes, and one line is logged."""
    fake = short_tmp / "Temp"
    fake.mkdir()
    ref = make(fake, "as_forecast_ref", 5)
    gone = make(fake, "as_forecast_gone", 5)
    env = {k: v for k, v in os.environ.items()
           if k not in ("AS_TEMP_CLEANUP_OFF", "AS_TEMP_CLEANUP_PID")}
    env.update(TEMP=str(fake), TMP=str(fake), TMPDIR=str(fake),
               PYTHONDONTWRITEBYTECODE="1")
    src = _APP_DRIVER % {"root": str(ROOT), "app": str(APP),
                         "fake": str(fake),
                         "out": str(ref / "out_planned.xlsm")}
    p = subprocess.run([sys.executable, "-c", src], capture_output=True,
                       text=True, timeout=900, cwd=str(ROOT), env=env)
    out = p.stdout or ""
    if "SKIP" in out:
        pytest.skip(out.strip().splitlines()[-1])
    assert p.returncode == 0 and "RAN" in out, (out + p.stderr)[-2000:]
    assert ref.exists() and (ref / "a.txt").exists(), (
        "the app removed a folder its session's result points into")
    assert not gone.exists(), f"the app did not sweep at start:\n{out[-1500:]}"
    logged = [ln for ln in out.splitlines() if ln.startswith("temp clean-up:")]
    assert logged == ["temp clean-up: removed 1 folders (0.0 MB) older than "
                      "2 days"], out[-1500:]


def test_no_temp_delete_swallows_its_errors_however_spelled():
    """rmtree(..., ignore_errors=True) — or rmtree(path, True) — left a
    read-only config copy behind without a word; the app's own code deletes
    through remove_tree (a positional True slipped past a line scan)."""
    offenders = []
    for p in [APP] + sorted((ROOT / "forecast").glob("*.py")):
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if not (isinstance(n, ast.Call) and "rmtree" in (
                    getattr(n.func, "attr", None), getattr(n.func, "id", None))):
                continue
            kw = {k.arg: k.value for k in n.keywords}
            ign = kw.get("ignore_errors")
            if ign is None and len(n.args) >= 2:
                ign = n.args[1]
            if ign is not None and not (isinstance(ign, ast.Constant)
                                        and ign.value is False):
                offenders.append(f"{p.name}:{n.lineno}: {ast.unparse(n)}")
    assert not offenders, offenders
