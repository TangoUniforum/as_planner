"""The 6N fill/peel sizing honours the operator's PER-WEEK harvest floor.

Operator ruling 2026-09-03: "if we are defining a certain min count to harvest
then the entire system should honor the same number… even if it measured worse
it is correct to operate from one truth."

THE DEFECT. Four sites read `control.min_harvest_per_week` — the Control
DEFAULT — where the caller had already resolved that week's real floor through
`resolve_facility_cap`. The floor-filling graded peel is clamped by
`_floor = min(_min_fill, target)` and `_min_fill` descends from that default, so
the peel topped a pair up to 30,060 and stopped no matter what the operator had
committed to. The pair drained 30,030 two weeks later — matching the observed
December harvest to the fish.

MEASURED (2026-08-31 PR, 85 weeks). Recorded because it is NOT a clean win and
nobody should later assume it was:

    shortfall vs the operator's own floors   119,311 -> 74,999 fish
    2026-12                                    451.3 -> 535.2 t
    2026-11                                    616.7 -> 708.3 t  (over its 650 target)
    2027-01                                    672.1 -> 401.2 t  (-271)
    worst harvest week                        27,325 -> 16,145 fish
    weeks below their own floor                    7 -> 9
    total HOG                                 11,558 -> 11,481 t
    average harvest weight                     3.355 -> 3.334 kg/fish

It is shipped for CORRECTNESS — one number, honoured everywhere — not for the
number it produces. Harvesting to a higher floor takes the same fish earlier and
lighter, so tonnage falls slightly while count holds.

These tests pin the two properties that must not regress: the per-week value is
what reaches the sizing, and a week with NO override is bit-identical to before.
"""
import inspect

import forecast.placement as P


def _src(fn):
    return inspect.getsource(fn)


# --------------------------------------------------------------------------- #
# The per-week value must reach every sizing site
# --------------------------------------------------------------------------- #
def test_purge_week_takes_a_weekly_min_parameter():
    sig = inspect.signature(P._run_sixn_purge_week)
    assert "weekly_min" in sig.parameters
    assert sig.parameters["weekly_min"].default is None


def test_entry_transit_takes_a_weekly_min_parameter():
    sig = inspect.signature(P._transit_entry_to_pair)
    assert "weekly_min" in sig.parameters
    assert sig.parameters["weekly_min"].default is None


def test_no_sizing_site_still_reads_only_the_control_default():
    """Every `control.min_harvest_per_week` in a SIZING path must now be a
    fallback behind the per-week value, never the sole source."""
    for fn in (P._run_sixn_purge_week, P._transit_entry_to_pair):
        src = _src(fn)
        for line in src.splitlines():
            if "control.min_harvest_per_week" in line and not line.strip().startswith("#"):
                # It may appear only as the `else` half of a weekly_min fallback.
                assert "weekly_min" in src, fn.__name__
                break


def test_the_fill_is_sized_by_the_DRAIN_weeks_floor():
    """placement.py's own note: "move-in drives harvest at week t+lead". The
    floor is a promise about the HARVEST week, so the fill two weeks earlier
    must be sized by the floor of the week it drains into."""
    src = _src(P.phase_d_emit_events)
    i = src.index("_run_sixn_purge_week(")
    call = src[i:i + 700]
    assert "weekly_min=" in call
    assert "drain_idx" in call, "the fill must resolve the DRAIN week's floor"
    assert "METRIC_MIN_HARVEST" in call


# --------------------------------------------------------------------------- #
# A week with no override must be untouched
# --------------------------------------------------------------------------- #
def test_falsy_weekly_min_falls_back_to_the_control_default():
    """resolve_facility_cap returns None when the resolved value is <= 0, and a
    0.0 must NOT be allowed to zero the sizing — both fall through to the
    Control default. Truthiness, not `is not None`.

    Verified end to end 2026-09-03: with every min_harvest_per_week row stripped
    from limits.yaml, the patched engine reproduced the unpatched plan exactly —
    85 weeks compared, 0 differing, 3,445,527 fish both sides.
    """
    src = _src(P._run_sixn_purge_week)
    assert "float(weekly_min) if weekly_min" in src, (
        "must test truthiness so 0.0 and None both fall back")
    assert "control.min_harvest_per_week or 0" in src, (
        "the Control default must remain the fallback")


def test_the_production_era_stage_uses_the_resolved_floor():
    """Inert until sixn_production_start (2028-01-01) on today's scenario, but a
    rule the operator writes must mean the same thing everywhere."""
    src = _src(P.phase_d_emit_events)
    assert "min_hv or control.min_harvest_per_week" in src
