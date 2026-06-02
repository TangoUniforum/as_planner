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

    if "result" in st.session_state:
        st.success(
            f"Last run: {st.session_state.result['elapsed']:.1f}s, "
            f"{st.session_state.result['violations']} viols"
        )
        st.download_button(
            label="⬇ Download output workbook",
            data=st.session_state.result["output_bytes"],
            file_name=st.session_state.result["output_name"],
            mime="application/vnd.ms-excel.sheet.macroenabled.12",
            use_container_width=True,
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

    # Harvest totals from HarvestPlan (skip Section headers).
    harvest_kg = 0.0
    harvest_count = 0
    if "HarvestPlan" in wb.sheetnames:
        ws = wb["HarvestPlan"]
        for row in ws.iter_rows(values_only=True):
            if not row or row[0] is None:
                continue
            # Planner rows have Source="Planner" (col 10), Pin rows have "Pin"
            if len(row) >= 10 and row[9] in ("Pin", "Planner"):
                if isinstance(row[3], (int, float)) and isinstance(row[5], (int, float)):
                    harvest_count += row[3]
                    harvest_kg += row[5]

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
            status.update(label="✗ Pipeline failed", state="error")


# ============================================================
# Results view
# ============================================================

if "result" in st.session_state and st.session_state.result.get("ok"):
    r = st.session_state.result

    # ---- KPIs ----
    st.subheader("Summary")
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Violations", r["violations"], help="Tanks where density > 95 kg/m³")
    k2.metric("Worst density", f"{r['worst_density']:.1f} kg/m³",
              help="Highest per-tank density across the horizon")
    k3.metric("Total harvest", f"{r['harvest_kg']/1000:,.1f} t",
              help="Sum of all harvest events across the horizon")
    k4.metric("Run time", f"{r['elapsed']:.1f}s")

    # ---- Advisory grouped by category ----
    st.subheader("Advisory")
    if r["advisory_summary"]:
        col_table, col_detail = st.columns([1, 2])
        with col_table:
            st.caption("Issues by category")
            st.dataframe(
                pd.DataFrame(r["advisory_summary"]),
                hide_index=True,
                use_container_width=True,
            )
        with col_detail:
            st.caption("Details (expand each category)")
            entries = r["advisory_entries"]
            by_cat: dict[str, list] = defaultdict(list)
            for e in entries:
                by_cat[e["Category"]].append(e["Detail"])
            # Sort categories by count desc.
            for cat in sorted(by_cat, key=lambda c: -len(by_cat[c])):
                with st.expander(f"{cat} ({len(by_cat[cat])})"):
                    for d in by_cat[cat][:50]:
                        st.text(d)
                    if len(by_cat[cat]) > 50:
                        st.caption(f"… and {len(by_cat[cat]) - 50} more")
    else:
        st.info("No advisory entries — clean run.")

    # ---- Tank occupancy heatmap ----
    st.subheader("Tank occupancy over time")
    bl = r["batch_locations"]
    if bl:
        df = pd.DataFrame(bl)
        # Pivot: rows=Tank, cols=Week, cell=Batch (text). Density as a
        # secondary color via a second pivot used for hover info.
        df["TankLabel"] = df.apply(lambda r: f"{r['System']}-{r['Tank']}", axis=1)
        # Sort tanks by system then tank id.
        tank_order = sorted(
            df["TankLabel"].unique(),
            key=lambda t: (t.split("-")[0], int(t.split("-")[1]) if t.split("-")[1].isdigit() else 0),
        )
        weeks = sorted(df["Week"].dropna().unique())
        # Build density matrix; color by density. Hover shows batch + density.
        density_pivot = df.pivot_table(
            index="TankLabel", columns="Week",
            values="Density_kg_m3", aggfunc="first",
        ).reindex(index=tank_order, columns=weeks)
        batch_pivot = df.pivot_table(
            index="TankLabel", columns="Week",
            values="Batch", aggfunc="first",
        ).reindex(index=tank_order, columns=weeks)
        # Plotly heatmap colored by density.
        fig = px.imshow(
            density_pivot.values,
            x=weeks, y=tank_order,
            color_continuous_scale=[
                (0.0, "#f0f0f0"),
                (0.50, "#a8d5a8"),   # under 85% target — light green
                (0.85, "#f5d49a"),   # 80-95 — amber
                (1.0, "#e8615e"),    # over cap — red
            ],
            range_color=[0, 130],
            labels=dict(x="Week", y="Tank", color="Density (kg/m³)"),
            aspect="auto",
        )
        # Custom hover: tank, week, batch, density.
        customdata = []
        for tl in tank_order:
            row_cd = []
            for wk in weeks:
                bid = batch_pivot.loc[tl, wk] if wk in batch_pivot.columns else None
                d = density_pivot.loc[tl, wk] if wk in density_pivot.columns else None
                row_cd.append([str(bid) if bid else "—", f"{d:.1f}" if isinstance(d, (int, float)) else "—"])
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
    else:
        st.info("No BatchLocations data — pipeline may have failed silently.")

    # ---- Run log (collapsed by default) ----
    with st.expander("Run log (console output)"):
        st.code(r["stdout"], language="text")
else:
    st.info(
        "Upload a workbook in the sidebar and click ▶ Run forecast to "
        "begin. The input workbook is never modified — output is written "
        "to a new file you can download."
    )
