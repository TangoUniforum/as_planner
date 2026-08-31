# The leveling trade, re-measured — 2026-08-30

50 forecasts on the 8.23.26 PR, every rebalancer/repair lever, ranked on
CONSTRAINTS (hard gates → contract floor → per-system feed → handling → score).
Score is reported throughout and never decisive. Operator inputs
(`max_transfers_per_week`, `density_target_pct`, `min_harvest_per_week`,
`tran_og_default_tanks`, `min_tank_control`) were refused by the driver, not
merely omitted. Reproduce with `tools/measure_leveling.py`.

## The headline is a null result

**`system_feed` FAILS in 50 of 50 runs.** No configuration of any lever, alone
or crossed, produces a feedable plan on this PR. The best feed result in the
entire search is 17 breaching system-weeks (from 67) and it costs the contract.
Everything below is about *reducing* an unfixed breach, not clearing it.

## What the standing figure got wrong

"Enabling the balancer halves breaches but costs $7.7M and five sub-floor weeks"
was measured under the old objective, one knob at a time. Its *direction* holds
— the balancer does buy feed relief and does break the floor — but it framed a
five-pass family as one switch, and the pass that matters most was never in it.

`cap_repair_budget` was **0 = OFF**. It is the only pass that runs AFTER the
week's biology, immediately before the snapshot `SystemLimitsAudit` measures.
Every other pass aims at START-of-week load while the metric reads END-of-week,
a full week of growth later (~+7% biomass, ~+11% feed).

## The recommendation: `cap_repair_budget: 4`

| | baseline | **cap_repair=4** | delta |
|---|---|---|---|
| feed system-weeks over cap | 67 | **42** | −25 (noise range 13) |
| systems breaching | 9 | **8** | −1 |
| weeks below contract floor | 3 | 4 | +1 (noise band 2–5) |
| worst harvest week | 26,347 | **28,274** | **+1,927** |
| total harvest | 2,485,489 | 2,483,184 | −2,305 (0.09%) |
| moves/week peak | 15 | 15 | at cap, never over |
| hard gate failures | 0 | 0 | — |

One lever, no operator input touched, no hard gate, no handling breach, and the
worst harvest week improves. It introduces one `density_quality` WARN — a
noise-scale flip (the neutral perturbation `nf_pressweeks4` flips the same gate).

**It saturates at 4.** Budgets 4, 15 and 30 are metric-identical; only
`cap_repair=2` differs, and it is worse (harvest 2,471,243, −14,246, a bigger
loss than any neutral lever produced). 4 is the setting.

## What deep feed relief actually costs

Every config that reaches 17–23 breaches uses `rebalance_balance_budget`, and
every one breaks the sales contract:

| config | feed | weeks below floor | worst week | harvest |
|---|---|---|---|---|
| baseline | 67 | 3 | 26,347 | 2,485,489 |
| `capr4_bal8` | **17** | 9 | 23,144 | 2,465,537 |
| `capr4_bal4` | 18 | 9 | 20,485 | 2,465,553 |
| `capr4_bal8_fr1` | 18 | 11 | **7,128** | 2,465,614 |

That is the real trade, and it is a business decision, not a tuning one:
roughly a 75% cut in feed breaches against tripling the weeks that miss the
weekly harvest contract. `min_harvest_per_week` is a sales commitment — the
tuner has no standing to spend it.

Note 7,128–7,129 recurs as the worst week across several of these. It is the
same lone-sub-floor-drain signature recorded in the 6N fill/drain findings: a
remnant draining alone instead of sharing its week.

## Methodology note — the noise floor is chaos, not randomness

Eight neutral perturbations (knobs that should barely matter) produced:

| metric | range across neutral levers |
|---|---|
| feed system-weeks over cap | 13 |
| weeks below floor | 3 (2–5) |
| **worst harvest week** | **8,629 fish** |
| total harvest | 14,040 (0.56%) |

Any improvement smaller than these is not evidence. Two consequences:

1. **`feed_worst` is useless as a discriminator.** 1.2135 is both the global
   minimum across all 50 runs and the bottom of the neutral band — the whole
   observable range is noise.
2. **The engine is deterministic** — four configs are bit-identical to baseline
   across all 16 metrics including 16-digit floats. So this band is *chaos
   sensitivity*, reproducible and systematic, not run-to-run scatter. Calling it
   a "noise floor" invites treating a real, repeatable degradation as random.

## A rejected candidate, and why

The sweep's own synthesis recommended a three-lever config
(`cap_repair_budget=4, chronic_relief_pct=0.7, chronic_max_frees_per_week=1`,
feed 37) as costing nothing. An adversarial pass refuted it and the refutation
verified:

- it harvests 2,474,207 — **1,991 fish below the minimum of the entire neutral
  band**, a loss ~10x anything a neutral lever produced. "Zero measurable cost"
  was wrong.
- it is **not separable from `cap_repair=4` alone**: feed 42→37 is 5 against a
  band of 13, `min_week` is identical at 28,274 — while it systematically loses
  8,977 more fish, breaches 10 systems instead of 8, and posts the highest
  `transfers_per_fish` of all 50 runs.

Buying noise-scale wins with systematic losses, then reporting it as dominant.
The single-lever change is the honest one.
