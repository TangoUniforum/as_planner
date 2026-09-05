"""Every mode must actually RENDER — and here is exactly what that does not cover.

WHY THIS EXISTS. On 2026-09-04 I added a call to `_ana.adoption_blocked` inside
`_compare_and_choose`, where `_ana` is not bound. Six tests written for that
very change passed straight through it, because none of them execute a
Streamlit body — they check contracts (AST shape, pure helpers) and never run
the page. It was caught by reading the file, which is not a control.

The same shape has bitten this project repeatedly and always the same way: the
banner that announced the wrong build, the title that contradicted the banner
under it, the Limits tab that died on a KeyError after a config import. Each
was found by opening the app.

WHAT THIS CATCHES, verified by negative control: an undefined name (or any
raising statement) on a RENDER path. Injecting one into the calibration section
fails the mode sweep.

WHAT IT DOES NOT CATCH, also verified by negative control: the bug it was
written for. Re-injecting that exact NameError inside the "Use this plan"
button handler leaves this passing, because a handler body only runs when the
button is clicked and rendering never clicks. So this closes the render-path
half of the gap and not the click-path half. Said plainly here rather than left
for the next reader to assume otherwise.

CLOSING THE REST would take one of two things, neither done:
  * a scope-aware undefined-name checker over app.py (pyflakes does this
    properly). A hand-rolled AST version was tried and rejected: it flags 28
    functions and every one inspected was a legitimate closure over enclosing
    locals — naive scope models cannot tell those apart. pyflakes is not
    installed and adding a dependency is the operator's call.
  * driving the handlers, which needs state fixtures (an uploaded PR, a
    populated compare board) AppTest cannot conjure cheaply.

WHY A SUBPROCESS. `app.py` runs Streamlit calls at import, and most tests here
`import app`. Executing the file AGAIN under AppTest in that same interpreter
corrupts Streamlit's form context and raises "st.button() can't be used in an
st.form()" — a false alarm with nothing to do with the app: it passes
standalone and the real app runs fine. Diagnosed by reproducing it with
`import app` alone, no monkeypatching, no other tests. So the render happens in
a fresh interpreter and this test can sit in the suite without being
order-dependent.

Deliberately shallow: it proves the page BUILDS, not that the numbers are
right. Everything else here does the second job and none of it does the first.
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
         and os.path.exists(os.path.join(ROOT, "scenario", "limits.yaml"))),
    reason="needs app.py + a seeded config/scenario")

_DRIVER = r'''
import sys
sys.path.insert(0, %(root)r)
try:
    from streamlit.testing.v1 import AppTest
except Exception as e:
    print("SKIP no AppTest: %%s" %% e)
    raise SystemExit(0)

def fresh():
    at = AppTest.from_file(%(app)r, default_timeout=240)
    at.run()
    return at

at = fresh()
if at.exception:
    print("FAIL initial render: " + "; ".join(str(e)[:300] for e in at.exception))
    raise SystemExit(1)
if not at.radio:
    print("FAIL no Mode selector rendered")
    raise SystemExit(1)

modes = at.radio[0].options
if len(modes) < 4:
    print("FAIL mode list looks wrong: %%r" %% (modes,))
    raise SystemExit(1)

broken = []
for m in modes:
    a = fresh()
    try:
        a.radio[0].set_value(m).run()
    except Exception as e:
        broken.append("%%s: could not select (%%s)" %% (m, str(e)[:200]))
        continue
    if a.exception:
        broken.append("%%s: %%s" %% (m, "; ".join(str(e)[:300] for e in a.exception)))
if broken:
    print("FAIL " + " | ".join(broken))
    raise SystemExit(1)
print("OK %%d modes rendered" %% len(modes))
'''


def _render_in_a_fresh_interpreter():
    src = _DRIVER % {"root": ROOT, "app": APP}
    p = subprocess.run([sys.executable, "-c", src], capture_output=True,
                       text=True, timeout=900, cwd=ROOT)
    tail = [ln for ln in (p.stdout or "").splitlines()
            if ln.startswith(("OK", "FAIL", "SKIP"))]
    return p.returncode, (tail[-1] if tail else (p.stderr or "")[-500:])


def test_every_mode_renders():
    rc, msg = _render_in_a_fresh_interpreter()
    if msg.startswith("SKIP"):
        pytest.skip(msg)
    assert rc == 0, msg
    assert msg.startswith("OK"), msg
