"""Tank rota (fixed standard schedule) for a finished run. Writes a companion workbook; changes nothing else.

    python -m tools.tank_rota --workbook "<run>_planned_<scenario>.xlsm" --steps "0:4, 21:6, 32:+1"
        [--fish 310000]                          fish per batch (default: the plan's)
        [--batch B59]                            whose life to copy (default: the latest one that lives its whole life
                                                 inside the plan)
        [--check-only]                           the one-second tank count only
        [--config-dir config] [--scenario-dir scenario] [--workers 12] [--out <file.xlsx>]

Steps: entry tanks at week 0, the move to grow-out (whole batch) at week N, then +n tanks at later weeks.
See docs/USER_GUIDE.md, "Tank rota".
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forecast import least_moves, tank_rota                       # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--workbook', required=True)
    ap.add_argument('--steps', required=True)
    ap.add_argument('--fish', type=float, default=None)
    ap.add_argument('--batch', default=None)
    ap.add_argument('--check-only', action='store_true')
    ap.add_argument('--config-dir', default=str(ROOT / 'config'))
    ap.add_argument('--scenario-dir', default=str(ROOT / 'scenario'))
    ap.add_argument('--workers', type=int, default=max(1, (os.cpu_count() or 4) - 4))
    ap.add_argument('--out', default=None)
    a = ap.parse_args(argv)
    log = lambda s: print(s, flush=True)
    D = least_moves.read_endpoint(a.workbook, a.config_dir, a.scenario_dir)
    steps = tank_rota.parse_steps(a.steps)
    if a.check_only:
        errs = tank_rota.validate_steps(steps, len(tank_rota._facility(D)['entry']))
        if errs:
            log('; '.join(errs)); return 1
        q = tank_rota.quick_check(D, tank_rota.batch_profile(D, a.batch, a.fish), steps)
        for ln in q['lines']:
            log(ln)
        return 0 if q['ok'] else 1
    res = tank_rota.run(D, steps, a.batch, a.fish, workers=a.workers, log=log)
    if res['stage'] != 'done':
        for ln in res.get('lines', []):
            log(ln)
        return 1
    ev = res['eval']
    log(f"result: {'clean' if res['ok'] else ev['check']['issues']} | transfers per harvested fish {ev['tpf']:.2f} | "
        f"move events/week max {ev['max_moves']} | busiest system {ev['busiest_t']:.0f} t")
    out = a.out or str(Path(a.workbook).with_name(Path(a.workbook).stem + '_tank_rota.xlsx'))
    tank_rota.write_workbook(res, out, Path(a.workbook).name)
    log(f'saved {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
