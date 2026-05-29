# AS Production Forecast — Design

Greenfield Python replacement for the legacy VBA tool (v8). Planner mode:
the tool decides placements + harvest schedule. Reads + writes
`Forecast.xlsm` in the parent folder.

---

## 1. Pipeline

```
INPUTS
  Control            (defaults, scenario params, buffers)
  BatchRegistry      (incoming batches: dates, models, targets)
  Tables             (SGR, FCR, mortality, feed types, culling DSI)
  FacilityConfig     (tanks: system, volume, density cap, feed/day cap)
  FacilityLimits     (per-week facility overrides; blank = use Control)
  SystemLimits       (per-week per-system caps; blank = no cap)
  ProductionReport   (per-(batch, tank) state at forecast_start - 1 day)
  HarvestPlan        (operator-pinned harvests, if any; dual-use sheet)
  TransferPlan       (operator-pinned transfers, if any; dual-use sheet)

      v
[1] Biology engine
      For each in-flight (batch, tank): hydrate from ProductionReport,
      project forward applying batch's models.
      For each incoming batch: project Egg -> FW -> SW from Input_Date.
      Output: per-(batch, tank) trajectory, internal grain = daily.

      v
[2] Harvest scheduler
      Walk weeks anchored at forecast_start. FIFO oldest first.
      Determine kg/week per batch to keep facility biomass + feed under
      cap (with R24 / R29 buffers). Respect min harvest weight,
      max/min harvest count.
      Seed from operator-pinned HarvestPlan rows; extend as needed.
      Output: HarvestPlan rows, Daily Harvest Schedule (Mon-Fri split).

      v
[3] Placement / system allocator
      For each (batch, tank, week) of remaining biomass, allocate tanks
      and emit transfer / split / grade events.
      Seed from operator-pinned TransferPlan rows; extend as needed.
      Hard caps: tank density (Control + FacilityConfig), system Feed/Day
      and Biomass (SystemLimits).
      Continuity: every count change in a tank is one of 5 logged events.
      Objectives (in priority order):
        a. feasibility (caps + system-progression law)
        b. spread across systems (minimize peak load + variance)
        c. minimize transfer count
      Output: TransferPlan, BatchLocations.

      v
[4] Reporting + Diagnostics
      WeeklyReport, MonthlyReport, FeedForecastWeekly/Monthly,
      HarvestReport, HarvestPlan Report, FacilityMap, AccumulatedOutput.
      Advisory + ValidationLog consolidate every warning/issue
      (FW calibration residuals + planner infeasibilities + soft
      cap excursions + min_tank_control violations).
      Control status block (R8-R16) overwritten with run summary.
```

**Time grid.** Internal simulation = date-driven (daily where required:
mid-week TranOG, starvation, intra-week event ordering). External
aggregation = weekly ticks anchored at `forecast_start + N*7`. Daily
resolution surfaces in `Daily Harvest Schedule` and in dated
`TransferPlan` rows.

**ProductionReport handoff.** PR closing date = `forecast_start - 1 day`.
No bridging projection needed; PR closing state -> forecast week-0
opening state.

---

## 2. Per-(batch, tank) state in OG

**FW + Egg phases:** single-stream per batch (no tank-level
differentiation needed — handled by current `biology.py`).

**At TranOG:**

1. Handling mortality applied to count.
2. Reconciliation cull (bottom-X% truncated normal) sized to land on
   `TranOG_Count`.
3. Remaining distribution split into 2 size classes (big / small) by
   the post-cull median.
4. Spread across `Default tanks TranOG` (Control R28, default 3) tanks.
   - Default attempt: equal counts per tank (biomass differs).
   - Algorithm may test equal-biomass alternative (counts differ).
   - Tank-count → size-class mapping (algorithm-decided, default
     biased to spread):
     - 2 tanks: 1 big, 1 small
     - 3 tanks: 2 of one class + 1 of the other (algorithm picks)
     - N tanks: distribute proportionally
   - Tanks selected across both OG1 and OG2 where possible (spread
     objective).

**Each (batch, tank) thereafter:**

- Own count, avg wt, CV.
- Same `FCR_Model` + `SGR_Correction` (multipliers apply to its current
  avg wt — model identity is per-batch, state is per-(batch, tank)).
- Growth + mortality continuous (no event row).
- Count + state change only through one of the 5 event types in §3.

**One-batch-per-tank invariant.** A tank holds fish from at most one
batch at any time. No mixing, ever.

---

## 3. Event grammar (5 event types)

Every count change in a tank is one of these. Each is a logged row in
either `TransferPlan` or `HarvestPlan`.

| Event | From | To | TransferPlan row | Notes |
|---|---|---|---|---|
| **TranOG entry** | FW (single-stream) | N OG1/OG2 tanks | one row per destination | 2 size classes spread across N tanks |
| **Transfer** | 1 tank | 1+ tanks (split) | one row per destination | Within-system or cross-system |
| **Grade** | N source tanks | N+1 destination tanks | one row per source-destination link | Adds one size class; cannot grade in place. 2→2 also valid (resize two tanks). |
| **Harvest (direct)** | 1 tank | (processing) | HarvestPlan row | Partial allowed; count drops, batch retained |
| **Graded harvest** | 1 source tank | 1 harvest pickup tank + 1 retention tank | TransferPlan (source→pickup, source→retention) + HarvestPlan (pickup→processing) | All three tanks remain same batch (one-batch-per-tank preserved) |

**Mortality** is continuous — it reduces tank count daily without an
event row. **Growth** is continuous — it changes avg wt daily without
an event row. Both apply per-day to each `(batch, tank)` state.
Starvation = zero feed + zero growth + mortality still applies +
biomass still counts to facility cap + continuity preserved.

---

## 4. System-progression law

```
                   < 1 kg : split / grade allowed *within* OG1/2
                            (must move between tanks, no in-place)
                            outbound to 3/4/5/6 allowed any time

   FW -> OG1/OG2 ----------------------------------------------> 3/4/5/6
                                                                    |
                                                                    | (free transfer
                                                                    |  any tank ↔ any tank
                                                                    |  in 3/4/5/6)
                                                                    v
                   >= 1 kg : tanks frozen *within* OG1/2.       6N tanks
                            No tank-to-tank moves inside       (purge mode) or
                            OG1/2. Only outbound transfers     direct harvest
                            to 3/4/5/6 + harvest + mortality +  after starvation
                            growth.                            (production mode)
```

**Key constraints:**

- TranOG entry is the only way fish enter OG (always OG1 or OG2 or
  spread).
- Above 1 kg in OG1/2: fish may *remain* (no forced exodus) but cannot
  be re-arranged between OG1/2 tanks (equipment limit).
- Within 3/4/5/6: any-to-any transfer allowed.
- Outbound paths depend on 6N mode (§5).

---

## 5. 6N twin-mode behavior

Governed by Control R26 `6N Production Start Date`.

### 5a. Purge mode (date blank, or before date)

- 6N = depuration only. Sister pairs: 61/67, 63/69, 65/71.
- No feed (no system feed cap), no biomass cap.
- Harvest flow: 3/4/5/6 → 6N tank → harvest (after rolling 2-week purge).
- Round-robin pipeline:
  - **Week 1 only** (forecast startup, no prior plan): harvest the 6N
    pair with **lowest combined count**. This rule seeds the cycle.
  - **Week 2+**: follow round-robin order (next pair in sequence).
    Each week harvest the next pair, and move FIFO-eligible biomass
    (oldest batch first; tiebreak by avg wt closest to harvest weight)
    into the just-vacated pair to keep the rolling 2-week pipeline
    full.
  - From **Week 3** onward, the harvested pair is what was moved in
    on Week 1 (cycle established).
  - Pair tanks never have equal count (assumed); if observed → log +
    fail.
- Single-tank usage: prefer 61/63/65 (main tanks). Sister 67/69/71
  used only when both tanks of a pair are needed (harvesting 2 batches
  in same week).
- Graded harvest path active for batches near min harvest weight:
  per source tank, split into a `>= harvest weight` portion and a
  `< harvest weight` portion such that the harvest-pickup tank's
  resulting avg wt lands at or above `Min_Harvest_Weight`. The big
  portion goes to the per-batch harvest pickup tank; the small portion
  accumulates in the per-batch retention tank. The exact ratio is
  computed from the source tank's distribution (avg wt + CV) — 90/10
  is one possible outcome, not a fixed split. Iterate FIFO across the
  batch's tanks until harvest demand met.

### 5b. Production mode (on/after date)

- 6N = full production system. Tanks 67/69/71 unavailable.
- Standard system caps apply (SystemLimits row "6N" for Feed/Day +
  Biomass).
- Harvest flow: 3/4/5/6 (any tank) → starvation in-place →
  harvest direct. No 6N transfer required.
- Starvation period: Control R30 `Starvation period (days)` (default
  10). During starvation: zero feed, zero growth, mortality + biomass
  retention as normal.
- Operator-pinned `HarvestPlan` rows past the transition date carry
  implicit pinned starvation windows backward (the planner reserves
  zero-feed time for those tanks).

### 5c. Transition

- Control R27 `6N Transition Window (weeks)` (default 2) = empty
  period between last purge-cycle fish leaving 6N and first
  production-stocking of 6N tanks.
- During this window: 6N tanks empty, not available for either
  purpose.

### 5d. Forecast start in production mode

- No 6N purge pipeline to seed.
- Operator pre-populates initial `HarvestPlan` rows (oldest-first
  schedule of tanks queued for harvest at forecast start).
- Algorithm builds on top of those pinned rows.

---

## 6. Cap resolution

Three cap levels:

| Level | Source | Override | Buffer |
|---|---|---|---|
| Tank | `FacilityConfig.max_density_kg_m3 × volume_m3` per tank | — | — |
| System | `SystemLimits[system, week, metric]`; blank = no cap | — | R29 `Global buffer` (5% default) |
| Facility | `Control` defaults; `FacilityLimits[week, metric]` overrides per (week, field); blank cell = fall back to Control | as listed | R24 `Target Biomass deviation` (±1% default) — **symmetric** tolerance on facility biomass (allows both +1% over and −1% under the target). Feed cap assumed same ± behavior; harvest count caps strict. |

**Order:** facility caps satisfied first (drives harvest schedule),
then system caps drive placement. If placement cannot meet system caps
with the harvest schedule given, planner first attempts to use the
buffer headroom, then escalates to Advisory.

**HarvestPlan dual-use:** operator-pinned rows are truth (treated as
hard constraints); algorithm adds further rows to meet facility caps.
Pinned harvests post-transition imply pinned starvation windows.

---

## 7. Placement algorithm

Forward-deterministic precalc over the full forecast horizon. Four
phases, run in order. No iteration unless local repair forces it.

### 7.1 Phase A — Per-batch trajectory (independent)

Precompute for every batch B and every week W in the horizon:

- `count_B(W)`, `biomass_B(W)`, `feed_B(W)`, `avg_wt_B(W)` — from biology
- `harvest_B(W)` — from harvest scheduler
- `tanks_min_B(W)` = `ceil(biomass / max_kg_per_tank)` — minimum tank
  count under density cap
- `E_B(W)` = eligible systems set — from §4 progression law + §5 6N mode

Output is each batch's full **load curve** + **system corridor**
through the forecast horizon, derived deterministically with no
dependence between batches.

**Lifetime-max tank sizing.** `tanks_min_B(W)` is not the current
week's density requirement — it is the **maximum** density requirement
over the cohort's remaining lifecycle until the next harvest-driven
biomass shrink. A backward sweep over each batch's weekly facts
propagates each future peak back to earlier weeks, so the plan demands
the full lifecycle tank count from the cohort's first OG week. Rationale:
a tank claimed while a batch is under 1 kg can be filled via a legal
intra-OG1/2 split; once the batch crosses 1 kg, INV-4 forbids intra-OG1/2
moves and the only way to spread the cohort is one-tank-at-a-time
cross-system migration throttled by INV-1. Sizing for the lifetime peak
up front avoids being caught short when the free pool dries up.

### 7.1a Facility assignment coordinator (precalc, between A and B)

The coordinator turns Phase A's per-batch load curves into a single
canonical **(batch, week, tank_id) assignment table** for the whole
horizon — `precalc.TankAssignmentPlan`. Phase B/C consume this table;
the migration plan is a thin diff layer over it (keep/add/drop per
week derived by comparing consecutive weeks' tank sets).

**Interval-layout model, not greedy.** Each batch is a step function on
the (tank × week) grid: a constant tank count over each segment between
transitions. The coordinator walks a chronologically-sorted event list
and commits multi-week tank claims, so a tank claimed for a batch at
week N stays with that batch through the segment — stickiness by
construction, not by runtime tiebreak.

**Event types** (processed in this priority within a week):

1. `PR_INIT` — pre-existing batch's ProductionReport tank set (locked
   starting state).
2. `PR_REBALANCE` — at forecast start only, release any PR tanks beyond
   the batch's lifetime-max need back to the free pool. PR is the
   operator's given input; the coordinator normalizes it to the
   canonical plan at horizon start. This is the **only** week tank
   counts may drop without harvest; from week 2 on, the sticky floor
   applies.
3. `TRANOG_ARRIVAL` — hard placement into OG1/2 (with OG3+ overflow if
   OG1/2 vacancies are insufficient; bottleneck if still short).
4. `HARVEST_RELEASE` — batch shrinks; release tanks **before** any adds
   in the same week so freed capacity rejoins the pool immediately
   (models "as larger tanks empty, smaller tanks fill").
5. `GROWTH_ADD` — batch grows; claim newly-freed + free-pool tanks.

**Deterministic rules — no scoring weights.** Every assignment traces
to a stated rule, never a tuned weight:

- **Batch order within a (week, type):** FIFO by `input_date` ascending
  (operational fact — the operator stocked older batches first).
- **Tank pick for GROWTH_ADD:** (1) prefer systems the batch already
  occupies (no Transfer needed); (2) otherwise eligible systems in
  alphabetical `system_id` order; (3) within a system, lowest-numbered
  free tank.
- **Tank pick for RELEASE / REBALANCE:** OG3+ before OG1/2 (OG1/2 is
  the constrained resource for TranOG); within that, highest tank_id
  (newest additions shed first).

**Sticky floor.** Total tank count per batch never decreases except via
a `HARVEST_RELEASE` event (harvest reduced the cohort). Outside harvest
weeks the count is monotonic non-decreasing. This is an operational
rule, not an optimization: consolidating a batch into fewer tanks
without harvest would be a physically meaningless move.

**Why deterministic, not LP/scored.** The plan must be defendable —
every (batch, week, tank) cell traces to one of the rules above. A
deterministic single forward pass produces the same plan every run and
the per-pair `notes` explain each decision. Density violations that
remain are an honest operational signal (PR concentration + cohort
timing exceed facility capacity under the operational rules), not an
artifact of either reactive grade-trigger logic or weight tuning.

### 7.2 Phase B — System assignment (global, single forward pass)

Walk weeks forward. Within each week, process batches in TranOG-entry
order (existing batches from ProductionReport are pre-placed at week 0).

For each batch B in week W: pick a set of systems from `E_B(W)`
totaling `tanks_min_B(W)` tanks.

- **Hard:** system feed + biomass within cap + R29 buffer
- **Hard:** physical tank count per system ≥ batches assigned
  (one-batch-per-tank, INV-1)
- **Hard:** TranOG entry must succeed; if not enough OG1/OG2 vacancies
  → infeasibility, log + fail (per user direction: we can never not
  have enough room for an incoming FW group)
- **Soft:** spread across systems by default
- **Soft:** minimize peak system feed + biomass + cross-system variance
- **Tiebreak:** FIFO batch age, then load smoothing

### 7.3 Phase C — Tank assignment (sticky + rotation-aware)

Within each (system, week), assign physical tank IDs to each batch's
tank-count slot.

- **Sticky.** A tank stays with a batch week-over-week unless tank
  count must drop or the batch leaves the system. No churn.
- **Rotation.** When a new tank slot opens for a batch, pick the tank
  in the system **most recently emptied by a prior harvest**. Keeps
  tanks full as a rotation cycle; initial stocking decisions are made
  anticipating future need.
- **TranOG availability look-back.** If Phase B says batch B needs 3
  OG1/OG2 tanks at TranOG, Phase C verifies (or schedules in earlier
  weeks) the prior batches' moves out of those tanks. If impossible →
  infeasibility, log + fail.
- **min_tank_control floor (INV-5).** Any removal that would leave a
  tank under threshold force-empties the whole tank.

### 7.4 Phase D — Event emission

Walk the assignment table week by week; diff successive weeks:

- New tank added to a batch → split (transfer event)
- Tank dropped from a batch → harvest event OR consolidation transfer
- Tank reassigned across batches → harvest (old) + TranOG / transfer
  (new), sequenced within the week
- Grade events fired when needed (target harvest avg wt, density-driven
  splits above 1 kg in OG1/2 that require routing through 3/4/5/6)
- **INV-4 repair**: any proposed within-OG1/2 transfer above 1 kg is
  re-routed through 3/4/5/6 instead

Emit: `TransferPlan` rows, `HarvestPlan` rows (mostly already from
scheduler), `BatchLocations` table.

### 7.5 Objective priority (big-picture)

1. **Feasibility** — caps, progression law, TranOG availability
2. **Facility utilization** — rotation rule + `min_tank_control`
3. **Load smoothing** — minimize peak system feed + biomass +
   variance across systems over time
4. **Spread** — default, fall back to compact only when space tight
5. **Transfer minimization** — sticky tank assignment

### 7.6 Where local repair kicks in

Forward precalc handles the common case. Three predictable repair sites:

- **System-cap collision** — shift one batch's new tanks to a
  less-loaded system this week.
- **TranOG vacancy shortfall** — the coordinator (§7.1a) schedules
  prior batches' release out of OG1/2 before TranOG arrivals via event
  ordering (RELEASE before ADD); PR_REBALANCE frees PR surplus up front.
- **6N round-robin under tight harvest demand** — graded harvest path
  fires; ratio computed from source distribution.

LP / MILP is a future wrapper only if forward precalc proves
insufficient. The coordinator (§7.1a) deliberately avoids it: a
deterministic interval-layout pass keeps the plan defendable.

**Status (2026-05-28, `feature/precalc-coordinator`).** Coordinator
implemented and wired (migration plan derives diffs from the assignment
table). Empirical on reference workbook: 0 count/biomass drift, all 7
TranOG arrivals placed, harvest output 8.99M kg (vs 8.59M pre-
coordinator — better cohort spreading). Density violations 353 (vs 324
pre-coordinator) — the residual is PR-concentration-bound (B47/B46
arrive over-concentrated from ProductionReport and cannot be spread
under the operational rules without operator-side PR correction). See
`docs/GREENFIELD_COORDINATOR_LOCKS.md` for the full lock record.

---

## 8. Continuity invariants

These hold at every (tank, day) in the simulation. Violations are
diagnostic failures.

**INV-1.** A tank holds at most one batch.

**INV-2.** A tank's **batch identity** changes only via a logged event
in `TransferPlan` (transfer, split, grade, TranOG entry, graded
harvest). **Count** changes via logged events OR continuous mortality.
**Avg wt** changes via logged events OR continuous growth. No silent
batch-identity change is allowed.

**INV-3.** Sum of fish across all event rows referencing a batch on day
D, applied to (yesterday's count − mortality), equals today's count.

**INV-4.** No transfer between two tanks within OG1/2 if either
side's avg wt ≥ 1 kg.

**INV-5.** `min_tank_control` floor: any planned action that would
leave a tank below `Control.Min_Tank_Control` fish must instead take
ALL fish (force-empty the tank). Protects against undersized leftovers
and forces consolidation toward fully utilized facility.

---

## 9. Module breakdown

Existing code (`forecast/`):

- `models.py` — dataclasses. Needs additions: `TankState`, `BatchTankState`,
  `TransferEvent`, `HarvestEvent`, expanded `ControlParams`. Drop
  `TankConfig.capacity_kg`.
- `excel_io.py` — readers + writers. Needs:
  - new readers: `FacilityLimits`, `SystemLimits`, `ProductionReport`,
    `HarvestPlan` (input mode)
  - new writers: `TransferPlan`, `HarvestPlan`, `BatchLocations`,
    `HarvestReport`, `WeeklyReport`, `MonthlyReport`,
    `FeedForecastWeekly`, `FeedForecastMonthly`, `Daily Harvest Schedule`,
    `Advisory`, consolidated `ValidationLog`, Control status block
    overwrite
- `biology.py` — restructure: date-driven internally, per-(batch, tank)
  state in OG. FW + Egg stay single-stream. Mortality and SGR/FCR
  interp re-used. TranOG split logic extended to N tanks × 2 size
  classes.

New modules to add:

- `time_grid.py` — date arithmetic, forecast week index, weekly
  aggregation helpers.
- `state.py` — `(batch, tank)` state container, event application,
  continuity invariant checks.
- `harvest_scheduler.py` — layer [2] from §1. FIFO walk, facility-cap
  driven, pin-aware.
- `placement.py` — layer [3]. Tank allocation, system-cap binding,
  spread + minimize-transfers objectives, system-progression law
  enforcement, 6N twin-mode dispatcher.
- `sixn.py` — 6N-specific logic (purge round-robin, production
  starvation, transition window).
- `events.py` — 5 event types and their effect on tank state.
- `caps.py` — cap resolution (Control / FacilityLimits / SystemLimits
  + buffers).
- `advisory.py` — consolidated diagnostic output writer.
- `reports.py` — weekly/monthly/feed/harvest rollup writers.

Build order:
1. `time_grid.py` + restructure `biology.py` to date-driven
2. `state.py` + `events.py` (continuity foundation)
3. ProductionReport reader + biology hydration for in-flight batches
4. `caps.py` + remaining readers
5. `harvest_scheduler.py` (layer 2)
6. `placement.py` + `sixn.py` (layer 3)
7. `reports.py` + `advisory.py` (layer 4)
8. Validation against v8 reference outputs

---

## 10. Open / to-confirm

- `capacity_kg` column physical deletion from FacilityConfig sheet
  pending. Code already ignores it.

Resolved during framework:

- Facility feed cap buffer = ± symmetric (same as R24 biomass behavior).
- v8 reference outputs not available. Validation = operator-eyeball
  against intuition + known cases.
