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
  - 26 were last seen on 2015-11-06, the CNX → Nifty rename event. These are the names M11.1 left
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

---

# M11.3 — Widening the index-name alias table from the unknown-names list

**Date:** 2026-10-06 (IST) · **Inputs:** the backfill's L0 (`data/L0/nse_index_close_snapshot/`,
3,456 files) and the unknown-names list in §3 above · **Requests made: 0.**

## Result

| | Unknown names | Of which no longer published |
|---|---|---|
| Before (base table, M11.1) | **192** | 47 |
| After (this table) | **0** | 0 |

Both counts were measured **read-only from L0** over 2012-10-01 .. 2026-10-05: 3,453 sessions,
with the runner's own refusal rule for a misdated file. "Before" was measured with the base branch's
code and table, "after" with `--unknown-names` (below). Neither run wrote to the lake or the
database. With the new table the re-derivation yields the same **994,224** facts, so no two names in
any one file collapse onto one series. Only the `series_id` of renamed indices changes.

Every one of the 192 names is now in `dataplatform/ingest/macro/index_aliases.yaml` (version 2):

- **43 aliases** (renames), each with its own dated, cited switch. The 23 M11.1 rows are re-cited the
  same way, for 66 rows in total.
- **117 recorded series**: 113 new indices and 4 retired ones.

## How a rename was decided

The rule is evidence, never string similarity. For each name that stopped appearing, look at the
session after its last appearance. A rename is admitted only when one name first published there:

- continues the closing level (and, for equity indices, P/E, P/B and dividend yield);
- is the **only** new name that does;
- is not already the successor of another index.

Several near misses show why the multiples matter:

- `CNX Smallcap` (5222.5) is 0.14% from `Nifty Growth Sectors 15`, but its P/B is 1.00 against 8.02.
  It continues as `Nifty Smallcap 100` (P/B 1.02), and Growth Sectors continues `NI15`.
- `LIX 15` is nearest to `Nifty PSU Bank` on level, but the multiples point to `Nifty100 Liquid 15`.
- `CNX High Beta` is nearest to a G-Sec index on level, but the multiples point to `Nifty High Beta 50`.

Series that publish no multiples need a mechanical rule instead:

- **Leveraged and inverse Nifty:** the 2015-11-09 level replicates −1× or 2× Nifty 50's −0.49% move.
  For example, `NIFTY PR 1X Inverse` implies 491.10 and 491.4 was published.
- **Dividend points:** the level is identical on both sides of the switch.
- **G-Sec indices:** the successor is the only new name within 2–4%, and the three G-Sec indices
  moved together that day (−0.20 to −0.27%).

Every alias row now *requires*:

- `renamed_on`;
- `before` and `after`, each a `{name, file}`;
- `evidence`, holding the measured levels.

`before` and `after` are the two archive files straddling the switch. The predecessor appears in
the `before` file and not the `after`, and the successor the reverse. The loader refuses a row
without them. It also refuses a switch whose two names resolve to different series, a canonical that
is itself remapped, and a name recorded both as an alias and as its own series.

The cited files (17 new, plus the existing 2015-11-06 capture) are frozen byte-identical from L0
under `tests/fixtures/nifty_index_close/renames/`. A parametrized test pins each of the 66 aliases:

- each name appears only on its own side of the switch;
- both names give one `series_id` on either side of `renamed_on`;
- the level continues (under 3% for a one-session switch).

## The renames, by switch

| Switch (last old → first new) | Renames |
|---|---|
| 2013-02-07 → 02-08 ("S&P" dropped) | `S&P CNX 500`, `S&P CNX 500 Shariah`, `S&P CNX Nifty Dividend`, `S&P CNX Nifty Shariah` |
| 2014-08-12 → 09-08 (case-only respelling, 16 unpublished sessions) | `Nifty TR 1X Inverse`, `Nifty TR 2X Leverage`, evidenced by compounding CNX Nifty's daily moves across the gap (inverse: 418.16 implied vs 419.96 published; 2X: 4219.01 vs 4205.58) |
| 2015-11-06 → 11-09 (CNX → Nifty) | `CNX Consumption`, `CNX Dividend Opportunities`, `CNX Finance`, `CNX Midcap`, `CNX Service Sector`, `CNX Smallcap`, `NIFTY Midcap 50`, `CNX Alpha Index`, `CNX High Beta`, `CNX Low Volatility`, `CNX Nifty Dividend`, `CNX Nifty Shariah`, `CNX DEFTY`, `LIX 15`, `CPSE`, `NV 20`, `LIX15 Midcap`, `GSEC10 NSE Index`, `GSECBM NSE Index`, `NSE GSECBM Clean Price Index`, `NI15`, `NIFTY PR 1X Inverse`, `NIFTY PR 2x Leverage`, `NIFTY TR 1X Inverse`, `NIFTY TR 2X Leverage`, `NSE Quality 30` |
| 2016-03-31 → 04-01 (free-float/full split) | `Nifty Midcap 100` → `Nifty Free Float Midcap 100`, `Nifty Smallcap 100` → `Nifty Free Float Smallcap 100` |
| 2018-03-28 → 04-02 (split undone) | `Nifty Free Float Midcap 100` → `NIFTY Midcap 100`, `Nifty Free Float Smallcap 100` → `NIFTY Smallcap 100` |
| 2018-07-13 → 07-16 | `Nifty Quality 30` → `NIFTY100 Quality 30` |
| 2020-06-19 → 06-22 (one-session relabel) | `Nifty100 ESG Sector Leaders - Old` → `Nifty100 ESG Sector Leaders` |
| 2024-04-26 → 04-29 | `Nifty Aditya Birla Group`, `Nifty Mahindra Group`, `Nifty Tata Group`, `Nifty Tata Group 25% Cap` → `Nifty India Corporate Group Index - …` |
| 2025-05-02 → 05-05 | `Nifty India Internet & E-Commerce` → `Nifty India Internet` |

**Retired, with their own series and a `last_seen`:**

- `S&P ESG India`: last seen 2013-10-03, and no name was first published the next session.
- `Nifty Full Midcap 100` and `Nifty Full Smallcap 100`: the full-cap variants launched beside the
  free-float ones on 2016-04-01. Both were last published 2018-03-28. No name first published on
  2018-04-02 continues their level: `NIFTY Midcap 100` and `NIFTY Smallcap 100` start at 19097.4
  and 7929.2, which continue the *Free Float* variants (18757 and 7791.95), not the Full ones
  (5987.56 and 4000.01). So they are recorded as discontinued, not renamed.
- `Nifty BHARAT Bond Index - April 2025`: a target-maturity index that matured.

## Findings worth keeping

1. **Two archive files are regenerated, not as-of.** `ind_close_all_07072016.csv` (122 rows) and
   `ind_close_all_12042023.csv` carry that day's values under *later* names. They include indices
   launched years afterwards, and on those two dates the then-current names are missing. This is
   why `NIFTY Midcap 100` is "first seen" 2016-07-07. It also independently corroborates two renames:
   the 2016-07-07 file has `NIFTY Midcap 100` at 14095.35, between Free Float's 14122.85 (07-05) and
   14077.45 (07-08).
2. **At a financial-year start, multiples rebase for every index.** Nifty 50's P/B went from 3.10 to
   3.26 on 2016-04-01 on a −0.3% day. On the FY switches, the evidence is level continuity against
   peers plus uniqueness, not multiples, and each of those rows says so.
3. **⚠ The midcap/smallcap 100 P/E and P/B series break at the FY2016 and FY2018 switches.** The
   *closing level* is one index across them; the *multiples* are not continuous, and valuation
   consumers must not read them as one series across these dates:

   | Switch | Series | P/E | P/B | Yield |
   |---|---|---|---|---|
   | 2016-03-31 → 04-01 | `NIFTY_SMALLCAP_100` | 43.18 → 204.75 | 0.96 → 0.83 | 1.56 → 1.48 |
   | 2016-03-31 → 04-01 | `NIFTY_MIDCAP_100` | 23.79 → 27.51 | 2.16 → 2.14 | 1.52 → 1.62 |
   | 2018-03-28 → 04-02 | `NIFTY_SMALLCAP_100` | 343.52 → 70.66 | 1.63 → 1.98 | 0.69 → 0.55 |
   | 2018-03-28 → 04-02 | `NIFTY_MIDCAP_100` | 46.87 → 51.67 | 2.66 → 3.12 | 1.09 → 0.65 |

   These moves are far beyond Nifty 50's own FY rebase. They are consistent with a change in how the
   variant's earnings and book are aggregated (loss-makers, a full-cap vs free-float base), not with
   one day's prices. A P/E-based regime or percentile over these series needs a break at both dates,
   or should start from 2018-04-02. The table maps identity, and identity is right; it does not, and
   cannot, make a methodology change continuous.
4. **Some M11.1 evidence figures were wrong.** For example, the M11.1 rows put `CNX Pharma` at
   "4.98%" and `CNX Realty` at "4.72%" across the 2015 switch. The measured values are −1.94% and
   −2.14%. The mappings stand, and the figures are replaced with measured ones.
5. **Case variants already shared a series.** The lookup key and `series_id` are case-insensitive,
   so before this task `Nifty TR 1X Inverse` (2014) and `NIFTY TR 1X Inverse` were already merged
   without evidence. They are now evidenced (row 2 of the table). `Nifty Midcap 100` and
   `NIFTY Midcap 100` (2015-16 and 2018-) are likewise one series, and the free-float rows evidence
   it.
6. **One open caveat, outside this table.** `Nifty100 ESG Sector Leaders` was published neither
   2020-06-23 nor 06-30 and resumes 2020-07-01 at 1814.39. The "- Old" label suggests a methodology
   change around then. The row maps only the one-session relabel, which its evidence supports. The
   level from 07-01 under the unchanged name was already one series before this task.

## Commands (offline: no fetch, no lease, no `sync_state` change)

```bash
# read-only: the unknown-names count against L0 (prints the count and the list)
DATA_ROOT=/home/ubuntu/stock-manager/data uv run python -m dataplatform.ingest.macro.backfill \
  --from 2012-10-01 --to 2026-10-05 --unknown-names
```

**Post-merge re-derive (not run by this task).** This rewrites only `nse_index_close_snapshot`'s
rows in each `macro_series` partition, through `write_release(..., replace_source=True)`. FBIL,
World Bank, WPI, GST and RBI rows in the same partitions are kept. Run it from the main checkout
after the merge, with no other `macro_series` writer running over 2012-10-01 .. 2026-10-05:

```bash
DATA_ROOT=/home/ubuntu/stock-manager/data nohup uv run python -m dataplatform.ingest.macro.backfill \
  --from 2012-10-01 --to 2026-10-05 --rederive > ~/campaign/macro-rederive-$(date +%F).log 2>&1 &
```

Expected result: `3453 sessions rewritten, 994224 facts, 12 not in L0, 3 refused (0 requests)`.
The 3 refusals are the misdated April 2023 files, and the 12 are the archive's 404 sessions. Then
re-run the `--unknown-names` command above: it should print 0.

Operating notes:

- **Idempotent; re-run it if interrupted.** Each partition is rewritten whole, from immutable L0,
  through a temporary file renamed over the target. A kill mid-run leaves every partition either
  old or new, never half-written, and a second run converges to the same bytes.
- **`--to` must cover every captured session.** Only sessions inside `--from .. --to` are rewritten.
  A session captured after `--to` keeps its old `series_id`s, and a renamed index then reads as two
  series again from that date. Set `--to` to the last session in L0 (`ls data/L0/nse_index_close_snapshot/*/* | tail -1`),
  not to a fixed date copied from this note, if later sessions have been captured since.
- **`--stop-before` does not apply.** The offline modes hold no lease and make no request, so there
  is no evening window to leave. Passing `--stop-before` with `--rederive` or `--unknown-names` is
  refused (exit 2) rather than silently ignored.

## Verification

```bash
uv run pytest tests/unit/test_macro_series.py tests/unit/test_macro_backfill.py -q
```
