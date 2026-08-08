# ops — the deployable stack

Two services and no more (decision #13). `ops/docker-compose.yml` is the whole deployment:
Postgres for state, one app container for the monolith. Nothing else belongs here — no redis,
no nginx, no sidecars.

| Service | Image | Holds / runs | Ports (host → container) |
|---|---|---|---|
| `postgres` | `postgres:16` | masters, sync state, cases, policies, append-only journal | `5433 → 5432` |
| `app` | built from `ops/Dockerfile` | the D5 status API (`uvicorn dataplatform.status.api:app`) | `8000 → 8000` |

## Running it

```bash
make up      # creates data/L0 data/L1 data/L2, then docker compose up -d
make logs    # follow both services
make psql    # interactive shell on the container DB
make down    # stop; the pgdata volume and the lake survive
make backup  # pg_dump + a checksummed L0 manifest into ops/backups/<ts>
make restore # rebuild the newest backup into a scratch DB and verify every row count
```

`make restore` is a *drill*: it never writes to the live database, and it refuses a target named
after it. Real recovery, the object-storage gap, and the executed drill transcripts are in
[runbooks/backup-restore.md](runbooks/backup-restore.md).

`docker compose -f ops/docker-compose.yml up -d` works identically with no environment set —
every variable has a default. `make up` additionally creates the lake directories, which
matters: if docker creates a bind-mount source itself it creates it owned by root, and the app
runs as uid 1000.

## Postgres is on host port 5433, not 5432

Inside the compose network the database is `postgres:5432` and that is what the app uses
(`DATABASE_URL=postgresql://trading:trading@postgres:5432/trading`). The **host-side** publish
is 5433, deliberately:

> This host already runs its own Postgres bound to `127.0.0.1:5432`. Docker will publish over
> it on `0.0.0.0:5432` without complaint, but the kernel prefers the more specific bind, so a
> host client connecting to `localhost:5432` reaches the *other* server and fails with
> `FATAL: role "trading" does not exist`. Verified on 2026-08-08.

So, for anything running on the host — `make migrate`, `tests/integration`, `psql` from a
terminal — put this in your repo-root `.env`:

```
DATABASE_URL=postgresql://trading:trading@localhost:5433/trading
```

`.env.example` ships the conventional `localhost:5432`, which is right on a host without its
own Postgres; on this one it silently resolves to the wrong server. Set
`POSTGRES_HOST_PORT=5432` (in `ops/.env` or the environment) to get the conventional mapping
back once the host Postgres is gone.

## Configuration

Compose reads variables from the environment or from **`ops/.env`** — its project directory is
this file's directory, so the repo-root `.env` is *not* auto-loaded for interpolation. All have
defaults:

| Variable | Default | Meaning |
|---|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `trading` | container DB credentials |
| `POSTGRES_HOST_PORT` | `5433` | host-side publish (in-network is always 5432) |
| `APP_HOST_PORT` | `8000` | host-side publish for the status API |
| `DATA_ROOT` | `../data` | **host** path of the data lake |

The repo-root `.env` *is* passed into the app container (`env_file`, `required: false`), so
runtime settings the owner keeps there reach the process. `DATABASE_URL`, `DATA_ROOT` and `TZ`
are set explicitly in the compose file and win over it — their host-side values would be wrong
inside the container.

## The data lake is a bind mount

`DATA_ROOT` (host, default `<repo>/data`) is mounted at `/data`, and the app sees
`DATA_ROOT=/data`. L0/L1/L2 therefore live on the host filesystem and survive every rebuild,
which is what makes L0's immutability (invariant #1) a property of the disk rather than of a
container's lifetime. Postgres, by contrast, uses a **named volume** (`pgdata`): it is
rebuildable from L0 + migrations, and its data directory must never be edited from the host.

On a Linux host the bind mount enforces uid: `sudo chown -R 1000:1000 "$DATA_ROOT"`. Docker
Desktop maps ownership for you, so macOS needs nothing.

## The image

`ops/Dockerfile` — `python:3.12-slim`, dependencies installed by `uv` from `uv.lock` (so the
container runs the same resolution `make check` does), non-root uid 1000, no `uv run` at
runtime (the project venv is first on `PATH`). Only the §8.2 product packages are copied in;
`tests/` and `orchestrator/` are not part of the product. The root `.dockerignore` keeps the
host `.venv/`, `.git/` and `data/` out of the build context.

Rebuild after a dependency change:

```bash
docker compose -f ops/docker-compose.yml build app && make up
```

A code change needs the same rebuild — there is no source bind mount, on purpose: what runs in
the container is what was built, which is what makes a container's behaviour reproducible.

## Status of the app entrypoint

`dataplatform/status/api.py` is a skeleton serving only `/health` (process liveness, no
database access). **M0.5** replaces it with the §4.4 surface — `/status/sync`,
`/status/sources`, `/status/gaps`, `/status/quality`, `/archives` — and a DB-backed scheduler
heartbeat. The compose stack does not change when that lands.

## Verifying the stack by hand

```bash
docker compose -f ops/docker-compose.yml ps                      # postgres healthy, app healthy
curl -s localhost:8000/health                                    # {"status":"ok"}
docker compose -f ops/docker-compose.yml exec -T app \
  python -c "import os,psycopg; print(psycopg.connect(os.environ['DATABASE_URL']).info.dsn)"
```

The third command is the one that proves the app reaches Postgres *by service name*; a
localhost DSN inside the container points at the container itself.
