# Cap-tier study, step 0: the size measure (2026-10-05)

Settled **before** any cap-tier strategy was written or any return was computed. Reproduce with
`DATA_ROOT=/home/ubuntu/stock-manager/data uv run python ops/studies/cap_tier_size_measure.py --out <file>`
(read-only; no fetch; L0 payloads re-hashed on read).

## Decision

**True point-in-time market cap is not available across 2012-2026, so the size measure is a proxy,
used unchanged for the whole span:** a name's median daily traded value (close x quantity, L1
`prices_raw`) over the trailing 126 NSE sessions ending on the decision session, counting the
sessions it traded on — ranked among NSE `EQ` equities (`INE` ISIN, valid check digit) that printed
that session. Tiers: rank 1-100 / 101-250 / 251-500; past 500 excluded. Code:
`backtest/cap_tiers.py` (`CAP_TIER_IDENTITY = liquidity_rank_tiers/v1`).

They are named everywhere as **liquidity-rank tiers that proxy AMFI cap tiers**, never market cap.

Why not a real-mcap series:

- From 2013-07 to 2017 **no source** carries a dated share count for any liquid name (table 1, 0 %).
- XBRL `shares_outstanding` covers 49-73 % of the liquid universe from 2018 to 2023, 93 % from 2025.
- `mcap` Issue Size covers 100 %, but only from 2024-02-01; `ffix` ISSUE_CAP covers index members
  only, 2010-01-04..2013-04-30.

Splicing them would change the measure at 2013-04, 2018-05 and 2024-02 — structural breaks a
ranking would learn. Today's market cap or today's index list is never used for a past date.

## Proxy validation, in one paragraph

Where real market cap exists, the proxy orders names well but places tier boundaries loosely.
Against `mcap` (2024-02..2026-08, 31 monthly sessions, ~1,600-2,100 names, the strategy's own
population and boundaries): **mean Spearman 0.916** (0.889-0.928). Of each real tier, the share the
proxy puts in the same tier: **large 68.2 %, mid 48.3 %, small 45.1 %**. Of the proxy's SMALL tier,
45.1 % are real small caps and 99.3 % are within one tier (real mid, small, or past rank 500).
Against `ffix` (2011-02..2013-04, CNX 500 members only, ranked within CNX 500): mean Spearman
0.704; same-tier share large 67.6 %, mid 46.9 %, small 74.9 %. The `ffix` ISSUE_CAP is the full share
count (FF_MKT_CAP / (ISSUE_CAP x IWF x close) = 1.0000 on all 11,415 rows checked), so ISSUE_CAP x
close is full market cap.

**What that means for a reader of the results:** the "smallcap" arm holds names ranked 251-500 by
how much they trade. About half of them are AMFI small caps; most of the rest are real micro caps
that trade like small caps, plus some real mid caps. It is a liquidity-tier strategy first.

## A consequence for the Rs 10 crore floor, measured before any run

The proxy's rank-250 and rank-500 sizes, first decision session of each year (trailing 126-session
median close x quantity):

| year | rank 100 | rank 250 | rank 500 |
|---|---|---|---|
| 2012 | 19.2 cr | 2.6 cr | 0.34 cr |
| 2014 | 17.4 cr | 1.8 cr | 0.24 cr |
| 2016 | 32.8 cr | 5.5 cr | 1.32 cr |
| 2018 | 59.2 cr | 15.8 cr | 2.91 cr |
| 2020 | 63.5 cr | 6.5 cr | 0.98 cr |
| 2022 | 130.9 cr | 31.3 cr | 8.90 cr |
| 2024 | 137.5 cr | 51.3 cr | 15.46 cr |
| 2026 | 162.0 cr | 58.5 cr | 19.21 cr |

The swing universe's floor is a 365-day median traded value of at least Rs 10 crore (or Rs 1 crore).
Until about 2021, almost no SMALL-tier name and only part of the MID tier passes the Rs 10 crore
floor, so at that floor the focused-smallcap arm has few or no candidates for most of the history
and holds cash. That is a property of the floor, and it is reported rather than worked around.

## 1. PIT shares-outstanding coverage of the liquid universe

Liquid universe: NSE EQ equities (INE, valid check digit) printing on the session with a 365-day median traded value >= Rs 1 crore. 2026 uses the first session of July 2026.

| session | liquid names | mcap Issue Size | ffix ISSUE_CAP (any index) | XBRL shares (filed, period <= 400d old) | any source |
|---|---|---|---|---|---|
| 2011-07-01 | 375 | 0 (0%) | 303 (81%) | 0 (0%) | 303 (81%) |
| 2012-07-02 | 342 | 0 (0%) | 297 (87%) | 0 (0%) | 297 (87%) |
| 2013-07-01 | 341 | 0 (0%) | 0 (0%) | 0 (0%) | 0 (0%) |
| 2014-07-01 | 339 | 0 (0%) | 0 (0%) | 0 (0%) | 0 (0%) |
| 2015-07-01 | 552 | 0 (0%) | 0 (0%) | 0 (0%) | 0 (0%) |
| 2016-07-01 | 543 | 0 (0%) | 0 (0%) | 0 (0%) | 0 (0%) |
| 2017-07-03 | 677 | 0 (0%) | 0 (0%) | 0 (0%) | 0 (0%) |
| 2018-07-02 | 780 | 0 (0%) | 0 (0%) | 386 (49%) | 386 (49%) |
| 2019-07-01 | 584 | 0 (0%) | 0 (0%) | 372 (64%) | 372 (64%) |
| 2020-07-01 | 531 | 0 (0%) | 0 (0%) | 346 (65%) | 346 (65%) |
| 2021-07-01 | 820 | 0 (0%) | 0 (0%) | 548 (67%) | 548 (67%) |
| 2022-07-01 | 1064 | 0 (0%) | 0 (0%) | 733 (69%) | 733 (69%) |
| 2023-07-03 | 1042 | 0 (0%) | 0 (0%) | 725 (70%) | 725 (70%) |
| 2024-07-01 | 1352 | 1352 (100%) | 0 (0%) | 984 (73%) | 1352 (100%) |
| 2025-07-01 | 1334 | 1334 (100%) | 0 (0%) | 1246 (93%) | 1334 (100%) |
| 2026-07-01 | 1395 | 1395 (100%) | 0 (0%) | 1304 (93%) | 1395 (100%) |

## 2. The proxy against real market cap

Population: names both measures cover on the session. `mcap` ranks are over all such NSE EQ equities, so the tier boundaries are the strategy's own; `ffix` ranks are within CNX 500 only (both measures re-ranked on that subset).

| session | source | names | Spearman | large same tier | mid same tier | small same tier |
|---|---|---|---|---|---|---|
| 2011-06-22 | ffix | 500 | 0.701 | 56/100 | 67/150 | 190/250 |
| 2011-07-01 | ffix | 500 | 0.733 | 67/100 | 69/150 | 186/250 |
| 2011-08-01 | ffix | 500 | 0.734 | 68/100 | 69/150 | 187/250 |
| 2011-09-02 | ffix | 500 | 0.732 | 69/100 | 72/150 | 189/250 |
| 2011-10-03 | ffix | 500 | 0.729 | 68/100 | 74/150 | 189/250 |
| 2011-11-01 | ffix | 500 | 0.702 | 69/100 | 74/150 | 188/250 |
| 2011-12-01 | ffix | 499 | 0.692 | 68/100 | 75/150 | 188/249 |
| 2012-01-02 | ffix | 500 | 0.680 | 68/100 | 74/150 | 185/250 |
| 2012-02-01 | ffix | 490 | 0.712 | 69/100 | 77/150 | 181/240 |
| 2012-03-01 | ffix | 490 | 0.716 | 71/100 | 76/150 | 180/240 |
| 2012-04-02 | ffix | 495 | 0.719 | 67/100 | 75/150 | 186/245 |
| 2012-05-02 | ffix | 494 | 0.699 | 65/100 | 73/150 | 183/244 |
| 2012-06-01 | ffix | 494 | 0.687 | 67/100 | 68/150 | 178/244 |
| 2012-07-02 | ffix | 497 | 0.702 | 68/100 | 75/150 | 188/247 |
| 2012-08-01 | ffix | 495 | 0.694 | 67/100 | 71/150 | 184/245 |
| 2012-09-03 | ffix | 492 | 0.684 | 65/100 | 68/150 | 180/242 |
| 2012-10-01 | ffix | 494 | 0.682 | 66/100 | 65/150 | 178/244 |
| 2012-11-01 | ffix | 493 | 0.675 | 68/100 | 65/150 | 177/243 |
| 2012-12-03 | ffix | 496 | 0.694 | 68/100 | 67/150 | 185/246 |
| 2013-01-01 | ffix | 493 | 0.704 | 68/100 | 65/150 | 182/243 |
| 2013-02-01 | ffix | 493 | 0.711 | 69/100 | 65/150 | 184/243 |
| 2013-03-01 | ffix | 500 | 0.708 | 72/100 | 67/150 | 190/250 |
| 2013-04-01 | ffix | 500 | 0.697 | 72/100 | 66/150 | 187/250 |
| 2024-02-01 | mcap | 1609 | 0.897 | 69/100 | 76/150 | 109/250 |
| 2024-03-01 | mcap | 1584 | 0.889 | 66/100 | 73/150 | 111/250 |
| 2024-04-01 | mcap | 1643 | 0.896 | 68/100 | 76/150 | 113/250 |
| 2024-05-02 | mcap | 1685 | 0.904 | 67/100 | 77/150 | 114/250 |
| 2024-06-03 | mcap | 1722 | 0.908 | 70/100 | 76/150 | 115/250 |
| 2024-07-01 | mcap | 1707 | 0.906 | 71/100 | 77/150 | 117/250 |
| 2024-08-01 | mcap | 1701 | 0.913 | 70/100 | 78/150 | 119/250 |
| 2024-09-02 | mcap | 1649 | 0.915 | 68/100 | 75/150 | 121/250 |
| 2024-10-01 | mcap | 1628 | 0.910 | 68/100 | 72/150 | 118/250 |
| 2024-11-01 | mcap | 1692 | 0.916 | 67/100 | 69/150 | 118/250 |
| 2024-12-02 | mcap | 1742 | 0.919 | 68/100 | 69/150 | 116/250 |
| 2025-01-01 | mcap | 1758 | 0.912 | 66/100 | 67/150 | 113/250 |
| 2025-02-03 | mcap | 1785 | 0.911 | 69/100 | 70/150 | 108/250 |
| 2025-03-03 | mcap | 1807 | 0.915 | 71/100 | 70/150 | 112/250 |
| 2025-04-01 | mcap | 1871 | 0.922 | 67/100 | 66/150 | 110/250 |
| 2025-05-02 | mcap | 1845 | 0.924 | 71/100 | 65/150 | 115/250 |
| 2025-06-02 | mcap | 1836 | 0.927 | 71/100 | 69/150 | 115/250 |
| 2025-07-01 | mcap | 1787 | 0.925 | 67/100 | 70/150 | 118/250 |
| 2025-08-01 | mcap | 1790 | 0.921 | 69/100 | 73/150 | 122/250 |
| 2025-09-01 | mcap | 1856 | 0.923 | 69/100 | 71/150 | 120/250 |
| 2025-10-01 | mcap | 1983 | 0.926 | 69/100 | 70/150 | 110/250 |
| 2025-11-03 | mcap | 2004 | 0.928 | 69/100 | 72/150 | 111/250 |
| 2025-12-01 | mcap | 2030 | 0.926 | 66/100 | 71/150 | 103/250 |
| 2026-01-01 | mcap | 2079 | 0.926 | 65/100 | 72/150 | 109/250 |
| 2026-02-02 | mcap | 2102 | 0.925 | 66/100 | 76/150 | 109/250 |
| 2026-03-02 | mcap | 2109 | 0.925 | 66/100 | 72/150 | 107/250 |
| 2026-04-01 | mcap | 2127 | 0.924 | 69/100 | 75/150 | 112/250 |
| 2026-05-04 | mcap | 2137 | 0.916 | 69/100 | 78/150 | 115/250 |
| 2026-06-01 | mcap | 2136 | 0.921 | 69/100 | 76/150 | 105/250 |
| 2026-07-01 | mcap | 2078 | 0.918 | 68/100 | 73/150 | 108/250 |
| 2026-08-03 | mcap | 2078 | 0.919 | 67/100 | 70/150 | 104/250 |

**Pooled:**

- `ffix`: 23 sessions, mean Spearman 0.704 (min 0.675, max 0.734); share of each real tier the proxy puts in the same tier: large 67.6%, mid 46.9%, small 74.9%; of each proxy tier's names, the share whose real tier is the same / within one tier (past rank 500 counts as a fourth tier): large 67.6% same / 89.5% within one tier, mid 46.9% same / 100.0% within one tier, small 74.9% same / 98.4% within one tier
- `mcap`: 31 sessions, mean Spearman 0.916 (min 0.889, max 0.928); share of each real tier the proxy puts in the same tier: large 68.2%, mid 48.3%, small 45.1%; of each proxy tier's names, the share whose real tier is the same / within one tier (past rank 500 counts as a fourth tier): large 68.2% same / 92.7% within one tier, mid 48.3% same / 94.5% within one tier, small 45.1% same / 99.3% within one tier
- `ffix` ISSUE_CAP check: FF_MKT_CAP / (ISSUE_CAP x INVESTIBLE_FACTOR x close) median 1.0000, p1 1.0000, p99 1.0000 over 11415 rows
