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

> **§3b.1 AND §3b.2 BELOW ARE SUPERSEDED — see §3b.3.** Their figures were
> computed on a broken basis (horizon-wide, and excluding Grade rows). They
> are kept because the design's early reasoning rests on them and the record
> of what was wrong is worth more than a clean-looking document.

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

### 3b.2 MANDATORY vs DISCRETIONARY — where the cost actually is

Operator, 2026-08-25, on excluding FW→OG: *"that's part of the production
cycle that we cannot remove... focus on after OG introduction."*

The principle generalises: **only AVOIDABLE handling belongs in an objective
the solver is asked to minimise.** A move that must happen regardless of the
plan is not a decision variable. Inside OG, one such move remains — every
fish must enter 6N before harvest, so OG→6N staging is structural in exactly
the same way FW→OG is.

Decomposed (fish-transfers, 85 weeks):

| run | →6N (mandatory) | in-6N | grow-out | total/fish | **discretionary/fish** |
|---|---|---|---|---|---|
| stable | 3,462,746 | 155,737 | 5,639,146 | 2.57 | 1.61 |
| **fix1 (shipping)** | 3,457,864 | 305,620 | 5,647,331 | 2.61 | **1.65** |
| anchored | 3,086,324 | 294,752 | **173,041** | 0.99 | **0.13** |
| relief | 3,482,609 | 335,748 | 5,076,353 | 2.47 | 1.50 |

1. **~0.96 transfers/fish is structural** (the 6N staging). The shipping
   engine's avoidable handling is **1.65 per fish** — every fish moved about
   one and a half times for no operational reason.
2. **Grow-out redistribution is 93% of the avoidable cost** (5.6M
   fish-transfers vs 0.3M inside 6N). The 6N pipeline, which absorbed almost
   all engineering attention on 2026-08-25, is ~5% of the problem.
3. **Anchoring cut grow-out handling 97%** (5,647,331 → 173,041), taking
   discretionary handling 1.65 → 0.13. That is the size of the prize, and it
   is measured rather than projected.

**Objective for the grid solver (§4a): minimise DISCRETIONARY fish-transfers**
— in-6N plus grow-out — with mandatory staging excluded, since including a
constant the solver cannot change only obscures the number.

### 3b.3 CORRECTED 2026-08-26 — three measurement errors, and the ranking flips

The operator challenged the figures: *"less than 1 handling per fish does not
make sense, I think there may be an issue in the constraints for the method"*
and *"I'm also not sure how the fish can be moved less than once in grow-out
and less than once into 6N."* Both objections were correct. Three errors:

**Error 1 — mixed populations.** Handling was computed as
(transfers in horizon) / (fish harvested in horizon). Those are different
populations. B41–B48 are PRE-EXISTING at the PR handover — most of their life
predates week 1, so their transfers are invisible (B42 read 0.25/fish, which
is impossible for a full life). B56–B60 are stocked but never harvested —
transfers counted, no denominator. **Fix: measure only batches stocked AND
≥90% harvested inside the horizon (B49–B54).**

**Error 2 — Grade rows excluded.** `Type=Grade` rows were not counted. The
operator confirms grading is **full handling — "a whole tank move adds one
transfer for each of the fish that were transferred"** applies equally: every
fish goes through the sorter. Excluding it hid 2.64 transfers/fish on the
tuned controller.

**Error 3 — the "0.13 prize" was the density bug.** The anchored run's
grow-out handling of **0.00** was not an achievement. It kept fish in 44%
fewer tanks at up to 613 kg/m³, so batches never spread and never needed to
move. **The low handling and the density violation are the same fact.** §3b.1
cites 0.13 as the design's central justification. That is withdrawn.

**Corrected — complete journeys (B49–B54), Grade included:**

| arm | Transfer | Grade | **total/fish** | harvest | over 95 | peak | topology |
|---|---|---|---|---|---|---|---|
| controller tuned | 1.31 | 2.64 | **3.95** | 3,652,084 | 0 | 89 | none |
| controller stock | 2.13 | 1.34 | **3.47** | 3,635,023 | 0 | 89 | none |
| global-lp stock | 2.75 | **0.00** | **2.75** | 3,604,211 | 185 (7.2%) | 153 | R3:10 |

**Two findings that change the picture:**

1. **TUNING MAKES HANDLING WORSE.** Stock 3.47 → tuned 3.95. The tuner
   switches the rebalancer off (Transfer 2.13 → 1.31) but grading rises
   (1.34 → 2.64) and more than eats the gain. **The scoring function does not
   count grading as handling**, so the tuner optimises one bucket while the
   cost moves into another. This is actionable independently of anything else
   in this document.
2. **GLOBAL DOES NOT GRADE AT ALL** — zero Grade rows in every Global run;
   the machinery lives in `placement.py` (65 refs) with one passing mention in
   the Global pick. So its lower handling is achieved by SKIPPING an operation
   the controller performs, not by doing it better. It compensates by growing
   fish heavier (4,048 g average vs 3,884 g) with slightly more below spec
   (0.6% vs 0.3%) — and holding fish heavier is precisely what drives its
   density pressure and its 7.2% over-cap tank-weeks.

**Net:** the controller wins harvest, density, tier legality and weight-spec
compliance. Global wins handling only, and only because it omits grading
while producing a plan that is not executable (10 R3 breaches, 185 over-cap
tank-weeks). A handling comparison between them is NOT like-for-like until
Global grades or the controller's grading is priced into the tuner's score.

**Process note.** A full day was spent on 6N moves (5% of avoidable handling)
because that is where the loud failures were — topology warnings and
purge-hold breaches — while grow-out re-levelling (95%) produced no warning
at all and went untouched. Attention followed alarms rather than magnitude.
Report discretionary handling per fish on every run so magnitude is visible
without anyone having to ask for it.

## 3c. THE PAYOFF, measured 2026-08-26 — Global holds a cap the controller cannot

The justification in §3b.1 (a 92% handling cut) was withdrawn in §3b.3: it was
the density bug in disguise. **This replaces it, and it is real.**

| arm | peak % of biomass cap | weeks over | `biomass_cap` gate |
|---|---|---|---|
| controller stock | 107.0% | 24 | FAIL |
| controller tuned (SHIPPING) | 105.7% | 11 | **WARN** |
| global-lp | 97.1% | **0** | **PASS** |
| **global-milp** | **96.7%** | **0** | **PASS** |

`max_biomass_kg` is a hard operator input. The shipping engine cannot hold it;
both Global arms hold it with room to spare, and global-milp does so without
straining any per-system cap either (worst system at peak: OG3S at 80%).

**Why the controller cannot.** Its level-load smoother is one-directional:

```python
harvest_target = max(harvest_target, _level_floor)      # placement.py
```

A floor, never a ceiling. It can pre-harvest to shed a coming peak; it has no
way to hold fish back. So it runs at the 55,000/week processing limit for 8 of
10 weeks before the peak, drains every fish over 3,500 g, and then starves for
eight weeks (23-28k/week, below the 30,000 contract floor) while 330-390k fish
sit in the 3,000-3,500 g band.

**Global does exactly what the operator proposed** — less before the gap, more
during it:

| week | controller | global-milp |
|---|---|---|
| 2026-W44 | 54,912 | 42,839 |
| 2026-W46 | 23,366 | 36,961 |
| 2026-W48 | 25,040 | 51,879 |
| 2026-W49 | 25,873 | 52,463 |
| 2026-W53 | 27,515 | 46,671 |

**But it is NOT a transferable knob.** Tested directly (2026-08-26): capping
`max_harvest_per_week` at 42k/48k over W36-W45 via the per-week FacilityLimits
override made things WORSE -- peak 105.7% -> 111.0/112.3% (crossing into FAIL),
weeks over cap 11 -> 26, below-floor weeks 8 -> 18. Fish held back do not
vanish; they carry into the peak window and add to it. Average weight and HOG
tonnage DID rise (3.88 -> 3.91 kg, +70 t), so the operator's intuition about
fish size was right -- the biomass constraint simply binds first.

Global achieves it by shaping the WHOLE trajectory: it stocks more (3,772,010
vs 3,712,430), holds more fish standing (2,043,475 vs 1,980,614), yet peaks
lower, and maxes out in 26 weeks against the controller's 36. That is a
property of solving the horizon simultaneously, not of any setting.

**The `controller-hybrid` bridge already exists and pulls the wrong way.** It
feeds Global's L1 envelope to the controller as a target band. Its own blurb:
as shipped it is INERT (the levers ship false, so the guide steers nothing);
with the levers ON, weeks under the contract floor fall 20 -> 14 but peak
biomass rises 102.6% -> 107.1%. The tuned tournament searched those levers and
switched them OFF. It buys floor compliance by holding fish -- the opposite of
what the biomass cap needs.

**Conclusion: the only route to Global's biomass performance is a Global that
obeys the rules.** Operator ruling 2026-08-26: *"I would like Global to work
properly too, even if it is very slow -- I do not want code that does not
function as intended."* Runtime is therefore NOT a reason to reject an approach;
correctness is the constraint, speed is a preference.

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

### 5.1 DECIDED (operator, 2026-08-25): grid first, template as an OUTPUT

> *"We don't have a standard batch path. Use grid first — it would be nice
> to determine the standard path as a result of the method."*

Two decisions, and the second inverts the section above.

**There is no existing standard path to encode.** The template-first option
is therefore not available even in principle: it would require deriving the
template first, which is the study §5 warned about, before any planning
could start.

**The template becomes a RESULT, not an input.** Solve the grid freely under
the constraints, then look at what the solutions have in common. If the
solver, across many batches and a long horizon, keeps producing residences
of a similar shape, THAT is the plant's natural production model —
discovered from the physics and the constraints rather than assumed from
habit.

This is a better shape than imposing a template, for three reasons:

1. **It cannot be wrong the way an assumed template can.** A hand-written
   standard path encodes today's practice, including its accidents. A
   derived one encodes what the facility and the rules actually admit.
2. **It is falsifiable.** If no recurring pattern emerges, that is a real
   finding — it means the plant genuinely has no steady state under these
   constraints, and any template would have been a fiction.
3. **It pays back into runtime.** A derived template can warm-start or
   restrict later solves, which is the most promising answer to the cost
   concern in §6.1. Deriving it first would have been guesswork; deriving it
   from solved plans makes it evidence.

**How to derive it (proposed, not yet specified in detail):** take the
solved residences — `(batch, tank, start week, end week, entry count, exit
count, entry weight, exit weight)` — and look for modal structure in the
system sequence a batch takes, the weights at which it moves, and residence
lengths. The output is a description like *"a batch typically enters OG1/2
at X g, splits at Y g into N tanks, enters grow-out at Z g, and stages to 6N
at W g"* — with the spread around each figure, since the spread is what says
whether it is a real pattern or an average of unlike things.

**Do NOT constrain the solver to the derived template without measuring the
cost.** The moment it becomes an input it can only reduce the solution
space; it must earn that against the §7 baseline like any other change.

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

**MEASURED 2026-08-25 — the idea does NOT work.** A 30-week run with
`sixn_production_start` pulled to 2026-12-01 consumed ~30 MINUTES of CPU at
1.18 cores and had still not finished when it was killed. A normal 12-week
run takes **0.4 minutes**; linear scaling predicts ~1 minute for 30 weeks.
Regime compression is roughly **75x worse than linear**.

Likely cause (not confirmed): at `sixn_production_start` the 6N mains rejoin
the production pool, enlarging the placement search space — and compressing
the switch to week ~15 leaves so little purge history that the problem may
also be near-degenerate. A gentler compression (switch at ~2027-06 rather
than 2026-12) might be cheaper while still spanning both regimes, and is
worth one measurement when the machine is free. It was not pursued
immediately because it was competing for CPU with the shipping engine's
confirming run.

**Consequence for the build (E1): there is no cheap gate for the
production-mode regime.** What exists:

| gate | covers | cost |
|---|---|---|
| `fast_check --weeks 12` | startup + purge mode | ~0.4 min |
| full 85-week run | both regimes, incl. the week-74 switch | ~3.2 h |

Purge-mode behaviour — which is 86% of the horizon — can be iterated on in
seconds. Anything touching the mode switch, the 6N mains rejoining
production, or the R4 class costs a full run. Sequence the build so
mode-switch work is batched rather than iterated.

**This does not relax the rule earned on 2026-08-25:** a short run is a
filter for obviously-broken. `fast_check` was overturned three times that
day. Regime coverage makes a short run BETTER, not sufficient.

## 10. Open questions for the operator

- ~~**Q1 (§5)** Template-first, grid-first, or grid-with-template-preference?~~
  **ANSWERED (§5.1): GRID FIRST**, with the standard path derived from the
  solved plans as an output of the method.
- ~~**Q2** Which lever is preferred when a residence cannot end cleanly?~~
  **ANSWERED (§3b):** no ranking — all are tools, and the choice depends on
  what other systems and tanks have available. The ranking IS the
  optimisation, under the lifetime-handling objective.
- ~~**Q3** Is `min_transfer_count` a hard floor or a preference?~~
  **ANSWERED (§3b):** a proxy for the real objective, therefore relaxable
  once lifetime handling is modelled directly.
- ~~**Q4** Does a "general production model" already exist informally?~~
  **ANSWERED (operator): NO.** *"We don't have a standard batch path."* So
  there is nothing to encode, and §5.1 derives one instead.

**All five opening questions are now closed.** What remains open is
engineering, not judgement:

- ~~**E1** Is the mode switch expensive to solve?~~ **ANSWERED (§9): YES,
  ~75x worse than linear.** No cheap gate exists for the production-mode
  regime; purge mode iterates in 0.4 min, anything touching the week-74
  switch costs a full 3.2 h run. Batch mode-switch work rather than
  iterating on it. One measurement still worth taking: a gentler compression
  (switch at ~2027-06) may be cheaper while still spanning both regimes.
- **E2** Does the pick dominate runtime, or the LP/MILP solve? Decides
  whether §4b's up-to-10x pick passes cost minutes or hours (§6.1).
- **E3** Does a recurring residence pattern actually emerge from solved
  plans (§5.1)? Unknowable until the grid solver exists — and a negative
  answer is a legitimate result.
- **E4 — THIS DESIGN MAY BE AIMED AT THE WRONG ENGINE.** Discretionary
  handling (§3b.2) has NEVER been measured on any controller arm. It was
  defined hours before this document and applied only to Global's runs. If
  the controller family also sits near **1.65 transfers/fish**, then
  grow-out re-levelling is a SHARED defect and the fix belongs somewhere
  other than the Global pick — the whole premise of §1 ("Global is the
  outlier") would be wrong. If it sits near **0.13**, Global is confirmed as
  the outlier and the grid build is justified.

  There is a structural reason to expect the controller is better — it plans
  in tanks, one representation, so it should not have the even-split
  re-levelling that costs Global 5.6M fish-moves. **That is reasoning, not
  measurement**, and reasoning about this codebase was wrong repeatedly on
  2026-08-25. Do not start the build on it. MEASURING — the tuned tournament
  launched 2026-08-25 22:25 will settle it, with discretionary handling
  computed post-hoc from each arm's workbook (the tournament does not know
  about the metric).
- **E5** The controller family's density profile and tier legality have not
  been independently verified in this work. "The controller passes every hard
  gate" is carried forward from earlier sessions, not re-measured. The
  premise that only Global is non-compliant rests on it.
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

  **CLOSED (operator, 2026-08-25):** FW → OG stocking does NOT count —
  *"that's part of the production cycle that we cannot remove, we should just
  focus on after OG introduction."* The measured figures already exclude it
  (`Type=Transfer` only). See §3b.2: the same logic excludes mandatory 6N
  staging, leaving DISCRETIONARY handling as the objective.
