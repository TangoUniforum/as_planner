"""Read inputs and write outputs against Forecast.xlsm."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

import openpyxl
from openpyxl.utils import get_column_letter

from .models import CalibrationResidual, BatchWeekState


# ---------- Writer: BiologyProjection sheet ----------

def write_biology_projection(wb, states: Iterable[BatchWeekState], sheet_name: str = "BiologyProjection") -> None:
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    headers = [
        "Batch", "Week", "WeekStart", "DaysSinceInput", "WeekFromInput",
        "Stage", "Count", "AvgWt_g", "Biomass_kg", "SGR_pct_day", "FCR",
        "FeedType", "Mortality_pct_wk", "Cull_pct", "Cull_Count",
        "Cull_Biomass_kg", "Feed_kg_day", "Feed_kg_week",
    ]
    ws.append(headers)
    for s in states:
        ws.append([
            s.batch_id, s.week_label, s.week_start, s.days_since_input,
            s.week_from_input, s.stage, round(s.count, 1), round(s.avg_weight_g, 3),
            round(s.biomass_kg, 1), round(s.sgr_pct_day, 4), round(s.fcr, 4),
            s.feed_type, round(s.mortality_pct_weekly, 4), round(s.cull_event_pct, 4),
            round(s.cull_count_week, 0) if s.cull_count_week > 0 else None,
            round(s.cull_biomass_kg_week, 1) if s.cull_biomass_kg_week > 0 else None,
            round(s.feed_kg_day, 2), round(s.feed_kg_week, 1),
        ])

    widths = {1: 8, 2: 12, 3: 14, 4: 14, 5: 14, 6: 6, 7: 12, 8: 10,
              9: 13, 10: 12, 11: 8, 12: 18, 13: 16, 14: 9,
              15: 12, 16: 16, 17: 12, 18: 13}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w


def write_batch_locations(wb, batch_locations, sheet_name: str = "BatchLocations") -> None:
    """Per-(week, batch, tank) occupancy from the placement plan."""
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append(["BATCH LOCATIONS"])
    ws.append(["Per-tank batch occupancy from the forecast plan. Auto-generated."])
    ws.append([])
    ws.append([
        "Week", "Week_Start", "Batch", "Tank", "System",
        "Count (fish)", "AvgWt (kg)", "Biomass (kg)", "Density (kg/m3)",
    ])
    for r in batch_locations:
        ws.append([
            r.week_label, r.week_start, r.batch_id, r.tank_id, r.system_id,
            round(r.count, 0),
            round(r.avg_wt_g / 1000.0, 3),
            round(r.biomass_kg, 0),
            round(r.density_kg_m3, 1),
        ])
    widths = {1: 11, 2: 12, 3: 8, 4: 6, 5: 9, 6: 12, 7: 11, 8: 13, 9: 14}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w


def write_harvest_plan_output(
    wb,
    harvest_events,
    default_hog_yield: float,
    facility_limits_hog: dict,
    pinned_harvests=None,
    sheet_name: str = "HarvestPlan",
) -> None:
    """Per-event harvest plan, with operator pins clearly separated
    from planner-generated rows.

    Layout: two sections separated by a section header.
      [1] OPERATOR-PINNED — preserved across runs; rows the operator
          set Pinned=TRUE on. These are HONORED as hard constraints by
          the harvest scheduler.
      [2] PLANNER-GENERATED — rewritten every run; the algorithm's
          additional harvest events to meet facility caps.

    `facility_limits_hog` is a dict `{week_label: hog_yield}` for
    per-week HOG yield overrides; default falls back to `default_hog_yield`.
    """
    from openpyxl.styles import Font, PatternFill
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append(["HARVEST PLAN"])
    ws.append([
        "Two sections: OPERATOR-PINNED rows first (preserved across runs, "
        "honored as hard constraints), then PLANNER-GENERATED rows "
        "(rewritten each run). Operator marks Pinned=TRUE in the pin "
        "section to keep a row across runs."
    ])
    ws.append([])

    headers = [
        "Week", "Batch", "Tank", "Count (fish)",
        "Gross_AvgWt (kg)", "Gross_Biomass (kg)",
        "HOG_Yield (ratio)", "HOG_AvgWt (kg)", "HOG_Biomass (kg)",
        "Source", "Pinned",
    ]
    bold = Font(bold=True)
    pin_fill = PatternFill("solid", fgColor="FFF3CD")
    section_fill = PatternFill("solid", fgColor="D9E2F3")

    # Identify which harvest events match operator pins (by week, batch,
    # tank). The scheduler honors pins as hard constraints, so each pin
    # appears as an event; we tag those rows as Source="Pin".
    pin_keys = {
        (p.week_label, p.batch_id, p.tank_id)
        for p in (pinned_harvests or [])
    }

    # Track current row explicitly (openpyxl's append+max_row dance is
    # unreliable for empty rows). Row 1=title, 2=description, 3=blank.
    cur_row = 4

    def _write_row(values: list, fill=None, bold_font: bool = False):
        nonlocal cur_row
        for c, v in enumerate(values, start=1):
            cell = ws.cell(row=cur_row, column=c, value=v)
            if fill is not None:
                cell.fill = fill
            if bold_font:
                cell.font = bold
        cur_row += 1

    # ----- Section 1: operator-pinned rows -----
    _write_row(
        [f"=== OPERATOR-PINNED ({len(pinned_harvests or [])} row(s) — "
         f"honored as hard constraints) ==="]
        + [None] * (len(headers) - 1),
        fill=section_fill, bold_font=True,
    )
    _write_row(headers, bold_font=True)
    for p in (pinned_harvests or []):
        _write_row([
            p.week_label or p.raw_week_cell, p.batch_id, p.tank_id,
            round(p.count, 0),
            round(p.gross_avg_wt_kg, 3),
            round(p.gross_biomass_kg, 0),
            round(p.hog_yield, 4) if p.hog_yield else None,
            round(p.hog_avg_wt_kg, 3) if p.hog_avg_wt_kg else None,
            round(p.hog_biomass_kg, 0) if p.hog_biomass_kg else None,
            "Pin",
            True,
        ], fill=pin_fill)

    # ----- Divider + Section 2 header -----
    cur_row += 1  # blank divider row
    _write_row(
        ["=== PLANNER-GENERATED (rewritten every run) ==="]
        + [None] * (len(headers) - 1),
        fill=section_fill, bold_font=True,
    )
    _write_row(headers, bold_font=True)

    events_sorted = sorted(harvest_events, key=lambda e: (e.event_date, e.source_tank_id))
    for ev in events_sorted:
        wk = iso_week_label(ev.event_date)
        # Skip events that came from operator pins — they're already in
        # the pin section above.
        if (wk, ev.batch_id, ev.source_tank_id) in pin_keys:
            continue
        gross_avg_kg = ev.avg_wt_g / 1000.0
        gross_biomass = ev.count * gross_avg_kg
        hog_yield = facility_limits_hog.get(wk, default_hog_yield)
        hog_avg = gross_avg_kg * hog_yield
        hog_biomass = gross_biomass * hog_yield
        _write_row([
            wk, ev.batch_id, ev.source_tank_id,
            round(ev.count, 0),
            round(gross_avg_kg, 3),
            round(gross_biomass, 0),
            round(hog_yield, 4),
            round(hog_avg, 3),
            round(hog_biomass, 0),
            "Planner",
            None,   # Pinned — operator sets TRUE to keep across runs
        ])
    widths = {1: 11, 2: 8, 3: 6, 4: 13, 5: 16, 6: 17, 7: 17, 8: 14, 9: 16,
              10: 9, 11: 8}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w


def write_transfer_plan_output(
    wb,
    transfer_events,
    tranog_events,
    grade_events=None,
    pinned_transfers=None,
    sheet_name: str = "TransferPlan",
) -> None:
    """Per-event transfer + TranOG + Grade plan, with operator pins
    clearly separated from planner-generated rows.

    Layout: two sections separated by a blank row + section header.
      [1] OPERATOR-PINNED — preserved across runs; rows the operator
          set Pinned=TRUE on. NOTE: placement does not yet HONOR these
          as hard constraints (run.py prints a WARN); they're echoed
          here for visibility so the operator can see what was set.
      [2] PLANNER-GENERATED — rewritten every run; the algorithm's
          decisions for transfers / TranOG / Grade events.

    Row schema: Week, Batch, From_Tank, To_Tank, Count, Avg_Weight (kg),
    Type, CV (%), Status, Source, Pinned. From_Tank is 'FW' for TranOG;
    multi-tank source for Grade is comma-separated. Source is "Pin" or
    "Planner" for at-a-glance distinction.
    """
    from openpyxl.styles import Font, PatternFill
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append(["TRANSFER PLAN"])
    ws.append([
        "Two sections: OPERATOR-PINNED rows first (preserved across runs), "
        "then PLANNER-GENERATED rows (rewritten each run). Operator marks a "
        "Pinned=TRUE row in the pin section to keep it across runs."
    ])
    ws.append([])

    headers = [
        "Week", "Batch", "From_Tank", "To_Tank",
        "Count (fish)", "Avg_Weight (kg)", "Type", "CV (%)", "Status",
        "Source", "Pinned",
    ]
    bold = Font(bold=True)
    pin_fill = PatternFill("solid", fgColor="FFF3CD")     # soft yellow
    section_fill = PatternFill("solid", fgColor="D9E2F3")  # soft blue

    cur_row = 4

    def _write_row(values: list, fill=None, bold_font: bool = False):
        nonlocal cur_row
        for c, v in enumerate(values, start=1):
            cell = ws.cell(row=cur_row, column=c, value=v)
            if fill is not None:
                cell.fill = fill
            if bold_font:
                cell.font = bold
        cur_row += 1

    # ----- Section 1: operator-pinned rows -----
    _write_row(
        [f"=== OPERATOR-PINNED ({len(pinned_transfers or [])} row(s) — "
         f"NOT YET HONORED by placement) ==="]
        + [None] * (len(headers) - 1),
        fill=section_fill, bold_font=True,
    )
    _write_row(headers, bold_font=True)
    for p in (pinned_transfers or []):
        _write_row([
            p.week_label or p.raw_week_cell, p.batch_id, p.from_tank, p.to_tank,
            round(p.count, 0),
            round(p.avg_weight_kg, 3),
            p.grade or "Transfer",
            round(p.cv_pct, 1) if p.cv_pct else None,
            "pinned",
            "Pin",
            True,
        ], fill=pin_fill)

    # ----- Divider + Section 2 header -----
    cur_row += 1  # blank divider row
    _write_row(
        ["=== PLANNER-GENERATED (rewritten every run) ==="]
        + [None] * (len(headers) - 1),
        fill=section_fill, bold_font=True,
    )
    _write_row(headers, bold_font=True)

    rows: list[tuple] = []
    for ev in tranog_events:
        wk = iso_week_label(ev.event_date)
        for dest in ev.destinations:
            rows.append((
                ev.event_date, wk, ev.batch_id, "FW", dest.tank_id,
                dest.count, dest.avg_wt_g / 1000.0,
                "TranOG", dest.cv_pct, "applied",
            ))
    for ev in transfer_events:
        # Show ALL events, including rejected ones (count_transferred==0)
        # so the audit can account for every state change attempt.
        ct = getattr(ev, "count_transferred", None)
        if ct is None:
            status = "applied"
        elif ct <= 0:
            status = "rejected"
        elif ct < sum(d.count for d in ev.destinations) - 0.5:
            status = "partial"
        else:
            status = "applied"
        wk = iso_week_label(ev.event_date)
        for dest in ev.destinations:
            rows.append((
                ev.event_date, wk, ev.batch_id, str(ev.source_tank_id), dest.tank_id,
                dest.count, dest.avg_wt_g / 1000.0,
                "Transfer", dest.cv_pct, status,
            ))
    for ev in (grade_events or []):
        wk = iso_week_label(ev.event_date)
        src_str = ",".join(str(t) for t in ev.source_tank_ids)
        for dest in ev.destinations:
            rows.append((
                ev.event_date, wk, ev.batch_id, src_str, dest.tank_id,
                dest.count, dest.avg_wt_g / 1000.0,
                "Grade", dest.cv_pct, "applied",
            ))
    rows.sort(key=lambda r: (r[0], r[2]))

    for r in rows:
        _write_row([
            r[1], r[2], r[3], r[4],
            round(r[5], 0),
            round(r[6], 3),
            r[7],
            round(r[8], 1) if r[8] else None,
            r[9],
            "Planner",
            None,   # Pinned — operator sets TRUE to keep across runs
        ])
    widths = {1: 11, 2: 8, 3: 10, 4: 8, 5: 13, 6: 14, 7: 9, 8: 8, 9: 10,
              10: 9, 11: 8}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w


def write_daily_harvest_schedule(
    wb,
    harvest_events,
    forecast_start,
    default_hog_yield: float,
    facility_limits_hog: dict,
    sheet_name: str = "Daily Harvest Schedule",
) -> None:
    """Mon-Fri split of weekly harvests with HOG conversions.

    Each Harvest event is distributed evenly across the Mon-Fri operating
    days of its ISO week. Days before `forecast_start` or after the
    horizon are dropped.
    """
    from datetime import timedelta
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append(["DAILY HARVEST SCHEDULE"])
    ws.append([f"Weekly harvests split Mon-Fri. Forecast start {forecast_start}"])
    ws.append([])
    ws.append([
        "Year", "Week", "Date", "Tank", "Batch", "Count (fish)",
        "Weight (kg HOG)", "Avg Weight (kg HOG)", "Live Weight (kg)",
    ])

    fs = forecast_start.date() if hasattr(forecast_start, "date") else forecast_start
    events_sorted = sorted(harvest_events, key=lambda e: (e.event_date, e.source_tank_id))
    for ev in events_sorted:
        ev_date = ev.event_date.date() if hasattr(ev.event_date, "date") else ev.event_date
        # Mon-Fri of this event's ISO week, filtered to forecast horizon.
        monday = ev_date - timedelta(days=ev_date.weekday())
        mon_fri = [monday + timedelta(days=i) for i in range(5) if monday + timedelta(days=i) >= fs]
        if not mon_fri:
            mon_fri = [ev_date]  # fall back to event date itself
        per_day_count = ev.count / len(mon_fri)
        per_day_live_kg = per_day_count * ev.avg_wt_g / 1000.0
        wk_label = iso_week_label(ev_date)
        hog_yield = facility_limits_hog.get(wk_label, default_hog_yield)
        per_day_hog_kg = per_day_live_kg * hog_yield
        hog_avg_kg = (ev.avg_wt_g / 1000.0) * hog_yield
        iso_y, iso_w, _ = ev_date.isocalendar()
        for d in mon_fri:
            ws.append([
                iso_y, iso_w, d, ev.source_tank_id, ev.batch_id,
                round(per_day_count, 0),
                round(per_day_hog_kg, 0),
                round(hog_avg_kg, 3),
                round(per_day_live_kg, 0),
            ])
    widths = {1: 6, 2: 6, 3: 12, 4: 6, 5: 8, 6: 12, 7: 15, 8: 17, 9: 14}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w


def write_harvest_report(
    wb,
    harvest_events,
    default_hog_yield: float,
    facility_limits_hog: dict,
    sheet_name: str = "HarvestReport",
) -> None:
    """Per-week harvest aggregates: total fish + biomass + HOG."""
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append(["HARVEST REPORT"])
    ws.append(["Per-week harvest totals."])
    ws.append([])
    ws.append([
        "Week", "Batches", "Tanks", "Count (fish)",
        "Avg_AvgWt (kg)", "Gross_Biomass (kg)",
        "HOG_Yield", "HOG_AvgWt (kg)", "HOG_Biomass (kg)",
    ])

    from collections import defaultdict
    by_week: dict[str, list] = defaultdict(list)
    for ev in harvest_events:
        wk = iso_week_label(ev.event_date)
        by_week[wk].append(ev)

    for wk in sorted(by_week.keys()):
        evs = by_week[wk]
        total_count = sum(e.count for e in evs)
        total_biomass = sum(e.count * e.avg_wt_g / 1000.0 for e in evs)
        avg_wt_kg = (total_biomass / total_count) if total_count > 0 else 0.0
        batches = sorted({e.batch_id for e in evs})
        tanks = sorted({e.source_tank_id for e in evs})
        hog_yield = facility_limits_hog.get(wk, default_hog_yield)
        hog_avg = avg_wt_kg * hog_yield
        hog_biomass = total_biomass * hog_yield
        ws.append([
            wk,
            ",".join(batches),
            ",".join(str(t) for t in tanks),
            round(total_count, 0),
            round(avg_wt_kg, 3),
            round(total_biomass, 0),
            round(hog_yield, 4),
            round(hog_avg, 3),
            round(hog_biomass, 0),
        ])
    widths = {1: 11, 2: 14, 3: 16, 4: 13, 5: 14, 6: 17, 7: 10, 8: 14, 9: 16}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w


def write_feed_forecast_weekly(
    wb,
    biology_states_by_batch,
    forecast_start,
    sheet_name: str = "FeedForecastWeekly",
) -> None:
    """Per-week facility-wide feed forecast.

    Aggregates per-batch weekly feed projections (from biology) into a
    single facility-level series.
    """
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append(["FEED FORECAST — WEEKLY"])
    ws.append(["Per-week facility-wide feed projections from biology."])
    ws.append([])
    ws.append(["Week", "Feed_kg_week", "Feed_kg_day (peak)", "Active_Batches"])

    from collections import defaultdict
    by_week_total: dict[str, float] = defaultdict(float)
    by_week_peak: dict[str, float] = defaultdict(float)
    by_week_batches: dict[str, set] = defaultdict(set)
    for batch_id, states in biology_states_by_batch.items():
        for s in states:
            by_week_total[s.week_label] += s.feed_kg_week
            by_week_peak[s.week_label] = max(by_week_peak[s.week_label], s.feed_kg_day)
            if s.feed_kg_week > 0:
                by_week_batches[s.week_label].add(batch_id)

    for wk in sorted(by_week_total.keys()):
        ws.append([
            wk,
            round(by_week_total[wk], 0),
            round(by_week_peak[wk], 0),
            len(by_week_batches[wk]),
        ])
    widths = {1: 11, 2: 14, 3: 19, 4: 16}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w


def write_feed_forecast_monthly(
    wb,
    biology_states_by_batch,
    forecast_start,
    sheet_name: str = "FeedForecastMonthly",
) -> None:
    """Per-month rollup of weekly feed projections."""
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append(["FEED FORECAST — MONTHLY"])
    ws.append(["Per-month facility-wide feed projections from biology."])
    ws.append([])
    ws.append(["Month", "Feed_kg_month", "Feed_kg_day (avg)", "Feed_kg_day (peak)"])

    from collections import defaultdict
    by_month: dict[str, float] = defaultdict(float)
    by_month_peak: dict[str, float] = defaultdict(float)
    by_month_days: dict[str, int] = defaultdict(int)
    for batch_id, states in biology_states_by_batch.items():
        for s in states:
            mo = s.week_start.strftime("%Y-%m") if hasattr(s.week_start, "strftime") else str(s.week_start)[:7]
            by_month[mo] += s.feed_kg_week
            by_month_peak[mo] = max(by_month_peak[mo], s.feed_kg_day)
            by_month_days[mo] += 7

    for mo in sorted(by_month.keys()):
        days = by_month_days[mo] or 1
        avg_day = by_month[mo] / days
        ws.append([
            mo,
            round(by_month[mo], 0),
            round(avg_day, 0),
            round(by_month_peak[mo], 0),
        ])
    widths = {1: 10, 2: 15, 3: 19, 4: 19}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w


def write_weekly_report(
    wb,
    batch_locations,
    harvest_events,
    batch_week_states=None,
    sheet_name: str = "WeeklyReport",
) -> None:
    """Per-(week, batch) aggregated snapshot: count, biomass, feed, harvest, cull.

    Rolls up the BatchLocations rows (which are per-tank) into one row
    per (week, batch). Adds harvest + cull totals from harvest_events
    and batch_week_states. FW-pre-TranOG weeks with culls but no
    BatchLocations rows still emit a row carrying the cull totals.
    """
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append(["WEEKLY REPORT"])
    ws.append(["Per-(week, batch) aggregate across all tanks. Auto-generated."])
    ws.append([])
    ws.append([
        "Week", "Week_Start", "Batch", "Tanks",
        "Count (fish)", "AvgWt (kg)", "Biomass (kg)",
        "Peak_Density (kg/m3)",
        "Harvest_Count (fish)", "Harvest_Biomass (kg)",
        "Cull_Count (fish)", "Cull_Biomass (kg)",
    ])

    # Aggregate locations by (week, batch).
    from collections import defaultdict
    agg: dict[tuple, dict] = defaultdict(
        lambda: {"tanks": [], "count": 0.0, "biomass": 0.0, "peak_density": 0.0,
                 "week_start": None},
    )
    for r in batch_locations:
        key = (r.week_label, r.batch_id)
        e = agg[key]
        e["tanks"].append(r.tank_id)
        e["count"] += r.count
        e["biomass"] += r.biomass_kg
        e["peak_density"] = max(e["peak_density"], r.density_kg_m3)
        e["week_start"] = r.week_start

    # Harvests by (week, batch).
    harvest_agg: dict[tuple, dict] = defaultdict(lambda: {"count": 0.0, "biomass": 0.0})
    for ev in harvest_events:
        wk = iso_week_label(ev.event_date)
        e = harvest_agg[(wk, ev.batch_id)]
        e["count"] += ev.count
        e["biomass"] += ev.count * ev.avg_wt_g / 1000.0

    # Culls by (week, batch) from biology projection.
    cull_agg: dict[tuple, dict] = defaultdict(lambda: {"count": 0.0, "biomass": 0.0, "week_start": None})
    for s in batch_week_states or ():
        if s.cull_count_week <= 0:
            continue
        e = cull_agg[(s.week_label, s.batch_id)]
        e["count"] += s.cull_count_week
        e["biomass"] += s.cull_biomass_kg_week
        e["week_start"] = s.week_start

    rows = sorted(set(agg.keys()) | set(cull_agg.keys()))
    for k in rows:
        wk, b = k
        a = agg.get(k, {"tanks": [], "count": 0.0, "biomass": 0.0,
                        "peak_density": 0.0, "week_start": None})
        h = harvest_agg.get(k, {"count": 0.0, "biomass": 0.0})
        c = cull_agg.get(k, {"count": 0.0, "biomass": 0.0, "week_start": None})
        avg_wt_kg = (a["biomass"] / a["count"]) if a["count"] > 0 else 0.0
        ws.append([
            wk, a["week_start"] or c["week_start"], b,
            ",".join(str(t) for t in sorted(a["tanks"])) if a["tanks"] else None,
            round(a["count"], 0) if a["count"] > 0 else None,
            round(avg_wt_kg, 3) if a["count"] > 0 else None,
            round(a["biomass"], 0) if a["biomass"] > 0 else None,
            round(a["peak_density"], 1) if a["peak_density"] > 0 else None,
            round(h["count"], 0) if h["count"] > 0 else None,
            round(h["biomass"], 0) if h["biomass"] > 0 else None,
            round(c["count"], 0) if c["count"] > 0 else None,
            round(c["biomass"], 1) if c["biomass"] > 0 else None,
        ])
    widths = {1: 11, 2: 12, 3: 8, 4: 18, 5: 13, 6: 11, 7: 13, 8: 18,
              9: 18, 10: 19, 11: 16, 12: 17}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w


def write_monthly_report(
    wb,
    batch_locations,
    harvest_events,
    batch_week_states=None,
    sheet_name: str = "MonthlyReport",
) -> None:
    """Per-(month, batch) rollup of weekly per-batch state + harvest + cull."""
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append(["MONTHLY REPORT"])
    ws.append(["Per-(month, batch) aggregate. Closing-of-month state + harvest sum + cull sum."])
    ws.append([])
    ws.append([
        "Month", "Batch", "Closing_Tanks",
        "Closing_Count (fish)", "Closing_AvgWt (kg)", "Closing_Biomass (kg)",
        "Harvest_Count (fish)", "Harvest_Biomass (kg)",
        "Cull_Count (fish)", "Cull_Biomass (kg)",
    ])

    from collections import defaultdict
    # For closing state per (month, batch): take the last week in the month.
    by_month_batch: dict[tuple[str, str], dict] = {}
    for r in batch_locations:
        mo = r.week_start.strftime("%Y-%m") if hasattr(r.week_start, "strftime") else str(r.week_start)[:7]
        key = (mo, r.batch_id)
        e = by_month_batch.setdefault(key, {
            "last_week": "", "tanks": [], "count": 0.0, "biomass": 0.0,
        })
        # Reset on new week within the month (we want the LAST week's snapshot).
        if r.week_label > e["last_week"]:
            e["last_week"] = r.week_label
            e["tanks"] = []
            e["count"] = 0.0
            e["biomass"] = 0.0
        if r.week_label == e["last_week"]:
            e["tanks"].append(r.tank_id)
            e["count"] += r.count
            e["biomass"] += r.biomass_kg

    harvest_agg: dict[tuple, dict] = defaultdict(lambda: {"count": 0.0, "biomass": 0.0})
    for ev in harvest_events:
        ev_d = ev.event_date.date() if hasattr(ev.event_date, "date") else ev.event_date
        mo = ev_d.strftime("%Y-%m")
        e = harvest_agg[(mo, ev.batch_id)]
        e["count"] += ev.count
        e["biomass"] += ev.count * ev.avg_wt_g / 1000.0

    cull_agg: dict[tuple, dict] = defaultdict(lambda: {"count": 0.0, "biomass": 0.0})
    for s in batch_week_states or ():
        if s.cull_count_week <= 0:
            continue
        mo = s.week_start.strftime("%Y-%m") if hasattr(s.week_start, "strftime") else str(s.week_start)[:7]
        e = cull_agg[(mo, s.batch_id)]
        e["count"] += s.cull_count_week
        e["biomass"] += s.cull_biomass_kg_week

    keys = sorted(set(by_month_batch) | set(harvest_agg) | set(cull_agg))
    for k in keys:
        mo, b = k
        a = by_month_batch.get(k, {"tanks": [], "count": 0.0, "biomass": 0.0})
        h = harvest_agg.get(k, {"count": 0.0, "biomass": 0.0})
        c = cull_agg.get(k, {"count": 0.0, "biomass": 0.0})
        avg_wt = (a["biomass"] / a["count"]) if a["count"] > 0 else 0.0
        ws.append([
            mo, b,
            ",".join(str(t) for t in sorted(a["tanks"])) if a["tanks"] else None,
            round(a["count"], 0) if a["count"] > 0 else None,
            round(avg_wt, 3) if a["count"] > 0 else None,
            round(a["biomass"], 0) if a["biomass"] > 0 else None,
            round(h["count"], 0) if h["count"] > 0 else None,
            round(h["biomass"], 0) if h["biomass"] > 0 else None,
            round(c["count"], 0) if c["count"] > 0 else None,
            round(c["biomass"], 1) if c["biomass"] > 0 else None,
        ])
    widths = {1: 10, 2: 8, 3: 18, 4: 19, 5: 17, 6: 17, 7: 18, 8: 19, 9: 16, 10: 17}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w


def write_reconciliation_report(
    wb,
    batch_locations,
    batch_week_states,
    harvest_events,
    tranog_events,
    initial_state,
    sheet_name: str = "ReconciliationReport",
) -> None:
    """Per-(batch, week) count + biomass balance check (OG side).

    Formula: open - mortality - harvest + input = expected_close.
    (Cull is FW-side: applied before fish reach OG; the `input` count
    is already POST-cull, so cull doesn't enter this OG-side balance.
    Shown in the output for transparency only.)

    Open for first week = PR-hydrated initial count (in-flight batches)
    or 0 (incoming batches arrive via TranOG events).
    """
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append(["RECONCILIATION REPORT"])
    ws.append([
        "Per-(batch, week) count + biomass balance. open - mortality - cull - "
        "harvest + input = expected_close. Mismatches above tolerance are flagged."
    ])
    ws.append([])
    ws.append([
        "Week", "Batch",
        "Open_Count", "Mortality_Count", "Cull_Count", "Harvest_Count",
        "Input_Count", "Expected_Close", "Actual_Close",
        "Count_Delta",
        "Open_Bio_kg", "Growth_kg", "Mort_kg", "Cull_kg",
        "Harvest_kg", "Input_kg",
        "Expected_Bio_kg", "Actual_Bio_kg", "Biomass_Delta_kg",
        "Flag",
    ])

    from collections import defaultdict

    # Initial counts/biomass from PR-hydrated state.
    pr_count: dict[str, float] = defaultdict(float)
    pr_biomass: dict[str, float] = defaultdict(float)
    if initial_state is not None:
        for tank in initial_state.tanks_by_id.values():
            if tank.batch_id:
                pr_count[tank.batch_id] += tank.count
                pr_biomass[tank.batch_id] += tank.biomass_kg

    # Per-(batch, week) aggregates from BatchLocations.
    loc_count: dict[tuple[str, str], float] = defaultdict(float)
    loc_biomass: dict[tuple[str, str], float] = defaultdict(float)
    weeks_seen: set[str] = set()
    week_start_by_label: dict[str, object] = {}
    for r in batch_locations:
        loc_count[(r.batch_id, r.week_label)] += r.count
        loc_biomass[(r.batch_id, r.week_label)] += r.biomass_kg
        weeks_seen.add(r.week_label)
        week_start_by_label[r.week_label] = r.week_start
    weeks = sorted(weeks_seen)

    # Per-(batch, week) mortality % + cull count + cull biomass + SGR.
    mort_pct: dict[tuple[str, str], float] = {}
    sgr_pct_day: dict[tuple[str, str], float] = {}
    cull_count: dict[tuple[str, str], float] = {}
    cull_biomass: dict[tuple[str, str], float] = {}
    for s in (batch_week_states or []):
        mort_pct[(s.batch_id, s.week_label)] = s.mortality_pct_weekly
        sgr_pct_day[(s.batch_id, s.week_label)] = s.sgr_pct_day
        cull_count[(s.batch_id, s.week_label)] = s.cull_count_week or 0
        cull_biomass[(s.batch_id, s.week_label)] = s.cull_biomass_kg_week or 0

    # Per-(batch, week) harvest count + biomass from events.
    from .time_grid import iso_week_label
    harv_count: dict[tuple[str, str], float] = defaultdict(float)
    harv_biomass: dict[tuple[str, str], float] = defaultdict(float)
    for ev in (harvest_events or []):
        wk = iso_week_label(ev.event_date)
        harv_count[(ev.batch_id, wk)] += ev.count
        harv_biomass[(ev.batch_id, wk)] += ev.count * ev.avg_wt_g / 1000.0

    # Per-(batch, week) input count + biomass from TranOG events.
    tin_count: dict[tuple[str, str], float] = defaultdict(float)
    tin_biomass: dict[tuple[str, str], float] = defaultdict(float)
    for ev in (tranog_events or []):
        wk = iso_week_label(ev.event_date)
        total_c = sum(d.count for d in ev.destinations)
        total_b = sum(d.count * d.avg_wt_g / 1000.0 for d in ev.destinations)
        tin_count[(ev.batch_id, wk)] += total_c
        tin_biomass[(ev.batch_id, wk)] += total_b

    # Walk batches × weeks, write rows + flag mismatches.
    TOLERANCE = 100.0   # fish; below this absolute, treat as numerical noise
    all_batches = sorted({b for (b, _) in loc_count} | set(pr_count.keys()))
    for batch in all_batches:
        prev_count = pr_count.get(batch, 0.0)
        prev_biomass = pr_biomass.get(batch, 0.0)
        for wk in weeks:
            actual_c = loc_count.get((batch, wk), 0.0)
            actual_b = loc_biomass.get((batch, wk), 0.0)
            if prev_count == 0 and actual_c == 0:
                continue
            m_pct = mort_pct.get((batch, wk), 0.0) or 0.0
            mort = prev_count * (m_pct / 100.0)
            cull = cull_count.get((batch, wk), 0.0) or 0.0
            cull_b = cull_biomass.get((batch, wk), 0.0) or 0.0
            hv_c = harv_count.get((batch, wk), 0.0)
            hv_b = harv_biomass.get((batch, wk), 0.0)
            in_c = tin_count.get((batch, wk), 0.0)
            in_b = tin_biomass.get((batch, wk), 0.0)
            # OG-side balance: cull is FW-side (input is already post-cull),
            # so cull is shown as informational but not subtracted.
            expected_c = prev_count - mort - hv_c + in_c
            # Biomass timing (matches Phase D order):
            #   pre-biology: harvest
            #   biology: growth + mortality + TranOG (mid-week arrival)
            # TranOG fish only get partial-week growth (~half).
            sgr = sgr_pct_day.get((batch, wk), 0.0)
            growth_factor = (1.0 + sgr / 100.0) ** 7
            partial_factor = 1.0 + (growth_factor - 1.0) * 0.5
            bio_full_growth = prev_biomass - hv_b
            growth_full = bio_full_growth * (growth_factor - 1.0)
            growth_tnin = in_b * (partial_factor - 1.0)
            growth_kg = growth_full + growth_tnin
            mort_kg = bio_full_growth * (m_pct / 100.0)
            expected_b = bio_full_growth + growth_full - mort_kg + in_b + growth_tnin
            delta_c = actual_c - expected_c
            delta_b = actual_b - expected_b
            flag = ""
            if abs(delta_c) > TOLERANCE and abs(delta_c) > 0.005 * max(actual_c, prev_count, 1):
                flag = "COUNT_DRIFT"
            elif abs(delta_b) > 1000 and abs(delta_b) > 0.02 * max(actual_b, prev_biomass, 1):
                flag = "BIO_DRIFT"
            ws.append([
                wk, batch,
                round(prev_count, 0),
                round(mort, 0) if mort > 0 else None,
                round(cull, 0) if cull > 0 else None,
                round(hv_c, 0) if hv_c > 0 else None,
                round(in_c, 0) if in_c > 0 else None,
                round(expected_c, 0),
                round(actual_c, 0),
                round(delta_c, 0),
                round(prev_biomass, 0),
                round(growth_kg, 0) if growth_kg > 0 else None,
                round(mort_kg, 0) if mort_kg > 0 else None,
                round(cull_b, 0) if cull_b > 0 else None,
                round(hv_b, 0) if hv_b > 0 else None,
                round(in_b, 0) if in_b > 0 else None,
                round(expected_b, 0),
                round(actual_b, 0),
                round(delta_b, 0),
                flag,
            ])
            prev_count = actual_c
            prev_biomass = actual_b

    widths = {1: 11, 2: 7, 3: 11, 4: 13, 5: 11, 6: 13, 7: 11, 8: 14,
              9: 13, 10: 12,
              11: 12, 12: 11, 13: 10, 14: 9, 15: 12, 16: 11,
              17: 16, 18: 14, 19: 16, 20: 11}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w


def write_tank_continuity_audit(
    wb,
    batch_locations,
    batch_week_states,
    harvest_events,
    transfer_events,
    grade_events,
    tranog_events,
    initial_state,
    sheet_name: str = "TankContinuityAudit",
) -> None:
    """Per-(tank, week, batch) reconciliation.

    Formula: open - mortality - harvest_out - transfer_out + transfer_in
             - grade_out + grade_in + tranog_in = expected_close
    Compares to BatchLocations actual close. Flags any drift > tolerance.

    Open for first week = PR-hydrated initial tank count (in-flight).
    Batch transitions in a tank: when tank batch changes week-over-week,
    each batch gets its own row showing its arrival/departure path.
    """
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append(["TANK CONTINUITY AUDIT"])
    ws.append([
        "Per-(tank, week) count balance: every count change is accounted for "
        "by an event (mortality, harvest, transfer, grade, TranOG)."
    ])
    ws.append([])
    ws.append([
        # Count balance
        "Week", "Tank", "Batch", "Open_Count", "Mortality",
        "Harvest_Out", "Transfer_Out", "Transfer_In",
        "Grade_Out", "Grade_In", "TranOG_In",
        "Expected_Close", "Actual_Close", "Delta", "Flag",
        # Biomass balance
        "Open_Bio_kg", "Growth_kg", "Mort_kg",
        "Harvest_Out_kg", "Transfer_Out_kg", "Transfer_In_kg",
        "Grade_Out_kg", "Grade_In_kg", "TranOG_In_kg",
        "Expected_Close_kg", "Actual_Close_kg", "Delta_kg", "Bio_Flag",
    ])

    from collections import defaultdict
    from .time_grid import iso_week_label

    # Per-(tank, week) state from BatchLocations.
    tank_wk_state: dict[tuple[int, str], tuple[str, float]] = {}
    weeks_seen: set[str] = set()
    for r in batch_locations:
        tank_wk_state[(r.tank_id, r.week_label)] = (r.batch_id, r.count)
        weeks_seen.add(r.week_label)
    weeks = sorted(weeks_seen)
    all_tanks = sorted({t for (t, _) in tank_wk_state})

    # PR-hydrated initial tank state.
    pr_tank: dict[int, tuple[str | None, float]] = {}
    if initial_state is not None:
        for tid, tank in initial_state.tanks_by_id.items():
            if not tank.is_empty:
                pr_tank[tid] = (tank.batch_id, tank.count)

    # Per-batch per-week mortality % + SGR (from BatchWeekState).
    mort_pct: dict[tuple[str, str], float] = {}
    sgr_pct_day: dict[tuple[str, str], float] = {}
    for s in (batch_week_states or []):
        mort_pct[(s.batch_id, s.week_label)] = s.mortality_pct_weekly
        sgr_pct_day[(s.batch_id, s.week_label)] = s.sgr_pct_day

    # Per-(tank, week) event aggregates — counts AND biomass (kg).
    harvest_out: dict[tuple[int, str], float] = defaultdict(float)
    harvest_out_kg: dict[tuple[int, str], float] = defaultdict(float)
    for ev in (harvest_events or []):
        wk = iso_week_label(ev.event_date)
        harvest_out[(ev.source_tank_id, wk)] += ev.count
        harvest_out_kg[(ev.source_tank_id, wk)] += ev.count * ev.avg_wt_g / 1000.0

    transfer_out: dict[tuple[int, str], float] = defaultdict(float)
    transfer_out_kg: dict[tuple[int, str], float] = defaultdict(float)
    transfer_in: dict[tuple[int, str], float] = defaultdict(float)
    transfer_in_kg: dict[tuple[int, str], float] = defaultdict(float)
    for ev in (transfer_events or []):
        ct = getattr(ev, "count_transferred", None)
        if ct is None or ct <= 0:
            continue   # rejected transfers don't count
        wk = iso_week_label(ev.event_date)
        # Split count_transferred proportionally across destinations.
        total_planned = sum(d.count for d in ev.destinations) or 1.0
        # Weighted-average avg_wt across destinations (~source avg_wt).
        avg_wt_g = sum(d.count * d.avg_wt_g for d in ev.destinations) / total_planned
        transfer_out[(ev.source_tank_id, wk)] += ct
        transfer_out_kg[(ev.source_tank_id, wk)] += ct * avg_wt_g / 1000.0
        for d in ev.destinations:
            share = d.count / total_planned
            transfer_in[(d.tank_id, wk)] += ct * share
            transfer_in_kg[(d.tank_id, wk)] += ct * share * d.avg_wt_g / 1000.0

    grade_out: dict[tuple[int, str], float] = defaultdict(float)
    grade_out_kg: dict[tuple[int, str], float] = defaultdict(float)
    grade_in: dict[tuple[int, str], float] = defaultdict(float)
    grade_in_kg: dict[tuple[int, str], float] = defaultdict(float)
    for ev in (grade_events or []):
        wk = iso_week_label(ev.event_date)
        total_dest = sum(d.count for d in ev.destinations)
        avg_wt_g = (sum(d.count * d.avg_wt_g for d in ev.destinations) / total_dest
                    if total_dest > 0 else 0.0)
        for src_tid in ev.source_tank_ids:
            grade_out[(src_tid, wk)] += total_dest / len(ev.source_tank_ids)
            grade_out_kg[(src_tid, wk)] += (
                total_dest / len(ev.source_tank_ids) * avg_wt_g / 1000.0
            )
        for d in ev.destinations:
            grade_in[(d.tank_id, wk)] += d.count
            grade_in_kg[(d.tank_id, wk)] += d.count * d.avg_wt_g / 1000.0

    tranog_in: dict[tuple[int, str], float] = defaultdict(float)
    tranog_in_kg: dict[tuple[int, str], float] = defaultdict(float)
    for ev in (tranog_events or []):
        wk = iso_week_label(ev.event_date)
        for d in ev.destinations:
            tranog_in[(d.tank_id, wk)] += d.count
            tranog_in_kg[(d.tank_id, wk)] += d.count * d.avg_wt_g / 1000.0

    # Per-(tank, week) biomass from BatchLocations.
    tank_wk_bio: dict[tuple[int, str], float] = {}
    for r in batch_locations:
        tank_wk_bio[(r.tank_id, r.week_label)] = r.biomass_kg

    pr_tank_bio: dict[int, float] = {}
    if initial_state is not None:
        for tid, tank in initial_state.tanks_by_id.items():
            if not tank.is_empty:
                pr_tank_bio[tid] = tank.biomass_kg

    TOLERANCE = 50.0      # fish
    BIO_TOLERANCE = 500.0  # kg

    for tid in all_tanks:
        prev_batch, prev_count = pr_tank.get(tid, (None, 0.0))
        prev_biomass = pr_tank_bio.get(tid, 0.0)
        for wk in weeks:
            cur = tank_wk_state.get((tid, wk))
            cur_batch, cur_count = cur if cur else (None, 0.0)
            cur_biomass = tank_wk_bio.get((tid, wk), 0.0)
            if prev_count == 0 and cur_count == 0:
                continue

            # ---- Count balance ----
            m_pct = mort_pct.get((prev_batch, wk), 0.0) if prev_batch else 0.0
            mort = prev_count * (m_pct / 100.0)
            h_out = harvest_out.get((tid, wk), 0.0)
            t_out = transfer_out.get((tid, wk), 0.0)
            t_in = transfer_in.get((tid, wk), 0.0)
            g_out = grade_out.get((tid, wk), 0.0)
            g_in = grade_in.get((tid, wk), 0.0)
            tn_in = tranog_in.get((tid, wk), 0.0)
            expected_count = prev_count - mort - h_out - t_out + t_in - g_out + g_in + tn_in
            delta_count = cur_count - expected_count
            flag = ""
            if abs(delta_count) > TOLERANCE and abs(delta_count) > 0.005 * max(cur_count, prev_count, 1):
                flag = "TANK_DRIFT"

            # ---- Biomass balance ----
            # Phase D event order per week:
            #   1) 6N purge harvests + Layer 2 harvest demands (pre-biology)
            #   2) Migration transfers (pre-biology)
            #   3) Day-by-day biology: mortality + growth + TranOG entries
            #   4) Density-trigger Grade events (post-biology)
            h_out_kg = harvest_out_kg.get((tid, wk), 0.0)
            t_out_kg = transfer_out_kg.get((tid, wk), 0.0)
            t_in_kg = transfer_in_kg.get((tid, wk), 0.0)
            g_out_kg = grade_out_kg.get((tid, wk), 0.0)
            g_in_kg = grade_in_kg.get((tid, wk), 0.0)
            tn_in_kg = tranog_in_kg.get((tid, wk), 0.0)
            # Use the batch present in tank DURING biology for SGR.
            bio_batch = cur_batch or prev_batch
            sgr = sgr_pct_day.get((bio_batch, wk), 0.0) if bio_batch else 0.0
            growth_factor = (1.0 + sgr / 100.0) ** 7
            # TranOG entries fire MID-WEEK (the TranOG date can fall any
            # day in the ISO week), so they only get partial-week growth.
            # Approximate as half-week growth for tn_in fish.
            partial_factor = 1.0 + (growth_factor - 1.0) * 0.5
            # Biomass that grew the FULL week (in tank at start of biology):
            bio_full_growth = prev_biomass - h_out_kg - t_out_kg + t_in_kg
            growth_full = bio_full_growth * (growth_factor - 1.0)
            growth_tnin = tn_in_kg * (partial_factor - 1.0)
            growth_kg = growth_full + growth_tnin
            mort_kg = bio_full_growth * (m_pct / 100.0)  # tn_in too young
            # Expected close = biomass after biology, then grade events:
            bio_after_biology = bio_full_growth + growth_full - mort_kg + tn_in_kg + growth_tnin
            expected_bio = bio_after_biology - g_out_kg + g_in_kg
            delta_bio = cur_biomass - expected_bio
            bio_flag = ""
            if abs(delta_bio) > BIO_TOLERANCE and abs(delta_bio) > 0.01 * max(cur_biomass, prev_biomass, 1):
                bio_flag = "BIO_DRIFT"

            display_batch = cur_batch or (prev_batch if prev_count > 0 else "")
            ws.append([
                wk, tid, display_batch,
                round(prev_count, 0),
                round(mort, 0) if mort > 0 else None,
                round(h_out, 0) if h_out > 0 else None,
                round(t_out, 0) if t_out > 0 else None,
                round(t_in, 0) if t_in > 0 else None,
                round(g_out, 0) if g_out > 0 else None,
                round(g_in, 0) if g_in > 0 else None,
                round(tn_in, 0) if tn_in > 0 else None,
                round(expected_count, 0),
                round(cur_count, 0),
                round(delta_count, 0),
                flag,
                round(prev_biomass, 0),
                round(growth_kg, 0) if growth_kg > 0 else None,
                round(mort_kg, 0) if mort_kg > 0 else None,
                round(h_out_kg, 0) if h_out_kg > 0 else None,
                round(t_out_kg, 0) if t_out_kg > 0 else None,
                round(t_in_kg, 0) if t_in_kg > 0 else None,
                round(g_out_kg, 0) if g_out_kg > 0 else None,
                round(g_in_kg, 0) if g_in_kg > 0 else None,
                round(tn_in_kg, 0) if tn_in_kg > 0 else None,
                round(expected_bio, 0),
                round(cur_biomass, 0),
                round(delta_bio, 0),
                bio_flag,
            ])
            prev_batch = cur_batch
            prev_count = cur_count
            prev_biomass = cur_biomass

    widths = {1: 11, 2: 6, 3: 7, 4: 11, 5: 10, 6: 11, 7: 11, 8: 11,
              9: 9, 10: 9, 11: 10, 12: 14, 13: 12, 14: 9, 15: 11,
              16: 12, 17: 11, 18: 11, 19: 13, 20: 14, 21: 13,
              22: 11, 23: 11, 24: 11, 25: 16, 26: 14, 27: 11, 28: 9}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w


def write_facility_map(
    wb,
    batch_locations,
    facility,
    sheet_name: str = "FacilityMap",
) -> None:
    """Tank × Week matrix showing which batch occupies each tank each week.

    Cell value is the batch_id (or blank if empty). Rows are tanks
    ordered by system then tank_id; columns are forecast weeks in
    chronological order.
    """
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append(["FACILITY MAP"])
    ws.append(["Per-tank occupancy across weeks. Cell = batch_id; blank = empty."])
    ws.append([])

    # Order tanks by system + tank_id (OG tanks only for compactness).
    og_tanks = sorted(
        [t for t in facility.tanks if t.type == "OG"],
        key=lambda t: (t.system_id, t.tank_id),
    )
    # Weeks in chronological order.
    weeks = sorted({r.week_label for r in batch_locations})

    # Build occupancy map (tank, week) → batch_id
    occ: dict[tuple[int, str], str] = {}
    for r in batch_locations:
        occ[(r.tank_id, r.week_label)] = r.batch_id

    # Header row: Tank | System | Vol_m3 | <week1> | <week2> | ...
    header = ["Tank", "System", "Vol_m3"] + weeks
    ws.append(header)

    for t in og_tanks:
        row = [t.location_id, t.system_id, t.volume_m3]
        for wk in weeks:
            row.append(occ.get((t.tank_id, wk), ""))
        ws.append(row)
    # Column widths
    ws.column_dimensions[get_column_letter(1)].width = 10
    ws.column_dimensions[get_column_letter(2)].width = 7
    ws.column_dimensions[get_column_letter(3)].width = 8
    for c in range(4, 4 + len(weeks)):
        ws.column_dimensions[get_column_letter(c)].width = 10


def write_advisory(
    wb,
    residuals,
    placement_warnings,
    scheduler_warnings,
    bottlenecks,
    density_violations=None,
    invariant_warnings=None,
    sheet_name: str = "Advisory",
) -> None:
    """Consolidated diagnostics + warnings from every layer.

    Args:
        residuals: FW calibration residuals.
        placement_warnings: strings from the placement walk (INV-x,
            TranOG, 6N, Phase B/C/D).
        scheduler_warnings: harvest scheduler warning strings.
        bottlenecks: precalc-detected supply/demand gaps.
        density_violations: iterable of tuples
            (week_label, location_id, batch_id, density, cap_kg_m3) for
            every BatchLocations row that exceeds its tank's density cap.
        invariant_warnings: strings from hydration + check_invariants
            (INV-1/5 + density at snapshot time).
    """
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append(["ADVISORY"])
    ws.append([f"Generated: {datetime.now().isoformat(timespec='seconds')}"])

    entries: list[tuple[str, str]] = []
    for r in residuals:
        if abs(r.residual_pct) >= 0.5:
            sug = (f"; suggested FW_Correction = {r.suggested_fw_correction:.3f}"
                   if r.suggested_fw_correction is not None else "")
            entries.append((
                "FW Calibration",
                f"Batch {r.batch_id} TranOG {r.tran_og_date.date()}: "
                f"residual {r.residual_pct:+.2f}%{sug}",
            ))
    for b in bottlenecks:
        entries.append((f"Bottleneck / {b.kind}", f"{b.week_label}: {b.detail}"))
    for w in scheduler_warnings:
        entries.append(("Harvest Scheduler", w))
    for w in invariant_warnings or ():
        if "INV-1" in w:
            cat = "INV-1 (one-batch-per-tank)"
        elif "INV-5" in w:
            cat = "INV-5 (min_tank_control)"
        elif "Density" in w:
            cat = "Hydration density"
        else:
            cat = "Hydration / Invariants"
        entries.append((cat, w))
    for v in density_violations or ():
        wk, loc, bid, d, cap = v
        entries.append((
            "Density violation",
            f"{wk}: tank {loc} (batch {bid}) at {d:.1f} kg/m³ "
            f"> cap {cap:.1f}",
        ))
    for w in placement_warnings:
        if "INV-4" in w:
            cat = "INV-4 (1 kg rule)"
        elif "INV-5" in w:
            cat = "INV-5 (min_tank_control)"
        elif "INV-1" in w:
            cat = "INV-1 (one-batch-per-tank)"
        elif "6N" in w or "purge" in w.lower():
            cat = "6N Pipeline"
        elif "TranOG" in w:
            cat = "TranOG Entry"
        elif w.startswith("[B]"):
            cat = "Placement / Phase B"
        elif w.startswith("[C]"):
            cat = "Placement / Phase C"
        elif w.startswith("[D]"):
            cat = "Placement / Phase D"
        else:
            cat = "Placement"
        entries.append((cat, w))

    # Summary roll-up by category (count + first example).
    by_cat: dict[str, int] = {}
    for cat, _ in entries:
        by_cat[cat] = by_cat.get(cat, 0) + 1

    ws.append([f"{len(entries)} issue(s) found across {len(by_cat)} categories"])
    ws.append([])
    ws.append(["Summary by category"])
    ws.append(["Category", "Count"])
    for cat in sorted(by_cat, key=lambda c: -by_cat[c]):
        ws.append([cat, by_cat[cat]])
    ws.append([])
    ws.append(["Full list"])
    ws.append(["#", "Category", "Detail"])
    for i, (cat, detail) in enumerate(entries, 1):
        ws.append([i, cat, detail])

    widths = {1: 6, 2: 30, 3: 110}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w


# Regex helpers used by both write_advisory categorization and ValidationLog
# message parsing.
_RE_WEEK = re.compile(r"\b(\d{4}-W\d{2})\b")
_RE_BATCH = re.compile(r"\bB\d{2,3}\b")
_RE_TANK = re.compile(r"\b(OG[1-6][NS])-(\d+)\b")


def _vlog_parse(msg: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Best-effort extraction of (week_label, batch_id, location_id) from
    a free-form warning string."""
    week = (_RE_WEEK.search(msg).group(1) if _RE_WEEK.search(msg) else None)
    batch = (_RE_BATCH.search(msg).group(0) if _RE_BATCH.search(msg) else None)
    tm = _RE_TANK.search(msg)
    loc = tm.group(0) if tm else None
    return week, batch, loc


def write_validation_log(
    wb,
    residuals=None,
    placement_warnings=None,
    scheduler_warnings=None,
    bottlenecks=None,
    density_violations=None,
    invariant_warnings=None,
    sheet_name: str = "ValidationLog",
) -> None:
    """Raw per-event audit trail.

    One row per warning/diagnostic, sortable by severity / source / code /
    week / batch / tank. Companion to Advisory: Advisory is the curated
    operator-facing summary, ValidationLog is the structured stream for
    filtering and triage.
    """
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append([
        "Severity", "Source", "Code", "Week", "Batch", "Tank", "Message",
    ])

    def emit(sev, src, code, msg):
        wk, b, t = _vlog_parse(msg)
        ws.append([sev, src, code, wk or "", b or "", t or "", msg])

    for r in residuals or ():
        if abs(r.residual_pct) >= 0.5:
            sev = "WARN" if abs(r.residual_pct) >= 1.0 else "INFO"
            sug = (f"; suggested FW_Correction={r.suggested_fw_correction:.3f}"
                   if r.suggested_fw_correction is not None else "")
            emit(sev, "FWCalibration", "FW_RESIDUAL",
                 f"Batch {r.batch_id} TranOG {r.tran_og_date.date()}: "
                 f"residual {r.residual_pct:+.2f}%{sug}")

    for b in bottlenecks or ():
        emit("WARN", "Precalc", f"BOTTLE_{b.kind.upper()}",
             f"{b.week_label}: {b.detail}")

    for w in scheduler_warnings or ():
        emit("WARN", "HarvestScheduler", "HSCHED", w)

    for w in invariant_warnings or ():
        if "INV-1" in w:
            code = "INV1"
        elif "INV-5" in w:
            code = "INV5"
        elif "Density" in w:
            code = "DENSITY_HYDRATION"
        else:
            code = "HYDRATION"
        emit("WARN", "Hydration", code, w)

    for v in density_violations or ():
        wk, loc, bid, d, cap = v
        ws.append([
            "WARN", "PhaseD", "DENSITY", wk, bid, loc,
            f"{loc} (batch {bid}) at {d:.1f} kg/m³ > cap {cap:.1f}",
        ])

    for w in placement_warnings or ():
        if "INV-4" in w:
            code = "INV4"
        elif "INV-5" in w:
            code = "INV5"
        elif "INV-1" in w:
            code = "INV1"
        elif "TranOG" in w:
            code = "TRANOG"
        elif "6N" in w or "purge" in w.lower():
            code = "SIXN"
        else:
            code = "PLACEMENT"
        # Source tag from Phase B/C/D prefix if present.
        if w.startswith("[B]"):
            src = "PhaseB"
        elif w.startswith("[C]"):
            src = "PhaseC"
        elif w.startswith("[D]"):
            src = "PhaseD"
        else:
            src = "Placement"
        emit("WARN", src, code, w)

    widths = {1: 9, 2: 14, 3: 18, 4: 10, 5: 8, 6: 10, 7: 110}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = "A2"


def write_forecast_start(wb, forecast_start, sheet_name: str = "Control") -> None:
    """Write the derived forecast start back into the Control INPUT cell.

    Mirrors the VBA DetectForecastStart(), which writes the derived start
    (= ProductionReport closing + 1 day) into Control B3 on every run so
    the input cell never drifts from the ProductionReport. This closes the
    gap between a stale, un-reconciled input sheet and a post-run sheet:
    after a run, `B3 == PR closing + 1` is an invariant that means
    "reconciled". The status-echo row (R11, written by
    write_control_status) is informational only — this writer keeps the
    actual INPUT cell honest.

    Finds the 'Forecast Start Date' label in column A (the same label
    read_control matches), falling back to the VBA's B3 convention.
    """
    if sheet_name not in wb.sheetnames:
        return
    ws = wb[sheet_name]
    target_row = None
    for r in range(1, 30):
        a = ws.cell(row=r, column=1).value
        if (isinstance(a, str)
                and re.sub(r"\s+", " ", a.strip().lower()).rstrip(":")
                == "forecast start date"):
            target_row = r
            break
    if target_row is None:
        target_row = 3  # VBA convention: Control!B3
    ws.cell(row=target_row, column=2).value = forecast_start


def write_control_status(
    wb,
    *,
    status: str,
    scenario: str,
    forecast_start,
    horizon_weeks: int,
    batches: int,
    og_tanks: int,
    elapsed_s: float,
    warnings: int,
    sheet_name: str = "Control",
) -> None:
    """Overwrite Control R8-R16 column B with the run summary (DESIGN §1).

    Row layout (label in A, value in B) is fixed by the workbook
    template: R8 Last Run, R9 Timestamp, R10 Scenario, R11 Forecast
    Start, R12 Horizon, R13 Batches, R14 OG Tanks, R15 Elapsed,
    R16 Warnings.
    """
    if sheet_name not in wb.sheetnames:
        return
    ws = wb[sheet_name]
    ws.cell(row=8, column=2).value = status
    ws.cell(row=9, column=2).value = datetime.now()
    ws.cell(row=10, column=2).value = scenario
    ws.cell(row=11, column=2).value = forecast_start
    ws.cell(row=12, column=2).value = f"{horizon_weeks} weeks"
    ws.cell(row=13, column=2).value = batches
    ws.cell(row=14, column=2).value = og_tanks
    ws.cell(row=15, column=2).value = f"{elapsed_s:.1f}s"
    ws.cell(row=16, column=2).value = warnings


def write_calibration_diagnostics(
    wb,
    residuals: Iterable[CalibrationResidual],
    sheet_name: str = "Diagnostics",
) -> None:
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append(["FW Calibration: projected pre-cull avg wt vs target at TranOG_Date, plus suggested FW_Correction"])
    ws.append([
        "Batch", "TranOG_Date", "Target_AvgWt_g",
        "Current_FW_Correction", "Projected_PreCull_AvgWt_g", "Residual_pct",
        "Suggested_FW_Correction",
    ])
    for r in residuals:
        ws.append([
            r.batch_id, r.tran_og_date, round(r.target_avg_wt_g, 2),
            round(r.current_fw_correction, 4),
            round(r.projected_pre_cull_avg_wt_g, 2), round(r.residual_pct, 2),
            None if r.suggested_fw_correction is None else round(r.suggested_fw_correction, 4),
        ])
    widths = {1: 8, 2: 12, 3: 16, 4: 22, 5: 26, 6: 12, 7: 24}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w


# ---------- Workbook helpers ----------

def load_workbook(path: Path):
    return openpyxl.load_workbook(path, keep_vba=True, data_only=True)


def iso_week_label(d: datetime) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def write_system_limits_audit(
    wb,
    batch_locations,
    batch_by_id,
    tables,
    system_limits,
    control,
    sheet_name: str = "SystemLimitsAudit",
):
    """Per-(week, system) REALIZED biomass + feed vs the system caps.

    The engine checks tank density but never checked per-system biomass/feed
    against the SystemLimits caps — so over-cap systems went unsurfaced. This
    audit closes that gap, mirroring TankContinuityAudit. Feed is the realized
    tank feed (biomass × SGR × FCR at the tank's actual weight), NOT the raw
    projection (which over-counts un-harvested fish). Caps are carried forward
    past the data's last week and buffered by Control R29. OG6N (depuration)
    has no biomass/feed caps in purge mode, so it's reported but unflagged.

    Returns (n_biomass_over, n_feed_over, worst_biomass_ratio, worst_feed_ratio).
    """
    from collections import defaultdict
    from .biology import realized_feed_kg_day

    buf = 1.0 + (getattr(control, "global_buffer_pct", 0.0) or 0.0)

    # Carry-forward cap lookup per (system, metric).
    smw: dict = defaultdict(list)
    for (wk, sysid, metric), val in system_limits.caps.items():
        smw[(sysid, metric)].append((wk, val))
    for k in smw:
        smw[k].sort()

    def _cap(wk, sysid, metric):
        lst = smw.get((sysid, metric))
        if not lst:
            return None
        best = lst[0][1]
        for w, v in lst:
            if w <= wk:
                best = v
            else:
                break
        return best

    sb: dict = defaultdict(float)
    sf: dict = defaultdict(float)
    for r in batch_locations:
        if r.count <= 0:
            continue
        b = batch_by_id.get(r.batch_id)
        sb[(r.week_label, r.system_id)] += r.biomass_kg
        sf[(r.week_label, r.system_id)] += realized_feed_kg_day(
            r.avg_wt_g, r.biomass_kg, b, tables)

    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append(["SYSTEM LIMITS AUDIT"])
    ws.append(["Realized per-system biomass + feed vs caps "
               "(carry-forward, +R29 buffer). OG6N = depuration (no caps)."])
    ws.append([])
    ws.append(["Week", "System", "Biomass_kg", "Biomass_cap", "Bio_flag",
               "Feed_kg_day", "Feed_cap", "Feed_flag"])

    nb = nf = 0
    worst_b = worst_f = 0.0
    for (wk, sysid) in sorted(sb.keys()):
        bio = sb[(wk, sysid)]
        feed = sf[(wk, sysid)]
        bcap = _cap(wk, sysid, "biomass")
        fcap = _cap(wk, sysid, "feed_per_day")
        bflag = fflag = ""
        # OG6N is the depuration pool (purge mode): fish starve (no feed) and
        # the system is intentionally uncapped. Report its rows but never flag.
        flaggable = sysid != "OG6N"
        if bcap and flaggable:
            worst_b = max(worst_b, bio / bcap)
            if bio > bcap * buf:
                bflag = "BIOMASS_OVER"
                nb += 1
        if fcap and flaggable:
            worst_f = max(worst_f, feed / fcap)
            if feed > fcap * buf:
                fflag = "FEED_OVER"
                nf += 1
        ws.append([wk, sysid, round(bio, 0),
                   round(bcap, 0) if bcap else None, bflag,
                   round(feed, 1), round(fcap, 0) if fcap else None, fflag])

    widths = {1: 10, 2: 8, 3: 12, 4: 12, 5: 13, 6: 12, 7: 10, 8: 11}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w
    return nb, nf, worst_b, worst_f
