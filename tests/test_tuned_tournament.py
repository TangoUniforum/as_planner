"""The TUNED tournament — registry knob spaces, probe logic, labeling, and the
promote/quick-run round-trip with a Global method.

Pure-logic tests (no pipeline runs). What they lock down:

  * the knob-space REGISTRY contract: the Global methods exclude the
    conservation-breaking knobs (empty space today), nobody's space touches a
    business constant or a wave-3 operational rule, and register() REFUSES an
    illegal space (structural, not advisory);
  * the gate-bound probe logic as pure functions (plan_for / probe_grid /
    probe_outcome / variant_hard_ok / pick_winner);
  * tuned-candidate labeling (pins excluded — the tuning shows, the identity
    doesn't);
  * the promote -> quick-run round-trip with a GLOBAL method + overrides:
    what analysis_defaults.yaml stores must replay through the exact
    config-merge mechanism Quick run uses.
"""
import dataclasses

import pytest
import yaml

from forecast import analysis as A
from forecast import methods as M
from forecast import optimize as O
from forecast import tournament as T


# --------------------------------------------------------------------------- #
# Knob-space registry contents
# --------------------------------------------------------------------------- #
def test_every_method_declares_its_spaces():
    for m in M.REGISTRY.values():
        assert isinstance(m.knob_grid, tuple)
        assert isinstance(m.knob_space, tuple)


def test_globals_have_empty_spaces():
    # Experimentally validated 2026-08-07: the only tunable knobs the global
    # path reads (dev / density) BROKE its conservation proof; the proposed
    # safe candidates are consumed only by the controller engine. Until a
    # knob passes both bars, the honest Global space is EMPTY.
    for key in ("global-lp", "global-milp"):
        m = M.REGISTRY[key]
        assert m.knob_grid == ()
        assert m.knob_space == ()


def test_controller_family_gets_the_full_optimizer_space():
    for key in ("controller", "controller-lns", "controller-hybrid"):
        m = M.REGISTRY[key]
        assert [lbl for lbl, _ in m.knob_grid] == [
            lbl for lbl, _ in O.OPT_FULL_GRID]
        assert [(k, list(vs)) for k, vs in m.knob_space] == [
            (k, list(vs)) for k, vs in O.CD_KNOB_SPACE]


def test_nobody_tunes_business_constants_or_wave3_rules():
    for m in M.REGISTRY.values():
        assert not (M._space_knobs(m) & M.UNTUNABLE_KNOBS), m.key


def test_register_refuses_untunable_knob():
    bad = M.Method(key="_bad", label="x", family="Controller", blurb="",
                   engine="controller",
                   knob_space=(("min_harvest_weight_g", (3000, 3500)),))
    with pytest.raises(ValueError, match="untunable"):
        M.register(bad)
    assert "_bad" not in M.REGISTRY


def test_register_refuses_unvetted_global_knob():
    bad = M.Method(key="_badg", label="x", family="Global", blurb="",
                   engine="global",
                   knob_grid=((("dev=0.02"), {"facility_biomass_deviation_pct":
                                              0.02}),))
    with pytest.raises(ValueError, match="conservation"):
        M.register(bad)
    assert "_badg" not in M.REGISTRY


# --------------------------------------------------------------------------- #
# Plan + probe logic (pure)
# --------------------------------------------------------------------------- #
def test_plan_for_all_branches():
    space = (("k", (1, 2)),)
    assert T.plan_for([], space) == "full-search"
    assert T.plan_for([], ()) == "stock-only"
    assert T.plan_for(["no_empty_week"], space) == "probe"
    assert T.plan_for(["no_empty_week"], ()) == "gate-bound"


def test_probe_grid_is_single_knob_with_pins_merged():
    m = M.REGISTRY["controller-hybrid"]
    grid = T.probe_grid(m)
    per_round = sum(len(vs) for _, vs in m.knob_space)
    assert 0 < len(grid) <= per_round
    for _lbl, ov in grid:
        # pins always present...
        for pk, pv in m.overrides.items():
            assert ov[pk] == pv
        # ...plus EXACTLY ONE searched knob on top
        extra = {k for k in ov if k not in m.overrides}
        assert len(extra) == 1
        # and never a candidate identical to stock (it already failed)
        assert ov != m.overrides


def test_probe_grid_skips_pin_valued_candidates():
    m = dataclasses.replace(
        M.REGISTRY["controller"], overrides={"tran_og_default_tanks": 2},
        knob_space=(("tran_og_default_tanks", (2, 3)),))
    grid = T.probe_grid(m)
    assert [ov["tran_og_default_tanks"] for _, ov in grid] == [3]


def test_search_grid_merges_pins_under_every_row():
    m = M.REGISTRY["controller-lns"]
    grid = T.search_grid(m)
    assert len(grid) == len(m.knob_grid)
    base = dict(grid[0][1])
    assert base == dict(m.overrides)          # "baseline" row == the pins
    for _lbl, ov in grid:
        assert ov["hybrid_follow"] == "off"   # a pin survives every row


def _variant(overrides, dropped=0, overprod=0, zero_weeks=0, failed=None,
             label="v", **metric_overrides):
    m = O._infeasible_metrics()
    m = dataclasses.replace(m, **{c: 1.0 for c in O.COMPONENTS})
    m = dataclasses.replace(m, harvest_zero_weeks=zero_weeks,
                            **metric_overrides)
    return O.OptVariant(label=label, overrides=dict(overrides), metrics=m,
                        dropped=dropped, overprod=overprod, failed=failed)


def test_variant_hard_ok_conservation_and_empty_week():
    assert T.variant_hard_ok(_variant({}, zero_weeks=0)) is True
    assert T.variant_hard_ok(_variant({}, zero_weeks=3)) is False
    assert T.variant_hard_ok(_variant({}, dropped=5)) is False
    assert T.variant_hard_ok(_variant({}, failed="boom")) is False
    # None (pre-field cache entry) = UNKNOWN, never a pass
    assert T.variant_hard_ok(_variant({}, zero_weeks=None)) is None


def test_probe_outcome_gate_bound_unless_a_knob_measurably_fixes():
    assert T.probe_outcome([False, False]) == "gate-bound"
    assert T.probe_outcome([]) == "gate-bound"
    assert T.probe_outcome([None, False]) == "gate-bound"   # unknown != fixed
    assert T.probe_outcome([False, True]) == "fixable"


def test_pick_winner_prefers_full_hard_gate_pass_over_score():
    w = {c: 1.0 for c in O.COMPONENTS}
    better_score_but_empty = _variant({"a": 1}, zero_weeks=2, label="empty",
                                      harvest_var=0.0)
    clean = _variant({"a": 2}, zero_weeks=0, label="clean")
    win = T.pick_winner([better_score_but_empty, clean], w)
    assert win.overrides == {"a": 2}


def test_pick_winner_falls_back_to_conservation_only_then_none():
    w = {c: 1.0 for c in O.COMPONENTS}
    v1 = _variant({"a": 1}, zero_weeks=1, label="z1")
    assert T.pick_winner([v1], w).overrides == {"a": 1}
    assert T.pick_winner([_variant({}, dropped=9)], w) is None
    assert T.pick_winner([], w) is None


# --------------------------------------------------------------------------- #
# The CONTRACT-FLOOR no-regression guard
#
# Measured 2026-08-12 on the operator's 7.29 PR: a 40-variant "Walk the line"
# controller search chose knobs that cut the worst harvest week 20,526 ->
# 16,185 fish. corr(worst week, score) over that pool was -0.03 — the emphasis
# objective scores no floor term at all, so the search could sell the hardest
# business rule for biomass/density gains. These lock the guard that stops it.
# --------------------------------------------------------------------------- #
def test_floor_eligible_keeps_only_non_regressing_candidates():
    worse = _variant({"a": 1}, harvest_min_week=16185.0, label="worse")
    same = _variant({"a": 2}, harvest_min_week=20526.0, label="same")
    better = _variant({"a": 3}, harvest_min_week=27462.0, label="better")
    keep = T.floor_eligible([worse, same, better], 20526.0)
    assert [v.label for v in keep] == ["same", "better"]


def test_floor_eligible_stands_down_without_a_measured_baseline():
    v = _variant({"a": 1}, harvest_min_week=100.0)
    assert T.floor_eligible([v], None) == []       # no baseline -> no guard
    assert T.floor_eligible([v], "n/a") == []      # unusable baseline
    # an UNMEASURED candidate (old cache entry) is never eligible: a gate is
    # only ever cleared by a measurement, never by a missing field
    assert T.floor_eligible([_variant({}, harvest_min_week=None)], 10.0) == []


def test_pick_winner_refuses_a_tuned_winner_that_lowers_the_worst_week():
    # The real 7.29 shape: the emphasis-best candidate regresses the floor,
    # a slightly worse-scoring one holds it.
    w = {c: 1.0 for c in O.COMPONENTS}
    best_score = _variant({"dev": 0.01}, harvest_min_week=16185.0,
                          label="score-best", harvest_var=0.0)
    holds_floor = _variant({"dev": 0.005}, harvest_min_week=21871.0,
                           label="holds", harvest_var=0.5)
    assert T.pick_winner([best_score, holds_floor], w).overrides == {"dev": 0.01}
    assert T.pick_winner([best_score, holds_floor], w,
                         stock_min_week=20526.0).overrides == {"dev": 0.005}


def test_pick_winner_guard_stands_down_when_nothing_holds_the_floor():
    # The guard must never return None / crash a search: if no candidate holds
    # the floor the emphasis-best still wins (and tune_method reports it).
    w = {c: 1.0 for c in O.COMPONENTS}
    a = _variant({"a": 1}, harvest_min_week=9000.0, label="a", harvest_var=0.0)
    b = _variant({"a": 2}, harvest_min_week=8000.0, label="b", harvest_var=0.5)
    assert T.pick_winner([a, b], w, stock_min_week=30000.0).overrides == {"a": 1}


def test_tune_method_refuses_global_engine_with_a_space():
    m = dataclasses.replace(M.REGISTRY["global-lp"],
                            knob_space=(("some_knob", (1, 2)),))
    with pytest.raises(NotImplementedError, match="controller engine"):
        T.tune_method(m, "in.xlsx", "cfg", "scen", emphasis="Balanced")


def test_tune_method_stock_only_and_gate_bound_run_nothing():
    g = M.REGISTRY["global-lp"]
    out = T.tune_method(g, "in.xlsx", "cfg", "scen", emphasis="Balanced")
    assert out["status"] == "stock-only" and out["variants"] == []
    out = T.tune_method(g, "in.xlsx", "cfg", "scen", emphasis="Balanced",
                        stock_hard_fails=["no_empty_week"])
    assert out["status"] == "gate-bound" and out["variants"] == []


def test_estimate_budget_and_cached_count():
    m = M.REGISTRY["controller-hybrid"]
    b = T.estimate_budget(m)
    assert b["grid"] == len(O.OPT_FULL_GRID)
    assert b["probe_if_gate_fails"] == len(T.probe_grid(m))
    assert b["max_total"] >= b["grid"] + b["probe_if_gate_fails"]
    g = M.REGISTRY["global-lp"]
    bg = T.estimate_budget(g)
    assert bg["grid"] == bg["probe_if_gate_fails"] == bg["verify"] == 0
    # cache-reuse counting uses the sweep's own key
    grid = T.search_grid(m)
    vc = {O._overrides_key(grid[0][1]): "x", O._overrides_key(grid[3][1]): "x"}
    assert T.cached_count(vc, grid) == 2
    assert T.cached_count(None, grid) == 0


# --------------------------------------------------------------------------- #
# Tuned-candidate labeling
# --------------------------------------------------------------------------- #
def test_tuned_label_excludes_pins_shows_chosen_knobs():
    pins = {"hybrid_follow": "full", "hybrid_follow_band": 0.05}
    win = {**pins, "tran_og_default_tanks": 3}
    lbl = T.tuned_label("Hybrid", win, pins)
    assert lbl == "Hybrid (tuned: tran_og_default_tanks=3)"
    assert T.tuned_label("Hybrid", dict(pins), pins) == "Hybrid (tuned)"
    assert T.tuned_label("Hybrid") == "Hybrid (tuned)"


# --------------------------------------------------------------------------- #
# Promote -> Quick-run round-trip with a GLOBAL method + overrides
# --------------------------------------------------------------------------- #
def test_promote_quick_run_round_trip_global_method(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "control.yaml").write_text(
        "max_biomass_kg: 3800000.0\nglobal_buffer_pct: 0.05\n")
    # Promote a tuned GLOBAL candidate: method key + its winning overrides.
    overrides = {"global_buffer_pct": 0.07}
    A.save_promoted_default(str(cfg), method="global-lp", overrides=overrides,
                            promoted_ts="2026-08-09T12:00:00",
                            note="tuned tournament winner")
    promoted = A.load_promoted_default(str(cfg))
    assert promoted["method"] == "global-lp"
    assert promoted["overrides"] == overrides
    # The method key must resolve to a real registered method (the app falls
    # back to the default method otherwise — that would replay the WRONG plan).
    assert promoted["method"] in M.REGISTRY
    assert M.REGISTRY[promoted["method"]].engine == "global"
    # Quick run's exact mechanism: merge the promoted overrides into a temp
    # config copy, then run the promoted method against it.
    merged_dir = O.config_dir_with_overrides(str(cfg), promoted["overrides"])
    merged = yaml.safe_load(open(f"{merged_dir}/control.yaml"))
    assert merged["global_buffer_pct"] == 0.07
    assert merged["max_biomass_kg"] == 3800000.0       # untouched knobs kept
    # ...and the user's own config was never mutated.
    original = yaml.safe_load(open(cfg / "control.yaml"))
    assert original["global_buffer_pct"] == 0.05


def test_promoted_evidence_survives_round_trip(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    A.save_promoted_default(
        str(cfg), method="controller-hybrid",
        overrides={"tran_og_default_tanks": 3},
        promoted_ts="2026-08-09T12:00:00", note="",
        evidence={"gates": {"conservation": "PASS"}, "score": 1.23,
                  "emphasis": "Walk the line"})
    p = A.load_promoted_default(str(cfg))
    assert p["evidence"]["gates"]["conservation"] == "PASS"
    assert p["overrides"] == {"tran_og_default_tanks": 3}


# --------------------------------------------------------------------------- #
# The RELIEF-CEILING no-breach guard
#
# Measured 2026-08-13 across 8 starting states / 717 conserved plans. The
# weekly processing limit is carried in the objective by ONE term,
# `harvest_overshoot`, and its gate is SOFT — so `pick_winner` never filtered
# on it. The shipped "Product quality" preset sets that term's weight to 0
# (every other preset: 0.5-2), which left NOTHING in the search able to see a
# breach: its winners planned 82,181 / 83,152 / 82,626 / 83,504-fish weeks on
# 4 of 8 states — ~50% over the 55,000 limit, 36-38% over the 60,500 ceiling
# the config calls never legal. "Walk the line" breached on none, purely
# because its weight for that one term happens to be 2.
#
# A constraint enforced by a single weight in a single preset is not enforced.
# Hence a RANK, like the contract floor: emphasis-independent by construction.
# --------------------------------------------------------------------------- #

def test_ceiling_eligible_keeps_only_plans_inside_the_relief_ceiling():
    inside = _variant({"a": 1}, weeks_over_relief_ceiling=0, label="ok")
    over = _variant({"a": 2}, weeks_over_relief_ceiling=2, label="breach")
    got = T.ceiling_eligible([inside, over])
    assert [v.label for v in got] == ["ok"]


def test_ceiling_eligible_treats_unmeasured_as_unknown_never_a_pass():
    """An old cache entry predating the field must not be assumed compliant —
    the same rule the floor guard and variant_hard_ok already follow."""
    assert T.ceiling_eligible(
        [_variant({}, weeks_over_relief_ceiling=None)]) == []


def test_a_ceiling_breach_can_never_win_however_good_its_score():
    """THE control: the Product-quality failure in one assertion. The breaching
    plan is given the better emphasis score, exactly as it had in the real
    searches (nothing in that objective could see the breach)."""
    w = {c: 1.0 for c in O.COMPONENTS}
    breach = _variant({"a": 1}, weeks_over_relief_ceiling=2, label="breach",
                      harvest_var=0.0, crowded_biomass_fraction=0.0)
    legal = _variant({"a": 2}, weeks_over_relief_ceiling=0, label="legal")
    assert T.pick_winner([breach, legal], w).overrides == {"a": 2}


def test_the_ceiling_guard_stands_down_rather_than_returning_nothing():
    """If every candidate breaches, a search still reports its best — the board
    shows the failure honestly rather than the tuner going silent."""
    w = {c: 1.0 for c in O.COMPONENTS}
    a = _variant({"a": 1}, weeks_over_relief_ceiling=1, label="a")
    b = _variant({"a": 2}, weeks_over_relief_ceiling=3, label="b")
    assert T.pick_winner([a, b], w) is not None


def test_the_ceiling_outranks_the_floor():
    """Priority order: a week over the processing ceiling cannot be executed at
    all; a lean week is a shortfall. So a legal-but-leaner plan beats an
    illegal-but-fuller one."""
    w = {c: 1.0 for c in O.COMPONENTS}
    illegal_but_full = _variant({"a": 1}, weeks_over_relief_ceiling=1,
                                harvest_min_week=30000.0, label="illegal")
    legal_but_lean = _variant({"a": 2}, weeks_over_relief_ceiling=0,
                              harvest_min_week=20000.0, label="legal")
    win = T.pick_winner([illegal_but_full, legal_but_lean], w,
                        stock_min_week=25000.0)
    assert win.overrides == {"a": 2}
