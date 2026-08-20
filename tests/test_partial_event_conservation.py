"""Partially-refused events must not destroy or mis-report fish.

Three defects reproduced 2026-08-20 against the real event classes. Each one
lost fish silently AND passed the hard conservation gate, because that gate
string-scans the workbook for "DROP"/"OVER-PRODUCED" while the audit that does
catch these writes COUNT_DRIFT to a sheet no gate reads.

  1. Transfer.apply — a multi-destination transfer with one leg refused still
     honoured `leaves_source_empty`, emptying the source and ANNIHILATING the
     refused leg's fish, which were physically still in the tank.
  2. TranOGEntry.apply — no accounting of any kind. A refused destination
     dropped that share of the cohort while every downstream consumer went on
     reporting the PLANNED destination sum as delivered.
  3. _apply_og_to_6n — the STARVE freeze iterated REQUESTED destinations and
     tested only `not is_empty`, so a destination refused for holding another
     batch (INV-1) froze that unrelated cohort off-feed and R7-locked it.

State fixtures follow the tiny-TankState pattern of tests/test_transfer_rules.py
(no workbook needed — these must run everywhere).
"""
from __future__ import annotations

from datetime import date

import pytest

from forecast.events import TankAllocation, Transfer, TranOGEntry
from forecast.state import FacilityState, TankState


TODAY = date(2026, 8, 3)


def _mk_state():
    """Entry tier, grow-out, and a 6N pair."""
    return FacilityState(TODAY, [
        TankState("OG1N-1", 1, "OG1N", 200.0, 95.0, 1000.0, "OG"),
        TankState("OG1S-2", 2, "OG1S", 200.0, 95.0, 1000.0, "OG"),
        TankState("OG3N-3", 3, "OG3N", 200.0, 95.0, 1000.0, "OG"),
        TankState("OG4N-4", 4, "OG4N", 200.0, 95.0, 1000.0, "OG"),
        TankState("OG5N-5", 5, "OG5N", 200.0, 95.0, 1000.0, "OG"),
    ])


def _live_fish(state) -> float:
    return sum(t.count for t in state.tanks_by_id.values())


# ---------------------------------------------------------------------------
# 1. Transfer: a refused leg must leave its fish in the source
# ---------------------------------------------------------------------------

class TestTransferPartialRefusal:
    def test_refused_leg_does_not_delete_fish(self):
        """The reproduction: 100k out, one leg of two refused on INV-1.

        Before the fix this emptied the source and deleted 50,000 fish.
        """
        st = _mk_state()
        st.tanks_by_id[3].assign(batch_id="B50", count=100_000, avg_wt_g=2_000.0,
                                 cv_pct=12.0, stage="SW")
        # Destination 5 already holds a DIFFERENT batch -> refused (INV-1).
        st.tanks_by_id[5].assign(batch_id="B77", count=10_000, avg_wt_g=2_500.0,
                                 cv_pct=12.0, stage="SW")
        before = _live_fish(st)

        tr = Transfer(
            batch_id="B50", event_date=TODAY, source_tank_id=3,
            destinations=[
                TankAllocation(tank_id=4, count=50_000, avg_wt_g=2_000.0, cv_pct=12.0),
                TankAllocation(tank_id=5, count=50_000, avg_wt_g=2_000.0, cv_pct=12.0),
            ],
            leaves_source_empty=True,
        )
        warns = tr.apply(st)

        assert tr.count_transferred == pytest.approx(50_000)
        assert tr.count_refused == pytest.approx(50_000)
        # The refused leg's fish are still in the source, not deleted.
        assert st.tanks_by_id[3].count == pytest.approx(50_000)
        assert st.tanks_by_id[3].batch_id == "B50"
        # Facility-wide conservation: nothing created, nothing destroyed.
        assert _live_fish(st) == pytest.approx(before)
        assert any("PARTIALLY APPLIED" in w for w in warns)

    def test_full_success_still_empties_source(self):
        """leaves_source_empty must keep working when every leg is accepted."""
        st = _mk_state()
        st.tanks_by_id[3].assign(batch_id="B50", count=100_000, avg_wt_g=2_000.0,
                                 cv_pct=12.0, stage="SW")
        before = _live_fish(st)

        tr = Transfer(
            batch_id="B50", event_date=TODAY, source_tank_id=3,
            destinations=[
                TankAllocation(tank_id=4, count=60_000, avg_wt_g=2_000.0, cv_pct=12.0),
                TankAllocation(tank_id=5, count=40_000, avg_wt_g=2_000.0, cv_pct=12.0),
            ],
            leaves_source_empty=True,
        )
        tr.apply(st)

        assert tr.count_refused == pytest.approx(0.0)
        assert st.tanks_by_id[3].is_empty
        assert _live_fish(st) == pytest.approx(before)

    def test_all_legs_refused_is_a_no_op(self):
        st = _mk_state()
        st.tanks_by_id[3].assign(batch_id="B50", count=100_000, avg_wt_g=2_000.0,
                                 cv_pct=12.0, stage="SW")
        st.tanks_by_id[4].assign(batch_id="B77", count=1_000, avg_wt_g=2_500.0,
                                 cv_pct=12.0, stage="SW")
        before = _live_fish(st)

        tr = Transfer(
            batch_id="B50", event_date=TODAY, source_tank_id=3,
            destinations=[
                TankAllocation(tank_id=4, count=100_000, avg_wt_g=2_000.0, cv_pct=12.0),
            ],
            leaves_source_empty=True,
        )
        tr.apply(st)

        assert tr.count_transferred == pytest.approx(0.0)
        assert st.tanks_by_id[3].count == pytest.approx(100_000)
        assert _live_fish(st) == pytest.approx(before)


# ---------------------------------------------------------------------------
# 2. TranOGEntry: a refused destination must be counted, not silently dropped
# ---------------------------------------------------------------------------

class TestTranOGEntryAccounting:
    def test_short_stock_is_counted_and_named(self):
        """The reproduction: 1.2M planned, one dest non-empty, 600k stocked.

        Before the fix the event carried no counter at all and downstream
        reported the planned 1.2M as delivered.
        """
        st = _mk_state()
        # Entry tank 2 already holds a remnant -> refused (not empty).
        st.tanks_by_id[2].assign(batch_id="B49", count=500, avg_wt_g=900.0,
                                 cv_pct=12.0, stage="SW")

        ev = TranOGEntry(
            batch_id="B56", event_date=TODAY,
            destinations=[
                TankAllocation(tank_id=1, count=600_000, avg_wt_g=370.0, cv_pct=18.0),
                TankAllocation(tank_id=2, count=600_000, avg_wt_g=370.0, cv_pct=18.0),
            ],
        )
        warns = ev.apply(st)

        assert ev.count_placed == pytest.approx(600_000)
        assert ev.count_refused == pytest.approx(600_000)
        assert any("SHORT-STOCKED" in w for w in warns)
        # The half that landed is real; the half that did not is NOT in the facility.
        assert st.tanks_by_id[1].count == pytest.approx(600_000)
        assert st.tanks_by_id[2].batch_id == "B49"

    def test_clean_entry_reports_full_placement(self):
        st = _mk_state()
        ev = TranOGEntry(
            batch_id="B56", event_date=TODAY,
            destinations=[
                TankAllocation(tank_id=1, count=600_000, avg_wt_g=370.0, cv_pct=18.0),
                TankAllocation(tank_id=2, count=600_000, avg_wt_g=370.0, cv_pct=18.0),
            ],
        )
        warns = ev.apply(st)

        assert ev.count_placed == pytest.approx(1_200_000)
        assert ev.count_refused == pytest.approx(0.0)
        assert not any("SHORT-STOCKED" in w for w in warns)


# ---------------------------------------------------------------------------
# 3. og_to_6n: the STARVE freeze must not touch a batch it did not stock
# ---------------------------------------------------------------------------

class TestSixNFreezeScope:
    def test_refused_dest_does_not_freeze_an_unrelated_batch(self):
        """The reproduction: dests [61 empty, 63 holding B48] froze B48.

        A frozen tank is off-feed AND R7-locked (no transfer out of STARVE),
        so this stranded an unrelated cohort for the rest of the horizon.
        """
        from forecast.manual_events import ManualEvent, ManualDest, _apply_og_to_6n
        from forecast.state import TankState as TS

        st = FacilityState(TODAY, [
            TS("OG3N-10", 10, "OG3N", 200.0, 95.0, 1000.0, "OG"),
            TS("OG6N-61", 61, "OG6N", 200.0, 120.0, 1000.0, "OG"),
            TS("OG6N-63", 63, "OG6N", 200.0, 120.0, 1000.0, "OG"),
        ])
        st.tanks_by_id[10].assign(batch_id="B50", count=80_000, avg_wt_g=4_200.0,
                                  cv_pct=12.0, stage="SW")
        # 63 holds a DIFFERENT batch -> the transfer leg is refused (INV-1).
        st.tanks_by_id[63].assign(batch_id="B48", count=30_000, avg_wt_g=4_500.0,
                                  cv_pct=12.0, stage="SW")

        ev = ManualEvent(
            type="og_to_6n", week=1, from_tank=10, batch="B50",
            destinations=[ManualDest(tank=61, count=40_000),
                          ManualDest(tank=63, count=40_000)],
        )
        _apply_og_to_6n(st, ev, 0, event_date=TODAY)

        # B48 is untouched: still its own batch, still fed, still movable.
        assert st.tanks_by_id[63].batch_id == "B48"
        assert st.tanks_by_id[63].count == pytest.approx(30_000)
        assert st.tanks_by_id[63].stage != "STARVE", (
            "an unrelated batch was frozen off-feed by a refused destination"
        )
        # The leg that DID land is frozen, as depuration requires.
        assert st.tanks_by_id[61].batch_id == "B50"
        assert st.tanks_by_id[61].stage == "STARVE"
