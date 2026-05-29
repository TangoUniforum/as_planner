# Greenfield reservation-grid scheduler — design

> **STATUS 2026-05-29: BUILT + SHELVED.** Implemented as
> `_build_reservation_plan` (behind `RESERVATION_PLAN=1` flag on branch
> `feature/reservation-scheduler`). One focused tuning pass done. Runs
> clean (0 drift, all TranOG placed) but underperforms the incremental
> coordinator and tuning didn't close the gap:
>
> | Approach | Violations | Worst |
> |---|---|---|
> | Incremental coordinator (default) | **212** | 185 |
> | Reservation FIFO (first cut) | 348 | 194 |
> | Reservation FIFO + sticky tuning | 344 | 208 |
> | Reservation peak-order + tuning | 383 | 203 |
>
> Root cause: cohort-outer greedy commits the future by cohort priority,
> under-allocating cohorts that lose the race (B47/B44/B45/B48 far worse
> than under the incremental's per-week + even-out). Span/order/
> stickiness levers don't help (per-week marking yields the same grid).
> The dominant residual is operator-side PR concentration, which neither
> approach fixes — so the cohort-outer rewrite was never justified.
> **Decision: keep the incremental coordinator (212) as the shipping
> default; preserve this as a documented capability for a future
> global/ILP exploration or a cleaner workbook.**

Forward-looking design for the greenfield `Python/forecast/` coordinator
(see `GREENFIELD_COORDINATOR_LOCKS.md` for the current incremental
coordinator). Originally NOT YET IMPLEMENTED. This document specifies the
anticipatory tank-reservation scheduler that replaces the incremental
per-week event loop's reactive acquisition with forward reservation.

---

## 1. Problem this solves

The current coordinator is **reactive**: it walks weeks and, at each
week, tops a batch up toward its needed tank count from whatever is
free *that week*. Consequence (the "B46 class"): a big cohort that
needs many grow-out tanks loses the contention race — other cohorts
took the OG3-6 tanks first — so it's under-allocated, concentrates,
and (once >1 kg) is frozen by INV-4. The even-out pass fixes
*unevenness* but cannot fix *under-allocation*: if all of a cohort's
tanks are over cap, there's no under-tank to level into; it needs
*more tanks*, which weren't reserved for it.

Empirical anchor (2027-W06): facility at 100% of the 3,900 t biomass
cap but only ~68% of physical density capacity (~5,720 t). OG5N at
573 t (~111 kg/m³, over) while OG6S sits at 159 t (~31 kg/m³). The
room exists; the tanks just weren't committed to the right cohort
early enough.

**Anticipatory fix:** plan each cohort's full grow-out tank trajectory
up front and RESERVE the specific (tank, week) cells it will need, so a
big cohort's future tanks are held for it and it's never starved by a
later cohort grabbing them first.

## 2. What this is NOT (the failed naive version)

Lifetime-max sizing (tried + removed, Q-COORD.A→G): every batch
claimed its PEAK tank count for ALL weeks. It over-subscribed OG3-6 at
W20 because all peaks were demanded simultaneously even though peaks
occur at different times. **Reservation must reserve the RIGHT count
PER week, not the peak count for every week.** Tanks are time-shared:
a tank a cohort needs at W40 can serve a different cohort at W20.

## 3. Core model — the (tank × week) grid

```
              W20  W21  W22  ...  W40  ...  Wend
   tank 31    B43  B43  B43  ...  free ...  B50
   tank 32    free B47  B47  ...  B47  ...  free
   ...
```

- `grid[tank][week] -> batch_id | None` — reservation per cell.
- A cohort `c` has a known demand curve `N_c(w)` = tanks needed at week
  `w` (from biology biomass / 95 kg/m³ density math — already computed
  as `tanks_needed_at_density_cap`).
- Reservation = assign `N_c(w)` specific cells `grid[·][w] = c` for every
  week `w` in `c`'s grow-out span, choosing tanks that are free for the
  weeks needed.
- Cells are **released** as the cohort harvests down (N_c(w) shrinks),
  returning them to the pool for later cohorts — this is the
  time-sharing that prevents over-subscription.

## 4. Algorithm (deterministic, single pass over cohorts)

```
1. Build each cohort's grow-out demand curve N_c(w) and per-tank
   biomass curve b_c(w) (both from batch_week_facts).
   Grow-out span = [first SW week >= 1 kg .. last week with biomass].
   (Nursery/OG1-2 phase handled by the existing TranOG + sub-1 kg
   logic; reservation governs the OG3-6 grow-out phase.)

2. Order cohorts by a deterministic priority. Candidates:
   - FIFO by input_date (operational fact), OR
   - largest-peak-demand first (big cohorts pick first — better
     packing, since they're the constrained ones).
   Pick ONE rule; document it. Try both offline, lock the better.

3. For each cohort c in priority order:
   a. For each grow-out week w, it needs N_c(w) cells.
   b. Choose a SYSTEM using forward-peak (the staggering rule already
      built, Q-COORD.H): the system whose existing reserved load curve
      + b_c(w) has the lowest resulting peak.
   c. Within the chosen system, reserve specific tanks that are free
      across c's needed weeks (lowest tank_id first, deterministic).
   d. As N_c(w) steps down (harvest), stop reserving the shed tanks
      from that week on (release back to the grid).
   e. If insufficient free cells exist for some week -> Bottleneck
      (honest infeasibility surfaced at plan-build time).

4. The grid IS the assignment plan: assignment_plan[(c, w)] =
   {tanks reserved for c at w}. Feed it to the migration-plan diff
   layer exactly as today (Phase B/C/D unchanged).
```

## 5. Integration with existing pieces

- **Demand curves** — reuse `tanks_needed_at_density_cap` + biomass
  curves from `_build_batch_week_facts`. No new biology.
- **Forward-peak staggering (Q-COORD.H)** — becomes the system-choice
  step (4b). Already built; just called during reservation instead of
  incremental GROWTH_ADD.
- **Exit-at-1 kg (Q-COORD.G)** — the grow-out span starts at the 1 kg
  crossing; reservation governs OG3-6. The OG1/2 nursery phase keeps
  its current handling (TranOG placement + sub-1 kg within-nursery).
- **Even-out (Q-COORD.I)** — still runs in Phase D as a safety net for
  residual unevenness, but with proper reservation it should rarely
  fire (cohorts get enough tanks up front).
- **Sticky / transfer-minimization** — reservation is inherently
  sticky: a cohort reserves the SAME tanks across consecutive weeks
  unless its count changes. Week-over-week diff churn is minimized by
  construction.
- **6N purge** — OG6N still excluded from the reservable grid
  (pipeline-owned in purge mode), as today.

## 6. Determinism & defensibility

- Single forward pass over cohorts in a fixed priority order; within
  each cohort, fixed system-choice (forward-peak) + fixed tank-pick
  (lowest free id). Same plan every run.
- No scoring weights. The only "objective" terms are the forward-peak
  system choice (single criterion: minimize resulting peak) and the
  priority order (single operational rule).
- Every reserved cell traces to: cohort priority + forward-peak system
  + lowest-free-tank. Fully explainable in the Advisory.

## 7. Risks / open questions

1. **Greedy reservation order is not globally optimal.** Processing
   cohorts one at a time means an early cohort can reserve cells a
   later, more-constrained cohort needed. Mitigation: choose the
   priority order deliberately (largest-peak-first tends to pack
   better); accept it's a heuristic, not an optimum. Going fully
   optimal = ILP, which violates the precalc-first / no-solver stance.
2. **Reservation vs. harvest-schedule coupling.** N_c(w) depends on the
   harvest schedule (when biomass drops). Harvest is decided upstream
   (biomass-level). Reservation consumes that as given — but if
   reservation infeasibility suggests harvesting earlier would help,
   that's a signal back to the scheduler (future coupling, not v1).
3. **Pre-existing PR cohorts** start mid-grow-out already in tanks.
   Their reservation seeds from PR state at W20 (like PR_INIT today),
   then reservation governs forward. Over-concentrated PR cohorts
   (B46/B47) get their forward cells reserved so they can spread as
   they grow — the key win over the reactive coordinator.
4. **Build size.** This replaces the incremental event loop's
   acquisition with a grid reservation pass — a substantial rewrite of
   `_build_facility_assignment_plan`, though the surrounding pieces
   (facts, diff layer, Phase B/C/D, audits) are unchanged.

## 8. Build phases (proposed)

1. Grid data structure + demand/biomass curve extraction; reserve a
   single cohort end-to-end; verify its assignment matches expectation.
2. Full reservation pass over all cohorts (FIFO order) + bottleneck
   emission; wire to the migration-plan diff; regression vs current
   212-violation / 0-drift baseline.
3. Forward-peak system choice in the reservation step; measure system
   levelness + violations.
4. Priority-order experiment (FIFO vs largest-peak-first); lock the
   better; golden-cell + slow-test updates.
5. Docs + memory + (separately) merge decision.

## 9. Success criteria

- Violations < current 212 (target: the B46-class under-allocation
  dissolves because grow-out tanks are reserved up front).
- 0 count/biomass drift maintained.
- Per-system within-week biomass variance lower than current
  (staggering exercised during reservation).
- All decisions deterministic + traceable (no scoring weights).
- Honest bottlenecks where the grid is genuinely infeasible.
