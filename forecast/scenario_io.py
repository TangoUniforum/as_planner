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

System capacities are stated once per system in `system_defaults`, with
per-week rows kept as one-off EXCEPTIONS (keyed by absolute ISO week label,
e.g. "2026-W23"). The row-per-week form was the original schema and still
loads: it made the horizon's coverage a function of when the file was
generated, so a ProductionReport that moved the horizon left weeks at the
end with no cap at all — invisibly. A default covers every week there is.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml

from .caps import FacilityLimits, SystemLimits
from .models import BatchInput
from .yaml_atomic import read_text_resilient, write_text_atomic

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
    import re as _re
    out: list[BatchInput] = []
    for d in data or []:
        tog_count = d.get("tran_og_count")
        tog_wt = d.get("tran_og_avg_wt_g")
        _fcr = str(d.get("fcr_model") or "")
        if _fcr and not _re.search(r"\d{2,3}", _fcr):
            # biology._fcr_model_key silently falls back to "1.18" for a
            # string it can't parse — a typo'd model must be named at LOAD
            # time, not quietly change the feed math.
            print(f"WARN: batch {d.get('batch_id')}: fcr_model {_fcr!r} has "
                  f"no parseable FCR digits — biology will use the 1.18 "
                  f"fallback")
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
    """The per-week EXCEPTION rows only (`system:` in the YAML)."""
    return [
        {"week": wk, "system": s, "metric": m, "value": v}
        for (wk, s, m), v in sorted(sl.caps.items())
    ]


def system_limits_from_list(data: list[dict]) -> SystemLimits:
    return SystemLimits(caps={
        (str(r["week"]), str(r["system"]), str(r["metric"])): float(r["value"])
        for r in data or []
    })


# ---------- System capacity defaults (the `system_defaults:` block) ----------
#
# Shape — one block per system, the whole capacity of that system in one
# place, with an optional `modes:` sub-block for a system whose capacity
# depends on its operating mode:
#
#   system_defaults:
#     OG1N:
#       biomass: 400000.0
#       feed_per_day: 3000.0
#     OG6N:
#       feed_per_day: 3000.0
#       modes:
#         purge:      {biomass: 700000.0}
#         production: {biomass: 400000.0}
#
# `modes` is the one reserved key inside a system block; every other key is
# a metric name (see caps.METRIC_*).

MODES_KEY = "modes"


def system_defaults_to_dict(sl: SystemLimits) -> dict:
    """Serialize `defaults` + `mode_defaults` into the nested block above."""
    out: dict[str, dict] = {}
    for (sys_id, metric), v in sorted(sl.defaults.items()):
        out.setdefault(sys_id, {})[metric] = v
    for (sys_id, mode, metric), v in sorted(sl.mode_defaults.items()):
        modes = out.setdefault(sys_id, {}).setdefault(MODES_KEY, {})
        modes.setdefault(mode, {})[metric] = v
    # `modes` last within each system so the plain metrics read first.
    for sys_id, block in out.items():
        if MODES_KEY in block:
            block[MODES_KEY] = block.pop(MODES_KEY)
    return out


def system_defaults_from_dict(data: dict) -> tuple[dict, dict]:
    """Parse the block into (defaults, mode_defaults) keyed as SystemLimits."""
    defaults: dict[tuple[str, str], float] = {}
    mode_defaults: dict[tuple[str, str, str], float] = {}
    for sys_id, block in (data or {}).items():
        for key, val in (block or {}).items():
            if key == MODES_KEY:
                for mode, metrics in (val or {}).items():
                    for metric, v in (metrics or {}).items():
                        if v is None:
                            continue
                        mode_defaults[(str(sys_id), str(mode), str(metric))] = float(v)
            elif val is not None:
                defaults[(str(sys_id), str(key))] = float(val)
    return defaults, mode_defaults


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

    batches_text = (
        "# Forward batch schedule + batch metadata (input/TranOG dates,\n"
        "# counts, FCR model, corrections). In-flight state comes from the\n"
        "# ProductionReport; this is the planning/metadata layer.\n"
        + yaml.safe_dump({"batches": batches_to_list(batches)},
                         sort_keys=False, allow_unicode=True,
                         default_flow_style=False)
    )
    write_text_atomic(d / BATCHES_FILE, batches_text)

    write_text_atomic(d / LIMITS_FILE, limits_yaml_text(facility_limits,
                                                        system_limits))


_LIMITS_HEADER = """\
# Capacity limits — OPERATOR INPUT. Capacities live here, never in code.
#
# A capacity is a fact about the facility: it changes rarely, so it is
# stated ONCE and a per-week row is the exception, not the rule.
#
#   system_defaults: the standing capacity of each system. One block per
#       system; each key is a metric (biomass = kg of standing fish;
#       feed_per_day = kg of feed per day). A system whose capacity depends
#       on its operating MODE adds a `modes:` sub-block — 6N is the one that
#       does, because it holds more while it is the depuration station than
#       it does from sixn_production_start (config/control.yaml), when its
#       3 mains become grow-out and only the 3 sisters stage harvest. Which
#       weeks are which is DERIVED from that date, so the split cannot drift
#       away from it.
#   system: one-off EXCEPTION rows, one per (week, system, metric). Absent
#       is the normal case. A row here overrides the default for that week
#       alone.
#   facility: one row per (week, metric); absent = use the Control default.
#
# Precedence, highest first:
#   per-week `system` row  >  system+mode default  >  system default  >
#   no cap at all (an engine that needs one then names the missing input
#   rather than inventing a number).
#
# Weeks are absolute ISO labels (e.g. 2028-W01).
"""


def limits_yaml_text(facility_limits: FacilityLimits,
                     system_limits: SystemLimits) -> str:
    """The full text of limits.yaml — header plus the three blocks.

    Written here rather than by editing the file in place: the header
    documents the schema below it, so the two are generated together and a
    save from the app cannot leave the rationale behind (it did once —
    dump_scenario's old three-line header silently replaced a longer
    hand-written one).
    """
    return _LIMITS_HEADER + yaml.safe_dump({
        "system_defaults": system_defaults_to_dict(system_limits),
        "system": system_limits_to_list(system_limits),
        "facility": facility_limits_to_list(facility_limits),
    }, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _load_yaml(path) -> dict:
    return yaml.safe_load(read_text_resilient(path)) or {}


def load_batches(scenario_dir) -> list[BatchInput]:
    return batches_from_list(_load_yaml(Path(scenario_dir) / BATCHES_FILE).get("batches", []))


def load_limits(scenario_dir, control=None) -> tuple[FacilityLimits, SystemLimits]:
    """Read limits.yaml into (FacilityLimits, SystemLimits).

    Pass `control` whenever the result will be RESOLVED (i.e. anywhere a
    forecast runs). Mode-specific defaults need Control's `sixn_growth` +
    `sixn_production_start` to know which mode a week is in; resolving
    without them raises rather than guessing. Editors that only display or
    rewrite the file may omit it.
    """
    d = _load_yaml(Path(scenario_dir) / LIMITS_FILE)
    defaults, mode_defaults = system_defaults_from_dict(d.get("system_defaults") or {})
    sl = system_limits_from_list(d.get("system", []))
    sl.defaults = defaults
    sl.mode_defaults = mode_defaults
    if control is not None:
        sl.bind_sixn_mode(control)
    return (facility_limits_from_list(d.get("facility", [])), sl)
