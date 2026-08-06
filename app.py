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
import os
import sys
import uuid
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
from forecast import methods as _methods  # noqa: E402

# The ONE method list. Board roster, run-mode label and the engine dispatch all
# read this, so adding a method is a single register() call in forecast/methods.
_METHODS = _methods.REGISTRY
# The method ▶ Run forecast uses until the board picks another. The hybrid, not
# the plain controller: once zero-harvest weeks were actually counted (34ecbaf)
# the controller turned out to leave an empty week on 5 of 6 real PRs, which
# breaks the hard steady-harvest contract rule. See forecast/methods.py.
_DEFAULT_METHOD = "controller-hybrid"
# Operator-facing runtime hints + which methods the board runs unprompted.
_TYPICAL = {"controller": "~30 s", "controller-hybrid": "~40 s",
            "controller-lns": "~30 s", "global-lp": "~4 min",
            "global-milp": "~30 min+"}
_BOARD_OPTIONAL = {"global-milp"}          # behind its own checkbox (slow)
# Pseudo-method: run the controller pipeline on the given config EXACTLY as-is,
# with NO registry pins layered on top. Optimize's sweep measures variants that
# way (variant knobs onto the live config, nothing else), so its verification
# runs must too — passing "controller" here would pin hybrid_follow off and
# verify a DIFFERENT engine than every variant the sweep just scored.
_AS_CONFIGURED = _methods.Method(
    key="as-configured", label="As configured", family="Controller",
    blurb="", engine="controller")

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


def _cpu_workers() -> int:
    """Parallel-work budget from the sidebar's "Computer power" percent: that
    share of this machine's logical CPUs, at least 1. One number feeds both
    kinds of heavy work — CP-SAT search threads and Optimize sweep processes."""
    pct = int(st.session_state.get("cpu_pct", 40))
    return max(1, round((os.cpu_count() or 2) * pct / 100.0))


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
    # Key on CONTENT, not (name, size): re-uploading an edited PR under the same
    # name at the same size served the previous parse — stale forecast_start,
    # stale counts and stale validation warnings, silently, for the whole
    # session. Everything downstream anchors on forecast_start, so this is not a
    # cosmetic staleness. The hydration cache below already hashes content; this
    # was the one PR cache that did not. md5 of a few MB is ~milliseconds.
    import hashlib
    key = hashlib.md5(uploaded.getvalue()).hexdigest()
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


def _preserved_facility_limits(fl_cur, shown_weeks):
    """Facility limit records for weeks the grid is NOT showing.

    The limits editor renders only the current forecast horizon but Save
    REPLACES limits.yaml, so anything without a column here would be deleted.
    See _edit_limits. Extracted so this is testable without Streamlit.
    """
    shown = set(shown_weeks)
    return [{"week": wk, "metric": m, "value": v}
            for (wk, m), v in fl_cur.items() if wk not in shown]


def _preserved_system_limits(sl_cur, shown_weeks):
    """System limit records for weeks the grid is NOT showing (see above)."""
    shown = set(shown_weeks)
    return [{"week": wk, "system": s, "metric": m, "value": v}
            for (wk, s, m), v in sl_cur.items() if wk not in shown]


def _result_rid(r):
    """Stable identity of a result dict, for binding derived data to the run it
    came from. Results stored before `_rid` existed fall back to something
    run-unique."""
    r = r or {}
    return r.get("_rid") or f"{r.get('output_path')}|{r.get('elapsed')}"


def _blank(v):
    return v is None or (isinstance(v, str) and not v.strip())


def _clean_rows(records, key_field, what):
    """Drop never-filled rows from a `num_rows="dynamic"` grid, and refuse
    half-filled ones.

    Streamlit's dynamic editor appends an empty row as soon as you click +, and
    it is returned whether or not you type in it. Saved verbatim those become
    junk config: a batch literally named "None", or a facility tank with a null
    tank_id that bricks the NEXT run deep inside precalc. An all-blank row is
    unambiguously an accident, so drop it silently. A row with data but no
    identifier is NOT — the operator typed something and would lose it, so say
    so instead of guessing.
    """
    out = []
    for i, r in enumerate(records, start=1):
        if all(_blank(v) for v in r.values()):
            continue
        if _blank(r.get(key_field)):
            raise ValueError(
                f"row {i} has no {key_field} — every {what} needs one. "
                f"Fill it in, or clear the row to discard it."
            )
        out.append(r)
    return out


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
    "handling_mortality_pct": "Mortality PERCENT applied to fish on each transfer "
        "(divided by 100 before use, unlike the deviation band above): 0.01 = "
        "0.01%, 1 = 1%.",
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
    "density_welfare_threshold_kg_m3": "Welfare density line (kg/m³) — a SOFT quality "
        "threshold below the hard cap (~95). Fish reared above it count as 'crowded'. "
        "The Run 'Reared density' KPI, the Compare 'Best welfare' lens, and the Optimize "
        "'Product quality' preset all measure against it. It only scores/reports — it "
        "does NOT constrain the plan. 80 is the accepted salmon welfare line.",
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
    "density_welfare_threshold_kg_m3": "Welfare density line (kg/m³)",
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
            # control_from_dict coerces to the declared types and raises on a
            # value that cannot be one (e.g. text typed into a knob that is
            # currently null, so it rendered as a text box). Catch it here: the
            # alternative is a saved string that fails much later in arithmetic.
            try:
                _ctl = control_from_dict(new)
            except ValueError as e:
                st.error(f"Not saved — {e}")
            else:
                dump_config(CONFIG_DIR, control=_ctl,
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
            fac2 = facility_from_dict(
                {"tanks": _clean_rows(_records(edited), "tank_id", "tank")})
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
            batches2 = batches_from_list(
                _clean_rows(_records(edited), "batch_id", "batch"))
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
    """Hydrate a FacilityState from the uploaded PR, so the editor can populate
    tank/batch context + dry-run validate events. Returns (state, fw, ctx).

    Cached by PR content hash AND config fingerprint: `ctx` embeds control /
    tables / batches read from disk below, so keying on the PR alone left the
    manual window validating against pre-edit config for the rest of the
    session after any in-app config save."""
    import hashlib
    import io
    data = uploaded.getvalue()
    ck = ("_hydrated_" + hashlib.md5(data).hexdigest() + "_"
          + _config_fingerprint())
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
    # Keep only the current hydration: each entry is a deep FacilityState, and
    # the key now varies with config too, so an un-evicted cache would grow one
    # entry per (upload × config save) for the life of the session.
    for k in [k for k in st.session_state
              if isinstance(k, str) and k.startswith("_hydrated_") and k != ck]:
        st.session_state.pop(k, None)
    st.session_state[ck] = (state, fw, ctx)
    return state, fw, ctx


def _dest_token(d):
    """One `to_tanks` token: `tank`, `tank:count`, `tank@size`, `tank:count@size`."""
    tok = str(d.tank) if d.count is None else f"{d.tank}:{int(d.count)}"
    return f"{tok}@{d.size_class}" if d.size_class else tok


def _parse_dest_token(tok):
    """Inverse of _dest_token -> (tank, count|None, size_class|None)."""
    size = None
    if "@" in tok:
        tok, size = tok.split("@", 1)
        size = size.strip().lower() or None
    if ":" in tok:
        _t, _c = tok.split(":", 1)
        return int(float(_t)), float(_c), size
    return int(float(tok)), None, size


def _manual_events_to_df_rows(events):
    rows = []
    for e in events:
        # Encode per-dest counts as "tank:count" so explicit / UNEQUAL counts
        # round-trip losslessly; a bare "tank" means None (split the `count`
        # column evenly across the bare tanks at run time). "@big"/"@small"
        # carries fw_to_og size routing — WITHOUT it a round-trip through this
        # grid silently collapsed the big/small split, so "Apply to window"
        # quietly planned a different transfer than the one that was saved.
        to_tanks = ",".join(_dest_token(d) for d in e.destinations)
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
        # parse to_tanks: "tank" (bare), "tank:count" (explicit per-dest), either
        # optionally suffixed "@big"/"@small" for fw_to_og size routing. Bare
        # tanks share the `count` column evenly.
        specs = []
        for tok in str(r.get("to_tanks") or "").replace(" ", "").split(","):
            if not tok:
                continue
            specs.append(_parse_dest_token(tok))
        notes = str(r.get("notes") or "")
        if typ in ("og_transfer", "og_to_6n"):
            bare = [t for t, c, _s in specs if c is None]
            per_bare = (count / len(bare)) if (bare and count is not None) else None
            dests = [ManualDest(tank=t, count=(c if c is not None else per_bare),
                                size_class=s)
                     for t, c, s in specs]
            out.append(ManualEvent(type=typ, week=week, from_tank=from_tank,
                                   destinations=dests, batch=batch, notes=notes))
        elif typ == "harvest":
            out.append(ManualEvent(type=typ, week=week, from_tank=from_tank,
                                   count=count, batch=batch, notes=notes))
        elif typ == "fw_to_og":
            # size_class is the whole point here: it routes the big half of the
            # FW cohort to one tank and the small half to another.
            dests = [ManualDest(tank=t, size_class=s) for t, c, s in specs]
            out.append(ManualEvent(type=typ, week=week, batch=batch, count=count,
                                   destinations=dests, notes=notes))
        elif typ == "graded_harvest":
            # from_tank = source, count = biggest-N to harvest, to_tanks =
            # pickup[,retention] (retention defaults to the source).
            dests = [ManualDest(tank=t, size_class=s) for t, c, s in specs]
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
    heavy biology projection / validation across idle reruns.

    Includes the config fingerprint: the projection and the reject-at-entry
    validation both run against control/tables/batches, so an in-app config
    save has to invalidate them — otherwise the editor keeps judging events
    against pre-edit knobs while ▶ Run forecast reads the fresh YAML."""
    import hashlib
    import json
    from forecast.manual_events import manual_events_to_list
    payload = json.dumps({"e": manual_events_to_list(events), "x": extra,
                          "pr": st.session_state.get("_mw_pr_key", ""),
                          "cfg": _config_fingerprint()},
                         sort_keys=True, default=str)
    return hashlib.md5(payload.encode()).hexdigest()


def _mw_project(state, ctx, events, n_weeks, view="open"):
    """Project the facility through `n_weeks` of the override window (operator
    events + full biology) on a COPY of the hydrated state — the SAME engine the
    real run uses (forecast.manual_window.advance_facility_window) — and return
    (rows, labels, moves) for `view`:

      view="open"  → start-of-week, pre-event, pre-biology snapshot (what's in
                     the tank WHEN you click to act on it).
      view="close" → end-of-week, post-event, post-biology snapshot (what holds
                     fish and what's empty at week's end; a tank you harvest or
                     move shows empty here).

    `moves` is the view-independent list of tank→tank relocations that happened
    in the window (OG→OG splits and OG→6N sends), one dict per Transfer:
    {"week": label, "src": tank_id, "dests": [tank_id,...], "batch", "count"}.
    The grid uses it to light up BOTH ends of a move in the week it fires — the
    end that actually holds the fish in this view solid, the counterpart end as
    a ghost arrow — so a relocation is legible without diffing open vs close.

    Both snapshots + the moves come from ONE projection and are cached together
    by (PR, events, n_weeks), so toggling the view or clicking around never
    recomputes biology."""
    import copy
    from datetime import timedelta
    from forecast.manual_window import advance_facility_window
    from forecast.time_grid import forecast_week_labels
    # "proj3" (bump on every cached-schema change) so a stale cache from an
    # older schema can never satisfy this {sig,open,close,labels,moves} read.
    sig = _mw_sig(events, extra=f"proj4:{n_weeks}")  # view is NOT in the sig:
    cache = st.session_state.get("_mw_proj_cache")    # one projection feeds both
    if not (cache and cache.get("sig") == sig):
        labels = forecast_week_labels(ctx["forecast_start"], n_weeks)
        moves: list[dict] = []
        err = None
        try:
            sc = copy.deepcopy(state)
            win = advance_facility_window(
                sc, ctx["batch_by_id"], ctx["tables"], ctx["forecast_start"],
                n_weeks, events=events, control=ctx["control"],
                pr_closing=ctx["pr_closing"], fw_records=ctx["fw_records"])
            # Fall back to the closing snapshot for older engines without an
            # opening_locations, and vice-versa, so neither view blanks out.
            open_rows = win.get("opening_locations") or win["batch_locations"]
            close_rows = win.get("batch_locations") or open_rows
            # Each Transfer is dated at its week's start (forecast_start + i*7),
            # the same arithmetic the window loop uses — map it back to a label.
            label_by_date = {ctx["forecast_start"] + timedelta(days=7 * i): lbl
                             for i, lbl in enumerate(labels)}
            for tr in (win.get("transfer_events") or []):
                lbl = label_by_date.get(getattr(tr, "event_date", None))
                if lbl is None:
                    continue
                if hasattr(tr, "pickup_tank_id"):
                    # GradedHarvest carries pickup/retention tanks, not
                    # .destinations — without this branch a scripted grading
                    # draws NO arrows and the fish just "appear" in the grid.
                    dests = [tr.pickup_tank_id]
                    cnt = float(tr.pickup_count)
                    if tr.retention_tank_id != tr.source_tank_id:
                        dests.append(tr.retention_tank_id)
                        cnt += float(tr.retention_count)
                    moves.append({"week": lbl, "src": tr.source_tank_id,
                                  "dests": dests, "batch": tr.batch_id,
                                  "count": cnt})
                    continue
                dests = [a.tank_id for a in getattr(tr, "destinations", []) or []]
                if not dests:
                    continue
                moves.append({"week": lbl, "src": tr.source_tank_id,
                              "dests": dests, "batch": tr.batch_id,
                              "count": getattr(tr, "count_transferred", 0.0)})
        except Exception as e:  # noqa: BLE001 — a bad event must not blank the view
            # Record WHY. Empty rows are indistinguishable from "facility is
            # fine" downstream, so a silent failure here reports an all-clear.
            open_rows, close_rows = [], []
            err = f"{type(e).__name__}: {e}"
        cache = {"sig": sig, "open": open_rows, "close": close_rows,
                 "labels": labels, "moves": moves, "error": err}
        st.session_state["_mw_proj_cache"] = cache
    rows = cache["close"] if view == "close" else cache["open"]
    return rows, cache["labels"], cache.get("moves", [])


def _mw_proj_error():
    """Why the last _mw_project failed, or None.

    A failed projection caches EMPTY rows, and empty rows read downstream as
    "nothing in the facility" — which renders as a clean bill of health rather
    than a failure. Callers must check this before showing any all-within-limits
    verdict, or a crashed projection silently reports the safest possible answer.
    """
    return (st.session_state.get("_mw_proj_cache") or {}).get("error")


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


def _mw_fw_load(ctx, window_labels):
    """{week_label: {"open_bio": kg, "close_bio": kg, "feed": kg/day}} summed over
    every in-flight FW cohort still in freshwater in the window — the standing FW
    LOAD to fold into the system rollup as an "FW" row + into the facility total.

    Same projection as _mw_fw_avail, but keeps biomass + feed. The feed figure is
    the projection's own stage-correct daily feed (FW-stage SGR/FCR while the fish
    are in freshwater) — NOT realized_feed_kg_day, which assumes seawater. FW fish
    are fed in the freshwater area, not from OG feed capacity, so the rollup shows
    this row neutral (uncapped) and only rolls it into the neutral TOTAL."""
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
    out: dict[str, dict] = defaultdict(
        lambda: {"open_bio": 0.0, "close_bio": 0.0, "feed": 0.0})
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
        for s in states:
            if s.stage != "FW" or s.week_label not in win:
                continue
            rec = out[s.week_label]
            rec["open_bio"] += s.open_biomass_kg or s.biomass_kg
            rec["close_bio"] += s.close_biomass_kg or s.biomass_kg
            rec["feed"] += s.feed_kg_day
    return dict(out)


def _mw_grid(state, rows, labels, color_by, batch_filter=None, moves=None):
    """Colour-styled DataFrame of the projected facility (index = tank, columns =
    weeks, cell text = "batch · avg-weight · density") for a CLICKABLE st.dataframe.
    The per-cell weight + density let you read grow-out state at a glance to decide
    moves without clicking every tank. color_by 'fill'
    shades by density-vs-cap (green→red); 'batch' gives each batch its own colour.
    `batch_filter` (a set of batch ids, or None) restricts the rows to only the
    tanks that hold one of those batches in some displayed week — so the operator
    can focus on a few cohorts instead of the whole facility; batch COLOURS stay
    consistent with the unfiltered view.

    `moves` (from _mw_project) lights up BOTH ends of a relocation in the week it
    fires: the tank that holds the fish in this snapshot gets a solid cell with a
    trailing arrow (⇢ leaving / ⇠ arrived), and the counterpart tank — empty in
    this snapshot — gets a faint GHOST cell naming where the fish went / came
    from. So a move reads at a glance instead of by diffing open vs close.

    Returns (styler, ylabels, tank_by_y). Unlike a plotly heatmap, a single click
    on a dataframe row reliably emits a Streamlit selection."""
    from collections import defaultdict
    from forecast.sixn import SIXN_ALL_TANKS
    idx = {(r.tank_id, r.week_label): r for r in rows}
    loc_by_tank = {t.tank_id: t.location_id for t in state.tanks_by_id.values()}

    def _uniq(seq):
        seen, out = set(), []
        for x in seq:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return ",".join(out)

    # Per-(tank, week) move markers: out_to = where this tank's fish LEAVE to;
    # in_from = where a tank's fish ARRIVE from. Keyed by location label so the
    # arrow text reads OG1N/6N-63 (what the operator sees), not a raw tank id.
    out_to: dict = defaultdict(list)
    in_from: dict = defaultdict(list)
    for m in (moves or []):
        wk, src = m["week"], m["src"]
        out_to[(src, wk)].extend(loc_by_tank.get(d, f"#{d}") for d in m["dests"])
        for d in m["dests"]:
            in_from[(d, wk)].append(loc_by_tank.get(src, f"#{src}"))

    tanks = _mw_tanks(state)
    if batch_filter:
        keep = {r.tank_id for r in rows
                if r.count > 0 and r.batch_id in batch_filter}
        # keep both ends of a filtered batch's moves, even where a snapshot shows
        # the tank empty (the ghost end) — else half the move would vanish.
        for m in (moves or []):
            if m["batch"] in batch_filter:
                keep.add(m["src"])
                keep.update(m["dests"])
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
            outs = out_to.get((t.tank_id, wk))
            ins = in_from.get((t.tank_id, wk))
            if r is not None and r.count > 0:
                # Solid occupancy cell. If this tank is a move end THIS week,
                # trail an arrow: ⇢ when the fish here are leaving (open view of
                # a source), ⇠ when they arrived (close view of a destination).
                arrow = (f"  ⇢{_uniq(outs)}" if outs
                         else f"  ⇠{_uniq(ins)}" if ins else "")
                trow.append(
                    f"{r.batch_id} · {r.avg_wt_g / 1000:.2f}kg · "
                    f"{r.density_kg_m3:.0f}{arrow}")
                if color_by == "batch":
                    # background already encodes the batch; keep a dark bold font.
                    bg, fg = bcolor.get(r.batch_id, "#cccccc"), "#1f1f1f"
                else:
                    bg = _fill_hex((r.density_kg_m3 / cap) if cap > 0 else 0.0)
                    base = fcolor.get(r.batch_id, "#1f1f1f")
                    fg = _toward_white(base, 0.7) if _lum(bg) < 0.6 else base
                crow.append(f"background-color:{bg};color:{fg};font-weight:700")
            elif outs or ins:
                # GHOST counterpart: this end is empty in THIS snapshot, but a
                # move touched it this week — show where the fish went / came
                # from so both ends of the relocation are visible in one column.
                trow.append(f"⇢ {_uniq(outs)}" if outs else f"⇠ {_uniq(ins)}")
                crow.append("background-color:#e3e7f0;color:#3a4a7a;"
                            "font-style:italic;font-weight:600")
            else:
                trow.append("")
                crow.append("background-color:#f0f0f0;color:#1f1f1f")
        text_grid.append(trow)
        css_grid.append(crow)
    df = pd.DataFrame(text_grid, index=ylabels, columns=labels)
    css_df = pd.DataFrame(css_grid, index=ylabels, columns=labels)
    styler = df.style.apply(lambda _: css_df, axis=None)
    return styler, ylabels, tank_by_y


def _mw_system_rollup(state, rows, labels, tables, batch_by_id, ctx=None,
                      view="open"):
    """Render, under the grid, per-SYSTEM **biomass (tonnes)** and **feed
    (kg/day)** tables (systems = rows, weeks = columns). Cells are coloured
    green→red by fraction of the system's tank capacity (biomass = Σ volume ×
    max_density; feed = Σ max_feed/day) so system-level capacity pressure — which
    the per-tank grid can't show, especially FEED — is visible at a glance.

    Follows the grid's Week open / Week close `view`. When `ctx` is supplied, a
    neutral **FW (freshwater)** row folds the standing freshwater cohorts into
    each table + the facility TOTAL (see _mw_fw_load); STARVE (6N) tanks feed 0."""
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

    # Standing FW load as a neutral row folded into the TOTAL (biomass matches the
    # view's open/close snapshot; feed is the FW-stage projected daily feed).
    fw = _mw_fw_load(ctx, labels) if ctx else {}
    fw_bio = {wk: v["open_bio" if view == "open" else "close_bio"]
              for wk, v in fw.items()}
    fw_feed = {wk: v["feed"] for wk, v in fw.items()}
    FW_LABEL = "FW (freshwater)"

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

    # Feed is coloured by ABSOLUTE kg/day/system load (operator setting), not by the
    # per-system cap fraction: green below FEED_AMBER, amber between, red at/above
    # FEED_RED. Change the two numbers to retune the thresholds.
    FEED_AMBER, FEED_RED = 2600.0, 2800.0

    def _fill_feed_abs(v):
        if v <= 0:
            return "#f0f0f0"
        if v < FEED_AMBER:
            return "#a8d5a8"
        if v < FEED_RED:
            return "#f5d49a"
        return "#e8615e"

    def _table(agg, cap, scale, extra=None, color_of=None):
        # extra = {wk: raw_value} for a neutral (uncapped) FW row folded into the
        # facility TOTAL — FW fish aren't in an OG system, so no cap fraction.
        # color_of(value, system_cap) -> css colour; default = fraction-of-cap fill.
        if color_of is None:
            color_of = lambda v, caps: _fill((v / caps) if caps else 0.0)  # noqa: E731
        txt, css, idx = [], [], []
        for s in systems:
            idx.append(s)
            trow, crow = [], []
            for wk in labels:
                v = agg.get((s, wk), 0.0)
                trow.append(f"{v * scale:,.0f}")
                crow.append(f"background-color:{color_of(v, cap.get(s, 0.0))};"
                            f"color:#1f1f1f")
            txt.append(trow)
            css.append(crow)
        if extra:
            idx.append(FW_LABEL)
            trow, crow = [], []
            for wk in labels:
                trow.append(f"{extra.get(wk, 0.0) * scale:,.0f}")
                crow.append("background-color:#eef1e8;color:#3a4a2a;"
                            "font-style:italic")
            txt.append(trow)
            css.append(crow)
        idx.append("TOTAL")           # facility total (neutral, not cap-coloured)
        trow, crow = [], []
        for wk in labels:
            tot = sum(agg.get((s, wk), 0.0) for s in systems)
            if extra:
                tot += extra.get(wk, 0.0)
            trow.append(f"{tot * scale:,.0f}")
            crow.append("background-color:#e8eaf0;color:#1f1f1f;font-weight:700")
        txt.append(trow)
        css.append(crow)
        d = pd.DataFrame(txt, index=idx, columns=labels)
        c = pd.DataFrame(css, index=idx, columns=labels)
        return d.style.apply(lambda _: c, axis=None)

    _w = "open" if view == "open" else "close"
    _nrows = len(systems) + 1 + (1 if fw else 0)
    _h = min(560, 44 + 33 * _nrows)
    _fwnote = (f" The **{FW_LABEL}** row is standing freshwater cohorts — fed in "
               "the FW area (no OG cap), shown neutral and folded only into the "
               "TOTAL." if fw else "")
    # Render the matrices in the SAME left 3/5 column the tank grid uses (the
    # st.columns([3, 2]) split above the grid) and let them fill it, so the
    # biomass/feed tables line up under the grid at the same width.
    _mcol, _ = st.columns([3, 2], gap="medium")
    with _mcol:
        st.caption(f"Biomass colour = fraction of each OG system's tank capacity "
                   f"(green roomy, amber near cap, red over).{_fwnote} Same "
                   f"week-{_w} state as the grid.")
        st.markdown(f"**{_w.capitalize()} biomass — tonnes / system / week**")
        st.dataframe(_table(bio, sys_bio_cap, 0.001, fw_bio if fw else None),
                     use_container_width=True, height=_h)
        st.markdown(f"**{_w.capitalize()} feed — kg/day / system / week** "
                    f"(6N depuration eats 0)")
        st.caption(f"Feed colour = absolute load: green < {FEED_AMBER:,.0f} · amber "
                   f"{FEED_AMBER:,.0f}–{FEED_RED:,.0f} · red ≥ {FEED_RED:,.0f} "
                   f"kg/day/system.")
        st.dataframe(_table(feed, sys_feed_cap, 1.0, fw_feed if fw else None,
                            color_of=lambda v, caps: _fill_feed_abs(v)),
                     use_container_width=True, height=_h)


def _mw_recommendations(state, rows, labels, ctx, view="open"):
    """Rank the projected window's cap breaches (most out of bounds FIRST) and
    suggest a relief action for each — harvest the heaviest tank in the offending
    system when it's at harvest weight, else move it to the system with the most
    feed headroom; a per-tank density breach recommends splitting that tank.

    Covers per-system FEED, per-system BIOMASS, per-tank DENSITY and FACILITY
    biomass, all against the SAME caps the System-rollup shows (tank-derived
    system caps + the Control facility cap). Reads the current `rows`, so as the
    operator scripts harvests/moves the breaches shrink. Returns
    (collapsed_worst_first, weeks_with_breaches, {week: breaches_worst_first}); each
    breach is {frac, week, tank_id, msg, action}, `week` the ISO week-of-year label
    (the project's canonical id — forecast.time_grid). The by-week map + week list
    drive the panel's week picker; `collapsed` is the 'all weeks' default (each
    distinct breach shown once, at its worst week)."""
    from collections import defaultdict
    from forecast.biology import realized_feed_kg_day
    from forecast.sixn import SIXN_ALL_TANKS
    control, tables = ctx.get("control"), ctx.get("tables")
    batch_by_id = ctx.get("batch_by_id") or {}
    min_hg = float(getattr(control, "min_harvest_weight_g", 0) or 0)
    fac_bio_cap = float(getattr(control, "max_biomass_kg", 0) or 0)

    sys_bio_cap, sys_feed_cap, tank_cap = defaultdict(float), defaultdict(float), {}
    # 6N systems are depuration (off-feed, harvest-staging) — never a valid relief
    # destination and not grow-out feed/biomass constraints, so exclude them.
    sixn_systems = {t.system_id for t in state.tanks_by_id.values()
                    if t.tank_id in SIXN_ALL_TANKS}
    for t in state.tanks_by_id.values():
        if t.type != "OG":
            continue
        sys_bio_cap[t.system_id] += (t.volume_m3 or 0.0) * (t.max_density_kg_m3 or 0.0)
        sys_feed_cap[t.system_id] += (t.max_feed_kg_day_cap or 0.0)
        tank_cap[t.tank_id] = t.max_density_kg_m3 or 0.0

    sys_bio, sys_feed, fac_bio = (defaultdict(float), defaultdict(float),
                                  defaultdict(float))
    rows_by_sw, rows_by_w = defaultdict(list), defaultdict(list)
    for r in rows:
        if r.count <= 0:
            continue
        sys_bio[(r.system_id, r.week_label)] += r.biomass_kg
        fac_bio[r.week_label] += r.biomass_kg
        rows_by_sw[(r.system_id, r.week_label)].append(r)
        rows_by_w[r.week_label].append(r)
        if getattr(r, "stage", "") != "STARVE":
            sys_feed[(r.system_id, r.week_label)] += realized_feed_kg_day(
                r.avg_wt_g, r.biomass_kg, batch_by_id.get(r.batch_id), tables)

    def _loc(tid):
        t = state.tanks_by_id.get(tid)
        return t.location_id if t else f"#{tid}"

    def _heaviest(rws):
        cand = [x for x in rws if x.count > 0 and x.tank_id not in SIXN_ALL_TANKS]
        return max(cand, key=lambda x: x.avg_wt_g) if cand else None

    def _roomiest_feed(wk, exclude):
        best, best_head = None, 0.0
        for s, cap in sys_feed_cap.items():
            if s == exclude or s in sixn_systems:  # never relocate INTO 6N
                continue
            head = cap - sys_feed.get((s, wk), 0.0)
            if head > best_head:
                best, best_head = s, head
        return best, best_head

    def _shed_action(rws, sysid, wk):
        """Harvest the heaviest ready tank, else move it to a roomier system."""
        tank = _heaviest(rws)
        if tank is None:
            return "no non-6N tank here to relieve — check 6N / FW inflow", None
        loc = _loc(tank.tank_id)
        if tank.avg_wt_g >= min_hg > 0:
            return (f"**Harvest {loc}** ({tank.batch_id} @ "
                    f"{tank.avg_wt_g / 1000:.2f} kg)"), tank.tank_id
        dest, head = _roomiest_feed(wk, sysid)
        if dest and head > 0:
            return (f"**Move {loc} → {dest}** (feed room ~{head:,.0f} kg/day) — "
                    f"too light to harvest ({tank.avg_wt_g / 1000:.2f} kg)"), tank.tank_id
        return (f"**Move {loc} off {sysid}** — no system has feed headroom, "
                f"consider 6N"), tank.tank_id

    breaches = []
    for (s, wk), used in sys_feed.items():
        cap = sys_feed_cap.get(s, 0.0)
        if s not in sixn_systems and cap > 0 and used > cap:
            breaches.append(("feed", s, wk, used, cap))
    for (s, wk), used in sys_bio.items():
        cap = sys_bio_cap.get(s, 0.0)
        if s not in sixn_systems and cap > 0 and used > cap:
            breaches.append(("sysbio", s, wk, used, cap))
    for r in rows:
        if r.count <= 0 or r.tank_id in SIXN_ALL_TANKS:
            continue
        cap = tank_cap.get(r.tank_id, 0.0)
        if cap > 0 and r.density_kg_m3 > cap:
            breaches.append(("dens", r.tank_id, r.week_label, r.density_kg_m3, cap))
    # FACILITY biomass must be FW-INCLUSIVE to match the cap it is judged against.
    # `rows` are TANK rows, so they cover OG + 6N only; the freshwater cohorts are
    # tracked separately and were simply missing here. The engine's setpoint counts
    # FW (see the dual-limit setpoint), so leaving it out understated facility
    # biomass and showed headroom that does not exist — the same OG-only-vs-
    # FW-inclusive mismatch already fixed in the results biomass charts.
    _fw_load = _mw_fw_load(ctx, labels) if ctx else {}
    # Match the FW snapshot to the view `rows` came from — mixing open OG rows
    # with close FW biomass would disagree with the rollup TOTAL beside it.
    _fw_key = "open_bio" if view == "open" else "close_bio"
    for wk, used in fac_bio.items():
        used += float((_fw_load.get(wk) or {}).get(_fw_key, 0.0) or 0.0)
        if fac_bio_cap > 0 and used > fac_bio_cap:
            breaches.append(("facbio", None, wk, used, fac_bio_cap))

    out = []
    for kind, where, wk, used, cap in breaches:
        frac = used / cap if cap else 0.0
        if kind == "feed":
            act, tid = _shed_action(rows_by_sw.get((where, wk), []), where, wk)
            msg = (f"**{where}** feed {used:,.0f} / {cap:,.0f} kg/day "
                   f"({frac * 100:.0f}%)")
        elif kind == "sysbio":
            act, tid = _shed_action(rows_by_sw.get((where, wk), []), where, wk)
            msg = (f"**{where}** biomass {used / 1000:.1f} / {cap / 1000:.1f} t "
                   f"({frac * 100:.0f}%)")
        elif kind == "dens":
            tid = where
            act = f"**Split {_loc(where)}** — move part to an empty / roomier tank"
            msg = (f"**{_loc(where)}** density {used:.0f} / {cap:.0f} kg/m³ "
                   f"({frac * 100:.0f}%)")
        else:  # facbio — only harvest reduces the facility total
            tank = _heaviest(rows_by_w.get(wk, []))
            tid = tank.tank_id if tank else None
            act = (f"**Harvest {_loc(tid)}** (heaviest ready, {tank.batch_id} @ "
                   f"{tank.avg_wt_g / 1000:.2f} kg)"
                   if tank and tank.avg_wt_g >= min_hg > 0
                   else "**Harvest facility-wide** — total over cap")
            msg = (f"**Facility** biomass {used / 1000:.1f} / {cap / 1000:.1f} t "
                   f"({frac * 100:.0f}%)")
        out.append({"frac": frac, "week": wk, "tank_id": tid,
                    "msg": msg, "action": act,
                    "key": (kind, where if where is not None else "FAC")})
    # Per-week view (everything out of bounds in a chosen week) + the ordered set
    # of weeks that have ANY breach, for the panel's week picker.
    by_week: dict = {}
    for o in out:
        by_week.setdefault(o["week"], []).append(o)
    for _w in by_week:
        by_week[_w].sort(key=lambda x: -x["frac"])
    breach_weeks = sorted(by_week, key=lambda w: labels.index(w) if w in labels else 10**6)
    # Collapse a breach that recurs across weeks to its WORST week, so the default
    # 'all weeks' top-N shows distinct problems instead of one tank every week.
    worst = {}
    for o in out:
        if o["key"] not in worst or o["frac"] > worst[o["key"]]["frac"]:
            worst[o["key"]] = o
    collapsed = sorted(worst.values(), key=lambda x: -x["frac"])
    return collapsed, breach_weeks, by_week


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
    head[0].markdown(f"#### ▶ {loc} — {wlabel}{ds}")
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
        if st.button(f"➕ Add harvest in {wlabel}", key="mw_h_add", type="primary"):
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
        # Live cut-weight readout — the SAME split the run applies. CV travels
        # with the BATCH through every event, so read the hydrated tank's cv
        # only while it still holds this batch; if the window moved fish here,
        # fall back to any hydrated tank of the batch, then the batch's PR cv —
        # the hydration-time cv of the clicked tank would be a stale 0.
        _cvt = state.tanks_by_id.get(tid)
        if _cvt is not None and _cvt.batch_id == r.batch_id and _cvt.cv_pct:
            _cv = _cvt.cv_pct
        else:
            _cv = next((t.cv_pct for t in state.tanks_by_id.values()
                        if t.batch_id == r.batch_id and t.cv_pct), 0.0)
            if not _cv:
                _bm = (ctx.get("batch_by_id") or {}).get(r.batch_id)
                _cv = float(getattr(_bm, "tran_og_cv", 0.0) or 0.0)
        _big, _small = _mw_cut_weights(r.avg_wt_g, _cv, r.count, n_big)
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
        if st.button(f"➕ Add graded 6N move in {wlabel}", key="mw_g6_add",
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
        if st.button(f"➕ Add move in {wlabel}", key="mw_m_add", type="primary",
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
        if st.button(f"➕ Add 6N move in {wlabel}", key="mw_6_add", type="primary",
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
    # Seed both pickers from durable copies: adding/deleting a grid event calls
    # st.rerun() BEFORE this section renders, so Streamlit drops the widget-
    # backed keys on that interrupted pass and the selection silently snapped
    # back to the first cohort (same cleanup mechanism as the cpsat_depth fix).
    _opts = sorted(avail)
    _saved_bid = st.session_state.get("_mw_fw_batch_saved")
    bid = st.selectbox("Freshwater cohort", options=_opts, key="mw_fw_batch",
                       index=(_opts.index(_saved_bid)
                              if _saved_bid in _opts else 0))
    st.session_state["_mw_fw_batch_saved"] = bid
    wk_labels = [w for w in labels if w in avail.get(bid, {})]
    if not wk_labels:
        st.caption("This cohort has already crossed to seawater in this window.")
        return
    _saved_wk = st.session_state.get("_mw_fw_week_saved")
    wlabel = st.selectbox(
        "Week to bring it in", options=wk_labels,
        format_func=lambda w: f"{w}"
        + (f" · {date_for[w].strftime('%b %d')}" if date_for.get(w) else ""),
        key="mw_fw_week",
        index=(wk_labels.index(_saved_wk) if _saved_wk in wk_labels else 0))
    st.session_state["_mw_fw_week_saved"] = wlabel
    cnt, _wt, _cv = avail[bid][wlabel]
    wk = labels.index(wlabel) + 1
    # Scope input keys to THIS cohort+week — a fixed key would keep the previous
    # cohort's target count / tank picks alive (Streamlit ignores value= once a
    # key has state), silently scripting e.g. a huge bottom-cull on the new one.
    sfx = f"{bid}_{wlabel}"

    # Planned vs. current: the batch's originally-scheduled TranOG (from the PR /
    # scenario) next to what you're actually picking, so you can see how far off
    # the plan — in timing and in size — this intake is.
    from forecast.time_grid import iso_week_label, _as_date
    b_meta = ctx["batch_by_id"].get(bid)
    # tran_og_date may be a datetime; normalise to date so it subtracts cleanly
    # against the (date) week-starts below.
    _pd = getattr(b_meta, "tran_og_date", None) if b_meta else None
    p_date = _as_date(_pd) if _pd else None
    p_wt = getattr(b_meta, "tran_og_avg_wt_g", None) if b_meta else None
    p_week = iso_week_label(p_date) if p_date else "—"
    p_wt_s = f"{p_wt / 1000:.2f} kg" if p_wt else "—"
    st.markdown(
        "| | Transfer week | Avg weight |\n"
        "|---|---|---|\n"
        f"| **Planned** (PR) | {p_week} | {p_wt_s} |\n"
        f"| **This intake** | {wlabel} | {_wt / 1000:.2f} kg |")
    # One-line read-out of the deltas that matter operationally.
    notes = []
    cur_date = date_for.get(wlabel)
    if p_date and cur_date:
        dwk = round((cur_date - p_date).days / 7)
        notes.append("same week as planned" if dwk == 0 else
                     f"{abs(dwk)} wk {'earlier' if dwk < 0 else 'later'} than planned")
    if p_wt:
        dwt = (_wt - p_wt) / 1000.0
        notes.append("on planned weight" if abs(dwt) < 0.005 else
                     f"{abs(dwt):.2f} kg {'lighter' if dwt < 0 else 'heavier'} "
                     f"than planned")
    if notes:
        st.caption("↳ " + " · ".join(notes))
    # The target is judged AFTER handling mortality (validate_manual_events:
    # avail = fw_count * (1 - handling/100)) — defaulting to the raw FW count
    # would pre-fill a target the validator itself rejects as infeasible.
    _hf = float(getattr(ctx.get("control"), "handling_mortality_pct", 0.0)
                or 0.0) / 100.0
    avail_sw = cnt * (1.0 - _hf)
    st.caption(f"Projected freshwater state: ~{cnt:,.0f} fish at {wlabel}; "
               f"after handling mortality **~{avail_sw:,.0f} can enter "
               f"seawater**. Target is the count entering seawater (the "
               f"engine culls down to it).")
    target = st.number_input("Target fish entering seawater", min_value=0.0,
                             value=float(avail_sw), step=1000.0,
                             key=f"mw_fw_target_{sfx}")

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
        format_func=dfmt, key=f"mw_fw_big_{sfx}")
    # A tank can't hold both grades — drop the big picks from the small options.
    small_opts = [t for t in empty_og if t not in big_picks]
    small_picks = st.multiselect(
        "Tank(s) for the SMALLER grade", options=small_opts,
        format_func=dfmt, key=f"mw_fw_small_{sfx}")

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
    if st.button(f"➕ Add FW→OG intake in {wlabel}", key="mw_fw_add",
                 type="primary", disabled=bool(gaps) or not dests):
        _mw_add(ManualEvent(type="fw_to_og", week=wk, batch=bid, count=target,
                            destinations=dests))
        st.rerun()


# ---- Readback timeline + save bar ----

def _mw_iso_week(week, forecast_start):
    """ISO week-of-year label (e.g. '2026-W08') for a 1-based override-window week —
    the project's canonical week identifier (forecast.time_grid), matching the grid
    columns, the feed matrix and the co-pilot. Falls back to 'Wk N' when
    forecast_start is unknown (non-hydrated PR)."""
    from forecast.time_grid import week_label
    if forecast_start is None or not week:
        return f"Wk {week}"
    try:
        return week_label(int(week) - 1, forecast_start)
    except Exception:  # noqa: BLE001
        return f"Wk {week}"


def _mw_move_amount(ev):
    """How much a transfer/6N event actually moves, for the timeline label.

    These event types carry their counts PER DESTINATION, not on ev.count — so
    reading ev.count alone always found None and every partial move was
    described as "whole tank". That misreads what the plan will do, and it is
    exactly what a grid round-trip produces (each dest gets an explicit count).
    Only claim a number when EVERY destination has one; a mix of explicit and
    bare destinations is genuinely ambiguous (the bare ones split the remainder).
    """
    ds = ev.destinations or []
    if ev.count is not None:
        return f"{ev.count:,.0f}"
    if ds and all(d.count is not None for d in ds):
        return f"{sum(d.count for d in ds):,.0f}"
    return "whole tank"


def _mw_event_summary(state, ev, forecast_start=None):
    loc = lambda t: _mw_loc(state, t)  # noqa: E731
    dests = ", ".join(loc(d.tank) for d in ev.destinations) or "—"
    wk = _mw_iso_week(ev.week, forecast_start)   # week-of-year, like the matrix
    if ev.type == "harvest":
        amt = f"{ev.count:,.0f} fish" if ev.count is not None else "the whole tank"
        return f"{wk}: **Harvest** {amt} from {loc(ev.from_tank)}"
    if ev.type == "og_transfer":
        return (f"{wk}: **Move** {_mw_move_amount(ev)} from "
                f"{loc(ev.from_tank)} → {dests}")
    if ev.type == "og_to_6n":
        return (f"{wk}: **Send to 6N** {_mw_move_amount(ev)} from "
                f"{loc(ev.from_tank)} → {dests}")
    if ev.type == "fw_to_og":
        tgt = f"target {ev.count:,.0f}" if ev.count else "all available"
        big = ", ".join(loc(d.tank) for d in ev.destinations
                        if (d.size_class or "").lower() == "big")
        small = ", ".join(loc(d.tank) for d in ev.destinations
                          if (d.size_class or "").lower() == "small")
        if big or small:
            return (f"{wk}: **FW→OG** {ev.batch} → bigger {big or '—'} · "
                    f"smaller {small or '—'} ({tgt})")
        return f"{wk}: **FW→OG** {ev.batch} → {dests} ({tgt})"
    if ev.type == "graded_harvest":
        from forecast.sixn import SIXN_ALL_TANKS
        amt = f"{ev.count:,.0f}" if ev.count else "?"
        pk_id = ev.destinations[0].tank if ev.destinations else None
        pk = loc(pk_id) if pk_id is not None else "—"
        ret = (loc(ev.destinations[1].tank) if len(ev.destinations) >= 2
               else "source")
        if pk_id in SIXN_ALL_TANKS:
            return (f"{wk}: **Graded → 6N** biggest {amt} from "
                    f"{loc(ev.from_tank)} → 6N {pk}, retain smaller in {ret}")
        return (f"{wk}: **Graded harvest** biggest {amt} from "
                f"{loc(ev.from_tank)} (via {pk}), retain smaller in {ret}")
    return f"{wk}: {ev.type}"


def _mw_timeline(state, events, bad, forecast_start=None):
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
                st.markdown(f"❌ {_mw_event_summary(state, ev, forecast_start)}")
                st.caption("&nbsp;&nbsp;&nbsp;↳ " + "; ".join(problems))
            else:
                st.markdown(f"✅ {_mw_event_summary(state, ev, forecast_start)}")
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
        "to_tanks: comma-separated; `tank:count` for an explicit per-tank "
        "amount; `tank@big` / `tank@small` to route an fw_to_og size split "
        "(combinable: `45:1000@small`).")
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
    # -1 is _mw_validate's "validation itself crashed" sentinel — it has no
    # timeline row, so without this branch the bar says "fix the ❌ rows above"
    # while every row shows ✅ and the actual exception is displayed nowhere.
    _sentinel = (bad or {}).get(-1)
    _n_bad = len([k for k in (bad or {}) if k != -1])
    if n and not bad:
        st.success(f"All {n} operation(s) feasible against the uploaded PR.")
    elif _sentinel:
        st.error("Couldn't validate the window — " + "; ".join(_sentinel)
                 + ". Saving is disabled until validation runs; check the "
                   "operations in the raw table below, or ↻ Reload from file.")
        if _n_bad:
            st.warning(f"{_n_bad} operation(s) infeasible — fix the ❌ rows "
                       f"above before saving.")
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


def _mw_copilot(uploaded, events, forecast_start=None, bad=None):
    """Human-in-the-loop co-pilot (Sequential). Runs the planners forward ONCE
    from the scripted window (respect mode — your transfers are fixed) and lets
    you browse the recommended moves for the next several weeks. Only the NEXT
    week is approvable: ticked moves append as that week's operations, extending
    the window by one week — then run again for the week after. The look-ahead
    weeks are projections from the current plan and refresh once the nearer weeks
    are scripted. Engine is forecast.copilot (UI-free); this is just the shell.

    `bad` is the save bar's infeasibility map. Both buttons here write
    scenario/manual_events.yaml — the same file ▶ Run forecast reads — so they
    honour the same reject-at-entry gate the Save button does."""
    import shutil
    import tempfile
    from forecast.copilot import propose_upcoming, to_manual_events
    from forecast.manual_events import dump_manual_events
    _n = max((e.week or 1) for e in events) if events else 0
    _LOOKAHEAD = 6
    _blocked = bool(bad)
    if _blocked:
        st.warning(f"{len(bad)} operation(s) in the window are infeasible — fix the "
                   "❌ rows above before running the co-pilot. It has to save your "
                   "window to disk first, and that is the file the forecast reads.")
    # A stale proposal is worse than none: it was computed against a different
    # working set / PR, so its week number and tank picks may no longer line up.
    _cp_sig = _mw_sig(events, extra="copilot1")
    if st.session_state.get("mw_cp_sig") != _cp_sig:
        st.session_state.pop("mw_cp_props", None)
    # Approve reruns immediately, so its confirmation has to survive the rerun.
    _flash = st.session_state.pop("mw_cp_flash", None)
    if _flash:
        st.success(_flash)
    st.caption(
        "Runs the controller + global optimiser forward from your window — your "
        "transfers stay fixed — and recommends the **next** week plus a few weeks "
        "of **look-ahead**. Harvest + 6N staging come from the validated "
        "controller (pre-ticked); the OG↔OG transfer plan comes from the global "
        "optimiser (ranked, opt-in). Tick what you want on the **next** week, "
        "approve, and it's added as that week's ops — then run again for the week "
        "after. Look-ahead weeks are view-only projections. **~20-30 s per run.**")
    if events:
        _tr = sum(1 for e in events if e.type == "og_transfer")
        _hv = sum(1 for e in events if e.type == "harvest")
        _s6 = sum(1 for e in events if e.type in ("og_to_6n", "graded_harvest"))
        _fw = sum(1 for e in events if e.type == "fw_to_og")
        st.caption(
            f"✓ Building on your **{len(events)} scripted operation(s)** through "
            f"{_mw_iso_week(_n, forecast_start)}: {_tr} transfer · {_hv} harvest · "
            f"{_s6} into 6N · {_fw} FW→OG. Both engines run your full manual window "
            f"first, so every recommendation is computed on top of these.")
    _next_iso = _mw_iso_week(_n + 1, forecast_start)
    if st.button(f"🤖 Recommend from {_next_iso}", key="mw_cp_run", type="primary",
                 disabled=_blocked,
                 help="Disabled while any operation is infeasible — the co-pilot "
                      "saves your window to disk before it runs."
                      if _blocked else None):
        try:
            dump_manual_events(SCENARIO_DIR, events)
        except Exception as e:  # noqa: BLE001
            st.error(f"Couldn't save your events first: {e}")
            return
        _wd = tempfile.mkdtemp(prefix="as_copilot_")   # per-run dir: no cross-session clobber
        tmp = Path(_wd) / "copilot_pr.xlsm"
        tmp.write_bytes(uploaded.getvalue())
        with st.spinner(f"Running the controller + global optimiser from "
                        f"{_next_iso}… (~20-30 s)"):
            try:
                st.session_state["mw_cp_props"] = propose_upcoming(
                    str(tmp), str(CONFIG_DIR), str(SCENARIO_DIR), n_weeks=_LOOKAHEAD)
                # Stamp the working set these proposals were computed against —
                # any later edit (or a new PR) invalidates them on the next render.
                st.session_state["mw_cp_sig"] = _cp_sig
                st.session_state["mw_cp_nonce"] = \
                    st.session_state.get("mw_cp_nonce", 0) + 1
            except Exception as e:  # noqa: BLE001
                st.session_state.pop("mw_cp_props", None)
                st.error(f"Co-pilot failed: {type(e).__name__}: {e}")
                return
            finally:
                shutil.rmtree(_wd, ignore_errors=True)

    props = st.session_state.get("mw_cp_props")
    if not props:
        return
    handoff = props[0]

    # Week picker — browse any upcoming week; approval stays on the handoff week
    # (Sequential model: you approve one contiguous week at a time).
    def _wk_label(i):
        p = props[i]
        return (f"{p.week_label}  ·  week {p.window_week}  ·  "
                f"{'next — approvable' if i == 0 else 'look-ahead (view only)'}")
    sel = st.selectbox("Show recommendations for week", range(len(props)),
                       format_func=_wk_label, key="mw_cp_week_sel")
    prop = props[sel]
    is_handoff = (sel == 0)

    for _w in prop.warnings:
        st.caption(f"⚠ {_w}")
    if prop.is_empty():
        st.info(f"The planners recommend no moves for {prop.week_label}.")
        return

    if is_handoff:
        st.markdown(f"**Recommended for {prop.week_label}** — ticked moves become "
                    f"that week's operations:")
    else:
        st.info(f"👁 **Look-ahead: {prop.week_label}.** "
                f"A projection from the current plan — approve advances one week at "
                f"a time, so approve {handoff.week_label} first. These numbers "
                f"refresh once the "
                f"nearer weeks are scripted.")

    _nonce = st.session_state.get("mw_cp_nonce", 0)
    picks: list = []

    def _row(m, i, label, default):
        # Handoff: a tickable checkbox (approvable). Look-ahead: a read-only line.
        if not is_handoff:
            st.markdown(f"- {label}")
            return
        key = f"mw_cp_pick_{_nonce}_{i}"
        c1, c2 = st.columns([1, 16])
        c1.checkbox("pick", value=default, key=key, label_visibility="collapsed")
        c2.markdown(label)
        picks.append((m, key))

    i = 0
    if prop.harvest_recs:
        st.markdown("**① Harvest — controller (contract / caps)**")
        for m in prop.harvest_recs:
            _row(m, i, f"Harvest **{m.from_loc}** · {m.batch} · "
                       f"{m.count:,.0f} fish @ {m.avg_wt_kg:.2f} kg", True)
            i += 1
    if prop.sixn_recs:
        st.markdown("**② Stage into 6N for harvest — controller**")
        for m in prop.sixn_recs:
            _row(m, i, f"**{m.from_loc} → {m.to_loc}** · {m.batch} · "
                       f"{m.count:,.0f} fish", True)
            i += 1
    for opt in prop.transfer_options:
        mv = sorted(opt.moves, key=lambda x: -x.count)
        st.markdown(f"**③ OG↔OG transfers — {opt.label}** · _{opt.why}_ "
                    f"— {len(mv)} moves (the optimiser's full transition; "
                    f"tick the ones you want)")
        for m in mv:
            _row(m, i, f"**{m.from_loc} → {m.to_loc}** · {m.batch} · "
                       f"{m.count:,.0f} fish · _{m.note}_", False)
            i += 1

    if not is_handoff:
        st.caption(f"Viewing a look-ahead week — switch the picker back to "
                   f"**{handoff.week_label}** to approve.")
        return

    if st.button(f"✓ Approve ticked → add to {prop.week_label}",
                 key="mw_cp_approve", type="primary", disabled=_blocked,
                 help="Disabled while any operation is infeasible."
                      if _blocked else None):
        chosen = [m for m, key in picks if st.session_state.get(key)]
        if not chosen:
            st.warning("Nothing ticked — tick at least one move to approve.")
        else:
            for ev in to_manual_events(chosen, prop.window_week):
                _mw_add(ev)
            try:
                dump_manual_events(SCENARIO_DIR, _mw_events())
            except Exception as e:  # noqa: BLE001
                # Never claim success on a failed write: scenario/ is OneDrive-
                # synced and the dump can lose a lock race (see yaml_atomic).
                # The ops are in the working set, so Save window can retry.
                st.session_state.pop("mw_cp_props", None)
                st.error(f"Added {len(chosen)} operation(s) to the window, but "
                         f"couldn't write scenario/manual_events.yaml: {e}. "
                         f"They are NOT on disk yet — use 💾 Save window to retry.")
                return
            st.session_state.pop("mw_cp_props", None)
            # The window grew, so the copilot signature moved with it; re-stamp
            # it or the next render would drop the (already consumed) proposal.
            st.session_state["mw_cp_sig"] = _mw_sig(_mw_events(), extra="copilot1")
            st.session_state["mw_cp_flash"] = (
                f"Added {len(chosen)} operation(s) to {prop.week_label}. "
                f"Run me again for the next week "
                f"({_mw_iso_week(prop.window_week + 1, forecast_start)}).")
            st.rerun()


def _manual_window_editor(uploaded):
    """Run-mode editor: SEE the projected facility week by week, click a tank to
    act on it in context (harvest / move / 6N / FW→OG), validated against the
    uploaded PR, saved to scenario/manual_events.yaml (which the run reads). The
    flat grid lives on behind an Advanced expander. No Excel sheets involved."""
    from forecast.time_grid import week_start as _week_start
    # The section-toggle widgets (rollup / FW intake / advanced) render BELOW the
    # clickable grid + action panel. When an action-panel button (add harvest/
    # move/6N, close ✕) or a timeline delete calls st.rerun(), the script aborts
    # BEFORE those toggles are re-instantiated that run — and Streamlit drops the
    # state of any keyed widget it didn't render, snapping the toggles back to
    # off (their content vanishes while the switch still looks on). Re-touching
    # the keys here, at the top (which always runs), keeps their state across
    # such a rerun. (Verified against a headless Streamlit repro.)
    for _tk in ("mw_rollup_toggle", "mw_fw_toggle", "mw_adv_toggle",
                "mw_copilot_toggle"):
        if _tk in st.session_state:
            st.session_state[_tk] = st.session_state[_tk]
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
            _vmode = st.radio(
                "Show tank state at", ["Week open", "Week close"],
                horizontal=True, key="mw_view_at",
                help="Week open = start-of-week, before that week's growth AND "
                     "before your scripted events run — what's in the tank when "
                     "you click to act on it. Week close = end-of-week, after "
                     "growth and after your events run — so you can see what "
                     "holds fish and what's empty at week's end (a tank you "
                     "harvest or move shows empty here, at the week you act).")
            _view = "close" if _vmode.startswith("Week close") else "open"
            rows, labels, moves = _mw_project(
                state, ctx, events, n_weeks, view=_view)
            date_for = {lbl: _week_start(i, ctx["forecast_start"])
                        for i, lbl in enumerate(labels)}

            _cmode = st.radio(
                "Colour cells by", ["Fill (density)", "Batch"], horizontal=True,
                key="mw_color_by",
                help="Fill = how full each tank is vs its cap (green→red). Batch = "
                     "a distinct colour per batch, to see which tanks hold which "
                     "fish and how a batch moves across the weeks.")
            _cb = "batch" if _cmode.startswith("Batch") else "fill"
            _when = (
                "**week-open** (start of the week, before that week's growth and "
                "before your scripted events) — what's in the tank when you act"
                if _view == "open" else
                "**week-close** (end of the week, after growth and after your "
                "scripted events) — what holds fish and what's empty at week's "
                "end; a tank you harvest or move shows empty here")
            st.caption(
                ("**Each batch has its own background colour** (grey = empty). "
                 if _cb == "batch" else
                 "**Background = how full each tank is vs its cap** — grey empty, "
                 "green roomy, amber near cap, red over — and the **batch id is bold "
                 "in its own colour** so you can follow a cohort across tanks. ")
                + "Columns are weeks, rows are tanks (⛔6N = depuration), and each cell "
                  "shows **batch · avg weight · density** at " + _when + ". "
                  "A **move lights up both ends in its week**: the tank that holds "
                  "the fish in this view is solid with an arrow (**⇢** leaving / "
                  "**⇠** arrived), and the counterpart tank shows a faint **ghost "
                  "arrow** naming where the fish went / came from. "
                  "**Click a tank's cell at the week you want** to act on it. "
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
                state, rows, labels, color_by=_cb, batch_filter=_bf, moves=moves)
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
                # Recommendations — what's most out of bounds this window and the
                # relief action, ranked worst-first. Reads the current projection,
                # so it updates as you script events. ▶ jumps to that tank.
                _collapsed, _breach_weeks, _by_week = _mw_recommendations(
                    state, rows, labels, ctx, view=_view)
                with st.container(border=True):
                    st.markdown("**⚠ Most out of bounds — recommended actions**")
                    _proj_err = _mw_proj_error()
                    if _proj_err:
                        # The projection failed, so `rows` is empty and NOTHING can
                        # look out of bounds. Never let that read as an all-clear.
                        st.error(
                            f"Projection failed — **limits could not be checked**. "
                            f"Treat this as unknown, not as within-limits.\n\n"
                            f"`{_proj_err}`"
                        )
                    elif not _breach_weeks:
                        st.caption("✓ Every tank, system and the facility are "
                                   "within limits across this window.")
                    else:
                        # Week picker: 'All weeks' collapses each distinct breach to
                        # its worst week; a specific week shows everything wrong then.
                        _opts = ["All weeks (worst first)", *_breach_weeks]
                        if st.session_state.get("mw_rec_week") not in _opts:
                            st.session_state["mw_rec_week"] = _opts[0]
                        _sel = st.selectbox(
                            "Show recommendations for week", _opts, key="mw_rec_week",
                            help="'All weeks' lists each distinct breach at its worst "
                                 "week; pick a week to see everything out of bounds then.")
                        _recs = (_collapsed if _sel.startswith("All weeks")
                                 else _by_week.get(_sel, []))
                        st.caption("Ranked by how far over cap. **▶** jumps to the "
                                   "tank so you can act on it.")
                        for _i, _rc in enumerate(_recs[:6]):
                            _sev = "🔴" if _rc["frac"] >= 1.15 else "🟠"
                            _ca, _cb = st.columns([9, 1])
                            _ca.markdown(
                                f"{_sev} **{_rc['week']}** · {_rc['msg']}  \n"
                                f"↳ {_rc['action']}")
                            if (_rc["tank_id"] is not None
                                    and _rc["week"] in labels
                                    and _cb.button("▶", key=f"mw_rec_{_i}",
                                                   help="Select this tank")):
                                st.session_state["mw_sel"] = (
                                    _rc["tank_id"], _rc["week"],
                                    labels.index(_rc["week"]) + 1)
                                st.rerun()
                        if len(_recs) > 6:
                            st.caption(f"… and {len(_recs) - 6} more breach(es).")

                sel = st.session_state.get("mw_sel")
                if sel and sel[1] in labels:
                    with st.container(border=True):
                        _mw_action_panel(state, ctx, rows, labels, sel, date_for)
                else:
                    st.info("👆 Click a tank's cell in the grid to harvest it, move / "
                            "split it, or send it to 6N — the options appear here.")

            _roll_when = "open" if _view == "open" else "close"
            if st.toggle(f"📊 System rollup — {_roll_when} biomass + feed/day "
                         f"per week", key="mw_rollup_toggle"):
                _mw_system_rollup(state, rows, labels, ctx["tables"],
                                  ctx["batch_by_id"], ctx=ctx, view=_view)

            if st.toggle("🐟 FW→OG intake — bring a freshwater cohort into OG",
                         key="mw_fw_toggle"):
                with st.container(border=True):
                    _mw_fw_intake(state, ctx, rows, labels, date_for)

            if st.toggle("🤖 Co-pilot — let the forecast propose the next week",
                         key="mw_copilot_toggle"):
                with st.container(border=True):
                    _mw_copilot(uploaded, events,
                                ctx.get("forecast_start") if ctx else None, bad)

            st.divider()
            _mw_timeline(state, events, bad, ctx.get("forecast_start") if ctx else None)

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
    _hidden = ({k[0] for k in fl_cur} | {k[0] for k in sl_cur}) - set(weeks)
    if _hidden:
        st.caption(
            f"ℹ️ {len(_hidden)} week(s) in `limits.yaml` fall outside the current "
            f"forecast horizon and are not shown here "
            f"({min(_hidden)} … {max(_hidden)}). They are **kept** on save, not "
            f"deleted — edit them by loading a PR whose horizon covers them."
        )
    b1, b2, _ = st.columns([1, 1, 3])
    if b1.button("💾 Save Limits", key="save_lim"):
        try:
            # Save REPLACES limits.yaml wholesale, but this grid only shows the
            # current forecast horizon (_limit_week_cols). Any week stored in the
            # file OUTSIDE that horizon has no column here, so rebuilding purely
            # from the grid would silently DELETE it — e.g. every earlier week
            # after uploading a PR that starts later. Carry those through
            # untouched; the operator never saw them and cannot have edited them.
            fl_recs = _preserved_facility_limits(fl_cur, weeks)
            sl_recs = _preserved_system_limits(sl_cur, weeks)
            fl_recs += [{"week": wk, "metric": r["metric"], "value": float(r[wk])}
                        for r in _records(fdf) for wk in weeks
                        if r.get(wk) not in (None, "")]
            sl_recs += [{"week": wk, "system": r["system"], "metric": r["metric"],
                         "value": float(r[wk])}
                        for r in _records(sdf) for wk in weeks
                        if r.get(wk) not in (None, "")]
            fl_recs.sort(key=lambda r: (r["week"], r["metric"]))
            sl_recs.sort(key=lambda r: (r["week"], r["system"], r["metric"]))
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


def _sweep_inputs_sig() -> str:
    """Identity of the inputs a sweep ran against (PR content + config/scenario
    state) — stored beside Tune/Optimize/Frontier results so a recommendation
    computed on different inputs is flagged instead of presented as current."""
    import hashlib
    return hashlib.md5(
        f"{st.session_state.get('_pr_key', '')}|{_config_fingerprint()}"
        .encode()).hexdigest()


def _warn_if_sweep_stale(sig_key: str, what: str) -> None:
    """Compare a stored sweep's input signature to the live inputs and warn —
    the Compare board has this staleness check; the sweeps were missing it, so
    a knob set validated on another PR/config could be saved as if current."""
    stored = st.session_state.get(sig_key)
    if stored is not None and stored != _sweep_inputs_sig():
        st.warning(f"⚠ These {what} were computed on a **different PR or "
                   f"config** than what's loaded now — re-run before trusting "
                   f"or saving anything from them.")


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
    # "Computer power" — how much of this machine the heavy runs may use. Stored
    # as a PERCENT of logical CPUs (operator-friendly); _cpu_workers() translates
    # it into CP-SAT search threads / sweep worker processes at the call sites.
    st.slider(
        "Computer power", min_value=10, max_value=100, value=40, step=10,
        format="%d%%", key="cpu_pct",
        help="How much of this computer the heavy runs may use — the Global "
             "optimal (CP-SAT) solver (also inside Compare & Choose) and the "
             "Optimize sweeps. Higher = faster runs, but other applications "
             "may feel slower (and Optimize sweeps use more memory) while a "
             "run is going. A plain controller Run forecast and Tune are "
             "sequential and unaffected.",
    )
    st.caption(f"Heavy runs may use up to **{_cpu_workers()}** of "
               f"{os.cpu_count() or 2} processor cores.")
    st.divider()
    # If ▶ Run forecast was clicked from another mode, jump to Run forecast HERE —
    # before the radio is instantiated, so setting its session_state value is allowed
    # (Streamlit forbids mutating a widget's key after it renders). The pending run is
    # then honored by the run handler below.
    if st.session_state.pop("_goto_run_mode", False):
        st.session_state["app_mode"] = "Run forecast"
    app_mode = st.radio(
        "Mode",
        ["Run forecast", "Configure (models & control)", "Tune (density knobs)",
         "Optimize (multi-objective)", "Compare & Choose (all methods)"],
        help="Run forecast: upload a PR and run. Configure: edit the app's "
             "biology models, facility, control, batches, and limits. "
             "Tune: sweep the controller knobs and read the per-batch "
             "density distribution. Optimize: sweep knobs and rank variants on a "
             "selectable objective (walk the line + minimize feed/handling). "
             "Compare & Choose: run all planning methods, grade them on several "
             "lenses, and pick which plan becomes the report.",
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
            "- **Compare & Choose (all methods)** — runs the *different engines* "
            "(Controller, Global heuristic, Global optimal CP-SAT) on one PR, grades "
            "them on several lenses (fewest moves, steadiest harvest, between/within-"
            "system balance, density, footprint) with hard-rule badges, and lets you "
            "pick which whole plan becomes the report. Unlike Tune/Optimize (same "
            "engine, different knobs), this compares *engines*.\n"
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
    # The planning method is chosen ONCE, on the Compare & Choose board, where
    # you can see every method graded side by side. Run forecast just re-runs
    # whichever plan you picked — no second, blind choice on the main screen.
    _chosen = st.session_state.get("_chosen_method", _DEFAULT_METHOD)
    _chosen_m = _METHODS.get(_chosen) or _METHODS[_DEFAULT_METHOD]
    st.caption(f"Method: **{_chosen_m.label}**")
    if _chosen == _DEFAULT_METHOD:
        st.caption("The default. It is the only method measured to harvest "
                   "something every single week — the plain Controller has an "
                   "empty week on 5 of 6 real PRs. Compare & Choose runs every "
                   "method and lets you pick a different one.")
    else:
        st.caption("Picked on the Compare & Choose board. Pick another there, "
                   "or re-select the hybrid to go back to the default.")
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

class _TeeIO(io.StringIO):
    """Captures stdout AND forwards each completed line to `on_line`.

    The pipeline narrates its stages to stdout; capturing it into a plain
    StringIO meant the operator saw nothing until the run returned — a static
    spinner for anything up to half an hour. This tees the stream so the UI can
    show the current stage live, while `getvalue()` stays byte-identical to the
    old capture (super().write runs first and unconditionally, whatever the
    callback does). A callback that raises disables itself rather than
    spamming; callbacks must only touch `st` APIs — printing from one would
    re-enter this writer under redirect_stdout and pollute the transcript.
    """

    def __init__(self, on_line=None):
        super().__init__()
        self._on_line = on_line
        self._buf = ""

    def write(self, s: str) -> int:
        n = super().write(s)            # transcript first: never lose output
        if self._on_line and s:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    self._on_line(line)
                except Exception:  # noqa: BLE001 — narration is best-effort
                    self._on_line = None
                    break
        return n


def _run_with_workbook_bytes(
    input_bytes: bytes,
    input_name: str,
    config_dir: str | None = None,
    scenario_dir: str | None = None,
    method: str = "controller",
    cpsat_time: float = 300.0,
    cpsat_workers: int | None = None,
    cpsat_det_time: float | None = None,
    on_line=None,
) -> dict:
    """Run the pipeline against `input_bytes` in a temp directory.

    `cpsat_workers` = CP-SAT search threads for the global-optimal method
    (None -> the engine default of 8); callers pass _cpu_workers() so the
    sidebar's "Computer power" percent governs it.

    `on_line` receives each stage line the pipeline prints, as it prints it, so
    the caller can narrate progress. The captured stdout is unaffected.

    When config_dir/scenario_dir are given (PR-only mode), the stable
    config + scenario load from YAML and the uploaded workbook supplies
    only the ProductionReport. Returns a dict with metrics + the output
    workbook bytes + parsed data needed for visualization.
    """
    work_dir = Path(tempfile.mkdtemp(prefix="as_forecast_"))
    in_path = work_dir / input_name
    _m = (_AS_CONFIGURED if method == "as-configured"
          else _METHODS.get(method) or _METHODS["controller"])
    _is_global_engine = (_m.engine == "global")
    _is_optimal = bool(_m.engine_kwargs.get("optimal"))
    # The global method emits a fresh .xlsx (no VBA to carry); the controller
    # keeps the uploaded macro workbook's suffix.
    if _is_global_engine and _is_optimal:
        out_name = Path(input_name).stem + "_planned_OPTIMAL.xlsx"
    elif _is_global_engine:
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

    # A method's control-knob overrides (LNS placement, hybrid follow mode, ...)
    # are applied via a throwaway config COPY so the user's control.yaml is
    # never touched — same contract as forecast.methods.run_method.
    run_config_dir = config_dir
    if _m.overrides and config_dir:
        import shutil
        import yaml as _yaml
        _tmpcfg = work_dir / f"config_{_m.key.replace('-', '_')}"
        shutil.copytree(config_dir, _tmpcfg)
        _cy = _tmpcfg / "control.yaml"
        _d = _yaml.safe_load(_cy.read_text()) or {}
        _d.update(_m.overrides)
        _cy.write_text(_yaml.safe_dump(_d, sort_keys=False))
        run_config_dir = str(_tmpcfg)

    # Run the pipeline, capturing console output for display.
    t0 = time.time()
    captured = _TeeIO(on_line)
    try:
        with redirect_stdout(captured):
            if _is_global_engine:
                from tools.run_global_forecast import run_global
                rc = run_global(in_path, out_path, run_config_dir, scenario_dir,
                                optimal=_is_optimal,
                                cpsat_time=cpsat_time,
                                cpsat_workers=(cpsat_workers or 8),
                                cpsat_det_time=(cpsat_det_time or 30.0))
            else:
                rc = run_pipeline(input_path=in_path, output_path=out_path,
                                  config_dir=run_config_dir, scenario_dir=scenario_dir)
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
    # Capture the EFFECTIVE config this run used, so the result can always show
    # what produced it. MUST read run_config_dir, not config_dir: with method
    # overrides it is the throwaway copy they were applied to, while config_dir
    # still holds the user's un-overridden values. Reading config_dir made an LNS
    # run report placement_method=greedy, and would now report the pinned
    # controller arms as hybrid_follow=full (the base config's value) — i.e. the
    # panel would describe a different plan than the one on screen. It falls back
    # to config_dir automatically: run_config_dir IS config_dir when no overrides.
    config_used = {}
    if run_config_dir:
        try:
            from forecast.config_io import load_control, control_to_dict
            config_used = control_to_dict(load_control(run_config_dir))
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
        # Identity of this run, so the results view can memoize its derived
        # frames and only rebuild them when a DIFFERENT run is displayed.
        "_rid": uuid.uuid4().hex,
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
            hog_avg_kg = row[7] if len(row) > 7 and isinstance(row[7], (int, float)) else None
            if isinstance(cnt, (int, float)) and isinstance(gross_kg, (int, float)):
                harvest_count += cnt
                harvest_kg += gross_kg
                harvest_events.append({
                    "Week": row[0], "Batch": row[1], "Tank": row[2],
                    "Count": cnt, "Gross_kg": gross_kg,
                    "Avg_wt_kg": gross_avg_kg,
                    "HOG_kg": hog_kg if hog_kg is not None else 0.0,
                    "HOG_avg_kg": hog_avg_kg,
                })

    # FACILITY-WIDE weekly biomass + feed, straight from the Advisory sheet.
    # This is the FW-INCLUSIVE basis the engine's cap actually governs (audit
    # H2/M3): FW/EGG fish are real facility biomass but live in FW tanks, so
    # they never appear in BatchLocations. Charts that summed tank rows and
    # drew the facility cap line beside them understated the load by the FW
    # share — which peaks near 7% of cap, so a chart reading 98% was really
    # ~105%. Read the number the cap is enforced against instead.
    facility_weekly = []
    if "Advisory" in wb.sheetnames:
        ws = wb["Advisory"]
        hdr = None
        for r in ws.iter_rows(values_only=True):
            if hdr is None:
                if r and r[0] == "Week" and any(
                        str(c).startswith("Total_Biomass") for c in r if c):
                    hdr = {str(c): i for i, c in enumerate(r) if c}
                continue
            if not r or not r[0] or not str(r[0]).startswith("20"):
                continue

            def _num(key, _row=r, _h=hdr):
                k = next((c for c in _h if c.startswith(key)), None)
                v = _row[_h[k]] if k is not None else None
                return float(v) if isinstance(v, (int, float)) else None

            facility_weekly.append({
                "Week": str(r[0]),
                "Total_Biomass_kg": _num("Total_Biomass"),
                "Biomass_Limit_kg": _num("Biomass_Limit"),
                "Total_Feed_kg_day": _num("Total_Feed"),
                "Feed_Limit_kg_day": _num("Feed_Limit"),
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

    from forecast.optimize import _density_quality, WELFARE_DENSITY_KG_M3
    _wl = WELFARE_DENSITY_KG_M3
    try:                                    # operator's welfare line from Configure
        from forecast.config_io import load_control
        _wl = float(load_control(CONFIG_DIR).density_welfare_threshold_kg_m3)
    except Exception:  # noqa: BLE001
        pass
    _q_mean, _q_fw, _q_frac = _density_quality(wb, _wl)
    return {
        "violations": len(violations),
        "worst_density": max(violations, default=0.0),
        "growout_density_cap": growout_cap,
        "mean_rearing_density": _q_mean,
        "crowded_biomass_fraction": _q_frac,
        "crowded_fish_weeks": _q_fw,
        "welfare_density": _wl,
        "system_biomass_cap": sys_cap_biomass,
        "harvest_kg": harvest_kg,
        "harvest_count": harvest_count,
        "batch_locations": bl_rows,
        "harvest_events": harvest_events,
        "biology_projection": bio_rows,
        "advisory_summary": advisory_summary,
        "advisory_entries": advisory_entries,
        "facility_weekly": facility_weekly,
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


def _stocking_frontier_section():
    """Stocking-for-quality frontier: sweep a stocking CUT across FUTURE batches
    and plot the quality-vs-volume trade — the lever that works when the density
    knobs can't (a tank-full facility). Engine = forecast.stocking_frontier."""
    import plotly.graph_objects as pgo
    st.divider()
    st.subheader("🌿 Stocking-for-quality frontier")
    st.caption(
        "When the facility is **tank-full** the density knobs above can't lower "
        "density — the real quality lever is stocking **fewer fish**. This sweeps a "
        "stocking cut across your **future** batches (fish already in the facility "
        "are fixed) and shows the trade: fewer fish rear **gentler** (lower "
        "experienced density) but yield **less harvest**. Each point runs the full "
        "forecast (~20s); your config/scenario/PR are never touched.")
    _fd = st.radio("Frontier depth", ["Quick (0, 10%)",
                                      "Full (0, 5, 10, 15, 20%)"],
                   horizontal=True, key="frontier_depth")
    reductions = ((0.0, 0.10) if _fd.startswith("Quick")
                  else (0.0, 0.05, 0.10, 0.15, 0.20))
    if st.button(f"▶ Run stocking frontier ({len(reductions)} runs, "
                 f"~{len(reductions) * 20 // 60 + 1} min)", key="frontier_go"):
        from forecast.config_io import load_control
        from forecast.stocking_frontier import stocking_frontier
        _wl = 80.0
        try:
            _wl = float(load_control(CONFIG_DIR).density_welfare_threshold_kg_m3)
        except Exception:  # noqa: BLE001
            pass
        import shutil
        _wd = tempfile.mkdtemp(prefix="as_frontier_")   # per-run dir: no cross-session clobber
        _tmp = Path(_wd) / "frontier_pr.xlsm"
        _tmp.write_bytes(uploaded.getvalue())
        try:
            with st.spinner(f"Running {len(reductions)} forecasts…"):
                st.session_state["frontier_pts"] = stocking_frontier(
                    str(_tmp), str(CONFIG_DIR), str(SCENARIO_DIR),
                    reductions=reductions, welfare_density=_wl)
                st.session_state["_frontier_sig"] = _sweep_inputs_sig()
        finally:
            shutil.rmtree(_wd, ignore_errors=True)

    pts = st.session_state.get("frontier_pts")
    if not pts:
        return
    _warn_if_sweep_stale("_frontier_sig", "frontier points")
    for p in pts:
        if p.error:
            st.caption(f"⚠ {p.reduction * 100:.0f}% cut failed: {p.error}")
    ok = [p for p in pts if not p.error and p.conserves]
    if not ok:
        st.warning("No valid frontier points (all failed or broke conservation).")
        return
    df = pd.DataFrame([{
        "Stocking cut": f"{p.reduction * 100:.0f}%",
        "Future batches cut": p.scaled_batches,
        "Harvest (t)": round(p.harvest_t),
        "Reared density (kg/m³)": round(p.mean_rearing_density, 1),
        "% crowded": round(p.crowded_biomass_fraction * 100, 1),
        "Worst density": round(p.worst_density),
    } for p in ok])
    st.dataframe(df, hide_index=True, use_container_width=True)
    fig = pgo.Figure()
    fig.add_trace(pgo.Scatter(
        x=[p.harvest_t for p in ok], y=[p.mean_rearing_density for p in ok],
        mode="lines+markers+text",
        text=[f"{p.reduction * 100:.0f}%" for p in ok], textposition="top center",
        line=dict(color="#2e7d32")))
    fig.update_layout(
        height=380, title="Quality vs volume — each point is a stocking cut",
        xaxis_title="Harvest volume (t) — more is better →",
        yaxis_title="Reared density (kg/m³) — lower is gentler ↓")
    st.plotly_chart(fig, use_container_width=True)
    base = next((p for p in ok if p.reduction == 0), ok[0])
    for p in ok:
        if p.reduction > 0:
            dq = base.mean_rearing_density - p.mean_rearing_density
            dv = base.harvest_t - p.harvest_t
            st.caption(
                f"**{p.reduction * 100:.0f}% fewer future fish** → reared "
                f"**{dq:+.1f} kg/m³ gentler** "
                f"({base.crowded_biomass_fraction * 100:.0f}% → "
                f"{p.crowded_biomass_fraction * 100:.0f}% crowded), for "
                f"**{dv:,.0f} t less harvest**.")


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
        st.session_state["_tune_sig"] = _sweep_inputs_sig()

    results = st.session_state.get("_tune_results")
    if not results:
        _stocking_frontier_section()   # available without running the knob sweep
        return
    _warn_if_sweep_stale("_tune_sig", "tuning results")

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
                # The save itself moved the config fingerprint — refresh the
                # sweep's input sig so only EXTERNAL changes flag it stale.
                st.session_state["_tune_sig"] = _sweep_inputs_sig()
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

    # The density KNOBS above can't lower density on a tank-full facility; the
    # stocking-cut frontier is the lever that can. Always available (own button).
    _stocking_frontier_section()


# ============================================================
# Configure / Tune modes — render and stop
# ============================================================

def _opt_winner(results, rec):
    """The variant the recommendation actually chose.

    Resolve by OVERRIDES, not by label: coordinate_descent names each candidate
    for the single knob it changed that step, so the same label recurs across
    rounds carrying different accumulated overrides. A by-label lookup returns
    the earliest match — a round-1 partial set — which then gets run and (with
    auto-save on) written to control.yaml in place of the winning combination.
    Falls back to the old behaviour for a recommendation without overrides.
    """
    ov = dict(getattr(rec, "overrides", None) or {})
    for v in results:                      # exact winner: label AND knobs
        if v.label == rec.best_label and dict(v.overrides) == ov:
            return v
    for v in results:                      # knobs alone still identify it
        if dict(v.overrides) == ov:
            return v
    return next((v for v in results if v.label == rec.best_label), results[0])


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


def _rv_memo(name: str, rid: str, build):
    """Per-run memo for the results view.

    st.tabs renders ALL tab bodies on every rerun, so without this every widget
    tick anywhere in the app (including the manual-window editor above the
    results) rebuilt the pivots, groupbys and derived tables and re-opened the
    output workbook from disk. Holds exactly one run's artifacts: a different
    `rid` clears the lot, so stale data can't survive a new run.

    Values are returned BY REFERENCE — callers must .copy() before mutating,
    which the tab code already does throughout.
    """
    cache = st.session_state.setdefault("_rv_cache", {})
    if cache.get("_rid") != rid:
        cache.clear()
        cache["_rid"] = rid
    if name not in cache:
        cache[name] = build()
    return cache[name]


def _system_feed_audit(out_path):
    """REALIZED per-system feed rows + PER-SYSTEM caps, from the output
    workbook's SystemLimitsAudit sheet.

    Returns (rows, {system: cap_kg_day}). The cap used to be a single scalar
    overwritten on every row, so it ended up holding whatever the LAST audit row
    happened to say — one cap line drawn for every system, wrong for all but one
    of them whenever the per-system caps differ (which is the normal case: OG1/2
    and OG3-6 have very different feed capacity).

    Returns ([], {}) when the file is gone: the output lives in a temp dir that
    the OS can sweep mid-session, and this read previously took the whole
    results view down with it."""
    from pathlib import Path as _P
    if not out_path or not _P(out_path).exists():
        return [], {}
    rows, caps = [], {}
    try:
        _wb = load_workbook(out_path, data_only=True, read_only=True)
        try:
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
                    rows.append({"System": str(_sys),
                                 "Week": str(_row[_hdr.get("Week", 0)]),
                                 "Feed_kg_day": float(_fd)})
                    if _fc:
                        caps[str(_sys)] = float(_fc)
        finally:
            _wb.close()   # was outside any guard: an exception mid-scan leaked it
    except Exception:  # noqa: BLE001 — an unreadable audit just hides the chart
        return [], {}
    return rows, caps


def _daily_harvest_table(he_df):
    """Per-day (Mon–Fri) breakout of the WEEK's total harvest for the Harvest tab.

    All tanks harvesting in the same ISO week are COMBINED into one block: their
    count + biomass are summed and split evenly across the five operating days,
    with blended average weights (total biomass ÷ total fish), a **Total** row,
    and a blank row before the next week. The Tank/Batch columns list every tank
    and batch that contributed. Returns (DataFrame, {total-row positions},
    {blank-row positions}) so the caller can shade the totals."""
    import datetime as _dt
    cols = ["Week", "Date", "Tank", "Batch", "Count", "Live kg",
            "Avg live (kg)", "HOG kg", "Avg HOG (kg)"]
    rows: list[dict] = []
    total_pos, blank_pos = set(), set()
    if he_df.empty or "Week" not in he_df.columns:
        return pd.DataFrame(rows, columns=cols), total_pos, blank_pos
    df = he_df.copy()
    df["Week"] = df["Week"].astype(str)

    def _num(s):
        return float(pd.to_numeric(s, errors="coerce").fillna(0).sum())

    for wk in sorted(df["Week"].unique()):
        sub = df[df["Week"] == wk]
        try:
            y, w = int(wk[:4]), int(wk[6:8])
            days = [_dt.date.fromisocalendar(y, w, 1) + _dt.timedelta(days=i)
                    for i in range(5)]
        except Exception:  # noqa: BLE001 — a non-week label just gets skipped
            continue
        cnt = _num(sub["Count"])
        gross = _num(sub["Gross_kg"]) if "Gross_kg" in sub else 0.0
        hog = _num(sub["HOG_kg"]) if "HOG_kg" in sub else 0.0
        live_avg = gross / cnt if cnt else 0.0     # blended live kg/fish
        hog_avg = hog / cnt if cnt else 0.0         # blended HOG kg/fish
        tanks = (", ".join(str(int(t)) if float(t).is_integer() else str(t)
                           for t in sorted(sub["Tank"].dropna().unique()))
                 if "Tank" in sub else "")
        bats = (", ".join(sorted(sub["Batch"].dropna().astype(str).unique()))
                if "Batch" in sub else "")
        n = len(days)
        for d in days:
            rows.append({
                "Week": wk, "Date": d.strftime("%Y-%m-%d"),
                "Tank": tanks, "Batch": bats,
                "Count": f"{round(cnt / n):,}", "Live kg": f"{round(gross / n):,}",
                "Avg live (kg)": f"{live_avg:.2f}",
                "HOG kg": f"{round(hog / n):,}", "Avg HOG (kg)": f"{hog_avg:.2f}"})
        total_pos.add(len(rows))
        rows.append({
            "Week": wk, "Date": "Total", "Tank": tanks, "Batch": bats,
            "Count": f"{round(cnt):,}", "Live kg": f"{round(gross):,}",
            "Avg live (kg)": f"{live_avg:.2f}", "HOG kg": f"{round(hog):,}",
            "Avg HOG (kg)": f"{hog_avg:.2f}"})
        blank_pos.add(len(rows))
        rows.append({c: "" for c in cols})
    return pd.DataFrame(rows, columns=cols), total_pos, blank_pos


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
    # WHOLE-FACILITY biomass (OG + 6N + freshwater) — the basis the cap is
    # enforced on. Summing tank rows omits the FW phase entirely (~7% of cap at
    # peak), which drew a line the plan appeared to sit comfortably under while
    # the engine was at or over it.
    _fw = pd.DataFrame(r.get("facility_weekly") or [])
    if not _fw.empty and _fw["Total_Biomass_kg"].notna().any():
        _fw = _fw.dropna(subset=["Total_Biomass_kg"]).sort_values("Week")
        _fw["Biomass_t"] = _fw["Total_Biomass_kg"] / 1000.0
        fig2 = px.line(_fw, x="Week", y="Biomass_t",
                       title="Facility biomass per week (t) — whole facility, "
                             "incl. freshwater")
        _lim = _fw["Biomass_Limit_kg"]
        if _lim.dropna().nunique() > 1:
            # The cap is a per-week limit (FacilityLimits overrides) — a single
            # hline at week 1's value misreads any ramp across the horizon.
            fig2.add_scatter(x=_fw["Week"], y=_lim / 1000.0, mode="lines",
                             line={"dash": "dot", "color": "red"},
                             name="cap (per-week)")
        else:
            _cap_t = (float(_lim.dropna().iloc[0]) if _lim.notna().any() else
                      float((r.get("config_used") or {}).get("max_biomass_kg")
                            or 3_800_000)) / 1000.0
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

    # Optimize tunes the controller-family pipeline (the live config's engine).
    # If the plan picked on the Compare board is a GLOBAL engine, knobs found
    # here were never measured on it — say so instead of letting a save look
    # like it was validated for the chosen plan.
    _ch = st.session_state.get("_chosen_method", _DEFAULT_METHOD)
    _chm = _METHODS.get(_ch)
    if _chm is not None and _chm.engine == "global":
        st.warning(
            f"Your picked plan is **{_chm.label}** (a Global engine). Optimize "
            f"sweeps and validates the **controller-family** engine, so a "
            f"recommendation saved here was not measured on your picked plan — "
            f"re-run **Compare & Choose** after saving to see its effect there.")

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
                    # This is the SEARCH method (grid/deep), not a planning
                    # engine — label it so it can't be misread as one.
                    "Search": h.get("method", ""),
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
                    emphasis=emphasis, weights=_w, progress=_prog,
                    max_workers=_cpu_workers())
            elif deep:
                results = optimize.coordinate_descent(
                    str(in_path), str(CONFIG_DIR), str(SCENARIO_DIR),
                    emphasis=emphasis, weights=_w, progress=_prog,
                    max_workers=_cpu_workers())
            else:
                results = optimize.sweep(
                    str(in_path), str(CONFIG_DIR), str(SCENARIO_DIR), grid=grid,
                    progress=_prog, max_workers=_cpu_workers())
        except Exception as e:  # noqa: BLE001
            bar.empty()
            st.error(f"Optimization failed: {e}")
            st.code(traceback.format_exc())
            return
        bar.progress(1.0, text="Done")
        st.session_state["_opt_results"] = results
        st.session_state["_opt_sig"] = _sweep_inputs_sig()
        if _auto_opt:
            # AUTO: pick the validated best, run the FULL forecast with it, load it
            # into the viz tabs, and (optionally) persist the winning knobs.
            _rec0 = optimize.recommend(results, emphasis=emphasis, weights=_w)
            _best0 = _opt_winner(results, _rec0)
            _knobs = optimize.overrides_yaml(_best0.overrides).replace("\n", " · ") or "baseline"
            with st.spinner(f"Auto-optimize — running the full forecast with {_knobs} …"):
                try:
                    _tmpcfg = optimize.config_dir_with_overrides(str(CONFIG_DIR), _best0.overrides)
                    # "as-configured": the SAME engine the sweep measured (the
                    # live config + winning knobs, no method pins) — the default
                    # "controller" method pins hybrid_follow off and would load
                    # a different engine's plan into the tabs than was scored.
                    _res = _run_with_workbook_bytes(
                        uploaded.getvalue(), uploaded.name,
                        config_dir=_tmpcfg, scenario_dir=str(SCENARIO_DIR),
                        method="as-configured")
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
                    # The save moved the config fingerprint — refresh the sig
                    # so only EXTERNAL changes flag the results stale.
                    st.session_state["_opt_sig"] = _sweep_inputs_sig()
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
    _warn_if_sweep_stale("_opt_sig", "optimization results")

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
    best = _opt_winner(results, rec)
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
                # The save moved the config fingerprint — refresh the sig so
                # only EXTERNAL changes flag the results stale.
                st.session_state["_opt_sig"] = _sweep_inputs_sig()
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
                    # "as-configured" — match the sweep's engine exactly (see
                    # the auto-optimize call above for why).
                    result = _run_with_workbook_bytes(
                        uploaded.getvalue(), uploaded.name,
                        config_dir=tmpcfg, scenario_dir=str(SCENARIO_DIR),
                        method="as-configured")
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
                            # Bind these metrics to the run they describe. Without
                            # it the panel survives a later ordinary Run-forecast,
                            # pairing THESE numbers with THAT workbook and still
                            # offering it as "Forecast_optimized.xlsm".
                            "rid": _result_rid(result),
                        }
                    else:
                        st.error(f"Run failed: {result.get('error', 'unknown')}")
                except Exception as e:  # noqa: BLE001
                    st.error(f"Run failed: {e}")
                    st.code(traceback.format_exc())
        run_out = st.session_state.get("_opt_run")
        _cur = st.session_state.get("result") or {}
        # Only render while the loaded result IS the run these metrics came from.
        # A later plain Run-forecast replaces `result` but leaves `_opt_run`
        # behind, and the panel would then describe a workbook it never measured.
        if run_out and _cur.get("ok") and run_out.get("rid") == _result_rid(_cur):
            r = st.session_state.result
            ok = run_out["dropped"] == 0 and run_out["overprod"] == 0
            cc1, cc2, cc3 = st.columns(3)
            cc1.metric("Conservation", "PASS ✓" if ok else "FAIL ✗",
                       help=f"{run_out['dropped']} dropped / {run_out['overprod']} over-produced")
            cc2.metric("Harvest CV", f"{run_out['cv']:.3f}",
                       help="How lumpy the weekly harvest is (coefficient of "
                            "variation). Lower = steadier week-to-week; 0 = a "
                            "perfectly flat harvest.")
            cc3.metric("Weeks over 55k", run_out["over"],
                       help="Number of weeks whose harvest exceeds the 55,000-fish "
                            "processing ceiling — spikes your plant has to absorb.")
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

    best = _opt_winner(results, rec)
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


# ============================================================
# Compare & Choose board — run the methods, grade, pick the plan
# ============================================================

# Grading lenses: (label, getter(res)->value [lower is better], one-line blurb).
_BOARD_LENSES = [
    ("Fewest fish moves", lambda r: r["_score"]["metrics"].transfers_per_fish,
     "least handling / stress"),
    ("Steadiest harvest", lambda r: r["_score"]["metrics"].harvest_var,
     "flattest weekly harvest"),
    ("Most balanced across systems",
     lambda r: r["_score"]["metrics"].between_system.get("bio_cv_mean"),
     "even load system-to-system"),
    ("Most even within systems",
     lambda r: r["_score"]["metrics"].within_system.get("bio_cv_mean"),
     "even load tank-to-tank"),
    ("Tightest density", lambda r: r["_score"]["metrics"].density_peak,
     "most density headroom"),
    ("Best welfare / product quality",
     lambda r: r["_score"]["metrics"].crowded_biomass_fraction,
     "least product reared above the welfare density line"),
    ("Smallest tank footprint", lambda r: r["_score"]["metrics"].tank_footprint_mean,
     "fewest grow-out tanks used"),
    ("Fastest run", lambda r: r.get("elapsed"), "least wall time"),
]


def _board_score(out_path):
    """Metrics + HARD-GATE status for one method's output workbook. Gates are
    pass/fail badges shown on every method; a method that fails Conserves is
    excluded from winning a lens (it lost fish), the rest are warning flags the
    operator weighs. Reuses the compare lobby's authoritative verdicts."""
    import yaml as _yaml
    from forecast import optimize as _opt
    from tools.run_compare import _conservation_verdict, _harvest_extras
    with open(CONFIG_DIR / "control.yaml") as _f:
        _cfg = _yaml.safe_load(_f) or {}
    hv_cap = float(_cfg.get("max_harvest_per_week", 55000) or 55000)
    min_hv = float(_cfg.get("min_harvest_per_week", 0) or 0)
    welfare = float(_cfg.get("density_welfare_threshold_kg_m3", 80) or 80)
    m, _dropped, _overprod = _opt.metrics_from_workbook(out_path, hv_cap,
                                                        welfare_density=welfare)
    verdict = _conservation_verdict(out_path)
    harv = _harvest_extras(out_path, min_hv)
    # "No empty week": the HARD contract rule is "never a NEAR-EMPTY week". A week a
    # little under the floor (a pinned startup week, or 15 fish short of rounding) is
    # not a breach; a crater (e.g. 377 fish) is. Flag only weeks below a quarter of
    # the floor so the badge isolates real craters from benign sub-floor weeks.
    near_empty = 0.25 * min_hv
    min_wk = harv.get("min_week", 0) or 0
    # "Under cap": a method riding its DESIGNED deviation band (~0.5% crest) is at the
    # cap, not over it; only a material overshoot fails. Tolerance = the band + margin.
    dev = float(_cfg.get("facility_biomass_deviation_pct", 0.005) or 0.005)
    cap_tol = 1.0 + max(dev, 0.005) + 0.01
    under_cap = (m.overall_peak_biomass <= m.biomass_cap * cap_tol) if m.biomass_cap else True
    gates = {
        "Conserves": verdict["gate"] != "FAIL",
        "Fully placed": verdict.get("unplaced_batches", 0) == 0,
        "No empty week": (min_wk >= near_empty) if min_hv else True,
        "Under cap": under_cap,
    }
    return {"metrics": m, "verdict": verdict, "harvest": harv, "gates": gates}


def _board_badges(gates):
    return "  ".join(f"{'✅' if ok else '⚠️'} {name}" for name, ok in gates.items())


_BOARD_ORDER = tuple(_methods.DEFAULT_ROSTER)


def _board_method_sig(mkey: str, pr_md5: str) -> str:
    """Identity of one board leg's inputs, so a finished method can be reused
    instead of re-run. "board2" is a schema tag — bump it when the stored
    result shape or the method keys change (mirrors the "proj3" tag in
    _mw_project). The CP-SAT knobs enter only the method they affect, so moving
    the Computer power slider doesn't needlessly invalidate the fast methods."""
    import hashlib
    parts = ["board2", pr_md5, _config_fingerprint(), mkey]
    if (_METHODS.get(mkey) or _METHODS["controller"]).engine_kwargs.get("optimal"):
        parts += [f"cpsat{_cpsat_det_time()}", str(_cpu_workers())]
    return hashlib.md5("|".join(parts).encode()).hexdigest()


def _cpsat_det_time() -> float:
    """CP-SAT's per-week DETERMINISTIC work budget — the criterion that actually
    stops each solve. (The wall-clock limit is only a safety cap for a
    pathological week, so tuning it changes nothing.) Higher = tighter layout,
    longer solve.

    Reads the durable copy when the Compare-board slider isn't rendered —
    Streamlit deletes widget-backed keys on any rerun that doesn't draw the
    widget, so without the fallback every other mode (including ▶ Run forecast
    re-running a picked CP-SAT plan) silently reverted to 30.0."""
    return float(st.session_state.get(
        "cpsat_depth", st.session_state.get("_cpsat_depth_saved", 30.0)))


def _ensure_board_score(res: dict, label: str) -> None:
    """Grade a finished run, once. The grading is three full workbook reads, so
    it lives inside the stored result — and a run whose grading failed
    transiently can be re-graded on reuse without re-running the solve."""
    if not (res.get("ok") and res.get("output_path")) or res.get("_score"):
        return
    with st.spinner(f"Grading {label}…"):
        try:
            res["_score"] = _board_score(res["output_path"])
            res.pop("_score_err", None)
        except Exception as e:  # noqa: BLE001
            res["_score"] = None
            res["_score_err"] = str(e)


def _compare_and_choose():
    st.header("⚖️ Compare & Choose — run the methods, pick the plan")
    st.caption(
        "Runs the planning methods on your PR, grades them on several lenses, and "
        "lets **you** pick which plan becomes the report. Each plan is internally "
        "consistent (0-drift, tank continuity) — you choose a whole plan, not a "
        "splice. The hard rules (conserves · fully placed · harvest floor · under "
        "cap) show as badges on every method, so a low-transfer plan can't hide a "
        "contract breach.")

    _cfg_ok = _config_ready() and _scenario_ready()
    _pr_ok = pr is not None and pr["ok"]
    if not _cfg_ok:
        st.info("No config yet — set it up in **Configure** first.")
        return
    if not _pr_ok:
        st.info("Upload a valid **ProductionReport** in the sidebar first.")
        return

    include_milp = st.checkbox(
        "Include the optimal CP-SAT placement (slow — tightest density, most "
        "balanced across systems)", value=True, key="board_milp")
    _always = [k for k in _BOARD_ORDER if k not in _BOARD_OPTIONAL]
    st.caption(
        ", ".join(f"{_METHODS[k].label} ({_TYPICAL.get(k, '?')})" for k in _always)
        + " always run. The CP-SAT leg gives each of your ~130 weeks its own "
        "solver budget, so it can run well past its estimate — uncheck it for a "
        "fast compare and add it later, since finished methods are reused. On a "
        "capacity-bound config (facility full at peak) **Controller + LNS "
        "usually matches plain Controller** — LNS only diverges when there's "
        "tank slack to relocate into.")
    if include_milp:
        # value= seeds from the durable copy so leaving Compare mode (which
        # drops the widget key) doesn't snap the depth back to Balanced — that
        # both re-ran picked plans at the wrong budget and falsely marked
        # finished CP-SAT legs stale (the board sig embeds _cpsat_det_time()).
        st.select_slider(
            "CP-SAT solve depth", options=[8.0, 30.0, 60.0],
            value=st.session_state.get("_cpsat_depth_saved", 30.0),
            format_func=lambda v: {8.0: "Quick", 30.0: "Balanced",
                                   60.0: "Thorough"}[v],
            key="cpsat_depth",
            help="Deterministic work budget per week — the criterion that "
                 "actually stops each solve (the wall-clock limit is only a "
                 "safety cap). Quick trades layout tightness for a much shorter "
                 "run; Balanced is the validated default.")
        st.session_state["_cpsat_depth_saved"] = float(
            st.session_state["cpsat_depth"])

    roster = [(k, _METHODS[k].label) for k in _BOARD_ORDER
              if k not in _BOARD_OPTIONAL or include_milp]

    _b1, _b2 = st.columns([3, 2])
    run_all = _b1.button("▶ Run all methods & compare", type="primary",
                         use_container_width=True,
                         help="Reuses any method already finished for these exact "
                              "inputs — only missing or stale methods run.")
    rerun_all = _b2.button("↻ Re-run all from scratch", use_container_width=True,
                           help="Discards finished results and runs every method again.")
    st.caption("Each method is saved the moment it finishes, so an interrupted "
               "compare keeps the legs that completed — click ▶ again to finish "
               "the rest.")

    if run_all or rerun_all:
        import hashlib
        from datetime import datetime as _dtn
        store = st.session_state.setdefault("_board_store", {})
        if rerun_all:
            store.clear()
        st.session_state["_board_roster"] = roster
        pr_md5 = hashlib.md5(uploaded.getvalue()).hexdigest()
        n = len(roster)
        bar = st.progress(0.0, text="Starting…")
        for i, (mkey, mlabel) in enumerate(roster):
            msig = _board_method_sig(mkey, pr_md5)
            done = store.get(mkey)
            if (done and done.get("sig") == msig and done["res"].get("ok")
                    and done["res"].get("output_path")):
                _ensure_board_score(done["res"], mlabel)
                bar.progress((i + 1) / n, text=f"{mlabel}: reusing finished result")
                continue
            # The bar can only move between methods — the engine call below
            # blocks the script — so say so, and give a clock to judge against.
            bar.progress(i / n, text=(
                f"{i}/{n} finished · running {mlabel} (typically "
                f"{_TYPICAL.get(mkey, '?')}, started {_dtn.now():%H:%M}) — "
                f"this bar next moves when the method finishes"))
            with st.status(f"Running {mlabel}…", expanded=False) as _ms:
                res = _run_with_workbook_bytes(
                    uploaded.getvalue(), uploaded.name,
                    config_dir=str(CONFIG_DIR), scenario_dir=str(SCENARIO_DIR),
                    method=mkey, cpsat_time=300.0,
                    cpsat_det_time=_cpsat_det_time(),
                    cpsat_workers=_cpu_workers(),
                    on_line=lambda ln, _s=_ms, _l=mlabel: _s.update(
                        label=f"{_l} — {ln[:100]}"))
                _ms.update(
                    label=(f"{mlabel} — done in {res.get('elapsed', 0):,.0f}s"
                           if res.get("ok") else f"{mlabel} — failed"),
                    state="complete" if res.get("ok") else "error")
            res["_label"] = mlabel
            _ensure_board_score(res, mlabel)
            store[mkey] = {"sig": msig, "res": res}   # persists NOW, per method
            bar.progress((i + 1) / n,
                         text=f"✓ {mlabel} done in {res.get('elapsed', 0):,.0f}s "
                              f"({i + 1}/{n})")
        bar.progress(1.0, text=f"All {n} method(s) complete")

    store = st.session_state.get("_board_store") or {}
    results = {k: store[k]["res"] for k in _BOARD_ORDER if k in store}
    if not results:
        return

    _planned = st.session_state.get("_board_roster") or []
    _missing = [lbl for k, lbl in _planned if k not in results]
    if _missing:
        st.warning(f"Partial compare — {len(results)} of {len(_planned)} methods "
                   f"finished. Missing: {', '.join(_missing)}. Click **▶ Run all "
                   f"methods & compare** to run only those.")

    # Results outlive the inputs that produced them: a config save or a new PR
    # doesn't clear the board, so say which cards no longer match. (The run loop
    # re-runs a stale method, but only once the operator presses ▶ — until then
    # they could otherwise pick a plan built under different knobs.)
    import hashlib as _hl
    _now_pr = _hl.md5(uploaded.getvalue()).hexdigest()
    _stale = {k for k in results
              if store[k].get("sig") != _board_method_sig(k, _now_pr)}
    if _stale:
        st.warning(f"Inputs changed since {len(_stale)} of these result(s) were "
                   f"computed ({', '.join(results[k].get('_label', k) for k in _stale)}"
                   f") — they are shown as-is. **▶ Run all methods & compare** "
                   f"refreshes just those.")

    scored = {k: v for k, v in results.items() if v.get("ok") and v.get("_score")}
    for k, v in results.items():
        if k not in scored:
            st.error(f"**{v.get('_label', k)}** failed: "
                     f"{v.get('error') or v.get('_score_err') or 'no output produced'}")
    if not scored:
        return

    # ---- Grading-lens cards: who wins each (conservation-passers only) ----
    st.subheader("Grading lenses — who wins each")
    with st.expander("ℹ️ What do the badges, metrics and lenses mean?"):
        st.markdown(
            "**Hard-gate badges** (✅ pass · ⚠️ flag) — the non-negotiables every "
            "plan is judged on first:\n"
            "- **Conserves** — no fish lost or created; mass balance ties out "
            "(0 drift). ⚠️ = the plan lost fish, which disqualifies it from winning "
            "any lens.\n"
            "- **Fully placed** — every batch got tanks; none dropped for lack of "
            "space.\n"
            "- **No empty week** — never a near-empty harvest week (meets the weekly "
            "contract floor); ⚠️ = a crater week.\n"
            "- **Under cap** — facility biomass stays within its cap plus the "
            "designed deviation band; ⚠️ = a real overshoot.\n\n"
            "**Per-method metrics** (lower is better on all of these):\n"
            "- **peak % cap** — the single busiest *system-week's* biomass/feed "
            "load vs that system's cap. 100% = right at the cap; over 100% = a "
            "system runs hot that week.\n"
            "- **moves/fish** — tank-to-tank transfers ÷ fish placed. Lower = less "
            "handling, stress and labour.\n"
            "- **density** — the worst per-tank density (kg/m³) reached anywhere; "
            "compare to your ~95 kg/m³ cap. Lower = more headroom.\n"
            "- **between-sys CV** — how *evenly* biomass is spread **system-to-"
            "system**. 0 = perfectly balanced; higher = some systems packed while "
            "others sit light.\n"
            "- **within-sys CV** — the same, but **tank-to-tank inside** each "
            "system.\n"
            "- **reared … kg/m³ (…% crowded)** — the **product-quality** view: the "
            "biomass-weighted average density your fish were *reared at*, and the "
            "fraction of grow-out biomass that spent time **above the welfare line** "
            "(~80 kg/m³, below the hard cap). Lower = gentler rearing = better "
            "welfare / flesh quality — but usually means fewer fish / more tanks.\n\n"
            "**Grading lenses** — each card names the method that's best on one "
            "axis (fewest moves, steadiest harvest, most balanced, tightest "
            "density, smallest footprint, fastest). No method wins them all — the "
            "board shows the trade-offs so **you** pick the plan that fits your "
            "priority, then press **Use this plan**.")
    eligible = {k: v for k, v in scored.items()
                if v["_score"]["gates"]["Conserves"]}
    pool = eligible or scored
    cols = st.columns(2)
    for i, (label, getter, blurb) in enumerate(_BOARD_LENSES):
        vals = {}
        for k, v in pool.items():
            try:
                x = getter(v)
            except Exception:  # noqa: BLE001
                x = None
            if isinstance(x, (int, float)):
                vals[k] = x
        if not vals:
            continue
        win_k = min(vals, key=vals.get)
        win = scored[win_k]
        with cols[i % 2].container(border=True):
            st.markdown(f"**{label}** — *{blurb}*")
            st.markdown(f"→ **{win['_label']}**  ·  `{vals[win_k]:,.3f}`")
            st.caption(_board_badges(win["_score"]["gates"]))

    # ---- Per-method summary + pick ----
    st.subheader("Pick the plan for your report")
    for k, v in scored.items():
        m = v["_score"]["metrics"]
        with st.container(border=True):
            c1, c2 = st.columns([3, 1])
            with c1:
                st.markdown(f"**{v['_label']}**  ·  {v.get('elapsed', 0):.0f}s"
                            + ("  ·  ⚠️ stale" if k in _stale else ""))
                st.caption(_board_badges(v["_score"]["gates"]))
                st.caption(
                    f"peak {m.overall_peak_biomass / (m.biomass_cap or 1) * 100:.0f}% cap"
                    f"  ·  {m.transfers_per_fish:.2f} moves/fish"
                    f"  ·  density {m.density_peak:.0f}"
                    f"  ·  between-sys CV {m.between_system.get('bio_cv_mean', 0):.3f}"
                    f"  ·  within-sys CV {m.within_system.get('bio_cv_mean', 0):.3f}"
                    f"  ·  reared {m.mean_rearing_density:.0f} kg/m³ "
                    f"({m.crowded_biomass_fraction * 100:.0f}% crowded)")
            with c2:
                if st.button("Use this plan", key=f"board_pick_{k}",
                             use_container_width=True):
                    # Shallow copy: `v` IS the board's stored entry, so writing
                    # _run_label straight onto it would relabel the board card
                    # too (and any later annotation of the active result would
                    # leak back into the store). The big payloads stay shared.
                    st.session_state.result = {
                        **v, "_run_label": f"Compare & Choose — {v['_label']}"}
                    # This is where the planning method is chosen — ▶ Run
                    # forecast re-runs THIS method from now on.
                    st.session_state["_chosen_method"] = k
                    st.session_state["_goto_run_mode"] = True
                    st.rerun()


if app_mode.startswith("Compare"):
    _compare_and_choose()
    st.stop()

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
    _method = st.session_state.get("_chosen_method", _DEFAULT_METHOD)
    _mobj = _METHODS.get(_method) or _METHODS[_DEFAULT_METHOD]
    _spin = (f"Running {_mobj.label} — typically {_TYPICAL.get(_method, '?')}...")
    with st.status(_spin, expanded=True) as status:
        st.write("Config + scenario from the app; ProductionReport from upload...")

        def _narrate(line, _s=status):
            """Show each pipeline stage as it happens: the newest line as the
            status label, the whole sequence in the body below it."""
            _s.update(label=line[:110])
            st.write(line)

        result = _run_with_workbook_bytes(
            uploaded.getvalue(), uploaded.name,
            config_dir=str(CONFIG_DIR), scenario_dir=str(SCENARIO_DIR),
            method=_method, cpsat_time=300.0,
            cpsat_det_time=_cpsat_det_time(),
            cpsat_workers=_cpu_workers(),
            on_line=_narrate,
        )
        if result["ok"]:
            st.write(
                f"✓ Pipeline complete in {result['elapsed']:.1f}s — "
                f"{result['violations']} violations, "
                f"worst {result['worst_density']:.1f} kg/m³"
            )
            status.update(label="✓ Forecast complete", state="complete")
            result["_run_label"] = (
                _mobj.label if _method != "controller"
                else f"Controller — {_harvest_mode_label(CONFIG_DIR)}")
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
    # Cache key for every derived frame below.
    _rid = _result_rid(r)

    # Provenance — always show WHICH run is on screen (keep the correct data).
    st.caption(f"📋 Showing: **{r.get('_run_label', 'forecast run')}**")
    if r.get("config_used"):
        _render_active_config(r["config_used"],
                              "ℹ️ Configuration this run used")

    # ---- KPIs + prominent download button ----
    top_kpi, top_dl = st.columns([3, 1])
    with top_kpi:
        st.subheader("Summary")
        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric("Violations", r["violations"],
                  help="Tanks where realized density exceeds that tank's own "
                       "density cap (per-tank, from facility config; the OG6N "
                       "depuration pool is excluded).")
        k2.metric("Worst density", f"{r['worst_density']:.1f} kg/m³",
                  help="Highest per-tank density across the horizon")
        _wl = r.get("welfare_density", 80)
        k3.metric("Reared density",
                  f"{r.get('mean_rearing_density', 0):.0f} kg/m³",
                  help=f"Product-quality view: the biomass-weighted average density "
                       f"your fish were REARED at over grow-out (lower = gentler = "
                       f"better welfare / flesh quality). The delta shows the share "
                       f"of biomass that spent time above the {_wl:.0f} kg/m³ "
                       f"welfare line.",
                  delta=f"{r.get('crowded_biomass_fraction', 0) * 100:.0f}% crowded",
                  delta_color="inverse")
        k4.metric("Total harvest", f"{r['harvest_kg']/1000:,.1f} t",
                  help="Sum of all harvest events across the horizon")
        k5.metric("Run time", f"{r['elapsed']:.1f}s")
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
    bl_df = _rv_memo("bl_df", _rid, lambda: (
        pd.DataFrame(r["batch_locations"]) if r["batch_locations"] else pd.DataFrame()))
    he_df = _rv_memo("he_df", _rid, lambda: (
        pd.DataFrame(r["harvest_events"]) if r["harvest_events"] else pd.DataFrame()))
    bio_df = _rv_memo("bio_df", _rid,
                      lambda: pd.DataFrame(r.get("biology_projection", [])))

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
            def _build_heatmap():
                """Pivots + hover matrix. Widget-independent, and the hover loop
                is O(tanks x weeks) scalar lookups — the single most expensive
                thing the results view used to redo on every click."""
                df = bl_df.copy()
                df["TankLabel"] = df.apply(
                    lambda r: f"{r['System']}-{r['Tank']}", axis=1)
                tank_order = sorted(
                    df["TankLabel"].unique(),
                    key=lambda t: (t.split("-")[0],
                                   int(t.split("-")[1]) if t.split("-")[1].isdigit() else 0),
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
                _dv = pd.to_numeric(df["Density_kg_m3"], errors="coerce")
                vmax = max(130.0, float(_dv.max()) if _dv.notna().any() else 130.0)
                customdata = []
                for tl in tank_order:
                    row_cd = []
                    for wk in weeks:
                        bid = batch_pivot.loc[tl, wk] if wk in batch_pivot.columns else None
                        d = density_pivot.loc[tl, wk] if wk in density_pivot.columns else None
                        row_cd.append([str(bid) if bid else "—",
                                       f"{d:.1f}" if isinstance(d, (int, float)) else "—"])
                    customdata.append(row_cd)
                return density_pivot, batch_pivot, tank_order, weeks, vmax, customdata

            (density_pivot, batch_pivot, tank_order, weeks, vmax,
             customdata) = _rv_memo("ov_heatmap", _rid, _build_heatmap)
            # Severity-honest scale: span the TRUE worst density (clamping at 130
            # hid 3.8x spikes as ordinary red). Hard color break at the cap;
            # over-cap tanks deepen toward dark red as they get worse.
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
            sys_feed_rows, feed_caps = _rv_memo(
                "sysfeed", _rid, lambda: _system_feed_audit(r.get("output_path")))
            if sys_feed_rows:
                sys_feed = pd.DataFrame(sys_feed_rows).sort_values(["Week", "System"])
                fig = px.line(
                    sys_feed, x="Week", y="Feed_kg_day", color="System",
                    markers=True,
                    title="Per-system feed (kg/day) over time — REALIZED",
                )
                # One dashed line per DISTINCT cap, labelled with the systems it
                # applies to. Systems sharing a cap collapse to a single line so
                # the chart is not striped with duplicates.
                _by_cap = {}
                for _s, _c in (feed_caps or {}).items():
                    _by_cap.setdefault(round(float(_c), 6), []).append(_s)
                for _c, _syss in sorted(_by_cap.items()):
                    _lbl = (", ".join(sorted(_syss)) if len(_syss) <= 3
                            else f"{len(_syss)} systems")
                    fig.add_hline(y=_c, line_dash="dash", line_color="red",
                                  opacity=0.55,
                                  annotation_text=f"{_lbl}: {_c:,.0f} kg/day cap",
                                  annotation_font_size=10)
                fig.update_layout(height=380, yaxis_title="kg/day",
                                  legend=dict(title="System"))
                st.plotly_chart(fig, use_container_width=True)
                st.caption(
                    "REALIZED per-system feed from SystemLimitsAudit — the actual "
                    "fed plan (after harvest + FIFO), the exact series the feed "
                    "caps are checked against. NOT the unharvested biology "
                    "projection (which ignores harvest and spikes well past the "
                    "cap). Each dashed line is the cap for the system(s) named on "
                    "it — OG1/2 and OG3-6 do not share a feed cap. Lines riding "
                    "just under their own cap = leveled correctly; brief "
                    "crossings are the residual over-cap weeks."
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
            def _build_batch_agg():
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
                agg["AvgWt_kg"] = (agg["Biomass_kg"] / agg["Count"]).where(
                    agg["Count"] > 0, 0)
                return agg

            agg = _rv_memo("pb_agg", _rid, _build_batch_agg)
            batches = sorted(agg["Batch"].dropna().unique())
            default = ["B46", "B47"] if all(b in batches for b in ("B46", "B47")) else batches[:2]
            all_weeks = sorted(agg["Week"].dropna().unique())
            # Keyed so the selection survives reruns triggered elsewhere in the
            # app; dropped when a new run's options no longer contain it (a
            # select_slider raises on a value outside its options).
            if ("pb_batches" in st.session_state
                    and not set(st.session_state["pb_batches"]) <= set(batches)):
                del st.session_state["pb_batches"]
            if ("pb_period" in st.session_state
                    and not all(w in all_weeks for w in st.session_state["pb_period"])):
                del st.session_state["pb_period"]

            ctrl_l, ctrl_r = st.columns([2, 3])
            with ctrl_l:
                picked = st.multiselect(
                    "Batches", batches, default=default, key="pb_batches",
                    help="Pick one or more batches to compare trajectories.",
                )
            with ctrl_r:
                if len(all_weeks) >= 2:
                    wk_lo, wk_hi = st.select_slider(
                        "Period",
                        options=all_weeks,
                        value=(all_weeks[0], all_weeks[-1]),
                        key="pb_period",
                        help="Slide endpoints to zoom in on a specific window.",
                    )
                else:
                    # NB: parenthesised so the empty case yields BOTH values —
                    # unparenthesised, `all_weeks[0]` ran before the guard.
                    wk_lo, wk_hi = ((all_weeks[0], all_weeks[-1]) if all_weeks
                                    else (None, None))

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
            def _build_period():
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
                return wk_facility

            wk_facility = _rv_memo("period", _rid, _build_period)

            c1, c2 = st.columns(2)
            with c1:
                # WHOLE-FACILITY basis (OG + 6N + freshwater) — what the cap
                # actually governs. The tank-row sum below omits the FW phase
                # (~7% of cap at peak), so plotting it against this cap line
                # showed headroom that did not exist.
                _fwk = pd.DataFrame(r.get("facility_weekly") or [])
                _has_fac = (not _fwk.empty
                            and _fwk["Total_Biomass_kg"].notna().any())
                if _has_fac:
                    _fwk = _fwk.dropna(subset=["Total_Biomass_kg"]).sort_values("Week")
                    fig = px.line(_fwk, x="Week", y="Total_Biomass_kg",
                                  markers=True,
                                  title="Facility biomass (kg) — whole facility, "
                                        "incl. freshwater")
                    _lim = _fwk["Biomass_Limit_kg"]
                    if _lim.dropna().nunique() > 1:
                        # Per-week cap (FacilityLimits ramp) — one hline at
                        # week 1's value would misread the whole horizon.
                        fig.add_scatter(x=_fwk["Week"], y=_lim, mode="lines",
                                        line={"dash": "dash", "color": "red"},
                                        name="Max Biomass cap (per-week)")
                        _cap_kg = None
                    else:
                        _cap_kg = (float(_lim.dropna().iloc[0])
                                   if _lim.notna().any() else
                                   float((r.get("config_used") or {})
                                         .get("max_biomass_kg") or 3_800_000))
                else:
                    fig = px.line(wk_facility, x="Week", y="FacilityBiomass_kg",
                                  markers=True,
                                  title="Facility biomass (kg) — TANKS ONLY "
                                        "(freshwater not included)")
                    _cap_kg = float((r.get("config_used") or {})
                                    .get("max_biomass_kg") or 3_800_000)
                if _cap_kg is not None:
                    fig.add_hline(
                        y=_cap_kg, line_dash="dash", line_color="red",
                        annotation_text=f"Max Biomass cap ({_cap_kg / 1000:,.0f} t)")
                fig.update_layout(height=350, yaxis_title="kg")
                st.plotly_chart(fig, use_container_width=True)
                if not _has_fac:
                    st.caption("⚠ Advisory sheet not found in this workbook — "
                               "showing tank biomass only, which sits below the "
                               "cap line by the freshwater share (~7% at peak).")
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
            k1.metric("Total harvest", f"{tot_kg/1000:,.1f} t",
                      help="Total gross (live) biomass harvested across the whole "
                           "forecast horizon.")
            k2.metric("Total fish", f"{tot_count:,.0f}",
                      help="Total number of fish harvested across the horizon.")
            k3.metric("Avg weight at harvest", f"{avg_kg:.2f} kg",
                      help="Harvest-weighted average LIVE weight per fish "
                           "(total kg ÷ total fish).")

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

            # Daily harvest schedule — each weekly tank-harvest split Mon–Fri,
            # with a per-week Total row and a blank line between weeks.
            st.markdown("**Daily harvest schedule (Mon–Fri)**")
            st.caption(
                "**All tanks harvesting in a week are combined**, then split "
                "evenly across the five operating days (Mon–Fri), with a **Total** "
                "row per week and a blank line between weeks. Tank/Batch list every "
                "tank + batch that contributed; average weights are blended (total "
                "biomass ÷ total fish). Same as the Excel 'Daily Harvest Schedule' "
                "sheet.")
            _dh, _totpos, _blankpos = _rv_memo(
                "harvest_daily", _rid, lambda: _daily_harvest_table(he_df))
            if _dh.empty:
                st.info("No datable harvest events for a daily breakout.")
            else:
                def _hl_totals(row):
                    if row.name in _totpos:
                        return ["background-color:#e8eaf0;font-weight:700"] * len(row)
                    return [""] * len(row)
                st.dataframe(_dh.style.apply(_hl_totals, axis=1),
                             hide_index=True, use_container_width=True,
                             height=min(760, 44 + 35 * len(_dh)))

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
            c1.metric("Batches", len(pf),
                      help="Number of production batches in this plan.")
            c2.metric("Density risk (OVER CAP)", n_over,
                      help="How many batches peak ABOVE the density cap at some "
                           "point in grow-out — the cohorts to watch for crowding.")
            if peak_col:
                worst = pd.to_numeric(pf[peak_col], errors="coerce").max()
                c3.metric("Worst peak density", f"{worst:.2f}× cap" if pd.notna(worst) else "—",
                          help="The single highest peak density any batch reaches, "
                               "as a multiple of its tank's cap (1.0× = right at "
                               "cap, 1.3× = 30% over).")

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
        bplans = _rv_memo("bplans", _rid,
                          lambda: _derive_batch_plans(bl_df, he_df))
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
            # A previous run's pick may not exist in this run's plans (different
            # PR, or a Global-LP plan without per-tank rows) — Streamlit passes
            # the stale value through verbatim, so guard it or next() raises.
            _bp_opts = [p["Batch"] for p in bplans]
            if st.session_state.get("batchplan_pick") not in _bp_opts:
                st.session_state.pop("batchplan_pick", None)
            pick = st.selectbox("Batch", _bp_opts, key="batchplan_pick")
            bp = next((p for p in bplans if p["Batch"] == pick), bplans[0])
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("SW entry", bp["SW_entry"],
                      help="The week this batch enters seawater (its FW→OG / "
                           "TranOG transfer).")
            m2.metric("Peak tanks", bp["Peak_tanks"],
                      help="The most grow-out tanks this batch occupies at once — "
                           "its peak facility footprint.")
            m3.metric("Harvest window", bp["Harvest_window"],
                      help="The span of weeks over which this batch is harvested "
                           "out.")
            m4.metric("HOG (t)", f"{bp['HOG_t']:.0f}",
                      help="Total head-on-gutted tonnes this batch yields over its "
                           "harvest window.")
            st.dataframe(pd.DataFrame(bp["milestones"]), hide_index=True,
                         use_container_width=True)
            # Flat export (one row per batch-milestone) for sharing/review.
            _csv = _rv_memo("bplans_csv", _rid, lambda: pd.DataFrame(
                [{"Batch": p["Batch"], **m} for p in bplans for m in p["milestones"]]
            ).to_csv(index=False).encode())
            st.download_button(
                "⬇ Download all batch plans (CSV)",
                data=_csv,
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
