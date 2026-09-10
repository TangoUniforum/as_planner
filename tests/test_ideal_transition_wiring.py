"""Step 3's what-if limits in the app: which schedule gets them, and when a
box counts as an edit.

The engine side is covered in test_ideal_engine (an override reaches the
run's control.yaml). This covers the app wiring a review found unguarded:
today's plan must run at the CURRENT limits and the proposal at the what-if
ones, and a box nobody moved — including when Control's cap is not a whole
number of tonnes, or lies outside the box's range — must send nothing.
"""
import pytest

app = pytest.importorskip("app", reason="app.py not importable without Streamlit")


class _Ctrl:
    def __init__(self, cap=3_800_000.0, hmax=55_000.0, hmin=26_000.0,
                 wmin=3_200.0, feed=33_000.0):
        self.max_biomass_kg = cap
        self.max_harvest_per_week = hmax
        self.min_harvest_per_week = hmin
        self.min_harvest_weight_g = wmin
        self.max_feed_per_day_kg = feed


def test_untouched_boxes_send_nothing_even_when_the_cap_is_not_whole_tonnes():
    seeds = app._ideal_tr_seeds(_Ctrl(cap=3_849_600.0))   # box shows 3,850 t
    assert app._ideal_tr_overrides(seeds, dict(seeds)) == {}


def test_a_moved_box_sends_exactly_that_limit_in_control_units():
    seeds = app._ideal_tr_seeds(_Ctrl())
    moved = dict(seeds, ideal_tr_cap=4200, ideal_tr_hmin=30_000)
    assert app._ideal_tr_overrides(seeds, moved) == {
        "max_biomass_kg": 4_200_000.0, "min_harvest_per_week": 30_000.0}


def test_a_control_cap_outside_the_box_range_is_clamped_and_not_sent():
    assert app._ideal_tr_seeds(_Ctrl(cap=0.0))["ideal_tr_cap"] == 500
    assert app._ideal_tr_seeds(_Ctrl(cap=12_000_000.0))["ideal_tr_cap"] == 10_000
    seeds = app._ideal_tr_seeds(_Ctrl(cap=0.0))
    # Untouched, Control's own "no cap" stands — the clamp is display only.
    assert app._ideal_tr_overrides(seeds, dict(seeds)) == {}


def test_the_limit_text_uses_box_labels_and_units_not_engine_keys():
    txt = app._ideal_limit_text({"max_biomass_kg": 4_200_000.0}, _Ctrl())
    assert "Biomass cap 4,200 t" in txt and "Control 3,800 t" in txt
    assert "max_biomass_kg" not in txt


def test_today_runs_at_current_limits_and_the_proposal_at_the_what_if(monkeypatch):
    from forecast import ideal_engine
    calls = []

    def fake_run(batches, root, **kw):
        calls.append((batches, kw))
        return kw

    monkeypatch.setattr(ideal_engine, "run_schedule", fake_run)
    monkeypatch.setattr(app, "_cpu_workers", lambda: 1)   # no process pool here
    _a, _b, note = app._ideal_transition_runs(
        ["today"], ["proposal"], b"pr", "pr.xlsm", "controller",
        {"chronic_pressure_weeks": 6}, {"max_biomass_kg": 4_200_000.0})
    (ba, ka), (bb, kb) = calls
    assert ba == ["today"] and bb == ["proposal"]
    assert not ka.get("overrides")                      # where you are
    assert kb["overrides"] == {"max_biomass_kg": 4_200_000.0}   # where you'd go
    assert ka["method_overrides"] == kb["method_overrides"] == {
        "chronic_pressure_weeks": 6}                    # same engine both sides
    assert note is None
