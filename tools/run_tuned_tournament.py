"""Headless TUNED TOURNAMENT — tune every method, then compare the tuned methods.

The command-line twin of the app's "Analyze -> Tuned tournament" depth: for each
roster method it runs the stock leg, checks the HARD gates (conservation,
never-an-empty-week), then follows forecast.tournament's per-method plan —
full search (the method's own knob space) for gate-passers, the cheap one-knob
probe for gate-failers ('gate-bound' when no knob fixes it), 'stock-only' for
methods with no tunable knobs (the Global family — see forecast/methods.py for
the evidence). Each tuned winner gets a verification run on its OWN engine and
joins the final ranking as "METHOD (tuned: knobs)".

Usage:
    python -m tools.run_tuned_tournament --workbook "7.29.26 PR.xlsx" \
        --methods controller,controller-hybrid,controller-lns,global-lp \
        --out-dir out_tournament [--emphasis "Walk the line"] [--max-workers 8]

Writes per-run workbooks + tournament_summary.json into --out-dir and prints
the ranked result table. Touches no production file.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import pickle
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml                                          # noqa: E402

from forecast import analysis as _ana                # noqa: E402
from forecast import methods as _methods             # noqa: E402
from forecast import optimize as _opt                # noqa: E402
from forecast import tournament as _tour             # noqa: E402
from tools.run_compare import (                      # noqa: E402
    _conservation_verdict, _harvest_extras)


class _FileCache(dict):
    """Write-through pickle dict — each finished search variant survives a
    crash / rerun (the CLI twin of the app's _WriteThroughCache)."""

    def __init__(self, path: Path):
        self._path = path
        try:
            with path.open("rb") as fh:
                super().__init__(pickle.load(fh))
        except Exception:                            # noqa: BLE001
            super().__init__()

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        try:
            tmp = self._path.with_suffix(".tmp")
            with tmp.open("wb") as fh:
                pickle.dump(dict(self), fh, protocol=pickle.HIGHEST_PROTOCOL)
            tmp.replace(self._path)
        except Exception:                            # noqa: BLE001
            pass


def _grade(out_path, cfg: dict, targets) -> dict:
    """Headless mirror of the app's per-candidate grading: metrics + each
    method's OWN conservation proof + harvest extras -> the analysis gate
    checklist. Returns {metrics, verdict, harvest, gates, ctx}."""
    hv_cap = float(cfg.get("max_harvest_per_week", 55000) or 55000)
    hv_tgt = float(cfg.get("harvest_target_per_week", 0) or 0) or None
    min_hv = float(cfg.get("min_harvest_per_week", 0) or 0)
    welfare = float(cfg.get("density_welfare_threshold_kg_m3", 80) or 80)
    mv_cap = int(float(cfg.get("max_transfers_per_week", 15) or 0)) or None
    m, _d, _o = _opt.metrics_from_workbook(str(out_path), hv_cap,
                                           welfare_density=welfare,
                                           harvest_target=hv_tgt,
                                           move_cap=mv_cap)
    verdict = _conservation_verdict(str(out_path))
    harv = _harvest_extras(str(out_path), min_hv)
    tr = None
    if targets:
        rows = _ana.harvest_rows(str(out_path))
        monthly, yearly = _ana.harvest_by_period(
            rows, basis=targets.get("basis", "hog"))
        tr = _ana.review_targets(monthly, yearly, targets)
    try:
        sixn_out = _ana.sixn_outbound_transfers(
            str(out_path), str(cfg.get("sixn_production_start") or ""))
    except Exception:                                # noqa: BLE001
        sixn_out = None
    peak_pct = (m.overall_peak_biomass / m.biomass_cap * 100.0
                if m.biomass_cap else None)
    ctx = {
        "dropped": verdict["dropped"], "overprod": verdict["overprod"],
        "zero_weeks": harv.get("zero_weeks"),
        "weeks_over_harvest_cap": m.weeks_over_harvest_cap,
        "weeks_over_harvest_target": m.weeks_over_harvest_target,
        "sixn_outbound_purge": sixn_out,
        "weeks_moves_over_cap": m.weeks_moves_over_cap,
        "weeks_moves_warn": m.weeks_moves_warn,
        "moves_week_max": m.moves_week_max,
        "peak_pct_of_cap": peak_pct,
        "targets_review": tr,
        "density_review": _ana.density_review(str(out_path)),
    }
    return {"metrics": m, "verdict": verdict, "harvest": harv,
            "gates": _ana.evaluate_gates(ctx), "targets_review": tr}


def _gate_str(gates) -> str:
    icon = {"PASS": "P", "WARN": "w", "FAIL": "F", "N/A": "-"}
    return "".join(icon.get(g["status"], "?") for g in gates)


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--workbook", required=True)
    ap.add_argument("--config-dir", default=str(ROOT / "config"))
    ap.add_argument("--scenario-dir", default=str(ROOT / "scenario"))
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--methods", default=None,
                    help="comma-separated method keys (default: full roster)")
    ap.add_argument("--emphasis", default=_opt.DEFAULT_EMPHASIS)
    ap.add_argument("--max-workers", type=int, default=None)
    ap.add_argument("--max-rounds", type=int, default=3)
    args = ap.parse_args(argv)

    wb = Path(args.workbook)
    out_dir = Path(args.out_dir) if args.out_dir else wb.with_name(
        f"{wb.stem}_tournament")
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(Path(args.config_dir) / "control.yaml") as f:
        cfg = yaml.safe_load(f) or {}
    targets = _ana.load_targets(args.config_dir)
    weights = _opt.weights_for(args.emphasis)
    roster = _methods.get_roster(
        [k.strip() for k in args.methods.split(",")] if args.methods else None)
    inputs_sig = hashlib.md5(wb.read_bytes()).hexdigest()[:12]

    print(f"TUNED TOURNAMENT — {len(roster)} method(s) on {wb.name} "
          f"(emphasis: {args.emphasis})")
    for m in roster:
        b = _tour.estimate_budget(m, args.max_rounds)
        print(f"  {m.key:20s} budget: stock 1"
              f" | probe(if gate fails) {b['probe_if_gate_fails']}"
              f" | grid {b['grid']} + descent<= {b['descent_max']}"
              f" | verify {b['verify']}")
    print()

    candidates = []            # rows for the final ranked board
    summary = {"workbook": wb.name, "emphasis": args.emphasis, "methods": {}}

    for m in roster:
        print(f"== {m.key} — stock run")
        stock_out = out_dir / f"{m.key}__stock.xlsx"
        t0 = time.time()
        rc, elapsed = _methods.run_method(m, wb, stock_out,
                                          args.config_dir, args.scenario_dir)
        actual = stock_out if stock_out.exists() else next(
            iter(out_dir.glob(stock_out.stem + ".*")), None)
        if rc != 0 or actual is None:
            print(f"   FAILED rc={rc} — excluded from the board")
            summary["methods"][m.key] = {"status": "run-failed", "rc": rc}
            continue
        g = _grade(actual, cfg, targets)
        fails = _tour.hard_gate_fails(g["gates"])
        print(f"   gates [{_gate_str(g['gates'])}] hard-fails: {fails or 'none'}"
              f"  ({elapsed:.0f}s)")
        candidates.append({"key": m.key, "label": m.label, "overrides": {},
                           "grade": g, "out": str(actual)})

        vc = _FileCache(out_dir / f"vc_{m.key}_{inputs_sig}.pkl")
        pre_keys = set(vc.keys())
        reuse = _tour.cached_count(vc, _tour.search_grid(m))
        if m.knob_space:
            print(f"   search space: {len(m.knob_grid)} grid rows "
                  f"({reuse} cached) + {len(m.knob_space)} descent axes")

        def _prog(i, n, label, _k=m.key):
            print(f"   [{_k}] {i}{'/' + str(n) if n else ''} {label}",
                  flush=True)

        tr = _tour.tune_method(
            m, wb, args.config_dir, args.scenario_dir,
            emphasis=args.emphasis, weights=weights, stock_hard_fails=fails,
            progress=_prog, max_workers=args.max_workers, variant_cache=vc,
            max_rounds=args.max_rounds)
        n_reused = sum(1 for v in tr["variants"]
                       if _opt._overrides_key(v.overrides) in pre_keys)
        msum = {"status": tr["status"], "plan": tr["plan"],
                "stock_hard_fails": fails,
                "n_variants": len(tr["variants"]), "n_cache_reused": n_reused,
                "winner_overrides": tr["winner_overrides"],
                "stock_gates": {x["key"]: x["status"] for x in g["gates"]}}
        summary["methods"][m.key] = msum
        print(f"   -> {tr['status']}"
              + (f", winner {tr['winner_overrides']}"
                 if tr["winner_overrides"] else "")
              + (f" ({n_reused} of {len(tr['variants'])} variants from cache)"
                 if tr["variants"] else ""))

        if tr["status"] != "tuned":
            continue
        winner = dict(tr["winner_overrides"])
        if winner == dict(m.overrides):
            print("   tuned winner == stock config — the stock leg IS the "
                  "tuned candidate")
            msum["tuned_is_stock"] = True
            continue
        # Verification run: the method's OWN engine + the winning overrides.
        print(f"   verification run — {m.key} + winner knobs")
        tuned_m = dataclasses.replace(m, overrides=winner)
        tuned_out = out_dir / f"{m.key}__tuned.xlsx"
        rc2, el2 = _methods.run_method(tuned_m, wb, tuned_out,
                                       args.config_dir, args.scenario_dir)
        actual2 = tuned_out if tuned_out.exists() else next(
            iter(out_dir.glob(tuned_out.stem + ".*")), None)
        if rc2 != 0 or actual2 is None:
            print(f"   verification FAILED rc={rc2} — competing at stock only")
            msum["verify"] = "failed"
            continue
        g2 = _grade(actual2, cfg, targets)
        lbl = _tour.tuned_label(m.label, winner, m.overrides)
        print(f"   gates [{_gate_str(g2['gates'])}]  ({el2:.0f}s)  -> {lbl}")
        msum["verify"] = "ok"
        msum["verify_gates"] = {x["key"]: x["status"] for x in g2["gates"]}
        candidates.append({"key": m.key, "label": lbl, "overrides": winner,
                           "grade": g2, "out": str(actual2)})
        print(f"   {time.time() - t0:.0f}s total for {m.key}\n")

    if not candidates:
        print("No candidate survived — nothing to rank.")
        return 1

    # ---- Rank: same scorer + pick order as the app's Analyze board ----
    variants = [_opt.OptVariant(label=c["label"], overrides=c["overrides"],
                                metrics=c["grade"]["metrics"],
                                dropped=0, overprod=0)
                for c in candidates]
    _opt.score_variants(variants, weights)
    by_label = {v.label: v.score for v in variants}
    rows = []
    for c in candidates:
        rows.append({
            "label": c["label"], "key": c["key"],
            "overrides": c["overrides"],
            "gates": {x["key"]: x["status"] for x in c["grade"]["gates"]},
            "gates_str": _gate_str(c["grade"]["gates"]),
            "score": by_label.get(c["label"]),
            "targets_review": c["grade"]["targets_review"],
            "workbook": c["out"],
            # rank_key inputs
            "gates_list": c["grade"]["gates"],
        })
    ranked = sorted(rows, key=lambda r: _ana.rank_key(
        {"gates": r["gates_list"], "targets_review": r["targets_review"],
         "score": r["score"]}))
    gate_names = " · ".join(g["label"] for g in candidates[0]["grade"]["gates"])
    print("\n==== FINAL RANKING (hard rules -> soft rules -> shortfall -> "
          f"score; gate order: {gate_names})")
    for i, r in enumerate(ranked, 1):
        ov = (" | " + ", ".join(f"{k}={v}" for k, v in r["overrides"].items())
              if r["overrides"] else "")
        print(f"  {i}. [{r['gates_str']}] score="
              f"{r['score']:.3f}  {r['label']}{ov}")

    summary["ranking"] = [{k: r[k] for k in
                           ("label", "key", "overrides", "gates", "score")}
                          for r in ranked]
    winner = ranked[0]
    summary["winner"] = {"label": winner["label"], "key": winner["key"],
                         "overrides": winner["overrides"]}
    (out_dir / "tournament_summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"\nWinner: {winner['label']}\nSummary -> "
          f"{out_dir / 'tournament_summary.json'}")
    print("Promote it in the app (Analyze -> Promote) or via "
          "analysis.save_promoted_default(method, overrides).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
