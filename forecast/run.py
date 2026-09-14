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
from .tiers import HARVEST_PREP_DENSITY_CAP, effective_density_cap
from .caps import (
    apply_facility_buffer,
    resolve_facility_cap,
    METRIC_BIOMASS,
    METRIC_FEED_DAY,
    METRIC_MAX_HARVEST,
    METRIC_MIN_HARVEST,
)
from .excel_io import (
    load_workbook,
    write_advisory,
    write_control_status,
    write_forecast_start,
    write_validation_log,
    write_batch_locations,
    write_biology_projection,
    write_calibration_diagnostics,
    write_daily_harvest_schedule,
    write_facility_map,
    write_feed_forecast_monthly,
    write_feed_forecast_weekly,
    write_harvest_plan_output,
    write_harvest_plan_report,
    write_yearly_summary,
    write_harvest_report,
    write_monthly_report,
    write_reconciliation_report,
    write_input_conservation_audit,
    write_tank_continuity_audit,
    write_system_limits_audit,
    write_transfer_plan_output,
    write_realization_report,
    write_transfer_template,
    write_batch_plan,
    annotate_batch_plan_handling,
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


def _pr_sheet(wb):
    """ProductionReport worksheet, tolerating name variants (see
    forecast.production_report.find_pr_sheet). Was an exact-string lookup."""
    from .production_report import find_pr_sheet
    return find_pr_sheet(wb)


def main(
    input_path: str | Path | None = None,
    output_path: str | Path | None = None,
    config_dir: str | Path | None = None,
    scenario_dir: str | Path | None = None,
    advance_weeks: int = 0,
    calib_log_path: str | Path | None = None,
) -> int:
    """Run the full forecast pipeline.

    Args:
        input_path: workbook to read. None → default `Forecast.xlsm`
            in the project root.
        output_path: workbook to write. None → write back to input
            (legacy CLI behavior; mutates the source workbook).
            Pass a separate path (e.g. from a UI) to leave the input
            untouched and produce a fresh output file.
        config_dir: stable-config YAML directory (control/biology/facility).
            Defaults to the repo's config/. The source of truth for models +
            control.
        scenario_dir: scenario YAML directory (batches/limits). Defaults to
            the repo's scenario/. The forward batch schedule + caps.

    The workbook supplies ONLY the ProductionReport (current state, and the
    forecast_start derived from its closing date). Everything else comes from
    config_dir + scenario_dir.
    """
    in_path = Path(
        input_path or Path(__file__).resolve().parent.parent / "Forecast.xlsm"
    )
    out_path = Path(output_path) if output_path is not None else in_path
    # Config + scenario YAML are the tracked source of truth; the workbook
    # supplies only the ProductionReport. Default to the repo's config/ +
    # scenario/ when not given.
    _root = Path(__file__).resolve().parent.parent
    config_dir = Path(config_dir) if config_dir is not None else _root / "config"
    scenario_dir = Path(scenario_dir) if scenario_dir is not None else _root / "scenario"
    t0 = time.time()
    print(f"Loading {in_path} ...")
    wb = load_workbook(in_path)
    # Costs & profit (additive, 2026-09-11): drop a CostsAndProfit sheet carried
    # in from an earlier planned workbook before anything else sees the
    # workbook. It is re-written at the end only when config/costs.yaml exists.
    from .costs_report import drop_stale_costs_sheet
    drop_stale_costs_sheet(wb)

    from .config_io import load_config
    from .scenario_io import load_batches as _load_scenario_batches
    control, tables, facility = load_config(config_dir)
    batches = _load_scenario_batches(scenario_dir)
    print(f"  Config:   Control + biology + facility from {config_dir}")
    print(f"  Scenario: {len(batches)} batches from {scenario_dir}")

    # ----- Derive forecast_start from ProductionReport (DESIGN §1) -----
    # Mirrors the VBA DetectForecastStart(): the ProductionReport
    # "Closing Month" is the single source of truth for the forecast
    # start (= closing + 1 day), and the VBA writes that value back into
    # Control B3 on every run. The Python port must do the same
    # derivation rather than trust a possibly-stale Control value —
    # otherwise refreshing only the ProductionReport leaves forecast_start
    # pointing at the prior cycle, and week-0 opening state (hydrated from
    # the new PR snapshot) no longer aligns with the start week. Falls
    # back to the Control sheet value only when PR has no closing date.
    from datetime import datetime as _dt, timedelta as _td
    pr_closing, og_records, fw_records = read_production_report(wb)
    # The elapsed slice of the PR's closing month. Only the two MONTH-shaped
    # report sheets use it, and only when the PR closes mid-month; see
    # production_report.read_pr_period.
    from .production_report import read_pr_period as _read_pr_period
    _pr_period = _read_pr_period(
        _pr_sheet(wb),
        pr_closing)
    _control_start = control.forecast_start
    _ctrl_date = (_control_start.date() if hasattr(_control_start, "date")
                  else _control_start)
    if pr_closing is not None:
        derived_start = _dt(pr_closing.year, pr_closing.month,
                            pr_closing.day) + _td(days=1)
        control.forecast_start = derived_start
        if _ctrl_date != derived_start.date():
            print(f"  ForecastStart: derived {derived_start.date()} from "
                  f"ProductionReport closing {pr_closing} (+1 day); "
                  f"Control sheet had {_ctrl_date} — using derived value "
                  f"(Control value ignored, mirrors VBA DetectForecastStart)")
        else:
            print(f"  ForecastStart: {derived_start.date()} "
                  f"(ProductionReport closing {pr_closing} + 1 day; "
                  f"matches Control)")
    elif _control_start is not None:
        print(f"  WARN - ForecastStart: ProductionReport closing date "
              f"missing/unparseable; falling back to Control value {_ctrl_date}")
    else:
        raise ValueError(
            "No forecast start date found: ProductionReport has no parseable "
            "'Closing Month' header and the Control sheet has no valid "
            "forecast start date. Set one or the other before running."
        )

    print(f"  Control: scenario={control.scenario_name}, start={control.forecast_start.date()}, "
          f"horizon={control.horizon_weeks}w, 6N growth={control.sixn_growth}")
    print(f"           handling mortality per transfer = {control.handling_mortality_pct}% "
          f"(= {control.handling_mortality_pct / 100:.6f} fraction)")
    print(f"  Batches: {len(batches)} in registry")
    print(f"  Tables : {len(tables.sgr_size_g)} SGR rows, {len(tables.mortality_pct_weekly)} mortality rows, "
          f"{len(tables.feed_types)} feed types, {len(tables.culling)} cull events")
    print(f"  Tanks  : {len(facility.tanks)}")

    # ----- Hydrate FacilityState from ProductionReport -----
    # pr_closing / og_records / fw_records were read above, where
    # forecast_start was derived from the PR closing date. By construction
    # pr_closing == forecast_start - 1, so no alignment warning is needed.
    state = FacilityState.from_facility_config(facility, today=control.forecast_start.date())
    hydration_warns = hydrate_facility_state(state, og_records, batches)
    # GRADE EFFICIENCY on the state, so the manual-event handlers and the
    # automatic graded-split path both see one value without either having to
    # thread it through. A real grader does not cut cleanly at the threshold --
    # the two populations overlap near the cut line -- so the clean
    # truncated-normal conditional means are further apart than reality.
    state.grade_efficiency = float(
        getattr(control, "grade_efficiency", 1.0) or 1.0)
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
    # PR FW WEIGHT DERIVED. A freshwater batch the PR gives a COUNT but no
    # BIOMASS seeds the projection at 0 g, and FW growth is MULTIPLICATIVE --
    # so it stays 0 g for its entire freshwater phase. Nothing downstream can
    # recover it, and every symptom appears far from the cause: the FW
    # calibration reports residual -100% / "did not converge", the batch's FW
    # biomass and size-class split are fiction, and the TranOG reconcile to
    # tran_og_count has no size distribution to rank, so before d3e3d43 it
    # culled nobody and the batch entered seawater 24% over plan.
    # Measured on the 2026-08-31 PR: B56, 563,234 fish across 46 hatchery units,
    # every one 0.00 kg (B54 0.56 g, B55 0.21 g, B56 0.00 g -- the youngest
    # batch, whose weight simply is not recorded yet).
    # 842ade4 changed the RESPONSE, not the detection. The batch is no longer
    # left at 0 g: biology.py seeds it from its OWN lifecycle (hatch weight at
    # its tran_sf_date, else input_date + HATCHERY_DAYS, then the FW curve under
    # its own fw_correction) exactly as a batch not yet in the PR is projected.
    # That is not "inventing a weight from the growth table" -- it is the same
    # model the planner already trusts for every un-arrived batch, applied to a
    # batch whose lifecycle dates the PR does give us. Leaving it at 0 g was the
    # worse lie: multiplicative growth froze it at 0 g forever, so it never
    # reached min_harvest_weight_g, was never harvested, and held its tanks for
    # the whole horizon (B56 sat in tanks 14 and 21 from 2027-W32 to 2029-W05).
    # Detect, do not coerce still holds -- the warning below is LOUD, names the
    # batch and unit count, and says in the operator's own terms that these
    # weights are MODELLED, not measured. The data gap is reported, not hidden.
    _fw_zero: dict[str, list] = {}
    for (batch, _system), info in fw_rolled.items():
        if info["count"] > 0 and info["biomass_kg"] <= 0:
            e = _fw_zero.setdefault(batch, [0.0, 0])
            e[0] += info["count"]
            e[1] += info["units"]
    for _b in sorted(_fw_zero):
        _cnt, _units = _fw_zero[_b]
        hydration_warns.append(
            f"PR FW WEIGHT DERIVED — {_b}: {_cnt:,.0f} fish across {_units} "
            f"freshwater unit(s) carry 0 kg in the ProductionReport, so the "
            f"batch's starting weight was derived from its OWN lifecycle "
            f"instead — hatch weight at its tran_sf_date, then the FW curve "
            f"under its fw_correction, exactly as a batch not yet in the PR is "
            f"projected. The plan is sound, but this batch's FW weights are "
            f"MODELLED, not measured: record a weight for those units if you "
            f"want them anchored to reality."
        )
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
    # PR fish no tank receives (a fish group without a Bnn id, a batch roll-up
    # its Unit rows do not hold): named in the ValidationLog, so a forecast
    # opening that differs from the PR's own total is never silent. Detection
    # only -- what is hydrated is unchanged (production_report.
    # pr_structure_warnings).
    try:
        from .production_report import pr_structure_warnings as _pr_struct
        hydration_warns.extend(_pr_struct(_pr_sheet(wb)))
    except Exception as _prs_err:                                  # noqa: BLE001
        hydration_warns.append(f"PR NOT HYDRATED - check failed: {_prs_err!r}")
    for w in hydration_warns:
        print(f"  WARN: {w}")
    # Pass the 6N mode explicitly: R8 exempts 6N only WHILE IT PURGES,
    # and this snapshot is taken at the forecast start.
    inv_warns = state.check_invariants(
        min_tank_control=control.min_tank_control,
        sixn_purge_mode=is_purge_mode(control, control.forecast_start))
    if inv_warns:
        print(f"  Invariant violations at hydration ({len(inv_warns)}):")
        for w in inv_warns:
            print(f"    - {w}")

    # ----- Manual override window (operator-authored) -----
    # Script operations week by week for weeks 1..N: the window EXECUTES your
    # events (transfers/harvests, recorded) + REAL biology, produces the state at
    # week N+1, then the normal pipeline plans forward from there. The OG
    # in-flight projection re-anchors automatically (it reads the advanced
    # `state` below). Window length N is implicit (last week with an event);
    # --advance-weeks can extend it with pure-biology weeks. The prefix weeks are
    # stitched into the output near the writers. See forecast/manual_window.py.
    from datetime import timedelta as _td
    from .manual_events import load_manual_events
    # Events are PR-SPECIFIC: keyed by the PR closing date, which at this
    # point is forecast_start - 1 day (DetectForecastStart contract).
    manual_events = load_manual_events(
        scenario_dir, pr_closing=control.forecast_start - _td(days=1))
    _ev_max_week = max((e.week or 1) for e in manual_events) if manual_events else 0
    window_n = max(advance_weeks or 0, _ev_max_week)
    # The FW in-flight projection must cover the prefix window weeks (1..N) so
    # those weeks get FW growth + feed. It uses the ORIGINAL start + horizon
    # (captured here, before the window shifts control.forecast_start for the OG
    # pipeline). The extra prefix FW states only reach the feed reports — the
    # scheduler/placement key off the shifted (post-window) week labels.
    _fw_proj_fs = control.forecast_start
    _fw_proj_horizon = control.horizon_weeks
    # THE DATE THE REPORT OPENS, which is NOT the date the planner opens.
    # A manual override window shifts control.forecast_start forward by the
    # window length (below), so from here on `fs_date` is the PLANNING start --
    # 2026-09-15 on the 8/31 closing with a two-week window. The report still
    # covers the window weeks, and it opens the day after the PR closes.
    # Operator, 2026-09-08: "the report open is the start of the day after the
    # production report ... so there should be no entries in this document for
    # Aug as it starts on 9/1." Reporting surfaces clip on THIS date; clipping
    # on the shifted one would delete the manual-window weeks outright, which
    # is what retired the earlier clip on working_day_month_split.
    report_start = (_fw_proj_fs.date() if hasattr(_fw_proj_fs, "date")
                    else _fw_proj_fs)
    prefix_realized: dict = {}
    prefix_batch_locations: list = []
    prefix_transfers: list = []
    prefix_harvests: list = []
    prefix_tranogs: list = []
    transferred_fw: set = set()
    manual_fw_balance: dict = {}
    prefix_openings: list = []
    prefix_fw_cull: dict = {}
    manual_warns: list = []
    # The continuity audit anchors each tank's first week to this initial state.
    # The window advances `state` in place to week N+1 (what the pipeline plans
    # from), so the audit must keep the ORIGINAL PR-hydrated anchor — else the
    # prefix weeks (which open from the PR) reconcile against the wrong opening.
    audit_initial_state = state
    # LIMITS AND THE PER-WEEK OG GROWTH FACTOR ARE BOUND *BEFORE* THE WINDOW.
    # The manual override window walks 7 days of real biology per week through
    # biology.advance_tank_one_day, which reads tables.og_sgr_by_week. That
    # binding used to happen ~90 lines BELOW this point, so the window ran with
    # an EMPTY factor dict and og_sgr_factor returned 1.0 for every one of its
    # days. The operator's own per-week settings for exactly those weeks --
    # scenario/limits.yaml, sgr_correction_og 0.90 at 2026-W36 and 0.92 at
    # 2026-W37 -- were therefore completely inert: measured 2026-09-08, forcing
    # both to 0.10 produced a byte-identical workbook across all 2,832
    # BatchLocations rows. Every day the run ever simulates with an ISO label of
    # W36 or W37 lies inside the window, so those two knobs could never fire.
    # The window grew ~8.5% too fast and handed the planner a facility 29,541 kg
    # (0.78%) too heavy. Safe to hoist: load_limits reads only sixn_growth and
    # sixn_production_start from control, neither of which the window changes,
    # and facility_limits comes straight from the file.
    from .scenario_io import load_limits
    facility_limits, system_limits = load_limits(scenario_dir, control)
    from .caps import og_sgr_factors as _og_sgr_factors
    tables.og_sgr_by_week = _og_sgr_factors(facility_limits)
    if window_n > 0:
        # Reject a window that is as long as (or longer than) the whole forecast:
        # the planner needs at least one week after the window to plan, else the
        # post-window horizon silently clamped to 1 (max(1, horizon - window_n))
        # and the run no longer reflected the requested terms. Fail fast.
        if window_n >= control.horizon_weeks:
            raise ValueError(
                f"Manual override window ({window_n} week(s)) is >= the forecast "
                f"horizon ({control.horizon_weeks} week(s)) — no weeks left for "
                f"the planner. Shorten the window (last event week / "
                f"--advance-weeks) to at most {control.horizon_weeks - 1} week(s), "
                f"or extend the horizon.")
        import copy as _copy_aw
        audit_initial_state = _copy_aw.deepcopy(state)
        from .manual_window import advance_facility_window
        from datetime import datetime as _dt_aw
        _bbi = {b.batch_id: b for b in batches}
        _fs0 = (control.forecast_start.date()
                if hasattr(control.forecast_start, "date") else control.forecast_start)
        _win = advance_facility_window(
            state, _bbi, tables, _fs0, window_n, events=manual_events,
            control=control, pr_closing=pr_closing, fw_records=fw_records)
        prefix_realized = _win["realized_biology"]
        prefix_batch_locations = _win["batch_locations"]
        prefix_openings = _win.get("opening_locations") or []
        prefix_fw_cull = _win.get("manual_fw_cull") or {}
        prefix_transfers = _win["transfer_events"]
        prefix_harvests = _win["harvest_events"]
        prefix_tranogs = _win["tranog_events"]
        transferred_fw = _win["transferred_fw_batches"]
        manual_fw_balance = _win.get("manual_fw_balance", {})
        manual_warns = _win["warnings"]
        _new_start = _win["new_start"]
        control.forecast_start = _dt_aw(_new_start.year, _new_start.month, _new_start.day)
        control.horizon_weeks = max(1, control.horizon_weeks - window_n)
        print(f"\n  Manual override window: {window_n} week(s) "
              f"({len(prefix_transfers)} transfer + {len(prefix_harvests)} harvest "
              f"event(s), {len(manual_warns)} warning(s)); forecast now opens "
              f"{_new_start} (horizon {control.horizon_weeks}w).")
        for w in manual_warns:
            print(f"    - {w}")

        # Reject-at-entry: a window that opens the forecast PAST an in-flight FW
        # batch's automatic FW->OG entry week would orphan that batch — the
        # window does pure biology (no TranOG), and the FW projection (run from
        # the original start) then emits an OG row before the shifted start that
        # the coordinator never covers, aborting Phase B deep in placement with
        # no output. Fail fast with an actionable message. The operator can
        # shorten the window, or script each crossing as an explicit fw_to_og
        # event (which removes that batch from the auto schedule).
        from .time_grid import og_entry_week_start as _oge_wk_start
        _crossed = set()
        for _r in fw_records:
            _bm = _bbi.get(_r.batch_id)
            if (_bm is None or not _bm.tran_og_date
                    or _r.batch_id in transferred_fw):
                continue
            if _oge_wk_start(_bm.tran_og_date, _fs0) < _new_start:
                _crossed.add((_r.batch_id,
                              _oge_wk_start(_bm.tran_og_date, _fs0)))
        if _crossed:
            _lst = ", ".join(f"{b} (FW->OG ~{d})" for b, d in sorted(_crossed))
            # A batch whose tran_og_date had already passed at the PR close (an
            # overdue wholly-FW batch, or a split with a past date) crosses in
            # week 1, so no window can end before it: its one way through is
            # an fw_to_og in week 1, which the editor offers at the PR's own
            # freshwater count and weight (manual_window.pr_fw_week1_fallback).
            from .batch_order import batch_sort_key as _bsk_cr

            def _d_cr(v):
                return v.date() if hasattr(v, "date") else v
            _past_cr = sorted({b for b, _d in _crossed
                               if _d_cr(_bbi[b].tran_og_date) < _fs0}, key=_bsk_cr)
            _how_cr = ("Add an explicit fw_to_og event for each crossing batch "
                       "(Manual starting events -> FW->OG intake), or shorten the "
                       "window so it ends before those dates.")
            if _past_cr:
                _how_cr += (f" {', '.join(_past_cr)}: its transfer date had already "
                            f"passed at the PR close, so no window can end before "
                            f"it - script its fw_to_og in week 1 (the editor offers "
                            f"it there at the ProductionReport's own freshwater "
                            f"count and weight).")
            raise ValueError(
                f"Manual override window of {window_n} week(s) opens the forecast "
                f"at {_new_start}, PAST the automatic FW->OG entry of: {_lst}. The "
                f"window does not auto-transfer FW batches. {_how_cr}")

    # ----- Caps -----
    fs_date = control.forecast_start.date() if hasattr(control.forecast_start, "date") else control.forecast_start
    # `facility_limits`, `system_limits` and `tables.og_sgr_by_week` are all
    # bound ABOVE, before the manual override window, so the window's own
    # biology sees the operator's per-week growth factors. See the note there.
    # PER-WEEK OG GROWTH FACTOR: on `tables` because tables is the object
    # already threaded to every growth and feed call site; the alternative is a
    # second argument on ~24 signatures where a single miss would desync the
    # projection from the realized walk. Applied SW-only in
    # biology.sgr_pct_per_day, so growth AND the feed derived from it move
    # together. An empty dict (nothing configured) is exactly the old behaviour.
    if tables.og_sgr_by_week:
        _ogs = tables.og_sgr_by_week
        _odd = {w: f for w, f in _ogs.items() if f > 1.5 or f == 0.0}
        print(f"    OG SGR factor:            {len(_ogs)} week(s) "
              f"(range {min(_ogs.values()):.2f}-{max(_ogs.values()):.2f})")
        if _odd:
            # Loud, not silent: 0 stops growth outright and >1.5 is almost
            # certainly a percentage typed as 90 instead of 0.90.
            print(f"      NOTE unusual value(s): {dict(sorted(_odd.items()))} "
                  f"— 1.0 = normal growth, 0.90 = 90% of it")
    # Operator-authored starting events are applied above (manual_events); the
    # workbook upload is the ProductionReport only. The realized closed-loop
    # planner has no separate pin-ingestion path.
    print(f"\n  Caps:")
    print(f"    FacilityLimits overrides: {len(facility_limits.overrides)}")
    print(f"    System defaults:          {len(system_limits.defaults)} "
          f"(+{len(system_limits.mode_defaults)} mode-specific)")
    print(f"    Per-week exceptions:      {len(system_limits.caps)}")
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

    # ----- SPLIT FW/SW BATCH + OVERDUE WHOLLY-FW BATCH (2026-09-11) -----
    # A batch the PR holds partly in freshwater: the exclusion above keeps its
    # FW part out of the freshwater projection, so nothing modelled it. With
    # control.split_batch_fw "auto" (the default) its FW part is projected on
    # its own FW track -- scenario tran_og_date, or the first forecast week once
    # that has passed; culled to the REMAINING target -- merged into the
    # batch's SW stream and landed on its own entry tanks. A wholly-FW batch
    # whose tran_og_date is before the PR close (never placed before: its
    # projection started in seawater) moves in the first forecast week. The
    # classification reads the PR, not the post-window state; a manual fw_to_og
    # wins (transferred_fw). Empty sets -- every PR with no split, every split
    # the operator scripts, and `off` -- change nothing below.
    from . import split_batch as _sb
    _sb_class = _sb.SplitClassification()
    if _sb.mode(control) == "auto":
        _sb_class = _sb.classify(
            og_records, fw_records, transferred_fw=transferred_fw,
            batch_by_id={b.batch_id: b for b in batches},
            forecast_start=_fw_proj_fs)
    # A split batch whose SW part a window emptied is no longer OG-in-flight:
    # it stays on the V1 path (projected as a wholly-FW batch).
    _sb_split = {b for b in _sb_class.split_ids if b in og_in_flight_ids}
    _sb_overdue = {b for b in _sb_class.overdue_ids if b in fw_in_flight_ids}
    _sb_fw_states: dict = {}     # split batch -> its FW-track FW/EGG rows
    _sb_splits: list = []        # their arrivals (one SizeClassSplit each)
    _sb_expected: dict = {}      # auto batch -> fish its transfer places
    _sb_entry: dict = {}         # auto batch -> the split (entry date, counts)
    _sb_entry_row: dict = {}     # past-date split -> its FW track's entry row

    # ----- Optional: auto-calibrate FW growth to hit the transfer target -----
    # When control.auto_calibrate_fw is on, REPLACE each FW batch's fw_correction
    # with the back-solved value that lands its pre-cull avg weight exactly on
    # tran_og_avg_wt_g at its transfer date — the same solve already reported as
    # Suggested_FW_Correction in Diagnostics — applied BEFORE projection so the
    # on-target growth flows through the whole forecast. Clamped to a sane range
    # so the model can't silently assume absurd growth; a clamped batch is flagged.
    fw_calib_warns: list = []
    # Same rewrites, kept as STRUCTURED records so they can outlive the run.
    # The strings above scroll past in stdout and land in the ValidationLog of
    # one workbook; a correction the model has needed every month for six
    # months therefore looked exactly like a one-off. Persisted (below) it
    # becomes a standing model error to fix at source. Purely additive: this
    # list is read by nothing in the pipeline.
    fw_calib_records: list = []

    def _apply_fw_calib(_b, _solved):
        _lo, _hi = control.auto_calibrate_fw_min, control.auto_calibrate_fw_max
        _prev = _b.fw_correction
        if _solved is None:
            fw_calib_warns.append(
                f"AUTO-FW-CALIB {_b.batch_id}: FW correction did not converge; "
                f"kept configured {_prev:.3f}")
            _record_fw_calib(_b, configured=_prev, applied=_prev, solved=None,
                             clamped=False, converged=False, lo=_lo, hi=_hi)
            return
        _app = min(_hi, max(_lo, _solved))
        _b.fw_correction = _app
        _tgt = _b.tran_og_avg_wt_g or 0.0
        _clamped = abs(_app - _solved) > 1e-6
        if _clamped:
            fw_calib_warns.append(
                f"AUTO-FW-CALIB {_b.batch_id}: landing on {_tgt:.0f}g needs "
                f"fw_correction {_solved:.3f} — CLAMPED to {_app:.3f} "
                f"[{_lo:.2f},{_hi:.2f}] (target likely unreachable at this growth)")
        else:
            fw_calib_warns.append(
                f"AUTO-FW-CALIB {_b.batch_id}: fw_correction {_prev:.3f} -> "
                f"{_app:.3f} to land pre-cull on {_tgt:.0f}g at transfer")
        _record_fw_calib(_b, configured=_prev, applied=_app, solved=_solved,
                         clamped=_clamped, converged=True, lo=_lo, hi=_hi)

    # ONE timestamp for the whole run, not one per batch: the drift view counts
    # distinct timestamps as runs, and a per-record clock ticking mid-loop would
    # report a single run as a dozen.
    from datetime import datetime as _dtc
    _fw_calib_ts = _dtc.now().isoformat(timespec="seconds")

    def _record_fw_calib(_b, **kw):
        """Mirror one rewrite into the durable history. Never raises: a
        diagnostic that can break a forecast run is worse than no diagnostic."""
        try:
            from .accuracy import calibration_record
            fw_calib_records.append(calibration_record(
                _b.batch_id,
                ts=_fw_calib_ts,
                target_wt_g=_b.tran_og_avg_wt_g or 0.0,
                pr_closing=pr_closing,
                source="run.main",
                **kw))
        except Exception:  # noqa: BLE001 — see docstring
            pass

    if control.auto_calibrate_fw:
        from .biology import solve_fw_correction as _solve_fw
        for _b in incoming_batches:
            _apply_fw_calib(_b, _solve_fw(_b, tables))

    states, residuals, splits, warnings = project_all_batches(incoming_batches, tables, control)
    print(f"\n  Projected {len(states)} batch-week rows across {len({s.batch_id for s in states})} batches (incoming)")
    print(f"  TranOG size-class splits captured: {len(splits)}")
    for w in warnings:
        print(f"  WARN: {w}")

    # ----- In-flight batches: forward-project per batch using PR-hydrated state -----
    batch_by_id = {b.batch_id: b for b in batches}
    in_flight_states: list = []
    if _sb_split or _sb_overdue:
        import dataclasses as _dc_sb
        _sb_fw_control = _dc_sb.replace(
            control, forecast_start=_fw_proj_fs, horizon_weeks=_fw_proj_horizon)
    # OG-in-flight projection (anchored to PR OG tank state).
    # sorted(): og_in_flight_ids is a SET OF batch_id STRINGS, so iterating it
    # raw put the OG in-flight batches into states_by_batch in hash-seed order
    # (its FW twin 16 lines below has always been sorted). That order reached
    # batch_week_facts and, through it, a tied pick in precalc._relieve_tank_supply
    # -- which is how the whole forecast became irreproducible (2026-09-08).
    for batch_id, tank_list in [(bid, state.tanks_for_batch(bid))
                                for bid in sorted(og_in_flight_ids)]:
        b_meta = batch_by_id.get(batch_id)
        if b_meta is None:
            continue
        total_count = sum(t.count for t in tank_list)
        total_biomass = sum(t.biomass_kg for t in tank_list)
        if total_count <= 0:
            continue
        agg_avg_wt = total_biomass * 1000.0 / total_count
        agg_cv = tank_list[0].cv_pct if tank_list else 16.0
        _og_rows = project_in_flight_batch(
            b_meta, tables, control, total_count, agg_avg_wt, agg_cv)
        if batch_id in _sb_split:
            # The split's FW part: its own FW track (configured fw_correction,
            # no auto-calibration, residuals not reported -- tran_og_avg_wt_g
            # is a whole-batch figure and this is the remainder), merged into
            # ONE SW row per week from its entry week, in this batch's place.
            _fw_st, _fw_res, _fw_sp = _sb.project_fw_track(
                b_meta, tables, _sb_fw_control, _sb_class.fw_count[batch_id],
                _sb_class.fw_kg[batch_id], pr_closing,
                target=_sb.remaining_target(b_meta.tran_og_count,
                                            _sb_class.sw_count[batch_id]))
            _og_rows, _sb_fw_states[batch_id] = _sb.merge_states(_og_rows, _fw_st)
            if not _sb_fw_states[batch_id]:
                # A PAST-DATE split crosses on day 1: no freshwater week, so
                # the input audit balances its FW part on the track's own
                # entry row (its seawater rows are the merged stream).
                _er = next((s for s in _fw_st if s.stage == "SW"), None)
                if _er is not None:
                    _sb_entry_row[batch_id] = _er
            _sb_splits.extend(_fw_sp)
            if _fw_sp:
                _sb_expected[batch_id] = _fw_sp[0].post_cull_count
                _sb_entry[batch_id] = _fw_sp[0]
        in_flight_states.extend(_og_rows)
    # FW-in-flight projection (anchored to PR FW physical-unit state).
    fw_in_flight_residuals: list = []
    fw_in_flight_splits: list = []
    # The batches the freshwater projector actually RAN for -- recorded, not
    # re-derived, for the report layer (held_fw_openings): their PR FW fish are
    # carried by a projection, even one that starts in seawater because the
    # batch's tran_og_date is already past. Read by nothing in the planner.
    fw_projected_ids: set = set()
    for batch_id in sorted(fw_in_flight_ids):
        b_meta = batch_by_id.get(batch_id)
        if b_meta is None:
            continue
        agg = fw_in_flight_aggregates[batch_id]
        if agg["count"] <= 0:
            continue
        avg_wt_g = agg["biomass_kg"] * 1000.0 / agg["count"]
        if control.auto_calibrate_fw:
            from .biology import (solve_inflight_fw_correction as _solve_ifw,
                                  _as_date as _asd)
            _dsi = (_asd(pr_closing) - _asd(b_meta.input_date)).days
            _apply_fw_calib(
                b_meta, _solve_ifw(b_meta, tables, _fw_proj_fs, avg_wt_g, _dsi))
        import dataclasses as _dc_fw
        _fw_control = _dc_fw.replace(
            control, forecast_start=_fw_proj_fs, horizon_weeks=_fw_proj_horizon)
        _b_proj = b_meta
        if batch_id in _sb_overdue:
            # OVERDUE: tran_og_date before the PR close. Under the raw date the
            # projection starts in seawater and emits no arrival -- the batch
            # was never placed. The first forecast week is the earliest the
            # model can honour that date (same rule as a split).
            _b_proj = _sb.fw_track_batch(
                b_meta, forecast_start=_fw_proj_fs,
                target=_sb.remaining_target(b_meta.tran_og_count, 0.0))
        fw_states, fw_resids, fw_splits = project_in_flight_fw_batch(
            _b_proj, tables, _fw_control, agg["count"], avg_wt_g, pr_closing
        )
        if batch_id in _sb_overdue and fw_splits:
            _sb_expected[batch_id] = fw_splits[0].post_cull_count
            _sb_entry[batch_id] = fw_splits[0]
        fw_projected_ids.add(batch_id)
        in_flight_states.extend(fw_states)
        fw_in_flight_residuals.extend(fw_resids)
        fw_in_flight_splits.extend(fw_splits)
    residuals.extend(fw_in_flight_residuals)
    splits.extend(fw_in_flight_splits)
    # Split batches' arrivals, after every other in-flight arrival (natural
    # batch order, from the OG loop's sorted walk). Their FW part is carried by
    # a projection now, so the report layer must neither hold it at the PR
    # figures nor warn that nothing models it.
    splits.extend(_sb_splits)
    fw_projected_ids |= _sb_split
    if _sb_split or _sb_overdue:
        for _b in _sb.sorted_ids(_sb_split | _sb_overdue):
            _e = _sb_entry.get(_b)
            print(f"  SPLIT/OVERDUE FW (split_batch_fw=auto): {_b} "
                  + ("split" if _b in _sb_split else "overdue wholly-FW")
                  + f", PR FW {_sb_class.fw_count.get(_b, 0.0):,.0f} fish -> "
                  + (f"{_e.post_cull_count:,.0f} enter seawater "
                     f"{_sb._as_date(_e.tran_og_date)}"
                     if _e is not None else "no arrival within the horizon"))
    if fw_calib_warns:
        print(f"\n  FW auto-calibration ON — adjusted {len(fw_calib_warns)} batch(es) "
              f"to hit the pre-cull transfer target (Diagnostics residuals -> ~0):")
        for _w in fw_calib_warns:
            print(f"    {_w}")
    if fw_calib_records:
        # Durable history, beside optimize_history.jsonl / adoption_history.jsonl
        # and gitignored like them. Root-anchored rather than CWD-relative so a
        # run launched from anywhere still appends to the one true history.
        #
        # `calib_log_path` exists so a NON-OPERATIONAL run cannot contaminate
        # that history. This is not hypothetical: on 2026-08-20 a synthetic test
        # fixture wrote 83 records into it under a pr_closing that collides with
        # a real one, and they had to be identified and removed by hand. Any
        # caller that is not a real forecast of the real facility — the backtest
        # driver, the reference fixture, a sweep — must pass its own path, or
        # "" to record nothing. Explicit at the call site, so the separation is
        # visible in the code rather than assumed.
        from pathlib import Path as _PathC
        from .accuracy import (append_calibration_log as _append_calib,
                               DEFAULT_CALIB_LOG as _CALIB_LOG)
        if calib_log_path is None:
            _target = str(_PathC(__file__).resolve().parent.parent / _CALIB_LOG)
        else:
            _target = str(calib_log_path)
        if _target:
            _append_calib(fw_calib_records, _target)
        else:
            print(f"  FW calibration history: NOT recorded "
                  f"({len(fw_calib_records)} records suppressed — non-operational run)")
    in_flight_batches = sorted({s.batch_id for s in in_flight_states})
    print(f"  In-flight projection: {len(in_flight_states)} batch-week rows across "
          f"{len(in_flight_batches)} batches {in_flight_batches}")
    if fw_in_flight_ids:
        print(f"    FW-in-flight (PR-anchored): {sorted(fw_in_flight_ids)}")

    # ----- Layer 2: harvest scheduler -----
    states_by_batch: dict[str, list] = {}
    for s in states + in_flight_states:
        states_by_batch.setdefault(s.batch_id, []).append(s)
    # ----- HYBRID: L1 harvest-envelope guide (opt-in via control.hybrid_follow)
    # Runs standalone L1 ONCE, here — after the manual override window, so it
    # sees the post-window facility state the controller will actually plan
    # from. The guide is a per-week harvest QUANTITY the controller aims at;
    # everything it can do is bounded by the existing clamps downstream.
    harvest_guide = None
    if str(getattr(control, "hybrid_follow", "off") or "off").lower() != "off":
        import time as _time
        from .hybrid_guide import build_harvest_guide
        from .placement import fw_addends_by_week
        from .sixn import SIXN_ALL_TANKS
        _og: dict[str, list] = {}
        _pg: dict[str, list] = {}
        for _t in state.tanks_by_id.values():
            if _t.is_empty or not _t.batch_id:
                continue
            _bucket = _pg if _t.tank_id in SIXN_ALL_TANKS else _og
            _e = _bucket.setdefault(_t.batch_id, [0.0, 0.0])
            _e[0] += _t.count
            _e[1] += _t.biomass_kg
        _cv = {b.batch_id: getattr(b, "tran_og_cv", 16.0) for b in batches}
        _guide_inflight_og = {
            b: (c, kg * 1000.0 / c, _cv.get(b, 16.0))
            for b, (c, kg) in _og.items() if c > 0}
        _guide_purge_inflight = {
            b: (c, kg * 1000.0 / c) for b, (c, kg) in _pg.items() if c > 0}
        _guide_fw_inflight = {
            b: (agg["count"], agg["biomass_kg"] * 1000.0 / agg["count"], pr_closing)
            for b, agg in fw_in_flight_aggregates.items()
            if b in fw_in_flight_ids and agg["count"] > 0}
        # WINDOW SEMANTICS: after a manual override window the guide's L1 must
        # not assume unscripted pre-start 6N staging, and the window-close 6N
        # contents carry their release timing (scripted stagings release after
        # the purge hold; untouched PR-start fish from the handoff) — so the
        # envelope agrees with what the realized layer can actually release.
        _guide_purge_schedule = None
        if window_n > 0:
            from .manual_window import sixn_release_schedule
            _fs_orig = (_fw_proj_fs.date() if hasattr(_fw_proj_fs, "date")
                        else _fw_proj_fs)
            _guide_purge_schedule = sixn_release_schedule(
                state, prefix_transfers, _fs_orig, window_n)
        _t0g = _time.time()
        harvest_guide = build_harvest_guide(
            control=control, tables=tables, facility=facility, batches=batches,
            inflight_og=_guide_inflight_og,
            fw_inflight=_guide_fw_inflight,
            purge_inflight=_guide_purge_inflight,
            purge_release_schedule=_guide_purge_schedule,
            manual_window_weeks=window_n,
            fw_by_label=fw_addends_by_week(
                states_by_batch, extra_fw_states=_sb_fw_states or None),
            facility_limits=facility_limits)
        print(f"\n  HYBRID guide (hybrid_follow={control.hybrid_follow}): "
              + (harvest_guide.source if harvest_guide
                 else "UNAVAILABLE — running as the plain controller")
              + f"  [{_time.time() - _t0g:.1f}s]")

    # Precalc the achievable biomass trajectory under min-only harvest.
    # The scheduler tracks this curve instead of chasing the unachievable
    # facility cap when carrying capacity is the binding constraint.
    from .harvest_scheduler import project_biomass_under_min_only
    biomass_projection = project_biomass_under_min_only(
        states_by_batch, batch_by_id, control, facility_limits,
    )
    demands, sched_warns = schedule_harvests(
        states_by_batch, batch_by_id, control, facility_limits,
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
            harvest_demands=demands, initial_state=state,
            projected_biomass_by_week=biomass_projection,
            allowed_pr_corrections=allowed,
        )
        p, fs = run_placement(
            state, batch_by_id, states_by_batch, demands, splits,
            system_limits, control, facility, tables,
            migration_plan=c.migration_plan,
            facility_limits=facility_limits,
            harvest_guide=harvest_guide,
            extra_fw_states=_sb_fw_states or None,
            split_batch_ids=_sb_split or None,
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
            # R8 — the SAME rule as the run's own audit. The old test
            # handled 6N-in-purge but NOT a STARVE tank outside 6N, so the
            # trial evaluator scored legal harvest-prep consolidation as a
            # density violation and tuning was pushed away from it.
            cap = effective_density_cap(cap, tank_sys.get(r.tank_id, ""),
                                        getattr(r, "stage", ""),
                                        is_purge_mode(control, r.week_start))
            if cap != float("inf") and r.density_kg_m3 > cap:
                viols.append(r.density_kg_m3)
        # ZERO-HARVEST weeks (the hard steady-harvest contract): horizon weeks
        # with no harvested fish at all. Ranked ABOVE density in the trial
        # acceptance below — the evaluator used to judge candidates on density
        # viols alone, so it would happily trade an empty harvest week for a
        # lower viol count (measured on the 7.29.26 PR: a trial set with 101
        # viols but 2 empty 2028 weeks beat the 111-viol zero-empty set).
        horizon_weeks = {r.week_label for r in p.batch_locations}
        harvested_weeks: set[str] = set()
        for ev in p.harvest_events:
            if getattr(ev, "count", 0) and ev.count > 0:
                iso = ev.event_date.isocalendar()
                harvested_weeks.add(f"{iso[0]}-W{iso[1]:02d}")
        zero_weeks = len(horizon_weeks - harvested_weeks)
        return c, p, fs, len(viols), max(viols, default=0.0), zero_weeks

    # Probe run to discover candidates.
    (canvas_probe, placement_probe, final_state_probe, viols_probe,
     worst_probe, zeros_probe) = _build_and_place(set())
    candidates = list(canvas_probe.pr_correction_candidates)

    if not candidates:
        print(f"\n  PR_CORRECTION evaluator: no over-concentrated PR cohorts; "
              f"advisory-only mode (baseline {viols_probe} viols / "
              f"{worst_probe:.1f} worst / {zeros_probe} empty wks).")
        canvas = canvas_probe
        placement = placement_probe
        final_state = final_state_probe
        accepted_pr_corrections: set[str] = set()
    else:
        print(f"\n  PR_CORRECTION evaluator (Q-COORD.L 2-pass): "
              f"{len(candidates)} candidate(s)")
        print(f"    baseline (no actions): {viols_probe} viols / "
              f"{worst_probe:.1f} worst / {zeros_probe} empty wks")
        accepted_pr_corrections = set()
        best_canvas = canvas_probe
        best_placement = placement_probe
        best_final_state = final_state_probe
        best_viols = viols_probe
        best_worst = worst_probe
        best_zeros = zeros_probe
        for bid in candidates:
            trial_allowed = accepted_pr_corrections | {bid}
            try:
                tc, tp, tfs, tv, tw, tz = _build_and_place(trial_allowed)
            except RuntimeError as _trial_err:
                # A trial correction set can produce an UNRUNNABLE trajectory
                # (the placement's no-drop abort — e.g. it consumes the entry
                # tank a later TranOG arrival needed). That is a verdict on
                # the CANDIDATE, not on the forecast: reject it and keep the
                # best-so-far plan, exactly like a no-improvement trial. Only
                # the accepted/base configuration must be runnable.
                print(f"    +{bid}: trial UNRUNNABLE -> reject "
                      f"({str(_trial_err)[:120]}...)")
                continue
            # LEXICOGRAPHIC acceptance: the steady-harvest contract (no empty
            # harvest week — a HARD business rule) outranks density quality.
            # A trial that trades an empty week for fewer viols is a worse
            # plan, full stop; a trial that removes an empty week wins even
            # at a higher viol count.
            if (tz, tv) < (best_zeros, best_viols):
                print(f"    +{bid}: {tv} viols / {tw:.1f} worst / {tz} empty "
                      f"->ACCEPT (was {best_viols} viols / {best_zeros} empty)")
                accepted_pr_corrections = trial_allowed
                best_canvas, best_placement, best_final_state = tc, tp, tfs
                best_viols, best_worst, best_zeros = tv, tw, tz
            else:
                print(f"    +{bid}: {tv} viols / {tw:.1f} worst / {tz} empty "
                      f"->reject (no improvement over {best_viols} viols / "
                      f"{best_zeros} empty)")
        if accepted_pr_corrections:
            print(f"    accepted: {sorted(accepted_pr_corrections)}; "
                  f"final {best_viols} viols / {best_worst:.1f} worst / "
                  f"{best_zeros} empty wks")
        else:
            print(f"    no corrections net-positive; advisory-only mode "
                  f"({best_viols} viols / {best_worst:.1f} worst / "
                  f"{best_zeros} empty wks)")
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
    # SW-SIDE HANDLING MORTALITY -- the fish the PLAN killed by moving them.
    # Every tank-to-tank deposit is charged (forecast/events.py), so mortality
    # is no longer the FW-only story the cull block below tells. It reconciles
    # inside the tank audit -- it is booked on the destination tank -- but
    # folded into the same column as natural mortality the operator cannot
    # SEE it, and "a plan that shuffles more fish kills more of them" is only
    # actionable as a number.
    _hm = {}
    for _ev in placement.transfer_events:
        for _t, _k in (getattr(_ev, "handling_mort_by_tank", None) or {}).items():
            _hm[_t] = _hm.get(_t, 0.0) + _k
    _hm_total = sum(_hm.values())
    if _hm_total > 0:
        _moves = sum(1 for _e in placement.transfer_events
                     if getattr(_e, "count_transferred", 0) > 0)
        print("")
        print(f"  Handling mortality (SW transfers -- fish lost to MOVING "
              f"them, {control.handling_mortality_pct}% per deposit):")
        print(f"    Lost to handling:       {_hm_total:>12,.0f} fish  "
              f"over {_moves:,} transfer(s) into {len(_hm)} tank(s)")
        print(f"    booked as mortality on the destination tank, so the tank "
              f"continuity audit still balances")
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

    # Reports reflect the REALIZED harvest, not the raw "if-never-harvested"
    # biology curve. The biology projection grows a batch forever — SGR is flat
    # past the table's harvest-size ceiling, so an un-harvested fish compounds
    # to 100+ kg over a multi-year horizon, and FeedForecast would sum batches
    # that actually left the facility years earlier. The realized plan harvests
    # every batch out FIFO at ~harvest weight (verified: 0 drift), so drop each
    # batch's projected weeks AFTER its realized harvest-out week.
    realized_last_week: dict[str, str] = {}
    for _r in placement.batch_locations:
        if _r.count > 0 and (_r.batch_id not in realized_last_week
                             or _r.week_label > realized_last_week[_r.batch_id]):
            realized_last_week[_r.batch_id] = _r.week_label

    def _to_realized_lifespan(states_list):
        # Keep weeks up to (and including) the batch's realized harvest-out;
        # batches never placed in OG (FW-only this horizon) are kept whole.
        return [s for s in states_list
                if realized_last_week.get(s.batch_id) is None
                or s.week_label <= realized_last_week[s.batch_id]]

    # An auto-modelled split's FW track (split_batch.py) is real freshwater
    # biology, kept out of the planner's one-SW-row-per-(batch, week) states:
    # its FW rows join the sheet here. Without them B49 on 8/31 showed only
    # its 47,743 seawater fish for 2026-W36/W37 while the ledger opened on
    # 297,968. Empty (no split, or `off`) = the old sheet.
    _bp_split_fw = [s for _b in _sb.sorted_ids(_sb_fw_states) for s in _sb_fw_states[_b]]
    write_biology_projection(
        wb, _to_realized_lifespan(states + in_flight_states + _bp_split_fw))
    write_calibration_diagnostics(wb, residuals)

    # ----- Stage 2.5: optional LP-guided LNS placement refinement (opt-in) -----
    #
    # placement_method=="lns": refine the REALIZED layout in place — relocate
    # grow-out tank occupancy off the hottest systems onto cooler ones, emitting
    # each move as a conserved Transfer. The greedy plan is the warm start AND the
    # fallback: every candidate edit is gated on the real continuity audit (0
    # drift) + input conservation (0 dropped) + a strictly-lower hot spot, so
    # turning LNS on can never make the forecast worse or break conservation. No
    # second placement run — see lns_placement.refine_realized.
    if getattr(control, "placement_method", "greedy") == "lns":
        from . import lns_placement
        try:
            edited = lns_placement.refine_realized(
                placement, initial_state=state,
                batch_week_states=_to_realized_lifespan(states + in_flight_states),
                control=control, facility=facility, system_limits=system_limits,
                facility_limits=facility_limits, batch_meta=batch_by_id, tables=tables)
            if edited is not None:
                placement = edited
        except Exception as e:  # noqa: BLE001 — any failure keeps greedy
            print(f"  LNS placement: refine failed ({type(e).__name__}: {e}); "
                  f"greedy stands")

    # Stitch the manual override window (weeks 1..N) into the output so the
    # operator's scripted weeks are visible + audited. The continuity audit is
    # event-stream driven, so the prefix BatchLocations + realized_biology
    # reconcile with no audit changes (Phase A: biology-only prefix).
    if prefix_batch_locations:
        placement.batch_locations = prefix_batch_locations + placement.batch_locations
        placement.realized_biology.update(prefix_realized)
        placement.transfer_events = prefix_transfers + placement.transfer_events
        placement.harvest_events = prefix_harvests + placement.harvest_events
        placement.tranog_events = prefix_tranogs + placement.tranog_events
        print(f"  Stitched manual override window into the output: "
              f"{len(prefix_batch_locations)} BatchLocations rows, "
              f"{len(prefix_transfers)} transfer + {len(prefix_harvests)} harvest "
              f"+ {len(prefix_tranogs)} TranOG event(s)"
              + (f"; manual FW->OG: {sorted(transferred_fw)}" if transferred_fw else "")
              + ".")

    # Write the plan outputs from Stage 2 placement.
    write_batch_locations(wb, placement.batch_locations)
    # Density violations enumerated from BatchLocations vs per-tank cap.
    #
    # NO DENSITY CONSTRAINT ON FISH PREPARING FOR HARVEST (operator,
    # 2026-08-21): "purge tanks or tanks preparing for harvest do not have a
    # density constraint". Those fish are off feed, not growing, and leave
    # within the purge window, so the water-quality reasoning behind the cap
    # (feed load -> TAN/CO2) does not apply to them.
    #
    # The test is the STAGE, not the system. STARVE covers BOTH regimes:
    #   * purge mode  -- fish moved into a 6N depuration tank, and
    #   * production mode (from control.sixn_production_start) -- where there
    #     is no separate depuration tank and fish purge IN PLACE in their own
    #     growout tank for control.starvation_period_days before harvest.
    # Keying on "system == OG6N and purge mode" missed the second case
    # entirely and reported in-place harvest-prep tanks as violations (one
    # OG4N tank at 118.8 kg/m3 in the 2028 tail).
    from .sixn import is_purge_mode as _is_purge_mode
    from .tiers import effective_density_cap as _eff_cap
    tank_cap_by_id = {t.tank_id: t.max_density_kg_m3 for t in facility.tanks}
    tank_sys_by_id = {t.tank_id: t.system_id for t in facility.tanks}
    density_violations = []
    for r in placement.batch_locations:
        cap = tank_cap_by_id.get(r.tank_id, 0.0)
        if cap <= 0:
            continue
        # R8 (tiers.effective_density_cap) — ONE definition, shared with
        # placement.py and lns_placement.py. A tank preparing for harvest is
        # judged at HARVEST_PREP_DENSITY_CAP (the operator's 150),
        # NOT at +inf: harvest prep is a raised cap, not an exemption, and
        # before 2026-09-08 this audit could not report a STARVE tank at any
        # density at all. Judging only — the planner's sizing paths keep the
        # historical +inf, see the note in tiers.effective_density_cap.
        cap = _eff_cap(cap, tank_sys_by_id.get(r.tank_id, ""),
                       getattr(r, "stage", ""),
                       _is_purge_mode(control, r.week_start),
                       harvest_prep_cap=HARVEST_PREP_DENSITY_CAP)
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
    )
    write_harvest_plan_report(
        wb, placement.harvest_events,
        scenario_name=control.scenario_name,
        default_hog_yield=control.default_hog_yield,
        facility_limits_hog=facility_hog_overrides,
        forecast_start=fs_date,
        report_start=report_start,
        pr_period=_pr_period,
    )
    write_transfer_plan_output(
        wb, placement.transfer_events, placement.tranog_events,
        grade_events=placement.grade_events,
    )
    # Intent check: TransferPlan is what HAPPENED, this is what was DECIDED and
    # whether it happened. A refused move is invisible to every other sheet.
    write_realization_report(
        wb, placement.transfer_events,
        harvest_events=placement.harvest_events,
        tranog_events=placement.tranog_events,
        grade_events=placement.grade_events,
    )
    write_transfer_template(
        wb, placement.batch_locations, placement.harvest_events,
        placement.tranog_events, control, facility,
    )
    # Per-batch plan: the 'where each batch is + how it got there' journey
    # (summary header + tier-by-tier milestones), as a shareable sheet.
    write_batch_plan(
        wb, placement.batch_locations, placement.harvest_events,
        default_hog_yield=control.default_hog_yield,
        tranog_events=placement.tranog_events,
    )
    # Realized-lifespan biology states (FW/EGG biomass + feed for the FW-inclusive
    # report corrections). Defined here so the Advisory can use it too.
    rl_states_by_batch = {
        b: _to_realized_lifespan(sl) for b, sl in states_by_batch.items()
    }
    if _sb_fw_states:
        # A split's FW part is real freshwater biomass and feed: the report
        # writers below read FW/EGG rows only (never one-row-per-week), so its
        # FW track joins them here -- never states_by_batch, which the planner
        # reads and which must hold one row per (batch, week).
        rl_states_by_batch = dict(rl_states_by_batch)
        for _b in _sb.sorted_ids(_sb_fw_states):
            rl_states_by_batch[_b] = (list(rl_states_by_batch.get(_b, []))
                                      + list(_sb_fw_states[_b]))
    write_advisory(
        wb, placement.batch_locations, placement.harvest_events,
        facility_limits, control, batches=batch_by_id, tables=tables,
        biology_states_by_batch=rl_states_by_batch,
    )
    # HYBRID GUIDE ledger -> the ValidationLog. The guide records every lever it
    # REFUSED and every week it declined to steer; until now nothing read that
    # list, so the tool was silently choosing not to use a lever the operator
    # had switched on (e.g. the purge lever self-disables when
    # sixn_level_drains is off). A decision the tool takes on its own has to be
    # readable even when it is the correct decision. Written only when the
    # ledger is non-empty, so a clean hybrid run stays silent.
    _guide_notes = [
        f"HYBRID GUIDE - {m}"
        for m in (list(getattr(harvest_guide, "ledger", []))
                  if harvest_guide is not None else [])
    ]
    if harvest_guide is not None:
        # The L1 guide seeds an in-flight OG batch and moves on, so an
        # automatically modelled split's ARRIVAL never reaches it (its FW
        # biomass does, through fw_by_label). Folding the arrival into the
        # seed is not built; say so where the guide's decisions are read.
        from .time_grid import iso_week_label as _iwl_sb
        for _b in _sb.sorted_ids(_sb_split):
            _e = _sb_entry.get(_b)
            if _e is not None:
                _guide_notes.append(_sb.hybrid_guide_line(
                    _b, _e.post_cull_count, _iwl_sb(_e.tran_og_date)))
        # An OVERDUE batch is missing from the guide altogether: it holds no
        # seawater fish at the close to seed, the guide skips an incoming
        # batch dated before the forecast start, and its track has no FW week.
        for _b in _sb.sorted_ids(_sb_overdue):
            _e = _sb_entry.get(_b)
            if _e is not None:
                _guide_notes.append(_sb.hybrid_guide_overdue_line(
                    _b, _e.post_cull_count, _iwl_sb(_e.tran_og_date)))
    if _guide_notes:
        print(f"\n  HYBRID guide ledger ({len(_guide_notes)} decision(s) — "
              f"also in the ValidationLog):")
        for _m in _guide_notes[:10]:
            print(f"    {_m}")
        if len(_guide_notes) > 10:
            print(f"    ... and {len(_guide_notes) - 10} more")
    # REALIZED-PLAN audit. Every other floor/budget warning in the log is raised
    # mid-plan by whichever pass first noticed a shortfall; later passes then fix
    # some of those weeks and break others, so the log describes a plan that was
    # never produced. This one runs last, over the events actually emitted, and
    # against the PER-WEEK resolved caps. Pure measurement — see
    # analysis.realized_plan_audit.
    from .analysis import realized_plan_audit
    from . import caps as _caps_mod
    from .time_grid import forecast_week_labels
    # The manual window sits BEFORE the planner's start: `control.forecast_start`
    # has already been advanced past it, so the window is the `window_n` weeks
    # immediately preceding it. Labelling forward from the advanced start tags
    # the wrong weeks (it marked 2026-W37/W39 as scripted when the window was
    # W33-W36).
    if window_n:
        from datetime import timedelta as _td_win
        _win_start = fs_date - _td_win(weeks=window_n)
        _window_labels = frozenset(forecast_week_labels(_win_start, window_n))
    else:
        _window_labels = frozenset()
    _realized_warns = realized_plan_audit(
        placement.harvest_events, placement.transfer_events,
        facility_limits, control, window_weeks=_window_labels,
        # Every week the realized plan covers, so a week with NO harvest at all
        # is judged (and named) too -- not only weeks that had an event.
        plan_weeks=sorted({r.week_label for r in placement.batch_locations}))

    # PLAN ENDS EARLY (detection only -- a SAFETY NET). The realized loop used to
    # walk only the weeks the projection has load for, so once the projection
    # ran out of batches the plan simply stopped -- 11 of 85 horizon weeks on
    # the 2026-02-28 era run, with 7,128 fish / 37,762 kg still in tank 63. It
    # now walks every horizon week (placement.phase_d_emit_events, calendar-09),
    # so this should not fire; if it ever does, something cut the walk short.
    # Named
    # here when the last realized week is before the horizon's last week AND
    # fish are still in tanks then. Nothing below feeds the planner.
    _plan_end_notes: list = []
    try:
        _bl_wks = sorted({r.week_label for r in placement.batch_locations})
        _hz = forecast_week_labels(fs_date, control.horizon_weeks)
        if _bl_wks and _hz and _bl_wks[-1] < _hz[-1]:
            _last = _bl_wks[-1]
            _left = [r for r in placement.batch_locations
                     if r.week_label == _last and (r.count or 0) > 0]
            # Fish still in the tanks at the END of the realized walk. The rows
            # above list only non-empty tanks, so a facility that empties before
            # the horizon (its last fish harvested) also has no rows after its
            # last full week -- that is a plan that ran to the end, not one that
            # stopped. Only fish left standing when the walk ended prove it was
            # cut short (2026-02-28 era run: 7,128 fish in tank 63 without
            # calendar-09; harvested in 2027-W30 with it).
            _fish_at_end = sum(
                (t.count or 0) for t in getattr(final_state, 'tanks_by_id', {}).values()
                if not t.is_empty)
            if _left and _fish_at_end > 0:
                _missing = sum(1 for w in _hz if w > _last)
                _plan_end_notes.append(
                    f"PLAN ENDS EARLY - the realized plan stops at {_last}, "
                    f"{_missing} week(s) before the horizon's last week "
                    f"{_hz[-1]} (horizon_weeks={control.horizon_weeks}), with "
                    f"{sum(r.count for r in _left):,.0f} fish "
                    f"({sum(r.biomass_kg for r in _left):,.0f} kg) still in "
                    f"{len({r.tank_id for r in _left})} tank(s). No sheet "
                    f"covers the remaining weeks and those fish are never "
                    f"harvested in this run.")
    except Exception as _pe_err:                                   # noqa: BLE001
        _plan_end_notes = [f"PLAN ENDS EARLY - check failed: {_pe_err!r}"]
    for _m in _plan_end_notes:
        print(f"  WARN: {_m}")

    # PER-WEEK COVERAGE. A metric the operator steers week by week, whose rows
    # STOP before the horizon ends, hands the remaining weeks a number nobody
    # chose -- on the 2026-08-31 PR that was worth ~131 t of horizon production.
    # Detection only: resolution is unchanged, and a metric with no rows at all
    # stays silent because the default is then the deliberate answer.
    try:
        _coverage_notes = _caps_mod.coverage_gap_notes(
            facility_limits, control,
            forecast_week_labels(fs_date, control.horizon_weeks))
    except Exception as _cov_err:                                  # noqa: BLE001
        # Loud, never silent: a broken check must not look like a clean run.
        _coverage_notes = [f"PER-WEEK COVERAGE - check failed: {_cov_err!r}"]
    if _coverage_notes:
        print(f"\n  PER-WEEK COVERAGE ({len(_coverage_notes)} metric(s) fall "
              f"back mid-horizon - also in the ValidationLog):")
        for _m in _coverage_notes:
            print(f"    {_m}")

    # THE PR's FRESHWATER FISH THAT NO PROJECTION CARRIES (report layer,
    # 2026-09-11). Operator rule: the ledger opening holds every fish the PR
    # holds, FW and SW. A batch split across FW and SW at the close is hydrated
    # as seawater only and kept out of the FW projection (above), and a
    # wholly-FW batch moved by a manual fw_to_og is the same -- so their FW
    # part is HELD for the ledgers (excel_io.held_fw_openings). A held part a
    # scripted fw_to_og moves is modelled; one nothing moves never reaches
    # seawater, and is named here, loudly, rather than absorbed. Pure
    # measurement: nothing below feeds the planner.
    # The projected set is the projector's OWN list (fw_projected_ids), not
    # "batches with an FW row": a batch whose tran_og_date is already past is
    # projected from the PR's FW count starting in SEAWATER, has no FW row, and
    # was held as well -- the same fish twice in the opening.
    from .excel_io import held_fw_openings, unmodelled_fw_warnings
    fw_openings = held_fw_openings(fw_in_flight_aggregates, fw_projected_ids)
    split_warns, unmodelled_fw = unmodelled_fw_warnings(
        fw_openings, placement.tranog_events,
        {t.batch_id for t in audit_initial_state.tanks_by_id.values()
         if t.batch_id and t.count > 0})
    for _m in split_warns:
        print(f"  WARN: {_m}")
    # ONE line per automatic transfer (batch, fish, week, the rule, how to
    # override), and a loud ERROR when its fish did not all reach seawater.
    _sb_lines: list = []
    _sb_unplaced: dict = {}
    if _sb_expected:
        from .time_grid import iso_week_label as _iwl_sb2
        for _b in _sb.sorted_ids(_sb_expected):
            _e = _sb_entry[_b]
            _evs = [e for e in placement.tranog_events if e.batch_id == _b]
            _placed = sum(float(getattr(e, "count_placed", 0.0) or 0.0) for e in _evs)
            _wk = _iwl_sb2(_evs[0].event_date if _evs else _e.tran_og_date)
            _bm = batch_by_id[_b]
            _sb_lines.append(_sb.auto_transfer_line(
                _b, fw_count=_sb_class.fw_count.get(_b, 0.0), week_label=_wk,
                placed=_placed, tran_og_date=_bm.tran_og_date, pr_closing=pr_closing,
                tran_og_count=_bm.tran_og_count,
                sw_count=_sb_class.sw_count.get(_b, 0.0), overdue=_b in _sb_overdue,
                arrival=_e.post_cull_count))
            _miss = _sb.unplaced_split_parts({_b: _sb_expected[_b]}, _evs)
            if _miss:
                _sb_unplaced.update(_miss)
                _sb_lines.append(_sb.not_placed_line(
                    _b, expected=_sb_expected[_b], placed=_placed, week_label=_wk,
                    overdue=_b in _sb_overdue))
        for _m in _sb_lines:
            print(f"  WARN: {_m}")
    write_validation_log(
        wb,
        residuals=residuals,
        placement_warnings=placement.warnings,
        scheduler_warnings=sched_warns,
        bottlenecks=canvas.bottlenecks,
        density_violations=density_violations,
        invariant_warnings=(list(hydration_warns) + list(inv_warns)
                            + list(manual_warns) + list(fw_calib_warns)
                            + _guide_notes + _realized_warns
                            + _coverage_notes + split_warns + _sb_lines
                            + _plan_end_notes),
        placed_batches={r.batch_id for r in placement.batch_locations},
    )
    write_daily_harvest_schedule(
        wb, placement.harvest_events, fs_date,
        default_hog_yield=control.default_hog_yield,
        facility_limits_hog=facility_hog_overrides,
        report_start=report_start,
    )
    write_harvest_report(
        wb, placement.harvest_events,
        default_hog_yield=control.default_hog_yield,
        facility_limits_hog=facility_hog_overrides,
        forecast_start=fs_date,
        report_start=report_start,
    )
    write_yearly_summary(
        wb, placement.batch_locations, placement.harvest_events,
        facility_limits, control, batches=batch_by_id, tables=tables,
        default_hog_yield=control.default_hog_yield, hog_overrides=facility_hog_overrides,
        sixn_move_in_feed=getattr(placement, "sixn_move_in_feed", None),
        biology_states_by_batch=rl_states_by_batch,
        report_start=report_start, pr_period=_pr_period,
    )
    write_feed_forecast_weekly(
        wb, placement.batch_locations, rl_states_by_batch, fs_date, tables, batch_by_id,
        sixn_move_in_feed=getattr(placement, "sixn_move_in_feed", None))
    write_feed_forecast_monthly(
        wb, placement.batch_locations, rl_states_by_batch, fs_date, tables, batch_by_id,
        sixn_move_in_feed=getattr(placement, "sixn_move_in_feed", None),
        report_start=report_start)
    all_states = _to_realized_lifespan(states + in_flight_states)
    # The PR-hydrated seawater opening per batch (the same anchor the
    # continuity audits open from). The ledgers use it only for a batch with
    # nothing else to open on -- a PR batch missing from batches.yaml.
    _pr_openings: dict = {}
    for _t in audit_initial_state.tanks_by_id.values():
        if _t.batch_id and (_t.count or 0) > 0:
            _e = _pr_openings.setdefault(_t.batch_id, [0.0, 0.0])
            _e[0] += float(_t.count)
            _e[1] += float(_t.biomass_kg or 0.0)
    write_weekly_report(
        wb, placement.batch_locations, placement.harvest_events, all_states,
        transfer_events=placement.transfer_events, batches=batch_by_id, tables=tables,
        scenario_name=control.scenario_name, hog_yield=control.default_hog_yield,
        hog_overrides=facility_hog_overrides,
        realized_biology=getattr(placement, "realized_biology", None),
        tranog_events=getattr(placement, "tranog_events", None),
        window_openings=prefix_openings,
        # INPUT = EGGS ONLY (operator, 2026-09-11): the FW->SW move is a move
        # inside the batch, shown in Xfer_In/Xfer_Out, and the ledger opens on
        # EVERY fish the PR holds -- the freshwater part of a split batch
        # included (fw_openings). So the fish a manual fw_to_og culls in
        # freshwater WERE in the opening, and the cull is a real removal:
        # booked once, on the transfer week (window_culls). The FW fish lost
        # between the PR close and the transfer are booked as mortality there
        # (fw_transfer_basis = the window's fw_count_at_transfer).
        # This reverses a deliberate omission: while the opening held
        # seawater only and the TranOG was credited as input NET of the cull,
        # booking the cull removed it twice.
        window_culls=prefix_fw_cull,
        fw_openings=fw_openings,
        fw_transfer_basis=manual_fw_balance,
        fw_projected=fw_projected_ids,
        # An auto-modelled split's FW track (split_batch.py): its FW feed,
        # mortality and crossing cull, and the fish that cross.
        split_fw=_sb_fw_states or None,
        sixn_move_in_feed=getattr(placement, "sixn_move_in_feed", None),
        pr_openings=_pr_openings)
    write_monthly_report(
        wb, placement.batch_locations, placement.harvest_events, all_states,
        transfer_events=placement.transfer_events, batches=batch_by_id, tables=tables,
        scenario_name=control.scenario_name, hog_yield=control.default_hog_yield,
        hog_overrides=facility_hog_overrides, forecast_start=control.forecast_start,
        report_start=report_start,
        realized_biology=getattr(placement, "realized_biology", None),
        # THE SAME CHOICES AS THE WEEKLY CALL ABOVE, AT THE SAME SCOPE -- the
        # month is a roll-up of those very weekly rows.
        #
        # `window_openings`: without them a batch whose scripted harvest lands
        # in a manual window opened the month at 0 and the harvest came out of
        # nothing (2026-08 closed at -4,562 fish for B41 on the 2026-08-31 PR).
        #
        # `tranog_events` and `window_culls`: since 2026-09-11 INPUT = EGGS ONLY
        # and the ledger opens on every fish the PR holds, freshwater included
        # (fw_openings -- B49 opens at 47,743 SW + 250,225 FW = 297,968, the
        # PR's own figure). The TranOG is therefore a move (Xfer_In/Xfer_Out,
        # used to find the FW->SW week), never an input, and the freshwater
        # cull of a manual fw_to_og removes fish that WERE in the opening, so
        # it is booked. The earlier trade-offs measured here (crediting the
        # arrival, omitting the cull) existed only because the month opened on
        # seawater alone; with the freshwater part in the opening neither
        # double-books.
        window_openings=prefix_openings,
        tranog_events=placement.tranog_events,
        window_culls=prefix_fw_cull,
        fw_openings=fw_openings,
        fw_transfer_basis=manual_fw_balance,
        fw_projected=fw_projected_ids,
        split_fw=_sb_fw_states or None,
        sixn_move_in_feed=getattr(placement, "sixn_move_in_feed", None),
        pr_period=_pr_period, pr_openings=_pr_openings)
    write_reconciliation_report(
        wb,
        placement.batch_locations,
        all_states,
        placement.harvest_events,
        placement.tranog_events,
        # The PR-hydrated anchor, NOT `state` — a manual override window advances
        # `state` in place to week N+1, so passing it opened the ledger's FIRST
        # row (the first window week) from a state N weeks in the FUTURE. Every
        # batch then read as drift on that one row: the count by the window's
        # mortality + harvest, the biomass by the window's growth. Same anchor
        # write_tank_continuity_audit uses, so the two ledgers agree. Without a
        # window audit_initial_state IS state, so no-window runs are unchanged.
        audit_initial_state,
        realized_biology=getattr(placement, "realized_biology", None),
        transfer_events=placement.transfer_events,
        grade_events=placement.grade_events,
    )
    # The FW mass balance reads each batch's FW rows plus its crossing week:
    # for an auto split those FW rows live on its own track (kept out of
    # states_by_batch), so the audit gets them beside the merged SW stream.
    _audit_states = states_by_batch
    if _sb_fw_states:
        _audit_states = dict(states_by_batch)
        for _b in _sb.sorted_ids(_sb_fw_states):
            _audit_states[_b] = (list(_sb_fw_states[_b])
                                 + list(states_by_batch.get(_b, [])))
    write_input_conservation_audit(
        wb, batches, placement.batch_locations, placement.harvest_events, control,
        tranog_events=placement.tranog_events,
        biology_states_by_batch=_audit_states,
        manual_fw_balance=manual_fw_balance,
        # Fish no model carries must not read PLACED (see split_warns above).
        unmodelled_fw=unmodelled_fw,
        # An auto split is judged against its REMAINING target, and an
        # automatic transfer that did not place its fish is a DROP.
        split_remaining=({_b: _sb.remaining_target(batch_by_id[_b].tran_og_count,
                                                   _sb_class.sw_count.get(_b, 0.0))
                          for _b in _sb.sorted_ids(_sb_split)} or None),
        unplaced_split_fw=_sb_unplaced or None,
        # An overdue batch moved in the first forecast week: In_Horizon, and
        # its FW balance from its entry row.
        overdue_effective=({_b: _sb._as_date(_sb_entry[_b].tran_og_date)
                            for _b in _sb.sorted_ids(_sb_overdue) if _b in _sb_entry}
                           or None),
        split_entry_rows=_sb_entry_row or None,
    )
    write_tank_continuity_audit(
        wb,
        placement.batch_locations,
        all_states,
        placement.harvest_events,
        placement.transfer_events,
        placement.grade_events,
        placement.tranog_events,
        audit_initial_state,
        realized_biology=placement.realized_biology,
    )
    # Realized per-system biomass + feed vs the SystemLimits caps. The engine
    # checks tank density but never checked the SYSTEM caps against the realized
    # plan — this surfaces over-cap systems (esp. feed, the tighter constraint).
    n_bio_over, n_feed_over, worst_bio, worst_feed = write_system_limits_audit(
        wb, placement.batch_locations, batch_by_id, tables, system_limits, control,
    )
    print(f"  SystemLimits:  biomass over-cap {n_bio_over} (worst "
          f"{worst_bio:.2f}x), feed over-cap {n_feed_over} (worst {worst_feed:.2f}x) "
          f"-> SystemLimitsAudit sheet")
    # DENSITY gets the same one-line summary SystemLimits gets. Without it the
    # per-tank breaches land in ValidationLog and nowhere else, so the console
    # shows a bare "warnings=N" and a real welfare breach reads as noise. The
    # count is R8-judged (purge / harvest-prep tanks are exempt and excluded),
    # so every line here is a tank that is genuinely over its cap.
    if density_violations:
        _worst = max(density_violations, key=lambda v: v[3] / v[4])
        print(f"  Density:       {len(density_violations)} tank-week(s) over "
              f"per-tank cap (worst {_worst[3]:.1f} kg/m3 vs cap {_worst[4]:.0f} "
              f"in {_worst[1]}, {_worst[0]}) -> ValidationLog")
    else:
        print(f"  Density:       no tank over its per-tank cap "
              f"(purge / harvest-prep exempt under R8)")

    write_facility_map(wb, placement.batch_locations, facility,
                       batches=batch_by_id, tables=tables,
                       biology_states_by_batch=rl_states_by_batch,
                       control=control)

    # Config snapshot: embed the exact app-managed config + scenario this
    # run used into the output workbook, so the saved file is a complete,
    # re-importable record. Only when running from app config (YAML).
    from .config_snapshot import write_config_snapshot
    write_config_snapshot(wb, config_dir=config_dir, scenario_dir=scenario_dir)
    print(f"  Wrote RunConfig snapshot (config + scenario embedded in output)")

    # Run summary back to Control R8-R16 (DESIGN §1) — operator's
    # in-workbook signal that the run completed + a snapshot of scope.
    elapsed = time.time() - t0
    total_warnings = (
        len(residuals) + len(canvas.bottlenecks)
        + len(sched_warns) + len(placement.warnings)
        + len(density_violations) + len(hydration_warns) + len(inv_warns)
        + len(fw_calib_warns) + len(split_warns) + len(_sb_lines)
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
    # Sync the Control INPUT cell (B3) to the derived start, mirroring the
    # VBA. After a run, Control B3 == ProductionReport closing + 1, so a
    # stale B3 unambiguously means "not yet run against the current PR".
    write_forecast_start(wb, control.forecast_start)

    # Presentation pass over every sheet written above. Last thing before the
    # save so it sees the finished workbook, and isolated in its own module so
    # a styling change can never reach a forecast number. If it raises, the
    # run still produces a correct (if plain) workbook — an export is worth
    # more unstyled than not at all.
    try:
        from .excel_format import apply_workbook_formatting
        apply_workbook_formatting(wb)
    except Exception as exc:                      # pragma: no cover - cosmetic
        print(f"  NOTE: workbook formatting skipped ({exc.__class__.__name__}: {exc}); "
              f"data is unaffected.")

    # Match the output extension to the workbook's actual content type: a
    # macro-enabled workbook (kept VBA from an .xlsm/template input) MUST be saved
    # `.xlsm`, a plain one `.xlsx` — otherwise Excel refuses to open it (content-
    # type / extension mismatch). Coerce here as a backstop so NO caller (CLI,
    # app, or a manual output name) can produce a mis-stamped, un-openable export.
    _macro = getattr(wb, "vba_archive", None) is not None
    _want = ".xlsm" if _macro else ".xlsx"
    if out_path.suffix.lower() != _want:
        _new = out_path.with_suffix(_want)
        print(f"  NOTE: output extension '{out_path.suffix}' does not match the "
              f"workbook type; saving as '{_new.name}' so Excel can open it.")
        out_path = _new
    # Per-batch handling (moves/fish) onto the Batch Plan. LAST: it reads
    # TransferPlan and InputConservationAudit, so every sheet must exist.
    annotate_batch_plan_handling(wb)
    # Costs & profit (additive, 2026-09-11): ONLY when config/costs.yaml exists;
    # appended LAST so every existing sheet's XML, index and style ids stay put.
    if (Path(config_dir) / "costs.yaml").is_file():
        from .costs_report import write_costs_sheet
        write_costs_sheet(wb, config_dir=config_dir,
                          batch_locations=placement.batch_locations,
                          states_by_batch=rl_states_by_batch, tables=tables,
                          batch_by_id=batch_by_id, batches=batches,
                          sixn_move_in_feed=getattr(placement, "sixn_move_in_feed", None),
                          report_start=report_start)
    wb.save(out_path)
    wb.close()
    print(f"\nSaved workbook {out_path}  ({elapsed:.2f}s, status={status}, "
          f"warnings={total_warnings})")
    return 0


def _cli():
    p = argparse.ArgumentParser()
    p.add_argument("--workbook", default=None,
                   help="Input workbook path. Output is written back to this "
                        "file unless --output is given.")
    p.add_argument("--output", default=None,
                   help="Output workbook path. If omitted, output is written "
                        "back to the input file (legacy behavior).")
    p.add_argument("--config-dir", default=None,
                   help="Stable-config YAML directory (control/biology/facility). "
                        "When set, those inputs load from YAML instead of the "
                        "workbook; the workbook still supplies PR/batches/limits.")
    p.add_argument("--scenario-dir", default=None,
                   help="Scenario YAML directory (batches/limits). When set, the "
                        "forward batch schedule and facility/system limits load "
                        "from YAML instead of the workbook.")
    p.add_argument("--advance-weeks", type=int, default=0,
                   help="Manual override window (Phase A): advance the facility "
                        "this many weeks of biology before the pipeline plans "
                        "forward from the advanced state.")
    a = p.parse_args()
    return main(a.workbook, a.output, a.config_dir, a.scenario_dir, a.advance_weeks)


if __name__ == "__main__":
    raise SystemExit(_cli())
