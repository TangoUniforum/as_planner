"""entry_tier_exit -- when fish leave the entry tier (OG1/2).

Operator ruling 2026-10-02: "1kg is the point that they cannot move between
1/2; after 1kg they only can move from 1/2 to the other, they cannot move
within 1/2 -- that's the rule." 1 kg bans moves WITHIN OG1/2 (R3); it never
forces fish out (R6). The coordinator's EVT_MIGRATE nevertheless swapped every
OG1/2 tank of a >= 1 kg batch for OG3-6 the first week a grow-out tank was
free, and on the 9.30 PR that left 4-7 of the 12 entry tanks idle for weeks.

`when_needed` (the default since 2026-10-02) frees an
entry tank only for a TranOG arrival, moving the biggest fish forward;
`at_1kg` keeps the historical behaviour byte for byte.
"""
from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import yaml

from forecast.config_io import control_from_dict, load_control, load_facility_config
from forecast.models import ControlParams
from forecast.precalc import (BatchLifecycle, BatchWeekFact,
                              _build_facility_assignment_plan)
from forecast.state import FacilityState
from forecast.tiers import (ENTRY_EXIT_AT_1KG, ENTRY_EXIT_WHEN_NEEDED,
                            ENTRY_SYSTEMS, entry_exit_mode)

ROOT = Path(__file__).resolve().parent.parent
FACILITY = load_facility_config(ROOT / "config")
CONTROL = load_control(ROOT / "config")
SYS = {t.tank_id: t.system_id for t in FACILITY.tanks if t.type == "OG"}
OG12 = sorted(t for t, s in SYS.items() if s in ENTRY_SYSTEMS)
OG36 = sorted(t for t, s in SYS.items() if s not in ENTRY_SYSTEMS)
WEEKS = [f"2027-W{i:02d}" for i in range(1, 9)]
START = date(2027, 1, 4)
ARRIVAL = "2027-W04"
OG36_ALL = ("OG3N", "OG3S", "OG4N", "OG4S", "OG5N", "OG5S", "OG6N", "OG6S")


# ---------- the setting ----------

@pytest.mark.parametrize("raw,want", [
    (None, ENTRY_EXIT_WHEN_NEEDED), ("at_1kg", ENTRY_EXIT_AT_1KG),
    (" When_Needed ", ENTRY_EXIT_WHEN_NEEDED), ("when_needed", ENTRY_EXIT_WHEN_NEEDED)])
def test_mode_reads_the_two_values(raw, want):
    assert entry_exit_mode(SimpleNamespace(entry_tier_exit=raw)) == want


@pytest.mark.parametrize("raw", ["at1kg", "never", True, 1])
def test_a_typo_is_refused_not_guessed(raw):
    with pytest.raises(ValueError, match="entry_tier_exit"):
        entry_exit_mode(SimpleNamespace(entry_tier_exit=raw))


def test_default_is_the_operator_rule():
    # Operator 2026-10-02 ("do both"): when_needed is the default; at_1kg is
    # kept only to reproduce the historical plans.
    assert ControlParams.__dataclass_fields__["entry_tier_exit"].default         == ENTRY_EXIT_WHEN_NEEDED
    assert entry_exit_mode(CONTROL) == ENTRY_EXIT_WHEN_NEEDED     # shipped config
    assert entry_exit_mode(object()) == ENTRY_EXIT_WHEN_NEEDED       # key absent


def test_control_yaml_carries_it():
    d = yaml.safe_load((ROOT / "config" / "control.yaml").read_text(encoding="utf-8"))
    c = control_from_dict({**d, "entry_tier_exit": "when_needed"})
    assert entry_exit_mode(c) == ENTRY_EXIT_WHEN_NEEDED


# ---------- the coordinator ----------

def _fact(bid, wl, stage, wt, need, elig, tranog=False):
    n = 50_000.0
    return BatchWeekFact(
        batch_id=bid, week_label=wl,
        week_start=START + timedelta(weeks=WEEKS.index(wl)), stage=stage,
        count=n, avg_wt_g=wt, biomass_kg=n * wt / 1000, feed_kg_day=0.0,
        feed_kg_week=0.0, cv_pct=16.0, cumulative_harvested_count=0.0,
        count_after_harvest=n, biomass_kg_after_harvest=n * wt / 1000,
        feed_kg_day_after_harvest=0.0, tanks_needed_at_density_cap=need,
        eligible_systems=tuple(sorted(elig)), is_tranog_week=tranog,
        fraction_above_min_harvest_weight=0.0, tanks_needed_base=need)


def _life(bid, d):
    return BatchLifecycle(bid, d, None, None, None, None, 16.0, [], "", 1.0, 1.0)


def _plan(mode, big_tanks=(11, 12, 13), big_need=3, fill_og36=False,
          big_wt=1500.0, arrival=True, empty_entry=(), big_last=None,
          arrival_wk=ARRIVAL, big_need_last=None):
    """BIG (>= 1 kg) holds `big_tanks`; MID (sub-1 kg) holds every other
    entry tank; NEW arrives in ARRIVAL needing 2 entry tanks."""
    st = FacilityState.from_facility_config(FACILITY, START)
    mid_tanks = [t for t in OG12 if t not in big_tanks and t not in empty_entry]
    for t in big_tanks:
        st.tanks_by_id[t].assign("BIG", 20_000, big_wt, 16.0, "SW")
    for t in mid_tanks:
        st.tanks_by_id[t].assign("MID", 20_000, 600.0, 16.0, "SW")
    if fill_og36:            # every grow-out tank taken by an older batch
        for t in OG36:
            st.tanks_by_id[t].assign("OLD", 20_000, 3000.0, 16.0, "SW")
    facts = {}
    for wl in WEEKS:
        if big_last is None or wl <= big_last:
            need = (big_need_last if (big_need_last is not None
                                      and wl == big_last) else big_need)
            facts[("BIG", wl)] = _fact("BIG", wl, "SW", big_wt, need, OG36_ALL)
        facts[("MID", wl)] = _fact("MID", wl, "SW", 600.0, len(mid_tanks),
                                   ENTRY_SYSTEMS)
        if fill_og36:
            facts[("OLD", wl)] = _fact("OLD", wl, "SW", 3000.0, len(OG36),
                                       OG36_ALL)
        if wl < arrival_wk or not arrival:
            facts[("NEW", wl)] = _fact("NEW", wl, "FW", 200.0, 0, ())
        else:
            facts[("NEW", wl)] = _fact("NEW", wl, "SW", 300.0, 2, ENTRY_SYSTEMS,
                                       tranog=(wl == arrival_wk))
    lives = {"OLD": _life("OLD", date(2025, 1, 1)),
             "BIG": _life("BIG", date(2025, 6, 1)),
             "MID": _life("MID", date(2025, 9, 1)),
             "NEW": _life("NEW", date(2026, 1, 1))}
    if not fill_og36:
        del lives["OLD"]
    bots = []
    ctl = dataclasses.replace(CONTROL, entry_tier_exit=mode)
    plan, _ = _build_facility_assignment_plan(
        WEEKS, st, FACILITY, facts, lives, bots, control=ctl)
    return {k: set(v.tank_ids) for k, v in plan.items()}, bots


def _entry(tanks):
    return {t for t in tanks if t in OG12}


def test_at_1kg_still_swaps_the_big_batch_out_in_week_one():
    plan, _ = _plan(ENTRY_EXIT_AT_1KG)
    assert _entry(plan[("BIG", WEEKS[0])]) == set()


def test_when_needed_keeps_the_big_batch_in_its_entry_tanks():
    plan, _ = _plan(ENTRY_EXIT_WHEN_NEEDED)
    assert plan[("BIG", "2027-W01")] == {11, 12, 13}
    assert plan[("BIG", "2027-W02")] == {11, 12, 13}


def test_when_needed_frees_exactly_the_arrivals_tanks_the_week_before():
    plan, bots = _plan(ENTRY_EXIT_WHEN_NEEDED)
    # W03: two entry tanks handed forward, one kept (only 2 are needed).
    assert len(_entry(plan[("BIG", "2027-W03")])) == 1
    assert len(plan[("BIG", "2027-W03")]) == 3              # 1:1 swap, no loss
    new = plan[("NEW", ARRIVAL)]
    assert len(new) == 2 and new <= set(OG12)
    assert not [b for b in bots if b.kind == "assignment_tranog_unmet"]
    # the sub-1 kg batch was never touched
    assert all(plan[("MID", wl)] == set(OG12) - {11, 12, 13} for wl in WEEKS)


def test_when_needed_never_adds_a_new_entry_tank_to_a_big_batch():
    # R3: a >= 1 kg batch can only GROW forward -- even with an empty entry
    # tank in its own system (15 is OG1N, like 11 and 13) sitting free.
    plan, _ = _plan(ENTRY_EXIT_WHEN_NEEDED, big_need=5, empty_entry=(15,),
                    arrival=False)
    for wl in WEEKS:
        assert _entry(plan[("BIG", wl)]) == {11, 12, 13}
        assert len(plan[("BIG", wl)]) == 5


def test_when_needed_counts_kept_entry_tanks_toward_the_target():
    # need 4, holds 3 entry tanks -> ONE grow-out tank added, not four.
    plan, _ = _plan(ENTRY_EXIT_WHEN_NEEDED, big_need=4)
    w1 = plan[("BIG", "2027-W01")]
    assert _entry(w1) == {11, 12, 13} and len(w1) == 4


def test_biggest_fish_go_first_then_a_sub_1kg_batch_if_still_short():
    # BIG holds only ONE entry tank; the arrival needs two.
    plan, bots = _plan(ENTRY_EXIT_WHEN_NEEDED, big_tanks=(11,), big_need=1)
    assert _entry(plan[("BIG", "2027-W03")]) == set()
    mid3 = plan[("MID", "2027-W03")]
    assert len(_entry(mid3)) == len(OG12) - 2 and len(mid3) == len(OG12) - 1
    assert len(plan[("NEW", ARRIVAL)]) == 2
    assert not [b for b in bots if b.kind == "assignment_tranog_unmet"]


def test_no_grow_out_room_means_a_loud_bottleneck_not_a_bent_rule():
    plan, bots = _plan(ENTRY_EXIT_WHEN_NEEDED, fill_og36=True)
    assert plan[("BIG", "2027-W03")] == {11, 12, 13}         # nowhere to go
    assert [b for b in bots if b.kind == "assignment_tranog_unmet"
            and b.week_label == ARRIVAL]


def test_no_arrival_means_no_eviction_at_all():
    plan, _ = _plan(ENTRY_EXIT_WHEN_NEEDED, arrival=False)
    assert all(plan[("BIG", wl)] == {11, 12, 13} for wl in WEEKS)


# ---------- a finished batch hands back every tank ----------

def test_a_batch_whose_seawater_life_ends_releases_all_its_tanks():
    # BIG's facts stop after W04 while it still owns 11/12/13: from W05 those
    # tanks are free, so the W07 arrival takes two of them and the sub-1 kg
    # batch is never moved. (EVT_RELEASE alone frees a COUNT and never fires
    # when a batch simply ends -- the tanks stayed owned forever.)
    plan, bots = _plan(ENTRY_EXIT_WHEN_NEEDED, big_last="2027-W04",
                       arrival_wk="2027-W07")
    assert plan[("NEW", "2027-W07")] <= {11, 12, 13}
    assert all(plan[("MID", wl)] == set(OG12) - {11, 12, 13} for wl in WEEKS)
    assert not [b for b in bots if b.kind == "assignment_tranog_unmet"]
