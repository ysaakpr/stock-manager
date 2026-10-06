# Data study — multi-fund automatic thematic fund manager

**Date:** 2026-09-07 · **Scope:** data only. No code change was made and none is proposed here.
**Question asked:** what data must exist to build and honestly backtest a system that constructs and
manages multiple customised funds, each defined by a theme or thesis, automatically — and how much
of it do we already have, with what completeness over the last 20 years?

Six parallel read-only investigations produced this. Their full reports (≈500 KB, with every query,
probe and file:line citation) are at `/tmp/fund-data-study/`:

| Report | Bytes | Lens |
|---|---|---|
| `l0-raw-lake-inventory.md` | 39,250 | what raw data we possess, measured |
| `l1-l2-inventory.md` | 54,346 | curated stores, schema + per-year completeness |
| `spec-entity-baseline.md` | 74,178 | what the plan already commits to |
| `spec-answers-B1-B4.md` | 40,788 | depth mandate, multi-fund gap, source register |
| `backtest-data-contract.md` | 58,791 | what the engine demands; what breaks at 20 years |
| `multi-fund-data-requirement.md` | 204,919 | 62-entity requirement spec, 12 families |
| `india-acquisition-atlas.md` | 66,916 | ~113 live probes; verified depth per source |

Those files are outside the repo and will not survive a reboot. If this study is worth keeping,
they should be copied in under `ops/studies/evidence/`.

---

## 1. Verdict

**The 20-year backtest is achievable for prices and benchmarks, and is not achievable for
fundamentals, sector classification, or index membership.**

> **Amended 2026-09-08.** The index-membership half of that verdict is now wrong for **3.3 of the
> 20 years.** The `ffix` member of the PR bundle — already in L0 the whole time, registered in our
> own code as "Fixed income" — carries dated constituent membership with weightage for 17 indices
> on all 827 sessions of 2010-01-04..2013-04-30, and ten of those indices are sectoral, which
> makes their membership an exchange-published *dated sector assignment*. See
> `ops/gates/ffix-index-membership-census-2026-09-08.md` for the census, and rows 2 and 3 of §4
> below for what it does and does not change. The verdict stands unamended for 2013-2026, for
> fundamentals, and for anything outside an index. That split is the whole answer, and it
falls almost exactly along the line between *what an exchange publishes as a daily file* and *what
requires someone to have recorded a judgement on a date*.

Three findings reframe the question:

1. **We hold 10 years, not 20 — and that was the plan working correctly.** `EXECUTION_PLAN.md:10`
   commits to "10 years of corporate-action-correct history." The measured lake is
   2016-09-01 → 2026-09-04. Nothing is broken; the target was 10.
2. **Extending to 20 needs no constitutional amendment.** History depth appears nowhere in §1's
   Decision Register, and §1 states everything outside it "is derived and negotiable"
   (`EXECUTION_PLAN.md:20`). The owner's own D8 memo already settled this: *"It is silent on depth.
   The binding constraint is 10 years, set by M4.10's verify line"* (`HUMAN_DECISIONS.md:263-264`).
3. **Far more free history exists than the source register believes.** ~113 live probes found NSE
   bhavcopy reaching **1995**, delivery data reaching **2002**, and 25 years of NIFTY total-return
   index **in a single unauthenticated POST**. The register's own depth fields understate all three.

So the binding constraint was never source availability. It is **identity below 2011-06-22**, and
**the absence of any dated record of classification** at any horizon.

### The one hard wall

**NSE bhavcopy carries no ISIN column before 2011-06-22.** Pinned to the day by probe: 2011-06-21 is
11 columns, 2011-06-22 is 13 columns with ISIN, both HTTP 200 in the same session. BSE legacy carries
no ISIN in *any* era — only `SC_CODE`. Since invariant #2 makes ISIN the only join key, everything
before 2011-06-22 requires a point-in-time symbol→ISIN master that does not exist and cannot be
fully reconstructed: `symbolchange.csv` gives the rename chain, but `EQUITY_L.csv` is a *current*
listing, so companies delisted before today are simply absent from it.

This yields three honest horizons, and they should be named separately in every result:

| Horizon | Span | Cost to reach | What it supports |
|---|---|---|---|
| **A — now** | 2016-09 → 2026-09 (10.0 y) | held | prices, delivery, fundamentals FY2019+ |
| **B — cheap** | 2011-06-22 → 2026-09 (15.2 y) | ~2,670 requests, ~2 h, **zero new parser code** | prices, delivery, CAs, benchmark |
| **C — expensive** | 1995 → 2011-06-21 | new parser + a symbol→ISIN reconstruction with a permanent unresolved tail | price panel only, with a published refusal rate |

**Horizon B is the recommendation.** The production parser was run against the live 2011-06-22
payload during this study and parsed it clean — `refused=0 rows=1502 state=NORMALIZED`. The only
blocker is `nse_holidays.yaml`, whose coverage starts 2016-01-01 and which *refuses* rather than
guesses. Extending one YAML file plus one backfill run buys **a 52% increase in span**.

---

## 2. What we have, measured

### 2.1 L0 — the raw lake

96,097 payloads + 96,097 sidecar receipts, 1:1, zero orphans, 6.27 GiB. **Checksum verification:
96,097 checked, 0 defects.** Immutability is enforced in three independent layers (mode `0o444`,
writes refuse to overwrite, differing bytes at the same key raise), and `get()` re-hashes on read.

**13 of 30 registered sources have ever been materialised.** 77% of the lake by bytes is XBRL
filings.

Daily price coverage is *perfect over its span*: NSE 100% of every session 2016-09-02 → 2026-09-01
against the real NSE holiday calendar, with **zero unexpected dates** (no phantom files on closed
days; Muhurat handled). BSE **2,475 of 2,475 sessions, 0 missing**. Era splices are exact to the
session: NSE legacy ends 2024-07-05 / UDiFF begins 2024-07-08; MTO ends 2019-09-30 /
`sec_bhavdata_full` begins 2019-10-01.

The engineering here is not the problem. Depth is.

### 2.2 Derived stores

| Store | Rows | ISINs | Span | 20-year? |
|---|---|---|---|---|
| L1 `prices_raw` | 14,298,626 | 14,023 | 2016-09-01 → 2026-09-04 (2,475 dates) | **no — 10 y** |
| L2 `prices_adjusted` | 4,276,035 | 3,349 | 2016-09-02 → 2026-09-01, NSE only | **no — 10 y** |
| L1 `prices_raw_quarantine` | 1,794,742 | — | 2016 → 2026 | delivery rows never dropped |
| L1 `pit_fundamentals` | 1,349,562 facts / 102,650 filings | 2,291 | filings 2018-05-21 → 2026-09-05 | **no — 8 y** |
| PG `corporate_actions` | 47,887 | 3,624 | 2000 → 2026, **BSE-only pre-2016** | rows deep, PIT broken (D1) |
| PG `adjustment_factors` | 2,784 | 1,823 | 2000 → 2026 | **zero CA lineage** (D2) |
| PG `security_master` | 8,598 | — | 5,120 ACTIVE / 1,167 SUSPENDED / **2,311 DELISTED** | no dated history |
| PG `symbol_history` | 11,359 | 8,598 | `valid_from` from 1995 | append-on-rename works |
| PG `isin_lineage` | 399 | — | 2016 → 2026 | — |

Per-year price detail: **0 rows in every year 2006–2015.** 2016 (from 09-01) 373K rows / 4,507
ISINs, rising to 1.90M / 9,401 in 2025.

Corporate actions reach 2000 but the density collapse is real: 52–271 dividends/year market-wide
across 2002–2006, versus 3,000+ now. Pre-2016 is BSE-only (35,382 BSE vs 12,505 NSE). **Delisting
events stop at 2003-03-27 — zero recorded in 23 years**, despite 2,311 DELISTED securities in the
master. We know *who* died, not *when*.

### 2.3 Declared and empty

**Zero rows:** `case_`, `policy_set`, `thesis`, `decision_journal`, `order_`, `token_usage`,
`job_run`, `scheduler_heartbeat`. The multi-case machinery has never held a single case. The
scheduler has never run.

**Datasets declared in code with no directory on disk:** `index_constituents`, `benchmark_tri`,
`macro_series`, `announcements`, `shareholding`, `fii_dii_flows`, `news`, `deals`, `fo_contracts`,
`fo_aggregates`, `RESTATED/screener_fundamentals`.

The first two matter most: **there is no benchmark series in the lake, so there is currently nothing
to measure excess return against.**

### 2.4 Survivorship — better than it looks, but the filters are poisoned

Of 1,870 EQ ISINs trading in 2017, the fraction still trading each later year: 95.7, 92.1, 88.1,
85.6, 80.6, 77.2, 74.4, 71.1, **68.6% by 2026 — 31.4% attrition over 9 years, ~3.7%/yr.**

**The raw lake is not survivorship-biased.** A 2006 bhavcopy contains every security that traded
that day, including those that no longer exist. The bias enters *downstream*, through any universe
filter drawn from a current-day list — which is exactly what `bse_scrip_master` (3 files, one date)
and `nifty_index_constituents` (one snapshot) are today. The disease is in the filters, not the data.

---

## 3. Verified external depth

All labels below are **VERIFIED** — a request was issued 2026-09-07 and the bytes inspected — unless
marked BELIEVED.

### 3.1 Deeper than the register claims

| Source | Register says | Measured |
|---|---|---|
| NSE bhavcopy | `era.start: null` | **1995-01-02**; ISIN from **2011-06-22** |
| NSE MTO delivery | "at least 2016-01-04" | **2002-01-02** — a ~24-year series, 4 format eras |
| NIFTY 50 TRI | `status: FAILED` | **2001-04-02 → 2026-09-04, 6,321 rows, ONE POST, no cookie** |
| Thematic TRI (`NIFTY INDIA CONSUMPTION`) | — | **5,125 rows to 2006-01-02** |
| NSE announcements | never fetched | **2006-01**, ISIN-tagged, timestamped |
| F&O legacy bhavcopy | UDiFF era only | **2006-01-02** |
| BSE legacy bhavcopy | `era.start: null` | **2006-04-03** |
| `ind_close_all` index closes | — | **2012-10-01** |

### 3.2 `PR<DDMMYY>.zip` — the largest unexploited free source

NSE's daily report bundle, **2010-01-04 → 2026-09-04**, ~250–670 KB/session. No `pr`/`mcap`/`Ix`/`Bc`
string appears anywhere in `dataplatform/ingest/`. Seven datasets the repo has zero of, in one
campaign — the highest information-per-request in the entire atlas. But the three headline members
were probed hard, and two of the three hopes deflate:

- **`Bc<DDMMYY>.csv` — genuinely valuable.** A *rolling list of pending* corporate actions, not an
  "announced today" list: consecutive-session diffs show actions persisting and dropping off as
  their book-closure passes. Therefore **the first session on which an action appears is an observed
  upper bound on its knowable date.** Lead time of the 16 actions new on 2010-01-08: min 17, median
  27, max 42 days before ex-date. Present in every PR zip probed, 2010-01-04 → 2026-09-04. **This is
  the fix for defect D1, back to 2010.**
- **`Ix<DDMMYY>.csv` — nearly worthless.** Index membership *with weights and issue-cap*, but present
  only **2010-01-04 → 2010-10-08** (~190 sessions, both ends verified), and the index set changes
  monthly: five indices in Jan–Feb 2010, `CNX 500` alone Apr–Jul, two indices Aug–Oct. Useful as
  spot ground-truth for a reconstruction, not as a membership history.
- **`mcap<DDMMYYYY>.csv` — recent only.** Daily shares outstanding (`Issue Size`) + market cap, but
  **from 2024-02-01** (absent 2024-01-31). `Category` is only `Listed`/`Permitted` — **there is no
  `Delisted` category**; a delisted name just stops appearing. And `Last Trade Date` is an
  *illiquidity marker, not a delisting date*: one security still `Listed` in 2024 last traded
  2017-03-10, and the literal string `Not Traded` appears.

The PR archive also has genuine holes — `PR150110.zip` is 404 while that session's bhavcopy is 200 —
which widens any `Bc` first-appearance bound by the length of each gap.

### 3.3 Traps found live

- **`sec_bhavdata_full` returns HTTP 200 with the *previous* session's rows on a market holiday.**
  Republic Day 2026-01-26 → 200, 352,669 B, 3,102 rows, **every row dated 23-Jan-2026**. Siblings
  correctly 404. Our parser is safe by design (`delivery.py:210` reads `DATE1` from rows, not the
  filename) but **the sync layer will still mark 26-Jan `PUBLISHED`**. We are exposed today.
- **`ind_close_all`: 48 of 53 index names were renamed 2015-11-06.** Today's names read **zero rows**
  before that date. Needs an alias map applied *before* loading.
- **NSE CA API `caBroadcastDate` is null in every dated window, including 2026.** So D1 is a source
  limitation, not only a code bug — `Bc` is the only fix.
- **NSE FII/DII silently ignores `?date=`** and returns a byte-identical payload. Only one session
  exists, ever. **A missed day is permanent.**
- **BSE soft-404s with HTTP 200 + a 14,287-byte Angular shell.** Same failure class already burned us
  once: the `niftyprivatebank` constituents file in L0 is an HTML shell, and it is the one FAILED
  `sync_state` unit.
- **AMFI's semi-annual large/mid/smallcap classification lists 404, and no archive of past editions
  exists anywhere.** If Jan-2010's edition was not captured in Jan-2010, it is gone.

### 3.4 Effectively unobtainable free at 20-year depth

| # | Entity | Free reality | Realistic options |
|---|---|---|---|
| 1 | **PIT fundamentals with restatements** | NSE XBRL honestly **FY2018-19 →** (~101,446 usable; FY2017-18 are 100% placeholder) — **8 years** | **CMIE Prowess / Prowess dx** (~1989 →, keeps original-as-filed *and* restated *and* dead companies; IP-based institutional subscription, quoted) · **Capitaline** (20+ y) · LSEG PIT / Bloomberg CoFi PIT (~US$20k+/yr). **No reconstruction path exists.** |
| 2 | **Index constituent history** | as-of-today only; `Ix` for ~9 months of 2010. **MEASURED 2026-09-08 and revised: the `ffix` member of the same PR bundle carries the complete dated constituent list of 17 indices with free-float weightage on all 827 sessions of 2010-01-04..2013-04-30**, gapless against the calendar, every headline index at exactly its nominal size, 154 dated reconstitution events (`ops/gates/ffix-index-membership-census-2026-09-08.md`). It was already in L0, mislabelled "Fixed income" in `MemberKind`. Symbol-keyed, so unusable for an ISIN join until W4 | **Reconstruction works and is the recommended path**: walk backwards through NSE Indices' semi-annual reconstitution press releases (each names inclusions, exclusions, effective date), cross-check against the `ind_close_all` index-count series so an index is never placed before it existed, and against `symbolchange.csv` so a rename is not read as an add/drop. Days of desk work; the press-release listing is JS-rendered so needs a browser. Or licence from IISL / Prowess / Capitaline. |
| 3 | **Sector reclassification history** | every source carries *today's* sector, undated; NSE's taxonomy was itself overhauled around 2015 | Prowess (NIC-based, dated) · Capitaline · GICS via LSEG/MSCI. Partial free reconstruction from sectoral index membership covers only the 50–200 names in those indices — **nothing recovers the sector of a 2008 micro-cap.** **This row is now measured rather than estimated, and the estimate was right:** `ffix` gives ten sectoral indices' dated membership over 2010-01-04..2013-04-30, and that is **162 distinct symbols — 8.8% of the 1,842 that actually traded in the span**, with 42 holding more than one sector at once and 10 changing sector. The other 91.2% has no dated sector at any date (`ops/gates/ffix-index-membership-census-2026-09-08.md`) |
| 4 | **Free float / shares outstanding** | `mcap` total shares **2024-02 →**; `Ix` index-cap 2010–11; promoter/public split **~2022 →** | Prowess/Capitaline dated series; IISL free-float factor files. Free reconstruction: back out shares from `mcap` market-cap/close and walk the `adjustment_factors` chain to 2011-06-22 — gives defensible **total** shares, **not free float** (issuance to promoters/QIBs changes float with no price-adjusting action), and nothing for delisted names. |
| 5 | **Bid-ask / intraday microstructure** | **zero, at any horizon** | NSE DOTEX licensed tick (priced per segment per year) · TrueData / GDFL / AlgoTest resell minute bars ~INR 1–10k/mo. **No path from EOD to a spread.** Consequence: slippage must be *modelled*, conservatively enough that the edge survives the model being wrong. For 2006–2010 small caps an EOD backtest **cannot** tell you whether a position was fillable — enforce a hard turnover floor and treat sub-floor names as untradeable, not tradeable-with-slippage. |
| 6 | **Delisted price + fundamental history** | **prices: free and good** (L0 immutability saves us — fetch sessions, never query a "current universe" API). **Fundamentals: nil** — a company delisted in 2012 filed nothing in XBRL and is absent from Screener | Prowess keeps dead companies (its main selling point for survivorship work). For the pre-2011 ISIN gap: build a symbol→ISIN map *as of each date* from accumulated `EQUITY_L.csv` + `symbolchange.csv` + BSE scrip master (all three statuses — Delisted does carry ISIN) + the post-2011 bhavcopies' own symbol↔ISIN pairs, and **refuse the row rather than guess** where a symbol was reused. Publish rows-refused per year as the honest bound. |

**The single most important line in this table is row 3.** A themed fund is a point-in-time universe
rule; the theme label *is* the alpha; and there is no free dated record of what a company was
classified as on a past date. A company called "defence manufacturing" today was filed under
Industrial Products in 2012. Related trap: **thematic indices launched 2019–2024, and their
pre-launch "history" is back-computed using today's classification** — usable as PIT evidence only
from live-launch date forward.

---

## 4. Entity catalog — requirement vs. holding

The full requirement spec defines **62 entities across 12 families**, each with grain, primary key,
typed attribute list, PIT flags, 20-year volume estimate, acquisition path, and failure mode.
Priority split: **34 MUST-HAVE, 20 fidelity, 8 nice-to-have**, plus a 27-row register of places where
the 2006→2026 window genuinely breaks. Condensed status by family:

| Family | Entities | Status |
|---|---|---|
| Universe & identity | 8 | **PARTIAL.** Master + symbol history + lineage exist and work; no dated listing/delisting series; no PIT symbol master below 2011-06-22 |
| Prices & liquidity | 7 | **HAVE for 10 y, cheap to 15.2 y.** Free to 1995 without ISIN. Delivery to 2002 (4 eras, 1 handled). No spread at any horizon |
| Corporate actions & adjustment | 5 | **HAVE rows, BROKEN PIT** (D1) and **no lineage** (D2). `Bc` fixes PIT to 2010 |
| **Classification & thematics** | 9 | **ABSENT — the critical gap.** 42-ISIN 2026 fixture is all that exists (D6). No dated taxonomy free at any horizon |
| Fundamentals (PIT) | 7 | **HAVE 8 y, honestly FY2019+.** Pre-2019 is not shallow — it is look-ahead-biased. Ind AS FY2016-18 is a *definitional* break |
| Ownership & flows | 6 | **ABSENT.** Shareholding ~2022+ only; FII/DII one session ever and unrecoverable |
| Events & narrative | 6 | **ABSENT but cheap.** Announcements verified to 2006-01; `An`/`Bm` in PR zips from 2010 |
| Macro & benchmarks | 6 | **ABSENT, and one POST from 25 years** (D8) |
| Costs, frictions, tax | 4 | **PARTIAL.** Model is correctly time-varying; card starts 2017-07-01 (D5); 2 states only |
| **Fund & multi-fund** | 17 | **PARTIAL — see §6.** `cross_fund_exposure` absent entirely |
| Calendar & market structure | 4 | **PARTIAL.** Calendar 2016+ and refuses outside coverage; settlement hard-coded T+1 (D4) |
| Cross-cutting (registry, resolution, issuance, estimates) | 6 | mostly absent |

### The bitemporal envelope — adopt this as the acceptance test

The requirement spec's most useful contribution is a single testable rule. Every fetched row should
carry `knowledge_ts`, `effective_from/to`, `revision_no`, `superseded_by`, `l0_ref`; and every
strategy read must be `WHERE knowledge_ts <= :decision_cutoff` AND `max(revision_no)`.

**A table that cannot answer that query cannot be used in a backtest.** Our `corporate_actions`
table fails it today.

### Turning a theme into a PIT rule — four layers

1. **Structural** — taxonomy as-of date (the missing layer).
2. **Economic** — segment-revenue share. The only real measure of *true* theme exposure.
3. **Textual** — keyword/embedding over **dated** documents, never today's company description.
4. **Relational** — supply-chain linkage, for ranking only.

Plus a cheap, checkable guard worth adopting as policy: **a theme vocabulary may contain concepts,
products and programmes, but never company names.** A validator can enforce it and a reviewer can
verify it.

---

## 5. Defects found

These are independent of the 20-year decision. Several invalidate results already reported.

### Correctness — silently wrong answers

| # | Defect | Evidence | Effect |
|---|---|---|---|
| **D1** | **All 47,887 corporate actions share one `knowable_date` — today.** `announcement_date` NULL on every row | `bse/corp_actions.py:214` stamps `clock.now().date()`; NSE's `caBroadcastDate` lands on 0 of 12,505 rows and is null at source | A backtest honouring invariant #7 sees **zero CAs on every historical decision date**; one joining on `ex_date` uses look-ahead. Invariant #7 is satisfied *vacuously* |
| **D2** | `adjustment_factors.corporate_action_id` **NULL on all 2,784 rows** | `0001_init.sql:175` FK unused; 414 rows flagged `structural_break` | No factor traces to its cause; the adjusted series cannot be audited or rebuilt |
| **D3** | **Corporate actions are never applied to the book during a walk** | `apply_split`/`apply_bonus`/`apply_demerger` exist at `backtest/accounting.py:286+` with **no call site in any driver** | A holding through a 2:1 split keeps its pre-split share count, marked at the post-split close — **the position silently halves.** Only the signal is corrected |
| **D4** | **Settlement hard-coded T+1 for all history** | `execution/sim_broker.py:516-537` | India was T+3 to 2003, T+2 2003–2022, **and T+1 was phased per-security by market-cap rank 2022-02-25 → 2023-01-27** — so a global flag is wrong for 11 months, correlated with exactly the smallcaps a theme fund holds |
| **D9** | **Dividends never reach cash** | no dividend method in `backtest/accounting.py`; `tr_close` is computed, stored, and **consumed by nothing** | 20-year total return understated by the entire cumulative yield |
| **D11** | Holiday stale-data trap on `sec_bhavdata_full` | §3.3 | Friday's delivery filed under Monday, marked `PUBLISHED` |

### Invalidated reported results

| # | Defect | Effect |
|---|---|---|
| **D6** | **Sector map is 42 ISINs from `tests/fixtures/`, dated 2026, applied backward.** No as-of dimension, so reclassification is inexpressible; `run.py:2601` stamps `knowable_date=as_of` unconditionally, so **the PIT guard passes trivially on 2026 labels asserted knowable in 2017**; `run.py:2584` narrows the universe to mapped names. The docstring at `run.py:2502-2508` describes a `membership_asof` path that **was never written** | Every sector-rotation result is engine plumbing, not evidence about sector rotation |
| **D7** | **Index membership screen is a dead no-op.** `membership_asof` → `None` at all 18 probed dates; at `run.py:642-645` `None` means "do not narrow" | **Every "investable universe" figure ever reported was turnover-floor-only** |
| **D8** | **Benchmark has never been the real TRI.** `_resolve_benchmark` always takes the L1-proxy fallback | Every M9/M10/M12 benchmark figure — **including the M12.3 verdict on the >25% bar** — is struck against a proxy. Treat those verdicts as provisional |

### Governance

**D8's history is the item to look at hardest.** On 2026-08-10 the owner probed the TRI endpoint
live, confirmed 6,213 rows to 2001-04-02 in one keyless request, and recorded three binding
consequences: correct the register URL — *"That correction is what actually unblocks the graph; it
must not wait on the parser"* — split M3.9, and ingest the daily TRI. **None of the three happened.**
`source_register.yaml:715-716` still carries the stale `.aspx` path; there is no `M3.9.a`/`M3.9.b`;
no TRI row exists in `sync_state`. And **M3.9 is recorded `DONE`** with a `BUILD_STATE.json` note
asserting the source is "session-gated/FAILED" — *the premise D8 had disproved three weeks earlier*.
`M9.4` then built on that same disproved premise by design, dutifully disclosing the proxy in its
report. **The disclosure happened; the fix didn't.**

Also: **`BUILD_STATE.json` is stale** (98 tasks recorded vs 106 in `TASK_GRAPH.yaml`; M10.7, M12.1,
M12.2, M12.3 merged in git but absent from build state), and **1,827 lines of off-graph code**
(`backtest/forecast.py`, `forecast_run.py`, `policies/forecast_daily.py`) were committed under a bare
`[X2]` tag with no task id. And a stale citation propagating through the docs: `ops/BACKLOG.md:126`
cites `AGENTIC_CONTEXT §4.1`, **which does not exist** — the substantive text is `EXECUTION_PLAN §4.1`.

### Other

**D5** — rate card starts 2017-07-01 and *raises* below it (correct behaviour), leaving ~10
unpriceable months **even inside the current lake**; only 2 states encoded (`KA`, `MH`);
`account_state` hard-coded `"MH"`; both pre-2024 schedules self-labelled `provenance: reconstructed`.
Over a 20-year window the missing regimes are numerous: service-tax→GST (2017-07-01), state-wise→
uniform stamp duty (2020-07-01), three STT regimes, **LTCG exempt 2004→2018 then 10% then 12.5% from
2024-07-23**, dividend taxation inverting in 2020, buyback taxation inverting 2024-10-01. Hard-coded
2026 rates misprice half the backtest, **always flatteringly**.
**D10** — rails are wired into no backtest at all (`grep -rn 'RailEngine|check_order' backtest/
execution/` → nothing), so no backtest proves the unbypassable-rails invariant.
**D12** — four `CAST(... AS DOUBLE)` in the swing signal path (`run.py:3936-3939`); ranking-order
risk. Cost model and drawdown math are clean Decimal.

---

## 6. Multi-fund readiness

**A `case_` is most of a themed-fund mandate already.** It carries `theme`, `horizon_years`,
`benchmark_primary`/`secondary`, `funding_mode` (PAPER/REAL), a SIP schedule and an 8-state
lifecycle; `policy_set` versions the rails per case, append-only and DB-enforced, so *"which rails
were in force when that order was placed"* stays answerable years later; `thesis` makes a CORE buy
impossible without a RATIFIED thesis. **N funds genuinely coexist** — `case_id` partitions policy,
thesis, orders, journal and token spend, and two cases can hold the same ISIN under different theses
and different rails.

**But the backtest stack is single-book.** `grep -rn '\bfund_id\b'` across `analyst/ backtest/
execution/ accounting/ dataplatform/` → **zero hits**. 23 structures need a fund dimension.
`CROSS_CASE_CONCENTRATION` (`analyst/rails/engine.py:257-273`) is already designed for N books and
takes a `HouseholdExposure` that **nothing in `backtest/` builds**.

**Missing entities, with nothing close in the repo:**

| Missing | Nearest thing |
|---|---|
| **Unit ledger / NAV per unit** | Nothing. No `nav`/`unit`/`units_outstanding` column in any of the 10 migrations. **A case is a rupee book with no denominator** |
| **External subscriptions / redemptions** | `sip_amount_inr` models exactly one owner-funded stream; `PortfolioBook._external` has the right shape but no `case_id`, no investor id, in-memory only. **No investor entity exists at all** |
| **Per-fund fee accrual** | Nothing. Cost model is per-*trade* only — no management fee, expense ratio, or performance fee |
| **Benchmark attribution** | `BenchmarkComparison` gives a single excess number; no allocation/selection/interaction decomposition |
| **Investor-level tax lots** | `BookPosition` deliberately stores **one blended basis per ISIN**, making FIFO/LIFO and STCG/LTCG classification impossible as built. `analyst/rails/policies.py`'s `Lot` is a risk-concentration unit with no acquisition date and no basis |
| **Persisted position / cash** | Position derived by summing fills; cash in memory only |

**And the entity that makes this genuinely multi-fund, absent entirely: `cross_fund_exposure`.**
Six thematic funds each taking 8% of a smallcap's ADTV is 48% of ADTV. **Six individually-plausible
backtests that were never simultaneously achievable is the defining failure mode of this product,
and it is invisible to per-fund testing.**

---

## 7. Acquisition plan

Assumptions: ~250 NSE sessions/year; 2.6 s/request per host (register policy is 2.5 s); one campaign
at a time on this box (4 cores) per `CLAUDE.md`; check `uptime` and `ps aux | grep -E
'backfill|campaign'` first; `nohup … &` with a dated log under `~/campaign/`.

**Governance:** `AGENTIC_CONTEXT.md:88` reserves *any bulk-fetch campaign over ~200 requests to one
source* to the human. **Every wave below except W0 and W8 needs owner sign-off.**

| Wave | What | Requests | Wall clock | Why |
|---|---|---|---|---|
| **W0** | NIFTY 50 / IT / CPSE TRI + price history at full depth; extend `nse_holidays.yaml` to 2006; add pre-2017 cost regimes | **3–6** + desk | minutes | 25 years of benchmark in one POST each. Fixes D8. Nothing downstream can be scored honestly without it |
| **W8** | **Start the daily snapshotter** — FII/DII, bulk/block deals, ASM, GSM, `sec_list` bands, constituents, `EQUITY_L` | ~20/day | ongoing | **None of these has a past.** Every day the scheduler doesn't run is a day of history destroyed. No sign-off needed |
| **W1** | NSE legacy bhavcopy 2006-01 → 2016-09 | ~2,670 | ~2 h | The price spine; ~135 MB. Quarantine era E1 rather than refuse the session. By-product: the exact trading calendar (404 = holiday) |
| **W2** | `PR{DDMMYY}.zip` 2010-01 → 2026-09 | ~4,175 | ~3 h | ~1.5 GB; seven datasets we have zero of. **Highest information-per-request in the atlas.** Delivers `Bc` → fixes D1 back to 2010 |
| **W3** | MTO delivery 2006-01 → 2019-09 | ~3,440 | ~2.5 h | Extend `mto.py` for era M4 (repeated settlement blocks) first; decide era M1's fate |
| **W4** | Identity: BSE scrip master ×3 statuses; `EQUITY_L.csv`; `symbolchange.csv`; per-scrip BSE CAs | ~250 | ~30 min + desk | **Publish the unresolved-row count per year** — that number is the honest bound on how far back we can claim to reach |
| **W5** | CA reconciliation + adjustment factors | ~250 | ~20 min | Fix `corp_actions.py:191-192` first — `clock.now().date()` must become explicit UNKNOWN/quarantine. Fixes D1/D2 |
| **W6** | `ind_close_all` 2012-10 → 2026-09; start the monthly constituent job; begin the backwards press-release reconstruction | ~3,475 | ~2.5 h | Map the 2015-11 rename through an alias file **before** loading |
| **W7** | NSE announcements, monthly windows 2006-01 → 2026-09 | ~250 | ~20 min | 20 years of ISIN-tagged timestamped disclosure, ~250 MB. Flag the pre-2007 midnight-only `an_dt` era |
| **W9** | Optional: F&O legacy (~4,620 req); BSE legacy bhavcopy (~4,575 req, needs W4); AMFI; SEBI/RBI | — | — | BSE legacy needs identity first — no ISIN in any era |

**Waves 0–7 total: ~14,500 requests, ~11 hours wall clock at policy spacing, ~2 GB of L0.**

**W8 is the only item with a deadline.** Industry classification files, index constituent lists,
ASM/GSM/ESM lists, price bands, `EQUITY_L.csv` and the Kite instrument dump are snapshot-only with no
archive. **Their history is being lost at one day per day.** The job is already built with
integration tests (`constituents_snapshot_job.py`) and has run zero times. A daily snapshotter
costing an afternoon is worth more to the 2031 system than any retrospective effort will then be
able to buy.

---

## 8. Decisions needed

1. **Confirm the target horizon.** Recommendation: **Horizon B (2011-06-22, 15.2 years)**. Zero new
   parser code, one YAML edit, ~2 h of fetching, +52% span. Horizon C (1995) buys a price panel with
   a permanent unresolved identity tail — worth doing only if a long price history is wanted for its
   own sake.
2. **Approve W0 now** — 3–6 requests, minutes, fixes the benchmark defect and unblocks honest
   scoring of everything else. This is the highest value-per-request action available.
3. **Approve W8 now** — no sign-off needed, but it needs a decision to *start*, and it is the only
   irreversible item.
4. **Decide on paid fundamentals.** Free XBRL is honest only from FY2019. A 20-year
   fundamentals-driven backtest is not shallow — **it is look-ahead-biased**, because the only
   pre-2019 source (Screener) is restated with no as-of date. If thesis-driven thematic funds are to
   be qualified on 20 years, **CMIE Prowess is the realistic answer** and §3.9 reserves spending to
   the human. Otherwise cap fundamentals strategies at FY2019+ and say so in every report.
5. **Decide the classification strategy** — the crux for thematic funds. Three options: buy dated
   classification (Prowess/Capitaline), fund the press-release reconstruction as desk work (days of
   effort, genuinely works for index-member names, recovers nothing for micro-caps), or stamp every
   result `classification_pit = FALSE` and never report index-relative alpha. **Recommendation: run
   the reconstruction for index members and cap the thematic universe at names it covers** — that
   keeps the product honest without a vendor contract.
6. **Schedule the defect fixes independently of the 20-year decision.** D1, D2, D3, D9 and D11 are
   wrong *today*, at 10 years. D3 (positions silently halving through splits) and D9 (dividends never
   reaching cash) are the two that most distort reported returns.
7. **Decide whether "fund" is a new abstraction or an extension of "case."** Recommendation: extend
   `case_`, since it already provides theme, benchmark, versioned rails, lifecycle and cross-case
   concentration. The additive work is a unit/NAV ledger, an investor entity, fee accrual, tax lots,
   and a fund dimension across 23 structures — plus **`cross_fund_exposure`, which should be built
   before any multi-fund result is believed.**

---

## 9. What to build first, if the answer is "all of it"

An honest ordering that keeps every intermediate state publishable:

1. W0 + W8 (hours) — real benchmark; stop destroying snapshot history.
2. Fix D1/D2 with `Bc` from W2, and D3/D9/D11 in the book and sync layers.
3. W1 + W3 + W4 — extend to Horizon B and **publish the per-year unresolved-row count**.
4. W2 + W5 — PR bundle and CA reconciliation; PIT-correct corporate actions to 2010.
5. W6 + W7 — index levels, constituent accumulation, the press-release reconstruction, announcements.
6. Wire rails into the backtest (D10); add the fund dimension and `cross_fund_exposure`.
7. Only then qualify a thematic strategy — and label its classification-PIT status on every result.

**Nothing in this study requires a code change to be decided.** Everything above is a data decision
or a defect ticket.
