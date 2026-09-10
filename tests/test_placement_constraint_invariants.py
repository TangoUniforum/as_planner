"""Constraint invariants rescued from the GLOBAL parity suite.

These tests were written as part of the Global placement work but do NOT
exercise any Global module — they cover shared behaviour that outlives it:
the 6N purge-release weekly limit, transfer-topology breach REPORTING, the
entry-tier movement rules, degrade recording, unplaced-batch error filing,
and STARVE-as-a-state density exclusion.

Extracted mechanically (AST, not by hand) when the Global method was dropped,
because deleting test_global_placement_parity.py wholesale would have taken
16 passing tests with it. Original class grouping and bodies are preserved
verbatim so `git log -S` still finds them.
"""
import os
import sys
from datetime import datetime

import openpyxl
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forecast import excel_io, window_weeks                       # noqa: E402
from forecast.models import ControlParams, FacilityConfig, TankConfig  # noqa: E402
from forecast.sixn import SIXN_MAIN_TANKS, SIXN_SISTER_TANKS      # noqa: E402


class TestEntryTierIsNotAClosedBox:
    """R2 forward relief — the whole of the remaining density breach.

Measured on the operator's 7.29 PR: 74 of 75 tank-weeks over the 95 kg/m3
cap were sub-1 kg fish crammed into OG1/OG2 (up to 187.4 kg/m3) while ~8
grow-out tanks sat free AND spread-eligible in every one of those weeks.
The cause was neither cap headroom nor tank availability (both hypotheses
were tested and refuted) but a tier lock in the Global code that is
STRICTER than the operator's own rule module: tiers.R2 allows an entry-tier
cohort to move forward to any OG3/4/5/6 tank AT ANY WEIGHT."""

    def test_the_rule_module_permits_forward_moves_at_any_weight(self):
        """The authority. If this ever changes, the relief below is illegal and
        must change with it — which is why it is asserted, not assumed."""
        from forecast.tiers import move_allowed
        for wt in (200.0, 500.0, 999.0, 1500.0):
            ok, why = move_allowed("OG1N", "OG3N", wt)
            assert ok, f"R2 forward move refused at {wt} g: {why}"

    def test_backward_is_still_forbidden_at_every_weight(self):
        """NEGATIVE CONTROL: R2 must not be read as "movement is free". R4 is
        untouched — nothing may come back into the entry tier."""
        from forecast.tiers import move_allowed
        for wt in (200.0, 999.0, 5000.0):
            ok, _ = move_allowed("OG3N", "OG1N", wt)
            assert not ok, f"backward move wrongly allowed at {wt} g"

    def test_intra_entry_move_still_locked_above_1kg(self):
        """R3 likewise untouched."""
        from forecast.tiers import move_allowed
        assert move_allowed("OG1N", "OG2N", 500.0)[0]
        assert not move_allowed("OG1N", "OG2N", 1500.0)[0]


class TestTransferTopologyIsJudgedAndSurfaced:
    """R1-R7 conformance of the EMITTED transfer stream.

The controller family emits ZERO topology violations; Global emitted 208
even before this repair series and no gate had ever measured it. A plan that
plays by different movement rules is not comparable to one that does not,
which is the whole point of the compare board."""

    def test_a_breach_that_cannot_be_avoided_is_reported_not_hidden(self):
        """Conservation wins when no legal source exists (the fish must come
        from somewhere), so the move is emitted — but it must be recorded as an
        ERROR, never dropped silently."""
        from forecast.excel_io import write_validation_log
        wb = openpyxl.Workbook()
        write_validation_log(wb, invariant_warnings=[
            "TOPOLOGY VIOLATION - 2026-W34: batch B45 OG6S-64 -> OG1N-11 at "
            "1958 g. R4: backward move OG6S->OG1N"])
        rows = [r for r in wb["ValidationLog"].iter_rows(values_only=True)
                if r and isinstance(r[0], int)]
        assert rows[0][1] == "ERROR - Topology violation (R1-R7)"

    def test_the_rule_module_is_the_authority_for_both_families(self):
        """NEGATIVE CONTROL: the checker must actually reject the two shapes we
        measured, or 'zero violations' would be meaningless."""
        from forecast.tiers import move_allowed
        assert not move_allowed("OG6S", "OG1N", 1958.0)[0]   # R4 backward
        assert not move_allowed("OG1N", "OG1S", 1500.0)[0]   # R3 intra-entry
        assert move_allowed("OG1N", "OG3N", 500.0)[0]        # R2 forward, legal


class TestCpSatWasOverConstrainedNotOverFull:
    """R6 in the CP-SAT model — the cause of the 103/127 infeasible weeks.

R6: ">= 1 kg fish MAY remain in entry-tier tanks (stuck-in-place is legal;
the >= 1 kg overflow in OG1/2 is measured-necessary -- never force-evict)."
CP-SAT barred them outright, so all heavy biomass had to fit the grow-out
tanks alone: 106 of 130 weeks then needed MORE grow-out tanks than exist
while the entry tier sat at 1-3 of its 12. Respecting R6 took infeasible
weeks 103 -> 36 and solver slack 4,878,644 -> 210,481 kg.

Proof it was never a time budget: the infeasible count was EXACTLY 103 at
both a 6 s and a 30 s per-week deterministic budget (802 s vs 2034 s total)."""

    def test_r6_permits_heavy_fish_to_remain_in_the_entry_tier(self):
        """The rule the model was contradicting. tiers has no prohibition on a
        heavy batch OCCUPYING an entry tank — only on MOVING into one (R4)."""
        from forecast.tiers import move_allowed
        # staying put is not a move at all; moving backward is what is banned
        assert not move_allowed("OG3N", "OG1N", 5000.0)[0]
        # and forward is always fine
        assert move_allowed("OG1N", "OG3N", 5000.0)[0]


class TestSolvesAreReproducible:
    """A non-reproducible measurement is a measurement bug (project law).

`time_limit` is WALL CLOCK, so whichever incumbent branch-and-bound held
when the clock ran out became the plan: the same PR gave 54 / 55 / 81
idle-tank weeks purely with CPU contention, which silently poisons every
A/B built on top of it. A bigger budget is NOT the fix -- measured, 9
Pass A.2 weeks still bound at 120 s and a run took 21 minutes instead of 3."""

    def test_every_degrade_is_recorded(self):
        from forecast.excel_io import write_validation_log
        wb = openpyxl.Workbook()
        write_validation_log(wb, invariant_warnings=[
            "NON-DETERMINISTIC SOLVE - Pass A.1 hit its wall-clock time limit on week 2027-W04.",
            "PASS A.2 FALLBACK - 2 week(s) could not PROVE the cap-slack refinement.",
            "PASS B FALLBACK - 18 week(s) kept Pass A's layout.",
        ])
        cats = [r[1] for r in wb["ValidationLog"].iter_rows(values_only=True)
                if r and isinstance(r[0], int)]
        assert cats[0] == "ERROR - Non-reproducible solve"
        assert cats[1].startswith("WARNING - Pass A.2 fallback")
        assert cats[2].startswith("WARNING - Pass B fallback")


class TestUnplacedBatchIsLoud:
    """Fish with L1 standing but no physical tank must be IMPOSSIBLE to miss.
They previously vanished while conservation still reported them standing."""

    def test_unplaced_batch_files_as_an_error_not_a_hydration_note(self):
        wb = openpyxl.Workbook()
        excel_io.write_validation_log(wb, invariant_warnings=[
            "UNPLACED BATCH - 2028-W49: batch B66 (570,000 kg) has L1 standing "
            "but NO legal free tank in its tier (grow-out); it is absent from "
            "BatchLocations."])
        rows = [r for r in wb["ValidationLog"].iter_rows(values_only=True)
                if r and isinstance(r[0], int)]
        assert len(rows) == 1
        assert rows[0][1].startswith("ERROR"), \
            f"an unplaced batch filed as {rows[0][1]!r}"
        assert "Unplaced batch" in rows[0][1]

    def test_unplaced_warning_cannot_be_read_as_a_manual_window_week(self):
        """It carries an ISO week label, so it MUST NOT also carry the manual
        markers — a planner week wrongly tagged 'window' is excluded from the
        harvest-compliance gates, hiding breaches in the degraded run."""
        wb = openpyxl.Workbook()
        excel_io.write_validation_log(wb, invariant_warnings=[
            "UNPLACED BATCH - 2028-W49: batch B66 has L1 standing but no tank.",
            "MANUAL EVENT OK - 2026-W31: harvested 21,812 fish",
        ])
        assert window_weeks.manual_window_weeks(wb) == {"2026-W31"}


class TestPurgeReleaseRespectsTheWeeklyLimit:
    """The weekly processing LIMIT must bind the 6N RELEASE, not only the draw.

The hold length changes at sixn_production_start (2 purge weeks -> 1 in
production), so two weeks' draws mature on the same week. Measured on the
operator's PR: the 2027-W52 draw (hold 2) and the 2028-W01 draw (hold 1)
both landed on 2028-W02 and released 72,040 fish -- 31% over the 55,000
limit, 19% over the 60,500 relief ceiling."""
    LIMIT = 55_000.0
    def _buf(self):
        # the real collision: two matured cohorts on one week
        return {2: [{"batch_id": "B53", "count": 44_101.0, "biomass_kg": 182_578.0},
                    {"batch_id": "B54", "count": 27_939.0, "biomass_kg": 111_478.0}]}

    def test_the_collision_week_is_capped_at_the_limit(self):
        from forecast.global_planner_poc import release_due_capped
        buf = self._buf()
        out = release_due_capped(buf, 2, self.LIMIT)
        assert sum(c for _, c, _ in out) <= self.LIMIT + 1e-6

    def test_the_excess_is_deferred_not_dropped(self):
        """Conservation is non-negotiable: every fish held back must still be
        in the buffer, and biomass must follow the count on a split cohort."""
        from forecast.global_planner_poc import release_due_capped
        buf = self._buf()
        before_c = sum(e["count"] for v in buf.values() for e in v)
        before_kg = sum(e["biomass_kg"] for v in buf.values() for e in v)
        out = release_due_capped(buf, 2, self.LIMIT)
        after_c = sum(e["count"] for v in buf.values() for e in v)
        after_kg = sum(e["biomass_kg"] for v in buf.values() for e in v)
        assert sum(c for _, c, _ in out) + after_c == pytest.approx(before_c)
        assert sum(k for _, _, k in out) + after_kg == pytest.approx(before_kg)
        assert 3 in buf, "the remainder must wait for the FOLLOWING week"

    def test_a_split_cohort_keeps_its_frozen_mean_weight(self):
        """Held fish are off-feed and frozen at move-in weight; a deferral must
        not silently re-price them."""
        from forecast.global_planner_poc import release_due_capped
        buf = self._buf()
        src = dict(buf[2][1])
        out = release_due_capped(buf, 2, self.LIMIT)
        rel = next((c, k) for b, c, k in out if b == "B54")
        defer = next(e for e in buf[3] if e["batch_id"] == "B54")
        assert rel[1] / rel[0] == pytest.approx(src["biomass_kg"] / src["count"])
        assert (defer["biomass_kg"] / defer["count"]
                == pytest.approx(src["biomass_kg"] / src["count"]))

    def test_an_under_limit_week_is_untouched(self):
        """POSITIVE CONTROL: the cap must not perturb an ordinary week."""
        from forecast.global_planner_poc import release_due_capped
        buf = {2: [{"batch_id": "B1", "count": 30_000.0, "biomass_kg": 120_000.0}]}
        out = release_due_capped(buf, 2, self.LIMIT)
        assert out == [("B1", 30_000.0, 120_000.0)]
        assert 3 not in buf

    def test_no_cap_configured_releases_everything(self):
        """Byte-identical to the pre-fix behaviour when the limit is unset, so
        no existing caller changes."""
        from forecast.global_planner_poc import release_due_capped
        buf = self._buf()
        out = release_due_capped(buf, 2, 0)
        assert sum(c for _, c, _ in out) == pytest.approx(72_040.0)
        assert 3 not in buf

    def test_the_defect_reproduces_without_the_cap(self):
        """NEGATIVE CONTROL: prove the fixture really contains the defect --
        uncapped, this exact week emits the 72,040 the operator saw, 19% over
        the 60,500 relief ceiling. Without this, the cap test proves nothing."""
        from forecast.global_planner_poc import release_due_capped
        uncapped = sum(c for _, c, _ in release_due_capped(self._buf(), 2, 0))
        assert uncapped == pytest.approx(72_040.0)
        assert uncapped > 60_500.0
        capped = sum(c for _, c, _ in release_due_capped(self._buf(), 2, self.LIMIT))
        assert capped <= 60_500.0


class TestStarveIsAStateNotATankId:
    """Once the 6N mains carry production grow-out, stamping them STARVE would
hide those fish from every density/welfare metric (they all exclude purge
rows) and corrupt the depuration-hold audit."""

    def test_density_metrics_exclude_starve_rows(self):
        """The reason the stamp matters: a STARVE row is invisible to the
        density peak. If production fish were stamped STARVE, an over-packed
        6N main would read as a clean facility."""
        from forecast.optimize import _is_purge_row
        hdr_si, hdr_sti = 4, 9
        starve = ("2028-W20", None, "B1", 61, "OG6N", 1, 1.0, 1, 500.0, "STARVE")
        grow = ("2028-W20", None, "B1", 61, "OG6N", 1, 1.0, 1, 500.0, "SW")
        assert _is_purge_row(starve, hdr_si, hdr_sti) is True
        assert _is_purge_row(grow, hdr_si, hdr_sti) is False
