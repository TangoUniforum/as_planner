"""Faithful reject-at-entry validation for the manual override window.

`validate_manual_events` must match what `advance_facility_window` actually
does on the run, or the in-app editor accepts events that misbehave in trials:
  * fw_to_og is checked against the SAME FW projection — the batch must be in
    freshwater at its week and the target must be feasible (was previously a
    dest-tanks-only check that passed any in/out-of-FW batch);
  * events are sequenced by week with biology advancing between weeks.

These need the gitignored Forecast.xlsm + seeded config/scenario (the FW
projection is real biology), so they skip cleanly when absent — they run in the
main checkout where the data lives.
"""
from __future__ import annotations

from datetime import datetime as _dt, timedelta as _td
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent  # Python/
WORKBOOK = ROOT / "Forecast.xlsm"
CONFIG_DIR = ROOT / "config"
SCENARIO_DIR = ROOT / "scenario"

pytestmark = pytest.mark.skipif(
    not (WORKBOOK.exists()
         and (CONFIG_DIR / "control.yaml").exists()
         and (SCENARIO_DIR / "limits.yaml").exists()),
    reason="needs Forecast.xlsm + seeded config/scenario (gitignored)",
)


@pytest.fixture(scope="module")
def hydrated():
    """(state, ctx) hydrated from the live PR + config, exactly as the app's
    _hydrate_state_from_upload assembles them for faithful validation."""
    from forecast.excel_io import load_workbook
    from forecast.config_io import load_config
    from forecast.scenario_io import load_batches
    from forecast.production_report import (
        read_production_report, hydrate_facility_state)
    from forecast.state import FacilityState
    wb = load_workbook(str(WORKBOOK))
    control, tables, facility = load_config(str(CONFIG_DIR))
    batches = load_batches(str(SCENARIO_DIR))
    pc, og, fw = read_production_report(wb)
    fs = _dt(pc.year, pc.month, pc.day) + _td(days=1)
    control.forecast_start = fs
    state = FacilityState.from_facility_config(facility, today=fs.date())
    hydrate_facility_state(state, og, batches)
    ctx = dict(batch_by_id={b.batch_id: b for b in batches}, tables=tables,
               forecast_start=fs.date(), control=control, pr_closing=pc,
               fw_records=fw)
    # Pick a batch that is actually in freshwater at week 1 (the first one an
    # fw_to_og can legally reference) so the tests don't hard-code an id.
    from forecast.manual_window import _build_fw_lookup
    from forecast.manual_events import ManualEvent
    from forecast.time_grid import forecast_week_labels
    wk1 = forecast_week_labels(fs.date(), 1)[0]
    probe = [ManualEvent(type="fw_to_og", week=1, batch=b.batch_id,
                         destinations=[]) for b in batches]
    lookup = _build_fw_lookup(probe, fw, control, pc, tables, ctx["batch_by_id"])
    fw_batches_wk1 = sorted({bid for (bid, wl) in lookup if wl == wk1})
    return state, ctx, fw_batches_wk1


def _empty_og_tanks(state, n):
    return [t for t, tk in sorted(state.tanks_by_id.items())
            if tk.type == "OG" and tk.is_empty][:n]


class TestFaithfulFwToOgValidation:
    def test_valid_fw_to_og_passes(self, hydrated):
        from forecast.manual_events import (
            ManualEvent, ManualDest, validate_manual_events)
        state, ctx, fw_batches = hydrated
        assert fw_batches, "expected at least one in-FW batch at week 1"
        dests = _empty_og_tanks(state, 2)
        ev = ManualEvent(type="fw_to_og", week=1, batch=fw_batches[0],
                         count=50000,
                         destinations=[ManualDest(tank=t) for t in dests])
        (i, ok, msgs), = validate_manual_events(state, [ev], **ctx)
        # A cull to hit the target must NOT block (informational note filtered).
        assert ok, msgs

    def test_non_fw_batch_is_rejected(self, hydrated):
        # An OG (already-in-seawater) batch has no FW state at week 1 — the old
        # dest-only check passed this; the faithful check must reject it.
        from forecast.manual_events import (
            ManualEvent, ManualDest, validate_manual_events)
        state, ctx, fw_batches = hydrated
        og_batch = next(tk.batch_id for tk in state.tanks_by_id.values()
                        if not tk.is_empty and tk.batch_id not in fw_batches)
        dests = _empty_og_tanks(state, 1)
        ev = ManualEvent(type="fw_to_og", week=1, batch=og_batch, count=10000,
                         destinations=[ManualDest(tank=dests[0])])
        (i, ok, msgs), = validate_manual_events(state, [ev], **ctx)
        assert not ok
        assert any("not in freshwater" in m for m in msgs)

    def test_target_exceeding_fw_is_rejected(self, hydrated):
        from forecast.manual_events import (
            ManualEvent, ManualDest, validate_manual_events)
        state, ctx, fw_batches = hydrated
        dests = _empty_og_tanks(state, 2)
        ev = ManualEvent(type="fw_to_og", week=1, batch=fw_batches[0],
                         count=99_000_000,
                         destinations=[ManualDest(tank=t) for t in dests])
        (i, ok, msgs), = validate_manual_events(state, [ev], **ctx)
        assert not ok
        assert any("exceeds available FW" in m for m in msgs)

    def test_legacy_fallback_skips_fw_feasibility(self, hydrated):
        # Without the run-time context, fw_to_og falls back to structural-only
        # (feasibility deferred to run) — a non-FW batch is NOT rejected.
        from forecast.manual_events import (
            ManualEvent, ManualDest, validate_manual_events)
        state, ctx, fw_batches = hydrated
        og_batch = next(tk.batch_id for tk in state.tanks_by_id.values()
                        if not tk.is_empty and tk.batch_id not in fw_batches)
        dests = _empty_og_tanks(state, 1)
        ev = ManualEvent(type="fw_to_og", week=1, batch=og_batch, count=10000,
                         destinations=[ManualDest(tank=dests[0])])
        (i, ok, msgs), = validate_manual_events(state, [ev])  # no ctx
        assert ok, msgs  # structural-only: dest is empty OG, so it passes


class TestGradedHarvest:
    """Manual graded harvest (top-N-by-size): the split conserves count + biomass
    exactly, reject-at-entry gates an occupied pickup, and a full-pipeline run
    reconciles in EVERY forecast audit (tank continuity 0-drift, 0 dropped)."""

    @staticmethod
    def _source_pickup(state):
        from forecast.sixn import SIXN_ALL_TANKS
        src = max((t for t in state.tanks_by_id.values()
                   if t.type == "OG" and t.tank_id not in SIXN_ALL_TANKS
                   and not t.is_empty), key=lambda t: t.count)
        pickup = next(t for t in sorted(state.tanks_by_id.values(),
                                        key=lambda t: t.tank_id)
                      if t.type == "OG" and t.tank_id not in SIXN_ALL_TANKS
                      and t.is_empty and t.tank_id != src.tank_id)
        return src, pickup

    def test_split_conserves_count_and_biomass(self, hydrated):
        import copy
        from forecast.manual_events import (
            ManualEvent, ManualDest, validate_manual_events)
        from forecast.manual_window import advance_facility_window
        state, ctx, _fw = hydrated
        src, pickup = self._source_pickup(state)
        K = round(src.count * 0.4)
        open_n, open_bio = src.count, src.count * src.avg_wt_g
        ev = ManualEvent(type="graded_harvest", week=1, from_tank=src.tank_id,
                         count=K, destinations=[ManualDest(tank=pickup.tank_id)])
        (_i, ok, msgs), = validate_manual_events(state, [ev], **ctx)
        assert ok, msgs
        sc = copy.deepcopy(state)
        win = advance_facility_window(
            sc, ctx["batch_by_id"], ctx["tables"], ctx["forecast_start"], 2,
            events=[ev], control=ctx["control"], pr_closing=ctx["pr_closing"],
            fw_records=ctx["fw_records"])
        ghs = [e for e in win["transfer_events"] if hasattr(e, "pickup_tank_id")]
        assert len(ghs) == 1
        gh = ghs[0]
        assert abs((gh.pickup_count + gh.retention_count) - open_n) < 1.0   # I1
        bio = (gh.pickup_count * gh.pickup_avg_wt_g
               + gh.retention_count * gh.retention_avg_wt_g)
        assert abs(bio - open_bio) / open_bio < 1e-6                        # I2
        assert abs(gh.pickup_count - K) < 1.0
        assert gh.pickup_avg_wt_g > gh.retention_avg_wt_g   # big class heavier
        hv = [h for h in win["harvest_events"]
              if h.source_tank_id == pickup.tank_id]
        assert hv and abs(hv[0].count - K) < 1.0            # pickup drained
        assert abs(hv[0].avg_wt_g - gh.pickup_avg_wt_g) < 1e-6   # at big weight

    def test_rejects_occupied_pickup(self, hydrated):
        from forecast.sixn import SIXN_ALL_TANKS
        from forecast.manual_events import (
            ManualEvent, ManualDest, validate_manual_events)
        state, ctx, _fw = hydrated
        src, _ = self._source_pickup(state)
        occ = next(t for t in state.tanks_by_id.values()
                   if t.type == "OG" and t.tank_id not in SIXN_ALL_TANKS
                   and not t.is_empty and t.batch_id != src.batch_id
                   and t.tank_id != src.tank_id)
        ev = ManualEvent(type="graded_harvest", week=1, from_tank=src.tank_id,
                         count=round(src.count * 0.3),
                         destinations=[ManualDest(tank=occ.tank_id)])
        (_i, ok, msgs), = validate_manual_events(state, [ev], **ctx)
        assert not ok and any("not empty" in m for m in msgs)

    def test_full_pipeline_audits_clean(self, hydrated, tmp_path):
        import shutil
        from openpyxl import load_workbook as _lw
        import forecast.run as run_mod
        state, _ctx, _fw = hydrated
        src, pickup = self._source_pickup(state)
        K = round(src.count * 0.4)
        sdir = tmp_path / "scenario"
        shutil.copytree(SCENARIO_DIR, sdir)
        (sdir / "manual_events.yaml").write_text(
            "events:\n  - type: graded_harvest\n    week: 1\n"
            f"    from_tank: {src.tank_id}\n    count: {K}\n"
            f"    destinations:\n      - {{tank: {pickup.tank_id}}}\n")
        wb = tmp_path / "Forecast.xlsm"
        shutil.copy(WORKBOOK, wb)
        out = tmp_path / "out.xlsm"
        run_mod.main(str(wb), output_path=str(out),
                     config_dir=str(CONFIG_DIR), scenario_dir=str(sdir))
        owb = _lw(str(out), data_only=True)
        tc = [[c.value for c in r]
              for r in owb["TankContinuityAudit"].iter_rows()]
        drift = [r for r in tc
                 if any("DRIFT" in str(c).upper() for c in r if c is not None)]
        assert not drift, f"{len(drift)} tank-continuity DRIFT rows"
        ratio = next((float(str(r[3])) for r in tc
                      if r and str(r[0]) == "Count (fish)"), None)
        assert ratio is not None and abs(ratio) < 0.3, f"facility ratio {ratio}"
        ic = [[c.value for c in r]
              for r in owb["InputConservationAudit"].iter_rows()]
        assert not any("*** DROP" in str(c).upper()
                       for r in ic for c in r if c is not None), "dropped batch"


class TestWindowHorizonGuard:
    """A manual override window as long as (or longer than) the forecast horizon
    leaves the planner no weeks to plan; the run must reject it, not silently
    clamp the post-window horizon to 1 week."""

    def test_window_at_or_beyond_horizon_is_rejected(self, tmp_path):
        import shutil
        import forecast.run as run_mod
        sdir = tmp_path / "scenario"
        shutil.copytree(SCENARIO_DIR, sdir)
        # A harvest event at a week far beyond any plausible horizon forces
        # window_n >= horizon_weeks.
        (sdir / "manual_events.yaml").write_text(
            "events:\n"
            "  - type: harvest\n"
            "    week: 9999\n"
            "    from_tank: 35\n"
            "    count: 1000\n")
        wb = tmp_path / "Forecast.xlsm"
        shutil.copy(WORKBOOK, wb)
        out = tmp_path / "out.xlsm"
        with pytest.raises(ValueError, match="horizon"):
            run_mod.main(str(wb), output_path=str(out),
                         config_dir=str(CONFIG_DIR), scenario_dir=str(sdir))
