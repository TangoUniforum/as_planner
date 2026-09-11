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

# Step 2 must FOLLOW the scan (a keyed widget ignores value= after its first
# render — the defaults used to stay at today's rhythm).
if at.number_input(key="ideal_ref_size").value not in (250000, 280000):
    fail("step 2 did not follow the scan's best rhythm: size = %%r"
         %% (at.number_input(key="ideal_ref_size").value,))

# A value typed on this page must survive a trip to another mode (Streamlit
# drops the state of widgets that are not rendered).
at.number_input(key="ideal_ref_hmax").set_value(30000).run()
# By key: the Ideal page has its own radio (the optimizer's objective).
at.radio(key="app_mode").set_value("How it works (the rules)").run()
at.radio(key="app_mode").set_value("Ideal (what should we stock?)").run()
if at.exception:
    fail("mode round-trip: " + "; ".join(str(e)[:300] for e in at.exception))
if at.number_input(key="ideal_ref_hmax").value != 30000:
    fail("a step-2 limit was lost on a mode round-trip: %%r"
         %% (at.number_input(key="ideal_ref_hmax").value,))

# Step 2: the reference sheet through the REAL engine (~30 s).
ref = [b for b in at.button if "Run the reference sheet" in b.label]
if not ref:
    fail("the reference-sheet Run button is missing")
ref[0].click().run()
if at.exception:
    fail("reference run: " + "; ".join(str(e)[:300] for e in at.exception))
errs = [e.value for e in at.error]
if errs:
    fail("reference run showed an error: " + errs[0][:300])
if not any("Engine answer, steady year" in m.value for m in at.markdown):
    fail("the reference sheet ran but shows no engine answer")

# Step 3 without a PR must say what it needs, not crash.
if not any("Upload today's" in i.value for i in at.info):
    fail("the transition step does not ask for a ProductionReport")
print("OK ideal click path")
'''


# Step 2's optimizer on a ONE-cell grid (one real engine run, ~20-30 s): it
# must say the grid size before the button, answer with a winner or a LOUD
# no-plan verdict, show the table, mark itself stale when the objective
# changes, and hand a winner to step 2's rhythm inputs.
_OPT_DRIVER = r'''
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

def boom(at, what):
    if at.exception:
        fail(what + ": " + "; ".join(str(e)[:300] for e in at.exception))

at = AppTest.from_file(%(app)r, default_timeout=600)
at.session_state["app_mode"] = "Ideal (what should we stock?)"
at.run()
boom(at, "render")
if not any(b.label == "Find the best rhythm" for b in at.button):
    fail("the optimizer button is missing")
at.text_input(key="ideal_opt_cads").set_value("49").run()
at.number_input(key="ideal_opt_smin").set_value(200000).run()
at.number_input(key="ideal_opt_smax").set_value(200000).run()
boom(at, "grid inputs")
if not any("1 rhythm(s)" in c.value and "min" in c.value
           and "stability runs" in c.value for c in at.caption):
    fail("no grid size / time estimate (with the stability wave) before "
         "the button")
[b for b in at.button if b.label == "Find the best rhythm"][0].click().run()
boom(at, "optimizer click")
won = [s.value for s in at.success if "Best within every limit" in s.value]
lost = [e.value for e in at.error
        if "No rhythm in this grid is within every limit" in e.value]
if not (won or lost):
    fail("neither a winner nor a loud no-plan verdict; errors: %%r"
         %% [e.value[:200] for e in at.error])
# The cost of the limits (display only): always said once a rhythm ran.
cost = [i.value for i in at.info if "Cost of the limits" in i.value
        or "The limits cost nothing" in i.value]
if not cost:
    fail("no cost-of-the-limits line")
# The stability line: only with a winner (its +/-5,000 neighbours ran too).
stab = "no stability line: no winner"
if won:
    if any(s.value.startswith("**Stable:**") for s in at.success):
        stab = "Stable"
    elif any(w.value.startswith("**Fragile:**") for w in at.warning):
        stab = "Fragile"
    else:
        fail("a winner but neither a Stable nor a Fragile line")
tbl = [d for d in at.dataframe if "Within every limit" in list(d.value.columns)]
if not tbl:
    fail("no optimizer table")
if list(tbl[0].value["Rhythm"]) != ["49 d × 200,000"]:
    fail("the table does not hold the one rhythm run: %%r"
         %% list(tbl[0].value["Rhythm"]))
for col in ("Tank-weeks over density", "System-weeks over feed limit",
            "Weeks over the biomass cap", "Total breaches",
            "Other failed checks"):
    if col not in tbl[0].value.columns:
        fail("the table has no %%r column" %% col)
at.radio(key="ideal_opt_obj").set_value("hog").run()
boom(at, "objective change")
if not any("Showing the last optimizer run" in w.value for w in at.warning):
    fail("changed the objective but the result is not marked stale")
if won:
    # A stale result must not hand its rhythm over.
    if not at.button(key="ideal_opt_use").disabled:
        fail("the Use button is live on a stale result")
    at.radio(key="ideal_opt_obj").set_value("revenue").run()
    boom(at, "objective back")
    if any("Showing the last optimizer run" in w.value for w in at.warning):
        fail("the objective is back but the result is still marked stale")
    if at.button(key="ideal_opt_use").disabled:
        fail("the Use button is disabled on a current result")
    at.button(key="ideal_opt_use").click().run()
    boom(at, "use this rhythm")
    if (at.number_input(key="ideal_ref_cad").value != 49
            or at.number_input(key="ideal_ref_size").value != 200000):
        fail("Use this rhythm did not load it into step 2: %%r x %%r" %% (
            at.number_input(key="ideal_ref_cad").value,
            at.number_input(key="ideal_ref_size").value))
print("OK ideal optimizer path (%%s; %%s; cost: %%s)" %% (
    "winner" if won else "no plan", stab, cost[0][:160]))
'''


# The optimizer's cost / stability lines and Use buttons on SYNTHETIC results
# (no engine, seconds): the real function bodies are lifted out of app.py by
# name, so every branch the one-cell real grid cannot reach is drawn — a
# fragile winner with a stable second (and its click reaching step 2's keys),
# none of three stable, the only plan fragile, a stale result, and a cost
# line whose rhythm fails a check that is not a limit.
_OPT_PAGE_DRIVER = r'''
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

SCRIPT = "ROOT = " + repr(%(root)r) + "\n" + r"""
import ast, sys
import streamlit as st
sys.path.insert(0, ROOT)
from forecast import ideal_optimize as io
from forecast import ideal_engine as ie
tree = ast.parse(open(ROOT + "/app.py", encoding="utf-8").read())
want = {"_ideal_breach_text", "_ideal_opt_diff_text", "_ideal_opt_cost",
        "_ideal_opt_stability", "_ideal_opt_value_text", "_ideal_opt_use"}
ns = {"st": st}
exec(compile(ast.Module(body=[n for n in tree.body
                              if isinstance(n, ast.FunctionDef)
                              and n.name in want], type_ignores=[]),
             "app.py", "exec"), ns)

def cell(cad, size, ok=True, rev=0.0, error=None, fails=(), **br):
    b = {k: 0 for k in io.BREACH_KEYS}
    b.update(br)
    return io.Cell(cadence_days=cad, batch_size=size,
                   fish_per_week=size * 7 / cad, year=2029, revenue=rev,
                   hog_t=rev / 2e4, breaches=b, total=sum(b.values()),
                   gates=tuple(ie.Gate(n, "FAIL", "x") for n in fails),
                   within_limits=ok and error is None, error=error)

w = cell(49, 210_000, rev=103.1e6)
s2 = cell(42, 180_000, rev=96.6e6)
s3 = cell(56, 232_000, rev=91.4e6)
big = cell(49, 280_000, ok=False, rev=136.8e6, density=76, sys_feed=37,
           sys_biomass=1, floor=1)
audit = cell(49, 300_000, ok=False, rev=150e6, fails=("Conservation audits",))
ok_n = lambda c, d: cell(c.cadence_days, c.batch_size + d)
bad_n = lambda c, d: cell(c.cadence_days, c.batch_size + d, ok=False,
                          sys_feed=1)
err_n = lambda c, d: cell(c.cadence_days, c.batch_size + d,
                          error="RuntimeError: boom")
case = st.session_state.get("case", "stable")
if case == "stable":
    res = io.Result(cells=(w, big), objective="revenue", best=w, closest=None,
                    unconstrained=big, best_stable=w,
                    stability=((w, (ok_n(w, -5000), ok_n(w, 5000)), True),))
elif case == "fragile_stable2":
    res = io.Result(cells=(w, s2), objective="revenue", best=w, closest=None,
                    unconstrained=w, best_stable=s2, stability=(
                        (w, (ok_n(w, -5000), bad_n(w, 5000)), False),
                        (s2, (ok_n(s2, -5000), ok_n(s2, 5000)), True)))
elif case == "none3":
    res = io.Result(cells=(w, s2, s3), objective="hog", best=w, closest=None,
                    unconstrained=audit, best_stable=None, stability=tuple(
                        (c, (err_n(c, -5000), bad_n(c, 5000)), False)
                        for c in (w, s2, s3)))
elif case == "only1":
    res = io.Result(cells=(w,), objective="gain", best=w, closest=None,
                    unconstrained=w, best_stable=None,
                    stability=((w, (bad_n(w, -5000), ok_n(w, 5000)), False),))
else:                                           # no winner
    res = io.Result(cells=(big, audit), objective="revenue", best=None,
                    closest=big, unconstrained=big)
ns["_ideal_opt_cost"](res)
if res.best is not None:
    ns["_ideal_opt_stability"](res, st.session_state.get("stale", False))
"""

def page(case, stale=False):
    at = AppTest.from_string(SCRIPT, default_timeout=60)
    at.session_state["case"] = case
    at.session_state["stale"] = stale
    at.run()
    if at.exception:
        fail(case + ": " + "; ".join(str(e)[:300] for e in at.exception))
    return at

def text(at, kind):
    return [e.value for e in getattr(at, kind)]

def one(at, kind, *parts):
    got = [v for v in text(at, kind) if all(p in v for p in parts)]
    if len(got) != 1:
        fail("want one %%s with %%r, got %%r" %% (kind, parts, text(at, kind)))

def keys(at):
    return sorted(b.key for b in at.button)

at = page("stable")
one(at, "success", "**Stable:**", "49 d × 205,000 and × 215,000 are also "
    "within every limit")
one(at, "info", "Cost of the limits", "**49 d × 280,000**", "+$33.7M",
    "breaks the limits 115 times", "76 tank-weeks over density",
    "37 system-weeks over feed", "1 week under the floor")
if text(at, "warning") or keys(at) != ["ideal_opt_use"]:
    fail("a stable winner: want no warning and one Use button, got %%r %%r"
         %% (text(at, "warning"), keys(at)))
if at.button(key="ideal_opt_use").disabled:
    fail("the Use button is disabled on a current result")

at = page("fragile_stable2")
one(at, "warning", "**Fragile:**", "at ±5,000 fish per batch",
    "49 d × 215,000 breaks the limits (1 system-week over feed)",
    "Best stable plan: **42 d × 180,000**")
one(at, "info", "The limits cost nothing")
if keys(at) != ["ideal_opt_use", "ideal_opt_use_stable"]:
    fail("a fragile winner with a stable second: want two buttons, got %%r"
         %% keys(at))
if "Use the best stable plan" not in at.button(key="ideal_opt_use_stable").label:
    fail("the primary button does not load the best stable plan")
at.button(key="ideal_opt_use_stable").click().run()
got = tuple(at.session_state[k] for k in
            ("ideal_ref_cad", "ideal_ref_size", "ideal_tr_sizes"))
if got != (42, 180000, "180000") or not at.session_state["_ideal_ref_regen"]:
    fail("Use the best stable plan did not hand 42 d x 180,000 to step 2: "
         "%%r" %% (got,))
at.button(key="ideal_opt_use").click().run()
got = (at.session_state["ideal_ref_cad"], at.session_state["ideal_ref_size"])
if got != (49, 210000):
    fail("Use the top plan anyway did not hand the winner over: %%r" %% (got,))

at = page("fragile_stable2", stale=True)
if not all(at.button(key=k).disabled
           for k in ("ideal_opt_use", "ideal_opt_use_stable")):
    fail("a Use button is live on a stale result")

at = page("none3")
one(at, "warning", "**Fragile:**", "49 d × 205,000 could not run "
    "(RuntimeError: boom)", "None of the top 3 plans is stable")
one(at, "info", "Cost of the limits", "**49 d × 300,000**",
    "it is not a valid plan: it fails Conservation audits",
    "not a real cost of the limits")
if keys(at) != ["ideal_opt_use"]:
    fail("none stable: want one Use button (the winner), got %%r" %% keys(at))

at = page("only1")
one(at, "warning", "**Fragile:**", "It is the only plan within every limit")

at = page("nowinner")
one(at, "info", "Cost of the limits", "**49 d × 280,000**",
    "breaks the limits 115 times")
if keys(at) or text(at, "success"):
    fail("no winner: want no Use button and no Stable line, got %%r"
         %% keys(at))
print("OK optimizer page lines and buttons")
'''


def _app_functions(*names):
    """The named top-level functions lifted out of app.py (no Streamlit run,
    no import of app.py), in a fresh namespace."""
    import ast
    with open(APP, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    body = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in names]
    assert sorted(n.name for n in body) == sorted(names)
    ns = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), APP, "exec"), ns)
    return ns


def _years(counts=None):
    """{year: a YearRead stand-in} with every step-3 count at 0 unless set
    in `counts` ({year: {count: value}})."""
    from types import SimpleNamespace
    base = dict(r8_over_tank_weeks=0, sys_bio_over_weeks=0,
                sys_feed_over_weeks=0, weeks_over_move_budget=0,
                under_floor_weeks=0, zero_weeks=0, over_cap_weeks=0)
    counts = counts or {}
    return {y: SimpleNamespace(**dict(base, **counts.get(y, {})))
            for y in (2027, 2028, 2029)}


def test_step3_verdict_a_failed_check_blocks_the_green_verdict():
    verdict = _app_functions("_ideal_tr_verdict")["_ideal_tr_verdict"]
    clean = {y: [] for y in (2027, 2028, 2029)}
    both, rows, broken, failed = verdict(_years(), _years(), clean)
    assert both == [2027, 2028, 2029] and not broken and not failed
    assert [r["Within the limits?"] for r in rows] == ["✓"] * 7
    # Zero on every count, but the proposal fails a check in a compared year:
    # no green verdict, and the check is named with its year.
    fails = {**clean, 2028: ["Conservation audits"]}
    both, _rows, broken, failed = verdict(_years(), _years(), fails)
    assert not broken and failed == {"Conservation audits": [2028]}
    # A year only the proposal covers is not compared.
    b = {**_years(), 2030: _years()[2029]}
    _both, _r, _b, failed = verdict(_years(), b,
                                    {**clean, 2030: ["Harvest floor"]})
    assert failed == {}
    # A count is a breach on its own; today's plan never decides.
    both, rows, broken, failed = verdict(
        _years({2027: dict(r8_over_tank_weeks=5)}),
        _years({2029: dict(sys_feed_over_weeks=2)}), clean)
    assert broken == ["system-weeks over feed limit: 2"] and not failed
    assert rows[0]["Today's plan"] == 5 and rows[0]["Within the limits?"] == "✓"


def test_step3_verdict_a_missing_count_raises_not_zero():
    """Detect, don't coerce: a YearRead without a count is an error, never a
    silent 0 that would read as within the limits."""
    from types import SimpleNamespace
    verdict = _app_functions("_ideal_tr_verdict")["_ideal_tr_verdict"]
    broken_read = {y: SimpleNamespace(r8_over_tank_weeks=0)
                   for y in (2027, 2028)}
    with pytest.raises(AttributeError):
        verdict(broken_read, broken_read, {2027: [], 2028: []})
    assert verdict({}, _years(), {})[0] == []       # no shared year: none


def test_the_optimizer_progress_bar_never_goes_backwards():
    prog = _app_functions("_ideal_opt_progress")["_ideal_opt_progress"]
    # A 3-rhythm grid, then a stability wave of 2: the total grows 3 -> 5.
    steps = [(1, 3), (2, 3), (3, 3), (4, 5), (5, 5)]
    fr = [prog(d, n, 3)[0] for d, n in steps]
    assert fr == sorted(fr) and len(set(fr)) == len(fr), fr
    assert fr[2] < 1.0 and fr[-1] == pytest.approx(1.0)
    assert "3 / 3 rhythms" in prog(3, 3, 3)[1]
    assert "stability check 1 / 2" in prog(4, 5, 3)[1]
    # A full wave of 6 on a 1-cell grid ends at 100% too.
    assert prog(7, 7, 1)[0] == pytest.approx(1.0)
    assert prog(1, 1, 1)[0] < prog(2, 7, 1)[0]


def _drive(driver):
    src = driver % {"root": ROOT, "app": APP}
    p = subprocess.run([sys.executable, "-c", src], capture_output=True,
                       text=True, timeout=900, cwd=ROOT)
    tail = [ln for ln in (p.stdout or "").splitlines()
            if ln.startswith(("OK", "FAIL", "SKIP"))]
    msg = tail[-1] if tail else (p.stderr or "")[-500:]
    if msg.startswith("SKIP"):
        pytest.skip(msg)
    assert p.returncode == 0, msg
    assert msg.startswith("OK"), msg


def test_the_ideal_button_answers_and_a_moved_cap_marks_it_stale():
    _drive(_DRIVER)


def test_the_optimizer_answers_on_a_one_cell_grid_and_hands_over_the_winner():
    _drive(_OPT_DRIVER)


def test_the_optimizer_cost_and_stability_lines_on_every_branch():
    _drive(_OPT_PAGE_DRIVER)
