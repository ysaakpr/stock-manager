# M1.13 — 10-year NSE backfill completion report

**Task:** M1.13 — Execute 10-year NSE backfill
**Go:** pre-authorized by HUMAN_DECISIONS.md **D1** ("M1.13 pre-authorized … runs unattended as
soon as M1.9–M1.12 are DONE and the dry-run plan verifies. A 403 hard stop parks rather than
retries.") Deps M1.9–M1.12 are DONE; the dry-run plan verified (2471 requests, both eras).
**Range:** 2016-09-02 .. 2026-09-01 (inclusive), source set `nse_bhavcopy` (legacy + UDiFF eras).
**Runner:** `dataplatform.ingest.backfill` (M1.9), resume enabled, real network + Postgres.

---

## Outcome

| Acceptance criterion | Result |
|---|---|
| 1. 10 years of NSE equities present in L1 | **PASS** |
| 2. gap report explains 100% of missing days | **FAIL** — 2 unexplained days (corrupt source files) |
| 3. delisted symbols present in history (spot-check 3) | **PASS** |

**Two of three criteria pass. Criterion 2 cannot be met by the agent without weakening a data
validator that guards a §6 invariant — escalated to the owner (see "Finding" below).**

## Sessions fetched

- **Planned:** 2471 trading sessions (dry-run count).
- **Published to L1:** **2469** (2467 fetched live this run + resume of the B1 sample; one date,
  2024-07-05, self-healed on the first resume after a transient L0 restatement flicker).
- **Failed (permanent):** **2** — corrupt source files, see below.
- **403 tripwire:** never fired (`hard_stopped=False`, 0 forbidden spikes).
- **Time taken:** ~2h 04m for the main run (17:52:28 → 19:56:07 IST), rate-limited at
  `http_min_interval_seconds = 3.0` against `nsearchives.nseindia.com`.

## L1 row counts by year (`prices_raw`, all series)

| Year | Sessions | Rows |
|---|---:|---:|
| 2016 | 81 | 136,972 |
| 2017 | 248 | 437,241 |
| 2018 | 246 | 463,036 |
| 2019 | 245 | 472,664 |
| 2020 | 250 | 494,757 |
| 2021 | 247 | 513,092 |
| 2022 | 248 | 557,031 |
| 2023 | 246 | 605,228 |
| 2024 | 246 | 685,596 |
| 2025 | 248 | 760,926 |
| 2026 | 164 | 555,522 |
| **TOTAL** | **2469** | **5,682,065** |

2016 and 2026 are partial by construction (range starts 2016-09-02, ends 2026-09-01). All ten
calendar years are represented — **criterion 1 holds.**

## Gap report by reason (`nse_bhavcopy`, 2016-09-02..2026-09-01)

3652 (source, date) pairs examined — 2469 complete, 1181 explained, **2 UNEXPLAINED**.

| Reason | Count | Explained? |
|---|---:|---|
| WEEKEND | 1040 | yes (exchange shut) |
| HOLIDAY | 141 | yes (C.2 holiday calendar) |
| FAILED | 2 | **no** |

`fully_explained = False`. The two unexplained pairs, enumerated with their sync_state history:

1. **2020-07-13** — state=FAILED, attempts=5, retryable=True
   `parse failed: cm13JUL2020bhav.csv.zip:2: TIMESTAMP is '13-Jul-20', which is not a
   DD-MON-YYYY exchange date` (the source file carries a malformed 2-digit-year timestamp).
2. **2021-02-16** — state=FAILED, attempts=5, retryable=True
   `parse failed: cm16FEB2021bhav.csv.zip:27: row is not a valid price row … isin 'DUMMY' does
   not match ^[A-Z]{2}[A-Z0-9]{9}[0-9]$` (a bogus ISIN in one row of the source file).

Both are **deterministic**: three additional resume passes re-fetched and re-parsed each date and
failed identically. The parsers (M1.4/M1.5) and identity validator are behaving as CLAUDE.md
requires — *fail loud and specific* — rather than silently admitting a bad date or a non-ISIN row.

> A third date, **2024-07-05**, first failed with `L0ImmutabilityError` (the source served bytes
> whose sha256 differed from the B1 sample stored 2026-08-08; L0 is write-once, invariant #1, so
> the store refused to overwrite). It self-healed on the next resume when the source served the
> original bytes again. Recorded here as a transient restatement flicker, now PUBLISHED.

## Delisted symbol spot-check (criterion 3)

Delisted names are preserved in history and stop exactly at their delisting, keyed by ISIN:

| Symbol | ISIN | Present | Last session | Real-world event |
|---|---|---:|---|---|
| DHFL | INE202B01012 | 1178 sessions | 2021-06-11 | IBC resolution / delisting, 2021 |
| JETAIRWAYS | INE802G01018 | 754 sessions | 2019-09-23 | grounded & suspended, 2019 |
| RELCAPITAL | INE013A01015 | 1657 sessions | 2024-02-26 | IBC delisting, 2024 |

Each appears from the start of the window and disappears at its delisting date — **criterion 3
holds.**

---

## Finding for the owner (blocks criterion 2)

Two of 2471 NSE bhavcopy source files are corrupt at the source and the pipeline correctly
rejects them, leaving 2 of 2469 sessions unpublished and the gap report at 99.95% explained
(2 FAILED days). Resolving this to "100% explained" requires a decision reserved to you, because
every agent-side fix would weaken a validator that guards a §6 invariant:

- **A. Relax the validators** — teach the legacy date parser to accept 2-digit years and/or make
  the row parser quarantine (not reject) rows with malformed ISINs. This loosens date and
  identity (ISIN-only join) validation platform-wide to admit two bad files — a validation
  weakening this project's rules forbid an agent from doing to pass a gate.
- **B. Accept 2 permanently-missing sessions** out of 2469 and amend criterion 2 to "100% of
  missing days *classified*" (FAILED-with-enumerated-cause counts as explained). The gap report
  already enumerates both with full cause and history.
- **C. Manually re-source** clean copies of `cm13JUL2020bhav.csv.zip` and `cm16FEB2021bhav.csv.zip`
  and place them for re-ingest.

**Recommendation: B.** The backfill is otherwise complete and correct; the two files are corrupt
upstream, not a defect in the platform, and the gap report gives each a specific, enumerated
cause. Weakening the date/ISIN validators (A) to swallow two bad files would trade a core data
invariant for a green box, which is the opposite of what criterion 2 exists to protect.

## Operational notes

- The gap scanner reported `L1 unverified for 2469 published pair(s)` — its `LakeL1Presence`
  lake-root probe did not resolve the on-disk partitions in this run, so published pairs are
  counted "complete" but not physically re-verified against the parquet lake. It does not affect
  the unexplained count (the 2 FAILED days). Logged to `ops/BACKLOG.md` for M1.14's audit.
