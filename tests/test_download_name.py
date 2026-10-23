"""The workbook download is named after the run — `app._download_name`.

Operator, 2026-10-22: "would it make sense to change the code so it saves the
file as the run name?" The download is now `<PR stem>_planned_<run name>.<ext>`;
a blank run name keeps the old `<PR stem>_planned.<ext>`, and characters
Windows refuses in a file name become '-'.
"""
from __future__ import annotations

import app

SAVED = "10.18.26_PR_planned.xlsm"
ILLEGAL = '\\/:*?"<>|'


def test_run_name_is_appended():
    got = app._download_name(
        SAVED, "10.18 PR taper W44-45 105-110t to W07 then limits Dec +5.8k")
    assert got == ("10.18.26_PR_planned_10.18 PR taper W44-45 105-110t "
                   "to W07 then limits Dec +5.8k.xlsm")


def test_extension_is_kept():
    assert app._download_name("x_planned.xlsx", "Run A") == "x_planned_Run A.xlsx"


def test_blank_run_name_keeps_old_name():
    for blank in (None, "", "   ", "..."):
        assert app._download_name(SAVED, blank) == SAVED


def test_illegal_characters_are_replaced():
    got = app._download_name(SAVED, 'Q1: 110t / "best" <v2>?')
    assert not any(c in got for c in ILLEGAL)
    assert got == "10.18.26_PR_planned_Q1- 110t - -best- -v2--.xlsm"


def test_negative_control_raw_name_would_be_illegal():
    # Proves the illegal-character test can fail: the same name pasted in
    # unchanged carries characters Windows refuses.
    raw = SAVED[:-5] + "_" + 'Q1: 110t / "best" <v2>?' + ".xlsm"
    assert any(c in raw for c in ILLEGAL)
