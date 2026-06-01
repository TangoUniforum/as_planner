# Greenfield coordinator locks

Lock record for the **facility assignment coordinator** added to the
greenfield `Python/forecast/` codebase (Phase A-D rebuild). This is a
SEPARATE lineage from the `as_forecast/option_c/` LP-solver production
code documented in `M5_ROUND_1_LOCKS.md` / `M4_ROUND_1_LOCKS.md`. The
two codebases share the `Forecast.xlsm` workbook contract but nothing
else; do not cross-reference milestone tags between them.

Branch: `feature/precalc-coordinator` (off `master`).

---

## §1 — Motivation (2026-05-26 → 2026-05-28)

Density-violation investigation on the reference workbook surfaced that
the greenfield migration plan (`precalc._build_migration_plan`) used a
FIFO-oldest-first greedy allocator with three structural gaps:

1. **Per-week sizing.** `tanks_needed_at_density_cap` reflected only the
   current week's biomass, so cohorts were under-tanked early and could
   not recover once past the OG1/2 1 kg lock (INV-4).
2. **Tank reduction without harvest.** The plan consolidated cohorts
   (e.g., B47 W20: moved tank 21's fish into tank 16) when current-week
   density permitted fewer tanks — an operationally illegal move that
   masked the real concentration problem.
3. **No cross-batch coordination.** FIFO age order let older batches
   (B41 holding 9 tanks for a 7-tank need) starve younger under-
   allocated batches (B47 needing 9, getting 2) of the free pool.

Root-cause trace (B47): ProductionReport hydrates B47 with 195,526 fish
in tank 16 (OG1S) + 95,454 in tank 21 (OG2N) = 290k cohort. The FIFO
plan consolidated to tank 16, density climbed to 160+ kg/m³ by harvest,
and the density-trigger Grade events (`placement.py`) fired too late
(throttled by `TRANOG_RESERVE` and 1-destination-per-tank-per-week) to
recover.

---

## §2 — Locked design decisions

### Q-COORD.A — Lifetime-max tank sizing (LOCKED)

`_build_batch_week_facts` applies a backward sweep so each SW week's
`tanks_needed_at_density_cap` = max density requirement over the
remaining lifecycle until the next harvest-driven biomass shrink.
Propagation resets at biomass drops (post-harvest segments size
independently). Rationale: claim lifecycle-peak tanks while the cohort
is still under 1 kg (legal intra-OG1/2 split) rather than being caught
short after the INV-4 lock engages.

### Q-COORD.B — Sticky floor (LOCKED)

Total tank count per batch never decreases except via a harvest event.
Operational rule from project lead 2026-05-27: *"total tank count per
batch can not reduce unless the batch is under harvest and
consolidation as a result of that... we should not go backwards."*
Implemented as `HARVEST_RELEASE` being the only event that shrinks a
batch's tank set.

### Q-COORD.C — Coordinator architecture (LOCKED)

New precalc stage `_build_facility_assignment_plan` produces the
canonical `TankAssignmentPlan[(batch, week)] -> tank_ids`. Sits between
`_build_batch_week_facts` (per-batch needs) and `_build_migration_plan`
(now a thin diff layer deriving keep/add/drop). Event-driven interval
layout: chronologically-sorted event list, multi-week tank claims
committed per segment.

Event types + priority within a week:
`PR_INIT (0)` → `PR_REBALANCE (1)` → `TRANOG_ARRIVAL (2)` →
`HARVEST_RELEASE (3)` → `GROWTH_ADD (4)`. Releases before adds so freed
capacity rejoins the pool the same week.

### Q-COORD.D — PR_REBALANCE (LOCKED)

At forecast start ONLY, release PR tanks beyond a batch's lifetime-max
need back to the free pool. PR is operator-given; the coordinator
normalizes to the canonical plan at horizon start. This is the sole
week tank counts may drop without harvest (sticky floor §Q-COORD.B
applies from week 2 onward).

### Q-COORD.E — Deterministic rules, NO scoring weights (LOCKED)

Project-lead direction 2026-05-28: *"what is the point of the
precalculating if we using scoring weights?"* The coordinator uses
explicit deterministic rules, never tuned weights:

- **Batch order within (week, type):** FIFO by `input_date` ascending.
- **GROWTH_ADD tank pick:** (1) systems the batch already occupies,
  then (2) eligible systems alphabetically, then (3) lowest free
  tank_id.
- **RELEASE / REBALANCE tank pick:** OG3+ before OG1/2; highest tank_id
  first within that.

An earlier session-4 implementation with a `sticky=1000 / load=100`
scoring function was **removed** as contradicting precalc-first
principles. Every assignment must trace to a stated rule.

### Q-COORD.F — Honest density signal (LOCKED, REVISED 2026-05-28)

Density violations that remain after the coordinator are an operational
signal, not an algorithm artifact. **Original thesis (PR concentration
is the dominant driver) was tested and only partially confirmed** —
see the de-concentration experiment below.

**Experiment (`scripts/experiment_pr_deconcentrate.py`, 2026-05-28):**
spread B47's PR cohort across N tanks, re-run the real pipeline:

| Scenario | Violations | Worst | B47 | B46 |
|---|---|---|---|---|
| baseline (as-is) | 353 | 230.2 | 102 | 79 |
| B47 → 4 tanks | 317 | 211.3 | 72 | 69 |
| B47 → 6 tanks | 317 | 211.3 | 72 | 69 |
| B47 → 8 tanks | 317 | 211.3 | 72 | 69 |

**Findings:**
- PR concentration IS a contributor: spreading B47 cuts ~36 violations
  (~10%), B47's own 102→72, worst 230→211.
- But it **plateaus at 4 tanks** — 6/8 give zero further benefit, and
  ~317 violations persist regardless of B47 spread.
- **The dominant driver is facility peak over-subscription, NOT PR
  concentration.** Canvas reports demand 2386 / supply 2028 tank-weeks
  (117.7%); peak biomass 6,643 t @ 2027-W18 vs ~6,370 t total
  density-cap capacity (≈39 allocatable OG tanks × 163.4 t). When total
  biomass exceeds total 95 kg/m³ volume, violations are forced
  *somewhere* by physics, independent of placement.

**Harvest-scheduler check (2026-05-28) — corrected the capacity thesis
too.** Total facility biomass near the 2027-W18 peak is 3,860-3,930 t,
held right at the Control max biomass cap (3,900 t). The 6,643 t figure
was the min-only-FIFO projection (hypothetical carrying capacity), NOT
realized biomass. The scheduler IS harvesting to the ceiling correctly.

At 3,900 t the facility is only ~61% of total density-cap capacity
(≈6,373 t = 39 OG tanks × 163.4 t, less 4 OG6N pipeline tanks ≈ 35
allocatable × 163.4 ≈ 5,719 t). **So it is NOT over-subscribed.**

**Per-system state @ W18** (3,881 t, 33 tanks occupied, 6 empty):
violations in OG1N(1), OG2N(1), OG5S(1), OG6S(2) — those systems run
81-92 kg/m³ *average* so individual tanks tip >95. Empty tanks sit in
OG4N + OG5N (plus 4 OG6N pipeline) — systems the violating batches
cannot legally move to under the progression law.

**True root cause (3rd revision): distribution under the system-
progression law.** 3,881 t ÷ 35 tanks = 64 kg/m³ — a hypothetical
perfect spread has zero violations. But the operational rules forbid
free redistribution:
- system-progression law funnels cohorts through systems in sequence;
- INV-4 1 kg lock prevents intra-OG1/2 spread above 1 kg;
- INV-1 one-batch-per-tank;
so biomass clusters in the systems where the active cohorts currently
sit, tipping a handful of tanks over 95 even while other systems and
6 empty tanks have headroom.

**Magnitude:** modest — ~6 tank-violations at the peak week. NOT a
capacity problem, NOT a scheduler problem, ~10% PR-concentration, the
rest is progression-law-bounded distribution.

**Open question for future work:** whether the coordinator can give
peak-stressed batches more tanks EARLIER (while still in an accessible
system under 1 kg) to lower per-tank counts before the progression law
funnels them — i.e., spread proactively in OG1/2 before the lock. This
is the lifetime-max direction; it plateaus when eligible systems fill.
Bounded by progression law; likely cannot reach zero without relaxing
an operational rule (operator decision).

---

### Q-COORD.G — Exit-at-1 kg enforcement (LOCKED 2026-05-28)

**Diagnosis correction (supersedes the capacity/distribution thesis in
Q-COORD.F as the DOMINANT driver).** Tank-by-tank inspection of OG1/2
at W30/W31 showed **11 of 12 nursery tanks held fish already OVER 1 kg**
(B43 at 3.4-3.6 kg, B44 at 2.7 kg, B45 at 2.0 kg). Per the saved rule
([[feedback-og12-1kg-rule]]) these must exit OG1/2 at 1 kg — they
hadn't. Over-1 kg fish overstaying the nursery was clogging the 12
tanks and causing the density violations, NOT young-fish crowding.

**Root cause:** `_build_batch_week_facts` set
`eligible_systems = _OG_ALL` for SW non-TranOG weeks — which includes
OG1/2. An over-1 kg batch was therefore still "eligible" for the
nursery, and the coordinator's sticky rule kept it there indefinitely.
No event forced the OG1/2→OG3-6 exit.

**Fix (two parts):**
1. **Eligibility flip.** `_build_batch_week_facts`: SW batch with
   `avg_wt_g >= 1000` → `eligible = _OG36` (grow-out only); else
   `eligible = _OG12` (nursery, incl. TranOG). Encodes the
   progression law's exit-at-1 kg.
2. **EVT_MIGRATE event (per-week, 1:1 swap).** Fires EVERY SW week a
   batch is ≥ 1 kg (priority 2, before TranOG arrivals). For each OG1/2
   tank still held, claims one free OG3-6 tank and releases the OG1/2
   tank (1:1 — total count constant, no transient over-concentration).
   If OG3-6 is full, stops and the next weekly MIGRATE retries
   (contention-resilient drain). Phase D emits the cross-system
   Transfer from the keep/add/drop diff. NOTE: a "claim up to needed
   then release covered" variant was tried and made density WORSE
   (303/352 — Phase D rebalance consolidated whole cohorts into one
   tank); 1:1 swap is the locked choice.

**Per-week TARGET top-up (EVT_ADD reworked).** EVT_ADD now fires every
SW week carrying the ABSOLUTE per-week needed count and tops the batch
up TOWARD it (counting only eligible-system tanks), instead of a
one-shot delta at transitions. A batch under-allocated because the
pool was momentarily full keeps acquiring tanks in following weeks as
harvests/migrations free capacity. Still precalc-first — each week's
target is the deterministic density requirement; the handler only
ADDS (sticky), shrinks are explicit EVT_RELEASE.

**Lifetime-max sizing REMOVED (Q-COORD.A reverted).** It existed to
claim OG1/2 tanks before the 1 kg lock; exit-at-1 kg makes it moot
(fish leave OG1/2 at 1 kg into OG3-6, where any-to-any transfer is
allowed — no lock). Lifetime-max also actively harmed: every
pre-existing >1 kg batch demanded its peak tank count simultaneously
at W20, over-subscribing OG3-6 (4 batches × 8 tanks > 21) and starving
each to 2 tanks → 136k-fish/tank violations. Per-week sizing + the
coordinator's incremental GROWTH_ADD (legal in OG3-6, no lock) is
correct.

**Empirical progression (reference workbook):**
- Coordinator baseline (lifetime-max): 353 / worst 230
- + exit-at-1 kg eligibility + MIGRATE, lifetime-max removed: 294 / 231
- + per-week TARGET top-up + per-week 1:1 MIGRATE swap: **240 / 249**
- 0 count/biomass drift throughout; harvest 8.81M kg; 7/7 TranOG placed.

Net: **353 → 240 violations (−32%)**, all moves rule-compliant and
deterministic. Worst 230 → 249 is the pre-existing PR-concentration
residual: B46/B47 arrive over-concentrated (≈143k fish in 1-2 OG1/2
tanks from ProductionReport); the 1:1 swap relocates that concentration
to OG3-6 but cannot SPLIT it without a grade-split, and the
claim-and-split variant regressed. These worst cases are operator-side
PR concentration (per Q-COORD.F, ~10% lever) — not algorithm-fixable
without crossing into optimization.

### Q-COORD.H — Forward-peak system routing / staggering (LOCKED 2026-05-28)

Project-lead direction: keeping per-system feed + biomass LEVEL and
low-variance is an explicit system target; the planner should DISCOVER
batch staggering arithmetically rather than need it hand-coded.

**Rule (replaces alphabetical system ordering for non-sticky picks).**
When a batch needs a tank in a system it doesn't already occupy
(GROWTH_ADD into a new system, or MIGRATE exit into OG3-6), order
candidate systems by the **resulting peak system biomass**:

```
per_tank_curve[batch][w] = biomass_post(w) / tanks_needed(w)   # known forward
system_load_curve[S][w]   = sum of per_tank_curve over tanks currently in S
resulting_peak(S)         = max over w of (system_load_curve[S][w]
                                           + per_tank_curve[batch][w])
pick S with lowest resulting_peak  (tiebreak: system_id)
```

Staggering emerges automatically: a cohort peaking at week W avoids a
system already peaking at W, so cohorts interleave and each system's
load stays level. Single operational criterion (minimize peak system
biomass) — NOT a scoring weight. Forward-derived from the biology
engine's curves, deterministic, replayable.

**Sticky still wins first.** Systems the batch already occupies are
tried before any new system (transfer minimization). Forward-peak only
orders the NEW-system candidates.

**Empirical (reference workbook):** within-week cross-system OG3-6
biomass stdev 98.4 → 96.1 t (systems measurably more level). Violations
240 → 248 (+8), worst 249 → 257. The violation cost is the same PR
over-concentration reshuffled, not new damage; the leveling benefit is
marginal HERE because (a) most placement is sticky and pre-existing PR
batches dominate the load with no staggering freedom, (b) the residual
violations are tank-level concentration (B46/B47 ~143k fish/tank from
PR), which choosing a system can't un-concentrate. The capability is
correct and will pay off more on a clean workbook where cohorts enter
staggered without PR over-concentration. Kept per project-lead
direction (levelness is a named target). 0 drift maintained.

### Q-COORD.I — Even-out density pass (PR de-concentration via transfers, LOCKED 2026-05-28)

Project-lead direction: the system SHOULD actively fix PR-created
problems via transfers when the result is an optimal (under-cap)
config — "the lesser of two evils."

**Root insight:** PR over-concentration manifests as UNEVEN fish
distribution across a batch's existing tanks (e.g. B47: 195k in one
tank + 95k in another), not too-few-tanks. The coordinator works in
tank COUNT assuming even spread (needed=2 → "≈145k each, fine"), but
Phase D's `_emit_transfers_for_batch_diff` only rebalances when the
tank SET changes — so an unchanged-but-uneven set keeps a tank over
95 kg/m^3 forever.

**Fix:** new Phase D pass `_even_out_density`, run every week for all
active batches (including unchanged sets the diff skips). When any tank
in a batch's group exceeds its density cap, level fish across the group
via Transfers. Legality:
- OG1/2 tanks: only sub-1 kg (INV-4 forbids ≥1 kg intra-OG1/2 moves);
  OG1 and OG2 evened as one pool (sub-1 kg moves between any OG1/2
  tanks allowed).
- OG3-6 tanks: any weight (any-to-any). OG6N pipeline-owned, excluded.
Only triggers when a tank is actually over cap (transfers emitted only
where needed).

**What it fixes vs doesn't:**
- Fixes UNEVENNESS — B47 49→12 violations (195k/95k leveled to ~145k
  each, both under cap through 1 kg).
- Does NOT fix UNDER-ALLOCATION — B46 is a big pre-existing cohort
  under-tanked in grow-out (67k/tank at 4.7 kg needs 2× tanks); all
  its OG3-6 tanks are over, so there's no under-tank to level into.
  That needs more tanks, blocked by OG3-6 contention. Also can't touch
  B46's 143k stuck >1 kg in OG1N (INV-4 frozen; needs the OG3-6 exit
  to complete, which OG3-6 contention prevented).

**Empirical:** 248 → 212 violations (−15%), worst 257 → 185, transfers
~716 (the authorized cost), 0 drift. Combined session total:
**353 → 212 (−40%), worst 230 → 185 (−20%).**

### Q-COORD.J — Cross-scope even-out OG1/2 → OG3-6 (LOCKED 2026-05-30)

Diagnosis (2026-05-30 polish session): profiling the 162 OG6N-excluded
residual violations after Q-COORD.I showed **0 pure under-allocation**
— all 162 were over-concentration / failed redistribution. B46 alone
accounted for 84 (52%): ~288k fish PR-concentrated in two OG1N tanks
that the existing `_even_out_density` couldn't relieve because both
were over cap (no OG1/2 under-tank to spill into) AND it scoped OG1/2
and OG3-6 as separate pools. B48-class hotspots (e.g. W51 OG2S-22 at
173 kg/m³ alongside OG4N/OG6S sister tanks at 76) showed the same
pattern: the over-cap OG1/2 tank was stuck while its OG3-6 sisters had
plenty of headroom.

**Insight (operational):** the system-progression law (DESIGN §4)
explicitly permits OG1/2 → OG3-6 transfer at any weight — only
*intra-OG1/2* moves at ≥1 kg are forbidden by INV-4. So a batch with
an over-cap OG1/2 tank and an under-cap OG3-6 tank can legally
re-level across the boundary.

**Fix:** a third pass in `_even_out_density` (after the existing
sub-1kg-OG1/2 and OG3-6 passes). For each over-cap OG1/2 tank of the
batch, iterate the batch's under-cap OG3-6 tanks; transfer
`min(src_excess_to_cap, dst_room_to_90%_of_cap)` fish via cross-system
Transfer. The 90% headroom on the destination prevents the same-week
density-trigger Grade from firing on the newly-bumped destination
(which would otherwise create knock-on violations elsewhere — verified
empirically: 100% room yielded 215 violations / worst 163.8 vs 90%
room's 196 / 169.5; the 90% form is the locked configuration).

**Empirical (reference workbook):**
- Q-COORD.I baseline: 212 violations, worst 185 kg/m³
- + cross-scope even-out (90% headroom): **196 violations
  (-16, -7.5%), worst 169.5 (-15.5, -8.4%)**
- 0 count/biomass drift, 7/7 TranOG placed, harvest output unchanged.

**Why not more?** The remaining 196 are predominantly the early-life
B46 weeks (W27-W32) where *all* of B46's tanks (both OG1N) are over
cap and B46 has no OG3-6 tanks yet — there is nowhere to spill. That
residual is the genuine operator-side PR concentration limit
(Q-COORD.F) and is bounded by progression-law-mandated incremental
allocation. Recovering it would require either operator-side PR
correction or relaxing an operational rule.

### Q-COORD.K — Pre-emptive MIGRATE trigger (INVESTIGATED, NOT IMPLEMENTED, 2026-05-30)

Hypothesis: lower the EVT_MIGRATE eligibility gate from `avg_wt >= 1.0`
to e.g. `>= 0.85` (or `>= 0.7`) so cohorts approaching the 1 kg
threshold opportunistically claim OG3-6 tanks while the pool has free
slots, before they're consumed by older-FIFO batches' EVT_ADDs in
later weeks. The system-progression law (DESIGN §4) explicitly permits
OG1/2 → OG3-6 transfer at any weight, so the trigger is legal.

**Diagnosis**: traced B46's MIGRATE attempts with thresholds at 850g
and 700g. In both cases, `free_og36` (free OG3-6 tank count in the
assignment plan) is **0 from forecast week W20 onward** — the plan is
over-subscribed by older PR batches (B41–B45) whose `EVT_ADD` events
claim every OG3-6 tank to meet their own per-week 85% density target.
MIGRATE for B46 has zero destinations to claim regardless of trigger
weight. The realized state shows 1–2 free OG3-6 tanks per week because
some plan claims fail to materialize in Phase D (plan-vs-realized
divergence, e.g. INV-4 refusals), but the *plan* has no slack to
exploit.

**Empirical**: 196 violations / worst 169.5 kg/m³ at both 850g and
700g thresholds — identical to the unchanged 1000g baseline. Reverted.

**Implication**: the residual ~147 addressable violations after
Q-COORD.J are bounded by the Q-COORD.B sticky floor (PR-allocated
older batches monopolize OG3-6 even when their per-tank density is
under cap). Recovering them requires either:
1. An operator-blessed exception to the sticky floor allowing
   donor-recipient OG3-6 rebalance from less-stressed batches to
   over-cap batches.
2. Operator-side correction of B46's PR concentration.
3. A workbook with staggered input dates that avoids the W20-W34
   peak-overlap of B41-B46.

### Q-COORD.L — PR-correction: planner-action ACCEPTED with raised baseline (2026-06-01)

**Initial framing (advisory-only).** First locked as advisory-only after

four planner-action variants regressed the simulation metric:

| Variant | Claims per batch | Eligibility | Result |
|---|---|---|---|
| Naive: claim for every flagged batch (≥85% target) | up to N (target − current) | peak-week | 196 → 291 viols, worst 169.5 → 240.9 |
| Coordinated: worst-first, 1 tank max per batch | 1 | peak-week | 196 → 283 viols, worst 169.5 → 173.1 |
| Hard-cap gate: only batches projected to exceed 95 kg/m³ | 1 | peak-week | 196 → 243 viols, worst 169.5 → 193.0 |
| Sub-1kg intra-OG1/2 only (no OG3-6 claims) | 1 | OG1/2 only | 196 → 243 viols, worst 169.5 → 193.0 |

**Revised framing — planner-action ACCEPTED (2026-06-01, project lead
direction):** *"The pipeline should do that on its own, it might not
be a perfect solution but it's the best we have."*

Rationale: the simulation regression (196 → 243) is not damage — it's
the simulation honestly cascading the downstream effects of taking the
recommended action (TranOG overflow, contested OG3-6). The OPERATOR'S
real-world experience improves: instead of a passive advisory that
they may or may not act on, the recommended split appears as a
concrete Week-1 Transfer event in TransferPlan. Semantic correctness
(operator gets actionable instructions) wins over the simulation
violation counter. The cascading view shows the operator what to plan
around in the rest of week 1.

**Locked configuration:** the coordinated hard-cap variant (attempt #3,
243/193). EVT_PR_CORRECTION fires in severity order (worst-first), one
tank claimed per batch maximum, only for batches projected to exceed
the HARD cap (>95 kg/m³ — not just 85% target). Per-batch eligibility:
sub-1kg → OG1/2 (intra-OG1/2 split per progression law); ≥1kg → OG3-6
(cross-system Transfer). On the reference workbook this fires only
for B47 (1 tank @ 131 kg/m³ projected), claiming 1 OG1/2 tank at W0.

Regression baseline raised: 243 viols / 193.0 worst (was 196 / 169.5).
The 5 batches that don't trigger the hard-cap gate (B42-B46) still
get advisory bottlenecks the operator can act on upstream.

## §3 — Empirical results (reference workbook, 2026-05-28)

| Metric | Pre-coordinator | Coordinator |
|---|---|---|
| Count drift (TankContinuityAudit) | 0 | 0 |
| Biomass drift | 0 | 0 |
| TranOG arrivals placed | 7 / 7 | 7 / 7 |
| Harvest output | 8.59M kg | 8.99M kg |
| Harvest count | 1.91M fish | 2.00M fish |
| Density violations | 324 | 353 |
| Worst density | 178 kg/m³ | 230 kg/m³ |

Productivity up ~5% (better cohort spreading for in-horizon arrivals
B48-B50: violations dropped B50 12→4, B49 27→15). Pre-existing PR
batches (B47, B46) shifted higher — the honest PR-concentration signal
per §Q-COORD.F. Net violation count +29 is the cost of removing the
illegal-consolidation masking that the pre-coordinator FIFO plan used.

---

## §4 — Files touched

- `forecast/precalc.py` — `TankAssignmentPlan` dataclass;
  `_build_facility_assignment_plan`; lifetime-max backward sweep in
  `_build_batch_week_facts`; sticky-floor + diff-path in
  `_build_migration_plan`; `assignment_plan` field on `PrecalcCanvas`.
- `forecast/DESIGN.md` — §7.1a coordinator spec; §7.1 lifetime-max
  sizing; §7.6 status.
- `scripts/verify_biology.py` — per-batch lifecycle reconciliation dump
  (biology-layer verification before coordinator build).
- `scripts/compare_plans.py` — assignment_plan vs migration_plan
  side-by-side diagnostic.

---

## §5 — 6N purge pipeline interaction (VERIFIED 2026-05-28)

All six pipeline tanks (61/63/65 main, 67/69/71 sister) are in system
**OG6N** (verified via FacilityConfig). The reference workbook is
**52/52 weeks purge** (`sixn_production_start=None`).

**Coordinator handoff is clean — emergent, no special-casing needed:**

1. OG6N is excluded from the coordinator free pool, so no GROWTH_ADD /
   TRANOG_ARRIVAL ever claims a pipeline tank.
2. PR_INIT ingests OG6N tanks occupied at forecast start (B40 in 61/63,
   B41 in 69), but PR_REBALANCE releases them: B40 needs 0 tanks
   (harvesting out → all surplus), B41's OG6N tank is surplus and the
   release rule sheds non-OG12 tanks first. Released OG6N tanks can't be
   re-claimed (excluded from pool).
3. Result: **0 OG6N references in assignment_plan, 0 OG6N add/drop in
   migration_plan.** The coordinator cleanly hands OG6N to
   `placement._run_sixn_purge_week`, which manages it independently.
   Phase D already excludes OG6N from the assignment-diff transfers in
   purge mode.
4. 0 count/biomass drift confirms biomass conserved across the handoff.

**Caveat (not a regression):** the coordinator excludes OG6N
*unconditionally*, so in PRODUCTION mode it would leave OG6N
unallocated. The legacy `_build_migration_plan` did the same
(`free_pool["OG6N"] = []` unconditionally), so this is a pre-existing
shared limitation. Production-mode 6N allocation is future work; the
reference workbook never enters production mode.

## §6 — Open / deferred

- **Production-mode 6N allocation** — make the OG6N exclusion in
  `placement._OG_ALL` conditional on `is_purge_mode` so OG6N becomes
  allocatable post-transition; remove sister tanks 67/69/71 from the
  free pool in production mode. DELIBERATELY DEFERRED 2026-05-30 (this
  session) per operator decision: the reference workbook is 52/52 weeks
  purge mode, so any production-mode implementation cannot be
  regression-verified. Scope reopens when a production-mode workbook
  exists. The scaffolding cost is low (~30 lines + a gate), but writing
  unverifiable code violates the project's precalc-first-and-defendable
  principle. Track 4 of the 2026-05-30 polish session.
- **Starvation window (DESIGN §5b)** — `STAGE_STARVE` and
  `starvation_days_remaining` are defined in `state.py` and respected
  in `biology.advance_tank_one_day` (zero growth, mortality continues),
  but no code path ever sets a tank to STARVE. The window is the
  production-mode equivalent of the §5a 6N purge cycle, so it shares
  the same deferral rationale as production-mode 6N above.
- **AccumulatedOutput sheet** — listed in DESIGN §1 layer [4] alongside
  WeeklyReport / MonthlyReport but never specified. DEFERRED 2026-05-30
  pending a schema definition (operator-side decision). The other 11
  DESIGN-required outputs are implemented (see commits 49371bc /
  63d2daa / 6869e36).
- **GradedHarvest path (DESIGN §5a)** — IMPLEMENTED 2026-05-30 as a
  fallback in `placement._try_graded_move_in`. Fires only when the
  regular FIFO purge cascade finds no batch with avg ≥
  `min_harvest_weight`. Computes upper-truncated normal conditional
  means (`biology.upper_truncated_split`) to size the pickup (≥
  threshold) and retention (< threshold) portions. Unit-tested in
  `tests/test_units.py`. Does not fire on the reference workbook
  (production inventory is always sufficient), so no behavioral shift
  vs the 212 baseline — correct-by-construction.
- **Merge to master + golden-cell refresh** — left to operator per
  commit cadence. Master remains at pre-coordinator baseline as a
  clean rollback point.
- **Operator-side PR correction** — the dominant density driver
  (§Q-COORD.F) is outside the code; flag to operations.
