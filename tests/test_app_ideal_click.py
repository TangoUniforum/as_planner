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
# ONE cap (the slider's), so this real run stays one engine run + its wave.
slider = at.slider(key="ideal_cap_t").value
at.text_input(key="ideal_opt_caps").set_value(str(slider)).run()
boom(at, "grid inputs")
if not any("1 rhythm(s)" in c.value and "min" in c.value and "1 caps" in c.value
           and "stability runs" in c.value for c in at.caption):
    fail("no grid size / time estimate (with the caps and the stability "
         "wave) before the button")
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
if list(tbl[0].value["Cap (t)"]) != [slider]:
    fail("the table does not name the cap it ran at: %%r"
         %% list(tbl[0].value["Cap (t)"]))
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
            or at.number_input(key="ideal_ref_size").value != 200000
            or at.number_input(key="ideal_ref_cap").value != slider
            or at.session_state["ideal_tr_cap"] != slider):
        fail("Use this rhythm did not load it into steps 2 and 3: %%r x %%r "
             "@ %%r / %%r" %% (
            at.number_input(key="ideal_ref_cad").value,
            at.number_input(key="ideal_ref_size").value,
            at.number_input(key="ideal_ref_cap").value,
            at.session_state["ideal_tr_cap"]))
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
        "_ideal_opt_stability", "_ideal_opt_value_text", "_ideal_opt_use",
        "_IDEAL_TR_LIMITS"}
ns = {"st": st}
exec(compile(ast.Module(body=[
    n for n in tree.body
    if (isinstance(n, ast.FunctionDef) and n.name in want)
    or (isinstance(n, ast.Assign) and any(getattr(t, "id", None) in want
                                          for t in n.targets))],
    type_ignores=[]), "app.py", "exec"), ns)
# Step 3's seeds as the page passes them (_ideal_tr_seeds of Control).
TR_SEEDS = {"ideal_tr_cap": 3800, "ideal_tr_hmax": 60000,
            "ideal_tr_hmin": 26000, "ideal_tr_wmin": 3150,
            "ideal_tr_feed": 27500}

def cell(cad, size, ok=True, rev=0.0, error=None, fails=(), cap=3_800_000.0,
         **br):
    b = {k: 0 for k in io.BREACH_KEYS}
    b.update(br)
    return io.Cell(cadence_days=cad, batch_size=size,
                   fish_per_week=size * 7 / cad, cap_kg=float(cap), year=2029,
                   revenue=rev,
                   hog_t=rev / 2e4, breaches=b, total=sum(b.values()),
                   gates=tuple(ie.Gate(n, "FAIL", "x") for n in fails),
                   within_limits=ok and error is None, error=error)

w = cell(49, 210_000, rev=103.1e6)
s2 = cell(42, 180_000, rev=96.6e6, cap=3_200_000)      # a lower cap
s3 = cell(56, 232_000, rev=91.4e6)
big = cell(49, 280_000, ok=False, rev=136.8e6, density=76, sys_feed=37,
           sys_biomass=1, floor=1)
audit = cell(49, 300_000, ok=False, rev=150e6, fails=("Conservation audits",))
ok_n = lambda c, d: cell(c.cadence_days, c.batch_size + d, cap=c.cap_kg)
bad_n = lambda c, d: cell(c.cadence_days, c.batch_size + d, ok=False,
                          sys_feed=1, cap=c.cap_kg)
err_n = lambda c, d: cell(c.cadence_days, c.batch_size + d,
                          error="RuntimeError: boom", cap=c.cap_kg)
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
elif case == "samerhythm":
    w_lo = cell(49, 210_000, ok=False, rev=110e6, cap=3_200_000, density=3)
    res = io.Result(cells=(w, w_lo), objective="revenue", best=w,
                    closest=None, unconstrained=w_lo, best_stable=w,
                    stability=((w, (ok_n(w, -5000), ok_n(w, 5000)), True),))
else:                                           # no winner
    res = io.Result(cells=(big, audit), objective="revenue", best=None,
                    closest=big, unconstrained=big)
ns["_ideal_opt_cost"](res)
if res.best is not None:
    ns["_ideal_opt_stability"](res, st.session_state.get("stale", False),
                               TR_SEEDS)
"""

def page(case, stale=False, **state):
    at = AppTest.from_string(SCRIPT, default_timeout=60)
    at.session_state["case"] = case
    at.session_state["stale"] = stale
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    if at.exception:
        fail(case + ": " + "; ".join(str(e)[:300] for e in at.exception))
    return at

# The same seeds the page script passes (SCRIPT's own copy runs in the app).
TR_SEEDS = {"ideal_tr_cap": 3800, "ideal_tr_hmax": 60000,
            "ideal_tr_hmin": 26000, "ideal_tr_wmin": 3150,
            "ideal_tr_feed": 27500}

def text(at, kind):
    return [e.value for e in getattr(at, kind)]

def one(at, kind, *parts):
    got = [v for v in text(at, kind) if all(p in v for p in parts)]
    if len(got) != 1:
        fail("want one %%s with %%r, got %%r" %% (kind, parts, text(at, kind)))

def keys(at):
    return sorted(b.key for b in at.button)

at = page("stable")
one(at, "success", "**Stable:**", "49 d × 205,000 and × 215,000 at 3,800 t "
    "are also within every limit")
one(at, "info", "Cost of the limits", "**49 d × 280,000 @ 3,800 t**",
    "+$33.7M", "breaks the limits 115 times", "76 tank-weeks over density",
    "37 system-weeks over feed", "1 week under the floor")
if text(at, "warning") or keys(at) != ["ideal_opt_use"]:
    fail("a stable winner: want no warning and one Use button, got %%r %%r"
         %% (text(at, "warning"), keys(at)))
if at.button(key="ideal_opt_use").disabled:
    fail("the Use button is disabled on a current result")

# Step 3's seeds are stale in this session (Control changed since its boxes
# were filled): the handover re-fills them first, then sets the cap on the
# fresh boxes — step 3's own re-fill would otherwise drop the cap.
at = page("fragile_stable2", _ideal_tr_seeds={"old": 1}, ideal_tr_hmax=12345,
          _keep_ideal_tr_hmax=12345)
one(at, "warning", "**Fragile:**", "at ±5,000 fish per batch (same cap)",
    "49 d × 215,000 @ 3,800 t breaks the limits (1 system-week over feed)",
    "Best stable plan: **42 d × 180,000 @ 3,200 t**")
one(at, "info", "The limits cost nothing")
if keys(at) != ["ideal_opt_use", "ideal_opt_use_stable"]:
    fail("a fragile winner with a stable second: want two buttons, got %%r"
         %% keys(at))
if "Use the best stable plan" not in at.button(key="ideal_opt_use_stable").label:
    fail("the primary button does not load the best stable plan")
if "@ 3,200 t" not in at.button(key="ideal_opt_use_stable").label:
    fail("the stable button does not name its cap: %%r"
         %% at.button(key="ideal_opt_use_stable").label)
at.button(key="ideal_opt_use_stable").click().run()
got = tuple(at.session_state[k] for k in
            ("ideal_ref_cad", "ideal_ref_size", "ideal_tr_sizes",
             "ideal_ref_cap", "ideal_tr_cap", "_keep_ideal_ref_cap",
             "_keep_ideal_tr_cap", "_keep_ideal_ref_size",
             "_ideal_ref_cap_by_opt"))
if (got != (42, 180000, "180000", 3200, 3200, 3200, 3200, 180000, 3200)
        or not at.session_state["_ideal_ref_regen"]):
    fail("Use the best stable plan did not hand 42 d x 180,000 @ 3,200 t "
         "to steps 2 and 3: %%r" %% (got,))
if at.session_state["_ideal_tr_seeds"] != TR_SEEDS:
    fail("the handover did not re-fill step 3's stale seeds")
if "ideal_tr_hmax" in at.session_state or "_keep_ideal_tr_hmax" in at.session_state:
    fail("a what-if box from the old Control survived the re-fill")
# ...and step 3 still gets its "Control limits changed" notice (it shows it
# once, on its next draw, from this flag).
if at.session_state["_ideal_tr_reset_note"] is not True:
    fail("the handover reset step 3's boxes but left it no notice")
at.button(key="ideal_opt_use").click().run()
got = (at.session_state["ideal_ref_cad"], at.session_state["ideal_ref_size"],
       at.session_state["ideal_ref_cap"], at.session_state["ideal_tr_cap"])
if got != (49, 210000, 3800, 3800):
    fail("Use the top plan anyway did not hand the winner over: %%r" %% (got,))

# Seeds already current: a what-if box the operator set stays as it is.
at = page("stable", _ideal_tr_seeds=dict(TR_SEEDS), ideal_tr_hmax=12345)
at.button(key="ideal_opt_use").click().run()
if at.session_state["ideal_tr_hmax"] != 12345:
    fail("the handover reset step 3's boxes although its seeds were current")
if at.session_state["ideal_tr_cap"] != 3800:
    fail("the handover did not set step 3's cap")
if "_ideal_tr_reset_note" in at.session_state:
    fail("a reset notice although nothing was reset (seeds were current)")
# First visit (step 3 never filled its boxes): a re-fill, but no notice —
# step 3's own rule.
at = page("stable", _keep_ideal_tr_hmax=12345)
at.button(key="ideal_opt_use").click().run()
if "_ideal_tr_reset_note" in at.session_state:
    fail("a reset notice on step 3's first fill")

at = page("fragile_stable2", stale=True)
if not all(at.button(key=k).disabled
           for k in ("ideal_opt_use", "ideal_opt_use_stable")):
    fail("a Use button is live on a stale result")

at = page("none3")
one(at, "warning", "**Fragile:**", "49 d × 205,000 @ 3,800 t could not run "
    "(RuntimeError: boom)", "None of the top 3 plans is stable")
one(at, "info", "Cost of the limits", "**49 d × 300,000 @ 3,800 t**",
    "it is not a valid plan: it fails Conservation audits",
    "not a real cost of the limits")
if keys(at) != ["ideal_opt_use"]:
    fail("none stable: want one Use button (the winner), got %%r" %% keys(at))

at = page("only1")
one(at, "warning", "**Fragile:**", "It is the only plan within every limit")

at = page("samerhythm")
# The same rhythm at a lower cap earns more but breaks a limit: the limits
# DO cost something — never "cost nothing" for a different cap.
one(at, "info", "Cost of the limits", "**49 d × 210,000 @ 3,200 t**")

at = page("nowinner")
one(at, "info", "Cost of the limits", "**49 d × 280,000 @ 3,800 t**",
    "breaks the limits 115 times")
if keys(at) or text(at, "success"):
    fail("no winner: want no Use button and no Stable line, got %%r"
         %% keys(at))
print("OK optimizer page lines and buttons")
'''


# The REAL page with forecast.ideal_optimize.optimize replaced by a synthetic
# one (no engine, seconds per rerun): the caps box starts from the cap slider;
# a cap above the slider, under 500 t or unparseable is refused with NO run;
# the table names each cell's cap; Use hands the winner's cap to step 2's cap
# box and step 3's what-if cap, and both survive a rerun and a mode round
# trip; loading a winner does not make the optimizer's own result stale,
# while changing the caps does; moving the slider re-seeds the caps.
_OPT_CAPS_DRIVER = r'''
import sys
sys.path.insert(0, %(root)r)
try:
    from streamlit.testing.v1 import AppTest
except Exception as e:
    print("SKIP no AppTest: %%s" %% e)
    raise SystemExit(0)
from forecast import ideal_optimize as io

def fail(msg):
    print("FAIL " + msg)
    raise SystemExit(1)

def boom(at, what):
    if at.exception:
        fail(what + ": " + "; ".join(str(e)[:300] for e in at.exception))

CALLS = []

def mk(c, s, p):
    # Revenue rises as the cap falls, so the winner is the lowest cap.
    return io.Cell(cadence_days=c, batch_size=s, fish_per_week=s * 7 / c,
                   cap_kg=float(p), year=2029, revenue=1e8 + (4e6 - p) * 10,
                   hog_t=4000.0, gain_t=4000.0, avg_gross_kg=4.2,
                   peak_pct_of_cap=0.8,
                   breaches={k: 0 for k in io.BREACH_KEYS}, total=0,
                   within_limits=True)

def fake_optimize(cadences, sizes, cap_kg, project_dir, *, caps=None,
                  objective="revenue", **kw):
    CALLS.append(dict(cap_kg=cap_kg, caps=list(caps)))
    cells = tuple(mk(c, s, p) for c in cadences for s in sizes for p in caps)
    b = io.best(cells, objective)
    ns = tuple(mk(b.cadence_days, b.batch_size + d, b.cap_kg)
               for d in (-5000, 5000))
    return io.Result(cells=cells, objective=objective, best=b, closest=None,
                     unconstrained=io.ignoring_limits(cells, objective),
                     stability=((b, ns, True),), best_stable=b)

io.optimize = fake_optimize            # app.py imports this very module

at = AppTest.from_file(%(app)r, default_timeout=600)
at.session_state["app_mode"] = "Ideal (what should we stock?)"
at.run()
boom(at, "render")
slider = at.slider(key="ideal_cap_t").value
seed = lambda t: ", ".join(str(t - d) for d in (0, 200, 400, 600))
if at.text_input(key="ideal_opt_caps").value != seed(slider):
    fail("the caps do not start from the cap slider: %%r"
         %% at.text_input(key="ideal_opt_caps").value)
at.text_input(key="ideal_opt_cads").set_value("49")
at.number_input(key="ideal_opt_smin").set_value(200000)
at.number_input(key="ideal_opt_smax").set_value(200000).run()
boom(at, "grid inputs")

for bad, words in ((f"{slider + 100}, {slider}",
                    "never searches above your cap slider"),
                   ("abc", "not a whole number of tonnes"),
                   ("300", "at least 500 t"),
                   ("3,800", "at least 500 t"),
                   ("", "one cap")):
    at.text_input(key="ideal_opt_caps").set_value(bad).run()
    boom(at, "caps %%r" %% bad)
    if not any(words in e.value for e in at.error):
        fail("caps %%r: no refusal naming %%r; errors %%r"
             %% (bad, words, [e.value[:200] for e in at.error]))
    if not at.button(key="ideal_opt_run").disabled:
        fail("caps %%r: the Find button is live" %% bad)
    at.button(key="ideal_opt_run").click().run()
    boom(at, "click on refused caps %%r" %% bad)
    if CALLS:
        fail("caps %%r were refused but the optimizer ran" %% bad)

lo = slider - 400
at.text_input(key="ideal_opt_caps").set_value(f"{slider}, {lo}").run()
boom(at, "two caps")
if not any("2 rhythm(s)" in c.value and "2 caps" in c.value
           for c in at.caption):
    fail("the grid caption does not count the caps: %%r"
         %% [c.value[:160] for c in at.caption if "rhythm" in c.value])
at.button(key="ideal_opt_run").click().run()
boom(at, "find")
if CALLS != [dict(cap_kg=slider * 1000.0, caps=[slider * 1000.0, lo * 1000.0])]:
    fail("the optimizer was not asked for exactly the parsed caps under the "
         "slider: %%r" %% CALLS)
won = [s.value for s in at.success if "Best within every limit" in s.value]
if not won or f"49 d × 200,000 @ {lo:,} t" not in won[0]:
    fail("the winner line does not name the cap: %%r" %% won)
if not any(f"× 195,000 and × 205,000 at {lo:,} t" in s.value
           for s in at.success):
    fail("the stability line does not name the cap")
tbl = [d for d in at.dataframe if "Cap (t)" in list(d.value.columns)]
if not tbl or list(tbl[0].value["Cap (t)"]) != [lo, slider]:
    fail("no Cap (t) column with each cell's cap: %%r"
         %% (list(tbl[0].value["Cap (t)"]) if tbl else None))
if list(tbl[0].value["Rhythm"]) != ["49 d × 200,000"] * 2:
    fail("the Rhythm column: %%r" %% list(tbl[0].value["Rhythm"]))
at.button(key="ideal_opt_use").click().run()
boom(at, "use")

def handed_over(what):
    got = (at.number_input(key="ideal_ref_cad").value,
           at.number_input(key="ideal_ref_size").value,
           at.number_input(key="ideal_ref_cap").value,
           at.session_state["ideal_tr_cap"])
    if got != (49, 200000, lo, lo):
        fail(what + ": the winner and its cap are not in steps 2 and 3: %%r"
             %% (got,))
    if not any(f"Cap set to {lo:,} t by the optimizer" in c.value
               and "your Control cap is" in c.value for c in at.caption):
        fail(what + ": step 2 does not say the optimizer set its cap")
    if any("Showing the last optimizer run" in w.value for w in at.warning):
        fail(what + ": loading the winner made the optimizer result stale")
    if at.button(key="ideal_opt_use").disabled:
        fail(what + ": the Use button is disabled")

handed_over("after Use")
at.run()
boom(at, "rerun")
handed_over("rerun")
at.radio(key="app_mode").set_value("How it works (the rules)").run()
at.radio(key="app_mode").set_value("Ideal (what should we stock?)").run()
boom(at, "mode round trip")
handed_over("mode round trip")
if at.text_input(key="ideal_opt_caps").value != f"{slider}, {lo}":
    fail("the caps were lost on a mode round trip: %%r"
         %% at.text_input(key="ideal_opt_caps").value)
# A hand edit of step 2's cap: no longer the optimizer's, and never stale.
at.number_input(key="ideal_ref_cap").set_value(lo + 100).run()
boom(at, "step 2 cap edit")
if any("by the optimizer" in c.value for c in at.caption):
    fail("step 2 still credits the optimizer after a hand edit")
if any("Showing the last optimizer run" in w.value for w in at.warning):
    fail("step 2's cap box made the optimizer result stale")
# Changing the caps searched does make it stale.
at.text_input(key="ideal_opt_caps").set_value(str(slider)).run()
boom(at, "caps edit")
if not any("Showing the last optimizer run" in w.value for w in at.warning):
    fail("changed the caps but the optimizer result is not marked stale")
# Moving the slider re-seeds the caps from it.
at.slider(key="ideal_cap_t").set_value(slider - 100).run()
boom(at, "slider")
if at.text_input(key="ideal_opt_caps").value != seed(slider - 100):
    fail("moving the slider did not re-seed the caps: %%r"
         %% at.text_input(key="ideal_opt_caps").value)
print("OK optimizer caps and handover")
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
    # The bar is sized for the largest wave (2 x 10 = 20) from the start:
    # the grid alone never reads full, and a full wave of 20 ends at 100%.
    assert prog(3, 3, 3)[0] == pytest.approx(3 / 23)
    assert "up to 20 stability runs" in prog(3, 3, 3)[1]
    fr = [prog(d, 23, 3)[0] for d in range(1, 24)]
    assert fr == sorted(fr) and fr[-1] == pytest.approx(1.0)


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


def test_the_optimizer_caps_are_refused_above_the_slider_and_handed_over():
    _drive(_OPT_CAPS_DRIVER)
