"""PR validation is cached on the workbook alone, but it VALIDATES the config.

DEFECT (2026-09-04). `_ingest_pr` keys its cache on the uploaded bytes:

    key = hashlib.md5(uploaded.getvalue()).hexdigest()
    if st.session_state.get("_pr_key") == key:
        return st.session_state["_pr"]

but the result it caches contains config-dependent findings, read from disk:

    fac_ids   = {t.tank_id for t in load_facility_config(CONFIG_DIR).tanks}
    batch_ids = {b.batch_id for b in load_batches(SCENARIO_DIR)}
    ... "PR batches not in config Batches ..." / "PR tank ids not in Facility config"

So with the workbook unchanged, editing facility.yaml or batches.yaml cannot
change the verdict for the rest of the session. Add the missing tank the error
told you to add and the error persists; REMOVE a referenced tank and no error
ever appears. `ok` gates downstream actions, so this is a stale verdict that
authorises a current action.

The precedent is already in the file: `_hydrate_state_from_upload` folds
`_config_fingerprint()` into its key and says why -- "keying on the PR alone
left the manual window validating against pre-edit config for the rest of the
session after any in-app config save". `_ingest_pr` is the one that was missed.

`_pr_key` itself must NOT absorb the fingerprint: `_mw_events` uses it to decide
when the manual-window working set belongs to a different PR, and folding
config into it would blow away unsaved window edits on every config save. The
identity of "which PR" and the identity of "what this verdict was computed
against" are different things and need different keys.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = pytest.importorskip("app", reason="app.py not importable without Streamlit")


class _Upload:
    name = "pr.xlsx"

    def __init__(self, data=b"not-a-real-workbook"):
        self._d = data

    def getvalue(self):
        return self._d


class _FakeSt:
    def __init__(self):
        self.session_state = {}

    def error(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


@pytest.fixture
def counted(monkeypatch):
    """_ingest_pr with the workbook read stubbed out and counted."""
    import forecast.excel_io as _xio
    calls = {"n": 0}

    def _load(*a, **k):
        calls["n"] += 1
        raise ValueError("stub: not a workbook")

    monkeypatch.setattr(_xio, "load_workbook", _load)
    monkeypatch.setattr(app, "st", _FakeSt())
    return calls


def test_the_same_workbook_is_not_reparsed(counted, monkeypatch):
    """NEGATIVE CONTROL. The cache must still work — this is the whole reason
    it exists, and re-parsing a multi-MB workbook on every rerun is why."""
    monkeypatch.setattr(app, "_config_fingerprint", lambda: "cfg-A")
    up = _Upload()
    app._ingest_pr(up)
    app._ingest_pr(up)
    assert counted["n"] == 1, "the PR cache stopped working"


def test_a_config_change_revalidates_the_same_workbook(counted, monkeypatch):
    """THE DEFECT. Same bytes, edited config -> the verdict must be recomputed,
    because the verdict is partly ABOUT the config."""
    up = _Upload()
    monkeypatch.setattr(app, "_config_fingerprint", lambda: "cfg-A")
    app._ingest_pr(up)
    monkeypatch.setattr(app, "_config_fingerprint", lambda: "cfg-B")
    app._ingest_pr(up)
    assert counted["n"] == 2, (
        "the PR verdict was served from cache after the config changed — a "
        "tank/batch mismatch warning (or its absence) is now stale, and `ok` "
        "gates downstream actions")


def test_pr_key_still_identifies_the_PR_alone(counted, monkeypatch):
    """_mw_events scopes the manual-window working set by `_pr_key`. If the
    config fingerprint leaked into it, saving any config setting would look
    like a new PR and discard the operator's unsaved window edits."""
    up = _Upload()
    monkeypatch.setattr(app, "_config_fingerprint", lambda: "cfg-A")
    app._ingest_pr(up)
    first = app.st.session_state.get("_pr_key")
    monkeypatch.setattr(app, "_config_fingerprint", lambda: "cfg-B")
    app._ingest_pr(up)
    assert app.st.session_state.get("_pr_key") == first, (
        "_pr_key moved when only the config changed — the manual window will "
        "reseed and lose unsaved edits on every config save")


def test_a_different_workbook_still_reparses(counted, monkeypatch):
    monkeypatch.setattr(app, "_config_fingerprint", lambda: "cfg-A")
    app._ingest_pr(_Upload(b"one"))
    app._ingest_pr(_Upload(b"two"))
    assert counted["n"] == 2
