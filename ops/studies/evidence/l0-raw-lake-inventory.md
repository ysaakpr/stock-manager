# INVENTORY OF THE L0 RAW LAKE — `data/L0`

**Repo:** `/home/ubuntu/stock-manager` · **As of:** 2026-09-07 · **Read-only; nothing in the repo was modified.**

> **Two corrections to the first delivery of this report, applied throughout:**
> 1. `source_register.yaml` has **30** registered sources, not 28 (`grep -c "^  - id:"` → 30). 13 are materialized, so **17** are never-materialized, not 15.
> 2. §3.1 originally left the NSE pre-ISIN boundary as UNKNOWN. It has since been **pinned by probe to 2011-06-22** — see §9 (Addendum B).

## Headline

| | |
|---|---|
| Payload files | **96,097** |
| Sidecar receipts (`.meta.json`) | **96,097** (1:1, zero orphans) |
| Payload bytes | **6,728,865,158** = 6.27 GiB apparent / 6.9 G on disk |
| Distinct sources materialized | **13** (of 30 registered) |
| Checksum verification | **96,097 checked, 0 defects** |
| **True lake start** | **2016-09-01** (BSE) / **2016-09-02** (NSE) |

> **The 20-year premise is false.** Nothing in this lake reaches 2006. Daily price history begins
> **September 2016** — a 10.0-year span, not 20. One isolated 2010 file exists and is unparseable (§3.1).
> The single source with genuine deep history is BSE corporate actions, whose *content* reaches back
> to **2000** (§3.9).

Verification evidence:

```
$ uv run python -c "L0Store(clock=SystemClock()).verify_checksums()"
2026-09-07 18:19:37 [info] l0.verify checked=96097 defects=0 source=None
report: L0VerificationReport  checked=96097 defects=()
```

---

## 1. Physical layout — universal

Every source obeys one scheme, `dataplatform/store/l0.py`:

```
data/L0/<source_id>/<YYYY>/<MM>/<original-filename>
data/L0/<source_id>/<YYYY>/<MM>/<original-filename>.meta.json
```

Partitioning is **by logical month only** — never by symbol, never by quarter. Filenames are kept
exactly as the vendor served them (`dataplatform/ingest/fetcher.py:632`, "The L0 filename a URL
implies: its last path segment, kept exactly as the source names it").

Every receipt carries the same seven fields:

```json
{ "source": "nse_bhavcopy_legacy", "logical_date": "2010-01-04",
  "filename": "cm04JAN2010bhav.csv.zip",
  "sha256": "003b5499fbff00357b057e64402c682774063f68de49d75ad3ce4483ade76dc3",
  "size_bytes": 39269, "fetched_at": "2026-08-08T19:30:48.189817+05:30",
  "content_type": "application/zip" }
```

---

## 2. Immutability and provenance — L0 invariant #1

**Holds, and is enforced in three independent layers.**

| Mechanism | Evidence |
|---|---|
| Files created mode `0o444` | `dataplatform/store/l0.py:71` `_READ_ONLY: Final = 0o444`; `l0.py:12` "created mode `0o444`, so a later bug (or a careless shell) has to escalate before it" — verified on disk: `stat -c '%a'` → `444` on every sampled payload |
| Write refuses to overwrite | `l0.py:505` "Create `path` containing `data`, or report False because it already exists… never reach the bytes of a file that already exists" |
| Differing bytes at same key is an error | `l0.py:431-435` — "already holds sha256 … refusing to store different content … L0 is [append-only]" |
| Per-file checksum receipt | 96,097 sidecars, 1:1, all parseable |
| Read-back re-verifies | `l0.py:260-276` `get()` re-hashes and raises on mismatch |
| Auditor exists and repairs nothing | `l0.py:330` `verify_checksums` — "repairs nothing, by design (§3.10)" |

There is **no global manifest file**; provenance is per-payload sidecars plus
`dataplatform/ingest/source_register.yaml` (1,557 lines, 30 registered sources with `verified_url`,
`cadence`, `era`).

---

## 3. Source-by-source inventory

Ranked by size. `Files` = payloads (sidecars excluded).

| # | Source | Files | MiB | % | Format | Earliest | Latest | Distinct dates |
|---|---|---|---|---|---|---|---|---|
| 1 | `nse_xbrl_filing` | 81,752 | 4,936.7 | 76.9% | xml | 2018-05-21 | 2026-09-05 | 2,020 |
| 2 | `nse_sec_bhavdata_full` | 1,712 | 450.9 | 7.0% | csv | 2019-10-01 | 2026-09-01 | 1,712 |
| 3 | `bse_bhavcopy_udiff` | 536 | 407.4 | 6.3% | csv | 2024-07-08 | 2026-09-04 | 536 |
| 4 | `bse_bhavcopy_legacy` | 1,939 | 195.4 | 3.0% | zip | 2016-09-01 | 2024-07-05 | 1,939 |
| 5 | `nse_bhavcopy_legacy` | 1,940 | 141.4 | 2.2% | zip | 2010-01-04 | 2024-07-05 | 1,940 |
| 6 | `nse_financial_results_index` | 92 | 109.2 | 1.7% | json | 2016-04-01 | 2026-07-01 | 42 |
| 7 | `nse_bhavcopy_udiff` | 533 | 90.1 | 1.4% | zip | 2024-07-08 | 2026-09-01 | 533 |
| 8 | `nse_mto` | 759 | 45.3 | 0.7% | DAT | 2016-09-02 | 2019-09-30 | 759 |
| 9 | `nse_integrated_filing_index` | 114 | 19.3 | 0.3% | json | 2025-03-01 | 2026-09-01 | 19 |
| 10 | `bse_corp_actions` | 6,689 | 10.9 | 0.2% | json | 2016-09-01 | 2026-09-06 | 2 |
| 11 | `nse_corp_actions` | 11 | 6.7 | 0.1% | json | 2016-09-01 | 2026-09-01 | 11 |
| 12 | `bse_scrip_master` | 3 | 3.6 | 0.1% | json | 2026-09-06 | 2026-09-06 | 1 |
| 13 | `nifty_index_constituents` | 17 | 0.1 | 0.0% | csv | 2026-09-03 | 2026-09-03 | 1 |

### 3.1 `nse_bhavcopy_legacy` — NSE EOD equity bhavcopy, pre-UDiFF era

- **Vendor:** NSE, `https://nsearchives.nseindia.com/content/historical/EQUITIES/<YYYY>/<MON>/cm<DDMONYYYY>bhav.csv.zip` (`source_register.yaml:225-233`, `cadence: backfill_only`, `era: {start: null, end: 2024-07-08}`)
- **Adapter:** `dataplatform/ingest/nse/bhavcopy_legacy.py:61` (`LEGACY_SOURCE_ID`); fetch request built at `dataplatform/ingest/backfill.py:177` `_bhavcopy_request`, which selects legacy vs udiff at `backfill.py:186-194`
- **Naming:** `cm02SEP2016bhav.csv.zip` → single member `cm02SEP2016bhav.csv`
- **Schema (2016→2024, stable):** `SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,` (13 named fields; the trailing comma produces a 14th empty field and is part of the format — `bhavcopy_legacy.py:273-295` counts it deliberately)
- **⚠️ Schema era break:** the single 2010 file has **11 named columns and no ISIN**:
  `SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,`
  The current parser **refuses it by design** — `bhavcopy_legacy.py:288-292`: *"Files from the older
  pre-ISIN sub-era of the archive … are refused on purpose"*. So
  `data/L0/nse_bhavcopy_legacy/2010/01/cm04JAN2010bhav.csv.zip` is a **reachability probe, not usable
  history**. Confirmed empirically: the universe scan found **0 ISINs** in 2010 vs 2,009 in 2016.
  **The exact cutover date is now pinned — see §9.**
- **Real coverage:** 2016-09-02 → 2024-07-05, 1,939 usable dates + 1 unusable 2010 file.

### 3.2 `nse_bhavcopy_udiff` — NSE EOD, UDiFF era

- **Vendor:** NSE, `.../content/cm/BhavCopy_NSE_CM_0_0_0_<YYYYMMDD>_F_0000.csv.zip` (`source_register.yaml:264-272`, `cadence: daily`, `era.start: 2024-07-08`)
- **Adapter:** `dataplatform/ingest/nse/bhavcopy_udiff.py:75`
- **Schema (34 cols):** `TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1..4`
- **Era splice is exact and seamless:** legacy last = 2024-07-05, udiff first = 2024-07-08 (the next session). Register encodes it as two touching half-open ranges — `dataplatform/ingest/nse/bhavcopy.py:11-12`.

### 3.3 NSE bhavcopy — combined coverage and gaps

Measured against the repo's own trading calendar (`dataplatform/ingest/calendar.py:298`
`expected_data_dates` = sessions **plus** Muhurat, backed by
`dataplatform/ingest/data/nse_holidays.yaml`, coverage 2016-01-01..2026-12-31). **This is the
exchange's real calendar, not a weekday approximation.**

```
span 2016-01-01 .. 2026-09-01   have=2472  expected=2637   MISSING=165  UNEXPECTED=0
  2016: 165 missing of 247   ( 33.2% present)  <- all before 2016-09-02; lake had not begun
  2017:   0 missing of 248   (100.0%)
  2018:   0 missing of 246   (100.0%)
  2019:   0 missing of 245   (100.0%)
  2020:   0 missing of 251   (100.0%)
  2021:   0 missing of 248   (100.0%)
  2022:   0 missing of 248   (100.0%)
  2023:   0 missing of 246   (100.0%)
  2024:   0 missing of 246   (100.0%)
  2025:   0 missing of 248   (100.0%)
  2026:   0 missing of 164   (100.0%)
```

**Zero interior gaps.** All 165 "missing" dates are 2016-01-04 … 2016-09-01 — the head of the
calendar's coverage window, before fetching began. From **2016-09-02 onward the NSE daily price
series is 100% complete with no holes**, and contains no unexpected dates (no phantom files on
closed days — Muhurat handled correctly).

### 3.4 `bse_bhavcopy_legacy` + `bse_bhavcopy_udiff` — BSE EOD equity

- **Vendors:** `https://www.bseindia.com/download/BhavCopy/Equity/EQ<DDMMYY>_CSV.ZIP` (`source_register.yaml:413-421`) and `.../BhavCopy_BSE_CM_0_0_0_<YYYYMMDD>_F_0000.CSV` (`:382-390`)
- **Adapter:** `dataplatform/ingest/bse/bhavcopy.py:81-82` (both ids); requests at `backfill.py:235` and `backfill.py:291`
- **Legacy schema (14 cols, stable 2016→2024, no ISIN):** `SC_CODE,SC_NAME,SC_GROUP,SC_TYPE,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,NO_TRADES,NO_OF_SHRS,NET_TURNOV,TDCLOINDI` — joins on **BSE scrip code, not ISIN**; resolution requires `bse_scrip_master` (§3.11). `bse/bhavcopy.py:21` also flags this era has **no timestamp column**, a PIT hazard.
- **UDiFF schema:** byte-identical header to NSE UDiFF (`bse/bhavcopy.py:93`), and *does* carry ISIN.
- **Gaps — perfect:**

```
span 2016-09-01 .. 2026-09-04   have=2475  expected=2475   MISSING=0  UNEXPECTED=0
  2016..2026: 0 missing every year (82,248,246,245,251,248,248,246,246,248,167)
```

**BSE daily prices are 100.0% complete over their entire span with zero missing sessions.**

### 3.5 `nse_mto` — NSE delivery, legacy MTO era

- **Vendor:** NSE, `.../archives/equities/mto/MTO_<DDMMYYYY>.DAT` (`source_register.yaml:297-309`)
- **Adapter:** `dataplatform/ingest/nse/mto.py:57`
- **Format:** fixed-preamble `.DAT`, record-type-prefixed. Schema drift observed between eras: 2016
  line 3 is `Trade Date <02-SEP-2016>,Settlement Type <N>,Settlement No <2016167>,Settlement Date <07-SEP-2016>`;
  2019 line 3 is `Trade Date <30-SEP-2019>,Settlement Type <N>` — **settlement number and date
  dropped**. Data rows: `20,<srno>,<symbol>,<series>,<qty traded>,<deliverable qty>,<% deliv>`.
- **Gaps:** `2016-09-02 .. 2019-09-30, have=759, expected=759, MISSING=0` — 100.0% complete.

### 3.6 `nse_sec_bhavdata_full` — NSE delivery, modern era

- **Vendor:** NSE, `.../products/content/sec_bhavdata_full_<DDMMYYYY>.csv` (`source_register.yaml:338-354`, `era.start: 2019-09-30`)
- **Adapter:** `dataplatform/ingest/nse/delivery.py:72`
- **Schema (15 cols, leading spaces are real):** `SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER`
- **Gaps:** `2019-10-01 .. 2026-09-01, have=1712, expected=1712, MISSING=0` — 100.0% complete.

**The delivery splice is exact.** MTO ends **2019-09-30**; sec_bhavdata begins **2019-10-01** — the
next session. Zero overlap, zero gap. Combined delivery coverage **2016-09-02 → 2026-09-01, 2,471
sessions, no holes**. `dataplatform/quality/gaps.py:402` treats them as one family:
`"nse_delivery": ("nse_mto", "nse_sec_bhavdata_full")`.

### 3.7 `nse_xbrl_filing` — company financial filings (77% of the lake)

- **Vendor:** NSE archives, `https://nsearchives.nseindia.com/corporate/xbrl/<NAME>.xml` (`source_register.yaml:1379-1387`, `cadence: per_filing`)
- **Adapter/parser:** `dataplatform/ingest/xbrl/parser.py:87`; driver `dataplatform/ingest/fundamentals_backfill.py:137` (each filing resumable under `nse_xbrl_filing/<filing_id>`)
- **Partitioning:** by filing date month, **not** by company. 81,752 XML documents over 2,020 distinct dates, **2018-05-21 → 2026-09-05**.
- **Taxonomy families (from filename prefix, and these are genuine schema eras):**

| Prefix | Count |
|---|---|
| `INDAS` | 52,278 |
| `INTEGRATED_FILING_INDAS` | 23,346 |
| `NBFC_INDAS` | 2,818 |
| `INTEGRATED_FILING_NBFC_INDAS` | 1,667 |
| `BANKING` | 692 |
| `INTEGRATED_FILING_BANKING` | 424 |
| `NONINDAS` | 345 |
| `INTEGRATED_FILING_NONINDAS` | 78 |
| `INTEGRATED_FILING_LI` / `_GI` | 53 / 50 |

- Root namespace is BSE's XBRL taxonomy even for NSE-served files:
  `xmlns:in-bse-fin="http://www.bseindia.com/xbrl/fin/2018-03-31/in-bse-fin"`. Parser normalises
  schemes at `xbrl/parser.py:141`.
- **Gaps:** not a daily source — calendar gap analysis is **not applicable**. Completeness would have
  to be judged against the discovery index (§3.8), not the calendar.
- **Distinct issuers: UNKNOWN.** Filenames carry sequence ids, not ISINs. Determining it requires
  parsing all 81,752 XMLs for their entity identifier, or joining the `filing_id`s against the index
  in §3.8 (which knows 2,476 ISINs).

### 3.8 `nse_financial_results_index` + `nse_integrated_filing_index` — filing discovery

- **Vendors:** `https://www.nseindia.com/api/corporates-financial-results?index=equities&period=<Quarterly|Annual>` (`source_register.yaml:1268-1278`) and `.../api/integrated-filing-results?...` (`:1324-1335`, `era.start: 2025-04-01`)
- **Adapters:** `dataplatform/ingest/xbrl/discovery.py:61` and `dataplatform/ingest/xbrl/integrated.py:68`
- **Partitioning:** by `(period, date-chunk)` — e.g. `corporates-financial-results_Annual_20160401_20160630.json`. 92 files = **46 Annual + 46 Quarterly** chunks; 42 distinct logical dates, 2016-04-01 → 2026-07-01.
- **Content:** **148,984 index entries, 2,476 distinct ISINs.** Records carry `isin`, `companyName`,
  `period`, `indAs`, `format` ("Old"/new — a declared format-era flag), `filingDate`,
  `resultDetailedDataLink`.
- **filingDate histogram** — note the deliberate handover:

```
2016:10421 2017:14625 2018:15268 2019:23497 2020:17225 2021:14448
2022:14926 2023:16491 2024:17886 2025: 4129 2026:   68
```

The 2025 collapse is **not a gap** — it is the SEBI Integrated Filing regime taking over from
2025-04-01 (`source_register.yaml:1335`). `nse_integrated_filing_index` picks it up: 114 files, 19
dates, 2025-03-01 → 2026-09-01, **26,610 entries**, paginated (`_p01`…`_p06`).

- ⚠️ Some index pages are legitimately empty:
  `nse_integrated_filing_index/2025/03/...p01.json` = `{"data":[],"size":1000,"page":0,"totalCount":0}`
  (March 2025 predates the regime).

### 3.9 `bse_corp_actions` — **the only genuinely deep source**

- **Vendor:** `https://api.bseindia.com/BseIndiaAPI/api/DefaultData/w?...` (`source_register.yaml:508-516`)
- **Adapter:** `dataplatform/ingest/bse/corp_actions.py:58`; driver `dataplatform/ingest/corp_actions_backfill.py:119` — tracked per scrip under `bse_corp_actions/<scrip>`
- **Partitioning: by scrip, not by date.** 6,689 files named `DefaultData_<scripcode>.json`, under only
  **2** logical dates (the fetch dates 2016-09-01 and 2026-09-06) — the `logical_date` here is *when we
  fetched*, not what the data covers.
- **Content coverage is the real number:** **38,105 records across 3,790 distinct BSE scrip codes**,
  with ex-dates spanning **2000 → 2026**:

```
2000: 147  2001: 931  2002: 395  2003: 214  2004: 209  2005: 247  2006: 236
2007:1220  2008:1600  2009:1442  2010:1757  2011:1693  2012:1594  2013:1659
2014:1636  2015:1574  2016:1636  2017:1702  2018:1769  2019:1772  2020:1472
2021:1858  2022:2161  2023:2220  2024:2523  2025:2446  2026:1992
```

- ⚠️ **2,894 of 6,689 files (43%) are empty JSON arrays** — scrips with no corporate-action history.
  Structurally valid, but any completeness metric must exclude them.
- **Schema:** `scrip_code, short_name, Ex_date, Purpose, RD_Date, BCRD_FROM, BCRD_TO, ND_START_DATE,
  ND_END_DATE, payment_date, exdate (YYYYMMDD), long_name`. Note the two date encodings
  (`Ex_date` = `"17 May 2001"`, `exdate` = `"20010517"`).

### 3.10 `nse_corp_actions`

- **Vendor:** `https://www.nseindia.com/api/corporates-corporateActions?index=equities` (`source_register.yaml:449-457`)
- **Adapter:** `dataplatform/ingest/nse/corp_actions.py:68`; chunked backfill at `corp_actions_backfill.py:270`
- **Partitioning: by yearly range window.** 11 files, `corporateActions_<YYYYMMDD>_<YYYYMMDD>.json`,
  spanning **2016-09-01 → 2026-09-30** in ten 1-year chunks plus a partial current month.
  `quality/gaps.py:415` correctly exempts this from daily gap logic via `_RANGE_FETCHED_SOURCES`.
- **Content:** **22,838 records, 2,640 distinct ISINs**; exDate histogram 2016 (1,010) → 2026 (1,421), continuous.
- **Schema:** `bcEndDate, bcStartDate, caBroadcastDate, comp, exDate, faceVal, ind, isin, ndEndDate,
  ndStartDate, recDate, series, subject, symbol` — **carries ISIN**, unlike the BSE feed.

### 3.11 `bse_scrip_master` — the BSE scrip-code → ISIN bridge

- **Vendor:** `https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?...&status=<Active|Suspended|Delisted>` (`source_register.yaml:639-647`)
- **Adapter:** `dataplatform/ingest/bse/scrip_master.py:87`; refresh job `dataplatform/ingest/bse_scrip_refresh.py:50`
- **3 files, one snapshot only, 2026-09-06:** `ListofScripData_Active.json` (1.75 MB),
  `_Delisted.json` (1.58 MB), `_Suspended.json` (0.45 MB)
- **Schema:** `SCRIP_CD, Scrip_Name, Status, GROUP, FACE_VALUE, ISIN_NUMBER, INDUSTRY, scrip_id,
  Segment, NSURL, Issuer_Name, Mktcap`
- ⚠️ **This is a single point-in-time snapshot with no history.** It is the *only* bridge from BSE
  legacy bhavcopy's `SC_CODE` to ISIN (§3.4), so joining 2016-2024 BSE prices to ISIN uses a 2026
  mapping — a live PIT hazard. Credit where due: the delisted file being fetched materially reduces
  the survivorship damage.

### 3.12 `nifty_index_constituents` — **one snapshot, and one corrupt file**

- **Vendor:** `https://niftyindices.com/IndexConstituent/ind_<index>list.csv` (`source_register.yaml:675-683`)
- **Adapter:** `dataplatform/ingest/indices.py:119`; snapshot job `dataplatform/ingest/constituents_snapshot_job.py`
- **17 files, one date only: 2026-09-03** — nifty50, nifty500, and 15 sector indices.
- **Schema:** `Company Name,Industry,Symbol,Series,ISIN Code`
- 🔴 **DEFECT: `ind_niftyprivatebanklist_20260903.csv` is not a CSV.** Its receipt records
  `"content_type": "text/html; charset=utf-8"` and the payload begins `<!DOCTYPE html> <html> <head>…`
  — 78,512 bytes of an error page stored as if it were data. 16 of 17 files are valid; this one is
  poison. The L0 checksum audit cannot catch this (the bytes match their hash — they are just the
  wrong bytes).
- The module itself documents the underlying problem (`indices.py:10`): *"So the history has to be
  *made*: snapshot…"* — **NSE publishes no constituent history, so index membership history does not
  exist and cannot be backfilled.** Every month not snapshotted is permanently lost.

---

## 4. Registered but **ABSENT** from L0 — 17 of 30 sources

Verified by directory existence; none has any `data/L0/<id>` directory. Full metadata in §10.

`nse_equity_list` · `nse_symbol_changes` · `nifty_tri_history` · `nifty_index_close_snapshot` ·
`nse_fii_dii_flows` · `nse_bulk_deals` · `nse_block_deals` · `nse_shareholding_pattern` ·
`nse_fo_bhavcopy` · `nse_announcements` · `bse_announcements` · `gdelt_v2_event_files` ·
`gdelt_doc_api` · `curated_rss` · `screener_company_fundamentals` · `worldbank_indicator_api` ·
`alfred_series_vintage`

Parsers and fixtures exist for most (`tests/fixtures/nse_flows/`, `nse_deals/`, `nse_shareholding/`,
`nse_fo/udiff/`, `screener/2026-08`, `gdelt/v2`, `rss/pib`, `rss/rbi`, `kite`, `yfinance`) — **code
written, campaign never run**. Notably `nifty_tri_history` and `nifty_index_close_snapshot` are
absent, so **there is no benchmark index series in L0** — a gap that matters directly for any
return/drawdown ranking.

**No Yahoo/yfinance, no Zerodha/Kite, no AMFI, no screener data is present in L0** despite fixtures
for the first three.

---

## 5. Instrument universe and survivorship

Derived by decompressing and parsing **all 2,473 NSE bhavcopy files** (legacy `SYMBOL`/`ISIN`; UDiFF
`TckrSymb`/`ISIN` filtered to `FinInstrmTp == STK`). `*_eq` restricts to equity-like series
`{EQ,BE,BZ,SM,ST,MT,IL}`.

| Year | Symbols (all) | ISINs (all) | Symbols (EQ) | **ISINs (EQ)** | New EQ ISINs |
|---|---|---|---|---|---|
| 2010 | 1,309 | **0** | 1,306 | **0** | — |
| 2016 | 1,763 | 2,009 | 1,720 | 1,728 | 1,728 |
| 2017 | 1,939 | 2,259 | 1,856 | 1,870 | 198 |
| 2018 | 2,061 | 2,381 | 1,955 | 1,955 | 166 |
| 2019 | 2,128 | 2,448 | 1,991 | 1,983 | 97 |
| 2020 | 2,198 | 2,463 | 2,015 | 2,009 | 104 |
| 2021 | 2,387 | 2,674 | 2,163 | 2,183 | 246 |
| 2022 | 2,621 | 2,787 | 2,304 | 2,321 | 271 |
| 2023 | 3,141 | 3,101 | 2,579 | 2,565 | 361 |
| 2024 | 4,139 | 3,473 | 2,876 | 2,915 | 506 |
| 2025 | 4,538 | 3,845 | 3,221 | 3,227 | 485 |
| 2026 | 4,900 | 4,213 | 3,572 | 3,587 | 503 |

**Totals across the whole lake: 7,483 distinct symbols, 6,090 distinct ISINs.**

The 2010 row showing **0 ISINs** is the independent confirmation that the pre-2011 bhavcopy format
has no ISIN column (§3.1, §9) — identity cannot be established for that era at all.

### Survivorship decay

Of the **1,870 EQ ISINs trading in 2017**, how many still appear in each later year:

| Year | Survivors | Rate |
|---|---|---|
| 2018 | 1,789 | 95.7% |
| 2019 | 1,722 | 92.1% |
| 2020 | 1,648 | 88.1% |
| 2021 | 1,600 | 85.6% |
| 2022 | 1,508 | 80.6% |
| 2023 | 1,444 | 77.2% |
| 2024 | 1,392 | 74.4% |
| 2025 | 1,330 | 71.1% |
| 2026 | **1,283** | **68.6%** |

**31.4% of the 2017 equity universe is gone by 2026** — roughly 3.7%/year attrition. A backtest built
on the 2026 universe would silently discard 587 instruments that traded in 2017. The delisted names
*are* in L0 (the lake is not survivorship-biased at the raw layer) — the bias would be introduced
downstream by any universe filter drawn from a current-day list, which is exactly what
`bse_scrip_master` (§3.11) and `nifty_index_constituents` (§3.12) are today.

---

## 6. Freshness — the live tail is stale

Today is 2026-09-07; the calendar says 2026-09-02, 09-03, 09-04 were all trading sessions.

| Source | Latest | Sessions behind |
|---|---|---|
| `bse_bhavcopy_udiff` | 2026-09-04 | 0 (current) |
| `nse_xbrl_filing` | 2026-09-05 | — |
| `nse_bhavcopy_udiff` | **2026-09-01** | **3** |
| `nse_sec_bhavdata_full` | **2026-09-01** | **3** |

**NSE prices and delivery are 3 sessions behind BSE.** Not a historical gap — a stalled daily job.

---

## 7. EXPLICIT VERDICT: is 2006–2026 coverage complete?

**No source has 20-year coverage. Not one.**

| Source | Verdict | Actual span | Years |
|---|---|---|---|
| `nse_bhavcopy` (legacy+udiff) | **PARTIAL** | 2016-09-02 → 2026-09-01 | 10.0 |
| `bse_bhavcopy` (legacy+udiff) | **PARTIAL** | 2016-09-01 → 2026-09-04 | 10.0 |
| `nse_delivery` (mto+sec_bhavdata) | **PARTIAL** | 2016-09-02 → 2026-09-01 | 10.0 |
| `bse_corp_actions` | **PARTIAL — deepest** | ex-dates 2000 → 2026 | ~26 (content) |
| `nse_corp_actions` | **PARTIAL** | 2016-09-01 → 2026-09-30 | 10.1 |
| `nse_financial_results_index` | **PARTIAL** | 2016-04-01 → 2026-07-01 | 10.3 |
| `nse_xbrl_filing` | **PARTIAL** | 2018-05-21 → 2026-09-05 | 8.3 |
| `nse_integrated_filing_index` | **PARTIAL** | 2025-03-01 → 2026-09-01 | 1.5 |
| `bse_scrip_master` | **ABSENT (as history)** | single snapshot 2026-09-06 | 0 |
| `nifty_index_constituents` | **ABSENT (as history)** | single snapshot 2026-09-03 | 0 |
| Index levels / TRI (benchmark) | **ABSENT** | — | 0 |
| F&O, flows, deals, shareholding, news, macro | **ABSENT** | — | 0 |

**The binding constraint is 2016-09.** Any strategy study is a **10-year** study, and the "three
windows" mandate must be cut inside 2016-09-02 … 2026-09-01. The good news is that within that
window the daily price and delivery series are **flawless** — 0 missing sessions on either exchange,
0 checksum defects across 96,097 payloads.

**But see §9:** the NSE archive itself can extend this to **15.2 years (2011-06-22 →)** with no new
parser code.

---

## 8. Defects and open questions

**Confirmed defects (2):**
1. 🔴 `data/L0/nifty_index_constituents/2026/09/ind_niftyprivatebanklist_20260903.csv` is an HTML
   error page, not CSV (§3.12). Checksum-clean but semantically poison.
2. 🟡 NSE price + delivery are 3 sessions stale vs BSE (§6).

**Structural risks (not defects):**
3. `bse_scrip_master` is a 2026-only snapshot used to resolve 2016-2024 BSE scrip codes to ISIN — a
   point-in-time violation waiting to happen (§3.11, §9.2).
4. Index membership history is unrecoverable going backwards; only forward snapshotting builds it
   (`indices.py:10`).
5. 43% of `bse_corp_actions` files are empty arrays (§3.9).

**UNKNOWN, and what would settle each:**

| Question | What would determine it |
|---|---|
| Distinct issuers in `nse_xbrl_filing` | Parse the entity identifier from all 81,752 XMLs, or join `filing_id`s to the §3.8 index (2,476 ISINs known there) |
| Is `nse_xbrl_filing` *complete* vs. what was filed? | Reconcile the 81,752 downloaded filings against the 148,984 + 26,610 index entries; the index is the denominator, the calendar is not |
| Are the 2,894 empty `bse_corp_actions` files true negatives or failed fetches? | Re-fetch a sample and compare; the receipts record HTTP 200 + `application/json`, which is suggestive but not proof |
| Why did the NSE daily job stop after 2026-09-01? | `dataplatform/status/sync_state.py` / the campaign logs under `~/campaign/` — not inspected, out of scope for a read-only L0 inventory |

---

## 9. ADDENDUM B — the pre-ISIN problem, pinned

### 9.1 NSE: the ISIN column appears on **2011-06-22**

**What the repo knew:** nothing precise, and it says so.
`ops/BACKLOG.md:47` — *"a deeper history would need its own parser plus a symbol→ISIN resolution
through D2, and **the exact year the `ISIN` column appears is unpinned**."*
`source_register.yaml:242-246` — *"Carries ISIN directly… **but only back to some point in the early
2010s**… `era.start: null` above means 'as far back as the 14-column format goes', not 'all of it'."*
`tests/fixtures/nse_bhavcopy/legacy/PROVENANCE.md:24` — *"The pre-ISIN sub-era is deliberately not
represented here."*

**Pinned by probe** (12 requests to `nsearchives.nseindia.com`, ≥2.5 s spacing, register headers,
nothing written to the repo):

| Session | Named cols | ISIN | Evidence |
|---|---|---|---|
| 2010-01-04 | 11 | NO | in L0 |
| 2010-10-04 | 11 | NO | probe |
| 2011-02-18 | 11 | NO | probe |
| 2011-04-27 | 11 | NO | probe |
| 2011-05-31 | 11 | NO | probe |
| 2011-06-17 | 11 | NO | probe |
| 2011-06-20 | 11 | NO | probe |
| **2011-06-21** | **11** | **NO** | **probe — last session without ISIN** |
| **2011-06-22** | **13** | **YES** | **probe — first session with ISIN** |
| 2011-06-27 | 13 | YES | probe |
| 2011-07-05 | 13 | YES | probe |
| 2013-01-02 | 13 | YES | probe |
| 2016-01-01 | 13 | YES | checked-in fixture |

**A clean single-day cutover: 2011-06-21 → 2011-06-22.**

**And the format that appears is exactly the one already shipped.** Verified by running the
production parser on the live 2011-06-22 payload:

```
header: SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,
LEGACY_COLUMNS match: True
bhavcopy.parsed era=legacy refused=0 rows=1502 state=NORMALIZED trade_date=2011-06-22
sample: isin='INE144J01019' symbol='20MICRONS' ... close=Decimal('46.65') total_trades=107
```

**Consequence: NSE history back to 2011-06-22 requires ZERO new parser code.** The only blocker is
`nse_holidays.yaml`, whose `coverage.start` is 2016-01-01 and which *refuses* rather than guesses
(`calendar.py:316` `CalendarCoverageError`). Extending the calendar to 2011 unlocks **15.2 years**
(2011-06-22 → 2026-09-01) instead of 10.0 — a 52% increase in backtest span for a holiday-file edit
and a backfill run.

One caution surfaced by the probe: `20MICRONS` is `INE144J01019` in 2011 but `INE144J01027` in 2016.
**ISINs themselves change** (face-value splits reissue them), so a 15-year join needs ISIN-lineage
handling (`dataplatform/identity/lineage.py`), not just an ISIN column.

### 9.2 BSE: no ISIN in any legacy row, and the bridge is a today-snapshot

BSE legacy (2016→2024, and everything before) carries **only `SC_CODE`** — confirmed identical
headers at both ends of the span (§3.4). `source_register.yaml:430-432`: *"there is NO ISIN column in
this era — only SC_CODE. Backfilled BSE rows must be resolved to ISIN through the BSE scrip master
(D2) before use; joining on SC_CODE or SC_NAME anywhere downstream breaks invariant #2."*

The bridge is `bse_scrip_master`, and it is **a snapshot, not a series**: 3 files, all
`logical_date = 2026-09-06`, one per `status` value (Active / Suspended / Delisted). `era: {start:
null, end: null}`, `cadence: weekly` — so it is *designed* to accumulate history forward, but only
one week has ever been fetched.

**What a today-snapshot means for a 2008 scrip code:**

1. **Delisted before 2026 → resolvable only if BSE still lists it.** The `Delisted` file (1.58 MB) is
   the saving grace and is why the fetch was done correctly (`source_register.yaml` `pit_notes`:
   *"Delisted and suspended scrips must be pulled with the other status values or the backfill
   silently acquires survivorship bias"*). But BSE's delisted list is BSE's *current* record of past
   delistings; anything it has purged is unrecoverable.
2. **Scrip code reused → silent mis-join.** A retired code reassigned to a different company maps the
   2008 row to the 2026 issuer's ISIN. Nothing in a snapshot can detect this; there is no
   `valid_from`/`valid_to` on the mapping.
3. **ISIN changed since 2008 → wrong ISIN, silently.** The snapshot holds today's ISIN. A company
   that split its face value has a different ISIN now than in 2008, so the 2008 price attaches to the
   post-split identity — exactly the `20MICRONS` case above, and it produces a corrupted adjustment
   chain rather than an error.
4. **Renamed company → cosmetic only.** `SCRIP_CD` is stable across renames, so this one is benign.

The identity master **does** have the right machinery — `dataplatform/identity/master.py:13`
describes windows over `[valid_from, valid_to]` with renames appending rather than overwriting, and
exposes `symbol_as_of`. But it is fed by `nse_equity_list` + `nse_symbol_changes`, **both absent from
L0** (§4, §10). So the PIT apparatus exists and is empty, and BSE has no equivalent history source at
all.

### 9.3 Verdict: what 2006–2016 ingestion actually requires

**NSE, 2011-06-22 → 2016-09-01 (5.2 years) — CHEAP. No new parser.**

| Need | Status |
|---|---|
| Parser era | **none — `bhavcopy_legacy.parse` accepts it, proven on live 2011-06-22 data** |
| Calendar | extend `nse_holidays.yaml` `coverage.start` from 2016-01-01 to 2011; ~5 years of NSE holiday lists, derivable the same way 2016-2025 were (`nse_holidays.yaml` provenance block) |
| Identity | ISIN is native; needs ISIN-lineage for reissues (`identity/lineage.py`) |
| Fetch | ~1,290 sessions, one GET each, existing `backfill.py` path |

**NSE, 2006 → 2011-06-21 (5.5 years) — EXPENSIVE. Identity is the blocker, not the parser.**

1. **New parser era** for the 11-column header (no `TOTALTRADES`, no `ISIN`) — mechanically trivial,
   a second `LEGACY_COLUMNS` tuple and width branch.
2. **A symbol→ISIN resolution path for a period with no ISIN anywhere in the price file.** This is
   the real cost. It needs a point-in-time NSE symbol master covering 2006-2011, which requires
   `nse_equity_list` + `nse_symbol_changes` (both unfetched) *and* those sources to reach back 15+
   years — `nse_symbol_changes` publishes full rename history, but `EQUITY_L.csv` is a **current**
   listing, so companies delisted before today are simply absent. **That is a hard survivorship hole
   the architecture cannot close from NSE alone.**
3. **A corporate-action history for 2006-2011** to build adjustment factors —
   `nse_corp_actions` starts 2016-09-01. `bse_corp_actions` *does* cover 2000-2026 (§3.9) and could
   supply it, but only via BSE scrip codes, which reintroduces problem (2) through the BSE side.
4. **Invariant #2 conflict.** `bhavcopy_legacy.py:288-292` refuses these rows *on purpose* rather than
   emit a null ISIN. Ingesting 2006-2011 means either resolving every symbol to an ISIN as-of its
   session, or amending the invariant. There is no third option that keeps the current design honest.

**BSE, 2006 → 2016 — BLOCKED on identity, regardless of parser.**

1. **No new parser needed** — the legacy header is unchanged from 2016 to 2024 and near-certainly
   further back (UNPINNED: no BSE file before 2016-09-01 has been fetched; one probe of an
   `EQ<DDMMYY>_CSV.ZIP` from 2008 would settle it).
2. **A historical BSE scrip-code→ISIN mapping must exist, and today it does not.** A 2026 snapshot
   cannot honestly resolve a 2008 code (§9.2). Options: accumulate weekly snapshots going forward
   (useless for backfill), or find a vendor with dated BSE scrip masters (none registered).
3. `resolve_legacy` / `scrip_to_isin` (`bse/scrip_master.py:245`) is already written and wired per
   `ops/BACKLOG.md:85` — the code is ready; the *data* is what is missing.

**Bottom line.** A 20-year (2006-2026) backtest is **not possible** in this architecture today, and
the obstacle is identity, not parsing. A **15-year (2011-06-22 → 2026)** NSE-only backtest is
reachable with a holiday-file extension and a backfill run, no new parser, and is by far the highest
return-on-effort move available.

---

## 10. ADDENDUM D — the 17 registered-but-never-materialized sources

`plan_row` is the EXECUTION_PLAN §4.1 row each was scoped to fill. `era` is what the register claims
the source offers; `{start: null, end: null}` means "no declared bound" — an open claim, not a
verified 20-year depth.

| # | Source id | Entity it yields | Status / HTTP | Declared era | Parser (task) | Fixture |
|---|---|---|---|---|---|---|
| 1 | `nse_equity_list` | NSE full equity list — the symbol→ISIN spine of the identity master | **VERIFIED** 200 | null → null | `identity.ingest` (M1.7) | frozen |
| 2 | `nse_symbol_changes` | Every NSE rename, old→new symbol with date — the other half of D2 | **VERIFIED** 200 | null → null | `identity.ingest` (M1.7) | frozen |
| 3 | `nifty_tri_history` | Historical total-return index values per index | **FAILED** 200-but-HTML | null → null | `ingest.indices` (M3.9) | none |
| 4 | `nifty_index_close_snapshot` | Daily closes for all NIFTY indices + Div Yield (computed-TRI input) | **VERIFIED** 200 | null → null | `ingest.indices` (M3.9) | frozen |
| 5 | `nse_fii_dii_flows` | FII/DII buy/sell/net per session | **VERIFIED** 200 | null → null | `nse.fii_dii` (M3.4) | frozen |
| 6 | `nse_bulk_deals` | Bulk deals for the session | **VERIFIED** 200 | null → null | `nse.deals` (M3.5) | frozen |
| 7 | `nse_block_deals` | Block deals for the session | **VERIFIED** 200 | null → null | `nse.deals` (M3.5) | frozen |
| 8 | `nse_shareholding_pattern` | Quarterly shareholding filings, promoter/public splits | **VERIFIED** 200 | null → null | `ingest.shareholding` (M3.6) | not frozen |
| 9 | `nse_fo_bhavcopy` | NSE derivatives EOD (OI, PCR, basis) — sentiment only, never traded | **VERIFIED** 200 | **2024-07-08** → null | `nse.fo_bhavcopy` (M3.7) | not frozen |
| 10 | `nse_announcements` | NSE corporate announcements + attachment links, XBRL flags | **VERIFIED** 200 | null → null | `ingest.announcements` (M3.8) | not frozen |
| 11 | `bse_announcements` | BSE announcements, paged by date | **VERIFIED** 200 | null → null | `ingest.announcements` (M3.8) | not frozen |
| 12 | `gdelt_v2_event_files` | GDELT 2.0 raw 15-min export/mention/GKG files | **VERIFIED** 200 | null → null | `ingest.gdelt` (M6.1) | frozen |
| 13 | `gdelt_doc_api` | GDELT DOC 2.0 article search | **FAILED** 429 | null → null | `ingest.gdelt` (M6.1) | none |
| 14 | `curated_rss` | Curated RSS headlines (RBI active; PIB/business press configured) | **VERIFIED** 200 | null → null | `ingest.rss` (M6.1) | frozen |
| 15 | `screener_company_fundamentals` | Screener per-company restated fundamentals (monitoring-only, quarantined) | **BLOCKED_CREDENTIAL** 404 | null → null | `ingest.screener` (M7.1) | none |
| 16 | `worldbank_indicator_api` | World Bank WDI annual macro for India (CPI, GDP) | **VERIFIED** 200 | null → null | `ingest.macro` (M11.1) | none |
| 17 | `alfred_series_vintage` | ALFRED — FRED series *as published on* a past date (solves macro vintage) | **FAILED** 0 (unreachable) | null → null | `ingest.macro` (M11.1) | none |

**Reading of the table:**

- **12 of 17 are VERIFIED 200 with a parser and (mostly) frozen fixtures** — scoped, coded, merely
  unfetched. Turning any of these on is a campaign, not a build.
- **4 have failed verification** and need work before any campaign: `nifty_tri_history` (site returns
  its HTML home page to the POST — the same class of failure as the corrupt constituents file in
  §3.12), `gdelt_doc_api` (IP-level 429; source explicitly asks high-traffic users to switch
  datasets), `screener_company_fundamentals` (export gated behind a login — an owner decision, not an
  agent one), `alfred_series_vintage` (unreachable from this host, cause undetermined).
- **Only one declares a bounded era**: `nse_fo_bhavcopy` at `2024-07-08 →`. Every other absent source
  declares `null → null`, i.e. **the register makes no claim about how far back they go**. None of
  them is evidence of available 20-year history.
- **The two that most directly block §9.3** are #1 `nse_equity_list` and #2 `nse_symbol_changes` —
  both VERIFIED 200, both with frozen fixtures, both never fetched. They are the identity spine, and
  they are one campaign away.
- **The benchmark gap** is #3 + #4: `nifty_tri_history` FAILED and `nifty_index_close_snapshot`
  VERIFIED-but-unfetched, so there is no index series in L0 at all — the denominator for any
  return/drawdown ranking is currently missing.

---

## Method notes

Coverage came from all 96,097 `.meta.json` `logical_date` fields, not filename inference. Gap
analysis used the repo's own `dataplatform.ingest.calendar.expected_data_dates` (real NSE holidays +
Muhurat sessions), **not** a weekday approximation. The universe table came from decompressing and
parsing every one of the 2,473 NSE bhavcopy files. Integrity came from `L0Store.verify_checksums()`
re-hashing all 96,097 payloads.

§9.1 required network access: 12 GET requests to `nsearchives.nseindia.com` at ≥2.5 s spacing with
the register's declared headers, performed after confirming no fetch driver was running against that
host (`ps aux` — the only heavy process was a CPU-bound `backtest.duration` replay in a separate
worktree). Responses were held in memory and written only under `/tmp`; the repo's `Fetcher` was
deliberately **not** used because it would have written to `data/L0`.

**No file in `/home/ubuntu/stock-manager` was created, modified, or deleted.**
