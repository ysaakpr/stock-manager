# M11.2 — Index valuation backfill (close-all snapshot → `macro_series`)

**Date:** 2026-10-06 (IST) · **Authority:** owner go 2026-10-06 (chat) for the B1 campaign ·
**Status: COMPLETE.** The window 2012-10-01 → 2026-10-05 is fully worked, with 0 sessions PENDING.

| Outcome | Sessions |
|---|---|
| PUBLISHED | 3,453 |
| NOT_PUBLISHED (archive 404) | 12 |
| REFUSED (misdated files) | 3 |
| FAILED | 0 |

The last run finished 2026-10-06 at 20:42 IST (§5). All three acceptance criteria are met (§1).

## 1. What was built

- `dataplatform/ingest/macro/backfill.py` — one unit per C.2 expected-data date under the new
  register row `nse_index_close_snapshot` (the archive-host copy of `ind_close_all_<DDMMYYYY>.csv`;
  byte-identical to `nifty_index_close_snapshot`, which is registered on `niftyindices.com` and
  whose crawl policy cannot cover an `nsearchives` URL). `sync_state` checkpoint per session,
  commit per session, L0 reuse before any request, `--dry-run`, `--max-sessions`,
  `--stop-before HH:MM` (IST), the `nsearchives.nseindia.com` host lease, and a 403 spike that
  parks with `ParkReason.FORBIDDEN_SPIKE` and exit 3 (exit 4 if another driver holds the lease).
- Coverage is surveyed **from L0 and `sync_state`**, not from the run, so a resumed run reports the
  whole window. Per-session CSV + Markdown summary go to `ops/reports/` (gitignored) or `--report`.
- `tests/unit/test_macro_backfill.py` — every acceptance criterion offline against the real
  captured files of both naming eras.

| Acceptance | Evidence |
|---|---|
| resumable and checkpointed; a re-run redoes no published session | `test_rerun_redoes_no_published_session` (second run's transport answers nothing); live: the 14:03 restart resumed 610 published sessions with zero requests for them |
| coverage per session, unmapped names separate | `test_coverage_is_per_session_and_unmapped_names_are_listed_separately`; live report §3 |
| a 403 spike parks with an enumerated cause and a non-zero exit | `test_403_spike_parks_with_enumerated_cause`, `test_main_exits_non_zero_when_parked`; no 403 was seen live |

## 2. What the archive actually contains — three findings the probe did not

1. **There is a format era.** M11.1 recorded "no format eras"; that is wrong. Files from
   **2014-06-26 into 2015-06** write `Index Date` as `DD/MM/YYYY`; every other session uses
   `DD-MM-YYYY`. The first build refused all 59 such sessions. Fixed in the parser (day-month order
   is the same — 26/06/2014 pins it — and the runner also checks each file's date against the
   session requested); the 59 were re-derived **from L0 with zero requests**.
2. **The archive publishes ambiguous rows.** `ind_close_all_08022013.csv` lists `CNX Alpha Index`
   twice; the second row carries what was `CNX High Beta`'s level the day before. The store refused
   the conflict (correctly) and the first run stopped. Neither row is guessed: every fact of a
   subject published twice with different values is **withheld** and named on the report.
3. **12 sessions the calendar calls trading days answer 404** (listed in §3). Closed in
   `sync_state` (non-retryable), never re-asked.

Also: the **10Y G-Sec is in the file as index levels, not a yield.** `Nifty 10 yr Benchmark G-Sec`
(total-return level) and `… (Clean Price)` land as `IN.NSE.<name>.CLOSE` series from 2015-11-09,
and the pre-switch `GSEC10 NSE Index` / `GSECBM NSE Index` / `NSE GSECBM Clean Price Index` from
2012-10-01. The P/E/P/B/Div Yield columns are `-` on every G-Sec row, so **no yield series exists
in this source**; deriving one from a clean-price index would need the benchmark bond's coupon and
maturity, which this file does not carry. Not fabricated.

## 3. Final coverage (whole window 2012-10-01 .. 2026-10-05, 3,468 expected-data dates)

| Outcome | Sessions |
|---|---|
| PUBLISHED | **3,453** (2012-10-01 → 2026-10-05) |
| NOT_PUBLISHED (404 at the archive, closed non-retryable) | 12 |
| REFUSED (file dated to another session) | 3 |
| FAILED (retryable) / PENDING | 0 / 0 |

Facts in published sessions: **994,224**. One subject withheld (2013-02-08 `CNX_ALPHA_INDEX`,
published twice with different values).

404 sessions: 2013-10-09, 2014-03-19, 2014-12-15, 2015-02-02, 2015-03-12, 2015-03-13, 2015-05-19,
2015-07-08, 2015-09-04, 2015-10-16, 2015-12-01, 2016-06-20.

**The 3 refused sessions: the archive misdated them.** `ind_close_all_06042023.csv`, `…10042023`
and `…11042023` print `Index Date` as `04-06-2023`, `04-10-2023` and `04-11-2023`, which is
month-first. Their neighbours `05042023` and `12042023` print `05-04-2023` and `12-04-2023`
(day-first). Read in the file's own format, each date names a different session from the one
requested, so the runner refuses it: a misdated file would land its facts in the wrong partition.

- The bytes are in L0.
- In `sync_state` the three rows are FAILED with `retryable = true`. Each re-run re-parses them from
  L0 with no request and refuses them again.
- Admitting them would take a parser rule: accept a date written month-first only when the filename
  date, read day-first, matches it exactly. That is a code change and out of scope for this run;
  it is logged here as a follow-up.

**Requests over the whole campaign:** 3,468 to `nsearchives.nseindia.com` (1,176 at the park, then
1,999 and 293). Every request used the register's ~2.5 s spacing. There was no 403 and no 5xx from
the archive.

### Unknown index names (final)

192 published names are not in the alias table:

- **Stopped appearing: 47.**
  - 25 were last seen on 2015-11-06, the CNX → Nifty rename event. These are the names M11.1 left
    unmapped on purpose.
  - The 9 names "last seen 2017-07-03" at the park were artefacts of the park and are gone (0 now).
  - The rest are genuine earlier or later disappearances worth researching. For example, `S&P CNX
    500` was last seen 2013-02-07, and groups of names stop on 2018-03-28 and 2024-04-26.
- **Still published, never renamed on the evidence: 145.**

The full table, with first seen, last seen and sessions per name, is in
`~/campaign/macro-backfill-2026-10-06-final.md`. That report is gitignored and lives on the server.
Widening `index_aliases.yaml` from it is a research task with evidence per row, M11.3's input. It is
not part of this task.

## 4. State changes worth knowing

- The first build wrote parse refusals as `FAILED retryable=false`. The 59 slash-era rows were
  re-opened with one scoped `UPDATE sync_state SET retryable = true` (this source, FAILED, error
  text "is not DD-MM-YYYY"); a refusal is now written retryable by the runner itself.
- Nothing was written to `prices_raw` or L2. `macro_series` now has a partition for every
  PUBLISHED session from 2012-10-01 to 2026-10-05. The 2018-07-10 → 2026-09-29 partitions already
  held FBIL (`INR`) rows from PR #58, and `write_release` merged into them after the resume picked
  up main.

## 5. Runs: park, resume, completion (all IST)

| Run | Window | Result | Log / report |
|---|---|---|---|
| first run | 2026-10-06 → 14:24 (SIGINT park) | 1,164 published | `~/campaign/macro-backfill-2026-10-06.{log,md,csv}`, park survey `…-parked.{md,csv}` |
| resume (after PR #58 merged) | 16:26 → 17:50 (`--stop-before 17:50`) | 1,996 published, 3 refused, 1,999 requests, 642,272 facts | `~/campaign/macro-backfill-2026-10-06-resume.{log,md,csv}` |
| **final** | **20:30:09 → 20:42:58** | **293 published, 3 refused, 293 requests (3 L0 reuses), 155,022 facts, 0 × 403, 0 failed, not stopped early** | `~/campaign/macro-backfill-2026-10-06-final.{log,md,csv}` |

**Why the final run started at 20:30 IST.** The 18:00–20:30 IST quiet window comes from the
scheduler's evening jobs (`dataplatform/scheduler/registry.py`), and two NSE host leases are
involved:

- **`www.nseindia.com`:** `shareholding_poll` at 18:05.
- **The archive host, `nsearchives.nseindia.com`:**
  - `eod_pipeline` at 18:30 (45-minute budget);
  - `daily_snapshot` at 19:15 (30 minutes);
  - `nse_daily_capture` at 20:00 (30 minutes).

  The last two capture latest-only endpoints. A campaign holding the archive lease then would cost
  that night's history, which no later run can recover.

The final run began only after the 20:00 `nse_daily_capture` had finished (SUCCEEDED, 20:00:08), and
with no lease held and no other driver running. It ran with `--stop-before 22:50` so it could not
hold the archive lease into the 23:00 same-night retry of `nse_daily_capture`. It finished at 20:42.

Command (from the main checkout at `22b616a`):

```bash
DATA_ROOT=/home/ubuntu/stock-manager/data nohup uv run python -m dataplatform.ingest.macro.backfill \
  --from 2012-10-01 --to 2026-10-05 --stop-before 22:50 \
  --report ~/campaign/macro-backfill-2026-10-06-final.md > ~/campaign/macro-backfill-2026-10-06-final.log 2>&1 &
```

`--to` stops at 2026-10-05, the last session whose file was certainly published. A file for today
that is not yet published answers 404, and a 404 closes the session non-retryably.

**Going forward.** No scheduler job captures `ind_close_all` daily, so sessions after 2026-10-05
accrue as owed. Re-run the same command with a later `--to`, at least a day after the session.
Published sessions cost no request.

`write_release` is last-write-wins per partition: never run it beside another `macro_series`
writer over the same dates.

## Verification

```bash
uv run pytest tests/unit/test_macro_backfill.py tests/unit/test_macro_series.py -q
```

Logs: `~/campaign/macro-backfill-2026-10-06.log`, `~/campaign/macro-backfill-sample-2026-10-06.log`;
coverage at the park: `~/campaign/macro-backfill-2026-10-06-parked.{md,csv}`.
