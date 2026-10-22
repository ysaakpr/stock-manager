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

## `master_2026_10/corporate-share-holdings-master_20261021_subset.json` — live, a record-verbatim cut

A **subset** of the real payload, records unedited: cut from
`data/L0/nse_shareholding_pattern/2026/10/corporate-share-holdings-master_20261021.json`, captured
by `shareholding_poll` at `2026-10-21T18:05:09+05:30` (L0 sidecar `fetched_at`), 2,438,321 bytes,
2,272 records, sha256 `fc764ed42e42796d76cd5b47872fba1610f32bf878d45f5cedcce9614ef6743c` (the
sidecar's, re-verified when cut). No network request; L0 was read, never written.

How it was cut: `json.loads` the payload, keep records **0-38 and 982 and 1084** in payload order
(41 records), and write them with `json.dumps(..., separators=(",", ":"), ensure_ascii=False)`.
That serialisation reproduces the whole L0 payload byte for byte, so every record here is the exact
byte sequence NSE served; only the array around them is shorter. Result: 48,762 bytes, sha256
`a88e7c0f4ede7ebd61f7e449f025665e51f2cc7ad84fffeb057a9084acfd5a56`, asserted by
`tests/unit/test_shareholding_bad_isin.py`.

What it carries:

* Records 39 and 40 (L0 records 982 and 1084) are the two filings NSE published with a malformed
  ISIN, in every poll from 2026-10-16 on: `ASAHIINDIA` with `isin: "INE 439A0401"` (an embedded
  space) and `PGHL` with `isin: "INE07180108"` (eleven characters). Both for `30-SEP-2026`,
  broadcast 16-Oct. Record 982's was the `ParseError` that failed `shareholding_poll` 2026-10-16..21.
* Record 2 (`SPIC`) has `isin: null` — the existing no-ISIN path.
* The other 38 are well-formed and publish.

Why 41 records and not fewer: the parser fails a file whose malformed-ISIN share is over
`MAX_BAD_ISIN_PCT` (5%), so carrying both real malformed records under it takes at least 40. 41
keeps the main case off the boundary; the tests drop one and two well-formed records to pin it
(2 of 40 publishes, 2 of 39 fails).

## `json_v1/corporate-share-holdings-master_20260807.json` — synthetic, the M3.6 build era

A **format-faithful synthetic fixture** written at M3.6 to the key set then believed to be served
(`fixture.frozen: false` in the register until M13.3). It is not a live sample: it carries
`public_prcnt`, `pledgeShares_prcnt`, `fii_prcnt` and `dii_prcnt`, which the 2026-10-06 capture
showed the live master does not. It stays as the pledge-present era — the fixture that proves a
stated pledge over 50% still breaches BC3 — and so that a payload in that shape keeps parsing.
