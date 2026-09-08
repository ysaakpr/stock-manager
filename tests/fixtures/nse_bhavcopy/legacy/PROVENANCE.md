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
| 2011-06-22 | `cm22JUN2011bhav.csv.zip` | 53,301 | `1eba7f9259b5638bf09c74bf96a0f66c8ac1c2c41c9e5fe40caf7c1c1da24381` | 1,502 | **the first session ever published with an ISIN column** (W1, fetched 2026-09-08) |
| 2016-01-01 | `cm01JAN2016bhav.csv.zip` | 58,943 | `8623d28a92924fd6d3432b3e21d307dd501a6f3891006e37d720fda2e436a5d7` | 1,607 | early |
| 2020-03-23 | `cm23MAR2020bhav.csv.zip` | 66,967 | `2806459732d60d63adf7d3340307aa257d3677593dc5f27d118f5af89e1a7d32` | 1,965 | mid (the −13% circuit-breaker session) |
| 2024-07-05 | `cm05JUL2024bhav.csv.zip` | 109,203 | `e08c8c0650e6807f8b1abd0658bc8d87cbe2d78c2fc77e05c82878f30be46665` | 2,775 | last legacy session before the 08-Jul-2024 UDiFF cutover |
| 2020-07-13 | `cm13JUL2020bhav.csv.zip` | 71,297 | `fbb63a75cd0be53640989b6f904b0517d2b0ba7c724f24c8a195ac9bc8dd9d88` | 2,001 | **two-digit year, mixed-case month, nested zip member** |
| 2021-02-16 | `cm16FEB2021bhav.csv.zip` | 74,595 | `eb59a06b86f4db851a8c02e1fdf1172b7f324df5bdfa976e0804e69d1e7e2a82` | 2,026 | **one row with `ISIN` = `DUMMY`** |

All six carry the era's 14-field header
`SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,`
(the trailing comma is part of the format).

## The pre-ISIN sub-era now has its own directory (superseded 2026-09-08, W1)

This section used to say the eleven-column sub-era was "deliberately not represented here" because
"no planned backfill reaches that sub-era". W1 reaches it. The frozen payloads live in
`../pre_isin/`, with their own `PROVENANCE.md`, and `cm22JUN2011bhav.csv.zip` was added *here* as
their counterpart: 2011-06-21 and 2011-06-22 are consecutive sessions in two different formats, and
freezing only one side of a cutover pins nothing.

What has not changed is the refusal. `bhavcopy_legacy.parse` still rejects the eleven-column header
rather than filling `ISIN` with `None` — a price row with no ISIN cannot be joined (invariant #2) —
and `bhavcopy_legacy.parse_pre_isin` reads that era into `UnidentifiedRow` only, for quarantine.
The pre-ISIN era is *retained*, not resolved.

## The last two: the sessions the parser could not read until 2026-09-06

Both were fetched in the M1.13 backfill, both landed in L0 intact, and both then failed to parse —
so `prices_raw` had no partition for either date and, with `/status/gaps` answering 500 (audit
finding N1), nothing said so for months. They are frozen here because they are the archive's only
examples of what they show, measured over all 1,940 legacy payloads in the lake:

* **`cm13JUL2020bhav.csv.zip`** is the only file whose `TIMESTAMP` is `13-Jul-20` — a two-digit
  year *and* a mixed-case month, in all 2,001 of its rows. It is also the only archive whose CSV is
  nested inside a directory of the same name (`cm13JUL2020bhav.csv/cm13JUL2020bhav.csv`). One
  session, three format quirks, none of them anywhere else.
* **`cm16FEB2021bhav.csv.zip`** holds the only non-ISIN value ever published in the `ISIN` column:
  `ABFRLPP1` (Aditya Birla Fashion's partly-paid rights entitlement, series `E1`) carries the
  literal `DUMMY`. That is the exchange stating the instrument has no ISIN, not a corrupt field, so
  the row is refused and quarantined and the session's other 2,025 prices are kept.

Neither is a hypothetical. Every rule they pin — `_TWO_DIGIT_PIVOT`, the case-insensitive month
lookup, `PLACEHOLDER_ISINS` — exists because of one of these two files, and deleting either fixture
would let the next "simplification" silently lose a session again.
