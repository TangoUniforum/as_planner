"""The app's own temp folders are cleaned up once per server process.

2026-09-12: the app and the forecast modules make tempfile.mkdtemp folders
(as_forecast_, ideal_engine_, as_cmp_, ...) and several are never removed —
491 as_forecast_ folders, 111 MB, on the operator's machine, some holding a
copy of config/ (which can include costs.yaml). forecast.temp_cleanup deletes,
once per server process at app start, folders directly in
tempfile.gettempdir() whose NAME starts with one of the app's prefixes and
whose mtime is older than MAX_AGE_DAYS — never a file, a link, anything
outside the temp dir, a folder the session's results reference, or a folder
something still has open (it is skipped, untouched).

Every test runs on a FAKE temp dir (tempfile.tempdir monkeypatched); the
suite itself never sweeps the real one (conftest sets OFF_ENV, so AppTest
runs of app.py do not either).
"""
import ast
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DAY = 86400.0
_REAL_TEMP = tempfile.gettempdir()   # read at import, before a test fakes it


@pytest.fixture
def short_tmp(tmp_path):
    """tmp_path — or, when it is long (a deep --basetemp), a fresh folder in
    the real temp dir. These tests nest folders four deep plus a
    '~removing' suffix, and Windows refuses a path over 260 characters
    (WinError 206): under a scratchpad basetemp 10 of these tests failed,
    one because the app's own sweep could not remove a folder that deep
    (review 2026-09-12). Its name carries no app prefix, so no sweep ever
    touches it."""
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


def _age(p, days):
    t = time.time() - days * DAY
    os.utime(p, (t, t))


def make(root, name, age_days, size=1000):
    """A folder like the app's: two files, one in a sub-folder; its own
    mtime `age_days` old (set last: writing inside moves it)."""
    d = Path(root) / name
    (d / "sub").mkdir(parents=True)
    (d / "a.txt").write_bytes(b"x" * size)
    (d / "sub" / "b.txt").write_bytes(b"y" * size)
    _age(d / "sub", age_days)
    _age(d, age_days)
    return d


def test_old_app_folders_go_and_young_ones_stay(tc):
    t, fake = tc
    old1 = make(fake, "as_forecast_abc", 3)
    old2 = make(fake, "ideal_engine_q1", 2.5)
    young = make(fake, "as_forecast_new", 1)
    edge = make(fake, "as_cmp_controller_z", 1.9)
    n, nbytes = t.sweep()
    assert not old1.exists() and not old2.exists()
    assert young.exists() and edge.exists()
    assert (young / "sub" / "b.txt").exists()
    assert (n, nbytes) == (2, 4000)
    assert t.MAX_AGE_DAYS == 2


def test_other_folders_files_and_nested_names_stay(tc):
    t, fake = tc
    other = make(fake, "someone_elses_dir", 30)
    f = fake / "as_forecast_note.txt"            # a FILE with an app prefix
    f.write_text("keep me")
    _age(f, 30)
    outer = make(fake, "not_ours", 30)
    nested = make(outer, "as_forecast_inside", 30)   # not directly in temp
    _age(outer, 30)
    gone = make(fake, "as_run_old", 30)
    assert t.sweep()[0] == 1
    assert not gone.exists()
    assert other.exists() and f.exists() and f.read_text() == "keep me"
    assert nested.exists() and (nested / "a.txt").exists()


def test_a_locked_or_unreadable_folder_is_skipped_untouched(tc, monkeypatch):
    t, fake = tc
    locked = make(fake, "as_forecast_locked", 5)
    fh = open(locked / "sub" / "b.txt", "rb")        # something still has it open
    unreadable = make(fake, "as_ana_unreadable", 5)
    ok = make(fake, "as_pr_ok", 5)
    real_rename = os.rename

    def rename(src, dst, *a, **kw):
        if Path(src).name == unreadable.name:
            raise PermissionError(13, "Access is denied", str(src))
        return real_rename(src, dst, *a, **kw)
    monkeypatch.setattr(os, "rename", rename)
    try:
        n, _b = t.sweep()                        # must not raise
    finally:
        fh.close()
    assert not ok.exists() and n == 1
    for d in (unreadable,) + ((locked,) if os.name == "nt" else ()):
        # Skipped means UNTOUCHED — not half deleted: every file still there.
        assert d.exists(), d
        assert (d / "a.txt").exists() and (d / "sub" / "b.txt").exists(), d


def test_a_delete_that_fails_is_swallowed_and_not_counted(tc, monkeypatch):
    t, fake = tc
    bad = make(fake, "as_mcheck_bad", 5)
    ok = make(fake, "as_opt_in_ok", 5)
    real = t.remove_tree

    def remove_tree(p):
        if bad.name in str(p):
            raise OSError("disk says no")
        return real(p)
    monkeypatch.setattr(t, "remove_tree", remove_tree)
    n, _b = t.sweep()
    assert n == 1 and not ok.exists()


def test_links_are_never_followed_or_removed(tc, short_tmp):
    t, fake = tc
    target = short_tmp / "outside_target"
    target.mkdir()
    (target / "precious.txt").write_text("must survive")
    made = []
    if os.name == "nt":
        import _winapi
        j = fake / "as_forecast_junction"
        _winapi.CreateJunction(str(target), str(j))
        made.append(j)
    s = fake / "as_forecast_symlink"
    try:
        os.symlink(target, s, target_is_directory=True)
        made.append(s)
    except (OSError, NotImplementedError):
        pass                                     # no symlink privilege here
    if not made:
        pytest.skip("this machine can make neither a junction nor a symlink")
    real = make(fake, "as_forecast_real", 0)
    # Everything counts as old: the link rule alone must keep the links.
    n, _b = t.sweep(now=time.time() + 30 * DAY)
    assert n == 1 and not real.exists()
    for m in made:
        assert os.path.lexists(m), m
    assert (target / "precious.txt").read_text() == "must survive"


def test_nothing_outside_the_temp_dir_is_removed(tc, short_tmp):
    t, fake = tc
    outside = make(short_tmp / "elsewhere", "as_forecast_outside", 30)
    nested = make(fake / "deeper", "as_forecast_nested", 30)
    assert t.remove_if_old(outside) is False
    assert t.remove_if_old(nested) is False      # not DIRECTLY in the temp dir
    assert t.remove_if_old(fake / ".." / fake.name / "as_forecast_missing") \
        is False
    assert outside.exists() and (outside / "a.txt").exists()
    assert nested.exists()
    inside = make(fake, "as_forecast_inside", 30)
    assert t.remove_if_old(inside) is True and not inside.exists()


def test_a_folder_the_session_references_stays(tc):
    t, fake = tc
    ref = make(fake, "as_forecast_ref", 30)
    gone = make(fake, "as_forecast_gone", 30)
    state = {"result": {"ok": True, "output_path": str(ref / "out_planned.xlsm"),
                        "output_bytes": b"not a path"},
             "_board_store": {"controller": {"res": {"output_path":
                                                     Path(ref) / "x.xlsm"}}},
             "elsewhere": [str(Path("C:/not/in/temp/as_forecast_zzz"))],
             "n": 3}
    keep = t.referenced_folders(state)
    assert keep == {ref.name}
    n, _b = t.sweep(keep=keep)
    assert n == 1 and ref.exists() and not gone.exists()


def test_it_runs_once_per_process_and_logs_one_line(tc):
    t, fake = tc
    make(fake, "as_forecast_a", 3, size=2 * 1024 * 1024)
    make(fake, "as_tmpl_b", 3)
    lines = []
    got = t.run_once(keep=lambda: set(), log=lines.append)
    assert got[0] == 2
    assert len(lines) == 1, lines
    assert re.fullmatch(r"temp clean-up: removed 2 folders \(4\.0 MB\) older "
                        r"than 2 days", lines[0]), lines[0]
    later = make(fake, "as_forecast_later", 3)
    assert t.run_once(log=lines.append) is None   # not again in this process
    assert later.exists() and len(lines) == 1
    assert os.environ[t.DONE_ENV] == str(os.getpid())


def test_it_does_nothing_when_switched_off(tc, monkeypatch):
    t, fake = tc
    d = make(fake, "as_forecast_a", 3)
    monkeypatch.setenv(t.OFF_ENV, "1")
    lines = []
    assert t.run_once(log=lines.append) is None
    assert d.exists() and lines == []


def test_a_crash_in_the_sweep_never_reaches_the_app(tc, monkeypatch):
    t, fake = tc

    def boom(*a, **kw):
        raise RuntimeError("scandir exploded")
    monkeypatch.setattr(t, "sweep", boom)
    lines = []
    got = t.run_once(log=lines.append)           # must not raise
    assert got == (0, 0) and len(lines) == 1 and "removed 0 folders" in lines[0]


def test_remove_tree_clears_read_only_files(tmp_path):
    from forecast import temp_cleanup as t
    d = tmp_path / "ro"
    (d / "sub").mkdir(parents=True)
    f = d / "sub" / "locked.yaml"
    f.write_text("x")
    os.chmod(f, stat.S_IREAD)
    if os.name == "nt":
        os.chmod(d / "sub", stat.S_IREAD)       # the ReadOnly attribute
        # Why it is needed: the plain delete the app used leaves it behind.
        shutil.rmtree(d, ignore_errors=True)
        assert f.exists()
    assert t.remove_tree(d) is True and not d.exists()
    assert t.remove_tree(tmp_path / "never_existed") is True


# --------------------------------------------------------------------------- #
# The list of prefixes, the call at app start, and the robust deletes
# --------------------------------------------------------------------------- #
def _created_prefixes():
    """{prefix: [files]} for every mkdtemp(prefix=...) call in app.py and
    the forecast package (the app's own code) — the literal part of an
    f-string prefix — and the calls with no literal prefix."""
    found, bare = {}, []
    for p in [ROOT / "app.py"] + sorted((ROOT / "forecast").glob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and "mkdtemp" in (
                    getattr(n.func, "attr", None), getattr(n.func, "id", None))):
                continue
            pre = {k.arg: k.value for k in n.keywords}.get("prefix")
            if isinstance(pre, ast.JoinedStr) and pre.values:
                pre = pre.values[0]
            if not (isinstance(pre, ast.Constant) and isinstance(pre.value, str)
                    and pre.value):
                bare.append(f"{p.name}:{n.lineno}: {ast.unparse(n)}")
                continue
            found.setdefault(pre.value, []).append(p.name)
    return found, bare


def test_every_temp_prefix_the_app_creates_is_on_the_list():
    from forecast import temp_cleanup as t
    found, bare = _created_prefixes()
    assert len(found) >= 15, found
    assert not bare, f"a temp folder with no prefix can never be cleaned: {bare}"
    missing = [p for p in found if not p.startswith(t.PREFIXES)]
    assert not missing, (f"mkdtemp prefixes the clean-up does not know: "
                         f"{missing} — add them to PREFIXES")
    unused = [p for p in t.PREFIXES if not any(f.startswith(p) for f in found)]
    assert not unused, f"PREFIXES nothing creates any more: {unused}"


def test_the_app_runs_the_clean_up_once_at_start():
    """A module-level call in app.py (not inside a function: once per run of
    the script, and run_once makes it once per process), before any mode
    draws, passing the session's referenced folders."""
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    calls = [n for n in tree.body if isinstance(n, ast.Expr)
             and isinstance(n.value, ast.Call)
             and ast.unparse(n.value.func).endswith("temp_cleanup.run_once")]
    assert len(calls) == 1, "app.py must call temp_cleanup.run_once once, at " \
                            "module level"
    call = calls[0]
    assert "referenced_folders" in ast.unparse(call.value), \
        "the call does not pass the session's referenced folders"
    first_mode = min(n.lineno for n in tree.body if isinstance(n, ast.If)
                     and "app_mode" in ast.unparse(n.test))
    assert call.lineno < first_mode


def test_the_suite_never_sweeps_the_real_temp_dir():
    from forecast import temp_cleanup as t
    assert os.environ.get(t.OFF_ENV) == "1", (
        "conftest.py must switch the clean-up off for the suite: AppTest runs "
        "of app.py would otherwise delete real folders in %TEMP%")


def test_the_old_plain_deletes_now_use_the_robust_one():
    """shutil.rmtree(..., ignore_errors=True) left a read-only config copy
    behind without a word (OneDrive marks folders ReadOnly and copytree
    copies that). The app's temp deletes go through remove_tree."""
    offenders = []
    for rel in ("app.py", "forecast/methods.py", "forecast/stocking_frontier.py",
                "forecast/copilot.py"):
        for i, line in enumerate((ROOT / rel).read_text(encoding="utf-8")
                                 .splitlines(), 1):
            if "rmtree(" in line and "ignore_errors=True" in line:
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, offenders


# --------------------------------------------------------------------------- #
# Review fixes (2026-09-12)
# --------------------------------------------------------------------------- #
def test_remove_tree_goes_on_past_a_file_it_cannot_delete(short_tmp):
    """A file something still has open cannot be deleted. The plain
    rmtree(ignore_errors=True) the app used went on and removed everything
    else; remove_tree's handler re-raised, which stopped rmtree at that file
    and left every entry after it — config copies with costs.yaml among
    them. remove_tree must never leave more than the plain delete did (and
    still clears a read-only entry)."""
    if os.name != "nt":
        pytest.skip("an open file blocks its delete only on Windows")
    from forecast import temp_cleanup as t
    d = short_tmp / "as_run_rt"
    (d / "config").mkdir(parents=True)
    for n in ("a.txt", "b.txt", "c.txt"):
        (d / n).write_text(n)
    (d / "config" / "costs.yaml").write_text("stand_in: 1\n")   # not real costs
    ro = d / "z_readonly.txt"
    ro.write_text("ro")
    os.chmod(ro, stat.S_IREAD)
    fh = open(d / "b.txt", "rb")                     # something still has it open
    try:
        assert t.remove_tree(d) is False             # the folder stays ...
        left = sorted(p.relative_to(d).as_posix() for p in d.rglob("*"))
    finally:
        fh.close()
    assert left == ["b.txt"], (
        f"remove_tree stopped at the open file and left {left}; only the "
        f"open file may stay")
    assert t.remove_tree(d) is True and not d.exists()   # once it is closed


class _Busy:
    """A stand-in for st.spinner: records what the temp dir held when it
    was entered and left."""

    def __init__(self, fake, events):
        self.fake, self.events = fake, events

    def __call__(self):
        return self

    def __enter__(self):
        self.events.append(("enter", sorted(p.name for p in self.fake.iterdir())))
        return self

    def __exit__(self, *exc):
        self.events.append(("exit", sorted(p.name for p in self.fake.iterdir())))
        return False


def test_the_sweep_runs_inside_busy_and_only_when_it_runs(tc, monkeypatch):
    """The app shows a spinner around the first sweep (6,576 folders took
    24.6 s on the operator's machine, and the page was blank): `busy` wraps
    the sweep itself, and is never entered when the sweep does not run."""
    t, fake = tc
    make(fake, "as_forecast_a", 3)
    events, lines = [], []
    busy = _Busy(fake, events)
    got = t.run_once(keep=set, log=lines.append, busy=busy)
    assert got[0] == 1
    assert events == [("enter", ["as_forecast_a"]), ("exit", [])], events
    assert t.run_once(log=lines.append, busy=busy) is None    # not again
    monkeypatch.setenv(t.DONE_ENV, "")
    monkeypatch.setenv(t.OFF_ENV, "1")
    assert t.run_once(log=lines.append, busy=busy) is None    # switched off
    assert len(events) == 2 and len(lines) == 1


def test_the_first_sweep_says_so_on_the_page():
    """app.py's call passes a spinner as `busy`, with a message that says
    what it is doing and that it happens once per server start."""
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    calls = [n.value for n in tree.body if isinstance(n, ast.Expr)
             and isinstance(n.value, ast.Call)
             and ast.unparse(n.value.func).endswith("temp_cleanup.run_once")]
    assert len(calls) == 1
    kw = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
    assert "busy" in kw, "the first sweep runs with a blank page — pass busy="
    assert "st.spinner(" in kw["busy"], kw["busy"]
    for word in ("temp folders", "once per server start"):
        assert word in kw["busy"], f"the spinner text does not say {word!r}"


def test_the_guide_does_not_overstate_what_the_clean_up_protects():
    """It keeps only folders THIS process's session references (empty on a
    fresh start in practice); another copy of the app running at the same
    time (LIVE + NEXT, V1) is not seen. The guide once promised 'never a
    folder your open session's results point to', and the module said a
    loss was 'the same as Windows' own temp clean-up' — which does not run
    on the operator's machine (~12,400 as_* folders piled up)."""
    guide = (ROOT / "docs" / "USER_GUIDE.md").read_text(encoding="utf-8")
    para = guide[guide.index("**Temp folders.**"):]
    para = " ".join(para[:para.index("\n\n")].split())
    assert "folder your open session's results point to" not in para
    assert "another copy of the app running at the same time" in para, para
    assert "first start" in para and "Clearing the app's old temp folders" \
        in para, para
    from forecast import temp_cleanup as t
    doc = " ".join(t.__doc__.split())
    assert "the same as when Windows' own temp clean-up" not in doc
    assert "another copy of the app running at the same time" in doc
