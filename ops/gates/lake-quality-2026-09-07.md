# Lake quality and completeness — the server, 2026-09-07

**Server tree:** `41cb40e` · **quiet** (0 drivers; the Integrated Filing, BSE legacy, BSE CA and
lineage campaigns all exited overnight) · **migrations** 0001–0010 applied · **disk** 160 GB free.
Everything below was measured read-only through `ops/remote.sh exec`: the platform's own tools
(`GapScanner`, `/status/*`, `ops/verify_pit_fundamentals.py`), SQL aggregates, whole-store pyarrow
scans, a full L0 re-hash, and two external references — Yahoo Finance and Screener — pulled from
the server itself. Companion reports written today: [`l2-vs-yahoo-2026-09-07.md`](l2-vs-yahoo-2026-09-07.md),
[`price-yfinance-sanity-2026-09-07.md`](price-yfinance-sanity-2026-09-07.md),
[`fundamentals-screener-sanity-2026-09-07.md`](fundamentals-screener-sanity-2026-09-07.md).
Yesterday's catalogue ([`data-catalogue-2026-09-06.md`](data-catalogue-2026-09-06.md)) is the
baseline; the server has moved from "four sources" to the superset since.

---

## 0. The headline

**The raw lake is complete and intact; the adjusted layer is not.**

- **L0** — 13 sources, 96,097 payloads, 6.9 GB, every payload re-hashed against its sidecar today:
  **0 defects**. The server is now the superset (the laptop is the one behind).
- **L1** — 14.3 M price rows over 2,475 sessions with no bad OHLC, no bad ISIN, no thin session;
  the PIT fundamentals store (1.35 M facts, 102,650 filings, to 2026-09-05) verifies clean against
  its checkpoints and re-derives fact-for-fact from L0. Gap report over the whole window: every
  missing (source, date) pair is accounted for except 5 never-attempted sessions.
- **L2** — internally consistent (no nulls, no non-positive prices, factors self-consistent) but
  **29 % of the currently-trading NSE EQ universe has no L2 partition**, ~68 single-feed splits and
  bonuses on L2 names are unadjusted, 73 of 399 lineage stitches have a price break across them,
  and 330 k sessions where a name traded in BE/BZ are dropped. Yahoo Finance confirms the
  unadjusted events name by name and clears the rest: 90.5 % of 195,852 sessions agree to 0.5 %.
- **Delivery** is NULL on every one of the 14.3 M rows — both delivery sources sit in the server's
  L0, unused, because the price runner has no rebuild-from-L0.
- **Operationally**: the status API container runs an image built 2026-08-10; nothing scheduled has
  ever run; `identity_refresh` (2 requests) has still never run; the server-only third of L0 has no
  off-host copy.

---

## 1. L0 — the immutable lake

| Source | Payloads | Size | Sidecar hash | Range |
|---|---|---|---|---|
| `nse_xbrl_filing` | 81,752 | 5.18 GB | 0 mismatches | 2018 → 2026-09-05 |
| `nse_sec_bhavdata_full` | 1,712 | 0.47 GB | 0 | delivery, UDiFF era |
| `bse_bhavcopy_udiff` | 536 | 0.43 GB | 0 | 2024-07-08 → 2026-09-04 |
| `bse_bhavcopy_legacy` | 1,939 | 0.20 GB | 0 | 2016-09-01 → 2024-07-05 |
| `nse_bhavcopy_legacy` | 1,940 | 0.15 GB | 0 | 2016-09-02 → 2024-07 |
| `nse_financial_results_index` | 92 | 0.11 GB | 0 | 2016-04 → 2026-07 |
| `nse_bhavcopy_udiff` | 533 | 0.09 GB | 0 | 2024-07 → 2026-09-01 |
| `nse_mto` | 759 | 0.05 GB | 0 | delivery, legacy era |
| `nse_integrated_filing_index` | 114 | 0.02 GB | 0 | 2025-03 → 2026-09-06 |
| `bse_corp_actions` | 6,689 | 59 MB | 0 | one scrip each, fetched 2026-09-06/07 |
| `nse_corp_actions` | 11 | 6.9 MB | 0 | yearly chunks 2016-09 → 2026-09 |
| `nifty_index_constituents` | 17 | 252 KB | 0 | 2026-09-03 only |
| `bse_scrip_master` | 3 | 3.7 MB | 0 | 2026-09-06 |

XBRL documents by year: 2018 3,669 · 2019 5,660 · 2020 7,594 · 2021 7,994 · 2022 8,306 ·
2023 9,437 · 2024 10,578 · 2025 14,886 · 2026 13,628. The sweep took 7 s (page cache); the
scheduled `run_l0_verify` job that should be doing this weekly has never fired.

**Off-host copies: none.** The laptop holds the NSE sources and 536 BSE UDiFF sessions; the
~25,000 XBRL documents fetched since 2026-09-05 (~1.6 GB), `bse_corp_actions` and most of
`bse_bhavcopy_legacy` exist on this one disk. Postgres has a pre-lineage dump of this morning in
`~/campaign/` (9.6 MB) and the 2026-09-04 seed backup; nothing newer leaves the machine.

---

## 2. Sync state and the gap report

`GapScanner` over 2016-09-01 → 2026-09-06, all nine tracked sources, on the current code:

```
145,429 pairs — 114,505 complete, 6,023 explained, 2,965 UNEXPLAINED
explained:   WEEKEND 3,126 · HOLIDAY 423 · OUTSIDE_SOURCE_ERA 2,474
unexplained: FAILED 2,958 · NEVER_ATTEMPTED 5 · L0_PRESENT_L1_ABSENT 2
L1 unverified for 12 published pairs (index constituents — no per-date L1 dataset to probe)
```

The seven non-FAILED pairs, each with its cause:

| Source | Date | Reason | What it is |
|---|---|---|---|
| `nse_bhavcopy` | 2016-09-01 | never attempted | the window's first session; the NSE backfill started 09-02 |
| `nse_bhavcopy` | 2026-09-02, 03, 04 | never attempted | no daily job runs anywhere; BSE has these three |
| `bse_bhavcopy_legacy` | 2024-07-08 | never attempted | era overlap in the register — UDiFF owns the day |
| `bse_bhavcopy_legacy` | 2021-12-29 | L0 present, L1 absent | truncated at source (row 1773 has 27 fields); re-fetch, not re-parse |
| `nifty_index_constituents` | 2026-09-03 `niftyprivatebank` | L0 present, L1 absent | the site answered HTML with a 200; parser refused it correctly |

Per source (`/status/sources`): `bse_bhavcopy` 536 PUBLISHED to 2026-09-04 · `bse_bhavcopy_legacy`
1,938 + 1 FAILED · `bse_corp_actions` 6,683 + 1 FAILED (scrip 517380: a ratio of 0:0) ·
`nse_bhavcopy` 2,471 to 2026-09-01 · `nse_corp_actions` 11 · `nse_financial_results_index` 90 + 1
FAILED (Annual 2026-04..06 came back as an empty array) · `nse_integrated_filing_index` 114 to
2026-09-01 · `nse_xbrl_filing` **102,650 PUBLISHED / 2,956 FAILED** (§5) · `nifty_index_constituents`
16 + 1 FAILED. Every source reports `healthy: true`; no failure streaks.

---

## 3. L1 prices

`prices_raw`: **14,298,626 rows**, 2,475 date partitions (2016-09-01 → 2026-09-04), 918 MB.

| | NSE | BSE |
|---|---|---|
| rows | 5,686,091 | 8,612,535 |
| sessions | 2,471 (2016-09-02 → 2026-09-01) | 2,474 (2016-09-01 → 2026-09-04) |
| distinct ISINs | 7,536 | 11,129 |
| regular-market rows | EQ 4,274,581 · BE 427,904 · SM 319,265 | A 1.44 M · B 2.73 M · X 1.98 M · XT 1.06 M · T 0.43 M |
| `deliv_qty` populated | **0** | **0** (BSE has no delivery feed) |

Integrity: 0 rows with `close <= 0` or `high < low`; 0 null/empty/malformed ISINs; 0 sessions with
fewer than 1,000 NSE or 1,500 BSE rows; 4 BSE-only dates (2016-09-01 and the three September
sessions NSE never fetched); 0 NSE-only dates after the BSE cutover. Quarantine: 1 row.

**116 duplicate keys, all BSE UDiFF** (33 ISINs, 2025-01-20 → 2026-02-23, group A): the feed prints
a `SYMBOL#` row beside `SYMBOL` for the same ISIN — one trade of one share at the same close
(ASHOKLEY#: qty 1, value ₹208; ASHOKLEY: 425,165 shares). The parser keeps both and the writer's
key `(exchange, isin, symbol, series)` treats them as distinct. Harmless while L2 excludes BSE;
a parser rule (drop or fold `#` rows) closes it.

**Delivery is absent on every row.** The catalogue said this yesterday; what changed is that both
delivery sources (`nse_mto`, `nse_sec_bhavdata_full`, 2,471 payloads) now sit in the server's L0.
Nothing can use them: the price backfill has no `--rebuild-from-l0` (BACKLOG M3.1), so joining
delivery would mean re-fetching 2,471 sessions. The laptop's join was 59 % of rows / 79 % of EQ,
with 1.36 M rows quarantined `symbol_unresolved`; the server has not got that far.

Funds/ETFs: 817 `INF` ISINs, 591,924 NSE rows; 351 trade in Aug/Sep 2026 and 193 are in the master.

---

## 4. L2 — adjusted prices

`prices_adjusted`: 2,237 ISINs (all NSE), 3,483,230 rows, 2016-09-02 → 2026-09-01, 155 MB. No
nulls; `adj_close` and `tr_close` strictly positive (0.04 → 162,295); `cum_price_factor` in
[0.0083, 1]; `cum_qty_factor` in [1, 120]; no L2 partition is shorter than its L1 EQ history.

### 4.1 Coverage — L2 only exists where a corporate action once fired

L2's ISIN set equals the set of ISINs that ever had an `l2_invalidation` row (2,237 = 2,237; "in L2
with neither a factor nor an invalidation" = 0). There has never been a whole-universe
materialisation, only the queue drain.

| Population | ISINs | In L2 |
|---|---|---|
| NSE EQ ever (2016-09 →) | 3,731 | 2,231 |
| NSE EQ trading in Aug/Sep 2026 | 2,716 | **1,923 (71 %)** |
| — absent, by first EQ session | 793 | 2016: 83 · 2021: 66 · 2022: 56 · 2023: 68 · 2024: 124 · 2025: 167 · 2026: 164 |
| — absent with ≥ 1,000 sessions | 219 | |

None of the 793 has a factor, an invalidation or a dividend on record; 46 have some other action.
They are the names with no reconciled corporate action at all — recent listings, but also
**ADANIGREEN, ADANIENSOL and ETERNAL** (never paid a dividend, never split). `QueryService.cross_section`
and the backtest read L2, so these names do not exist to any strategy.

### 4.2 The EQ-only stitch drops sessions the name traded in another series

The stitch, the query layer and the backtest all filter `series = 'EQ'`. NSE moves a stock to BE
(trade-for-trade) after a demerger or under surveillance, and to BZ/SM for compliance and SME cases.
For the 2,237 L2 names, **330,177 (ISIN, session) pairs traded only in a non-EQ series**
(1,096 ISINs; BE 268,226 · BZ 37,621 · SM 23,556), and all 330,177 are absent from L2 — rising
from 16 k in 2017 to 56 k in 2024. SKFINDIA is the shape of it: EQ close 5,008 on 2025-10-14,
demerger ex-date 10-15, twelve BE sessions at ~2,200, EQ again on 10-31 at 2,141. In L2 the name
vanishes for twelve sessions and reappears 57 % lower — a structural break that is correct and a
gap that is not.

### 4.3 Correctness — what Yahoo Finance says

`ops/sanity/l2_yahoo_check.py` (new today) compares `adj_close` with Yahoo's split- and
bonus-adjusted close for every common session since 2016-09-01: the 60 most-liquid names plus 40
from this audit. Report: [`l2-vs-yahoo-2026-09-07.md`](l2-vs-yahoo-2026-09-07.md).

```
89 names · 195,852 sessions · 177,319 within 0.5 % (90.54 %)
71 names fully consistent · 11 with no L2 partition · 26 persistent ratio shifts on 16 names
22 one-day Yahoo blips ignored (a bad Yahoo close on 2025-03-18 hit 11 of the 100 names)
```

The persistent shifts sort into three kinds:

| Kind | Names | Verdict |
|---|---|---|
| **Our L2 missed a split or bonus** | UNOMINDA 2018-07-11 (ours/Yahoo 3.0 → 1.0, a 2:1 bonus), HINDPETRO 2017-07-11 (1.5 → 1.0, 1:2 bonus), BEARDSELL 2017-05-30 (6.4 → 1.07, 5:1 face-value split), GLOBE, HBSL, ORTINGLOBE, PATANJALI (100× at the insolvency relisting) | **defect** — §4.4 explains each mechanism |
| Demerger or rights: Yahoo adjusts, we mark a structural break | RELIANCE 2023-07-20 (Jio Financial), TATACHEM 2020-03-04, ADANIENT 2018, CESC 2018-10-30, STAR 2018-04-06, GFLLIMITED 2019-08-22, CHOLAHLDNG 2017-10-10, BHARTIARTL 2019-04-23 (rights) | by design (EXECUTION_PLAN §4.3); a reader of L2 must respect `structural_break` |
| Yahoo is wrong | TRENT — the June-2026 1:2 bonus (both feeds, factor 0.667) is applied by Yahoo only back to 2026-01-01; our series is consistent | none |

The 11 names with no L2 are the §4.1 gap in the liquid end of the market: ETERNAL, GROWW, LENSKART,
ADANIGREEN, ADANIENSOL, ATHERENERG, SYMBIOTEC, LALITHAA, SKYWAYS, MILKYMIST, TEMPSENS.

The raw-close check ([`price-yfinance-sanity-2026-09-07.md`](price-yfinance-sanity-2026-09-07.md),
60 names, trailing year) is unchanged from 2026-09-06: 99.07 % identical to the tick, 135 sessions
> 1 % (128 of them CUPID, where Yahoo re-based history after a 5:1 split and our raw is right),
335 Yahoo sessions absent from our L1 (the September sessions no daily job fetched).

### 4.4 Why the adjustments are missing — three mechanisms, measured

**(a) 521 NSE feed strings naming a bonus or split were never parsed.** Of 777 EQ rows in the NSE
corporate-action L0 whose subject says bonus/split/sub-division/consolidation, 521 have no
`corporate_actions` row from `nse_corp_actions`. Two forms account for them: the compound
"Bonus 2:1/Dividend- Rs 1.60 Per Share" and "Face Value Split (Sub-Division) - From Rs 10/- Per
Share To Re 1/-" (JSWSTEEL 2017, EICHERMOT 2020, BAJAJFINSV 2022, TRENT 2016, BAJFINANCE 2016,
IEX 2018, CESC 2021, SAREGAMA 2022 …). 164 of them show a > 15 % raw price break at the ex-date.
Most changed the ISIN, which is why they surface as lineage edges rather than as L2 breaks on a
current name — and why the lineage cannot corroborate them (b).

**(b) A single feed never reaches the factor chain.** The `QUEUE` policy is the two-exchange
invariant: an action one feed reports is a `SINGLE_SOURCE` flag a human confirms. Nobody has
confirmed one. Open queue: **10,142 WARN** — DIVIDEND 9,827 · SPLIT 169 · BONUS 92 · RIGHTS 31 ·
BUYBACK 20 · DEMERGER 3; 10,062 raised by the BSE feed, 80 by NSE. Restricted to L2 names and
ex-dates since 2016-09: **44 splits (43 ISINs), 21 bonuses (18), 3 rights** — about 68 price events
the adjusted series does not adjust. UNOMINDA and HINDPETRO are two: the BSE row carries the ratio
(2:1, 1:2), the NSE twin is an unparsed compound string, so the quantified row is single-source.
Also open: 225 `RATIO_MISMATCH` ERRORs (all dividends) and 4 `EX_DATE_MISMATCH`; resolved so far:
8,301 ERROR + 2,480 WARN, all by the 2026-09-07 re-reconciliation, none by a person.

**(c) Lineage edges without a corroborating action stitch without a factor.** `isin_lineage` has
399 edges (273 CORROBORATED by a SPLIT/BONUS on the predecessor, 126 DERIVED from price
contiguity alone). Testing L2 continuity across each edge (adjusted close before vs after the
effective date, ±15 %):

| | CORROBORATED | DERIVED |
|---|---|---|
| continuous | 237 | 57 |
| **break across the edge** | **34** | **39** |
| successor has no L2 / no rows after | 2 | 28 |
| predecessor history not stitched | – | 2 |

A DERIVED break is a face-value split with no ratio (BEARDSELL: the successor's history starts at
one-fifth of the predecessor's). A CORROBORATED break where the raw prices were already continuous
(ATLASCYCLE: raw ratio 0.95, adjusted 3.94; TIRUPATIFL 0.96 vs 0.25) is a factor applied where the
exchange had already re-based — a double adjustment. 24 DERIVED edges lead to a successor with no
L2 at all (§4.1 again).

Cross-checks that hold: `adjustment_factors` 2,681 rows on 1,820 ISINs, `price_factor × qty_factor`
= 1 on every row, 411 structural breaks (410 at factor 1.0), 2 future ex-dates; 1,490 of the 1,561
SPLIT/BONUS events since 2016-09 have a factor on the ex-date; the 71 without are 70 BSE-sourced
rows (mostly `unquantified` purpose text, 26 on names still trading) and one NSE consolidation.
`l2_invalidation` 6,888 rows, all resolved; `corporate_action_id` is NULL on every factor (BACKLOG).

---

## 5. Fundamentals

`pit_fundamentals`: **2,015 partitions · 1,349,562 facts · 102,650 filings · 2,291 ISINs**, filing
dates 2018-05-21 → 2026-09-05. Filings by year: 2018 4,617 · 2019 6,746 · 2020 9,368 · 2021 9,786 ·
2022 10,434 · 2023 11,749 · 2024 13,158 · 2025 18,648 · 2026 18,144 (2,275 ISINs filing in 2026).
81,194 distinct documents: 21,456 documents answer two index entries with **different** periods
(a quarter and its year-to-date column, never the same period twice — 0 duplicated facts).

**The verifier was broken, not the store.** `ops/verify_pit_fundamentals.py` read the filing id from
the pre-0008 compound `source`; after migration 0008 that is `''` on every row, so it saw one
checkpoint, called all 102,650 filings orphans and re-derived a sample of zero. Fixed (`0afba9e`);
the same store now reports: 102,650 stable checkpoints · 0 orphans · 0 missing · every invariant
PASS · `shares_outstanding` and equity derivations recompute exactly · all 96,569 corroborable
share counts inside the 3× EPS band · **1,500 filings re-derived from L0 match fact for fact**.
Independently, the L1 filing-id set equals the PUBLISHED unit set (0 either way).

Concept coverage (share of filings): P&L concepts 99 %+ · `shares_outstanding` 94.1 % ·
`profit_attributable_to_owners` 38.6 % · `debt_equity_ratio` 26.1 % · `reserves_excl_revaluation`
21.3 % · `shareholders_equity_excl_revaluation` 15.1 % · bank NPA/CET1/RoA 0.6 %.

**Coverage of the trading universe:** 2,103 of the 2,716 NSE EQ names trading in Aug/Sep 2026 have
a filing (77 %), 2,094 of them dated 2026-07-01 or later. Of the 613 without: 351 are `INF`
funds/ETFs (no results filings exist), 312 first traded in 2026, and **521 have no NSE
`symbol_history` row** — the D2 identity check refuses their index entries (§6).

**The 2,956 FAILED filings**, classified from `last_error`:

| Class | Count | Nature |
|---|---|---|
| no results column covers the index entry's period | 1,807 | the period is a label, not the period (parser; memory) |
| payload never fetched (`MissingPayloadError`) | 710 | the 2018–2025 documents the old feed listed and nobody downloaded; the "fetch this list" runner does not exist |
| identity refused (index symbol vs ISIN as-of) | 195 | D2 as-of check; 521 trading names lack NSE symbols |
| integrated-filing parse refusals (`filing names…`, entity id, paid-up, body) | 193 | 2025-10 → 2026-08, new-feed shapes |
| HTTP errors | 51 | retryable |

By year: 2018 1,186 · 2019 470 · 2020 181 · 2021 169 · 2022 98 · 2023 120 · 2024 67 · 2025 379 ·
2026 286. `l0_path` is NULL on every FAILED row, so "captured under a sibling entry" cannot be
read from `sync_state`; the memory note's 83.8 % figure came from the index files.

### Against Screener

`ops/sanity/screener_fetch.py` + `screener_compare.py`, run on the server today: 120 companies (the
100 most liquid on 2026-09-01 plus 20 at random), one robots-permitted page each at the register's
5 s cadence, compared with the server's PIT store. Full report:
[`fundamentals-screener-sanity-2026-09-07.md`](fundamentals-screener-sanity-2026-09-07.md).

| Comparison | Pairs | Agree (±max(₹1 Cr, 0.5 %)) | 2026-09-06 laptop run |
|---|---|---|---|
| Quarterly Net Profit | 1,136 | **98.9 %** | 96.3 % |
| TTM Net Profit (4 latest common quarters) | 109 | **98.2 %** | 91.5 % |
| Equity Capital (paid-up) | 623 | **98.4 %** | 97.0 % |
| Annual Net Profit | 606 | 97.5 % | 93.5 % |
| Quarterly Sales | 1,137 | 93.5 % | 91.4 % |
| Annual Sales | 607 | 86.3 % | 83.9 % |
| Reserves (ours excl. revaluation) | 612 | 69.4 % | 64.9 % |
| Quarterly EPS identical / re-based by Screener / unexplained | 1,136 | 80.3 % / 3.6 % / 16.1 % | 74.7 / 6.8 / 18.5 |

Every line improved on the laptop's 2026-09-06 run — the store now carries the Integrated Filing
campaign's 2025–26 quarters. Where the two still disagree, the reasons are the known ones:

- **Scale**: a filing stated in rupees or lakhs where the taxonomy says crores — PFC Dec-2023 and
  PAYTM Sep-2023 100× and 10× low, GRAPHITE Mar-2022 100× high on every line, KAYNES equity 10⁶×,
  ZEEL equity Mar-2019 (reserves in the equity slot). The paid-up-vs-median detector
  ([memory: ~1 % of documents](xbrl-filings-stated-at-wrong-scale)) does not yet cover these.
- **Scope**: gross revenue including excise vs Screener's net (CHENNPETRO every quarter ~20 %, ITC
  Jun-2026 54 %), standalone vs consolidated columns (TMPV), restated years (CGPOWER's sign flips).
- **Reserves**: 110 of the 187 disagreements are ours = 0 — the concept is on only 21 % of filings
  (TECHM, WEBELSOLAR, SUMIT); ADANIGREEN's reserves are negative excluding revaluation where
  Screener shows total reserves.
- **EPS**: Screener re-bases history for later splits; ours is the filed figure (ADANIPOWER 5×).
- **Banks do not overlap at all**: AXISBANK, BANKBARODA, HDFCBANK, ICICIBANK, KOTAKBANK, SBIN (and
  JISLDVREQS, a DVR line) have no common quarter — bank filings use the BANKING taxonomy, whose P&L
  lines are not the ones the comparison keys on; the bank-specific concepts are on 654 filings.
- Valuation snapshot (our last close and shares vs Screener live): price within 5 % for 83 %,
  market cap 78 %, P/E 45 % (TTM window and re-basing differences), face value exact 116/119.

---

## 6. Identity

| Table | Rows | Note |
|---|---|---|
| `security_master` | 8,598 | INE 7,747 · INF 675 · IN9 174 |
| `exchange_listing` NSE | 2,397 ACTIVE | the 2026-08-08 EQUITY_L snapshot from fixtures — unchanged |
| `exchange_listing` BSE | 4,995 ACTIVE · 1,167 SUSPENDED · 2,311 DELISTED | from the 2026-09-06 scrip master |
| `symbol_history` | 11,359 | |
| `identity_reconciliation` | 10 open | BSE symbols mapping to two ISINs (IDEA, BOSTON, …) |
| `isin_lineage` | 399 | 273 corroborated · 126 derived |

Coverage against what actually trades: 14,023 ISINs in L1 → 6,809 in the master (49 %); NSE's
7,536 → 3,369 in the master, 2,397 with an NSE listing. Of the **2,716 NSE EQ names trading now,
2,556 are in the master (94 %) but only 2,195 have an NSE `symbol_history` row (81 %)** — 521 names
whose delivery, corporate actions and filings all resolve through the symbol have nothing to
resolve to. `identity_refresh` (2 requests to NSE, built 2026-09-06) has still never run on either
machine. The 351 trading `INF` ISINs have no scheme master (193 in the master via BSE).

---

## 7. Freshness (Monday 2026-09-07)

| Dataset | Server ends | Behind by |
|---|---|---|
| NSE prices | 2026-09-01 | 3 sessions — no daily job |
| BSE prices | 2026-09-04 | 1 session |
| Delivery | never | — |
| XBRL filings | 2026-09-05 | current |
| Integrated index | 2026-09-01 (page 6) | current |
| NSE corporate actions | fetched 2026-09-03, ex-dates to 2026-09-21 | current |
| BSE corporate actions | fetched 2026-09-07 | current |
| Index constituents | L0 2026-09-03; **no L1 dataset on the server** (re-derivable from L0) | one snapshot |
| Identity snapshot | 2026-08-08 (fixtures) | stale |
| Scheduler | `NEVER_RAN`; `job_run` 0; `decision_journal` 0; `archive_bundle` 1 (2024-01-02) | |

---

## 8. Operational findings

1. **The API container is a month behind the code.** `trading-platform-app-1` runs an image built
   2026-08-10 (started 2026-09-04): `/status/quarantine` is absent, `/status/sync` shows the
   pre-0008 shape (duplicate rows per document, no `unit`), and its `GapScanner` is old code. Every
   `/status/*` reading an operator takes on the server is from that image. Rebuild after each sync.
2. **The server tree was dirty**: the BSE CA campaign rewrote the tracked
   `ops/gates/M9-ca-backfill-report.md` (a run report landing in a tracked file), and a stray
   `verify_remote.py` sat at the root. Both moved to `~/campaign/` and the report restored, so
   `remote.sh sync` cannot fail on a conflicting pull. The runner should write dated reports.
3. **Nothing runs on a schedule**: scheduler never ran, no L0 sweep, no price sentinel (the
   D7 check that would have raised the L2 breaks above), no daily fetch — on either machine.
4. **The compose exposure noted on 2026-09-06 is unchanged.**
5. Small register/era items: `bse_bhavcopy_legacy` and `bse_bhavcopy` both claim 2024-07-08;
   NSE 2016-09-01 was never attempted; `nse_financial_results_index` Annual 2026-04..06 is a
   legitimate empty answer recorded as FAILED.

---

## 9. What follows, in order

1. **Materialise L2 for every NSE EQ ISIN in L1**, not only the invalidated ones (§4.1) — 793 names,
   ADANIGREEN/ETERNAL among them; then make "in L1 EQ, not in L2" a gap-report reason.
2. **Parse the compound and face-value-split purpose strings** (§4.4a, 521 rows). This corroborates
   most DERIVED edges and turns most single-source BSE bonuses into two-feed agreements at once.
   Re-run the CA reconciliation, the recompute and the L2 drain; then `l2_yahoo_check.py` — the
   persistent-shift table should shrink to demergers and rights.
3. **Decide the single-source policy** for the remaining quantified splits/bonuses on NSE-traded
   names (§4.4b, ~68 events), and review the 73 edges with breaks (§4.4c) — the corroborated ones
   for double adjustment.
4. **Include BE/BZ sessions in the stitch** (§4.2) or declare them explicitly out of the universe.
5. **Build `--rebuild-from-l0` for prices** and join delivery on the server, where the L0 is (§3).
6. **Run `identity_refresh`** (2 requests) and ingest a scheme master for `INF` (§6); re-derive the
   195 identity-refused filings afterwards.
7. **Fundamentals**: the 710 never-fetched documents need the list-driven runner; the 1,807
   period-column refusals are a parser change; both offline except the 710 downloads.
8. **Ops**: rebuild the app image on every sync; schedule the L0 sweep, the sentinel and the daily
   jobs; make an off-host copy of the server-only L0.

---

## Addendum, 2026-09-07 afternoon — the L2 fixes, applied and re-verified on the server

Four commits (`5221551`, `66fbde5`, `b9f31fa`, `61ad8c3`), gate green, pushed; the server synced to
`61ad8c3` and ran `lineage_rebuild` (2 min 54 s, rollback dump `~/campaign/pre-l2fix-20260907-060325.dump`):

| Stage | Before | After |
|---|---|---|
| NSE actions replayed from L0 | 12,477 | 12,505 (compound lines now parse: `Bonus 2:1/Dividend…`) |
| Reconciliation queue (open flags) | 10,371 | **403** — 9,970 resolved as superseded |
| `adjustment_factors` rows | 2,681 on 1,820 ISINs | 2,784 on 1,823 ISINs |
| L2 partitions | 2,237 (only ever-invalidated ISINs) | **3,349** — 3,444 redrained + 1,112 filled |
| NSE EQ ISINs (not retired) without L2 | 1,100 | **0** |
| Currently trading NSE EQ names in L2 | 1,923 of 2,716 | **2,714 of 2,716** (the two are reissued this month) |
| Split/bonus events since 2016-09 with no factor | 71 | 49 (BSE `unquantified` text; no ratio to apply) |
| Yahoo: names fully consistent / no L2 partition | 71 / 11 | **84 / 0** ([`l2-vs-yahoo-2026-09-07-after.md`](l2-vs-yahoo-2026-09-07-after.md)) |
| Yahoo: persistent shifts | 26 on 16 names | 23 on 14 — UNOMINDA, HINDPETRO and BEARDSELL's 2017 split are gone |

UNOMINDA now carries 0.2 / **0.333** / 0.5; HINDPETRO 0.333 / **0.667** / 0.667; BEARDSELL 0.1667
(the 1:5 bonus and the 10→2 split on one line); ADANIGREEN and ETERNAL have partitions with an
empty chain. Every remaining Yahoo shift is a demerger or rights issue Yahoo adjusts and this
platform marks structural (RELIANCE, TATACHEM, ADANIENT, CESC, STAR, GFLLIMITED, CHOLAHLDNG,
BHARTIARTL), Yahoo's own TRENT inconsistency, PATANJALI's insolvency relisting, or an illiquid
name (GLOBE, HBSL, ORTINGLOBE).

**What the fixes did not close, measured:**

- **Lineage edges with a price break across the stitch: 86 of 399** (35 corroborated, 51 derived;
  was 73 with 24 successors having no L2 at all). A derived edge's reissue ratio is unknown to
  the system — the feed row was dated outside the two-session window, or the retired ISIN's
  action could not be filed (below). The stitched history is continuous in *name* but not in
  *price* across those 86 boundaries; a consumer should treat them as structural breaks.
- **Retired ISINs are not in the identity master**, so a corporate action filed against one
  (ASTRAL's 2019 and 2021 bonuses under its pre-split ISIN, 112 feed events in all) is recorded
  as an unresolved identity and never becomes a factor. 8 filled partitions show a split-shaped
  jump for exactly this reason; the rest of the 112 are on names below ₹5 or before the window.
  Seeding the master with the ISINs L1 has observed trading is the fix (D2, not L2).
- Of the **382 large day-over-day moves** in L2 now (256 before the fill added 1,112 names),
  357 coincide with no corporate action and no feed row: genuine moves in illiquid names, and
  re-entries after a trade-for-trade period the EQ-only stitch drops (§4.2, unchanged).
- The 403 open flags: 158 dividend `RATIO_MISMATCH`, 156 single-source splits with no stated
  ratio, 82 single-source dividends, 7 ex-date mismatches.

**The backtest on the rebuilt L2.** The ten-year adjusted run first died a minute in: a stitched
survivor (GOLDIAM, `INE025B01025`, 2017-10-03) has L2 bars on sessions L1 files under its retired
ISIN, so neither the liquidity scan nor the backtest's own day map could name its primary exchange
and `canonical_daily` refused the whole day. Fixed in `8ab8377` + `daad207`: an ISIN only one
venue printed takes that venue. `python -m backtest.run --policy naive_momentum --from 2016-09-02
--to 2026-08-31 --delta-report` on the server at `daad207`
([`M9-adjusted-backtest-report.md`](M9-adjusted-backtest-report.md)):

| | Raw L1 signal | L2 adjusted signal |
|---|---|---|
| Sessions / rebalances | 2,470 / 120 | 2,470 / 120 |
| Portfolio XIRR | 11.67 % | 10.92 % |
| Turnover (fills) | 3,147 | 3,251 |
| Total costs | ₹92,377 | ₹89,018 |
| Replay time | 50.0 s | 52.5 s |
| Tracebacks / PIT violations | 0 / 0 | 0 / 0 |

The adjusted signal costs 0.75 pp of XIRR against the raw one: the raw run was buying fake
post-split "momentum" and the adjusted run is not. The report's prose still says the store holds
no corporate actions — that text is the M9.2 template, written when it was true, not a measurement;
the digests differ because the factors are real now. **Determinism (M9.2 acceptance 3):** a second, independent run on the same lake produced the same two digests — raw `8deb0088…a559c20`, adjusted `dba85e3b…962d0ea` — so the rebuilt L2 replays byte-identically. The report's template text was corrected in the same session so its prose follows the runs rather than asserting an empty store.

