"""Merging modes must not quietly lose what only those modes could do.

Analyze, Compare & Choose and Optimize were merged into one **Decide** mode on
2026-08-31. They answered three views of ONE question — "which plan should I
run?" — and two of them ran the same engine legs over the same roster writing
the same cache, so the split cost the operator a decision without buying
anything.

The merge is a WRAPPER: the three functions are unchanged and rendered as tabs.
That is deliberate. This repo's documented failure mode is retiring a mode and
losing the capabilities that lived only inside it (USER_GUIDE 13.1 exists
because of it), and the mode audit found thirteen such capabilities in Compare
and Optimize alone. A wrapper cannot drop them; a future deeper integration
could, and these tests are what would catch it.
"""
import io
import re

import pytest

SRC = io.open("app.py", encoding="utf-8").read()


def test_the_mode_list_is_five_and_offers_decide():
    m = re.search(r'app_mode = st\.radio\(\s*"Mode",\s*\[(.*?)\]', SRC, re.S)
    assert m, "could not find the mode radio"
    opts = re.findall(r'"([^"]+)"', m.group(1))
    assert len(opts) == 5, f"expected 5 modes, found {len(opts)}: {opts}"
    assert any(o.startswith("Decide") for o in opts)
    for gone in ("Analyze (", "Compare & Choose", "Optimize ("):
        assert not any(o.startswith(gone.rstrip(" (")) for o in opts), gone


def test_decide_renders_all_three_as_tabs():
    """The wrapper must actually call all three, or a capability is unreachable
    however present its code is."""
    body = SRC[SRC.index("def _decide():"):SRC.index("def _analyze():")]
    for fn in ("_analyze()", "_compare_and_choose()", "_optimizer()"):
        assert fn in body, f"Decide does not render {fn}"
    assert "st.tabs(" in body


def test_the_three_functions_still_exist_untouched():
    for fn in ("def _analyze():", "def _compare_and_choose():", "def _optimizer():"):
        assert fn in SRC, fn


def test_a_stored_retired_mode_name_does_not_crash_the_radio():
    """Streamlit raises when a stored selection is outside the options, so the
    three retired names must be migrated BEFORE the radio instantiates — the
    same bug the Tune retirement had to fix."""
    i_mig = SRC.index("_MERGED_INTO_DECIDE")
    i_radio = SRC.index('app_mode = st.radio(')
    assert i_mig < i_radio, "the migration must run before the radio renders"
    for old in ("Analyze", "Compare", "Optimize"):
        assert f'"{old}"' in SRC[i_mig:i_mig + 600], old


# --- the thirteen capabilities the mode audit found ------------------------
# Each entry: (what it is, a marker that proves it is still reachable).
CAPABILITIES = [
    ("between-system CV lens", '"Most balanced across systems"'),
    ("within-system CV lens", '"Most even within systems"'),
    ("tank footprint lens", '"Smallest tank footprint"'),
    ("fastest-run lens", '"Fastest run"'),
    ("raw density peak lens", '"Tightest density"'),
    ("welfare / crowded-biomass lens", '"Best welfare / product quality"'),
    ("fewest-moves lens", '"Fewest fish moves"'),
    ("steadiest-harvest lens", '"Steadiest harvest"'),
    ("pick a plan without writing config", "_chosen_method"),
    ("force re-run / cache invalidation", "store.clear()"),
    ("CP-SAT solve-depth control", "cpsat_depth"),
    ("per-method metric readout", "_BOARD_LENSES"),
    ("emphasis presets on the knob sweep", "EMPHASIS"),
]


@pytest.mark.parametrize("what,marker", CAPABILITIES,
                         ids=[c[0] for c in CAPABILITIES])
def test_capability_survives_the_merge(what, marker):
    assert marker in SRC, (
        f"'{what}' lost in the mode merge — its marker {marker!r} is gone from "
        f"app.py. Retiring a mode in this repo requires a dated record of where "
        f"each capability went (USER_GUIDE 13.1 precedent); losing one silently "
        f"is the failure this test exists to prevent.")


def test_the_retirement_is_documented():
    """House rule: a retired mode gets a dated section saying where its
    capabilities went. Two retirements now — Tune, and these three."""
    guide = io.open("docs/USER_GUIDE.md", encoding="utf-8").read()
    assert "13.1 Tune mode retired" in guide
    assert re.search(r"13\.4[^\n]*merged into Decide", guide), (
        "USER_GUIDE needs a dated section recording the Analyze / Compare & "
        "Choose / Optimize merge")


def test_decide_leads_with_the_cheap_question():
    """The monthly lever check is 2 minutes and most months says 'keep what you
    have'; the engine tournament is far heavier. Recommend must come first."""
    body = SRC[SRC.index("def _decide():"):SRC.index("def _analyze():")]
    order = [body.index(x) for x in ("_analyze()", "_compare_and_choose()",
                                     "_optimizer()")]
    assert order == sorted(order), "tab order should be recommend -> compare -> tune"
