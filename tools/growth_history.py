"""Growth history + actual growth model, from the command line.

    python -m tools.growth_history --store growth_history.json backfill --since 2024-11-01 DIR [DIR ...]
    python -m tools.growth_history --store growth_history.json add PR.xlsx [PR.xlsx ...]
    python -m tools.growth_history --store growth_history.json show
    python -m tools.growth_history --store growth_history.json fit [--as-of DATE] [--window 24] [--dry-run]

--store is required (no default), so nothing writes the operator's history by
accident. `fit` saves a new FROZEN version; --dry-run prints it and saves
nothing. Nothing here touches config/ or scenario/: a version reaches a plan
only when it is copied into biology.yaml sgr_sw_by_model and a batch picks it.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from forecast import growth_history as gh                          # noqa: E402
from forecast.biology import _interp                               # noqa: E402
from forecast.config_io import load_config                         # noqa: E402


def _report_files(dirs):
    for d in dirs:
        for p in sorted(Path(d).rglob("*")):
            if p.is_file() and gh.is_report_name(p.name):
                yield p


def cmd_add(store, paths, since=None):
    added = dup = old = 0
    bad = []
    for p in paths:
        try:
            s = gh.read_snapshot(p)
        except ValueError as e:
            bad.append(str(e))
            continue
        if since and s.closing < since:
            old += 1
            continue
        r = store.add(s)
        added += r == "added"
        dup += r == "duplicate"
        print(f"  {r:<9} {s.closing} {s.kind:<9} {len(s.og):>3} tank rows  {p.name}")
    store.save()
    print(f"\n{added} added, {dup} already in, {old} before {since}, {len(bad)} unreadable")
    for b in bad:
        print("  UNREADABLE", b)


def cmd_show(store, facility):
    chain, sup = store.chain()
    sixn, og = gh.facility_tank_sets(facility)
    ivs = gh.clean_intervals(chain, sixn, og)
    print(f"{len(store.snapshots)} reports stored, {len(chain)} in the chain:")
    for s in chain:
        print(f"  {s.closing}  {s.kind:<9} {s.source}")
    if sup:
        print("not used:")
        for s, why in sorted(sup, key=lambda x: x[0].closing):
            print(f"  {s.closing}  {s.source}  ({why})")
    print(f"{len(ivs)} clean tank intervals ({sum(i.provisional for i in ivs)} provisional)")
    for f in store.fits:
        print(f"version {f.name!r}: as of {f.as_of}, window from {f.window_start}, "
              f"n={f.n}, cap {f.cap_g_day:.1f} g/day above {f.breakpoint_g:,.0f} g")


def cmd_fit(store, facility, tables, as_of, window, name, dry_run):
    chain, _ = store.chain()
    if not chain:
        print("no reports in the history")
        return 1
    as_of = as_of or chain[-1].closing
    sixn, og = gh.facility_tank_sets(facility)
    ivs = gh.clean_intervals(chain, sixn, og)
    fit = gh.fit_actual(ivs, sizes=tables.sgr_size_g, base_sw=tables.sgr_sw_pct_day,
                        as_of=as_of, window_months=window, name=name,
                        sources=[s.source for s in chain
                                 if s.closing > gh._months_before(
                                     date.fromisoformat(as_of), window).isoformat()
                                 and s.closing <= as_of])
    print(f"{fit.name}: as of {fit.as_of}, window {fit.window_start} .. {fit.as_of} "
          f"({fit.window_months} months)")
    print(f"  {fit.n} clean tank intervals ({fit.n_provisional} provisional), "
          f"{fit.n_below} below / {fit.n_above} above {fit.breakpoint_g:,.0f} g")
    print(f"  AGR = {fit.a:.4f} x w^{fit.b:.3f} g/day, capped at {fit.cap_g_day:.1f} g/day")
    print(f"  {'size':>6} {'base %/d':>9} {'fit %/d':>8} {'base g/d':>9} {'fit g/d':>8}")
    for w in (500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000):
        b = _interp(w, tables.sgr_size_g, tables.sgr_sw_pct_day)
        f = _interp(w, fit.sizes_g, fit.sgr_pct_day)
        print(f"  {w:>6} {b:>9.3f} {f:>8.3f} {w * b / 100:>9.1f} {w * f / 100:>8.1f}")
    if fit.holdout:
        h = fit.holdout
        print(f"  holdout (last {gh.HOLDOUT_MONTHS} months, {h['n']} intervals, end weight): "
              f"fit {h['fit']['median_err_pct']:+.1f}% (typ {h['fit']['median_abs_err_pct']:.1f}%), "
              f"base curve {h['base']['median_err_pct']:+.1f}% (typ {h['base']['median_abs_err_pct']:.1f}%)")
    if dry_run:
        print("  dry run - not saved")
        return 0
    store.add_fit(fit)
    store.save()
    print(f"  saved as frozen version {fit.name!r}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", required=True, help="growth history JSON file")
    ap.add_argument("--config-dir", default=str(_ROOT / "config"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill")
    b.add_argument("--since", default="2024-11-01")
    b.add_argument("dirs", nargs="+")
    a = sub.add_parser("add")
    a.add_argument("files", nargs="+")
    sub.add_parser("show")
    f = sub.add_parser("fit")
    f.add_argument("--as-of")
    f.add_argument("--window", type=int, default=gh.DEFAULT_WINDOW_MONTHS)
    f.add_argument("--name")
    f.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    store = gh.GrowthHistory(args.store)
    _ctrl, tables, facility = load_config(Path(args.config_dir))
    if args.cmd == "backfill":
        cmd_add(store, list(_report_files(args.dirs)), since=args.since)
    elif args.cmd == "add":
        cmd_add(store, [Path(p) for p in args.files])
    elif args.cmd == "show":
        cmd_show(store, facility)
    elif args.cmd == "fit":
        return cmd_fit(store, facility, tables, args.as_of, args.window,
                       args.name, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
