"""Least-moves layout for a finished run (Global v2). Writes a companion workbook; changes nothing else.

    python -m tools.least_moves --workbook "<run>_planned_<scenario>.xlsm"
        [--config-dir config] [--scenario-dir scenario]   the folders the run was made with
                                                 (default: this repo's live config/ and scenario/)
        [--start 2026-W49]                       first week to re-plan (default: the week after the manual events)
        [--minutes-per-step 6] [--workers 12]    solver effort; a full 2-year horizon is ~2 h at the defaults
        [--out <file.xlsx>]                      default: next to the workbook, '<name>_least_moves.xlsx'

See docs/USER_GUIDE.md, "Least-moves layout".
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forecast import least_moves                                  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--workbook', required=True)
    ap.add_argument('--config-dir', default=str(ROOT / 'config'))
    ap.add_argument('--scenario-dir', default=str(ROOT / 'scenario'))
    ap.add_argument('--start', default=None)
    ap.add_argument('--minutes-per-step', type=float, default=6.0)
    ap.add_argument('--workers', type=int, default=max(1, (os.cpu_count() or 4) - 4))
    ap.add_argument('--out', default=None)
    a = ap.parse_args(argv)
    least_moves.run(a.workbook, a.config_dir, a.scenario_dir, out_path=a.out, start_week=a.start,
                    secs=a.minutes_per_step * 60, workers=a.workers, log=lambda s: print(s, flush=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
