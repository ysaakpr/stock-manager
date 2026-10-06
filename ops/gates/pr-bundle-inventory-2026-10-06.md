# PR-bundle member inventory, and what was promoted to L1 — 2026-10-06

**Task** `pr-bundle-parse` · **lake** `/home/ubuntu/stock-manager/data` (L0 read, four new L1 datasets
written, nothing else) · **no network requests** · times IST.

`data/L0/nse_pr_bundle/` holds **4,156** `PR<DDMMYY>.zip` bundles, 2010-01-04 → 2026-10-01, 1.56 GB.
Every member of every bundle was opened for this inventory (one read-only pass, 13:27 IST); nothing
below is sampled unless it says so.

---

## 1. The headline

1. **26 data-member kinds** exist across the corpus (plus 7 documentation members and one misnamed
   one-off). `Pd`, `bh`, `Bc`, `Bm`, `mcap`, `ffix`, `fo` and `etf`'s `UNDERLYING` column carry
   facts the lake has nowhere else (`An` too, for 2010-2013); the rest are redundant with the
   bhavcopy (measured below, row by row), one-offs, or retail-debt/currency data outside scope.
2. **Three members promoted, four datasets**, all additive: `pr_band_hits` (`bh`),
   `pr_security_marks` and `pr_index_eod` (`Pd`), `pr_ca_broadcasts` (`Bc`), plus the enumerated
   quarantine `pr_bundle_quarantine`. `prices_raw` and L2 untouched. Numbers in §4.
3. **`Pd` is the find.** It is not "52-week high/low" as `MemberKind` said. It carries
   **(a)** the OHLC and 52-week range of **every NSE index, daily, 2010-01-04 → 2026-10-01** —
   184 published index names, India VIX included — where the lake otherwise holds index levels
   only as the `benchmark_tri` series; **(b)** `IND_SEC`, which on a security row is **NIFTY 50
   membership on every session, 2010 → 2026**: identical to `ffix`'s NIFTY constituent set on
   **all 830** sessions the two share (2010-01-04..2013-04-30), so the dated membership history
   that `ffix` stops at 2013-04-30 runs on, from the same exchange file, to 2026-10-01; and
   **(c)** `CORP_IND`, NSE's own same-session ex-marker (`XB`, `XR`, `XO`, `XD`, `XI` and
   combinations), 36,552 marks — which also exposes **119 `XB` and 42 `XR` ex-dates with no row
   in `corporate_actions`**.
4. **`Bc` reader defects from the 2026-09-08 close-out are fixed**: the 11 members that died on an
   unquoted comma, the one `DD-MM-YYYY` date and the one 0-byte member now parse (0 `Bc` failures
   over the corpus; the four undatable bundles stay refused, as they must).
5. **D7**: a witness rule `unexplained_move_witness` (INFO) annotates every would-be
   `unexplained_move` flag with what NSE printed about that security that session. It never
   resolves or suppresses. `quality_flag` holds no `unexplained_move` rows today; of the 8,517 the
   rule would raise over ISIN-era `prices_raw`, **5,842 (68.6%) have a witness — 559 strong**
   (a split/bonus/rights the `corporate_actions` table lacks on that date), 5,245 a same-side
   band hit. §5.

---

## 2. Every member kind

`bundles` counts bundles carrying the member; `rows` are non-blank lines after the header, so a
report with banner rows (`Pd`, `Gl`) counts its banners too. "Header shapes" is the number of
distinct first lines (whitespace collapsed) across the corpus. The census dates each file by its
archive name, so `PR020118.zip` — the 2019-01-02 payload served under 2018-01-02 — counts once
under 2018; the L1 build, which dates by the members, refuses it.

### 2a. Equity-cash members

| member | file pattern | span | bundles | rows | header shapes | verdict |
|---|---|---|---:|---:|---:|---|
| `Pd` price detail | `Pd<DDMMYY>.csv` → `pd<DDMMYYYY>.csv` (2025-10-13) | 2010-01-04 → open | 4,156 | 8,602,196 | 2 (one 17-cell one-off, 2010-05-14) | **UNIQUE in part — parsed.** OHLC/qty/value/trades of every security row are byte-equal to `prices_raw` (§3); index rows, `IND_SEC`, `CORP_IND`, `HI_52_WK`/`LO_52_WK` and the section banners are not anywhere else |
| `Pr` price report | `Pr<DDMMYY>.csv` | 2010-01-04 → open | 4,156 | 8,602,185 | 2 | **REDUNDANT** — `Pd` minus `SYMBOL`/`SERIES`, row for row (8,602,185 vs 8,602,196) |
| `bh` band hits | `bh<DDMMYY\|DDMMYYYY>.csv` | 2010-01-04 → open | 4,156 | 845,128 | 8 (`SR`/`SERIES`, padded, one column swap, `INDEXFLAG`, no-flag) | **UNIQUE — parsed** (reader existed, H2 read it in memory; now L1) |
| `Bc` corporate actions | `Bc<DDMMYY>.csv` → `bc<DDMMYYYY>.csv` | 2010-01-04 → open | 4,156 | 1,585,523 | 1 (+1 empty member) | **UNIQUE — parsed.** The only knowable-dated CA book; `corporate_actions` has one ingest-day `knowable_date` |
| `An` announcements | `An<DDMMYY>.txt` | 2010-01-04 → open | 4,156 | 7,552,580 | 1 (`COMPANY NAME SYMBOL : ANNOUNCEMENTS`) | **UNIQUE for 2010-2013** (`nse_announcements` L0 starts 2014); free text, symbol-keyed. Not parsed — NLP-shaped, see §6 |
| `Bm` board meetings | `Bm<DDMMYY>.txt` | 2010-01-04 → open | 4,156 | 158,347 | 1 (`… : BM DATE : BM PURPOSE`) | **UNIQUE** — dated board-meeting (results-date) calendar, knowable on the bundle date. Not parsed — §6 follow-up 1 |
| `etf` ETFs | `etf<DDMMYY>.csv` | 2010-03-08 → open | 4,113 | 394,601 | 1 | **REDUNDANT for prices** (§3); `UNDERLYING` (372 distinct strings, e.g. `GOLD`, `NIFTY 50`) is unique. Not parsed — §6 follow-up 3 |
| `sme` SME platform | `sme<DDMMYY>.csv` | 2012-05-30 → open | 3,476 | 398,653 | 8 (a `TRADE_DATE`/`Trade Date`/`TRADING DATE` lead column, 2012-09..2013-05) | **REDUNDANT** — every row in `prices_raw` with the same close (§3) |
| `corpbond` corporate bonds | `corpbond<DDMMYY>.csv` | 2011-10-03 → open | 3,717 | 425,681 | 2 (one dated-banner one-off, 2016-01-12) | **REDUNDANT** for prices (§3); debt, out of scope |
| `mcap` market cap | `MCAP<DDMMYYYY>.csv` → `mcap…` | 2024-02-01 → open | 662 | 1,743,050 | 1 | **UNIQUE** (issue size) — parsed by `mcap.py` since W2, consumed by `ops/studies/cap_tier_size_measure.py`; not promoted here |
| `ffix` free-float index | `ffix<DDMMYY>.csv` | 2010-01-04 → 2013-04-30 | 832 | 749,544 | 1 | **UNIQUE** — parsed by `ffix.py`/`membership.py` since 2026-09-08 |
| `Ix` index (old) | `Ix<DDMMYY>.csv` | 2010-01-04 → 2010-10-08 (+ one zip, 2013-02-19) | 195 | 106,412 | 1 | parsed by `ix.py`; superseded by `ffix` |
| `cap` index cap | `cap<DDMMYY>.csv` | 2012-04-12 → 2013-05-24 | 3 | 354 | 1 (`ffix` + `CAP FACTOR`) | one-off; not parsed |
| `PE_` P/E | `PE_<DDMMYY>.csv` | 2024-02-01 → 2025-03-21 | 284 | 479,468 | 1 (`SYMBOL,SYMBOL P/E,ADJUSTED P/E`, blank-line-led) | unique but 14 months; not parsed |
| `Gl` gainers/losers | `Gl<DDMMYY>.csv` | 2010-01-04 → open | 4,155 | 1,360,713 | 3 | **REDUNDANT** — `CLOSE`/`PREV_CL`/`%` of index constituents; keyed by *security name only* |
| `HL` new highs/lows | `HL<DDMMYY>.csv` | 2010-01-04 → open | 4,155 | 328,167 | 1 | **REDUNDANT** with `Pd`'s 52-week columns; name-keyed |
| `Tt` top traded | `Tt<DDMMYY>.csv` | 2010-01-04 → open | 4,156 | 103,900 (25/day) | 1 | **REDUNDANT** — top 25 by value from the bhavcopy; name-keyed |
| `MA` market activity | `MA091222.csv` | 2022-12-09 only | 1 | 2,348 | 1 | one-off prose report |

### 2b. Retail debt, derivatives — out of the equity platform's scope

| member | span | bundles | rows | note |
|---|---|---:|---:|---|
| `NPD` / `RPD` / `Rtt` retail debt | 2010-01-04 → 2014-12-31 | 1,243 | 101,865 / 2,501 / 15 | near-empty after 2010 |
| `fo` F&O bhav | 2010-01-04 → 2018-12-21 | 2,227 | 128,574 (CSV era) | a `.csv`+`.doc` to 2011-02-18, then a nested zip of `fo_*`, `futidx`, `futstk`, `optidx`, `optstk`, `ttfut`, `ttopt`, `futivx` (1,187). **Unique in the lake** — `nse_fo_bhavcopy` is registered, never fetched. Not parsed (§6 follow-up 4) |
| `op` options | 2010-01-04 → 2011-02-18 | 286 | 415,369 | pre-zip F&O options |
| `cd` / `cf` / `co` currency derivatives | 2010-01-04 → 2018-12-21 | 2,172 / 668 / 468 | — / 14,069 / 23,295 | out of scope |

**Documentation members** (no rows): `readme.txt` 4,149 · `nuver.txt` 2,095 · `rdm.doc` 1,480 ·
`rdm_help.txt` 1,243 · `rdm.docx` 609 · `help.txt` 286 · `readmenew.txt` 7, plus `rdm.doc.docx`,
`rdm.dot`, `rdm.rtf`. One misnamed data member, `Gl220617-.csv` (2017-06-22), is a `Gl`.

---

## 3. Redundancy, measured rather than assumed

For three sessions in three eras, every priced row of `Pd`, `etf`, `sme` and `corpbond` was looked
up in that session's NSE `prices_raw` by `(symbol, series)`:

| session | `Pd` | `etf` | `sme` | `corpbond` |
|---|---|---|---|---|
| 2013-07-01 | 1,445 / 1,445 present, 1,445 same close | 31/31/31 | 2/2/2 | 45/45/45 |
| 2019-07-01 | 1,927 / 1,927 / 1,927 | 68/68/68 | 67/67/67 | 152/152/152 |
| 2026-09-04 | 3,655 / 3,655 / 3,655 | 349/349/349 | 473/473/473 | 122/122/122 |

So for prices the bundle adds nothing the bhavcopy does not already hold from 2011-06-22. Before
that date L1 has no `prices_raw`, and the L0 legacy bhavcopy for 2010-01-04..2011-06-21 has **no
ISIN column** (its pre-ISIN sub-era) — which is also why identity before 2011-06-22 falls back to
the master (§4c).

**`Pd` security rows were not promoted as prices.** `pr_security_marks` carries `prev_close` and
`close` as a same-file witness and nothing else of the OHLC; it is not a price series and nothing
should read it as one.

---

## 4. L1 build

`python -m dataplatform.ingest.pr_bundle_l1 --data-root /home/ubuntu/stock-manager/data --from
2010-01-04 --to 2026-10-06 --report ~/campaign/pr-bundle-l1-2026-10-06-final.json`, single
process, `nice -n 10`, beside the running fold campaign. **4,152 of 4,156 bundles built, 0 member
failures.** The four not built are the four `PrBundle` refuses to date, the same four the
2026-09-08 close-out named: 2011-08-19 and 2013-06-04 (a stale previous-session member inside),
2013-01-10 (every member under a `nupr100113/` directory), 2018-01-02 (the archive served the
2019-01-02 bundle under that name). They are counted, never dated by their filename.

**A boundary found mid-build.** While the first full build ran (13:43-13:51 IST), another task
began writing NSE `prices_raw` partitions for 2006-2011 — sessions whose bhavcopy prints **no
ISIN**, so whatever ISIN those rows carry was assigned by the platform, not stated by NSE. A
same-session statement built from them would have passed a derivation off as the exchange's own
fact. `load_session_identity` now refuses any session before `ISIN_BHAVCOPY_START` (2011-06-22);
those sessions resolve through the master alone and say so (`resolved_via = identity_master`).
The first build had already read 2010-01..2011-06 before those partitions appeared, so its
numbers and the final build's agree (§7); the final build (14:06-14:28 IST) is what is on disk.

### 4a. The datasets

| dataset | from | rows | partitions | key | notes |
|---|---|---:|---:|---|---|
| `pr_band_hits`      | `bh` | 830,910 | 4,152 | ISIN | `side` H/L; 4,147 sessions with rows, 5 published empty (empty partition) |
| `pr_security_marks` | `Pd` security rows | 8,116,924 | 4,152 | ISIN | `corp_ind`, `nifty50_flag`, `section`, `hi/lo_52wk_published`, `prev_close`/`close` as a witness |
| `pr_index_eod`      | `Pd` index rows | 226,485 | 4,152 | `index_id` | 184 names; stable ids across the 2013-03-04 and 2015-11-09 renames |
| `pr_ca_broadcasts`  | `Bc` | 955,138 | 4,152 | ISIN | by `knowable_date` (the broadcast session); 2022-01-10 published empty |
| `pr_bundle_quarantine` | all three | 832,753 | 4,151 | — | `member`, `reason`, `detail`; written only for a session with something to hold |

Rows per year:

| year | band hits | security marks | index EOD | CA broadcasts | quarantined |
|---:|---:|---:|---:|---:|---:|
| 2010 | 8,026 | 226,735 | 2,754 | 51,203 | 170,386 |
| 2011 | 12,951 | 307,546 | 4,841 | 57,606 | 97,726 |
| 2012 | 27,944 | 391,191 | 7,088 | 62,189 | 36,983 |
| 2013 | 31,906 | 364,610 | 7,125 | 63,406 | 36,981 |
| 2014 | 59,484 | 386,388 | 7,623 | 60,429 | 34,778 |
| 2015 | 30,392 | 391,769 | 8,812 | 71,502 | 45,534 |
| 2016 | 30,078 | 409,163 | 10,781 | 69,278 | 32,787 |
| 2017 | 33,383 | 437,241 | 11,408 | 75,702 | 49,862 |
| 2018 | 58,147 | 461,167 | 12,158 | 73,304 | 44,140 |
| 2019 | 71,558 | 472,656 | 13,430 | 74,340 | 48,244 |
| 2020 | 111,052 | 498,644 | 14,176 | 70,538 | 47,727 |
| 2021 | 85,506 | 514,981 | 15,132 | 68,475 | 48,639 |
| 2022 | 65,913 | 556,879 | 17,644 | 57,011 | 45,299 |
| 2023 | 48,267 | 605,201 | 17,466 | 29,901 | 15,032 |
| 2024 | 62,070 | 693,104 | 19,655 | 33,548 | 15,942 |
| 2025 | 51,909 | 763,792 | 30,930 | 15,922 | 18,568 |
| 2026 | 42,324 | 635,857 | 25,462 | 20,784 | 44,125 |

### 4b. The unresolved share

**No row is dropped**: per member and per session `rows == written + quarantined`, asserted by the
tests and summed by the build report.

| member | published rows | written | quarantined | **unresolved share** | `EQ` only | after 2011-06-22 |
|---|---:|---:|---:|---:|---:|---|
| `bh` | 844,580 | 830,910 | 13,670 | **1.62%** | 2.04% | 112 rows (`symbol_unresolved`), ≈0.01% |
| `Pd` securities | 8,307,016 | 8,116,924 | 190,092 | **2.29%** | 2.52% | 35 rows, ≈0.0005% |
| `Bc` | 1,584,129 | 955,138 | 628,991 | **39.7%** | 6.87% | 581,488 `symbol_unresolved` |
| `Pd` indices | 226,485 | 226,485 | 0 | — | — | (no identity needed) |

Reasons, all enumerated (`QuarantineReason`): `no_session_statement` 251,118 (every one is
2010-01-04..2011-06-21, the 369 sessions with no ISIN-bearing bhavcopy, where the master knew no
window), `symbol_unresolved` 581,635, `ambiguous_master` 0.

How to read it:

- **`bh` and `Pd` resolve essentially completely wherever the session's own bhavcopy exists**
  (2011-06-22 onward). The whole loss is the 2010-01-04..2011-06-21 window, where only the master
  can answer and it is built from today's equity list: 36% of 2010's `Pd` rows and 54% of its
  band hits are delisted or renamed names it has never heard of. This is survivorship, measured,
  not hidden — the NIFTY 50 flag shows it directly: 50 resolved members on 3,259 sessions, but
  **43 on the 369 pre-ISIN sessions** (the parse itself reads 50 on every one of them).
- **`Bc`'s 39.7% is not an identity failure on equities.** The book lists debt (`N1`…`Z*`),
  mutual-fund (`MF`) and other series that did not trade that session, so the session statement
  is silent, and the master is deliberately never asked about a non-equity series (its windows
  are series-blind; it would put a debenture's interest payment on the issuer's equity ISIN).
  For `EQ` the unresolved share is 6.87%, falling to 0.4-0.6% in 2025-2026.
- Placed by source: security marks 7,779,281 by the session statement / 337,643 by the master;
  band hits 819,819 / 11,091; broadcasts 502,587 / 452,551 (a `Bc` row is for a future ex-date,
  so the security often trades under a different series that session and the master answers).

### 4c. What the datasets were checked against

| check | result |
|---|---|
| `IND_SEC=Y` set vs `ffix` NIFTY constituents, every shared session (2010-01-04..2013-04-30) | **830 of 830 identical** |
| NIFTY 50 count per `Pd` file (parse) | 50 on 3,628 files; 51 on 380 (all within 2016-04-01..2026-02-23); 49 on 144 (2023-07-13..2024-02-08) — published as such, not reinterpreted here (a dual-class constituent and a merger gap would each produce exactly this; not verified) |
| each aliased rename: the new name's first `prev_close` == the old name's last `close` | **10 of 10 equal** (S&P CNX Nifty→CNX Nifty→Nifty 50, CNX Nifty Junior→Nifty Next 50, BANK Nifty, CNX IT, CNX 100, CNX 200, S&P CNX 500→CNX 500→Nifty 500, India Vix→India VIX) |
| a case-variant name that is *not* a rename | `Nifty Midcap 100` (last close 12,752.60, 2016-03-31) vs `NIFTY MIDCAP 100` (first `prev_close` 18,757.00, 2018-04-02): kept as two ids — the first build folded case and spliced them; fixed before the final build |
| is NSE's published 52-week range adjusted? | **No.** INFY's 1:1 bonus, ex 2015-06-15 (`XDBO`): `HI_52_WK` stays 4,402.20 across the ex-date while the close halves to 990.45. Raw, as invariant #3 wants; never use it as an adjusted high |
| `CORP_IND` vs `corporate_actions` on the same `(isin, ex_date)`, EQ/BE | `XB` on 681 bonuses (119 `XB` with no CA row); `XR` on 473 rights (42 without); `XD`/`XDO` on 27,355 dividends; `XO` on buybacks 266, demergers 202, splits 93, schemes 62 — and on 7,861 sessions with no CA. **Splits are mostly unmarked** (276 of 399 split ex-dates carry no `CORP_IND`) |
| redundancy of `Pd` prices | §3 — byte-equal to `prices_raw` |
| rebuild determinism | sampled partitions of the four ISIN-keyed datasets re-hashed after the second full build: identical (§7) |

---

## 5. D7 — the PR-bundle move witness

**There are no `unexplained_move` flags in `quality_flag` today** (its 21,718 rows are
`ca_reconciliation` and `l2_continuity`; the sentinel's move rule has never been run over the
lake). So "how many current flags does it explain" is answered against the flags the rule
**would** raise: `python -m dataplatform.quality.pr_witnesses` runs `UnexplainedMoveRule` and the
new `MoveWitnessRule` over every NSE row of `prices_raw` in the ISIN era (2011-06-22 → 2026-10-01;
the 2006-2011 partitions another task is writing today are left out, §4), with every
`corporate_actions` ex-date as the explanation the move rule accepts. Read-only: no flag raised,
annotated or resolved.

| | count |
|---|---:|
| NSE close-to-close moves beyond 20% | 9,746 |
| explained by a `corporate_actions` ex-date | 666 |
| **would-be `unexplained_move` ERROR flags** | **8,517** (EQ 2,866) |
| …with at least one PR-bundle witness | **5,842 (68.6%)** — EQ 2,123 (74.1%) |
| best witness **strong** (a bonus/rights marker or a split/bonus/rights/scheme broadcast) | 559 — EQ 405 |
| best witness **band** (hit the band on the move's side) | 5,245 — EQ 1,712 |
| best witness **other** (`XO`, buyback broadcast) | 11 |
| best witness **weak** (dividend/interest only) | 27 |
| no witness | 2,675 — EQ 743 |

Witness occurrences: `band_hit:H` 3,284 · `band_hit:L` 1,980 · `ca_broadcast:SPLIT` 370 ·
`corp_ind:XB` 187 · `ca_broadcast:BONUS` 181 · `ca_broadcast:SCHEME` 11 · `ca_broadcast:RIGHTS` 9 ·
`corp_ind:XR` 8 · `XDB`/`XDBO`/`XDR` 5 · other-grade `XO`/`XDO` 53 · weak (`XD`, `XI`, and
dividend/AGM/interest/redemption/untagged broadcasts) 109.

What it means:

- **The 559 strong ones are corporate actions the `corporate_actions` table is missing on that
  date** — mostly face-value splits and bonuses, spread over 2011-2026 and heaviest in 2021-2022
  (63 and 71). They are the CA backlog this source can close, and the reason the witness exists. They are *not* resolved: a human (or the
  promotion task) has to land the action, and then the move rule stops raising on its own.
- **Band hits are an explanation of a different kind.** A move a hair over 20% on a 20%-band
  stock that closed limit-up rode the exchange's own cap — the case `circuit_bands` was designed
  for and nothing populates. A band hit on the *wrong* side never counts.
- Only **2,675 would-be flags (31%; 743 of them `EQ`) have nothing NSE printed behind them** —
  the set a human should look at first. 1,439 of the 8,517 are `BL` (block-window) rows, which is
  a series filter question for whoever wires the sentinel to `prices_raw`, not a data defect.
- `FVSPLT`/`FV SPLT`, NSE's abbreviation for a face-value split, matched no purpose tag before
  this task; it was the commonest untagged purpose on a flagged move's ex-date. `SPLT` now tags.

Wiring: `SentinelInput.move_witnesses` (new, defaulted) carries the witnesses;
`dataplatform.quality.pr_witnesses.read_move_witnesses` fills it from L1; the rule is one file
under `dataplatform/quality/rules/` and self-registers. Nothing calls `persist_findings` with it
yet — no sentinel caller exists for the move rule either.

---

## 6. Not done, and what I would take next

1. **`Bm` → a dated results calendar.** 158,347 board-meeting notices, each knowable on its
   bundle date, `COMPANY NAME  SYMBOL : BM DATE : BM PURPOSE` in free text. The only PIT-dated
   earnings-date source before the filings feeds begin; an event-driven strategy's prerequisite.
2. **Promote the strong witnesses.** The 559 strong-witnessed flags, the 119 `XB` and 42 `XR`
   ex-dates without a CA row, and `pr_ca_broadcasts` as a whole are the input the `Bc` →
   `corporate_actions` promotion task needs; that task still owns the taxonomy and the terms.
3. **`etf.UNDERLYING`** — 372 distinct underlying strings for ETFs, the only ETF→benchmark map.
4. **`fo` (2010-01-04 → 2018-12-21)** — the only F&O bhavcopy in the lake (`nse_fo_bhavcopy` is
   registered and was never fetched): futures, options, `futivx`, trade counts. Out of this
   task's equity scope.
5. **`An` 2010-2013** — announcements before `nse_announcements` L0 begins (2014).
6. **A point-in-time symbol master for 2010-01-04..2011-06-21** would recover the 190,057 `Pd`
   and 13,558 `bh` rows quarantined `no_session_statement` — the pre-ISIN bhavcopy cannot.
7. **`index_constituents`** could take a NIFTY 50 history from `pr_security_marks.nifty50_flag`
   now; not done, because what that table promises (and to whom) is M9.3's decision.
8. **Published duplicates are kept, not deduplicated.** On 2022-03-07 the `Pd` file prints its
   whole 71-index block twice, value-identical, so `pr_index_eod` holds two rows per index that
   session; and 6 `(session, symbol, series)` security rows appear in two sections (5 on
   2025-03-28, Next 50 reconstitution eve; 1 on 2023-04-13). A consumer keys on
   `(session, index_id)` / `(session, isin, series)` and takes either.

---

## 7. Determinism and provenance

- **Two full builds, one report.** The first (13:43-13:51 IST) and the final (14:06-14:28 IST) build
  reports agree on every per-member, per-year count (`~/campaign/pr-bundle-l1-2026-10-06.json`,
  `…-final.json`); the only code differences between them are the case-exact `index_id` and the
  pre-ISIN statement guard, which changes no count because the first build read 2010-2011 before
  the derived partitions existed. Partitions sampled from `pr_security_marks`, `pr_band_hits`,
  `pr_ca_broadcasts` and `pr_bundle_quarantine` were re-hashed after the final build: byte-identical
  to the first; the sampled `pr_index_eod` partition (2016-01-04, which carries `Nifty Midcap 100`)
  differs, as the id fix requires. `test_a_rebuild_is_byte_identical` asserts the same property.
- **Every reader re-swept the whole corpus** with the code on this branch: `Pd` 4,152 files /
  8,307,016 security rows / 226,485 index rows / 33,038 furniture lines, 0 failures; `Bc`, `bh`
  0 member failures in the build.
- **Fixtures** (`tests/fixtures/nse_pr_bundle/pd/<era>/`, `…/bc/<shape>/`) are reduced from the
  authoritative lake, zero requests, each member byte-for-byte with its sha256 in `manifest.json`
  and re-hashed by the tests. `PROVENANCE.md` lists them.
- **Regenerate** (read-only except the four `pr_*` datasets and the quarantine):

```text
uv run python -m dataplatform.ingest.pr_bundle_l1 --data-root /home/ubuntu/stock-manager/data \
  --from 2010-01-04 --to 2026-10-06 --report ~/campaign/pr-bundle-l1-<date>.json
uv run python -m dataplatform.quality.pr_witnesses --data-root /home/ubuntu/stock-manager/data \
  --from 2011-06-22 --to 2026-10-06 --out ~/campaign/pr-bundle-move-witness-<date>.json
```

The inventory in §2 came from a one-off read-only census script over the same 4,156 zips (member
names, first lines, line counts); §3 and §4c's cross-checks are DuckDB queries over L1 and a CSV
export of `corporate_actions (isin, ex_date, action_type)`. None of them wrote anything.
