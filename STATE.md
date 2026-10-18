# Where things stand

Updated at the end of every working session (`/forecast-handoff`). Newest first in
each list. Last updated: **2026-10-17**.

## Live setup (what the app runs right now)

- Branch `v2/capacity-and-transition`, HEAD = this handoff commit (code = `dd21e45`).
  Run name in `config/control.yaml`: "Sep Production Report Forecast" (stale label, harmless).
- Latest input: the **10.15.26 PR** (`Reports\2026\Oct\10.15.26_PR.xlsx`, mid-month,
  closing 2026-10-15). Not yet used: `Reports\2026\Sep\Sep'26 Production Report.xlsx`
  (full monthly report, 2026-10-16).
- Live config = scenario set **`..\_backups\scenario_sets\10.15 PR 130t-wk, Dec 125t, B44 out W47\`**
  ("stable"; control + limits + manual events identical). Operator's workbook:
  `Forecasts\2026\Oct\10.15.26_PR_planned_stable 130t-wk Dec125.xlsm` (= scratch exactly).
  - `control.yaml`: max_harvest_per_week 55,000, min_harvest_weight_g 3,500, biomass
    3,800 t, feed 33,000 kg/d, `sixn_max_source_tanks: 2` (≤2 OG tanks fill a 6N pair/week).
  - `manual_events/2026-10-15.yaml`: operator's weeks 1–4 (2026-W42..W45): harvests
    63 (W1), 65+71 (W2), 61 (W3), 67+63 (W4); og_to_6n 41→61, 33→67 (W1), 43→63 32,000 (W2),
    52→65, 43→71 (W3), 53→61 (W4); B50 fw_to_og W1 252,205 → 13 big / 23+22 small.
  - `limits.yaml`: 2026 W44–W53 bands 36–50k fish, min weight 3,000 g (3,600 g W51–W52);
    2027 per-week bands ~35–47k fish all year.
  - Result: Q4 2026 598k fish / 1,746 t / 6.44 lb HOG; Q1 2027 520k / 1,625 t / 6.90 lb;
    Nov/Dec/Jan/Feb/Mar 539/580/538/533/554 t; 2027 7,037 t; end-Dec standing 3,540 t.
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

- Push `dd21e45` + this handoff commit: `! git push origin v2/capacity-and-transition`.

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
