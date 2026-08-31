"""Which lever settings for THIS month? Decide it by measurement, not by default.

WHY THIS EXISTS
---------------
Four levers were measured across eight starting states in August 2026 and all
four were rejected as defaults — not because they do nothing, but because each
one helps some starting states and hurts others:

    cap_repair_budget=4      feed breaches improve 8/8, weeks below the
                             contract floor worsen in 4 of 8
    rebalance_headroom_days  feed improves 8/8, floor worsens in 7 of 8
    the two 6N drain orders  trapped biomass down ~a third, but one tripled it
                             on one state and one drove a worst harvest week
                             to 1,684 fish

The cap_repair withdrawal note (2026-08-15) records the lesson that produced
this module: a wide distribution sampled eight times reads as noise. So there is
no better default to find. What there IS, is a cheap per-month answer — the same
note says it: "run that PR both ways and compare — it costs forty seconds."

THE RULES THIS ENCODES
----------------------
Ranking is CONSTRAINT-FIRST, and score never enters it:

  1. a hard gate failure disqualifies outright (conservation / no empty week /
     6N one-way). These are not trade-offs.
  2. the CONTRACT FLOOR: weeks below min_harvest_per_week, then the worst week
     in fish. This is a sales commitment, not a preference — a plan that misses
     it more often is worse even if everything else improves.
  3. per-system feed breaches.
  4. the handling budget.

  Score is not consulted at any tier. A plan that holds the floor beats a
  better-scoring plan that does not.

AND THE THIRD VERDICT IS FIRST-CLASS
------------------------------------
"No measurable difference — keep what you have" is the most common honest answer
and must be reported as loudly as a winner. A difference smaller than the
measured sensitivity band is not a difference: neutral perturbations of knobs
that should barely matter swing the worst harvest week by 8,629 fish (a third of
its own value) on a deterministic engine. Anything inside that is the plan's own
chaos, not the lever's doing.

This module DECIDES ONLY. It never writes config, never applies a leg, and never
hides a null.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

#: The legs a monthly check runs. Deliberately SHORT and DECLARED — this is not
#: a search. Each is a lever set with a measured reason to be a candidate.
LEGS: tuple[dict, ...] = (
    {"name": "Your config",
     "overrides": {},
     "why": "what you would run today — the baseline everything is judged against"},
    {"name": "+ end-of-week cap repair",
     "overrides": {"cap_repair_budget": 4},
     "why": "the only pass that runs AFTER the week's growth, on the state the "
            "per-system audit measures. Saturates at 4. Cut feed breaches on "
            "8/8 states tested, but moved the harvest floor both ways."},
    {"name": "+ rebalancer / feed leveling",
     "overrides": {"rebalance_balance_budget": 8, "rebalance_level": True},
     "why": "the multi-objective balancer, which also switches on load "
            "leveling (they share this budget). Saturates at 8. Strongest feed "
            "lever measured; historically the most expensive on the floor."},
)

#: Sensitivity bands, measured 2026-08-30 on the 8.23.26 PR across eight
#: deliberately-neutral perturbations. A change smaller than these is the plan's
#: own chaos, not the lever's. ONE PR's evidence — indicative, not a constant of
#: nature, which is why every rendering of a verdict states them.
NOISE = {
    "weeks_below_floor": 3,
    "min_week": 8_629,
    "feed_over": 13,
    "harvest_count": 14_040,
}

HARD_GATES = ("conservation", "no_empty_week", "sixn_one_way")


@dataclass
class Verdict:
    winner: Optional[str]
    baseline: str
    reason: str
    material: bool
    disqualified: list[tuple] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def keep_current(self) -> bool:
        return self.winner is None or self.winner == self.baseline


def hard_failures(row: dict) -> list[str]:
    g = row.get("gates") or {}
    return [k for k in HARD_GATES if g.get(k) == "FAIL"]


def _f(row: dict, key: str, default=0.0) -> float:
    v = row.get(key)
    try:
        return float(v if v is not None else default)
    except (TypeError, ValueError):
        return default


def beats(cand: dict, base: dict) -> tuple[bool, str]:
    """Is `cand` materially better than `base` on the CONSTRAINTS?

    Returns (better, reason). Ties and inside-noise differences are NOT better —
    the incumbent wins them, because changing a setting has its own cost and
    'it moved a bit' is not evidence.
    """
    cf, bf = _f(cand, "weeks_below_floor"), _f(base, "weeks_below_floor")
    cm, bm = _f(cand, "min_week"), _f(base, "min_week")

    # TIER 2 — the contract floor. A leg that misses it MORE often is worse
    # however good the rest looks; this is a sales commitment.
    if cf > bf:
        return False, (f"misses the weekly contract floor more often "
                       f"({bf:.0f} -> {cf:.0f} weeks)")
    if cm < bm - NOISE["min_week"]:
        return False, (f"drops the worst harvest week materially "
                       f"({bm:,.0f} -> {cm:,.0f} fish)")
    if cf < bf:
        return True, (f"misses the contract floor in {cf:.0f} weeks instead of "
                      f"{bf:.0f}")

    # TIER 3 — per-system feed, only once the floor is no worse.
    co, bo = _f(cand, "feed_over"), _f(base, "feed_over")
    if co < bo - NOISE["feed_over"]:
        return True, (f"cuts per-system feed breaches {bo:.0f} -> {co:.0f} "
                      f"system-weeks with no cost to the floor")
    if co > bo + NOISE["feed_over"]:
        return False, f"adds per-system feed breaches ({bo:.0f} -> {co:.0f})"

    # TIER 4 — handling.
    cmv, bmv = _f(cand, "moves_week_max"), _f(base, "moves_week_max")
    if cmv > bmv:
        return False, f"needs more moves in its busiest week ({bmv:.0f} -> {cmv:.0f})"
    return False, "no measurable difference"


def decide(rows: list[dict], baseline_name: str = "Your config") -> Verdict:
    """Rank measured legs constraint-first and name a winner, or say plainly
    that nothing beat what you already have."""
    by = {r.get("name"): r for r in rows if r and "error" not in r}
    errs = [(r.get("name"), r.get("error")) for r in rows if r and "error" in r]
    base = by.get(baseline_name)
    if base is None:
        return Verdict(None, baseline_name,
                       "the baseline leg did not produce a result, so nothing "
                       "can be compared against it", False, errs)

    dq, notes = list(errs), []
    hb = hard_failures(base)
    if hb:
        notes.append(
            f"WARNING: your current config fails hard gate(s) {', '.join(hb)} "
            f"on this PR. Fix that before choosing a lever.")

    best, best_reason = None, ""
    for name, row in by.items():
        if name == baseline_name:
            continue
        hf = hard_failures(row)
        if hf:
            dq.append((name, f"hard gate failure: {', '.join(hf)}"))
            continue
        ok, why = beats(row, base)
        if not ok:
            notes.append(f"{name}: {why}")
            continue
        if best is None:
            best, best_reason = name, why
        else:
            better, why2 = beats(row, by[best])
            if better:
                best, best_reason = name, why2
            else:
                notes.append(f"{name}: also beats your config, but {why2} "
                             f"against {best}")

    if best is None:
        return Verdict(
            None, baseline_name,
            "Nothing beat your current settings on this PR. That is a real "
            "result, not a failed run — keep what you have.",
            False, dq, notes)
    return Verdict(best, baseline_name, best_reason, True, dq, notes)


def summary_rows(rows: list[dict], baseline_name: str = "Your config") -> list[dict]:
    """Constraint columns FIRST, score last and labelled — the reading order the
    decision actually uses."""
    out = []
    for r in rows:
        if not r or "error" in r:
            out.append({"Leg": (r or {}).get("name", "?"),
                        "Hard gates": "DID NOT RUN",
                        "Weeks below floor": None, "Worst week (fish)": None,
                        "Feed system-wks over": None, "Moves/wk peak": None,
                        "Harvest (fish)": None, "Score (not decisive)": None})
            continue
        hf = hard_failures(r)
        out.append({
            "Leg": r.get("name"),
            "Hard gates": "FAIL: " + ", ".join(hf) if hf else "pass",
            "Weeks below floor": r.get("weeks_below_floor"),
            "Worst week (fish)": r.get("min_week"),
            "Feed system-wks over": r.get("feed_over"),
            "Moves/wk peak": r.get("moves_week_max"),
            "Harvest (fish)": r.get("harvest_count"),
            "Score (not decisive)": r.get("system_overshoot"),
        })
    return out


def noise_caveat() -> str:
    return (
        "Differences smaller than the measured sensitivity band are not "
        f"evidence: weeks below floor ±{NOISE['weeks_below_floor']}, worst week "
        f"±{NOISE['min_week']:,} fish, feed system-weeks ±{NOISE['feed_over']}, "
        f"harvest ±{NOISE['harvest_count']:,} fish. The engine is deterministic, "
        "so this is reproducible chaos sensitivity — the same inputs give the "
        "same plan, but a tiny input change can give a very different one. "
        "Measured on the 8.23.26 PR; treat as indicative for other months.")
