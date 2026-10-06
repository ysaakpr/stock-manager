# NSE shareholding-pattern master fixtures (M3.6, M13.3)

Matches `source_register.yaml` row `nse_shareholding_pattern` (§4.1 row 11) —
`https://www.nseindia.com/api/corporate-share-holdings-master?index=equities`, parsed by
`dataplatform.ingest.shareholding`. One directory per **format era**; the parser must keep passing
every one (`FormatEra`).

## `master_2026_10/corporate-share-holdings-master_20261006.json` — live, frozen from L0

The real payload, **byte for byte**: copied from
`data/L0/nse_shareholding_pattern/2026/10/corporate-share-holdings-master_20261006.json`, captured
by the `shareholding_poll` daily capture at `2026-10-06T13:44:23+05:30` (L0 sidecar `fetched_at`),
`application/json; charset=utf-8`, 30,615 bytes,
sha256 `6bed2ff4c3a38b161a1dd78ff0d75e6f3423cb8087fe4e2b3929fc108af9faf5` — the sha256 the L0
sidecar recorded at capture, asserted by `tests/unit/test_shareholding_live_era.py`. Frozen with no
network request (M13.3); L0 was read, never written.

What it shows about the live era, all of which the parser now reads:

* 32 records, every one for quarter end `30-SEP-2026`, filed 01-Oct to 06-Oct 2026.
* Public holding is `public_val`, not `public_prcnt`. There is **no pledge** and **no FII/DII
  split** — those live in the per-filing XBRL each record links to (`xbrl`). BC3 is therefore
  `not_applicable` on every row (decision D17).
* `employeeTrusts` is a third holding bucket (one company: 19.31 + 79.99 + 0.7 = 100).
* `revisedData` is `N` or `Revised` (one record, A-One Steels, revised 05-Oct).
* Two records (`CHENNPETRO`, `HLVLTD`) have `isin: null`. They are skipped and counted, never
  resolved from their symbol (invariant #2).
* `broadcastDate` is set and `cgTimeStamp` is null throughout; months are upper-case (`OCT`).

Free-text remark fields are exchange-published filing notes, kept as served.

## `json_v1/corporate-share-holdings-master_20260807.json` — synthetic, the M3.6 build era

A **format-faithful synthetic fixture** written at M3.6 to the key set then believed to be served
(`fixture.frozen: false` in the register until M13.3). It is not a live sample: it carries
`public_prcnt`, `pledgeShares_prcnt`, `fii_prcnt` and `dii_prcnt`, which the 2026-10-06 capture
showed the live master does not. It stays as the pledge-present era — the fixture that proves a
stated pledge over 50% still breaches BC3 — and so that a payload in that shape keeps parsing.
