"""The board engine-leg cache must never replay a stale leg, and stored
grades must never outlive the metric semantics they were computed under.

2026-08-10 live incident: the operator edited the scenario (added a W33
manual harvest) and updated the metrics code (METRICS_SCHEMA v2), then
re-ran the tuned tournament. The TUNED variants ran fresh (their cache keys
fold in scenario content + schema), but the five STOCK engine legs were
hydrated from %LOCALAPPDATA%\\as_planner\\result_cache\\board_<method>.pkl
and replayed: their key used an mtime PROXY for config/scenario content
(which the edit dodged) and no schema, so the board judged the stock
methods on a pre-edit scenario with pre-fix zero_weeks — a stale-cache
artifact reported as 'stock hard-fails no_empty_week'.

Two independent invalidation axes now enforced:
  * engine identity (PR content + config/scenario CONTENT + method):
    mismatch => the leg is ABSENT, re-run (analysis.board_leg_current);
  * grading identity (METRICS_SCHEMA stamped inside _score/_ana_rows):
    mismatch => grades dropped and re-derived from the cached workbook,
    engine output reused (analysis.drop_stale_grades).
"""
import os

from forecast import analysis as ana


# --------------------------------------------------------------------------- #
# board_leg_current — a leg under sig A is never returned for sig B
# --------------------------------------------------------------------------- #

def test_leg_with_matching_sig_is_current():
    entry = {"sig": "sig-A", "res": {"ok": True, "output_path": "x.xlsx"}}
    assert ana.board_leg_current(entry, "sig-A") is True


def test_leg_stored_under_sig_a_not_returned_for_sig_b():
    entry = {"sig": "sig-A", "res": {"ok": True, "output_path": "x.xlsx"}}
    assert ana.board_leg_current(entry, "sig-B") is False


def test_old_format_leg_without_sig_is_ignored_gracefully():
    # Pre-fix cache files carry no stored sig — stale by definition, no crash.
    assert ana.board_leg_current({"res": {"ok": True}}, "sig-B") is False


def test_missing_or_malformed_legs_are_ignored_gracefully():
    assert ana.board_leg_current(None, "sig-A") is False
    assert ana.board_leg_current({}, "sig-A") is False
    assert ana.board_leg_current({"sig": "sig-A"}, "sig-A") is False   # no res
    assert ana.board_leg_current({"sig": "sig-A", "res": "junk"}, "sig-A") is False
    assert ana.board_leg_current("not-a-dict", "sig-A") is False


def test_leg_round_trip_through_disk_cache(tmp_path):
    """The real flow: legs pickled per method, hydrated with cache_load_all,
    then sig-checked — an old-format leg and a corrupt file are both simply
    not current, while a matching leg replays."""
    good = {"sig": "sig-A", "res": {"ok": True, "output_path": "a.xlsx"}}
    old = {"res": {"ok": True, "output_path": "b.xlsx"}}       # pre-fix format
    ana.cache_save("board_controller", good, cache_dir=tmp_path)
    ana.cache_save("board_global_lp", old, cache_dir=tmp_path)
    (tmp_path / "board_broken.pkl").write_bytes(b"\x00not a pickle")

    loaded = ana.cache_load_all(cache_dir=tmp_path, prefix="board_")
    assert "board_broken" not in loaded                        # corrupt-skip
    assert ana.board_leg_current(loaded["board_controller"], "sig-A") is True
    assert ana.board_leg_current(loaded["board_controller"], "sig-B") is False
    assert ana.board_leg_current(loaded["board_global_lp"], "sig-A") is False


# --------------------------------------------------------------------------- #
# drop_stale_grades — schema bump re-grades, engine output is reused
# --------------------------------------------------------------------------- #

def _res(schema):
    return {
        "ok": True, "output_path": "out.xlsx", "output_bytes": b"WORKBOOK",
        "elapsed": 42.0, "_label": "Controller",
        "_score": {"gates": {"No empty week": False}, "harvest": {"zero_weeks": 1},
                   "schema": schema},
        "_ana_rows": {"rid": "r1", "schema": schema, "rows": [1, 2]},
        "_ana_density": {"rid": "r1", "schema": schema, "review": None},
    }


def test_stale_schema_grades_dropped_engine_output_kept():
    res = _res("metrics-v1-old")
    assert ana.drop_stale_grades(res, "metrics-v2-new") is True
    # Grading artifacts gone => consumers re-grade from the cached workbook…
    assert "_score" not in res
    assert "_ana_rows" not in res
    assert "_ana_density" not in res
    # …while the ENGINE output is untouched (no re-run needed).
    assert res["ok"] is True
    assert res["output_path"] == "out.xlsx"
    assert res["output_bytes"] == b"WORKBOOK"
    assert res["elapsed"] == 42.0


def test_current_schema_grades_are_kept():
    res = _res("metrics-v2-new")
    assert ana.drop_stale_grades(res, "metrics-v2-new") is False
    assert res["_score"]["harvest"] == {"zero_weeks": 1}
    assert res["_ana_rows"]["rows"] == [1, 2]


def test_unstamped_grades_are_stale_by_definition():
    # Grades from before schema-stamping existed carry no "schema" key.
    res = _res(None)
    for part in ("_score", "_ana_rows", "_ana_density"):
        res[part].pop("schema", None)
    assert ana.drop_stale_grades(res, "metrics-v2-new") is True
    assert "_score" not in res and "_ana_rows" not in res


def test_no_grades_is_a_noop():
    res = {"ok": True, "output_path": "out.xlsx"}
    assert ana.drop_stale_grades(res, "metrics-v2-new") is False
    assert res == {"ok": True, "output_path": "out.xlsx"}
    assert ana.drop_stale_grades(None, "s") is False           # junk-tolerant


def test_score_none_after_failed_grading_is_noop():
    # A transiently failed grading stores _score=None + _score_err; that is
    # "not graded yet", not "stale" — the ensure path re-grades it anyway.
    res = {"ok": True, "_score": None, "_score_err": "boom"}
    assert ana.drop_stale_grades(res, "s") is False
    assert res["_score_err"] == "boom"


# --------------------------------------------------------------------------- #
# dirs_fingerprint — CONTENT-keyed, immune to the mtime pathology
# --------------------------------------------------------------------------- #

def test_content_change_with_preserved_mtime_changes_fingerprint(tmp_path):
    """THE 2026-08-10 hole: an edit whose mtime the scan doesn't see. Content
    hashing catches it even when the timestamp is byte-identical."""
    d = tmp_path / "scenario"
    (d / "manual_events").mkdir(parents=True)
    f = d / "manual_events" / "2026-07-29.yaml"
    f.write_text("events: [w31, w32]")
    st0 = f.stat()
    f1 = ana.dirs_fingerprint([d])

    f.write_text("events: [w31, w32, w33-harvest]")
    os.utime(f, ns=(st0.st_atime_ns, st0.st_mtime_ns))   # mtime dodge
    assert f.stat().st_mtime_ns == st0.st_mtime_ns
    f2 = ana.dirs_fingerprint([d])
    assert f1 != f2


def test_fingerprint_stable_when_nothing_changed(tmp_path):
    d = tmp_path / "config"
    d.mkdir()
    (d / "control.yaml").write_text("horizon_weeks: 52")
    assert ana.dirs_fingerprint([d]) == ana.dirs_fingerprint([d])


def test_fingerprint_excludes_overlay_files(tmp_path):
    d = tmp_path / "config"
    d.mkdir()
    (d / "control.yaml").write_text("a: 1")
    (d / "targets.yaml").write_text("t: 1")
    f1 = ana.dirs_fingerprint([d], exclude={"targets.yaml"})
    (d / "targets.yaml").write_text("t: 2")                # overlay edit
    assert ana.dirs_fingerprint([d], exclude={"targets.yaml"}) == f1
    (d / "control.yaml").write_text("a: 2")                # engine-input edit
    assert ana.dirs_fingerprint([d], exclude={"targets.yaml"}) != f1


def test_fingerprint_missing_dir_is_ok(tmp_path):
    assert isinstance(ana.dirs_fingerprint([tmp_path / "nope"]), str)


# --------------------------------------------------------------------------- #
# _score survives the disk round-trip WITH its schema stamp
# --------------------------------------------------------------------------- #

def test_schema_stamp_survives_pickling(tmp_path):
    entry = {"sig": "sig-A", "res": _res("metrics-v2-new")}
    ana.cache_save("board_x", entry, cache_dir=tmp_path)
    back = ana.cache_load_all(cache_dir=tmp_path, prefix="board_x")["board_x"]
    assert back["res"]["_score"]["schema"] == "metrics-v2-new"
    # And a later schema bump still invalidates the round-tripped grade.
    assert ana.drop_stale_grades(back["res"], "metrics-v3") is True
