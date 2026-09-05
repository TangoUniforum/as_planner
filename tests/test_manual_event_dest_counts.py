"""`count` is the TOTAL to move — including what the explicit destinations take.

DEFECT (2026-09-04). `_rows_to_manual_events` splits the `count` column across
the destinations that have no explicit count of their own:

    bare = [t for t, c, _s in specs if c is None]
    per_bare = (count / len(bare)) if (bare and count is not None) else None

`count` is not reduced by the explicit allocations, so `count=100000` with
`to_tanks="43:40000,45"` builds 40,000 + 100,000 and the engine moves 140,000 —
logged as MANUAL EVENT OK. Reproduced end to end against a real hydrated PR.

The two layers disagreed about what a BARE destination means:

  * the engine (`manual_events._apply_og_transfer`) reads no `count` at all for
    these types — explicit counts are honoured and bare destinations share the
    remainder of the SOURCE TANK ("single None dest => all remaining");
  * the editor materialises a bare destination into an EXPLICIT count taken
    from the `count` column — which is precisely what stops the engine's
    remainder logic from ever running.

Downstream protection exists but is an accident of population, not a contract:
`_apply_og_transfer` refuses when the explicit total exceeds the source tank, so
the over-request is caught only when the tank happens to be smaller than the
over-request. With a big enough source it applies in full and silently.

The negative controls are load-bearing. All-bare and all-explicit rows are the
only shapes the operator's real files use (verified: zero mixed destinations
across all three), so those must come out byte-identical or this "fix" would
rewrite working plans.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = pytest.importorskip("app", reason="app.py not importable without Streamlit")


def _row(to_tanks, count, typ="og_transfer"):
    return {"week": 1, "type": typ, "batch": "", "from_tank": 41,
            "to_tanks": to_tanks, "count": count, "mode": "", "notes": ""}


def _dests(to_tanks, count, typ="og_transfer"):
    ev = app._rows_to_manual_events([_row(to_tanks, count, typ)])[0]
    return {d.tank: d.count for d in ev.destinations}


def test_a_bare_destination_takes_the_remainder_not_the_whole_total():
    """THE DEFECT: 40,000 explicit + 100,000 bare = 140,000 against a stated
    total of 100,000."""
    d = _dests("43:40000,45", 100000)
    assert d == {43: 40000.0, 45: 60000.0}, (
        "the bare destination took the whole `count` instead of what is left "
        "after the explicit allocations")
    assert sum(d.values()) == 100000.0


def test_several_bare_destinations_share_the_remainder():
    d = _dests("43:40000,45,47", 100000)
    assert d == {43: 40000.0, 45: 30000.0, 47: 30000.0}
    assert sum(d.values()) == 100000.0


def test_og_to_6n_is_allocated_the_same_way():
    """Same branch, same contract — a 6N staging over-request is worse, because
    the drain that follows is what the plant has to process."""
    d = _dests("63:10000,65", 30000, typ="og_to_6n")
    assert d == {63: 10000.0, 65: 20000.0}


def test_explicit_counts_over_the_total_are_refused_by_name():
    """Silently moving 180,000 on a stated 100,000 is the same defect without
    a bare destination to hide behind. The grid's Apply already catches
    ValueError and leaves the working set unchanged."""
    with pytest.raises(ValueError) as e:
        app._rows_to_manual_events([_row("43:90000,45:90000", 100000)])
    msg = str(e.value)
    assert "180,000" in msg or "180000" in msg
    assert "100,000" in msg or "100000" in msg


def test_a_duplicate_destination_is_refused():
    """`43:40000,43:40000` built two allocations for one tank. Whatever the
    operator meant, the plan is not it."""
    with pytest.raises(ValueError) as e:
        app._rows_to_manual_events([_row("43:40000,43:40000", 100000)])
    assert "43" in str(e.value)


# --------------------------------------------------------------------------- #
# NEGATIVE CONTROLS — the shapes the operator's real files actually use.
# --------------------------------------------------------------------------- #
def test_all_bare_is_unchanged():
    assert _dests("43,45", 100000) == {43: 50000.0, 45: 50000.0}


def test_all_explicit_is_unchanged():
    assert _dests("43:40000,45:60000", 100000) == {43: 40000.0, 45: 60000.0}


def test_bare_destinations_with_no_total_still_defer_to_the_engine():
    """`count: null` with bare destinations is what every real file uses: the
    editor must leave the counts as None so the ENGINE splits the source
    tank's remainder. Filling them in here would silently re-plan every stored
    operation."""
    assert _dests("43,45", None) == {43: None, 45: None}


def test_a_single_bare_destination_with_no_total_means_all_remaining():
    assert _dests("63", None, typ="og_to_6n") == {63: None}


def test_the_round_trip_is_stable():
    """editor -> events -> grid rows -> events must not drift."""
    evs = app._rows_to_manual_events([_row("43:40000,45", 100000)])
    rows = app._manual_events_to_df_rows(evs)
    again = app._rows_to_manual_events(rows)
    assert ({d.tank: d.count for d in evs[0].destinations}
            == {d.tank: d.count for d in again[0].destinations})


def test_a_duplicate_destination_is_refused_for_every_type():
    """The duplicate check sits where the destination tokens are parsed, so it
    covers fw_to_og and graded_harvest too — a tank named twice is a data error
    whatever the event is."""
    for typ in ("fw_to_og", "graded_harvest", "og_to_6n"):
        with pytest.raises(ValueError):
            app._rows_to_manual_events([_row("43,43", 100000, typ=typ)])


def test_every_stored_event_file_survives_a_round_trip_unchanged():
    """THE REGRESSION GUARD THAT MATTERS. These are the operator's real
    operations for three PRs. Whatever this fix does, loading them, rendering
    them into the grid and parsing them back must reproduce the SAME
    destinations and counts — otherwise a plan that has already been run would
    silently re-plan."""
    from pathlib import Path

    from forecast.manual_events import load_manual_events

    root = Path(__file__).resolve().parent.parent
    d = root / "scenario" / "manual_events"
    files = sorted(d.glob("*.yaml")) if d.exists() else []
    if not files:
        pytest.skip("no stored manual-event files in this checkout")
    checked = 0
    for f in files:
        evs = load_manual_events(str(root / "scenario"), pr_closing=f.stem)
        if not evs:
            continue
        rows = app._manual_events_to_df_rows(evs)
        again = app._rows_to_manual_events(rows)
        assert len(again) == len(evs), f.name
        for a, b in zip(evs, again):
            assert a.type == b.type and a.week == b.week, f.name
            assert a.from_tank == b.from_tank, f.name
            assert ([(d_.tank, d_.count, d_.size_class) for d_ in a.destinations]
                    == [(d_.tank, d_.count, d_.size_class) for d_ in b.destinations]), (
                f"{f.name}: destinations changed under the round trip")
        checked += 1
    assert checked, "no file yielded any events — the guard checked nothing"
