"""Ideal page: a loud warning for a limit far from Control, and Reset to Control.

2026-09-11: the operator's step-2 optimizer ran with every limit at its
MINIMUM (cap 500 t, harvest 0, floor 0, feed 0, move budget 1) after a mode
round trip — 99 cells, every one dropping batches with empty harvest weeks —
and nothing on the page said the limits were far from Control. The
round-trip bug is fixed (18e5735); this is the second line of defence:

- a limit is FLAGGED when it is 0 where its seed (the value its box or cell
  starts at: Control, the files, or for step 2's cap step 1's slider) is
  not, or below _IDEAL_FAR_LOW / above _IDEAL_FAR_HIGH times that seed (no
  ratio test on a seed of 0 or None); one st.warning per step lists each by
  its label, page value, seed and unit, above the step's run buttons;
- one Reset to Control button per step puts every limit of that step back to
  its seed — boxes, the tank & system limits table (a NEW editor, so the
  browser shows the seeds), the move budget and their _keep_ shadows —
  before a run, after one and after a mode round trip;
- the warning is display only: a run uses the values exactly as shown.

AppTest cannot see a browser's own widget copies; each box is also checked
to carry its value to a browser that has none (set_value), and the table is
driven with the stand-in the round-trip tests use. The real browser drive is
in the change report.
"""
import ast
import math
import os
import subprocess
import sys

import pandas as pd
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
# The rule and its texts (pure)
# --------------------------------------------------------------------------- #
def test_the_thresholds_are_one_named_pair():
    ns = _lift(_RULE, _CONSTS)
    assert (ns["_IDEAL_FAR_LOW"], ns["_IDEAL_FAR_HIGH"]) == (0.5, 2.0)


@pytest.mark.parametrize("value, seed, flagged", [
    (0, 26_000, True),          # 0 where the seed is not 0
    (0, 0, False),              # 0 where the seed is 0
    (0, None, True),            # 0 where the file sets nothing
    (12_999, 26_000, True),     # under half
    (13_000, 26_000, False),    # exactly half
    (52_000, 26_000, False),    # exactly double
    (52_001, 26_000, True),     # over double
    (78_000, 26_000, True),     # 3x
    (26_000, 26_000, False),
    (5, 0, False),              # no ratio test on a seed of 0 ...
    (5, None, False),           # ... or None
    (None, 100, False),         # a cleared cell keeps the file's value
    (float("nan"), 100, False),
    (-1, 100, True),
])
def test_the_far_rule(value, seed, flagged):
    assert _lift(_RULE, _CONSTS)["_ideal_far"](value, seed) is flagged


def test_the_flag_text_names_label_value_seed_and_unit():
    flags = _lift(_RULE, _CONSTS)["_ideal_limit_flags"]
    assert flags([("Min harvest / wk", 0, 26_000, "fish", "Control"),
                  ("Max harvest / wk", 60_000, 60_000, "fish", "Control"),
                  ("Biomass cap", 500, 3_800, "t", "step 1's cap slider"),
                  ("OG3N tank density cap", 85.25, 40, "kg/m³", "your files"),
                  ("OG6 system biomass limit", 0.0, None, "t", "your files")]) \
        == ["Min harvest / wk: 0 fish (Control 26,000)",
            "Biomass cap: 500 t (step 1's cap slider 3,800)",
            "OG3N tank density cap: 85.25 kg/m³ (your files 40)",
            "OG6 system biomass limit: 0 t (your files: not set)"]


def test_the_table_and_move_budget_become_flag_items():
    ns = _lift(_RULE, _CONSTS)
    items, flags = ns["_ideal_table_flag_items"], ns["_ideal_limit_flags"]
    seeds = {"OG3N": dict(tanks=10, density=95.0, biomass_t=450.0, feed=3000.0),
             "OG6": dict(tanks=6, density=None, biomass_t=None, feed=2000.0)}
    got = flags(items(seeds, 15,
                      {"OG3N": 300.0, "OG6": 500.0},
                      {"OG3N": {"biomass": 100_000.0, "feed_per_day": 3100.0},
                       "OG6": {"biomass": 0.0, "feed_per_day": 2500.0}},
                      {"max_transfers_per_week": 1}))
    assert got == ["OG3N tank density cap: 300 kg/m³ (your files 95)",
                   "OG3N system biomass limit: 100 t (your files 450)",
                   "OG6 system biomass limit: 0 t (your files: not set)",
                   "Weekly move budget: 1 moves (Control 15)"]
    # Control's 0 means "budget off": no ratio test against it.
    assert flags(items(seeds, 0, {}, {}, {"max_transfers_per_week": 40})) == []
    assert flags(items(seeds, 15, {}, {}, {})) == []


def test_a_density_seed_of_none_is_named_as_differing_tanks_not_unset():
    """_ideal_limit_seeds gives a system's density seed as None when its
    tanks have DIFFERENT caps in the files — not when nothing is set. A 0
    typed into that cell must not read '(your files: not set)' (review
    2026-09-12). A system limit the files leave unset still reads so."""
    ns = _lift(_RULE, _CONSTS)
    items, flags = ns["_ideal_table_flag_items"], ns["_ideal_limit_flags"]
    seeds = {"OG3N": dict(tanks=10, density=None, biomass_t=None, feed=None)}
    got = flags(items(seeds, 15, {"OG3N": 0.0},
                      {"OG3N": {"biomass": 0.0}}, {}))
    assert got == ["OG3N tank density cap: 0 kg/m³ (your files: its tanks' "
                   "caps differ)",
                   "OG3N system biomass limit: 0 t (your files: not set)"], got


# --------------------------------------------------------------------------- #
# Reset on the tank & system limits table (the round-trip stand-in)
# --------------------------------------------------------------------------- #
class _EditorSt:
    """data_editor records the table the browser is SENT and returns it with
    the edits the SERVER holds under the editor's key; number_input keeps
    its value in session state."""

    class column_config:
        Column = NumberColumn = staticmethod(lambda **kw: kw)

    def __init__(self):
        self.session_state = {}
        self.sent = {}
        self.last_key = None

    def data_editor(self, df, key=None, **kw):
        self.sent[key], self.last_key = df.copy(), key
        out = df.copy()
        edits = (self.session_state.get(key) or {}).get("edited_rows", {})
        for row, cells in edits.items():
            for col, v in cells.items():
                out.loc[out.index[row], col] = v
        return out

    def number_input(self, label, key=None, value=None, **kw):
        if key not in self.session_state:
            self.session_state[key] = value
        return self.session_state[key]

    def caption(self, *a, **kw):
        pass

    def warning(self, *a, **kw):
        pass


def test_reset_shows_and_runs_the_seeded_table_after_a_round_trip():
    from pathlib import Path
    from forecast.config_io import load_config
    control, _t, facility = load_config(os.path.join(ROOT, "config"))
    ctx = {"control": control, "facility": facility}
    fake = _EditorSt()
    ss = fake.session_state
    ns = _lift(("_ideal_limits_table", "_ideal_limit_seeds", "_ideal_default",
                "_resend_widget", "_ideal_editor_key", "_ideal_reset_limits"),
               ns={"st": fake, "os": os, "_ROOT": Path(ROOT)})
    table, reset = ns["_ideal_limits_table"], ns["_ideal_reset_limits"]
    col = "Tank density cap (kg/m³)"
    box_moves = max(1, int(control.max_transfers_per_week))

    def run(n):                             # one Ideal run, the n-th overall
        ss["_run_n"] = n
        return table(ctx, "t")

    run(1)
    seeded = fake.sent[fake.last_key].copy()
    new = float(seeded[col].iloc[0]) * 3

    def edit():
        ss[fake.last_key] = {"edited_rows": {0: {col: new}}, "added_rows": [],
                             "deleted_rows": []}   # the operator edits a cell
        ss["t_moves"] = box_moves + 7              # ... and the move budget

    def reset_then(n, what):
        k = fake.last_key
        reset({"ideal_x": 5}, "t", box_moves)      # the button's on_click
        assert ss["ideal_x"] == ss["_keep_ideal_x"] == 5
        dens, sys_ov, ctl = run(n)
        shown = fake.sent[fake.last_key]
        assert (dens, sys_ov, ctl) == ({}, {}, {}), (
            f"{what}: after Reset the run still uses {dens} {sys_ov} {ctl}")
        assert shown.equals(seeded), (
            f"{what}: after Reset the browser is sent a table that is not the "
            f"seeds:\n{shown}\nwant\n{seeded}")
        assert fake.last_key != k and ss.get(fake.last_key) is None, \
            f"{what}: Reset must draw the table as a NEW editor, no edits held"
        assert ss["t_moves"] == ss["_keep_t_moves"] == box_moves

    # Reset straight after an edit, on the page.
    edit()
    dens, _sys, ctl = run(2)
    assert dens and ctl                     # both are overrides now
    reset_then(3, "on the page")
    # Reset after a round trip: run 5 is another mode that ends in
    # st.stop(); run 6 is back here (the editor comes back as a new one,
    # showing the edit it still holds).
    edit()
    assert run(4)[0]
    assert run(6)[0] and float(fake.sent[fake.last_key][col].iloc[0]) == new
    reset_then(7, "after a round trip")
    run(8)                                  # a rerun on the page keeps it
    assert fake.sent[fake.last_key].equals(seeded)


def test_reset_of_step2_frees_the_cap_from_the_optimizer():
    fake = _EditorSt()
    ss = fake.session_state
    reset = _lift(("_ideal_reset_limits",), ns={"st": fake})[
        "_ideal_reset_limits"]
    ss.update({"ideal_ref_cap": 3200, "_keep_ideal_ref_cap": 3200,
               "_ideal_ref_cap_by_opt": 3200})
    reset({"ideal_ref_cap": 3800, "ideal_ref_hmin": 26_000}, "ideal_ref_lim", 15)
    assert ss["ideal_ref_cap"] == ss["_keep_ideal_ref_cap"] == 3800
    assert ss["ideal_ref_hmin"] == ss["_keep_ideal_ref_hmin"] == 26_000
    assert "_ideal_ref_cap_by_opt" not in ss
    assert ss["ideal_ref_lim_moves"] == ss["_keep_ideal_ref_lim_moves"] == 15


def test_each_reset_button_says_what_it_overwrites():
    """House style: the help names every input it overwrites and says it
    changes nothing in Configure or the files."""
    with open(APP, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    helps = {}
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "button"
                and any(k.arg == "on_click"
                        and ast.unparse(k.value) == "_ideal_reset_limits"
                        for k in n.keywords)):
            kw = {k.arg: k.value for k in n.keywords}
            helps[ast.literal_eval(kw["key"])] = ast.unparse(kw["help"])
            assert "Reset to Control" in ast.unparse(n.args[0])
    assert set(helps) == {"ideal_ref_reset", "ideal_tr_reset"}, helps
    for key, h in helps.items():
        for word in ("Biomass cap", "Max harvest / wk", "Min harvest / wk",
                     "Min harvest weight", "Max feed / day",
                     "Tank & system limits", "move budget", "Configure",
                     "files"):
            assert word in h, f"{key}: its help does not name {word!r}"


def _flat_help(key):
    """The help text of the widget with key=`key`, its f-string parts
    joined and whitespace squashed."""
    with open(APP, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        kw = {k.arg: k.value for k in n.keywords}
        if "key" in kw and isinstance(kw["key"], ast.Constant) \
                and kw["key"].value == key and "help" in kw:
            h = kw["help"]
            parts = ([v.value if isinstance(v, ast.Constant) else "{}"
                      for v in h.values] if isinstance(h, ast.JoinedStr)
                     else [ast.literal_eval(h)])
            return " ".join("".join(parts).split())
    raise AssertionError(f"no widget with key={key!r} and a help text")


def test_what_else_a_reset_overwrites_is_named():
    """Step 2's optimizer Use button also writes step 3's what-if cap
    (_ideal_opt_use: ss["ideal_tr_cap"] = cap_t), and a step-1 scan copies
    its weight into step 2's Min harvest weight: each Reset overwrites
    those too, so its help must say so — as must step 3's cap box, which
    named only the transition optimizer (review 2026-09-12)."""
    tr = _flat_help("ideal_tr_reset")
    assert "step 2's optimizer" in tr and "transition optimizer" in tr, tr
    ref = _flat_help("ideal_ref_reset")
    assert "step-1 scan" in ref and "optimizer Use button" in ref, ref
    cap = _flat_help("ideal_tr_cap")
    assert "step 2's optimizer" in cap and "transition optimizer" in cap, cap
    # ... and the code still does what the help now says.
    src = open(APP, encoding="utf-8").read()
    use = src[src.index("def _ideal_opt_use("):]
    use = use[:use.index("\ndef ")]
    assert 'ss["ideal_tr_cap"] = cap_t' in use
    scan = src[src.index('"🎯 Find the ideal rhythm"'):]
    assert 'st.session_state["ideal_ref_wmin"] = int(min_wt)' in \
        scan[:scan.index("_ideal_reference(ctx, today, cap_t)")]


# --------------------------------------------------------------------------- #
# The real page (step 2) and the synthetic step-3 page, in AppTest
# --------------------------------------------------------------------------- #
_COMMON = r'''
import sys
sys.path.insert(0, %(root)r)
try:
    from streamlit.testing.v1 import AppTest
    from streamlit.testing.v1.element_tree import Button, Warning
except Exception as e:
    print("SKIP no AppTest: %%s" %% e)
    raise SystemExit(0)
from forecast.config_io import load_config
CTRL, _t, _f = load_config(%(root)r + "/config")
HMAX, HMIN = int(CTRL.max_harvest_per_week), int(CTRL.min_harvest_per_week)
WMIN, FEED = int(CTRL.min_harvest_weight_g), int(CTRL.max_feed_per_day_kg)
MOVES = max(1, int(CTRL.max_transfers_per_week))

def fail(msg):
    print("FAIL " + msg)
    raise SystemExit(1)

def boom(at, what):
    if at.exception:
        fail(what + ": " + "; ".join(str(e)[:300] for e in at.exception))

def walk(node, out=None):
    out = [] if out is None else out
    for c in getattr(node, "children", {}).values():
        out.append(c)
        walk(c, out)
    return out

def far(at):
    return [w.value for w in at.warning if "far from Control" in w.value]

def check_far(at, what, want, buttons):
    w = far(at)
    if len(w) != 1:
        fail(what + ": want ONE far-from-Control warning, got %%r" %% w)
    miss = [x for x in want if x not in w[0]]
    if miss:
        fail(what + ": the warning does not list %%r: %%r" %% (miss, w[0]))
    els = walk(at.main)
    iw = next(i for i, e in enumerate(els)
              if isinstance(e, Warning) and "far from Control" in e.value)
    for lab in buttons:
        ib = [i for i, e in enumerate(els)
              if isinstance(e, Button) and lab in str(e.label)]
        if not ib or min(ib) < iw:
            fail(what + ": the warning is not above the %%r button" %% lab)

def check_reset(at, what, key, seeds):
    at.button(key=key).click().run()
    boom(at, what + ": Reset")
    got = {k: at.number_input(key=k).value for k in seeds}
    if got != seeds:
        fail(what + ": after Reset %%r, want %%r" %% (got, seeds))
    if far(at):
        fail(what + ": the warning is still up after Reset: %%r" %% far(at))
    for k, v in seeds.items():
        if at.session_state["_keep_" + k] != v:
            fail(what + ": %%s's _keep_ shadow is %%r after Reset"
                 %% (k, at.session_state["_keep_" + k]))
        p = at.number_input(key=k).proto
        if not p.set_value and float(p.default) != float(v):
            fail(what + ": %%s would show %%r in a browser without its own "
                 "copy (server %%r)" %% (k, p.default, v))
    at.run()
    boom(at, what + ": rerun after Reset")
    got = {k: at.number_input(key=k).value for k in seeds}
    if got != seeds:
        fail(what + ": a rerun after Reset lost the seeds: %%r" %% got)
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
    CALLS.append(dict(overrides or {}))
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
SLIDER = at.slider(key="ideal_cap_t").value
SEEDS = {"ideal_ref_cap": SLIDER, "ideal_ref_hmax": HMAX,
         "ideal_ref_hmin": HMIN, "ideal_ref_wmin": WMIN,
         "ideal_ref_feed": FEED, "ideal_ref_lim_moves": MOVES}
got = {k: at.number_input(key=k).value for k in SEEDS}
if got != SEEDS:
    fail("the page does not start at the seeds: %%r vs %%r" %% (got, SEEDS))
if far(at):
    fail("a warning on the untouched page: %%r" %% far(at))
WANT = [f"Min harvest / wk: 0 fish (Control {HMIN:,})",
        f"Max harvest / wk: {3 * HMAX:,} fish (Control {HMAX:,})",
        f"Biomass cap: 500 t (step 1's cap slider {SLIDER:,})"]
if 1 < 0.5 * MOVES:
    WANT.append(f"Weekly move budget: 1 moves (Control {MOVES})")
BUTTONS = ("Find the best rhythm", "Run the reference sheet")

def go_far():
    at.number_input(key="ideal_ref_hmin").set_value(0)
    at.number_input(key="ideal_ref_hmax").set_value(3 * HMAX)
    at.number_input(key="ideal_ref_cap").set_value(500)
    at.number_input(key="ideal_ref_lim_moves").set_value(1).run()
    boom(at, "far values")

go_far()
check_far(at, "before any run", WANT, BUTTONS)
n0 = at.session_state["ideal_ref_lim_nonce"]
check_reset(at, "before any run", "ideal_ref_reset", SEEDS)
if at.session_state["ideal_ref_lim_nonce"] <= n0:
    fail("Reset did not draw the limits table as a new editor")

# Display only: the optimizer runs on the values as typed, flagged or not.
go_far()
at.text_input(key="ideal_opt_cads").set_value("49")
at.number_input(key="ideal_opt_smin").set_value(200000)
at.number_input(key="ideal_opt_smax").set_value(200000)
at.text_input(key="ideal_opt_caps").set_value(str(SLIDER)).run()
at.button(key="ideal_opt_run").click().run()
boom(at, "optimizer run")
if len(CALLS) != 1:
    fail("the optimizer did not run once: %%r" %% CALLS)
ov = CALLS[0]
if (ov.get("min_harvest_per_week"), ov.get("max_harvest_per_week")) != (
        0.0, 3.0 * HMAX):
    fail("the run did not use the page's values as typed: %%r" %% ov)
check_far(at, "after a run", WANT, BUTTONS)
check_reset(at, "after a run", "ideal_ref_reset", SEEDS)

go_far()
at.radio(key="app_mode").set_value("How it works (the rules)").run()
at.radio(key="app_mode").set_value(IDEAL).run()
boom(at, "round trip")
check_far(at, "after a How it works round trip", WANT, BUTTONS)
check_reset(at, "after a How it works round trip", "ideal_ref_reset", SEEDS)
print("OK step 2: flagged before a run, after a run and after a round trip; "
      "Reset restores the seeds each time")
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
    CALLS.append(dict(kw.get("overrides") or {}))
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
mode = st.radio("Mode", ["Ideal", "Other"], key="app_mode")
if mode == "Ideal":
    ns["_ideal_restore"]()
    ns["_ideal_transition"](ctx, (49, 340000), 3800)
    ns["_ideal_save"]()
    st.stop()
st.write("another mode")
st.stop()
"""

at = AppTest.from_string(SCRIPT, default_timeout=120)
at.run()
boom(at, "render")
CAP = min(10_000, max(500, int(round(float(CTRL.max_biomass_kg) / 1000.0))))
SEEDS = {"ideal_tr_cap": CAP, "ideal_tr_hmax": HMAX, "ideal_tr_hmin": HMIN,
         "ideal_tr_wmin": WMIN, "ideal_tr_feed": FEED,
         "ideal_tr_lim_moves": MOVES}
got = {k: at.number_input(key=k).value for k in SEEDS}
if got != SEEDS:
    fail("step 3 does not start at the seeds: %%r vs %%r" %% (got, SEEDS))
if far(at):
    fail("a warning on the untouched step 3: %%r" %% far(at))
WANT = [f"Min harvest / wk: 0 fish (Control {HMIN:,})",
        f"Max feed / day: {3 * FEED:,} kg (Control {FEED:,})"]
if 1 < 0.5 * MOVES:
    WANT.append(f"Weekly move budget: 1 moves (Control {MOVES})")
BUTTONS = ("Find the best transition", "Check both schedules")

def go_far():
    at.number_input(key="ideal_tr_hmin").set_value(0)
    at.number_input(key="ideal_tr_feed").set_value(3 * FEED)
    at.number_input(key="ideal_tr_lim_moves").set_value(1).run()
    boom(at, "far values")

go_far()
check_far(at, "before any run", WANT, BUTTONS)
# Display only: the proposal's what-ifs are the values as typed.
if not any("The proposal runs with changed limits:" in c.value
           and "Min harvest / wk 0 fish" in c.value for c in at.caption):
    fail("the proposal does not run with the typed floor of 0")
check_reset(at, "before any run", "ideal_tr_reset", SEEDS)

go_far()
at.number_input(key="ideal_tr_opt_smin").set_value(280000)
at.number_input(key="ideal_tr_opt_smax").set_value(280000)
at.text_input(key="ideal_tr_opt_caps").set_value("3800").run()
at.button(key="ideal_tr_opt_run").click().run()
boom(at, "transition optimizer run")
if not CALLS or CALLS[0].get("min_harvest_per_week") != 0.0:
    fail("the transition optimizer did not run on the typed floor of 0: %%r"
         %% CALLS[:1])
check_far(at, "after a run", WANT, BUTTONS)
check_reset(at, "after a run", "ideal_tr_reset", SEEDS)

go_far()
at.radio(key="app_mode").set_value("Other").run()
at.radio(key="app_mode").set_value("Ideal").run()
boom(at, "round trip")
check_far(at, "after a round trip", WANT, BUTTONS)
check_reset(at, "after a round trip", "ideal_tr_reset", SEEDS)
print("OK step 3: flagged before a run, after a run and after a round trip; "
      "Reset restores the seeds each time")
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


def test_step2_flags_far_limits_and_reset_restores_them():
    _drive(_STEP2_DRIVER)


def test_step3_flags_far_limits_and_reset_restores_them():
    _drive(_STEP3_DRIVER)
