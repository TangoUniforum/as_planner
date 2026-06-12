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
selector has four windows: **Run forecast**, **Configure** (edit Control
parameters and per-batch models before running), **Tune (density knobs)** (sweep
the controller knobs and read the per-batch density distribution — §7.1), and
**Optimize (multi-objective)** (rank knob variants on a selectable walk-the-line /
feed / handling objective — §7.2).

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
| `max_feed_per_day_kg` | facility daily feed cap (kg/day) | 34,000 |
| `max_harvest_per_week` | weekly harvest/processing ceiling (fish) | 55,000 |
| `min_harvest_per_week` | weekly harvest floor | 30,000 |
| `min_harvest_weight_g` | minimum weight a fish can be harvested at | 3,500 |
| `min_tank_control` | force-empty floor (fish): a harvest/transfer leaving fewer than this empties the tank (INV-5) | 7,000 |
| `default_hog_yield` | gross→HOG conversion (per-week overrides in FacilityLimits) | 0.81 |
| `scenario_name` | label for the run (reports + RunConfig) | Forecast |
| `facility_biomass_deviation_pct` | ± tolerance band around the cap (R24) | 0.01 |
| `handling_mortality_pct` | mortality applied per transfer | small |
| `sixn_growth` | 6N runs as growout (vs purge) for the whole horizon | false |
| `sixn_production_start` | date 6N flips purge → production | e.g. 2028-01-01 |
| `sixn_transition_weeks` | empty/fallow window at the 6N transition (0 = none) | 0 |
| `starvation_period_days` | in-place purge length in 6N production mode | **7** (= one weekly step; clean single-cohort pipeline) |
| `tran_og_default_tanks` | min tanks a TranOG arrival gets | 2–3 |
| `density_target_pct` | per-tank density target as a fraction of cap | 0.85–0.99 |
| `rebalance_balance_budget` | multi-objective rebalancer moves/week (density+feed+biomass) | 30 |
| `rebalance_feed_aware` | also relieve a system over its **feed** cap (not only density) — moves over-feed nursery fish out to grow-out early; fixes "feed-only over-cap" (see §4.4) | **false** |
| `rebalance_split_budget` | split over-dense batches into free tanks (moves/week) | 8 |
| `rebalance_varqty_budget` | precise-count shaving of over-cap systems (opt-in) | 0 |
| `harvest_setpoint_lookahead_weeks` | **anticipatory harvest margin** = weeks of realized growth held below the cap (see §4.2) | **0.75** |
| `harvest_level_load` | **opt-in smoother** — enforce `max_harvest_per_week` as a HARD ceiling + pre-harvest earlier so harvest is flat and biomass stays under cap (see §4.3) | **false** |
| `harvest_smooth_lookahead_weeks` | level-load window K — weeks of coming-due biomass to spread the pre-harvest over | 6 |
| `harvest_level_target` | flat fish/week floor when level-loading (unset/null = auto from realized growth) | null |

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

### 4.3 Harvest level-loading (opt-in smoother): `harvest_level_load`

Even with uniform stocking, the default controller produces **lumpy** harvest — it
builds biomass to the cap then dumps a big harvest, a sawtooth that on config(7)
**breaches the 55k/week processing cap in 12 weeks (up to 113k fish)**. Level-loading
fixes this. Set `harvest_level_load: true` (Configure → Control) to:

1. **Enforce `max_harvest_per_week` as a HARD weekly ceiling** across *every* harvest
   pass (the default only clamps the main pass; three others — 6N supplemental,
   make-room, production — bypassed it). The one allowed exception is make-room
   freeing a tank for a TranOG arrival: dropping a stocked batch is a worse,
   unrecoverable conservation breach, so that pass may exceed the cap for one week
   and the overage is **borrowed from next week's ceiling** (the multi-week total
   stays within cap × weeks).
2. **Pre-harvest cohorts earlier** (`harvest_smooth_lookahead_weeks` = K) so weekly
   throughput is leveled under the cap and biomass never piles into a dump — fish are
   harvested 1–2 weeks earlier (slightly lower avg weight, still above
   `min_harvest_weight_g`). Walks the line: near the cap, flat.
3. **Make-room drains the SMALLEST tank first.** The residual spikes are whole-tank
   make-room dumps (a tank harvested whole to free space for a TranOG arrival on a
   tank-tight facility — the one pass allowed over the cap). Under level-load,
   make-room frees the **smallest harvestable tank** instead of the readiest/fullest,
   so the dump — which *is* the spike — is as small as possible. This is the
   **dominant spike lever**: on config(8) it cut harvest CV 0.215→**0.157** and the
   worst spike 86k→**67k**, with avg harvest weight *unchanged-to-higher* and
   conservation intact. (Three other smoothing ideas — count-leveling, anticipatory
   make-room, tank consolidation — were tried and all made spikes *worse*, because
   they compete for the harvest budget or pack tanks fuller so the dump is bigger;
   minimizing the dump itself is what works.)

**Opt-in and safe:** default `false` = today's behavior, byte-identical (same
conservation, same determinism). Anchored in REALIZED growth (the Phase-A projection
under-predicts peaks and is unsafe). Measured on config(7):

| setting | weeks over 55k | harvest CV | peak biomass | mean biomass |
|---|---|---|---|---|
| OFF (default) | 12 | 0.359 | 4.29M | 3.87M |
| ON, K=6 | 10 | 0.293 | 4.24M | 3.85M |
| **ON, K=10 + setpoint_lookahead=2.0** | **8** | **0.247** | **4.20M** | 3.81M |

Higher K / setpoint-lookahead = flatter + fewer breaches, at slightly lower mean
utilization. **The residual (8 weeks, biomass still ~8% over cap) is a stocking/
capacity limit** — this config is over-stocked (it wants >55k/week in burst weeks),
which no controller setting can fully fix. Use the **Optimizer (§7.2)** to find the
best level-load + knob combination for your scenario, and re-stock if the residual
matters.

### 4.4 Feed-aware rebalancing (opt-in): `rebalance_feed_aware`

**Symptom:** the per-system feed chart shows a couple of systems — usually the
**nursery (OG1/2)** — running well over their feed cap (config(8): 353 system-weeks
over, worst 1.55×). **Cause:** small fast-growing fish have high feed-per-kg, so a
nursery system can be *under* its biomass/density cap but *over* its feed cap. The
multi-objective rebalancer only triggered on **density**, so it never relieved this
"feed-only" case — half the violations.

It's a **distribution problem, not capacity**: the data showed grow-out (8 systems)
has ~5× the feed headroom needed in the worst weeks, and facility-*total* feed is
never over cap. Set `rebalance_feed_aware: true` and the rebalancer also relieves a
system over its **feed** cap — moving the readiest fish out (including sub-1 kg
nursery fish pushed to grow-out *early*, where they expand into the spare capacity).
The move is capped by the destination's feed/biomass/density headroom, so it can't
create a new violation.

Measured on config(8): **feed over-cap 353 → 132 (−63%, worst 1.55×→1.36×), and
biomass over-cap 208 → 28 (−87%)** as a bonus (the same moves relieve both),
conservation intact, deterministic. **Cost:** harvest CV ticks up (~0.17→0.20) —
the extra rebalancing moves perturb harvest timing, and each move is a transfer
(handling). It's the feed/biomass-compliance ↔ harvest-flatness trade; turn it on
when per-system feed compliance matters. Raising `rebalance_balance_budget` (more
moves/week) relieves more of the residual, at more transfers.

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

### 7.2 The multi-objective optimizer (Optimize mode)

The tuner (§7.1) reads ONE axis (per-batch density). The **optimizer** ranks knob
variants on a **selectable, weighted objective** across several goals at once. Run
it from the app sidebar **Mode → Optimize (multi-objective)**, or the CLI:
```
python -m tools.optimize_sweep --emphasis "Walk the line"
python -m tools.optimize_sweep --emphasis "Minimize handling" --quick
python -m tools.optimize_sweep --weights biomass_var=3,harvest_var=3,feed_load=1
```

**Objective components** (all "less is better"), built to *walk the line* — near the
limit AND flat, not minimized:

| component | meaning | direction |
|---|---|---|
| `biomass_overshoot` | peak / weeks of biomass over cap | no breach |
| `biomass_var` | per-system CV + facility weekly swing | flat |
| `biomass_util_gap` | distance of mean biomass below cap | close to the limit |
| `harvest_var` | weekly-harvest fish CV | flat harvest |
| `harvest_overshoot` | weeks over the 55k processing cap | no breach |
| `feed_load` | mean daily feed | minimize (the one cost target) |
| `feed_var` | feed CV + swing | flat |
| `transfers_per_fish` | avg tank-to-tank moves a fish sees | minimize handling |

**Emphasis presets:** *Walk the line* (default — flatness + no-breach dominate),
*Flatten biomass*, *Minimize feed*, *Minimize handling*, *Balanced*; plus advanced
custom weights. In the app, **changing the emphasis re-scores instantly** without
re-running the sweep — explore the trade-offs live.

**The transfer/density trade is real and is why it's selectable, not auto:** the
rebalancer cuts biomass variability by *adding* transfers, so there's no single
optimum — you choose. **Conservation is a hard gate:** any variant with dropped or
over-produced fish is rejected and never recommended. When no variant beats baseline
on the chosen objective, the optimizer says so (capacity-bound — a stocking problem,
not a knob).

**Apply, verify, and visualize a recommendation.** The recommendation is just
control-knob overrides — the same knobs a normal run reads. Under it, an **Apply &
verify** panel shows them as a pasteable `control.yaml` snippet plus a **▶ Run full
forecast with these knobs** button. Clicking it:
- runs the full pipeline with the knobs applied (config never mutated — a temp copy),
- shows inline **Conservation / Harvest CV / Weeks-over-55k** so you confirm it's
  correct, and
- **loads the run into all the visualization tabs** — switch **Mode → Run forecast**
  to explore Overview / Per-Batch / Harvest / Yearly / Plan for the optimized forecast,
  and **⬇ download** the workbook (all 24 report sheets) for Excel.

To keep the knobs permanently, paste the snippet into **Configure → Control** (or
`config/control.yaml`); every later run then uses them. The CLI prints the same
snippet at the end of a sweep.

---

## 8. Key facts to remember
- The PR sets the **start**; the scenario sets the **batches + models**. Reuse the
  PR, change the models, to test scenarios.
- The harvest spike and the biomass overage are **two symptoms of one cause** — the
  stocking plan vs the facility's combined hold (cap) + process (55k/week) capacity.
  You can trade one for the other; eliminating both needs a stocking change.
- Conservation holds for *any* models; the models' *biological correctness* is the
  one thing the audits can't certify.

---

## 9. Running locally (CLI)

Everything the app does can be run from the terminal — useful for understanding the
pipeline (a direct run **narrates every stage to the console**) and for scripting.

```powershell
# The app (visual: Run / Configure / Tune / Optimize)
streamlit run app.py

# A single forecast, directly — prints the full stage-by-stage narration
python -m forecast.run --workbook Forecast.xlsm --output out.xlsm `
    --config-dir config --scenario-dir scenario

# Tests (the conservation + determinism guardrails)
python -m pytest tests/ -q          # -v = test names, -s = see the pipeline prints

# Density tuner (per-batch density sweep — §7.1)
python -m tools.tune_sweep --quick [--config-template "C:\path\config_template (N).xlsx"]

# Multi-objective optimizer (§7.2) — prints the recommended knobs at the end
python -m tools.optimize_sweep --emphasis "Walk the line" [--quick] [--weights bvar=3,...]
```

**The narration maps to the pipeline stages** (`forecast/run.py` orchestrates):
`Load` inputs → `Hydrate` facility state from the PR → `Resolve caps` (`caps.py`) →
**Precalc** projection + demand (`precalc.py`, the static "canvas") → **Layer-2
harvest plan** (`harvest_scheduler.py`) → **Phase-D realized engine** (`placement.py`
`phase_d_emit_events` — the closed-loop controller + level-load) → **Reports**
(`excel_io.py`, 24 sheets + audits). Read one run top-to-bottom and the function
names match the narration. The `config/` + `scenario/` dirs are the working config a
direct run reads; load a different `config_template (N).xlsx` into them via
**Configure → upload** in the app.
