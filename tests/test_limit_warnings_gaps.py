"""Gaps an independent mutation run found in tests/test_ideal_limit_warnings.py.

Each test here is a behaviour the Ideal page's far-from-Control warning and
its Reset to Control promise, that a single-behaviour mutant broke while
every test in test_ideal_limit_warnings.py still passed (2026-09-12):

- step 2's Min harvest weight and Max feed / day boxes are flagged, with
  their unit (a mutant dropped them, or showed "kg" for grams);
- step 2's cap is judged against step 1's SLIDER, and Reset puts it back to
  the slider — not to Control's cap (the two differ once the slider moves);
- a far table cell is flagged on the real page, for step 2 AND step 3 (a
  mutant passed empty overrides to the flag list), and the system FEED limit
  family is flagged at all;
- step 3's Biomass cap box is flagged;
- ONE far value alone warns (a mutant needed two);
- display only: every flagged value reaches the optimizers exactly as typed
  — feed, min weight, move budget and table cells, not only the harvest
  boxes (mutants clamped a 3x feed to 2x, or dropped a flagged budget);
- Reset draws step 3's table as a new editor too.

AppTest cannot see a browser's own widget copies (the real-browser drive is
in the change report); the table edit is the page's own round-trip state: the
edited table as its base, drawn as a new editor.
"""
import ast
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")
sys.path.insert(0, ROOT)

pytestmark = pytest.mark.skipif(
    not (os.path.exists(APP)
         and os.path.exists(os.path.join(ROOT, "config", "control.yaml"))
         and os.path.exists(os.path.join(ROOT, "scenario", "batches.yaml"))),
    reason="needs app.py + a seeded config/scenario")


def _lift(names, assigns=(), ns=None):
    """app.py's own functions (and module constants), by name."""
    with open(APP, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in names)
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) in assigns for t in n.targets))]
    found = {n.name for n in body if isinstance(n, ast.FunctionDef)}
    assert found == set(names), f"app.py lacks {sorted(set(names) - found)}"
    ns = dict(ns or {})
    exec(compile(ast.Module(body=body, type_ignores=[]), APP, "exec"), ns)
    return ns


_RULE = ("_ideal_far", "_ideal_flag_num", "_ideal_limit_flags",
         "_ideal_table_flag_items")
_CONSTS = ("_IDEAL_FAR_LOW", "_IDEAL_FAR_HIGH")


# --------------------------------------------------------------------------- #
# Pure
# --------------------------------------------------------------------------- #
def test_the_system_feed_limit_cell_is_a_flag_item():
    ns = _lift(_RULE, _CONSTS)
    items, flags = ns["_ideal_table_flag_items"], ns["_ideal_limit_flags"]
    seeds = {"OG3N": dict(tanks=10, density=95.0, biomass_t=450.0, feed=3000.0),
             "OG4N": dict(tanks=10, density=95.0, biomass_t=450.0, feed=3000.0),
             "OG6": dict(tanks=6, density=None, biomass_t=None, feed=None)}
    got = flags(items(seeds, 15, {},
                      {"OG3N": {"feed_per_day": 9000.0},
                       "OG4N": {"feed_per_day": 0.0},
                       "OG6": {"feed_per_day": 2500.0}}, {}))
    assert got == ["OG3N system feed limit: 9,000 kg/day (your files 3,000)",
                   "OG4N system feed limit: 0 kg/day (your files 3,000)"]
    # Within 50–200 % of the file: not flagged.
    assert flags(items(seeds, 15, {}, {"OG3N": {"feed_per_day": 5999.0}},
                       {})) == []


class _WarnSt:
    def __init__(self):
        self.warnings = []

    def warning(self, body, *a, **kw):
        self.warnings.append(body)


def test_one_far_value_alone_warns():
    fake = _WarnSt()
    ns = _lift(_RULE + ("_ideal_far_warning",), _CONSTS, ns={"st": fake})
    one = ns["_ideal_limit_flags"]([("Max feed / day", 0, 33_000, "kg",
                                     "Control")])
    assert one == ["Max feed / day: 0 kg (Control 33,000)"]
    ns["_ideal_far_warning"](one, "step-2", "The run")
    assert len(fake.warnings) == 1, "a single far value must warn"
    assert one[0] in fake.warnings[0] and "far from Control" in fake.warnings[0]
    ns["_ideal_far_warning"]([], "step-2", "The run")
    assert len(fake.warnings) == 1, "nothing far: no warning"


# --------------------------------------------------------------------------- #
# The real page (step 2) and the synthetic step-3 page, in AppTest
# --------------------------------------------------------------------------- #
_COMMON = r'''
import math, sys
sys.path.insert(0, %(root)r)
try:
    from streamlit.testing.v1 import AppTest
except Exception as e:
    print("SKIP no AppTest: %%s" %% e)
    raise SystemExit(0)
import pandas as pd
from forecast.config_io import load_config
CTRL, _t, _f = load_config(%(root)r + "/config")
HMAX, HMIN = int(CTRL.max_harvest_per_week), int(CTRL.min_harvest_per_week)
WMIN, FEED = int(CTRL.min_harvest_weight_g), int(CTRL.max_feed_per_day_kg)
MOVES = max(1, int(CTRL.max_transfers_per_week))
MOVES_FLAGGED = 1 < 0.5 * MOVES          # a budget of 1 is far from Control

def num(v):
    return f"{float(v):,.2f}".rstrip("0").rstrip(".")

def fail(msg):
    print("FAIL " + msg)
    raise SystemExit(1)

def boom(at, what):
    if at.exception:
        fail(what + ": " + "; ".join(str(e)[:300] for e in at.exception))

def far(at):
    return [w.value for w in at.warning if "far from Control" in w.value]

def check_far(at, what, want):
    w = far(at)
    if len(w) != 1:
        fail(what + ": want ONE far-from-Control warning, got %%r" %% w)
    miss = [x for x in want if x not in w[0]]
    if miss:
        fail(what + ": the warning does not list %%r: %%r" %% (miss, w[0]))

def edit_table(at, prefix, col, factor):
    """A cell of the tank & system limits table set to `factor` x its file
    value: the edited table as the editor's base, drawn as a new editor —
    the state the page itself builds after a mode round trip."""
    base = at.session_state[prefix + "_base"].copy()
    i = next(i for i in range(len(base)) if pd.notna(base[col].iloc[i]))
    system, seed = str(base["System"].iloc[i]), float(base[col].iloc[i])
    base.loc[base.index[i], col] = seed * factor
    at.session_state[prefix + "_base"] = base
    at.session_state[prefix + "_nonce"] = at.session_state[prefix + "_nonce"] + 1
    return i, system, seed

def check_table_seeded(at, what, prefix, col, i, seed):
    base = at.session_state[prefix + "_base"]
    if float(base[col].iloc[i]) != seed:
        fail(what + ": after Reset the table's %%s is %%r, want the file's %%r"
             %% (col, base[col].iloc[i], seed))
'''

_STEP2_DRIVER = _COMMON + r'''
from forecast import ideal_optimize as io

CALLS = []

def mk(c, s, p):
    return io.Cell(cadence_days=c, batch_size=s, fish_per_week=s * 7 / c,
                   cap_kg=float(p), year=2029, revenue=1e8, hog_t=4000.0,
                   gain_t=4000.0, avg_gross_kg=4.2, peak_pct_of_cap=0.8,
                   breaches={k: 0 for k in io.BREACH_KEYS}, total=0,
                   within_limits=True)

def fake_optimize(cadences, sizes, cap_kg, project_dir, *, caps=None,
                  objective="revenue", overrides=None, **kw):
    CALLS.append(dict(ov=dict(overrides or {}),
                      dens=dict(kw.get("density_overrides") or {})))
    cells = tuple(mk(c, s, p) for c in cadences for s in sizes for p in caps)
    b = io.best(cells, objective)
    ns = tuple(mk(b.cadence_days, b.batch_size + d, b.cap_kg)
               for d in (-5000, 5000))
    return io.Result(cells=cells, objective=objective, best=b, closest=None,
                     unconstrained=io.ignoring_limits(cells, objective),
                     stability=((b, ns, True),), best_stable=b)

io.optimize = fake_optimize            # app.py imports this very module

IDEAL = "Ideal (what should we stock?)"
at = AppTest.from_file(%(app)r, default_timeout=600)
at.session_state["app_mode"] = IDEAL
at.run()
boom(at, "render")
# Step 1's slider away from Control's cap: step 2's cap box is judged against
# (and reset to) the SLIDER, the value it starts at.
SLIDER = 6500
at.slider(key="ideal_cap_t").set_value(SLIDER).run()
boom(at, "slider")
COL = "Tank density cap (kg/m³)"
i, SYS, D = edit_table(at, "ideal_ref_lim", COL, 3)
at.number_input(key="ideal_ref_cap").set_value(3000)
at.number_input(key="ideal_ref_wmin").set_value(0)
at.number_input(key="ideal_ref_feed").set_value(3 * FEED)
at.number_input(key="ideal_ref_lim_moves").set_value(1).run()
boom(at, "far values")
WANT = [f"Biomass cap: 3,000 t (step 1's cap slider {num(SLIDER)})",
        f"Min harvest weight: 0 g (Control {num(WMIN)})",
        f"Max feed / day: {num(3 * FEED)} kg (Control {num(FEED)})",
        f"{SYS} tank density cap: {num(3 * D)} kg/m³ (your files {num(D)})"]
if MOVES_FLAGGED:
    WANT.append(f"Weekly move budget: 1 moves (Control {MOVES})")
check_far(at, "step 2", WANT)

# Display only: the optimizer gets every flagged value exactly as typed.
at.text_input(key="ideal_opt_cads").set_value("49")
at.number_input(key="ideal_opt_smin").set_value(200000)
at.number_input(key="ideal_opt_smax").set_value(200000)
at.text_input(key="ideal_opt_caps").set_value(str(SLIDER)).run()
at.button(key="ideal_opt_run").click().run()
boom(at, "optimizer run")
if len(CALLS) != 1:
    fail("the optimizer did not run once: %%r" %% CALLS)
ov, dens = CALLS[0]["ov"], CALLS[0]["dens"]
want_ov = {"min_harvest_weight_g": 0.0, "max_feed_per_day_kg": 3.0 * FEED}
if MOVES_FLAGGED:
    want_ov["max_transfers_per_week"] = 1
bad = {k: (ov.get(k), v) for k, v in want_ov.items() if ov.get(k) != v}
if bad:
    fail("the optimizer did not get the flagged values as typed "
         "(got, typed): %%r" %% bad)
if dens != {SYS: 3 * D}:
    fail("the optimizer did not get the flagged density cell as typed: %%r"
         %% dens)
check_far(at, "step 2 after a run", WANT)

at.button(key="ideal_ref_reset").click().run()
boom(at, "Reset")
SEEDS = {"ideal_ref_cap": SLIDER, "ideal_ref_hmax": HMAX,
         "ideal_ref_hmin": HMIN, "ideal_ref_wmin": WMIN,
         "ideal_ref_feed": FEED, "ideal_ref_lim_moves": MOVES}
got = {k: at.number_input(key=k).value for k in SEEDS}
if got != SEEDS:
    fail("after Reset %%r, want %%r (the cap goes back to step 1's slider)"
         %% (got, SEEDS))
if far(at):
    fail("the warning is still up after Reset: %%r" %% far(at))
check_table_seeded(at, "step 2", "ideal_ref_lim", COL, i, D)
print("OK step 2 gaps")
'''

_STEP3_DRIVER = _COMMON + r'''
import datetime as dt
from types import SimpleNamespace
from forecast import transition_optimize as to

YEARS = (2026, 2027, 2028, 2029, 2030)
ATTRS = [a for _k, a in to.YEAR_COUNTS]

def reads(rev):
    return {y: SimpleNamespace(revenue=rev, hog_t=1500.0, gain_t=1500.0,
                               **{a: 0 for a in ATTRS}) for y in YEARS}

def fake_today(live, project_dir, **kw):
    return to.TrCell(batch_size=0, cap_kg=3.8e6, today=True, reads=reads(1e8),
                     gates_by_year={y: () for y in YEARS})

CALLS = []

def fake_cell(cell, project_dir, **kw):
    CALLS.append(dict(ov=dict(kw.get("overrides") or {}),
                      sys=dict(kw.get("system_overrides") or {})))
    size, cap = cell
    return to.TrCell(batch_size=size, cap_kg=float(cap),
                     reads=reads(size * 350.0), n_changed=5,
                     gates_by_year={y: () for y in YEARS},
                     first_tran_og=dt.date(2027, 9, 23))

to.run_today, to.run_cell = fake_today, fake_cell

SCRIPT = "ROOT = " + repr(%(root)r) + "\n" + r"""
import ast, os, sys, datetime as dt
from pathlib import Path
from types import SimpleNamespace
import streamlit as st
sys.path.insert(0, ROOT)
from forecast import ideal as im
tree = ast.parse(open(ROOT + "/app.py", encoding="utf-8").read())

def keep(n):
    if isinstance(n, ast.FunctionDef):
        return n.name.startswith("_ideal") or n.name == "_resend_widget"
    return isinstance(n, ast.Assign) and any(
        getattr(t, "id", "").startswith(("_IDEAL", "_TR_", "_REF_"))
        for t in n.targets)

ns = {"st": st, "os": os, "_ROOT": Path(ROOT),
      "uploaded": SimpleNamespace(name="pr.xlsm", getvalue=lambda: b"PR bytes"),
      "pr": {"ok": True, "forecast_start": dt.datetime(2026, 9, 1)},
      "_effective_method": lambda: ("controller", {}, "test"),
      "_method_obj": lambda k: SimpleNamespace(label="Controller"),
      "_config_fingerprint": lambda: "fp",
      "_cpu_workers": lambda: 1}
exec(compile(ast.Module(body=[n for n in tree.body if keep(n)],
                        type_ignores=[]), "app.py", "exec"), ns)
ctx = im.load_context(ROOT)
ns["_ideal_restore"]()
ns["_ideal_transition"](ctx, (49, 340000), 3800)
ns["_ideal_save"]()
st.stop()
"""

at = AppTest.from_string(SCRIPT, default_timeout=120)
at.run()
boom(at, "render")
CAP = min(10_000, max(500, int(round(float(CTRL.max_biomass_kg) / 1000.0))))
COL = "System feed limit (kg/day)"
i, SYS, F = edit_table(at, "ideal_tr_lim", COL, 3)
at.number_input(key="ideal_tr_cap").set_value(500)
at.number_input(key="ideal_tr_hmax").set_value(3 * HMAX)
at.number_input(key="ideal_tr_wmin").set_value(0)
at.number_input(key="ideal_tr_feed").set_value(3 * FEED)
at.number_input(key="ideal_tr_lim_moves").set_value(1).run()
boom(at, "far values")
WANT = [f"Biomass cap: 500 t (Control {num(CAP)})",
        f"Max harvest / wk: {num(3 * HMAX)} fish (Control {num(HMAX)})",
        f"Min harvest weight: 0 g (Control {num(WMIN)})",
        f"Max feed / day: {num(3 * FEED)} kg (Control {num(FEED)})",
        f"{SYS} system feed limit: {num(3 * F)} kg/day (your files {num(F)})"]
if MOVES_FLAGGED:
    WANT.append(f"Weekly move budget: 1 moves (Control {MOVES})")
check_far(at, "step 3", WANT)

# Display only: the transition optimizer gets every flagged value as typed
# (its caps replace the cap box, so the cap is not among its overrides).
at.number_input(key="ideal_tr_opt_smin").set_value(280000)
at.number_input(key="ideal_tr_opt_smax").set_value(280000)
at.text_input(key="ideal_tr_opt_caps").set_value("3800").run()
at.button(key="ideal_tr_opt_run").click().run()
boom(at, "transition optimizer run")
if not CALLS:
    fail("the transition optimizer did not run")
ov, sys_ov = CALLS[0]["ov"], CALLS[0]["sys"]
want_ov = {"max_harvest_per_week": 3.0 * HMAX, "min_harvest_weight_g": 0.0,
           "max_feed_per_day_kg": 3.0 * FEED}
if MOVES_FLAGGED:
    want_ov["max_transfers_per_week"] = 1
bad = {k: (ov.get(k), v) for k, v in want_ov.items() if ov.get(k) != v}
if bad:
    fail("the transition optimizer did not get the flagged values as typed "
         "(got, typed): %%r" %% bad)
if (sys_ov.get(SYS) or {}).get("feed_per_day") != 3 * F:
    fail("the transition optimizer did not get the flagged feed cell as "
         "typed: %%r" %% sys_ov)
check_far(at, "step 3 after a run", WANT)

n0 = at.session_state["ideal_tr_lim_nonce"]
at.button(key="ideal_tr_reset").click().run()
boom(at, "Reset")
if at.session_state["ideal_tr_lim_nonce"] <= n0:
    fail("Reset did not draw step 3's limits table as a new editor")
SEEDS = {"ideal_tr_cap": CAP, "ideal_tr_hmax": HMAX, "ideal_tr_hmin": HMIN,
         "ideal_tr_wmin": WMIN, "ideal_tr_feed": FEED,
         "ideal_tr_lim_moves": MOVES}
got = {k: at.number_input(key=k).value for k in SEEDS}
if got != SEEDS:
    fail("after Reset %%r, want %%r" %% (got, SEEDS))
if far(at):
    fail("the warning is still up after Reset: %%r" %% far(at))
check_table_seeded(at, "step 3", "ideal_tr_lim", COL, i, F)
print("OK step 3 gaps")
'''


def _drive(driver):
    src = driver % {"root": ROOT, "app": APP}
    p = subprocess.run([sys.executable, "-c", src], capture_output=True,
                       text=True, timeout=900, cwd=ROOT)
    tail = [ln for ln in (p.stdout or "").splitlines()
            if ln.startswith(("OK", "FAIL", "SKIP"))]
    msg = tail[-1] if tail else (p.stderr or "")[-1500:]
    if msg.startswith("SKIP"):
        pytest.skip(msg)
    assert p.returncode == 0, msg
    assert msg.startswith("OK"), msg


def test_step2_every_family_is_flagged_run_as_typed_and_reset_to_its_seed():
    _drive(_STEP2_DRIVER)


def test_step3_every_family_is_flagged_run_as_typed_and_reset_to_its_seed():
    _drive(_STEP3_DRIVER)
