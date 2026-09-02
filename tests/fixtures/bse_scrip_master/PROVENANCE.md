# BSE scrip master fixture (M3.1)

Matches `source_register.yaml` row `bse_scrip_master` (§4.1 row 6, "Symbol / ISIN master") —
`ListofScripData/w`, the `SC_CODE → ISIN` spine without which no BSE row can be joined
(invariant #2).

## What this file is

A **format-faithful fixture** built to the exact JSON shape the endpoint was verified to serve. It is
not the live sample: the real verification fetch on **2026-08-08** (4,949 active equity scrips) is
recorded in `source_register.yaml` (`sample_bytes`, `sample_sha256`, `parse_check`), which is the
real evidence for that row. This fixture reproduces the verified key set so the offline suite (B8)
exercises the parser, the derivation and the identity merge without the network.

## `2026-08-08/ListofScripData.json`

A JSON array with the register's documented keys: `SCRIP_CD`, `Scrip_Name`, `scrip_id`, `Status`,
`GROUP`, `FACE_VALUE`, `ISIN_NUMBER`, `INDUSTRY`, `Segment`, `NSURL`, `Issuer_Name`, `Mktcap`.

| SCRIP_CD | symbol | ISIN | Status | Note |
|---|---|---|---|---|
| 500325 | RELIANCE | INE002A01018 | Active | dual-listed with NSE |
| 532540 | TCS | INE467B01029 | Active | dual-listed with NSE |
| 500209 | INFY | INE009A01021 | Active | dual-listed with NSE |
| 543066 | BSEONLY | INE0J1Y01017 | Active | BSE-only ISIN |
| 590999 | NOISIN | *(blank)* | Suspended | **no ISIN** — skipped and counted, never guessed |

The RELIANCE/TCS/INFY ISINs match the NSE identity fixtures, so the merge test proves a dual-listed
ISIN gains a BSE `exchange_listing` row without disturbing its NSE listing (acceptance 3). The blank
ISIN row exercises `skipped_no_isin` (the `Suspended`/`Delisted` pulls include the occasional blank,
per the register `pit_notes`).
