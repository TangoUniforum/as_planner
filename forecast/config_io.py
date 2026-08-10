"""YAML config I/O for the stable-config tier (Phase 1 of the data-path
inversion — see docs/DATA_PATH_REDESIGN.md).

The stable models live in the app as human-editable YAML instead of being
read from the workbook every run:
  - control.yaml   -> ControlParams (caps defaults, horizon, knobs)
  - biology.yaml   -> BiologyTables (SGR / FCR / mortality / feed / culling)
  - facility.yaml  -> FacilityConfig (tanks)

`dump_config` serializes the dataclasses the Excel readers already produce,
so the YAML is seeded from the real workbook (round-trip faithful). The
loaders rebuild the *same* dataclasses, so the compute core is untouched and
the regression baseline must be preserved bit-for-bit.

forecast_start is intentionally NOT authoritative here — it is derived from
the ProductionReport at run time (see forecast/run.py). The value written to
control.yaml is a harmless seed that the derivation overwrites.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from .models import BiologyTables, ControlParams, FacilityConfig, TankConfig
from .yaml_atomic import read_text_resilient, write_text_atomic

CONTROL_FILE = "control.yaml"
BIOLOGY_FILE = "biology.yaml"
FACILITY_FILE = "facility.yaml"


# ---------- datetime helpers ----------

def _iso(d) -> Optional[str]:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date().isoformat()
    if hasattr(d, "isoformat"):
        return d.isoformat()
    return str(d)


def _from_iso(s) -> Optional[datetime]:
    if s is None or s == "":
        return None
    if isinstance(s, datetime):
        return s
    if hasattr(s, "year") and not isinstance(s, str):  # date
        return datetime(s.year, s.month, s.day)
    return datetime.fromisoformat(str(s))


# ---------- Control ----------

def control_to_dict(c: ControlParams) -> dict:
    d = asdict(c)
    d["forecast_start"] = _iso(c.forecast_start)
    d["sixn_production_start"] = _iso(c.sixn_production_start)
    return d


def _coerce_control_value(key: str, raw, type_str: str):
    """Coerce a control value to its DECLARED dataclass type.

    Dataclasses do not validate types, so a numeric knob arriving as a string
    (`"5"`) is stored as a string and written straight back to YAML quoted —
    then blows up much later in arithmetic, far from the cause. Two real ways in:
    the app's Control editor picks its widget from the CURRENT value, so a knob
    that is presently null (harvest_level_target, sixn_transition_weeks) renders
    as a text box; and a hand-edited YAML can quote a number.

    Blank means "unset": allowed only where the declared type is Optional.
    Anywhere else it is a real error, and a loud one here beats a TypeError deep
    in the engine.
    """
    t = (type_str or "").replace(" ", "")
    optional = "Optional" in t or "None" in t
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if optional or not t:
            return None
        raise ValueError(
            f"control knob {key!r} is required and cannot be blank "
            f"(declared {type_str})"
        )
    try:
        if "bool" in t:
            if isinstance(raw, str):
                return raw.strip().lower() in ("true", "1", "yes", "on")
            return bool(raw)
        if "float" in t:
            return float(raw)
        if "int" in t:
            return int(float(raw))          # tolerate "5" and "5.0" alike
    except (TypeError, ValueError):
        raise ValueError(
            f"control knob {key!r} expects {type_str}, got {raw!r}"
        ) from None
    return raw


def control_from_dict(d: dict) -> ControlParams:
    d = dict(d)
    d["forecast_start"] = _from_iso(d.get("forecast_start"))
    d["sixn_production_start"] = _from_iso(d.get("sixn_production_start"))
    # Only pass keys ControlParams accepts (tolerate extra/missing YAML keys).
    fields = ControlParams.__dataclass_fields__
    # MIGRATION (2026-08-09): harvest_target_per_week was removed — the weekly
    # harvest is demand-driven and CAPPED at max_harvest_per_week; the stretch
    # allowance is now the relief band (harvest_relief_pct). Old YAMLs that
    # still carry the key load fine; the value is ignored, loudly.
    if "harvest_target_per_week" in d:
        print("NOTE: control config carries the removed knob "
              "'harvest_target_per_week' — ignored. The weekly harvest plans "
              "to demand capped at max_harvest_per_week; the relief band is "
              "harvest_relief_pct (ceiling = max * (1 + relief)).")
    kwargs = {k: v for k, v in d.items() if k in fields}
    # Coerce to declared types. models.py uses `from __future__ import
    # annotations`, so field.type is the annotation SOURCE TEXT, not a type
    # object — matched as a string. datetime/str fields fall through untouched.
    for k, v in list(kwargs.items()):
        if k in ("forecast_start", "sixn_production_start"):
            continue                        # already datetime via _from_iso
        kwargs[k] = _coerce_control_value(k, v, str(fields[k].type))
    return ControlParams(**kwargs)


# ---------- Biology ----------

def biology_to_dict(t: BiologyTables) -> dict:
    return {
        "sgr_size_g": list(t.sgr_size_g),
        "sgr_fw_pct_day": list(t.sgr_fw_pct_day),
        "sgr_sw_pct_day": list(t.sgr_sw_pct_day),
        "fcr_size_g": list(t.fcr_size_g),
        "fcr_by_model": {k: list(v) for k, v in t.fcr_by_model.items()},
        "mortality_week_from_input": list(t.mortality_week_from_input),
        "mortality_pct_weekly": list(t.mortality_pct_weekly),
        # tuples -> lists for clean YAML; restored on load.
        "feed_types": [[mx, name] for (mx, name) in t.feed_types],
        "culling": [[dsi, pct] for (dsi, pct) in t.culling],
    }


def biology_from_dict(d: dict) -> BiologyTables:
    return BiologyTables(
        sgr_size_g=[float(x) for x in d.get("sgr_size_g", [])],
        sgr_fw_pct_day=[None if x is None else float(x) for x in d.get("sgr_fw_pct_day", [])],
        sgr_sw_pct_day=[None if x is None else float(x) for x in d.get("sgr_sw_pct_day", [])],
        fcr_size_g=[float(x) for x in d.get("fcr_size_g", [])],
        fcr_by_model={k: [float(x) for x in v] for k, v in d.get("fcr_by_model", {}).items()},
        mortality_week_from_input=[int(x) for x in d.get("mortality_week_from_input", [])],
        mortality_pct_weekly=[float(x) for x in d.get("mortality_pct_weekly", [])],
        feed_types=[(float(mx), str(name)) for mx, name in d.get("feed_types", [])],
        culling=[(int(dsi), float(pct)) for dsi, pct in d.get("culling", [])],
    )


# ---------- Facility ----------

def facility_to_dict(f: FacilityConfig) -> dict:
    return {"tanks": [asdict(t) for t in f.tanks]}


def facility_from_dict(d: dict) -> FacilityConfig:
    fields = TankConfig.__dataclass_fields__
    tanks = [TankConfig(**{k: v for k, v in row.items() if k in fields})
             for row in d.get("tanks", [])]
    return FacilityConfig(tanks=tanks)


# ---------- Top-level dump / load ----------

def dump_config(
    config_dir,
    *,
    control: ControlParams,
    tables: BiologyTables,
    facility: FacilityConfig,
) -> None:
    """Write the three stable-config YAML files into `config_dir`."""
    d = Path(config_dir)
    d.mkdir(parents=True, exist_ok=True)

    def _write(name, obj, header):
        text = header + yaml.safe_dump(
            obj, sort_keys=False, allow_unicode=True, default_flow_style=False)
        write_text_atomic(d / name, text)

    _write(CONTROL_FILE, control_to_dict(control),
           "# Control parameters (caps defaults, horizon, planner knobs).\n"
           "# forecast_start is derived from the ProductionReport at run time;\n"
           "# the value here is only a seed.\n")
    _write(BIOLOGY_FILE, biology_to_dict(tables),
           "# Biology models: SGR (FW/SW), FCR curves, mortality, feed types,\n"
           "# culling schedule. Edit to add/adjust models.\n")
    _write(FACILITY_FILE, facility_to_dict(facility),
           "# Facility definition: tanks (system, stage, volume, caps, type).\n")


def _load_yaml(path) -> dict:
    return yaml.safe_load(read_text_resilient(path)) or {}


def load_control(config_dir) -> ControlParams:
    return control_from_dict(_load_yaml(Path(config_dir) / CONTROL_FILE))


def load_biology_tables(config_dir) -> BiologyTables:
    return biology_from_dict(_load_yaml(Path(config_dir) / BIOLOGY_FILE))


def load_facility_config(config_dir) -> FacilityConfig:
    return facility_from_dict(_load_yaml(Path(config_dir) / FACILITY_FILE))


def load_config(config_dir) -> tuple[ControlParams, BiologyTables, FacilityConfig]:
    """Load all three stable-config dataclasses from `config_dir`."""
    return (
        load_control(config_dir),
        load_biology_tables(config_dir),
        load_facility_config(config_dir),
    )
