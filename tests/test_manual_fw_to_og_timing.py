"""A manual FW->OG must draw its cohort at the week's OPEN, not its close.

THE DEFECT (2026-09-08). `manual_window._build_fw_lookup` indexed each
freshwater week by `s.close_count, s.close_avg_weight_g`. A manual `fw_to_og`
event in window week W then deposited the cohort into OG at the START of W --
and the window promptly grew it through W again. One freshwater week, grown
twice.

Measured on the live plan: B49 arrived 8,431 kg heavy at the 2026-W36 stitch,
+6.4% of the batch, compounding to 9,422 kg by W38, with the fish COUNT
unchanged (287,599 in both) -- the signature of weight created rather than fish
added.

WHY NO AUDIT CAUGHT IT, which is the part worth remembering:
  * TankContinuityAudit and ReconciliationReport book the arrival as an
    EXOGENOUS input at whatever weight it carries -- Delta_kg 0 by construction.
  * InputConservationAudit's FW balance for a MANUAL transfer is arithmetically
    self-referential: the same count is used as both the base and the placement,
    so the residual is 0 whatever the weight.
  * The Weekly/Monthly ledgers say it in their own header: "Bio_Check is 0 by
    construction."
Three conservation surfaces, none of which can see biomass invented at an
exogenous boundary. Only a check that compares the arrival against the FW
record it came from would show it.

THE RULE. The automatic path already does this correctly: `biology.py:1141`
makes the FW->SW split at the first forecast-week boundary on/after
tran_og_date, using the state as of that DAY -- the week's opening. The manual
path now matches it, so a scripted transfer and an automatic one in the same
week deliver the same fish at the same weight.
"""
from __future__ import annotations

from types import SimpleNamespace

import forecast.manual_window as mw
from forecast.manual_events import TYPE_FW_TO_OG


def _state(week, open_c, open_w, close_c, close_w, stage="FW"):
    return SimpleNamespace(week_label=week, stage=stage,
                           open_count=open_c, open_avg_weight_g=open_w,
                           close_count=close_c, close_avg_weight_g=close_w)


def _lookup(monkeypatch, states):
    monkeypatch.setattr(mw, "_build_fw_lookup", mw._build_fw_lookup)  # explicit
    import forecast.biology as bio
    monkeypatch.setattr(bio, "project_in_flight_fw_batch",
                        lambda *a, **k: (states, None, None))
    ev = SimpleNamespace(type=TYPE_FW_TO_OG, batch="B49")
    rec = SimpleNamespace(batch_id="B49", closing_count=250225.0,
                          closing_biomass_kg=92350.0)
    meta = SimpleNamespace(tran_og_cv=16.0)
    return mw._build_fw_lookup([ev], [rec], SimpleNamespace(), None,
                               SimpleNamespace(), {"B49": meta})


def test_the_cohort_is_taken_at_the_week_open(monkeypatch):
    """The incident: close was 8,431 kg heavier than open on the stitch week."""
    got = _lookup(monkeypatch, [_state("2026-W36", 250000.0, 300.0,
                                       249900.0, 330.0)])
    count, wt, cv = got[("B49", "2026-W36")]
    assert wt == 300.0, "must be the OPENING weight, not the close"
    assert count == 250000.0
    assert cv == 16.0


def test_it_never_takes_the_heavier_close(monkeypatch):
    """NEGATIVE CONTROL — a growing week must show a real difference."""
    got = _lookup(monkeypatch, [_state("2026-W36", 100.0, 10.0, 99.0, 12.0)])
    assert got[("B49", "2026-W36")][1] != 12.0


def test_a_state_with_no_open_falls_back_to_close(monkeypatch):
    """Older states may not populate the open fields; must not index 0 g."""
    got = _lookup(monkeypatch, [_state("2026-W36", 0.0, 0.0, 99.0, 12.0)])
    assert got[("B49", "2026-W36")] == (99.0, 12.0, 16.0)


def test_only_freshwater_weeks_are_indexed(monkeypatch):
    """A manual FW->OG must happen while the batch is still in freshwater."""
    got = _lookup(monkeypatch, [
        _state("2026-W36", 100.0, 10.0, 99.0, 11.0),
        _state("2026-W37", 99.0, 11.0, 98.0, 12.0, stage="SW"),
    ])
    assert ("B49", "2026-W36") in got
    assert ("B49", "2026-W37") not in got
