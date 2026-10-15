"""The "12 weeks rolling harvest plan" table (REPORT ONLY, operator 2026-10-15).

The operator pastes a weekly harvest plan into a working Excel document whose
columns are fixed (A notes | B Source Tank | C Year | D Harvest ID | E Week |
F Date | G Tank | H Batch | I Count | J kg HOG | K avg kg HOG | L live kg |
M Origin FT weight | N lb HOG | O avg lb HOG). This module builds that table
ONCE, as plain rows, for both surfaces: the workbook's RollingHarvestPlan sheet
(excel_io.write_rolling_harvest_plan, which turns K/L/N/O into formulas) and the
app's Harvest tab (computed values + a TSV a paste lands in A..O).

Per harvest week, one block: a row per harvest day (time_grid.
harvest_schedule_days -- Mon-Fri, none before the report opens), a Total row
and a check row. The week's whole fish are split evenly over its days (whole
fish, remainder to the earliest days -- excel_io.whole_parts, the evenness
harvest_month_split assumes) and the days are filled tank after tank, the way
the site harvests: a day that finishes one tank and starts the next is ONE row
listing both tanks and batches. Nothing here changes a plan number; the
inputs are the whole fish and HOG kg every other harvest sheet already shows.

Inputs are plain dicts so the app (which reads the workbook) and the writer
(which holds the engine's events) feed the same function:

harvests:  {"date": date, "tank": int, "batch": str, "count": whole fish,
            "hog_kg": kg HOG, "hog_yield": ratio}
transfers: {"week": "YYYY-Www", "batch": str, "from_tank": int,
            "to_tank": int, "count": fish}
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from .batch_order import sorted_batches
from .sixn import SIXN_ALL_TANKS
from .time_grid import harvest_schedule_days

LB_PER_KG = 2.2046
COLUMNS = ["", "Source Tank", "Year ", "Harvest ID", "Week", "Date", "Tank ",
           "Batch", "Count", "Weight (kg HOG)", "Average weight (kg HOG)",
           "Live weight (kg)", "Origin FT Weight (kg)", "Weight (lb HOG)",
           "Average weight (lb HOG)"]
# How far back a 6N fill is looked for when the tank has no earlier harvest.
_FILL_LOOKBACK_WEEKS = 5


def _d(x) -> date:
    return x.date() if isinstance(x, datetime) else x


def _week_label(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _whole_parts(values) -> list[float]:
    from .excel_io import whole_parts
    return whole_parts(values)


def _tank_text(tanks: list) -> object:
    """One tank -> the int (61); several -> '61,67'."""
    if len(tanks) == 1:
        return tanks[0]
    return ",".join(str(t) for t in tanks)


def _uniq(seq) -> list:
    out = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


def source_tank_text(week_harvests: list[dict], all_harvests: list[dict],
                     transfers: list[dict]) -> str:
    """'41(77%)/33(23%)': the grow-out tanks that filled this week's 6N tanks.

    For each harvested (6N tank, batch), the fill is every transfer of that
    batch INTO that tank from the week of the tank's previous harvest (a 6N
    tank is emptied and refilled in the same week) up to, not including, this
    harvest week (6N fish are harvested two weeks after they move
    in; matching on 'since the tank last emptied' does not hard-code the
    lag). Sources that are themselves 6N tanks and graded-harvest retention
    legs (source == destination) are not grow-out sources. A fill that
    happened before the forecast has no transfer event: blank."""
    by_src: dict = {}
    for h in week_harvests:
        tank, batch = h["tank"], h["batch"]
        hw = _week_label(_d(h["date"]))
        prev = [_week_label(_d(x["date"])) for x in all_harvests
                if x["tank"] == tank and _week_label(_d(x["date"])) < hw]
        if prev:
            lo = max(prev)
        else:
            lo = _week_label(_d(h["date"]) - timedelta(weeks=_FILL_LOOKBACK_WEEKS))
        for t in transfers:
            if t["to_tank"] != tank or t["batch"] != batch:
                continue
            if not (lo <= t["week"] < hw):
                continue
            src = t["from_tank"]
            if src == t["to_tank"] or src in SIXN_ALL_TANKS:
                continue
            by_src[src] = by_src.get(src, 0.0) + float(t["count"] or 0.0)
    by_src = {k: v for k, v in by_src.items() if v > 0}
    if not by_src:
        return ""
    if len(by_src) == 1:
        return str(next(iter(by_src)))
    tot = sum(by_src.values())
    order = sorted(by_src, key=lambda k: (-by_src[k], k))
    return "/".join(f"{k}({round(100.0 * by_src[k] / tot):.0f}%)" for k in order)


def build_rolling_rows(harvests: Iterable[dict], transfers: Iterable[dict] = (),
                       report_start=None, first_id: int = 1,
                       weeks: int = 12) -> list[dict]:
    """The RollingHarvestPlan rows, block after block (no blank rows).

    Each row is a dict: kind ('day' | 'total' | 'check'), week_label, yield,
    block_top (index of the block's first day row), and the A..O values with
    K/L/N/O computed: source, year, harvest_id, week, date, tank, batch,
    count, kg_hog, avg_kg_hog, live_kg, lb_hog, avg_lb_hog."""
    hs = [dict(h, date=_d(h["date"])) for h in harvests]
    trs = list(transfers)
    if not hs:
        return []
    rs = _d(report_start) if report_start is not None else None
    first_week = _week_label(rs) if rs is not None else min(
        _week_label(h["date"]) for h in hs)
    by_week: dict = {}
    for h in hs:
        by_week.setdefault(_week_label(h["date"]), []).append(h)
    labels = sorted(w for w in by_week if w >= first_week)
    if weeks and weeks > 0:
        y, w = int(first_week[:4]), int(first_week[6:8])
        last = _week_label(date.fromisocalendar(y, w, 1)
                           + timedelta(weeks=weeks - 1))
        labels = [w for w in labels if w <= last]

    rows: list[dict] = []
    n_id = int(first_id)
    for wk in labels:
        evs = sorted(by_week[wk], key=lambda h: (h["date"], h["tank"]))
        days = harvest_schedule_days(evs[0]["date"], rs)
        yld = evs[0].get("hog_yield") or 0.81
        total_fish = sum(float(h["count"] or 0) for h in evs)
        quotas = _whole_parts([total_fish / len(days)] * len(days))
        # Walk the tanks: (remaining whole fish, kg per fish, tank, batch).
        queue = []
        loose_kg = 0.0
        for h in evs:
            c = float(h["count"] or 0)
            if c > 0:
                queue.append([c, float(h["hog_kg"] or 0) / c, h["tank"], h["batch"]])
            else:
                loose_kg += float(h["hog_kg"] or 0)
        src = source_tank_text(evs, hs, trs)
        top = len(rows)
        qi = 0
        for di, d in enumerate(days):
            need = quotas[di]
            taken_fish, taken_kg, tanks, bats = 0.0, 0.0, [], []
            while need > 0 and qi < len(queue):
                piece = queue[qi]
                take = min(need, piece[0])
                taken_fish += take
                taken_kg += take * piece[1]
                tanks.append(piece[2])
                bats.append(piece[3])
                piece[0] -= take
                need -= take
                if piece[0] <= 0:
                    qi += 1
            if di == 0:
                taken_kg += loose_kg     # kg of a 0-whole-fish event, once
            tanks, bats = _uniq(tanks), _uniq(bats)
            g = _tank_text(tanks) if tanks else ""
            hb = ",".join(bats)
            avg = taken_kg / taken_fish if taken_fish else 0.0
            iy, iw, _ = d.isocalendar()
            rows.append({
                "kind": "day", "week_label": wk, "yield": yld, "block_top": top,
                "source": src if di == 0 else "",
                "year": d.year,
                "harvest_id": f"{hb}T{g}-{n_id}" if tanks else "",
                "week": iw, "date": d, "tank": g, "batch": hb,
                "count": taken_fish, "kg_hog": taken_kg,
                "avg_kg_hog": avg, "live_kg": avg / yld,
                "lb_hog": taken_kg * LB_PER_KG, "avg_lb_hog": avg * LB_PER_KG,
            })
            if tanks:
                n_id += 1
        day_rows = rows[top:]
        t_fish = sum(r["count"] for r in day_rows)
        t_kg = sum(r["kg_hog"] for r in day_rows)
        t_avg = t_kg / t_fish if t_fish else 0.0
        iy, iw, _ = days[0].isocalendar()
        w_tanks = sorted({h["tank"] for h in evs})
        w_bats = sorted_batches({h["batch"] for h in evs})
        rows.append({
            "kind": "total", "week_label": wk, "yield": yld, "block_top": top,
            "source": "", "year": iy, "harvest_id": "", "week": iw,
            "date": f"{iw} Total",
            "tank": ", ".join(str(t) for t in w_tanks),
            "batch": ", ".join(w_bats),
            "count": t_fish, "kg_hog": t_kg, "avg_kg_hog": t_avg,
            "live_kg": t_avg / yld, "lb_hog": t_kg * LB_PER_KG,
            "avg_lb_hog": t_avg * LB_PER_KG,
        })
        o_t = t_avg * LB_PER_KG
        rows.append({
            "kind": "check", "week_label": wk, "yield": yld, "block_top": top,
            "source": "", "year": "", "harvest_id": "", "week": "", "date": "",
            "tank": "", "batch": "", "count": t_fish, "kg_hog": "",
            "avg_kg_hog": o_t / LB_PER_KG, "live_kg": o_t / LB_PER_KG / yld,
            "lb_hog": o_t * t_fish, "avg_lb_hog": o_t,
        })
    return rows


def rows_as_table(rows: list[dict]) -> list[list]:
    """Rows as A..O value lists (A and M blank) -- what a paste must land on."""
    out = []
    for r in rows:
        out.append(["", r["source"], r["year"], r["harvest_id"], r["week"],
                    r["date"], r["tank"], r["batch"], r["count"], r["kg_hog"],
                    r["avg_kg_hog"], r["live_kg"], "", r["lb_hog"],
                    r["avg_lb_hog"]])
    return out


def week_totals_lb(rows: list[dict]) -> dict:
    """{week label: lb HOG} from the Total rows."""
    return {r["week_label"]: r["lb_hog"] for r in rows if r["kind"] == "total"}


# ---- inputs from the engine's events (the workbook writer) -----------------

def harvest_inputs_from_events(harvest_events, default_hog_yield: float,
                               facility_limits_hog: dict) -> list[dict]:
    """Harvest dicts from engine Harvest events: whole fish as every harvest
    sheet shows them (excel_io.whole_harvest_counts) and HOG kg as HarvestPlan
    computes it (fractional fish x live avg x the week's yield)."""
    from .excel_io import whole_harvest_counts
    shown = whole_harvest_counts(harvest_events)
    out = []
    for ev in harvest_events:
        d = _d(ev.event_date)
        y = facility_limits_hog.get(_week_label(d), default_hog_yield)
        out.append({
            "date": d, "tank": ev.source_tank_id, "batch": ev.batch_id,
            "count": shown.get(id(ev), round(ev.count, 0)),
            "hog_kg": ev.count * ev.avg_wt_g / 1000.0 * y,
            "hog_yield": y,
        })
    return out


def transfer_inputs_from_events(transfer_events, grade_events=()) -> list[dict]:
    """Transfer dicts (one per destination leg) from engine events: Transfer
    legs (rejected attempts dropped), a graded harvest's pickup leg, and
    single-source Grade legs."""
    out = []
    for ev in transfer_events or []:
        wk = _week_label(_d(ev.event_date))
        if hasattr(ev, "pickup_tank_id"):
            out.append({"week": wk, "batch": ev.batch_id,
                        "from_tank": ev.source_tank_id,
                        "to_tank": ev.pickup_tank_id, "count": ev.pickup_count})
            continue
        ct = getattr(ev, "count_transferred", None)
        if ct is not None and ct <= 0:
            continue
        for dst in ev.destinations:
            out.append({"week": wk, "batch": ev.batch_id,
                        "from_tank": ev.source_tank_id, "to_tank": dst.tank_id,
                        "count": dst.count})
    for ev in grade_events or []:
        srcs = list(getattr(ev, "source_tank_ids", []) or [])
        if len(srcs) != 1:
            continue
        wk = _week_label(_d(ev.event_date))
        for dst in ev.destinations:
            out.append({"week": wk, "batch": ev.batch_id, "from_tank": srcs[0],
                        "to_tank": dst.tank_id, "count": dst.count})
    return out


# ---- inputs from a finished workbook (the app) ------------------------------

def _int_or_none(v) -> Optional[int]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else None


def inputs_from_workbook(wb) -> tuple[list[dict], list[dict], Optional[date]]:
    """(harvests, transfers, report_start) read from a run's workbook:
    HarvestPlan (whole fish, HOG kg, yield; dated on the week's Monday --
    the sheet has no day), TransferPlan, and the report start from the Daily
    Harvest Schedule header ('Forecast start YYYY-MM-DD')."""
    import re
    harvests, transfers, rs = [], [], None
    if "HarvestPlan" in wb.sheetnames:
        for row in wb["HarvestPlan"].iter_rows(min_row=5, values_only=True):
            if not row or not isinstance(row[0], str) or "-W" not in row[0]:
                continue
            tank = _int_or_none(row[2])
            if tank is None or not isinstance(row[3], (int, float)):
                continue
            y, w = int(row[0][:4]), int(row[0][6:8])
            harvests.append({
                "date": date.fromisocalendar(y, w, 1), "tank": tank,
                "batch": str(row[1]), "count": float(row[3]),
                "hog_kg": float(row[8] or 0) if len(row) > 8 else 0.0,
                "hog_yield": float(row[6]) if len(row) > 6 and row[6] else 0.81,
            })
    if "TransferPlan" in wb.sheetnames:
        for row in wb["TransferPlan"].iter_rows(min_row=5, values_only=True):
            if not row or not isinstance(row[0], str) or "-W" not in row[0]:
                continue
            src, dst = _int_or_none(row[3]), _int_or_none(row[4])
            if src is None or dst is None:
                continue
            if row[2] not in ("Transfer", "Grade"):
                continue
            if row[2] == "Grade" and row[7] == "retention":
                continue
            transfers.append({"week": row[0], "batch": str(row[1]),
                              "from_tank": src, "to_tank": dst,
                              "count": float(row[5] or 0)})
    if "Daily Harvest Schedule" in wb.sheetnames:
        hdr = wb["Daily Harvest Schedule"].cell(row=2, column=1).value or ""
        m = re.search(r"(\d{4}-\d{2}-\d{2})", str(hdr))
        if m:
            rs = date.fromisoformat(m.group(1))
    return harvests, transfers, rs
