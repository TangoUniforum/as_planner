"""Tank layouts page helpers: the steps table and the status of a background least-moves job (read from its files)."""
import os
import time

import pandas as pd

import tank_layouts_page as tl


def test_steps_from_table():
    df = pd.DataFrame([{'From week of life': 0, 'Tanks': 4}, {'From week of life': 21, 'Tanks': 6},
                       {'From week of life': 32, 'Tanks': 1}, {'From week of life': None, 'Tanks': None}])
    assert tl._steps_from_table(df) == [{'week': 0, 'tanks': 4}, {'week': 21, 'tanks': 6}, {'week': 32, 'add': 1}]


def test_job_status(tmp_path):
    run = tmp_path / 'lm_run'; run.mkdir()
    (run / 'run.log').write_text('weeks 2026-W49..2026-W53 done\n')
    assert tl._job_status(run)[0] == 'running'
    old = time.time() - 3 * 3600
    os.utime(run / 'run.log', (old, old))
    assert tl._job_status(run)[0] == 'stalled?'                       # no log line for hours: say so
    bad = tmp_path / 'lm_bad'; bad.mkdir()
    (bad / 'run.log').write_text('Traceback (most recent call last):\n  ...\nValueError: x\n')
    assert tl._job_status(bad)[0] == 'failed'
    (run / 'x_least_moves.xlsx').write_bytes(b'')
    assert tl._job_status(run)[0] == 'done'
