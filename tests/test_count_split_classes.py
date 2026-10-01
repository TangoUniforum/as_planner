"""biology.count_split_classes — the K-way grader split behind graded_transfer.

Pins the three properties the manual graded_transfer relies on to say "it all
adds up": K = 2 is the existing two-way split exactly, biomass is conserved
for any K / fractions / grader efficiency, and total variance is conserved.
"""
from __future__ import annotations

import itertools

import pytest

from forecast.biology import count_split_classes, count_split_cvs, count_split_means

EFFS = (1.0, 0.85, 0.5, 0.0)   # 0.0 = "feature off" (perfect grader), as elsewhere


@pytest.mark.parametrize("p", [0.03, 0.1, 0.37, 0.5, 0.64, 0.9, 0.97])
@pytest.mark.parametrize("eff", EFFS)
def test_two_classes_equal_the_existing_two_way_split(p, eff):
    got = count_split_classes(3200.0, 16.0, [p, 1 - p], eff)
    m_big, m_small = count_split_means(3200.0, 16.0, p, grade_efficiency=eff)
    c_big, c_small = count_split_cvs(3200.0, 16.0, p, grade_efficiency=eff)
    assert got[0][0] == pytest.approx(m_big, abs=1e-9)
    assert got[1][0] == pytest.approx(m_small, abs=1e-9)
    assert got[0][1] == pytest.approx(c_big, abs=1e-9)
    assert got[1][1] == pytest.approx(c_small, abs=1e-9)


_FRACTION_SETS = [
    [0.5, 0.5], [0.2, 0.8], [0.9, 0.1],
    [1 / 3, 1 / 3, 1 / 3], [0.1, 0.6, 0.3], [0.45, 0.05, 0.5],
    [0.25, 0.25, 0.25, 0.25], [0.2, 0.3, 0.1, 0.4], [0.05, 0.05, 0.05, 0.85],
]


@pytest.mark.parametrize("fr", _FRACTION_SETS)
@pytest.mark.parametrize("eff", EFFS)
@pytest.mark.parametrize("mu,cv", [(450.0, 18.0), (2500.0, 16.0), (4100.0, 12.0)])
def test_biomass_and_variance_are_conserved(fr, eff, mu, cv):
    out = count_split_classes(mu, cv, fr, eff)
    assert len(out) == len(fr)
    # Mean of the class means, weighted by count share == parent mean.
    assert sum(f * m for f, (m, _c) in zip(fr, out)) == pytest.approx(mu, rel=1e-12)
    # Within + between == parent variance.
    sd = mu * cv / 100.0
    tot = sum(f * ((m * c / 100.0) ** 2 + (m - mu) ** 2) for f, (m, c) in zip(fr, out))
    assert tot == pytest.approx(sd * sd, rel=1e-9)


@pytest.mark.parametrize("fr", _FRACTION_SETS)
def test_classes_come_out_biggest_first_and_narrower_than_parent_on_average(fr):
    out = count_split_classes(2500.0, 16.0, fr, 1.0)
    means = [m for m, _c in out]
    assert means == sorted(means, reverse=True)
    # A perfect grader can only REDUCE the within-class spread.
    within = sum(f * (m * c / 100.0) ** 2 for f, (m, c) in zip(fr, out))
    assert within < (2500.0 * 0.16) ** 2


def test_degenerate_inputs_return_the_parent():
    assert count_split_classes(2000.0, 0.0, [0.5, 0.5]) == [(2000.0, 0.0)] * 2
    assert count_split_classes(0.0, 16.0, [0.5, 0.5]) == [(0.0, 16.0)] * 2
    assert count_split_classes(2000.0, 16.0, [1.0]) == [(2000.0, 16.0)]
    assert count_split_classes(2000.0, 16.0, []) == []


def test_bad_fractions_are_rejected():
    with pytest.raises(ValueError):
        count_split_classes(2000.0, 16.0, [0.5, 0.4])
    with pytest.raises(ValueError):
        count_split_classes(2000.0, 16.0, [1.2, -0.2])


def test_order_matters_and_is_not_symmetric():
    """Same fractions, different order -> the 10% slice sits at a different
    place in the distribution, so its weight differs."""
    a = count_split_classes(2500.0, 16.0, [0.1, 0.9])
    b = count_split_classes(2500.0, 16.0, [0.9, 0.1])
    assert a[0][0] > b[1][0] - 1e-9 and a[0][0] != pytest.approx(b[0][0])
    for perm in itertools.permutations([0.2, 0.3, 0.5]):
        out = count_split_classes(2500.0, 16.0, list(perm))
        assert sum(f * m for f, (m, _c) in zip(perm, out)) == pytest.approx(2500.0)
