"""Configure -> Control editor: every knob still reaches a widget.

The editor renders one widget per field of the control dict and rebuilds the
config from what it collected, so a field that fails to render is not cosmetic
-- it is silently DROPPED from the saved config, because `control_from_dict`
sees only the keys the loop produced.

That risk rose when the inactive knobs moved into a collapsed expander (a
widget inside a container is easy to render into the wrong scope), and this
repo had no AppTest coverage at all, so nothing would have caught it.

The render happens in a SUBPROCESS, and that is not incidental. Importing a
Streamlit script (`import app`) executes it outside an AppTest context and
leaves a form context open, so the next AppTest render in the same interpreter
dies with "st.button() can't be used in an st.form()". Other modules in this
suite do import app, so an in-process render passes alone and fails in the full
run -- exactly the kind of order-dependent green that hides a real regression.
A clean interpreter makes the result independent of what ran before.

Labels are read from app.py with `ast` (parsed, never executed) for the same
reason.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest        # noqa: E402

from forecast.config_io import control_to_dict, load_control  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
_APP = _ROOT / "app.py"
_MODE = "Configure (models & control)"


def _control_labels() -> dict[str, str]:
    """`_CONTROL_LABEL` read from app.py source -- parsed, never executed."""
    tree = ast.parse(_APP.read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "_CONTROL_LABEL"
                        for t in node.targets)):
            return ast.literal_eval(node.value)
    raise AssertionError("_CONTROL_LABEL not found in app.py")


_PROBE = r"""
import json, sys
from streamlit.testing.v1 import AppTest
at = AppTest.from_file(sys.argv[1], default_timeout=300)
at.session_state["app_mode"] = sys.argv[2]
at.run()
labels = set()
for g in (at.checkbox, at.number_input, at.text_input):
    labels |= {w.label for w in g}
print("<<<RESULT>>>" + json.dumps({
    "exceptions": [str(e.value) for e in at.exception],
    "labels": sorted(labels),
}))
"""


@pytest.fixture(scope="module")
def rendered():
    """One Configure render, in a clean interpreter. See the module docstring."""
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(_APP), _MODE],
        capture_output=True, text=True, timeout=600, cwd=str(_ROOT))
    marker = [ln for ln in proc.stdout.splitlines() if ln.startswith("<<<RESULT>>>")]
    assert marker, (
        f"probe produced no result. stdout tail: {proc.stdout[-2000:]} "
        f"| stderr tail: {proc.stderr[-2000:]}")
    return json.loads(marker[-1][len("<<<RESULT>>>"):])


@pytest.fixture(scope="module")
def widget_labels(rendered):
    return set(rendered["labels"])


def test_the_configure_page_renders_without_an_exception(rendered):
    assert not rendered["exceptions"], rendered["exceptions"]


def test_every_control_knob_reaches_a_widget(widget_labels):
    """The one that matters: a knob with no widget is lost on Save."""
    labels = _control_labels()
    missing = []
    for k in control_to_dict(load_control(_ROOT / "config")):
        if k == "forecast_start":
            continue          # shown as a derived message, never a widget
        want = labels.get(k, k.replace("_", " ").capitalize())
        if want not in widget_labels:
            missing.append(k)
    assert not missing, f"knobs with no widget (dropped on Save): {missing}"


def test_inactive_knobs_are_tucked_away_but_still_render(widget_labels):
    """Out of the working set is the point; missing is a data-loss bug."""
    labels = _control_labels()
    knobs = set(control_to_dict(load_control(_ROOT / "config")))
    inactive = [k for k in knobs if "(INACTIVE)" in labels.get(k, "")]
    assert inactive, "expected at least one knob marked INACTIVE"
    for k in inactive:
        assert labels[k] in widget_labels, (
            f"{k} is inactive but no longer renders — it would be dropped "
            f"from the saved config")


def test_the_inactive_set_is_derived_from_the_labels_not_a_second_list():
    labels = _control_labels()
    assert "(INACTIVE)" in labels["harvest_setpoint_lookahead_weeks"]
    assert "(INACTIVE)" not in labels.get("max_harvest_per_week", "")
