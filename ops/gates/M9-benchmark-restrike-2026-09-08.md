# M9.4 — Every reported benchmark figure, re-struck against the published NIFTY 50 TRI

*2026-09-08. Post-processing over the committed reports and the lake: the two benchmark legs were
computed through the sweep's own code path, and every portfolio figure was read out of the report
named in its section heading rather than recomputed. **No strategy was re-run**, and no figure in
any historical report has been overwritten. Each of those reports now carries a banner pointing
here; their own numbers stand as they were generated, against the benchmark they were generated
against.*

The published NIFTY total-return history landed in the authoritative lake on 2026-09-08
(`[M3.9.b]`, `ops/runbooks/benchmark-tri.md`). Until then `_resolve_benchmark` fell back to an L1
proxy — an equal-weight basket of the 50 most-liquid names on the window's first session, seeded to
1000 — and every excess figure this repo has published was struck against that. This document
re-strikes all of them.

## Why this is arithmetic and not a re-run

The benchmark leg is a function of the window and the series, and of nothing else:

- **One external cashflow.** A run deposits the opening capital on `first_session` and never again
  (`backtest/run.py:1492` — *"the one external cashflow: the opening capital"*).
  `PortfolioBook._benchmark_xirr` replays exactly that stream into the index
  (`backtest/accounting.py:519`), so the benchmark XIRR is the index's own money-weighted return
  over the window and is **identical for every arm**. `backtest/sweep.py:475` relies on this
  already: it takes the figure from the first arm that runs and prints it once for the whole table.
- **The benchmark never reaches a decision.** `_resolve_benchmark`'s series is consumed only by
  `compare_to_benchmarks` at the terminal valuation and by the report's provenance flags
  (`backtest/run.py:1531`, `:1670`, `:2879`, `:3020`, `:3774`, `:4527`). Nothing in the signal,
  sizing, exit or fill path reads it.
- **The regime overlay is not the benchmark.** `_RegimeSource` (`backtest/run.py:794`) builds its
  *own* broad-market L1 basket for the 200-session trend filter. It resolves nothing and would not
  have changed had the published TRI been on disk. The two are the same *kind* of proxy, which is
  why the reports describe them together, but they are separate objects and landing the TRI moves
  neither the regime signal nor any position it gated.

So every portfolio XIRR, drawdown, round-trip count, cost and **rank** below is unchanged, and the
only figures that move are the benchmark and the excess. M9.4's own re-strike is the empirical
check on that claim: regenerated at the identical window it produced a byte-identical run digest
(`dba85e3b…`, commit `95326d2`), the portfolio leg provably untouched.

**Total re-run cost of this document: zero.** Nothing here required a sweep, a walk-forward or a
lake pass.

## The two benchmark legs, and how they were validated

Computed on the authoritative lake through the sweep's own code path — `_reserve_fill_headroom` for
the window, `_build_benchmark_tri` for the proxy, `read_tri_series(..., method="published")` for the
real series, `PortfolioBook._benchmark_xirr` for both legs.

| Window | Sessions | Proxy (what was reported) | Published NIFTY 50 TRI | Bias in the proxy |
| --- | ---: | ---: | ---: | ---: |
| 2016-09-02 → 2026-08-31 | 2470 | 8.56% | **11.92%** | understated by 3.37 pp/yr |
| 2019-07-01 → 2026-08-31 | 1773 | 11.18% | **11.70%** | understated by 0.52 pp/yr |
| 2016-09-02 → 2021-08-31 (M12.3 selection) | 1235 | 5.13% | **15.65%** | understated by 10.53 pp/yr |
| 2021-09-01 → 2026-08-31 (M12.3 verification) | 1235 | 11.58% | **8.39%** | **over**stated by 3.19 pp/yr |

Three independent checks that this reconstruction is the reports' own arithmetic and not a
lookalike:

1. The proxy legs reproduce the published headline figures **exactly** — 8.56% and 11.18% — from
   the same code on the same lake.
2. The session counts reproduce exactly — 2470 and 1773.
3. The decade's published leg, 11.92%, is the figure M9.4 arrived at independently on 2026-09-08
   (commit `95326d2`), against the same window.

And every one of the 92 Excess cells in the two M12.2 sweeps satisfies `Excess = XIRR − proxy` to
the last printed digit, asserted rather than assumed while the tables below were generated. That is
what licenses re-striking them by subtraction.

**The bias is not a constant offset and not even one-directional.** Over the decade the proxy
understated the market by 337 bps a year; over the M12.3 selection window by 1053 bps; over the
verification window it *overstated* it by 319 bps. A price-return basket of the 50 most-liquid
names seeded to 1000 tracks the market's total return by coincidence, and the coincidence changes
sign. No report's excess figure can be repaired by adding back "roughly the dividend yield", which
is what several of them invite the reader to do.

## What does **not** change: the M12.3 verdict

`ops/gates/M12-strategy-verdict.md` ranks arms on **XIRR ÷ max drawdown** — two portfolio
quantities — and tests the owner's bar against **portfolio XIRR**. Neither touches the benchmark.
So:

- The chosen arm is still **M10.7 + regime gate** (selection 14.68% / 19.13% DD = 0.77).
- Its verification-window result is still 16.01% against 17.25% (0.93), rank 4 of 23.
- Every selection and verification rank in that table stands, and so does the 0.372 Spearman.
- **The answer to the >25% bar is unchanged**: no arm cleared 25% on either window (best 14.68%
  selection, 18.55% verification), and the six-year sweep's M10.7 composite still clears it at
  27.91% on the ₹1 crore floor.

The verdict's own caveat list is what needs amending, not its numbers. It warns that *"an error
common to both — … the price-return benchmark proxy — moves selection and verification together and
is invisible to this comparison."* Measured, that is wrong in an interesting way: the proxy error
did **not** move the two windows together. It understated the selection window by 1053 bps and
overstated the verification window by 319 bps — a 1372 bp swing across the split. Had the verdict
ranked on excess rather than on return per drawdown, that alone would have rewritten it. It ranks on
neither, so it survives; but it survives by a design choice, not because the error was benign.

## M12.2 — the strategy sweeps, re-struck

Ranks and every portfolio column are as published. Only the two right-hand columns move.

#### decade — ₹1 crore/day liquidity floor

| # | Strategy | XIRR | Max DD | XIRR/DD | Excess vs proxy (as published) | Excess vs published TRI | Δ | sign |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | Swing composite (M10.7) | 17.11% | 24.74% | 0.69 | 8.56% | **5.19%** | -3.37 |  |
| 2 | M10.7 + regime gate | 14.12% | 20.76% | 0.68 | 5.57% | **2.20%** | -3.37 |  |
| 3 | Momentum v2, all on (M9.5) | 14.74% | 23.04% | 0.64 | 6.18% | **2.82%** | -3.36 |  |
| 4 | M10.7 + delivery acceleration | 14.47% | 24.53% | 0.59 | 5.91% | **2.55%** | -3.36 |  |
| 5 | M10.7 + 1-month trend | 13.91% | 32.94% | 0.42 | 5.36% | **1.99%** | -3.37 |  |
| 6 | Short composite, 3-month holds | 9.65% | 27.36% | 0.35 | 1.10% | **-2.27%** | -3.37 | **flips +→−** |
| 7 | Short composite + regime gate | 6.35% | 20.39% | 0.31 | -2.21% | **-5.57%** | -3.36 |  |
| 8 | Short composite + reversal | 8.72% | 35.63% | 0.24 | 0.16% | **-3.20%** | -3.36 | **flips +→−** |
| 9 | Naive momentum (M4.10) | 11.52% | 49.36% | 0.23 | 2.96% | **-0.40%** | -3.36 | **flips +→−** |
| 10 | Short composite | 7.34% | 40.56% | 0.18 | -1.21% | **-4.58%** | -3.37 |  |
| 11 | Breakout | 6.67% | 41.87% | 0.16 | -1.89% | **-5.25%** | -3.36 |  |
| 12 | Short composite, 1-month holds | 6.14% | 47.17% | 0.13 | -2.42% | **-5.78%** | -3.36 |  |
| 13 | Short composite, top-10 + regime | 3.64% | 30.92% | 0.12 | -4.92% | **-8.28%** | -3.36 |  |
| 14 | Short composite, top-5 | 4.60% | 47.22% | 0.10 | -3.96% | **-7.32%** | -3.36 |  |
| 15 | Short composite, top-10 | 4.37% | 46.05% | 0.09 | -4.19% | **-7.55%** | -3.36 |  |
| 16 | Short composite + regime + low-vol | 1.83% | 21.17% | 0.09 | -6.72% | **-10.09%** | -3.37 |  |
| 17 | Mean proximity (50d) | 4.91% | 61.35% | 0.08 | -3.64% | **-7.01%** | -3.37 |  |
| 18 | Short composite + low-vol leg | 2.44% | 31.04% | 0.08 | -6.12% | **-9.48%** | -3.36 |  |
| 19 | Trend: 1-month | 3.11% | 62.37% | 0.05 | -5.44% | **-8.81%** | -3.37 |  |
| 20 | Delivery acceleration | 2.45% | 50.39% | 0.05 | -6.11% | **-9.47%** | -3.36 |  |
| 21 | Short composite, 2-week holds | 2.11% | 48.30% | 0.04 | -6.45% | **-9.81%** | -3.36 |  |
| 22 | Reversal: 5-day losers | -0.81% | 54.90% | -0.01 | -9.37% | **-12.73%** | -3.36 |  |
| 23 | Turnover expansion | -2.17% | 53.71% | -0.04 | -10.73% | **-14.09%** | -3.36 |  |

#### decade — ₹10 crore/day liquidity floor

| # | Strategy | XIRR | Max DD | XIRR/DD | Excess vs proxy (as published) | Excess vs published TRI | Δ | sign |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | Momentum v2, all on (M9.5) | 13.49% | 24.87% | 0.54 | 4.94% | **1.57%** | -3.37 |  |
| 2 | Swing composite (M10.7) | 11.98% | 26.70% | 0.45 | 3.43% | **0.06%** | -3.37 |  |
| 3 | M10.7 + regime gate | 9.16% | 23.02% | 0.40 | 0.60% | **-2.76%** | -3.36 | **flips +→−** |
| 4 | M10.7 + 1-month trend | 11.28% | 32.18% | 0.35 | 2.73% | **-0.64%** | -3.37 | **flips +→−** |
| 5 | M10.7 + delivery acceleration | 9.48% | 27.79% | 0.34 | 0.92% | **-2.44%** | -3.36 | **flips +→−** |
| 6 | Naive momentum (M4.10) | 10.98% | 46.88% | 0.23 | 2.42% | **-0.94%** | -3.36 | **flips +→−** |
| 7 | Short composite, 3-month holds | 5.62% | 27.89% | 0.20 | -2.93% | **-6.30%** | -3.37 |  |
| 8 | Short composite + reversal | 6.05% | 31.39% | 0.19 | -2.50% | **-5.87%** | -3.37 |  |
| 9 | Short composite | 5.90% | 31.04% | 0.19 | -2.66% | **-6.02%** | -3.36 |  |
| 10 | Short composite + regime gate | 4.39% | 23.91% | 0.18 | -4.17% | **-7.53%** | -3.36 |  |
| 11 | Short composite + low-vol leg | 3.70% | 27.92% | 0.13 | -4.85% | **-8.22%** | -3.37 |  |
| 12 | Mean proximity (50d) | 6.27% | 47.60% | 0.13 | -2.29% | **-5.65%** | -3.36 |  |
| 13 | Breakout | 4.57% | 35.24% | 0.13 | -3.99% | **-7.35%** | -3.36 |  |
| 14 | Delivery acceleration | 4.25% | 40.91% | 0.10 | -4.30% | **-7.67%** | -3.37 |  |
| 15 | Short composite, 1-month holds | 3.81% | 40.94% | 0.09 | -4.75% | **-8.11%** | -3.36 |  |
| 16 | Short composite, top-10 | 3.12% | 37.54% | 0.08 | -5.44% | **-8.80%** | -3.36 |  |
| 17 | Trend: 1-month | 4.06% | 51.71% | 0.08 | -4.49% | **-7.86%** | -3.37 |  |
| 18 | Short composite + regime + low-vol | 1.42% | 25.11% | 0.06 | -7.14% | **-10.50%** | -3.36 |  |
| 19 | Short composite, top-10 + regime | 1.26% | 29.38% | 0.04 | -7.30% | **-10.66%** | -3.36 |  |
| 20 | Short composite, 2-week holds | 1.29% | 43.08% | 0.03 | -7.26% | **-10.63%** | -3.37 |  |
| 21 | Short composite, top-5 | 1.30% | 44.08% | 0.03 | -7.25% | **-10.62%** | -3.37 |  |
| 22 | Turnover expansion | -0.83% | 40.39% | -0.02 | -9.38% | **-12.75%** | -3.37 |  |
| 23 | Reversal: 5-day losers | -1.53% | 51.81% | -0.03 | -10.08% | **-13.45%** | -3.37 |  |

#### sixyear — ₹1 crore/day liquidity floor

| # | Strategy | XIRR | Max DD | XIRR/DD | Excess vs proxy (as published) | Excess vs published TRI | Δ | sign |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | Swing composite (M10.7) | 27.91% | 23.86% | 1.17 | 16.73% | **16.21%** | -0.52 |  |
| 2 | Short composite + regime gate | 13.25% | 12.07% | 1.10 | 2.07% | **1.55%** | -0.52 |  |
| 3 | Short composite, top-10 + regime | 13.70% | 13.98% | 0.98 | 2.52% | **2.00%** | -0.52 |  |
| 4 | M10.7 + regime gate | 19.42% | 20.82% | 0.93 | 8.23% | **7.72%** | -0.51 |  |
| 5 | M10.7 + delivery acceleration | 20.94% | 23.96% | 0.87 | 9.75% | **9.24%** | -0.51 |  |
| 6 | M10.7 + 1-month trend | 23.96% | 28.66% | 0.84 | 12.77% | **12.26%** | -0.51 |  |
| 7 | Trend: 1-month | 19.85% | 26.97% | 0.74 | 8.66% | **8.15%** | -0.51 |  |
| 8 | Mean proximity (50d) | 18.64% | 25.94% | 0.72 | 7.46% | **6.94%** | -0.52 |  |
| 9 | Momentum v2, all on (M9.5) | 21.19% | 31.82% | 0.67 | 10.01% | **9.49%** | -0.52 |  |
| 10 | Breakout | 14.45% | 21.82% | 0.66 | 3.27% | **2.75%** | -0.52 |  |
| 11 | Short composite + regime + low-vol | 6.94% | 11.45% | 0.61 | -4.25% | **-4.76%** | -0.51 |  |
| 12 | Naive momentum (M4.10) | 16.58% | 29.80% | 0.56 | 5.40% | **4.88%** | -0.52 |  |
| 13 | Short composite, 3-month holds | 10.86% | 19.52% | 0.56 | -0.33% | **-0.84%** | -0.51 |  |
| 14 | Short composite, 1-month holds | 15.60% | 28.49% | 0.55 | 4.42% | **3.90%** | -0.52 |  |
| 15 | Short composite + reversal | 13.40% | 24.94% | 0.54 | 2.22% | **1.70%** | -0.52 |  |
| 16 | Short composite, 2-week holds | 13.85% | 26.19% | 0.53 | 2.67% | **2.15%** | -0.52 |  |
| 17 | Short composite | 12.82% | 25.68% | 0.50 | 1.64% | **1.12%** | -0.52 |  |
| 18 | Short composite, top-10 | 10.75% | 29.04% | 0.37 | -0.43% | **-0.95%** | -0.52 |  |
| 19 | Short composite, top-5 | 10.34% | 31.66% | 0.33 | -0.84% | **-1.36%** | -0.52 |  |
| 20 | Short composite + low-vol leg | 4.89% | 20.63% | 0.24 | -6.30% | **-6.81%** | -0.51 |  |
| 21 | Delivery acceleration | 6.85% | 34.17% | 0.20 | -4.33% | **-4.85%** | -0.52 |  |
| 22 | Turnover expansion | 0.77% | 24.92% | 0.03 | -10.41% | **-10.93%** | -0.52 |  |
| 23 | Reversal: 5-day losers | 1.31% | 43.60% | 0.03 | -9.87% | **-10.39%** | -0.52 |  |

#### sixyear — ₹10 crore/day liquidity floor

| # | Strategy | XIRR | Max DD | XIRR/DD | Excess vs proxy (as published) | Excess vs published TRI | Δ | sign |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | Swing composite (M10.7) | 21.08% | 23.23% | 0.91 | 9.90% | **9.38%** | -0.52 |  |
| 2 | M10.7 + 1-month trend | 18.49% | 20.95% | 0.88 | 7.31% | **6.79%** | -0.52 |  |
| 3 | M10.7 + regime gate | 15.36% | 17.68% | 0.87 | 4.17% | **3.66%** | -0.51 |  |
| 4 | M10.7 + delivery acceleration | 18.41% | 23.09% | 0.80 | 7.23% | **6.71%** | -0.52 |  |
| 5 | Momentum v2, all on (M9.5) | 22.19% | 28.76% | 0.77 | 11.01% | **10.49%** | -0.52 |  |
| 6 | Mean proximity (50d) | 15.05% | 24.41% | 0.62 | 3.87% | **3.35%** | -0.52 |  |
| 7 | Trend: 1-month | 13.50% | 22.04% | 0.61 | 2.32% | **1.80%** | -0.52 |  |
| 8 | Short composite + regime + low-vol | 7.16% | 12.02% | 0.60 | -4.02% | **-4.54%** | -0.52 |  |
| 9 | Naive momentum (M4.10) | 18.44% | 34.44% | 0.54 | 7.26% | **6.74%** | -0.52 |  |
| 10 | Short composite + reversal | 12.34% | 23.11% | 0.53 | 1.16% | **0.64%** | -0.52 |  |
| 11 | Short composite, top-10 + regime | 7.25% | 14.26% | 0.51 | -3.94% | **-4.45%** | -0.51 |  |
| 12 | Short composite + regime gate | 6.85% | 18.77% | 0.37 | -4.33% | **-4.85%** | -0.52 |  |
| 13 | Short composite | 7.98% | 22.16% | 0.36 | -3.20% | **-3.72%** | -0.52 |  |
| 14 | Short composite, top-5 | 8.14% | 23.23% | 0.35 | -3.04% | **-3.56%** | -0.52 |  |
| 15 | Short composite, 3-month holds | 6.67% | 20.71% | 0.32 | -4.51% | **-5.03%** | -0.52 |  |
| 16 | Breakout | 7.30% | 23.51% | 0.31 | -3.89% | **-4.40%** | -0.51 |  |
| 17 | Short composite, top-10 | 7.45% | 24.43% | 0.30 | -3.73% | **-4.25%** | -0.52 |  |
| 18 | Short composite + low-vol leg | 6.12% | 21.66% | 0.28 | -5.06% | **-5.58%** | -0.52 |  |
| 19 | Short composite, 1-month holds | 8.09% | 30.06% | 0.27 | -3.09% | **-3.61%** | -0.52 |  |
| 20 | Short composite, 2-week holds | 6.59% | 30.03% | 0.22 | -4.59% | **-5.11%** | -0.52 |  |
| 21 | Delivery acceleration | 5.46% | 29.21% | 0.19 | -5.72% | **-6.24%** | -0.52 |  |
| 22 | Turnover expansion | 2.84% | 25.65% | 0.11 | -8.34% | **-8.86%** | -0.52 |  |
| 23 | Reversal: 5-day losers | -2.46% | 51.91% | -0.05 | -13.64% | **-14.16%** | -0.52 |  |

## The single-strategy reports, re-struck

### M4.10 — naive momentum (`ops/gates/M4-momentum-report.md`)

Window 2016-09-02 → 2026-08-31. The report's portfolio XIRR is 11.70%; M9.4's regeneration of the same policy on the current lake gives 10.92% and is the figure to prefer — the row below re-strikes the benchmark leg of *this* report, unchanged portfolio and all.

| Strategy | Portfolio XIRR | Excess vs proxy (as published) | Excess vs published TRI | Δ | sign |
| --- | ---: | ---: | ---: | ---: | --- |
| Portfolio (naive momentum, costs included) | 11.70% | 3.14% | **-0.22%** | -3.36 | **flips +→−** |

### M9.5 — the momentum-v2 toggle table (`ops/gates/M9-momentum-v2-report.md`)

Window 2016-09-02 → 2026-08-31. Ten configurations; the point of the table is the *relative* effect of each toggle, which is untouched — every row moves by the same 3.36 pp.

| Strategy | Portfolio XIRR | Excess vs proxy (as published) | Excess vs published TRI | Δ | sign |
| --- | ---: | ---: | ---: | ---: | --- |
| Naive (all off) | 11.56% | 3.00% | **-0.36%** | -3.36 | **flips +→−** |
| + 12-1 momentum | 14.92% | 6.37% | **3.00%** | -3.37 |  |
| + Turnover banding | 11.07% | 2.51% | **-0.85%** | -3.36 | **flips +→−** |
| + Regime filter | 9.80% | 1.24% | **-2.12%** | -3.36 | **flips +→−** |
| + Vol-scaled weights | 9.84% | 1.29% | **-2.08%** | -3.37 | **flips +→−** |
| + Redeploy proceeds next session | 13.71% | 5.16% | **1.79%** | -3.37 |  |
| All on (four M9.5 toggles) | 12.02% | 3.46% | **0.10%** | -3.36 |  |
| All on + redeploy | 13.04% | 4.49% | **1.12%** | -3.37 |  |
| + Vol target 15% | 6.39% | -2.17% | **-5.53%** | -3.36 |  |
| All on + redeploy + vol target 15% | 14.74% | 6.18% | **2.82%** | -3.36 |  |

### M10.7 — the swing composite (`ops/gates/M10-swing-composite-report.md`)

Window 2016-09-02 → 2026-08-31. The parameter arms below the first three are the report's own sensitivity sweep; all of them shift by the same 3.36 pp.

| Strategy | Portfolio XIRR | Excess vs proxy (as published) | Excess vs published TRI | Δ | sign |
| --- | ---: | ---: | ---: | ---: | --- |
| Naive momentum (M4.10) | 11.52% | 2.96% | **-0.40%** | -3.36 | **flips +→−** |
| Momentum v2, all on (M9.5) | 14.74% | 6.18% | **2.82%** | -3.36 |  |
| Swing composite (default) | 17.11% | 8.56% | **5.19%** | -3.37 |  |
| Cadence: weekly | 18.18% | 9.62% | **6.26%** | -3.36 |  |
| Cadence: monthly | 17.10% | 8.55% | **5.18%** | -3.37 |  |
| Band: 1.5x top_n | 13.63% | 5.07% | **1.71%** | -3.36 |  |
| Band: 5x top_n | 17.10% | 8.54% | **5.18%** | -3.36 |  |
| Exit: no trailing stop | 17.66% | 9.11% | **5.74%** | -3.37 |  |
| Exit: 12% trailing stop | 14.18% | 5.62% | **2.26%** | -3.36 |  |
| Exit: max hold 21 sessions | 16.42% | 7.86% | **4.50%** | -3.36 |  |
| Signal: delivery only | 14.99% | 6.43% | **3.07%** | -3.36 |  |
| Signal: no delivery leg | 9.25% | 0.70% | **-2.67%** | -3.37 | **flips +→−** |

### M10.3 — sector rotation (`ops/gates/M10-sector-rotation-report.md`)

Window 2016-09-02 → 2026-08-31. **The comparison this report exists for is rotation vs plain momentum on the same universe (+0.27 pp), and that is a difference of two portfolio legs — it does not move at all.** Only the market column does.

| Strategy | Portfolio XIRR | Excess vs market vs proxy (as published) | Excess vs market vs published TRI | Δ | sign |
| --- | ---: | ---: | ---: | ---: | --- |
| Sector rotation | 9.69% | 1.14% | **-2.23%** | -3.37 | **flips +→−** |
| Plain momentum (same universe) | 9.42% | 0.87% | **-2.50%** | -3.37 | **flips +→−** |

### M10.6 — the fundamentals signal (`ops/gates/M10-fundamentals-signal-report.md`)

Window 2019-07-01 → 2026-08-31 — the short window, where the proxy's bias is smallest (0.52 pp). No sign changes here.

| Strategy | Portfolio XIRR | Excess vs market vs proxy (as published) | Excess vs market vs published TRI | Δ | sign |
| --- | ---: | ---: | ---: | ---: | --- |
| Fundamentals: VALUE | 21.53% | 10.34% | **9.83%** | -0.51 |  |
| Fundamentals: GROWTH | 17.55% | 6.37% | **5.85%** | -0.52 |  |
| Fundamentals: QUALITY_VALUE | 12.82% | 1.63% | **1.12%** | -0.51 |  |
| Fundamentals: MOMENTUM_VALUE | 12.25% | 1.07% | **0.55%** | -0.52 |  |
| Momentum: naive (all off) | 16.29% | 5.10% | **4.59%** | -0.51 |  |
| Momentum: v2 all-on | 17.13% | 5.94% | **5.43%** | -0.51 |  |

### X2 — the daily forecast policy (`ops/gates/X2-forecast-daily-report.md`)

Window 2019-07-01 → 2026-08-31. The forecast policy was already the worst row on the page by a wide margin; it gets worse by 0.52 pp and the conclusion is not in doubt either way.

| Strategy | Portfolio XIRR | Excess vs proxy (as published) | Excess vs published TRI | Δ | sign |
| --- | ---: | ---: | ---: | ---: | --- |
| Forecast, daily | -2.71% | -13.89% | **-14.41%** | -0.52 |  |
| Momentum: naive (all off) | 16.29% | 5.10% | **4.59%** | -0.51 |  |
| Momentum: v2 all-on | 17.13% | 5.94% | **5.43%** | -0.51 |  |

## What the re-strike actually changed

**Sixteen reported outperformances become underperformances. None goes the other way.** Every one
of them sits on the 2016-09-02 → 2026-08-31 decade window, where the proxy's 337 bp annual
understatement is larger than the excess it was compared against:

| Report | Row | Was | Is |
| --- | --- | ---: | ---: |
| M4.10 | naive momentum | +3.14% | **−0.22%** |
| M9.5 | Naive (all off) | +3.00% | **−0.36%** |
| M9.5 | + Turnover banding | +2.51% | **−0.85%** |
| M9.5 | + Regime filter | +1.24% | **−2.12%** |
| M9.5 | + Vol-scaled weights | +1.29% | **−2.08%** |
| M10.7 | Naive momentum (M4.10) | +2.96% | **−0.40%** |
| M10.7 | Signal: no delivery leg | +0.70% | **−2.67%** |
| M10.3 | Sector rotation | +1.14% | **−2.23%** |
| M10.3 | Plain momentum (same universe) | +0.87% | **−2.50%** |
| M12.2 decade, ₹1 cr | Short composite, 3-month holds | +1.10% | **−2.27%** |
| M12.2 decade, ₹1 cr | Short composite + reversal | +0.16% | **−3.20%** |
| M12.2 decade, ₹1 cr | Naive momentum (M4.10) | +2.96% | **−0.40%** |
| M12.2 decade, ₹10 cr | M10.7 + regime gate | +0.60% | **−2.76%** |
| M12.2 decade, ₹10 cr | M10.7 + 1-month trend | +2.73% | **−0.64%** |
| M12.2 decade, ₹10 cr | M10.7 + delivery acceleration | +0.92% | **−2.44%** |
| M12.2 decade, ₹10 cr | Naive momentum (M4.10) | +2.42% | **−0.94%** |

Two consequences worth stating plainly, because no individual table above says either:

**Over the full decade, at the liquidity floor the M12.2 report itself says to plan against
(₹10 crore/day), only two of twenty-three arms beat the real NIFTY 50 total-return index** —
Momentum v2 all-on by 1.57% and the M10.7 swing composite by 0.06%. Against the proxy, six did.
Every arm the M10.3 sector-rotation study measured now trails the index, and so does naive momentum
in all four places it appears.

**The M10.3 conclusion survives intact and is a good illustration of what a benchmark error can and
cannot damage.** Its whole claim is rotation *vs plain momentum on the same universe* — +0.27 pp —
which is a difference between two portfolio legs. Subtracting a different benchmark from both leaves
it exactly where it was. What changes is only the sentence about beating the market, which was never
that report's finding.

## Provenance

- Benchmark: the **published** NIFTY 50 total-return series, `L1/benchmark_tri/…/nifty50.published.parquet`,
  6,764 points 1999-06-30 → 2026-09-07, re-derived from
  `L0/nifty_tri_history/2026-09-08/tri_nifty50_19900401_20260908.json`
  (sha256 `575c311a686f9723890f91983be3f09ba96233aa9a5fcbd8bf9688813fb1792d`).
  Spot-checked against the owner's independently recorded level for 2026-03-30: 33655.43, exact.
- Proxy: `_build_benchmark_tri` on the same lake, reproduced to the last printed digit for both
  windows.
- Every portfolio figure is quoted from the committed report named in its section heading; none was
  recomputed and none was changed.

## What this document cannot be asked to prove

- **It re-strikes a benchmark; it does not re-validate a strategy.** Ranks, drawdowns and costs are
  reproduced from reports whose own caveats still apply in full — one lake, one cost model, one
  fill model, signals selected inside this repo.
- **The theme leg is still the broad benchmark.** `compare_to_benchmarks` is called with
  `theme=benchmark` everywhere (`backtest/run.py:1533` and its four siblings), so no report here has
  ever carried a genuine sector-theme comparison, and this one does not add one. The lake now holds
  published NIFTY IT and NIFTY CPSE total-return series, which is what a real theme leg would read.
- **Nothing here is out-of-sample.** Re-scoring the same runs against a better benchmark makes the
  numbers honest, not new.
