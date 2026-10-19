# Pre-registration: holding-period study, 2026-10-19

Written **before** any run of this study exists. No XIRR, drawdown or probability of any interval or grid
cell below has been computed. What has been looked at: the NIFTY 500 / NIFTY Smallcap 250 / NIFTY Midcap
150 TRI levels (to date the peaks and troughs the named intervals start on), the NIFTY 500 P/E series'
coverage, and the frozen D13 records of two earlier studies (§2.2). Nothing below may change after the
first study figure exists; an idea that arises from the results goes under "follow-ups (post hoc)".

## 0. The question

The owner (2026-10-19): *take the most successful algo (momentum v2 D13, or the market-P/E variant if
that wins); consider at least 20 intervals chosen by market situation; find the minimum investment period
needed to get the expected return from the data we have — very long, very short, starting any time in
the year, and starting and stopping several times. Keep going until there is clarity.*

The study measures **horizon and entry-timing sensitivity only**. No strategy parameter is tuned.

## 1. Which algorithm (fixed before the P/E study's result is known)

- **D13 = `PAPER_RATIFIED_2026_09_06`** ("Momentum v2, D13 paper config": top 20, 12-1 ranking, sell band
  30, monthly regime filter on the NIFTY 50 TRI against its 200-session mean, inverse-vol weights,
  next-session redeploy) is always run.
- If the P/E-timing study's report (`ops/studies/pe-timing-*.md`, branch `research/pe-timing`) gives any
  arm **ADOPT** under `backtest/decision_rule.py`, the ADOPT arm with the **highest mean after-tax XIRR**
  (as that report states it) is run as a second strategy through the identical phases and render. If no
  arm is ADOPT, D13 only, and the report says so. No other selection, and no eyeballing.

## 2. Code, data and the frozen baseline

### 2.1 Code and engine

Branch `research/holding-period` from `main` at `c373ba2`. The study runs on the commit that adds this
file; the driver is `backtest/holding_period.py` (arithmetic in `holding_period_stats.py`). Engine
switches exactly as the fold studies (M12.R, M14.x, M16, per-stock rebalance, P/E timing): book corporate
actions on, interest on idle cash at repo − 50 bp, every order through A8's ratified rails, the one
`SimBroker` and shared cost model, ₹10 lakh opening book, one deposit, universe **`turnover_floor`**
(liquidity floor only — §3). The store's corporate actions are loaded **once per driver invocation** and
handed to every worker, so all runs of one phase share one store state; the render lists every
book-action state found across runs. Momentum-v2 candidate sets are served from the M14.6 signal panel
`~/campaign/m14-6-panel-5283bc5` where it covers (2016-09-01 → 2026-10-13); elsewhere they are computed
live by the same code (identical records by construction, M14.6).

### 2.2 Frozen D13 check, and what was found before any run

D13 is first run on the fold test windows F1, F2, F3 and the continuous F1–F3 window (2016-09-01 →
2026-08-31) at both floors, then frozen (`frozen-baseline.json`) and compared with the per-stock rebalance
study's frozen D13 (`~/campaign/per-stock-rebalance-a0be143/frozen-baseline.json`, lake through
2026-10-14).

**Found before this study ran anything** (comparing the per-stock record with the P/E study's 2026-10-19
re-run of the same eight runs, which stopped that study): every digest moved because the run spec's
`book_actions` identity moved (store dividends 30,736 → 30,643, rights 840 → 841, with no code change
on `main`). Six runs are economically identical (NAV path, fills, figures). Two — F1 and the continuous
window at ₹10 crore — differ: **one TCS dividend credit (INE467B01029, ₹92, 2018-10-23) is in the
2026-10-14 store and not in today's**, so cash interest compounds a few rupees differently from that day;
F1's fills are identical (401) and it ends ₹96 lower (pre-tax XIRR 4.6007 % → 4.5977 %); the continuous
run's first fill difference comes a year later (2019-11-04, one share fewer of a buy), and it ends at
24.18 % pre-tax against 24.14 %. This is corporate-action-store drift, not a code change.

**Rule (fixed now):** a baseline run that differs from the reference is accepted as *store drift* and
D13 is re-frozen on the current store, with the reason printed, when (a) a dividend or corporate event in
the run's ledger moved, (b) the NAV first diverges no earlier than the first moved credit, and (c) no
fill differs before that date. Anything else — a fill differing first, or a NAV moving with no credit
moved — is a different D13 and stops the study.

## 3. The span, and why it is not longer

- **Study span: 2012-07-04 → 2026-10-15.** 2012-07-04 is the first session on which D13's whole
  look-back (12-1 momentum: 253 prints; the 365-day liquidity window) lies in joinable, ISIN-bearing L1
  history, which starts 2011-06-22 (`backtest/windows.yaml`, `full.start`). 2026-10-15 is pinned: the
  lake's last session at writing is 2026-10-16, kept as T+1 fill headroom, and pinning keeps every run's
  terminal fixed however many sessions the scheduler lands during the campaign.
- **Not earlier.** L1 now reaches 2006, but the 2006 → 2011-06-21 rows come from bhavcopies with no ISIN:
  they are linked to ISINs by chain, names that stopped trading before 2011-06-22 have no ISIN witness
  (≈ 92,000 rows quarantined), and corporate actions before 2011 are largely missing
  (`ops/gates/l1-widen-2026-10-06.md`). A D13 universe there would be survivorship-biased, and
  extending L2 over it is held (M13.4). Not used.
- **Universe.** The paper book screens NIFTY 500 point-in-time membership, which opens 2016-10-24; the
  study runs `turnover_floor` (₹ median-turnover floor only), which is survivorship-safe from 2011-06-22
  and is what every fold study ran. It is close to, not identical with, the paper book. A nominal
  ₹10 crore/day floor is stricter in 2012 rupees than in 2026 rupees, so early-span books are drawn
  from fewer names.
- **Independence.** The span holds 170 month starts. A 12-month horizon has only 14 non-overlapping
  windows, 36 months 4, 60 months 2, 84 months 2. Every probability is shown with its count of
  non-overlapping windows and a 90 % Wilson interval struck on that count, not on the overlapping
  count. Long-horizon answers rest on two or three independent market paths and are labelled as such.

## 4. What is run

All runs start from cash with D13's real entry: it rebalances on its first session (fills T+1), and a
book that opens with the regime off stays in cash until a risk-on month.

| Phase | Runs | Floors |
|---|---|---|
| `baseline` | F1, F2, F3 test windows, continuous F1–F3 (§2.2) | ₹10 cr, ₹1 cr |
| `named` | the 40 named intervals (§4.1) | ₹10 cr (deciding), ₹1 cr (information only) |
| `grid-start` | from the first session of every month 2012-08 … 2026-09 to 2026-10-15 | ₹10 cr |
| `grid-mid` | from the first session on or after the 15th of every month, same span | ₹10 cr |
| `grid-day8` | the same on the 8th — **only if** §6 finds a hurdle unstable after the two grids | ₹10 cr |

`baseline`, `named`, `grid-start` and `grid-mid` run unconditionally, whatever the first results
show. At most two replays at a time; nothing starts while the P/E study's driver is alive.

**Grid cells are exact, not approximated.** A replay never reads past its decision session, so the run
from start S to the span end holds, up to any session T, exactly the NAV path and fills of a run from S
to T. Each grid cell (S, h) is scored from that run's NAV at T and its fills, credits and events
truncated at T. This is verified rather than assumed: every named interval and baseline window that
opens on a month-start grid session is a separate run, compared point by point with the grid run's
prefix (it already holds for F1 against the continuous window on the P/E study's runs: 741 of 741 NAV
points and 401 of 401 fills identical).

### 4.1 Named intervals

Peaks and troughs are the NIFTY 500 TRI's own all-time highs and drawdown lows (of its >10 % drawdowns
since 2012, these are the ones used; 2016-09 → 2016-12 at −12 % and 2018-01 → 2018-03 at −11 % are covered
by N11 and N14/N15): 2013-01-15 → 2013-08-28, 2015-03-03 → 2016-02-25, 2018-08-31 → 2018-10-26, 2020-01-17 →
2020-03-23 (−38 %), 2021-10-18 → 2022-06-20, 2022-12-01 → 2023-03-28, 2024-09-26 → 2025-02-28. The
2018 small/mid-cap peak is the NIFTY Smallcap 250's (2018-01-15).

| Key | Interval | Start → end | Regime (a priori) |
|---|---|---|---|
| N01 | Full span (defines E, §5.2) | 2012-07-04 → 2026-10-15 | mixed |
| N02 | D13 decade (F1–F3 continuous) | 2016-09-01 → 2026-08-31 | mixed |
| N03 | Six-year window of the old ~23 % figures | 2019-07-01 → 2026-10-15 | mixed |
| N04 | First year of the span | 2012-07-04 → 2013-07-03 | sideways |
| N05 | Taper tantrum: Jan-2013 high to Aug-2013 low | 2013-01-15 → 2013-08-28 | bear |
| N06 | Aug-2013 low, 12 m | 2013-08-28 → 2014-08-27 | recovery |
| N07 | 2014 election year | 2014-01-01 → 2014-12-31 | bull |
| N08 | Mar-2015 all-time high to Feb-2016 low | 2015-03-03 → 2016-02-25 | bear |
| N09 | Mar-2015 all-time high, 24 m (flat) | 2015-03-03 → 2017-03-02 | sideways |
| N10 | Feb-2016 low, 12 m | 2016-02-25 → 2017-02-24 | recovery |
| N11 | Demonetisation, 3 m | 2016-11-09 → 2017-02-08 | crash |
| N12 | Demonetisation, 12 m | 2016-11-09 → 2017-11-08 | bull |
| N13 | 2017 bull year | 2017-01-02 → 2017-12-29 | bull |
| N14 | 2018 small/mid-cap bear from the Jan-2018 small-cap peak | 2018-01-15 → 2019-08-30 | bear |
| N15 | Jan-2018 high, 24 m (flat broad market) | 2018-01-23 → 2020-01-17 | sideways |
| N16 | IL&FS sell-off, 6 m | 2018-09-21 → 2019-03-20 | crash |
| N17 | Aug-2018 all-time high, 12 m | 2018-08-31 → 2019-08-30 | bear |
| N18 | COVID: from just before the peak, calendar 2020 | 2020-01-01 → 2020-12-31 | mixed |
| N19 | COVID: at the Jan-2020 peak, 3 m | 2020-01-17 → 2020-04-16 | crash |
| N20 | COVID: at the Jan-2020 peak, 24 m | 2020-01-17 → 2022-01-17 | mixed |
| N21 | COVID: at the Mar-2020 trough, 3 m | 2020-03-23 → 2020-06-22 | recovery |
| N22 | COVID: at the Mar-2020 trough, 12 m | 2020-03-23 → 2021-03-22 | recovery |
| N23 | COVID: in the recovery, Jun-2020, 12 m | 2020-06-01 → 2021-05-31 | recovery |
| N24 | 2021 bull run to the Oct-2021 high | 2021-01-01 → 2021-10-18 | bull |
| N25 | Oct-2021 all-time high, 12 m | 2021-10-18 → 2022-10-17 | bear |
| N26 | 2022: sideways, rate hikes | 2022-01-03 → 2022-12-30 | sideways |
| N27 | Jun-2022 low, 12 m | 2022-06-20 → 2023-06-19 | recovery |
| N28 | 2023–24 small-cap melt-up: Mar-2023 low to Sep-2024 peak | 2023-03-28 → 2024-09-26 | bull |
| N29 | Sep-2024 all-time high to Feb-2025 low | 2024-09-26 → 2025-02-28 | bear |
| N30 | Sep-2024 all-time high, 12 m | 2024-09-26 → 2025-09-25 | bear |
| N31 | Sep-2024 all-time high to span end | 2024-09-26 → 2026-10-15 | mixed |
| N32 | Feb-2025 low, 12 m | 2025-02-28 → 2026-02-27 | recovery |
| N33 | Latest year | 2025-10-15 → 2026-10-15 | mixed |
| N34 | Latest 6 months | 2026-04-15 → 2026-10-15 | mixed |
| N35 | Latest 3 months | 2026-07-15 → 2026-10-15 | mixed |
| N36 | First 7 years of the span | 2012-07-04 → 2019-07-03 | mixed |
| N37 | Mar-2015 all-time high, 5 years | 2015-03-03 → 2020-03-02 | mixed |
| N38 | COVID peak, 5 years | 2020-01-17 → 2025-01-16 | mixed |
| N39 | COVID trough, 5 years | 2020-03-23 → 2025-03-21 | mixed |
| N40 | 2017–2021, 5 years | 2017-01-02 → 2021-12-31 | mixed |

Each row is also tagged, from the data, with the NIFTY 500 P/E at its start, the NIFTY 500 TRI's distance
below its all-time high at its start, and D13's regime reading at its start.

### 4.2 Grid horizons

Month-start grid: h ∈ **{1, 3, 6, 9, 12, 18, 24, 36, 48, 60, 84} months** plus "to span end"; the
dense set adds {2, 4, 15, 21, 30, 42, 54, 72} (free, the same runs). A cell (S, h) runs from the anchor
session S to the session before the anchor session h months later; a cell is scored only if that later
anchor session is on or before 2026-10-15 (no cell ends inside an incomplete month). The mid-month grid
uses the coarse horizons.

## 5. Figures

### 5.1 Per window (cell or named interval)

- **Walk-away after-tax XIRR — the primary figure.** The book at T is deemed sold at T's marks (each
  held name's last close, as the engine marks its own terminal), the sale is charged through the shared
  cost model, and the deemed gains are taxed (`backtest.tax.compute_after_tax`, FIFO lots, 30 % slab,
  no surcharge, tax paid at FY end — the fold campaigns' `PROFILE`; the *liquidated* figure). It is what
  an investor who exits at T keeps, which is the question.
- **Realised after-tax XIRR** — the decision rule's figure (holdings at T still valued pre-tax), beside
  it. **Pre-tax XIRR.**
- Max drawdown (pre-tax NAV, within the window), longest time under water (calendar days below the
  window's running peak, an unrecovered spell counted to the end), trades, charges (named intervals: the
  run's own total), and the **parked share**: sessions on which D13's latest rebalance reading was
  risk-off (the book's regime parking; its first session counts as cash).
- **NIFTY 500 TRI, same window:** bought at the first session's close and held as an index ETF through
  `backtest.ma_timing` (card + 0.1 % slippage a side, repo − 50 bp on residual cash), sold at the last
  close the same way, taxed the same way (walk-away). **Cash, same window:** repo − 50 bp, Actual/365,
  credited monthly, interest taxed at the 30 % slab.

### 5.2 Hurdles

1. **E** = D13's own walk-away after-tax XIRR on the full span (N01) at ₹10 crore — the "expected
   return".
2. Cash after tax over the same window (the repo-based FD-like rate).
3. NIFTY 500 TRI walk-away after tax over the same window.
4. **No loss** (walk-away XIRR ≥ 0 %).
5. 12 % and 6. 15 % a year after tax.

A window meets a hurdle when its walk-away XIRR is **≥** the hurdle. A window whose walk-away figure the
tax layer cannot compute is not scored and is counted in the report.

### 5.3 Minimum holding period

For a hurdle and a confidence c ∈ {**75 %, 90 %, 100 %** (worst case)}: the smallest grid horizon h with
P(meet) ≥ c at h **and at every longer grid horizon up to 84 months** (a horizon that clears the bar and
then falls back is not a minimum). "Not within 84 m" when none qualifies. "To span end" cells are
reported but never enter the minimum. The headline reads the month-start grid, coarse horizons, at
**90 %**.

### 5.4 Also reported (descriptive; no decision rides on them)

- Distribution of walk-away XIRR by horizon (worst, 10th, median, 90th, best), worst and best
  cumulative after-tax outcome with its start date, P(loss), worst max drawdown, longest time under
  water, median TRI and median excess over TRI.
- **Start month:** at 12 and 36 months, per calendar month of the start: median, worst, P(≥ E),
  P(≥ TRI); and a permutation test (2,000 relabellings, fixed seed) of the spread of the twelve monthly
  medians.
- **Month-start vs mid-month starts**, per horizon, including the paired difference within a month.
- **In and out vs staying invested:** patterns 3 m in / 3 m out, 6/6, 12/12, each **in-first** and
  **out-first**, over 24, 36 and 60 months, from every month start. Each invested leg is a fresh-start
  cell (its walk-away after-tax XIRR, i.e. exit, charges and tax at the end of every leg), each cash leg
  earns after-tax cash; legs chain as growth factors. Compared with the staying cell (S, total).
  Approximation stated: a leg's FY-end tax is folded into its XIRR rather than paid on its date.
- **Monthly SIP vs lump sum** over 12, 36 and 60 months from every month start, pre-tax: the SIP buys
  units of the fresh-start run's NAV (D13) or of the TRI level on each month's first session, sold at the
  window's end. `backtest/sip.py` allocates instalments but the replay engine takes one deposit, so a
  per-instalment replay is not available; the unitised (fund-like) figure is a stated approximation.
- **Exact vs the continuous-run slice:** every month-start cell's pre-tax XIRR against the same window
  sliced out of the full-span run (N01) as if bought at S's close — the gap per horizon.
- **Validation** of the exact-grid claim (§4).

## 6. Stability — when the answer counts as settled

For each hurdle, the 90 % minimum horizon (month-start grid, coarse horizons, walk-away) is **stable**
when all hold:

- **S1 (densified starts):** the mid-month grid (and the day-8 grid if run) gives the same horizon, and so
  do all grids pooled.
- **S3 (approximation swapped for exact):** on pre-tax figures against pre-tax hurdles (E pre-tax, cash
  and TRI pre-tax, the fixed rates), the 90 % horizon from exact fresh starts equals the one from the
  continuous-run slice.
- **S4 (densified horizons):** the dense-horizon answer lies between the coarse horizon below the coarse
  answer (exclusive) and the coarse answer (inclusive); "not within 84 m" must agree.

If, after `grid-mid`, any hurdle fails S1, `grid-day8` is run and S1 re-read over three grids. If a
hurdle is still unstable, the report says so plainly and states what moved; that is the answer for that
hurdle. ₹1 crore is information only and enters no rule.

## 7. Report

`ops/studies/holding-period-2026-10-19.md`: a plain-English headline ("minimum holding period to expect X
with Y % confidence"), the horizon × probability tables per hurdle, the named-interval table, worst and
best case by horizon, start-month and in/out findings, SIP, the validation and approximation tables, the
data-span and independence caveats, and what it means for someone investing today. Charts: SVG under
`ops/studies/holding-period-2026-10-19/`. Generated tables come from `python -m backtest.holding_period
render`. If a run fails, the bug is fixed, noted in the report, and the run repeated; no definition here
changes after a figure exists.

## 8. Amendments

(none)
