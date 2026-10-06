# M11.2 — Index valuation backfill (close-all snapshot → `macro_series`)

**Date:** 2026-10-06 (IST) · **Authority:** owner go 2026-10-06 (chat) for the B1 campaign ·
**Status: PARKED at 2017-07-03** (last published session), by request, before the run reached
partitions another branch has written. **Resumes once PR #58 is on main and this branch includes
main** (see §5).

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

## 3. Coverage at the park (whole window 2012-10-01 .. 2026-10-05, 3,468 expected-data dates)

| Outcome | Sessions |
|---|---|
| PUBLISHED | 1,164 (2012-10-01 → 2017-07-03, plus the 2026-10-01 / 10-05 sample) |
| NOT_PUBLISHED (404 at the archive) | 12 |
| REFUSED / FAILED | 0 |
| PENDING (after the park) | 2,292 |

Facts in published sessions: **196,930**. One subject withheld (2013-02-08 `CNX_ALPHA_INDEX`).

404 sessions: 2013-10-09, 2014-03-19, 2014-12-15, 2015-02-02, 2015-03-12, 2015-03-13, 2015-05-19,
2015-07-08, 2015-09-04, 2015-10-16, 2015-12-01, 2016-06-20.

**Requests:** 1,176 to `nsearchives.nseindia.com` (1,164 stored + 12 × 404), at the register's
~2.5 s spacing, no 403, no 5xx. The remaining 2,292 sessions are ~2,292 requests.

### Unknown index names (provisional until the window completes)

189 published names are not in the alias table. Split by whether they are still published:

- **Stopped appearing — 44.** 26 vanish at the 2015-11-06/10 rename event: exactly the 26 M11.1
  left unmapped on purpose. 9 more last-seen 2017-07-03 are **artefacts of the park** and will move
  once the run completes. The rest are genuine earlier disappearances worth researching, e.g.
  `S&P CNX 500` last seen 2013-02-07 (89 sessions) — a likely `S&P CNX` → `CNX` rename two years
  before the big event, which the table does not yet carry.
- **Still published, never renamed on the evidence so far — 145** (current names such as
  `India VIX`, the G-Sec indices, `Nifty Alpha 50`). Not errors; listed for completeness.

The final list is produced by the completing run's report (`--report`), and widening
`index_aliases.yaml` from it is a research task with evidence per row, not part of this one.

## 4. State changes worth knowing

- The first build wrote parse refusals as `FAILED retryable=false`. The 59 slash-era rows were
  re-opened with one scoped `UPDATE sync_state SET retryable = true` (this source, FAILED, error
  text "is not DD-MM-YYYY"); a refusal is now written retryable by the runner itself.
- Nothing written to `prices_raw` or L2. `macro_series` partitions written: 2012-10-01 → 2017-07-03
  and 2026-10-01, 2026-10-05 only.

## 5. Why parked, and the resume

Another branch (PR #58, open, owner to merge) landed FBIL rows (unit `INR`) in `macro_series`
partitions 2018-07-10 .. 2026-09-29 and World Bank rows (`USD`) in 2026-07-13. This branch's `Unit`
enum lacks those members, so `write_release`'s read-back of such a partition would raise. The run
was stopped (SIGINT, graceful) at 14:24 IST, last published session 2017-07-03, before reaching
them. Those partitions were not touched.

Resume, after PR #58 is merged and this branch includes main, outside 18:00–20:30 IST:

```bash
DATA_ROOT=/home/ubuntu/stock-manager/data nohup uv run python -m dataplatform.ingest.macro.backfill \
  --from 2012-10-01 --to <latest session> --stop-before 17:50 \
  --report ~/campaign/macro-backfill-<date>.md > ~/campaign/macro-backfill-<date>.log 2>&1 &
```

`write_release` is last-write-wins per partition: never run it beside another `macro_series`
writer over the same dates.

## Verification

```bash
uv run pytest tests/unit/test_macro_backfill.py tests/unit/test_macro_series.py -q
```

Logs: `~/campaign/macro-backfill-2026-10-06.log`, `~/campaign/macro-backfill-sample-2026-10-06.log`;
coverage at the park: `~/campaign/macro-backfill-2026-10-06-parked.{md,csv}`.
