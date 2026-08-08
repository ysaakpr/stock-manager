# Backlog

Improvements agents noticed while building something else. One line each, with the task id that
spotted it. Nothing here is committed work — it is a list of candidates for the owner to promote
into `TASK_GRAPH.yaml` if worth doing.

Agents: add a line here rather than expanding your task's scope (AGENTIC_CONTEXT.md §1).

| Spotted by | Item |
|---|---|
| setup | Object-storage target for L0 + Postgres backups is undecided; `ops/backup.sh` is local-only until one exists. |
| setup | Golden CA suite starts at 7 named cases; §4.3 expects ~13 more ugly cases collected during backfill. |
| M0.1 | `orchestrator/` is excluded from the gate (6 files unformatted, 7 mypy-strict errors). Bringing the build machinery under `make check` is a small, separate task. |
| M0.1 | The `platform/` → `dataplatform/` rename is documented in CLAUDE.md but ~30 `platform/…` path strings in TASK_GRAPH.yaml specs/deliverables still read the old name; a mechanical sweep would remove the indirection. |
| M8.2 | Regulation forces a daily broker API logout (NSE/INVG/73992 §8.3.2.1.8) — the daily loop needs a journaled `AUTH_REQUIRED` no-trade state alongside `SKIPPED_DATA_RED`; no task owns it. |
| M8.2 | Broker API terms are undated and change silently (kite.trade/terms preamble); a quarterly dated capture into `ops/compliance/` would make a term change detectable — C-track candidate. |
| M0.3 | This host runs its own Postgres on `127.0.0.1:5432`, so compose publishes the container DB on 5433 (ops/README.md). `.env.example` / `config.py` still default `DATABASE_URL` to `localhost:5432`, which reaches the wrong server from the host — either retire the host Postgres or move the default to 5433. |
| M0.2 | Closes the M0.3 row above: `DATABASE_URL` now defaults to `localhost:5433` to match what compose publishes. Flip both back to 5432 when the host Postgres goes away. |
| M0.2 | `configure_logging` sets up structlog only; httpx/uvicorn/apscheduler records still go wherever stdlib logging sends them, so an operator sees two log formats. Routing stdlib through structlog belongs with whatever owns the app entrypoint (M0.5). |
| C.1 | M1.2's acceptance covers spacing, backoff and the 403 hard stop but not payload validation. Three of nine sources answer unknown paths with HTTP 200 + HTML (`ops/gates/source-verification.md` §5.1), so a status-code-only fetcher will checksum markup into L0 as data — worth an explicit acceptance criterion on M1.2. |
| C.1 | `ListofScripData` defaults to `status=Active`, so a BSE identity backfill that uses the default silently imports survivorship bias. M3.1's spec does not mention pulling delisted/suspended scrips. |
| C.1 | EXECUTION_PLAN §4.1's Status column still reads `VERIFY-AT-BUILD` for rows the C.1 sweep verified. The register is now authoritative; the plan table was deliberately left untouched. A one-off sync would remove the discrepancy. |
| C.1 | Nothing re-verifies the register on a schedule. Endpoints drift (the niftyindices TRI gate is a live example) and today the first signal would be a daily-pipeline failure. A periodic sweep through M1.2's fetcher, reusing this register's evidence fields, would catch it earlier. |
