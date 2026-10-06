# The daily snapshotter — source probe and first capture (OPS, 2026-09-08)

The five snapshot-only feeds this job exists for, probed live before a line of the job was
written. **Nothing here was fetched by the test suite**; the payloads are frozen under
`tests/fixtures/nse_market_structure/2026-09-08/` and every test replays them.

## Why this job has a deadline and nothing else in the programme does

Each endpoint below serves exactly one snapshot — the current one. There is no date parameter, no
archive host, and no historical form. `ops/studies/multi-fund-data-study-2026-09-07.md` (wave W8)
put it plainly: *none of these has a past*, and their history is destroyed at a rate of one day per
day. Before today the lake held one day of index constituents (2026-09-03) and nothing else on
this list; `job_run` and `scheduler_heartbeat` were both empty, so the platform had never run a
scheduled job at all.

## Method

One GET per URL pattern, 3.2 s spacing per host, browser User-Agent and the register's Referer,
warm session on `www.nseindia.com`, hard stop armed on the first 403 from a data endpoint (never
tripped). Host leases were held in the authoritative lake (`/home/ubuntu/stock-manager/data`) for
all four hosts for the duration. **7 requests total.** No login anywhere, no retry with a different
agent, no attempt to evade anything.

Probed at 2026-09-08T18:36 IST.

## Results

| Candidate | URL | Status | Bytes | Content-Type | Verdict |
|---|---|---|---|---|---|
| Industry classification | `nsearchives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv` | 200 | 49,377 | `text/csv` | **VERIFIED** — 755 rows, 22 sectors, ISIN native |
| Industry classification (alt) | `niftyindices.com/IndexConstituent/ind_niftytotalmarketlist.csv` | 200 | 78,512 | `text/html` | **REJECTED** — soft 404: HTTP 200 carrying the site's HTML shell. The `IndexConstituent/` path the register uses for the sectoral lists does **not** serve the total-market file |
| Price bands | `nsearchives.nseindia.com/content/equities/sec_list.csv` | 200 | 172,082 | `text/csv` | **VERIFIED** — 3,524 rows, symbol-keyed |
| ASM | `www.nseindia.com/api/reportASM` | 200 | 59,094 | `application/json` | **VERIFIED** — `longterm` 138 + `shortterm` 84, ISIN native |
| GSM | `www.nseindia.com/api/reportGSM` | 200 | 18,757 | `application/json` | **VERIFIED** — 75 rows, ISIN native |
| ESM | `www.nseindia.com/api/reportESM` | 200 | 64,437 | `application/json` | **VERIFIED** — 274 rows, ISIN native |
| Surveillance indicator | `www.nseindia.com/api/surveillance-indicator` | 404 | 379 | `text/html` | **REJECTED** — no such endpoint; the three framework endpoints above cover it |

Checksums are recorded per entry in `source_register.yaml` and match the frozen fixtures byte for
byte (`sha256sum tests/fixtures/nse_market_structure/2026-09-08/*`).

## The finding that shaped the failure design

ASM, GSM and ESM each **stamp every row with the file's own date**, not the name's entry date:

```
ASM longterm  {'08-Sep-2026': 138}  distinct=1
ASM shortterm {'08-Sep-2026':  84}  distinct=1
GSM           {'08-Sep-2026':  75}  distinct=1
ESM           {'08-Sep-2026': 274}  distinct=1
```

One distinct value per file, equal to the capture date. NSE is known to answer 200 with a previous
session's payload (`nse_sec_bhavdata_full` does exactly this on a market holiday), and this stamp
is what makes that detectable from the payload alone. The snapshotter therefore parses each
payload's own date and, when it does not equal the capture date, records a distinct `STALE` outcome
and a `FAILED` sync row rather than filing yesterday's surveillance list under today.

The two CSVs carry no date anywhere. For those the guard is structural — exact header match and a
minimum row count — which is what catches the soft-404 case the `niftyindices.com` candidate
demonstrated above: HTTP 200, plausible byte count, HTML body.

## Register consequences

Five new entries under a new §4.1 row 18 "Market-structure snapshots", proposed as an amendment in
EXECUTION_PLAN §12. All five are **archive-only** (`parser: {id: null, task: null}`) — the bytes
are captured for L0's sake and no L1 parser is written yet, which is the honest state and the one
the deadline demands.

All five carry `era.start: 2026-09-08`. That is not a claim about when the URL began serving. For a
snapshot-only endpoint the only dates a file was ever *obtainable* for are the dates we were
running, so the era begins at first capture — otherwise the D7 gap report manufactures an owed
session for every trading day since 1994 and buries the one day that actually matters.
