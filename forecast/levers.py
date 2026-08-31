"""What is each setting ACTUALLY doing to the plan?

WHY THIS EXISTS
---------------
The app told the operator "Feed leveling — ON" whenever `rebalance_level` was
true. Leveling is an argument to `_balance_loads`, which is called inside
`if _rebal_on and _bal_budget > 0:` — so with `rebalance_balance_budget: 0` the
flag steers nothing and the panel was stating something false about the plan
about to run. That is not a missing feature; it is the tool misreporting itself,
and it is the reason "why did my change do nothing?" is answerable only by
reading placement.py.

A setting can fail to bite in several distinct ways, and an operator needs them
distinguished, not merged into a vague warning:

    ACTIVE              it is steering the plan
    OFF                 deliberately disabled (0 / False), the shipped state
    INERT               read, but another setting gates it to nothing
    CLAMPED             a smaller limit binds before this value does
    SATURATED           higher values produce an identical plan
    NO_MEASURED_EFFECT  measured across starting states, no change observed
    SUPERSEDED          no engine reads it any more; kept so old configs load

HONESTY ABOUT WHAT IS KNOWABLE HERE
-----------------------------------
Some clamps are DYNAMIC: the rebalancer gets
`min(rebalance_balance_budget, _moves_left_quality())`, and the second term is
what the week has left after essential moves — it is not knowable from config
alone. So a CLAMPED row names the binding limit rather than inventing a precise
effective number. This module is READ-ONLY ANNOTATION: it never changes a plan,
and it deliberately does not re-implement engine arithmetic it cannot see.

Every measured claim carries its date and sample size, because the alternative
is prose that rots against a config it no longer describes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

ACTIVE = "ACTIVE"
OFF = "OFF"
INERT = "INERT"
CLAMPED = "CLAMPED"
SATURATED = "SATURATED"
NO_MEASURED_EFFECT = "NO_MEASURED_EFFECT"
SUPERSEDED = "SUPERSEDED"

#: statuses that mean "this setting is not shaping the plan the way it reads"
NOT_STEERING = frozenset({INERT, NO_MEASURED_EFFECT, SUPERSEDED})


@dataclass(frozen=True)
class LeverState:
    key: str
    label: str
    raw: Any
    status: str
    reason: str

    @property
    def steering(self) -> bool:
        return self.status not in NOT_STEERING


def _num(cd: dict, key: str, default: float = 0.0) -> float:
    v = cd.get(key, default)
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return default


def effective_levers(cd: dict) -> list[LeverState]:
    """Read a control dict (live config, or a run's RunConfig) and report what
    each tunable lever is really doing. Order is by how much the operator is
    likely to care, not dataclass order."""
    out: list[LeverState] = []
    bal = _num(cd, "rebalance_balance_budget")
    handling = _num(cd, "max_transfers_per_week")
    repair = _num(cd, "cap_repair_budget")

    # --- the rebalancer family -------------------------------------------
    if bal <= 0:
        out.append(LeverState(
            "rebalance_balance_budget", "Rebalancer budget", bal, OFF,
            "0 moves/wk — the multi-objective balancer never runs, and neither "
            "does load leveling, which shares this budget."))
    else:
        bits = []
        if handling > 0 and bal > handling:
            bits.append(
                f"your handling budget of {handling:,.0f} moves/wk binds first, "
                f"so {bal:,.0f} cannot all be spent")
        if bal > 8:
            bits.append(
                "measured 2026-08-30 (8.23.26 PR): 8, 15 and 30 produce an "
                "identical plan — this saturates at 8")
        status = CLAMPED if (handling > 0 and bal > handling) else (
            SATURATED if bal > 8 else ACTIVE)
        out.append(LeverState(
            "rebalance_balance_budget", "Rebalancer budget", bal, status,
            "; ".join(bits) if bits
            else f"{bal:,.0f} moves/wk relieving over-cap systems."))

    lv = bool(cd.get("rebalance_level"))
    if lv and bal <= 0:
        out.append(LeverState(
            "rebalance_level", "Feed leveling", True, INERT,
            "reads ON but steers nothing: leveling shares the rebalancer "
            "budget, which is 0. Raise the rebalancer budget to make this bite."))
    else:
        out.append(LeverState(
            "rebalance_level", "Feed leveling", lv, ACTIVE if lv else OFF,
            "spreads load off the hottest system onto the coolest" if lv
            else "density-only — per-system feed can spike"))

    hd = _num(cd, "rebalance_headroom_days")
    if hd > 0 and bal <= 0:
        out.append(LeverState(
            "rebalance_headroom_days", "Rebalancer forward headroom", hd, INERT,
            "the rebalancer is off (budget 0), so there is nothing to look "
            "ahead for."))
    elif hd > 0:
        out.append(LeverState(
            "rebalance_headroom_days", "Rebalancer forward headroom", hd, ACTIVE,
            "destinations are scored on projected end-of-week load. MEASURED "
            "2026-08-31 on 8 states: cuts feed breaches 8/8 but worsens weeks "
            "below the contract floor in 7/8 (live workbook 2 -> 12). Not a "
            "recommended default."))

    for key, label, note in (
        ("rebalance_split_budget", "Rebalancer fan-out",
         "0 and 16 both reproduce the baseline plan bit-for-bit"),
        ("rebalance_varqty_budget", "Rebalancer precise-count moves",
         "40 reproduces the baseline plan bit-for-bit; only 0 differs"),
    ):
        v = _num(cd, key)
        out.append(LeverState(
            key, label, v, NO_MEASURED_EFFECT,
            f"measured 2026-08-30 across 50 runs on the 8.23.26 PR: {note}. "
            "It may still matter on a different starting state — this is one "
            "PR's evidence, not a proof."))

    # --- end-of-week repair ----------------------------------------------
    if repair <= 0:
        out.append(LeverState(
            "cap_repair_budget", "End-of-week cap repair", repair, OFF,
            "off. Adopted 2026-08-14 and WITHDRAWN a day later: the per-system "
            "gain is robust but the harvest-floor effect is high-variance "
            "(one PR's worst week collapsed 23,259 -> 4,578). Check it on YOUR "
            "PR rather than switching it on."))
    else:
        out.append(LeverState(
            "cap_repair_budget", "End-of-week cap repair", repair,
            SATURATED if repair > 4 else ACTIVE,
            "measured 2026-08-30: 4, 15 and 30 give an identical plan — this "
            "saturates at 4" if repair > 4
            else f"{repair:,.0f} moves/wk after the week's growth, on the state "
                 "the per-system audit actually measures."))

    # --- 6N drain order (both rejected 2026-08-31) ------------------------
    if bool(cd.get("sixn_drain_largest_first")) or _num(cd, "sixn_overdue_drain_weeks") > 0:
        out.append(LeverState(
            "sixn_drain_largest_first", "6N drain order",
            (cd.get("sixn_drain_largest_first"), cd.get("sixn_overdue_drain_weeks")),
            ACTIVE,
            "changes which 6N tank empties first. MEASURED 2026-08-31 on 8 "
            "states: both forms cut trapped biomass ~a third but regress at "
            "least one state badly (one tripled it; one drove a worst harvest "
            "week to 1,684 fish). See docs/SIXN_PURGE_LIVELOCK_2026-08-31.md."))

    # --- knobs no engine reads -------------------------------------------
    for key, label, why in (
        ("harvest_setpoint_lookahead_weeks", "Setpoint lookahead",
         "superseded by the dual-limit setpoint; no harvest path reads it"),
        ("harvest_grade_to_min", "Grade-to-min harvest",
         "historical: the floor-filling grade now runs unconditionally"),
    ):
        if cd.get(key):
            out.append(LeverState(key, label, cd.get(key), SUPERSEDED,
                                  f"{why} — changing it cannot change a plan."))
    return out


def not_steering(cd: dict) -> list[LeverState]:
    """Just the levers that read as set but are not shaping the plan."""
    return [s for s in effective_levers(cd) if not s.steering]


def summary_line(cd: dict) -> Optional[str]:
    """One line for the top of the panel, or None when everything is honest."""
    bad = not_steering(cd)
    if not bad:
        return None
    n = len(bad)
    return (f"{n} setting{'s' if n != 1 else ''} "
            f"{'are' if n != 1 else 'is'} not steering this plan: "
            + ", ".join(s.label for s in bad))
