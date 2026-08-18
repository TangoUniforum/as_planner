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
→ review KPIs + tabs → **download** the output workbook.

The sidebar **Mode** selector lists six windows, in the order you normally work
in them. Each carries a one-line caption in the app itself; the same list, with
pointers into this guide:

| Mode | What it is for | Section |
|---|---|---|
| **Configure (models & control)** | Set up once — biology curves, tanks, batches, per-week limits, control knobs, harvest targets and prices | §3 |
| **Run forecast** | The everyday step — run your chosen plan on today's PR and download the workbook | §5 |
| **Analyze (find my best plan)** | "Which plan should I use?" — runs every engine, searches the knobs, grades them all, recommends ONE | §13 |
| **Compare & Choose (all methods)** | Run the engines side by side and pick which whole plan becomes the report. **This is where the planning method is chosen** | §7.4 |
| **Optimize (multi-objective)** | Sweep control knobs on ONE engine and rank the settings on an objective you choose | §7.2 |
| **How it works (the rules)** | The plain-language rulebook — what each layer decides, what it may never do, the honest limits | — |

The app still *lands* on **Run forecast**; the ordering above is reading order,
not a change of entry point.

> **Retired:** the old **Tune (density knobs)** mode is gone. Its density
> distribution and severe-batch readout are now a checklist gate plus a
> per-candidate drill-in on the **Analyze** board, and the stocking frontier
> moved there with it (§13.1). The headless sweep remains: `python
> tools/tune_sweep.py`.
>
> **Also gone:** the sidebar no longer has a **Planning method** selector. The
> method is chosen on the **Compare & Choose** board, where you can see every
> engine graded side by side first; ▶ Run forecast then re-runs whichever plan
> you picked. The current pick is shown in the sidebar above the Run button.

At the top of the sidebar, above the Mode selector, a **Computer power** slider
(10–100%, default **40%**) sets how much of the machine the *heavy* runs may
use — the Global optimal (CP-SAT) solver (also inside Compare & Choose) and the
Optimize sweeps. The caption under it translates the percent into processor
cores ("up to N of M"). Raising it lets those runs go wider, but other
applications feel slower and Optimize sweeps use more memory while a run is
going (at 100% every core may be busy — an explicit opt-in). A plain controller
**▶ Run forecast** and **Tune** are sequential and unaffected by this setting.

**How much it actually buys depends on the shape of the work.** Optimize sweeps
scale nearly linearly — each variant is a whole forecast in its own process, so
twice the workers is roughly twice the throughput. CP-SAT scales only as far as
the *individual solve* is big enough to keep the threads busy: a whole-horizon
solve will use everything you give it, but the per-week solves used by
**Compare & Choose** are small, and measured on a 20-core machine a 12-worker
setting kept only about 3 cores busy. If a heavy run isn't using the share you
set, that's usually the reason — not a broken setting.

Lower in the sidebar (visible in every mode, but it governs **▶ Run forecast**)
the app shows **which planning method is currently picked**, and — when it is
the default — why. You change it on **Compare & Choose** (§7.4), not here. The
shipped default is **Controller — hybrid (L1-guided harvest)**: the only method
measured to harvest something every single week (§4.5).

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
| `max_transfers_per_week` | weekly HANDLING BUDGET (transfer moves/week). A "move" = one distinct src→dst tank transfer with fish in it, exactly what a TransferPlan `Transfer` row shows (same-week duplicate legs are merged into one row; 0-fish float-residue legs are dropped; TranOG/Grade rows are not moves) — the engine's internal budget counts the **same unit**. Once a week's moves reach the budget, the deferrable quality passes (plan-diff *evening* top-ups, even-out, balancer, variable-quantity, remnant sweep) wait for a calmer week and the leveling resumes there; essential moves (6N rotation fills, arrival make-room/vacates, plan-diff *source drains* — tanks another batch takes over) are never blocked. A week can still end 1-2 moves OVER the budget, because the essential passes run LAST: the deferrable work spends the budget out to the cap and the essential moves that follow land on top. Two anticipatory layers that close that gap are BUILT but shipped **off** (`_ANTICIPATE_ARRIVAL_RESERVE` / `_ANTICIPATE_PACING_DEFER` in `placement.py` — engineering switches, not knobs, with no config key): a 4-arm x 3-PR x 2-knob-set ablation measured that they buy full budget compliance by starving the quality rebalancer, and pay for it out of the **harvest floor** — on the operator's own PR, weeks under `min_harvest_per_week` go 3 -> 5 and the shortfall more than doubles, and on one PR a 69,677-fish week lands past the 60,500 relief ceiling. Steady harvest outranks handling, so the plan may show a 16-17 move week instead. An overrun on the handling gate (WARN >12 / FAIL >15) means the week's quality work and essential work together exceeded the budget — most often a TranOG arrival week coinciding with a 6N rotation fill. 0 = off. **Two scope limits worth knowing:** the *split* pass is NOT budget-gated (`placement.py:3491-3498`), and the **Global engines never read this knob at all** — expect Global plans well over the budget, flagged only by the (soft) handling gate | 15 |
| `min_harvest_weight_g` | minimum weight a fish can be harvested at | 3,500 |
| `min_tank_control` | force-empty floor (fish): a harvest/transfer leaving fewer than this empties the tank (INV-5) | 7,000 |
| `min_transfer_count` | min rebalancer transfer size (fish): the density/load balancer won't split a sub-group **smaller than this OUT** of a tank (the OUT-side mirror of `min_tank_control`). **0 = OFF.** Suppresses tiny partial moves — trades fewer transfers for more *marginal* density over-cap (the small moves were doing fine-grained relief); whole-tank consolidation moves are unaffected | 0 (off) |
| `harvest_grade_to_min` | **INACTIVE — this switch no longer controls anything.** The behaviour it used to gate (on a 6N purge week whose move-in falls below `min_harvest_per_week`, peel just enough of the over-weight tail from near-market tanks to reach the floor: big → 6N purge, the small tail stays in the source tank) now runs **unconditionally** — `placement.py:1733`: *"NOW UNCONDITIONAL (subsumes the old opt-in `harvest_grade_to_min`, which remains accepted but no longer gates this)"*. Leaving it off does **not** disable the behaviour; it produced empty harvest weeks, which breaks the steady-harvest contract. Flipping the box changes only a row in the app's run summary. Kept so older configs load | n/a (inert) |
| `default_hog_yield` | gross→HOG conversion (per-week overrides in FacilityLimits) | 0.81 |
| `scenario_name` | label for the run (reports + RunConfig) | Forecast |
| `facility_biomass_deviation_pct` | **FACILITY** setpoint band — the soft band below the (FW-inclusive) facility biomass/feed cap the harvest controller runs at; the one knob for how close to the *facility* cap to run (§4.3) | (config default) |
| `global_buffer_pct` | **SYSTEM-limits** buffer (R29) — a *separate* symmetric ±% applied to per-**system** feed/biomass caps (the rebalancer headroom + SystemLimitsAudit, `caps.py`); does **not** touch the facility setpoint above | (config default) |
| `handling_mortality_pct` | mortality applied per transfer | small |
| `sixn_growth` | 6N runs as growout (vs purge) for the whole horizon | false |
| `sixn_production_start` | date 6N flips purge → production | e.g. 2028-01-01 |
| `sixn_transition_weeks` | empty/fallow window at the 6N transition (0 = none) | 0 |
| `sixn_level_drains` | **ON by default.** 6N PURGE mode only. Caps how full a 6N purge pair may get (at `max_harvest_per_week`) so weekly fills don't **accumulate** into one pair across its rotation residency — the root cause of the 90–113k drain spikes that starve other pairs into sub-`min_harvest_per_week` troughs. Surplus stays in grow-out and becomes the move-in for the next thin pair, lifting its drain toward the floor so every week meets the harvest minimum (the steady-weekly-harvest contract). *Verified vs OFF:* 6N drain peak 110k→68k (−38%), CV 0.46→0.32, weeks-below-min 38→27, fish conserved. Joins `rebalance_level` + `harvest_level_load` as a leveling default; set `false` for the old accumulate-then-dump behavior. No effect in 6N production mode | true |
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
| `hybrid_follow` | **L1 HARVEST GUIDE — `full` in the shipped config (§4.5).** Runs the Global engine's whole-horizon L1 harvest envelope first and feeds it to the controller as a per-week target band. The ceiling half is the point: it tells the reactive controller to harvest **less** in fat weeks so those fish are still there for lean ones — the one thing it can never decide for itself (all its own levers are `max()`). *Measured, 6 real PRs:* **totally empty harvest weeks 6 → 0**, weeks below floor 22.5 → 9.0, worst week 0 → 16,148 fish; **cost** peak biomass 102.6 → 107.1% of cap, peak density 102 → 124. `off` = old reactive-only behaviour. `floor` is **not** a no-op (that claim was retracted 2026-08-12) but it is **dominated** — measured on the 7.29 PR it produces a genuinely different plan (worst week 23,754 vs `off`'s 20,526) yet **11** weeks below the contract floor, worse than `off`'s 9 and far worse than `full`'s 3. Applying only the guide's floor half raises the lean weeks it can reach while leaving the controller free to over-harvest the fat ones; the **ceiling** half is what actually banks fish for later. Use `full` | `full` (dataclass default `off`) |
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
much feed its line can deliver. It changes rarely, so you state it **once** and
it applies to every week of every horizon.

**Configure → Limits** has three parts:

| Part | What it is | When you touch it |
|---|---|---|
| **System capacities** | One row per system, one column per metric: `biomass` (kg of standing fish) and `feed_per_day` (kg of feed per day). Blank = no *standing* capacity, which means no cap at all for that metric **unless** a mode row below supplies one — which is exactly why **OG6N's biomass is blank here**. The editor names any such cell under the grid. | Whenever a real capacity changes. This is the normal edit — one cell. |
| **Mode-specific capacities** | A capacity that depends on what the system is being *used for*. Today only **OG6N** has one: it holds more (700,000 kg) while it is the depuration station than it does (400,000 kg) once its 3 mains become grow-out. | Rarely. |
| **Per-week system exceptions** (collapsed, advanced) | A cap for ONE unusual week — a shutdown, a trial, maintenance. Blank is the normal state. | Almost never. |

**Which weeks are which mode is derived**, not typed: a week is in `purge` mode
while its start date is before Control's `sixn_production_start`, and
`production` from that date on (and every week is production if *Run 6N as
grow-out* is on). Move that date and the capacity step follows it — the two
cannot disagree.

**Resolution order**, highest first:

```
per-week exception  >  system + mode default  >  system default  >  no cap at all
```

The last rung is real: a capacity nobody set stays unset. An engine that needs a
hard bound (the Global MILP / L3 placement passes) then **raises, naming the
missing input** — it will not substitute an invented ceiling. No capacity number
lives anywhere in the code.

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
co-pilot runs the planners forward from your window (**respect mode** — your
scripted transfers are never changed) and proposes the *next* week's operations:
**harvest + 6N staging from the validated controller** (pre-ticked — these are
the load-bearing, contract/cap moves) and an **optimised OG↔OG transfer plan from
the global optimiser** (ranked biggest-first, *opt-in* — the optimiser's first
week is often a full layout transition, so you tick only the moves you want).
Approve the ticked moves and they're appended as week N+1's operations, extending
your window by a week; run it again for the week after, and so on. Each run takes
**~20–30 seconds** (it runs both planners). The engine (`forecast/copilot.py`) is
UI-free by design, so this loop is portable to a future desktop build.

Both co-pilot buttons write `scenario/manual_events.yaml` — the same file the
forecast reads — so they follow the same **reject-at-entry** rule as *Save window*:
while any operation in your window shows ❌, Recommend and Approve are disabled
until you fix it. Recommendations are also tied to the window they were computed
from; edit or delete an operation (or upload a different PR) and the proposals
clear rather than letting you approve moves planned against a facility state that
no longer exists. If a save ever fails — `scenario/` is OneDrive-synced, so a sync
lock can win the race — you get an explicit error saying the operations are in your
window but **not** on disk, and *💾 Save window* retries it. *Planned
next (v1b): genuinely ranked transfer **options** side-by-side, each tagged by the
priority it serves (contract → caps → utilisation → transfers).*

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
  *(One benign known approximation: 6N depuration mortality is slightly
  under-counted in the per-tank continuity audit — within the 50-fish tolerance;
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

This is the answer to *"never an empty harvest week."* It is on by default because
without it the plain controller **leaves a totally empty week on 5 of 6 real PRs.**

**Why the reactive controller can't fix this itself.** Every lever it owns is a
`max()` — it can always harvest *more*, never less. So when a fat week arrives it
takes what it can, and the fish that would have carried a lean week three weeks
later are already gone. It cannot see the lean week coming because it only ever
looks at *now*.

**What the hybrid adds.** Before planning, it runs the Global engine's L1 stage —
a whole-horizon, tankless harvest envelope — and feeds that curve back into the
validated controller as a per-week target **band**. The floor half tells the
controller to harvest *at least* this much; the ceiling half, which is the part
that matters, tells it to harvest *at most* this much in the fat weeks, leaving
those fish in the water for the lean ones. The controller still does all the
actual planning; the guide only shapes how much it takes. It is a **request, not
a command** — the controller's own cap-shedding always wins, and the ceiling is
never allowed below `min_harvest_per_week`.

**Measured across 6 real July-2026 PRs** (2026-08-03, after the zero-harvest-week
metric fix):

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
`controller` and `controller-lns` entries on the Compare board are pinned `off`
so you can always see the two side by side.

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
| **WeeklyReport / MonthlyReport** | per-(batch, week/month) open/close ledger (count, weight, biomass, **Peak_Density**, SGR, feed, FCR, mortality, harvest, transfers, checks) | detailed batch accounting |
| **FeedForecastWeekly / Monthly** | feed by feed-type × period matrix | feed ordering |
| **Advisory** | per-week capacity table: biomass/feed vs caps + excess + OK/REDUCE | capacity headroom + over-cap weeks |
| **FacilityMap** | tank × week grid (cell = "Batch# AvgWt/Density"); **below it**: per-system × week **feed (kg/day)** and **biomass (kg)** blocks, each with a FACILITY total row | occupancy at a glance + per-system load vs caps |
| **BatchLocations** | per-(week, batch, tank) occupancy | raw realized placement |
| **ValidationLog** | numbered warnings (# / Category / Detail), incl. FW-calibration + bottleneck (annotated with resolution) and the **realized-plan** categories below | diagnostics — **read the `(realized plan)` categories first** |
| **InputConservationAudit** | per batch: placed/dropped, harvested, standing, **FW reconciliation** (planned vs realized seawater entry) + **closed FW mass-balance** (`first_FW_count` vs `realized_TranOG + FW_mort + FW_cull`; §6 #6) | conservation + FW calibration gaps |
| **TankContinuityAudit** | per-(tank, week) balance + **facility conservation summary** | 0-drift proof |
| **ReconciliationReport / SystemLimitsAudit** | per-batch open/close balance (count reconciles **exactly** via recorded realized biology; biomass within tolerance) / per-system realized biomass + feed vs cap, flagged `BIOMASS_OVER` / `FEED_OVER` | deeper audits — *TankContinuityAudit is the authoritative 0-drift biomass check* |
| **Diagnostics** | FW-calibration: per batch, the target vs projected pre-cull avg weight at TranOG, the residual, and a back-solved `Suggested_FW_Correction` | tuning `fw_correction` (§7 step 2) |
| **RunConfig** | the exact config + scenario embedded in the output | reproducibility |

> The `ProductionReport` sheet stays the **historical** input month only — the
> *forecast* is in the sheets above (same as the reference workbook). Skipped vs the
> reference: AccumulatedReport, AccumulatedOutput, MonthlyTargets, RunComparison.
>
> **`Peak_Density (kg/m³)` in the two ledgers** is the **worst tank** the batch
> occupied in that week/month (max over its tanks), not a mean — a batch normally
> sits in several tanks, and a mean would hide one over-cap tank inside a roomy
> average. Monthly = the max of the month's weekly peaks (a boundary week counts
> in both months). **Blank** means the batch held no tank that period (e.g. a
> freshwater week carried by the biology projection). Before 2026-08 this column
> was a literal `0` on every row.
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

> **`… (realized plan)` categories — judge the plan, not a pass inside it.**
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
underneath. A controller run emits ~200 such lines. Two things it can't tell you:
the placement walk itself is silent (it prints only when it finishes, so a long run
rests on its last line for a while), and the CP-SAT solver reports only at the end
of each solve. Neither is a hang — check CPU in Task Manager if in doubt.

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

**This is what §14 measures.** Every invariant above grades the tool against
itself. The *independent* check is your own ProductionReport: last month's
forecast made a prediction for a date, and this month's PR says what actually
happened on it. Grading one against the other is the only measurement here that
can call the growth model wrong — see **§14 Accuracy (forecast vs actuals)**.

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

- **In the app (recommended):** **Mode → Analyze**, then the *📊 Density quality*
  expander at the bottom of the board. It shows the peak-density distribution
  per candidate, the severe-batch list, and the gate's verdict. (This replaced
  the retired Tune mode — §13.1.) Reading rule: the gate counts batches at
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
| `density_overshoot` | per-tank density over-cap fraction, **OG6N purge excluded** (compliance, §7.3) | no breach |
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

> **OG6N counts on biomass now (2026-08-14).** The audit used to exempt the
> depuration system from *both* its caps, on the reasoning that purge is
> "intentionally uncapped". But that cap is an operator input in
> `limits.yaml`, and code ignoring an operator input is code overruling the
> operator — it hid a real breach: against a 400,000 kg cap the rotation was
> staging a **674,070 kg** peak, 68% over, on every run and visible on no
> sheet. So `BIOMASS_OVER` can now appear on OG6N rows, and 674 t still
> exceeds the 600 t the operator states 6N holds in purge. Its **feed** cap
> stays exempt for a physical reason rather than a policy one: purge fish are
> `STARVE` and eat nothing, so a feed-rate check on that system can only ever
> report 0.
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

- **What runs.** Controller (~30s), **Controller — hybrid** (~40s, the default
  planning method — §4.5), **Controller + LNS** (~30s) and the Global
  heuristic LP (~4 min) always; the
  optimal CP-SAT placement is a checkbox that is **on by default** — uncheck
  it for a fast two-method compare. The CP-SAT leg is quoted at ~30 min but gives
  **each week its own solver budget**, so the cost scales with your horizon: on a
  130-week PR it has been measured well past **90 minutes**, and it reports nothing
  until the whole solve is done. Watch the clock in the progress text, not the bar.
  It also can't use the full **Computer power** share you set — the per-week models
  are small (see §2) — so raising the slider won't rescue a long CP-SAT leg.
- **Suggested way to work.** Uncheck CP-SAT first and get the three fast methods
  graded in ~5 minutes; that's a complete, usable board. Then tick CP-SAT and run
  again if you want it — the finished legs are reused instantly, so you only pay for
  the new one, and if you interrupt that you lose only that leg. Compare this with
  running everything up front, where the long leg is the one blocking your first look
  at any result.
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
- **Stocking-for-quality frontier** (`Mode → Tune`, below the density-knob sweep). On a
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
> pick the **Global** plan on the board — it paces the whole horizon and holds the
> floor. The regression `test_no_harvest_craters` guards this invariant.

---

## 8. Key facts to remember
- The PR sets the **start**; the scenario sets the **batches + models**. Reuse the
  PR, change the models, to test scenarios.
- The harvest spike and the biomass overage are **two symptoms of one cause** — the
  stocking plan vs the facility's combined hold (cap) + process (55k/week) capacity.
  You can trade one for the other; eliminating both needs a stocking change.
- **The default planning method is the L1-guided hybrid (§4.5), not the plain
  controller.** The reactive controller leaves a *totally empty* harvest week on
  5 of 6 real PRs — it can only ever harvest *more*, never less, so it cannot save
  fish for a lean week it has no way of seeing. The hybrid gives it a forward
  harvest envelope to track. This is a hard contract rule, so it outranks the
  higher biomass peak the hybrid runs at.
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
# The app (Configure / Run / Analyze / Compare & Choose / Optimize /
#          Accuracy / How it works)
streamlit run app.py

# A single forecast, directly — prints the full stage-by-stage narration
python -m forecast.run --workbook Forecast.xlsm --output out.xlsm `
    --config-dir config --scenario-dir scenario

# NOTE: the command above runs whatever config/control.yaml says, which now means
# the L1-guided hybrid (§4.5). It has no --method flag; to run a specific method,
# either set hybrid_follow in the config or use Compare & Choose in the app.

# A GLOBAL whole-horizon method instead of the controller (§12) — writes
# <stem>_GLOBAL.xlsx; or pick it on the app's Compare & Choose board.
# Read §12's "What they do NOT do" before treating the output as executable.
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

## 12. The Global (whole-horizon) planning methods

Two of the five selectable methods (§7.4) are **Global**: instead of deciding
week by week like the controller (§4), they lay out the whole horizon up front.

| Method (as it appears on the board) | Placement layer |
|---|---|
| **Global — lexicographic LP** | a lexicographic linear program |
| **Global — CP-SAT optimal** | same front end; the grow-out tank layout is re-solved each week by an OR-Tools CP-SAT constraint solver |

Both share the same front end: an **L1 harvest envelope** (run the biology and
harvest just enough to hold the *true whole-facility* biomass — freshwater +
grow-out + the 6N purge backlog — under the cap every week, paced against the
2-week depuration lag), then **L3**, which decides how many tanks each batch
holds in each system each week, then a **specific-tank pick** that turns that
into physical tanks. `optimal=True` is the only difference: it replaces the
grow-out layout with the CP-SAT solve. Run them from the app's **Compare &
Choose** board, or headlessly via `tools/run_global_forecast.run_global(...)`.

### What they genuinely give you

- **Exact conservation.** Seeded == harvested + standing + mortality + cull;
  `TankContinuityAudit` shows 0 TANK_DRIFT. This is the same bar the controller
  passes, and it is real.
- **The whole-horizon view.** L1 sees fat weeks and lean weeks at once, which
  is precisely what a week-by-week controller cannot. That is valuable enough
  that the shipped default *borrows* it: the hybrid controller (§4.5) runs L1
  first and follows it as a target band.
- **One batch per tank**, structurally, in both arms.
- **Mode-aware 6N.** 33 production tanks in the purge era, 36 after the
  production start date — the three 6N *mains* become grow-out then, while the
  three *sisters* are harvest-staging in both modes and are never production
  tanks (`global_planner_l3_poc.production_tanks_per_system`).
- **The weekly harvest limit binds the 6N release.** A cohort whose release
  would exceed `max_harvest_per_week` is split pro-rata and the remainder
  deferred to the following week (`release_due_capped`).
- **CP-SAT: a real per-tank density cap.** Each tank's own
  `max_density_kg_m3 × volume_m3` from your facility config is a hard solver
  constraint, plus system-load balancing.

### What they do NOT do — read this before adopting a Global plan

These are not caveats about polish. They are the difference between a benchmark
and a plan the crew can execute.

- **They never read the handling budget.** `max_transfers_per_week` does not
  appear anywhere in the Global code path. They *minimise* moves in their
  objective, but nothing caps a week, so expect weeks well over the budget. The
  handling-budget gate in Analyze will show it — and that gate is soft, so it
  will not stop the plan being recommended.
- **They enforce only part of the tier rulebook.** The Global pick imports only
  `SIXN_SYSTEM`, `is_entry` and `move_allowed` from `forecast/tiers.py`. R2, R3
  and R4 are checked when a move is paired up, and CP-SAT respects R6. **R1, R5
  and R7 are not checked at all.** Worse, when no legal source exists for a move
  the pick **emits the move anyway** and logs a `TOPOLOGY VIOLATION` row — the
  controller would have refused it and left state unchanged.
- **The planning pass decomposes into independent weekly problems**, which is
  why week-to-week topology can break in the first place.
- **The LP arm has no per-tank density constraint.** It sizes tanks off a single
  facility-wide number — the *smallest* OG tank's legal mass × `density_target_pct`
  — and where a batch cannot get enough tanks the pick packs it denser and flags
  the row. Nothing rejects an over-cap tank. Only the CP-SAT arm constrains
  density per tank.
- **CP-SAT's infeasible-week fallback is a live path.** If a week cannot be
  solved, that week falls through to a placement with **no density test at
  all**, and the run writes `PLACEMENT DEGRADED — CP-SAT could not place N of M
  week(s)` to the ValidationLog. Seeding the solver with real starting occupancy
  took this to 0 weeks on the operator's PR — that is a *measurement*, not a
  guarantee; check for the row.
- **No gate fails a run for any of the above.** The Compare board's four badges
  are conserves / fully placed / no empty week / under cap. Topology violations,
  the placement gap, the CP-SAT degrade and the L3 solver warnings have **no
  gate at all** — they are ValidationLog text.

### Determinism

L3's Pass A.2 and Pass B use **proved-only** solves (a solve that hits its limit
is discarded and the previous pass's layout stands), and system symmetry is
broken so equivalent systems cannot swap between runs. Two honest exceptions:
Pass A.1 *does* use a limit-bound incumbent and only warns
(`NON-DETERMINISTIC SOLVE` in the ValidationLog, whose text says the run is not
reproducible), and CP-SAT accepts a FEASIBLE — not only OPTIMAL — solve, resting
reproducibility on a fixed seed plus a deterministic work budget.

### So which do I run?

Use a **Controller** method for a plan you intend to execute: the controller
family enforces R1-R7 while planning (an illegal move is refused, not logged),
respects the handling budget by deferring its optional quality passes, and routes
all harvest through 6N. Use **Global** to ask a different question — *how good
could this facility's layout be if handling and topology cost nothing?* — and to
read the L1 envelope, which is genuinely better information than the controller
can produce alone.

**Before adopting a Global plan**, open its workbook's **ValidationLog** and
search for `TOPOLOGY VIOLATION`, `DEPURATION HOLD`, `PLACEMENT GAP`,
`PLACEMENT DEGRADED`, `UNPLACED BATCH`, `NON-DETERMINISTIC SOLVE`,
`PASS A.2 FALLBACK` and `PASS B FALLBACK`, and check the handling-budget gate
in Analyze.

> **`PASS B FALLBACK`** (new 2026-08 — the category existed but nothing ever
> emitted it) means L3 could not *prove* the transfer-minimising solve on those
> weeks within the time limit and kept Pass A's layout. The plan is still legal
> and still reproducible, but those weeks carry **more transfers than L3's
> transfer count implies**. It names how many of how many weeks.

> **Two different sheets are both called `RunConfig`.** A controller run embeds
> the re-importable YAML dump; a Global run writes a **method stamp** headed
> "RUN CONFIG — GLOBAL METHOD EXPORT" — a record of what ran, with nothing to
> restore. **Configure → Import from workbook** now tells you which one it found
> and why there is nothing to import, instead of reporting a flat "not found".
> Import config from a controller run or from a config template.


## 13. Analyze mode — find my best plan (one flow)

The modes above each answer a PIECE of the real question — *which engine, with
which knobs, gives the best plan that passes the hard rules?* **Analyze** runs
that whole composition in one flow and ends in a single recommendation card:

1. **Engine round** — every planning method once on your current config (the
   same runs as Compare & Choose; finished legs are shared both ways, nothing
   runs twice). Global CP-SAT is an opt-in checkbox (slow).
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
     *"METHOD (tuned: knobs)"*. The Global methods have **no tunable knobs
     at all** (the only tunable knobs their path reads broke the Global
     conservation proof when overridden, so the registry refuses to put them
     in a search space — evidence in `forecast/methods.py`), so they compete
     at stock in both depths. A consequence worth knowing: a Global method
     that fails a hard rule is marked **gate-bound with no probe run** —
     there is no knob to try.
     Business constants (`min_harvest_weight_g`, stocking) and the
     operational rules (`max_harvest_per_week`, `harvest_relief_pct`,
     `min_harvest_per_week`, `max_transfers_per_week`) are **untunable by
     anyone** — the registry rejects a space that touches them. A **run budget** expander shows the
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
   read it before pressing Adopt. This matters most for the Global methods,
   which do not enforce R7 or the handling budget at all (§12).
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

### 13.1 Tune mode retired (2026-08-06)

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

## 14. Accuracy (forecast vs actuals) — grading the biology

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
model grew that batch **23 % faster than reality**. Those rewrites used to
scroll past in the run log and land in one workbook's `ValidationLog`, so a
correction needed every month for six months looked exactly like a one-off.

They are now appended to **`fw_calibration_history.jsonl`** at the repo root,
beside `optimize_history.jsonl` and `adoption_history.jsonl` (gitignored, and
written best-effort so a logging failure can never break a run). The mode reads
it back as a drift table. A batch flagged **persistent** — the applied
correction has sat away from the configured value across at least three runs —
is a **standing model error to fix in the biology config**, not to re-discover
every month.
