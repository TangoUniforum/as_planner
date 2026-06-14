# LP-Guided LNS Placement — an opt-in, switchable optimal-placement engine

**Status:** Design draft (2026-06-13) — shelf-ready, not yet built.
**Goal:** Add a near-optimal **placement** engine that drives per-system hot spots
toward their provable floor — *beside* the current greedy heuristic, never replacing
it. The operator flips a switch to use it or not.

---

## 0. Non-negotiable principle: ADD, don't remove

Everything here is **additive and opt-in**. We do **not** delete or rewrite a single
existing function. The current pipeline — precalc canvas → greedy Phase-D realized
engine → `rebalance_level`/split/varqty → reports — stays exactly as it is and remains
the **default**. The LNS engine is a **new, switchable refinement layer** that runs
*only* when turned on.

- New control knob `placement_method: "greedy" | "lns"`, **default `"greedy"`**.
- `"greedy"` (default) ⇒ the pipeline is **byte-identical** to today. Determinism
  signature and the regression baseline are unchanged.
- `"lns"` ⇒ the greedy plan still runs first (it's the LNS **warm start**); LNS then
  *refines the tank assignment* on top of it.
- If LNS is ever unavailable (missing solver dependency, solve timeout, a worse
  result) it **falls back to the greedy plan** — so turning it on can never make the
  forecast worse or unrunnable. "Hopefully we haven't lost anything, just added more"
  is enforced *by construction*: greedy is always the floor.

---

## 1. Where it plugs in (and what it does NOT touch)

The LNS engine optimizes **one thing**: *which tanks each batch's biomass occupies
each week* — the spatial layout. It does **not** touch:

- biology (growth / mortality / FCR / culls) — `biology.py`, `precalc.py` projections,
- the harvest schedule / closed-loop controller — `harvest_scheduler.py`, the Phase-D
  setpoint + level-load logic,
- conservation accounting — `events.py` `Transfer/Harvest.apply`, the audits.

It is a **post-pass on the greedy realized state**: the greedy engine produces a
feasible `FacilityState` (the `batch_locations` we already write); LNS takes that as a
warm start and emits **additional conserved `Transfer` events** to move the layout
toward the optimum. Because every move is a normal `Transfer` through `apply()`, the
existing **continuity gate applies unchanged** — LNS literally cannot create or lose a
fish (see §5).

```
  precalc canvas ─► greedy Phase-D ─► (placement_method=="lns"?) ─► reports
   (unchanged)       (unchanged,        │ no  → emit as today
                      = warm start)     │ yes → LNS refine pass:
                                        │        destroy + LP-guided repair,
                                        │        emit extra conserved Transfers
                                        └─────► same FacilityState → same 24 sheets
```

---

## 2. The optimization model (the "repair" solved exactly)

Per **rolling window** of weeks `W = [t … t+h]` (so the model stays small — see §3),
with everything outside the window held fixed:

**Given (constants from biology + the greedy warm start):** each batch's per-week
biomass `B[b,w]` and avg weight (hence feed intensity), tank set + caps, system caps,
the fixed assignment on the window boundary.

**Decision variables:**
- `y[b,t,w] ∈ {0,1}` — batch `b` occupies tank `t` in week `w` (one batch per tank
  unless co-tenancy is enabled).
- `q[b,t,w] ≥ 0` — biomass (kg) of `b` in `t` in `w` (continuous), with
  `q ≤ y · tank_cap`.
- `move[b,t,w] ∈ {0,1}` — `b` enters/leaves `t` between `w-1` and `w` (linearized from
  `y` differences) — the transfer count to minimize.

**Constraints (all linear ⇒ MILP):**
1. **Mass placement:** `Σ_t q[b,t,w] = B[b,w]` — every batch's biomass each week is
   placed somewhere (the fish exist; this is **continuity in the model**).
2. **Density cap:** `Σ_b q[b,t,w] ≤ density_cap(t)`.
3. **System feed cap:** `Σ_{b,t∈s} feed(q,wt) ≤ feed_cap(s)` (feed is linear in biomass
   at a known weight).
4. **System biomass cap:** `Σ_{b,t∈s} q[b,t,w] ≤ biomass_cap(s)`.
5. **Move-lock:** `y[b,t,w] = 0` for `t` not in grow-out when `wt(b,w) ≥ 1 kg`.
6. **Continuity link:** window-boundary assignment fixed = the rest of the (greedy)
   plan, so the window splices in seamlessly.
7. **6N / depuration** carve-outs mirror the current rules.

**Objective (the "Minimize loads / no hot spots" goal, exactly):**
```
  minimize   α · peak  +  β · Σ move[b,t,w]  +  γ · Σ system_load_slack
  s.t.       peak ≥ load(s,w)/cap(s)   ∀ s,w     (minimax linearization → the HOT SPOT)
```
`peak` is the single hottest system-week load — driving it down is exactly your
objective; `Σ move` is handling (transfers); weights `α,β,γ` map to the optimizer
emphases (so "Minimize loads", "Walk the line", etc. set them).

---

## 3. The method: warm-started, LP-guided LNS

Monolithic MILP over 33 tanks × 140 weeks × ~20 batches is intractable. LNS makes it
practical:

1. **Warm start = the greedy plan.** Feasible from move 0; LNS only improves.
2. **LP relaxation, once per round.** Solve the full-horizon LP (continuous `y`) — cheap.
   Its fractional/over-tight cells reveal *where* the integer plan is far from the
   relaxed optimum, i.e. **which hot spots are relievable**.
3. **Destroy = pick a neighborhood, LP-guided:** unfix `y` for either
   - a **rolling time window** `[t…t+h]` (h≈4–8 weeks), swept across the horizon, or
   - a **hot-spot-targeted** set: the systems/weeks around the current worst `load(s,w)`
     the LP says can be cooled.
4. **Repair = MILP solve** the unfixed neighborhood (rest fixed), minimizing the §2
   objective. Accept if the global objective improves; else keep the incumbent.
5. **Iterate** until no neighborhood improves (or a wall-clock / round budget).
6. **Emit** the difference between the LNS layout and the greedy layout as conserved
   `Transfer` events.

This is "grid explores / descent exploits" taken to the placement layer: the LP gives
global direction; the MILP repair exploits it locally and **exactly**.

---

## 4. The switches (knobs — all additive)

Added to `ControlParams` (`models.py`), all defaulting to today's behavior:

| Knob | Default | Meaning |
|---|---|---|
| `placement_method` | `"greedy"` | `"lns"` turns on the refinement pass |
| `lns_neighborhood` | `"hotspot"` | `"window"` (rolling) or `"hotspot"` (targeted) |
| `lns_window_weeks` | `6` | window size for the rolling neighborhood |
| `lns_time_budget_s` | `60` | wall-clock cap per solve (then take best-so-far) |
| `lns_seed` | `0` | fixes neighborhood randomness → reproducible |
| `lns_co_tenancy` | `false` | allow 2 batches per tank (bigger search; off = current 1/ tank) |

`config_io.control_from_dict` already tolerates unknown keys, so old configs/templates
load unchanged. Access via `getattr(control, "...", default)` (same pattern as
`harvest_level_load`).

---

## 5. Hard gates (the two the operator keeps repeating)

- **Continuity / conservation — guaranteed by construction.** Constraint (1) places
  every batch's exact biomass each week, and every realized move is a normal
  `Transfer` through `events.apply()` — so `TankContinuityAudit` (0 drift) and
  `InputConservationAudit` (0 dropped/over-produced) gate the LNS output *exactly* as
  they gate the greedy. A solve that can't preserve mass is infeasible → fallback to
  greedy. **You cannot lose a fish.**
- **Determinism.** Fix `lns_seed`, single-thread the solver (CP-SAT/CBC are
  deterministic single-threaded), and order neighborhoods canonically → identical
  output across `PYTHONHASHSEED`. Guarded by the existing
  `test_engine_deterministic_across_hash_seeds`.
- **Measure-or-revert.** A new `tests/` fixture runs `placement_method="lns"` on the
  small workbook and asserts: 0 drift, 0 dropped, deterministic, and
  `peak_lns ≤ peak_greedy` (never worse on the objective). If a config makes LNS worse,
  the accept-only-if-better rule + greedy fallback keep the shipped plan ≥ greedy.

---

## 6. Optimizer integration (free — it's just another knob)

Because `placement_method` is a Control knob, the existing optimizer sweeps it with no
new code: add `("lns", {"placement_method": "lns"})` to the grid, or let coordinate
descent toggle it. The **best-of-both** search then compares greedy vs LNS *and* every
knob combo, and `recommend()` returns whichever wins under the chosen emphasis — so the
operator gets "use LNS or not" answered *by the optimizer*, measured, per scenario.

---

## 7. Build phases (each phase keeps `"greedy"` byte-identical)

1. **Scaffold + switch.** Add the knobs; add a `forecast/lns_placement.py` module with
   a no-op `refine(state, ...) -> state` wired in behind `placement_method=="lns"`.
   Prove regression + determinism **byte-identical** with the switch off, and a no-op
   with it on. (No solver yet.)
2. **Model + single-window MILP repair** (OR-tools CP-SAT). Solve ONE window from the
   greedy warm start, emit the diff as Transfers, gate on conservation. Measure peak vs
   greedy on one window.
3. **Rolling LNS** across the horizon (`lns_neighborhood="window"`). Full-horizon
   refinement; validate 0 drift / determinism / peak ≤ greedy.
4. **LP guidance + hot-spot neighborhoods.** Add the LP relaxation to pick neighborhoods
   and warm-start repairs; add `"hotspot"` mode. Measure the gain over plain rolling.
5. **Optimizer + app + docs.** Add `placement_method` to the grid; surface the choice in
   the optimizer results; document in USER_GUIDE (a new §11). Re-lock nothing — greedy
   default is unchanged.

Each phase is independently shippable and measure-or-revert; abandoning at any phase
leaves the greedy default untouched.

## 8. Dependencies, scale, risk

- **Dependency:** Google OR-tools (CP-SAT — Apache-2.0, pip-installable, deterministic
  single-thread) — or PuLP+CBC. Pin it; the engine imports lazily so the dependency is
  only needed when `placement_method="lns"`.
- **Scale:** a 6-week × 33-tank × ~8-active-batch window is a few thousand binaries —
  CP-SAT solves that in seconds. The rolling/LNS loop keeps every solve that size.
- **Risk + fallback:** solver missing / timeout / infeasible / worse-than-greedy → log a
  warning and **return the greedy plan**. Opt-in + fallback = zero downside.

## 9. Critical files (all additive)

- `forecast/lns_placement.py` **(new)** — the model, LP guidance, destroy/repair loop,
  greedy-diff → Transfer emission.
- `forecast/models.py` — the new opt-in knobs (§4).
- `forecast/placement.py` — **one** call site: after the greedy pass, `if lns: state =
  lns_placement.refine(state, ...)`. Nothing else changes.
- `forecast/optimize.py` — add `placement_method` to the grid (one line).
- `tests/test_lns_placement.py` **(new)** — conservation + determinism + peak≤greedy gate.
- `docs/USER_GUIDE.md` — new §11; `requirements` — add the pinned solver.

## 10. Why it's worth it (honest ROI)

The greedy + leveling already runs at 94–97% utilization with a small residual, so on
*today's* config LNS chases the last few % of hot spots **plus** delivers a
provably-near-optimal, PR-agnostic layout (trust + robustness across future PRs). On a
harder PR — a tighter stocking plan, a future facility expansion, a worse starting
state — the greedy floor is further from optimal and LNS's gain grows. It is the one
lever that breaks past the heuristic floor, and it does so without giving anything up:
the greedy plan is always the warm start and the fallback.
