"""The Ideal mode's BUTTON path — the half test_app_renders cannot reach.

test_app_renders proves every mode builds, and says plainly that it never
clicks a button, so a handler body can raise and still pass it. The Ideal
mode's whole value sits behind its button, so this drives it: select the mode,
shrink the grid to two rhythms, click, and check the page answers. Then move
the cap slider and check the result is marked STALE rather than shown under a
slider that now reads something else ("state outlives its inputs", the bug
class app_py_review found most often).

Behaviour only, no pinned numbers. Runs in a fresh interpreter for the same
reason test_app_renders does: executing app.py under AppTest in an interpreter
that already imported it corrupts Streamlit's form context.
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")

pytestmark = pytest.mark.skipif(
    not (os.path.exists(APP)
         and os.path.exists(os.path.join(ROOT, "config", "control.yaml"))
         and os.path.exists(os.path.join(ROOT, "scenario", "batches.yaml"))),
    reason="needs app.py + a seeded config/scenario")

_DRIVER = r'''
import sys
sys.path.insert(0, %(root)r)
try:
    from streamlit.testing.v1 import AppTest
except Exception as e:
    print("SKIP no AppTest: %%s" %% e)
    raise SystemExit(0)

def fail(msg):
    print("FAIL " + msg)
    raise SystemExit(1)

at = AppTest.from_file(%(app)r, default_timeout=600)
at.session_state["app_mode"] = "Ideal (what should we stock?)"
at.run()
if at.exception:
    fail("render: " + "; ".join(str(e)[:300] for e in at.exception))
if not any("Find the ideal" in i.value for i in at.info):
    fail("no prompt before the first run")

at.multiselect(key="ideal_cads").set_value([49]).run()
at.multiselect(key="ideal_sizes").set_value([250, 280]).run()
btn = [b for b in at.button if "Find the ideal" in b.label]
if not btn:
    fail("the Find button is missing")
btn[0].click().run()
if at.exception:
    fail("click: " + "; ".join(str(e)[:300] for e in at.exception))
answered = ([s.value for s in at.subheader if s.value.startswith("Ideal at")]
            or [e.value for e in at.error if "No balanced rhythm" in e.value])
if not answered:
    fail("clicked, but the page gave neither an ideal nor a no-plan verdict")
if not at.dataframe:
    fail("no rhythm table")
rows = list(at.dataframe[0].value["Rhythm"])
if not any("(today)" in r for r in rows):
    fail("today's rhythm is missing from the table: %%r" %% rows)
if not any("Today's scenario" in m.value for m in at.markdown):
    fail("no today-vs-ideal sentence")

at.slider(key="ideal_cap_t").set_value(at.slider(key="ideal_cap_t").value + 400).run()
if at.exception:
    fail("slider: " + "; ".join(str(e)[:300] for e in at.exception))
if not any("Showing the result for" in w.value for w in at.warning):
    fail("moved the cap but the old result is not marked stale")
print("OK ideal click path")
'''


def test_the_ideal_button_answers_and_a_moved_cap_marks_it_stale():
    src = _DRIVER % {"root": ROOT, "app": APP}
    p = subprocess.run([sys.executable, "-c", src], capture_output=True,
                       text=True, timeout=900, cwd=ROOT)
    tail = [ln for ln in (p.stdout or "").splitlines()
            if ln.startswith(("OK", "FAIL", "SKIP"))]
    msg = tail[-1] if tail else (p.stderr or "")[-500:]
    if msg.startswith("SKIP"):
        pytest.skip(msg)
    assert p.returncode == 0, msg
    assert msg.startswith("OK"), msg
