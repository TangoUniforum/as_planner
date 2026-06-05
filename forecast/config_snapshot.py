"""Embed / extract the active config + scenario in the output workbook.

So a saved forecast workbook is a self-documenting, re-importable record:
the exact biology/facility/control + batches/limits that produced it travel
WITH the result. Stored as the raw YAML text (lossless, human-readable,
trivially re-importable) in a single `RunConfig` sheet, one labelled block
per file.
"""
from __future__ import annotations

import re
from pathlib import Path

SNAPSHOT_SHEET = "RunConfig"

# (filename, kind) — kind selects config_dir vs scenario_dir.
_FILES = [
    ("control.yaml", "config"),
    ("biology.yaml", "config"),
    ("facility.yaml", "config"),
    ("batches.yaml", "scenario"),
    ("limits.yaml", "scenario"),
]

_MARK = "# ===== {key} ====="
_MARK_RE = re.compile(r"^# ===== (.+?) =====$")


def write_config_snapshot(wb, config_dir=None, scenario_dir=None,
                          sheet_name: str = SNAPSHOT_SHEET) -> None:
    """Write the active config/scenario YAML into a `RunConfig` sheet.

    Only files whose directory is provided AND present are written.
    """
    dirs = {"config": config_dir, "scenario": scenario_dir}
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append(["RUN CONFIG SNAPSHOT — the exact config + scenario this "
               "forecast ran from."])
    ws.append(["Re-importable: app Configure mode > 'Import from workbook', "
               "or forecast.config_snapshot.import_config_snapshot()."])
    ws.append([])
    for name, kind in _FILES:
        base = dirs.get(kind)
        if base is None:
            continue
        p = Path(base) / name
        if not p.exists():
            continue
        ws.append([_MARK.format(key=f"{kind}/{name}")])
        for line in p.read_text(encoding="utf-8").splitlines():
            ws.append([line])
        ws.append([])
    ws.column_dimensions["A"].width = 100


def read_config_snapshot(wb, sheet_name: str = SNAPSHOT_SHEET) -> dict[str, str]:
    """Return {'config/control.yaml': <yaml text>, ...} from the snapshot."""
    if sheet_name not in wb.sheetnames:
        return {}
    ws = wb[sheet_name]
    blocks: dict[str, str] = {}
    cur: str | None = None
    lines: list[str] = []
    for row in ws.iter_rows(values_only=True):
        v = row[0] if row else None
        s = "" if v is None else str(v)
        m = _MARK_RE.match(s)
        if m:
            if cur is not None:
                blocks[cur] = "\n".join(lines).strip("\n")
            cur = m.group(1)
            lines = []
        elif cur is not None:
            lines.append(s)
    if cur is not None:
        blocks[cur] = "\n".join(lines).strip("\n")
    return blocks


def import_config_snapshot(wb, config_dir, scenario_dir) -> list[str]:
    """Write a workbook's RunConfig snapshot back into config_dir/scenario_dir.

    Returns the list of restored keys (e.g. ['config/control.yaml', ...]).
    """
    blocks = read_config_snapshot(wb)
    written: list[str] = []
    for key, text in blocks.items():
        kind, _, name = key.partition("/")
        base = Path(config_dir) if kind == "config" else Path(scenario_dir)
        base.mkdir(parents=True, exist_ok=True)
        (base / name).write_text(text + "\n", encoding="utf-8")
        written.append(key)
    return written
