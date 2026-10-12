# Where things stand

Updated at the end of every working session (`/forecast-handoff`). Newest first in
each list. Last updated: **2026-10-11**.

## Live setup (what the app runs right now)

- Branch `v2/capacity-and-transition`, HEAD = the docs commit after `5f7e30a` (code = `5f7e30a`).
- Live config = the **"Oct Scenario 2"** set (limits identical; control identical apart
  from the run name), renamed by the operator to **"Sep Production Report Forecast"**,
  with `scenario/manual_events/2026-09-30.yaml` **cleared** (events: []). The real
  Scenario 2 events are kept in `..\_backups\scenario_sets\Oct Scenario 2\` and in the
  saved workbook.
- Latest input: the full **Sep'26 Production Report** (has an Acc tab) →
  operator's run `Downloads\Sep'26 Production Report_planned.xlsm`, validated 2026-10-11:
  lifetime sheets tie to Acc + MonthlyReport; 2026 / 2027 / 2028 =
  523,943 / 2,237,524 / 769,686 fish harvested.
- `config/costs.yaml` feed prices fixed 2026-10-10 (Price/MT ÷ 1000).

## Waiting on the operator

- **Push** `a5503c5`, `5f7e30a` and the CLAUDE.md/STATE.md docs commit: `git push origin v2/capacity-and-transition`.

## Open items (offered, not started)

- B55 eggs: FishTalk 550,000 vs `scenario/batches.yaml` 570,000 — which is right?
- Saved Scenario 1 / 2 workbooks still carry the old, inflated feed costs — re-run them.
- Growth model runs hot (+3–5% weight; forecast FCR ~1.17 vs actual ~1.4): recalibrate
  SGR/FCR tables from Acc-tab lifetime actuals (plan-changing → sandbox + approval).
- Align WeeklyReport/MonthlyReport SGR (compound), Econ FCR (net of culls), SFR with
  FishTalk definitions (report-only).
- Move-size: 52 legs < 7k in the Sep'26 run (mostly relief / split legs); option to
  tighten plan-ahead relief and 6N carry-overs (sandbox before/after).
- Target solver as library code (`forecast/target_solver.py`) — not started.
- TranOG date inside the manual window drops an incoming batch silently — add a warning.
- Manual-preview 6N week mismatch (handoff week vs hold-end week) — nit.

## Recent history

- 2026-10-11 `5f7e30a` lifetime per batch (BatchAccumulated + Monthly) from the Acc tab.
- 2026-10-10 `a5503c5` mid-month PR months whole on HarvestReport / FeedForecastMonthly /
  YearlySummary; eggs stocked on the PR closing day booked whole.
- 2026-10-10 Scenario 2 small-fish change (vK) written live; costs.yaml price fix.
- 2026-10-09 move-size rules merged; Scenario 1 and 2 rebuilt on the new code.

Rollback tags: `git tag --list "v2-*"`. Backups: `..\_backups\`.
