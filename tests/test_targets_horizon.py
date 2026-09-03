"""The targets gate must judge only the periods the plan actually COVERS.

Two defects, both measured 2026-09-02 on the live config and the 2026-07-31 run:

1. NO LOWER CLIP. `in_horizon` tested only `period <= horizon_end`, so a target
   for a month BEFORE the run started was graded 0-vs-target and counted as a
   miss. Targets accumulate in config/targets.yaml as the horizon rolls forward
   each cycle, so this fires on its own. One stale month turned a real 237 t
   shortfall into 837 t and dropped worst_pct from 79% to 0% — the number the
   gate reports and the tournament grades on.

2. HORIZON INFERRED FROM HARVEST. The bounds came from the months that recorded
   harvest, so a blackout week made its month look partly-covered and the month
   went unjudged. On the 2026-07-31 run the plan spans 60 weeks and harvests in
   58; inferring from harvest drops all of 2026-08 from judgement — the gate
   falling silent on the month a blackout just damaged.

A blackout month INSIDE the horizon must still read MISSED. That is a real
failure and the reason the gate exists; these tests pin that too.
"""
import datetime

from forecast import analysis as A


def _targets(monthly=None, tol=5.0):
    return {"basis": "hog", "tolerance_pct": tol,
            "monthly": monthly or {}, "yearly": {}}


def _weeks(start_monday, n):
    """n consecutive ISO week labels starting at a given Monday."""
    d = start_monday
    out = []
    for _ in range(n):
        iy, iw, _x = d.isocalendar()
        out.append("%04d-W%02d" % (iy, iw))
        d += datetime.timedelta(days=7)
    return out


# --------------------------------------------------------------------------- #
# full_periods — what the horizon really covers
# --------------------------------------------------------------------------- #
def test_full_periods_excludes_partly_covered_months():
    # 2026-08-03 is the first Monday of August; five Mondays fall in August
    # 2026 (3, 10, 17, 24, 31), so August needs all five to count as covered.
    full_m, _ = A.full_periods(_weeks(datetime.date(2026, 8, 3), 5))
    assert full_m == {"2026-08"}

    partial, _ = A.full_periods(_weeks(datetime.date(2026, 8, 3), 4))
    assert partial == set()          # one week short -> not judgeable


def test_full_periods_needs_a_whole_calendar_year():
    # 14 months of weeks still contains no complete calendar year.
    _m, full_y = A.full_periods(_weeks(datetime.date(2026, 8, 3), 60))
    assert full_y == set()


def test_full_periods_empty_input_is_empty_not_everything():
    assert A.full_periods(None) == (set(), set())
    assert A.full_periods([]) == (set(), set())


# --------------------------------------------------------------------------- #
# The stale-target defect
# --------------------------------------------------------------------------- #
def test_stale_target_before_the_horizon_is_not_a_miss():
    weeks = _weeks(datetime.date(2026, 8, 3), 10)
    monthly = {"2026-08": 500_000.0, "2026-09": 500_000.0}
    tr = A.review_targets(
        monthly, {}, _targets({"2026-01": 600_000.0, "2026-08": 500_000.0}),
        horizon_weeks=weeks)
    by = {r["period"]: r for r in tr["rows"]}
    assert by["2026-01"]["status"] == "N/A"
    assert by["2026-01"]["note"] == "before horizon"
    assert by["2026-08"]["status"] == "MET"
    # The stale month contributes NOTHING to the headline numbers.
    assert tr["total_shortfall_kg"] == 0.0
    assert tr["judged"] == 1
    assert tr["skipped"]["before horizon"] == 1


def test_stale_target_is_reported_not_silently_dropped():
    """N/A must be visible. A target that never gets graded should be noticed
    and cleaned up, not quietly ignored every cycle."""
    weeks = _weeks(datetime.date(2026, 8, 3), 10)
    tr = A.review_targets({"2026-08": 1.0}, {},
                          _targets({"2025-03": 1.0, "2030-01": 1.0}),
                          horizon_weeks=weeks)
    assert tr["skipped"] == {"before horizon": 1, "beyond horizon": 1}


def test_beyond_horizon_still_na():
    weeks = _weeks(datetime.date(2026, 8, 3), 10)
    tr = A.review_targets({"2026-08": 500_000.0}, {},
                          _targets({"2027-06": 600_000.0}),
                          horizon_weeks=weeks)
    r = tr["rows"][0]
    assert r["status"] == "N/A" and r["note"] == "beyond horizon"


# --------------------------------------------------------------------------- #
# The gate must NOT go quiet on real failures
# --------------------------------------------------------------------------- #
def test_blackout_month_inside_the_horizon_is_still_missed():
    """The whole point of the gate. A covered month that harvested nothing is
    a real failure, not an excuse for N/A."""
    weeks = _weeks(datetime.date(2026, 8, 3), 14)     # Aug .. Oct+
    monthly = {"2026-08": 500_000.0, "2026-10": 500_000.0}   # September blank
    tr = A.review_targets(monthly, {}, _targets({"2026-09": 500_000.0}),
                          horizon_weeks=weeks)
    r = tr["rows"][0]
    assert r["status"] == "MISSED"
    assert r["actual_kg"] == 0.0
    assert tr["total_shortfall_kg"] == 500_000.0


def test_harvest_inferred_horizon_would_have_hidden_that_month():
    """Why `horizon_weeks` exists rather than inferring from harvest.

    Same plan, same target. Told the real horizon the gate reports MISSED;
    left to infer from the weeks that harvested, it never judges the month.
    """
    plan = _weeks(datetime.date(2026, 8, 3), 14)
    # Harvest starts a month late, so an inferred horizon begins in September.
    monthly = {"2026-09": 400_000.0, "2026-10": 500_000.0}
    tgt = _targets({"2026-08": 500_000.0})

    told = A.review_targets(monthly, {}, tgt, horizon_weeks=plan)
    assert told["rows"][0]["status"] == "MISSED"

    inferred = A.review_targets(monthly, {}, tgt)      # no horizon supplied
    assert inferred["rows"][0]["status"] == "N/A"


# --------------------------------------------------------------------------- #
# Backward compatibility — the fallback path
# --------------------------------------------------------------------------- #
def test_without_horizon_the_lower_clip_still_applies():
    """Callers that cannot supply a horizon still get the stale-target fix."""
    tr = A.review_targets({"2026-08": 500_000.0}, {},
                          _targets({"2026-01": 600_000.0}))
    assert tr["rows"][0]["status"] == "N/A"
    assert tr["total_shortfall_kg"] == 0.0
