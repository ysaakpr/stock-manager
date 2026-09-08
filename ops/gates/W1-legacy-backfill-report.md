# W1 — NSE legacy bhavcopy deep backfill (2006-01-02 .. 2016-09-01)

**Task:** W1 — the price spine, per `ops/studies/packets/w1-nse-legacy-bhavcopy.md`.
**Go:** owner sign-off on the ~2,670-request budget (packet §0 gate 1); `w0/era-coverage` on `main`
(gate 2) extended `nse_holidays.yaml` coverage back to 2006-01-01.
**Runner:** `dataplatform.ingest.legacy_backfill`, real network + real Postgres
(`trading-platform-postgres-1`), authoritative lake at `data/L0`.
**Campaign:** 2026-09-08 05:20:07 → 07:10:06 UTC, detached, log `~/campaign/w1-2026-09-08.log`.

---

## 1. Acquisition tally

| | count |
|---|---:|
| candidates planned (calendar basis) | 2,636 |
| fetched | 2,627 |
| already in L0 (Phase 1 smoke) | 9 |
| **404 (`HOLIDAY_OR_NO_SESSION`)** | **0** |
| failed | **0** |
| requests spent | 2,627 |
| hard stop (403 spike / 5 errors / 20 404s) | never tripped |

Plus **10** non-calendar weekend sessions (§3): 10 fetched, 10 × HTTP 200, 0 failed.
**Total spend 2,637 requests** against the ~2,670 approved. Spacing was the driver's
`http_min_interval_seconds = 3.0` per host throughout, under a single host lease.

The era cutover was observed live and is exact: **2011-06-21 → E1, 2011-06-22 → E2**.
E1 closed at 1,354 sessions.

## 2. Calendar reconciliation — the headline

`reconcile_calendar` over the whole span, `covered=True`:

- **`undeclared_closures` (calendar says session, archive served nothing): 0 — no dates.**
- **`unexpected_sessions` (calendar says closed, archive served a file): 0 — no dates.**

All 2,636 calendar-expected dates served a payload. The two sources agree on every date, in both
directions. Nothing was adjusted, filtered or tuned to reach that result; the diff was empty as
computed.

Before this campaign the same reconcile had evidence for 11 of 2,636 candidates and agreed by
construction. It is now a real cross-validation of two independently derived sources: the shipped
calendar came from NSE Indices' NIFTY 50 daily history plus exchange circulars, and this driver
proves closures by 404 against the bhavcopy archive.

**It is not uniformly independent, and must not be reported as though it were.** 2012 and 2013
carry `source: derived_bhavcopy_verified` in `nse_holidays.yaml` — no NSE publication was located
for either year, and every weekday closure in them was established by 404 against *this same
archive* (provenance block: "2012 … 14/14 confirmed by bhavcopy", "2013 … 13/13"). For those two
years the check is **partly circular and is not independent confirmation**. The other nine years
(2006-2011, 2014-2016) rest on NSE's own circulars or holiday pages, and for those the agreement is
a genuine two-source cross-validation.

The only two 404s in the journal in range — **2006-08-15** and **2011-08-15**, both Independence Day
— are Phase 1 smoke probes of a known holiday, not campaign results. Both are declared holidays, so
neither is an undeclared closure.

## 3. The ten non-Muhurat weekend sessions

NSE holds occasional weekend sessions that are not Muhurat — Union Budget Saturdays and
disaster-recovery-site sessions — and publishes a bhavcopy for them. They are not holidays, so they
cannot carry `special_session`; `SpecialSession` has no member for them; `calendar.py` classifies
each as `WEEKEND` and expects no data. They were therefore **excluded from the 2,636 candidates**,
and `reconcile` structurally cannot see the miss, because nothing ever fetches them.

All ten were fetched (10 × 200) and all ten **promoted successfully** — promotion does not consult
the calendar (the only calendar guard in `sync_state.py` is on `mark_gap`, which promotion never
calls), so nothing refused them:

| date | day | era | outcome | rows |
|---|---|---|---|---:|
| 2006-04-29 | Sat | E1 | quarantine (no ISIN) | 923 |
| 2006-06-25 | Sun | E1 | quarantine (no ISIN) | 876 |
| 2010-02-06 | Sat | E1 | quarantine (no ISIN) | 1,299 |
| 2012-01-07 | Sat | E2 | `prices_raw` | 1,362 |
| 2012-03-03 | Sat | E2 | `prices_raw` | 1,394 |
| 2012-04-28 | Sat | E2 | `prices_raw` | 1,338 |
| 2012-09-08 | Sat | E2 | `prices_raw` | 1,388 |
| 2013-05-11 | Sat | E2 | `prices_raw` | 1,267 |
| 2014-03-22 | Sat | E2 | `prices_raw` | 1,375 |
| 2015-02-28 | Sat | E2 | `prices_raw` | 1,534 |
| **total** | | | 3 quarantined / 7 promoted | **9,658** priced + **3,098** quarantined |

These are real trading sessions whose absence would look exactly like missing data rather than a
schema limitation. They are listed separately from the 2,636 throughout this report because they
are evidence the calendar is incomplete **in a way `reconcile` cannot detect** — a more interesting
finding than a disagreement inside the covered set would have been. `calendar.classify()` still
returns `WEEKEND` for all ten; that is unchanged and deliberate.

**Neither `nse_holidays.yaml` nor `calendar.py` was edited.** The schema gap stays a filed,
deliberate limitation (the `known_limitation` block in `nse_holidays.yaml`). Capturing the payload
needed no schema fix: L0 is raw bytes under a key and needs no calendar. A proposed fix — a new
`SpecialSession` member plus a way to declare a session on a non-holiday weekend — is left to a
separate, explicit change.

### Is ten the complete set for 2006-2015?

**Complete with respect to NSE Indices' NIFTY 50 daily history; not proven complete absolutely.**

W0-6 did not find these by chance. Its session record was one POST to the NIFTY 50 daily history for
01-Jan-2006..31-Dec-2015, returning **2,480 sessions**; a weekend date present in that feed is a
weekend session. That total decomposes exactly, with no residual:

| component | count | source |
|---|---:|---|
| weekday expected sessions | 2,460 | `calendar.expected_sessions(2006-01-01..2015-12-31)` |
| weekday Muhurat (holidays that trade) | 7 | `nse_holidays.yaml` |
| weekend Muhurat | 3 | 2006-10-21, 2009-10-17, 2013-11-03 |
| **non-Muhurat weekend** | **10** | the ten above |
| **total** | **2,480** | matches the feed exactly |

There is no eleventh unaccounted date in the feed. **The bound on that claim:** completeness is
only as good as the index feed, and that feed is known to drop real trading days — C.2 found six
such gaps in 2016-2025 (2016-01-01, 2016-08-12, 2018-01-01, 2019-01-01, 2019-02-13, 2019-03-29). A
weekend session for which NIFTY 50 published no index value would be invisible to this enumeration.
Proving completeness absolutely means probing all ~1,040 weekend dates in the span — a separate
campaign and a separate budget, not done here.

## 4. Per-year coverage

Counts read off the L1 parquet footers and the L0 tree, not carried from a run's memory, so the
table describes what the lake *is* and can be regenerated.

| year | era | calendar-expected | weekend extra | in L0 | prices_raw sessions | prices_raw rows | distinct ISINs | quarantined sessions | quarantined rows |
|---:|:---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2006 | E1 | 248 | 2 | 250 | 0 | 0 | 0 | 250 | 233,256 |
| 2007 | E1 | 249 | 0 | 249 | 0 | 0 | 0 | 249 | 277,898 |
| 2008 | E1 | 246 | 0 | 246 | 0 | 0 | 0 | 246 | 303,673 |
| 2009 | E1 | 243 | 0 | 243 | 0 | 0 | 0 | 243 | 310,044 |
| 2010 | E1 | 251 | 1 | 252 | 0 | 0 | 0 | 252 | 354,006 |
| 2011 | E1/E2 | 247 | 0 | 247 | 130 | 198,162 | 1,670 | 117 | 173,694 |
| 2012 | E2 | 247 | 4 | 251 | 251 | 391,194 | 1,758 | 0 | 0 |
| 2013 | E2 | 249 | 1 | 250 | 249 | 366,282 | 1,789 | 0 | 0 |
| 2014 | E2 | 243 | 1 | 244 | 244 | 386,388 | 1,893 | 0 | 0 |
| 2015 | E2 | 247 | 1 | 248 | 248 | 391,769 | 1,996 | 0 | 0 |
| 2016 | E2 | 166 | 0 | 166 | 166 | 275,093 | 3,606 | 0 | 0 |
| **total** | E1/E2 | **2,636** | **10** | **2,646** | **1,288** | **2,008,888** | **4,071** | **1,357** | **1,652,571** |

`1,288 + 1,357 = 2,645`; the one session in L0 with neither partition is **2013-11-06** (§6).

2016 is partial by construction (range ends 2016-09-01) and its distinct-ISIN figure is the one
number in this table not comparable to its neighbours: **2016-09-01 is the seam with the
pre-existing lake**, and `write_prices_raw` preserved the 2,902 rows already there rather than
clobbering them (1,690 new + 2,902 preserved = 4,592 rows, 3,331 distinct ISINs on that date alone).
Excluding the seam, 2016-01-01..08-31 holds 270,501 rows and **1,985** distinct ISINs, in line with
2015's 1,996. Preserving at the seam is correct behaviour, not a defect.

## 5. E1 unresolved rows — the measured bound

Era E1 (2006-01-02 .. 2011-06-21) has 11 columns and **no ISIN**. Invariant #2 makes ISIN the only
join key, so these rows cannot enter `prices_raw`; they are enumerated into
`prices_raw_quarantine`, which drops nothing. **No symbol→ISIN mapping was invented** — the only
resolver available is a current-day listing, and every company delisted before today is absent from
it, so mapping through it is survivorship-biased in exactly the direction that matters. Resolving
E1 is Horizon C (W4 identity work), a separate funded decision.

Projection going in was ~1.5-2.0M rows. **Measured:**

| year | unresolved rows |
|---:|---:|
| 2006 | 233,256 |
| 2007 | 277,898 |
| 2008 | 303,673 |
| 2009 | 310,044 |
| 2010 | 354,006 |
| 2011 (to 06-21) | 173,694 |
| **TOTAL** | **1,652,571** |

**1,652,571 rows across 1,357 sessions carry no ISIN and are not joinable.** That is the honest
published bound on how far back this platform can claim to reach: the *price spine* is continuous
from 2006-01-02, but the **joinable** history begins **2011-06-22**. Anything claiming a 2006 start
for ISIN-keyed analysis is claiming these 1.65M rows are usable, and they are not.

**Confirmed: no `prices_raw` partition exists for any E1 date.** Checked every E1 date in range —
the set of E1 dates with a `prices_raw` partition is empty. The invariant holds on disk, not merely
in intent.

## 6. The one failure — a source defect, not a pipeline defect

**2013-11-06** failed to promote and has no `prices_raw` partition. Row 527 of
`cm06NOV2013bhav.csv` is:

    ICICI,M1,3197,3197,3197,3197,3197,3197,5,15985,06-NOV-2013,1,INE,

The ISIN field contains the literal string `INE` — a truncated ISIN in NSE's own published file.
The validator refused it (`String should match pattern '^[A-Z]{2}[A-Z0-9]{9}[0-9]$'`), the whole
file was rejected, and the failure is filed loudly in `sync_state` as `FAILED` (retryable), so it
reaches the status API rather than a log line nobody reads.

**This was not worked around and the validator was not weakened.** That is the same escalation the
M1 backfill report raised (2 dates lost to corrupt source files) and it is the owner's call, not an
agent's — weakening an ISIN pattern guards a §6 invariant.

**Cost:** one session, and with it the 1,442 *good* rows in that file. **Proposed fix, for the
owner, not applied here:** route a row that fails ISIN validation into the same
`unidentified_rows` / quarantine path the parser already uses for unidentifiable rows, instead of
failing the entire date. That would recover 1,442 rows and quarantine 1, and it changes a parser
policy rather than the pattern itself. It is out of W1's scope and is not in this PR.

## 7. L0 integrity

`L0Store.verify_checksums()` — payload re-hashed against its sidecar, missing payloads, orphan
payloads and unreadable sidecars:

| scope | checked | defects |
|---|---:|---:|
| `nse_bhavcopy_legacy`, 2006-01-02..2016-09-01 | 2,646 | **0** |
| **whole lake** | **98,744** | **0** |

Baseline before this campaign was 96,107 checked / 0 defects. 96,107 + 2,627 (campaign) + 10
(weekend sessions) = **98,744**. Payload/sidecar pairing is 1:1 with zero orphans across the lake.

L1 was additionally checked for damage after the concurrency incident in §8: all 1,288 `prices_raw`
partitions are unique on `(isin, series)` and all 1,357 quarantine partitions on `(symbol, series)`
— **0 violations**. (Raw `isin` alone repeats within a session, legitimately: one ISIN trades in
several series, e.g. EQ and BE.)

## 8. Incident — two agents supervised the same campaign

Reported because it affected execution, not because it changed the result.

A sibling agent under the same orchestrator (`.polly/registry.json` task `w1-phase2-close`,
"ended turn while monitoring; campaign continues detached. Timer set to reconvene") was supervising
this campaign concurrently with this session. Neither agent knew about the other. Consequences:

1. **The ten-date weekend fetch** was executed by that agent at 07:10:42, 36 s after the main
   campaign released the host lease. It took the lease properly rather than fighting it, held the
   3.0 s spacing, and fetched exactly the ten correct dates. No budget or politeness breach.
2. **`promote` ran twice concurrently**, both processes writing the same log path, the same
   Postgres rows and the same L1 partitions. The log holds two `promote_done` lines
   (`published=390 skipped=2245` and `published=2543 skipped=92`), which is why the two disagree.

The end state is correct and complete — verified independently in §7 by the `(isin, series)` and
`(symbol, series)` uniqueness sweep over all 2,645 partitions, which is what a double-write would
have broken. Promotion is idempotent per date (`SKIPPED_PUBLISHED` in `sync_state`, and
`write_prices_raw` replaces a partition), and that is what absorbed the race. **It should not be
relied on to absorb the next one**: two drivers writing one Postgres and one parquet tree is a
data-loss shape, and the host lease that correctly serialised the *fetching* does not cover
*promotion*. Either promotion needs the same lease discipline, or the orchestrator must not hold
two agents on one task.

## 9. What this does not do

Nothing about the thematic problem — sector classification, index membership and PIT fundamentals
are untouched, and those are the gaps no amount of price fetching closes. W1 makes E1 *retained*,
not *usable*. It does not fix the five defects (D1, D2, D3, D9, D11) that are wrong today at ten
years, several of which deeper history makes worse rather than better.

**Do W1 for span. It is not progress on the fund manager's hard problems.**
