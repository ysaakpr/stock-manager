# Ingestion audit — the layers, the lake, and what is wrong with both

**Date:** 2026-09-06 · **Scope:** `dataplatform/ingest/**`, `dataplatform/store/l0|l1|l2`,
`dataplatform/identity`, `dataplatform/quality`, `dataplatform/status`, and the contents of
`data/L0`, `data/L1`, `data/L2` and the local Postgres as they stand on the laptop.

Everything below is measured against the tree at `1d33c65`, not inferred from documentation. Where
a claim is a count, the query that produced it is stated. Findings already recorded in
`ops/BACKLOG.md`, `ops/gates/audit-2026-09-06.md` or `ops/gates/M10-data-gap-plan.md` are listed
separately from findings that are not, because the request was for the latter.

---

## 0. The lake, measured

| Layer | Size | Shape |
|---|---|---|
| L0 | 3.1 GB | 9 source trees, 61,511 payloads, 61,511 `.meta.json` sidecars — **pairing is exact in all 9** |
| L1 `prices_raw` | 451 MB | 2,469 date partitions · 5,682,065 rows · 7,536 ISINs · 2016-09-02 → 2026-09-01 · **NSE only** |
| L1 `prices_raw_quarantine` | 46 MB | 2,467 partitions · 1,798,418 rows |
| L1 `pit_fundamentals` | — | 1,607 partitions · 918,541 facts · 69,344 filings · 1,645 ISINs · 22 concepts |
| L1 `index_constituents` | — | **one** partition, `date=2026-09-03` |
| L2 `prices_adjusted` | 21 MB | **252 ISINs** of 7,536 |
| Postgres | — | `sync_state` 76,803 rows / **71,853 distinct sources** · `security_master` 2,397 · `corporate_actions` 9,573 · `quality_flag` 2,487 open |

Of the 29 rows in `source_register.yaml`, **9 have ever landed a byte in L0**. The other 20 —
every BSE source, F&O, deals, announcements, shareholding, FII/DII, TRI, GDELT, RSS, macro — are
VERIFIED or FAILED in the register and empty in the lake.

---

## 1. Strengths

These are not politeness. Each is a design decision that is visibly paying off in the data.

**1.1 One door to the network, and it hands back a reference, not bytes.**
`Fetcher.fetch` returns an `L0Ref`; a parser that wants content reads it back through
`L0Store.get`, re-checksummed. A caller cannot parse a response it was never given, so invariant #1
("every L1 value is re-derivable from L0") is structural rather than aspirational. The payoff is
concrete and was collected: backfilling delivery for ten years cost **zero** price re-fetches,
because all 2,473 bhavcopy payloads were already in L0 and the join happens at L1 write time.

**1.2 L0 integrity is real.** 61,511 payloads, 61,511 sidecars, no orphan on either side across
all nine sources. Payloads are `0o444`, written `O_EXCL` + fsync. Re-fetching a key with different
bytes raises `L0ImmutabilityError` rather than overwriting.

**1.3 The politeness policy is a stated invariant with teeth, not a config knob.**
One user agent from `Settings`; 403 is not in the retryable set; `ForbiddenWatch` stops the process
and raises CRITICAL rather than rotating agents. `tests/unit/test_fetcher.py` asserts there is no
second user agent in the file to rotate *to*. The one 403 exception (NSE's own homepage during the
cookie handshake) is narrow, documented at the seam, and does not extend to the data request.

**1.4 Absence is modelled as absence.** The delivery parser maps `-` to `None`, never `0`, with the
reasoning written down: a zero would read downstream as "0 % delivered", the strongest possible
distribution signal, and would make a spike detector fire on every trade-to-trade name every day.
The lake shows this held — BE/BZ/GS/SM carry `deliv_pct` NULL, not zero, at 100 %.

**1.5 The trading calendar knows the hard case.** `DayKind.MUHURAT` exists: a declared holiday that
still publishes a bhavcopy. `expected_sessions` excludes those dates; `expected_gap_kind` refuses to
call them a gap; `tests/unit/test_calendar.py` freezes archive probes so a later "simplification"
cannot collapse Muhurat into an ordinary holiday. The lake bears it out — all ten Diwali sessions
2016→2025 are present as partitions and none is reported as a gap. This is the single most commonly
botched detail in Indian EOD data and it is handled correctly.

**1.6 Format eras are first-class.** Two cash-market eras (`bhavcopy_legacy`, `bhavcopy_udiff`),
two delivery eras (`nse_mto` pre-2019, `nse_sec_bhavdata_full` after), each with its own parser,
its own register era and frozen fixtures. The delivery module's era boundary is documented as a
*sourcing* boundary rather than a data boundary, with the measurement behind it — the archive
answers `sec_bhavdata_full_30092019.csv` with HTTP 200 and a body of 27-Jun-2019 rows, so the parser
checks the body's own date against the one requested rather than trusting the URL. That check is
what caught the 2021-11-04 Muhurat file being served as the previous session's.

**1.7 Nothing is dropped silently at the L1 join.** The delivery join is reconciled:
`delivery_rows == joined + unresolved + orphaned`, with unresolved and orphaned rows written to a
quarantine dataset carrying a reason. An *ambiguous* symbol is not quarantined — `resolve` raises,
because "two contradictory ISINs" is a different fact from "none". That distinction is correct and
rare.

**1.8 ISIN-only joins are enforced where it counts.** No module resolves a symbol except through
`IdentityMaster.resolve(symbol, on_date)`, and the XBRL path was corrected on 2026-09-05 to take
ISIN from the index entry and cross-check the document's *symbol as of the filing date* — after the
"any symbol ever held" version filed 65 Dhunseri Tea filings under Dhunseri Ventures.

**1.9 Idempotence is tested as bytes, not as intent.** `rebuild_prices_raw_from_l0` sorts by a total
key, quantises money to a fixed scale, and writes via staging rename, so re-deriving a partition
produces a byte-identical file. The re-derivation is exercised at scale — 199 partitions
byte-identical after the write path was rewritten.

**1.10 The test suite is proportionate to the risk.** 48,692 lines of tests against 38,432 lines of
`dataplatform/`; 72 unit, 28 integration, 6 golden, 6 property modules; 22 fixture families. CI runs
replay determinism, the PIT-leak audit and the golden CA suite *before* the rest of the gate.

**1.11 The failures are legible.** Every stuck ingest carries a specific error string — `"index says
this filing is 'SEPC' … but the document states …"`, `"TIMESTAMP is '13-Jul-20', which is not a
DD-MON-Y"`, `"payload is not UTF-8 text at byte 22"`. The diagnosis work in §3 took minutes rather
than days entirely because of this.

**1.12 The self-critique culture is real.** `ops/BACKLOG.md` holds 132 entries, several of which
retract earlier claims made by the same system ("this row corrected an earlier claim that
balance-sheet items needed a new source"). Two of the findings below are only visible *because* the
previous audit wrote down what it assumed.

---

## 2. Weaknesses already identified — confirmed, with current numbers

Listed so this document is not read as if they were news, and because two of them have moved.

| # | Already recorded | Current measurement |
|---|---|---|
| K1 | Identity master is a single 2026-08-08 `EQUITY_L.csv` snapshot; historical resolution is thin (BACKLOG M9.1, gap-plan Action 3) | **2,397 of 7,536 traded ISINs (31.8 %) are known to D2.** 5,139 unknown |
| K2 | `security_master.status` always ACTIVE; delisted names invisible → survivorship bias (BACKLOG M1.7) | unchanged |
| K3 | PIT fundamentals stop at the Dec-2024 quarter; the integrated feed's period-guess refusals (BACKLOG, memory) | **1,463 of 2,392 stuck filings** are `no results column covers 2025-01-01→2025-03-31` |
| K4 | Balance sheet is partial: no cash-flow statement, ROE annual not rolling, segment assets/liabilities deferred (gap-plan) | 22 concepts; equity on 19 % of filings, D/E on 27 % |
| K5 | BSE rows never reach L2; `series='EQ'` filters exclude BSE groups (BACKLOG M3.1) | L2 = 252 ISINs; L1 = **0 BSE rows locally** (campaign is on the server) |
| K6 | `nse_holidays.yaml` ends 2026-12-31; nothing schedules the refresh (BACKLOG C.1) | unchanged — hard-fails Jan 2027 |
| K7 | Soft-404s: three of nine sources answer unknown paths with 200 + HTML (BACKLOG C.1/M1.2) | live example in the lake: `niftyprivatebank` parked, "body is markup, not CSV" |
| K8 | No wired daily runner for CA, deals, announcements, shareholding, FII/DII (BACKLOG M2.2/M3.5/M3.8/M3.4) | `SOURCE_SETS` has **3** members: `nse_bhavcopy`, `bse_bhavcopy`, `nse_delivery` |
| K9 | `symbolchange.csv` has no Source Register row (BACKLOG M1.7) | still true — see N4, which is the larger version of this |
| K10 | Backups cover L0 only; no object-storage target (BACKLOG setup/M5.1) | unchanged |

**One backlog row is now stale and should be struck:** the M1.6 row still reads *"The delivery file
was never fetched … `deliv_qty`/`deliv_pct` are NULL in 100 % of `prices_raw` rows."* Delivery was
backfilled (`9456c40`…`0ae0fb7`); coverage is 59.1 % overall and 78.6 % of EQ rows.

---

## 3. Weaknesses **not** previously identified

Ordered by blast radius. Each has a reproduction.

### N1 — The gap report is dead in production, and a second subsystem killed it

`/status/gaps` with no `source` parameter returns **HTTP 500**.

```
PathLayoutError: dataset 'nse_xbrl_filing/1197621' is not a valid lake identifier
```

Cause: `sync_state.source` was designed as a low-cardinality column — one value per registered
source. The XBRL/fundamentals backfill repurposed it as a per-filing checkpoint key, writing
`nse_xbrl_filing/IF87614` and `nse_xbrl_filing/IF87614-FY` per filing. The table now holds
**71,853 distinct "sources"**, 71,737 of them synthetic. `GapScanner` defaults to "every source
`sync_state` has ever held a row for" and treats each as a lake dataset identifier.

Consequences, all measured on the live database:

* `/status/gaps` (default view) → **500** in 4.9 s. `PathLayoutError` is not in the endpoint's
  caught `(CalendarError, GapReportError)` tuple, so it escapes as an unhandled error.
* `/status/sources` → **200, but 27 MB in 65 s.** It enumerates all 71,853.
* **The M1 gate criterion "the gap report explains 100 % of missing days" is currently
  unverifiable.** The gate passed against a database that did not yet contain the fundamentals
  campaign's checkpoints.
* The EOD job survives only by accident of scoping: `EodPipeline._gap_check` passes
  `sources=self._sources` (its 3 names), so the daily run never hits the explosion — and therefore
  never sees the other 71,850 either.

This is the failure mode the platform is best defended against — a subsystem quietly poisoning a
shared table — arriving through the one column nobody thought of as an interface.

### N2 — Two trading sessions are missing from the price lake, permanently and invisibly

```
calendar expected sessions 2016-09-02 → 2026-09-01 : 2,461
lake partitions                                    : 2,469   (2,461 + 10 Muhurat, correctly kept)
expected but absent                                : 2  →  2020-07-13, 2021-02-16
```

Neither `data/L1/prices_raw/date=2020-07-13` nor `date=2021-02-16` exists. Both are ordinary
trading days with neighbours present. `sync_state` says FAILED after 5 attempts:

| Date | Error |
|---|---|
| 2020-07-13 | `cm13JUL2020bhav.csv.zip:2: TIMESTAMP is '13-Jul-20', which is not a DD-MON-Y` |
| 2021-02-16 | `cm16FEB2021bhav.csv.zip:27: row is not a valid price row` |

**The L0 payloads are present and healthy** — 71,297 and 74,595 bytes, real bhavcopies, not error
pages. The platform holds the bytes and cannot parse them. The 2020-07-13 case is a *third*
timestamp format inside the legacy era (two-digit year), which the fixture-per-era discipline
should have caught and did not, because no fixture was ever frozen from a payload that failed.

Why nothing surfaces it:
* A parse failure is correctly non-retryable, so EOD self-heal will never re-attempt it — the fix
  is a parser change plus a re-derive from L0, which is a human decision.
* The gap report that exists to enumerate exactly this is the one in N1.
* The scheduler reports `NEVER_RAN`, so no daily job has ever run the check anyway.

Every backtest over a window spanning July 2020 or February 2021 has silently skipped a session.

### N3 — The quarantine is write-only. 1.8 M rows, no reader, no threshold, no alert

`prices_raw_quarantine` holds **1,798,418 rows across 2,467 of 2,469 partitions**:

| reason | rows |
|---|---|
| `symbol_unresolved` | 1,361,670 |
| `no_matching_price` | 436,748 |

`grep` over the whole tree finds exactly one writer (`store/l1.py`) and **two readers, both
assertions in `tests/integration/test_l1_writer.py`**. No status endpoint, no quality rule, no
sentinel, no gap-report integration, no runbook step, no threshold, no alert. The archive publisher
explicitly excludes it. The docstring's promise — *"a visible gap, not silent data loss"* — is half
true: the rows are on disk, and nothing in the platform will ever mention them. Discovering the
1.8 M required opening DuckDB by hand.

Related and separately true: 1,077 of 3,635 EQ symbols (625,391 price rows) have **never once**
received a delivery figure.

### N4 — The identity master's inputs are not in L0. They are test fixtures

`security_master`, `symbol_history` and `exchange_listing` — the authority behind every symbol→ISIN
resolution in the platform, invariant #2 — were built from:

```
tests/fixtures/nse_equity_list/2026-08-08/EQUITY_L.csv
tests/fixtures/nse_equity_list/2026-08-08/symbolchange.csv
```

There is no `data/L0/nse_equity_list/` tree. The register row `nse_equity_list` is VERIFIED and has
**never been fetched**. `ops/runbooks/identity-master.md` states *"D1 fetches them into L0; this
command reads files off disk"* — the first clause has never happened, and the runbook calls the
fixture copies "frozen copies … use them to reproduce a parse failure offline", which is not the
role they are actually playing.

Consequences:

* **Invariant #1 does not hold for D2.** The identity layer cannot be re-derived from L0, because
  its input is not in L0. Every ISIN in `prices_raw` and every CA resolution ultimately depends on
  a file outside the lake's provenance chain.
* Not covered by `L0Store` checksums, not covered by `ops/backup.sh` (L0-only), not versioned as
  data, and living in a directory whose contents a test-hygiene cleanup is entitled to prune.
* This is the larger version of backlog row M1.7 ("`symbolchange.csv` has no Source Register row").
  `EQUITY_L.csv` *has* a register row; the gap is that neither file has ever travelled the ingest
  path the invariant assumes.

### N5 — 593 fund/ETF ISINs are structurally outside D2, and Action 3 will not fix them

```
traded ISINs                     7,536
known to security_master         2,397
traded with INF prefix (funds)     593   → known to D2: 0
traded with INE prefix (cos)     5,393   → unknown to D2: 2,998
```

The 2,998 unknown companies are Action 3's territory (delisted and renamed names; reconstructable
from bhavcopy symbol history already in L0). The **593 INF-scheme securities are a different
problem**: `EQUITY_L.csv` does not list mutual-fund/ETF schemes at all, so no amount of *historical*
listing reconstruction produces them. They need a different source.

This is why NIFTYBEES, BANKBEES, GOLDBEES, LIQUIDBEES, CPSEETF and the rest have **zero** delivery
coverage across all 2,469 sessions while sitting in `prices_raw` perfectly well — the bhavcopy
carries their ISIN natively, the delivery file does not, and D2 cannot bridge it. It also means
backlog row M5.7 is not a tidiness nit: `DEFAULT_PARKING_ISIN = LIQUIDBEES` is a module constant
**because it cannot be resolved through D2**, so the cash sleeve's default instrument is the one
security the identity invariant structurally cannot serve.

### N6 — `L0Store.verify_checksums` has never run outside the test suite

Every reference is in `tests/unit/test_l0.py`. No CLI wiring, no scheduler job (`default_registry`
holds `eod_pipeline` and `constituents_snapshot` and nothing else), no runbook step, no cron.

The immutable lake that everything is re-derivable from has **no integrity sweep**, and bit-rot
would be discovered only when a re-derivation happened to read the damaged file — which, for the
3.1 GB now on disk, could be years. Backlog row M5.1 makes this worse by recording the opposite
belief: *"the evidence store has no integrity sweep … whereas `L0Store.verify_checksums` reports L0
damage proactively."* The capability exists; the proactivity does not.

### N7 — Rate limiting is per-process, and the operating model is now two machines

`RateLimiter` is an in-memory `dict[str, datetime]` behind a thread `Lock`. Its own docstring states
the assumption: *"it assumes it is the only gate in front of the transport — spacing enforced
anywhere else is spacing that a second caller can skip."*

That assumption held when one process fetched. It no longer does. `CLAUDE.md` now describes laptop
+ server, both able to run campaigns, and states the guard as a **manual procedure**: *"before
starting a fetch campaign on the server, make sure no driver runs on the laptop against the same
host, and the other way round."* Nothing enforces it. The machinery for a mutual exclusion already
exists in the codebase (`pg_try_advisory_lock` in `scheduler/runner.py`) but it is per-Postgres, and
the two machines have separate databases.

Two concurrent drivers halve the effective spacing on `nsearchives.nseindia.com` — which is the
politeness policy being circumvented through a side channel rather than changed, and the penalty is
a 403 spike on the single host that carries prices, delivery, corporate actions and fundamentals.
The gap plan noticed this for *one* pair of campaigns ("sequence delivery after fundamentals"); the
general case is unguarded.

### N8 — 2,392 filings are stuck FAILED, and 774 of them are a class nobody has named

| class | filings |
|---|---|
| `no results column covers <period>` — integrated feed period guess | 1,463 |
| **`MissingPayloadError: no L0 payload for <doc>`** | **774** |
| `index says this filing is X but the document states Y` — D2 symbol history | 156 |

The first and third classes are recorded (backlog `e0440e5`, and the "21 filings, 2 companies" row —
whose 21 is now 156 and should be updated). The **774 `MissingPayloadError`** class is not: the
index entry exists, the document was never fetched into L0, and the re-derivation therefore has
nothing to parse. These are permanently absent facts for real companies, concentrated in 2017–2019,
and they will not be fixed by any re-derivation because the bytes are not there. They need a
targeted re-fetch of a known list.

### N9 — `/status/quality` has 2,487 open flags and truncates at 200

`open_total: 2487`, `flags: 200`, `limit: 200`, and **one** distinct count category. The sentinel is
firing at a volume nobody is draining, and the endpoint's default page shows 8 % of it. An operator
surface that always says "2,487 open" says nothing; the useful signal (is today worse than
yesterday?) is not derivable from it.

---

## 4. Mitigation plan for §3

Sequenced so that each step makes the next one's result trustworthy. Nothing here needs a fetch
except P4 and P7, and only P7 is large enough to need an owner GO under B1 (>200 requests).

### Priority 0 — restore the ability to see (no fetching, ~1 day)

**P0.1 · Give `sync_state` back its low-cardinality `source` column.** *Fixes N1.*
Add a `unit` column (nullable text) and move the per-filing key into it; `source` becomes
`nse_xbrl_filing` again with `unit = 'IF87614'`. Primary key becomes `(source, logical_date, unit)`
with `unit = ''` for per-session sources, so the price and delivery rows are untouched. Migrate the
71,737 existing rows in the same migration by splitting on the first `/`.
Guard: a CHECK constraint, or a `SyncStateStore` assertion, that `source` is a valid lake identifier
— the same predicate `PathLayoutError` already applies, moved to write time so the next subsystem
cannot repeat this. Then `GapScanner`'s default becomes ~10 sources and `/status/gaps` answers.
*Acceptance:* `/status/gaps?from=&to=` returns 200 over a month; `/status/sources` returns under
100 KB; the pair `(nse_xbrl_filing, IF87614)` still resumes correctly on a re-run.

**P0.2 · Make the gap endpoint fail as a 400, not a 500.** *Defence in depth for N1.*
Add `PathLayoutError` to the caught tuple in `status_gaps`, and have `GapScanner` refuse a source
the register does not know with a message naming it. A poisoned table should produce a diagnosis,
not a stack trace.

**P0.3 · Run the gap report over all of history and act on the output.** *Depends on P0.1.*
This is the first honest answer the platform will have given to "what am I missing?". Expect
2020-07-13, 2021-02-16 and the 2,392 stuck filings to be the bulk of it. File the result as
`ops/gates/gap-report-<date>.md`.

### Priority 1 — close the two price holes (no fetching, ~half a day)

**P1.1 · Freeze fixtures from the two failing L0 payloads and fix the parsers.** *Fixes N2.*
`cm13JUL2020bhav.csv.zip` and `cm16FEB2021bhav.csv.zip` are already in L0 and are the only
authentic examples of these variants the project will ever get. Copy them into
`tests/fixtures/nse_bhavcopy_legacy/<era>/` with provenance, write the failing test first, then
widen the timestamp acceptance to include `DD-MON-YY` (locale-independent, as the existing month
table already is) and diagnose row 27 of the February file.
*Acceptance:* both parse; `rebuild_prices_raw_from_l0` produces both partitions; the lake has 2,471
partitions and `expected_sessions` reports zero absent.

**P1.2 · Make "an L0 payload that no L1 partition was derived from" a standing check.**
A one-query sweep — every `sync_state` row not PUBLISHED whose L0 payload exists — belongs next to
the gap report, because it is the class both N2 failures fell into and neither was noticed for
months. Wire it as a `GapReason` (`L0_PRESENT_L1_ABSENT`) rather than a separate script.

### Priority 2 — make the quarantine and the flags operable (no fetching, ~1 day)

**P2.1 · A quarantine reader with a per-session ratio and a threshold.** *Fixes N3.*
Add `/status/quarantine?from=&to=` returning, per session and per reason, the quarantined count and
the ratio to `delivery_rows`. Emit a `quality_flag` when a session's `symbol_unresolved` ratio moves
more than a set amount against its trailing median — the level is a known 30-ish per cent and is
Action 3's problem, but a *step change* is a new identity break and is worth waking someone for.
Add the current totals to the daily EOD report so the number is in front of someone once a day.
*Acceptance:* the endpoint reproduces the 1,798,418 / two-reason split; an induced identity break
in a scratch database raises a flag.

**P2.2 · Make `/status/quality` answer the operational question.** *Fixes N9.*
Return counts grouped by kind and by first-seen day with the enumerated list paged, so "2,487 open,
of which 3 are new today" is one call. Then triage the 2,487: they are one kind, so either the rule
is too sensitive or there is a real systemic break behind it — decide which, and either retune the
rule or open a task, but do not leave the endpoint permanently saturated.

### Priority 3 — put the identity inputs inside the provenance chain (small fetch, ~half a day)

**P3.1 · Fetch `EQUITY_L.csv` and `symbolchange.csv` into L0 through the `Fetcher`.** *Fixes N4,
and closes backlog M1.7's `symbolchange.csv` row.* Two requests. Add the `symbolchange.csv` register
row. Then re-run `dataplatform.identity.ingest` reading **back out of L0** rather than from a path,
so the ingest CLI's `--equity-list` takes an `L0Ref` (keeping the path form for offline
reproduction). Keep the existing fixtures as fixtures.
*Acceptance:* `security_master` re-derivable from L0 alone; `verify_checksums` covers both files;
`ops/backup.sh` picks them up with no change (they are under L0).

**P3.2 · Schedule the weekly refresh as a scheduler job.** The runbook says weekly; nothing does it
weekly. Register `identity_refresh` alongside `eod_pipeline`.

### Priority 4 — a source for the 593 fund/ETF ISINs (small fetch, ~1 day)

**P4.1 · Ingest an ETF/scheme master and extend D2 to carry INF-scheme securities.** *Fixes N5.*
Candidate sources, to be probed and recorded in `source_register.yaml` the way §5.1 of
`source-verification.md` does: NSE's ETF list, and the BSE `ListofScripData` call already in the
register with a non-`Equity` segment. Model them as securities with an instrument class, so a
screen can exclude them and the delivery join can resolve them.
*Acceptance:* NIFTYBEES/GOLDBEES/LIQUIDBEES resolve through `IdentityMaster.resolve`; their
delivery rows leave quarantine on re-derive; `DEFAULT_PARKING_ISIN` becomes a D2 lookup and backlog
M5.7 closes with it.

**Note the ordering dependency:** do P4.1 *before* Action 3 from the gap plan, not after. It is a
tenth of the work, it removes 593 of the 5,139 unknown ISINs, and it stops Action 3 from being
measured against a target it structurally cannot hit.

### Priority 5 — one host budget across machines (~half a day)

**P5.1 · A host lease, not a procedure.** *Fixes N7.*
Before its first request, a driver takes a named lease on the host (`nsearchives.nseindia.com`) with
a TTL and a heartbeat; a second driver that cannot take the lease exits 2 naming the holder, the
same way `ops/remote.sh` already exits 2 on a missing `.remote.env`. Simplest durable home that
both machines can see is a small file in the repo-adjacent state the sync already carries, or the
server's Postgres reached over the existing ssh path — decide in the task, but the property to hold
is *the laptop and the server cannot both be fetching from one host*.
Add a lease check to `ops/remote.sh run` and to `Fetcher.__init__`, so a driver started by hand is
covered too.
*Acceptance:* two concurrent drivers against one host — one wins, one exits 2 with the holder named;
a driver killed mid-run releases within the TTL.

### Priority 6 — an integrity sweep that actually sweeps (~half a day)

**P6.1 · Register `l0_verify` as a scheduler job.** *Fixes N6.*
Weekly, scoped by source and date range so a full 3.1 GB pass is not run every time; a rolling
window plus a monthly full pass. Damage raises CRITICAL through the existing `Alerter` and lands as
a `quality_flag`. Correct the M5.1 backlog row, which currently records the opposite.
*Acceptance:* an induced single-byte corruption in a scratch lake produces one flag and one alert
naming the file.

**P6.2 · While there: `.DS_Store` in `data/L0/`, `data/L0/nse_xbrl_filing/` and
`data/L0/nse_xbrl_filing/2026/`.** Three files that are neither payload nor sidecar sitting inside
the immutable lake. Harmless today because `verify_checksums` was never run; it will report them as
"a payload nobody claims" the moment P6.1 lands. Delete them and add the ignore to the sweep.

### Priority 7 — the 774 absent XBRL payloads (fetch, needs owner GO)

**P7.1 · Enumerate and re-fetch.** *Fixes N8.*
The `MissingPayloadError` rows name their document. Extract the 774, confirm each is genuinely
absent from L0 (rather than present under a different key), and run a scoped re-fetch through the
existing `fundamentals_backfill` path. 774 requests at 2.5 s ≈ 32 min — over B1's 200-request
threshold, so it needs an owner GO, and it must hold the P5.1 lease.
*Acceptance:* the 774 either publish or fail with a *new, specific* reason recorded per filing.

**P7.2 · Update the two stale backlog rows.** The M1.6 delivery row (delivery has been fetched) and
the M10.4 "21 filings, 2 companies" row (now 156 filings). A backlog whose rows are contradicted by
the lake is the specific hazard `AGENTIC_CONTEXT` §7 warns about.

---

## 4b. Status — what landed the same day

| | Finding | State | Evidence |
|---|---|---|---|
| P0.1 | N1 | **DONE** `9a0a3f9` | Migration 0008 gives `sync_state` a `unit` column; 71,853 distinct sources → **7**, no row lost of 76,803. `/status/sources` 27 MB in 65 s → **3.5 KB in 0.1 s**. `/status/gaps` default view 500 → **200 in 0.1 s**; all ten years in 1.1 s. A CHECK now refuses a non-identifier `source` at write time. |
| P0.2 | N1 | **DONE** `9a0a3f9` | A malformed `?source=` is a 400 naming it, not a 500. |
| P0.3 | — | **DONE** `158a66a` | [gap-report-2026-09-06.md](gap-report-2026-09-06.md): 97,445 pairs, 2,409 unexplained, every non-XBRL one listed. |
| P1.1 | N2 | **DONE** `63c3c98` | Both sessions parse and are in the lake — 2,471 partitions, `expected_sessions` reports nothing absent. Two payloads frozen as fixtures with their provenance. |
| P1.2 | N2 | **DONE** `5c50055` | `L0_PRESENT_L1_ABSENT` separates "fix the parser" from "fetch it again". Conservative for unit rows on purpose. |
| P2.1 | N3 | **DONE** `5c50055` | `/status/quarantine` reproduces the 1,799,849 rows and their reason split; a step change becomes a WARN flag; the EOD job runs the check daily. Zero steps over ten years — the level is stable, which is why the rule watches the *change*. |
| P2.2 | N9 | **DONE** `5c50055` | `/status/quality` groups by check with `raised_today` beside each count. The standing queue is one check: 2,487 `ca_reconciliation` WARNs, none raised today. |
| P3.1 | N4 | **CODE DONE**, fetch queued | `nse_symbol_changes` has a register row; `identity.ingest --from-l0` reads both files back out of the lake; `ingest.identity_refresh` fetches them under the host lease. **The two requests wait for the host.** |
| P3.2 | N4 | **DONE** | `identity_refresh` registered, 07:00 IST Saturday. The runbook's "D1 fetches them into L0" is true for the first time, and says so. |
| P4.1 | N5 | **BLOCKED on the host** | Needs a source probe, and building a parser against a guessed format is the false-DONE this project already paid for once (M7.3). Recorded in the backlog with the candidates and the ordering argument. |
| P5.1 | N7 | **DONE** `3dfdfca` | `host_lease` refuses a second driver and names the holder; `remote.sh run` now refuses in the other direction too. |
| P6.1 | N6 | **DONE** `87b7197` | `l0_verify` weekly. First real full sweep: **62,047 payloads re-hashed in 260 s, zero defects.** |
| P6.2 | N6 | **DONE** `87b7197` | Five `.DS_Store` files removed from inside `data/`. |
| P7.1 | N8 | **ENUMERATED**, fetch queued | 774 filings → **536 distinct documents**, 153 ISINs, 2018-06 → 2025-02, listed in `missing-xbrl-payloads-2026-09-06.json`. ~22 min of fetching. |
| P7.2 | — | **DONE** | The two stale backlog rows corrected: delivery *was* fetched, and "21 filings" is 156. |

### Why three items are queued rather than done

`ops/remote.sh status` during this work showed the Integrated Filing campaign still running on the
server against `nsearchives.nseindia.com` — the same host `EQUITY_L.csv`, `symbolchange.csv` and
the 536 XBRL documents come from. Fetching from the laptop while it runs is exactly the collision
N7 is about, so P3.1's two requests, P4.1's probe and P7.1's 536 are queued behind it.

That is worth recording as evidence rather than as an inconvenience: **the only thing that stopped
the collision was reading a log by hand.** P5.1 now makes it a refusal.

When the campaign finishes, in this order:

```bash
ops/remote.sh status                                    # drivers: 0
uv run python -m dataplatform.ingest.identity_refresh   # P3.1 — 2 requests, ~5 s
```

P7.1 needs one more thing built first, and it is worth being exact about it: the fundamentals
runner has `--rebuild-from-l0` (derive from payloads already held) but **no mode that fetches a
named list of documents**. Its plan is built from the index feed, not from a file. So the 536
requests need a small addition — read the enumeration, skip anything L0 already holds, fetch the
rest under the lease — and that is a task, not a command that exists today. The list is its input
and is committed; the runner is not written.

## 5. What this does not cover

The BSE campaign completed on the server (536/536 sessions) and its L0 has not been brought home;
L1 here holds no BSE rows, so nothing above is a statement about BSE data quality. `data/L2` (252 ISINs) is gated on the ISIN-lineage work already
recorded, not on anything found here. The 20 registered-but-empty sources are a scope question for
the owner, not a defect — but the register's `status: VERIFIED` currently means "the URL answered
once in August", not "we have data", and a reader could reasonably take it for the latter.
