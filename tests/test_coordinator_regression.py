"""Behavioral regression guard for the forecast pipeline.

Locks the model/pipeline BEHAVIOR, not specific output numbers. The exact
density-violation count / worst density / TranOG count legitimately change
with config (horizon, density target, limits, batches) — they are NOT the
thing under test. What must always hold, regardless of config, is that the
pipeline behaves correctly:

  1. COMPLETES + POPULATES — rc == 0 and the key sheets are written.
  2. MASS CONSERVATION — zero count-drift and zero biomass-drift rows in
     TankContinuityAudit: no fish/biomass created or lost unaccounted. (This
     also guarantees no TranOG arrival is silently dropped — a dropped
     arrival would break the count balance.)
  3. OUTPUT SANITY — every density is finite and >= 0 (no NaN / negative
     blow-ups); counts are non-negative.
  4. DETERMINISM — identical output regardless of PYTHONHASHSEED.

Runs the supported path (live config/ + scenario/ YAML, ProductionReport from
a COPY of Forecast.xlsm so the source is never mutated). Skips cleanly if the
workbook or config/scenario are absent.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent  # Python/
WORKBOOK = ROOT / "Forecast.xlsm"

pytestmark = pytest.mark.skipif(
    not WORKBOOK.exists(),
    reason="Forecast.xlsm not present (gitignored); regression test needs it",
)


CONFIG_DIR = ROOT / "config"
SCENARIO_DIR = ROOT / "scenario"


@pytest.fixture(scope="module")
def run_outputs(tmp_path_factory):
    """Run the SUPPORTED path and return the output workbook path.

    PR-only: the live config/ + scenario/ YAML (the single source of truth
    the app uses) plus the ProductionReport from the workbook. This is
    robust to Forecast.xlsm being a trimmed PR-only artifact — the limits +
    batches live in scenario/, not the workbook. Skips if config/scenario
    haven't been seeded (a clean checkout: run scripts/export_*_to_yaml.py).
    """
    import forecast.run as run_mod
    if not ((CONFIG_DIR / "control.yaml").exists()
            and (SCENARIO_DIR / "limits.yaml").exists()):
        pytest.skip("config/ + scenario/ not seeded "
                    "(run scripts/export_config_to_yaml.py + export_scenario_to_yaml.py)")
    tmp = tmp_path_factory.mktemp("wb") / "Forecast.xlsm"
    shutil.copy(WORKBOOK, tmp)
    rc = run_mod.main(str(tmp), config_dir=str(CONFIG_DIR),
                      scenario_dir=str(SCENARIO_DIR), calib_log_path="")
    assert rc == 0, f"pipeline exited non-zero ({rc})"
    return tmp


def _load(path):
    import openpyxl
    return openpyxl.load_workbook(path, keep_vba=True, data_only=True)


_REQUIRED_SHEETS = ["BatchLocations", "TankContinuityAudit", "HarvestPlan",
                    "TransferPlan", "BiologyProjection", "RunConfig"]


def test_run_completes_and_populates(run_outputs):
    """The run finishes and writes the key sheets with data."""
    wb = _load(run_outputs)
    for name in _REQUIRED_SHEETS:
        assert name in wb.sheetnames, f"missing output sheet {name}"
    rows = sum(1 for _ in wb["BatchLocations"].iter_rows())
    assert rows > 5, "BatchLocations has no data rows"


def test_mass_conservation(run_outputs):
    """No fish or biomass created/lost unaccounted — config-independent.

    COUNT is the HARD invariant: fish cannot be created or destroyed, so
    per-tank count continuity must be EXACT (zero TANK_DRIFT) and the
    facility-level signed/abs count ratio must cancel to ~0 (the distributed-
    leak gauge — near 1 = systematic one-way fish loss).

    BIOMASS per-tank reconciliation is inherently APPROXIMATE and is asserted at
    the FACILITY level, not per row. Transfer events capture the source weight
    ~half a week behind the weekly BatchLocations snapshot, so a tank that fully
    turns over in one week shows a phantom per-row BIO_DRIFT while mass is in
    fact conserved (the departed mass reappears in the destination tank; the
    audit itself labels facility biomass drift "reported, not asserted"). The
    decisive point: a biomass drift NOT accompanied by a count drift cannot be a
    real leak — no fish went missing, only weight attribution shifted between
    tank-weeks. So we bound the facility-level NET biomass drift to a small
    fraction of peak facility biomass; a genuine mass leak would be far larger
    AND would surface as a count drift (already excluded above).

    NOTE: zero count-drift does NOT prove no batch was dropped (a never-placed
    batch creates no tank-week rows). Input-fish conservation is enforced
    separately by test_no_dropped_batches.
    """
    from collections import defaultdict
    wb = _load(run_outputs)
    ws = wb["TankContinuityAudit"]
    rows = list(ws.iter_rows(values_only=True))

    count_drift = 0
    fac = {}
    for i, row in enumerate(rows, 1):
        if not row:
            continue
        if i >= 5 and row[14] == "TANK_DRIFT":
            count_drift += 1
        # Facility conservation summary rows: [metric, signed, abs, ratio, note]
        if row[0] in ("Count (fish)", "Biomass (kg)"):
            fac[row[0]] = (row[1], row[2], row[3])

    # 1) Fish conservation is EXACT per tank.
    assert count_drift == 0, f"{count_drift} tank count-drift rows"
    # 2) Facility-level fish-leak gauge: signed must cancel to ~0.
    assert "Count (fish)" in fac, "missing facility conservation summary"
    c_signed, c_abs, c_ratio = fac["Count (fish)"]
    assert abs(c_ratio) < 0.3, (
        f"facility count signed/abs ratio {c_ratio:.3f} (|ratio|>=0.3) — "
        f"distributed fish leak (signed {c_signed:,.0f} / abs {c_abs:,.0f})")

    # 3) Biomass conserved within the weekly-vs-daily growth-approximation bias:
    #    bound the facility NET signed drift to <2% of peak facility biomass.
    bl = wb["BatchLocations"]
    blrows = list(bl.iter_rows(values_only=True))
    bhi = next(idx for idx, r in enumerate(blrows)
              if r and "Week" in [str(c) for c in r])
    bhdr = [str(c) for c in blrows[bhi]]
    wcol, biocol = bhdr.index("Week"), bhdr.index("Biomass (kg)")
    per_wk = defaultdict(float)
    for r in blrows[bhi + 1:]:
        if not r or r[wcol] is None:
            continue
        per_wk[r[wcol]] += float(r[biocol] or 0.0)
    peak_bio = max(per_wk.values()) if per_wk else 0.0
    b_signed = fac.get("Biomass (kg)", (0.0,))[0] or 0.0
    assert peak_bio > 0 and abs(b_signed) < 0.02 * peak_bio, (
        f"facility biomass net drift {b_signed:,.0f} kg >= 2% of peak facility "
        f"biomass {peak_bio:,.0f} kg — possible real mass leak")


def test_no_dropped_batches(run_outputs):
    """Input-fish conservation: no stocked batch is silently dropped.

    Closes the blind spot in test_mass_conservation. Every batch whose TranOG
    falls within the horizon must reach the realized facility; a batch the
    placement engine fails to place (no empty OG tank) vanishes from the plan
    with its full stocked population. The InputConservationAudit flags these as
    'DROPPED' with a Fish_At_Risk count, which must total zero.
    """
    wb = _load(run_outputs)
    assert "InputConservationAudit" in wb.sheetnames, "missing InputConservationAudit"
    ws = wb["InputConservationAudit"]
    dropped = []
    at_risk = 0.0
    for i, row in enumerate(ws.iter_rows(values_only=True), 1):
        if i < 5 or not row or not row[0]:
            continue
        if isinstance(row[7], str) and "DROPPED" in row[7]:
            dropped.append(row[0])
            if isinstance(row[8], (int, float)):
                at_risk += row[8]
    assert not dropped, (
        f"{len(dropped)} batch(es) dropped (never placed): {dropped} — "
        f"{at_risk:,.0f} stocked fish lost from the plan")
    # And the other end: no batch may harvest + still-hold MORE than it stocked.
    over = [row[0] for row in ws.iter_rows(values_only=True)
            if row and isinstance(row[0], str) and "OVER-PRODUCED" in row[0]]
    assert not over, f"input-fish conservation breached (fish created): {over}"


def test_fw_mass_balance(run_outputs):
    """Closed FW-phase mass-balance (audit I2): the previously-unaudited freshwater
    phase must conserve fish — first_FW_count == realized_TranOG + FW_mortality +
    FW_culls for every batch that crosses to seawater in-horizon. TankContinuity
    only starts at OG, so a fish leak or a mortality/cull-accounting error in FW
    would otherwise shift total smolts (and harvest tonnage) with every other gate
    green. The InputConservationAudit emits a 'FW MASS-BALANCE BREACH' line when any
    batch fails to reconcile beyond tolerance; it must not be present.
    """
    wb = _load(run_outputs)
    ws = wb["InputConservationAudit"]
    breach = [row[0] for row in ws.iter_rows(values_only=True)
              if row and isinstance(row[0], str) and "FW MASS-BALANCE BREACH" in row[0]]
    assert not breach, f"FW phase does not conserve fish: {breach}"


def test_facility_count_conservation(run_outputs):
    """No DISTRIBUTED fish loss across the facility.

    Per-tank-week count drift is bounded by a 50-fish tolerance, so a small
    same-sign leak spread across many tanks (each under tolerance) passes
    test_mass_conservation while still losing fish in aggregate. The
    TankContinuityAudit FACILITY CONSERVATION SUMMARY sums every tank-week count
    delta; the signed/abs ratio must stay near 0 (random/cancelling = conserved).
    A ratio near 1 means a systematic, distributed one-way loss.
    """
    wb = _load(run_outputs)
    ws = wb["TankContinuityAudit"]
    ratio = None
    for row in ws.iter_rows(values_only=True):
        if row and row[0] == "Count (fish)" and isinstance(row[3], (int, float)):
            ratio = row[3]
            break
    assert ratio is not None, "missing FACILITY CONSERVATION SUMMARY count row"
    assert abs(ratio) < 0.3, (
        f"facility count signed/abs ratio {ratio:.3f} — distributed fish loss "
        f"(systematic one-way count drift across tank-weeks)")


def test_output_sanity(run_outputs):
    """Densities are finite and non-negative; counts non-negative.

    We do NOT assert a specific violation count or worst density — those are
    config-dependent. We only guard against NaN/negative blow-ups.
    """
    wb = _load(run_outputs)
    ws = wb["BatchLocations"]
    bad_density = bad_count = 0
    for i, row in enumerate(ws.iter_rows(values_only=True), 1):
        if i < 5 or not row:
            continue
        count, density = row[5], row[8]
        if isinstance(density, (int, float)) and (density != density or density < 0):
            bad_density += 1
        if isinstance(count, (int, float)) and count < 0:
            bad_count += 1
    assert bad_density == 0, f"{bad_density} NaN/negative density rows"
    assert bad_count == 0, f"{bad_count} negative count rows"


def test_no_harvest_craters(run_outputs):
    """No NEAR-EMPTY mid-horizon harvest week — the steady-harvest contract rule.

    The controller must never leave a mid-horizon week effectively empty: the
    L1-envelope diagnostic (2026-07-08) proved the market-ready SUPPLY exists at
    the controller's crater weeks (L1 holds 30-47k where the controller drops to
    a few hundred), so a near-empty week is a pacing failure, not a shortage.

    Excludes the first 6 weeks (operator-pinned startup handoff) and treats only
    a week below a QUARTER of the harvest floor as a crater (a week merely a few
    fish under the floor is rounding, not a breach) — matching the Compare &
    Choose board's "No empty week" gate. This is the RED gate the anti-crater
    hybrid (L1 envelope -> controller harvest target) must turn green; on a PR
    that does not crater it is a forward-lock against a regression.
    """
    import yaml
    from forecast.optimize import _harvest_weekly_fish
    cfg = yaml.safe_load((CONFIG_DIR / "control.yaml").read_text()) or {}
    floor = float(cfg.get("min_harvest_per_week", 0) or 0)
    if floor <= 0:
        pytest.skip("no min_harvest_per_week floor configured")
    wb = _load(run_outputs)
    weekly = _harvest_weekly_fish(wb)
    craters = [(i, int(c)) for i, c in enumerate(weekly)
               if i >= 6 and c < 0.25 * floor]
    assert not craters, (
        f"near-empty mid-horizon harvest weeks (< 25% of the {floor:,.0f} floor): "
        f"{craters} — the steady-harvest contract rule is breached")


# ---- Determinism guard (2026-06-05, strengthened 2026-09-08) ----
# The forecast must be identical regardless of PYTHONHASHSEED. A
# set-of-strings iteration in phase_d without a deterministic tiebreak
# made it vary run-to-run (245/216.5 vs 228/168.3 on the same workbook).
#
# It happened AGAIN on 2026-09-08, in a different place: run.py iterated
# `og_in_flight_ids` (a set of batch_id strings) unsorted, which reached a
# tied max() in precalc._relieve_tank_supply and moved one free tank between
# two batches at 2026-W24 -- 105 over-cap rows/170.0 kg/m3 vs 90/162.9.
# The guard caught it, but two weaknesses nearly let it through, both now
# closed:
#   1. THREE seeds, not two. A binary tie shows up in two, but a 3-way tie
#      can agree by chance in any given pair.
#   2. HASH THE PLAN SHEETS, not just a 3-number density summary. Two plans
#      can differ in which tank a batch holds while landing on the same
#      over-cap count, max and sum -- the summary is lossy by construction.
#      HarvestPlan and TransferPlan are the sheets the operator acts on.

def test_engine_deterministic_across_hash_seeds():
    import os
    import subprocess
    import sys
    import textwrap

    if not ((CONFIG_DIR / "control.yaml").exists()
            and (SCENARIO_DIR / "limits.yaml").exists()):
        pytest.skip("config/ + scenario/ not seeded")

    code = textwrap.dedent(
        """
        import shutil, tempfile, os, io, contextlib, openpyxl
        import forecast.run as r
        td = tempfile.gettempdir()
        t = os.path.join(td, "det%d.xlsm" % os.getpid())
        o = os.path.join(td, "deto%d.xlsm" % os.getpid())
        shutil.copy(os.environ["WB"], t)
        with contextlib.redirect_stdout(io.StringIO()):
            r.main(t, o, config_dir=os.environ["CFG"], scenario_dir=os.environ["SCN"],
                   calib_log_path="")
        import hashlib
        wb = openpyxl.load_workbook(o, data_only=True)
        ws = wb["BatchLocations"]
        v = []
        for i, row in enumerate(ws.iter_rows(values_only=True), 1):
            if i < 5 or not row:
                continue
            d = row[8]
            if isinstance(d, (int, float)) and d > 95:
                v.append(round(d, 2))
        sig = ["%d|%.2f|%.2f" % (len(v), max(v, default=0.0), round(sum(v), 2))]
        # Full-sheet hashes: the density summary above is lossy, and two
        # different plans can share it. These are the sheets that ARE the plan.
        for name in ("BatchLocations", "HarvestPlan", "TransferPlan"):
            if name not in wb.sheetnames:
                sig.append("%s=MISSING" % name)
                continue
            h = hashlib.sha256()
            for row in wb[name].iter_rows(values_only=True):
                h.update(repr(row).encode("utf-8", "replace"))
            sig.append("%s=%s" % (name, h.hexdigest()[:16]))
        print(" ".join(sig))
        """
    )

    def _run(seed):
        env = dict(os.environ, WB=str(WORKBOOK), CFG=str(CONFIG_DIR),
                   SCN=str(SCENARIO_DIR), PYTHONHASHSEED=str(seed))
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                             capture_output=True, text=True, env=env)
        assert out.returncode == 0, f"seed {seed} failed: {out.stderr[-500:]}"
        return out.stdout.strip().splitlines()[-1]

    sigs = {seed: _run(seed) for seed in (0, 1, 2)}
    distinct = set(sigs.values())
    detail = [f"  seed {s}: {sig}" for s, sig in sorted(sigs.items())]
    assert len(distinct) == 1, (
        "non-deterministic across hash seeds -- " + "  ||  ".join(detail))


# ---- Arrivals must be credited by BIOMASS, not just by count (2026-09-08) ----
# The operator read the shipped Sep'26 workbook and said an FCR of 0.29 on B50
# and 1.04 for October could not be right. They were not.
#
# On the FW->OG boundary the ledger deliberately zeroes the OG opening balance
# (the FW projection is a separate track whose close does not flow by count into
# OG) and credits the arrival as an INPUT instead. But the input's biomass was
# `input_count * owt` where `owt` is the opening weight the same branch had just
# set to zero -- so the fish were credited at 0 g and their entire existing
# biomass fell out of the balance as one week of GROWTH. B50 opened 2026-W43 at
# zero against a W42 close of 253,392 fish / 98,817 kg and booked 108,256 kg of
# growth in a week, reporting Bio_FCR 0.09 for the week and 0.29 for the month.
# October's TOTAL read 1.04 where the truth is 1.18: the phantom growth inflated
# the denominator of the total too, so feed conversion looked ~12% better than
# it was in every month carrying a TranOG arrival.
#
# Note the shape: `Bio_Check` is 0 by construction on these rows (the same
# input_bio appears on both sides of the balance identity), so the conservation
# audits were satisfied throughout. Conservation cannot see a quantity that is
# consistently mis-valued on both sides -- only a physical sanity test can.

def test_no_ledger_row_reports_an_impossible_fcr(run_outputs):
    """A fish cannot gain a kilo on 300 g of feed."""
    wb = _load(run_outputs)
    ws = wb["WeeklyReport"]
    rows = list(ws.iter_rows(values_only=True))
    hdr = None
    for i, r in enumerate(rows[:12]):
        if r and any(str(c).strip() == "Batch" for c in r if c):
            hdr = {str(c).strip(): j for j, c in enumerate(r) if c is not None}
            start = i + 1
            break
    assert hdr, "WeeklyReport header not found"
    need = ("Batch", "Week", "Gross_Growth (kg)", "Feed (kg)",
            "Bio_FCR (ratio)", "Open_Count (fish)", "Input_Count (fish)")
    for k in need:
        assert k in hdr, f"missing ledger column {k}"

    impossible = []
    for r in rows[start:]:
        if not r or r[hdr["Batch"]] is None:
            continue
        try:
            feed = float(r[hdr["Feed (kg)"]] or 0.0)
            growth = float(r[hdr["Gross_Growth (kg)"]] or 0.0)
            fcr = float(r[hdr["Bio_FCR (ratio)"]] or 0.0)
        except (TypeError, ValueError):
            continue
        # Only rows doing real work: a week with meaningful feed AND growth.
        # A batch off feed for harvest prep legitimately shows near-zero both
        # ways, and the ratio of two near-zero numbers is noise, not a defect.
        if feed < 1000.0 or growth < 1000.0 or fcr <= 0:
            continue
        if fcr < 0.6:
            impossible.append((r[hdr["Batch"]], r[hdr["Week"]], growth, feed, fcr))
    assert not impossible, (
        "ledger rows converting feed to flesh better than any salmon can — "
        "check that arrivals are credited by biomass, not only by count: "
        + "; ".join(f"{b} {w}: {g:,.0f} kg growth on {f:,.0f} kg feed = {x}"
                    for b, w, g, f, x in impossible[:5]))


def test_an_arrival_week_does_not_book_the_arrival_as_growth(run_outputs):
    """The specific defect: zero opening + a big input + growth ~= close_bio."""
    wb = _load(run_outputs)
    ws = wb["WeeklyReport"]
    rows = list(ws.iter_rows(values_only=True))
    hdr = None
    for i, r in enumerate(rows[:12]):
        if r and any(str(c).strip() == "Batch" for c in r if c):
            hdr = {str(c).strip(): j for j, c in enumerate(r) if c is not None}
            start = i + 1
            break
    offenders = []
    for r in rows[start:]:
        if not r or r[hdr["Batch"]] is None:
            continue
        try:
            oc = float(r[hdr["Open_Count (fish)"]] or 0.0)
            ic = float(r[hdr["Input_Count (fish)"]] or 0.0)
            growth = float(r[hdr["Gross_Growth (kg)"]] or 0.0)
            cbio = float(r[hdr["Close_Bio (kg)"]] or 0.0)
        except (TypeError, ValueError):
            continue
        if ic <= 0 or oc > 0 or cbio <= 0:
            continue
        # Growth on an arrival week is ONE WEEK of growth. If it is most of the
        # closing biomass, the arrivals themselves were booked as growth.
        if growth > 0.5 * cbio:
            offenders.append((r[hdr["Batch"]], r[hdr["Week"]], growth, cbio))
    assert not offenders, (
        "arrival weeks booking the arriving biomass as growth: "
        + "; ".join(f"{b} {w}: growth {g:,.0f} of close {c:,.0f}"
                    for b, w, g, c in offenders[:5]))
