"""The monthly check must rank on CONSTRAINTS and must not hide a null.

Four levers were measured across eight starting states and all four rejected as
defaults, because each helps some starting states and hurts others. There is no
better default to find — only a cheap per-month answer. These tests pin the
rules that answer encodes.
"""
import pytest

from forecast import monthly_check as mc


def _row(name, *, floor=3, minwk=26_000, feed=60, moves=15,
         harvest=2_480_000, gates=None, **kw):
    r = {
        "name": name,
        "weeks_below_floor": floor,
        "min_week": minwk,
        "feed_over": feed,
        "moves_week_max": moves,
        "harvest_count": harvest,
        "system_overshoot": 0.3,
        "gates": gates or {"conservation": "PASS", "no_empty_week": "PASS",
                           "sixn_one_way": "PASS", "system_feed": "FAIL"},
    }
    r.update(kw)
    return r


BASE = "Your config"


def test_a_leg_that_misses_the_floor_more_often_loses_however_good_the_rest_is():
    """min_harvest_per_week is a SALES CONTRACT. This is the rule the whole
    module exists for: a huge feed win does not buy a missed commitment."""
    base = _row(BASE, floor=3, feed=60)
    cand = _row("+ repair", floor=4, feed=5)      # feed massively better
    ok, why = mc.beats(cand, base)
    assert not ok
    assert "contract floor" in why
    v = mc.decide([base, cand])
    assert v.keep_current
    assert v.winner is None


def test_a_leg_that_holds_the_floor_and_cuts_feed_materially_wins():
    base = _row(BASE, floor=3, feed=60)
    cand = _row("+ repair", floor=3, feed=20)     # -40, well past the ±13 band
    ok, why = mc.beats(cand, base)
    assert ok and "feed" in why
    assert mc.decide([base, cand]).winner == "+ repair"


def test_fewer_floor_misses_wins_even_if_feed_is_unchanged():
    base = _row(BASE, floor=8)
    cand = _row("+ repair", floor=3)
    assert mc.beats(cand, base)[0]
    assert mc.decide([base, cand]).winner == "+ repair"


def test_a_feed_gain_inside_the_noise_band_is_not_a_win():
    """±13 system-weeks is the measured band; 10 is the plan wobbling."""
    base = _row(BASE, feed=60)
    cand = _row("+ repair", feed=50)
    ok, why = mc.beats(cand, base)
    assert not ok
    assert why == "no measurable difference"


def test_collapsing_the_worst_week_disqualifies_even_with_the_same_floor_count():
    """The 8.13 PR case that got cap_repair withdrawn: same weeks-below-floor,
    but the worst week fell 23,259 -> 4,578."""
    base = _row(BASE, floor=3, minwk=23_259)
    cand = _row("+ repair", floor=3, minwk=4_578, feed=5)
    ok, why = mc.beats(cand, base)
    assert not ok
    assert "worst harvest week" in why


def test_a_hard_gate_failure_disqualifies_outright():
    base = _row(BASE)
    bad = _row("+ rebalancer", floor=0, feed=1,
               gates={"conservation": "FAIL", "no_empty_week": "PASS",
                      "sixn_one_way": "PASS"})
    v = mc.decide([base, bad])
    assert v.winner is None
    assert any("conservation" in str(d) for d in v.disqualified)


def test_the_null_is_stated_plainly_and_not_as_a_failure():
    v = mc.decide([_row(BASE), _row("+ repair")])
    assert v.keep_current
    assert not v.material
    assert "keep what you have" in v.reason
    assert "real result" in v.reason


def test_score_is_never_consulted():
    """A leg with a far better score but a worse floor must still lose."""
    base = _row(BASE, floor=3, system_overshoot=0.9)
    cand = _row("+ repair", floor=6, system_overshoot=0.01)
    assert not mc.beats(cand, base)[0]


def test_a_baseline_failing_a_hard_gate_is_surfaced_not_swallowed():
    base = _row(BASE, gates={"conservation": "FAIL", "no_empty_week": "PASS",
                             "sixn_one_way": "PASS"})
    v = mc.decide([base, _row("+ repair")])
    assert any("your current config fails hard gate" in n.lower() for n in v.notes)


def test_a_leg_that_did_not_run_is_reported_not_dropped():
    v = mc.decide([_row(BASE), {"name": "+ repair", "error": "engine rc=1"}])
    assert any(d[0] == "+ repair" for d in v.disqualified)


def test_a_missing_baseline_is_an_honest_refusal():
    v = mc.decide([_row("+ repair")])
    assert v.winner is None
    assert "baseline" in v.reason


def test_summary_puts_constraints_before_score():
    cols = list(mc.summary_rows([_row(BASE)])[0].keys())
    assert cols.index("Weeks below floor") < cols.index("Score (not decisive)")
    assert cols.index("Hard gates") < cols.index("Weeks below floor")
    assert cols[-1] == "Score (not decisive)"


def test_legs_are_declared_and_short():
    """This is a CHECK, not a search: a handful of legs with stated reasons."""
    assert 2 <= len(mc.LEGS) <= 4
    assert mc.LEGS[0]["overrides"] == {}, "the first leg must be the baseline"
    for leg in mc.LEGS:
        assert leg["why"].strip()


def test_legs_never_touch_operator_inputs():
    from forecast.methods import UNTUNABLE_KNOBS
    for leg in mc.LEGS:
        assert not (set(leg["overrides"]) & UNTUNABLE_KNOBS), leg["name"]


def test_the_noise_caveat_names_its_source_and_its_limits():
    t = mc.noise_caveat()
    assert "8.23.26" in t and "indicative" in t
    assert "deterministic" in t
