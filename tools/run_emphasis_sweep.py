"""How much does the WINNER depend on which objective you chose?

    python -m tools.run_emphasis_sweep --tournament-dir out_tournament

WHY THIS EXISTS
---------------
A tournament searches METHODS and KNOBS under ONE emphasis. But the emphasis is
itself a choice -- `Walk the line` weights flatness 3 and handling 0.5, while
`Minimize handling` inverts that. Two objectives, two different "best" plans,
and nothing in the current flow shows how far apart they are.

That matters because the real trade-offs in this facility are BETWEEN
objectives, not within one: density against handling, biomass against the
contract floor. A sweep over emphases explores that space; a sweep over knobs
cannot.

IT COSTS ALMOST NOTHING
-----------------------
`optimize.score_variants` is a pure function of a variant's METRICS and the
emphasis WEIGHTS -- running the plan and scoring it are separate steps. The
tuned tournament already caches every variant it ran (vc_*.pkl, ~59 per
method) WITH its full metrics. So this re-scores work already done rather than
re-running it: a sweep that would cost 7 tournaments costs one, plus a second.

READING IT
----------
    * A method that wins under EVERY emphasis is robust -- the choice of
      objective is not what is driving the result.
    * A winner that changes with the emphasis is telling you the objective IS
      the decision, and it belongs to the operator, not the tuner.
    * The knobs matter as much as the method: the same arm can win under two
      emphases with different budgets, which is the trade made visible.

CAVEAT: the tuner SEARCHED under one emphasis, so the variant pool is biased
toward it. A re-score says "of the plans we generated, which would win under
objective X" -- not "what is the best plan for X". A pool generated under
`Minimize handling` might contain a better handling plan than any here. Treat
this as sensitivity analysis, not a substitute for tuning under the objective
you actually want.
"""
from __future__ import annotations

import argparse
import glob
import os
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forecast import optimize as _opt                           # noqa: E402


class _M:
    """Adapter: cached metrics are a plain dict; score_variants wants
    .component(name)."""

    def __init__(self, d):
        self._d = d

    def component(self, name):
        try:
            return float(self._d.get(name, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0


class _V:
    """Minimal stand-in for a tournament variant, enough for score_variants."""

    def __init__(self, method, overrides, metrics, ok):
        self.method = method
        self.overrides = overrides or {}
        self.metrics = _M(metrics)
        self.conservation_ok = ok
        self.norm = {}
        self.score = 0.0

    def label(self):
        if not self.overrides:
            return f"{self.method} (stock)"
        pk = ", ".join(f"{k}={v}" for k, v in sorted(self.overrides.items()))
        return f"{self.method} ({pk})"


def load_variants(tdir: str) -> list:
    out = []
    for f in sorted(glob.glob(os.path.join(tdir, "vc_*.pkl"))):
        base = os.path.basename(f)
        method = base[3:].rsplit("_", 1)[0]          # vc_<method>_<hash>.pkl
        try:
            cache = pickle.load(open(f, "rb"))
        except Exception as e:                        # noqa: BLE001
            print(f"  !! could not read {base}: {e}")
            continue
        for _key, rec in cache.items():
            if not isinstance(rec, dict) or "metrics" not in rec:
                continue
            # A variant the tournament rejected is kept out of normalisation,
            # exactly as score_variants does for conservation failures.
            ok = not rec.get("failed") and not rec.get("dropped")
            out.append(_V(method, rec.get("overrides"), rec["metrics"], ok))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tournament-dir", required=True,
                    help="a tuned-tournament --out-dir (needs its vc_*.pkl caches)")
    ap.add_argument("--top", type=int, default=3, help="rows to show per emphasis")
    args = ap.parse_args()

    variants = load_variants(args.tournament_dir)
    if not variants:
        print("no cached variants found — was this a tuned tournament out-dir?")
        return 1
    ok = sum(1 for v in variants if v.conservation_ok)
    methods = sorted({v.method for v in variants})
    print(f"loaded {len(variants)} cached variants ({ok} scoreable) "
          f"across {len(methods)} method(s): {', '.join(methods)}\n")

    winners = {}
    for name in _opt.EMPHASIS_PRESETS:
        weights = _opt.emphasis_weights(name) if hasattr(_opt, "emphasis_weights") \
            else _opt.EMPHASIS_PRESETS[name]
        _opt.score_variants(variants, weights)
        ranked = sorted((v for v in variants if v.conservation_ok),
                        key=lambda v: v.score)
        if not ranked:
            continue
        # SNAPSHOT, not a reference: score_variants mutates .score IN PLACE, so
        # holding the object means the summary below prints whatever the LAST
        # emphasis scored. That bug made "Minimize handling" read 6.698 in its
        # own section and 7.067 in the summary.
        top = ranked[0]
        winners[name] = (top.method, top.score,
                         tuple(sorted(top.overrides.items())),
                         # arms whose score TIES the winner: with the hybrid arm
                         # inert these are the same plan under different names,
                         # and picking one of them is sort order, not merit.
                         sorted({v.method for v in ranked
                                 if abs(v.score - top.score) < 1e-9}))
        print(f"=== {name} ===")
        for v in ranked[:args.top]:
            print(f"    {v.score:>8.3f}  {v.label()[:110]}")
        print()

    print("=" * 78)
    print("WINNER BY EMPHASIS (lower score is better)")
    for name, (meth, sc, _ov, tied) in winners.items():
        note = f"   [TIED with {', '.join(m for m in tied if m != meth)}]" if len(tied) > 1 else ""
        print(f"  {name:>18}: {meth:<20} {sc:>8.3f}{note}")
    distinct_m = {m for m, _s, _o, _t in winners.values()}
    ever_tied = any(len(t) > 1 for _m, _s, _o, t in winners.values())
    print()
    if ever_tied:
        print("  !! TIES PRESENT. Arms that tie exactly are the SAME PLAN under")
        print("     different names -- the controller-hybrid arm ships INERT (its")
        print("     levers are false, so the L1 guide steers nothing), so it is")
        print("     byte-identical to the plain controller. Which of them 'wins' is")
        print("     sort order, not merit. Do not read a tie as agreement.")
        print()
    if len(distinct_m) == 1 and not ever_tied:
        print(f"  ROBUST: {distinct_m.pop()} wins under every emphasis — the objective")
        print("  is not what is driving the choice of METHOD.")
    elif len(distinct_m) > 1:
        print(f"  SENSITIVE: {len(distinct_m)} different methods win depending on the")
        print("  objective. The emphasis IS the decision, and it belongs to the")
        print("  operator, not the tuner.")
    distinct_k = {ov for _m, _s, ov, _t in winners.values()}
    print(f"  distinct winning KNOB SETS across emphases: {len(distinct_k)}")
    print("  (the knobs are where the trade actually shows -- same arm, different")
    print("   budgets, is the objective changing what the plan does)")
    print()
    print("  CAVEAT: the pool was generated by tuning under ONE emphasis, so this")
    print("  says 'of the plans we generated, which wins under X' -- not 'what is")
    print("  the best plan for X'. Sensitivity analysis, not a substitute for")
    print("  tuning under the objective you actually want.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
