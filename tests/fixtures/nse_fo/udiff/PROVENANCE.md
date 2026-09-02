# NSE F&O bhavcopy fixture (post-08-Jul-2024 UDiFF era)

**Synthetic, hand-authored — not a live download.** The build host does not fetch during a task
(AGENTIC_CONTEXT §8: the crawl engine fetches, tests never touch the network — B8), and the F&O
source's real fixture was left unfrozen at planning time (`source_register.yaml` →
`nse_fo_bhavcopy`, `fixture.frozen: false`, `task: M3.7`). This file is therefore constructed to
mirror the **structure** of the verified real sample recorded in the register (that sweep confirmed
`https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_20260807_F_0000.csv.zip` → 200,
1,057,426 B, `application/zip`, one member, 34 columns, the same UDiFF header as the cash file with
`XpryDt`/`StrkPric`/`OptnTp`/`OpnIntrst`/`ChngInOpnIntrst` populated).

Its **values** are deliberately small and round so that PCR, total OI + change, futures basis, and
the rollover proxy reconcile to an exact hand check in `tests/unit/test_fo.py` (acceptance criterion
1). It is a reconciliation fixture, not evidence of a fetch. When the real F&O file is fetched
through the M1.2 crawl policy in a later run, freeze it beside this one and flip `fixture.frozen` in
the register; this synthetic file can then stay as the arithmetic-reconciliation case.

| File | Bytes | sha256 |
|---|---|---|
| `BhavCopy_NSE_FO_0_0_0_20260807_F_0000.csv.zip` | 816 | `8c675cc9b11c30afcc738efdddececdc3527536ca27263af093469aa202c19d1` |

One zip member, `BhavCopy_NSE_FO_0_0_0_20260807_F_0000.csv`, 34-column UDiFF header (identical to the
cash file's — the per-row `Sgmt=FO` guard, not the header, is what refuses a cash file):

```
TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,
StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,
SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,
Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4
```

## Contents (trade date 2026-08-07)

Two underlyings, near expiry `2026-08-27`, next expiry `2026-09-24`, all rows `Sgmt=FO`, ISIN column
empty (as the real file ships it for derivatives):

**NIFTY** (index, spot `UndrlygPric=24000`): near future settle 24050 OI 1000 (Δ+100); next future
settle 24100 OI 400 (Δ+50); CE 24000 OI 500 (Δ+20); CE 24100 OI 300 (Δ+10); PE 24000 OI 700 (Δ+30);
PE 23900 OI 300 (Δ+5).

**RELIANCE** (stock, spot `UndrlygPric=3000`, resolves to `INE002A01018`): near future settle 3010 OI
800 (Δ−50); next future settle 3025 OI 200 (Δ+20); CE 3000 OI 400 (Δ+10); PE 3000 OI 300 (Δ−20); PE
2900 OI 100 (Δ+5).

### Hand-check aggregates the test asserts

| underlying | total_oi | total_oi_change | call_oi | put_oi | pcr_oi | spot | near_future | basis | basis_pct | near_month_oi | next_month_oi | rollover_pct |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| NIFTY (INDEX) | 3200 | +215 | 800 | 1000 | 1.250000 | 24000 | 24050 | +50 | 0.208333 | 1000 | 400 | 0.285714 |
| RELIANCE (STOCK) | 1800 | −35 | 400 | 400 | 1.000000 | 3000 | 3010 | +10 | 0.333333 | 800 | 200 | 0.200000 |

Regenerate with `scratchpad/gen_fixture.py` (kept in the M3.7 build log, not committed) — any change
to the values must be reflected in this table and in the test's expectations.
