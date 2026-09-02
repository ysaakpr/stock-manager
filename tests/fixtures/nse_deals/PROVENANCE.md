# NSE bulk & block deal fixtures (`bulk.csv` / `block.csv`)

Real files, fetched from the live NSE archive host under the `nse_bulk_deals` and
`nse_block_deals` register policies (`source_register.yaml`) — browser UA,
`Referer: https://www.nseindia.com/`, no session cookie — during M3.5 on **2026-09-02**.
Copied here byte-for-byte.

URL patterns (`source_register.yaml`):

- `nse_bulk_deals`  → `https://nsearchives.nseindia.com/content/equities/bulk.csv`
- `nse_block_deals` → `https://nsearchives.nseindia.com/content/equities/block.csv`

Both endpoints serve a **rolling current-session file**: the URL carries no date parameter, so
the file is whatever the exchange last published, and history accrues forward from the first daily
capture (the same shape as `nse_fii_dii_flows`, M3.4). A missed day is a permanent hole; nothing
can re-fetch a past session, which is why the parser keys on the file's own `Date` column and the
L0 filename carries the session date (`l0_filename`). Because the file rolls, these fixtures are
the **2026-09-01 session as captured on 2026-09-02**, not the 2026-08-08 payloads the C.1
verification sweep sampled in `source_register.yaml` — a rolling source cannot reproduce a past
sample, so the sweep's `sample_sha256` is the verification record and these are the frozen fixture.

| Deal type | File | Bytes | sha256 | Data rows | Session |
|---|---|---|---|---|---|
| Bulk  | `bulk_01092026.csv`  | 22,722 | `cabbd6187946ae9622c36ad1766527c98b4bafa5b75e554a2be70f2846ef2c2b` | 248 | 2026-09-01 |
| Block | `block_01092026.csv` | 334    | `0d5478ab448d5a15bd8bf2cb0b662388930dc6122e4678706e160ec0f43919ca` | 3   | 2026-09-01 |

## What these two files prove

**Two column shapes, one row model.** Bulk carries 8 columns; block carries 7 (no `Remarks`):

```
Bulk:  Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price,Remarks
Block: Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price
```

Both parse into the same `DealRow`; `deal_type` (`BULK`/`BLOCK`) is the only thing that tells them
apart afterward, and a block row's `remarks` is `None` because the column does not exist rather
than because it was `-`.

**There is no ISIN in either file.** A deal is joined to a holding only after
`dataplatform.ingest.nse.deals.resolve` maps `(symbol, trade_date)` to an ISIN through the D2
identity master (invariant #2) — the file states a symbol and a security *name*, never an ISIN.

**Client names are messy, so the raw string is the source of truth.** The 2026-09-01 bulk file
carries real quirks that T0's "same client accumulating" signal would trip over if the raw string
were the only form, and that entity-matching would trip over if the raw string were *lost*:

- `R G FAMILY  TRUST` and `KABRA  PRIYA` — a double space inside the name.
- `ISTAA SECURITIES PRIVATE LIMITED  -` — a stray trailing `  -` the exchange left in the field.

The parser keeps `client_name` exactly as published and derives `client_name_normalized`
(upper-cased, whitespace-collapsed) alongside it. Normalization is deliberately conservative — it
does **not** strip corporate suffixes (`LIMITED`, `LLP`, `PRIVATE LIMITED`) or the stray `-`,
because that is entity resolution, a T0 concern, not an ingestion one. Keeping both forms means a
wording change on the feed is visible in L1 rather than silently absorbed.

**A block file can be tiny and still whole.** `block_01092026.csv` is 334 B / 3 deals — a quiet
day, not a truncated download. The register notes the same: a small `block.csv` must not be read
as a failure. (A day with *no* deals serves a header-only file, which the parser reads as zero
rows, never as an error — the distinction a truncation check would get wrong.)
