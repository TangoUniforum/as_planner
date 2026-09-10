"""The Transition: how today's real fish get to the Ideal rhythm.

`forecast.ideal` answers "what rhythm would this facility run forever?" from a
clean slate. The facility is not a clean slate: every batch already stocked is
fixed, and everything that enters OG in the next ~350 days is one of them. The
only lever left is the size of FUTURE stockings, so the transition is a
re-size of the batches not yet stocked, nothing else. Dates, cadence and batch
metadata are untouched.

Two measured findings shape the API:
  * A batch's input_count must scale with its OWN input/TranOG ratio (its FW
    survival). Set only tran_og_count and the model still carries the old egg
    count through freshwater, overstating FW biomass; scaled, biology culls to
    exactly the target at TranOG with no "below target" warning.
  * A flat re-size to 280k from 2026-10-01 still ran 2028-2030 at ~102% of
    the cap; a ramp "240k x3 then 280k" had zero over-cap weeks. So the
    proposal takes a SIZE SEQUENCE (the last value repeats), not one size.

This module is PURE: schedule transforms plus the batches.yaml text the app
offers as a download. It never runs the engine and never writes a file, so the
operator's live scenario cannot be overwritten from here — adopting a
proposal is a deliberate act (Configure -> Batches, or replacing
scenario/batches.yaml by hand). Engine runs of a proposal live in
forecast/ideal_engine: its entry point `run_schedule(batches, project_dir,
...)` takes the proposed batch list exactly as `propose` returns it.

`stocking_frontier.scale_future_batches` is the older "future stockings only"
transform; it mutates in place and applies a proportional factor, so it is not
reused here. The frontier rule is the same: input_date AFTER forecast start.
"""
from __future__ import annotations

import datetime as dt
import numbers
from collections import Counter
from collections.abc import Mapping, Set
from dataclasses import dataclass, replace
from typing import Optional, Sequence

import yaml

from forecast import scenario_io as sio
from forecast.models import BatchInput

# Copied verbatim from scenario_io.dump_scenario, which cannot be called here:
# it also rewrites limits.yaml. test_transition pins the two together by
# comparing against the live scenario/batches.yaml the app's Save wrote.
BATCHES_HEADER = (
    "# Forward batch schedule + batch metadata (input/TranOG dates,\n"
    "# counts, FCR model, corrections). In-flight state comes from the\n"
    "# ProductionReport; this is the planning/metadata layer.\n"
)


@dataclass(frozen=True)
class Change:
    """One future batch re-sized by a proposal."""
    batch_id: str
    input_date: dt.datetime
    old_tran_og_count: int
    new_tran_og_count: int
    old_input_count: int
    new_input_count: int


def _as_date(value, what: str) -> dt.date:
    # datetime first: it is a subclass of date, and comparing a datetime to a
    # date raises, so everything is reduced to a plain date.
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    raise ValueError(f"{what} must be a date or datetime, got {value!r}")


def _check_sizes(sizes) -> tuple[int, ...]:
    # Every refusal is a ValueError with a plain message. Without the guards
    # below, tuple() would raise TypeError on a bare number or None, split a
    # str into digit characters, and turn bytes b"280000" into the ramp
    # (50, 56, 48, 48, 48, 48) — whole positive numbers that would pass.
    if sizes is None:
        raise ValueError("sizes is missing: give a list of OG batch sizes, "
                         "e.g. [280000]")
    if isinstance(sizes, numbers.Number):
        raise ValueError(f"sizes must be a list of OG batch sizes, not the "
                         f"single value {sizes!r}: wrap it in a list")
    if isinstance(sizes, (str, bytes, bytearray, memoryview)):
        raise ValueError(f"sizes must be a list of whole numbers of fish, not "
                         f"text {sizes!r}")
    if isinstance(sizes, (Set, Mapping)):
        raise ValueError(f"sizes must be an ordered list: a "
                         f"{type(sizes).__name__} has no ramp order")
    try:
        out = tuple(sizes)
    except TypeError:
        raise ValueError(f"sizes must be a list of OG batch sizes, got "
                         f"{type(sizes).__name__} {sizes!r}") from None
    if not out:
        raise ValueError("sizes is empty: give at least one OG batch size")
    for s in out:
        if isinstance(s, bool) or not isinstance(s, numbers.Integral):
            raise ValueError(f"batch size {s!r} is not a whole number of fish "
                             f"(pass an int; a float is refused, not rounded)")
        if s <= 0:
            raise ValueError(f"batch size {s} must be greater than zero")
    return tuple(int(s) for s in out)


def _check_unique(batches: Sequence[BatchInput]) -> None:
    dupes = sorted(i for i, n in Counter(b.batch_id for b in batches).items()
                   if n > 1)
    if dupes:
        raise ValueError(f"duplicate batch ids {dupes}: a result could not say "
                         f"which batch it means")


def _frontier(forecast_start, cutoff) -> dt.date:
    return max(_as_date(forecast_start, "forecast_start"),
               _as_date(cutoff, "cutoff"))


def _has_og(b: BatchInput) -> bool:
    return bool(b.tran_og_count) and b.tran_og_count > 0


def _eligible(batches: Sequence[BatchInput], frontier: dt.date) -> list[BatchInput]:
    """Batches with an OG transfer whose stocking is strictly after `frontier`."""
    out = []
    for b in batches:
        if not _has_og(b):
            continue
        if b.input_date is None:
            raise ValueError(f"batch {b.batch_id} has an OG count but no input "
                             f"date: cannot tell whether it is already stocked")
        if _as_date(b.input_date, f"batch {b.batch_id} input_date") > frontier:
            out.append(b)
    return out


def propose(batches: Sequence[BatchInput], forecast_start, cutoff,
            sizes: Sequence[int]) -> tuple[list[BatchInput], list[Change]]:
    """Re-size every future stocking to the size sequence.

    Eligible: tran_og_count > 0 and input_date strictly after
    max(cutoff, forecast_start). The floor at forecast_start is not optional:
    a batch stocked on or before it is already swimming (in FW if its TranOG
    is still ahead), whatever cutoff the caller passes.

    Eligible batches are taken in (input_date, batch_id) order — input date,
    never TranOG date — and the i-th gets sizes[i], the last size repeating.
    Every eligible batch consumes its slot whether or not it changes: a batch
    already at sizes[i] stays as it is and the next batch gets sizes[i + 1].
    input_count scales by the batch's own input/TranOG ratio; every other
    field is unchanged.

    `sizes` is an ordered list (or tuple) of ints > 0. A float is refused even
    when whole (280000.0): a fractional fish count is a caller bug, not
    something to round away — the app casts to int before calling. A bare
    number, None, a str/bytes, a set or a mapping is refused too, each with a
    plain ValueError.

    Future batches with NO OG count (tran_og_count 0 or None) are not
    eligible: they come back unchanged and are not in `changes`.
    `future_without_og(batches, forecast_start, cutoff)` lists their ids, so
    a caller can show them instead of letting them pass unseen.

    -> (batches in the INPUT order, changes in input-date order). A batch
    already at its target is not a change. The inputs are never mutated.
    """
    sizes = _check_sizes(sizes)
    _check_unique(batches)
    frontier = _frontier(forecast_start, cutoff)

    eligible = sorted(_eligible(batches, frontier),
                      key=lambda b: (_as_date(b.input_date, "input_date"),
                                     b.batch_id))
    resized: dict[str, BatchInput] = {}
    changes: list[Change] = []
    for i, b in enumerate(eligible):
        size = sizes[min(i, len(sizes) - 1)]
        if size == b.tran_og_count:
            continue
        if not b.input_count or b.input_count <= 0:
            raise ValueError(f"batch {b.batch_id} has no input count, so its FW "
                             f"survival is unknown and its eggs cannot be "
                             f"re-sized to match {size:,} to OG")
        new_input = int(round(size * b.input_count / b.tran_og_count))
        resized[b.batch_id] = replace(b, tran_og_count=size,
                                      input_count=new_input)
        changes.append(Change(
            batch_id=b.batch_id, input_date=b.input_date,
            old_tran_og_count=int(b.tran_og_count), new_tran_og_count=size,
            old_input_count=int(b.input_count), new_input_count=new_input))
    return [resized.get(b.batch_id, b) for b in batches], changes


def future_without_og(batches: Sequence[BatchInput], forecast_start,
                      cutoff) -> list[str]:
    """Ids of future batches `propose` skips because they have no OG count.

    The same frontier as `propose` (input_date strictly after
    max(cutoff, forecast_start)) and the complement of its eligibility rule:
    tran_og_count 0, None or negative. With `propose`'s changes (for sizes
    that differ from every batch) this accounts for every future batch.

    A batch with no OG count and no input date cannot be shown to be
    stocked, so it is reported too, after the dated ones. Order:
    (input_date, batch_id).
    """
    _check_unique(batches)
    frontier = _frontier(forecast_start, cutoff)
    found = []
    for b in batches:
        if _has_og(b):
            continue
        if b.input_date is None:
            found.append((1, dt.date.max, b.batch_id))
            continue
        d = _as_date(b.input_date, f"batch {b.batch_id} input_date")
        if d > frontier:
            found.append((0, d, b.batch_id))
    return [bid for _, _, bid in sorted(found)]


def batches_yaml_text(batches: Sequence[BatchInput]) -> str:
    """Exactly what dump_scenario writes to batches.yaml for these batches."""
    return BATCHES_HEADER + yaml.safe_dump(
        {"batches": sio.batches_to_list(list(batches))},
        sort_keys=False, allow_unicode=True, default_flow_style=False)


def _day(d) -> str:
    return _as_date(d, "date").isoformat() if d is not None else ""


def changes_rows(changes: Sequence[Change]) -> list[dict]:
    """One plain dict per change, for a table."""
    return [{
        "Batch": c.batch_id,
        "Input date": _day(c.input_date),
        "OG count (old -> new)": f"{c.old_tran_og_count:,} -> {c.new_tran_og_count:,}",
        "OG change": c.new_tran_og_count - c.old_tran_og_count,
        "Input count (old -> new)": f"{c.old_input_count:,} -> {c.new_input_count:,}",
        "Input change": c.new_input_count - c.old_input_count,
    } for c in changes]


def summarize(batches: Sequence[BatchInput], changes: Sequence[Change]) -> dict:
    """Headline of a proposal. `batches` is either side of it (dates match).

    first_changed_tran_og_date is the earliest week the proposal can touch
    OG: every OG arrival before it is already stocked and cannot move. It is
    taken over the changed batches that HAVE a TranOG date; the ones that do
    not are listed under changed_without_tran_og (and named in the note).
    Both first_changed_* dates are plain dates (a datetime is reduced to its
    day), so a schedule mixing dates and datetimes compares cleanly.
    """
    by_id = {b.batch_id: b for b in batches}
    missing = [c.batch_id for c in changes if c.batch_id not in by_id]
    if missing:
        raise ValueError(f"changes name batches not in the schedule: {missing}")
    without_og = [c.batch_id for c in changes
                  if by_id[c.batch_id].tran_og_date is None]
    first_in: Optional[dt.date] = min(
        (_as_date(c.input_date, f"change {c.batch_id} input_date")
         for c in changes), default=None)
    first_og: Optional[dt.date] = min(
        (_as_date(by_id[c.batch_id].tran_og_date,
                  f"batch {c.batch_id} tran_og_date")
         for c in changes if by_id[c.batch_id].tran_og_date is not None),
        default=None)
    if not changes:
        note = "No future stocking changes: the schedule is already at these sizes."
    else:
        if first_og is not None:
            parts = [f"Nothing that enters OG before {first_og.isoformat()} "
                     f"changes: those fish are already stocked (or inside the "
                     f"cutoff)."]
        else:
            parts = ["None of the re-sized batches has a TranOG date, so the "
                     "first OG week this proposal touches is unknown."]
        parts.append(f"The first re-sized batch is stocked on "
                     f"{first_in.isoformat()}.")
        if without_og and first_og is not None:
            parts.append(f"Re-sized with no TranOG date (when they reach OG is "
                         f"unknown): {', '.join(without_og)}.")
        elif without_og:
            parts.append(f"Re-sized: {', '.join(without_og)}.")
        note = " ".join(parts)
    return {
        "n_changed": len(changes),
        "changed_ids": [c.batch_id for c in changes],
        "first_changed_input_date": first_in,
        "first_changed_tran_og_date": first_og,
        "changed_without_tran_og": without_og,
        "smolt_removed": sum(c.old_tran_og_count - c.new_tran_og_count
                             for c in changes),
        "input_removed": sum(c.old_input_count - c.new_input_count
                            for c in changes),
        "note": note,
    }
