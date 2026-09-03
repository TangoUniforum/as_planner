"""ProductionReport reader + FacilityState hydration.

ProductionReport is the canonical current-state snapshot. By
agreement (DESIGN §1, item 3), its closing date is the day BEFORE
forecast_start, so the PR closing values are exactly the tank states
at forecast_start opening — no bridging projection needed.

Sheet layout (row-tuple zero-indexed; cells refer to 1-indexed column):
  col 1: 'Closing Month: <m/d/yyyy>'   (top-level total row)
  col 2: 'Site: <name>'                 (site-level rollup)
  col 3: 'Fish group name: <id>'        (batch-level rollup)
  col 4: 'Unit: <tank_id>'              (per-tank row — the ones we want)
  col 7:  Closing Count   (fish)
  col 9:  Closing Biomass (kg)
  col 11: Closing Avg weight (g)

CV is not surfaced by PR; we default per-tank to the batch's
TranOG_CV from BatchRegistry (16% if the batch isn't found).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from .models import BatchInput
from .state import FacilityState, STAGE_FW, STAGE_SW


@dataclass
class PRTankRecord:
    """One OG (batch, tank) closing-state row from ProductionReport.

    Only emitted for OG-side rows where the Unit identifier is a bare
    integer matching a FacilityConfig OG tank_id (e.g. "Unit: 61").
    """
    batch_id: str
    tank_id: int
    closing_count: float
    closing_biomass_kg: float
    closing_avg_wt_g: float


@dataclass
class PRFWRecord:
    """One FW (batch, physical-unit) closing-state row.

    FW PR identifiers are stage-prefixed strings ("PostS.01", "Smolt.03").
    FacilityConfig models each FW system as a single logical tank but PR
    can place multiple batches in the same FW system simultaneously, so
    FW state needs a different representation than OG TankState.
    """
    batch_id: str
    unit_label: str          # raw Unit string, e.g. "PostS.01"
    fw_system: str           # parsed prefix, e.g. "PostS"
    closing_count: float
    closing_biomass_kg: float
    closing_avg_wt_g: float


# Map PR Unit-prefix -> FacilityConfig system_id.
_FW_PREFIX_TO_SYSTEM = {
    "HA1": "HA1",
    "HA2": "HA2",
    "SF": "SF",
    "Parr": "Par",
    "Pre": "PS",
    "PreS": "PS",
    "Smolt": "SM",
    "PostS": "PSM",
}


def _parse_unit_label(unit_str: str) -> tuple[Optional[int], Optional[str]]:
    """Classify a 'Unit:' value as OG (int) or FW (prefix string).

    Returns (og_tank_id, fw_prefix). Exactly one is non-None.
    "61"        -> (61, None)
    "PostS.01"  -> (None, "PostS")
    """
    s = unit_str.strip()
    if s.isdigit():
        return int(s), None
    # FW prefix: text before the dot (if any) else text up to first digit.
    prefix = re.split(r"[.\d]", s, maxsplit=1)[0]
    return None, prefix or None


def parse_pr_worksheet(
    ws, quiet: bool = False,
) -> tuple[Optional[date], list[PRTankRecord], list[PRFWRecord]]:
    """Parse ONE worksheet as a ProductionReport.

    Split out from read_production_report so a sheet can be identified by what
    is IN it rather than what it is CALLED -- see find_pr_sheet. `quiet`
    suppresses the per-row warnings while probing candidate sheets, so scanning
    a workbook does not print warnings about sheets that are not the report.
    """
    closing_date: Optional[date] = None
    current_batch: Optional[str] = None
    og_records: list[PRTankRecord] = []
    fw_records: list[PRFWRecord] = []

    for row in ws.iter_rows(values_only=True):
        if not row:
            continue

        # Closing Month — col 1.
        c1 = row[0] if len(row) > 0 else None
        if isinstance(c1, str) and "Closing Month" in c1:
            m = re.search(r"(\d+/\d+/\d+)", c1)
            if m:
                try:
                    closing_date = datetime.strptime(m.group(1), "%m/%d/%Y").date()
                except ValueError:
                    pass
            continue

        # Fish group — col 3.
        c3 = row[2] if len(row) > 2 else None
        if isinstance(c3, str) and "Fish group" in c3:
            m = re.search(r"B\d+", c3)
            if m:
                current_batch = m.group(0)
            else:
                # Unrecognized batch label — any tanks under this group
                # would be silently dropped. Set current_batch to None
                # so the data rows below are skipped, and log it so the
                # operator sees the gap.
                current_batch = None
                if not quiet:
                    print(f"  WARN: ProductionReport 'Fish group' row "
                          f"'{c3.strip()}' has no Bnn identifier; "
                          f"its tanks will not be hydrated")
            continue

        # Unit (per-tank) — col 4.
        c4 = row[3] if len(row) > 3 else None
        if not (isinstance(c4, str) and "Unit" in c4):
            continue
        if current_batch is None:
            continue
        unit_raw = c4.replace("Unit:", "").strip()
        og_id, fw_prefix = _parse_unit_label(unit_raw)

        closing_count = row[6] if len(row) > 6 else None       # col 7
        closing_biomass = row[8] if len(row) > 8 else None     # col 9
        closing_avg_wt = row[10] if len(row) > 10 else None    # col 11
        if not (isinstance(closing_count, (int, float)) and closing_count > 0):
            continue

        biomass = float(closing_biomass) if isinstance(closing_biomass, (int, float)) else 0.0
        avg_wt = float(closing_avg_wt) if isinstance(closing_avg_wt, (int, float)) else 0.0

        if og_id is not None:
            og_records.append(PRTankRecord(
                batch_id=current_batch,
                tank_id=og_id,
                closing_count=float(closing_count),
                closing_biomass_kg=biomass,
                closing_avg_wt_g=avg_wt,
            ))
        else:
            system = _FW_PREFIX_TO_SYSTEM.get(fw_prefix or "", fw_prefix or "?")
            fw_records.append(PRFWRecord(
                batch_id=current_batch,
                unit_label=unit_raw,
                fw_system=system,
                closing_count=float(closing_count),
                closing_biomass_kg=biomass,
                closing_avg_wt_g=avg_wt,
            ))

    return closing_date, og_records, fw_records


_PR_CANON = "productionreport"


def _norm_sheet(name) -> str:
    """Sheet name reduced to letters+digits, lowercased."""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def find_pr_sheet(wb):
    """The ProductionReport worksheet, identified by CONTENT — or None.

    THE TAB NAME DOES NOT MATTER. Real reports arrive called things like
    "8 23 2026 AS Monthly Production", and a name test rejected them even
    though the sheet was set up correctly. tools/build_pr_corpus.py exists
    partly to RENAME sheets to satisfy the old exact-string lookup, which was
    the clue that the name was never the right thing to key on.

    A sheet IS a production report if it parses as one: a `Closing Month` date
    plus at least one tank record (`Fish group` Bnn + `Unit:` rows with a
    positive closing count). That is a strong signature -- an unrelated sheet
    will not produce both -- so this accepts any name while still refusing a
    workbook that does not actually contain the report.

    Order: the canonical name first (fast, and unambiguous when present), then
    any name that reduces to "productionreport", then a content scan. If
    several sheets parse, the one with the MOST records wins, because a summary
    or partial copy of the report will parse thinly next to the real thing.
    """
    if wb is None:
        return None
    if "ProductionReport" in wb.sheetnames:
        return wb["ProductionReport"]
    for n in wb.sheetnames:
        if _norm_sheet(n) == _PR_CANON:
            return wb[n]
    # ---- content scan: whichever sheet actually parses as the report -------
    best, best_n = None, 0
    for n in wb.sheetnames:
        try:
            closing, og, fw = parse_pr_worksheet(wb[n], quiet=True)
        except Exception:                       # noqa: BLE001 — not this sheet
            continue
        if closing is None:
            continue
        n_rec = len(og) + len(fw)
        if n_rec > best_n:
            best, best_n = wb[n], n_rec
    return best


def pr_sheet_name(wb):
    """Name of the sheet find_pr_sheet would use, or None."""
    ws = find_pr_sheet(wb)
    return getattr(ws, "title", None) if ws is not None else None


def read_production_report(
    wb,
) -> tuple[Optional[date], list[PRTankRecord], list[PRFWRecord]]:
    """Parse the workbook's ProductionReport into
    (closing_date, og_records, fw_records).

    The sheet is found by CONTENT, not by name (see find_pr_sheet).

    OG records are emitted with closing_count > 0; same for FW.
    Empty tanks at closing are silently dropped.
    """
    ws = find_pr_sheet(wb)
    if ws is None:
        raise KeyError(
            "no sheet in this workbook parses as a ProductionReport (needs a "
            "'Closing Month' date plus 'Fish group' / 'Unit:' rows); sheets "
            f"found: {list(wb.sheetnames)}")
    return parse_pr_worksheet(ws)


def summarize_fw_records(fw_records: list[PRFWRecord]) -> dict:
    """Per-(batch, system) aggregate of FW PR records for diagnostic display."""
    rolled: dict[tuple[str, str], dict] = {}
    for r in fw_records:
        key = (r.batch_id, r.fw_system)
        e = rolled.setdefault(key, {"count": 0.0, "biomass_kg": 0.0, "units": 0})
        e["count"] += r.closing_count
        e["biomass_kg"] += r.closing_biomass_kg
        e["units"] += 1
    return rolled


def hydrate_facility_state(
    state: FacilityState,
    records: list[PRTankRecord],
    batches: list[BatchInput],
) -> list[str]:
    """Stock each tank in `state` from the matching PR record.

    Stage is derived from TankConfig.type (FW tank -> FW stage, OG tank
    -> SW stage). CV defaults to the batch's TranOG_CV.

    Returns list of warning strings for tanks that couldn't be hydrated
    (unknown tank id, tank already stocked, etc.).
    """
    warns: list[str] = []
    batch_by_id = {b.batch_id: b for b in batches}
    _no_meta_warned: set[str] = set()
    for r in records:
        tank = state.tanks_by_id.get(r.tank_id)
        if tank is None:
            warns.append(
                f"PR: unknown tank #{r.tank_id} for batch {r.batch_id} "
                f"(count={r.closing_count:.0f}, biomass={r.closing_biomass_kg:.0f} kg)"
            )
            continue
        if not tank.is_empty:
            warns.append(
                f"PR: tank {tank.location_id} (#{r.tank_id}) already holds "
                f"batch {tank.batch_id}; cannot hydrate PR record for {r.batch_id}"
            )
            continue
        b = batch_by_id.get(r.batch_id)
        if b is None and r.batch_id not in _no_meta_warned:
            # Downstream biology silently FREEZES tanks whose batch has no
            # metadata (no growth, no mortality — 0-drift still passes).
            # Warn once per batch at the hydration site, where the cause is.
            _no_meta_warned.add(r.batch_id)
            warns.append(
                f"PR: batch {r.batch_id} has no Batches-sheet metadata — its "
                f"tank(s) hydrate but biology will NOT advance them "
                f"(no growth/mortality)")
        cv = b.tran_og_cv if b else 16.0
        stage = STAGE_FW if tank.type == "FW" else STAGE_SW
        tank.assign(
            batch_id=r.batch_id,
            count=r.closing_count,
            avg_wt_g=r.closing_avg_wt_g,
            cv_pct=cv,
            stage=stage,
        )
    return warns


def summarize_hydration(state: FacilityState) -> dict:
    """Snapshot summary for diagnostics: counts, biomass, per-system rollup."""
    occupied_tanks = [t for t in state.tanks_by_id.values() if not t.is_empty]
    by_system_biomass = state.biomass_by_system()
    by_system_occupied = {
        s: sum(1 for t in state.tanks_in_system(s) if not t.is_empty)
        for s in state.systems()
    }
    by_batch = state.count_by_batch()
    return {
        "occupied_tanks": len(occupied_tanks),
        "total_tanks": len(state.tanks_by_id),
        "total_biomass_kg": state.total_biomass(),
        "by_system_biomass": by_system_biomass,
        "by_system_occupied": by_system_occupied,
        "by_batch_count": by_batch,
        "num_batches_in_facility": len(by_batch),
    }


# ============================================================
# PR period flows — the part of the closing month already ELAPSED
# ============================================================

# LEGACY positional fallback, used ONLY when a sheet carries no recognisable
# header row. These positions are correct for the 2026-02-onward layout and
# WRONG for older exports -- which is why _resolve_pr_columns exists and this
# map is the last resort, not the first.
#: Field -> the EXACT header label it ships under, lowercased and space-collapsed.
#: Matching is by LABEL, never by position -- see _resolve_pr_columns.
_PR_LABELS = {
    "open_count":     ("opening count",),
    "close_count":    ("closing count",),
    "open_bio_kg":    ("opening biomass [kg]", "opening biomass"),
    "open_avg_wt_g":  ("opening avg weight",),
    "growth_kg":      ("gross growth in period",),
    "feed_kg":        ("feed amount in period",),
    "harv_count":     ("harvested count (incl discards) in period",),
    "harv_gross_kg":  ("gross harvested biomass, incl. discards [kg] in period",
                       "gross harvested biomass, incl. discards [kg]",
                       "gross harvested biomass, incl. discards"),
    "mort_bio_kg":    ("mortality biomass in period",),
    "mort_count":     ("mortality count in period",),
    "cull_bio_kg":    ("culling biomass in period",),
    "cull_count":     ("culling count in period",),
    "dev_count":      ("deviation count in period",),
}

#: Fields whose absence is survivable. `dev_count` is the site system's own
#: count-correction term and simply does not exist in the pre-2026 layout;
#: reading it as 0 is correct there (no correction was recorded). Every OTHER
#: field is a real flow, and guessing one is how a facility harvesting 550 t a
#: month came to report 439 kg.
_PR_OPTIONAL = frozenset({"dev_count"})

#: The elapsed-period FLOWS -- the only reason read_pr_period exists. A sheet
#: carrying none of them is a state-only export with nothing to report, which
#: is different from a sheet that has flows this reader failed to locate. The
#: first returns None; the second raises. Collapsing the two would either break
#: state-only sheets or restore the silence this whole change removes.
_PR_FLOW_FIELDS = frozenset({
    "growth_kg", "feed_kg", "harv_count", "harv_gross_kg",
    "mort_count", "mort_bio_kg", "cull_count", "cull_bio_kg"})


class PRLayoutError(ValueError):
    """The sheet has a header, but not the columns this reader needs.

    Raised rather than absorbed. A forecast built on a PR whose harvest column
    could not be identified is worse than no forecast: it looks finished.
    """


def _norm_label(v) -> str:
    return " ".join(str(v or "").split()).strip().lower()


def _layout_error(missing, labels_found) -> str:
    """Say which column is missing, what it should be called, and the nearest
    thing actually on the sheet.

    The first version dumped all ~25 labels and left the reader to spot the
    difference. When a vendor renames one column -- `Feed amount in period` to
    `Feed qty in period` -- the whole diagnosis is that one pair, and burying it
    in a wall of text turns a 10-second fix into a hunt. Naming the near-miss
    makes the report self-diagnosing: the fix is to add the new spelling to
    _PR_LABELS, and the message tells you exactly what to add.
    """
    import difflib
    have = sorted(l for l in (labels_found or ()) if l)
    lines = []
    for field in sorted(missing):
        wanted = _PR_LABELS.get(field, ())
        near = difflib.get_close_matches(wanted[0] if wanted else field,
                                         have, n=2, cutoff=0.6)
        lines.append("  %s — expected %r%s" % (
            field,
            wanted[0] if wanted else "?",
            ("; the sheet has %s — renamed?"
             % " or ".join(repr(x) for x in near)) if near
            else "; nothing on the sheet resembles it"))
    return ("ProductionReport header is missing %d required column(s):\n%s\n"
            "Refusing to read the sheet. The positional fallback would return "
            "a plausible-looking number from whichever column happens to sit "
            "there, which is the failure this check exists to prevent. If the "
            "report has simply been renamed, add the new spelling to "
            "_PR_LABELS in forecast/production_report.py.\n"
            "All %d labels found: %s"
            % (len(lines), "\n".join(lines), len(have),
               ", ".join(have)[:800]))


def _resolve_pr_columns(header_row) -> tuple[dict, list]:
    """(field -> column index, missing fields) resolved BY LABEL.

    The ProductionReport ships in at least three layouts and the positional map
    below is only correct for the newest. Measured across the 21-report corpus
    (2026-09-02): on the 2025 layout, position 23 is `Biological FCR in period`
    where the code expected `Feed amount`, and position 29 is `Harvest deviation
    count` where it expected `Gross harvested biomass`. A site harvesting
    ~550 t/month therefore read as 439-8,973 kg, with monthly feed of -7 to 23
    kg, silently -- no error, just wrong numbers, in the reader every forecast,
    ledger and backtest is built on.

    Returning the missing list rather than raising lets the caller decide: a
    forecast can proceed without `dev_count`, but nothing should proceed while
    silently inventing a harvest figure.
    """
    idx, seen = {}, {}
    for j, cell in enumerate(header_row or ()):
        lab = _norm_label(cell)
        if lab:
            seen.setdefault(lab, j)
    for field, labels in _PR_LABELS.items():
        for lab in labels:
            if lab in seen:
                idx[field] = seen[lab]
                break
    missing = [f for f in _PR_LABELS if f not in idx and f not in _PR_OPTIONAL]
    return idx, missing


_PR_COL = {
    "open_count": 5, "close_count": 6, "open_bio_kg": 7, "open_avg_wt_g": 9,
    "growth_kg": 20, "feed_kg": 23,
    "harv_count": 28, "harv_gross_kg": 29,
    "mort_bio_kg": 33, "mort_count": 36,
    "cull_bio_kg": 37, "cull_count": 38,
    # The site system's own count-correction term. It is NOT a fish flow, but
    # it sits inside the PR's closing balance, so deriving an input without
    # subtracting it would attribute the correction to stocking.
    "dev_count": 15,
}


@dataclass
class PRBatchPeriod:
    """One batch's ELAPSED-period flows from the ProductionReport.

    The PR's "in period" columns are MONTH-TO-DATE: they cover the 1st of the
    closing month through the closing date (operator-confirmed 2026-08-18, and
    independently consistent with the data — 370,225 kg of feed at a
    ~29,000 kg/day facility rate is 12.8 days, matching Aug 1-13). That is what
    makes this a clean addition to the forecast's own figures rather than an
    overlap: a since-last-report period would straddle two months and
    double-count part of the previous one.

    When the closing date is mid-month the forecast starts mid-month too (it
    begins the day after), so the month is split across two sources: this
    record is the part that already happened.
    """
    batch_id: str
    open_count: float
    open_bio_kg: float
    open_avg_wt_g: float
    growth_kg: float
    feed_kg: float
    harv_count: float
    harv_gross_kg: float
    mort_count: float
    mort_bio_kg: float
    cull_count: float
    cull_bio_kg: float
    # Optional: needed only to DERIVE the period's input (see input_count).
    # Defaulted so existing constructors keep working; a record without a
    # closing balance simply derives no input rather than a negative one.
    close_count: float = 0.0
    dev_count: float = 0.0

    @property
    def input_count(self) -> float:
        """Fish that ENTERED this batch during the elapsed period.

        The PR has no usable input column -- "Transfer in count in period"
        reads 0 even on a month that took a full smolt intake -- but the
        intake is inside the closing balance, so it can be recovered from the
        balance itself:

            input = close - open + harvested + mortality + cull - deviation

        Measured on the 8.23.26 PR: every batch derives 0 except B56, which
        derives exactly 570,000 -- one smolt intake, and precisely the figure
        by which that PR's own facility balance failed to close
        (opening 4,807,523 + flows = 4,537,983 against a stated closing of
        5,107,983). Without this the batch materialises out of nothing and the
        month's Count_Check reads -570,000.
        """
        if self.close_count <= 0:
            return 0.0          # no closing balance recorded -> nothing to derive
        return max(0.0, self.close_count - self.open_count + self.harv_count
                   + self.mort_count + self.cull_count - self.dev_count)


@dataclass
class PRPeriod:
    """The whole elapsed slice of the PR's closing month."""
    closing_date: date
    batches: dict[str, PRBatchPeriod]

    @property
    def month_label(self) -> str:
        return f"{self.closing_date.year:04d}-{self.closing_date.month:02d}"

    @property
    def is_mid_month(self) -> bool:
        """True when the PR closes part-way through its month.

        A PR closing on the LAST day of a month needs no merge at all: the
        forecast then starts on the 1st, so the forecast alone already covers
        the whole of its first month. Operator rule, 2026-08-18.
        """
        d = self.closing_date
        nxt = (d.replace(year=d.year + 1, month=1, day=1) if d.month == 12
               else d.replace(month=d.month + 1, day=1))
        last_day = (nxt - timedelta(days=1)).day
        return d.day < last_day

    def totals(self) -> dict[str, float]:
        out = {k: 0.0 for k in (
            "growth_kg", "feed_kg", "harv_count", "harv_gross_kg",
            "mort_count", "mort_bio_kg", "cull_count", "cull_bio_kg")}
        for b in self.batches.values():
            for k in out:
                out[k] += getattr(b, k)
        return out


def read_pr_period(ws, closing_date) -> Optional[PRPeriod]:
    """Per-batch elapsed-period flows from a ProductionReport worksheet.

    Reads the batch-level rollup rows (col 3 `Fish group name: <id>`), which
    carry both the batch's OPENING state and its flows for the elapsed part of
    the closing month. Returns None when the sheet has no batch rows.

    Only the reporting layer uses this. The audits deliberately do NOT: they
    prove the FORECAST conserves, and feeding actuals into them would break
    their identities and mask real defects.
    """
    if ws is None or closing_date is None:
        return None
    batches: dict[str, PRBatchPeriod] = {}
    colmap: Optional[dict] = None

    def _num(row, key) -> float:
        i = (colmap or _PR_COL).get(key)
        if i is None or i >= len(row):
            return 0.0
        v = row[i]
        try:
            return float(v or 0.0)
        except (TypeError, ValueError):
            return 0.0

    for row in ws.iter_rows(values_only=True):
        if len(row) < 4:
            continue
        if colmap is None:
            # Resolve the columns from the header row the sheet ships with,
            # by LABEL. Detected by content rather than by position, because
            # the header does not sit on the same row in every export.
            _labs = {_norm_label(c) for c in row if c}
            if "opening count" in _labs and "closing count" in _labs:
                colmap, _missing = _resolve_pr_columns(row)
                if not (set(colmap) & _PR_FLOW_FIELDS):
                    # State-only export: no flow columns to find, so there is
                    # nothing for this reader to return and nothing ambiguous
                    # about it.
                    return None
                if _missing:
                    raise PRLayoutError(_layout_error(_missing, _labs))
                continue
        c3 = row[2]
        if not (isinstance(c3, str) and "Fish group name" in c3):
            continue
        bid = c3.split(":", 1)[1].strip()
        if not bid:
            continue
        batches[bid] = PRBatchPeriod(
            batch_id=bid,
            open_count=_num(row, "open_count"),
            close_count=_num(row, "close_count"),
            dev_count=_num(row, "dev_count"),
            open_bio_kg=_num(row, "open_bio_kg"),
            open_avg_wt_g=_num(row, "open_avg_wt_g"),
            growth_kg=_num(row, "growth_kg"),
            feed_kg=_num(row, "feed_kg"),
            harv_count=_num(row, "harv_count"),
            harv_gross_kg=_num(row, "harv_gross_kg"),
            mort_count=_num(row, "mort_count"),
            mort_bio_kg=_num(row, "mort_bio_kg"),
            cull_count=_num(row, "cull_count"),
            cull_bio_kg=_num(row, "cull_bio_kg"),
        )
    if not batches:
        return None
    if colmap is None:
        # No recognisable header anywhere in the sheet. Positional is then the
        # only option left, but it is a GUESS and must say so out loud -- this
        # is the exact silence that let 15 of 21 corpus reports read their
        # harvest out of the deviation-count column.
        print("  WARNING: ProductionReport has no recognisable header row; "
              "falling back to fixed column positions, which are correct only "
              "for the 2026-02-onward layout. Check the harvest and feed "
              "figures before trusting this run.")
    d = closing_date.date() if hasattr(closing_date, "date") else closing_date
    return PRPeriod(closing_date=d, batches=batches)
