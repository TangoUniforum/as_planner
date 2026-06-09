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
        "Count (fish)", "AvgWt (kg)", "Biomass (kg)", "Density (kg/m3)", "Stage",
    ])
    for r in batch_locations:
        ws.append([
            r.week_label, r.week_start, r.batch_id, r.tank_id, r.system_id,
            round(r.count, 0),
            round(r.avg_wt_g / 1000.0, 3),
            round(r.biomass_kg, 0),
            round(r.density_kg_m3, 1),
            getattr(r, "stage", ""),
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
    """Per-event harvest plan as a single table (matches reference format).

    Columns: Week, Batch, Tank, Count (fish), Gross_AvgWt (kg),
    Gross_Biomass (kg), HOG_Yield (ratio), HOG_AvgWt (kg), HOG_Biomass (kg).

    `facility_limits_hog` is a dict `{week_label: hog_yield}` for per-week HOG
    yield overrides; default falls back to `default_hog_yield`. Pins (if any —
    the workbook is now write-only, so normally none) are honored upstream as
    harvest events and so already appear in `harvest_events`.
    """
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append(["HARVEST PLAN"])
    ws.append(["For each harvest: specify batch, date or week#, tank, counts and biomass."])
    ws.append([])
    ws.append([
        "Week", "Batch", "Tank", "Count (fish)",
        "Gross_AvgWt (kg)", "Gross_Biomass (kg)",
        "HOG_Yield (ratio)", "HOG_AvgWt (kg)", "HOG_Biomass (kg)",
    ])

    events_sorted = sorted(harvest_events, key=lambda e: (e.event_date, e.source_tank_id))
    for ev in events_sorted:
        wk = iso_week_label(ev.event_date)
        gross_avg_kg = ev.avg_wt_g / 1000.0
        gross_biomass = ev.count * gross_avg_kg
        hog_yield = facility_limits_hog.get(wk, default_hog_yield)
        ws.append([
            wk, ev.batch_id, ev.source_tank_id,
            round(ev.count, 0),
            round(gross_avg_kg, 2),
            round(gross_biomass, 0),
            round(hog_yield, 2),
            round(gross_avg_kg * hog_yield, 2),
            round(gross_biomass * hog_yield, 0),
        ])
    widths = {1: 11, 2: 8, 3: 6, 4: 13, 5: 16, 6: 17, 7: 17, 8: 14, 9: 16}
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
    """Per-event transfer + TranOG + Grade plan as a single table (matches
    reference format).

    Columns: Week, Batch, From_Tank, To_Tank, Count (fish), Avg_Weight (kg),
    Grade, CV (%). From_Tank is 'FW' for TranOG entries; a Grade event's
    multi-tank source is comma-separated. The Grade column carries the grade
    class (A/B/big/small) for Grade events, blank otherwise. Rejected transfer
    attempts (count_transferred == 0) are omitted — this is the actionable
    plan, not the attempt log (the audit sheets carry rejected attempts).
    """
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append(["TRANSFER PLAN"])
    ws.append(["For each transfer: specify batch, date or week#, from/to tanks, count, avg weight."])
    ws.append([])
    ws.append([
        "Week", "Batch", "From_Tank", "To_Tank",
        "Count (fish)", "Avg_Weight (kg)", "Grade", "CV (%)",
    ])

    rows: list[tuple] = []
    for ev in tranog_events:
        wk = iso_week_label(ev.event_date)
        for dest in ev.destinations:
            rows.append((
                ev.event_date, wk, ev.batch_id, "FW", dest.tank_id,
                dest.count, dest.avg_wt_g / 1000.0, "", dest.cv_pct,
            ))
    for ev in transfer_events:
        ct = getattr(ev, "count_transferred", None)
        if ct is not None and ct <= 0:
            continue  # rejected attempt — not part of the actionable plan
        wk = iso_week_label(ev.event_date)
        for dest in ev.destinations:
            rows.append((
                ev.event_date, wk, ev.batch_id, str(ev.source_tank_id), dest.tank_id,
                dest.count, dest.avg_wt_g / 1000.0, "", dest.cv_pct,
            ))
    for ev in (grade_events or []):
        wk = iso_week_label(ev.event_date)
        src_str = ",".join(str(t) for t in ev.source_tank_ids)
        for dest in ev.destinations:
            rows.append((
                ev.event_date, wk, ev.batch_id, src_str, dest.tank_id,
                dest.count, dest.avg_wt_g / 1000.0,
                getattr(dest, "grade", "") or "", dest.cv_pct,
            ))
    rows.sort(key=lambda r: (r[0], r[2]))

    for r in rows:
        ws.append([
            r[1], r[2], r[3], r[4],
            round(r[5], 0),
            round(r[6], 3),
            r[7],
            round(r[8], 1) if r[8] else None,
        ])
    widths = {1: 11, 2: 8, 3: 10, 4: 8, 5: 13, 6: 14, 7: 9, 8: 8}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w


def write_harvest_plan_report(
    wb,
    harvest_events,
    scenario_name: str,
    default_hog_yield: float,
    facility_limits_hog: dict,
    sheet_name: str = "HarvestPlan Report",
) -> None:
    """Annual per-batch harvest summary (matches reference format).

    One block per year. Each block: a "<scenario> <year>" title row, a month-
    header row (12 month-start columns + "TOTAL <year>"), then three rows per
    batch — Units, Av Weight - Kg HOG, Biomass - Tons HOG — with monthly values
    and a year total. Blank cells for months with no harvest.
    """
    from collections import defaultdict
    from datetime import date as _date

    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)

    # Aggregate per (year, batch, month) -> count + HOG biomass (kg).
    agg: dict[tuple, dict] = defaultdict(lambda: {"count": 0.0, "hog_kg": 0.0})
    years: set[int] = set()
    batches_by_year: dict[int, set] = defaultdict(set)
    for ev in harvest_events:
        d = ev.event_date.date() if hasattr(ev.event_date, "date") else ev.event_date
        hog_yield = facility_limits_hog.get(iso_week_label(ev.event_date), default_hog_yield)
        e = agg[(d.year, ev.batch_id, d.month)]
        e["count"] += ev.count
        e["hog_kg"] += ev.count * ev.avg_wt_g / 1000.0 * hog_yield
        years.add(d.year)
        batches_by_year[d.year].add(ev.batch_id)

    for year in sorted(years):
        ws.append([f"{scenario_name} {year}"])
        ws.append(["", ""] + [_date(year, m, 1) for m in range(1, 13)] + [f"TOTAL {year}"])
        for b in sorted(batches_by_year[year]):
            months = [agg.get((year, b, m)) for m in range(1, 13)]
            tot_count = sum(m["count"] for m in months if m)
            tot_hog = sum(m["hog_kg"] for m in months if m)
            units = [round(m["count"], 0) if m else "" for m in months]
            avgwt = [round(m["hog_kg"] / m["count"], 2) if m and m["count"] else "" for m in months]
            tons = [round(m["hog_kg"] / 1000.0, 0) if m else "" for m in months]
            ws.append([b, "Units"] + units + [round(tot_count, 0)])
            ws.append(["", "Av Weight - Kg HOG"] + avgwt
                      + [round(tot_hog / tot_count, 2) if tot_count else ""])
            ws.append(["", "Biomass - Tons HOG"] + tons + [round(tot_hog / 1000.0, 0)])
        ws.append([])

    ws.column_dimensions["A"].width = 8
    ws.column_dimensions["B"].width = 20
    for c in range(3, 16):
        ws.column_dimensions[get_column_letter(c)].width = 11


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
    forecast_start=None,
    sheet_name: str = "HarvestReport",
) -> None:
    """Per-event harvest forecast (one row per tank harvest), matches reference.

    Columns: Year, Month, Week, Date, Tank, Batch, Count (fish),
    Gross Biomass (kg), HOG Biomass (kg), Avg Live Wt (kg).
    """
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append(["HARVEST FORECAST"])
    fs = forecast_start.date() if hasattr(forecast_start, "date") else forecast_start
    ws.append([f"Generated from forecast starting {fs}" if fs else "Generated from forecast"])
    ws.append([])
    ws.append([
        "Year", "Month", "Week", "Date", "Tank", "Batch",
        "Count (fish)", "Gross Biomass (kg)", "HOG Biomass (kg)", "Avg Live Wt (kg)",
    ])

    def _d(ev_date):
        return ev_date.date() if hasattr(ev_date, "date") else ev_date

    evs = sorted(harvest_events, key=lambda e: (_d(e.event_date), e.source_tank_id))
    for ev in evs:
        d = _d(ev.event_date)
        wk = iso_week_label(ev.event_date)
        hog_yield = facility_limits_hog.get(wk, default_hog_yield)
        gross = ev.count * ev.avg_wt_g / 1000.0
        ws.append([
            d.year,
            d.replace(day=1),
            wk,
            d,
            ev.source_tank_id,
            ev.batch_id,
            round(ev.count, 0),
            round(gross, 0),
            round(gross * hog_yield, 0),
            round(ev.avg_wt_g / 1000.0, 2),
        ])
    widths = {1: 7, 2: 12, 3: 10, 4: 12, 5: 7, 6: 8, 7: 13, 8: 18, 9: 17, 10: 16}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w


def _feed_by_type_week(batch_locations, biology_states_by_batch, tables, batches=None):
    """(feed_name, week_label) -> kg/week, plus a week_label -> week_start map.

    OG/SW feed comes from REALIZED batch_locations via realized_feed_kg_day, so
    totals match the WeeklyReport Feed column and the Advisory feed/day. FW/EGG
    feed comes from the projection (FW fish live in FW tanks, which are absent
    from batch_locations). Phantom unharvested SW projection fish are excluded —
    they never appear in batch_locations — so later-year feed no longer balloons.
    """
    from collections import defaultdict
    from .biology import realized_feed_kg_day, _feed_type_for_size
    ftw: dict[tuple[str, str], float] = defaultdict(float)
    wk_start: dict[str, object] = {}
    for r in batch_locations:
        wk_start.setdefault(r.week_label, r.week_start)
        if tables is not None:
            b = (batches or {}).get(r.batch_id)
            fkg = realized_feed_kg_day(r.avg_wt_g, r.biomass_kg, b, tables) * 7.0
            if fkg:
                ftw[(_feed_type_for_size(tables, r.avg_wt_g), r.week_label)] += fkg
    for states in (biology_states_by_batch or {}).values():
        for s in states:
            if s.stage in ("FW", "EGG") and s.feed_kg_week:
                wk_start.setdefault(s.week_label, s.week_start)
                ftw[(s.feed_type, s.week_label)] += s.feed_kg_week
    return ftw, wk_start


def write_feed_forecast_weekly(
    wb,
    batch_locations,
    biology_states_by_batch,
    forecast_start,
    tables=None,
    batches=None,
    sheet_name: str = "FeedForecastWeekly",
) -> None:
    """Per-week feed forecast as a Feed Type x Week matrix (kg/week).

    Matches the reference report: rows are feed types (ordered by Max Size),
    columns are ISO weeks with a week-start date sub-row, cells are the weekly
    feed (kg) consumed by fish in that feed-type size band, plus a Total row.
    Feed source = realized OG/SW + projected FW (see _feed_by_type_week).
    """
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)

    ftw, wk_start = _feed_by_type_week(batch_locations, biology_states_by_batch, tables, batches)
    weeks = sorted(wk_start.keys())
    # Feed-type row order (by Max Size) from biology tables; fall back to the
    # feed_type values present if tables weren't passed.
    if tables is not None and getattr(tables, "feed_types", None):
        ftypes = sorted(tables.feed_types, key=lambda x: x[0])  # (max_size_g, name)
    else:
        ftypes = [(None, n) for n in sorted({ft for (ft, _) in ftw})]

    ws.append(["WEEKLY FEED FORECAST BY TYPE (kg)"])
    ws.append([])
    ws.append(["Feed Type", "Max Size (g)"] + weeks)
    ws.append(["", ""] + [wk_start[w] for w in weeks])
    totals = [0.0] * len(weeks)
    for max_size, name in ftypes:
        row = [name, max_size]
        for i, w in enumerate(weeks):
            v = ftw.get((name, w), 0.0)
            row.append(round(v, 0))
            totals[i] += v
        ws.append(row)
    ws.append(["Total (kg)", ""] + [round(t, 0) for t in totals])
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 12


def write_feed_forecast_monthly(
    wb,
    batch_locations,
    biology_states_by_batch,
    forecast_start,
    tables=None,
    batches=None,
    sheet_name: str = "FeedForecastMonthly",
) -> None:
    """Per-month feed forecast as a Feed Type x Month matrix (kg/month).

    Matches the reference: rows are feed types (by Max Size), columns are
    month-start dates, cells are monthly feed (kg) by feed-type band, plus a
    Grand Total row. Feed source = realized OG/SW + projected FW (rolled up
    from the same per-week aggregation as the weekly sheet).
    """
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)

    from collections import defaultdict
    from datetime import date as _date
    ftw, wk_start = _feed_by_type_week(batch_locations, biology_states_by_batch, tables, batches)
    ftm: dict[tuple[str, str], float] = defaultdict(float)  # (feed_type, month) -> kg
    months: set[str] = set()
    for (name, wk), v in ftw.items():
        ws_ = wk_start.get(wk)
        mo = (ws_.strftime("%Y-%m") if hasattr(ws_, "strftime") else str(ws_)[:7])
        months.add(mo)
        ftm[(name, mo)] += v
    months_sorted = sorted(months)
    mo_dates = [_date(int(m[:4]), int(m[5:7]), 1) for m in months_sorted]
    if tables is not None and getattr(tables, "feed_types", None):
        ftypes = sorted(tables.feed_types, key=lambda x: x[0])
    else:
        ftypes = [(None, n) for n in sorted({ft for (ft, _) in ftm})]

    ws.append(["MONTHLY FEED FORECAST BY TYPE (kg)"])
    ws.append([])
    ws.append(["Feed Type", "Max Size (g)"] + mo_dates)
    totals = [0.0] * len(months_sorted)
    for max_size, name in ftypes:
        row = [name, max_size]
        for i, m in enumerate(months_sorted):
            v = ftm.get((name, m), 0.0)
            row.append(round(v, 0))
            totals[i] += v
        ws.append(row)
    ws.append(["Grand Total", ""] + [round(t, 0) for t in totals])
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 12


# Open/Close ledger column headers (shared by Weekly + Monthly reports).
_LEDGER_COLS = [
    "Open_Count (fish)", "Open_AvgWt (g)", "Open_Bio (kg)",
    "Close_Count (fish)", "Close_AvgWt (g)", "Close_Bio (kg)",
    "Density (kg/m³)", "SGR (%/day)", "Gross_Growth (kg)",
    "Net_Production (kg)", "Feed (kg)", "SFR (%/day)",
    "Bio_FCR (ratio)", "Econ_FCR (ratio)",
    "Mort_Count (fish)", "Mort_Bio (kg)",
    "Harv_Count (fish)", "Harv_Gross (kg)", "Harv_HOG (kg)", "Harv_AvgWt_HOG (g)",
    "Cull_Count (fish)", "Cull_Bio (kg)",
    "Input_Count (fish)", "Xfer_In (fish)", "Xfer_Out (fish)",
    "Count_Check (fish)", "Bio_Check (kg)",
]


def _build_batch_week_ledger(
    batch_locations, harvest_events, batch_week_states,
    transfer_events=None, batches=None, tables=None, hog_yield=0.0,
):
    """Assemble a per-(batch, week) open/close production ledger.

    Open = prior week's realized close (chained); Close = this week's realized
    state (BatchLocations aggregate, falling back to the biology projection for
    pre-TranOG FW weeks with no tank rows). Growth/feed/FCR/mortality columns
    are derived to mirror the reference report definitions:
      Gross_Growth = (close_bio - open_bio) + mort_bio + harv_gross + cull_bio - input_bio
      Net_Production = Gross_Growth - mort_bio
      SGR = ln(close_wt/open_wt)/7*100 ; SFR = feed / avg_bio / 7 * 100
      Bio_FCR = feed / gross_growth ; Econ_FCR = feed / net_production
    Bio_Check is 0 by construction; Count_Check carries the small count residual.
    Returns a list of row dicts ordered by (batch, week).
    """
    from collections import defaultdict
    from math import log
    from .biology import realized_feed_kg_day

    # Realized close per (batch, week) from BatchLocations.
    rl: dict[tuple, dict] = defaultdict(
        lambda: {"count": 0.0, "bio": 0.0, "wt_sum": 0.0, "week_start": None})
    feed: dict[tuple, float] = defaultdict(float)
    for r in batch_locations:
        key = (r.batch_id, r.week_label)
        e = rl[key]
        e["count"] += r.count
        e["bio"] += r.biomass_kg
        e["wt_sum"] += r.avg_wt_g * r.count
        e["week_start"] = r.week_start
        if tables is not None:
            b = (batches or {}).get(r.batch_id)
            feed[key] += realized_feed_kg_day(r.avg_wt_g, r.biomass_kg, b, tables) * 7.0

    # Harvest per (batch, week).
    harv: dict[tuple, dict] = defaultdict(lambda: {"count": 0.0, "gross": 0.0, "wt_sum": 0.0})
    for ev in harvest_events:
        key = (ev.batch_id, iso_week_label(ev.event_date))
        e = harv[key]
        e["count"] += ev.count
        e["gross"] += ev.count * ev.avg_wt_g / 1000.0
        e["wt_sum"] += ev.avg_wt_g * ev.count

    # Transfers per (batch, week): intra-batch moves, so In == Out (net 0).
    xfer: dict[tuple, float] = defaultdict(float)
    for ev in (transfer_events or ()):
        moved = sum(getattr(d, "count", 0.0) for d in getattr(ev, "destinations", []))
        if not moved:
            moved = getattr(ev, "count_transferred", 0.0)
        xfer[(ev.batch_id, iso_week_label(ev.event_date))] += moved

    # Cull / mortality% / input / biology fallback, keyed by (batch, week).
    cull: dict[tuple, dict] = defaultdict(lambda: {"count": 0.0, "bio": 0.0})
    mortpct: dict[tuple, float] = {}
    inputc: dict[tuple, float] = defaultdict(float)
    bio_state: dict[tuple, object] = {}
    for s in batch_week_states or ():
        key = (s.batch_id, s.week_label)
        bio_state[key] = s
        mortpct[key] = s.mortality_pct_weekly
        if s.cull_count_week > 0:
            cull[key]["count"] += s.cull_count_week
            cull[key]["bio"] += s.cull_biomass_kg_week
        if s.week_from_input == 0:
            inputc[key] += s.count
        # FW (pre-TranOG) feed: FW fish live in FW tanks, which are NOT in
        # batch_locations (OG-only). Pull their feed from the projection so the
        # ledger + FeedForecast cover small-fish feed. SW/OG feed always comes
        # from realized batch_locations above — never the projection, which
        # would re-introduce phantom unharvested fish (see close_vals gate).
        if s.stage in ("FW", "EGG") and s.feed_kg_week and key not in rl:
            feed[key] += s.feed_kg_week

    def close_vals(key):
        e = rl.get(key)
        if e and e["count"] > 0:
            return e["count"], e["wt_sum"] / e["count"], e["bio"], e["week_start"]
        # Biology fallback ONLY for genuine pre-TranOG FW/EGG weeks. A projected
        # SW/STARVE week with no realized placement is a phantom (batch never
        # placed, or already harvested out) and must not contribute biomass —
        # else facility totals balloon far past the real ~4M kg.
        s = bio_state.get(key)
        if s and s.stage in ("FW", "EGG"):
            return s.count, s.avg_weight_g, s.biomass_kg, s.week_start
        return 0.0, 0.0, 0.0, None

    # All (batch, week) cells: union of realized + projected weeks.
    by_batch: dict[str, set] = defaultdict(set)
    for (b, wk) in set(rl) | set(bio_state) | set(harv) | set(cull):
        by_batch[b].add(wk)

    rows = []
    for b in sorted(by_batch):
        weeks = sorted(by_batch[b])
        for i, wk in enumerate(weeks):
            cc, cwt, cbio, ws_date = close_vals((b, wk))
            if i == 0:
                s0 = bio_state.get((b, wk))
                oc, owt, obio = ((s0.count, s0.avg_weight_g, s0.biomass_kg)
                                 if s0 else (cc, cwt, cbio))
            else:
                oc, owt, obio, _ = close_vals((b, weeks[i - 1]))
            h = harv.get((b, wk), {"count": 0.0, "gross": 0.0, "wt_sum": 0.0})
            cu = cull.get((b, wk), {"count": 0.0, "bio": 0.0})
            mort_count = oc * mortpct.get((b, wk), 0.0) / 100.0
            mort_bio = mort_count * owt / 1000.0
            input_count = inputc.get((b, wk), 0.0)
            input_bio = input_count * owt / 1000.0
            xf = xfer.get((b, wk), 0.0)
            harv_gross = h["gross"]
            harv_hog = harv_gross * hog_yield
            harv_avg_hog = ((h["wt_sum"] / h["count"]) * hog_yield) if h["count"] > 0 else 0.0
            gross_growth = (cbio - obio) + mort_bio + harv_gross + cu["bio"] - input_bio
            net_prod = gross_growth - mort_bio
            f = feed.get((b, wk), 0.0)
            avg_bio = (obio + cbio) / 2.0
            sgr = (log(cwt / owt) / 7.0 * 100.0) if owt > 0 and cwt > 0 else 0.0
            sfr = (f / avg_bio / 7.0 * 100.0) if avg_bio > 0 else 0.0
            bio_fcr = (f / gross_growth) if gross_growth > 0 else 0.0
            econ_fcr = (f / net_prod) if net_prod > 0 else 0.0
            count_check = (oc - mort_count - h["count"] - cu["count"]
                           + input_count + xf - xf - cc)
            # Bio_Check is 0 by construction (gross_growth balances the ledger).
            bio_check = (obio + gross_growth - mort_bio - harv_gross - cu["bio"]
                         + input_bio - cbio)
            if oc <= 0 and cc <= 0 and h["count"] <= 0 and cu["count"] <= 0:
                continue
            rows.append({
                "batch": b, "week": wk, "week_start": ws_date,
                "open_count": oc, "open_wt": owt, "open_bio": obio,
                "close_count": cc, "close_wt": cwt, "close_bio": cbio,
                "sgr": sgr, "gross_growth": gross_growth, "net_prod": net_prod,
                "feed": f, "sfr": sfr, "bio_fcr": bio_fcr, "econ_fcr": econ_fcr,
                "mort_count": mort_count, "mort_bio": mort_bio,
                "harv_count": h["count"], "harv_gross": harv_gross,
                "harv_hog": harv_hog, "harv_avg_hog": harv_avg_hog,
                "cull_count": cu["count"], "cull_bio": cu["bio"],
                "input_count": input_count, "xfer_in": xf, "xfer_out": xf,
                "count_check": count_check, "bio_check": bio_check,
            })
    return rows


def _ledger_value_cells(d: dict) -> list:
    """Format the 27 shared open/close ledger value columns from a row dict."""
    return [
        round(d["open_count"], 0), round(d["open_wt"], 1), round(d["open_bio"], 0),
        round(d["close_count"], 0), round(d["close_wt"], 1), round(d["close_bio"], 0),
        0, round(d["sgr"], 4), round(d["gross_growth"], 0),
        round(d["net_prod"], 0), round(d["feed"], 0), round(d["sfr"], 4),
        round(d["bio_fcr"], 2), round(d["econ_fcr"], 2),
        round(d["mort_count"], 0), round(d["mort_bio"], 1),
        round(d["harv_count"], 0), round(d["harv_gross"], 1),
        round(d["harv_hog"], 1), round(d["harv_avg_hog"], 1),
        round(d["cull_count"], 0), round(d["cull_bio"], 1),
        round(d["input_count"], 0), round(d["xfer_in"], 0), round(d["xfer_out"], 0),
        round(d["count_check"], 0), round(d["bio_check"], 0),
    ]


def write_weekly_report(
    wb,
    batch_locations,
    harvest_events,
    batch_week_states=None,
    transfer_events=None,
    batches=None,
    tables=None,
    scenario_name: str = "",
    hog_yield: float = 0.0,
    sheet_name: str = "WeeklyReport",
) -> None:
    """Per-(week, batch) open/close production ledger (matches reference format).

    Columns: Scenario, Week, Week_Start, Batch, then the 27 shared open/close
    ledger columns (open/close count-avgwt-bio, density, SGR, growth, feed, FCR,
    mortality, harvest, cull, transfers, and reconciliation checks).
    """
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append([f"{sheet_name} - populated by RunForecast"])
    ws.append([])
    ws.append([])
    ws.append(["Scenario", "Week", "Week_Start", "Batch"] + _LEDGER_COLS)

    rows = _build_batch_week_ledger(
        batch_locations, harvest_events, batch_week_states,
        transfer_events, batches, tables, hog_yield)
    for d in rows:
        ws.append([scenario_name, d["week"], d["week_start"], d["batch"]]
                  + _ledger_value_cells(d))
    for c in range(1, 5 + len(_LEDGER_COLS)):
        ws.column_dimensions[get_column_letter(c)].width = 14


def write_monthly_report(
    wb,
    batch_locations,
    harvest_events,
    batch_week_states=None,
    transfer_events=None,
    batches=None,
    tables=None,
    scenario_name: str = "",
    hog_yield: float = 0.0,
    sheet_name: str = "MonthlyReport",
) -> None:
    """Per-(month, batch) open/close production ledger (matches reference format).

    Rolls the weekly ledger up to months: Open = first week's open in the month,
    Close = last week's close; flows (growth, feed, mortality, harvest, cull,
    transfers, input) are summed; SGR/SFR/FCR are recomputed from the monthly
    aggregates. Columns mirror the weekly report minus Week_Start.
    """
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append([f"{sheet_name} - populated by RunForecast"])
    ws.append([])
    ws.append([])
    ws.append(["Scenario", "Month", "Batch"] + _LEDGER_COLS)

    from collections import defaultdict
    from math import log

    weekly = _build_batch_week_ledger(
        batch_locations, harvest_events, batch_week_states,
        transfer_events, batches, tables, hog_yield)

    # Group weekly rows into (batch, month); preserve week order for open/close.
    from datetime import date as _date
    def _month_key(d):
        ws_d = d["week_start"]
        if hasattr(ws_d, "strftime"):
            return ws_d.strftime("%Y-%m")
        # Fallback: derive the month from the ISO week label (NOT a raw slice —
        # d["week"][6:8] is the week NUMBER, which produced bogus "2027-20" keys).
        try:
            yy, wwk = int(d["week"][:4]), int(d["week"][6:8])
            return _date.fromisocalendar(yy, wwk, 1).strftime("%Y-%m")
        except Exception:
            return str(ws_d)[:7]
    grouped: dict[tuple, list] = defaultdict(list)
    for d in weekly:
        grouped[(_month_key(d), d["batch"])].append(d)

    for (mo, b) in sorted(grouped):
        wks = sorted(grouped[(mo, b)], key=lambda x: x["week"])
        first, last = wks[0], wks[-1]
        def s(key):
            return sum(w[key] for w in wks)
        gross_growth = s("gross_growth")
        net_prod = s("net_prod")
        f = s("feed")
        open_bio, close_bio = first["open_bio"], last["close_bio"]
        open_wt, close_wt = first["open_wt"], last["close_wt"]
        avg_bio = (open_bio + close_bio) / 2.0
        days = 7.0 * len(wks)
        sgr = (log(close_wt / open_wt) / days * 100.0) if open_wt > 0 and close_wt > 0 else 0.0
        sfr = (f / avg_bio / days * 100.0) if avg_bio > 0 else 0.0
        harv_count = s("harv_count")
        harv_gross = s("harv_gross")
        agg = {
            "batch": b, "week": mo, "week_start": None,
            "open_count": first["open_count"], "open_wt": open_wt, "open_bio": open_bio,
            "close_count": last["close_count"], "close_wt": close_wt, "close_bio": close_bio,
            "sgr": sgr, "gross_growth": gross_growth, "net_prod": net_prod,
            "feed": f, "sfr": sfr,
            "bio_fcr": (f / gross_growth) if gross_growth > 0 else 0.0,
            "econ_fcr": (f / net_prod) if net_prod > 0 else 0.0,
            "mort_count": s("mort_count"), "mort_bio": s("mort_bio"),
            "harv_count": harv_count, "harv_gross": harv_gross,
            "harv_hog": s("harv_hog"),
            "harv_avg_hog": (s("harv_hog") * 1000.0 / harv_count) if harv_count > 0 else 0.0,
            "cull_count": s("cull_count"), "cull_bio": s("cull_bio"),
            "input_count": s("input_count"), "xfer_in": s("xfer_in"), "xfer_out": s("xfer_out"),
            "count_check": s("count_check"), "bio_check": s("bio_check"),
        }
        ws.append([scenario_name, mo, b] + _ledger_value_cells(agg))
    for c in range(1, 4 + len(_LEDGER_COLS)):
        ws.column_dimensions[get_column_letter(c)].width = 14


def write_input_conservation_audit(
    wb,
    batches,
    batch_locations,
    harvest_events,
    control,
    sheet_name: str = "InputConservationAudit",
) -> None:
    """Input-fish conservation: every stocked batch must have a realized fate.

    The TankContinuityAudit proves 0 drift for fish that ARE in tanks, but it is
    BLIND to a batch that is never placed — a dropped TranOG arrival creates no
    tank-week row, so it never unbalances per-tank continuity. This audit closes
    that gap: every batch whose TranOG falls within the forecast horizon MUST
    appear in the realized placement (BatchLocations). Any in-horizon batch with
    no placement is DROPPED — its stocked fish vanished from the plan. The
    Fish_At_Risk total is the count of silently-lost fish; it must be 0.
    """
    from datetime import timedelta
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)

    fs = control.forecast_start
    fs = fs.date() if hasattr(fs, "date") else fs
    horizon_end = fs + timedelta(weeks=control.horizon_weeks)

    placed_first = {}
    placed_last = {}
    standing = {}
    last_wk = max((r.week_label for r in batch_locations), default=None)
    for r in batch_locations:
        b = r.batch_id
        if b not in placed_first or r.week_label < placed_first[b]:
            placed_first[b] = r.week_label
        if b not in placed_last or r.week_label > placed_last[b]:
            placed_last[b] = r.week_label
        if r.week_label == last_wk:
            standing[b] = standing.get(b, 0.0) + r.count
    harv = {}
    for ev in harvest_events:
        harv[ev.batch_id] = harv.get(ev.batch_id, 0.0) + ev.count

    dropped_fish = 0.0
    dropped_batches = 0
    in_horizon_input = 0.0
    rowbuf = []
    for bt in sorted(batches, key=lambda x: x.batch_id):
        bid = bt.batch_id
        tog = bt.tran_og_date
        togd = (tog.date() if hasattr(tog, "date") else tog) if tog else None
        is_placed = bid in placed_first
        hv = harv.get(bid, 0.0)
        in_h = togd is not None and fs <= togd <= horizon_end
        if is_placed or hv > 0:
            status = "PLACED"
        elif togd is None:
            status = "FW-only (no TranOG)"
        elif togd > horizon_end:
            status = "future (TranOG beyond horizon)"
        elif togd < fs:
            status = "pre-start"
        else:
            status = "*** DROPPED ***"
        at_risk = 0.0
        if in_h:
            in_horizon_input += bt.input_count or 0
        if status == "*** DROPPED ***":
            dropped_batches += 1
            at_risk = bt.input_count or 0.0
            dropped_fish += at_risk
        rowbuf.append([
            bid, round(bt.input_count or 0, 0),
            togd, "Y" if in_h else "N",
            "Y" if is_placed else "N",
            round(hv, 0) if hv else 0,
            round(standing.get(bid, 0.0), 0),
            status, round(at_risk, 0) if at_risk else "",
        ])

    pct = (100.0 * dropped_fish / in_horizon_input) if in_horizon_input > 0 else 0.0
    ws.append(["INPUT-FISH CONSERVATION AUDIT"])
    ws.append([f"Generated: {datetime.now().isoformat(timespec='seconds')}"])
    if dropped_fish > 0:
        ws.append([f"*** {dropped_batches} batch(es) DROPPED — {dropped_fish:,.0f} stocked "
                   f"fish ({pct:.1f}% of in-horizon input) never placed. NOT caught by "
                   f"TankContinuityAudit (a never-placed batch has no tank rows). ***"])
    else:
        ws.append(["OK — every in-horizon batch reached the realized facility (0 dropped fish)."])
    ws.append([
        "Batch", "Input_Count (fish)", "TranOG_Date", "In_Horizon",
        "Placed", "Harvested (fish)", "Standing@Horizon (fish)",
        "Status", "Fish_At_Risk (fish)",
    ])
    for r in rowbuf:
        ws.append(r)
    widths = {1: 8, 2: 17, 3: 13, 4: 11, 5: 8, 6: 16, 7: 20, 8: 28, 9: 18}
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
    # STARVE (6N production in-place purge) tanks neither grow nor take mortality;
    # track their count/biomass per (batch, week) so the reconciliation excludes
    # them from the growth + mortality expectation (else they read as drift).
    starve_count: dict[tuple[str, str], float] = defaultdict(float)
    starve_biomass: dict[tuple[str, str], float] = defaultdict(float)
    weeks_seen: set[str] = set()
    week_start_by_label: dict[str, object] = {}
    for r in batch_locations:
        loc_count[(r.batch_id, r.week_label)] += r.count
        loc_biomass[(r.batch_id, r.week_label)] += r.biomass_kg
        if getattr(r, "stage", "") == "STARVE":
            starve_count[(r.batch_id, r.week_label)] += r.count
            starve_biomass[(r.batch_id, r.week_label)] += r.biomass_kg
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
            # STARVE fish this week neither grow nor take mortality.
            st_b = starve_biomass.get((batch, wk), 0.0)
            st_c = starve_count.get((batch, wk), 0.0)
            mort = max(0.0, prev_count - st_c) * (m_pct / 100.0)
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
            # Only the NON-starve biomass grows / takes mortality.
            grow_bio = max(0.0, bio_full_growth - st_b)
            growth_full = grow_bio * (growth_factor - 1.0)
            growth_tnin = in_b * (partial_factor - 1.0)
            growth_kg = growth_full + growth_tnin
            mort_kg = grow_bio * (m_pct / 100.0)
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
    # STARVE (in-place purge) tank-weeks: no growth, no mortality.
    tank_wk_starve: dict[tuple[int, str], bool] = {}
    for r in batch_locations:
        tank_wk_bio[(r.tank_id, r.week_label)] = r.biomass_kg
        if getattr(r, "stage", "") == "STARVE":
            tank_wk_starve[(r.tank_id, r.week_label)] = True

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
            # STARVE (in-place purge) this week: no growth, no mortality.
            is_starve = tank_wk_starve.get((tid, wk), False)

            # ---- Count balance ----
            m_pct = mort_pct.get((prev_batch, wk), 0.0) if prev_batch else 0.0
            mort = 0.0 if is_starve else prev_count * (m_pct / 100.0)
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
            growth_full = 0.0 if is_starve else bio_full_growth * (growth_factor - 1.0)
            growth_tnin = tn_in_kg * (partial_factor - 1.0)
            growth_kg = growth_full + growth_tnin
            mort_kg = 0.0 if is_starve else bio_full_growth * (m_pct / 100.0)
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
    ws.append(["Each cell: Batch# AvgWt(kg) / Density(kg/m³). Color = batch."])

    # Order tanks by system + tank_id (OG tanks only for compactness).
    og_tanks = sorted(
        [t for t in facility.tanks if t.type == "OG"],
        key=lambda t: (t.system_id, t.tank_id),
    )
    # Weeks in chronological order + their start dates.
    weeks = sorted({r.week_label for r in batch_locations})
    wk_start: dict[str, object] = {}
    for r in batch_locations:
        wk_start.setdefault(r.week_label, r.week_start)

    # Build occupancy map (tank, week) → (batch_id, avg_wt_g, density).
    occ: dict[tuple[int, str], tuple] = {}
    for r in batch_locations:
        occ[(r.tank_id, r.week_label)] = (r.batch_id, r.avg_wt_g, r.density_kg_m3)

    # Two-row header: week labels then week-start dates.
    ws.append(["Week", ""] + weeks)
    ws.append(["Tank", "Sys"] + [wk_start.get(w) for w in weeks])

    for t in og_tanks:
        sys = t.system_id[2:] if t.system_id.startswith("OG") else t.system_id
        row = [t.tank_id, sys]
        for wk in weeks:
            cell = occ.get((t.tank_id, wk))
            if cell:
                bid, wt_g, dens = cell
                bnum = bid[1:] if bid and bid[:1] == "B" else bid
                row.append(f"{bnum} {wt_g / 1000.0:.1f}/{dens:.0f}")
            else:
                row.append("")
        ws.append(row)
    ws.column_dimensions[get_column_letter(1)].width = 6
    ws.column_dimensions[get_column_letter(2)].width = 6
    for c in range(3, 3 + len(weeks)):
        ws.column_dimensions[get_column_letter(c)].width = 12


def write_advisory(
    wb,
    batch_locations,
    harvest_events,
    facility_limits,
    control,
    batches=None,
    tables=None,
    sheet_name: str = "Advisory",
) -> None:
    """Per-week capacity advisory + harvest recommendations (matches reference).

    Caps summary header, then one row per week: facility biomass + feed vs their
    (per-week resolved) limits with excess, that week's harvest, and an advisory
    flag (OK / REDUCE ...). Realized feed (kg/day) via realized_feed_kg_day so
    the totals match what fish in the tanks actually eat.
    """
    from collections import defaultdict
    from .caps import resolve_facility_cap, METRIC_BIOMASS, METRIC_FEED_DAY
    from .biology import realized_feed_kg_day

    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append(["CAPACITY ADVISORY - HARVEST RECOMMENDATIONS"])
    ws.append(["Max Feed/Day:", f"{control.max_feed_per_day_kg:,.0f} kg/day"])
    ws.append(["Max Facility Biomass:", f"{control.max_biomass_kg:,.0f} kg"])
    ws.append([])
    ws.append([
        "Week", "Week_Start", "Total_Biomass (kg)", "Biomass_Limit (kg)",
        "Biomass_Excess (kg)", "Total_Feed (kg/day)", "Feed_Limit (kg/day)",
        "Feed_Excess (kg/day)", "Harvest_Count", "Harvest_Biomass (kg)",
        "Advisory", "Harvest_Batch", "Harvest_Recommended (kg)",
    ])

    bio: dict[str, float] = defaultdict(float)
    feed: dict[str, float] = defaultdict(float)
    wk_start: dict[str, object] = {}
    for r in batch_locations:
        bio[r.week_label] += r.biomass_kg
        wk_start.setdefault(r.week_label, r.week_start)
        if tables is not None:
            b = (batches or {}).get(r.batch_id)
            feed[r.week_label] += realized_feed_kg_day(r.avg_wt_g, r.biomass_kg, b, tables)

    harv_c: dict[str, float] = defaultdict(float)
    harv_b: dict[str, float] = defaultdict(float)
    for ev in harvest_events:
        wk = iso_week_label(ev.event_date)
        harv_c[wk] += ev.count
        harv_b[wk] += ev.count * ev.avg_wt_g / 1000.0

    for wk in sorted(set(bio) | set(feed) | set(harv_c)):
        tb = bio.get(wk, 0.0)
        tf = feed.get(wk, 0.0)
        bcap = resolve_facility_cap(METRIC_BIOMASS, wk, facility_limits, control) or 0.0
        fcap = resolve_facility_cap(METRIC_FEED_DAY, wk, facility_limits, control) or 0.0
        bex = max(0.0, tb - bcap)
        fex = max(0.0, tf - fcap)
        if bex > 0 and fex > 0:
            adv = "REDUCE BIOMASS + FEED"
        elif bex > 0:
            adv = "REDUCE BIOMASS"
        elif fex > 0:
            adv = "REDUCE FEED"
        else:
            adv = "OK"
        ws.append([
            wk, wk_start.get(wk),
            round(tb, 0), round(bcap, 0), round(bex, 0),
            round(tf, 0), round(fcap, 0), round(fex, 0),
            round(harv_c.get(wk, 0.0), 0), round(harv_b.get(wk, 0.0), 0),
            adv, "", round(bex, 0) if bex > 0 else "",
        ])
    widths = {1: 10, 2: 12, 3: 18, 4: 18, 5: 18, 6: 18, 7: 18, 8: 18,
              9: 14, 10: 18, 11: 22, 12: 14, 13: 22}
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
    """Numbered validation issue stream (matches reference format).

    Title rows + a "# | Category | Detail" table, one row per
    warning/diagnostic from every layer.
    """
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)

    entries: list[tuple[str, str]] = []
    for r in residuals or ():
        if abs(r.residual_pct) >= 0.5:
            sug = (f"; suggested FW_Correction={r.suggested_fw_correction:.3f}"
                   if r.suggested_fw_correction is not None else "")
            entries.append((
                "WARNING - FW Calibration",
                f"Batch {r.batch_id} TranOG {r.tran_og_date.date()}: "
                f"residual {r.residual_pct:+.2f}%{sug}"))
    for b in bottlenecks or ():
        entries.append((f"WARNING - Bottleneck/{b.kind}", f"{b.week_label}: {b.detail}"))
    for w in scheduler_warnings or ():
        entries.append(("WARNING - Harvest Scheduler", w))
    for w in invariant_warnings or ():
        if "INV-1" in w:
            cat = "WARNING - INV-1 (one-batch-per-tank)"
        elif "INV-5" in w:
            cat = "WARNING - INV-5 (min_tank_control)"
        elif "Density" in w:
            cat = "WARNING - Hydration Density"
        else:
            cat = "WARNING - Hydration"
        entries.append((cat, w))
    for v in density_violations or ():
        wk, loc, bid, d, cap = v
        entries.append((
            "WARNING - Density",
            f"{wk}: {loc} (batch {bid}) at {d:.1f} kg/m³ > cap {cap:.1f}"))
    for w in placement_warnings or ():
        if "INV-4" in w:
            cat = "WARNING - INV-4 (1 kg rule)"
        elif "INV-5" in w:
            cat = "WARNING - INV-5 (min_tank_control)"
        elif "INV-1" in w:
            cat = "WARNING - INV-1 (one-batch-per-tank)"
        elif "TranOG" in w:
            cat = "WARNING - TranOG Entry"
        elif "6N" in w or "purge" in w.lower():
            cat = "WARNING - 6N Pipeline"
        elif w.startswith("[B]"):
            cat = "WARNING - Placement/Phase B"
        elif w.startswith("[C]"):
            cat = "WARNING - Placement/Phase C"
        elif w.startswith("[D]"):
            cat = "WARNING - Placement/Phase D"
        else:
            cat = "WARNING - Placement"
        entries.append((cat, w))

    ws.append(["VALIDATION LOG"])
    ws.append([f"Generated: {datetime.now().isoformat(timespec='seconds')}"])
    ws.append([f"{len(entries)} issue(s) found - review below" if entries
               else "No issues found"])
    ws.append(["#", "Category", "Detail"])
    for i, (cat, detail) in enumerate(entries, 1):
        ws.append([i, cat, detail])

    widths = {1: 6, 2: 34, 3: 120}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = "A5"


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
        # STARVE = in-place purge: biomass counts to the system, but no feed.
        if getattr(r, "stage", "") != "STARVE":
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
