"""Facility transfer-tier rules — the single source of truth.

Operator-defined PHYSICAL constraints on fish movement between OG systems,
judged on the SOURCE TANK'S AVERAGE weight:

  R1. FW/TranOG arrivals enter ONLY the entry tier (OG1N/OG1S/OG2N/OG2S).
  R2. From the entry tier, forward moves to any OG3/4/5/6 tank are allowed
      at ANY weight (6N additionally gated by the purge/production pipeline).
  R3. WITHIN the entry tier, moves are allowed only while the SOURCE tank's
      avg weight < 1000 g (equipment limit). At/above 1 kg entry-tier fish
      may only move FORWARD.
  R4. NEVER backward: a non-entry OG source may never target an entry-tier
      destination, at any weight (sub-1 kg included).
  R5. NO harvest and NO 6N staging FROM entry-tier tanks, ever — fish route
      forward first.
  R6. Fish >= 1000 g MAY remain in entry-tier tanks (stuck-in-place is legal;
      the >=1 kg overflow in OG1/2 is measured-necessary — never force-evict).
  R7. 6N ONE-WAY COMMITMENT: fish moved into a 6N depuration tank (stage
      STARVE) may NEVER transfer out — only harvest empties the tank. In 6N
      production mode (post-2028) the mains are ordinary grow-out (stage SW)
      and move freely; the commitment binds exactly while depurating.

Every other module imports these names (events.py keeps its historical
OG12_* aliases for backward compatibility). This module must stay
dependency-free (events.py imports it).
"""
from __future__ import annotations

# The entry tier: the only systems FW/TranOG arrivals may enter (R1).
# FacilityConfig system identifiers (SystemLimits uses {1,2}{N,S}).
ENTRY_SYSTEMS = frozenset({"OG1N", "OG1S", "OG2N", "OG2S"})

# Avg-weight threshold (g) above which intra-entry-tier moves are illegal (R3).
# Historically known as the "1 kg lock" / INV-4 / OG12_MOVE_LOCK_WT_G.
ENTRY_SPLIT_MAX_WT_G = 1000.0


def is_entry(system_id: str) -> bool:
    """True if `system_id` is an entry-tier (OG1/2) system."""
    return system_id in ENTRY_SYSTEMS


def move_allowed(src_system: str, dst_system: str,
                 src_avg_wt_g: float) -> tuple[bool, str]:
    """Is a transfer from `src_system` to `dst_system` legal (R2-R4)?

    Judged on the SOURCE tank's average weight. Returns (ok, reason);
    `reason` names the violated rule when ok is False, else "".

    Does NOT gate 6N purge/production ownership — that pipeline logic is
    layered on top by the callers (do not use this as the only 6N gate).
    """
    src_entry = src_system in ENTRY_SYSTEMS
    dst_entry = dst_system in ENTRY_SYSTEMS
    if not src_entry:
        if dst_entry:
            return (False,
                    f"R4: backward move {src_system}->{dst_system} — a non-entry "
                    f"source may never target an entry-tier (OG1/2) destination")
        return True, ""  # growout -> growout: unrestricted here
    if dst_entry:
        if src_avg_wt_g >= ENTRY_SPLIT_MAX_WT_G:
            return (False,
                    f"R3: intra-entry-tier move {src_system}->{dst_system} at "
                    f"{src_avg_wt_g:.0f}g — over the {ENTRY_SPLIT_MAX_WT_G:.0f}g "
                    f"equipment limit, entry-tier fish may only move forward")
        return True, ""
    return True, ""  # R2: entry -> growout, any weight


def harvest_allowed(system_id: str) -> bool:
    """R5: harvest (and 6N staging) is forbidden FROM entry-tier tanks."""
    return system_id not in ENTRY_SYSTEMS


# The 6N depuration system (FacilityConfig identifier). Kept here (not
# imported from state/sixn) so this module stays dependency-free.
SIXN_SYSTEM = "OG6N"

# state.STAGE_STARVE literal — tiers.py may not import state (dependency-free).
_STAGE_STARVE = "STARVE"


def sixn_exit_allowed(src_system: str, src_stage: str) -> bool:
    """R7: may fish LEAVE this tank by transfer?

    False exactly when the tank is a 6N depuration tank mid-commitment
    (stage STARVE): those fish may only be harvested. 6N production-mode
    grow-out (stage SW) and every non-6N tank move freely.
    """
    return not (src_system == SIXN_SYSTEM and src_stage == _STAGE_STARVE)
