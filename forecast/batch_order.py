"""One batch order for every sheet and every list: B9 < B10 < B41 < B100.

The operator (2026-09-11): "all the batch names should be in chronological
order B41, B42, B43 ... top of month to bottom of month". Batch number order IS
chronological order -- in scenario/batches.yaml input_date and tran_og_date both
rise strictly with the number (pinned by tests/test_batch_order.py) -- so the
sort key is the number, not a date a batch may not have.

Before this module each writer sorted batches its own way: most on the raw
STRING, which is right only while every id has two digits ("B100" < "B37"), one
on the SW-entry week (ties kept the engine's tank-walk order, so the in-flight
block read B49 B48 ... B42), one on insertion order, one on a private numeric key
that was not unique ("B041" and "B41" tied, over a set of strings -- hash-seed
order). Every writer and app list now uses THIS key.

A TOTAL order: the full id string is the final term, so two distinct ids never
tie and sorting a SET of ids gives the same list under every PYTHONHASHSEED
(project rule: never iterate a set of strings without a unique final sort
term). Pure: imports nothing from the engine.
"""
from __future__ import annotations

import re

_ID = re.compile(r"^([A-Za-z]*)(\d+)(.*)$")


def batch_sort_key(batch_id) -> tuple:
    """Natural batch order: prefix, then the number, then the rest, then the
    full id (the unique final term). Ids without a number sort after, by name.
    None is safe (sorts as the empty id)."""
    s = "" if batch_id is None else str(batch_id).strip()
    m = _ID.match(s)
    if m:
        return (0, m.group(1).upper(), int(m.group(2)), m.group(3), s)
    return (1, "", 0, "", s)


def sorted_batches(ids) -> list:
    """`ids` (any iterable, a set included) in natural batch order."""
    return sorted(ids, key=batch_sort_key)
