# Where things stand

Updated at the end of every working session (`/forecast-handoff`). Newest first in
each list. Last updated: **2026-10-22**.

## ▶ PICK UP HERE (2026-10-22): merge the RollingHarvestPlan Source Tank change

Built, tested, gated, NOT merged. Branch **`v2/rhp-source`** (one commit on top of the STATE
commit; worktree was in session c33340bb's scratchpad `wt_rhp_source` - if that folder is gone,
`git worktree prune`; the branch itself is safe in git). Report only - plan sheets identical.
- What: Source Tank on the week's first day row now (a) marks sorted moves `66(13% graded)`
  (graded-harvest pickups and sorter grades; density splits are not graded), (b) fills 6N tanks
  filled BEFORE the forecast from the PR's per-unit "Transfer in/out count in period" (exact
  count pairs first, the rest pro rata; fish already in the tank on the 1st have no source),
  (c) weights shares by fish harvested that week. App Harvest tab reads the same (app == sheet, 85 weeks).
- Live 10.18 before/after (only these cells change): W43 blank -> `34(61%)/41(20%)/33(19%)`;
  W44 blank -> `41(52%)/33(48%)`; W45 `43` -> `43(57%)/41(22%)/33(21%)`; W49 `66(13% graded)`;
  2028-W10 `51(2% graded)`; 2028-W18 `33(20% graded)`.
  Saved in `..\_backups\2026-10-22_rhp_source_review\before_after.txt` (+ rhp_cmp.py, run_wt.py).
- Checks: 6 new tests (all fail on old code), full suite 2503 passed / 111 skipped; gate 23/24
  plan-identical, no report changes; USER_GUIDE updated.
- Shown to the operator; waiting for their **yes**. Then: app closed (`netstat -ano | findstr :8501`)
  -> `git tag v2-before-rhp-source-2026-10-22 HEAD` -> back up forecast/production_report.py,
  rolling_harvest.py, excel_io.py, run.py, tests/test_rolling_harvest.py, docs/USER_GUIDE.md to
  `..\_backups\2026-10-22_before_rhp_source\` -> `git merge --ff-only v2/rhp-source` (never touch
  the uncommitted live config) -> re-run live 10.18 in scratch, compare with rhp_cmp.py -> STATE +
  memory -> operator pushes.

## Live setup (what the app runs right now)

- **Since 2026-10-22 (newest; supersedes the 10-21 set below):** set
  `..\_backups\scenario_sets\10.18 PR taper W44-45, 105-110t to Feb, Mar 120t\` = the 10-21 set +
  (1) taper: manual events 2026-10-18 changed - W43 43->63 36,500 (was 21,000), W44 harvest 61
  45,000 (36,000), W44 52->65 30,500 (15,000), W45 53->67 29,500 (14,000), W46 36->69 32,000 (13,500),
  W47 graded pickup 66->65 24,000 (5,000); limits W44 max fish 45,500 (42,500);
  (2) Q1 band: max_harvest_per_week rows 2026-W51..2027-W13 lowered to 35,900-40,900 (W02 38,800 =
  300 over the Q1 ramp, operator OK). Result: W43/44/45 = 314k/300k/275k lb HOG, W46..2027-W09 all
  105.1-109.9 t, March 2027 120-122 t, Apr-May 2027 137-165 t/wk; 2026 1,608 / 2027 6,471 / 2028 2,823 t;
  HOG floor 76/79; ceilings 4 (2026-W49 +1,021, 2027-W26, W38, 2028-W14); facility biomass never over.
  **105-110 t to end of March does NOT fit**: every version broke the 3,800 t facility biomass in
  2027-W12..W15 (peak up to 3,932 t) - operator chose band to end Feb, March ~120 t.
  Backups: `_backups\2026-10-22_before_taper_W44_W45\` (10-21 live), `_backups\2026-10-22_before_q1_band\`
  (taper only = set `10.18 PR taper W44 300k W45 275k, 105t floor`). Scratch: session 327f3cff `tune\`
  (runcand.py, band_solve.py, measure.py). Operator to run it in the app for the workbook.

- **Since 2026-10-21 (newest; supersedes the description below):** set
  `..\_backups\scenario_sets\10.18 PR all 10-18 growth, 105t floor, Q4 1575\` - all batches on
  Actual 2026-10-18, 105 t HOG floor (control + limits rows 2026-W46..2027-W05), W51-53 max fish
  +7% (41,999 / 42,142 / 38,760), manual W46 36->69 13,500, W48 32->61 39,000. Q4 2026 1,575 t,
  2027 6,540 t, 79/79 floor weeks. Operator ran it in the app (matched). Operator's workbook:
  `Downloads\10.18.26_PR_planned_10-18 growth 105-110t Q4 1575t.xlsm` (to move to `2026\Oct\`).
  1,600 t studied and NOT recommended (+20% W51-53: 7 ceiling breaches, ~70 t HOG-equiv worse overall).
  Code = `d57c40c` (+ this STATE commit), pushed.

- Branch `v2/capacity-and-transition`, code = `a5cdd88` (growth models steps 1-4, harvest floor
  in kg HOG, RollingHarvestPlan sales weight ranges P-W + year totals, Daily Harvest Schedule
  retired; 2026-10-19; **Models** sheet + leave batches out of the growth fit + BatchInputs growth adjustment in words + set several batches at once + SGR check table 2026-10-20). RollingHarvestPlan: whole forecast (`rolling_harvest_weeks: 0`), IDs from 978.
  Live limits now also carry the Q1 ramp (2027 max fish 38.5k W01-05 / 40k / 42k / 44k / 45k W18+,
  100 t floor W01-05); set saved as `scenario_sets\10.18 PR manual to W48 + Q1 ramp\`; operator manual
  entries to W48. 2027 = 6,454 t (week sum). Run name in `config/control.yaml`: "Sep Production Report Forecast" (stale label).
- Latest input: the **10.18.26 PR** (`Reports\2026\Oct\10.18.26 PR.xlsx`, mid-month, closing 2026-10-18).
- Live config = scenario set **`..\_backups\scenario_sets\10.18 PR steady 100t Nov-Dec, 110t 2027, 3400g, Actual growth\`**
  ("steady", operator 2026-10-19: lower harvest now to grow size, then a 110 t HOG/wk floor).
  - Growth: every batch `growth_model: Actual 2026-10-15` (measured, `sgr_sw_by_model` in
    biology.yaml), `sgr_correction` 1.0. Walk-forward error -0.7..+1.0% (base curve ran +3.7..+4.9% hot).
  - `control.yaml`: min_harvest_weight_g 3,400, **min_harvest_hog_kg_per_week 110,000** (new knob).
  - `limits.yaml`: Nov-Dec `min_harvest_hog_kg` rows 100 t + max bands (floor +3%); fill min-weight
    rows 3,100 g W51-W53, 3,300 g 2027-W01..W08; 2027 bands 26-55k fish.
  - `manual_events/2026-10-18.yaml`: B50 fw_to_og W43; 43->63 38,000 W43, rest of 43->71 W44;
    52->65, 53->61 W45; harvests 65, 71, 61, 63.
  - Result: Nov/Dec/Jan/Feb/Mar 459/483/502/497/634 t HOG; 2027 6,340 t, 6.59 lb/fish; peak 3,683 t.
    ValidationLog: 72 of 82 floor weeks met; 2027 short W03 W12 W21-23 W27 W31 by 0.1-4.3 t and
    **W42 79 t**; 2028-W17 98 t. Sales size rules pass 25 of 60 weeks (base-curve plan 50) - growth
    is the measured one, so the 8 lb+ rule is the open question for sales.
- Previous live set (10.15 PR "stable") is still in `scenario_sets\10.15 PR 130t-wk, Dec 125t, B44 out W47\`;
  backups of the replaced live files: `..\_backups\2026-10-19_before_hog_floor\live\`.

- **Alternative saved and run**: `scenario_sets\10.15 PR aggressive Q4 (S1 rows, fish-matched)\`
  = stable set + the 10.11 S1-rebuild harvest rows from 2026-W42 (54–60k fish/wk Nov–Dec,
  min weight 2,950–3,100 g) + control min weight 3,100 g + 2027-W04/W05 floors −5,500 each.
  Workbook `Forecasts\2026\Oct\10.15.26_PR_planned_aggressive Q4 fish-matched.xlsm`
  (= scratch exactly). Q4 674k / 1,931 t / 6.32 lb; Q1 452k / 1,360 t / 6.63 lb; Dec 730 t;
  2027 6,622 t; end-Dec standing 3,273 t; W48–W53 at 56–60k fish (S1's own 60k rows).
  Oct–Mar fish 1,126k vs stable 1,118k — **8k is the closest reachable** (12 trims tried;
  whole-tank 6N drains move the fish to other weeks). Same fish, stable sells 81 t more HOG.
- `config/costs.yaml` feed prices fixed 2026-10-10 (Price/MT ÷ 1000).

## Waiting on the operator

- Yes/no on merging `v2/rhp-source` (see PICK UP HERE).
- Push after any commit: `! git push origin v2/capacity-and-transition` (origin at d57c40c 2026-10-21).
- Optional, offered: 10 harvests sit 3 weeks in 6N (make_room fills; planner rule change);
  586-fish graded harvest 2028-W10; W46/W47 manual weeks over the Nov fish limits by 611/1,342.

## In progress: learning growth model (agreed plan 2026-10-18)

Measured (scratch, read-only): fish under ~2.5 kg grow ~on the base curve; above 2 kg
they gain a flat ~14 g/day (curve says 15-21) -> ~0.71-0.77 of curve at 3-4 kg. 2026
grew slower than 2025 (0.84 vs 0.91 of curve). Hindcast May-Oct 2026: model B (AGR
power law, capped above 2 kg) best at 1/2/3 months (+0.6/+1.8/+2.8%). Stable 10.15 plan
under B: Jan 2027 falls to ~214 t, Q1 biomass > 3,800 t cap (needs a Dec-Jan min-weight
retune). Acc tabs: "closing better" = harvest weight (2.0 -> 3.6-4.2 kg/fish) + econ FCR
(~2 -> 1.27-1.54), NOT growth rate per size (B44-B48 0.92-1.00 vs B38-B43 1.03-1.14).
Operator decisions: rolling **24-month window** (full life cycle), history from **Nov 2024**;
full monthly reports (have Acc) supersede mid-month PRs (PR sheet only, provisional,
weighted by days); per batch BOTH fleet-only and fleet x own-history (shrunk to 1.0);
improvement BOTH % per year and blend-to-target, labelled ASSUMED everywhere; FCR learning
on the same machinery. Each step in its own sandbox + gate + approval.
1. DONE `b41ab98` engine: biology.yaml `sgr_sw_by_model` + batches.yaml `growth_model`
   (blank = Base curve, plan-identical: gate 23/24, live set identical but BatchInputs column).
2. DONE `4328b4b` forecast/growth_history.py + `python -m tools.growth_history --store <file>`
   backfill/add/show/fit (frozen versions). Scratch backfill: 38 reports, 23-report chain
   Nov 2024..2026-10-15, 370 clean tank-months; fit cap 13.9 g/d above 2 kg; holdout last 3 mo
   fit +1.0% vs base +3.1%. 22 files unreadable (2024 formats, Jan'25, Nov'25 V2, May'26 prelim).
   The live `growth_history.json` (repo root, gitignored) is NOT built yet - operator's call.
3. DONE `4fa29a0`..`8b8d5a3` (tag `v2-before-growth-app-2026-10-18`): every operational run
   adds its PR to `growth_history.json` (same rule as the calibration log; never fails a run;
   test runs with `calib_log_path=""` record nothing). Configure -> Biology models panel: scan
   Reports (from Nov 2024), add one report, Fit (preview + holdout), Save frozen version, Add a
   version to biology.yaml (biology only), before/after on a PR (copies). Batches tab:
   `growth_model` dropdown + "What each batch grows on". Gate 23/24, live set identical
   (31 sheets), suite 2454 passed / 0 failed.
   NEXT for the operator: Scan in the app (or ask for the backfill) -> Fit -> Save.
4. DONE `c55ba1a`..`a1c8924` (tag `v2-before-growth-improve-2026-10-19`): ASSUMED versions
   (+%/yr or straight-line blend to a target curve by a date, made for fish growing in a
   stated month, frozen, adoptable like a measured one) + per-batch own-history table
   (INFORMATION ONLY - operator 2026-10-19: the size slowdown is facility-wide and already
   in the measured version, so fleet only, sgr_correction 1.0, is the usual choice).
   Measured by size (Nov24-Oct26): actual/base 1.02 at 0.5-1 kg, ~0.89 at 1-2.5 kg,
   0.76 at 2.5-3, 0.70 at 3-4 kg; g/day flat 13.4-14.5 from 2-4 kg (no further decline;
   >4 kg only 1 tank-month). Gate 23/24, live set identical, suite 2466 passed.
5. FCR learning.
6. Adoption (before/after, corpus, scenario retune) - operator's call.
Scratch analysis: session c33340bb scratchpad `growth\` (measure.py, fit.py, chains.py, runs\).

## Open items (offered, not started)

- Neither 10.15 plan has 8 lb+ tanks in Q1 2027 (stable 0 t; aggressive ~100 t Feb–Mar
  only). If the ≥45k lb/wk 8 lb+ sales rule must hold in Q1: test higher min weights on
  the Dec–Feb fill weeks in scratch and price the tonnes lost.
- Run the Sep'26 full monthly production report (Acc tab → BatchAccumulated).
- Rename leftovers: `10.11.26_PR_planned_600t Nov-Dec 2-tank 6N.xlsm.xlsm`,
  `10.11.26_PR_planned_S1 rebuild opt1 2-tank..xlsm` (offered, not asked).
- 2027 weekly sales floor (~260k lb/wk, scratch `G55_260_i2` on the 10.11 PR).
- Overdue 6N drains break the weekly fish limit (55–66k, e.g. 2027-W03 56.5k stable) — engine timing.
- B55 eggs: FishTalk 550,000 vs `scenario/batches.yaml` 570,000 — which is right?
- Growth model runs hot (+3–5% weight; forecast FCR ~1.17 vs actual ~1.4): recalibrate
  SGR/FCR tables from Acc-tab lifetime actuals (plan-changing → sandbox + approval).
- Saved Scenario 1 / 2 workbooks still carry the old, inflated feed costs — re-run them.
- Align WeeklyReport/MonthlyReport SGR (compound), Econ FCR (net of culls), SFR with
  FishTalk definitions (report-only).
- Target solver as library code; a size-class report (lb by sales band per week).
- TranOG date inside the manual window: clearer app message. Manual-preview 6N week nit.

## Recent history

- 2026-10-21 `a5cdd88` **Growth checked against the data**: SGR %/day by fish size (500 g bands) -
  measured, model, 100% curve (biology.yaml `sgr_sw_pct_day`), model and measured as % of 100% -
  under the Fit preview, under each saved version (its window as the history reads now + tank-months
  after its as-of, which fills in as PRs are added) and in the Models sheet. Report only. Tag
  `v2-before-growth-check-2026-10-20`; gate 23/24, suite 2497 passed; live 10.18 plan identical.
  2026 data: measured ~100-105% of the 100% curve below 1 kg, 88% at 1.5-2 kg, ~70% at 3.5-4 kg.
  Operator ruling: the base curve numbers (Aquasim ASUS column; 4.5-5 kg rows x0.80) stay as they are.
- 2026-10-20 `5945a57` Batches tab **Set several batches at once**: From/To batch, seawater growth
  model, SW offset (sgr_correction), FW offset (fw_correction), Apply then Save Batches. Tag
  `v2-before-batch-bulk-2026-10-20`; gate 23/24, suite 2490 passed, live 10.18 plan identical;
  clicked end to end in Chrome on a copy. Operator's intended split: batches with fish (B43-B57) on
  Actual 2026-10-18 (9-month 2026 fit), future (B58+) on Base curve x 0.86 - not done live yet.
- 2026-10-20 `cc5ad8b` app click test (leave out, Fit, Save, Add to biology.yaml on a copy of the app
  tree). `e4d440b` BatchInputs column 'Growth adjustment (SW)' in words (was a bare sgr_correction 1);
  the app's 'added to biology.yaml' confirmation now survives the rerun. Tag
  `v2-before-sgr-words-2026-10-20`; gate 23/24 (no report changes), suite 2486 passed, live 10.18
  plan identical.
- 2026-10-20 `0afb391`..`ec49cc1` leave chosen batches out of the actual growth fit (app Fit picker,
  CLI `fit --exclude`, frozen version records them, Models sheet names them; CLI warns on an unknown
  batch). Tag `v2-before-growth-exclude-2026-10-20`; gate 23/24, suite 2484 passed, 15 recent PRs
  (8.13-10.18) old vs new identical but time stamps, live 10.18 plan identical.
  Operator wants the Actual model from 2026 only: Fit as of 2026-10-18, window 9 months (starts at
  the Dec-31-2025 closing; 190 tank-months, 13.7 g/d above 3 kg vs 13.9 live) - not yet saved.
- 2026-10-20 steady-state study (scratch, clean slate, Actual growth, 310k to OG every 49 d =
  44k fish/wk): 130 t HOG/wk needs ~3.7 kg harvest (min wt 3,500 g) and ~3,700 t mean standing,
  peaks ~3,900 t, density breach 60-80 tank-wk/yr; at 3,400 g it settles ~124-129 t on ~3,500 t.
  Scripts: session c33340bb scratchpad `ss130_tools\`.
- 2026-10-20 `7c82b51` Models sheet (report only): growth models per batch (measured vs assumed,
  adjustment in words, not a bare 1), curves by size, FCR tables, other assumptions. Tag
  `v2-before-models-tab-2026-10-20`; gate 23/24, suite 2481 passed, live 10.18 plan identical.
  Offered: same wording for BatchInputs "SGR correction (SW)".
- 2026-10-18 growth history step 2 merged (`4328b4b`..`43f9974`), tag
  `v2-before-growth-history-2026-10-18`; two more tests stopped copying the live costs.
- 2026-10-18 growth models step 1 merged (`b41ab98`..`edede1d`), tag
  `v2-before-growth-models-2026-10-18`; test fix: BatchAccumulated test no longer copies costs.
- 2026-10-17 10.15 PR: aggressive-Q4 set built, fish-matched, written live, operator ran it
  (verified), then live restored to the stable set (verified). Tags
  `v2-before-aggressive-q4-2026-10-17`, `v2-before-back-to-stable-2026-10-17`.
- 2026-10-16 live → 10.15 PR stable set (130t-wk, Dec 125t, B44 out W47); earlier that day
  10.11 PR 580 t/month Oct–Mar sets (saved, superseded).
- 2026-10-16 `dd21e45` 6N top-up after a make-room dump (pair filled to the band).
- 2026-10-15 `1934803`..`649f07a` RollingHarvestPlan sheet + app Harvest tab (TSV/copy).
- 2026-10-15 `d900c70`/`89e7e4a` opt-in 6N source limit `sixn_max_source_tanks`.
- 2026-10-15 10.11 PR Scenario 1 rebuild option 1 (642/723 t Nov/Dec, 3,100 g) — the
  source of the aggressive rows.
- 2026-10-14 10.11 PR 320k lb/wk 2026 + 55k facility limit + month-smoothed 2027.
- 2026-10-11 `5f7e30a` lifetime per batch (BatchAccumulated + Monthly) from the Acc tab.

Rollback tags: `git tag --list "v2-*"`. Backups: `..\_backups\`.
