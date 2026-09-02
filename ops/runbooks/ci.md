# Runbook — Continuous Integration

The CI workflow (`.github/workflows/ci.yml`) is the shared, offline gate for the platform. It runs
on every push to `main` and every pull request, and it runs exactly what a builder runs locally —
no more, no less — so a green check here means the same thing `make check` means on a laptop.

## What it runs, and why

One job, offline, in this order (fail-fast on the headline properties, then the full gate):

1. **Replay determinism** — `tests/integration/test_replay_determinism.py`
   Same inputs → byte-identical journal and book (EXECUTION_PLAN §8.3.3). This is the property the
   reference case and every backtest stand on; if it breaks, nothing downstream can be trusted.

2. **PIT-leak audit** — `tests/integration/test_pit_leak.py`
   The automated harness that instruments the query layer during a replay and audits every access
   against the session's `as_of` (§8.3.6, invariant #7). It proves two things: an honestly-declared
   future read is stopped by the query-layer guard (`PitError`), and a *mis-declared* future read —
   one the per-record guard is tricked into admitting — is still caught by the replay-wide audit.
   A deliberate leak is built into the test doubles precisely so the harness can be seen catching it.

3. **Golden corporate-action suite** — `tests/golden` (§4.3)
   The highest-value tests in the system: hand-computed adjustment expectations against reference
   data. Run with `-m "not network"` so the one live-Yahoo cross-check stays opt-in.

4. **`make check`** — the gate itself: `ruff format --check`, `ruff check`, `mypy`, then the whole
   test suite. This re-covers the three suites above; running them first is fail-fast, not
   duplication. This is the command CLAUDE.md and AGENTIC_CONTEXT §5 name as the gate for every task.

## Why it is offline (AGENTIC_CONTEXT B8)

CI provisions **no Postgres service and touches no network.**

- **Database-backed tests skip themselves.** Integration tests that need the docker database open a
  connection in a fixture and call `pytest.skip` when it is unreachable (see
  `test_replay_determinism.py::replay_settings`). Under a bare `uv run pytest` — which is what
  `make check` runs — those tests are reported skipped, not failed. To run them, bring the stack up
  locally with `make up` first; CI does not.
- **The live-network cross-check is opt-in.** Reference A (`tests/golden/test_yfinance_reference.py`)
  is marked `network` and skipped unless positively selected with `-m network`
  (`tests/golden/conftest.py`). Bare runs and `-m "not network"` both skip it, so CI never reaches
  Yahoo Finance. Refresh the cached fixtures deliberately, locally, with `-m network`.

This keeps CI fast, hermetic, and reproducible: `uv sync --frozen` builds the exact environment the
lockfile pins, and every test reads a checked-in fixture.

## Reproducing a CI failure locally

```bash
uv sync --frozen
uv run pytest tests/integration/test_replay_determinism.py -q   # determinism
uv run pytest tests/integration/test_pit_leak.py -q             # PIT-leak audit
uv run pytest tests/golden -q -m "not network"                  # golden suite
make check                                                      # the full gate
```

If `uv sync --frozen` fails, `uv.lock` is behind `pyproject.toml`: run `uv sync` (no `--frozen`),
commit the updated lock, and push.

## What CI does not cover

Database-backed integration tests (the `integration` marker paths that need a live Postgres) run
behind `make up` on a developer machine, not in this workflow. Adding a Postgres service to CI is a
future step tracked in `ops/BACKLOG.md`; today the fixtures-only path is the contract CI enforces.
