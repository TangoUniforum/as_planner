"""Editor side of graded_transfer: raw-grid round trip, the timeline summary,
and the click panel (rendered under AppTest in a fresh interpreter).

The panel test drives the real widgets: pick two destinations, set a count,
read the before/after table and the balance line, and press Add — then checks
the event that lands in the working set is the one the engine test suite
proves balances (tests/test_graded_transfer.py).
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

app = pytest.importorskip("app", reason="app.py not importable without Streamlit")


def _row(to_tanks, count, mode="grade"):
    return {"week": 2, "type": "graded_transfer", "batch": "", "from_tank": 41,
            "to_tanks": to_tanks, "count": count, "mode": mode, "notes": "n"}


def test_grid_row_to_event_keeps_order_counts_and_the_blank_remainder():
    ev = app._rows_to_manual_events([_row("43:20000,41,45:10000", None)])[0]
    assert ev.type == "graded_transfer" and ev.from_tank == 41
    assert [(d.tank, d.count) for d in ev.destinations] == [
        (43, 20000.0), (41, None), (45, 10000.0)]
    assert ev.count is None and ev.mode == "grade"


def test_grid_partial_split_round_trips():
    rows = [_row("43:15000,45", 25000, mode="split")]
    evs = app._rows_to_manual_events(rows)
    back = app._manual_events_to_df_rows(evs)[0]
    assert back["to_tanks"] == "43:15000,45"
    assert back["count"] == 25000 and back["mode"] == "split"
    again = app._rows_to_manual_events([back])[0]
    assert [(d.tank, d.count) for d in again.destinations] == [
        (43, 15000.0), (45, None)]
    assert again.count == 25000.0 and again.mode == "split"


def test_grid_blank_mode_means_grade():
    ev = app._rows_to_manual_events([_row("43:1000,45", None, mode="")])[0]
    assert ev.mode == "grade"
    assert app._manual_events_to_df_rows([ev])[0]["mode"] == "grade"


def test_grid_does_not_spread_the_count_column_over_bare_tanks():
    """Unlike og_transfer, `count` here is the number taken OUT of the source;
    the bare tank is the remainder the engine computes — it must stay None."""
    ev = app._rows_to_manual_events([_row("43:1000,45", 5000)])[0]
    assert [(d.tank, d.count) for d in ev.destinations] == [(43, 1000.0), (45, None)]


_DRIVER = r'''
import sys
sys.path.insert(0, %(root)r)
from streamlit.testing.v1 import AppTest

SCRIPT = r"""
import sys
sys.path.insert(0, %(root)r)
from datetime import date
import streamlit as st
import app
from forecast.state import FacilityState, TankState

st_ = FacilityState(date(2026, 10, 5), [
    TankState("OG3N-3", 3, "OG3N", 300.0, 95.0, 1000.0, "OG"),
    TankState("OG4N-4", 4, "OG4N", 300.0, 95.0, 1000.0, "OG"),
    TankState("OG5N-5", 5, "OG5N", 300.0, 95.0, 1000.0, "OG"),
])
st_.tanks_by_id[3].assign("B50", 60000, 2400.0, 16.0, "SW")

class _R:
    tank_id = 3; week_label = "2026-W41"; count = 60000.0; avg_wt_g = 2400.0
    batch_id = "B50"; density_kg_m3 = 480.0
r = _R()
class _C:
    grade_efficiency = 0.85
ctx = {"control": _C(), "batch_by_id": {}}
st.session_state.setdefault("_mw_events_test", [])
app._mw_add = lambda ev: st.session_state["_mw_events_test"].append(ev)
app._mw_graded_transfer_panel(st_, ctx, [r], r, 3, 1, "2026-W41", "3_1",
                              [4, 5], lambda t: f"T{t}")
"""

at = AppTest.from_string(SCRIPT, default_timeout=240)
at.run()
if at.exception:
    print("FAIL render: " + "; ".join(str(e)[:400] for e in at.exception)); raise SystemExit(1)
ms = [m for m in at.multiselect if m.key == "mw_gt_dest_3_1"]
if not ms:
    print("FAIL no destination picker"); raise SystemExit(1)
ms[0].set_value([4, 5]).run()
if at.exception:
    print("FAIL after picks: " + "; ".join(str(e)[:400] for e in at.exception)); raise SystemExit(1)
ni = [n for n in at.number_input if n.key == "mw_gt_cnt_3_1_4"]
if not ni:
    print("FAIL no count box"); raise SystemExit(1)
ni[0].set_value(20000.0).run()
ok = [s.value for s in at.success]
if not any("Balance" in v and "+0.0 kg" in v for v in ok):
    print("FAIL no balanced line: %%r / errors %%r" %% (ok, [e.value for e in at.error])); raise SystemExit(1)
df = at.dataframe[0].value
names = list(df["Tank"])
if len(names) != 4:
    print("FAIL table rows %%r" %% names); raise SystemExit(1)
big = df.iloc[1]; small = df.iloc[2]
if not (big["Fish"] == 20000 and small["Fish"] == 40000 and big["Avg wt (kg)"] > 2.4 > small["Avg wt (kg)"]):
    print("FAIL legs %%r" %% df.to_dict("records")); raise SystemExit(1)
btn = [b for b in at.button if b.key == "mw_gt_add"][0]
if btn.disabled:
    print("FAIL add disabled on a balanced plan"); raise SystemExit(1)
btn.click().run()
evs = at.session_state["_mw_events_test"]
if len(evs) != 1:
    print("FAIL events %%r" %% evs); raise SystemExit(1)
e = evs[0]
got = (e.type, e.from_tank, [(d.tank, d.count) for d in e.destinations], e.count, e.mode)
want = ("graded_transfer", 3, [(4, 20000.0), (5, None)], None, "grade")
if got != want:
    print("FAIL event %%r" %% (got,)); raise SystemExit(1)

# Partial + split, and a count that overshoots -> error, Add disabled.
at2 = AppTest.from_string(SCRIPT, default_timeout=240); at2.run()
[r_ for r_ in at2.radio if r_.key == "mw_gt_mode_3_1"][0].set_value("Not graded (split by count)").run()
[r_ for r_ in at2.radio if r_.key == "mw_gt_scope_3_1"][0].set_value("Part of the tank").run()
[n for n in at2.number_input if n.key == "mw_gt_out_3_1"][0].set_value(25000.0).run()
[m for m in at2.multiselect if m.key == "mw_gt_dest_3_1"][0].set_value([4, 5]).run()
[n for n in at2.number_input if n.key == "mw_gt_cnt_3_1_4"][0].set_value(30000.0).run()
if not at2.error or not [b for b in at2.button if b.key == "mw_gt_add"][0].disabled:
    print("FAIL overshoot not blocked"); raise SystemExit(1)
[n for n in at2.number_input if n.key == "mw_gt_cnt_3_1_4"][0].set_value(10000.0).run()
df2 = at2.dataframe[0].value
rows2 = df2.to_dict("records")
stay = [x for x in rows2 if "stays, not graded" in x["Tank"]]
if not stay or stay[0]["Fish"] != 35000 or abs(stay[0]["Avg wt (kg)"] - 2.4) > 1e-9:
    print("FAIL partial stay row %%r" %% rows2); raise SystemExit(1)
if any(abs(x["Avg wt (kg)"] - 2.4) > 1e-9 for x in rows2[1:3]):
    print("FAIL split legs not at parent weight %%r" %% rows2); raise SystemExit(1)
print("OK panel")
'''


def test_click_panel_previews_balances_and_adds_the_event():
    src = _DRIVER % {"root": ROOT}
    p = subprocess.run([sys.executable, "-c", src], capture_output=True,
                       text=True, timeout=900, cwd=ROOT)
    tail = [ln for ln in (p.stdout or "").splitlines()
            if ln.startswith(("OK", "FAIL"))]
    msg = tail[-1] if tail else (p.stderr or "")[-1500:]
    assert p.returncode == 0 and msg == "OK panel", msg
