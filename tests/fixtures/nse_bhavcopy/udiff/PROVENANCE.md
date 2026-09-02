# NSE UDiFF bhavcopy fixtures (post-08-Jul-2024 era)

Real files, fetched from the live NSE archive host (`nsearchives.nseindia.com`) under the
`nse_bhavcopy_udiff` register policy — browser UA, `Referer: https://www.nseindia.com/`, ≥2.5 s host
spacing — during M1.5 on **2026-09-02**. Fetched byte-for-byte from the source; the checksums below
are the sha256 of each downloaded zip exactly as served.

URL pattern (`source_register.yaml` → `nse_bhavcopy_udiff`):
`https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip`

| Session | File | Bytes | sha256 | Data rows | Era position |
|---|---|---|---|---|---|
| 2024-07-08 | `BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv.zip` | 166,236 | `0ef55b77c30c8a57d5451cd371424242ad515f708630736ea6dc44c38d6e1e85` | 2,815 | **the cutover session** — the first day published in UDiFF, and the day the legacy `cm08JUL2024bhav.csv.zip` URL 404s |
| 2026-08-07 | `BhavCopy_NSE_CM_0_0_0_20260807_F_0000.csv.zip` | 194,665 | `33817d3100c82c3c90bc5349447cf04c749b07bf01b5a66260213ede0f7fab8b` | 3,473 | recent; sha256 is byte-identical to the C.1 source-verification sweep's sample (`source_register.yaml`, `sample_sha256`) |

Both carry the 34-column UDiFF header, shared verbatim with the F&O bhavcopy:

```
TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,
StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,
SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,
Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4
```

Every data row is `Sgmt=CM`, `Src=NSE`, `FinInstrmTp=STK`, with the derivative columns
(`XpryDt`/`StrkPric`/`OptnTp`/`OpnIntrst`/…) empty. The parser requires `CM`/`STK` per row precisely
because the header alone cannot distinguish this cash file from the F&O file that shares it.

## The cutover, verified against the archive (M1.5)

`bhavcopy.py` routes `>= 2024-07-08` to this parser and `< 2024-07-08` to the legacy one. That the
boundary date itself belongs to UDiFF was checked live during M1.5, not assumed:

* `BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv.zip` → **HTTP 200**, 166,236 B (this fixture).
* `content/historical/EQUITIES/2024/JUL/cm08JUL2024bhav.csv.zip` (the legacy pattern for the same
  day) → **HTTP 404**, 3,425 B error page.

So 08-Jul-2024 is the first day the UDiFF file exists and the first day the legacy file does not.

## Cross-era continuity anchor (acceptance 3)

RELIANCE (`INE002A01018`, series `EQ`) is present in both eras. The last legacy session,
`cm05JUL2024bhav.csv.zip` (M1.4 fixture), closed RELIANCE at **3177.25**; this UDiFF fixture for the
very next session (2024-07-08) reports RELIANCE `PrvsClsgPric` = **3177.25** — the prior session's
close carried across the format break unchanged. The continuity test asserts the two parsers emit
the same `PriceRow` type with the same field types and the same semantics for this symbol.

## The empty `LastPric`

`BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv.zip` contains exactly one row (`IBHFZC25B`, series `AT`)
whose `LastPric` is empty though the security traded. This is the UDiFF spelling of the legacy era's
"`LAST = 0` when there is no last-traded-price snapshot" — the parser normalises the empty to
`Decimal(0)` so the two eras' `last` field means the same thing (see `bhavcopy_udiff.py`). It is a
real row in a real file, kept on purpose as the fixture for that normalisation.
