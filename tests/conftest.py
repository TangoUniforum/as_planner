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
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_CALIB_LOG = os.environ.get(
    "FW_CALIB_GUARD_PATH", os.path.join(ROOT, "fw_calibration_history.jsonl"))


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
