"""BACKTEST: run the model from each historical month and grade it against what
actually happened.

    python tools/backtest.py --corpus ../pr_corpus --out ../backtest

WHAT THIS MEASURES
------------------
For each month M in the corpus: hydrate the facility from month M's
ProductionReport, run the forecast forward, and compare its prediction for
month M+k against month M+k's ACTUAL ProductionReport, for every k that fits
the horizon. That yields ERROR VERSUS HORIZON — how wrong the model is one
month out, three months out, six months out — which is the first real
measurement of whether the biology matches the facility, and the input a
stochastic scenario tree needs.

The grading itself is forecast/accuracy.compare; this module only drives it.
Its honesty rules are inherited and worth restating:
  * BATCH level is the biology score. Fish are compared per batch, summed over
    whatever tanks they ended up in, so the operator having placed fish
    differently from the plan is not counted as model error.
  * MEAN WEIGHT is the headline. COUNT and BIOMASS mix model error with
    execution — a batch harvested, culled or split differently moves those
    numbers without the growth model being wrong.
  * SEAWATER ONLY. BatchLocations snapshots OG tanks; freshwater never
    appears there.
  * Harvest execution is NOT measurable: fish already sold are simply absent
    from a PR, so an early harvest looks like a catastrophic count miss.

=============================================================================
ISOLATION — WHY THIS CANNOT CONTAMINATE AN OPERATIONAL RUN
=============================================================================
A backtest is NOT a forecast of the facility. It is a measurement OF the model,
run against dates that have already happened. If its by-products leak into the
stores an operational run reads, every later number is quietly polluted — and
that is not hypothetical: on 2026-08-20 a synthetic fixture put 83 fake records
into the live FW calibration history and they had to be picked out by hand.

So the separation is enforced, not merely intended:

  1. FW CALIBRATION HISTORY. `run.main(..., calib_log_path=...)` is passed
     explicitly on every backtest run — either "" (record nothing) or this
     driver's OWN log under the output directory. It never touches
     fw_calibration_history.jsonl.
  2. CONFIG AND SCENARIO. Each run gets a throwaway COPY. The operator's
     config/ and scenario/ are read once and never written.
  3. OUTPUT WORKBOOKS. Written to a temp dir and discarded (or kept under
     --keep-runs, inside the backtest output dir, never beside a real run).
  4. RESULTS STORE. Everything this driver produces is written to its own
     directory and every record carries `"mode": "backtest"` plus the run and
     target PR dates. There is no shared file, so combining backtest and
     operational data has to be a deliberate act — you would have to open two
     files and join them on purpose.
  5. THE SOURCE REPORTS ARE READ-ONLY. The corpus is a copy; the operator's
     Production Report workbooks are never opened for writing.

If you ever DO want the two together, join on `mode` and say so in whatever
you produce. The one thing this design refuses to allow is the two mixing by
accident.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import openpyxl                                             # noqa: E402

from forecast import accuracy as _acc                       # noqa: E402
from forecast.production_report import read_production_report  # noqa: E402

MODE = "backtest"          # stamped on every record; the join key that keeps
                           # this data distinguishable from operational history


def corpus_months(corpus: Path) -> list[tuple[date, Path]]:
    """(closing_date, path) for every corpus entry, chronologically."""
    out = []
    for p in sorted(corpus.glob("*.xlsx")):
        try:
            y, m, d = p.stem.split("-")
            out.append((date(int(y), int(m), int(d)), p))
        except ValueError:
            continue
    return out


def _months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def run_one(src: Path, work: Path, config_dir: Path, scenario_dir: Path,
            horizon_weeks: int, calib_log: str) -> Path | None:
    """Run the pipeline from one historical PR. Returns the output workbook.

    Every mutable input is a COPY and the calibration log is redirected — see
    the ISOLATION section above.
    """
    from forecast.run import main as run_main

    cfg = work / "config"
    scn = work / "scenario"
    shutil.copytree(config_dir, cfg)
    shutil.copytree(scenario_dir, scn)

    # Horizon is set per backtest so a run cannot silently inherit whatever the
    # operator happens to have configured today.
    ctl = cfg / "control.yaml"
    txt = ctl.read_text(encoding="utf-8")
    lines = []
    for ln in txt.splitlines():
        if ln.startswith("horizon_weeks:"):
            ln = f"horizon_weeks: {horizon_weeks}"
        lines.append(ln)
    ctl.write_text("\n".join(lines) + "\n", encoding="utf-8")

    out = work / "out.xlsx"
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            run_main(str(src), str(out), config_dir=str(cfg),
                     scenario_dir=str(scn), calib_log_path=calib_log)
    except Exception as e:
        return None
    if out.exists():
        return out
    alt = out.with_suffix(".xlsm")
    return alt if alt.exists() else None


def grade(forecast_wb: Path, actual_wb: Path) -> dict | None:
    """accuracy.compare, reduced to the fields worth storing."""
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rep = _acc.compare(str(forecast_wb), str(actual_wb))
        head = _acc.headline(rep)
    except Exception:
        return None
    graded = getattr(rep, "batches", None) or []
    bias = {}
    with contextlib.suppress(Exception):
        bias = _acc.summarize_bias(graded)
    return {
        "headline": head,
        "bias": bias,
        "n_batches": len(graded),
        "batches": [
            {k: getattr(b, k, None) for k in
             ("batch_id", "present", "pred_wt_g", "act_wt_g",
              "pred_count", "act_count", "pred_biomass_kg", "act_biomass_kg")}
            for b in graded
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest the model against history.")
    ap.add_argument("--corpus", required=True, help="dir of normalised monthly PRs")
    ap.add_argument("--out", required=True, help="results dir (backtest-only)")
    ap.add_argument("--config-dir", default=str(_ROOT / "config"))
    ap.add_argument("--scenario-dir", default=str(_ROOT / "scenario"))
    ap.add_argument("--horizon-weeks", type=int, default=30)
    ap.add_argument("--max-horizon-months", type=int, default=6)
    ap.add_argument("--keep-runs", action="store_true",
                    help="keep each run's output workbook under --out/runs")
    ap.add_argument("--record-calibration", action="store_true",
                    help="write FW calibration records to the BACKTEST's own "
                         "log (default: suppress entirely). Never writes to the "
                         "operational history either way.")
    args = ap.parse_args()

    corpus = Path(args.corpus)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    runs_dir = out_dir / "runs"
    if args.keep_runs:
        runs_dir.mkdir(exist_ok=True)

    # Isolation point 1: the calibration log is this driver's own, or nothing.
    calib_log = str(out_dir / "backtest_fw_calibration.jsonl") if args.record_calibration else ""

    months = corpus_months(corpus)
    if len(months) < 2:
        print(f"need at least 2 corpus months, found {len(months)}")
        return 1
    print(f"corpus: {len(months)} months  {months[0][0]} .. {months[-1][0]}")
    print(f"horizon: {args.horizon_weeks} weeks, grading up to "
          f"{args.max_horizon_months} months ahead")
    print(f"FW calibration: {'own log' if args.record_calibration else 'SUPPRESSED'} "
          f"(operational history untouched)\n")

    results_path = out_dir / "backtest_results.jsonl"
    n_runs = n_grades = 0
    stamp = datetime.now().isoformat(timespec="seconds")

    with results_path.open("w", encoding="utf-8") as fh:
        for i, (run_date, src) in enumerate(months):
            targets = [(d, p) for d, p in months[i + 1:]
                       if 0 < _months_between(run_date, d) <= args.max_horizon_months]
            if not targets:
                continue
            with tempfile.TemporaryDirectory() as td:
                produced = run_one(src, Path(td), Path(args.config_dir),
                                   Path(args.scenario_dir), args.horizon_weeks,
                                   calib_log)
                if produced is None:
                    print(f"  {run_date}  RUN FAILED")
                    continue
                n_runs += 1
                kept = None
                if args.keep_runs:
                    kept = runs_dir / f"{run_date}{produced.suffix}"
                    shutil.copy2(produced, kept)
                line = []
                for tgt_date, tgt in targets:
                    g = grade(produced, tgt)
                    if g is None:
                        line.append(f"{_months_between(run_date, tgt_date)}m:--")
                        continue
                    n_grades += 1
                    rec = {
                        "mode": MODE,                    # <- the separation key
                        "generated": stamp,
                        "run_pr": run_date.isoformat(),
                        "target_pr": tgt_date.isoformat(),
                        "horizon_months": _months_between(run_date, tgt_date),
                        "horizon_weeks_configured": args.horizon_weeks,
                        **g,
                    }
                    fh.write(json.dumps(rec) + "\n")
                    hl = g.get("headline") or {}
                    line.append(f"{rec['horizon_months']}m:"
                                f"{hl.get('signed_median_pct', '?'):.1f}%"
                                if isinstance(hl.get('signed_median_pct'), (int, float))
                                else f"{rec['horizon_months']}m:?")
                print(f"  {run_date}  graded {len(targets)}  " + "  ".join(str(x) for x in line))

    print(f"\n{n_runs} runs, {n_grades} graded comparisons -> {results_path}")
    print(f"every record carries \"mode\": \"{MODE}\" — join on it deliberately, "
          f"never by accident")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
