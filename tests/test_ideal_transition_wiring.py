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
    # No tank/system limits given -> none sent (the run stays Run forecast's).
    assert "density_overrides" not in ka and "density_overrides" not in kb
    assert "system_overrides" not in ka and "system_overrides" not in kb


def test_tank_and_system_limits_reach_the_proposal_only(monkeypatch):
    from forecast import ideal_engine
    calls = []
    monkeypatch.setattr(ideal_engine, "run_schedule",
                        lambda b, root, **kw: calls.append(kw) or kw)
    monkeypatch.setattr(app, "_cpu_workers", lambda: 1)
    app._ideal_transition_runs(
        ["today"], ["proposal"], b"pr", "pr.xlsm", "controller", {}, {},
        {"OG3N": 95.0}, {"OG3N": {"biomass": 450_000.0}})
    ka, kb = calls
    assert "density_overrides" not in ka and "system_overrides" not in ka
    assert kb["density_overrides"] == {"OG3N": 95.0}
    assert kb["system_overrides"] == {"OG3N": {"biomass": 450_000.0}}


def test_an_optimizer_cell_makes_the_check_buttons_proposal_call(monkeypatch,
                                                                 tmp_path):
    """One tool: a transition-optimizer cell runs the engine with EXACTLY the
    call the Check button's proposal arm makes — the same proposed batches,
    project, 208 weeks, manual events, method, promoted knobs, what-if limits
    (the cell's cap in place of the cap box) and tank/system tables. Only the
    PR's temp path differs."""
    import datetime as dt
    from types import SimpleNamespace
    from forecast import ideal_engine
    from forecast import transition as tr
    from forecast import transition_optimize as to
    from forecast.config_io import load_config
    from forecast.models import BatchInput
    calls = []
    monkeypatch.setattr(ideal_engine, "run_schedule",
                        lambda b, root, **kw: calls.append((b, root, kw))
                        or SimpleNamespace(years={}, audits={}))
    monkeypatch.setattr(app, "_cpu_workers", lambda: 1)
    fs = dt.date(2026, 9, 1)
    live = [BatchInput(
        batch_id=f"X{i}", input_date=dt.datetime(2026, 8, 1)
        + dt.timedelta(days=49 * i), input_count=570_000,
        tran_sf_date=None, tran_og_date=dt.datetime(2027, 7, 1)
        + dt.timedelta(days=49 * i), tran_og_count=340_000,
        tran_og_avg_wt_g=370.0, tran_og_cv=16.0, fcr_model="FCR_116_Quick",
        fw_correction=1.0, sgr_correction=1.0) for i in range(6)]
    proposed, _changes = tr.propose(live, fs, fs, [300_000])
    knobs = {"chronic_pressure_weeks": 6}
    dens, sys_ov = {"OG3N": 95.0}, {"OG3N": {"biomass": 450_000.0}}
    app._ideal_transition_runs(
        live, proposed, b"pr", "pr.xlsm", "controller", knobs,
        {"max_biomass_kg": 3_600_000.0, "min_harvest_per_week": 30_000.0},
        dens, sys_ov)
    pr = tmp_path / "pr.xlsm"
    pr.write_bytes(b"pr")
    to.run_cell((300_000, 3_600_000.0), str(app._ROOT), live=live,
                forecast_start=fs, cutoff=fs, pr_path=pr,
                overrides={"min_harvest_per_week": 30_000.0},
                method="controller", method_overrides=knobs,
                density_overrides=dens, system_overrides=sys_ov,
                control=load_config(str(app._ROOT / "config"))[0])
    (_b_a, _r_a, _kw_a), (b_arm, r_arm, kw_arm), (b_cell, r_cell, kw_cell) = calls
    assert b_cell == b_arm == proposed and r_cell == r_arm == str(app._ROOT)
    drop = lambda kw: {k: v for k, v in kw.items() if k != "pr_path"}
    assert drop(kw_cell) == drop(kw_arm)
    assert kw_cell["overrides"] == {"max_biomass_kg": 3_600_000.0,
                                    "min_harvest_per_week": 30_000.0}


def test_the_tank_limit_text_is_plain_units():
    txt = app._ideal_tank_limit_text(
        {"OG3N": 95.0}, {"OG3N": {"biomass": 450_000.0, "feed_per_day": 3500.0}},
        {"max_transfers_per_week": 20})
    assert "OG3N density 95 kg/m³" in txt and "OG3N biomass 450 t" in txt
    assert "OG3N feed 3,500 kg/day" in txt and "move budget 20/week" in txt
    assert app._ideal_tank_limit_text({}, {}, {}) == ""
