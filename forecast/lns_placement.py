"""Opt-in LP-guided LNS placement refinement (the Tier-3 engine).

Design: docs/LP_GUIDED_LNS_PLACEMENT.md. Principle: ADD, never remove — the greedy
placement stays the default and is both the warm start and the fallback for this
pass, so turning it on can never lose anything or break a run.

PHASE 1 (current): scaffold + switch ONLY. `refine()` is a NO-OP that returns the
greedy result unchanged, so `placement_method="lns"` is byte-identical to "greedy"
today. No solver, no extra dependency. The MILP model + warm-started, LP-guided
destroy/repair loop land in later phases (§7 of the design doc) — each
measure-or-revert, each keeping the greedy default unchanged.
"""
from __future__ import annotations


def refine(result, final_state, *, control, facility, system_limits,
           facility_limits, batch_meta, tables):
    """Refine the greedy placement toward fewer per-system hot spots, emitting only
    conserved Transfers (continuity preserved by construction). Returns
    `(result, final_state)`.

    Phase 1: no-op — the greedy plan IS the result. Every later phase must keep this
    contract: return a result whose objective is no worse than the greedy warm start
    (else return the greedy unchanged), with 0 drift / 0 dropped and deterministic
    output.
    """
    return result, final_state
