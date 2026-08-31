"""The contract floor must be a GRADIENT, not only a veto.

Measured 2026-08-12: across a 40-variant search the worst harvest week ranged
7,855..27,462 fish while corr(min_week, score) was -0.03 — the objective was
statistically blind to the floor, and the best-floor plan ranked 36th of 40. A
guard caught the worst of it, but a guard can only reject a plan; it cannot
steer toward a better one. Scoring the floor gives the search the gradient.
"""
import pytest

from forecast import optimize


class _M:
    def __init__(self, **kw):
        self._d = {c: 0.0 for c in optimize.COMPONENTS}
        self._d.update(kw)

    def component(self, name):
        return self._d[name]


class _V:
    def __init__(self, **kw):
        self.metrics = _M(**kw)
        self.conservation_ok = True
        self.norm = {}
        self.score = 0.0


def test_the_floor_is_a_scored_component():
    assert "harvest_floor_gap" in optimize.COMPONENTS


def test_every_emphasis_weights_the_contract():
    """A sales contract is not optional under any objective."""
    for name, w in optimize.EMPHASIS_PRESETS.items():
        assert w.get("harvest_floor_gap", 0) > 0, name


def test_a_worse_floor_scores_worse_all_else_equal():
    good = _V(harvest_floor_gap=0.01)
    bad = _V(harvest_floor_gap=0.20)
    optimize.score_variants([good, bad], optimize.weights_for("Walk the line"))
    assert good.score < bad.score


def test_the_floor_now_holds_its_own_against_flatness():
    """The failure this fixes: a variant winning on flatness while starving its
    lean weeks used to come out ahead unopposed, because the floor had no term
    at all (it contributed 0 whatever it did).

    Under 'Walk the line' the floor carries weight 3, the SAME as harvest_var,
    so the two exactly balance and the starving plan no longer wins for free.
    Whether the contract should OUTWEIGH flatness is an operator judgement about
    the objective, not something this test should smuggle in — it pins that the
    floor is no longer free to give away."""
    starves = _V(harvest_floor_gap=0.20, harvest_var=0.0)
    steady = _V(harvest_floor_gap=0.0, harvest_var=0.10)
    optimize.score_variants([starves, steady], optimize.weights_for("Walk the line"))
    assert steady.score <= starves.score
    # and with the floor removed from the objective, the starving plan WINS —
    # which is exactly the behaviour that existed before this change
    w = dict(optimize.weights_for("Walk the line")); w["harvest_floor_gap"] = 0
    optimize.score_variants([starves, steady], w)
    assert starves.score < steady.score


def test_not_measured_is_scored_as_the_WORST_not_as_zero():
    """Absence must never be an advantage — the same reason the gates report
    N/A instead of PASS. A None scored as 0 would read as a perfect floor."""
    unknown = _V(harvest_floor_gap=None)
    known_bad = _V(harvest_floor_gap=0.20)
    known_good = _V(harvest_floor_gap=0.0)
    optimize.score_variants([unknown, known_bad, known_good],
                            optimize.weights_for("Walk the line"))
    assert unknown.norm["harvest_floor_gap"] == pytest.approx(1.0)
    assert unknown.score >= known_bad.score
    assert unknown.score > known_good.score


def test_all_none_does_not_crash_or_reward():
    a, b = _V(harvest_floor_gap=None), _V(harvest_floor_gap=None)
    optimize.score_variants([a, b], optimize.weights_for("Balanced"))
    assert a.norm["harvest_floor_gap"] == 0.0   # no maximum exists to scale against
