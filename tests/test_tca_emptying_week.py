"""The tank audit covers the week the facility ends EMPTY.

Since calendar-09 the realized plan walks every horizon week, so a plan whose
projection runs out of batches keeps going and harvests the fish still in the
tanks (2026-02-28 era run: B48's last 7,128 fish from tank 63 in 2027-W30).
That week ends with no fish anywhere, so BatchLocations -- which lists only
non-empty tanks -- has no row for it. TankContinuityAudit took its weeks from
BatchLocations alone, so the week was skipped and that last harvest was in no
audit row: the conservation proof never saw those fish leave.

It now also audits every week an event touched. The shipped sheet and, since
engine change 5 (counts-02), the LNS placement accept gate both use that
scope; audit_touched_empty_tanks=False still gives the pre-2026-09-12 scope.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import openpyxl

from forecast import excel_io
from forecast.placement import BatchLocationRow

W1, W2 = "2027-W29", "2027-W30"
D1, D2 = date(2027, 7, 19), date(2027, 7, 26)


def _inputs():
    loc = BatchLocationRow(
        week_label=W1, week_start=D1, batch_id="B48", tank_id=63,
        location_id="OG6N-63", system_id="OG6N", count=1000.0,
        avg_wt_g=5300.0, biomass_kg=5300.0, density_kg_m3=50.0)
    harvest = SimpleNamespace(event_date=D2, batch_id="B48", source_tank_id=63,
                              count=1000.0, avg_wt_g=5300.0)
    # No mortality or growth recorded in W2 (the tank is harvested out).
    realized = {(63, W1, "B48"): [0.0, 0.0], (63, W2, "B48"): [0.0, 0.0]}
    return dict(batch_locations=[loc], batch_week_states=[],
                harvest_events=[harvest], transfer_events=[], grade_events=[],
                tranog_events=[], initial_state=None, realized_biology=realized)


def _rows(**over):
    wb = openpyxl.Workbook()
    kw = _inputs()
    kw.update(over)
    excel_io.write_tank_continuity_audit(wb, **kw)
    rows = list(wb["TankContinuityAudit"].iter_rows(values_only=True))
    hi = next(i for i, r in enumerate(rows) if r and r[0] == "Week")
    hdr = [str(c) for c in rows[hi]]
    return [dict(zip(hdr, r)) for r in rows[hi + 1:] if r and r[0]]


def test_the_week_the_facility_empties_is_audited():
    rows = {(r["Week"], r["Tank"]): r for r in _rows()}
    assert (W2, 63) in rows, sorted(rows)
    r = rows[(W2, 63)]
    assert r["Open_Count"] == 1000
    assert r["Harvest_Out"] == 1000
    assert (r["Actual_Close"] or 0) == 0
    assert abs(r["Delta"] or 0) < 1
    assert not r["Flag"]


def test_the_old_scope_switch_leaves_the_emptying_week_out():
    """NEGATIVE CONTROL for the scope switch: with audit_touched_empty_tanks
    False (the pre-2026-09-12 scope) the event-only week is not added."""
    rows = {(r["Week"], r["Tank"]) for r in _rows(audit_touched_empty_tanks=False)}
    assert (W2, 63) not in rows
    assert (W1, 63) in rows
