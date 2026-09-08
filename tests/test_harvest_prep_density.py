"""HARVEST PREP IS A RAISED CAP (150 kg/m3), NOT AN EXEMPTION.

Operator, 2026-09-08: "when we are starving to harvest we can increase the
density limit to 150kg/m3 when a tank is being prepared for harvest and when 6N
is production."

The model had it as +infinity. `_consolidate_harvest_prep` merges a batch's
in-place harvest-prep tanks into its fullest one and, until today, checked no
density at all -- its docstring simply asserted the exemption. On the live
2026-08-31 plan it fired 48 times and **28 of those merges exceeded 150 kg/m3,
the worst at 254**. Only TWO ever reached a BatchLocations row, because a
consolidated tank is usually harvested inside the same week and the end-of-week
snapshot never sees the peak. So the condition was real, large, and invisible.

TWO SEPARATE THINGS, and this file pins the seam between them:

  DETECTION (always on). The merge warning names the limit when it is crossed,
  and the R8 audit judges a harvest-prep tank at 150 instead of +inf. Plan
  neutral -- measured, the plan is identical on every metric and only the audit
  count moves.

  ENFORCEMENT (opt-in, `control.harvest_prep_density_limit`, default 0.0). The
  pass bin-packs the group into as few of its own tanks as fit. It WORKS -- 29
  over-limit merges go to 0 -- but on the live plan it costs 2 weeks over the
  HARD 15-move handling budget and 3 over the 55,000 harvest ceiling, including
  2027-W05 at 64,136 (+16.6%). That is why it does not ship on. Two independent
  implementations produced a bit-identical plan, so the cost is structural.

NEGATIVE CONTROL: test_without_the_limit_it_still_overfills proves the alarm can
ring -- the same fixture with the limit off merges to ~213 kg/m3.
"""
from __future__ import annotations

from datetime import date

from forecast.placement import _consolidate_harvest_prep
from forecast.state import STAGE_STARVE, FacilityState, TankState
from forecast.tiers import HARVEST_PREP_DENSITY_CAP

TODAY = date(2028, 3, 1)


def _tank(tid, system, *, batch=None, count=0.0, wt=0.0, vol=1000.0,
          dens=85.0, stage=STAGE_STARVE):
    t = TankState(f"{system}-{tid}", tid, system, vol, dens, 3000.0, "OG")
    if batch is not None:
        t.assign(batch, count, wt, 0.0, stage)
    return t


def _group():
    """One batch, three harvest-prep tanks: 90 + 70 + 50 t in 1,000 m3 each.

    Merged into one tank that is 210,000 kg in 1,000 m3 = 210 kg/m3, far over
    the 150 limit. Bin-packed at 150 it needs two tanks (90+50 = 140, then 70).
    """
    return [
        _tank(31, "OG3N", batch="B1", count=18000.0, wt=5000.0),   # 90,000 kg
        _tank(32, "OG3N", batch="B1", count=14000.0, wt=5000.0),   # 70,000 kg
        _tank(33, "OG3N", batch="B1", count=10000.0, wt=5000.0),   # 50,000 kg
    ]


def _run(limit):
    state = FacilityState(TODAY, _group())
    events, warnings = [], []
    freed = _consolidate_harvest_prep(
        state, TODAY, events, warnings, max_moves=8, density_limit=limit)
    occupied = [t for t in state.tanks_by_id.values() if not t.is_empty]
    return state, events, warnings, freed, occupied


def test_without_the_limit_it_still_overfills():
    """NEGATIVE CONTROL. Default behaviour is unchanged: one tank, any density."""
    _, _, warnings, freed, occupied = _run(0.0)
    assert freed == 2                       # both other tanks handed back
    assert len(occupied) == 1
    assert occupied[0].density_kg_m3 > HARVEST_PREP_DENSITY_CAP
    assert round(occupied[0].density_kg_m3) == 210
    assert any("HARVEST-PREP OVER LIMIT" in w for w in warnings)


def test_the_over_limit_warning_names_the_limit_and_the_density():
    _, _, warnings, _, _ = _run(0.0)
    hit = [w for w in warnings if "HARVEST-PREP OVER LIMIT" in w]
    assert len(hit) == 1
    assert "210 kg/m3" in hit[0]
    assert "150 kg/m3" in hit[0]
    assert "not executable as planned" in hit[0]


def test_enforcing_bin_packs_instead_of_overfilling():
    _, _, warnings, freed, occupied = _run(HARVEST_PREP_DENSITY_CAP)
    assert all(t.density_kg_m3 <= HARVEST_PREP_DENSITY_CAP for t in occupied), \
        [(t.tank_id, t.density_kg_m3) for t in occupied]
    assert not any("HARVEST-PREP OVER LIMIT" in w for w in warnings)
    # 90 + 50 fits at 140; 70 does not, so it keeps its own tank.
    assert len(occupied) == 2
    assert freed == 1
    assert sorted(round(t.density_kg_m3) for t in occupied) == [70, 140]


def test_enforcement_conserves_every_fish():
    """A refused merge must leave fish where they are, never drop them."""
    before = sum(t.count for t in _group())
    state, _, _, _, occupied = _run(HARVEST_PREP_DENSITY_CAP)
    assert sum(t.count for t in occupied) == before


def test_a_group_that_already_fits_is_merged_whole_either_way():
    """The limit must not block a consolidation that was always legal."""
    tanks = [_tank(31, "OG3N", batch="B1", count=10000.0, wt=5000.0),
             _tank(32, "OG3N", batch="B1", count=4000.0, wt=5000.0)]
    for limit in (0.0, HARVEST_PREP_DENSITY_CAP):
        state = FacilityState(TODAY, [
            _tank(t.tank_id, "OG3N", batch="B1", count=t.count, wt=5000.0)
            for t in tanks])
        events, warnings = [], []
        freed = _consolidate_harvest_prep(
            state, TODAY, events, warnings, max_moves=8, density_limit=limit)
        occupied = [t for t in state.tanks_by_id.values() if not t.is_empty]
        assert freed == 1 and len(occupied) == 1
        assert round(occupied[0].density_kg_m3) == 70
