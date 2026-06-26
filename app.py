"""Streamlit UI for the AS Production Forecast tool.

Local-only single-operator workflow:
  1. Upload an input workbook (Forecast.xlsm).
  2. Click "Run forecast" → pipeline runs in-memory; input is never mutated.
  3. Review summary KPIs + Advisory + tank-occupancy heatmap.
  4. Download the output workbook with all populated tabs.

Run with:  streamlit run app.py
"""
from __future__ import annotations

import io
import sys
import tempfile
import time
import traceback
from collections import defaultdict
from contextlib import redirect_stdout
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from openpyxl import load_workbook

# Local imports — adjust path so this file works whether streamlit is
# launched from the project root or elsewhere.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from forecast.run import main as run_pipeline  # noqa: E402
from forecast import tuning  # noqa: E402
from forecast import optimize  # noqa: E402

# App-managed config (Phase 1) + scenario (Phase 2) live here. In PR-only
# mode the app reads these instead of pulling everything from the upload;
# the uploaded workbook then supplies only the ProductionReport.
_ROOT = Path(__file__).resolve().parent
CONFIG_DIR = _ROOT / "config"
SCENARIO_DIR = _ROOT / "scenario"


def _config_ready() -> bool:
    return (CONFIG_DIR / "control.yaml").exists()


def _scenario_ready() -> bool:
    return (SCENARIO_DIR / "batches.yaml").exists()


def _active_config_summary(cd: dict) -> list[tuple]:
    """Plain-language (label, value, what-it-does) for the settings that actually
    shape a run — so the operator can always see WHAT the app is doing. Reads a
    control dict (from the live config, or a run's RunConfig)."""
    def g(k, default=None):
        return cd.get(k, default)
    lv, hl = bool(g("rebalance_level")), bool(g("harvest_level_load"))
    pm = str(g("placement_method", "greedy"))
    dt = g("density_target_pct", 0) or 0
    return [
        ("Feed leveling", "ON" if lv else "OFF",
         "spreads load off the hottest system → no per-system feed spikes"
         if lv else "density-only → per-system feed can spike"),
        ("Harvest smoother", "ON" if hl else "OFF",
         "holds the weekly harvest cap hard + pre-harvests → flat harvest"
         if hl else "reactive → harvest can be lumpy"),
        ("TranOG tanks / arrival", g("tran_og_default_tanks"),
         "more tanks = lower per-system feed but a tighter facility (spikier harvest)"),
        ("Harvest setpoint", f"{g('harvest_setpoint_lookahead_weeks')} wk",
         "holds biomass ~this many weeks of growth below the cap (higher = safer, "
         "lower utilisation)"),
        ("Density target", f"{dt * 100:.0f}%",
         "packs each tank to this % of its density cap (lower = more headroom, "
         "fewer hot spots)"),
        ("Rebalancer budget", f"{g('rebalance_balance_budget')} moves/wk",
         "max fish-moves per week to relieve over-cap systems"),
        ("Placement engine", pm.upper(),
         "greedy heuristic + rebalancer (default)" if pm == "greedy"
         else "LP-guided LNS optimal-layout refinement"),
        ("Caps", f"biomass {g('max_biomass_kg', 0):,.0f} kg · "
                 f"feed {g('max_feed_per_day_kg', 0):,.0f} kg/day · "
                 f"harvest {g('max_harvest_per_week', 0):,.0f} fish/wk",
         "the hard limits the plan must respect"),
    ]


def _render_active_config(cd: dict, title: str):
    """Render the active-config summary as a collapsible panel (informs without
    clutter)."""
    import streamlit as _st
    with _st.expander(title, expanded=False):
        if not cd:
            _st.caption("No config loaded yet.")
            return
        for label, value, effect in _active_config_summary(cd):
            _st.markdown(f"**{label}: {value}** — {effect}")


def _ingest_pr(uploaded):
    """Parse + validate the uploaded ProductionReport (cached by file identity).

    The PR is the anchor: forecast_start is DERIVED from its closing date
    (+1 day). Returns dict(ok, forecast_start, closing, n_og, n_fw, errors,
    warnings). `ok` is False (locks downstream actions) on any hard error.
    """
    key = (uploaded.name, uploaded.size)
    if st.session_state.get("_pr_key") == key:
        return st.session_state["_pr"]
    from datetime import datetime as _dt, timedelta as _td
    from forecast.excel_io import load_workbook
    from forecast.production_report import read_production_report
    res = {"ok": False, "forecast_start": None, "closing": None,
           "n_og": 0, "n_fw": 0, "errors": [], "warnings": []}
    try:
        wd = Path(tempfile.mkdtemp(prefix="as_pr_"))
        p = wd / uploaded.name
        p.write_bytes(uploaded.getvalue())
        wb = load_workbook(p)
        if "ProductionReport" not in wb.sheetnames:
            res["errors"].append("No 'ProductionReport' sheet in the workbook.")
        else:
            closing, og, fw = read_production_report(wb)
            res["closing"], res["n_og"], res["n_fw"] = closing, len(og), len(fw)
            if closing is None:
                res["errors"].append(
                    "ProductionReport has no parseable 'Closing Month' date.")
            else:
                res["forecast_start"] = _dt(closing.year, closing.month,
                                            closing.day) + _td(days=1)
            if not og and not fw:
                res["errors"].append(
                    "ProductionReport has no tank rows (no OG/FW records).")
            if (og or fw) and _config_ready() and _scenario_ready():
                try:
                    from forecast.config_io import load_facility_config
                    from forecast.scenario_io import load_batches
                    fac_ids = {t.tank_id for t in load_facility_config(CONFIG_DIR).tanks}
                    batch_ids = {b.batch_id for b in load_batches(SCENARIO_DIR)}
                    pr_b = {r.batch_id for r in og} | {r.batch_id for r in fw}
                    miss_b = sorted(pr_b - batch_ids)
                    if miss_b:
                        res["warnings"].append(
                            f"PR batches not in config Batches {miss_b} — "
                            f"their fish would be dropped.")
                    unk_t = sorted({r.tank_id for r in og} - fac_ids)
                    if unk_t:
                        res["warnings"].append(
                            f"PR tank ids not in Facility config: {unk_t}.")
                except Exception:  # noqa: BLE001
                    pass
        wb.close()
        res["ok"] = not res["errors"]
    except Exception as e:  # noqa: BLE001
        res["errors"].append(f"Failed to read the workbook: {e}")
    st.session_state["_pr_key"] = key
    st.session_state["_pr"] = res
    return res


# ============================================================
# Mother ship — in-app config + scenario editor
# ============================================================
# Edit the app's stable config (biology/facility/control) and scenario
# (forward batches + limits) directly, saved back to the YAML the engine
# runs from. This is what makes the models + control points live in the
# app instead of a workbook. Round-trip converters are kept pure (no
# Streamlit) so they can be unit-tested.

def _records(df):
    """DataFrame -> list[dict] with NATIVE python types (data_editor returns
    numpy scalars / NaN which yaml.safe_dump can't serialize). JSON round-trip
    coerces numpy->native and NaN->None."""
    import json
    return json.loads(df.to_json(orient="records"))


def _persist(key, loader):
    """Load a DataFrame into session_state ONCE and return it.

    Streamlit reruns the whole script on every click; loading from YAML each
    rerun would discard in-progress edits. Holding the base in session_state
    (constant) + a keyed data_editor (key=`<key>_w`) keeps edits across reruns
    and Run/Configure mode switches until the user explicitly Saves or Reloads.
    """
    if key not in st.session_state:
        st.session_state[key] = loader()
    return st.session_state[key]


def _reset_keys(*keys):
    """Drop a working df + its data_editor widget state so it reloads fresh."""
    for k in keys:
        st.session_state.pop(k, None)
        st.session_state.pop(k + "_w", None)


def _clear_all_editor_state():
    """Drop every editor's cached working copy so they reload from disk.

    Used after an import (which rewrites the YAML out from under the open
    editors) so the tabs reflect the freshly-imported config, not stale
    session_state from before the import.
    """
    _reset_keys("bio_growth", "bio_mort", "bio_feed", "bio_cull",
                "fac_df", "batch_df", "flim_wide", "slim_wide")
    for k in ("bio_models", "_lim_weeks", "_tmpl_bytes", "_tmpl_fp"):
        st.session_state.pop(k, None)


def _biology_to_frames(tables):
    """BiologyTables -> (growth_df, mort_df, feed_df, cull_df, model_keys)."""
    n = len(tables.sgr_size_g)
    models = sorted(tables.fcr_by_model.keys())
    growth = []
    for i in range(n):
        row = {"size_g": tables.sgr_size_g[i],
               "SGR_FW": tables.sgr_fw_pct_day[i],
               "SGR_SW": tables.sgr_sw_pct_day[i]}
        for m in models:
            col = tables.fcr_by_model.get(m, [])
            row[f"FCR_{m}"] = col[i] if i < len(col) else None
        growth.append(row)
    growth_df = pd.DataFrame(growth)
    mort_df = pd.DataFrame({"week_from_input": tables.mortality_week_from_input,
                            "mortality_pct": tables.mortality_pct_weekly})
    feed_df = pd.DataFrame([{"max_size_g": mx, "feed_name": nm}
                            for mx, nm in tables.feed_types])
    cull_df = pd.DataFrame([{"days_since_input": d, "cull_pct": p}
                            for d, p in tables.culling])
    return growth_df, mort_df, feed_df, cull_df, models


def _frames_to_biology(growth_df, mort_df, feed_df, cull_df, models):
    """Inverse of _biology_to_frames -> BiologyTables."""
    from forecast.models import BiologyTables
    g = growth_df.dropna(subset=["size_g"])
    sizes = [float(x) for x in g["size_g"]]

    def _col(df, name):
        return [None if pd.isna(x) else float(x) for x in df[name]]

    md = mort_df.dropna(subset=["week_from_input"])
    fd = feed_df.dropna(subset=["max_size_g"]) if len(feed_df) else feed_df
    cd = cull_df.dropna(subset=["days_since_input"]) if len(cull_df) else cull_df
    return BiologyTables(
        sgr_size_g=sizes,
        sgr_fw_pct_day=_col(g, "SGR_FW"),
        sgr_sw_pct_day=_col(g, "SGR_SW"),
        fcr_size_g=list(sizes),
        fcr_by_model={
            m: [float("nan") if pd.isna(x) else float(x)
                for x in g[f"FCR_{m}"]]
            for m in models if f"FCR_{m}" in g.columns
        },
        mortality_week_from_input=[int(x) for x in md["week_from_input"]],
        mortality_pct_weekly=[0.0 if pd.isna(x) else float(x)
                              for x in md["mortality_pct"]],
        feed_types=[(float(mx), str(nm))
                    for mx, nm in zip(fd["max_size_g"], fd["feed_name"])],
        culling=[(int(d), float(p))
                 for d, p in zip(cd["days_since_input"], cd["cull_pct"])],
    )


# Per-parameter explanations shown as the hover-"?" tooltip on each Control
# widget. Keep in sync with docs/USER_GUIDE.md §3.2 and ControlParams in
# forecast/models.py (the authoritative descriptions).
_CONTROL_HELP = {
    "forecast_start": "Week-0 of the forecast. DERIVED from the ProductionReport's "
        "closing date at run time — the stored value is an ignored seed.",
    "horizon_weeks": "Forecast length, in weeks.",
    "scenario_name": "Label for this run; appears in the reports and the RunConfig "
        "snapshot.",
    "max_feed_per_day_kg": "Facility-wide daily feed ceiling (kg/day). Per-week "
        "overrides come from FacilityLimits.",
    "max_biomass_kg": "Facility biomass cap (kg). This is the default; per-week "
        "overrides in FacilityLimits can raise it (e.g. later years).",
    "max_harvest_per_week": "Weekly harvest / processing ceiling (fish). Enforced as "
        "a HARD cap when harvest_level_load is on.",
    "min_harvest_weight_g": "Minimum weight (g) at which a fish may be harvested.",
    "min_harvest_per_week": "Weekly harvest floor (fish) the controller tries to meet.",
    "min_tank_control": "Force-empty floor (fish): a harvest/transfer that would leave "
        "fewer than this many fish empties the tank instead (invariant INV-5).",
    "default_hog_yield": "Gross→HOG (head-off, gutted) conversion factor. Per-week "
        "overrides in FacilityLimits.",
    "facility_biomass_deviation_pct": "± tolerance band around the biomass cap "
        "(R24). 0.01 = 1%.",
    "handling_mortality_pct": "Mortality fraction applied to fish on each transfer. "
        "0.01 = 1%.",
    "sixn_growth": "Run the OG6N system as a normal grow-out system for the whole "
        "horizon, instead of depuration/purge rotation.",
    "sixn_production_start": "Date OG6N flips from purge to production mode "
        "(ignored if sixn_growth is on).",
    "sixn_transition_weeks": "Empty/fallow window (weeks) at the 6N purge→production "
        "switch. 0 = none.",
    "tran_og_default_tanks": "Minimum number of tanks a new seawater arrival (TranOG) "
        "is spread across — the strongest feed↔harvest lever. More (3, default) spreads "
        "feed thinner (fewer feed-cap breaches) but tightens the facility (bigger "
        "make-room harvest dumps); fewer (2) is the reverse (harvest-friendly, hotter "
        "feed).",
    "global_buffer_pct": "Safety buffer added when sizing against caps. 0.05 = 5%.",
    "starvation_period_days": "In-place purge length (days) in 6N production mode. "
        "7 = one weekly step (clean single-cohort pipeline).",
    "density_target_pct": "Per-tank density target as a fraction of the cap — how full "
        "to pack each tank. 0.9 = fill to 90% of the density cap.",
    "rebalance_varqty_budget": "Variable-quantity rebalancer moves per week: shave a "
        "PRECISE count of fish off an over-cap system. 0 = off (opt-in; small benefit "
        "for the extra transfers).",
    "rebalance_split_budget": "Split-pass moves per week: fan an over-DENSE batch out "
        "across free tanks (one crowded tank → several).",
    "rebalance_balance_budget": "Main multi-objective balancer moves per week: relieve "
        "over-cap tanks into destinations with headroom across density + feed + biomass "
        "at once. Shared with rebalance_level.",
    "rebalance_level": "Load-LEVELING (ON by default). Spreads load off the hottest OG "
        "system onto the coldest, leveling feed + biomass + density together — the fix "
        "for per-system feed spikes. Set false for the old density-only behavior.",
    "harvest_setpoint_lookahead_weeks": "INACTIVE (audit L7): superseded by the "
        "dual-limit setpoint (one facility_biomass_deviation_pct band below the FW-"
        "inclusive cap). No harvest path reads this knob — tuning it has no effect; "
        "kept only for config back-compatibility.",
    "harvest_level_load": "Harvest smoother (ON by default). Holds the weekly harvest "
        "cap as a hard ceiling and pre-harvests earlier so harvest is flat, not a "
        "sawtooth. Pairs with rebalance_level (which otherwise spikes harvest).",
    "harvest_smooth_lookahead_weeks": "Level-load window K: how many weeks of "
        "coming-due biomass to spread the pre-harvest over. Only used when "
        "harvest_level_load is on; bigger = smoother/earlier.",
    "harvest_level_target": "Flat fish/week harvest floor when level-loading. Blank = "
        "auto-computed from realized growth. Only used when harvest_level_load is on.",
    "placement_method": "Tank-placement engine. 'greedy' (default) is the production "
        "engine. 'lns' runs greedy first, then an LNS pass that relocates/swaps grow-out "
        "tank occupancy off the hottest systems onto cooler ones (each move a conserved "
        "Transfer). Every edit is gated on the continuity audit (0 drift) + 0 dropped + a "
        "strictly-lower hot spot, and greedy is the fallback — so it never makes a run "
        "worse. Helps most when the facility has free-tank room; correctly no-ops when "
        "capacity-bound. Adds runtime (a second, audit-checked pass).",
    "lns_max_moves": "LNS budget: the most relocations/swaps the 'lns' placement engine "
        "will make per run (only used when placement_method = lns). Higher = chases more "
        "hot spots but slower.",
}

# Column tooltips for the tabular editors (shown on the column header in the grid).
_FACILITY_HELP = {
    "location_id": "Human label for the tank (free text).",
    "system_id": "System the tank belongs to (e.g. OG3N) — groups tanks for the "
        "per-system feed/biomass caps.",
    "tank_id": "Unique tank number.",
    "volume_m3": "Tank volume (m³). Drives the biomass cap = volume × max_density.",
    "max_density_kg_m3": "Max stocking density (kg/m³). Tank biomass cap = volume × "
        "this (OG tanks are 95).",
    "max_feed_kg_day": "Max feed the tank can deliver per day (kg/day) — the binding "
        "constraint for grow-out fish (OG tanks are 1000).",
    "type": "Tank type: FW (freshwater stages) or OG (seawater grow-out).",
}
_BATCH_HELP = {
    "batch_id": "Unique batch label (e.g. B53).",
    "input_date": "When the fry are stocked into freshwater (YYYY-MM-DD).",
    "input_count": "Number of fry stocked.",
    "tran_sf_date": "Freshwater → start-feed/smolt transition date.",
    "tran_og_date": "Smolt → seawater (TranOG) date — when the batch enters the OG "
        "conveyor and the forecast starts tracking it by tank.",
    "tran_og_count": "Planned number of fish entering seawater.",
    "tran_og_avg_wt_g": "Planned average weight (g) at seawater entry (pre-cull target).",
    "tran_og_cv": "Size-distribution CV (%) at entry — drives the big/small grade split.",
    "fcr_model": "Feed-conversion curve, e.g. FCR_116_Quick → 1.16.",
    "fw_correction": "Multiplier calibrating freshwater growth/survival (tunes the FW "
        "projection so the batch lands on its target entry weight).",
    "sgr_correction": "Multiplier calibrating seawater growth (tunes the SW SGR curve "
        "for this batch).",
    "notes": "Free-text notes.",
}


def _edit_control():
    from forecast.config_io import (
        load_control, load_biology_tables, load_facility_config,
        control_to_dict, control_from_dict, dump_config,
    )
    st.caption("Caps defaults, horizon, and planner knobs. "
               "`forecast_start` is derived from the ProductionReport at "
               "run time — the stored value is an ignored seed.")
    _pr = _ingest_pr(uploaded) if uploaded is not None else None
    _derived = (_pr["forecast_start"].date()
                if (_pr and _pr["ok"] and _pr["forecast_start"]) else None)
    d = control_to_dict(load_control(CONFIG_DIR))
    with st.form("control_form"):
        new = {}
        for k, v in d.items():
            if k == "forecast_start":
                if _derived is not None:
                    st.success(f"forecast_start = **{_derived}** — derived from "
                               f"the uploaded ProductionReport (the stored seed "
                               f"is ignored).")
                else:
                    st.info("forecast_start is derived from the ProductionReport "
                            "at run time — upload a PR to see it. The stored "
                            "value is an ignored seed.")
                new[k] = v
            elif isinstance(v, bool):
                new[k] = st.checkbox(k, value=v, help=_CONTROL_HELP.get(k))
            elif isinstance(v, int):
                new[k] = int(st.number_input(k, value=int(v), step=1,
                                             help=_CONTROL_HELP.get(k)))
            elif isinstance(v, float):
                new[k] = float(st.number_input(k, value=float(v), format="%.5f",
                                               help=_CONTROL_HELP.get(k)))
            else:
                new[k] = st.text_input(k, value="" if v is None else str(v),
                                       help=_CONTROL_HELP.get(k)) or None
        if st.form_submit_button("💾 Save Control"):
            dump_config(CONFIG_DIR, control=control_from_dict(new),
                        tables=load_biology_tables(CONFIG_DIR),
                        facility=load_facility_config(CONFIG_DIR))
            st.success("Saved config/control.yaml")


def _edit_biology():
    from forecast.config_io import (
        load_control, load_biology_tables, load_facility_config, dump_config,
    )
    st.caption("Growth (SGR FW/SW), FCR curves, mortality, feed types, and "
               "culling. Edits persist until you Save or Reload.")
    if "bio_models" not in st.session_state:
        g, m, f, c, models = _biology_to_frames(load_biology_tables(CONFIG_DIR))
        st.session_state.update({"bio_growth": g, "bio_mort": m, "bio_feed": f,
                                 "bio_cull": c, "bio_models": models})
    models = st.session_state["bio_models"]
    st.markdown("**Growth + FCR** (by fish size, grams)")
    g2 = st.data_editor(st.session_state["bio_growth"], num_rows="dynamic",
                        hide_index=True, use_container_width=True, key="bio_growth_w")
    cols = st.columns(3)
    with cols[0]:
        st.markdown("**Mortality** (% / wk)")
        m2 = st.data_editor(st.session_state["bio_mort"], num_rows="dynamic",
                            hide_index=True, key="bio_mort_w")
    with cols[1]:
        st.markdown("**Feed types**")
        f2 = st.data_editor(st.session_state["bio_feed"], num_rows="dynamic",
                            hide_index=True, key="bio_feed_w")
    with cols[2]:
        st.markdown("**Culling**")
        c2 = st.data_editor(st.session_state["bio_cull"], num_rows="dynamic",
                            hide_index=True, key="bio_cull_w")
    b1, b2, _ = st.columns([1, 1, 3])
    if b1.button("💾 Save Biology", key="save_bio"):
        try:
            tables2 = _frames_to_biology(g2, m2, f2, c2, models)
            dump_config(CONFIG_DIR, control=load_control(CONFIG_DIR),
                        tables=tables2, facility=load_facility_config(CONFIG_DIR))
            _reset_keys("bio_growth", "bio_mort", "bio_feed", "bio_cull")
            st.session_state.pop("bio_models", None)
            st.success("Saved config/biology.yaml")
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"Save failed: {e}")
    if b2.button("↻ Reload", key="reload_bio"):
        _reset_keys("bio_growth", "bio_mort", "bio_feed", "bio_cull")
        st.session_state.pop("bio_models", None)
        st.rerun()


def _edit_facility():
    from forecast.config_io import (
        load_control, load_biology_tables, load_facility_config,
        facility_to_dict, facility_from_dict, dump_config,
    )
    st.caption("Tank definitions: system, stage, volume, density/feed caps, type.")
    base = _persist("fac_df", lambda: pd.DataFrame(
        facility_to_dict(load_facility_config(CONFIG_DIR))["tanks"]))
    edited = st.data_editor(base, num_rows="dynamic", hide_index=True,
                            use_container_width=True, key="fac_df_w",
                            column_config={c: st.column_config.Column(help=h)
                                           for c, h in _FACILITY_HELP.items()})
    b1, b2, _ = st.columns([1, 1, 3])
    if b1.button("💾 Save Facility", key="save_fac"):
        try:
            fac2 = facility_from_dict({"tanks": _records(edited)})
            dump_config(CONFIG_DIR, control=load_control(CONFIG_DIR),
                        tables=load_biology_tables(CONFIG_DIR), facility=fac2)
            _reset_keys("fac_df")
            st.success("Saved config/facility.yaml")
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"Save failed: {e}")
    if b2.button("↻ Reload", key="reload_fac"):
        _reset_keys("fac_df")
        st.rerun()


def _edit_batches():
    from forecast.scenario_io import (
        load_batches, load_limits, batches_to_list, batches_from_list,
        dump_scenario,
    )
    st.caption("Forward batch schedule + metadata. In-flight state comes from "
               "the ProductionReport; this is the planning/metadata layer.")
    base = _persist("batch_df", lambda: pd.DataFrame(
        batches_to_list(load_batches(SCENARIO_DIR))))
    edited = st.data_editor(base, num_rows="dynamic", hide_index=True,
                            use_container_width=True, key="batch_df_w",
                            column_config={c: st.column_config.Column(help=h)
                                           for c, h in _BATCH_HELP.items()})
    b1, b2, _ = st.columns([1, 1, 3])
    if b1.button("💾 Save Batches", key="save_batch"):
        try:
            batches2 = batches_from_list(_records(edited))
            fl, sl = load_limits(SCENARIO_DIR)
            dump_scenario(SCENARIO_DIR, batches=batches2,
                          facility_limits=fl, system_limits=sl)
            _reset_keys("batch_df")
            st.success("Saved scenario/batches.yaml")
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"Save failed: {e}")
    if b2.button("↻ Reload", key="reload_batch"):
        _reset_keys("batch_df")
        st.rerun()


def _og_systems_app():
    """OG system ids from facility config, or the standard 12 default."""
    try:
        from forecast.config_io import load_facility_config
        s = sorted({t.system_id for t in load_facility_config(CONFIG_DIR).tanks
                    if t.type == "OG" and t.system_id})
        if s:
            return s
    except Exception:  # noqa: BLE001
        pass
    return ["OG1N", "OG1S", "OG2N", "OG2S", "OG3N", "OG3S",
            "OG4N", "OG4S", "OG5N", "OG5S", "OG6N", "OG6S"]


def _limit_week_cols(fl_cur, sl_cur):
    """Week columns for the limits grid: the forecast horizon (PR start +
    Control horizon) when a PR is uploaded, else the weeks already present."""
    pr = _ingest_pr(uploaded) if uploaded is not None else None
    if pr and pr["ok"] and _config_ready():
        try:
            from forecast.config_io import load_control
            from forecast.time_grid import forecast_week_labels
            return forecast_week_labels(pr["forecast_start"],
                                        int(load_control(CONFIG_DIR).horizon_weeks))
        except Exception:  # noqa: BLE001
            pass
    return sorted({k[0] for k in fl_cur} | {k[0] for k in sl_cur})


def _edit_limits():
    from forecast.scenario_io import (
        load_batches, load_limits, facility_limits_to_list,
        system_limits_to_list, facility_limits_from_list,
        system_limits_from_list, dump_scenario,
    )
    from forecast.caps import (METRIC_BIOMASS, METRIC_FEED_DAY, METRIC_MAX_HARVEST,
                               METRIC_MIN_HARVEST, METRIC_HOG_YIELD)
    st.caption("Per-week caps — weeks across the top, one row per parameter. "
               "Label columns + header stay frozen as you scroll. Blank = no "
               "cap (facility blank = use the Control default).")
    fl, sl = load_limits(SCENARIO_DIR)
    fl_cur = {(r["week"], r["metric"]): r["value"] for r in facility_limits_to_list(fl)}
    sl_cur = {(r["week"], r["system"], r["metric"]): r["value"]
              for r in system_limits_to_list(sl)}
    weeks = _limit_week_cols(fl_cur, sl_cur)
    if not weeks:
        st.info("No weeks yet — upload a ProductionReport (sets the horizon) or "
                "import a template with limits.")
        return
    fl_metrics = [METRIC_BIOMASS, METRIC_FEED_DAY, METRIC_MAX_HARVEST,
                  METRIC_MIN_HARVEST, METRIC_HOG_YIELD]
    sl_metrics = [METRIC_BIOMASS, METRIC_FEED_DAY]
    systems = _og_systems_app()
    if "flim_wide" not in st.session_state:
        fac = pd.DataFrame([{"metric": m, **{wk: fl_cur.get((wk, m)) for wk in weeks}}
                            for m in fl_metrics]).astype({wk: "float64" for wk in weeks})
        sysd = pd.DataFrame([{"system": s, "metric": m,
                              **{wk: sl_cur.get((wk, s, m)) for wk in weeks}}
                             for s in systems for m in sl_metrics]
                            ).astype({wk: "float64" for wk in weeks})
        st.session_state["flim_wide"] = fac
        st.session_state["slim_wide"] = sysd
        st.session_state["_lim_weeks"] = weeks
    weeks = st.session_state["_lim_weeks"]
    wk_cfg = {wk: st.column_config.NumberColumn(width="small") for wk in weeks}
    fac_cfg = {"metric": st.column_config.Column(pinned=True, disabled=True), **wk_cfg}
    sys_cfg = {"system": st.column_config.Column(pinned=True, disabled=True),
               "metric": st.column_config.Column(pinned=True, disabled=True), **wk_cfg}
    st.markdown("**Facility limits**")
    fdf = st.data_editor(st.session_state["flim_wide"], hide_index=True,
                         column_config=fac_cfg, key="flim_wide_w")
    st.markdown("**System limits**")
    sdf = st.data_editor(st.session_state["slim_wide"], hide_index=True,
                         column_config=sys_cfg, key="slim_wide_w", height=400)
    b1, b2, _ = st.columns([1, 1, 3])
    if b1.button("💾 Save Limits", key="save_lim"):
        try:
            fl_recs = [{"week": wk, "metric": r["metric"], "value": float(r[wk])}
                       for r in _records(fdf) for wk in weeks
                       if r.get(wk) not in (None, "")]
            sl_recs = [{"week": wk, "system": r["system"], "metric": r["metric"],
                        "value": float(r[wk])}
                       for r in _records(sdf) for wk in weeks
                       if r.get(wk) not in (None, "")]
            dump_scenario(SCENARIO_DIR, batches=load_batches(SCENARIO_DIR),
                          facility_limits=facility_limits_from_list(fl_recs),
                          system_limits=system_limits_from_list(sl_recs))
            _reset_keys("flim_wide", "slim_wide")
            st.session_state.pop("_lim_weeks", None)
            st.success("Saved scenario/limits.yaml")
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"Save failed: {e}")
    if b2.button("↻ Reload", key="reload_lim"):
        _reset_keys("flim_wide", "slim_wide")
        st.session_state.pop("_lim_weeks", None)
        st.rerun()


def _config_template_bytes(horizon_weeks=None, forecast_start=None) -> bytes:
    from forecast.config_template import write_config_template
    wd = Path(tempfile.mkdtemp(prefix="as_tmpl_"))
    out = wd / "config_template.xlsx"
    write_config_template(
        out,
        config_dir=CONFIG_DIR if _config_ready() else None,
        scenario_dir=SCENARIO_DIR if _scenario_ready() else None,
        horizon_weeks=horizon_weeks, forecast_start=forecast_start,
    )
    return out.read_bytes()


def _current_horizon_start():
    """Default horizon + start for the template, from current control if any."""
    from datetime import date
    h, s = 52, date.today()
    if _config_ready():
        try:
            from forecast.config_io import load_control
            c = load_control(CONFIG_DIR)
            h = int(c.horizon_weeks)
            fs = c.forecast_start
            s = fs.date() if hasattr(fs, "date") else (fs or s)
        except Exception:  # noqa: BLE001
            pass
    return h, s


def _config_fingerprint() -> str:
    """Hash of config/ + scenario/ file names + mtimes — changes whenever any
    config is saved, so a cached template can be invalidated."""
    import hashlib
    h = hashlib.md5()
    for d in (CONFIG_DIR, SCENARIO_DIR):
        if d.exists():
            for p in sorted(d.iterdir()):
                if p.is_file():
                    h.update(p.name.encode())
                    h.update(str(p.stat().st_mtime_ns).encode())
    return h.hexdigest()


def _config_io_section():
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Template — build config offline**")
        pr = _ingest_pr(uploaded) if uploaded is not None else None
        if not (pr and pr["ok"]):
            st.info("⬆ Upload a valid **ProductionReport** (sidebar) to enable "
                    "the template — its week labels come from the PR's closing "
                    "date. The forecast length comes from Control → horizon.")
        else:
            h = _current_horizon_start()[0]
            fs = pr["forecast_start"]
            # Invalidate a cached template if config changed since it was built,
            # so Download always reflects the latest saved config.
            _fp = _config_fingerprint()
            if st.session_state.get("_tmpl_fp") != _fp:
                st.session_state.pop("_tmpl_bytes", None)
            st.caption(f"Template covers **{h} weeks from {fs.date()}** — start "
                       f"derived from the PR, horizon from Control (edit on the "
                       f"Control tab). Every per-week limit slot is laid out to "
                       f"fill in; current values pre-filled.")
            if st.button("Build template", use_container_width=True):
                st.session_state["_tmpl_bytes"] = _config_template_bytes(int(h), fs)
                st.session_state["_tmpl_fp"] = _fp
            if "_tmpl_bytes" in st.session_state:
                st.download_button(
                    "⬇ Download config template (.xlsx)",
                    data=st.session_state["_tmpl_bytes"],
                    file_name="config_template.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )
            else:
                st.caption("↑ Click **Build template** to (re)generate from the "
                           "current saved config.")
    with c2:
        st.markdown("**Import — load config from a file**")
        st.caption("A filled config template, or a saved forecast workbook "
                   "(RunConfig snapshot). Overwrites current config/ + scenario/.")
        imp = st.file_uploader("Config template or saved workbook",
                               type=["xlsx", "xlsm"], key="cfg_import")
        if st.button("📥 Import config", disabled=imp is None, key="do_import",
                     use_container_width=True):
            try:
                from forecast.config_template import (
                    import_config_template, is_config_template,
                )
                from forecast.config_snapshot import (
                    import_config_snapshot, read_config_snapshot,
                )
                wd = Path(tempfile.mkdtemp(prefix="as_import_"))
                p = wd / imp.name
                p.write_bytes(imp.getvalue())
                wb = load_workbook(p, keep_vba=(p.suffix.lower() == ".xlsm"))
                if is_config_template(wb):
                    restored = import_config_template(wb, CONFIG_DIR, SCENARIO_DIR)
                    src = "config template"
                elif read_config_snapshot(wb):
                    restored = import_config_snapshot(wb, CONFIG_DIR, SCENARIO_DIR)
                    src = "RunConfig snapshot"
                else:
                    restored, src = [], None
                wb.close()
                if not restored:
                    st.error("No config template or RunConfig snapshot found "
                             "in that file.")
                else:
                    _clear_all_editor_state()  # refresh open editors from disk
                    st.success(f"Imported {len(restored)} file(s) from {src}: "
                               f"{', '.join(restored)}")
                    st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"Import failed: {e}")


def _config_editor():
    st.header("⚙️ Configure — models & control")
    st.caption("Build the forecast config here — saved to `config/` + `scenario/` "
               "YAML (the source of truth). Or populate it offline via the "
               "template/import below, separately from the PR.")
    ready = _config_ready() and _scenario_ready()
    with st.expander("📦 Template & import", expanded=not ready):
        _config_io_section()
    if not ready:
        st.warning("No config yet — download the template above, fill it in, and "
                   "import; or seed from a workbook in Run mode.")
        return

    tabs = st.tabs(["Control", "Biology models", "Facility (tanks)",
                    "Batches", "Limits"])
    with tabs[0]:
        _edit_control()
    with tabs[1]:
        _edit_biology()
    with tabs[2]:
        _edit_facility()
    with tabs[3]:
        _edit_batches()
    with tabs[4]:
        _edit_limits()


# ============================================================
# Page setup
# ============================================================

st.set_page_config(
    page_title="AS Production Forecast",
    page_icon="🐟",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("AS Production Forecast")
st.caption(
    "Upload an input workbook, run the planner, review the results, "
    "and download a new workbook with all tabs populated. "
    "The input workbook is never modified."
)


# ============================================================
# Sidebar — upload + run controls
# ============================================================

with st.sidebar:
    app_mode = st.radio(
        "Mode",
        ["Run forecast", "Configure (models & control)", "Tune (density knobs)",
         "Optimize (multi-objective)"],
        help="Run forecast: upload a PR and run. Configure: edit the app's "
             "biology models, facility, control, batches, and limits. "
             "Tune: sweep the controller knobs and read the per-batch "
             "density distribution. Optimize: sweep knobs and rank variants on a "
             "selectable objective (walk the line + minimize feed/handling).",
    )
    st.divider()

    st.header("ProductionReport")
    # Keyed by a nonce so "Clear PR & last run" can reset the widget (bumping
    # the nonce gives the uploader a fresh key → it renders empty).
    uploaded = st.file_uploader(
        "ProductionReport workbook (.xlsx / .xlsm)",
        type=["xlsm", "xlsx"],
        help="Only the ProductionReport sheet is read. The forecast start is "
             "derived from its closing date. Models/limits come from config.",
        key=f"pr_upload_{st.session_state.get('_pr_nonce', 0)}",
    )

    pr = _ingest_pr(uploaded) if uploaded is not None else None
    if pr is not None:
        if pr["ok"]:
            st.success(
                f"PR ✓ — forecast start **{pr['forecast_start'].date()}** "
                f"(closing {pr['closing']}) · {pr['n_og']} OG + {pr['n_fw']} FW rows"
            )
        else:
            for e in pr["errors"]:
                st.error(e)
        for w in pr.get("warnings", []):
            st.warning(w)

    _cfg_ok = _config_ready() and _scenario_ready()
    _pr_ok = pr is not None and pr["ok"]
    if not _cfg_ok:
        st.info("No config yet — set it up in **Configure**.")

    st.header("Run")
    forecast_method = st.radio(
        "Planning method",
        ["Controller (validated)", "Global (precalculated)"],
        help="Controller: the validated closed-loop production planner (forecast/"
             "run.py). Global: the precalculated L1→L3 method — whole-facility, "
             "within-limits L1; swap-free, 0-drift specific-tank pick; optimizer-"
             "tuned selective over-stock. Same PR in, same workbook shape out "
             "(stamped with the method) so you can compare apples-to-apples. The "
             "global method runs an LP per week, so it's slower.",
        key="forecast_method",
    )
    _is_global = forecast_method.startswith("Global")
    _global_optimal = False
    _cpsat_time = 300.0
    if _is_global:
        st.caption("⚠ Global is the experimental precalculated engine — the "
                   "BatchLocations/Transfers are real (0-drift). Heuristic mode "
                   "leaves some per-tank density over-cap (the structural limit the "
                   "controller also hits); the optimal mode below removes it.")
        _global_optimal = st.checkbox(
            "Optimal placement (CP-SAT) — fills tanks, 0 over-cap, low variance",
            value=False, key="global_optimal",
            help="Solves the WHOLE-horizon tank layout as ONE precalculated "
                 "optimization (OR-Tools CP-SAT): keeps tanks as full as 0-drift "
                 "allows (~90% — the remainder is the mandatory fallow week between "
                 "batches in a tank), spreads biomass to low EVEN density (0 "
                 "over-cap, worst ~94 vs the controller's 176), and minimizes "
                 "system-load variance. Determined proportional-volume split; 0 "
                 "TANK_DRIFT, audited. SLOWER — minutes, not seconds.")
        if _global_optimal:
            _cpsat_time = float(st.slider(
                "CP-SAT solve budget (seconds)", 60, 900, 300, 60,
                key="cpsat_time",
                help="More time tightens the optimum (fewer transfers); fill "
                     "plateaus past ~300s, so 300s is a good default."))
    if _cfg_ok:
        from forecast.config_io import load_control, control_to_dict
        _render_active_config(
            control_to_dict(load_control(CONFIG_DIR)),
            "ℹ️ Active configuration — what this run will do")
    run_clicked = st.button(
        "▶ Run forecast",
        type="primary",
        disabled=(not _pr_ok or not _cfg_ok),
        use_container_width=True,
        help=None if (_pr_ok and _cfg_ok)
        else "Upload a valid ProductionReport and set up config first.",
    )

    if "result" in st.session_state and st.session_state.result.get("ok"):
        r = st.session_state.result
        st.success(
            f"Last run: {r['elapsed']:.1f}s, {r['violations']} viols"
        )
        st.download_button(
            label="⬇ Download output workbook",
            data=r["output_bytes"],
            file_name=r["output_name"],
            mime=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                  if str(r["output_name"]).lower().endswith(".xlsx")
                  else "application/vnd.ms-excel.sheet.macroenabled.12"),
            use_container_width=True,
        )
        # Fallback: show where the file lives on disk in case the
        # download button doesn't work (browser quirks, file size, etc.)
        if r.get("output_path"):
            st.caption(f"Also saved at:\n`{r['output_path']}`")
    elif "result" in st.session_state and not st.session_state.result.get("ok"):
        st.error("Last run failed — see error in main panel.")
        if st.session_state.result.get("output_path"):
            st.caption(
                f"Partial output (if any):\n"
                f"`{st.session_state.result['output_path']}`"
            )
    else:
        st.caption(
            "Upload a workbook, click ▶ Run forecast. The download "
            "button appears here after the run completes."
        )

    # ---- Clear / start fresh (config + scenario are kept) ----
    if uploaded is not None or "result" in st.session_state:
        st.divider()
        if st.button("🗑 Clear PR & last run", use_container_width=True,
                     help="Remove the uploaded ProductionReport and the last run "
                          "result, to start a fresh forecast. Your config + "
                          "scenario (models, limits, batches) are kept."):
            for _k in ("result", "_pr", "_pr_key"):
                st.session_state.pop(_k, None)
            # Bump the nonce so the file_uploader widget resets to empty.
            st.session_state["_pr_nonce"] = st.session_state.get("_pr_nonce", 0) + 1
            st.rerun()


# ============================================================
# Pipeline runner
# ============================================================

def _run_with_workbook_bytes(
    input_bytes: bytes,
    input_name: str,
    config_dir: str | None = None,
    scenario_dir: str | None = None,
    method: str = "controller",
    cpsat_time: float = 300.0,
) -> dict:
    """Run the pipeline against `input_bytes` in a temp directory.

    When config_dir/scenario_dir are given (PR-only mode), the stable
    config + scenario load from YAML and the uploaded workbook supplies
    only the ProductionReport. Returns a dict with metrics + the output
    workbook bytes + parsed data needed for visualization.
    """
    work_dir = Path(tempfile.mkdtemp(prefix="as_forecast_"))
    in_path = work_dir / input_name
    # The global method emits a fresh .xlsx (no VBA to carry); the controller
    # keeps the uploaded macro workbook's suffix.
    if method == "global_optimal":
        out_name = Path(input_name).stem + "_planned_OPTIMAL.xlsx"
    elif method == "global":
        out_name = Path(input_name).stem + "_planned_GLOBAL.xlsx"
    else:
        out_name = Path(input_name).stem + "_planned" + Path(input_name).suffix
    out_path = work_dir / out_name
    in_path.write_bytes(input_bytes)

    # Run the pipeline, capturing console output for display.
    t0 = time.time()
    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            if method in ("global", "global_optimal"):
                from tools.run_global_forecast import run_global
                rc = run_global(in_path, out_path, config_dir, scenario_dir,
                                optimal=(method == "global_optimal"),
                                cpsat_time=cpsat_time)
            else:
                rc = run_pipeline(input_path=in_path, output_path=out_path,
                                  config_dir=config_dir, scenario_dir=scenario_dir)
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "traceback": traceback.format_exc(),
            "stdout": captured.getvalue(),
            "elapsed": time.time() - t0,
        }
    elapsed = time.time() - t0

    if rc != 0 or not out_path.exists():
        return {
            "ok": False,
            "error": f"Pipeline returned rc={rc} or no output produced",
            "stdout": captured.getvalue(),
            "elapsed": elapsed,
            "output_path": str(out_path) if out_path.exists() else None,
        }

    # Read parsed outputs for visualization.
    output_bytes = out_path.read_bytes()
    parsed = _parse_output_workbook(out_path)
    # Capture the EFFECTIVE config this run used (config_dir includes any optimizer
    # overrides for applied runs), so the result can always show what produced it.
    config_used = {}
    if config_dir:
        try:
            from forecast.config_io import load_control, control_to_dict
            config_used = control_to_dict(load_control(config_dir))
        except Exception:  # noqa: BLE001
            config_used = {}
    parsed.update({
        "ok": True,
        "elapsed": elapsed,
        "stdout": captured.getvalue(),
        "output_bytes": output_bytes,
        "output_name": out_name,
        "output_path": str(out_path),
        "config_used": config_used,
    })
    return parsed


def _parse_output_workbook(path: Path) -> dict:
    """Extract data from the saved workbook for the UI's visualization."""
    wb = load_workbook(path, keep_vba=str(path).lower().endswith(".xlsm"),
                       data_only=False)

    # Density violations from BatchLocations (header at row 4).
    violations = []
    bl_rows = []
    if "BatchLocations" in wb.sheetnames:
        ws = wb["BatchLocations"]
        for i, row in enumerate(ws.iter_rows(values_only=True), 1):
            if i < 5 or not row or row[0] is None:
                continue
            wk, ws_d, bid, tid, sys_id, count, avg_wt, biomass, density = row[:9]
            bl_rows.append({
                "Week": wk, "Batch": bid, "Tank": tid, "System": sys_id,
                "Count": count, "AvgWt_kg": avg_wt, "Biomass_kg": biomass,
                "Density_kg_m3": density,
            })
            if isinstance(density, (int, float)) and density > 95:
                violations.append(density)

    # BiologyProjection — per (batch, week) explicit mortality % + cull
    # events. BatchLocations only shows END-OF-WEEK count (mortality
    # implicitly applied); this sheet has the per-week mortality fraction
    # and cull counts/biomass so we can chart "weekly losses" at scale.
    bio_rows = []
    if "BiologyProjection" in wb.sheetnames:
        ws = wb["BiologyProjection"]
        header = None
        for i, row in enumerate(ws.iter_rows(values_only=True), 1):
            if header is None and row and row[0] == "Batch":
                header = list(row)
                idx = {h: j for j, h in enumerate(header)}
                continue
            if header is None or not row or row[0] is None:
                continue
            def g(name, default=None):
                j = idx.get(name)
                if j is None or j >= len(row):
                    return default
                return row[j]
            bid = g("Batch")
            wk = g("Week")
            stage = g("Stage")
            count = g("Count")
            mort_pct_wk = g("Mortality_pct_wk", 0) or 0
            cull_count = g("Cull_Count", 0) or 0
            if not isinstance(count, (int, float)):
                continue
            # Weekly mortality count = current count * mort_pct/100
            # (approximation; the geometric daily compound is close).
            mort_count = float(count) * float(mort_pct_wk) / 100.0
            bio_rows.append({
                "Batch": bid, "Week": wk, "Stage": stage,
                "Count": float(count),
                "Mortality_count_wk": mort_count,
                "Cull_count_wk": float(cull_count),
            })

    # Harvest events from HarvestPlan. Single-table format (title rows 1-3,
    # header row 4, data rows from row 5):
    #   Week | Batch | Tank | Count (fish) | Gross_AvgWt (kg) | Gross_Biomass (kg)
    #   | HOG_Yield | HOG_AvgWt | HOG_Biomass
    # Data rows are identified by an ISO week label in col 0 ("2026-W23").
    harvest_kg = 0.0
    harvest_count = 0
    harvest_events = []
    if "HarvestPlan" in wb.sheetnames:
        ws = wb["HarvestPlan"]
        for row in ws.iter_rows(values_only=True):
            if not row or not isinstance(row[0], str) or "-W" not in row[0]:
                continue
            if len(row) < 6:
                continue
            cnt = row[3]
            gross_kg = row[5]
            gross_avg_kg = row[4]
            hog_kg = row[8] if len(row) > 8 and isinstance(row[8], (int, float)) else None
            if isinstance(cnt, (int, float)) and isinstance(gross_kg, (int, float)):
                harvest_count += cnt
                harvest_kg += gross_kg
                harvest_events.append({
                    "Week": row[0], "Batch": row[1],
                    "Count": cnt, "Gross_kg": gross_kg,
                    "Avg_wt_kg": gross_avg_kg,
                    "HOG_kg": hog_kg if hog_kg is not None else 0.0,
                })

    # Validation warnings now live in ValidationLog ("# | Category | Detail",
    # data rows have a numeric '#'); the Advisory sheet is the per-week capacity
    # table. Build the issues-by-category summary + detail list from ValidationLog.
    advisory_entries = []
    advisory_summary = []
    if "ValidationLog" in wb.sheetnames:
        ws = wb["ValidationLog"]
        cat_counts: dict[str, int] = defaultdict(int)
        for r in ws.iter_rows(values_only=True):
            if not r or not isinstance(r[0], (int, float)):
                continue  # title/header rows have no numeric '#'
            cat = str(r[1]) if len(r) > 1 and r[1] else ""
            det = str(r[2]) if len(r) > 2 and r[2] else ""
            advisory_entries.append({"#": int(r[0]), "Category": cat, "Detail": det})
            cat_counts[cat] += 1
        advisory_summary = [{"Category": c, "Count": n}
                            for c, n in sorted(cat_counts.items(), key=lambda x: -x[1])]

    # Yearly summary (facility-wide per-year rollup) for the app's yearly trends.
    yearly = []
    if "YearlySummary" in wb.sheetnames:
        ws = wb["YearlySummary"]
        hdr = None
        for row in ws.iter_rows(values_only=True):
            if row and row[0] == "Year":
                hdr = [str(c) for c in row if c is not None]
                continue
            if hdr and row and isinstance(row[0], (int, float)):
                yearly.append({hdr[i]: row[i] for i in range(min(len(hdr), len(row)))})

    # TransferTemplate: Section A = the canonical production-flow template (the
    # seawater journey every batch follows), Section B = per-batch plan summary.
    # Parse both for the Plan tab.
    plan_summary = []
    flow_template = []
    if "TransferTemplate" in wb.sheetnames:
        ws = wb["TransferTemplate"]
        hdr = None            # Section B (per-batch) header
        flow_hdr = None       # Section A (production-flow) header
        for row in ws.iter_rows(values_only=True):
            if row and row[0] == "Stage":          # Section A header row
                flow_hdr = [str(c) if c is not None else "" for c in row]
                continue
            if row and row[0] == "Batch":          # Section B header row
                hdr = [str(c) for c in row if c is not None]
                continue
            if (flow_hdr and row and isinstance(row[0], str)
                    and row[0][:1].isdigit()):     # Section A stage rows ("1. …")
                flow_template.append({flow_hdr[i]: row[i]
                                      for i in range(min(len(flow_hdr), len(row)))
                                      if flow_hdr[i]})
            if (hdr and row and isinstance(row[0], str)
                    and row[0].startswith("B") and len(row[0]) > 1 and row[0][1].isdigit()):
                plan_summary.append({hdr[i]: row[i] for i in range(min(len(hdr), len(row)))})

    # Control status (R8-R16).
    status = {}
    if "Control" in wb.sheetnames:
        ws = wb["Control"]
        labels = {
            8: "status", 9: "timestamp", 10: "scenario",
            11: "forecast_start", 12: "horizon", 13: "batches",
            14: "og_tanks", 15: "elapsed", 16: "warnings",
        }
        for r, k in labels.items():
            status[k] = ws.cell(row=r, column=2).value

    return {
        "violations": len(violations),
        "worst_density": max(violations, default=0.0),
        "harvest_kg": harvest_kg,
        "harvest_count": harvest_count,
        "batch_locations": bl_rows,
        "harvest_events": harvest_events,
        "biology_projection": bio_rows,
        "advisory_summary": advisory_summary,
        "advisory_entries": advisory_entries,
        "control_status": status,
        "yearly": yearly,
        "plan_summary": plan_summary,
        "flow_template": flow_template,
    }


# ============================================================
# Tune mode — sweep the controller knobs, read the distribution
# ============================================================

def _results_to_frame(results) -> pd.DataFrame:
    rows = []
    for r in results:
        d = r.dist
        rows.append({
            "Variant": r.label,
            "OVER": f"{d.over}/{d.n}",
            "Severe (>1.3x)": d.severe,
            "Worst": round(d.worst, 2),
            "Median": round(d.median, 2),
            "<=1.0": d.buckets["<=1.0"],
            "1.0-1.1": d.buckets["1.0-1.1"],
            "1.1-1.3": d.buckets["1.1-1.3"],
            ">1.3": d.buckets[">1.3"],
            "Conservation": "OK" if r.conservation_ok else f"FAIL ({r.dropped} drop/{r.overprod} over)",
        })
    return pd.DataFrame(rows)


def _tuner():
    st.header("🎛️ Tune — per-batch density knobs")
    st.caption(
        "Sweeps the controller knobs and reports the per-batch **peak-density "
        "distribution** for each, using the current config + the uploaded "
        "ProductionReport. Read the distribution, not the raw OVER count: 1.0–1.1 "
        "is *at cap* (normal near full utilisation); only **severe (>1.3×)** rows "
        "matter. Pick the variant with the fewest severe while conservation holds. "
        "If none beats baseline, it's a capacity problem, not a tuning one "
        "(see USER_GUIDE §7.1)."
    )

    _cfg_ok = _config_ready() and _scenario_ready()
    _pr_ok = pr is not None and pr["ok"]
    if not _cfg_ok:
        st.info("No config yet — set it up in **Configure** first.")
        return
    if not _pr_ok:
        st.info("Upload a valid **ProductionReport** in the sidebar first.")
        return

    depth = st.radio(
        "Sweep depth",
        ["Quick", "Full"],
        horizontal=True,
        help="Quick: baseline + the dominant lever on each axis (fast read). "
             "Full: both directions of every relevant knob.",
    )
    quick = depth == "Quick"
    grid = tuning.grid_for(quick)
    n_variants = len(grid)
    st.write(
        f"**{depth}** sweep — runs the forecast **{n_variants} times** "
        f"(~{n_variants * 90 // 60}–{max(1, n_variants * 100 // 60)} min). "
        "The current config is never modified — each variant runs on a temp copy."
    )
    go = st.button("▶ Run tuning sweep", type="primary")

    if go:
        work = Path(tempfile.mkdtemp(prefix="as_tune_in_"))
        in_path = work / (uploaded.name or "input.xlsm")
        in_path.write_bytes(uploaded.getvalue())
        bar = st.progress(0.0, text="Starting sweep…")

        def _progress(i, n, label):
            bar.progress(i / n, text=f"[{i+1}/{n}] running {label} …")

        try:
            results = tuning.sweep(str(in_path), str(CONFIG_DIR),
                                   str(SCENARIO_DIR), grid=grid, progress=_progress)
        except Exception as e:  # noqa: BLE001
            bar.empty()
            st.error(f"Sweep failed: {e}")
            st.code(traceback.format_exc())
            return
        bar.progress(1.0, text="Sweep complete")
        st.session_state["_tune_results"] = results

    results = st.session_state.get("_tune_results")
    if not results:
        return

    rec = tuning.recommend(results)
    if rec.is_capacity_bound:
        st.warning(f"**Capacity-bound:** {rec.text}")
    else:
        st.success(f"**Recommendation:** {rec.text}")

    df = _results_to_frame(results)

    def _hl(row):
        if row["Variant"] == rec.best_label:
            return ["background-color: #d7f0d7"] * len(row)
        if row["Variant"] == "baseline":
            return ["background-color: #eef3fb"] * len(row)
        return [""] * len(row)

    st.dataframe(df.style.apply(_hl, axis=1), use_container_width=True,
                 hide_index=True)

    # Peak-density distribution chart (stacked bands per variant).
    band_cols = ["<=1.0", "1.0-1.1", "1.1-1.3", ">1.3"]
    long = df.melt(id_vars="Variant", value_vars=band_cols,
                   var_name="Band", value_name="Batches")
    fig = px.bar(long, x="Variant", y="Batches", color="Band",
                 title="Peak-density distribution by variant",
                 color_discrete_map={"<=1.0": "#2e7d32", "1.0-1.1": "#9ccc65",
                                     "1.1-1.3": "#ffb300", ">1.3": "#c62828"})
    st.plotly_chart(fig, use_container_width=True)

    # Severe-batch detail for the recommended (or baseline) variant.
    best = next((r for r in results if r.label == rec.best_label), results[0])
    if best.severe_rows:
        st.subheader(f"Batches over 1.2× cap — {best.label}")
        st.caption(
            "Where the density pressure actually is. If these cluster in time "
            "(close Entry weeks) and peak mid-grow-out, it's a capacity collision."
        )
        st.dataframe(pd.DataFrame(best.severe_rows), use_container_width=True,
                     hide_index=True)
    else:
        st.info(f"No batch exceeds 1.2× cap in **{best.label}**.")


# ============================================================
# Configure / Tune modes — render and stop
# ============================================================

def _opt_table(results) -> pd.DataFrame:
    rows = []
    for v in results:
        m = v.metrics
        rows.append({
            "Variant": v.label,
            "Score": round(v.score, 3),
            "Sys_over-cap": round(m.system_overshoot, 3),
            "Density_over-cap": round(m.density_overshoot, 3),
            "Biomass_overshoot": round(m.biomass_overshoot, 3),
            "Biomass_var": round(m.biomass_var, 3),
            "Util_gap": round(m.biomass_util_gap, 3),
            "Harvest_var": round(m.harvest_var, 3),
            "Harvest_overshoot": round(m.harvest_overshoot, 3),
            "Feed_load": round(m.feed_load),
            "Transfers/fish": round(m.transfers_per_fish, 2),
            "Wks_over_55k": m.weeks_over_harvest_cap,
            "Conservation": "OK" if v.conservation_ok else f"FAIL ({v.dropped}/{v.overprod})",
        })
    return pd.DataFrame(rows)


# System -> conveyor tier, for the per-batch journey ("how it got there").
_BATCH_TIER = {
    "OG1N": "Nursery (OG1/2)", "OG1S": "Nursery (OG1/2)",
    "OG2N": "Nursery (OG1/2)", "OG2S": "Nursery (OG1/2)",
    "OG3N": "Grow-out OG3", "OG3S": "Grow-out OG3",
    "OG4N": "Grow-out OG4", "OG4S": "Grow-out OG4",
    "OG5N": "Grow-out OG5", "OG5S": "Grow-out OG5",
    "OG6S": "Finishing OG6", "OG6N": "Finishing/depuration OG6N",
}
_BATCH_TIER_ORDER = ["Nursery (OG1/2)", "Grow-out OG3", "Grow-out OG4",
                     "Grow-out OG5", "Finishing OG6", "Finishing/depuration OG6N"]


def _derive_batch_plans(bl_df, he_df):
    """Per-batch journey from BatchLocations (+ HarvestPlan): a summary header plus
    the milestone timeline (the tier transitions each batch makes, when, at what
    weight/tanks, through to harvest). Pure derivation from data already in the
    output workbook — the 'where each batch is + how it got there' traceability."""
    plans = []
    if bl_df is None or bl_df.empty:
        return plans
    bl = bl_df.copy()
    bl["Tier"] = bl["System"].map(lambda s: _BATCH_TIER.get(str(s), str(s)))
    bl["Week"] = bl["Week"].astype(str)
    hv = {}
    if he_df is not None and not he_df.empty:
        for b, g in he_df.groupby("Batch"):
            wks = sorted(str(w) for w in g["Week"])
            hv[str(b)] = {"first": wks[0], "last": wks[-1],
                          "hog_t": float(g["HOG_kg"].sum()) / 1000.0,
                          "avg_wt": float(pd.to_numeric(g["Avg_wt_kg"], errors="coerce").mean())}
    for b, g in bl.groupby("Batch"):
        g = g.sort_values("Week")
        weeks = list(dict.fromkeys(g["Week"]))
        peak_tanks = int(g.groupby("Week")["Tank"].nunique().max())
        milestones, seen = [], set()
        for wk in weeks:
            gw = g[g["Week"] == wk]
            here = set(gw["Tier"])
            for tier in _BATCH_TIER_ORDER:
                if tier in here and tier not in seen:
                    seen.add(tier)
                    sub = gw[gw["Tier"] == tier]
                    avgwt = pd.to_numeric(sub["AvgWt_kg"], errors="coerce").mean()
                    if not milestones:   # first appearance: real entry vs in-flight
                        ev = ("Seawater entry (TranOG)"
                              if pd.notna(avgwt) and avgwt < 0.6
                              else "In-flight at forecast start")
                    else:
                        ev = f"→ {tier}"
                    milestones.append({
                        "Week": wk, "Event": ev,
                        "Systems": ", ".join(sorted(set(str(s) for s in sub["System"]))),
                        "AvgWt (kg)": round(float(avgwt), 2) if pd.notna(avgwt) else None,
                        "Tanks": int(sub["Tank"].nunique()),
                    })
        h = hv.get(str(b))
        if h:
            milestones.append({"Week": f"{h['first']}–{h['last']}", "Event": "Harvest",
                               "Systems": "→ harvest",
                               "AvgWt (kg)": round(h["avg_wt"], 2), "Tanks": None})
        plans.append({"Batch": str(b), "SW_entry": weeks[0] if weeks else "—",
                      "Peak_tanks": peak_tanks,
                      "Harvest_window": (f"{h['first']}–{h['last']}" if h else "—"),
                      "HOG_t": round(h["hog_t"], 0) if h else 0.0,
                      "milestones": milestones})
    plans.sort(key=lambda p: p["SW_entry"])
    return plans


def _harvest_mode_label(config_dir) -> str:
    """Short description of the harvest controller mode in a config dir, so the
    Results view can always say WHICH run is on screen (keeping the correct data)."""
    import yaml
    try:
        c = yaml.safe_load(open(Path(config_dir) / "control.yaml"))
    except Exception:  # noqa: BLE001
        return "current app config"
    if c.get("harvest_level_load"):
        return (f"level-load ON (K={c.get('harvest_smooth_lookahead_weeks')}, "
                f"setpoint={c.get('harvest_setpoint_lookahead_weeks')})")
    return "level-load OFF (default controller)"


def _quick_viz(r):
    """Inline visualization of a forecast result (used in the Optimize tab so the
    optimized run can be visualized without switching modes). Charts the harvest-
    flattening + facility biomass straight from the parsed result."""
    he = pd.DataFrame(r.get("harvest_events") or [])
    bl = pd.DataFrame(r.get("batch_locations") or [])
    if not he.empty and "Week" in he and "Count" in he:
        hw = (he.groupby("Week", as_index=False)["Count"].sum()
                .sort_values("Week"))
        fig = px.bar(hw, x="Week", y="Count", title="Harvest — fish per week")
        fig.add_hline(y=55000, line_dash="dot", annotation_text="55k cap")
        fig.update_layout(height=340, xaxis_title="", yaxis_title="fish")
        st.plotly_chart(fig, use_container_width=True)
    if not bl.empty and "Week" in bl and "Biomass_kg" in bl:
        bw = (bl.groupby("Week", as_index=False)["Biomass_kg"].sum()
                .sort_values("Week"))
        bw["Biomass_t"] = bw["Biomass_kg"] / 1000.0
        fig2 = px.line(bw, x="Week", y="Biomass_t",
                       title="Facility biomass per week (t)")
        fig2.add_hline(y=3900, line_dash="dot", annotation_text="3.9M cap")
        fig2.update_layout(height=340, xaxis_title="", yaxis_title="tonnes")
        st.plotly_chart(fig2, use_container_width=True)


def _optimizer():
    st.header("🧭 Optimize — multi-objective")
    st.caption(
        "Sweeps the controller knobs and ranks variants on a **selectable** "
        "objective. The goal is to **walk the line**: hold biomass and harvest "
        "close to their limits AND flat (no lumps, no breaches), with feed and "
        "handling minimized — gated on conservation. The transfer/density trade "
        "is real (relieving density adds transfers), so you pick the emphasis; "
        "nothing is auto-decided. Conservation-failing variants are rejected."
    )

    _cfg_ok = _config_ready() and _scenario_ready()
    _pr_ok = pr is not None and pr["ok"]
    if not _cfg_ok:
        st.info("No config yet — set it up in **Configure** first.")
        return
    if not _pr_ok:
        st.info("Upload a valid **ProductionReport** in the sidebar first.")
        return

    from forecast.config_io import load_control, control_to_dict
    _render_active_config(
        control_to_dict(load_control(CONFIG_DIR)),
        "ℹ️ Base configuration — the search tunes knobs ON TOP of this")

    _hist = optimize.read_run_log(n=15)
    if _hist:
        with st.expander(f"📜 Recent auto-optimize runs ({len(_hist)}) — settings used + results",
                         expanded=False):
            _rows = []
            for h in reversed(_hist):   # newest first
                mt = h.get("metrics", {}) or {}
                kb = h.get("winning_knobs") or {}
                _rows.append({
                    "When": h.get("ts", ""),
                    "Method": h.get("method", ""),
                    "Emphasis": h.get("emphasis", ""),
                    "Winning knobs": ", ".join(f"{k}={v}" for k, v in kb.items()) or "(baseline)",
                    "Hot spot": mt.get("system_peak"),
                    "Feed": mt.get("feed_load"),
                    "Wks>55k": mt.get("weeks_over_harvest_cap"),
                    "Saved": "✓" if h.get("saved_to_config") else "",
                    "Dropped": h.get("dropped"),
                })
            st.dataframe(pd.DataFrame(_rows), hide_index=True, use_container_width=True)
            st.caption("Each Auto-optimize run is logged to `optimize_history.jsonl` — "
                       "the settings used and what it produced, kept across sessions.")

    emphasis = st.radio("Objective emphasis", list(optimize.EMPHASIS_PRESETS.keys()),
                        horizontal=True,
                        help="Re-scoring is instant — change this after a sweep "
                             "without re-running.")
    custom = None
    with st.expander("Advanced: custom weights"):
        st.caption("Override the preset (all 'less is better'). 0 drops a component.")
        base = optimize.EMPHASIS_PRESETS[emphasis]
        cols = st.columns(4)
        custom = {}
        for i, comp in enumerate(optimize.COMPONENTS):
            with cols[i % 4]:
                custom[comp] = st.number_input(comp, min_value=0.0, max_value=10.0,
                                               value=float(base.get(comp, 0.0)), step=0.5,
                                               key=f"w_{comp}")
        if st.checkbox("Use custom weights", key="use_custom_w"):
            pass
        else:
            custom = None

    method = st.radio(
        "Search method",
        ["Quick grid", "Full grid", "Deep search (finds combos)",
         "Grid + Deep (best of both)"],
        horizontal=True,
        help="TWO search algorithms, offered as FOUR choices. GRID (Quick = 4 configs, "
             "Full = 14) enumerates a hand-picked list — fast and broad, but mostly one "
             "knob at a time, so it misses COMBINATIONS. DEEP SEARCH is a coordinate "
             "descent that tunes one knob at a time and FINDS combinations (~15–30 runs). "
             "GRID + DEEP runs the full grid, then deep-searches FROM the grid's best and "
             "keeps the global best of both — grid explores, descent exploits (most "
             "thorough, ~30–45 runs; what Auto-optimize uses). The emphasis above guides "
             "deep/combined.")
    combined = method.startswith("Grid +")
    deep = method.startswith("Deep")
    if combined:
        st.write(f"**Grid + Deep** — the full grid, then coordinate descent from its "
                 f"best, guided by the **{emphasis}** emphasis (~30–45 runs). Returns "
                 f"the best of both methods. Config never modified.")
    elif deep:
        st.write(f"**Deep search** — greedy local search guided by the **{emphasis}** "
                 f"emphasis (~15–30 runs). Finds knob COMBINATIONS. Config never modified.")
    else:
        grid = optimize.opt_grid_for(method == "Quick grid")
        n = len(grid)
        st.write(f"**{method}** — runs the forecast **{n} times** "
                 f"(~{n * 90 // 60}–{max(1, n * 100 // 60)} min). Config never modified.")
    _c1, _c2 = st.columns(2)
    _run_opt = _c1.button("▶ Run optimization", type="primary", use_container_width=True)
    _auto_opt = _c2.button(
        "🤖 Auto-optimize & run", use_container_width=True,
        help="One click: find the best config (this method + emphasis), then run the "
             "FULL forecast with it and load it into the tabs. The winning knobs are "
             "validated TOGETHER, so it's safe to apply them as a set.")
    _auto_save = st.checkbox(
        "When auto-optimizing, save the winning knobs to config", value=True,
        key="auto_save_cfg",
        help="Writes the best knobs into config/control.yaml so future normal runs use "
             "them. Uncheck to just produce the optimized run without changing config.")
    if _run_opt or _auto_opt:
        work = Path(tempfile.mkdtemp(prefix="as_opt_in_"))
        in_path = work / (uploaded.name or "input.xlsm")
        in_path.write_bytes(uploaded.getvalue())
        bar = st.progress(0.0, text="Starting…")
        _prog = lambda i, m, label: bar.progress(  # noqa: E731
            min(0.98, i / m) if m else min(0.95, i / 40.0),
            text=f"[{i}{'/' + str(m) if m else ''}] {label} …")
        _w = optimize.weights_for(emphasis, custom)
        try:
            if combined:
                results = optimize.deep_search_combined(
                    str(in_path), str(CONFIG_DIR), str(SCENARIO_DIR),
                    emphasis=emphasis, weights=_w, progress=_prog)
            elif deep:
                results = optimize.coordinate_descent(
                    str(in_path), str(CONFIG_DIR), str(SCENARIO_DIR),
                    emphasis=emphasis, weights=_w, progress=_prog)
            else:
                results = optimize.sweep(
                    str(in_path), str(CONFIG_DIR), str(SCENARIO_DIR), grid=grid,
                    progress=_prog)
        except Exception as e:  # noqa: BLE001
            bar.empty()
            st.error(f"Optimization failed: {e}")
            st.code(traceback.format_exc())
            return
        bar.progress(1.0, text="Done")
        st.session_state["_opt_results"] = results
        if _auto_opt:
            # AUTO: pick the validated best, run the FULL forecast with it, load it
            # into the viz tabs, and (optionally) persist the winning knobs.
            _rec0 = optimize.recommend(results, emphasis=emphasis, weights=_w)
            _best0 = next((v for v in results if v.label == _rec0.best_label), results[0])
            _knobs = optimize.overrides_yaml(_best0.overrides).replace("\n", " · ") or "baseline"
            with st.spinner(f"Auto-optimize — running the full forecast with {_knobs} …"):
                try:
                    _tmpcfg = optimize.config_dir_with_overrides(str(CONFIG_DIR), _best0.overrides)
                    _res = _run_with_workbook_bytes(
                        uploaded.getvalue(), uploaded.name,
                        config_dir=_tmpcfg, scenario_dir=str(SCENARIO_DIR))
                except Exception as e:  # noqa: BLE001
                    st.error(f"Auto-optimize run failed: {e}")
                    _res = {"ok": False}
            if _res.get("ok"):
                _res["_run_label"] = "Auto-optimized — " + _knobs
                st.session_state.result = _res
                _saved = bool(_auto_save and _best0.overrides)
                if _saved:
                    optimize.save_overrides_to_config(str(CONFIG_DIR), _best0.overrides)
                    _clear_all_editor_state()
                # Log this run (settings + results) to optimize_history.jsonl so
                # there's a durable record of what was run and what it produced.
                from datetime import datetime as _dt
                optimize.append_run_log(optimize.make_run_record(
                    _best0, method, emphasis,
                    ts=_dt.now().isoformat(timespec="seconds"),
                    saved=_saved, source="auto-optimize (app)"))
                st.success(
                    f"🤖 Auto-optimized → **{_best0.label}** ({_knobs})"
                    + (" · **saved to config**" if _saved else " · config unchanged")
                    + ". Loaded into the **Run forecast** tabs. *(logged to history)*")

    results = st.session_state.get("_opt_results")
    if not results:
        return

    # Re-score instantly against the currently selected emphasis (no re-run).
    rec = optimize.recommend(results, emphasis=emphasis,
                             weights=optimize.weights_for(emphasis, custom))
    if rec.is_capacity_bound:
        st.warning(f"**Capacity-bound:** {rec.text}")
    else:
        st.success(f"**Recommendation:** {rec.text}")

    # ---- Feed the recommendation back into the forecast ----
    best = next((v for v in results if v.label == rec.best_label), results[0])
    with st.container(border=True):
        st.markdown(f"**Apply & verify — `{best.label}`**")
        st.caption("These are control-knob overrides — the same knobs a normal run "
                   "reads. Paste them into Configure → Control to keep them, or run a "
                   "full forecast now to verify the optimized result end-to-end.")
        st.code(optimize.overrides_yaml(best.overrides) or "# baseline", language="yaml")
        _bcol1, _bcol2 = st.columns(2)
        with _bcol2:
            if best.overrides and st.button("💾 Save these knobs to my config",
                                            key="opt_save", use_container_width=True):
                optimize.save_overrides_to_config(str(CONFIG_DIR), best.overrides)
                _clear_all_editor_state()
                st.success("Saved to config — **Run forecast** and future runs now "
                           "use these knobs (no longer baseline).")
        _run_clicked_opt = _bcol1.button("▶ Run full forecast with these knobs",
                                         key="opt_apply_run", use_container_width=True)
        if _run_clicked_opt:
            with st.spinner("Running the full pipeline with the recommended knobs…"):
                try:
                    # Run via the SAME path as Run-forecast mode, against a temp
                    # config with the knobs applied — so the result populates the
                    # full visualization tabs AND the download, not just metrics.
                    tmpcfg = optimize.config_dir_with_overrides(str(CONFIG_DIR), best.overrides)
                    result = _run_with_workbook_bytes(
                        uploaded.getvalue(), uploaded.name,
                        config_dir=tmpcfg, scenario_dir=str(SCENARIO_DIR))
                    if result.get("ok"):
                        result["_run_label"] = ("Optimized — "
                            + optimize.overrides_yaml(best.overrides).replace("\n", " · "))
                        st.session_state.result = result  # lights up Run-forecast tabs
                        m, dropped, overprod = optimize.metrics_from_workbook(
                            result["output_path"],
                            optimize._harvest_cap(str(CONFIG_DIR), best.overrides))
                        st.session_state["_opt_run"] = {
                            "dropped": dropped, "overprod": overprod,
                            "cv": m.harvest_var, "over": m.weeks_over_harvest_cap,
                        }
                    else:
                        st.error(f"Run failed: {result.get('error', 'unknown')}")
                except Exception as e:  # noqa: BLE001
                    st.error(f"Run failed: {e}")
                    st.code(traceback.format_exc())
        run_out = st.session_state.get("_opt_run")
        if run_out and "result" in st.session_state and st.session_state.result.get("ok"):
            r = st.session_state.result
            ok = run_out["dropped"] == 0 and run_out["overprod"] == 0
            cc1, cc2, cc3 = st.columns(3)
            cc1.metric("Conservation", "PASS ✓" if ok else "FAIL ✗",
                       help=f"{run_out['dropped']} dropped / {run_out['overprod']} over-produced")
            cc2.metric("Harvest CV", f"{run_out['cv']:.3f}")
            cc3.metric("Weeks over 55k", run_out["over"])
            st.download_button(
                "⬇ Download optimized forecast workbook",
                data=r["output_bytes"], file_name="Forecast_optimized.xlsm",
                mime="application/vnd.ms-excel.sheet.macroenabled.12",
                use_container_width=True)
            # Visualize inline — no mode-switch needed (the harvest chart is the
            # level-loading result you want to see).
            st.markdown("**Visualize this run**")
            _quick_viz(r)
            st.caption("For the full interactive tabs (Per-Batch, Yearly, Plan, "
                       "occupancy heatmap), switch **Mode → Run forecast** — this "
                       "run is already loaded there. Or open the downloaded "
                       "workbook in Excel for every report sheet.")

    df = _opt_table(results).sort_values("Score")

    def _hl(row):
        if row["Variant"] == rec.best_label:
            return ["background-color: #d7f0d7"] * len(row)
        if "FAIL" in str(row["Conservation"]):
            return ["color: #999"] * len(row)
        return [""] * len(row)

    st.dataframe(df.style.apply(_hl, axis=1), use_container_width=True, hide_index=True)

    # ---- Pareto trade-off map: the feed <-> harvest tension across variants ----
    st.subheader("Trade-off map — feed/biomass vs harvest")
    st.caption(
        "Every knob setting plotted by its two competing cap pressures: per-system "
        "feed/biomass over-cap (x) vs weeks over the 55k harvest cap (y). "
        "**Lower-left is best** — both caps held. The lower-left envelope is the "
        "Pareto frontier; your operating point is a choice along it. E.g. "
        "`tran_og=3` slides left on feed but up on harvest, `baseline` the reverse — "
        "so you can SEE the trade instead of discovering it after a run."
    )
    pdf = df.copy()
    pdf["Kind"] = [
        "Recommended" if v == rec.best_label
        else ("Rejected" if "FAIL" in str(c) else "Variant")
        for v, c in zip(pdf["Variant"], pdf["Conservation"])
    ]
    fig = px.scatter(
        pdf, x="Sys_over-cap", y="Wks_over_55k", text="Variant", color="Kind",
        color_discrete_map={"Recommended": "#2e7d32", "Rejected": "#bbbbbb",
                            "Variant": "#1f77b4"},
        title="Operating-point trade-off (lower-left = both caps held)",
    )
    fig.update_traces(textposition="top center", marker=dict(size=11))
    fig.update_layout(height=430,
                      xaxis_title="Per-system feed/biomass over-cap (fraction)",
                      yaxis_title="Weeks over 55k harvest cap")
    st.plotly_chart(fig, use_container_width=True)

    best = next((v for v in results if v.label == rec.best_label), results[0])
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Component scores — recommended")
        comp_df = pd.DataFrame(
            [{"Component": c, "Normalized": round(best.norm.get(c, 0.0), 3)}
             for c in optimize.COMPONENTS])
        fig = px.bar(comp_df, x="Normalized", y="Component", orientation="h",
                     title=f"{best.label} (lower = better)")
        st.plotly_chart(fig, use_container_width=True)
    with c2:
        st.subheader("Transfers by type — recommended")
        tt = best.metrics.transfers_by_type
        st.caption("TranOG + Transfer are structural; Grade is the discretionary "
                   "part the rebalancer budgets add (the handling/density trade).")
        st.dataframe(pd.DataFrame([{"Type": k, "Fish moved": round(v)} for k, v in tt.items()]),
                     use_container_width=True, hide_index=True)

    # Per-system biomass peak/CV for the recommended variant.
    ps = best.metrics.per_system
    if ps:
        st.subheader("Per-system biomass — recommended")
        psd = pd.DataFrame([
            {"System": s, "Mean_kg": round(d["mean"]), "Peak_kg": round(d["peak"]),
             "CV": round(d["cv"], 3),
             "Peak/cap": round(d["peak"] / d["cap"], 3) if d.get("cap") else None}
            for s, d in sorted(ps.items())])
        st.dataframe(psd, use_container_width=True, hide_index=True)


if app_mode.startswith("Configure"):
    _config_editor()
    st.stop()

if app_mode.startswith("Tune"):
    _tuner()
    st.stop()

if app_mode.startswith("Optimize"):
    _optimizer()
    st.stop()


# ============================================================
# Run the pipeline when the button is clicked
# ============================================================

if run_clicked and uploaded is not None:
    _method = ("global_optimal" if (_is_global and _global_optimal)
               else "global" if _is_global else "controller")
    _spin = ("Running GLOBAL OPTIMAL (CP-SAT whole-horizon solve) — this takes "
             f"~{int(_cpsat_time)}s plus setup, please wait..."
             if _method == "global_optimal"
             else "Running GLOBAL (precalculated L1→L3) planner — LP per week, slower..."
             if _is_global else "Running forecast pipeline...")
    with st.status(_spin, expanded=True) as status:
        st.write("Config + scenario from the app; ProductionReport from upload...")
        result = _run_with_workbook_bytes(
            uploaded.getvalue(), uploaded.name,
            config_dir=str(CONFIG_DIR), scenario_dir=str(SCENARIO_DIR),
            method=_method, cpsat_time=_cpsat_time,
        )
        if result["ok"]:
            st.write(
                f"✓ Pipeline complete in {result['elapsed']:.1f}s — "
                f"{result['violations']} violations, "
                f"worst {result['worst_density']:.1f} kg/m³"
            )
            status.update(label="✓ Forecast complete", state="complete")
            _mlabel = ("Global (precalculated L1→L3)" if _is_global
                       else f"Controller — {_harvest_mode_label(CONFIG_DIR)}")
            result["_run_label"] = _mlabel
            st.session_state.result = result
        else:
            st.error(f"Pipeline failed: {result.get('error', 'unknown')}")
            if result.get("traceback"):
                st.code(result["traceback"])
            if result.get("stdout"):
                with st.expander("Console output (stdout)", expanded=False):
                    st.code(result["stdout"], language="text")
            status.update(label="✗ Pipeline failed", state="error")
            # Store the failure result so the sidebar can show diagnostics.
            st.session_state.result = result


# ============================================================
# Results view
# ============================================================

if "result" in st.session_state and st.session_state.result.get("ok"):
    r = st.session_state.result

    # Provenance — always show WHICH run is on screen (keep the correct data).
    st.caption(f"📋 Showing: **{r.get('_run_label', 'forecast run')}**")
    if r.get("config_used"):
        _render_active_config(r["config_used"],
                              "ℹ️ Configuration this run used")

    # ---- KPIs + prominent download button ----
    top_kpi, top_dl = st.columns([3, 1])
    with top_kpi:
        st.subheader("Summary")
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Violations", r["violations"], help="Tanks where density > 95 kg/m³")
        k2.metric("Worst density", f"{r['worst_density']:.1f} kg/m³",
                  help="Highest per-tank density across the horizon")
        k3.metric("Total harvest", f"{r['harvest_kg']/1000:,.1f} t",
                  help="Sum of all harvest events across the horizon")
        k4.metric("Run time", f"{r['elapsed']:.1f}s")
    with top_dl:
        st.subheader("Output")
        st.download_button(
            label="⬇ Download workbook",
            data=r["output_bytes"],
            file_name=r["output_name"],
            mime="application/vnd.ms-excel.sheet.macroenabled.12",
            use_container_width=True,
            type="primary",
        )
        if r.get("output_path"):
            st.caption(f"Saved to:\n`{r['output_path']}`")

    # ---- Tabs ----
    bl = r["batch_locations"]
    bl_df = pd.DataFrame(bl) if bl else pd.DataFrame()
    he_df = pd.DataFrame(r["harvest_events"]) if r["harvest_events"] else pd.DataFrame()
    bio_df = pd.DataFrame(r.get("biology_projection", []))

    tab_over, tab_batch, tab_period, tab_harvest, tab_yearly, tab_plan = st.tabs([
        "Overview",
        "Per-Batch",
        "Period Summary",
        "Harvest",
        "Yearly",
        "Plan",
    ])

    # ============================================================
    # Tab 1: Overview — Advisory + occupancy heatmap
    # ============================================================
    with tab_over:
        st.subheader("Advisory")
        if r["advisory_summary"]:
            col_table, col_detail = st.columns([1, 2])
            with col_table:
                st.caption("Issues by category")
                st.dataframe(
                    pd.DataFrame(r["advisory_summary"]),
                    hide_index=True, use_container_width=True,
                )
            with col_detail:
                st.caption("Details (expand each category)")
                entries = r["advisory_entries"]
                by_cat: dict[str, list] = defaultdict(list)
                for e in entries:
                    by_cat[e["Category"]].append(e["Detail"])
                for cat in sorted(by_cat, key=lambda c: -len(by_cat[c])):
                    with st.expander(f"{cat} ({len(by_cat[cat])})"):
                        for d in by_cat[cat][:50]:
                            st.text(d)
                        if len(by_cat[cat]) > 50:
                            st.caption(f"… and {len(by_cat[cat]) - 50} more")
        else:
            st.info("No advisory entries — clean run.")

        st.subheader("Tank occupancy over time")
        if not bl_df.empty:
            df = bl_df.copy()
            df["TankLabel"] = df.apply(lambda r: f"{r['System']}-{r['Tank']}", axis=1)
            tank_order = sorted(
                df["TankLabel"].unique(),
                key=lambda t: (t.split("-")[0], int(t.split("-")[1]) if t.split("-")[1].isdigit() else 0),
            )
            weeks = sorted(df["Week"].dropna().unique())
            density_pivot = df.pivot_table(
                index="TankLabel", columns="Week",
                values="Density_kg_m3", aggfunc="first",
            ).reindex(index=tank_order, columns=weeks)
            batch_pivot = df.pivot_table(
                index="TankLabel", columns="Week",
                values="Batch", aggfunc="first",
            ).reindex(index=tank_order, columns=weeks)
            # Severity-honest scale: span the TRUE worst density (clamping at 130
            # hid 3.8x spikes as ordinary red). Hard color break at the 95 cap;
            # over-cap tanks deepen toward dark red as they get worse.
            _dv = pd.to_numeric(df["Density_kg_m3"], errors="coerce")
            vmax = max(130.0, float(_dv.max()) if _dv.notna().any() else 130.0)
            CAP = 95.0
            c_lo, c_cap = 80.0 / vmax, CAP / vmax
            colorscale = sorted({
                0.0: "#f0f0f0",          # empty / ~zero
                min(c_lo, c_cap * 0.5): "#a8d5a8",   # green, under target
                max(0.0, c_cap - 1e-3): "#f5d49a",   # amber, just under cap
                c_cap: "#e8615e",        # red AT the 95 cap
                1.0: "#7a0d0b",          # dark red at the worst observed
            }.items())
            # 6N rows are depuration/purge — empty cells there are NOT free
            # growout capacity. Mark them so they aren't read as availability.
            disp_y = [f"{t}  ⛔purge" if t.startswith("OG6N") else t
                      for t in tank_order]
            fig = px.imshow(
                density_pivot.values,
                x=weeks, y=disp_y,
                color_continuous_scale=[(p, c) for p, c in colorscale],
                range_color=[0, vmax],
                labels=dict(x="Week", y="Tank", color="Density (kg/m³)"),
                aspect="auto",
            )
            customdata = []
            for tl in tank_order:
                row_cd = []
                for wk in weeks:
                    bid = batch_pivot.loc[tl, wk] if wk in batch_pivot.columns else None
                    d = density_pivot.loc[tl, wk] if wk in density_pivot.columns else None
                    row_cd.append([str(bid) if bid else "—",
                                   f"{d:.1f}" if isinstance(d, (int, float)) else "—"])
                customdata.append(row_cd)
            fig.update_traces(
                customdata=customdata,
                hovertemplate=(
                    "Tank: %{y}<br>Week: %{x}<br>Batch: %{customdata[0]}"
                    "<br>Density: %{customdata[1]} kg/m³<extra></extra>"
                ),
            )
            fig.update_layout(height=600, margin=dict(l=80, r=20, t=20, b=40))
            st.plotly_chart(fig, use_container_width=True)
            st.caption(
                f"Color = per-tank density. Green = under target, amber = "
                f"approaching the 95 kg/m³ cap, red = over cap, deepening to "
                f"dark red at the worst observed ({vmax:.0f} kg/m³) — so a 3.8x "
                f"spike no longer looks like a mild one. Rows tagged ⛔purge are "
                f"OG6N depuration tanks: empty cells there are NOT free growout "
                f"capacity. Hover any cell for the batch and exact density."
            )

            # ---- Per-system biomass + feed over time ----
            st.subheader("Per-system biomass + feed")
            sys_bio = (
                bl_df.assign(Biomass_kg=bl_df["Biomass_kg"].fillna(0))
                .groupby(["System", "Week"]).agg(
                    Biomass_kg=("Biomass_kg", "sum"),
                ).reset_index().sort_values(["Week", "System"])
            )

            c1, c2 = st.columns(2)
            with c1:
                fig = px.line(
                    sys_bio, x="Week", y="Biomass_kg", color="System",
                    markers=True,
                    title="Per-system biomass (kg) over time",
                )
                fig.update_layout(height=380, yaxis_title="kg",
                                  legend=dict(title="System"))
                st.plotly_chart(fig, use_container_width=True)
            with c2:
                # Per-system biomass as % of typical density-cap capacity
                # (avg tank volume × density cap × tank count per system).
                # Approximation: pull tank count per system from data.
                tanks_per_sys = bl_df.groupby("System")["Tank"].nunique().to_dict()
                # Avg cap-biomass per tank: 95 kg/m³ × 1720 m³ = 163,400 kg.
                CAP_PER_TANK = 95 * 1720
                sys_bio_pct = sys_bio.copy()
                sys_bio_pct["Cap_kg"] = sys_bio_pct["System"].map(
                    lambda s: tanks_per_sys.get(s, 0) * CAP_PER_TANK
                )
                sys_bio_pct["Pct_of_cap"] = (
                    sys_bio_pct["Biomass_kg"] / sys_bio_pct["Cap_kg"] * 100
                ).where(sys_bio_pct["Cap_kg"] > 0, 0)
                fig = px.line(
                    sys_bio_pct, x="Week", y="Pct_of_cap", color="System",
                    markers=True,
                    title="Per-system utilization (% of density-cap capacity)",
                )
                fig.add_hline(y=100, line_dash="dash", line_color="red",
                              annotation_text="100% (cap)")
                fig.add_hline(y=85, line_dash="dot", line_color="orange",
                              annotation_text="85% target")
                fig.update_layout(height=380, yaxis_title="% of cap",
                                  legend=dict(title="System"))
                st.plotly_chart(fig, use_container_width=True)

            st.caption(
                "Left: absolute biomass per system, summed across that "
                "system's tanks. Right: same as % of the system's "
                "density-cap capacity (95 kg/m³ × volume × tank count). "
                "Watch for systems pinned at 100% while others sit idle "
                "— that's the operational signal of imbalance."
            )

            # Per-system feed/day — REALIZED, read straight from the
            # SystemLimitsAudit sheet (the per-(system, week) feed the caps are
            # actually checked against). NOT BiologyProjection.Feed_kg_day, which
            # is the raw UNHARVESTED projection (fish growing along the curve,
            # ignoring harvest + caps) and spikes far above any realized
            # per-system feed (e.g. >10,000 vs a realized ~3,980 / 3,000 cap) —
            # the same projection-vs-realized gap fixed at the report layer in
            # 63dd6cc. This chart must mirror the realized plan, so it reads the
            # audit directly (also exact, not a biomass-share approximation).
            sys_feed_rows = []
            feed_cap_val = None
            from openpyxl import load_workbook as _lwb
            if r.get("output_path"):
                _wb = _lwb(r["output_path"], data_only=True, read_only=True)
                if "SystemLimitsAudit" in _wb.sheetnames:
                    _hdr = None
                    for _row in _wb["SystemLimitsAudit"].iter_rows(values_only=True):
                        if _hdr is None:
                            if _row and _row[0] == "Week":
                                _hdr = {h: j for j, h in enumerate(_row)}
                            continue
                        if not _row or _row[0] is None:
                            continue
                        _sys = _row[_hdr.get("System", 1)]
                        _fd = _row[_hdr.get("Feed_kg_day", 5)]
                        _fc = _row[_hdr.get("Feed_cap", 6)]
                        if _sys is None or _fd is None:
                            continue
                        sys_feed_rows.append({"System": str(_sys),
                                              "Week": str(_row[_hdr.get("Week", 0)]),
                                              "Feed_kg_day": float(_fd)})
                        if _fc:
                            feed_cap_val = float(_fc)
                _wb.close()
            if sys_feed_rows:
                sys_feed = pd.DataFrame(sys_feed_rows).sort_values(["Week", "System"])
                fig = px.line(
                    sys_feed, x="Week", y="Feed_kg_day", color="System",
                    markers=True,
                    title="Per-system feed (kg/day) over time — REALIZED",
                )
                if feed_cap_val:
                    fig.add_hline(y=feed_cap_val, line_dash="dash",
                                  line_color="red",
                                  annotation_text=f"{feed_cap_val:.0f} kg/day cap")
                fig.update_layout(height=380, yaxis_title="kg/day",
                                  legend=dict(title="System"))
                st.plotly_chart(fig, use_container_width=True)
                st.caption(
                    "REALIZED per-system feed from SystemLimitsAudit — the actual "
                    "fed plan (after harvest + FIFO), the exact series the feed "
                    "caps are checked against. NOT the unharvested biology "
                    "projection (which ignores harvest and spikes well past the "
                    "cap). Lines riding just under the dashed cap = leveled "
                    "correctly; brief crossings are the residual over-cap weeks."
                )

    # ============================================================
    # Tab 2: Per-Batch lifecycle
    # ============================================================
    with tab_batch:
        st.subheader("Per-batch trajectories")
        if bl_df.empty:
            st.info("No batch data to display.")
        else:
            # Aggregate per (Batch, Week): sum count + biomass.
            df = bl_df.copy()
            df["Biomass_kg"] = df["Biomass_kg"].fillna(0)
            df["Count"] = df["Count"].fillna(0)
            agg = df.groupby(["Batch", "Week"]).agg(
                Count=("Count", "sum"),
                Biomass_kg=("Biomass_kg", "sum"),
                MaxDensity=("Density_kg_m3", "max"),
                MeanDensity=("Density_kg_m3", "mean"),
                Tanks=("Tank", "nunique"),
            ).reset_index()
            agg["AvgWt_kg"] = (agg["Biomass_kg"] / agg["Count"]).where(agg["Count"] > 0, 0)
            batches = sorted(agg["Batch"].dropna().unique())
            default = ["B46", "B47"] if all(b in batches for b in ("B46", "B47")) else batches[:2]

            ctrl_l, ctrl_r = st.columns([2, 3])
            with ctrl_l:
                picked = st.multiselect(
                    "Batches", batches, default=default,
                    help="Pick one or more batches to compare trajectories.",
                )
            with ctrl_r:
                all_weeks = sorted(agg["Week"].dropna().unique())
                if len(all_weeks) >= 2:
                    wk_lo, wk_hi = st.select_slider(
                        "Period",
                        options=all_weeks,
                        value=(all_weeks[0], all_weeks[-1]),
                        help="Slide endpoints to zoom in on a specific window.",
                    )
                else:
                    wk_lo, wk_hi = all_weeks[0], all_weeks[-1] if all_weeks else (None, None)

            if not picked:
                st.info("Select at least one batch.")
            else:
                view = agg[
                    (agg["Batch"].isin(picked))
                    & (agg["Week"] >= wk_lo)
                    & (agg["Week"] <= wk_hi)
                ].sort_values(["Batch", "Week"])
                c1, c2 = st.columns(2)
                with c1:
                    fig = px.line(
                        view, x="Week", y="AvgWt_kg", color="Batch",
                        markers=True, title="Average weight per fish (kg)",
                    )
                    fig.update_layout(height=350, yaxis_title="kg/fish")
                    st.plotly_chart(fig, use_container_width=True)
                with c2:
                    fig = px.line(
                        view, x="Week", y="Biomass_kg", color="Batch",
                        markers=True, title="Total batch biomass (kg)",
                    )
                    fig.update_layout(height=350, yaxis_title="kg")
                    st.plotly_chart(fig, use_container_width=True)
                c3, c4 = st.columns(2)
                with c3:
                    fig = px.line(
                        view, x="Week", y="MaxDensity", color="Batch",
                        markers=True, title="Max per-tank density (kg/m³)",
                    )
                    fig.add_hline(y=95, line_dash="dash", line_color="red",
                                  annotation_text="cap")
                    fig.add_hline(y=95*0.85, line_dash="dot", line_color="orange",
                                  annotation_text="85% target")
                    fig.update_layout(height=350, yaxis_title="kg/m³")
                    st.plotly_chart(fig, use_container_width=True)
                with c4:
                    fig = px.line(
                        view, x="Week", y="Count", color="Batch",
                        markers=True, title="Fish count (end of week)",
                    )
                    fig.update_layout(height=350, yaxis_title="fish")
                    st.plotly_chart(fig, use_container_width=True)

                # Explicit weekly losses: mortality + cull events per
                # week, plotted at their own scale so they don't disappear
                # next to the much larger total fish count.
                if not bio_df.empty:
                    bv = bio_df[
                        (bio_df["Batch"].isin(picked))
                        & (bio_df["Week"] >= wk_lo)
                        & (bio_df["Week"] <= wk_hi)
                    ].copy()
                    bv["Mortality + Cull (fish)"] = (
                        bv["Mortality_count_wk"] + bv["Cull_count_wk"]
                    )
                    fig = px.line(
                        bv, x="Week", y="Mortality + Cull (fish)",
                        color="Batch", markers=True,
                        title="Weekly losses (mortality + scheduled culls)",
                    )
                    fig.update_layout(height=300, yaxis_title="fish lost / week")
                    st.plotly_chart(fig, use_container_width=True)
                    st.caption(
                        "Mortality from the per-week mortality table, plus "
                        "any cull events at scheduled DSI thresholds. Plotted "
                        "separately because per-week losses (~50–200 fish) "
                        "are tiny next to the 200k+ batch total — they "
                        "disappear in the Count chart above."
                    )

                with st.expander("Raw weekly table"):
                    st.dataframe(view, hide_index=True, use_container_width=True)

    # ============================================================
    # Tab 3: Period Summary — facility-wide weekly metrics
    # ============================================================
    with tab_period:
        st.subheader("Facility-wide weekly summary")
        if bl_df.empty:
            st.info("No batch data to display.")
        else:
            df = bl_df.copy()
            df["Biomass_kg"] = df["Biomass_kg"].fillna(0)
            wk_facility = df.groupby("Week").agg(
                FacilityBiomass_kg=("Biomass_kg", "sum"),
                ActiveTanks=("Tank", "nunique"),
                ActiveBatches=("Batch", "nunique"),
                MeanDensity=("Density_kg_m3", "mean"),
            ).reset_index().sort_values("Week")

            # Merge harvest per week (kg + count)
            if not he_df.empty:
                hw = he_df.groupby("Week").agg(
                    HarvestKg=("Gross_kg", "sum"),
                    HarvestCount=("Count", "sum"),
                ).reset_index()
                wk_facility = wk_facility.merge(hw, on="Week", how="left")
                wk_facility[["HarvestKg", "HarvestCount"]] = (
                    wk_facility[["HarvestKg", "HarvestCount"]].fillna(0)
                )

            c1, c2 = st.columns(2)
            with c1:
                fig = px.line(
                    wk_facility, x="Week", y="FacilityBiomass_kg",
                    markers=True, title="Facility biomass (kg)",
                )
                fig.add_hline(y=3_900_000, line_dash="dash", line_color="red",
                              annotation_text="Max Biomass cap (3,900 t)")
                fig.update_layout(height=350, yaxis_title="kg")
                st.plotly_chart(fig, use_container_width=True)
            with c2:
                if "HarvestKg" in wk_facility.columns:
                    fig = px.bar(
                        wk_facility, x="Week", y="HarvestKg",
                        title="Weekly harvest (kg, gross)",
                    )
                    fig.update_layout(height=350, yaxis_title="kg")
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("No harvest events.")

            c3, c4 = st.columns(2)
            with c3:
                fig = px.line(
                    wk_facility, x="Week", y="ActiveBatches",
                    markers=True, title="Active batches per week",
                )
                fig.update_layout(height=300, yaxis_title="count")
                st.plotly_chart(fig, use_container_width=True)
            with c4:
                fig = px.line(
                    wk_facility, x="Week", y="MeanDensity",
                    markers=True,
                    title="Mean per-tank density across facility (kg/m³)",
                )
                fig.add_hline(y=95, line_dash="dash", line_color="red",
                              annotation_text="cap")
                fig.update_layout(height=300, yaxis_title="kg/m³")
                st.plotly_chart(fig, use_container_width=True)

            with st.expander("Raw weekly table"):
                st.dataframe(wk_facility, hide_index=True, use_container_width=True)

    # ============================================================
    # Tab 4: Harvest Overview
    # ============================================================
    with tab_harvest:
        st.subheader("Harvest plan overview")
        if he_df.empty:
            st.info("No harvest events.")
        else:
            tot_kg = he_df["Gross_kg"].sum()
            tot_count = he_df["Count"].sum()
            avg_kg = tot_kg / tot_count if tot_count else 0
            k1, k2, k3 = st.columns(3)
            k1.metric("Total harvest", f"{tot_kg/1000:,.1f} t")
            k2.metric("Total fish", f"{tot_count:,.0f}")
            k3.metric("Avg weight at harvest", f"{avg_kg:.2f} kg")

            c1, c2 = st.columns(2)
            with c1:
                fig = px.bar(
                    he_df, x="Week", y="Gross_kg", color="Batch",
                    title="Harvest kg per week, stacked by batch",
                )
                fig.update_layout(height=400, yaxis_title="kg")
                st.plotly_chart(fig, use_container_width=True)
            with c2:
                fig = px.bar(
                    he_df, x="Week", y="Count", color="Batch",
                    title="Harvest count per week, stacked by batch",
                )
                fig.update_layout(height=400, yaxis_title="fish")
                st.plotly_chart(fig, use_container_width=True)

            # Avg harvest weight per week (weighted)
            hw = he_df.groupby("Week").agg(
                Count=("Count", "sum"),
                Gross_kg=("Gross_kg", "sum"),
            ).reset_index()
            hw["AvgWt_kg"] = (hw["Gross_kg"] / hw["Count"]).where(hw["Count"] > 0, 0)
            fig = px.line(
                hw, x="Week", y="AvgWt_kg", markers=True,
                title="Average harvest weight per week (kg/fish)",
            )
            fig.add_hline(y=3.5, line_dash="dot", line_color="orange",
                          annotation_text="Min harvest weight (3.5 kg)")
            fig.update_layout(height=300, yaxis_title="kg/fish")
            st.plotly_chart(fig, use_container_width=True)

            # Monthly rollup (sales planning): HOG tonnes + count per calendar
            # month, derived from each event's ISO week.
            import datetime as _dt

            def _wk_to_month(wk):
                try:
                    y, w = int(str(wk)[:4]), int(str(wk)[6:8])
                    return _dt.date.fromisocalendar(y, w, 1).strftime("%Y-%m")
                except Exception:
                    return None

            hm = he_df.copy()
            hm["Month"] = hm["Week"].map(_wk_to_month)
            hm = hm.dropna(subset=["Month"])
            if not hm.empty:
                mo = hm.groupby("Month").agg(
                    HOG_t=("HOG_kg", lambda s: s.sum() / 1000.0),
                    Count=("Count", "sum"),
                ).reset_index()
                st.markdown("**Monthly harvest (sales planning)**")
                cc1, cc2 = st.columns(2)
                with cc1:
                    fig = px.bar(mo, x="Month", y="HOG_t", text_auto=".0f",
                                 title="HOG tonnes harvested per month")
                    fig.update_layout(height=320, yaxis_title="t HOG", xaxis_title="")
                    st.plotly_chart(fig, use_container_width=True)
                with cc2:
                    fig = px.bar(mo, x="Month", y="Count", text_auto=".2s",
                                 title="Fish harvested per month")
                    fig.update_layout(height=320, yaxis_title="fish", xaxis_title="")
                    st.plotly_chart(fig, use_container_width=True)

            with st.expander("Raw harvest events"):
                st.dataframe(he_df, hide_index=True, use_container_width=True)

    # ============================================================
    # Tab 5: Yearly — facility-wide per-year trends
    # ============================================================
    with tab_yearly:
        st.subheader("Yearly trends (facility-wide)")
        yr = pd.DataFrame(r.get("yearly", []))
        if yr.empty:
            st.info("No yearly summary in this workbook.")
        else:
            st.caption("Per-calendar-year rollup. Partial first/last years reflect "
                       "the forecast horizon, not full calendar years.")
            st.dataframe(yr, hide_index=True, use_container_width=True)
            yr["Year"] = yr["Year"].astype(str)
            charts = [
                ("Harvest_HOG (t)", "Harvest (HOG tonnes) per year"),
                ("Feed (t)", "Feed (tonnes) per year"),
                ("Peak_Biomass (t)", "Peak facility biomass (tonnes) per year"),
                ("Harvest_Count (fish)", "Harvest count (fish) per year"),
            ]
            cols = st.columns(2)
            for i, (col, title) in enumerate(charts):
                if col not in yr.columns:
                    continue
                fig = px.bar(yr, x="Year", y=col, title=title, text_auto=True)
                fig.update_layout(height=320, xaxis_title="")
                cols[i % 2].plotly_chart(fig, use_container_width=True)

    # ============================================================
    # Tab 6: Plan — per-batch plan summary + density risk
    # ============================================================
    with tab_plan:
        # Production-flow template (TransferTemplate Section A) — the canonical
        # journey every batch follows through the seawater conveyor.
        ft = pd.DataFrame(r.get("flow_template", []))
        if not ft.empty:
            st.subheader("Production flow — the canonical batch journey")
            st.caption(
                "Every batch follows this seawater journey; only the timing "
                "shifts with stocking date, the shape is fixed: FW → OG1/2 "
                "nursery → the 1 kg move-lock → grow-out fan-out across systems "
                "→ finishing/depuration in the top systems → harvest drain."
            )
            st.dataframe(ft, hide_index=True, use_container_width=True)
            st.divider()

        st.subheader("Batch plan summary")
        pf = pd.DataFrame(r.get("plan_summary", []))
        if pf.empty:
            st.info("No TransferTemplate plan summary in this workbook.")
        else:
            st.caption("When each batch enters seawater, its tank footprint, density "
                       "risk, and harvest window. Rows flagged OVER CAP peak above the "
                       "density cap at some point in their life.")
            status_col = next((c for c in pf.columns if c.startswith("Density_Status")), None)
            peak_col = next((c for c in pf.columns if c.startswith("Peak_Density")), None)
            n_over = int((pf[status_col] == "OVER CAP").sum()) if status_col else 0
            c1, c2, c3 = st.columns(3)
            c1.metric("Batches", len(pf))
            c2.metric("Density risk (OVER CAP)", n_over)
            if peak_col:
                worst = pd.to_numeric(pf[peak_col], errors="coerce").max()
                c3.metric("Worst peak density", f"{worst:.2f}× cap" if pd.notna(worst) else "—")

            def _hl(row):
                over = status_col and row.get(status_col) == "OVER CAP"
                return ['background-color: #fde2e2' if over else '' for _ in row]
            st.dataframe(pf.style.apply(_hl, axis=1), hide_index=True, use_container_width=True)

            if peak_col and status_col:
                pf2 = pf.copy()
                pf2["_peak"] = pd.to_numeric(pf2[peak_col], errors="coerce")
                pf2 = pf2.dropna(subset=["_peak"])
                if not pf2.empty:
                    fig = px.bar(pf2, x="Batch", y="_peak", color=status_col,
                                 color_discrete_map={"OVER CAP": "#e45756", "OK": "#54a24b"},
                                 title="Peak density per batch (× cap)")
                    fig.add_hline(y=1.0, line_dash="dot", line_color="red",
                                  annotation_text="density cap")
                    fig.update_layout(height=360, yaxis_title="× cap", xaxis_title="")
                    st.plotly_chart(fig, use_container_width=True)

        # ---- Per-batch plan: where each batch is + how it got there ----
        st.divider()
        st.subheader("Per-batch plan — journey + milestones")
        bplans = _derive_batch_plans(bl_df, he_df)
        if not bplans:
            st.info("No batch-location data to build per-batch plans.")
        else:
            st.caption("Each batch's planned journey through the conveyor — a summary "
                       "header (entry, peak tanks, harvest window, HOG) plus the "
                       "milestone timeline (when it enters each tier, at what weight, "
                       "through to harvest). Pick a batch to review; download all to share.")
            hdr_df = pd.DataFrame([{k: p[k] for k in
                                    ("Batch", "SW_entry", "Peak_tanks", "Harvest_window", "HOG_t")}
                                   for p in bplans])
            st.dataframe(hdr_df, hide_index=True, use_container_width=True)
            pick = st.selectbox("Batch", [p["Batch"] for p in bplans], key="batchplan_pick")
            bp = next(p for p in bplans if p["Batch"] == pick)
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("SW entry", bp["SW_entry"])
            m2.metric("Peak tanks", bp["Peak_tanks"])
            m3.metric("Harvest window", bp["Harvest_window"])
            m4.metric("HOG (t)", f"{bp['HOG_t']:.0f}")
            st.dataframe(pd.DataFrame(bp["milestones"]), hide_index=True,
                         use_container_width=True)
            # Flat export (one row per batch-milestone) for sharing/review.
            rows = [{"Batch": p["Batch"], **m} for p in bplans for m in p["milestones"]]
            st.download_button(
                "⬇ Download all batch plans (CSV)",
                data=pd.DataFrame(rows).to_csv(index=False).encode(),
                file_name="batch_plans.csv", mime="text/csv")

    # ---- Run log (collapsed) ----
    with st.expander("Run log (console output)"):
        st.code(r["stdout"], language="text")
else:
    st.info(
        "Upload a workbook in the sidebar and click ▶ Run forecast to "
        "begin. The input workbook is never modified — output is written "
        "to a new file you can download."
    )
