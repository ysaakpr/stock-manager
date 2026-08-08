# NSE legacy bhavcopy fixtures (pre-Jul-2024 era)

Real files, fetched from the live archive through the M1.2 crawl engine
(`dataplatform.ingest.fetcher.Fetcher`) under the `nse_bhavcopy_legacy` register policy — browser
UA, `Referer: https://www.nseindia.com/`, ≥2.5 s host spacing — during M1.4 on **2026-08-08**.
Each landed in L0 first and was copied here byte-for-byte, so the checksums below are the L0
sidecar checksums.

URL pattern (`source_register.yaml` → `nse_bhavcopy_legacy`):
`https://nsearchives.nseindia.com/content/historical/EQUITIES/{YYYY}/{MON}/cm{DD}{MON}{YYYY}bhav.csv.zip`

| Session | File | Bytes | sha256 | Data rows | Era position |
|---|---|---|---|---|---|
| 2016-01-01 | `cm01JAN2016bhav.csv.zip` | 58,943 | `8623d28a92924fd6d3432b3e21d307dd501a6f3891006e37d720fda2e436a5d7` | 1,607 | early |
| 2020-03-23 | `cm23MAR2020bhav.csv.zip` | 66,967 | `2806459732d60d63adf7d3340307aa257d3677593dc5f27d118f5af89e1a7d32` | 1,965 | mid (the −13% circuit-breaker session) |
| 2024-07-05 | `cm05JUL2024bhav.csv.zip` | 109,203 | `e08c8c0650e6807f8b1abd0658bc8d87cbe2d78c2fc77e05c82878f30be46665` | 2,775 | last legacy session before the 08-Jul-2024 UDiFF cutover |

All three carry the era's 14-field header
`SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,`
(the trailing comma is part of the format).

## The pre-ISIN sub-era is deliberately not represented here

The archive reaches back past this format. `cm04JAN2010bhav.csv.zip` — fetched in the same sweep,
kept in L0, not checked in — has only **eleven** columns and neither `TOTALTRADES` nor `ISIN`:

```
SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,
```

`bhavcopy_legacy.parse` refuses that header on purpose rather than filling `ISIN` with `None`: a
price row with no ISIN cannot be joined (invariant #2), and the platform's own trading calendar
covers 2016-01-01 onwards, so no planned backfill reaches that sub-era. See `ops/BACKLOG.md` if it
ever needs to.
