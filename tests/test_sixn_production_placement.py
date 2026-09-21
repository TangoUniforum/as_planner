"""6N opens to ordinary placement on the operator's production date.

From control.sixn_production_start the 6N MAIN tanks (61/63/65) become normal
production tanks (sixn.py design §5) while the SISTERS (67/69/71) stay
purge-only -- they are physically purge-only (operator, 2026-09-20).

placement.py enforced that with ~24 tests of the OG6N SYSTEM, none of which
looked at the mode, so the mains stayed shut out of every spread, balance,
reserve and consolidate pass for ever. Measured on the 9.16 PR with the
production date forced early: in the 77 weeks of true production phase the
mains held fish in 26% of tank-weeks against 94% for grow-out, and 36 of those
weeks had a tank OVER its density cap while a main sat empty.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from forecast.models import ControlParams
from forecast.placement import sixn_closed_tanks
from forecast.sixn import SIXN_ALL_TANKS, SIXN_MAIN_TANKS, SIXN_SISTER_TANKS
from forecast.state import FacilityState


def _control(**kw):
    f = {x.name: (x.default if x.default is not dataclasses.MISSING else None)
         for x in dataclasses.fields(ControlParams)}
    c = ControlParams(**f)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


PSD = dt.date(2028, 1, 1)


def test_every_6n_tank_is_closed_while_purge_mode_runs():
    c = _control(sixn_growth=False, sixn_production_start=PSD)
    assert sixn_closed_tanks(c, dt.date(2027, 12, 31)) == SIXN_ALL_TANKS


def test_the_mains_open_on_the_production_date():
    c = _control(sixn_growth=False, sixn_production_start=PSD)
    closed = sixn_closed_tanks(c, PSD)
    assert closed == SIXN_SISTER_TANKS
    assert not (closed & SIXN_MAIN_TANKS), "the mains must be available"


def test_the_sisters_are_never_available():
    """Operator 2026-09-20: physically purge-only. No date opens them."""
    c = _control(sixn_growth=False, sixn_production_start=PSD)
    for day in (dt.date(2026, 1, 1), PSD, dt.date(2030, 6, 1)):
        assert SIXN_SISTER_TANKS <= sixn_closed_tanks(c, day)


def test_sixn_growth_opens_the_mains_immediately():
    """sixn_growth=True is production mode from the first day (sixn.is_purge_mode)."""
    c = _control(sixn_growth=True, sixn_production_start=None)
    assert sixn_closed_tanks(c, dt.date(2026, 1, 1)) == SIXN_SISTER_TANKS


def test_no_production_date_means_purge_for_ever():
    c = _control(sixn_growth=False, sixn_production_start=None)
    assert sixn_closed_tanks(c, dt.date(2099, 1, 1)) == SIXN_ALL_TANKS


def test_a_state_nobody_told_keeps_every_6n_tank_closed():
    """The default must be the OLD behaviour: a FacilityState built by hand, or
    used outside the weekly loop that sets this, may not quietly open 6N."""
    st = FacilityState(dt.date(2026, 9, 1), [])
    assert st.sixn_closed_tanks == SIXN_ALL_TANKS


def test_the_placement_passes_read_the_per_week_set_not_the_system():
    """The regression this fixes: ~24 hard-coded OG6N system tests that no
    date could ever change. Pins that placement asks `state` instead."""
    import inspect

    from forecast import placement
    src = inspect.getsource(placement)
    assert src.count("state.sixn_closed_tanks") >= 20, \
        "the placement passes should consult the per-week set"
    # the weekly loop must actually set it, or every pass reads the default
    assert "state.sixn_closed_tanks = sixn_closed_tanks(control, week_start_date)" in src


def test_filling_a_main_in_production_does_not_put_it_off_feed():
    """_freeze_6n_dest marks a filled 6N tank STARVE. That is depuration, and
    in production mode a main is an ordinary growout tank -- freezing it would
    stop the fish feeding and growing."""
    import inspect

    from forecast import placement
    src = inspect.getsource(placement._freeze_6n_dest)
    assert "state.sixn_closed_tanks" in src
    assert '"OG6N"' not in src and "_SIXN_SYSTEMS" not in src
