"""ENTRY POINT: produce the STANDARD forecast workbook from the GLOBAL method.

METHOD: GLOBAL (precalculated L1->L3)
====================================

This is a NEW entry point — it does NOT touch `forecast/run.py`. It hydrates the
PR exactly like the loop runners, runs the converged L1<->L3 global planner
(`forecast.global_planner_loop_poc.run_loop`) with the CORRECT whole-facility
models (6N purge hold + FW+OG+purge counted vs the cap), adapts the result into
the structures the SHARED `forecast.excel_io` writers consume
(`forecast.global_forecast.build_tables`), and emits a standard workbook with a
clear METHOD STAMP and a "_GLOBAL" filename suffix.

The production pipeline (`forecast.run.main`) stays byte-identical; this runner
imports the writers but adds no branch to run.py.

Sheets emitted
--------------
  RunConfig (method stamp), HarvestPlan, HarvestReport, Batch Plan,
  FeedForecastWeekly, FeedForecastMonthly, Advisory, WeeklyReport,
  MonthlyReport, ReconciliationReport (L1 conservation), StandingTrace (L1).
SPECIFIC-TANK PICK (step #2 — NOW REAL, `forecast.global_tank_pick_poc`):
  BatchLocations (real per-physical-tank occupancy, continuity-preserving),
  FacilityMap (real physical tank grid), TransferPlan (real tank-to-tank moves;
  6N sixn pair round-robin), and TankContinuityAudit (proves 0 TANK_DRIFT /
  0 BIO_DRIFT over the emitted locations + transfers + harvest events). The known
  1-week structural over-subscription is double-stacked + flagged, not dropped.

Usage:
    python -m tools.run_global_forecast
    python -m tools.run_global_forecast --workbook Forecast.xlsm --out out.xlsx
    python -m tools.run_global_forecast --no-pr        # incoming-only
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from forecast.config_io import load_config
from forecast.scenario_io import load_batches, load_limits
from forecast import global_forecast as gf
from forecast import global_planner_loop_poc as loop
from tools.run_full_facility_poc import _hydrate_pr


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config-dir", default=str(_ROOT / "config"))
    ap.add_argument("--scenario-dir", default=str(_ROOT / "scenario"))
    ap.add_argument("--workbook", default=str(_ROOT / "Forecast.xlsm"))
    ap.add_argument("--out", default=None,
                    help="output .xlsx path; default = <workbook stem>_GLOBAL.xlsx")
    ap.add_argument("--max-iterations", type=int, default=10)
    ap.add_argument("--margin-frac", type=float, default=0.5)
    ap.add_argument("--slack-epsilon", type=float, default=1000.0)
    ap.add_argument("--mip-time-limit", type=float, default=180.0)
    ap.add_argument("--mip-rel-gap", type=float, default=0.01)
    ap.add_argument("--no-pr", action="store_true")
    args = ap.parse_args()

    print("=" * 72)
    print(f"  METHOD: GLOBAL (precalculated L1->L3) — STANDARD WORKBOOK EXPORT")
    print("=" * 72)

    control, tables, facility = load_config(args.config_dir)
    batches = load_batches(args.scenario_dir)
    _facility_limits, system_limits = load_limits(args.scenario_dir)
    print(f"  Config:   {args.config_dir}")
    print(f"  Scenario: {len(batches)} batches from {args.scenario_dir}")

    inflight_og, fw_inflight, purge_inflight = {}, {}, {}
    if not args.no_pr:
        # Hydrate BOTH OG and FW in-flight units — model_full_facility (the
        # correct default) REQUIRES fw_inflight or it under-counts the FW phase.
        # purge_inflight = fish already in 6N at hand-over (primes the purge
        # pipeline so L1 doesn't spin a fresh backlog -> startup overshoot).
        inflight_og, fw_inflight, derived_start, purge_inflight = _hydrate_pr(
            Path(args.workbook), batches)
        if derived_start is not None:
            control.forecast_start = derived_start
            print(f"  ForecastStart: derived {derived_start.date()} from PR closing +1d")
        print(f"  In-flight OG batches: {len(inflight_og)}; "
              f"FW-in-flight batches: {len(fw_inflight)}")

    fs = control.forecast_start
    fs_date = fs.date() if hasattr(fs, "date") else fs
    print(f"  forecast_start={fs_date}, horizon={control.horizon_weeks}w")
    print(f"  facility caps: biomass<={control.max_biomass_kg:,.0f} kg, "
          f"feed<={control.max_feed_per_day_kg:,.0f} kg/day")

    # ---- Run the converged L1<->L3 loop with the CORRECT whole-facility models.
    print("\n  RUNNING L1<->L3 LOOP (model_purge_hold=True, model_full_facility=True)")
    result = loop.run_loop(
        batches, tables, control, facility, system_limits,
        inflight_og=inflight_og,
        max_iterations=args.max_iterations,
        margin_frac=args.margin_frac,
        l3_kwargs=dict(slack_epsilon=args.slack_epsilon,
                       mip_time_limit=args.mip_time_limit,
                       mip_rel_gap=args.mip_rel_gap, verbose=False),
        model_purge_hold=True,
        model_full_facility=True,
        fw_inflight=fw_inflight,
        purge_inflight=purge_inflight,
        verbose=True,
    )
    print(f"  converged={result.converged}, iterations={result.n_iterations}, "
          f"peak {result.peak_biomass_kg:,.0f} kg ({result.pct_of_cap_peak:.1f}% cap), "
          f"HOG {result.total_hog_kg:,.0f} kg")

    # ---- Adapt to excel_io-ready structures.
    gft = gf.build_tables(result, batches, tables, control, facility,
                          fw_inflight=fw_inflight)
    cons = gf.conservation_summary(gft)
    print(f"\n  CONSERVATION (facility): seeded {cons['seeded']:,.0f} == "
          f"harvested {cons['harvested']:,.0f} + standing {cons['standing']:,.0f} "
          f"+ mort {cons['mortality']:,.0f} + cull {cons['cull']:,.0f}")
    print(f"    accounted {cons['accounted']:,.0f}; residual {cons['residual']:,.3f} "
          f"({cons['residual_pct']:.4f}%); worst per-batch "
          f"{cons['worst_batch_residual_pct']:.4f}% "
          f"({'OK — conserves' if cons['worst_batch_residual_pct'] < 0.01 else 'CHECK'})")
    print(f"  SPECIFIC-TANK PICK (step #2): {gft.n_transfers} tank-to-tank "
          f"transfers; {len(gft.batch_locations)} tank-week rows; "
          f"over-subscribed weeks: {len(gft.oversub_weeks)} "
          f"({gft.n_oversub_rows} double-stacked rows)"
          + (f" {gft.oversub_weeks}" if gft.oversub_weeks else ""))

    # ---- Emit the standard workbook via the SHARED writers.
    out_path = (Path(args.out) if args.out
                else Path(args.workbook).with_name(
                    Path(args.workbook).stem + "_GLOBAL.xlsx"))
    _emit_workbook(gft, result, batches, tables, control, facility,
                   _facility_limits, cons, out_path)
    print(f"\n  Wrote {out_path}")
    return 0


def _apply_manual_window(input_path, scenario_dir, control, tables, facility,
                         batches, inflight_og, fw_inflight, purge_inflight):
    """Execute the operator's manual override window (scenario/manual_events.yaml)
    BEFORE the global plan, then CONVERT the resulting facility state into the
    Global engine's aggregated seeds — so the precalculated method plans forward
    from the SAME starting point the controller uses (the manual transfers /
    harvests + their biology), not the raw PR.

    The global engine consumes aggregated per-batch dicts, not a tank-level state,
    so we: (1) hydrate a tank-level FacilityState from the PR, (2) run
    advance_facility_window (events + biology for the manual weeks), (3) aggregate
    the post-window tanks back into inflight_og (non-6N OG) + purge_inflight (6N),
    and drop any FW batch manually moved to OG from fw_inflight. Returns
    (inflight_og, fw_inflight, purge_inflight, new_start_datetime, new_horizon,
    warnings, window_weeks). No manual events -> inputs unchanged (window_weeks=0).
    """
    from datetime import datetime as _dt
    from collections import defaultdict
    from forecast.manual_events import load_manual_events
    events = load_manual_events(str(scenario_dir))
    if not events:
        return (inflight_og, fw_inflight, purge_inflight,
                control.forecast_start, control.horizon_weeks, [], 0, None)
    from forecast.excel_io import load_workbook
    from forecast.production_report import (
        read_production_report, hydrate_facility_state)
    from forecast.state import FacilityState
    from forecast.manual_window import advance_facility_window
    from forecast.sixn import SIXN_ALL_TANKS
    wb = load_workbook(str(input_path))
    pr_closing, og_records, fw_records = read_production_report(wb)
    wb.close()
    fs = control.forecast_start
    fs_date = fs.date() if hasattr(fs, "date") else fs
    state = FacilityState.from_facility_config(facility, today=fs_date)
    hydrate_facility_state(state, og_records, batches)
    # Snapshot the PR-hydrated OPENING (pre-window) as a second, un-advanced state
    # so the TankContinuityAudit can open the first manual week from the real
    # in-flight tanks instead of from 0 (else every opening batch reads as a
    # +drift on the manual window's week 1 — the +2.07M boundary artifact).
    init_state = FacilityState.from_facility_config(facility, today=fs_date)
    hydrate_facility_state(init_state, og_records, batches)
    bbid = {b.batch_id: b for b in batches}
    window_n = max((e.week or 1) for e in events)
    win = advance_facility_window(
        state, bbid, tables, fs_date, window_n, events=events, control=control,
        pr_closing=pr_closing, fw_records=fw_records)
    # `state` is now the manual window's CLOSE (last manual week) — the tank
    # positions the global plan must CONTINUE from (not re-stock), so the W27
    # hand-off reconciles as transfers rather than vanish+restock.
    # Aggregate the post-window tanks into the Global engine's seed dicts.
    og_agg = defaultdict(lambda: [0.0, 0.0])
    purge_agg = defaultdict(lambda: [0.0, 0.0])
    for t in state.tanks_by_id.values():
        if t.is_empty:
            continue
        tgt = purge_agg if t.tank_id in SIXN_ALL_TANKS else og_agg
        tgt[t.batch_id][0] += t.count
        tgt[t.batch_id][1] += t.biomass_kg
    cv = {b.batch_id: b.tran_og_cv for b in batches}
    new_og = {bid: (c, b * 1000.0 / c, cv.get(bid, 16.0))
              for bid, (c, b) in og_agg.items() if c > 0}
    new_purge = {bid: (c, b * 1000.0 / c)
                 for bid, (c, b) in purge_agg.items() if c > 0}
    # FW batches manually crossed to OG are now in new_og -> drop from fw_inflight.
    transferred = set(win.get("transferred_fw_batches", set()))
    new_fw = {bid: v for bid, v in fw_inflight.items() if bid not in transferred}
    ns = win["new_start"]
    new_start = _dt(ns.year, ns.month, ns.day)
    # STITCH bundle: the manual weeks' per-tank rows + events, read-compatible with
    # the Global writers (placement.BatchLocationRow == TankLocRow read shape;
    # events.Transfer/Harvest/TranOGEntry == TankTransfer/Harvest/TranOG). Prepended
    # to gft so the output timeline OPENS with the manual work, then the global plan.
    stitch = {
        "batch_locations": win.get("batch_locations", []),
        "transfer_events": win.get("transfer_events", []),
        "harvest_events": win.get("harvest_events", []),
        "tranog_events": win.get("tranog_events", []),
        "realized_biology": win.get("realized_biology", {}),
        "initial_state": init_state,   # W23 audit opening (pre-window PR tanks)
        "window_close_state": state,   # W27 pick continuation (manual close tanks)
    }
    return (new_og, new_fw, new_purge, new_start,
            control.horizon_weeks - window_n, win.get("warnings", []), window_n, stitch)


def run_global(input_path, output_path, config_dir, scenario_dir, *,
               no_pr: bool = False, overstock: bool = True,
               max_iterations: int = 10, margin_frac: float = 0.5,
               slack_epsilon: float = 1000.0, mip_time_limit: float = 180.0,
               mip_rel_gap: float = 0.01,
               optimal: bool = False, cpsat_time: float = 300.0) -> int:
    """Produce the standard GLOBAL-method workbook at `output_path` from the PR at
    `input_path` + the app's config/scenario. Callable mirror of `main()` for the
    UI (parallel to `forecast.run.main`). `overstock=True` bakes in the placement
    optimizer's winning SELECTIVE over-stock (light<2.5kg toward the hard cap).
    Returns 0 on success. Touches no production file.
    """
    from forecast import global_planner_l3_poc as _l3
    control, tables, facility = load_config(str(config_dir))
    batches = load_batches(str(scenario_dir))
    _facility_limits, system_limits = load_limits(str(scenario_dir))
    inflight_og, fw_inflight, purge_inflight = {}, {}, {}
    _mw_stitch = None   # manual-window rows/events to prepend into the output
    if not no_pr:
        inflight_og, fw_inflight, derived_start, purge_inflight = _hydrate_pr(
            Path(input_path), batches)
        if derived_start is not None:
            control.forecast_start = derived_start
        # Manual override window: execute the operator's starting-state events
        # (manual transfers/harvests) + biology, then seed the global plan from the
        # resulting state — same starting point the controller honors. No events =>
        # unchanged. FW re-anchoring is approximate for now (fine when no fw_to_og).
        (inflight_og, fw_inflight, purge_inflight, _mw_start, _mw_horizon,
         _mw_warns, _mw_weeks, _mw_stitch) = _apply_manual_window(
            input_path, scenario_dir, control, tables, facility, batches,
            inflight_og, fw_inflight, purge_inflight)
        if _mw_weeks:
            control.forecast_start = _mw_start
            control.horizon_weeks = _mw_horizon
            print(f"  Manual override window: {_mw_weeks} week(s) executed before "
                  f"the global plan ({len(_mw_warns)} warning(s)); global now opens "
                  f"{_mw_start.date()}, horizon {_mw_horizon}w.")

    _prev = (_l3._OVERSTOCK_DENSITY_PCT, _l3._OVERSTOCK_MAX_WT_G)
    if overstock:
        _l3._OVERSTOCK_DENSITY_PCT, _l3._OVERSTOCK_MAX_WT_G = 1.0, 2500.0
    try:
        result = loop.run_loop(
            batches, tables, control, facility, system_limits,
            inflight_og=inflight_og, max_iterations=max_iterations,
            margin_frac=margin_frac,
            l3_kwargs=dict(slack_epsilon=slack_epsilon, mip_time_limit=mip_time_limit,
                           mip_rel_gap=mip_rel_gap, verbose=False),
            model_purge_hold=True, model_full_facility=True,
            fw_inflight=fw_inflight, purge_inflight=purge_inflight, verbose=False)
        grow_q = None
        if optimal:
            grow_q = _solve_cpsat_q(result, facility, system_limits, control,
                                    cpsat_time)
        gft = gf.build_tables(result, batches, tables, control, facility,
                              fw_inflight=fw_inflight, grow_q_by_week=grow_q,
                              initial_tank_state=(_mw_stitch or {}).get(
                                  "window_close_state"))
        # Stitch the MANUAL WINDOW weeks into the output so the timeline OPENS with
        # the operator's starting-state work (transfers/harvests + biology), then the
        # global plan. The manual rows/events are read-compatible with the writers
        # (BatchLocationRow == TankLocRow; events.* == Tank* shapes), so BatchLocations
        # / HarvestPlan / TransferPlan all show the manual weeks first. Prepended (the
        # writers sort by week). This does NOT touch the ReconciliationReport — that
        # is L1's per-batch conservation of the GLOBAL plan (still residual ~0); the
        # manual window conserves in its own right (it is the controller's audited
        # advance). The per-tank TankContinuityAudit stays an advisory cross-check.
        if _mw_stitch:
            gft.batch_locations = (list(_mw_stitch["batch_locations"])
                                   + list(gft.batch_locations))
            gft.harvest_events = (list(_mw_stitch["harvest_events"])
                                  + list(gft.harvest_events))
            gft.transfer_events = (list(_mw_stitch["transfer_events"])
                                   + list(gft.transfer_events))
            gft.tranog_events = (list(_mw_stitch["tranog_events"])
                                 + list(gft.tranog_events))
            if isinstance(getattr(gft, "realized_biology", None), dict):
                gft.realized_biology.update(_mw_stitch["realized_biology"])
            print(f"  Stitched the manual override window into the output: "
                  f"{len(_mw_stitch['batch_locations'])} BatchLocations rows, "
                  f"{len(_mw_stitch['transfer_events'])} transfer + "
                  f"{len(_mw_stitch['harvest_events'])} harvest event(s).")
        cons = gf.conservation_summary(gft)
        _emit_workbook(gft, result, batches, tables, control, facility,
                       _facility_limits, cons, Path(output_path),
                       initial_state=(_mw_stitch or {}).get("initial_state"))
    finally:
        _l3._OVERSTOCK_DENSITY_PCT, _l3._OVERSTOCK_MAX_WT_G = _prev
    return 0


def _solve_cpsat_q(result, facility, system_limits, control, time_limit):
    """Run the CP-SAT full-horizon optimal placement on L1's standing and return
    {week: {(batch, tank): kg}} for the optimal grow-out layout (0-swap)."""
    from collections import defaultdict
    from forecast.global_placement_milp_poc import solve_cpsat
    og = {t.tank_id: t.system_id for t in facility.tanks
          if t.type == "OG" and t.system_id != "OG6N"}
    tvol = {t.tank_id: t.max_density_kg_m3 * t.volume_m3 for t in facility.tanks
            if t.type == "OG" and t.system_id != "OG6N"}
    vol = {t.tank_id: t.volume_m3 for t in facility.tanks
           if t.type == "OG" and t.system_id != "OG6N"}
    by_week, wl_of = defaultdict(dict), {}
    for r in result.final_l1.batch_standing:
        if getattr(r, "in_purge", False) or r.biomass_kg <= 1e-9:
            continue
        by_week[r.week][r.batch_id] = (r.biomass_kg, r.feed_kg_day, r.avg_wt_g)
        wl_of[r.week] = r.week_label
    q, info = solve_cpsat(by_week, og, tvol, vol, wl_of, system_limits, control,
                          time_limit=time_limit, verbose=True)
    print(f"  [CP-SAT optimal placement] status={info['status']} "
          f"obj={info.get('obj')} bound={info.get('bound')} "
          f"slack={info.get('slack_kg')} over={info.get('over_kg')}")
    return q


def _emit_workbook(gft, result, batches, tables, control, facility,
                   facility_limits, cons, out_path: Path,
                   initial_state=None) -> None:
    from openpyxl import Workbook
    from forecast.excel_io import (
        write_advisory, write_batch_locations, write_batch_plan,
        write_facility_map, write_feed_forecast_monthly,
        write_feed_forecast_weekly, write_harvest_plan_output,
        write_harvest_plan_report, write_harvest_report,
        write_monthly_report, write_tank_continuity_audit,
        write_transfer_plan_output, write_weekly_report,
    )

    batch_by_id = {b.batch_id: b for b in batches}
    fs_date = gft.forecast_start
    hog = control.default_hog_yield
    bl = gft.batch_locations
    hv = gft.harvest_events
    fw_states_by_batch: dict[str, list] = {}
    for s in gft.fw_states:
        fw_states_by_batch.setdefault(s.batch_id, []).append(s)

    # ---- TankContinuityAudit: build the sheet, then scan it for drift flags ----
    # so the RunConfig stamp can report the drift counts (must be 0/0). The audit
    # reconciles the REAL BatchLocations + Transfers + Harvest events; the
    # specific-tank pick supplies `realized_biology` (per-tank net growth/mort)
    # and `mort_states` (per-(batch, week) weekly mortality %) so both the count
    # and biomass balances close exactly. No PR initial tank state is passed
    # (the global pick stocks every batch from empty), so first-week opens are 0.
    _audit_wb = Workbook()
    write_tank_continuity_audit(
        _audit_wb, bl, gft.mort_states, hv, gft.transfer_events,
        grade_events=[], tranog_events=gft.tranog_events,
        initial_state=initial_state,
        realized_biology=gft.realized_biology)
    n_tank_drift, n_bio_drift, fac_count_signed, fac_count_abs = \
        _scan_audit_drift(_audit_wb["TankContinuityAudit"])
    _audit_wb.close()
    # NOTE: the authoritative conservation proof for the GLOBAL (LP) method is the
    # ReconciliationReport (batch-level seeded == harvested + standing + mort + cull;
    # see conservation_summary). This per-tank TankContinuityAudit reconciles the
    # CONTROLLER's tank-to-tank Transfer/Harvest EVENT stream, which the LP placement
    # does not fully emit (it re-solves the whole-facility layout each week rather
    # than moving fish tank-by-tank) — so it can OVER-REPORT drift on LP output even
    # when every fish is accounted for. It is a cross-check, not the source of truth.
    _recon_ok = abs(cons.get("residual_pct", 0.0)) < 0.01
    # The per-tank audit is a REAL proof: with the specific-tank pick's complete
    # event stream (stocking, transfers, harvests, per-tank mortality) it must
    # reconcile open+events==close for every (tank, week). 0 TANK_DRIFT means
    # every fish is accounted per tank; nonzero is a genuine gap to INVESTIGATE,
    # not an artifact. (BIO_DRIFT is the kg cross-check; batch-level conservation
    # is separately proven by the ReconciliationReport.)
    _tank_clean = (n_tank_drift == 0 and abs(fac_count_signed) < 1.0)
    _verdict = ("PASS — every fish accounted per tank, per week"
                if _tank_clean else
                "INVESTIGATE — per-tank count flow does not reconcile")
    tank_drift_note = (
        f"TankContinuityAudit (per-tank cross-check): {_verdict}. "
        f"TANK_DRIFT={n_tank_drift}, BIO_DRIFT={n_bio_drift}, facility count "
        f"signed/abs {fac_count_signed:.0f}/{fac_count_abs:.0f}. Batch-level "
        f"ReconciliationReport residual {cons.get('residual_pct', 0.0):+.4f}% "
        f"({'CONSERVES' if _recon_ok else 'CHECK RECON'}).")
    print("  " + tank_drift_note)

    wb = Workbook()

    # ---- RunConfig: the METHOD STAMP + model flags + conservation. ----
    ws = wb.active
    ws.title = "RunConfig"
    ws["A1"] = "RUN CONFIG — GLOBAL METHOD EXPORT"
    rows = [
        ("planning_method", gf.METHOD_STAMP),
        ("model_purge_hold", "True (2-week off-feed 6N purge depuration flow)"),
        ("model_full_facility", "True (FW + OG + 6N purge counted vs the cap)"),
        ("fw_inflight", "hydrated from PR (FW-phase not under-counted)"),
        ("forecast_start", str(fs_date)),
        ("horizon_weeks", control.horizon_weeks),
        ("biomass_cap_kg", control.max_biomass_kg),
        ("feed_cap_kg_day", control.max_feed_per_day_kg),
        ("loop_converged", result.converged),
        ("loop_iterations", result.n_iterations),
        ("peak_facility_biomass_kg", round(result.peak_biomass_kg, 0)),
        ("peak_pct_of_cap", f"{result.pct_of_cap_peak:.1f}%"),
        ("total_HOG_kg", round(result.total_hog_kg, 0)),
        ("CONSERVATION seeded", round(cons["seeded"], 0)),
        ("  = harvested", round(cons["harvested"], 0)),
        ("  + standing (incl FW + 6N hold)", round(cons["standing"], 0)),
        ("  + mortality", round(cons["mortality"], 0)),
        ("  + cull", round(cons["cull"], 0)),
        ("  residual_pct", f"{cons['residual_pct']:.4f}%"),
        ("SPECIFIC-TANK PICK", gf.TANKPICK_STAMP),
        ("tank_to_tank_transfers", gft.n_transfers),
        ("tank_week_rows", len(gft.batch_locations)),
        ("over_subscribed_weeks",
         f"{len(gft.oversub_weeks)} (double-stacked {gft.n_oversub_rows} rows): "
         + (", ".join(gft.oversub_weeks) if gft.oversub_weeks else "none")),
        ("tank_continuity", tank_drift_note),
    ]
    for i, (k, v) in enumerate(rows, start=2):
        ws[f"A{i}"] = k
        ws[f"B{i}"] = v
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 56

    # ---- FULL sheets (the L1/L3 layers support these). ----
    write_harvest_plan_output(wb, hv, default_hog_yield=hog,
                              facility_limits_hog={})
    write_harvest_plan_report(wb, hv, scenario_name=control.scenario_name,
                              default_hog_yield=hog, facility_limits_hog={},
                              forecast_start=fs_date)
    write_harvest_report(wb, hv, default_hog_yield=hog,
                         facility_limits_hog={}, forecast_start=fs_date)
    write_batch_plan(wb, bl, hv, default_hog_yield=hog)
    write_feed_forecast_weekly(wb, bl, fw_states_by_batch, fs_date, tables,
                               batch_by_id)
    write_feed_forecast_monthly(wb, bl, fw_states_by_batch, fs_date, tables,
                                batch_by_id)
    write_advisory(wb, bl, hv, facility_limits, control,
                   batches=batch_by_id, tables=tables)
    write_weekly_report(wb, bl, hv, list(gft.fw_states),
                        batches=batch_by_id, tables=tables,
                        scenario_name=control.scenario_name, hog_yield=hog,
                        tranog_events=gft.tranog_events,
                        og_mort_states=gft.mort_states)
    write_monthly_report(wb, bl, hv, list(gft.fw_states),
                         batches=batch_by_id, tables=tables,
                         scenario_name=control.scenario_name, hog_yield=hog,
                         forecast_start=fs_date,
                         tranog_events=gft.tranog_events,
                         og_mort_states=gft.mort_states)

    # ---- L1-native StandingTrace + ReconciliationReport (the conservation). ----
    _write_standing_trace(wb, gft, control)
    _write_reconciliation(wb, gft, cons)

    # ---- REAL specific-tank sheets (step #2): physical tanks + transfers. ----
    write_facility_map(wb, bl, facility, batches=batch_by_id, tables=tables)
    write_batch_locations(wb, bl)
    write_transfer_plan_output(wb, gft.transfer_events,
                               tranog_events=gft.tranog_events, grade_events=[])
    # The REAL continuity audit over the emitted BatchLocations + Transfers +
    # TranOG + Harvest events (proves 0 TANK_DRIFT / 0 BIO_DRIFT).
    write_tank_continuity_audit(
        wb, bl, gft.mort_states, hv, gft.transfer_events,
        grade_events=[], tranog_events=gft.tranog_events,
        initial_state=initial_state,
        realized_biology=gft.realized_biology)

    # Order: RunConfig first.
    wb.move_sheet("RunConfig", -(wb.sheetnames.index("RunConfig")))
    wb.save(out_path)
    wb.close()


def _scan_audit_drift(ws):
    """Scan a written TankContinuityAudit sheet for TANK_DRIFT / BIO_DRIFT flags
    + the facility count signed/abs totals. Columns (1-based): Flag=15,
    Bio_Flag=28; the facility summary row 'Count (fish)' carries signed/abs."""
    n_tank = n_bio = 0
    fac_signed = fac_abs = 0.0
    for row in ws.iter_rows(values_only=True):
        if not row:
            continue
        if len(row) >= 28:
            if row[14] == "TANK_DRIFT":
                n_tank += 1
            if row[27] == "BIO_DRIFT":
                n_bio += 1
        if row and row[0] == "Count (fish)" and len(row) >= 3:
            try:
                fac_signed = float(row[1] or 0.0)
                fac_abs = float(row[2] or 0.0)
            except (TypeError, ValueError):
                pass
    return n_tank, n_bio, fac_signed, fac_abs


def _write_standing_trace(wb, gft, control) -> None:
    ws = wb.create_sheet("StandingTrace")
    ws.append(["FACILITY STANDING TRACE (L1; TRUE total = FW + OG + 6N purge)"])
    ws.append([gf.METHOD_STAMP])
    ws.append([])
    ws.append(["Week", "Week_Label", "Standing_TOTAL (kg)", "Biomass_Cap (kg)",
               "Feed (kg/day)", "Feed_Cap (kg/day)", "FW (kg)", "OG (kg)",
               "Purge_6N (kg)", "Harvested (kg)", "Legal", "Binding"])
    for r in gft.trace:
        ws.append([r.week, r.week_label, round(r.standing_biomass_kg, 0),
                   round(r.biomass_cap, 0), round(r.feed_kg_day, 0),
                   round(r.feed_cap, 0), round(r.fw_biomass_kg, 0),
                   round(r.og_biomass_kg, 0), round(r.purge_biomass_kg, 0),
                   round(r.harvested_kg, 0), r.legal, r.binding])
    for c, w in {1: 6, 2: 11, 3: 19, 4: 16, 5: 14, 6: 17, 7: 12, 8: 12,
                 9: 14, 10: 14, 11: 7, 12: 10}.items():
        from openpyxl.utils import get_column_letter
        ws.column_dimensions[get_column_letter(c)].width = w


def _write_reconciliation(wb, gft, cons) -> None:
    ws = wb.create_sheet("ReconciliationReport")
    ws.append(["RECONCILIATION REPORT (L1 conservation; FW counted in the total)"])
    ws.append([gf.METHOD_STAMP])
    ws.append(["Per batch: seeded == harvested + standing + mortality + cull. "
               "Standing@horizon folds any fish still in the 6N purge hold."])
    ws.append([])
    ws.append(["Batch", "Seeded", "Harvested", "Standing", "Mortality", "Cull",
               "Accounted", "Residual", "Residual_%"])
    for bid in sorted(gft.conservation):
        c = gft.conservation[bid]
        ws.append([bid, round(c["seeded_count"], 0),
                   round(c["harvested_count"], 0), round(c["standing_count"], 0),
                   round(c["mortality_count"], 0), round(c["cull_count"], 0),
                   round(c["accounted_count"], 0), round(c["residual_count"], 3),
                   round(c["residual_pct"], 4)])
    ws.append([])
    ws.append(["FACILITY", round(cons["seeded"], 0), round(cons["harvested"], 0),
               round(cons["standing"], 0), round(cons["mortality"], 0),
               round(cons["cull"], 0), round(cons["accounted"], 0),
               round(cons["residual"], 3), round(cons["residual_pct"], 4)])
    from openpyxl.utils import get_column_letter
    for c in range(1, 10):
        ws.column_dimensions[get_column_letter(c)].width = 12


if __name__ == "__main__":
    raise SystemExit(main())
