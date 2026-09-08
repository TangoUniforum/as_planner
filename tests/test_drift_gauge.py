"""The facility drift gauge must not scream when conservation is PERFECT.

`excel_io.drift_ratio` is the scale-free leak gauge on the TankContinuityAudit
facility summary: |signed|/|abs|, near 0 = random and cancelling (conserved),
near 1 = a systematic one-way loss. The regression suite asserts |ratio| < 0.3.

Scale-free is the point and also the trap: the ratio is UNDEFINED when there is
no drift to characterise. The original guard was `if abs_sum` — exactly zero —
so float dust divided float dust. On 2026-09-08 a planner change made the
residual dust one-signed and the gauge returned **-1.000, its maximum alarm,
for a leak of zero fish**: the same run had 0 TANK_DRIFT rows and both signed
and abs rounding to 0. Three test files failed for a defect that did not exist.

That is measurement bug number eleven on this project, and the same shape as
the others: a metric that fails silently in the REASSURING direction is bad,
and one that fails loudly in the ALARMING direction is just as expensive — it
sends you hunting a conservation bug you do not have.

The floor costs no detection: a distributed leak worth naming moves many fish.
"""
from __future__ import annotations

import pytest

from forecast.excel_io import DRIFT_FLOOR, drift_ratio


def test_float_dust_is_not_a_leak():
    """The incident: one -1e-9 fish returned the gauge's maximum alarm."""
    assert drift_ratio(-1e-9, 1e-9) == 0.0
    assert drift_ratio(1e-12, 1e-12) == 0.0


def test_exactly_zero_is_still_zero():
    assert drift_ratio(0.0, 0.0) == 0.0


def test_a_real_one_way_leak_still_reads_one():
    """NEGATIVE CONTROL — the alarm must still be able to ring."""
    assert drift_ratio(-9000.0, 9000.0) == -1.0
    assert drift_ratio(9000.0, 9000.0) == 1.0


def test_a_real_leak_trips_the_suites_threshold():
    """The regression guard asserts |ratio| < 0.3; this must exceed it."""
    assert abs(drift_ratio(-5000.0, 9000.0)) >= 0.3


def test_cancelling_drift_reads_near_zero():
    """Large but random per-row drift is conserved and must read ~0."""
    assert abs(drift_ratio(50.0, 10000.0)) < 0.3


@pytest.mark.parametrize("abs_sum", [DRIFT_FLOOR, DRIFT_FLOOR + 1e-6, 5.0])
def test_at_or_above_the_floor_the_gauge_engages(abs_sum):
    assert drift_ratio(-abs_sum, abs_sum) == -1.0


@pytest.mark.parametrize("abs_sum", [0.0, 0.5, DRIFT_FLOOR - 1e-9])
def test_below_the_floor_the_gauge_stays_silent(abs_sum):
    assert drift_ratio(-abs_sum, abs_sum) == 0.0


def test_the_floor_is_one_whole_fish():
    """Sub-fish drift is arithmetic, not biology."""
    assert DRIFT_FLOOR == 1.0
