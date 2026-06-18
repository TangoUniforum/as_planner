"""Atomic YAML file I/O resilient to OneDrive / Excel file locks.

The config/ and scenario/ trees live inside a OneDrive-synced folder. Two
failure modes follow from that:

  1. A plain truncate-in-place write (`open(path, "w")`) holds the file open
     and zero-length for the duration of the write. Any concurrent reader
     (e.g. the Streamlit app re-running its config-editor loader right after
     an import) sees `PermissionError [Errno 13]` on Windows, or reads a
     half-written / empty file. An interrupted write leaves it truncated.

  2. OneDrive's sync client briefly opens files exclusively as it uploads
     them, so even a read of an untouched file can transiently fail.

`write_text_atomic` writes to a sibling temp file and `os.replace()`s it into
place (atomic on the same filesystem — the reader sees either the old or the
new file, never a partial one), shrinking the lock window to a single rename.
`read_text_resilient` retries a few times on the transient lock before giving
up, so a passing OneDrive scan doesn't surface as a hard error.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

# Short bounded retry — OneDrive/AV locks clear in well under a second; we do
# not want to hang the UI, so cap total wait at ~1s.
_RETRIES = 8
_RETRY_DELAY_S = 0.125


def write_text_atomic(path, text: str) -> None:
    """Write `text` to `path` atomically (temp file in the same dir + replace)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.tmp-{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp, p)
    finally:
        # If the replace failed, don't leave the temp file behind.
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def read_text_resilient(path) -> str:
    """Read `path` as UTF-8 text, retrying briefly on a transient file lock."""
    p = Path(path)
    last: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            with p.open("r", encoding="utf-8") as fh:
                return fh.read()
        except PermissionError as exc:  # OneDrive / Excel holding the file open
            last = exc
            time.sleep(_RETRY_DELAY_S * (attempt + 1))
    raise PermissionError(
        f"{p} is locked (likely OneDrive sync or another program has it open) "
        f"after {_RETRIES} retries — close it / pause OneDrive and try again"
    ) from last


def _replace_with_retry(src: Path, dst: Path) -> None:
    last: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:  # dst momentarily locked by OneDrive
            last = exc
            time.sleep(_RETRY_DELAY_S * (attempt + 1))
    raise PermissionError(
        f"Could not replace {dst} (locked, likely OneDrive sync or another "
        f"program has it open) after {_RETRIES} retries"
    ) from last
