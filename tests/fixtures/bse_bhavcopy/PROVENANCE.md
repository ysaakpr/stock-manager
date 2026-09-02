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
