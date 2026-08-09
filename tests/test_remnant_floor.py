"""No-remnant-tanks rule (INV-5 at the SOURCE) — unit truth for the emitters.

The operator floor: every operation that removes fish from a tank must obey
"take all, or leave >= min_tank_control", and no placement should CREATE an
occupancy under the floor when avoidable. A sub-min "remnant" ties up a whole
tank + feed line for a rounding error of fish (the OG1N-15 / OG1S-16 finding
on the 7.29.26 PR + operator window — created by partial outbound transfers
and by refused (R4) refills the even-split emitter had planned).

Covers:
  - _floored_take / _floored_partial arithmetic
  - _emit_transfers_for_batch_diff: legality-aware pairing + sub-min-target
    destination shrink + floored partial drains
  - _consolidate_remnants: the weekly sweep folds an attrition remnant into
    its own batch's tanks (and refuses illegal / impossible folds)

State fixtures follow the tiny-TankState pattern of tests/test_transfer_rules.py.
"""
from __future__ import annotations

from datetime import date

from forecast.placement import (
    _REMNANT_KEEP_PAD,
    _consolidate_remnants,
    _emit_transfers_for_batch_diff,
    _floored_partial,
    _floored_take,
)
from forecast.state import FacilityState, TankState

TODAY = date(2026, 8, 3)
MIN = 7000.0


def _mk_state():
    """Tiny facility: 2 entry tanks, 3 grow-out tanks, 1 6N tank.
    1,000 m3 x 95 kg/m3 -> 95 t per tank (~30k fish at 3 kg)."""
    return FacilityState(TODAY, [
        TankState("OG1N-1", 1, "OG1N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG1S-2", 2, "OG1S", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG3N-3", 3, "OG3N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG4N-4", 4, "OG4N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG5N-5", 5, "OG5N", 1000.0, 95.0, 1000.0, "OG"),
        TankState("OG6N-61", 61, "OG6N", 1000.0, 120.0, 1000.0, "OG"),
    ])


def _no_submin(state, min_keep=MIN, exclude=()):
    """Assert no occupied non-6N tank holds a sub-min remnant."""
    bad = [
        (t.location_id, t.batch_id, round(t.count))
        for t in state.tanks_by_id.values()
        if not t.is_empty and t.system_id != "OG6N"
        and t.tank_id not in exclude and 0 < t.count < min_keep
    ]
    assert not bad, f"sub-min remnants: {bad}"


# ---------------------------------------------------------------------------
# The floor arithmetic
# ---------------------------------------------------------------------------

class TestFlooredTake:
    def test_legal_residue_passthrough(self):
        assert _floored_take(30000, 10000, MIN) == 10000

    def test_reduced_to_leave_the_floor(self):
        # The measured OG1N-15 creation: 13,852 fish, plan wanted 13,160
        # (would leave 692) -> take is reduced so the padded floor stays
        # (the pad covers weekly mortality erosion below 7,000).
        take = _floored_take(13852, 13160, MIN)
        assert take == 13852 - MIN * _REMNANT_KEEP_PAD
        assert 13852 - take >= MIN

    def test_take_all_when_floor_unretainable(self):
        # A source already under the floor is drained WHOLE, never nibbled.
        assert _floored_take(6800, 500, MIN) == 6800

    def test_full_drain_intended(self):
        assert _floored_take(9000, 9000, MIN) == 9000
        assert _floored_take(9000, 12000, MIN) == 9000

    def test_disabled_floor(self):
        assert _floored_take(9000, 8500, 0.0) == 8500

    def test_partial_never_escalates(self):
        # _floored_partial reduces or skips, never take-all.
        take = _floored_partial(13000, 7500, MIN)
        assert take == 13000 - MIN * _REMNANT_KEEP_PAD and 13000 - take >= MIN
        assert _floored_partial(6800, 500, MIN) == 0.0
        assert _floored_partial(30000, 10000, MIN) == 10000


# ---------------------------------------------------------------------------
# The assignment-diff emitter
# ---------------------------------------------------------------------------

class TestDiffEmitterFloor:
    def test_no_backward_refill_planned_no_remnant_stranded(self):
        """The OG1S-16 class: batch holds an entry tank (over target) + a
        grow-out tank; the plan wants the entry tank refilled FROM grow-out
        (R4-illegal — the old emitter planned it, apply refused it, and the
        entry tank's successful OUTBOUND leg stranded a sub-min remnant).
        With legality-aware pairing the entry tank is never drained below
        the floor by a plan that cannot legally refill it."""
        s = _mk_state()
        s.tanks_by_id[1].assign("B47", 26438, 2228.0, 16.0, "SW")  # entry, >1kg
        s.tanks_by_id[3].assign("B47", 2000, 2318.0, 16.0, "SW")   # grow-out
        transfers, warns = [], []
        # Plan: keep 1 and 3, add 4 (empty grow-out). Even split would drag
        # the entry tank toward total/3 ~ 9.5k — fine; now shrink the fish so
        # the split target is SUB-MIN and the guard must kick in.
        _emit_transfers_for_batch_diff(
            s, "B47", {1, 3}, {1, 3, 4}, TODAY, transfers, warns, min_keep=MIN)
        _no_submin(s)
        # Nothing may have been emitted growout -> entry (R4).
        for ev in transfers:
            src_sys = {1: "OG1N", 2: "OG1S", 3: "OG3N", 4: "OG4N",
                       5: "OG5N", 61: "OG6N"}[ev.source_tank_id]
            for d in ev.destinations:
                if d.tank_id in (1, 2):
                    assert src_sys in ("OG1N", "OG1S"), (
                        f"planned backward refill {src_sys} -> entry #{d.tank_id}")

    def test_submin_target_drops_new_destinations(self):
        """A plan that fans 10,500 fish across 3 tanks (3,500 each — all
        sub-min) must shrink to the floor: new destinations are dropped."""
        s = _mk_state()
        s.tanks_by_id[3].assign("B50", 10500, 3000.0, 16.0, "SW")
        transfers, warns = [], []
        _emit_transfers_for_batch_diff(
            s, "B50", {3}, {3, 4, 5}, TODAY, transfers, warns, min_keep=MIN)
        _no_submin(s)
        # The batch may occupy at most 1 tank (10,500 // 7,000 = 1).
        occ = [t for t in s.tanks_by_id.values() if not t.is_empty]
        assert len(occ) == 1 and occ[0].count == 10500

    def test_partial_drain_leaves_floor_or_empties(self):
        """A dropped source partially drained toward a deficit leaves >= MIN
        (routed whole afterwards) — never a sub-min tail."""
        s = _mk_state()
        s.tanks_by_id[3].assign("B51", 13852, 3000.0, 16.0, "SW")
        s.tanks_by_id[4].assign("B51", 20000, 3000.0, 16.0, "SW")
        transfers, warns = [], []
        # Plan drops tank 3; all fish consolidate onto 4 and 5.
        _emit_transfers_for_batch_diff(
            s, "B51", {3, 4}, {4, 5}, TODAY, transfers, warns, min_keep=MIN)
        _no_submin(s)
        assert s.tanks_by_id[3].is_empty  # residual routing finished the job

    def test_conservation(self):
        s = _mk_state()
        s.tanks_by_id[3].assign("B52", 15000, 3000.0, 16.0, "SW")
        s.tanks_by_id[4].assign("B52", 22000, 3000.0, 16.0, "SW")
        before = sum(t.count for t in s.tanks_by_id.values())
        transfers, warns = [], []
        _emit_transfers_for_batch_diff(
            s, "B52", {3, 4}, {3, 4, 5}, TODAY, transfers, warns, min_keep=MIN)
        after = sum(t.count for t in s.tanks_by_id.values())
        assert abs(before - after) < 0.5
        _no_submin(s)


# ---------------------------------------------------------------------------
# The weekly remnant sweep
# ---------------------------------------------------------------------------

class TestConsolidationSweep:
    def test_folds_attrition_remnant_forward(self):
        """An entry-tier remnant (mortality attrition / stranded tail) folds
        into its own batch's grow-out tank — the tank + feed line are freed."""
        s = _mk_state()
        s.tanks_by_id[1].assign("B48", 692, 2228.0, 16.0, "SW")     # remnant
        s.tanks_by_id[3].assign("B48", 20000, 2125.0, 16.0, "SW")   # absorber
        transfers, warns = [], []
        folds = _consolidate_remnants(s, TODAY, "2026-W53", transfers, warns, MIN)
        assert folds == 1
        assert s.tanks_by_id[1].is_empty
        assert abs(s.tanks_by_id[3].count - 20692) < 0.5
        assert any("REMNANT SWEEP" in w for w in warns)
        _no_submin(s)

    def test_splits_across_tanks_when_one_lacks_headroom(self):
        """Neither absorber alone can hold the remnant; the fold splits it
        across both (a final short split tops up the previous destination —
        it never opens a new sub-min tank)."""
        s = _mk_state()
        # 0.98 * 95,000 kg = 93,100 kg usable. Headrooms at 3 kg/fish:
        # tank 3: 90,000 kg -> 1,033 fish; tank 4: 84,000 kg -> 3,033 fish.
        s.tanks_by_id[3].assign("B49", 30000, 3000.0, 16.0, "SW")
        s.tanks_by_id[4].assign("B49", 28000, 3000.0, 16.0, "SW")
        s.tanks_by_id[5].assign("B49", 4000, 3000.0, 16.0, "SW")   # remnant
        transfers, warns = [], []
        folds = _consolidate_remnants(s, TODAY, "2027-W01", transfers, warns, MIN)
        assert folds == 1
        assert s.tanks_by_id[5].is_empty
        assert len(transfers) == 1 and len(transfers[0].destinations) == 2
        _no_submin(s)
        # Neither absorber pushed past its 98% density headroom.
        for tid in (3, 4):
            t = s.tanks_by_id[tid]
            assert t.biomass_kg <= t.max_biomass_kg * 0.98 + 1.0

    def test_never_folds_backward_into_entry(self):
        """Grow-out remnant with only ENTRY same-batch tanks: R4 forbids the
        fold — remnant stays (legitimate residual, reported not repaired)."""
        s = _mk_state()
        s.tanks_by_id[3].assign("B50", 500, 900.0, 16.0, "SW")     # grow-out remnant
        s.tanks_by_id[1].assign("B50", 20000, 900.0, 16.0, "SW")   # entry absorber
        transfers, warns = [], []
        folds = _consolidate_remnants(s, TODAY, "2027-W02", transfers, warns, MIN)
        assert folds == 0
        assert not s.tanks_by_id[3].is_empty
        assert not transfers

    def test_lone_batch_tail_stays(self):
        """A batch whose total remainder < MIN lives alone — nothing to fold
        into (INV-1 forbids mixing batches), so it stays and is reported."""
        s = _mk_state()
        s.tanks_by_id[3].assign("B51", 4000, 3800.0, 16.0, "SW")
        transfers, warns = [], []
        folds = _consolidate_remnants(s, TODAY, "2027-W03", transfers, warns, MIN)
        assert folds == 0 and not transfers
        assert s.tanks_by_id[3].count == 4000

    def test_starve_and_6n_tanks_exempt(self):
        """Purge-pipeline tanks (6N depuration, in-place STARVE) are transient
        staging — never swept, never topped up."""
        s = _mk_state()
        s.tanks_by_id[61].assign("B52", 1381, 4475.0, 16.0, "SW")
        s.tanks_by_id[61].stage = "STARVE"
        s.tanks_by_id[4].assign("B52", 20000, 4000.0, 16.0, "SW")
        s.tanks_by_id[5].assign("B52", 600, 4000.0, 16.0, "SW")
        s.tanks_by_id[5].stage = "STARVE"   # in-place purge remnant: pipeline-owned
        transfers, warns = [], []
        folds = _consolidate_remnants(s, TODAY, "2027-W04", transfers, warns, MIN)
        assert folds == 0 and not transfers

    def test_disabled_floor_is_noop(self):
        s = _mk_state()
        s.tanks_by_id[1].assign("B53", 692, 2228.0, 16.0, "SW")
        s.tanks_by_id[3].assign("B53", 20000, 2125.0, 16.0, "SW")
        assert _consolidate_remnants(s, TODAY, "2027-W05", [], [], 0.0) == 0
