"""Freeze the reference baseline.

    python tests/fixtures/freeze_golden.py

Runs the pipeline on the reference fixture and writes
`tests/fixtures/reference/golden.json`.

RUN THIS DELIBERATELY, NEVER TO MAKE A RED TEST GO GREEN. The baseline exists
so that a number moving is a decision, not an accident. The workflow when
tests/test_reference_baseline.py fails is:

  1. read the diff the test prints — it names every metric that moved;
  2. satisfy yourself the move is intended and understood;
  3. re-freeze IN THE SAME COMMIT as the change that caused it, and say in the
     commit message which numbers moved and why.

If you cannot explain a line of the diff, that line is the bug.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.fixtures.golden import extract          # noqa: E402

HERE = Path(__file__).resolve().parent
REF = HERE / "reference"
GOLDEN = REF / "golden.json"


def run_reference(out_dir: Path) -> Path:
    """Run the pipeline on the fixture; return the produced workbook path."""
    from forecast.run import main as run_main
    out = Path(out_dir) / "reference_out.xlsx"
    # calib_log_path="" — the fixture is synthetic. Without this every run of
    # it appended to the live fw_calibration_history.jsonl; on 2026-08-20 that
    # put 83 fake records into the operator's real FW history, under a
    # pr_closing date that collides with a real one. Test artifacts never write
    # to operational history.
    run_main(str(REF / "production_report.xlsx"), str(out),
             config_dir=str(REF / "config"), scenario_dir=str(REF / "scenario"),
             calib_log_path="")
    # run.main may rename the extension to .xlsm when the source carries VBA.
    if not out.exists():
        alt = out.with_suffix(".xlsm")
        if alt.exists():
            return alt
    return out


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        produced = run_reference(Path(td))
        metrics = extract(produced)
    GOLDEN.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    t = metrics["totals"]
    print(f"froze {GOLDEN.relative_to(_ROOT)}")
    print(f"  harvest events {t['harvest_events']}")
    print(f"  fish           {t['fish']:,.0f}")
    print(f"  gross          {t['gross_kg']:,.0f} kg")
    print(f"  HOG            {t['hog_kg']:,.0f} kg")
    print(f"  months pinned  {len(metrics['monthly_hog_kg'])}")
    print(f"  weeks pinned   {len(metrics['weekly_fish'])}")
    print(f"  compliance     {metrics['compliance']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
