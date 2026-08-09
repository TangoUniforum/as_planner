"""The optimizer must REJECT-AND-CONTINUE: a single infeasible/errored variant
(e.g. the placement engine's 'refuse to drop fish' guard) must be recorded as
`failed` and excluded from selection, NOT abort the whole sweep."""
from __future__ import annotations

from dataclasses import fields as _f

import forecast.optimize as opt
import forecast.tuning as tuning


def _zero_metrics():
    kw = {}
    for fld in _f(opt.Metrics):
        if fld.name in ("transfers_by_type", "per_system"):
            continue
        kw[fld.name] = 0 if fld.name == "weeks_over_harvest_cap" else 0.0
    return opt.Metrics(**kw)


def test_run_variant_catches_infeasible_instead_of_raising(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("2027-W11: TranOG arrival B53 CANNOT be placed — "
                           "needs 3 OG tank(s), 0 free")
    monkeypatch.setattr(tuning, "_run_in_tempdir", boom)
    v = opt.run_variant("hot", {"density_target_pct": 0.95}, "c", "s", "in.xlsm")
    assert v.failed and "CANNOT be placed" in v.failed
    assert not v.conservation_ok           # excluded from selection
    assert v.metrics is not None           # sentinel keeps scoring/display alive


def test_recommend_ignores_failed_and_picks_feasible():
    good = opt.OptVariant("baseline", {}, _zero_metrics(), 0, 0)
    bad = opt.OptVariant("hot", {"density_target_pct": 0.95},
                         opt._infeasible_metrics(), 0, 0, failed="CANNOT be placed")
    rec = opt.recommend([good, bad], emphasis=opt.DEFAULT_EMPHASIS)
    assert rec.best_label == "baseline"    # the infeasible variant never wins


def test_sweep_completes_when_one_variant_fails(monkeypatch):
    # Good variants return a fake path; the "hot" one raises. The sweep must
    # return BOTH variants (one ok, one failed) and never propagate the error.
    def fake_run(label, overrides, cdir, sdir, inp):
        if label == "hot":
            raise RuntimeError("infeasible TranOG arrival — 0 free tanks")
        return "/fake/out.xlsm"
    monkeypatch.setattr(tuning, "_run_in_tempdir", fake_run)
    monkeypatch.setattr(
        opt, "metrics_from_workbook",
        lambda out, cap, welfare_density=None, harvest_target=None,
        move_cap=None: (_zero_metrics(), 0, 0))
    grid = [("baseline", {}), ("hot", {"density_target_pct": 0.95})]
    res = opt.sweep("in.xlsm", "c", "s", grid=grid, parallel=False)
    assert len(res) == 2
    by = {v.label: v for v in res}
    assert by["baseline"].conservation_ok
    assert by["hot"].failed and not by["hot"].conservation_ok
