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
