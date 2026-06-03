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
    st.header("Input")
    uploaded = st.file_uploader(
        "Workbook (.xlsm)",
        type=["xlsm", "xlsx"],
        help="The Forecast.xlsm template. Read-only — not modified.",
    )

    st.header("Run")
    run_clicked = st.button(
        "▶ Run forecast",
        type="primary",
        disabled=uploaded is None,
        use_container_width=True,
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
            mime="application/vnd.ms-excel.sheet.macroenabled.12",
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


# ============================================================
# Pipeline runner
# ============================================================

def _run_with_workbook_bytes(input_bytes: bytes, input_name: str) -> dict:
    """Run the pipeline against `input_bytes` in a temp directory.

    Returns a dict with metrics + the output workbook bytes + parsed
    data needed for visualization.
    """
    work_dir = Path(tempfile.mkdtemp(prefix="as_forecast_"))
    in_path = work_dir / input_name
    out_name = (
        Path(input_name).stem + "_planned" + Path(input_name).suffix
    )
    out_path = work_dir / out_name
    in_path.write_bytes(input_bytes)

    # Run the pipeline, capturing console output for display.
    t0 = time.time()
    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            rc = run_pipeline(input_path=in_path, output_path=out_path)
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
    parsed.update({
        "ok": True,
        "elapsed": elapsed,
        "stdout": captured.getvalue(),
        "output_bytes": output_bytes,
        "output_name": out_name,
        "output_path": str(out_path),
    })
    return parsed


def _parse_output_workbook(path: Path) -> dict:
    """Extract data from the saved workbook for the UI's visualization."""
    wb = load_workbook(path, keep_vba=True, data_only=False)

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

    # Harvest events from HarvestPlan (skip section headers).
    harvest_kg = 0.0
    harvest_count = 0
    harvest_events = []
    if "HarvestPlan" in wb.sheetnames:
        ws = wb["HarvestPlan"]
        for row in ws.iter_rows(values_only=True):
            if not row or row[0] is None:
                continue
            # Planner rows have Source="Planner" (col 10), Pin rows have "Pin"
            if len(row) >= 10 and row[9] in ("Pin", "Planner"):
                wk = row[0]
                bid = row[1]
                cnt = row[3]
                gross_kg = row[5]
                gross_avg_kg = row[4]
                if isinstance(cnt, (int, float)) and isinstance(gross_kg, (int, float)):
                    harvest_count += cnt
                    harvest_kg += gross_kg
                    harvest_events.append({
                        "Week": wk, "Batch": bid, "Source": row[9],
                        "Count": cnt, "Gross_kg": gross_kg,
                        "Avg_wt_kg": gross_avg_kg,
                    })

    # Advisory entries (skip the summary section).
    advisory_entries = []
    advisory_summary = []
    if "Advisory" in wb.sheetnames:
        ws = wb["Advisory"]
        rows = list(ws.iter_rows(values_only=True))
        # Find "Summary by category" and "Full list" markers.
        in_summary = False
        in_full = False
        for r in rows:
            if not r:
                continue
            v0 = r[0] if len(r) > 0 else None
            if isinstance(v0, str):
                if v0.strip() == "Summary by category":
                    in_summary = True
                    continue
                if v0.strip() == "Full list":
                    in_summary = False
                    in_full = True
                    continue
            if in_summary and len(r) >= 2 and isinstance(r[1], (int, float)):
                advisory_summary.append({"Category": str(r[0]), "Count": int(r[1])})
            elif in_full and len(r) >= 3 and isinstance(r[0], (int, float)):
                advisory_entries.append({
                    "#": int(r[0]),
                    "Category": str(r[1]) if r[1] else "",
                    "Detail": str(r[2]) if r[2] else "",
                })

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
    }


# ============================================================
# Run the pipeline when the button is clicked
# ============================================================

if run_clicked and uploaded is not None:
    with st.status("Running forecast pipeline...", expanded=True) as status:
        st.write("Loading workbook + projecting biology...")
        result = _run_with_workbook_bytes(uploaded.getvalue(), uploaded.name)
        if result["ok"]:
            st.write(
                f"✓ Pipeline complete in {result['elapsed']:.1f}s — "
                f"{result['violations']} violations, "
                f"worst {result['worst_density']:.1f} kg/m³"
            )
            status.update(label="✓ Forecast complete", state="complete")
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

    tab_over, tab_batch, tab_period, tab_harvest = st.tabs([
        "Overview",
        "Per-Batch",
        "Period Summary",
        "Harvest",
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
            fig = px.imshow(
                density_pivot.values,
                x=weeks, y=tank_order,
                color_continuous_scale=[
                    (0.0, "#f0f0f0"),
                    (0.50, "#a8d5a8"),
                    (0.85, "#f5d49a"),
                    (1.0, "#e8615e"),
                ],
                range_color=[0, 130],
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
                "Color = per-tank density (light green = under 85% target, "
                "amber = approaching cap, red = over 95 kg/m³ cap). Hover "
                "any cell for the batch and exact density."
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
            n_pin = (he_df["Source"] == "Pin").sum()
            n_plan = (he_df["Source"] == "Planner").sum()
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Total harvest", f"{tot_kg/1000:,.1f} t")
            k2.metric("Total fish", f"{tot_count:,.0f}")
            k3.metric("Avg weight at harvest", f"{avg_kg:.2f} kg")
            k4.metric("Operator pins / Planner", f"{n_pin} / {n_plan}")

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

            with st.expander("Raw harvest events"):
                st.dataframe(he_df, hide_index=True, use_container_width=True)

    # ---- Run log (collapsed) ----
    with st.expander("Run log (console output)"):
        st.code(r["stdout"], language="text")
else:
    st.info(
        "Upload a workbook in the sidebar and click ▶ Run forecast to "
        "begin. The input workbook is never modified — output is written "
        "to a new file you can download."
    )
