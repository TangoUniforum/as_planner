# Where things stand

Updated at the end of every working session (`/forecast-handoff`). Newest first in
each list. Last updated: **2026-11-05**.

## LIVE since 2026-11-02: "Scenario A" forecast trial (10.29 PR, to 2029-W07)

- Built on the 10.29 sheet-moves set (same manual events/taper). Every batch Actual 2026-10-18 x 0.90, new FCR curve
  '1.20' (biology.yaml, between 1.18 and 1.21); B53-B59 330k to OG, B60+ 310k (B51/B52 290k); biomass cap 4,000 t 2027
  rows / 4,200 t 2028+; feed 28 t/d 2027 rows / 32 t/d 2028+; min weight 3,100 g W01-W02, ramp 3,300 -> 3,500 g W09-W30,
  3,500 g Control; 2028+ weekly fish limit 48,500; horizon 121.
- 2027 5,857 t (26.9 t/d feed, max 4,075 t standing); 2028 7,048 t (29.6 t/d, max 4,278 t). Operator findings: 32 t/d at
  3.8 kg needs ~4.7-5.0 kt (not housable); 2027 feed capped by fish already in water under 4,000 t.
- Saved set `..\_backups\scenario_sets\Scenario A - 10.29 PR, growth 90pct, ...\` (README); backup
  `..\_backups\2026-11-02_before_scenario_A\`; tag `v2-before-scenario-A-2026-11-02`. Re-run from live = candidate.
- To go back: copy the 5 files from the backup (or the saved set "10.29 PR sheet moves, graded 6N, ...").
- Operator asked to use the app (Ideal -> Transition) as the sandbox for further batch-size/cap trials.
- **Re-based to the 10.31 PR (2026-11-02 later):** events `scenario/manual_events/2026-10-31.yaml` (61 already in 63 -> "harvest 61"
  dropped); 2027-W02 cap 38,000, 2026-W53 min weight 3,000 g. 2027 5,883 t / 2028 7,036 t; known 2027-W29..W32 gap 53-85 t.
  Saved set `Scenario A - 10.31 PR, ...`; backup `..\_backups\2026-11-02_before_10.31_pr\`. Run the app on the 10.31 PR.
- **Flat 3,400 g in 2027 (2026-11-02 latest, LIVE):** min weight rows 2027-W09..W52 = 3,400 g (ramp to 3,500 g left a B49->B50
  gap W29-W32). 2027 5,896 t, all weeks >= 99 t, max 3,884 t; 2028 6,928 t. Open: 2028-W09 77 t, 2027-W23 156 t.
  Saved set `Scenario A - 10.31 PR, ... min 3.4kg 2027 3.5kg 2028, ...`; backup `..\_backups\2026-11-02_before_flat_3400\`.
- **Scenario A mod 290k LIVE (2026-11-02, latest; not Scenario B):** Scenario A flat-3,400 but 290k for all future batches, hold ~110 t (34k fish cap)
  2027-W08..W38 and 2028-W01..W30 then ride the 4,000/4,200 t limits, 3,700 g from 2028. 2027 5,888 t (3.72 kg), 2028 6,435 t
  (3.93 kg); 9 weeks >1% over 4,200 t in 2028 to tidy. Saved set `Scenario A mod 290k - 10.31 PR, ...`; backup `..\_backups\2026-11-02_before_scenario_A_mod_290k\`.
- **Scenario B LIVE (2026-11-02, latest):** same limits as A; FCR 1.15 curve, growth x0.925, 310k from B53; hold 110 t
  2027-W08..W38 and 2028-W01..W08, ride limits (52k Control), 3,500 g from 2028. 2027 6,109 t, 2028 6,759 t, every week >= 96 t,
  max 3,994 / 4,007 t. 310k beat 290k/310k (cap unused) and 310k/330k (short weeks). Saved set `Scenario B - 10.31 PR, ...`;
  backup `..\_backups\2026-11-02_before_scenario_B\`. Scenario A mod 290k: hold-to-W22 fix offered, not applied.
- **CORRECTION + Scenario A v2 LIVE (2026-11-02, latest):** earlier A/B sets applied the growth % to the Actual curve; operator
  wants it on the base SW curve. A v2: B47 and earlier Actual x1.0, B48+ Base x0.90, FCR 1.20 all, 290k, no holds (ride limits),
  44k Control fish cap. 2027 ~6,620 t (feed-bound at 28 t/d), 2028 6,415 t at 4.04 kg. Scenario B to be redone on the same basis.
  Saved set `Scenario A v2 - 10.31 PR, ...`; backup `..\_backups\2026-11-02_before_scenario_A_v2\`.
- **Scenario A v2 hold 2028 LIVE (2026-11-02, latest):** v2 + 34,000 fish/week cap 2028-W01..W24 so standing builds to the
  32 t/d feed limit (2027 already at 28 t/d, no hold there). 2028 6,628 t (+213) at 4.14 kg, feed 31.4-31.6 t/d Jun-Sep,
  max 3,942 t; 2 weeks < 100 t (min 86), max week 204 t; 2027 6,543 t (-78, planner reshuffle). Alternative not taken:
  35k to W26 (2028 6,452 t, 4.08 kg, min week 98, 2027 unchanged). Saved set `Scenario A v2 hold 2028 - ...`; backup
  `..\_backups\2026-11-02_before_scenario_A_v2_hold2028\`; tag `v2-before-scenario-A-v2-hold2028-2026-11-02`.
  Next: operator runs it in the app; then redo Scenario B on the base-curve basis (Base x0.925 B48+, FCR 1.15) with the same hold logic.
- **CULL CAP MERGED e2fce72 + Scenario B v2 LIVE (2026-11-03, latest):** found the culling plan (biology.yaml, unit %) at 0.07 =
  0.07% per cull (~300 fish), so FW carried ~100k surplus fish to the transfer cull (FW feed ~3.4 t/d). Operator: 7% per cull,
  and "never cull below the transfer to OG plan count". New `biology.scheduled_cull_fractions` caps each scheduled cull at the
  TranOG target (both FW projections + both FW calibration solvers); tests/test_cull_cap.py (neg. control: old code 291k vs 330k).
  Golden re-frozen (fixture B06/B07 short of target). Gate 13/24 identical, rest move by batches-already-short no longer culled
  (planner amplifies to up to +-250 t/yr on old PRs). Live 10.31 Scenario A identical on the new code with 0.07%. Suite: 2547 pass,
  least-moves broken-rebuild test flaky under full-suite load (passes alone). Tag `v2-before-cull-cap-2026-11-03`; backup
  `..\_backups\2026-11-03_before_cull_cap\` (code + biology/control/batches/limits).
  Scenario B v2 live: Actual to B47, B48+ Base x0.925, FCR 1.15 all, 290k B50-52, 310k B53+, culls 7/7/7, no hold, same limits.
  2027 6,801 t (3.90 kg), 2028 7,080 t (4.12 kg, 30% 8 lb+), weeks >= 101 t, biomass never over. Scenario A with 7% culls (not live):
  6,618 / 6,546 t. Facility limits are FW-inclusive in the engine (placement.py:5652) - only my old check script excluded FW.
  2028 vs 2027: +862 t growth but +571 t goes into standing (ramp to 4,200 t) and harvested fish ~flat (~7.5 batches/yr).
  Saved set `Scenario B v2 - 10.31 PR, ...`. Next: operator runs it in the app; push; option = 330k to use 2029 feed.
- **Scenario A v3 levelled LIVE (2026-11-03, latest; replaces B v2 as live):** operator asked for 2027 feed 28.5 t/d and steadier
  monthly HOG, A first then B ("don't move to B until A is finished and the workbook reviewed"). A v2 hold 2028 + 7% culls, 2027
  feed rows 28,500, plus per-week max fish caps 2027-W09..2028-W52 from a levelling loop (scenA3/level.py, propose_bands).
  2027 6,610 t (3.92 kg, Apr-Dec 126-134 t/5-day wk), 2028 6,758 t (4.24 kg, 35% 8 lb+; Jan-Jun 109-127, Jul-Dec 138-145),
  0 weeks over limits, min week 94 t. Remaining early-2028 bump = whole-tank 6N dumps freeing entry tanks for B60/B62 arrivals
  (W08 103 t, W09-W10 ~140 t); pre-arrival cap relief (P1-P3) unsettled 2027, not taken. Live rerun = candidate (plan sheets
  identical). Saved set `Scenario A v3 levelled - ...`; backup `..\_backups\2026-11-03_ScenarioBv2_before_scenario_A_v3_levelled\`;
  tag `v2-before-scenario-A-v3-levelled-2026-11-03`. Next: operator runs it in the app on the 10.31 PR, we review the
  workbook, THEN Scenario B gets the same 28.5 t/d + levelling (B v2 saved set is the start point).
  Operator ran it in the app: workbook = verified run (all plan sheets identical); reviewed and closed.
- **Scenario B v3 LIVE (2026-11-03, latest; replaces A v3 as live):** on A v3's base limits (AH: 28.5 t/d 2027), B48+ Base x0.925,
  FCR 1.15 all, 290k to B53, 310k from B54, no hold, no levelling caps. 2027 6,844 t (3.98 kg, Apr-Dec 131-150 t/wk), 2028 7,136 t
  (4.15 kg, 31% 8 lb+), 0 weeks over limits, weeks 95-166 t. Compared (scratch scenB3/): 290k all (+/-hold, levelled: 6,788/6,883 t
  at 4.00/4.29 kg = closest A counterpart), 310k from B53 (2027 over feed 4-19 wks), 310k from B51 (no gain). Findings: any 2028
  hold or month-levelling cap pushes B over the 2028 limits (B rides 4,200 t / 32 t/d, harvest must follow growth); 2027-only
  levelling saved little for 310k (131-150 -> 128-146, -56 t 2028). Live rerun = candidate. Saved set `Scenario B v3 - ...`;
  backup `..\_backups\2026-11-03_ScenarioAv3_before_scenario_B_v3\`; tag `v2-before-scenario-B-v3-2026-11-03`.
  Next: operator runs B v3 in the app on the 10.31 PR and sends the workbook; push.
  Operator ran it: workbook = verified run; reviewed (2027 density overs 55 tank-weeks vs A's 6; 1 handling overrun 2028-W15).
- **Scenario C LIVE (2026-11-03, latest; replaces B v3 as live):** B48+ Base x0.95 + new FCR curve '1.12' (biology.yaml; 1.15 minus
  the 1.15->1.18 step, floored 1.0, non-decreasing), 290k B50-53, 330k B54+. Operator reframed feed limits as whole-facility:
  30 t/d 2027 (rows), 34 t/d 2028+ (Control); standing 4,000/4,200 t; weekly fish cap 52k 2028+ (plant OK); HOG yield 0.83 rows
  2028 (NB yield feeds the 105 t HOG floor -> plan moves, not report-only). 2027-only month levelling (scenC2/LB it3).
  2027 7,031 t (4.07 kg), 2028 7,903 t (4.17 kg), 0 weeks over limits. Findings: at 32 t/d feed binds (~4,250-4,300 t standing),
  so 4,400 t cap/345k gave nothing; at 34 t/d the 4,200 t cap + 44k fish/wk bind (330k ran 31 wks over until 52k); 2028 month
  levelling breaks the 4,200 t cap. Saved set `Scenario C - ...`; backup `..\_backups\2026-11-03_ScenarioBv3_before_scenario_C\`;
  tag `v2-before-scenario-C-2026-11-03`. Next: operator runs C in the app and sends the workbook; push.
  Operator ran it: workbook = verified run; reviewed (density overs 88 tank-weeks 2027 / 173 2028, 5 handling overruns).
- **Scenario C "2027 levelled v2" LIVE (2026-11-03, latest):** operator updated the 10.31 PR (Reports\2026\Oct, 15:41; +20 t biomass,
  -386 fish, OG fish 0.5-1% heavier, B51 FW 157 g vs 145 g, no tank changes). Live C on it: 7,075/7,918 t but 1 week +0.1% feed and
  3 weeks < 100 t -> re-ran the 2027-only levelling on the new PR (scenC3/NA it2): 35 max_harvest_per_week rows 2027-W18..W52 changed.
  2027 7,011 t, 2028 7,902 t (harvest-week basis; harvest_by_period books 2027 as 7,032), 0 weeks over, months 137-151 / 132-167,
  5 weeks 94-99 t. Saved set `Scenario C - ... 2027 levelled v2 ...`; backup `..\_backups\2026-11-03_ScenarioC_before_relevel_new_pr\`;
  tag `v2-before-scenario-C-relevel-2026-11-03`. Next: operator runs C on the UPDATED 10.31 PR in the app; push.
  Operator ran it: workbook = verified run (Models "after 2026-10-18" check rows differ: app stored the updated PR in history).
- **Scenario B v3b LIVE (interim, 2026-11-03):** B v3 on the updated 10.31 PR (6,825/7,175 t, 1 wk +0.07% feed) + 2027-only
  levelling (scenB4/LV it1): 6,839/7,115 t, 0 over. Live biology.yaml keeps the 1.12 curve (B unaffected - checked). Backup of
  live C v2: `..\_backups\2026-11-03_ScenarioCv2_before_scenario_B_v3b\`; tag `v2-before-scenario-B-v3b-2026-11-03`.
- **Scenario B4 LIVE (2026-11-03, latest; replaces B v3b):** B on C's framework per operator (feed 30/34 t/d, 4.0/4.2 kt, 52k
  fish 2028, HOG 0.83 2028), updated 10.31 PR. B48+ Base x0.925, FCR 1.15, 290k B50-53, 320k B54+ (operator pick; 330k 2 wks over,
  310k -170 t). 2027-only levelling (scenB5/LB it3, gain 0.4). 2027 6,804 t (4.03 kg, months 121-146), 2028 7,665 t (4.19 kg),
  0 weeks over, no week < 100 t. Saved set `Scenario B4 - ...`; backup `..\_backups\2026-11-03_ScenarioBv3b_before_scenario_B4\`;
  tag `v2-before-scenario-B4-2026-11-03`. Next: operator runs B4 in the app on the updated PR; push.
  Operator ran it: workbook = verified run, reviewed (2027-W10 218 t / 70.7k fish whole-tank drain before B53 arrival).
- **Scenario A4 LIVE (2026-11-03, latest; replaces B4):** A on the common framework (same limits as B4 / C levelled v2), updated
  10.31 PR. B48+ Base x0.90, FCR 1.20 all, 290k B50-53, 320k B54+ (operator pick). 2027-only levelling (scenA5/LA it7).
  2027 6,675 t (3.95 kg, months 110-143), 2028 7,456 t (4.09 kg, 28% 8 lb+), 0 weeks over, 2028 weeks >= 101 t.
  Comparable set on the updated PR (2027 / 2028 HOG): A4 6,675 / 7,456; B4 6,804 / 7,665; C lev v2 7,011 / 7,902.
  Saved set `Scenario A4 - ...`; backup `..\_backups\2026-11-03_ScenarioB4_before_scenario_A4\`; tag `v2-before-scenario-A4-2026-11-03`.
  Next: operator runs A4 in the app; push.
- **Scenario Av5 LIVE (2026-11-04, latest; replaces A4):** reviewer comments on the A4/B4/C set -> operator rule: every
  month-to-month step < 100 t HOG (gradual for sales contracts, not levelled), keep A4's totals; also prefer monthly avg
  <= 3.4 kg HOG (harvest big fish earlier). A4 had 4 steps >= 100 t (Mar-27 +237 = B53-arrival make-room 6N drains, Apr-27 -175,
  Jun-28 +135, Jul-28 -133). Av5 = A4 + 68 limits.yaml rows only (Jan-Feb 2027 HOG floors 119/125 t + 48k caps W01-W08,
  March caps 40k W09-W13, smoothing floors/caps 2027 May-Jun/Nov and 2028 Feb, Apr-Jul; list in the set's
  limits_changes_vs_A4.txt), from an anchored step-limit search (scratch q1lev/L5_A_s2 it3; 8 more anchored searches found
  nothing closer). 2027 6,583 t (-92), 2028 7,453 t (-3), largest step 86 t, heaviest month 3.46 kg HOG (A4 3.55), 0 weeks
  over limits; costs: 2028-W20/W34 at 90 t, moves 1,171 vs 1,067, density warnings 42/158 vs 35/145. Months 2027:
  535 504 512 497 562 586 526 580 520 583 575 625; 2028: 565 543 542 598 632 674 638 679 620 604 690 666.
  Live rerun = candidate (all plan/report sheets identical except the name column). Short name per operator (long names made
  workbooks unopenable). Saved set `Scenario Av5\`; backup `..\_backups\2026-11-04_ScenarioA4_before_scenario_Av5\`;
  tag `v2-before-scenario-Av5-2026-11-04`. Next: operator runs Av5 in the app on the updated 10.31 PR; push.
  Operator ran it (Downloads\10.31.26_PR_planned_Scenario Av5.xlsm): workbook = verified run (all data sheets identical;
  RunConfig / RunConfig YAML differ only in order/comments - parsed values identical).
- **Scenario Bv5 LIVE (2026-11-04, latest; replaces Av5 as live):** B4 under the same rule (monthly steps < 100 t). B4 had 6 steps
  >= 100 t (Mar-27 +208, Apr-27 -102, Mar-28 +206, Apr-28 -163, May-28 +182, Jun-28 -114). Bv5 = B4 + 63 limits.yaml rows
  (Jan-Feb 2027 HOG floors 121/128 t + 48k caps W01-W08, March 40k W09-W13, 2027-W36..W38 caps 44k, 2028 Jan-Feb/Apr-Jun/Oct
  floors/caps, 2029-W02..W03 130 t floor to clear 2 weeks 0.1-0.4% over 4,200 t in 2029). Found by scratch q1lev LB_s4 it12
  (level4 from the B Q1 fix, gain 0.4, 80 t) + R2/s4_h130; 13 searches / 126 iterations, the only other all-rules plan lost
  267 t in 2028. 2027 6,683 t (-120), 2028 7,614 t (-52), largest step 97 t, heaviest month 3.51 kg HOG (B4 3.63; months
  > 3.4 kg 4 vs 9), 0 weeks over (2029 incl.), 2027-W34/W41 96/94 t, moves 1,097 vs 1,163, density warnings 53/163 vs 61/171.
  Months 2027: 533 509 583 524 567 530 518 544 586 558 609 643; 2028: 608 587 589 636 714 621 656 583 680 630 703 608.
  Live rerun = candidate (plan/report sheets identical except name). Saved set `Scenario Bv5\`; backup
  `..\_backups\2026-11-04_ScenarioAv5_before_scenario_Bv5\`; tag `v2-before-scenario-Bv5-2026-11-04`.
  Operator ran it (Downloads\10.31.26_PR_planned_Scenario Bv5.xlsm): workbook = verified run (all data sheets identical,
  RunConfig values + parsed YAML identical, ledger OK).
  Next: push. Then Scenario C under the same rule (Q1 fix ready:
  Jan-Feb HOG floors 115/121 t + 48k caps W01-W08 + March 40k; base set scratch q1lev/base_C).
  Pushed by operator (fd782b7).
- **Scenario Cv5 LIVE (2026-11-04, latest; replaces Bv5 as live):** C levelled v2 under the step rule. C had 3 steps >= 100 t
  (Mar-27 +151, May-28 +106, Oct-28 +122). Cv5 = C + 79 limits.yaml rows (Q1 fix: Jan-Feb HOG floors 115/121 t + 48k caps,
  March 40k; step-search rows 2027 Aug-Sep/Dec, 2028 Jan/Apr-May/Aug/Oct-Nov; 2028 cap rows held <= 52,000; 2029-W06 150 t
  floor to clear 2029-W07 +16 t over 4,200 t). From scratch q1lev LC_s4 it7 -> R3/C_cl_h6. 2027 6,946 t (-65), 2028 7,820 t
  (-83), largest step 94 t, 0 weeks over (2029 incl.), weeks >= 95 t, moves 1,116 vs 1,201, density warnings 55/172 vs 86/195,
  0 handling overruns (C 4). Months 2027: 519 488 551 583 556 593 585 663 619 552 633 625; 2028: 663 580 565 639 680 635 624
  718 631 701 699 685. Live rerun = candidate. Saved set `Scenario Cv5\`; backup `..\_backups\2026-11-04_ScenarioBv5_before_scenario_Cv5\`;
  tag `v2-before-scenario-Cv5-2026-11-04`.
  Operator ran it (Downloads\10.31.26_PR_planned_Scenario Cv5.xlsm): workbook = verified run (all data sheets identical,
  RunConfig values + parsed YAML identical, ledger OK). All three step-rule workbooks (Av5/Bv5/Cv5) now verified.
  Comparison PDF for the reviewer: `Forecasts\2026\Nov\10.31.26 PR - Scenario comparison Av5 Bv5 Cv5.pdf` (5 pages; growth/FCR
  model table + plain explanation of the measured "Actual 2026-10-18" curve for B37-B47 vs Base curve x 90/92.5/95% for B48+,
  monthly HOG + monthly kg HOG charts with the 3.4 kg guide, month tables, feed/biomass charts; corrects the 3 Nov PDF's 2028
  live weights). All figures on the workbook YearlySummary basis (harvest weeks split over Mon-Fri days): 2027 6,605 / 6,705 /
  6,967 t, 2028 7,453 / 7,614 / 7,820 t. Built by scratch cmp5/data.py + mkcharts.py + mkpdf.py from the verified app workbooks.
  52k finding: the step searches' limits controller raised 2028 fish-cap rows above 52k (Av5 to 53,161, Bv5 to 58,890) and
  whole-tank 6N drains exceed the cap anyway (A4/B4 originals too: B4 2027-W10 70.7k; "Harvest ceiling" warnings). Operator
  ruling 2026-11-04: OK to go over 52k fish/week -> Av5 and Bv5 stand unchanged (clamped variants scratch R3/A_clamp,
  R3/B_clamp not used). Comparable set (2027 / 2028 HOG): Av5 6,583 / 7,453; Bv5 6,683 / 7,614; Cv5 6,946 / 7,820.
  Open: B and C under the step rule (Q1-only fix found: Jan-Feb HOG floors B 121/128, C 115/121 + caps 48k W01-W08 + March 40k);
  the 3.4 kg monthly rule as its own step (tested: costs ~140 t 2027 and rough weeks); rename the long B/C workbooks and
  long saved-set folders (asked, not answered). NB the comparison PDF's 2028 "avg harvest weight (live)" row used 0.81 yield:
  true 2028 live weights A4/B4/C = 3.99 / 4.08 / 4.08 kg (HOG tonnes in the PDF are right).
- **Scenario Av6 LIVE (2026-11-05, latest; replaces Cv5 as live):** reviewer table: growth/FCR by harvest year. A: B37-B47
  Actual 100% FCR 1.20 (same); B48-B52 Base x0.90 FCR 1.18; B53+ Base x0.925 FCR 1.16 (table listed B52 in both rows - put
  in the 2027 group, all of it harvested 2027). Same constraints as Av5; harvest rows re-searched (scratch q1lev A6/s1 it6,
  level4 from A4 + Q1 fix, gain 0.6, 85 t; 9 searches). YearlySummary: 2027 6,727 t (+122 vs Av5), 2028 7,423 t (-30), feed
  -365 t over 2027-28, largest step 79 t, 0 weeks over, months > 3.4 kg 3 (max 3.48), 2027-W34 93 t, 4 weeks > 52k (max 54.3k),
  moves 1,106. Without the re-search 2028 = 7,717 t but 15 weeks over and steps to 191 t. Live rerun = candidate (all plan/report
  sheets identical except the name column). Saved set `Scenario Av6\` (README, limits_changes_vs_Av5.txt); backup
  `..\_backups\2026-11-05_ScenarioCv5_before_scenario_Av6\`; tag `v2-before-scenario-Av6-2026-11-05`.
  Operator ran it (`Forecasts\2026\Nov\10.31.26_PR_planned_Scenario Av6.xlsm`): workbook = verified run (all data sheets
  identical; only "Generated" stamps differ; RunConfig YAML identical parsed). Next: Bv6 and Cv6 (B: 1.20 / 92.5% 1.16 / 95% 1.14; C: 1.20 /
  95% 1.14 / 97.5% 1.12) - they need a new FCR curve '1.14' in biology.yaml (show the operator first).
- **Scenario Bv6 LIVE (2026-11-05, latest; replaces Av6 as live):** reviewer table on B: B37-B47 Actual FCR 1.20 (was 1.15),
  B48-B52 Base x0.925 FCR 1.16, B53+ Base x0.95 FCR 1.14. NEW FCR curve '1.14' in biology.yaml (operator: accumulated FCR
  at 4 kg = 1.14 = feed per kg gained 0.1 g -> 4 kg; blend of 1.12/1.16, weight 0.288; existing curves sit on that basis;
  adding it leaves Av6 identical; engine feeds ~0.7% above every curve - closing-biomass pricing, not changed). Harvest
  rows re-searched like Av6 (q1lev B6/s3 it4, level4 from B4 + Q1 fix, gain 0.5, 85 t, weight check off); operator picked
  this "most tonnes" plan over a lighter fish-floor variant as the like-for-like comparison with Av6. YearlySummary:
  2027 6,807 t (+102 vs Bv5), 2028 7,740 t (+126), largest step 95 t, months > 3.4 kg 8 (max 3.54), 1 week > 52k,
  end-2028 standing 4,094 t, Dec 2026 948 t (-2). 2026-W49 feed 27,550 vs 27,500 kg/d (+0.2%) ACCEPTED by operator (manual
  Nov moves; B37-B47 FCR 1.20). Live rerun = candidate (only the name differs). Saved set `Scenario Bv6\` (README,
  limits_changes_vs_Bv5.txt, curve builder); backup `..\_backups\2026-11-05_ScenarioAv6_before_scenario_Bv6\`; tag
  `v2-before-scenario-Bv6-2026-11-05`. Next: operator runs Bv6 in the app; then Cv6 the same way (curves 1.14/1.12 exist).
- Study 2026-11-05 (scratch, nothing live): Cv5 is within 2.1% of the HOG ceiling; no planner knob robustly beats it;
  3.4 kg guide ~400 t per 0.1 kg; least-moves on Cv5 tpf 2.15 -> 1.59. Three offers open (memory ceiling_and_template_study).

## Superseded 2026-11-02: "10.29 PR sheet moves, graded 6N, 105-110t to W07 then limits"

- Replaces the earlier 10.29 set the same day. Events from the operator's movement sheet: 43 -> 65 36,000 (3k off 65
  Thu W45 = W46 fish pulled forward, no harvest Monday W46), 52 -> 61 30,000, graded 36 (top 32k -> 63) and 32 (top
  38k -> 65) with B-grade -> 52, rest of 52 (B44) -> sister 69, B48 OG moves 12->53, 25->43, 15->33. 2027-W02 cap 36,000.
- 2026 1,586 / 2027 6,394 / 2028 2,800 t; W46 99 t (by design), 2027-W02 100 t; W31 drain 71,880 fish; <4 lb W48/W49 1.1%.
- Saved set `..\_backups\scenario_sets\10.29 PR sheet moves, graded 6N, 105-110t to W07 then limits\` (README);
  backup of the earlier 10.29 live set `..\_backups\2026-10-30_before_10.29_pr_sheet_moves\`. Re-run from live = candidate.

## Superseded 2026-10-30: "10.29 PR taper 105-110t to W07 then limits" on the 10.29 PR

- Rebuild of the 10.18 "taper ... Dec +5.8k" plan on `Reports6\Oct.29.26 PR.xlsx`, with the operator's moves as
  manual events (`scenario/manual_events/2026-10-29.yaml`): W45 harvest 61 (fish moved into 63) + 63, 43 -> 65 38.8k,
  3k off 65 Thu, rest of 43 + 31.8k of 52 -> 61; W46 rest of 65; W47 61. Scripted fills (planner can't fill 6N inside
  the manual window): W46 rest of 52 -> 63 + 34.8k of 36 -> 69 (sister); W47 32 -> 65.
- Limits: 100 t HOG floor on 2026-W48/W51, 2027-W01/W04 (100-105 t), others 105-110 t to 2027-W07 via resized fish
  caps; 3,200 g min weight 2027-W01/W02; 2027-W10 cap 44,000. Batches = 10-22 file (Actual growth, 290k). Horizon 85.
- Result: 2026 1,598 / 2027 6,398 / 2028 2,916 t HOG; peak 3,714 t; feed overs 3, bio 0. Only miss in the band:
  2027-W04 99.5 t. W49 whole-tank fill is 42.0k vs its 39.0k row (ValidationLog). Dec +5.8k dropped.
- Saved set `..\_backups\scenario_sets\10.29 PR taper 105-110t to W07 then limits\`; previous live set backup
  `..\_backups\2026-10-30_before_10.29_pr_set\` (= saved set "10.18 PR build to 4200t, B48 Actual, ...").
  Scratch re-run from the live files = candidate (plan sheets identical).
- Open: operator never ruled on graded harvest into 6N for W48-W52 (whole tanks used; <4 lb 0.9-2.1%).

## 2026-10-29: least-moves as a pipeline step merged (3d52575) - OFF by default, plan numbers unchanged

- `placement_method: least_moves` (forecast/least_moves_stage.py, run.py Stage 2.6): after the normal placement,
  re-lays OG1-5/6S with the least-moves solver on the run's own endpoint; gates = rule check no worse per category,
  fewer transfers, per-tank biology reconciles per batch-week, else the normal plan stands (ValidationLog line).
  Weeks with harvest from grow-out / 6N back to grow-out cut the span (pinned end, entry tanks as planned).
  Method `controller-least-moves` registered but OFF the Decide/Compare roster (slow). USER_GUIDE 16.1.
- Live 10.18 set, 60 s/step (47 min): tpf 2.26 -> 1.80, move rows 844 -> 426, density over cap 223 -> 26 (21 in manual
  weeks), sys feed overs 20 -> 10, sys bio 3 -> 0; harvest/HOG/6N identical; feed +0.04%. Fixture: 2.33 -> 1.92.
- Solver fixes (also in tools.least_moves / tank rota): 1 kg rule at week-open weight; 6N staging piece floor at the
  staging factor; per-week system caps.
- Gate 24/24 plan-identical with it off; suite 2542 passed. Tag `v2-before-least-moves-stage-2026-10-29`; backup
  `..\_backups\2026-10-29_before_least_moves_stage\`. Live control stays `placement_method: greedy`.
- Next: step 3 - faster variants judged on the corpus (`python -m tools.knob_corpus --method controller-least-moves`).

## 2026-10-28 late: Tank layouts app mode + tank rota merged (57a6478) - plan numbers unchanged

- New app mode **Tank layouts (fewer moves)** (`tank_layouts_page.py`, 7th mode, appended last):
  - *Tank rota*: upload a planned workbook, pick a batch's life + fish per batch, steps table (entry tanks wk 0,
    whole-batch move to grow-out, +n tanks later) -> instant tank count -> full check (~25 s-5 min) -> workbook.
    Engine `forecast/tank_rota.py`, CLI `python -m tools.tank_rota --steps "0:4, 21:6, 32:+1"`.
  - *Least-moves*: starts `tools.least_moves` as a detached background job (jobs in %TEMP%/as_forecast_layout_jobs),
    lists running/done/failed with download.
- Results on the 10.18 plan (B59 life): 4->8 cannot fit (needs >= 25 of 21 grow tanks); 4->6 +1@32 at 310k clean,
  1.16 transfers/fish, busiest system 439 t (uses the x1.1 tolerance); at 330k too dense at week 31.
- Tests: tests/test_tank_rota.py (6, negative controls), tests/test_tank_layouts_page.py (2); test_decide_mode now
  expects 7 modes. Suite in sandbox: 2535 passed, 111 skipped, 1 failed = that mode-count guard (updated). Live tree:
  new + render + least-moves tests 35 passed. USER_GUIDE section 17. Tag `v2-before-tank-rota-2026-10-28`;
  backup `..\_backups\2026-10-28_before_tank_rota\`.
- Vetted after the push (2026-10-29), old 0f15a7b vs new 418d21e: gate 24/24 plan-identical, no report changes
  (corpus + Oct Scenario 1/2/V6); live set "10.18 PR build to 4200t, B48 Actual, ..." on the 10.18 PR: plan
  identical, no report changes; full suite on 418d21e 2536 passed, 111 skipped, 0 failed.
- Not done: transition from today's plan into a rota (operator: not pursuing for now).

## 2026-10-28: least-moves layout (Global v2) merged (27a2477) - plan numbers unchanged

- New tool `python -m tools.least_moves --workbook "<planned .xlsm>"` (~2.5 h): reads a finished run, re-lays
  the same fish per batch per week into tanks with the fewest moves (CP-SAT rolling windows + independent checker),
  writes `<name>_least_moves.xlsx` next to the workbook (Summary / Transfers / Layout / Batches). Changes nothing else.
  Code `forecast/least_moves.py`, tests `tests/test_least_moves.py` (6, with negative controls), USER_GUIDE section 16,
  `ortools` optional in requirements.txt. Tag `v2-before-least-moves-2026-10-28`; backup
  `..\_backups\2026-10-28_before_least_moves\`.
- Live 10-26 plan: transfers per harvested fish 2.37 -> 1.51 (2027 1.62, 2028 1.46); move events/wk 10.6 -> 5.9;
  same checker: density over cap 198 -> 0, system feed over 27 -> 0, weeks over 15 moves 11 -> 0. Companion workbook
  in `Forecasts\2026\Oct\` next to the live workbook. Fix 27a2477: 6N staged pieces now equal the plan's staging
  (two pieces were 42/53 fish short). Confirming rerun from the merged tree: 1.47, 0 staging flags, one system-week
  1.7 kg/day over the feed cap; its workbook replaced the first one in the Oct folder.
- Next (after the book is closed): fixed 4->8 tank standard schedule (parked; 310k at 95 kg/m3 looked possible).

## 2026-10-28: code merged (af9312a), best settings REVERTED - live set unchanged from 2026-10-26

- Merged (ff, rebased onto f4751ed; code identical to the gated d3bb8ef): density trim (`density_trim_budget`,
  default off) + arrival make-room merge (acts only when the entry tier is completely full). Tag
  `v2-before-arrival-room-2026-10-28`; backup `..\_backups\2026-10-28_before_arrival_room_best_settings\`.
  With the live set the plan is identical to the operator's 10-26 workbook (all plan/report sheets).
- Best settings (trim 4 + rebalance_level false + hybrid_follow full) + bands 40k/50k were made live, then REVERTED
  the same morning (operator chose revert): rebalance_level false raises SystemLimitsAudit FEED_OVER system-weeks
  18 -> 208 (bio 3 -> 7) - the sweep had not scored system feed. With rebalance on, trim+hybrid keeps the caps but
  costs 150-270 t HOG. Saved set "...best settings, hold 40k 2027 50k 2028+..." is kept for reference, NOT live.
  Live control/limits restored from the backup (== lchk2 == 10-26 set).
- Fewer transfers now come from Global v2 (least-moves layout, same endpoint): sandbox `v2/least-moves`
  (forecast/least_moves.py + tools/least_moves.py), kept simple; in progress.
- Parked: fixed 4->8 tank standard schedule (310k looks possible on bounds; scratch tmpl_compact.py).
- Operator must push: `git push origin v2/capacity-and-transition`.

## ▶ IN PROGRESS (2026-10-27): least-transfer plan research (scratch only) + density trim (sandbox)

- Operator yardstick: fewer grow-out transfers per harvested fish with the same or more HOG. Full notes in memory
  `least_moves_template_roadmap_2026_10_26.md`; harness in session 857a2183 scratchpad (sweep.py, corpus_par.py,
  patch_all.py -> exp2 throwaway engine copy, sched*.py model searches).
- Sandbox branch `v2/density-trim` (b8fd34c + bc0c4e3): knob `density_trim_budget` (off). Gate 24/25 identical,
  suite only the 6 known path/live-dir failures. NOT merged - operator: no code changes until the decision is sure.
- Best practical settings (trim 4 + rebalance_level false + controller-hybrid): live set to 2029 tpf 2.35 -> 1.81,
  HOG +19 t; corpus tpf lower on 18/18 but 2 old PRs become infeasible (TranOG arrival: no free entry tank).
- Standard schedule: sound in principle (~1.4 tpf when it holds) but cannot start from today's tanks (B55 forward in
  2027-W45 finds grow-out full -> B58 arrival unplaceable). Waiting on operator: A fix arrival entry tanks + adopt
  settings / B design schedule transition / C both.

## LIVE since 2026-10-26 evening: 4,200 t build/hold with B49+ on Base curve x0.95

- Set `..\_backups\scenario_sets\10.18 PR build to 4200t, B48 Actual, B49+ Base 95pct, 85 then 95 kgm3 from W20, hold to 2029\` (README inside).
  B37-B48 Actual 2026-10-18; B49-B81 growth_model blank (Base) + sgr_correction 0.95 (grows FASTER than Actual:
  harvest avg 4.13 kg vs 3.82-3.95). og_density 95 from 2027-W20 (week standing hits 4,000 t); build cap 36k to
  2027-W05, 40k W06-W20; hold 2027-W21..W52 41k fish / 111 t; 2028+ 48k / 125 t. Result: 4,200 t 2027-W48, standing
  4,113-4,248 t; 2027 6,943 / 2028 8,014 t HOG; 200 tank-weeks over cap (11 by >5, max 103). Live rerun == sandbox.
  Backup of the replaced live files: `..\_backups\2026-10-26_before_b49_base95\`. The set below (B55+ 330k, W13) is the previous live.

## Done 2026-10-26: per-week grow-out density + 4,200 t build/hold scenario LIVE

- Code merged (ff, 8f1afd9 + 394a34d docs): limits.yaml facility row **`og_density_kg_m3`** = that week's
  density cap for every OG1-5/6S tank (6N never touched: purge = no cap / 150 prep judge, production 120; FW
  untouched; no row = facility.yaml). Planner loop sets caps per week; run audit, trial evaluator,
  TransferTemplate + FacilityMap judge the week's cap; editable in app Limits tab; USER_GUIDE updated.
  Checks: 4 new tests (e2e fails on old engine); gate 24/25 plan-identical (2025-07-31 known); suite 2595
  passed / 7 failed = all pre-existing (long sandbox paths / live-dir guard), identical on old code.
  Tag `v2-before-og-density-2026-10-26`; backup `..\_backups\2026-10-26_before_og_density\` (code + live
  control/limits/batches/manual_events 2026-10-18).
- **Live set now:** `..\_backups\scenario_sets\10.18 PR build to 4200t, 85 then 95 kgm3 from W13, B55+ 330k, hold to 2029\`
  (README inside; first set that also carries batches.yaml). Horizon 121 wks (to 2029-W06), 4.2M biomass,
  36k feed, B55-B81 TranOG 330k, og_density 95 from 2027-W13, build cap 36k fish 2026-W49..2027-W20,
  hold 2027-W21..W52 39.5k fish / 111 t HOG, 2028+ 46k / 121 t. Result: 4,000 t 2027-W20, 4,200 t
  2027-W42, standing 4,125-4,314 t Oct-27..Feb-29 (peak Dec 2027); 2027 6,462 t, 2028 7,525 t HOG;
  2028 ~141-149 t/wk. Live rerun from merged tree == sandbox. Previous live set = "10.18 PR 53 A-grade ..." (below).
- v2/guards and v2/rhp-source must now be REBASED onto the new HEAD before their merges.
- Operator must push: `git push origin v2/capacity-and-transition`.

## ▶ PICK UP HERE (2026-10-23): merge the review guards (A+B+C) - paused for a PC restart

Built, tested, gated, NOT merged. Branch **`v2/guards`** (3 commits on 5d770eb: c313014, b2b5fe4,
85ca2c9). Worktree was `wt_guards` in session 327f3cff's scratchpad (if gone: `git worktree prune`;
the branch is safe in git). Extra copy: `..\_backups\2026-10-23_guards_branch_paused\` (bundle +
patches + last suite log). Plan sheets unchanged; adds warnings/refusals/app display only.
- A (warnings): Control `min_week_avg_harvest_wt_g` (report-only weekly avg-weight check, off by
  default); ValidationLog "LIMITS ROW" notes (bad week labels, duplicates, cap <= 0, tonnes/kg typos);
  HOG FLOOR lines filed under "HOG floor" (were "Hydration"); coverage notes for weight/HOG rows;
  manual-move notes for <2-week purge and over-cap density.
- B (refusals / fixes): og_transfer into 6N refused; fw_to_og only into entry tanks (also empty);
  graded_harvest retention not in 6N / must pass move rules; in-week pickup must be empty;
  B11 purge-hold not restored on tanks refilled in the window; B12 6N release week index.
- C (app): results-page error/warning box (severe density only), ERROR first in Advisory,
  PR month-to-date in monthly chart, "Total harvest (gross)" labels, graded-harvest dest filter.
- Checks: 20 new tests (17 fail on old code); full suite 2521 passed / 1 failed -> that test's
  fixture fixed in 85ca2c9 (51 targeted tests pass). A final full rerun on 85ca2c9 was STARTED and
  STOPPED for the restart - rerun it first (~26 min). Gate 22/24 plan-identical; 2025-07-31 fails
  both arms (known); 2026-07-31 differs only by B11 (same fish, 2026 3,070 -> 3,062 t). Live 10.18
  plan identical on all 26 sheets; banner shows 2027-W13 below fish + HOG floors.
- Shown to the operator; waiting for their **yes** (+ optional "set 3200" = `min_week_avg_harvest_wt_g:
  3200` in live control.yaml). Then: app closed -> `git tag v2-before-guards-2026-10-23 HEAD` ->
  back up app.py, forecast/{run,excel_io,caps,scenario_io,manual_events,manual_window,manual_preview,
  models}.py, tests/test_units.py to `..\_backups\2026-10-23_before_guards\` -> `git rebase` v2/guards onto HEAD (main moved by STATE commits only) -> `git merge --ff-only
  v2/guards` -> re-run live in scratch -> USER_GUIDE (new knob, ValidationLog categories, banner,
  refusals) + STATE + memory -> operator pushes.
- Deferred: Group D (engine number changes) from the same review, one at a time with before/after.

## ▶ ALSO WAITING (2026-10-22): merge the RollingHarvestPlan Source Tank change

Built, tested, gated, NOT merged. Branch **`v2/rhp-source`** (one commit on top of the STATE
commit; worktree was in session c33340bb's scratchpad `wt_rhp_source` - if that folder is gone,
`git worktree prune`; the branch itself is safe in git). Report only - plan sheets identical.
- What: Source Tank on the week's first day row now (a) marks sorted moves `66(13% graded)`
  (graded-harvest pickups and sorter grades; density splits are not graded), (b) fills 6N tanks
  filled BEFORE the forecast from the PR's per-unit "Transfer in/out count in period" (exact
  count pairs first, the rest pro rata; fish already in the tank on the 1st have no source),
  (c) weights shares by fish harvested that week. App Harvest tab reads the same (app == sheet, 85 weeks).
- Live 10.18 before/after (only these cells change): W43 blank -> `34(61%)/41(20%)/33(19%)`;
  W44 blank -> `41(52%)/33(48%)`; W45 `43` -> `43(57%)/41(22%)/33(21%)`; W49 `66(13% graded)`;
  2028-W10 `51(2% graded)`; 2028-W18 `33(20% graded)`.
  Saved in `..\_backups\2026-10-22_rhp_source_review\before_after.txt` (+ rhp_cmp.py, run_wt.py).
- Checks: 6 new tests (all fail on old code), full suite 2503 passed / 111 skipped; gate 23/24
  plan-identical, no report changes; USER_GUIDE updated.
- Shown to the operator; waiting for their **yes**. Then: app closed (`netstat -ano | findstr :8501`)
  -> `git tag v2-before-rhp-source-2026-10-22 HEAD` -> back up forecast/production_report.py,
  rolling_harvest.py, excel_io.py, run.py, tests/test_rolling_harvest.py, docs/USER_GUIDE.md to
  `..\_backups\2026-10-22_before_rhp_source\` -> `git merge --ff-only v2/rhp-source` (never touch
  the uncommitted live config) -> re-run live 10.18 in scratch, compare with rhp_cmp.py -> STATE +
  memory -> operator pushes.

## Code merged 2026-10-22 night
- 1b19cbe: workbook download named `<PR stem>_planned_<run name>.xlsm` (blank run name = old name; illegal
  file-name chars -> '-'). Download name only; plan identical (live Dec +5.8k run via the app function == sandbox
  on all plan/report sheets). Suite 2502 passed / 111 skipped. Tag `v2-before-runname-file-2026-10-22`, backup
  `_backups\2026-10-22_before_runname_file\app.py`.

## Live setup (what the app runs right now)

- **Since 2026-10-22 late night (newest):** set `..\_backups\scenario_sets\10.18 PR 53 A-grade W43 ramp 274-242-240-226 min 3.2kg Dec +5.8k\`
  (run name the same) = the Dec +5.8k set below + operator's change of plan: tank 53 graded THIS week (W43), top
  36,000 -> 6N 63, harvested W45 with 61's leftover (6,719). Manual events: W43 43->63 dropped; graded_harvest 53->63
  36,000 (stage) added; tanks 43/53 swap later roles: W44 53 rest (~1.2k) + ALL of 52 -> 65; W45 52 rest->67 dropped,
  43->67 36,000; W46 43 rest (~6.2k) -> 63. Limits: 2026-W49 min_harvest_weight_g 3000 -> 3200 (operator rule: no
  week < 3.2 kg avg gross; W51 was 3.16 kg since the Dec rows). Weeks W43-W48: 314/300/274/242/240/226k lb HOG.
  2026 559,029 fish 1,620 t; Oct/Nov/Dec 643/467/511; 2027 6,420 (-24) / 2028 2,783 (+47) t; avg wt 3.745 kg, no week
  < 3.2 kg; 2027-W13 73 t (cost of the 3.2 kg row, accepted). All 19 manual events OK, 0 refused. Live rerun
  (`tune\live_53`) == approved W1 on all 26 sheets. Backup: `_backups\2026-10-22_before_53_grade\`.
  Rejected: raising W48 via 36->69 / 66 pickup (moves 20-80 t out of 2027, 2027-W22 ~80 t).
- **Earlier 2026-10-22 night (superseded by the line above):** set `..\_backups\scenario_sets\10.18 PR taper W44-45 105-110t to W07 then limits Dec +5.8k\`
  (run name the same; no commas so it doubles as the workbook name) = P7 below + max_harvest_per_week 2026-W50..W53 +2,500 each (42,334 / 43,438 / 41,123 / 41,260).
  Operator asked for ~10k more fish in 2026 (5k Nov + 5k Dec). Measured: Nov cannot take net fish (a bigger 6N fill
  only shifts fish to/from the next Nov week; the chain has no spare); Dec rows deliver +5,784 fish. 2026 1,620 /
  2027 6,444 / 2028 2,736 t; W52 114 t, W53 118 t (over the 110 band); floor misses 2027-W06 104 t, 2028-W02 102 t.
  Full +10k needs the W47 graded pickup 24k->34k (W49 292k lb, 2027-W22 71 t) - not chosen. Live run in scratch
  (`tune\live_dec`) == approved candidate F10B on all 26 sheets. Backup of P7 live: `_backups\2026-10-22_before_dec_plus5k\`.
  Rollback tag NOT made (blocked by permission check) - operator may run `git tag v2-before-dec-plus5k-2026-10-22 HEAD`.
- **Earlier 2026-10-22 late (superseded by the line above):** set `..\_backups\scenario_sets\10.18 PR taper W44-45, 105-110t to W07, then limits\`
  (run name the same) = the M2 set below with max_harvest_per_week 2027-W08..W13 back to the operator ramp
  (40,000 / 40,000 / 42,000 x4): 105-110 t until standing reaches the 3,800 t limit (2027-W07), then the pipeline
  holds the limit (operator rule). Standing peak 3,819 t (W09; 2 weeks > 3,800, inside the +-1% band) vs M2 3,861 t /
  9 weeks. 2026 1,608 / 2027 6,452 / 2028 2,789 t; W08-W09 117-119 t, March 128-139 t; floor 77/79; ceilings 3.
  Measure note: WeeklyReport close biomass (all fish) != planner check (projected post-harvest, +-1%).
  Backup of M2 live: `_backups\2026-10-22_before_p7\`.
  2027 levelling studied and REJECTED by the operator ("live is better and smoother"): per-week max-fish + min-HOG
  rows W08-W52 (scratch R129-1, session 327f3cff `tune\`, level_solve.py). Sustainable level at the limit ~129-130 t/wk
  (124/127 t broke the 3,800 t cap). R129-1: 2027 sd 15.6 -> 6.6 t, 6,580 t, but 2028 2,789 -> 2,609 t, 2028-W10 63 t,
  density 70 -> 96, small moves 44 -> 80. P7 stays live.
- **Earlier 2026-10-22 (superseded by the line above):** set
  `..\_backups\scenario_sets\10.18 PR taper W44-45, 105-110t to Feb, Mar 120t\` = the 10-21 set +
  (1) taper: manual events 2026-10-18 changed - W43 43->63 36,500 (was 21,000), W44 harvest 61
  45,000 (36,000), W44 52->65 30,500 (15,000), W45 53->67 29,500 (14,000), W46 36->69 32,000 (13,500),
  W47 graded pickup 66->65 24,000 (5,000); limits W44 max fish 45,500 (42,500);
  (2) Q1 band: max_harvest_per_week rows 2026-W51..2027-W13 lowered to 35,900-40,900 (W02 38,800 =
  300 over the Q1 ramp, operator OK). Result: W43/44/45 = 314k/300k/275k lb HOG, W46..2027-W09 all
  105.1-109.9 t, March 2027 120-122 t, Apr-May 2027 137-165 t/wk; 2026 1,608 / 2027 6,471 / 2028 2,823 t;
  HOG floor 76/79; ceilings 4 (2026-W49 +1,021, 2027-W26, W38, 2028-W14); facility biomass never over.
  **105-110 t to end of March does NOT fit**: every version broke the 3,800 t facility biomass in
  2027-W12..W15 (peak up to 3,932 t) - operator chose band to end Feb, March ~120 t.
  Backups: `_backups\2026-10-22_before_taper_W44_W45\` (10-21 live), `_backups\2026-10-22_before_q1_band\`
  (taper only = set `10.18 PR taper W44 300k W45 275k, 105t floor`). Scratch: session 327f3cff `tune\`
  (runcand.py, band_solve.py, measure.py). Operator to run it in the app for the workbook.
  Run name = set name (control.yaml, live + saved set). Suggested workbook name:
  `10.18.26_PR_planned_taper W44-45 105-110t to Feb Mar 120t.xlsm` (app names it <PR>_planned; rename on save).

- **Since 2026-10-21 (newest; supersedes the description below):** set
  `..\_backups\scenario_sets\10.18 PR all 10-18 growth, 105t floor, Q4 1575\` - all batches on
  Actual 2026-10-18, 105 t HOG floor (control + limits rows 2026-W46..2027-W05), W51-53 max fish
  +7% (41,999 / 42,142 / 38,760), manual W46 36->69 13,500, W48 32->61 39,000. Q4 2026 1,575 t,
  2027 6,540 t, 79/79 floor weeks. Operator ran it in the app (matched). Operator's workbook:
  `Downloads\10.18.26_PR_planned_10-18 growth 105-110t Q4 1575t.xlsm` (to move to `2026\Oct\`).
  1,600 t studied and NOT recommended (+20% W51-53: 7 ceiling breaches, ~70 t HOG-equiv worse overall).
  Code = `d57c40c` (+ this STATE commit), pushed.

- Branch `v2/capacity-and-transition`, code = `a5cdd88` (growth models steps 1-4, harvest floor
  in kg HOG, RollingHarvestPlan sales weight ranges P-W + year totals, Daily Harvest Schedule
  retired; 2026-10-19; **Models** sheet + leave batches out of the growth fit + BatchInputs growth adjustment in words + set several batches at once + SGR check table 2026-10-20). RollingHarvestPlan: whole forecast (`rolling_harvest_weeks: 0`), IDs from 978.
  Live limits now also carry the Q1 ramp (2027 max fish 38.5k W01-05 / 40k / 42k / 44k / 45k W18+,
  100 t floor W01-05); set saved as `scenario_sets\10.18 PR manual to W48 + Q1 ramp\`; operator manual
  entries to W48. 2027 = 6,454 t (week sum). Run name (`scenario_name`) set 2026-10-22 to "10.18 PR taper W44-45, 105-110t to Feb, Mar 120t".
- Latest input: the **10.18.26 PR** (`Reports\2026\Oct\10.18.26 PR.xlsx`, mid-month, closing 2026-10-18).
- Live config = scenario set **`..\_backups\scenario_sets\10.18 PR steady 100t Nov-Dec, 110t 2027, 3400g, Actual growth\`**
  ("steady", operator 2026-10-19: lower harvest now to grow size, then a 110 t HOG/wk floor).
  - Growth: every batch `growth_model: Actual 2026-10-15` (measured, `sgr_sw_by_model` in
    biology.yaml), `sgr_correction` 1.0. Walk-forward error -0.7..+1.0% (base curve ran +3.7..+4.9% hot).
  - `control.yaml`: min_harvest_weight_g 3,400, **min_harvest_hog_kg_per_week 110,000** (new knob).
  - `limits.yaml`: Nov-Dec `min_harvest_hog_kg` rows 100 t + max bands (floor +3%); fill min-weight
    rows 3,100 g W51-W53, 3,300 g 2027-W01..W08; 2027 bands 26-55k fish.
  - `manual_events/2026-10-18.yaml`: B50 fw_to_og W43; 43->63 38,000 W43, rest of 43->71 W44;
    52->65, 53->61 W45; harvests 65, 71, 61, 63.
  - Result: Nov/Dec/Jan/Feb/Mar 459/483/502/497/634 t HOG; 2027 6,340 t, 6.59 lb/fish; peak 3,683 t.
    ValidationLog: 72 of 82 floor weeks met; 2027 short W03 W12 W21-23 W27 W31 by 0.1-4.3 t and
    **W42 79 t**; 2028-W17 98 t. Sales size rules pass 25 of 60 weeks (base-curve plan 50) - growth
    is the measured one, so the 8 lb+ rule is the open question for sales.
- Previous live set (10.15 PR "stable") is still in `scenario_sets\10.15 PR 130t-wk, Dec 125t, B44 out W47\`;
  backups of the replaced live files: `..\_backups\2026-10-19_before_hog_floor\live\`.

- **Alternative saved and run**: `scenario_sets\10.15 PR aggressive Q4 (S1 rows, fish-matched)\`
  = stable set + the 10.11 S1-rebuild harvest rows from 2026-W42 (54–60k fish/wk Nov–Dec,
  min weight 2,950–3,100 g) + control min weight 3,100 g + 2027-W04/W05 floors −5,500 each.
  Workbook `Forecasts\2026\Oct\10.15.26_PR_planned_aggressive Q4 fish-matched.xlsm`
  (= scratch exactly). Q4 674k / 1,931 t / 6.32 lb; Q1 452k / 1,360 t / 6.63 lb; Dec 730 t;
  2027 6,622 t; end-Dec standing 3,273 t; W48–W53 at 56–60k fish (S1's own 60k rows).
  Oct–Mar fish 1,126k vs stable 1,118k — **8k is the closest reachable** (12 trims tried;
  whole-tank 6N drains move the fish to other weeks). Same fish, stable sells 81 t more HOG.
- `config/costs.yaml` feed prices fixed 2026-10-10 (Price/MT ÷ 1000).

## Waiting on the operator

- Yes/no on merging `v2/guards` (see PICK UP HERE) and `v2/rhp-source` (ALSO WAITING).
- Push after any commit: `! git push origin v2/capacity-and-transition` (origin at d57c40c 2026-10-21).
- Optional, offered: 10 harvests sit 3 weeks in 6N (make_room fills; planner rule change);
  586-fish graded harvest 2028-W10; W46/W47 manual weeks over the Nov fish limits by 611/1,342.

## In progress: learning growth model (agreed plan 2026-10-18)

Measured (scratch, read-only): fish under ~2.5 kg grow ~on the base curve; above 2 kg
they gain a flat ~14 g/day (curve says 15-21) -> ~0.71-0.77 of curve at 3-4 kg. 2026
grew slower than 2025 (0.84 vs 0.91 of curve). Hindcast May-Oct 2026: model B (AGR
power law, capped above 2 kg) best at 1/2/3 months (+0.6/+1.8/+2.8%). Stable 10.15 plan
under B: Jan 2027 falls to ~214 t, Q1 biomass > 3,800 t cap (needs a Dec-Jan min-weight
retune). Acc tabs: "closing better" = harvest weight (2.0 -> 3.6-4.2 kg/fish) + econ FCR
(~2 -> 1.27-1.54), NOT growth rate per size (B44-B48 0.92-1.00 vs B38-B43 1.03-1.14).
Operator decisions: rolling **24-month window** (full life cycle), history from **Nov 2024**;
full monthly reports (have Acc) supersede mid-month PRs (PR sheet only, provisional,
weighted by days); per batch BOTH fleet-only and fleet x own-history (shrunk to 1.0);
improvement BOTH % per year and blend-to-target, labelled ASSUMED everywhere; FCR learning
on the same machinery. Each step in its own sandbox + gate + approval.
1. DONE `b41ab98` engine: biology.yaml `sgr_sw_by_model` + batches.yaml `growth_model`
   (blank = Base curve, plan-identical: gate 23/24, live set identical but BatchInputs column).
2. DONE `4328b4b` forecast/growth_history.py + `python -m tools.growth_history --store <file>`
   backfill/add/show/fit (frozen versions). Scratch backfill: 38 reports, 23-report chain
   Nov 2024..2026-10-15, 370 clean tank-months; fit cap 13.9 g/d above 2 kg; holdout last 3 mo
   fit +1.0% vs base +3.1%. 22 files unreadable (2024 formats, Jan'25, Nov'25 V2, May'26 prelim).
   The live `growth_history.json` (repo root, gitignored) is NOT built yet - operator's call.
3. DONE `4fa29a0`..`8b8d5a3` (tag `v2-before-growth-app-2026-10-18`): every operational run
   adds its PR to `growth_history.json` (same rule as the calibration log; never fails a run;
   test runs with `calib_log_path=""` record nothing). Configure -> Biology models panel: scan
   Reports (from Nov 2024), add one report, Fit (preview + holdout), Save frozen version, Add a
   version to biology.yaml (biology only), before/after on a PR (copies). Batches tab:
   `growth_model` dropdown + "What each batch grows on". Gate 23/24, live set identical
   (31 sheets), suite 2454 passed / 0 failed.
   NEXT for the operator: Scan in the app (or ask for the backfill) -> Fit -> Save.
4. DONE `c55ba1a`..`a1c8924` (tag `v2-before-growth-improve-2026-10-19`): ASSUMED versions
   (+%/yr or straight-line blend to a target curve by a date, made for fish growing in a
   stated month, frozen, adoptable like a measured one) + per-batch own-history table
   (INFORMATION ONLY - operator 2026-10-19: the size slowdown is facility-wide and already
   in the measured version, so fleet only, sgr_correction 1.0, is the usual choice).
   Measured by size (Nov24-Oct26): actual/base 1.02 at 0.5-1 kg, ~0.89 at 1-2.5 kg,
   0.76 at 2.5-3, 0.70 at 3-4 kg; g/day flat 13.4-14.5 from 2-4 kg (no further decline;
   >4 kg only 1 tank-month). Gate 23/24, live set identical, suite 2466 passed.
5. FCR learning.
6. Adoption (before/after, corpus, scenario retune) - operator's call.
Scratch analysis: session c33340bb scratchpad `growth\` (measure.py, fit.py, chains.py, runs\).

## Open items (offered, not started)

- Neither 10.15 plan has 8 lb+ tanks in Q1 2027 (stable 0 t; aggressive ~100 t Feb–Mar
  only). If the ≥45k lb/wk 8 lb+ sales rule must hold in Q1: test higher min weights on
  the Dec–Feb fill weeks in scratch and price the tonnes lost.
- Run the Sep'26 full monthly production report (Acc tab → BatchAccumulated).
- Rename leftovers: `10.11.26_PR_planned_600t Nov-Dec 2-tank 6N.xlsm.xlsm`,
  `10.11.26_PR_planned_S1 rebuild opt1 2-tank..xlsm` (offered, not asked).
- 2027 weekly sales floor (~260k lb/wk, scratch `G55_260_i2` on the 10.11 PR).
- Overdue 6N drains break the weekly fish limit (55–66k, e.g. 2027-W03 56.5k stable) — engine timing.
- B55 eggs: FishTalk 550,000 vs `scenario/batches.yaml` 570,000 — which is right?
- Growth model runs hot (+3–5% weight; forecast FCR ~1.17 vs actual ~1.4): recalibrate
  SGR/FCR tables from Acc-tab lifetime actuals (plan-changing → sandbox + approval).
- Saved Scenario 1 / 2 workbooks still carry the old, inflated feed costs — re-run them.
- Align WeeklyReport/MonthlyReport SGR (compound), Econ FCR (net of culls), SFR with
  FishTalk definitions (report-only).
- Target solver as library code; a size-class report (lb by sales band per week).
- TranOG date inside the manual window: clearer app message. Manual-preview 6N week nit.

## Recent history

- 2026-10-21 `a5cdd88` **Growth checked against the data**: SGR %/day by fish size (500 g bands) -
  measured, model, 100% curve (biology.yaml `sgr_sw_pct_day`), model and measured as % of 100% -
  under the Fit preview, under each saved version (its window as the history reads now + tank-months
  after its as-of, which fills in as PRs are added) and in the Models sheet. Report only. Tag
  `v2-before-growth-check-2026-10-20`; gate 23/24, suite 2497 passed; live 10.18 plan identical.
  2026 data: measured ~100-105% of the 100% curve below 1 kg, 88% at 1.5-2 kg, ~70% at 3.5-4 kg.
  Operator ruling: the base curve numbers (Aquasim ASUS column; 4.5-5 kg rows x0.80) stay as they are.
- 2026-10-20 `5945a57` Batches tab **Set several batches at once**: From/To batch, seawater growth
  model, SW offset (sgr_correction), FW offset (fw_correction), Apply then Save Batches. Tag
  `v2-before-batch-bulk-2026-10-20`; gate 23/24, suite 2490 passed, live 10.18 plan identical;
  clicked end to end in Chrome on a copy. Operator's intended split: batches with fish (B43-B57) on
  Actual 2026-10-18 (9-month 2026 fit), future (B58+) on Base curve x 0.86 - not done live yet.
- 2026-10-20 `cc5ad8b` app click test (leave out, Fit, Save, Add to biology.yaml on a copy of the app
  tree). `e4d440b` BatchInputs column 'Growth adjustment (SW)' in words (was a bare sgr_correction 1);
  the app's 'added to biology.yaml' confirmation now survives the rerun. Tag
  `v2-before-sgr-words-2026-10-20`; gate 23/24 (no report changes), suite 2486 passed, live 10.18
  plan identical.
- 2026-10-20 `0afb391`..`ec49cc1` leave chosen batches out of the actual growth fit (app Fit picker,
  CLI `fit --exclude`, frozen version records them, Models sheet names them; CLI warns on an unknown
  batch). Tag `v2-before-growth-exclude-2026-10-20`; gate 23/24, suite 2484 passed, 15 recent PRs
  (8.13-10.18) old vs new identical but time stamps, live 10.18 plan identical.
  Operator wants the Actual model from 2026 only: Fit as of 2026-10-18, window 9 months (starts at
  the Dec-31-2025 closing; 190 tank-months, 13.7 g/d above 3 kg vs 13.9 live) - not yet saved.
- 2026-10-20 steady-state study (scratch, clean slate, Actual growth, 310k to OG every 49 d =
  44k fish/wk): 130 t HOG/wk needs ~3.7 kg harvest (min wt 3,500 g) and ~3,700 t mean standing,
  peaks ~3,900 t, density breach 60-80 tank-wk/yr; at 3,400 g it settles ~124-129 t on ~3,500 t.
  Scripts: session c33340bb scratchpad `ss130_tools\`.
- 2026-10-20 `7c82b51` Models sheet (report only): growth models per batch (measured vs assumed,
  adjustment in words, not a bare 1), curves by size, FCR tables, other assumptions. Tag
  `v2-before-models-tab-2026-10-20`; gate 23/24, suite 2481 passed, live 10.18 plan identical.
  Offered: same wording for BatchInputs "SGR correction (SW)".
- 2026-10-18 growth history step 2 merged (`4328b4b`..`43f9974`), tag
  `v2-before-growth-history-2026-10-18`; two more tests stopped copying the live costs.
- 2026-10-18 growth models step 1 merged (`b41ab98`..`edede1d`), tag
  `v2-before-growth-models-2026-10-18`; test fix: BatchAccumulated test no longer copies costs.
- 2026-10-17 10.15 PR: aggressive-Q4 set built, fish-matched, written live, operator ran it
  (verified), then live restored to the stable set (verified). Tags
  `v2-before-aggressive-q4-2026-10-17`, `v2-before-back-to-stable-2026-10-17`.
- 2026-10-16 live → 10.15 PR stable set (130t-wk, Dec 125t, B44 out W47); earlier that day
  10.11 PR 580 t/month Oct–Mar sets (saved, superseded).
- 2026-10-16 `dd21e45` 6N top-up after a make-room dump (pair filled to the band).
- 2026-10-15 `1934803`..`649f07a` RollingHarvestPlan sheet + app Harvest tab (TSV/copy).
- 2026-10-15 `d900c70`/`89e7e4a` opt-in 6N source limit `sixn_max_source_tanks`.
- 2026-10-15 10.11 PR Scenario 1 rebuild option 1 (642/723 t Nov/Dec, 3,100 g) — the
  source of the aggressive rows.
- 2026-10-14 10.11 PR 320k lb/wk 2026 + 55k facility limit + month-smoothed 2027.
- 2026-10-11 `5f7e30a` lifetime per batch (BatchAccumulated + Monthly) from the Acc tab.

Rollback tags: `git tag --list "v2-*"`. Backups: `..\_backups\`.
