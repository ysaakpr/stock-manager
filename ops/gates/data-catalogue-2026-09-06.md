# Data catalogue — what the server captures, what it inherited, what nothing captures

**Date:** 2026-09-06, evening · **Server tree:** `cb87cd5` · **Laptop tree:** `6d3137c`
Everything here was measured read-only: the server through `ops/remote.sh exec` (file counts,
partition names, one small SQL aggregate, three single-partition reads), the laptop directly.
"Captured" means *a payload the machine fetched itself, sitting in its own L0 with a checksum*.
That distinction turns out to be the whole story.

---

## 0. The headline

The server is the fetch engine, and **it has fetched exactly four sources**:

```
data/L0/
  bse_bhavcopy_udiff             536 payloads   411 MB   2024-07 .. 2026-09
  nse_financial_results_index     92 payloads   110 MB   2016-04 .. 2026-07
  nse_integrated_filing_index     67 payloads    12 MB   2025-03 .. 2026-02
  nse_xbrl_filing             70,421 payloads   4.1 GB   2018-05 .. 2026-08
```

Everything else the server *holds* — 2,469 NSE price sessions in `sync_state`, the whole
`prices_raw` history, the identity master, 9,573 corporate actions — **it did not fetch and cannot
re-derive**. Its Postgres was seeded from the laptop's 2026-09-04 dump (the two `SHA256SUMS` files
are byte-identical: `9e9d172595972c2f…`), and its NSE `prices_raw` partitions are a copy of the
laptop's L1 as of that date. There is no `nse_bhavcopy_*`, `nse_mto`, `nse_sec_bhavdata_full`,
`nse_corp_actions` or `nifty_index_constituents` tree in the server's L0.

So on the server, invariant #1 — every L1 value re-derivable from L0 — holds for BSE prices and
fundamentals, and for nothing else it holds. A full-history gap report run there would report every
NSE session as `PUBLISHED` with its payload absent.

---

## 1. Captured on the server (its own L0)

| Dataset | Source | Server L0 | Derived on server | Fields |
|---|---|---|---|---|
| **BSE equity OHLCV** | `bse_bhavcopy_udiff` | 536 sessions, 2024-07-08 → 2026-09-04, all PUBLISHED | `prices_raw` rows tagged `exchange=BSE` on those 536 dates | OHLC, last, prev close, qty, value, trades — same schema as NSE; `series` = BSE group (`A`,`B`,`T`,`X`…) |
| **Fundamentals, old feed** | `nse_financial_results_index` | 92 index pages (Quarterly + Annual chunks), 2016-04 → 2026-07 | index entries → filings | filing id, ISIN, symbol, period, nature, document URL |
| **Fundamentals, integrated feed** | `nse_integrated_filing_index` | 67 pages, 2025-03 → 2026-02, **campaign still running** (`--to 2026-09-06`, 12 pages / 5,508 filings this run) | as above; no ISIN, no period start (memory) | |
| **XBRL filings** | `nse_xbrl_filing` | **70,421 documents**, 4.1 GB, 2018-05 → 2026-08 | `pit_fundamentals`: **1,139,430 facts · 2,212 ISINs · 86,414 filings · 1,839 partitions · 2018-05-21 → 2026-02-18** | 22 concepts: P&L (revenue, other income, expenses, PBT, PAT, EPS basic/diluted, attributable profit), capital (paid-up, face value, shares outstanding), equity (reserves, shareholders' equity excl. revaluation), `debt_equity_ratio`, segment revenue, and for banks gross/net NPA (₹ and %), CET1, RoA |

Filings on the server by year: 2018 4,617 · 2019 6,746 · 2020 9,368 · 2021 9,786 · 2022 10,434 ·
2023 11,749 · 2024 13,158 · **2025 18,294 · 2026 2,262**. The laptop's copy of the same store stops
at 3,476 for 2025 and 10 for 2026 — the server is ~17,000 filings ahead, and that is the campaign
that is still running.

`sync_state` on the server: `nse_xbrl_filing` 86,379 PUBLISHED / **3,093 FAILED**
(2018-05-25 → 2026-02-14). The failure classes are the ones the morning audit named — integrated
period-guess refusals, never-fetched documents, D2 symbol refusals — at server scale.

---

## 2. Held on the server but not captured there (inherited from the laptop)

| Dataset | What the server has | Where it came from | Re-derivable on the server? |
|---|---|---|---|
| **NSE equity OHLCV** | `prices_raw` NSE rows, 2016-09-02 → 2026-09-01; `sync_state nse_bhavcopy` 2,469 PUBLISHED + 2 FAILED | Laptop L1 copied ≤ 2026-09-04; sync rows from the laptop dump | **No** — no NSE L0 on the server |
| — the two fixed sessions | `date=2020-07-13`, `date=2021-02-16` **absent** | Copied before P1.1 re-derived them on the laptop | No |
| **NSE delivery %** | `deliv_qty`/`deliv_pct` **NULL on every NSE row sampled** (2019-06-03: 1,947 rows, 0 with delivery; 2026-09-01: 3,656, 0); no `nse_delivery` in `sync_state` | Copied before the M1.6 delivery backfill landed on the laptop | No — no `nse_mto` / `nse_sec_bhavdata_full` L0 |
| **Identity master** | `security_master` 2,397 · `symbol_history` 2,886 | Laptop dump; the laptop built it from `tests/fixtures/` (finding N4) | No — no `nse_equity_list` L0 anywhere yet |
| **Corporate actions** | `corporate_actions` 9,573 (the laptop has since moved to 12,126 under the lineage work) | Laptop dump | No — no `nse_corp_actions` L0 on the server |
| **Index constituents** | `sync_state` 16 PUBLISHED + 1 FAILED for 2026-09-03; **no L1 dataset on the server** | Laptop dump (rows only) | No |
| **Adjusted prices (L2)** | `data/L2/` **empty** | — | Nothing to derive from |
| **Archives** | none | — | — |
| **Quality flags** | 2,487 `ca_reconciliation` (all `SINGLE_SOURCE`) | Laptop dump | — |

Two partitions illustrate the mix. `date=2026-09-01` on the server holds **5,005 BSE rows the
server fetched beside 3,656 NSE rows it copied**; `date=2026-09-04` holds **5,042 BSE rows and no
NSE rows at all**, because the laptop never fetched that session and the server has no NSE source
to fetch it from. The server's `prices_raw` reads as 2,472 sessions; only its BSE half is its own.

---

## 3. Captured on the laptop only

For completeness, since the user's question is about coverage and the two lakes differ:

| Dataset | Laptop L0 | Laptop derived |
|---|---|---|
| NSE OHLCV, both eras | 1,940 legacy + 533 UDiFF payloads | `prices_raw` 2,471 sessions, 5,686,091 rows, 2016-09-02 → 2026-09-01, NSE only |
| NSE delivery, both eras | `nse_mto` 759 + `nse_sec_bhavdata_full` 1,712 | joined onto 59.1 % of rows / 78.6 % of EQ; 1,799,849 rows quarantined |
| NSE corporate actions | 11 yearly chunks | `corporate_actions` 12,126 · `isin_lineage` 381 · `adjustment_factors` 696 on 532 ISINs (**0 linked to a CA**) |
| Index constituents | 17 lists for 2026-09-03 | `index_constituents` 794 rows, 16 indices, **one snapshot** |
| BSE scrip master | 3 payloads (Active, Suspended, Delisted), 2026-09 | fetched this afternoon by the lineage work; not yet on the server |
| BSE OHLCV | 536 payloads (brought home) | **0 L1 rows** — the price backfill has no `--rebuild-from-l0` |
| Fundamentals | 56,435 XBRL documents | `pit_fundamentals` 918,541 facts · 1,645 ISINs · 69,344 filings → 2026-08-24 (a stale-tail copy; the live store is the server's) |
| L2 | — | `prices_adjusted` **1,515 ISINs**, 2,655,616 rows, 2016-09-02 → 2026-09-01 (up from 252 this morning, after the lineage rebuild) |

Neither machine holds a complete lake. The union does — BSE prices and the fundamentals campaign
on the server, NSE prices, delivery, corporate actions, identity and index lists on the laptop — and
nothing off either host holds any of it.

---

## 4. Captured nowhere

Thirty rows in `source_register.yaml`; eleven have an L0 tree on at least one machine. These do not,
grouped by why.

**Built and never run** — parser, fixtures and a consumer exist; no runner ever fetched a byte:

| Dataset | Register id(s) | What is missing |
|---|---|---|
| Corporate actions, BSE side | `bse_corp_actions` | Never fetched; this is why all 2,487 CA flags are `SINGLE_SOURCE` |
| Bulk & block deals | `nse_bulk_deals`, `nse_block_deals` | No daily runner (BACKLOG M3.5) |
| Corporate announcements | `nse_announcements`, `bse_announcements` | No poll runner (BACKLOG M3.8); BSE page 1 only |
| Shareholding pattern | `nse_shareholding_pattern` | No runner |
| F&O EOD (OI, PCR, basis) | `nse_fo_bhavcopy` | No runner; `fo_aggregates` store exists empty |
| FII/DII flows | `nse_fii_dii_flows` | No runner, **and a missed day is a permanent hole** — the feed has no date parameter and no archive (BACKLOG M3.4) |
| Index close snapshot | `nifty_index_close_snapshot` | No runner |
| News / geopolitical | `gdelt_v2_event_files`, `curated_rss` | No runner; the A5 monitor's news path has never seen a live headline |
| Macro backdrop | `worldbank_indicator_api` | No runner |
| Identity inputs | `nse_equity_list`, `nse_symbol_changes` | Runner built today (`identity_refresh`); **2 requests queued behind the campaign** |

**Blocked at the source** — verified as unreachable or gated when probed:

| Dataset | Register id | Status | Why |
|---|---|---|---|
| Benchmark TRI | `nifty_tri_history` | FAILED | niftyindices' POST endpoint refuses without a browser session; the backtest benchmark is a **proxy TRI computed from L1** |
| News search | `gdelt_doc_api` | FAILED | HTTP 429 on both probes |
| Macro vintages | `alfred_series_vintage` | FAILED | unreachable from the fetch host, cause undetermined |
| Restated fundamentals | `screener_company_fundamentals` | BLOCKED_CREDENTIAL | the Excel export needs a login; the anonymous company page is the substitute and is used only for sanity checks |
| BSE legacy OHLCV | `bse_bhavcopy_legacy` | VERIFIED, unfetched | pre-2024-07 files carry no ISIN; the scrip-master resolution path exists but no backfill has run |

**Not in the register at all** — the plan (§4.1) or the analyst needs it and no row describes it:

| Need | Where it shows |
|---|---|
| A **fund/ETF scheme master** for the 593 `INF` ISINs that trade on NSE | 0 of 593 resolvable; LIQUIDBEES is the default cash-parking instrument and D2 cannot name it (N5) |
| Balance-sheet and cash-flow statements | The results XBRL is P&L-plus-capital; no cash flow anywhere, equity on 19 % of filings, `debt_equity_ratio` on 27 % |
| Historical index membership | One snapshot; the weekly job that would accumulate it has never fired |
| Historical FII/DII | Unobtainable from NSE (measured, M3.4); NSDL/CDSL carry FPI-only history and are not registered |
| Intraday anything | Out of scope by design (EOD platform) — stated so a reader does not look for it |

---

## 5. Freshness, as of this evening

| | Server | Laptop |
|---|---|---|
| NSE prices end | 2026-09-01 (copy) | 2026-09-01 |
| BSE prices end | **2026-09-04** | — |
| Delivery end | never | 2026-09-01 |
| PIT fundamentals, latest filing date | **2026-02-18** | 2026-08-24 (stale tail — see §3) |
| Corporate actions, latest ex-date | 2026-09-21 (copied) | 2026-09-21 |
| Identity snapshot | 2026-08-08 (from fixtures) | 2026-08-08 (from fixtures) |
| Index constituents | none in L1 | 2026-09-03 |
| Scheduler runs | 0 | 0 |

Today is Sunday 2026-09-06. Sessions 2026-09-02/03/04 exist on the server for BSE and on neither
machine for NSE, because no daily job runs anywhere (project re-evaluation §2.1).

---

## 6. What follows from this

1. **The server's own coverage is BSE prices + fundamentals.** Any statement that "the server has
   ten years of NSE prices" means it has a copy it cannot rebuild. Bringing the NSE L0 to the server
   — or accepting that the laptop is the NSE fetch engine and the server the BSE/fundamentals one —
   is a decision to make explicitly rather than by default.
2. **Delivery does not exist on the server** in any form, and the daily job would not fetch it
   anyway (`DAILY_NSE_SOURCES` is bhavcopy only).
3. **The two lakes must both survive** until one holds the union. No off-host copy exists of either.
4. **Twenty register rows describe data nothing has ever captured.** For nine of them the code is
   built and waiting on a runner and a scheduler; for five the source itself is the blocker; the
   rest need a register row before anything can be said about them.
