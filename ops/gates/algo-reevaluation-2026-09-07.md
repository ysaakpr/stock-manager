# Re-evaluation — every backtest policy on the rebuilt lake (2026-09-07)

*Every algo report in `ops/gates/` was struck against the lake as it stood on the day its task ran.
The lake has changed under all of them since. This note re-runs each one on **one** lake state and
**one** commit and reads the result against the figure it replaces — in particular against the
"best ≈23 %, stable ≈22 %" pair that came out of M10.6 on 2026-09-06.*

Runs: on the server at `ad0746f`, driver log `~/campaign/backtest-rerun-2026-09-07.log`, five
reports in one sequential pass so no two arms see different data. The reports themselves are the
primary artefacts (`M9-universe-report.md`, `M9-benchmark-report.md`, `M9-adjusted-backtest-report.md`,
`M9-momentum-v2-report.md`, `M10-sector-rotation-report.md`, `M10-fundamentals-signal-report.md`);
this note is the cross-report reading.

## The short answer

**The ≈23 % / ≈22 % pair survives the rebuild almost exactly, and it was never a decade number.**

- Those two figures are M10.6's **2019-07-01 → 2025-08-29** window: VALUE at 23.13 % and momentum
  v2 all-on at 21.72 %. On the rebuilt lake, same window, same parameters: **VALUE 22.87 %**
  (−0.26 pp) and **momentum v2 all-on 21.72 %** (unchanged to the digit).
- Extend the window by the year the old PIT store could not cover — to **2026-08-31** — and every
  arm gives back a chunk: **VALUE 21.53 %**, **momentum v2 18.52 %**, and the market's own XIRR
  falls from 14.21 % to 11.18 %. The strategies did not change; the six-year window was the
  flattering one.
- Over the **full decade** (2016-09 → 2026-08) the same momentum family earns 12–15 %, not 22 %:
  naive 12.21 %, the best stated configuration 14.95 %, benchmark 8.56 %. A 22 % figure and a 12 %
  figure from "the same policy" differ only in which years they are asked about.

## Fundamentals arms — identical window, old lake vs rebuilt lake

2019-07-01 → 2025-08-29, 1,526 sessions, 74 monthly rebalances, market (L1 proxy) 14.21 % in both.

| Strategy | XIRR before | XIRR now | Δ | Max DD | Trades before → now |
| --- | --- | --- | --- | --- | --- |
| Fundamentals: VALUE | 23.13% | **22.87%** | −0.26 pp | 45.27% | 1,026 → 1,044 |
| Fundamentals: GROWTH | 17.54% | **17.60%** | +0.06 pp | 29.51% | 960 → 973 |
| Fundamentals: QUALITY_VALUE | 13.85% | **13.40%** | −0.45 pp | 45.63% | 779 → 796 |
| Fundamentals: MOMENTUM_VALUE | 14.15% | **13.85%** | −0.30 pp | 38.28% | 1,338 → 1,324 |
| Momentum: naive (all off) | 21.11% | **21.11%** | 0.00 | 28.90% | 1,939 → 1,939 |
| Momentum: v2 all-on | 21.72% | **21.72%** | 0.00 | 21.95% | 1,260 → 1,260 |

Two things to read here.

**The momentum arms reproduce exactly.** They rank on raw NSE EQ closes, and nothing in the
rebuild touched those bars: the BSE decade that landed on 2026-09-06 never enters (every L1 read in
the backtest is NSE equity), and the corporate-action and L2 work only moves the *adjusted* series,
which these arms do not read. Byte-identical digests, not merely equal percentages.

**The fundamentals arms drift down by ~0.3 pp, and that is the store getting more honest.** The PIT
fundamentals store behind them is materially better than the one M10.6 ran on: newest filing
2026-08-24 → **2026-09-05**, ISINs with a filing under 200 days old on the terminal session
**749 → 2,069**, rankable names at the last rebalance 869 → 894, and the M10.5 scale guard now
catches 8,270 mis-scaled (ISIN, rebalance) computations rather than 8,258. More filings, resolved
against the right ISIN as-of the filing date and at the right scale, means a slightly different —
and slightly less flattering — ranking each month. A third of a point of movement across a rebuilt
fundamentals store is the robustness answer, not a defect.

## Fundamentals arms — the window the current store allows

2019-07-01 → **2026-08-31**, 1,773 sessions, 86 rebalances. This is the run the old store could not
support: M10.6 stopped in Aug 2025 because a name is unrankable once its newest filing is over 200
days old, and the store thinned out before that. It is now current to 2026-09-05.

| Strategy | XIRR | Max DD | Risk-on cum. | Risk-off cum. | Trades | Cost |
| --- | --- | --- | --- | --- | --- | --- |
| Fundamentals: VALUE | **21.53%** | 45.27% | 279.35% | +6.73% | 1,243 | ₹79,197 |
| Momentum: v2 all-on | **18.52%** | **21.95%** | 202.79% | +11.73% | 1,453 | ₹66,891 |
| Momentum: naive (all off) | 17.85% | 28.90% | 241.76% | −4.95% | 2,359 | ₹99,697 |
| Fundamentals: GROWTH | 17.55% | 30.45% | 115.27% | **+48.17%** | 1,152 | ₹79,317 |
| Fundamentals: QUALITY_VALUE | 12.82% | 45.63% | 157.31% | −7.70% | 954 | ₹48,903 |
| Fundamentals: MOMENTUM_VALUE | 12.23% | 38.28% | 145.91% | −6.94% | 1,587 | ₹88,390 |
| Market (L1 proxy) | 11.18% | — | 76.96% | +20.87% | — | — |

The ordering is the same as before — VALUE first, momentum v2 second on return and first on
drawdown, QUALITY_VALUE and MOMENTUM_VALUE failing to beat their own components — so the extra year
changed the level, not the ranking. Two shifts worth naming:

- **GROWTH is now the only arm that earns real money with the trend against it** (+48.17 % risk-off,
  against the market's +20.87 %), where in the 2019-25 window it made +20.30 %. It is the
  diversifier in this set; VALUE's risk-off contribution went from −14.62 % to +6.73 % and momentum
  v2's from +15.82 % to +11.73 %.
- **VALUE keeps its 45 % drawdown** in every window. It is the highest-return arm and the worst
  drawdown in the table; momentum v2 all-on gives up ~3 pp of XIRR for half the drawdown and a
  third fewer trades. On a risk-adjusted read that pair, not VALUE alone, is the honest "best".

## The ten-year momentum picture

2016-09-02 → 2026-08-31, 2,470 sessions, 120 rebalances, M9.3 investable universe (mean 888.9
names), raw signal, benchmark 8.56 %. Unchanged from the previous edition of the v2 report to two
decimal places — the session count and the digests moved, the returns did not.

| Configuration | XIRR | Max DD | Fills | Cost |
| --- | --- | --- | --- | --- |
| All on + redeploy + vol target 15 % | **14.95%** | 22.73% | 2,766 | ₹136,232 |
| + Redeploy proceeds next session | 14.23% | 56.13% | 3,281 | ₹149,848 |
| All on + redeploy | 13.97% | 25.57% | 2,107 | ₹107,199 |
| + 12-1 momentum | 13.32% | 42.54% | 2,725 | ₹120,574 |
| **Naive (all off)** | **12.21%** | 50.21% | 2,934 | ₹105,659 |
| All on (four M9.5 toggles) | 12.01% | **22.51%** | 1,776 | ₹77,175 |
| + Turnover banding | 11.11% | 57.26% | 2,819 | ₹81,611 |
| + Regime filter | 10.49% | 25.51% | 1,926 | ₹79,462 |
| + Vol-scaled weights | 9.51% | 51.71% | 2,860 | ₹92,650 |
| + Vol target 15 % (alone) | 5.94% | 35.38% | 2,960 | ₹57,910 |

Beside it, two single-run reports on the same window:

- **Universe (M9.3):** the investable/liquid screen still earns its keep — full universe (1,483
  names/rebalance) 11.67 %, investable+liquid (894) **12.05 %**, +0.38 pp on 131 fewer fills.
- **Benchmark / adjusted signal (M9.4 / M9.2):** naive momentum on the **L2 adjusted** signal,
  full universe, **10.92 %** against the raw run's 11.67 % — the adjusted signal is 0.75 pp
  *worse* over the decade, on 104 more fills and ₹3,359 less cost.

That last line is the one genuinely new fact the rebuild produced, and it deserves care. Before the
corporate-action backfill, adjusted and raw were byte-identical (an action-free store makes every
factor chain the identity), so no report had ever measured the gap. It is now real: 47,887
reconciled actions, 1,544 splits and 1,676 bonuses, factors on 1,823 ISINs. **The adjusted signal
being worse is not evidence that adjusting is wrong** — the raw signal reads a 2:1 split as a fake
−50 % twelve-month return and drops the name, and over 2016-2026 that accidental "sell what just
split" filter happened to pay. It is an artefact with no reason to persist; the adjusted number is
the one to plan against, and the raw one is a coin-flip that landed well.

## Sector rotation

2016-09-02 → 2026-08-31: sector rotation **8.72 %**, plain momentum on the identical universe
**9.36 %**, market 8.56 %. Identical to the previous edition in every figure. The sector gate still
costs 0.64 pp on this universe, and the universe is still 38.5 names a rebalance under a static
current-day sector map applied backward (survivorship-biased, stated in the report). Nothing in the
rebuild could have moved it and nothing did; this arm stays blocked on M10.2's forward membership
history rather than on data quality.

## What actually changed in the lake

| | M10.6's run (2026-09-06 09:25) | Now |
| --- | --- | --- |
| `corporate_actions` | before the two-feed compounding fix (a shared split was squared) | 47,887 rows / 3,624 ISINs |
| `adjustment_factors` | before the lineage rebuild — a few hundred ISINs | 2,784 rows / **1,823 ISINs** |
| L2 `prices_adjusted` partitions | invalidated ISINs only (793 of 2,716 EQ names had none) | **3,349** |
| `pit_fundamentals` | → 2026-08-24 | 2,015 date partitions, 2018-05-21 → **2026-09-05** |
| L1 `prices_raw` | NSE only in practice | 2,475 dates, NSE EQ 4.27 M rows + the BSE decade |

The BSE bars are in the lake but out of every number above: the backtest reads NSE equity only.
That is now explicit in the reader rather than a side-effect of `series = 'EQ'` (BSE labels its cash
segment by group code, so no BSE row has ever matched) — the venue predicate and its test are on
`claude/laughing-shirley-90d17c`, unmerged, and are behavior-neutral on this data (0 non-NSE rows
with `series = 'EQ'`, checked).

## What has not been re-evaluated yet

**The adjusted-signal sweep.** `--v2-report`, `--sector-rotation-report` and
`--fundamentals-report` hard-coded `adjusted=False` — correct when written (raw *was* the M9.2
signal over an action-free store), wrong now. So nine of the ten momentum-v2 rows, both
sector-rotation arms and MOMENTUM_VALUE's second signal above are ranked on closes that read a
split as a price move. `0e53f4a` makes the source a parameter wired to the existing `--raw` flag,
with every arm of a sweep on one source and the source named in the report. It needs a push before
the server can run it:

```bash
ops/remote.sh run uv run python -m backtest.run --policy fundamentals_value --fundamentals-report --from 2019-07-01 --to 2026-08-31
ops/remote.sh run uv run python -m backtest.run --policy naive_momentum --v2-report --from 2016-09-01 --to 2026-08-31
ops/remote.sh run uv run python -m backtest.run --policy sector_rotation --sector-rotation-report --from 2016-09-01 --to 2026-08-31
```

Also still open, and each of them bounds how much any number above can be trusted:

- **The benchmark is a price-return L1 proxy**, not a total-return index — no licensed NIFTY-TRI
  (session-gated, FAILED at C.1) and not even M3.9's computed TRI (its close-all snapshot is a gated
  bulk fetch). Every "excess vs market" figure is against an estimate that understates the market by
  roughly its dividend yield. Do not read any of them as alpha.
- **No historical index membership.** `index_constituents` holds no snapshots, so the M9.3
  investable screen is the liquidity floor alone and the sector map is today's list applied
  backward. Both flatter results in the same direction.
- **The regime overlay reads the same proxy**, so the risk-on/risk-off split inherits the proxy's
  error.
