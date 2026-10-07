# Corporate-action ingestion fixtures (M2.2)

Frozen samples for the NSE and BSE corporate-action parsers
(`dataplatform.ingest.nse.corp_actions`, `dataplatform.ingest.bse.corp_actions`). Tests read these
and never the network (B8 / AGENTIC_CONTEXT §8).

```
nse/2026-08-08/corporateActions.json   # NSE corporates-corporateActions response shape
bse/2026-08-08/defaultdata.json        # BSE DefaultData/w response shape
```

## Provenance and its limitation

The **field schema** of each file is the real one: the C.1 verification sweep (2026-08-08) fetched
both endpoints once and recorded their exact keys and a response checksum in
`dataplatform/ingest/source_register.yaml` (see the `nse_corp_actions` and `bse_corp_actions`
entries' `parse_check`). These fixtures reproduce that schema key-for-key:

- **NSE:** `symbol, series, isin, faceVal, subject, exDate, recDate, bcStartDate, bcEndDate,
  ndStartDate, ndEndDate, caBroadcastDate` — ISIN native.
- **BSE:** `scrip_code, short_name, long_name, Ex_date, exdate, Purpose, RD_Date, BCRD_FROM,
  BCRD_TO, ND_START_DATE, ND_END_DATE, payment_date` — keyed on scrip code, two ex-date spellings.

The **record contents** are hand-built rather than captured verbatim: the C.1 sweep stored
checksums, not bodies, and a full response capture across format eras is part of the gated B1
verify-and-sample fetch (AGENTIC_CONTEXT §2 B1 / §3.3), which is not run offline. The rows are
constructed to be realistic and internally consistent, and both files describe the **same five
corporate actions** for the same five ISINs — a bonus, a dividend, a face-value split, a rights
issue with a premium, and a merger — so the ingest test can prove that two dialects normalize to
one model. Each file also carries a deliberate leftover: an NSE `Annual General Meeting` subject
(unclassifiable → manual-entry queue) and a BSE row for a scrip the identity master does not know
(→ unresolved), so the "surfaced, never dropped" behaviour is exercised.

When the B1 sample fetch runs, replace these with captured bodies for each format era and re-freeze.

## `bse/2026-09-07/defaultdata_540386.json` — real bytes (M13.4)

A byte-for-byte copy of the L0 payload `bse_corp_actions/2016/09/defaultdata_540386.json` (sha256
`1c835688c87b3f40d8d12641bda01c76a840ffa464c020730eb8c4a29d1b0d8f`, 582 bytes, fetched
2026-09-07): ONTIC's per-scrip response. It holds the `Consolidation of Shares` record with ex-date
2017-04-13, which states no ratio and so parses to `SPLIT` with unquantified terms, and a 2022
Rs 10 -> Re 1 split. It is paired with the two legacy bhavcopies in `../../bse_bhavcopy/legacy/` to
pin the ex-marker witness's first-session match.
