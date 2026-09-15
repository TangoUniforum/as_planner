"""The assignment-diff emitter never fills a tank past the severe line.

Found 2024-11-30 PR, 2025-W03 (after the numbers audit): the plan moved B30
off OG4S-42 onto {OG3S-36, OG3N-31}. OG3N-31 was still held by B33, so B30's
leg into it was refused, and the residual router moved the rest of OG4S-42
into OG3S-36 WHOLE -- 99,904 fish, 193-200 kg/m3 in a 1,720 m3 tank capped
at 85, for two weeks. The even-split target is a fish count, so the pairing
itself had already put OG3S-36 at 97 kg/m3 the week before. Nothing in
_emit_transfers_for_batch_diff looked at the destination's density.

No move there now takes a tank past DENSITY_SEVERE_RATIO (1.3) x its cap --
the "severe" line (USER_GUIDE §7.1), chosen by the operator 2026-09-14 over
the cap itself. Fish with no room in the batch's plan stay in the source,
like a refused move. Below the line the moves are exactly as before.

Tiny-TankState fixtures, as in tests/test_remnant_floor.py.
"""
from __future__ import annotations

from datetime import date

import pytest

from forecast.placement import _emit_transfers_for_batch_diff
from forecast.state import FacilityState, TankState
from forecast.tiers import DENSITY_SEVERE_RATIO

TODAY = date(2025, 1, 13)
VOL, CAP = 1720.0, 85.0          # the real OG3/OG4 tanks


def _mk(t36_fish, t42_fish, wt_g=3200.0, vol=VOL):
    s = FacilityState(TODAY, [
        TankState("OG3N-31", 31, "OG3N", vol, CAP, 1000.0, "OG"),
        TankState("OG3S-36", 36, "OG3S", vol, CAP, 1000.0, "OG"),
        TankState("OG4S-42", 42, "OG4S", vol, CAP, 1000.0, "OG"),
    ])
    s.tanks_by_id[31].assign("B33", 57_000, 2500.0, 16.0, "SW")   # still there
    s.tanks_by_id[36].assign("B30", t36_fish, wt_g, 16.0, "SW")
    s.tanks_by_id[42].assign("B30", t42_fish, wt_g, 16.0, "SW")
    return s


def _density(t):
    return 0.0 if t.is_empty else t.count * t.avg_wt_g / 1000.0 / t.volume_m3


def _run(s):
    transfers, warns = [], []
    # The plan: B30 gives up 42 and takes {36, 31}; 31 is still B33's.
    _emit_transfers_for_batch_diff(s, "B30", {36, 42}, {36, 31}, TODAY,
                                   transfers, warns)
    return transfers, warns


def test_the_residual_does_not_overfill_the_batchs_other_tank():
    s = _mk(40_000, 47_595)
    before = sum(t.count for t in s.tanks_by_id.values() if t.batch_id == "B30")
    _transfers, warns = _run(s)
    over = {t.location_id: round(_density(t), 1) for t in s.tanks_by_id.values()
            if _density(t) > t.max_density_kg_m3 * DENSITY_SEVERE_RATIO + 0.01}
    assert not over, f"tanks filled past {DENSITY_SEVERE_RATIO}x their density cap: {over}"
    # It fills UP TO the line (the moves below it are unchanged), not to the cap.
    assert _density(s.tanks_by_id[36]) == pytest.approx(CAP * DENSITY_SEVERE_RATIO, abs=0.1)
    after = sum(t.count for t in s.tanks_by_id.values() if t.batch_id == "B30")
    assert after == pytest.approx(before, abs=1)          # every fish accounted for
    assert s.tanks_by_id[31].batch_id == "B33"            # never mixed
    assert s.tanks_by_id[31].count == pytest.approx(57_000)
    t42 = s.tanks_by_id[42]
    assert not t42.is_empty and t42.batch_id == "B30"     # the rest stays put
    assert any("DENSITY CAP" in w and "B30" in w and "OG4S-42" in w for w in warns), warns


def test_with_room_the_residual_still_moves_whole():
    """Where the destination has room nothing changes: the residual moves
    whole and the source empties, as it always did."""
    s = _mk(20_000, 20_000, vol=5000.0)
    _transfers, warns = _run(s)
    assert s.tanks_by_id[42].is_empty
    assert s.tanks_by_id[36].count == pytest.approx(40_000, abs=1)
    assert not any("DENSITY CAP" in w for w in warns), warns
