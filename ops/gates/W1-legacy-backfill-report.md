# W1 — NSE legacy bhavcopy deep backfill (2006-01-02 .. 2016-09-01)

**Task:** W1 — the price spine, per `ops/studies/packets/w1-nse-legacy-bhavcopy.md`.
**Go:** owner sign-off on the ~2,670-request budget (packet §0 gate 1); `w0/era-coverage` on `main`
(gate 2) extended `nse_holidays.yaml` coverage back to 2006-01-01.
**Runner:** `dataplatform.ingest.legacy_backfill`, real network + real Postgres
(`trading-platform-postgres-1`), authoritative lake at `data/L0`.
**Campaign:** 2026-09-08 05:20:07 → 07:10:06 UTC, detached, log `~/campaign/w1-2026-09-08.log`.

---

## 0. Corrections applied after this report was first committed

This report was written by a sibling agent (see §8) at 07:22:57 and three later commits on this
branch moved four of its numbers and one of its headline claims. Corrected in place below; recorded
here so the change is legible rather than silent.

| § | was | now | why |
|---|---|---|---|
| 2 | `unexpected_sessions: 0` | **10, with dates** | `coverage_report` was reconciling against its own *plan*. A calendar-derived plan can only hold dates the calendar expects, so the field was structurally always empty. It now reads L0, and the ten weekend sessions of §3 surface as the real disagreement they are (`90476ab`). |
| 4 | 1,288 `prices_raw` sessions | **1,289** | 2013-11-06 recovered (§6). |
| 4 | 2,008,888 `prices_raw` rows | **2,007,427 NSE** (+2,902 pre-existing BSE preserved at the 2016-09-01 seam = 2,010,329 in the files) | the old figure counted every row in the partitions, including BSE rows this wave did not write, and predated the recovery. NSE-only is the honest figure for what W1 added. |
| 4, 5 | 1,652,571 unresolved rows | **1,652,572** | the one quarantined `ICICI`/`INE` row (§6). |
| 6 | "proposed fix, for the owner, not applied here" | **applied** (`30e34e0`) | see the scope note in §6. |

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
- **`unexpected_sessions` (calendar says closed, archive served a file): 10 — every one a
  Saturday or Sunday, listed in §3:** 2006-04-29, 2006-06-25, 2010-02-06, 2012-01-07, 2012-03-03,
  2012-04-28, 2012-09-08, 2013-05-11, 2014-03-22, 2015-02-28.

All 2,636 calendar-expected dates served a payload, so direction 1 is empty by measurement: over
2,627 requests the archive never once said the exchange was shut on a day the calendar called a
session. **That is the clean half of the result and it is a real agreement.**

Direction 2 is not a disagreement about the holiday *list* — none of the ten is a weekday and none
is a declared holiday. It is the schema limitation §3 describes, now visible as evidence instead of
as a footnote. This field read `0` when the report was first written, and that was an artefact of
the reconcile being fed the plan rather than the lake; §0 records the fix. Nothing was adjusted,
filtered or tuned in either direction — the correction made the diff *larger*, not smaller.

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
| 2013 | E2 | 249 | 1 | 250 | 250 | 367,723 | 1,789 | 1 | 1 |
| 2014 | E2 | 243 | 1 | 244 | 244 | 386,388 | 1,893 | 0 | 0 |
| 2015 | E2 | 247 | 1 | 248 | 248 | 391,769 | 1,996 | 0 | 0 |
| 2016 | E2 | 166 | 0 | 166 | 166 | 275,093 | 3,606 | 0 | 0 |
| **total** | E1/E2 | **2,636** | **10** | **2,646** | **1,289** | **2,007,427** | **4,071** | **1,358** | **1,652,572** |

`1,289 + 1,358 = 2,647`, one more than the 2,646 sessions in L0, because **2013-11-06 has both** —
1,441 rows in `prices_raw` and the single unjoinable `ICICI` row in quarantine (§6). Every other
session has exactly one partition: E1 dates quarantine only, E2 dates price only.

The `prices_raw` figure is **NSE rows**. The files hold 2,010,329, the extra 2,902 being BSE rows
already present at the 2016-09-01 seam which `write_prices_raw` preserved rather than clobbered.
Counting those as W1's would overstate what this wave added.

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
| 2013 (one row, §6) | 1 |
| **TOTAL** | **1,652,572** |

**1,652,572 rows across 1,358 sessions carry no ISIN and are not joinable.** That is the honest
published bound on how far back this platform can claim to reach: the *price spine* is continuous
from 2006-01-02, but the **joinable** history begins **2011-06-22**. Anything claiming a 2006 start
for ISIN-keyed analysis is claiming these 1.65M rows are usable, and they are not.

**Confirmed: no `prices_raw` partition exists for any E1 date.** Checked every E1 date in range —
the set of E1 dates with a `prices_raw` partition is empty. The invariant holds on disk, not merely
in intent.

## 6. The one failure — a source defect, now recovered

**2013-11-06** failed to promote in the campaign run. Row 527 of `cm06NOV2013bhav.csv` is:

    ICICI,M1,3197,3197,3197,3197,3197,3197,5,15985,06-NOV-2013,1,INE,

Fourteen fields, every price and count valid, and an ISIN three characters long — the literal
`INE`, in NSE's own published file. `PLACEHOLDER_ISINS` held `DUMMY`, `NA` and `-`, so `INE` was
not a recognised absence of identity; the row raised, the whole file was rejected, and
`prices_raw` had no partition for the date. **Cost: 1,441 good prices.**

**This is the defect the 2026-09-06 audit already fixed once, for `DUMMY`, recurring with a
different literal** — because a closed list can only ever be as complete as the literals someone
has already seen. Finding it took a 2,637-request campaign; the next one would take another.

### It has been fixed (`30e34e0`), and two agents disagreed about whether it should be

This report first said the fix was "the owner's call, not an agent's" and left it unapplied,
reasoning that "weakening an ISIN pattern guards a §6 invariant". That reasoning is right about the
pattern and the pattern was **not** touched: `models.ISIN_PATTERN` is unchanged, `PriceRow.isin`
still requires a syntactically valid ISIN, and no row reaches `prices_raw` without one. What
changed is the *routing* of a row that fails it — from "raise and lose the session" to "quarantine
the row and keep the session" — which is verbatim the fix this section proposed as the safe one
("it changes a parser policy rather than the pattern itself").

The test is now the ISIN *shape* rather than membership of a list (`_isin_is_unusable`). This
**strengthens** invariant #2: there is now no value of the ISIN column that can yield a `PriceRow`
without being a real ISIN, where previously an unlisted malformed literal was merely fatal.
Everything structural stays session-fatal — unrecognised header, short or wide row, non-numeric
price, two sessions in one file — each with its own test.

Two tests asserted the old behaviour deliberately, arguing a shape test "would turn a truncated
field into a silently missing row". Three things answer that, and the replacement test states them:
the row is not silent (quarantine drops nothing and `bhavcopy.row_without_isin` warns by symbol); a
truncated download truncates the *tail*, which the row-width check catches under its own two
untouched tests; and the distinction survives, because `stated_isin` keeps each literal verbatim so
`DUMMY` stays distinguishable from `INE`.

**Result:** re-promoted to **1,441 `prices_raw` rows + 1 quarantined**, and the whole
2006-01-02..2016-09-01 span now has **0 parse failures and 0 `FAILED` sync rows**.
`cm06NOV2013bhav.csv.zip` is frozen as a fixture so the rule cannot narrow again.

**If the owner judges this outside W1's scope, `30e34e0` is a single self-contained revert** — it
would restore the refusal and re-lose the session. The disagreement is recorded rather than
resolved unilaterally, but the fix is in the branch and the gate is green with it.

## 7. L0 integrity

`L0Store.verify_checksums()` — payload re-hashed against its sidecar, missing payloads, orphan
payloads and unreadable sidecars:

| scope | checked | defects |
|---|---:|---:|
| `nse_bhavcopy_legacy`, 2006-01-02..2016-09-01 | 2,646 | **0** |
| **whole lake** | **98,744** | **0** |

Baseline before this campaign was 96,107 checked / 0 defects. 96,107 + 2,627 (campaign) + 10
(weekend sessions) = **98,744**. Payload/sidecar pairing is 1:1 with zero orphans across the lake.

L1 was additionally checked for damage after the concurrency incident in §8, twice and two ways.
First, all `prices_raw` partitions unique on `(isin, series)` and all quarantine partitions on
`(symbol, series)` — **0 violations**. (Raw `isin` alone repeats within a session, legitimately: one
ISIN trades in several series, e.g. EQ and BE.) Then, independently and after every write had
landed, **every one of the 2,646 sessions was re-parsed from its L0 payload and its row count
compared against the parquet**: 0 read failures (a torn file fails there), 0 row-count mismatches,
0 E1 dates with a `prices_raw` partition, 0 payloads that would not parse, and 0 leftover
`.partial` staging files. That is the check a double-write would have broken, run against L0 rather
than against either run's log.

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

The end state is correct and complete — verified independently in §7 — the uniqueness sweep over
every partition, and then a full re-parse of all 2,646 L0 payloads compared row for row against
the parquet, which is what a double-write would have broken. Promotion is idempotent per date (`SKIPPED_PUBLISHED` in `sync_state`, and
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
