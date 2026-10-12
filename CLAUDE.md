# Atlantic Sapphire production forecast tool — working notes for Claude

Read this first, then `STATE.md` (where things stand right now). The operator is
Julian F (production planning, salmon RAS). They run the app and own the live
config; you build, test and explain. Full user documentation: `docs/USER_GUIDE.md`
(keep it in sync when a knob, report or rule changes).

## Standing rules (operator rulings — never bend these)

1. **V2 is additive only.** Add functionality; never damage what works. Keep it simple.
2. **Reversible first.** Before any merge or live-file write: a rollback tag
   (`git tag v2-before-<what>-<date> HEAD`) and a backup copy in
   `..\_backups\<date>_before_<what>\` of every live file you will touch.
3. **Numbers change only with approval.** Anything that changes plan or report
   numbers: build in a sandbox, show the operator before/after on real PRs, merge
   only after they say yes. Report-only fixes still get a before/after.
4. **The app must be closed** before you merge or write live files
   (check: nothing listening on port 8501 — `netstat -ano | findstr :8501`).
5. **You cannot push.** The operator runs `git push origin v2/capacity-and-transition`
   themselves (`! git push origin v2/capacity-and-transition` from the Claude prompt).
6. **Test runs never write the calibration log.** Scratch/corpus runs call
   `forecast.run.main(pr, out, config_dir=..., scenario_dir=..., calib_log_path="")`
   in-process on COPIES of config/scenario, never `python -m forecast.run` and
   never against the live `config/` or `scenario/` folders.
7. **Untrusted files** (downloads, PRs the operator sends): copy into their own new
   folder in the scratchpad and read with `python -I`.
8. **Follow the pipeline's constraints** — don't invent ramps/defaults; an honest
   "nothing meets the limits" beats a bent rule. Every new check ships with a
   negative control (a test proving it can fail).
9. Judge a knob or method on the **whole PR corpus** (`python -m tools.knob_corpus`),
   never one PR — the planner is mode-discontinuous.
10. Explain in plain language with the numbers that matter; the operator decides.

## Vocabulary

- **PR** = FishTalk production report (the starting state). The system is
  **FishTalk** (not FIIZK); manual: `Desktop\Fishtalk_UserGuide.pdf` (glossary p.452–453).
  A **mid-month PR** merges month-to-date actuals into the monthly reports
  (CostsAndProfit stays forecast-only). A **full monthly production report** has an
  **Acc** tab (lifetime per fish group) → the run also writes BatchAccumulated(+Monthly).
- **HOG** = head-on gutted = gross × 0.81 yield. Weekly targets are usually in lb HOG.
- Tanks: FW → OG1/2 (entry) → OG3-6 (grow-out) → **6N** purge/harvest tanks
  (pairs 61/67, 63/69, 65/71). TranOG = FW→OG move (a move, not an input; ledger
  input = eggs only). 6N fish are harvested exactly 2 weeks after transfer in.
- **Move-size rules** (`move_size_rules: true` in control): partial moves ≥ 7,000 fish;
  6N carry-over pieces ≥ 3,000; relief from an over-cap tank is the only exception.
- Floor beats cap (never hold a 6N tank if the week falls below the harvest floor).
- `scenario/limits.yaml` per-week rows (harvest band, min harvest weight …) override
  Control defaults; weeks without rows silently use Control.

## Where things live

| What | Where |
|---|---|
| App (Streamlit, port 8501) | `app.py`; launcher `RUN_V2.cmd` / desktop "AS Forecast V2" |
| Run entry point | `forecast/run.py` (`main`) |
| Growth / feed / mortality | `forecast/biology.py`, `forecast/models.py`, tables in `config/biology.yaml` |
| Placement, moves, density, 6N | `forecast/placement.py`, `forecast/sixn.py`, `forecast/tiers.py` |
| Harvest | `forecast/harvest_scheduler.py`, `forecast/harvest_plan.py` |
| PR reading | `forecast/production_report.py`, `forecast/pr_state.py` |
| Manual starting events | `forecast/manual_events.py`; file `scenario/manual_events/<PR closing>.yaml` |
| Workbook reports | `forecast/excel_io.py`, `forecast/costs_report.py`, `forecast/accumulated.py` |
| Costs | `forecast/costs.py`, `config/costs.yaml` (gitignored, commercially sensitive) |
| Live operator config | `config/control.yaml`, `scenario/limits.yaml`, `scenario/batches.yaml`, `scenario/manual_events/` (uncommitted edits are the operator's — never commit or overwrite them without asking) |
| Saved scenario sets | `..\_backups\scenario_sets\<name>\` (control.yaml, limits.yaml, manual_events_<date>.yaml) |
| Backups | `..\_backups\<date>_before_<what>\` |
| PR corpus (gate) | `pr_corpus\*.xlsx` (+ `pr_corpus\registries\` for the 4 oldest) |
| Test fixture PR | `tests/fixtures/pr_2026-09-30.xlsx` |
| Operator's reports | `..\..\..\Reports\<year>\<Mon>\` ; forecast workbooks `..\..\<year>\<Mon>\` |
| Other trees | `..\Python-v1` (frozen V1 fallback), `..\Python-sandbox` (old) |

## How to check work

- Full suite: `python -m pytest -q` (~26 min; run in the background). Known: tests
  needing `Forecast.xlsm` skip outside the live tree.
- Plan-identity gate for code changes: `.claude/skills/forecast-change/gate_old_vs_new.py`
  (corpus + saved scenario sets, old code vs new code, plan sheets hashed).
  `2025-07-31` always fails on both arms — that is expected.
- Knob/method changes: `python -m tools.knob_corpus --set name=value`.
- Hitting monthly targets: `python -m tools.solve_targets --pr <PR>` (writes nothing).

## Skills (type these at the prompt)

- `/forecast` — start of a session: load state, check git + live files, ask what's next.
- `/forecast-tune` — tailor a scenario (limits, bands, manual events) safely.
- `/forecast-change` — change the code: sandbox, gates, approval, merge.
- `/forecast-handoff` — end of a session: update `STATE.md` and memory.
