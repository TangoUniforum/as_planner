"""Selectable multi-objective production optimizer — a tool beside the tuner.

Sweeps Control knobs and ranks variants on a SELECTABLE, weighted objective, so
the operator can choose what "best" means for a scenario. The objective is to
WALK THE LINE: hold the production targets (biomass, harvest throughput) close to
their limits AND flat — high, stable utilization with no lumps and no breaches —
not to minimize biomass. Components (all "less is better"):

  biomass_overshoot  peak/weeks of facility biomass OVER its cap          -> no breach
  biomass_var        mean per-system CV + facility weekly swing           -> flat
  biomass_util_gap   (cap - mean biomass)/cap                             -> close to the limit
  harvest_var        weekly-harvest fish CV                               -> flat harvest
  harvest_overshoot  fraction of weeks over max_harvest_per_week (55k)    -> no processing breach
  feed_load          mean daily feed (the one cost target, pushed DOWN)   -> minimize
  feed_var           feed CV + swing                                       -> flat feed
  transfers_per_fish avg tank-to-tank moves a fish experiences            -> minimize handling

HARD GATE (never traded): tank continuity + zero unaccounted count/biomass drift
+ all fish accounted (reused from forecast.tuning._conservation). Conservation-
failing variants are excluded from ranking, normalization, and recommendation.

Reuses forecast.tuning's run harness (`_run_in_tempdir`) and conservation gate —
one run harness, two thin metric layers. The transfer/density trade is REAL (the
rebalancer cuts biomass variability by ADDING transfers), so there is no single
optimum — the operator picks the emphasis; nothing is auto-decided.
"""
from __future__ import annotations

import os
import statistics
from dataclasses import dataclass, field

import openpyxl
import yaml

from . import tuning

# Knob grid: (label, {control-knob: value}); baseline first. Every variant
# inherits the caller's config (both leveling defaults — rebalance_level +
# harvest_level_load — ON) and changes only the listed knobs. Built to span the
# real FEED<->HARVEST trade so the emphasis has spread to choose between:
#   - tran_og_default_tanks is the strongest single lever (3 tanks/arrival spreads
#     feed thinner -> fewer feed breaches, but tightens the facility -> bigger
#     make-room harvest dumps; 2 is the reverse). The sweep MUST carry it.
#   - density_target_pct (packing) and the harvest setpoint/K knobs tune each side.
#   - the two CONTROLS — `density-only` (rebalance_level off) and `reactive-harvest`
#     (harvest_level_load off) — let the sweep VERIFY each leveling default earns
#     its keep rather than assuming it.
# NOTE: variants set knobs EXPLICITLY (both endpoints of a lever), never relying
# on the baseline's current value — else if config already sits at one end the
# trade is invisible (e.g. a config at tran_og=3 makes a "tran_og=3" variant a
# no-op duplicate of baseline). baseline = "where your config is now".
OPT_QUICK_GRID = [
    ("baseline", {}),
    ("tran_og=2", {"tran_og_default_tanks": 2}),   # harvest-favoring end
    ("tran_og=3", {"tran_og_default_tanks": 3}),   # feed-favoring end
    ("density-only", {"rebalance_level": False}),
]
OPT_FULL_GRID = [
    ("baseline", {}),
    # tran_og — the strongest feed<->harvest lever; test BOTH ends explicitly,
    # plus 3 paired with a tighter cap band to try to claw harvest back.
    ("tran_og=2", {"tran_og_default_tanks": 2}),
    ("tran_og=3", {"tran_og_default_tanks": 3}),
    ("tran_og=3,dev=0.005", {"tran_og_default_tanks": 3,
                             "facility_biomass_deviation_pct": 0.005}),
    # packing density
    ("density=0.85", {"density_target_pct": 0.85}),
    ("density=0.95", {"density_target_pct": 0.95}),
    # harvest controller — facility_biomass_deviation_pct is now THE "how close to the
    # cap" knob (the dual-limit setpoint runs one band below the binding cap): a TIGHT
    # band runs closer to the cap (more breach risk), a LOOSE one keeps more headroom.
    # harvest_setpoint_lookahead_weeks is vestigial post-redesign, so it is not swept.
    ("dev=0.005 (tight)", {"facility_biomass_deviation_pct": 0.005}),
    ("dev=0.02 (loose)", {"facility_biomass_deviation_pct": 0.02}),
    ("smooth:K12", {"harvest_smooth_lookahead_weeks": 12}),
    # rebalancer effort
    ("balance=60", {"rebalance_balance_budget": 60}),
    ("varqty=20", {"rebalance_varqty_budget": 20}),
    # leveling controls — test BOTH endpoints explicitly so the trade shows up no
    # matter where the baseline config sits (else, if the config already has
    # rebalance_level off, "density-only" is a no-op duplicate of baseline and the
    # genuinely-different ON case is never tried — same lesson as tran_og's two ends).
    ("density-only", {"rebalance_level": False}),     # OFF endpoint
    ("level-rebalance", {"rebalance_level": True}),    # ON endpoint (feed-leveling)
    ("reactive-harvest", {"harvest_level_load": False}),
    ("level-harvest", {"harvest_level_load": True}),
    ("handling:balance=0", {"rebalance_balance_budget": 0}),
    # LNS placement refinement (opt-in) — lets the optimizer decide, per PR, whether
    # the realized relocate/swap pass beats greedy on the chosen emphasis. No-ops
    # (== greedy) when the facility is capacity-bound; helps when there's tank room.
    ("lns-placement", {"placement_method": "lns"}),
]


def opt_grid_for(quick: bool):
    return OPT_QUICK_GRID if quick else OPT_FULL_GRID


# Objective components in display order. All "less is better".
COMPONENTS = [
    "biomass_overshoot", "biomass_var", "biomass_util_gap",
    "harvest_var", "harvest_overshoot", "feed_load", "feed_var",
    "transfers_per_fish",
    "system_overshoot",    # per-system feed+biomass over-cap (compliance)
    "density_overshoot",   # per-tank density over-cap (compliance)
    "system_peak",         # hottest single (system, week) load — the HOT SPOT
]

# Selectable emphasis presets (component -> weight; 0 drops out).
EMPHASIS_PRESETS = {
    # Lumpiness/fluctuation is the headline complaint, so flatness (var) and
    # not-breaching (overshoot) dominate; closeness-to-the-limit (util_gap) is
    # secondary (don't waste capacity, but never at the cost of breaching).
    "Walk the line": {"biomass_var": 3, "harvest_var": 3,
                      "biomass_overshoot": 2, "harvest_overshoot": 2,
                      "system_overshoot": 2, "density_overshoot": 2,
                      "biomass_util_gap": 1,
                      "feed_load": 0.5, "feed_var": 0.5, "transfers_per_fish": 0.5},
    "Flatten biomass": {"biomass_var": 3, "biomass_overshoot": 2, "harvest_var": 2,
                        "biomass_util_gap": 1, "harvest_overshoot": 1,
                        "system_overshoot": 1, "density_overshoot": 1,
                        "feed_var": 0.5, "feed_load": 0.5, "transfers_per_fish": 0.25},
    "Minimize feed": {"feed_load": 3, "feed_var": 2, "system_overshoot": 2,
                      "biomass_var": 1, "biomass_util_gap": 0.5, "biomass_overshoot": 0.5,
                      "density_overshoot": 1,
                      "harvest_var": 0.5, "harvest_overshoot": 0.5,
                      "transfers_per_fish": 0.5},
    "Minimize handling": {"transfers_per_fish": 3, "biomass_var": 1, "harvest_var": 1,
                          "biomass_util_gap": 1, "biomass_overshoot": 1,
                          "harvest_overshoot": 1, "system_overshoot": 1,
                          "density_overshoot": 1, "feed_load": 0.5, "feed_var": 0.5},
    # Respect caps: minimize ALL over-cap excursions (per-system feed/biomass,
    # per-tank density, facility biomass + harvest) above everything else — the
    # emphasis for judging the leveling knob's compliance trade.
    "Respect caps": {"system_overshoot": 3, "density_overshoot": 3,
                     "biomass_overshoot": 2, "harvest_overshoot": 2,
                     "biomass_var": 1, "harvest_var": 1,
                     "feed_load": 0.5, "feed_var": 0.5,
                     "biomass_util_gap": 0.5, "transfers_per_fish": 0.5},
    # Minimize loads / no hot spots: keep every system's biomass+feed as LOW and
    # EVEN as possible (minimize the peak per-system load + all CVs), minimize feed
    # and handling — and explicitly DROP biomass_util_gap, the "press to the cap"
    # reward, because the goal here is the opposite (run cool, lots of headroom).
    "Minimize loads": {"system_peak": 3, "system_overshoot": 2,
                       "feed_load": 2, "feed_var": 2,
                       "biomass_var": 2, "transfers_per_fish": 2,
                       "harvest_var": 1, "harvest_overshoot": 1,
                       "biomass_overshoot": 1, "density_overshoot": 1,
                       "biomass_util_gap": 0},
    "Balanced": {c: 1 for c in COMPONENTS},
}
DEFAULT_EMPHASIS = "Walk the line"


@dataclass
class Metrics:
    # objective components (less is better)
    biomass_overshoot: float
    biomass_var: float
    biomass_util_gap: float
    harvest_var: float
    harvest_overshoot: float
    feed_load: float
    feed_var: float
    transfers_per_fish: float
    system_overshoot: float
    density_overshoot: float
    system_peak: float
    # display-only context
    overall_peak_biomass: float
    overall_mean_biomass: float
    biomass_cap: float
    system_load: float
    feed_peak: float
    weeks_over_harvest_cap: int
    transfers_by_type: dict = field(default_factory=dict)
    per_system: dict = field(default_factory=dict)

    def component(self, name):
        return getattr(self, name)


@dataclass
class OptVariant:
    label: str
    overrides: dict
    metrics: "Metrics"
    dropped: int
    overprod: int
    score: float = 0.0
    norm: dict = field(default_factory=dict)

    @property
    def conservation_ok(self) -> bool:
        return self.dropped == 0 and self.overprod == 0


@dataclass
class OptRecommendation:
    best_label: str
    emphasis: str
    score: float
    is_capacity_bound: bool
    text: str


# --------------------------------------------------------------------------- #
# Workbook parsing
# --------------------------------------------------------------------------- #
def _table(ws, is_header, is_data):
    """Yield dicts for the data rows under the first row matching `is_header`."""
    hdr = None
    for row in ws.iter_rows(values_only=True):
        if hdr is None:
            if is_header(row):
                hdr = [str(c) if c is not None else "" for c in row]
            continue
        if is_data(row):
            yield {hdr[i]: row[i] for i in range(min(len(hdr), len(row)))}


def _col_map(ws, is_header):
    """{header_name: col_index} from the first row matching is_header, else {}."""
    for row in ws.iter_rows(values_only=True):
        if is_header(row):
            return {str(c).strip(): i for i, c in enumerate(row) if c is not None}
    return {}


def _find_col(colmap, *prefixes, default=None):
    """Index of the first column whose header starts with any prefix (case-insens).

    Lets the workbook parsers locate columns BY NAME instead of a hard-coded
    position, so a report-writer column reorder doesn't silently feed the
    optimizer the wrong metric. Falls back to `default` if not found."""
    for name, idx in colmap.items():
        low = name.lower()
        if any(low.startswith(p.lower()) for p in prefixes):
            return idx
    return default


def _cv(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    m = statistics.mean(xs) if xs else 0.0
    return (statistics.pstdev(xs) / m) if m else 0.0


def _swing(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    if len(xs) < 2:
        return 0.0
    m = statistics.mean(xs)
    return (statistics.mean(abs(b - a) for a, b in zip(xs, xs[1:])) / m) if m else 0.0


def _is_week(v):
    return isinstance(v, str) and "-W" in v


def _biomass_and_feed(wb):
    """Facility biomass + feed series + caps from Advisory; per-system biomass
    series + caps from SystemLimitsAudit (for the smoothness term)."""
    adv = list(_table(
        wb["Advisory"],
        lambda r: r and r[0] == "Week" and len(r) > 2 and "Total_Biomass" in str(r[2]),
        lambda r: _is_week(r[0])))
    bcol = next((k for k in (adv[0] if adv else {}) if k.startswith("Total_Biomass")), None)
    blim = next((k for k in (adv[0] if adv else {}) if k.startswith("Biomass_Limit")), None)
    fcol = next((k for k in (adv[0] if adv else {}) if k.startswith("Total_Feed")), None)
    flim = next((k for k in (adv[0] if adv else {}) if k.startswith("Feed_Limit")), None)
    bio = [d[bcol] for d in adv if isinstance(d.get(bcol), (int, float))]
    feed = [d[fcol] for d in adv if isinstance(d.get(fcol), (int, float))]
    bcap = next((d[blim] for d in adv if isinstance(d.get(blim), (int, float)) and d[blim] > 0), 0.0)
    fcap = next((d[flim] for d in adv if isinstance(d.get(flim), (int, float)) and d[flim] > 0), 0.0)

    # per-system biomass series (exclude OG6N depuration from the smoothness term)
    per = {}
    if "SystemLimitsAudit" in wb.sheetnames:
        for d in _table(
                wb["SystemLimitsAudit"],
                lambda r: r and r[0] == "Week" and len(r) > 1 and r[1] == "System",
                lambda r: _is_week(r[0])):
            sysid = d.get("System")
            b = d.get("Biomass_kg")
            cap = d.get("Biomass_cap")
            if not isinstance(b, (int, float)):
                continue
            e = per.setdefault(sysid, {"series": [], "cap": cap})
            e["series"].append(b)
    return bio, feed, bcap, fcap, per


def _harvest_weekly_fish(wb):
    ws = wb["HarvestPlan"]
    cols = _col_map(ws, lambda r: r and str(r[0]).strip() == "Week"
                    and any(str(c).strip() == "Batch" for c in r if c))
    ci = _find_col(cols, "Count", default=3)   # "Count (fish)"
    weekly = {}
    for row in ws.iter_rows(values_only=True):
        if _is_week(row[0]) and len(row) > ci and isinstance(row[ci], (int, float)):
            weekly[row[0]] = weekly.get(row[0], 0.0) + row[ci]
    return [weekly[w] for w in sorted(weekly)]


def _transfers_per_fish(wb):
    by_type = {"TranOG": 0.0, "Transfer": 0.0, "Grade": 0.0}
    for d in _table(
            wb["TransferPlan"],
            lambda r: r and r[0] == "Week" and len(r) > 2 and r[2] == "Type",
            lambda r: _is_week(r[0])):
        t = d.get("Type")
        c = d.get("Count (fish)")
        if t in by_type and isinstance(c, (int, float)):
            by_type[t] += c
    total_in = 0.0
    if "InputConservationAudit" in wb.sheetnames:
        for d in _table(
                wb["InputConservationAudit"],
                lambda r: r and r[0] == "Batch" and len(r) > 1 and "Input_Count" in str(r[1]),
                lambda r: isinstance(r[0], str) and len(r[0]) > 1
                          and r[0][0] == "B" and r[0][1:].isdigit()):
            ic = d.get("Input_Count (fish)")
            if isinstance(ic, (int, float)):
                total_in += ic
    tpf = (sum(by_type.values()) / total_in) if total_in else 0.0
    return tpf, by_type


def _system_overshoot(wb):
    """Fraction of (system, week) cells over their per-system biomass OR feed cap.
    The per-system compliance dimension the leveling knob targets — the optimizer
    needs this to 'see' whether leveling actually helps."""
    if "SystemLimitsAudit" not in wb.sheetnames:
        return 0.0
    over = tot = 0
    for d in _table(
            wb["SystemLimitsAudit"],
            lambda r: r and r[0] == "Week" and len(r) > 1 and r[1] == "System",
            lambda r: _is_week(r[0])):
        b = d.get("Biomass_kg"); bc = d.get("Biomass_cap")
        f = d.get("Feed_kg_day"); fc = d.get("Feed_cap")
        has_bc = isinstance(bc, (int, float)) and bc > 0
        has_fc = isinstance(fc, (int, float)) and fc > 0
        if not (has_bc or has_fc):
            continue
        tot += 1
        bover = has_bc and isinstance(b, (int, float)) and b > bc
        fover = has_fc and isinstance(f, (int, float)) and f > fc
        if bover or fover:
            over += 1
    return (over / tot) if tot else 0.0


def _density_overshoot(wb):
    """Fraction of (tank, week) cells over the ~95 kg/m3 density cap (per-tank
    density compliance — the trade leveling can create)."""
    if "BatchLocations" not in wb.sheetnames:
        return 0.0
    ws = wb["BatchLocations"]
    cols = _col_map(ws, lambda r: r and str(r[0]).strip() == "Week"
                    and any(str(c).strip().startswith("Density") for c in r if c))
    di = _find_col(cols, "Density", default=8)   # "Density (kg/m3)"
    si = _find_col(cols, "System", default=4)
    over = tot = 0
    for i, row in enumerate(ws.iter_rows(values_only=True), 1):
        if i < 5 or not row or row[0] is None:
            continue
        # EXCLUDE the OG6N depuration/purge pool: harvest-size fish held off-feed
        # at high density just before shipping is expected, not a compliance breach
        # (consistent with the engine, the app alert, and the smoothness term above).
        if len(row) > si and row[si] == "OG6N":
            continue
        dens = row[di] if len(row) > di else None
        if isinstance(dens, (int, float)):
            tot += 1
            if dens > 95:
                over += 1
    return (over / tot) if tot else 0.0


def _system_peak(wb):
    """The single HOTTEST (system, week) load across biomass AND feed, as a
    fraction of cap (OG6N depuration excluded). Minimizing this = no hot spots —
    keep every system's load as low as possible, evenly. Unlike system_overshoot
    (a COUNT of breaches), this is the worst peak, so the optimizer can drive the
    tallest spike down even while it stays under cap."""
    if "SystemLimitsAudit" not in wb.sheetnames:
        return 0.0
    peak = 0.0
    for d in _table(
            wb["SystemLimitsAudit"],
            lambda r: r and r[0] == "Week" and len(r) > 1 and r[1] == "System",
            lambda r: _is_week(r[0])):
        if d.get("System") == "OG6N":
            continue
        b = d.get("Biomass_kg"); bc = d.get("Biomass_cap")
        f = d.get("Feed_kg_day"); fc = d.get("Feed_cap")
        if isinstance(bc, (int, float)) and bc > 0 and isinstance(b, (int, float)):
            peak = max(peak, b / bc)
        if isinstance(fc, (int, float)) and fc > 0 and isinstance(f, (int, float)):
            peak = max(peak, f / fc)
    return peak


def metrics_from_workbook(out_path, harvest_cap) -> tuple["Metrics", int, int]:
    wb = openpyxl.load_workbook(out_path, data_only=True)
    bio, feed, bcap, fcap, per = _biomass_and_feed(wb)
    fish = _harvest_weekly_fish(wb)
    tpf, by_type = _transfers_per_fish(wb)

    peak_b = max(bio) if bio else 0.0
    mean_b = statistics.mean(bio) if bio else 0.0
    overshoot = 0.0
    util_gap = 0.0
    system_load = 0.0
    if bcap > 0:
        overshoot = max(0.0, peak_b - bcap) / bcap \
            + (sum(1 for x in bio if x > bcap) / len(bio) if bio else 0.0)
        util_gap = max(0.0, (bcap - mean_b) / bcap)
    sys_cvs = []
    per_out = {}
    for sysid, e in per.items():
        s = e["series"]
        cap = e["cap"] if isinstance(e["cap"], (int, float)) else 0.0
        mean_s = statistics.mean(s) if s else 0.0
        peak_s = max(s) if s else 0.0
        cv_s = _cv(s)
        per_out[sysid] = {"mean": mean_s, "peak": peak_s, "cv": cv_s, "cap": cap}
        if sysid != "OG6N":
            sys_cvs.append(cv_s)
            if cap > 0:
                system_load = max(system_load, peak_s / cap)
    biomass_var = (statistics.mean(sys_cvs) if sys_cvs else 0.0) + _swing(bio)

    metrics = Metrics(
        biomass_overshoot=overshoot,
        biomass_var=biomass_var,
        biomass_util_gap=util_gap,
        harvest_var=_cv(fish),
        harvest_overshoot=(sum(1 for x in fish if x > harvest_cap) / len(fish)) if fish else 0.0,
        feed_load=statistics.mean(feed) if feed else 0.0,
        feed_var=_cv(feed) + _swing(feed),
        transfers_per_fish=tpf,
        system_overshoot=_system_overshoot(wb),
        density_overshoot=_density_overshoot(wb),
        system_peak=_system_peak(wb),
        overall_peak_biomass=peak_b,
        overall_mean_biomass=mean_b,
        biomass_cap=bcap,
        system_load=system_load,
        feed_peak=max(feed) if feed else 0.0,
        weeks_over_harvest_cap=sum(1 for x in fish if x > harvest_cap),
        transfers_by_type=by_type,
        per_system=per_out,
    )
    dropped, overprod = tuning._conservation(out_path)
    return metrics, dropped, overprod


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def weights_for(emphasis, custom=None) -> dict:
    if custom:
        return {c: float(custom.get(c, 0.0)) for c in COMPONENTS}
    base = EMPHASIS_PRESETS.get(emphasis, EMPHASIS_PRESETS[DEFAULT_EMPHASIS])
    return {c: float(base.get(c, 0.0)) for c in COMPONENTS}


def score_variants(variants, weights) -> None:
    """Fill each variant's .norm and .score in place. Only conservation-OK
    variants participate in normalization (a rejected variant never skews
    another's score). Normalization is per-component max over OK variants."""
    ok = [v for v in variants if v.conservation_ok]
    maxima = {c: max((v.metrics.component(c) for v in ok), default=0.0) for c in COMPONENTS}
    for v in variants:
        v.norm = {c: (v.metrics.component(c) / maxima[c]) if maxima[c] > 0 else 0.0
                  for c in COMPONENTS}
        v.score = sum(weights.get(c, 0.0) * v.norm[c] for c in COMPONENTS)


def recommend(variants, emphasis=DEFAULT_EMPHASIS, weights=None) -> OptRecommendation:
    w = weights or weights_for(emphasis)
    score_variants(variants, w)
    ok = [v for v in variants if v.conservation_ok]
    if not ok:
        return OptRecommendation("(none)", emphasis, float("inf"), False,
                                 "No variant held conservation — investigate before optimizing.")
    best = min(ok, key=lambda v: (v.score, 0 if v.label == "baseline" else 1, v.label))
    baseline = next((v for v in variants if v.label == "baseline"), None)
    capacity_bound = (baseline is not None and baseline.conservation_ok
                      and best.score >= baseline.score - 1e-9)
    if capacity_bound:
        text = (f"No variant beats baseline on the '{emphasis}' objective — the "
                "remaining lumpiness/over-cap is a stocking/capacity limit, not a "
                "knob (see USER_GUIDE). Baseline stands.")
    else:
        text = (f"Best for '{emphasis}': {best.label} (score {best.score:.3f} vs "
                f"baseline {baseline.score:.3f}). Use the Apply & verify panel below "
                f"to run it now (or paste the knobs into Configure → Control to keep them).")
    return OptRecommendation(best.label, emphasis, best.score, capacity_bound, text)


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #
def _harvest_cap(config_dir, overrides):
    """Effective max_harvest_per_week (fish/week) for a variant."""
    cap = 55000.0
    try:
        with open(os.path.join(config_dir, "control.yaml")) as f:
            cap = float(yaml.safe_load(f).get("max_harvest_per_week", cap) or cap)
    except (OSError, ValueError, TypeError):
        pass
    if "max_harvest_per_week" in overrides:
        cap = float(overrides["max_harvest_per_week"])
    return cap


def run_variant(label, overrides, config_dir, scenario_dir, input_path) -> OptVariant:
    out = tuning._run_in_tempdir(label, overrides, config_dir, scenario_dir, input_path)
    metrics, dropped, overprod = metrics_from_workbook(out, _harvest_cap(config_dir, overrides))
    return OptVariant(label=label, overrides=dict(overrides),
                      metrics=metrics, dropped=dropped, overprod=overprod)


def overrides_yaml(overrides) -> str:
    """Render a recommendation's knob overrides as a control.yaml snippet."""
    if not overrides:
        return "# (baseline — no knob changes)"
    return "\n".join(f"{k}: {('null' if v is None else str(v).lower() if isinstance(v, bool) else v)}"
                     for k, v in overrides.items())


def run_full_forecast(input_path, config_dir, scenario_dir, overrides) -> str:
    """Apply `overrides` onto control.yaml and run the FULL pipeline, returning
    the output workbook path — i.e. feed a recommendation straight back into the
    forecast. Reuses the shared run harness; nothing mutates the caller's config."""
    return tuning._run_in_tempdir("optimized", overrides or {},
                                  config_dir, scenario_dir, input_path)


def save_overrides_to_config(config_dir, overrides) -> None:
    """Persist `overrides` by merging them into the REAL config_dir/control.yaml,
    so every later normal run (and the Configure editor) uses them. This is how a
    recommendation becomes the standing config — without it, the knobs live only
    in the optimizer's temp run and a normal 'Run forecast' reverts to baseline."""
    cy = os.path.join(config_dir, "control.yaml")
    with open(cy) as f:
        cfg = yaml.safe_load(f)
    cfg.update(overrides or {})
    with open(cy, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)


# --------------------------------------------------------------------------- #
# Run log — a durable record of WHAT was run and WHAT it produced
# --------------------------------------------------------------------------- #
DEFAULT_RUN_LOG = "optimize_history.jsonl"


def make_run_record(best, method, emphasis, *, ts, saved, source,
                    dropped=None, overprod=None) -> dict:
    """Build one optimize/auto-optimize log record: the SETTINGS used (method,
    emphasis, the winning knobs) + the RESULTS (key metrics + conservation). `ts` is
    an ISO timestamp the caller supplies (so this stays import-light)."""
    m = best.metrics if best else None
    return {
        "ts": ts,
        "source": source,
        "method": method,
        "emphasis": emphasis,
        "winning_knobs": dict(best.overrides) if best and best.overrides else {},
        "saved_to_config": bool(saved),
        "dropped": dropped if dropped is not None else (best.dropped if best else None),
        "overprod": overprod if overprod is not None else (best.overprod if best else None),
        "metrics": {
            "system_peak": round(m.system_peak, 3),
            "feed_load": round(m.feed_load),
            "weeks_over_harvest_cap": m.weeks_over_harvest_cap,
            "harvest_cv": round(m.harvest_var, 3),
            "system_overshoot": round(m.system_overshoot, 3),
        } if m else {},
    }


def append_run_log(record: dict, log_path: str = DEFAULT_RUN_LOG) -> None:
    """Append one run record as a JSON line — a durable, reviewable history of what
    the optimizer was asked to do and what it produced. Best-effort: a logging
    failure never breaks the run."""
    import json
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:  # noqa: BLE001
        pass


def read_run_log(log_path: str = DEFAULT_RUN_LOG, n: int = 20) -> list:
    """Return the last `n` run records (oldest→newest), or [] if no log yet."""
    import json
    try:
        with open(log_path, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        return [json.loads(ln) for ln in lines[-n:]]
    except Exception:  # noqa: BLE001
        return []


def config_dir_with_overrides(config_dir, overrides) -> str:
    """Return a TEMP copy of `config_dir` with `overrides` merged into
    control.yaml — so a caller (e.g. the app) can run the full pipeline against
    it and parse the result for visualization. Nothing mutates `config_dir`."""
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="as_optcfg_")
    dst = os.path.join(tmp, "config")
    shutil.copytree(config_dir, dst)
    cy = os.path.join(dst, "control.yaml")
    with open(cy) as f:
        cfg = yaml.safe_load(f)
    cfg.update(overrides or {})
    with open(cy, "w") as f:
        yaml.safe_dump(cfg, f)
    return dst


def _default_workers(n_tasks: int) -> int:
    """Process-pool size for the sweep: leave a core for the UI, never more workers
    than tasks, capped at 8 (each worker runs a full pipeline — memory + diminishing
    returns past that)."""
    try:
        cpu = os.cpu_count() or 2
    except Exception:  # noqa: BLE001
        cpu = 2
    return max(1, min(n_tasks, cpu - 1, 8))


def sweep(input_path, config_dir, scenario_dir, grid=None, progress=None,
          parallel=True, max_workers=None) -> list[OptVariant]:
    """Run every grid row and return per-variant results (unscored — call
    recommend()/score_variants() with an emphasis). Nothing mutates the caller's
    config; each variant runs in its own temp copy.

    Each variant is an INDEPENDENT full forecast, so by default they run across a
    PROCESS POOL — the pipeline is CPU-bound + deterministic, so parallel execution
    yields IDENTICAL results (sorted back to grid order), just N× faster. Falls back
    to sequential automatically if a pool can't start (restricted env). Pass
    parallel=False to force the old one-at-a-time path."""
    grid = grid or OPT_FULL_GRID
    n = len(grid)
    if max_workers is None:
        max_workers = _default_workers(n)

    if not parallel or max_workers <= 1 or n <= 1:
        results = []
        for i, (label, overrides) in enumerate(grid):
            if progress is not None:
                progress(i, n, label)
            results.append(run_variant(label, overrides, config_dir, scenario_dir,
                                       input_path))
        return results

    from concurrent.futures import ProcessPoolExecutor, as_completed
    order = {label: i for i, (label, _) in enumerate(grid)}
    results: list = []
    done = 0
    try:
        with ProcessPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(run_variant, label, ov, config_dir, scenario_dir,
                              input_path): label for label, ov in grid}
            for fut in as_completed(futs):
                done += 1
                if progress is not None:
                    progress(done, n, futs[fut])
                results.append(fut.result())
    except Exception:  # noqa: BLE001
        # A pool that can't even start (sandboxed env) -> sequential. But if some
        # variants already ran, a failure is a real variant error -> surface it.
        if results:
            raise
        return sweep(input_path, config_dir, scenario_dir, grid=grid,
                     progress=progress, parallel=False)
    results.sort(key=lambda v: order.get(v.label, 1 << 30))   # deterministic order
    return results


# Knob search space for the deep (coordinate-descent) search: each knob + the
# candidate values to try. Spans the levers that move the Minimize-loads objective
# — placement (tran_og), the harvest controller (setpoint/K), packing density, and
# the rebalancer budgets. Edit here to widen/narrow the search.
CD_KNOB_SPACE = [
    ("tran_og_default_tanks", [2, 3]),
    ("facility_biomass_deviation_pct", [0.005, 0.01, 0.02]),
    ("harvest_smooth_lookahead_weeks", [6, 12]),
    ("density_target_pct", [0.85, 0.90, 0.95]),
    ("rebalance_balance_budget", [30, 60]),
    ("rebalance_split_budget", [8, 12]),
]


def _overrides_key(ov):
    """Deterministic, order-independent sort key for an overrides dict — so a tie on
    score breaks the SAME way regardless of evaluation order (needed once variants
    are evaluated in parallel)."""
    return tuple(sorted((str(k), str(v)) for k, v in ov.items()))


def coordinate_descent(input_path, config_dir, scenario_dir, emphasis=DEFAULT_EMPHASIS,
                       weights=None, knob_space=None, max_rounds=3, seed=None,
                       progress=None, parallel=True) -> list[OptVariant]:
    """Greedy local search that finds COMBINATIONS the grid can't.

    From the `seed` config (default = current config / baseline), improve ONE knob
    at a time: try each candidate value for a knob (holding the others at the current
    best), keep the value that scores best, move to the next knob. Loop over all knobs
    each round; stop when a full round makes no improvement (local optimum) or
    max_rounds.

    The candidate VALUES of one knob are independent, so they're evaluated across a
    PROCESS POOL (one pool reused for the whole descent). The cyclic knob order +
    accept-best-per-knob trajectory is UNCHANGED, and ties break by a deterministic
    override key, so the result is identical to (and as reproducible as) the
    sequential version — just faster. Conservation-OK variants only ever win. Returns
    ALL evaluated variants (same shape as sweep()). Nothing mutates the caller's config."""
    knob_space = knob_space or CD_KNOB_SPACE
    w = weights or weights_for(emphasis)
    cache: dict = {}
    evaluated: list = []

    def _key(ov):
        return tuple(sorted(ov.items()))

    pool = None
    if parallel and _default_workers(8) > 1:
        from concurrent.futures import ProcessPoolExecutor
        try:
            pool = ProcessPoolExecutor(max_workers=_default_workers(8))
        except Exception:  # noqa: BLE001 — restricted env -> sequential
            pool = None

    def _record(k, v):
        cache[k] = v
        evaluated.append(v)
        if progress is not None:
            progress(len(evaluated), None, v.label)

    def _eval(ov, label):
        k = _key(ov)
        if k in cache:
            return cache[k]
        _record(k, run_variant(label, dict(ov), config_dir, scenario_dir, input_path))
        return cache[k]

    def _eval_many(items):
        """Evaluate a list of (key, overrides, label) — all uncached — in parallel."""
        if not items:
            return
        if pool is None or len(items) == 1:
            for k, ov, label in items:
                _eval(ov, label)
            return
        from concurrent.futures import as_completed
        futs = {pool.submit(run_variant, label, dict(ov), config_dir, scenario_dir,
                            input_path): k for k, ov, label in items}
        for fut in as_completed(futs):
            _record(futs[fut], fut.result())

    def _best_overrides():
        # Re-score the whole evaluated set (consistent normalization over all OK
        # variants) and return the lowest-scoring conservation-OK config; ties break
        # deterministically so parallel == sequential.
        score_variants(evaluated, w)
        ok = [v for v in evaluated if v.conservation_ok]
        best = (min(ok, key=lambda v: (v.score, _overrides_key(v.overrides)))
                if ok else evaluated[0])
        return dict(best.overrides)

    try:
        seed = dict(seed or {})
        _eval(seed, "seed" if seed else "baseline")   # warm-start (seq: it's evaluated[0])
        current = _best_overrides()
        for _ in range(max_rounds):
            improved = False
            for knob, values in knob_space:
                todo = []
                for val in values:
                    ov = dict(current); ov[knob] = val
                    k = _key(ov)
                    if k != _key(current) and k not in cache:
                        todo.append((k, ov, f"{knob}={val}"))
                _eval_many(todo)
                nb = _best_overrides()
                if _key(nb) != _key(current):
                    current = nb
                    improved = True
            if not improved:
                break
    finally:
        if pool is not None:
            pool.shutdown()
    return evaluated


def deep_search_combined(input_path, config_dir, scenario_dir, emphasis=DEFAULT_EMPHASIS,
                         weights=None, grid=None, max_rounds=3,
                         progress=None) -> list[OptVariant]:
    """Best-of-both for ANY emphasis: run the full GRID (broad, diverse coverage —
    incl. the off-by-default controls), then coordinate descent SEEDED FROM the
    grid's best (local refinement that finds combinations around the broadly-best
    point). Pool every evaluated variant, dedup by overrides, and return the lot —
    recommend() then picks the GLOBAL best across both methods. Grid explores,
    descent exploits; the winner is whichever wins. Same OptVariant list shape, so
    the app/score-table/Pareto/apply-verify consume it unchanged."""
    w = weights or weights_for(emphasis)
    gvars = sweep(input_path, config_dir, scenario_dir,
                  grid=grid or OPT_FULL_GRID, progress=progress)
    score_variants(gvars, w)
    ok = [v for v in gvars if v.conservation_ok]
    seed = dict((min(ok, key=lambda v: v.score) if ok else gvars[0]).overrides)
    dvars = coordinate_descent(input_path, config_dir, scenario_dir, emphasis=emphasis,
                               weights=w, seed=seed, max_rounds=max_rounds, progress=progress)
    pool = {}
    for v in gvars + dvars:        # dedup by overrides; identical configs collapse
        pool[tuple(sorted(v.overrides.items()))] = v
    return list(pool.values())
