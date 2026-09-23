"""tools/knob_corpus: judge a knob across the corpus, never on one PR.

The rule this enforces was learned the expensive way. On 2026-09-19
density_relief_pct 0.85 was the best-looking lever of the session on ONE PR --
fewer moves, lower harvest CV, a processing-cap breach fixed -- and across the
17 corpus PRs it was a wash or worse, AND it made one PR infeasible that plans
fine today. The planner is mode-discontinuous, so a single-PR win is not
evidence.

These tests cover the judging, the config isolation and the verdicts. They never
run the engine.
"""
from __future__ import annotations

import pytest
import yaml

from tools import knob_corpus as kc


# --------------------------------------------------------------------------- #
# reading a knob off the command line
# --------------------------------------------------------------------------- #
def test_values_keep_their_type():
    """3200 must not become a string, 0.85 must not become 0, false must not
    become the string 'false' -- each would reach control.yaml as the wrong
    thing and change a run without anyone noticing."""
    got = kc.parse_set(["min_harvest_weight_g=3200", "density_relief_pct=0.85",
                        "harvest_grade_to_min=false", "harvest_level_target="])
    assert got == {"min_harvest_weight_g": 3200,
                   "density_relief_pct": 0.85,
                   "harvest_grade_to_min": False,
                   "harvest_level_target": None}
    assert isinstance(got["min_harvest_weight_g"], int)
    assert got["harvest_grade_to_min"] is False


def test_a_malformed_pair_is_refused():
    with pytest.raises(ValueError, match="knob=value"):
        kc.parse_set(["density_relief_pct"])


def test_no_pairs_is_no_knobs():
    assert kc.parse_set(None) == {} and kc.parse_set([]) == {}


# --------------------------------------------------------------------------- #
# config isolation
# --------------------------------------------------------------------------- #
def test_the_variant_config_is_a_copy_that_never_carries_costs(tmp_path):
    """Every run is an isolated copy; costs.yaml holds real cost data and no
    corpus comparison needs it."""
    src = tmp_path / "config"
    src.mkdir()
    (src / "control.yaml").write_text(
        yaml.safe_dump({"density_relief_pct": 0.95, "min_harvest_per_week": 26000}),
        encoding="utf-8")
    (src / "costs.yaml").write_text("secret: 1\n", encoding="utf-8")
    (src / "facility.yaml").write_text("tanks: []\n", encoding="utf-8")

    dst = kc.write_config(src, tmp_path / "out", {"density_relief_pct": 0.85})
    doc = yaml.safe_load((dst / "control.yaml").read_text(encoding="utf-8"))
    assert doc["density_relief_pct"] == 0.85       # changed
    assert doc["min_harvest_per_week"] == 26000    # untouched
    assert (dst / "facility.yaml").exists()        # the rest comes along
    assert not (dst / "costs.yaml").exists(), "costs.yaml must never be copied"
    # and the source is untouched
    src_doc = yaml.safe_load((src / "control.yaml").read_text(encoding="utf-8"))
    assert src_doc["density_relief_pct"] == 0.95


def test_no_knobs_still_gives_a_usable_baseline_config(tmp_path):
    src = tmp_path / "config"
    src.mkdir()
    (src / "control.yaml").write_text(yaml.safe_dump({"a": 1}), encoding="utf-8")
    dst = kc.write_config(src, tmp_path / "out", {})
    assert yaml.safe_load((dst / "control.yaml").read_text(encoding="utf-8")) == {"a": 1}


# --------------------------------------------------------------------------- #
# the verdicts
# --------------------------------------------------------------------------- #
def _rows(n, identical, base=None, variant=None):
    base = base or {"hog_t": 10_000.0, "moves": 500, "harvest_cv": 0.26,
                    "weeks_below_floor": 1, "weeks_over_cap": 0}
    variant = variant or dict(base)
    out = []
    for i in range(n):
        same = i < identical
        out.append({"pr": "PR%02d" % i, "base": dict(base), "variant": dict(variant),
                    "base_hash": "aaa", "variant_hash": "aaa" if same else "bbb"})
    return out


def test_identical_on_every_pr_is_safe_to_delete():
    s = kc.summarise(_rows(17, 17), identical_mode=True)
    assert s["identical"] == 17 and s["n"] == 17
    assert "DEAD" in kc.verdict(s, True) and "safe to delete" in kc.verdict(s, True)


def test_one_differing_pr_blocks_a_deletion():
    """The whole point: 16 of 17 identical is NOT dead."""
    s = kc.summarise(_rows(17, 16), identical_mode=True)
    v = kc.verdict(s, True)
    assert "NOT DEAD" in v and "1 of 17" in v


def test_an_infeasible_pr_rejects_the_change_outright():
    """density_relief_pct 0.85 made 2025-06-30 unplannable. A knob that breaks a
    PR which plans fine today is rejected whatever its averages look like."""
    rows = _rows(16, 16)
    rows.append({"pr": "2025-06-30", "base": {"hog_t": 1.0, "moves": 1,
                                              "harvest_cv": 0.0,
                                              "weeks_below_floor": 0,
                                              "weeks_over_cap": 0},
                 "base_hash": "aaa", "variant": None,
                 "variant_err": "RuntimeError: cannot be placed"})
    s = kc.summarise(rows, identical_mode=False)
    assert s["failed"] == ["2025-06-30"]
    assert kc.verdict(s, False).startswith("REJECT")


def test_a_real_lever_is_reported_with_how_many_prs_it_helped():
    """Never a bare mean: the count of PRs improved is what says whether a
    win is real or one lucky PR."""
    rows = _rows(10, 0)
    for i, r in enumerate(rows):
        r["variant"]["hog_t"] = r["base"]["hog_t"] * (1.02 if i < 3 else 0.99)
        r["variant"]["moves"] = r["base"]["moves"] - 20
    s = kc.summarise(rows, identical_mode=False)
    assert s["hog_better"] == 3 and s["moves_fewer"] == 10
    v = kc.verdict(s, False)
    assert "better on 3/10" in v and "fewer on 10/10" in v


def test_a_change_that_costs_the_floor_says_so():
    rows = _rows(5, 0)
    for r in rows:
        r["variant"]["weeks_below_floor"] = r["base"]["weeks_below_floor"] + 2
    s = kc.summarise(rows, identical_mode=False)
    assert s["floor_worse"] == 5
    assert "floor worse on 5" in kc.verdict(s, False)


def test_no_pr_ran_on_both_arms_is_no_evidence():
    """The three oldest corpus PRs fail on BOTH arms for a pre-existing reason.
    That must read as NO EVIDENCE, never as agreement."""
    rows = [{"pr": "2024-11-30", "base": None, "variant": None}]
    s = kc.summarise(rows, identical_mode=True)
    assert s["n"] == 0 and s["failed"] == []
    assert "NO EVIDENCE" in kc.verdict(s, True)


def test_an_inert_knob_is_named_inert_outside_deletion_mode():
    s = kc.summarise(_rows(12, 12), identical_mode=False)
    assert "INERT" in kc.verdict(s, False)


def test_the_plan_is_what_is_hashed():
    """A knob is dead only if it leaves the sheets an operator acts on
    untouched -- not merely the totals."""
    assert kc.PLAN_SHEETS == ("BatchLocations", "TransferPlan", "HarvestPlan")
