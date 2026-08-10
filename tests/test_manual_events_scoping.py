"""Manual events are PR-SPECIFIC (operator requirement 2026-08-07).

Events describe one PR's starting reality — "week 2, harvest tank 45" means
something different under another PR. These tests pin the scoping contract:
per-PR files keyed by closing date, no bleed between PRs, and the legacy
shared file honored only until the first per-PR save exists.
"""
from datetime import date, datetime

from forecast.manual_events import (
    ManualEvent, dump_manual_events, events_file_for, load_manual_events)


def _ev(week=1, tank=45, count=100.0):
    return ManualEvent(type="harvest", week=week, from_tank=tank, count=count)


def test_per_pr_roundtrip_and_key_normalization(tmp_path):
    dump_manual_events(tmp_path, [_ev()], pr_closing=date(2026, 7, 31))
    assert events_file_for(tmp_path, "2026-07-31").exists()
    # date, datetime, and ISO-string closings all resolve to the same file.
    for key in (date(2026, 7, 31), datetime(2026, 7, 31, 0, 0), "2026-07-31"):
        got = load_manual_events(tmp_path, pr_closing=key)
        assert len(got) == 1 and got[0].from_tank == 45


def test_no_bleed_between_prs(tmp_path):
    # The bug this feature kills: PR-A's operations must NEVER surface
    # under PR-B.
    dump_manual_events(tmp_path, [_ev(tank=45)], pr_closing="2026-07-31")
    assert load_manual_events(tmp_path, pr_closing="2026-08-07") == []
    dump_manual_events(tmp_path, [_ev(tank=12)], pr_closing="2026-08-07")
    a = load_manual_events(tmp_path, pr_closing="2026-07-31")
    b = load_manual_events(tmp_path, pr_closing="2026-08-07")
    assert a[0].from_tank == 45 and b[0].from_tank == 12


def test_legacy_fallback_only_pre_migration(tmp_path):
    # Old shared file + NO per-PR files yet -> honored (nothing already
    # scripted is lost)...
    dump_manual_events(tmp_path, [_ev(tank=45)])          # legacy write
    got = load_manual_events(tmp_path, pr_closing="2026-07-31")
    assert len(got) == 1
    # ...but the FIRST per-PR save ends the legacy era: another PR now
    # starts clean instead of inheriting the shared file.
    dump_manual_events(tmp_path, got, pr_closing="2026-07-31")
    assert load_manual_events(tmp_path, pr_closing="2026-08-07") == []
    # The migrated PR itself still gets its events (from its own file now).
    assert len(load_manual_events(tmp_path, pr_closing="2026-07-31")) == 1


def test_no_closing_keeps_legacy_behavior(tmp_path):
    assert load_manual_events(tmp_path) == []
    dump_manual_events(tmp_path, [_ev()])
    assert len(load_manual_events(tmp_path)) == 1


def test_manual_lines_categorized_loudly_in_validation_log():
    # HARD RULE (operator-hit 2026-08): manual-window outcomes must be
    # impossible to miss in the ValidationLog — refusals as ERROR rows,
    # executions as INFO rows, window lints as WARNING rows. Silent no-ops
    # are forbidden.
    from openpyxl import Workbook
    from forecast.excel_io import write_validation_log
    wb = Workbook()
    write_validation_log(wb, invariant_warnings=[
        "MANUAL EVENT REFUSED — 2026-W33: graded_harvest #5 from tank #32 "
        "did NOT execute; the fish stay where they were. Reason(s): pickup "
        "OG4N-45 not empty (holds B41)",
        "MANUAL EVENT OK — 2026-W32: graded_harvest #4 staged the biggest "
        "32,000 fish of B42 from tank #55 into 6N tank #65 to purge",
        "MANUAL WINDOW — 2026-W33 schedules NO harvest: window weeks execute "
        "only your scripted events",
    ])
    rows = [[c.value for c in r] for r in wb["ValidationLog"].iter_rows()]
    cats = {str(r[2])[:20]: str(r[1]) for r in rows if r and len(r) >= 3
            and isinstance(r[2], str) and r[2].startswith("MANUAL")}
    assert cats["MANUAL EVENT REFUSED"] == "ERROR - Manual window (REFUSED)"
    assert cats["MANUAL EVENT OK — 20"] == "INFO - Manual window (executed)"
    assert cats["MANUAL WINDOW — 2026"] == "WARNING - Manual window"
