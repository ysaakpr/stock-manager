# trading-platform — repo conventions

EOD market-data platform (System 1) + analyst agent (System 2). Solo-maintained monolith.

**Read before working:** [EXECUTION_PLAN.md](EXECUTION_PLAN.md) (what to build — the constitution) and
[AGENTIC_CONTEXT.md](AGENTIC_CONTEXT.md) (how agents build it, autonomy limits, hard invariants).
Your task's spec is its entry in [TASK_GRAPH.yaml](TASK_GRAPH.yaml).

## Toolchain

Python 3.12 via `uv` (never the host's 3.9). Postgres via docker-compose.

```bash
uv sync                     # install/refresh env from uv.lock
uv run pytest               # tests
uv run pytest tests/unit    # fast subset
make check                  # format + lint + types + tests — the gate for every task
make up                     # docker compose up -d (postgres + app)
make migrate                # apply platform/store/migrations/*.sql
```

Never `pip install` into the host. Never run `python3` directly — always `uv run python`.

## Layout

```
dataplatform/ ingest/ identity/ corpactions/ store/ quality/ query/ archives/ status/  # D1-D7
analyst/     cases/ interview/ mapper/ thesis/ monitor/ rotation/ cash/ rails/ journal/ # A1-A9
execution/   broker.py sim_broker.py kite_broker.py costs/ recon.py kill_switch.py      # X1
backtest/    replay engine                                                             # X2
accounting/  token/cost metering                                                        # X3
orchestrator/ the autonomous build system itself (not product code)
ops/         compose, backups, deploy, runbooks
tests/       unit/ integration/ golden/ fixtures/
data/        L0/ L1/ L2  — gitignored, never committed
```

Module boundaries are packages. A module's public surface is what its `__init__.py` exports; reach into a
sibling's internals and you have created the coupling this layout exists to prevent.

**`dataplatform/` is the plan's `platform/`.** EXECUTION_PLAN §8.2 names System 1 `platform/`, which Python
cannot have as a top-level package: it shadows the stdlib `platform` module. Both directions break — once
anything has imported the stdlib module (pytest does, at startup) `platform.config` fails with *'platform' is
not a package*, and when the local package wins the path race instead, `import pandas` dies on
`platform.python_implementation()`. Renamed at M0.1 and guarded by `tests/unit/test_layout.py`. **A spec,
deliverable path or task entry that says `platform/x.py` means `dataplatform/x.py`** — that substitution is
the whole change; nothing else about §8.2 moved. Proposed as an amendment in EXECUTION_PLAN §12.

## Code conventions

- Type hints everywhere; `mypy --strict` on new packages. Dataclasses or pydantic models for anything crossing
  a module boundary — no bare dicts as interfaces.
- **Money:** `Decimal`, never `float`. Prices, quantities, costs, P&L. A float in the cost model is a bug.
- **Dates:** `datetime.date` for trading dates, tz-aware `datetime` for timestamps (Asia/Kolkata).
  Never `datetime.now()` — inject a `Clock` (AGENTIC_CONTEXT §6.11).
- **Identity:** ISIN is the only join key. A function taking a `symbol` and querying prices is a bug.
- **Errors:** fail loud and specific. Bare `except:` and silent `pass` on an ingestion failure are defects —
  a source that broke must reach the status API, not a log line nobody reads.
- **Logging:** `structlog`, key-value, one event per meaningful step. Log the source, date, and state on every
  ingestion transition.
- Docstrings on public functions: what it does, what it assumes, what it never does.
- Comment density matches surrounding code. Comment *why*, not *what*.

## Testing

`tests/unit` (offline, fast), `tests/integration` (needs docker postgres), `tests/golden` (the CA suite).

- Ingestion parsers: frozen fixture per format era in `tests/fixtures/<source>/<era>/`. **Tests never hit the
  network.**
- Anything touching adjustment factors, costs, rails, or PIT boundaries needs a test that fails if the logic
  is inverted. Property tests for rails (no generated order stream may breach a cap).
- Replay determinism: same inputs → byte-identical journal and book.

## Data invariants (full list in AGENTIC_CONTEXT.md §6)

L0 immutable · ISIN-only joins · no adjusted prices in L1 · one shared cost model · one decision path for
paper and real · rails unbypassable · no future data in a decision · restated fundamentals quarantined from
backtests · every decision journaled including no-ops · red data means no trading.

## Git

Every commit message is `[<task-id>] <title>`, with `Task:` and `Acceptance:` trailers. Commit each coherent
green piece as you reach it rather than saving one commit for the end of the task — the runner has died
mid-task seven times here and taken the uncommitted tree with it every time (AGENTIC_CONTEXT §7). Several
well-named commits per task is the intended shape; squashing is the merger's option, never a reason to hold
work in a dirty tree. Never commit `data/`, `.env`, or credentials. Never force-push or rewrite history.

**This repo is public** (`github.com/ysaakpr/stock-manager`). Every commit is world-readable once pushed,
and a secret cannot be un-published by deleting it in a later commit — it can only be rotated.

**Secrets:** environment and the untracked `.env` are the only places a credential may live — never source,
YAML, fixture, migration, runbook, or commit message. Anything that can authenticate is a secret, including
a Postgres DSN with an embedded password, and every such setting is a `SecretStr`. A secret in a log line,
the status API, or the journal is a bug — and the journal is append-only, so that one is permanent. Never
log a whole `Settings` object, never put a token in a URL or an argv. A credential in a commit is a defect
of the same order as a float in the cost model. Full rule: AGENTIC_CONTEXT §6 invariant #13.
