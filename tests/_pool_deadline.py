"""A bounded wait for the tests that start a REAL process pool (2026-09-11).

ideal_optimize._run_wave waits on `as_completed` with no timeout. That is
right in production, where a long grid is legitimate. In a test, a worker
that hangs (a spawn that never comes up, a job that never returns) would
block the whole suite. `pool_deadline` gives that wait a deadline. Past it
the wave raises TimeoutError, which takes the wave's own error path (cancel
what has not started, re-raise). The pool's worker processes are then
killed so the interpreter can still exit, and the test FAILS naming the
deadline.

Only the WAIT is bounded. The pool is the real ProcessPoolExecutor (a
subclass that records its worker processes as they start), and the wait is
the real as_completed with a timeout. What the wave does with the results
is untouched. Standard library and pytest only: no new dependency.

NOT BOUNDED: _run_wave's one-at-a-time fallback. When the pool breaks
(BrokenProcessPool / PicklingError) the wave runs the remaining jobs in THIS
process, and a job that hangs there is not interrupted by this deadline (a
thread or signal would be needed, and would change what the wave does). The
tests that use this also assert the wave's note is None, so a fallback that
does finish still fails them.
"""
from __future__ import annotations

import contextlib
import time
from concurrent import futures

import pytest


@contextlib.contextmanager
def pool_deadline(module, seconds: float, what: str):
    """Within the block, `module`'s ProcessPoolExecutor / as_completed (as
    ideal_optimize imports them) wait at most `seconds` in total, across
    every wave. Yields the list of worker processes the pools started."""
    deadline = time.monotonic() + float(seconds)
    procs: list = []

    class _Pool(futures.ProcessPoolExecutor):
        def submit(self, fn, /, *args, **kwargs):
            fut = super().submit(fn, *args, **kwargs)
            for p in list((self._processes or {}).values()):
                if p not in procs:
                    procs.append(p)
            return fut

    def _as_completed(fs, timeout=None):
        left = max(deadline - time.monotonic(), 0.0)
        return futures.as_completed(
            fs, timeout=left if timeout is None else min(timeout, left))

    def _kill():
        for p in procs:
            if p.is_alive():
                p.kill()
        for p in procs:
            p.join(timeout=30)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(module, "ProcessPoolExecutor", _Pool)
        mp.setattr(module, "as_completed", _as_completed)
        try:
            yield procs
        except futures.TimeoutError:
            _kill()
            pytest.fail(f"{what}: the real process pool did not finish "
                        f"within {seconds:g} s - a worker hung; its processes "
                        f"were killed so the suite can go on", pytrace=False)
        finally:
            _kill()          # a no-op after a clean wave: its workers exited
