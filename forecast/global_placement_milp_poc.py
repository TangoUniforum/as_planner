"""OPTIMAL per-tank placement MILP (the 'one answer' — slow-OK global mode).

Unlike the layered L3-LP + greedy tank-pick (heuristics that trade density vs
transfers), this solves the placement as a true mixed-integer LINEAR program by
modeling the BIOMASS in each tank directly (q), not the tank count — which keeps
density (q/vol <= cap) linear. Per week, sequentially (each week sees the prior
week's assignment, so it minimizes moves), it finds the optimal layout:

  vars (per eligible (batch b, tank t)):
    x[b,t] in {0,1}   - b occupies t
    q[b,t] >= 0       - kg of b in t
    over[b,t] >= 0    - kg over the per-tank density cap (penalized)
    tr[b,t] >= 0      - REAL fish (kfish) moved INTO t = max(0, count - prev_count),
                        count = q/avg_wt (growth-robust, captures within-tank shuffle)
  + per-system slacks sbio[s], sfeed[s] >= 0
  constraints:
    one batch/tank:    sum_b x[b,t] <= 1
    place all biomass: sum_t q[b,t] = bio[b]
    link + density:    q[b,t] <= BIG*x[b,t];  over[b,t] >= q[b,t] - cap_kg[t]
    system caps:       sum_(b,t in s) q[b,t] <= bio_cap + sbio[s]   (feed likewise)
    conveyor:          x[b,t] = 0 if t not eligible for b's tier
    swap-free (0-drift): x[b,t] <= x_prev[b,t] + empty_prev[t]
    transfer:          tr[b,t] >= x[b,t] - x_prev[b,t]
  objective: minimize  W_SLACK*sum(sbio+sfeed) + W_OVER*sum(over) + sum(tr)
             (caps + density dominate; among those, fewest transfers — the
              optimum, not a heuristic trade-off)

This module is a STANDALONE PoC (additive); nothing in production imports it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .caps import METRIC_BIOMASS, METRIC_FEED_DAY, SystemLimits, resolve_system_cap
from .global_planner_l2_poc import GROWOUT_SYSTEMS, NURSERY_SYSTEMS
from .global_planner_poc import PlannerResult
from .models import ControlParams, FacilityConfig

_DEFAULT_BIO_CAP = 400000.0
_DEFAULT_FEED_CAP = 3000.0
W_SLACK = 1.0e7     # meet per-system caps first
W_SWAP = 1.0e6      # then avoid same-week A->B swaps (each would cause drift)
W_OVER = 1.0e3      # then minimize per-tank over-density
# (transfers have weight 1.0 — the last priority)


@dataclass
class MilpWeek:
    week: int
    week_label: str
    q: dict             # (batch_id, tank_id) -> kg
    transfers: int
    over_kg: float
    sys_slack_kg: float
    status: int


def _eligible_tanks(avg_wt_g, og_tanks, growout_ids, nursery_ids):
    """Tanks a batch may occupy this week (conveyor): >=1kg -> grow-out (may spill
    to nursery); <1kg -> nursery only."""
    if avg_wt_g >= 1000.0:
        return [t for t in og_tanks if og_tanks[t] in growout_ids
                or og_tanks[t] in nursery_ids]
    return [t for t in og_tanks if og_tanks[t] in nursery_ids]


def solve_week(
    bw, prev_count, og_tanks, tank_vol, week_label, system_limits, control,
    np, linprog, time_limit=8.0, mip_rel_gap=0.01,
):
    """Solve one week's placement MILP. `bw` = {batch_id: (bio_kg, feed_kg_day,
    avg_wt_g)}; `prev_count` = {(batch_id, tank_id): kfish last week}.
    Returns (q dict, fish_moved_kfish, over_kg, sys_slack_kg, status)."""
    from scipy.sparse import coo_matrix
    prev_x = set(prev_count)                  # (batch,tank) occupied last week
    occupied_prev = {t for (_, t) in prev_count}   # tanks held by ANY batch last week
    growout = set(GROWOUT_SYSTEMS)
    nursery = set(NURSERY_SYSTEMS)
    dens_cap = control.density_target_pct  # used only for the SOFT operating ref
    # per-tank hard cap kg = max_density * volume (the 95 kg/m3 welfare cap)
    cap_kg = {t: tank_vol[t] for t in og_tanks}   # tank_vol already = max_density*vol

    # ---- variable index: x, q, over, tr per eligible (b,t); sbio,sfeed per sys.
    bt = []                                   # (batch_id, tank_id)
    for b, (bio, feed, avg) in bw.items():
        if bio <= 1e-9:
            continue
        for t in _eligible_tanks(avg, og_tanks, growout, nursery):
            bt.append((b, t))
    if not bt:
        return {}, 0, 0.0, 0.0, 0
    n = len(bt)
    bt_idx = {bt[i]: i for i in range(n)}
    systems = sorted({og_tanks[t] for t in og_tanks})
    sys_idx = {s: i for i, s in enumerate(systems)}
    ns = len(systems)
    OFF_X, OFF_Q, OFF_OV, OFF_TR = 0, n, 2 * n, 3 * n
    OFF_SB, OFF_SF = 4 * n, 4 * n + ns
    OFF_SW = 4 * n + 2 * ns          # SOFT swap (penalized, keeps every week feasible)
    nv = 5 * n + 2 * ns

    er, ec, ev, beq = [], [], [], []
    ur, uc, uv, bub = [], [], [], []
    R = 0   # ub row counter
    E = 0   # eq row counter

    # place all biomass: sum_t q[b,t] = bio[b]
    by_b = {}
    for i, (b, t) in enumerate(bt):
        by_b.setdefault(b, []).append(i)
    for b, idxs in by_b.items():
        for i in idxs:
            er.append(E); ec.append(OFF_Q + i); ev.append(1.0)
        beq.append(bw[b][0]); E += 1

    # one batch per tank: sum_b x[b,t] <= 1
    by_t = {}
    for i, (b, t) in enumerate(bt):
        by_t.setdefault(t, []).append(i)
    for t, idxs in by_t.items():
        for i in idxs:
            ur.append(R); uc.append(OFF_X + i); uv.append(1.0)
        bub.append(1.0); R += 1

    for i, (b, t) in enumerate(bt):
        bigM = bw[b][0]                       # batch can't exceed its own biomass
        # q - BIG*x <= 0
        ur.append(R); uc.append(OFF_Q + i); uv.append(1.0)
        ur.append(R); uc.append(OFF_X + i); uv.append(-bigM)
        bub.append(0.0); R += 1
        # q - over <= cap_kg[t]   (over >= q - cap)
        ur.append(R); uc.append(OFF_Q + i); uv.append(1.0)
        ur.append(R); uc.append(OFF_OV + i); uv.append(-1.0)
        bub.append(cap_kg[t]); R += 1
        # MOVE-IN objective in REAL fish (kfish): tr >= count[b,t] - prev_count,
        # count = q/avg_wt. This is the actual transfer measure (fish moved into a
        # tank) — it captures within-tank shuffle, and is robust to GROWTH (count
        # is conserved as fish grow) and MORTALITY (only lowers count, so the
        # max(0,.) ignores it). Replaces the tank-ENTRY proxy.
        avg = bw[b][2]
        ur.append(R); uc.append(OFF_Q + i); uv.append(1.0 / (avg * 1000.0))
        ur.append(R); uc.append(OFF_TR + i); uv.append(-1.0)
        bub.append(prev_count.get((b, t), 0.0)); R += 1
        # SOFT swap-free: x - sw <= x_prev[b,t] + empty_prev[t]  (sw>0 = a swap,
        # heavily penalized but allowed so the tight weeks stay FEASIBLE).
        empty_prev = 0.0 if t in occupied_prev else 1.0   # truly empty = no batch last wk
        x_prev_bt = 1.0 if (b, t) in prev_x else 0.0
        ur.append(R); uc.append(OFF_X + i); uv.append(1.0)
        ur.append(R); uc.append(OFF_SW + i); uv.append(-1.0)
        bub.append(x_prev_bt + empty_prev); R += 1

    # per-system caps (soft): sum q <= bio_cap + sbio ; feed <= feed_cap + sfeed
    for s in systems:
        sb = _scap(METRIC_BIOMASS, week_label, s, system_limits, _DEFAULT_BIO_CAP)
        sf = _scap(METRIC_FEED_DAY, week_label, s, system_limits, _DEFAULT_FEED_CAP)
        idxs = [i for i, (b, t) in enumerate(bt) if og_tanks[t] == s]
        if not idxs:
            continue
        for i in idxs:
            ur.append(R); uc.append(OFF_Q + i); uv.append(1.0)
        ur.append(R); uc.append(OFF_SB + sys_idx[s]); uv.append(-1.0)
        bub.append(sb); R += 1
        for i in idxs:
            b = bt[i][0]
            sfr = (bw[b][1] / bw[b][0]) if bw[b][0] > 0 else 0.0   # feed per kg
            ur.append(R); uc.append(OFF_Q + i); uv.append(sfr)
        ur.append(R); uc.append(OFF_SF + sys_idx[s]); uv.append(-1.0)
        bub.append(sf); R += 1

    c = np.zeros(nv)
    c[OFF_TR:OFF_TR + n] = 1.0
    c[OFF_OV:OFF_OV + n] = W_OVER
    c[OFF_SB:OFF_SB + ns] = W_SLACK
    c[OFF_SF:OFF_SF + ns] = W_SLACK
    c[OFF_SW:OFF_SW + n] = W_SWAP
    integ = np.zeros(nv)
    integ[OFF_X:OFF_X + n] = 1               # only occupancy is integer; move-in is kfish
    A_eq = coo_matrix((ev, (er, ec)), shape=(E, nv)) if er else coo_matrix((E, nv))
    A_ub = coo_matrix((uv, (ur, uc)), shape=(R, nv)) if ur else coo_matrix((R, nv))
    res = linprog(c, A_ub=A_ub, b_ub=np.array(bub), A_eq=A_eq, b_eq=np.array(beq),
                  bounds=[(0.0, None)] * nv, method="highs", integrality=integ,
                  options={"time_limit": time_limit, "mip_rel_gap": mip_rel_gap})
    if res.x is None:
        return {}, 0, 0.0, 0.0, res.status
    x = res.x
    # keep ALL placed biomass (the >1kg cut was display-only and made nearly-
    # harvested batches read as 'unplaced' dust — conservation must be exact).
    q = {bt[i]: x[OFF_Q + i] for i in range(n) if x[OFF_Q + i] > 1e-6}
    moved_kfish = float(x[OFF_TR:OFF_TR + n].sum())   # REAL fish moved in (thousands)
    over = float(x[OFF_OV:OFF_OV + n].sum())
    sslk = float(x[OFF_SB:OFF_SB + ns].sum() + x[OFF_SF:OFF_SF + ns].sum())
    return q, moved_kfish, over, sslk, res.status


def _scap(metric, wl, s, system_limits, default):
    v = resolve_system_cap(metric, wl, s, system_limits)
    return v if v is not None else default
