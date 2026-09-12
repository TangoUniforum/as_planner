"""Two defects the widget round-trip fix left behind (review 2026-09-12).

1. _resend_widget re-sent a pick that was no longer among the widget's
   options. Streamlit 1.50 puts a select's options in its widget id, so new
   options make a new widget, and a stale pick pushed into it raises (int
   options: TypeError "bad argument type for built-in operation"; radio:
   ValueError) — where before the re-send it quietly started from its
   default. One ordinary click did it on the Run page's FW->OG intake: a tank
   picked for SMALLER, then added to BIGGER (the SMALLER options drop it),
   and the page crashed on that click and every rerun after. A string pick
   survived instead (the promote picker, the batch filter).

2. Configure's data editors: after Configure -> How it works -> Configure a
   table showed the FILE's value while Save wrote the unsaved edit the page
   no longer showed (real browser, on a copy: volume_m3 5.5 edited to 999,
   shown 5.5, saved 999). Every mode but Run forecast ends in st.stop(); the
   server keeps an undrawn editor's edits, the browser drops them.
"""
import ast
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")
sys.path.insert(0, ROOT)

pytestmark = pytest.mark.skipif(
    not (os.path.exists(APP)
         and os.path.exists(os.path.join(ROOT, "config", "facility.yaml"))),
    reason="needs app.py + a seeded config")


def _tree():
    with open(APP, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _lift(names, assigns=(), ns=None):
    """app.py's own functions (and module constants), by name."""
    body = [n for n in _tree().body
            if (isinstance(n, ast.FunctionDef) and n.name in names)
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) in assigns for t in n.targets))]
    found = {n.name for n in body if isinstance(n, ast.FunctionDef)}
    assert found == set(names), f"app.py lost {sorted(set(names) - found)}"
    ns = dict(ns or {})
    exec(compile(ast.Module(body=body, type_ignores=[]), APP, "exec"), ns)
    return ns


def _parents(tree):
    return {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}


def _scope(n, parent, tree):
    while n in parent:
        n = parent[n]
        if isinstance(n, ast.FunctionDef):
            return n
    return tree


def _ident(e):
    return (("lit", e.value) if isinstance(e, ast.Constant)
            else ("expr", ast.dump(e)))


# --------------------------------------------------------------------------- #
# 1. A pick no longer among the options is not re-sent
# --------------------------------------------------------------------------- #
# The call sites in their exact shape (app.py _mw_fw_intake, _mw_copilot, the
# promote picker, the batch filter, a radio), with app.py's own
# _resend_widget lifted from the file and passed the options the widget gets.
_STALE_PICK_DRIVER = r'''
import sys
try:
    from streamlit.testing.v1 import AppTest
except Exception as e:
    print("SKIP no AppTest: %s" % e)
    raise SystemExit(0)

SCRIPT = "APP = " + repr(@APP@) + "\n" + """
import ast
import streamlit as st
tree = ast.parse(open(APP, encoding="utf-8").read())
exec(compile(ast.Module(body=[n for n in tree.body
     if isinstance(n, ast.FunctionDef) and n.name == "_resend_widget"],
     type_ignores=[]), "app.py", "exec"))
ss = st.session_state
case = ss["_case"]
if case == "fw":                        # tank_id is an int (models.py)
    sfx = "B60_3"
    open_og = [11, 12, 13, 14]
    _resend_widget(f"mw_fw_big_{sfx}", options=open_og)
    big = st.multiselect("Tank(s) for the BIGGER grade", options=open_og,
                         key=f"mw_fw_big_{sfx}")
    small_opts = [t for t in open_og if t not in big]
    _resend_widget(f"mw_fw_small_{sfx}", options=small_opts)
    small = st.multiselect("Tank(s) for the SMALLER grade",
                           options=small_opts, key=f"mw_fw_small_{sfx}")
    st.write("big=%r small=%r" % (big, small))
elif case == "copilot":                 # options = range(len(props))
    props = ss.get("props", ["W37", "W38", "W39", "W40", "W41"])
    _resend_widget("mw_cp_week_sel", options=range(len(props)))
    sel = st.selectbox("Show recommendations for week", range(len(props)),
                       format_func=lambda i: props[i], key="mw_cp_week_sel")
    st.write("sel=%r" % (sel,))
elif case == "sel_str":                 # the promote picker
    opts = ss.get("opts", ["A", "B", "C"])
    _resend_widget("w", options=opts)
    st.write("sel=%r" % (st.selectbox("w", opts, key="w"),))
elif case == "multi_str":               # the batch filter
    opts = ss.get("opts", ["B9", "B10", "B11"])
    _resend_widget("w", options=opts)
    st.write("multi=%r" % (st.multiselect("w", opts, key="w"),))
else:                                   # a radio whose options change
    opts = ss.get("opts", ["A", "B", "C"])
    _resend_widget("w", options=opts)
    st.write("radio=%r" % (st.radio("w", opts, key="w"),))
"""


def fail(msg):
    print("FAIL " + msg)
    raise SystemExit(1)


def ok(at, what):
    if at.exception:
        fail(what + " raised: "
             + "; ".join(str(e.value)[:200] for e in at.exception))


def shows(at, want, what):
    got = [m.value for m in at.markdown]
    if got != [want]:
        fail("%s: the page says %r, want %r" % (what, got, [want]))


def new(case):
    at = AppTest.from_string(SCRIPT, default_timeout=60)
    at.session_state["_case"] = case
    at.run()
    ok(at, case + ": the first render")
    return at


what = "FW->OG intake: tank 12 picked for SMALLER, then added to BIGGER"
at = new("fw")
at.multiselect(key="mw_fw_small_B60_3").set_value([12]).run()
at.multiselect(key="mw_fw_big_B60_3").set_value([12]).run()
ok(at, what)
shows(at, "big=[12] small=[]", what)
at.run()
ok(at, what + ", then any rerun")

what = "FW->OG intake: a SMALLER pick still offered"
at = new("fw")
at.multiselect(key="mw_fw_small_B60_3").set_value([13]).run()
at.multiselect(key="mw_fw_big_B60_3").set_value([12]).run()
ok(at, what)
shows(at, "big=[12] small=[13]", what)

what = "co-pilot: week 4 of 5 picked, then 2 weeks re-proposed"
at = new("copilot")
at.selectbox(key="mw_cp_week_sel").set_value(3).run()
at.session_state["props"] = ["W38", "W39"]
at.run()
ok(at, what)
shows(at, "sel=0", what)

for case, pick, opts, want, what in (
        ("sel_str", "C", ["A", "B"], "sel='A'",
         "promote picker: a candidate no longer on the board"),
        ("sel_str", "B", ["A", "B"], "sel='B'",
         "promote picker: a candidate still on the board"),
        ("multi_str", ["B10", "B11"], ["B9", "B10"], "multi=['B10']",
         "batch filter: one batch no longer in the PR"),
        ("radio_str", "C", ["A", "B"], "radio='A'",
         "radio: an option no longer offered")):
    at = new(case)
    w = (at.selectbox if case == "sel_str" else
         at.multiselect if case == "multi_str" else at.radio)
    w(key="w").set_value(pick).run()
    at.session_state["opts"] = opts
    at.run()
    ok(at, what)
    shows(at, want, what)
print("OK a pick no longer offered starts from the default; one still "
      "offered is kept")
'''


def _run_driver(src):
    p = subprocess.run([sys.executable, "-c", src], capture_output=True,
                       text=True, timeout=600, cwd=ROOT)
    tail = [ln for ln in (p.stdout or "").splitlines()
            if ln.startswith(("OK", "FAIL", "SKIP"))]
    msg = tail[-1] if tail else (p.stderr or "")[-800:]
    if msg.startswith("SKIP"):
        pytest.skip(msg)
    assert p.returncode == 0, msg
    assert msg.startswith("OK"), msg


def test_a_pick_no_longer_offered_is_not_re_sent():
    _run_driver(_STALE_PICK_DRIVER.replace("@APP@", repr(APP)))


# A radio drawn by _ideal_restore's loop whose options are not literal, and
# what keeps a stale pick away from it instead of options=.
_OWN_GUARD = {"ideal_tr_opt_obj": "_ideal_tr_obj_reset"}


def test_every_re_sent_pick_whose_options_can_change_passes_them():
    """The runtime half is above; this is the call sites. A keyed selectbox /
    radio / multiselect whose key is re-sent must pass _resend_widget the
    SAME options expression it draws with, unless its options are a literal
    (or list() of a module-level literal tuple). It reads app.py: AppTest
    cannot reach the Run page's panels without an uploaded PR."""
    tree = _tree()
    parent = _parents(tree)
    consts = {t.id for n in tree.body if isinstance(n, ast.Assign)
              and isinstance(n.value, (ast.Tuple, ast.List))
              and all(isinstance(e, ast.Constant) for e in n.value.elts)
              for t in n.targets if isinstance(t, ast.Name)}

    def literal(e):
        if isinstance(e, (ast.List, ast.Tuple)):
            return all(isinstance(x, ast.Constant) for x in e.elts)
        return (isinstance(e, ast.Call) and getattr(e.func, "id", None) == "list"
                and len(e.args) == 1 and isinstance(e.args[0], ast.Name)
                and e.args[0].id in consts)

    keep = next(n for n in tree.body if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "_IDEAL_KEEP"
                        for t in n.targets))
    ideal_keep = set(ast.literal_eval(keep.value))
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for k, fn in _OWN_GUARD.items():
        assert fn in funcs and k in ast.unparse(funcs[fn]), (k, fn)

    resent = {}
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "_resend_widget" and n.args):
            kw = {k.arg: k.value for k in n.keywords if k.arg}
            resent.setdefault(_scope(n, parent, tree), []).append(
                (n.lineno, _ident(n.args[0]),
                 ast.dump(kw["options"]) if "options" in kw else None))
    offenders, checked = [], 0
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in ("selectbox", "radio", "multiselect")):
            continue
        kw = {k.arg: k.value for k in n.keywords if k.arg}
        opts = n.args[1] if len(n.args) > 1 else kw.get("options")
        if "key" not in kw or opts is None:
            continue
        k = _ident(kw["key"])
        if k[0] == "lit" and k[1] in ideal_keep:
            checked += 1
            if not literal(opts) and k[1] not in _OWN_GUARD:
                offenders.append(f"line {n.lineno}: {k[1]} (re-sent by "
                                 f"_ideal_restore, which passes no options) "
                                 f"draws options {ast.unparse(opts)}")
            continue
        prior = [r for r in resent.get(_scope(n, parent, tree), ())
                 if r[0] < n.lineno and r[1] == k]
        if not prior:
            continue
        checked += 1
        if literal(opts):
            continue
        line, _k, sent = max(prior)
        if sent != ast.dump(opts):
            offenders.append(
                f"line {line}: _resend_widget({ast.unparse(kw['key'])}) for "
                f"the {n.func.attr} at line {n.lineno} "
                + ("passes no options=" if sent is None
                   else "passes options= that differ from the widget's")
                + f" (the widget draws {ast.unparse(opts)})")
    assert checked >= 20, f"the scan found only {checked} re-sent pickers"
    assert not offenders, (
        "a re-sent pick whose options can change: when a pick is no longer "
        "offered the re-send pushes it into the new widget and the page "
        "raises (int options) or keeps it (strings) — " + "; ".join(offenders))


# --------------------------------------------------------------------------- #
# 2. Configure's data editors: shown == returned == saved
# --------------------------------------------------------------------------- #
class _Rerun(BaseException):
    """Streamlit's RerunException is a BaseException too: the page's
    `except Exception` around Save must not swallow it."""


class _BrowserSt:
    """Stand-in Streamlit. `data_editor` records the table the browser is
    SENT under each key and returns it with the edits the SERVER holds under
    that key (registered, empty, on its first draw). A browser that dropped
    its copy shows exactly the sent table."""

    class column_config:
        Column = NumberColumn = TextColumn = SelectboxColumn = staticmethod(
            lambda *a, **kw: kw)

    def __init__(self):
        self.session_state = {}
        self.sent = {}
        self.last_key = None
        self.clicks = set()
        self.errors = []

    def data_editor(self, df, key=None, **kw):
        self.sent[key], self.last_key = df.copy(), key
        state = self.session_state.setdefault(
            key, {"edited_rows": {}, "added_rows": [], "deleted_rows": []})
        out = df.copy()
        for row, cells in state.get("edited_rows", {}).items():
            for col, v in cells.items():
                out.loc[out.index[int(row)], col] = v
        return out

    def columns(self, spec, **kw):
        return [self] * (len(spec) if isinstance(spec, (list, tuple))
                         else int(spec))

    def button(self, label, key=None, **kw):
        return key in self.clicks

    def rerun(self):
        raise _Rerun()

    def error(self, msg, *a, **kw):
        self.errors.append(str(msg))

    def caption(self, *a, **kw):
        pass

    markdown = success = warning = info = caption


def _edits(state):
    return any((state or {}).get(k) for k in
               ("edited_rows", "added_rows", "deleted_rows"))


@pytest.fixture
def cfg_dir(tmp_path):
    """A copy of the config, never the live one (and never costs.yaml)."""
    dst = tmp_path / "config"
    shutil.copytree(os.path.join(ROOT, "config"), dst,
                    ignore=shutil.ignore_patterns("costs.yaml"))
    return dst


def _facility_page(fake, cfg_dir):
    ns = _lift(("_edit_facility", "_persist", "_read_or_explain", "_records",
                "_clean_rows", "_blank", "_reset_keys", "_forget_editor",
                "_data_editor", "_editor_has_edits", "_same_table"),
               assigns=("_FACILITY_HELP", "_READ_FIX_HINT"),
               ns={"st": fake, "pd": pd, "CONFIG_DIR": cfg_dir})
    page = ns["_edit_facility"]

    def run(n):                         # one Configure run, the n-th overall
        fake.session_state["_run_n"] = n
        try:
            page()
        except _Rerun:
            return "rerun"
        assert not fake.errors, fake.errors
        return None
    return run


def _volume_on_file(cfg_dir, tank_id):
    from forecast.config_io import facility_to_dict, load_facility_config
    tanks = facility_to_dict(load_facility_config(str(cfg_dir)))["tanks"]
    return float(next(t["volume_m3"] for t in tanks
                      if str(t["tank_id"]) == str(tank_id)))


def _edit_row0_volume(fake, run):
    """Draw Configure -> Facility, edit row 0's volume, rerun on the page,
    then leave for a mode that ends in st.stop() (run 3: not drawn) and come
    back (run 4). -> (tank_id, volume before, volume typed)."""
    ss = fake.session_state
    run(1)
    k0 = fake.last_key
    assert k0 == "fac_df_w"             # no round trip: the key as always
    tank = fake.sent[k0]["tank_id"].iloc[0]
    was = float(fake.sent[k0]["volume_m3"].iloc[0])
    new = was + 993.5
    ss[k0] = {"edited_rows": {0: {"volume_m3": new}}, "added_rows": [],
              "deleted_rows": []}       # the operator edits the cell
    run(2)
    assert fake.last_key == k0, "a rerun on the page re-mounted the editor"
    run(4)
    return tank, was, new


def test_a_configure_table_edit_is_shown_and_saved_after_a_round_trip(cfg_dir):
    fake = _BrowserSt()
    run = _facility_page(fake, cfg_dir)
    tank, was, new = _edit_row0_volume(fake, run)
    k = fake.last_key
    shown = float(fake.sent[k]["volume_m3"].iloc[0])  # the browser: no edits
    assert shown == new, (
        f"back on Configure the table shows {shown:g} while the server holds "
        f"{new:g} — which Save would write")
    assert not _edits(fake.session_state.get(k))      # nothing unseen held
    fake.clicks = {"save_fac"}
    assert run(5) == "rerun"
    saved = _volume_on_file(cfg_dir, tank)
    assert saved == shown, f"Save wrote {saved:g}, the page showed {shown:g}"


def test_reload_after_a_round_trip_shows_the_file_again(cfg_dir):
    fake = _BrowserSt()
    run = _facility_page(fake, cfg_dir)
    tank, was, new = _edit_row0_volume(fake, run)
    fake.clicks = {"reload_fac"}
    assert run(5) == "rerun"
    fake.clicks = set()
    run(6)
    k = fake.last_key
    shown = float(fake.sent[k]["volume_m3"].iloc[0])
    assert shown == was == _volume_on_file(cfg_dir, tank), (
        f"after Reload the table shows {shown:g}, the file holds {was:g}")
    assert k == "fac_df_w" and not _edits(fake.session_state.get(k))


def test_a_folded_table_gives_way_when_the_table_underneath_changes():
    """The targets, price-band and feed-price tables are rebuilt from their
    files on every run. A folded table stands in for that data only while
    the data is unchanged — a Save or a change on disk wins."""
    fake = _BrowserSt()
    ss = fake.session_state
    ed = _lift(("_data_editor", "_editor_has_edits", "_same_table"),
               ns={"st": fake})["_data_editor"]
    d1 = pd.DataFrame({"x": [1.0, 2.0]})

    def run(n, data):
        ss["_run_n"] = n
        out = ed(data, key="t")
        return fake.sent[fake.last_key]["x"].tolist(), out["x"].tolist()

    run(1, d1.copy())
    ss["t"] = {"edited_rows": {0: {"x": 9.0}}, "added_rows": [],
               "deleted_rows": []}
    assert run(2, d1.copy())[1] == [9.0, 2.0]
    assert fake.last_key == "t"
    shown, out = run(4, d1.copy())      # back after a stopped mode
    assert shown == out == [9.0, 2.0], (shown, out)
    k = fake.last_key
    assert run(5, d1.copy()) == ([9.0, 2.0], [9.0, 2.0])
    assert fake.last_key == k, "a rerun re-mounted the folded editor"
    ss.pop(k)                           # a Run-forecast trip drops it all
    assert run(7, d1.copy()) == ([9.0, 2.0], [9.0, 2.0])
    d2 = pd.DataFrame({"x": [9.0, 3.0]})  # saved / changed on disk
    shown, out = run(8, d2)
    assert shown == out == [9.0, 3.0], (
        f"the file now holds [9, 3]; the page shows {shown}, runs {out}")


def test_bumping_the_manual_grid_forgets_the_old_grid():
    ss = {"mw_grid_nonce": 3, "_ed_gen_mw_grid_3": 1, "mw_grid_3~1": {},
          "_ed_fold_mw_grid_3": ("mw_grid_3~1", None, None),
          "_ed_last_mw_grid_3": ("mw_grid_3~1", None), "_ed_run_mw_grid_3": 7}
    ns = _lift(("_mw_bump_grid", "_forget_editor"),
               ns={"st": SimpleNamespace(session_state=ss)})
    ns["_mw_bump_grid"]()
    assert ss == {"mw_grid_nonce": 4}


def test_every_data_editor_in_the_app_goes_through_a_round_trip_rule():
    """Only _data_editor draws st.data_editor, apart from the Ideal page's
    two editors, which take their key from _ideal_editor_key (pinned by
    test_both_ideal_data_editors_take_their_key_from_the_shared_rule)."""
    tree = _tree()
    parent = _parents(tree)
    allowed = {("_data_editor", "wkey"), ("_ideal_reference", "ed_key"),
               ("_ideal_limits_table", "ed_key")}
    direct = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == \
                "data_editor":
            fn = _scope(n, parent, tree)
            key = next((ast.unparse(k.value) for k in n.keywords
                        if k.arg == "key"), None)
            if (getattr(fn, "name", None), key) not in allowed:
                direct.append(f"line {n.lineno} in "
                              f"{getattr(fn, 'name', '<module>')}: "
                              f"data_editor(key={key})")
    assert not direct, (
        "a data editor drawn outside the round-trip rule shows the table "
        "without the edits the server still holds after a trip through "
        "another mode, while returning them — " + "; ".join(direct))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "_data_editor"]
    assert len(calls) >= 15, f"only {len(calls)} editors use _data_editor"
    assert all(any(k.arg == "key" for k in c.keywords) for c in calls)
