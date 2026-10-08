# trading-platform — `make check` is the gate for every task (AGENTIC_CONTEXT.md §5).
#
# Everything runs inside uv's pinned 3.12 venv; the host's python3 is never used.

COMPOSE := docker compose -f ops/docker-compose.yml

# Host path of the data lake, bind-mounted into the app at /data. Absolute, because compose
# resolves relative paths against ops/ while make resolves them against the repo root — an
# override like `DATA_ROOT=./elsewhere` would otherwise mean two different directories.
DATA_ROOT ?= $(CURDIR)/data
export DATA_ROOT

.DEFAULT_GOAL := check
.PHONY: check secret-scan hooks fmt test up down logs psql migrate backup restore

## check: secret scan + format check + lint + types + tests. Must pass before any task is DONE.
##
## The secret scan runs FIRST (M15.1, invariant #13): a credential must be caught on a tree that is
## red for every other reason too, not only on one clean enough to reach the last step. The repo is
## public, so a leak is undone only by rotation. tests/unit/test_secret_scan.py fails if it is removed.
check:
	uv run python ops/secret_scan.py
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy
	uv run pytest

## secret-scan: the working-tree scan alone (detect-secrets, offline, against .secrets.baseline).
secret-scan:
	uv run python ops/secret_scan.py

## hooks: point this clone's git hooks at ops/hooks — the staged-diff scan (pre-commit) and the
## commit-message scan (commit-msg). core.hooksPath is shared by every worktree of the clone.
hooks:
	git config core.hooksPath ops/hooks

## fmt: rewrite files to the canonical format and apply safe lint fixes.
fmt:
	uv run ruff format .
	uv run ruff check --fix .

## test: the full suite. `uv run pytest tests/unit` for the fast offline subset.
test:
	uv run pytest

## up / down / logs / psql: the two-service stack — postgres + app (ops/docker-compose.yml).
## The lake directories are created first: docker would otherwise create the bind-mount
## source as root, and the container runs as uid 1000.
up:
	mkdir -p "$(DATA_ROOT)/L0" "$(DATA_ROOT)/L1" "$(DATA_ROOT)/L2"
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100

## psql: an interactive shell on the container DB. Credentials come from the container's own
## environment, so this keeps working when the compose defaults are overridden.
psql:
	$(COMPOSE) exec postgres sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

## migrate: apply dataplatform/store/migrations/*.sql (runner lands in M0.4).
migrate:
	uv run python -m dataplatform.store.migrate

## backup / restore: pg_dump + L0 manifest, and the restore drill. `restore` verifies the newest
## backup by rebuilding it into a scratch database — it never writes to the live one. The real
## recovery procedure is deliberately not a make target: ops/runbooks/backup-restore.md.
backup:
	bash ops/backup.sh

restore:
	bash ops/restore.sh --scratch
