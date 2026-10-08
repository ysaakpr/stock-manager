# Pre-registration: M16 strategy exploration, 2026-10-08

Written **before** any M16 arm has been implemented or run, and before the baselines have been
re-run on the campaign commit. No M16 return, XIRR, drawdown or Sharpe exists anywhere yet.
Nothing below may be changed after the first M16 evaluation run. A changed arm (a different
lookback, threshold, weight, universe rule or filter) is a **new trial**. It gets a new id here
and counts toward the trial total in §6.

## 0. Why this document exists

D13 (`PAPER_RATIFIED_2026_09_06`, momentum v2 without the vol target) is the paper book. The
M12.R re-run (`ops/gates/M12-strategy-rerun-2026-10-07.md`) and M14.5
(`ops/gates/M14.5-regime-reentry-report.md`) left it as the strongest risk-adjusted configuration
on this lake. M16 asks whether any of seven stated changes beats it. This lake has already seen 68
configurations (Appendix A), so the winner of a new sweep will look good partly by luck. This file
therefore fixes the arms, the grid, the selection rule and the luck correction in advance, so the
answer can be trusted whichever way it comes out.

## 1. Code and data state it applies to

- `main` at `523882e` (PR #92, M14.5, and PR #93, M15.1, merged) or later. The campaign runs on
  one commit that also carries M16.1–M16.3 (the A1–A5 policy code) and this task's plumbing. That
  commit is recorded in every run directory's manifest. **The baselines are re-run on that same
  commit**; no baseline figure is carried over from an earlier directory.
- Engine switches, the same as M12.R and M14.5: book corporate actions on, interest on idle cash
  on (`backtest/repo_rates.yaml`, repo − 50 bp), every order through A8's ratified rails, ₹10 lakh
  opening book, a single deposit at the start.
- Lake: last L1 session 2026-10-07 at the time of writing. The PIT NIFTY 500 membership history
  opens 2016-10-24. PIT XBRL fundamentals reach back to 2018-05.

## 2. Arms (parameters fixed now)

Every arm differs from a named reference by the stated change and nothing else. "D13" means
`PAPER_RATIFIED_2026_09_06` exactly: top 20, 12-1 ranking, sell band 30, monthly regime filter on
the NIFTY 50 TRI against its 200-session mean, inverse-vol weights, next-session redeploy, no vol
target. "M10.7" means `SwingCompositeParameters()` at its defaults.

**A1 — D13 + absolute momentum.** At each rebalance, D13 picks its basket as it does today. A
basket name's slot is held only if the name's 12-1 return is above the cash hurdle over the same
span. Otherwise that slot's weight stays in cash. The cash hurdle is the simple, uncompounded sum
of (repo rate − 0.50%) / 365 over every calendar day of the 12-1 span. That is the rate idle book
cash earns (`backtest/repo_rates.yaml`, `backtest/cash_interest.py`), read from the same schedule.
A held name that fails the hurdle at a rebalance is sold, and its slot stays in cash. The test is
made only on rebalance sessions. Failing slots are **not** passed down to the 21st name or below.
Reference: D13.

**A2 — D13 + industry gate.** On each D13 rebalance session, rank the NSE sectoral indices by
their 6-1 month return: the published price-index level 21 sessions before the decision over the
level 126 sessions before it, minus one. Only levels knowable on the decision date are used. Keep
the top five, with ties broken by index slug. A name is eligible only if its industry maps to one
of those five indices. A name whose industry maps to a non-top-5 index, or to no index, is
ineligible. It cannot be bought, and a holding that becomes ineligible is sold at that rebalance,
as if it had left the band. D13 then ranks, bands and weights the eligible names unchanged.
- The index set is the sectoral indices, not thematic, strategy or broad-market ones, whose
  published levels are in the lake. An index with no level at either end of the 6-1 span in a
  month is unranked that month. If fewer than five are ranked, all ranked indices pass.
- M16.2 freezes the index list and the industry→index map as a checked-in table before any A2
  run, and the report prints both.
- The industry label is the static current-day NSE classification. That limitation is the one
  M10.3 stated, and it is restated in the report.
- Reference: D13.

**A3 — residual momentum v2.** D13 with its ranking signal replaced by round 2's H1 residual
momentum, exactly as defined in `ops/studies/preregistration-signals-2026-09-29.md` §3 and §6:
- no-intercept regression on NIFTY 50 TRI daily log returns over the 252 sessions before t;
- residuals summed over t−252 … t−21 and divided by the residual standard deviation over the same
  span;
- at least 200 valid sessions in that span (`backtest/policies/residual_momentum.py`, unchanged).

Everything else is D13: band, regime, inverse-vol weights, redeploy. A name with no residual score
is treated exactly as D13 treats a name with no 12-1 return today. Reference: D13.

**A4 — D13 + profitability filter.** A name is eligible only if both of these hold on the decision
date:
- its trailing-twelve-month profit after tax is above zero, summed over the four latest quarters
  knowable then from the PIT XBRL store, on the first-filed values (restatements stay quarantined,
  invariant #8);
- its latest such filing is no more than 200 calendar days old.

A name with fewer than four knowable quarters is ineligible. Ineligible names are handled as in
A2. Runs **only on the six-year and verification windows** (fundamentals open 2018-05), so its
evidence is **verification-only evidence**. Reference: D13.

**A5 — M10.7 + earnings-surprise leg.** A fourth leg on the M10.7 swing composite, at equal
weight (1) with its three existing legs.
- *Surprise.* (latest quarter's EPS − EPS of the same quarter a year earlier) ÷ the sample
  standard deviation (ddof = 1) of those year-on-year differences over the last 8 quarters.
  Basic EPS, as first filed, from the PIT XBRL store.
- *Inputs.* It needs 12 knowable quarters on one basis (consolidated where filed, otherwise
  standalone; a mixed basis is undefined).
- *Active window.* The leg is active for 63 sessions from the first session after the latest
  filing's `filing_date`.
- *Undefined.* Outside that window, with fewer than 8 differences, or with a zero standard
  deviation, the name's leg value is undefined. It then takes the leg's mean rank, the H1
  convention (`composite_scores`), and is not dropped.

Runs **only on the verification window and the six-year window's tail**. On the six-year window
the leg is undefined until enough quarters exist, and the report prints the first session the leg
is active. Its evidence is verification-only. Reference: M10.7.

**A6 — static 50/50 blend of D13 and M10.7.** Half the capital in each, no rebalancing between
the halves, built **at report time** from the two baselines' saved runs:
- NAV(t) = ½ NAV_D13(t) + ½ NAV_M10.7(t), with each run's last known NAV carried across a
  session the other has and it lacks;
- XIRR from the halved cashflows of both runs;
- max drawdown from the blended path;
- charges, trades and rail refusals summed and halved where a rupee figure, summed where a count.

Both halves are full ₹10 lakh runs scaled by ½, not ₹5 lakh replays. A8's order-value floor and
holdings floor would bind a little differently on a ₹5 lakh book, and the report says so. A6 has no
engine arm and no new code path. Reference: D13 and M10.7.

**A7 — pure low volatility.** The M10.7 swing engine with `weight_volatility = −1` and every other
leg's weight 0. Cadence, band, trailing stop, re-underwrite and the default volatility screen
(`exclude_vol_fraction = 0.10`) are M10.7's. Reference: M10.7.

**Baselines, re-run on the campaign commit:** D13, Momentum v2 all on (M9.5), M10.7.

No other arms, variants, lookbacks, thresholds, weights or combinations will be run in M16. The
grid below is not mined: no cell, floor or window is added or dropped after a result is seen.

## 3. Grid (fixed)

| Universe | Windows | Floors |
|---|---|---|
| Floor-only (`turnover_floor`) | decade 2016-09-01→2026-08-31, six-year 2019-07-01→2026-08-31, walk-forward selection 2016-09-01→2021-08-31, verification 2021-09-01→2026-08-31 | ₹1 cr and ₹10 cr median daily turnover |
| PIT NIFTY 500 (`nifty500`) | every window above that its PIT membership coverage supports at campaign time | ₹1 cr and ₹10 cr |

- **The NIFTY 500 rule.** A window is run on NIFTY 500 if, and only if, the published PIT
  membership history covers that window's first session when the campaign starts. The engine
  refuses an uncovered date, and that refusal is the test. Coverage today opens 2016-10-24, so
  this means six-year and verification. If the pre-2016 membership build is published before the
  campaign starts, it means all four windows. The manifest records which applied.
- **A4 and A5** run on the six-year and verification windows only, in both universes and at both
  floors.
- **A6** is built on every cell where both D13 and M10.7 ran.

## 4. Selection rule (fixed)

**Step 1: walk-forward choice (primary).** On the selection window, floor-only universe, at the
**₹10 crore** floor, rank every arm with a selection-window row by XIRR ÷ max drawdown. Those are
A1, A2, A3, A6, A7 and the three baselines; A4 and A5 have no selection-window row. Ties go to the
smaller drawdown, then the label. The top arm is the **choice**. It is frozen, and printed, before
any verification figure is read.

- **Deviation from M12, justified.** M12's walk-forward chose at ₹1 crore first. M16 chooses at
  ₹10 crore for three reasons. It is the floor at which the fill model's slippage is defensible
  for a real book (`backtest/sweep.py`, "Two liquidity floors"). Round 2's pre-registration already
  made ₹10 crore its only deciding floor. And the question M16 answers is what paper should run on
  the way to real money, where only the ₹10 crore result is reachable.
- **Secondary.** The ₹1 crore ranking is reported beside it, labelled *informational only*. It
  decides nothing.
- **The primary floor is pending owner confirmation before the campaign launches.** Until the
  owner confirms ₹10 crore (or names ₹1 crore), M16.4 does not start. A change made then is
  recorded in §7, before any run.

**Step 2: replacement test.** The Step-1 choice replaces D13 for paper only if **all** of these
hold. If the choice is D13 itself, nothing changes.

1. Verification XIRR ÷ max DD ≥ D13's on the floor-only universe at **both** floors.
2. Verification XIRR ÷ max DD ≥ D13's on **NIFTY 500 at ₹10 crore**.
3. Verification XIRR > **25%** at ₹10 crore, floor-only.
4. Max drawdown no more than **3.0 pp** worse than D13's in **any** covered cell (universe ×
   window × floor where both ran).
5. **Deflated Sharpe** (`backtest/sharpe.py`, `deflated_sharpe_ratio`) probability **≥ 0.95**
   that the arm's true Sharpe exceeds D13's.
   - Statistic: the per-period Sharpe of the daily pre-tax NAV returns on the verification window,
     floor-only, ₹10 crore.
   - `benchmark_sharpe` is D13's Sharpe on the same cell.
   - `trials` = **N = 75** (§6).
   - `sharpe_variance` = **V**, the sample variance (`sharpe_variance`) of the same statistic over
     every one of those trials with a saved run on that cell. That covers the M16 campaign's runs
     plus the saved M12.R and M14.5 runs on the same window and floor, one Sharpe per distinct
     configuration, taking the newest commit's run if a configuration has several.
   - Trials with no saved run on that cell count toward N but cannot contribute to V. The report
     prints V's n and lists those trials.

A tie in criteria 1 and 2 passes (≥). Criterion 4 compares exact figures, not rounded ones.

**Step 3: A4 and A5.** These have only verification-window evidence, so they can never replace
D13. Each is put through criteria 1–5 of Step 2 on its verification-window cells. If it passes all
five, the most it can become is **"shadow in paper"**: run beside the paper book, without capital.

**Step 4: default.** If no arm passes, D13 stays, and the report says "no improvement found".

An arm that passes Step 2 without being the Step-1 choice is reported as passing and changes
nothing. Re-choosing after the verification window has been read is the error this rule exists to
prevent.

## 5. What the report carries (`backtest/m16_report.py`)

- **Rendered from saved runs only.** Nothing in the tables is typed by hand.
- **Tables.** One table per universe × window × floor, with D13 in every one. Each row gives
  XIRR, max DD, XIRR ÷ DD, the change against D13, excess over the NIFTY 50 TRI, trades, charges
  and the >25% bar.
- **Rail refusals.** Each arm's A8 min-holdings refusals over the run, and its total rail blocks,
  as in M14.5.
- **Scorecard against D13.** Generated, counted in code over every covered cell.
- **Decision.** The Step-1 choice at ₹10 crore, and the ₹1 crore choice as informational. Then
  Step 2's criteria, PASS/FAIL per criterion for the choice and for A4/A5. The DSR line shows N,
  V, V's n and both Sharpes.

## 6. Trial count N (fixed now)

**N = 68 + 7 = 75.**

- **68** is the number of distinct strategy configurations ever run on this lake (Appendix A).
- **7** is the M16 arms A1–A7. A6 is a configuration even though it has no engine arm.

The three baselines are already among the 68. Re-running them is not a new trial.

**What counts as "distinct".** A distinct configuration is one policy (runner) with one set of
parameters, plus its band-hit and cap-tier settings. Changes to the engine, the data, the
universe, the floor or the window do not make a new configuration.
- Examples that do not count: raw vs adjusted signal, cash interest on/off, book corporate
  actions on/off, buy-sizing before/after the order-value ceiling, floor-only vs NIFTY 500,
  ₹1 crore vs ₹10 crore, pre-tax vs after-tax rendering.
- Signal studies are not strategy runs and are not counted either. That covers the M10.7 leg
  deciles and the hold-period table.
- A smoke counts under the configuration it ran.

## 7. Clarifications

Recorded here, dated, before any M16 evaluation. A clarification may only resolve an ambiguity in
the text above from the definitions alone, and must say what had been computed when it was made.
Anything else is a new trial.

(none yet)

## 8. Amendments

An amendment changes an arm, the grid or the rule. It is allowed **only before any M16 campaign
figure exists**, meaning no M16 return, XIRR, drawdown, Sharpe or ranking computed on any window.
Each amendment is a dated entry here. It states what changed, why, and what had been computed when
it was made. The text above is **not** rewritten: where an amendment and §2–§6 or an appendix
differ, the amendment governs. An amended arm has never been run, so an amendment adds no trial,
and N stays 75. This section closes when the first M16 campaign figure exists. After that, any
change is a new trial.

### Amendment 1 (2026-10-08, before any run)

At the time of this amendment, no M16 campaign figure had been computed on any window. That covers
the arms and the baselines re-run. Only unit tests on synthetic runs and count-only checks of
saved-run identities had been run.

**(a) A2: the survivorship of the industry classification.** The classification A2 reads is a 2026
snapshot. It leaves out every name that was later delisted or merged. The review of #97 found that
all 116 dead 2016 NIFTY 500 members are unclassified, and 42.4% of the 2016 members are
unclassified overall. Under §2 as written, all of those names would be ineligible, so A2 would
mechanically hold only names that survived to 2026.

The A2 rule is therefore amended:
- A name **not in the classification is gate-neutral**: it passes the gate.
- Only a **classified** name is ineligible, and only if its industry maps to a non-top-5 index or
  to no mapped index.

The report changes with it:
- It prints the unclassified share of the eligible (floor) universe for every cell, and per
  calendar year, next to A2.
- In any cell where **more than 30%** of the floor universe is unclassified, A2 is labelled
  **diluted**. There it is informational and decides nothing: Step 1 does not rank it, and Step 2
  does not read it.
- A cell whose share was not measured counts as diluted.
- It prints each sector index's first rankable date.

The shares come from a coverage file produced at campaign time (`--a2-coverage`,
`backtest.m16_report.A2Coverage`). The report refuses to render A2 rows without it.

**(b) A2: the lookback is in calendar days.** "6-1 months" is 180 and 30 calendar days before the
decision date, the same convention as D13's 12-1. The 6-1 return is the index level at the
30-day reference date over the level at the 180-day reference date, minus one. It replaces the
126 and 21 sessions of §2. An index level is used only if both of these hold:
- it was published on or before the decision date;
- it is no more than 10 calendar days older than the reference date it stands for.

Otherwise the index is unranked that month, as §2 already provides.

**(c) A2 is a boolean switch.** A2 is momentum v2 with `industry_gate = True`. The gate's K = 5 is a
fixed constant of the gate, not a parameter. This replaces Appendix B's `industry_gate_top = 5`.

**(d) The arms resolve through the owning PRs' presets, checked exactly.** This replaces Appendix
B's table. The M16 arm set refers to A1–A5 by the parameter presets their PRs publish. Each preset
is imported only when its arm is resolved. It must equal its reference with exactly the named
switch on, and nothing else changed; otherwise the arm is refused.

| Arm | Preset (module) | Must equal | Owner |
|---|---|---|---|
| A1 | `D13_ABS_MOM` (`backtest.policies.momentum_v2`) | D13 + `absolute_momentum = True` | M16.1, #98 |
| A2 | `D13_INDUSTRY_GATE` (`backtest.policies.momentum_v2`) | D13 + `industry_gate = True` | M16.2, #97 |
| A3 | `D13_RESID_MOM` (`backtest.policies.momentum_v2`) | D13 + `residual_ranking = True` | M16.1, #98 |
| A4 | `D13_PROFIT_FILTER` (`backtest.policies.momentum_v2`) | D13 + `profitability_filter = True` | M16.1, #98 |
| A5 | `M10_7_EARNINGS_SURPRISE` (`backtest.policies.earnings_surprise`) | M10.7 + `weight_earnings_surprise = 1` | M16.3, #96 |

This was checked on 2026-10-08 against each open PR branch merged alone: #98 at `dc70712`, #97 at
`9c8392c`, #96 at `a4277b2`. All five presets resolve exactly. Until a preset exists, asking for
its arm set raises `M16ArmError`, naming the missing preset and its PR. These are names; behaviour
is §2 as amended here.

**(d′) A1 and A8's minimum-holdings rail interact.** When a name fails A1's hurdle, A1 sells it to
cash. A8 refuses a sell that would take the book below its 8-holding floor. So at a rebalance where
fewer than 8 names clear the hurdle, the failing names beyond the floor **stay held**, and A1 is
then less in cash than §2 describes. That is intended: the rails are not bypassed for any arm. The
report already shows every arm's rail-refused sells, as A8 min-holdings refusals plus all rail
blocks by rail, so the size of this effect is visible per arm.

## Appendix A — the 68 configurations already run on this lake

**How it was counted (2026-10-08).**
- **Part 1 (47).** Every run summary saved under the campaign directory (1,370 files, `runs/*.json`).
  Each was keyed by runner, parameter values (a repr made field-complete with the current
  defaults, so a field added later does not split a configuration), band-hit setting and
  cap-tier setting.
- **Part 2 (21).** Every gate report whose runs predate saved run records, read table by table.
  A configuration that appears in both parts is counted once, in Part 1.

**Part 1, from saved run records (47).**

| # | Configuration | Seen in |
|---|---|---|
| 1 | Momentum v2, all on (M9.5) | after-tax, trial-sharpes, cap-tiers, M12.R |
| 2 | Momentum v2, D13 paper config | M12.R, M14.5 |
| 3 | D13 + daily regime re-entry | M14.5 |
| 4 | D13 + daily regime re-entry and exit | M14.5 |
| 5 | D13 + daily regime re-entry, 2% band | M14.5 |
| 6 | Naive momentum (M4.10) | after-tax, trial-sharpes, cap-tiers, M12.R |
| 7 | Naive momentum (M4.10) + redeploy | redeploy |
| 8 | Swing composite (M10.7) | every swing campaign |
| 9 | Reversal: 5-day losers | after-tax, trial-sharpes, M12.R |
| 10 | Trend: 1-month | 〃 |
| 11 | Delivery acceleration | 〃 |
| 12 | Turnover expansion | 〃 |
| 13 | Mean proximity (50d) | 〃 |
| 14 | Short composite | 〃 |
| 15 | Short composite + reversal | 〃 |
| 16 | Breakout | 〃 |
| 17 | M10.7 + delivery acceleration | 〃 |
| 18 | M10.7 + 1-month trend | 〃 |
| 19 | Short composite, 2-week holds | 〃 |
| 20 | Short composite, 1-month holds | 〃 |
| 21 | Short composite, 3-month holds | 〃 |
| 22 | M10.7 + regime gate | after-tax, baseline-folds, cap-tiers, M12.R |
| 23 | Short composite + regime gate | after-tax, trial-sharpes, M12.R |
| 24 | Short composite + low-vol leg | 〃 |
| 25 | Short composite + regime + low-vol | 〃 |
| 26 | Short composite, top-10 | 〃 |
| 27 | Short composite, top-10 + regime | 〃 |
| 28 | Short composite, top-5 (retired: never traded under the rails) | after-tax |
| 29 | Swing composite + residual momentum (H1) | round2, M12.R |
| 30 | Swing composite + band-hit avoidance (H2) | round2, M12.R |
| 31 | Swing composite + residual momentum + band-hit avoidance (H3) | round2, M12.R |
| 32 | M10.7 @ fortnightly / 21-session hold | M12.R |
| 33 | M10.7 @ fortnightly / 42-session hold | M12.R |
| 34 | M10.7 @ weekly / 21-session hold | M12.R |
| 35 | M10.7 @ weekly / 10-session hold | M12.R |
| 36 | M10.7 @ weekly / 10-session hold, 2-session floor | M12.R |
| 37 | M10.7 @ monthly / 63-session hold | M12.R |
| 38 | M10.7 @ monthly / 126-session hold | M12.R |
| 39 | M10.7 @ quarterly / 126-session hold | M12.R |
| 40 | M10.7, band 1.5x top_n | M12.R |
| 41 | M10.7, band 5x top_n | M12.R |
| 42 | Multi cap: 8 large + 8 mid + 8 small (liquidity tiers) | cap-tiers |
| 43 | Focused midcap: top 20 mid (liquidity tier) | cap-tiers |
| 44 | Focused smallcap: top 20 small (liquidity tier) | cap-tiers |
| 45 | Swing composite (M10.7) + redeploy | redeploy |
| 46 | M10.7 + regime gate + redeploy | redeploy |
| 47 | Multi cap + redeploy | redeploy |

**Part 2, from reports that predate saved run records (21).**

| # | Configuration | Report |
|---|---|---|
| 48 | v2: + 12-1 momentum (alone) | `M9-momentum-v2-report.md` |
| 49 | v2: + turnover banding (alone) | 〃 |
| 50 | v2: + regime filter (alone) | 〃 |
| 51 | v2: + vol-scaled weights (alone) | 〃 |
| 52 | v2: + redeploy proceeds next session (alone) | 〃 |
| 53 | v2: all four M9.5 toggles, no redeploy | 〃 |
| 54 | v2: + vol target 15% (alone) | 〃 |
| 55 | M10.7, cadence weekly (63-session re-underwrite) | `M10-swing-composite-report.md` |
| 56 | M10.7, no trailing stop | 〃 |
| 57 | M10.7, 12% trailing stop | 〃 |
| 58 | M10.7, delivery leg only | 〃 |
| 59 | M10.7, no delivery leg | 〃 |
| 60 | M10.7, 12-1 momentum leg only | 〃 |
| 61 | M10.7, no volatility screen | 〃 |
| 62 | Sector rotation | `M10-sector-rotation-report.md` |
| 63 | Plain momentum on the sector-rotation universe | 〃 |
| 64 | Fundamentals: VALUE | `M10-fundamentals-signal-report.md` |
| 65 | Fundamentals: GROWTH | 〃 |
| 66 | Fundamentals: QUALITY_VALUE | 〃 |
| 67 | Fundamentals: MOMENTUM_VALUE | 〃 |
| 68 | Forecast, daily | `X2-forecast-daily-report.md` |

**Found in those reports but not counted, because each is already in Part 1:**
- the M9.5 "naive (all off)" row, which is #6;
- "all on + redeploy", which is D13, #2;
- "all on + redeploy + vol target 15%", which is #1;
- the M10.7 report's monthly cadence, max-hold-21 and both band rows (#37, #32, #40, #41);
- its "10x liquidity floor" row, which is #8 at the high floor.

Row 63 is counted conservatively. It is naive momentum restricted to a different universe, and
counting it raises N, which makes criterion 5 harder to pass, never easier.

**Plus the M16 arms (7):** A1, A2, A3, A4, A5, A6, A7. **N = 75.**

## Appendix B — implementation names

These are names, not behaviour; §2 governs behaviour. The M16 arm set
(`backtest/m16_arms.py`) refers to the A1–A5 policy options by these names, which M16.1–M16.3
implement:

| Arm | Policy | Option |
|---|---|---|
| A1 | `MomentumV2Parameters` | `absolute_momentum = True` |
| A2 | `MomentumV2Parameters` | `industry_gate_top = 5` |
| A3 | `MomentumV2Parameters` | `residual_momentum = True` |
| A4 | `MomentumV2Parameters` | `profitability_filter = True` |
| A5 | `SwingCompositeParameters` | `weight_earnings_surprise = 1` |
| A7 | `SwingCompositeParameters` | `weight_volatility = −1`, `weight_high = weight_delivery = weight_momentum = 0` (exists) |

If a landed PR chose a different field name, the arm set's mapping is updated to it. That is a
rename, not a new trial, as long as the behaviour is the one §2 states.
