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

In **Run forecast** mode the sidebar also has a **Planning method** selector:
**Controller (validated)** — the default closed-loop planner (§4) — or **Global
(precalculated)** — an experimental alternative engine (§12). Same PR in, same
workbook shape out, each stamped with the method that produced it, so you can run
both and compare apples-to-apples.

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
| `max_biomass_kg` | facility biomass cap — checked against **TOTAL** facility biomass (FW + OG + 6N purge), per-week overrides in FacilityLimits | (config default; overridable per week) |
| `max_feed_per_day_kg` | facility daily feed cap — checked against total **feeding** (SW + FW) feed/day; off-feed purge fish excluded (§4.1) | (config default) |
| `max_harvest_per_week` | weekly harvest/processing ceiling (fish) | 55,000 |
| `min_harvest_per_week` | weekly harvest floor | 30,000 |
| `min_harvest_weight_g` | minimum weight a fish can be harvested at | 3,500 |
| `min_tank_control` | force-empty floor (fish): a harvest/transfer leaving fewer than this empties the tank (INV-5) | 7,000 |
| `min_transfer_count` | min rebalancer transfer size (fish): the density/load balancer won't split a sub-group **smaller than this OUT** of a tank (the OUT-side mirror of `min_tank_control`). **0 = OFF.** Suppresses tiny partial moves — trades fewer transfers for more *marginal* density over-cap (the small moves were doing fine-grained relief); whole-tank consolidation moves are unaffected | 0 (off) |
| `harvest_grade_to_min` | **opt-in (default OFF).** On a 6N purge week whose move-in is short of `min_harvest_per_week`, peel the over-weight **tail** from near-market tanks into a free 6N pair tank (big → purge); the **small tail stays in the source tank** (same batch — no extra tank needed), honoring `min_transfer_count` (won't peel a sub-min group out) and `min_tank_control` (won't leave a sub-min dribble). An **exception** (fires only when short), not a rule; trades grow-out yield (the tail lands at the low end of market weight + a grading event) for a steady floor. *Measured:* lifts genuine trough weeks ~7→4, conservation-clean (0 dropped / FW-balanced / 0-drift). Bounded by free 6N pair tanks; an anticipatory reserved-tank complement (serial consolidation into an empty tank) is the next step for the residual troughs | false |
| `default_hog_yield` | gross→HOG conversion (per-week overrides in FacilityLimits) | 0.81 |
| `scenario_name` | label for the run (reports + RunConfig) | Forecast |
| `facility_biomass_deviation_pct` | **FACILITY** setpoint band — the soft band below the (FW-inclusive) facility biomass/feed cap the harvest controller runs at; the one knob for how close to the *facility* cap to run (§4.3) | (config default) |
| `global_buffer_pct` | **SYSTEM-limits** buffer (R29) — a *separate* symmetric ±% applied to per-**system** feed/biomass caps (the rebalancer headroom + SystemLimitsAudit, `caps.py`); does **not** touch the facility setpoint above | (config default) |
| `handling_mortality_pct` | mortality applied per transfer | small |
| `sixn_growth` | 6N runs as growout (vs purge) for the whole horizon | false |
| `sixn_production_start` | date 6N flips purge → production | e.g. 2028-01-01 |
| `sixn_transition_weeks` | empty/fallow window at the 6N transition (0 = none) | 0 |
| `starvation_period_days` | in-place purge length in 6N production mode | **7** (= one weekly step; clean single-cohort pipeline) |
| `tran_og_default_tanks` | min tanks a TranOG arrival gets | 2–3 |
| `density_target_pct` | per-tank density target as a fraction of cap | 0.85–0.99 |
| `rebalance_balance_budget` | multi-objective rebalancer moves/week (density+feed+biomass) | 30 |
| `rebalance_level` | **load-LEVELING (ON by default)** — cap-agnostic balancer that spreads load off the hottest system onto the COLDEST (vs concentrating); levels feed+biomass+density together. Cuts per-system feed/biomass over-cap ~90% at the cost of more marginal-density tank-weeks (see §7.3). Set `false` for the old density-only behavior | **true** |
| `rebalance_split_budget` | split over-dense batches into free tanks (moves/week) | 8 |
| `rebalance_varqty_budget` | precise-count shaving of over-cap systems (opt-in) | 0 |
| `harvest_setpoint_lookahead_weeks` | **VESTIGIAL** — superseded by the dual-limit setpoint (§4.1/§4.3); kept for config back-compat but **not read** by the engine. Use `facility_biomass_deviation_pct` to set how close to the cap to run | 0.75 (ignored) |
| `harvest_level_load` | **harvest smoother (ON by default)** — enforce `max_harvest_per_week` as a HARD ceiling + pre-harvest earlier so harvest is flat and biomass stays under cap. Paired with `rebalance_level`, which otherwise spikes harvest (see §4.3). Set `false` for old reactive behavior | **true** |
| `harvest_smooth_lookahead_weeks` | level-load window K — weeks of coming-due biomass to spread the pre-harvest over | 6 |
| `harvest_level_target` | flat fish/week floor when level-loading (unset/null = auto from realized growth) | null |
| `placement_method` | placement engine: `greedy` (default heuristic + rebalancer) or `lns` (opt-in LP-guided optimal-layout refinement — *scaffold only today, identical to greedy until the solver phases land*; see `docs/LP_GUIDED_LNS_PLACEMENT.md`) | `greedy` |

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

The controller decides how much to harvest each week to hold the facility at **both**
its caps — biomass *and* feed — **without** spiking past the 55k/week processing
ceiling, and to **build toward** the caps when below them.

### 4.1 How it works
- **Dual-limit setpoint, measured on TOTAL facility biomass.** Both caps are
  **FW-inclusive**: the biomass and feed the controller checks count the freshwater (FW)
  fish, the grow-out (OG), *and* the off-feed 6N purge hold — not OG alone — so the
  facility never silently runs over the *true* cap. The setpoint sits one
  `facility_biomass_deviation_pct` band below the **effective ceiling** — the *lower* of
  (a) the biomass cap and (b) the biomass at which facility **feed** reaches its cap (the
  feed-implied ceiling converts only the *feeding* biomass — SW + FW — since off-feed
  purge fish eat nothing). Whichever limit binds drives the harvest. Both caps are
  **hard**; `facility_biomass_deviation_pct` is the single **soft** margin — your one knob
  for *how close to the cap to run* (§4.3).
- **Anticipates the known FW curve.** The FW biomass trajectory is known forward, so the
  controller pre-positions OG drawdown *ahead* of each FW peak (over `_FW_ANTICIPATE_WEEKS`
  = 8 weeks) instead of reacting after the total has spiked. This is what lets the
  55k/week harvest clip hold the FW-inclusive cap with **0 weeks over** — FW itself is
  never harvested; only OG is shed earlier to make room for it.
- **Build-then-maintain.** When biomass + feed are **below** the band, the predictive
  move-in floors to `min_harvest_per_week` — harvest is minimal so growth **fills the
  facility up toward the caps**. As they reach the band, harvest **ramps between min and
  max to maintain** them (without breaching). `min_harvest_weight_g` is an **eligibility
  gate** (which fish *may* be harvested), not a mandate — only the count needed to hold
  the caps is taken, not every fish that hits weight.
- **In 6N purge mode, harvest flows ONLY through 6N** (§4.2) — the facility **never
  harvests a production tank directly** while purging. In 6N *production* mode (after
  `sixn_production_start`), harvest flows through an **in-place purge**: a mature tank
  enters STARVE (weight frozen) and is harvested `starvation_period_days` later.

### 4.2 The 6N purge rotation (everything routes through it)
While 6N is in **purge mode**, depuration is a **3-pair fallow rotation** on the sister
pairs **61/67, 63/69, 65/71**:
- **Fixed cyclic order 61 → 63 → 65**, entered just *after* the empty (resting) pair —
  the empty slot marks where the rotation sits, so no fish-age data is needed. Two pairs
  purge while one rests; each week the front pair is harvested and the resting pair is
  restocked from the oldest mature production fish (Wed-fill / Fri-harvest).
- **Same batch mixes** across a pair's two tanks; **different batches** use the main +
  sister tank so they never mix. The harvest limit applies to the pair's **combined** drain.
- **Make-room routes through 6N too.** When a TranOG arrival needs an empty OG tank, the
  freed tank's fish are **moved into 6N to purge** — freeing the tank *and* staging them
  for harvest — never harvested in place. If 6N has no room, the run **warns** (a real
  capacity signal) rather than bypass.

Holding the make-room fish in 6N for the ~2-week purge keeps them in the facility longer,
so standing biomass **builds to the cap** instead of being dumped early. Verified: **0
direct production harvest across the whole purge period**, biomass utilisation ~95% mean
/ ~99.8% peak (right at the cap, no breach).

### 4.3 The tuning knob: `facility_biomass_deviation_pct`
A **Control parameter** (config/control.yaml, or the app's Configure → Control). It is
the **± tolerance band around the cap**, and it now sets how close the setpoint runs:
- **Smaller** (e.g. 0.01 = ±1% ≈ ±39 t on the 3.9M cap) → runs **tighter** to the cap
  (higher utilisation), more risk of a brief touch above it.
- **Larger** → more headroom (safer, lower utilisation).

To run **within ±X tons** of the cap, set it to `X_tons / cap` — e.g. ±50 t → `≈ 0.013`.
If a setting touches the hard cap more than you want, **widen** the band; to run closer,
**tighten** it. (`harvest_setpoint_lookahead_weeks` is now vestigial — superseded by this
band. Peak anticipation comes from two live channels instead: the FW-curve lookahead
`_FW_ANTICIPATE_WEEKS` (§4.1) and the level-load window `harvest_smooth_lookahead_weeks`
(§4.4).)

> **Utilisation is also a stocking question.** If standing biomass sits well *below* the
> band no matter how tight you set it, the pipeline isn't being fed enough fish — that's
> a **stocking** decision (more/heavier batches), not a controller one. The per-system
> caps have headroom (they sum to >100% of the facility caps), so the capacity is there;
> the stocking cadence is what fills it.

### 4.4 Harvest level-loading (ON by default): `harvest_level_load`

> Applies mainly to **6N production mode** (after `sixn_production_start`). In **purge
> mode**, harvest routes through the 6N rotation and make-room **moves** fish into 6N
> rather than dumping a whole tank (§4.2), so the make-room-spike discussion below is
> about the production-mode harvest.

A reactive controller produces **lumpy** harvest — it builds biomass to the cap then
dumps a big harvest, a sawtooth that on config(7) **breaches the 55k/week processing
cap in 12 weeks (up to 113k fish)**. Level-loading (now **on by default**) fixes this
— it:

1. **Enforce `max_harvest_per_week` as a HARD weekly ceiling** across *every* harvest
   pass (the default only clamps the main pass; make-room and production bypassed
   it). The one allowed exception is make-room
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

**On by default, paired with `rebalance_level`:** feed-leveling spreads fish thinner
→ fewer free whole tanks → more make-room harvest dumps, so on config(8) it *worsens*
harvest (11→15 weeks over 55k). Level-loading recovers and beats that: 15→**10** weeks,
max 119k→**89k**/wk, CV 0.407→**0.251**, biomass over-cap 19→9, with HOG tonnage + avg
weight unchanged, for a minor **+7 feed system-weeks**. The two travel together — set
`harvest_level_load: false` for the old reactive behavior. Anchored in REALIZED growth
(the Phase-A projection under-predicts peaks and is unsafe). Measured on config(7):

| setting | weeks over 55k | harvest CV | peak biomass | mean biomass |
|---|---|---|---|---|
| OFF (default) | 12 | 0.359 | 4.29M | 3.87M |
| ON, K=6 | 10 | 0.293 | 4.24M | 3.85M |
| **ON, K=10** | **8** | **0.247** | **4.20M** | 3.81M |

Higher K = flatter + fewer breaches, at slightly lower mean utilization. (These are
historical config(7) measurements; the setpoint-lookahead lever once tested here is
now vestigial — §4.3.) **The residual (8 weeks, biomass still ~8% over cap) is a stocking/
capacity limit** — this config is over-stocked (it wants >55k/week in burst weeks),
which no controller setting can fully fix. Use the **Optimizer (§7.2)** to find the
best level-load + knob combination for your scenario, and re-stock if the residual
matters.

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
| **FacilityMap** | tank × week grid (cell = "Batch# AvgWt/Density"); **below it**: per-system × week **feed (kg/day)** and **biomass (kg)** blocks, each with a FACILITY total row | occupancy at a glance + per-system load vs caps |
| **BatchLocations** | per-(week, batch, tank) occupancy | raw realized placement |
| **ValidationLog** | numbered warnings (# / Category / Detail), incl. FW-calibration + bottleneck (annotated with resolution) | diagnostics |
| **InputConservationAudit** | per batch: placed/dropped, harvested, standing, **FW reconciliation** (planned vs realized seawater entry) + **closed FW mass-balance** (`first_FW_count` vs `realized_TranOG + FW_mort + FW_cull`; §6 #6) | conservation + FW calibration gaps |
| **TankContinuityAudit** | per-(tank, week) balance + **facility conservation summary** | 0-drift proof |
| **ReconciliationReport / SystemLimitsAudit** | per-batch open/close balance (count reconciles **exactly** via recorded realized biology; biomass within tolerance) / per-system cap usage | deeper audits — *TankContinuityAudit is the authoritative 0-drift biomass check* |
| **RunConfig** | the exact config + scenario embedded in the output | reproducibility |

> The `ProductionReport` sheet stays the **historical** input month only — the
> *forecast* is in the sheets above (same as the reference workbook). Skipped vs the
> reference: AccumulatedReport, AccumulatedOutput, MonthlyTargets, RunComparison.
>
> **Monthly harvest attribution:** harvest is a Mon–Fri activity, so the **HarvestPlan
> Report** and the **MonthlyReport** ledger both attribute each week's harvest to
> months by **working-day** fraction (a boundary week splits by its Mon–Fri days) —
> so the two sheets' monthly HOG tie out. Continuous flows (feed, growth, mortality)
> split by calendar-day. The per-event **HarvestReport** is unprorated detail (each
> row keeps its event-date month).
>
> **Total feed is one number:** the **FeedForecast** sheets, the **WeeklyReport/
> MonthlyReport** Feed column, and the **YearlySummary** Feed total all sum the same
> three sources — OG/SW realized feed + FW (hatchery) projected feed + the 6N purge
> move-in's 4-day pre-transfer feed — so they reconcile. The per-day cap-check sheets
> (**Advisory**, **SystemLimitsAudit**) instead show the steady realized feed *rate*
> vs cap, so they intentionally exclude the move-in (a total-accounting item, not a
> per-day rate).

### Knowing what the app is doing
Run mode, the Optimize tab, and every result show a collapsible **"Active
configuration"** panel — plain-language label / value / *effect* for the settings
that actually shape a run (feed leveling, harvest smoother, TranOG tanks, setpoint,
density target, rebalancer budget, placement engine, caps). Run mode shows *what this
run will do*, a result shows *the config it used* (incl. optimizer overrides), and
Optimize shows *the base the search tunes on top of* — so you can always see what's
selected and what it does.

### The app tabs
- **Overview** — advisory issues + tank-occupancy heatmap + per-system biomass + **realized** per-system feed (read from `SystemLimitsAudit`, with the per-system feed-cap line). This is the *fed plan after harvest/FIFO* — **not** the `BiologyProjection` per-batch feed, which is the unharvested projection (fish growing along the curve, ignoring harvest/caps) and runs far higher (10k+ vs a realized ~3–4k). If a feed line looks like it spikes to 5–10× the cap, you're looking at projection feed, not the plan.
- **Per-Batch** — per-batch weight/biomass/density/losses over a period slider
- **Period Summary** — facility biomass, weekly harvest, active batches, density
- **Harvest** — totals, per-week stacked harvest, avg harvest weight, **monthly HOG rollup (sales planning)**
- **Yearly** — HOG tonnes / feed / peak biomass / count per year
- **Plan** — the **production-flow template** (TransferTemplate §A: the canonical seawater journey every batch follows — FW → OG1/2 nursery → 1 kg lock → grow-out fan-out → finishing → harvest drain) at the top, then the **per-batch plan summary** (§B): entry timing, footprint, harvest window, with a **density-risk highlight** + peak-density-per-batch chart (OVER CAP flagged)

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
6. **Closed FW-phase mass-balance** — for every batch crossing to seawater,
   `first_FW_count == realized_TranOG + FW_mortality + FW_culls`. The freshwater phase
   was previously *unaudited* — continuity (#1) only starts at OG — so a fish leak or a
   mortality/cull-accounting error inside FW could shift total smolts (and harvest
   tonnage) with every other gate green. Now gated (`test_fw_mass_balance`); a breach
   beyond ~2% (the band absorbs the FW→SW transition week) flags in
   `InputConservationAudit`. Reconciles from each batch's first projected FW count, not
   the egg seed — the egg→startfeed phase is pre-horizon for in-flight batches.

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
   **Diagnostics** tab's FW-Calibration table back-solves a suggested value — for
   **both** incoming batches *and* in-flight ones already in FW at the forecast
   start, where it solves the correction on the remaining growth to TranOG) if you
   want it to hit your plan.
3. **Check `Advisory`** for over-cap weeks. If biomass runs over the cap, **widen**
   `facility_biomass_deviation_pct` (more headroom below the cap); to run tighter,
   narrow it. (This replaced the old `harvest_setpoint_lookahead_weeks` walk, now
   vestigial — see §4.3.)
4. **Check the `Plan` tab / `TransferTemplate` §B for per-batch density.** See
   §7.1 — read the *distribution*, not the raw OVER CAP count.
5. **Check `YearlySummary` / HarvestPlan Report monthly totals** for the production
   and sales plan.
6. For a **new scenario**, re-run the K sweep (Section 4.2) to re-anchor the tuning
   table before trusting the recommendation.

### 7.1 Tuning per-batch density over-cap (the Plan tab)

The Plan tab flags every batch whose **peak tank density** exceeds its tank's
`max_density_kg_m3` cap. The **OG6N depuration/purge pool is excluded** from this
peak (and from the app's density alert and the optimizer's `density_overshoot`):
harvest-size fish held off-feed at high density just before shipping is expected, not
a stocking problem — counting it buried the real grow-out signal. Do **not** chase the
raw "OVER CAP" count to zero — read the *distribution*:

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
`density_target_pct` and `facility_biomass_deviation_pct`) is a fast read.
*Full* sweeps both directions of every relevant knob.

Both run the forecast across a grid of `density_target_pct`, the rebalancer
budgets, and `facility_biomass_deviation_pct`, and report the peak-density
distribution + conservation for each. Pick the row that **minimises severe
(>1.3×) while conservation holds** (must always be 0 dropped / 0 over-produced).
Edit `DEFAULT_GRID` in `forecast/tuning.py` to sweep other knobs/values.

**Counter-intuitive but important — this facility is tank-constrained.** The
obvious moves backfire:
- *Lowering* `density_target_pct` (more per-tank headroom) demands **more** tanks
  per batch. There aren't any, so placement crams the survivors **harder** —
  over-cap gets *worse*. On config(7), `0.99` (tight packing) is the **best**
  setting, beating 0.90/0.85.
- *Widening* `facility_biomass_deviation_pct` (more headroom below the cap) lowers
  standing biomass but frees finishing tanks, not the grow-out tanks where mid-life
  density peaks happen — so per-batch density over-cap isn't relieved by it. (The
  old `harvest_setpoint_lookahead_weeks` lever this bullet used to cite is now
  vestigial — see §4.3.)
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
| `system_overshoot` | per-system feed+biomass over-cap fraction (compliance, §7.3) | no breach |
| `density_overshoot` | per-tank density over-cap fraction, **OG6N purge excluded** (compliance, §7.3) | no breach |
| `system_peak` | the single **hottest** (system, week) load — biomass *or* feed, as a fraction of cap | **no hot spots** |

**Emphasis presets:** *Walk the line* (default — flatness + no-breach dominate),
*Flatten biomass*, *Minimize feed*, *Minimize handling*, *Respect caps* (minimize all
over-cap excursions — see §7.3), **_Minimize loads_** (keep every system's biomass+feed
as LOW and EVEN as possible — minimizes `system_peak` + all CVs + feed + handling, and
DROPS the press-to-cap reward; the "no hot spots" objective), *Balanced*; plus advanced
custom weights. In the app, **changing the emphasis re-scores instantly** without
re-running the sweep — explore the trade-offs live.

**Search method (Quick/Full grid vs Deep search).** The grids *enumerate* hand-picked
configs and mostly vary one knob at a time, so they miss **combinations** (e.g. a
`tran_og=2` + `deviation=0.005` + `K=12` combo has to be found by hand). **Deep search**
is a greedy **coordinate descent**: from the current config it tunes one knob at a time
toward the best score under the chosen emphasis, looping until nothing improves — so it
**finds combinations the grid can't** (~15–30 runs, deterministic, conservation-gated).
The emphasis *guides* the deep search, so pick it first. Both methods return the same
ranked variants, Pareto map, and apply/verify panel.

**It runs in parallel.** Each grid variant is an independent full forecast, so the sweep
runs them across a process pool (up to 8 at once) — typically **3–5× faster** than
one-at-a-time, with **byte-identical** results (they're sorted back to grid order and
re-scored). Deep search is inherently sequential (each knob depends on the previous
best), so it parallelizes only the candidate values within a knob — a smaller win. If a
restricted environment blocks process spawning, it falls back to sequential
automatically. Nothing about the *result* changes — only the wall-clock.

**The sweep grid spans the FEED↔HARVEST trade.** The strongest single lever is
`tran_og_default_tanks`: 3 tanks/arrival spreads feed thinner (fewer feed breaches)
but tightens the facility → bigger make-room harvest dumps; 2 is the reverse. The
grid tests **both endpoints explicitly** (plus density, the harvest setpoint/K, and
the two `density-only` / `reactive-harvest` controls), so the optimizer *finds* the
trade instead of you discovering it after a run.

**The trade-off map (Pareto view):** under the score table, every variant is plotted
by its two competing pressures — per-system feed/biomass over-cap (x) vs weeks over
the 55k harvest cap (y). **Lower-left is best (both held);** the lower-left envelope
is the Pareto frontier, and your operating point is a *choice* along it. This is how
you SEE that `tran_og=3` slid left-and-up (feed for harvest) before committing to it.

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

**Two buttons — same search, different ending.** Both run the identical search
(method + emphasis); they differ only in what happens *after* the recommendation:

- **▶ Run optimization** runs the search and **stops at the recommendation.** You read
  the score table + the trade-off map, switch emphases (instant re-score), then
  *manually* apply via the **Apply & verify** panel above (▶ Run full forecast / 💾 Save
  to config). Use it to **explore and decide** — you control every step.
- **🤖 Auto-optimize & run** runs the *same* search, then **acts on the recommendation
  for you**: it takes the conservation-validated best config, runs the **full forecast**
  with it, loads it into the tabs, and — if *"save the winning knobs to config"* is
  checked — persists them. One click. Use it to **commit** once you trust the objective.
  (The score table + map still render, so you can always see what it chose.)

The CLI equivalent of auto-optimize is `python -m tools.auto_optimize --emphasis
"Minimize loads" --method combined [--save-config]`. **Use the `combined` (Grid + Deep)
method** so the knobs are validated *together* as a set — auto-optimize never stacks
two separately-measured single-knob recs (the thing that can interact badly; see the
note below).

**Every auto-optimize run is logged** (app *and* CLI) to `optimize_history.jsonl` —
timestamp, method, emphasis, the winning knobs, key results (hot spot / feed / weeks
over 55k / dropped), and whether it was saved to config. The app shows the recent runs
in a **📜 Recent auto-optimize runs** panel at the top of Optimize mode, so you always
have a durable record of *what settings were used and what they produced*, kept across
sessions.

> **Don't hand-stack single-knob recommendations.** Each recommendation is measured
> against *one* baseline; applying two of them at once is unvalidated and can be worse
> than either alone (knob interactions are real). Let **Grid + Deep** (or auto-optimize
> with `--method combined`) find the *combination* — that's validated as a whole.

### 7.3 Load-leveling (`rebalance_level`) + the compliance objective

**Symptom:** the per-system utilization maps (density / biomass / feed) show a wide
spread — some systems run away over cap while others sit idle (the facility has the
capacity, it's just badly positioned). **`rebalance_level` (ON by default)** is a
cap-agnostic load-leveler: it computes each system's utilization as `max(biomass,
feed, density) / cap` (the *binding* constraint), and moves fish off the **hottest**
system onto the **coldest eligible** one — *spreading* load instead of *concentrating*
it into the most-headroom tank (which is what over-densified earlier attempts). It
levels feed, biomass and density together, from any starting state, following the
rules (1 kg move-lock, conservation, destination headroom).

**Why on by default — and the trade.** Without it the density-only balancer leaves
per-system FEED badly skewed: on config(8), **312 feed + 149 biomass** over-cap
system-weeks, with the nursery (OG1/2) running away while OG5/6 idle. The diagnosis
(2026-06-12): total OG feed *fits* capacity every week (86% mean / 97% peak; 0 weeks
over the 11-system total), so those breaches are pure **distribution**, not a capacity
wall — and `≥1 kg` fish *must* use OG1/2 feed capacity because OG3-6's feed cap alone
(21k kg/day) is below their demand (22-28k) in every week. Turning leveling on cuts
the breaches to **25 feed / 6 biomass** (−90%), 0 dropped fish, byte-identical across
`PYTHONHASHSEED`. **The cost:** per-tank density over-cap rises **110 → 195**
tank-weeks (2.3% → 4.0%) — but these are tanks pushed *marginally* over 95; the
**worst** density is unchanged (~142, capacity-bound either way), and it adds
transfers. Set `rebalance_level: false` to recover the old density-only behavior. The
optimizer still *measures* this trade via two compliance components:

- **`system_overshoot`** — fraction of (system, week) cells over their feed *or*
  biomass cap (read from SystemLimitsAudit; the per-system cap carries the
  `global_buffer_pct` R29 headroom — distinct from the facility setpoint band).
- **`density_overshoot`** — fraction of (tank, week) cells over the per-tank
  `max_density_kg_m3` cap (the OG6N depuration pool excluded — §7.1).

Leveling is on by default, so the optimizer grid carries the **`density-only`**
control (`rebalance_level: false`) instead — the sweep runs *both* and scores the
trade, so it *verifies* leveling earns its keep rather than assuming it. The
**Respect caps** emphasis (and Walk the line) weight compliance heavily and keep
leveling when its per-system gain outweighs the density/transfer cost; **Minimize
handling** may pick `density-only` (fewer transfers). So you confirm the knob
*through* the optimizer + your emphasis — never blind. **Honest limit:** every *greedy* balancer
trades feed ↔ density; a layout respecting all caps provably exists (it fits in the
tanks), but finding it perfectly needs a constraint *solver*, not a greedy pass —
leveling gets most of the way, the optimizer tells you how far.

---

## 8. Key facts to remember
- The PR sets the **start**; the scenario sets the **batches + models**. Reuse the
  PR, change the models, to test scenarios.
- The harvest spike and the biomass overage are **two symptoms of one cause** — the
  stocking plan vs the facility's combined hold (cap) + process (55k/week) capacity.
  You can trade one for the other; eliminating both needs a stocking change.
- **`rebalance_level` and `harvest_level_load` are both ON by default and travel
  together.** Feed-leveling spreads fish thinner (which would otherwise spike harvest
  via make-room dumps); the harvest smoother holds the 55k cap. Together they keep
  feed, biomass *and* harvest near their limits and flat. Turn both off for the old
  reactive behavior.
- **Per-system feed/biomass spikes are a *distribution* problem, not a capacity wall.**
  Total OG feed fits capacity every week (≈86% mean / 97% peak); `rebalance_level`
  (on by default) levels load off the hottest system onto the coldest, cutting
  per-system over-cap ~90%. A residual few % at peak weeks is the greedy-balancer
  floor (§7.3).
- **≥1 kg fish in the nursery (OG1/2) is correct, not a bug** — OG3-6's feed cap
  alone is below the grow-out fish's feed demand, so they *must* use OG1/2 feed
  capacity. The conveyor's 1 kg "move-lock" is a placement preference, not a hard
  biological rule.
- **The per-system feed chart shows *realized* feed** (`SystemLimitsAudit`), capped
  near the per-system limit — not the much-higher unharvested biology projection.
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

# The GLOBAL (precalculated) method instead of the controller (§12) — writes
# <stem>_GLOBAL.xlsx; or use the app's Run forecast -> Planning method selector
python -m tools.run_global_forecast --workbook Forecast.xlsm

# Tests (the conservation + determinism guardrails)
python -m pytest tests/ -q          # -v = test names, -s = see the pipeline prints

# Density tuner (per-batch density sweep — §7.1)
python -m tools.tune_sweep --quick [--config-template "C:\path\config_template (N).xlsx"]

# Multi-objective optimizer (§7.2) — prints the recommended knobs at the end
python -m tools.optimize_sweep --emphasis "Walk the line" [--quick] [--weights bvar=3,...]

# Auto-optimize (§7.2) — FIND the best knobs and USE them: search, then run the full
# forecast with the validated-best config and write it. --save-config also persists them.
python -m tools.auto_optimize --emphasis "Minimize loads" --method combined `
    --input Forecast.xlsm --output optimized.xlsm [--save-config]
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

---

## 10. Data flow — how it all ties together (and why no information is lost)

There is **one pipeline** and **one output workbook (24 sheets)**, and that workbook
is the **single source of truth**. Every visualization, export, report, and the
optimizer's "apply" all derive from it — so nothing is lost between stages.

```
  Inputs                         One run                   One workbook (source of truth)
  ──────                         ───────                   ──────────────────────────────
  ProductionReport ─┐                                      ┌─ where each batch is ───────────
  (current state)   ├─► run.py ─► precalc ─► placement ─►  │   BatchLocations, FacilityMap
  config/ + scenario┘            (engine, deterministic)   ├─ how it got there ──────────────
  (plan + knobs)                                           │   TransferPlan, TransferTemplate,
                                                           │   per-batch plan (Plan tab)
                                                           ├─ outcome ───────────────────────
                                                           │   HarvestPlan/Report, Feed, Yearly
                                                           └─ proof ─────────────────────────
                                                               Conservation/Continuity audits
                                       │
            ┌──────────────────────────┼───────────────────────────┐
            ▼                          ▼                           ▼
      App viz tabs              ⬇ Download .xlsm              Optimizer (grid / deep search)
   (Overview, Per-Batch,       (all 24 sheets — the          picks a CONFIG → re-runs the SAME
    Period, Harvest, Yearly,    Excel deliverable)           pipeline → SAME workbook → feeds
    Plan + per-batch plan)                                   every tab + the download again
```

**Where each batch is, at any point** → `BatchLocations` (per-tank, per-week) and the
`FacilityMap` grid. **How it got there** → `TransferPlan` (every move/grade) and the
**per-batch plan** (Plan tab: tier-by-tier milestone timeline + summary header,
exportable as CSV). **What it produced** → `HarvestPlan`/`HarvestReport`. **That it's
all conserved** → the audit sheets (0 drift / 0 dropped).

**The optimizer doesn't break the chain.** Grid *and* deep search only choose a
**config**; that config re-runs the *same* pipeline into the *same* workbook, which
feeds the *same* tabs/export. So an optimized plan is as fully traceable as a normal
run — no separate, lossy path.

## 11. Optional: LNS placement refinement (`placement_method`)

An **opt-in** second pass that tries to flatten per-system hot spots beyond what the
default rebalancer reaches. **Off by default** (`placement_method: greedy`) — turning
it on (`placement_method: lns`) never changes a greedy run's result unless it finds a
*strictly better, fully-conserved* layout.

**What it does.** After the normal (greedy) plan is realized, LNS looks at the hottest
grow-out **(system, week)** and **relocates** a feed/biomass-heavy tank's worth of fish
to a free tank in a cooler system — or, when the facility is full, tries to **swap** it
with a lighter batch in a cooler system. Each move is emitted as a normal, conserved
`Transfer`.

**Why it's safe (the floor is greedy).** Every candidate move is checked against the
**real continuity audit** (0 drift), input conservation (0 dropped batches), and must
**strictly lower the hot-spot peak** — otherwise it's reverted. Any error falls back to
greedy. So it can never make a run worse or break conservation. Knob `lns_max_moves`
(default 30) caps how many moves it will make.

**When it helps — and when it (correctly) does nothing.** It helps when the facility
has **free-tank room** (a slacker stocking plan, a future expansion, a harder PR). When
the facility is **capacity-bound** — every tank full at the peak week, the residual hot
spot being the *structural* OG3–6 feed limit (§8) — there's nowhere to move fish, so it
**correctly no-ops and greedy stands** (you'll see "greedy already near the capacity
floor" in the run log). On the current production config it no-ops; it's there for when
the layout has slack.

**Cost.** It runs an extra, audit-checked pass, so an `lns` run is **slower** than a
greedy run. Leave it `greedy` for routine runs; switch to `lns` (or add it to an
optimizer sweep — it's the `lns-placement` grid variant) when you want to test whether a
given PR's layout can be flattened further. Measure it with `python -m tools.lns_measure`
(compares greedy vs lns: hot spot, weeks-over-cap, drift, determinism).

---

## 12. Optional: the Global (precalculated) planning method

An **alternative planning engine** to the closed-loop controller (§4), selectable in
the app's sidebar (**Run forecast → Planning method → Global (precalculated)**) or
via `tools/run_global_forecast.run_global(...)`. It is **experimental**; the
**Controller is the validated default** and the one you should plan against. Global
exists so you can run the same PR through a different engine and **compare
apples-to-apples** — same workbook shape, each stamped with its method.

**How it differs.** The controller decides harvest *reactively*, week by week, against
the realized placement. The global method **pre-calculates** the whole horizon in
layers:
- **L1 (tankless harvest envelope).** Runs the biology and harvests just enough to
  hold the **true whole-facility biomass** — FW (freshwater) + OG (grow-out) + the 6N
  purge backlog, all counted — **within the cap every week**. It paces harvest
  anticipatorily against the 2-week 6N purge lag, and **primes the purge pipeline with
  the fish already mid-purge in the PR's 6N tanks** (mirroring `sixn.initial_purge_pair_
  queue`) so it doesn't over-shoot at startup.
- **L3 (placement LP).** A lexicographic linear program lays the whole horizon out at
  once — meet the per-system caps first, then minimize transfers.
- **Specific-tank pick.** Realizes the system plan as physical tanks **swap-free** (a
  tank a batch leaves stays fallow a week before another batch enters it), which is what
  guarantees **0 TANK_DRIFT** — every fish accounted for, the same continuity audit the
  controller passes.
- **Over-stock (optimizer-tuned).** A placement optimizer
  (`tools/run_placement_optimize.py`) sweeps candidate levers and bakes in the winner:
  **selectively concentrating light/young batches toward the hard density cap** to free
  tanks (heavier batches stay at operating density). It cuts density-over-cap tank-weeks
  ~30%.

**What's guaranteed.** Conservation is exact (seeded == harvested + standing +
mortality + cull, 0.0000%); `TankContinuityAudit` shows **0 TANK_DRIFT**; L1 holds the
true total within limits every week. The output is the standard workbook via the shared
writers, with a `RunConfig` "GLOBAL METHOD EXPORT" stamp and a `_planned_GLOBAL.xlsx`
filename.

**Honest limitations.**
- It emits the core sheets (HarvestPlan, FeedForecast, Advisory, WeeklyReport,
  BatchLocations, FacilityMap, TransferPlan, TankContinuityAudit, StandingTrace,
  ReconciliationReport) but **not** BiologyProjection, ValidationLog, YearlySummary,
  TransferTemplate, or Control — so those app tabs render **empty** for a Global run.
  The density heatmap + violation/worst-density/harvest KPIs work for both methods.
- The residual per-tank **density over-cap is a structural capacity limit** — the
  *same* one the controller hits (§7.1: it's a stocking/capacity problem, not a tuning
  one). No placement engine removes it without lowering the stocking target.
- It runs an LP per week, so it's **slower** than the controller (though fast once L1 is
  within limits).

**What the build established (useful regardless of which engine you ship).** Holding the
global method to the same conservation bar as the controller showed, from first
principles, that **the controller is already near-optimal** for this facility — every
correctness refinement made the global method converge toward what the controller does.
It also surfaced one actionable finding in the *shipped* tool: the binding harvest
controller enforced its cap on **OG only**, ignoring the 100–266k kg of **FW** standing
on-farm, so the true total ran ~3–4% over the cap at peaks. **This has since been FIXED
in the controller** (a 37-agent deployment audit, `docs/DEPLOYMENT_AUDIT.md`): the cap
basis is now FW-inclusive, the controller anticipates the known FW curve, and the
operator cap reports + the feed dual-limit were aligned to match (§4.1, §6). The two
engines now agree on tonnage (~8.2M kg), both 0 weeks over the true cap. Use Global as a
**cross-check and diagnostic**, not a replacement for the validated controller.
