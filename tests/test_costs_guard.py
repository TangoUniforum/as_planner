"""The suite can never write the live config/costs.yaml — the guard's own
negative control (tests/conftest.py, `_live_costs_never_written`).

Each case runs a tiny pytest session in a subprocess with a COPY of the real
conftest.py beside it and COSTS_GUARD_PATH pointed at a tmp file, so the
guard is proved to fire without the operator's file ever being in reach:

  * a test that writes the watched file directly -> the SESSION fails;
  * a test that calls forecast.costs.save_costs on the watched dir -> the
    call raises RuntimeError at once, the file is never created;
  * a test that only reads an existing file -> no false alarm;
  * save_costs into any other dir still works.

Every number here is made up.
"""
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_COSTS = ("{'schema': 1, 'fixed_monthly': 10.0, 'oxygen_per_kg_feed': 0.5, "
          "'chemicals_per_kg_feed': 0.25, 'feed_shipping_per_kg': 0.125, "
          "'egg_price': 0.01, 'feed_prices': {'Made-up 1.0': "
          "{'item': '', 'price_per_kg': 2.0}}}")


def _session(tmp_path, body, existing=None):
    d = tmp_path / "guard"
    d.mkdir()
    shutil.copy(ROOT / "tests" / "conftest.py", d / "conftest.py")
    (d / "test_case.py").write_text(textwrap.dedent(body), encoding="utf-8")
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    watched = cfg / "costs.yaml"
    if existing is not None:
        watched.write_text(existing, encoding="utf-8")
    env = dict(os.environ, COSTS_GUARD_PATH=str(watched),
               FW_CALIB_GUARD_PATH=str(tmp_path / "calib.jsonl"),
               PYTHONPATH=str(ROOT) + os.pathsep
               + os.environ.get("PYTHONPATH", ""))
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         str(d), "--rootdir", str(d), f"--basetemp={tmp_path / 'bt'}"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
        timeout=300)
    return p, watched


def test_a_direct_write_fails_the_session(tmp_path):
    p, watched = _session(tmp_path, """
        import os
        def test_writes():
            with open(os.environ["COSTS_GUARD_PATH"], "w") as fh:
                fh.write("schema: 1\\n")
        """)
    out = p.stdout + p.stderr
    assert p.returncode != 0, out
    assert "the live costs file changed during the test session" in out, out


def test_save_costs_on_the_live_dir_is_refused_at_the_call(tmp_path):
    p, watched = _session(tmp_path, f"""
        import os
        import pytest
        from forecast import costs as C
        def test_refused():
            live = os.path.dirname(os.environ["COSTS_GUARD_PATH"])
            with pytest.raises(RuntimeError, match="live costs file"):
                C.save_costs(live, {_COSTS})
            assert not os.path.exists(os.environ["COSTS_GUARD_PATH"])
        def test_another_dir_still_saves(tmp_path):
            C.save_costs(tmp_path, {_COSTS})
            assert C.load_costs(tmp_path)["fixed_monthly"] == 10.0
        """)
    out = p.stdout + p.stderr
    assert p.returncode == 0, out
    assert "2 passed" in out, out
    assert not watched.exists()


def test_reading_an_existing_file_is_no_false_alarm(tmp_path):
    p, watched = _session(tmp_path, """
        import os
        def test_reads():
            with open(os.environ["COSTS_GUARD_PATH"]) as fh:
                assert fh.read()
        """, existing="schema: 1\n")
    out = p.stdout + p.stderr
    assert p.returncode == 0, out
    assert watched.read_text(encoding="utf-8") == "schema: 1\n"
