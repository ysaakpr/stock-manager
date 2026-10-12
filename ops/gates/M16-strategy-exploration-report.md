# M16 — strategy exploration (gate report)

> **Generated** by `uv run python -m backtest.m16_report` from the saved runs listed below; nothing in the tables is typed by hand. The section after the marker at the end is hand-written analysis and is labelled as such.

Pre-registration: `ops/studies/preregistration-m16-2026-10-08.md` (arms, grid, rule and N fixed before any run).

| Run directory | Universe | Commit | Lake last session | Units | Arms |
| --- | --- | --- | --- | --- | --- |
| `m16-floor-3aed926-frz1010` | turnover_floor | `3aed926412` | 2026-10-09 | walk-forward, decade, six-year | 7 |
| `m16-fund-floor-3aed926-frz1010` | turnover_floor | `3aed926412` | 2026-10-09 | six-year, wf-verification | 4 |
| `m16-nifty500-3aed926-frz1010` | nifty500 | `3aed926412` | 2026-10-09 | walk-forward, decade, six-year | 7 |
| `m16-fund-nifty500-3aed926-frz1010` | nifty500 | `3aed926412` | 2026-10-09 | six-year, wf-verification | 4 |

**A6** (50/50 blend of D13 and M10.7 (A6)) is built here from D13's and M10.7's saved runs: half of each full ₹10 lakh run, never rebalanced between the halves; NAV the mean of the two paths, XIRR on the halved cashflows, max DD from the blended path, charges halved, trades and rail refusals summed. A8's floors would bind a little differently on two ₹5 lakh books.

**A8 min-holdings refusals** are all sells A8's 8-name floor refused over the run, on any session; **all rail blocks** list every rail that refused an order, with counts.

## Results — floor-only universe

### decade (2016-09-01 → 2026-08-31)

**₹1 cr/day floor**

| # | Strategy | XIRR | Max DD | **XIRR/DD** | Δ XIRR vs D13 | Δ DD vs D13 | Excess vs NIFTY 50 TRI | Trades | Charges | A8 min-holdings refused sells | All rail blocks | >25%? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 50/50 blend of D13 and M10.7 (A6) | 25.37% | 22.08% | **1.15** | +0.30pp | -6.63pp | 13.40% | 6282 | ₹2.85L | 245 | MAX_SECTOR 1363, MIN_HOLDINGS 245 | yes |
| 2 | Swing composite (M10.7) | 25.66% | 23.56% | **1.09** | +0.60pp | -5.15pp | 13.70% | 4047 | ₹3.50L | 4 | MAX_SECTOR 899, MIN_HOLDINGS 4 | yes |
| 3 | D13 + industry gate (A2) *(diluted: informational, decides nothing)* | 21.06% | 20.28% | **1.04** | -4.00pp | -8.43pp | 9.09% | 1749 | ₹1.61L | 264 | MAX_SECTOR 1332, MIN_HOLDINGS 264 | no |
| 4 | Momentum v2, all on (M9.5) | 22.64% | 24.04% | **0.94** | -2.42pp | -4.67pp | 10.68% | 2830 | ₹2.15L | 237 | MAX_SECTOR 336, MIN_HOLDINGS 237 | no |
| 5 | D13 + absolute momentum (A1) | 25.06% | 28.71% | **0.87** | +0.00pp | +0.00pp | 13.10% | 2235 | ₹2.20L | 241 | MAX_SECTOR 464, MIN_HOLDINGS 241 | yes |
| 6 | **Momentum v2, D13 paper config** | 25.06% | 28.71% | **0.87** | — | — | 13.10% | 2235 | ₹2.20L | 241 | MAX_SECTOR 464, MIN_HOLDINGS 241 | yes |
| 7 | Residual momentum v2 (A3) | 18.11% | 20.83% | **0.87** | -6.96pp | -7.88pp | 6.14% | 1440 | ₹1.04L | 257 | MAX_POSITION 1, MAX_SECTOR 532, MIN_HOLDINGS 257 | no |
| 8 | Pure low volatility (A7) | 7.71% | 16.64% | **0.46** | -17.35pp | -12.07pp | -4.25% | 1117 | ₹0.60L | 2 | MAX_SECTOR 1217, MIN_HOLDINGS 2 | no |

**₹10 cr/day floor**

| # | Strategy | XIRR | Max DD | **XIRR/DD** | Δ XIRR vs D13 | Δ DD vs D13 | Excess vs NIFTY 50 TRI | Trades | Charges | A8 min-holdings refused sells | All rail blocks | >25%? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 50/50 blend of D13 and M10.7 (A6) | 23.70% | 21.48% | **1.10** | -0.44pp | -8.57pp | 11.73% | 5251 | ₹2.28L | 234 | MAX_SECTOR 148, MIN_HOLDINGS 234 | no |
| 2 | Swing composite (M10.7) | 23.24% | 22.62% | **1.03** | -0.90pp | -7.43pp | 11.28% | 3236 | ₹2.78L | 0 | MAX_SECTOR 64 | no |
| 3 | Residual momentum v2 (A3) | 18.90% | 22.71% | **0.83** | -5.24pp | -7.34pp | 6.93% | 1582 | ₹1.14L | 232 | MAX_POSITION 1, MAX_SECTOR 138, MIN_HOLDINGS 232 | no |
| 4 | D13 + absolute momentum (A1) | 24.14% | 30.05% | **0.80** | +0.00pp | +0.00pp | 12.17% | 2015 | ₹1.78L | 234 | MAX_SECTOR 84, MIN_HOLDINGS 234 | no |
| 5 | **Momentum v2, D13 paper config** | 24.14% | 30.05% | **0.80** | — | — | 12.17% | 2015 | ₹1.78L | 234 | MAX_SECTOR 84, MIN_HOLDINGS 234 | no |
| 6 | Momentum v2, all on (M9.5) | 22.75% | 30.14% | **0.75** | -1.39pp | +0.09pp | 10.78% | 2627 | ₹1.86L | 236 | MAX_SECTOR 53, MIN_HOLDINGS 236 | no |
| 7 | D13 + industry gate (A2) *(diluted: informational, decides nothing)* | 16.25% | 31.07% | **0.52** | -7.89pp | +1.02pp | 4.29% | 1643 | ₹1.24L | 230 | MAX_POSITION 1, MAX_SECTOR 487, MIN_HOLDINGS 230 | no |
| 8 | Pure low volatility (A7) | 10.29% | 22.26% | **0.46** | -13.85pp | -7.80pp | -1.68% | 1688 | ₹0.95L | 0 | MAX_ORDER_VALUE 17, MAX_SECTOR 312 | no |

### six-year (2019-07-01 → 2026-08-31)

**₹1 cr/day floor**

| # | Strategy | XIRR | Max DD | **XIRR/DD** | Δ XIRR vs D13 | Δ DD vs D13 | Excess vs NIFTY 50 TRI | Trades | Charges | A8 min-holdings refused sells | All rail blocks | >25%? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Momentum v2, all on (M9.5) | 28.42% | 16.61% | **1.71** | -0.64pp | -2.23pp | 16.72% | 2044 | ₹1.52L | 215 | MAX_SECTOR 178, MIN_HOLDINGS 215 | yes |
| 2 | D13 + absolute momentum (A1) | 29.07% | 18.84% | **1.54** | +0.00pp | +0.00pp | 17.37% | 1562 | ₹1.45L | 220 | MAX_SECTOR 280, MIN_HOLDINGS 220 | yes |
| 3 | **Momentum v2, D13 paper config** | 29.07% | 18.84% | **1.54** | — | — | 17.37% | 1562 | ₹1.45L | 220 | MAX_SECTOR 280, MIN_HOLDINGS 220 | yes |
| 4 | 50/50 blend of D13 and M10.7 (A6) | 32.93% | 22.02% | **1.50** | +3.86pp | +3.19pp | 21.23% | 4636 | ₹2.17L | 233 | MAX_SECTOR 995, MIN_HOLDINGS 233 | yes |
| 5 | D13 + industry gate (A2) *(diluted: informational, decides nothing)* | 24.93% | 16.70% | **1.49** | -4.13pp | -2.14pp | 13.23% | 1269 | ₹1.10L | 236 | MAX_SECTOR 835, MIN_HOLDINGS 236 | no |
| 6 | M10.7 + earnings-surprise leg (A5) | 37.52% | 25.61% | **1.46** | +8.45pp | +6.78pp | 25.82% | 3449 | ₹3.31L | 6 | MAX_SECTOR 353, MIN_HOLDINGS 6 | yes |
| 7 | Swing composite (M10.7) | 36.20% | 25.61% | **1.41** | +7.13pp | +6.78pp | 24.50% | 3074 | ₹2.88L | 13 | MAX_SECTOR 715, MIN_HOLDINGS 13 | yes |
| 8 | Residual momentum v2 (A3) | 19.56% | 14.64% | **1.34** | -9.51pp | -4.20pp | 7.86% | 931 | ₹0.58L | 251 | MAX_SECTOR 412, MIN_HOLDINGS 251 | no |
| 9 | D13 + profitability filter (A4) | 23.88% | 22.16% | **1.08** | -5.19pp | +3.33pp | 12.18% | 1445 | ₹1.15L | 223 | MAX_SECTOR 104, MIN_HOLDINGS 223 | no |
| 10 | Pure low volatility (A7) | 9.15% | 16.97% | **0.54** | -19.92pp | -1.87pp | -2.55% | 579 | ₹0.30L | 0 | MAX_SECTOR 1441 | no |

**₹10 cr/day floor**

| # | Strategy | XIRR | Max DD | **XIRR/DD** | Δ XIRR vs D13 | Δ DD vs D13 | Excess vs NIFTY 50 TRI | Trades | Charges | A8 min-holdings refused sells | All rail blocks | >25%? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | D13 + absolute momentum (A1) | 31.08% | 19.87% | **1.56** | +0.00pp | +0.00pp | 19.38% | 1478 | ₹1.31L | 214 | MAX_SECTOR 18, MIN_HOLDINGS 214 | yes |
| 2 | **Momentum v2, D13 paper config** | 31.08% | 19.87% | **1.56** | — | — | 19.38% | 1478 | ₹1.31L | 214 | MAX_SECTOR 18, MIN_HOLDINGS 214 | yes |
| 3 | Momentum v2, all on (M9.5) | 30.21% | 21.31% | **1.42** | -0.87pp | +1.44pp | 18.51% | 2027 | ₹1.48L | 215 | MAX_SECTOR 15, MIN_HOLDINGS 215 | yes |
| 4 | 50/50 blend of D13 and M10.7 (A6) | 29.18% | 21.31% | **1.37** | -1.90pp | +1.44pp | 17.48% | 3798 | ₹1.59L | 215 | MAX_SECTOR 68, MIN_HOLDINGS 215 | yes |
| 5 | Residual momentum v2 (A3) | 25.78% | 19.66% | **1.31** | -5.30pp | -0.21pp | 14.08% | 1163 | ₹0.88L | 213 | MAX_POSITION 3, MAX_SECTOR 95, MIN_HOLDINGS 213 | yes |
| 6 | D13 + profitability filter (A4) | 23.27% | 18.49% | **1.26** | -7.82pp | -1.38pp | 11.56% | 1175 | ₹0.90L | 219 | MAX_SECTOR 39, MIN_HOLDINGS 219 | no |
| 7 | Swing composite (M10.7) | 27.10% | 22.95% | **1.18** | -3.98pp | +3.08pp | 15.40% | 2320 | ₹1.88L | 1 | MAX_SECTOR 50, MIN_HOLDINGS 1 | yes |
| 8 | M10.7 + earnings-surprise leg (A5) | 25.43% | 23.07% | **1.10** | -5.66pp | +3.20pp | 13.72% | 2159 | ₹1.68L | 1 | MAX_SECTOR 4, MIN_HOLDINGS 1 | yes |
| 9 | D13 + industry gate (A2) *(diluted: informational, decides nothing)* | 22.69% | 22.06% | **1.03** | -8.40pp | +2.19pp | 10.99% | 1249 | ₹1.01L | 212 | MAX_SECTOR 283, MIN_HOLDINGS 212 | no |
| 10 | Pure low volatility (A7) | 12.12% | 19.64% | **0.62** | -18.97pp | -0.23pp | 0.41% | 1270 | ₹0.70L | 0 | MAX_ORDER_VALUE 18, MAX_SECTOR 314 | no |

### wf-selection (2016-09-01 → 2021-08-31)

**₹1 cr/day floor**

| # | Strategy | XIRR | Max DD | **XIRR/DD** | Δ XIRR vs D13 | Δ DD vs D13 | Excess vs NIFTY 50 TRI | Trades | Charges | A8 min-holdings refused sells | All rail blocks | >25%? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | D13 + industry gate (A2) *(diluted: informational, decides nothing)* | 25.32% | 20.28% | **1.25** | +1.03pp | -8.43pp | 9.58% | 561 | ₹0.35L | 102 | MAX_SECTOR 604, MIN_HOLDINGS 102 | yes |
| 2 | 50/50 blend of D13 and M10.7 (A6) | 25.78% | 22.08% | **1.17** | +1.50pp | -6.63pp | 10.04% | 2106 | ₹0.59L | 93 | MAX_SECTOR 483, MIN_HOLDINGS 93 | yes |
| 3 | Swing composite (M10.7) | 27.21% | 23.56% | **1.15** | +2.92pp | -5.15pp | 11.47% | 1505 | ₹0.78L | 2 | MAX_SECTOR 269, MIN_HOLDINGS 2 | yes |
| 4 | Residual momentum v2 (A3) | 19.87% | 20.83% | **0.95** | -4.42pp | -7.88pp | 4.13% | 677 | ₹0.41L | 90 | MAX_POSITION 1, MAX_SECTOR 92, MIN_HOLDINGS 90 | no |
| 5 | Momentum v2, all on (M9.5) | 21.41% | 24.04% | **0.89** | -2.87pp | -4.67pp | 5.68% | 1193 | ₹0.53L | 89 | MAX_SECTOR 159, MIN_HOLDINGS 89 | no |
| 6 | D13 + absolute momentum (A1) | 24.29% | 28.71% | **0.85** | +0.00pp | +0.00pp | 8.55% | 601 | ₹0.41L | 91 | MAX_SECTOR 214, MIN_HOLDINGS 91 | no |
| 7 | **Momentum v2, D13 paper config** | 24.29% | 28.71% | **0.85** | — | — | 8.55% | 601 | ₹0.41L | 91 | MAX_SECTOR 214, MIN_HOLDINGS 91 | no |
| 8 | Pure low volatility (A7) | 9.42% | 16.64% | **0.57** | -14.86pp | -12.07pp | -6.31% | 781 | ₹0.39L | 2 | MAX_SECTOR 446, MIN_HOLDINGS 2 | no |

**₹10 cr/day floor**

| # | Strategy | XIRR | Max DD | **XIRR/DD** | Δ XIRR vs D13 | Δ DD vs D13 | Excess vs NIFTY 50 TRI | Trades | Charges | A8 min-holdings refused sells | All rail blocks | >25%? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Swing composite (M10.7) | 24.55% | 22.62% | **1.09** | +5.02pp | -7.43pp | 8.82% | 978 | ₹0.52L | 0 | MAX_SECTOR 14 | no |
| 2 | 50/50 blend of D13 and M10.7 (A6) | 22.15% | 21.48% | **1.03** | +2.61pp | -8.57pp | 6.41% | 1611 | ₹0.43L | 85 | MAX_SECTOR 85, MIN_HOLDINGS 85 | no |
| 3 | Residual momentum v2 (A3) | 15.30% | 22.71% | **0.67** | -4.24pp | -7.34pp | -0.44% | 619 | ₹0.31L | 86 | MAX_POSITION 1, MAX_SECTOR 46, MIN_HOLDINGS 86 | no |
| 4 | D13 + absolute momentum (A1) | 19.54% | 30.05% | **0.65** | +0.00pp | +0.00pp | 3.80% | 633 | ₹0.34L | 85 | MAX_SECTOR 71, MIN_HOLDINGS 85 | no |
| 5 | **Momentum v2, D13 paper config** | 19.54% | 30.05% | **0.65** | — | — | 3.80% | 633 | ₹0.34L | 85 | MAX_SECTOR 71, MIN_HOLDINGS 85 | no |
| 6 | Pure low volatility (A7) | 12.78% | 22.26% | **0.57** | -6.76pp | -7.80pp | -2.96% | 891 | ₹0.42L | 0 | MAX_SECTOR 4 | no |
| 7 | Momentum v2, all on (M9.5) | 15.08% | 30.14% | **0.50** | -4.45pp | +0.09pp | -0.65% | 951 | ₹0.39L | 86 | MAX_SECTOR 47, MIN_HOLDINGS 86 | no |
| 8 | D13 + industry gate (A2) *(diluted: informational, decides nothing)* | 12.98% | 31.07% | **0.42** | -6.56pp | +1.02pp | -2.76% | 607 | ₹0.31L | 84 | MAX_POSITION 1, MAX_SECTOR 264, MIN_HOLDINGS 84 | no |

### wf-verification (2021-09-01 → 2026-08-31)

**₹1 cr/day floor**

| # | Strategy | XIRR | Max DD | **XIRR/DD** | Δ XIRR vs D13 | Δ DD vs D13 | Excess vs NIFTY 50 TRI | Trades | Charges | A8 min-holdings refused sells | All rail blocks | >25%? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 50/50 blend of D13 and M10.7 (A6) | 29.87% | 15.18% | **1.97** | -3.17pp | -1.91pp | 21.48% | 2497 | ₹0.86L | 161 | MAX_SECTOR 851, MIN_HOLDINGS 161 | yes |
| 2 | D13 + absolute momentum (A1) | 33.04% | 17.10% | **1.93** | +0.00pp | +0.00pp | 24.65% | 910 | ₹0.70L | 153 | MAX_SECTOR 255, MIN_HOLDINGS 153 | yes |
| 3 | **Momentum v2, D13 paper config** | 33.04% | 17.10% | **1.93** | — | — | 24.65% | 910 | ₹0.70L | 153 | MAX_SECTOR 255, MIN_HOLDINGS 153 | yes |
| 4 | Momentum v2, all on (M9.5) | 27.24% | 16.05% | **1.70** | -5.80pp | -1.05pp | 18.85% | 1302 | ₹0.81L | 151 | MAX_SECTOR 173, MIN_HOLDINGS 151 | yes |
| 5 | Swing composite (M10.7) | 26.36% | 15.84% | **1.66** | -6.68pp | -1.26pp | 17.96% | 1587 | ₹1.02L | 8 | MAX_SECTOR 596, MIN_HOLDINGS 8 | yes |
| 6 | Residual momentum v2 (A3) | 18.60% | 13.93% | **1.34** | -14.44pp | -3.17pp | 10.21% | 631 | ₹0.31L | 148 | MAX_POSITION 27, MAX_SECTOR 244, MIN_HOLDINGS 148 | no |
| 7 | M10.7 + earnings-surprise leg (A5) | 25.81% | 20.97% | **1.23** | -7.23pp | +3.87pp | 17.42% | 1735 | ₹1.13L | 0 | MAX_SECTOR 264 | yes |
| 8 | D13 + profitability filter (A4) | 24.94% | 20.83% | **1.20** | -8.10pp | +3.73pp | 16.55% | 957 | ₹0.72L | 156 | MAX_SECTOR 115, MIN_HOLDINGS 156 | no |
| 9 | D13 + industry gate (A2) *(diluted: informational, decides nothing)* | 17.32% | 17.34% | **1.00** | -15.72pp | +0.24pp | 8.92% | 698 | ₹0.44L | 166 | MAX_SECTOR 554, MIN_HOLDINGS 166 | no |
| 10 | Pure low volatility (A7) | 8.48% | 9.64% | **0.88** | -24.56pp | -7.46pp | 0.09% | 441 | ₹0.22L | 0 | MAX_SECTOR 330 | no |

**₹10 cr/day floor**

| # | Strategy | XIRR | Max DD | **XIRR/DD** | Δ XIRR vs D13 | Δ DD vs D13 | Excess vs NIFTY 50 TRI | Trades | Charges | A8 min-holdings refused sells | All rail blocks | >25%? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Momentum v2, all on (M9.5) | 31.29% | 18.64% | **1.68** | +3.12pp | -4.92pp | 22.90% | 1423 | ₹0.89L | 151 | MAX_SECTOR 15, MIN_HOLDINGS 151 | yes |
| 2 | D13 + profitability filter (A4) | 22.83% | 17.49% | **1.31** | -5.34pp | -6.07pp | 14.44% | 754 | ₹0.50L | 151 | MAX_SECTOR 20, MIN_HOLDINGS 151 | no |
| 3 | Residual momentum v2 (A3) | 20.83% | 17.00% | **1.23** | -7.34pp | -6.56pp | 12.43% | 665 | ₹0.43L | 165 | MAX_POSITION 4, MAX_SECTOR 104, MIN_HOLDINGS 165 | no |
| 4 | D13 + absolute momentum (A1) | 28.17% | 23.56% | **1.20** | +0.00pp | +0.00pp | 19.78% | 847 | ₹0.62L | 149 | MAX_SECTOR 26, MIN_HOLDINGS 149 | yes |
| 5 | **Momentum v2, D13 paper config** | 28.17% | 23.56% | **1.20** | — | — | 19.78% | 847 | ₹0.62L | 149 | MAX_SECTOR 26, MIN_HOLDINGS 149 | yes |
| 6 | 50/50 blend of D13 and M10.7 (A6) | 26.25% | 22.18% | **1.18** | -1.92pp | -1.38pp | 17.86% | 2336 | ₹0.79L | 149 | MAX_SECTOR 48, MIN_HOLDINGS 149 | yes |
| 7 | Swing composite (M10.7) | 24.21% | 22.45% | **1.08** | -3.96pp | -1.11pp | 15.82% | 1489 | ₹0.96L | 0 | MAX_SECTOR 22 | no |
| 8 | M10.7 + earnings-surprise leg (A5) | 24.18% | 23.54% | **1.03** | -3.99pp | -0.02pp | 15.78% | 1462 | ₹0.91L | 0 | MAX_SECTOR 2 | no |
| 9 | D13 + industry gate (A2) *(diluted: informational, decides nothing)* | 21.61% | 24.07% | **0.90** | -6.56pp | +0.51pp | 13.22% | 766 | ₹0.51L | 147 | MAX_SECTOR 210, MIN_HOLDINGS 147 | no |
| 10 | Pure low volatility (A7) | 8.15% | 18.19% | **0.45** | -20.03pp | -5.37pp | -0.25% | 712 | ₹0.33L | 0 | MAX_SECTOR 345 | no |

## Results — nifty500 universe

### decade (2016-09-01 → 2026-08-31)

**₹1 cr/day floor**

| # | Strategy | XIRR | Max DD | **XIRR/DD** | Δ XIRR vs D13 | Δ DD vs D13 | Excess vs NIFTY 50 TRI | Trades | Charges | A8 min-holdings refused sells | All rail blocks | >25%? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Residual momentum v2 (A3) | 19.42% | 16.60% | **1.17** | -2.60pp | -8.07pp | 7.45% | 1825 | ₹1.52L | 234 | MAX_SECTOR 78, MIN_HOLDINGS 234 | no |
| 2 | Momentum v2, all on (M9.5) | 22.79% | 20.73% | **1.10** | +0.77pp | -3.94pp | 10.82% | 2705 | ₹2.05L | 230 | MAX_SECTOR 40, MIN_HOLDINGS 230 | no |
| 3 | 50/50 blend of D13 and M10.7 (A6) | 22.34% | 20.77% | **1.08** | +0.33pp | -3.90pp | 10.38% | 5649 | ₹2.39L | 232 | MAX_SECTOR 126, MIN_HOLDINGS 232 | no |
| 4 | D13 + industry gate (A2) *(diluted: informational, decides nothing)* | 22.53% | 23.84% | **0.94** | +0.51pp | -0.82pp | 10.57% | 1979 | ₹1.72L | 237 | MAX_SECTOR 401, MIN_HOLDINGS 237 | no |
| 5 | Swing composite (M10.7) | 22.66% | 25.29% | **0.90** | +0.64pp | +0.62pp | 10.70% | 3694 | ₹3.08L | 0 | MAX_SECTOR 70 | no |
| 6 | D13 + absolute momentum (A1) | 22.02% | 24.67% | **0.89** | +0.00pp | +0.00pp | 10.05% | 1955 | ₹1.69L | 232 | MAX_SECTOR 56, MIN_HOLDINGS 232 | no |
| 7 | **Momentum v2, D13 paper config** | 22.02% | 24.67% | **0.89** | — | — | 10.05% | 1955 | ₹1.69L | 232 | MAX_SECTOR 56, MIN_HOLDINGS 232 | no |
| 8 | Pure low volatility (A7) | 12.86% | 21.00% | **0.61** | -9.16pp | -3.66pp | 0.89% | 2418 | ₹1.57L | 0 | MAX_ORDER_VALUE 17, MAX_SECTOR 54 | no |

**₹10 cr/day floor**

| # | Strategy | XIRR | Max DD | **XIRR/DD** | Δ XIRR vs D13 | Δ DD vs D13 | Excess vs NIFTY 50 TRI | Trades | Charges | A8 min-holdings refused sells | All rail blocks | >25%? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Residual momentum v2 (A3) | 19.26% | 19.62% | **0.98** | -1.58pp | -8.93pp | 7.30% | 1711 | ₹1.39L | 228 | MAX_SECTOR 62, MIN_HOLDINGS 228 | no |
| 2 | 50/50 blend of D13 and M10.7 (A6) | 20.55% | 23.31% | **0.88** | -0.29pp | -5.24pp | 8.58% | 4668 | ₹1.92L | 229 | MAX_SECTOR 65, MIN_HOLDINGS 229 | no |
| 3 | Swing composite (M10.7) | 20.25% | 24.13% | **0.84** | -0.60pp | -4.41pp | 8.28% | 2940 | ₹2.38L | 0 | MAX_SECTOR 17 | no |
| 4 | Momentum v2, all on (M9.5) | 19.56% | 24.79% | **0.79** | -1.28pp | -3.76pp | 7.60% | 2288 | ₹1.60L | 228 | MAX_SECTOR 43, MIN_HOLDINGS 228 | no |
| 5 | D13 + industry gate (A2) | 17.56% | 23.09% | **0.76** | -3.28pp | -5.46pp | 5.59% | 1641 | ₹1.21L | 238 | MAX_POSITION 1, MAX_SECTOR 291, MIN_HOLDINGS 238 | no |
| 6 | D13 + absolute momentum (A1) | 20.84% | 28.55% | **0.73** | +0.00pp | +0.00pp | 8.88% | 1728 | ₹1.45L | 229 | MAX_SECTOR 48, MIN_HOLDINGS 229 | no |
| 7 | **Momentum v2, D13 paper config** | 20.84% | 28.55% | **0.73** | — | — | 8.88% | 1728 | ₹1.45L | 229 | MAX_SECTOR 48, MIN_HOLDINGS 229 | no |
| 8 | Pure low volatility (A7) | 11.78% | 23.89% | **0.49** | -9.06pp | -4.66pp | -0.19% | 2179 | ₹1.34L | 0 | MAX_ORDER_VALUE 16 | no |

### six-year (2019-07-01 → 2026-08-31)

**₹1 cr/day floor**

| # | Strategy | XIRR | Max DD | **XIRR/DD** | Δ XIRR vs D13 | Δ DD vs D13 | Excess vs NIFTY 50 TRI | Trades | Charges | A8 min-holdings refused sells | All rail blocks | >25%? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Residual momentum v2 (A3) | 22.81% | 15.62% | **1.46** | -3.83pp | -5.95pp | 11.11% | 1253 | ₹1.02L | 213 | MAX_SECTOR 37, MIN_HOLDINGS 213 | no |
| 2 | Momentum v2, all on (M9.5) | 25.98% | 17.81% | **1.46** | -0.67pp | -3.76pp | 14.28% | 1895 | ₹1.33L | 209 | MAX_SECTOR 21, MIN_HOLDINGS 209 | yes |
| 3 | 50/50 blend of D13 and M10.7 (A6) | 26.52% | 20.47% | **1.30** | -0.13pp | -1.10pp | 14.82% | 3602 | ₹1.50L | 215 | MAX_SECTOR 58, MIN_HOLDINGS 215 | yes |
| 4 | D13 + absolute momentum (A1) | 26.65% | 21.57% | **1.24** | +0.00pp | +0.00pp | 14.95% | 1317 | ₹1.11L | 211 | MAX_SECTOR 41, MIN_HOLDINGS 211 | yes |
| 5 | **Momentum v2, D13 paper config** | 26.65% | 21.57% | **1.24** | — | — | 14.95% | 1317 | ₹1.11L | 211 | MAX_SECTOR 41, MIN_HOLDINGS 211 | yes |
| 6 | Swing composite (M10.7) | 26.39% | 25.56% | **1.03** | -0.25pp | +3.99pp | 14.69% | 2285 | ₹1.88L | 4 | MAX_SECTOR 17, MIN_HOLDINGS 4 | yes |
| 7 | D13 + industry gate (A2) | 25.43% | 24.73% | **1.03** | -1.22pp | +3.16pp | 13.73% | 1299 | ₹1.05L | 225 | MAX_SECTOR 264, MIN_HOLDINGS 225 | yes |
| 8 | M10.7 + earnings-surprise leg (A5) | 26.05% | 25.56% | **1.02** | -0.60pp | +3.99pp | 14.35% | 2320 | ₹1.88L | 4 | MAX_SECTOR 7, MIN_HOLDINGS 4 | yes |
| 9 | D13 + profitability filter (A4) | 19.28% | 19.04% | **1.01** | -7.36pp | -2.53pp | 7.58% | 1036 | ₹0.73L | 210 | MAX_SECTOR 36, MIN_HOLDINGS 210 | no |
| 10 | Pure low volatility (A7) | 14.68% | 22.23% | **0.66** | -11.97pp | +0.66pp | 2.97% | 1778 | ₹1.04L | 0 | MAX_ORDER_VALUE 17, MAX_SECTOR 10 | no |

**₹10 cr/day floor**

| # | Strategy | XIRR | Max DD | **XIRR/DD** | Δ XIRR vs D13 | Δ DD vs D13 | Excess vs NIFTY 50 TRI | Trades | Charges | A8 min-holdings refused sells | All rail blocks | >25%? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | D13 + profitability filter (A4) | 20.78% | 15.96% | **1.30** | -4.32pp | -4.45pp | 9.08% | 1043 | ₹0.75L | 214 | MAX_SECTOR 46, MIN_HOLDINGS 214 | no |
| 2 | D13 + absolute momentum (A1) | 25.10% | 20.41% | **1.23** | +0.00pp | +0.00pp | 13.40% | 1208 | ₹0.99L | 210 | MAX_SECTOR 32, MIN_HOLDINGS 210 | yes |
| 3 | **Momentum v2, D13 paper config** | 25.10% | 20.41% | **1.23** | — | — | 13.40% | 1208 | ₹0.99L | 210 | MAX_SECTOR 32, MIN_HOLDINGS 210 | yes |
| 4 | 50/50 blend of D13 and M10.7 (A6) | 24.91% | 21.83% | **1.14** | -0.19pp | +1.41pp | 13.21% | 3290 | ₹1.30L | 212 | MAX_SECTOR 49, MIN_HOLDINGS 212 | no |
| 5 | Residual momentum v2 (A3) | 24.37% | 22.35% | **1.09** | -0.73pp | +1.94pp | 12.67% | 1247 | ₹1.05L | 212 | MAX_SECTOR 29, MIN_HOLDINGS 212 | no |
| 6 | Swing composite (M10.7) | 24.72% | 23.65% | **1.05** | -0.38pp | +3.23pp | 13.02% | 2082 | ₹1.62L | 2 | MAX_SECTOR 17, MIN_HOLDINGS 2 | no |
| 7 | D13 + industry gate (A2) | 23.47% | 22.73% | **1.03** | -1.63pp | +2.32pp | 11.77% | 1197 | ₹0.93L | 218 | MAX_POSITION 1, MAX_SECTOR 182, MIN_HOLDINGS 218 | no |
| 8 | Momentum v2, all on (M9.5) | 23.44% | 23.05% | **1.02** | -1.67pp | +2.63pp | 11.73% | 1799 | ₹1.17L | 207 | MAX_SECTOR 28, MIN_HOLDINGS 207 | no |
| 9 | M10.7 + earnings-surprise leg (A5) | 23.95% | 23.98% | **1.00** | -1.15pp | +3.57pp | 12.25% | 2055 | ₹1.60L | 2 | MAX_SECTOR 9, MIN_HOLDINGS 2 | no |
| 10 | Pure low volatility (A7) | 13.01% | 21.46% | **0.61** | -12.10pp | +1.04pp | 1.31% | 1625 | ₹0.90L | 0 | MAX_ORDER_VALUE 16 | no |

### wf-selection (2016-09-01 → 2021-08-31)

**₹1 cr/day floor**

| # | Strategy | XIRR | Max DD | **XIRR/DD** | Δ XIRR vs D13 | Δ DD vs D13 | Excess vs NIFTY 50 TRI | Trades | Charges | A8 min-holdings refused sells | All rail blocks | >25%? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | D13 + industry gate (A2) *(diluted: informational, decides nothing)* | 23.38% | 18.40% | **1.27** | +1.66pp | -6.27pp | 7.65% | 680 | ₹0.40L | 85 | MAX_SECTOR 249, MIN_HOLDINGS 85 | no |
| 2 | 50/50 blend of D13 and M10.7 (A6) | 25.02% | 20.77% | **1.20** | +3.30pp | -3.90pp | 9.29% | 2053 | ₹0.57L | 87 | MAX_SECTOR 95, MIN_HOLDINGS 87 | yes |
| 3 | Momentum v2, all on (M9.5) | 23.57% | 20.73% | **1.14** | +1.84pp | -3.94pp | 7.83% | 1083 | ₹0.54L | 86 | MAX_SECTOR 21, MIN_HOLDINGS 86 | no |
| 4 | Swing composite (M10.7) | 28.01% | 25.29% | **1.11** | +6.29pp | +0.62pp | 12.27% | 1365 | ₹0.73L | 0 | MAX_SECTOR 61 | yes |
| 5 | Residual momentum v2 (A3) | 17.10% | 16.60% | **1.03** | -4.62pp | -8.07pp | 1.37% | 671 | ₹0.38L | 89 | MAX_SECTOR 54, MIN_HOLDINGS 89 | no |
| 6 | D13 + absolute momentum (A1) | 21.72% | 24.67% | **0.88** | +0.00pp | +0.00pp | 5.99% | 688 | ₹0.41L | 87 | MAX_SECTOR 34, MIN_HOLDINGS 87 | no |
| 7 | **Momentum v2, D13 paper config** | 21.72% | 24.67% | **0.88** | — | — | 5.99% | 688 | ₹0.41L | 87 | MAX_SECTOR 34, MIN_HOLDINGS 87 | no |
| 8 | Pure low volatility (A7) | 16.50% | 21.00% | **0.79** | -5.22pp | -3.66pp | 0.76% | 1090 | ₹0.55L | 0 | MAX_SECTOR 54 | no |

**₹10 cr/day floor**

| # | Strategy | XIRR | Max DD | **XIRR/DD** | Δ XIRR vs D13 | Δ DD vs D13 | Excess vs NIFTY 50 TRI | Trades | Charges | A8 min-holdings refused sells | All rail blocks | >25%? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Swing composite (M10.7) | 22.77% | 24.13% | **0.94** | +2.94pp | -4.41pp | 7.04% | 952 | ₹0.51L | 0 | MAX_SECTOR 7 | no |
| 2 | 50/50 blend of D13 and M10.7 (A6) | 21.34% | 23.31% | **0.92** | +1.51pp | -5.24pp | 5.60% | 1546 | ₹0.42L | 84 | MAX_SECTOR 33, MIN_HOLDINGS 84 | no |
| 3 | Residual momentum v2 (A3) | 17.21% | 19.62% | **0.88** | -2.62pp | -8.93pp | 1.47% | 593 | ₹0.31L | 84 | MAX_SECTOR 36, MIN_HOLDINGS 84 | no |
| 4 | Momentum v2, all on (M9.5) | 17.89% | 24.79% | **0.72** | -1.94pp | -3.76pp | 2.16% | 802 | ₹0.37L | 85 | MAX_SECTOR 21, MIN_HOLDINGS 85 | no |
| 5 | D13 + absolute momentum (A1) | 19.83% | 28.55% | **0.69** | +0.00pp | +0.00pp | 4.09% | 594 | ₹0.32L | 84 | MAX_SECTOR 26, MIN_HOLDINGS 84 | no |
| 6 | **Momentum v2, D13 paper config** | 19.83% | 28.55% | **0.69** | — | — | 4.09% | 594 | ₹0.32L | 84 | MAX_SECTOR 26, MIN_HOLDINGS 84 | no |
| 7 | D13 + industry gate (A2) | 14.55% | 23.09% | **0.63** | -5.28pp | -5.46pp | -1.19% | 625 | ₹0.31L | 85 | MAX_SECTOR 180, MIN_HOLDINGS 85 | no |
| 8 | Pure low volatility (A7) | 13.02% | 23.89% | **0.54** | -6.81pp | -4.66pp | -2.72% | 938 | ₹0.45L | 0 | — | no |

### wf-verification (2021-09-01 → 2026-08-31)

**₹1 cr/day floor**

| # | Strategy | XIRR | Max DD | **XIRR/DD** | Δ XIRR vs D13 | Δ DD vs D13 | Excess vs NIFTY 50 TRI | Trades | Charges | A8 min-holdings refused sells | All rail blocks | >25%? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Residual momentum v2 (A3) | 20.22% | 16.82% | **1.20** | -2.56pp | -5.21pp | 11.83% | 800 | ₹0.56L | 144 | MAX_SECTOR 18, MIN_HOLDINGS 144 | no |
| 2 | Momentum v2, all on (M9.5) | 21.32% | 17.94% | **1.19** | -1.46pp | -4.10pp | 12.93% | 1236 | ₹0.66L | 144 | MAX_SECTOR 15, MIN_HOLDINGS 144 | no |
| 3 | 50/50 blend of D13 and M10.7 (A6) | 21.03% | 19.49% | **1.08** | -1.76pp | -2.55pp | 12.64% | 2166 | ₹0.66L | 147 | MAX_SECTOR 21, MIN_HOLDINGS 147 | no |
| 4 | D13 + absolute momentum (A1) | 22.79% | 22.03% | **1.03** | +0.00pp | +0.00pp | 14.39% | 793 | ₹0.55L | 147 | MAX_SECTOR 18, MIN_HOLDINGS 147 | no |
| 5 | **Momentum v2, D13 paper config** | 22.79% | 22.03% | **1.03** | — | — | 14.39% | 793 | ₹0.55L | 147 | MAX_SECTOR 18, MIN_HOLDINGS 147 | no |
| 6 | D13 + industry gate (A2) | 22.02% | 22.24% | **0.99** | -0.77pp | +0.20pp | 13.62% | 775 | ₹0.51L | 155 | MAX_SECTOR 139, MIN_HOLDINGS 155 | no |
| 7 | D13 + profitability filter (A4) | 16.80% | 18.83% | **0.89** | -5.98pp | -3.21pp | 8.41% | 654 | ₹0.41L | 143 | MAX_SECTOR 24, MIN_HOLDINGS 143 | no |
| 8 | M10.7 + earnings-surprise leg (A5) | 20.00% | 22.90% | **0.87** | -2.79pp | +0.87pp | 11.60% | 1345 | ₹0.75L | 0 | MAX_SECTOR 1 | no |
| 9 | Swing composite (M10.7) | 19.17% | 22.92% | **0.84** | -3.62pp | +0.88pp | 10.77% | 1373 | ₹0.78L | 0 | MAX_SECTOR 3 | no |
| 10 | Pure low volatility (A7) | 9.87% | 16.88% | **0.58** | -12.92pp | -5.15pp | 1.47% | 1196 | ₹0.54L | 0 | — | no |

**₹10 cr/day floor**

| # | Strategy | XIRR | Max DD | **XIRR/DD** | Δ XIRR vs D13 | Δ DD vs D13 | Excess vs NIFTY 50 TRI | Trades | Charges | A8 min-holdings refused sells | All rail blocks | >25%? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Residual momentum v2 (A3) | 20.56% | 16.29% | **1.26** | +0.72pp | -5.16pp | 12.16% | 778 | ₹0.53L | 143 | MAX_SECTOR 17, MIN_HOLDINGS 143 | no |
| 2 | Momentum v2, all on (M9.5) | 19.98% | 19.89% | **1.00** | +0.14pp | -1.56pp | 11.59% | 1180 | ₹0.61L | 144 | MAX_SECTOR 16, MIN_HOLDINGS 144 | no |
| 3 | D13 + profitability filter (A4) | 17.23% | 17.33% | **0.99** | -2.61pp | -4.11pp | 8.83% | 625 | ₹0.39L | 146 | MAX_SECTOR 27, MIN_HOLDINGS 146 | no |
| 4 | D13 + industry gate (A2) | 21.33% | 22.00% | **0.97** | +1.49pp | +0.56pp | 12.93% | 783 | ₹0.51L | 153 | MAX_POSITION 2, MAX_SECTOR 84, MIN_HOLDINGS 153 | no |
| 5 | D13 + absolute momentum (A1) | 19.84% | 21.44% | **0.93** | +0.00pp | +0.00pp | 11.45% | 706 | ₹0.48L | 144 | MAX_SECTOR 20, MIN_HOLDINGS 144 | no |
| 6 | **Momentum v2, D13 paper config** | 19.84% | 21.44% | **0.93** | — | — | 11.45% | 706 | ₹0.48L | 144 | MAX_SECTOR 20, MIN_HOLDINGS 144 | no |
| 7 | 50/50 blend of D13 and M10.7 (A6) | 19.08% | 20.69% | **0.92** | -0.76pp | -0.76pp | 10.68% | 2023 | ₹0.62L | 144 | MAX_SECTOR 23, MIN_HOLDINGS 144 | no |
| 8 | M10.7 + earnings-surprise leg (A5) | 20.95% | 23.93% | **0.88** | +1.11pp | +2.49pp | 12.56% | 1317 | ₹0.77L | 0 | MAX_SECTOR 1 | no |
| 9 | Swing composite (M10.7) | 18.29% | 23.99% | **0.76** | -1.55pp | +2.55pp | 9.90% | 1317 | ₹0.75L | 0 | MAX_SECTOR 3 | no |
| 10 | Pure low volatility (A7) | 8.58% | 21.55% | **0.40** | -11.26pp | +0.10pp | 0.19% | 1104 | ₹0.48L | 0 | — | no |

## A2 — industry-classification coverage (Amendment 1)

Unclassified share of the floor universe (names absent from the 2026 classification, which pass the gate). Over 30.00% in a cell, A2 is **diluted** there: shown, but informational, and read by no step of the rule.

| Cell | Unclassified | A2 | By year |
| --- | --- | --- | --- |
| floor-only decade ₹1 cr | 69.51% | diluted | 2016 52.90%, 2017 56.09%, 2018 55.49%, 2019 47.34%, 2020 41.57%, 2021 53.60%, 2022 56.40%, 2023 55.59%, 2024 60.69%, 2025 59.59%, 2026 56.21% |
| floor-only decade ₹10 cr | 46.05% | diluted | 2016 31.82%, 2017 35.76%, 2018 33.72%, 2019 27.03%, 2020 24.36%, 2021 25.26%, 2022 25.04%, 2023 21.91%, 2024 30.64%, 2025 29.91%, 2026 23.88% |
| floor-only six-year ₹1 cr | 67.59% | diluted | 2019 41.94%, 2020 41.57%, 2021 53.60%, 2022 56.40%, 2023 55.59%, 2024 60.69%, 2025 59.59%, 2026 56.21% |
| floor-only six-year ₹10 cr | 43.44% | diluted | 2019 23.48%, 2020 24.36%, 2021 25.26%, 2022 25.04%, 2023 21.91%, 2024 30.64%, 2025 29.91%, 2026 23.88% |
| floor-only wf-selection ₹1 cr | 60.93% | diluted | 2016 52.90%, 2017 56.09%, 2018 55.49%, 2019 47.34%, 2020 41.57%, 2021 48.88% |
| floor-only wf-selection ₹10 cr | 36.35% | diluted | 2016 31.82%, 2017 35.76%, 2018 33.72%, 2019 27.03%, 2020 24.36%, 2021 23.10% |
| floor-only wf-verification ₹1 cr | 66.79% | diluted | 2021 52.73%, 2022 56.40%, 2023 55.59%, 2024 60.69%, 2025 59.59%, 2026 56.21% |
| floor-only wf-verification ₹10 cr | 42.59% | diluted | 2021 24.42%, 2022 25.04%, 2023 21.91%, 2024 30.64%, 2025 29.91%, 2026 23.88% |
| NIFTY500 decade ₹1 cr | 34.24% | diluted | 2016 40.62%, 2017 39.07%, 2018 36.93%, 2019 32.10%, 2020 27.27%, 2021 25.23%, 2022 22.30%, 2023 18.38%, 2024 13.44%, 2025 6.14%, 2026 2.06% |
| NIFTY500 decade ₹10 cr | 21.77% | decides | 2016 30.20%, 2017 29.46%, 2018 27.89%, 2019 23.99%, 2020 20.57%, 2021 17.71%, 2022 17.44%, 2023 12.53%, 2024 10.02%, 2025 5.21%, 2026 1.37% |
| NIFTY500 six-year ₹1 cr | 25.81% | decides | 2019 27.79%, 2020 27.27%, 2021 25.23%, 2022 22.30%, 2023 18.38%, 2024 13.44%, 2025 6.14%, 2026 2.06% |
| NIFTY500 six-year ₹10 cr | 17.15% | decides | 2019 22.00%, 2020 20.57%, 2021 17.71%, 2022 17.44%, 2023 12.53%, 2024 10.02%, 2025 5.21%, 2026 1.37% |
| NIFTY500 wf-selection ₹1 cr | 41.10% | diluted | 2016 40.62%, 2017 39.07%, 2018 36.93%, 2019 32.10%, 2020 27.27%, 2021 25.67% |
| NIFTY500 wf-selection ₹10 cr | 29.72% | decides | 2016 30.20%, 2017 29.46%, 2018 27.89%, 2019 23.99%, 2020 20.57%, 2021 18.13% |
| NIFTY500 wf-verification ₹1 cr | 20.73% | decides | 2021 23.72%, 2022 22.30%, 2023 18.38%, 2024 13.44%, 2025 6.14%, 2026 2.06% |
| NIFTY500 wf-verification ₹10 cr | 14.49% | decides | 2021 16.75%, 2022 17.44%, 2023 12.53%, 2024 10.02%, 2025 5.21%, 2026 1.37% |

| Sector index | First rankable date |
| --- | --- |
| NIFTY AUTO | 2012-03-31 |
| NIFTY CHEMICALS | 2026-05-09 |
| NIFTY CONSUMER DURABLES | 2022-02-05 |
| NIFTY ENERGY | 2011-07-30 |
| NIFTY FIN SERVICE | 2012-08-25 |
| NIFTY FMCG | 2011-07-30 |
| NIFTY INFRA | 2011-01-15 |
| NIFTY IT | 2010-07-03 |
| NIFTY MEDIA | 2012-03-31 |
| NIFTY METAL | 2012-03-31 |
| NIFTY PHARMA | 2011-07-30 |
| NIFTY REALTY | 2011-01-15 |

## Scorecard against D13 (generated)

Counted in code (`backtest.m16_report.scorecard`) over every cell (universe, window, floor) that has both the arm and D13. Comparisons are exact.

| Strategy | Cells | XIRR/DD better / tie / worse | Cells where XIRR/DD is worse | Max DD worse / tie / better | Worst DD increase | XIRR > 25% | A8 min-holdings refusals |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **Momentum v2, D13 paper config** | 16 | — | — | — | — | 7 / 16 | 2731 |
| 50/50 blend of D13 and M10.7 (A6) | 16 | 11 / 0 / 5 | floor-only six-year ₹1 cr; floor-only six-year ₹10 cr; floor-only wf-verification ₹10 cr; NIFTY500 wf-verification ₹10 cr; NIFTY500 six-year ₹10 cr | 3 / 0 / 13 | +3.19pp | 8 / 16 | 2765 |
| D13 + absolute momentum (A1) | 16 | 0 / 16 / 0 | — | 0 / 16 / 0 | — | 7 / 16 | 2731 |
| D13 + industry gate (A2) | 16 | 6 / 0 / 10 | floor-only wf-verification ₹1 cr; floor-only six-year ₹10 cr; floor-only wf-verification ₹10 cr; floor-only wf-selection ₹10 cr; floor-only six-year ₹1 cr; floor-only decade ₹10 cr; NIFTY500 six-year ₹10 cr; NIFTY500 six-year ₹1 cr; NIFTY500 wf-verification ₹1 cr; NIFTY500 wf-selection ₹10 cr | 9 / 0 / 7 | +3.16pp | 2 / 16 | 2837 |
| D13 + profitability filter (A4) | 8 | 3 / 0 / 5 | floor-only six-year ₹1 cr; floor-only wf-verification ₹1 cr; floor-only six-year ₹10 cr; NIFTY500 wf-verification ₹1 cr; NIFTY500 six-year ₹1 cr | 2 / 0 / 6 | +3.73pp | 0 / 8 | 1462 |
| M10.7 + earnings-surprise leg (A5) | 8 | 0 / 0 / 8 | all 8 | 7 / 0 / 1 | +6.78pp | 4 / 8 | 13 |
| Momentum v2, all on (M9.5) | 16 | 11 / 0 / 5 | floor-only wf-selection ₹10 cr; floor-only wf-verification ₹1 cr; floor-only decade ₹10 cr; floor-only six-year ₹10 cr; NIFTY500 six-year ₹10 cr | 4 / 0 / 12 | +2.63pp | 5 / 16 | 2713 |
| Pure low volatility (A7) | 16 | 0 / 0 / 16 | all 16 | 3 / 0 / 13 | +1.04pp | 0 / 16 | 4 |
| Residual momentum v2 (A3) | 16 | 11 / 0 / 5 | floor-only six-year ₹1 cr; floor-only wf-verification ₹1 cr; floor-only six-year ₹10 cr; floor-only decade ₹1 cr; NIFTY500 six-year ₹10 cr | 1 / 0 / 15 | +1.94pp | 1 / 16 | 2789 |
| Swing composite (M10.7) | 16 | 8 / 0 / 8 | floor-only six-year ₹1 cr; floor-only six-year ₹10 cr; floor-only wf-verification ₹10 cr; floor-only wf-verification ₹1 cr; NIFTY500 wf-verification ₹10 cr; NIFTY500 wf-verification ₹1 cr; NIFTY500 six-year ₹1 cr; NIFTY500 six-year ₹10 cr | 8 / 0 / 8 | +6.78pp | 7 / 16 | 34 |

## Decision (pre-registered rule)

Rule: `ops/studies/preregistration-m16-2026-10-08.md` §4. Trial count **N = 75** (68 configurations on record before M16 + 7 M16 arms; Appendix A lists every one).

**Step 1 — walk-forward choice** on the selection window, floor-only, at the **₹10 cr/day floor** (primary; pending owner confirmation before the campaign launched), by XIRR/DD:

1. Swing composite (M10.7) — 1.09
2. 50/50 blend of D13 and M10.7 (A6) — 1.03
3. Residual momentum v2 (A3) — 0.67
4. D13 + absolute momentum (A1) — 0.65
5. Momentum v2, D13 paper config — 0.65
6. Pure low volatility (A7) — 0.57
7. Momentum v2, all on (M9.5) — 0.50

Excluded as diluted (Amendment 1): D13 + industry gate (A2).

Choice: **Swing composite (M10.7)**.

Secondary, *informational only* — the same ranking at the ₹1 cr/day floor: 50/50 blend of D13 and M10.7 (A6) (1.17), Swing composite (M10.7) (1.15), Residual momentum v2 (A3) (0.95), Momentum v2, all on (M9.5) (0.89), D13 + absolute momentum (A1) (0.85), Momentum v2, D13 paper config (0.85), Pure low volatility (A7) (0.57). It decides nothing.

**Step 2 — replacement test** for the choice (all five must pass):

**Swing composite (M10.7)**

| Criterion | Result | Figures |
| --- | --- | --- |
| 1a. verification XIRR/DD ≥ D13, floor-only ₹1 cr | FAIL | 1.66 vs D13 1.93 |
| 1b. verification XIRR/DD ≥ D13, floor-only ₹10 cr | FAIL | 1.08 vs D13 1.20 |
| 2. verification XIRR/DD ≥ D13, NIFTY 500 ₹10 cr | FAIL | 0.76 vs D13 0.93 |
| 3. verification XIRR > 25%, floor-only ₹10 cr | FAIL | 24.21% |
| 4. max DD ≤ D13 + 3.0pp in every covered cell | FAIL | worst +6.78pp over 16 cells |
| 5. deflated Sharpe ≥ 0.95 vs D13 | FAIL | p = 0.2162; SR 0.10738 vs D13 0.08520 per session; N = 75; V = 3.610e-04 over n = 46 |

→ does not pass; nothing changes.

**Step 3 — A4 and A5** (verification-only evidence; at most shadow in paper):

**D13 + profitability filter (A4)**

| Criterion | Result | Figures |
| --- | --- | --- |
| 1a. verification XIRR/DD ≥ D13, floor-only ₹1 cr | FAIL | 1.20 vs D13 1.93 |
| 1b. verification XIRR/DD ≥ D13, floor-only ₹10 cr | PASS | 1.31 vs D13 1.20 |
| 2. verification XIRR/DD ≥ D13, NIFTY 500 ₹10 cr | PASS | 0.99 vs D13 0.93 |
| 3. verification XIRR > 25%, floor-only ₹10 cr | FAIL | 22.83% |
| 4. max DD ≤ D13 + 3.0pp in every covered cell | FAIL | worst +3.73pp over 8 cells |
| 5. deflated Sharpe ≥ 0.95 vs D13 | FAIL | p = 0.0321; SR 0.07608 vs D13 0.08520 per session; N = 75; V = 3.610e-04 over n = 46 |

→ does not pass; nothing changes.

**M10.7 + earnings-surprise leg (A5)**

| Criterion | Result | Figures |
| --- | --- | --- |
| 1a. verification XIRR/DD ≥ D13, floor-only ₹1 cr | FAIL | 1.23 vs D13 1.93 |
| 1b. verification XIRR/DD ≥ D13, floor-only ₹10 cr | FAIL | 1.03 vs D13 1.20 |
| 2. verification XIRR/DD ≥ D13, NIFTY 500 ₹10 cr | FAIL | 0.88 vs D13 0.93 |
| 3. verification XIRR > 25%, floor-only ₹10 cr | FAIL | 24.18% |
| 4. max DD ≤ D13 + 3.0pp in every covered cell | FAIL | worst +6.78pp over 8 cells |
| 5. deflated Sharpe ≥ 0.95 vs D13 | FAIL | p = 0.1928; SR 0.10522 vs D13 0.08520 per session; N = 75; V = 3.610e-04 over n = 46 |

→ does not pass; nothing changes.

**V** (criterion 5) is the sample variance of the per-session Sharpe on the verification window, floor-only, ₹10 cr/day floor, over the n = 46 trials with a saved run there: 50/50 blend of D13 and M10.7 (A6), D13 + absolute momentum (A1), D13 + industry gate (A2), D13 + profitability filter (A4), D13 + daily regime re-entry, 2% band, D13 + daily regime re-entry and exit, D13 + daily regime re-entry, Residual momentum v2 (A3), Momentum v2, D13 paper config, Momentum v2, all on (M9.5), Naive momentum (M4.10), Swing composite + band-hit avoidance (H2), Swing composite (M10.7), M10.7 @ weekly / 10-session hold, 2-session floor, Short composite, 2-week holds, Reversal: 5-day losers, M10.7 @ weekly / 10-session hold, M10.7 @ monthly / 126-session hold, M10.7 @ quarterly / 126-session hold, M10.7 @ fortnightly / 21-session hold, M10.7 @ weekly / 21-session hold, Short composite, 1-month holds, Trend: 1-month, M10.7 @ fortnightly / 42-session hold, M10.7 @ monthly / 63-session hold, Short composite, 3-month holds, M10.7 + regime gate, Short composite, top-10 + regime, Short composite + regime gate, Short composite + regime + low-vol, M10.7, band 5x top_n, M10.7, band 1.5x top_n, Short composite, top-10, Delivery acceleration, Short composite, Short composite + reversal, Short composite + low-vol leg, Mean proximity (50d), Turnover expansion, Pure low volatility (A7), Breakout, M10.7 + delivery acceleration, M10.7 + earnings-surprise leg (A5), Swing composite + residual momentum + band-hit avoidance (H3), Swing composite + residual momentum (H1), M10.7 + 1-month trend.

Counted toward N = 75 only, with no saved run on that cell: Naive momentum (M4.10) + redeploy, Short composite, top-5, Multi cap: 8 large + 8 mid + 8 small (liquidity tiers), Focused midcap: top 20 mid (liquidity tier), Focused smallcap: top 20 small (liquidity tier), Swing composite (M10.7) + redeploy, M10.7 + regime gate + redeploy, Multi cap + redeploy, v2: + 12-1 momentum (alone), v2: + turnover banding (alone), v2: + regime filter (alone), v2: + vol-scaled weights (alone), v2: + redeploy proceeds next session (alone), v2: all four M9.5 toggles, no redeploy, v2: + vol target 15% (alone), M10.7, cadence weekly (63-session re-underwrite), M10.7, no trailing stop, M10.7, 12% trailing stop, M10.7, delivery leg only, M10.7, no delivery leg, M10.7, 12-1 momentum leg only, M10.7, no volatility screen, Sector rotation, Plain momentum on the sector-rotation universe, Fundamentals: VALUE, Fundamentals: GROWTH, Fundamentals: QUALITY_VALUE, Fundamentals: MOMENTUM_VALUE, Forecast, daily.

<!-- hand-written analysis below: kept verbatim on re-render -->
## Gate note — M16.4 campaign (hand-written, 2026-10-12)

*Hand-written.* Everything above the marker is generated by `backtest.m16_report` from the saved
runs, and nothing there is typed by hand. This section applies the pre-registered rule to those
figures, records how the runs were made, and notes what did not fit. It changes no rule and no
figure. Every number below is copied from the generated tables above, or computed by
`backtest.m16_report.criteria` from the same runs (the per-arm table under **Every arm against
Step 2**).

### Verdict

**No improvement found. D13 stays the paper book** (§4 Step 4). No arm passes every clause of the
pre-registered rule: §4, as amended by Amendments 1–3, with D25 fixing the selection floor at
₹10 cr.

- **Step 1.** On the selection window, floor-only universe, ₹10 cr floor, ranked by XIRR/DD, the
  choice is **Swing composite (M10.7)** at 1.09. A2 is excluded because it is diluted there
  (36.35% unclassified).
- **Step 2.** M10.7 fails all five criteria:
  - 1a: 1.66 vs D13's 1.93.
  - 1b: 1.08 vs 1.20.
  - 2: 0.76 vs 0.93.
  - 3: 24.21% XIRR, not above 25%.
  - 4: its drawdown is up to +6.78 pp worse than D13's.
  - 5: p = 0.2162.
- **Step 3.** A4 and A5 each fail; the most either could have become is "shadow in paper".
  - A4 fails 1a, 3, 4 and 5.
  - A5 fails all five.
- **The ₹1 cr floor does not change the outcome.** At ₹1 cr, Step 1 would choose the A6 blend
  (1.17). A6 fails Step 2 too: 1b (1.18 vs 1.20), 2 (0.92 vs 0.93), 4 (+3.19 pp) and 5
  (p = 0.1545). So the verdict does not depend on D25, which is still pending the owner's
  ratification.

### Every arm against Step 2

Step 2 is applied to the Step-1 choice only (§4). The table below runs all five criteria over
every arm, for completeness. §4 says an arm that passes without being the choice "is reported as
passing and changes nothing". None passes.

All figures are out-of-sample: the verification window, 2021-09-01 → 2026-08-31. Cells read
XIRR / max DD (XIRR÷DD). "✗ diluted" means a required cell where A2 is diluted, which fails the
criterion under Amendment 2 (i).

| Arm | Floor-only ₹1 cr | Floor-only ₹10 cr | NIFTY 500 ₹10 cr (ratio) | XIRR at ₹10 cr | Worst DD vs D13 (all cells) | DSR p (N = 75) | 1a | 1b | 2 | 3 | 4 | 5 | Result |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **D13 (reference)** | 33.04% / 17.10% (1.93) | 28.17% / 23.56% (1.20) | 0.93 | 28.17% | — | — | — | — | — | — | — | — | stays |
| Swing composite (M10.7), **Step-1 choice** | 26.36% / 15.84% (1.66) | 24.21% / 22.45% (1.08) | 0.76 | 24.21% | +6.78 pp | 0.2162 | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | fail |
| D13 + absolute momentum (A1) | 33.04% / 17.10% (1.93) | 28.17% / 23.56% (1.20) | 0.93 | 28.17% | +0.00 pp | 0.0602 | ✓ (tie) | ✓ (tie) | ✓ (tie) | ✓ | ✓ | ✗ | fail |
| D13 + industry gate (A2) | 17.32% / 17.34% (1.00) | 21.61% / 24.07% (0.90) | 0.97 | 21.61% | diluted in 10 of 16 cells | — | ✗ diluted | ✗ diluted | ✓ | ✗ diluted | ✗ diluted | ✗ diluted | fail |
| Residual momentum v2 (A3) | 18.60% / 13.93% (1.34) | 20.83% / 17.00% (1.23) | 1.26 | 20.83% | +1.94 pp | 0.0911 | ✗ | ✓ | ✓ | ✗ | ✓ | ✗ | fail |
| D13 + profitability filter (A4) | 24.94% / 20.83% (1.20) | 22.83% / 17.49% (1.31) | 0.99 | 22.83% | +3.73 pp (8 cells) | 0.0321 | ✗ | ✓ | ✓ | ✗ | ✗ | ✗ | fail |
| M10.7 + earnings-surprise leg (A5) | 25.81% / 20.97% (1.23) | 24.18% / 23.54% (1.03) | 0.88 | 24.18% | +6.78 pp (8 cells) | 0.1928 | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | fail |
| 50/50 blend of D13 and M10.7 (A6) | 29.87% / 15.18% (1.97) | 26.25% / 22.18% (1.18) | 0.92 | 26.25% | +3.19 pp | 0.1545 | ✓ | ✗ | ✗ | ✓ | ✗ | ✗ | fail |
| Pure low volatility (A7) | 8.48% / 9.64% (0.88) | 8.15% / 18.19% (0.45) | 0.40 | 8.15% | +1.04 pp | 0.0106 | ✗ | ✗ | ✗ | ✗ | ✓ | ✗ | fail |
| Momentum v2, all on (M9.5), baseline | 27.24% / 16.05% (1.70) | 31.29% / 18.64% (1.68) | 1.00 | 31.29% | +2.63 pp | 0.1242 | ✗ | ✓ | ✓ | ✓ | ✓ | ✗ | fail |

The clauses:
- **1a / 1b**: verification XIRR÷DD ≥ D13's, floor-only, at ₹1 cr and at ₹10 cr.
- **2**: the same on NIFTY 500 at ₹10 cr.
- **3**: verification XIRR > 25%, floor-only, ₹10 cr.
- **4**: max DD no more than 3.0 pp worse than D13's in any covered cell. A4 and A5 are covered
  on six-year and verification only.
- **5**: the deflated Sharpe probability is ≥ 0.95 that the arm's Sharpe exceeds D13's. N = 75;
  V = 3.610e-04 over the n = 46 trials with a saved run on the cell.

**No arm comes near clause 5.** The highest probability is M10.7's 0.2162. The return gaps behind
these comparisons are within what 75 trials on one lake produce by luck.

### The ₹1 cr ranking (informational only, decides nothing)

These are the same rankings at the ₹1 cr floor, reported beside the deciding ₹10 cr figures as
§4 requires:

- **Selection window, floor-only** (Step 1's secondary ranking):
  1. A6 — 1.17
  2. M10.7 — 1.15
  3. A3 — 0.95
  4. M9.5 — 0.89
  5. A1 — 0.85
  6. D13 — 0.85
  7. A7 — 0.57

  A2 has the top ratio, 1.25, but is diluted there (60.93% unclassified), so it is not ranked.
- **Verification window, floor-only, ₹1 cr:**
  1. A6 — 1.97
  2. A1 — 1.93
  3. D13 — 1.93
  4. M9.5 — 1.70
  5. M10.7 — 1.66
  6. A3 — 1.34
  7. A5 — 1.23
  8. A4 — 1.20
  9. A7 — 0.88

  A2 is diluted there and not ranked (its ratio is 1.00).

### A1 and the 8-holding minimum (Amendment 3 (b))

A8's minimum-holdings rail refused sells as follows. The figures are the "A8 min-holdings refused
sells" column above; the total is the scorecard's.

| Universe | Window | A1 ₹1 cr | A1 ₹10 cr | D13 ₹1 cr | D13 ₹10 cr |
| --- | --- | --- | --- | --- | --- |
| floor-only | decade | 241 | 234 | 241 | 234 |
| floor-only | six-year | 220 | 214 | 220 | 214 |
| floor-only | wf-selection | 91 | 85 | 91 | 85 |
| floor-only | wf-verification | 153 | 149 | 153 | 149 |
| NIFTY 500 | decade | 232 | 229 | 232 | 229 |
| NIFTY 500 | six-year | 211 | 210 | 211 | 210 |
| NIFTY 500 | wf-selection | 87 | 84 | 87 | 84 |
| NIFTY 500 | wf-verification | 147 | 144 | 147 | 144 |
| **all 16 cells** | | **2,731** | | **2,731** | |

**A1 ran identically to D13 in all 16 cells.** It matches D13 on:
- XIRR, max DD, trades, charges and every rail-block count;
- the same scorecard tie, 0 / 16 / 0.

**This was checked against the saved runs, not inferred from the tables.**
- **A1 really was on.** Each A1 run's spec carries `absolute_momentum=True`, and its run digest
  and replay digest differ from D13's.
- **The books are identical.** In every cell the A1 and D13 runs end at the same terminal NAV,
  with the same round trips and refusal counts.
- **The trades are identical.** For floor-only decade ₹10 cr, the full ledgers were compared: all
  2,015 trades and every dividend, interest and corporate-event line match exactly.

So A1's refusals are not A1's doing. Every one is a sell that D13 itself made and the rail
refused, so the interaction Amendment 1 (d′) anticipated never arose. The likely reading is that
the top-20 12-1 basket never contained a name whose 12-1 return fell below the repo-based cash
hurdle. That reading is not proven here: the saved runs keep trades, not the policy's
`absolute_momentum_hurdle` evidence, so no rebalance's hurdle can be read back from them. Under the
rule this changes nothing. A1 ties D13 on criteria 1–4 and fails 5, since an identical Sharpe
gives p = 0.0602. Even if A1 had passed, it would not be the Step-1 choice.

### Run inputs and provenance

- **Code.** Every reported run was made at commit `3aed926` (`3aed926412f0…`).
  - That is `main` @ `97b8f95` (PR #100) plus one documentation commit: pre-registration
    Amendment 3 and D25.
  - That commit is dated 2026-10-09 14:02:00 IST (08:32:00Z). The first M16 campaign run of any
    kind started 56 s later, at 14:02:56 IST.
  - The reported runs started 2026-10-10 12:17:29 IST and finished 2026-10-11 06:33 IST.
  - All four run directories' manifests pin commit `3aed926412`.
- **Driver.** `backtest.m12_rerun`, with two workers, ran from the worktree at `3aed926`.
  - A supervisor stopped heavy work from 18:25 to 22:15 IST each day.
  - Resume is run by run, so no completed run was replayed after a pause.
- **Frozen inputs.** The live lake and database change under scheduled jobs, and the
  corporate-action loader has no as-of cutoff. The campaign therefore read frozen copies.
  - **Lake.** A hard-link copy of `data/` taken 2026-10-10 12:12:25 IST
    (`~/campaign/m16-frozen-2026-10-10/data`; `frozen_at.txt` reads 06:42:25Z).
    - **L1's last session is 2026-10-09**, which every manifest's `lake_last_session` records.
    - The copy's 65,072 input files match an inode/size/mtime fingerprint recorded at 11:58 IST
      (`m16-inputs-fingerprint-l1010-start.txt`, sha256 `9712f4cb…2bf45a`).
    - The fingerprint taken after the campaign (`m16-inputs-fingerprint-frozen-end.txt`) is
      identical to the one taken at the freeze.
    - Every run directory's manifest pins `data_root` to the frozen copy.
  - **Database.** A `pg_dump` of the live `trading` database was taken at 12:12:36 IST and
    restored into a throwaway container.
    - Its `corporate_actions` table has 49,671 rows, max `recorded_at` 2026-10-10 10:00:04 IST,
      and max id 158,255.
    - Its row-md5 is `6944a4fa26be93d7ba5fbaebf24ab520`, identical to the live table at the
      freeze. The supervisor re-checked this fingerprint before every launch.
- **Run directories** (under `~/campaign/`, 144 rendered rows):
  - `m16-floor-3aed926-frz1010`: 56 runs.
  - `m16-fund-floor-3aed926-frz1010`: 16 runs.
  - `m16-nifty500-3aed926-frz1010`: 56 runs.
  - `m16-fund-nifty500-3aed926-frz1010`: 16 runs.
- **Trial directories for V.** Exactly the two that Amendment 2 (iii) pins: `m12-rerun-2bb2b08`
  and `m14-5-regime-floor-cb1fcf9`.
- **A2 coverage.** Computed by Amendment 3 (c)'s method on the frozen lake:
  `m16-a2-coverage-3aed926-frz1010.json`, sha256 `f390a198…09593de7`.
- **Render.** The command below, run against those directories, reproduces everything above the
  marker byte-for-byte. It was re-run on 2026-10-12 and gave the same sha256 as the first render.

  ```
  uv run python -m backtest.m16_report \
    ~/campaign/m16-floor-3aed926-frz1010 ~/campaign/m16-fund-floor-3aed926-frz1010 \
    ~/campaign/m16-nifty500-3aed926-frz1010 ~/campaign/m16-fund-nifty500-3aed926-frz1010 \
    --trial-dirs ~/campaign/m12-rerun-2bb2b08 ~/campaign/m14-5-regime-floor-cb1fcf9 \
    --a2-coverage ~/campaign/m16-a2-coverage-3aed926-frz1010.json
  ```

### Earlier attempts, discarded unread

No figure from any of these was read, rendered or compared.

1. **Run 1** started 2026-10-09 14:02 IST, on a lake whose last session was 2026-10-08. It saved
   31 runs.
   - That evening's EOD added a session, and `check_manifest` refuses to resume a directory on a
     different lake.
   - It is kept as `m16-floor-3aed926-aborted-lake1008`.
2. **Run 2** started 22:16 IST, on the lake as of 2026-10-09. It saved 84 of 116 runs, then was
   held before Saturday's refreshes.
   - Saturday's `ca_refresh` rewrote L2 adjusted history inside the windows for INE793A01012,
     INE835B01035 and INE893J01029. That was back-adjustment for actions with ex-dates of
     2026-10-09.
   - It also added 8 corporate actions with ex-dates 2026-10-07..09.
   - The manifest cannot tell the two states apart, because both have last session 2026-10-09.
     So run 2 was set aside as `*-l1009-superseded-preCArefresh` rather than finished on mixed
     inputs.
3. **A start on the live, unfrozen inputs** ran for 12 minutes and saved 0 runs. It was stopped
   to move onto the frozen copies, and is kept as `…-l1010-aborted-unfrozen`.

### Grid as run

**NIFTY 500 runs on all four windows.** §3's NIFTY 500 rule is mechanical: a window is run on
NIFTY 500 iff the published PIT membership history covers its first session when the campaign
starts.
- Saturday's `index_press_refresh` (2026-10-10 09:49 IST) published a build covering from
  2015-03-28.
- On the frozen lake the engine answers on all 120 rebalance dates from 2016-09-01 to 2026-08-03.
- This is the rule's own contingency, applied at launch and before any figure was read. It is
  not an amendment.
- It can only make Step 2 harder: criterion 4 must hold in more cells.
- Had the rule been read at run 1's launch, the engine would have refused 2016-09-01, as
  `m16-nifty500-refusal-3aed926.txt` records.

**A4 and A5 run on six-year and verification only**, in both universes and at both floors (§2,
§3).

### Did not fit

- **The primary-floor confirmation is in §8, not §7.** The task graph's acceptance asks for it in
  pre-registration **§7**. It is recorded as **§8 Amendment 3** instead, because that run was told
  to append only. Amendment 3 says it stands in for the §7 entry.
- **D25 is still pending the owner's ratification.** As shown above, choosing at ₹1 cr instead
  would not change the verdict.
- **Why A1 never bound is not proven.** A1's equality with D13 is explained above only as far as
  the saved runs allow. Confirming that the hurdle never bound would take a replay that records
  the policy evidence. That replay is outside this task, and no new backtest was launched.
