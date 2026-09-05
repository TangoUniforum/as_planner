"""Re-running the same solve is not new evidence about the fish.

DEFECT (2026-09-04). `calibration_drift` counts RECORDS: `"runs": len(recs)`,
and `persistent` is `len(recs) >= min_runs and |gap| >= threshold`. Every run
with FW auto-calibration on appends one record per FW batch -- and a knob
search or a tuned tournament runs the engine hundreds of times on ONE PR, each
leg re-solving the same fixed target and logging the same answer again.

Measured on the real log (2026-09-04): 121,307 records, 52 batches, but only
**516** distinct (batch, PR closing, answer) triples -- a 235x inflation. B40
holds 3,302 records across 15 PR closings and 9 distinct answers; B56 holds
3,269 records and ONE answer, logged 3,269 times. So the "Runs" column counts
how often you pressed Run, and "persistent — a standing model error seen across
several runs" is guaranteed for any batch with a gap, whatever the fish did.

The unit of independent evidence is the PR CLOSING: one month, one observation
of the facility. Two solves against the same PR are the same measurement twice,
however far apart they were run.

This also fixes a weighting bug the same line causes: `median_applied` and
`spread` are averaged over records, so a batch re-solved 3,000 times in one
afternoon has that afternoon dominate its own median.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forecast.accuracy import calibration_drift  # noqa: E402


def _rec(batch, pr, applied, configured=1.0, ts="2026-01-01"):
    return {"batch": batch, "pr_closing": pr, "applied": applied,
            "configured": configured, "ts": ts, "converged": True,
            "clamped": False}


def _one(recs, batch="B40"):
    return next(d for d in calibration_drift(recs) if d["batch"] == batch)


def test_runs_counts_months_not_button_presses():
    """THE DEFECT: 10 solves of the same two PRs is two observations."""
    recs = ([_rec("B40", "2026-07-31", 0.84) for _ in range(5)]
            + [_rec("B40", "2026-08-31", 0.84) for _ in range(5)])
    assert _one(recs)["runs"] == 2, (
        "the drift view reports how many times the engine ran, not how many "
        "independent observations of the batch there were")


def test_persistence_needs_independent_observations():
    """A batch solved 50 times against ONE PR has been seen once."""
    recs = [_rec("B40", "2026-08-31", 0.60) for _ in range(50)]
    assert not _one(recs)["persistent"], (
        "one PR re-solved 50 times was reported as a standing model error "
        "across several runs")


def test_a_real_standing_error_is_still_flagged():
    """NEGATIVE CONTROL. The feature must keep working: the same correction
    needed on three different months IS the finding it was built for."""
    recs = [_rec("B40", pr, 0.60)
            for pr in ("2026-06-30", "2026-07-31", "2026-08-31")]
    d = _one(recs)
    assert d["runs"] == 3 and d["persistent"], d


def test_one_afternoon_cannot_dominate_the_median():
    """300 identical solves on one PR against one solve on another must not
    drag the median onto the busy day."""
    recs = ([_rec("B40", "2026-07-31", 1.00) for _ in range(300)]
            + [_rec("B40", "2026-08-31", 0.50)])
    d = _one(recs)
    assert d["median_applied"] == pytest.approx(0.75), (
        "the median is weighted by how often you pressed Run: %r"
        % d["median_applied"])


def test_the_latest_answer_for_a_PR_wins():
    """Within one PR the last solve is the current one — an earlier leg of a
    knob search is not a competing observation."""
    recs = [_rec("B40", "2026-08-31", 0.90, ts="2026-08-31T09:00"),
            _rec("B40", "2026-08-31", 0.70, ts="2026-08-31T10:00")]
    d = _one(recs)
    assert d["runs"] == 1
    assert d["median_applied"] == pytest.approx(0.70)


def test_records_without_a_PR_are_not_silently_dropped():
    """Older records predate the pr_closing field. Losing them would quietly
    shrink the history rather than report it."""
    recs = [_rec("B40", None, 0.80), _rec("B40", None, 0.80)]
    d = _one(recs)
    assert d["runs"] >= 1, "records with no PR stamp vanished from the view"
