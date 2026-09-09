"""Operator-facing surfaces must NAME the method, never print its registry key.

THE INCIDENT (2026-09-08). The Quick-run card rendered

    ⚡ Promoted default — `controller` · hybrid_follow=off, chronic_pressure...

and the operator read it as the hybrid arm and reported the promotion as
broken. It was not: `controller` IS "Controller — reactive greedy", and
`hybrid_follow=off` is the knob that DISABLES the hybrid guide. The card was
correct and unreadable at the same time — the key and one of the knobs both
contain the word "hybrid", and the only word naming the actual method was a
lowercase key that looks like a placeholder.

That cost a round-trip on a decision the operator had already made correctly,
and it is the second time in one day that the method a run would use was hard
to see (the first: a promoted default that could not run at all, fixed in
59e3c3c). A card that is right but reads as wrong is a defect in the card.

The registry label is the name the operator chose FROM on the board, so it is
the name they should see everywhere afterwards. The key stays as a small tag
so a card still ties to analysis_defaults.yaml and adoption_history.jsonl.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("streamlit")

_APP = Path(__file__).resolve().parent.parent / "app.py"
_SRC = _APP.read_text(encoding="utf-8")


def test_the_promoted_card_shows_the_label():
    """The line that names the promoted default must use the method LABEL."""
    m = re.search(r'\*\*⚡ Promoted default\*\* — (.{0,60})', _SRC)
    assert m, "the promoted-default card line was renamed — update this guard"
    assert ".label" in _SRC[m.start():m.start() + 260], (
        "the promoted-default card names the method by registry key. Print "
        "_method_obj(promoted['method']).label — `controller` reads as a "
        "placeholder, and next to hybrid_follow=off it reads as the hybrid.")


def test_the_run_page_caption_shows_the_label():
    m = re.search(r'st\.caption\(f"Method: (.{0,60})', _SRC)
    assert m, "the Run-page method caption was renamed — update this guard"
    assert ".label" in m.group(1), "the Run page must name the method, not key it"


def test_every_registry_label_is_distinct_from_its_key():
    """A label that merely echoes the key would defeat the point."""
    from forecast.methods import REGISTRY
    for key, meth in REGISTRY.items():
        assert meth.label and meth.label != key, key
        assert len(meth.label) > len(key), (
            f"{key}: label {meth.label!r} is not more informative than the key")


def test_the_two_controller_arms_are_not_confusable_by_name():
    """The incident in one assertion: these must not read alike."""
    from forecast.methods import REGISTRY
    plain = REGISTRY["controller"].label
    hybrid = REGISTRY["controller-hybrid"].label
    assert "hybrid" not in plain.lower(), (
        f"the plain controller's label {plain!r} contains 'hybrid' — the very "
        f"confusion this guard exists to prevent")
    assert "hybrid" in hybrid.lower()
