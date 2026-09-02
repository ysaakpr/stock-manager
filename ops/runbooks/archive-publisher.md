# Runbook — archive publisher (D6)

The archive publisher turns a backfilled trading date's normalized L1 data into one downloadable
daily **bundle** — Parquet + CSV — described by a checksummed **manifest** that maps every output
file back to the immutable L0 payloads it derived from (invariant #1). `GET /archives?date=` serves
the manifest; `GET /archives/download?date=&name=` serves the files.

## ⚠️ Legal — local and personal download only (HUMAN_GATE)

These bundles contain exchange (NSE/BSE) data. **Public redistribution of exchange data is a legal
question reserved to the human** (EXECUTION_PLAN §10, AGENTIC_CONTEXT §3.8): it likely requires an
exchange data licence. Until that question is answered by the owner:

- the publisher writes bundles only under the operator's own lake (`<data_root>/archives/…`), and
- `/archives/download` serves only the operator's own host — do not expose it publicly, put it
  behind a public URL, a CDN, or a shared bucket, or hand a bundle to a third party.

An agent must not act on the redistribution question; it may only research and write a memo.

## Publish a bundle

Reads L1 off disk and writes files + one `archive_bundle` row. Never opens a socket.

```python
from dataplatform.archives import publish_bundle
from dataplatform.clock import SystemClock
from dataplatform.store.db import connection
from dataplatform.store.l0 import L0Store

clock = SystemClock()
store = L0Store(clock=clock)  # default lake from settings
with connection() as conn:  # commit is the caller's
    report = publish_bundle(
        conn,
        store,
        logical_date,
        clock=clock,
        archive_root=settings.data_root,  # bundles live under <archive_root>/archives/
    )
    conn.commit()
print(report.file_count, report.total_bytes, report.manifest_sha256)
```

The bundle lands at `<archive_root>/archives/<date>/` with `prices_raw.parquet`,
`prices_raw.csv` and `manifest.json`. `bundle_path` in the row is stored **relative** to the
archive root (`archives/<date>`), so the same row means the same bundle from the container, a
restored backup, or a developer's checkout.

## Idempotency

Bundles are derived, not L0, so **re-publishing a date is safe** and is the first thing to try if a
run is in doubt. The Parquet is a byte-identical copy of the L1 partition, the CSV is written from
the same table, and the manifest carries no wall-clock field — so re-deriving the same date from the
same L0 produces byte-identical files and an identical `manifest_sha256`. Only `published_at` in the
row moves.

## When it refuses (`ArchivePublishError`)

The publisher fails loud rather than emit an empty or unprovable bundle:

- **no L1 partition for the date** — the date is not backfilled (run D4/M1.8 first); nothing is
  recorded.
- **no L0 payloads for the date** — a bundle's files must map to the L0 refs they derived from
  (acceptance 3), and there are none. Backfill L0 first.

## Verify a downloaded bundle

For each file in the manifest, hash the bytes and compare to the manifest's `sha256`; the file's
`lineage` names the L0 payloads (source, filename, sha256) it came from. `manifest_sha256` in the
`archive_bundle` row is the hash of `manifest.json` on disk, so a stored row can be checked against
the bundle instead of assumed to match it.
