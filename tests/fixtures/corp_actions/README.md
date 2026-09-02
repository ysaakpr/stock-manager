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
