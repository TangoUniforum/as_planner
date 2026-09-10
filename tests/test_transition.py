"""forecast.transition — behavioural invariants, never pinned numbers.

What is asserted: fish already stocked are never touched whatever cutoff is
passed, re-sized batches land on the target with their own FW survival and
their dates intact, a size ramp follows INPUT-date order (not list order, not
TranOG order) and every eligible batch consumes its slot even when it is
already at target, the caller's schedule is never mutated, bad sizes are
refused with a ValueError, future batches with no OG count are reported
rather than skipped unseen, the summary survives mixed date/datetime input
and names changed batches with no TranOG date, and the download text is the
same format the app's own Save writes.

The synthetic ramps below are contract inputs, not model output: they check
WHICH batch gets which slot, not what any run produces.
"""
import copy
import datetime as dt
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from forecast import scenario_io as sio
from forecast import transition as tr
from forecast.models import BatchInput

ROOT = Path(__file__).resolve().parents[1]
FS = dt.date(2026, 9, 1)            # the 8/31 PR's forecast start
CUTOFF = dt.date(2026, 10, 1)
RAMP = [240_000, 240_000, 240_000, 280_000]
STEPS = [200_000, 250_000, 300_000]  # all distinct, so a slot slip shows
EARLY = dt.date(2026, 7, 1)          # before every _stream input date


def _load_live():
    return sio.load_batches(str(ROOT / "scenario"))


@pytest.fixture
def live():
    # Function-scoped on purpose: a module-scoped schedule is shared, so a
    # propose that mutated it in place would rewrite it for every later test.
    return _load_live()


def _batch(bid, input_date, og=340_000, inp=570_000):
    d = dt.datetime.combine(input_date, dt.time())
    return BatchInput(
        batch_id=bid, input_date=d, input_count=inp,
        tran_sf_date=d + dt.timedelta(days=81),
        tran_og_date=d + dt.timedelta(days=350),
        tran_og_count=og, tran_og_avg_wt_g=370.0, tran_og_cv=16.0,
        fcr_model="FCR_116_Quick", fw_correction=1.0, sgr_correction=1.0)


def _stream(n=8, start=dt.date(2026, 8, 1), gap=49):
    return [_batch(f"X{i:02d}", start + dt.timedelta(days=gap * i))
            for i in range(n)]


def _d(x):
    return x.date() if isinstance(x, dt.datetime) else x


# --- the frontier ------------------------------------------------------------

@pytest.mark.parametrize("sizes", [[280_000], RAMP])
def test_stocked_batches_are_untouched(live, sizes):
    out, _ = tr.propose(live, FS, CUTOFF, sizes)
    for before, after in zip(live, out):
        if _d(before.input_date) <= CUTOFF:
            assert after is before
            assert after == before


def test_a_cutoff_before_forecast_start_never_reaches_stocked_fish(live):
    early = dt.date(2020, 1, 1)
    out, changes = tr.propose(live, FS, early, [100_000])
    changed = {c.batch_id for c in changes}
    for before, after in zip(live, out):
        if _d(before.input_date) <= FS:
            assert after is before
            assert before.batch_id not in changed
    assert changes, "every future batch differs from 100k, so some must change"
    assert all(_d(c.input_date) > FS for c in changes)


def test_the_frontier_is_strict(live):
    on_the_day = [b for b in live if b.tran_og_count][5]
    out, changes = tr.propose([on_the_day], _d(on_the_day.input_date),
                              _d(on_the_day.input_date), [1_000])
    assert out[0] is on_the_day and not changes


def test_datetime_and_date_frontiers_agree(live):
    as_date = tr.propose(live, FS, CUTOFF, RAMP)
    as_dt = tr.propose(live, dt.datetime.combine(FS, dt.time()),
                       dt.datetime.combine(CUTOFF, dt.time(23, 59)), RAMP)
    assert as_date == as_dt


# --- the re-size -------------------------------------------------------------

@pytest.mark.parametrize("sizes", [[280_000], RAMP])
def test_resized_batches_hit_the_target_with_their_own_survival(live, sizes):
    out, changes = tr.propose(live, FS, CUTOFF, sizes)
    assert changes
    by_id = {b.batch_id: b for b in live}
    new_by_id = {b.batch_id: b for b in out}
    for c in changes:
        old, new = by_id[c.batch_id], new_by_id[c.batch_id]
        assert new.tran_og_count == c.new_tran_og_count
        assert new.input_count == c.new_input_count
        # the batch's own input/OG ratio, to within one fish of rounding
        assert abs(new.input_count - new.tran_og_count * old.input_count
                   / old.tran_og_count) <= 0.5
        for f in ("input_date", "tran_sf_date", "tran_og_date",
                  "tran_og_avg_wt_g", "tran_og_cv", "fcr_model",
                  "fw_correction", "sgr_correction", "notes"):
            assert getattr(new, f) == getattr(old, f)


def test_the_input_schedule_is_never_mutated():
    # Its OWN schedule, loaded here, never a shared fixture: if propose ever
    # mutated in place, earlier tests would already have rewritten a shared
    # copy to these sizes, propose would find nothing to change, and this
    # test would compare a mutated schedule with itself and pass.
    mine = _load_live()
    snapshot = copy.deepcopy(mine)
    _, changes = tr.propose(mine, FS, CUTOFF, RAMP)
    assert changes, "the proposal must change something or this proves nothing"
    assert mine == snapshot
    assert mine == _load_live()                     # still what is on disk

    s = _stream()
    snapshot = copy.deepcopy(s)
    _, changes = tr.propose(s, EARLY, EARLY, STEPS)
    assert changes and s == snapshot


def test_output_keeps_the_input_order(live):
    shuffled = live[::-1]
    out, _ = tr.propose(shuffled, FS, CUTOFF, RAMP)
    assert [b.batch_id for b in out] == [b.batch_id for b in shuffled]


def test_a_batch_already_at_target_is_not_a_change():
    s = _stream()
    out, changes = tr.propose(s, EARLY, EARLY, [340_000])
    assert changes == [] and out == s
    assert all(a is b for a, b in zip(out, s))


def test_batches_without_an_og_count_are_left_alone():
    s = _stream()
    s[4] = _batch("NOOG", dt.date(2027, 6, 1), og=0)
    out, changes = tr.propose(s, EARLY, EARLY, [200_000])
    assert out[4] is s[4]
    assert "NOOG" not in {c.batch_id for c in changes}


# --- the ramp ----------------------------------------------------------------

def test_ramp_follows_input_date_order_whatever_the_list_order():
    s = _stream(n=8)
    orders = [s, s[::-1], s[1::2] + s[::2]]
    results = [tr.propose(o, EARLY, EARLY, RAMP) for o in orders]
    for order, (out, changes) in zip(orders, results):
        got = {b.batch_id: b.tran_og_count for b in out}
        in_date_order = sorted(order, key=lambda b: (b.input_date, b.batch_id))
        want = [RAMP[min(i, len(RAMP) - 1)] for i in range(len(in_date_order))]
        assert [got[b.batch_id] for b in in_date_order] == want
        assert [c.batch_id for c in changes] == [b.batch_id
                                                 for b in in_date_order]
    # same changes, same resulting batches, whatever the list order
    assert results[0][1] == results[1][1] == results[2][1]
    assert ({b.batch_id: b for b in results[0][0]}
            == {b.batch_id: b for b in results[1][0]})


def test_ramp_follows_input_date_not_tranog_date():
    # Stocked first but reaches OG last (a long FW stay), and the reverse.
    # The ids sort the other way too, so neither a TranOG-date nor an id
    # ordering can reproduce the input-date answer by accident.
    first_in = _batch("LATE_OG", dt.date(2027, 1, 1))
    second_in = replace(_batch("EARLY_OG", dt.date(2027, 2, 1)),
                        tran_og_date=dt.datetime(2027, 6, 1))
    assert first_in.input_date < second_in.input_date
    assert first_in.tran_og_date > second_in.tran_og_date
    assert first_in.batch_id > second_in.batch_id
    for order in ([first_in, second_in], [second_in, first_in]):
        out, changes = tr.propose(order, EARLY, EARLY, STEPS[:2])
        got = {b.batch_id: b.tran_og_count for b in out}
        assert got == {"LATE_OG": STEPS[0], "EARLY_OG": STEPS[1]}
        assert [c.batch_id for c in changes] == ["LATE_OG", "EARLY_OG"]


@pytest.mark.parametrize("at", [0, 1])
def test_a_batch_already_at_target_still_consumes_its_ramp_slot(at):
    # Eligible batch `at` already holds STEPS[at]: it is not a change, but it
    # keeps its slot, so every later batch still gets the next step.
    s = _stream(n=len(STEPS) + 1)
    s[at] = replace(s[at], tran_og_count=STEPS[at])
    out, changes = tr.propose(s, EARLY, EARLY, STEPS)
    want = [STEPS[min(i, len(STEPS) - 1)] for i in range(len(s))]
    assert [b.tran_og_count for b in out] == want
    assert out[at] is s[at]
    assert [c.batch_id for c in changes] == [b.batch_id for i, b in enumerate(s)
                                             if i != at]


def test_ramp_counts_only_eligible_batches():
    s = _stream(n=8)
    fs = _d(s[2].input_date)                    # X00..X02 are stocked
    out, changes = tr.propose(s, fs, fs, RAMP)
    assert [b.tran_og_count for b in out[:3]] == [340_000] * 3
    assert [b.tran_og_count for b in out[3:]] == [240_000] * 3 + [280_000] * 2
    assert changes[0].batch_id == "X03"


# --- refusals ----------------------------------------------------------------

@pytest.mark.parametrize("bad", [[], [0], [-5], [280_000, 0], [280_000.0],
                                 [True], ["280000"], [2.5e5]])
def test_invalid_sizes_are_refused(live, bad):
    with pytest.raises(ValueError):
        tr.propose(live, FS, CUTOFF, bad)


@pytest.mark.parametrize("bad", [280_000, 280_000.0, True, None, "280000",
                                 b"280000", bytearray(b"280000"), {280_000},
                                 {280_000: 1}, object()],
                         ids=lambda b: type(b).__name__)
def test_a_sizes_value_that_is_not_a_list_is_a_plain_value_error(live, bad):
    # ValueError, never TypeError, and bytes are never read as a ramp of
    # their byte values (b"280000" iterates as 50, 56, 48, ...).
    with pytest.raises(ValueError, match="sizes"):
        tr.propose(live, FS, CUTOFF, bad)


def test_any_ordered_sequence_of_ints_is_accepted(live):
    want = tr.propose(live, FS, CUTOFF, RAMP)
    assert tr.propose(live, FS, CUTOFF, tuple(RAMP)) == want
    assert tr.propose(live, FS, CUTOFF, iter(RAMP)) == want


def test_duplicate_batch_ids_are_refused():
    s = _stream(n=3)
    s.append(s[1])
    with pytest.raises(ValueError):
        tr.propose(s, EARLY, EARLY, [280_000])
    with pytest.raises(ValueError):
        tr.future_without_og(s, EARLY, EARLY)


def test_a_future_batch_without_an_input_count_is_refused():
    s = _stream(n=3)
    s[2] = _batch("NOIN", dt.date(2027, 3, 1), inp=0)
    with pytest.raises(ValueError, match="NOIN"):
        tr.propose(s, EARLY, EARLY, [280_000])


def test_a_non_date_frontier_is_refused(live):
    with pytest.raises(ValueError):
        tr.propose(live, "2026-09-01", CUTOFF, [280_000])
    with pytest.raises(ValueError):
        tr.future_without_og(live, "2026-09-01", CUTOFF)


# --- future batches with no OG count -----------------------------------------

def test_future_batches_without_an_og_count_are_reported():
    s = _stream(n=6)
    fs = dt.date(2026, 9, 1)
    s[0] = _batch("PAST_NOOG", dt.date(2026, 6, 1), og=0)       # stocked
    s[2] = _batch("FUT_ZERO", dt.date(2027, 6, 1), og=0)
    s[3] = _batch("FUT_NONE", dt.date(2027, 3, 1), og=None)
    s[4] = _batch("FUT_NEG", dt.date(2027, 9, 1), og=-1)
    s.append(replace(_batch("NO_DATE", dt.date(2027, 1, 1), og=0),
                     input_date=None))
    reported = tr.future_without_og(s, fs, fs)
    assert set(reported) == {"FUT_ZERO", "FUT_NONE", "FUT_NEG", "NO_DATE"}
    dated = [b for b in s if b.batch_id in reported and b.input_date]
    assert reported[:len(dated)] == [b.batch_id for b in
                                     sorted(dated, key=lambda b: b.input_date)]
    assert reported[-1] == "NO_DATE"

    out, changes = tr.propose(s, fs, fs, [1])   # no batch is 1 fish: all change
    changed = {c.batch_id for c in changes}
    future = {b.batch_id for b in s
              if b.input_date is None or _d(b.input_date) > fs}
    assert changed.isdisjoint(reported)
    assert changed | set(reported) == future
    for before, after in zip(s, out):
        if before.batch_id in reported:
            assert after is before

    # the frontier is max(cutoff, forecast_start), as in propose
    later = tr.future_without_og(s, fs, dt.date(2027, 4, 1))
    assert "FUT_NONE" not in later and "FUT_ZERO" in later


def test_every_live_future_batch_is_resized_or_reported(live):
    _, changes = tr.propose(live, FS, CUTOFF, [1])
    changed = {c.batch_id for c in changes}
    reported = tr.future_without_og(live, FS, CUTOFF)
    future = {b.batch_id for b in live
              if b.input_date is None or _d(b.input_date) > CUTOFF}
    assert len(reported) == len(set(reported))
    assert changed.isdisjoint(reported)
    assert changed | set(reported) == future


# --- the download text -------------------------------------------------------

def test_yaml_text_round_trips(live):
    out, _ = tr.propose(live, FS, CUTOFF, RAMP)
    text = tr.batches_yaml_text(out)
    assert sio.batches_from_list(yaml.safe_load(text)["batches"]) == out


def test_yaml_text_is_what_the_apps_save_writes(live, tmp_path):
    from forecast.caps import FacilityLimits, SystemLimits
    sio.dump_scenario(tmp_path, batches=live, facility_limits=FacilityLimits(),
                      system_limits=SystemLimits())
    saved = (tmp_path / sio.BATCHES_FILE).read_text(encoding="utf-8")
    assert tr.batches_yaml_text(live) == saved


def test_unchanged_live_scenario_text_equals_the_live_file(live):
    text = tr.batches_yaml_text(live)
    on_disk = (ROOT / "scenario" / sio.BATCHES_FILE).read_bytes().decode("utf-8")
    assert text.startswith(tr.BATCHES_HEADER)
    assert text[len(tr.BATCHES_HEADER):] == on_disk[len(tr.BATCHES_HEADER):]
    assert text == on_disk


# --- table + summary ---------------------------------------------------------

def test_changes_rows_and_summary_agree(live):
    out, changes = tr.propose(live, FS, CUTOFF, RAMP)
    rows = tr.changes_rows(changes)
    assert [r["Batch"] for r in rows] == [c.batch_id for c in changes]
    s = tr.summarize(out, changes)
    assert s["n_changed"] == len(changes) == len(rows)
    assert s["changed_ids"] == [c.batch_id for c in changes]
    assert s["smolt_removed"] == -sum(r["OG change"] for r in rows)
    by_id = {b.batch_id: b for b in out}
    assert s["first_changed_tran_og_date"] == min(
        _d(by_id[c.batch_id].tran_og_date) for c in changes)
    assert s["first_changed_input_date"] == min(_d(c.input_date)
                                                for c in changes)
    assert s["first_changed_input_date"] > CUTOFF
    assert s["changed_without_tran_og"] == [
        c.batch_id for c in changes if by_id[c.batch_id].tran_og_date is None]
    assert s["note"]
    assert tr.summarize(live, changes) == s        # either side of the proposal


def test_summary_of_no_changes_says_so(live):
    s = tr.summarize(live, [])
    assert s["n_changed"] == 0 and s["smolt_removed"] == 0
    assert s["first_changed_tran_og_date"] is None
    assert s["changed_without_tran_og"] == []
    assert s["note"]


def test_summary_refuses_changes_for_unknown_batches(live):
    _, changes = tr.propose(live, FS, CUTOFF, RAMP)
    with pytest.raises(ValueError):
        tr.summarize(live[:1], changes)


def test_summary_of_mixed_dates_and_datetimes():
    s = _stream(n=4)
    s[2] = replace(s[2], input_date=_d(s[2].input_date),
                   tran_og_date=_d(s[2].tran_og_date))        # plain dates
    out, changes = tr.propose(s, EARLY, EARLY, [200_000])
    summary = tr.summarize(out, changes)
    assert summary["first_changed_input_date"] == min(_d(b.input_date)
                                                      for b in s)
    assert summary["first_changed_tran_og_date"] == min(_d(b.tran_og_date)
                                                        for b in s)


def test_summary_names_changed_batches_without_a_tranog_date():
    s = _stream(n=4)
    s[2] = replace(s[2], tran_og_date=None)
    out, changes = tr.propose(s, EARLY, EARLY, [200_000])
    assert "X02" in {c.batch_id for c in changes}
    summary = tr.summarize(out, changes)
    assert summary["changed_without_tran_og"] == ["X02"]
    assert summary["first_changed_tran_og_date"] == min(
        _d(b.tran_og_date) for b in s if b.tran_og_date is not None)
    assert "X02" in summary["note"]


def test_summary_when_no_changed_batch_has_a_tranog_date():
    s = [replace(b, tran_og_date=None) for b in _stream(n=3)]
    out, changes = tr.propose(s, EARLY, EARLY, [200_000])
    summary = tr.summarize(out, changes)
    assert summary["first_changed_tran_og_date"] is None
    assert summary["changed_without_tran_og"] == [c.batch_id for c in changes]
    note = summary["note"]
    assert "before  " not in note and "before ." not in note   # no blank date
    assert all(c.batch_id in note for c in changes)
    assert summary["first_changed_input_date"].isoformat() in note
