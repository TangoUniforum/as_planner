"""R8 -- the density exemption, and the rule that there is only ONE of it.

R8 says a tank is exempt from its per-tank density cap when the fish in it are
preparing for harvest: 6N while it runs in PURGE mode, and ANY tank whose stage
is STARVE, wherever it sits. Fish held off-feed before shipping are meant to be
dense; judging them against a growing tank's welfare cap is a category error.

WHY THIS FILE EXISTS
--------------------
The rule was re-implemented independently across reporting surfaces, each time
as a SYSTEM-MEMBERSHIP test (`system != "OG6N"`, `tank_id in SIXN_ALL_TANKS`)
rather than a stage test. That shape is wrong in both directions, and both were
live in the shipped tool:

  * FALSE POSITIVES. An in-place harvest-prep tank OUTSIDE 6N is exempt but was
    counted. On the shipped workbook this reported 38 breaches where 31 exist --
    7 phantom, the worst reading 194.8 kg/m3. One surface went further and
    RECOMMENDED SPLITTING those tanks, undoing a deliberate consolidation.
  * FALSE NEGATIVES. Once 6N runs as a PRODUCTION system its cap applies
    normally, but a `!= "OG6N"` test exempts it forever, hiding real breaches.

So the truth table below is pinned, and `TestNoSecondRule` fails if another
copy appears. Fix the rule in forecast/tiers.py; never beside a caller.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from forecast.sixn import purge_mode_on
from forecast.tiers import density_exempt, effective_density_cap

CAP = 95.0
INF = float("inf")


class TestTruthTable:
    """Both axes matter: WHICH system, and WHAT the fish are doing."""

    @pytest.mark.parametrize("system,stage,purge,expected", [
        # 6N in PURGE: exempt whatever the stage says.
        ("OG6N", "GROW",   True,  INF),
        ("OG6N", "STARVE", True,  INF),
        # 6N in PRODUCTION: an ordinary production system again...
        ("OG6N", "GROW",   False, CAP),
        # ...except a tank actually preparing for harvest.
        ("OG6N", "STARVE", False, INF),
        # Ordinary grow-out: capped while growing, in EITHER mode. A 6N mode
        # flag must never leak into another system's judgement.
        ("OG3N", "GROW",   True,  CAP),
        ("OG3N", "GROW",   False, CAP),
        # Harvest-prep OUTSIDE 6N -- the false-positive case that motivated R8.
        ("OG3N", "STARVE", True,  INF),
        ("OG3N", "STARVE", False, INF),
    ])
    def test_effective_cap(self, system, stage, purge, expected):
        assert effective_density_cap(CAP, system, stage, purge) == expected

    def test_stage_match_is_case_insensitive(self):
        """Stage arrives from a spreadsheet cell; casing is not a guarantee."""
        for s in ("STARVE", "starve", "Starve"):
            assert density_exempt("OG3N", s, purge_mode=False) is True

    def test_missing_or_unknown_stage_is_not_exempt(self):
        """Absence of evidence is not an exemption -- default to the cap."""
        for s in ("", "GROW", "UNKNOWN"):
            assert density_exempt("OG3N", s, purge_mode=False) is False

    @pytest.mark.parametrize("cap", [0.0, -1.0, None])
    def test_absent_cap_means_unbounded_not_zero(self, cap):
        """A missing cap must not become a cap of ZERO, which would make every
        occupied tank a breach."""
        assert effective_density_cap(cap, "OG3N", "GROW", False) == INF


class TestModeBoundary:
    """R8's purge flag comes from ONE boundary function; pin its edges."""

    START = dt.date(2028, 1, 1)

    def test_boundary_day_is_production_not_purge(self):
        assert purge_mode_on(False, self.START, dt.date(2027, 12, 31)) is True
        assert purge_mode_on(False, self.START, self.START) is False

    def test_sixn_growth_forces_production_immediately(self):
        assert purge_mode_on(True, self.START, dt.date(2020, 1, 1)) is False

    def test_no_production_start_means_purge_forever(self):
        assert purge_mode_on(False, None, dt.date(2099, 1, 1)) is True

    def test_cap_follows_the_boundary(self):
        """The end-to-end path a reporting surface actually walks."""
        before = effective_density_cap(
            CAP, "OG6N", "GROW",
            purge_mode_on(False, self.START, dt.date(2027, 12, 31)))
        after = effective_density_cap(
            CAP, "OG6N", "GROW", purge_mode_on(False, self.START, self.START))
        assert before == INF and after == CAP


class TestNoSecondRule:
    """Guard against a hand-rolled copy of the exemption appearing again.

    Scans for a 6N MEMBERSHIP test sitting within a few lines of a density-vs-cap
    comparison -- the exact shape of every bug this rule has produced. Membership
    tests are fine on their own (routing, colouring, "never move INTO 6N"); it is
    their use as a DENSITY verdict that is wrong.

    Sites below are allowlisted with a reason. Adding one means arguing here, in
    writing, that it is not a density verdict -- which is the point.
    """

    _SIXN = re.compile(r'!=\s*"OG6N"|==\s*"OG6N"|in\s+SIXN_ALL_TANKS')
    _DENS = re.compile(r'density\w*\s*>\s*|>\s*\w*cap|dens\w*\s*>')

    # (filename, distinctive snippet) -> why it is NOT a density verdict.
    ALLOWED = {
        ("excel_io.py", '_sixn_purge = (sysid == "OG6N"'):
            "BIOMASS, not density: the operator ruling of 2026-08-20 gives 6N "
            "no biomass cap while it purges. Already mode-aware, and correct "
            "for the cap it actually judges.",
        ("placement.py", 'and t.system_id != "OG6N"'):
            "ROUTING, not judgement: selects candidate grow-out tanks to move "
            "fish INTO. The nearby `max_density_kg_m3 > 0` is an is-this-a-"
            "real-tank test, not a breach test, and STARVE is already excluded.",
    }

    def _sources(self):
        root = Path(__file__).resolve().parents[1]
        for f in list(root.glob("*.py")) + list((root / "forecast").glob("*.py")):
            if f.name == "tiers.py":          # the one legitimate home
                continue
            yield f

    def _allowed(self, name, line):
        return any(fn == name and snip in line for (fn, snip) in self.ALLOWED)

    def test_no_membership_test_next_to_a_density_comparison(self):
        offenders = []
        for f in self._sources():
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            for i, line in enumerate(lines):
                if line.lstrip().startswith("#") or not self._SIXN.search(line):
                    continue
                if self._allowed(f.name, line):
                    continue
                # Ignore comments in the window: the fix notes describe the OLD
                # rule in prose and must not re-trip the guard.
                window = "\n".join(w for w in lines[max(0, i - 4):i + 5]
                                   if not w.lstrip().startswith("#"))
                if self._DENS.search(window):
                    offenders.append(f"{f.name}:{i + 1}: {line.strip()[:88]}")
        assert not offenders, (
            "A 6N membership test sits beside a density comparison. R8 is "
            "defined ONCE in forecast/tiers.py (effective_density_cap) and "
            "judges by STAGE, not system:\n  " + "\n  ".join(offenders))

    def test_allowlist_entries_still_exist(self):
        """A stale allowlist silently re-opens the hole it was covering."""
        for (name, snip), why in self.ALLOWED.items():
            hits = [f for f in self._sources()
                    if f.name == name
                    and snip in f.read_text(encoding="utf-8", errors="replace")]
            assert hits, (
                f"allowlisted site {name!r} / {snip!r} no longer exists - "
                f"remove the entry (reason on file: {why})")
