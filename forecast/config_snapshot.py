"""Embed / extract the active config + scenario in the output workbook.

So a saved forecast workbook is a self-documenting, re-importable record:
the exact biology/facility/control + batches/limits that produced it travel
WITH the result. Stored as the raw YAML text (lossless, human-readable,
trivially re-importable), one labelled block per file.

TWO TABS SINCE 2026-10-02 (operator: "make the RunConfig output more visually
pleasing"). `RunConfig` is now the READABLE view -- run identity, then one
formatted table per file (forecast.run_config_view) -- and the lossless YAML
moved, unchanged in format, to `RunConfig YAML`, written last in the workbook.
`read_config_snapshot` reads `RunConfig YAML` first and falls back to the old
single-sheet layout, so every workbook written before the split still imports.
The readable view never carries a `# ===== key =====` block marker, so it can
never be mistaken for a snapshot by the fallback.

TWO DIFFERENT SHEETS ARE BOTH CALLED `RunConfig`. This module writes the
re-importable YAML snapshot, the only kind still produced. The other kind is a
METHOD STAMP under the same name: a key/value record of what ran (method,
caps, conservation), with no YAML and nothing to restore. Nothing writes a
stamp any more — the exporter that did went with the Global method — but
stamped workbooks are still on disk, so both kinds carry an explicit marker in
cell A1: `run_config_kind()` tells them apart and the import path says WHICH
it found instead of reporting "no snapshot" on a workbook that plainly has a
RunConfig sheet.
"""
from __future__ import annotations

import re
from pathlib import Path

SNAPSHOT_SHEET = "RunConfig"
# The lossless YAML companion of the readable `RunConfig` view. Named after its
# sheet so a caller passing a non-default `sheet_name` gets its own companion.
YAML_SUFFIX = " YAML"
YAML_SHEET = SNAPSHOT_SHEET + YAML_SUFFIX

# A1 markers — the sheet-kind discriminator. `KIND_STAMP_MARK` is the A1 text
# the removed Global exporter wrote: read-only history now, and it must not
# drift or those workbooks stop being recognised (a test pins the pair).
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


def _excluded_line() -> str:
    return ("NOT included (analysis overlays — they steer scoring, not the "
            "run, and importing must not overwrite your current ones): "
            + "; ".join(f"{k} ({why})" for k, why in _EXCLUDED))


def write_config_snapshot(wb, config_dir=None, scenario_dir=None,
                          sheet_name: str = SNAPSHOT_SHEET, *,
                          run_info=None, pr_closing=None) -> None:
    """Write the active config/scenario into the workbook: the READABLE
    `RunConfig` view (where the YAML-only sheet used to sit) plus the lossless
    `RunConfig YAML` companion that import reads.

    Only files whose directory is provided AND present are written. The
    analysis overlays in `_EXCLUDED` are never written; both sheets name them
    and say why, so the omission is visible rather than inferred.

    `run_info` is an optional list of (label, value, meaning) rows for the
    view's run-identity block; `pr_closing` selects this PR's manual-events
    file for display. Neither changes what is re-importable.
    """
    dirs = {"config": config_dir, "scenario": scenario_dir}
    yaml_name = sheet_name + YAML_SUFFIX
    for stale in (sheet_name, yaml_name):
        if stale in wb.sheetnames:
            del wb[stale]
    view = wb.create_sheet(sheet_name)
    _write_yaml_sheet(wb.create_sheet(yaml_name), dirs)
    try:
        from .run_config_view import write_readable_view
        write_readable_view(view, dirs, files=_FILES, run_info=run_info,
                            pr_closing=pr_closing, yaml_sheet=yaml_name,
                            title=(f"{KIND_SNAPSHOT_MARK} — the exact config + "
                                   "scenario this forecast ran from."),
                            excluded_line=_excluded_line())
    except Exception as exc:  # noqa: BLE001 - the YAML tab is the record
        # A rendering bug must never cost the operator the importable record:
        # fall back to a marked, near-empty view that points at the YAML tab.
        wb.remove(view)
        view = wb.create_sheet(sheet_name, wb.sheetnames.index(yaml_name))
        view["A1"] = (f"{KIND_SNAPSHOT_MARK} — readable view unavailable "
                      f"({type(exc).__name__}); the full config is on the "
                      f"'{yaml_name}' tab.")
        view["A3"] = _excluded_line()


def move_yaml_sheet_last(wb, sheet_name: str = SNAPSHOT_SHEET) -> None:
    """Put the YAML companion at the end of the workbook (it is the machine
    record; the readable view keeps RunConfig's old place among the tabs)."""
    name = sheet_name + YAML_SUFFIX
    if name in wb.sheetnames:
        idx = wb.sheetnames.index(name)
        wb.move_sheet(name, offset=len(wb.sheetnames) - 1 - idx)


def is_yaml_dump(ws) -> bool:
    """True for a sheet laid out as `# ===== key =====` YAML blocks (the
    companion tab, or a pre-2026-10-02 RunConfig)."""
    for row in ws.iter_rows(min_col=1, max_col=1, values_only=True):
        v = row[0] if row else None
        if isinstance(v, str) and _MARK_RE.match(v):
            return True
    return False


def _write_yaml_sheet(ws, dirs) -> None:
    """The lossless record, in exactly the block format RunConfig used to
    hold (so the pre-split reader parses it unchanged)."""
    ws.append([f"{KIND_SNAPSHOT_MARK} — the exact config + scenario this "
               "forecast ran from."])
    ws.append(["Re-importable: app Configure mode > 'Import from workbook', "
               "or forecast.config_snapshot.import_config_snapshot(). The "
               "readable view of the same files is the RunConfig tab."])
    ws.append([_excluded_line()])
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
        return "snapshot"   # the pre-split dump AND the readable view
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
        if n == 0:
            return (f"'{sheet_name}' is the readable config view, but its "
                    f"'{sheet_name}{YAML_SUFFIX}' tab — the part an import "
                    f"reads — is missing from this workbook, so there is "
                    f"nothing to import.")
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
    """Return {'config/control.yaml': <yaml text>, ...} from the snapshot.

    Reads the `<sheet_name> YAML` companion first (2026-10-02 onward), then
    the old single-sheet layout, so workbooks from before the split import.
    """
    blocks = _read_blocks(wb, sheet_name + YAML_SUFFIX)
    if blocks:
        return blocks
    return _read_blocks(wb, sheet_name)


def _read_blocks(wb, sheet_name: str) -> dict[str, str]:
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
