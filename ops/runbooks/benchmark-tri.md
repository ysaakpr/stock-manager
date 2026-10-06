# Runbook — benchmark TRI ingest (M3.9.b)

Lands the **published** NIFTY total-return series — the real one, not §4.1's dividend estimate — in
`L0/nifty_tri_history/`, `L1/benchmark_tri/` and `sync_state`, for NIFTY 50, NIFTY IT and NIFTY
CPSE at whatever depth the endpoint has.

Entry point: `dataplatform/ingest/tri_backfill.py`.

## What makes this source unusual: it is three requests

`POST https://niftyindices.com/BackPage/getTotalReturnIndexString` returns an entire index's
history in one response. No session cookie, no Referer, no key. So the *whole* campaign is one
request per index, and **re-fetching later is the expensive path** (D8) — which is why the default
window starts at 1990-04-01, below every index's launch, and takes everything the endpoint gives.

Three requests is far under the ~200-request threshold AGENTIC_CONTEXT §4 reserves to the owner, so
this run needs no sign-off. It is still a driver run: check `uptime` and `ps aux | grep -E
'backfill|campaign'` first, and the runner takes the `niftyindices.com` host lease for its
lifetime so a second driver refuses to start rather than halving the 2.5 s spacing.

## Preconditions

- Postgres up and migrated (`make up`, `make migrate`) — the runner writes `sync_state`.
- Nothing else running against `niftyindices.com` (the lease enforces it; check first anyway).

## Live run

```bash
nohup uv run python -m dataplatform.ingest.tri_backfill \
  > ~/campaign/tri-backfill-$(date +%F).log 2>&1 &
```

Options: `--index SLUG` (repeatable) restricts the set; `--start` / `--end` set the window (`--end`
defaults to today and becomes the sync row's logical date). It takes about 10 seconds end to end;
the time is parquet writing, not the network.

Expected output, measured 2026-09-08:

```
nifty50: 6764 points, 1999-06-30 .. 2026-09-07 (NIFTY 50)
niftyit: 6765 points, 1999-06-30 .. 2026-09-07 (NIFTY IT)
niftycpse: 4382 points, 2009-01-01 .. 2026-09-07 (NIFTY CPSE)
benchmark_tri: 3 of 3 index(es) fetched, window 1990-04-01 .. 2026-09-08
```

## Verify

```bash
# sync_state: one PUBLISHED row per index, keyed nifty_tri_history/<slug>
make psql -- -c "select unit, logical_date, state from sync_state where source='nifty_tri_history'"

# the spot-check: the published level on a known date
uv run python -c "
import datetime as dt
from dataplatform.config import get_settings
from dataplatform.ingest.indices import read_tri_series, TRI_METHOD_PUBLISHED
s = read_tri_series('nifty50', dt.date(2026,3,30), method=TRI_METHOD_PUBLISHED,
                    data_root=get_settings().data_root)
print(s.points[-1].as_of, s.points[-1].tri_value)   # 2026-03-30 33655.4300
"
```

Reference levels on **2026-03-30**, recorded independently by the owner from the live endpoint on
2026-08-10 (HUMAN_DECISIONS D8) and reproduced by this ingest: NIFTY 50 **33655.43**, NIFTY IT
**41606.83**, NIFTY CPSE **11793.29**. NIFTY CPSE's earliest point is **1000.0000 on 2009-01-01** —
the index's published base — which is a second, independent sanity anchor.

## Resume, and re-running

`already_published` reads the **L1 artefact**, not the sync row: an index whose published series
already reaches the requested `start` is skipped and no request is made. Two consequences worth
knowing:

- A *computed*-fallback series on disk is **not** a reason to skip. That was the whole defect — the
  estimate existed, everything downstream used it, and nothing went looking for the real series.
- A stored series from 2021 does not satisfy a run asking back to 1990. Widen the window and it
  re-fetches; the L0 payload for the new window is a different file, so nothing is overwritten.

## Same-evening refresh: `tri_evening` (M13.7)

The Saturday `tri_refresh` keeps the series weekly. The paper session decides session D on D's
evening, and its regime filter needs D's published NIFTY 50 TRI that night. `tri_evening` is the
weekday job that lands it.

**When D is published.** These are measured facts, not documented ones:

| fetch (IST) | session D | newest row returned |
|---|---|---|
| 2026-09-08 09:41 | 09-07 is the previous session | 07-Sep: D is out by the next morning |
| 2026-10-05 16:08 | 10-05 | 01-Oct (02-Oct holiday): D is **not** out 38 min after the close |
| 2026-10-06 20:47 | 10-06 | 06-Oct (34608.14): D **is** out the same evening |

The first two come from L0 sidecar `fetched_at`s. The third is a deliberate one-request probe
through `leased_fetcher`. Each record's `RequestNumber` is .NET ticks of the request instant in
UTC, so it says when the request was made, not when the level was published. The earliest
publication time inside 16:08–20:47 has not been measured.

**Schedule.** The job fires at `50 19,20 * * mon-fri; 30 21 * * mon-fri` (19:50, 20:50 and 21:30
IST, Monday to Friday). A `;` joins crontabs whose union no single expression can say, and it is
still one job.
- 19:50 is the first attempt, after `daily_snapshot`'s niftyindices.com lease (19:15 + 30-minute
  budget).
- 20:50 is the retry after the measured 20:47 point.
- 21:30 is the last attempt. Its 10-minute budget ends before the paper session decides D at 21:45.

Each fire lands the latest session on or before today for the three default indices: one short
POST each (about 1 KB), with the window starting at the stored series' last level or 14 days back,
whichever is earlier. An index already at D makes no request, so later fires are no-ops once one
has landed.

**Tune the first fire after two weeks.** Each landing logs `tri_evening.first_landed` once per
index and session, with `landed_at_ist` and `attempts`. About two weeks after this ships, read
those events from the scheduler's log. The scheduler runs as the systemd user unit
`scheduler.service`, so
`XDG_RUNTIME_DIR=/run/user/$(id -u) journalctl --user -u scheduler.service --since -14d | grep tri_evening.first_landed`
should find them.

Tune on **`landed_at_ist`**, which is the instant of the attempt that landed the session (19:50,
20:50 or 21:30 for a scheduled fire). `attempts` counts tries on that session's sync row, and
`attempts == 1` does **not** prove the 19:50 fire landed it. A fire that was refused the host
lease, or died before `begin`, never touched the row, so a session first attempted, and landed,
at 20:50 also reads 1. If 19:50 shows up in `landed_at_ist` for nearly every session, the later
fires are only insurance. If it rarely does, move the first fire later rather than letting it fail
most evenings.

**Before dissemination.** The answer is kept in L0, and the name carries the attempt instant
(`tri_nifty50_<start>_<D>_at<YYYYMMDD>T<HHMMSS>.json`), so a later fire cannot collide with it. The
`nifty_tri_history/<slug>` row for D parks `FAILED` with `retryable=True` and is committed, so it
shows on `/status/sync`. L1 is left alone and the run is FAILED.

**Running it by hand: only after the close.** `uv run python -m dataplatform.scheduler run-once
tri_evening` owes the latest session on or before today, and that includes today. Run during
market hours, it asks for a level that cannot exist yet: it spends a request and leaves a FAILED
row and a FAILED run behind. Run it after the close, and in practice after the evening
dissemination (20:47 IST is the measured point).

**A missed evening heals itself.** When an evening lands session D, every earlier retryable FAILED
`nifty_tri_history/<slug>` row inside that window whose session the payload carries is walked
`PENDING → FETCHED → VALIDATED → NORMALIZED → PUBLISHED`. Its receipt is D's payload, the bytes that
actually carry the missed level, and each healed row logs one `tri_evening.healed` event. Gap scans
therefore stop reporting a session whose level L1 already holds. Two kinds of row stay as they
are: a non-retryable failure (a dead end on purpose), and a row for a date the payload does not
carry.

D's row is committed before healing starts. A heal that fails logs `tri_evening.heal_failed` and
fails the run without undoing D, and the missed rows stay FAILED and retryable for the next
landing.

**Lag budget is now 1 session.** `tri_evening` answers for `nifty_tri_history` with
`max_lag_sessions=1`, and `lag_budgets` keeps the tighter of that and `tri_refresh`'s 6. So
`/status/sources` shows the source overdue after **one** missed evening, not after a missed
week. A single missed evening heals at the next landing; an overdue that persists means the job,
the host or the endpoint needs looking at.

**The paper job's cron.** PR #69 is moving `PAPER_SESSION` to **21:45 IST** (`45 21 * * mon-fri`),
after the last `tri_evening` attempt. The paper job decides "the latest owed session", so a 21:45
run still decides D.

## Moving the lake

`data/L0` is the immutable record and L1 is a derivation of it, never copied between lakes
(`move-campaign-to-a-server.md`). That rule holds here too, and since 2026-09-08 there is an entry
point for it:

```bash
# 1. carry the raw payloads across, with their receipts. cp -a, so mode 0o444, the mtimes and
#    each sidecar's original `fetched_at` survive: the record of when these bytes were first seen
#    is itself immutable, and `L0Store.put` would mint a fresh one.
cp -a <from>/data/L0/nifty_tri_history <to>/data/L0/

# 2. re-derive L1 from them. No network, no request, no sync write. ~10 s for the three indices.
DATA_ROOT=<to>/data uv run python -m dataplatform.ingest.tri_backfill --from-l0
```

`--from-l0` reads every stored `nifty_tri_history` payload back through `L0Store.get` (which
re-verifies the recorded sha256), recovers each one's index from its filename, re-parses and
rewrites the L1 partitions. It is idempotent, and `tests/unit/test_tri_backfill.py` pins the
property the route depends on: **the parquet it writes is byte-identical to what the fetching run
wrote**, so re-deriving is not a second-best substitute for copying L1 — it is the same bytes with
the immutable layer as the single source. Measured on the real transfer of 2026-09-08: 6,764 +
6,765 + 4,382 points into 6,765 date partitions, 17,911 files, all byte-identical to the run that
fetched them.

Prefer this over re-running the fetch whenever the payloads are already on disk. A re-fetch costs
three requests, but it also mints **new** L0 receipts with today's timestamps, and the endpoint
stamps every record with a `RequestNumber` that regenerates per request — so the same logical
history comes back as different bytes under a different key, and the lake ends up with two raw
records of one fact. Route (2), a re-run against the target lake, stays the answer when the
payloads are *not* on disk:

```bash
DATA_ROOT=<to>/data uv run python -m dataplatform.ingest.tri_backfill
```

**What `--from-l0` does not do: touch `sync_state`.** The sync row records an *ingestion*, and a
rebuild is not one — L0 is unchanged, and §4.4 closes `PUBLISHED` absolutely, so `begin` on an
already-published date is an illegal transition by design. Two consequences:

- Moving a lake *within one host* (the 2026-09-08 transfer between worktrees) needs nothing extra:
  the sync rows already exist and their `checksum` and `l0_path` still name the payloads that were
  carried across, so the row and the lake agree.
- Moving a lake to a host with a **different database** leaves `/status/sync` with no history for
  these payloads, and no rebuild can honestly invent one. Either accept that (nothing reads the
  sync row to find the benchmark — `read_tri_series` and `_resolve_benchmark` read L1) or re-fetch
  on that host so the ingestion really does happen there.

Do not hand-edit `L1/benchmark_tri`: the stored `knowable_date` is re-derived and checked on read,
so an edited partition raises rather than quietly moving the PIT boundary.

## Failure modes

| symptom | meaning | action |
|---|---|---|
| `ParseError: body is markup, not JSON` | the URL path is wrong, or the site is serving its home page | check `url_template` in the register is `/BackPage/…` with **no** `.aspx`; the sync row parks `FAILED` with `retryable=False` because the same request returns the same markup |
| `ParseError: body is a JSON object` | the endpoint started wrapping records in an envelope | a format-era change: freeze a new fixture, then widen the parser deliberately |
| `ParseError: … not a decimal string` | levels arrived as JSON numbers | same — do **not** coerce; a float in the benchmark is a defect (CLAUDE.md) |
| `ParseError: … different index` | the name echo does not match what was requested | the endpoint answered about another index; nothing is filed under the wrong slug |
| `IngestError: … not editable` on read | a stored `knowable_date` disagrees with the schedule | a partition was hand-edited; re-derive it |
| lease refused, naming a holder | another driver holds `niftyindices.com` | wait for it; do not lower the spacing |

`Content-Type` is **`text/html` even on success** for this source, so no content-type check can
tell a good response from the block page (D9). The shape assertion in `parse_tri_native` is the
only thing that can, and it is why a 200 with markup never becomes a benchmark.
