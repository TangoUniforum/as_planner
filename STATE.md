# Where things stand

Updated at the end of every working session (`/forecast-handoff`). Newest first in
each list. Last updated: **2026-10-14**.

## Live setup (what the app runs right now)

- Branch `v2/capacity-and-transition`, HEAD = this handoff commit (code = `5f7e30a`).
  Run name in `config/control.yaml`: "Sep Production Report Forecast".
- Latest input: the **10.11.26 PR** (`Reports\2026\Oct\10.11.26_PR.xlsx`, mid-month,
  closing 2026-10-11). Operator's verified run: `Downloads\10.11.26_PR_planned (1).xlsm`
  (17:51, to be renamed `…_320k W53 325k smoothed 2027.xlsm`) = scratch exactly.
- Live config = scenario set **`..\_backups\scenario_sets\10.11 PR 320k lb 2026 W53 325k +
  smoothed 2027 (55k)\`** (control + limits + manual events identical).
  - `control.yaml`: **max_harvest_per_week 55,000** (operator ruling 2026-10-14: the 2027
    facility limit is 55k fish/wk; was 42,500 in the working copy, 55k in the committed one).
    Other facility limits unchanged: biomass 3,800 t (FW-inclusive), feed 33,000 kg/d.
  - `manual_events/2026-10-11.yaml`: operator's 5 harvests (W1: 63, 61, 67; W2: 65, 71) +
    B50 fw_to_og W1 252,412 → 13 big / 22 small (REQUIRED — without it the run errors,
    B50's TranOG falls inside the manual window) + og_to_6n W1 41→61 36,000, 33→67 whole,
    W2 43→63 38,000, 53→69 8,000.
  - `limits.yaml`: 2026 W44–W53 weekly bands solved to ~320k lb HOG/wk (W53 325k); min
    weight 3,000 g on fill weeks W46–W50 (smaller fish allowed in harvest W48–W52),
    3,600 g W51–W52; 2027 per-week caps/floors (≤55k) shaping months to the operator's
    moves (Mar→Feb/Apr, May→Apr/Jun, Aug→Jul, Dec→Nov). No rows after 2027.
  - Result: 2026 W44–W53 = 308/302/326/323/314/316/316/324/301/325k lb; Oct/Nov/Dec
    644/602/659 t; 2027 months 430/457/566/602/576/613/620/598/564/511/615/608 t (6,760 t);
    2028 H1 2,817 t. Biomass max 3,759 t, feed max 30,401. **2027-W37 = 59,869 fish**
    (overdue 6N drain, inside the 10% relief) — operator chose this (option A) over the
    strict-limit option B (`scratch SW_i2`, lumpier Jul/Aug/Oct).
  - Size rules (operator's sales needs, lb HOG/week: ≥45k of 8 lb+, ≤80k under 5.8 lb,
    none under 4 lb): broken W48–W52 2026 (accepted) and ~12 weeks of 2027 on 8 lb+.
- `config/costs.yaml` feed prices fixed 2026-10-10 (Price/MT ÷ 1000).

## Waiting on the operator

- Push this handoff commit: `! git push origin v2/capacity-and-transition`.
- Rename the latest workbook (see above).

## Open items (offered, not started)

- 2027 **weekly sales floor**: scratch shows ~260k lb/wk holds in 48/52 weeks with a 3,600 g
  min weight + per-week fish floors under the 55k limit and 0 facility breaches
  (scratch `G55_260_i2`); combine with the monthly shaping + fix the 2027 8 lb+ weeks.
- Shareable sales page (floor, monthly profile, size check).
- Overdue 6N drains break the weekly fish limit (55–66k) — engine timing; worth a look.
- B55 eggs: FishTalk 550,000 vs `scenario/batches.yaml` 570,000 — which is right?
- Growth model runs hot (+3–5% weight; forecast FCR ~1.17 vs actual ~1.4): recalibrate
  SGR/FCR tables from Acc-tab lifetime actuals (plan-changing → sandbox + approval).
- Saved Scenario 1 / 2 workbooks still carry the old, inflated feed costs — re-run them.
- Align WeeklyReport/MonthlyReport SGR (compound), Econ FCR (net of culls), SFR with
  FishTalk definitions (report-only).
- Target solver as library code (`forecast/target_solver.py`); a size-class report
  (lb by sales band per week) would make the size rules visible in the workbook.
- TranOG date inside the manual window drops an incoming batch silently — the engine now
  errors instead; a clearer message in the app would help.
- Manual-preview 6N week mismatch (handoff week vs hold-end week) — nit.

## Recent history

- 2026-10-14 live writes on the 10.11.26 PR (all backed up + tagged, re-run verified):
  45–48k weekly → Nov/Dec 630 t → 2027 leftover rows (156, from the Oct scenarios)
  removed → 45–48k with 2027 facility-only → **320k lb/wk 2026 + 55k facility limit +
  month-smoothed 2027** → W53 trimmed to 325k (live). Tags `v2-before-*-2026-10-14`.
- 2026-10-12 10.11.26 PR tuned to Oct/Nov/Dec 590/600/600 t (superseded).
- 2026-10-11 `5f7e30a` lifetime per batch (BatchAccumulated + Monthly) from the Acc tab.
- 2026-10-10 `a5503c5` mid-month PR months whole on HarvestReport / FeedForecastMonthly /
  YearlySummary; eggs stocked on the PR closing day booked whole.
- 2026-10-10 Scenario 2 small-fish change (vK) written live; costs.yaml price fix.
- 2026-10-09 move-size rules merged; Scenario 1 and 2 rebuilt on the new code.

Rollback tags: `git tag --list "v2-*"`. Backups: `..\_backups\`.
