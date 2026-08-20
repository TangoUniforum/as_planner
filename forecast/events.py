"""The 5 logged event types (DESIGN §3).

Every count change in a tank is one of:
  - TranOGEntry      FW single-stream -> N OG tanks (2 size classes)
  - Transfer         1 source tank -> 1+ destinations
  - Grade            N source tanks -> N+1 destination tanks (or 2->2)
  - Harvest          1 source tank -> processing (partial allowed)
  - GradedHarvest    1 source -> 1 pickup tank + 1 retention tank,
                     pickup harvested same window

Mortality and growth are continuous (no event row). Their daily
application lives on TankState.

Each event class is a plain dataclass + an `apply(state)` method that
mutates the FacilityState in place and returns a list of warnings
(empty list when fully successful). Hard violations (e.g. INV-4)
return a warning but do NOT mutate state for that row.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .state import FacilityState, STAGE_FW, STAGE_SW, STAGE_STARVE
from .tiers import (ENTRY_SPLIT_MAX_WT_G, ENTRY_SYSTEMS, harvest_allowed,
                    sixn_exit_allowed)

# Backward-compatible aliases — the tier constants now live in tiers.py
# (single source of truth for the operator transfer rules R1-R6).
# Avg-weight threshold above which intra-OG1/2 tank moves are illegal (R3/INV-4).
OG12_MOVE_LOCK_WT_G = ENTRY_SPLIT_MAX_WT_G

# Systems considered "OG1/OG2" (the entry tier). FacilityConfig uses
# OG{1,2}{N,S} identifiers; SystemLimits uses {1,2}{N,S}. Membership uses
# FacilityConfig's identifiers (events operate on TankState).
OG12_SYSTEMS = ENTRY_SYSTEMS

# Sentinel "from" identifier for TranOG entries (FW pre-cull stream is not a
# physical tank).
FROM_FW_STREAM = "FW"


@dataclass
class TankAllocation:
    """One tank's share of a multi-destination event."""
    tank_id: int
    count: float
    avg_wt_g: float
    cv_pct: float
    size_class: str = ""  # "big" / "small" / "" — informational only


# ============================================================
# Event 1: TranOGEntry — FW single-stream -> N OG tanks
# ============================================================

@dataclass
class TranOGEntry:
    """Stock a freshly-cull-completed batch into N OG1/OG2 tanks.

    Caller supplies `destinations` derived from the placement decision
    + the `SizeClassSplit` produced by biology at TranOG.
    """
    batch_id: str
    event_date: date
    destinations: list[TankAllocation]
    # Fish actually stocked, and fish this event planned to stock but could
    # not. Populated by apply(). Before these existed the event had NO
    # accounting of any kind: a refused destination silently dropped that
    # share of the cohort while every downstream consumer went on reporting
    # the PLANNED destination sum as delivered (reproduced 2026-08-20:
    # 1,200,000 planned across two tanks, one refused as non-empty,
    # 600,000 actually stocked, 1,200,000 reported).
    count_placed: float = 0.0          # populated by apply()
    count_refused: float = 0.0         # populated by apply()

    def apply(self, state: FacilityState) -> list[str]:
        warns: list[str] = []
        placed = 0.0
        for dest in self.destinations:
            tank = state.tanks_by_id.get(dest.tank_id)
            if tank is None:
                warns.append(f"TranOG {self.batch_id}: unknown tank #{dest.tank_id}")
                continue
            if not tank.is_empty:
                warns.append(
                    f"TranOG {self.batch_id}: tank {tank.location_id} not empty "
                    f"(holds batch {tank.batch_id})"
                )
                continue
            if tank.type != "OG":
                warns.append(
                    f"TranOG {self.batch_id}: tank {tank.location_id} is not OG "
                    f"(type={tank.type})"
                )
                continue
            tank.assign(
                batch_id=self.batch_id,
                count=dest.count,
                avg_wt_g=dest.avg_wt_g,
                cv_pct=dest.cv_pct,
                stage=STAGE_SW,
            )
            placed += dest.count

        planned = sum(d.count for d in self.destinations)
        self.count_placed = placed
        self.count_refused = max(0.0, planned - placed)
        if self.count_refused > 0:
            warns.append(
                f"TranOG {self.batch_id}: COHORT SHORT-STOCKED — placed "
                f"{placed:.0f} of {planned:.0f} planned; {self.count_refused:.0f} "
                f"fish had no tank to enter and are NOT in the facility"
            )
        return warns


# ============================================================
# Event 2: Transfer — 1 source -> 1+ destinations
# ============================================================

@dataclass
class Transfer:
    """Move fish from one tank to one or more tanks.

    Handling mortality is applied to each destination's count
    (caller pre-multiplies count by (1 - handling_frac)).

    INV-4 enforcement: refuses any source-destination pair where both
    tanks are in OG1/OG2 and source avg_wt >= 1 kg.

    `count_transferred` is populated by `apply()` with the actual total
    moved (sum across non-refused destinations). Callers should treat
    count_transferred == 0 as "this event did not apply" and avoid
    recording it as a real transfer in outputs.
    """
    batch_id: str
    event_date: date
    source_tank_id: int
    destinations: list[TankAllocation]
    leaves_source_empty: bool = False  # True if source is fully drained
    count_transferred: float = 0.0     # populated by apply()
    # Fish this event PLANNED to move but could not, because a destination was
    # refused (INV-1, INV-4/R3, R4, RESERVED, unknown tank). Populated by
    # apply(). > 0 means the event applied PARTIALLY: neither honoured nor
    # refused. Callers that must not ship a half-applied plan should test this,
    # not count_transferred, which is non-zero for a partial.
    count_refused: float = 0.0         # populated by apply()
    # Source week-open avg weight (g), set only for 6N purge-mode move-ins where
    # the destination carries the GROWN (mid-week transfer) weight but the source
    # is drained by count at its week-open weight. The continuity audit debits the
    # source at THIS weight (not the grown dest avg) so the source balances; the
    # 4-day growth then shows as real injected biomass on the frozen 6N tank.
    source_avg_wt_g: Optional[float] = None

    def apply(self, state: FacilityState) -> list[str]:
        """Atomic: source is drained ONLY by the count of destinations that
        actually accepted fish. Refused destinations (INV-1, INV-4, etc.)
        leave the source's share intact, preserving fish-count continuity.
        """
        warns: list[str] = []
        src = state.tanks_by_id.get(self.source_tank_id)
        if src is None:
            return [f"Transfer {self.batch_id}: unknown source tank #{self.source_tank_id}"]
        if src.is_empty or src.batch_id != self.batch_id:
            warns.append(
                f"Transfer {self.batch_id}: source {src.location_id} holds "
                f"batch {src.batch_id} (count={src.count:.0f}); expected {self.batch_id}"
            )
            return warns

        # R7: 6N one-way commitment — fish in a 6N DEPURATION tank (stage
        # STARVE) may never transfer out; only harvest empties the tank.
        # Non-destructive refusal of the whole event (state unchanged,
        # count_transferred stays 0 so callers see "did not apply"). 6N
        # production-mode grow-out (stage SW) moves freely.
        if not sixn_exit_allowed(src.system_id, src.stage):
            warns.append(
                f"R7: refused transfer of batch {self.batch_id} out of 6N "
                f"depuration tank {src.location_id} — fish moved into 6N "
                f"may never transfer out, only harvest empties the tank"
            )
            return warns

        src_in_og12 = src.system_id in OG12_SYSTEMS
        src_above_lock = src.avg_wt_g >= OG12_MOVE_LOCK_WT_G

        total_dest_count = 0.0
        for dest in self.destinations:
            tgt = state.tanks_by_id.get(dest.tank_id)
            if tgt is None:
                warns.append(f"Transfer {self.batch_id}: unknown dest tank #{dest.tank_id}")
                continue

            # R4: NEVER backward — a non-entry source may not target an
            # entry-tier (OG1/2) destination, at any weight.
            tgt_in_og12 = tgt.system_id in OG12_SYSTEMS
            if tgt_in_og12 and not src_in_og12:
                warns.append(
                    f"R4: refused backward transfer of batch {self.batch_id} "
                    f"from {src.location_id} ({src.system_id}) to "
                    f"{tgt.location_id} ({tgt.system_id}) — a non-entry source "
                    f"may never target an entry-tier destination"
                )
                continue

            # R3 / INV-4: no intra-OG1/2 move above 1 kg.
            if src_in_og12 and tgt_in_og12 and src_above_lock:
                warns.append(
                    f"INV-4 (R3): refused intra-OG1/2 transfer of batch {self.batch_id} "
                    f"from {src.location_id} to {tgt.location_id} at "
                    f"{src.avg_wt_g:.0f}g (above 1 kg lock)"
                )
                continue

            if not tgt.is_empty and tgt.batch_id != self.batch_id:
                warns.append(
                    f"Transfer {self.batch_id}: dest {tgt.location_id} holds different "
                    f"batch {tgt.batch_id} (INV-1)"
                )
                continue

            # RESERVED hold: an empty tank held for an imminent TranOG arrival
            # (anticipatory purge pacing) must not be re-stocked by any rebalancing
            # path. Refuse like an INV violation — the source's share stays put
            # (continuity-safe; the caller retains the residual in place). Only
            # blocks stocking an EMPTY reserved tank; reserved tanks are empty by
            # construction, so this never blocks a same-batch top-up. No-op unless
            # the anticipatory pass populated state.reserved_tanks.
            if tgt.is_empty and dest.tank_id in state.reserved_tanks:
                warns.append(
                    f"Transfer {self.batch_id}: dest {tgt.location_id} is RESERVED "
                    f"for an imminent TranOG arrival; refused (held empty)"
                )
                continue

            if tgt.is_empty:
                tgt.assign(
                    batch_id=self.batch_id,
                    count=dest.count,
                    avg_wt_g=dest.avg_wt_g,
                    cv_pct=dest.cv_pct,
                    stage=src.stage,
                )
            else:
                new_count = tgt.count + dest.count
                if new_count > 0:
                    tgt.avg_wt_g = (tgt.count * tgt.avg_wt_g + dest.count * dest.avg_wt_g) / new_count
                tgt.count = new_count

            total_dest_count += dest.count

        # Drain source by what was ACTUALLY transferred (not what was
        # planned). leaves_source_empty applies only when transfers
        # succeeded — refused destinations don't drain the source.
        planned_dest_count = sum(d.count for d in self.destinations)
        self.count_refused = max(0.0, planned_dest_count - total_dest_count)
        fully_applied = self.count_refused <= 1e-9

        src.count = max(0.0, src.count - total_dest_count)
        # leaves_source_empty is a FULL-DRAIN intent: it exists to clear the
        # rounding residue when every destination accepted. If any destination
        # was refused, the residue is not rounding — it is the refused
        # destination's fish, still physically in the source tank. Emptying
        # then DELETES them (reproduced 2026-08-20: source 100,000, one leg of
        # two refused on INV-1, 50,000 fish annihilated, and the loss passes
        # the hard conservation gate because that gate string-scans for
        # "DROP"/"OVER-PRODUCED" while the continuity audit calls it
        # COUNT_DRIFT on a sheet no gate reads). The `src.count <= 0.5` arm is
        # the physically correct self-empty and stands on its own.
        if total_dest_count > 0 and ((self.leaves_source_empty and fully_applied)
                                     or src.count <= 0.5):
            src.empty(self.event_date)

        if self.count_refused > 0:
            warns.append(
                f"Transfer {self.batch_id}: PARTIALLY APPLIED — moved "
                f"{total_dest_count:.0f} of {planned_dest_count:.0f} planned; "
                f"{self.count_refused:.0f} fish stayed in {src.location_id} "
                f"because a destination was refused"
            )

        self.count_transferred = total_dest_count
        return warns


# ============================================================
# Event 3: Grade — N sources -> N+1 destinations (or 2->2)
# ============================================================

@dataclass
class Grade:
    """Size-sort N source tanks of one batch into N+1 destinations.

    Same one-batch-per-tank rule: all sources + destinations belong to
    the same batch_id. Cannot grade in place — sources must end empty.

    `destinations` carries the post-grade (count, avg_wt) per dest tank,
    typically with monotonically increasing avg_wt to represent the
    sorted size classes.
    """
    batch_id: str
    event_date: date
    source_tank_ids: list[int]
    destinations: list[TankAllocation]

    def apply(self, state: FacilityState) -> list[str]:
        warns: list[str] = []
        # Validate sources.
        srcs = []
        for sid in self.source_tank_ids:
            t = state.tanks_by_id.get(sid)
            if t is None:
                warns.append(f"Grade {self.batch_id}: unknown source tank #{sid}")
                continue
            if t.batch_id != self.batch_id:
                warns.append(
                    f"Grade {self.batch_id}: source {t.location_id} holds batch "
                    f"{t.batch_id}, expected {self.batch_id}"
                )
                continue
            srcs.append(t)
        # Validate destinations.
        dests_resolved = []
        for dest in self.destinations:
            t = state.tanks_by_id.get(dest.tank_id)
            if t is None:
                warns.append(f"Grade {self.batch_id}: unknown dest tank #{dest.tank_id}")
                continue
            if not t.is_empty and t.tank_id not in self.source_tank_ids:
                warns.append(
                    f"Grade {self.batch_id}: dest {t.location_id} not empty "
                    f"(holds batch {t.batch_id})"
                )
                continue
            dests_resolved.append((t, dest))

        # R7: a grade whose SOURCE is a 6N depuration tank (STARVE) would move
        # committed fish out by the side door — refuse the whole event
        # (atomic, state unchanged). Only harvest empties a depuration tank.
        _r7_srcs = [s for s in srcs
                    if not sixn_exit_allowed(s.system_id, s.stage)]
        if _r7_srcs:
            warns.append(
                f"Grade {self.batch_id}: R7 — refused; sources "
                f"{[s.location_id for s in _r7_srcs]} are 6N depuration tanks "
                f"(fish moved into 6N may only be harvested)"
            )
            return warns

        # INV-4: a grade that MOVES fish between OG1/2 tanks is illegal
        # once any participant is at >= 1 kg (equipment limit). A "move"
        # means a destination that is NOT one of the sources — fish kept
        # in their own source tank are not being rearranged. Grade is
        # atomic — refuse the whole event rather than partial-apply.
        src_ids = {s.tank_id for s in srcs}
        og12_srcs_locked = [
            s for s in srcs
            if s.system_id in OG12_SYSTEMS and s.avg_wt_g >= OG12_MOVE_LOCK_WT_G
        ]
        og12_external_dests = [
            t for (t, _d) in dests_resolved
            if t.system_id in OG12_SYSTEMS and t.tank_id not in src_ids
        ]
        if og12_srcs_locked and og12_external_dests:
            warns.append(
                f"Grade {self.batch_id}: INV-4 violation — sources "
                f"{[s.location_id for s in og12_srcs_locked]} >= 1 kg "
                f"and destinations include external OG1/2 tanks "
                f"{[t.location_id for t in og12_external_dests]}; refused"
            )
            return warns

        # INV-3 (count conservation): destinations must match sources.
        src_count = sum(s.count for s in srcs)
        dest_count = sum(d.count for (_t, d) in dests_resolved)
        if abs(src_count - dest_count) > 0.5:
            warns.append(
                f"Grade {self.batch_id}: count not conserved — sources "
                f"hold {src_count:.0f} fish, destinations sum {dest_count:.0f} "
                f"(diff {dest_count - src_count:+.0f}); refused"
            )
            return warns

        # Apply: empty all sources, stock destinations.
        # Stage carried from the first valid source.
        stage = srcs[0].stage if srcs else STAGE_SW
        for s in srcs:
            s.empty(self.event_date)
        for tgt, dest in dests_resolved:
            tgt.assign(
                batch_id=self.batch_id,
                count=dest.count,
                avg_wt_g=dest.avg_wt_g,
                cv_pct=dest.cv_pct,
                stage=stage,
            )
        return warns


# ============================================================
# Event 4: Harvest — 1 source -> processing
# ============================================================

@dataclass
class Harvest:
    """Remove fish from a tank to processing.

    Partial harvest leaves the same batch in the source tank with
    reduced count. Full harvest empties the tank.

    `min_tank_control` (passed by caller, default 0) triggers
    force-empty: if remaining count would fall below the threshold,
    the whole tank is harvested instead of the partial amount.
    """
    batch_id: str
    event_date: date
    source_tank_id: int
    count: float
    avg_wt_g: float
    min_tank_control: float = 0.0

    def apply(self, state: FacilityState) -> list[str]:
        warns: list[str] = []
        src = state.tanks_by_id.get(self.source_tank_id)
        if src is None:
            return [f"Harvest {self.batch_id}: unknown source tank #{self.source_tank_id}"]
        if src.is_empty or src.batch_id != self.batch_id:
            warns.append(
                f"Harvest {self.batch_id}: source {src.location_id} holds "
                f"batch {src.batch_id}; expected {self.batch_id}"
            )
            return warns

        # R5: no harvest FROM an entry-tier (OG1/2) tank — fish route forward
        # first. Non-destructive refusal (state unchanged, count zeroed so
        # callers see "did not apply").
        if not harvest_allowed(src.system_id):
            warns.append(
                f"R5: refused harvest of batch {self.batch_id} from "
                f"{src.location_id} ({src.system_id}) — fish can't be harvested "
                f"from the entry tier (OG1/2); move them forward first"
            )
            self.count = 0.0
            return warns

        take = min(self.count, src.count)
        remaining = src.count - take

        # INV-5 force-empty.
        if 0 < remaining < self.min_tank_control:
            take = src.count
            remaining = 0.0
            warns.append(
                f"INV-5 force-empty: harvest from {src.location_id} would leave "
                f"{src.count - self.count:.0f} fish < min_tank_control "
                f"{self.min_tank_control:.0f}; full tank taken"
            )

        if remaining <= 0:
            src.empty(self.event_date)
        else:
            src.count = remaining
            # avg_wt may shift slightly if the harvest took a non-mean
            # slice; for direct harvest we treat the remaining
            # distribution as unchanged in avg_wt.

        # Record what was ACTUALLY harvested, not what was requested.
        # `take` may exceed self.count when INV-5 force-empty fires, or
        # be less when the tank had fewer fish than requested. The audit
        # / reconciliation depends on harvest_events reflecting reality.
        self.count = take

        return warns


# ============================================================
# Event 5: GradedHarvest — 1 source -> pickup tank + retention tank
# ============================================================

@dataclass
class GradedHarvest:
    """Grade a source tank into a harvest-pickup tank + retention tank.

    The pickup tank receives the >= harvest-weight portion and is then
    harvested (separate Harvest event chained by the planner). The
    retention tank receives the < harvest-weight portion and continues
    growing.

    Both destination tanks belong to the same batch_id as the source
    (one-batch-per-tank preserved).
    """
    batch_id: str
    event_date: date
    source_tank_id: int
    pickup_tank_id: int
    pickup_count: float
    pickup_avg_wt_g: float
    retention_tank_id: int
    retention_count: float
    retention_avg_wt_g: float
    cv_pct: float = 0.0
    # Pre-growth pickup weight (g). When the pickup is grown a few SW days before
    # the mid-week transfer (6N purge move-in), pickup_avg_wt_g carries the GROWN
    # weight but the SOURCE held the fish at this pre-growth weight. The continuity
    # audit debits the source at this weight so the +N-day growth shows as injected
    # biomass on the (frozen) pickup tank instead of over-debiting the source.
    pickup_source_avg_wt_g: Optional[float] = None

    def apply(self, state: FacilityState) -> list[str]:
        warns: list[str] = []
        src = state.tanks_by_id.get(self.source_tank_id)
        pickup = state.tanks_by_id.get(self.pickup_tank_id)
        retention = state.tanks_by_id.get(self.retention_tank_id)
        if src is None or pickup is None or retention is None:
            return [
                f"GradedHarvest {self.batch_id}: tank lookup failed "
                f"(src={self.source_tank_id}, pickup={self.pickup_tank_id}, "
                f"retention={self.retention_tank_id})"
            ]
        if src.batch_id != self.batch_id:
            warns.append(
                f"GradedHarvest {self.batch_id}: source {src.location_id} holds "
                f"batch {src.batch_id}"
            )
            return warns
        # R5: no harvest / 6N staging FROM an entry-tier (OG1/2) tank — a
        # GradedHarvest is precisely that (pickup routes to harvest or 6N
        # depuration). Non-destructive refusal, state unchanged.
        if not harvest_allowed(src.system_id):
            warns.append(
                f"R5: refused graded harvest of batch {self.batch_id} from "
                f"{src.location_id} ({src.system_id}) — fish can't be harvested "
                f"or staged to 6N from the entry tier (OG1/2); move them "
                f"forward first"
            )
            return warns
        # Pickup may already hold this batch (cross-tank accumulation) or be empty.
        if not pickup.is_empty and pickup.batch_id != self.batch_id:
            warns.append(
                f"GradedHarvest {self.batch_id}: pickup {pickup.location_id} holds "
                f"different batch {pickup.batch_id}"
            )
            return warns
        # Retention may already hold this batch (accumulated smalls) or be empty.
        if not retention.is_empty and retention.batch_id != self.batch_id:
            warns.append(
                f"GradedHarvest {self.batch_id}: retention {retention.location_id} "
                f"holds different batch {retention.batch_id}"
            )
            return warns

        stage = src.stage

        # Top up or assign pickup.
        if pickup.is_empty:
            pickup.assign(
                batch_id=self.batch_id,
                count=self.pickup_count,
                avg_wt_g=self.pickup_avg_wt_g,
                cv_pct=self.cv_pct,
                stage=stage,
            )
        else:
            new_count = pickup.count + self.pickup_count
            pickup.avg_wt_g = (
                (pickup.count * pickup.avg_wt_g + self.pickup_count * self.pickup_avg_wt_g)
                / new_count if new_count > 0 else pickup.avg_wt_g
            )
            pickup.count = new_count

        # SMALL-STAYS-IN-SOURCE mode (grade-to-min top-up): when the retention tank
        # IS the source, the < harvest-weight portion simply remains in the source at
        # its lower mean — only the pickup (>= weight) tail is peeled to the 6N purge
        # tank. No separate retention tank is needed; the source is NOT drained, it
        # becomes the small portion. (Conserves: pickup_count + retention_count = the
        # source's original count.)
        if self.retention_tank_id == self.source_tank_id:
            src.assign(
                batch_id=self.batch_id, count=self.retention_count,
                avg_wt_g=self.retention_avg_wt_g, cv_pct=self.cv_pct, stage=stage,
            )
            return warns

        # Top up or assign retention.
        if retention.is_empty:
            retention.assign(
                batch_id=self.batch_id,
                count=self.retention_count,
                avg_wt_g=self.retention_avg_wt_g,
                cv_pct=self.cv_pct,
                stage=stage,
            )
        else:
            new_count = retention.count + self.retention_count
            retention.avg_wt_g = (
                (retention.count * retention.avg_wt_g + self.retention_count * self.retention_avg_wt_g)
                / new_count if new_count > 0 else retention.avg_wt_g
            )
            retention.count = new_count

        # Drain source.
        src.empty(self.event_date)
        return warns


# ============================================================
# Apply helper
# ============================================================

Event = TranOGEntry  # type alias placeholder; events are duck-typed by .apply()


def apply_events(state: FacilityState, events: list) -> list[str]:
    """Apply a sequence of events to state in order. Returns aggregated warnings."""
    warns: list[str] = []
    for ev in events:
        warns.extend(ev.apply(state))
    return warns
