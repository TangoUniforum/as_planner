"""Noise floor for the contract metrics — how much do they move on their own?

Six "obviously correct" fixes to the harvest controller measured WORSE this
session, and one of them (a 0.1% change to a single week's move-in) swung the
worst harvest week by 12x. That is the signature of a plan that is not smoothly
responsive to its inputs: tank selection and make-room dumps amplify a small
nudge into a materially different plan.

The consequence is methodological, and it applies to every future change: a
single before/after run on one PR cannot distinguish "this fix works" from
"this fix happened to land well". You need to know the SPREAD first.

This runs the same PR through a set of deliberately tiny, neutral perturbations
of knobs that should barely matter, and reports the distribution of the metrics
we actually hold the plan to. The resulting spread is the NOISE FLOOR: an
improvement smaller than it is not evidence.

    python -m tools.robustness <PR.xlsm> [--config-dir config] [--out report.txt]

Deliberately NOT a pass/fail gate — it is a measuring instrument. Nothing here
touches the caller's config: every variant runs in its own temp copy via
forecast.methods.run_method.
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from forecast import methods as _methods  # noqa: E402
from forecast import optimize as _opt  # noqa: E402
from tools.run_compare import _conservation_verdict, _harvest_extras  # noqa: E402


# Perturbations that SHOULD be close to neutral. None of these changes what the
# facility can physically do; they nudge how the planner packs and paces. If the
# contract metrics swing wildly across them, the swing is the plan's own
# sensitivity, not a property of the change under test.
PERTURBATIONS = [
    ("baseline",              {}),
    ("density_target -0.01",  {"density_target_pct": 0.84}),
    ("density_target +0.01",  {"density_target_pct": 0.86}),
    ("deviation -0.002",      {"facility_biomass_deviation_pct": 0.008}),
    ("deviation +0.002",      {"facility_biomass_deviation_pct": 0.012}),
    ("smooth K=5",            {"harvest_smooth_lookahead_weeks": 5}),
    ("smooth K=7",            {"harvest_smooth_lookahead_weeks": 7}),
    ("balance budget 28",     {"rebalance_balance_budget": 28}),
    ("balance budget 32",     {"rebalance_balance_budget": 32}),
]

METRICS = [
    ("weeks_below_floor", "lower better"),
    ("worst_week",        "higher better"),
    ("weeks_over_cap",    "lower better"),
    ("peak_pct_of_cap",   "lower better"),
    ("peak_density",      "lower better"),
    ("transfers_per_fish", "lower better"),
]


def _measure(out_path, control: dict) -> dict:
    hv = float(control.get("max_harvest_per_week") or 55000)
    mn = float(control.get("min_harvest_per_week") or 0)
    wd = float(control.get("density_welfare_threshold_kg_m3") or 80)
    m, dropped, overprod = _opt.metrics_from_workbook(out_path, hv,
                                                      welfare_density=wd)
    h = _harvest_extras(out_path, mn)
    v = _conservation_verdict(out_path)
    return {
        "conserves": v.get("gate") != "FAIL" and dropped == 0 and overprod == 0,
        "weeks_below_floor": float(h.get("weeks_below_min") or 0),
        "zero_weeks": float(h.get("zero_weeks") or 0),
        "worst_week": float(h.get("min_week") or 0),
        "weeks_over_cap": float(m.weeks_over_harvest_cap),
        "peak_pct_of_cap": m.overall_peak_biomass / (m.biomass_cap or 1) * 100.0,
        "peak_density": float(m.density_peak),
        "transfers_per_fish": float(m.transfers_per_fish),
    }


def run(pr_path: str, config_dir: str, scenario_dir: str,
        method_key: str = "controller") -> dict:
    control = yaml.safe_load(
        (Path(config_dir) / "control.yaml").read_text()) or {}
    base = _methods.REGISTRY[method_key]
    work = Path(tempfile.mkdtemp(prefix="as_robust_"))
    rows: dict[str, dict] = {}
    for label, ov in PERTURBATIONS:
        merged = dict(base.overrides)
        merged.update(ov)
        _safe = "".join(c if c.isalnum() else "_" for c in label)
        meth = _methods.Method(key=f"robust_{_safe}", label=label,
                               family=base.family, engine=base.engine,
                               overrides=merged,
                               engine_kwargs=dict(base.engine_kwargs),
                               blurb="")
        out = work / f"{label.replace(' ', '_').replace('/', '_')}.xlsm"
        print(f"  running {label} …", flush=True)
        rc, elapsed = _methods.run_method(meth, pr_path, str(out), config_dir,
                                          scenario_dir, quiet=True)
        if rc == 0 and out.exists():
            rows[label] = _measure(str(out), control)
            rows[label]["secs"] = elapsed
        else:
            print(f"    FAILED rc={rc}", flush=True)
    return rows


def report(rows: dict) -> str:
    if not rows:
        return "no runs completed"
    out = []
    out.append(f"{len(rows)} runs — same PR, same facility, neutral knob nudges\n")
    hdr = f"{'perturbation':22}" + "".join(f"{n[:11]:>13}" for n, _ in METRICS)
    out.append(hdr)
    out.append("-" * len(hdr))
    for label, r in rows.items():
        line = f"{label:22}"
        for name, _ in METRICS:
            v = r[name]
            line += f"{v:>13,.1f}" if name != "transfers_per_fish" else f"{v:>13.2f}"
        out.append(line)

    out.append("\nNOISE FLOOR (spread across perturbations that should barely matter):")
    for name, sense in METRICS:
        vals = [r[name] for r in rows.values()]
        lo, hi = min(vals), max(vals)
        med = statistics.median(vals)
        out.append(f"  {name:20} min {lo:>10,.2f}  median {med:>10,.2f}  "
                   f"max {hi:>10,.2f}   SPREAD {hi - lo:>10,.2f}   ({sense})")

    bad = [l for l, r in rows.items() if not r["conserves"]]
    out.append(f"\n  conservation: {'ALL PASS' if not bad else 'FAILED: ' + ', '.join(bad)}")
    out.append(
        "\nHOW TO USE THIS: the SPREAD column is how much each metric moves when\n"
        "nothing meaningful changed. A candidate fix that improves a metric by\n"
        "less than its spread has not been shown to work — it is inside the\n"
        "noise. Re-measure it across these same perturbations and compare\n"
        "distributions, not single runs.")
    return "\n".join(out)


def run_multi_pr(pr_paths, config_dir, scenario_dir, method_key="controller"):
    """The SECOND axis: the same method+config across several real PRs.

    Perturbation spread says how twitchy the plan is; PR spread says how much
    the answer depends on which week you happened to run. A fix has to beat
    both to be believable.
    """
    control = yaml.safe_load(
        (Path(config_dir) / "control.yaml").read_text()) or {}
    base = _methods.REGISTRY[method_key]
    work = Path(tempfile.mkdtemp(prefix="as_multipr_"))
    rows: dict[str, dict] = {}
    for p in pr_paths:
        name = Path(p).stem
        out = work / f"{''.join(c if c.isalnum() else '_' for c in name)}.xlsm"
        print(f"  running {name} …", flush=True)
        rc, elapsed = _methods.run_method(base, str(p), str(out), config_dir,
                                          scenario_dir, quiet=True)
        if rc == 0 and out.exists():
            rows[name] = _measure(str(out), control)
            rows[name]["secs"] = elapsed
        else:
            print(f"    FAILED rc={rc}", flush=True)
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("workbook", nargs="+",
                    help="one PR = perturbation sweep (noise floor); "
                         "several PRs = across-PR spread for one method")
    ap.add_argument("--config-dir", default="config")
    ap.add_argument("--scenario-dir", default="scenario")
    ap.add_argument("--method", default="controller",
                    help=f"registry key (default controller); available: "
                         f"{', '.join(_methods.REGISTRY)}")
    ap.add_argument("--out", default=None, help="also write the report here")
    a = ap.parse_args(argv)

    if len(a.workbook) == 1:
        rows = run(a.workbook[0], a.config_dir, a.scenario_dir, a.method)
        text = report(rows)
    else:
        rows = run_multi_pr(a.workbook, a.config_dir, a.scenario_dir, a.method)
        text = report(rows).replace(
            "same PR, same facility, neutral knob nudges",
            f"across {len(rows)} real PRs, identical config + method")

    print("\n" + text)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"\nwritten: {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
