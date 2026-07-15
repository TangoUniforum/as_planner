"""_short_horizon_config: the co-pilot shortens the planner horizon to
(window + look-ahead + buffer) when that's less than the config horizon, so it
never wastes minutes running the full-horizon global solve for a 6-week look-ahead.
The whole config dir is copied and only horizon_weeks is rewritten, so the buffer
keeps the near-term recommendations identical to the full-horizon run."""
from __future__ import annotations

import os
import shutil

import yaml

from forecast.copilot import _short_horizon_config


def _cfg(tmp_path, horizon):
    d = tmp_path / "config"
    d.mkdir()
    (d / "control.yaml").write_text(
        f"# control\nhorizon_weeks: {horizon}\nmax_harvest_per_week: 55000\n",
        encoding="utf-8")
    (d / "biology.yaml").write_text("sgr: 1\n", encoding="utf-8")   # must be copied
    return str(d)


def test_shortens_when_shorter(tmp_path):
    cfg = _cfg(tmp_path, 130)
    cdir, tmp = _short_horizon_config(cfg, window_n=3, n_weeks=6, buffer=10)  # want=19
    try:
        assert tmp is not None and cdir != cfg, "should produce a shortened temp config"
        got = yaml.safe_load(open(os.path.join(cdir, "control.yaml")))
        assert got["horizon_weeks"] == 19, "horizon = window(3) + look-ahead(6) + buffer(10)"
        assert got["max_harvest_per_week"] == 55000, "other knobs preserved"
        assert os.path.exists(os.path.join(cdir, "biology.yaml")), "whole config dir copied"
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def test_no_shorten_when_not_shorter(tmp_path):
    # want = 0 + 6 + 10 = 16 >= config horizon 12 -> leave the config untouched.
    cfg = _cfg(tmp_path, 12)
    cdir, tmp = _short_horizon_config(cfg, window_n=0, n_weeks=6, buffer=10)
    assert tmp is None and cdir == cfg


def test_missing_control_returns_original(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    cdir, tmp = _short_horizon_config(str(d), window_n=0, n_weeks=6, buffer=10)
    assert tmp is None and cdir == str(d)
