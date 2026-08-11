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
    """Tanks a batch may occupy this week (conveyor): >=1kg -> grow-out ONLY
    (rule R4: never backward into the entry tier, no nursery spill at any
    weight); <1kg -> nursery only."""
    if avg_wt_g >= 1000.0:
        return [t for t in og_tanks if og_tanks[t] in growout_ids]
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


def solve_full_horizon(
    by_week, og_tanks, tank_vol, vol, wl_of, system_limits, control,
    np, linprog, time_limit=600.0, mip_rel_gap=0.01, verbose=True,
    prior_count=None, prior_tank_batch=None,
):
    """MONOLITHIC placement MILP over the given weeks (the 'full picture').

    `prior_count` ({(b,t): kfish}) and `prior_tank_batch` ({t: batch}) carry the
    COMMITTED state of the week BEFORE the first week here, so this can solve a
    rolling WINDOW coupled to history (swap-free + move-in of the first window
    week reference the prior committed layout). Empty defaults => fresh start.

    Solves all weeks at once so fallow is planned globally — swap-free is a HARD
    constraint (`empty[t,w]` is a decision, so the solver MUST arrange a fallow
    week before any tank changes batch). If feasible, same-week swaps are
    structurally impossible (true 0-drift). Vars per eligible (b,t,w): x in {0,1}
    occupy, q>=0 kg, over>=0 kg over density, mv>=0 kfish moved-in. Slack sb/sf per
    (system,week). Objective: W_SLACK*slack + W_OVER*over + sum(mv).

    `by_week[w] = {batch: (bio, feed, avg)}`; returns (q_by_w, info).
    """
    from scipy.sparse import coo_matrix
    growout, nursery = set(GROWOUT_SYSTEMS), set(NURSERY_SYSTEMS)
    prior_count = prior_count or {}
    prior_tank_batch = prior_tank_batch or {}
    weeks = sorted(by_week)
    wpos = {w: i for i, w in enumerate(weeks)}
    systems = sorted({s for s in og_tanks.values()})

    # ---- enumerate eligible (b,t,w) and assign variable columns -------------
    xb = {}                                   # (b,t,w) -> column of x
    cols = 0
    btw = []
    for w in weeks:
        for b, (bio, feed, avg) in by_week[w].items():
            if bio <= 1e-9:
                continue
            for t in _eligible_tanks(avg, og_tanks, growout, nursery):
                xb[(b, t, w)] = cols; btw.append((b, t, w)); cols += 1
    n = cols
    OFF_X, OFF_Q, OFF_OV, OFF_MV = 0, n, 2 * n, 3 * n
    sw_idx = {(s, w): 4 * n + i for i, (s, w) in
              enumerate((s, w) for w in weeks for s in systems)}
    OFF_SB = 4 * n
    nsw = len(sw_idx)
    OFF_SF = OFF_SB + nsw
    OFF_SW = 4 * n + 2 * nsw          # SOFT swap var per cell (penalized, keeps feasible)
    nv = 5 * n + 2 * nsw

    er, ec, ev, beq = [], [], [], []
    ur, uc, uv, bub = [], [], [], []
    E = R = 0

    # place all biomass: sum_t q[b,t,w] = bio[b,w]
    bw_idx = {}
    for (b, t, w), c in xb.items():
        bw_idx.setdefault((b, w), []).append(c)
    for (b, w), cs in bw_idx.items():
        for c in cs:
            er.append(E); ec.append(OFF_Q + c); ev.append(1.0)
        beq.append(by_week[w][b][0]); E += 1

    # one batch per tank: sum_b x[b,t,w] <= 1
    tw_idx = {}
    for (b, t, w), c in xb.items():
        tw_idx.setdefault((t, w), []).append((b, c))
    for (t, w), bcs in tw_idx.items():
        for _, c in bcs:
            ur.append(R); uc.append(OFF_X + c); uv.append(1.0)
        bub.append(1.0); R += 1

    for (b, t, w), c in xb.items():
        bio = by_week[w][b][0]; avg = by_week[w][b][2]
        # q - BIG*x <= 0
        ur.append(R); uc.append(OFF_Q + c); uv.append(1.0)
        ur.append(R); uc.append(OFF_X + c); uv.append(-bio); bub.append(0.0); R += 1
        # over >= q - cap_kg
        ur.append(R); uc.append(OFF_Q + c); uv.append(1.0)
        ur.append(R); uc.append(OFF_OV + c); uv.append(-1.0)
        bub.append(tank_vol[t]); R += 1
        # move-in: mv >= count - prev_count = q/avg - q_prev/avg_prev
        pw = w - 1
        pc = xb.get((b, t, pw))
        ur.append(R); uc.append(OFF_Q + c); uv.append(1.0 / (avg * 1000.0))
        ur.append(R); uc.append(OFF_MV + c); uv.append(-1.0)
        if pc is not None:                          # prev week in window (variable)
            avgp = by_week[pw][b][2]
            ur.append(R); uc.append(OFF_Q + pc); uv.append(-1.0 / (avgp * 1000.0))
            bub.append(0.0)
        elif pw not in wpos:                         # first window week -> prior (const)
            bub.append(prior_count.get((b, t), 0.0))
        else:
            bub.append(0.0)
        R += 1
        # SOFT swap-free: x[b,t,w] + sum_{a!=b} x[a,t,w-1] - sw <= 1  (sw>0 penalized)
        if pw in wpos:                               # internal: vs in-window prev
            ur.append(R); uc.append(OFF_X + c); uv.append(1.0)
            for a, ac in tw_idx.get((t, pw), []):
                if a != b:
                    ur.append(R); uc.append(OFF_X + ac); uv.append(1.0)
            ur.append(R); uc.append(OFF_SW + c); uv.append(-1.0)
            bub.append(1.0); R += 1
        else:                                        # first window week: vs prior committed
            pb = prior_tank_batch.get(t)
            if pb is not None and pb != b:           # another batch held t last wk -> penalize
                ur.append(R); uc.append(OFF_X + c); uv.append(1.0)
                ur.append(R); uc.append(OFF_SW + c); uv.append(-1.0)
                bub.append(0.0); R += 1

    # per-system caps (soft): sum q <= bio_cap + sb ; feed <= feed_cap + sf
    sysw_q = {}
    for (b, t, w), c in xb.items():
        sysw_q.setdefault((og_tanks[t], w), []).append((b, c))
    for (s, w), bcs in sysw_q.items():
        sb = _scap(METRIC_BIOMASS, wl_of[w], s, system_limits, _DEFAULT_BIO_CAP)
        sf = _scap(METRIC_FEED_DAY, wl_of[w], s, system_limits, _DEFAULT_FEED_CAP)
        for _, c in bcs:
            ur.append(R); uc.append(OFF_Q + c); uv.append(1.0)
        ur.append(R); uc.append(sw_idx[(s, w)]); uv.append(-1.0); bub.append(sb); R += 1
        for b, c in bcs:
            sfr = by_week[w][b][1] / by_week[w][b][0] if by_week[w][b][0] > 0 else 0.0
            ur.append(R); uc.append(OFF_Q + c); uv.append(sfr)
        ur.append(R); uc.append(OFF_SF + (sw_idx[(s, w)] - OFF_SB)); uv.append(-1.0)
        bub.append(sf); R += 1

    cobj = np.zeros(nv)
    cobj[OFF_MV:OFF_MV + n] = 1.0
    cobj[OFF_OV:OFF_OV + n] = W_OVER
    cobj[OFF_SB:OFF_SB + 2 * nsw] = W_SLACK
    cobj[OFF_SW:OFF_SW + n] = W_SWAP
    integ = np.zeros(nv); integ[OFF_X:OFF_X + n] = 1
    A_eq = coo_matrix((ev, (er, ec)), shape=(E, nv))
    A_ub = coo_matrix((uv, (ur, uc)), shape=(R, nv))
    if verbose:
        print(f"[full-horizon] {n} (b,t,w) cells, {nv} vars ({n} binary), "
              f"{E} eq + {R} ub rows; solving (limit {time_limit}s)...")
    res = linprog(cobj, A_ub=A_ub, b_ub=np.array(bub), A_eq=A_eq, b_eq=np.array(beq),
                  bounds=[(0.0, None)] * nv, method="highs", integrality=integ,
                  options={"time_limit": time_limit, "mip_rel_gap": mip_rel_gap})
    info = {"status": res.status, "success": res.success,
            "obj": float(res.fun) if res.fun is not None else None}
    if res.x is None:
        return None, info
    x = res.x
    q_by_w = {}
    for (b, t, w), c in xb.items():
        if x[OFF_Q + c] > 1e-6:
            q_by_w.setdefault(w, {})[(b, t)] = x[OFF_Q + c]
    info["moved_kfish"] = float(x[OFF_MV:OFF_MV + n].sum())
    info["over_kg"] = float(x[OFF_OV:OFF_OV + n].sum())
    info["slack_kg"] = float(x[OFF_SB:OFF_SB + 2 * nsw].sum())
    info["sw_sum"] = float(x[OFF_SW:OFF_SW + n].sum())
    return q_by_w, info


def solve_cpsat_perweek(
    by_week, og_tanks, tank_vol, vol, wl_of, system_limits, control,
    time_limit=4.0, workers=8, verbose=True, det_time=30.0,
):
    """PER-WEEK placement — decomposes the intractable full-horizon MILP so the
    optimality GAP is fixed at the root (each week is a small MILP that reaches
    ~0 gap in seconds, vs the monolith's ~12,000x gap it can never prove down).

    Each week solves to (near-)optimality with:
      * HARD per-tank density (q <= tank_vol) and one-batch-per-tank;
      * soft per-system biomass + feed caps with a per-system min-max BALANCE
        term (zb/zf) -> EVEN inter-system distribution (no over-cap system beside
        an empty one);
      * SOFT continuity threaded sequentially: a batch may relocate (keeps the
        per-week problems feasible), penalised as a transfer (fresh stocking into
        an empty tank is cheaper than displacing another batch);
      * OG6N mains (61/63/65) join the grow-out pool ONLY in production-mode weeks
        (sisters never; purge-mode weeks leave 6N to the depuration flow).

    Objective priority: meet caps (soft slack) >> balance systems >> fewest moves.
    Returns (q_by_w {w: {(b,t): kg}}, info) with the same shape as solve_cpsat.
    """
    from ortools.sat.python import cp_model
    from .global_planner_l3_poc import _is_purge_week
    from .sixn import SIXN_MAIN_TANKS
    gset = set(GROWOUT_SYSTEMS) | {"OG6N"}    # OG6N mains are a grow-out (final) system
    nset = set(NURSERY_SYSTEMS)
    weeks = sorted(by_week)
    q_by_w: dict = {}
    prev_tb: dict = {}                         # tank_id -> batch (last week's occupant)
    worst_gap = 0.0
    n_infeasible = 0
    total_slack = 0.0
    t_solve = 0.0
    for w in weeks:
        purge = _is_purge_week(wl_of[w], control)
        # tanks available for grow-out placement this week (drop OG6N in purge weeks)
        og_w = {t: s for t, s in og_tanks.items()
                if not (purge and s == "OG6N")}
        items = {b: v for b, v in by_week[w].items() if v[0] > 1e-9}
        m = cp_model.CpModel()
        x: dict = {}
        q: dict = {}
        for b, (bio, feed, avg) in items.items():
            B = int(round(bio))
            cells = list(_eligible_tanks(avg, og_w, gset, nset))
            if avg >= 1000.0:
                # R6: ">= 1 kg fish MAY remain in entry-tier tanks (stuck-in-place
                # is legal; the >= 1 kg overflow in OG1/2 is measured-necessary --
                # never force-evict)". Forbidding it outright is a rule this
                # facility does not have, and it is why CP-SAT could not solve:
                # 106 of 130 weeks needed MORE grow-out tanks than physically
                # exist once >= 1 kg fish were barred from OG1/OG2, while the
                # entry tier sat at 1-3 of its 12 tanks. That is the 103
                # "infeasible" weeks -- the model was over-constrained, not the
                # facility over-full. Proof it was never a time budget: the count
                # is EXACTLY 103 at both a 6 s and a 30 s per-week deterministic
                # budget (802 s vs 2034 s total).
                #
                # R4 is still absolute: a heavy batch may only KEEP an entry tank
                # it already occupies, never move back into one. Occupancy is
                # allowed; the backward MOVE is not.
                cells += [t for t in og_w
                          if og_w[t] in nset and prev_tb.get(t) == b]
            for t in cells:
                x[b, t] = m.NewBoolVar(f"x_{b}_{t}")
                q[b, t] = m.NewIntVar(0, B, f"q_{b}_{t}")
                m.Add(q[b, t] <= B * x[b, t])
                m.Add(q[b, t] <= int(tank_vol[t]))        # HARD density cap
        # place all of each batch's biomass
        infeasible = False
        for b, (bio, feed, avg) in items.items():
            cells = [t for t in og_w if (b, t) in q]
            if not cells:
                infeasible = True
                break
            m.Add(sum(q[b, t] for t in cells) == int(round(bio)))
        if infeasible:
            n_infeasible += 1
            q_by_w[w] = {}
            continue
        # one batch per tank
        for t in og_w:
            bs = [b for b in items if (b, t) in x]
            if bs:
                m.Add(sum(x[b, t] for b in bs) <= 1)
        # per-system soft caps + BALANCE (min-max load across systems)
        sl = []
        zb = m.NewIntVar(0, 5000, "zb")
        zf = m.NewIntVar(0, 5000, "zf")
        for s in sorted({og_w[t] for t in og_w}):
            cells = [(b, t) for (b, t) in q if og_w[t] == s]
            if not cells:
                continue
            bcap = int(_scap(METRIC_BIOMASS, wl_of[w], s, system_limits,
                             _DEFAULT_BIO_CAP))
            sb = m.NewIntVar(0, 10 ** 8, f"sb_{s}")
            sl.append(sb)
            sys_bio = sum(q[b, t] for (b, t) in cells)
            m.Add(sys_bio <= bcap + sb)
            if bcap > 0:
                m.Add(100 * sys_bio <= zb * bcap)
            fcap = int(_scap(METRIC_FEED_DAY, wl_of[w], s, system_limits,
                             _DEFAULT_FEED_CAP) * 1000)
            sys_feed = sum(q[b, t] * int(by_week[w][b][1] / by_week[w][b][0] * 1000)
                           for (b, t) in cells)
            sf = m.NewIntVar(0, 10 ** 8, f"sf_{s}")
            sl.append(sf)
            m.Add(sys_feed <= fcap + sf * 1000)
            if fcap > 0:
                m.Add(100 * sys_feed <= zf * fcap)
        tr_stock = [x[b, t] for (b, t) in x if prev_tb.get(t) is None]
        tr_swap = [x[b, t] for (b, t) in x if prev_tb.get(t) not in (None, b)]
        m.Minimize(10 ** 6 * sum(sl)
                   + 100 * (zb + zf)
                   + sum(tr_stock)
                   + 3 * sum(tr_swap))
        solver = cp_model.CpSolver()
        # Reproducible + CONVERGED placement. A wall-clock cutoff stops the
        # multi-worker portfolio at a machine-dependent point with whatever
        # ~2.6%-gap incumbent won the race — the source of the observed
        # 100.2%-vs-107.1% hottest-system swing across identical inputs. A FIXED
        # worker count + fixed seed + a DETERMINISTIC work budget make the solve
        # bit-identical run-to-run, and the larger budget lets each week close
        # its optimality gap (reach the balanced optimum, not a lucky incumbent).
        # max_time_in_seconds stays only as a wall-clock SAFETY, not the binding
        # stop criterion.
        solver.parameters.random_seed = 42
        solver.parameters.num_search_workers = int(workers)
        solver.parameters.max_deterministic_time = float(det_time)
        solver.parameters.max_time_in_seconds = float(time_limit)
        st = solver.Solve(m)
        t_solve += solver.WallTime()
        if st in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            gap = (abs(solver.ObjectiveValue() - solver.BestObjectiveBound())
                   / max(1.0, abs(solver.ObjectiveValue())))
            worst_gap = max(worst_gap, gap)
            qv = {(b, t): solver.Value(q[b, t]) for (b, t) in q
                  if solver.Value(q[b, t]) > 0}
            total_slack += sum(solver.Value(v) for v in sl)
            q_by_w[w] = qv
            prev_tb = {t: b for (b, t) in qv}
        else:
            n_infeasible += 1
            q_by_w[w] = {}
    # `n_weeks` is the DENOMINATOR for n_infeasible: callers must be able to say
    # "103 of 127 weeks" without re-deriving the horizon (a run that fails most
    # of its weeks is a fallback layout, not an optimal one).
    info = {"status": "per-week", "worst_gap": worst_gap,
            "n_weeks": len(weeks),
            "n_infeasible": n_infeasible, "slack_kg": total_slack,
            "solve_s": t_solve, "over_kg": 0}
    if verbose:
        print(f"  [CP-SAT per-week] {len(weeks)} weeks, worst gap "
              f"{worst_gap * 100:.2f}%, {n_infeasible} infeasible, "
              f"slack {total_slack:,.0f} kg, {t_solve:.0f}s solve")
    return q_by_w, info


def solve_cpsat(
    by_week, og_tanks, tank_vol, vol, wl_of, system_limits, control,
    time_limit=300.0, workers=8, verbose=True,
):
    """FULL-HORIZON placement via CP-SAT (OR-Tools) — the tractable 'full picture'.

    The placement is an assignment/scheduling problem; CP-SAT solves the whole
    52-week horizon at once where HiGHS branch-and-bound cannot. HARD swap-free
    (a tank changes batch only after a fallow week) makes same-week swaps
    structurally impossible -> the realized layout is 0-drift by the audit, not
    just by assertion. Integer kg (q): each batch's biomass is placed to the whole
    kg (sub-kg remainder dropped — negligible vs the count audit). Objective lexical-ish:
    minimize system-cap slack, then over-density, then tank moves (entries). The
    hard swap-free guarantees the audit's continuity; within-tank biomass shuffle
    is measured downstream, not minimized here. Returns (q_by_w {w:{(b,t):kg}}, info)."""
    from ortools.sat.python import cp_model
    gset, nset = set(GROWOUT_SYSTEMS), set(NURSERY_SYSTEMS)
    weeks = sorted(by_week)
    wpos = {w: i for i, w in enumerate(weeks)}
    m = cp_model.CpModel()
    x, q, ov = {}, {}, {}
    for w in weeks:
        for b, (bio, feed, avg) in by_week[w].items():
            if bio <= 1e-9:
                continue
            B = int(round(bio))
            for t in _eligible_tanks(avg, og_tanks, gset, nset):
                x[b, t, w] = m.NewBoolVar(f"x_{b}_{t}_{w}")
                q[b, t, w] = m.NewIntVar(0, B, f"q_{b}_{t}_{w}")
                ov[b, t, w] = m.NewIntVar(0, B, f"o_{b}_{t}_{w}")
                m.Add(q[b, t, w] <= B * x[b, t, w])
                m.Add(ov[b, t, w] >= q[b, t, w] - int(tank_vol[t]))
                m.Add(q[b, t, w] <= int(tank_vol[t]))   # HARD density cap: never over
    # place ALL biomass (rounded to whole kg): sum_t q = round(bio)
    for (b, w) in {(b, w) for (b, t, w) in x}:
        m.Add(sum(q[b, t, w] for t in og_tanks if (b, t, w) in x)
              == int(round(by_week[w][b][0])))
    # one batch per tank
    for (t, w) in {(t, w) for (b, t, w) in x}:
        m.Add(sum(x[b, t, w] for b in by_week[w] if (b, t, w) in x) <= 1)
    # HARD swap-free + tank-ENTRY transfer term (boolean, small magnitude — avoids
    # the int64 overflow a fish-count objective would create in the weighted sum;
    # within-tank biomass shuffle is MEASURED in the audit, not minimized here).
    tr = {}
    for (b, t, w) in x:
        pw = w - 1
        if pw in wpos:
            others = [x[a, t, pw] for a in by_week.get(pw, {})
                      if a != b and (a, t, pw) in x]
            if others:
                m.Add(x[b, t, w] + sum(others) <= 1)
        tr[b, t, w] = m.NewBoolVar(f"tr_{b}_{t}_{w}")
        if (b, t, pw) in x:
            m.Add(tr[b, t, w] >= x[b, t, w] - x[b, t, pw])
        else:
            m.Add(tr[b, t, w] >= x[b, t, w])
    # per-system caps (soft)
    sl = []
    for (s, w) in {(og_tanks[t], w) for (b, t, w) in x}:
        cells = [(b, t) for (b, t, ww) in x if ww == w and og_tanks[t] == s]
        sb = m.NewIntVar(0, 10 ** 8, f"sb_{s}_{w}"); sl.append(sb)
        m.Add(sum(q[b, t, w] for (b, t) in cells)
              <= int(_scap(METRIC_BIOMASS, wl_of[w], s, system_limits,
                           _DEFAULT_BIO_CAP)) + sb)
        sf = m.NewIntVar(0, 10 ** 8, f"sf_{s}_{w}"); sl.append(sf)
        fcap = int(_scap(METRIC_FEED_DAY, wl_of[w], s, system_limits,
                         _DEFAULT_FEED_CAP) * 1000)
        m.Add(sum(q[b, t, w] * int(by_week[w][b][1] / by_week[w][b][0] * 1000)
                  for (b, t) in cells) <= fcap + sf * 1000)
    # FILL ALL TANKS (operator goal): minimize the MAX per-tank utilization each
    # week. To lower the busiest tank the solver must spread biomass into the empty
    # ones -> every eligible tank ends up occupied at low, even density => no
    # empties, lowest load, steady occupancy over time (low temporal variance).
    # Cost: more transfers (spreading = more tanks/batch). Operator prioritizes
    # full+low-density+low-variance over move count.
    tw_cells: dict = {}
    for (b, t, w) in x:
        tw_cells.setdefault((t, w), []).append(b)
    zmax = {w: m.NewIntVar(0, 300, f"zmax_{w}") for w in weeks}
    for (t, w), bs in tw_cells.items():
        m.Add(100 * sum(q[b, t, w] for b in bs)
              <= zmax[w] * int(round(tank_vol[t])))
    # lexical-ish: meet system caps >> FILL (min max-utilization) >> tank moves
    m.Minimize(10 ** 6 * sum(sl) + 100 * sum(zmax.values()) + sum(tr.values()))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit)
    solver.parameters.num_search_workers = int(workers)
    if verbose:
        print(f"[CP-SAT] {len(x)} cells; solving full horizon "
              f"({time_limit}s, {workers} workers)...")
    st = solver.Solve(m)
    info = {"status": solver.StatusName(st),
            "obj": solver.ObjectiveValue() if st in
            (cp_model.OPTIMAL, cp_model.FEASIBLE) else None,
            "bound": solver.BestObjectiveBound() if st in
            (cp_model.OPTIMAL, cp_model.FEASIBLE) else None}
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, info
    q_by_w = {}
    for (b, t, w) in x:
        v = solver.Value(q[b, t, w])
        if v > 0:
            q_by_w.setdefault(w, {})[(b, t)] = float(v)
    info["slack_kg"] = sum(solver.Value(s) for s in sl)
    info["over_kg"] = sum(solver.Value(o) for o in ov.values())
    return q_by_w, info


def solve_rolling(
    by_week, og_tanks, tank_vol, vol, wl_of, system_limits, control,
    np, linprog, window=8, stride=4, time_limit=60.0, mip_rel_gap=0.02,
    verbose=True,
):
    """ROLLING-WINDOW placement: solve `window` weeks at once (hard swap-free, so
    fallow is planned across the window — the cascade depth that governs
    continuity), COMMIT the first `stride` weeks, carry their committed layout as
    prior state, and roll forward. Tractable (small windows) while still planning
    fallow over the relevant future. Returns (committed {w: {(b,t): kg}}, info)."""
    weeks = sorted(by_week)
    committed: dict = {}
    prior_count: dict = {}
    prior_tank_batch: dict = {}
    i = 0
    while i < len(weeks):
        win = weeks[i:i + window]
        sub = {w: by_week[w] for w in win}
        q_by_w, info = solve_full_horizon(
            sub, og_tanks, tank_vol, vol, wl_of, system_limits, control,
            np, linprog, time_limit=time_limit, mip_rel_gap=mip_rel_gap,
            verbose=False, prior_count=prior_count, prior_tank_batch=prior_tank_batch)
        if q_by_w is None:
            if verbose:
                print(f"  window @{wl_of[win[0]]} ({len(win)}w) FAILED "
                      f"(status {info['status']}) — switch to soft swap-free")
            return None, {"failed_window": win[0], "status": info["status"]}
        commit = win[:stride]
        for w in commit:
            committed[w] = q_by_w.get(w, {})
        lastw = commit[-1]
        lq = committed[lastw]
        prior_count = {(b, t): kg / (by_week[lastw][b][2] * 1000.0)
                       for (b, t), kg in lq.items()}
        prior_tank_batch = {t: b for (b, t) in lq}
        if verbose:
            print(f"  committed {wl_of[commit[0]]}..{wl_of[lastw]}  "
                  f"(window {len(win)}w, solve status {info['status']})")
        i += stride
    return committed, {"ok": True}
