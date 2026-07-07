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
    rows = [
        ("Feed leveling", "ON" if lv else "OFF",
         "spreads load off the hottest system → no per-system feed spikes"
         if lv else "density-only → per-system feed can spike"),
        ("Harvest smoother", "ON" if hl else "OFF",
         "holds the weekly harvest cap hard + pre-harvests → flat harvest"
         if hl else "reactive → harvest can be lumpy"),
        ("TranOG tanks / arrival", g("tran_og_default_tanks"),
         "more tanks = lower per-system feed but a tighter facility (spikier harvest)"),
        ("Biomass setpoint band", f"{(g('facility_biomass_deviation_pct') or 0) * 100:.2f}%",
         "how close to the FW-inclusive cap the harvest controller runs (the only soft "
         "margin; tighter = higher utilisation)"),
        ("Density target", f"{dt * 100:.0f}%",
         "packs each tank to this % of its density cap (lower = more headroom, "
         "fewer hot spots)"),
        ("Rebalancer budget", f"{g('rebalance_balance_budget')} moves/wk",
         "max fish-moves per week to relieve over-cap systems"),
        ("Placement engine", pm.upper(),
         "greedy heuristic + rebalancer (default)" if pm == "greedy"
         else "LP-guided LNS optimal-layout refinement"),
        ("FW auto-calibration", "ON" if bool(g("auto_calibrate_fw")) else "OFF",
         "FW growth is auto-adjusted so each batch hits its pre-cull transfer target "
         f"(clamped {g('auto_calibrate_fw_min', 0.5):.2f}–{g('auto_calibrate_fw_max', 1.5):.2f}) "
         "— a planning assumption, not a guarantee"
         if bool(g("auto_calibrate_fw"))
         else "FW growth uses each batch's configured FW_Correction (residuals shown in "
              "Diagnostics)"),
        ("Caps", f"biomass {g('max_biomass_kg', 0):,.0f} kg · "
                 f"feed {g('max_feed_per_day_kg', 0):,.0f} kg/day · "
                 f"harvest {g('max_harvest_per_week', 0):,.0f} fish/wk",
         "the hard limits the plan must respect"),
    ]
    # Surface the opt-in knobs only when they're engaged (off by default).
    if g("harvest_grade_to_min"):
        rows.append(("Grade-to-min harvest", "ON",
            "weeks short of the floor grade near-market tails into 6N to hold the min "
            "(small stays in source); net production-positive"))
    if (g("min_transfer_count") or 0) > 0:
        rows.append(("Min transfer size", f"{g('min_transfer_count'):,.0f} fish",
            "rebalancer won't split a smaller sub-group out of a tank"))
    return rows


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
    "min_transfer_count": "Min rebalancer transfer size (fish): the density/load balancer "
        "won't split a sub-group smaller than this OUT of a tank — the OUT-side mirror of "
        "min_tank_control. 0 = off. Trades fewer transfers for more MARGINAL density "
        "over-cap (the small moves do fine-grained relief); whole-tank consolidation "
        "moves are unaffected. Sweep knee ~5000 on the current config.",
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
        "auto-computed from realized growth. Only used when harvest_level_load is on. "
        "Good value = the sustainable average weekly harvest (between min and max).",
    "harvest_grade_to_min": "Grade-harvest to the floor (opt-in, OFF by default). On a "
        "6N purge week below min_harvest_per_week, peel just enough of the over-weight "
        "tail from near-market tanks (big → 6N purge; small stays in the source tank) to "
        "REACH the floor — honoring min_transfer_count + min_tank_control. An exception "
        "(only fires when short), not a rule. Measured net production-positive (more cap "
        "headroom, slightly higher avg harvest weight) while holding the harvest floor.",
    "sixn_level_drains": "Level the 6N purge drains (ON by default). Caps how "
        "full a 6N purge pair may get (at max_harvest_per_week) so weekly fills don't "
        "ACCUMULATE into one pair across its rotation — the root cause of 90-113k drain "
        "spikes that starve other pairs into sub-min troughs. Surplus waits in grow-out and "
        "fills the next thin pair, lifting its drain toward the floor so every week meets "
        "the harvest minimum. Only affects 6N PURGE mode.",
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
    "auto_calibrate_fw": "Auto-calibrate freshwater growth (OPT-IN, default off). "
        "Replaces each FW batch's FW_Correction with the value that lands its pre-cull "
        "avg weight EXACTLY on its TranOG target at transfer (the Suggested_FW_Correction "
        "shown in Diagnostics) — for incoming AND in-flight FW batches — so the FW "
        "calibration residuals go to ~0. NOTE: this makes the forecast ASSUME the growth "
        "needed to hit target (a planning assumption, NOT a guarantee the fish grow that "
        "fast); a correction > 1 means faster-than-nominal growth. Solved values are "
        "clamped to the [min, max] below, and any clamped (unreachable) batch is flagged.",
    "auto_calibrate_fw_min": "Lower clamp on the auto FW correction (only used when "
        "Auto-calibrate FW is on). A batch that would need a smaller correction is capped "
        "here and flagged in the log.",
    "auto_calibrate_fw_max": "Upper clamp on the auto FW correction (only used when "
        "Auto-calibrate FW is on). A batch that would need MORE growth than this is capped "
        "here and flagged as likely unreachable.",
}

# Friendly display labels for the Control editor (the raw field name stays the key).
_CONTROL_LABEL = {
    "forecast_start": "Forecast start (derived from PR)",
    "horizon_weeks": "Horizon (weeks)",
    "scenario_name": "Scenario name",
    "max_feed_per_day_kg": "Max feed / day (kg)",
    "max_biomass_kg": "Max facility biomass (kg)",
    "max_harvest_per_week": "Max harvest / week (fish)",
    "min_harvest_per_week": "Min harvest / week (fish)",
    "min_harvest_weight_g": "Min harvest weight (g)",
    "min_tank_control": "Force-empty floor (fish)",
    "min_transfer_count": "Min transfer size (fish)",
    "default_hog_yield": "Default HOG yield",
    "facility_biomass_deviation_pct": "Biomass setpoint band (R24)",
    "handling_mortality_pct": "Handling mortality (per transfer)",
    "sixn_growth": "Run 6N as grow-out",
    "sixn_production_start": "6N production start date",
    "sixn_transition_weeks": "6N transition fallow (weeks)",
    "sixn_level_drains": "Level 6N purge drains (on/off)",
    "tran_og_default_tanks": "TranOG default tanks",
    "global_buffer_pct": "System-cap buffer (R29)",
    "starvation_period_days": "In-place purge length (days)",
    "density_target_pct": "Density target (% of cap)",
    "rebalance_balance_budget": "Rebalancer moves / week",
    "rebalance_split_budget": "Split-pass moves / week",
    "rebalance_varqty_budget": "Variable-qty moves / week",
    "rebalance_level": "Load leveling (on/off)",
    "harvest_setpoint_lookahead_weeks": "Setpoint lookahead (INACTIVE)",
    "harvest_level_load": "Harvest smoother (on/off)",
    "harvest_smooth_lookahead_weeks": "Harvest smoother window K",
    "harvest_level_target": "Harvest level target (fish/wk)",
    "harvest_grade_to_min": "Grade-harvest to the floor",
    "placement_method": "Placement engine",
    "lns_max_moves": "LNS move budget",
    "auto_calibrate_fw": "Auto-calibrate FW to transfer target",
    "auto_calibrate_fw_min": "  ↳ FW correction clamp — min",
    "auto_calibrate_fw_max": "  ↳ FW correction clamp — max",
}


def _ctl_label(k: str) -> str:
    """Friendly Control-editor label for a knob (falls back to a prettified name)."""
    return _CONTROL_LABEL.get(k, k.replace("_", " ").capitalize())


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
                new[k] = st.checkbox(_ctl_label(k), value=v, help=_CONTROL_HELP.get(k))
            elif isinstance(v, int):
                new[k] = int(st.number_input(_ctl_label(k), value=int(v), step=1,
                                             help=_CONTROL_HELP.get(k)))
            elif isinstance(v, float):
                new[k] = float(st.number_input(_ctl_label(k), value=float(v),
                                               format="%.5f", help=_CONTROL_HELP.get(k)))
            else:
                new[k] = st.text_input(_ctl_label(k), value="" if v is None else str(v),
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


# ============================================================
# Manual override window editor (Run mode) — script week-by-week
# operations the forecast EXECUTES before the planner takes over.
# ============================================================

_MANUAL_COLS = ["week", "type", "batch", "from_tank", "to_tanks", "count", "notes"]


def _hydrate_state_from_upload(uploaded):
    """Hydrate a FacilityState from the uploaded PR (cached by content hash), so
    the editor can populate tank/batch context + dry-run validate events.
    Returns (state, fw_records)."""
    import hashlib
    import io
    data = uploaded.getvalue()
    ck = "_hydrated_" + hashlib.md5(data).hexdigest()
    if ck in st.session_state:
        return st.session_state[ck]
    from openpyxl import load_workbook
    from datetime import datetime as _dt, timedelta as _td
    from forecast.config_io import load_config
    from forecast.scenario_io import load_batches
    from forecast.production_report import read_production_report, hydrate_facility_state
    from forecast.state import FacilityState
    wb = load_workbook(io.BytesIO(data), data_only=True,
                       keep_vba=str(uploaded.name).lower().endswith(".xlsm"))
    control, tables, facility = load_config(CONFIG_DIR)
    batches = load_batches(SCENARIO_DIR)
    pc, og, fw = read_production_report(wb)
    fs = _dt(pc.year, pc.month, pc.day) + _td(days=1)
    # Mirror the real run: the PR closing date is the forecast start (run.py
    # DetectForecastStart). The faithful manual-event validation needs it on
    # control so its FW projection anchors to the same week the run will.
    control.forecast_start = fs
    state = FacilityState.from_facility_config(facility, today=fs.date())
    hydrate_facility_state(state, og, batches)
    # Run-time context so the editor can validate events FAITHFULLY (per-week
    # biology + the FW projection for fw_to_og), matching forecast/run.py.
    ctx = {
        "batch_by_id": {b.batch_id: b for b in batches},
        "tables": tables,
        "forecast_start": fs.date(),
        "control": control,
        "pr_closing": pc,
        "fw_records": fw,
    }
    st.session_state[ck] = (state, fw, ctx)
    return state, fw, ctx


def _manual_events_to_df_rows(events):
    rows = []
    for e in events:
        # Encode per-dest counts as "tank:count" so explicit / UNEQUAL counts
        # round-trip losslessly; a bare "tank" means None (split the `count`
        # column evenly across the bare tanks at run time).
        to_tanks = ",".join(
            (f"{d.tank}:{int(d.count)}" if d.count is not None else str(d.tank))
            for d in e.destinations)
        count = e.count if e.type not in ("og_transfer", "og_to_6n") else None
        rows.append({"week": e.week, "type": e.type, "batch": e.batch or "",
                     "from_tank": e.from_tank, "to_tanks": to_tanks,
                     "count": count, "notes": e.notes})
    return rows


def _rows_to_manual_events(rows):
    """Flat editor rows -> list[ManualEvent]. `count` = total to move (split
    evenly across to_tanks) for transfer/6N; = target for fw_to_og; = amount for
    harvest."""
    from forecast.manual_events import ManualEvent, ManualDest
    out = []
    for r in rows:
        typ = str(r.get("type") or "").strip()
        if not typ:
            continue
        week = int(r.get("week") or 1)
        batch = str(r.get("batch")).strip() if r.get("batch") else None
        ft = r.get("from_tank")
        from_tank = int(ft) if ft not in (None, "") else None
        cnt = r.get("count")
        count = float(cnt) if cnt not in (None, "") else None
        # parse to_tanks: each token is "tank" (bare) or "tank:count" (explicit
        # per-dest). Bare tanks share the `count` column evenly.
        specs = []
        for tok in str(r.get("to_tanks") or "").replace(" ", "").split(","):
            if not tok:
                continue
            if ":" in tok:
                _t, _c = tok.split(":", 1)
                specs.append((int(float(_t)), float(_c)))
            else:
                specs.append((int(float(tok)), None))
        notes = str(r.get("notes") or "")
        if typ in ("og_transfer", "og_to_6n"):
            bare = [t for t, c in specs if c is None]
            per_bare = (count / len(bare)) if (bare and count is not None) else None
            dests = [ManualDest(tank=t, count=(c if c is not None else per_bare))
                     for t, c in specs]
            out.append(ManualEvent(type=typ, week=week, from_tank=from_tank,
                                   destinations=dests, batch=batch, notes=notes))
        elif typ == "harvest":
            out.append(ManualEvent(type=typ, week=week, from_tank=from_tank,
                                   count=count, batch=batch, notes=notes))
        elif typ == "fw_to_og":
            dests = [ManualDest(tank=t) for t, c in specs]
            out.append(ManualEvent(type=typ, week=week, batch=batch, count=count,
                                   destinations=dests, notes=notes))
        elif typ == "graded_harvest":
            # from_tank = source, count = biggest-N to harvest, to_tanks =
            # pickup[,retention] (retention defaults to the source).
            dests = [ManualDest(tank=t) for t, c in specs]
            out.append(ManualEvent(type=typ, week=week, from_tank=from_tank,
                                   count=count, destinations=dests,
                                   batch=batch, notes=notes))
        else:
            out.append(ManualEvent(type=typ, week=week, notes=notes))
    return out


# ---- Shared working set: one in-memory list[ManualEvent] both the visual
# editor and the Advanced raw grid mutate. Seeded once from the YAML; a Save
# dumps it back. (Single source of truth avoids the two surfaces clobbering each
# other's unsaved edits at the YAML boundary.)

def _mw_events():
    from forecast.manual_events import load_manual_events
    if "mw_events" not in st.session_state:
        st.session_state["mw_events"] = load_manual_events(SCENARIO_DIR)
    return st.session_state["mw_events"]


def _mw_bump_grid():
    """Bump the raw-grid remount nonce so it re-seeds from the working set after
    the visual editor changes it (a data_editor keeps widget state by key)."""
    st.session_state["mw_grid_nonce"] = st.session_state.get("mw_grid_nonce", 0) + 1


def _mw_set(events):
    st.session_state["mw_events"] = list(events)
    _mw_bump_grid()


def _mw_add(ev):
    _mw_events().append(ev)
    _mw_bump_grid()


def _mw_tanks(state):
    """All tanks in heatmap order (system, then tank id)."""
    return sorted(state.tanks_by_id.values(),
                  key=lambda t: (t.system_id or "", t.tank_id))


def _mw_loc(state, tid):
    t = state.tanks_by_id.get(int(tid))
    return t.location_id if t else f"#{tid}"


def _mw_sig(events, extra=""):
    """Cheap stable signature of the working set (+ context) for caching the
    heavy biology projection / validation across idle reruns."""
    import hashlib
    import json
    from forecast.manual_events import manual_events_to_list
    payload = json.dumps({"e": manual_events_to_list(events), "x": extra,
                          "pr": st.session_state.get("_mw_pr_key", "")},
                         sort_keys=True, default=str)
    return hashlib.md5(payload.encode()).hexdigest()


def _mw_project(state, ctx, events, n_weeks):
    """Project the facility through `n_weeks` of the override window (operator
    events + full biology) on a COPY of the hydrated state — the SAME engine the
    real run uses (forecast.manual_window.advance_facility_window) — and return
    its per-(tank, week) BatchLocationRows + the week labels. Cached by
    (PR, events, n_weeks) so clicking around doesn't recompute biology."""
    import copy
    from forecast.manual_window import advance_facility_window
    from forecast.time_grid import forecast_week_labels
    sig = _mw_sig(events, extra=f"proj:{n_weeks}")
    cache = st.session_state.get("_mw_proj_cache")
    if cache and cache.get("sig") == sig:
        return cache["rows"], cache["labels"]
    labels = forecast_week_labels(ctx["forecast_start"], n_weeks)
    try:
        sc = copy.deepcopy(state)
        win = advance_facility_window(
            sc, ctx["batch_by_id"], ctx["tables"], ctx["forecast_start"], n_weeks,
            events=events, control=ctx["control"], pr_closing=ctx["pr_closing"],
            fw_records=ctx["fw_records"])
        # OPENING (start-of-week, pre-biology) snapshot so each cell shows what's in
        # the tank WHEN you act on it — not the end-of-week grown state. Fall back
        # to the closing snapshot for older engines without opening_locations.
        rows = win.get("opening_locations") or win["batch_locations"]
    except Exception:  # noqa: BLE001 — a bad event must not blank the view
        rows = []
    st.session_state["_mw_proj_cache"] = {"sig": sig, "rows": rows, "labels": labels}
    return rows, labels


def _mw_validate(state, ctx, events):
    """{event_index(1-based): [messages]} for every infeasible event, faithful to
    the run (forecast.manual_events.validate_manual_events). Cached by the working
    set so the Save gate + per-row status don't re-run biology every rerun."""
    from forecast.manual_events import validate_manual_events
    if not events:
        return {}
    sig = _mw_sig(events, extra="val")
    cache = st.session_state.get("_mw_val_cache")
    if cache and cache.get("sig") == sig:
        return cache["bad"]
    try:
        res = validate_manual_events(state, events, **ctx)
        bad = {i: msgs for i, ok, msgs in res if not ok}
    except Exception as e:  # noqa: BLE001
        bad = {-1: [f"validation unavailable ({type(e).__name__}: {e})"]}
    st.session_state["_mw_val_cache"] = {"sig": sig, "bad": bad}
    return bad


def _mw_fw_avail(ctx, window_labels):
    """{batch_id: {week_label: (count, avg_wt_g, cv)}} for every in-flight FW
    cohort still in freshwater somewhere in the window — the candidates a manual
    FW→OG intake can pull from (projected exactly like the run's _build_fw_lookup,
    but over ALL FW batches, not just ones already referenced by an event)."""
    from collections import defaultdict
    from forecast.biology import project_in_flight_fw_batch
    fw_records = ctx.get("fw_records") or []
    if not fw_records or ctx.get("control") is None:
        return {}
    agg = defaultdict(lambda: {"count": 0.0, "biomass_kg": 0.0})
    for r in fw_records:
        agg[r.batch_id]["count"] += r.closing_count
        agg[r.batch_id]["biomass_kg"] += r.closing_biomass_kg
    win = set(window_labels)
    out: dict[str, dict] = {}
    for bid, a in agg.items():
        b_meta = ctx["batch_by_id"].get(bid)
        if a["count"] <= 0 or b_meta is None:
            continue
        avg_wt = a["biomass_kg"] * 1000.0 / a["count"]
        try:
            states, _, _ = project_in_flight_fw_batch(
                b_meta, ctx["tables"], ctx["control"], a["count"], avg_wt,
                ctx["pr_closing"])
        except Exception:  # noqa: BLE001
            continue
        cv = b_meta.tran_og_cv or 16.0
        wk = {s.week_label: (s.close_count, s.close_avg_weight_g, cv)
              for s in states if s.stage == "FW" and s.week_label in win}
        if wk:
            out[bid] = wk
    return out


def _mw_grid(state, rows, labels, color_by, batch_filter=None):
    """Colour-styled DataFrame of the projected facility (index = tank, columns =
    weeks, cell text = "batch · avg-weight · density") for a CLICKABLE st.dataframe.
    The per-cell weight + density let you read grow-out state at a glance to decide
    moves without clicking every tank. color_by 'fill'
    shades by density-vs-cap (green→red); 'batch' gives each batch its own colour.
    `batch_filter` (a set of batch ids, or None) restricts the rows to only the
    tanks that hold one of those batches in some displayed week — so the operator
    can focus on a few cohorts instead of the whole facility; batch COLOURS stay
    consistent with the unfiltered view. Returns (styler, ylabels, tank_by_y).
    Unlike a plotly heatmap, a single click on a dataframe row reliably emits a
    Streamlit selection."""
    from forecast.sixn import SIXN_ALL_TANKS
    idx = {(r.tank_id, r.week_label): r for r in rows}
    tanks = _mw_tanks(state)
    if batch_filter:
        keep = {r.tank_id for r in rows
                if r.count > 0 and r.batch_id in batch_filter}
        tanks = [t for t in tanks if t.tank_id in keep]
    ubatches = sorted({r.batch_id for r in rows if r.count > 0})
    _pal = px.colors.qualitative.Light24
    bcolor = {b: _pal[i % len(_pal)] for i, b in enumerate(ubatches)}
    # Per-batch BOLD FONT colour so a cohort is trackable across tanks even in Fill
    # mode (where the background encodes density, not batch). Dark24 is a dark
    # palette -> readable on the light/green/amber fills; on the dark over-cap reds
    # we lighten the same hue so it stays legible without losing batch identity.
    _fpal = px.colors.qualitative.Dark24
    fcolor = {b: _fpal[i % len(_fpal)] for i, b in enumerate(ubatches)}

    def _rgb(h):
        if h.startswith("rgb"):
            return tuple(int(x) for x in h[h.find("(") + 1:h.find(")")].split(","))
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

    def _lum(h):
        r, g, b = _rgb(h)
        return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0

    def _toward_white(h, t):
        r, g, b = _rgb(h)
        return "#%02x%02x%02x" % (round(r + (255 - r) * t),
                                  round(g + (255 - g) * t),
                                  round(b + (255 - b) * t))

    def _fill_hex(frac):
        if frac <= 0:
            return "#f0f0f0"
        if frac < 0.8:
            return "#a8d5a8"
        if frac < 1.0:
            return "#f5d49a"
        if frac < 1.15:
            return "#e8615e"
        return "#7a0d0b"

    ylabels, tank_by_y = [], {}
    text_grid, css_grid = [], []
    for t in tanks:
        is6n = t.tank_id in SIXN_ALL_TANKS
        yl = f"{t.location_id}" + ("  ⛔6N" if is6n else "")
        ylabels.append(yl)
        tank_by_y[yl] = t.tank_id
        cap = t.max_density_kg_m3 or 0.0
        trow, crow = [], []
        for wk in labels:
            r = idx.get((t.tank_id, wk))
            if r is None or r.count <= 0:
                trow.append("")
                crow.append("background-color:#f0f0f0;color:#1f1f1f")
            else:
                trow.append(
                    f"{r.batch_id} · {r.avg_wt_g / 1000:.2f}kg · {r.density_kg_m3:.0f}")
                if color_by == "batch":
                    # background already encodes the batch; keep a dark bold font.
                    bg, fg = bcolor.get(r.batch_id, "#cccccc"), "#1f1f1f"
                else:
                    bg = _fill_hex((r.density_kg_m3 / cap) if cap > 0 else 0.0)
                    base = fcolor.get(r.batch_id, "#1f1f1f")
                    fg = _toward_white(base, 0.7) if _lum(bg) < 0.6 else base
                crow.append(f"background-color:{bg};color:{fg};font-weight:700")
        text_grid.append(trow)
        css_grid.append(crow)
    df = pd.DataFrame(text_grid, index=ylabels, columns=labels)
    css_df = pd.DataFrame(css_grid, index=ylabels, columns=labels)
    styler = df.style.apply(lambda _: css_df, axis=None)
    return styler, ylabels, tank_by_y


def _mw_system_rollup(state, rows, labels, tables, batch_by_id):
    """Render, under the grid, per-SYSTEM week-open **biomass (tonnes)** and
    **feed (kg/day)** tables (systems = rows, weeks = columns). Cells are coloured
    green→red by fraction of the system's tank capacity (biomass = Σ volume ×
    max_density; feed = Σ max_feed/day) so system-level capacity pressure — which
    the per-tank grid can't show, especially FEED — is visible at a glance. Uses
    the same OPENING (week-open) rows the grid shows; STARVE (6N) tanks feed 0."""
    from collections import defaultdict
    from forecast.biology import realized_feed_kg_day
    sys_bio_cap, sys_feed_cap = defaultdict(float), defaultdict(float)
    for t in state.tanks_by_id.values():
        if t.type != "OG":
            continue
        sys_bio_cap[t.system_id] += (t.volume_m3 or 0.0) * (t.max_density_kg_m3 or 0.0)
        sys_feed_cap[t.system_id] += (t.max_feed_kg_day_cap or 0.0)
    systems = sorted(sys_bio_cap)
    bio, feed = defaultdict(float), defaultdict(float)
    for r in rows:
        if r.count <= 0:
            continue
        bio[(r.system_id, r.week_label)] += r.biomass_kg
        if getattr(r, "stage", "") != "STARVE":
            feed[(r.system_id, r.week_label)] += realized_feed_kg_day(
                r.avg_wt_g, r.biomass_kg, batch_by_id.get(r.batch_id), tables)

    def _fill(frac):
        if frac <= 0:
            return "#f0f0f0"
        if frac < 0.8:
            return "#a8d5a8"
        if frac < 1.0:
            return "#f5d49a"
        if frac < 1.15:
            return "#e8615e"
        return "#7a0d0b"

    def _table(agg, cap, scale):
        txt, css, idx = [], [], []
        for s in systems:
            idx.append(s)
            trow, crow = [], []
            for wk in labels:
                v = agg.get((s, wk), 0.0)
                trow.append(f"{v * scale:,.0f}")
                frac = (v / cap[s]) if cap.get(s) else 0.0
                crow.append(f"background-color:{_fill(frac)};color:#1f1f1f")
            txt.append(trow)
            css.append(crow)
        idx.append("TOTAL")           # facility total (neutral, not cap-coloured)
        trow, crow = [], []
        for wk in labels:
            trow.append(f"{sum(agg.get((s, wk), 0.0) for s in systems) * scale:,.0f}")
            crow.append("background-color:#e8eaf0;color:#1f1f1f;font-weight:700")
        txt.append(trow)
        css.append(crow)
        d = pd.DataFrame(txt, index=idx, columns=labels)
        c = pd.DataFrame(css, index=idx, columns=labels)
        return d.style.apply(lambda _: c, axis=None)

    _h = min(560, 44 + 33 * (len(systems) + 1))
    st.caption("Colour = fraction of the system's tank capacity (green roomy, amber "
               "near cap, red over). Same week-open state as the grid.")
    st.markdown("**Open biomass — tonnes / system / week**")
    st.dataframe(_table(bio, sys_bio_cap, 0.001), use_container_width=True, height=_h)
    st.markdown("**Open feed — kg/day / system / week** (6N depuration eats 0)")
    st.dataframe(_table(feed, sys_feed_cap, 1.0), use_container_width=True, height=_h)


# ---- The contextual action panel (opens on a tank click) ----

def _mw_split_dests(picks, total, whole):
    """Build ManualDest list mirroring the run's even-split semantics: whole tank
    -> count=None dests (engine splits the whole source); a partial total ->
    explicit per-dest counts (the UI does the division, like the raw grid)."""
    from forecast.manual_events import ManualDest
    if whole or not total:
        return [ManualDest(tank=int(d)) for d in picks]
    per = float(total) / len(picks)
    return [ManualDest(tank=int(d), count=per) for d in picks]


def _mw_cut_weights(avg_wt_g, cv_pct, count, k):
    """(big_avg_g, small_avg_g) for a top-`k`-by-size cut — the SAME split the run
    applies (forecast.biology.upper_truncated_split at the count-implied cutoff),
    so the panel's live readout matches what will actually be moved. Returns
    (None, None) for a degenerate cut (k<=0, k>=count, or no weight)."""
    from statistics import NormalDist
    from forecast.biology import upper_truncated_split
    if not k or k <= 0 or k >= count or avg_wt_g <= 0:
        return None, None
    cv = cv_pct or 16.0
    sigma = avg_wt_g * (cv / 100.0)
    if sigma <= 0:
        return avg_wt_g, avg_wt_g
    z = NormalDist().inv_cdf(1.0 - k / count)
    return upper_truncated_split(avg_wt_g, cv, avg_wt_g + sigma * z)


def _mw_occ_at(rows, wlabel):
    """{tank_id: (batch_id, density_kg_m3)} for tanks occupied at `wlabel`."""
    return {x.tank_id: (x.batch_id, x.density_kg_m3)
            for x in rows if x.week_label == wlabel and x.count > 0}


def _mw_dest_fmt(state, occ):
    """format_func for a destination picker: 'LOC · BATCH · DENSITY' when the tank
    holds fish at the selected week (so you see the current batch + density before
    picking), else 'LOC · empty'. `occ` = _mw_occ_at() map."""
    def _f(tid):
        loc = _mw_loc(state, tid)
        o = occ.get(tid)
        return f"{loc} · empty" if o is None else f"{loc} · {o[0]} · {o[1]:.0f} kg/m³"
    return _f


def _mw_action_panel(state, ctx, rows, labels, sel, date_for):
    from forecast.sixn import SIXN_ALL_TANKS
    from forecast.manual_events import ManualDest, ManualEvent
    tid, wlabel, wk = sel
    r = next((x for x in rows if x.tank_id == tid and x.week_label == wlabel), None)
    occupied = r is not None and r.count > 0
    loc = _mw_loc(state, tid)
    dt = date_for.get(wlabel)
    ds = f" · {dt.strftime('%b %d')}" if dt else ""
    head = st.columns([8, 1])
    head[0].markdown(f"#### ▶ {loc} — week {wk} ({wlabel}{ds})")
    if head[1].button("✕", key="mw_close_sel", help="Close this panel"):
        st.session_state.pop("mw_sel", None)
        # bump the grid remount nonce so the selected row clears too
        st.session_state["mw_grid_nonce2"] = \
            st.session_state.get("mw_grid_nonce2", 0) + 1
        st.rerun()
    if occupied:
        st.caption(f"Projected here: batch **{r.batch_id}** · {r.count:,.0f} fish "
                   f"@ {r.avg_wt_g / 1000:.2f} kg · {r.density_kg_m3:.0f} kg/m³")
    else:
        st.caption("Projected **empty** at this week — pick it as a destination "
                   "in a Move or an FW→OG intake.")
        return

    other_og = [t for t in _mw_tanks(state)
                if t.type == "OG" and t.tank_id not in SIXN_ALL_TANKS
                and t.tank_id != tid]
    # Current occupancy at this week + a shared picker format (LOC · batch · density,
    # or LOC · empty) so EVERY destination dropdown shows what's in each tank.
    occ_map = _mw_occ_at(rows, wlabel)
    dfmt = _mw_dest_fmt(state, occ_map)
    # Scope input keys to THIS tank+week so a previous selection's destinations /
    # counts don't linger when you click a different cell.
    sfx = f"{tid}_{wk}"
    act = st.radio("What do you want to do here?",
                   ["Harvest", "Graded → 6N", "Move (OG→OG)",
                    "Send to 6N depuration"],
                   horizontal=True, key="mw_act")

    if act == "Harvest":
        whole = st.checkbox("Harvest the whole tank", value=True, key=f"mw_h_whole_{sfx}")
        cnt = None
        if not whole:
            cnt = st.number_input("Fish to harvest", min_value=0.0,
                                  value=float(r.count), step=1000.0, key=f"mw_h_cnt_{sfx}")
        if st.button(f"➕ Add harvest in week {wk}", key="mw_h_add", type="primary"):
            _mw_add(ManualEvent(type="harvest", week=wk, from_tank=tid,
                                count=(None if whole else cnt)))
            st.rerun()

    elif act == "Graded → 6N":
        st.caption("Grade the tank by size — it **empties**: the **biggest N fish** "
                   "go to a 6N depuration tank (frozen, off-feed to purge, harvested "
                   "later from 6N) and the **smaller remainder moves to an OG tank** "
                   "to keep growing. Conserves count + biomass exactly.")
        # Destinations = EMPTY tanks OR tanks already holding THIS batch (top-up),
        # roomiest-first; each option shows its current batch + density.
        def _dest_opts(tank_ids):
            opts = [t for t in tank_ids
                    if t not in occ_map or occ_map[t][0] == r.batch_id]
            return sorted(opts, key=lambda t: occ_map.get(t, (None, 0.0))[1])
        dest_6n = _dest_opts(sorted(SIXN_ALL_TANKS))   # mains 61/63/65 + sisters 67/69/71
        dest_og = _dest_opts([t.tank_id for t in other_og])
        n_big = st.number_input(
            "Fish to send to 6N (the biggest N)", min_value=0.0,
            max_value=float(r.count), value=float(int(r.count // 2)), step=1000.0,
            key=f"mw_g6_cnt_{sfx}",
            help="The N largest fish are graded out at their (higher) mean weight; "
                 "the rest stay at their (lower) mean.")
        # Live cut-weight readout — the SAME split the run applies.
        _cvt = state.tanks_by_id.get(tid)
        _big, _small = _mw_cut_weights(
            r.avg_wt_g, (_cvt.cv_pct if _cvt else 0.0), r.count, n_big)
        if _big is not None:
            st.caption(f"↳ biggest **{n_big:,.0f} ≈ {_big / 1000:.2f} kg** → 6N · "
                       f"smaller {r.count - n_big:,.0f} ≈ "
                       f"{_small / 1000:.2f} kg → OG")
        dest6n = st.selectbox(
            "6N depuration tank — biggest fish (· batch · density)",
            options=dest_6n, format_func=dfmt, key=f"mw_g6_dest_{sfx}",
            help="Mains 61/63/65 + sisters 67/69/71 — empty or same-batch; each "
                 "shows its current batch + density (a same-pair main holding a "
                 "different batch = a mixed harvest, so watch the batch column).",
        ) if dest_6n else None
        # Grading empties the source, so the smaller remainder is graded OUT too and
        # moves to an OG tank (empty, or one already holding this batch). Required.
        ret_tank = st.selectbox(
            "Send the smaller fish to — OG tank (· batch · density)",
            options=dest_og, format_func=dfmt, key=f"mw_g6_ret_{sfx}",
            help="Empty or same-batch OG tanks. Grading empties the source; the "
                 "smaller fish move here.",
        ) if dest_og else None
        if not dest_6n:
            st.caption("⚠ No empty / same-batch 6N tank available this week.")
        if not dest_og:
            st.caption("⚠ No empty / same-batch OG tank for the smaller fish.")
        dests = ([ManualDest(tank=int(dest6n)), ManualDest(tank=int(ret_tank))]
                 if dest6n is not None and ret_tank is not None else [])
        if st.button(f"➕ Add graded 6N move in week {wk}", key="mw_g6_add",
                     type="primary",
                     disabled=(dest6n is None or ret_tank is None
                               or not n_big or n_big <= 0)):
            _mw_add(ManualEvent(type="graded_harvest", week=wk, from_tank=tid,
                                count=n_big, destinations=dests))
            st.rerun()

    elif act == "Move (OG→OG)":
        # Regular OG grow-out tanks only (the 6N depuration system is reached via
        # Send-to-6N / Graded->6N, not a plain grow-out move). Offer EMPTY tanks or
        # ones already holding this batch, each showing current density, roomiest-first.
        move_dests = sorted(
            (t.tank_id for t in other_og
             if t.tank_id not in occ_map or occ_map[t.tank_id][0] == r.batch_id),
            key=lambda t: occ_map.get(t, (None, 0.0))[1])
        picks = st.multiselect(
            "Destination grow-out tank(s) — empty or same batch (· batch · density)",
            options=move_dests, format_func=dfmt, key=f"mw_m_dest_{sfx}")
        whole = st.checkbox("Move the whole tank (split evenly)", value=True,
                            key=f"mw_m_whole_{sfx}")
        total = None
        if not whole:
            total = st.number_input("Total fish to move (split evenly across dests)",
                                    min_value=0.0, value=float(r.count), step=1000.0,
                                    key=f"mw_m_total_{sfx}")
        if st.button(f"➕ Add move in week {wk}", key="mw_m_add", type="primary",
                     disabled=not picks):
            _mw_add(ManualEvent(type="og_transfer", week=wk, from_tank=tid,
                                destinations=_mw_split_dests(picks, total, whole),
                                count=(None if whole else total)))
            st.rerun()

    else:  # Send to 6N depuration
        picks = st.multiselect(
            "6N depuration tank(s) — mains + sisters (· batch · density)",
            options=sorted(SIXN_ALL_TANKS), format_func=dfmt, key=f"mw_6_dest_{sfx}")
        whole = st.checkbox("Move the whole tank (split evenly)", value=True,
                            key=f"mw_6_whole_{sfx}")
        total = None
        if not whole:
            total = st.number_input("Total fish to send (split evenly)",
                                    min_value=0.0, value=float(r.count), step=1000.0,
                                    key=f"mw_6_total_{sfx}")
        if st.button(f"➕ Add 6N move in week {wk}", key="mw_6_add", type="primary",
                     disabled=not picks):
            _mw_add(ManualEvent(type="og_to_6n", week=wk, from_tank=tid,
                                destinations=_mw_split_dests(picks, total, whole),
                                count=(None if whole else total)))
            st.rerun()


def _mw_fw_split_preview(ctx, bid, fw_count, fw_avg_wt_g, fw_cv, target, event_date):
    """(big_n, big_avg_g, small_n, small_avg_g) for the entry grade of a FW→OG
    intake — replays the run's handling-mortality + reconcile-to-target bottom
    cull + median size split (manual_events._apply_fw_to_og / biology), so the
    picker preview matches exactly what will be placed. None on a bad projection."""
    from forecast.biology import _apply_bottom_cull, compute_size_class_split
    control = ctx.get("control")
    hf = (control.handling_mortality_pct / 100.0) if control is not None else 0.0
    cnt = fw_count * (1.0 - hf)
    wt = fw_avg_wt_g
    if cnt <= 0 or wt <= 0:
        return None
    if target and cnt > target:
        cnt, wt, _cn, _cb = _apply_bottom_cull(cnt, wt, fw_cv, 1.0 - target / cnt)
    try:
        split = compute_size_class_split(
            batch_id=bid, tran_og_date=event_date,
            post_cull_count=cnt, post_cull_avg_wt_g=wt, cv_pct=fw_cv)
    except Exception:  # noqa: BLE001
        return None
    return (split.big_class_count, split.big_class_avg_wt_g,
            split.small_class_count, split.small_class_avg_wt_g)


def _mw_fw_intake(state, ctx, rows, labels, date_for):
    """FW→OG intake — a freshwater cohort isn't a tank yet, so it gets its own
    picker: choose the cohort, the week, a target count (engine culls down to
    it), and — because the cohort is graded into a bigger + smaller class on
    entry — separate destination tanks for each grade. Rendered into the
    caller's container (no inner expander — the editor already lives in one)."""
    from forecast.sixn import SIXN_ALL_TANKS
    from forecast.manual_events import ManualEvent, ManualDest
    avail = _mw_fw_avail(ctx, labels)
    if not avail:
        st.caption("No in-flight freshwater cohorts are still in freshwater "
                   "during this window.")
        return
    bid = st.selectbox("Freshwater cohort", options=sorted(avail), key="mw_fw_batch")
    wk_labels = [w for w in labels if w in avail.get(bid, {})]
    if not wk_labels:
        st.caption("This cohort has already crossed to seawater in this window.")
        return
    wlabel = st.selectbox(
        "Week to bring it in", options=wk_labels,
        format_func=lambda w: f"{w}"
        + (f" · {date_for[w].strftime('%b %d')}" if date_for.get(w) else ""),
        key="mw_fw_week")
    cnt, _wt, _cv = avail[bid][wlabel]
    wk = labels.index(wlabel) + 1
    st.caption(f"Projected freshwater state: ~{cnt:,.0f} fish available at "
               f"{wlabel}. Target is the count entering seawater (the engine "
               f"applies handling mortality + culls down to it).")
    target = st.number_input("Target fish entering seawater", min_value=0.0,
                             value=float(cnt), step=1000.0, key="mw_fw_target")

    # Live entry-grade preview — the two classes after handling + cull, so the
    # operator sees the counts/weights they're placing before picking tanks.
    prev = _mw_fw_split_preview(ctx, bid, cnt, _wt, _cv, target, date_for.get(wlabel))
    if prev:
        big_n, big_avg, small_n, small_avg = prev
        st.caption(f"Entry grade → **bigger {big_n:,.0f} ≈ {big_avg / 1000:.2f} kg** · "
                   f"**smaller {small_n:,.0f} ≈ {small_avg / 1000:.2f} kg**")

    occ = {x.tank_id for x in rows if x.week_label == wlabel and x.count > 0}
    empty_og = [t.tank_id for t in _mw_tanks(state)
                if t.type == "OG" and t.tank_id not in SIXN_ALL_TANKS
                and t.tank_id not in occ]
    dfmt = _mw_dest_fmt(state, _mw_occ_at(rows, wlabel))
    big_picks = st.multiselect(
        "Tank(s) for the BIGGER grade", options=empty_og,
        format_func=dfmt, key="mw_fw_big")
    # A tank can't hold both grades — drop the big picks from the small options.
    small_opts = [t for t in empty_og if t not in big_picks]
    small_picks = st.multiselect(
        "Tank(s) for the SMALLER grade", options=small_opts,
        format_func=dfmt, key="mw_fw_small")

    need_big = bool(prev and prev[0] > 0)
    need_small = bool(prev and prev[2] > 0)
    gaps = []
    if need_big and not big_picks:
        gaps.append("a tank for the bigger grade")
    if need_small and not small_picks:
        gaps.append("a tank for the smaller grade")
    if gaps:
        st.caption("⚠ Still need " + " and ".join(gaps) + ".")
    dests = ([ManualDest(tank=int(t), size_class="big") for t in big_picks]
             + [ManualDest(tank=int(t), size_class="small") for t in small_picks])
    if st.button(f"➕ Add FW→OG intake in week {wk}", key="mw_fw_add",
                 type="primary", disabled=bool(gaps) or not dests):
        _mw_add(ManualEvent(type="fw_to_og", week=wk, batch=bid, count=target,
                            destinations=dests))
        st.rerun()


# ---- Readback timeline + save bar ----

def _mw_event_summary(state, ev):
    loc = lambda t: _mw_loc(state, t)  # noqa: E731
    dests = ", ".join(loc(d.tank) for d in ev.destinations) or "—"
    if ev.type == "harvest":
        amt = f"{ev.count:,.0f} fish" if ev.count is not None else "the whole tank"
        return f"Wk {ev.week}: **Harvest** {amt} from {loc(ev.from_tank)}"
    if ev.type == "og_transfer":
        amt = f"{ev.count:,.0f}" if ev.count else "whole tank"
        return f"Wk {ev.week}: **Move** {amt} from {loc(ev.from_tank)} → {dests}"
    if ev.type == "og_to_6n":
        amt = f"{ev.count:,.0f}" if ev.count else "whole tank"
        return f"Wk {ev.week}: **Send to 6N** {amt} from {loc(ev.from_tank)} → {dests}"
    if ev.type == "fw_to_og":
        tgt = f"target {ev.count:,.0f}" if ev.count else "all available"
        big = ", ".join(loc(d.tank) for d in ev.destinations
                        if (d.size_class or "").lower() == "big")
        small = ", ".join(loc(d.tank) for d in ev.destinations
                          if (d.size_class or "").lower() == "small")
        if big or small:
            return (f"Wk {ev.week}: **FW→OG** {ev.batch} → bigger {big or '—'} · "
                    f"smaller {small or '—'} ({tgt})")
        return f"Wk {ev.week}: **FW→OG** {ev.batch} → {dests} ({tgt})"
    if ev.type == "graded_harvest":
        from forecast.sixn import SIXN_ALL_TANKS
        amt = f"{ev.count:,.0f}" if ev.count else "?"
        pk_id = ev.destinations[0].tank if ev.destinations else None
        pk = loc(pk_id) if pk_id is not None else "—"
        ret = (loc(ev.destinations[1].tank) if len(ev.destinations) >= 2
               else "source")
        if pk_id in SIXN_ALL_TANKS:
            return (f"Wk {ev.week}: **Graded → 6N** biggest {amt} from "
                    f"{loc(ev.from_tank)} → 6N {pk}, retain smaller in {ret}")
        return (f"Wk {ev.week}: **Graded harvest** biggest {amt} from "
                f"{loc(ev.from_tank)} (via {pk}), retain smaller in {ret}")
    return f"Wk {ev.week}: {ev.type}"


def _mw_timeline(state, events, bad):
    st.markdown("**Scripted operations — this window**")
    if not events:
        st.caption("None yet. Click a tank in the grid above (or use the FW→OG "
                   "intake) to add your first operation.")
        return
    for i, ev in enumerate(events, 1):
        c1, c2 = st.columns([10, 1])
        problems = bad.get(i)
        with c1:
            if problems:
                st.markdown(f"❌ {_mw_event_summary(state, ev)}")
                st.caption("&nbsp;&nbsp;&nbsp;↳ " + "; ".join(problems))
            else:
                st.markdown(f"✅ {_mw_event_summary(state, ev)}")
            if ev.notes:
                st.caption(f"&nbsp;&nbsp;&nbsp;_{ev.notes}_")
        if c2.button("🗑", key=f"mw_del_{i}", help="Delete this operation"):
            _mw_events().pop(i - 1)
            _mw_bump_grid()
            st.rerun()


def _mw_raw_grid(state):
    """Power-user fallback: the same four event types as a flat table. Seeds from
    and writes back to the shared working set (not the YAML directly)."""
    st.caption(
        "Same four event types as a raw table — for bulk edits or unequal per-tank "
        "splits the click flow doesn't cover. **Apply to window** pushes these rows "
        "into the visual editor + timeline above.")
    st.caption(
        "**og_transfer**: from_tank → to_tanks (count split evenly) · "
        "**harvest**: from_tank, count · **graded_harvest**: from_tank, "
        "count=biggest-N, to_tanks=pickup[,retention] · "
        "**og_to_6n**: from_tank → 6N to_tanks · "
        "**fw_to_og**: batch + count=target → to_tanks. "
        "to_tanks: comma-separated; `tank:count` for an explicit per-tank amount.")
    nonce = st.session_state.get("mw_grid_nonce", 0)
    base = pd.DataFrame(_manual_events_to_df_rows(_mw_events()), columns=_MANUAL_COLS)
    edited = st.data_editor(
        base, num_rows="dynamic", hide_index=True, use_container_width=True,
        key=f"mw_grid_{nonce}",
        column_config={
            "week": st.column_config.NumberColumn("Week", min_value=1, step=1,
                help="1-based forecast week the event fires in"),
            "type": st.column_config.SelectboxColumn("Type",
                options=["og_transfer", "harvest", "graded_harvest",
                         "og_to_6n", "fw_to_og"]),
            "batch": st.column_config.TextColumn("Batch", help="FW batch (fw_to_og)"),
            "from_tank": st.column_config.NumberColumn("From tank", step=1),
            "to_tanks": st.column_config.TextColumn("To tanks",
                help="comma-separated tank IDs; tank:count for an explicit amount"),
            "count": st.column_config.NumberColumn("Count / target", step=1000),
            "notes": st.column_config.TextColumn("Notes"),
        })
    if st.button("Apply to window", key="mw_grid_apply"):
        try:
            _mw_set(_rows_to_manual_events(_records(edited)))
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"Couldn't parse the grid: {e}")


def _mw_save_bar(events, bad):
    from forecast.manual_events import dump_manual_events, load_manual_events
    n = len(events)
    if n and not bad:
        st.success(f"All {n} operation(s) feasible against the uploaded PR.")
    elif bad:
        st.warning(f"{len(bad)} operation(s) infeasible — fix the ❌ rows above "
                   f"before saving.")
    c1, c2, c3, _ = st.columns([1, 1, 1, 2])
    if c1.button("💾 Save window", key="mw_save", disabled=bool(bad),
                 help="Reject-at-entry: disabled while any operation is infeasible."):
        try:
            dump_manual_events(SCENARIO_DIR, events)
            st.success(f"Saved scenario/manual_events.yaml ({n}). Click ▶ Run forecast.")
        except Exception as e:  # noqa: BLE001
            st.error(f"Save failed: {e}")
    if c2.button("↻ Reload from file", key="mw_reload"):
        _mw_set(load_manual_events(SCENARIO_DIR))
        st.rerun()
    if c3.button("🧹 Clear window", key="mw_clear", disabled=not n):
        _mw_set([])
        st.rerun()


def _manual_window_editor(uploaded):
    """Run-mode editor: SEE the projected facility week by week, click a tank to
    act on it in context (harvest / move / 6N / FW→OG), validated against the
    uploaded PR, saved to scenario/manual_events.yaml (which the run reads). The
    flat grid lives on behind an Advanced expander. No Excel sheets involved."""
    from forecast.time_grid import week_start as _week_start
    with st.expander("🗓 Starting setup — manual override window (optional)",
                     expanded=False):
        st.caption(
            "See the facility projected forward and **click a tank to act on it** — "
            "harvest it, move/split it, send it to 6N, or bring a freshwater cohort "
            "into OG. The forecast EXECUTES your operations with full biology "
            "(growth/mortality/feed), records them in the reports, then the planner "
            "takes over after your last scripted week. Leave it empty to let the "
            "planner do everything.")

        try:
            state, _fw, ctx = _hydrate_state_from_upload(uploaded)
            import hashlib
            st.session_state["_mw_pr_key"] = hashlib.md5(uploaded.getvalue()).hexdigest()
            hydrated = True
        except Exception as e:  # noqa: BLE001
            st.warning(f"Couldn't read the facility from this PR "
                       f"({type(e).__name__}: {e}) — the visual view needs a "
                       f"hydratable PR. Use the raw grid below.")
            state, ctx, hydrated = None, None, False

        events = _mw_events()
        bad = _mw_validate(state, ctx, events) if hydrated else {}

        if hydrated:
            horizon = int(getattr(ctx["control"], "horizon_weeks", 52) or 52)
            max_ev = max((e.week or 1) for e in events) if events else 0
            cap_view = min(max(1, horizon - 1), 26)
            default_view = min(max(8, max_ev), cap_view)
            if cap_view <= 1:
                view = 1
            else:
                view = st.slider(
                    "Weeks to project / act in", 1, cap_view,
                    min(max(default_view, 1), cap_view),
                    help="How far ahead to project the facility and let you act. "
                         "The saved window length stays implicit — it runs through "
                         "your last scripted operation, then the planner takes over.")
            n_weeks = max(view, max_ev, 1)
            rows, labels = _mw_project(state, ctx, events, n_weeks)
            date_for = {lbl: _week_start(i, ctx["forecast_start"])
                        for i, lbl in enumerate(labels)}

            _cmode = st.radio(
                "Colour cells by", ["Fill (density)", "Batch"], horizontal=True,
                key="mw_color_by",
                help="Fill = how full each tank is vs its cap (green→red). Batch = "
                     "a distinct colour per batch, to see which tanks hold which "
                     "fish and how a batch moves across the weeks.")
            _cb = "batch" if _cmode.startswith("Batch") else "fill"
            st.caption(
                ("**Each batch has its own background colour** (grey = empty). "
                 if _cb == "batch" else
                 "**Background = how full each tank is vs its cap** — grey empty, "
                 "green roomy, amber near cap, red over — and the **batch id is bold "
                 "in its own colour** so you can follow a cohort across tanks. ")
                + "Columns are weeks, rows are tanks (⛔6N = depuration), and each cell "
                  "shows **batch · avg weight · density** at **week-open** (start of "
                  "the week, before that week's growth) — what's in the tank when you "
                  "act. **Click a tank's cell at the week you want** to act on it. "
                  "The grid redraws as you script.")

            # Optional batch filter — show only the tanks holding the selected
            # cohort(s) instead of the whole facility (empty = show all).
            _all_batches = sorted({r.batch_id for r in rows if r.count > 0})
            _sel_batches = st.multiselect(
                "Filter to batches (empty = whole facility)", options=_all_batches,
                default=[], key="mw_batch_filter",
                help="Show only the tanks that hold the selected batch(es) in some "
                     "displayed week. Batch colours stay the same as the full view.")
            _bf = set(_sel_batches) or None

            # Clickable facility grid (left) + contextual action panel (right), side
            # by side so a cell click shows the options right next to the grid instead
            # of below a tall, internally-scrolling table.
            styler, ylabels, tank_by_y = _mw_grid(
                state, rows, labels, color_by=_cb, batch_filter=_bf)
            if _bf and not ylabels:
                st.caption("No tanks hold the selected batch(es) in this window.")
            gnonce = st.session_state.get("mw_grid_nonce2", 0)
            grid_col, panel_col = st.columns([3, 2], gap="medium")
            with grid_col:
                # single-cell selection (Streamlit >=1.49) reliably emits the clicked
                # (row, column), which plotly-heatmap clicks do not. _mw_grid lays the
                # frame out rows=tanks / columns=week-labels, so one click picks BOTH
                # the tank AND the week — no separate week control needed.
                gev = st.dataframe(
                    styler, use_container_width=True,
                    # Full height: show EVERY tank row without a vertical
                    # scrollbar so the whole facility is visible at once (tall
                    # for large facilities, by design — weeks still scroll
                    # horizontally). +2px avoids a residual scrollbar from
                    # row-height rounding.
                    height=46 + 35 * len(ylabels),
                    on_select="rerun", selection_mode="single-cell",
                    key=f"mw_grid_sel_{gnonce}")
                try:
                    _cells = list(gev["selection"]["cells"])
                except Exception:  # noqa: BLE001
                    _cells = list(
                        getattr(getattr(gev, "selection", None), "cells", []) or [])
                if _cells:
                    _rowpos, _wlabel = _cells[0]
                    if 0 <= _rowpos < len(ylabels) and _wlabel in labels:
                        st.session_state["mw_sel"] = (
                            tank_by_y[ylabels[_rowpos]], _wlabel,
                            labels.index(_wlabel) + 1)
            with panel_col:
                sel = st.session_state.get("mw_sel")
                if sel and sel[1] in labels:
                    with st.container(border=True):
                        _mw_action_panel(state, ctx, rows, labels, sel, date_for)
                else:
                    st.info("👆 Click a tank's cell in the grid to harvest it, move / "
                            "split it, or send it to 6N — the options appear here.")

            if st.toggle("📊 System rollup — open biomass + feed/day per week",
                         key="mw_rollup_toggle"):
                _mw_system_rollup(state, rows, labels, ctx["tables"],
                                  ctx["batch_by_id"])

            if st.toggle("🐟 FW→OG intake — bring a freshwater cohort into OG",
                         key="mw_fw_toggle"):
                with st.container(border=True):
                    _mw_fw_intake(state, ctx, rows, labels, date_for)

            st.divider()
            _mw_timeline(state, events, bad)

            if st.toggle("⚙ Advanced — raw event grid (power users)",
                         key="mw_adv_toggle"):
                with st.container(border=True):
                    _mw_raw_grid(state)
        else:
            _mw_raw_grid(None)

        st.divider()
        _mw_save_bar(events, bad)


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
    # If ▶ Run forecast was clicked from another mode, jump to Run forecast HERE —
    # before the radio is instantiated, so setting its session_state value is allowed
    # (Streamlit forbids mutating a widget's key after it renders). The pending run is
    # then honored by the run handler below.
    if st.session_state.pop("_goto_run_mode", False):
        st.session_state["app_mode"] = "Run forecast"
    app_mode = st.radio(
        "Mode",
        ["Run forecast", "Configure (models & control)", "Tune (density knobs)",
         "Optimize (multi-objective)"],
        help="Run forecast: upload a PR and run. Configure: edit the app's "
             "biology models, facility, control, batches, and limits. "
             "Tune: sweep the controller knobs and read the per-batch "
             "density distribution. Optimize: sweep knobs and rank variants on a "
             "selectable objective (walk the line + minimize feed/handling).",
        key="app_mode",
    )
    with st.expander("ℹ️ Which mode? — Run vs Tune vs Optimize"):
        st.markdown(
            "- **Run forecast** — runs the pipeline with your **current** Control knobs "
            "and produces the plan + reports. This is the everyday mode. *\"Run with "
            "tuned knobs\"* just means a normal Run **after** Tune or Optimize has saved "
            "better knobs into your config.\n"
            "- **Tune (density knobs)** — sweeps **only the density knobs**, shows the "
            "per-batch peak-density distribution, and recommends + saves the best set. "
            "**One axis (density).** Use when the Plan tab's per-batch density is the "
            "concern.\n"
            "- **Optimize (multi-objective)** — sweeps knobs against **several goals at "
            "once** (flat biomass, feed, handling, cap compliance) on a *selectable* "
            "weighted objective, ranks variants, and applies the best. **Many axes**, and "
            "it finds knob *combinations* a single-axis sweep can't.\n"
            "- **Configure** — hand-edit the models, control knobs, facility, batches, and "
            "limits (every knob has a tooltip).\n\n"
            "**Tune and Optimize don't use a different engine** — they run *the same "
            "forecast* many times with different Control knobs, then save the winning set "
            "to `config/control.yaml`. So after either, a plain **Run forecast** uses "
            "those tuned knobs — and you can review/adjust them in **Configure → Control**."
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
        st.caption("⚠ Global is the experimental precalculated engine — "
                   "BatchLocations/Transfers are real (0-drift). The default "
                   "heuristic placement concentrates per-tank density well over cap; "
                   "the optimal mode below drives it back to the cap (~100 kg/m³) at "
                   "the cost of more transfers and a ~30-min solve.")
        _global_optimal = st.checkbox(
            "Optimal placement (CP-SAT) — density at cap, low variance, fully placed",
            value=False, key="global_optimal",
            help="Places each week's grow-out layout with OR-Tools CP-SAT instead "
                 "of the greedy heuristic. Drives per-tank density to the cap "
                 "(~100 kg/m³, where the greedy heuristic leaves it 300+ over-cap), "
                 "holds facility biomass at ~100%, places every batch to a real "
                 "tank (full conservation PASS), and minimizes system-load "
                 "variance. Trade-off: MORE transfers per fish (~1.9 vs ~0.8 "
                 "heuristic / ~0.7 controller) — it moves fish to keep density even. "
                 "A fixed per-week deterministic budget + fixed seed make the solve "
                 "QUALITY reproducible (~0.8% optimality gap); equally-optimal "
                 "layouts can differ tank-for-tank. 0 TANK_DRIFT, audited. SLOWER — "
                 "~30 min for a 52-week horizon (heuristic LP ~4 min; controller, "
                 "seconds).")
        if _global_optimal:
            st.caption("CP-SAT runs a fixed, tuned deterministic budget per week — "
                       "no tuning needed. Expect ~30 min for a full 52-week horizon.")
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

    # The ▶ Run forecast button lives in the sidebar in EVERY mode, but the run
    # results render only in Run forecast mode — so clicking it from Configure/Tune/
    # Optimize used to silently do nothing (the main panel st.stop()s before the run
    # handler). Jump to Run forecast and run there instead.
    if run_clicked and app_mode != "Run forecast":
        st.session_state["_goto_run_mode"] = True
        st.session_state["_pending_run"] = True
        st.rerun()

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
        # Match the output extension to the workbook's MACRO STATE, not the input
        # name's suffix: the controller keeps VBA on load, so a macro-enabled input
        # yields a macro-enabled output that MUST be .xlsm — Excel refuses a macro
        # workbook wearing a .xlsx extension. (run.py also backstops this on save.)
        from forecast.excel_io import is_macro_enabled_workbook
        _suf = ".xlsm" if is_macro_enabled_workbook(input_bytes) else ".xlsx"
        out_name = Path(input_name).stem + "_planned" + _suf
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

    # run.py coerces the output extension to match the workbook's macro state (.xlsm
    # vs .xlsx, so Excel can open it), which may differ from the name the app chose —
    # locate the actual saved file (same stem, any extension) before reading it.
    if not out_path.exists():
        _alt = next(iter(work_dir.glob(out_path.stem + ".*")), None)
        if _alt is not None:
            out_path = _alt

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
        "output_name": out_path.name,
        "output_path": str(out_path),
        "config_used": config_used,
    })
    return parsed


def _parse_output_workbook(path: Path) -> dict:
    """Extract data from the saved workbook for the UI's visualization."""
    wb = load_workbook(path, keep_vba=str(path).lower().endswith(".xlsm"),
                       data_only=False)

    # Per-tank density caps from facility config (a control input) — never a
    # hardcoded literal. Each tank's over-cap is judged against ITS OWN cap
    # (nursery 30–65, grow-out 95), so the Violations count stays correct if
    # the cap is retuned or if lower-cap tanks ever enter the placement.
    tank_caps, sys_cap_biomass = {}, {}
    growout_cap = 95.0
    try:
        from forecast.config_io import load_facility_config
        _fac = load_facility_config(CONFIG_DIR)
        for t in _fac.tanks:
            tank_caps[t.tank_id] = t.max_density_kg_m3
            sys_cap_biomass[t.system_id] = (
                sys_cap_biomass.get(t.system_id, 0.0)
                + t.volume_m3 * t.max_density_kg_m3
            )
        # Representative grow-out density cap for facility-wide reference lines:
        # the OG production tanks, excluding the OG6N depuration pool.
        _grow = [t.max_density_kg_m3 for t in _fac.tanks
                 if t.system_id.startswith("OG") and t.system_id != "OG6N"]
        if _grow:
            growout_cap = float(max(_grow))
    except Exception:  # noqa: BLE001
        pass

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
            # Density alert EXCLUDES the OG6N depuration/purge pool: those tanks
            # hold harvest-size fish concentrated + off-feed for depuration just
            # before shipping, so high density there is expected, not a welfare
            # flag. Mirrors the engine's own density-violation count, which skips
            # the 6N purge pool (run.py). This parse is shared by every pipeline's
            # output (controller + global), so the exclusion applies to all.
            if (isinstance(density, (int, float))
                    and density > tank_caps.get(tid, growout_cap)
                    and sys_id != "OG6N"):
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
        "growout_density_cap": growout_cap,
        "system_biomass_cap": sys_cap_biomass,
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

    # Apply & save the recommended knobs — same mechanism as Optimize mode, so a
    # tuning sweep flows straight into config/control.yaml without retyping anything.
    best = next((r for r in results if r.label == rec.best_label), results[0])
    from forecast import optimize as _opt
    if best.overrides:
        with st.container(border=True):
            st.markdown(f"**Apply — `{best.label}`** · these are Control-knob overrides "
                        "(the same knobs the **Configure → Control** tab edits).")
            st.code(_opt.overrides_yaml(best.overrides) or "# baseline", language="yaml")
            if st.button("💾 Save these tuning knobs to my config", key="tune_save",
                         type="primary"):
                _opt.save_overrides_to_config(str(CONFIG_DIR), best.overrides)
                _clear_all_editor_state()
                st.success("Saved to config/control.yaml — switch to **Run forecast** "
                           "to use them (or open **Configure → Control** to review).")
    else:
        st.caption("Recommended variant is the baseline — no knob change to save.")

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
        failed = bool(getattr(v, "failed", None))
        rows.append({
            "Variant": v.label,
            "Score": None if failed else round(v.score, 3),
            "Sys_over-cap": None if failed else round(m.system_overshoot, 3),
            "Density_over-cap": None if failed else round(m.density_overshoot, 3),
            "Biomass_overshoot": None if failed else round(m.biomass_overshoot, 3),
            "Biomass_var": None if failed else round(m.biomass_var, 3),
            "Util_gap": None if failed else round(m.biomass_util_gap, 3),
            "Harvest_var": None if failed else round(m.harvest_var, 3),
            "Harvest_overshoot": None if failed else round(m.harvest_overshoot, 3),
            "Feed_load": None if failed else round(m.feed_load),
            "Transfers/fish": None if failed else round(m.transfers_per_fish, 2),
            "Wks_over_55k": None if failed else m.weeks_over_harvest_cap,
            "Conservation": (f"INFEASIBLE — {v.failed[:90]}" if failed
                             else "OK" if v.conservation_ok
                             else f"FAIL ({v.dropped}/{v.overprod})"),
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
        _hv_cap = float((r.get("config_used") or {}).get("max_harvest_per_week")
                        or 55000)
        fig.add_hline(y=_hv_cap, line_dash="dot",
                      annotation_text=f"{_hv_cap / 1000:,.0f}k cap")
        fig.update_layout(height=340, xaxis_title="", yaxis_title="fish")
        st.plotly_chart(fig, use_container_width=True)
    if not bl.empty and "Week" in bl and "Biomass_kg" in bl:
        bw = (bl.groupby("Week", as_index=False)["Biomass_kg"].sum()
                .sort_values("Week"))
        bw["Biomass_t"] = bw["Biomass_kg"] / 1000.0
        fig2 = px.line(bw, x="Week", y="Biomass_t",
                       title="Facility biomass per week (t)")
        _cap_t = float((r.get("config_used") or {}).get("max_biomass_kg")
                       or 3_800_000) / 1000.0
        fig2.add_hline(y=_cap_t, line_dash="dot",
                       annotation_text=f"{_cap_t / 1000:.2f}M cap")
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

    # Some variants may be INFEASIBLE on this PR (the engine refused to plan them —
    # e.g. a TranOG arrival with no free tanks). They're excluded from selection but
    # kept in the table; surface how many so the operator sees the search wasn't clean.
    _infeasible = [v for v in results if getattr(v, "failed", None)]
    if _infeasible:
        _lbls = ", ".join(v.label for v in _infeasible[:6])
        st.warning(
            f"⚠ **{len(_infeasible)} of {len(results)} variants were infeasible** on "
            f"this ProductionReport (excluded from selection): {_lbls}"
            f"{' …' if len(_infeasible) > 6 else ''}. This means the tighter/fuller "
            f"settings can't physically place every TranOG arrival here — a "
            f"capacity limit (add 6N depuration, raise the biomass cap, or re-time "
            f"the TranOG schedule), not a tuning problem. See the table for each "
            f"reason.")

    # Re-score instantly against the currently selected emphasis (no re-run).
    rec = optimize.recommend(results, emphasis=emphasis,
                             weights=optimize.weights_for(emphasis, custom))
    _no_feasible = rec.best_label == "(none)"
    if _no_feasible:
        st.error(
            f"**No feasible variant** on this ProductionReport — {rec.text} Every "
            "setting hit the engine's capacity guard (each reason is in the table "
            "below). This is a capacity limit, not a tuning problem: add 6N "
            "depuration capacity, raise the facility biomass cap, or re-time the "
            "TranOG arrival schedule, then re-run.")
    elif rec.is_capacity_bound:
        st.warning(f"**Capacity-bound:** {rec.text}")
    else:
        st.success(f"**Recommendation:** {rec.text}")

    # ---- Feed the recommendation back into the forecast ----
    # When nothing is feasible, `best` is an infeasible variant — the Apply panel's
    # Save button is hidden (no overrides) and its Run button surfaces the same
    # capacity error, so it's survivable; the error above + the table make the
    # situation clear.
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


# ---- Run mode: manual override window editor (above the run results) ----
if uploaded is not None:
    _manual_window_editor(uploaded)


# ============================================================
# Run the pipeline when the button is clicked
# ============================================================

if (run_clicked or st.session_state.pop("_pending_run", False)) and uploaded is not None:
    _method = ("global_optimal" if (_is_global and _global_optimal)
               else "global" if _is_global else "controller")
    _spin = ("Running GLOBAL OPTIMAL (per-week CP-SAT placement) — ~30 min for a "
             "full 52-week horizon, please wait..."
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
        k1.metric("Violations", r["violations"],
                  help="Tanks where realized density exceeds that tank's own "
                       "density cap (per-tank, from facility config; the OG6N "
                       "depuration pool is excluded).")
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
            # hid 3.8x spikes as ordinary red). Hard color break at the cap;
            # over-cap tanks deepen toward dark red as they get worse.
            _dv = pd.to_numeric(df["Density_kg_m3"], errors="coerce")
            vmax = max(130.0, float(_dv.max()) if _dv.notna().any() else 130.0)
            CAP = float(r.get("growout_density_cap") or 95.0)
            c_lo, c_cap = 80.0 / vmax, CAP / vmax
            colorscale = sorted({
                0.0: "#f0f0f0",          # empty / ~zero
                min(c_lo, c_cap * 0.5): "#a8d5a8",   # green, under target
                max(0.0, c_cap - 1e-3): "#f5d49a",   # amber, just under cap
                c_cap: "#e8615e",        # red AT the cap
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
                f"approaching the {CAP:.0f} kg/m³ cap, red = over cap, deepening to "
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
                # Exact per-system density-cap capacity from facility config:
                # Σ(tank volume × tank max_density), not a flat 95×1720 guess.
                sys_cap_biomass = r.get("system_biomass_cap") or {}
                sys_bio_pct = sys_bio.copy()
                sys_bio_pct["Cap_kg"] = sys_bio_pct["System"].map(
                    lambda s: sys_cap_biomass.get(s, 0.0)
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
                "density-cap capacity (Σ tank volume × tank max_density, "
                "from facility config). "
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
                    _dcap = float(r.get("growout_density_cap") or 95.0)
                    fig.add_hline(y=_dcap, line_dash="dash", line_color="red",
                                  annotation_text="cap")
                    fig.add_hline(y=_dcap * 0.85, line_dash="dot",
                                  line_color="orange",
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
                _cap_kg = float((r.get("config_used") or {}).get("max_biomass_kg")
                                or 3_800_000)
                fig.add_hline(y=_cap_kg, line_dash="dash", line_color="red",
                              annotation_text=f"Max Biomass cap ({_cap_kg / 1000:,.0f} t)")
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
                fig.add_hline(y=float(r.get("growout_density_cap") or 95.0),
                              line_dash="dash", line_color="red",
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
            _minw = float((r.get("config_used") or {}).get("min_harvest_weight_g")
                          or 3500) / 1000.0
            fig.add_hline(y=_minw, line_dash="dot", line_color="orange",
                          annotation_text=f"Min harvest weight ({_minw:.1f} kg)")
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
