# trading-platform — `make check` is the gate for every task (AGENTIC_CONTEXT.md §5).
#
# Everything runs inside uv's pinned 3.12 venv; the host's python3 is never used.

COMPOSE := docker compose -f ops/docker-compose.yml

.DEFAULT_GOAL := check
.PHONY: check fmt test up down migrate backup restore

## check: format check + lint + types + tests. Must pass before any task is DONE.
check:
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy
	uv run pytest

## fmt: rewrite files to the canonical format and apply safe lint fixes.
fmt:
	uv run ruff format .
	uv run ruff check --fix .

## test: the full suite. `uv run pytest tests/unit` for the fast offline subset.
test:
	uv run pytest

## up / down: the two-service stack — postgres + app (ops/docker-compose.yml, M0.3).
up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

## migrate: apply dataplatform/store/migrations/*.sql (runner lands in M0.4).
migrate:
	uv run python -m dataplatform.store.migrate

## backup / restore: pg_dump + L0 manifest, and the restore drill (ops/*.sh, M1.12).
backup:
	bash ops/backup.sh

restore:
	bash ops/restore.sh
