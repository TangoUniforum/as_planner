"""Analysis layer — the composition that turns many runs into ONE decision.

The app's modes (Compare & Choose, Optimize, Tune) each answer a PIECE of the
operator's real question: "which engine, with which knobs, gives the best plan
that passes the hard rules?" This module holds the pieces the composition
needs that don't exist anywhere else:

  * hard/soft GATES as a registry (conservation, never-an-empty-week, caps,
    harvest targets) — the checklist that makes "did I miss something?"
    structurally impossible to answer wrong;
  * harvest TARGETS (monthly/yearly kg) — config-owned, penalized not
    hard-gated (operator decision 2026-08-05);
  * ECONOMICS (price per fish size) — turns harvest kg into revenue;
  * the PROMOTED DEFAULT — the operator-blessed best candidate, stored in
    config/ (versioned, exported with config snapshots, never in an output
    workbook) so it survives sessions and cannot be lost by a run.

Extensibility rule: the analysis flow iterates registries — it does not know
what the gates/objectives are. A future lever (facility expansion, stocking
frequency/size) or objective (growth, revenue emphasis) is a register() call
plus its implementation, not a rewrite of the flow. Same pattern as
forecast.methods.REGISTRY.

Everything here is pure logic + file IO; Streamlit stays in app.py.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import yaml

from .yaml_atomic import read_text_resilient, write_text_atomic

TARGETS_FILE = "targets.yaml"
ECONOMICS_FILE = "economics.yaml"
DEFAULTS_FILE = "analysis_defaults.yaml"


# --------------------------------------------------------------------------- #
# Config files: targets, economics, promoted default
# --------------------------------------------------------------------------- #
def _load_yaml_or_none(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    data = yaml.safe_load(read_text_resilient(path))
    return data if isinstance(data, dict) else None


def _dump_yaml(path: Path, data: dict) -> None:
    write_text_atomic(path, yaml.safe_dump(data, sort_keys=False,
                                           allow_unicode=True))


def load_targets(config_dir) -> Optional[dict]:
    """{basis: 'hog'|'gross', tolerance_pct: float,
        monthly: {'YYYY-MM': kg}, yearly: {'YYYY': kg}} or None if unset.

    Missing/empty file -> None (the targets gate reports N/A, never FAIL:
    absent targets must not block analysis)."""
    d = _load_yaml_or_none(Path(config_dir) / TARGETS_FILE)
    if not d:
        return None
    out = {
        "basis": str(d.get("basis", "hog")).lower(),
        "tolerance_pct": float(d.get("tolerance_pct", 5.0)),
        "monthly": {str(k): float(v) for k, v in (d.get("monthly") or {}).items()
                    if v is not None},
        "yearly": {str(k): float(v) for k, v in (d.get("yearly") or {}).items()
                   if v is not None},
    }
    return out if (out["monthly"] or out["yearly"]) else None


def save_targets(config_dir, targets: dict) -> None:
    _dump_yaml(Path(config_dir) / TARGETS_FILE, targets)


def load_economics(config_dir) -> Optional[dict]:
    """{currency, basis: 'hog'|'gross', model_cv_pct,
        price_bands: [{min_kg, max_kg, price_per_kg, monthly: {YYYY-MM: p}}]}
    or None.

    model_cv_pct — the SALES-side harvest weight-distribution CV (%), the
    operator's Model_CV: each harvest event's kg is spread across the bands
    with a size-biased lognormal around the event's average weight (the
    operator's own Excel method). Re-tuned against historical harvest results,
    hence a config variable, not a constant. Distinct from the biological
    grading CV (tran_og_cv).

    price_per_kg is the default price; `monthly` optionally overrides it per
    forecast month. kg falling outside every band is reported unpriced — a
    loud gap beats silently inventing a price."""
    d = _load_yaml_or_none(Path(config_dir) / ECONOMICS_FILE)
    if not d:
        return None
    bands = []
    for b in d.get("price_bands") or []:
        try:
            monthly = {str(k): float(v)
                       for k, v in (b.get("monthly") or {}).items()
                       if v is not None}
            bands.append({"min_kg": float(b["min_kg"]),
                          "max_kg": float(b["max_kg"]),
                          "price_per_kg": float(b["price_per_kg"]),
                          "monthly": monthly})
        except (KeyError, TypeError, ValueError):
            continue
    if not bands:
        return None
    return {"currency": str(d.get("currency", "USD")),
            "basis": str(d.get("basis", "hog")).lower(),
            "model_cv_pct": float(d.get("model_cv_pct", 18.0) or 18.0),
            "price_bands": sorted(bands, key=lambda b: b["min_kg"])}


def save_economics(config_dir, economics: dict) -> None:
    _dump_yaml(Path(config_dir) / ECONOMICS_FILE, economics)


def load_promoted_default(config_dir) -> Optional[dict]:
    """The operator-blessed analysis candidate:
    {method, overrides, promoted_ts, note, evidence} or None."""
    d = _load_yaml_or_none(Path(config_dir) / DEFAULTS_FILE)
    return d if d and d.get("method") else None


def save_promoted_default(config_dir, method: str, overrides: dict,
                          promoted_ts: str, note: str = "",
                          evidence: Optional[dict] = None) -> None:
    _dump_yaml(Path(config_dir) / DEFAULTS_FILE, {
        "method": method,
        "overrides": overrides or {},
        "promoted_ts": promoted_ts,
        "note": note,
        "evidence": evidence or {},
    })


# --------------------------------------------------------------------------- #
# Harvest readers — HarvestPlan rows -> period totals + revenue
# --------------------------------------------------------------------------- #
def week_to_month(week_label: str) -> Optional[str]:
    """'2026-W31' -> '2026-07' (month of the ISO week's Monday — the SAME
    convention as the app's monthly harvest table, so targets and the Harvest
    tab can never disagree about which month a week belongs to)."""
    try:
        y, w = int(str(week_label)[:4]), int(str(week_label)[6:8])
        return _dt.date.fromisocalendar(y, w, 1).strftime("%Y-%m")
    except (ValueError, TypeError):
        return None


def harvest_rows(out_path) -> list[dict]:
    """Per-event harvest rows from the output workbook's HarvestPlan sheet:
    [{week, count, gross_avg_kg, gross_kg, hog_kg, hog_avg_kg}, ...]."""
    import openpyxl
    wb = openpyxl.load_workbook(out_path, read_only=True, data_only=True)
    try:
        if "HarvestPlan" not in wb.sheetnames:
            return []
        ws = wb["HarvestPlan"]
        header = None
        rows: list[dict] = []
        for r in ws.iter_rows(values_only=True):
            if header is None:
                if r and str(r[0]).strip() == "Week" and any(
                        str(c).strip() == "Batch" for c in r if c):
                    header = {str(c).strip(): i for i, c in enumerate(r) if c}
                continue
            if not r or not str(r[0]).startswith("20"):
                continue

            def _num(prefix, _r=r, _h=header):
                k = next((c for c in _h if c.startswith(prefix)), None)
                v = _r[_h[k]] if k is not None and _h[k] < len(_r) else None
                return float(v) if isinstance(v, (int, float)) else 0.0

            count = _num("Count")
            hog_kg = _num("HOG_Biomass")
            rows.append({
                "week": str(r[0]).strip(),
                "count": count,
                "gross_avg_kg": _num("Gross_AvgWt"),
                "gross_kg": _num("Gross_Biomass"),
                "hog_kg": hog_kg,
                "hog_avg_kg": (hog_kg / count) if count > 0 else 0.0,
            })
        return rows
    finally:
        wb.close()


def harvest_by_period(rows: list[dict], basis: str = "hog"
                      ) -> tuple[dict, dict]:
    """({'YYYY-MM': kg}, {'YYYY': kg}) on the given basis ('hog'|'gross')."""
    key = "hog_kg" if basis == "hog" else "gross_kg"
    monthly: dict[str, float] = {}
    yearly: dict[str, float] = {}
    for r in rows:
        m = week_to_month(r["week"])
        if m is None:
            continue
        monthly[m] = monthly.get(m, 0.0) + r[key]
        y = m[:4]
        yearly[y] = yearly.get(y, 0.0) + r[key]
    return monthly, yearly


def review_targets(monthly: dict, yearly: dict, targets: dict) -> dict:
    """Score actuals against targets — PENALIZED, not hard-gated (operator
    decision): a miss beyond tolerance is flagged, never disqualifying.

    Only periods the plan's horizon actually reaches are judged: a target for
    a month with zero recorded harvest AND no neighboring in-horizon month is
    still judged (0 vs target) IF any harvest month >= it exists — i.e. we
    judge every target period up to the last month with any harvest, so a
    blackout month inside the horizon shows as MISSED, while targets beyond
    the horizon end are N/A rather than false misses.

    Returns {rows: [{period, target_kg, actual_kg, pct, status}],
             judged, met, close, missed, worst_pct, total_shortfall_kg}."""
    tol = float(targets.get("tolerance_pct", 5.0))
    horizon_end_m = max(monthly) if monthly else ""
    horizon_end_y = max(yearly) if yearly else ""
    rows = []

    def _judge(period, target_kg, actual_kg, in_horizon):
        if not in_horizon:
            return {"period": period, "target_kg": target_kg,
                    "actual_kg": actual_kg, "pct": None, "status": "N/A"}
        pct = (actual_kg / target_kg * 100.0) if target_kg > 0 else 100.0
        status = ("MET" if pct >= 100.0 - 1e-9
                  else "CLOSE" if pct >= 100.0 - tol else "MISSED")
        return {"period": period, "target_kg": target_kg,
                "actual_kg": actual_kg, "pct": pct, "status": status}

    for period in sorted(targets.get("monthly") or {}):
        rows.append(_judge(period, targets["monthly"][period],
                           monthly.get(period, 0.0),
                           bool(horizon_end_m) and period <= horizon_end_m))
    for period in sorted(targets.get("yearly") or {}):
        rows.append(_judge(period, targets["yearly"][period],
                           yearly.get(period, 0.0),
                           bool(horizon_end_y) and period <= horizon_end_y))

    judged = [r for r in rows if r["status"] != "N/A"]
    shortfall = sum(max(0.0, r["target_kg"] - r["actual_kg"]) for r in judged)
    worst = min((r["pct"] for r in judged), default=None)
    return {
        "rows": rows,
        "judged": len(judged),
        "met": sum(1 for r in judged if r["status"] == "MET"),
        "close": sum(1 for r in judged if r["status"] == "CLOSE"),
        "missed": sum(1 for r in judged if r["status"] == "MISSED"),
        "worst_pct": worst,
        "total_shortfall_kg": shortfall,
    }


def biomass_band_fraction(mean_kg: float, cv: float,
                          lo_kg: float, hi_kg: float) -> float:
    """Fraction of a harvest event's BIOMASS falling in [lo, hi) — the
    operator's Excel method, verbatim: fish weights ~ lognormal around the
    event mean with the sales-model CV; the biomass (not count) share uses
    the size-biased distribution, i.e. LogN(mu + s^2, s):

        s  = sqrt(ln(1 + cv^2))
        mu = ln(mean) - s^2/2
        share = F(hi; mu+s^2, s) - F(lo; mu+s^2, s)

    cv is a FRACTION here (0.18, not 18). Scale-invariant: mean and band
    edges just need the same unit."""
    import math
    from statistics import NormalDist
    if mean_kg <= 0 or cv < 0:
        return 0.0
    if cv == 0:
        return 1.0 if lo_kg <= mean_kg < hi_kg else 0.0
    s = math.sqrt(math.log(1.0 + cv * cv))
    mu_b = math.log(mean_kg) - 0.5 * s * s + s * s   # mu + s^2 (size-biased)
    nd = NormalDist()

    def _F(x):
        return nd.cdf((math.log(x) - mu_b) / s) if x > 0 else 0.0

    return max(0.0, _F(hi_kg) - _F(lo_kg))


def sixn_outbound_transfers(out_path, production_start_iso: str = ""
                            ) -> Optional[int]:
    """R7 lens: TransferPlan rows moving fish OUT of a 6N tank
    (61/63/65/67/69/71) during the DEPURATION era (weeks before the 6N
    production start). In production mode the mains are ordinary grow-out,
    so later outbound moves are legal rebalancing, not violations.

    Returns the violation count, or None when the sheet is absent (the gate
    reports N/A, never a false verdict)."""
    import openpyxl
    sixn = {"61", "63", "65", "67", "69", "71"}
    cutoff = ""
    if production_start_iso:
        try:
            d = _dt.date.fromisoformat(str(production_start_iso)[:10])
            iso = d.isocalendar()
            cutoff = f"{iso[0]}-W{iso[1]:02d}"
        except (ValueError, TypeError):
            cutoff = ""
    wb = openpyxl.load_workbook(out_path, read_only=True, data_only=True)
    try:
        if "TransferPlan" not in wb.sheetnames:
            return None
        ws = wb["TransferPlan"]
        header = None
        n = 0
        for r in ws.iter_rows(values_only=True):
            if header is None:
                if r and str(r[0]).strip() == "Week" and any(
                        str(c).strip() == "From_Tank" for c in r if c):
                    header = {str(c).strip(): i for i, c in enumerate(r) if c}
                continue
            if not r or not str(r[0]).startswith("20"):
                continue
            week = str(r[0]).strip()
            if cutoff and week >= cutoff:
                continue                      # production era — legal moves
            typ = str(r[header.get("Type", -1)] or "") if "Type" in header else ""
            if typ != "Transfer":
                continue
            ft = r[header["From_Tank"]] if "From_Tank" in header else None
            try:
                ft_s = str(int(float(ft)))
            except (TypeError, ValueError):
                continue
            if ft_s in sixn:
                n += 1
        return n
    finally:
        wb.close()


def density_review(out_path) -> Optional[dict]:
    """Per-batch peak-density distribution for one plan — the diagnostic that
    was Tune mode's reason to exist, now a per-candidate lens. Reuses Tune's
    exact math (TransferTemplate Section B peaks; severe = >=1.3x cap).

    KEY READING RULE (hard-won): severe batches clustering in time and peaking
    mid-grow-out = a STOCKING/CAPACITY problem — no knob fixes it; the stocking
    lever does. Scattered mild overshoot near 1.0-1.1x = normal near-cap
    operation. Returns None when the sheet is absent (e.g. Global outputs
    without Section B) — the gate reports N/A, never a false verdict."""
    from . import tuning
    try:
        peaks, detail = tuning._peaks_and_detail(out_path)
    except Exception:  # noqa: BLE001 — sheet absent/foreign shape -> no lens
        return None
    if not peaks:
        return None
    d = tuning.analyze(peaks)
    return {"n": d.n, "over": d.over, "severe": d.severe,
            "worst": float(d.worst), "median": float(d.median),
            "buckets": dict(d.buckets), "severe_rows": list(detail)}


def revenue_for(rows: list[dict], economics: dict) -> dict:
    """Revenue for a plan: each harvest event's kg is SPREAD across the price
    bands with the size-biased lognormal (model_cv_pct), then priced per band
    — with the band's monthly price override when one exists for the event's
    month, else its default price. kg in no band (distribution tails outside
    the ladder) is unpriced, reported loudly.

    Returns {total, priced_kg, unpriced_kg, currency, by_band}."""
    basis = economics.get("basis", "hog")
    wt_key = "hog_avg_kg" if basis == "hog" else "gross_avg_kg"
    kg_key = "hog_kg" if basis == "hog" else "gross_kg"
    cv = float(economics.get("model_cv_pct", 18.0)) / 100.0
    bands = economics["price_bands"]
    by_band = [{"band": f"{b['min_kg']:g}–{b['max_kg']:g} kg",
                "price_per_kg": b["price_per_kg"], "kg": 0.0, "revenue": 0.0}
               for b in bands]
    total = priced = unpriced = 0.0
    for r in rows:
        w, kg = r[wt_key], r[kg_key]
        if kg <= 0 or w <= 0:
            continue
        month = week_to_month(r["week"])
        event_priced = 0.0
        for i, b in enumerate(bands):
            frac = biomass_band_fraction(w, cv, b["min_kg"], b["max_kg"])
            if frac <= 0:
                continue
            price = (b.get("monthly") or {}).get(month, b["price_per_kg"])
            part = kg * frac
            by_band[i]["kg"] += part
            by_band[i]["revenue"] += part * price
            total += part * price
            event_priced += part
        priced += event_priced
        unpriced += max(0.0, kg - event_priced)
    return {"total": total, "priced_kg": priced, "unpriced_kg": unpriced,
            "currency": economics.get("currency", "USD"), "by_band": by_band}


# --------------------------------------------------------------------------- #
# Result cache — finished runs survive reloads, frozen tabs, and new sessions
# --------------------------------------------------------------------------- #
# Streamlit session_state dies with the browser session; a finished CP-SAT leg
# is 30 minutes of compute. Entries are pickled OUTSIDE OneDrive (multi-MB
# binaries would churn sync) and keyed by name; staleness is the CALLER's
# problem — every stored entry carries its input signature and is checked at
# use, so an old entry is simply re-run, never wrongly trusted.
def _default_cache_dir() -> Path:
    import os
    import tempfile
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "as_planner" / "result_cache"


def cache_save(name: str, obj, cache_dir=None, keep: int = 40) -> None:
    """Pickle `obj` under `name` atomically; evict oldest beyond `keep`."""
    import os
    import pickle
    d = Path(cache_dir) if cache_dir else _default_cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{name}.pkl.tmp-{os.getpid()}"
    with tmp.open("wb") as fh:
        pickle.dump(obj, fh, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, d / f"{name}.pkl")
    files = sorted(d.glob("*.pkl"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    for p in files[keep:]:
        try:
            p.unlink()
        except OSError:
            pass


def cache_load_all(cache_dir=None, prefix: str = "") -> dict:
    """{name: obj} for every readable cached entry (optionally filtered by
    name prefix). A corrupt/unreadable file is SKIPPED, never fatal — the
    worst outcome of a bad cache must be a re-run, not a crash."""
    import pickle
    d = Path(cache_dir) if cache_dir else _default_cache_dir()
    out: dict = {}
    if not d.exists():
        return out
    for p in sorted(d.glob(f"{prefix}*.pkl")):
        try:
            with p.open("rb") as fh:
                out[p.stem] = pickle.load(fh)
        except Exception:  # noqa: BLE001 — see docstring
            continue
    return out


# --------------------------------------------------------------------------- #
# Gate registry — the checklist
# --------------------------------------------------------------------------- #
@dataclass
class Gate:
    key: str
    label: str
    hard: bool                     # hard = a FAIL disqualifies the candidate
    fn: Callable[[dict], tuple]    # ctx -> (status, detail); status in
    #                                PASS / WARN / FAIL / N/A


GATES: list[Gate] = []


def register_gate(key: str, label: str, hard: bool,
                  fn: Callable[[dict], tuple]) -> None:
    """Add a gate to the checklist. The analysis flow evaluates EVERY
    registered gate on every candidate — new rules become part of the
    checklist by registering, not by editing the flow."""
    GATES.append(Gate(key=key, label=label, hard=hard, fn=fn))


def evaluate_gates(ctx: dict) -> list[dict]:
    """Run every registered gate against one candidate's context dict.
    ctx keys used by the built-ins: dropped, overprod, zero_weeks,
    weeks_over_cap, weeks_over_harvest_cap, peak_pct_of_cap, worst_density,
    targets_review (from review_targets, or None)."""
    out = []
    for g in GATES:
        try:
            status, detail = g.fn(ctx)
        except Exception as e:  # noqa: BLE001 — a broken gate must be VISIBLE
            status, detail = "FAIL", f"gate error: {type(e).__name__}: {e}"
        out.append({"key": g.key, "label": g.label, "hard": g.hard,
                    "status": status, "detail": detail})
    return out


def _gate_conservation(ctx):
    d, o = int(ctx.get("dropped") or 0), int(ctx.get("overprod") or 0)
    if d == 0 and o == 0:
        return "PASS", "0 dropped / 0 over-produced"
    return "FAIL", f"{d} dropped / {o} over-produced fish"


def _gate_no_empty_week(ctx):
    z = ctx.get("zero_weeks")
    if z is None:
        return "N/A", "zero-week count unavailable"
    return (("PASS", "harvests something every week") if int(z) == 0
            else ("FAIL", f"{int(z)} totally empty harvest week(s)"))


def _gate_biomass_cap(ctx):
    p = ctx.get("peak_pct_of_cap")
    if p is None:
        return "N/A", "peak biomass unavailable"
    p = float(p)
    if p <= 100.0:
        return "PASS", f"peak {p:.1f}% of cap"
    return ("WARN" if p <= 110.0 else "FAIL"), f"peak {p:.1f}% of cap"


def _gate_harvest_cap(ctx):
    """Weekly harvest target/ceiling gate (operator ruling: 50/60 split).

    FAIL — any week above the HARD processing ceiling (max_harvest_per_week,
           60k): the plan asks the plant for more than it can take.
    WARN — week(s) in the stretch band between the planning target
           (harvest_target_per_week, 50k) and the ceiling: legal, but the
           plan leans on the stretch allowance.
    PASS — every week at/below the target.
    Legacy contexts that only provide `weeks_over_harvest_cap` (no target
    count) keep the historical WARN-only reading of that single number."""
    wc = ctx.get("weeks_over_harvest_cap")
    wt = ctx.get("weeks_over_harvest_target")
    if wc is None and wt is None:
        return "N/A", "weekly harvest series unavailable"
    wc = int(wc or 0)
    if wt is None:                              # legacy single-number context
        return (("PASS", "no week over the processing cap") if wc == 0
                else ("WARN", f"{wc} week(s) over the processing cap"))
    wt = int(wt)
    if wc > 0:
        return "FAIL", (f"{wc} week(s) over the HARD processing ceiling "
                        f"(60k) — must be replanned, the plant cannot take it")
    if wt > 0:
        return "WARN", (f"{wt} week(s) in the 50-60k stretch band (over the "
                        f"weekly target, under the hard ceiling) — legal "
                        f"when biomass demands it")
    return "PASS", "every week at/below the 50k weekly harvest target"


def _gate_targets(ctx):
    tr = ctx.get("targets_review")
    if not tr or not tr.get("judged"):
        return "N/A", "no harvest targets configured (Configure → Targets)"
    if tr["missed"] == 0 and tr["close"] == 0:
        return "PASS", f"all {tr['judged']} target period(s) met"
    if tr["missed"] == 0:
        return "WARN", (f"{tr['close']} period(s) within tolerance, "
                        f"worst {tr['worst_pct']:.0f}% of target")
    return "WARN", (f"{tr['missed']} period(s) missed "
                    f"(worst {tr['worst_pct']:.0f}% of target, "
                    f"short {tr['total_shortfall_kg'] / 1000.0:,.0f} t) — "
                    f"penalized, not disqualifying")


register_gate("conservation", "Conservation (no fish created or lost)",
              hard=True, fn=_gate_conservation)
register_gate("no_empty_week", "Never an empty harvest week", hard=True,
              fn=_gate_no_empty_week)
register_gate("biomass_cap", "Facility biomass cap", hard=False,
              fn=_gate_biomass_cap)
register_gate("harvest_cap", "Weekly harvest target/ceiling (50k/60k)",
              hard=False, fn=_gate_harvest_cap)
register_gate("targets", "Harvest targets (monthly/yearly)", hard=False,
              fn=_gate_targets)


def _gate_density_quality(ctx):
    dr = ctx.get("density_review")
    if not dr:
        return "N/A", "per-batch peak-density data unavailable for this plan"
    if dr["severe"] == 0:
        return "PASS", (f"no batch over 1.3× cap (worst {dr['worst']:.2f}×, "
                        f"{dr['over']}/{dr['n']} touch the cap — normal near "
                        f"full utilisation)")
    return "WARN", (f"{dr['severe']} batch(es) peak ≥1.3× cap (worst "
                    f"{dr['worst']:.2f}×) — if these cluster in time and peak "
                    f"mid-grow-out it's a STOCKING/capacity problem, not a "
                    f"knob: see the stocking lever, don't re-tune")


register_gate("density_quality", "Per-batch density quality", hard=False,
              fn=_gate_density_quality)


def _gate_sixn_one_way(ctx):
    """R7 — 6N one-way commitment: fish moved into 6N depuration may never
    transfer out (only harvest). Counts depuration-era outbound 6N moves."""
    n = ctx.get("sixn_outbound_purge")
    if n is None:
        return "N/A", "TransferPlan unavailable for this plan"
    n = int(n)
    if n == 0:
        return "PASS", "no fish left a 6N depuration tank except by harvest"
    return "FAIL", (f"{n} transfer(s) moved fish OUT of a 6N depuration tank "
                    f"— the one-way commitment (R7) forbids this; those fish "
                    f"may only be harvested")


register_gate("sixn_one_way", "6N one-way commitment (R7)", hard=False,
              fn=_gate_sixn_one_way)


# --------------------------------------------------------------------------- #
# Ranking — the operator-approved pick order
# --------------------------------------------------------------------------- #
def rank_key(candidate: dict) -> tuple:
    """Sort key for candidates (ascending; first = best). Encodes the
    operator-approved ordering (2026-08-05): hard gates absolutely first,
    then soft-gate failures, then target shortfall, then the emphasis score.
    Revenue enters through the card display (and later as a scored component
    once real price data is in economics.yaml), not the pick order."""
    gates = candidate.get("gates") or []
    hard_fails = sum(1 for g in gates if g["hard"] and g["status"] == "FAIL")
    soft_fails = sum(1 for g in gates if not g["hard"] and g["status"] == "FAIL")
    warns = sum(1 for g in gates if g["status"] == "WARN")
    tr = candidate.get("targets_review") or {}
    shortfall_kg = float(tr.get("total_shortfall_kg") or 0.0)
    score = float(candidate.get("score") if candidate.get("score") is not None
                  else 1e9)
    return (hard_fails, soft_fails, warns, round(shortfall_kg), score)
