# Pre-registration: per-stock rebalance, 2026-10-15

Written **before** either arm below has been run on any window. No return, XIRR, drawdown or Sharpe
of either arm exists anywhere, and the baseline has not been re-run on the study commit. Only unit
tests on synthetic books have been run. Nothing below may be changed after the first study run. A
changed arm (another band, another trigger, another target definition, another cadence) is a **new
trial**: it gets a new name here and counts toward the trial total in §6.

## 0. The question

The owner asked whether rebalancing should be per stock instead of per portfolio. D13
(`PAPER_RATIFIED_2026_09_06`, the paper book) rebalances the whole book on the first trading session
of each month (`backtest.run._first_session_of_each_month`; the policy asks `is_rebalance`). On that
session it ranks the universe on 12-1 momentum, sells every holding outside the top 30
(`sell_band`), and buys toward inverse-volatility target weights over the top 20 through the shared
allocator (`simulate_sip_instalment`). The allocator tops up underweight names and **never trims an
overweight one**. Between rebalance sessions nothing happens per stock: the session after a
rebalance redeploys its sale proceeds into the basket it chose, and every other session is a
heartbeat.

Two per-stock rules are measured against D13, each alone. Neither is a tuning exercise: each rule
and its one number are fixed here.

## 1. Code and data state

- Branch `research/per-stock-rebalance`, from `main` at `89c588a` (PR #122 and PR #123, M14.6,
  merged). The study runs on one commit carrying the two switches, their tests and the driver
  (`backtest/per_stock_rebalance.py`). That commit is recorded in the frozen baseline record.
- **The baseline is re-run on that same commit** and frozen (`frozen-baseline.json`: every run's
  digest and the sha256 of its summary, ledger and NAV files) before either arm is replayed. The
  arms phase refuses to start unless the record exists and the current code gives the same digests.
- Engine switches, the same as M12.R, M14.5, M14.6 and M16: book corporate actions on (the store's
  actions, in the book and as the signal's pre-seam split factors), interest on idle cash on
  (`backtest/repo_rates.yaml`, repo − 50 bp), every order through A8's ratified rails
  (`RailGate`), the one `SimBroker` and shared cost model, ₹10 lakh opening book, one deposit.
- Universe: **floor-only** (`turnover_floor`). The PIT NIFTY 500 membership history opens
  2016-10-24, after F1's test window opens (2016-09-01), so NIFTY 500 cannot run F1 and is not run.
- Lake: L1 prices through 2026-10-14 at the time of writing. The momentum-v2 candidate sets are
  served from the M14.6 signal panel `~/campaign/m14-6-panel-5283bc5` (cut 2016-09-01 … 2026-10-13
  with the same `_L1MomentumV2Data._compute` every backtest uses; its 145-session live re-check on
  2026-10-14 found 0 mismatches; no `prices_raw` or `prices_adjusted` partition before 2026-10-14
  has changed since it was cut). A panel does not enter a run's digest. Baseline and arms read the
  same panel, so they see the same candidate sets.

## 2. Frozen baseline

**D13 = `PAPER_RATIFIED_2026_09_06`**, sweep label "Momentum v2, D13 paper config"
(`backtest.sweep.D13_PAPER_BASELINE`): top 20, 12-1 ranking, sell band 30, monthly regime filter on
the NIFTY 50 TRI against its 200-session mean, inverse-vol weights, next-session redeploy, no vol
target.

Why D13 and not the round-2 pair. `backtest/decision_rule.py` is the round-2 rule
(`preregistration-signals-2026-09-29.md` §4). Its campaign driver, `backtest/fold_campaign.py`,
freezes the M10.7 swing composite and M10.7 + regime gate, because round 2's H-arms were swing
composite variants. Both arms here are D13 with one switch added. The rule compares a candidate with
the configuration it changes. M16's pre-registration (§2) and M14.6 name D13 as the reference for
every D13 variant, and D13 is the book paper trading runs. Comparing a D13 variant with M10.7 would
measure the gap between two strategies, not the effect of the switch. So the baseline is D13, run on
the fold test windows by this study's driver, before either arm.

## 3. Arms (parameters fixed now)

Each arm is D13 with exactly one switch on. Every other parameter is D13's. Both switches are off by
default and absent from `MomentumV2Parameters.__repr__` while off, so D13's run fingerprint and
replay digest are unchanged. A unit test pins D13's replay digest struck before M14.6.

**Arm 1 — "D13 + daily band exit"** (`D13_DAILY_BAND_EXIT`, `band_exit_daily = True`).

- *When.* On every session that is not a rebalance session. Rebalance sessions are exactly D13's.
- *Ranking.* The ranking the monthly rebalance would make that session: the candidate set read
  through `ctx.pit.admit` (point-in-time; a record dated after the session trips the guard), ranked
  on 12-1 momentum descending, ties by ISIN.
- *Rule.* Each **settled** holding outside the top 30 of that ranking is sold in full. "Outside"
  includes a name not ranked that session at all, exactly as the rebalance treats it. Shares still
  in settlement cannot be delivered; they wait for a later session.
- *Never.* The rule never buys and never sells part of a holding.
- *Parked book.* While the last rebalance parked the book (regime risk-off), there is no check. A
  park already sells everything, and the monthly park re-issues those sells.
- *Freed cash (decided now).* D13 has `redeploy_next_session` on. That mechanism **applies** to
  these sells, through the same code path (`_redeploy`). On the session after an exit, the free cash
  is deployed toward the last rebalance's target weights at that session's prices. An exited name
  leaves that basket until the next rebalance, and the remaining weights are renormalised to sum to
  one. The allocator tops up underweight basket names, never buys a name outside the basket, and
  never buys a name being sold that session. The rule creates no new buy logic. Cash that is not
  yet settled on that session waits, exactly as it does after a rebalance.

**Arm 2 — "D13 + daily drift trim 25%"** (`D13_DRIFT_TRIM`, `drift_trim_band = 0.25`).

- *When.* On every session, rebalance sessions included. On a rebalance session it runs against
  that session's new weights, beside the rebalance's own band sells and buys.
- *Target weight.* The inverse-volatility weight that the last risk-on rebalance gave the name in
  its top-20 basket: D13's model weight, sum 1 over the basket.
- *Book value V.* Cash (settled and unsettled), plus every lot, settled and in settlement, marked at
  that session's admitted candidate price. A lot with no candidate price that session is carried at
  its average cost. This is the valuation the rebalance's buy sizing uses.
- *Target value.* T = weight × V.
- *Name value.* H = (settled + in-settlement shares) × the session's price.
- *Rule.* A settled holding is trimmed **iff H > 1.25 × T**, strictly: exactly +25% is not trimmed.
  - The trim sells floor((H − T) / price) shares, which brings the name back to target without going
    below it, capped at the settled quantity.
  - A trim worth less than ₹5,000 (`MIN_ORDER_VALUE_INR`, the floor below which the allocator
    places no buy) is not placed.
  - A held name with no target weight is never trimmed. That is a name kept inside the 21–30 band
    but outside the top-20 basket.
  - A name with no price that session is never trimmed. Its value is not guessed.
- *Parked book.* While the book is parked, there are no trims.
- *Freed cash.* As in arm 1: the next session redeploys free cash toward the last rebalance's target
  weights, through `_redeploy`. An overweight name is never bought by it, because the allocator only
  reduces drift.
- *25 % is fixed a priori.* It is `CONTROL_REBALANCE_BAND` in `analyst/fundmanager/controls.py`, the
  AI fund managers' control rebalance band, and a unit test pins the two equal. It is not swept.

**Both arms.**

- Every sell goes through A8's rails, like any other sell. A sell that would take the book below
  the 8-holding floor is refused and journaled `RAIL_BLOCK`. The rails are not bypassed.
- Every decision is journaled. A session where the check found nothing carries the check's evidence
  item (`daily_band_exit` / `drift_trim`, value = sells placed) on the engine's heartbeat. A parked
  session carries a `per_stock_rebalance` item saying it was skipped and why.
- Money is Decimal throughout, joins are by ISIN only, and costs come from the injected broker's
  shared model.
- A run tallies its SELL decisions by kind (`RunSummary.policy_sells`), so the report can say how
  many exits and trims fired without replaying.

No other arm, band, cadence or combination is run in this study.

## 4. Windows and floors (fixed)

| Fold | Test window (`backtest/folds.yaml`) |
|---|---|
| F1 | 2016-09-01 → 2019-08-30 |
| F2 | 2019-09-02 → 2022-08-31 |
| F3 | 2022-09-01 → 2026-08-31 |

- Each arm and the baseline run on each fold's test window, from cash.
- Each also runs **one continuous run**, 2016-09-01 → 2026-08-31, the union of the test windows. It
  is the report's headline, the fold campaign's convention, and **presentation only**: no criterion
  reads it.
- **₹10 crore/day** median-turnover floor: the decision is struck here.
- **₹1 crore/day** floor: every run is repeated and reported beside the decision, labelled
  *information only*. It has no verdict and decides nothing, per the strategy-search mandate's both
  floors, every arm.

## 5. Decision rule (fixed): `backtest/decision_rule.py`, exactly

Each arm is judged by `evaluate(candidate, D13, trials=N, sharpe_variance=V)` on F1–F3 at ₹10 crore:

1. Its mean after-tax XIRR across the three test windows improves on D13's by **≥ 1.0 pp**. This is
   the arithmetic mean of the per-fold realised after-tax XIRRs (30% slab, no surcharge, tax paid at
   FY end, cash interest on).
2. After-tax XIRR improves strictly in **at least 2 of 3** test windows.
3. Max drawdown (each run's own, pre-tax NAV path) is worse than D13's by **no more than 3.0 pp** in
   every test window.
4. Deflated Sharpe ratio of its daily after-tax NAV returns over the concatenated test windows,
   deflated for N trials with variance V, measured against D13's Sharpe on the same basis, is
   **≥ 0.95**.

**Uncomputable is FAIL.** A fold without an after-tax figure or returns fails the criteria that need
it, and folds are never dropped. Figures are compared as exact Decimals, never rounded first.

**Verdicts.**

- **KEEP**: all four PASS (the code's "KEPT").
- **REJECT**: any FAIL ("NOT KEPT").
- **INCONCLUSIVE**: no FAIL, but criterion 4 INCONCLUSIVE because V has too few trial Sharpes.
  Still not kept.

**A KEEP changes nothing by itself.** It is evidence for the owner. Paper keeps running D13 until the
owner decides otherwise. Both arms are judged against D13 independently. There is no "pick the
better arm" step, so passing one does not depend on the other.

## 6. Trial count N and the DSR variance (fixed now)

**N = 89 + 2 = 91.** The 89 trials already run on this lake:

- **75**: M16's count (`preregistration-m16-2026-10-08.md` §6 and Appendix A). That is the 68
  configurations on record before M16 plus M16's seven arms.
- **5**: M14.6's per-stock entry variants V1, V2, V3, V3b, V4 (`ops/gates/M14.6-per-stock-entry-
  report.md`; D13 itself is already in the 68).
- **2**: the MA-timing study's part 2, D13 with `regime_ma_days` = 50 and = 30 (branch
  `research/ma-timing`, run on this lake on 2026-10-14).
- **7**: that study's part 3, the stock-level 30-DMA arms run on the mandate windows.

**Not counted:**

- The MA-timing study's part-1 index-timing rules. They time an index ETF, not a stock-selection
  configuration on this universe, and are left out on the same footing as signal studies (M16 §6).
- The M17 AI fund managers. They are a forward paper test, not a backtest.

**+2** are this study's two arms. The D13 re-run is not a new trial. Counting conservatively raises
N, which only makes criterion 4 harder to pass.

**The Sharpe variance V** is `trial_sharpe_variance` over two sources:

- *This study's runs:* D13 and both arms, each a per-period Sharpe of its daily after-tax returns
  over the concatenated F1–F3 test windows at ₹10 crore.
- *Supplied:* the **20 round-1 trial Sharpes** in `~/campaign/trial-sharpes-a4a4003/trial-
  sharpes.json`. These are the round-1 sweep arms on the same three folds, at the same ₹10 crore
  floor, on the same floor-only universe, with book corporate actions on and interest at repo −
  50 bp.

The code's own rules apply. A duplicate label or an identical Sharpe is counted once. An arm missing
a fold is excluded and named. Below 20 distinct Sharpes, criterion 4 cannot PASS.

**Provenance stated, not hidden.**

- The 20 supplied Sharpes were struck at commit `a4a4003` on the lake through 2026-10-01, under the
  round-2 pre-registration.
- The repo-rate schedule has since gained only one row, effective 2026-10-07, and carried its
  coverage forward. No rate in force over F1–F3 changed (`git diff a4a4003 -- backtest/
  repo_rates.yaml`).
- They are the variance's sample, not a comparison. Re-running them would cost about 60 more
  replays on a shared box.
- The report prints V, its n, and every Sharpe in it.

## 7. What the report carries (`ops/studies/per-stock-rebalance-2026-10-15.md`)

- Generated tables come from `python -m backtest.per_stock_rebalance render`.
- **Per arm and per fold, both floors:**
  - pre-tax and after-tax XIRR, and max drawdown;
  - buys and sells (fills), and how many daily band exits and drift trims fired (decided);
  - charges (the shared cost model) and realised-gains tax paid;
  - rail blocks by rail.
- The baseline's figures appear on the same basis, plus the continuous headline.
- **Each arm's four-criterion verdict, with its figures printed beside it:** N, V, V's n and the
  Sharpes. At ₹1 crore there are figures only.
- If a run fails, the bug is fixed, the fix is noted in the report, and the run is repeated. The
  arm's definition above is never changed after a figure exists.

## 8. Amendments

An amendment may change an arm, the windows or the rule **only before any study figure exists**,
meaning no XIRR, drawdown or Sharpe of the baseline re-run or of either arm. It is dated here,
states what changed and why, and says what had been computed. After the first figure exists, any
change is a new trial.

(none)
