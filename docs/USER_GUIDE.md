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

The **app** never modifies your input workbook — it works on a copy and writes a fresh output file. The **CLI** writes the report sheets back into the input workbook unless `--output` names a different path (§2).

---

## 2. Quick start

### App (recommended)
```
cd "…/Forecasts/Tool/Python"
streamlit run app.py
```
Opens `localhost:8501`. Flow: **upload** a Production Report → **▶ Run forecast**
→ review KPIs + tabs → **download** the output workbook.

The sidebar **Mode** selector lists seven windows, in the order you normally work
in them. Each carries a one-line caption in the app itself; the same list, with
pointers into this guide (the seventh, **Accuracy (forecast vs actuals)** — which
grades a past forecast against the ProductionReport that followed it — is §13):

| Mode | What it is for | Section |
|---|---|---|
| **Configure (models & control)** | Set up once — biology curves, tanks, batches, per-week limits, control knobs, harvest targets and prices | §3 |
| **Run forecast** | The everyday step — run your chosen plan on today's PR and download the workbook | §5 |
| **Analyze (find my best plan)** | "Which plan should I use?" — runs every engine, searches the knobs, grades them all, recommends ONE | §12 |
| **Compare & Choose (all methods)** | Run the engines side by side and pick which whole plan becomes the report. **This is where the planning method is chosen** | §7.4 |
| **Optimize (multi-objective)** | Sweep control knobs on ONE engine and rank the settings on an objective you choose | §7.2 |
| **How it works (the rules)** | The plain-language rulebook — what each layer decides, what it may never do, the honest limits | — |

The app still *lands* on **Run forecast**; the ordering above is reading order,
not a change of entry point.

> **Retired:** the old **Tune (density knobs)** mode is gone. Its density
> distribution and severe-batch readout are now a checklist gate plus a
> per-candidate drill-in on the **Analyze** board, and the stocking frontier
> moved there with it (§12.1). The headless sweep remains: `python
> tools/tune_sweep.py`.
>
> **Also gone:** the sidebar no longer has a **Planning method** selector. The
> method is chosen on the **Compare & Choose** board, where you can see every
> engine graded side by side first; ▶ Run forecast then re-runs whichever plan
> you picked. The current pick is shown in the sidebar above the Run button.

At the top of the sidebar, above the Mode selector, a **Computer power** slider
(10–100%, default **40%**) sets how much of the machine the *heavy* runs may
use — the Optimize sweeps. The caption under it translates the percent into
processor cores ("up to N of M"). Raising it lets those runs go wider, but other
applications feel slower and Optimize sweeps use more memory while a run is
going (at 100% every core may be busy — an explicit opt-in). A plain controller
**▶ Run forecast** and **Tune** are sequential and unaffected by this setting.

**How much it actually buys depends on the shape of the work.** Optimize sweeps
scale nearly linearly — each variant is a whole forecast in its own process, so
twice the workers is roughly twice the throughput. Deep search is sequential by
construction (each knob depends on the previous best), so it parallelizes only
the candidate values within a knob — a smaller win (§7.2).

Lower in the sidebar (visible in every mode, but it governs **▶ Run forecast**)
the app shows **which planning method is currently picked**, and — when it is
the default — why. You change it on **Compare & Choose** (§7.4), not here. The
shipped default is **Controller — hybrid (L1-guided harvest)** — but note that
and it **does steer** — two independent routes turn its levers on and
either alone suffices: `config/control.yaml` ships `hybrid_purge_lever: true`
and `hybrid_production_lever: true`, **and** the `controller-hybrid` method
pins both `True` in its own overrides, so setting the config values back to
`false` would not make that arm inert. The **production** half is live; the
**purge** half is refused outright while `sixn_level_drains: false`, and the
guide logs that refusal to the ValidationLog. That was the arm measured
throughout this guide. ⚠ **On 2026-09-08 the operator set `sixn_level_drains:
true`, which lifts the refusal** — so `full` on the live tree would now steer
both halves, which has never been measured. See §4.5. (Until 2026-09-03 this section said
the arm was inert. That was wrong, and it predated the 2026-08-27 pins.)

### CLI
```
python -m forecast.run --workbook <input.xlsm> [--output <output.xlsm>] --config-dir config --scenario-dir scenario
```
If `--output` is omitted it defaults to the input path (in-place; the app always
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

> **"Typical" = the code (dataclass) default.** Your `config/control.yaml` is the
> operating value a run actually uses and may set a different number — it is the
> source of truth (e.g. the shipped config turns `harvest_grade_to_min`,
> `min_transfer_count` and **`hybrid_follow`** on, which the dataclass leaves off).
> Check `control.yaml` or **Configure → Control** in the app for what a given run
> will do. **This gap matters when you compare settings:** an A/B whose override
> happens to equal the shipped value runs the same plan twice and reports "no
> difference" — always read the config value before trusting a null result.

| Knob | Meaning | Typical |
|---|---|---|
| `horizon_weeks` | forecast length | 130 |
| `max_biomass_kg` | facility biomass cap — checked against **TOTAL** facility biomass (FW + OG + 6N purge), per-week overrides in FacilityLimits | (config default; overridable per week) |
| `max_feed_per_day_kg` | facility daily feed cap — checked against total **feeding** (SW + FW) feed/day; off-feed purge fish excluded (§4.1) | (config default) |
| `max_harvest_per_week` | **THE** weekly processing limit (fish) — a **constraint** the demand-driven harvest respects, never a level to plan up to: harvest = what biomass/density/floor/contracts need, capped here (the 6N drain holds a purge tank back one rotation rather than exceed it). The removed `harvest_target_per_week` knob is ignored with a console note if an old config still carries it | 55,000 |
| `harvest_relief_pct` | pressure-relief band used to **judge** a plan: derived absolute ceiling = `max_harvest_per_week × (1 + relief)` = 60,500. **No engine reads this knob** — the planner's own weekly ceiling is the limit itself; weeks land in the relief band when a whole 6N pair had to drain or an INV-5 force-empty overdrew (that overage is borrowed back from the next week). What the knob decides is how such weeks are SCORED: the Analyze checklist shows amber at 1–3 relief weeks and red beyond 3 — or on any week past the derived ceiling — telling you to ramp harvests up earlier instead. It also drives the manual-window over-ceiling lint. 0 = no band | 0.10 |
| `min_harvest_per_week` | weekly harvest floor | 30,000 |
| `plan_tank_feasibility` | **Plan within the tanks you actually have.** The precalc canvas already detects, weeks ahead, that OG tank demand exceeds placeable supply (`tank_supply` bottleneck) — and then plans past it: that list is handed to `_build_facility_assignment_plan` and only ever *appended* to, never read. The excess is not real need — each SW week's `tanks_needed_at_density_cap` is raised to that batch's own peak over the next 6 weeks so it can claim grow-out early, per batch, with nothing arbitrating the sum. On: the canvas hands those forward reservations back, deepest slack first and **never below a batch's need for that week**, until each week fits. **Measured on 3 PR closings and it does NOT win** — the tank-supply shortfall goes to zero, transfer legs fall on all three (626→585, 604→589, 550→516), ceiling breaches improve on 2 of 3, and worst grow-out density improves sharply on 2 of 3 (198.7→109.6 kg/m³ on 8/19) — but HOG slips on all three (−54.0, −4.2, −24.3 t), refused transfers **rise** (the forward claim was doing real work: hand it back and batches get boxed in later), and 1–3 weeks per horizon exceed the 15-move handling budget. Off by default; the **Controller — plan-feasible tanks** method in Compare pins it on. | false |
| `max_transfers_per_week` | weekly HANDLING BUDGET (transfer moves/week). A "move" = one distinct src→dst tank transfer with fish in it, exactly what a TransferPlan `Transfer` row shows (same-week duplicate legs are merged into one row; 0-fish float-residue legs are dropped; TranOG/Grade rows are not moves) — the engine's internal budget counts the **same unit**. Once a week's moves reach the budget, the deferrable quality passes (plan-diff *evening* top-ups, even-out, balancer, variable-quantity, remnant sweep) wait for a calmer week and the leveling resumes there; essential moves (6N rotation fills, arrival make-room/vacates, plan-diff *source drains* — tanks another batch takes over) are never blocked. A week can still end 1-2 moves OVER the budget, because the essential passes run LAST: the deferrable work spends the budget out to the cap and the essential moves that follow land on top. That is common, not exceptional — measured across **8 test months, 5 of them contain a week over 15 moves**, which is why the handling-budget gate is soft (§12). Two anticipatory layers that close that gap are BUILT but shipped **off** (`_ANTICIPATE_ARRIVAL_RESERVE` / `_ANTICIPATE_PACING_DEFER` in `placement.py` — engineering switches, not knobs, with no config key): a 4-arm x 3-PR x 2-knob-set ablation measured that they buy full budget compliance by starving the quality rebalancer, and pay for it out of the **harvest floor** — on the operator's own PR, weeks under `min_harvest_per_week` go 3 -> 5 and the shortfall more than doubles, and on one PR a 69,677-fish week lands past the 60,500 relief ceiling. Steady harvest outranks handling, so the plan may show a 16-17 move week instead. An overrun on the handling gate (WARN >12 / FAIL >15) means the week's quality work and essential work together exceeded the budget — most often a TranOG arrival week coinciding with a 6N rotation fill. 0 = off. **One scope limit worth knowing:** the *split* pass is NOT budget-gated (the `_rebalance_systems_realized` split pass, which takes its own `split_budget` and does not consult the weekly move budget) | 15 |
| `min_harvest_weight_g` | minimum weight a fish can be harvested at — a **tank is eligible when its MEAN reaches this**, which is why a tank averaging 3,438 g holds ~92,000 fish individually over 3,500 g and still cannot be harvested whole (§4.5, the graded peel exists for exactly that). **No dataclass default** — `ControlParams` declares it without one, so `config/control.yaml` is the only source. **Live value 3,300** since 2026-09-03: measured on the 8.31 PR it takes rest-of-2026 from 2,582 t to 2,715 t and the weekly-contract shortfall from 65,470 fish to 5,686, at the cost of five December-trough weeks shipping at 3.43–3.48 kg live (nothing below 3.4 kg). Beware 3,150 — a 600 t hole sits there | *(none — must be set)* |
| `min_tank_control` | force-empty floor (fish): a harvest/transfer leaving fewer than this empties the tank (INV-5) | 7,000 |
| `min_transfer_count` | min rebalancer transfer size (fish): the density/load balancer won't split a sub-group **smaller than this OUT** of a tank (the OUT-side mirror of `min_tank_control`). **0 = OFF.** Suppresses tiny partial moves — trades fewer transfers for more *marginal* density over-cap (the small moves were doing fine-grained relief); whole-tank consolidation moves are unaffected | 0 (off) |
| `min_grade_count` | min GRADED-TAIL size (fish) for the floor-fill peel — a **different rule** from the two 7,000s beside it. `min_tank_control` says how thin a tank may be **left**; `min_transfer_count` says how small a group is worth rigging a **pump** for; this says how small a ripe tail is worth running the **grader** for, on a week that is short of the harvest floor. They shared one number until 2026-09, which made the peel take **nothing** whenever the ripe fish stood as 3-7k tails spread over 8-12 tanks. **Blank = inherit `min_transfer_count`** (the historical behaviour, and the only setting measured to hold every hard gate at both 3,300 g and 3,500 g). A number is the explicit floor; **0 = no floor** (the tail must still be ≥ `min_fraction` = 10% of the tank and must leave a legal remnant). MEASURED 2026-09-03 on the 8.31 PR: at a **3,300 g** harvest gate the value is inert — 0 / 3,000 / 7,000 give the identical plan; at **3,500 g** it bites, and dropping it to 0 buys rest-of-2026 2,582 → 2,700 t with thin tank-weeks 9 → 0, but pushes a 2027-W39 grow-out tank to 115.6 kg/m³ and the weekly move peak to 17. Check the density and handling gates before keeping a low value | blank (inherit) |
| `harvest_grade_to_min` | **INACTIVE — this switch no longer controls anything.** The behaviour it used to gate (on a 6N purge week whose move-in falls below `min_harvest_per_week`, peel just enough of the over-weight tail from near-market tanks to reach the floor: big → 6N purge, the small tail stays in the source tank) now runs **unconditionally** — `placement._run_sixn_purge_week`: *"NOW UNCONDITIONAL (subsumes the old opt-in `harvest_grade_to_min`, which remains accepted but no longer gates this)"*. Leaving it off does **not** disable the behaviour; it produced empty harvest weeks, which breaks the steady-harvest contract. Flipping the box changes only a row in the app's run summary. Kept so older configs load | n/a (inert) |
| `default_hog_yield` | gross→HOG conversion (per-week overrides in FacilityLimits) | 0.81 |
| `scenario_name` | label for the run (reports + RunConfig) | Forecast |
| `facility_biomass_deviation_pct` | **FACILITY** setpoint band — the soft band below the (FW-inclusive) facility biomass/feed cap the harvest controller runs at; the one knob for how close to the *facility* cap to run (§4.3) | (config default) |
| `global_buffer_pct` | **SYSTEM-limits** buffer (R29) — a *separate* symmetric ±% applied to per-**system** feed/biomass caps (the rebalancer headroom + SystemLimitsAudit, `caps.py`); does **not** touch the facility setpoint above | (config default) |
| `handling_mortality_pct` | mortality charged on **every tank-to-tank deposit** (2026-08-21) — rebalances, consolidation, relief moves, 6N move-ins and the TranOG entry alike, not TranOG only as before. Accounting is **gross-out / net-in**: the source is drained by the full amount, the destination keeps `(1 - rate)`, and the difference is booked as mortality so the tank audit still balances. **Untunable** (a physical fact, not a lever) | `0.01` = 0.01%, i.e. a 0.0001 fraction |
| `grade_efficiency` | how cleanly a real grader separates sizes, 0-1. `1.0` = a perfect cut at the threshold; lower leaves the two graded populations OVERLAPPING near the cut line, so the big leg comes out lighter and the small leg heavier. Total biomass is unchanged at any setting — only how it splits. `0` means "off" and behaves as perfect. **Untunable** (describes your grader, not policy) | **0.85** (matches the VBA) |
| `chronic_pressure_frac` | an OG1/2 tank at or above this fraction of its density cap for `chronic_pressure_weeks` running is treated as STRUCTURALLY short of tanks — it gets another tank once instead of being shaved weekly. Keep it CLEAR of `density_relief_pct`: when the two were equal, relieved tanks landed exactly on the trigger and flipped on rounding noise | 0.92 (fraction, not %) |
| `chronic_pressure_weeks` | consecutive weeks above that level before a tank counts as chronic | 4 |
| `chronic_relief_pct` | a chronic tank is emptied down to this fraction of cap — deeper than the ordinary target, since trimming a tank that has sat at 91% back to 90% moves almost nothing | 0.80 |
| `chronic_max_frees_per_week` | cap on tanks the ANTICIPATORY pass may free per week. Consolidation and 6N harvest staging share one weekly transfer budget, so an unbounded sweep starves harvest and misses the sales floor. Tanks ALREADY over cap are urgent and ignore this. `0` stops the anticipatory freeing only — chronic tanks are still detected and still shed deeper | 1 |
| `density_relief_pct` | an over-cap OG1/2 tank is relieved down to this fraction of cap. NOT 1.0: relieving to exactly the cap leaves no margin and one week of growth puts it straight back over — which is what made the same tanks breach every week for months | 0.90 |
| `consolidation_fill_pct` | when a batch's grow-out tanks are consolidated to free one, the keepers fill to this fraction of cap. 0.80 not 0.90, for the same growth-margin reason | 0.80 |
| `global_assume_primed_6n` | Read by the **L1 tankless planner** (`forecast/global_planner_poc.py`), which is what the Controller's hybrid harvest guide runs (§4.5) — so it shapes the guide's first ~2 weeks. `false` (default) models the REAL 6N handover — L1 primes only from the fish actually in 6N at forecast start, so expect a genuine startup ramp over the first ~2 purge-hold weeks. `true` restores the older idealisation that assumed a steady-state-full 6N; it scores smoother but the tank picker cannot execute it. **Untunable** (a modelling assumption, not a lever) | false |
| `sixn_growth` | 6N runs as growout (vs purge) for the whole horizon | false |
| `sixn_production_start` | date 6N flips purge → production | e.g. 2028-01-01 |
| `sixn_transition_weeks` | empty/fallow window at the 6N transition (0 = none) | 0 |
| `sixn_level_drains` | **ON in the dataclass; the committed `config/control.yaml` still ships `false`, but the operator's live tree has run it `true` since 2026-09-08** — read the file, do not assume. 6N PURGE mode only. Caps how full a 6N purge pair may get (at `max_harvest_per_week`) so weekly fills don't **accumulate** into one pair across its rotation residency — the root cause of the 90–113k drain spikes that starve other pairs into sub-`min_harvest_per_week` troughs. Surplus stays in grow-out and becomes the move-in for the next thin pair, lifting its drain toward the floor so every week meets the harvest minimum (the steady-weekly-harvest contract). *Verified ON vs OFF:* 6N drain peak 110k→68k (−38%), CV 0.46→0.32, weeks-below-min 38→27, fish conserved. It is a **safety guard, not a lever** (`methods.py` UNTUNABLE_KNOBS): while it is off, `hybrid_guide.py` **refuses the hybrid's 6N purge lever** outright rather than steer around it — and the shipped config runs `hybrid_follow: full` with `hybrid_purge_lever: false`, so that steering is off twice over today. Set `true` to get the leveled behaviour, joining `rebalance_level` + `harvest_level_load` (which the shipped config *does* leave on); `false` is the old accumulate-then-dump behavior. No effect in 6N production mode. **Re-measured 2026-09-08 on the live 130-week plan (the operator turned it on):** it is the only mechanism that caps a 6N fill by the pair's remaining headroom (`target = min(target, max_h - existing)`, `placement.py:1917`). With it OFF the rotation fill topped up occupied tanks and whole-tank drains breached the weekly processing ceiling on three weeks — **88,155 / 72,309 / 72,279 fish** against a 55,000 ceiling. ON: **0 ceiling breaches, worst week 54,945, over-cap tank-weeks 95 → 37, +85 t.** That is why it is a guard and not a lever: off, the plan is not merely worse, it proposes weeks the plant cannot process | `true` dataclass default; `false` in the committed config; **`true` in the operator's live tree** |
| `starvation_period_days` | in-place purge length in 6N production mode | **7** (= one weekly step; clean single-cohort pipeline) |
| `tran_og_default_tanks` | min tanks a TranOG arrival gets | 2–3 |
| `density_target_pct` | per-tank density target as a fraction of cap | 0.85–0.99 |
| `rebalance_balance_budget` | multi-objective rebalancer moves/week (density+feed+biomass) | 30 |
| `rebalance_level` | **load-LEVELING (ON by default)** — cap-agnostic balancer that spreads load off the hottest system onto the COLDEST (vs concentrating); levels feed+biomass+density together. Cuts per-system feed/biomass over-cap ~90% at the cost of more marginal-density tank-weeks (see §7.3). Set `false` for the old density-only behavior | **true** |
| `rebalance_split_budget` | split over-dense batches into free tanks (moves/week) | 8 |
| `rebalance_varqty_budget` | precise-count shaving of over-cap systems (opt-in) | 0 |
| `cap_repair_budget` | **end-of-week cap repair (opt-in, OFF by default)** — every *other* rebalancing pass runs before the week's growth is applied, but the reports measure the state *after* it, so a system left just under its cap grows back over with nothing left to catch it. This pass runs last, on the state that is actually reported, and moves the least it can out of any system still over its feed/biomass cap into the coldest system that can legally take it. Big, clean per-system gain; the cost lands on the **harvest floor**, and it is high-variance across ProductionReports — it was adopted and then **withdrawn** within a day (see §7.3). Off is the shipped setting; if you try it, try **8** and judge it on your own PR's worst harvest week, not on the per-system numbers | 0 (off) |
| `harvest_setpoint_lookahead_weeks` | **VESTIGIAL** — superseded by the dual-limit setpoint (§4.1/§4.3); kept for config back-compat but **not read** by the engine. Use `facility_biomass_deviation_pct` to set how close to the cap to run | 0.75 (ignored) |
| `harvest_level_load` | **harvest smoother (ON by default)** — enforce `max_harvest_per_week` as a HARD ceiling + pre-harvest earlier so harvest is flat and biomass stays under cap. Paired with `rebalance_level`, which otherwise spikes harvest (see §4.3). Set `false` for old reactive behavior | **true** |
| `hybrid_follow` | **L1 HARVEST GUIDE — `full` in the shipped config, and STEERING.** Two independent routes turn it on and either alone is enough: `config/control.yaml` ships `hybrid_purge_lever: true` and `hybrid_production_lever: true`, **and** the `controller-hybrid` arm pins both `True` in its own `overrides` (`forecast/methods.py`) — so setting the config values back to `false` would still leave that arm steering. Runs the whole-horizon L1 harvest envelope (`forecast/global_planner_poc.py`, via `forecast/hybrid_guide.py`) first and feeds it to the controller as a per-week target band. The **production** half is live. The **purge** half is refused outright while `sixn_level_drains: false` (`hybrid_guide.py:194` — level drains are the guard against over-filling one 6N pair, and the guide may not remove it). ⚠ **That refusal lifted on 2026-09-08**, when the operator set `sixn_level_drains: true`. Every `full` measurement quoted in this row was taken with the purge half REFUSED — they describe the *production-lever-alone* arm. Setting `hybrid_follow: full` on the live tree now runs **both** levers for the first time, an arm this table does not describe. Measure it before trusting it. (The live tree currently runs `hybrid_follow: 'off'`; the committed config still says `full`. That disagreement is an open operator decision, not a defect.) Note the guide's ceiling half applies only on weeks L1 itself calls production weeks and while the facility is under its hard cap; elsewhere it degrades to floor-only. The ceiling half is the point: it tells the reactive controller to harvest **less** in fat weeks so those fish are still there for lean ones — the one thing it can never decide for itself (all its own levers are `max()`). *Measured, 6 real PRs:* **totally empty harvest weeks 6 → 0**, weeks below floor 22.5 → 9.0, worst week 0 → 16,148 fish; **cost** peak biomass 102.6 → 107.1% of cap, peak density 102 → 124. `off` = old reactive-only behaviour. `floor` is **not** a no-op (that claim was retracted 2026-08-12) but it is **dominated** — measured on the 7.29 PR it produces a genuinely different plan (worst week 23,754 vs `off`'s 20,526) yet **11** weeks below the contract floor, worse than `off`'s 9 and far worse than `full`'s 3. Applying only the guide's floor half raises the lean weeks it can reach while leaving the controller free to over-harvest the fat ones; the **ceiling** half is what actually banks fish for later. Use `full` | `full` (dataclass default `off`) |
| `hybrid_follow_band` | how tightly the controller tracks the guide (± fraction). Chosen by a 90-cell paired sweep as the most **stable** setting: holds 0–1 empty weeks under neutral perturbation where wider bands drift to 3–4 | **0.05** |
| `harvest_smooth_lookahead_weeks` | level-load window K — weeks of coming-due biomass to spread the pre-harvest over | 6 |
| `harvest_level_target` | flat fish/week floor when level-loading (unset/null = auto from realized growth) | null |
| `placement_method` | placement engine: `greedy` (default heuristic + rebalancer) or `lns` (opt-in LP-guided refinement of the realized layout — implemented and audit-gated, see §11; it correctly no-ops on a capacity-bound config, where there is no tank slack to relocate into) | `greedy` |

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

### 3.3a Facility capacity limits (`scenario/limits.yaml`, **Configure → Limits**)
A capacity is a fact about the facility — how much fish a system can hold, how
much feed its line can deliver. It changes rarely, so for a **system** you state
it **once** and it applies to every week of every horizon.

> ⚠️ **The two grids on this page resolve differently, and the difference has
> cost real tonnage.** A **system** cap falls back: per-week row → system+mode
> default → system default → no cap at all. A **facility** cap has no
> "stated once" tier — it is per-week row → **the `config/control.yaml`
> default**, full stop (`caps.resolve_facility_cap`). So a facility metric you
> steer week by week silently reverts to the Control default the moment your
> rows run out. On the 2026-08-31 PR the `biomass` and `feed_per_day` rows
> stopped at 2026-W53, so all of 2027 planned against the 3,800,000 kg /
> 34,000 kg/day **design** figures while the operator was entering the
> 3,650,000 / 27,500 **derate** — worth ~131 t of horizon production, silently.
> Since 2026-09-03 the run says so: a `PER-WEEK COVERAGE` line in the
> ValidationLog names any facility metric whose rows stop before the horizon
> ends (§5). It reports; it changes no cap.

**Configure → Limits** has three parts:

| Part | What it is | When you touch it |
|---|---|---|
| **System capacities** | One row per system, one column per metric: `biomass` (kg of standing fish) and `feed_per_day` (kg of feed per day). Blank = no *standing* capacity, which means no cap at all for that metric **unless** a mode row below supplies one — which is exactly why **OG6N's biomass is blank here**. The editor names any such cell under the grid. | Whenever a real capacity changes. This is the normal edit — one cell. |
| **Mode-specific capacities** | A capacity that depends on what the system is being *used for*. Today only **OG6N** has one: it holds more (700,000 kg) while it is the depuration station than it does (400,000 kg) once its 3 mains become grow-out. | Rarely. |
| **Per-week system exceptions** (collapsed, advanced) | A cap for ONE unusual week — a shutdown, a trial, maintenance. Blank is the normal state. | Almost never. |

The **Facility limits** grid below those three carries the per-week facility
numbers. It holds **six** metrics, not the harvest ones alone: `biomass` and
`feed_per_day` (the whole-facility totals, which are *also* where a temporary
derate against the Control design figure is expressed), `max_harvest_per_week`,
`min_harvest_per_week`, `hog_yield`, and one entry that is not a cap at all:

> **`sgr_correction_og` — the per-week OG growth factor.** The weeks you know
> the site will not achieve the modelled growth. `1.0` (or blank) is the model;
> `0.90` means "we expect 90% of it that week". It **layers**: effective SGR =
> growth curve x the batch's own `sgr_correction` x this week factor — it does
> not replace either, so a batch calibrated to 0.8 in a 0.9 week grows at 0.72
> of curve.
>
> **Feed follows it.** Feed is `biomass x SGR/100 x FCR`, and the factor is
> applied at the single source for the growth rate, so a 90% week eats 90% and
> grows 90% and **FCR is unchanged** (measured: Bio_FCR x0.998). That is the
> operator's choice of the two readings — "they ate less, so they grew less",
> rather than a normal ration against impaired growth, which would instead have
> worsened FCR by ~11%.
>
> **Seawater only.** Freshwater has its own `fw_correction`, and this is an
> OG-tank input. Measured on a 3-week 0.90 test: seawater batches x0.899,
> freshwater batches x1.0000 exactly, and the Nutra (small/FW) feeds flat while
> only the Optimax grow-out feeds dropped.
>
> **What moves.** Growth, weights, biomass, density, feed and feed-type mix, and
> facility cap pressure (biomass over-cap x0.900). **Counts never move** — SGR
> neither kills nor creates fish. **Harvest does not move in the week you set**:
> those fish were already in 6N purge, filled ~3 weeks earlier. It lands
> downstream as smaller fish (whole-horizon harvest -0.4% on the 3-week test).
>
> **It shifts the PLAN, not only the numbers.** Smaller fish change rebalancing
> decisions — transfers moved x0.83 in the test weeks — so tank usage and
> harvest timing shift downstream too. Compare a before/after run rather than
> assuming only the weeks you set are affected.
>
> `0` is a real answer (no growth that week), not "unset". A negative value is
> dropped rather than applied. The run log prints the weeks it read and flags a
> value that looks like a percentage typed as `90` instead of `0.90`.

**Which weeks are which mode is derived**, not typed: a week is in `purge` mode
while its start date is before Control's `sixn_production_start`, and
`production` from that date on (and every week is production if *Run 6N as
grow-out* is on). Move that date and the capacity step follows it — the two
cannot disagree.

**Resolution order**, highest first:

```
per-week exception  >  system + mode default  >  system default  >  no cap at all
```

The last rung is real: a capacity nobody set stays unset. Code that needs a hard
bound **raises, naming the missing input**, rather than substituting an invented
ceiling: `caps.require_system_cap` is that contract, and the TranOG cohort
sizing in `placement.py` follows it. No capacity number lives anywhere in the
code.

> **Why this replaced the per-week grid (2026-08-14).** `limits.yaml` used to
> hold one row per (week, system, metric) — 3,120 near-identical rows — so
> changing one capacity meant editing 130 cells, and the actual value was
> invisible. It also silently expired: the rows covered a fixed span of absolute
> weeks, so a ProductionReport that moved the horizon left the tail of the run
> with **no cap at all**. On the operator's own 2026-08-12 PR that was six weeks
> × twelve systems. A default has no week axis and cannot run out.

### 3.4 The biology tables (`config/biology.yaml`, **Configure → Biology models**)
These are the curves every batch grows and eats along, edited as four grids:

| Grid | Keyed on | Drives |
|---|---|---|
| **Growth** | fish size (g) | SGR %/day in freshwater and seawater, plus the FCR curve for each model |
| **Mortality** | weeks since input | weekly mortality % |
| **Feed types** | max size (g) | which feed a fish of that size is on |
| **Culling** | days since input | scheduled cull % |

Each grid is a **lookup curve read by size or age**, so the rows must run smallest
to largest. You don't have to maintain that by hand — add a row wherever it's
convenient and the app sorts it into place when you save, which is why a row can
appear to jump after **💾 Save Biology**. That's the sort working, not an error.

> **Why it matters.** Values between two rows are interpolated, and anything past
> the last row holds that row's value. So an out-of-order row used to silently
> flatten the whole curve beyond it — one stray 50 g row could make every
> market-weight fish grow at the 50 g rate. The sort is now enforced wherever
> tables enter (this editor, the Excel template import, hand-edited YAML), so this
> class of silent error is gone. Values are never changed — only row order. One
> limit worth knowing if you hand-edit the YAML: a value column shorter than its
> key column is left alone rather than reordered, since there is no safe pairing —
> so keep each curve's columns the same length. The app editor always writes them
> that way.

Between edits, remember the app is the source of truth: a save writes
`config/biology.yaml` and every later run reads it. Per-batch multipliers
(`fcr_model`, `sgr_correction`, `fw_correction` in §3.3) scale these shared curves
for one batch without touching them.

### 3.5 Manual override window (optional starting-state editor)

Sometimes the PR-hydrated starting state isn't quite the starting point you want
to forecast from — you want to script a few operational moves first (relocate a
batch, harvest a tank early, push fish into 6N depuration, or do a specific
FW→OG transfer), and only **then** let the planner take over. That's what the
**manual override window** is for.

**Where:** Run mode, the **"🗓 Starting setup — manual override window
(optional)"** expander above the results (appears once a PR is uploaded). Leave
it empty to let the planner do everything (the default). What you enter is saved
to `scenario/manual_events.yaml`.

**You drive it by clicking the facility, not by filling a table.** The editor
shows a **projected facility grid** — columns are weeks, rows are tanks, and
**each cell is labelled by the batch it holds** so you can read it directly. A
**"Colour cells by"** toggle switches what the cell colour means: **Fill
(density)** — how full each tank is versus its own density cap (grey empty,
green roomy, amber near cap, red over) — or **Batch** — a distinct colour per
batch, so you can see which tanks hold the same cohort and how a batch moves
across the weeks. Rows tagged **⛔6N** are depuration.

Each cell shows **batch · average weight · density**. A **"Show tank state at"**
toggle picks *when* in the week that snapshot is taken. **Week open** (the
default) is the **start-of-week** state — before that week's growth *and* before
your scripted events run — i.e. exactly what's in the tank at the moment you
click to act on it. **Week close** is the **end-of-week** state — after growth
and after your events run — so you can see what actually **holds fish and what's
empty at the end of each week** (a tank you harvest or move shows empty in that
week). The rule of thumb: **script in Week open** (weights and occupancy are the
values you're acting on), **inspect end-of-week room in Week close**.

**Transfers light up both ends.** In the week a move fires, the tank that holds
the fish *in the current view* is shown **solid with a trailing arrow** (**⇢**
the fish are leaving, **⇠** they arrived), and the counterpart tank — empty in
that view — shows a faint **ghost arrow** naming where the fish went or came
from. So an **OG→OG** relocation or an **OG→6N** send reads at a glance in one
column, instead of having to compare the open and close views. (Biomass is only
counted at the solid end, so density/fill scans stay honest. A move the engine
*refuses* — e.g. the 1 kg-lock on intra-OG1/2 transfers — moves no fish, so no
arrows are drawn and the refusal shows in the timeline. An **FW→OG intake** has
no source tank, so it only ever appears at its destination.)

To act: **click the cell**
for the tank and week you want — a **single click picks both** the tank (its row)
and the week (its column) — and a panel opens *in context* showing what's actually
in that tank (batch, fish, weight, density) and offering **Harvest / Graded →
6N / Move / Send to 6N** with real tank pickers — no tank numbers to
memorise, no codes to type.
The grid **re-draws as you script**, so you watch each operation ripple forward
over the weeks. A **"Weeks to project / act in"** slider sets how far ahead to
look. Below the map: a **📊 System rollup** toggle (see below), an **🐟 FW→OG
intake** picker (freshwater cohorts aren't tanks yet), a plain-English
**timeline** of everything you've scripted (with delete), and a **Save window**
button.

**⚠ Most out of bounds — recommended actions.** At the top of the right-hand
panel, a recommendations box reads the current projection against the caps and
lists what's **most out of bounds**, ranked worst-first — per-system **feed**,
per-system **biomass**, per-tank **density**, and **facility** biomass (the same
caps the System rollup shades against). Each line names the breach (value / cap /
%) and a **relief action**: *harvest* the heaviest tank in the offending system
when it's at harvest weight, else *move* it to the grow-out system with the most
feed headroom (never into 6N depuration), and *split* a tank that's over its own
density cap. A recurring breach is collapsed to its **worst week** so the list
shows distinct problems. Press **▶** on any line to jump straight to that tank
and act. Because it reads the live projection, the breaches **shrink as you
script** — so it doubles as your "am I done yet?" check (empty = everything is
within limits across the window).

**🤖 Co-pilot — let the forecast propose the next week (v1a).** A toggle at the
bottom of the window turns on a human-in-the-loop planner. You script the start
and trend by hand; when you want help, press **"Recommend week N+1"** and the
co-pilot runs the controller forward from your window (**respect mode** — your
scripted transfers are never changed) and proposes the *next* week's operations:
**harvest + 6N staging from the validated controller**, pre-ticked — these are
the load-bearing, contract/cap moves. Approve the ticked moves and they're
appended as week N+1's operations, extending your window by a week; run it again
for the week after, and so on. Each run costs one forward controller run over
your window plus a short look-ahead. The engine (`forecast/copilot.py`) is
UI-free by design, so this loop is portable to a future desktop build.

> **It does not propose OG↔OG transfers.** That leg was produced by the Global-LP
> optimiser and by nothing else, so with that engine removed the co-pilot's
> `transfer_options` list is always empty: it offers harvest and 6N staging
> recommendations only. Relocations are yours to script by clicking the grid.

Both co-pilot buttons write `scenario/manual_events.yaml` — the same file the
forecast reads — so they follow the same **reject-at-entry** rule as *Save window*:
while any operation in your window shows ❌, Recommend and Approve are disabled
until you fix it. Recommendations are also tied to the window they were computed
from; edit or delete an operation (or upload a different PR) and the proposals
clear rather than letting you approve moves planned against a facility state that
no longer exists. If a save ever fails — `scenario/` is OneDrive-synced, so a sync
lock can win the race — you get an explicit error saying the operations are in your
window but **not** on disk, and *💾 Save window* retries it.

**📊 System rollup — spotting capacity pressure.** The per-tank grid shows
*density* per tank, but a system can be fine on every individual tank and still
be **over its feed budget** — feed usually binds before biomass here. The
rollup toggle opens two colour-coded tables (systems as rows, weeks as columns,
with a facility **TOTAL** row): **biomass** (tonnes) and **feed** (kg/day, 6N
depuration eats 0). Each cell is coloured by the fraction of that **system's**
capacity it uses — biomass vs Σ(volume × density-cap), feed vs Σ(per-tank feed
cap) — green roomy, amber near cap, red over. A neutral **FW (freshwater)** row
adds the standing freshwater cohorts (biomass = count × projected FW weight;
feed = the FW-stage projected daily feed): those fish are fed in the freshwater
area and don't draw on any OG system's capacity, so the row is **shown uncoloured
and folded only into the facility TOTAL** — giving a whole-site biomass and
feed-demand figure, not OG-cap pressure. Both tables follow the same **Week open
/ Week close** choice as the grid (the toggle labels it, e.g. *open biomass*), so
they reflect the same moment you're reading above. Use it to catch a
system you're about to push over its feed cap before you commit the move. The old flat table still lives under **⚙
Advanced — raw event grid** for bulk edits or unequal per-tank splits; both write
the same YAML.

**How it works:** you script operations **week by week** for weeks 1..N. In each
scripted week the forecast **executes only your events** (the planner makes no
decisions that week) and then runs **full biology** — growth, mortality, and
feed — exactly as the normal engine would. The window length N is implicit: it
runs through the **last week that has an event**, and the planner takes over the
week after. Everything you script is **recorded in the reports** (TransferPlan /
HarvestPlan / feed) and **reconciled by the conservation audits** (§6), so the
window is fully traceable — it is not a silent pre-run mutation.

> **Starting-state only, not pins.** These events adjust week-0 reality and then
> the planner builds forward on top. They are **not** future commitments the
> planner must honour later — once the window ends, the closed-loop controller
> has full control again.

> **6N is held in depuration during the window.** While 6N is in **purge mode**
> (before `sixn_production_start` — see §4.2), every occupied 6N tank is held
> **frozen** through the window: **no growth, no feed** (mortality still applies),
> shown as `STARVE`/⛔6N. This matches the engine's depuration rules — the normal
> planner harvests 6N out on its rotation within a week or two, but the window
> runs no rotation, so without the hold those fish would wrongly grow like
> grow-out for the whole window. The hold is **date-gated per week**, so if
> `sixn_production_start` ever fell inside your window, 6N would grow from that
> week on. In **6N production mode** (`sixn_growth` on, or on/after the start
> date) 6N is *not* held — it grows normally.
>
> The hold is a **manual-window concern only.** It does **not** carry downstream:
> at the handoff, each held 6N tank is restored to its normal stage so the auto
> planner starts from a clean condition and runs **its own** 6N rotation. Only the
> **depurated (un-grown) weight** carries forward — that *is* the starting state
> the manual inputs produce, so the auto pipeline correctly builds on lighter,
> purged 6N fish.

**The five event types** (every operation you script — by click or in the raw
grid — is one of these):

- **`og_transfer`** — move/split OG fish from `From tank` into one or more
  `To tanks` (same batch). Pure relocation; the destination inherits the source
  weight. Count conserved exactly.
- **`harvest`** — directly harvest `Count` fish from `From tank` (blank = the
  whole tank), recorded as a real harvest in that week.
- **`graded_harvest`** — a **size-sorted grade**: take the **biggest `Count`
  fish** from `From tank` and move them to the **first `To tank`** (the pickup),
  keeping the smaller remainder growing (in the source, or an optional
  **second `To tank`**). The pickup type + **Mode decide WHEN they are
  harvested**:
  - **6N pickup, default** (Mode blank or `stage` — the panel's "Purge first"
    choice; also what a co-pilot-approved planner Grade leg uses) — the graded
    fish **depurate** in the 6N tank (frozen off-feed, harvested *later* —
    script a later `harvest` of that tank, or the planner takes it after the
    ~2-week hold). **They do NOT appear in that week's HarvestPlan** — the
    ValidationLog's `MANUAL EVENT OK` line says so explicitly, and if that
    leaves the week with no harvest at all, a `MANUAL WINDOW` warning flags
    the zero-harvest week (steady-harvest contract). That lint now fires on
    **every** window week with no scripted harvest — including a window opened
    purely with `--advance-weeks`, where nothing is scripted at all and *every*
    week is a zero-harvest week (until 2026-08 that was the one case it could
    never fire on).
  - **Mode `harvest`** (the panel's "Harvest them this week" choice), or an
    **OG pickup** — the graded fish are **harvested in the scripted week**:
    the pickup is drained to processing that same week and the harvest appears
    in that week's HarvestPlan (a 6N pickup is just the staging route and ends
    the week empty).
  The panel shows a live read-out of the **cut weight** (the average weight of
  the biggest `Count` you're moving). Either way the split is exact — the
  biggest `Count` leave at their (higher) mean, the rest stay at their (lower)
  mean — so **count + biomass conserve** and it reconciles in the
  **TankContinuityAudit** (0 drift) + **InputConservationAudit** like every
  other event. Every scripted event writes a **`MANUAL EVENT OK`** line into
  the ValidationLog saying exactly what it did — and one that cannot run writes
  a **`MANUAL EVENT REFUSED`** line with the reason (never a silent no-op).
- **`og_to_6n`** — move OG fish from `From tank` into a **6N depuration tank**.
  The pickers offer all six 6N tanks — mains (61, 63, 65) **and** sisters (67, 69,
  71) — each labelled with its **current batch + density** (or *empty*). Note the
  sisters exist to hold a *second* batch in a pair for a **mixed** same-week
  harvest; for single-batch-per-tank biomass fidelity, keep one batch per pair
  (the batch column lets you spot a same-pair main holding a different batch). The
  destination is frozen **off-feed** (no growth, no feed) for depuration.
- **`fw_to_og`** — a manual FW→OG transfer (TranOG): bring a **freshwater
  cohort** into seawater. Because a cohort isn't a tank yet, it has its own
  **🐟 FW→OG intake** panel below the grid (not a grid click). You pick:
  - **Freshwater cohort** — only cohorts still in freshwater during the window
    appear (projected from the FW trajectory);
  - **Week to bring it in**. Once picked, a small **Planned vs. This intake**
    table compares the cohort's originally-scheduled transfer (the PR's
    `tran_og_date` / `tran_og_avg_wt_g`) with your choice — **transfer week** and
    **average weight** side by side — and a one-line read-out flags the deltas
    (e.g. *12 wk earlier · 0.29 kg lighter than planned*), so you can see at a
    glance that pulling a cohort in early means placing much lighter fish;
  - **Target fish entering seawater** — `Count`. The engine applies the same
    logic as the automatic pipeline: **handling mortality**, then a
    **reconcile-to-target bottom cull** (it removes the *smallest* fish down to
    your target, which also lifts the survivors' average weight). The cull is
    surfaced in the **ValidationLog** and reconciled in the
    **InputConservationAudit** FW mass-balance, so no fish go unaccounted.
  - **Where the size classes go** — on entry the cohort is graded into a
    **bigger** and a **smaller** class (a median split, driven by the cohort's
    size CV — you don't set the ratio). A **live preview** shows both grades
    (*bigger N ≈ X kg · smaller N ≈ Y kg*), and **two pickers** — **"Tank(s) for
    the BIGGER grade"** and **"Tank(s) for the SMALLER grade"** — let you send
    each grade to its own empty OG tank(s) (a tank can't be in both; each grade's
    count splits evenly across its tanks). *Add* is blocked until every grade
    that has fish has a home. In the **⚙ Advanced** raw grid (which has no grade
    pickers), an `fw_to_og` with untagged `To tanks` falls back to the legacy
    rule — bigger grade → first half of the tanks, smaller → the rest.

**Advanced — raw grid columns.** The **⚙ Advanced** table is one row per
operation, for bulk edits or unequal per-tank splits the click flow doesn't
cover. Edit it and press **Apply to window** to push the rows into the visual
editor + timeline.

| Column | Meaning |
|---|---|
| **Week** | 1-based forecast week the event fires in (start of that week) |
| **Type** | one of the five event types above |
| **Batch** | the FW batch id — **only** for `fw_to_og` |
| **From tank** | source tank id — for `og_transfer` / `harvest` / `graded_harvest` / `og_to_6n` |
| **To tanks** | destination tank id(s), comma-separated; use `tank:count` to send an explicit count to a tank, or a bare `tank` to split the row's Count evenly across the bare tanks. For `graded_harvest` the **first** tank is the graded-fish pickup (a 6N pickup parks them to purge by default; Mode `harvest` drains it that week) and an optional **second** is the retention tank for the smaller fish |
| **Count / target** | `harvest` = fish to harvest (blank = whole tank); `graded_harvest` = the number of **biggest** fish to grade out; `og_transfer` / `og_to_6n` = split across To tanks; `fw_to_og` = the **target** count entering seawater (the engine culls down to it) |
| **Mode** | `graded_harvest` only: `stage` (the 6N-pickup default) = the graded fish are **parked in the 6N pickup to purge** (frozen off-feed, harvested later); `harvest` = they are **harvested in the scripted week** |
| **Notes** | free text |

**Reject-at-entry validation.** As you edit, each event is dry-run against your
uploaded PR using the **same** projection the real run uses. Infeasible events
are listed (e.g. *"batch B45 is not in freshwater at week 1"*, *"target exceeds
available FW"*, *"dest not empty"*) and the **Save** button is disabled until
they're fixed. A valid window shows *"All N event(s) feasible against the
uploaded PR."*

**Rules / limits:**
- `fw_to_og` destinations must be **empty OG tanks**, and the batch must still be
  **in freshwater** at the event's week (you can't FW→OG a batch that's already
  crossed to seawater).
- The window must be **shorter than the forecast horizon** — a window as long as
  the whole horizon is rejected (the planner needs weeks left to plan).
- Conservation is enforced end-to-end: every event is counted in the audits, and
  a mis-stated `fw_to_og` cull would now **breach** the FW mass-balance gate.
  *(6N depuration mortality is no longer approximated: the continuity audit takes it
  from the recorded realized biology, and since 2026-08-18 its mass is booked too;
  see §6.)*

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
  restocked from the oldest mature production fish (Thu-fill / Fri-harvest).
- **One batch, one tank.** A purge cohort fills a SINGLE 6N tank however dense it gets —
  purge has no density or biomass cap (§3.3a, §7.1, §7.3), so nothing forces a split.
  The sister (67/69/71) is used ONLY when a SECOND, DIFFERENT batch needs harvest the
  same week and would otherwise be mixed into an occupied tank; mixing destroys per-batch
  count fidelity at harvest. Spending a sister on one batch's overflow burns the slot
  that separation needs — that was a real defect, fixed 2026-08-20. The harvest limit
  applies to the pair's **combined** drain.
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
- **Smaller** (e.g. 0.01 = ±1% ≈ ±38 t on the 3.8M cap) → runs **tighter** to the cap
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

> ⚠️ **Widening this band to shrink the biomass peak costs you empty harvest weeks.**
> Measured, not assumed. Aiming 1.5% or 2.5% lower does pull the peak down (107.1% →
> 104.8% of cap), but a 90-cell paired sweep shows it puts empty weeks back — the
> lower-peak setting is worse in 9 of 10 non-tied comparisons. The same is true of
> smoothing the L1 guide. **The peak is the reserve that fills the lean weeks**, so
> trading it away trades away the steady-harvest contract. If the peak is genuinely
> hurting you, the lever is upstream — how many fish you stock and when — not here.

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

### 4.5 The L1 harvest guide (the hybrid — **ON by default**): `hybrid_follow`

This is the *intended* answer to *"never an empty harvest week"*, and **it is
live.** `hybrid_follow: full` is on in `config/control.yaml`, and both levers that let
it steer ship `true` (`hybrid_purge_lever`, `hybrid_production_lever`) — as well as
being pinned `True` by the `controller-hybrid` method's own overrides, which win over
`control.yaml` for that arm. The **production** path therefore steers on every
non-purge week. The **purge** path does not: the guide refuses it outright while
`sixn_level_drains: false`, because level drains are the guard against over-filling one
6N pair, and the refusal is written to the ValidationLog. To enable the purge path too,
set `sixn_level_drains: true` — **the live tree has done exactly that since 2026-09-08**, so
on that tree the purge path is no longer refused and `hybrid_follow: full` would steer both
halves for the first time. Note the guide's *ceiling* half — the half that actually
banks fish — applies only on weeks L1 itself calls production weeks and while the
facility is under its hard cap; elsewhere it degrades to floor-only.

**Why the reactive controller can't fix this itself.** Every lever it owns is a
`max()` — it can always harvest *more*, never less. So when a fat week arrives it
takes what it can, and the fish that would have carried a lean week three weeks
later are already gone. It cannot see the lean week coming because it only ever
looks at *now*.

**What the hybrid adds.** Before planning, it runs the **L1 stage** —
a whole-horizon, tankless harvest envelope — and feeds that curve back into the
validated controller as a per-week target **band**. The floor half tells the
controller to harvest *at least* this much; the ceiling half, which is the part
that matters, tells it to harvest *at most* this much in the fat weeks, leaving
those fish in the water for the lean ones. The controller still does all the
actual planning; the guide only shapes how much it takes. It is a **request, not
a command** — the controller's own cap-shedding always wins, and the ceiling is
never allowed below `min_harvest_per_week`.

**Measured across 6 real July-2026 PRs** (2026-08-03, after the zero-harvest-week
metric fix) — **with `hybrid_purge_lever` and `hybrid_production_lever` both ON.
`config/control.yaml` ships both `true`**, and the guide is acted on in exactly
three places (`forecast/placement.py`), every one of which tests a lever. Because the
purge lever is refused while `sixn_level_drains: false`, the configuration you actually
run is the **production-lever-alone** arm: the applicable measurement is *weeks under
the contract floor 20 → 14*, not the both-levers *20 → 16*. The figures predate the
2026-08-20/21 changes
(handling mortality on every deposit, `grade_efficiency` 0.85, Thursday purge move-in,
6N one-batch-one-tank), all of which move the weekly harvest series, and have not been
reproduced since. The `peak tank density` row was measured before **R8** stopped
capping purge and harvest-prep (`STARVE`) tanks, so it counts tanks that no longer
have a cap:

| | plain controller | **hybrid (default)** |
|---|---|---|
| **totally empty harvest weeks** | **6** | **0** |
| weeks below the contract floor | 22.5 | **9.0** |
| worst week (fish) | **0** | **16,148** |
| peak biomass (% of cap) | 102.6% | 107.1% |
| peak tank density | 102 | 124 |

**The cost is real and you should know it.** Holding fish back for a lean week
means they are still in the water, so the hybrid runs harder against the biomass
cap and the density line. That is not a bug to tune away — it *is* the mechanism.
Every knob that shrinks the peak puts empty weeks back (see the warning in §4.3).

**`hybrid_follow_band` (default 0.05)** is how tightly the controller must track
the guide. It was chosen over the alternatives by a 90-cell paired sweep as the
most **stable** setting: it holds 0–1 empty weeks under perturbations that should
not matter, where wider bands and lower deviation targets drift to 3–4.

Set **`hybrid_follow: off`** to return to the old reactive-only behaviour —
accepting known empty weeks in exchange for staying further under the caps. The
`controller`, `controller-lns` and `controller-feasible` entries on the Compare
board are pinned `off` so you can always see them side by side.

> **`controller-feasible`** is the third Controller arm. Its single variable is
> `plan_tank_feasibility` — the canvas plans within the tanks that exist instead
> of past its own `tank_supply` shortfall. It is in the lobby because it isolates
> a real planning question on **your** PR, not because it is the better plan: on
> the three closings measured it loses tonnage on all three and pushes 1–3 weeks
> over the handling budget, while removing the tank shortfall entirely and
> cutting worst grow-out density on two of three. Judge it on your own workbook.

> **If you compare methods yourself, pin the knob explicitly.** The base config
> now ships the hybrid **on**, so a comparison arm that simply *omits* an override
> inherits it. An A/B whose "off" arm is actually on runs the same plan twice and
> reports "no difference" — which is exactly how a real feature was once wrongly
> recorded as inert on this project.

---

## 5. Output reports — where to read what

| Sheet | What it is | Read it for |
|---|---|---|
| **HarvestReport** | one row per harvest event (Year/Month/Week/Date/Tank/Batch/Count/Gross/HOG/Avg wt) | the full harvest event log |
| **HarvestPlan** | single-table harvest plan (Week/Batch/Tank/Count/Gross/HOG…) | the actionable harvest plan |
| **HarvestPlan Report** | per-year blocks, per-batch Units/AvWt/Biomass by month + **bottom monthly TOTAL row** | **monthly sales planning** (HOG tonnes landed per month) |
| **YearlySummary** | facility-wide per-year: harvest count/HOG t/gross t/avg wt, feed t, peak+mean biomass, utilization | **year-over-year trends** |
| **TransferTemplate** | (A) the canonical batch journey through seawater; (B) per-batch summary: SW entry week + weeks-from-start, entry weight/count/density, peak tank footprint, peak density (×cap) + Density_Status flag, harvest window + weight | **the general plan at a glance** — which batches enter when, their footprint, density risk, and harvest timing |
| **Daily Harvest Schedule** | each week's harvest — **all tanks combined** — split evenly Mon–Fri (blended avg weights), with a per-week **Total** row and a blank line between weeks; Tank/Batch list every contributor | daily ops |
| **WeeklyReport / MonthlyReport** | per-(batch, week/month) open/close ledger (count, weight, biomass, **Avg_Density**, SGR, feed, FCR, mortality, harvest, transfers, checks) | detailed batch accounting |
| **FeedForecastWeekly / Monthly** | feed by feed-type × period matrix | feed ordering |
| **Advisory** | per-week capacity table: biomass/feed vs caps + excess + OK/REDUCE | capacity headroom + over-cap weeks |
| **FacilityMap** | tank × week grid (cell = "Batch# AvgWt/Density"); **below it**: per-system × week **feed (kg/day)** and **biomass (kg)** blocks, each with a FACILITY total row | occupancy at a glance + per-system load vs caps |
| **BatchLocations** | per-(week, batch, tank) occupancy | raw realized placement |
| **ValidationLog** | numbered warnings (# / Category / Detail), incl. FW-calibration + bottleneck (annotated with resolution), **`INFO - Per-week coverage`** (below) and the **realized-plan** categories below | diagnostics — **read the `(realized plan)` categories first** |
| **InputConservationAudit** | per batch: placed/dropped, harvested, standing, **FW reconciliation** (planned vs realized seawater entry) + **closed FW mass-balance** (`first_FW_count` vs `realized_TranOG + FW_mort + FW_cull`; §6 #6) | conservation + FW calibration gaps |
| **TankContinuityAudit** | per-(tank, week) balance + **facility conservation summary** | 0-drift proof |
| **ReconciliationReport / SystemLimitsAudit** | per-batch open/close balance (count reconciles **exactly** via recorded realized biology; biomass within tolerance) / per-system realized biomass + feed vs cap, flagged `BIOMASS_OVER` / `FEED_OVER` | deeper audits — *TankContinuityAudit is the authoritative 0-drift biomass check* |
| **RealizationReport** | the **intent** check, for all three event families. **Transfers**: events emitted / applied in full / in part / refused whole, fish planned vs moved, **share of planned movement realized**, a per-week table, and **STUCK RELATIONSHIPS** (one row per batch+source tank+reason, so a refusal repeated many times reads as one fact with a first/last week). **Harvest**: decided vs taken, split into taken-as-decided / INV-5 force-emptied (took *more*) / short / refused. **TranOG**: fish planned to enter vs entered, and **fish that never entered the facility at all**. **Grading**: Grade (size split) and GradedHarvest (the peel), applied vs refused whole | "did the plan actually happen?" — see the note below |
| **WeeklyReport / MonthlyReport** | the per-(period, batch) production ledger, plus a **TOTAL row per period** and a real **AutoFilter** already applied — filter these | reading one batch, or slicing by batch/period |
| **WeeklyReport Grouped / MonthlyReport Grouped** | the SAME rows with a **blank line between periods**, for reading and printing. **Do not filter these** — a blank row ends Excel's contiguous range, so a filter would silently cover only the first period | reading a period end-to-end |
| **Diagnostics** | FW-calibration: per batch, the target vs projected pre-cull avg weight at TranOG, the residual, and a back-solved `Suggested_FW_Correction` | tuning `fw_correction` (§7 step 2) |
| **RunConfig** | the exact config + scenario embedded in the output | reproducibility |

> **`PR FW WEIGHT DERIVED`** (console WARN + ValidationLog). A freshwater batch
> the ProductionReport gives a COUNT but no BIOMASS used to seed the projection
> at 0 g — and because FW growth is **multiplicative**, a 0 g seed stays 0 g for
> ever. It never reaches `min_harvest_weight_g`, so it is never selected for
> harvest, so it never leaves: on the 2026-08-31 PR, **B56 (563,234 fish across
> 46 hatchery units, every one 0.00 kg)** held tanks 14 and 21 from 2027-W32 to
> **2029-W05** — 162 zero-weight rows and two grow-out tanks removed from the
> facility for 80 weeks. Nothing crashed and no gate fired: conservation is
> satisfied by fish that never move.
>
> **Since `842ade4` the planner derives the weight instead** (`biology.py`),
> from the batch's OWN lifecycle rather than from thin air: hatch weight at its
> **Transfer SF date** (hatchery → start-feed, the real biological start), else
> `input_date + HATCHERY_DAYS` where `HATCHERY_DAYS = 81` — a fallback only, and
> the scenario's own measured median (min 71, max 88; the operator's rule of
> thumb was 90). Pre-hatch weeks carry `stage = "EGG"`; at EGG→FW the weight
> becomes `FW_START_WEIGHT_G` (0.15 g) and the normal FW curve takes over —
> exactly how a batch not yet in the PR is already projected. *Effect on the
> 2026-08-31 PR:* **B56 harvests 323,712 (was 0)**, total **+554.8 t**,
> zero-weight rows 162 → 0, weeks over the 15-move handling budget 2 → 0.
>
> ⚠ **The warning still fires, and you should still act on it.** These weights
> are **MODELLED, not measured** — the derivation is a floor under a data gap,
> not a substitute for the data. **Fix it in the PR**: record a weight for those
> units. Related: a zero-weight bottom cull used to remove nobody and report
> success (fixed `d3e3d43`, now culls proportionally).

> **Reading RealizationReport.** Every other check in the workbook verifies
> either *conservation* (nothing is lost) or an *outcome* (floors, empty weeks,
> caps, handling budget). A move the planner emitted and the engine refused
> passes all of them: it is perfectly conservative, trips no gate, does not
> consume the handling budget (which counts APPLIED pairs), and TransferPlan
> deliberately omits it as "not the actionable plan". This sheet is the only
> place that question is asked.
>
> **A high refusal count is not automatically a defect.** Measured on two PR
> closings, only ~36–37% of planned movement is realized, and essentially every
> refusal is `source_holds_other_batch` — the planner's record and the realized
> facility disagree about where a batch lives. Sourcing the plan-diff from
> realized occupancy instead removes *all* of them and makes the plan **worse**:
> transfer legs 626 → 1,166, weeks over the 15-move handling budget 0 → 13,
> worst grow-out density 163 → 281 kg/m³, with no tonnage gained. The refusal is
> throttling an emitter that plans roughly twice the movement the facility can
> execute. Read the sheet as a measure of that appetite, and treat a *rising*
> refusal count or a *new* stuck relationship as the signal — not the level.
>
> **Harvest and TranOG read clean**, and that is a real result rather than an
> absent one: on both PR closings tested, every harvest was taken exactly as
> decided (110/110 and 101/101, no force-empties, no shortfalls, no refusals)
> and 100.0% of planned TranOG entry was realized. Every refusal path in both
> is covered by a test that forces it, so a zero here means "did not happen",
> not "cannot be reported". **Grading reads clean too** (79/79 and 88/88 Grade
> events applied; 7/7 and 35/35 peels), likewise with every refusal path
> test-forced. The realization gap is confined to transfers.
>
> Every summary label is self-identifying (`transfers refused whole`,
> `harvests refused whole`, `grades refused whole`, `peels refused whole`) —
> read as key/value, a shared label would silently return the wrong section's
> number.
>
> ⚠ **One known gap this sheet exists to cover.** `write_transfer_plan_output`
> filters refused *transfers* out of TransferPlan, but emits a GradedHarvest's
> pickup and retention rows **without checking whether it applied** — so a
> refused peel would print on TransferPlan as a real move. It is 0 on both PRs
> tested, so nothing is currently misreported; if `peels refused whole` is ever
> non-zero, treat those TransferPlan rows as suspect.

> The `ProductionReport` sheet stays the **historical** input month only — the
> *forecast* is in the sheets above (same as the reference workbook). Skipped vs the
> reference: AccumulatedReport, AccumulatedOutput, MonthlyTargets, RunComparison.
>
> **`Avg_Density (kg/m³)` in the two ledgers** is the batch's **average** density
> that period — its total biomass divided by the total water it occupied — and the
> TOTAL row recomputes it the same way (never a sum, never a max of the rows above).
> **It is not a peak.** A batch normally sits in several tanks, so a roomy average
> can hide one over-cap tank: judge crowding by the density lines in ValidationLog,
> the SystemLimitsAudit, or Batch Plan's own `Peak_Density (×cap)` / `Density_Status`,
> which are unchanged and still peak-based. **Blank** means the batch held no tank
> that period (e.g. a freshwater week carried by the biology projection).
> *History:* a literal `0` on every row before 2026-08; the worst-tank peak until
> 2026-09-07, when the operator asked for the average (**reports only — no engine
> decision reads this column**, the planner's own density tests were untouched).
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
> move-in's **3-day** pre-transfer feed (Mon->Thu, the actual move day) — so they
> reconcile. The per-day cap-check sheets
> (**Advisory**, **SystemLimitsAudit**) instead show the steady realized feed *rate*
> vs cap, so they intentionally exclude the move-in (a total-accounting item, not a
> per-day rate).

> **`… (realized plan)` categories — judge the plan, not a pass inside it.**
#### `INFO - Per-week coverage (weeks on a Control default)`

Shipped 2026-09-03. One line per **facility** metric whose per-week rows in
`scenario/limits.yaml` **stop before the horizon ends**, e.g.

> `PER-WEEK COVERAGE - biomass: rows cover 2026-W36..2026-W53 (18 of 85 horizon
> week(s)); 67 after 2026-W53 take the Control default 3,800,000. An absent row
> means "use the default", so this matters only if that default is not what you
> intend for those weeks - check it rather than assume it.`

Read it as a **disclosure, not a defect** — falling back is correct behaviour
and an absent row genuinely does mean "use the default". What it removes is the
silence: on the 2026-08-31 PR the `biomass` and `feed_per_day` rows stopped at
2026-W53, so 2027 planned against the *design* figures (3,800,000 kg /
34,000 kg/day) while the operator was entering the *derate* (3,650,000 /
27,500) — a difference of ~131 t of horizon production that nothing announced.

Three things keep it worth reading rather than noise:

- it is **silent for a metric with no rows at all**, because there the Control
  default *is* your deliberate answer;
- it reports the **shape** of the gap — weeks *after* your last row mean entry
  stopped, weeks *before* your first usually mean the rows start mid-horizon on
  purpose;
- `sgr_correction_og` is **excluded**: it has no Control default to fall back
  to, so coverage is not a question for it.

It is **detection only**. `caps.resolve_facility_cap` is unchanged and still
falls back exactly as before; the note reaches `invariant_warnings`, a
report-layer list the planner never reads. Verified on the 8.31 PR: with and
without the check, 85 harvest weeks compared, **0 differ, 0.0 fish**.

> Most ValidationLog entries are raised *mid-plan* by whichever pass first
> noticed a problem. That is useful for tracing, but it is **not** the answer to
> "which weeks are short?" — the passes that run afterwards (make-room,
> level-loading, the 6N fallback ladder) both fix weeks that were flagged and
> break weeks that were not. Measured on the 8.13 PR: the realized plan was
> under the harvest floor in 29 weeks, the log named 3, and one of those 3 was
> comfortably fine in the plan that actually shipped.
>
> Three categories are therefore measured **last, on the events the run actually
> emitted**, against the **per-week resolved** caps (a check against the flat
> Control default silently passes every week you raised in `scenario/limits.yaml`):
>
> * `WARNING - Harvest floor (realized plan)` — every week under the floor in
>   force *that week*. Misses under 0.5% of the floor are tagged
>   `[rounding-scale]` so a handful of "72 fish" lines cannot train you to
>   ignore the category. Operator-scripted manual-window weeks are **excluded**
>   with a note saying so: those weeks run only your script (the `MANUAL WINDOW`
>   entries police that), and their harvests are stitched in separately.
> * `WARNING - Harvest ceiling (realized plan)` — weeks over the weekly
>   processing limit.
> * `WARNING - Handling budget (realized plan)` — weeks over
>   `max_transfers_per_week`, counted in the same unit the planner clamps to
>   (distinct applied source→dest tank pairs, not sheet rows).
>
> The older `WARNING - Harvest Scheduler` entries remain, but they now say
> plainly that they are a **demand-stage** observation and point here for the
> final answer.

> **A mid-month PR completes its own month.** The ProductionReport's closing
> date is the day *before* `forecast_start`, so when it closes mid-month the
> month is split across two sources: the days the PR already reported, and the
> forecast that starts the next day. **MonthlyReport** and **HarvestPlan
> Report** merge the two, so the month reads as the month rather than as the
> tail of it. Measured on the 8.13 PR: August showed 70,444 of its 134,289
> harvested fish — 48% of the real tonnage — on the two sheets sales planning
> reads.
>
> The merge fires **only when the PR closes mid-month**. A PR closing on a
> month's last day needs nothing: the forecast then starts on the 1st and
> already covers the whole month (operator rule, 2026-08-18). Month-ends and
> leap years are pinned in `tests/test_pr_month_merge.py`.
>
> Two consequences worth knowing:
> * The merged month **opens where the PR opened** (day 1), not where the
>   forecast picked up — otherwise the row shows a full month of flows against
>   half a month's opening.
> * Its `Count_Check` carries the PR's own **"Deviation count in period"**, the
>   site system's reconciliation figure. That is not a fish movement and has no
>   column here, so it surfaces in the residual rather than being hidden.
>
> **Reporting layer only.** The audits never see the merge: they exist to prove
> the *forecast* conserves, and feeding actuals into them would break their
> identities and mask real defects. Nothing else in the tool reads these two
> sheets, so the merge cannot reach a gate, a score, or the accuracy grader.
>
> The PR's "in period" columns are **month-to-date** (1st → closing date) —
> confirmed by the operator, 2026-08-18, and independently consistent with the
> data: 370,225 kg of feed at a ~29,000 kg/day facility rate is 12.8 days,
> matching Aug 1–13. That is what makes the merge a clean addition rather than
> an overlap; a *since-last-report* period would instead straddle two months.

> **`Count_Check` in the ledgers is not always zero, and that is expected.**
> The column carries the ledger's own residual, and two real movements land
> outside the Mort/Cull columns: a manual-window week whose 6N purge tanks are
> frozen (STARVE — the mortality *rate* is 0 by design while the count still
> falls), and the week a batch enters seawater (the FW cull at TranOG is booked
> to the freshwater phase). Neither is a lost fish; `Bio_Check` is 0 by
> construction, and conservation is proven separately by
> **InputConservationAudit** and **ReconciliationReport**. The sheet states this
> in its own header so nobody has to remember it.

> **The workbook is formatted on the way out.** Headers are frozen and
> filterable, numbers carry thousands separators and sensible precision, tabs
> are colour-coded by role (plan / reports / audits / inputs), and cells the
> **engine itself** flagged — `Bio_flag`, `Feed_flag`, `Flag`, `Advisory`,
> excess and drift columns — turn red automatically. Density is shaded
> relatively rather than cut at a fixed line, because per-tank caps differ by
> tier and a flat threshold would be wrong for the smolt tanks. This is a
> presentation pass only: it runs after every writer, touches no value, and if
> it ever fails you get a plain workbook and a note, never a failed run.

### Knowing what the app is doing
Run mode, the Optimize tab, and every result show a collapsible **"Active
configuration"** panel — plain-language label / value / *effect* for the settings
that actually shape a run (feed leveling, harvest smoother, TranOG tanks, setpoint,
density target, rebalancer budget, placement engine, caps). Run mode shows *what this
run will do*, a result shows *the config it used* (incl. optimizer overrides), and
Optimize shows *the base the search tunes on top of* — so you can always see what's
selected and what it does.

**While a run is going**, the status box narrates the engine's own progress live —
loading, hydration, caps, the harvest scheduler, FW calibration, the placement walk,
the audits, save — with the newest line as the heading and the full sequence
underneath. A controller run emits ~200 such lines. One thing it can't tell you:
the placement walk itself is silent (it prints only when it finishes, so a long run
rests on its last line for a while). That is not a hang — check CPU in Task
Manager if in doubt.

### The app tabs
Tab contents are computed **once per run** and reused, so clicking between tabs,
dragging the Per-Batch period slider, or working in the manual-window editor above
the results no longer rebuilds the pivots, tables and charts each time. A new run
(or picking a different plan on the Compare board) rebuilds everything. Two
consequences worth knowing: your Per-Batch batch/period selections now survive
interactions elsewhere in the app but reset when you load a different run, and if
the run's temporary output file has been cleaned up (after a reboot, say) the
Overview's realized-feed chart is simply omitted rather than erroring the page —
re-run to get it back.

- **Overview** — advisory issues + tank-occupancy heatmap + per-system biomass + **realized** per-system feed (read from `SystemLimitsAudit`, with the per-system feed-cap line). This is the *fed plan after harvest/FIFO* — **not** the `BiologyProjection` per-batch feed, which is the unharvested projection (fish growing along the curve, ignoring harvest/caps) and runs far higher (10k+ vs a realized ~3–4k). If a feed line looks like it spikes to 5–10× the cap, you're looking at projection feed, not the plan.
- **Per-Batch** — per-batch weight/biomass/density/losses over a period slider
- **Period Summary** — facility biomass, weekly harvest, active batches, density
- **Harvest** — totals, per-week stacked harvest, avg harvest weight, **monthly HOG rollup (sales planning)**, and a **Daily harvest schedule** table — each week's harvest with **all tanks combined**, split evenly across its five operating days (Mon–Fri), with a shaded per-week **Total** row and a blank line between weeks (the same as the *Daily Harvest Schedule* Excel sheet)
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
7. **No near-empty mid-horizon harvest week (steady-harvest contract)** — every week
   past the startup handoff harvests **> 25 % of the `min_harvest_per_week` floor**; a
   crater (a cohort-timing gap the controller fails to smooth) fails the gate
   (`test_no_harvest_craters`). PR-specific, so on most inputs it is a forward-lock.

> **Two biomass-accounting defects, fixed 2026-08-18.** This section used to
> describe the first of them as a benign approximation to be left alone. It was
> measured properly and it was neither benign nor an approximation — together
> the two accounted for ~40 t of unexplained biomass on the 8.13 PR, which is
> about one week of harvest.
>
> * **Purge mortality had no mass.** Off-feed depuration (STARVE) stops
>   *feeding*, not biology: growth halts, mortality does not. The count side
>   always booked those deaths — which is why continuity balanced to the fish —
>   but `Mort_kg` was written as 0, so the dead fish's biomass stayed inside
>   `Expected_Close`. Measured: 425 tank-weeks, **every one negative**,
>   −20,358 kg against an implied mortality mass of 19,892 kg. A systematic hole
>   in the sheet that exists to *prove* conservation is a hole a real loss could
>   hide in. Now booked at the same open-weight basis the recorded-biology branch
>   uses.
> * **Graded splits did not conserve mass.** The graded path chooses its pickup
>   **count** first (capped to exactly the floor shortfall, so it peels the least
>   it can) and then took both conditional means from the harvest **weight**
>   threshold. Those describe different partitions: when the cap bites, the
>   heavy fish left behind sit in the retention leg while it is still priced at
>   the full lower-tail mean, and the tank loses mass that never went anywhere.
>   24 splits swung −6,493 to +1,546 kg. `biology.count_split_means` now derives
>   both means from the fraction actually moved, which conserves by construction
>   and is *identical* to the old result whenever the cap does not bite — so an
>   uncapped split is unchanged. This was a **model** defect, not a reporting
>   one: the understated weight became the tank's state and grew from there.
>   Correcting it raised total harvest ~55 t on the 8.13 PR (the same fish at
>   their true weight, plus growth on mass no longer being destroyed) and raised
>   the reported density and biomass-cap pressure accordingly — those fish were
>   always in the tanks.
>
> After both: the facility conservation summary reads **count signed-sum 0**,
> **biomass signed-sum −474 kg** (abs 841 kg) across ~3,200 tank-weeks, the
> ReconciliationReport biomass residual is **exactly 0**, and **no** row carries
> a `BIO_DRIFT` flag. Before, the same run showed −38,776 kg and 5 flags.

### The negative-control policy — every alarm ships with a proof it can fire

A check exists to **detect** defects, never to coerce results — and a check that
cannot physically fire is itself a defect. Twice this project a gate could not
report the failure it existed to catch (the zero-week counter dropped empty
weeks by construction; the over-production alarm was structurally blind to the
audit's own headline). The standing fix is `tests/test_negative_controls.py`:
**every detection surface** — the analysis gate registry, the workbook audits
above, the realized-plan audit (§5's `(realized plan)` categories), the
compare-harness verdicts, the manual-window lints, the tournament hard-gate
predicates, the board cache-staleness checks — ships with a minimal
synthetic input containing exactly the defect it exists to catch, asserting the
alarm **fires**, plus a clean-input control asserting it stays **quiet**. A
meta-guard enumerates the gate registry and fails CI when a gate is registered
without an alarm proof. When a control does not trip its check, that is a
finding: fix the *check* so it can detect, never the control.

### The one standing limitation (be honest about it)
**"0 drift" proves *bookkeeping* consistency, not *model* correctness.** The audits
derive "expected" from the same growth/FCR/FW curves the engine used, so a
biologically *wrong but internally consistent* model reconciles to itself. Catching
that requires **independent biological validation** (e.g. checking realized SGR/FCR
against field data), not a code change. The FW reconciliation (#4) surfaces when a
batch's seawater entry diverges from plan — your first signal that a `fw_correction`
may need re-calibrating — but it can't tell you the model's *absolute* truth.

**This is what §13 measures.** Every invariant above grades the tool against
itself. The *independent* check is your own ProductionReport: last month's
forecast made a prediction for a date, and this month's PR says what actually
happened on it. Grading one against the other is the only measurement here that
can call the growth model wrong — see **§13 Accuracy (forecast vs actuals)**.

---

## 7. Calibration & tuning workflow

1. **Run** with your PR + scenario.
2. **Check `InputConservationAudit`**: 0 dropped, 0 over-produced, and review the
   **FW_Flag** column — any "FW UNDER/OVER plan" batch reached seawater off its
   planned `tran_og_count`. Adjust that batch's `fw_correction` (the downloaded
   workbook's **Diagnostics** sheet back-solves a suggested value — for
   **both** incoming batches *and* in-flight ones already in FW at the forecast
   start, where it solves the correction on the remaining growth to TranOG) if you
   want it to hit your plan.
   - **Or let the tool do it: the `auto_calibrate_fw` control toggle.** When on
     (Configure → *Auto-calibrate FW to transfer target*; default **off**), the run
     replaces every FW batch's `fw_correction` with that back-solved value **before
     projecting**, so each batch lands its pre-cull avg weight exactly on its
     `tran_og_avg_wt_g` target on the transfer date and the Diagnostics sheet's residuals go
     to ~0. Applies to incoming **and** in-flight FW batches. The solved value is
     **clamped** to `[auto_calibrate_fw_min, auto_calibrate_fw_max]` (default
     0.5–1.5) so the model can't silently assume absurd growth; a batch that would
     need more is capped and **flagged in the ValidationLog**. ⚠ This makes the
     forecast *assume* the growth needed to hit target — a **planning assumption, not
     a guarantee** the fish grow that fast (a correction > 1 means faster than the
     nominal SGR curve). Leave it **off** to see the honest residuals and calibrate
     by hand.
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
`max_density_kg_m3` cap. **Fish preparing for harvest are excluded** from this
peak (and from the app's density alert and the optimizer's `density_overshoot`) —
judged on STAGE (`STARVE`, rule **R8** in `forecast/tiers.py`), NOT on which system
the tank is in. That covers both 6N depuration tanks in purge mode and, after the
6N production switch, in-place starvation in an ordinary grow-out tank:
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

- **In the app (recommended):** **Mode → Analyze**, then the *📊 Density quality*
  expander at the bottom of the board. It shows the peak-density distribution
  per candidate, the severe-batch list, and the gate's verdict. (This replaced
  the retired Tune mode — §12.1.) Reading rule: the gate counts batches at
  **≥1.3× cap**; the drill-in table lists everything from **1.2×** so you can
  see what is approaching severe. Nothing here modifies your config — it runs each
  variant in a temp copy — but the results panel has a **💾 Save these tuning knobs
  to my config** button if you want to keep the winner.
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
| `density_overshoot` | per-tank density over-cap fraction, **off-feed harvest-prep (STARVE) tanks excluded wherever they sit** (compliance, §7.3) | no breach |
| `system_peak` | the single **hottest** (system, week) load — biomass *or* feed, as a fraction of cap | **no hot spots** |
| `crowded_biomass_fraction` | share of grow-out biomass reared above the welfare line (§7.4) | gentler rearing / product quality |

**Emphasis presets:** *Walk the line* (default — flatness + no-breach dominate),
*Flatten biomass*, *Minimize feed*, *Minimize handling*, *Respect caps* (minimize all
over-cap excursions — see §7.3), **_Minimize loads_** (keep every system's biomass+feed
as LOW and EVEN as possible — minimizes `system_peak` + all CVs + feed + handling, and
DROPS the press-to-cap reward; the "no hot spots" objective), *Product quality* (trade
packing for gentler rearing — weights `crowded_biomass_fraction`, see §7.4), *Balanced*;
plus advanced custom weights. In the app, **changing the emphasis re-scores instantly** without
re-running the sweep — explore the trade-offs live.

**Search method (Quick/Full grid vs Deep search).** The grids *enumerate* hand-picked
configs and mostly vary one knob at a time, so they miss **combinations** (e.g. a
`tran_og=2` + `deviation=0.005` + `K=12` combo has to be found by hand). **Deep search**
is a greedy **coordinate descent**: from the current config it tunes one knob at a time
toward the best score under the chosen emphasis, looping until nothing improves — so it
**finds combinations the grid can't** (~15–30 runs, deterministic, conservation-gated).
The emphasis *guides* the deep search, so pick it first. A fourth option, **Grid + Deep
(best of both)**, runs the full grid and then a deep search seeded from the grid's
winner, returning the best of either — it is what 🤖 Auto-optimize uses, and the
slowest/most thorough choice. All of them return the same ranked variants, Pareto map,
and apply/verify panel.

**It runs in parallel.** Each grid variant is an independent full forecast, so the sweep
runs them across a process pool (as many at once as the sidebar's **Computer power**
allows — the CLI defaults to 8) — typically **3–5× faster** than
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

**⚠ What the score does NOT contain: the harvest floor.** Every component above
is either a cap-breach, a variability measure, or a cost. **None of them
measures the contract floor** — the closest, `harvest_var`, is a coefficient of
variation, which is blind to *which side* of the mean a week sits on. Measured
on the 7.29 PR over a 40-variant search: the worst harvest week ranged
7,855–27,462 fish while `corr(worst week, harvest_var)` was **+0.04** and
`corr(worst week, score)` was **−0.03**. Worse, `biomass_util_gap` actively
*rewards* running with no headroom, and headroom is exactly what fills a lean
week — so the objective is mildly **anti**-floor. Read the **contract-floor
gate** (§ Analyze, gate 3) beside the score; do not read the score alone. The
tuned tournament now enforces a no-regression rank on the floor so this cannot
be promoted silently, but a hand-run Optimize sweep is still ranked by score
alone.

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

The knobs shown, applied and saved are the winner's **actual** knob set. That matters
for **Deep search**, which improves one knob at a time and names each candidate after
the single knob it just changed — so the same name recurs across rounds carrying
different accumulated settings. The recommendation carries its own knobs rather than
being looked up by name, which is what makes a multi-knob winner save in full. (Before
this, "save the winning knobs" could persist an early partial set.)

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

> **OG6N in PURGE mode is exempt from BOTH caps (operator, 2026-08-20 —
> this SUPERSEDES the 2026-08-14 ruling that reinstated the biomass cap).**
> What bounds a depuration tank is the HARVEST SCHEDULE, not a kg figure: the
> fish are off feed, not growing, and gone within the ~2-week rotation, so the
> water-quality reasoning behind both caps does not apply to them. The 600 t
> figure previously quoted here was, in the operator's words, a placeholder
> "just to add a number", never a real limit. In PRODUCTION mode 6N is an
> ordinary system and every cap applies again. The tonnage is still WRITTEN to
> SystemLimitsAudit so it stays auditable — exempt from FLAGGING is not the
> same as hidden. Superseded text, kept for history: 674 t still
> exceeds the 600 t the operator states 6N holds in purge. Its **feed** cap
> stays exempt for a physical reason rather than a policy one: purge fish are
> `STARVE` and eat nothing, so a feed-rate check on that system can only ever
> report 0.
- **`density_overshoot`** — fraction of (tank, week) cells over the per-tank
  `max_density_kg_m3` cap (every off-feed harvest-prep tank excluded — judged on
  stage `STARVE`, not on system, so it covers 6N depuration *and* in-place
  starvation in an ordinary grow-out tank after the 6N production switch — §7.1).

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

**End-of-week cap repair (`cap_repair_budget`, OFF by default).** Leveling and every
other rebalancing pass run *before* the week's growth is applied, but the
SystemLimitsAudit — and every per-system number you read — measures the state *after*
it. That gap is most of what is left. Traced on the 7.29 PR: of the 104 non-6N
over-cap (system, week) cells, **79 were already back under cap when the balancer
finished** (0.94–0.99 of cap), with no later event touching the system. A week of
growth alone carried them over, which is why the balancer's 0.90 destination margin
was not enough — 0.90 × 1.11 ≈ 1.0. `cap_repair_budget` adds one final pass, on the
state that is actually reported: while a system is over its cap it moves the minimum
into the coldest system that can legally take it, and stops. It only tops up tanks a
batch **already holds** (never opens a new one), so the free-tank pool the harvest
controller needs is untouched, and it spends only what is left of
`max_transfers_per_week` — being last, that arithmetic is exact.

*Measured across 8 starting states* (the six real July-2026 PRs, with 7.24 and 7.29
run both with and without the operator's manual window), at budget 8, against five
neutral control nudges that establish what this engine's chaos alone can move:

| | baseline (8 states) | `cap_repair_budget: 8` | neutral-nudge range |
|---|---|---|---|
| over-cap system-weeks | 1,223 | **724** (−41%, better on 8/8) | −10 … +37 |
| ...of which entry tier (OG1/2) | 757 | **342** (−55%, 8/8) | −23 … +36 |
| `system_overshoot` | 0.7845 | **0.4644** (8/8) | −0.006 … +0.024 |
| `system_peak` (hottest system-week) | 11.198 | **10.005** (8/8) | −0.46 … −0.08 |
| zero-harvest weeks | 0 | **0** | 0 |
| weeks over the 60,500 relief ceiling | 0 | **0** | 0 … +2 ⚠ |
| weeks below the 30,000 floor | 38 | **32** (no state worse) | −3 … +2 |
| cumulative floor shortfall (fish) | 193,485 | 208,953 (worse) | −19,950 … +44,400 |
| worst harvest week, summed | 134,172 | 123,454 (worse) | −26,694 … +10,848 |
| transfers per fish | 6.158 | 6.232 (worse) | −0.105 … +0.022 |
| dropped fish / topology breaches | 0 / 0 | **0 / 0** | 0 / 0 |

**Read the last column before the second.** On `system_overshoot` the effect is
**13× the largest excursion a neutral nudge produces**, every state improves, and
even the *smallest* per-state improvement (0.024) beats the *largest* per-state
nudge (0.016) — that is a real effect, not chaos. On `system_peak` it is 2.6× the
largest nudge and again 8/8, but note that every nudge lowered that total too (the
range is entirely negative), so read the peak as corroborating, not independent.
The harvest-floor and handling costs sit **inside** the nudge band — this engine
moves them that much on its own when you change `min_tank_control` by one fish — so
they are honest costs to watch, not measured regressions. The ⚠ is the same point in
reverse: a neutral nudge *did* breach the relief ceiling on a clean state (an
81,541-fish week on 7.24 without the window), so a single ceiling breach anywhere is
not by itself evidence about a knob.

It is **off by default** because it is not a strict improvement: the cumulative floor
shortfall and transfers per fish move the wrong way, and on two states the worst
harvest week drops sharply. Budget 15 measured **identical** to 8 — the leftover
handling budget binds first — so 8 is the budget to test with.

> **Adopted, then withdrawn one day later (2026-08-14 → 2026-08-15).** The
> per-system gain above held, so the knob was turned on at 8. Re-measured on the
> operator's next ProductionReport it had to come straight back off, and the two
> PRs disagree completely about the same setting:
>
> | | worst harvest week | worst per-batch density | relief-ceiling breaches |
> |---|---|---|---|
> | **7.29 PR**, repair 8 | 19,630 → **23,235** (better) | 116.8 → **102.2** (better) | none added |
> | **8.13 PR**, repair 8 | 23,259 → **4,578** (collapsed) | — | **+1** ⚠ |
>
> Read that as the honest shape of this knob: the **system-balance effect is
> robust** (8 of 8 states, 13× the neutral-nudge band), the **harvest-floor
> effect is not** — it is large, both-signed, and decided by the starting state.
> A 4,578-fish week is a contract breach, and a ceiling breach is a week the
> plant cannot take, so neither is a cost you can average away.
>
> Practical rule: leave it off. If per-system utilization is genuinely your
> binding problem, turn it on for **one PR at a time**, and accept it only after
> Analyze's checklist shows the **contract floor** and **processing limit +
> relief** gates no worse than with it off. Never adopt it on the cap-compliance
> numbers alone — that is exactly the reading that got it adopted and reverted.

### 7.4 Compare & Choose — run every method, pick the plan (`Mode → Compare & Choose`)

Instead of committing to one engine up front, this mode runs the planning methods on
your PR, **grades them on several lenses, and lets *you* pick which plan becomes the
report**. Each method's plan is internally consistent (0-drift, tank continuity), so
you choose a *whole* plan — never a splice.

- **What runs.** All four Controller-family arms, every time: **Controller**
  (~30s), **Controller — hybrid** (~40s, the default planning method — §4.5),
  **Controller + LNS** (~30s) and **Controller — plan-feasible tanks** (§4.5).
  A complete board is a couple of minutes. On a capacity-bound config (the
  facility full at the peak week) **Controller + LNS usually matches plain
  Controller** — LNS only diverges when there is tank slack to relocate into
  (§11).
- **Interrupting is safe.** Each method is saved the instant it finishes. If you click
  something mid-compare (which aborts the run — Streamlit restarts the script on any
  widget interaction), the board still shows the legs that completed and names the ones
  that didn't. Click **▶ Run all methods & compare** again and it reuses the finished
  work, running only what's missing. Use **↻ Re-run all from scratch** to discard
  everything and start over. You rarely need it: if you change config, edit the
  scenario (including manual events) or upload a different PR, the affected legs are
  treated as **not run** — a warning names them and they come OFF the board until the
  next ▶ re-runs exactly those. A plan computed under different inputs is never shown
  next to current ones (that once let a stale board blame every stock method for an
  empty harvest week the current scenario had already scripted away). Changing only
  the harvest **targets or prices** re-judges the existing board instantly — those are
  scoring overlays, not run inputs, so nothing re-runs.
- **Provenance on every result.** Every method card (and every Analyze candidate,
  and the header of any run on the report tabs) carries a small caption saying where
  that result came from: **●&nbsp;fresh run** (the engine ran in *this* browser
  session) vs **⟲&nbsp;cached run of `<date time>`** (replayed from the result cache
  — an earlier session, a reload), the grading-rules version it was judged under
  (`graded metrics-v2-…`), and an 8-character **inputs** signature prefix — the same
  prefix on two cards means they saw the same PR + config + scenario. When a cached
  engine run is kept but its *verdict* is recomputed after a grading-rules update, the
  caption says so explicitly: *"⟲ cached run of 2026-08-10 11:08 · re-graded under
  current rules 13:42"*. Nothing on a board is ever silently a replay — if a caption
  doesn't say what you expect, re-run before trusting it. (Legs cached before this
  label existed show "time not recorded" until they next re-run.)
- **Reading the progress bar.** It advances when a method *finishes*, not while one is
  running: the engine call blocks the app, so nothing can animate during it. The text
  tells you which method is in flight, its typical duration, and the wall-clock time it
  started. Each method also has its own status line that narrates the engine's stages
  as they happen.
- **Grading lenses** (each card shows the winning method + its value): fewest fish
  moves, steadiest harvest, most balanced *across* systems, most even *within* systems,
  tightest density, **best welfare / product quality**, smallest tank footprint, fastest
  run. A lens only ranks methods that pass the *Conserves* **and** *Fully placed* gates
  (a plan that dropped batches would win quality lenses on the fish it never reared —
  unplaced fish can't be crowded or moved); the other two badges are advisory flags,
  not filters.
- **Product-quality (welfare) view of density.** Beyond the pass/fail-vs-cap numbers,
  every method now reports the density your fish were actually **reared at** — the
  biomass-weighted average density over grow-out — and the share of biomass that spent
  time **above the welfare line** — a soft density threshold you set in **Configure →
  Control** (`density_welfare_threshold_kg_m3`, default 80, below the 95 hard cap). Lower =
  gentler rearing = better welfare / flesh quality, at a cost in throughput (fewer fish /
  more tanks). It shows on the board (a lens + the per-method line), on every **Run**
  (the *Reared density* KPI), and as an **Optimize** objective — pick the *"Product
  quality"* emphasis preset to have the optimizer trade packing for gentler rearing.
- **Stocking-for-quality frontier** (`Mode → Analyze`, at the bottom of the Analyze board). On a
  tank-full facility the density knobs can't lower density — the real quality lever is
  stocking **fewer fish**. This sweeps a stocking cut across your **future** batches
  (fish already in the facility are fixed) and plots the trade: fewer fish rear gentler
  (lower experienced density) but yield less harvest. Each point runs the full forecast.
  Example read: *10% fewer future fish → reared ~1 kg/m³ gentler for ~700 t less
  harvest* — you choose where on the curve to sit.
- **Hard-gate badges** show on *every* method so a soft win can't hide a hard breach:
  **Conserves** · **Fully placed** · **No empty week** · **Under cap**. A plan that
  fails *Conserves* or *Fully placed* can't win a lens; the rest are flags you weigh.
  **Use this plan** loads that method's plan into the report tabs + download.
- **New comparison metrics** (also rows in the `RunComparison` sheet): **tank footprint**
  (occupied grow-out tanks/week), **tanks per batch** (distinct tanks a batch passes
  through FW→OG — a count view of transfers), **between-system** biomass/feed spread
  (CV + range *across* systems — placement balance), and **within-system** biomass/feed
  variation (CV + range *across the tanks* of a system). Per-tank feed is the system's
  reported feed apportioned by biomass × a size-declining rate shape.

> **Known controller behavior the "No empty week" gate surfaces.** On some PRs (e.g. a
> particular July arrival schedule) the reactive controller leaves a few **near-empty
> mid-horizon harvest weeks** — a cohort-timing gap it doesn't smooth *even though the
> supply exists*: an L1-envelope diagnostic (2026-07-08) shows the tankless planner
> holds 30–47k at the exact weeks the controller drops to a few hundred. It is a pacing
> gap, not a shortage, and it is **PR-specific** (the reference PR does not exhibit it).
> Until an anti-crater fix lands, if steady weekly harvest is critical for such a PR,
> the lever that actually measured is the L1 guide's steering flags:
> `config/control.yaml` ships `hybrid_purge_lever` and
> `hybrid_production_lever` **true**, and the
> `controller-hybrid` arm pins both `True` itself, so that arm steers; with the purge
> lever refused by `sixn_level_drains: false`, what runs is the production lever
> alone — worth weeks under the contract floor 20 → 14 on the real
> workbook with **zero** empty harvest weeks (re-measured 2026-08-21). The
> regression `test_no_harvest_craters` guards this invariant.

---

## 8. Key facts to remember
- The PR sets the **start**; the scenario sets the **batches + models**. Reuse the
  PR, change the models, to test scenarios.
- The harvest spike and the biomass overage are **two symptoms of one cause** — the
  stocking plan vs the facility's combined hold (cap) + process (55k/week) capacity.
  You can trade one for the other; eliminating both needs a stocking change.
- **The default planning method is the L1-guided hybrid (§4.5), and it steers.**
  `hybrid_follow: full` builds the L1 harvest envelope, and the two levers that apply
  it, `hybrid_purge_lever` and `hybrid_production_lever`, both ship `true` — and are
  pinned `True` by the arm itself. The production path is live; the purge path is
  refused while `sixn_level_drains: false`. What you lose by that refusal is the 6N
  staging half, not the envelope: the ceiling still tells the controller to harvest
  *less* in fat weeks on production weeks under the cap. Set `sixn_level_drains: true`
  to buy the purge half as well —
  the purge one also needs `sixn_level_drains`, which ships `false` too. Measured on the
  real workbook with the live scenario, that takes weeks under the contract floor from
  20 → 16 with both levers, or 20 → 14 with the production lever alone, with zero empty
  harvest weeks either way. The older "empty week on 5 of 6 PRs" and biomass-peak
  figures predate the 2026-08-20/21 changes and have not been reproduced.
- **The biomass peak is the reserve, not waste.** Every knob that shrinks it —
  wider deviation band, guide smoothing, a more aggressive grading trigger —
  puts empty harvest weeks back. All measured across 6 real PRs; none is a
  free win. If the peak is a genuine operational problem, the lever is stocking.
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
# The app (Configure / Run / Decide / Ideal / Accuracy / How it works)
streamlit run app.py

# A single forecast, directly — prints the full stage-by-stage narration
python -m forecast.run --workbook Forecast.xlsm --output out.xlsm `
    --config-dir config --scenario-dir scenario

# NOTE: the command above runs whatever config/control.yaml says, which now means
# the L1-guided hybrid (§4.5). It has no --method flag; to run a specific method,
# either set hybrid_follow in the config or use Compare & Choose in the app.

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
with a lighter batch in a cooler system. Each move is a **relabel** of an existing occupancy segment — the batch's location rows
and its existing arrival/exit events are re-pointed at the new tank — so **no new
`Transfer` is emitted**. It therefore costs neither handling mortality nor handling
budget, and an `lns` run's transfer count is comparable with the plain controller's.

**Why it's safe (the floor is greedy).** Every candidate move is checked against the
**real continuity audit** (0 drift), input conservation (0 dropped batches), and must
**never raise the hot-spot peak** while strictly improving the lexicographic
(peak, over-cap area, balance CV) objective — so a peak-neutral move that cuts the
total over-cap area, or evens the load between systems, is kept, while a move that
lowers the peak by pushing *more* total area over cap is not. Anything else is reverted. Any error falls back to
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

## 12. Analyze mode — find my best plan (one flow)

The modes above each answer a PIECE of the real question — *which engine, with
which knobs, gives the best plan that passes the hard rules?* **Analyze** runs
that whole composition in one flow and ends in a single recommendation card:

1. **Engine round** — every planning method once on your current config (the
   same runs as Compare & Choose; finished legs are shared both ways, nothing
   runs twice).
2. **Knob round** — depends on the **Analysis depth** you pick:
   - **Quick tournament** (default, today's flow): the Grid + Deep search
     (what Auto-optimize uses) on the live-config engine, then a verification
     run of the winner on that SAME engine.
   - **Tuned tournament**: EVERY method gets its own knob search on its own
     tunable space (`knob_grid`/`knob_space` on the method registry,
     `forecast/methods.py`), so the board compares the methods **at their
     best**, not one tuned engine against the rest stock. Per method: a stock
     hard-gate check first — gate-passers get the full Grid + Deep search
     restricted to their space; a gate-failer gets a cheap **one-knob probe**
     (single pass over its space) and is marked **gate-bound** if no knob
     fixes the failure (the full search is skipped honestly). Each tuned
     winner is verified on its **own engine** and joins the board as
     *"METHOD (tuned: knobs)"*.
     Business constants (`min_harvest_weight_g`, stocking) and the
     operational rules (`max_harvest_per_week`, `harvest_relief_pct`,
     `min_harvest_per_week`, `max_transfers_per_week`) are **untunable by
     anyone** — the registry rejects a space that touches them. So are five
     knobs that are not policy at all: `grade_efficiency` and
     `handling_mortality_pct` describe **physical facts** — your grader and
     your losses — `global_assume_primed_6n` is a **modelling assumption**,
     `sixn_level_drains` is a **safety guard** (it is what stops a raised
     move-in accumulating into one 6N pair), and `hybrid_follow` is the
     **arm's identity** — a space containing it could turn `controller` into
     `controller-hybrid` and have the board compare a method with itself. A search that pushed the grader to 1.0, dropped the
     handling loss, or switched the 6N prime back on would score better by
     redefining the facility rather than by planning it better, which is the
     one thing a search must never be able to buy. A **run budget** expander shows the
     estimated engine runs per method (and how much the variant cache already
     paid for) before you press go; the headless twin is
     `python -m tools.run_tuned_tournament --workbook <PR>`.
3. **The checklist** — every candidate is judged on **nine gates**, in this
   order. Only the first two are **hard**; a hard FAIL sinks the plan whatever
   else it scores. The other seven rank a plan down without disqualifying it:

   | # | Gate | Hard? | PASS / WARN / FAIL |
   |---|---|---|---|
   | 1 | Conservation (no fish created or lost) | **HARD** | PASS iff 0 dropped and 0 over-produced |
   | 2 | Never an empty harvest week | **HARD** | PASS iff 0 empty weeks |
   | 3 | Weekly contract floor (min harvest/week) | soft | PASS iff every planner week clears `min_harvest_per_week`, else WARN with the count **and the worst week** |
   | 4 | Facility biomass cap | soft | PASS ≤100% of cap · WARN ≤110% · FAIL above |
   | 5 | Weekly processing limit + relief | soft | PASS 0 relief weeks · WARN 1–3 · FAIL >3, or any week past the derived relief ceiling |
   | 6 | Harvest targets (monthly/yearly) | soft | **never worse than WARN** — targets are penalized, never disqualifying |
   | 7 | Per-batch density quality | soft | PASS iff no batch peaks ≥1.3× its tank cap, else WARN — **never FAILs** (no knob fixes it; see §7.1) |
   | 8 | 6N one-way commitment (R7) | soft | PASS iff nothing left a depuration tank except by harvest |
   | 9 | Weekly handling budget | soft | PASS every week within `max_transfers_per_week` · WARN any week over ~80% · FAIL any week over |

   **Gate 3 is the contract; gate 2 is only its degenerate case.** "Never an
   empty week" catches a week that harvests *literally nothing*. The rule the
   business actually signed is a weekly **floor** (`min_harvest_per_week`), and
   a plan can pass gate 2 while missing that floor nine times. The count was
   always measured (it is a row on the RunComparison sheet) but until
   2026-08-12 **no gate and no score component read it** — so nothing in the
   tool defended it. Gate 3 is deliberately **soft**: near full utilisation
   every real plan misses the floor sometimes, and a gate that always FAILs is
   a gate you learn to ignore. Use it to **compare** candidates, not to accept
   or reject one.

   **The tuned tournament can no longer sell the floor to buy a better score.**
   The emphasis score has no floor term at all — its only harvest components
   are a variability CV and an over-the-limit count. Measured on the 7.29 PR
   across a 40-variant controller search, the correlation between a plan's
   worst harvest week and its score was **−0.03**: statistically blind. The
   search duly promoted knobs that cut the plain controller's worst week from
   **20,526 to 16,185 fish**, and ranked the pool's *best*-floor plan (27,462
   fish) **36th of 40**. A tuned winner is now chosen only from candidates
   whose worst harvest week is **at least as good as that method's own
   un-tuned run**. If none is, the search still returns its best and says so
   in the run log, so you can judge the trade yourself. (On the same PR this
   changed the controller's winner from the 16,185 set to one worth 21,871 —
   and left the hybrid's winner untouched, because that search was already
   holding the contract.)

   **Which weeks a gate judges.** Gates 2, 3 and 5 judge the **planner's weeks
   only**: manual-override window weeks you scripted yourself (§3.5) are
   excluded from the zero-week, sub-floor and over-limit counts, and the
   verdicts say how many were excluded. Those weeks execute exactly your script
   and are policed by the ValidationLog `MANUAL WINDOW` lints instead, so a
   deliberately harvest-free scripted week can't fail every engine at once.
   **Every other gate — including conservation — judges the whole horizon**,
   scripted weeks included.

   The practical consequence: a plan can be recommended with a red **handling
   budget** or **R7** gate. That is by design (they are operational quality,
   not correctness), but it means the checklist on the card is not decoration —
   read it before pressing Adopt.
4. **The card** — one recommended plan (pick order: hard rules → soft rules →
   target shortfall → emphasis score), with **✅ Adopt this plan** (saves the
   knobs, sets the ▶ Run forecast method, loads the run) and **⭐ Promote as
   Quick-run default**. The card and the *All candidates* table carry the same
   **provenance caption** as the Compare board (§7.4): fresh vs cached, engine
   run time, grading-rules version, inputs-signature prefix — so a candidate
   replayed from cache or re-graded under newer rules always says so.

   **Adopting a plan that breaks a rule (2026-08-15).** The card's pick order
   *ranks* on gate failures; it does not *filter* on them, and the weekly
   processing limit + relief gate is soft — so before this the recommended plan
   could carry a relief-ceiling breach straight into `control.yaml`. Adopt and
   Promote now apply the **same winner-eligibility rules** the tuned tournament
   and Optimize apply (hard gates, then the relief ceiling, then the
   contract-floor no-regression versus that method's own un-tuned run).
   Unlike those two, this door does **not** exclude anything: you can see the
   plan and may have a reason, so the buttons stay. What changes is that a
   breaching plan cannot be saved **silently** — the breach is spelled out by
   name and you tick an acknowledgement box before either button will write.
   The same applies to the *Promote a different candidate* picker, which can
   reach the rows that were ranked last precisely because they fail a rule.
   Whatever you accept is recorded: the breach text and the gate summary go
   into `analysis_defaults.yaml`'s `evidence` (so the **⚡ Quick run** card
   warns every time it offers that default), the run label in the tabs is
   flagged, and every adoption or promotion is appended to
   `adoption_history.jsonl` beside `optimize_history.jsonl`.

**Targets & prices** (Configure → Targets & prices): monthly/yearly harvest
targets in kg (HOG or gross) judged with a tolerance — *penalized, never
disqualifying* — and price-per-kg bands by fish size that turn each plan into
a revenue figure. Harvest outside every band is reported **unpriced** rather
than silently priced. These are analysis overlays: editing them re-judges
existing results instantly and never invalidates cached runs.

**The promoted default** lives in `config/analysis_defaults.yaml`, alongside
the rest of your config, and is **never written to an output workbook** — so it
cannot be lost to a run. Note the flip side: it is *not* part of the workbook's
`RunConfig` snapshot (which carries control/biology/facility/batches/limits
only), so importing config from a workbook will not restore it — and **must
not**: a workbook you open from last month would otherwise silently re-point
today's Quick run at a plan that won a tournament on a different PR. The same
goes for `targets.yaml` and `economics.yaml`, the yardstick a run is *judged*
against. The `RunConfig` sheet names all three omissions in its own header, so
the gap is visible in the workbook rather than inferred. Promotion is **manual by design**:
the tool never changes its own defaults. Once promoted, the **⚡ Quick run**
card at the top of Analyze re-validates that exact plan (one run + the
checklist, minutes not hours) — use it as the everyday sanity check and the
full analysis when the PR or the facility changes materially.

### 12.1 Tune mode retired (2026-08-06)

The old **Tune (density knobs)** mode is retired — nothing it did is gone:

- Its **per-batch peak-density distribution** and **severe-batch table** are now
  a checklist gate ("Per-batch density quality", soft — PASS/WARN, never
  disqualifying) plus a per-candidate drill-in expander on the **Analyze**
  board. The reading rule is printed right there: severe (>=1.3x) batches that
  cluster in time and peak mid-grow-out are a **stocking/capacity** problem —
  no knob fixes them.
- The **stocking-for-quality frontier** (the remedy for exactly that
  diagnosis) moved to the bottom of the Analyze board.
- Its knob *search* was already covered by Optimize's grid and Analyze's knob
  round. The headless density sweep remains available: `python tools/tune_sweep.py`.

---

### 12.2 What is actually steering the plan (2026-08-31)

The always-visible **Active configuration** panel now reports what each lever is
*doing*, not just what it is *set to*. It reads `forecast/levers.py`, which knows
which settings gate which others.

This exists because the panel was stating something false. It showed
**"Feed leveling — ON"** whenever `rebalance_level` was true — but leveling
shares `rebalance_balance_budget` and only runs inside `if _bal_budget > 0`, so
with the shipped budget of `0` the flag steered nothing. The shipped config is
exactly that combination, so the panel was wrong on every run.

Each lever now reports one of:

| status | meaning |
|---|---|
| **ACTIVE** | steering the plan |
| **OFF** | deliberately disabled — with the reason it ships off |
| **INERT** | read, but another setting gates it to nothing |
| **CLAMPED** | a smaller limit binds first (e.g. a 30-move rebalancer budget against a 15-move handling budget) |
| **SATURATED** | higher values produce an identical plan |
| **NO MEASURED EFFECT** | measured across starting states; no change observed |
| **SUPERSEDED** | no engine reads it; kept only so older configs load |

Anything not steering gets its own ⚠ row with the reason, so *"why did my change
do nothing?"* is answered on the page instead of in `placement.py`.

On the shipped configuration this surfaces **five** such settings: Feed leveling
(inert, rebalancer budget 0), Rebalancer fan-out and Rebalancer precise-count
moves (no measured effect), and the two superseded knobs.

**What it is not.** It is read-only annotation. It never changes a plan, and it
does not re-implement engine arithmetic it cannot see: the rebalancer's real
budget is `min(knob, moves left this week)`, and the second term is not knowable
from config, so a CLAMPED row names the binding limit rather than inventing a
number. Every measured claim carries its date — prose that rots against the
config is how the panel came to be wrong in the first place.

Where the measurements come from: `docs/LEVELING_TRADE_2026-08-30.md` (50 runs,
the rebalancer family) and `docs/SIXN_PURGE_LIVELOCK_2026-08-31.md` (8 states,
the 6N drain order). Re-run either with `python -m tools.measure_leveling`.

---

### 12.3 The monthly lever check (2026-08-31)

At the top of **Analyze**, before the engine tournament: run your config against
a couple of declared alternatives on **this month's PR**, ranked on the
constraints. Two minutes.

**Why it exists.** Four levers were measured across eight starting states in
August 2026 — `cap_repair_budget`, `rebalance_headroom_days`, and two 6N drain
orderings — and *all four were rejected as defaults*. Not because they do
nothing, but because each helps some starting states and hurts others. Running
the same three legs across those eight states gives eight different answers:

| starting state | verdict |
|---|---|
| 2026-01-31 | keep current |
| 2026-03-31 | + rebalancer — floor misses 13 → 9 |
| 2026-05-31 | + cap repair — feed 35 → 8 system-weeks, no floor cost |
| 2026-06-30 | + cap repair — floor 5 → 4 |
| 2026-07-31 | + cap repair — floor **10 → 3** |
| 8.13 PR | + cap repair — floor **5 → 0** |
| 8.23 PR | keep current |
| live workbook | keep current |

There is no better default to find. There is a cheap per-month answer.

**How it ranks.** Constraint-first, and score is never consulted:

1. a **hard gate** failure disqualifies outright (conservation, never-an-empty-week, 6N one-way)
2. the **contract floor** — weeks below `min_harvest_per_week`, then the worst week in fish. This is a sales commitment: a leg that misses it more often loses however good the rest looks
3. **per-system feed** breaches
4. the **handling budget**

A leg with a far better score and a worse floor still loses. The score column is
shown, labelled *"not decisive"*, and read by nobody.

**"Keep what you have" is a first-class verdict**, printed as loudly as a winner.
Differences smaller than the measured sensitivity band are not differences —
neutral perturbations swing the worst harvest week by 8,629 fish on a
deterministic engine, so anything inside that is the plan's own chaos.

**It never writes config.** When a leg wins, the check names the settings and
tells you to apply them yourself in Configure → Control. The decision, and the
audit trail, stay yours.

Headless equivalent: `python -m tools.measure_leveling --pr <PR> --combos @legs.json`.

---

### 12.4 Analyze, Compare & Choose and Optimize merged into Decide (2026-08-31)

Seven modes became five. The three modes above answered three views of **one**
question — *which plan should I run?* — and an operator had to know which one
fitted before they could ask it. Two of them ran the same engine legs over the
same roster writing the same cache, so the split cost a decision without buying
anything.

They are now one **numbered flow** in Decide, worked top to bottom:

| step | what it asks | typical answer |
|---|---|---|
| **1 · Check** | has anything changed this month? (~2 min) | *keep what you have* |
| **2 · Search** | which engine and knobs win on this PR? | confirms the plan you already run |
| **3 · Drill in** | only if you disagree with step 2 | not opened |

Steps 1 and 2 are the month's work. After that the everyday step is just
**▶ Run forecast**, until something changes.

**The first version got this wrong** and is worth recording: it made the three
modes three *tabs*, which rearranged the decisions instead of removing them —
you still had to choose a tab before you could ask anything, and picking a plan
in one tab did not feed the next. The operator's correction, on first use:
*"from one tab I can run the sweep for all, then pick the default to feed
forecast."* Compare's lenses and the knob sweep answer **follow-up** questions
("why did this win?", "what if I weighted it differently?"), so they are now
expanders under the result rather than siblings chosen up front.

Where each former mode went:

| tab | question | was |
|---|---|---|
| **Recommend a plan** | which plan, end to end — including the monthly lever check | Analyze |
| **Compare engines** | which ENGINE, on eight lenses | Compare & Choose |
| **Tune knobs** | which KNOBS on one engine | Optimize |

**Nothing was lost, and that is enforced rather than asserted.** The merge is a
*wrapper*: the three functions are unchanged and rendered as tabs, so every
capability survives by construction — the between-system and within-system CV
lenses, tank footprint, fastest run, raw density peak, welfare/crowded-biomass,
fewest-moves and steadiest-harvest lenses; the per-method metric readout;
setting the standing engine to a **non-winning** method; picking a plan while
writing **nothing** to config; the force re-run; the ~100-second engine-only
path; the partial-roster warning; and naming a failed engine leg with its error.

`tests/test_decide_mode.py` pins each of those to a marker in `app.py` and fails
the build if one disappears. That is the point: §12.1 exists because a mode was
once retired and its capabilities went quietly with it.

**Old links keep working.** A stored selection of *Analyze*, *Compare & Choose*
or *Optimize* migrates to Decide and opens on the matching tab, so a returning
operator lands where they left off. The migration runs *before* the mode radio
renders — Streamlit raises if a stored value sits outside the options, which is
the same bug the Tune retirement had to fix.

**What this does and does not change.** It makes the tool easier to operate
correctly: one place to ask one question, fewer chances to answer it with the
wrong tool. It does **not** change what any engine computes, and no plan differs
because of it.

---

### 12.5 Targets GRADE. The weekly band STEERS. (2026-09-01)

The single most confusing thing about this tool, now stated on the screen where
it matters.

**`config/targets.yaml` is read by the grading layer and by no planner module.**
Setting a December target of 600 t tells you the plan missed by 224 t. It does
not move one fish. That is by design — the targets gate is *penalised, never
disqualifying* — but an operator who sets a target, re-runs, sees nothing change
and concludes the tool is broken has been misled by the interface.

**What actually redistributes tonnage is the per-week harvest band:**
`min_harvest_per_week` / `max_harvest_per_week` overridden for a given week in
`scenario/limits.yaml` (Configure → **Limits**). Those resolve through
`caps.resolve_facility_cap` into the controller, so capping a fat month's weeks
genuinely defers fish into a lean one. It is the same mechanism as the
FacilityLimits bands in the VBA tool.

**Configure → Targets & prices** now opens with *"Where your last plan landed"*:
each month's planned tonnage, your target beside it, the gap, and the per-week
band currently in force — so targets are set against real numbers rather than
guessed, and the lever is named in the same view.

Three details it is careful about:

* **A month whose weeks disagree shows no band.** Blank means unset **or
  mixed**; a month with some weeks capped and others not has no single number,
  and inventing one would invite an edit that silently flattens the difference.
* **Partial months are flagged.** The first and last month of a horizon are
  short by arithmetic. Reading them as troughs is a real trap — an August run
  ends with a 217 t October that is simply the horizon stopping.
* **The fish-per-week readout is arithmetic, not advice.** This plan is
  chaos-sensitive (a deliberately neutral knob moves the worst harvest week by
  8,629 fish), so it reports the SIZE of a gap and never claims a setting will
  close it. A shortfall also says where the fish would have to come from,
  because raising a floor does not create them.

**The loop:** run a forecast → read where it landed → set targets so the gate
reports the gap → set the band in Limits to move it → re-run.

---

## 13. Accuracy (forecast vs actuals) — grading the biology

> **Measurement tooling (2026-08-21).** Four CLI tools now grade the model
> against the operator's own history, and `pr_corpus/` (21 monthly Production
> Reports, 2024-11 .. 2026-07, plus per-era batch registries) is committed to
> the repo so any of them reproduces from a clean clone.
>
> - `python tools/growth_check.py --corpus pr_corpus` — grades the SW growth
>   curve **alone**: one batch across two consecutive reports, no planner, no
>   placement, no harvest, no alignment. A miss here is the curve's, with
>   nothing else to blame. Currently **-1.4% median, 4.0% typical**.
> - `python tools/backtest.py --corpus pr_corpus --out backtest --registries pr_corpus/registries`
>   — replays the WHOLE pipeline from each historical month and grades it
>   against what actually happened.
> - `python tools/error_model.py --results backtest/backtest_results.jsonl`
>   — turns that into forecast error BY HORIZON. Currently **1m -0.2% median /
>   4.4% typical, 2m -3.2% / 6.1%, 3m -5.4% / 7.2%**.
> - `python tools/forecast_bands.py --forecast out.xlsm --model backtest/error_model.json`
>   — puts a confidence band on a live plan's monthly tonnage.
>
> **Read the horizon limit.** Only 1-3 months are quotable; 4-6 still carry
> harvest-execution contamination and print as INDICATIVE. And a whole-pipeline
> backtest cannot ATTRIBUTE a bias — it grades planner, placement, harvest,
> registries, alignment and growth at once. When one appears, isolate the
> component and grade it alone (that is what `growth_check.py` exists for); a
> -15% "model bias" chased through the harness in Aug 2026 turned out to be a
> reconstruction artifact, not biology.

Every other mode grades a **plan**. The test suite proves **bookkeeping** — no
fish created or lost, rules respected (§6). Neither can tell you whether the
**growth model matches your facility**. This mode can, and it is the only one
that can.

### Why it needs nothing new from you

You are already generating the measurement and throwing it away:

- **Each month's ProductionReport *is* the actuals** — real per-tank counts and
  weights at a real closing date.
- **The previous run's output workbook holds the prediction for that same
  date** (the `BatchLocations` sheet).

Because you re-anchor every month, model error never *accumulates* — but it was
also never *measured*: each month's prediction was replaced rather than graded.
This mode grades it. It reads two files, runs nothing, saves nothing, touches no
config, and can never change a plan.

### Using it

Sidebar → **Accuracy (forecast vs actuals)**.

1. Upload a forecast workbook you produced **earlier**.
2. The actuals default to the ProductionReport already in the sidebar — upload a
   different one only if you want to grade against another date.

You get: the **typical** and **worst** batch-level weight error over the elapsed
weeks, a **signed** bias verdict, a per-batch table, facility totals, an
alignment-sensitivity panel, and a separately-labelled tank-adherence view.

### The distinction that matters most

| View | What it measures | How to read a mismatch |
|---|---|---|
| **Per batch** (primary) | The **biology**. Fish summed per batch across whatever tanks they ended up in. | A real prediction error. **Weight** is the growth-model score. |
| **Per tank** (secondary) | **Plan adherence** — did the fish end up where the plan put them. | An operator **decision**, or a plan you improved on. **Not** model error. |

Conflating the two would make the report worse than useless: if you moved fish
differently from the plan, a tank mismatch says nothing about the growth model.

Within the batch view, **weight** is the clean score. **Count** and **biomass**
also move with harvest, culling, grading and transfers, so they mix model error
with execution.

### Date alignment (why this is not a footnote)

The forecast produces a value once a week; a PR closes on whatever date it
closes. On this facility the typical weight error moves **~0.8 percentage points
per day** of gap — grading one forecast against one PR at three consecutive
weeks returns 0.95 %, 6.36 %, 13.38 %. Grading against a snapshot up to 3.5 days
away would therefore charge the calendar to the growth model and swamp the
signal.

So the batch view reads the prediction at the **exact** closing date, by
interpolating between the two bracketing weekly snapshots — a value the forecast
already implies between two points it already produced, not a growth
assumption. The **📅 How much does the date alignment matter?** panel shows what
the neighbouring weeks would have said, so the choice is visible rather than
asserted. Tank occupancy is discrete and is **not** interpolated; that view
states its own offset in days.

### What it CANNOT measure

- **Harvest execution.** A PR is a snapshot of what is in the water; fish
  already sold are simply absent. A batch harvested earlier or later than
  planned shows as a count miss that is *not* a model error.
- **Freshwater.** `BatchLocations` snapshots seawater (OG) tanks only, and the
  PR's FW units have no counterpart there. FW error is tracked separately by the
  calibration history below.
- **Anything outside the overlap.** A batch present in only one file is listed
  under coverage and excluded from every average — never quietly averaged in.
- **A single pair is one observation.** Grade several to see whether a bias
  holds.

### The noise floor — check this before believing a small number

Two ProductionReport exports for the **same** closing date can disagree. On the
July 2026 chain, two PRs both closing 2026-07-31 differed by a **median 3.2 %
(max 6.2 %)** in per-batch mean weight. Below roughly that, a "model error" is
inside the resolution of your own actuals. Use it as the floor for how finely
this measurement can discriminate — and as a reason not to tune a plan
difference smaller than it.

### Freshwater calibration history

Every run with `auto_calibrate_fw` on back-solves each freshwater batch's
`fw_correction` and rewrites it — `B37: fw_correction 1.000 -> 0.774` means the
model grew that batch **29 % faster than reality**. Those rewrites used to
scroll past in the run log and land in one workbook's `ValidationLog`, so a
correction needed every month for six months looked exactly like a one-off.

They are now appended to **`fw_calibration_history.jsonl`** at the repo root,
beside `optimize_history.jsonl` and `adoption_history.jsonl` (gitignored, and
written best-effort so a logging failure can never break a run). The mode reads
it back as a drift table. A batch flagged **persistent** — the applied
correction has sat away from the configured value across at least three runs —
is a **standing model error to fix in the biology config**, not to re-discover
every month.

---

## 14. Ideal (what should we stock?) — the steady rhythm (2026-09-10)

Every other mode plans the fish you already have. **Ideal** asks the question
underneath: *what stocking rhythm — how many smolt, how often — could this
facility carry forever?* — and then how to get there from the fish you have.
It works in three steps on one page: **1 · Quick scan** (seconds, tankless)
rules rhythms in or out; **2 · Reference sheet** runs your batch table through
the real forecast engine from an empty facility; **3 · Transition** re-sizes
future stockings from today's ProductionReport and hands you the proposed
schedule. Nothing on the page writes your config or scenario.

### 1 · Quick scan — using it

1. **Mode → Ideal (what should we stock?)**.
2. Set the **Biomass cap** slider. It starts at the cap in your Control config,
   but the cap is a variable, not a permit — try values.
3. Optionally open **Grid** to choose which cadences (days between stockings)
   and batch sizes (fish to OG) to try. The default is 3 cadences × 6 sizes =
   18 runs of 2–5 s each, spread over the sidebar's **Computer power** workers.
4. Press **Find the ideal rhythm**.

It shows the best **balanced** rhythm at that cap, today's rhythm (read from
the last six batches in your scenario) measured at the same cap, and a table
of every rhythm tried. Move the slider afterwards and the page says the result
is stale until you press the button again.

### How it is measured

Each rhythm is a synthetic stream — one batch every *cadence* days, all the
same size, copied from your latest batch — run through the L1 harvest envelope
from a **clean start** for three years. Only year 3 is read, once the start-up
has washed out. Harvest is priced on the `config/economics.yaml` bands across
the model's size spread. There is no search and no solver: every number is one
plain run.

**Balanced** means both: it lands at least **95 %** of what it stocks, and its
peak standing biomass is no more than **2 %** over the cap. The 2 % allowance is
structural — the envelope harvests one week late, so a perfectly good plan
reads 1–1.5 % over. Judge a rhythm by its peak, never by counting "illegal"
weeks.

### What the numbers said when it was built (3,800 t vs 4,200 t)

| Cap | Best rhythm (quick scan) | Smolt/yr | Quick scan HOG / revenue | Your engine HOG / revenue / avg fish (step 2) |
|---|---|---|---|---|
| 3,800 t | 49 d × 280k | 2.09 M | 7,625 t / $163.6M | 6,583 t / $136.8M / 4.05 kg |
| 4,200 t | 49 d × 250k | 1.86 M | 7,642 t / $167.3M | 6,582 t / $141.0M / 4.56 kg |

"Your engine" is the method and settings Run forecast uses — measured with the
promoted plain controller (hybrid off, your tuned knobs). **Believe the
engine's numbers:** the quick scan runs about 16 % high on tonnage and 19 % on
revenue because it has no tanks — the engine grows smaller fish under real
density and handling limits. Here the two agree on the ranking (4,200 t with
250k batches wins: the same tonnage, much bigger fish), but a different engine
can disagree — the hybrid preferred 3,800 t. Use the quick scan to rule rhythms
out; settle close calls with the reference sheet, which runs your engine.

Today's scenario stocks 49 d × 340k (2.53 M smolt/yr), which at 3,800 t is not
balanced: stocked fish outrun what can be landed and pile up. Above roughly
**5,000 t** there is no balanced rhythm at all — harvest is limited to about
one smallest tank per week (`og_tank_ceiling_kg`, ≈7,700 t HOG/yr), so a bigger
cap only builds a backlog. These figures will move whenever biology or prices
are recalibrated; re-run the mode rather than quote the table.

### 2 · Reference sheet — the real engine

Enter batch count, input date, growth and FCR model per batch, plus facility
limits, and run it as a reference sheet. Fill the table from a rhythm (it
defaults to the quick scan's best, or today's), then edit any row — the columns
and format are exactly Configure → Batches'. Set the facility limits for this
run only; your Control values are the defaults and are not changed.
**Run the reference sheet** runs the same forecast method and settings
▶ Run forecast uses (the page names it) on that table from an **empty facility** for
three years and reads the steady third year: tonnage, revenue, harvest weight,
the share over 8 lb, peak biomass against the cap, tanks in use per system, one
week's full tank layout, each tank's batch sequence, and the checks. Download
the workbook to keep it.

**Tank & system limits for this run** (optional expander, also in step 3 for
the proposal): one row per seawater system with its tanks, **tank density cap**
(kg/m³), **system biomass limit** (t) and **system feed limit** (kg/day), filled
from `config/facility.yaml` and `scenario/limits.yaml`, plus the **weekly move
budget**. Change any cell to try it — only values you change are applied, only
for that run; your files are never written. 6N's biomass here is its
production-mode limit; its purge-mode limit is your depuration ruling and is
not changed. Dated per-week rows in Configure → Limits still win for their
weeks. A cleared cell keeps the value in your files (the page says so). The
**quick scan** takes a **Min harvest weight** too, and steps 2–3 follow it
after a scan; the tank and system limits apply in steps 2–3 only (the quick
model has no tanks).

Two things are fixed by the method, not chosen: an empty facility cannot hold
fish that would already be in seawater on day one, so those batches are left
out (and listed); and 6N runs in **production** mode, as it will after 2028,
because an empty 6N cannot start its purge rotation. A small FW mass-balance
warning on one early batch is expected for the same reason.

**The checks decide whether the answer is real — and every limit is hard.**
The engine finishing with clean conservation audits does not make a plan
feasible — an overstocked run once "finished" at 1,685 % of the cap. So each
run is graded, and your ruling (2026-09-10) is that a plan is **within the
limits only with zero breaches**: every week harvests, the harvest floor, the
biomass cap, tank density, the per-system biomass and feed limits and the
weekly move budget are all FAILs when broken, as are the engine and its
conservation audits. The biomass cap has **no tolerance** in the engine (the
2 % allowance belongs to the tankless quick scan, whose envelope harvests a
week late). The system limits are still flagged above the limit + your
`global_buffer_pct`, exactly as the SystemLimitsAudit sheet flags them — you
kept that buffer. **FW mass balance** stays a note (WARN): it is a
model-accounting check, not a facility limit, and an empty start trips it on
one early batch by construction. The headline reads **within every limit ✅**
or **breaks the limits ❌**. A plan that breaks a limit is not an answer: its
revenue prices fish the facility could not carry, feed, handle or land within
its limits.

### Optimizer — the best rhythm within every limit

Inside step 2, the **Optimizer — best rhythm within every limit** expander
searches stocking rhythms **and biomass caps** for you. It runs on what the
reference sheet runs on — its facility limits (the min harvest weight
included — step 2's box follows step 1's after a scan), the **Tank & system
limits** table and move budget, and the method and knobs ▶ Run forecast uses
— at each of the **caps to try** (below) instead of step 2's cap box.

- **Objective** — Revenue, Harvest tonnage (HOG), or Biomass gain: live weight
  harvested plus the change in standing fish over the year (dead fish are not
  gain). In a steady year the standing stock barely changes, so biomass gain
  tracks tonnage and the two usually pick the same rhythm; revenue can pick
  differently because it prices fish size.
- **Input frequencies** — days between stockings, a comma list (default
  42, 49, 56, 63).
- **Batch sizes** — fish to OG per batch: from / to / in steps of (default
  180,000 to 300,000 in steps of 20,000).
- **Caps to try (t)** — biomass caps in whole tonnes, a comma list. The
  default is your **cap slider in step 1** and three 200 t steps below it
  (3,800, 3,600, 3,400, 3,200 at a 3,800 t slider); move the slider and the
  list is re-filled from it. **The optimizer never searches above your cap
  slider:** a cap above it, a cap under 500 t or an entry that is not a whole
  number is refused in red, and nothing runs. Every rhythm runs, and is
  judged, at each cap — a lower cap can keep tanks under their density cap
  while bigger batches keep the tonnage (see the table above).

Before you press **Find the best rhythm** the page states the grid size —
frequencies × sizes × caps — and the estimated time. Each rhythm at each cap
is one real-engine run from an empty facility (~20 s), spread over the
sidebar's **Computer power** workers; the default 4 frequencies × 7 sizes ×
4 caps is 112 runs, plus one wave of up to 20 stability runs (below), and the
estimate gives the grid's minutes and the wave's separately. It takes gaps of
7–140 days and batches of 50,000–600,000 fish (step 2's own ranges, so a
winner always loads there), and refuses a grid with no frequency, size or cap,
or more than 150 runs.

**What it accepts:** a rhythm counts only if it breaks **no** limit at the cap
it ran at — the same checks as the reference sheet, zero breaches. Among those
it picks the highest objective (a tie goes to the smaller batch, then the
longer gap, then the higher cap — the closest to your setting). The answer
names the cap, for example **49 d × 220,000 @ 3,200 t**. If none qualifies it says so in
red and names the closest rhythm and cap — the fewest breaches — and what it
breaks. A rhythm that fails a check which is not a limit (the
engine not finishing, the conservation audits, input conservation) is never
the closest while another only breaks limits: it is further from a plan. The
table lists every rhythm at every cap (a **Cap (t)** column), the ones within
the limits first by the objective,
then the ones that only break limits by total breaches, then those that fail
another check, with one column per limit and an **Other failed checks**
column naming those checks. A rhythm the engine failed on shows its error and
never counts.

**Cost of the limits** (a blue line under the answer, for scale only): the
best rhythm in the grid if the limits were ignored, what it would score, how
much more than the plan within every limit, and everything it breaks — for
example 76 tank-weeks over density, 37 system-weeks over feed. When the
best-scoring rhythm is itself within every limit the line says the limits
cost nothing in this grid. If that rhythm also fails a check that is not a
limit (the engine not finishing, the conservation audits, input
conservation), the line says it is not a valid plan, so its score is not a
real cost of the limits. It never has a Use button: a plan that breaks a
limit is not an answer.

**Stability check.** The planner switches between modes under small changes,
so a rhythm with zero breaches can sit next to one that breaks a limit (49 d
× 203k did, between two zero-breach sizes). After the grid, the optimizer
takes the top 10 rhythms within every limit and runs each one's neighbours —
the same gap and the same cap, 5,000 fish per batch fewer and more — in one
extra wave of at most 20 runs (a neighbour already in the grid at that cap is
reused, not re-run). A rhythm is **stable** when both neighbours are within
every limit. A stable winner gets a green **Stable** line naming its cap. A
fragile one gets a yellow **Fragile** warning naming the neighbour that
breaks and what it breaks, then the best stable plan among the top 10 — or
says none of them is stable, so treat any of them as fragile.

**Using the answer.** When the winner is stable, **Use this rhythm and cap in
the reference sheet** loads the rhythm into step 2 (and step 3's size), the
way the quick scan does, **and the cap it ran at** into step 2's biomass cap
and step 3's what-if biomass cap. Step 2 then says so — for example "Cap set
to 3,200 t by the optimizer — your Control cap is 3,800 t" — and step 3's
proposal runs at that cap through its what-if limits (today's plan still runs
at your current limits). Your Control cap is not changed. When the winner is
fragile but another of the top 10 is stable, the main button is **Use the
best stable plan** and a second button, **Use the top plan anyway**, loads
the winner; both hand over their own cap. When none is stable there is one
button, for the winner. Then run the reference sheet to see its tanks and
checks. Loading a plan does not make the optimizer's answer stale. Change any
of its inputs afterwards — the frequencies, sizes, caps, objective or limits
— and the result is marked stale, and the Use buttons are greyed out until you
press **Find the best rhythm** again.

What drives the answer is the load, fish per week (batch size × 7 ÷ days
between stockings). Measured 2026-09-10 at 3,800 t with your promoted
controller: below about 27,000 fish/week the harvest floor fails, and above
about 29,000–30,000 tank density and system feed break — so the band that
meets every limit is narrow.

**Which rhythms meet every limit?** (2026-09-10; your engine, empty-facility
start, 3,800 t cap, steady year 2029; 54 rhythms, input every 35–70 days ×
135k–310k fish.) What decides it is **fish entering per week**. Below about
28,000 a week the harvest floor fails, because there are too few fish. Above
about 30,000 a week, tanks go over their density cap and systems over their
feed limit. Eight rhythms meet every limit; the best and the nearest misses:

| Rhythm | Fish / week | Revenue / yr | HOG / yr | Avg fish | Peak vs cap | Breaches |
|---|---|---|---|---|---|---|
| **49 d × 210k** | 30,000 | **$103.1M** | 4,874 t | 4.32 kg | 81 % | **0 ✅** |
| 42 d × 180k | 30,000 | $96.6M | 4,692 t | 3.89 kg | 65 % | 0 ✅ |
| 42 d × 174k | 29,000 | $91.9M | 4,455 t | 3.94 kg | 67 % | 0 ✅ |
| 56 d × 232k | 29,000 | $91.4M | 4,430 t | 3.92 kg | 67 % | 0 ✅ |
| 49 d × 200k | 28,571 | $89.3M | 4,383 t | 3.74 kg | 58 % | 0 ✅ |
| 49 d × 203k | 29,000 | $96.9M | 4,676 t | 4.04 kg | 67 % | 1 (feed) |
| 49 d × 217k | 31,000 | $121.0M | 5,643 t | 4.58 kg | 85 % | 17 (16 density, 1 feed) |
| 49 d × 280k | 40,000 | $136.8M | 6,583 t | 4.05 kg | 93 % | 115 (76 density, 1 biomass, 37 feed, 1 floor) |

Three things to take from this:

1. **Hard limits cost about a quarter of the revenue with today's engine.**
   $103M/yr against $137M for the 280k rhythm, which earlier looked ideal
   but breaks limits 115 times.
2. **The binding limits are tank density and system feed, not the biomass
   cap.** The best plan within every limit peaks at only 81 % of the cap. The
   fish fit the facility; the engine cannot spread them across the tanks
   without some tanks going over their density cap or some systems over their
   feed limit. That is the planning problem the density experiment targets.
3. **A zero is fragile.** 49 d × 203k sits between two sizes with zero
   breaches and still has one system-week over its feed limit: the planner
   switches between modes under small changes (see §4). Before adopting a
   rhythm, check that its neighbours are within the limits too.

**Which of those survive a small change, and what the cap does.** (2026-09-10;
the same setup; every plan with zero breaches re-run at ±5,000 fish per batch,
the optimizer's stability check.) At the 3,800 t cap only **one** of the nine
zero-breach rhythms stays within every limit at ±5,000 fish: 42 d × 168k,
$82.1M/yr, 3.54 kg fish. The others break something at one neighbour: the
floor on the smaller side, tank density or system feed on the bigger side.

The harvest floor is not what holds the plan back. With the floor lowered to
22,000 fish/week, 42 d × 168k earns $117.8M with 4.96 kg fish, but it breaks the
limits 38 times (36 tank-weeks over density). With less harvest forced, the
engine keeps fish longer, and standing biomass climbs to about 90 % of the cap.
With today's engine, tanks start going over their density cap at about 85 % of
3,800 t, so the floor is what keeps density in check.

**The biomass cap is the lever.** A lower cap holds standing biomass below the
point where tanks go over density, while bigger batches keep the tonnage up
(your floor of 26,000 fish/week, zero breaches and stable at ±5,000 fish):

| Cap | Best stable rhythm | Revenue / yr | HOG / yr | Avg fish |
|---|---|---|---|---|
| 3,800 t | 42 d × 168k | $82.1M | 4,094 t | 3.54 kg |
| **3,200 t** | **49 d × 220k** | **$108.8M** | **5,209 t** | **4.10 kg** |

The 3,200 t row comes from the optimizer's own run: input every 42, 49 or
56 days, 200k–256k fish per batch, caps 3,000–3,800 t. 49 d × 210k is stable
there too, at $104.0M.

Higher earners within every limit exist but are fragile. At 3,200 t,
56 d × 256k earns $117.1M, but 5,000 fish fewer per batch puts 7 tank-weeks over
density. Some combinations go badly wrong and the checks reject them. One
example: 42 d × 192k at a 26,000 floor let standing biomass run to 130–143 % of
3,000–3,400 t caps. That is why the optimizer searches the cap as well as batch
size and frequency, and keeps only plans that stay within every limit.

### 3 · Transition — from today's fish

Needs today's ProductionReport (sidebar). Fish already in the water can't
change, and everything entering seawater in the next ~12 months is already in
freshwater, so the only lever is the size of **future** stockings. Choose the
date after which stockings change (never earlier than the PR) and a size — or
a ramp, e.g. `240000, 240000, 240000, 280000` (the last size repeats). Each
re-sized batch keeps its dates; its egg count scales by its own freshwater
survival, so it still culls to exactly the target at transfer.

You get the list of changed batches and **the proposed `batches.yaml` as a
download** — your scenario is never changed from here. **Check both schedules
in the real engine** runs today's schedule and the proposal on your PR for four
years and shows, per year, peak biomass against the cap, tonnage and whether
each year passes the checks. To adopt a proposal: keep a copy of
`scenario/batches.yaml`, then replace it (or edit the same rows in Configure →
Batches). Run forecast then runs it unchanged — its 85-week horizon shows only
the start of the effect. To have the size and cap searched for you, use the
**transition optimizer** (below).

**Within the limits? — the two-window rule** (your ruling, 2026-09-11).
Today's plan already breaks limits in the near years from fish already
stocked — on the 8/31 PR, over 2027–29: 195 tank-weeks over density,
3 / 137 system-weeks over biomass / feed and 2 weeks over the move budget.
Re-sizing future stockings cannot change those fish, so a flat zero-breach
rule would reject every proposal. Instead the check splits the run at the
**effect year**:

- **Effect year** — the first calendar year that starts on or after the day
  the first re-sized batch reaches seawater (its TranOG date). A first
  re-sized TranOG on 2027-09-23 gives 2028; one on 1 January 2028 gives 2028.
  With no re-sized batch (only the cap changes) it is the first year the
  what-if cap applies to every week with no dated per-week cap row winning —
  2027 on today's limits file, whose dated cap rows are all 2026 — and, if
  that cannot be read, the second calendar year of the run. When nothing is
  re-sized and the cap is not changed either (only the harvest limits, the
  move budget or the tank & system table change), the same rule places the
  same year, and the page says the cap was not changed. The page names the
  effect year and why.
- **Early years** (before the effect year): the proposal must be **no worse
  than today's plan**, year by year and limit by limit — tank-weeks over
  density, system-weeks over biomass, system-weeks over feed, weeks over the
  move budget, weeks under the harvest floor, weeks with no harvest and weeks
  over the biomass cap. Being better in 2026 does not make up for being worse
  in 2027.
- **Judged years** (the effect year on, a partial last year included): **zero
  breaches** on every limit.
- In every year the proposal may fail no other check (the engine not
  finishing, the conservation audits, input conservation).

The proposal is within the limits only if all of these hold, each plan
judged against the limits it ran with. The page says so in green and names
the effect year. Otherwise it names each failure in red — for example "worse
than today: 12 vs 9 tank-weeks over density in 2027" or "3 tank-weeks over
density in 2029" — so the headline never contradicts the tables. The table
under it shows, per limit, today's and the proposal's totals in the early
years (✗ names each year the proposal is worse) and in the judged years (✗
names each year with a breach). The per-year table has a **Rule** column —
"no worse than today" or "zero breaches". The proposal's ✓/✗ follows that
rule; today's plan's column names every check it fails outright, for
reference. If the two runs share no year there is nothing to compare, and the
page says so in red instead of giving a verdict.

**No judged year = not certified.** If the effect year falls after the run's
last year (a late **Change stockings after** date: the first re-sized batch
reaches seawater in the run's last year or later), every year is an early
year and none is held to zero breaches. A proposal that is merely no worse
than today's plan everywhere then passes the rule's letter, but the page
shows it as a **yellow warning — "Not certified within the limits"**, never
green. Move the date earlier to see its effect judged.

**Facility limits for this check** (optional expander): try a different biomass
cap or harvest limits for the **proposal** — for example "what if the cap were
4,200 t and future batches were 250k?". Today's plan always runs with your
current limits, so the table compares where you are with where you would go;
with no batch changes it shows today's plan under the new limits. Your Control
values are the defaults and are not changed; only values you change are
applied, and the page says which — and what the shown result ran with. Each
limit applies wherever it has no dated per-week row in Configure → Limits,
exactly as in Run forecast; the page lists which limits have dated rows and
for which weeks (on the current config the biomass cap's rows cover late 2026,
so a new cap takes effect from 2027). If your Control values change, the boxes
reset to the new values rather than keep an old one.

*The two tables below were measured under an EARLIER rule, "no worse than
today" totalled over 2027–29 (2026-09-10, before the hard-limit ruling), and
are kept as dated measurements. A flat zero-breach test rejected every one of
them — today's plan itself has 195 tank-weeks over density — which is why the
two-window rule above replaced it (2026-09-11). Re-judge any of them with the
Check button or the transition optimizer below.*

**Can the cap go up within the system constraints?** (2026-09-10; your engine,
the 8/31 PR, 208 weeks; a new cap applies from 2027; breaches summed over
2027–29.) Your rule: the cap may rise if the plan stays within the system
constraints. Today's plan already breaches some limits, so the test is "no
worse than today's plan at today's cap":

| Cap · future batches | Revenue 2027–29 | Tank-weeks over density | System-weeks over biomass / feed | No worse than today? |
|---|---|---|---|---|
| **3,800 t · today's 340k** | $435.8M | 195 | 3 / 137 | the baseline |
| 3,800 t · 320k | $433.1M | 181 | 2 / 125 | ✅ |
| 3,800 t · 300k | $433.3M | 238 | 7 / 126 | ❌ |
| **3,800 t · 280k** | $424.1M | 136 | 2 / 59 | ✅ clearly |
| 4,000 t · today's 340k | $459.1M | 485 | 10 / 235 | ❌ |
| 4,000 t · 320k / 300k / 280k | $448.0M / $439.3M / $436.8M | 287 / 304 / 283 | 4 / 169 · 3 / 120 · 7 / 113 | ❌ |
| 4,200 t · today's 340k | $456.6M | 408 | 9 / 202 | ❌ |
| 4,200 t · 320k / 300k / 280k | $451.9M / $459.0M / $449.2M | 369 / 585 / 456 | 5 / 158 · 33 / 252 · 20 / 154 | ❌ |

**Not with today's tanks and engine.** Every 4,000 t and 4,200 t path earns
more ($1–23M over three years), but takes tank-density breaches from 195 to
283–585 tank-weeks, and most also add system biomass or feed breaches — the
extra fish do not fit the tanks at their density caps. **The binding limit is
tank density, not the biomass cap.** Within today's constraints the choices
stay at 3,800 t: today's schedule, 320k (−$2.7M, slightly fewer breaches), or
**280k** — the clean option: 30 % fewer density breaches and 57 % fewer system
feed breaches for −$11.7M (−3 %). Using a higher cap would need more room per
tank (higher density limits or more volume) or a planning engine that keeps
density in bounds at higher biomass; none of the stocking sizes tested does.

**What if the limits themselves go up?** (2026-09-10; the same runs, today's
batches, a 4,200 t cap, with the tank & system limits raised on the 11
seawater systems now at 85 kg/m³ — not 6N):

| Limits raised | Revenue 2027–29 | Tank-weeks over density | System-weeks over biomass / feed | No worse than today? |
|---|---|---|---|---|
| none (today at 3,800 t) | $435.8M | 195 | 3 / 137 | the baseline |
| density 95 kg/m³ | $464.7M | 196 | 43 / 304 | ❌ |
| density 95 + system biomass 450 t | $455.0M | 160 | 0 / 256 | ❌ feed |
| density 95 + biomass 450 t + feed 3,500 kg/day | $449.4M | 66 | 0 / 30 | ✅ |
| density 95 + biomass 450 t + feed 4,000 kg/day | $457.2M | 82 | 0 / 4 | ✅ |

The limits bind in a chain: tank density first, then each system's biomass,
then each system's feed. **Raise all four together and a 4,200 t plan is no
worse than today on every constraint — cleaner, in fact — for $13.6–21.4M
more over three years.** Raise only some and it breaks. Whether the tanks,
oxygen and feed systems can really carry those values is an engineering call,
not a software one; the limits table in steps 2 and 3 lets you test any
values you think are real.

What your engine said on the 8/31 PR (2026-09-10; the promoted plain
controller, 208 weeks, your manual events, every future batch re-sized from
the PR onward):

| Future batches | 2028 peak / HOG / avg fish | 2029 peak / HOG / avg fish (≥ 8 lb) | 2029 revenue | 2029 tank-weeks over density |
|---|---|---|---|---|
| 340k (today) | 97 % / 7,504 t / 3.66 kg | 91 % / 7,103 t / 3.55 kg (8 %) | $142.8M | 37 |
| 310k | 97 % / 7,220 t / 3.78 kg | 92 % / 6,885 t / 3.76 kg (15 %) | $140.5M | 56 |
| 300k | 96 % / 7,099 t / 3.89 kg | 100 % / 7,073 t / 4.00 kg (26 %) | $146.6M | 97 |
| 290k | 96 % / 7,093 t / 3.94 kg | 92 % / 6,747 t / 3.95 kg (24 %) | $139.4M | 64 |
| 280k | 96 % / 6,994 t / 3.99 kg | 91 % / 6,701 t / 4.06 kg (29 %) | $139.5M | 46 |

2026 is over the cap in every row (105 % of your 3,650 t 2026 limit — those
fish are already in the water), and 2027 barely moves, because nothing
re-sized reaches seawater before 2027-09-23. **With your engine, today's plan
stays under the cap** — it copes with the extra fish by harvesting them
smaller: by 2029 the average fish is 3.55 kg and only 8 % clear 8 lb.
Re-sizing to 280–300k keeps fish around 4 kg (a quarter or more over 8 lb) for
about the same revenue — today's plan earns most in 2028, 300k most in 2029.
**The choice is tonnage now against fish size later**, and it is yours. (The
hybrid engine answers differently — it held today's plan 3–6 % over the cap —
which is why the page always runs the method Run forecast uses.)

### Transition optimizer — best future batch size and cap within the limits

Inside step 3, once the PR is loaded, the **Optimizer — best future batch
size and cap within the limits** expander searches the two levers a
transition has: the size of **future** stockings and the **biomass cap**.
Every stocking after the **Change stockings after** date is re-sized, as the
sizes box does it, and dates and cadence are kept. The optimizer looks for the
plan that scores highest while staying within the limits under the two-window
rule above.

It runs on what the Check button runs on: the harvest limits and move budget
in **Facility limits for this check**, the **Tank & system limits** table,
and the method and knobs ▶ Run forecast uses. It uses the **caps to try**
instead of the what-if cap box. Each size and cap is one run of the real
engine on your PR — 208 weeks, with your manual events, the same call the
Check button makes for the proposal. **Today's plan** runs once beside them
at your current limits; the early years are compared with it.

- **Objective** — Revenue, Harvest tonnage (HOG) or Biomass gain, **summed
  over every year of the run** (2026–2030 on the 8/31 PR; the first and last
  years are partial; every plan covers the same years).
- **Future batch size** — from / to / in steps of (default 240,000 to
  340,000 in steps of 20,000, so today's 340k is in the grid).
- **Caps to try (t)** — whole tonnes, a comma list. The default is your **cap
  slider in step 1** and three 200 t steps below it (3,800, 3,600, 3,400,
  3,200 at a 3,800 t slider); move the slider and the list is re-filled from
  it. **The optimizer never searches above your cap slider:** a cap above it,
  a cap under 500 t or an entry that is not a whole number is refused in red,
  and nothing runs. At most 60 plans (sizes × caps).

Before you press **Find the best transition**, the page states the number of
plans and engine runs — one per plan, +1 for today's plan, then up to 10
stability runs — and the time, at about 90 s per engine run spread over the
sidebar's **Computer power** workers. The default 6 sizes × 4 caps is
24 plans and 25 runs, plus up to 10 stability runs. The progress bar counts
every run and never goes backwards.

**The answer.** The page first names the **effect year** and why, and which
years are early and which are judged. A plan with no re-sized batch (only the
cap changes) can have an earlier effect year; the page names those plans
separately. Then come today's plan's totals, and the **best within the
limits**: its size and cap, its objective total and the difference from
today's plan, its revenue and HOG, and how many future batches it re-sizes.
A tie goes to the smaller batch, then the higher cap. If the winner's effect
year is after the run's last year (no year judged), the winner line is a
yellow **"Best by the rule, but NOT certified within the limits"** warning
instead of green, for the reason given under **No judged year** above.

If no plan qualifies, the page says so in red. It names the closest plan and
what it fails. The closest plan has the fewest failures: how far it is worse
than today in the early years, plus its breaches in the judged years.

**Cost of the limits** (a blue line, for scale only) shows the best-scoring
plan whatever it breaks, and what keeps it out of the limits. It never has a
Use button.

**Stability**: the top 5 plans within the limits are re-run with 5,000 fish
per batch fewer and 5,000 more, at the same cap — at most 10 extra runs. A
stable winner gets a green line. A fragile winner gets a yellow warning
naming what breaks, and the best stable plan among the top 5.

The table lists every plan: size, cap, batches re-sized, effect year, within
✓/✗, revenue, HOG, biomass gain, the difference from today's plan, **Early
years: vs today** ("no worse than today ✓", or each year and limit it is
worse on), **Judged years: breaches** ("zero ✓", or each breach) and any
other failed check.

**Using the answer.** **Use this plan** puts the size into step 3's **Fish to
OG per future batch** box, and the cap, in whole tonnes, into the what-if
**Biomass cap** box. When the winner is fragile the buttons are **Use the
best stable plan** and **Use the top plan anyway**. Nothing in step 2
changes. **▶ Check both schedules** then runs exactly that proposal, and
**⬇ Download the proposed batches.yaml** hands it over; adopting it is still
your act. Loading a plan does not make the optimizer's answer stale. Changing
the PR, the cutoff date, the sizes, the caps, the objective, the other
limits, the tank & system table, the engine or the config does, and greys
out the Use buttons until you press **Find the best transition** again.

**What it found on the 8/31 PR** (2026-09-11; your engine, your limits,
future stockings re-sized from the PR onward, 208 weeks, your manual events).
The first re-sized batch reaches seawater on 2027-09-23, so the effect year is
**2028**: 2026–27 must be no worse than today's plan, and 2028–30 must have
zero breaches. Today's plan earns $584.4M over the run (28,749 t HOG).

**No future batch size and cap tried meets the limits**: 0 of 24 (240k–340k ×
3,200–3,800 t) and 0 of 21 (200k–260k × 3,000–3,400 t). The closest:

| Future batches @ cap | Revenue 2026–30 | Before 2028: no worse than today? | 2028–30: zero breaches? |
|---|---|---|---|
| 210k @ 3,200 t | $487.7M | ✗ 2 weeks over the cap in 2027 | ✗ 1 system-week over biomass |
| 240k @ 3,400 t | $519.9M | ✓ | ✗ 23 tank-weeks over density, 7 system-weeks over feed |
| 200k–230k @ 3,000 t | $465–481M | ✗ 6–7 weeks over the cap in 2027 | ✗ 1–5 breaches |
| 340k @ 3,800 t (the best-earning plan) | $593.5M | ✗ 2026: 25 vs 23 tank-weeks over density, 2 vs 0 weeks over the move budget | ✗ 260 tank-weeks over density, 182 system-weeks over feed, 4 over biomass, 8 weeks over the cap |

Why:

- **From 2028 on, every plan at 3,400 t and above breaks tank density, and
  most also break system feed.** At 3,000–3,200 t, 1–9 breaches remain
  (feed, density, system biomass, the cap, the floor).
- **A lower cap takes effect from 2027.** That is where the limits pipeline
  applies it: your dated per-week rows cover 2026 only. In 2027 today's
  larger batches are still in the tanks, so a 3,000–3,200 t cap is
  overshot for 2–7 weeks. The optimizer applies your constraints exactly as
  the inputs define them. It invents no phase-in date. If the cap is
  meant to fall later, enter it as dated rows in Configure → Limits.
- The closest plan misses by three weeks in five years. Check it with
  **Check both schedules** to see where those weeks fall.

### What it CANNOT tell you

- **The quick scan (step 1) is a carrying-capacity answer.** No tanks, no
  density limits, no 15-move handling budget — which is why it runs 16–25 %
  high. The reference sheet and the transition check use the real engine.
- **The growth model runs hot** (§13): a few percent optimistic on weight, so
  tonnage and revenue here are optimistic too.
- **An unbalanced rhythm's revenue is not real.** It prices fish that are
  stocked but never landed — inventory, not production.
- **Prices are flat above 8 lb**, so the ranking favours tonnage over size. If
  the market pays more for large fish than the bands say, a slower, bigger-fish
  rhythm may be the better call. That is your judgement, not the model's.
- **Hatchery cost is not included** — fewer smolt is cheaper than shown.
