"""Reconstruct the batch registry OF EACH ERA from the PR corpus.

    python tools/reconstruct_registry.py --corpus ../pr_corpus --out ../pr_corpus/registries

WHY
---
A backtest run from month M must be given the batch schedule that existed AT
month M. Handed today's scenario/batches.yaml instead, a 2024 run is told its
first incoming cohort arrives 2025-02-10 — which is exactly the phantom anchor
every historical grading produced, and the reason the first backtest reported a
17% "cold" bias that was an artifact, not a measurement.

The corpus already contains the answer. A batch's seawater entry is simply the
first month it appears in the OG rows of a Production Report, and that PR gives
its count and mean weight on arrival.

WHAT IS RECONSTRUCTED, AND HOW HONESTLY
---------------------------------------
OBSERVED, straight from the corpus — trustworthy:
  batch_id            the PR's own identifier
  tran_og_date        first month the batch appears in OG, dated to that
                      month's closing. Resolution is ONE MONTH: a batch that
                      entered on the 3rd and one that entered on the 28th are
                      indistinguishable here.
  tran_og_count       its OG count in that first month
  tran_og_avg_wt_g    its OG mean weight in that first month

INFERRED, because a PR cannot see them — treat with suspicion:
  input_date          tran_og_date minus the typical egg-to-transfer lead
  tran_sf_date        input_date plus the typical egg-to-start-feed lead
  tran_og_cv          a constant; the PR carries no CV
  fcr_model           a constant; the PR carries no feed model
  fw_correction       1.0 — NOT back-solved. A reconstructed FW leg is a guess,
                      and calibrating it against the transfer weight the same
                      corpus supplied would be fitting to the answer.

The FW leg is therefore reconstruction, not measurement. That is acceptable
because the thing being graded is SEAWATER growth — accuracy.compare reads
BatchLocations, which holds OG tanks only and never sees freshwater. A batch
already in seawater at month M carries its real state from the PR; the inferred
FW dates only matter for cohorts that transfer in DURING the horizon, and those
arrive at the count and weight the corpus observed.

A registry is emitted per corpus month: `<closing>.yaml`, containing every
batch observed at or after that month. Run the backtest for month M against
registry M.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import sys
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import openpyxl                                                # noqa: E402

from forecast.models import BatchInput                         # noqa: E402
from forecast.production_report import read_production_report  # noqa: E402
from forecast.scenario_io import dump_scenario, load_limits    # noqa: E402

# Typical leads, from the operator's own registry: input -> tran_sf is about
# three months, input -> tran_og about nine. Only used for cohorts whose FW leg
# is not observable in the corpus.
LEAD_INPUT_TO_OG_DAYS = 273       # ~9 months
LEAD_INPUT_TO_SF_DAYS = 91        # ~3 months
DEFAULT_CV = 16.0
DEFAULT_FCR_MODEL = "FCR_118_Quick"


def corpus_months(corpus: Path) -> list[tuple[date, Path]]:
    out = []
    for p in sorted(corpus.glob("*.xlsx")):
        try:
            y, m, d = p.stem.split("-")
            out.append((date(int(y), int(m), int(d)), p))
        except ValueError:
            continue
    return out


def observe(corpus: Path) -> dict[str, dict]:
    """First OG appearance of every batch across the corpus."""
    first: dict[str, dict] = {}
    for closing, p in corpus_months(corpus):
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _c, og, _fw = read_production_report(wb)
        wb.close()
        agg: dict[str, list] = {}
        for r in og:
            a = agg.setdefault(r.batch_id, [0.0, 0.0])
            a[0] += r.closing_count
            a[1] += r.closing_count * r.closing_avg_wt_g
        for bid, (cnt, wsum) in agg.items():
            if bid in first or cnt <= 0:
                continue
            first[bid] = {"batch_id": bid, "seen": closing,
                          "count": cnt, "avg_wt_g": wsum / cnt}
    return first


def to_batch(obs: dict) -> BatchInput:
    og_date = obs["seen"]
    input_date = og_date - timedelta(days=LEAD_INPUT_TO_OG_DAYS)
    sf_date = input_date + timedelta(days=LEAD_INPUT_TO_SF_DAYS)
    # Input count is unobservable; the OG count grossed up by a nominal FW
    # survival is the least-bad stand-in and is never used for a batch that is
    # already in seawater at the run's start date.
    return BatchInput(
        obs["batch_id"], input_date, int(round(obs["count"] / 0.9)),
        sf_date, og_date, int(round(obs["count"])), float(obs["avg_wt_g"]),
        DEFAULT_CV, DEFAULT_FCR_MODEL, 1.0, 1.0,
        f"reconstructed from PR corpus; first seen {og_date}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--scenario-dir", default=str(_ROOT / "scenario"),
                    help="source of limits.yaml, copied unchanged into each era")
    args = ap.parse_args()

    corpus = Path(args.corpus)
    out_dir = Path(args.out)
    months = corpus_months(corpus)
    if not months:
        print(f"no corpus months under {corpus}")
        return 1

    first = observe(corpus)
    print(f"observed {len(first)} distinct batches across {len(months)} months")
    for bid in sorted(first, key=lambda b: (first[b]["seen"], b)):
        o = first[bid]
        print(f"   {bid:6} first OG {o['seen']}  {o['count']:>10,.0f} fish "
              f"@ {o['avg_wt_g']:7.1f} g")

    fac_lim, sys_lim = load_limits(Path(args.scenario_dir))

    print()
    for closing, _p in months:
        # Every batch observed at or after this month. A batch that first
        # appears BEFORE it is already hydrated from the PR and does not need a
        # registry entry; including it would re-project a phantom lifecycle.
        era = [to_batch(o) for o in first.values() if o["seen"] >= closing]
        era.sort(key=lambda b: (b.tran_og_date, b.batch_id))
        d = out_dir / closing.isoformat()
        d.mkdir(parents=True, exist_ok=True)
        dump_scenario(d, batches=era, facility_limits=fac_lim, system_limits=sys_lim)
        span = (f"{era[0].tran_og_date:%Y-%m-%d} .. {era[-1].tran_og_date:%Y-%m-%d}"
                if era else "none")
        print(f"  {closing}  {len(era):>2} forward batches  ({span})")

    print(f"\nwrote {len(months)} era registries -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
