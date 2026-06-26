# Deployment-Readiness Audit — Production Controller Forecast Pipeline

**Date:** 2026-06-25
**Scope:** the production controller (`forecast/run.py` and the modules it calls). The experimental CP-SAT / global method (`*_poc.py`, `global_*.py`) is **out of scope** for this rollout.
**Method:** 37-agent multi-agent audit — 6 subsystem reviewers (conservation, cap math, 6N depuration-hold, biology, harvest+placement, reports), each finding adversarially re-verified by a second agent, then synthesized. 30 raw findings, **29 confirmed after adversarial verification**.

---

## 1. Recommendation

**GO-WITH-FIXES** — The realized closed-loop controller is conservation-sound and enforces the 3.8M cap correctly *in execution* (6N depuration biomass is counted; fish are not leaked or double-counted). However, the **binding setpoint and every operator-facing cap report omit FW biomass** (~4–7%, ~150–265k kg), so the primary compliance numbers an operator reads can show "OK" while true facility biomass is at/over cap. Ship only after the FW-in-cap reporting/setpoint cluster is fixed and the stale pin/lookahead documentation traps are removed.

> **Resolution status (updated 2026-06-25, branch `feature/closed-loop-harvest`, pending operator sign-off):**
> - **H1 FIXED** — binding setpoint now FW-inclusive (`placement.py`); weeks over true cap 36 → 0 after adding FW anticipation; tonnage 8.50M → 8.21M (over-production correction).
> - **H2 FIXED** — Advisory / YearlySummary / FacilityMap now report FW-inclusive biomass.
> - **I2 FIXED** — closed FW-phase mass-balance gate added to `InputConservationAudit` + `test_fw_mass_balance`. Confirmed B49/B50 are *not* dropped (the apparent gap was the pre-horizon egg phase).
> - **H3 / H4 / L7 FIXED** — false pin "honored" claims corrected, dead demand dicts removed, vestigial lookahead knob marked inactive.
> - **M3 FIXED** — feed/day is now a hard dual-limit with biomass (whichever binds first), within the single deviation-band buffer. The feed-implied ceiling no longer counts off-feed STARVE biomass in its ratio; Advisory feed is FW-inclusive. On the live config biomass binds first (98.4%), feed has headroom (88.4%), 0 weeks over either.
> - **REMAINING:** M1 moot (anticipation holds 0 over). M2/M4/M6/M7 and the LOW items are latent / by-design (documented). No CRITICAL or HIGH items open.

---

## 2. Findings by Severity

### CRITICAL
None. No finding produces an active conservation break, data-integrity failure, or runtime crash in the shipped pipeline.

### HIGH

**H1. FW biomass excluded from the binding harvest controller's 3.8M cap check**
- `placement.py:1528-1553` (`_realized_facility_metrics`), `state.py:181-182`, `placement.py:2577-2601`.
- The closed-loop controller sizes harvest against `fac_bio` = OG/SW(+STARVE) tank biomass only; FW-stage fish are never stocked into tanks (`events.py:85-90` stocks TranOG as SW into OG tanks; no path assigns stage=FW). The controller walks OG-only biomass up to ~3.8M, leaving FW (~150–265k kg) on top, uncounted. True facility biomass (FW+OG) systematically exceeds the cap by the FW load whenever the controller sits at setpoint.
- **Fix:** add the in-flight FW pool biomass (already read from the PR at `run.py:171-184`, logged "not in TankState; TBD") into the setpoint basis, or carry FW as a tracked facility-biomass term parallel to tank biomass. Confirm with the permit owner whether the cap counts small FW fish.

**H2. Operator cap reports (Advisory / FacilityMap / YearlySummary) report OG-only biomass against the FW-inclusive cap**
- `excel_io.py:2284-2330` (`write_advisory`), `2224-2247` (FacilityMap), `587-642` (`write_yearly_summary`); numerator from `batch_locations` (OG-only) vs the 3.8M whole-facility cap.
- `Total_Biomass`, `Biomass_Excess`, `Peak_Biomass`, `Mean_Utilisation`, and the `OK / REDUCE BIOMASS` flag all understate true biomass by the FW fraction. The Advisory can show "OK" / spare headroom while the engine's own cap basis is at/over the limit. (The same files already apply an FW-*feed* correction but never the analogous FW-*biomass* correction — an omission, not a convention.)
- **Fix:** add projected FW/EGG biomass to the report numerators (mirror the FW-feed pattern).

**H3. Operator-pinned harvests are silently ignored; docstring claims the opposite** *(latent)*
- `harvest_scheduler.py:176-191` (pins tagged), `placement.py` (no pin handling), `excel_io.py:193-195` (false docstring).
- Pins enter the scheduler demand list but `placement` ignores the demand list (see H4), so pins never become Harvest events. **In the production entry point `run.py:198` hardcodes `pinned_harvests = []`** — zero pins today, so this is a latent trap + stale docs, not an active miscalculation.
- **Fix:** wire pins to executed Harvest events, or delete the dead pathway and fix the docstring + the `run.py:203` print.

**H4. Scheduler `HarvestDemand` list is dead in the physical plan; placement recomputes harvest closed-loop**
- `placement.py:2288-2300` (`demands_by_week`/`weekly_demand_count` built, never read), `2577-2861` (harvest driven by realized metrics).
- The HarvestPlan an operator reasons about from scheduler diagnostics (FIFO order, per-week drawdown, min-harvest warnings, printed at `run.py:319-340`) is **not** what the facility executes. *(Verifier: the demand list is not fully inert — it shapes the Phase-A precalc load footprint — so this is plan-vs-execution divergence + dead variables + misleading diagnostics, no accounting bug. Adjusted severity: medium.)*
- **Fix:** remove the dead dicts; reconcile or relabel the printed diagnostics as advisory-only.

### MEDIUM

**M1. Cap-relevant `biomass_kg` is the within-week MEAN, not the week-close peak**
- `biology.py:461-463,479` (`biomass_mean`) consumed at `harvest_scheduler.py:92,214`.
- The scheduler caps on the weekly mean, but biomass rises within the week; at grow-out SGR ~0.7%/day the week-close is ~+2.1% above the mean (~80,000 kg, ~4× the 0.5% band). The facility can sit ~2% over the true 3.8M peak while the scheduler reads in-band. Biology already exports `close_biomass_kg`.
- **Fix:** have the cap path read `close_biomass_kg`, or tighten the band to absorb the offset.

**M2. No hard biomass clamp — intentional +0.5% overshoot plus min-only ride**
- `placement.py:2597-2644`, `harvest_scheduler.py:263-296`. Biomass is allowed to `cap*1.005` and higher transiently (55k-fish/week clip can't keep up, or the carrying-capacity min-only ride). Largely intentional, but stacks on the FW-understated base.
- **Fix:** document the intended overshoot; ensure the band sits on an FW-inclusive base once H1 lands.

**M3. Facility feed/day cap (34,000 kg) never enforced — only a biomass proxy, biased by off-feed STARVE biomass**
- `placement.py:2598-2599,1538-1545`. `eff_cap = feed_cap * fac_bio / _fac_feed_kg_day`; numerator includes off-feed STARVE/6N biomass while the denominator is SW-only feed, inflating the feed-implied ceiling during purge. Per-system caps (12×3,000=36,000) exceed the facility cap, so all system caps can pass while facility feed/day is ~2,000 kg/day over.
- **Fix:** add a hard facility feed/day check on realized SW feed, or exclude off-feed biomass from the proxy numerator.

**M4. Continuity-audit charges mortality on the pre-harvest count — systematic same-sign count delta every harvest week**
- `excel_io.py:2049-2058` vs realized order in `placement.py:2799-2808,3296-3307`. The audit computes `mort = (prev_count+tn_in)*m_pct/100` then subtracts `h_out`, but the engine harvests at week-start and applies mortality to the post-harvest count. Each harvest week the audit over-charges mortality, driving a positive `delta_count` of the same sign — a distributed bias that partially consumes the facility signed/abs leak detector. *Audit-layer defect; the realized plan conserves fish.*
- **Fix:** use realized mortality count `rmort_tw` for the count balance, or apply `m_pct` to `(prev_count - h_out + tn_in)`.

**M5. Two divergent `fac_biomass` definitions — the FW-inclusive one is advisory and does not bind**
- `harvest_scheduler.py:214` (FW-inclusive) vs `placement.py:2577-2580` (OG-only realized). Validating against the scheduler invites the false conclusion that the cap is FW-correct, while the executed plan and Advisory are FW-blind. *(This is the explanatory framing of H1/H2 — subsumed by fixing them.)*

**M6 / M7. Scheduler/precalc biomass projection cannot see 6N/STARVE depuration biomass; realized controller can**
- `harvest_scheduler.py:97-152,214`, `placement.py:2577-2599`. The realized cap check is 6N-correct, but the planning projection (early-warning, migration sizing, overflow trigger) under-reserves against the cap by the 6N hold near peak. Bounded to ~one week of SGR on the 6-tank pool under `starvation_period_days: 7`.
- **Fix:** project the 6N hold in the scheduler curve, or document the projection as a lower bound and rely on the realized layer.

### LOW (and latent)

- **L1.** Grade event conserves count but not biomass (`events.py:285-309`); the single live grade path is biomass-safe by construction. Add a biomass-conservation assert symmetric to INV-3.
- **L2.** `open_biomass_kg` pairs opening count with day-0 post-growth weight (`biology.py:490-493`) — one day's growth overstatement in the open-biomass ledger column; reporting-only, never reaches the cap.
- **L3.** Dropped-batch audit can't see a partially-placed batch losing a sub-population (`excel_io.py:1460-1524`); `PLACED` is binary. Latent coverage seam.
- **L4.** TranOG reconciliation cull vs scheduled bottom cull in the FW transit window (`biology.py:367-413`) — latent under-stocking risk; live culls fire ~100 days before TranOG.
- **L5.** Immediate-harvest path increments progress by requested `take`, not actual `ev.count` (`placement.py:2861`); bounded by one tank's remnant.
- **L6.** Move-in/harvest count uses FIFO-oldest avg weight but pulls biggest fish first (`placement.py:2623-2632`); biases *under* the cap, self-correcting.
- **L7.** Vestigial knob `harvest_setpoint_lookahead_weeks` (`models.py:71`, `app.py:68,316,1415`) — no longer read by any harvest path; the UI invites tuning a no-op. Remove or re-document as inactive.
- **L8.** FW/EGG feed added unconditionally to FeedForecast/YearlySummary but conditionally to Weekly/Monthly ledger (`excel_io.py:812-816,609-614` vs `1102-1103`); zero overlap today but contradicts the tie-out invariant.
- **L9.** ProductionReport FW prefix parsing absorbs unknown prefixes silently; no avg-wt/biomass consistency check on PR input (`production_report.py:62-87`).

### INFO (resolved questions / dormant guards)

- **I1.** **The 6N open question is RESOLVED:** 6N depuration biomass IS counted exactly once by the realized controller (`placement.py:1528-1536`, `state.py:181-182`) and the Advisory — neither missed nor double-counted. The "harvest removes count" worry applies only to the non-binding Layer-2 scheduler.
- **I2.** **No end-to-end mass balance:** FW-phase mortality + culls are never reconciled against the egg seed (`excel_io.py:1413-1579`, one-sided creation guard; the closed `seeded==harvested+standing+mort+cull` equation exists only in the out-of-scope POC). A FW survival-model error would silently change total harvest tonnage with all gates green. Partially mitigated by the `FW_Survival%`/`FW_Flag` 5% divergence column. **This is the single highest-value assurance upgrade** — schedule it as the top follow-up.
- **I3.** SGR/FCR curves clamp-extrapolate flat past the table top (`biology.py:81-84`); `run.py:495-508` truncates each batch to its realized harvest-out week before reporting, so realized/capped numbers are not inflated. Trap for any future consumer of raw `project_*_batch` output.
- **I4.** Size-class split lower-mean clamp (`biology.py:260`) only activates at CV > ~125% (production CV ~16%). Dormant guard.

---

## 3. What is SOUND

- **In-facility fish conservation (OG/TranOG onward):** `TankContinuityAudit` holds 0-drift on count and reconciles biomass against realized-biology ground truth. The realized plan does not leak or create fish.
- **6N depuration cap accounting:** Fully correct in the binding layer — STARVE biomass counts toward the cap exactly once for the hold duration, is excluded from feed/growth, and drains only at the pair Harvest event.
- **Closed-loop harvest execution:** decides harvest against realized tank biomass (not the scheduler projection) — the architecturally correct choice, internally consistent.
- **Size-class split & biology daily projection:** count and biomass conserve at realistic CVs; daily cull→mortality→growth ordering is consistent.
- **Per-system feed/density caps and the realized rebalancer:** hard-checked and enforced; 6N STARVE biomass counts to caps while eating nothing.
- **Realized-lifespan truncation:** neutralizes the non-physical flat-SGR tail before any report/cap consumer sees it.

---

## 4. Bottom line

The engine that *executes* the plan is conservation-correct and cap-correct (including 6N). The deployment risk is concentrated in **(a)** FW biomass being absent from the binding setpoint and from every operator-facing cap report (H1/H2/M5), and **(b)** a cluster of stale/dead planning surfaces (pins H3, dead demand list H4, vestigial knob L7) that mislead operators about what the facility will do. **Fix the FW-cap cluster and clear the misleading documentation/dead-code traps before company-wide rollout; schedule the closed mass-balance gate (I2) as the top assurance follow-up.**

*Full per-finding adversarial verdicts (the reasoning each verifier used to confirm/refute) are preserved in the workflow transcript under `.../subagents/workflows/wf_f82b666b-ab7`.*
