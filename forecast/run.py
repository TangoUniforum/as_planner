"""Entry point for the forecast pipeline."""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from .biology import (
    project_all_batches, project_in_flight_batch, project_in_flight_fw_batch,
)
from .harvest_scheduler import schedule_harvests, summarize_demands
from .placement import run_placement, summarize_placement
from .precalc import build_precalc_canvas, print_canvas_summary
from .sixn import is_purge_mode
from .caps import (
    apply_facility_buffer,
    read_facility_limits,
    read_system_limits,
    resolve_facility_cap,
    METRIC_BIOMASS,
    METRIC_FEED_DAY,
    METRIC_MAX_HARVEST,
    METRIC_MIN_HARVEST,
)
from .excel_io import (
    load_workbook,
    read_batches,
    read_biology_tables,
    read_control,
    read_facility_config,
    read_pinned_harvests,
    read_pinned_transfers,
    write_advisory,
    write_control_status,
    write_validation_log,
    write_batch_locations,
    write_biology_projection,
    write_calibration_diagnostics,
    write_daily_harvest_schedule,
    write_facility_map,
    write_feed_forecast_monthly,
    write_feed_forecast_weekly,
    write_harvest_plan_output,
    write_harvest_report,
    write_monthly_report,
    write_reconciliation_report,
    write_tank_continuity_audit,
    write_transfer_plan_output,
    write_weekly_report,
)
from .caps import METRIC_HOG_YIELD
from .production_report import (
    hydrate_facility_state,
    read_production_report,
    summarize_fw_records,
    summarize_hydration,
)
from .state import FacilityState


def main(workbook_path: str | Path | None = None) -> int:
    path = Path(workbook_path or Path(__file__).resolve().parent.parent / "Forecast.xlsm")
    t0 = time.time()
    print(f"Loading {path} ...")
    wb = load_workbook(path)

    control = read_control(wb)
    batches = read_batches(wb)
    tables = read_biology_tables(wb)
    facility = read_facility_config(wb)

    print(f"  Control: scenario={control.scenario_name}, start={control.forecast_start.date()}, "
          f"horizon={control.horizon_weeks}w, 6N growth={control.sixn_growth}")
    print(f"           handling mortality per transfer = {control.handling_mortality_pct}% "
          f"(= {control.handling_mortality_pct / 100:.6f} fraction)")
    print(f"  Batches: {len(batches)} in registry")
    print(f"  Tables : {len(tables.sgr_size_g)} SGR rows, {len(tables.mortality_pct_weekly)} mortality rows, "
          f"{len(tables.feed_types)} feed types, {len(tables.culling)} cull events")
    print(f"  Tanks  : {len(facility.tanks)}")

    # ----- Hydrate FacilityState from ProductionReport -----
    pr_closing, og_records, fw_records = read_production_report(wb)
    # DESIGN §1 handoff: PR closing date must be exactly forecast_start - 1.
    from datetime import timedelta as _td
    expected_pr_close = control.forecast_start.date() - _td(days=1)
    if pr_closing is None:
        print(f"  WARN: ProductionReport closing date missing/unparseable; "
              f"expected {expected_pr_close} (forecast_start - 1 day)")
    elif pr_closing != expected_pr_close:
        print(f"  WARN: ProductionReport closing {pr_closing} != "
              f"{expected_pr_close} (forecast_start - 1 day); PR state may "
              f"not align with week-0 opening state")
    state = FacilityState.from_facility_config(facility, today=control.forecast_start.date())
    hydration_warns = hydrate_facility_state(state, og_records, batches)
    summary = summarize_hydration(state)
    print(f"\n  ProductionReport: closing {pr_closing}, "
          f"{len(og_records)} OG (batch, tank) rows + {len(fw_records)} FW physical-unit rows")
    print(f"  Hydrated OG state @ {state.today}: {summary['occupied_tanks']}/{summary['total_tanks']} "
          f"tanks occupied, {summary['num_batches_in_facility']} batches in facility, "
          f"total OG biomass {summary['total_biomass_kg']:,.0f} kg")
    for system in sorted(summary["by_system_biomass"]):
        b = summary["by_system_biomass"][system]
        o = summary["by_system_occupied"][system]
        total = len(state.tanks_in_system(system))
        if b > 0 or o > 0:
            print(f"    {system:>5}: {b:>10,.0f} kg  in {o}/{total} tanks")
    fw_rolled = summarize_fw_records(fw_records)
    if fw_rolled:
        print(f"  FW in-flight rollup (not in TankState; representation TBD):")
        per_system: dict[str, dict] = {}
        for (batch, system), info in fw_rolled.items():
            e = per_system.setdefault(system, {"count": 0.0, "biomass_kg": 0.0, "batches": set(), "units": 0})
            e["count"] += info["count"]
            e["biomass_kg"] += info["biomass_kg"]
            e["batches"].add(batch)
            e["units"] += info["units"]
        for system, info in sorted(per_system.items()):
            print(f"    {system:>5}: {info['biomass_kg']:>10,.0f} kg, "
                  f"{info['count']:>10,.0f} fish across {info['units']} units "
                  f"in batches {sorted(info['batches'])}")
    for w in hydration_warns:
        print(f"  WARN: {w}")
    inv_warns = state.check_invariants(min_tank_control=control.min_tank_control)
    if inv_warns:
        print(f"  Invariant violations at hydration ({len(inv_warns)}):")
        for w in inv_warns:
            print(f"    - {w}")

    # ----- Caps + pinned plans -----
    fs_date = control.forecast_start.date() if hasattr(control.forecast_start, "date") else control.forecast_start
    facility_limits = read_facility_limits(wb, fs_date)
    system_limits = read_system_limits(wb, fs_date)
    pinned_harvests = read_pinned_harvests(wb, fs_date)
    pinned_transfers = read_pinned_transfers(wb, fs_date)
    print(f"\n  Caps + pinned plans:")
    print(f"    FacilityLimits overrides: {len(facility_limits.overrides)}")
    print(f"    SystemLimits caps:        {len(system_limits.caps)}")
    print(f"    Pinned harvests:          {len(pinned_harvests)} (honored as hard constraints)")
    print(f"    Pinned transfers:         {len(pinned_transfers)} "
          f"({'NOT YET HONORED — see warning below' if pinned_transfers else 'none'})")
    if pinned_transfers:
        print(f"  WARN: {len(pinned_transfers)} TransferPlan pin(s) detected but "
              f"placement does not yet honor them as hard constraints. The "
              f"planner re-decides all transfers from scratch. Pin rows are "
              f"preserved at the top of TransferPlan for visibility; the "
              f"planner-emitted rows follow below them.")
    print(f"    Control R24 deviation:    ±{control.facility_biomass_deviation_pct*100:.1f}% (biomass + feed)")
    print(f"    Control R29 global buf:   ±{control.global_buffer_pct*100:.1f}% (system caps)")
    print(f"    Default TranOG tanks:     {control.tran_og_default_tanks}")
    print(f"    Starvation period:        {control.starvation_period_days} days (6N production mode)")
    # Show resolved facility caps for the first forecast week.
    from .time_grid import forecast_week_labels as _fw_labels
    first_label = _fw_labels(fs_date, 1)[0]
    bio_cap = resolve_facility_cap(METRIC_BIOMASS, first_label, facility_limits, control)
    feed_cap = resolve_facility_cap(METRIC_FEED_DAY, first_label, facility_limits, control)
    mx_hv = resolve_facility_cap(METRIC_MAX_HARVEST, first_label, facility_limits, control)
    mn_hv = resolve_facility_cap(METRIC_MIN_HARVEST, first_label, facility_limits, control)
    print(f"    {first_label} facility caps (resolved):")
    if bio_cap:
        lo, hi = apply_facility_buffer(bio_cap, METRIC_BIOMASS, control)
        print(f"      biomass:  {bio_cap:>10,.0f} kg  band [{lo:,.0f}, {hi:,.0f}]")
    if feed_cap:
        lo, hi = apply_facility_buffer(feed_cap, METRIC_FEED_DAY, control)
        print(f"      feed/day: {feed_cap:>10,.0f} kg  band [{lo:,.0f}, {hi:,.0f}]")
    if mn_hv and mx_hv:
        print(f"      harvest count: [{mn_hv:,.0f}, {mx_hv:,.0f}]  (strict)")

    # Batches already represented in PR hydration are tracked via the
    # in-flight projection — exclude them from the incoming-batch
    # projection to avoid double-counting. Two kinds of in-flight:
    #   - OG-in-flight: have OG tanks in PR. Projected via
    #     project_in_flight_batch (SW phase only, anchored to PR
    #     OG state).
    #   - FW-in-flight: have FW physical-unit records in PR but no OG
    #     tanks yet. Projected via project_in_flight_fw_batch (FW phase
    #     anchored to PR FW state; handles FW→SW transition at TranOG).
    # Both use input_date for MODEL lookups (mortality, SGR/FCR curves)
    # but anchor STATE (count, biomass, avg_wt) to PR-measured values.
    og_in_flight_ids = {t.batch_id for t in state.tanks_by_id.values() if t.batch_id}
    fw_in_flight_aggregates: dict[str, dict] = {}
    for r in fw_records:
        e = fw_in_flight_aggregates.setdefault(r.batch_id, {"count": 0.0, "biomass_kg": 0.0})
        e["count"] += r.closing_count
        e["biomass_kg"] += r.closing_biomass_kg
    fw_in_flight_ids = {
        bid for bid, agg in fw_in_flight_aggregates.items()
        if agg["count"] > 0 and bid not in og_in_flight_ids
    }
    in_flight_ids = og_in_flight_ids | fw_in_flight_ids
    incoming_batches = [b for b in batches if b.batch_id not in in_flight_ids]
    states, residuals, splits, warnings = project_all_batches(incoming_batches, tables, control)
    print(f"\n  Projected {len(states)} batch-week rows across {len({s.batch_id for s in states})} batches (incoming)")
    print(f"  TranOG size-class splits captured: {len(splits)}")
    for w in warnings:
        print(f"  WARN: {w}")

    # ----- In-flight batches: forward-project per batch using PR-hydrated state -----
    batch_by_id = {b.batch_id: b for b in batches}
    in_flight_states: list = []
    # OG-in-flight projection (anchored to PR OG tank state).
    for batch_id, tank_list in [(bid, state.tanks_for_batch(bid)) for bid in og_in_flight_ids]:
        b_meta = batch_by_id.get(batch_id)
        if b_meta is None:
            continue
        total_count = sum(t.count for t in tank_list)
        total_biomass = sum(t.biomass_kg for t in tank_list)
        if total_count <= 0:
            continue
        agg_avg_wt = total_biomass * 1000.0 / total_count
        agg_cv = tank_list[0].cv_pct if tank_list else 16.0
        in_flight_states.extend(
            project_in_flight_batch(b_meta, tables, control, total_count, agg_avg_wt, agg_cv)
        )
    # FW-in-flight projection (anchored to PR FW physical-unit state).
    fw_in_flight_residuals: list = []
    fw_in_flight_splits: list = []
    for batch_id in sorted(fw_in_flight_ids):
        b_meta = batch_by_id.get(batch_id)
        if b_meta is None:
            continue
        agg = fw_in_flight_aggregates[batch_id]
        if agg["count"] <= 0:
            continue
        avg_wt_g = agg["biomass_kg"] * 1000.0 / agg["count"]
        fw_states, fw_resids, fw_splits = project_in_flight_fw_batch(
            b_meta, tables, control, agg["count"], avg_wt_g, pr_closing
        )
        in_flight_states.extend(fw_states)
        fw_in_flight_residuals.extend(fw_resids)
        fw_in_flight_splits.extend(fw_splits)
    residuals.extend(fw_in_flight_residuals)
    splits.extend(fw_in_flight_splits)
    in_flight_batches = sorted({s.batch_id for s in in_flight_states})
    print(f"  In-flight projection: {len(in_flight_states)} batch-week rows across "
          f"{len(in_flight_batches)} batches {in_flight_batches}")
    if fw_in_flight_ids:
        print(f"    FW-in-flight (PR-anchored): {sorted(fw_in_flight_ids)}")

    # ----- Layer 2: harvest scheduler -----
    states_by_batch: dict[str, list] = {}
    for s in states + in_flight_states:
        states_by_batch.setdefault(s.batch_id, []).append(s)
    # Precalc the achievable biomass trajectory under min-only harvest.
    # The scheduler tracks this curve instead of chasing the unachievable
    # facility cap when carrying capacity is the binding constraint.
    from .harvest_scheduler import project_biomass_under_min_only
    biomass_projection = project_biomass_under_min_only(
        states_by_batch, batch_by_id, control, facility_limits,
    )
    demands, sched_warns = schedule_harvests(
        states_by_batch, batch_by_id, pinned_harvests, control, facility_limits,
        projected_biomass=biomass_projection,
    )
    summary_d = summarize_demands(demands)
    print(f"\n  Harvest scheduler: {summary_d['rows']} demand rows, "
          f"total {summary_d['total_count']:,.0f} fish, "
          f"{summary_d['total_biomass_kg']:,.0f} kg")
    if summary_d["by_source"]:
        print(f"    by source:")
        for src, info in sorted(summary_d["by_source"].items()):
            print(f"      {src:<18}: {info['rows']:>4} rows, "
                  f"{info['count']:>10,.0f} fish, {info['biomass_kg']:>10,.0f} kg")
    weeks_with_demand = sorted(summary_d["by_week"].keys())
    if weeks_with_demand:
        print(f"    weeks with demand: {weeks_with_demand[0]}..{weeks_with_demand[-1]} "
              f"({len(weeks_with_demand)} weeks)")
        print(f"    first weeks:")
        for lbl in weeks_with_demand[:5]:
            info = summary_d["by_week"][lbl]
            print(f"      {lbl}: {info['count']:>10,.0f} fish, {info['biomass_kg']:>10,.0f} kg "
                  f"({info['rows']} batches)")
    for w in sched_warns[:10]:
        print(f"  SCHED-WARN: {w}")
    if len(sched_warns) > 10:
        print(f"  ... ({len(sched_warns) - 10} more scheduler warnings)")

    # ----- Stage 1: precalc canvas (deterministic landscape) -----
    #
    # 2-pass PR_CORRECTION evaluator (Q-COORD.L): build the canvas
    # once to discover PR-over-concentrated candidates, then for each
    # candidate (worst-first) test whether claiming a tank improves
    # the violation count. Accept only strict improvements. Final
    # canvas is built with the accepted set. Aligns with precalc-first:
    # planner acts when acting produces a better plan, advises otherwise.
    purge = is_purge_mode(control, fs_date)

    def _build_and_place(allowed):
        """Build canvas + run placement with `allowed` PR_CORRECTION set."""
        c = build_precalc_canvas(
            control=control, batches=batches, tables=tables,
            facility=facility, facility_limits=facility_limits,
            system_limits=system_limits,
            biology_states_by_batch=states_by_batch, splits=splits,
            harvest_demands=demands, pinned_harvests=pinned_harvests,
            pinned_transfers=pinned_transfers, initial_state=state,
            projected_biomass_by_week=biomass_projection,
            allowed_pr_corrections=allowed,
        )
        p, fs = run_placement(
            state, batch_by_id, states_by_batch, demands, splits,
            system_limits, control, facility, tables,
            migration_plan=c.migration_plan,
        )
        # Count violations: per-tank density > tank cap, OG6N excluded
        # in purge mode (depuration pool intentionally uncapped).
        tank_cap = {t.tank_id: t.max_density_kg_m3 for t in facility.tanks}
        tank_sys = {t.tank_id: t.system_id for t in facility.tanks}
        viols = []
        for r in p.batch_locations:
            cap = tank_cap.get(r.tank_id, 0.0)
            if cap <= 0:
                continue
            if (tank_sys.get(r.tank_id) == "OG6N"
                    and is_purge_mode(control, r.week_start)):
                continue
            if r.density_kg_m3 > cap:
                viols.append(r.density_kg_m3)
        return c, p, fs, len(viols), max(viols, default=0.0)

    # Probe run to discover candidates.
    canvas_probe, placement_probe, final_state_probe, viols_probe, worst_probe = (
        _build_and_place(set())
    )
    candidates = list(canvas_probe.pr_correction_candidates)

    if not candidates:
        print(f"\n  PR_CORRECTION evaluator: no over-concentrated PR cohorts; "
              f"advisory-only mode (baseline {viols_probe} viols / "
              f"{worst_probe:.1f} worst).")
        canvas = canvas_probe
        placement = placement_probe
        final_state = final_state_probe
        accepted_pr_corrections: set[str] = set()
    else:
        print(f"\n  PR_CORRECTION evaluator (Q-COORD.L 2-pass): "
              f"{len(candidates)} candidate(s)")
        print(f"    baseline (no actions): {viols_probe} viols / "
              f"{worst_probe:.1f} worst")
        accepted_pr_corrections = set()
        best_canvas = canvas_probe
        best_placement = placement_probe
        best_final_state = final_state_probe
        best_viols = viols_probe
        best_worst = worst_probe
        for bid in candidates:
            trial_allowed = accepted_pr_corrections | {bid}
            tc, tp, tfs, tv, tw = _build_and_place(trial_allowed)
            if tv < best_viols:
                print(f"    +{bid}: {tv} viols / {tw:.1f} worst  ->ACCEPT "
                      f"(was {best_viols})")
                accepted_pr_corrections = trial_allowed
                best_canvas, best_placement, best_final_state = tc, tp, tfs
                best_viols, best_worst = tv, tw
            else:
                print(f"    +{bid}: {tv} viols / {tw:.1f} worst  ->reject "
                      f"(no improvement over {best_viols})")
        if accepted_pr_corrections:
            print(f"    accepted: {sorted(accepted_pr_corrections)}; "
                  f"final {best_viols} viols / {best_worst:.1f} worst")
        else:
            print(f"    no corrections net-positive; advisory-only mode "
                  f"({best_viols} viols / {best_worst:.1f} worst)")
        canvas = best_canvas
        placement = best_placement
        final_state = best_final_state
    print_canvas_summary(canvas)

    # ----- Stage 2: placement (already run above by the evaluator) -----
    print(f"\n  Placement walk ({'6N=purge' if purge else '6N=production'}) [Stage 2 WIP]:")
    p_summary = summarize_placement(placement, final_state)
    print(f"    Phase A load rows:      {p_summary['load_rows']:>4}")
    print(f"    Phase B sys assigns:    {p_summary['system_assignments']:>4}")
    print(f"    Phase C tank assigns:   {p_summary['tank_assignments']:>4}")
    print(f"    Phase D events:")
    print(f"      TranOG entries:       {p_summary['tranog_events']:>4}  "
          f"({p_summary['tranog_fish_placed']:,.0f} fish placed)")
    print(f"      Transfers:            {p_summary['transfer_events']:>4}")
    print(f"      Harvests:             {p_summary['harvest_events']:>4}  "
          f"({p_summary['harvest_count_total']:,.0f} fish, "
          f"{p_summary['harvest_kg_total']:,.0f} kg)")
    print(f"    BatchLocations rows:    {p_summary['location_rows']:>4}")
    print(f"    End-of-horizon: {p_summary['end_state_occupied_tanks']} tanks occupied, "
          f"{p_summary['end_state_biomass_kg']:,.0f} kg biomass remaining")
    for system in sorted(p_summary["end_state_biomass_by_system"]):
        b = p_summary["end_state_biomass_by_system"][system]
        if b > 0:
            occupied = sum(1 for t in final_state.tanks_in_system(system) if not t.is_empty)
            total = len(final_state.tanks_in_system(system))
            print(f"      {system:>5}: {b:>10,.0f} kg in {occupied}/{total} tanks")
    for w in placement.warnings[:10]:
        print(f"    PLACE-WARN: {w}")
    if len(placement.warnings) > 10:
        print(f"    ... ({len(placement.warnings) - 10} more placement warnings)")

    # ----- Facility-wide fish accounting (FW culls + OG harvests) -----
    cull_count_total = sum(s.cull_count_week for s in states)
    cull_biomass_total = sum(s.cull_biomass_kg_week for s in states)
    cull_count_in_flight = sum(s.cull_count_week for s in in_flight_states)
    cull_biomass_in_flight = sum(s.cull_biomass_kg_week for s in in_flight_states)
    print(f"\n  Cull totals (FW-side biology: scheduled bottom culls + "
          f"TranOG handling-mort + reconciliation cull):")
    print(f"    Incoming-batch culls:   {cull_count_total:>12,.0f} fish  "
          f"({cull_biomass_total:>10,.1f} kg)")
    if cull_count_in_flight > 0:
        print(f"    In-flight-batch culls:  {cull_count_in_flight:>12,.0f} fish  "
              f"({cull_biomass_in_flight:>10,.1f} kg)")
    print(f"    Total culled:           {cull_count_total + cull_count_in_flight:>12,.0f} fish  "
          f"({cull_biomass_total + cull_biomass_in_flight:>10,.1f} kg)")
    print(f"    Per-(week, batch) breakdown in BiologyProjection / WeeklyReport / MonthlyReport.")

    # Calibration summary.
    if residuals:
        print("\n  FW calibration (projected pre-cull avg wt at TranOG vs target; suggested correction lands batch on target):")
        print(f"  {'Batch':<6} {'TranOG':<11} {'Target_g':>9} {'CurFW':>6} {'Projected_g':>12} {'Residual_%':>11} {'SugFW':>7}")
        for r in residuals:
            sug = f"{r.suggested_fw_correction:.3f}" if r.suggested_fw_correction is not None else "  --  "
            print(f"  {r.batch_id:<6} {r.tran_og_date.date()} "
                  f"{r.target_avg_wt_g:>9.2f} {r.current_fw_correction:>6.3f} "
                  f"{r.projected_pre_cull_avg_wt_g:>12.2f} "
                  f"{r.residual_pct:>10.2f}% {sug:>7}")

    write_biology_projection(wb, states + in_flight_states)
    write_calibration_diagnostics(wb, residuals)

    # Write the plan outputs from Stage 2 placement.
    write_batch_locations(wb, placement.batch_locations)
    # Density violations enumerated from BatchLocations vs per-tank cap.
    # OG6N is excluded in purge mode (no biomass cap on depuration tanks).
    from .sixn import is_purge_mode as _is_purge_mode
    tank_cap_by_id = {t.tank_id: t.max_density_kg_m3 for t in facility.tanks}
    tank_sys_by_id = {t.tank_id: t.system_id for t in facility.tanks}
    density_violations = []
    for r in placement.batch_locations:
        cap = tank_cap_by_id.get(r.tank_id, 0.0)
        if cap <= 0:
            continue
        # OG6N skipped when that week is in purge mode (state.check_invariants
        # mirrors this carve-out at hydration time).
        if (tank_sys_by_id.get(r.tank_id) == "OG6N"
                and _is_purge_mode(control, r.week_start)):
            continue
        if r.density_kg_m3 > cap:
            density_violations.append(
                (r.week_label, r.location_id, r.batch_id, r.density_kg_m3, cap)
            )
    # Per-week HOG yield overrides from FacilityLimits.
    facility_hog_overrides = {
        wk_label: y
        for (wk_label, metric), y in facility_limits.overrides.items()
        if metric == METRIC_HOG_YIELD
    }
    write_harvest_plan_output(
        wb, placement.harvest_events,
        default_hog_yield=control.default_hog_yield,
        facility_limits_hog=facility_hog_overrides,
        pinned_harvests=pinned_harvests,
    )
    write_transfer_plan_output(
        wb, placement.transfer_events, placement.tranog_events,
        grade_events=placement.grade_events,
        pinned_transfers=pinned_transfers,
    )
    advisory_kwargs = dict(
        residuals=residuals,
        placement_warnings=placement.warnings,
        scheduler_warnings=sched_warns,
        bottlenecks=canvas.bottlenecks,
        density_violations=density_violations,
        invariant_warnings=list(hydration_warns) + list(inv_warns),
    )
    write_advisory(wb, **advisory_kwargs)
    write_validation_log(wb, **advisory_kwargs)
    write_daily_harvest_schedule(
        wb, placement.harvest_events, fs_date,
        default_hog_yield=control.default_hog_yield,
        facility_limits_hog=facility_hog_overrides,
    )
    write_harvest_report(
        wb, placement.harvest_events,
        default_hog_yield=control.default_hog_yield,
        facility_limits_hog=facility_hog_overrides,
    )
    write_feed_forecast_weekly(wb, states_by_batch, fs_date)
    write_feed_forecast_monthly(wb, states_by_batch, fs_date)
    all_states = states + in_flight_states
    write_weekly_report(wb, placement.batch_locations, placement.harvest_events, all_states)
    write_monthly_report(wb, placement.batch_locations, placement.harvest_events, all_states)
    write_reconciliation_report(
        wb,
        placement.batch_locations,
        all_states,
        placement.harvest_events,
        placement.tranog_events,
        state,
    )
    write_tank_continuity_audit(
        wb,
        placement.batch_locations,
        all_states,
        placement.harvest_events,
        placement.transfer_events,
        placement.grade_events,
        placement.tranog_events,
        state,
    )
    write_facility_map(wb, placement.batch_locations, facility)

    # Run summary back to Control R8-R16 (DESIGN §1) — operator's
    # in-workbook signal that the run completed + a snapshot of scope.
    elapsed = time.time() - t0
    total_warnings = (
        len(residuals) + len(canvas.bottlenecks)
        + len(sched_warns) + len(placement.warnings)
        + len(density_violations) + len(hydration_warns) + len(inv_warns)
    )
    status = "ok" if total_warnings == 0 else "warn"
    og_tank_count = sum(1 for t in facility.tanks if t.type == "OG")
    write_control_status(
        wb,
        status=status,
        scenario=control.scenario_name,
        forecast_start=control.forecast_start,
        horizon_weeks=control.horizon_weeks,
        batches=len(batches),
        og_tanks=og_tank_count,
        elapsed_s=elapsed,
        warnings=total_warnings,
    )

    wb.save(path)
    wb.close()
    print(f"\nSaved workbook {path}  ({elapsed:.2f}s, status={status}, "
          f"warnings={total_warnings})")
    return 0


def _cli():
    p = argparse.ArgumentParser()
    p.add_argument("--workbook", default=None)
    a = p.parse_args()
    return main(a.workbook)


if __name__ == "__main__":
    raise SystemExit(_cli())
