"""The user-facing explanations must tell the truth about THIS install.

Two defect classes are locked out here.

1. A HARDCODED RULEBOOK. The How-it-works page is static prose, but its
   numbers (the harvest floor, the processing limit and its DERIVED relief
   ceiling, the caps, the handling budget) are read live from
   config/control.yaml via app._hiw_knobs — so retuning a knob can never leave
   the rulebook describing a facility that no longer exists.

2. PROSE THAT ASSERTS A CONFIG VALUE. The Control tooltips used to end with
   "Current setting: X" — a static string claiming a live value, which rotted
   on every retune (the 2026-08-12 sweep found density_target_pct claiming
   0.85 against a config holding 0.9, and the welfare line claiming 80 against
   85). Tooltips may say what a knob DOES; only the config may say what it is
   SET to, and _ctl_help appends that at render time.

Plus a regression net over the rulebook's CONTENT: phrases that were true once
and are not any more must not come back.
"""
import ast
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = pytest.importorskip("app", reason="app.py not importable without Streamlit")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.skipif(
    not os.path.isdir(os.path.join(ROOT, "config")),
    reason="config/ not seeded",
)


# --------------------------------------------------------------------------- #
# 1. The page's numbers track the live config
# --------------------------------------------------------------------------- #
def test_page_numbers_track_the_live_control_config():
    """DEFECT CLASS this locks out: a hardcoded rulebook. The page's numbers
    must equal whatever control.yaml currently says — the processing limit,
    the relief fraction and its DERIVED ceiling, the contract floor, the
    handling budget, the caps."""
    from forecast.config_io import load_control
    c = load_control(os.path.join(ROOT, "config"))
    k = app._hiw_knobs()
    assert k["min_hv"] == float(c.min_harvest_per_week)
    assert k["max_hv"] == float(c.max_harvest_per_week)
    assert k["relief_pct"] == float(c.harvest_relief_pct)
    assert k["ceiling_hv"] == pytest.approx(
        float(c.max_harvest_per_week) * (1.0 + float(c.harvest_relief_pct)))
    assert k["moves"] == int(c.max_transfers_per_week)
    assert k["bio_cap"] == float(c.max_biomass_kg)
    assert k["feed_cap"] == float(c.max_feed_per_day_kg)
    assert k["min_tank"] == float(c.min_tank_control)
    assert k["min_wt"] == float(c.min_harvest_weight_g)


def test_floor_limit_ceiling_ordering_in_the_shipped_config():
    """The limit+relief model's own invariant, as the page states it: the
    contract floor <= the processing limit <= the derived relief ceiling
    (30,000 <= 55,000 <= 60,500 on the shipped config). The ceiling is
    strictly above the limit whenever a relief band exists at all."""
    k = app._hiw_knobs()
    assert k["min_hv"] <= k["max_hv"] <= k["ceiling_hv"]
    if k["relief_pct"] > 0:
        assert k["ceiling_hv"] > k["max_hv"]


def test_knobs_survive_a_missing_config(monkeypatch, tmp_path):
    """The page must render before any config is seeded (first-launch state):
    _hiw_knobs falls back to the documented defaults instead of raising."""
    monkeypatch.setattr(app, "CONFIG_DIR", tmp_path / "nonexistent")
    k = app._hiw_knobs()
    assert k["min_hv"] > 0 and k["ceiling_hv"] >= k["max_hv"] > 0


def test_harvest_limit_reads_the_live_config_and_never_raises(monkeypatch,
                                                              tmp_path):
    """Readouts that name the weekly limit (Optimize's 'Weeks over N' metric,
    the trade-off map axis) must read it, not hardcode 55k."""
    from forecast.config_io import load_control
    c = load_control(os.path.join(ROOT, "config"))
    assert app._harvest_limit() == float(c.max_harvest_per_week)
    monkeypatch.setattr(app, "CONFIG_DIR", tmp_path / "nonexistent")
    assert app._harvest_limit(default=1234.0) == 1234.0


# --------------------------------------------------------------------------- #
# 2. Tooltips describe behaviour; only the config states a value
# --------------------------------------------------------------------------- #
def test_no_control_tooltip_hardcodes_a_config_value():
    """The staleness generator. A tooltip that writes 'Current setting: X' is
    asserting a live value from a static string and WILL drift — it already
    had, twice, when this test was written."""
    offenders = [k for k, v in app._CONTROL_HELP.items()
                 if "current setting" in v.lower() or "setting:" in v.lower()]
    assert not offenders, (
        f"{offenders} hardcode a config value in their tooltip. Say what the "
        f"knob DOES; _ctl_help(k, value) appends what it is SET to.")


def test_every_control_knob_has_a_label_and_a_tooltip():
    """A knob an operator can edit but not understand is a defect. The three
    sets must agree exactly — no orphan help for a deleted knob either."""
    from forecast.config_io import load_control, control_to_dict
    live = set(control_to_dict(load_control(os.path.join(ROOT, "config"))))
    assert live == set(app._CONTROL_HELP), (
        f"missing tooltips: {sorted(live - set(app._CONTROL_HELP))}; "
        f"stale tooltips: {sorted(set(app._CONTROL_HELP) - live)}")
    assert live == set(app._CONTROL_LABEL), (
        f"missing labels: {sorted(live - set(app._CONTROL_LABEL))}; "
        f"stale labels: {sorted(set(app._CONTROL_LABEL) - live)}")


def test_deleted_knob_cannot_reappear_in_the_ui():
    """harvest_target_per_week was deleted (it is also on the untunable list
    so no search space can resurrect it). It must not come back as a tooltip
    or a label either."""
    assert "harvest_target_per_week" not in app._CONTROL_HELP
    assert "harvest_target_per_week" not in app._CONTROL_LABEL


@pytest.mark.parametrize("value,expected", [
    (True, "on"), (False, "off"), (None, "blank (auto)"), ("", "blank (auto)"),
    (55000.0, "55,000"), (0.005, "0.005"), (15, "15"), (130, "130"),
    ("greedy", "greedy"),
])
def test_ctl_fmt_renders_values_the_way_an_operator_reads_them(value, expected):
    assert app._ctl_fmt(value) == expected


def test_ctl_help_appends_the_live_value_and_omits_it_when_unknown():
    base = app._ctl_help("max_harvest_per_week")
    assert "Currently set to" not in base
    withval = app._ctl_help("max_harvest_per_week", 55000.0)
    assert withval.startswith(base)
    assert withval.endswith("Currently set to: 55,000.")


# --------------------------------------------------------------------------- #
# 3. The rulebook's CONTENT — renders, tracks config, and stays current
# --------------------------------------------------------------------------- #
class _FakeCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeSt:
    """Minimal Streamlit stand-in that records every string the page emits, so
    the rulebook's prose is assertable headlessly."""

    def __init__(self):
        self.text = []

    def _record(self, *args, **kw):
        for a in args:
            if isinstance(a, str):
                self.text.append(a)
        for a in kw.values():
            if isinstance(a, str):
                self.text.append(a)
        return _FakeCtx()

    header = caption = markdown = subheader = write = info = _record

    def expander(self, label="", **kw):
        return self._record(label)

    @property
    def rendered(self):
        return "\n".join(self.text)


def _render_rulebook(monkeypatch, flat=False):
    """Render the page and return everything it emitted. `flat=True` collapses
    all whitespace, so an assertion about a phrase can't fail merely because
    the source happened to wrap it across two lines."""
    fake = _FakeSt()
    monkeypatch.setattr(app, "st", fake)
    app._how_it_works()
    out = fake.rendered
    return " ".join(out.split()) if flat else out


def test_rulebook_renders_and_shows_the_live_config_numbers(monkeypatch):
    """The page must actually render, and the numbers it prints must be the
    ones this install runs with — not prose someone typed once."""
    out = _render_rulebook(monkeypatch)
    k = app._hiw_knobs()
    assert len(out) > 4000, "the rulebook rendered suspiciously little"
    for label, num in (("floor", k["min_hv"]), ("limit", k["max_hv"]),
                       ("relief ceiling", k["ceiling_hv"]),
                       ("biomass cap", k["bio_cap"]),
                       ("feed cap", k["feed_cap"])):
        assert f"{num:,.0f}" in out, f"the {label} ({num:,.0f}) is not on the page"
    assert f"{k['moves']} transfer moves per week" in out or \
           f"{k['moves']}-move" in out, "the handling budget is not on the page"


STALE_PHRASES = [
    # harvest_target_per_week was deleted; the gate list must not resurrect it.
    "harvest floor/target/ceiling",
    # The Globals were repaired; they are no longer described as gate-bound,
    # and "all methods obey the same rules" was never true of them.
    "consume the same inputs and obey the same rules",
    # There are five manual event types, not four.
    "four event types",
]


@pytest.mark.parametrize("phrase", STALE_PHRASES)
def test_rulebook_does_not_contain_known_stale_phrasing(monkeypatch, phrase):
    """Regression net. Each of these was true of an earlier version of the
    tool and became false; they must not be reintroduced."""
    assert phrase.lower() not in _render_rulebook(monkeypatch, flat=True).lower()


def test_rulebook_states_the_global_limitations(monkeypatch):
    """The operator must be able to tell a benchmark from a runnable plan.
    These are the three things that decide it, all verified against the code:
    Global ignores the handling budget, does not enforce the full tier
    rulebook, and only its CP-SAT arm constrains per-tank density."""
    out = _render_rulebook(monkeypatch, flat=True).lower()
    assert "handling budget" in out and "never read" in out
    assert "topology violation" in out
    assert "r1, r5 and r7 are not checked" in out


def test_rulebook_names_all_five_manual_event_types(monkeypatch):
    """Layer 2 listed four; graded_harvest — the one with the staging-vs-
    harvest decision behind it — was the missing one."""
    out = _render_rulebook(monkeypatch)
    for t in ("harvest", "og_transfer", "og_to_6n", "graded_harvest",
              "fw_to_og"):
        assert t in out, f"manual event type {t!r} is not documented"


def test_rulebook_documents_every_registered_gate(monkeypatch):
    """The checklist an operator reads in Analyze and the gate list in the
    rulebook must not drift apart: adding a gate without documenting it is
    exactly how layer 9 went stale last time."""
    from forecast import analysis
    out = _render_rulebook(monkeypatch, flat=True).lower()
    for g in analysis.GATES:
        # match on the distinctive head of the label, not the whole string
        head = g.label.split("(")[0].strip().lower()
        assert head in out, f"gate {g.key!r} ({g.label!r}) is not in the rulebook"


# --------------------------------------------------------------------------- #
# 4. The sidebar mode selector
# --------------------------------------------------------------------------- #
def _mode_radio_call():
    """The sidebar radio lives in module-level page code that only runs inside
    a Streamlit script run, so inspect it structurally instead."""
    src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "radio"
                and any(isinstance(kw.value, ast.Constant)
                        and kw.value.value == "app_mode"
                        for kw in node.keywords if kw.arg == "key")):
            return node
    raise AssertionError("the sidebar Mode radio was not found in app.py")


def test_every_mode_has_a_caption_saying_what_it_is_for():
    """The operator asked for this one by name. A mode list with no per-mode
    explanation is the first thing a new user hits."""
    call = _mode_radio_call()
    opts = next(a for a in call.args if isinstance(a, ast.List))
    caps = next((kw.value for kw in call.keywords if kw.arg == "captions"), None)
    assert caps is not None, "the Mode selector has no per-mode captions"
    assert len(caps.elts) == len(opts.elts), (
        f"{len(opts.elts)} modes but {len(caps.elts)} captions — Streamlit "
        f"raises on a length mismatch")
    assert any(kw.arg == "help" for kw in call.keywords), \
        "the Mode selector lost its help text"


def test_mode_list_has_no_retired_modes():
    """Tune was retired; a stored selection outside the option list raises."""
    opts = next(a for a in _mode_radio_call().args if isinstance(a, ast.List))
    labels = [e.value for e in opts.elts]
    assert not any(l.startswith("Tune") for l in labels), labels
    for expected in ("Configure", "Run forecast", "Analyze",
                     "Compare & Choose", "Optimize", "How it works"):
        assert any(l.startswith(expected) for l in labels), \
            f"mode {expected!r} missing from {labels}"
