# Atlantic Sapphire Production Forecast — User Guide

A practical guide to running the salmon production forecast, understanding its
outputs, tuning the controller, and trusting (and not over-trusting) its
conservation guarantees.

---

## 1. What the tool does

Given the farm's **current state** (a Production Report export) and a **stocking
plan** (the batches, with per-batch growth models), the tool projects the whole
facility forward week-by-week: it grows every batch, transfers fish through the
tank conveyor (freshwater → OG1/2 → … → OG6 → harvest), schedules harvest to hold
biomass under the facility cap, and writes a workbook of reports plus an
interactive app for visualization.

Two stages under the hood:

- **Stage 1 — precalc (`forecast/precalc.py`)**: the static landscape derivable
  before any tank decision — biology projections, per-week demand, facility caps,
  and *bottlenecks* (supply-vs-demand gaps). A read-only "canvas."
- **Stage 2 — placement (`forecast/placement.py`)**: walks the plan week by week,
  emits real events (TranOG entries, transfers, harvests, grades) into a tank-level
  `FacilityState`, and runs the closed-loop harvest controller.

The input workbook is **never modified**; output is written to a new file.

---

## 2. Quick start

### App (recommended)
```
cd "…/Forecasts/Tool/Python"
streamlit run app.py
```
Opens `localhost:8501`. Flow: **upload** a Production Report → **▶ Run forecast**
→ review KPIs + tabs → **download** the output workbook. The sidebar **Mode**
selector has three windows: **Run forecast**, **Configure** (edit Control
parameters and per-batch models before running), and **Tune (density knobs)**
(sweep the controller knobs and read the per-batch density distribution — §7.1).

### CLI
```
python -m forecast.run <input.xlsm> [output.xlsm] --config-dir config --scenario-dir scenario
```
If `output` is omitted it defaults to the input path (in-place; the app always
uses a fresh file). Config + scenario come from YAML in `config/` and `scenario/`.

---

## 3. Inputs

### 3.1 The Production Report (PR)
The uploaded workbook's `ProductionReport` sheet is the farm's **historical
actuals**. The tool reads it to:
- **Derive the forecast start** = PR closing date + 1 day (mirrors the VBA
  `DetectForecastStart`).
- **Hydrate the in-flight batches** — batches already stocked at the start, with
  their current tank, count, and weight.

You can reuse the same PR across runs and change only the models/knobs (see 3.3) —
the PR sets the *start state*, the scenario sets the *batches and their biology*.

### 3.2 Control parameters (`config/control.yaml` / Control sheet)
Facility-wide knobs read into `ControlParams`:

| Knob | Meaning | Typical |
|---|---|---|
| `horizon_weeks` | forecast length | 140 |
| `max_biomass_kg` | facility biomass cap (default; per-week overrides in FacilityLimits) | 3,900,000 (→ 4,200,000 raised in later years) |
| `max_harvest_per_week` | weekly harvest/processing ceiling (fish) | 55,000 |
| `min_harvest_per_week` | weekly harvest floor | 30,000 |
| `min_harvest_weight_g` | minimum weight a fish can be harvested at | 3,500 |
| `default_hog_yield` | gross→HOG conversion (per-week overrides in FacilityLimits) | 0.81 |
| `facility_biomass_deviation_pct` | ± tolerance band around the cap (R24) | 0.01 |
| `handling_mortality_pct` | mortality applied per transfer | small |
| `sixn_growth` | 6N runs as growout (vs purge) for the whole horizon | false |
| `sixn_production_start` | date 6N flips purge → production | e.g. 2028-01-01 |
| `sixn_transition_weeks` | empty/fallow window at the 6N transition (0 = none) | 0 |
| `starvation_period_days` | in-place purge length in 6N production mode | **7** (= one weekly step; clean single-cohort pipeline) |
| `tran_og_default_tanks` | min tanks a TranOG arrival gets | 2–3 |
| `density_target_pct` | per-tank density target as a fraction of cap | 0.85–0.99 |
| `rebalance_balance_budget` | multi-objective rebalancer moves/week (density+feed+biomass) | 30 |
| `rebalance_split_budget` | split over-dense batches into free tanks (moves/week) | 8 |
| `rebalance_varqty_budget` | precise-count shaving of over-cap systems (opt-in) | 0 |
| `harvest_setpoint_lookahead_weeks` | **anticipatory harvest margin** = weeks of realized growth held below the cap (see §4.2) | **0.75** |

### 3.3 Scenario batches + per-batch models (`scenario/batches.yaml` / BatchRegistry)
Each batch row carries its stocking AND its **growth models**:

| Field | Meaning |
|---|---|
| `input_date`, `input_count` | when/how many fry stocked |
| `tran_sf_date`, `tran_og_date` | freshwater→smolt, smolt→seawater transition dates |
| `tran_og_count`, `tran_og_avg_wt_g` | **planned** count + target weight entering seawater |
| `tran_og_cv` | size-distribution CV (drives the grade split) |
| `fcr_model` | FCR curve, e.g. `FCR_116_Quick` → 1.16 |
| `fw_correction` | multiplier calibrating freshwater growth/survival |
| `sgr_correction` | multiplier calibrating seawater growth |

**To test a different scenario without a new PR:** keep the same PR upload and
change the per-batch models (`fcr_model`, `fw_correction`, `sgr_correction`) — the
batches then grow/feed differently, producing a different biomass trajectory,
harvest timing, and peaks. Conservation holds regardless of the models chosen, so
this is a safe way to stress-test or re-plan.

---

## 4. The closed-loop harvest controller (and how to tune it)

The controller decides how much to harvest each week to hold facility biomass
under the cap **without** spiking past the 55k/week processing ceiling.

### 4.1 How it works
- **Setpoint = cap − anticipatory margin.** The margin is ~`harvest_setpoint_lookahead_weeks`
  weeks of the facility's **realized** weekly growth, clamped to [0.5%, 4%] of the
  cap. It is *self-adapting*: it widens when the facility is climbing toward a peak
  (pre-sheds across the calm run-up weeks) and shrinks when flat (full utilization).
  Anchored in realized growth — **not** a forward projection (the Phase-A projection
  under-predicts realized peaks ~3% and would breach the hard cap).
- **Predictive move-in + reactive supplement** drive harvest toward the setpoint;
  harvest is capped per week (partial-tank harvest lands exactly on target instead
  of overshooting a whole tank).
- In 6N production mode, harvest flows through an **in-place purge**: a mature tank
  enters STARVE (no growth/feed, weight frozen) and is harvested
  `starvation_period_days` later — pre-staged during the 6N wind-down so there is no
  harvest gap at the purge→production handoff.

### 4.2 The one tuning knob: `harvest_setpoint_lookahead_weeks`
A **Control parameter** (config/control.yaml, or the app's Configure → Control
editor). Default **0.75**. Measured on config(7) — *biomass-over-cap weeks / mean
facility utilization*:

| K | over-cap weeks | worst breach | util mean | when to use |
|---|---|---|---|---|
| 0.50 | 3 | +1.8% | 96.1% | — too loose |
| 0.60 | 2 | +0.6% | 96.2% | aggressive |
| **0.75** | **1** | **+0.4%** | **95.8%** | **default** — touch is inside the ±1% tolerance band |
| 0.90 | 0 | 0 | 94.8% | strict zero-breach of a hard regulatory cap |

**Recommendation:** default **0.75** (tightest walk of the line with a touch inside
tolerance). Use **0.90** when the cap is a hard ceiling that must never be crossed.
Below ~0.6 the breaches grow faster than the utilization gain.

> **These numbers are config(7)-specific.** The *shape* (more margin = safer, lower
> utilization) always holds, but the exact breach/utilization figures shift with a
> different stocking cadence or cap schedule. A faster-growth scenario needs a
> larger K. **Re-run the K sweep to re-anchor this table for a new scenario.** The
> controller logic is config-agnostic; only the tuning value changes.

The remaining gap from ~96% to 100% is **natural cohort troughs** (weeks with
little mature biomass), not controller slack — only the stocking cadence could fill
those.

---

## 5. Output reports — where to read what

| Sheet | What it is | Read it for |
|---|---|---|
| **HarvestReport** | one row per harvest event (Year/Month/Week/Date/Tank/Batch/Count/Gross/HOG/Avg wt) | the full harvest event log |
| **HarvestPlan** | single-table harvest plan (Week/Batch/Tank/Count/Gross/HOG…) | the actionable harvest plan |
| **HarvestPlan Report** | per-year blocks, per-batch Units/AvWt/Biomass by month + **bottom monthly TOTAL row** | **monthly sales planning** (HOG tonnes landed per month) |
| **YearlySummary** | facility-wide per-year: harvest count/HOG t/gross t/avg wt, feed t, peak+mean biomass, utilization | **year-over-year trends** |
| **TransferTemplate** | (A) the canonical batch journey through seawater; (B) per-batch summary: SW entry week + weeks-from-start, entry weight/count/density, peak tank footprint, peak density (×cap) + Density_Status flag, harvest window + weight | **the general plan at a glance** — which batches enter when, their footprint, density risk, and harvest timing |
| **Daily Harvest Schedule** | weekly harvest split Mon–Fri | daily ops |
| **WeeklyReport / MonthlyReport** | per-(batch, week/month) open/close ledger (count, weight, biomass, SGR, feed, FCR, mortality, harvest, transfers, checks) | detailed batch accounting |
| **FeedForecastWeekly / Monthly** | feed by feed-type × period matrix | feed ordering |
| **Advisory** | per-week capacity table: biomass/feed vs caps + excess + OK/REDUCE | capacity headroom + over-cap weeks |
| **FacilityMap** | tank × week grid; cell = "Batch# AvgWt/Density" | occupancy at a glance |
| **BatchLocations** | per-(week, batch, tank) occupancy | raw realized placement |
| **ValidationLog** | numbered warnings (# / Category / Detail), incl. FW-calibration + bottleneck (annotated with resolution) | diagnostics |
| **InputConservationAudit** | per batch: placed/dropped, harvested, standing, **FW reconciliation** (planned vs realized seawater entry) | conservation + FW calibration gaps |
| **TankContinuityAudit** | per-(tank, week) balance + **facility conservation summary** | 0-drift proof |
| **ReconciliationReport / SystemLimitsAudit** | per-batch balance / per-system cap usage | deeper audits |
| **RunConfig** | the exact config + scenario embedded in the output | reproducibility |

> The `ProductionReport` sheet stays the **historical** input month only — the
> *forecast* is in the sheets above (same as the reference workbook). Skipped vs the
> reference: AccumulatedReport, AccumulatedOutput, MonthlyTargets, RunComparison.

### The app tabs
- **Overview** — advisory issues + tank-occupancy heatmap + per-system biomass/feed
- **Per-Batch** — per-batch weight/biomass/density/losses over a period slider
- **Period Summary** — facility biomass, weekly harvest, active batches, density
- **Harvest** — totals, per-week stacked harvest, avg harvest weight, **monthly HOG rollup (sales planning)**
- **Yearly** — HOG tonnes / feed / peak biomass / count per year
- **Plan** — per-batch plan summary (TransferTemplate §B): entry timing, footprint, harvest window, with a **density-risk highlight** + peak-density-per-batch chart (OVER CAP flagged)

---

## 6. Conservation guarantees — what's proven, and what isn't

The tool enforces **independent** conservation invariants. The hard lesson behind
them: "all tests green" once coexisted with a silent 17% production loss because
the audits had blind spots. Each invariant below catches a *different* failure
mode (see `tests/test_coordinator_regression.py`):

1. **In-facility continuity (0 drift)** — every tank-week balances. Catches fish
   moved/grown/harvested wrong *between tanks*. Blind to fish that never enter a tank.
2. **Input conservation, both ends (0 dropped, 0 over-produced)** — every in-horizon
   batch reaches the facility, and none harvests + holds more than it stocked.
3. **Facility-level distributed loss** — sums every tank-week delta; the count
   signed/abs ratio must stay near 0. Catches a small same-sign leak spread across
   many tanks (each under the per-row tolerance).
4. **FW → seawater reconciliation** — realized seawater-entry count vs the planned
   `tran_og_count` per batch; flags batches >5% off plan. This is a **calibration
   signal, not a lost-fish gate** (the realized count is conserved downstream).
5. **GradedHarvest accounting + HOG consistency** — every event type is accounted
   for; HOG biomass matches across sheets.

### The one standing limitation (be honest about it)
**"0 drift" proves *bookkeeping* consistency, not *model* correctness.** The audits
derive "expected" from the same growth/FCR/FW curves the engine used, so a
biologically *wrong but internally consistent* model reconciles to itself. Catching
that requires **independent biological validation** (e.g. checking realized SGR/FCR
against field data), not a code change. The FW reconciliation (#4) surfaces when a
batch's seawater entry diverges from plan — your first signal that a `fw_correction`
may need re-calibrating — but it can't tell you the model's *absolute* truth.

---

## 7. Calibration & tuning workflow

1. **Run** with your PR + scenario.
2. **Check `InputConservationAudit`**: 0 dropped, 0 over-produced, and review the
   **FW_Flag** column — any "FW UNDER/OVER plan" batch reached seawater off its
   planned `tran_og_count`. Adjust that batch's `fw_correction` (the
   ValidationLog's FW-Calibration warning suggests a corrected value) if you want it
   to hit your plan.
3. **Check `Advisory`** for over-cap weeks. If biomass runs over the cap, either
   raise `harvest_setpoint_lookahead_weeks` toward 0.90 (tighter walk) or accept the touch
   if it's within your deviation band.
4. **Check the `Plan` tab / `TransferTemplate` §B for per-batch density.** See
   §7.1 — read the *distribution*, not the raw OVER CAP count.
5. **Check `YearlySummary` / HarvestPlan Report monthly totals** for the production
   and sales plan.
6. For a **new scenario**, re-run the K sweep (Section 4.2) to re-anchor the tuning
   table before trusting the recommendation.

### 7.1 Tuning per-batch density over-cap (the Plan tab)

The Plan tab flags every batch whose **peak tank density** exceeds the cap. Do
**not** chase the raw "OVER CAP" count to zero — read the *distribution*:

- **≤ 1.0** — under cap.
- **1.0–1.1** — *at* cap. Running the facility near full utilisation means many
  batches peak right at the cap; with ~10%/week growth and weekly rebalancing, a
  tank sitting at cap crosses it mid-week before the next check. This is the
  structural between-check touch, **not a problem**.
- **1.1–1.3** — mild; worth a glance but usually transient.
- **> 1.3** — **severe**: a batch crammed well over cap. These are the only ones
  worth acting on.

**To find the right knobs, sweep — don't guess.** Two ways, both driven by the
same engine (`forecast/tuning.py`):

- **In the app (recommended):** sidebar **Mode → Tune (density knobs)**. With your
  config set and a Production Report uploaded, pick **Quick** or **Full** sweep
  depth and click **▶ Run tuning sweep**. It shows the peak-density distribution
  per variant, a stacked-band chart, the **recommended** variant, and the
  severe-batch list. The current config is never modified.
- **CLI:** `python -m tools.tune_sweep --config-template "C:\path\config_template (N).xlsx"`
  (or no `--config-template` to use the repo `config/` + `scenario/` yaml; add
  `--quick` for the cheap subset).

**Quick vs full.** *Quick* (3 runs: baseline + the dominant lever on each axis —
`density_target_pct` and `harvest_setpoint_lookahead_weeks`) is a fast read.
*Full* (7 runs) sweeps both directions of every relevant knob.

Both run the forecast across a grid of `density_target_pct`, the rebalancer
budgets, and `harvest_setpoint_lookahead_weeks`, and report the peak-density
distribution + conservation for each. Pick the row that **minimises severe
(>1.3×) while conservation holds** (must always be 0 dropped / 0 over-produced).
Edit `DEFAULT_GRID` in `forecast/tuning.py` to sweep other knobs/values.

**Counter-intuitive but important — this facility is tank-constrained.** The
obvious moves backfire:
- *Lowering* `density_target_pct` (more per-tank headroom) demands **more** tanks
  per batch. There aren't any, so placement crams the survivors **harder** —
  over-cap gets *worse*. On config(7), `0.99` (tight packing) is the **best**
  setting, beating 0.90/0.85.
- *Raising* `harvest_setpoint_lookahead_weeks` frees finishing tanks but not the
  grow-out tanks where mid-life peaks happen — over-cap got worse (worst 1.42→1.92).
- More `rebalance_*` budget had **no effect** on the severe peaks: the rebalancer
  can only move fish into a tank with room, and at peak there are none.

**When no knob helps, it's not a tuning problem.** On config(7) the severe
batches (B45, B52, B51, B61, B47, B49 at 1.3–1.4×) all peak **mid-grow-out**
(+28–44 weeks from entry) — a *capacity collision*: too much biomass wanting
grow-out tanks at the same time for the tank count available. The fix is upstream
of the controller: **stagger batch entries**, **reduce input counts**, or **add
grow-out tanks** — see §8. The current config(7) controller tuning is already
optimal; the residual over-cap is a stocking-vs-capacity fact, not slack.

---

## 8. Key facts to remember
- The PR sets the **start**; the scenario sets the **batches + models**. Reuse the
  PR, change the models, to test scenarios.
- The harvest spike and the biomass overage are **two symptoms of one cause** — the
  stocking plan vs the facility's combined hold (cap) + process (55k/week) capacity.
  You can trade one for the other; eliminating both needs a stocking change.
- Conservation holds for *any* models; the models' *biological correctness* is the
  one thing the audits can't certify.
