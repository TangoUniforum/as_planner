"""L1-guided harvest target for the validated controller (the "hybrid").

The controller decides harvest reactively, week by week, off realized biomass.
That is why it is trustworthy — and why it paces poorly: it cannot see that a
week three months out will be short of the contract floor. The global engine's
L1 planner solves the whole horizon tanklessly and produces a steady weekly
harvest quantity, but its placement layer is a different (unvalidated) machine.

This module takes ONLY L1's per-week harvest quantity and hands it to the
controller as a target, so the controller's own audited machinery executes a
better-paced plan. L1 runs standalone here (~5s) — no L2/L3, no LP, no CP-SAT.

Two properties make this safe:

  * The guide is a REQUEST, not a command. Both injection points degrade by
    moving or entering fewer fish when the request cannot be met; neither can
    raise, drop or double-count a fish. Conservation is untouched.

  * The guide is OPEN-LOOP. It is computed once, before the walk, and never
    reads pipeline state. That is what distinguishes it from the move-in sizing
    that self-amplified into a 30k<->55k bang-bang (see caps.py) — it cannot
    close a feedback loop it never observes.

The two engines do NOT share a 6N clock. L1 derives purge/transition/production
from the calendar; the controller runs a stateful phase machine advanced by how
fast the pairs actually drain, so it reaches production strictly later. The
guide therefore records L1's mode per week and lets the CONSUMER decide, at
runtime, whether the two agree enough to apply a ceiling.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

_PURGE_HOLD_WEEKS = 2       # mirrors global_planner_poc._PURGE_HOLD_WEEKS


@dataclass(frozen=True)
class GuideWeek:
    week_label: str
    count: float            # L1 harvested_count (FISH — the controller's unit)
    kg: float               # L1 harvested_kg (diagnostic only)
    l1_mode: str            # "purge" | "transition" | "production"


@dataclass
class HarvestGuide:
    """A conditioned per-week harvest curve + the semantics for following it.

    Weeks the guide cannot speak to are ABSENT, never zero. A zero would become
    a ceiling of zero and clamp genuine harvest to nothing — the single highest
    consequence mistake available in this design.
    """
    weeks: dict[str, GuideWeek]
    follow: str                     # "floor" | "full"
    band: float
    min_harvest: float
    max_harvest: float
    purge_lever: bool = True
    production_lever: bool = True
    n_l1_infeasible: int = 0
    n_dropped: int = 0
    source: str = ""
    ledger: list[str] = field(default_factory=list)

    def mode_for(self, label) -> Optional[str]:
        g = self.weeks.get(label) if label else None
        return g.l1_mode if g else None

    def count_for(self, label) -> Optional[float]:
        g = self.weeks.get(label) if label else None
        return g.count if g else None

    def note(self, msg: str) -> None:
        """Record what the guide did or deliberately did NOT do, for the audit."""
        self.ledger.append(msg)

    def target(self, label, base: float, min_hv: float, weekly_max: float,
               *, allow_ceiling: bool) -> float:
        """THE follow-semantics function. Returns `base` untouched for any week
        the guide does not cover, so an absent week is a true no-op.

        The floor half always applies. The ceiling half applies only in "full"
        mode AND when the caller says the two engines agree about this week —
        the caller owns that judgement because only it knows the controller's
        realized 6N phase.
        """
        g = self.count_for(label)
        if g is None or not math.isfinite(g):
            return base
        lo = g if self.follow == "floor" else g * (1.0 - self.band)
        lo = max(min_hv, lo)          # never below contract
        out = max(base, lo)
        if self.follow == "full" and allow_ceiling:
            hi = max(min_hv, g * (1.0 + self.band))
            if math.isfinite(weekly_max):
                hi = min(weekly_max, hi)
            hi = max(min_hv, hi)      # a ceiling can never sit under the floor
            out = min(out, hi)
        return min(out, weekly_max) if math.isfinite(weekly_max) else out


def _condition(rows, modes, min_hv: float, max_hv: float,
               min_frac: float, smooth_weeks: int):
    """Turn L1's raw trace into a usable curve. Returns (weeks, dropped, why)."""
    labels = [r.week_label for r in rows]
    raw = {r.week_label: (float(r.harvested_count or 0.0),
                          float(r.harvested_kg or 0.0)) for r in rows}

    drop: set[str] = set()
    why: dict[str, str] = {}

    # C1/C2 — L1 zeroes its DRAW in a transition week, which suppresses the
    # RELEASE (the reported harvest) _PURGE_HOLD_WEEKS later. Dropping only the
    # transition week itself would leave the actual zero-harvest weeks in place.
    for i, lbl in enumerate(labels):
        if modes.get(lbl) == "transition":
            for j in range(i, min(len(labels), i + 1 + _PURGE_HOLD_WEEKS)):
                drop.add(labels[j])
                why.setdefault(labels[j], "transition window (L1 draw zeroed)")

    # C3 — structural dropouts: startup priming, tail, and anything else that
    # collapses toward zero. Same 25% threshold the crater gate uses.
    floor_gate = min_frac * min_hv if min_hv > 0 else 0.0
    for lbl in labels:
        if raw[lbl][0] < floor_gate:
            drop.add(lbl)
            why.setdefault(lbl, f"L1 harvest below {min_frac:.0%} of the floor")

    # C4 — L1 leaves fish in the hold at the horizon end; the tail is artifact.
    for lbl in labels[-_PURGE_HOLD_WEEKS:]:
        drop.add(lbl)
        why.setdefault(lbl, "horizon tail (fish still in the L1 hold)")

    kept = [lbl for lbl in labels if lbl not in drop]

    # C6 — optional centered rolling mean. OFF by default: L1's peaks are
    # cap-driven and are the signal, not noise.
    counts = {lbl: raw[lbl][0] for lbl in kept}
    if smooth_weeks and smooth_weeks > 1 and kept:
        half = smooth_weeks // 2
        smoothed = {}
        for i, lbl in enumerate(kept):
            lo, hi = max(0, i - half), min(len(kept), i + half + 1)
            win = [counts[k] for k in kept[lo:hi]]
            smoothed[lbl] = sum(win) / len(win)
        counts = smoothed

    weeks = {}
    for lbl in kept:
        # C5 — clip to the contract bounds.
        c = counts[lbl]
        if max_hv > 0:
            c = min(c, max_hv)
        c = max(c, min_hv)
        weeks[lbl] = GuideWeek(week_label=lbl, count=c, kg=raw[lbl][1],
                               l1_mode=modes.get(lbl, "purge"))
    return weeks, drop, why


def build_harvest_guide(*, control, tables, facility, batches,
                        inflight_og=None, fw_inflight=None, purge_inflight=None,
                        fw_by_label=None, facility_limits=None,
                        purge_release_schedule=None, manual_window_weeks=0
                        ) -> Optional[HarvestGuide]:
    """Run standalone L1 once and condition its harvest curve.

    Returns None — never raises — whenever the guide would be unusable. The
    caller then runs as the plain controller.

    `manual_window_weeks` / `purge_release_schedule`: the manual-override-window
    semantics (see global_planner_poc.plan) — when the guide is built AFTER a
    scripted window, L1 must not assume any unscripted pre-start 6N staging, so
    its envelope agrees with what the realized controller can actually release
    at the handoff.
    """
    follow = str(getattr(control, "hybrid_follow", "off") or "off").lower()
    if follow not in ("floor", "full"):
        return None

    min_hv = float(getattr(control, "min_harvest_per_week", 0) or 0)
    max_hv = float(getattr(control, "max_harvest_per_week", 0) or 0)
    if min_hv <= 0:
        return None                 # nothing to floor against

    purge_lever = bool(getattr(control, "hybrid_purge_lever", True))
    production_lever = bool(getattr(control, "hybrid_production_lever", True))

    # sixn_level_drains is what structurally stops a raised move-in from
    # accumulating into ONE pair and starving the others (the documented
    # 90-113k drain-spike backfire). The hybrid must never be what removes it.
    notes: list[str] = []
    if purge_lever and not bool(getattr(control, "sixn_level_drains", False)):
        purge_lever = False
        notes.append("purge lever REFUSED: sixn_level_drains is off, which is "
                     "the guard against over-filling one 6N pair; enable it to "
                     "let the hybrid steer purge weeks")

    try:
        from . import global_planner_poc as gpp
        res = gpp.plan(
            batches, tables, control, facility,
            inflight_og=inflight_og or {},
            record_standing=False,
            model_purge_hold=True,
            model_full_facility=True,
            fw_inflight=fw_inflight or {},
            purge_inflight=purge_inflight or {},
            purge_release_schedule=purge_release_schedule,
            manual_window_weeks=int(manual_window_weeks or 0),
            fw_by_label=fw_by_label,
            biomass_ceiling=_per_week_bio_ceiling(control, facility_limits),
        )
    except Exception as e:  # noqa: BLE001 — never let the guide break a run
        return None if not notes else HarvestGuide(
            weeks={}, follow=follow, band=0.0, min_harvest=min_hv,
            max_harvest=max_hv, purge_lever=False, production_lever=False,
            source=f"L1 failed ({type(e).__name__}: {e}) — plain controller",
            ledger=notes)

    rows = list(getattr(res, "trace", []) or [])
    if not rows:
        return None
    modes = {p.week_label: p.mode for p in (getattr(res, "purge_trace", []) or [])}

    weeks, dropped, why = _condition(
        rows, modes, min_hv, max_hv,
        float(getattr(control, "hybrid_guide_min_frac", 0.25) or 0.0),
        int(getattr(control, "hybrid_guide_smooth_weeks", 0) or 0))

    # Too little signal to steer by — fall back rather than half-steer.
    if len(weeks) < 0.5 * len(rows):
        return None

    n_infeasible = len(getattr(res, "infeasible_weeks", []) or [])
    guide = HarvestGuide(
        weeks=weeks, follow=follow,
        band=float(getattr(control, "hybrid_follow_band", 0.10) or 0.0),
        min_harvest=min_hv, max_harvest=max_hv,
        purge_lever=purge_lever, production_lever=production_lever,
        n_l1_infeasible=n_infeasible, n_dropped=len(dropped),
        source=(f"L1 envelope over {len(rows)} weeks — {len(weeks)} usable, "
                f"{len(dropped)} dropped; follow={follow} "
                f"band=±{float(getattr(control, 'hybrid_follow_band', 0.10)):.0%}; "
                f"levers purge={purge_lever} production={production_lever}; "
                f"L1 flagged {n_infeasible} week(s) infeasible (expected — its "
                f"cap verdict, not its harvest quantity)"),
        ledger=notes)
    for lbl in sorted(dropped):
        guide.note(f"{lbl}: guide not applied — {why.get(lbl, 'dropped')}")
    return guide


def _per_week_bio_ceiling(control, facility_limits):
    """Per-week biomass ceilings so L1 sees the SAME cap the controller does.

    None (the flat-cap path) unless the scenario actually overrides biomass by
    week, keeping L1 byte-identical to its other callers in the common case.
    """
    if facility_limits is None:
        return None
    try:
        from .caps import METRIC_BIOMASS, resolve_facility_cap
        from .time_grid import forecast_week_labels
        labels = forecast_week_labels(control.forecast_start,
                                      control.horizon_weeks)
        out, flat = {}, None
        for lbl in labels:
            cap = resolve_facility_cap(METRIC_BIOMASS, lbl, facility_limits,
                                       control)
            cap = cap[0] if isinstance(cap, tuple) else cap
            out[lbl] = cap
            flat = cap if flat is None else flat
        if all(abs(v - flat) < 1e-9 for v in out.values()):
            return None            # no per-week overrides — keep the flat path
        return out
    except Exception:  # noqa: BLE001 — a ceiling is an optimization, not a need
        return None
