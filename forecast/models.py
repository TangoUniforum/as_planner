"""Typed inputs and intermediate state for the forecast pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class ControlParams:
    forecast_start: datetime
    horizon_weeks: int
    scenario_name: str
    max_feed_per_day_kg: float
    max_biomass_kg: float
    max_harvest_per_week: float
    min_harvest_weight_g: float
    min_harvest_per_week: float
    min_tank_control: float
    default_hog_yield: float
    # R24: symmetric ± deviation around facility biomass + feed caps.
    # 0.01 means the planner accepts being within 1% above or below target.
    facility_biomass_deviation_pct: float
    # Control sheet "Handling Mortality" stores a percentage value (e.g. 0.01
    # means 0.01%).  Apply as fraction = handling_mortality_pct / 100.
    handling_mortality_pct: float
    sixn_growth: bool
    sixn_production_start: Optional[datetime] = None
    sixn_transition_weeks: Optional[int] = None
    # R28-R30 (added 2026-05).
    tran_og_default_tanks: int = 3
    global_buffer_pct: float = 0.05      # R29: system-limits symmetric buffer
    # R-GE: how cleanly a real grader separates sizes, 0-1. 1.0 = perfect cut
    # at the threshold; lower leaves the two populations OVERLAPPING near the
    # cut line, which is what actually happens. VBA default 0.85
    # (ForecastEngine_V2.bas:1662). Values outside (0,1) mean "perfect".
    # GLOBAL ENGINE ONLY. False (default, operator 2026-08-21) = model the REAL
    # 6N handover: L1 primes its purge pipeline ONLY from the fish actually in
    # 6N at forecast start. True = the older idealisation, which topped the
    # first purge slots up to steady-state fill by drawing harvest-ready fish
    # out of grow-out.
    #
    # The idealisation buys a smooth harvest from week 1 with no startup ramp,
    # and it conserves INSIDE L1 (it only moves fish already in the seeds). But
    # the tank picker cannot realise it: there are six 6N tanks and the
    # ProductionReport has already filled them with other batches, so the fish
    # L1 "staged" get harvested straight out of production tanks. That is the
    # entire 6N-only-rule violation -- 39,094 fish, 99.96% of them in week 1 --
    # and it is why simply enforcing the rule at the draw DELETED 39,077 fish:
    # the plan had already spent them.
    #
    # A plan that cannot be executed is not a plan, so the default models the
    # truth and accepts the startup ramp as real information.
    global_assume_primed_6n: bool = False
    grade_efficiency: float = 0.85
    # ---- Density relief / consolidation policy (was hard-coded in placement.py).
    # Promoted to knobs 2026-08-21: these were HAND-TUNED on one workbook, and
    # the reference fixture and the real workbook disagreed about the best
    # values, so they belong in the optimizer's search space rather than in a
    # developer's judgement. Defaults are the hand-tuned values, so promoting
    # them changes no plan.
    #
    # An entry tank at or above this fraction of its density cap for
    # `chronic_pressure_weeks` consecutive weeks is STRUCTURALLY short of
    # capacity and gets a tank, not weekly shaving. MUST stay clear of
    # density_relief_pct: when the trigger sat exactly on the level relief
    # drives tanks TO, relieved tanks piled up on it and flipped in and out of
    # "chronic" on a 0.01% perturbation.
    chronic_pressure_frac: float = 0.92
    chronic_pressure_weeks: int = 4
    # Chronic tanks are shed DEEPER than the ordinary target — trimming a tank
    # that has sat at 91% for a month to 90% moves nothing.
    chronic_relief_pct: float = 0.80
    # Anticipatory work is BOUNDED: consolidation and the 6N harvest move-in
    # share one weekly handling budget, so an unbounded chronic sweep starves
    # harvest staging (measured: weeks under the sales floor 13 -> 20). Tanks
    # already OVER cap are urgent and exempt from this cap.
    chronic_max_frees_per_week: int = 1
    # An over-cap OG1/2 tank is shed to this fraction of its cap. Relieving to
    # exactly the cap left zero margin and the tank re-breached within one week
    # of growth.
    density_relief_pct: float = 0.90
    # Consolidating a batch's growout tanks fills the keepers to this fraction.
    # 0.80, not 0.90: a tank filled to its cap re-breaches within a week.
    consolidation_fill_pct: float = 0.80
    starvation_period_days: int = 7      # R30: in-place purge length (production mode); 7d = one weekly step (single-cohort pipeline)
    # HARVEST PRESSURE RELIEF (operator semantic correction 2026-08-09,
    # replacing the short-lived target/ceiling pair — the removed
    # `harvest_target_per_week` knob is IGNORED with a console note when an
    # old YAML still carries it). `max_harvest_per_week` above is THE weekly
    # processing LIMIT (55k): a CONSTRAINT the demand-driven harvest respects
    # — no pass ever sizes harvest UP to reach it. This knob is the relief
    # band as a fraction of that limit: the derived absolute ceiling is
    # max_harvest_per_week * (1 + harvest_relief_pct), and a week may sit in
    # the band only EXCEPTIONALLY (a whole 6N tank that must drain, a
    # make-room dump that saves an arrival). The checklist gate WARNs at 1-3
    # relief weeks and FAILs beyond — a plan that pins to relief every week
    # must ramp its harvests up earlier instead. 0 = no relief band.
    harvest_relief_pct: float = 0.10
    # HANDLING BUDGET (operator ruling 2026-08): max transfer MOVES per week.
    # Once the week's emitted move count reaches this, the DEFERRABLE quality
    # passes (even-out, multi-objective balancer, variable-quantity shave,
    # remnant sweep) stop emitting — their work waits for a calmer week.
    # ESSENTIAL moves (6N rotation fills, arrival make-room/entry-vacate, the
    # plan-diff) are never blocked: a week they alone push past the cap is
    # REPORTED by the handling gate (WARN above ~80% of the cap, FAIL above
    # it), never silently truncated at the contract's expense. 0 = off.
    max_transfers_per_week: int = 15
    # Minimum rebalancer transfer size (fish): the density/load rebalancer will not
    # split a sub-group smaller than this OUT of a tank (tiny moves cost handling for
    # little relief) — the OUT-side mirror of min_tank_control's "don't leave a
    # dribble" rule. 0 = OFF (no floor; default = byte-identical). Set e.g. to
    # min_tank_control (7000) or 10000 to suppress small partial transfers; whole-
    # tank consolidation moves are unaffected.
    min_transfer_count: float = 0.0
    # GRADE-TO-MIN (opt-in): on a 6N purge week where whole mature tanks can't fill
    # the harvest floor, peel the over-weight TAIL from near-market tanks into a free
    # pair tank (big -> 6N purge) with the small tail to a free OG retention tank,
    # topping the move-in up toward min_harvest_per_week. An EXCEPTION that fires
    # only when short (never a routine rule); trades some grow-out yield (those fish
    # land at the low end of market weight + a grading handling event) for a steady
    # processing floor. Default OFF = byte-identical.
    harvest_grade_to_min: bool = False
    # LEVEL 6N DRAINS (ON by default): cap how full a 6N purge pair may get (at
    # max_harvest_per_week) so weekly fills don't ACCUMULATE into one pair across
    # its rotation residency — the root cause of 90-113k drain spikes that starve
    # other pairs into sub-min troughs. Surplus waits in grow-out and fills the next
    # thin pair, lifting its drain toward the floor (meet the harvest min every week).
    # Verified vs OFF: 6N drain peak 110k->68k (-38%), CV 0.46->0.32, 6N weeks-below-min
    # 38->27, fish conserved. Joins rebalance_level + harvest_level_load as a leveling
    # default. Set false for the old accumulate-then-dump behavior.
    # 6N DRAIN ORDER (False = OFF = byte-identical). A purge pair is drained in
    # TANK-ID order, and the weekly processing limit is spent as it goes -- so a
    # small tank draining first can leave too little budget for its large
    # partner, which is then HELD "until the pair's next rotation". Next
    # rotation the same small-first order repeats, and the hold repeats with it.
    # MEASURED on 8 starting states, 2026-08-31: this livelocks. OG6N-69 held
    # 53,006 fish from 2026-W16 to 2027-W20 -- 58 consecutive rotations, frozen
    # at 4.371 kg, off feed, bleeding ~79 fish/week to mortality and never
    # harvested. 5 of the 8 states trap fish this way, up to 1,463 t live on the
    # July'26 close. It is not a conservation fault (nothing is lost; the fish
    # stand at horizon end) -- it is a PHYSICALLY IMPOSSIBLE plan, since salmon
    # held off feed for a year are dead. It also manufactures the sub-floor
    # harvest weeks: a trapped tank means its partner drains ALONE, which is the
    # recurring 7,129 = min_tank_control x 1.02 eroded by mortality.
    # True drains the pair LARGEST FIRST, so the tank that can only fit in an
    # empty budget gets the budget while it is still empty. Ties break on
    # tank_id, so the order stays deterministic.
    sixn_drain_largest_first: bool = False
    # OVERDUE 6N DRAIN (weeks; 0 = OFF = byte-identical). The HARVEST LIMIT hold
    # defers a 6N tank whose fish would not fit in the week's remaining
    # processing budget, promising it "drains next rotation". For a tank holding
    # close to a full week's limit that promise can never be kept: any earlier
    # harvest in the week leaves too little room, and the same thing happens on
    # every rotation after. Measured: 53,006 fish held from 2026-W16 to
    # 2027-W20, and 5 of 8 starting states trap fish this way.
    # Draining biggest-first WITHIN the pair (`sixn_drain_largest_first`) only
    # half-fixes it -- it cannot help when the budget was already spent before
    # the pair was reached (one measured week had 60 fish of headroom left).
    # This gives a tank that has sat in purge longer than N weeks FIRST CLAIM on
    # the week: it is ordered ahead of its pair-mates and is EXEMPT from the
    # harvest-limit hold, so it drains even when that pushes the week into the
    # exceptional relief band (limit .. limit x (1 + harvest_relief_pct)) that
    # the code already reserves for this case and the harvest gate counts. The
    # depuration guard is NEVER waived -- an overdue tank has by definition
    # purged long enough, so the two can not conflict.
    # A tank cannot be both overdue and legally held: that is the livelock.
    sixn_overdue_drain_weeks: int = 0
    sixn_level_drains: bool = True
    # R31: per-tank density target as a fraction of the cap. Drives
    # precalc `tanks_needed_at_density_cap` sizing, the Phase D Grade-
    # split trigger, and the PR-concentration advisory/action gate.
    # 0.85 leaves 15% headroom for growth between weekly checks.
    density_target_pct: float = 0.85
    # Product-quality WELFARE density line (kg/m3), a SOFT threshold below the hard
    # per-tank cap (~95). Fish reared above it are "crowded" — the metric that
    # reports/optimizes how gently the product was reared (Run KPI, Compare "Best
    # welfare" lens, Optimize "Product quality" preset). Does NOT constrain the
    # plan; it only measures/scores. 80 is the accepted salmon welfare line.
    density_welfare_threshold_kg_m3: float = 80.0
    # Realized-rebalancer budgets (moves/week). 0 disables the pass. VARQTY
    # moves a precise count of fish off over-cap systems; SPLIT fans over-dense
    # batches into free tanks. Exposed as Control knobs so they can be swept
    # from the app without code changes (the floor fixes do the heavy lifting;
    # these are tunable extras with a transfer cost).
    rebalance_varqty_budget: int = 0     # off by default (marginal ROI, transfer cost); opt-in knob
    rebalance_split_budget: int = 8
    # Multi-objective balancer (moves/week): relieves over-dense tanks into
    # destinations with headroom in density + system feed + system biomass at
    # once, so it cuts out-of-bounds across all three without trading one for
    # another. 0 disables.
    rebalance_balance_budget: int = 30
    # General load-LEVELING: a cap-agnostic balancer that spreads load off the
    # hottest OG system (highest utilization = max of biomass/feed/density vs cap)
    # onto the COLDEST eligible one, instead of concentrating fish into the
    # most-headroom tank. Levels density, biomass AND feed together, from any
    # starting state, following the rules (1 kg move-lock, conservation, dest
    # headroom). Shares rebalance_balance_budget moves/week. ON by default: the
    # density-only balancer leaves per-system FEED badly skewed (measured on
    # config(8): 312 feed + 149 biomass over-cap system-weeks); leveling cuts that
    # to 25 / 6 with 0 dropped fish and byte-identical determinism — total OG feed
    # fits capacity every week (86% mean / 97% peak), so the breaches were pure
    # distribution. Set false to recover the old density-only behavior.
    rebalance_level: bool = True
    # FORWARD HEADROOM (days; 0 = OFF = byte-identical). The balancer scores
    # destinations on the load it can see NOW, but the SystemLimitsAudit measures
    # the END-of-week snapshot a full week of growth later. The constants above
    # (_BALANCE_TARGET_FRAC 0.88 / _BALANCE_TRIGGER_FRAC 0.92 / _BALANCE_SYS_FILL
    # 0.90) are a FLAT ~10%/week allowance standing in for that growth -- and
    # A flat number cannot fit: measured on the configured curves, weekly
    # biomass growth runs +10.0% at the 370 g entry tier but only +3.7% at
    # 3.5 kg, so one constant is simultaneously too tight for new arrivals and
    # too generous for grow-out, where most of the biomass sits.
    # Set to 7 and the balancer projects each tank forward at its OWN SGR (the
    # same forecast.biology curve the sim uses, geometric-mean weight) before
    # summing system load, so the growth allowance is MEASURED per tank rather
    # than assumed uniform. The flat margin is then redundant, so the projected
    # path fills to _BALANCE_SYS_FILL_FWD instead -- a small model-error margin
    # rather than a large unmodelled-growth one.
    #
    # MEASURED AND REJECTED AS A DEFAULT, 8 states x 4 variants, 2026-08-31.
    # It does what it was built to do -- feed system-weeks over cap improve in
    # 8/8 states vs the shipped config (-19 to -50), and `bal8_fwd7` produced
    # the ONLY run of 32 to escape a system_feed FAIL. It is unusable anyway:
    #   * weeks below the contract floor WORSEN in 7 of 8 states. On the live
    #     workbook, 2 -> 12. min_harvest_per_week is a sales commitment.
    #   * the worst harvest week falls in 6 of 8, by up to -38.6%.
    #   * harvest_cap FAILS 8/8 with the balancer on, headroom or not (the
    #     shipped config is 3 PASS / 1 WARN / 4 FAIL).
    #   * it posts the study's worst single system, 1.528x -- breach COUNT down,
    #     PEAK up, the same trade metrics-v6 exists to catch.
    # It also does NOT repair the balancer's floor damage, which was the whole
    # hypothesis: vs `bal8` the floor is better in 3 states and worse in 4.
    # CONFOUND, unresolved: the projection and the destination fill fraction
    # (0.90 -> _BALANCE_SYS_FILL_FWD) changed together, so it is not yet known
    # whether the floor damage comes from seeing forward or from packing
    # tighter. Re-test with the fill held at 0.90 before concluding the idea
    # itself is dead. Data: docs/LEVELING_TRADE_2026-08-30.md.
    rebalance_headroom_days: int = 0
    # END-OF-WEEK CAP REPAIR (moves/week; 0 = OFF = byte-identical). Every other
    # realised repair pass runs BEFORE the week's day-by-day biology, but the
    # BatchLocations snapshot that the SystemLimitsAudit sums is taken AFTER it
    # — so they all aim at the START-of-week load while every published metric
    # measures the END-of-week one, with a full week of growth (~+7% biomass,
    # ~+11% feed at entry-tier weights) landing in between unchecked. Measured
    # on the 7.29 PR: 79 of 104 non-6N over-cap system-weeks were ALREADY
    # compliant when the balancer finished (0.94-0.99 of cap) and nothing but
    # growth carried them over. This pass runs LAST — after biology, after the
    # grade split, immediately before the snapshot — and moves the minimum out
    # of any system over its RAW cap into the coldest legal one. A repair, not
    # a leveller. It relaxes no business rule (tiers R2-R4/R7, one batch per
    # tank, per-tank density, remnant + min-transfer floors, TranOG holds) and
    # is deferrable: it spends only the weekly handling budget left over, which
    # — uniquely, because nothing follows it — is exact.
    cap_repair_budget: int = 0
    # Anticipatory harvest setpoint: hold facility biomass below the cap by ~this
    # many weeks of the facility's REALIZED weekly growth (margin clamped to
    # [0.5%, 4%] of cap), so harvest pre-sheds each peak across the calm run-up
    # weeks instead of spiking past the processing max. 0.75 = tightest walk of
    # the line (~1 wk ~0.4% over cap, within the R24 deviation band, ~95.8%
    # utilisation); 0.90 = strict zero-breach of a hard cap (~94.8%); higher =
    # safer/lower utilisation. Config(7)-anchored — re-tune per scenario (see
    # docs/USER_GUIDE.md).
    # INACTIVE (audit L7): SUPERSEDED by the dual-limit setpoint — no harvest path
    # reads this knob anymore; tuning it has no effect. Retained only so configs
    # predating the redesign still load. (The comment above describes the old
    # superseded mechanism.)
    harvest_setpoint_lookahead_weeks: float = 0.75
    # Harvest LEVEL-LOADING smoother. When True, the realized harvest controller
    # enforces max_harvest_per_week as a HARD weekly ceiling across every harvest
    # pass AND pre-harvests cohorts earlier so weekly throughput is leveled (no
    # sawtooth dump) while biomass stays under its cap — walking the line near the
    # cap, flat. ON by default, PAIRED with rebalance_level: feed-leveling spreads
    # fish thinner, leaving fewer free whole tanks, so the controller does more
    # make-room dumps and harvest gets SPIKIER (config(8): 11->15 weeks over the
    # 55k processing cap). This recovers and beats it — 15->10 weeks, max 119k->
    # 89k/wk, harvest CV 0.407->0.251, biomass over-cap 19->9, HOG tonnage + avg
    # weight unchanged, for +7 feed system-weeks (of ~1540). The processing cap is
    # hard, so holding it wins. harvest_smooth_lookahead_weeks (K) = weeks of
    # coming-due biomass to spread the pre-harvest over; harvest_level_target pins
    # a flat fish/week floor (None = auto). Set false for the old reactive behavior.
    harvest_level_load: bool = True
    harvest_smooth_lookahead_weeks: int = 6
    harvest_level_target: Optional[float] = None
    # Placement engine selector (OPT-IN, additive). "greedy" (default) = the current
    # heuristic placement + rebalancer, byte-identical to today. "lns" = run greedy as
    # a WARM START, then an LNS pass refines the REALIZED layout: it relocates / swaps
    # grow-out tank occupancy off the hottest systems onto cooler ones, emitting each
    # move as a conserved Transfer. Every edit is gated on the real continuity audit
    # (0 drift) + input conservation (0 dropped) + a strictly-lower hot-spot peak, and
    # greedy is the FALLBACK, so it can never lose anything or make the run worse. See
    # docs/LP_GUIDED_LNS_PLACEMENT.md and forecast/lns_placement.py.
    placement_method: str = "greedy"
    # LNS budget: max relocations/swaps per run (only used when placement_method=="lns").
    lns_max_moves: int = 30
    # Auto-calibrate FW growth (OPT-IN, default OFF -> shipped behaviour unchanged).
    # When True, before projecting, each FW batch's `fw_correction` is REPLACED by
    # the value that lands its pre-cull avg weight exactly on `tran_og_avg_wt_g` at
    # its transfer date (the same back-solve already reported as Suggested_FW_Correction
    # in Diagnostics) — incoming batches via solve_fw_correction, in-flight FW batches
    # via solve_inflight_fw_correction (on their REMAINING growth). The solved value is
    # clamped to [auto_calibrate_fw_min, auto_calibrate_fw_max] so the model can't
    # silently assume absurd growth; a batch clamped short of target is flagged. NOTE:
    # this makes the forecast ASSUME the growth needed to hit target (a planning
    # assumption, not a guarantee) — a correction >1 means faster-than-nominal SGR.
    auto_calibrate_fw: bool = False
    auto_calibrate_fw_min: float = 0.5
    auto_calibrate_fw_max: float = 1.5

    # HYBRID L1-GUIDED HARVEST (opt-in, additive — see forecast/hybrid_guide.py).
    # "off" = the validated controller, byte-identical. "floor"/"full" run a
    # standalone L1 pre-pass and feed its per-week harvest quantity in as a
    # target: "floor" never harvests LESS than L1 that week; "full" follows L1
    # as a BAND, and its ceiling is released whenever realized biomass is over
    # the facility cap or the two engines disagree about that week's 6N mode.
    hybrid_follow: str = "off"              # "off" | "floor" | "full"
    hybrid_follow_band: float = 0.10        # ± around the guide, "full" only
    # Guide weeks below this fraction of min_harvest_per_week are DROPPED (not
    # zeroed — a zero would become a ceiling of zero): they are L1 structural
    # dropouts (transition-zeroed draws, startup priming, horizon tail).
    hybrid_guide_min_frac: float = 0.25
    hybrid_guide_smooth_weeks: int = 0      # 0/1 = raw L1 curve (recommended)
    # Per-lever kill switches, so a backfire can be bisected to ONE mechanism
    # without a code change — the purge and production paths are independent.
    hybrid_purge_lever: bool = True         # 6N move-in sizing (purge weeks)
    hybrid_production_lever: bool = True    # harvest cap + STARVE entry


@dataclass
class BatchInput:
    batch_id: str
    input_date: datetime
    input_count: int
    tran_sf_date: Optional[datetime]
    tran_og_date: Optional[datetime]
    tran_og_count: Optional[int]
    tran_og_avg_wt_g: Optional[float]       # pre-cull target avg weight at TranOG
    tran_og_cv: float                       # CV of batch size distribution (%)
    fcr_model: str                          # "FCR_121_Quick" -> "1.21"
    fw_correction: float                    # multiplier on SGR_FW (FW calibration)
    sgr_correction: float                   # multiplier on SGR_SW (SW user adjustment)
    notes: str = ""


@dataclass
class CalibrationResidual:
    """Per-batch FW calibration residual at TranOG_Date."""
    batch_id: str
    tran_og_date: datetime
    target_avg_wt_g: float
    current_fw_correction: float
    projected_pre_cull_avg_wt_g: float
    residual_pct: float                     # (projected - target) / target * 100
    suggested_fw_correction: Optional[float] = None  # back-solved to land on target


@dataclass
class SizeClassSplit:
    """Post-TranOG 2-class split metadata.

    Computed at the moment of TranOG handling-mortality + reconciliation
    cull. Placement consumes this to seed per-(batch, tank) sub-populations
    across N tanks. Median split of the post-cull distribution; the lower
    half goes to the 'small' class, upper half to the 'big' class.
    """
    batch_id: str
    tran_og_date: datetime
    post_cull_count: float
    post_cull_avg_wt_g: float       # mean of full distribution
    post_cull_cv_pct: float         # CV of distribution (typically batch.tran_og_cv)
    big_class_count: float          # = post_cull_count / 2
    big_class_avg_wt_g: float       # mean of upper half (above median)
    small_class_count: float        # = post_cull_count / 2
    small_class_avg_wt_g: float     # mean of lower half (below median)


def _ascending_permutation(keys) -> Optional[list[int]]:
    """Index order that sorts `keys` ascending, or None if already ascending.

    Returning None for the already-sorted case keeps the common path
    allocation-free, so a well-formed table is passed through untouched.
    Python's sort is stable, so duplicate keys keep their relative order.
    """
    n = len(keys)
    if n < 2 or all(keys[i] <= keys[i + 1] for i in range(n - 1)):
        return None
    return sorted(range(n), key=lambda i: keys[i])


def _permute(seq, perm: list[int]):
    """Reorder `seq` by `perm`, or return it untouched when the lengths don't
    match — a ragged payload column (a short FCR model, an empty list) keeps
    whatever behaviour it has today instead of raising here."""
    if seq is None or len(seq) != len(perm):
        return seq
    return [seq[i] for i in perm]


@dataclass
class BiologyTables:
    """Lookup tables parsed from the Tables sheet.

    `__post_init__` sorts each group ascending by its key column, stably, and
    co-permutes the parallel payload lists. The lookups in `biology.py` clamp
    and scan POSITIONALLY (`_interp` ends at `pairs[-1]`, `_mortality_weekly_pct`
    breaks on the first larger key, `_feed_type_for_size` takes the first
    bracket that fits), so an out-of-order row would silently flatten a curve
    onto that row rather than raise. Enforcing order here fixes every entry
    path at once: YAML load, Excel template import, the app's biology grid,
    and the VBA migration.
    """
    sgr_size_g: list[float] = field(default_factory=list)
    sgr_fw_pct_day: list[Optional[float]] = field(default_factory=list)
    sgr_sw_pct_day: list[Optional[float]] = field(default_factory=list)
    fcr_size_g: list[float] = field(default_factory=list)
    # FCR models keyed by their numeric tag, e.g. "1.21", "1.18", "1.16",
    # "1.15". Open-ended so new models are data, not code.
    fcr_by_model: dict[str, list[float]] = field(default_factory=dict)
    mortality_week_from_input: list[int] = field(default_factory=list)
    mortality_pct_weekly: list[float] = field(default_factory=list)
    # Feed-type schedule: (max_size_g, feed_name), sorted ascending by max_size.
    feed_types: list[tuple[float, str]] = field(default_factory=list)
    # PER-WEEK OG (seawater) SGR factor, keyed by ISO week label. An operator
    # input from `scenario/limits.yaml` (facility metric `sgr_correction_og`),
    # NOT a biology curve: it is the weeks the site knows it cannot achieve the
    # modelled growth — 0.90 for "90% of expected this week". Absent week = 1.0.
    #
    # It rides on BiologyTables because `tables` is the one object already
    # threaded to every growth and feed call site; carrying it separately would
    # mean a second parameter on ~24 signatures and a silent miss on any one of
    # them would desync the projection from the realized walk. Applied in
    # `biology.sgr_pct_per_day`, the single source for the growth rate, so
    # growth AND the feed derived from it (biomass x SGR/100 x FCR) both move
    # together — operator decision 2026-08-19.
    og_sgr_by_week: dict[str, float] = field(default_factory=dict)
    # Culling schedule: (days_since_input, cull_pct), sorted ascending by day.
    culling: list[tuple[int, float]] = field(default_factory=list)

    def __post_init__(self):
        # Five independent key columns. sgr_size_g and fcr_size_g are SEPARATE
        # keys (the YAML permits them to diverge), so they permute separately.
        perm = _ascending_permutation(self.sgr_size_g)
        if perm is not None:
            self.sgr_size_g = _permute(self.sgr_size_g, perm)
            self.sgr_fw_pct_day = _permute(self.sgr_fw_pct_day, perm)
            self.sgr_sw_pct_day = _permute(self.sgr_sw_pct_day, perm)
        perm = _ascending_permutation(self.fcr_size_g)
        if perm is not None:
            self.fcr_size_g = _permute(self.fcr_size_g, perm)
            self.fcr_by_model = {k: _permute(v, perm)
                                 for k, v in self.fcr_by_model.items()}
        perm = _ascending_permutation(self.mortality_week_from_input)
        if perm is not None:
            self.mortality_week_from_input = _permute(
                self.mortality_week_from_input, perm)
            self.mortality_pct_weekly = _permute(self.mortality_pct_weekly, perm)
        # Self-contained (key, value) tuples — sort in place by the key.
        if _ascending_permutation([t[0] for t in self.feed_types]) is not None:
            self.feed_types = sorted(self.feed_types, key=lambda t: t[0])
        if _ascending_permutation([t[0] for t in self.culling]) is not None:
            self.culling = sorted(self.culling, key=lambda t: t[0])


@dataclass
class TankConfig:
    location_id: str
    system_id: str
    tank_id: int
    volume_m3: float
    max_density_kg_m3: float
    max_feed_kg_day: float
    type: str                # "FW" or "OG"


@dataclass
class FacilityConfig:
    tanks: list[TankConfig]


@dataclass
class BatchWeekState:
    """Projected weekly biological state for one batch in one ISO week.

    `week_label` (ISO format like "2026-W20") is the canonical week
    identifier — chronological sort by string works correctly across
    year boundaries ("2026-W52" < "2027-W01").
    """
    batch_id: str
    week_label: str                # canonical week id, e.g. "2026-W20"
    week_start: datetime           # date of the week's start
    days_since_input: int
    week_from_input: int           # batch-local week count (mortality table lookup)
    count: float
    avg_weight_g: float
    biomass_kg: float
    feed_kg_day: float
    feed_kg_week: float
    sgr_pct_day: float
    fcr: float
    stage: str                     # "EGG" | "FW" | "SW"
    feed_type: str
    mortality_pct_weekly: float
    cull_event_pct: float = 0.0    # >0 if a culling event landed in this week
    cull_count_week: float = 0.0   # absolute fish removed by culls in this week
    cull_biomass_kg_week: float = 0.0  # biomass of fish removed by culls in this week
    # End-of-week (close) state — the last simulated day's values. `count` etc.
    # above are weekly MEANS (for feed/density); these are the closing balance so
    # the Weekly/Monthly ledger can chain open->close consistently across a week
    # where a mid-week cull fires (esp. the FW->OG TranOG reconciliation cull),
    # instead of the mean under-counting the drop. Default 0 (filled by the
    # projectors; the ledger falls back to the mean when unset).
    open_count: float = 0.0        # start-of-week balance (before the week's
    open_avg_weight_g: float = 0.0  # losses) — used as the ledger's open on the
    open_biomass_kg: float = 0.0    # FIRST forecast week (week 0), where there is
    #                                 no prior-week close to chain from and the
    #                                 weekly mean would mis-state the open.
    close_count: float = 0.0
    close_avg_weight_g: float = 0.0
    close_biomass_kg: float = 0.0
    # Realized fish lost to MORTALITY this week (the daily geometric survival,
    # summed). The Weekly/Monthly ledger uses this instead of open*weekly_rate%
    # so Count_Check reconciles across weeks where the mortality table steps
    # mid-week (early FW) — the end-of-week rate over/under-counts the real loss.
    mort_count_week: float = 0.0
