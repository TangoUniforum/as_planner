"""Runner: WHOLE-FACILITY tankless L1 (model_full_facility) — FW + OG + purge.

METHOD: GLOBAL (tankless L1 POC) — TRUE whole-facility biomass/feed correction
================================================================================

The production controller (and the original L1 POC) enforces the facility
biomass cap against OG (grow-out / seawater) biomass ONLY. But the real facility
limit covers the ENTIRE farm:

    facility standing = FW-phase (smolt/egg, pre-TranOG) + OG grow-out
                        + 6N purge-hold
    facility feed     = FW-phase feed + OG feed            (purge = off-feed)

FW biomass is a GIVEN — set by the stocking cadence + FW growth + each batch's
TranOG date — and is NOT harvestable. So when the whole facility is counted,
the OG pool is squeezed to (cap - FW(week) - purge(week)) and L1 must harvest
OG harder / earlier to hold the TRUE total under the cap.

This runner, on the repo config/ + scenario/ + Forecast.xlsm (PR-hydrated):

  1. Runs L1 with model_full_facility=ON and prints the per-week FW / OG / purge
     breakdown + the TOTAL vs the facility cap.
  2. Runs L1 with model_full_facility=OFF (== the controller's OG-only modeling
     philosophy) and RE-SCORES it on the TRUE total (its OG standing + the same
     FW + its own purge), exposing how far the OG-only plan VIOLATES the true
     limit (it ignores FW).
  3. Prints the corrected head-to-head: peak TRUE total + weeks-over-cap for the
     OG-only (controller-style) plan vs the full-facility plan, and the HOG
     trade-off (full-facility harvests OG harder).

ADDITIVE; touches no production file; not imported by the pipeline.

Usage:
    python -m tools.run_full_facility_poc
    python -m tools.run_full_facility_poc --no-pr
    python -m tools.run_full_facility_poc --purge-hold     # also model 6N hold
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from forecast.config_io import load_config
from forecast.scenario_io import load_batches
from forecast import global_planner_poc as gpp


def _hydrate_pr(workbook_path: Path, batches):
    """Read PR -> (inflight_og, fw_inflight, derived_start).

    inflight_og: batch_id -> (count, avg_wt_g, cv_pct)  [OG seeds for L1]
    fw_inflight: batch_id -> (count, avg_wt_g, pr_closing_date)  [FW-phase
                 override so FW-in-flight batches project from PR state]
    """
    try:
        from forecast.excel_io import load_workbook
        from forecast.production_report import read_production_report
    except Exception as e:  # noqa: BLE001
        print(f"  (could not import PR reader: {e}); running incoming-only")
        return {}, {}, None
    if not workbook_path.exists():
        print(f"  (workbook {workbook_path} not found; running incoming-only)")
        return {}, {}, None
    wb = load_workbook(workbook_path)
    pr_closing, og_records, fw_records = read_production_report(wb)
    wb.close()
    derived_start = None
    pr_close_date = None
    if pr_closing is not None:
        derived_start = datetime(pr_closing.year, pr_closing.month,
                                 pr_closing.day) + timedelta(days=1)
        pr_close_date = datetime(pr_closing.year, pr_closing.month, pr_closing.day)

    og_agg: dict[str, dict] = {}
    for r in og_records:
        e = og_agg.setdefault(r.batch_id, {"count": 0.0, "biomass_kg": 0.0})
        e["count"] += r.closing_count
        e["biomass_kg"] += r.closing_biomass_kg
    batch_cv = {b.batch_id: b.tran_og_cv for b in batches}
    inflight_og = {}
    for bid, e in og_agg.items():
        if e["count"] > 0:
            avg_wt = e["biomass_kg"] * 1000.0 / e["count"]
            inflight_og[bid] = (e["count"], avg_wt, batch_cv.get(bid, 16.0))

    # FW-in-flight: measured in FW units at PR, NOT yet in OG. Mirrors run.py.
    fw_agg: dict[str, dict] = {}
    for r in fw_records:
        e = fw_agg.setdefault(r.batch_id, {"count": 0.0, "biomass_kg": 0.0})
        e["count"] += r.closing_count
        e["biomass_kg"] += r.closing_biomass_kg
    fw_inflight = {}
    for bid, e in fw_agg.items():
        if e["count"] > 0 and bid not in inflight_og:
            avg_wt = e["biomass_kg"] * 1000.0 / e["count"]
            fw_inflight[bid] = (e["count"], avg_wt, pr_close_date)
    return inflight_og, fw_inflight, derived_start


def _score_true_total(res, fw_bio_by_label):
    """Re-score a PlannerResult on the TRUE total = OG (post-harvest) + purge + FW.

    For a model_full_facility=ON result the trace.standing_biomass_kg already IS
    the true total. For an OG-only result (model_full_facility=OFF) the trace
    standing is OG+purge only; we ADD the (given) FW biomass per week to expose
    the true total the OG-only plan actually carries.
    """
    rows = []
    for r in res.trace:
        fw = fw_bio_by_label.get(r.week_label, 0.0)
        # If the result already counted FW (full-facility), don't double-add.
        already_has_fw = abs(r.fw_biomass_kg) > 1e-9
        true_total = (r.standing_biomass_kg if already_has_fw
                      else r.standing_biomass_kg + fw)
        og_purge = (r.standing_biomass_kg - r.fw_biomass_kg if already_has_fw
                    else r.standing_biomass_kg)
        rows.append((r.week, r.week_label, fw, og_purge, true_total, r.biomass_cap,
                     r.harvested_kg))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config-dir", default=str(_ROOT / "config"))
    ap.add_argument("--scenario-dir", default=str(_ROOT / "scenario"))
    ap.add_argument("--workbook", default=str(_ROOT / "Forecast.xlsm"))
    ap.add_argument("--no-pr", action="store_true")
    ap.add_argument("--purge-hold", action="store_true",
                    help="also model the 6N off-feed purge hold (counts to total)")
    args = ap.parse_args()

    print("=" * 78)
    print("  METHOD: GLOBAL (tankless L1 POC) — TRUE WHOLE-FACILITY biomass/feed")
    print("=" * 78)

    control, tables, facility = load_config(args.config_dir)
    batches = load_batches(args.scenario_dir)
    inflight_og, fw_inflight = {}, {}
    if not args.no_pr:
        inflight_og, fw_inflight, derived_start = _hydrate_pr(
            Path(args.workbook), batches)
        if derived_start is not None:
            control.forecast_start = derived_start
            print(f"  ForecastStart: derived {derived_start.date()} (PR closing +1d)")
        print(f"  In-flight OG batches: {len(inflight_og)}; "
              f"FW-in-flight batches: {len(fw_inflight)}")

    fs = control.forecast_start
    fs_date = fs.date() if hasattr(fs, "date") else fs
    cap_b = control.max_biomass_kg
    cap_f = control.max_feed_per_day_kg
    print(f"  forecast_start={fs_date}, horizon={control.horizon_weeks}w")
    print(f"  facility caps: biomass<={cap_b:,.0f} kg, feed<={cap_f:,.0f} kg/day")

    common = dict(inflight_og=inflight_og, record_standing=True,
                  model_purge_hold=args.purge_hold)

    # OG-only (the controller's modeling philosophy): cap checked vs OG only.
    og_only = gpp.plan(batches, tables, control, facility,
                       model_full_facility=False, **common)
    # Whole-facility: cap checked vs FW + OG + purge.
    full = gpp.plan(batches, tables, control, facility,
                    model_full_facility=True, fw_inflight=fw_inflight, **common)

    # The FW-phase biomass per week (the given) — same for both scorings.
    fw_bio, fw_feed = gpp.fw_phase_biomass_feed_by_week(
        batches, tables, control, fw_inflight=fw_inflight)

    # ---------------------------------------------------------------
    # 1) Per-week FW / OG / purge / TOTAL breakdown (full-facility ON)
    # ---------------------------------------------------------------
    print("\n" + "-" * 78)
    print("  PER-WEEK BREAKDOWN (model_full_facility=ON): FW + OG + purge vs cap")
    print("-" * 78)
    print(f"  {'wk':>3} {'label':<9} {'FW_kg':>11} {'OG_kg':>11} {'purge_kg':>10} "
          f"{'TOTAL_kg':>12} {'%cap':>6} {'HOG_kg':>10}")
    for r in full.trace:
        total = r.standing_biomass_kg
        pct = 100.0 * total / cap_b if cap_b else 0.0
        flag = "  <-OVER" if total > cap_b + 1e-3 else ""
        print(f"  {r.week:>3} {r.week_label:<9} {r.fw_biomass_kg:>11,.0f} "
              f"{r.og_biomass_kg:>11,.0f} {r.purge_biomass_kg:>10,.0f} "
              f"{total:>12,.0f} {pct:>5.1f}% {r.harvested_kg:>10,.0f}{flag}")

    # ---------------------------------------------------------------
    # 2) OG-loading + harvest-rate change (OG-only vs full-facility)
    # ---------------------------------------------------------------
    def _og_standing(res):
        # OG (post-harvest) standing per week, excluding FW.
        return [r.og_biomass_kg + r.purge_biomass_kg if r.fw_biomass_kg or r.purge_biomass_kg
                else r.standing_biomass_kg for r in res.trace]

    og_only_og = [r.standing_biomass_kg for r in og_only.trace]  # OG+purge (no FW)
    full_og = [r.og_biomass_kg + r.purge_biomass_kg for r in full.trace]
    peak_og_only = max(og_only_og, default=0.0)
    peak_full_og = max(full_og, default=0.0)
    mean_og_only = sum(og_only_og) / len(og_only_og) if og_only_og else 0.0
    mean_full_og = sum(full_og) / len(full_og) if full_og else 0.0
    hog_og_only = sum(r.harvested_kg for r in og_only.trace)
    hog_full = sum(r.harvested_kg for r in full.trace)

    print("\n" + "-" * 78)
    print("  OG LOADING + HARVEST RATE: OG-only plan vs full-facility plan")
    print("-" * 78)
    print(f"  {'metric':<34} {'OG-only':>14} {'full-facility':>15} {'delta':>12}")
    print(f"  {'-'*34} {'-'*14} {'-'*15} {'-'*12}")
    print(f"  {'peak OG(+purge) standing kg':<34} {peak_og_only:>14,.0f} "
          f"{peak_full_og:>15,.0f} {peak_full_og-peak_og_only:>+12,.0f}")
    pk_pct = (100.0*(peak_full_og-peak_og_only)/peak_og_only) if peak_og_only else 0.0
    print(f"  {'  (% change)':<34} {'':>14} {'':>15} {pk_pct:>+11.1f}%")
    print(f"  {'mean OG(+purge) standing kg':<34} {mean_og_only:>14,.0f} "
          f"{mean_full_og:>15,.0f} {mean_full_og-mean_og_only:>+12,.0f}")
    mn_pct = (100.0*(mean_full_og-mean_og_only)/mean_og_only) if mean_og_only else 0.0
    print(f"  {'  (% change)':<34} {'':>14} {'':>15} {mn_pct:>+11.1f}%")
    print(f"  {'total HOG harvested kg':<34} {hog_og_only:>14,.0f} "
          f"{hog_full:>15,.0f} {hog_full-hog_og_only:>+12,.0f}")

    # ---------------------------------------------------------------
    # 3) CORRECTED head-to-head on TRUE total (OG + FW + purge)
    # ---------------------------------------------------------------
    og_only_scored = _score_true_total(og_only, fw_bio)
    full_scored = _score_true_total(full, fw_bio)

    def _true_summary(scored):
        peak = max((s[4] for s in scored), default=0.0)
        over = [s for s in scored if s[4] > cap_b + 1e-3]
        peak_over_pct = 100.0 * peak / cap_b if cap_b else 0.0
        return peak, peak_over_pct, len(over), over

    peak_co, pct_co, nover_co, over_co = _true_summary(og_only_scored)
    peak_full, pct_full, nover_full, _ = _true_summary(full_scored)

    print("\n" + "=" * 78)
    print("  CORRECTED HEAD-TO-HEAD: scored on TRUE TOTAL facility biomass "
          "(OG + FW + purge)")
    print("=" * 78)
    print(f"  facility cap = {cap_b:,.0f} kg")
    print(f"  {'plan':<34} {'peak TRUE total':>16} {'% of cap':>10} {'wks over cap':>13}")
    print(f"  {'-'*34} {'-'*16} {'-'*10} {'-'*13}")
    print(f"  {'OG-only (controller philosophy)':<34} {peak_co:>16,.0f} "
          f"{pct_co:>9.1f}% {nover_co:>13}")
    print(f"  {'full-facility (global L1)':<34} {peak_full:>16,.0f} "
          f"{pct_full:>9.1f}% {nover_full:>13}")
    print(f"\n  HOG trade-off: full-facility harvests {hog_full-hog_og_only:+,.0f} kg "
          f"({100*(hog_full-hog_og_only)/hog_og_only:+.1f}%) "
          f"vs the OG-only plan to make room for FW.")
    if over_co:
        print(f"\n  Weeks the OG-only plan VIOLATES the true cap "
              f"(showing up to 12 of {len(over_co)}):")
        print(f"    {'wk':>3} {'label':<9} {'FW_kg':>11} {'OG+purge_kg':>12} "
              f"{'TRUE_kg':>12} {'%cap':>6}")
        for s in over_co[:12]:
            pct = 100.0 * s[4] / cap_b
            print(f"    {s[0]:>3} {s[1]:<9} {s[2]:>11,.0f} {s[3]:>12,.0f} "
                  f"{s[4]:>12,.0f} {pct:>5.1f}%")

    # ---- Conservation (both must close exactly) ----
    worst_co = max((abs(c["residual_pct"]) for c in og_only.conservation.values()),
                   default=0.0)
    worst_full = max((abs(c["residual_pct"]) for c in full.conservation.values()),
                     default=0.0)
    print(f"\n  CONSERVATION worst |residual|: OG-only {worst_co:.4f}%, "
          f"full-facility {worst_full:.4f}%  "
          f"({'OK both conserve' if max(worst_co, worst_full) < 0.01 else 'CHECK'})")

    # Feed (full-facility): peak total feed vs cap.
    peak_feed_full = max((r.feed_kg_day for r in full.trace), default=0.0)
    peak_fw_feed = max(fw_feed.values(), default=0.0) if fw_feed else 0.0
    print(f"  Feed (full-facility): peak total {peak_feed_full:,.0f} kg/day "
          f"({100*peak_feed_full/cap_f:.1f}% of {cap_f:,.0f} cap); "
          f"peak FW-phase feed alone {peak_fw_feed:,.0f} kg/day")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
