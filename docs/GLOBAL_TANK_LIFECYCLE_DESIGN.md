# Global engine — tank-lifecycle planning

> **STATUS 2026-08-25: DESIGN, NOT BUILT.** Written for operator review
> before any code. Supersedes nothing yet. Sibling to
> `GREENFIELD_RESERVATION_SCHEDULER_DESIGN.md` (built + shelved for the
> incremental coordinator) — this is the global/ILP exploration that
> document explicitly parked its grid model for.

---

## 1. The problem, stated structurally

The Global engine is three stages. **L1** plans quantities (harvest and
purge staging per batch-week). **L3** assigns tank COUNTS. The **pick**
assigns physical tanks. L1 and L3 already iterate to convergence
(`max_iterations=10`, `result.converged`).

**The pick is not in that loop.** It runs once, afterwards, and its
findings — `unmet_harvest`, `depuration_warnings` — are collected as
warnings nobody acts on. It is a renderer, not a participant.

That single fact explains every failure of 2026-08-25. Enforcing a tank
rule requires the authority to say *"no, plan differently."* The pick can
only say *"here is what I could not do."* Four attempts to give it
decision-making power all deviated from the plan it is contracted to
render, and conservation broke every time:

| attempt | what it did | result |
|---|---|---|
| `min_tank_control` force-empty | draw MORE than L1 planned | reverted before shipping — would lose fish |
| anchored write | carry contents forward | 44% fewer tanks, **613 kg/m³** |
| + density relief | spread to assigned tanks | +512 transfers, R3 breaches 10 → 32 |
| 6N draw cap | draw LESS than L1 planned | **696 fish destroyed** |

The pick has no legitimate way to change the plan, so every change it makes
unilaterally is a conservation break wearing a different hat.

## 2. The operator's reframing (2026-08-25) — why repairs are the wrong shape

> *"Instead of harvest taking the whole tank, why not plan that tank to
> have a different number of fish from the start, or do a split earlier in
> that tank's life... properly planning each tank based on its planned life
> through its residence in the OGs?"*

"Take the whole tank" is a repair at the last moment. Planning the tank's
residence means the situation never arises: the tank is stocked, split and
grown so it ENDS at a count that harvests cleanly. The constraint is
satisfied by the plan's shape, not by a rule firing at week 40.

> *"There should be some interaction between all of the transfers... so
> that we are keeping within headroom for the tank and system and density
> limits."*

Transfers are currently decided one destination at a time, greedily. Joint
consideration is what makes headroom a planned property rather than an
emergent one.

**ASSUMPTION (needs operator confirmation):** a tank's residence is
naturally described as `(batch, start week, end week, entry count, exit
count)` with splits at defined points, and a "clean" ending means the tank
empties in one harvest rather than leaving a sub-`min_tank_control`
remnant. If your operation thinks in different units, the model changes.

## 3. Prior art — and why it failed for a reason that no longer applies

`GREENFIELD_RESERVATION_SCHEDULER_DESIGN.md` specifies a
`grid[tank][week] -> batch` reservation model, was BUILT, and was SHELVED:
348 violations against the incremental coordinator's 212.

Its stated root cause: **cohort-outer greedy**. Cohorts reserve in priority
order, so an early cohort takes cells a later, more-constrained cohort
needed. Its own risk #1: *"Going fully optimal = ILP, which violates the
precalc-first / no-solver stance."*

**That constraint is a property of the coordinator codebase, not of the
problem.** Global already runs LP (`global_planner_l3_poc`), MILP
(`global_placement_milp_poc`) and CP-SAT. A reservation grid solved as an
assignment problem has no cohort-ordering bias, because no cohort goes
first. The failure mode that shelved it is precisely the one a solver
removes.

The shelving decision anticipated this: *"preserve this as a documented
capability for a future global/ILP exploration."* This is that exploration.

## 3b. THE OBJECTIVE (operator, 2026-08-25) — lifetime handling per fish

> *"Global is beneficial as a reference model as it is based on pure
> mathematical result. The challenge is the mathematical result implies many
> small changes; the goal should be to find a result that balances all of
> the small changes while hitting all of the other constraints... all should
> be tools, with the constraint of minimising the total number of transfers
> each individual fish must go through at the end of life prior to harvest."*

This is the objective function, and it is **per-fish over a lifetime**, not
per-week over the facility.

Everything measured on 2026-08-25 was the wrong quantity: "903 transfers"
counts LEGS, which cannot distinguish one fish moved five times from five
fish moved once. Operationally those are entirely different — handling
stress accumulates in the animal, not in the plan. The metric to minimise is
therefore

```
    sum over fish of (transfers that fish experiences before harvest)
  = sum over transfer legs of (fish in that leg x 1)
```

i.e. **fish-transfers**, weighted by count, and attributable to a cohort's
whole journey rather than to a week.

Consequences for the design:

- **`min_transfer_count` is a PROXY, not a goal.** The operator: *"just a
  constraint to try and force the result of not having many small
  transfers."* It is a crude instrument aimed at lifetime handling. With the
  real objective in the model, the proxy should be relaxable — and it
  explains why patching it produced neutral or harmful results: the proxy
  was being enforced against plans whose real handling cost it never
  measured.
- **No lever is privileged.** Stock differently at the start, split earlier,
  shift the harvest week, re-time a transfer — *"all should be tools"*, and
  which one applies *"depends on what the other systems and tanks have
  available"*. So the choice is a solver decision under availability, not a
  fixed precedence list. **This answers Q2: there is no ranking; the ranking
  is the optimisation.**
- **Global's role is REFERENCE.** *"Global is beneficial as a reference
  model... based on pure mathematical result."* Its job is to say what is
  achievable, and the reason it cannot ship as-is is that a mathematical
  optimum expresses itself as many small changes. Balancing those against
  lifetime handling is the whole problem — not a detail to clean up
  afterwards.

**Reporting change (do this even before any rewrite):** report
fish-transfers and per-fish lifetime transfer counts alongside leg counts,
in `fast_check` and the workbook. The current comparison cannot see the
thing the operator actually cares about.

### 3b.1 Measured — and it reverses the 2026-08-25 conclusions

Applying the objective to the runs already on disk:

| run | legs | fish-transfers | **transfers/fish** | why not shipping |
|---|---|---|---|---|
| stable (2 days prior) | 893 | 9,257,629 | 2.57 | R4 breaches |
| **fix1 (shipping)** | 903 | 9,410,815 | **2.61** | — |
| anchored | 383 | 3,554,117 | **0.99** | 613 kg/m³, 38% over cap |
| relief | 1,415 | 8,894,710 | **2.47** | R3 breaches 10 → 32 |

Two conclusions from that day were wrong because they were drawn from LEG
COUNTS:

1. **`relief` was reverted partly for having the most legs (1,415). By the
   real objective it was the BEST legal engine (2.47).** Its extra legs were
   small; the fish moved less. It remains unshippable for tier breaches —
   but the handling argument against it was backwards.
2. **"Anchoring made every plan worse" is false.** It cut lifetime handling
   **62%**, from 2.61 to 0.99 — barely one transfer per fish. It failed on
   density, not on principle. Anchoring is the right mechanism missing two
   constraints (density headroom, tier legality), which is precisely what
   §4a puts into the solver.

**The shipping engine is the worst legal option on handling.** `fix1` at
2.61 moves fish more than the baseline it replaced. It ships because it is
legal and conserves, not because it is good on this axis.

This is the clearest available evidence for the design: the prize is real
and large (a 62% handling reduction was demonstrated, not projected), and it
is unreachable by patching, because the two constraints it violates are
exactly the ones a renderer cannot enforce.

## 4. Design

Three changes. Each is independently useful; together they are the
architecture.

### 4a. Reservation grid as a solved assignment (replaces greedy)

Reuse the grid model from the shelved design unchanged — cells, demand
curves `N_c(w)`, time-sharing on release. Change only HOW cells are
assigned: from a single greedy pass to a solver objective over all cohorts
and weeks simultaneously, subject to

- per-tank single occupancy,
- per-system and per-tank density/biomass headroom,
- tier topology R1–R7 (a reservation needing an illegal move is never made),
- transfers per week ≤ `max_transfers_per_week`,
- no transfer below `min_transfer_count`,
- no residence ending below `min_tank_control`.

**The three handling controls become CONSTRAINTS, not post-hoc checks.**
They currently have zero references in any `global_*` module, while
`placement.py` enforces them at 37 / 8 / 4 sites respectively.

### 4b. The pick joins the convergence loop

The pick becomes a participant. When it cannot legally realise something it
returns a CONSTRAINT rather than a warning, L1 re-plans, and they converge
on a plan that is feasible in quantity AND legal in tanks.

Conservation is never at risk, because the pick still never deviates
unilaterally — **L1 moves instead.** This is the difference between a
renderer and a participant, and it makes every failure in §1 structurally
impossible rather than individually patched.

### 4c. Harvest ↔ reservation coupling

Risk #2 of the shelved design, never built: when reservation infeasibility
would be relieved by harvesting earlier or splitting sooner, that is a
signal back to the harvest scheduler. This is the operator's point in §2,
and it is the mechanism by which a tank gets planned to END at a
harvestable count.

## 5. The operator's alternative — a general production model

> *"The system may want to model the facility then find a general transfer
> model that works in general and try to fit everything inside that general
> production model."*

A different and possibly better framing: derive a standard rotation — the
canonical path a batch takes through OG1/2 → grow-out → 6N, with defined
split points and residence lengths — and require every batch to fit it, the
planner choosing only timing and sizing within the template.

**For:** transfers become predictable and countable; headroom is designed in
rather than discovered; the plan is explainable to operations because every
batch looks the same; and it matches how the 6N pair rotation already works
— a healthy rotation is a repeating pattern, and a blocked week means the
pattern was broken, not that more tanks are needed.

**Against:** it constrains the solution space, so it may cost harvest
against a free-form optimum; irregular batches and the starting state (the
PR handover is never in template position) need an explicit exception path;
and the template itself has to be derived, which is its own study.

**These are not exclusive.** The template can be the objective's preferred
shape while the grid handles deviations — the solver prefers
template-conforming residences and pays a penalty to depart. That is likely
the best of both, but it doubles the design surface, so it is offered as a
decision rather than assumed.

**OPEN QUESTION Q1:** template-first, grid-first, or
grid-with-template-preference? This is the largest single fork in the
design, and it is an operational judgement rather than an engineering one.

## 6. Implications — what this costs and breaks

1. **The pick runs up to 10× per forecast.** One full run is currently
   ~3.2 h. UNMEASURED: whether the LP/MILP solve dominates (in which case
   extra pick passes are nearly free) or the pick does. Measure before
   committing — this is the difference between ~3.5 h and ~30 h.
2. **The loop contract changes.** `converged` currently means L1 and L3
   agree on quantities; it would come to mean all three agree, and
   non-convergence becomes a reportable outcome. An honest "this plan is not
   realisable" beats a silently illegal one.
3. **Harvest may move.** If a tank must end at a harvestable count, the
   harvest week or size shifts. `MonthlyTargets` and `Min Harvest/Week` are
   INVIOLABLE operator inputs, so the coupling must treat them as hard
   constraints and surface infeasibility rather than quietly re-timing.
4. **The tournament comparison changes — for the better.** Global would plan
   under the same handling constraints as the controller, so its margin
   stops being partly bought with transfers the operator would not
   authorise.
5. **Scope.** This is a rewrite of the realisation layer, not a patch. Days,
   not hours.

## 7. Protecting the gains

The operator's constraint: *"we need to make sure we don't lose any of our
gains too and we have back up."* Concretely:

- **Baseline to beat, measured on the full 85 weeks** (commit `a81f6c2`):
  transfers 903, R3 10 / R4 0, peak density 153 kg/m³, over-cap 7.2%,
  harvest 3,604,211, conservation clean, 816 tests green. A candidate that
  loses on ANY of these does not ship.
- **Build behind a flag.** The existing engine stays the default until the
  new path beats the baseline on the full horizon — exactly as
  `RESERVATION_PLAN=1` did for the shelved design.
- **Tag the baseline** before work starts, so rollback is one command.
- **Full-horizon verification is the gate, never the 12-week harness.**
  `fast_check` was overturned three times on 2026-08-25 — it predicted 216
  transfers / R3 4 where the truth was 1,415 / R3 32. It is a filter for
  obviously-broken; it is not evidence that anything works.
- **`check_global_invariants.py` stays a hard gate.** It caught the 696-fish
  loss that four layers of reasoning missed.

## 8. Literature to update when this lands

- `GREENFIELD_RESERVATION_SCHEDULER_DESIGN.md` — cross-reference this as the
  global/ILP exploration its shelving note anticipated.
- `docs/USER_GUIDE.md` — any change to what `converged` means, and to the
  handling controls' scope (they would newly bind Global).
- `MEMORY.md` + `project_global_engine_seam.md` — the seam description
  changes if the pick becomes a participant.
- Help text and popups referencing transfer behaviour.

## 9. Verification horizon — what shortening actually costs

The operator offered to shorten the model run *"to 52 weeks or 60 weeks,
whatever we need to ensure good coverage and enforced accurate results."*
Measured, the horizon is not the dial that matters:

| | |
|---|---|
| horizon | 2026-W33 → 2028-W12 (85 weeks) |
| `sixn_production_start` 2028-01-01 | **week 74** |
| purge mode | weeks 1–73 (**86%** of the run) |
| production mode (6N mains rejoin production) | weeks 74–85 (14%) |
| R4 breaches observed | week **82** |

A 52-week run ends at 2027-W31; a 60-week run at 2027-W39. **Both are
entirely inside purge mode.** Truncating trims the regime that is already
well covered and still never reaches the one that is not — which is exactly
where the R4 class lives.

The horizon has TWO REGIMES, and coverage is about regimes, not weeks. So
the cheap gate is to move the boundary rather than cut the length:
`sixn_production_start` is a config date, so a test config that pulls it
forward puts startup, purge, the switch, and production mode inside a short
run.

**STATUS: being measured.** A 30-week regime-compressed run exceeded 10
minutes where a 30-week normal run should take ~1, so the switch may be
expensive to solve (or it was CPU contention with a concurrent full run).
If it is genuinely cheap once isolated, it becomes the development gate and
the full 85 weeks stays the ship gate. If not, the full run remains the only
honest gate and iteration stays slow.

**This does not relax the rule earned on 2026-08-25:** a short run is a
filter for obviously-broken. `fast_check` was overturned three times that
day. Regime coverage makes a short run BETTER, not sufficient.

## 10. Open questions for the operator

- **Q1 (§5)** Template-first, grid-first, or grid-with-template-preference?
- ~~**Q2** Which lever is preferred when a residence cannot end cleanly?~~
  **ANSWERED (§3b):** no ranking — all are tools, and the choice depends on
  what other systems and tanks have available. The ranking IS the
  optimisation, under the lifetime-handling objective.
- ~~**Q3** Is `min_transfer_count` a hard floor or a preference?~~
  **ANSWERED (§3b):** a proxy for the real objective, therefore relaxable
  once lifetime handling is modelled directly.
- **Q4** Does a "general production model" (§5) already exist informally — a
  standard path operations expects a batch to take? If so it should be
  written down and used, not derived from the data.
- ~~**Q5 (§3b)** Should a whole-tank move count for less than a split?~~
  **ANSWERED (operator, 2026-08-25):** *"A whole tank move adds one transfer
  for each of the fish that were transferred."* No discount. The objective is
  therefore exactly `sum over legs of (fish in that leg)` — total fish moved
  — with no structural weighting.

  Two consequences worth stating, because they are not obvious:

  1. **Consolidation is expensive.** Moving a small remnant into another tank
     charges every fish in it. So "tidy up the dribble by merging tanks" is
     not free under this objective, and can be worse than leaving the remnant
     — which is the opposite of what `min_tank_control` pushes toward. The
     proxy and the objective genuinely conflict here (see §3b), and the
     objective wins.
  2. **There is no preference between one big move and several small ones**
     carrying the same fish. Leg count is irrelevant except where it hits
     `max_transfers_per_week`, which is a crew-capacity limit and a real,
     separate constraint.

  **ASSUMPTION (open, minor):** the measured figures count `Type=Transfer`
  legs only. TranOG (the FW → OG stocking event) and Grade pickup/retention
  rows are excluded. Including TranOG would add roughly one transfer per fish
  uniformly across every engine, so it does not change any ranking — but if
  the operator counts stocking as a handling event, the absolute numbers
  should include it.
