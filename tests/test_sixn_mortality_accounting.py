"""What the 6N off-feed mortality note actually describes, measured.

The app's Known limits page said:

    "6N off-feed mortality is slightly under-counted per tank — a
     few-fish-per-week approximation that nets out facility-wide and is
     deliberately left (correcting it destabilizes the facility-level balance
     it currently cancels against)."

The review brief asked whether that is an intentional approximation, stale
documentation, or two compensating errors. Measured independently on a real
85-week run -- my arithmetic, the engine's reported rows -- it is the first,
described wrongly:

  * `ReconciliationReport.Count_Delta` is 0 on all 572 rows and the sheet is
    INTERNALLY CONSISTENT: recomputing its identity from its own columns
    reproduces it to under one fish (rounding only). Fish are not being lost.
  * The engine applies NO mortality to fish while off feed in 6N. excel_io
    says so: "STARVE (6N production in-place purge) tanks neither grow nor take
    mortality ... exclude them from the growth + mortality expectation (else
    they read as drift)."
  * `WeeklyReport` nevertheless REPORTS mortality on those batch-weeks -- about
    1,000 fish over the horizon -- that the population never lost. So the two
    sheets disagree, by up to 28 fish, on the same (batch, week), and only in
    the Mortality column: Open, Cull, Harvest, Input and Close match exactly.
  * It does NOT "net out facility-wide": non-6N weeks carry +325 and 6N weeks
    -1,147, leaving -822 across 385 of 1,191 ledger rows. Partial offset, not
    cancellation, and the residual is signed.

So: count conservation is REAL, the modelling choice is deliberate (and two
attempts to change it backfired -- off-feed mortality is load-bearing), and the
defect is a REPORTING one plus a wrong description. This test does not change
the model. It pins the boundary, so that if the discrepancy ever escapes 6N or
grows past what the off-feed rule can explain, it fails here instead of being
absorbed into a mortality term.
"""
import collections
import contextlib
import io
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
# A FRESH output, generated here (2026-09-11). This used to read another
# session's saved workbook, so a writer change could never reach it -- after the
# ReconciliationReport's `Input_Count` became `TranOG_In` the identity below
# would have kept passing against the stale file.
PR = Path(r"C:\Users\julian.f\Downloads"
          r"\8 31 2026 AS Monthly Production Report_planned (31).xlsm")

pytestmark = pytest.mark.skipif(not PR.exists(),
                                reason="needs the 2026-08-31 ProductionReport")


@pytest.fixture(scope="module")
def sheets(tmp_path_factory, copy_config):
    import openpyxl
    from forecast.run import main
    tmp = tmp_path_factory.mktemp("sixn_mort")
    cfg, scn = tmp / "config", tmp / "scenario"
    copy_config(ROOT / "config", cfg)
    shutil.copytree(ROOT / "scenario", scn)
    inp, out = tmp / "pr.xlsm", tmp / "out.xlsm"
    shutil.copy(PR, inp)
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(str(inp), str(out), config_dir=str(cfg),
                    scenario_dir=str(scn), calib_log_path="") == 0
    wb = openpyxl.load_workbook(out, read_only=True, data_only=True)

    def load(name, first):
        ws, hdr, out = wb[name], None, {}
        for r in ws.iter_rows(values_only=True):
            if hdr is None:
                if r and str(r[0]).strip() == first:
                    hdr = {str(c).strip(): i for i, c in enumerate(r) if c}
                continue
            if not r or hdr.get("Week") is None or hdr.get("Batch") is None:
                continue
            w, b = r[hdr["Week"]], r[hdr["Batch"]]
            if w and b and str(w).startswith("20"):
                out[(str(b).strip(), str(w).strip())] = (hdr, r)
        return out

    starve = collections.defaultdict(float)
    for r in wb["BatchLocations"].iter_rows(min_row=5, values_only=True):
        if not r or not r[0]:
            continue
        if str(r[9]).strip() == "STARVE":
            starve[(str(r[2]).strip(), str(r[0]).strip())] += float(r[5])
    return (load("ReconciliationReport", "Week"),
            load("WeeklyReport", "Scenario"), starve)


def _g(pair, k):
    hdr, r = pair
    i = hdr.get(k)
    v = r[i] if i is not None and i < len(r) else None
    return float(v) if isinstance(v, (int, float)) else 0.0


def test_count_conservation_is_real_not_a_rounded_headline(sheets):
    """Recompute the reconciliation's identity from its OWN columns. It must
    agree with the Count_Delta it publishes to under one fish -- if a future
    change starts absorbing a real imbalance into the mortality term, the
    published zero and the recomputed identity part company here."""
    rec, _, _ = sheets
    worst = 0.0
    for p in rec.values():
        # The sheet's formula: open - mortality - harvest + TranOG_In. Cull is
        # informational (freshwater, before arrival) and NOT subtracted -- the
        # draft of this test subtracted it and passed only because the cull
        # column is empty on seawater weeks.
        expected = (_g(p, "Open_Count") - _g(p, "Mortality_Count")
                    - _g(p, "Harvest_Count") + _g(p, "TranOG_In"))
        worst = max(worst, abs(expected - _g(p, "Actual_Close")))
        assert abs(_g(p, "Count_Delta")) < 0.5
    assert any(_g(p, "TranOG_In") > 0 for p in rec.values()), (
        "no TranOG_In on the sheet -- renamed header, or no arrivals to check")
    assert worst <= 1.0 + 1e-9, (
        "the published identity and its own columns disagree by %.2f fish — "
        "more than the whole-fish rounding that explains it" % worst)


def test_the_two_sheets_disagree_only_about_mortality(sheets):
    """Harvest and Close must match exactly on every shared (batch, week), and
    WeeklyReport books no Input on a seawater week (only eggs are input).

    Open and Cull match exactly except on a batch's FW->SW week (TranOG_In >
    0), where the seawater-only ReconciliationReport opens WITHOUT the
    arriving fish and lists them as TranOG_In, while WeeklyReport opens ON
    them and shows the move in Xfer_In (2026-09-11). There the two must agree
    through R2 -- both reach the same close:
        Weekly.Open - Weekly.Mort - Weekly.Cull
            == Recon.Open + Recon.TranOG_In - Recon.Mortality   (+/- rounding)
    (Weekly's Cull/Mort there also carry a manual fw_to_og's freshwater cull
    and pre-transfer loss, which Recon never sees.) If a second column starts
    drifting, the off-feed explanation no longer covers it."""
    rec, wkl, _ = sheets
    bad = []
    for k in set(rec) & set(wkl):
        r, w = rec[k], wkl[k]
        for a, b in (("Harvest_Count", "Harv_Count (fish)"),
                     ("Actual_Close", "Close_Count (fish)")):
            if abs(_g(r, a) - _g(w, b)) > 0.5:
                bad.append((k, a))
        if abs(_g(w, "Input_Count (fish)")) > 0.5:
            bad.append((k, "Input on a seawater week"))
        if _g(r, "TranOG_In") > 0:
            lhs = (_g(w, "Open_Count (fish)") - _g(w, "Mort_Count (fish)")
                   - _g(w, "Cull_Count (fish)"))
            rhs = (_g(r, "Open_Count") + _g(r, "TranOG_In")
                   - _g(r, "Mortality_Count"))
            if abs(lhs - rhs) > 3.0:
                bad.append((k, "R2", lhs, rhs))
            if _g(w, "Xfer_In (fish)") + 0.5 < _g(r, "TranOG_In"):
                bad.append((k, "the move is not shown in Xfer_In"))
        else:
            for a, b in (("Open_Count", "Open_Count (fish)"),
                         ("Cull_Count", "Cull_Count (fish)")):
                if abs(_g(r, a) - _g(w, b)) > 0.5:
                    bad.append((k, a))
    assert not bad, "columns beyond Mortality now disagree: %r" % bad[:5]


def test_there_are_exactly_two_effects_and_they_have_different_shapes(sheets):
    """THE BOUNDARY, corrected by measurement. My first draft asserted the
    discrepancy lives only on off-feed weeks. It does not, and the test caught
    me: 77 ordinary grow-out batch-weeks also differ -- always WeeklyReport
    LOWER, never on a harvest week, 314 fish in total, about 4 per row against
    batches of ~300,000. That is rounding scale (0.007%), consistent with
    whole-fish rounding applied per tank per day and then summed, against a
    rate computed once for the batch.

    So there are two effects, and what this pins is their SHAPE, not a story:
    the off-feed one may be large per row, the grow-out one must stay at
    rounding scale. If an ordinary week ever drifts past that, something other
    than rounding is in play and the off-feed explanation no longer covers the
    ledger.

    Two refinements, measured when this test moved from a stale saved workbook
    to a FRESH run of the 2026-08-31 PR (2026-09-11):
      * a batch's FW->SW week is exempt -- WeeklyReport books a manual
        fw_to_og's freshwater loss between the PR close and the transfer there
        (B49 2026-W36: +107), which the seawater-only ReconciliationReport never
        sees; test_the_two_sheets_disagree_only_about_mortality pins that week
        through R2 instead;
      * "never on a harvest week" is false on this PR: B55 2028-W14 harvested
        38,004 of 54,379 and the sheets differ by 19. WeeklyReport charges the
        weekly rate on the OPENING count, the realized biology only on the fish
        left after the harvest -- ~ harvested x rate (38,004 x 27/54,379 = 18.9).
        Byte-identical in V1 (Python-v1 on the same PR), so it predates the
        ledger change; a harvest week is allowed that much and no more."""
    rec, wkl, starve = sheets
    loud = []
    for k in set(rec) & set(wkl):
        if starve.get(k, 0.0) > 0 or _g(rec[k], "TranOG_In") > 0:
            continue
        d = abs(_g(rec[k], "Mortality_Count") - _g(wkl[k], "Mort_Count (fish)"))
        opened = _g(rec[k], "Open_Count")
        harvested = _g(wkl[k], "Harv_Count (fish)")
        rate = (_g(wkl[k], "Mort_Count (fish)") / opened) if opened > 0 else 0.0
        if opened > 0 and d > 0.0002 * opened + harvested * rate + (1.0 if harvested else 0.0):
            loud.append((k, d, opened))
    assert not loud, (
        "mortality differs on a NON-off-feed week by more than rounding can "
        "explain: %r" % loud[:5])


def test_the_size_of_the_approximation_is_bounded(sheets):
    """Quantified, not hand-waved: the fish WeeklyReport reports as dead in 6N
    that the population never lost cannot exceed the off-feed population times
    a plausible weekly rate. ~1,000 fish over 85 weeks against 3.4M harvested.
    A regression that made it materially larger fails here."""
    rec, wkl, starve = sheets
    diff = sum(abs(_g(wkl[k], "Mort_Count (fish)") - _g(rec[k], "Mortality_Count"))
               for k in set(rec) & set(wkl) if starve.get(k, 0.0) > 0)
    fish_weeks = sum(v for v in starve.values())
    assert fish_weeks > 0
    assert diff < 0.005 * fish_weeks, (
        "the 6N mortality discrepancy is %.0f fish against %.0f off-feed "
        "fish-weeks (%.4f%%) — larger than the off-feed rule can explain"
        % (diff, fish_weeks, diff / fish_weeks * 100))
