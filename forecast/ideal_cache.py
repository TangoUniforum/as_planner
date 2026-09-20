"""A persistent, version-keyed cache of Ideal cell evaluations.

WHY. One cell is a 156-week engine run. A cadence x size x cap sweep is
hundreds of them, every search repeats most of the previous one, and the
answers were being thrown away between runs -- 2026-09-19 alone re-ran the
same cells across five separate sweeps.

The dangerous half is staleness, not cost. A cached verdict is only valid for
the CODE and the CONFIG that produced it: the grading-CV change measured on the
same day flipped 42 d x 225k from "zero breach" to two breach-weeks without
touching cadence, size or cap. A cache that answered from the old entry would
have reported a plan as legal when the current engine says it is not, and
nothing would have looked wrong.

So every entry carries a FINGERPRINT of what produced it:
  * `engine`  -- the content of the modules that decide a plan
  * `config`  -- the control/facility/biology/limits files the run reads
  * `knobs`   -- the method and its overrides
  * `horizon` -- 156-week full fidelity and a 104-week screening run are
                 different measurements and never share an entry
A lookup with a different fingerprint MISSES. It does not warn and reuse.

Not a correctness mechanism: nothing here decides anything, it only remembers.
Delete the file and every search still gives the same answers, more slowly.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

# The modules whose content changes a plan. excel_io is excluded deliberately:
# it decides how a run is REPORTED, and a reporting change must not invalidate
# thousands of cells. placement/biology/events/tiers/caps/run decide the plan.
ENGINE_FILES = ("placement.py", "biology.py", "events.py", "tiers.py",
                "caps.py", "run.py", "harvest_scheduler.py", "sixn.py",
                "ideal_engine.py")
# The config a cell's verdict depends on. costs.yaml is NOT read: no Ideal
# verdict depends on it, and hashing it would put cost data in the cache key.
CONFIG_FILES = ("control.yaml", "facility.yaml", "biology.yaml")
SCENARIO_FILES = ("limits.yaml",)


def _digest(paths) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        p = Path(p)
        h.update(p.name.encode())
        h.update(p.read_bytes() if p.exists() else b"<missing>")
    return h.hexdigest()[:16]


def engine_fingerprint(package_dir=None) -> str:
    """Hash of the modules that decide a plan."""
    d = Path(package_dir) if package_dir else Path(__file__).resolve().parent
    return _digest([d / f for f in ENGINE_FILES])


def inputs_fingerprint(config_dir, scenario_dir) -> str:
    """Hash of the config + scenario files a cell's verdict depends on."""
    c, s = Path(config_dir), Path(scenario_dir)
    return _digest([c / f for f in CONFIG_FILES] + [s / f for f in SCENARIO_FILES])


def cell_key(cadence_days: int, batch_size: int, cap_kg: float, *,
             horizon_weeks: int, method: str, knobs: Optional[dict],
             engine: str, inputs: str) -> str:
    """One cell's identity. Knobs are sorted so {a,b} and {b,a} are one key."""
    payload = json.dumps({
        "cad": int(cadence_days), "size": int(batch_size),
        "cap": round(float(cap_kg), 3), "horizon": int(horizon_weeks),
        "method": str(method), "knobs": sorted((str(k), str(v))
                                               for k, v in (knobs or {}).items()),
        "engine": engine, "inputs": inputs,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


class CellCache:
    """A JSON-lines store of evaluated cells. Append-only; last write wins.

    Concurrency: appends are single writes of one line, which is atomic enough
    for the process pool the optimizer uses (workers evaluate, the parent
    writes). The file is rewritten only by `compact`.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._mem: dict[str, dict] = {}
        self._loaded = False

    def load(self) -> "CellCache":
        self._mem.clear()
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue          # a torn line never breaks a search
                if isinstance(rec, dict) and "key" in rec:
                    self._mem[rec["key"]] = rec
        self._loaded = True
        return self

    def get(self, key: str) -> Optional[dict]:
        if not self._loaded:
            self.load()
        rec = self._mem.get(key)
        return rec.get("value") if rec else None

    def put(self, key: str, value: Any, meta: Optional[dict] = None) -> None:
        if not self._loaded:
            self.load()
        rec = {"key": key, "value": value, "meta": meta or {}}
        self._mem[key] = rec
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")

    def compact(self) -> int:
        """Rewrite the file with one line per key. Returns the line count."""
        if not self._loaded:
            self.load()
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent or "."),
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for rec in self._mem.values():
                    fh.write(json.dumps(rec, sort_keys=True) + "\n")
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return len(self._mem)

    def __len__(self) -> int:
        if not self._loaded:
            self.load()
        return len(self._mem)
