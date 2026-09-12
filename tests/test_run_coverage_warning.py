"""The Run page says when weeks plan on a Control default (2026-09-12).

WHY. `scenario/limits.yaml`'s per-week rows ended at 2026-W53, so every later
week of an 85-week horizon (all of 2027) planned on the Control default — feed
34,000 kg/day against the 27,500 entered, a harvest floor of 30,000 against
50,000-53,000. The engine already DETECTS it (`caps.coverage_gap_notes` ->
`run.py` prints `PER-WEEK COVERAGE - <metric>: ...` -> `excel_io` files it in
the ValidationLog as `INFO - Per-week coverage (weeks on a Control default)`),
but only as an INFO row in a workbook sheet: nothing on screen said it.

WHAT. `app._coverage_warning_lines(advisory_entries)` turns the ValidationLog
rows the results parser already read (`_parse_output_workbook` ->
`advisory_entries`) into one warning — a headline, one bullet per metric with
the line's own detail, and where to fix it — and the Run results view draws it
directly above the result tabs. Display only: it reads the run's own lines and
recomputes nothing, so no run and no workbook changes.

HOW IT IS TESTED.
  * the helper, lifted out of app.py by name (no Streamlit run): no entries,
    other categories only, one and two metrics, a gap with no "after" part,
    the check-failed line (verbatim), and junk rows from an older cache;
  * the REAL Run page, rendered by AppTest from a seeded result (the way a
    fresh run, a cache replay or a board pick hands it one): the warning is
    drawn, directly above the tabs, for a result carrying coverage lines, and
    nothing is drawn — and nothing raises — without them (incl. a result from
    before `advisory_entries` existed). A subprocess, for the reason given in
    tests/test_app_renders.py.
"""
import ast
import json
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")

CAT = "INFO - Per-week coverage (weeks on a Control default)"
TAIL = (" An absent row means \"use the default\", so this matters only if "
        "that default is not what you intend for those weeks - check it "
        "rather than assume it.")
BIOMASS = ("PER-WEEK COVERAGE - biomass: rows cover 2026-W36..2026-W53 (18 of "
           "85 horizon week(s)); 67 after 2026-W53 take the Control default "
           "3,800,000." + TAIL)
FEED = ("PER-WEEK COVERAGE - feed_per_day: rows cover 2026-W36..2026-W53 (18 "
        "of 85 horizon week(s)); 67 after 2026-W53 take the Control default "
        "34,000." + TAIL)
FAILED = "PER-WEEK COVERAGE - check failed: KeyError('biomass')"


def _entry(detail, cat=CAT, n=1):
    return {"#": n, "Category": cat, "Detail": detail}


def _helper():
    """`_coverage_warning_lines` and the `_COVERAGE_*` names it reads, lifted
    out of app.py (importing app would run the whole page)."""
    tree = ast.parse(open(APP, encoding="utf-8").read())
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef)
                and n.name == "_coverage_warning_lines")
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", "").startswith("_COVERAGE_")
                        for t in n.targets))]
    assert any(isinstance(n, ast.FunctionDef) for n in body), (
        "app.py has no _coverage_warning_lines")
    ns = {"re": re}
    exec(compile(ast.Module(body=body, type_ignores=[]), APP, "exec"), ns)
    return ns["_coverage_warning_lines"]


# --------------------------------------------------------------------------- #
# The helper
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("entries", [None, [], ()])
def test_no_entries_say_nothing(entries):
    assert _helper()(entries) == []


def test_other_categories_only_say_nothing():
    entries = [_entry("PER-WEEK COVERAGE - biomass: looks like one",
                      cat="WARNING - Hydration"),
               _entry("FW calibration applied",
                      cat="INFO - FW growth calibration (fw_correction "
                          "rewritten)", n=2)]
    assert _helper()(entries) == []


def test_two_metrics_one_headline_a_bullet_each_and_the_fix():
    lines = _helper()([_entry("something else", cat="WARNING - Hydration"),
                       _entry(BIOMASS, n=2), _entry(FEED, n=3)])
    head, *rest = lines
    assert head.startswith("⚠ **2 metrics run past your dated per-week "
                           "limits**"), head
    assert "Control default after 2026-W53" in head, head
    bullets = [x for x in rest if x.startswith("- ")]
    # One bullet per metric, in the log's order, carrying the line's OWN
    # detail (its first sentence) — nothing recomputed.
    assert bullets == [
        "- `biomass`: rows cover 2026-W36..2026-W53 (18 of 85 horizon "
        "week(s)); 67 after 2026-W53 take the Control default 3,800,000.",
        "- `feed_per_day`: rows cover 2026-W36..2026-W53 (18 of 85 horizon "
        "week(s)); 67 after 2026-W53 take the Control default 34,000."]
    fix = [x for x in rest if not x.startswith("- ")]
    assert len(fix) == 1, fix
    assert "add per-week rows in **Configure → Limits**" in fix[0]
    assert "(or change the Control default)" in fix[0]
    # The engine's own reading note, quoted — not softened into "nothing
    # needs changing" (review 2026-09-12, MINOR 4).
    assert TAIL.strip() in fix[0], fix[0]
    assert "check it rather than assume it" in fix[0], fix[0]
    assert "nothing needs changing" not in fix[0], fix[0]
    assert lines.index(fix[0]) == len(lines) - 1          # the fix comes last


def test_one_metric_is_singular():
    # The WHOLE headline: "1 metric runs ... and uses" (review 2026-09-12,
    # MINOR 1 — a startswith check let "and use" through).
    head = _helper()([_entry(BIOMASS)])[0]
    assert head == ("⚠ **1 metric runs past your dated per-week limits** and "
                    "uses the Control default after 2026-W53."), head


# --------------------------------------------------------------------------- #
# The helper on the ENGINE'S OWN lines (caps.coverage_gap_notes), not on
# strings copied from it by hand: if the engine's wording drifts, the headline
# must not quietly change shape (review 2026-09-12, MINOR 3).
# --------------------------------------------------------------------------- #
def _engine_lines(overrides):
    """ValidationLog entries built from `caps.coverage_gap_notes` itself, on
    the 8/31 PR's horizon shape (2026-W38 .. 2027-W52)."""
    from types import SimpleNamespace
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from forecast import caps
    labels = ([f"2026-W{w:02d}" for w in range(38, 54)]
              + [f"2027-W{w:02d}" for w in range(1, 53)])
    control = SimpleNamespace(max_biomass_kg=3_800_000.0,
                              max_feed_per_day_kg=33_000.0,
                              max_harvest_per_week=55_000.0,
                              min_harvest_per_week=26_000.0,
                              default_hog_yield=0.81)
    ov = {}
    for metric, weeks, v in overrides:
        metric = getattr(caps, metric)
        ov.update({(labels[i], metric): v for i in weeks})
    notes = caps.coverage_gap_notes(SimpleNamespace(overrides=ov), control,
                                    labels)
    assert notes, "the engine found no coverage gap in a gapped fixture"
    return notes, [_entry(d, n=i + 1) for i, d in enumerate(notes)]


def test_the_engines_own_trailing_lines_give_the_run_past_headline():
    # The live shape: biomass and feed rows stop at 2026-W53.
    notes, entries = _engine_lines([("METRIC_BIOMASS", range(16), 3_650_000),
                                    ("METRIC_FEED_DAY", range(16), 27_500)])
    lines = _helper()(entries)
    assert lines[0] == ("⚠ **2 metrics run past your dated per-week limits** "
                        "and use the Control default after 2026-W53."), lines[0]
    bullets = [x for x in lines if x.startswith("- ")]
    assert len(bullets) == len(notes) == 2, lines
    for b, d in zip(bullets, notes):
        # The bullet is the note's own first sentence, word for word.
        assert b.split(": ", 1)[1] in d, (b, d)
        assert "after 2026-W53 take the Control default" in b, b
    tail = notes[0][notes[0].index(" An absent row means"):].strip()
    assert tail in lines[-1], lines[-1]


def test_the_engines_before_and_after_line_says_there_is_more_below():
    # Rows cover 2026-W42..2027-W14: 4 weeks before AND 38 after. The headline
    # names the trailing gap, and must say the bullet holds more
    # (review 2026-09-12, MINOR 2).
    notes, entries = _engine_lines([("METRIC_FEED_DAY", range(4, 30),
                                     27_500)])
    assert " before " in notes[0] and " after " in notes[0], notes[0]
    head = _helper()(entries)[0]
    assert head == ("⚠ **1 metric runs past your dated per-week limits** and "
                    "uses the Control default after 2027-W14 (plus other weeks "
                    "your rows do not cover — see below)."), head


def test_the_engines_after_and_interior_line_says_there_is_more_below():
    notes, entries = _engine_lines([("METRIC_BIOMASS",
                                     list(range(10)) + list(range(12, 30)),
                                     3_650_000)])
    assert "inside the covered span" in notes[0], notes[0]
    head = _helper()(entries)[0]
    assert head.endswith("(plus other weeks your rows do not cover — see "
                         "below)."), head


def test_a_trailing_only_line_does_not_say_there_is_more():
    _notes, entries = _engine_lines([("METRIC_MIN_HARVEST", range(16),
                                      50_000)])
    head = _helper()(entries)[0]
    assert "plus other" not in head, head


# --------------------------------------------------------------------------- #
# The guide describes the warning as the helper writes it (both headlines).
# --------------------------------------------------------------------------- #
def test_the_user_guide_describes_both_headlines_and_the_page():
    guide = " ".join(open(os.path.join(ROOT, "docs", "USER_GUIDE.md"),
                          encoding="utf-8").read().split())
    f = _helper()
    run_past = f([_entry(BIOMASS), _entry(FEED, n=2)])[0]
    not_cover = f([_entry(
        "PER-WEEK COVERAGE - min_harvest_per_week: rows cover "
        "2026-W40..2027-W49 (62 of 66 horizon week(s)); 4 before 2026-W40 "
        "take the Control default 30,000." + TAIL),
        _entry(FEED, n=2)])[0]
    more = f([_entry(BIOMASS.replace("67 after", "4 before 2026-W36 and 63 "
                                     "after"))])[0]
    for head in (run_past, not_cover):
        # "⚠ **2 metrics run past ...**" -> "metrics run past ..."
        phrase = head.split("**")[1].split(" ", 1)[1]
        assert phrase in guide, (phrase, "is not in docs/USER_GUIDE.md")
    clause = more[more.index("(plus"):].rstrip(".")
    assert clause in guide, (clause, "is not in docs/USER_GUIDE.md")
    assert "last week your rows cover" in guide
    assert "Run forecast" in guide


def test_different_last_rows_name_the_earliest():
    early = BIOMASS.replace("2026-W53", "2026-W40").replace("67 after",
                                                            "80 after")
    head = _helper()([_entry(early), _entry(FEED, n=2)])[0]
    assert "after 2026-W40 at the earliest" in head, head


def test_a_gap_with_no_after_part_does_not_claim_the_rows_run_out():
    # Rows that START mid-horizon: weeks before the first row, none after.
    before = ("PER-WEEK COVERAGE - min_harvest_per_week: rows cover "
              "2026-W40..2027-W49 (62 of 66 horizon week(s)); 4 before "
              "2026-W40 take the Control default 30,000." + TAIL)
    lines = _helper()([_entry(before)])
    assert "run past" not in lines[0] and "runs past" not in lines[0], lines[0]
    assert lines[0].startswith("⚠ **1 metric has weeks your dated per-week "
                               "rows do not cover**"), lines[0]
    assert lines[1] == ("- `min_harvest_per_week`: rows cover "
                        "2026-W40..2027-W49 (62 of 66 horizon week(s)); 4 "
                        "before 2026-W40 take the Control default 30,000.")


def test_a_failed_check_is_shown_verbatim():
    lines = _helper()([_entry(FAILED)])
    assert len(lines) == 1, lines
    assert FAILED in lines[0], lines[0]
    assert "run past" not in lines[0]


def test_a_failed_check_next_to_metric_lines_is_shown_too():
    lines = _helper()([_entry(BIOMASS), _entry(FAILED, n=2)])
    assert lines[0].startswith("⚠ **1 metric runs past"), lines[0]
    assert sum(FAILED in x for x in lines) == 1, lines


def test_junk_rows_from_an_older_cache_never_crash():
    entries = ["not a dict", None, {}, {"Category": None, "Detail": None},
               {"Category": CAT}, {"Category": CAT, "Detail": ""}]
    assert _helper()(entries) == []


# --------------------------------------------------------------------------- #
# The real Run page
# --------------------------------------------------------------------------- #
_DRIVER = r'''
import json, os, sys
P = json.load(open(sys.argv[1], encoding="utf-8"))
sys.path.insert(0, P["root"])
try:
    from streamlit.testing.v1 import AppTest
except Exception as e:
    print("SKIP no AppTest: %s" % e)
    raise SystemExit(0)

def fail(msg):
    print(ascii("FAIL " + msg)[1:-1])
    raise SystemExit(1)

BASE = {"ok": True, "violations": 0, "worst_density": 0.0, "harvest_kg": 0.0,
        "elapsed": 1.0, "output_bytes": b"x", "output_name": "x.xlsm",
        "batch_locations": [], "harvest_events": [], "biology_projection": [],
        "advisory_summary": [], "facility_weekly": [], "yearly": [],
        "plan_summary": [], "flow_template": [], "control_status": {},
        "stdout": ""}

def page(case, entries):
    r = dict(BASE, _rid="cov_" + case)
    if entries is not None:
        r["advisory_entries"] = entries
    at = AppTest.from_file(os.path.join(P["root"], "app.py"),
                           default_timeout=240)
    at.session_state["result"] = r
    at.run()
    if at.exception:
        fail(case + ": " + "; ".join(str(e)[:300] for e in at.exception))
    if not at.tabs:
        fail(case + ": the Run results view did not draw its tabs")
    return at

def coverage(at):
    return [w for w in at.warning if "per-week" in w.value]

def top_index(at, node):
    """Index of the main-area child that is, or holds, `node`."""
    kids = at.main.children
    for k in sorted(kids):
        stack = [kids[k]]
        while stack:
            n = stack.pop()
            if n is node:
                return k
            ch = getattr(n, "children", None)
            if isinstance(ch, dict):
                stack.extend(ch.values())
    return None

at = page("two", P["two"])
ws = coverage(at)
if len(ws) != 1:
    fail("two metrics: want ONE coverage warning, got %r"
         % [w.value[:120] for w in ws])
v = ws[0].value
for want in P["want"]:
    if want not in v:
        fail("two metrics: %r is not in the warning: %r" % (want, v[:400]))
wi, ti = top_index(at, ws[0]), top_index(at, at.tabs[0])
if wi is None or ti is None or ti != wi + 1:
    fail("the warning is not directly above the tabs (warning at %r, tabs "
         "at %r)" % (wi, ti))

at = page("failed", P["failed"])
ws = coverage(at)
if len(ws) != 1 or P["failed_line"] not in ws[0].value:
    fail("a failed check is not shown verbatim: %r"
         % [w.value[:200] for w in ws])

for case, entries in (("older cache", None), ("clean", []),
                      ("other categories", P["other"])):
    at = page(case, entries)
    if coverage(at):
        fail(case + ": a coverage warning without a coverage line: %r"
             % [w.value[:200] for w in coverage(at)])
print("OK drawn above the tabs with coverage lines; none without")
'''


pytestmark_page = pytest.mark.skipif(
    not (os.path.exists(APP)
         and os.path.exists(os.path.join(ROOT, "config", "control.yaml"))
         and os.path.exists(os.path.join(ROOT, "scenario", "limits.yaml"))),
    reason="needs app.py + a seeded config/scenario")


@pytestmark_page
def test_the_run_results_view_draws_the_warning_above_the_tabs(tmp_path):
    params = {
        "root": ROOT,
        "two": [_entry("something else", cat="WARNING - Hydration"),
                _entry(BIOMASS, n=2), _entry(FEED, n=3)],
        "failed": [_entry(FAILED)],
        "failed_line": FAILED,
        "other": [_entry("PER-WEEK COVERAGE - biomass: a lookalike",
                         cat="WARNING - Hydration")],
        "want": ["2 metrics run past your dated per-week limits",
                 "Control default after 2026-W53", "`biomass`",
                 "`feed_per_day`", "3,800,000", "34,000",
                 "Configure → Limits"],
    }
    pfile = tmp_path / "params.json"
    pfile.write_text(json.dumps(params), encoding="utf-8")
    p = subprocess.run([sys.executable, "-c", _DRIVER, str(pfile)],
                       capture_output=True, text=True, timeout=900, cwd=ROOT)
    tail = [ln for ln in (p.stdout or "").splitlines()
            if ln.startswith(("OK", "FAIL", "SKIP"))]
    msg = tail[-1] if tail else (p.stderr or "")[-800:]
    if msg.startswith("SKIP"):
        pytest.skip(msg)
    assert p.returncode == 0, msg
    assert msg.startswith("OK"), msg
