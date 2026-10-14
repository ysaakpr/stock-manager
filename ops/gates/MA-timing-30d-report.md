# MA timing — the 30-day moving-average thesis on NSE TRIs, and D13's regime length

> **Generated** by `uv run python -m backtest.ma_timing_report` from the published TRIs in the lake and the saved D13 runs listed in part two; nothing in the tables is typed by hand. The section after the marker at the end is hand-written analysis and is labelled as such. Research only: nothing here changes the paper strategy.

Lake read through **2026-10-13** (read-only; nothing fetched).

## What was run

**Rule (the reading implemented — the article's own rule is not known here):** hold the index's total-return index when its close is at or above its N-session simple moving average of closes, otherwise hold cash. The decision is struck at the close of session t using only closes knowable by t (t's own included) and is traded at the close of the **next** session t+1 — never the same close. N=30 is the thesis; N=10, 20, 50, 100, 150 and 200 are context so 30 is not read alone. No band, daily check unless a table says otherwise. Buy-and-hold of the same TRI is the benchmark, bought at the window's first close.

**Money:** ₹10 L opening book; each switch trades the whole book in a liquid index ETF (cost-model key INF204KB14I2, NIFTYBEES; one unit = TRI ÷ 100) at the close ± 0.1% slippage a side, charged by the shared cost model (`execution.costs`, invariant #4) at the rate card in force on the trade date. The card is delivery equity and charges delivery STT (0.1% a leg since 2012) because it has no ETF instrument type; an equity ETF's exchange STT is far lower, so the ex-STT column brackets the true cost from below. Before 2006-01-01 (where the card starts) a switch is priced on the 2006-01-01 card — a stated proxy. Buy-and-hold pays the same entry costs.

**Cash:** while out of the market the book earns RBI repo - 50 bp, Actual/365, credited monthly (`backtest.cash_interest`, the repo's convention). The lake has FBIL *FX* reference rates only — no FBIL overnight MIBOR / TREPS series — so the repo schedule is the cash rate. It starts 2006-01-01; before that the 2006-01-01 rate (6.25% - 0.5%) is carried back as a proxy. Indian short rates were mostly higher than that in 1995-2001, so the proxy, if anything, understates what timing earned in cash in those years.

**Tax:** `backtest.tax` for a resident individual, 30% slab, no surcharge, tax paid at FY end; STCG/LTCG by FIFO lot on the ETF (Sec 111A/112A, Sec 55(2)(ac) grandfathering at the 31-01-2018 close), interest at slab. Reported as the liquidated after-tax CAGR (open holding deemed sold at the end, so buy-and-hold's deferred LTCG is charged too) and only on windows that open on or after 2006-01-01.

**Not modelled:** ETF expense ratio and tracking error (both strategies hold the ETF; timing holds it less), the TRI's own dividend timing vs an ETF's, intraday execution.

## Data checks (generated)

| Index | Points | First | Last | `index_name` spellings (sessions) | Max |daily move| |
| --- | --- | --- | --- | --- | --- |
| NIFTY 50 TRI | 6788 | 1999-06-30 | 2026-10-13 | 'Nifty 50' x6786; 'NIFTY 50' x2 (2025-12-23, 2025-12-24) | 17.74% |
| NIFTY 500 TRI | 7876 | 1995-01-01 | 2026-10-13 | 'Nifty 500' x7874; 'NIFTY 500' x2 (2025-12-23, 2025-12-24) | 16.22% |
| NIFTY Midcap 150 TRI | 5334 | 2005-04-01 | 2026-10-01 | 'Nifty Midcap 150' x5332; 'NIFTY MIDCAP 150' x2 (2025-12-23, 2025-12-24) | 12.99% |
| NIFTY Smallcap 250 TRI | 5334 | 2005-04-01 | 2026-10-01 | 'Nifty Smallcap 250' x5332; 'NIFTY SMALLCAP 250' x2 (2025-12-23, 2025-12-24) | 12.37% |

Each index is one parquet file per session: the case-variant `index_name` on two sessions is a label on the only point for that session, not a second value, so there is nothing to disagree; `load_index` normalises case and would refuse a name that differed otherwise. Every point's `knowable_date` equals its session (a TRI close is knowable at that close).

## Headline — N=30 against buy-and-hold

Daily check, no band, signal at close t, switch at close t+1. **After costs** is the shared cost model's delivery card plus slippage (the reported figure); **ex-STT** leaves the card's STT line out (the lower bound on what an ETF switch costs); **after tax** is the after-cost run taxed (FIFO lots, STCG/LTCG, slab tax on cash interest) with the terminal holding deemed sold — only on windows every model covers. CAGR/DD is after costs.

### NIFTY 50 TRI

| Window | Strategy | CAGR before costs | **CAGR after costs** | CAGR ex-STT | CAGR after tax | Max DD | **CAGR/DD** | % invested | Switches/yr | Excess vs B&H (after costs) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| full | Buy-and-hold | 12.44% | **12.44%** | 12.44% | — | 59.48% | **0.21** | 100.00% | 0.00 | — |
| full | **N=30** | 13.61% | **8.89%** | 11.16% | — | 34.42% | **0.26** | 61.64% | 19.59 | -3.55pp |
| 2006→ | Buy-and-hold | 11.86% | **11.85%** | 11.86% | 11.47% | 59.46% | **0.20** | 100.00% | 0.00 | — |
| 2006→ | **N=30** | 11.40% | **6.44%** | 8.82% | 4.45% | 34.42% | **0.19** | 62.54% | 20.74 | -5.42pp |
| decade | Buy-and-hold | 11.95% | **11.94%** | 11.94% | 11.16% | 38.24% | **0.31** | 100.00% | 0.00 | — |
| decade | **N=30** | 10.08% | **4.92%** | 7.31% | 3.27% | 18.67% | **0.26** | 64.70% | 22.51 | -7.02pp |
| six-year | Buy-and-hold | 11.68% | **11.66%** | 11.67% | 10.70% | 38.23% | **0.31** | 100.00% | 0.00 | — |
| six-year | **N=30** | 10.17% | **5.09%** | 7.46% | 3.33% | 16.74% | **0.30** | 63.16% | 22.18 | -6.57pp |
| wf-selection | Buy-and-hold | 15.71% | **15.68%** | 15.69% | 14.92% | 38.24% | **0.41** | 100.00% | 0.00 | — |
| wf-selection | **N=30** | 13.12% | **7.98%** | 10.35% | 6.24% | 18.67% | **0.43** | 69.42% | 21.61 | -7.70pp |
| wf-verification | Buy-and-hold | 8.38% | **8.34%** | 8.36% | 7.67% | 16.43% | **0.51** | 100.00% | 0.00 | — |
| wf-verification | **N=30** | 7.19% | **1.97%** | 4.40% | 0.64% | 16.75% | **0.12** | 59.97% | 23.42 | -6.38pp |
| full-1st-half | Buy-and-hold | 12.55% | **12.54%** | 12.54% | — | 59.48% | **0.21** | 100.00% | 0.00 | — |
| full-1st-half | **N=30** | 17.56% | **13.15%** | 15.37% | — | 34.42% | **0.38** | 59.71% | 17.23 | +0.61pp |
| full-2nd-half | Buy-and-hold | 12.32% | **12.31%** | 12.32% | 11.74% | 38.24% | **0.32** | 100.00% | 0.00 | — |
| full-2nd-half | **N=30** | 9.79% | **4.79%** | 7.11% | 3.21% | 18.66% | **0.26** | 63.56% | 21.86 | -7.52pp |

### NIFTY 500 TRI

| Window | Strategy | CAGR before costs | **CAGR after costs** | CAGR ex-STT | CAGR after tax | Max DD | **CAGR/DD** | % invested | Switches/yr | Excess vs B&H (after costs) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| full | Buy-and-hold | 13.47% | **13.47%** | 13.47% | — | 67.02% | **0.20** | 100.00% | 0.00 | — |
| full | **N=30** | 17.31% | **12.71%** | 14.84% | — | 26.00% | **0.49** | 62.44% | 17.95 | -0.77pp |
| 2006→ | Buy-and-hold | 12.40% | **12.39%** | 12.40% | 11.99% | 63.68% | **0.19** | 100.00% | 0.00 | — |
| 2006→ | **N=30** | 14.50% | **9.62%** | 11.96% | 7.03% | 23.31% | **0.41** | 64.17% | 19.78 | -2.77pp |
| decade | Buy-and-hold | 13.32% | **13.30%** | 13.31% | 12.47% | 38.08% | **0.35** | 100.00% | 0.00 | — |
| decade | **N=30** | 11.99% | **7.13%** | 9.39% | 5.23% | 19.19% | **0.37** | 66.84% | 20.81 | -6.17pp |
| six-year | Buy-and-hold | 14.25% | **14.23%** | 14.24% | 13.11% | 38.07% | **0.37** | 100.00% | 0.00 | — |
| six-year | **N=30** | 13.54% | **8.92%** | 11.07% | 6.56% | 15.72% | **0.57** | 66.59% | 19.53 | -5.31pp |
| wf-selection | Buy-and-hold | 15.57% | **15.54%** | 15.55% | 14.83% | 38.08% | **0.41** | 100.00% | 0.00 | — |
| wf-selection | **N=30** | 13.58% | **8.69%** | 10.95% | 6.93% | 19.19% | **0.45** | 70.39% | 20.41 | -6.84pp |
| wf-verification | Buy-and-hold | 11.12% | **11.08%** | 11.10% | 10.15% | 18.57% | **0.60** | 100.00% | 0.00 | — |
| wf-verification | **N=30** | 10.44% | **5.56%** | 7.84% | 3.68% | 15.73% | **0.35** | 63.28% | 21.21 | -5.52pp |
| full-1st-half | Buy-and-hold | 15.23% | **15.23%** | 15.23% | — | 67.02% | **0.23** | 100.00% | 0.00 | — |
| full-1st-half | **N=30** | 24.50% | **20.13%** | 22.13% | — | 26.00% | **0.77** | 60.71% | 15.15 | +4.90pp |
| full-2nd-half | Buy-and-hold | 11.74% | **11.73%** | 11.73% | 11.19% | 38.09% | **0.31** | 100.00% | 0.00 | — |
| full-2nd-half | **N=30** | 10.56% | **5.73%** | 7.99% | 4.14% | 19.18% | **0.30** | 64.16% | 20.73 | -6.00pp |

## Is 30 special? Rank of N=30 among the seven N (generated)

Rank 1 = best of 7 (buy-and-hold excluded). After costs.

| Index | Window | Rank by CAGR/DD | Rank by CAGR | Best N by CAGR/DD | Best N by CAGR | Does any N beat B&H on CAGR? | On CAGR/DD? |
| --- | --- | --- | --- | --- | --- | --- | --- |
| NIFTY 50 TRI | full | 4 | 4 | N=100 | N=100 | none | N=20, N=30, N=50, N=100, N=150, N=200 |
| NIFTY 50 TRI | 2006→ | 5 | 5 | N=100 | N=100 | none | N=50, N=100, N=150, N=200 |
| NIFTY 50 TRI | decade | 5 | 5 | N=100 | N=100 | none | N=100, N=200 |
| NIFTY 50 TRI | six-year | 4 | 5 | N=200 | N=100 | none | N=100, N=150, N=200 |
| NIFTY 50 TRI | wf-selection | 4 | 5 | N=50 | N=50 | none | N=30, N=50, N=100, N=200 |
| NIFTY 50 TRI | wf-verification | 4 | 4 | N=200 | N=200 | none | none |
| NIFTY 50 TRI | full-1st-half | 5 | 3 | N=100 | N=100 | N=20, N=30, N=50, N=100, N=150 | N=20, N=30, N=50, N=100, N=150, N=200 |
| NIFTY 50 TRI | full-2nd-half | 5 | 5 | N=200 | N=100 | none | N=100, N=200 |
| NIFTY 500 TRI | full | 2 | 3 | N=20 | N=50 | N=50 | N=10, N=20, N=30, N=50, N=100, N=150, N=200 |
| NIFTY 500 TRI | 2006→ | 3 | 4 | N=150 | N=50 | none | N=20, N=30, N=50, N=100, N=150, N=200 |
| NIFTY 500 TRI | decade | 3 | 5 | N=50 | N=50 | none | N=30, N=50, N=100, N=150 |
| NIFTY 500 TRI | six-year | 3 | 5 | N=100 | N=100 | none | N=30, N=50, N=100, N=150, N=200 |
| NIFTY 500 TRI | wf-selection | 5 | 5 | N=50 | N=50 | none | N=20, N=30, N=50, N=100, N=150, N=200 |
| NIFTY 500 TRI | wf-verification | 1 | 1 | N=30 | N=30 | none | none |
| NIFTY 500 TRI | full-1st-half | 2 | 3 | N=20 | N=20 | N=10, N=20, N=30, N=50, N=100, N=150 | N=10, N=20, N=30, N=50, N=100, N=150, N=200 |
| NIFTY 500 TRI | full-2nd-half | 4 | 5 | N=50 | N=50 | none | N=50, N=100 |
| NIFTY Midcap 150 TRI | full | 1 | 1 | N=30 | N=30 | N=20, N=30, N=50, N=100, N=150, N=200 | N=10, N=20, N=30, N=50, N=100, N=150, N=200 |
| NIFTY Midcap 150 TRI | 2006→ | 1 | 1 | N=30 | N=30 | N=20, N=30, N=50, N=100, N=150, N=200 | N=10, N=20, N=30, N=50, N=100, N=150, N=200 |
| NIFTY Midcap 150 TRI | decade | 1 | 1 | N=30 | N=30 | none | N=20, N=30, N=50, N=100, N=150, N=200 |
| NIFTY Midcap 150 TRI | six-year | 5 | 5 | N=100 | N=100 | none | N=30, N=50, N=100, N=150, N=200 |
| NIFTY Midcap 150 TRI | wf-selection | 1 | 2 | N=30 | N=50 | N=30, N=50 | N=20, N=30, N=50, N=100, N=150, N=200 |
| NIFTY Midcap 150 TRI | wf-verification | 4 | 4 | N=100 | N=150 | none | none |
| NIFTY Midcap 150 TRI | full-1st-half | 2 | 1 | N=20 | N=30 | N=20, N=30, N=50, N=100, N=150, N=200 | N=10, N=20, N=30, N=50, N=100, N=150, N=200 |
| NIFTY Midcap 150 TRI | full-2nd-half | 1 | 1 | N=30 | N=30 | none | N=20, N=30, N=50, N=100, N=150, N=200 |
| NIFTY Smallcap 250 TRI | full | 1 | 3 | N=30 | N=20 | N=10, N=20, N=30, N=50, N=100, N=150, N=200 | N=10, N=20, N=30, N=50, N=100, N=150, N=200 |
| NIFTY Smallcap 250 TRI | 2006→ | 1 | 3 | N=30 | N=20 | N=10, N=20, N=30, N=50, N=100, N=150, N=200 | N=10, N=20, N=30, N=50, N=100, N=150, N=200 |
| NIFTY Smallcap 250 TRI | decade | 5 | 5 | N=100 | N=150 | N=50, N=100, N=150 | N=10, N=20, N=30, N=50, N=100, N=150, N=200 |
| NIFTY Smallcap 250 TRI | six-year | 5 | 5 | N=100 | N=100 | N=100, N=150 | N=10, N=20, N=30, N=50, N=100, N=150, N=200 |
| NIFTY Smallcap 250 TRI | wf-selection | 1 | 3 | N=30 | N=50 | N=10, N=20, N=30, N=50, N=100, N=150, N=200 | N=10, N=20, N=30, N=50, N=100, N=150, N=200 |
| NIFTY Smallcap 250 TRI | wf-verification | 6 | 6 | N=150 | N=150 | none | N=100, N=150 |
| NIFTY Smallcap 250 TRI | full-1st-half | 2 | 2 | N=20 | N=20 | N=10, N=20, N=30, N=50, N=100, N=150, N=200 | N=10, N=20, N=30, N=50, N=100, N=150, N=200 |
| NIFTY Smallcap 250 TRI | full-2nd-half | 5 | 5 | N=100 | N=150 | N=50, N=100, N=150 | N=10, N=20, N=30, N=50, N=100, N=150, N=200 |

## Every N, every window — NIFTY 50 and NIFTY 500 (ranked by CAGR/DD after costs)

### NIFTY 50 TRI

**full (2000-04-17 → 2026-10-13)** — whole published history (pre-2006 costs and cash on proxies)

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | N=100 | 11.74% | 13.91% | 25.15% | **0.47** | 14.03% | 0.45 | 64.40% | 8.95 | 2010 (-16.41pp, 12 sw) | -0.70pp | — | ₹25.81L (₹23.02L) | ₹22.44L |
| 2 | N=150 | 10.56% | 12.43% | 26.26% | **0.40** | 14.56% | 0.37 | 67.33% | 7.59 | 2007 (-17.39pp, 12 sw) | -1.87pp | — | ₹13.93L (₹12.40L) | ₹11.71L |
| 3 | N=50 | 9.46% | 12.73% | 32.97% | **0.29** | 13.81% | 0.30 | 62.79% | 13.63 | 2009 (-35.94pp, 22 sw) | -2.98pp | — | ₹25.55L (₹22.77L) | ₹22.01L |
| 4 | **N=30** | 8.89% | 13.61% | 34.42% | **0.26** | 13.59% | 0.27 | 61.64% | 19.59 | 2007 (-40.30pp, 22 sw) | -3.55pp | — | ₹34.58L (₹30.79L) | ₹29.48L |
| 5 | N=200 | 8.70% | 10.38% | 37.10% | **0.23** | 15.24% | 0.24 | 70.55% | 7.06 | 2009 (-32.46pp, 5 sw) | -3.74pp | — | ₹8.60L (₹7.64L) | ₹7.38L |
| 6 | N=20 | 7.23% | 12.99% | 31.48% | **0.23** | 13.55% | 0.15 | 60.80% | 24.05 | 2003 (-37.43pp, 30 sw) | -5.21pp | — | ₹34.64L (₹30.82L) | ₹29.23L |
| 7 | buy-and-hold | 12.44% | 12.44% | 59.48% | **0.21** | 21.24% | 0.39 | 100.00% | 0.00 | — | — | — | ₹0.01L (₹0.01L) | ₹0.01L |
| 8 | N=10 | 1.43% | 10.17% | 60.29% | **0.02** | 13.38% | -0.27 | 58.95% | 38.09 | 2009 (-69.76pp, 44 sw) | -11.01pp | — | ₹20.82L (₹18.47L) | ₹17.22L |

**2006→ (2006-01-01 → 2026-10-13)** — span every model covers (after-tax available)

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | N=100 | 10.25% | 12.59% | 22.83% | **0.45** | 13.79% | 0.36 | 67.11% | 9.67 | 2010 (-16.41pp, 12 sw) | -1.61pp | 7.82% | ₹10.17L (₹9.07L) | ₹8.83L |
| 2 | N=150 | 9.07% | 11.04% | 26.27% | **0.35** | 14.49% | 0.27 | 70.62% | 8.04 | 2007 (-17.38pp, 12 sw) | -2.78pp | 7.60% | ₹5.76L (₹5.12L) | ₹4.83L |
| 3 | N=200 | 9.02% | 10.80% | 29.79% | **0.30** | 15.01% | 0.26 | 74.41% | 7.36 | 2009 (-32.41pp, 5 sw) | -2.83pp | 8.19% | ₹5.48L (₹4.86L) | ₹4.70L |
| 4 | N=50 | 7.86% | 11.34% | 29.36% | **0.27** | 13.59% | 0.20 | 64.95% | 14.49 | 2009 (-35.89pp, 22 sw) | -4.00pp | 5.75% | ₹10.94L (₹9.74L) | ₹9.41L |
| 5 | buy-and-hold | 11.85% | 11.86% | 59.46% | **0.20** | 20.78% | 0.37 | 100.00% | 0.00 | — | — | 11.47% | ₹0.01L (₹0.01L) | ₹0.01L |
| 6 | **N=30** | 6.44% | 11.40% | 34.42% | **0.19** | 13.39% | 0.10 | 62.54% | 20.74 | 2007 (-40.28pp, 22 sw) | -5.42pp | 4.45% | ₹12.80L (₹11.39L) | ₹10.88L |
| 7 | N=20 | 4.93% | 10.96% | 31.49% | **0.16** | 13.32% | -0.01 | 61.24% | 25.36 | 2007 (-34.84pp, 22 sw) | -6.92pp | 2.90% | ₹14.17L (₹12.60L) | ₹11.92L |
| 8 | N=10 | -1.57% | 7.33% | 60.38% | **-0.03** | 13.11% | -0.51 | 59.18% | 39.23 | 2009 (-69.73pp, 44 sw) | -13.43pp | -2.47% | ₹8.94L (₹7.92L) | ₹7.30L |

**decade (2016-09-01 → 2026-08-31)** — owner mandate window

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | N=100 | 8.12% | 10.73% | 18.83% | **0.43** | 10.55% | 0.32 | 71.41% | 11.20 | 2019 (-11.13pp, 13 sw) | -3.82pp | 6.38% | ₹2.08L (₹1.85L) | ₹1.85L |
| 2 | N=200 | 7.32% | 9.12% | 21.09% | **0.35** | 11.48% | 0.24 | 79.48% | 7.70 | 2019 (-13.95pp, 16 sw) | -4.61pp | 6.58% | ₹1.17L (₹1.03L) | ₹1.03L |
| 3 | buy-and-hold | 11.94% | 11.95% | 38.24% | **0.31** | 16.17% | 0.47 | 100.00% | 0.00 | — | — | 11.16% | ₹0.01L (₹0.01L) | ₹0.01L |
| 4 | N=50 | 6.29% | 10.13% | 20.77% | **0.30** | 10.28% | 0.16 | 67.12% | 16.61 | 2022 (-16.50pp, 24 sw) | -5.64pp | 4.47% | ₹2.95L (₹2.62L) | ₹2.62L |
| 5 | N=150 | 6.60% | 8.04% | 24.06% | **0.27** | 11.14% | 0.18 | 75.32% | 6.20 | 2018 (-16.77pp, 12 sw) | -5.34pp | 5.86% | ₹0.94L (₹0.83L) | ₹0.83L |
| 6 | **N=30** | 4.92% | 10.08% | 18.67% | **0.26** | 10.35% | 0.03 | 64.70% | 22.51 | 2021 (-19.23pp, 25 sw) | -7.02pp | 3.27% | ₹3.58L (₹3.17L) | ₹3.17L |
| 7 | N=20 | 2.56% | 8.24% | 21.21% | **0.12** | 10.27% | -0.20 | 62.76% | 25.31 | 2021 (-29.12pp, 38 sw) | -9.38pp | 1.13% | ₹3.55L (₹3.15L) | ₹3.15L |
| 8 | N=10 | -1.14% | 7.73% | 28.88% | **-0.04** | 9.98% | -0.58 | 59.94% | 40.32 | 2021 (-29.63pp, 48 sw) | -13.07pp | -2.13% | ₹4.51L (₹3.99L) | ₹3.99L |

**six-year (2019-07-01 → 2026-08-31)** — owner mandate window

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | N=200 | 8.84% | 10.19% | 13.13% | **0.67** | 11.81% | 0.38 | 76.27% | 5.72 | 2022 (-6.46pp, 12 sw) | -2.83pp | 7.78% | ₹0.67L (₹0.60L) | ₹0.60L |
| 2 | N=150 | 8.57% | 9.82% | 15.07% | **0.57** | 11.54% | 0.36 | 73.79% | 5.30 | 2022 (-6.92pp, 10 sw) | -3.09pp | 7.47% | ₹0.63L (₹0.56L) | ₹0.56L |
| 3 | N=100 | 9.20% | 11.83% | 18.83% | **0.49** | 10.98% | 0.43 | 69.29% | 11.16 | 2021 (-8.39pp, 9 sw) | -2.46pp | 7.20% | ₹1.44L (₹1.29L) | ₹1.29L |
| 4 | buy-and-hold | 11.66% | 11.68% | 38.23% | **0.31** | 17.61% | 0.45 | 100.00% | 0.00 | — | — | 10.70% | ₹0.01L (₹0.01L) | ₹0.01L |
| 5 | **N=30** | 5.09% | 10.17% | 16.74% | **0.30** | 11.04% | 0.07 | 63.16% | 22.18 | 2021 (-19.23pp, 25 sw) | -6.57pp | 3.33% | ₹2.43L (₹2.16L) | ₹2.16L |
| 6 | N=50 | 5.98% | 9.97% | 20.77% | **0.29** | 10.86% | 0.15 | 64.79% | 17.30 | 2022 (-16.50pp, 24 sw) | -5.68pp | 4.10% | ₹1.98L (₹1.77L) | ₹1.77L |
| 7 | N=20 | 2.40% | 8.18% | 21.21% | **0.11** | 10.98% | -0.17 | 61.64% | 25.81 | 2021 (-29.11pp, 38 sw) | -9.26pp | 0.88% | ₹2.50L (₹2.22L) | ₹2.22L |
| 8 | N=10 | -1.64% | 7.29% | 22.61% | **-0.07** | 10.59% | -0.57 | 58.21% | 40.88 | 2021 (-29.63pp, 48 sw) | -13.30pp | -2.63% | ₹3.23L (₹2.87L) | ₹2.87L |

**wf-selection (2016-09-01 → 2021-08-31)** — owner mandate window

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | N=50 | 12.79% | 16.74% | 12.80% | **1.00** | 10.50% | 0.74 | 72.01% | 16.01 | 2021 (-15.32pp, 18 sw) | -2.89pp | 10.52% | ₹1.17L (₹1.03L) | ₹1.03L |
| 2 | N=100 | 12.72% | 14.89% | 17.92% | **0.71** | 11.15% | 0.70 | 76.38% | 8.81 | 2019 (-11.13pp, 13 sw) | -2.96pp | 10.73% | ₹0.59L (₹0.52L) | ₹0.52L |
| 3 | N=200 | 9.45% | 11.58% | 21.09% | **0.45** | 11.83% | 0.41 | 82.77% | 8.81 | 2019 (-13.95pp, 16 sw) | -6.22pp | 8.85% | ₹0.53L (₹0.46L) | ₹0.46L |
| 4 | **N=30** | 7.98% | 13.12% | 18.67% | **0.43** | 10.93% | 0.31 | 69.42% | 21.61 | 2017 (-18.91pp, 22 sw) | -7.70pp | 6.24% | ₹1.41L (₹1.24L) | ₹1.24L |
| 5 | buy-and-hold | 15.68% | 15.71% | 38.24% | **0.41** | 18.26% | 0.63 | 100.00% | 0.00 | — | — | 14.92% | ₹0.01L (₹0.01L) | ₹0.01L |
| 6 | N=150 | 8.67% | 10.21% | 24.06% | **0.36** | 11.63% | 0.36 | 77.83% | 6.40 | 2018 (-16.77pp, 12 sw) | -7.01pp | 8.06% | ₹0.38L (₹0.33L) | ₹0.33L |
| 7 | N=20 | 5.75% | 11.93% | 19.12% | **0.30** | 10.62% | 0.12 | 66.75% | 26.42 | 2021 (-26.35pp, 28 sw) | -9.93pp | 4.23% | ₹1.68L (₹1.48L) | ₹1.48L |
| 8 | N=10 | 0.22% | 9.40% | 28.88% | **0.01** | 10.77% | -0.39 | 63.92% | 40.83 | 2019 (-27.77pp, 48 sw) | -15.46pp | -0.70% | ₹2.26L (₹1.98L) | ₹1.98L |

**wf-verification (2021-09-01 → 2026-08-31)** — owner mandate window

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | buy-and-hold | 8.34% | 8.38% | 16.43% | **0.51** | 13.79% | 0.28 | 100.00% | 0.00 | — | — | 7.67% | ₹0.01L (₹0.01L) | ₹0.01L |
| 2 | N=200 | 5.26% | 6.79% | 13.13% | **0.40** | 11.13% | 0.05 | 76.19% | 6.60 | 2022 (-6.46pp, 12 sw) | -3.08pp | 4.57% | ₹0.42L (₹0.38L) | ₹0.38L |
| 3 | N=150 | 4.60% | 5.98% | 15.08% | **0.31** | 10.63% | -0.01 | 72.80% | 6.00 | 2022 (-6.92pp, 10 sw) | -3.74pp | 3.90% | ₹0.38L (₹0.34L) | ₹0.34L |
| 4 | N=100 | 3.73% | 6.80% | 18.83% | **0.20** | 9.92% | -0.10 | 66.42% | 13.61 | 2023 (-7.12pp, 18 sw) | -4.62pp | 2.59% | ₹0.83L (₹0.74L) | ₹0.74L |
| 5 | **N=30** | 1.97% | 7.19% | 16.75% | **0.12** | 9.74% | -0.29 | 59.97% | 23.42 | 2025 (-14.47pp, 24 sw) | -6.38pp | 0.64% | ₹1.49L (₹1.33L) | ₹1.33L |
| 6 | N=50 | 0.19% | 3.96% | 20.78% | **0.01** | 10.05% | -0.45 | 62.23% | 17.21 | 2022 (-16.50pp, 24 sw) | -8.16pp | -0.71% | ₹0.99L (₹0.88L) | ₹0.88L |
| 7 | N=20 | -0.52% | 4.75% | 21.22% | **-0.02** | 9.90% | -0.53 | 58.76% | 24.22 | 2025 (-18.45pp, 26 sw) | -8.86pp | -1.65% | ₹1.43L (₹1.27L) | ₹1.27L |
| 8 | N=10 | -2.46% | 6.16% | 19.98% | **-0.12** | 9.12% | -0.81 | 55.93% | 39.83 | 2023 (-16.20pp, 38 sw) | -10.81pp | -3.47% | ₹2.24L (₹2.00L) | ₹2.00L |

**full-1st-half (2000-04-17 → 2013-06-21)** — long walk-forward: selection

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | N=100 | 15.74% | 17.53% | 25.15% | **0.63** | 16.61% | 0.60 | 58.77% | 6.91 | 2010 (-16.41pp, 12 sw) | +3.20pp | — | ₹4.68L (₹4.18L) | ₹3.60L |
| 2 | N=150 | 13.60% | 15.75% | 26.26% | **0.52** | 17.31% | 0.47 | 61.48% | 8.27 | 2007 (-17.39pp, 12 sw) | +1.06pp | — | ₹4.63L (₹4.15L) | ₹3.46L |
| 3 | N=20 | 12.84% | 18.62% | 31.48% | **0.41** | 16.12% | 0.45 | 59.71% | 22.39 | 2003 (-37.43pp, 30 sw) | +0.30pp | — | ₹12.06L (₹10.78L) | ₹9.19L |
| 4 | N=50 | 12.73% | 15.68% | 32.97% | **0.39** | 16.52% | 0.44 | 59.14% | 11.61 | 2009 (-35.94pp, 22 sw) | +0.19pp | — | ₹5.55L (₹4.96L) | ₹4.21L |
| 5 | **N=30** | 13.15% | 17.56% | 34.42% | **0.38** | 16.13% | 0.47 | 59.71% | 17.23 | 2007 (-40.30pp, 22 sw) | +0.61pp | — | ₹9.55L (₹8.55L) | ₹7.23L |
| 6 | N=200 | 9.93% | 11.42% | 37.10% | **0.27** | 18.27% | 0.28 | 64.42% | 6.00 | 2009 (-32.46pp, 5 sw) | -2.61pp | — | ₹2.01L (₹1.79L) | ₹1.54L |
| 7 | buy-and-hold | 12.54% | 12.55% | 59.48% | **0.21** | 25.36% | 0.35 | 100.00% | 0.00 | — | — | — | ₹0.01L (₹0.01L) | ₹0.01L |
| 8 | N=10 | 4.65% | 13.44% | 47.65% | **0.10** | 15.87% | -0.02 | 58.38% | 36.35 | 2009 (-69.76pp, 44 sw) | -7.89pp | — | ₹11.02L (₹9.81L) | ₹8.55L |

**full-2nd-half (2013-06-21 → 2026-10-13)** — long walk-forward: verification

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | N=200 | 7.48% | 9.35% | 21.09% | **0.35** | 11.45% | 0.21 | 76.68% | 8.04 | 2019 (-13.95pp, 16 sw) | -4.83pp | 6.79% | ₹1.90L (₹1.68L) | ₹1.68L |
| 2 | N=100 | 7.89% | 10.42% | 22.83% | **0.35** | 10.84% | 0.25 | 70.03% | 10.89 | 2015 (-13.19pp, 15 sw) | -4.43pp | 6.15% | ₹3.08L (₹2.73L) | ₹2.73L |
| 3 | buy-and-hold | 12.31% | 12.32% | 38.24% | **0.32** | 16.10% | 0.47 | 100.00% | 0.00 | — | — | 11.74% | ₹0.01L (₹0.01L) | ₹0.01L |
| 4 | N=150 | 7.63% | 9.22% | 24.06% | **0.32** | 11.16% | 0.22 | 73.19% | 6.84 | 2018 (-16.76pp, 12 sw) | -4.69pp | 6.85% | ₹1.73L (₹1.53L) | ₹1.53L |
| 5 | N=50 | 6.29% | 9.88% | 22.10% | **0.28** | 10.43% | 0.11 | 66.44% | 15.55 | 2022 (-16.50pp, 24 sw) | -6.02pp | 4.53% | ₹4.12L (₹3.66L) | ₹3.66L |
| 6 | **N=30** | 4.79% | 9.79% | 18.66% | **0.26** | 10.43% | -0.03 | 63.56% | 21.86 | 2021 (-19.23pp, 25 sw) | -7.52pp | 3.21% | ₹4.91L (₹4.35L) | ₹4.35L |
| 7 | N=20 | 1.91% | 7.64% | 23.00% | **0.08** | 10.36% | -0.30 | 61.89% | 25.62 | 2021 (-29.12pp, 38 sw) | -10.40pp | 0.65% | ₹4.59L (₹4.06L) | ₹4.06L |
| 8 | N=10 | -1.70% | 7.00% | 36.44% | **-0.05** | 10.29% | -0.66 | 59.52% | 39.74 | 2021 (-29.64pp, 48 sw) | -14.01pp | -2.52% | ₹5.38L (₹4.74L) | ₹4.74L |

### NIFTY 500 TRI

**full (1995-11-09 → 2026-10-13)** — whole published history (pre-2006 costs and cash on proxies)

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | N=20 | 12.63% | 18.50% | 23.40% | **0.54** | 14.26% | 0.50 | 60.85% | 22.80 | 2003 (-51.28pp, 24 sw) | -0.84pp | — | ₹156.38L (₹139.26L) | ₹134.18L |
| 2 | **N=30** | 12.71% | 17.31% | 26.00% | **0.49** | 14.59% | 0.50 | 62.44% | 17.95 | 2009 (-36.66pp, 18 sw) | -0.77pp | — | ₹113.48L (₹101.13L) | ₹97.49L |
| 3 | N=50 | 14.17% | 17.18% | 43.32% | **0.33** | 15.00% | 0.58 | 63.32% | 11.67 | 2009 (-31.95pp, 10 sw) | +0.69pp | — | ₹91.14L (₹81.24L) | ₹78.98L |
| 4 | N=150 | 11.81% | 13.60% | 43.50% | **0.27** | 15.89% | 0.42 | 66.24% | 7.08 | 1997 (-33.51pp, 14 sw) | -1.66pp | — | ₹32.63L (₹29.10L) | ₹27.99L |
| 5 | N=200 | 11.01% | 12.50% | 42.38% | **0.26** | 16.07% | 0.37 | 67.92% | 5.98 | 2009 (-33.38pp, 5 sw) | -2.47pp | — | ₹22.18L (₹19.74L) | ₹19.05L |
| 6 | N=100 | 13.45% | 15.59% | 53.30% | **0.25** | 15.43% | 0.53 | 64.26% | 8.50 | 2010 (-26.69pp, 22 sw) | -0.02pp | — | ₹60.16L (₹53.60L) | ₹51.69L |
| 7 | N=10 | 8.28% | 17.21% | 39.03% | **0.21** | 14.36% | 0.22 | 59.36% | 35.66 | 1999 (-58.59pp, 32 sw) | -5.19pp | — | ₹114.28L (₹101.71L) | ₹96.11L |
| 8 | buy-and-hold | 13.47% | 13.47% | 67.02% | **0.20** | 22.10% | 0.42 | 100.00% | 0.00 | — | — | — | ₹0.01L (₹0.01L) | ₹0.01L |

**2006→ (2006-01-01 → 2026-10-13)** — span every model covers (after-tax available)

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | N=150 | 10.88% | 12.82% | 24.85% | **0.44** | 14.09% | 0.39 | 69.28% | 7.85 | 2012 (-21.29pp, 13 sw) | -1.52pp | 9.47% | ₹8.64L (₹7.70L) | ₹7.40L |
| 2 | N=100 | 11.04% | 13.43% | 26.72% | **0.41** | 13.53% | 0.42 | 66.47% | 9.67 | 2010 (-26.68pp, 22 sw) | -1.35pp | 8.84% | ₹10.42L (₹9.27L) | ₹8.93L |
| 3 | **N=30** | 9.62% | 14.50% | 23.31% | **0.41** | 13.01% | 0.33 | 64.17% | 19.78 | 2009 (-36.59pp, 18 sw) | -2.77pp | 7.03% | ₹18.39L (₹16.37L) | ₹15.76L |
| 4 | N=20 | 8.80% | 14.74% | 23.41% | **0.38** | 12.90% | 0.27 | 62.45% | 24.21 | 2007 (-32.84pp, 22 sw) | -3.59pp | 6.01% | ₹21.95L (₹19.53L) | ₹18.79L |
| 5 | N=50 | 11.13% | 14.07% | 36.94% | **0.30** | 13.52% | 0.42 | 66.08% | 11.89 | 2009 (-31.88pp, 10 sw) | -1.27pp | 8.48% | ₹13.11L (₹11.67L) | ₹11.34L |
| 6 | N=200 | 9.36% | 10.94% | 33.09% | **0.28** | 14.57% | 0.29 | 72.62% | 6.50 | 2009 (-33.30pp, 5 sw) | -3.03pp | 8.42% | ₹5.45L (₹4.85L) | ₹4.67L |
| 7 | buy-and-hold | 12.39% | 12.40% | 63.68% | **0.19** | 20.25% | 0.40 | 100.00% | 0.00 | — | — | 11.99% | ₹0.01L (₹0.01L) | ₹0.01L |
| 8 | N=10 | 2.17% | 11.16% | 39.15% | **0.06** | 13.02% | -0.22 | 60.12% | 38.36 | 2009 (-49.68pp, 32 sw) | -10.23pp | 0.66% | ₹13.96L (₹12.39L) | ₹11.65L |

**decade (2016-09-01 → 2026-08-31)** — owner mandate window

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | N=50 | 8.81% | 11.97% | 18.87% | **0.47** | 10.57% | 0.38 | 69.10% | 13.41 | 2019 (-12.33pp, 14 sw) | -4.49pp | 6.76% | ₹2.48L (₹2.21L) | ₹2.21L |
| 2 | N=150 | 8.19% | 9.88% | 21.82% | **0.38** | 11.12% | 0.31 | 73.26% | 7.20 | 2018 (-12.98pp, 21 sw) | -5.11pp | 7.19% | ₹1.32L (₹1.17L) | ₹1.17L |
| 3 | **N=30** | 7.13% | 11.99% | 19.19% | **0.37** | 10.47% | 0.23 | 66.84% | 20.81 | 2021 (-19.88pp, 17 sw) | -6.17pp | 5.23% | ₹3.56L (₹3.16L) | ₹3.16L |
| 4 | N=100 | 8.62% | 11.03% | 23.55% | **0.37** | 10.82% | 0.36 | 71.04% | 10.20 | 2019 (-12.22pp, 19 sw) | -4.68pp | 7.11% | ₹1.76L (₹1.56L) | ₹1.56L |
| 5 | buy-and-hold | 13.30% | 13.32% | 38.08% | **0.35** | 16.17% | 0.55 | 100.00% | 0.00 | — | — | 12.47% | ₹0.01L (₹0.01L) | ₹0.01L |
| 6 | N=200 | 7.79% | 9.33% | 24.52% | **0.32** | 11.55% | 0.27 | 77.83% | 6.60 | 2019 (-13.74pp, 11 sw) | -5.52pp | 6.90% | ₹1.11L (₹0.98L) | ₹0.98L |
| 7 | N=20 | 5.10% | 11.06% | 18.87% | **0.27** | 10.28% | 0.04 | 64.26% | 25.91 | 2021 (-26.04pp, 28 sw) | -8.20pp | 3.30% | ₹4.05L (₹3.59L) | ₹3.59L |
| 8 | N=10 | 1.41% | 10.75% | 26.82% | **0.05** | 9.87% | -0.33 | 61.19% | 41.42 | 2021 (-25.48pp, 40 sw) | -11.90pp | 0.08% | ₹5.18L (₹4.58L) | ₹4.58L |

**six-year (2019-07-01 → 2026-08-31)** — owner mandate window

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | N=100 | 11.14% | 13.28% | 17.23% | **0.65** | 11.29% | 0.58 | 70.70% | 8.93 | 2026 (-10.64pp, 14 sw) | -3.10pp | 8.90% | ₹1.21L (₹1.08L) | ₹1.08L |
| 2 | N=50 | 10.90% | 13.70% | 18.87% | **0.58** | 11.16% | 0.56 | 68.50% | 11.72 | 2021 (-12.18pp, 13 sw) | -3.33pp | 8.34% | ₹1.67L (₹1.49L) | ₹1.49L |
| 3 | **N=30** | 8.92% | 13.54% | 15.72% | **0.57** | 11.16% | 0.39 | 66.59% | 19.53 | 2021 (-19.87pp, 17 sw) | -5.31pp | 6.56% | ₹2.52L (₹2.25L) | ₹2.25L |
| 4 | N=150 | 10.00% | 11.53% | 18.18% | **0.55** | 11.54% | 0.48 | 73.40% | 6.42 | 2026 (-9.65pp, 12 sw) | -4.23pp | 8.60% | ₹0.91L (₹0.81L) | ₹0.81L |
| 5 | N=200 | 9.38% | 10.64% | 17.50% | **0.54** | 11.97% | 0.41 | 76.77% | 5.30 | 2026 (-11.85pp, 12 sw) | -4.85pp | 8.10% | ₹0.68L (₹0.61L) | ₹0.61L |
| 6 | buy-and-hold | 14.23% | 14.25% | 38.07% | **0.37** | 17.44% | 0.58 | 100.00% | 0.00 | — | — | 13.11% | ₹0.01L (₹0.01L) | ₹0.01L |
| 7 | N=20 | 5.42% | 11.37% | 18.87% | **0.29** | 10.95% | 0.10 | 64.06% | 25.81 | 2021 (-26.04pp, 28 sw) | -8.82pp | 3.44% | ₹2.80L (₹2.49L) | ₹2.49L |
| 8 | N=10 | 2.18% | 11.61% | 21.24% | **0.10** | 10.47% | -0.20 | 61.08% | 41.58 | 2021 (-25.47pp, 40 sw) | -12.06pp | 0.64% | ₹3.89L (₹3.47L) | ₹3.47L |

**wf-selection (2016-09-01 → 2021-08-31)** — owner mandate window

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | N=50 | 12.56% | 16.30% | 18.00% | **0.70** | 10.60% | 0.72 | 72.41% | 15.21 | 2021 (-12.44pp, 12 sw) | -2.98pp | 10.34% | ₹1.03L (₹0.91L) | ₹0.91L |
| 2 | N=150 | 10.97% | 12.54% | 21.82% | **0.50** | 11.29% | 0.55 | 73.79% | 6.40 | 2018 (-12.98pp, 21 sw) | -4.57pp | 10.18% | ₹0.40L (₹0.35L) | ₹0.35L |
| 3 | N=20 | 7.97% | 14.46% | 15.90% | **0.50** | 10.42% | 0.32 | 67.39% | 27.22 | 2021 (-19.30pp, 18 sw) | -7.57pp | 6.17% | ₹1.73L (₹1.51L) | ₹1.51L |
| 4 | N=100 | 11.79% | 14.35% | 23.55% | **0.50** | 11.01% | 0.63 | 73.46% | 10.41 | 2019 (-12.22pp, 19 sw) | -3.74pp | 10.94% | ₹0.64L (₹0.56L) | ₹0.56L |
| 5 | **N=30** | 8.69% | 13.58% | 19.19% | **0.45** | 10.74% | 0.38 | 70.39% | 20.41 | 2017 (-19.31pp, 17 sw) | -6.84pp | 6.93% | ₹1.28L (₹1.12L) | ₹1.12L |
| 6 | N=200 | 10.23% | 11.98% | 24.52% | **0.42** | 11.49% | 0.49 | 77.91% | 7.20 | 2019 (-13.74pp, 11 sw) | -5.31pp | 9.59% | ₹0.46L (₹0.40L) | ₹0.40L |
| 7 | buy-and-hold | 15.54% | 15.57% | 38.08% | **0.41** | 17.75% | 0.64 | 100.00% | 0.00 | — | — | 14.83% | ₹0.01L (₹0.01L) | ₹0.01L |
| 8 | N=10 | 2.69% | 12.27% | 26.82% | **0.10** | 10.37% | -0.17 | 62.94% | 41.63 | 2017 (-25.47pp, 38 sw) | -12.85pp | 1.40% | ₹2.33L (₹2.04L) | ₹2.04L |

**wf-verification (2021-09-01 → 2026-08-31)** — owner mandate window

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | buy-and-hold | 11.08% | 11.12% | 18.57% | **0.60** | 14.43% | 0.45 | 100.00% | 0.00 | — | — | 10.15% | ₹0.01L (₹0.01L) | ₹0.01L |
| 2 | **N=30** | 5.56% | 10.44% | 15.73% | **0.35** | 10.21% | 0.08 | 63.28% | 21.21 | 2025 (-10.29pp, 26 sw) | -5.52pp | 3.68% | ₹1.51L (₹1.35L) | ₹1.35L |
| 3 | N=100 | 5.51% | 7.81% | 17.23% | **0.32** | 10.64% | 0.07 | 68.60% | 10.01 | 2026 (-10.64pp, 14 sw) | -5.57pp | 3.66% | ₹0.65L (₹0.58L) | ₹0.58L |
| 4 | N=200 | 5.37% | 6.76% | 17.50% | **0.31** | 11.61% | 0.06 | 77.72% | 6.00 | 2026 (-11.85pp, 12 sw) | -5.71pp | 4.46% | ₹0.41L (₹0.37L) | ₹0.37L |
| 5 | N=150 | 5.45% | 7.30% | 18.18% | **0.30** | 10.95% | 0.07 | 72.72% | 8.01 | 2026 (-9.65pp, 12 sw) | -5.63pp | 4.50% | ₹0.56L (₹0.50L) | ₹0.50L |
| 6 | N=50 | 5.15% | 7.81% | 18.88% | **0.27** | 10.54% | 0.04 | 65.78% | 11.61 | 2022 (-11.29pp, 12 sw) | -5.93pp | 3.60% | ₹0.81L (₹0.73L) | ₹0.73L |
| 7 | N=20 | 2.27% | 7.78% | 18.88% | **0.12** | 10.15% | -0.24 | 61.10% | 24.62 | 2025 (-14.59pp, 30 sw) | -8.81pp | 0.74% | ₹1.59L (₹1.42L) | ₹1.42L |
| 8 | N=10 | 0.10% | 9.26% | 21.24% | **0.00** | 9.35% | -0.50 | 59.40% | 41.23 | 2023 (-15.70pp, 38 sw) | -10.98pp | -1.18% | ₹2.50L (₹2.23L) | ₹2.23L |

**full-1st-half (1995-11-09 → 2011-04-20)** — long walk-forward: selection

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | N=20 | 21.17% | 27.04% | 23.40% | **0.90** | 17.27% | 0.87 | 59.64% | 20.20 | 2003 (-51.28pp, 24 sw) | +5.94pp | — | ₹26.21L (₹23.46L) | ₹19.87L |
| 2 | **N=30** | 20.13% | 24.50% | 26.00% | **0.77** | 17.71% | 0.80 | 60.71% | 15.15 | 2009 (-36.66pp, 18 sw) | +4.90pp | — | ₹18.44L (₹16.52L) | ₹13.87L |
| 3 | N=50 | 20.42% | 23.45% | 43.32% | **0.47** | 18.25% | 0.80 | 60.11% | 10.62 | 2009 (-31.95pp, 10 sw) | +5.19pp | — | ₹11.05L (₹9.87L) | ₹8.47L |
| 4 | N=10 | 17.14% | 25.86% | 37.98% | **0.45** | 17.53% | 0.66 | 59.43% | 30.89 | 1999 (-58.59pp, 32 sw) | +1.91pp | — | ₹34.79L (₹31.09L) | ₹26.80L |
| 5 | N=150 | 16.53% | 18.16% | 43.50% | **0.38** | 19.49% | 0.59 | 62.58% | 5.89 | 1997 (-33.51pp, 14 sw) | +1.30pp | — | ₹4.19L (₹3.76L) | ₹3.14L |
| 6 | N=100 | 19.60% | 21.38% | 53.30% | **0.37** | 18.91% | 0.74 | 60.58% | 6.47 | 2010 (-26.69pp, 22 sw) | +4.37pp | — | ₹8.98L (₹8.05L) | ₹6.76L |
| 7 | N=200 | 14.88% | 16.24% | 42.38% | **0.35** | 19.64% | 0.51 | 63.13% | 4.99 | 2009 (-33.38pp, 5 sw) | -0.35pp | — | ₹3.12L (₹2.79L) | ₹2.37L |
| 8 | buy-and-hold | 15.23% | 15.23% | 67.02% | **0.23** | 26.81% | 0.45 | 100.00% | 0.00 | — | — | — | ₹0.01L (₹0.01L) | ₹0.01L |

**full-2nd-half (2011-04-20 → 2026-10-13)** — long walk-forward: verification

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | N=50 | 8.23% | 11.25% | 21.41% | **0.38** | 10.80% | 0.26 | 66.54% | 12.72 | 2012 (-16.56pp, 17 sw) | -3.50pp | 6.32% | ₹4.55L (₹4.04L) | ₹3.99L |
| 2 | N=100 | 7.60% | 10.09% | 23.54% | **0.32** | 10.89% | 0.20 | 67.94% | 10.53 | 2012 (-17.02pp, 7 sw) | -4.12pp | 6.39% | ₹3.24L (₹2.87L) | ₹2.83L |
| 3 | buy-and-hold | 11.73% | 11.74% | 38.09% | **0.31** | 16.07% | 0.42 | 100.00% | 0.00 | — | — | 11.19% | ₹0.01L (₹0.01L) | ₹0.01L |
| 4 | N=150 | 7.28% | 9.21% | 23.67% | **0.31** | 11.20% | 0.17 | 69.90% | 8.20 | 2012 (-21.29pp, 13 sw) | -4.45pp | 6.36% | ₹2.68L (₹2.38L) | ₹2.33L |
| 5 | **N=30** | 5.73% | 10.56% | 19.18% | **0.30** | 10.57% | 0.03 | 64.16% | 20.73 | 2014 (-23.43pp, 23 sw) | -6.00pp | 4.14% | ₹5.60L (₹4.96L) | ₹4.90L |
| 6 | N=200 | 7.26% | 8.88% | 24.52% | **0.30** | 11.42% | 0.17 | 72.71% | 6.91 | 2012 (-20.13pp, 13 sw) | -4.47pp | 6.38% | ₹2.24L (₹1.98L) | ₹1.95L |
| 7 | N=20 | 4.68% | 10.56% | 18.87% | **0.25** | 10.40% | -0.06 | 62.05% | 25.38 | 2021 (-26.04pp, 28 sw) | -7.04pp | 3.03% | ₹6.71L (₹5.94L) | ₹5.86L |
| 8 | N=10 | 0.08% | 9.17% | 32.34% | **0.00** | 10.23% | -0.52 | 59.29% | 40.37 | 2021 (-25.49pp, 40 sw) | -11.65pp | -0.97% | ₹6.91L (₹6.10L) | ₹5.99L |

## Walk-forward — choose N on the first half, verify on the second (generated)

The choice is the N (daily, no band) with the best after-cost CAGR/DD on the selection window, made in code before the verification window is read.

| Index | Split | Chosen N (selection CAGR/DD) | Verification: chosen N CAGR / DD / ratio | Verification: N=30 | Verification: buy-and-hold |
| --- | --- | --- | --- | --- | --- |
| NIFTY 50 TRI | 2016-09-01..2021-08-31 → 2021-09-01..2026-08-31 | N=50 (1.00) | 0.19% / 20.78% / **0.01** | 1.97% / 16.75% / **0.12** | 8.34% / 16.43% / **0.51** |
| NIFTY 50 TRI | 2000-04-17..2013-06-21 → 2013-06-21..2026-10-13 | N=100 (0.63) | 7.89% / 22.83% / **0.35** | 4.79% / 18.66% / **0.26** | 12.31% / 38.24% / **0.32** |
| NIFTY 500 TRI | 2016-09-01..2021-08-31 → 2021-09-01..2026-08-31 | N=50 (0.70) | 5.15% / 18.88% / **0.27** | 5.56% / 15.73% / **0.35** | 11.08% / 18.57% / **0.60** |
| NIFTY 500 TRI | 1995-11-09..2011-04-20 → 2011-04-20..2026-10-13 | N=20 (0.90) | 4.68% / 18.87% / **0.25** | 5.73% / 19.18% / **0.30** | 11.73% / 38.09% / **0.31** |
| NIFTY Midcap 150 TRI | 2016-09-01..2021-08-31 → 2021-09-01..2026-08-31 | N=30 (1.41) | 10.81% / 18.40% / **0.59** | 10.81% / 18.40% / **0.59** | 17.63% / 21.10% / **0.84** |
| NIFTY Midcap 150 TRI | 2006-01-17..2016-05-26 → 2016-05-26..2026-10-01 | N=20 (1.31) | 9.58% / 20.59% / **0.47** | 15.25% / 18.39% / **0.83** | 17.62% / 43.02% / **0.41** |
| NIFTY Smallcap 250 TRI | 2016-09-01..2021-08-31 → 2021-09-01..2026-08-31 | N=30 (1.74) | 5.17% / 22.85% / **0.23** | 5.17% / 22.85% / **0.23** | 16.45% / 26.60% / **0.62** |
| NIFTY Smallcap 250 TRI | 2006-01-17..2016-05-26 → 2016-05-26..2026-10-01 | N=20 (1.55) | 12.03% / 23.44% / **0.51** | 13.12% / 22.84% / **0.57** | 15.73% / 59.74% / **0.26** |

## Robustness knobs — reported separately, never tuned into the headline

N=30 and, for reference, N=200 with a ±1% / ±2% hysteresis band (in above SMAx(1+b), out below SMAx(1-b)) and with the signal read weekly (last session of the week) or monthly (last session of the month). After costs.

### NIFTY 50 TRI

**full (2000-04-17 → 2026-10-13)** — buy-and-hold: CAGR 12.44%, max DD 59.48%, CAGR/DD 0.21

| Rule | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | % invested | Switches/yr | CAGR after tax |
| --- | --- | --- | --- | --- | --- | --- | --- |
| N=30 | 8.89% | 13.61% | 34.42% | **0.26** | 61.64% | 19.59 | — |
| N=30, ±1% band | 10.21% | 12.44% | 32.76% | **0.31** | 61.27% | 9.25 | — |
| N=30, ±2% band | 13.31% | 14.70% | 23.85% | **0.56** | 63.17% | 5.63 | — |
| N=30, weekly | 10.37% | 12.82% | 28.49% | **0.36** | 62.37% | 10.16 | — |
| N=30, ±1% band, weekly | 9.99% | 11.56% | 41.27% | **0.24** | 63.09% | 6.53 | — |
| N=30, ±2% band, weekly | 10.95% | 12.08% | 33.99% | **0.32** | 65.22% | 4.64 | — |
| N=30, monthly | 11.17% | 12.36% | 33.86% | **0.33** | 59.63% | 4.95 | — |
| N=30, ±1% band, monthly | 10.72% | 11.58% | 37.11% | **0.29** | 63.58% | 3.59 | — |
| N=30, ±2% band, monthly | 11.85% | 12.48% | 39.20% | **0.30** | 68.47% | 2.60 | — |
| N=200 | 8.70% | 10.38% | 37.10% | **0.23** | 70.55% | 7.06 | — |
| N=200, ±1% band | 9.17% | 10.01% | 36.58% | **0.25** | 70.47% | 3.51 | — |
| N=200, ±2% band | 10.89% | 11.41% | 30.03% | **0.36** | 70.00% | 2.15 | — |
| N=200, weekly | 9.81% | 10.64% | 35.36% | **0.28** | 70.65% | 3.51 | — |
| N=200, ±1% band, weekly | 9.94% | 10.49% | 35.50% | **0.28** | 71.17% | 2.30 | — |
| N=200, ±2% band, weekly | 10.44% | 10.86% | 31.60% | **0.33** | 70.00% | 1.77 | — |
| N=200, monthly | 11.02% | 11.44% | 31.49% | **0.35** | 71.00% | 1.77 | — |
| N=200, ±1% band, monthly | 12.14% | 12.40% | 29.65% | **0.41** | 72.79% | 1.09 | — |
| N=200, ±2% band, monthly | 11.01% | 11.28% | 31.33% | **0.35** | 71.22% | 1.09 | — |

Of these 18 rules, **1** beat buy-and-hold on CAGR after costs and **18** on CAGR/DD.

**2006→ (2006-01-01 → 2026-10-13)** — buy-and-hold: CAGR 11.85%, max DD 59.46%, CAGR/DD 0.20, after tax 11.47%

| Rule | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | % invested | Switches/yr | CAGR after tax |
| --- | --- | --- | --- | --- | --- | --- | --- |
| N=30 | 6.44% | 11.40% | 34.42% | **0.19** | 62.54% | 20.74 | 4.45% |
| N=30, ±1% band | 7.89% | 10.17% | 32.76% | **0.24** | 62.12% | 9.48 | 5.75% |
| N=30, ±2% band | 11.39% | 12.81% | 23.85% | **0.48** | 64.93% | 5.73 | 8.83% |
| N=30, weekly | 9.08% | 11.67% | 28.49% | **0.32** | 63.53% | 10.64 | 6.96% |
| N=30, ±1% band, weekly | 9.17% | 10.78% | 28.31% | **0.32** | 63.92% | 6.59 | 7.18% |
| N=30, ±2% band, weekly | 10.13% | 11.25% | 30.34% | **0.33** | 67.57% | 4.57 | 8.19% |
| N=30, monthly | 9.97% | 11.21% | 33.86% | **0.29** | 62.02% | 5.05 | 7.92% |
| N=30, ±1% band, monthly | 8.94% | 9.82% | 37.12% | **0.24** | 65.79% | 3.61 | 7.18% |
| N=30, ±2% band, monthly | 10.11% | 10.72% | 39.20% | **0.26** | 72.45% | 2.45 | 8.61% |
| N=200 | 9.02% | 10.80% | 29.79% | **0.30** | 74.41% | 7.36 | 8.19% |
| N=200, ±1% band | 9.59% | 10.37% | 32.13% | **0.30** | 74.45% | 3.22 | 8.72% |
| N=200, ±2% band | 10.78% | 11.26% | 29.60% | **0.36** | 73.75% | 1.97 | 9.79% |
| N=200, weekly | 10.18% | 11.01% | 27.26% | **0.37** | 74.37% | 3.42 | 9.24% |
| N=200, ±1% band, weekly | 10.66% | 11.17% | 29.70% | **0.36** | 75.24% | 2.07 | 9.62% |
| N=200, ±2% band, weekly | 10.99% | 11.35% | 29.70% | **0.37** | 73.86% | 1.49 | 10.02% |
| N=200, monthly | 10.87% | 11.31% | 31.52% | **0.34** | 75.36% | 1.78 | 9.96% |
| N=200, ±1% band, monthly | 12.21% | 12.46% | 29.70% | **0.41** | 78.47% | 1.01 | 11.20% |
| N=200, ±2% band, monthly | 11.35% | 11.60% | 29.70% | **0.38** | 77.24% | 1.01 | 10.36% |

Of these 18 rules, **1** beat buy-and-hold on CAGR after costs and **17** on CAGR/DD; **0** on CAGR after tax.

**decade (2016-09-01 → 2026-08-31)** — buy-and-hold: CAGR 11.94%, max DD 38.24%, CAGR/DD 0.31, after tax 11.16%

| Rule | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | % invested | Switches/yr | CAGR after tax |
| --- | --- | --- | --- | --- | --- | --- | --- |
| N=30 | 4.92% | 10.08% | 18.67% | **0.26** | 64.70% | 22.51 | 3.27% |
| N=30, ±1% band | 7.44% | 9.48% | 16.51% | **0.45** | 64.50% | 8.80 | 5.31% |
| N=30, ±2% band | 9.88% | 11.11% | 12.56% | **0.79** | 68.09% | 5.20 | 7.83% |
| N=30, weekly | 9.40% | 11.87% | 15.08% | **0.62** | 65.87% | 10.50 | 7.24% |
| N=30, ±1% band, weekly | 10.32% | 11.70% | 13.04% | **0.79** | 65.27% | 5.80 | 8.10% |
| N=30, ±2% band, weekly | 10.72% | 11.62% | 15.68% | **0.68** | 70.80% | 3.80 | 8.86% |
| N=30, monthly | 11.25% | 12.35% | 15.25% | **0.74** | 62.92% | 4.60 | 8.84% |
| N=30, ±1% band, monthly | 10.84% | 11.65% | 10.77% | **1.01** | 60.54% | 3.40 | 8.38% |
| N=30, ±2% band, monthly | 10.79% | 11.24% | 18.34% | **0.59** | 77.14% | 1.90 | 9.01% |
| N=200 | 7.32% | 9.12% | 21.09% | **0.35** | 79.48% | 7.70 | 6.58% |
| N=200, ±1% band | 7.63% | 8.32% | 23.63% | **0.32** | 79.77% | 2.90 | 6.90% |
| N=200, ±2% band | 8.52% | 8.93% | 14.08% | **0.60** | 79.73% | 1.70 | 7.70% |
| N=200, weekly | 7.30% | 8.21% | 23.84% | **0.31** | 79.20% | 3.90 | 6.60% |
| N=200, ±1% band, weekly | 7.76% | 8.26% | 15.19% | **0.51** | 81.34% | 2.10 | 6.98% |
| N=200, ±2% band, weekly | 6.82% | 7.23% | 20.77% | **0.33** | 77.91% | 1.70 | 6.14% |
| N=200, monthly | 8.22% | 8.72% | 18.82% | **0.44** | 78.63% | 2.10 | 7.40% |
| N=200, ±1% band, monthly | 10.27% | 10.50% | 15.83% | **0.65** | 86.79% | 0.90 | 9.30% |
| N=200, ±2% band, monthly | 8.76% | 8.98% | 21.96% | **0.40** | 82.63% | 0.90 | 7.84% |

Of these 18 rules, **0** beat buy-and-hold on CAGR after costs and **16** on CAGR/DD; **0** on CAGR after tax.

**six-year (2019-07-01 → 2026-08-31)** — buy-and-hold: CAGR 11.66%, max DD 38.23%, CAGR/DD 0.31, after tax 10.70%

| Rule | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | % invested | Switches/yr | CAGR after tax |
| --- | --- | --- | --- | --- | --- | --- | --- |
| N=30 | 5.09% | 10.17% | 16.74% | **0.30** | 63.16% | 22.18 | 3.33% |
| N=30, ±1% band | 6.98% | 9.18% | 16.51% | **0.42** | 62.82% | 9.49 | 4.76% |
| N=30, ±2% band | 10.16% | 11.43% | 12.56% | **0.81** | 66.09% | 5.30 | 7.98% |
| N=30, weekly | 10.26% | 12.74% | 15.08% | **0.68** | 63.72% | 10.46 | 7.80% |
| N=30, ±1% band, weekly | 11.10% | 12.50% | 13.04% | **0.85** | 63.22% | 5.86 | 8.61% |
| N=30, ±2% band, weekly | 10.40% | 11.40% | 15.68% | **0.66** | 67.04% | 4.19 | 8.16% |
| N=30, monthly | 13.01% | 14.03% | 15.25% | **0.85** | 60.91% | 4.19 | 10.04% |
| N=30, ±1% band, monthly | 9.96% | 10.83% | 10.77% | **0.92** | 54.22% | 3.63 | 7.52% |
| N=30, ±2% band, monthly | 9.82% | 10.39% | 18.35% | **0.54** | 70.47% | 2.37 | 7.71% |
| N=200 | 8.84% | 10.19% | 13.13% | **0.67** | 76.27% | 5.72 | 7.78% |
| N=200, ±1% band | 9.10% | 9.61% | 13.08% | **0.70** | 76.60% | 2.09 | 8.06% |
| N=200, ±2% band | 8.80% | 9.24% | 13.89% | **0.63** | 75.87% | 1.81 | 7.80% |
| N=200, weekly | 8.97% | 9.67% | 14.01% | **0.64** | 76.66% | 2.93 | 7.95% |
| N=200, ±1% band, weekly | 7.93% | 8.43% | 15.19% | **0.52** | 77.39% | 2.09 | 6.97% |
| N=200, ±2% band, weekly | 7.14% | 7.58% | 20.77% | **0.34** | 76.77% | 1.81 | 6.32% |
| N=200, monthly | 9.99% | 10.44% | 14.82% | **0.67** | 74.80% | 1.81 | 8.82% |
| N=200, ±1% band, monthly | 10.07% | 10.32% | 15.83% | **0.64** | 82.73% | 0.98 | 8.88% |
| N=200, ±2% band, monthly | 8.77% | 9.02% | 21.96% | **0.40** | 81.55% | 0.98 | 7.70% |

Of these 18 rules, **1** beat buy-and-hold on CAGR after costs and **17** on CAGR/DD; **0** on CAGR after tax.

### NIFTY 500 TRI

**full (1995-11-09 → 2026-10-13)** — buy-and-hold: CAGR 13.47%, max DD 67.02%, CAGR/DD 0.20

| Rule | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | % invested | Switches/yr | CAGR after tax |
| --- | --- | --- | --- | --- | --- | --- | --- |
| N=30 | 12.71% | 17.31% | 26.00% | **0.49** | 62.44% | 17.95 | — |
| N=30, ±1% band | 14.14% | 16.43% | 29.81% | **0.47** | 61.51% | 8.89 | — |
| N=30, ±2% band | 14.62% | 16.28% | 29.14% | **0.50** | 61.80% | 6.43 | — |
| N=30, weekly | 12.10% | 14.39% | 36.99% | **0.33** | 63.14% | 9.02 | — |
| N=30, ±1% band, weekly | 12.53% | 14.14% | 35.76% | **0.35** | 62.94% | 6.37 | — |
| N=30, ±2% band, weekly | 13.01% | 14.27% | 34.46% | **0.38** | 64.74% | 4.95 | — |
| N=30, monthly | 10.47% | 11.76% | 48.84% | **0.21** | 60.65% | 5.14 | — |
| N=30, ±1% band, monthly | 11.74% | 12.68% | 48.84% | **0.24** | 64.18% | 3.72 | — |
| N=30, ±2% band, monthly | 12.78% | 13.53% | 45.59% | **0.28** | 67.96% | 2.94 | — |
| N=200 | 11.01% | 12.50% | 42.38% | **0.26** | 67.92% | 5.98 | — |
| N=200, ±1% band | 11.70% | 12.44% | 41.14% | **0.28** | 67.86% | 2.94 | — |
| N=200, ±2% band | 11.15% | 11.73% | 47.24% | **0.24** | 67.11% | 2.30 | — |
| N=200, weekly | 11.79% | 12.52% | 44.55% | **0.26** | 68.09% | 2.94 | — |
| N=200, ±1% band, weekly | 11.99% | 12.51% | 39.51% | **0.30** | 67.99% | 2.10 | — |
| N=200, ±2% band, weekly | 11.55% | 11.97% | 47.47% | **0.24** | 66.85% | 1.71 | — |
| N=200, monthly | 12.34% | 12.75% | 42.19% | **0.29** | 67.24% | 1.65 | — |
| N=200, ±1% band, monthly | 12.16% | 12.50% | 42.18% | **0.29** | 67.93% | 1.33 | — |
| N=200, ±2% band, monthly | 10.66% | 10.98% | 41.36% | **0.26** | 67.44% | 1.26 | — |

Of these 18 rules, **2** beat buy-and-hold on CAGR after costs and **18** on CAGR/DD.

**2006→ (2006-01-01 → 2026-10-13)** — buy-and-hold: CAGR 12.39%, max DD 63.68%, CAGR/DD 0.19, after tax 11.99%

| Rule | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | % invested | Switches/yr | CAGR after tax |
| --- | --- | --- | --- | --- | --- | --- | --- |
| N=30 | 9.62% | 14.50% | 23.31% | **0.41** | 64.17% | 19.78 | 7.03% |
| N=30, ±1% band | 11.76% | 14.00% | 21.49% | **0.55** | 63.15% | 9.00 | 8.95% |
| N=30, ±2% band | 12.65% | 14.18% | 28.42% | **0.45** | 64.17% | 6.11 | 9.94% |
| N=30, weekly | 11.98% | 14.24% | 26.75% | **0.45** | 65.13% | 9.10 | 9.49% |
| N=30, ±1% band, weekly | 11.24% | 12.74% | 30.33% | **0.37** | 65.38% | 6.11 | 9.03% |
| N=30, ±2% band, weekly | 10.53% | 11.73% | 32.26% | **0.33** | 67.83% | 4.86 | 8.40% |
| N=30, monthly | 11.51% | 12.71% | 33.90% | **0.34** | 63.30% | 4.86 | 9.21% |
| N=30, ±1% band, monthly | 9.54% | 10.40% | 41.71% | **0.23** | 69.79% | 3.51 | 7.90% |
| N=30, ±2% band, monthly | 10.43% | 11.10% | 42.40% | **0.25** | 74.14% | 2.65 | 8.80% |
| N=200 | 9.36% | 10.94% | 33.09% | **0.28** | 72.62% | 6.50 | 8.42% |
| N=200, ±1% band | 10.61% | 11.35% | 33.92% | **0.31** | 72.56% | 3.03 | 9.65% |
| N=200, ±2% band | 10.68% | 11.21% | 33.74% | **0.32** | 71.63% | 2.17 | 9.64% |
| N=200, weekly | 10.96% | 11.72% | 29.75% | **0.37** | 73.18% | 3.13 | 9.81% |
| N=200, ±1% band, weekly | 10.93% | 11.45% | 32.98% | **0.33** | 72.99% | 2.17 | 9.86% |
| N=200, ±2% band, weekly | 10.37% | 10.78% | 29.75% | **0.35** | 70.83% | 1.68 | 9.35% |
| N=200, monthly | 12.11% | 12.50% | 33.70% | **0.36** | 72.62% | 1.59 | 11.02% |
| N=200, ±1% band, monthly | 12.50% | 12.80% | 33.70% | **0.37** | 74.16% | 1.20 | 11.31% |
| N=200, ±2% band, monthly | 10.13% | 10.42% | 34.37% | **0.29** | 73.81% | 1.20 | 9.09% |

Of these 18 rules, **2** beat buy-and-hold on CAGR after costs and **18** on CAGR/DD; **0** on CAGR after tax.

**decade (2016-09-01 → 2026-08-31)** — buy-and-hold: CAGR 13.30%, max DD 38.08%, CAGR/DD 0.35, after tax 12.47%

| Rule | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | % invested | Switches/yr | CAGR after tax |
| --- | --- | --- | --- | --- | --- | --- | --- |
| N=30 | 7.13% | 11.99% | 19.19% | **0.37** | 66.84% | 20.81 | 5.23% |
| N=30, ±1% band | 9.68% | 11.67% | 12.04% | **0.80** | 65.47% | 8.40 | 7.60% |
| N=30, ±2% band | 10.95% | 12.33% | 12.01% | **0.91** | 66.24% | 5.80 | 8.80% |
| N=30, weekly | 11.93% | 14.14% | 15.38% | **0.78** | 67.85% | 9.20 | 9.58% |
| N=30, ±1% band, weekly | 12.63% | 13.93% | 15.89% | **0.79** | 67.73% | 5.40 | 10.22% |
| N=30, ±2% band, weekly | 10.71% | 11.72% | 20.43% | **0.52** | 71.32% | 4.20 | 8.60% |
| N=30, monthly | 11.86% | 13.02% | 17.76% | **0.67** | 63.81% | 4.80 | 9.49% |
| N=30, ±1% band, monthly | 10.53% | 11.30% | 18.07% | **0.58** | 69.71% | 3.20 | 8.46% |
| N=30, ±2% band, monthly | 12.05% | 12.54% | 18.98% | **0.64** | 79.68% | 2.00 | 10.09% |
| N=200 | 7.79% | 9.33% | 24.52% | **0.32** | 77.83% | 6.60 | 6.90% |
| N=200, ±1% band | 9.15% | 9.82% | 17.97% | **0.51** | 77.83% | 2.80 | 8.24% |
| N=200, ±2% band | 8.90% | 9.43% | 16.50% | **0.54** | 77.79% | 2.20 | 7.98% |
| N=200, weekly | 7.92% | 8.68% | 20.11% | **0.39** | 78.39% | 3.20 | 7.05% |
| N=200, ±1% band, weekly | 8.13% | 8.71% | 18.52% | **0.44** | 77.38% | 2.40 | 7.24% |
| N=200, ±2% band, weekly | 7.77% | 8.20% | 25.46% | **0.31** | 75.73% | 1.80 | 7.01% |
| N=200, monthly | 9.46% | 9.94% | 21.06% | **0.45** | 76.21% | 2.00 | 8.50% |
| N=200, ±1% band, monthly | 10.07% | 10.41% | 18.81% | **0.54** | 79.48% | 1.40 | 8.94% |
| N=200, ±2% band, monthly | 8.72% | 9.07% | 18.76% | **0.46** | 78.63% | 1.40 | 7.74% |

Of these 18 rules, **0** beat buy-and-hold on CAGR after costs and **16** on CAGR/DD; **0** on CAGR after tax.

**six-year (2019-07-01 → 2026-08-31)** — buy-and-hold: CAGR 14.23%, max DD 38.07%, CAGR/DD 0.37, after tax 13.11%

| Rule | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | % invested | Switches/yr | CAGR after tax |
| --- | --- | --- | --- | --- | --- | --- | --- |
| N=30 | 8.92% | 13.54% | 15.72% | **0.57** | 66.59% | 19.53 | 6.56% |
| N=30, ±1% band | 11.68% | 13.62% | 12.04% | **0.97** | 65.19% | 8.09 | 9.08% |
| N=30, ±2% band | 12.94% | 14.36% | 11.10% | **1.17** | 66.59% | 5.86 | 10.32% |
| N=30, weekly | 14.56% | 16.68% | 15.38% | **0.95** | 67.04% | 8.65 | 11.56% |
| N=30, ±1% band, weekly | 14.74% | 16.04% | 15.89% | **0.93** | 67.32% | 5.30 | 11.82% |
| N=30, ±2% band, weekly | 12.71% | 13.73% | 20.43% | **0.62** | 70.13% | 4.19 | 10.10% |
| N=30, monthly | 16.08% | 17.12% | 11.38% | **1.41** | 62.20% | 4.19 | 12.61% |
| N=30, ±1% band, monthly | 12.47% | 13.29% | 18.07% | **0.69** | 65.92% | 3.35 | 9.85% |
| N=30, ±2% band, monthly | 12.34% | 12.96% | 18.98% | **0.65** | 74.02% | 2.51 | 9.81% |
| N=200 | 9.38% | 10.64% | 17.50% | **0.54** | 76.77% | 5.30 | 8.10% |
| N=200, ±1% band | 10.20% | 10.81% | 14.46% | **0.70** | 76.83% | 2.51 | 9.01% |
| N=200, ±2% band | 9.17% | 9.78% | 16.50% | **0.56** | 76.04% | 2.51 | 8.03% |
| N=200, weekly | 8.25% | 9.05% | 17.38% | **0.47** | 77.05% | 3.35 | 7.14% |
| N=200, ±1% band, weekly | 8.64% | 9.25% | 18.52% | **0.47** | 75.93% | 2.51 | 7.49% |
| N=200, ±2% band, weekly | 7.94% | 8.41% | 25.46% | **0.31** | 73.45% | 1.95 | 6.99% |
| N=200, monthly | 11.01% | 11.49% | 19.89% | **0.55** | 76.21% | 1.95 | 9.72% |
| N=200, ±1% band, monthly | 10.25% | 10.67% | 18.81% | **0.54** | 78.35% | 1.67 | 8.88% |
| N=200, ±2% band, monthly | 8.38% | 8.79% | 18.77% | **0.45** | 77.17% | 1.67 | 7.24% |

Of these 18 rules, **3** beat buy-and-hold on CAGR after costs and **17** on CAGR/DD; **0** on CAGR after tax.

## Calendar years — N=30 against buy-and-hold (full history, after costs)

### NIFTY 50 TRI (2000-04-17 → 2026-10-13; first and last years partial)

| Year | Buy-and-hold | N=30 after costs | N=30 before costs | Excess (after costs) | Switches | % invested |
| --- | --- | --- | --- | --- | --- | --- |
| 2000 | -12.07% | -1.34% | 0.20% | +10.73pp | 7 | 48.88% |
| 2001 | -15.05% | -1.85% | 1.28% | +13.20pp | 16 | 42.74% |
| 2002 | 5.39% | 7.81% | 12.42% | +2.41pp | 21 | 46.61% |
| 2003 | 76.51% | 66.13% | 71.59% | -10.38pp | 16 | 68.11% |
| 2004 | 13.03% | 9.61% | 14.84% | -3.42pp | 22 | 66.14% |
| 2005 | 38.60% | 37.06% | 38.68% | -1.53pp | 6 | 74.50% |
| 2006 | 41.89% | 41.65% | 46.95% | -0.24pp | 16 | 82.40% |
| 2007 | 56.80% | 16.50% | 22.74% | -40.30pp | 22 | 72.69% |
| 2008 | -51.26% | -17.53% | -14.73% | +33.73pp | 14 | 34.15% |
| 2009 | 77.55% | 45.34% | 53.67% | -32.21pp | 24 | 69.96% |
| 2010 | 19.22% | 13.47% | 17.87% | -5.75pp | 16 | 64.68% |
| 2011 | -23.80% | -20.50% | -16.00% | +3.31pp | 23 | 42.11% |
| 2012 | 29.44% | 9.51% | 13.81% | -19.93pp | 17 | 64.14% |
| 2013 | 8.07% | 1.07% | 5.52% | -7.01pp | 20 | 56.80% |
| 2014 | 32.89% | 16.15% | 20.41% | -16.74pp | 17 | 74.59% |
| 2015 | -3.01% | -8.46% | -2.62% | -5.46pp | 29 | 44.76% |
| 2016 | 4.39% | 6.09% | 9.79% | +1.70pp | 16 | 54.66% |
| 2017 | 30.27% | 11.36% | 16.70% | -18.91pp | 22 | 83.87% |
| 2018 | 4.64% | 3.31% | 7.84% | -1.33pp | 20 | 64.23% |
| 2019 | 13.47% | -1.90% | 2.82% | -15.38pp | 22 | 67.35% |
| 2020 | 16.14% | 24.94% | 29.77% | +8.80pp | 18 | 71.03% |
| 2021 | 25.59% | 6.37% | 12.11% | -19.23pp | 25 | 70.97% |
| 2022 | 5.69% | 0.47% | 4.37% | -5.22pp | 18 | 56.05% |
| 2023 | 21.30% | 18.81% | 23.65% | -2.49pp | 19 | 60.98% |
| 2024 | 10.09% | 3.50% | 9.10% | -6.59pp | 25 | 67.47% |
| 2025 | 11.88% | -2.58% | 2.48% | -14.47pp | 24 | 63.05% |
| 2026 | -12.22% | -8.77% | -4.01% | +3.45pp | 24 | 43.01% |

N=30 beat buy-and-hold (after costs) in **8 of 27** calendar years.

### NIFTY 500 TRI (1995-11-09 → 2026-10-13; first and last years partial)

| Year | Buy-and-hold | N=30 after costs | N=30 before costs | Excess (after costs) | Switches | % invested |
| --- | --- | --- | --- | --- | --- | --- |
| 1995 | -7.47% | 0.35% | 0.35% | +7.82pp | 0 | 0.00% |
| 1996 | -5.94% | 11.08% | 14.84% | +17.01pp | 12 | 35.71% |
| 1997 | 14.58% | 2.98% | 6.72% | -11.60pp | 14 | 58.13% |
| 1998 | -6.78% | -4.85% | -0.74% | +1.93pp | 16 | 44.94% |
| 1999 | 100.70% | 64.72% | 70.03% | -35.98pp | 14 | 82.28% |
| 2000 | -23.42% | 22.73% | 25.06% | +46.15pp | 9 | 52.40% |
| 2001 | -21.51% | 1.61% | 4.44% | +23.12pp | 12 | 41.53% |
| 2002 | 13.83% | 0.41% | 6.83% | -13.42pp | 27 | 59.76% |
| 2003 | 104.81% | 77.75% | 83.05% | -27.06pp | 14 | 75.20% |
| 2004 | 21.09% | 15.57% | 20.91% | -5.52pp | 20 | 68.90% |
| 2005 | 38.79% | 29.03% | 30.61% | -9.76pp | 6 | 74.90% |
| 2006 | 36.13% | 30.27% | 33.26% | -5.87pp | 10 | 80.40% |
| 2007 | 64.58% | 48.51% | 55.62% | -16.07pp | 20 | 76.71% |
| 2008 | -56.54% | -11.47% | -8.39% | +45.07pp | 14 | 32.52% |
| 2009 | 90.95% | 54.29% | 61.02% | -36.66pp | 18 | 72.02% |
| 2010 | 15.27% | 10.59% | 16.06% | -4.68pp | 20 | 65.08% |
| 2011 | -26.39% | -17.66% | -12.98% | +8.73pp | 23 | 40.49% |
| 2012 | 33.48% | 12.97% | 18.52% | -20.51pp | 21 | 66.53% |
| 2013 | 4.82% | 4.35% | 8.41% | -0.46pp | 18 | 54.40% |
| 2014 | 39.29% | 15.86% | 21.72% | -23.43pp | 23 | 74.59% |
| 2015 | 0.23% | -8.49% | -2.21% | -8.72pp | 31 | 52.82% |
| 2016 | 5.12% | 6.13% | 9.12% | +1.00pp | 13 | 63.97% |
| 2017 | 37.65% | 18.32% | 22.67% | -19.33pp | 17 | 86.29% |
| 2018 | -2.14% | -0.94% | 4.73% | +1.20pp | 26 | 57.72% |
| 2019 | 8.97% | -3.00% | 2.11% | -11.97pp | 24 | 60.82% |
| 2020 | 17.89% | 26.71% | 31.04% | +8.82pp | 16 | 75.79% |
| 2021 | 31.59% | 11.71% | 15.76% | -19.88pp | 17 | 77.42% |
| 2022 | 4.25% | 4.34% | 8.83% | +0.09pp | 20 | 56.45% |
| 2023 | 26.91% | 29.68% | 33.80% | +2.77pp | 15 | 66.26% |
| 2024 | 16.24% | 9.11% | 12.59% | -7.13pp | 15 | 73.49% |
| 2025 | 7.76% | -2.52% | 2.98% | -10.28pp | 26 | 59.44% |
| 2026 | -6.73% | -8.21% | -3.43% | -1.48pp | 24 | 51.30% |

N=30 beat buy-and-hold (after costs) in **12 of 32** calendar years.

## Drawdown episodes — when did N=30 get out and back in

Peak and trough are the index's own (largest peak-to-trough fall inside the search range). N=30's path here is before costs (TRI when in, cash at repo-50bp when out), positions lagged one session as everywhere else. **Out** is N=30's first exit on or after the peak; **back in for good** is its last entry before the index regained the peak (or the range end).

| Index | Episode | Index peak → trough (fall) | Recovered peak | N=30 out (index vs peak then) | Round trips peak→recovery | N=30 back in for good (index vs trough then) | N=30 peak→trough vs index | N=30 peak→recovery vs index |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| NIFTY 50 TRI | 2000-03 dot-com | 2000-02-11 → 2001-09-21 (50.23%) | 2003-12-01 | 2000-03-01 (-2.47pp) | 34 | 2003-11-27 (+96.19pp) | -1.25pp vs -50.23pp | +91.66pp vs +1.26pp |
| NIFTY 50 TRI | 2008 GFC | 2008-01-08 → 2008-10-27 (59.50%) | 2010-10-01 | 2008-01-17 (-5.96pp) | 24 | 2010-09-02 (+121.89pp) | -12.86pp vs -59.50pp | +54.15pp vs +0.71pp |
| NIFTY 50 TRI | 2020 Covid | 2020-01-14 → 2020-03-23 (38.27%) | 2020-11-06 | 2020-01-22 (-2.07pp) | 8 | 2020-10-05 (+51.99pp) | -2.97pp vs -38.27pp | +16.82pp vs +0.25pp |
| NIFTY 50 TRI | 2024-26 | 2024-09-26 → 2025-03-04 (15.43%) | 2025-10-23 | 2024-10-04 (-4.58pp) | 12 | 2025-10-07 (+14.67pp) | -6.19pp vs -15.43pp | +3.89pp vs +0.03pp |
| NIFTY 500 TRI | 2000-03 dot-com | 2000-02-21 → 2001-09-21 (67.02%) | 2004-01-01 | 2000-03-10 (-15.23pp) | 31 | 2003-11-25 (+144.73pp) | -7.76pp vs -67.02pp | +129.03pp vs +1.00pp |
| NIFTY 500 TRI | 2008 GFC | 2008-01-04 → 2008-10-27 (63.71%) | 2014-03-27 | 2008-01-17 (-6.38pp) | 61 | 2014-02-24 (+157.42pp) | -3.55pp vs -63.71pp | +155.35pp vs +0.59pp |
| NIFTY 500 TRI | 2020 Covid | 2020-01-17 → 2020-03-23 (38.11%) | 2020-11-06 | 2020-01-31 (-2.53pp) | 7 | 2020-10-05 (+53.22pp) | -2.13pp vs -38.11pp | +18.53pp vs +0.15pp |
| NIFTY 500 TRI | 2024-26 | 2024-09-26 → 2025-02-28 (18.59%) | not yet | 2024-10-04 (-3.93pp) | 27 | still out | -4.37pp vs -18.59pp | +1.37pp vs -8.08pp |

## Secondary — Midcap 150 and Smallcap 250 (ranked by CAGR/DD after costs)

### NIFTY Midcap 150 TRI

**full (2006-01-17 → 2026-10-01)** — whole published history (pre-2006 costs and cash on proxies)

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | **N=30** | 19.25% | 23.28% | 18.39% | **1.05** | 13.77% | 0.94 | 64.39% | 15.21 | 2021 (-37.86pp, 21 sw) | +4.10pp | 14.55% | ₹52.28L (₹46.70L) | ₹45.90L |
| 2 | N=20 | 16.10% | 21.86% | 20.58% | **0.78** | 13.68% | 0.74 | 63.72% | 22.07 | 2009 (-37.24pp, 18 sw) | +0.95pp | 11.52% | ₹57.38L (₹51.17L) | ₹50.11L |
| 3 | N=100 | 17.45% | 19.37% | 31.07% | **0.56** | 14.76% | 0.78 | 66.30% | 7.49 | 2012 (-26.90pp, 9 sw) | +2.31pp | 14.55% | ₹19.74L (₹17.62L) | ₹17.43L |
| 4 | N=150 | 15.96% | 17.63% | 30.90% | **0.52** | 15.38% | 0.67 | 68.95% | 6.52 | 2012 (-33.67pp, 13 sw) | +0.82pp | 14.40% | ₹10.99L (₹9.78L) | ₹9.58L |
| 5 | N=50 | 17.70% | 20.47% | 43.06% | **0.41** | 14.51% | 0.81 | 66.16% | 10.67 | 2009 (-34.75pp, 8 sw) | +2.56pp | 13.81% | ₹32.07L (₹28.68L) | ₹28.32L |
| 6 | N=200 | 15.40% | 16.70% | 37.56% | **0.41** | 15.77% | 0.63 | 72.26% | 5.07 | 2012 (-29.08pp, 11 sw) | +0.26pp | 14.06% | ₹7.83L (₹6.96L) | ₹6.82L |
| 7 | N=10 | 8.94% | 17.73% | 34.54% | **0.26** | 13.43% | 0.27 | 61.27% | 35.21 | 2007 (-45.56pp, 36 sw) | -6.21pp | 5.80% | ₹32.77L (₹29.19L) | ₹28.17L |
| 8 | buy-and-hold | 15.14% | 15.15% | 72.85% | **0.21** | 20.91% | 0.51 | 100.00% | 0.00 | — | — | 14.66% | ₹0.01L (₹0.01L) | ₹0.01L |

**2006→ (2006-01-01 → 2026-10-01)** — span every model covers (after-tax available)

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | **N=30** | 19.34% | 23.37% | 18.39% | **1.05** | 13.77% | 0.94 | 64.46% | 15.18 | 2021 (-37.86pp, 21 sw) | +4.10pp | 14.61% | ₹53.50L (₹47.79L) | ₹46.98L |
| 2 | N=20 | 16.19% | 21.95% | 20.58% | **0.79** | 13.68% | 0.75 | 63.80% | 22.03 | 2009 (-37.25pp, 18 sw) | +0.95pp | 11.57% | ₹58.72L (₹52.36L) | ₹51.28L |
| 3 | N=100 | 17.54% | 19.46% | 31.07% | **0.56** | 14.76% | 0.79 | 66.36% | 7.47 | 2012 (-26.91pp, 9 sw) | +2.30pp | 14.62% | ₹20.20L (₹18.03L) | ₹17.84L |
| 4 | N=150 | 16.06% | 17.72% | 30.90% | **0.52** | 15.37% | 0.68 | 69.01% | 6.51 | 2012 (-33.67pp, 13 sw) | +0.82pp | 14.47% | ₹11.25L (₹10.01L) | ₹9.80L |
| 5 | N=50 | 17.79% | 20.56% | 43.06% | **0.41** | 14.50% | 0.81 | 66.23% | 10.65 | 2009 (-34.76pp, 8 sw) | +2.55pp | 13.87% | ₹32.81L (₹29.35L) | ₹28.98L |
| 6 | N=200 | 15.39% | 16.67% | 37.56% | **0.41** | 15.75% | 0.63 | 72.12% | 5.06 | 2012 (-29.08pp, 11 sw) | +0.15pp | 14.04% | ₹7.85L (₹6.98L) | ₹6.84L |
| 7 | N=10 | 9.04% | 17.83% | 34.54% | **0.26** | 13.43% | 0.28 | 61.35% | 35.14 | 2007 (-45.56pp, 36 sw) | -6.20pp | 5.87% | ₹33.54L (₹29.87L) | ₹28.83L |
| 8 | buy-and-hold | 15.24% | 15.24% | 72.85% | **0.21** | 20.89% | 0.51 | 100.00% | 0.00 | — | — | 14.76% | ₹0.01L (₹0.01L) | ₹0.01L |

**decade (2016-09-01 → 2026-08-31)** — owner mandate window

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | **N=30** | 14.45% | 18.24% | 18.39% | **0.79** | 11.95% | 0.78 | 66.56% | 15.41 | 2021 (-37.85pp, 21 sw) | -3.08pp | 11.52% | ₹4.11L (₹3.66L) | ₹3.66L |
| 2 | N=50 | 14.21% | 17.20% | 19.72% | **0.72** | 12.14% | 0.75 | 68.26% | 12.21 | 2017 (-25.49pp, 13 sw) | -3.32pp | 11.37% | ₹3.38L (₹3.02L) | ₹3.02L |
| 3 | N=100 | 14.28% | 16.25% | 21.70% | **0.66** | 12.56% | 0.74 | 69.91% | 8.00 | 2017 (-15.87pp, 5 sw) | -3.24pp | 12.36% | ₹1.87L (₹1.66L) | ₹1.66L |
| 4 | N=150 | 14.03% | 15.46% | 22.45% | **0.63** | 12.95% | 0.70 | 72.90% | 5.80 | 2026 (-9.81pp, 6 sw) | -3.50pp | 12.53% | ₹1.21L (₹1.07L) | ₹1.07L |
| 5 | N=200 | 13.44% | 14.57% | 25.85% | **0.52** | 13.29% | 0.65 | 76.49% | 4.60 | 2026 (-9.54pp, 6 sw) | -4.09pp | 12.08% | ₹0.92L (₹0.81L) | ₹0.81L |
| 6 | N=20 | 8.74% | 14.48% | 20.59% | **0.42** | 12.11% | 0.34 | 65.75% | 24.21 | 2021 (-35.63pp, 17 sw) | -8.79pp | 6.56% | ₹4.68L (₹4.16L) | ₹4.16L |
| 7 | buy-and-hold | 17.53% | 17.54% | 43.02% | **0.41** | 17.69% | 0.73 | 100.00% | 0.00 | — | — | 16.52% | ₹0.01L (₹0.01L) | ₹0.01L |
| 8 | N=10 | 5.72% | 14.32% | 34.55% | **0.17** | 11.43% | 0.10 | 62.28% | 36.82 | 2021 (-33.89pp, 34 sw) | -11.81pp | 3.46% | ₹6.33L (₹5.63L) | ₹5.63L |

**six-year (2019-07-01 → 2026-08-31)** — owner mandate window

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | N=100 | 18.70% | 20.55% | 16.22% | **1.15** | 13.10% | 1.02 | 73.51% | 7.25 | 2021 (-6.26pp, 4 sw) | -2.21pp | 16.27% | ₹1.36L (₹1.22L) | ₹1.22L |
| 2 | N=200 | 16.27% | 17.18% | 15.94% | **1.02** | 13.80% | 0.83 | 78.85% | 3.63 | 2026 (-9.54pp, 6 sw) | -4.65pp | 14.32% | ₹0.54L (₹0.48L) | ₹0.48L |
| 3 | N=150 | 17.26% | 18.38% | 17.26% | **1.00** | 13.45% | 0.91 | 75.42% | 4.46 | 2026 (-9.81pp, 6 sw) | -3.66pp | 15.12% | ₹0.74L (₹0.66L) | ₹0.66L |
| 4 | N=50 | 17.17% | 20.32% | 18.29% | **0.94** | 12.86% | 0.94 | 69.74% | 12.56 | 2024 (-13.83pp, 12 sw) | -3.75pp | 13.56% | ₹2.43L (₹2.17L) | ₹2.17L |
| 5 | **N=30** | 15.50% | 19.67% | 18.39% | **0.84** | 12.73% | 0.83 | 68.00% | 16.74 | 2021 (-37.85pp, 21 sw) | -5.42pp | 12.27% | ₹2.64L (₹2.36L) | ₹2.36L |
| 6 | buy-and-hold | 20.92% | 20.93% | 38.42% | **0.54** | 18.65% | 0.87 | 100.00% | 0.00 | — | — | 19.45% | ₹0.01L (₹0.01L) | ₹0.01L |
| 7 | N=20 | 9.11% | 15.02% | 20.59% | **0.44** | 12.86% | 0.37 | 66.65% | 24.83 | 2021 (-35.63pp, 17 sw) | -11.81pp | 6.78% | ₹3.11L (₹2.78L) | ₹2.78L |
| 8 | N=10 | 6.74% | 15.55% | 34.56% | **0.20** | 12.02% | 0.21 | 63.10% | 37.39 | 2021 (-33.89pp, 34 sw) | -14.18pp | 4.09% | ₹4.65L (₹4.15L) | ₹4.15L |

### NIFTY Smallcap 250 TRI

**full (2006-01-17 → 2026-10-01)** — whole published history (pre-2006 costs and cash on proxies)

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | **N=30** | 19.01% | 22.98% | 22.84% | **0.83** | 13.95% | 0.91 | 62.26% | 15.02 | 2021 (-46.15pp, 20 sw) | +5.49pp | 13.79% | ₹61.49L (₹54.98L) | ₹54.20L |
| 2 | N=20 | 19.41% | 24.52% | 23.44% | **0.83** | 13.93% | 0.94 | 61.70% | 19.18 | 2021 (-34.93pp, 20 sw) | +5.88pp | 13.64% | ₹87.42L (₹78.05L) | ₹77.19L |
| 3 | N=100 | 19.17% | 20.58% | 29.61% | **0.65** | 15.28% | 0.86 | 62.97% | 5.41 | 2012 (-30.31pp, 7 sw) | +5.64pp | 16.10% | ₹18.34L (₹16.39L) | ₹16.23L |
| 4 | N=150 | 17.45% | 18.90% | 36.40% | **0.48** | 15.79% | 0.74 | 64.48% | 5.60 | 2012 (-32.23pp, 11 sw) | +3.92pp | 15.87% | ₹9.31L (₹8.29L) | ₹8.09L |
| 5 | N=50 | 18.09% | 20.80% | 43.26% | **0.42** | 15.07% | 0.80 | 63.92% | 10.38 | 2021 (-30.97pp, 10 sw) | +4.56pp | 14.18% | ₹33.06L (₹29.58L) | ₹29.23L |
| 6 | N=200 | 14.67% | 15.93% | 36.69% | **0.40** | 16.00% | 0.58 | 65.65% | 4.93 | 2009 (-49.14pp, 1 sw) | +1.14pp | 13.40% | ₹7.05L (₹6.28L) | ₹6.14L |
| 7 | N=10 | 13.91% | 22.28% | 35.39% | **0.39** | 13.73% | 0.60 | 60.20% | 32.31 | 2021 (-40.41pp, 30 sw) | +0.38pp | 10.16% | ₹52.31L (₹46.65L) | ₹45.63L |
| 8 | buy-and-hold | 13.53% | 13.53% | 75.53% | **0.18** | 21.49% | 0.43 | 100.00% | 0.00 | — | — | 13.10% | ₹0.01L (₹0.01L) | ₹0.01L |

**2006→ (2006-01-01 → 2026-10-01)** — span every model covers (after-tax available)

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | **N=30** | 19.10% | 23.06% | 22.84% | **0.84** | 13.95% | 0.92 | 62.34% | 14.99 | 2021 (-46.16pp, 20 sw) | +5.48pp | 13.84% | ₹62.85L (₹56.20L) | ₹55.39L |
| 2 | N=20 | 19.49% | 24.60% | 23.44% | **0.83** | 13.93% | 0.94 | 61.77% | 19.14 | 2021 (-34.93pp, 20 sw) | +5.87pp | 13.69% | ₹89.36L (₹79.77L) | ₹78.89L |
| 3 | N=100 | 19.26% | 20.66% | 29.62% | **0.65** | 15.27% | 0.86 | 63.04% | 5.40 | 2012 (-30.31pp, 7 sw) | +5.64pp | 16.15% | ₹18.75L (₹16.75L) | ₹16.59L |
| 4 | N=150 | 17.53% | 18.99% | 36.40% | **0.48** | 15.79% | 0.74 | 64.55% | 5.59 | 2012 (-32.23pp, 11 sw) | +3.91pp | 15.94% | ₹9.51L (₹8.47L) | ₹8.27L |
| 5 | N=50 | 18.17% | 20.88% | 43.26% | **0.42** | 15.07% | 0.81 | 63.99% | 10.36 | 2021 (-30.97pp, 10 sw) | +4.55pp | 14.24% | ₹33.79L (₹30.24L) | ₹29.87L |
| 6 | N=200 | 14.65% | 15.91% | 36.69% | **0.40** | 15.98% | 0.58 | 65.53% | 4.92 | 2009 (-49.14pp, 1 sw) | +1.03pp | 13.38% | ₹7.07L (₹6.30L) | ₹6.16L |
| 7 | N=10 | 14.00% | 22.36% | 35.39% | **0.40** | 13.73% | 0.61 | 60.28% | 32.25 | 2021 (-40.41pp, 30 sw) | +0.38pp | 10.22% | ₹53.47L (₹47.69L) | ₹46.64L |
| 8 | buy-and-hold | 13.62% | 13.62% | 75.53% | **0.18** | 21.48% | 0.44 | 100.00% | 0.00 | — | — | 13.19% | ₹0.01L (₹0.01L) | ₹0.01L |

**decade (2016-09-01 → 2026-08-31)** — owner mandate window

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | N=100 | 17.04% | 18.60% | 17.06% | **1.00** | 13.40% | 0.88 | 63.73% | 6.20 | 2017 (-21.42pp, 9 sw) | +1.92pp | 14.49% | ₹1.83L (₹1.63L) | ₹1.63L |
| 2 | N=150 | 17.65% | 18.90% | 21.28% | **0.83** | 13.77% | 0.90 | 65.02% | 5.00 | 2017 (-6.03pp, 1 sw) | +2.53pp | 15.69% | ₹1.18L (₹1.05L) | ₹1.05L |
| 3 | N=50 | 15.40% | 18.32% | 22.22% | **0.69** | 13.10% | 0.79 | 65.15% | 11.80 | 2021 (-30.94pp, 10 sw) | +0.28pp | 12.38% | ₹3.55L (₹3.17L) | ₹3.17L |
| 4 | N=200 | 14.57% | 15.75% | 25.00% | **0.58** | 14.08% | 0.69 | 67.33% | 4.80 | 2023 (-11.16pp, 8 sw) | -0.55pp | 12.98% | ₹1.12L (₹1.00L) | ₹1.00L |
| 5 | **N=30** | 12.71% | 16.50% | 22.84% | **0.56** | 12.57% | 0.62 | 63.97% | 15.61 | 2021 (-46.14pp, 20 sw) | -2.41pp | 9.84% | ₹4.32L (₹3.85L) | ₹3.85L |
| 6 | N=20 | 12.04% | 17.35% | 23.44% | **0.51** | 12.48% | 0.58 | 62.64% | 21.81 | 2021 (-34.91pp, 20 sw) | -3.08pp | 8.97% | ₹5.51L (₹4.91L) | ₹4.91L |
| 7 | N=10 | 10.78% | 18.54% | 29.28% | **0.37** | 12.34% | 0.49 | 60.95% | 32.01 | 2021 (-40.39pp, 30 sw) | -4.34pp | 8.13% | ₹6.92L (₹6.16L) | ₹6.16L |
| 8 | buy-and-hold | 15.12% | 15.13% | 59.73% | **0.25** | 19.25% | 0.57 | 100.00% | 0.00 | — | — | 14.24% | ₹0.01L (₹0.01L) | ₹0.01L |

**six-year (2019-07-01 → 2026-08-31)** — owner mandate window

| # | Strategy | CAGR after costs | CAGR before costs | Max DD | **CAGR/DD** | Vol | Sharpe vs cash | % invested | Switches/yr | Worst year vs B&H | Excess vs B&H | CAGR after tax | Charges (of which STT) | Slippage |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | N=100 | 22.49% | 24.02% | 15.54% | **1.45** | 13.86% | 1.20 | 67.83% | 5.86 | 2024 (-17.55pp, 11 sw) | +2.15pp | 18.99% | ₹1.40L (₹1.25L) | ₹1.25L |
| 2 | N=150 | 22.45% | 23.69% | 17.36% | **1.29** | 14.20% | 1.18 | 68.17% | 4.74 | 2023 (-4.76pp, 4 sw) | +2.11pp | 19.54% | ₹0.82L (₹0.73L) | ₹0.73L |
| 3 | N=50 | 19.23% | 22.37% | 22.22% | **0.87** | 13.88% | 1.00 | 67.77% | 12.28 | 2021 (-30.94pp, 10 sw) | -1.10pp | 15.24% | ₹2.65L (₹2.37L) | ₹2.37L |
| 4 | N=200 | 18.37% | 19.50% | 25.00% | **0.73** | 14.45% | 0.92 | 69.80% | 4.46 | 2023 (-11.16pp, 8 sw) | -1.97pp | 16.00% | ₹0.79L (₹0.70L) | ₹0.70L |
| 5 | **N=30** | 15.04% | 19.33% | 22.84% | **0.66** | 13.35% | 0.77 | 65.58% | 17.30 | 2021 (-46.13pp, 20 sw) | -5.30pp | 11.44% | ₹3.21L (₹2.87L) | ₹2.87L |
| 6 | N=20 | 12.92% | 18.25% | 23.45% | **0.55** | 13.06% | 0.64 | 63.72% | 21.76 | 2021 (-34.91pp, 20 sw) | -7.42pp | 9.44% | ₹3.59L (₹3.20L) | ₹3.20L |
| 7 | N=10 | 14.82% | 22.79% | 27.52% | **0.54** | 12.76% | 0.78 | 62.37% | 31.81 | 2021 (-40.39pp, 30 sw) | -5.52pp | 11.12% | ₹5.59L (₹5.00L) | ₹5.00L |
| 8 | buy-and-hold | 20.34% | 20.35% | 43.44% | **0.47** | 19.93% | 0.80 | 100.00% | 0.00 | — | — | 18.90% | ₹0.01L (₹0.01L) | ₹0.01L |

## Today — what each rule says on the lake's last session

The decision struck at the last session's close (traded at the next session's close). **Since** is the session whose close last flipped the decision.

| Index | Last session | Close | N | SMA | Close vs SMA | Decision | Since |
| --- | --- | --- | --- | --- | --- | --- | --- |
| NIFTY 50 TRI | 2026-10-13 | 34525.83 | 10 | 34269.30 | +0.75pp | **invested** | 2026-10-13 |
| NIFTY 50 TRI | 2026-10-13 | 34525.83 | 20 | 34770.44 | -0.70pp | cash | 2026-08-17 |
| NIFTY 50 TRI | 2026-10-13 | 34525.83 | 30 | 35210.72 | -1.95pp | cash | 2026-08-26 |
| NIFTY 50 TRI | 2026-10-13 | 34525.83 | 50 | 35936.09 | -3.92pp | cash | 2026-08-31 |
| NIFTY 50 TRI | 2026-10-13 | 34525.83 | 100 | 36056.21 | -4.24pp | cash | 2026-09-02 |
| NIFTY 50 TRI | 2026-10-13 | 34525.83 | 150 | 35958.84 | -3.99pp | cash | 2026-09-01 |
| NIFTY 50 TRI | 2026-10-13 | 34525.83 | 200 | 36625.77 | -5.73pp | cash | 2026-08-07 |
| NIFTY 500 TRI | 2026-10-13 | 35535.73 | 10 | 35335.85 | +0.57pp | **invested** | 2026-10-13 |
| NIFTY 500 TRI | 2026-10-13 | 35535.73 | 20 | 35883.57 | -0.97pp | cash | 2026-08-26 |
| NIFTY 500 TRI | 2026-10-13 | 35535.73 | 30 | 36337.04 | -2.21pp | cash | 2026-08-31 |
| NIFTY 500 TRI | 2026-10-13 | 35535.73 | 50 | 36985.89 | -3.92pp | cash | 2026-09-01 |
| NIFTY 500 TRI | 2026-10-13 | 35535.73 | 100 | 36909.56 | -3.72pp | cash | 2026-09-09 |
| NIFTY 500 TRI | 2026-10-13 | 35535.73 | 150 | 36427.12 | -2.45pp | cash | 2026-09-24 |
| NIFTY 500 TRI | 2026-10-13 | 35535.73 | 200 | 36675.40 | -3.11pp | cash | 2026-09-24 |
| NIFTY Midcap 150 TRI | 2026-10-01 | 27668.82 | 10 | 28604.99 | -3.27pp | cash | 2026-09-24 |
| NIFTY Midcap 150 TRI | 2026-10-01 | 27668.82 | 20 | 28910.43 | -4.29pp | cash | 2026-09-01 |
| NIFTY Midcap 150 TRI | 2026-10-01 | 27668.82 | 30 | 29250.34 | -5.41pp | cash | 2026-09-01 |
| NIFTY Midcap 150 TRI | 2026-10-01 | 27668.82 | 50 | 29427.40 | -5.98pp | cash | 2026-09-07 |
| NIFTY Midcap 150 TRI | 2026-10-01 | 27668.82 | 100 | 29144.20 | -5.06pp | cash | 2026-09-24 |
| NIFTY Midcap 150 TRI | 2026-10-01 | 27668.82 | 150 | 28455.02 | -2.76pp | cash | 2026-09-28 |
| NIFTY Midcap 150 TRI | 2026-10-01 | 27668.82 | 200 | 28322.69 | -2.31pp | cash | 2026-09-28 |
| NIFTY Smallcap 250 TRI | 2026-10-01 | 22471.40 | 10 | 23065.08 | -2.57pp | cash | 2026-09-24 |
| NIFTY Smallcap 250 TRI | 2026-10-01 | 22471.40 | 20 | 23212.70 | -3.19pp | cash | 2026-09-24 |
| NIFTY Smallcap 250 TRI | 2026-10-01 | 22471.40 | 30 | 23300.40 | -3.56pp | cash | 2026-09-24 |
| NIFTY Smallcap 250 TRI | 2026-10-01 | 22471.40 | 50 | 23232.40 | -3.28pp | cash | 2026-09-24 |
| NIFTY Smallcap 250 TRI | 2026-10-01 | 22471.40 | 100 | 22687.33 | -0.95pp | cash | 2026-10-01 |
| NIFTY Smallcap 250 TRI | 2026-10-01 | 22471.40 | 150 | 21807.81 | +3.04pp | **invested** | 2026-04-17 |
| NIFTY Smallcap 250 TRI | 2026-10-01 | 22471.40 | 200 | 21495.84 | +4.54pp | **invested** | 2026-04-27 |

<!-- hand-written analysis below: kept verbatim on re-render -->


## Hand-written analysis (not generated — written after reading the tables above)

### Part 1 — does a 30-day moving average beat buy-and-hold on Indian indices?

**Short answer: no — not after costs and not after tax, on any window the mandate ranks on. What it reliably does is cut the drawdown roughly in half; on NIFTY 50 / NIFTY 500 that makes its return per unit of drawdown sometimes better and sometimes worse, while its return is lower, often by a lot. 30 is not a special length.** Reading used: the common "hold the index above its N-day SMA, else cash" rule (stated at the top); the article's own rule turned out to be stock-level, which is Part 3.

1. **Before costs, over the long history, the thesis is partly true — and that is plausibly the article's effect.** Daily N=30 beats buy-and-hold before costs over the full history on NIFTY 50 (13.61% vs 12.44% CAGR, 2000→) and NIFTY 500 (17.31% vs 13.47%, 1995→). Almost all of it is the early years: in the 2000-13 half N=30 made 17.6% vs 12.5% on NIFTY 50, in the 2013-26 half 9.8% vs 12.3% — a loss even before costs. In the mandate windows (decade, six-year, walk-forward) it loses to buy-and-hold before costs on both indices.
2. **Switching ~20 times a year is what costs it the comparison.** At ~20 switches a year, the shared cost model's delivery card plus 0.1% slippage a side costs ~4.5-5 points of CAGR a year. Even the ex-STT lower bound (an equity ETF pays far less STT than the delivery card charges) costs ~2.5 points. After costs N=30 trails buy-and-hold on CAGR in every mandate window on both headline indices (decade: −7.0pp NIFTY 50, −6.2pp NIFTY 500; six-year −6.6pp, −5.3pp). It is ahead only on the 2000-13 half (+0.6pp, +4.9pp).
3. **Tax makes it worse, as expected in India.** Every round trip is short-term (STCG at 15%, then 20% from 2024-07-23), while buy-and-hold defers LTCG to the end. Decade after tax: 3.27% vs 11.16% (NIFTY 50), 5.23% vs 12.47% (NIFTY 500).
4. **What it does deliver is a smaller drawdown.** Max drawdown falls 59% → 34% (NIFTY 50, full), 67% → 26% (NIFTY 500, full), and 38% → ~19% on both over the decade. Under the mandate's ranking (CAGR/DD) it wins on the full histories (0.26 vs 0.21; 0.49 vs 0.20) and on NIFTY 500 six-year (0.57 vs 0.37). It is level on NIFTY 500 decade (0.37 vs 0.35), loses on NIFTY 50 decade (0.26 vs 0.31), and loses clearly on the 2021-26 verification half on both (0.12 vs 0.51; 0.35 vs 0.60). Not a consistent win, even on the mandate's own measure.
5. **The episodes show how it behaves.** It gets out early — within days of the 2008 and 2020 peaks, a few percent off the top. It then whipsaws on the way back (24 round trips from the 2008 peak to NIFTY 50's recovery, 61 for NIFTY 500) and is back in for good only well above the trough. In 2024-26 it went to cash on 2024-10-04. On NIFTY 50 it whipsawed 12 times before re-entering on 2025-10-07; on NIFTY 500 it is still out. Its worst years against the index are strong recovery years (2007, 2009, 2012, 2017, 2021), when it sat in cash or kept switching while the index ran. N=30 beat buy-and-hold in only 8 of 27 calendar years on NIFTY 50 and 12 of 32 on NIFTY 500.
6. **Is 30 special? No.** On NIFTY 50, N=30 ranks 4th-5th of 7 on CAGR/DD in every window; N=100 is the best length on most windows and N=200 on some. On NIFTY 500 it ranks 2nd-3rd on the long windows and 1st only on the 2021-26 verification half, where no N beat buy-and-hold on either measure. The walk-forward choices on the first halves (N=50; N=100 / N=20 on the long split) do not beat buy-and-hold on the second halves.
7. **Slower reading of the same signal is the robust part — for drawdown, not for return.** Reading the signal weekly or monthly, or adding a ±1-2% band, cuts switching 2-8x. Of the 18 band/cadence variants (N=30 and N=200), 16-18 beat buy-and-hold on CAGR/DD in every window shown. Only 0-3 of 18 beat it on CAGR after costs, and **none** beats it on CAGR after tax in any window where tax is modelled. A variant that "wins" on CAGR in one window (e.g. monthly N=30 on NIFTY 50 six-year, 13.0% vs 11.7%) is one of 18 looked at and is a different variant on the next window. A band or cadence picked from these tables would be a fitted parameter, not evidence.
8. **Midcap 150 / Smallcap 250 (secondary).** Over 2006→, N=30 does beat buy-and-hold after costs (19.3% vs 15.2%; 19.1% vs 13.6%), with drawdowns of 18-23% against 73-76%. Two cautions apply. These indices' pre-launch history is back-calculated by NSE. And short-horizon trend rules on small/mid-cap indices are flattered by stale prices in thin constituents: that makes the index autocorrelated, and an ETF cannot actually capture the effect. On the decade and six-year windows N=30 loses after costs on both (−3.1 / −5.4pp; −2.4 / −5.3pp). After tax it is level with buy-and-hold even over 2006→ (14.6% vs 14.8%; 13.8% vs 13.2%).

**Data notes.** The `index_name` case variant ('NIFTY 50' on 2025-12-23/24, 'Nifty 50' elsewhere; the same for every index) is a label on the only point for those sessions. There is no second value to disagree with, and the daily moves across those sessions are ordinary. The lake has no FBIL overnight series (only FBIL FX reference rates), so cash earns repo − 50 bp, the repo's convention. Before 2006 that is the 2006-01-01 rate carried back, which likely understates 1995-2001 cash returns.

**What N=30 says today (2026-10-13 close).** Cash on NIFTY 50 (1.95% under its 30-session average, out since the 2026-08-26 close) and on NIFTY 500 (out since 2026-08-31). Midcap 150 and Smallcap 250 are also cash (their series end 2026-10-01). Of the seven lengths on NIFTY 50 / 500, only N=10 is invested, and only from the 2026-10-13 close.
