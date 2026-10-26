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

## `2026-09-06/ListofScripData_{Active,Suspended,Delisted}.json`

A **record-verbatim cut** of the live snapshot the one BSE scrip-master merge read on 2026-09-06
(L0 `bse_scrip_master/2026/09/`, logical date 2026-09-06, fetched 20:41 IST). Every record is
byte-for-byte the object BSE served; only the selection is ours. Source payload sha256:

| Status | records (live → cut) | L0 sha256 |
|---|---|---|
| Active | 4,997 → 11 | `697a93a103903e75103b9225e2af32fb5bad854725275d86eb992a7ff42fbe5e` |
| Suspended | 1,240 → 1 | `c5bb65675fb2c470571340f9a5ddbb163d2ab727498aa1e2a89d434d15d363cc` |
| Delisted | 4,614 → 11 | `e39ca0fee767564141d5153c94631cca23a71a63b7c5093f939ac4ab2206578b` |

What is in it, and why:

* **The ten reused tickers** behind `identity_reconciliation` ids 1-10: each `scrip_id` is listed
  once Active under a live ISIN and once Delisted under a dead one — ALPHA, BAYERCROP, BOSTON,
  CMSINFO, IDEA (Ideaspace Solutions 530297, then Vodafone Idea 532822), MILLENNIUM, ONIDA,
  SAWACA, SUMMITSEC, SUPHA. A merge that opens a current window for the delisted scrip makes each
  ticker resolve two ways from the snapshot date.
* **RELIANCE 500325 Active and RILBBPH 532611 Delisted, one ISIN** — the buyback counter. The ISIN
  is listed, so it keeps its window; the dead counter must not take it away.
* **BALLARPUR 500102 Suspended** — suspended is still listed, and keeps its window.
