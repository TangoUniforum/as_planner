"""Lifetime (accumulated) per-batch results: the report's history + the forecast.

Operator 2026-10-10: "if a full production report is uploaded then the app
should build the accumulated results too, and if it doesn't or it's just the
PR, then we keep all the current methods". A full monthly production report
carries an accumulated tab ("Acc"): one row per fish group, the FishTalk
"in period" columns summed from the group's first day to the report's closing
date (opening counts 0). This module finds that tab BY CONTENT (any name), adds
the forecast's per-batch flows after the closing date, and writes two sheets:

* BatchAccumulated -- per batch: History (the Acc row), Forecast (closing + 1
  to the horizon end) and Lifetime (the two together).
* BatchAccumulatedMonthly -- per batch, lifetime-to-date at the report date and
  at the end of every forecast month.

What adds up and what does not (measured 2026-10-10 on the Aug'26 and Sep'26
reports: Aug Acc + Sep month == Sep Acc on every summed column, 16/16 batches):
counts, kg, feed and growth are SUMS and are exact; Bio FCR = feed / gross
growth, Econ FCR = feed / net growth (incl discards) and Mortality % =
mortality / eggs are recomputed from those sums exactly as FishTalk does
(FishTalk Control User Guide, glossary). FishTalk does not document how it
accumulates SGR over a lifetime; a day-weighted average of the period SGRs
reproduces the Acc tab to ~0.03 on most batches, so SGR is shown as that and
labelled approximate. SFR, temperature and growth-vs-model are not carried.

Costs use config/costs.yaml (operator): forecast feed is priced by the feed
type it actually is; HISTORY feed has no type in the Acc tab, so it is split by
type along a typical growth path (feed in each size band ~ weight gained in the
band x model FCR at that size, from the batch's egg weight to its weight at the
report) and priced the same way -- an estimate, labelled as one. Eggs are
priced at costs.yaml's egg price. Fixed monthly cost is not per batch and is
left out.

Report layer only: nothing here feeds the planner, the audits or a gate.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Optional

ACC_SHEET = "BatchAccumulated"
ACC_MONTHLY_SHEET = "BatchAccumulatedMonthly"

# Acc column -> field. Labels are matched after production_report._norm_label.
_ACC_LABELS = {
    "open_count": ("opening count",),
    "close_count": ("closing count",),
    "close_bio": ("closing biomass [kg]", "closing biomass"),
    "close_wt": ("closing avg weight",),
    "xin_count": ("transfer in count in period",),
    "xout_count": ("transfer out count in period",),
    "dev_count": ("deviation count in period",),
    "sgr": ("sgr in period",),
    "net_growth": ("net growth (incl discards) [kg] in period",),
    "gross_growth": ("gross growth in period",),
    "gross_growth_pct": ("gross growth % in period",),
    "feed": ("feed amount in period",),
    "harv_count": ("harvested count (incl discards) in period",),
    "harv_gross": ("gross harvested biomass, incl. discards [kg] in period",
                   "gross harvested biomass, incl. discards [kg]"),
    "mort_bio": ("mortality biomass in period",),
    "mort_count": ("mortality count in period",),
    "cull_bio": ("culling biomass in period",),
    "cull_count": ("culling count in period",),
    "mort_pct": ("mortality percent in period",),
    "bio_fcr": ("biological fcr in period",),
    "econ_fcr": ("economical fcr in period",),
}
_ACC_REQUIRED = ("open_count", "close_count", "close_bio", "gross_growth",
                 "feed", "harv_count", "harv_gross", "mort_count", "cull_count")


@dataclass
class AccBatch:
    """One fish group's lifetime-to-date row from the accumulated tab."""
    batch_id: str
    close_count: float = 0.0
    close_bio: float = 0.0
    close_wt: float = 0.0
    xin_count: float = 0.0
    xout_count: float = 0.0
    dev_count: float = 0.0
    sgr: float = 0.0
    net_growth: float = 0.0
    gross_growth: float = 0.0
    gross_growth_pct: float = 0.0
    feed: float = 0.0
    harv_count: float = 0.0
    harv_gross: float = 0.0
    mort_bio: float = 0.0
    mort_count: float = 0.0
    cull_bio: float = 0.0
    cull_count: float = 0.0
    mort_pct: float = 0.0
    bio_fcr: float = 0.0
    econ_fcr: float = 0.0

    @property
    def eggs(self) -> float:
        """Fish that entered the group. The tab has no input column, but the
        lifetime balance holds it (opening is 0): close + mortality + cull +
        harvest - deviation - transfers in + transfers out. Measured: equals
        batches.yaml's input_count on 14 of 15 Sep'26 batches."""
        return max(0.0, self.close_count + self.mort_count + self.cull_count
                   + self.harv_count - self.dev_count - self.xin_count
                   + self.xout_count)

    @property
    def egg_bio(self) -> Optional[float]:
        """Biomass stocked, from FishTalk's Gross growth % (= gross growth over
        the opening-of-life biomass): ~0.16-0.25 g an egg on Sep'26."""
        if self.gross_growth_pct > 0 and self.gross_growth > 0:
            return self.gross_growth / self.gross_growth_pct * 100.0
        return None


def _norm(v) -> str:
    from .production_report import _norm_label
    return _norm_label(v)


def _group_name(row) -> Optional[str]:
    for c in row[:4]:
        if isinstance(c, str) and "Fish group name" in c:
            b = c.split(":", 1)[1].strip()
            return b or None
    return None


def _scan(ws):
    """(colmap, batch rows, has_units, has_closing_month) for one sheet."""
    colmap = None
    rows = []
    has_units = has_cm = False
    for row in ws.iter_rows(values_only=True):
        if not row:
            continue
        if colmap is None:
            labs = {_norm(c): i for i, c in enumerate(row) if isinstance(c, str)}
            if "opening count" in labs and "closing count" in labs:
                colmap = {}
                for f, wants in _ACC_LABELS.items():
                    for w in wants:
                        if w in labs:
                            colmap[f] = labs[w]
                            break
                continue
        for c in row[:5]:
            if isinstance(c, str):
                s = c.strip()
                if s.startswith("Unit:"):
                    has_units = True
                if s.startswith("Closing Month"):
                    has_cm = True
        if colmap is not None and _group_name(row):
            rows.append(row)
    return colmap, rows, has_units, has_cm


def find_acc_sheet(wb):
    """The accumulated (lifetime-to-date) tab, identified by CONTENT, or None.

    It looks like the ProductionReport -- same FishTalk columns, one row per
    fish group -- but it has no tank (Unit:) rows, no Closing Month line and
    every group OPENS at 0, because its period starts at the group's first day.
    The tab name is not trusted (find_pr_sheet's lesson): a sheet called "Acc"
    is only checked first."""
    if wb is None:
        return None
    names = sorted(wb.sheetnames, key=lambda n: (n.strip().lower() != "acc", 0))
    for n in names:
        ws = wb[n]
        try:
            colmap, rows, has_units, has_cm = _scan(ws)
        except Exception:  # noqa: BLE001 - an unreadable sheet is simply not it
            continue
        if colmap is None or not rows or has_units or has_cm:
            continue
        oc = colmap.get("open_count")
        if oc is None:
            continue
        if all(not (isinstance(r[oc], (int, float)) and r[oc]) for r in rows):
            return ws
    return None


def read_acc(ws) -> tuple[dict, list]:
    """({batch: AccBatch}, missing required columns)."""
    colmap, rows, _u, _c = _scan(ws)
    colmap = colmap or {}
    missing = [f for f in _ACC_REQUIRED if f not in colmap]
    out: dict[str, AccBatch] = {}
    for row in rows:
        b = _group_name(row)
        rec = AccBatch(batch_id=b)
        for f, i in colmap.items():
            if f == "open_count" or i >= len(row):
                continue
            v = row[i]
            if isinstance(v, (int, float)):
                setattr(rec, f, float(v))
        out[b] = rec
    return out, missing


def check_against_pr(acc: dict, pr_period) -> list:
    """Batches whose Acc closing count disagrees with the PR sheet's, or that
    the PR holds and the Acc tab lacks. Both come from the same report, so they
    must agree to the fish -- anything else means the tab is from another
    report and lifetime figures built on it would be wrong."""
    bad = []
    if pr_period is None:
        return ["the ProductionReport has no batch rows to check against"]
    for b, pb in pr_period.batches.items():
        a = acc.get(b)
        if a is None:
            if (pb.close_count or 0.0) > 0:
                bad.append(f"{b}: in the PR ({pb.close_count:,.0f} fish) but not in the accumulated tab")
            continue
        if abs(a.close_count - (pb.close_count or 0.0)) > 1.0:
            bad.append(f"{b}: accumulated tab closes at {a.close_count:,.0f} fish, "
                       f"the PR at {pb.close_count:,.0f}")
    return bad


def history_feed_shares(w0_g: float, w1_g: float, tables, fcr_key: str) -> dict:
    """{feed type: share} of a batch's feed from w0 to w1 grams, along a
    typical growth path: feed in each size step = weight gained x model FCR at
    that size, booked to the feed type the model uses at that size. Survival is
    not modelled (every step is per fish), so the small, early feeds are a
    slight under-estimate -- they are a few percent of a lifetime's kg."""
    from .biology import _feed_type_for_size, _interp
    if tables is None or not getattr(tables, "feed_types", None):
        return {}
    curve = tables.fcr_by_model.get(fcr_key, []) if getattr(tables, "fcr_by_model", None) else []
    w0 = max(float(w0_g or 0.0), 0.01)
    w1 = float(w1_g or 0.0)
    if w1 <= w0:
        return {_feed_type_for_size(tables, max(w1, w0)): 1.0}
    n = 400
    r = (w1 / w0) ** (1.0 / n)
    feed: dict[str, float] = defaultdict(float)
    w = w0
    for _ in range(n):
        w2 = w * r
        mid = math.sqrt(w * w2)
        fcr = _interp(mid, tables.fcr_size_g, curve) if curve else 1.2
        feed[_feed_type_for_size(tables, mid)] += (w2 - w) * fcr
        w = w2
    tot = sum(feed.values())
    return {k: v / tot for k, v in feed.items()} if tot > 0 else {}


def _fcr_key(batch, batch_by_id) -> str:
    from .biology import _fcr_model_key
    if batch is not None and getattr(batch, "fcr_model", None):
        return _fcr_model_key(batch.fcr_model)
    keys = [_fcr_model_key(b.fcr_model) for b in (batch_by_id or {}).values()
            if getattr(b, "fcr_model", None)]
    return max(set(keys), key=keys.count) if keys else "1.18"


def _cost(feed_by_type: dict, eggs: float, costs) -> Optional[dict]:
    if costs is None:
        return None
    from .costs import period_cost
    return period_cost(feed_by_type, eggs, 0.0, costs)


# --------------------------------------------------------------------------- #
# Totals
# --------------------------------------------------------------------------- #
_SUM_KEYS = ("eggs", "xin", "mort_count", "mort_bio", "cull_count", "cull_bio",
             "harv_count", "harv_gross", "harv_hog", "feed", "gross_growth",
             "net_growth", "feed_cost", "feed_linked_cost", "egg_cost",
             "unpriced_kg", "sgr_days", "sgr_x_days")


def _zero() -> dict:
    return {k: 0.0 for k in _SUM_KEYS}


def _add(a: dict, b: dict) -> dict:
    return {k: (a.get(k) or 0.0) + (b.get(k) or 0.0) for k in _SUM_KEYS}


def _history_totals(a: AccBatch, hog_yield, costs, tables, batch, batch_by_id,
                    days_hist) -> dict:
    t = _zero()
    t.update(eggs=a.eggs, xin=a.xin_count, mort_count=a.mort_count, mort_bio=a.mort_bio,
             cull_count=a.cull_count, cull_bio=a.cull_bio,
             harv_count=a.harv_count, harv_gross=a.harv_gross,
             harv_hog=a.harv_gross * hog_yield, feed=a.feed,
             gross_growth=a.gross_growth, net_growth=a.net_growth)
    if days_hist and a.sgr:
        t["sgr_days"], t["sgr_x_days"] = days_hist, a.sgr * days_hist
    if costs is not None:
        w0 = (a.egg_bio / a.eggs * 1000.0) if (a.egg_bio and a.eggs) else 0.2
        w1 = (a.close_wt if a.close_count > 0 and a.close_wt
              else (a.harv_gross / a.harv_count * 1000.0 if a.harv_count else w0))
        shares = history_feed_shares(w0, w1, tables, _fcr_key(batch, batch_by_id))
        c = _cost({k: a.feed * s for k, s in shares.items()} if shares
                  else {"(unknown type)": a.feed}, a.eggs, costs)
        t["feed_cost"] = c["feed"]
        t["feed_linked_cost"] = c["shipping"] + c["oxygen"] + c["chemicals"]
        t["egg_cost"] = c["eggs"]
        t["unpriced_kg"] = c["unpriced_kg"]
    return t


def _month_totals(m: dict, feed_by_type: dict, costs) -> dict:
    t = _zero()
    t.update(eggs=m.get("input_count") or 0.0,
             mort_count=m.get("mort_count") or 0.0,
             mort_bio=m.get("mort_bio") or 0.0,
             cull_count=m.get("cull_count") or 0.0,
             cull_bio=m.get("cull_bio") or 0.0,
             harv_count=m.get("harv_count_shown") or m.get("harv_count") or 0.0,
             harv_gross=m.get("harv_gross") or 0.0,
             harv_hog=m.get("harv_hog") or 0.0,
             feed=m.get("feed") or 0.0,
             gross_growth=m.get("gross_growth") or 0.0)
    # FishTalk: Net growth = Gross growth - mortality - culls (glossary).
    t["net_growth"] = t["gross_growth"] - t["mort_bio"] - t["cull_bio"]
    oc, ob, cc, cb = (m.get("open_count") or 0.0, m.get("open_bio") or 0.0,
                      m.get("close_count") or 0.0, m.get("close_bio") or 0.0)
    days = m.get("days") or 0.0
    if oc > 0 and cc > 0 and ob > 0 and cb > 0 and days > 0:
        # FishTalk's SGR: ((W2/W1)^(1/days) - 1) x 100 on average weights.
        sgr = ((cb / cc) / (ob / oc)) ** (1.0 / days) * 100.0 - 100.0
        t["sgr_days"], t["sgr_x_days"] = days, sgr * days
    if costs is not None:
        c = _cost(feed_by_type, t["eggs"], costs)
        t["feed_cost"] = c["feed"]
        t["feed_linked_cost"] = c["shipping"] + c["oxygen"] + c["chemicals"]
        t["egg_cost"] = c["eggs"]
        t["unpriced_kg"] = c["unpriced_kg"]
    return t


def _ratios(t: dict, egg_bio=None) -> dict:
    # Mortality / cull % of the fish that ENTERED the group: eggs plus fish
    # transferred in from other groups (FishTalk's base; B13 on Sep'26: within
    # 69 fish of the tab's 36.61%). Batches stocked as eggs have no transfers.
    eggs = t["eggs"] + (t.get("xin") or 0.0)
    total_cost = t["feed_cost"] + t["feed_linked_cost"] + t["egg_cost"]
    return {
        "mort_pct": 100.0 * t["mort_count"] / eggs if eggs else None,
        "cull_pct": 100.0 * t["cull_count"] / eggs if eggs else None,
        "avg_harv_hog": t["harv_hog"] / t["harv_count"] if t["harv_count"] else None,
        "bio_fcr": t["feed"] / t["gross_growth"] if t["gross_growth"] > 0 else None,
        "econ_fcr": t["feed"] / t["net_growth"] if t["net_growth"] > 0 else None,
        "gg_pct": 100.0 * t["gross_growth"] / egg_bio if egg_bio else None,
        "sgr": t["sgr_x_days"] / t["sgr_days"] if t["sgr_days"] else None,
        "total_cost": total_cost,
        "cost_per_kg_hog": total_cost / t["harv_hog"] if t["harv_hog"] > 0 else None,
    }


# --------------------------------------------------------------------------- #
# Sheets
# --------------------------------------------------------------------------- #
_COLS = [
    ("Eggs_In (fish)", "eggs", 0), ("Mort_Count (fish)", "mort_count", 0),
    ("Mortality (% of fish in)", "mort_pct", 2), ("Cull_Count (fish)", "cull_count", 0),
    ("Cull (% of fish in)", "cull_pct", 2), ("Harv_Count (fish)", "harv_count", 0),
    ("Harv_Gross (kg)", "harv_gross", 0), ("Harv_HOG (kg)", "harv_hog", 0),
    ("Avg_Harv_HOG (kg)", "avg_harv_hog", 2), ("Feed (kg)", "feed", 0),
    ("Gross_Growth (kg)", "gross_growth", 0), ("Net_Growth (kg)", "net_growth", 0),
    ("Bio_FCR", "bio_fcr", 2), ("Econ_FCR", "econ_fcr", 2),
    ("Gross_Growth (%)", "gg_pct", 0), ("SGR approx (%/day)", "sgr", 2),
    ("Feed_Cost", "feed_cost", 0), ("Feed_Linked_Cost", "feed_linked_cost", 0),
    ("Egg_Cost", "egg_cost", 0), ("Total_Cost", "total_cost", 0),
    ("Cost per kg HOG", "cost_per_kg_hog", 2),
]


def _cells(t: dict, egg_bio=None, costs_on=True) -> list:
    r = _ratios(t, egg_bio)
    r.update({k: v for k, v in (t.get("_tab") or {}).items() if v is not None})
    vals = {**t, **r}
    out = []
    for _h, k, nd in _COLS:
        if not costs_on and k in ("feed_cost", "feed_linked_cost", "egg_cost",
                                  "total_cost", "cost_per_kg_hog"):
            out.append(None)
            continue
        v = vals.get(k)
        out.append(None if v is None else (round(v, nd) if nd else round(v)))
    return out


def write_batch_accumulated(wb, *, acc_ws, pr_period, month_rows, feed_bt_week,
                            wk_start, report_start, pr_closing, batch_by_id,
                            tables, config_dir, hog_yield: float,
                            scenario_name: str = "") -> dict:
    """Write BatchAccumulated + BatchAccumulatedMonthly. Returns a status dict
    {built: bool, reason: str, batches: int}. Costs come from
    `config_dir/costs.yaml` (read-only; CostsAndProfit's hook is untouched)."""
    costs, costs_note = None, "config/costs.yaml not set - no costs."
    try:
        from .costs import load_costs
        costs = load_costs(config_dir)
        if costs is not None:
            costs_note = ("config/costs.yaml (feed by type, shipping/oxygen/"
                          "chemicals per kg feed, eggs).")
    except ValueError as exc:
        costs_note = f"config/costs.yaml unreadable ({exc}) - no costs."

    from .batch_order import sorted_batches
    from .time_grid import calendar_day_month_split

    acc, missing = read_acc(acc_ws)
    if missing:
        return {"built": False, "batches": 0,
                "reason": f"accumulated tab '{acc_ws.title}' lacks column(s): {', '.join(missing)}"}
    bad = check_against_pr(acc, pr_period)
    if bad:
        return {"built": False, "batches": 0,
                "reason": (f"accumulated tab '{acc_ws.title}' does not match the "
                           f"ProductionReport ({len(bad)} batch(es): "
                           + "; ".join(bad[:4]) + ("..." if len(bad) > 4 else "") + ")")}

    # Forecast feed by (batch, type, month), clipped at the report start --
    # the same roll-up FeedForecastMonthly prints.
    fbtm: dict = defaultdict(lambda: defaultdict(float))   # (batch, month) -> {type: kg}
    for (bid, name, wk), v in (feed_bt_week or {}).items():
        for (yr, mon), frac in calendar_day_month_split(wk_start.get(wk), clip_start=report_start).items():
            fbtm[(bid, f"{yr}-{mon:02d}")][name] += v * frac
    months_by_batch: dict = defaultdict(list)
    for mo, b, a in month_rows or ():
        months_by_batch[b].append((mo, a))

    pcd = pr_closing.date() if hasattr(pr_closing, "date") else pr_closing
    costs_on = costs is not None
    all_batches = sorted_batches(set(acc) | set(months_by_batch))

    def _days_hist(b):
        bt = (batch_by_id or {}).get(b)
        d = getattr(bt, "input_date", None)
        d = d.date() if hasattr(d, "date") else d
        if d is None or pcd is None:
            return None
        return (pcd - d).days + 1

    lifetime_rows, monthly_rows = [], []
    for b in all_batches:
        a = acc.get(b)
        bt = (batch_by_id or {}).get(b)
        if a is None:
            status, hist, egg_bio = "Stocked after the report", _zero(), None
        else:
            status = ("In stock at the report" if a.close_count > 0
                      else "Closed before the report")
            hist = _history_totals(a, hog_yield, costs, tables, bt, batch_by_id,
                                   _days_hist(b) if months_by_batch.get(b) else None)
            egg_bio = a.egg_bio
        fc = _zero()
        run = dict(hist)
        # A batch in stock at the report with no stocking date cannot weight
        # its history SGR by days: its lifetime SGR is left blank, never the
        # forecast's alone passed off as a lifetime figure.
        sgr_ok = not (a is not None and a.sgr and not hist["sgr_days"])
        last_close = (a.close_count, a.close_bio) if a else (0.0, 0.0)
        if a is not None:
            monthly_rows.append([b, status, f"to {pcd}", "history",
                                 round(a.close_count), round(a.close_bio)]
                                + _cells(_hist_display(hist, a), egg_bio, costs_on))
        for mo, m in sorted(months_by_batch.get(b, []), key=lambda x: x[0]):
            mt = _month_totals(m, dict(fbtm.get((b, mo), {})), costs)
            fc = _add(fc, mt)
            run = _add(run, mt)
            if not sgr_ok:
                run["sgr_days"] = run["sgr_x_days"] = 0.0
            last_close = (m.get("close_count") or 0.0, m.get("close_bio") or 0.0)
            monthly_rows.append([b, status, mo, "lifetime to month end",
                                 round(last_close[0]), round(last_close[1])]
                                + _cells(run, egg_bio, costs_on))
        life = _add(hist, fc)
        if not sgr_ok:
            life["sgr_days"] = life["sgr_x_days"] = 0.0
        if a is not None:
            lifetime_rows.append([b, status, "History", round(a.close_count), round(a.close_bio)]
                                 + _cells(_hist_display(hist, a), egg_bio, costs_on))
        if months_by_batch.get(b):
            lifetime_rows.append([b, status, "Forecast", round(last_close[0]), round(last_close[1])]
                                 + _cells(fc, None, costs_on))
            lifetime_rows.append([b, status, "Lifetime", round(last_close[0]), round(last_close[1])]
                                 + _cells(life, egg_bio, costs_on))

    head = ["Batch", "Status", "Part", "Fish in tanks at end", "Biomass in tanks at end (kg)"] + [h for h, _k, _n in _COLS]
    mhead = ["Batch", "Status", "Month", "Basis", "Fish in tanks (month end)", "Biomass (kg, month end)"] + [h for h, _k, _n in _COLS]
    legend = (
        f"Lifetime per batch = the report's accumulated tab ('{acc_ws.title}', its "
        f"history to {pcd}) + this forecast from {report_start}. Counts, kg, feed and "
        f"growth are sums (exact). Mortality/Cull % are of the fish that entered (eggs + transfers in; eggs = the "
        f"tab's own lifetime balance). Bio FCR = feed / gross growth, Econ FCR = feed / "
        f"net growth (FishTalk). Gross growth % = gross growth / biomass stocked "
        f"(history only). SGR is a day-weighted average of period SGRs - "
        f"APPROXIMATE (FishTalk does not document its lifetime SGR). HOG of history "
        f"harvest = gross x {hog_yield}. Costs: {costs_note} History feed has no "
        f"type in the tab, so its cost is an ESTIMATE: split by type along a "
        f"typical growth path (weight gained x model FCR per size band), priced at "
        f"today's prices. Fixed monthly cost is not per batch and is not included."
    )
    for name, hdr, rows, title in (
            (ACC_SHEET, head, lifetime_rows, "BATCH ACCUMULATED - lifetime per batch (history + forecast)"),
            (ACC_MONTHLY_SHEET, mhead, monthly_rows, "BATCH ACCUMULATED MONTHLY - lifetime to date at each month end")):
        if name in wb.sheetnames:
            del wb[name]
        ws = wb.create_sheet(name)
        ws.append([f"{title}" + (f" - {scenario_name}" if scenario_name else "")])
        ws.append([legend])
        ws.append([])
        ws.append(hdr)
        for r in rows:
            ws.append(r)
    return {"built": True, "batches": len(all_batches), "reason": ""}


def _hist_display(hist: dict, a: AccBatch) -> dict:
    """History as the tab states it: its own SGR, Mortality %, Bio and Econ FCR
    are shown, not re-derived. They agree with the re-derivation on every batch
    stocked as eggs; on old groups merged by transfers (B13-B19) FishTalk's
    bases differ, and the tab is the authority for its own history."""
    h = dict(hist)
    h["sgr_days"], h["sgr_x_days"] = (1.0, a.sgr) if a.sgr else (0.0, 0.0)
    h["_tab"] = {"mort_pct": a.mort_pct or None, "bio_fcr": a.bio_fcr or None,
                 "econ_fcr": a.econ_fcr or None}
    return h
