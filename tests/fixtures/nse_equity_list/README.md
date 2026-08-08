# nse_equity_list — frozen samples

The two surfaces of EXECUTION_PLAN.md §4.1 row 6 ("Symbol / ISIN master") that the NSE half of
the identity master (D2, task M1.7) is built from. Both are checked in per AGENTIC_CONTEXT.md B8
so `dataplatform/identity/ingest.py` is tested offline and deterministically.

One directory per snapshot date rather than per format era: neither file has ever changed shape,
but `EQUITY_L.csv` is a *snapshot of today's listings* with no history in it at all, so the only
way the identity master ever learns that a security was delisted is by accumulating snapshots.
The directory name is the date the snapshot describes.

## 2026-08-08

Fetched by the M1.7 builder agent under the §4.1 crawl policy — browser UA,
`Referer: https://www.nseindia.com/`, one request per URL, >2.5 s apart, no session cookie, no
retry, nothing evaded.

| File | URL | HTTP | Bytes | Content-Type | SHA-256 |
|---|---|---|---|---|---|
| `EQUITY_L.csv` | `https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv` | 200 | 169,183 | `text/csv` | `153db8e940a615513151a7e2aed74eb9551f9529755861959a8c1f7f80ce914b` |
| `symbolchange.csv` | `https://nsearchives.nseindia.com/content/equities/symbolchange.csv` | 200 | 68,434 | `text/csv` | `eee469d1ec499c66aa21a84c1b874150bfa5e98532fe65bf49ac0c8a7a08004e` |

`EQUITY_L.csv`'s checksum is byte-identical to the one C.1's sweep recorded for the same URL on
2026-08-08 (`source_register.yaml`, `nse_equity_list.sample_sha256`) — the file is weekly and did
not move between the two fetches.

`symbolchange.csv` has no Source Register row yet: §4.1 row 6 names "name-change history files"
and C.1's sweep only verified `EQUITY_L.csv`. Its evidence is above so the next sweep can add the
row without re-deriving it (`ops/BACKLOG.md`).

### Shapes

`EQUITY_L.csv` — 2,397 data rows, header
`SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE`
(the leading spaces are the source's). Symbols and ISINs are both unique within a snapshot; series
is EQ (2,075), BE (294) or BZ (28).

`symbolchange.csv` — 1,054 rows, **no header**, four fields: company name, old symbol, new symbol,
effective date `DD-MON-YYYY`. Covers 1999-09-15 to 2026-08-06. Includes the two renames the
identity tests assert on: `CADILAHC → ZYDUSLIFE` (07-MAR-2022) and `LTI → LTIM` (05-DEC-2022).
