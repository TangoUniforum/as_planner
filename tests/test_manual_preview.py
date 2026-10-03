"""Before/after previews for manual-window events (forecast/manual_preview.py).

Operator, 2026-10-03: "we should add some information to the manual transfers
windows so I know how much I am moving into and what it will look like once
done." Their October script filled 6N tank 63 in W44 with 23,357 + 32,000 =
55,357 fish -- above the 51,500 limit pinned for W46, the week that tank comes
out of its depuration hold -- and nothing in the editor said so.

Every preview here is checked AGAINST THE RUN: the same events are executed by
manual_window.advance_facility_window (the window engine run.py uses) and the
preview's numbers must equal the run's own Transfer / Harvest / TranOG records.

Uses the operator's 9.30 workbook when it is on this machine, like the other
real-PR tests (test_split_batch_integration); skipped otherwise.
"""
from __future__ import annotations

import contextlib
import copy
import io
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PR_0930 = Path(r"C:\Users\julian.f\OneDrive - Atlantic Sapphire\Production"
               r"\Forecasts\2026\Oct\Oct Forecast V2.xlsm")

pytestmark = pytest.mark.skipif(not PR_0930.exists(),
                                reason="the 2026-09-30 ProductionReport is not on this machine")

# The operator's October window (scenario/manual_events/2026-09-30.yaml as
# saved 2026-10-02), in file order. Index 17 = the W44 og_to_6n 53 -> 63.
OCT_EVENTS = [
    {'type': 'harvest', 'week': 1, 'from_tank': 63},
    {'type': 'harvest', 'week': 2, 'from_tank': 65},
    {'type': 'harvest', 'week': 2, 'from_tank': 71},
    {'type': 'harvest', 'week': 3, 'from_tank': 61},
    {'type': 'og_to_6n', 'week': 1, 'from_tank': 45, 'destinations': [{'tank': 67}]},
    {'type': 'harvest', 'week': 3, 'from_tank': 67},
    {'type': 'og_to_6n', 'week': 2, 'from_tank': 34, 'destinations': [{'tank': 63}]},
    {'type': 'og_to_6n', 'week': 2, 'from_tank': 33, 'destinations': [{'tank': 69, 'count': 28000.0}], 'count': 28000.0},
    {'type': 'og_to_6n', 'week': 3, 'from_tank': 33, 'destinations': [{'tank': 65}]},
    {'type': 'og_to_6n', 'week': 3, 'from_tank': 41, 'destinations': [{'tank': 65}]},
    {'type': 'harvest', 'week': 4, 'from_tank': 63},
    {'type': 'harvest', 'week': 4, 'from_tank': 69},
    {'type': 'harvest', 'week': 5, 'from_tank': 65},
    {'type': 'og_to_6n', 'week': 4, 'from_tank': 43, 'destinations': [{'tank': 61}]},
    {'type': 'og_to_6n', 'week': 4, 'from_tank': 52, 'destinations': [{'tank': 61, 'count': 13000.0}], 'count': 13000.0},
    {'type': 'harvest', 'week': 6, 'from_tank': 61},
    {'type': 'og_to_6n', 'week': 5, 'from_tank': 52, 'destinations': [{'tank': 63}]},
    {'type': 'og_to_6n', 'week': 5, 'from_tank': 53, 'destinations': [{'tank': 63, 'count': 32000.0}], 'count': 32000.0},
    {'type': 'og_to_6n', 'week': 6, 'from_tank': 53, 'destinations': [{'tank': 65}]},
    {'type': 'graded_harvest', 'week': 6, 'from_tank': 36, 'destinations': [{'tank': 71}, {'tank': 33}], 'count': 35000.0, 'mode': 'stage'},
    {'type': 'og_transfer', 'week': 1, 'from_tank': 22, 'destinations': [{'tank': 42}]},
    {'type': 'og_transfer', 'week': 2, 'from_tank': 13, 'destinations': [{'tank': 45}]},
    {'type': 'fw_to_og', 'week': 3, 'destinations': [{'tank': 22, 'size_class': 'big'}, {'tank': 13, 'size_class': 'small'}], 'count': 252747.98735609098, 'batch': 'B50'},
]
W46_LIMIT = [{"week": "2026-W46", "metric": "max_harvest_per_week", "value": 51500.0},
             {"week": "2026-W46", "metric": "min_harvest_per_week", "value": 51500.0}]


@pytest.fixture(scope="module")
def env():
    from openpyxl import load_workbook
    from forecast.config_io import load_config
    from forecast.manual_events import manual_events_from_list
    from forecast.production_report import (hydrate_facility_state,
                                            read_production_report)
    from forecast.scenario_io import load_batches
    from forecast.state import FacilityState
    wb = load_workbook(PR_0930, data_only=True, read_only=False)
    control, tables, facility = load_config(ROOT / "config")
    batches = load_batches(ROOT / "scenario")
    pc, og, fw = read_production_report(wb)
    assert pc == date(2026, 9, 30)
    fs = datetime(pc.year, pc.month, pc.day) + timedelta(days=1)
    control.forecast_start = fs                 # as app._hydrate_state_from_upload
    state = FacilityState.from_facility_config(facility, today=fs.date())
    hydrate_facility_state(state, og, batches)
    state.grade_efficiency = float(control.grade_efficiency or 1.0)
    ctx = {"batch_by_id": {b.batch_id: b for b in batches}, "tables": tables,
           "forecast_start": fs.date(), "control": control, "pr_closing": pc,
           "fw_records": fw}
    return state, ctx, manual_events_from_list(OCT_EVENTS)


def _run_window(env, events, n=6):
    """The run's own window over the operator's events."""
    from forecast.manual_window import advance_facility_window
    state, ctx, _ = env
    sc = copy.deepcopy(state)
    with contextlib.redirect_stdout(io.StringIO()):
        return advance_facility_window(
            sc, ctx["batch_by_id"], ctx["tables"], ctx["forecast_start"], n,
            events=events, control=ctx["control"], pr_closing=ctx["pr_closing"],
            fw_records=ctx["fw_records"])


def _preview(env, idx, limits=None):
    """Preview event `idx` as if it were the last one added to the window."""
    from forecast import manual_preview as mp
    from forecast.scenario_io import facility_limits_from_list
    state, ctx, evs = env
    cand = evs[idx]
    rest = evs[:idx] + evs[idx + 1:]
    base = mp.state_before_event(state, ctx, rest, cand.week)
    fl = facility_limits_from_list(limits) if limits is not None else None
    return mp.preview_event(base, cand, ctx, rest, facility_limits=fl,
                            window_weeks=6)


def _week_date(env, wk):
    from forecast.time_grid import week_start
    return week_start(wk - 1, env[1]["forecast_start"])


def _transfers_into(win, tank, wk_date):
    return [tr for tr in win["transfer_events"]
            if tr.event_date == wk_date
            and any(a.tank_id == tank for a in getattr(tr, "destinations", []) or [])]


# ---------- the operator's case: tank 63 in W44 ----------

def test_second_fill_of_tank_63_shows_what_the_run_puts_there(env):
    win = _run_window(env, env[2])
    into63 = _transfers_into(win, 63, _week_date(env, 5))
    assert [round(t.count_transferred) for t in into63][1] == 32000
    first = into63[0].count_transferred                  # 52 -> 63, whole tank
    p = _preview(env, 17, W46_LIMIT)
    t63 = next(t for t in p.tanks if t.tank_id == 63)
    assert p.week == "2026-W44" and not p.refused
    # "before" already holds the same-week fill that runs first.
    assert t63.before_count == pytest.approx(first, abs=0.5)
    assert t63.after_count == pytest.approx(first + 32000, abs=0.5)
    src = next(t for t in p.tanks if t.tank_id == 53)
    assert src.before_count - src.after_count == pytest.approx(32000, abs=0.5)
    assert p.d_fish == pytest.approx(0, abs=0.5)
    assert p.d_biomass_kg == pytest.approx(0, abs=0.5)


def test_tank_63_is_flagged_against_the_w46_limit(env):
    p = _preview(env, 17, W46_LIMIT)
    s6 = [s for s in p.sixn if s.tank_id == 63]
    assert len(s6) == 1
    s6 = s6[0]
    # Filled in window week 5 (W44); comes out after the 2-week hold: W46,
    # which is past the 6-week window -> the planner's week.
    assert s6.harvest_week == "2026-W46" and not s6.scripted and not s6.in_window
    assert s6.limit == 51500 and s6.floor == 51500
    assert s6.count_after > 51500 and s6.over_limit
    # The same fill under a roomier limit is not flagged.
    roomy = [{"week": "2026-W46", "metric": "max_harvest_per_week", "value": 60000.0}]
    assert not [s for s in _preview(env, 17, roomy).sixn if s.over_limit]


def test_limit_falls_back_to_control_without_a_week_row(env):
    from forecast.scenario_io import facility_limits_from_list
    p = _preview(env, 17, [])
    s6 = next(s for s in p.sixn if s.tank_id == 63)
    assert s6.limit == env[1]["control"].max_harvest_per_week
    assert facility_limits_from_list([]) is not None


def test_a_scripted_harvest_of_the_tank_sets_its_harvest_week(env):
    from forecast import manual_preview as mp
    from forecast.manual_events import ManualEvent
    evs = env[2] + [ManualEvent(type="harvest", week=6, from_tank=63)]
    assert mp.harvest_week_of(63, 5, evs) == (6, True)
    assert mp.harvest_week_of(63, 5, env[2]) == (7, False)


# ---------- every action type agrees with the run ----------

def test_harvest_preview_equals_the_runs_harvest(env):
    win = _run_window(env, env[2])
    d = _week_date(env, 5)
    run_h = [h for h in win["harvest_events"]
             if h.event_date == d and h.source_tank_id == 65]
    p = _preview(env, 12)                                  # W44 harvest 65
    assert p.harvested_count == pytest.approx(sum(h.count for h in run_h), abs=0.5)
    t65 = next(t for t in p.tanks if t.tank_id == 65)
    assert t65.after_count == 0 and p.harvest is not None
    assert p.harvest.this_event == pytest.approx(p.harvested_count)


def test_move_preview_equals_the_runs_transfer(env):
    win = _run_window(env, env[2])
    run_t = _transfers_into(win, 45, _week_date(env, 2))
    p = _preview(env, 21)                                  # W41 og_transfer 13 -> 45
    t45 = next(t for t in p.tanks if t.tank_id == 45)
    assert t45.after_count - t45.before_count == pytest.approx(
        sum(t.count_transferred for t in run_t), abs=0.5)
    assert abs(p.d_fish) <= 0.5 and abs(p.d_biomass_kg) <= 0.5


def test_fw_intake_preview_equals_the_runs_tranog(env):
    win = _run_window(env, env[2])
    placed = sum(float(t.count_placed)
                 for t in win["tranog_events"] if t.batch_id == "B50")
    p = _preview(env, 22)                                  # W42 fw_to_og B50
    got = sum(t.after_count - t.before_count for t in p.tanks)
    assert placed > 0 and got == pytest.approx(placed, rel=1e-9, abs=0.5)
    assert p.from_fw_count >= got + p.culled_count - 0.5


def test_staged_graded_harvest_preview_names_its_6n_harvest_week(env):
    p = _preview(env, 19, [])                              # W45 graded 36 -> 71 (stage)
    s6 = [s for s in p.sixn if s.tank_id == 71]
    assert len(s6) == 1 and s6[0].harvest_week == "2026-W47"
    assert s6[0].count_after == pytest.approx(35000, abs=0.5)
    assert abs(p.d_fish) <= 0.5 and abs(p.d_biomass_kg) <= 0.5


def test_graded_transfer_preview_conserves_and_equals_the_plan(env):
    from forecast import manual_preview as mp
    from forecast.manual_events import ManualDest, ManualEvent
    state, ctx, evs = env
    base = mp.state_before_event(state, ctx, evs, 2)
    src = base[0].tanks_by_id[46]
    assert not src.is_empty
    empties = [t.tank_id for t in base[0].tanks_by_id.values()
               if t.is_empty and t.type == "OG" and t.system_id[:3] in ("OG3", "OG4", "OG5")]
    assert len(empties) >= 2
    ev = ManualEvent(type="graded_transfer", week=2, from_tank=46,
                     destinations=[ManualDest(tank=empties[0], count=20000.0),
                                   ManualDest(tank=empties[1])], mode="grade")
    p = mp.preview_event(base, ev, ctx, evs)
    assert not p.refused, p.warnings
    assert abs(p.d_fish) <= 0.5 and abs(p.d_biomass_kg) <= 0.5
    big = next(t for t in p.tanks if t.tank_id == empties[0])
    small = next(t for t in p.tanks if t.tank_id == empties[1])
    assert big.after_count == pytest.approx(20000, abs=0.5)
    assert big.after_avg_wt_g > src.avg_wt_g > small.after_avg_wt_g


def test_a_refused_event_is_reported_not_previewed_as_done(env):
    from forecast import manual_preview as mp
    from forecast.manual_events import ManualDest, ManualEvent
    state, ctx, evs = env
    base = mp.state_before_event(state, ctx, evs, 2)
    occupied = [t.tank_id for t in base[0].tanks_by_id.values()
                if not t.is_empty and t.type == "OG" and t.batch_id != "B46"
                and t.system_id[:3] in ("OG3", "OG4", "OG5")]
    ev = ManualEvent(type="og_transfer", week=2, from_tank=46,
                     destinations=[ManualDest(tank=occupied[0])])
    p = mp.preview_event(base, ev, ctx, evs)
    dest = next(t for t in p.tanks if t.tank_id == occupied[0])
    assert dest.after_batch == dest.before_batch           # nothing landed
    assert dest.after_count == dest.before_count


def test_preview_does_not_touch_the_hydrated_state(env):
    from forecast import manual_preview as mp
    state, ctx, evs = env
    snap = {tid: (t.batch_id, t.count, t.avg_wt_g) for tid, t in state.tanks_by_id.items()}
    base = mp.state_before_event(state, ctx, evs[:17] + evs[18:], 5)
    mp.preview_event(base, evs[17], ctx, evs[:17] + evs[18:])
    assert snap == {tid: (t.batch_id, t.count, t.avg_wt_g)
                    for tid, t in state.tanks_by_id.items()}


# ---------- the window summary strip ----------

def test_summary_strip_lists_tank_63_over_the_w46_limit(env):
    from forecast import manual_preview as mp
    from forecast.scenario_io import facility_limits_from_list
    from forecast.sixn import SIXN_ALL_TANKS
    from forecast.time_grid import forecast_week_labels, week_start
    state, ctx, evs = env
    win = _run_window(env, evs, n=8)
    labels = forecast_week_labels(ctx["forecast_start"], 8)
    by_date = {week_start(i, ctx["forecast_start"]): l for i, l in enumerate(labels)}
    moves = []
    for tr in win["transfer_events"]:
        dests = ([tr.pickup_tank_id] if hasattr(tr, "pickup_tank_id")
                 else [a.tank_id for a in tr.destinations])
        moves.append({"week": by_date[tr.event_date], "src": tr.source_tank_id,
                      "dests": dests})
    # Every window move maps to a week on the Monday grid -- the app's old
    # forecast_start + 7*i map labelled only week 1's (2 of 13 on this PR).
    assert len(moves) == len(win["transfer_events"])
    fills = mp.staged_6n_summary(evs, moves, win["batch_locations"], labels,
                                 ctx["forecast_start"],
                                 facility_limits_from_list(W46_LIMIT), ctx["control"])
    row = [f for f in fills if f[0] == "2026-W44" and f[1] == 63]
    assert len(row) == 1
    _w, _t, cnt, hl, scripted, lim = row[0]
    assert hl == "2026-W46" and not scripted and lim == 51500 and cnt > lim
    assert all(f[1] in SIXN_ALL_TANKS for f in fills)


def test_the_editor_grid_maps_every_window_move_to_its_week(env):
    """app._mw_project labels each Transfer by its date. It used
    forecast_start + 7*i, which the Monday week grid no longer matches: on
    this PR (start Thu 2026-10-01) the grid drew arrows for 2 of 13 moves."""
    import sys
    sys.path.insert(0, str(ROOT))
    app = pytest.importorskip("app")
    from forecast.time_grid import forecast_week_labels
    state, ctx, evs = env
    win = _run_window(env, evs, n=6)
    labels = forecast_week_labels(ctx["forecast_start"], 6)
    by_date = app._mw_label_by_date(labels, ctx["forecast_start"])
    trs = win["transfer_events"]
    assert len(trs) == 13
    assert all(by_date.get(tr.event_date) for tr in trs)
    assert all(by_date.get(h.event_date) for h in win["harvest_events"])
