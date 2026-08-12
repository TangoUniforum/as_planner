"""Embed / extract the active config + scenario in the output workbook.

So a saved forecast workbook is a self-documenting, re-importable record:
the exact biology/facility/control + batches/limits that produced it travel
WITH the result. Stored as the raw YAML text (lossless, human-readable,
trivially re-importable) in a single `RunConfig` sheet, one labelled block
per file.

TWO DIFFERENT SHEETS ARE BOTH CALLED `RunConfig`. This module writes the
re-importable YAML snapshot (controller family). `tools/run_global_forecast.py`
writes a Global METHOD STAMP under the same name: a key/value record of what
ran (method, caps, conservation), with no YAML and nothing to restore. Both
now carry an explicit kind marker in cell A1 so `run_config_kind()` can tell
them apart, and the import path can say WHICH it found instead of reporting
"no snapshot" on a workbook that plainly has a RunConfig sheet.
"""
from __future__ import annotations

import re
from pathlib import Path

SNAPSHOT_SHEET = "RunConfig"

# A1 markers — the sheet-kind discriminator. `KIND_STAMP_MARK` must stay in
# sync with the A1 text run_global_forecast.py writes (a test pins the pair).
KIND_SNAPSHOT_MARK = "RUN CONFIG SNAPSHOT"
KIND_STAMP_MARK = "RUN CONFIG — GLOBAL METHOD EXPORT"

# (filename, kind) — kind selects config_dir vs scenario_dir.
_FILES = [
    ("control.yaml", "config"),
    ("biology.yaml", "config"),
    ("facility.yaml", "config"),
    ("batches.yaml", "scenario"),
    ("limits.yaml", "scenario"),
]

# DELIBERATELY NOT SNAPSHOTTED — and said out loud, in the sheet itself, so an
# operator who imports a workbook knows what did NOT come back.
#
# These are analysis-layer OVERLAYS, not engine inputs: no run's numbers depend
# on them (cf. app._NON_ENGINE_CONFIG, which excludes the same three from the
# config fingerprint for exactly this reason). Two of them are also actively
# DANGEROUS to restore from a workbook:
#   * analysis_defaults.yaml is the PROMOTED QUICK-RUN DEFAULT — which method
#     and knobs the ⚡ Quick run card fires. It is a property of the operator's
#     current judgement, not of the run being imported. Restoring it would let
#     opening an old workbook silently re-point today's Quick run at a plan
#     that won a tournament on a DIFFERENT PR.
#   * targets.yaml / economics.yaml are the scoring yardstick. A workbook must
#     not be able to move the bar it is being graded against.
# The snapshot's job is "reproduce this run", and all three are outside it.
_EXCLUDED = [
    ("config/analysis_defaults.yaml",
     "promoted Quick-run default — your current choice, not the run's; "
     "importing a workbook must not re-point the Quick run card"),
    ("config/targets.yaml", "scoring targets — the yardstick, not an input"),
    ("config/economics.yaml", "price bands — the yardstick, not an input"),
]

_MARK = "# ===== {key} ====="
_MARK_RE = re.compile(r"^# ===== (.+?) =====$")


def write_config_snapshot(wb, config_dir=None, scenario_dir=None,
                          sheet_name: str = SNAPSHOT_SHEET) -> None:
    """Write the active config/scenario YAML into a `RunConfig` sheet.

    Only files whose directory is provided AND present are written. The
    analysis overlays in `_EXCLUDED` are never written; the sheet names them
    and says why, so the omission is visible rather than inferred.
    """
    dirs = {"config": config_dir, "scenario": scenario_dir}
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append([f"{KIND_SNAPSHOT_MARK} — the exact config + scenario this "
               "forecast ran from."])
    ws.append(["Re-importable: app Configure mode > 'Import from workbook', "
               "or forecast.config_snapshot.import_config_snapshot()."])
    ws.append(["NOT included (analysis overlays — they steer scoring, not the "
               "run, and importing must not overwrite your current ones): "
               + "; ".join(f"{k} ({why})" for k, why in _EXCLUDED)])
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


def run_config_kind(wb, sheet_name: str = SNAPSHOT_SHEET) -> str | None:
    """Which `RunConfig` sheet is this? -> 'snapshot' | 'stamp' | 'unknown' | None.

    None means no such sheet at all. 'stamp' is the Global method export: a
    record of what ran, with nothing to restore. The caller must be able to
    say so — reporting "no snapshot found" for a workbook that visibly HAS a
    RunConfig sheet is the silent failure this function exists to prevent.
    """
    if sheet_name not in wb.sheetnames:
        return None
    a1 = wb[sheet_name]["A1"].value
    a1 = "" if a1 is None else str(a1)
    if a1.startswith(KIND_SNAPSHOT_MARK):
        return "snapshot"
    if a1.startswith(KIND_STAMP_MARK) or a1.startswith("RUN CONFIG —"):
        return "stamp"
    # Older workbooks predate the A1 marker: fall back to the content test.
    return "snapshot" if read_config_snapshot(wb, sheet_name) else "unknown"


def describe_run_config_sheet(wb, sheet_name: str = SNAPSHOT_SHEET) -> str:
    """One operator-legible sentence about what this workbook's RunConfig is."""
    kind = run_config_kind(wb, sheet_name)
    if kind is None:
        return (f"This workbook has no '{sheet_name}' sheet, so it carries no "
                f"config to import.")
    if kind == "snapshot":
        n = len(read_config_snapshot(wb, sheet_name))
        return (f"'{sheet_name}' is a re-importable YAML config snapshot "
                f"({n} file(s)).")
    if kind == "stamp":
        return (f"This workbook's '{sheet_name}' sheet is a GLOBAL METHOD "
                f"STAMP, not a config snapshot: it records what ran (method, "
                f"caps, conservation) for the reader, and holds no YAML to "
                f"restore. Only controller-family runs (forecast.run) embed an "
                f"importable snapshot — re-export from one of those, or import "
                f"a filled config template instead.")
    return (f"This workbook's '{sheet_name}' sheet is in a format this version "
            f"does not recognise, so there is nothing to import from it.")


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
    Raises ValueError — never returns an empty list quietly — when the workbook
    has a RunConfig sheet that is not an importable snapshot (the Global method
    stamp), so a user-facing "Import" can never report success-shaped silence.
    """
    blocks = read_config_snapshot(wb)
    if not blocks:
        raise ValueError(describe_run_config_sheet(wb))
    written: list[str] = []
    for key, text in blocks.items():
        kind, _, name = key.partition("/")
        base = Path(config_dir) if kind == "config" else Path(scenario_dir)
        base.mkdir(parents=True, exist_ok=True)
        (base / name).write_text(text + "\n", encoding="utf-8")
        written.append(key)
    return written
