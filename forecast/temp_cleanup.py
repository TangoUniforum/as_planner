"""Clean up the app's own temp folders — once per server process.

The app and the forecast modules make their work folders with
tempfile.mkdtemp (the PREFIXES below). Several are never removed — the Run
page's as_forecast_ folders alone: 491 folders, 111 MB on the operator's
machine (2026-09-12) — and some hold a copy of config/, which can include
costs.yaml. Once per server process, at app start, `run_once` deletes the
folders DIRECTLY in tempfile.gettempdir() whose name starts with one of
PREFIXES and whose own mtime is older than MAX_AGE_DAYS. Never a file, a
symlink or junction (nor what it points at), anything outside the temp dir,
or a folder the sweeping session's state references (this process only —
and on a fresh start that state is empty in practice; the real guarantee
is the age rule, below). A folder something still has open is skipped
UNTOUCHED: it is first renamed, which Windows refuses while any file inside
is open, and only a renamed folder is deleted. Every error is swallowed per
folder; one line goes to the server log, never a file name or its contents.

Why no reader IN THIS PROCESS can lose a folder it needs: the sweep runs
once, in the process's first script run, before any mode draws, and every
folder the process makes afterwards is younger than the threshold. The
readers of a result's "output_path" (the Run page's feed / system-feed /
transfer tabs, Decide's harvest rows and reviews, the board score) read
folders this process made — or, for a result hydrated from the disk cache
(the Decide board, the analysis summary), a path _restore_output_path
regenerates from the cached bytes when its folder is gone; that hydration
runs after the sweep. The per-run folders (ideal_engine_, as_cmp_,
ideal_ref_keep_, ...) are read only inside the run that made them.

What it CANNOT see: another copy of the app running at the same time (the
LIVE and NEXT copies on their two ports, or the V1 fallback) — they share
this temp dir and these prefixes. A result such a copy has kept open for
longer than MAX_AGE_DAYS can find its folder gone when this one starts; the
readers above then return empty / say the file is gone rather than fail.
Windows does NOT clean these up on its own on the operator's machine
(~12,400 as_* folders had piled up by 2026-09-12), so nothing else will.

The first sweep on a machine with that backlog is slow (measured
2026-09-12: 6,576 folders, ~1 GB, 24.6 s); `run_once(busy=...)` lets the
app say so on the page while it runs.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

# Every tempfile.mkdtemp prefix the app (app.py) and the forecast package
# use (tests/test_temp_cleanup.py checks this list against the code).
PREFIXES = ("as_forecast_", "as_pr_", "as_copilot_", "as_tmpl_", "as_import_",
            "as_solveui_", "as_frontier_", "as_opt_in_", "as_mcheck_",
            "as_ana_", "as_cmp_", "as_optcfg_", "as_stock_", "as_run_",
            "ideal_ref_keep_", "ideal_tr_pr_", "ideal_tr_opt_",
            "ideal_engine_")
# A folder is removed only when its own mtime is older than this. A run
# lasts minutes; two days leaves every folder a live session could still
# be reading alone.
MAX_AGE_DAYS = 2
OFF_ENV = "AS_TEMP_CLEANUP_OFF"     # "1" switches it off (the test suite)
DONE_ENV = "AS_TEMP_CLEANUP_PID"    # the process that already swept
_LOCK = threading.Lock()
_TOMB = "~removing"                 # a folder being deleted (still prefixed)


def _clear_readonly_and_retry(func, path, _exc) -> None:
    """rmtree's error handler: a read-only entry is made writable and tried
    once more. One that still cannot go (a file something has open) is left
    and the delete GOES ON with the rest, as ignore_errors=True did —
    re-raising here aborted rmtree and left every entry after that file,
    more than the plain delete it replaced (review 2026-09-12)."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def remove_tree(path) -> bool:
    """Delete a folder the app made; True when it is gone (or never was).

    shutil.rmtree(ignore_errors=True) silently left folders behind: OneDrive
    marks its folders ReadOnly and copytree copies that onto a config copy
    (forecast.ideal_engine._remove_tree, 2026-09-10). A read-only entry is
    made writable and retried; an entry that still cannot go is left and
    the rest is deleted. Never raises."""
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_clear_readonly_and_retry)
        else:
            shutil.rmtree(path, onerror=_clear_readonly_and_retry)
    except OSError:
        pass
    return not os.path.lexists(path)


def _tree_size(path) -> int:
    total = 0
    for top, _dirs, files in os.walk(path, followlinks=False):
        for f in files:
            try:
                total += os.lstat(os.path.join(top, f)).st_size
            except OSError:
                pass
    return total


def _is_link(st_) -> bool:
    """A symlink, or on Windows any reparse point (a junction included)."""
    return (stat.S_ISLNK(st_.st_mode)
            or bool(getattr(st_, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))


def _remove_if_old(path, now=None, keep=()):
    """-> bytes removed, or None when `path` is left alone."""
    p = Path(path)
    try:
        root = os.path.normcase(os.path.realpath(tempfile.gettempdir()))
        if os.path.normcase(os.path.realpath(p.parent)) != root:
            return None                     # only folders DIRECTLY in temp
        if (not p.name.startswith(PREFIXES)
                or os.path.normcase(p.name) in keep):
            return None
        st_ = os.lstat(p)
        if _is_link(st_) or not stat.S_ISDIR(st_.st_mode):
            return None
        if (time.time() if now is None else now) - st_.st_mtime \
                < MAX_AGE_DAYS * 86400.0:
            return None
        tomb = p if p.name.endswith(_TOMB) else p.with_name(p.name + _TOMB)
        if tomb != p:
            # Windows refuses to rename a folder while anything inside it is
            # open: such a folder is skipped, untouched — never half deleted.
            _rename(p, tomb)
        size = _tree_size(tomb)
        return size if remove_tree(tomb) else None
    except Exception:  # noqa: BLE001 — one folder must never stop the rest
        return None


def _rename(src, dst, tries=5, pause=0.05) -> None:
    """os.rename, retried briefly on PermissionError: a virus scanner holds
    a just-written file for a few ms (measured 2026-09-12: 6 of 40 fresh
    folders refused an immediate rename, every one was free 20 ms later).
    A folder still refused after ~0.2 s is in use and raises."""
    for i in range(tries):
        try:
            os.rename(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(pause)


def remove_if_old(path, now=None, keep=()) -> bool:
    """Delete one folder when it is the app's, directly in the temp dir,
    older than MAX_AGE_DAYS, not a link, not in `keep` (folder names) and
    not in use. True when it was removed."""
    return _remove_if_old(path, now=now,
                          keep={os.path.normcase(k) for k in keep}) is not None


def sweep(now=None, keep=()) -> tuple:
    """-> (folders removed, bytes removed) for one pass over the temp dir.
    `keep`: folder names never to remove (referenced_folders)."""
    keep = {os.path.normcase(k) for k in keep}
    try:
        entries = [e.path for e in os.scandir(tempfile.gettempdir())
                   if e.name.startswith(PREFIXES)]
    except OSError:
        return 0, 0
    n = nbytes = 0
    for path in entries:
        got = _remove_if_old(path, now=now, keep=keep)
        if got is not None:
            n += 1
            nbytes += got
    return n, nbytes


def referenced_folders(obj) -> set:
    """Names of the temp-dir folders a path anywhere in `obj` (nested dicts,
    lists and tuples — session state, a result's "output_path") points into."""
    root = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
    out = set()

    def walk(o, depth):
        if depth > 8:
            return
        if isinstance(o, (str, os.PathLike)):
            try:
                s = os.fspath(o)
                if not isinstance(s, str) or len(s) > 1000 or "\n" in s:
                    return
                p = os.path.normcase(os.path.abspath(s))
            except (TypeError, ValueError, OSError):
                return
            if p.startswith(root + os.sep):
                out.add(p[len(root) + 1:].split(os.sep)[0])
        elif isinstance(o, dict):
            for v in list(o.values()):
                walk(v, depth + 1)
        elif isinstance(o, (list, tuple, set, frozenset)):
            for v in list(o):
                walk(v, depth + 1)
    walk(obj, 0)
    return out


def _log(line) -> None:
    print(line, flush=True)     # flushed: a server log redirected to a file


def run_once(keep=(), log=_log, busy=None):
    """The sweep, once per server process (it survives a module reload:
    the process id is kept in the environment). `keep`: folder names, or a
    callable returning them (called only when the sweep runs). `busy`: a
    callable returning a context manager the sweep runs inside — entered
    ONLY when the sweep runs (the app passes st.spinner, so a slow first
    sweep shows a message instead of a blank page). Logs one line. ->
    (folders, bytes) removed, or None when it did not run. Never raises."""
    if os.environ.get(OFF_ENV, "") not in ("", "0"):
        return None
    with _LOCK:
        if os.environ.get(DONE_ENV) == str(os.getpid()):
            return None
        os.environ[DONE_ENV] = str(os.getpid())
    n = nbytes = 0
    try:
        with (busy() if busy is not None else contextlib.nullcontext()):
            n, nbytes = sweep(keep=keep() if callable(keep) else keep)
    except Exception:  # noqa: BLE001 — a clean-up must never stop the app
        pass
    try:
        log(f"temp clean-up: removed {n} folders ({nbytes / 2 ** 20:.1f} MB) "
            f"older than {MAX_AGE_DAYS} days")
    except Exception:  # noqa: BLE001
        pass
    return n, nbytes
