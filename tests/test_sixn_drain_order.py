"""The 6N purge pipeline can trap fish forever; both drain-order fixes cost the floor.

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

Scale across the eight states: 1,222 live tonnes trapped, 4 of 8 states affected.

WHY THE TWO OBVIOUS FIXES ARE OFF
---------------------------------
`sixn_drain_largest_first` (drain the pair's biggest tank first) and
`sixn_overdue_drain_weeks` (a tank past N weeks in purge gets first claim on the
week and is exempt from the hold) are OFF -- but note the reason narrowed once
the detector was fixed.

RE-MEASURED with the corrected detector. Trapped biomass, tonnes:

    state         base   od4   od4+big        weeks below floor (base/od4/od4+big)
    2026-01-31     143     0         0        7 / 12 / 7
    2026-03-31     402     0         0       13 / 12 / 8
    2026-07-31     493   110        30       10 /  5 / 16
    8.13 PR        183    32       244        5 /  3 / 0
    TOTAL        1,222   142       274

So the BENEFIT is much larger than first reported: od4 removes 88% of the
trapped biomass (1,222 -> 142 t), not "about a third" -- that understatement was
an artifact of the row-counting detector, not a property of the fix.

They stay off on the FLOOR, which is the sales contract and unaffected by the
detector bug: od4 takes 2026-01-31 from 7 to 12 weeks below the weekly minimum,
and od4+big takes July'26 from 10 to 16 and drives its worst week to 1,684 fish.
Better on three states, materially worse on one, is not a default.

WORTH RECONSIDERING rather than closed: a variant that keeps od4's trapped-fish
gain without the 2026-01-31 floor regression would be a real fix, and the gain
is now known to be worth chasing.

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
        "adopting this needs a fresh 8-state measure: it made 2026-01-31 worse "
        "on every axis, and it is the weaker of the two on trapped fish")
    assert _field("sixn_overdue_drain_weeks").default == 0, (
        "adopting this needs a fresh 8-state measure: it removes 88% of the "
        "trapped biomass (1,222 -> 142 t) but takes 2026-01-31 from 7 to 12 "
        "weeks below the weekly contract floor. The gain is real; the floor "
        "cost is what disqualifies it as a DEFAULT")
