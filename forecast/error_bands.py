"""Confidence bands on a forecast, from measured history.

ONE definition of the band arithmetic, shared by the app and
tools/forecast_bands.py, so a number on screen and a number on the command
line can never disagree.

WHAT A BAND MEANS HERE
----------------------
`tools/backtest.py` replays the model from each historical Production Report
and grades it against what actually happened; `tools/error_model.py` reduces
that to a per-horizon distribution of the model's AVERAGE-WEIGHT error. This
module projects that distribution onto a plan: given that a 2-month-out
forecast has historically landed within this range, where does this month's
tonnage land?

THE INVERSION — the easy thing to get backwards
-----------------------------------------------
The error is (predicted - actual) / actual, so

    actual = predicted / (1 + err)

A HIGH error means the model ran HOT, which means the ACTUAL comes in LOW. So
the p90 ERROR produces the LOW edge of the tonnage band and p10 produces the
HIGH edge: the bounds cross over. `apply_band` is the only place this is
computed.

WHAT THE BAND COVERS, AND WHAT IT DOES NOT
------------------------------------------
COVERS: biological uncertainty in average weight — the only part of a forecast
the backtest can grade against reality.

DOES NOT COVER: execution. Harvest COUNT is a plan, not a prediction. If the
facility harvests a different number of fish than planned, tonnage moves for
reasons no growth model can foresee. Every caller must say so; a band read as
covering everything is worse than no band.

HORIZON LIMIT: only horizons the model rates sound are returned. `weak` rows
(too few graded comparisons) and horizons past QUOTABLE_MAX_MONTHS return None
rather than a number — refusing to look confident is the point.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Optional

# Operator ruling 2026-08-21: 1-3 months are trustworthy. Beyond that the
# measurement still carries harvest-execution contamination, so those months
# are shown as plan-only rather than banded.
QUOTABLE_MAX_MONTHS = 3

# Searched in order, first hit wins. The committed default lives beside the
# corpus it was derived from.
_DEFAULT_PATHS = (
    "pr_corpus/error_model.json",
    "backtest_v2/error_model.json",
    "backtest/error_model.json",
)


def load_error_model(root: Path | str, path: Optional[str] = None) -> Optional[dict]:
    """The error model, or None when there isn't one.

    Absence is normal — a clone that has never run a backtest has no model —
    and every caller must degrade to plan-only rather than inventing a band.
    """
    root = Path(root)
    candidates = [path] if path else list(_DEFAULT_PATHS)
    for c in candidates:
        if not c:
            continue
        p = Path(c)
        if not p.is_absolute():
            p = root / p
        if p.is_file():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(d, dict) and d.get("horizons_months"):
                d["_source"] = str(p)
                return d
    return None


def months_between(a: dt.date, b: dt.date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def band_for_horizon(model: Optional[dict], horizon: int,
                     max_months: int = QUOTABLE_MAX_MONTHS) -> Optional[dict]:
    """{median_pct, p10_pct, p90_pct, n} for a horizon, or None if not quotable.

    None means "do not show a band" — no model, horizon outside the quotable
    window, or too few graded comparisons at that horizon for a spread to mean
    anything.
    """
    if not model or horizon < 1 or horizon > max_months:
        return None
    h = (model.get("horizons_months") or {}).get(str(horizon))
    if not h or h.get("weak"):
        return None
    if h.get("p10_pct") is None or h.get("p90_pct") is None:
        return None
    return {"median_pct": h.get("median_signed_pct"),
            "p10_pct": h["p10_pct"], "p90_pct": h["p90_pct"],
            "n": h.get("n"), "typical_abs_pct": h.get("typical_abs_pct")}


def apply_band(value: float, band: Optional[dict]) -> Optional[tuple]:
    """(low, high) for `value` under `band`, or None.

    THE ONLY PLACE THE INVERSION LIVES. actual = predicted / (1 + err), so the
    p90 error gives the LOW edge and p10 the HIGH edge — the bounds cross.
    """
    if not band or value is None:
        return None
    lo_den = 1.0 + float(band["p90_pct"]) / 100.0
    hi_den = 1.0 + float(band["p10_pct"]) / 100.0
    if lo_den <= 0 or hi_den <= 0:          # a >100% error would flip the sign
        return None
    low = value / lo_den
    high = value / hi_den
    return (low, high) if low <= high else (high, low)


def describe(model: Optional[dict]) -> str:
    """One line naming what the band is built from, for the caller to show."""
    if not model:
        return ("No measured error model found — months are shown as planned, "
                "with no confidence band.")
    used = model.get("batches_used")
    excl = model.get("batches_excluded_exec_confounded")
    return (f"Band from {used:,} graded batch readings of past forecasts vs "
            f"what actually happened ({excl:,} excluded where a harvest made "
            f"the comparison unfair).")
