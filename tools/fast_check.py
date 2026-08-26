"""The REAL pipeline on a SHORT horizon, with the REAL audits. Minutes, not hours.

    python tools/fast_check.py --workbook <PR.xlsm> --weeks 12

WHY THIS EXISTS
---------------
Every wrong answer on 2026-08-24/25 came from testing against a MODEL of the
pipeline instead of the pipeline. A hand-written move detector reported 0 moves
for a plan containing 56. A hand-written identity check called a stable key
unstable. A "0 moves by construction" claim was wrong by 13. Each proxy encoded
an assumption, and a proxy built on a wrong assumption agrees with whoever wrote
it.

The reason proxies kept getting written is that the only faithful signal cost
3.2 HOURS, so the honest check was never the cheap one. This closes that gap:
same engine, same audits, same invariant checker -- just fewer weeks. It cannot
drift from the real pipeline because it IS the real pipeline.

WHAT IT CANNOT TELL YOU
-----------------------
A short horizon does not reach the 2028 PRODUCTION-mode era, so R4/R3 topology
breaches that only appear there will NOT show up. It proves conservation, 6N
legality and tank-control compliance over the weeks it runs -- nothing about the
weeks it does not. Use it to iterate; use a full run to ship.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

SIXN = {61, 63, 65, 67, 69, 71}


def _short_config(config_dir: Path, weeks: int, dest: Path) -> Path:
    """Copy the real config, overriding ONLY horizon_weeks."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(config_dir, dest)
    ctl = dest / "control.yaml"
    out = []
    for line in ctl.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("horizon_weeks:"):
            out.append(f"horizon_weeks: {weeks}")
        else:
            out.append(line)
    ctl.write_text("\n".join(out) + "\n", encoding="utf-8")
    return dest


def _tid(v):
    try:
        return int(str(v).strip())
    except Exception:
        return None


def audit(xlsx: Path, control) -> dict:
    import openpyxl
    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    out = {}

    # ---- intra-6N moves + tier breaches, from the REAL TransferPlan --------
    rows = list(wb["TransferPlan"].values)
    hdr = [str(c or "").strip() for c in rows[3]]
    ix = {h: i for i, h in enumerate(hdr)}
    n_tr = moves6 = fish6 = 0
    for r in rows[4:]:
        if not r or not r[ix["Week"]] or str(r[ix["Type"]]).strip() != "Transfer":
            continue
        n_tr += 1
        f, t = _tid(r[ix["From_Tank"]]), _tid(r[ix["To_Tank"]])
        if f in SIXN and t in SIXN:
            moves6 += 1
            try:
                fish6 += float(r[ix["Count (fish)"]] or 0)
            except Exception:
                pass
    assert n_tr > 0, "no Transfer rows parsed -- detector broken, not a clean run"
    out["transfer_rows"], out["sixn_moves"], out["sixn_move_fish"] = n_tr, moves6, fish6

    # ---- topology violations, straight from ValidationLog ------------------
    kinds = {}
    for r in wb["ValidationLog"].values:
        line = " ".join(str(c) for c in r if c is not None)
        if "TOPOLOGY VIOLATION" in line:
            for k in ("R1", "R2", "R3", "R4", "R5", "R6", "R7"):
                if f"{k}:" in line:
                    kinds[k] = kinds.get(k, 0) + 1
                    break
    out["topology"] = kinds

    # ---- min_tank_control remnants ----------------------------------------
    mtc = float(getattr(control, "min_tank_control", 0.0) or 0.0)
    blrows = list(wb["BatchLocations"].values)
    h_i = next(i for i, r in enumerate(blrows)
               if r and any(str(c).strip() == "Week" for c in r if c))
    bx = {str(c or "").strip(): i for i, c in enumerate(blrows[h_i])}
    occ = {}
    for r in blrows[h_i + 1:]:
        if r and r[bx["Week"]]:
            occ[(str(r[bx["Week"]]), str(r[bx["Tank"]]).strip())] = float(
                r[bx["Count (fish)"]] or 0)
    hrows = list(wb["HarvestPlan"].values)
    hh = next(i for i, r in enumerate(hrows)
              if r and any(str(c).strip() == "Week" for c in r if c))
    hx = {str(c or "").strip(): i for i, c in enumerate(hrows[hh])}
    bad = draws = 0
    for r in hrows[hh + 1:]:
        if not r or not r[hx["Week"]]:
            continue
        have = occ.get((str(r[hx["Week"]]), str(r[hx["Tank"]]).strip()))
        try:
            got = float(r[hx["Count (fish)"]] or 0)
        except Exception:
            continue
        if not have or got <= 0:
            continue
        draws += 1
        if 1.0 < have - got < mtc:
            bad += 1
    out["draws"], out["sub_min_remnants"], out["mtc"] = draws, bad, mtc

    # ---- DENSITY + tank utilisation ---------------------------------------
    # Added 2026-08-25 after an anchoring change passed every check here and
    # then, on the full run, turned out to have crammed the same fish into 44%
    # fewer tanks at up to 613 kg/m3 -- 6x the hard cap. Nothing in this harness
    # looked at density, so a catastrophic plan read as a clean pass. A check
    # that cannot see the failure mode is not a check.
    over = totd = 0
    peak = 0.0
    for r in blrows[h_i + 1:]:
        if not r or not r[bx["Week"]]:
            continue
        if str(r[bx["Stage"]]).strip() == "STARVE":
            continue                      # purge is density-exempt (R8)
        try:
            d = float(r[bx["Density (kg/m3)"]] or 0)
        except Exception:
            continue
        if d <= 0:
            continue
        totd += 1
        peak = max(peak, d)
        if d > 95.0:
            over += 1
    out["prod_tank_weeks"], out["over_cap"], out["peak_density"] = totd, over, peak
    wb.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workbook", required=True)
    ap.add_argument("--weeks", type=int, default=12)
    ap.add_argument("--out", default=None)
    ap.add_argument("--config-dir", default=str(_ROOT / "config"))
    ap.add_argument("--scenario-dir", default=str(_ROOT / "scenario"))
    args = ap.parse_args()

    # NOT inside the repo: it lives under OneDrive, which holds locks on
    # directories it is syncing and makes rmtree fail with WinError 5.
    scratch = Path(args.out).parent if args.out else Path(
        os.environ.get("FASTCHECK_DIR")
        or Path(tempfile.gettempdir()) / "as_fastcheck")
    scratch.mkdir(parents=True, exist_ok=True)
    cfg = _short_config(Path(args.config_dir), args.weeks, scratch / "config")
    xlsx = Path(args.out) if args.out else scratch / f"fast_{args.weeks}w.xlsx"

    from forecast.config_io import load_config
    from tools.run_global_forecast import run_global

    t0 = time.time()
    rc = run_global(args.workbook, str(xlsx), config_dir=str(cfg),
                    scenario_dir=args.scenario_dir)
    dt = time.time() - t0
    if rc != 0:
        print(f"run_global FAILED rc={rc}")
        return rc

    control, _t, _f = load_config(str(cfg))
    a = audit(xlsx, control)
    print(f"\n=== fast_check: {args.weeks} weeks in {dt/60:.1f} min ===")
    print(f"  transfer rows          : {a['transfer_rows']:,}")
    print(f"  intra-6N moves         : {a['sixn_moves']:,}"
          f"  ({a['sixn_move_fish']:,.0f} fish)     <- target 0")
    print(f"  topology violations    : {a['topology'] or 'none'}")
    print(f"  harvest draws          : {a['draws']:,}")
    print(f"  prod tank-weeks        : {a['prod_tank_weeks']:,}")
    print(f"  over 95 kg/m3          : {a['over_cap']:,}"
          f"  ({100.0 * a['over_cap'] / max(1, a['prod_tank_weeks']):.1f}%)"
          f"   peak {a['peak_density']:,.0f}        <- watch for CONCENTRATION")
    print(f"  sub-min remnants       : {a['sub_min_remnants']:,}"
          f"   (min_tank_control {a['mtc']:,.0f})   <- target 0")
    print("\n  NOTE: a short horizon does not reach 2028 production mode;"
          "\n  R3/R4 breaches from that era cannot appear here.")
    print("\n  Conservation is judged by check_global_invariants.py:")
    print(f"    python tools/check_global_invariants.py \"{xlsx}\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
