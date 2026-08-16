"""Analyze's ✅ Adopt / ⭐ Promote may not save a rule-breaking plan SILENTLY.

Three doors lead from a search to the operator's live config. Two were already
closed:

  * `tournament.pick_winner` — hard gates, then the relief ceiling, then the
    contract floor, each STANDING DOWN rather than emptying the pool;
  * `optimize.recommend` — the same three, by IMPORTING those predicates.

The third was Analyze's card. It selects with `analysis.rank_key`, which RANKS
by gate-failure counts instead of FILTERING on them — and `harvest_cap` (the
weekly processing limit and its relief ceiling) is registered `hard=False`, so
a ceiling breach lowered a plan's rank and could still be adopted. ✅ Adopt
writes the winning knobs into control.yaml; ⭐ Promote writes method + knobs
into analysis_defaults.yaml, which is exactly what ⚡ Quick run replays.

The fix is NOT to hide the button: this is the operator's decision surface,
not an automatic winner-pick, and a guard they cannot override on their own
judgement would be wrong here. It is to make the breach impossible to trip
without noticing, and to record it with whatever gets saved.

Every test here fails on the parent commit (the functions did not exist).
"""
from __future__ import annotations

import dataclasses
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import forecast.analysis as A          # noqa: E402
import forecast.optimize as O          # noqa: E402
import forecast.tournament as T        # noqa: E402


# --------------------------------------------------------------------------- #
# A graded Analyze candidate, in the shape the app's board builds
# --------------------------------------------------------------------------- #
def _metrics(*, zero_weeks=0, ceiling_weeks=0, min_week=20000.0):
    m = O._infeasible_metrics()
    m = dataclasses.replace(m, **{c: 1.0 for c in O.COMPONENTS})
    return dataclasses.replace(m, harvest_zero_weeks=zero_weeks,
                               weeks_over_relief_ceiling=ceiling_weeks,
                               harvest_min_week=min_week)


def _gates(*, conservation="PASS", no_empty_week="PASS", harvest_cap="PASS",
           extra=()):
    """The registry's own shape: [{key, label, hard, status, detail}]."""
    rows = [
        {"key": "conservation", "label": "Conservation (no fish created or lost)",
         "hard": True, "status": conservation, "detail": "0 dropped / 0 over-produced"},
        {"key": "no_empty_week", "label": "Never an empty harvest week",
         "hard": True, "status": no_empty_week, "detail": "harvests something every week"},
        {"key": "harvest_cap", "label": "Weekly processing limit + relief",
         "hard": False, "status": harvest_cap, "detail": "no week over the limit"},
    ]
    rows.extend(extra)
    return rows


def _cand(label="plan", *, key="controller", overrides=None, metrics=None,
          gates=None, dropped=0, overprod=0):
    """A candidate that is ELIGIBLE on every rule unless a test breaks one."""
    return {
        "key": key,
        "label": label,
        "overrides": dict(overrides or {}),
        "metrics": metrics if metrics is not None else _metrics(),
        "gates": gates if gates is not None else _gates(),
        "res": {"ok": True,
                "_score": {"verdict": {"dropped": dropped, "overprod": overprod}}},
    }


# --------------------------------------------------------------------------- #
# The negative controls: what may not be adopted without a confirmation
# --------------------------------------------------------------------------- #
def test_a_relief_ceiling_breach_is_a_breach_even_though_its_gate_is_soft():
    """THE GAP. `harvest_cap` is hard=False, so `rank_key` ranked a
    ceiling-breaching plan down and adopted it anyway. Both other doors refuse
    to crown it."""
    c = _cand(metrics=_metrics(ceiling_weeks=4),
              gates=_gates(harvest_cap="FAIL"))
    br = A.adoption_breaches(c)
    assert br, "a relief-ceiling breach must block a silent adopt"
    assert any("relief ceiling" in b for b in br)
    assert any("4 week(s)" in b for b in br)
    assert A.adoption_blocked(br, acknowledged=False) is True


def test_an_empty_harvest_week_is_a_breach():
    """Never-an-empty-week is the contract, not a preference."""
    c = _cand(metrics=_metrics(zero_weeks=3), gates=_gates(no_empty_week="FAIL"))
    br = A.adoption_breaches(c)
    assert any("empty harvest week" in b for b in br)
    assert A.adoption_blocked(br, acknowledged=False) is True


def test_a_conservation_failure_is_a_breach():
    c = _cand(dropped=875, gates=_gates(conservation="FAIL"))
    br = A.adoption_breaches(c)
    assert any("conserve" in b for b in br)


def test_a_clean_plan_adopts_with_no_friction():
    """No crying wolf: a guard that fires on ordinary plans trains the operator
    to click through it, which is worse than no guard at all."""
    c = _cand()
    assert A.adoption_breaches(c) == []
    assert A.adoption_blocked([], acknowledged=False) is False


def test_an_unmeasured_plan_is_never_read_as_a_pass():
    """An ungraded candidate (metrics lost to schema drift / a failed grade)
    must read UNKNOWN on every guarded measurement, never clean."""
    c = _cand()
    c["metrics"] = None
    c["res"] = {"ok": True, "_score": {}}
    br = A.adoption_breaches(c)
    assert any("never measured" in b for b in br)
    # ...and specifically not just the empty-week one: the ceiling and the
    # floor default to 0 on the sentinel, which would read as a clean sweep.
    assert sum(1 for b in br if "never measured" in b) >= 2


# --------------------------------------------------------------------------- #
# The contract-floor baseline: the candidate's OWN stock run, or nothing
# --------------------------------------------------------------------------- #
def test_a_tuned_plan_that_regresses_its_own_stock_worst_week_is_a_breach():
    """The measured 7.29 shape: a tuned knob set cut the worst harvest week
    20,526 -> 16,185 fish and the emphasis score never noticed."""
    stock = _cand("Controller", key="controller",
                  metrics=_metrics(min_week=20526.0))
    tuned = _cand("Controller (tuned: dev=0.01)", key="controller",
                  overrides={"facility_biomass_deviation_pct": 0.01},
                  metrics=_metrics(min_week=16185.0))
    base = A.stock_reference_min_week(tuned, [stock, tuned])
    assert base == 20526.0
    br = A.adoption_breaches(tuned, base)
    assert any("16,185" in b and "20,526" in b for b in br)


def test_the_floor_guard_is_off_without_the_methods_own_stock_leg():
    """A guard never invents a baseline. A stock candidate is its own
    reference; a tuned one whose stock leg isn't on the board has none."""
    stock = _cand("Controller", key="controller",
                  metrics=_metrics(min_week=20526.0))
    assert A.stock_reference_min_week(stock, [stock]) is None

    lonely = _cand("Global LP (tuned: x=1)", key="global-lp",
                   overrides={"x": 1}, metrics=_metrics(min_week=1.0))
    assert A.stock_reference_min_week(lonely, [stock, lonely]) is None
    assert A.adoption_breaches(lonely, None) == []      # guard off, not failed


def test_the_floor_baseline_never_comes_from_a_different_method():
    """Two methods' worst weeks are not comparable — the floor guard asks
    whether TUNING regressed THIS method."""
    other = _cand("Global LP", key="global-lp", metrics=_metrics(min_week=90000.0))
    tuned = _cand("Controller (tuned)", key="controller", overrides={"a": 1},
                  metrics=_metrics(min_week=20000.0))
    assert A.stock_reference_min_week(tuned, [other, tuned]) is None


def test_an_unmeasured_stock_worst_week_does_not_arm_the_floor():
    stock = _cand("Controller", key="controller", metrics=_metrics(min_week=None))
    tuned = _cand("Controller (tuned)", key="controller", overrides={"a": 1},
                  metrics=_metrics(min_week=1.0))
    assert A.stock_reference_min_week(tuned, [stock, tuned]) is None


# --------------------------------------------------------------------------- #
# One implementation, not two
# --------------------------------------------------------------------------- #
def test_the_predicates_are_not_a_second_copy(monkeypatch):
    """Adoption must REUSE the tournament's predicates. Disabling one AT THE
    TOURNAMENT changes the adoption verdict — proof there is no private
    duplicate here quietly enforcing (or one day not enforcing) its own
    version of the rule."""
    c = _cand(metrics=_metrics(ceiling_weeks=4))
    assert A.adoption_breaches(c)

    monkeypatch.setattr(T, "ceiling_eligible", lambda vs: list(vs))
    monkeypatch.setattr(O, "ineligibility_reasons",
                        lambda v, b=None: ([] if T.ceiling_eligible([v]) else ["x"]))
    assert A.adoption_breaches(c) == []          # the rule came from elsewhere


def test_adoption_agrees_with_the_other_two_doors_on_the_same_plan():
    """Same plan, same rules: what `pick_winner`/`recommend` refuse to crown is
    what Adopt refuses to save silently. A divergence here is the drift this
    reuse exists to prevent."""
    breach = _cand("breach", metrics=_metrics(ceiling_weeks=2))
    v = A.adoption_variant(breach)
    assert T.ceiling_eligible([v]) == []                     # tournament refuses
    assert O.ineligibility_reasons(v)                        # optimize refuses
    assert A.adoption_breaches(breach)                       # so does adoption


def test_a_hard_gate_registered_later_guards_this_door_automatically():
    """Registry, not rewrite: a new hard gate must protect adoption the day it
    is registered, without editing the adoption code."""
    future = {"key": "future_rule", "label": "Some future hard rule",
              "hard": True, "status": "FAIL", "detail": "broke it 7 times"}
    c = _cand(gates=_gates(extra=[future]))
    br = A.adoption_breaches(c)
    assert any("Some future hard rule" in b and "7 times" in b for b in br)


def test_a_soft_gate_failure_alone_does_not_block_adoption():
    """Only HARD rules (plus the imported ceiling/floor ranks) need a
    confirmation — a WARN-heavy plan is a normal operator choice."""
    soft = {"key": "handling_budget", "label": "Weekly handling budget",
            "hard": False, "status": "FAIL", "detail": "3 weeks over budget"}
    c = _cand(gates=_gates(extra=[soft]))
    assert A.adoption_breaches(c) == []


# --------------------------------------------------------------------------- #
# The operator can always overrule — but never by accident
# --------------------------------------------------------------------------- #
def test_an_acknowledged_breach_is_allowed_through():
    """The button must not vanish: the operator can see the plan and may have
    a reason. Explicit confirmation unblocks it."""
    br = A.adoption_breaches(_cand(metrics=_metrics(ceiling_weeks=1)))
    assert br
    assert A.adoption_blocked(br, acknowledged=True) is False


# --------------------------------------------------------------------------- #
# What is saved must say what was accepted
# --------------------------------------------------------------------------- #
def test_the_promoted_default_records_the_accepted_breach(tmp_path):
    """⚡ Quick run replays the promoted default; whoever presses it later was
    not in the session that accepted the breach."""
    c = _cand("Controller (tuned)", overrides={"a": 1},
              metrics=_metrics(ceiling_weeks=2))
    br = A.adoption_breaches(c)
    A.save_promoted_default(
        str(tmp_path), method="controller", overrides=c["overrides"],
        promoted_ts="2026-08-15T10:00:00",
        note="⚠ ACCEPTED WITH A KNOWN RULE BREACH — won analysis on PR.xlsm",
        evidence={"gates": {g["key"]: g["status"] for g in c["gates"]},
                  "breaches": br, "accepted_with_breach": True})

    got = A.load_promoted_default(str(tmp_path))
    assert got["evidence"]["accepted_with_breach"] is True
    assert any("relief ceiling" in b for b in got["evidence"]["breaches"])
    assert "BREACH" in got["note"]


def test_a_clean_promotion_records_no_breach(tmp_path):
    c = _cand()
    A.save_promoted_default(
        str(tmp_path), method="controller", overrides={},
        promoted_ts="2026-08-15T10:00:00", note="won analysis on PR.xlsm",
        evidence={"breaches": A.adoption_breaches(c),
                  "accepted_with_breach": False})
    got = A.load_promoted_default(str(tmp_path))
    assert got["evidence"]["accepted_with_breach"] is False
    assert got["evidence"]["breaches"] == []
    assert "BREACH" not in got["note"]


def test_the_adoption_log_carries_the_breach(tmp_path):
    """✅ Adopt's durable artifact is control.yaml, which holds knobs and no
    verdict — so the decision itself is logged beside it."""
    log = str(tmp_path / "adoption_history.jsonl")
    c = _cand("Controller (tuned)", overrides={"density_target_pct": 0.99},
              metrics=_metrics(ceiling_weeks=4), gates=_gates(harvest_cap="FAIL"))
    rec = A.adoption_record(c, ts="2026-08-15T10:00:00", action="adopt",
                            method="controller", overrides=c["overrides"],
                            breaches=A.adoption_breaches(c),
                            source="Analyze (balanced) on PR.xlsm")
    A.append_adoption_log(rec, log)

    back = A.read_adoption_log(log)
    assert len(back) == 1
    assert back[0]["action"] == "adopt"
    assert back[0]["accepted_with_breach"] is True
    assert back[0]["overrides"] == {"density_target_pct": 0.99}
    assert back[0]["gates"]["harvest_cap"] == "FAIL"
    assert any("relief ceiling" in b for b in back[0]["breaches"])


def test_a_clean_adoption_is_logged_without_a_breach(tmp_path):
    log = str(tmp_path / "adoption_history.jsonl")
    c = _cand()
    A.append_adoption_log(
        A.adoption_record(c, ts="2026-08-15T10:00:00", action="promote",
                          method="controller", overrides={},
                          breaches=A.adoption_breaches(c)), log)
    back = A.read_adoption_log(log)
    assert back[0]["accepted_with_breach"] is False
    assert back[0]["breaches"] == []


def test_a_logging_failure_never_blocks_an_adoption(tmp_path):
    """Best-effort by design: the record must not become a new way for a legal
    plan to be un-saveable."""
    A.append_adoption_log({"ts": "x"}, str(tmp_path / "no" / "such" / "dir.jsonl"))
    assert A.read_adoption_log(str(tmp_path / "nope.jsonl")) == []


# --------------------------------------------------------------------------- #
# The app layer: the buttons consult the guard
# --------------------------------------------------------------------------- #
app = pytest.importorskip("app", reason="app.py not importable without Streamlit")


def test_the_app_refuses_an_unacknowledged_breach_and_says_why():
    c = _cand("Controller (tuned)", metrics=_metrics(ceiling_weeks=2))
    c["breaches"] = A.adoption_breaches(c)
    msg = app._adoption_refusal(c, acknowledged=False)
    assert msg is not None
    assert "Nothing was saved" in msg
    assert "relief ceiling" in msg          # names the breach, not just "a rule"


def test_the_app_lets_an_acknowledged_breach_through():
    c = _cand("Controller (tuned)", metrics=_metrics(ceiling_weeks=2))
    c["breaches"] = A.adoption_breaches(c)
    assert app._adoption_refusal(c, acknowledged=True) is None


def test_the_app_never_stops_a_clean_plan():
    c = _cand()
    c["breaches"] = A.adoption_breaches(c)
    assert app._adoption_refusal(c, acknowledged=False) is None
    assert app._adoption_refusal(c, acknowledged=True) is None


def test_the_refusal_does_not_call_a_soft_gate_a_hard_rule():
    """WORDING, and it matters. The findings list is deliberately WIDER than
    the hard gates — its whole reason to exist is the relief ceiling, whose
    checklist gate is SOFT — and it also carries 'never measured' entries,
    which are UNKNOWNs rather than breaches. Calling all of that 'hard rules'
    contradicts the checklist the operator is reading two inches above it."""
    c = _cand("Controller (tuned)", metrics=_metrics(ceiling_weeks=2))
    c["breaches"] = A.adoption_breaches(c)
    msg = app._adoption_refusal(c, acknowledged=False)
    assert msg and "Nothing was saved" in msg
    assert "hard rule" not in msg.lower(), msg
    # ...and the ceiling breach is still named, in the operator's own terms.
    assert "relief ceiling" in msg.lower()


def test_the_findings_panel_explains_the_soft_gate_and_the_unknowns():
    """The panel is where an operator decides whether to overrule. It has to
    say why this list is wider than the checklist, and that an unmeasured
    value is never read as a pass."""
    import inspect
    src = inspect.getsource(app._adoption_gate)
    low = src.lower()
    assert "soft" in low, "must say the relief-ceiling gate is soft"
    assert "never measured" in low or "unknown" in low
    assert "hard rule(s)" not in low


def test_every_write_door_in_analyze_consults_the_guard():
    """A wiring guard. The rules above are pure and well tested; what a future
    edit can still silently remove is the CALL from a button handler — and
    there are three (card Adopt, card Promote, promote picker)."""
    import inspect
    src = inspect.getsource(app._analyze)
    assert src.count("_adoption_refusal(") >= 3
    assert src.count("_adoption_gate(") >= 2      # the card's, the picker's
    assert "adoption_breaches(" in src            # computed for every candidate
    # ...and the breach reaches the durable records, not just the screen.
    assert "append_adoption_log(" in src or "_log_adoption(" in src
    assert "accepted_with_breach" in src
