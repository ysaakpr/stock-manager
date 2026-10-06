# BSE bhavcopy fixtures (M3.1)

Two format eras, matching `source_register.yaml` rows `bse_bhavcopy_udiff` and
`bse_bhavcopy_legacy` (§4.1 row 4, "BSE equity OHLCV").

## What these files are

These are **format-faithful fixtures**, constructed to the exact column layout and row shape the
BSE endpoints were verified to serve. They are not the live verification sample bytes: the live
fetch that verified each endpoint on **2026-08-08** (browser UA + `Referer: https://www.bseindia.com/`,
no session cookie) is recorded in `source_register.yaml` — the real evidence for those rows is the
`last_http_status`, `sample_bytes`, `content_type` and `sample_sha256` captured there. The frozen
fixtures here reproduce that verified format so the offline suite (AGENTIC_CONTEXT B8 — tests never
touch the network) exercises the parser against every shape it must handle, including the edge cases
below. When the B1/M1.13 bulk fetch runs, the real per-era sample lands in L0 and can replace these
byte-for-byte without a parser change.

## `udiff/BhavCopy_BSE_CM_0_0_0_20260807_F_0000.CSV` — UDiFF era (post-08-Jul-2024)

A **bare CSV** (not a zip — the register `parse_check` notes BSE serves this uncompressed despite the
sibling NSE `.csv.zip` pattern). The 34-column UDiFF header is identical to NSE's, verbatim:

```
TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,
StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,
SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,
Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4
```

Every data row is `Sgmt=CM`, `Src=BSE`, `FinInstrmTp=STK`, derivative columns empty. Five rows,
session **2026-08-07**:

| Scrip | ISIN | Symbol | Group | Note |
|---|---|---|---|---|
| 500325 | INE002A01018 | RELIANCE | A | dual-listed with NSE — the no-clobber anchor |
| 532540 | INE467B01029 | TCS | A | dual-listed with NSE |
| 500209 | INE009A01021 | INFY | A | dual-listed with NSE |
| 543066 | INE0J1Y01017 | BSEONLY | B | BSE-only ISIN — gets a fresh security_master row |
| 974567 | INE002A08013 | RELNCD26 | F | **empty `LastPric`** — the "no last snapshot" normalisation case |

ISIN is native to this era, so a row becomes a `PriceRow` directly and lands in L1 ISIN-keyed under
the identical schema as NSE (acceptance 2).

## `legacy/EQ020124_CSV.ZIP` — legacy era (pre-08-Jul-2024)

A **zip of one CSV** (`EQ020124.CSV`), 14 columns, **no ISIN and no timestamp column**:

```
SC_CODE,SC_NAME,SC_GROUP,SC_TYPE,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,NO_TRADES,NO_OF_SHRS,
NET_TURNOV,TDCLOINDI
```

Three rows for session **2024-01-02** (the date the `EQ{DDMMYY}` filename encodes — the parser is
handed it, since the file carries no date). Scrips 500325 (RELIANCE) and 532540 (TCS) resolve to
ISIN through the scrip master; **999999 (GHOSTSCRIP)** is deliberately absent from the scrip master
so `resolve_legacy` quarantines it — the "unknown scrip is counted, never guessed" case
(invariant #2).

## `legacy/EQ291221_merged_records.CSV` — a real defect in BSE's own file (2021-12-29)

**Real bytes, not constructed.** A verbatim excerpt of the CSV member (`EQ291221.CSV`) of the L0
payload `bse_bhavcopy_legacy/2021/12/EQ291221_CSV.ZIP` (sha256
`e709f214f4de3170fe9b97b8462bdadf4da9193da3e0dcdddf34bdcd786f85f1`, fetched 2026-09-06): the
header (line 1) and lines 1772–1774, CRLF line endings kept. Excerpt sha256
`b88937545f2a675a9d3d99ac62e839492aca48238f657399fc910ea316491af4`.

Line 1773 of BSE's file holds two records run together: the CRLF after scrip **531358** (CHOICE
INT., whose `TDCLOINDI` is empty) is missing, so that empty field fuses with the next record's
`SC_CODE` **531359** (SHRIRAM ASSE) and the line is 27 fields wide (13 + 14). Lines 1772 (531352)
and 1774 (531360) are the well-formed neighbours. A sweep of all 1,943 legacy sessions in L0 found
this to be the only wrong-width line. The parser splits it back into the two records
(`bse.bhavcopy.split_merged_records`); the tests derive the ambiguous variants (non-empty
`TDCLOINDI` in the seam, a non-numeric field in either half, the same scrip on both sides) from this
excerpt in memory, so the fixture stays the real bytes.

## `legacy/EQ250517_CSV.ZIP` — a whole real session with ex-markers (2017-05-25)

**Real bytes, not constructed.** A byte-for-byte copy of the L0 payload
`bse_bhavcopy_legacy/2017/05/EQ250517_CSV.ZIP` (sha256
`be03e5905a0e766cecb6826da1aeb37d27ac9f5ea36547c62fc9007fa1fec33f`, 93,863 bytes, fetched
2026-09-06). Chosen (l1-widen, 2026-10-06) by sweeping every 2017-2019 legacy session for the most
distinct `TDCLOINDI` values: this one carries five — `XD` ×4, `SA`, `XB`, `SS`, `CS` — on otherwise
ordinary rows, so the ex-marker the parser now keeps (`BseLegacyQuote.close_indicator`) is tested on
the exchange's own publication rather than on a constructed value.
