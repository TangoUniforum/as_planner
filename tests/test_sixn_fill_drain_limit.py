"""A purge tank drains as ONE unit, so its fill is bounded by one week's processing.

DEFECT (2026-09-04, traced from a 102,459-fish harvest week). 6N depuration is
fill → hold → drain the whole tank, so the size of the harvest is decided when
the tank is FILLED, two weeks before anyone sees it.

`_make_room_into_6n` already splits a dump across slots so none passes the
density fill cap, and falls back to overloading the last slot rather than drop
an arrival. But `_sixn_fill_capacity_fish` bounds a slot on kg/m³ ONLY. Nothing
bounded it on what the plant can process on release.

In 2026-W53 two separate make-room calls — freeing grow-out tanks for a TranOG
arrival due in 2027-W04 — each chose OG6N-63:

    MOVED OG4S-46 (batch B47, 30450 fish) into 6N OG6N-63
    MOVED OG4N-41 (batch B47, 22122 fish) into 6N OG6N-63

Tank 63 reached 102,459 fish at 201 kg/m³ — legal, because R8 exempts purge
tanks from the density cap — and in 2027-W02 it drained as one harvest against
a 55,000 processing limit. The drain-side guard could not save it: it stands
down when nothing else has been harvested that week, because an empty harvest
week outranks the limit.

OPERATOR RULING (2026-09-04): the processing limit wins; only when the hold
clock expires do we take an oversized drain, and even then we first look for
headroom in another tank of the same batch. Applied at FILL time that is one
rule — a slot may not be filled past what one week can process — with the
existing no-drop fallback kept as the genuine last resort, because an
overloaded purge tank still beats a dropped arrival.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forecast import placement  # noqa: E402


class _Tank:
    def __init__(self, tid, count=0.0, volume=1720.0, cap=95.0,
                 system_id="OG6N", stage="STARVE"):
        self.system_id = system_id
        self.stage = stage
        self.tank_id = tid
        self.count = count
        self.volume_m3 = volume
        self.max_density_kg_m3 = cap
        self.location_id = f"OG6N-{tid}"
        self.batch_id = "B47" if count else None
        self.avg_wt_g = 3400.0

    @property
    def is_empty(self):
        return self.count <= 0


class _State:
    def __init__(self, tanks):
        self.tanks_by_id = {t.tank_id: t for t in tanks}


def test_an_empty_slot_is_bounded_by_one_weeks_processing():
    """THE RULE. Density would allow far more fish than the plant can take."""
    st = _State([_Tank(63)])
    cap = placement._sixn_fill_slot_cap(st, 63, 3400.0, purge=True,
                                        harvest_cap=55000.0)
    assert cap <= 55000.0, (
        "a slot was offered more fish than one week can process — that tank "
        "drains as a single harvest")


def test_a_partly_filled_slot_offers_only_the_remainder():
    """The 2026-W53 shape: a second make-room call in the same week must see
    what the first one already put there."""
    st = _State([_Tank(63, count=48000.0)])
    cap = placement._sixn_fill_slot_cap(st, 63, 3400.0, purge=True,
                                        harvest_cap=55000.0)
    assert cap == pytest.approx(7000.0), cap


def test_a_slot_already_at_the_limit_offers_nothing():
    """So the fill moves on to the next slot instead of stacking this one."""
    st = _State([_Tank(63, count=60000.0)])
    assert placement._sixn_fill_slot_cap(st, 63, 3400.0, purge=True,
                                         harvest_cap=55000.0) == 0.0


def test_in_purge_the_processing_limit_is_the_only_bound_there_is():
    """WHY THIS FIX IS LOAD-BEARING RATHER THAN BELT-AND-BRACES. R8 gives a
    depuration tank an INFINITE density cap on purpose — the harvest schedule
    is what bounds it, not kg/m³ — so `_sixn_fill_capacity_fish` returns inf
    in purge and stopped nothing at all. That is how OG6N-63 reached
    201 kg/m³ without breaking a rule."""
    st = _State([_Tank(63)])
    assert placement._sixn_fill_capacity_fish(st, 63, 3400.0,
                                              purge=True) == float("inf")
    assert placement._sixn_fill_slot_cap(st, 63, 3400.0, purge=True,
                                         harvest_cap=55000.0) == 55000.0


def test_density_still_binds_outside_purge():
    """NEGATIVE CONTROL. Production-mode 6N is an ordinary system and its
    density cap must keep binding — this must not become a harvest-only rule."""
    st = _State([_Tank(63, stage="SW")])
    dense = placement._sixn_fill_capacity_fish(st, 63, 400.0, purge=False)
    assert dense < 10 ** 9
    got = placement._sixn_fill_slot_cap(st, 63, 400.0, purge=False,
                                        harvest_cap=10 ** 9)
    assert got == pytest.approx(dense), (
        "the density cap stopped binding — %r vs %r" % (got, dense))


def test_no_harvest_cap_means_the_old_behaviour_exactly():
    """NEGATIVE CONTROL. Callers that pass nothing must be byte-identical to
    the density-only rule, so this cannot change a path it was not wired into."""
    st = _State([_Tank(63, count=1000.0, stage="SW")])
    for purge in (True, False):
        for wt in (400.0, 3400.0):
            assert (placement._sixn_fill_slot_cap(st, 63, wt, purge=purge,
                                                  harvest_cap=None)
                    == placement._sixn_fill_capacity_fish(st, 63, wt,
                                                          purge=purge))


def test_it_never_returns_a_negative_offer():
    """A tank over the limit already (a PR handover, say) must offer zero, not
    a negative number that would flip a min() somewhere downstream."""
    st = _State([_Tank(63, count=90000.0)])
    assert placement._sixn_fill_slot_cap(st, 63, 3400.0, purge=True,
                                         harvest_cap=55000.0) >= 0.0
