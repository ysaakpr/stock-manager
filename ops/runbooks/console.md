# Operator console (M18)

One web view of the running platform, served by the status API itself at `/console` — no new
service, no new port, read-only. It reads through `dataplatform.status.queries`, the same
functions the JSON endpoints use, so a page never disagrees with `/status/*`.

## Reaching it

**From a browser: `https://sm.<domain>/console`** (M18.6), behind one shared basic-auth login.
`<domain>` is the `DOMAIN` in the front proxy's untracked `/home/ubuntu/caddy/.env`.

- The host's shared front proxy (Caddy, its own compose project in `/home/ubuntu/caddy`, outside
  this repo) has an `sm.{$DOMAIN}` site that proxies **only `/console*`** to the app over the
  external `edge` network, as `stock-manager-app:8000`. Every other path — `/status/*`, `/health`,
  `/archives`, `/cases/*` — answers 404 there. The console's "raw JSON" links to `/status/*` work
  over the tunnel below, not over `sm.`.
- The app joins `edge` in `ops/docker-compose.yml`; Postgres never does
  (`tests/unit/test_compose_ports.py`). The app's own port is still published on loopback only.
- Login: the username and password live in the untracked, mode-600
  `~/.config/smagent/console-basic-auth` on the server. Caddy holds only the bcrypt hash, inline in
  its Caddyfile (not a repo, never committed). To rotate: generate a new password into that file,
  `docker exec -i caddy-caddy-1 caddy hash-password < <(sed -n 's/^password=//p' FILE)` (stdin,
  never argv), replace the hash in the `sm.` block, validate and reload (below).
- Edit the Caddyfile **in place** (it is a single-file bind mount; a tool that writes a new file
  and renames it leaves the container reading the old inode), then
  `docker exec caddy-caddy-1 caddy validate --config /etc/caddy/Caddyfile` and
  `docker exec caddy-caddy-1 caddy reload --config /etc/caddy/Caddyfile`. A reload, not a
  restart: the same Caddy serves every other app on the host.

**Over ssh**, with no login, everything the API serves:

```bash
ssh -L 8000:127.0.0.1:8000 <the server>      # host and user from your own ssh config
# then open http://127.0.0.1:8000/console in a local browser
```

Nothing on the page is loaded from outside the process: htmx 1.9.12 is vendored at
`dataplatform/status/console/static/` (sha256 pinned in `tests/unit/test_console_ops.py`, SRI on
the script tag), so the page works with no outbound network. Every time shown is Asia/Kolkata,
labelled `IST`, as of the injected clock.

The running `app` container serves whatever code it was built from — after a merge, the console
appears only once that container is rebuilt and restarted (an owner step).

## The Operations page (`/console`, `/console/ops`)

| Panel | What it shows | Where it comes from |
|---|---|---|
| Health banner | OK/DEGRADED, database reachable, scheduler heartbeat state, age and threshold | `assess_health` — the same verdict as `GET /health` |
| Jobs | every registered job, worst first (FAILING, OVERDUE, NEVER_RAN, RUNNING, OK): state, cron, last run, its duration, last success, due since, next fire, last error (masked). Refreshes every 30 s | `read_job_health` — the same call `GET /status/jobs` makes |
| Job drill-down (`/console/ops/jobs/<name>`) | the newest 50 `job_run` rows: start, finish, state, duration, instance, error (masked, whole) | `read_job_runs` |
| Alerts | open `alert_condition` rows (critical first), the alert channel (`ALERT_PROVIDER`; `log` means nothing pages), and the `failure_alerts` job's state and last tick | `read_alerts` |
| Backups | for `postgres_backup` and `l0_backup`: the job's state, last run and last success, the newest dump (or the L0 manifest) under `BACKUP_ROOT` with size, IST stamp and age | `read_backups` (`store.backup.list_backups`) |

What it does not show, and why:

- **A restore-drill verdict.** The drill prints its report and records none, so the panel says so
  (BACKLOG M18.1). Run `uv run python -m dataplatform.store.backup drill` for a fresh one.
- **Backup files, when served from the container.** The `app` container does not mount
  `BACKUP_ROOT`; the panel says "not visible to this process" rather than "no backups". The job
  lines still come from `job_run` and are authoritative for whether the jobs ran.
- **A secret.** Every free-text field (`job_run.error`, alert titles, the database error) goes
  through `dataplatform.redaction.mask_and_truncate` before it renders, even when the writer
  already masked it.

## Accepted deviations: amber is known, red is news

**Rule (owner):** an ERROR on these pages means something deviated from an agreed plan. A failure
the owner has knowingly accepted is shown as **ACCEPTED** (amber, dashed edge) with the reason
beside it; hover it for the entry id, since/review dates and basis, or open the job's drill-down.
The agreements are `dataplatform/status/accepted_deviations.yaml`, read by
`dataplatform.status.deviations`. Today it holds the api.bseindia.com 403 block (HUMAN_DECISIONS
D26): the three BSE sources, the BSE legs of `ca_refresh` and `announcements_capture`, their
`failure_alerts` conditions and the BSE FAILED gap days.

- **Narrow by design.** A source is accepted only while *every* FAILED row of its current streak
  is the known error; a job only while its run raised the known exception and every leg it failed
  is an accepted source failure (an NSE leg failing keeps it red); an alert only while its subject
  is accepted right now. A new failure mode on an accepted source or job stays red, and sorts above
  the accepted ones.
- **Expiry.** An entry with `review_by` stops matching the day after: the item turns red again and
  says *acceptance … expired — review it*.
- **Counts.** Headings and `/status/*` split `errors` from `accepted`; `/status/gaps` adds
  `accepted_total`. Every `healthy`, and `fully_explained`, keep their strict meaning — an
  accepted failure is still `healthy: false`.
- **Alerts.** `failure_alerts` opens an accepted condition at **warning**, not critical, with the
  remark in its body; an open critical that becomes accepted is lowered without a page, and an
  accepted one that stops being accepted is raised back to critical **with** a page. The fetcher's
  own hard-stop alert on a 403 spike (`ingest:403-spike:<host>`) is sent by the ingestion process,
  not by `failure_alerts`, and is not affected.
- **Display and alerting only.** No gate reads the registry: not `is_green` / "red data means no
  trading", the kill switch, paper or M17 gating, `job_run`, or `/health`
  (`tests/unit/test_deviations_surfaces.py` fails if a gate module imports it).

**Adding or ending an acceptance** is a YAML edit with a `basis` that names where the decision is
written down (normally a HUMAN_DECISIONS entry). The loader refuses a matcher that names only a
target, and a malformed file stops the API at startup and fails `make check`. It reaches the
console after the app container is rebuilt, and `failure_alerts` after the scheduler restarts.

## The Desk page (`/console/desk`, M18.3)

| Panel | What it shows | Where it comes from |
|---|---|---|
| Kill-switch banner | red **KILL SWITCH ENGAGED** (or **STATE UNREADABLE** — never shown as armed) naming each book, its trip source, reason and time; green when every paper book's switch is armed | the `kill_switch` fields of `read_paper_status` — the same read as `GET /status/paper` |
| Broker-session banner | red **BROKER AUTH REQUIRED** when the newest journaled session carries an `AUTH_REQUIRED`, with the streak start, decisions deferred and the re-authenticate line; otherwise CLEAR / NO_LAPSE_RECORDED | `decision_journal` `AUTH_REQUIRED` entries (`queries_desk.read_broker_session`) |
| Paper books | per book: whether the next session may trade, switch, latest session and outcome, reason, recon (CLEAN/BREAK), and every unresolved escalation or break (`key@terms`) blocking it | `read_paper_status` (`/status/paper`) |
| Positions | each paper book's holdings and cash after its newest session that recorded an accounting book | `paper_session.expected_book` |
| M17 fund managers | the mandates (capital, caps, decision model), each book's latest mark, the latest session's decisions with refusals (RAIL_BLOCK, FM_DECISION_REFUSED) in red, suspended holdings, the scoreboard (k of n, per-book verdicts, both/one/neither, style books, graduation floor) | `read_managers_status` (`/status/managers`) and the roster |
| Orders | the newest 50 `order_` rows (staged, sent, executed, rail-blocked, rejected) with broker and sleeve tags, and each paper book's newest placed orders with their own tag | `order_`, `paper_session.orders` |
| Model cost | `token_usage` per manager (case), provider, model and IST day over the last 30 days, INR and USD as recorded, with totals | `token_usage` |

Display only: there is no control for the kill switch or the broker session anywhere on the
console. Trip or reset a switch with `uv run python -m execution.kill_switch` (`ops/runbooks/daily-eod.md`);
re-authenticate per `ops/runbooks/broker-reauth.md`. The broker-session state is what the journal
recorded — the interlock journals every dead-session day and nothing on a clear one — not a live
probe of the broker, which System 1 never imports.

Data and Journal are in the navigation and say "not built yet" until M18.2 and M18.4.

## The Portfolio page (`/console/portfolio?book=<id>`, M18.7)

One M17 book at a time, as a portfolio of its own (owner, 2026-10-13: every book is a portfolio).
The **book picker** at the top lists every book the roster records — each manager's 10 L primary
and 1 cr mirror, the controls, the style books and the bench — grouped by kind (Manager, Control,
Style, Bench), a stem's 10 L and 1 cr side by side on one line (`FM-POS-TREND  10L · 1CR`). One
click shows a book: each item is a plain link to `/console/portfolio?book=<id>` (works with
JavaScript off, bookmarkable, back-button friendly); with htmx it fetches the same URL with
`HX-Request: true`, gets only the portfolio region (picker included, so the highlight follows) and
pushes the URL. The selected book is highlighted (`aria-current="page"`). Each item shows the
book's latest NAV (to the rupee) and its NAV change % against the previous mark ("not marked"
before its first mark), and an amber ● when it has a BUY or SELL staged for the next session.
The default is the first manager book. An unknown book id is a 404.

- **Stream.** The live stream when it has any entry; until S0 it has none, and the page shows the
  dry stream under a yellow **DRY RUN** banner naming the stream (`m17-dry`) and the desk record
  the holdings came from (`m17_dry2_fund_managers` today). Nothing in the dry stream counts.
- **Holdings** as of the book's latest `BOOK_MARK` — only what the book holds, so every row has a
  quantity (owner, 2026-10-13): ISIN (the key) and symbol (display only),
  quantity, CMP (the NSE L1 close the book was marked at, with its session; a BE/BZ print when
  the name left EQ), average price (cost basis per share, buy charges included), day's change
  against the previous session's close — **per share** (₹), **for the position** (₹, per-share
  change × quantity held) and once as a % (the same for both; owner, 2026-10-13) — current value,
  profit on cost (₹ and %). A suspended holding shows "suspended since <date>", no day's change,
  and is valued at its last close. A book that holds nothing says "No holdings yet" (with "— first
  orders fill on <date>" when it has buys staged) instead of an empty table.
- **Next session**: `BUY n` or `SELL n` only where the book journaled a staging line (an entry
  with an `order_uid`: `STAGED`, `MIRROR_FOLLOW`, `STOP_EXIT`) on that session — those fill at the
  next open. On a held name it is the last column of its row: a trim or a sale is `SELL n`, an
  add `BUY n`.
- **Buying next session (<date>)**: staged BUYs of names the book does not hold yet, in their own
  compact table below the holdings — symbol, ISIN, quantity to buy, CMP (with its session), day's
  change % and an estimated cost (quantity × CMP, before charges; the fill is at the next
  session's price). Omitted when there are none.
- **Fund**: invested (sum of cost bases), current value of holdings, cash, NAV, total profit
  against the book's opening capital, and the NAV change against the previous mark (not the sum
  of the rows' position changes — fills, charges and cash moves are in the NAV change). NAV and cash
  are the mark's own figures — the ones `/status/managers` and the digest print. If the listed
  holdings plus cash do not equal that NAV the page says so in a yellow banner instead of hiding
  it. The bench holds its index as one position: the page says so and shows its fund totals.

Sources: the journal (`decision_journal`, through `analyst.journal.Journal`, the dry stream read
back via `StreamJournal.unprefixed`), `paper_session.book_state` (the desk record of the session),
`symbol_history`, and the L1 `prices_raw` partition of the session and the one before it.

## Read-only

Every console route answers GET and HEAD only; POST, PUT, PATCH and DELETE are 405. The unit
suite enumerates the router's routes to prove it, so a new route is covered without being listed.
