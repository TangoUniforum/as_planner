"""Manual-override-window semantics for the GLOBAL planning layer + the
handoff lint.

The window weeks (1..N) are operator-scripted truth: ONLY scripted events
happen there (plus biology). These tests pin:

  1. GLOBAL WINDOW PURITY — after a manual window, L1 contributes NO implicit
     pre-start 6N staging (the steady-fill prime is off), so the earliest
     unscripted harvest is the purge hold's length after the handoff.
  2. RELEASE-SCHEDULE FIDELITY — the window-close 6N contents release exactly
     when their schedule says (scripted stagings honor the hold), and the
     legacy purge_inflight spread is REPLACED, not added, when a schedule is
     given.
  3. NO-WINDOW EQUIVALENCE — the new kwargs at their defaults are byte-
     identical to not passing them at all.
  4. THE LINT DETECTOR — forecast.manual_window.dark_handoff_weeks, the pure
     function behind the editor's "window drains 6N without restaging"
     warning (no Streamlit required).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from forecast import global_planner_poc as gpp
from forecast.config_io import load_config
from forecast.global_planner_poc import _PURGE_HOLD_WEEKS
from forecast.manual_events import ManualDest, ManualEvent
from forecast.manual_window import dark_handoff_weeks
from forecast.scenario_io import load_batches

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    not (ROOT / "config").is_dir() or not (ROOT / "scenario").is_dir(),
    reason="config/ + scenario/ not seeded",
)


@pytest.fixture(scope="module")
def cfg():
    control, tables, facility = load_config(str(ROOT / "config"))
    batches = load_batches(str(ROOT / "scenario"))
    control.horizon_weeks = 40          # plenty of purge-mode weeks, fast
    return control, tables, facility, batches


# A big in-flight OG cohort ABOVE the sales weight, so week 0 has harvest-
# eligible grow-out mass for the steady-fill prime to draw from. The id must
# exist in scenario/batches.yaml (build_seeds only seeds known batches).
def _inflight(batches):
    bid = batches[0].batch_id
    return {bid: (200_000.0, 4_500.0, 16.0)}


def _plan(cfg, **kw):
    control, tables, facility, batches = cfg
    kw.setdefault("inflight_og", _inflight(batches))
    kw.setdefault("model_purge_hold", True)
    return gpp.plan(batches, tables, control, facility, **kw)


def _harvest_by_week(res):
    out: dict[int, float] = {}
    for r in res.envelope:
        out[r.week] = out.get(r.week, 0.0) + r.count
    return out


class TestGlobalWindowPurity:

    def test_no_window_primes_the_handoff_weeks(self, cfg):
        """Baseline: WITHOUT a window the steady-fill prime releases harvest in
        the first hold weeks (the modeled pre-start staging). This is the
        behavior the window flag must switch OFF — if this stops holding, the
        purity test below stops meaning anything."""
        hv = _harvest_by_week(_plan(cfg))
        assert all(hv.get(w, 0.0) > 0 for w in range(_PURGE_HOLD_WEEKS))

    def test_window_yields_no_staging_during_window_weeks(self, cfg):
        """After a manual window, nothing releases before the purge hold has
        run from the handoff: the plan's first possible staging is week 0 (the
        handoff), so the first harvest is week _PURGE_HOLD_WEEKS. Anything
        earlier would be implicit staging during operator-scripted weeks."""
        res = _plan(cfg, manual_window_weeks=2)
        hv = _harvest_by_week(res)
        for w in range(_PURGE_HOLD_WEEKS):
            assert hv.get(w, 0.0) == 0.0, (
                f"week {w} harvests {hv.get(w):,.0f} fish — implicit staging "
                f"during the manual window")
        assert hv.get(_PURGE_HOLD_WEEKS, 0.0) > 0

    def test_schedule_releases_exactly_when_it_says(self, cfg):
        """The window-close 6N contents release at their scheduled week — a
        scripted staging in the last window week is NOT releasable at the
        handoff (its hold has not run)."""
        control, _t, _f, batches = cfg
        bid = batches[1].batch_id
        sched = [{"batch_id": bid, "count": 10_000.0,
                  "biomass_kg": 45_000.0, "avg_wt_g": 4_500.0,
                  "release_week": 1}]
        res = _plan(cfg, manual_window_weeks=2, purge_release_schedule=sched)
        wk0 = [r for r in res.envelope if r.week == 0]
        wk1 = [r for r in res.envelope if r.week == 1 and r.batch_id == bid]
        assert not wk0, "nothing is releasable at the handoff in this scenario"
        assert len(wk1) == 1 and wk1[0].count == pytest.approx(10_000.0)

    def test_schedule_replaces_the_purge_inflight_spread(self, cfg):
        """When a schedule is given, purge_inflight must NOT also seed the
        buffer (that would double-release the same 6N fish)."""
        control, _t, _f, batches = cfg
        bid = batches[1].batch_id
        other = batches[2].batch_id
        sched = [{"batch_id": bid, "count": 10_000.0,
                  "biomass_kg": 45_000.0, "avg_wt_g": 4_500.0,
                  "release_week": 1}]
        res = _plan(cfg, manual_window_weeks=2, purge_release_schedule=sched,
                    purge_inflight={other: (5_000.0, 5_000.0)})
        early = {r.batch_id for r in res.envelope
                 if r.week < _PURGE_HOLD_WEEKS}
        assert other not in early


class TestNoWindowEquivalence:

    def test_new_kwargs_at_defaults_are_byte_identical(self, cfg):
        """Passing manual_window_weeks=0 / purge_release_schedule=None must be
        exactly the plan produced by not passing them at all."""
        a = _plan(cfg)
        b = _plan(cfg, manual_window_weeks=0, purge_release_schedule=None)
        assert ([(r.week, r.batch_id, r.count, r.biomass_kg)
                 for r in a.envelope]
                == [(r.week, r.batch_id, r.count, r.biomass_kg)
                    for r in b.envelope])
        assert ([(t.week, t.standing_biomass_kg, t.harvested_kg)
                 for t in a.trace]
                == [(t.week, t.standing_biomass_kg, t.harvested_kg)
                    for t in b.trace])


# ---------------------------------------------------------------------------
# The lint's detector — pure, no Streamlit, no biology.
# ---------------------------------------------------------------------------

def _harvest(week, tank, count=None):
    return ManualEvent(type="harvest", week=week, from_tank=tank, count=count)


def _to_6n(week, src, dest, count):
    return ManualEvent(type="og_to_6n", week=week, from_tank=src,
                       destinations=[ManualDest(tank=dest, count=count)])


class TestDarkHandoffDetector:

    def test_drained_window_flags_both_handoff_weeks(self):
        # The measured 7.29.26 shape: 2-week window harvests both stocked 6N
        # tanks, restages nothing -> handoff (week 3) AND week 4 are dark.
        dark = dark_handoff_weeks({65: 30_000, 61: 25_000},
                                  [_harvest(1, 65), _harvest(2, 61)])
        assert dark == [3, 4]

    def test_surviving_start_inventory_covers_the_handoff(self):
        # A third stocked 6N tank the window does not touch -> releasable at
        # the handoff (its hold was served sitting through the window).
        dark = dark_handoff_weeks({65: 30_000, 61: 25_000, 63: 20_000},
                                  [_harvest(1, 65), _harvest(2, 61)])
        assert dark == []

    def test_restaging_in_time_covers_the_handoff(self):
        # 2-week window; Send-to-6N in week 1 releases at week 1+hold=3, which
        # IS the handoff -> covered.
        dark = dark_handoff_weeks(
            {65: 30_000},
            [_harvest(1, 65), _to_6n(1, 10, 63, 15_000)],
            window_weeks=2)
        assert dark == []

    def test_late_restaging_leaves_the_handoff_dark(self):
        # Send-to-6N in week 2 releases at week 4 -> week 3 is still dark.
        dark = dark_handoff_weeks(
            {65: 30_000},
            [_harvest(1, 65), _to_6n(2, 10, 63, 15_000)])
        assert dark == [3]

    def test_unknown_staged_count_still_counts_as_presence(self):
        # count=None ("whole tank") — the detector can't size it here, but it
        # is a positive presence, which is all the zero-check needs.
        dark = dark_handoff_weeks(
            {65: 30_000},
            [_harvest(1, 65), _to_6n(1, 10, 63, None)],
            window_weeks=2)
        assert dark == []

    def test_partial_6n_harvest_leaves_the_remainder_releasable(self):
        dark = dark_handoff_weeks({65: 30_000},
                                  [_harvest(1, 65, count=10_000)])
        assert dark == []

    def test_empty_6n_with_no_restaging_is_dark(self):
        # Start-empty 6N + a window that touches only grow-out: nothing can
        # release at the handoff either way — the lint should say so.
        ev = ManualEvent(type="og_transfer", week=1, from_tank=10,
                         destinations=[ManualDest(tank=11)])
        assert dark_handoff_weeks({}, [ev]) == [2, 3]

    def test_no_events_is_never_flagged(self):
        assert dark_handoff_weeks({65: 10_000}, []) == []
