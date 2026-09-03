"""The ProductionReport reader must find its columns by LABEL, not position.

Measured 2026-09-02 across the 21-report corpus: the sheet ships in at least
three layouts, and the fixed positional map was correct only for the newest.
On the 2025 layout the reader took `Biological FCR in period` as feed amount
and `Harvest deviation count` as gross harvested biomass, so a facility
harvesting ~550 t a month reported 439 kg against ~20 kg of feed -- silently,
with no error, in the reader every forecast, ledger and backtest is built on.

    2024-11 .. 2025-10   harvest read 439 - 8,973 kg   (actual 210 - 627 t)
    2025-11 .. 2026-01   harvest understated 85-100 t  (a partial shift)
    2026-02 onward       correct

The header rows below are copied verbatim from pr_corpus/2026-07-31.xlsx and
pr_corpus/2025-07-31.xlsx, positions included.
"""
import datetime

import pytest

from forecast.production_report import (
    PRLayoutError, _PR_COL, _resolve_pr_columns, read_pr_period)

_L2026 = [
    (5, 'Opening Count'), (6, 'Closing Count'), (7, 'Opening Biomass [kg]'),
    (9, 'Opening Avg weight'), (15, 'Deviation count in period'),
    (16, 'Closing Density'), (20, 'Gross growth in period'),
    (21, 'Gross growth % in period'), (23, 'Feed amount in period'),
    (25, 'Biological FCR in period'),
    (28, 'Harvested count (incl discards) in period'),
    (29, 'Gross harvested biomass, incl. discards [kg] in period'),
    (33, 'Mortality biomass in period'), (36, 'Mortality count in period'),
    (37, 'Culling biomass in period'), (38, 'Culling count in period'),
]
# Same sheet, ten months earlier. Note: no `Deviation count` column at all,
# `Opening Biomass` without its [kg] suffix, and the whole middle block
# shifted one to two places left.
_L2025 = [
    (5, 'Opening Count'), (6, 'Closing Count'), (7, 'Opening Biomass'),
    (9, 'Opening Avg weight'), (15, 'Closing Density'),
    (19, 'Gross growth in period'), (20, 'Gross growth % in period'),
    (21, 'Feed amount in period'), (23, 'Biological FCR in period'),
    (26, 'Harvested count (incl discards) in period'),
    (27, 'Gross harvested biomass, incl. discards [kg] in period'),
    (33, 'Mortality biomass in period'), (36, 'Mortality count in period'),
    (37, 'Culling biomass in period'), (38, 'Culling count in period'),
]


def _hdr(labels):
    row = [None] * 42
    for i, lab in labels:
        row[i] = lab
    return tuple(row)


def test_current_layout_still_reads_exactly_as_before():
    """The 2026 layout is what the positional map was written against.

    If this fails, the fix has MOVED today's live numbers -- which it must not.
    Every current report has to read identically before and after.
    """
    idx, missing = _resolve_pr_columns(_hdr(_L2026))
    assert not missing
    assert idx == _PR_COL


def test_old_layout_resolves_away_from_the_legacy_positions():
    idx, missing = _resolve_pr_columns(_hdr(_L2025))
    assert not missing                    # dev_count is optional and absent
    assert "dev_count" not in idx         # the older sheet simply lacks it
    # The four that were wrong, each on its LABEL and not its old slot.
    assert idx["feed_kg"] == 21 != _PR_COL["feed_kg"]
    assert idx["harv_gross_kg"] == 27 != _PR_COL["harv_gross_kg"]
    assert idx["harv_count"] == 26 != _PR_COL["harv_count"]
    assert idx["growth_kg"] == 19 != _PR_COL["growth_kg"]
    # ...and the ones that happened to align must NOT be disturbed.
    for f in ("open_count", "close_count", "mort_bio_kg", "cull_count"):
        assert idx[f] == _PR_COL[f], f


def test_percent_growth_is_not_mistaken_for_growth_kg():
    """`Gross growth % in period` sits directly beside `Gross growth in
    period` in BOTH layouts. Loose prefix matching would collapse them and
    report a percentage as a mass."""
    assert _resolve_pr_columns(_hdr(_L2026))[0]["growth_kg"] == 20
    assert _resolve_pr_columns(_hdr(_L2025))[0]["growth_kg"] == 19


def test_biological_fcr_is_never_read_as_feed():
    """The specific mix-up that produced ~20 kg of monthly feed."""
    for labels in (_L2026, _L2025):
        idx = _resolve_pr_columns(_hdr(labels))[0]
        fcr_col = next(i for i, lab in labels if lab.startswith("Biological"))
        assert idx["feed_kg"] != fcr_col


def test_opening_biomass_resolves_with_or_without_the_kg_suffix():
    assert _resolve_pr_columns(_hdr(_L2026))[0]["open_bio_kg"] == 7
    assert _resolve_pr_columns(_hdr(_L2025))[0]["open_bio_kg"] == 7


class _WS:
    def __init__(self, rows):
        self._rows = rows

    def iter_rows(self, values_only=True):
        return iter(self._rows)


def test_missing_required_column_raises_rather_than_returning_zero():
    """A header we can read but cannot map must STOP, not guess.

    Silently returning 0.0 for harvest is the failure mode being fixed: a
    forecast built on it looks finished.
    """
    hdr = list(_hdr(_L2026))
    hdr[29] = None                        # lose gross harvested biomass
    body = [None] * 42
    body[2] = "Fish group name: B01"
    with pytest.raises(PRLayoutError) as e:
        read_pr_period(_WS([tuple(hdr), tuple(body)]),
                       datetime.date(2026, 7, 31))
    assert "harv_gross_kg" in str(e.value)


def test_reads_the_old_layout_end_to_end():
    """Header found by CONTENT, not row number -- it does not sit on the same
    row in every export."""
    body = [None] * 42
    body[2] = "Fish group name: B01"
    body[27] = 12345.0                    # gross harvest, 2025 position
    body[21] = 6789.0                     # feed, 2025 position
    rows = [("Atlantic Sapphire", None, None, None), (None,) * 4,
            _hdr(_L2025), tuple(body)]
    t = read_pr_period(_WS(rows), datetime.date(2025, 7, 31)).totals()
    assert t["harv_gross_kg"] == 12345.0
    assert t["feed_kg"] == 6789.0


def test_state_only_sheet_returns_none_instead_of_raising():
    """A sheet with NO flow columns has nothing for this reader to return.

    That is different from a sheet whose flows exist but could not be located,
    which still raises. The reference-baseline fixture is exactly this shape,
    and treating it as a layout error would have failed a run that is simply
    not asking for period flows.
    """
    hdr = [None] * 42
    for i, lab in ((5, "Opening Count"), (6, "Closing Count"),
                   (7, "Opening Biomass [kg]"), (9, "Opening Avg weight")):
        hdr[i] = lab
    body = [None] * 42
    body[2] = "Fish group name: B01"
    assert read_pr_period(_WS([tuple(hdr), tuple(body)]),
                          datetime.date(2026, 7, 31)) is None


def test_partial_flow_columns_still_raise():
    """Some flows found, others not -> the layout is half-recognised, which is
    precisely when a positional guess produces a plausible wrong number."""
    hdr = list(_hdr(_L2026))
    for i in (23, 29):                    # lose feed and gross harvest
        hdr[i] = None
    body = [None] * 42
    body[2] = "Fish group name: B01"
    with pytest.raises(PRLayoutError):
        read_pr_period(_WS([tuple(hdr), tuple(body)]),
                       datetime.date(2026, 7, 31))
