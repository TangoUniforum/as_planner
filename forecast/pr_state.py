"""Read a Production Report workbook into planner-ready in-flight state.

Lifted verbatim out of tools/run_full_facility_poc.py when the Global method
was dropped, because this is shared infrastructure that outlives it: it is how
BOTH the L1 envelope and the capacity/transition study learn where the facility
actually is today. Moving it here means dropping Global cannot take PR
hydration with it.

VERBATIM RELOCATION — the body below is byte-identical to the original and its
output is hash-pinned by tests/test_pr_state_relocation.py. Two behaviours are
preserved deliberately rather than fixed, because a refactor must not change
behaviour:

  * the two early-return paths yield a 3-tuple while the success path yields a
    4-tuple, so a missing workbook raises ValueError in every caller (all of
    which unpack four). Pre-existing; fix it as its own change.
  * it prints to stdout rather than logging.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path


def hydrate_pr(workbook_path: Path, batches):
    """Read PR -> (inflight_og, fw_inflight, derived_start).

    inflight_og: batch_id -> (count, avg_wt_g, cv_pct)  [OG seeds for L1]
    fw_inflight: batch_id -> (count, avg_wt_g, pr_closing_date)  [FW-phase
                 override so FW-in-flight batches project from PR state]
    """
    try:
        from forecast.excel_io import load_workbook
        from forecast.production_report import read_production_report
    except Exception as e:  # noqa: BLE001
        print(f"  (could not import PR reader: {e}); running incoming-only")
        return {}, {}, None
    if not workbook_path.exists():
        print(f"  (workbook {workbook_path} not found; running incoming-only)")
        return {}, {}, None
    wb = load_workbook(workbook_path)
    pr_closing, og_records, fw_records = read_production_report(wb)
    wb.close()
    derived_start = None
    pr_close_date = None
    if pr_closing is not None:
        derived_start = datetime(pr_closing.year, pr_closing.month,
                                 pr_closing.day) + timedelta(days=1)
        pr_close_date = datetime(pr_closing.year, pr_closing.month, pr_closing.day)

    # Split OG closing fish into grow-out vs 6N-RESIDENT (already mid-purge at
    # hand-over). The 6N fish must NOT seed grow-out — they belong in the purge
    # pipeline, releasing within the next ~2 weeks. Mixing them into grow-out is
    # what made L1 spin a fresh purge backlog from empty (the startup overshoot).
    from forecast.sixn import SIXN_ALL_TANKS
    og_agg: dict[str, dict] = {}
    purge_agg: dict[str, dict] = {}
    for r in og_records:
        target = purge_agg if r.tank_id in SIXN_ALL_TANKS else og_agg
        e = target.setdefault(r.batch_id, {"count": 0.0, "biomass_kg": 0.0})
        e["count"] += r.closing_count
        e["biomass_kg"] += r.closing_biomass_kg
    batch_cv = {b.batch_id: b.tran_og_cv for b in batches}
    inflight_og = {}
    for bid, e in og_agg.items():
        if e["count"] > 0:
            avg_wt = e["biomass_kg"] * 1000.0 / e["count"]
            inflight_og[bid] = (e["count"], avg_wt, batch_cv.get(bid, 16.0))
    # purge_inflight: handed-over 6N fish, batch_id -> (count, avg_wt_g).
    purge_inflight = {}
    for bid, e in purge_agg.items():
        if e["count"] > 0:
            purge_inflight[bid] = (e["count"], e["biomass_kg"] * 1000.0 / e["count"])

    # FW-in-flight: measured in FW units at PR, NOT yet in OG. Mirrors run.py.
    fw_agg: dict[str, dict] = {}
    for r in fw_records:
        e = fw_agg.setdefault(r.batch_id, {"count": 0.0, "biomass_kg": 0.0})
        e["count"] += r.closing_count
        e["biomass_kg"] += r.closing_biomass_kg
    fw_inflight = {}
    for bid, e in fw_agg.items():
        if e["count"] > 0 and bid not in inflight_og:
            avg_wt = e["biomass_kg"] * 1000.0 / e["count"]
            fw_inflight[bid] = (e["count"], avg_wt, pr_close_date)
    return inflight_og, fw_inflight, derived_start, purge_inflight


# Historical name. tools/ and the analysis harnesses import this spelling.
_hydrate_pr = hydrate_pr
