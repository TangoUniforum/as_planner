# LP-Guided LNS Placement — an opt-in, switchable optimal-placement engine

**Status (2026-09-09):** Phase 1 of §7 is **built and live** as the method key
`controller-lns` (`placement_method: "lns"`) — `forecast/lns_placement.py`, called
from `forecast/run.py` Stage 2.5, documented in USER_GUIDE §11. What ships is a
**solver-free** pass: it finds the hottest grow-out (system, week), then relocates a
whole (batch, tank, weeks) occupancy segment to a free tank in a cooler system, or
1:1-swaps it with a lighter segment of the same week span. Each edit is a **relabel**
of the realized layout (batch_locations plus every event that names the tank) rather
than an extra `Transfer`, gated on the real continuity audit (0 drift) and input
conservation, and accepted only if it never raises the hot-spot peak and strictly
improves the lexicographic (peak, over-cap area, balance CV) objective.

Phases 2–4 of §7 — the CP-SAT window repair and the **LP guidance this title names** —
are not built, and are not buildable as written. That LP was always internal to *this*
design (an LP relaxation over the placement model in §2, used to choose neighborhoods);
it was never the Global method's LP. But OR-tools and every CP-SAT / MILP / LP solve
were removed from the project on 2026-09-10 together with the Global method (commit
932d018), which was the only caller — CP-SAT there went infeasible on ~81% of weeks behind a silent
uncapped fallback. §§2–3 and phases 2–4 stand below as the design of record; read them
as an unbuilt proposal that would have to re-argue its solver dependency first (§8).

**Original goal (2026-06-13):** Add a near-optimal **placement** engine that drives
per-system hot spots toward their provable floor — *beside* the current greedy
heuristic, never replacing it. The operator flips a switch to use it or not.

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
- If the LNS pass fails or finds nothing worth doing — no beneficial move, a failed
  final audit, any exception — it **falls back to the greedy plan** — so turning it on
  can never make the forecast worse or unrunnable. "Hopefully we haven't lost anything,
  just added more" is enforced *by construction*: greedy is always the floor.

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
warm start and moves the layout toward the optimum — in the §§2–3 design by emitting
**additional conserved `Transfer` events**, so that every move is a normal `Transfer`
through `apply()` and the existing **continuity gate applies unchanged**. Either way
LNS literally cannot create or lose a fish (see §5).

```
  precalc canvas ─► greedy Phase-D ─► (placement_method=="lns"?) ─► reports
   (unchanged)       (unchanged,        │ no  → emit as today
                      = warm start)     │ yes → LNS refine pass:
                                        │        hot-spot relocate / swap,
                                        │        relabel the realized layout
                                        └─────► same FacilityState → same 24 sheets
```

As built, the pass **relabels** occupancy instead of emitting extra `Transfer`s: it
rewrites `batch_locations` and every event that names a moved tank, then re-runs the
real `write_tank_continuity_audit` reconciliation and reverts anything that drifts.
Continuity is therefore checked the same way, but an `lns` run costs no extra handling
mortality and no handling budget — its transfer count is comparable with the plain
controller's. The Transfer-emitting variant described above belongs to the unbuilt
solver phases (§§2–3).

---

## 2. The optimization model (the "repair" solved exactly)

*Unbuilt — this is the design of record for phases 2–4, which need a solver the
project no longer carries (§8). What ships is described in the status note and §7.1.*

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

*Unbuilt, as §2 — steps 1 and 3's hot-spot targeting shipped in a greedy, solver-free
form; the LP relaxation and the MILP repair did not.*

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

Added to `ControlParams` (`models.py`), all defaulting to today's behavior.

**Shipped:**

| Knob | Default | Meaning |
|---|---|---|
| `placement_method` | `"greedy"` | `"lns"` turns on the refinement pass |
| `lns_max_moves` | `30` | budget: relocations/swaps accepted per run |

**Designed for the unbuilt solver phases (§7.2–4), never added** (defaults as
specified, kept for the record): `lns_neighborhood` = `"hotspot"` (`"window"` rolling
vs `"hotspot"` targeted), `lns_window_weeks` = `6` (rolling window size),
`lns_time_budget_s` = `60` (wall-clock cap per solve, then take best-so-far),
`lns_seed` = `0` (fixes neighborhood randomness), `lns_co_tenancy` = `false` (allow 2
batches per tank; off = one per tank as today). The shipped pass needs none of
them — it has no solve to time-box and no randomness to seed, and it targets the hot
spot directly; `lns_max_moves` took their place as its only budget.

`config_io.control_from_dict` already tolerates unknown keys, so old configs/templates
load unchanged. Access via `getattr(control, "...", default)` (same pattern as
`harvest_level_load`).

---

## 5. Hard gates (the two the operator keeps repeating)

- **Continuity / conservation — checked, not assumed.** As built, each candidate edit
  relabels the tank on the occupancy rows and on every event that names it (including
  the realized-biology keys the shipped audit reconciles against), then re-runs the
  real `write_tank_continuity_audit` and counts `TANK_DRIFT` + `BIO_DRIFT`; anything
  but 0, or any batch missing from `batch_locations`, and the edit is reverted. The
  same two audits (`TankContinuityAudit`, `InputConservationAudit`) then gate the run
  *exactly* as they gate greedy, and a final pass re-checks both reconciliations —
  modelled and ground-truth — before the edited plan is returned. In the unbuilt
  solver design this was instead guaranteed by construction: constraint (1) places
  every batch's exact biomass each week, every move is a normal `Transfer` through
  `events.apply()`, and a solve that cannot preserve mass is infeasible → greedy.
  Either way: **you cannot lose a fish.**
- **Determinism.** The shipped pass has no solver and no randomness: segments are
  built and sorted canonically, grow-out systems and their tanks are visited in
  sorted order, and candidate segments are ranked by (biomass at the hot week,
  batch_id, tank_id) — so the output is identical across `PYTHONHASHSEED`. Guarded by
  the existing `test_engine_deterministic_across_hash_seeds`. (A solver phase would
  have to re-establish this on its own; that is what `lns_seed` in §4 was for.)
- **Measure-or-revert.** `tests/test_lns_placement.py` drives `refine_realized` on a
  synthetic hot system that has a free tank in a cooler one, and asserts: the segment
  relocates, the peak drops, the continuity audit stays at 0 drift, the
  realized-biology keys move with the tank, and the engine refuses a move that would
  push a legal system over cap, breach per-tank density, or buy a peak drop with more
  over-cap area. `peak_lns ≤ peak_greedy` is structural (`_better` never accepts a
  higher peak), and determinism is covered by the regression test above; measure a
  real config with `python -m tools.lns_measure` (greedy vs lns: hot spot,
  weeks-over-cap, drift, determinism). If a config makes LNS worse, the
  accept-only-if-better rule + greedy fallback keep the shipped plan ≥ greedy.

---

## 6. Optimizer integration (free — it's just another knob)

Because `placement_method` is a Control knob, the existing optimizer sweeps it with no
new code: the grid carries `("lns-placement", {"placement_method": "lns"})`, and
coordinate descent can toggle it. The **best-of-both** search then compares greedy vs
LNS *and* every knob combo, and `recommend()` returns whichever wins under the chosen
emphasis — so the operator gets "use LNS or not" answered *by the optimizer*,
measured, per scenario.

---

## 7. Build phases (each phase keeps `"greedy"` byte-identical)

1. **Scaffold + switch** — **DONE.** The knobs, `forecast/lns_placement.py`, and the
   `placement_method=="lns"` call site. Regression + determinism are byte-identical
   with the switch off. It did not stay a no-op with it on: the module ships
   `refine_realized`, which does audit-gated hot-spot relocation and 1:1 segment
   swaps on the realized layout, budgeted by `lns_max_moves`. No solver.
2. **Model + single-window MILP repair** (OR-tools CP-SAT) — **not built**, and now
   blocked: the project no longer carries a solver (§8).
3. **Rolling LNS** across the horizon (`lns_neighborhood="window"`) — **not built**;
   same blocker.
4. **LP guidance + hot-spot neighborhoods.** Add the LP relaxation to pick neighborhoods
   and warm-start repairs; add `"hotspot"` mode. **Not built**; same blocker. (Hot-spot
   *targeting* did ship in phase 1 — it is greedy, chosen by the reported
   SystemLimitsAudit peak, not by an LP relaxation.)
5. **Optimizer + app + docs** — **DONE.** `("lns-placement", {"placement_method":
   "lns"})` is in the `optimize.py` grid, the app exposes `placement_method` and
   `lns_max_moves`, and USER_GUIDE §11 documents it. Greedy default unchanged.

Each phase is independently shippable and measure-or-revert; abandoning at any phase
leaves the greedy default untouched.

## 8. Dependencies, scale, risk

- **Dependency: none as shipped.** `forecast/lns_placement.py` imports nothing beyond
  the standard library, the project's own modules, and `openpyxl` (already required —
  it runs the real continuity audit it gates on), so turning `placement_method="lns"`
  on installs nothing. The solver this design assumed —
  Google OR-tools (CP-SAT), or PuLP+CBC — is **no longer a project dependency**: it
  was removed on 2026-09-10 with the Global method, its only caller. Reviving phases
  2–4 means re-adding and re-pinning it *and* answering the measurement that got it
  dropped: CP-SAT infeasible on ~81% of weeks, with a silent uncapped fallback
  labelled "optimal".
- **Scale (unbuilt solver phase):** a 6-week × 33-tank × ~8-active-batch window is a
  few thousand binaries — CP-SAT solves that in seconds. The rolling/LNS loop keeps
  every solve that size.
- **Risk + fallback:** the shipped pass has three exits, each printing a line and
  leaving the greedy plan standing — no beneficial relocation found (the usual result
  on a capacity-bound config), the final continuity/conservation gate failing, or any
  exception at all, which the `run.py` call site catches. Opt-in + fallback = zero
  downside.

## 9. Critical files (all additive)

- `forecast/lns_placement.py` — the hot-spot metric (`system_peak` / `system_score`,
  mirroring the reported SystemLimitsAudit), the segment model, relocate + swap, the
  `_relabel` rewrite of layout and events, and the audit gate.
- `forecast/models.py` — the opt-in knobs (§4).
- `forecast/run.py` — **one** call site, Stage 2.5: after the greedy pass,
  `if placement_method == "lns": lns_placement.refine_realized(placement, ...)`.
  Nothing else changes. (The design put this in `placement.py`; it landed in `run.py`
  because the pass needs the realized layout, not the canvas plan.)
- `forecast/optimize.py` — `placement_method` in the grid as `lns-placement` (one line).
- `tests/test_lns_placement.py` — conservation (0 drift) + relocation correctness +
  the refusal cases (§5); `tools/lns_measure.py` — greedy vs lns on a real config.
- `docs/USER_GUIDE.md` §11. No `requirements` change — the shipped pass needs no solver.

## 10. Why it's worth it (honest ROI)

The greedy + leveling already runs at 94–97% utilization with a small residual, so on
*today's* config LNS chases the last few % of hot spots. Measured, that is what it
gets: on the capacity-bound live config the shipped pass usually finds no beneficial
relocation and no-ops — every grow-out tank is occupied at the peak, so there is
nowhere cooler to move a segment to, and greedy is already at the capacity floor. Its
value shows up on a config with slack.

The rest of the original case belonged to the solver phases: a *provably*
near-optimal, PR-agnostic layout (trust + robustness across future PRs), and a gain
that grows on a harder PR — a tighter stocking plan, a future facility expansion, a
worse starting state — where the greedy floor sits further from optimal. The shipped
pass proves nothing about optimality; it is a greedy hot-spot search with an audit
gate. That case is the argument for reviving phases 2–4, and it now has to be weighed
against re-introducing the solver dependency (§8).

What holds either way: it gives nothing up. The greedy plan is always the warm start
and the fallback.
