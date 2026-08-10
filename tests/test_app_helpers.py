"""Guards for app.py's pure helpers — the layer that had ZERO test coverage.

app.py is a 5k-line Streamlit monolith and no test imported it, so twelve
confirmed defects in it were found by review and fixed by hand with nothing to
stop them coming back. The helpers exercised here are deliberately pure
(DataFrame/dict in, dict/str out) and need no Streamlit runtime.

Each test names the DEFECT it locks out, not just the behaviour, so a future
change that reintroduces one fails with an explanation rather than a diff.

Importing app.py executes module-level Streamlit calls. That is safe outside a
`streamlit run` context (they warn and no-op), but if it ever stops being safe
these tests skip rather than fail the suite for an unrelated reason.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = pytest.importorskip("app", reason="app.py not importable without Streamlit")

from forecast.manual_events import ManualDest, ManualEvent  # noqa: E402


# --------------------------------------------------------------------------- #
# Limits Save deleted every week outside the displayed horizon
# --------------------------------------------------------------------------- #
def test_limits_outside_the_grid_horizon_are_preserved():
    """DEFECT: Save rewrote limits.yaml wholesale from the grid's columns, but
    the grid shows only the forecast horizon — so a PR starting later silently
    DELETED every earlier week. Measured on the real file: 130 stored, 3 shown,
    127 erased."""
    fl_cur = {(f"2026-W{w:02d}", "max_biomass_kg"): 1000.0 + w for w in range(1, 11)}
    shown = ["2026-W08", "2026-W09", "2026-W10"]

    kept = app._preserved_facility_limits(fl_cur, shown)

    assert {r["week"] for r in kept} == {f"2026-W{w:02d}" for w in range(1, 8)}
    # The grid's own weeks are contributed separately by the caller — they must
    # NOT be duplicated here, or the merged file would carry two of each.
    assert not any(r["week"] in shown for r in kept)
    # Values survive intact, not just the keys.
    assert {r["value"] for r in kept} == {1000.0 + w for w in range(1, 8)}


def test_system_limits_outside_the_grid_horizon_are_preserved():
    sl_cur = {("2026-W01", "OG1N", "max_feed_kg_day"): 3000.0,
              ("2026-W09", "OG1N", "max_feed_kg_day"): 3100.0}
    kept = app._preserved_system_limits(sl_cur, ["2026-W09"])
    assert kept == [{"week": "2026-W01", "system": "OG1N",
                     "metric": "max_feed_kg_day", "value": 3000.0}]


def test_preserving_limits_is_a_no_op_when_the_grid_shows_everything():
    fl_cur = {("2026-W01", "max_biomass_kg"): 1.0}
    assert app._preserved_facility_limits(fl_cur, ["2026-W01"]) == []


# --------------------------------------------------------------------------- #
# fw_to_og size routing died on a grid round-trip
# --------------------------------------------------------------------------- #
def test_dest_token_round_trip_keeps_size_class():
    """DEFECT: to_tanks encoded only `tank[:count]`, so size_class was dropped
    and "Apply to window" planned an unsplit transfer. size_class IS the point
    of fw_to_og — big half to one tank, small half to another."""
    for tank, count, size in [(12, None, "big"), (13, None, "small"),
                              (21, 8000.0, None), (45, 1000.0, "small"),
                              (7, None, None)]:
        d = ManualDest(tank=tank, count=count, size_class=size)
        assert app._parse_dest_token(app._dest_token(d)) == (tank, count, size)


def test_manual_event_round_trip_preserves_routing_and_counts():
    events = [
        ManualEvent(type="fw_to_og", week=3, batch="B60", count=120000,
                    destinations=[ManualDest(tank=12, size_class="big"),
                                  ManualDest(tank=13, size_class="small")]),
        ManualEvent(type="og_transfer", week=5, from_tank=20,
                    destinations=[ManualDest(tank=21, count=8000),
                                  ManualDest(tank=22, count=5000)]),
        ManualEvent(type="harvest", week=7, from_tank=30, count=9000),
    ]
    back = app._rows_to_manual_events(app._manual_events_to_df_rows(events))

    assert len(back) == len(events)
    for before, after in zip(events, back):
        assert after.type == before.type
        assert [(d.tank, d.count, d.size_class) for d in after.destinations] == \
               [(d.tank, d.count, d.size_class) for d in before.destinations]


def test_bare_tokens_still_mean_split_evenly():
    """A bare tank must stay bare — encoding it with a count would silently
    convert "split the whole tank" into a fixed-size move."""
    assert app._dest_token(ManualDest(tank=9)) == "9"
    assert app._parse_dest_token("9") == (9, None, None)


# --------------------------------------------------------------------------- #
# Timeline described every partial move as "whole tank"
# --------------------------------------------------------------------------- #
def test_partial_move_is_not_reported_as_whole_tank():
    """DEFECT: transfer/6N events carry counts PER DESTINATION, so ev.count was
    always None and the timeline claimed "whole tank" for explicit partials —
    misreporting what the plan would do."""
    ev = ManualEvent(type="og_transfer", week=2, from_tank=20,
                     destinations=[ManualDest(tank=21, count=8000),
                                   ManualDest(tank=22, count=5000)])
    assert app._mw_move_amount(ev) == "13,000"


def test_genuine_whole_tank_move_still_says_whole_tank():
    ev = ManualEvent(type="og_transfer", week=2, from_tank=20,
                     destinations=[ManualDest(tank=21), ManualDest(tank=22)])
    assert app._mw_move_amount(ev) == "whole tank"


def test_mixed_explicit_and_bare_destinations_stay_conservative():
    """Bare destinations split an unknown remainder, so no total is claimable."""
    ev = ManualEvent(type="og_transfer", week=2, from_tank=20,
                     destinations=[ManualDest(tank=21, count=8000),
                                   ManualDest(tank=22)])
    assert app._mw_move_amount(ev) == "whole tank"


# --------------------------------------------------------------------------- #
# Blank dynamic-grid rows were saved as junk config
# --------------------------------------------------------------------------- #
def test_never_filled_grid_row_is_dropped():
    """DEFECT: Streamlit appends an empty row on +, and it was saved verbatim —
    a batch literally named "None", or a null tank_id that bricks the NEXT run
    inside precalc."""
    rows = [{"tank_id": 1, "volume_m3": 100.0},
            {"tank_id": None, "volume_m3": None}]
    assert app._clean_rows(rows, "tank_id", "tank") == [rows[0]]


def test_half_filled_grid_row_is_refused_not_silently_dropped():
    """The operator typed something. Dropping it loses their work; saving it
    corrupts the config. Refuse, and say which row."""
    rows = [{"tank_id": 1, "volume_m3": 100.0},
            {"tank_id": None, "volume_m3": 250.0}]
    with pytest.raises(ValueError, match="row 2"):
        app._clean_rows(rows, "tank_id", "tank")


def test_blank_detects_empty_and_whitespace_strings():
    assert app._blank(None) and app._blank("") and app._blank("   ")
    assert not app._blank(0) and not app._blank("x") and not app._blank(False)


# --------------------------------------------------------------------------- #
# Optimizer panel outlived the run it measured
# --------------------------------------------------------------------------- #
def test_result_rid_distinguishes_two_runs():
    """DEFECT: _opt_run survived a later plain Run-forecast, pairing the old
    metrics with a different workbook and still offering it as
    Forecast_optimized.xlsm."""
    a = {"output_path": "/tmp/a.xlsm", "elapsed": 12.0}
    b = {"output_path": "/tmp/b.xlsm", "elapsed": 12.0}
    assert app._result_rid(a) != app._result_rid(b)
    assert app._result_rid(a) == app._result_rid(dict(a))


def test_result_rid_prefers_an_explicit_id_and_tolerates_junk():
    assert app._result_rid({"_rid": "abc", "output_path": "/x"}) == "abc"
    app._result_rid({})      # must not raise
    app._result_rid(None)


def test_res_disk_roundtrip_is_pickle_safe_and_rebuilds_metrics():
    # Board results persist as PLAIN DATA and rebuild Metrics from the
    # CURRENT class — reload-proof by construction (2026-08-07).
    import pickle
    app = pytest.importorskip("app")
    from forecast import optimize as O
    m = O._infeasible_metrics()
    res = {"ok": True, "output_path": "x.xlsm",
           "_score": {"metrics": m, "verdict": {"gate": "PASS"},
                      "harvest": {"zero_weeks": 0}}}
    disk = app._res_for_disk(res)
    assert disk["_score"]["metrics"] is None
    pickle.dumps(disk)                                  # must never raise
    back = app._res_from_disk(disk)
    assert isinstance(back["_score"]["metrics"], O.Metrics)
    assert (back["_score"]["metrics"].weeks_over_harvest_cap
            == m.weeks_over_harvest_cap)
    assert back["_score"]["verdict"] == {"gate": "PASS"}
    # Schema drift (junk fields) -> _score dropped, not a crash: the board
    # re-grades from the workbook on demand.
    bad = {"ok": True,
           "_score": {"metrics": None, "_metrics_plain": {"nope": 1}}}
    assert "_score" not in app._res_from_disk(bad) or \
        app._res_from_disk(bad)["_score"].get("metrics") is not None


# --------------------------------------------------------------------------- #
# Provenance labels — a replayed cached result must SAY it's a replay
# --------------------------------------------------------------------------- #
# DEFECT CLASS (2026-08-10 stale-cache incident): the board replayed old
# engine legs and only pickle-spelunking revealed it. Every displayed result
# now carries a compact provenance caption; these lock its formatting.
def test_fmt_ts_minutes_shortens_iso_and_survives_junk():
    assert app._fmt_ts_minutes("2026-08-10T11:08:03") == "2026-08-10 11:08"
    assert app._fmt_ts_minutes("2026-08-10 11:08:03") == "2026-08-10 11:08"
    assert app._fmt_ts_minutes(None) == ""
    assert app._fmt_ts_minutes("") == ""
    assert app._fmt_ts_minutes("junk") == ""
    assert app._fmt_ts_minutes(12345) == ""


def test_provenance_fresh_run_names_time_schema_and_inputs():
    res = {"run_ts": "2026-08-10T11:08:03",
           "_score": {"schema": "metrics-v2-window-weeks-excluded"}}
    line = app._provenance_line(res, sig="1a2b3c4d5e6f", fresh=True)
    assert "● fresh run 2026-08-10 11:08" in line
    assert "graded metrics-v2-window-weeks-excluded" in line
    assert "inputs 1a2b3c4d" in line          # 8-char prefix, not the full md5
    assert "1a2b3c4d5e6f" not in line


def test_provenance_cache_replay_is_labelled_as_a_replay():
    res = {"run_ts": "2026-08-10T11:08:03", "_score": {"schema": "s"}}
    line = app._provenance_line(res, sig="feedbeef00", fresh=False)
    assert line.startswith("⟲ cached run of 2026-08-10 11:08")
    # A pre-provenance cached leg (no run_ts) must admit it, not invent a time.
    old = app._provenance_line({}, fresh=False)
    assert "time not recorded" in old


def test_provenance_regrade_says_engine_reused_judgement_redone():
    """The drop_stale_grades path: engine run reused, verdict recomputed under
    the CURRENT rules — the label must say both times, per the operator's
    'engine run 11:08, re-graded under current rules' requirement."""
    res = {"run_ts": "2026-08-10T11:08:00",
           "_graded_ts": "2026-08-10T13:42:00",
           "_regraded": True,
           "_score": {"schema": "metrics-v2-window-weeks-excluded"}}
    line = app._provenance_line(res, sig="abc", fresh=False)
    assert "cached run of 2026-08-10 11:08" in line
    assert "re-graded under current rules 2026-08-10 13:42" in line
    assert "(metrics-v2-window-weeks-excluded)" in line
    # No duplicate plain "graded <schema>" claim next to the re-grade note.
    assert "graded metrics" not in line.replace("re-graded", "")


def test_provenance_unknown_origin_claims_nothing():
    """fresh=None (no session runtime / legacy result) must not claim either
    'fresh' or 'cached' — an honest label beats a guessed one."""
    line = app._provenance_line({"run_ts": "2026-08-10T09:00:00"}, fresh=None)
    assert "fresh" not in line and "cached" not in line
    assert line.startswith("run 2026-08-10 09:00")
    assert app._provenance_line({}, fresh=None).startswith(
        "run time not recorded")


def test_ensure_board_score_regrade_flag_round_trips_disk():
    """_regraded/_graded_ts/run_ts are plain keys — they must survive the
    _res_for_disk/_res_from_disk pickle round-trip with the score."""
    import pickle
    from forecast import optimize as O
    res = {"ok": True, "run_ts": "2026-08-10T11:08:00",
           "_graded_ts": "2026-08-10T13:42:00", "_regraded": True,
           "_score": {"metrics": O._infeasible_metrics(), "schema": "s"}}
    back = app._res_from_disk(pickle.loads(pickle.dumps(app._res_for_disk(res))))
    assert back["run_ts"] == "2026-08-10T11:08:00"
    assert back["_regraded"] is True and back["_graded_ts"]


# --------------------------------------------------------------------------- #
# Compare board: a PARTIAL (fish-dropping) plan could win a grading lens
# --------------------------------------------------------------------------- #
def test_board_lens_pool_excludes_partial_plans():
    """DEFECT (2026-07-13 audit): lens eligibility only checked the Conserves
    gate, so a PARTIAL plan (batches dropped for lack of space, gate='PARTIAL')
    could be crowned 'Best welfare' — unplaced fish can't be crowded, so every
    quality metric flatters the plan that reared fewer fish."""
    def _res(conserves, placed):
        return {"_score": {"gates": {"Conserves": conserves,
                                     "Fully placed": placed,
                                     "No empty week": True,
                                     "Under cap": True}}}
    scored = {"full": _res(True, True),
              "partial": _res(True, False),      # conserves per its OWN proof...
              "lossy": _res(False, True)}
    pool = app._board_lens_pool(scored)
    assert set(pool) == {"full"}, "only the fully-placed conserving plan may win"

    # Nothing passes -> fall back to the whole board (cards still render).
    scored_bad = {"a": _res(False, True), "b": _res(True, False)}
    assert set(app._board_lens_pool(scored_bad)) == {"a", "b"}
