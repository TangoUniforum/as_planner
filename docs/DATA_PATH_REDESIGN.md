# Data-Path Redesign — App as Source of Truth, Excel as Export

**Status:** Draft for review (2026-06-04)
**Goal:** Invert the data flow. Today the monolithic `Forecast.xlsm` is the
system of record for *all* inputs AND the destination for all outputs — which
causes (a) cross-sheet mismatch when only some sheets are refreshed, and
(b) dual-use "IO bleed" where HarvestPlan/TransferPlan are read as input and
written as output. Target: the app holds stable config, the operator uploads
only the ProductionReport each cycle, the forecast is built/trialed/approved
in the app, and Excel is generated as the final approved deliverable.

## Principle

Every input gets exactly one home, and no artifact is ever both read and
written by the tool:

| Role | Home | Tool access |
|---|---|---|
| Stable models / config | App config (YAML in repo) | **read only** |
| Forward plan (scenario-editable) | App config / scenario store | read + edit in-app |
| Current state | ProductionReport upload | **read only** (per cycle) |
| Forecast output | Generated output workbook | **write only** |

This kills both bug classes by construction: there is one per-cycle input to
keep in sync (the PR), and Excel is never read back.

## Input inventory — current sheet → new home

| Current sheet / reader | What it carries | New home | Notes |
|---|---|---|---|
| **Control** (`read_control`) | horizon, caps defaults (max biomass/feed/harvest, density, HOG yield, deviation/buffer, starvation, 6N params) | **Config (YAML)** | Stable. `forecast_start` is no longer stored — it is **derived** from the PR (already done, see DATA path note below). `scenario_name` becomes a scenario label in the app. |
| **Tables** (`read_biology_tables`) | SGR (FW/SW), FCR curves (1.21/1.18/1.16/1.15), mortality by week-from-input, feed types, culling schedule | **Config (YAML)** | Pure biology science. Rarely changes. Bundle as defaults. |
| **FacilityConfig** (`read_facility_config`) | tank definitions (id, system, stage, volume, density cap, feed cap, type) | **Config (YAML)** | Physical plant. Very stable. |
| **FacilityLimits** (`read_facility_limits`) | per-week facility caps + HOG-yield overrides | **Scenario config** | Operator-tuned, rolls forward, edited per scenario in-app. |
| **SystemLimits** (`read_system_limits`) | per-week per-system caps | **Scenario config** | Same as above. |
| **BatchRegistry** (`read_batches`) — *incoming half* | future batches: input/TranSF/TranOG dates, counts, weights, CV, FCR model, corrections | **Scenario config (forward batch schedule)** | THE genuinely-new input. The PR cannot supply this — it's the stocking *plan*. Must be editable in-app. |
| **BatchRegistry** — *in-flight half* | batches already in tanks | **Derived from PR** | Engine already anchors these to PR state (`og_in_flight_ids` / `fw_in_flight_ids`). Don't double-supply. |
| **ProductionReport** (`read_production_report`) | closing date + per-tank OG state + FW physical-unit state | **Upload (per cycle)** | The one recurring manual input. Derives `forecast_start` (= closing + 1) and all in-flight state. |
| **HarvestPlan pins** (`read_pinned_harvests`) | operator harvest pins | **In-app planning** | No Excel pin sheet. Harvest overrides are set in the app UI. |
| **TransferPlan pins** (`read_pinned_transfers`) | operator transfer pins | **In-app planning** | Same. This is why pin *enforcement* doesn't need to be built in the Excel architecture — it's designed once, cleanly, in the app. |

### Outputs (unchanged in role — write only)
BiologyProjection, BatchLocations, HarvestPlan, TransferPlan, HarvestReport,
Daily Harvest Schedule, Feed forecasts, Weekly/Monthly reports, Reconciliation,
TankContinuityAudit, FacilityMap, Advisory, ValidationLog. All already produced
by `excel_io.write_*` — these become the export and need no logic change.

## Per-cycle workflow (target)

1. Export the ProductionReport from the source system; upload/paste into the app.
2. App derives `forecast_start` and hydrates in-flight state from the PR.
3. App loads stable config + the active scenario (limits, forward batches).
4. Run → review KPIs / heatmap / advisory in the app.
5. Tweak scenario inputs (limits, forward schedule, harvest/transfer overrides),
   re-run, compare scenarios.
6. Approve → **Finalize to Excel** (writes the output workbook deliverable).

## What's cheap vs expensive

- **Reusable as-is:** the compute core (`biology`, `placement`,
  `harvest_scheduler`, `precalc`, `caps`, `state`) — it runs on dataclasses, not
  Excel. And every `excel_io.write_*` (the export).
- **New work:** config models + loader (replaces the `excel_io` *readers*); a
  PR-only parser (already exists as `read_production_report`); a config /
  scenario store; the in-app editing UI (limits, forward batches, overrides).

## VBA sweep findings (2026-06-04) — complete input detail

A full pass over the authoritative `Tool\*.bas` (not the diverged `vba_reference`
copy) confirmed the input set above and surfaced detail that matters for the
config schema. Every loader and its exact fields:

### Sub-inputs hidden inside the "stable config" sheets

- **Tables** is not one model — `LoadModels` + `LoadFeedTypes` read FIVE:
  - SGR FW + SGR SW (cols B, C, keyed by size col A)
  - **FCR curves for FOUR models: 1.21, 1.18, 1.16, AND 1.15** (cols D–G)
  - Mortality by week-from-input (cols H, I)
  - Feed types: name + max-weight threshold (cols K, L)
  - FW culling schedule: days-since-input + cull % (cols O, P)
- **FacilityConfig** also supplies tank **volumes** (`LoadTankVolumes`, defaults
  to 1720 m³ if absent) — already covered by the Python `read_facility_config`.

### Limits — exact layout (operator scenario config)

- **FacilityLimits** (`LoadFacilityLimits`, ForecastEngine.bas:1008): date headers
  in row 4, weekly values in rows 5–9, one row per metric:
  **Biomass (kg), Feed/Day, Max Harvest/Week, Min Harvest/Week, HOG Yield.**
  Blank cell ⇒ fall back to the Control default. HOG Yield is entered per-month
  and *broadcast* to every week of that calendar month.
- **SystemLimits** (`ReadSystemLimits`, ForecastOutput.bas:2083): col 1 = system
  (1N…6S, 12 systems), col 2 = metric (**Feed/Day, Biomass**), date headers row 4,
  weekly values cols 3+. Blank/0 ⇒ no cap.
- Both sheets' week columns are rebuilt from `forecast_start` by `RebuildSLHeaders`
  / `RebuildFLHeaders`. In the app these become editable grids keyed by absolute
  date — no rebuild step needed.

### One real gap, one non-gap (verified)

1. **FCR model 1.15 is dropped (real gap).** VBA loads 4 FCR curves;
   `read_biology_tables` loads only 1.21/1.18/1.16. A batch on `FCR_115`/`1.15`
   would resolve to an empty curve. No current batch uses it, so it's latent —
   but the new config schema must carry all four (and be open-ended).
2. **Grade Efficiency — NOT a gap (verified).** The VBA `gradeEfficiency`
   (default 0.85) is a flat graded-transfer *separation* scalar. The Python `0.85`
   values are `density_target_pct` (density *headroom*) — a different concept.
   The port models grade separation directly via batch **CV + normal
   distribution** (`NormInv` / `_apply_bottom_cull`), which supersedes the flat
   scalar. So the knob is intentionally absent, not missing. Don't re-add it.

### Control knob set

`read_control` captures 20 knobs (horizon, all caps/limits defaults, HOG yield,
deviation, handling mortality, 6N params, TranOG default tanks, global buffer,
starvation, density headroom). VBA-only knobs are either **(a) AutoSuggest
tuning** — expansion threshold, target biomass %, smooth window, segment size —
which belong to the VBA's harvest tool and do NOT map to the Python scheduler, or
**(b) Grade Efficiency** (gap #2 above).

### Integration knob surface (`OptionCWrapper.bas` `OC_*` named ranges)

The Excel→Python bridge exposes an "advanced" tuning set worth folding into the
config schema's advanced section: facility/system cap tolerances, density
hard-violation multiplier, max tanks per FW→OG entry, LP time limit, system-limits
buffer %, min allocation kg, handling mortality %, disable-TP-pins, min-harvest
overrides, purge/production starvation. Some already map to `ControlParams`;
others (LP time limit, min allocation kg) imply optimizer features beyond the
current engine — list them, don't assume them.

### Deferred (per operator, 2026-06-04)

- **AccumulatedReport** (`LoadAccumulated`): per-batch YTD mortality / feed /
  harvested-biomass carry-in for cumulative output reporting. Not present in the
  current workbook; the Python port never read it. **Parked — revisit later.**

### Promoted to first-class (2026-06-04)

- **MonthlyTargets**: originally report-only (boundary-week attribution), but the
  strategic optimizer's objective set includes "hit monthly harvest targets" — so
  per-month HOG targets become a **first-class planning input/constraint**, not
  report fluff. (The attribution *rule* — proration/fifo/target — remains a
  report-side concern and can stay deferred.)

### Optimizer (`ForecastOptimizer.bas`)

Orthogonal — not in the `RunForecast` path (stubbed). Operator already decided not
to pursue the optimization paradigm. Ignore for the new pipeline.

## Target capabilities (operator vision, 2026-06-04)

The redesign is not just "invert the data path" — it's the foundation for a
forecasting platform with three headline capabilities. The data-path inversion is
the prerequisite (can't build these on dual-use Excel), but these define the end
state.

### 1. The "mother ship" — model registry + forecast construction hub

A central workspace to **construct a forecast end to end**, and to **author/edit
the biology models themselves**: SGR, FCR, feeding, culling, mortality. Models are
not static config — they are **named, versioned, editable library entities** with
CRUD. New models can be added as *data*, not code.

**Critical constraint — reproducibility:** editing a model must not retroactively
change past scenarios. Models are **versioned**; a scenario **pins the model
versions** it ran against. This pushes persistence toward a **structured store
(SQLite)** with YAML/JSON import/export for portability — NOT flat YAML alone.

### 2. Powerful grading subsystem (Python)

Promote grading from the current CV + normal-distribution split into a
**configurable, policy-driven subsystem**:
- multi-way grading (N size classes, not just A/B big/small)
- grade-to-target (hit a target weight distribution / CV per destination tank)
- density-aware grading (grade *because* a tank is about to breach its cap)
- grading becomes a **lever the operational optimizer can pull** (see #3).

### 3. Optimization pipeline — TWO optimizers

- **Operational optimizer (biomass handling):** given current state + stocking
  plan, choose harvest / transfer / grade decisions to respect caps and eliminate
  overstocking hotspots (e.g. the B48/OG2N tank-25 climb to 216 kg/m³). Replaces
  today's heuristic scheduler/placement with a real solver. The `OC_*` knobs
  (LP time limit, tolerances, min allocation) were anticipating this.
- **Strategic optimizer (facility design):** determine the **optimal smolt input
  frequency and batch sizes for this facility** — keep tanks full and harvest
  steady without overstocking, given facility geometry + growth curves + targets.

**Architectural consequence:** the **forward batch schedule flips from a manual
input to an optimizer OUTPUT** (with operator override). And **MonthlyTargets is
un-deferred** — monthly harvest targets are the natural objective/constraint for
the strategic optimizer, so they return as a first-class input, not report-only.

### Strategic optimizer — formulation sketch (objective resolved 2026-06-04)

The operator selected **all four objectives** — so this is an explicitly
**multi-objective** problem with built-in tensions, not a single cost function.

- **Decision variables:** over the planning horizon, for each candidate input
  slot — whether to stock, the **batch size** (smolt count), the **cadence**
  (weeks between inputs), and possibly target initial weight/timing.
- **Fixed givens:** facility geometry (tanks, systems, volumes, density + feed +
  biomass caps), biology models (growth / mortality / FCR), grading + transfer
  capability, smolt supply limits, and the current state (PR) as the start.
- **Constraints:** every week, every tank/system within density + feed + biomass
  caps; harvest within min/max per week; lifecycle timing (FW → TranOG → grow-out
  → harvest); transfer/grading capacity; smolt availability.
- **Objectives (all four, competing):**
  1. **Even harvest tonnage** — minimize week/month HOG variance.
  2. **Hit monthly targets** — minimize deviation from MonthlyTargets.
  3. **Max facility utilization** — maximize biomass throughput / tank-fill.
  4. **Min overstocking + cost** — minimize density-cap breaches and cost/kg.
- **Inherent tensions (need a weighting or priority scheme):**
  - (3) utilization ↔ (4) overstocking — fuller tanks risk density breaches.
  - (2) monthly targets ↔ (1) even tonnage — lumpy targets break smoothness.
  - Resolve via weighted-sum, lexicographic priority, or a Pareto frontier the
    operator picks from. **This weighting is the operator's tuning surface.**
- **Relationship to the operational optimizer:** strategic sets *what to stock
  when*; operational decides *how to harvest/transfer/grade* within a given plan.
  Natural as a **bi-level loop**: strategic proposes a stocking plan → simulation
  + operational optimizer evaluate it → strategic refines. Likely a
  heuristic/metaheuristic outer search (over cadence/size) wrapping a solver or
  simulation inner loop — flag for the solver-stack decision.

### Layered target architecture

```
Mother ship (config workspace + versioned model registry)
   ├─ Biology models: SGR / FCR / mortality / culling / feeding  (CRUD + versions)
   ├─ Facility: tanks, systems, volumes, caps
   └─ Limits: facility + system, per-week
                    │
   Strategic optimizer ──► proposes stocking plan (input freq + batch size)  ◄─ override
                    │
   Simulation engine (biology → placement → grading)   ◄── PR upload (current state)
                    │
   Operational optimizer ──► harvest / transfer / grade under caps
                    │
   Review · compare scenarios · approve ──► Excel export (deliverable)
```

The compute core (`biology`, `placement`, `harvest_scheduler`, `precalc`) is
reused; the optimizers wrap/replace the scheduler+placement heuristics; grading is
extended in place; the mother ship is new UI + the structured store.

## Open questions

1. **Persistence (decided by capability #1):** SQLite for the model registry +
   scenarios + runs (needs versioning + reproducibility), with YAML/JSON
   import/export for portability. Flat-YAML-only is ruled out by the versioned
   mother-ship requirement.
2. **Strategic optimizer objective:** RESOLVED (2026-06-04) — all four
   (even tonnage + monthly targets + max utilization + min overstocking/cost),
   multi-objective. See the formulation sketch above. Remaining sub-question: the
   **weighting/priority scheme** between the four competing objectives (weighted
   sum vs lexicographic vs Pareto) — this is the operator's tuning surface.
3. **Grading scope:** which of multi-class / grade-to-target / density-triggered
   grading is needed first, and what's the operationally realistic grading action
   (how many size classes, transfer/handling limits)?
4. **Calibration loop:** `FW_Correction`/`SGR_Correction` are *outputs* of the FW
   calibration (Diagnostics residuals suggest values). In-app: run → see residuals
   → apply → re-run. Persist applied corrections on the scenario, or on the batch?
5. **Solver stack:** operational + strategic optimizers — MILP via OR-Tools /
   PuLP, or heuristic/metaheuristic? Affects dependencies + runtime expectations.

## Phased rollout (each phase ships working software)

**Foundation — data-path inversion (prerequisite for everything below):**
- **Phase 1 — decouple stable config.** Load biology models + FacilityConfig +
  Control from the structured store (seeded from the current workbook); engine
  runs without reading them from Excel. Excel input still allowed as override.
- **Phase 2 — PR-only upload + app-managed scenario.** Limits + forward batch
  schedule + overrides move to the scenario store (SQLite) with UI. Upload only
  the PR.
- **Phase 3 — Excel output-only.** Remove the input-read path; add scenario
  save/compare/approve and "Finalize to Excel" export.

**Platform — the three headline capabilities:**
- **Phase 4 — Mother ship.** Model registry UI: CRUD + **versioning** for SGR /
  FCR / mortality / culling / feeding; scenarios pin model versions. This is the
  forecast-construction hub.
- **Phase 5 — Powerful grading.** Multi-class + grade-to-target + density-aware
  grading subsystem, configurable per scenario.
- **Phase 6 — Operational optimizer.** Solver for harvest/transfer/grade under
  caps; replaces the heuristic scheduler/placement. Kills overstocking hotspots.
- **Phase 7 — Strategic optimizer.** Designs the stocking plan (input frequency +
  batch sizes) against the objective from Q2; forward batch schedule becomes an
  optimizer output with operator override; MonthlyTargets returns as a first-class
  input.

Phases 4–7 can reorder by priority once the foundation is in place.
