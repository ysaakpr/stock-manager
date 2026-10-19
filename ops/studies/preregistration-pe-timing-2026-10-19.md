# Pre-registration: PE timing, 2026-10-19

Written **before** any arm, baseline or standalone timing run of this study exists. No return,
XIRR, drawdown or Sharpe of any arm below has been computed. What has been looked at: the P/E
series itself (its coverage and its discontinuities, §1.2) and unit tests on synthetic books. Nothing
below may be changed after the first study figure exists. A changed arm (another mean length,
another threshold, another series, another cadence) is a **new trial**: it gets a new name here and
counts toward the trial total in §6.

## 0. The question

The owner (2026-10-19): *"Can we see if market PE is below [its own average over] monthly, quarterly,
or 200-day history of PE to decide the entry and/or exit criteria, and evaluate whether such a
decision would have gained any benefit along with Momentum v2 or separately. Do a backtest."*

Two parts: **A**, the P/E rule as an overlay on D13 (`PAPER_RATIFIED_2026_09_06`, the paper book);
**B**, the P/E rule alone, timing the NIFTY 500 TRI against cash.

## 1. Code and data state

### 1.1 Code

- Branch `research/pe-timing`, from `main` at `c373ba2`. The study runs on the commit that adds
  this file (the valuation gate, its source, the 18 arms and the driver `backtest/pe_timing.py`
  are already committed below it). That commit is recorded in the frozen baseline record.
- Engine switches, as M12.R, M14.5, M14.6, M16 and the per-stock rebalance study: book corporate
  actions on, interest on idle cash on (`backtest/repo_rates.yaml`, repo − 50 bp), every order
  through A8's ratified rails (`RailGate`), the one `SimBroker` and shared cost model, ₹10 lakh
  opening book, one deposit.
- Universe **floor-only** (`turnover_floor`): NIFTY 500 PIT membership opens 2016-10-24, after F1's
  test window opens, so the `nifty500` universe cannot run F1.
- Momentum-v2 candidate sets are served from the M14.6 signal panel `~/campaign/m14-6-panel-5283bc5`
  (cut 2016-09-01 … 2026-10-13), as in the per-stock rebalance study. Baseline and arms read the
  same panel; a panel does not enter a run's digest.

### 1.2 Data: the P/E series

- `IN.NSE.NIFTY_500.PE` in `L1/macro_series` (NSE's daily close-all snapshot, landed by
  `dataplatform.ingest.macro.index_valuation`): 3,465 sessions, 2012-10-01 → 2026-10-16,
  `release_date` = `period_end` = the session, one revision. **Knowable the evening of the
  session**, exactly as the index close D13's price regime reads — so a decision on session t may
  use P/E through t and nothing later.
- 12 L1 trading sessions have no published P/E (2013-10-09 … 2016-06-20, the last before F1).
  None is inside any window of part A. A reading on a session with no P/E **fails loud**; nothing is
  carried or interpolated.
- **Methodology break, found in the data (not assumed):** the largest one-session move in implied
  earnings (index close ÷ P/E) over the whole series, on both NIFTY 500 and NIFTY 50, is
  **2021-03-31**: NIFTY 500 P/E 43.33 → 35.89 (−17.2 %) while the index moved −0.6 %; NIFTY 50
  40.43 → 33.20 (−17.9 %) the same day. This is NSE's switch from standalone to consolidated
  earnings. Read raw, every trailing mean straddling the date is inflated by pre-break points, so a
  raw gate reads *cheap* for up to L sessions after it whatever the market did. That is material
  for L = 63 and L = 200 (and one month for L = 21), so the study runs a **spliced** variant (§3.1).
- Other discontinuities, reported and **not** corrected: 2019-08-05, one NIFTY 500 P/E print of
  23.49 between 28.91 and 28.84 (NIFTY 50 unaffected) — a one-day error in the source; and
  one-session level steps on 2019-09-25 (−9.5 %, NIFTY 50 too) and 2020-09-25 (+10 %, NIFTY 500
  only), each about half the 2021 break, near NSE's September index reviews. Neither is an
  identified methodology change, so neither is spliced. No value is edited; L0 and L1 are untouched.

## 2. Frozen baseline (part A)

**D13 = `PAPER_RATIFIED_2026_09_06`**, sweep label "Momentum v2, D13 paper config": top 20, 12-1
ranking, sell band 30, monthly regime filter on the NIFTY 50 TRI against its 200-session mean,
inverse-vol weights, next-session redeploy, no vol target.

- It is re-run on the study commit on every window and floor below, then frozen
  (`frozen-baseline.json`: every run's digest and the sha256 of its summary, ledger and NAV files)
  **before any arm runs**. The arms phase refuses to start otherwise.
- **Re-verification:** each D13 run is compared, digest and file hashes, with the per-stock
  rebalance study's frozen D13 record (`~/campaign/per-stock-rebalance-a0be143/frozen-baseline.json`,
  struck at `a0be143` on the lake through 2026-10-14). Any difference stops the study: a moved D13
  is a different baseline.

## 3. The signal and the arms (fixed now)

### 3.1 The signal

On session t: **PE_gate(t) = cheap** iff P/E(t) **<** the simple mean of the last L published P/Es
knowable by t (t's own included); otherwise **rich** (equality is rich). L ∈ **{21, 63, 200}**
sessions (about a month, a quarter, and D13's own 200). Series: NIFTY 500 P/E (the market the
book trades in). NIFTY 50 P/E is used only to confirm the break.

**Spliced variant:** for a session on or after 2021-03-31, each window point dated before
2021-03-31 is multiplied by k = (close(2021-03-30) ÷ P/E(2021-03-30)) ÷ (close(2021-03-31) ÷
P/E(2021-03-31)) of the NIFTY 500 — the implied-earnings ratio across the break (≈ 0.833 from the
published figures; the run computes it from the lake). Both sessions are knowable on 2021-03-31; a
session before the break is never rescaled, so the splice is point-in-time. A window wholly on one
side of the break reads identically raw and spliced.

### 3.2 Part A — D13 overlays (18 arms)

Implemented as an alternative reading on D13's regime seam (`MomentumV2Parameters.valuation_gate`,
`valuation_mean_days`, `valuation_splice`; the P/E reading rides on `RegimeReading` through the same
PIT guard; off by default and absent from `repr` while off, so D13's fingerprint and replay digest
are unchanged — pinned by unit tests). Read **on D13's monthly rebalance session only** (D13's
regime is monthly; no daily reads).

For each L and each series (raw, spliced):

- **A1 — entry gate** (`ENTRY`): D13's price regime parks and re-enters exactly as D13. On a
  risk-on rebalance that reads **rich**, the rebalance places **no buy at all** — no new name, no
  top-up — and nothing is queued for the next-session redeploy. Its band sells (names outside the
  top 30) still happen and the proceeds wait in cash. A parked/empty book stays in cash while rich.
- **A2 — replace** (`REPLACE`): the P/E gate instead of the price regime — park when rich,
  re-enter (a full rebalance) when cheap.
- **A3 — both** (`BOTH`): invested only while the price regime is on **and** the P/E is cheap;
  park if either turns.

Labels: "D13 + PE<L d mean entry gate", "D13, PE<L d mean replaces price regime", "D13 + price
regime AND PE<L d mean", each with " (spliced P/E)" for the spliced series (`backtest.sweep.
PE_TIMING_ARMS`). 9 raw + 9 spliced = **18 arms**. Every park and every blocked rebalance journals
the P/E reading (invariant #9); every sell still goes through A8.

### 3.3 Part B — standalone index timing (6 arms, 2 references)

Hold the NIFTY 500 TRI (`L1/benchmark_tri`, published) while PE_gate is cheap, else cash.
Implemented with `backtest.ma_timing.simulate` (the MA-timing study's engine, unchanged):

- Read at **each month's last close**; the switch trades at the **next session's close** (never the
  same close). Between month ends the last decision stands.
- **Costs:** a whole-book switch of a liquid index ETF (cost key NIFTYBEES, one unit = TRI ÷ 100)
  through the shared cost model's delivery-equity card plus 0.1 % slippage a side. The card has no
  ETF instrument type and charges delivery STT; an *ex-STT* column brackets the true cost from below.
- **Cash:** repo − 50 bp, Actual/365, credited monthly.
- **Tax:** FIFO lots, 30 % slab, FY-end payment; reported **liquidated** (the open holding deemed
  sold at the window's last close), so buy-and-hold's deferred LTCG is charged too.
- Arms: "PE<L d mean timing" for L ∈ {21, 63, 200}, raw and spliced: **6 arms**.
- References (not trials): **buy-and-hold** NIFTY 500 TRI, and the **price rule** — the same
  monthly timing on the NIFTY 500 TRI against its own 200-session mean.
- Windows: **full** — from the first session the 200-session P/E mean is defined (P/E history
  starts 2012-10-01) to the lake's last TRI session, the same start for every row — and D13's
  windows (F1, F2, F3, continuous 2016-09-01 → 2026-08-31).

## 4. Windows and floors (part A, fixed)

| Fold | Test window (`backtest/folds.yaml`) |
|---|---|
| F1 | 2016-09-01 → 2019-08-30 |
| F2 | 2019-09-02 → 2022-08-31 |
| F3 | 2022-09-01 → 2026-08-31 |

Each arm and the baseline run on each fold's test window from cash, plus **one continuous run**
2016-09-01 → 2026-08-31 (headline, presentation only — no criterion reads it). **₹10 crore/day**
median-turnover floor decides; **₹1 crore/day** is run and reported beside it, *information only*.

## 5. Decision rule (fixed): `backtest/decision_rule.py`, exactly

**Part A** — each arm, independently: `evaluate(arm, D13, trials=N, sharpe_variance=V)` on F1–F3 at
₹10 crore:

1. mean after-tax XIRR (realised; 30 % slab, no surcharge, FY-end tax, cash interest on) improves
   on D13's by **≥ 1.0 pp**;
2. after-tax XIRR improves strictly in **≥ 2 of 3** test windows;
3. max drawdown (pre-tax NAV) worse than D13's by **≤ 3.0 pp** in every test window;
4. DSR of daily after-tax NAV returns over the concatenated test windows, deflated for N trials with
   variance V, against D13's Sharpe, **≥ 0.95**.

Uncomputable is FAIL; folds are never dropped; exact Decimals.
**ADOPT** = all four PASS (the code's KEPT); **REJECT** = any FAIL; **INCONCLUSIVE** = no FAIL but
criterion 4 inconclusive (not adopted). An ADOPT changes nothing by itself: paper keeps running D13
until the owner decides otherwise.

**Part B** — the same four criteria, the same code, each P/E timing arm against **buy-and-hold
NIFTY 500 TRI** on F1–F3, after costs (card), with one stated difference: the after-tax figure and
the after-tax NAV are the **liquidated** ones (buy-and-hold never sells, so a realised-gains figure
would leave its deferred LTCG untaxed). The price-rule reference gets no verdict.

## 6. Trial count N and the Sharpe variance (fixed now)

**N = 91 + 18 + 6 = 115.**

- **91**: the per-stock rebalance study's N (PR #125: 75 to M16, 5 M14.6, 2 + 7 MA-timing parts 2
  and 3, and its own 2 arms).
- **18**: this study's part-A arms (raw and spliced counted separately).
- **6**: this study's part-B arms. Index-timing rules were left out of N by earlier studies; they
  are counted here, as the owner's brief asks, which only makes criterion 4 harder to pass.
- Not counted: D13's re-run, buy-and-hold and the price-rule reference.

**V** is `trial_sharpe_variance` over this study's runs plus the **20 round-1 trial Sharpes** in
`~/campaign/trial-sharpes-a4a4003/trial-sharpes.json` (same folds, ₹10 crore, floor-only, book CAs
on, repo − 50 bp — the file the per-stock study used). Part A: D13 + 18 arms + 20 supplied. Part B:
buy-and-hold, the price rule, the 6 arms + the same 20 supplied (a stock-book population, stated as
such in the report: the variance's provenance is printed, not hidden). Duplicates count once; below
20 distinct Sharpes criterion 4 cannot PASS.

## 7. Descriptive statistics (fixed now; they decide nothing)

- **Agreement with the price regime:** on every D13 rebalance session of 2016-09 → 2026-08, the 2×2
  table of D13's price regime (NIFTY 50 TRI ≥ 200-session mean) against PE_gate, per L, raw and
  spliced; agreement share and P(cheap | price on / off). On every session: cheap share, the NIFTY
  500 TRI's mean trailing 21-session return on cheap vs rich sessions, and the cheap share after a
  21-session fall vs rise — whether the rule is a dip-buying signal or a trend filter.
- **The break:** the five largest implied-earnings moves per index, k, and the D13 rebalance
  sessions on which raw and spliced readings differ.
- **Per arm:** parks / re-entries, months parked, months whose buys the entry gate blocked
  (recounted from the readings), fills, charges, tax.

## 8. Report

`ops/studies/pe-timing-2026-10-19.md`: verdict per arm, headline table, the standalone timing
table, agreement statistics, the methodology-break finding, switches and costs per arm, and a
plain-English recommendation. Generated tables come from `python -m backtest.pe_timing render`. If a
run fails, the bug is fixed, the fix is noted in the report and the run is repeated; an arm's
definition is never changed after a figure exists. Ideas that arise from the results go under
"follow-ups (new trials)".

## 9. Amendments

An amendment may change an arm, the windows or the rule only before any study figure exists. After
that, any change is a new trial.

(none)
