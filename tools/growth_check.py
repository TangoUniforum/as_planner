"""Grade the SW GROWTH CURVE directly, with nothing else in the loop.

    python tools/growth_check.py --corpus pr_corpus

WHY A SEPARATE TOOL
-------------------
The backtest grades a whole forecast: planner, tank placement, harvest
execution, era registries, week alignment and the growth curve, all at once.
When it reports a bias, any of those can be the cause -- and chasing one through
that harness cost several wrong hypotheses (survivorship, execution confound,
hydration, alignment) before the real one turned up.

This removes every moving part except the curve. For one batch observed in two
consecutive Production Reports it asks a single question:

    starting at the weight the FIRST report states, and compounding the model's
    own SGR day by day, does the model land on the weight the SECOND report
    states?

No planner. No placement. No harvest. No registry lookup beyond the batch's own
calibration. If the model is right, the answer is ~0%; if it is not, the miss is
the growth curve's, with nothing else to blame.

WHAT IS COMPARED
----------------
Mean weight only, and only for batches present in BOTH reports. Count is
ignored on purpose: fish leave between reports (harvest, cull, transfer) and
that says nothing about how fast the survivors grew.

The observed weight is count-weighted across the batch's tanks, which is what
the reports state and what `sgr_pct_per_day` is defined on.

CAVEAT worth reading before believing a number: mean weight moves when the
POPULATION changes, not only when fish grow. A partial harvest takes the
biggest fish and drags the survivors' mean DOWN, which reads here as the model
over-predicting. Rows where the count fell materially are flagged `culled` and
reported separately -- they are the same execution confound the backtest has,
and no amount of curve work will fix them.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import openpyxl                                                   # noqa: E402

from forecast.biology import sgr_pct_per_day                      # noqa: E402
from forecast.config_io import load_config                        # noqa: E402
from forecast.production_report import read_production_report     # noqa: E402
from forecast.scenario_io import load_batches                     # noqa: E402
from forecast.time_grid import iso_week_label                     # noqa: E402

# A batch losing more than this share of its count between two reports was
# harvested or culled, so its mean-weight move is not a clean growth read.
CULL_FRAC = 0.05


def corpus_months(corpus: Path):
    out = []
    for p in sorted(corpus.glob("*.xlsx")):
        try:
            y, m, d = p.stem.split("-")
            out.append((date(int(y), int(m), int(d)), p))
        except ValueError:
            continue
    return out


def observe(path: Path) -> dict:
    """batch_id -> (count, count-weighted mean weight g) from one report."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _closing, og, _fw = read_production_report(wb)
    wb.close()
    agg = defaultdict(lambda: [0.0, 0.0])
    for r in og:
        agg[r.batch_id][0] += r.closing_count
        agg[r.batch_id][1] += r.closing_count * r.closing_avg_wt_g
    return {b: (c, w / c) for b, (c, w) in agg.items() if c > 0}


def project(wt0: float, days: int, batch, tables, start: date) -> float:
    """Compound the model's OWN SW SGR, one day at a time, exactly as the
    engine's daily walker does."""
    wt = wt0
    for i in range(days):
        lbl = iso_week_label(start)
        wt *= 1.0 + sgr_pct_per_day(wt, "SW", batch, tables, lbl) / 100.0
    return wt


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--config-dir", default=str(_ROOT / "config"))
    ap.add_argument("--scenario-dir", default=str(_ROOT / "scenario"))
    args = ap.parse_args()

    cfg = load_config(Path(args.config_dir))
    tables = cfg[1] if isinstance(cfg, tuple) else cfg.tables
    batches = {b.batch_id: b for b in load_batches(Path(args.scenario_dir))}

    months = corpus_months(Path(args.corpus))
    if len(months) < 2:
        print("need at least 2 corpus months")
        return 1

    clean, culled = [], []
    for (d0, p0), (d1, p1) in zip(months, months[1:]):
        a, b = observe(p0), observe(p1)
        days = (d1 - d0).days
        for bid in sorted(set(a) & set(b)):
            c0, w0 = a[bid]
            c1, w1 = b[bid]
            if w0 <= 0 or w1 <= 0 or days <= 0:
                continue
            pred = project(w0, days, batches.get(bid), tables, d0)
            err = 100.0 * (pred - w1) / w1
            row = (str(d0), str(d1), bid, days, w0, w1, pred, err,
                   100.0 * (c1 - c0) / c0 if c0 else 0.0,
                   bid in batches)
            (culled if (c0 - c1) / c0 > CULL_FRAC else clean).append(row)

    print(f"{len(clean)} clean intervals, {len(culled)} excluded "
          f"(count fell >{CULL_FRAC:.0%} — harvest/cull, not growth)\n")
    if not clean:
        print("nothing clean to grade")
        return 1

    errs = [r[7] for r in clean]
    known = [r[7] for r in clean if r[9]]
    unknown = [r[7] for r in clean if not r[9]]
    print(f"{'from':>12}{'to':>12}{'batch':>7}{'d':>5}{'obs g':>9}"
          f"{'->obs g':>9}{'model g':>10}{'err':>9}")
    for r in sorted(clean, key=lambda x: abs(x[7]), reverse=True)[:12]:
        print(f"{r[0]:>12}{r[1]:>12}{r[2]:>7}{r[3]:>5}{r[4]:>9.0f}"
              f"{r[5]:>9.0f}{r[6]:>10.0f}{r[7]:>8.1f}%")
    print(f"\nMEDIAN growth-curve error: {statistics.median(errs):+.1f}%  "
          f"(typical |err| {statistics.median([abs(x) for x in errs]):.1f}%, "
          f"n={len(errs)})")
    if known:
        print(f"   batches WITH a real calibration : {statistics.median(known):+.1f}%  (n={len(known)})")
    if unknown:
        print(f"   batches WITHOUT one (sgr=1.0)   : {statistics.median(unknown):+.1f}%  (n={len(unknown)})")
    if culled:
        print(f"   excluded (harvested) would read : "
              f"{statistics.median([r[7] for r in culled]):+.1f}%  (n={len(culled)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
