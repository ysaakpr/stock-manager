# Moving a fetch campaign to another machine, and bringing the data home

Written for the M10.4 fundamentals campaign, but the shape applies to any of the backfills.

The design already supports this, and the reason is invariant #1: **L0 is immutable and
checksummed, and everything else is derived from it.** So the archive is the only thing that has to
travel. L1, L2 and the `sync_state` checkpoints are all reproducible locally from L0 with no
network — which is what `--rebuild-from-l0` exists for.

Concretely: **out ≈ 1 GB, back ≈ 3 GB, and the homecoming needs no internet at all.**

## What has to travel, and what does not

| | Size | Out | Back | Why |
|---|---|---|---|---|
| `data/L0/` | 593 MB → ~3.1 GB | optional | **required** | The archive. The only irreplaceable thing. |
| `data/L1/prices_raw/` | 410 MB | **required** | no | The runner's universe is the ISINs traded in the window; without it every filing is "out of universe". |
| `security_master`, `symbol_history`, `exchange_listing` | ~1.7 MB | **required** | no | Symbol→ISIN resolution and the parser's rename cross-check. |
| `sync_state` | 6 MB | optional | no | Resume. Skip it and the remote re-fetches what is already done — which the L0 reuse then makes free anyway. |
| `data/L1/pit_fundamentals/`, `data/L2/` | 3 MB, 21 MB | no | no | Derived. Rebuilt from L0 at both ends. |

Send `sync_state` if you want the remote to pick up mid-campaign; leave it out for a clean start.
Send L0 if you would rather not re-fetch the ~9,700 filings already done. Neither is required for
correctness — only for not repeating work.

## 1. Package what the server needs

```bash
cd ~/Documents/work/stocks
ops/backup.sh                      # pg_dump + an L0 manifest, into ops/backups/<ts>/
COPYFILE_DISABLE=1 tar -czf /tmp/campaign-out.tgz \
    data/L1/prices_raw \
    data/L0/nse_financial_results_index \
    data/L0/nse_xbrl_filing \
    ops/backups/$(ls -t ops/backups | head -1)
```

**`COPYFILE_DISABLE=1` is not optional on macOS.** Without it `tar` writes an AppleDouble `._name`
companion for every file, which Linux extracts as a real file — 25,080 of them on a first attempt
here, doubling the apparent document count and putting junk inside an immutable lake. If you
already extracted without it, clean up before doing anything else:

```bash
find data ops/backups -name '._*' -delete
```

Then confirm the counts match the source: `find data/L0/nse_xbrl_filing -name '*.xml' | wc -l`
should equal the number of `*.meta.json` sidecars, and both should equal what the laptop reports.

`ops/backup.sh` dumps the database and *fingerprints* L0 rather than copying it — it is
deliberately not a transfer tool, so the `tar` above carries the lake itself. Keep the manifest: it
is how you verify the archive survived the trip.

## 2. Stand the server up

```bash
git clone <this repo> stocks && cd stocks
uv sync
make up && make migrate
tar -xzf campaign-out.tgz
echo "HTTP_MIN_INTERVAL_SECONDS=2.5" > .env
```

Load the database. Note that **`ops/restore.sh` is a verification drill, not a restore** — it
`pg_restore`s into a *scratch* database and compares row counts, then drops it, precisely so that
an unattended run can never touch the live one. Verify with it first, then do the real load
deliberately, which is the shape `ops/runbooks/backup-restore.md` documents:

```bash
ops/restore.sh ops/backups/<ts> --no-l0     # verify the dump (scratch DB, then dropped)

# `make migrate` built the schema and stamped schema_migrations; the dump carries the same stamps,
# so clear that one table rather than excluding it and leaving the two records to disagree.
docker compose -f ops/docker-compose.yml exec -T postgres \
  sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -q -c "TRUNCATE schema_migrations"'

docker compose -f ops/docker-compose.yml exec -T postgres \
  sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges \
         --data-only --disable-triggers --single-transaction --exit-on-error' \
  < ops/backups/<ts>/postgres.dump
```

Three flags each earn their place, and each was learned by the restore failing without it:

* `--no-l0` skips the manifest re-check, which is what you want on a host whose lake is only the
  slice you shipped.
* `--disable-triggers` is required because `--data-only` restores tables **alphabetically**, so
  `adjustment_factors` lands before `security_master` and every foreign key fails. Postgres
  implements FK checks as triggers, so this is the documented way through.
* `--single-transaction` makes a partial restore impossible. Without it the first failure leaves
  FK-broken rows behind and the only clean recovery is dropping the database and starting over.

Verify against the source before starting anything — row counts should match exactly:

```bash
uv run python -c "
from dataplatform.config import get_settings
from dataplatform.store.db import connection
with connection(get_settings()) as c:
    for t in ('security_master','symbol_history','exchange_listing','corporate_actions','sync_state'):
        print(t, c.execute(f'SELECT count(*) FROM {t}').fetchone()[0])"
```

Then run it exactly as here — the campaign driver and `ops/fundamentals_progress.sh` both work
unchanged:

```bash
uv run python -m dataplatform.ingest.fundamentals_backfill \
    --from 2018-04-01 --to 2026-09-03 --chunk-months 3
```

**One request budget, not two.** The politeness limit is per host, not per machine, so running the
campaign on the server while it is still running here means two independent rate limiters against
`nsearchives.nseindia.com` — the effective spacing halves and the recorded 2.5 s is breached through
a side channel. **Stop the laptop's driver before starting the server's**, and do not split years
across the two machines to "go faster".

### Things the server may lack

`uv` is often absent on a fresh box (`curl -LsSf https://astral.sh/uv/install.sh | sh`, then
`export PATH="$HOME/.local/bin:$PATH"`), and Ubuntu images generally have no `zsh` — so
`ops/fundamentals_progress.sh`, which is zsh, will not run there. Drive the campaign with a bash
script on the server and keep the zsh progress view for the laptop.

## 3. Bring the archive home

Only `data/L0/` needs to come back.

```bash
# on the server
tar -czf campaign-back.tgz data/L0
# on the laptop
tar -xzf campaign-back.tgz            # merges into the existing lake
```

Merging is safe because L0 is content-addressed: every payload has a `.meta.json` sidecar carrying
its sha256, and `L0Store.get` re-verifies the digest on every read. A file that arrived corrupt
fails loudly on first use rather than becoming a silently wrong number. Nothing is overwritten
in-place, because a payload's identity is `(source, logical_date, filename)` and the same key always
holds the same bytes.

Then rebuild locally, **offline**:

```bash
uv run python -m dataplatform.ingest.fundamentals_backfill \
    --from 2018-04-01 --to 2026-09-03 --chunk-months 3 --rebuild-from-l0
```

`--rebuild-from-l0` derives `pit_fundamentals` and the checkpoints from the archive and **never
opens a socket** — index chunks included. A payload the lake does not hold is a loud failure, not a
quiet fetch, so a store rebuilt from a partial archive cannot pass for one rebuilt from a whole
archive. Two unit tests hold this: one asserts a rebuild over a transferred L0 reproduces byte-equal
facts while a refusing transport is wired in, the other asserts an incomplete lake fails with the
reason named.

It is also the only mode that **batches its L1 writes**, and that changes what a kill costs. The
per-filing path rewrites a whole `filing_date` partition per filing, which a results-season
partition of 762 filings pays 762 times over; a rebuild instead buffers each partition's facts and
writes it once per index chunk. Measured over 10,132 real filings, the write path drops from 410s
to 3.4s, and every partition it produces is byte-identical to the one the per-filing path writes.

The checkpoint moves with the facts. Nothing reaches `sync_state` while a filing is buffered, and
the whole batch's rows are written and committed only after every partition is on disk — so a
killed run never leaves a `PUBLISHED` row for a filing whose facts were still in memory. What it
loses is up to one chunk's worth of *re-derivation*, which here is local reads and parses and no
requests. That is why the runner refuses to batch on a fetching run: there the same kill would cost
another chunk of NSE requests. A `Ctrl-C` is handled rather than merely survived — the buffer is
flushed and checkpointed before the run ends.

Verify the round trip:

```bash
ops/fundamentals_progress.sh
uv run pytest tests/unit/test_fundamentals_backfill.py tests/unit/test_xbrl.py -q
```

## Why index chunks are not reused unless you ask

`--rebuild-from-l0` is opt-in rather than automatic, and that is a deliberate asymmetry. A *filing*
document is immutable, so reusing its stored payload is always right and the runner does it by
default. The *announcements index* is a live feed: a stored chunk is a snapshot of what had been
broadcast when it was fetched, so reusing it by default would make a forward sync structurally blind
to anything filed since — the trap described under "Picking up new filings" in
[`fundamentals_backfill.md`](fundamentals_backfill.md). Asking for a rebuild by name keeps "rebuild
what I have" and "go and look for more" from being the same command.

## Costs

| | |
|---|---|
| Out | ~1 GB (prices + current L0 + a dump) |
| Back | ~3.1 GB (L0 only, compresses well — XBRL is verbose XML) |
| Local rebuild | **0 requests**; minutes of CPU, dominated by parsing rather than writing |
| Re-fetch if you skip L0 on the way out | ~9,700 filings ≈ 6.7 h at 2.5 s |

The last row is the only real decision. Sending L0 out costs ~600 MB of upload and saves about
seven hours of refetching; skipping it costs nothing but time you are already spending elsewhere.

## Day to day: `ops/remote.sh`

Once a server is stood up this way it becomes the standing testing engine (CLAUDE.md "Development
model"). `ops/remote.sh` wraps the routine — `status`, push-verified `sync`, `check`, `test`, `run`,
`logs`, `shell` — reading the connection details from the untracked `.remote.env`, so nothing in
this runbook or any other committed file names the host, the user or the key path.
