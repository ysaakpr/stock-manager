# Holding-period study tables — Momentum v2, D13 paper config

## Arm

Momentum v2, D13 paper config — `MomentumV2Parameters(top_n=20, use_12_1=True, sell_band=30, regime_filter=True, vol_scaled=True, redeploy_next_session=True, vol_target_annual=None, assumed_correlation=Decimal('0.3'), regime_ma_days=200, buy_budget_fraction=Decimal('0.98'), sleeve=<Sleeve.TACTICAL: 'TACTICAL'>, parking_sleeve=<Sleeve.CASH: 'CASH'>)`

## Frozen D13 check

F1-test @ ₹10 cr: first moved credit 2018-10-23; NAV first differs 2018-10-23; first fill difference none (401 fills now); terminal ₹1,144,185.14 then, ₹1,144,089.02 now; pre-tax XIRR 4.6007% then, 4.5977% now; credits moved: dividends: 1 only earlier, 0 only now; e.g. dropped DividendCredit(isin='INE467B01029', received=datetime.date(2018, 10, 23), amount=Decimal('92')); digest differs (spec fields moved: book_actions) — store drift, re-frozen on the current store
F1-test @ ₹1 cr: economics identical (NAV path, fills, figures); digest differs (spec fields moved: book_actions)
F2-test @ ₹10 cr: economics identical (NAV path, fills, figures); digest differs (spec fields moved: book_actions)
F2-test @ ₹1 cr: economics identical (NAV path, fills, figures); digest differs (spec fields moved: book_actions)
F3-test @ ₹10 cr: economics identical (NAV path, fills, figures); digest differs (spec fields moved: book_actions)
F3-test @ ₹1 cr: economics identical (NAV path, fills, figures); digest differs (spec fields moved: book_actions)
F1-F3-continuous @ ₹10 cr: first moved credit 2018-10-23; NAV first differs 2018-10-23; first fill difference 2019-11-04 (2010 fills now); terminal ₹8,696,876.47 then, ₹8,724,093.91 now; pre-tax XIRR 24.1397% then, 24.1785% now; credits moved: dividends: 41 only earlier, 39 only now; e.g. dropped DividendCredit(isin='INE003A01024', received=datetime.date(2020, 2, 3), amount=Decimal('217')); e.g. added DividendCredit(isin='INE003A01024', received=datetime.date(2020, 2, 3), amount=Decimal('224')); corporate_events: 4 only earlier, 4 only now; e.g. dropped BonusEvent(isin='INE066P01011', ex_date=datetime.date(2024, 5, 24), new_shares=3, held_shares=1, resulting_quantity=1352); e.g. added BonusEvent(isin='INE066P01011', ex_date=datetime.date(2024, 5, 24), new_shares=3, held_shares=1, resulting_quantity=1372); digest differs (spec fields moved: book_actions) — store drift, re-frozen on the current store
F1-F3-continuous @ ₹1 cr: economics identical (NAV path, fills, figures); digest differs (spec fields moved: book_actions)

## Data span

- L1 calendar on disk: 2006-01-02 → 2026-10-19; joinable (ISIN-bearing) history from 2011-06-22.
- Study span: **2012-07-04 → 2026-10-15** (3537 sessions); month-start grid 2012-08 onwards, 170 months.
- NIFTY 500 point-in-time membership opens 2015-03-28: the `nifty500` universe the paper book screens cannot run before it, so the study runs the `turnover_floor` universe (liquidity floor only) — close to, not identical with, the paper book.
- NIFTY 500 P/E (`IN.NSE.NIFTY_500.PE`) opens 2012-10-01.

## Run inventory

- Book-action states across the runs: 1 (one — every run on the same store)
  - `calendar[35206]:BONUS=1372,BONUS:curated=6,DIVIDEND=30643,MERGER:cash_exit=4,MERGER:share_…`: 592 runs
- Named-interval runs missing: none
- Month-start grid starts missing: none
- Mid-month grid starts missing: none
- Cells scored: month-start 2878, mid-month 1750; cells whose walk-away figure the tax layer withheld: 7

## Expected return (E)

- Full span 2012-07-04 → 2026-10-15 at ₹10 crore/day: walk-away after-tax XIRR **18.04%**, realised after-tax 18.1%, pre-tax 20.77%; NIFTY 500 TRI walk-away 12.9%; cash after tax 3.9%.

## Minimum holding period (month-start grid, walk-away after-tax)

| Hurdle | 75 % | 90 % | 100 % (worst case) | 90 %, dense horizons | 90 %, mid-month grid | 90 %, all grids pooled | 90 % pre-tax, exact | 90 % pre-tax, continuous-run slice | Stable? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D13's own full-span return (E) | not within 84 m | not within 84 m | not within 84 m | not within 84 m | not within 84 m | not within 84 m | not within 84 m | not within 84 m | yes |
| Cash (repo - 50 bp, after slab tax) | 18 m | 48 m | 84 m | 42 m | 36 m | 36 m | 48 m | 48 m | NO (S1 grids) |
| NIFTY 500 TRI, same window | 84 m | not within 84 m | not within 84 m | not within 84 m | not within 84 m | not within 84 m | 60 m | 84 m | NO (S3 approximation) |
| No loss (≥ 0 %) | 9 m | 24 m | 48 m | 21 m | 24 m | 24 m | 18 m | 18 m | yes |
| 12 % a year | 60 m | not within 84 m | not within 84 m | not within 84 m | not within 84 m | not within 84 m | 84 m | not within 84 m | NO (S3 approximation) |
| 15 % a year | not within 84 m | not within 84 m | not within 84 m | not within 84 m | not within 84 m | not within 84 m | not within 84 m | not within 84 m | yes |

## Probability by horizon (month-start grid, dense horizons)

**D13's own full-span return (E)** — E = 18.04%

| Horizon | Windows scored | Met | P(meet) | Non-overlapping windows | 90 % interval (on non-overlapping) |
| --- | --- | --- | --- | --- | --- |
| 1 m | 170 | 61 | 36% | 170 | 30% to 42% |
| 2 m | 169 | 73 | 43% | 85 | 35% to 52% |
| 3 m | 168 | 76 | 45% | 56 | 35% to 56% |
| 4 m | 167 | 68 | 41% | 42 | 29% to 53% |
| 6 m | 165 | 66 | 40% | 28 | 26% to 55% |
| 9 m | 162 | 63 | 39% | 18 | 23% to 58% |
| 12 m | 159 | 63 | 40% | 14 | 22% to 61% |
| 15 m | 156 | 63 | 40% | 11 | 20% to 64% |
| 18 m | 153 | 63 | 41% | 9 | 19% to 67% |
| 21 m | 150 | 70 | 47% | 8 | 22% to 73% |
| 24 m | 147 | 76 | 52% | 7 | 25% to 78% |
| 30 m | 141 | 80 | 57% | 5 | 25% to 84% |
| 36 m | 135 | 78 | 58% | 4 | 23% to 86% |
| 42 m | 129 | 77 | 60% | 4 | 24% to 87% |
| 48 m | 123 | 64 | 52% | 3 | 17% to 85% |
| 54 m | 117 | 51 | 44% | 3 | 12% to 81% |
| 60 m | 111 | 44 | 40% | 2 | 8% to 83% |
| 72 m | 99 | 39 | 39% | 2 | 8% to 83% |
| 84 m | 87 | 38 | 44% | 2 | 10% to 85% |

**Cash (repo - 50 bp, after slab tax)**

| Horizon | Windows scored | Met | P(meet) | Non-overlapping windows | 90 % interval (on non-overlapping) |
| --- | --- | --- | --- | --- | --- |
| 1 m | 170 | 117 | 69% | 170 | 63% to 74% |
| 2 m | 169 | 123 | 73% | 85 | 64% to 80% |
| 3 m | 168 | 108 | 64% | 56 | 53% to 74% |
| 4 m | 167 | 108 | 65% | 42 | 52% to 76% |
| 6 m | 165 | 113 | 68% | 28 | 53% to 81% |
| 9 m | 162 | 116 | 72% | 18 | 52% to 85% |
| 12 m | 159 | 115 | 72% | 14 | 50% to 87% |
| 15 m | 156 | 118 | 76% | 11 | 51% to 90% |
| 18 m | 153 | 122 | 80% | 9 | 52% to 93% |
| 21 m | 150 | 122 | 81% | 8 | 52% to 95% |
| 24 m | 147 | 124 | 84% | 7 | 53% to 96% |
| 30 m | 141 | 124 | 88% | 5 | 51% to 98% |
| 36 m | 135 | 121 | 90% | 4 | 49% to 99% |
| 42 m | 129 | 125 | 97% | 4 | 56% to 100% |
| 48 m | 123 | 121 | 98% | 3 | 51% to 100% |
| 54 m | 117 | 116 | 99% | 3 | 52% to 100% |
| 60 m | 111 | 110 | 99% | 2 | 42% to 100% |
| 72 m | 99 | 99 | 100% | 2 | 43% to 100% |
| 84 m | 87 | 87 | 100% | 2 | 43% to 100% |

**NIFTY 500 TRI, same window**

| Horizon | Windows scored | Met | P(meet) | Non-overlapping windows | 90 % interval (on non-overlapping) |
| --- | --- | --- | --- | --- | --- |
| 1 m | 170 | 92 | 54% | 170 | 48% to 60% |
| 2 m | 169 | 89 | 53% | 85 | 44% to 61% |
| 3 m | 168 | 95 | 57% | 56 | 46% to 67% |
| 4 m | 167 | 94 | 56% | 42 | 44% to 68% |
| 6 m | 165 | 95 | 58% | 28 | 42% to 72% |
| 9 m | 162 | 103 | 64% | 18 | 44% to 79% |
| 12 m | 159 | 99 | 62% | 14 | 41% to 80% |
| 15 m | 156 | 90 | 58% | 11 | 34% to 78% |
| 18 m | 153 | 92 | 60% | 9 | 34% to 81% |
| 21 m | 150 | 94 | 63% | 8 | 35% to 84% |
| 24 m | 147 | 100 | 68% | 7 | 38% to 88% |
| 30 m | 141 | 100 | 71% | 5 | 36% to 91% |
| 36 m | 135 | 102 | 76% | 4 | 36% to 94% |
| 42 m | 129 | 97 | 75% | 4 | 36% to 94% |
| 48 m | 123 | 95 | 77% | 3 | 33% to 96% |
| 54 m | 117 | 90 | 77% | 3 | 32% to 96% |
| 60 m | 111 | 82 | 74% | 2 | 24% to 96% |
| 72 m | 99 | 76 | 77% | 2 | 26% to 97% |
| 84 m | 87 | 70 | 80% | 2 | 28% to 98% |

**No loss (≥ 0 %)**

| Horizon | Windows scored | Met | P(meet) | Non-overlapping windows | 90 % interval (on non-overlapping) |
| --- | --- | --- | --- | --- | --- |
| 1 m | 170 | 117 | 69% | 170 | 63% to 74% |
| 2 m | 169 | 126 | 75% | 85 | 66% to 81% |
| 3 m | 168 | 118 | 70% | 56 | 59% to 79% |
| 4 m | 167 | 120 | 72% | 42 | 59% to 82% |
| 6 m | 165 | 123 | 75% | 28 | 59% to 85% |
| 9 m | 162 | 128 | 79% | 18 | 60% to 90% |
| 12 m | 159 | 128 | 81% | 14 | 59% to 92% |
| 15 m | 156 | 132 | 85% | 11 | 60% to 95% |
| 18 m | 153 | 134 | 88% | 9 | 61% to 97% |
| 21 m | 150 | 135 | 90% | 8 | 62% to 98% |
| 24 m | 147 | 133 | 90% | 7 | 60% to 98% |
| 30 m | 141 | 134 | 95% | 5 | 59% to 100% |
| 36 m | 135 | 133 | 99% | 4 | 58% to 100% |
| 42 m | 129 | 128 | 99% | 4 | 59% to 100% |
| 48 m | 123 | 123 | 100% | 3 | 53% to 100% |
| 54 m | 117 | 117 | 100% | 3 | 53% to 100% |
| 60 m | 111 | 111 | 100% | 2 | 43% to 100% |
| 72 m | 99 | 99 | 100% | 2 | 43% to 100% |
| 84 m | 87 | 87 | 100% | 2 | 43% to 100% |

**12 % a year**

| Horizon | Windows scored | Met | P(meet) | Non-overlapping windows | 90 % interval (on non-overlapping) |
| --- | --- | --- | --- | --- | --- |
| 1 m | 170 | 66 | 39% | 170 | 33% to 45% |
| 2 m | 169 | 84 | 50% | 85 | 41% to 58% |
| 3 m | 168 | 82 | 49% | 56 | 38% to 60% |
| 4 m | 167 | 82 | 49% | 42 | 37% to 61% |
| 6 m | 165 | 82 | 50% | 28 | 35% to 65% |
| 9 m | 162 | 81 | 50% | 18 | 32% to 68% |
| 12 m | 159 | 84 | 53% | 14 | 32% to 72% |
| 15 m | 156 | 90 | 58% | 11 | 34% to 78% |
| 18 m | 153 | 93 | 61% | 9 | 35% to 82% |
| 21 m | 150 | 93 | 62% | 8 | 34% to 84% |
| 24 m | 147 | 99 | 67% | 7 | 37% to 88% |
| 30 m | 141 | 104 | 74% | 5 | 38% to 93% |
| 36 m | 135 | 101 | 75% | 4 | 35% to 94% |
| 42 m | 129 | 95 | 74% | 4 | 35% to 94% |
| 48 m | 123 | 92 | 75% | 3 | 31% to 95% |
| 54 m | 117 | 87 | 74% | 3 | 31% to 95% |
| 60 m | 111 | 89 | 80% | 2 | 28% to 98% |
| 72 m | 99 | 76 | 77% | 2 | 26% to 97% |
| 84 m | 87 | 73 | 84% | 2 | 30% to 98% |

**15 % a year**

| Horizon | Windows scored | Met | P(meet) | Non-overlapping windows | 90 % interval (on non-overlapping) |
| --- | --- | --- | --- | --- | --- |
| 1 m | 170 | 66 | 39% | 170 | 33% to 45% |
| 2 m | 169 | 75 | 44% | 85 | 36% to 53% |
| 3 m | 168 | 81 | 48% | 56 | 38% to 59% |
| 4 m | 167 | 76 | 46% | 42 | 34% to 58% |
| 6 m | 165 | 75 | 45% | 28 | 31% to 61% |
| 9 m | 162 | 71 | 44% | 18 | 27% to 63% |
| 12 m | 159 | 76 | 48% | 14 | 28% to 68% |
| 15 m | 156 | 74 | 47% | 11 | 26% to 70% |
| 18 m | 153 | 82 | 54% | 9 | 29% to 77% |
| 21 m | 150 | 79 | 53% | 8 | 27% to 77% |
| 24 m | 147 | 86 | 59% | 7 | 30% to 82% |
| 30 m | 141 | 92 | 65% | 5 | 31% to 89% |
| 36 m | 135 | 93 | 69% | 4 | 31% to 92% |
| 42 m | 129 | 83 | 64% | 4 | 28% to 90% |
| 48 m | 123 | 82 | 67% | 3 | 25% to 92% |
| 54 m | 117 | 74 | 63% | 3 | 23% to 91% |
| 60 m | 111 | 72 | 65% | 2 | 19% to 94% |
| 72 m | 99 | 56 | 57% | 2 | 15% to 91% |
| 84 m | 87 | 56 | 64% | 2 | 19% to 93% |


## Distribution by horizon (month-start grid)

| Horizon | Windows | Worst | 10th pct | Median | 90th pct | Best | Worst cumulative (start) | Best cumulative (start) | P(loss) | Worst max DD | Longest under water | Median TRI walk-away | Median excess vs TRI | Median parked share |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 m | 170 | -85.4% | -43.8% | 0.0% | 136.9% | 537.0% | -12.4% (2018-09-03) | +15.3% (2023-07-03) | 31% | 14.5% | 30 d | 7.1% | +5.1 pp | 0% |
| 2 m | 169 | -68.2% | -33.1% | 11.7% | 82.8% | 409.5% | -16.7% (2018-09-03) | +30.6% (2023-06-01) | 25% | 20.7% | 60 d | 14.4% | +3.0 pp | 0% |
| 3 m | 168 | -57.4% | -26.8% | 10.7% | 68.0% | 360.9% | -18.6% (2018-09-03) | +46.4% (2023-06-01) | 30% | 20.7% | 91 d | 13.5% | +2.8 pp | 0% |
| 4 m | 167 | -45.3% | -21.0% | 10.9% | 72.5% | 248.4% | -17.9% (2018-09-03) | +50.7% (2023-06-01) | 28% | 21.4% | 122 d | 12.9% | +2.7 pp | 0% |
| 6 m | 165 | -38.2% | -13.4% | 11.8% | 66.2% | 172.3% | -21.0% (2024-09-02) | +64.8% (2023-06-01) | 25% | 24.3% | 182 d | 8.8% | +2.7 pp | 14% |
| 9 m | 162 | -23.9% | -10.8% | 11.8% | 64.6% | 138.8% | -18.6% (2018-05-02) | +92.2% (2023-05-02) | 21% | 27.4% | 274 d | 9.2% | +4.0 pp | 11% |
| 12 m | 159 | -20.6% | -5.4% | 14.0% | 62.8% | 105.3% | -20.4% (2018-09-03) | +104.9% (2023-05-02) | 19% | 28.3% | 364 d | 8.2% | +3.5 pp | 16% |
| 15 m | 156 | -17.1% | -2.4% | 14.0% | 56.5% | 99.2% | -20.8% (2018-05-02) | +136.5% (2023-05-02) | 15% | 28.3% | 456 d | 10.9% | +2.7 pp | 20% |
| 18 m | 153 | -11.8% | -0.9% | 15.6% | 49.5% | 84.7% | -17.1% (2018-02-01) | +150.4% (2023-02-01) | 12% | 28.3% | 547 d | 12.7% | +2.6 pp | 17% |
| 21 m | 150 | -10.3% | 0.2% | 15.9% | 45.9% | 63.9% | -17.3% (2018-01-01) | +137.1% (2022-11-01) | 10% | 28.3% | 639 d | 14.6% | +2.9 pp | 19% |
| 24 m | 147 | -9.0% | 1.9% | 18.4% | 40.0% | 61.1% | -17.1% (2018-05-02) | +159.7% (2022-08-01) | 10% | 28.3% | 729 d | 14.9% | +3.4 pp | 17% |
| 30 m | 141 | -5.4% | 2.3% | 21.2% | 36.5% | 45.0% | -13.1% (2018-01-01) | +153.2% (2022-04-01) | 5% | 33.2% | 912 d | 15.2% | +4.8 pp | 17% |
| 36 m | 135 | -1.0% | 3.6% | 21.5% | 36.4% | 51.4% | -2.8% (2017-04-03) | +246.9% (2021-02-01) | 1% | 33.2% | 1052 d | 14.0% | +5.0 pp | 22% |
| 42 m | 129 | -0.2% | 5.8% | 19.9% | 36.8% | 52.0% | -0.6% (2016-10-03) | +332.1% (2021-02-01) | 1% | 33.2% | 1070 d | 13.3% | +4.5 pp | 21% |
| 48 m | 123 | 2.6% | 6.5% | 18.5% | 38.6% | 48.6% | +10.7% (2015-08-03) | +386.4% (2020-08-03) | 0% | 33.2% | 1070 d | 12.8% | +3.7 pp | 21% |
| 54 m | 117 | 3.8% | 7.4% | 17.1% | 37.1% | 42.2% | +18.3% (2015-10-01) | +387.6% (2020-04-01) | 0% | 33.2% | 1070 d | 13.3% | +2.8 pp | 20% |
| 60 m | 111 | 3.6% | 8.0% | 16.4% | 34.6% | 38.4% | +19.4% (2015-04-01) | +407.5% (2019-08-01) | 0% | 33.2% | 1070 d | 14.3% | +2.3 pp | 20% |
| 72 m | 99 | 7.6% | 10.2% | 16.1% | 29.8% | 31.3% | +55.2% (2014-07-01) | +412.6% (2020-09-01) | 0% | 33.2% | 1070 d | 14.0% | +2.1 pp | 19% |
| 84 m | 87 | 8.9% | 10.3% | 17.0% | 26.0% | 27.3% | +81.4% (2013-01-01) | +442.2% (2019-10-01) | 0% | 33.2% | 1086 d | 13.1% | +3.4 pp | 20% |
| to span end | 170 | 2.3% | 14.7% | 20.5% | 28.8% | 57.0% | +0.3% (2026-09-01) | +972.8% (2012-09-03) | 0% | 33.2% | 1089 d | 11.3% | +10.4 pp | 25% |

## Distribution by horizon (mid-month grid)

| Horizon | Windows | Worst | 10th pct | Median | 90th pct | Best | Worst cumulative (start) | Best cumulative (start) | P(loss) | Worst max DD | Longest under water | Median TRI walk-away | Median excess vs TRI | Median parked share |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 m | 170 | -88.7% | -56.2% | 3.0% | 132.7% | 485.2% | -13.9% (2018-09-17) | +13.8% (2021-04-15) | 32% | 18.6% | 30 d | 10.2% | +2.8 pp | 0% |
| 3 m | 168 | -52.0% | -28.0% | 8.8% | 86.7% | 269.0% | -16.6% (2018-08-16) | +37.9% (2023-06-15) | 33% | 21.3% | 91 d | 10.9% | +1.6 pp | 0% |
| 6 m | 165 | -35.1% | -13.5% | 13.1% | 76.2% | 183.2% | -19.2% (2024-08-16) | +66.6% (2023-04-17) | 29% | 22.0% | 182 d | 9.3% | +3.4 pp | 8% |
| 9 m | 162 | -30.7% | -9.0% | 13.2% | 62.5% | 146.8% | -23.7% (2018-01-15) | +95.1% (2023-04-17) | 25% | 26.3% | 273 d | 9.1% | +3.7 pp | 11% |
| 12 m | 159 | -21.3% | -6.3% | 13.0% | 60.0% | 106.3% | -21.2% (2018-01-15) | +104.6% (2023-04-17) | 22% | 27.6% | 364 d | 8.2% | +3.6 pp | 20% |
| 18 m | 152 | -15.3% | -2.0% | 15.6% | 54.6% | 86.9% | -21.9% (2018-01-15) | +154.8% (2023-04-17) | 14% | 28.2% | 546 d | 12.8% | +3.1 pp | 19% |
| 24 m | 146 | -8.6% | 1.1% | 18.4% | 41.2% | 64.8% | -16.4% (2018-01-15) | +171.0% (2022-07-15) | 9% | 28.2% | 729 d | 15.1% | +3.7 pp | 17% |
| 36 m | 134 | -0.7% | 5.4% | 20.2% | 35.2% | 45.8% | -2.0% (2017-04-17) | +209.1% (2021-01-15) | 1% | 33.4% | 1053 d | 14.0% | +5.0 pp | 21% |
| 48 m | 122 | 2.8% | 6.2% | 18.2% | 36.8% | 49.6% | +11.7% (2015-08-17) | +400.3% (2020-07-15) | 0% | 33.4% | 1074 d | 13.4% | +3.2 pp | 21% |
| 60 m | 110 | 3.2% | 7.9% | 16.6% | 34.6% | 39.1% | +17.1% (2015-04-15) | +420.3% (2019-07-15) | 0% | 33.4% | 1074 d | 14.4% | +1.9 pp | 20% |
| 84 m | 86 | 8.9% | 10.4% | 16.9% | 26.0% | 27.6% | +81.5% (2013-04-15) | +450.0% (2017-08-16) | 0% | 33.4% | 1086 d | 13.2% | +3.8 pp | 20% |
| to span end | 169 | 1.8% | 14.2% | 20.3% | 28.6% | 43.7% | +0.1% (2026-09-15) | +954.4% (2012-09-17) | 0% | 33.4% | 1089 d | 11.2% | +10.8 pp | 25% |

## Named intervals

| Key | Interval | Regime | Start → end | NIFTY 500 P/E at start | NIFTY 500 below its high at start | D13 regime at start | Floor | Walk-away after-tax XIRR | Realised after-tax XIRR (decision rule) | Pre-tax XIRR | Max DD | Longest under water | NIFTY 500 TRI walk-away | Excess vs TRI | Cash after tax | Excess vs cash | Trades | Charges | Parked share |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| N01 | Full span | mixed | 2012-07-04 → 2026-10-15 | n/a (series opens 2012-10-01) | 19.2% | on | ₹10 cr | **18.0%** | 18.1% | 20.8% | 31.6% | 1086 d | 12.9% | +5.1 pp | 3.9% | +14.1 pp | 3287 | ₹322,160 | 22% |
| N01 | Full span | mixed | 2012-07-04 → 2026-10-15 | n/a (series opens 2012-10-01) | 19.2% | on | ₹1 cr (info) | **n/a** | 17.2% | 20.2% | 28.4% | 1037 d | 12.9% | n/a | 3.9% | n/a | 3418 | ₹343,244 | 22% |
| N02 | D13 decade (F1-F3 continuous) | mixed | 2016-09-01 → 2026-08-31 | 28.2 | 0.2% | on | ₹10 cr | **20.8%** | 21.2% | 24.2% | 29.9% | 1039 d | 12.4% | +8.4 pp | 3.5% | +17.3 pp | 2010 | ₹178,320 | 20% |
| N02 | D13 decade (F1-F3 continuous) | mixed | 2016-09-01 → 2026-08-31 | 28.2 | 0.2% | on | ₹1 cr (info) | **n/a** | 21.3% | 25.1% | 28.7% | 1043 d | 12.4% | n/a | 3.5% | n/a | 2235 | ₹220,343 | 20% |
| N03 | Six-year window of the old ~23 % figures | mixed | 2019-07-01 → 2026-10-15 | 31.1 | 2.0% | on | ₹10 cr | **25.6%** | 25.8% | 30.3% | 19.9% | 292 d | 12.0% | +13.6 pp | 3.4% | +22.2 pp | 1523 | ₹135,684 | 26% |
| N03 | Six-year window of the old ~23 % figures | mixed | 2019-07-01 → 2026-10-15 | 31.1 | 2.0% | on | ₹1 cr (info) | **23.4%** | 23.4% | 28.3% | 18.8% | 407 d | 12.0% | +11.4 pp | 3.4% | +20.0 pp | 1599 | ₹149,039 | 26% |
| N04 | First year of the span | sideways | 2012-07-04 → 2013-07-03 | n/a (series opens 2012-10-01) | 19.2% | on | ₹10 cr | **1.7%** | 1.8% | 2.7% | 13.1% | 180 d | 5.9% | -4.2 pp | 5.2% | -3.5 pp | 161 | ₹6,640 | 0% |
| N04 | First year of the span | sideways | 2012-07-04 → 2013-07-03 | n/a (series opens 2012-10-01) | 19.2% | on | ₹1 cr (info) | **-11.0%** | -10.9% | -10.2% | 23.0% | 181 d | 5.9% | -16.9 pp | 5.2% | -16.2 pp | 171 | ₹7,947 | 0% |
| N05 | Taper tantrum: Jan-2013 high to Aug-2013 low | bear | 2013-01-15 → 2013-08-28 | 18.8 | 6.3% | on | ₹10 cr | **-28.4%** | -28.3% | -28.1% | 18.8% | 225 d | -25.1% | -3.3 pp | 4.4% | -32.8 pp | 87 | ₹3,727 | 12% |
| N05 | Taper tantrum: Jan-2013 high to Aug-2013 low | bear | 2013-01-15 → 2013-08-28 | 18.8 | 6.3% | on | ₹1 cr (info) | **-27.6%** | -27.6% | -27.0% | 17.7% | 225 d | -25.1% | -2.6 pp | 4.4% | -32.0 pp | 92 | ₹4,025 | 12% |
| N06 | Aug-2013 low, 12 months | recovery | 2013-08-28 → 2014-08-27 | 15.1 | 21.2% | OFF (parked) | ₹10 cr | **25.0%** | 27.8% | 29.5% | 6.9% | 59 d | 51.2% | -26.2 pp | 4.9% | +20.2 pp | 101 | ₹4,358 | 18% |
| N06 | Aug-2013 low, 12 months | recovery | 2013-08-28 → 2014-08-27 | 15.1 | 21.2% | OFF (parked) | ₹1 cr (info) | **31.9%** | 34.1% | 37.4% | 5.7% | 36 d | 51.2% | -19.3 pp | 4.9% | +27.0 pp | 109 | ₹5,289 | 18% |
| N07 | 2014 election year | bull | 2014-01-01 → 2014-12-31 | 19.0 | 4.0% | on | ₹10 cr | **29.9%** | 34.7% | 35.2% | 10.1% | 61 d | 33.0% | -3.1 pp | 4.9% | +25.0 pp | 123 | ₹5,011 | 0% |
| N07 | 2014 election year | bull | 2014-01-01 → 2014-12-31 | 19.0 | 4.0% | on | ₹1 cr (info) | **42.0%** | 45.3% | 49.4% | 5.2% | 47 d | 33.0% | +9.0 pp | 4.9% | +37.1 pp | 150 | ₹6,971 | 0% |
| N08 | Mar-2015 all-time high to Feb-2016 low | bear | 2015-03-03 → 2016-02-25 | 25.3 | 0.0% | on | ₹10 cr | **-3.3%** | -3.2% | -2.5% | 15.1% | 190 d | -20.7% | +17.4 pp | 4.4% | -7.7 pp | 51 | ₹3,015 | 49% |
| N08 | Mar-2015 all-time high to Feb-2016 low | bear | 2015-03-03 → 2016-02-25 | 25.3 | 0.0% | on | ₹1 cr (info) | **-11.3%** | -11.3% | -10.5% | 15.0% | 322 d | -20.7% | +9.3 pp | 4.4% | -15.8 pp | 61 | ₹3,354 | 49% |
| N09 | Mar-2015 all-time high, 24 months (flat) | sideways | 2015-03-03 → 2017-03-02 | 25.3 | 0.0% | on | ₹10 cr | **10.5%** | 11.3% | 12.7% | 15.5% | 348 d | 3.3% | +7.2 pp | 4.5% | +6.0 pp | 159 | ₹8,439 | 41% |
| N09 | Mar-2015 all-time high, 24 months (flat) | sideways | 2015-03-03 → 2017-03-02 | 25.3 | 0.0% | on | ₹1 cr (info) | **5.9%** | 6.3% | 7.4% | 18.2% | 448 d | 3.3% | +2.6 pp | 4.5% | +1.5 pp | 179 | ₹8,756 | 41% |
| N10 | Feb-2016 low, 12 months | recovery | 2016-02-25 → 2017-02-23 | 20.5 | 20.1% | OFF (parked) | ₹10 cr | **22.0%** | 24.8% | 26.6% | 13.8% | 122 d | 28.9% | -6.9 pp | 4.0% | +18.1 pp | 83 | ₹4,453 | 34% |
| N10 | Feb-2016 low, 12 months | recovery | 2016-02-25 → 2017-02-23 | 20.5 | 20.1% | OFF (parked) | ₹1 cr (info) | **15.2%** | 16.2% | 18.7% | 18.0% | 116 d | 28.9% | -13.7 pp | 4.0% | +11.3 pp | 100 | ₹5,096 | 34% |
| N11 | Demonetisation, 3 months | crash | 2016-11-09 → 2017-02-08 | 26.2 | 5.0% | on | ₹10 cr | **9.3%** | 11.8% | 12.3% | 11.0% | 83 d | 16.8% | -7.4 pp | 3.7% | +5.6 pp | 45 | ₹2,359 | 33% |
| N11 | Demonetisation, 3 months | crash | 2016-11-09 → 2017-02-08 | 26.2 | 5.0% | on | ₹1 cr (info) | **6.1%** | 7.6% | 8.5% | 11.9% | 85 d | 16.8% | -10.7 pp | 3.7% | +2.4 pp | 37 | ₹1,773 | 33% |
| N12 | Demonetisation, 12 months | bull | 2016-11-09 → 2017-11-08 | 26.2 | 5.0% | on | ₹10 cr | **18.3%** | 19.5% | 21.5% | 11.0% | 83 d | 23.0% | -4.7 pp | 4.0% | +14.4 pp | 164 | ₹7,758 | 8% |
| N12 | Demonetisation, 12 months | bull | 2016-11-09 → 2017-11-08 | 26.2 | 5.0% | on | ₹1 cr (info) | **49.4%** | 53.0% | 57.3% | 11.9% | 85 d | 23.0% | +26.3 pp | 4.0% | +45.4 pp | 119 | ₹6,204 | 8% |
| N13 | 2017 bull year | bull | 2017-01-02 → 2017-12-29 | 25.5 | 8.2% | OFF (parked) | ₹10 cr | **27.5%** | 30.3% | 32.4% | 7.0% | 91 d | 31.7% | -4.2 pp | 3.7% | +23.8 pp | 156 | ₹7,135 | 8% |
| N13 | 2017 bull year | bull | 2017-01-02 → 2017-12-29 | 25.5 | 8.2% | OFF (parked) | ₹1 cr (info) | **55.4%** | 61.6% | 64.9% | 7.9% | 56 d | 31.7% | +23.7 pp | 3.7% | +51.7 pp | 92 | ₹5,074 | 8% |
| N14 | 2018 small/mid-cap bear: Jan-2018 small-cap peak | bear | 2018-01-15 → 2019-08-30 | 32.9 | 0.0% | on | ₹10 cr | **-15.7%** | -15.7% | -15.4% | 26.3% | 592 d | -3.5% | -12.2 pp | 3.8% | -19.6 pp | 178 | ₹8,116 | 10% |
| N14 | 2018 small/mid-cap bear: Jan-2018 small-cap peak | bear | 2018-01-15 → 2019-08-30 | 32.9 | 0.0% | on | ₹1 cr (info) | **-18.6%** | -18.6% | -18.3% | 31.7% | 592 d | -3.5% | -15.1 pp | 3.8% | -22.4 pp | 186 | ₹8,174 | 10% |
| N15 | Jan-2018 high, 24 months (flat broad market) | sideways | 2018-01-23 → 2020-01-17 | 33.2 | 0.0% | on | ₹10 cr | **-7.2%** | -7.1% | -6.8% | 25.4% | 724 d | 2.3% | -9.5 pp | 3.8% | -11.0 pp | 231 | ₹9,847 | 12% |
| N15 | Jan-2018 high, 24 months (flat broad market) | sideways | 2018-01-23 → 2020-01-17 | 33.2 | 0.0% | on | ₹1 cr (info) | **-8.0%** | -8.0% | -7.6% | 30.0% | 724 d | 2.3% | -10.3 pp | 3.8% | -11.8 pp | 225 | ₹10,052 | 12% |
| N16 | IL&FS sell-off, 6 months | crash | 2018-09-21 → 2019-03-20 | 32.4 | 5.6% | on | ₹10 cr | **-16.8%** | -16.6% | -16.2% | 14.6% | 180 d | 2.1% | -18.9 pp | 3.7% | -20.5 pp | 84 | ₹4,126 | 16% |
| N16 | IL&FS sell-off, 6 months | crash | 2018-09-21 → 2019-03-20 | 32.4 | 5.6% | on | ₹1 cr (info) | **-14.3%** | -14.1% | -13.7% | 15.5% | 180 d | 2.1% | -16.4 pp | 3.7% | -18.0 pp | 81 | ₹4,019 | 16% |
| N17 | Aug-2018 all-time high, 12 months | bear | 2018-08-31 → 2019-08-30 | 34.5 | 0.0% | on | ₹10 cr | **-21.9%** | -21.8% | -21.6% | 22.6% | 364 d | -9.4% | -12.4 pp | 3.7% | -25.6 pp | 139 | ₹6,222 | 16% |
| N17 | Aug-2018 all-time high, 12 months | bear | 2018-08-31 → 2019-08-30 | 34.5 | 0.0% | on | ₹1 cr (info) | **-20.7%** | -20.6% | -20.3% | 23.8% | 357 d | -9.4% | -11.2 pp | 3.7% | -24.4 pp | 141 | ₹6,470 | 16% |
| N18 | COVID: from just before the peak, calendar 2020 | mixed | 2020-01-01 → 2020-12-31 | 30.9 | 0.4% | on | ₹10 cr | **20.2%** | 23.1% | 24.3% | 19.6% | 172 d | 14.6% | +5.7 pp | 2.5% | +17.7 pp | 102 | ₹4,780 | 41% |
| N18 | COVID: from just before the peak, calendar 2020 | mixed | 2020-01-01 → 2020-12-31 | 30.9 | 0.4% | on | ₹1 cr (info) | **41.4%** | 47.5% | 48.9% | 19.5% | 168 d | 14.6% | +26.9 pp | 2.5% | +39.0 pp | 92 | ₹4,905 | 41% |
| N19 | COVID: at the Jan-2020 peak, 3 months | crash | 2020-01-17 → 2020-04-16 | 31.5 | 0.0% | on | ₹10 cr | **-23.3%** | -23.1% | -22.8% | 16.5% | 56 d | -71.7% | +48.4 pp | 2.7% | -26.0 pp | 31 | ₹1,782 | 48% |
| N19 | COVID: at the Jan-2020 peak, 3 months | crash | 2020-01-17 → 2020-04-16 | 31.5 | 0.0% | on | ₹1 cr (info) | **-31.7%** | -31.5% | -30.9% | 19.6% | 56 d | -71.7% | +40.1 pp | 2.7% | -34.4 pp | 34 | ₹2,016 | 48% |
| N20 | COVID: at the Jan-2020 peak, 24 months | mixed | 2020-01-17 → 2022-01-17 | 31.5 | 0.0% | on | ₹10 cr | **42.1%** | 44.2% | 47.6% | 16.5% | 166 d | 23.9% | +18.2 pp | 2.5% | +39.6 pp | 243 | ₹14,145 | 21% |
| N20 | COVID: at the Jan-2020 peak, 24 months | mixed | 2020-01-17 → 2022-01-17 | 31.5 | 0.0% | on | ₹1 cr (info) | **50.2%** | 51.8% | 57.0% | 19.6% | 167 d | 23.9% | +26.2 pp | 2.5% | +47.7 pp | 274 | ₹17,064 | 21% |
| N21 | COVID: at the Mar-2020 trough, 3 months | recovery | 2020-03-23 → 2020-06-22 | 19.4 | 38.1% | OFF (parked) | ₹10 cr | **2.1%** | 2.1% | 3.0% | 0.0% | 0 d | 216.2% | -214.1 pp | 2.1% | -0.0 pp | 0 | ₹0 | 100% |
| N21 | COVID: at the Mar-2020 trough, 3 months | recovery | 2020-03-23 → 2020-06-22 | 19.4 | 38.1% | OFF (parked) | ₹1 cr (info) | **2.1%** | 2.1% | 3.0% | 0.0% | 0 d | 216.2% | -214.1 pp | 2.1% | -0.0 pp | 0 | ₹0 | 100% |
| N22 | COVID: at the Mar-2020 trough, 12 months | recovery | 2020-03-23 → 2021-03-22 | 19.4 | 38.1% | OFF (parked) | ₹10 cr | **43.1%** | 49.6% | 51.6% | 5.8% | 48 d | 83.6% | -40.5 pp | 2.4% | +40.7 pp | 84 | ₹4,227 | 36% |
| N22 | COVID: at the Mar-2020 trough, 12 months | recovery | 2020-03-23 → 2021-03-22 | 19.4 | 38.1% | OFF (parked) | ₹1 cr (info) | **48.5%** | 55.5% | 58.0% | 8.4% | 58 d | 83.6% | -35.1 pp | 2.4% | +46.2 pp | 71 | ₹4,235 | 36% |
| N23 | COVID: in the recovery, Jun-2020, 12 months | recovery | 2020-06-01 → 2021-05-31 | 25.9 | 20.4% | OFF (parked) | ₹10 cr | **69.7%** | 76.5% | 78.8% | 5.8% | 48 d | 59.0% | +10.7 pp | 2.2% | +67.5 pp | 102 | ₹5,420 | 18% |
| N23 | COVID: in the recovery, Jun-2020, 12 months | recovery | 2020-06-01 → 2021-05-31 | 25.9 | 20.4% | OFF (parked) | ₹1 cr (info) | **67.4%** | 71.0% | 76.5% | 8.4% | 58 d | 59.0% | +8.5 pp | 2.2% | +65.2 pp | 104 | ₹6,415 | 18% |
| N24 | 2021 bull run to the Oct-2021 high | bull | 2021-01-01 → 2021-10-18 | 44.5 | 0.0% | on | ₹10 cr | **94.6%** | 104.3% | 108.7% | 5.1% | 37 d | 42.8% | +51.8 pp | 2.3% | +92.3 pp | 89 | ₹5,412 | 0% |
| N24 | 2021 bull run to the Oct-2021 high | bull | 2021-01-01 → 2021-10-18 | 44.5 | 0.0% | on | ₹1 cr (info) | **46.1%** | 49.2% | 54.2% | 7.5% | 38 d | 42.8% | +3.3 pp | 2.3% | +43.8 pp | 151 | ₹7,174 | 0% |
| N25 | Oct-2021 all-time high, 12 months | bear | 2021-10-18 → 2022-10-17 | 29.5 | 0.0% | on | ₹10 cr | **0.8%** | 1.0% | 1.6% | 18.0% | 323 d | -5.4% | +6.2 pp | 2.6% | -1.8 pp | 177 | ₹7,722 | 34% |
| N25 | Oct-2021 all-time high, 12 months | bear | 2021-10-18 → 2022-10-17 | 29.5 | 0.0% | on | ₹1 cr (info) | **8.8%** | 9.5% | 11.0% | 16.0% | 221 d | -5.4% | +14.1 pp | 2.6% | +6.1 pp | 136 | ₹6,704 | 34% |
| N26 | 2022: sideways, rate hikes | sideways | 2022-01-03 → 2022-12-30 | 25.8 | 4.1% | on | ₹10 cr | **7.1%** | 7.7% | 8.9% | 13.8% | 221 d | 2.0% | +5.1 pp | 2.8% | +4.3 pp | 144 | ₹7,094 | 34% |
| N26 | 2022: sideways, rate hikes | sideways | 2022-01-03 → 2022-12-30 | 25.8 | 4.1% | on | ₹1 cr (info) | **4.0%** | 3.8% | 5.3% | 17.7% | 347 d | 2.0% | +2.0 pp | 2.8% | +1.2 pp | 131 | ₹6,552 | 34% |
| N27 | Jun-2022 low, 12 months | recovery | 2022-06-20 → 2023-06-19 | 19.4 | 17.8% | OFF (parked) | ₹10 cr | **18.7%** | 21.4% | 22.4% | 16.6% | 189 d | 21.9% | -3.3 pp | 3.7% | +15.0 pp | 135 | ₹6,916 | 19% |
| N27 | Jun-2022 low, 12 months | recovery | 2022-06-20 → 2023-06-19 | 19.4 | 17.8% | OFF (parked) | ₹1 cr (info) | **21.6%** | 24.8% | 25.7% | 15.0% | 198 d | 21.9% | -0.4 pp | 3.7% | +17.9 pp | 144 | ₹6,691 | 19% |
| N28 | 2023-24 small-cap melt-up: Mar-2023 low to Sep-2024 peak | bull | 2023-03-28 → 2024-09-26 | 20.8 | 11.1% | OFF (parked) | ₹10 cr | **73.2%** | 75.0% | 87.4% | 17.5% | 76 d | 41.0% | +32.2 pp | 4.1% | +69.1 pp | 261 | ₹19,073 | 5% |
| N28 | 2023-24 small-cap melt-up: Mar-2023 low to Sep-2024 peak | bull | 2023-03-28 → 2024-09-26 | 20.8 | 11.1% | OFF (parked) | ₹1 cr (info) | **57.7%** | 58.3% | 69.2% | 15.5% | 76 d | 41.0% | +16.7 pp | 4.1% | +53.6 pp | 273 | ₹20,019 | 5% |
| N29 | Sep-2024 all-time high to Feb-2025 low | bear | 2024-09-26 → 2025-02-28 | 28.0 | 0.0% | on | ₹10 cr | **-37.3%** | -37.2% | -37.0% | 18.9% | 135 d | -39.0% | +1.7 pp | 3.5% | -40.7 pp | 57 | ₹3,187 | 40% |
| N29 | Sep-2024 all-time high to Feb-2025 low | bear | 2024-09-26 → 2025-02-28 | 28.0 | 0.0% | on | ₹1 cr (info) | **-32.2%** | -32.1% | -31.9% | 18.7% | 135 d | -39.0% | +6.8 pp | 3.5% | -35.7 pp | 70 | ₹3,861 | 40% |
| N30 | Sep-2024 all-time high, 12 months | bear | 2024-09-26 → 2025-09-25 | 28.0 | 0.0% | on | ₹10 cr | **-2.0%** | -1.9% | -1.5% | 18.9% | 293 d | -5.5% | +3.4 pp | 3.7% | -5.7 pp | 151 | ₹7,059 | 33% |
| N30 | Sep-2024 all-time high, 12 months | bear | 2024-09-26 → 2025-09-25 | 28.0 | 0.0% | on | ₹1 cr (info) | **4.8%** | 5.4% | 6.6% | 19.0% | 273 d | -5.5% | +10.3 pp | 3.7% | +1.1 pp | 175 | ₹8,316 | 33% |
| N31 | Sep-2024 all-time high to span end | mixed | 2024-09-26 → 2026-10-15 | 28.0 | 0.0% | on | ₹10 cr | **7.4%** | 7.6% | 8.4% | 18.9% | 293 d | -4.0% | +11.4 pp | 3.6% | +3.8 pp | 260 | ₹12,585 | 46% |
| N31 | Sep-2024 all-time high to span end | mixed | 2024-09-26 → 2026-10-15 | 28.0 | 0.0% | on | ₹1 cr (info) | **12.7%** | 12.8% | 15.7% | 19.0% | 273 d | -4.0% | +16.8 pp | 3.6% | +9.2 pp | 271 | ₹14,059 | 46% |
| N32 | Feb-2025 low, 12 months | recovery | 2025-02-28 → 2026-02-27 | 21.8 | 18.6% | OFF (parked) | ₹10 cr | **13.9%** | 14.0% | 18.0% | 9.9% | 58 d | 13.6% | +0.4 pp | 3.4% | +10.6 pp | 150 | ₹8,910 | 24% |
| N32 | Feb-2025 low, 12 months | recovery | 2025-02-28 → 2026-02-27 | 21.8 | 18.6% | OFF (parked) | ₹1 cr (info) | **17.2%** | 18.1% | 22.1% | 9.8% | 104 d | 13.6% | +3.7 pp | 3.4% | +13.9 pp | 151 | ₹9,040 | 24% |
| N33 | Latest year | mixed | 2025-10-15 → 2026-10-15 | 24.5 | 3.6% | on | ₹10 cr | **11.1%** | 13.7% | 14.2% | 12.9% | 237 d | -4.6% | +15.7 pp | 3.2% | +7.8 pp | 107 | ₹5,346 | 62% |
| N33 | Latest year | mixed | 2025-10-15 → 2026-10-15 | 24.5 | 3.6% | on | ₹1 cr (info) | **12.2%** | 13.5% | 15.7% | 10.8% | 227 d | -4.6% | +16.9 pp | 3.2% | +9.0 pp | 90 | ₹4,918 | 62% |
| N34 | Latest 6 months | mixed | 2026-04-15 → 2026-10-15 | 23.4 | 6.6% | OFF (parked) | ₹10 cr | **21.6%** | 24.2% | 27.7% | 2.1% | 14 d | -3.2% | +24.8 pp | 3.1% | +18.5 pp | 26 | ₹1,782 | 83% |
| N34 | Latest 6 months | mixed | 2026-04-15 → 2026-10-15 | 23.4 | 6.6% | OFF (parked) | ₹1 cr (info) | **15.5%** | 17.9% | 20.1% | 2.5% | 22 d | -3.2% | +18.7 pp | 3.1% | +12.4 pp | 21 | ₹1,456 | 83% |
| N35 | Latest 3 months | mixed | 2026-07-15 → 2026-10-15 | 23.0 | 3.4% | OFF (parked) | ₹10 cr | **43.7%** | 49.5% | 55.6% | 2.1% | 14 d | -17.8% | +61.5 pp | 2.8% | +40.9 pp | 26 | ₹1,767 | 68% |
| N35 | Latest 3 months | mixed | 2026-07-15 → 2026-10-15 | 23.0 | 3.4% | OFF (parked) | ₹1 cr (info) | **29.3%** | 34.5% | 37.6% | 2.5% | 22 d | -17.8% | +47.0 pp | 2.8% | +26.4 pp | 21 | ₹1,451 | 68% |
| N36 | First 7 years of the span | mixed | 2012-07-04 → 2019-07-03 | n/a (series opens 2012-10-01) | 19.2% | on | ₹10 cr | **11.6%** | 11.6% | 13.5% | 26.9% | 538 d | 14.0% | -2.4 pp | 4.6% | +7.0 pp | 928 | ₹60,507 | 16% |
| N36 | First 7 years of the span | mixed | 2012-07-04 → 2019-07-03 | n/a (series opens 2012-10-01) | 19.2% | on | ₹1 cr (info) | **n/a** | 12.3% | 14.3% | 25.6% | 543 d | 14.0% | n/a | 4.6% | n/a | 880 | ₹56,032 | 16% |
| N37 | Mar-2015 all-time high, 5 years | mixed | 2015-03-03 → 2020-03-02 | 25.3 | 0.0% | on | ₹10 cr | **5.5%** | 5.5% | 7.0% | 25.8% | 781 d | 5.7% | -0.2 pp | 4.1% | +1.4 pp | 578 | ₹31,676 | 21% |
| N37 | Mar-2015 all-time high, 5 years | mixed | 2015-03-03 → 2020-03-02 | 25.3 | 0.0% | on | ₹1 cr (info) | **n/a** | 6.7% | 8.1% | 31.4% | 780 d | 5.7% | n/a | 4.1% | n/a | 530 | ₹31,290 | 21% |
| N38 | COVID peak, 5 years | mixed | 2020-01-17 → 2025-01-16 | 31.5 | 0.0% | on | ₹10 cr | **34.0%** | 33.7% | 39.4% | 18.5% | 221 d | 16.1% | +18.0 pp | 3.3% | +30.7 pp | 1010 | ₹86,007 | 17% |
| N38 | COVID peak, 5 years | mixed | 2020-01-17 → 2025-01-16 | 31.5 | 0.0% | on | ₹1 cr (info) | **35.3%** | 35.1% | 41.3% | 19.6% | 221 d | 16.1% | +19.3 pp | 3.3% | +32.0 pp | 1099 | ₹101,123 | 17% |
| N39 | COVID trough, 5 years | mixed | 2020-03-23 → 2025-03-21 | 19.4 | 38.1% | OFF (parked) | ₹10 cr | **33.0%** | 32.4% | 38.6% | 18.4% | 227 d | 26.6% | +6.4 pp | 3.3% | +29.6 pp | 949 | ₹84,033 | 20% |
| N39 | COVID trough, 5 years | mixed | 2020-03-23 → 2025-03-21 | 19.4 | 38.1% | OFF (parked) | ₹1 cr (info) | **31.7%** | 31.2% | 37.6% | 16.7% | 249 d | 26.6% | +5.1 pp | 3.3% | +28.4 pp | 1010 | ₹91,743 | 20% |
| N40 | 2017-2021, 5 years | mixed | 2017-01-02 → 2021-12-31 | 25.5 | 8.2% | OFF (parked) | ₹10 cr | **16.7%** | 17.5% | 19.0% | 30.9% | 1058 d | 16.9% | -0.2 pp | 3.3% | +13.4 pp | 655 | ₹34,723 | 15% |
| N40 | 2017-2021, 5 years | mixed | 2017-01-02 → 2021-12-31 | 25.5 | 8.2% | OFF (parked) | ₹1 cr (info) | **n/a** | 21.9% | 24.4% | 28.8% | 1042 d | 16.9% | n/a | 3.3% | n/a | 605 | ₹41,057 | 15% |

## Validation: exact runs against the grid run's prefix

| Window | Start → end | NAV points compared | NAV points differing | Fills through end (exact run / grid prefix) | Walk-away XIRR, exact run | from the grid run truncated | Gap |
| --- | --- | --- | --- | --- | --- | --- | --- |
| N02 | 2016-09-01 → 2026-08-31 | 2477 | 0 | 2010 / 2010 | 20.8% | 20.8% | 0.00 bp |
| N07 | 2014-01-01 → 2014-12-31 | 244 | 0 | 123 / 123 | 29.9% | 29.9% | 0.00 bp |
| N13 | 2017-01-02 → 2017-12-29 | 248 | 0 | 156 / 156 | 27.5% | 27.5% | 0.00 bp |
| N18 | 2020-01-01 → 2020-12-31 | 252 | 0 | 102 / 102 | 20.2% | 20.2% | 0.00 bp |
| N23 | 2020-06-01 → 2021-05-31 | 251 | 0 | 102 / 102 | 69.7% | 69.7% | 0.00 bp |
| N24 | 2021-01-01 → 2021-10-18 | 196 | 0 | 89 / 89 | 94.6% | 94.6% | 0.00 bp |
| N26 | 2022-01-03 → 2022-12-30 | 248 | 0 | 144 / 144 | 7.1% | 7.1% | 0.00 bp |
| N40 | 2017-01-02 → 2021-12-31 | 1239 | 0 | 655 / 655 | 16.7% | 16.7% | 0.00 bp |
| F1-test | 2016-09-01 → 2019-08-30 | 741 | 0 | 401 / 401 | 3.2% | 3.2% | 0.00 bp |
| F2-test | 2019-09-03 → 2022-08-30 | 744 | 0 | 394 / 394 | 27.7% | 27.7% | 0.00 bp |
| F3-test | 2022-09-01 → 2026-08-31 | 992 | 0 | 644 / 644 | 26.4% | 26.4% | 0.00 bp |

## Exact fresh starts against the continuous-run slice (pre-tax)

| Horizon | Cells | Median gap (exact - slice) | Mean absolute gap | Largest absolute gap (start) | Share where the slice is higher |
| --- | --- | --- | --- | --- | --- |
| 1 m | 170 | -1.7 pp | +25.8 pp | +183.0 pp (2026-08-03) | 54% |
| 2 m | 169 | +1.5 pp | +15.2 pp | +127.9 pp (2021-02-01) | 47% |
| 3 m | 168 | +0.5 pp | +11.0 pp | +106.7 pp (2021-02-01) | 49% |
| 4 m | 167 | +0.5 pp | +9.1 pp | +85.1 pp (2021-02-01) | 47% |
| 6 m | 165 | +0.2 pp | +6.3 pp | +56.4 pp (2021-02-01) | 47% |
| 9 m | 162 | +0.9 pp | +5.0 pp | +28.2 pp (2021-02-01) | 44% |
| 12 m | 159 | +0.7 pp | +3.9 pp | +20.5 pp (2021-02-01) | 44% |
| 15 m | 156 | +0.7 pp | +3.4 pp | -13.2 pp (2024-02-01) | 42% |
| 18 m | 153 | +0.7 pp | +3.2 pp | -10.8 pp (2023-09-01) | 44% |
| 21 m | 150 | +0.7 pp | +2.9 pp | +12.8 pp (2025-01-01) | 39% |
| 24 m | 147 | +0.9 pp | +2.4 pp | -9.3 pp (2023-04-03) | 39% |
| 30 m | 141 | +0.7 pp | +1.9 pp | +8.8 pp (2021-02-01) | 40% |
| 36 m | 135 | +1.0 pp | +1.6 pp | +7.3 pp (2021-02-01) | 35% |
| 42 m | 129 | +0.9 pp | +1.5 pp | +7.7 pp (2021-02-01) | 28% |
| 48 m | 123 | +1.1 pp | +1.4 pp | +5.4 pp (2021-02-01) | 20% |
| 54 m | 117 | +1.2 pp | +1.3 pp | +5.9 pp (2021-02-01) | 14% |
| 60 m | 111 | +1.3 pp | +1.4 pp | +5.1 pp (2021-02-01) | 10% |
| 72 m | 99 | +1.2 pp | +1.2 pp | +3.3 pp (2020-09-01) | 12% |
| 84 m | 87 | +1.3 pp | +1.2 pp | +2.6 pp (2016-09-01) | 11% |
| to span end | 170 | +1.8 pp | +2.8 pp | +50.6 pp (2026-08-03) | 10% |

## Start month

**12-month horizon** (month-start grid, walk-away after-tax XIRR)

| Start month | Windows | Median | Worst | P(≥ E) | P(≥ NIFTY 500 TRI) |
| --- | --- | --- | --- | --- | --- |
| Jan | 13 | 19.3% | -16.5% | 54% | 62% |
| Feb | 13 | 15.1% | -14.5% | 31% | 54% |
| Mar | 13 | 15.8% | -10.8% | 38% | 54% |
| Apr | 13 | 10.0% | -12.2% | 38% | 69% |
| May | 13 | 14.4% | -12.9% | 46% | 77% |
| Jun | 13 | 8.2% | -6.6% | 31% | 62% |
| Jul | 13 | 6.7% | -4.3% | 23% | 62% |
| Aug | 14 | 9.9% | -15.6% | 36% | 57% |
| Sep | 14 | 15.4% | -20.6% | 36% | 64% |
| Oct | 14 | 15.4% | -9.0% | 50% | 57% |
| Nov | 13 | 15.3% | -10.4% | 46% | 62% |
| Dec | 13 | 16.8% | -10.4% | 46% | 69% |

Spread of the twelve monthly medians: 12.6%; permutation p-value (2,000 relabellings, seed fixed): **0.96** — the chance that start months with no effect at all would produce a spread this wide.

**36-month horizon** (month-start grid, walk-away after-tax XIRR)

| Start month | Windows | Median | Worst | P(≥ E) | P(≥ NIFTY 500 TRI) |
| --- | --- | --- | --- | --- | --- |
| Jan | 11 | 18.2% | 2.9% | 64% | 64% |
| Feb | 11 | 21.5% | 3.0% | 55% | 73% |
| Mar | 11 | 22.1% | 2.6% | 55% | 73% |
| Apr | 11 | 23.0% | -1.0% | 55% | 73% |
| May | 11 | 24.2% | -0.2% | 55% | 73% |
| Jun | 11 | 22.0% | 1.4% | 55% | 91% |
| Jul | 11 | 19.9% | 1.1% | 55% | 73% |
| Aug | 12 | 20.9% | 1.7% | 75% | 83% |
| Sep | 12 | 19.9% | 2.9% | 58% | 75% |
| Oct | 12 | 20.3% | 1.7% | 58% | 83% |
| Nov | 11 | 23.4% | 1.5% | 55% | 73% |
| Dec | 11 | 22.0% | 3.7% | 55% | 73% |

Spread of the twelve monthly medians: 6.0%; permutation p-value (2,000 relabellings, seed fixed): **1.00** — the chance that start months with no effect at all would produce a spread this wide.


## Month-start against mid-month starts

| Horizon | Month-start: windows / median / P(≥ E) / P(≥ TRI) | Mid-month: windows / median / P(≥ E) / P(≥ TRI) | Median paired difference (mid - start, same month) |
| --- | --- | --- | --- |
| 1 m | 170 / 0.0% / 36% / 54% | 170 / 3.0% / 42% / 54% | +2.2 pp |
| 3 m | 168 / 10.7% / 45% / 57% | 168 / 8.8% / 42% / 54% | +0.7 pp |
| 6 m | 165 / 11.8% / 40% / 58% | 165 / 13.1% / 42% / 54% | -0.7 pp |
| 9 m | 162 / 11.8% / 39% / 64% | 162 / 13.2% / 42% / 63% | -0.1 pp |
| 12 m | 159 / 14.0% / 40% / 62% | 159 / 13.0% / 39% / 62% | +0.0 pp |
| 18 m | 153 / 15.6% / 41% / 60% | 152 / 15.6% / 43% / 62% | -0.2 pp |
| 24 m | 147 / 18.4% / 52% / 68% | 146 / 18.4% / 51% / 65% | +0.0 pp |
| 36 m | 135 / 21.5% / 58% / 76% | 134 / 20.2% / 60% / 74% | +0.0 pp |
| 48 m | 123 / 18.5% / 52% / 77% | 122 / 18.2% / 50% / 79% | -0.3 pp |
| 60 m | 111 / 16.4% / 40% / 74% | 110 / 16.6% / 42% / 70% | +0.1 pp |
| 84 m | 87 / 17.0% / 44% / 80% | 86 / 16.9% / 45% / 80% | +0.1 pp |

## In and out against staying invested

| Pattern | Phase | Total horizon | Windows | In/out beats staying | Median XIRR, in/out | Median XIRR, staying | Median difference | Worst in/out | Worst staying |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 3 m in / 3 m out | in first | 24 m | 147 | 16% | 9.5% | 18.4% | -9.5 pp | -5.7% | -9.0% |
| 3 m in / 3 m out | in first | 36 m | 135 | 4% | 9.7% | 21.5% | -9.8 pp | -5.7% | -1.0% |
| 3 m in / 3 m out | in first | 60 m | 111 | 2% | 8.4% | 16.4% | -9.4 pp | -0.2% | 3.6% |
| 3 m in / 3 m out | out first | 24 m | 147 | 16% | 8.7% | 18.4% | -8.8 pp | -5.6% | -9.0% |
| 3 m in / 3 m out | out first | 36 m | 135 | 7% | 9.7% | 21.5% | -10.9 pp | -5.7% | -1.0% |
| 3 m in / 3 m out | out first | 60 m | 111 | 1% | 8.5% | 16.4% | -9.1 pp | -0.1% | 3.6% |
| 6 m in / 6 m out | in first | 24 m | 147 | 21% | 10.7% | 18.4% | -8.9 pp | -10.1% | -9.0% |
| 6 m in / 6 m out | in first | 36 m | 135 | 10% | 9.3% | 21.5% | -8.1 pp | -2.2% | -1.0% |
| 6 m in / 6 m out | in first | 60 m | 111 | 2% | 8.4% | 16.4% | -8.1 pp | -2.3% | 3.6% |
| 6 m in / 6 m out | out first | 24 m | 147 | 19% | 11.1% | 18.4% | -8.2 pp | -9.9% | -9.0% |
| 6 m in / 6 m out | out first | 36 m | 135 | 7% | 10.0% | 21.5% | -7.9 pp | -2.1% | -1.0% |
| 6 m in / 6 m out | out first | 60 m | 111 | 1% | 9.2% | 16.4% | -7.3 pp | -2.3% | 3.6% |
| 12 m in / 12 m out | in first | 24 m | 147 | 20% | 9.0% | 18.4% | -6.2 pp | -9.6% | -9.0% |
| 12 m in / 12 m out | in first | 36 m | 135 | 19% | 11.2% | 21.5% | -4.3 pp | -2.3% | -1.0% |
| 12 m in / 12 m out | in first | 60 m | 111 | 12% | 10.5% | 16.4% | -5.2 pp | 1.0% | 3.6% |
| 12 m in / 12 m out | out first | 24 m | 147 | 29% | 9.3% | 18.4% | -5.2 pp | -9.2% | -9.0% |
| 12 m in / 12 m out | out first | 36 m | 135 | 13% | 7.6% | 21.5% | -8.7 pp | -5.4% | -1.0% |
| 12 m in / 12 m out | out first | 60 m | 111 | 4% | 8.2% | 16.4% | -8.1 pp | -0.0% | 3.6% |

## Monthly SIP against a lump sum

| Horizon | Windows | D13 lump sum: median / worst / P(loss) | D13 monthly SIP: median / worst / P(loss) | SIP beats lump sum (D13) | TRI lump sum: median / worst | TRI SIP: median / worst |
| --- | --- | --- | --- | --- | --- | --- |
| 12 m | 159 | 17.4% / -20.2% / 18% | 14.6% / -24.0% / 18% | 53% | 10.2% / -26.9% | 11.4% / -44.0% |
| 36 m | 135 | 24.4% / -0.1% / 1% | 22.0% / -6.6% / 5% | 44% | 14.9% / -3.4% | 15.1% / -15.6% |
| 60 m | 111 | 18.6% / 5.3% / 0% | 23.9% / 0.3% / 0% | 56% | 14.8% / 1.1% | 15.6% / -4.5% |

## Stability verdicts

- D13's own full-span return (E): stable
- Cash (repo - 50 bp, after slab tax): NOT stable
- NIFTY 500 TRI, same window: NOT stable
- No loss (≥ 0 %): stable
- 12 % a year: NOT stable
- 15 % a year: stable
