"""The 6N purge pipeline can trap fish forever, and neither drain-order fix works.

THE DEFECT (measured 2026-08-31, 8 starting states, still OPEN)
--------------------------------------------------------------
`_run_sixn_purge_week` holds a 6N tank whose fish would not fit in the week's
remaining processing budget, promising it "drains next rotation". For a tank
holding close to a full week's limit that promise can never be kept: any earlier
harvest in the week leaves too little room, and the same thing recurs on every
rotation. Measured on the 2026-03-31 close, tank OG6N-69 held batch B40 from
2026-W16 to 2027-W20 -- 58 consecutive rotations, frozen at 4.371 kg, off feed,
bleeding ~79 fish/week to mortality, never harvested.

It is NOT a conservation fault: nothing is lost, the fish simply stand at horizon
end and every hard gate passes. It is a plan that cannot physically happen, since
salmon held off feed for a year are dead. It also manufactures the recurring
sub-floor harvest week: a trapped tank means its pair partner drains ALONE, which
is the 7,129 = min_tank_control (7,000) x _REMNANT_KEEP_PAD (1.02) eroded by a
week of mortality, seen across unrelated starting states.

Scale across the eight states: 3,121 live tonnes trapped, 5 of 8 states affected.

WHY THE TWO OBVIOUS FIXES ARE OFF
---------------------------------
`sixn_drain_largest_first` (drain the pair's biggest tank first) and
`sixn_overdue_drain_weeks` (a tank past N weeks in purge gets first claim on the
week and is exempt from the hold) both cut TOTAL trapped biomass by about a
third -- 3,121 t -> 2,171 t / 2,224 t -- and both are incoherent per state:

    state          trapped t          weeks below floor      worst week
                base  od4  od4+big   base  od4  od4+big   base   od4     od4+big
    2026-01-31   143   859      0      7    12       7    7,763  7,589   7,763
    2026-03-31   402     0     34     13    12       8    7,129  7,129   8,825
    2026-07-31 1,463   793  1,484     10     5      16    7,125 16,740   1,684
    8.13 PR      915   320    507      5     3       0    7,261  7,261  30,012

Each variant's worst failure is somewhere the other succeeds: od4 TRIPLES trapped
fish on 2026-01-31 while clearing 2026-03-31; od4+big clears 2026-01-31 while
driving July'26's worst harvest week to 1,684 fish and its floor misses to 16.

Reordering WHICH tank drains only moves the blockage, because the binding fact is
that a 53,000-fish tank cannot coexist with any other harvest inside a 55,000
weekly limit. The fix belongs upstream -- do not FILL a 6N tank to near the
weekly limit -- which is where `sixn_level_drains` already operates. This is the
fourth measured revert in the 6N ordering class; see the notes in placement.py
and docs/SIXN_PURGE_LIVELOCK_2026-08-31.md before attempting a fifth.
"""
import dataclasses

from forecast.models import ControlParams


def _field(name):
    return next(f for f in dataclasses.fields(ControlParams) if f.name == name)


def test_both_drain_knobs_ship_off():
    """OFF must be the default: an existing config is byte-identical until
    someone opts in, and neither knob is a recommendation."""
    assert _field("sixn_drain_largest_first").default is False
    assert _field("sixn_overdue_drain_weeks").default == 0


def test_zero_weeks_means_no_tank_is_ever_overdue():
    """The overdue rule must be a true bypass at 0, not a small threshold --
    `_is_overdue` gates on `_overdue_wks > 0` before comparing days, so a tank
    with no recorded fill date (PR-hydrated, treated as very old) cannot become
    overdue by accident when the knob is off."""
    from forecast import placement
    src = placement.__dict__
    assert "_run_sixn_purge_week" in src
    # the guard is a literal in the ordering closure; pin its shape so a
    # refactor cannot silently make 0 mean "always overdue"
    import inspect
    body = inspect.getsource(placement._run_sixn_purge_week)
    assert "_overdue_wks > 0 and" in body, (
        "the overdue test must short-circuit on the knob being > 0")


def test_the_livelock_is_recorded_not_fixed():
    """A guard against quietly 'fixing' this by flipping a default. If either
    knob is ever adopted it must come with a fresh 8-state measurement, because
    both measured variants regress at least one state badly."""
    assert _field("sixn_drain_largest_first").default is False, (
        "adopting this needs a re-measure: it made 2026-01-31 worse on every "
        "axis and left 507-713 t still trapped on the two worst states")
    assert _field("sixn_overdue_drain_weeks").default == 0, (
        "adopting this needs a re-measure: it tripled trapped biomass on "
        "2026-01-31 (143 t -> 859 t)")
