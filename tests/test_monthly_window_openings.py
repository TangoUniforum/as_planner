"""A month must never close at a NEGATIVE fish count -- and both ledgers make
the same choices at the same scope.

write_monthly_report once took neither `window_openings` nor `tranog_events`.
Without openings, a batch whose scripted harvest lands in a manual override
window opens the month at 0 and the harvest is subtracted from nothing. On the
2026-08-31 PR, B41 opened 2026-W36 at 5,322 in WeeklyReport and at 0 here, so
2026-08 closed at -4,562 fish / -17,309 kg and 2026-09 opened there.

INPUT = EGGS ONLY (operator, 2026-09-11). The ledger now opens on every fish
the PR holds, the freshwater part of a split batch included (`fw_openings`:
B49 opens at 47,743 SW + 250,225 FW = 297,968). The TranOG is a move
(Xfer_In/Xfer_Out; `tranog_events` still finds the FW->SW week), and the
freshwater cull of a manual fw_to_og removes fish that WERE in the opening, so
`window_culls` is now passed to BOTH ledgers -- the reverse of the old
decision, which existed only because the opening held seawater alone and the
TranOG was credited net of the cull.
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


def _monthly_call() -> str:
    src = inspect.getsource(run_mod)
    i = src.index("write_monthly_report(")
    call = src[i:i + 4000]
    return call[:call.index("pr_period=")]


def _weekly_call() -> str:
    src = inspect.getsource(run_mod)
    i = src.index("write_weekly_report(")
    call = src[i:i + 4000]
    return call[:call.index("sixn_move_in_feed=")]


def test_run_passes_both_openings_and_tranog_to_monthly():
    """Openings stop a scripted harvest coming out of nothing; tranog_events
    locate each batch's FW->SW week (shown as a move, never as input)."""
    call = _monthly_call()
    assert "window_openings=prefix_openings" in call
    assert "tranog_events=placement.tranog_events" in call


def test_run_passes_window_culls_to_BOTH_ledgers():
    """INVERTED 2026-09-11. The opening now holds the freshwater fish (held
    PR FW part), so a manual fw_to_og's freshwater cull removes fish that were
    in it: booked once, on the transfer week, in both ledgers. Omitting it
    would leave it in Count_Check."""
    # The ARGUMENT, not the word -- prose at the call site mentions it too.
    assert "window_culls=prefix_fw_cull" in _monthly_call()
    assert "window_culls=prefix_fw_cull" in _weekly_call()


def test_run_passes_the_held_freshwater_part_to_BOTH_ledgers():
    """Same scope in both: the opening holds every fish the PR holds, and a
    manual transfer's pre-transfer FW loss is booked from the window's own
    fw_count_at_transfer."""
    for call in (_monthly_call(), _weekly_call()):
        assert "fw_openings=fw_openings" in call
        assert "fw_transfer_basis=manual_fw_balance" in call


def test_the_weekly_ledger_still_gets_openings_too():
    assert "window_openings=prefix_openings" in _weekly_call()


def test_run_passes_the_projectors_own_list_to_BOTH_ledgers():
    """The ledger's own guard against holding a batch a projection carries
    (fw_projected) must be fed the projector's list at both call sites. The
    held set is already built from the same list (held_fw_openings), so
    dropping this argument changes no output today -- no run can see it
    (independent mutation proof, 2026-09-11: deleting both lines left every
    test green). It is the second line of defence against the B50 double
    count, so the call is pinned here."""
    for call in (_monthly_call(), _weekly_call()):
        assert "fw_projected=fw_projected_ids" in call


def test_run_counts_the_split_batch_warnings_in_the_control_status():
    """The split-batch / not-modelled warnings count toward the run's Control
    status (ok/warn) like every other warning stream. Source pin, not a run:
    a real run always carries other warnings, so no plan can show this count
    alone (independent mutation proof, 2026-09-11: dropping it left every
    test green)."""
    src = inspect.getsource(run_mod)
    i = src.index("total_warnings = (")
    block = src[i:src.index("status = ", i)]
    assert "len(split_warns)" in block
