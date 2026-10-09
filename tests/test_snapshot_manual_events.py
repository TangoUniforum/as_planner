"""A workbook carries its PR's manual events, and importing it restores them.

Operator 2026-10-09: "import an output workbook, and that would set up the app
the same as the settings used to make the workbook". Until then the RunConfig
YAML tab held control/biology/facility/batches/limits only, so re-running an
old scenario from its workbook silently used TODAY's manual events (exactly how
Scenario 2 could not be reproduced from its workbook on 2026-10-08).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import openpyxl

from forecast import config_snapshot as cs

PR = date(2026, 9, 30)
EVENTS = ("# operator header\n"
          "events:\n"
          "- type: harvest\n"
          "  week: 1\n"
          "  from_tank: 63\n"
          "  destinations: []\n"
          "  count: 4800.0\n")


def _seed(tmp_path, with_events=True) -> tuple[Path, Path]:
    cfg, scn = tmp_path / "config", tmp_path / "scenario"
    cfg.mkdir(parents=True)
    (scn / "manual_events").mkdir(parents=True)
    for n, t in (("control.yaml", "scenario_name: S\n"), ("biology.yaml", "a: 1\n"),
                 ("facility.yaml", "tanks: []\n")):
        (cfg / n).write_text(t, encoding="utf-8")
    for n, t in (("batches.yaml", "batches: []\n"), ("limits.yaml", "facility: []\n")):
        (scn / n).write_text(t, encoding="utf-8")
    if with_events:
        (scn / "manual_events" / "2026-09-30.yaml").write_text(EVENTS, encoding="utf-8")
    return cfg, scn


def _wb(cfg, scn, pr_closing=PR):
    wb = openpyxl.Workbook()
    cs.write_config_snapshot(wb, config_dir=cfg, scenario_dir=scn, pr_closing=pr_closing)
    return wb


def test_the_events_file_travels_in_the_yaml_tab(tmp_path):
    cfg, scn = _seed(tmp_path)
    blocks = cs.read_config_snapshot(_wb(cfg, scn))
    assert blocks["scenario/manual_events/2026-09-30.yaml"] == EVENTS.strip("\n")
    assert cs.snapshot_has_manual_events(_wb(cfg, scn))


def test_import_restores_the_events_and_keeps_the_replaced_file(tmp_path):
    cfg, scn = _seed(tmp_path / "run")
    wb = _wb(cfg, scn)
    c2, s2 = _seed(tmp_path / "now", with_events=False)
    current = s2 / "manual_events" / "2026-09-30.yaml"
    current.write_text("events: []  # today's script\n", encoding="utf-8")
    restored = cs.import_config_snapshot(wb, c2, s2)
    assert "scenario/manual_events/2026-09-30.yaml" in restored
    assert current.read_text(encoding="utf-8") == EVENTS
    backup = current.with_name(current.name + cs.EVENTS_BACKUP_SUFFIX)
    assert backup.read_text(encoding="utf-8") == "events: []  # today's script\n"


def test_a_run_without_events_restores_no_events(tmp_path):
    cfg, scn = _seed(tmp_path / "run", with_events=False)
    wb = _wb(cfg, scn)
    c2, s2 = _seed(tmp_path / "now")              # today HAS events for that PR
    cs.import_config_snapshot(wb, c2, s2)
    import yaml
    now = yaml.safe_load((s2 / "manual_events" / "2026-09-30.yaml").read_text(encoding="utf-8"))
    assert now == {"events": []}


def test_an_older_workbook_leaves_todays_events_alone(tmp_path):
    cfg, scn = _seed(tmp_path / "run")
    wb = _wb(cfg, scn, pr_closing=None)            # no PR date -> no events block
    assert not cs.snapshot_has_manual_events(wb)
    c2, s2 = _seed(tmp_path / "now")
    before = (s2 / "manual_events" / "2026-09-30.yaml").read_text(encoding="utf-8")
    cs.import_config_snapshot(wb, c2, s2)
    assert (s2 / "manual_events" / "2026-09-30.yaml").read_text(encoding="utf-8") == before


def test_import_writes_only_the_files_a_snapshot_writes(tmp_path):
    cfg, scn = _seed(tmp_path / "run")
    wb = _wb(cfg, scn)
    ws = wb[cs.YAML_SHEET]
    ws.append(["# ===== scenario/../../escaped.yaml ====="])
    ws.append(["x: 1"])
    c2, s2 = _seed(tmp_path / "now")
    restored = cs.import_config_snapshot(wb, c2, s2)
    assert not any("escaped" in k for k in restored)
    assert not (tmp_path / "escaped.yaml").exists()
