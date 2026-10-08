# Postgres and the status API are loopback-only (M15.6)

## What changed

`ops/docker-compose.yml` used to publish `"${POSTGRES_HOST_PORT:-5433}:5432"` and
`"${APP_HOST_PORT:-8000}:8000"`. With no host IP, docker binds **both `0.0.0.0` and `[::]`**,
and its own iptables `DOCKER` chain is consulted before the host's `INPUT` chain, so a host
firewall does not close the port. On 2026-10-08 `ss -ltn` showed `0.0.0.0:5433`, `[::]:5433`,
`0.0.0.0:8000` and `[::]:8000`: a Postgres with the dev-default credentials and the
unauthenticated status API on every interface, reachable from outside or not depending only on
the cloud security group.

Both are now published on `127.0.0.1` only:

```yaml
- "127.0.0.1:${POSTGRES_HOST_PORT:-5433}:5432"
- "127.0.0.1:${APP_HOST_PORT:-8000}:8000"
```

The host *port* is still a variable; the host *address* is not, because an override is one
`.env` line away from `0.0.0.0`. `tests/unit/test_compose_ports.py` fails on any publish with no
host IP, a wildcard, a non-loopback address, or an address taken from a variable.

## Who uses these ports — all on this host

| Consumer | Reaches | After M15.6 |
|---|---|---|
| scheduler (`systemctl --user` unit) | `DATABASE_URL` default `localhost:5433` (`dataplatform/config.py`) | unchanged; `localhost` resolves to `127.0.0.1` here |
| `make migrate`, `tests/integration`, ad-hoc `psql` | `localhost:5433` from `.env` / the default | unchanged |
| the app container | `postgres:5432` on the compose network | unaffected — not a published port |
| app healthcheck | `127.0.0.1:8000` *inside* the container | unaffected |
| failure alerts (`dataplatform/alerts.py`) | Telegram over HTTPS; reads job state from Postgres, not over HTTP | unaffected |
| runbook `curl`s | `127.0.0.1:8000/status/...` from a shell on the host | unchanged |
| Caddy (`caddy-caddy-1`, outside this repo) | `omnigent-server:8000` on its `edge` network — not the trading app | unaffected |

A libpq client given `localhost` tries every address it resolves to, so even on a host where
`localhost` also resolves to `::1` the refused IPv6 attempt falls through to `127.0.0.1`.

## Reaching the status API or Postgres from another machine

SSH tunnel; nothing is published for you any more:

```bash
ssh -N -L 8000:127.0.0.1:8000 -L 5433:127.0.0.1:5433 <you>@<this-host>
# then, on your machine:
curl -s 127.0.0.1:8000/status/sync
psql "host=127.0.0.1 port=5433 dbname=trading user=trading"
```

## The one-time post-merge step

The change takes effect only when the containers are recreated. It was **not** done by the
task that made it. The owner runs, from the merged main checkout:

```bash
docker compose -f ops/docker-compose.yml up -d
```

That recreates both containers (the `ports` of each changed). Postgres data survives: it lives in
the **named volume `pgdata`** (`pgdata:/var/lib/postgresql/data`, declared under top-level
`volumes:`), which `up -d` reattaches; only `down -v` would remove it. The lake is a host bind
mount and is untouched. The app is rebuilt from the current checkout only if its image is
missing — `up -d` does not rebuild an existing image.

**When:** a quiet window. Not 19:15–21:50 IST (`daily_snapshot` 19:15 through `paper_session`
21:45), not 02:00–05:45 IST (`fundamentals_forward` from 02:00), not Saturday 09:00–11:00 IST
(`index_press_refresh` 09:00, `ca_refresh` 10:00) — `dataplatform/scheduler/registry.py` has the
full list. Postgres is down for the seconds the recreate takes; a job that connects in that gap
fails, so pick a minute no job fires in (`failure_alerts` runs every 15 minutes — avoid :00, :15,
:30, :45).

**Then check:**

```bash
ss -ltn | grep -E ':(5433|8000)\b'
#   expect exactly: 127.0.0.1:5433 and 127.0.0.1:8000 — no 0.0.0.0, no [::]
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}' | grep trading-platform
#   both (healthy), ports 127.0.0.1:5433->5432/tcp and 127.0.0.1:8000->8000/tcp
XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user status scheduler --no-pager
#   active (running); its next job connects without error (journalctl --user -u scheduler)
curl -s 127.0.0.1:8000/health
curl -s "127.0.0.1:8000/status/sync" | jq '.'
```

## Independently of this change

Check the cloud security group for inbound 5433 and 8000 and remove any rule that allows them.
Loopback binding makes the security group irrelevant for these two ports, but a rule that once
admitted them is a rule nobody remembers the reason for, and it will admit whatever is next
published there.
