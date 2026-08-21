"""Build a FORECAST ERROR MODEL from the backtest, by horizon.

    python tools/error_model.py --results backtest/backtest_results.jsonl \
                                --out backtest/error_model.json

WHAT THIS IS FOR
----------------
A production plan is rarely asked "what is the number". It is asked "how much
should I trust it". This turns the operator's OWN history -- 21 monthly
Production Reports, each used to run the model forward and grade it against
what actually happened -- into the answer: how wrong has this model been, one
month out, three months out, six months out.

That measured distribution is also the honest input to anything stochastic.
A scenario tree built on an ASSUMED spread tells you what you assumed; one
built on this tells you what your facility has actually done.

WHAT IS MEASURED, AND WHAT IS DELIBERATELY NOT
----------------------------------------------
MEAN WEIGHT per batch is the score, because it is the only clean read on the
BIOLOGY. Count and biomass move when a batch is harvested, culled or split
differently from plan -- that is execution, not model error, and mixing the two
produces a number nobody can act on.

Even mean weight is confounded when the model harvested the batch over the
interval: a partial harvest takes the BIGGEST fish, so the survivors' mean
weight falls for reasons unrelated to growth. Those batches are excluded
(`exec_confounded`), and the count of exclusions is reported so the filtering
is visible rather than silent.

HORIZON IS THE AXIS THAT MATTERS. An error only means something next to the
time it accumulated over: 2% at one month and 2% at six months are different
statements about the model.

READ THE n BEFORE THE NUMBER. With 21 corpus months the longer horizons have
few comparisons, and a spread computed from three samples is not a
distribution. Horizons below MIN_N are reported but flagged `weak`.
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

# Below this many graded comparisons a horizon's spread is not worth quoting.
MIN_N = 5


def _pct(sorted_vals, q):
    """Simple empirical percentile (no interpolation games on tiny samples)."""
    if not sorted_vals:
        return None
    i = int(round(q * (len(sorted_vals) - 1)))
    return sorted_vals[max(0, min(i, len(sorted_vals) - 1))]


def build(records: list) -> dict:
    """Per-horizon error distribution from backtest result records."""
    by_h: dict[int, list] = defaultdict(list)
    n_conf = 0
    n_used = 0
    for rec in records:
        if rec.get("mode") != "backtest":
            continue
        h = rec.get("horizon_months")
        if h is None:
            continue
        for b in rec.get("batches") or []:
            if b.get("present") != "both":
                continue
            if b.get("exec_confounded"):
                n_conf += 1
                continue
            p, a = b.get("pred_wt_g"), b.get("act_wt_g")
            if not p or not a or a <= 0:
                continue
            by_h[h].append(100.0 * (p - a) / a)
            n_used += 1

    horizons = {}
    for h in sorted(by_h):
        v = sorted(by_h[h])
        horizons[str(h)] = {
            "n": len(v),
            "weak": len(v) < MIN_N,
            "median_signed_pct": round(statistics.median(v), 3),
            "typical_abs_pct": round(statistics.median([abs(x) for x in v]), 3),
            "p10_pct": round(_pct(v, 0.10), 3),
            "p90_pct": round(_pct(v, 0.90), 3),
            "worst_abs_pct": round(max(abs(x) for x in v), 3),
        }
    return {
        "source": "backtest (mode=backtest), mean weight per batch, "
                  "execution-confounded batches excluded",
        "batches_used": n_used,
        "batches_excluded_exec_confounded": n_conf,
        "min_n_for_confidence": MIN_N,
        "horizons_months": horizons,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--results", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    recs = []
    with open(args.results, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue          # a torn line is not worth aborting on
    if not recs:
        print(f"no records in {args.results}")
        return 1

    model = build(recs)
    h = model["horizons_months"]
    print(f"{len(recs)} graded comparisons; {model['batches_used']} batch reads "
          f"used, {model['batches_excluded_exec_confounded']} excluded as "
          f"execution-confounded\n")
    print(f"{'horizon':>8}{'n':>6}{'median':>10}{'typical':>10}"
          f"{'p10':>9}{'p90':>9}{'worst':>9}   note")
    for k in sorted(h, key=int):
        r = h[k]
        note = "FEW SAMPLES — do not quote" if r["weak"] else ""
        print(f"{k+'m':>8}{r['n']:>6}{r['median_signed_pct']:>9.1f}%"
              f"{r['typical_abs_pct']:>9.1f}%{r['p10_pct']:>8.1f}%"
              f"{r['p90_pct']:>8.1f}%{r['worst_abs_pct']:>8.1f}%   {note}")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(model, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
