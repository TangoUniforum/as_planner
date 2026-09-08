"""A freshwater batch with a count but no weight must say so at HYDRATION.

Growth is multiplicative, so a batch the ProductionReport seeds at 0 g stays at
0 g for its entire freshwater phase. Nothing downstream recovers it, and every
symptom surfaces far from the cause: the FW calibration reports residual -100%
and "did not converge", the batch's FW biomass and size-class split are not
meaningful, and the TranOG reconcile to tran_og_count has no size distribution
to rank -- which is how B56 entered seawater 24% over plan while the audit
described it as an "FW survival calibration gap".

Measured on the 2026-08-31 PR: B56 carries 563,234 fish across 46 hatchery
units, every one 0.00 kg. B54 reads 0.56 g and B55 0.21 g; B56 is simply the
youngest batch, whose weight is not recorded yet.

DETECT, DO NOT COERCE. Seeding a weight from the growth table would produce a
plausible trajectory built on a number nobody measured.
"""
from __future__ import annotations

import inspect

from forecast import run as run_mod


def _hydration_block() -> str:
    src = inspect.getsource(run_mod)
    i = src.index("fw_rolled = summarize_fw_records")
    return src[i:i + 2600]


def test_the_check_exists_and_keys_on_count_without_biomass():
    blk = _hydration_block()
    assert 'info["count"] > 0 and info["biomass_kg"] <= 0' in blk


def test_it_warns_through_hydration_warns():
    """That channel reaches three places at once: the console WARN block, the
    ValidationLog (via invariant_warnings) and the run's warning count. A bare
    print would reach only the first."""
    blk = _hydration_block()
    assert "hydration_warns.append(" in blk
    src = inspect.getsource(run_mod)
    assert "invariant_warnings=(list(hydration_warns)" in src


def test_the_message_names_the_batch_the_fish_and_the_consequence():
    blk = _hydration_block()
    assert "PR FW WEIGHT DERIVED" in blk
    for token in ("{_b}", "{_cnt:,.0f}", "{_units}"):
        assert token in blk, f"the warning must name {token}"
    assert "derived" in blk               # WHAT the planner did about it
    assert "MODELLED, not measured" in blk  # and what the operator still owes


def test_it_aggregates_per_batch_not_per_unit():
    """B56 spans 46 units. One warning per unit would bury the signal in the
    noise this check exists to cut through."""
    blk = _hydration_block()
    assert "_fw_zero.setdefault(batch" in blk
    assert 'e[1] += info["units"]' in blk


def test_a_batch_with_weight_is_not_flagged():
    """The guard is `<= 0`, so B54 (0.56 g) and B55 (0.21 g) stay silent -- a
    check that fired on every FW batch would be ignored within a week."""
    blk = _hydration_block()
    assert 'info["biomass_kg"] <= 0' in blk
    assert 'info["biomass_kg"] < ' not in blk
