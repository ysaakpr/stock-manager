# ACQUISITION ATLAS — Indian equities, 20-year depth (2006 → 2026)

Produced 2026-09-07 by a read-only research pass over `/home/ubuntu/stock-manager` plus ~90
spaced single-request network probes (browser UA, >=2.6 s/host, no bulk fetch, no evasion,
403 hard stop armed and never tripped). Nothing in the repo was written or committed.

**Method / labels.** `VERIFIED` = I issued the request on 2026-09-07 and inspected the bytes, or
it is recorded in this repo with checksum evidence (cited). `VERIFIED (repo)` = a real fetch
recorded in `dataplatform/ingest/source_register.yaml` with status/size/sha256. `BELIEVED` = from
knowledge, not probed today. Every depth number below carries one of these.

**Headline.** A genuinely honest 20-year Indian equity backtest *is* buildable free — but not from
the sources this repo currently wires. The three things that make it possible are all deeper than
the register claims:

| | Register says | Measured 2026-09-07 |
|---|---|---|
| NSE bhavcopy | `era.start: null`, "14-col format" | **1995-01-02** (11-col) / **2011-06-22** (14-col, ISIN) |
| NSE delivery (MTO) | "at least 2016-01-04" | **2002-01-02** (three format eras) |
| NIFTY TRI | `status: FAILED` | **2001-04-02 -> 2026-09-04, 6,321 rows, one POST** |

And one sleeper nobody in the repo has touched: `PR<DDMMYY>.zip`, NSE's daily report bundle, which
back to **2010-01-04** carries daily corporate actions, board meetings, announcements, price-band
hits, and — in two distinct eras — **shares outstanding (`mcap`)** and **index constituents with
weights and free-float caps (`Ix`)**.

---

# PART 1 — ENTITY BY ENTITY

## A. NSE equity EOD OHLCV

### A1. NSE legacy bhavcopy (pre-UDiFF)

- **Operator / URL:** NSE ·
  `https://nsearchives.nseindia.com/content/historical/EQUITIES/{YYYY}/{MON}/cm{DD}{MON}{YYYY}bhav.csv.zip`
  (MON = 3-letter upper, e.g. `JUL`)
- **Entity / grain:** one row per (symbol, series) per session — O/H/L/C/last/prevclose/qty/turnover.
- **REAL DEPTH — VERIFIED today:** `cm02JAN1995bhav.csv.zip` -> **HTTP 200, 4,177 B, 204 rows**.
  Also 200 at 2000-01-03, 2006-01-02, 2008-01-02, 2009-01-02, 2010-01-04, every 2011 month probed,
  2012, 2013, 2024. **The archive reaches 1995.** (The register's `era.start: null` understates
  this; the repo's own note "only back to some point in the early 2010s" is about *ISIN*, not about
  the files.)
- **FORMAT ERAS — all cutovers VERIFIED today by bisection:**

| Era | Range | Header | Notes |
|---|---|---|---|
| **E1** | 1995-01-02 -> **2011-06-21** | 11 names + trailing comma: `SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,` | **no ISIN, no TOTALTRADES** |
| **E1a** | 1995 -> **2010-01-04 (last seen unpadded)** | as E1 | `TIMESTAMP` day **not zero-padded** (`2-JAN-2006`, `4-JAN-2010`) |
| **E1b** | **2010-02-01 (first seen padded)** -> 2011-06-21 | as E1 | `TIMESTAMP` zero-padded (`01-FEB-2010`) |
| **E2** | **2011-06-22** -> 2024-07-05 | 13 names + trailing comma, adds `TOTALTRADES,ISIN` | ISIN native |

  Exact ISIN cutover: `cm21JUN2011` = 11 cols (42,395 B) · `cm22JUN2011` = **14 tokens with ISIN**
  (53,301 B). Both HTTP 200 same session. VERIFIED.
- **Access:** anonymous GET, no cookie. Requires `User-Agent: <browser>` +
  `Referer: https://www.nseindia.com/`. No robots.txt on `nsearchives` (404, VERIFIED repo) —
  sibling `www.nseindia.com` publishes `Allow: /` except `/market-data-test`. Programmatic bulk
  history is not prohibited by any published directive; the repo's own policy (2.5 s spacing,
  403 hard stop) is self-imposed.
- **Cost:** free.
- **Quirks:** trailing comma on every line is part of the format (14 tokens for 13 names).
  Holiday/non-session -> **404 with a 3,425 B HTML error page** (VERIFIED: `cm26JAN2026` = 404),
  *not* 200-with-empty. `ISIN` occasionally carries a placeholder (`DUMMY`/`NA`/`-`) for
  partly-paid instruments.
- **Reliability:** the pre-2011-06-22 half has **no ISIN at all** — the single hardest constraint on
  20-year work here (see Part 2).
- **Already ingested?** Yes, era E2 only. `dataplatform/ingest/nse/bhavcopy_legacy.py:61`
  (`LEGACY_SOURCE_ID`), `:65` (`LEGACY_ERA_END = 2024-07-08`), `:69` (`LEGACY_COLUMNS` — 14-col).
  The docstring at `:9-10` names the 11-col sub-era and the parser **refuses** it rather than emit
  ISIN-less rows. Lake coverage today: 2016-09-02 -> 2026-09-01
  (`ops/gates/data-catalogue-2026-09-06.md`).

### A2. NSE UDiFF bhavcopy

- **URL:** `https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip`
- **Depth:** 2024-07-08 -> present. VERIFIED (repo) 200 / 194,665 B / sha `33817d31...`.
- **Format:** 34 columns, `TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,...`.
  Holiday -> 404 (VERIFIED today, `20260126`).
- **Already ingested?** Yes — `dataplatform/ingest/nse/bhavcopy_udiff.py:75,80,85`; era switch
  asserted in `dataplatform/ingest/nse/bhavcopy.py:45-48`.

### A3. Fallback mirrors

`samco.in` mirror, `GetBhavcopy` — listed as fallbacks in the register
(`source_register.yaml:273`), never probed. BELIEVED to exist; both are third-party
redistributions, so ToS is theirs not NSE's. Not needed: the primary reaches 1995.

---

## B. NSE delivery / MTO — the big depth win

### B1. MTO (Security-wise Delivery Position)

- **URL:** `https://nsearchives.nseindia.com/archives/equities/mto/MTO_{DDMMYYYY}.DAT`
- **REAL DEPTH — VERIFIED today:** 200 at **2002-01-02** (15,683 B) · 2003-01-02 · 2005-01-03 ·
  2006-01-02 (28,379 B) · 2011-07-01 · 2026-08-07. **404 at 2001-07-02.** So the archive begins
  between 2001-07 and 2002-01. This is a **~24-year delivery-percentage series, free** — and it is
  17.5 years deeper than `sec_bhavdata_full`.
- **FORMAT ERAS — VERIFIED today, four of them, and only one is handled by this repo:**

| Era | Seen at | Shape |
|---|---|---|
| **M1** | 2002-01-02 | No title, no column header, no `Trade Date` line. `10,MTO,DDMMYYYY,<val>,<count>` then `20,<SYMBOL>,<SERIES>,<deliverable qty>` — **4 fields; NO traded quantity and NO delivery %** |
| **M2** | 2003-01-02 | Title line; **`Trade Date <...>` line BEFORE the `10,MTO` record**; no `Settlement Type`; 7-field `20` rows with Sr No + % |
| **M3** | 2005-01-03, 2006-01-02 | Title; `10,MTO`; **then** `Trade Date <...>,Settlement Type <N>,...`; 7-field rows |
| **M4** | 2011-07-01 -> | **Multiple settlement blocks in one file** — `Settlement Type <D>` block *and* `<N>` block, each with its own `Trade Date` line, its own column header, and Sr No restarting at 1 |

- **Access:** anonymous GET, browser UA + Referer. Free. Holiday -> **404** (VERIFIED,
  `MTO_26012026`).
- **Quirks:** `Name of Security` is **two** comma-separated fields (symbol AND series), so the
  header names six fields and a data row carries seven — splitting on the header count shears the
  series off (documented `dataplatform/ingest/nse/mto.py:27`). **No ISIN** — joins on
  SYMBOL+SERIES, must go through the D2 master.
- **Reliability / holes:** era M1 cannot yield delivery *percentage* on its own; you must divide by
  `TOTTRDQTY` from the same session's bhavcopy (which exists back to 1995). Era M4's two blocks
  will produce **two rows per (symbol, series)** with different quantities — a naive loader
  double-counts or last-write-wins.
- **Already ingested?** Yes, eras M2-M3 only. `dataplatform/ingest/nse/mto.py:57`
  (`MTO_SOURCE_ID`), `:63` (7-field record contract), `:118` raises on wrong field count ->
  **era M1 is refused**, and nothing in the parser segments era M4's repeated blocks. The register
  (`source_register.yaml:306-308`) claims only "at least 2016-01-04". Lake: 759 MTO payloads.

### B2. `sec_bhavdata_full`

- **URL:** `https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{DDMMYYYY}.csv`
- **Depth:** **2019-09-30** onward. VERIFIED (repo) by 9-request binary search; 2019-09-27 is the
  last 404 (`source_register.yaml:346-354`).
- **THE WORST TRAP FOUND TODAY.** On a **market holiday this file returns HTTP 200 with the
  previous session's data**: `sec_bhavdata_full_26012026.csv` (Republic Day) -> **200, 352,669 B,
  3,102 rows, every row `DATE1 = 23-Jan-2026`**. VERIFIED today. Its siblings (bhavcopy, MTO) all
  404 for the same date. Any fetcher that keys the payload by the requested date silently files
  Friday's delivery under Monday. The parser is safe by design —
  `dataplatform/ingest/nse/delivery.py:210` explicitly says the filename "is not parsed for the
  date... the rows carry `DATE1`" — but the *sync/fetch* layer will still mark 26-Jan `PUBLISHED`.
- **Quirks:** 15 columns, **leading space in every field name after the first** (`" SERIES"`,
  `" DATE1"`). No ISIN.
- **Already ingested?** Yes — `dataplatform/ingest/nse/delivery.py:72,83`. Lake: 1,712 payloads.

---

## C. NSE `PR<DDMMYY>.zip` — the daily report bundle (not in this repo at all)

- **URL:** `https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR{DDMMYY}.zip`
- **REAL DEPTH — VERIFIED today:** 200 at **2010-01-04** (255,693 B), 2010-07-01, 2011-07-01,
  2012-07-02, 2013-07-01, 2016-07-01, 2019-07-01, 2021-01-04, 2023-07-03, 2024-01-02, 2024-07-01,
  2025-07-01, 2026-09-04 (665,049 B). **404 at 2009-10-01, 2009-12-01, 2008-07-01, 2007-01-02,
  2006-01-02, 2006-07-01.** Archive begins ~ **2010-01**.
- **Members and what each one is worth** (from the bundle's own `readme.txt`, VERIFIED today):

| Member | Content | Why it matters for 20-year work |
|---|---|---|
| **`mcap<DDMMYYYY>.csv`** | `Trade Date,Symbol,Series,Security Name,Category,Last Trade Date,Face Value,Issue Size,Close,Market Cap` | **Daily shares-outstanding series.** `Category` = `Listed`/`Permitted`; `Last Trade Date` detects delistings |
| **`Ix<DDMMYY>.csv`** | `INDEX_FLG,SYMBOL,SERIES,SECURITY,ISSUE_CAP,CLOSE_PRIC,MKT_CAP,WEIGHTAGE` | **Historical index membership + index weight + index-cap** per index, per day |
| `Bc<DDMMYY>.csv` | `SERIES,SYMBOL,SECURITY,RECORD_DT,BC_STRT_DT,BC_END_DT,EX_DT,ND_STRT_DT,ND_END_DT,PURPOSE` | **Dated** corporate-action archive — its file date is when NSE published that state |
| `An<DDMMYY>.txt` | company · symbol · announcement text | dated announcement archive |
| `Bm<DDMMYY>.txt` | `COMPANY SYMBOL : BM DATE : BM PURPOSE` | **board meetings** |
| `bh<...>.csv` | securities that hit their price band that day | circuit-band hits |
| `HL`, `Gl`, `Tt` | 52-wk high/low, gainers/losers, top-25 by value | |
| `Pd`, `Pr` | security + index market data with `HI_52_WK`, `LO_52_WK` | 52-wk levels, *unadjusted* (readme says so) |
| `etf`, `sme`, `corpbond` | those segments | |
| 2010-2016 only | `fo...zip`/`cd...zip` nested, `RPD`, `NPD`, `Rtt`, `ffix`, `op`, `cf` | F&O + currency bundled in |

- **FORMAT ERAS — VERIFIED today:**
  - **Member-name case:** `Pd010719.csv`, `Bc010713.csv` (Mixed) through 2025-07-01 ->
    **all-lowercase** by 2026-09-04. A case-sensitive extractor breaks at the boundary.
  - **Intra-zip date-format inconsistency:** in `PR010710.zip`, most members are `DDMMYY` but
    `bh01072010.csv` is `DDMMYYYY`.
  - **`Ix` exists 2010-01-04 and 2010-07-01; gone by 2011-07-01.** And the index set **varies day to
    day**: 2010-01-04 carries `BANK Nifty, CNX 500, CNX IT, CNX Midcap, Nifty Midcap 50`;
    2010-07-01 carries **only `CNX 500`**. A rotating publication, not a full daily snapshot.
  - **`mcap` absent 2024-01-02, present 2024-07-01.** Cutover in H1-2024.
  - **`Bc` date format:** `DD/MM/YYYY` in 2010/2013/2016/2019 -> **`YYYY-MM-DD`** in 2026.
  - **`bh` header:** 2010 `SYMBOL,SR,SECURITY,HIGH/LOW,INDEX FLAG` -> 2013-2019
    `SYMBOL,SERIES,SECURITY,HIGH/LOW,INDEX FLAG` -> 2026 drops `INDEX FLAG`.
- **Cost:** free. Anonymous GET, browser UA + Referer. ~250-670 KB per session.
- **Already ingested?** **No.** No `pr`/`mcap`/`Ix`/`Bc` string appears anywhere in
  `dataplatform/ingest/`. This is the single largest unexploited free source for this project.

---

## D. BSE equity EOD

### D1. BSE legacy bhavcopy

- **URL:** `https://www.bseindia.com/download/BhavCopy/Equity/EQ{DDMMYY}_CSV.ZIP`
- **REAL DEPTH — VERIFIED today:** 200-zip at **2006-04-03** (85,497 B), 2006-07-03, 2007-07-02,
  2008-07-01, 2009-01-02, 2010-07-01, 2012-07-02, 2016-07-01, 2020-07-01, 2024-07-01.
  **Soft-404 at 2006-02-01, 2006-01-02 and 2005-07-01.** Archive begins **between 2006-02-01 and
  2006-04-03** — almost certainly the FY2006-07 boundary. **BSE is ~11 years shallower than NSE.**
- **Format:** one era only across the whole range (VERIFIED, headers byte-compared): 14 columns
  `SC_CODE,SC_NAME,SC_GROUP,SC_TYPE,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,NO_TRADES,NO_OF_SHRS,NET_TURNOV,TDCLOINDI`.
  **No ISIN in any era** — `SC_CODE` only.
- **SOFT 404:** a missing file returns **HTTP 200 with 14,287 B of BSE's Angular `index.html`**
  (VERIFIED, byte-identical across four missing dates). Status-code checks are useless here; you
  must sniff for zip magic or the known HTML size. `EQ_ISINCODE_{DDMMYY}.ZIP` (either case) is
  **also** a soft-404 — that variant does not exist at this path.
- **robots:** BSE publishes none — `/robots.txt` returns 200 + 13,850 B of the same shell
  (VERIFIED repo). `api.bseindia.com/robots.txt` 301s to a member page. Treat as "no directives
  published"; re-check before a campaign.
- **Already ingested?** Parser yes, data no. `dataplatform/ingest/bse/bhavcopy.py:82`
  (`LEGACY_SOURCE_ID`), `:88` (CUTOVER). Catalogue: "VERIFIED, unfetched — pre-2024-07 files carry
  no ISIN; no backfill has run."

### D2. BSE UDiFF bhavcopy

- **URL:** `https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{YYYYMMDD}_F_0000.CSV`
  — **uncompressed** despite the NSE sibling being a zip.
- **Depth:** 2024-07-08 -> present. VERIFIED (repo), 845,830 B, 34-col UDiFF with ISIN.
- **Already ingested?** Yes, and it is the one dataset the server fetched itself: 536 payloads,
  411 MB, 2024-07 -> 2026-09.

### D3. BSE scrip master

- **URL:** `https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?Group=&Scripcode=&industry=&segment=Equity&status={Active|Suspended|Delisted}`
- **Depth:** snapshot, no history. VERIFIED (repo): `status=Active` -> 4,949 scrips, 1.7 MB, keys
  `SCRIP_CD, ISIN_NUMBER, Status, GROUP, FACE_VALUE, INDUSTRY, Mktcap`.
- **This is the survivorship fix for BSE:** `status=Delisted` and `status=Suspended` must be pulled
  or the backfill is silently survivorship-biased (`source_register.yaml:656`). Needs `Referer` +
  `Origin` headers.
- **Already ingested?** Yes — `dataplatform/ingest/bse/scrip_master.py:167`. 3 payloads captured
  2026-09.

### D4. BSE corporate actions / notices

- **CA URL:** `https://api.bseindia.com/BseIndiaAPI/api/DefaultData/w?ddlcategorys=E&ddlindustrys=&scripcode={SCRIP_CD}&segment=0&strSearch=S`
  — **per-scrip, no date parameter**; one call returns that scrip's whole published history
  (VERIFIED repo: 197 records, 56 KB). Depth per scrip **unmeasured** — flagged as an open B1
  survey (`source_register.yaml:544`).
- Two ex-date spellings in the same record (`Ex_date` and `exdate`) — both must be read.
- **Notices/circulars:** `https://www.bseindia.com/markets/MarketInfo/DispNoticesNCirculars.aspx`
  (HTML) / `api.bseindia.com/BseIndiaAPI/api/Noticesnew/w` — **BELIEVED**, not probed.
- **BSE index (SENSEX) history:** an `IndexArchDaily` style endpoint exists; my guessed
  parameterisation returned `200 {}` (VERIFIED as "endpoint live, params wrong"). Treat the URL
  shape as **BELIEVED**.
- **Already ingested?** CA parser yes (`dataplatform/ingest/bse/corp_actions.py:58`), never fetched
  — which is why all 2,487 CA quality flags in the lake read `SINGLE_SOURCE`.

---

## E. Securities master, identity, delisting

| Source | URL | Depth | Notes |
|---|---|---|---|
| NSE equity list | `https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv` | **snapshot only** | 8 cols, `SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE` (leading spaces). VERIFIED repo, 169,183 B. **No history — accumulate weekly copies forever or you cannot reconstruct who was listed in 2010.** |
| NSE symbol changes | `https://nsearchives.nseindia.com/content/equities/symbolchange.csv` | cumulative, all renames NSE publishes | VERIFIED today, 68,638 B. **Headerless — line 1 is data.** 4 cols: company, old symbol, new symbol, `DD-MON-YYYY` |
| NSE price-band / surveillance master | `https://nsearchives.nseindia.com/content/equities/sec_list.csv` | **snapshot only** | VERIFIED today, 200, 172,045 B. `Symbol,Series,Security Name,Band,Remarks` — Remarks carries `GSM STAGE - 0` etc. |
| NSE ASM | `https://www.nseindia.com/api/reportASM` | **snapshot only** | VERIFIED today, 200, 57,764 B. `{"longterm":{"data":[{asmSurvIndicator,asmTime,companyName,isin,survCode,survDesc}]}}`, `shortterm` sibling. **ISIN native.** Needs session cookie |
| NSE GSM | `https://www.nseindia.com/api/reportGSM` | **snapshot only** | VERIFIED today, 200, 20,521 B. `companyName,gsmStage,gsmTime,isin,survCode,survDesc` |
| NSE delisted list | `api/delisted-companies?index=equities` | — | **VERIFIED 404 today** ("Resource not found"). No such endpoint |
| BSE delisted | scrip master `status=Delisted` | snapshot | the only free machine-readable delisted register I could verify |
| Guesses that 404 | `content/equities/asm_longterm.csv`, `asm_shortterm.csv`, `gsm.csv`, `eq_gsm.csv`, `archives/equities/bandchanges/bandchanges_04092026.csv` | — | all **VERIFIED 404 today** — don't waste requests on these |

- **Already ingested?** `dataplatform/identity/ingest.py:264` (`parse_equity_list`), `:312`
  (`parse_symbol_changes`). The lake's identity master was **built from `tests/fixtures/`, not a
  live fetch** (`data-catalogue-2026-09-06.md` §2, finding N4) — 2,397 securities, 2,886
  symbol-history rows, and no `nse_equity_list` L0 anywhere. ASM/GSM/`sec_list` are **not** in the
  register or the code at all.

---

## F. Corporate actions (NSE)

- **URL:** `https://www.nseindia.com/api/corporates-corporateActions?index=equities&from_date={DD-MM-YYYY}&to_date={DD-MM-YYYY}`
  — needs the session cookie + `X-Requested-With`.
- **REAL DEPTH — VERIFIED today:** Jan **2001** -> 3 records (923 B). Jan **2006** -> real records
  with native ISIN (`INE366A01033`, ex-date 06-Jan-2006). Jan 2013 (whole month) -> 35 records.
  Jan 2026 (one week) -> 6 records. So the dated window **does reach 2001**.
- **TWO PIT DEFECTS, BOTH VERIFIED TODAY:**
  1. **`caBroadcastDate` is `null` in every dated-window response I fetched** — 2001, 2006, 2013
     *and* 2026. Yet the register's C.1 fetch of the *undated* endpoint got 20 records **with**
     `caBroadcastDate` populated. The historical window mode simply does not carry the
     first-knowable timestamp.
  2. `dataplatform/ingest/nse/corp_actions.py:191-192` sets
     `knowable = broadcast if broadcast is not None else clock.now().date()`. A 20-year backfill
     through this feed therefore stamps **today** as the knowable date on every single historical
     action — which inverts invariant #7 for the whole history.
- **The fix is free and already sitting in §C:** `Bc<DDMMYY>.csv` inside `PR<DDMMYY>.zip` is a
  **dated** CA archive back to 2010-01 — the file's own date is when NSE published that state.
  Below 2010 there is no free broadcast-date source; the honest options are the announcements feed's
  `an_dt` (§G) or accepting ex-date-only PIT with a stated caveat.
- **Already ingested?** Yes — `dataplatform/ingest/nse/corp_actions.py:68`, backfill in
  `corp_actions_backfill.py`. Lake: 11 yearly chunks -> 12,126 CAs, `isin_lineage` 381,
  `adjustment_factors` 696 on 532 ISINs (**0 linked to a CA** — a known open defect).

---

## G. Corporate announcements & board meetings

- **URL:** `https://www.nseindia.com/api/corporate-announcements?index=equities&from_date={DD-MM-YYYY}&to_date={DD-MM-YYYY}`
- **REAL DEPTH — VERIFIED today, and it is excellent:** Jan 2006 (10 days) -> **428 records,
  406,643 B**. Jan 2010 (one month) -> **2,059 records, 1,867,218 B**. Payload keys include
  `an_dt, exchdisstime, dt, sort_date, desc, attchmntFile, attchmntText, hasXbrl, symbol, sm_isin,
  smIndustry`. **ISIN native.** ~20-year announcement archive, free.
- **PIT ERAS — VERIFIED today:**

| Era | Evidence |
|---|---|
| Jan **2006**: `an_dt` is **midnight-only** (`10-Jan-2006 00:00:00`) — date resolution, no intraday time | 2 distinct timestamps across 428 records |
| Jan **2008** ->: `an_dt` carries **real intraday times** (`07-Jan-2008 19:31:00`) | 64 records, all timestamped |
| `attchmntFile` = `-` (**no PDF**) in 2006 and 2008; **304 of 323** records have attachments by Jan 2016 | attachment archive starts between 2008 and 2016 |
| `exchdisstime` = `'-'` in **every** historical window (2006/2008/2010/2016) | only the live/recent feed populates it |

- So for anything before ~2007 the first-knowable time is a **date**, not a timestamp — a same-day
  decision cannot be gated honestly. State it as a caveat rather than pretend.
- **Board meetings:** `Bm<DDMMYY>.txt` in the PR zip, back to 2010-01 (VERIFIED, §C). Format
  `COMPANY NAME SYMBOL : BM DATE : BM PURPOSE`, colon-delimited, symbol glued to the company name —
  a nasty parse. No free BSE-equivalent archive verified.
- **BSE announcements:** `https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno={N}&strCat=-1&strPrevDate={YYYYMMDD}&strScrip=&strSearch=P&strToDate={YYYYMMDD}&strType=C&subcategory=-1`
  — VERIFIED (repo), paged, dated. Depth unmeasured.
- **Already ingested?** Parsers yes — `dataplatform/ingest/announcements.py:86-87`, `parse_nse` at
  `:267`, `parse_bse` at `:338`. **Never fetched** (catalogue §4: "No poll runner; BSE page 1
  only").

---

## H. Shareholding pattern (and why free-float history is not free)

- **URL:** `https://www.nseindia.com/api/corporate-share-holdings-master?index=equities[&from_date=&to_date=]`
- **Depth — VERIFIED today, and it is the worst result of the whole sweep:**

| Window | Records | `broadcastDate` of the records returned |
|---|---|---|
| Q1 **2010** | **0** | — |
| Q1 **2018** | **18** | `05-APR-2022 11:10:20` |
| Q1 **2021** | **49** | `04-OCT-2023`, `22-OCT-2024`, `03-JUN-2022` |
| Q1 **2023** | **2,020** | populated |
| unfiltered (repo C.1) | 2,284 | populated |

  The pre-2022 quarters were **retro-loaded**: their broadcast timestamps are 2022-2024. So the
  feed's own PIT field lies about history, and the record count collapses below ~2022.
  **NSE shareholding is honestly usable from ~2022-23 forward and no further.**
- **Quirks:** filing date != quarter end; payload carries both `date` (quarter end) and
  `broadcastDate`/`cgTimeStamp`.
- **Already ingested?** Parser yes — `dataplatform/ingest/shareholding.py:86,243`. Never fetched.
- **Consequence:** free-float and promoter-holding *history* for 2006-2021 does not exist in
  machine-readable form from NSE. See Part 2.

---

## I. Indices — levels, TOTAL RETURN, and constituents

### I1. NIFTY TRI / NTR — corrects a FAILED row, verified live today

- **Endpoint:** `POST https://niftyindices.com/BackPage/getTotalReturnIndexString` — **no `.aspx`**
- **Body:** `{"cinfo": "{'name':'NIFTY 50','startDate':'01-Apr-2001','endDate':'05-Sep-2026','indexName':'NIFTY 50'}"}`
  (single-quoted inner string, deliberately)
- **VERIFIED live 2026-09-07:** HTTP **200, 896,301 B, 6,321 rows**, first `04 Sep 2026` TRI
  `36312.44` / NTR `31531.71`, last **`02 Apr 2001`** TRI `1219.67`. **No cookie. No Referer.
  One request = 25 years.**
- **Thematic depth:** `NIFTY INDIA CONSUMPTION` -> 200, 769,525 B, **5,125 rows back to
  `02 Jan 2006`** at base `1000.00`. VERIFIED today.
- **Price-index OHLC sibling:** `POST /BackPage/getHistoricaldatatabletoString`, same body -> 200,
  **1,163,754 B, 6,321 rows**, `INDEX_NAME,HistoricalDate,OPEN,HIGH,LOW,CLOSE` back to
  `02 Apr 2001`. VERIFIED today.
- **Quirks (all VERIFIED today):** rows are **newest-first**; `Content-Type: text/html` on a JSON
  body; the response is a **bare JSON array** — *not* the `{"d": "<json-string>"}` envelope;
  `RequestNumber` regenerates per call; `NTR_Value` is the literal `'-'` for indices without a
  net-return series (must become `None`, never `Decimal 0`); the index name's case is
  **inconsistent inside one response** (`Nifty India Consumption` on the newest row,
  `NIFTY INDIA CONSUMPTION` on the oldest).
- **robots:** `niftyindices.com/robots.txt` = `Allow: /` with two disallowed report paths, neither
  of which is `/BackPage/` (VERIFIED repo). **ToS caution:** NSE Indices licenses index data
  commercially; free retrieval is technically open and not robots-disallowed, but redistribution is
  a licensing question, not a robots one.
- **Repo status — needs correcting:** `source_register.yaml:731` still says `status: FAILED` against
  the **stale** `Backpage.aspx` path, and `ops/gates/data-catalogue-2026-09-06.md` §4 still lists
  the benchmark as "a proxy TRI computed from L1". `HUMAN_DECISIONS.md:225-260` already records the
  corrected path and a 2026-08-10 probe (6,213 rows to 02-Apr-2001) — my probe today reproduces and
  extends it (6,321 rows). Meanwhile `dataplatform/ingest/indices.py:653` still expects the
  `{"d": ...}` envelope, which today's response does not have.

### I2. Index closing levels (all indices, one file per session)

- **URLs (two hosts, byte-identical payload):**
  `https://niftyindices.com/Daily_Snapshot/ind_close_all_{DDMMYYYY}.csv` ·
  `https://nsearchives.nseindia.com/content/indices/ind_close_all_{DDMMYYYY}.csv`
- **Depth:** **2012-10-01** is the epoch on *both* hosts. VERIFIED (repo, niftyindices: 404 at
  2012-07-01 and 2011-04-01, 200 at 2012-10-01) and VERIFIED today on nsearchives (2012-10-01 ->
  200/2,900 B; 2013-07-01 -> 200; 2015-07-01 -> 200; 2026-01-02 -> 200/15,284 B).
- **Format:** 13 columns, byte-identical header 2012->2026:
  `Index Name,Index Date,Open/High/Low/Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield`.
  No format eras (unusual, and worth remembering).
- **NAMING ERAS ARE THE REAL HAZARD:** NSE/IISL renamed **48 of 53 index names between 2015-11-06
  and 2015-11-10** (`S&P CNX Nifty`/`CNX Nifty` -> `Nifty 50`; `CNX Nifty Junior` ->
  `Nifty Next 50`; `CNX Bank` -> `Nifty Bank`). I saw all three generations today:
  `S&P CNX Nifty` (2012-10-01), `CNX Nifty` (2013-07-01, 2015-07-01), `Nifty 50` (2026-01-02).
  **A consumer keyed on today's names reads zero rows from any pre-2015-11 file** and silently
  loses three years. Evidence-backed alias map:
  `dataplatform/ingest/macro/index_aliases.yaml`; 26 pre-2015 names are deliberately left unmapped
  rather than guessed (`ops/gates/M11-macro-source-probe.md`).
- **Index count grows** 30 -> 48 -> 69 -> 78 -> 94 -> 165, so a PIT read must not assume an index
  existed.
- **Already ingested?** Parsers yes — `dataplatform/ingest/indices.py:120,581` and
  `dataplatform/ingest/macro/index_valuation.py:62,146` (the P/E, P/B, Div Yield spine). Never
  fetched by a runner.

### I3. Index constituents — the survivorship problem

- **Current:** `https://niftyindices.com/IndexConstituent/ind_{index_slug}list.csv` — VERIFIED
  (repo), 5 cols `Company Name,Industry,Symbol,Series,ISIN Code`, **ISIN native**,
  `/IndexConstituent/` not robots-disallowed. **Always "as of today". There is no historical
  constituents download.** (`source_register.yaml:686`)
- **The free historical route found today:** `Ix<DDMMYY>.csv` in the PR zip (§C) —
  `SYMBOL,SERIES,SECURITY,ISSUE_CAP,CLOSE_PRIC,MKT_CAP,WEIGHTAGE` per index. Covers roughly
  **2010-01 -> some point in H2-2010/H1-2011** and publishes a **rotating subset of indices per
  day**. Real, verified, and far from complete.
- **Already ingested?** `dataplatform/ingest/constituents_ingest.py:231` defines the slug set
  (broad + sectoral + thematic); `constituents_snapshot_job.py` is the monthly accumulator. Lake:
  **one snapshot**, 794 rows, 16 indices, 2026-09-03. The weekly job "has never fired".

---

## J. Bulk & block deals

- **URLs:** `https://nsearchives.nseindia.com/content/equities/bulk.csv` · `.../block.csv`
- **Depth:** **rolling current file, no date parameter.** VERIFIED (repo). History accrues forward
  only; a missed day is unrecoverable from these URLs.
- **Format:** bulk = 8 cols with `Remarks`; block = 7 cols without. **No ISIN.** A 250 B
  `block.csv` is a genuinely quiet day (one deal), not a truncated response.
- **Already ingested?** Parser yes — `dataplatform/ingest/nse/deals.py:98-99,315`. No daily runner.

---

## K. FII / DII and FPI flows — structurally unobtainable for history

- **NSE:** `https://www.nseindia.com/api/fiidiiTradeReact` — **one session, ever.** MEASURED (repo,
  M3.4, three live requests): the bare endpoint and `?date=05-08-2026` return **byte-identical
  215 B** still dated 07-Aug-2026 — the feed **silently ignores a date parameter**, so a caller
  cannot distinguish a rejected date from a served one by status code. A dated archive equivalent
  (`/content/equities/fii_dii_07082026.csv`) 404s. `source_register.yaml:856-880`.
- **Consequence, stated plainly in the register:** *"The M3 gate's 'flows queryable 10 years back'
  is NOT achievable from this source."* A missed session is a permanent hole.
- **Fallbacks (FII leg only):**
  - **NSDL FPI portal** — `https://www.fpi.nsdl.co.in`, "Daily Trends in FPI Investments".
    Reachable 2026-08-08, 200 / 59,035 B; serves no robots.txt (`/robots.txt` 302s to
    `contactus.html`). VERIFIED (repo). **FPI-only, custodian-confirmed basis** — not the
    exchange's provisional numbers, so not a substitute.
  - **CDSL** fortnightly/monthly series. BELIEVED.
  - **SEBI FPI statistics** — `https://www.sebi.gov.in/statistics/fpi-investment.html`, with
    `fpi-investment/latest.html`, `.../archive.html`, `.../trade-wise-equity-data-of-fpi.html`.
    Equity, debt, derivatives, **ODIs/P-Notes**, sector-wise (fortnightly), AUC. VERIFIED reachable
    today; the archive's depth is not stated on the page.
  - **F&O-segment FII stats** (a *different* dataset):
    `https://nsearchives.nseindia.com/content/fo/fii_stats_{DD-Mon-YYYY}.xls` — **is** dated and
    deep (200 / 9,216 B for 07-Aug-2026, VERIFIED repo).
- **Already ingested?** Parser yes — `dataplatform/ingest/nse/fii_dii.py:92,284`. No runner. Zero
  sessions captured.

---

## L. F&O EOD

- **Legacy:** `https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{YYYY}/{MON}/fo{DD}{MON}{YYYY}bhav.csv.zip`
  — **VERIFIED today: 200 at 2006-01-02, 96,788 B, 9,208 rows.** 16 cols:
  `INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTIONTYPE,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,`
  (trailing comma). No ISIN. **~20-year F&O depth exists and this repo ingests none of it.**
- **A third, distinct F&O format** lives inside the PR zips: `fo{DDMMYYYY}.csv`, 12
  fixed-width-ish columns (`OPEN_INT*` zero-padded to 15 chars, `EXP_DATE` as `DD/MM/YYYY`), seen
  2010-01-04 and 2010-07-01. VERIFIED today. Do not assume one F&O schema.
- **UDiFF:** `https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip`,
  2024-07-08 ->. VERIFIED (repo).
- **Already ingested?** UDiFF era only — `dataplatform/ingest/nse/fo_bhavcopy.py:74,78`. No runner;
  `fo_aggregates` store exists empty.

---

## M. Trading calendar / holidays

- **Live API:** `https://www.nseindia.com/api/holiday-master?type=trading` — VERIFIED today, 200,
  34,022 B, `{"CBM":[{tradingDate,weekDay,description,...}]}`. **Current calendar year only.**
- **The free 20-year route:** derive it. A weekday with **no bhavcopy (404)** is a holiday —
  VERIFIED today on 2026-01-26 (Republic Day): legacy bhavcopy 404, UDiFF 404, MTO 404. Combined
  with §A's 1995-deep archive this reconstructs the whole calendar exactly, at one request per
  candidate weekday (and the bhavcopy campaign gives it to you for free as a by-product).
- **Muhurat.** Diwali Laxmi Pujan appears in the holiday master as a holiday but the exchange
  **publishes a bhavcopy** for it, weekends included — 11 such dates this decade, four of them
  Saturdays/Sundays (2016-10-30 Sun, 2019-10-27 Sun, 2020-11-14 Sat, 2023-11-12 Sun). So "is this a
  session" != "should a file exist". Handled in `dataplatform/ingest/calendar.py:11-19`.
- **Already ingested?** Yes, as a curated file: `dataplatform/ingest/data/nse_holidays.yaml`,
  **`coverage: 2016-01-01 -> 2026-12-31`** (VERIFIED by reading it). `expected_sessions`
  **refuses** a range outside coverage rather than assume no holidays. **This is a hard blocker on
  any pre-2016 backfill.**

---

## N. Fundamentals

### N1. NSE XBRL — the only true PIT fundamentals, free

- **Discovery (old feed):** `https://www.nseindia.com/api/corporates-financial-results?index=equities&period={Quarterly|Annual}&from_date=&to_date=`
  — the date filter is on the **broadcast** date, which is what makes a forward-walking PIT
  backfill possible.
- **Measured depth (repo, M7.3, whole window probed):** **135,197 announcements over ten financial
  years**, but a *document* exists only from ~FY2018-19: FY2016-17 and FY2017-18 are **100%
  placeholder** (24,247 entries, no documents), FY2018-19 **81%**, FY2019-20 **86%**, FY2020-21
  onward effectively complete. **~101,446 filings ingestible — that is the honest depth.**
  (`source_register.yaml:1294-1320`)
- **Integrated Filing feed (SEBI regime, quarter ended 31-Mar-2025 ->):**
  `https://www.nseindia.com/api/integrated-filing-results?type=Integrated%20Filing-%20Financials&index=equities&from_date=&to_date=&page={N}&size={M}`
  — 26,610 records all-time; `page` is 1-based in the request and **0-based in the response**;
  `size` up to 1000 verified. VERIFIED (repo, 2026-09-06).
- **Documents:** `https://nsearchives.nseindia.com/corporate/xbrl/{filename}.xml` — **never
  construct these; take them from the index's `xbrl` field.**
- **Quirks (all VERIFIED in-repo against captured filings):** the instance is a column-by-column
  transcription of the printed results table, not one period per document; entity identified by
  **NSE symbol**, and Ind-AS filings carry **no ISIN element at all** (a captured HDFCBANK filing
  states a stale ISIN); three P&L vocabularies selected by `schemaRef` entry point (Ind-AS/NBFC,
  banking, pre-Ind-AS "other than banks"); 2018-2022 `..._WEB.xml` files declare only *dimensioned*
  contexts, some declare no `<context>` and hence no entity; on an annual filing `OneD` is
  zero-filled and the year's numbers sit in `FourD`, so reading the document-level header as
  `OneD`'s period **stores a company's revenue as zero**; `LevelOfRounding` describes the printed
  statement and must **not** scale the values (they are absolute rupees). Field vocabularies differ
  between the two feeds (`Non-Consolidated` vs `Standalone`; `Un-Audited` vs `Unaudited`).
- **Known coverage holes in the derived store:** P&L + capital only. **No cash-flow statement
  anywhere**; equity on 19% of filings; `debt_equity_ratio` on 27% (catalogue §4).
- **Already ingested?** Yes, and it is the best-covered dataset in the lake:
  `dataplatform/ingest/xbrl/parser.py:87,270`, `xbrl/integrated.py:68,211`. **70,421 documents /
  4.1 GB / 2018-05 -> 2026-08 -> `pit_fundamentals` 1,139,430 facts · 2,212 ISINs · 86,414
  filings** (plus 3,093 FAILED).

### N2. screener.in

- **URL:** `https://www.screener.in/company/{SYMBOL}/` (HTML). Export at `/company/{SYMBOL}/export/`.
- **Status:** `BLOCKED_CREDENTIAL`. The Excel export returns **HTTP 404 + 11,441 B HTML** to an
  anonymous client — gated behind a login. The robots-permitted company page **works**: 200 /
  227,493 B and carries the same statements. VERIFIED (repo).
- **robots (VERIFIED repo):** company pages allowed; `Disallow: /user/*, /*?q=, /*?sort=,
  /*?limit=, /*?page=, /company/source/quarter/*`; 5 s spacing self-imposed.
- **PIT:** **NOT point-in-time.** Restated figures, no as-of date. Monitoring surface only,
  physically quarantined from backtests (invariant #8, decision #7).
- **Already ingested?** `dataplatform/ingest/screener.py:64,216,244`; used only for sanity checks.

### N3. Other free / freemium fundamentals — all BELIEVED

| Source | Model | India 20-yr fundamentals? |
|---|---|---|
| **Tickertape** (Smallcase) | freemium web, no public API; ToS forbids scraping | ~10 yr annuals, restated, no PIT |
| **Trendlyne** | freemium; paid API tiers | good CA/results coverage, restated |
| **EODHD** | paid API, roughly **US$20-80/mo**; NSE/BSE EOD + fundamentals | EOD deep; fundamentals ~10 yr, restated, India coverage patchy on small caps |
| **FinancialModelingPrep** | paid API, ~US$20-100/mo | India coverage thin and unreliable — not recommended as a primary |
| **Yahoo Finance** | unofficial | see §R |

---

## O. Mutual funds / AMFI

| Item | URL | Depth | Status |
|---|---|---|---|
| All current NAVs + scheme master | `https://portal.amfiindia.com/spages/NAVAll.txt` | today only | **VERIFIED today**: 200, **1,517,609 B**, `;`-delimited, `Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date`, AMC section headers interleaved as bare lines |
| NAV history | `https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?mf=&tp=1&frmdt=DD-Mmm-YYYY&todt=DD-Mmm-YYYY` | historically deep (to ~2006) — **BELIEVED** | **VERIFIED today: a plain GET no longer returns data.** Both parameterisations returned **200 with a 7,952/7,969 B XHTML *frameset*** whose only content is a `wait.gif` and a self-posting form (`action="./DownloadNAVHistoryReport_Po.aspx?..."`). It is now an **ASP.NET postback** — GET, extract `__VIEWSTATE`/`__EVENTVALIDATION`, then POST |
| **Stock categorization (large/mid/small cap)** | `https://www.amfiindia.com/research-information/other-data/categorization-of-stocks` | semi-annual (Jan & Jul), published as XLSX | **VERIFIED 404 today.** The site is a Next.js SPA (`/_next/static/...`) whose nav is not in the served HTML, and its assets sit under `https://www.amfiindia.com/uploads/...`. The current URL is **BELIEVED unknown** — resolve it by hand in a browser once |

- **Why the cap list matters:** it is the **only official** large/mid/small-cap definition in India
  (top 100 / 101-250 / 251+ by six-month average full market cap), it is what every cap-band
  mandate is written against, and it is published **only twice a year as a point-in-time list**.
  AMFI does not publish a machine-readable archive of past lists; if you did not capture the
  January 2010 file in January 2010, you cannot get it now. Third parties republish recent editions
  (e.g. July-2026: large-cap cutoff ~ INR 1,06,300 cr, mid-cap ~ INR 33,500 cr — Mata Securities),
  but those are secondary and start ~2018.
- **Reconstruction path that does work:** `mcap<DDMMYYYY>.csv` from the PR zip (§C, 2024->) plus
  `Ix` `ISSUE_CAP` (2010-2011) plus bhavcopy closes gives you *your own* six-month-average
  full-market-cap ranking on any date — an AMFI-*rule* reconstruction rather than the AMFI *list*.
  Say which one you used.
- **Already ingested?** **No AMFI source is in the register at all.** The catalogue flags this as an
  active hole: 593 `INF*` fund ISINs trade on NSE, **0 of 593 resolvable**, and LIQUIDBEES is the
  default cash-parking instrument that D2 cannot name (finding N5).

---

## P. SEBI, RBI, MCA, ratings, news

| Source | URL | What / depth | Status |
|---|---|---|---|
| **SEBI FPI statistics** | `https://www.sebi.gov.in/statistics/fpi-investment.html` (+ `/latest.html`, `/archive.html`, `/trade-wise-equity-data-of-fpi.html`) | equity/debt/derivatives/ODI/sector-wise/AUC; monthly + fortnightly | **VERIFIED reachable today**; archive depth not stated on the page |
| SEBI regulations / orders / MF disclosures | `https://www.sebi.gov.in/legal/regulations/`, `/enforcement/orders/`, `/statistics/` | deep (1990s->), PDF + some XLS | **BELIEVED** |
| **RBI DBIE** | `https://data.rbi.org.in/DBIE/` | policy repo rate, G-sec yield curve, FX reference rates, CPI/WPI, banking aggregates; CSV/XLSX export | URL **VERIFIED reachable**; content **BELIEVED** — it is a JS SPA and returned nothing fetchable, so treat depth (typically 1990s-> for rates, longer for annual series) as unverified. Legacy `dbie.rbi.org.in` mirrors still float around |
| RBI press releases | `https://www.rbi.org.in/pressreleases_rss.xml` | rolling RSS | **VERIFIED (repo)**, 200. `www.rbi.org.in/robots.txt` answers **HTTP 418** with an anti-bot page — that is *not* a policy document. Fetch only the published feed, at EOD cadence, spaced; a 403 is a hard stop |
| **World Bank Indicators** | `https://api.worldbank.org/v2/country/IND/indicator/{code}?format=json&per_page=n` | **annual** macro, ~66 observations | **VERIFIED (repo)**, keyless. Annual only — a backdrop, never a daily state |
| ALFRED (vintage macro) | `https://alfred.stlouisfed.org/graph/alfredgraph.csv?id=&vintage_date=` | true macro vintages | **FAILED (repo)** — five attempts, TLS connects then zero bytes, `last_http_status: 0`. Both `fred`/`alfred` hosts unreachable from this box; robots never obtained, so **no crawl of those hosts is authorised by the register** |
| MoSPI (CPI/IIP source) | `https://www.mospi.gov.in/` | official CPI/WPI/IIP releases | reachable; **serves no robots.txt** — `/robots.txt` returns 200 with 2,657 B byte-identical to `/` (soft 404). VERIFIED (repo) |
| **MCA21** | `https://www.mca.gov.in/mcafoportal/` | filed annual returns, balance sheets, charges | **BELIEVED**: per-document **pay-per-view** (order of INR 100/doc), captcha-gated, no bulk. Unusable for a 20-year panel. Annual-report **PDFs** are better had from company IR sites or BSE/NSE annual-report filings |
| **CRISIL / ICRA / CARE** | `https://www.crisilratings.com/.../rating-rationales`, `https://www.icra.in/Rating/RatingList`, `https://www.careratings.com/rating-rationale` | rating actions + rationales, deep (2000s->), free HTML/PDF | **BELIEVED**. Excellent free credit-deterioration signal; no bulk export, per-issuer HTML |
| **GDELT v2** | `http://data.gdeltproject.org/gdeltv2/lastupdate.txt` -> 15-min export files | global news events, 2015-> | **VERIFIED (repo)**. **plain HTTP only** — the host is a CNAME to `c.storage.googleapis.com`, so HTTPS fails cert validation. Not worked around, recorded |
| GDELT DOC API | `https://api.gdeltproject.org/api/v2/doc/doc?query=...` | article search | **FAILED (repo)** — HTTP 429 on both probes; the 429 body states the policy: "one request every 5 seconds" |

- **Already ingested?** `dataplatform/ingest/gdelt.py:74`, `rss.py:125` (feeds in
  `dataplatform/ingest/data/rss_feeds.yaml`), `macro/index_valuation.py`, World Bank entry in the
  register. Catalogue: **no runner has ever fetched a live headline.**

---

## Q. Broker historical APIs — short, and per-instrument

| Broker | Endpoint | Depth | Constraints |
|---|---|---|---|
| **Zerodha Kite Connect** | `GET /instruments/historical/:instrument_token/:interval` | docs say only *"archived data ... spanning back several years"* — **VERIFIED today that no numeric limit is documented**. Practically ~2015 for equity dailies: **BELIEVED** | Intervals `minute...60minute, day`. Keyed on **`instrument_token`, not symbol or ISIN**, and tokens are reissued — a delisted 2010 scrip has no token, so this is structurally survivorship-biased. INR 2,000/mo/app + INR 2,000/mo historical add-on (**BELIEVED**). Terms 2(e): APIs "not meant for placing fully automated trades" (`EXECUTION_PLAN.md:390`) |
| **Upstox** | `/v3/historical-candle/{key}/{unit}/{interval}/{to}/{from}` | daily to instrument inception for many names — **BELIEVED**, inconsistently so | free with an account; instrument-key based |
| **Fyers** | `/data/history` | ~2017-> for equities; **366 days per request** for daily — **BELIEVED** | free with account |
| **ICICI Breeze** | `get_historical_data_v2` | ~2000-> claimed for NSE dailies — **BELIEVED, and not to be trusted unaudited** | free with an ICICI account |

**None of these is a substitute for the exchange archives**: all are symbol/token-keyed, none
carries delisted instruments, and none is the immutable record. Use them as a cross-check, never as
L0.

- **Already ingested?** Interface only — `execution/kite_broker.py` tested against
  `tests/fixtures/kite/` recorded responses. **No credential exists** (decision B4);
  `dataplatform/config.py:217-232` holds `kite_api_key`/`kite_api_secret` as `SecretStr`,
  defaulting to the stub broker.

---

## R. Yahoo Finance / stooq — fallbacks that failed from this host

- **Yahoo chart API:**
  `https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?period1=0&period2=...&interval=1d`
  — **VERIFIED today: HTTP 429 "Too Many Requests"** (19 B) on a first, cold, single request.
  Unofficial, unstable, rate-limited from cloud IPs.
- **India quality problems (well established, BELIEVED):** `.NS`/`.BO` suffix mapping is
  symbol-based with no ISIN anywhere; adjusted closes handle splits/bonuses tolerably but
  **demergers badly** — which is exactly why this repo uses yfinance only as *reference A* in the
  golden CA suite, network-gated, with a `DISCREPANCIES.md` for the cases it gets wrong
  (`TASK_GRAPH.yaml:659-676`, fixtures in `tests/fixtures/yfinance/` incl.
  `jiofin_demerger_2023.json`, `tatamotors_demerger_2025.json`).
- **stooq:** `https://stooq.com/q/d/l/?s=reliance.in&i=d` — **VERIFIED today: HTTP 200 but a 796 B
  JavaScript browser-verification challenge**, no CSV. India coverage is thin and unadjusted anyway.
- **Verdict:** neither is a viable 20-year India source.

---

## S. Transaction-cost / tax rate history

The repo's dated rate card is `execution/costs/rates.yaml`, and its **earliest schedule is
`effective_from: 2017-07-01`**. By design, "a trade before the first schedule **raises** rather
than borrowing a later rate card" — so **every pre-2017-07-01 backtest is blocked at the cost
model**, not merely approximate. Four schedules exist: `gst-era-state-stamp` (2017-07-01),
`uniform-stamp-duty` (2020-07-01), `true-to-label` (2024-10-01), `current` (2025-04-01,
`provenance: verified`).

To reach 2006 you must add regimes for the three real breaks below:

| Regime | Key rates | Source |
|---|---|---|
| **2004-10-01 -> 2013-05-31** | **STT 0.125% delivery, both sides**; service tax (not GST) on brokerage, rising 10.2% -> 12.36%; **state-wise stamp duty on both sides** (~0.01% in most states) | STT introduced by Finance (No.2) Act 2004 at 0.125% delivery — **BELIEVED**, corroborated by Wikipedia and 5paisa; primary source is the Finance Act notification, not read |
| **2013-06-01 -> 2017-06-30** | **STT cut to 0.1% delivery** (Finance Act 2013); service tax 12.36% -> 15% (incl. cesses, 2016-06) | **BELIEVED**, same sources |
| **2017-07-01 ->** | GST 18% replaces service tax; then the four schedules already in `rates.yaml` | **VERIFIED in-repo**; the 2025-04-01 card is `verified` against `https://zerodha.com/charges` read 2026-08-08 |

Also unmodelled: the **COVID-window 50% stamp relaxation** (Jun-2020 -> Mar-2021), capped-stamp
states (Telangana, Haryana) which currently **raise** rather than compute, and the fact that
**zero-brokerage delivery did not exist before ~2010** — a 2006 backtest charging Zerodha's INR 0
delivery brokerage is charging a rate that had not been invented. Percentage brokerage of ~0.3-0.5%
was the market norm; that alone can swallow a momentum edge.

---

# PART 2 — EFFECTIVELY UNOBTAINABLE FREE AT 20-YEAR DEPTH

### 1. Point-in-time fundamentals with restatement history
- **Free depth:** NSE XBRL, **honestly FY2018-19 -> present** (~101,446 filings; FY2016-18 are 100%
  placeholder). That is **8 years, not 20.** Measured, not assumed.
- **Paid:** **CMIE Prowess / Prowess dx** — the standard for Indian corporate financials, 50,000+
  companies, ~1989->, *and it retains original-as-filed alongside restated*. Access model:
  **IP-based institutional subscription** (`prowessiq.cmie.com`), quoted per organisation, not
  publicly listed; `Prowess dx` is the academic delivery designed for bulk download. ·
  **Capitaline / Capitaline Plus** (Capital Market Publishers, `capitaline.com`, AWS-hosted
  `awsone.capitaline.com`) — 20+ years of Indian standalone/consolidated financials with
  restatement, subscription, quoted. · **LSEG/Refinitiv Point-in-Time Fundamentals** and
  **Bloomberg CoFi PIT** — genuine as-first-reported vintages, enterprise contracts (order of
  US$20k+/yr).
- **Reconstruction:** none. You cannot rebuild pre-2018 as-first-reported statements from any free
  Indian archive. Announcement *text* from 2006 tells you *when* a result was disseminated, not
  *what the numbers were* in a parseable form; extracting them means OCR-and-LLM over ~150,000
  unstructured filings and would need its own validation regime.
- **Accept-the-limitation caveat:** *any fundamentals-driven strategy is honest only from FY2019
  forward. A 2006-2018 fundamentals backtest is not a shallow-data backtest; it is a
  look-ahead-biased one*, because the only pre-2019 fundamentals available (Screener, Tickertape)
  are restated with no as-of date. Invariant #8 / decision #7 already encode this. Keep it.

### 2. Historical index constituent membership
- **Free depth:** one 2026-09-03 snapshot in the lake; `Ix` in the PR zip for ~2010-2011 on a
  rotating subset (VERIFIED); niftyindices publishes **as-of-today only** (VERIFIED repo).
- **Paid:** **NSE Indices (IISL) licensed historical constituent files** — commercial data licence,
  contact `niftyindices.com`, priced per index family. · **Prowess** and **Capitaline** both carry
  index membership history. · Bloomberg/LSEG index membership via terminal or enterprise feed.
- **Reconstruction path (the real one, and it works):** start from today's `ind_{slug}list.csv` and
  walk **backwards** through NSE Indices' semi-annual reconstitution press releases, each of which
  names exactly the securities included and excluded and the effective date. Cross-check every
  reconstruction date against the `ind_close_all` index-count series (30->165) so you never place an
  index before it existed, and against `symbolchange.csv` so a rename is not read as an add/drop.
  Layer `Ix` (2010-2011) in as ground truth where it exists. Cost: a few hundred press releases,
  hand-verified — days of work, and the press-release listing on
  `niftyindices.com/resources/press-release` is a **JS-rendered page** (VERIFIED today: 200/78,516 B
  with no press-release links in the served HTML), so the listing itself needs a browser.
- **Accept-the-limitation caveat:** the repo already states it honestly — `backtest/run.py:2916`:
  *"niftyindices publishes constituents as of today only; the L1 universe is survivorship-biased."*
  Until the reconstruction is done, **do not report index-relative alpha**; report absolute return
  against the TRI (now available 25 years deep) and label the universe as as-of-today.

### 3. Historical sector / industry reclassification
- **Free depth:** each source carries **today's** sector — `ind_{slug}list.csv` `Industry`, BSE
  scrip master `INDUSTRY`, announcements `smIndustry`, `pit_fundamentals` industry tags. None is
  dated. NSE's macro-sector taxonomy itself was overhauled (the 2015 NIFTY renaming coincided with
  sector regrouping), so a 2010 stock's "sector" under today's scheme is an anachronism.
- **Paid:** **Prowess** (NIC-code based, dated) · **Capitaline** · **GICS via LSEG/MSCI** (licensed;
  GICS history is genuinely dated but the licence is expensive and India small-cap coverage is
  partial).
- **Reconstruction:** partial and laborious — the **sectoral index membership** (`Ix` 2010-11, plus
  reconstitution releases) tells you which stocks NSE considered "Bank"/"IT"/"Pharma" on a date, for
  the ~50-200 names in those indices. Nothing recovers the sector of a 2008 micro-cap.
- **Caveat:** sector-rotation and sector-neutral strategies before ~2019 carry an unquantified
  classification-drift bias. State it; don't quietly rank on it.

### 4. Historical free-float and shares-outstanding series
- **Free depth — better than expected, in two disjoint windows:**
  - **Shares outstanding (total):** `mcap<DDMMYYYY>.csv` `Issue Size` — daily, per symbol, from
    **~H1-2024** (VERIFIED absent 2024-01-02, present 2024-07-01).
  - **Index-cap (a free-float proxy for index members):** `Ix<DDMMYY>.csv` `ISSUE_CAP` +
    `WEIGHTAGE` — **2010-2011** only, rotating index subset (VERIFIED).
  - **Face value + paid-up value:** in `EQUITY_L.csv` (snapshot).
  - **Promoter/public split:** NSE shareholding, honestly **~2022 ->** (VERIFIED, §H).
- **Paid:** **Prowess** and **Capitaline** both carry dated shares-outstanding and promoter-holding
  series 20+ years. · NSE Indices licensed free-float factor files.
- **Reconstruction:** for a *listed survivor*, back out shares outstanding from `mcap`'s market cap
  / close today and walk it backwards through the corporate-action factor chain (bonus, split,
  rights) that this repo already builds in `adjustment_factors`. That gives a defensible **total**
  shares series to 2011-06-22 (where ISIN starts) — but **not free float**, because equity issuance
  to promoters/QIBs changes float without a price-adjusting corporate action. For delisted names it
  gives nothing.
- **Caveat:** any strategy weighted by free-float market cap, or screening on promoter-holding
  change, is honest only from ~2022. Total-market-cap weighting is reconstructible back to 2011 with
  stated assumptions.

### 5. Historical bid-ask spread / intraday microstructure
- **Free depth:** **zero, at any horizon.** NSE sells tick and order-book data through its Data
  Services / DOTEX products; nothing free and nothing retrospective. Closest free proxies:
  `NO_OF_TRADES` and `TTL_TRD_QNTY` (bhavcopy, trades back to 2011-06-22, quantity to 1995),
  `AVG_PRICE` (`sec_bhavdata_full`, 2019-09-30 ->), delivery ratio (2002 ->), price-band hits
  (`bh`, 2010 ->).
- **Paid:** **NSE DOTEX / NSE Data & Analytics** historical tick and snapshot files (licensed,
  priced per segment per year, sold by the year and not cheap). · **TrueData**, **GDFL (Global
  Datafeeds)**, **AlgoTest** resell Indian intraday history at roughly INR 1,000-10,000/mo for
  minute bars, less for tick.
- **Reconstruction:** not possible. There is no path from EOD data to a spread.
- **Caveat — the one that most often invalidates an Indian small-cap backtest:** with no spread
  series, slippage must be **modelled**, and the model must be conservative enough that the
  strategy's edge survives being wrong. This repo does the right thing structurally
  (`EXECUTION_PLAN.md:267`) — but for 2006-2010 small caps, where daily turnover was often a few
  lakh rupees, an EOD-only backtest **cannot** tell you whether the position was fillable. Enforce a
  hard turnover floor and treat sub-floor names as untradeable rather than
  tradeable-with-slippage.

### 6. Survivorship-complete delisted price *and* fundamental history
- **Free depth — prices: genuinely good.** L0 immutability saves you: a legacy bhavcopy from 2006
  contains every security that traded that day, **including the ones that no longer exist**. So a
  20-year survivorship-complete *price* panel **is** free — you just have to fetch the sessions
  rather than query a "current universe" API. Two catches: (a) below 2011-06-22 those rows carry
  **no ISIN**, only a symbol that may since have been reassigned; (b) `mcap`'s `Last Trade Date`
  (2024 ->) and BSE's `status=Delisted` scrip master give you a delisting register only for the
  recent era.
- **Free depth — fundamentals: nil.** A company delisted in 2012 filed nothing in XBRL and is
  absent from Screener.
- **Paid:** **Prowess** (keeps dead companies — its main selling point for survivorship work) ·
  **Capitaline** · LSEG/Bloomberg with dead-security coverage.
- **Reconstruction for the ISIN gap (1995 -> 2011-06-21):** build a **symbol->ISIN map as of each
  date** from the accumulated `EQUITY_L.csv` + `symbolchange.csv` + BSE scrip master (Active u
  Suspended u Delisted, which does carry ISIN) + the 2011-06-22-onward bhavcopies' own symbol<->ISIN
  pairs, then walk symbols backwards through the rename chain. Where the chain is ambiguous (a
  symbol reused by an unrelated company — common in the 1990s), **refuse the row** rather than
  guess. `_walk_chain` in `dataplatform/identity/ingest.py` is the right machinery; it needs the
  accumulated snapshots it does not have.
- **Caveat:** expect a real unresolved tail below 2011. Quantify it (rows refused / rows total per
  year) and publish it beside any long-horizon result. Precedent: 1,799,849 delivery rows are
  currently quarantined rather than joined on a guess.

---

# PART 3 — RECOMMENDED ACQUISITION SEQUENCE

Assumptions: ~250 NSE sessions/year; **2006-01-01 -> 2026-09-04 ~ 5,130 sessions**; 2.6 s/request
per host (the register's `min_spacing_seconds` is 2.5); the lake already holds NSE prices
2016-09-02 -> 2026-09-01 and BSE UDiFF 2024-07 ->.

**Governance, before anything:** `AGENTIC_CONTEXT.md:88` reserves *"any bulk-fetch campaign over
~200 requests to one source"* to the human. **Every campaign below except W0 and W8 crosses that
line and needs owner sign-off.** And per `CLAUDE.md`, one campaign at a time on this box: `uptime`
and `ps aux | grep -E 'backfill|campaign'` first, `nohup ... &` with a dated log under
`~/campaign/`.

### Wave 0 — free, tiny, unblocks everything (~5 requests, minutes)
| Step | Requests | Why first |
|---|---|---|
| **NIFTY 50 / NIFTY IT / NIFTY CPSE TRI + price history, full depth** | **3-6** | 25 years of benchmark in one POST each. Nothing downstream can be scored honestly without it. Fix `source_register.yaml:731` and `indices.py:653` at the same time |
| **Extend `dataplatform/ingest/data/nse_holidays.yaml` to 2006** | 0 (desk) | `expected_sessions` **refuses** ranges outside `coverage`, so no pre-2016 gap report can run |
| **Add pre-2017 cost regimes to `execution/costs/rates.yaml`** | 0 (desk) | the cost model **raises** below 2017-07-01 |

### Wave 1 — the price spine (~2,670 requests, ~2 h)
NSE legacy bhavcopy **2006-01-01 -> 2016-09-01**. ~50 KB avg -> **~135 MB**. Handle era E1 (11-col)
by **quarantining** rather than refusing the session. As a by-product this gives the exact trading
calendar (404 = holiday). *Optional extension:* 1995-01-02 -> 2005-12-31 adds ~2,700 requests /
~2 h and 0% more ISIN coverage.

### Wave 2 — the PR bundle (~4,175 requests, ~3 h, ~1.5 GB)
`PR{DDMMYY}.zip`, **2010-01-04 -> 2026-09-04**. One campaign yields seven datasets the repo has zero
of: dated CAs (`Bc`), board meetings (`Bm`), announcements (`An`), band hits (`bh`), 52-week levels
(`Pd`/`Pr`), shares outstanding (`mcap`), index constituents with weights (`Ix`). **Highest
information-per-request in the whole atlas.**

### Wave 3 — delivery (~3,440 requests, ~2.5 h)
MTO **2006-01-01 -> 2019-09-27**. First extend `mto.py` for era M4 (segment repeated settlement
blocks) and decide era M1's fate. Optional extension to 2002-01-02 adds ~1,000 requests.

### Wave 4 — identity, and the pre-2011 ISIN gap (~250 requests, ~30 min + desk work)
BSE scrip master x3 statuses; `EQUITY_L.csv` + `symbolchange.csv`; per-scrip BSE CAs for the
resolution set. **Publish the unresolved-row count per year** — that number is the honest bound on
how far back the platform can claim to reach.

### Wave 5 — CA reconciliation and adjustment factors (~250 requests, ~20 min)
NSE CA API in yearly windows 2006-2026 (~21 requests) as the ISIN-native spine; **`Bc` from Wave 2
as the broadcast-date source**; BSE CA per scrip as reference B. First fix
`corp_actions.py:191-192` — `clock.now().date()` must become an explicit UNKNOWN/quarantine.

### Wave 6 — index levels and constituents (~3,475 requests, ~2.5 h, ~52 MB)
`ind_close_all` **2012-10-01 -> 2026-09-04**. Map the 2015-11 rename through `index_aliases.yaml`
**before** loading. Then start the monthly constituent snapshot job (never fired) and begin the
backwards press-release reconstruction as desk work.

### Wave 7 — announcements (~250 requests, ~20 min, ~250 MB)
Monthly windows 2006-01 -> 2026-09. Twenty years of ISIN-tagged, timestamped disclosure. Store
`an_dt` as first-knowable and flag the pre-2007 midnight-only era.

### Wave 8 — everything with no history to fetch (start today, ~20 requests/day forever)
FII/DII, bulk/block deals, ASM, GSM, `sec_list` bands, index constituents, `EQUITY_L`. None has a
past. **Every day this repo runs no scheduler is a day of history destroyed** — the catalogue says
`Scheduler runs: 0`. Highest-urgency, lowest-cost item, and it needs no owner sign-off.

### Wave 9 — optional / later
F&O legacy 2006 -> 2024-07 (~4,620 requests, ~3.3 h). BSE legacy bhavcopy 2006-04 -> 2024-07
(~4,575 requests, ~3.3 h, ~435 MB) — needs Wave 4 first (no ISIN in any BSE legacy era), and
remember the 200-with-HTML soft 404. AMFI (resolve the categorization URL by hand; NAV history needs
an ASP.NET postback). SEBI/RBI/ratings as desk-cadence pulls.

**Total for Waves 0-7: ~14,500 requests, ~11 hours of wall clock at policy spacing, ~2 GB of L0.**

---

# PART 4 — WHAT THESE PROBES ESTABLISH AGAINST THIS REPO

1. **`source_register.yaml:731` — `nifty_tri_history: FAILED` is wrong.** The corrected path works:
   6,321 rows to 2001-04-02, one POST, no cookie. `HUMAN_DECISIONS.md:225-260` records the
   correction; the register, `data-catalogue-2026-09-06.md` §4 and `backtest/run.py:188,1692,2165`
   still say blocked/proxy.
2. **`dataplatform/ingest/indices.py:653` expects `{"d": "<json-string>"}`.** Today's response is a
   **bare JSON array** with `Content-Type: text/html`.
3. **`source_register.yaml:306-308` — MTO depth understated by 14 years.** Serves **2002-01-02**.
   Three format eras undocumented; `mto.py:118` refuses era M1; nothing segments era M4.
4. **`source_register.yaml:233,244` — legacy bhavcopy era boundary is now exactly knowable:**
   14-column/ISIN era starts **2011-06-22**.
5. **`dataplatform/ingest/nse/corp_actions.py:191-192` — PIT hazard at 20-year scale.**
   `caBroadcastDate` null in every dated-window response (2001/2006/2013/**2026**); fallback is
   `clock.now().date()`.
6. **`sec_bhavdata_full` returns 200-with-the-previous-session on a holiday.** `..._26012026.csv`
   -> 200, 3,102 rows, all dated `23-Jan-2026`.
7. **`nse_holidays.yaml` coverage is 2016-01-01 -> 2026-12-31**, and `calendar.py` refuses outside
   it. Hard blocker on any pre-2016 backfill.
8. **`execution/costs/rates.yaml` earliest schedule is 2017-07-01**, and a pre-first-schedule trade
   raises. Two regimes missing.
9. **NSE shareholding is retro-loaded**: Q1-2018 -> 18 records with `broadcastDate` in 2022;
   Q1-2021 -> 49 with 2022-2024; Q1-2023 -> 2,020. Usable ~2022 forward only.
10. **`bse_bhavcopy_legacy` `era.start: null` should read ~2006-04.**
11. **Four datasets with 20-year depth are wired to nothing:** the `PR` bundle
    (`mcap`/`Ix`/`Bc`/`Bm`/`An`/`bh`) — absent from the register entirely; legacy F&O bhavcopy
    (2006, verified); ASM/GSM/`sec_list`; AMFI (no register row, while 593 `INF*` fund ISINs sit
    unresolvable in D2).

---

## Open items not pinned

- **AMFI's current categorization-of-stocks URL** — the documented path 404s and the site is an SPA
  whose nav is not in the served HTML. One manual browser visit settles it.
- **RBI DBIE's real download endpoints and depth** — JS-only, nothing fetchable.

---

# ADDENDUM (same session, later probes) — Bc / Ix / mcap pinned exactly

23 further spaced probes against `nsearchives.nseindia.com`, 2026-09-07.

## Bc<DDMMYY>.csv — a rolling forward window, and therefore a real PIT source

Consecutive-session diff on (SERIES, SYMBOL, EX_DT, PURPOSE):

| | rows | shared with prev | new | dropped |
|---|---|---|---|---|
| 2010-01-05 | 37 | — | — | — |
| 2010-01-06 | 48 | 37 | +11 | 0 |
| 2010-01-07 | 52 | 44 | +8 | 4 |
| 2010-01-08 | 66 | 50 | +16 | 2 |

`Bc` is a **rolling list of pending corporate actions**, not a "announced today" list. Actions
persist day to day and drop off as their book-closure window passes. Therefore the **first session
on which an action appears is an observed upper bound on its knowable date.**

Lead time of the 16 actions new on 2010-01-08 (EX_DT minus file date): **min 17, median 27, max 42
days.** So first-appearance in `Bc` lands roughly **2.5-6 weeks before ex-date** — a conservative
but genuinely useful PIT bound.

Present in **every** PR zip probed, 2010-01-04 -> 2026-09-04. Date format `DD/MM/YYYY` through
2019, `YYYY-MM-DD` by 2026.

## Ix<DDMMYY>.csv — range is ~9 months, not multi-year

Present: 2010-01-04, 05, 06, 07, 08, 14, 18; 02-01; 04-01; 06-01; 07-01; 08-02; 09-01; 10-01;
**10-08**.
Absent: **2010-10-11**, 10-13, 10-15, 10-29, 11-01, 12-01, 2011-01-03, 04-01, 07-01, and every
later PR zip.

**Range: 2010-01-04 -> 2010-10-08 (VERIFIED both ends).** ~190 sessions.

Index set changes by month, not by day (stable across a week):
- Jan-Feb 2010: `BANK Nifty, CNX 500, CNX IT, CNX Midcap, Nifty Midcap 50` (697 rows)
- Apr-Jul 2010: `CNX 500` only
- Aug-Oct 2010: `CNX 500, CNX Infrastructure`

## mcap<DDMMYYYY>.csv — daily, from 2024-02-01, currently-listed only

Absent 2024-01-02, 2024-01-15, **2024-01-31**. Present **2024-02-01** and every later date probed
including mid-month (2024-09-17, 2025-03-12) -> **daily, starts 2024-02-01**.

`Category` is only `Listed` / `Permitted` — **there is no `Delisted` category**; a delisted name
simply stops appearing. Row counts 2,236 (Feb-2024) -> 3,168 (Sep-2026); `Permitted` grows 4 -> 255.
Three trailing rows per file have blank Category and zero close — skip them.

`Last Trade Date` is the **last actual trade**, an illiquidity marker, *not* a delisting date:
`3RDROCK` is `Listed` on 2024-02-01 with LTD `13 SEP 2023`; one security still listed in 2024 last
traded `10 MAR 2017`; the literal string `Not Traded` also appears. 49-114 rows per file lag the
trade date.

## PR archive has real holes

`PR150110.zip` (2010-01-15) is **404** while `cm15JAN2010bhav.csv.zip` is **200 / 40,379 B** — a
genuine trading session with no PR bundle. 2010-01-14 and 2010-01-18 are both 200. So the PR
archive is not session-complete, which widens any `Bc` first-appearance bound by the length of each
gap.
