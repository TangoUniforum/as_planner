# Fish trapped in 6N purge — a livelock, still open

**Status:** defect CONFIRMED and quantified. Two fixes measured and REJECTED.
Both knobs ship off; the shipped plan is unchanged. 2026-08-31.

## The defect

`_run_sixn_purge_week` defers a 6N tank whose fish would not fit in the week's
remaining processing budget:

```python
if (budget.used > 0 or pair_drain_count > 0) and tank.count > budget.remaining():
    hold it, "drains next rotation"
```

For a tank holding close to a full week's limit that promise **can never be
kept**. Any earlier harvest in the week leaves too little room, and nothing makes
the big tank go first, so the same hold repeats on every rotation.

Measured on the 2026-03-31 close, tank **OG6N-69**, batch B40:

| | |
|---|---|
| entered purge | 2026-W16 with 53,006 fish |
| still there | 2027-W20 with 51,516 fish |
| rotations held | **58** — W18, W21, W24, W27, W30, W33, W36, W39, W42, W45, W48 … |
| weight | frozen at 4.371 kg throughout (purge is off-feed; correct per the operator ruling) |
| lost to mortality while frozen | 1,490 fish |
| ever harvested | **no** |

**This is not a conservation fault.** Nothing is lost, the fish stand at horizon
end, and every hard gate passes — conservation, no-empty-week and 6N one-way are
PASS in all runs. It is a plan that **cannot physically happen**: salmon held off
feed for a year are dead. The arithmetic balances and the plan is still not real.

## SCALE — CORRECTED 2026-08-31 (the first figures were 2.5x too high)

The original detector counted occupancy ROWS per (tank, batch). A large batch
legitimately moves through 6N over SEVERAL rotations — filling, purging and
draining each time — so that read normal rotation as one long spell. Caught by
the operator: batch B45 in tank 61 was reported "held 8 weeks" when it was four
separate two-week purges, with the count RESETTING upward between them.

A real spell is one unbroken residency: consecutive weeks whose count only ever
falls. A rise is a new fill; a gap ends the spell. Mortality-only decay across
unbroken weeks is the stuck signature.

| state | first reported | corrected | longest | verdict |
|---|---|---|---|---|
| 2026-03-31 | 402 t | **402 t** | 58 wks | FAIL |
| 2026-07-31 | 1,463 t | **493 t** | 54 wks | FAIL |
| 8.13 PR | 915 t | **183 t** | 53 wks | FAIL |
| 2026-01-31 | 143 t | **143 t** | 11 wks | FAIL |
| 8.23 PR | 199 t | **0 t** | — | PASS |
| 2026-05-31 / 2026-06-30 / LIVE | 0 t | 0 t | — | PASS |
| **total** | 3,121 t | **1,222 t** | | |

The defect is unchanged and still real — tank OG6N-69 held 53,006 fish across 58
unbroken weeks, decaying only by mortality. What changed is how much of the
facility it affects: four states, not five, and 1,222 t rather than 3,121 t.

## It manufactures the sub-floor harvest weeks

A trapped tank means its **pair partner drains alone**. That lone drain is the
recurring **7,129** seen across unrelated starting states:

```
min_tank_control (7,000) x _REMNANT_KEEP_PAD (1.02) = 7,140
7,140 eroded by ~0.15% mortality over the purge  = 7,129
```

So the sub-floor week is a *symptom* of the livelock, not an independent problem,
and it is manufactured by a rule — not a shortage of fish. In one such week
**113,131 mature fish were sitting in 6N** while the plan harvested 7,129.

## Two fixes measured, both rejected

* `sixn_drain_largest_first` — drain the pair's biggest tank first.
* `sixn_overdue_drain_weeks` — a tank past N weeks in purge gets first claim on
  the week and is exempt from the hold, draining into the exceptional relief band
  the code already reserves for this case.

Eight states, shipped config as baseline:

RE-MEASURED with the corrected detector:

| state | trapped t (base / od4 / od4+big) | weeks below floor | worst week |
|---|---|---|---|
| 2026-01-31 | 143 / **0** / **0** | 7 / **12** / 7 | 7,763 / 7,589 / 7,763 |
| 2026-03-31 | 402 / **0** / **0** | 13 / 12 / **8** | 7,129 / 7,129 / **8,825** |
| 2026-05-31 | 0 / 0 / 0 | 3 / 3 / 3 | unchanged |
| 2026-06-30 | 0 / 0 / 0 | 5 / 5 / 5 | unchanged |
| 2026-07-31 | 493 / **110** / **30** | 10 / **5** / **16** | 7,125 / **16,740** / **1,684** |
| 8.13 PR | 183 / **32** / 244 | 5 / 3 / **0** | 7,261 / 7,261 / **30,012** |
| 8.23 PR | 0 / 0 / 0 | 3 / 3 / 3 | unchanged |
| LIVE | 0 / 0 / 0 | 2 / 2 / 2 | unchanged |
| **total trapped** | **1,222 / 142 / 274** | | |

**The benefit is much larger than first reported.** `od4` removes **88%** of the
trapped biomass (1,222 → 142 t) and clears two states outright. The earlier
"about a third" was an artifact of the row-counting detector, not a property of
the fix.

What disqualifies them as defaults is the **contract floor**, which the detector
bug never touched: `od4` takes 2026-01-31 from 7 to 12 weeks below the weekly
minimum, and `od4+big` takes July'26 from 10 to 16 and drives its worst week to
**1,684 fish**. Better on three states and materially worse on one is not a
default — but a variant keeping od4's gain without the 2026-01-31 regression
would be a real fix, and the gain is now known to be worth chasing.

An earlier within-pair-only variant behaved the same way — it fully cleared
2026-03-31 (+38,289 fish harvested) and took 8.13 to zero floor misses, but made
2026-01-31 worse on every axis and left 507–713 t trapped on the two worst states.

## ROOT CAUSE (found 2026-08-31, after the ordering attempts)

`_make_room_into_6n` moves **one growout tank's WHOLE population into ONE 6N
tank**, with no check that the result can ever be harvested in a single week.

Growout tank populations on the July'26 close (1,856 tank-weeks):

| | fish |
|---|---|
| median | 37,513 |
| p90 | 96,149 |
| max | **164,870** — three weeks of harvest capacity |
| **tank-weeks above one week's limit (55,000)** | **536 (29%)** |

A 6N tank is drained WHOLE. So any make-room dump of a tank above
`max_harvest_per_week` produces a 6N tank that **can never drain in one week**,
whatever the drain order. It is born un-harvestable. That is why every fix so
far only half-worked — all of them are downstream of the tank being created.

Confirmed by the fill-side clamp. `sixn_level_drains` (re-measured with the
corrected detector) cuts trapped biomass by **70–80%** — July'26 493 → 145 t,
8.13 PR 183 → 32 t — and improves the contract floor markedly, floor misses
10 → 4 and 5 → 1. It also costs: per-system feed worsens on both (58 → 64,
50 → 69) and 8.13 exceeds the 15-move handling budget at 17.

And it leaves **the biggest tank unchanged**, 89,397 → 91,276. It caps the
pair's weekly fill; it cannot stop a whole oversized growout tank being dumped
in. The trapped-fish gain comes from not over-stuffing a pair week to week, not
from fixing the dump — which is why the root cause below still stands.

The feed cost has a mechanism, not just a correlation: capping the fill leaves
the surplus in GROW-OUT, where fish keep eating, instead of moving them into 6N
where they go off feed. Steadier harvest is bought with feed load, by choosing
where the fish wait.

**Note** `sixn_level_drains` is in `UNTUNABLE_KNOBS` — an operator input, held
out of every automated search on purpose. It was measured here through
`--allow-operator-inputs`, an explicit flag that announces every such run. That
guard should stay: it exists so a SEARCH cannot win by redefining the operation.

### The remaining options each violate something

| option | what it costs |
|---|---|
| partial drain of an oversized 6N tank across consecutive weeks | breaks whole-tank drain and the pair-empties-to-rest rotation |
| refuse make-room when the dump would be undrainable | may block TranOG placement, cascading |
| split the dump across the pair's two tanks | **forbidden** — the reverted approach; the sister exists only for a second, different batch |

Physically, option 1 is the honest one: in reality you *would* harvest a big
tank over two weeks. It is an operator/architecture decision, not a tuning one.

### Meanwhile the defect is at least VISIBLE

A soft gate — **"Fish stuck in 6N purge"**, gate 12 — now reports it: PASS when
every tank drains within its rotation, WARN past 5 weeks, FAIL past 8. Soft on
purpose: the root cause is unfixed, so a hard gate would disqualify most plans
with no way to pass. On the live workbook it reads WARN (8 spells past 5 weeks,
longest 6 — running late, nothing stuck); on July'26 it reads FAIL with
363,116 fish.

## Why ordering cannot fix it

Reordering *which* tank drains only moves the blockage. The binding fact is that
a **53,000-fish tank cannot coexist with any other harvest inside a 55,000
weekly limit**. Whichever tank goes first, the other is held — and on the states
where the big tank finally drains, it displaces the harvest that would otherwise
have made those weeks legal, which is why the floor regressions appear.

**The fix belongs upstream: do not FILL a 6N tank to near the weekly processing
limit.** That is fill sizing, where `sixn_level_drains` already operates (it caps
a fill by the pair's remaining headroom, and ships off). A fill that can never be
drained in one week is the defect; the drain is only where it becomes visible.

This is the fourth measured revert in the 6N ordering class. The prior three are
recorded in `placement.py` and in the 6N fill/drain notes. **Read those and this
before attempting a fifth.**

## Reproducing

```
python -m tools.measure_leveling --pr <PR.xlsx> \
    --combos '[{"name":"base","overrides":{}},
               {"name":"od4","overrides":{"sixn_overdue_drain_weeks":4}}]' \
    --out res.jsonl --tag t
```

The holds are already logged — no probe needed. Search `ValidationLog` for
`HARVEST LIMIT` and `DEPURATION HOLD`; a tank name repeating every third week is
the livelock. `SIXNFILL_PROBE=1` (env-gated, stdout) dumps the fill side, and
needs `quiet=False` since `methods.run_method` redirects stdout by default.
