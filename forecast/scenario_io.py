"""Scenario config I/O for the app-managed tier (Phase 2 of the data-path
inversion — see docs/DATA_PATH_REDESIGN.md).

The per-scenario inputs the operator tunes — the **forward batch schedule**
and the **facility / system limits** — move out of the workbook into editable
YAML so a run needs only the ProductionReport (current state) plus the app's
config + scenario.

  batches.yaml -> list[BatchInput]   (batch metadata + forward stocking plan)
  limits.yaml  -> FacilityLimits + SystemLimits

`dump_scenario` serializes exactly what the Excel readers produce, so the YAML
is seeded faithfully from the workbook and round-trips bit-for-bit.

Limits are keyed by absolute ISO week label (e.g. "2026-W23"), which is stable
as long as forecast_start (= ProductionReport closing + 1) is stable for the
scenario. When limits become app-authored, the operator edits these weeks
directly.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml

from .caps import FacilityLimits, SystemLimits
from .models import BatchInput

BATCHES_FILE = "batches.yaml"
LIMITS_FILE = "limits.yaml"


def _iso(d):
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date().isoformat()
    if hasattr(d, "isoformat"):
        return d.isoformat()
    return str(d)


def _from_iso(s):
    if s is None or s == "":
        return None
    if isinstance(s, datetime):
        return s
    if hasattr(s, "year") and not isinstance(s, str):  # date
        return datetime(s.year, s.month, s.day)
    return datetime.fromisoformat(str(s))


# ---------- Batches (forward stocking plan + metadata) ----------

def batches_to_list(batches: list[BatchInput]) -> list[dict]:
    out: list[dict] = []
    for b in batches:
        out.append({
            "batch_id": b.batch_id,
            "input_date": _iso(b.input_date),
            "input_count": b.input_count,
            "tran_sf_date": _iso(b.tran_sf_date),
            "tran_og_date": _iso(b.tran_og_date),
            "tran_og_count": b.tran_og_count,
            "tran_og_avg_wt_g": b.tran_og_avg_wt_g,
            "tran_og_cv": b.tran_og_cv,
            "fcr_model": b.fcr_model,
            "fw_correction": b.fw_correction,
            "sgr_correction": b.sgr_correction,
            "notes": b.notes,
        })
    return out


def batches_from_list(data: list[dict]) -> list[BatchInput]:
    out: list[BatchInput] = []
    for d in data or []:
        tog_count = d.get("tran_og_count")
        tog_wt = d.get("tran_og_avg_wt_g")
        out.append(BatchInput(
            batch_id=str(d["batch_id"]),
            input_date=_from_iso(d.get("input_date")),
            input_count=int(d.get("input_count") or 0),
            tran_sf_date=_from_iso(d.get("tran_sf_date")),
            tran_og_date=_from_iso(d.get("tran_og_date")),
            tran_og_count=int(tog_count) if tog_count is not None else None,
            tran_og_avg_wt_g=float(tog_wt) if tog_wt is not None else None,
            tran_og_cv=float(d.get("tran_og_cv") if d.get("tran_og_cv") is not None else 16.0),
            fcr_model=str(d.get("fcr_model") or ""),
            fw_correction=float(d.get("fw_correction") if d.get("fw_correction") is not None else 1.0),
            sgr_correction=float(d.get("sgr_correction") if d.get("sgr_correction") is not None else 1.0),
            notes=str(d.get("notes") or ""),
        ))
    return out


# ---------- Limits ----------

def facility_limits_to_list(fl: FacilityLimits) -> list[dict]:
    return [
        {"week": wk, "metric": m, "value": v}
        for (wk, m), v in sorted(fl.overrides.items())
    ]


def facility_limits_from_list(data: list[dict]) -> FacilityLimits:
    return FacilityLimits(overrides={
        (str(r["week"]), str(r["metric"])): float(r["value"])
        for r in data or []
    })


def system_limits_to_list(sl: SystemLimits) -> list[dict]:
    return [
        {"week": wk, "system": s, "metric": m, "value": v}
        for (wk, s, m), v in sorted(sl.caps.items())
    ]


def system_limits_from_list(data: list[dict]) -> SystemLimits:
    return SystemLimits(caps={
        (str(r["week"]), str(r["system"]), str(r["metric"])): float(r["value"])
        for r in data or []
    })


# ---------- Top-level dump / load ----------

def dump_scenario(
    scenario_dir,
    *,
    batches: list[BatchInput],
    facility_limits: FacilityLimits,
    system_limits: SystemLimits,
) -> None:
    """Write batches.yaml + limits.yaml into `scenario_dir`."""
    d = Path(scenario_dir)
    d.mkdir(parents=True, exist_ok=True)

    with (d / BATCHES_FILE).open("w", encoding="utf-8") as fh:
        fh.write("# Forward batch schedule + batch metadata (input/TranOG dates,\n"
                 "# counts, FCR model, corrections). In-flight state comes from the\n"
                 "# ProductionReport; this is the planning/metadata layer.\n")
        yaml.safe_dump({"batches": batches_to_list(batches)}, fh,
                       sort_keys=False, allow_unicode=True, default_flow_style=False)

    with (d / LIMITS_FILE).open("w", encoding="utf-8") as fh:
        fh.write("# Per-week caps. facility: one row per (week, metric); system:\n"
                 "# one row per (week, system, metric). Weeks are absolute ISO\n"
                 "# labels. Blank/absent = use Control default (facility) / no cap.\n")
        yaml.safe_dump({
            "facility": facility_limits_to_list(facility_limits),
            "system": system_limits_to_list(system_limits),
        }, fh, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _load_yaml(path) -> dict:
    with Path(path).open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_batches(scenario_dir) -> list[BatchInput]:
    return batches_from_list(_load_yaml(Path(scenario_dir) / BATCHES_FILE).get("batches", []))


def load_limits(scenario_dir) -> tuple[FacilityLimits, SystemLimits]:
    d = _load_yaml(Path(scenario_dir) / LIMITS_FILE)
    return (
        facility_limits_from_list(d.get("facility", [])),
        system_limits_from_list(d.get("system", [])),
    )
