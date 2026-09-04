# Per-week operator inputs — one number, honoured everywhere

> **STATUS — superseded in part, later the same day (2026-09-03).** This note
> was written in the morning, before three commits that answered its open
> items. Read it for the reasoning; take the numbers from the commits.
>
> - **"What the peel still cannot do"** (the `min_transfer_count: 7000` wall)
>   was FIXED in `0e36f87`. The floor-fill peel now has its own
>   `min_grade_count` (unset = inherit `min_transfer_count`, so the default is
>   unchanged), and the cap-before-minimum ordering was corrected. At the live
>   3,300 g gate: floor misses 3 → 1, shortfall 5,686 → 1,172 fish.
>   ⚠️ Still open: only ONE of the three graded peels was converted.
>   `placement.py`'s entry-tier graded transit and the 2028 production-era
>   stage both still read `min_transfer_count` and still cap before testing the
>   minimum.
> - **The live row counts** ("17 biomass, 17 feed_per_day") are stale.
>   `576c21c` extended the derate across the whole horizon: 85 and 85, spanning
>   2026-W36..2028-W15. The 16 `min_harvest_per_week` and 9 `sgr_correction_og`
>   rows are unchanged.
> - **The silent-fallback problem this note identified** is now detected at run
>   time: `f50c278` added the `INFO - Per-week coverage` ValidationLog line,
>   which names any facility metric whose rows stop before the horizon ends.
>   Detection only — no cap resolves differently.

**2026-09-03. Operator ruling, binding:**

> "if we are defining a certain min count to harvest then the entire system
> should honor the same number… even if it measured worse it is correct to
> operate from one truth."

## The shape of the defect

`scenario/limits.yaml` lets the operator set a value **per week** for six
metrics. `caps.resolve_facility_cap(metric, week_label, facility_limits,
control)` is the only correct way to read one. Code that instead reads
`control.<metric>` gets the **Control default** and silently ignores every
per-week row the operator wrote.

Metrics that support per-week overrides:

| metric | constant | live rows on the 8/31 scenario |
|---|---|---|
| `biomass` | `METRIC_BIOMASS` | 17 |
| `feed_per_day` | `METRIC_FEED_DAY` | 17 |
| `min_harvest_per_week` | `METRIC_MIN_HARVEST` | 16 |
| `sgr_correction_og` | `METRIC_SGR_OG` | 16 |
| `max_harvest_per_week` | `METRIC_MAX_HARVEST` | 0 |
| `hog_yield` | `METRIC_HOG_YIELD` | 0 |

Four instances of the same defect were found in one session. They are not
coincidences — they are one class:

1. **`_gate_biomass_cap`** judged the horizon PEAK against a single cap.
   Reported 111.1% where the true worst was **114.0%** of that week's own cap.
2. **`_harvest_extras`** compared every week to the Control default.
   Reported "3 weeks below 30,000" where the truth was **7 weeks and 119,311
   fish** below the floors the operator had written.
3. **6N fill/peel sizing** (this document) — four sites in `placement.py`.
4. **The ProductionReport reader** — same disease, different organ: fixed
   column positions against a sheet whose layout had moved three times.

## What was fixed here

Four sites in `forecast/placement.py`, all previously reading
`control.min_harvest_per_week`:

| site | what it sizes |
|---|---|
| `_transit_entry_to_pair` — goal cap | entry-tier transit into the fill pair |
| `_transit_entry_to_pair` — `_floor_goal` | the graded entry transit |
| `_run_sixn_purge_week` — `min_h` | `_min_fill`, `target`, and the peel clamp |
| `phase_d_emit_events` — `_floor_p` | production-era graded stage (inert until 2028-01-01) |

**The mechanism.** The floor-filling graded peel is clamped by
`_floor = min(_min_fill, target)`, and `_min_fill = min_h * 1.002`. With
`min_h` at the global 30,000 the peel filled to 30,060 and stopped, regardless
of a 53,000 operator floor. The pair drained
`30,060 × 0.9995² = 30,030` two weeks later — the observed December harvest, to
the fish. Confirmed in **every one of 408 peel-gate evaluations**.

**The fill is sized by the DRAIN week's floor**, not the fill week's.
`placement.py` says so itself at the `drain_idx` computation: *"move-in drives
harvest at week t+lead"*. A floor is a promise about the harvest week.

## Measured effect — NOT a clean win

2026-08-31 PR, 85 weeks, real pipeline:

| | before | after |
|---|---|---|
| shortfall vs the operator's own floors | 119,311 fish | **74,999** |
| 2026-12 | 451.3 t | **535.2 t** |
| 2026-11 | 616.7 t | 708.3 t (over its 650 target) |
| 2027-01 | 672.1 t | **401.2 t** (−271) |
| 2027-03 | 608.9 t | 825.9 t (+217) |
| worst harvest week | 27,325 | **16,145** |
| weeks below their own floor | 7 | **9** |
| total HOG | 11,558 t | 11,481 t |
| average harvest weight | 3.355 kg | 3.334 kg |

Harvesting to a higher floor takes **the same fish earlier and lighter**, so
count holds while tonnage falls slightly. December improves because the peel is
finally allowed to reach the floor; January pays for it.

**This was shipped for correctness, not for the number.** Do not later read the
December improvement as proof the change was an optimisation.

### An earlier, partial version measured worse

A first attempt corrected only `_run_sixn_purge_week`, sized by the FILL week:
shortfall 103,929, worst week **10,658**, December **373.6 t**. Doing all four
sites with drain-week semantics is materially better on every axis. If this is
ever revisited, do not re-derive the partial form.

## The no-op guarantee

A week with no override must be bit-identical to before. The fallback tests
**truthiness**, not `is not None`, because `resolve_facility_cap` returns `None`
when the resolved value is ≤ 0 and a `0.0` must not be allowed to zero the
sizing.

Verified end to end: with every `min_harvest_per_week` row stripped from
`limits.yaml`, the patched engine reproduced the unpatched plan exactly —
**85 weeks compared, 0 differing, 3,445,527 fish both sides.**

Pinned by `tests/test_per_week_floor_sizing.py`.

## What the peel still cannot do

Behind the floor clamp sits a second wall: `min_transfer_count: 7000`
(`control.yaml`, enforced in `_try_graded_move_in`). After the peel takes the
heavy tanks, the largest remaining gradeable tail is ~6,300 fish — below the
minimum transfer size — so it stops. That limit has never been measured and is
the next thing to test if December is still short.

The peel itself is **not** the constraint people assume: it is explicitly the
*"graded harvest fallback when no batch's avg_wt is above min_harvest_weight"*
and computes the normal-tail fraction from each tank's CV. It can and does grade
a 3,438 g tank. `min_harvest_weight_g` does **not** need lowering to make it
work — lowering it would make whole sub-threshold tanks harvestable, which is
the opposite operation and costs average weight.

## Still open

- **The same-shape sweep.** ~106 reads of the five Control defaults across
  `forecast/` and `tools/`. Most are legitimate (defaults dict, display, config
  round-trip). Each needs one question asked of it: *does this decision happen
  per week, and if so does it resolve per week?*
- `max_harvest_per_week` and `hog_yield` have no per-week rows today, so any
  defect in their read path is currently invisible rather than absent.
