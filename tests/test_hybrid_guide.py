"""The hybrid guide's follow semantics and conditioning rules.

No workbook, no pipeline — these lock the decision table that governs how much
the L1 envelope is allowed to move the controller's harvest target.
"""
from __future__ import annotations

import math

from forecast.hybrid_guide import GuideWeek, HarvestGuide, _condition

MIN_HV, MAX_HV = 30_000.0, 55_000.0


def _guide(follow="full", band=0.10, **kw) -> HarvestGuide:
    weeks = {"2026-W31": GuideWeek("2026-W31", 40_000.0, 120_000.0, "purge")}
    return HarvestGuide(weeks=weeks, follow=follow, band=band,
                        min_harvest=MIN_HV, max_harvest=MAX_HV, **kw)


class _Row:
    def __init__(self, label, count, kg=0.0):
        self.week_label, self.harvested_count, self.harvested_kg = label, count, kg


class TestFollowSemantics:

    def test_absent_week_is_a_true_no_op(self):
        g = _guide()
        assert g.target("2099-W01", 33_000.0, MIN_HV, MAX_HV,
                        allow_ceiling=True) == 33_000.0

    def test_floor_lifts_a_short_week(self):
        g = _guide(follow="floor")
        # Controller wanted 31k; L1 says 40k -> follow L1 up.
        assert g.target("2026-W31", 31_000.0, MIN_HV, MAX_HV,
                        allow_ceiling=False) == 40_000.0

    def test_floor_mode_never_clamps_down(self):
        g = _guide(follow="floor")
        # Controller wants MORE than L1 — floor mode must leave it alone.
        assert g.target("2026-W31", 52_000.0, MIN_HV, MAX_HV,
                        allow_ceiling=True) == 52_000.0

    def test_full_clamps_to_the_band(self):
        g = _guide(follow="full", band=0.10)
        # 40k guide, ±10% -> [36k, 44k].
        assert g.target("2026-W31", 52_000.0, MIN_HV, MAX_HV,
                        allow_ceiling=True) == 44_000.0
        assert g.target("2026-W31", 31_000.0, MIN_HV, MAX_HV,
                        allow_ceiling=True) == 36_000.0

    def test_ceiling_suppressed_when_engines_disagree(self):
        g = _guide(follow="full")
        # Same inputs as above, but the caller says the 6N modes don't match:
        # the floor still applies, the clamp does not.
        assert g.target("2026-W31", 52_000.0, MIN_HV, MAX_HV,
                        allow_ceiling=False) == 52_000.0

    def test_ceiling_can_never_fall_below_the_contract_floor(self):
        # A guide value far under the floor must not produce a sub-floor ceiling.
        g = HarvestGuide(weeks={"W": GuideWeek("W", 1_000.0, 0.0, "purge")},
                         follow="full", band=0.10,
                         min_harvest=MIN_HV, max_harvest=MAX_HV)
        out = g.target("W", 30_000.0, MIN_HV, MAX_HV, allow_ceiling=True)
        assert out >= MIN_HV

    def test_never_exceeds_the_weekly_max(self):
        g = HarvestGuide(weeks={"W": GuideWeek("W", 54_000.0, 0.0, "purge")},
                         follow="full", band=0.50,
                         min_harvest=MIN_HV, max_harvest=MAX_HV)
        assert g.target("W", 50_000.0, MIN_HV, MAX_HV,
                        allow_ceiling=True) <= MAX_HV

    def test_infinite_weekly_max_is_tolerated(self):
        g = _guide(follow="floor")
        out = g.target("2026-W31", 31_000.0, MIN_HV, float("inf"),
                       allow_ceiling=False)
        assert math.isfinite(out) and out == 40_000.0


class TestConditioning:
    """Unusable weeks must be ABSENT, never zero — a zero becomes a ceiling."""

    def test_transition_week_and_its_release_lag_are_dropped(self):
        labels = [f"W{i:02d}" for i in range(8)]
        rows = [_Row(l, 40_000.0) for l in labels]
        modes = {l: "purge" for l in labels}
        modes["W03"] = "transition"
        weeks, dropped, _ = _condition(rows, modes, MIN_HV, MAX_HV, 0.25, 0)
        # The transition week AND the two release weeks it suppresses.
        for l in ("W03", "W04", "W05"):
            assert l not in weeks, f"{l} must be absent, not zero"
            assert l in dropped

    def test_near_zero_weeks_are_dropped_not_zeroed(self):
        labels = ["W00", "W01", "W02", "W03", "W04", "W05"]
        rows = [_Row(l, 40_000.0) for l in labels]
        rows[2] = _Row("W02", 500.0)          # structural dropout
        modes = {l: "purge" for l in labels}
        weeks, dropped, _ = _condition(rows, modes, MIN_HV, MAX_HV, 0.25, 0)
        assert "W02" not in weeks and "W02" in dropped

    def test_horizon_tail_is_dropped(self):
        labels = [f"W{i:02d}" for i in range(6)]
        rows = [_Row(l, 40_000.0) for l in labels]
        modes = {l: "purge" for l in labels}
        weeks, _, _ = _condition(rows, modes, MIN_HV, MAX_HV, 0.25, 0)
        assert "W04" not in weeks and "W05" not in weeks

    def test_survivors_are_clipped_to_contract_bounds(self):
        labels = [f"W{i:02d}" for i in range(6)]
        rows = [_Row(l, 90_000.0) for l in labels]   # way over the ceiling
        modes = {l: "purge" for l in labels}
        weeks, _, _ = _condition(rows, modes, MIN_HV, MAX_HV, 0.25, 0)
        assert weeks and all(w.count <= MAX_HV for w in weeks.values())
        assert all(w.count >= MIN_HV for w in weeks.values())

    def test_l1_mode_is_carried_through(self):
        labels = [f"W{i:02d}" for i in range(6)]
        rows = [_Row(l, 40_000.0) for l in labels]
        modes = {l: "production" for l in labels}
        weeks, _, _ = _condition(rows, modes, MIN_HV, MAX_HV, 0.25, 0)
        assert all(w.l1_mode == "production" for w in weeks.values())
