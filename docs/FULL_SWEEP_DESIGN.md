# Full Sweep — design

**Status:** design, not built. Preconditions complete 2026-08-30.
**Operator brief:** *"one long run to decide on the best method, then use that
method more quickly"* — and *"the best plan, not just the best score."*

---

## 1. The problem

Four modes point in the same direction and overlap badly:

| mode | what it does | overlap |
|---|---|---|
| **Configure** | the setup | — |
| **Run forecast** | one run on the live config | — |
| **Analyze** | roster + knob search + gates + one recommendation | runs the same legs as Compare |
| **Compare & Choose** | same legs, graded on eight lenses, no knob search | *is* Analyze Phase 1 + lenses |
| **Optimize** | knob search with custom weights + Pareto | same search, different presentation |

Analyze's Phase 1 loop and Compare's run loop are two copies of the same code
over the same roster writing the same cache (`app.py:7646-7679` vs `7055-7098`).
An operator has to know which mode answers which question — and the answers do
not compose.

**Target shape:**

```
Configure  ->  Full sweep  ->  preset  ->  Run forecast
                   ^                            |
                   +-- partial sweep reuses ----+
```

Two modes an operator must understand instead of four: *decide* (long, thorough,
occasional) and *run* (fast, routine).

---

## 2. Precondition — why this could not be built first

A sweep optimises what it can measure. An **exhaustive** search over a partially
blind objective finds the blind spots *faster* than a partial one: an unmeasured
violation is free score, so the search is actively drawn to it.

Three measured examples from this codebase:

- The tuner drove the worst harvest week to **11,510 fish** against a 30,000
  floor to gain marginally on peak biomass. A guard vetoed it; the objective had
  no floor term at all.
- `rebalance_balance_budget: 0` scores **best** on the 8.23.26 PR (+$7.7M, five
  fewer sub-floor weeks) *and* takes per-system feed breaches from 36 to 67,
  because the relief pass is `for _ in range(budget)`. Nothing connected the two.
- Per-system feed sat over cap in **67 of 720 system-weeks, worst 1.318x**, with
  no gate and a count-based score term that could not see severity.

**The rule this produced:** every constraint that matters needs BOTH halves — a
**gate** so a person sees it, and a **continuous score term** so a search feels
it. A gate alone cannot separate two failing plans; a score alone never tells
anyone.

State as of 2026-08-30 — the precondition is met:

| constraint | gate | score term |
|---|---|---|
| facility biomass | `biomass_cap` | `biomass_overshoot` |
| per-tank density | `density_quality` | `density_overshoot` |
| per-system feed/biomass | `system_feed` | `system_overshoot` (magnitude-weighted) |
| contract floor | `harvest_floor` + no-regression guard | `harvest_floor_gap` |
| processing limit | `harvest_cap` | `harvest_overshoot` |
| handling budget | `handling_budget` | `transfers_per_fish` |
| 6N one-way (R7) | `sixn_one_way` (HARD) | — (hard gate; no gradient needed) |
| conservation | `conservation` (HARD) | rejects the variant outright |

**Before adding a phase, re-check this table.** A new constraint with only one
half re-opens the hole.

---

## 3. The integration spine

The phases compose because each one **reads and writes named artifacts**, and
the expensive artifact is produced exactly once. This is the whole design:

```
          expensive, cached                  cheap, re-derivable
   +---------------------------+   +----------------------------------+
   |  A. RUN SET               |   |  D. SCORES (per emphasis)        |
   |     engine outputs        |-->|     pure f(B, weights)           |
   |  B. METRICS  (schema-     |   |  C. GATE VERDICTS                |
   |     stamped)              |-->|     pure f(workbook)             |
   +---------------------------+   +----------------------------------+
                                              |
                                              v
                                   +----------------------+
                                   |  E. PRESET           |
                                   |     winner + knobs   |
                                   |     + provenance     |
                                   +----------------------+
```

| artifact | keyed by | produced by | cost |
|---|---|---|---|
| **A** run set | PR md5 + config fingerprint + engine fingerprint + method + knobs | Phase 1 | ~15 s/run (controller) |
| **B** metrics | A + `METRICS_SCHEMA` | Phase 1 | reading, seconds |
| **C** gate verdicts | A + gate registry | Phase 2 | seconds |
| **D** scores | B + emphasis weights | Phase 2 | **free** |
| **E** preset | C + D + guards | Phase 3 | free |

**Three consequences that make the phases work together:**

1. **Emphasis is free.** Scoring is a pure function of cached metrics, so ONE
   run set is scored under ALL eight emphases. The sweep does not pick an
   objective and search it — it searches once and reports how the winner moves
   across objectives. This already exists as a tool (`run_emphasis_sweep`).
2. **Partial sweep is just a cache hit.** A later sweep on the same PR + config
   reuses every matching entry in A and only runs what is new. No separate code
   path — "partial" is a *coverage target*, not a different algorithm.
3. **Changing a metric or a gate invalidates cleanly.** `METRICS_SCHEMA` keys B;
   bump it and stale entries are dropped and recomputed. Gate verdicts are
   re-derived from the workbook every time and never cached across a rule change.
   (2026-08 lesson: stale cached metrics defeat a metrics fix silently.)

---

## 4. Phases

### Phase 1 — the run set (A + B)

Enumerate `method x knob combination`, run each, cache metrics.

Real sizes, measured:

- controller knob space = 5 knobs -> **162 combinations**
- 3 controller arms -> **486 runs ≈ 2 h** at ~15 s — genuinely exhaustive
- `global-lp` ~4 min/run -> 162 runs ≈ 11 h
- `global-milp` ~30 min/run -> 162 runs ≈ **81 h** — needs a coarse sub-grid

**Operator inputs are never searched.** `UNTUNABLE_KNOBS` holds 16 of them
(`min_harvest_weight_g`, `max_harvest_per_week`, `tran_og_default_tanks`, the
hybrid levers, …) and `register()` refuses any method whose space intersects it.
A search that could relax those would "win" by breaking the operator's contracts.

Standalone value: Phase 1 alone replaces a tuned tournament.

### Phase 2 — judge (C + D)

For every run: gate verdicts, then scores under all eight emphases.

Output is a **matrix**, not a ranking: candidate x emphasis. This is where the
sweep earns its length — the same run set answers "what if I cared about
handling instead?" without another engine run.

Ranking within an emphasis is the existing lexicographic key:
`(hard_fails, soft_fails, warns, target_shortfall, score)`. It degrades
gracefully: if every candidate fails the same soft gate they tie on that tier
and the decision falls through to warnings, then shortfall, then score — where
the magnitude-weighted components separate them.

Standalone value: Phase 2 alone re-judges an existing run set after a gate or
metric change, with no runs at all.

### Phase 3 — decide and record (E)

Apply the winner-eligibility guards (`tournament.pick_winner` /
`optimize.eligible_pool`), then write the preset.

**The preset must carry provenance**: PR identity, config fingerprint, code
version, emphasis, thoroughness level, date, and the gate verdicts it won with.
The current promoted default carries a hand-written caveat that its scores
predate a basis fix — provenance is what removes the need for such caveats.

**Guards must be HARD in an unattended run.** Today a guard that cannot be
satisfied "stands down" and the UI flags it amber for a human to notice. Nobody
watches a two-hour sweep: a stood-down guard must be a refusal to write the
preset, or a headline in the result — never a quiet amber.

---

## 5. Thoroughness levels

Not different algorithms — different **coverage targets** over the same spine.

| level | covers | ~time | typical use |
|---|---|---|---|
| **Quick** | cache-warm; runs only what is missing | minutes | after a small config edit |
| **Standard** | 3 controller arms x full knob grid | ~2 h | monthly, after a new PR |
| **Full** | + Global arms on a coarse sub-grid | overnight | quarterly, or when the roster changes |

Every level writes the same artifacts, so a Quick sweep's runs are reused by a
later Standard one. **Report what was searched AND what was not** — silent
truncation reads as "we covered everything."

---

## 6. Capabilities that must survive

Retiring Compare & Choose and Optimize as *modes* is fine; losing these is not.
Eleven, from the mode audit:

1–5. the four lenses with no Analyze equivalent (between-system CV, within-system
CV, tank footprint, fastest run) plus the raw `density_peak` lens
6. the per-method metric readout (peak % cap, moves/fish, density, both CVs,
   reared kg/m³, % crowded)
7. setting the standing engine to a **non-winning** method
8. picking a plan while writing **nothing** to config
9. the force re-run (`store.clear()`) — the only cache invalidation in the app
10. the ~100-second engine-only path
11. the partial-roster warning

Plus two the merge plan originally missed:

12. **the CP-SAT solve-depth control** — lives only in Compare, but is read by
    five call sites *including Analyze*
13. **naming a failed engine leg and its error** — Analyze drops `ok == False`
    legs silently

**House rule:** this repo retires a mode with a dated section enumerating where
each capability went (see USER_GUIDE §13.1, "Tune mode retired 2026-08-06").
Retiring two modes without that record is the documented failure mode.

---

## 7. Risks

| risk | handling |
|---|---|
| search optimises into an unmeasured constraint | §2 table — both halves, re-checked before each phase |
| a guard stands down unnoticed overnight | hard refusal or headline, never amber |
| stale cache mixes old and new metric meanings | `METRICS_SCHEMA` bump invalidates |
| preset applied to a PR it was not chosen on | provenance + Run forecast re-checks gates cheaply |
| Global's runtime swamps the sweep | coarse sub-grid, opt-in, its cost stated up front |
| "no better plan found" reads as failure | first-class result: report the search and the null |

**The starting state dominates the answer.** On the 8.23.26 PR the controller
had 1 avoidable red week; on July'26, 8. A preset is a good starting point for
next month, never a permanent answer — which is why Run forecast re-runs the
gates rather than trusting the preset's stored verdicts.

---

## 8. Out of scope

- Auto-applying growth recalibration. `BatchAccuracy.sgr_scale` compares
  forecast to actuals and **applies nothing**; it stays a comparison
  (operator instruction, 2026-08-30).
- Tuning any `UNTUNABLE_KNOBS` entry.
- Changing what the engines do. The sweep chooses among plans; it does not
  invent them.
