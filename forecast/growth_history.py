"""Growth history: the seawater growth the Production Reports actually show, kept
as every PR is added, and the ACTUAL growth model fitted from it.

Operator plan 2026-10-18 ("as we add new PRs it builds on the history to improve
the actual growth model"). Three pieces, all data, none of them read by the
engine -- a fitted curve only reaches a plan when the operator copies a frozen
version into biology.yaml sgr_sw_by_model and a batch picks it (growth_model):

1. SNAPSHOTS. One per Production Report added: closing date, kind and the OG
   tank rows (batch, tank, count, mean weight). Kind is FULL when the workbook
   has the accumulated (Acc) tab -- the full monthly report -- else MID-MONTH
   (the PR sheet only).
2. THE CHAIN. One snapshot per closing date, chosen by a fixed rule, and only
   one source per month: a full report supersedes every mid-month PR of its
   month; in a month with no full report yet, only the latest mid-month PR is
   used and its interval is PROVISIONAL. Consecutive chain snapshots give the
   CLEAN TANK INTERVALS: the same batch in the same tank at both ends, no fish
   added and under 5% lost, not a 6N tank. Anything else -- a harvest, grade
   or move -- changes the mean weight without growth and is left out.
3. THE FIT (the hindcast winner of 2026-10-17, "model B"): grams per day by
   size, AGR = a x w^b below a breakpoint (2 kg) and a flat cap above it,
   fitted on the intervals that end in the rolling window (24 months = one full
   life cycle), each weighted by its days (a 4-day mid-month interval counts
   4/30 of a month). It is converted to an SGR table on the biology size rows.
   Each fit is saved as a FROZEN version: adding PRs later never changes a saved
   version, it makes the next one.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

FULL, MID = "full", "mid-month"
STORE_FORMAT = 1
# A tank interval losing more than this share of its fish was harvested,
# graded or moved, so its mean-weight change is not growth.
CLEAN_LOSS = 0.05
SIXN_SYSTEM = "OG6N"
DEFAULT_WINDOW_MONTHS = 24
DEFAULT_BREAKPOINT_G = 2000.0
DEFAULT_SW_MIN_G = 150.0          # biology rows below this keep the base value
MIN_INTERVALS = 20                # per side of the breakpoint
HOLDOUT_MONTHS = 3


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #
@dataclass
class Snapshot:
    closing: str                  # ISO date
    kind: str                     # FULL or MID
    source: str                   # file name as added
    sha1: str
    added: str                    # ISO timestamp, the order of adding
    og: list = field(default_factory=list)   # [batch, tank, count, avg_wt_g]
    modified: str = ""            # the file's last-modified time (ISO)

    @property
    def closing_date(self) -> date:
        return date.fromisoformat(self.closing)

    @property
    def prelim(self) -> bool:
        return "prelim" in self.source.lower()


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_snapshot(path, *, added: Optional[str] = None) -> Snapshot:
    """One Production Report -> Snapshot. Raises ValueError (naming the file)
    when the workbook has no readable ProductionReport sheet."""
    import openpyxl
    from .accumulated import find_acc_sheet
    from .production_report import read_production_report
    path = Path(path)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                closing, og, _fw = read_production_report(wb)
            except Exception as e:  # noqa: BLE001 - reported by name below
                raise ValueError(f"{path.name}: no readable ProductionReport "
                                 f"({type(e).__name__}: {str(e)[:120]})") from e
            kind = FULL if find_acc_sheet(wb) is not None else MID
    finally:
        wb.close()
    if closing is None:
        raise ValueError(f"{path.name}: the ProductionReport has no closing date")
    closing = closing.date() if isinstance(closing, datetime) else closing
    return Snapshot(
        closing=closing.isoformat(), kind=kind, source=path.name,
        sha1=_sha1(path), added=added or datetime.now().isoformat(timespec="seconds"),
        modified=datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        og=[[r.batch_id, int(r.tank_id), float(r.closing_count),
             float(r.closing_avg_wt_g)] for r in og])


# A report with fewer tank rows than this share of the fullest one for the
# same closing date is an extract (a few batches), never the whole report.
EXTRACT_SHARE = 0.8


def _rank(s: Snapshot, most_rows: int) -> tuple:
    """Higher wins for one closing date: full > mid-month, final > prelim, a
    whole report > a few-batch extract, then the most recently modified file
    (V3 over V2), then the later add."""
    return (s.kind == FULL, not s.prelim, len(s.og) >= EXTRACT_SHARE * most_rows,
            s.modified, s.added)


def choose_chain(snapshots) -> tuple[list, list]:
    """(chain, superseded): one snapshot per closing date, one source per month.

    Superseded entries are (snapshot, reason) so the operator can see why a
    report was not used."""
    by_date: dict[str, list] = {}
    for s in snapshots:
        by_date.setdefault(s.closing, []).append(s)
    best: dict[str, Snapshot] = {}
    superseded = []
    for closing, group in by_date.items():
        most = max(len(s.og) for s in group)
        ranked = sorted(group, key=lambda s: _rank(s, most), reverse=True)
        best[closing] = ranked[0]
        for s in ranked[1:]:
            superseded.append((s, f"same closing date as {ranked[0].source}"))
    full_months = {s.closing[:7] for s in best.values() if s.kind == FULL}
    latest_mid: dict[str, Snapshot] = {}
    chain = []
    for s in sorted(best.values(), key=lambda x: x.closing):
        m = s.closing[:7]
        if s.kind == MID and m in full_months:
            superseded.append((s, "the full report for its month is in"))
            continue
        if s.kind == MID:
            prev = latest_mid.get(m)
            if prev is not None:
                superseded.append((prev, f"a later mid-month PR ({s.source}) "
                                         f"is in for {m}"))
                chain.remove(prev)
            latest_mid[m] = s
        chain.append(s)
    return chain, superseded


# --------------------------------------------------------------------------- #
# Clean tank intervals
# --------------------------------------------------------------------------- #
@dataclass
class Interval:
    batch: str
    tank: int
    d0: str
    d1: str
    days: int
    c0: float
    c1: float
    w0: float
    w1: float
    provisional: bool             # an end is a mid-month PR

    @property
    def wmid(self) -> float:
        return math.sqrt(self.w0 * self.w1)

    @property
    def agr(self) -> float:
        return (self.w1 - self.w0) / self.days

    @property
    def sgr(self) -> float:
        return 100.0 * math.log(self.w1 / self.w0) / self.days

    @property
    def weight(self) -> float:
        """Days covered, capped at one month: a 4-day interval counts 4/30."""
        return min(self.days, 30) / 30.0


def clean_intervals(chain, sixn_tanks, og_tanks=None) -> list:
    """Clean tank intervals between consecutive chain snapshots.

    `sixn_tanks`: tank ids of the 6N purge/harvest system (excluded).
    `og_tanks`: if given, only these tank ids count (unknown ids are skipped).
    """
    sixn = set(sixn_tanks)
    out = []
    for s0, s1 in zip(chain, chain[1:]):
        days = (s1.closing_date - s0.closing_date).days
        if days <= 0:
            continue
        a = {(b, t): (c, w) for b, t, c, w in s0.og}
        b_ = {(b, t): (c, w) for b, t, c, w in s1.og}
        for key in sorted(set(a) & set(b_)):
            batch, tank = key
            if tank in sixn or (og_tanks is not None and tank not in og_tanks):
                continue
            (c0, w0), (c1, w1) = a[key], b_[key]
            if w0 <= 0 or w1 <= 0 or c0 <= 0:
                continue
            if c1 > c0 * 1.001 or c1 < c0 * (1.0 - CLEAN_LOSS):
                continue
            out.append(Interval(batch, tank, s0.closing, s1.closing, days,
                                c0, c1, w0, w1,
                                provisional=(s0.kind == MID or s1.kind == MID)))
    return out


def facility_tank_sets(facility) -> tuple[set, set]:
    """(sixn_tank_ids, og_tank_ids) from a FacilityConfig."""
    sixn = {t.tank_id for t in facility.tanks if t.system_id == SIXN_SYSTEM}
    og = {t.tank_id for t in facility.tanks if t.type == "OG"}
    return sixn, og


# --------------------------------------------------------------------------- #
# The fit
# --------------------------------------------------------------------------- #
def _months_before(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + (d.month - 1) - months, 12)
    m += 1
    last = [31, 29 if (y % 4 == 0 and (y % 100 or y % 400 == 0)) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last))


def _wmedian(vals, wts) -> float:
    pairs = sorted(zip(vals, wts))
    half = sum(w for _, w in pairs) / 2.0
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= half:
            return v
    return pairs[-1][0]


@dataclass
class GrowthFit:
    name: str
    as_of: str
    window_months: int
    window_start: str
    breakpoint_g: float
    a: float
    b: float
    cap_g_day: float
    n: int
    n_provisional: int
    n_below: int
    n_above: int
    sizes_g: list
    sgr_pct_day: list             # one per biology size row
    holdout: Optional[dict] = None
    sources: list = field(default_factory=list)
    created: str = ""

    def agr_g_day(self, w: float) -> float:
        return min(self.a * w ** self.b, self.cap_g_day)


def _fit_params(ivs, breakpoint_g):
    lo = [i for i in ivs if i.wmid < breakpoint_g and i.agr > 0]
    hi = [i for i in ivs if i.wmid >= breakpoint_g]
    if len(lo) < MIN_INTERVALS or len(hi) < MIN_INTERVALS:
        raise ValueError(
            f"not enough clean tank intervals to fit: {len(lo)} below "
            f"{breakpoint_g:,.0f} g and {len(hi)} above (need {MIN_INTERVALS} each)")
    xs = [math.log(i.wmid) for i in lo]
    ys = [math.log(i.agr) for i in lo]
    ws = [i.weight for i in lo]
    sw = sum(ws)
    mx = sum(w * x for w, x in zip(ws, xs)) / sw
    my = sum(w * y for w, y in zip(ws, ys)) / sw
    b = (sum(w * (x - mx) * (y - my) for w, x, y in zip(ws, xs, ys))
         / sum(w * (x - mx) ** 2 for w, x in zip(ws, xs)))
    a = math.exp(my - b * mx)
    cap = _wmedian([i.agr for i in hi], [i.weight for i in hi])
    return a, b, cap, len(lo), len(hi)


def _agr_to_sgr(agr: float, w: float) -> float:
    return 100.0 * math.log(1.0 + agr / w)


def _predict(w0: float, days: int, sgr_fn) -> float:
    w = w0
    for _ in range(days):
        w *= 1.0 + sgr_fn(w) / 100.0
    return w


def _holdout(ivs, as_of: date, breakpoint_g, sizes, base_sw) -> Optional[dict]:
    """Fit without the last HOLDOUT_MONTHS of intervals, predict them, and score
    the end weight -- the same for the base curve. None if too little data."""
    from .biology import _interp
    cut = _months_before(as_of, HOLDOUT_MONTHS).isoformat()
    train = [i for i in ivs if i.d1 <= cut]
    test = [i for i in ivs if i.d1 > cut]
    if not test:
        return None
    try:
        a, b, cap, _, _ = _fit_params(train, breakpoint_g)
    except ValueError:
        return None
    fit_fn = lambda w: _agr_to_sgr(min(a * w ** b, cap), w)       # noqa: E731
    base_fn = lambda w: _interp(w, sizes, base_sw)                 # noqa: E731
    out = {"n": len(test), "from": cut}
    for label, fn in (("fit", fit_fn), ("base", base_fn)):
        errs = [100.0 * (_predict(i.w0, i.days, fn) - i.w1) / i.w1 for i in test]
        errs.sort()
        absd = sorted(abs(e) for e in errs)
        out[label] = {"median_err_pct": round(errs[len(errs) // 2], 2),
                      "median_abs_err_pct": round(absd[len(absd) // 2], 2)}
    return out


def fit_actual(intervals, *, sizes, base_sw, as_of, name=None,
               window_months=DEFAULT_WINDOW_MONTHS,
               breakpoint_g=DEFAULT_BREAKPOINT_G,
               sw_min_g=DEFAULT_SW_MIN_G, sources=()) -> GrowthFit:
    """Fit the actual growth model on the intervals ending inside the window.

    `sizes`/`base_sw`: biology.yaml sgr_size_g / sgr_sw_pct_day. Rows below
    `sw_min_g` keep the base value (no seawater fish are that small)."""
    as_of = as_of if isinstance(as_of, date) else date.fromisoformat(str(as_of))
    start = _months_before(as_of, window_months)
    ivs = [i for i in intervals
           if start.isoformat() < i.d1 <= as_of.isoformat()]
    a, b, cap, n_lo, n_hi = _fit_params(ivs, breakpoint_g)
    table = []
    for w, base in zip(sizes, base_sw):
        if w < sw_min_g:
            table.append(base)
        else:
            table.append(round(_agr_to_sgr(min(a * w ** b, cap), w), 4))
    return GrowthFit(
        name=name or f"Actual {as_of.isoformat()}", as_of=as_of.isoformat(),
        window_months=window_months, window_start=start.isoformat(),
        breakpoint_g=float(breakpoint_g), a=a, b=b, cap_g_day=cap,
        n=len(ivs), n_provisional=sum(i.provisional for i in ivs),
        n_below=n_lo, n_above=n_hi, sizes_g=list(sizes), sgr_pct_day=table,
        holdout=_holdout(ivs, as_of, breakpoint_g, list(sizes), list(base_sw)),
        sources=list(sources),
        created=datetime.now().isoformat(timespec="seconds"))


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #
class GrowthHistory:
    """The snapshots and the frozen fits, in one JSON file.

    There is no default path on purpose: every caller names the file, so a
    test or a scratch run can never write the operator's history by accident.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.snapshots: list[Snapshot] = []
        self.fits: list[GrowthFit] = []
        if self.path.exists():
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if d.get("format") != STORE_FORMAT:
                raise ValueError(f"{self.path}: unknown growth history format "
                                 f"{d.get('format')!r}")
            self.snapshots = [Snapshot(**s) for s in d.get("snapshots", [])]
            self.fits = [GrowthFit(**f) for f in d.get("fits", [])]

    def add(self, snap: Snapshot) -> str:
        """'duplicate' (same file content already in), else 'added'."""
        if any(s.sha1 == snap.sha1 for s in self.snapshots):
            return "duplicate"
        self.snapshots.append(snap)
        return "added"

    def chain(self):
        return choose_chain(self.snapshots)

    def intervals(self, sixn_tanks, og_tanks=None):
        return clean_intervals(self.chain()[0], sixn_tanks, og_tanks)

    def add_fit(self, fit: GrowthFit) -> None:
        """Frozen: a name is never overwritten with different values."""
        for f in self.fits:
            if f.name == fit.name:
                if f.sgr_pct_day == fit.sgr_pct_day:
                    return
                raise ValueError(
                    f"growth model version {fit.name!r} already exists with "
                    f"different values - versions are frozen; fit under a new name")
        self.fits.append(fit)

    def get_fit(self, name: str) -> GrowthFit:
        for f in self.fits:
            if f.name == name:
                return f
        raise KeyError(f"no growth model version {name!r} "
                       f"(have: {[f.name for f in self.fits]})")

    def save(self) -> None:
        d = {"format": STORE_FORMAT,
             "snapshots": [asdict(s) for s in self.snapshots],
             "fits": [asdict(f) for f in self.fits]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=self.path.name + ".tmp-",
                                   dir=str(self.path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=1)
        os.replace(tmp, self.path)


def biology_entry(fit: GrowthFit, sizes) -> tuple[str, list]:
    """(name, values) for biology.yaml sgr_sw_by_model. Refuses a fit made on
    other size rows -- its values would land on the wrong sizes."""
    if [float(x) for x in fit.sizes_g] != [float(x) for x in sizes]:
        raise ValueError(f"{fit.name!r} was fitted on different biology size rows")
    return fit.name, list(fit.sgr_pct_day)


_PR_NAME = re.compile(r"production report|(^|[ _])pr\.xlsx$", re.IGNORECASE)


def is_report_name(name: str) -> bool:
    """A file name that looks like a Production Report or a mid-month PR."""
    low = name.lower()
    return (low.endswith((".xlsx", ".xlsm")) and not low.startswith("~$")
            and "template" not in low and bool(_PR_NAME.search(name)))
