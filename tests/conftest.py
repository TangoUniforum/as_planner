"""Suite-wide guards.

The live FW calibration log (`fw_calibration_history.jsonl` at the repo root)
is the operator's record of how far the freshwater model drifts from reality.
`run.main` appends to it by default, and on 2026-09-10 three test files were
found doing exactly that — 2,664 fake records since the 2026-09-09 backup,
filed under a closing date that is also a real PR, so they cannot be filtered
out afterwards. A test that runs the pipeline must pass `calib_log_path=""`
(or a path of its own).

This guard fails the session if the live log changed while the tests ran, so
the next test that forgets is caught the day it lands, not months later. The
one false alarm: a real forecast run from THIS checkout's app while the suite
is running also appends — the message says so.

`FW_CALIB_GUARD_PATH` overrides the watched path; it exists so the guard's own
negative control can prove it fires without touching the operator's file.

The operator's REAL operating costs (`config/costs.yaml`, 2026-09-11) get the
same protection, twice over, because the numbers are commercially sensitive
and the suite has no business writing them: in this process
`forecast.costs.save_costs` refuses the live path outright (RuntimeError, at
the call), and at session end the file's content (or absence) must be what
it was — which also catches a subprocess (AppTest) or a direct write. Tests
use a tmp config dir with made-up numbers. `COSTS_GUARD_PATH` overrides the
watched file for the guard's negative control (tests/test_costs_guard.py).
"""
import functools
import hashlib
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_CALIB_LOG = os.environ.get(
    "FW_CALIB_GUARD_PATH", os.path.join(ROOT, "fw_calibration_history.jsonl"))
LIVE_COSTS = os.environ.get(
    "COSTS_GUARD_PATH", os.path.join(ROOT, "config", "costs.yaml"))


def _content(path):
    """sha256 of the file, or None when it does not exist."""
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except FileNotFoundError:
        return None


def _same_path(a, b) -> bool:
    return (os.path.normcase(os.path.realpath(a))
            == os.path.normcase(os.path.realpath(b)))


@pytest.fixture(scope="session", autouse=True)
def _live_costs_never_written():
    from forecast import costs as _fc
    real_save = _fc.save_costs

    @functools.wraps(real_save)
    def guarded(config_dir, costs):
        if _same_path(os.path.join(str(config_dir), _fc.COSTS_FILE),
                      LIVE_COSTS):
            raise RuntimeError(
                f"a test tried to write the live costs file ({LIVE_COSTS}) — "
                f"tests must use a tmp config dir with made-up numbers")
        return real_save(config_dir, costs)

    before = _content(LIVE_COSTS)
    _fc.save_costs = guarded
    try:
        yield
    finally:
        _fc.save_costs = real_save
    if _content(LIVE_COSTS) != before:
        pytest.fail(
            f"the live costs file changed during the test session "
            f"({LIVE_COSTS}). A test wrote the operator's real costs — find "
            f"it and give it a tmp config dir with made-up numbers. (If you "
            f"saved costs from THIS checkout's app while the suite ran, that "
            f"is the cause instead.)", pytrace=False)


def _stamp(path):
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return st.st_size, st.st_mtime_ns


@pytest.fixture(scope="session", autouse=True)
def _live_calibration_log_untouched():
    before = _stamp(LIVE_CALIB_LOG)
    yield
    after = _stamp(LIVE_CALIB_LOG)
    if after != before:
        grew = (after[0] - before[0]) if (after and before) else None
        pytest.fail(
            f"the live FW calibration log changed during the test session "
            f"({LIVE_CALIB_LOG}; size {before and before[0]} -> "
            f"{after and after[0]}" + (f", +{grew} bytes" if grew else "") +
            "). A test ran the pipeline without calib_log_path=\"\" — find it "
            "and pass one. (If you ran a real forecast from this checkout's app "
            "while the suite ran, that is the cause instead.)", pytrace=False)
