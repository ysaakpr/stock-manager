"""M0.4 acceptance: the schema applies to an empty database and holds its own invariants.

Everything here runs against a scratch database created for the session and dropped afterwards,
never against the developer's `trading` database: "applies cleanly to an empty DB" is only tested
if the DB really was empty, and the append-only tables cannot be cleaned up after a test that
wrote to them (that is the point of them). Writes inside a test are rolled back instead.

Needs the docker postgres (`make up`). If it is unreachable the module skips with a loud reason
rather than failing — but `make migrate`, which the task's verify command runs first, does not,
so a missing database still fails the gate rather than passing silently.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import psycopg
import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.store.db import Connection, connect, connection
from dataplatform.store.migrate import MigrationError, discover, migrate
from tests.integration.conftest import settings_for, skip_or_fail_on_connect_error

pytestmark = pytest.mark.integration

#: Created at the start of every session and dropped afterwards, so it is empty by construction.
#: Suffixed with the pid because two pytest sessions do run at once here — an autonomous build
#: wave has several agents in the suite simultaneously — and a fixed name means one session's
#: `DROP DATABASE ... WITH (FORCE)` deletes the database another is mid-test against (M0.6).
SCRATCH_DB = f"trading_m0_4_migrations_{os.getpid()}"

#: The tables M0.4 owes its dependants, from the task spec and EXECUTION_PLAN.md §4.2.
EXPECTED_TABLES = frozenset(
    {
        "security_master",
        "symbol_history",
        "exchange_listing",
        "corporate_actions",
        "adjustment_factors",
        "sync_state",
        "quality_flag",
        "case_",
        "policy_set",
        "thesis",
        "decision_journal",
        "order_",
        "token_usage",
        "schema_migrations",
    }
)

#: Tables the plan declares append-only (invariant #12) and the task requires to reject mutation.
APPEND_ONLY_TABLES = ("decision_journal", "policy_set")

#: Column types that can never hold money. `money` is PostgreSQL's own type and is excluded too:
#: its output depends on the server's lc_monetary, so the same row reads differently on two hosts.
NON_MONEY_TYPES = frozenset({"double precision", "real", "money"})

#: Columns that must exist and must be exact decimals — a spot-check with real names, so the
#: blanket "nothing is a float" assertion cannot pass by the columns having quietly disappeared.
MONEY_COLUMNS = (
    ("security_master", "face_value_inr"),
    ("exchange_listing", "face_value_inr"),
    ("corporate_actions", "dividend_amount_inr"),
    ("case_", "sip_amount_inr"),
    ("decision_journal", "cost_inr"),
    ("order_", "limit_price_inr"),
    ("order_", "avg_fill_price_inr"),
    ("order_", "gross_value_inr"),
    ("order_", "costs_inr"),
    ("order_", "net_value_inr"),
    ("token_usage", "cost_inr"),
)

#: Every table comment must open with the plan module that owns it (D1-D7, A1-A9, X1-X3).
PLAN_MODULE = re.compile(r"^(D[1-7]|A[1-9]|X[1-3]) ")

#: The instant the runner's injected clock is frozen at, so `applied_at` is asserted rather than
#: observed. Compared as an instant, not a local date: the server returns timestamptz in its own
#: session timezone, which is UTC here and IST in the app container.
APPLIED_AT = datetime(2026, 8, 8, 9, 15, tzinfo=IST)


@pytest.fixture(scope="session")
def scratch_settings() -> Iterator[Settings]:
    """Create an empty scratch database for the session; drop it again afterwards."""
    admin = settings_for("postgres")
    try:
        conn = connect(admin, autocommit=True)
    except psycopg.OperationalError as error:  # pragma: no cover - environment, not logic
        skip_or_fail_on_connect_error(error)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{SCRATCH_DB}"')
    finally:
        conn.close()

    yield settings_for(SCRATCH_DB)

    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture(scope="session")
def migrated(scratch_settings: Settings) -> tuple[list[str], list[str]]:
    """Migrate the scratch database twice; return what each run applied.

    One session-scoped run, because acceptance criterion 1 is about the *second* invocation being
    a no-op — re-running the pair per test would test something weaker.
    """
    clock = FrozenClock(APPLIED_AT)
    first = [migration.path.name for migration in migrate(scratch_settings, clock=clock)]
    second = [migration.path.name for migration in migrate(scratch_settings, clock=clock)]
    return first, second


@pytest.fixture
def conn(scratch_settings: Settings, migrated: tuple[list[str], list[str]]) -> Iterator[Connection]:
    """A connection to the migrated scratch database, rolled back at the end of the test.

    Rollback rather than cleanup: `decision_journal` and `policy_set` reject DELETE, so a test
    that inserted into them has no way to tidy up after itself. An aborted transaction is the
    only cleanup an append-only table permits.
    """
    with connection(scratch_settings) as live:
        try:
            yield live
        finally:
            live.rollback()


# ── acceptance 1: applies cleanly to an empty DB, idempotent on a second run ─────────────────


def test_migrations_are_discovered_in_order() -> None:
    versions = [migration.version for migration in discover()]
    assert versions == sorted(versions) and versions, versions
    assert versions[0] == "0001"


def test_applies_to_an_empty_database_then_is_a_no_op(
    migrated: tuple[list[str], list[str]],
) -> None:
    """Acceptance 1. The first run applies every file; the second applies nothing at all."""
    first, second = migrated
    assert first == [migration.path.name for migration in discover()]
    assert second == []


def test_every_expected_table_exists(conn: Connection) -> None:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    assert {str(name) for (name,) in rows} >= EXPECTED_TABLES


def test_schema_migrations_records_name_checksum_and_time(conn: Connection) -> None:
    """The ledger is what makes the second run a no-op — so its contents are part of the gate."""
    rows = conn.execute(
        "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()
    on_disk = discover()
    assert [str(row[0]) for row in rows] == [migration.version for migration in on_disk]
    assert [str(row[1]) for row in rows] == [migration.name for migration in on_disk]
    assert [str(row[2]) for row in rows] == [migration.checksum for migration in on_disk]
    # The runner takes its timestamp from the injected clock (B10), not the database's now().
    assert all(row[3] == APPLIED_AT for row in rows)


def test_an_edited_applied_migration_is_rejected(
    scratch_settings: Settings, migrated: tuple[list[str], list[str]], tmp_path: Path
) -> None:
    """Silently ignoring an edited migration is how a schema and its repo drift apart."""
    edited = tmp_path / "0001_init.sql"
    edited.write_text("SELECT 1;\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="applied with checksum"):
        migrate(scratch_settings, directory=tmp_path)


def test_a_file_that_is_not_a_migration_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "0001_init.sql.bak").write_text("SELECT 1;\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="not a migration"):
        discover(tmp_path)


# ── acceptance 2: decision_journal and policy_set reject UPDATE and DELETE ───────────────────


def _seed_case(conn: Connection) -> str:
    """Insert the case row the append-only tables reference. Rolled back with the test."""
    case_id = "CASE_M0_4"
    conn.execute(
        "INSERT INTO case_ (case_id, title, state, created_at, updated_at) "
        "VALUES (%s, 'M0.4 fixture', 'DRAFT', now(), now()) ON CONFLICT DO NOTHING",
        (case_id,),
    )
    return case_id


def _seed_decision_journal(conn: Connection) -> int:
    conn.execute(
        "INSERT INTO decision_journal (ts, trading_date, actor, decision, rationale, recorded_at) "
        "VALUES (now(), DATE '2026-08-07', 'T0', 'HEARTBEAT', 'nothing happened', now())"
    )
    row = conn.execute("SELECT max(id) FROM decision_journal").fetchone()
    assert row is not None
    return int(row[0])


def _seed_policy_set(conn: Connection, case_id: str) -> int:
    conn.execute(
        "INSERT INTO policy_set (case_id, version, policy, rotation_dial_pct, max_position_pct, "
        "max_sector_pct, min_holdings, drawdown_review_pct, ratified_by, ratified_at, "
        "ratification_kind, recorded_at) "
        "VALUES (%s, 1, '{}'::jsonb, 30, 15, 35, 8, 25, 'fixture', now(), 'FIXTURE', now())",
        (case_id,),
    )
    row = conn.execute("SELECT max(id) FROM policy_set").fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
def test_append_only_table_rejects_update_and_delete(conn: Connection, table: str) -> None:
    """Acceptance 2, on a row that really exists — so nothing passes by matching zero rows."""
    case_id = _seed_case(conn)
    row_id = (
        _seed_decision_journal(conn)
        if table == "decision_journal"
        else _seed_policy_set(conn, case_id)
    )

    for statement in (
        f"UPDATE {table} SET recorded_at = now() WHERE id = %s",
        f"DELETE FROM {table} WHERE id = %s",
    ):
        with (
            pytest.raises(psycopg.errors.FeatureNotSupported, match="append-only"),
            conn.transaction(),
        ):
            conn.execute(statement, (row_id,))

    survivor = conn.execute(f"SELECT count(*) FROM {table} WHERE id = %s", (row_id,)).fetchone()
    assert survivor is not None and survivor[0] == 1, "the row must still be there"


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
def test_append_only_table_rejects_a_statement_matching_no_rows(
    conn: Connection, table: str
) -> None:
    """A row-level trigger would let `DELETE ... WHERE false` look like it worked."""
    for statement in (
        f"UPDATE {table} SET recorded_at = now() WHERE false",
        f"DELETE FROM {table} WHERE false",
    ):
        with (
            pytest.raises(psycopg.errors.FeatureNotSupported, match="append-only"),
            conn.transaction(),
        ):
            conn.execute(statement)


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
def test_append_only_table_cannot_be_truncated(conn: Connection, table: str) -> None:
    """TRUNCATE is the one DELETE that does not go through the DELETE trigger.

    CASCADE deliberately: a plain TRUNCATE of `decision_journal` is already refused because
    `order_` references it, which would make this pass for the wrong reason. CASCADE clears that
    obstacle and still has to get past the guard.
    """
    with (
        pytest.raises(psycopg.errors.FeatureNotSupported, match="append-only"),
        conn.transaction(),
    ):
        conn.execute(f"TRUNCATE {table} CASCADE")


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
def test_append_only_table_still_accepts_inserts(conn: Connection, table: str) -> None:
    """The guard must block mutation, not writing — an unwritable journal is just as useless."""
    case_id = _seed_case(conn)
    row_id = (
        _seed_decision_journal(conn)
        if table == "decision_journal"
        else _seed_policy_set(conn, case_id)
    )
    assert row_id > 0


def test_only_the_append_only_tables_carry_the_guard(conn: Connection) -> None:
    """A guard on, say, sync_state would deadlock the ingestion state machine on its first retry."""
    rows = conn.execute(
        "SELECT DISTINCT tgrelid::regclass::text FROM pg_trigger "
        "WHERE NOT tgisinternal AND tgfoid = 'reject_mutation'::regproc"
    ).fetchall()
    assert {str(name) for (name,) in rows} == set(APPEND_ONLY_TABLES)


# ── acceptance 3: no money column is float or double precision ──────────────────────────────


def test_no_column_in_the_schema_is_a_binary_float(conn: Connection) -> None:
    """Acceptance 3. Nothing here is allowed to be inexact — money least of all.

    The blanket form is deliberate: a column added later by a task that never read CLAUDE.md
    fails here, whereas a curated allow-list would only catch the columns someone remembered.
    """
    rows = conn.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND data_type = ANY(%s)",
        (sorted(NON_MONEY_TYPES),),
    ).fetchall()
    assert rows == [], f"money and factors must be NUMERIC, never float: {rows}"


@pytest.mark.parametrize(("table", "column"), MONEY_COLUMNS)
def test_money_column_exists_and_is_numeric(conn: Connection, table: str, column: str) -> None:
    row = conn.execute(
        "SELECT data_type, domain_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
        (table, column),
    ).fetchone()
    assert row is not None, f"{table}.{column} is missing"
    assert row[0] == "numeric", f"{table}.{column} is {row[0]}"
    assert row[1] == "money_inr", f"{table}.{column} bypasses the money_inr domain"


def test_the_money_domain_is_an_exact_decimal(conn: Connection) -> None:
    """If `money_inr` itself were float-backed, every column above would pass while being wrong."""
    row = conn.execute(
        "SELECT data_type, numeric_precision, numeric_scale FROM information_schema.domains "
        "WHERE domain_schema = 'public' AND domain_name = 'money_inr'"
    ).fetchone()
    assert row is not None, "the money_inr domain is missing"
    assert (row[0], row[1], row[2]) == ("numeric", 20, 6)


# ── the spec's own requirement: every table names the plan module that owns it ───────────────


def test_every_table_is_commented_with_its_owning_plan_module(conn: Connection) -> None:
    rows = conn.execute(
        "SELECT c.relname, obj_description(c.oid, 'pg_class') FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r'"
    ).fetchall()
    assert rows, "no tables found — the migration did not run"
    uncommented = [
        str(name) for name, comment in rows if not (comment and PLAN_MODULE.match(str(comment)))
    ]
    assert uncommented == [], (
        f"tables must open their comment with D1-D7/A1-A9/X1-X3: {uncommented}"
    )


# ── invariant #2: ISIN is the join key, and the schema enforces its shape ────────────────────


def test_the_isin_domain_rejects_a_symbol(conn: Connection) -> None:
    """A symbol pasted into an ISIN column is the concrete form invariant #2 breaks in."""
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            "INSERT INTO security_master (isin, name, primary_exchange, status, "
            "first_seen_date, created_at, updated_at) "
            "VALUES ('RELIANCE', 'Reliance', 'NSE', 'ACTIVE', DATE '2026-08-07', now(), now())"
        )
