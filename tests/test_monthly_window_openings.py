"""A month must never close at a NEGATIVE fish count.

write_monthly_report took neither `window_openings` nor `tranog_events`. The
second was deliberate and documented; the first was an oversight, and the two
turned out to be a matched pair.

Without openings, a batch whose scripted harvest lands in a manual override
window opens the month at 0 and the harvest is subtracted from nothing. On the
2026-08-31 PR, B41 opened 2026-W36 at 5,322 in WeeklyReport and at 0 here, so
2026-08 closed at -4,562 fish / -17,309 kg and 2026-09 opened there.

Supplying openings alone then breaks the other half: a split FW/SW batch opens
at its true SEAWATER state (B49: 47,743, not 287,599), so its freshwater
arrival is no longer already counted and, uncredited, 250,225 fish vanish.
Both together is strictly better than either.
"""
from __future__ import annotations

import inspect

from forecast import excel_io, run as run_mod


def test_monthly_report_accepts_window_openings():
    sig = inspect.signature(excel_io.write_monthly_report)
    assert "window_openings" in sig.parameters


def test_monthly_report_forwards_openings_to_the_ledger():
    src = inspect.getsource(excel_io.write_monthly_report)
    assert "_build_batch_week_ledger" in src
    assert "window_openings=window_openings" in src


def test_run_passes_both_openings_and_tranog_to_monthly():
    """The matched pair. Passing one without the other is the bug either way:
    openings alone lose a split batch's arrival, arrivals alone double-book it.
    """
    src = inspect.getsource(run_mod)
    i = src.index("write_monthly_report(")
    call = src[i:i + 3000]
    end = call.index("pr_period=")
    call = call[:end]
    assert "window_openings=prefix_openings" in call
    assert "tranog_events=placement.tranog_events" in call


def test_the_weekly_ledger_still_gets_openings_too():
    src = inspect.getsource(run_mod)
    i = src.index("write_weekly_report(")
    assert "window_openings=prefix_openings" in src[i:i + 2000]
