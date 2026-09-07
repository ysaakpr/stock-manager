# Delivery columns rebuilt on the server from L0 (2026-09-07)

*`uv run python -m dataplatform.ingest.price_rebuild --from 2016-09-01 --to 2026-09-04`, run on the
server at `3ad5b55`, log `~/campaign/delivery-rebuild-2026-09-07.log`. Offline: no fetch, no
`sync_state` write. 2,465 sessions in ~5 minutes.*

## Why it was needed

L1 joins delivery at *write* time, so a partition written from the bhavcopy alone has
`deliv_qty`/`deliv_pct` NULL on every row and no later patch can fill them. The server's NSE L1
arrived as a copy of a laptop dump taken before the delivery backfill, and the delivery payloads
were rsync'd afterwards — so 1,712 `sec_bhavdata_full` and 759 MTO payloads sat in its L0 while
every one of its 4,274,581 NSE EQ rows carried no delivery figure. Any short-horizon signal that
reads delivery share was therefore unmeasurable on the machine the dev model says full-lake
backtests run on.

## What the run did

```
price_rebuild: 2461 rebuilt, 4 without an L0 payload, 0 failed of 2465 sessions
  delivery rows 5445944: 3651203 joined (67.0%), 1358650 unresolved, 436091 orphaned
```

`deliv_pct` on NSE EQ rows: **0 → 3,351,327 of 4,274,581 (78.4%)**. The four sessions without a
payload are the known never-attempted dates (NSE 2016-09-01 and 2026-09-02/03/04); they need the
backfill, which this deliberately does not perform.

Integrity after the rewrite, checked on the whole lake: 14,298,626 rows (BSE 8,612,535 + NSE
5,686,091) — unchanged, so the shared date partitions kept the other exchange's rows; 0 bad OHLC;
0 `deliv_pct` outside [0, 100]. NSE row count is identical to the laptop's 5,686,091, which is the
check that the rebuild touched the delivery columns and nothing else.

## Coverage is bounded by the identity master, and it is skewed to the recent end

A delivery row carries a symbol and a series, never an ISIN, so it can only be placed through D2 as
of its own trade date (invariant #2). The master here is a single 2026-08-08 `EQUITY_L` snapshot,
so today's symbols resolve and renamed or delisted ones do not:

| Year | NSE EQ rows | with `deliv_pct` | |
| --- | --- | --- | --- |
| 2016 | 123,718 | 79,950 | 64.6% |
| 2017 | 368,537 | 251,879 | 68.3% |
| 2018 | 367,880 | 267,179 | 72.6% |
| 2019 | 368,578 | 278,365 | 75.5% |
| 2020 | 378,311 | 288,823 | 76.3% |
| 2021 | 389,770 | 301,516 | 77.4% |
| 2022 | 438,894 | 347,313 | 79.1% |
| 2023 | 446,991 | 360,687 | 80.7% |
| 2024 | 461,929 | 381,831 | 82.7% |
| 2025 | 529,758 | 450,433 | 85.0% |
| 2026 | 400,215 | 343,351 | 85.8% |

**Read this before building a delivery signal.** The missing third of 2016-2019 is not random: it is
the names whose symbols the current master cannot reach, which skews to companies that were later
renamed, merged or delisted. A signal computed only where delivery exists is therefore computed on a
survivor-tilted subset in the early history, and its measured edge there is optimistic by an unknown
amount. The 1,358,650 unresolved rows are in `prices_raw_quarantine` as `symbol_unresolved`, counted
and available, not dropped.

The one lever that moves this: `uv run python -m dataplatform.ingest.identity_refresh` — 2 requests,
never run on either machine — then re-run the rebuild, which is idempotent and costs 5 minutes.
Nothing else about the pipeline needs to change.

## Laptop vs server, after

The two lakes now agree to 0.36%: laptop 3,363,340 rows with `deliv_pct`, server 3,351,327. Both
derive from the same L0 through the same source set, so the 12,013-row difference is the identity
master, whose laptop copy carries a few more symbol windows than the 2026-09-04 dump the server
holds. Per-year coverage differs by 0.0-0.8 pp and 2021 and 2026 match exactly. The same
`identity_refresh` closes this along with the rest.

L2 is unaffected: the price columns are byte-for-byte what they were, and L2 reads prices and factor
chains, never delivery.
