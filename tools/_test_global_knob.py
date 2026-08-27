"""Can GLOBAL be tuned at all? Test the 2026-08-07 conservation exclusion.

methods.GLOBAL_KNOB_SPACE is empty because overriding facility_biomass_deviation_pct
(and density_target_pct) "was experimentally shown to BREAK Global's conservation
proof (2026-08-07)". That finding predates three weeks of conservation work --
check_global_invariants.py as a hard gate, TankContinuityAudit on every run, and
several conservation bugs fixed since.

If the break no longer happens, Global gets a real knob search and the tournament
stops comparing a TUNED controller against a STOCK Global.

density_target_pct is not tested: it is now an untunable operator input.

    python -m tools._test_global_knob <PR.xlsx> <out-dir> <value>
"""
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import yaml                                                     # noqa: E402


def main() -> int:
    pr, out, val = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    out.mkdir(parents=True, exist_ok=True)
    cfg = out / "config"
    shutil.rmtree(cfg, ignore_errors=True)
    shutil.copytree(REPO / "config", cfg)
    ctl = cfg / "control.yaml"
    d = yaml.safe_load(ctl.read_text(encoding="utf-8"))
    before = d.get("facility_biomass_deviation_pct")
    if val != "stock":
        d["facility_biomass_deviation_pct"] = float(val)
        ctl.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
    print(f"facility_biomass_deviation_pct: {before} -> "
          f"{d.get('facility_biomass_deviation_pct')}", flush=True)

    import time
    from tools.run_global_forecast import run_global
    t0 = time.time()
    rc = run_global(pr, str(out / f"global_{val}.xlsx"),
                    config_dir=str(cfg), scenario_dir=str(REPO / "scenario"))
    print(f"rc={rc}  {time.time() - t0:.0f}s", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
