# Macro, news and geopolitical ingestion — status, plan, storage, backtest

**Date:** 2026-09-07. Written against the working tree at `1e2d40b`, the register at
`dataplatform/ingest/source_register.yaml`, and the lake as it actually is on this laptop.

Answers four questions: is the economic/news fetch ready, what is captured today for the
socio-economic and geopolitical backdrop, how the data gets ingested and stored, and what of it
can honestly be backtested.

---

## 1. Status, measured

**The code is built and green. Not one row has ever been fetched.**

| Piece | Task | State | Data in the lake |
|---|---|---|---|
| GDELT 2.0 export ingest (`ingest/gdelt.py`, 405 ln) | M6.1 | DONE `b416064` | **none** — no `data/L0/gdelt/` |
| Curated RSS ingest (`ingest/rss.py`, 331 ln) | M6.1 | DONE `b416064` | **none** — no `data/L0/curated_rss/` |
| L1 `news` store (`ingest/news.py`) | M6.1 | DONE | **none** — no `data/L1/news/` |
| News → ISIN linkage + break matcher | M6.2 | DONE `053578a` | n/a (precision 1.00 / recall 0.91 on a 60-item labelled sample) |
| PIT macro store (`store/macro_series.py`) | M11.1 | DONE `af5da21` | **none** — no `data/L1/macro_series/` |
| Index valuation parser (`ingest/macro/index_valuation.py`) | M11.1 | DONE `af5da21` | **none** |
| Index name history (`macro/index_aliases.yaml`, 23 rows) | M11.1 | DONE | n/a |
| Index valuation backfill (~2,470 sessions) | **M11.2** | **NEEDS_GO — never started** | — |

`uv run pytest tests/unit/test_news_ingest.py tests/unit/test_macro_series.py
tests/unit/test_linkage.py -q` → 50 passed.

### Why nothing has been fetched

Three separate reasons, and only one of them is a missing source:

1. **The daily pipeline fetches exactly one source.** `dataplatform/ingest/eod.py:100`:
   `DAILY_NSE_SOURCES = (NSE_BHAVCOPY,)`. Everything else — announcements, flows, F&O, news,
   macro — is not in it.
2. **The scheduler registers four jobs**, none of them news or macro
   (`dataplatform/scheduler/registry.py`): `eod_pipeline`, `constituents_snapshot`, `l0_verify`,
   `identity_refresh`. There is no job that would ever call `gdelt.ingest_slice` or
   `rss.ingest_feed`, so "is the fetch job ready" is answered by: **the fetcher is ready, the job
   does not exist.**
3. **M11.2 is a B1 bulk campaign** and needs the owner's go. It has never been requested.

### Five VERIFIED daily sources have never been fetched once

This is the finding that reframes the whole question. The register carries these as `VERIFIED`
with a live 200 — and the lake has no directory for any of them:

| Register row | Status | Carries | L0 |
|---|---|---|---|
| `nifty_index_close_snapshot` | VERIFIED | P/E, P/B, Div Yield **and `GSEC10` 10Y yield** for 165 indices, back to 2012-10-01 | **absent** |
| `nse_fii_dii_flows` | VERIFIED | daily foreign/domestic institutional net flow | **absent** |
| `nse_fo_bhavcopy` | VERIFIED | F&O EOD incl. `FUTIVX` (India VIX futures), OI, PCR inputs | **absent** |
| `nse_bulk_deals` | VERIFIED | daily bulk deals | **absent** |
| `nse_block_deals` | VERIFIED | daily block deals | **absent** |

The ingesters exist (`ingest/nse/fii_dii.py`, `fo_bhavcopy.py`, `deals.py`,
`indices.py:1151`). The store side exists (`store/fo_aggregates.py`). Nothing calls them on a
schedule.

### The macro row is not ratified yet

EXECUTION_PLAN §12 (2026-09-04) carries **§4.1 row 17 "Macro / economic backdrop" as PROPOSED,
awaiting owner ratification.** §4.1 v1.0 has no home for macro. Until that line is ratified,
`worldbank_indicator_api` and `alfred_series_vintage` sit under a row the constitution does not
have, and nothing new can be registered legitimately. **This is a one-line decision and it blocks
everything below.**

---

## 2. The constraint that shapes the whole design

Two ratified positions bound what news and macro are *allowed* to do, and they are the reason
this plan is organised the way it is rather than by topic.

**News is evidence, not signal** (HUMAN_DECISIONS D6, ratified 2026-08-10):

> M6.1 is **not** on the T0 mechanical decision path — BC3 triggers from M3.8 via M5.11 — so
> invariant #7 does not depend on this task. […] News enters only as evidence-bundle content shown
> to the LLM, downstream of ratified break conditions and deterministic rails.

**LLM judgment cannot be replayed** (EXECUTION_PLAN §7):

> LLM-dependent decisions (T1/T2) can't be truly replayed historically — backtests validate the
> *mechanical* skeleton […] agent judgment is evaluated forward, in paper mode.

Together: **a headline-driven or tone-driven strategy is not backtestable under the current
constitution, and is not permitted to trade mechanically either.** Any plan that quietly assumes
otherwise is proposing a §1 amendment without saying so.

That is not a dead end. It is a fork, and the fork is the plan:

* **Backdrop-as-evidence** — recency and breadth matter, current vintage is fine, no PIT
  obligation, no backtest. This is news, and it is cheap.
* **Backdrop-as-mechanical-input** — a regime, a filter, a position-size scalar. Fully
  backtestable, but *only* from series with true point-in-time release dates. This is macro, and
  the scarce resource is not the data — it is the vintage.

---

## 3. What is captured today for the socio-economic and geopolitical backdrop

**Nothing.** In full, so it is not overstated:

| Backdrop dimension | Source status | In the lake |
|---|---|---|
| Monetary policy (RBI) | `curated_rss` on `www.rbi.org.in` VERIFIED, active in `rss_feeds.yaml` | none |
| Global events / geopolitics | `gdelt_v2_event_files` VERIFIED | none |
| Government policy (PIB) | curated but **inactive** — feed carries no per-item timestamp | none |
| India finance press | `economic_times_markets`, `business_standard_markets` **inactive** | none |
| Inflation (CPI), IIP, GDP, WPI | **no working source** | none |
| Policy rate as a series | **no working source** | none |
| USD/INR, crude, gold | **no source registered** | none |
| India VIX | via `nse_fo_bhavcopy` `FUTIVX` (futures, not spot) | none |
| Market/sector valuation, breadth | `nifty_index_close_snapshot` VERIFIED | none |
| FII/DII flows | `nse_fii_dii_flows` VERIFIED | none |

### The curated feed set drifted from what was ratified

D7 (answered 2026-08-10 by the owner) states the set plainly:

> Business Standard is **removed** from M6.1's curated feed set — not parked as a non-VERIFIED
> row, not substituted. Moneycontrol, ET Markets, Livemint and PIB are the set.

What shipped in `dataplatform/ingest/data/rss_feeds.yaml`:

| Ratified | Shipped |
|---|---|
| Moneycontrol | **absent** — no feed entry, no register row, no host row |
| ET Markets | present, `active: false` |
| Livemint | **absent** — no feed entry, no register row, no host row |
| PIB | present, `active: false` (no per-item timestamp — a defensible, documented block) |
| Business Standard — *removed* | present, `active: false` — **parked, which the decision forbade** |
| — | `rbi_press_releases`, `active: true` — the only live feed, and not in the ratified set |

Two of the four ratified feeds were never implemented, and the one the owner ordered removed was
parked instead. The PIB block is legitimate and well-evidenced (an item that cannot be dated from
the source cannot be PIT-usable, and this pipeline never dates an item from the wall clock). The
rest is drift.

### GDELT will not carry the India signal on its own

From the same decision, measured at the time: GDELT's GKG sample was *"Australian and US local
crime, zero India-finance content"*, and the schema has **no ticker or ISIN field** — only fuzzy
free-text org names. GDELT is a geopolitics and global-attention feed. It is not an India equity
news feed, and treating it as one is how the news dataset ends up large and useless.

---

## 4. The design decision: capture the transmission channel, not the event

International politics moves Indian equities through a small number of observable prices. You do
not need to measure a war, a tariff or an election to trade the consequence — you need the
consequence, dated honestly.

| What happens in the world | How it reaches Indian equities | The observable, PIT-clean series |
|---|---|---|
| Fed turns hawkish, global risk-off | EM outflows | **FII net flow** (`nse_fii_dii_flows`) |
| Conflict, sanctions, OPEC action | crude → inflation, current account | **Brent/crude** *(source needed)* |
| Dollar strength, rate differentials | imported inflation, flow reversal | **USD/INR** *(source needed)* |
| Global fear | volatility repricing | **India VIX** (`FUTIVX` proxy today) |
| Rate expectations, fiscal stress | discount rate | **`GSEC10`** — inside `ind_close_all` |
| Domestic cycle, earnings repricing | multiple expansion/compression | **index P/E, P/B, Div Yield** |

**Four of these six come from sources already VERIFIED**, and three of the four arrive in the same
file: `ind_close_all_<DDMMYYYY>.csv` carries the valuation columns *and* the `GSEC10 NSE Index`
row. That file is what M11.2 backfills. Two sources are missing entirely (USD/INR, crude) and are
probe tasks, not campaigns.

This is the highest-value insight in the plan: **the geopolitical backdrop is one owner `go` and
two source probes away from being a daily, PIT-correct, 13-year, fully backtestable panel** —
without a single line of sentiment analysis and without touching a §1 decision.

---

## 5. The ingestion plan

Tiered by **backtestability**, because that is the property that determines what a series may be
used for, and it is invisible if you organise by topic.

### Tier A — daily, PIT-native, backtestable, source already VERIFIED

Release date equals the session; published after the close; immutable once published; never
revised. `MacroFact.revision_seq` is 0 for all of them, forever.

| Series family | `series_id` shape | Source | Depth | Volume |
|---|---|---|---|---|
| Index P/E, P/B, Div Yield (165 indices) | `IN.NSE.NIFTY_50.PE` | `ind_close_all` | **2012-10-01** measured | ~2,470 sessions |
| 10Y G-sec yield | `IN.NSE.GSEC10_NSE_INDEX.CLOSE` | same file | same | free with the above |
| Sector valuation dispersion, breadth | derived → **L2** | same file | same | offline |
| FII / DII net flow | `IN.NSE.FII.NET_INR_CRORE` | `nse_fii_dii_flows` | probe required | daily forward + backfill |
| India VIX (futures proxy) | `IN.NSE.INDIAVIX.FUT_CLOSE` | `nse_fo_bhavcopy` | probe required | daily forward |
| PCR, aggregate OI | derived → **L2** | `fo_aggregates.py` | — | offline |

**M11.2 is the whole of row 1–3 and it is one decision away.** ~2,470 requests to
`nsearchives.nseindia.com`, one small CSV each, resumable and checkpointed per session. Same shape
as the M1.13 price backfill. **No format eras** — the 13-column header is byte-identical across
13+ years, which is unusual for this platform and makes this the cheapest high-value campaign left.

The one real hazard is identity, and it is already handled: NSE/IISL renamed **48 of 53 indices in
a single event between 2015-11-06 and 2015-11-10**. A consumer keyed on today's names reads *zero*
rows from any earlier file — not an error, zero rows, three years silently gone.
`index_aliases.yaml` holds 23 evidence-backed renames; **26 pre-2015 names resolve to themselves**
rather than being guessed. The backfill must report unmapped published names separately — that
list is the input to widening the history, and it is already in M11.2's acceptance.

### Tier A′ — two probes, then Tier A

| Series | Candidate | Note |
|---|---|---|
| USD/INR reference rate | FBIL, or RBI (`www.rbi.org.in` already has a host row and a robots record) | RBI's DBIE presents a **certificate hostname mismatch** — do not work around it; probe the website/FBIL path instead |
| Brent / crude, gold | **open licence question** | yfinance is used for *sanity checks* (`ops/sanity/l2_yahoo_check.py`); storing it in L0 as a series is a different question and needs a §10 call. MCX settlement prices are the India-native alternative and are unprobed |
| India VIX **spot** history | NSE | the `FUTIVX` future is a proxy; spot is better and its archive is unprobed |

Each is a ≤10-request probe under C.1's sweep method. None is a campaign.

### Tier B — real economics, revised, and **not backtestable before capture day**

CPI, IIP, GDP, WPI, trade balance, GST collections, repo rate.

The honest position, and it is the most important sentence in this plan:

> **There is no free historical-vintage source for Indian macro series. You cannot backtest CPI or
> IIP over the past decade without fabricating release dates. What you can do is start capturing
> vintages today, and accept that the backtestable window begins on capture day.**

The evidence, from M11.1's probe (34 requests, 7 hosts):

| Surface | Result |
|---|---|
| `cpi.mospi.gov.in` | connect timeout |
| `www.mospi.gov.in` | robots.txt is a **soft 404** — 200 carrying the site's HTML shell |
| `dbie.rbi.org.in` | **TLS certificate hostname mismatch** |
| `alfred.stlouisfed.org` | **unreachable from this host**, cause undetermined |
| `api.worldbank.org` | 200, keyless, 66 annual points — **but no vintage parameter** |

World Bank is storable only with `release_date` = the envelope's `lastupdated`, which makes every
fact knowable *now*. Fine as present-day backdrop; useless for a historical backtest. The store's
release-date partitioning is what keeps that enforceable rather than a comment.

**ALFRED is the one surface that would change this answer.** It serves a series *as it was
published on* a past date — literally the `release_date` this store partitions by. It failed from
the laptop with `last_http_status: 0` (a documented encoding meaning *requested, no HTTP response
arrived*): TLS establishes, then nothing arrives, across httpx/HTTP-2, curl/HTTP-2 and
curl/HTTP-1.1, inside and outside the build sandbox, while a control request to
`api.worldbank.org` on the same network returned 200. That is a network-path signature, not an
application refusal.

**One re-probe from the campaign server settles it.** The server is quiet right now (`ops/remote.sh
status`: 0 drivers). This is the single highest-information-per-request action available and it
costs one command.

So Tier B splits:

* **B1 — forward vintage capture (do this regardless).** A weekly job that fetches each Tier B
  release and writes a `MacroFact` with the publisher's stated release date. The precedent is
  already in the codebase: `constituents_snapshot` exists purely to accumulate forward history
  that no retrospective source can provide. Same logic, same shape, same honesty.
* **B2 — historical vintages, if and only if ALFRED answers from the server.** Otherwise Tier B
  carries a hard "backtestable from `<capture-start>`" boundary that the store enforces by having
  no earlier partitions at all.

### Tier C — news and geopolitics: evidence only, never a mechanical signal

| Feed | Action |
|---|---|
| `rbi_press_releases` | **keep active** — the only live feed, monetary-policy dense, well-dated |
| Moneycontrol, Livemint | **implement** — ratified in D7, never built. Host row + robots record + register row each |
| ET Markets | **activate** — feed entry exists, carries per-item `<pubDate>`, needs its register row |
| Business Standard | **delete the entry** — D7 said removed, not parked. 403 from a WAF is a refusal and §8 forbids working around it |
| PIB | **stays inactive** until a timestamped endpoint is found. Note it serves Hindi items — normalisation must handle or explicitly filter non-English |
| GDELT export | **daily forward only.** Do not backfill without a separate decision (see below) |

**The GDELT backfill is not a small campaign, and the arithmetic should be on the table before
anyone proposes it.** GDELT 2.0 publishes a new slot every 15 minutes — 96/day. From its 2015
start to today is ~4,220 days: **~405,000 slot files at ~75 kB ≈ 30 GB**, against 112,870 files
for the largest campaign this repo has run (`nse_xbrl_filing`). It would be the largest fetch in
the project's history, for a feed whose India-finance density was measured as approximately zero
and which is not permitted to drive a decision. **Recommendation: daily forward capture only.** If
historical global attention is ever genuinely wanted, GDELT's own aggregate masterfiles are the
route and their shape needs a probe first — not 405,000 individual slots.

---

## 6. How the data is stored

Already designed and built. Nothing here is a proposal.

### L0 — immutable raw

```
data/L0/<source>/<yyyy>/<mm>/<filename>
data/L0/<source>/<yyyy>/<mm>/<filename>.meta.json    # sha256 + provenance + fetch time
```

Write-once: payload and sidecar are created `O_CREAT | O_EXCL`, so the kernel refuses the second
write. `L0Store` refuses to modify a stored payload for any reason including corruption
(AGENTIC_CONTEXT §3.10) — re-fetching over a defect would destroy the evidence. `l0_verify`
re-hashes against the sidecars weekly. Every L1 and L2 value must be re-derivable from here, which is why a
parser fix is never a reason to re-fetch (memory: *downloads are independent of parse fixes*).

### L1 macro — `macro_series`, partitioned by release date

```
data/L1/macro_series/date=<release_date>/part.parquet
```

`MacroFact` (`dataplatform/ingest/macro/models.py`), `decimal128(38, 6)`:

| Field | Meaning |
|---|---|
| `series_id` | canonical dotted `<COUNTRY>.<PUBLISHER>.<SUBJECT>.<MEASURE>`, e.g. `IN.NSE.NIFTY_50.PE`. Prefix-selectable: every index P/E is `IN.NSE.*.PE` |
| `period_start` / `period_end` | **what the number describes** |
| `release_date` | **when it became knowable** — the partition key |
| `revision_seq` | 0 is the first print; a same-day republication increments |
| `frequency` | DAILY / WEEKLY / MONTHLY / QUARTERLY / ANNUAL / **EVENT** (a policy rate has no cadence; forward-filling between meetings is the reader's job, not the store's) |
| `unit` | PCT / BPS / RATIO / INDEX / INR_CRORE / COUNT — so a consumer never infers denomination from a name |
| `value` | `Decimal`, never `float` |
| `source`, `l0_key` | traceable to the bytes |

Two guarantees carry the whole design:

1. **Partitioned by `release_date`, so `read_pit(on_date)` chooses partitions.** A release made
   after the as-of date is *physically absent* from the result, not filtered out by a `WHERE` a
   caller might forget. Invariant #7 made structural.
2. **A revision is a new record, never an overwrite.** IIP and GDP are revised routinely.
   Overwriting would destroy the number the market actually saw and fabricate a knowable date it
   never had. `read_pit` returns every version knowable as of a date; `read_latest` collapses to
   best-knowledge-then without the store having discarded anything.

Two validators worth knowing about, because they will reject real parse bugs:
`release_date < period_end` raises (*"a figure cannot be knowable before the period it measures has
ended; if the source really dates it that way, the parser read the wrong column"*), and a fact
whose `release_date` disagrees with its `MacroRelease` raises (*"a fact in the wrong partition is a
PIT leak"*).

**No macro table in Postgres, and that is correct** — no migration through `0010` mentions macro.
L1 is parquet; Postgres holds masters, `sync_state`, the journal and quality flags.

### L1 news — `news`, partitioned by ingest date

```
data/L1/news/date=<logical_date>/part.parquet
```

`NewsRow(ts, source, title, url, entities, tone)`. Three properties matter:

* **`ts` is required and tz-aware.** A naive datetime raises: *"a source time must be tz-aware to
  be point-in-time usable"*. This is why PIB is inactive — no per-item timestamp, so no PIT-usable
  row, and this pipeline never dates an item from the wall clock.
* **There is no `body` field.** The licence note is headlines + links only, and the schema enforces
  it structurally — an RSS parser physically cannot smuggle an article into L1 even by accident.
  `tests/unit/test_news_ingest.py` asserts the absence. **This is an acknowledged one-way door**
  (D6): article text can never be backfilled later. If an evidence bundle ever needs more than a
  headline, that is a new decision and a new source.
* **`entities` is unresolved source-native org text.** ISIN resolution is M6.2's job, under a
  tighter contract (alias table, labelled precision/recall). A half-resolved entity field next to
  the ISIN-only-joins invariant is worse than an unresolved one.

An empty batch writes an empty, schema-correct partition, so *"ingested, nothing new"* is
distinguishable from *"never ingested"*.

### L2 — derived, rebuildable, never source of truth

Nothing built yet. **This is where a regime label belongs**, and the reason is worth stating: a
stored label you cannot reconstruct from its inputs cannot answer the only question that matters
when it changes — *did the world move, or did the threshold?*

```
data/L2/macro_regime/date=<session>/part.parquet     # rule_version + inputs + label
```

Every L2 row carries the version of the rule that produced it and is rebuildable from L1 by
replaying that version.

---

## 7. How to backtest it

The mechanism already exists and is stronger than a convention.

`backtest/replay.py:374` constructs a fresh `PitContext(as_of=session)` per session and hands it to
the policy as the *only* data surface. `dataplatform/query/pit.py` defines `Dataset(records,
knowable_date=…)`, and `admit(dataset)`:

* **refuses** a dataset that declares no `knowable_date` — "no declaration" is never read as
  "always knowable";
* **raises** `PitError` on the first record with `knowable_date > as_of` — it does not filter. A
  silent filter would turn *"you leaked future data"* into *"this query returned fewer rows"*, and
  a bug that returns a plausible answer is the one that never gets caught.

**What is missing is one adapter**: a `Dataset` over `macro_series` whose `knowable_date` is
`release_date`, registered on the replay's PIT surface. Then a policy asking for macro on session
S *cannot* see a release dated after S, and the guard fires if the query was constructed wrong.
That is a small task — it is the M11.3 that `M11.2.escalation.unblocks` already names and which
**does not exist in TASK_GRAPH.yaml.**

### Three traps that will bite, all of them already documented here

1. **Look-back windows must walk the trading calendar, not the replay window.** This bug has
   already cost a full audit cycle: replay-window look-backs left every momentum run in cash for
   its first year. A 250-day macro z-score has exactly the same shape — a regime that reads
   "insufficient history" for the first year of every run silently deletes a year of the test.
2. **A revised figure used as if it were the first print is undetectable by eye.** The
   `release_date < period_end` validator catches a parser reading the wrong column. It cannot
   catch a *revision* presented as an original — only refusing to store a Tier B fact whose
   release date is not evidenced does that, which is why the store makes `release_date` required.
3. **Two dates that must never collapse.** Joining on `period_end` instead of `release_date` is
   the purest look-ahead bias and every number still looks plausible. April CPI released 12 May
   was not available on 5 May.

### What can and cannot be tested

| Question | Backtestable? |
|---|---|
| Does a valuation/breadth regime filter improve a momentum policy's drawdown? | **Yes** — Tier A, 13 years, PIT-native. This is the real prize |
| Does FII outflow intensity predict short-horizon reversal? | **Yes**, once flows are in the lake |
| Does a 10Y-yield or VIX regime justify a position-size scalar? | **Yes**, Tier A |
| Does a CPI surprise move sectors? | **Not before capture-start.** Forward-only, honestly bounded |
| Does news tone predict returns? | **No** — no vintage, and §7 says LLM decisions are not replayable. Forward paper mode only |
| Does a geopolitical event index add anything? | **Not testable as specified.** Test the transmission channel (§4) instead |

**Emit a leak certificate for the first macro-using policy.** `tests/integration/test_pit_leak.py`
already instruments `PitContext.admit` and counts reads vs leaks; BACKLOG item 118 says to promote
`PitAuditor` to `backtest/pit_audit.py` when a second caller appears. A macro regime policy is that
second caller, and *"N macro reads, 0 leaked"* on the run's report is the only cheap way to keep
this honest as the policy grows.

Set expectations on the headline number: the existing momentum pair earns **12–15% over the
decade**, not the 23%/22% of the 2019–2025 window. Judge a regime filter against the decade.

---

## 8. Sequencing, and the decisions that gate it

### Owner decisions — nothing below Phase 1 can start without these

1. **Ratify EXECUTION_PLAN §4.1 row 17 "Macro / economic backdrop."** One line in §12. Until then
   two register rows sit under a row the constitution does not have.
2. **`go` on M11.2** (~2,470 requests to `nsearchives.nseindia.com`). **This conflicts with M10.4**
   — the fundamentals backfill, also NEEDS_GO, also NSE hosts. Two concurrent runners halve the
   effective per-host spacing through a side channel. **Pick an order; do not authorise both.**
   Recommendation: M11.2 first — it is ~2,470 small CSVs with no format eras against M10.4's
   thousands of per-filing fetches, and it unblocks the entire Tier A panel.
3. **The RSS set:** implement Moneycontrol + Livemint as D7 ratified, or re-ratify RBI-only as the
   set. Either is fine; the current gap between decision and code is not.
4. **Is macro allowed to be a mechanical input, or is it evidence only?** Today, news is
   evidence-only by D6 and macro has no ratified role at all. A regime filter that changes position
   size is a mechanical decision input and needs a §1 decision. **If the answer is
   evidence-only, skip §7 entirely and Phase 3 disappears** — worth deciding before the work,
   not after.

### Phases

**Phase 0 — free, no decision needed, do it now**

| Action | Cost |
|---|---|
| Re-probe ALFRED from the campaign server (quiet: 0 drivers) | 1 command. Settles whether Tier B has any history at all |
| Probe USD/INR (FBIL / RBI website), India VIX spot archive, `nse_fii_dii_flows` depth | ~30 requests under C.1's sweep method |
| Add `M11.3` to `TASK_GRAPH.yaml` — the macro `Dataset` adapter that M11.2 already claims to unblock | graph fix |
| Delete the Business Standard entry from `rss_feeds.yaml` | config, per D7 |

**Phase 1 — turn on what is already built and VERIFIED** *(needs decision 1, and 2 for the backfill)*

1. Register a `news_ingest` scheduler job — `gdelt.ingest_slice` + `rss.ingest_feed` over the
   active feeds, daily, failures alerted and skipped, never aborting the others.
2. Extend `DAILY_NSE_SOURCES` beyond `(NSE_BHAVCOPY,)` to include flows, F&O, deals, and the
   `ind_close_all` snapshot — the daily forward capture that makes the backfill the *last* gap
   rather than a permanently receding one.
3. Run M11.2. Report coverage per session **and unmapped index names separately.**
4. Wire M6.2's matcher into the T0 sweep — the seam is `NewsMatch.to_t0_flag`, and BACKLOG item
   112 has the exact recipe. Scope `NameResolver` to windows covering the item's `ts.date()`
   (item 113) so a recycled ticker cannot mis-link a dated headline.

**Phase 2 — Tier B forward capture** *(needs decision 1)*

A weekly `macro_release_capture` job per publisher, writing `MacroFact`s with the publisher's
stated release date. Same forward-accumulation logic as `constituents_snapshot`. The
backtestable window starts the day it first runs, and the store makes that boundary physical.

**Phase 3 — make it backtestable** *(needs decision 4)*

M11.3 adapter → L2 regime with `rule_version` → one regime-filtered momentum run against the
existing v2 report, judged over the decade → leak certificate on the run.

---

## 9. What this plan deliberately does not claim

* **That any Tier B series can be backtested today.** It cannot. No free Indian macro vintage
  source has been found, and the only candidate that would fix it is unreachable from this host
  pending one server re-probe.
* **That USD/INR or crude have sources.** They have candidates and an open licence question.
  Nothing is registered.
* **That GDELT will carry an India equity signal.** Its measured India-finance density was
  approximately zero and it has no ticker or ISIN field.
* **That a news-driven strategy is on the table.** It is not, under D6 and §7, without a §1
  amendment that nobody has proposed.
* **That the register status means data exists.** Five VERIFIED daily sources have never been
  fetched. `VERIFIED` means the endpoint answered during a sweep — it says nothing about the lake.
