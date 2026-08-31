"""A setting must not read as ON while it steers nothing.

The active-config panel showed "Feed leveling — ON" whenever `rebalance_level`
was true. Leveling shares `rebalance_balance_budget` and is only reached inside
`if _rebal_on and _bal_budget > 0:`, so with a budget of 0 the panel was stating
something false about the plan the operator was about to run — and the live
config ships that exact combination.
"""
import pytest

from forecast import levers


def _cd(**kw):
    base = {
        "rebalance_balance_budget": 0,
        "rebalance_level": False,
        "rebalance_split_budget": 8,
        "rebalance_varqty_budget": 20,
        "cap_repair_budget": 0,
        "max_transfers_per_week": 15,
    }
    base.update(kw)
    return base


def _by_key(cd):
    return {s.key: s for s in levers.effective_levers(cd)}


def test_leveling_on_with_no_budget_is_INERT_not_ON():
    """THE REGRESSION. This is the shipped configuration."""
    st = _by_key(_cd(rebalance_level=True, rebalance_balance_budget=0))["rebalance_level"]
    assert st.status == levers.INERT
    assert not st.steering
    assert "budget" in st.reason.lower()


def test_leveling_with_budget_is_active():
    st = _by_key(_cd(rebalance_level=True, rebalance_balance_budget=8))["rebalance_level"]
    assert st.status == levers.ACTIVE
    assert st.steering


def test_a_budget_above_the_handling_limit_reads_CLAMPED():
    """30 moves/wk cannot be spent against a 15-move handling budget."""
    st = _by_key(_cd(rebalance_balance_budget=30,
                     max_transfers_per_week=15))["rebalance_balance_budget"]
    assert st.status == levers.CLAMPED
    assert "15" in st.reason


def test_a_saturating_budget_says_so():
    """8 / 15 / 30 measured identical, so 12 is 8 with extra steps."""
    st = _by_key(_cd(rebalance_balance_budget=12,
                     max_transfers_per_week=99))["rebalance_balance_budget"]
    assert st.status == levers.SATURATED
    assert "saturates" in st.reason


def test_cap_repair_off_carries_its_withdrawal_evidence():
    """Off is a decision with a reason; the panel must not present it as a
    blank default an operator should idly flip."""
    st = _by_key(_cd(cap_repair_budget=0))["cap_repair_budget"]
    assert st.status == levers.OFF
    assert "4,578" in st.reason or "withdrawn" in st.reason.lower()


def test_cap_repair_above_four_is_saturated():
    st = _by_key(_cd(cap_repair_budget=15))["cap_repair_budget"]
    assert st.status == levers.SATURATED


def test_forward_headroom_without_a_rebalancer_is_inert():
    st = _by_key(_cd(rebalance_headroom_days=7,
                     rebalance_balance_budget=0))["rebalance_headroom_days"]
    assert st.status == levers.INERT


def test_superseded_knobs_are_named_only_when_set():
    """A knob no engine reads is noise until someone has actually set it."""
    assert "harvest_grade_to_min" not in _by_key(_cd())
    st = _by_key(_cd(harvest_grade_to_min=True))["harvest_grade_to_min"]
    assert st.status == levers.SUPERSEDED
    assert not st.steering


def test_summary_line_is_None_when_everything_is_honest():
    cd = _cd(rebalance_balance_budget=8, rebalance_level=True,
             rebalance_split_budget=0, rebalance_varqty_budget=0)
    # split/varqty still report NO_MEASURED_EFFECT, so build the honest case by
    # checking the line names exactly the levers that are not steering.
    line = levers.summary_line(cd)
    bad = levers.not_steering(cd)
    if not bad:
        assert line is None
    else:
        for s in bad:
            assert s.label in line


def test_every_lever_state_carries_a_reason():
    """A status with no reason is a warning an operator cannot act on."""
    for s in levers.effective_levers(_cd(rebalance_level=True,
                                         rebalance_headroom_days=7,
                                         harvest_grade_to_min=True,
                                         cap_repair_budget=9)):
        assert s.reason.strip(), s.key
        assert s.label.strip(), s.key


def test_measured_claims_carry_their_date():
    """Prose that rots is how the panel got wrong in the first place: any claim
    about a measurement must say when it was measured."""
    for s in levers.effective_levers(_cd(rebalance_balance_budget=30,
                                         max_transfers_per_week=99)):
        if "measured" in s.reason.lower():
            assert "2026-" in s.reason, s.key


def test_it_reads_a_run_config_snapshot_not_just_live_config():
    """The panel renders over a finished run too, from its RunConfig dict —
    missing keys must degrade, never raise."""
    assert levers.effective_levers({}) is not None
    assert levers.summary_line({}) is None or isinstance(levers.summary_line({}), str)
