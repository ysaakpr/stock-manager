"""D4: the migration runner (B6) — plain numbered SQL files, applied once, in order.

Not Alembic. The schema is a dozen tables and mostly append-only, so the whole mechanism is: read
`migrations/NNNN_name.sql` in filename order, skip the ones `schema_migrations` says are already
applied, run the rest one transaction per file. Boring tech (decision #13), and an operator can
reproduce it by hand with `psql -f` if this file is ever unavailable.

Two properties it does have, because both are cheap and both are how migration runners actually
break in production:

* **Checksums.** Every applied file's SHA-256 is recorded, and a file whose content has changed
  since it was applied raises rather than being silently ignored. An edited migration means the
  database and the repo disagree about the schema — the one situation in which continuing is
  worse than stopping.
* **A session advisory lock.** Two processes starting at once (compose bringing up `app` while an
  operator runs `make migrate`) serialise instead of racing to create the same table.

Run it with `make migrate`, which is `uv run python -m dataplatform.store.migrate`.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.logging import configure_logging, get_logger
from dataplatform.store.db import Connection, connection

__all__ = [
    "MIGRATIONS_DIR",
    "Migration",
    "MigrationConnectionError",
    "MigrationError",
    "discover",
    "migrate",
]

#: Where the numbered SQL files live, beside this module.
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

#: `0001_init.sql`. The four-digit prefix is what orders them; the name is for humans.
_FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

#: Arbitrary constant identifying this runner's advisory lock. Session-scoped, so it is held
#: across the per-file transactions and released when the connection closes.
_LOCK_KEY = 8_240_401_004

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text        PRIMARY KEY,
    name       text        NOT NULL,
    checksum   text        NOT NULL,
    applied_at timestamptz NOT NULL
);
COMMENT ON TABLE schema_migrations IS
    'D4 · Ledger of applied migrations, written by dataplatform.store.migrate. One row per SQL '
    'file, with the SHA-256 of the content that was actually executed.';
"""

log = get_logger(__name__)


class MigrationError(RuntimeError):
    """The migrations on disk and the ones recorded in the database disagree."""


class MigrationConnectionError(RuntimeError):
    """The database could not be reached, or the driver rejected the connection string.

    Deliberately never wraps the original exception's message. Some drivers put the offending
    value verbatim into their own error text — psycopg does this for a malformed password
    (`unexpected spaces found in "..."`) — which would otherwise put a secret in this process's
    stdout/stderr on every restart (invariant #13). Only the exception's type name is safe to
    surface; the message here is written fresh, from what is already known to be non-secret.
    """


@dataclass(frozen=True, slots=True)
class Migration:
    """One `NNNN_name.sql` file: its version, its name, and the SQL it will run."""

    version: str
    name: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        """SHA-256 of the file's exact bytes, as recorded in `schema_migrations`."""
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover(directory: Path | None = None) -> list[Migration]:
    """Every migration in `directory`, ordered by version.

    What it assumes: the directory holds only `NNNN_name.sql` files. Anything else — a `.sql.bak`,
    an editor swap file, a differently-named script — raises, because a migration that silently
    does not run is indistinguishable from one that did until something downstream fails.
    """
    directory = MIGRATIONS_DIR if directory is None else directory
    migrations: list[Migration] = []
    for path in sorted(directory.iterdir()):
        if path.name.startswith("."):
            continue
        match = _FILENAME.match(path.name)
        if not match:
            raise MigrationError(
                f"{path} is not a migration: expected NNNN_lower_snake_name.sql "
                "(anything else in this directory would never be applied)"
            )
        migrations.append(
            Migration(
                version=match.group(1),
                name=match.group(2),
                path=path,
                sql=path.read_text(encoding="utf-8"),
            )
        )
    versions = [migration.version for migration in migrations]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"duplicate migration versions in {directory}: {versions}")
    return migrations


def migrate(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    directory: Path | None = None,
) -> list[Migration]:
    """Apply every migration the database has not seen yet; return the ones applied.

    What it does: bootstraps `schema_migrations`, verifies the checksum of everything already
    applied, then runs each pending file in its own transaction and records it in the same
    transaction — so a file either lands with its ledger row or not at all.
    What it assumes: the database exists and the connecting role may create objects in `public`.
    What it never does: re-run, reorder, or roll back an applied migration. Correcting a mistake
    means a new numbered file; this runner has no `down`, by design (B6).

    Returns an empty list when everything is already applied, which is what makes a second
    `make migrate` a no-op.
    """
    settings = get_settings() if settings is None else settings
    clock = SystemClock(settings.tzinfo) if clock is None else clock
    migrations = discover(directory)

    # Autocommit at the connection level, with an explicit transaction around each migration:
    # the advisory lock has to outlive those transactions (it guards the whole run, not one file).
    # The lock is taken *before* anything else touches the schema: `CREATE TABLE IF NOT EXISTS`
    # is not safe against a concurrent identical statement (two sessions can both decide the table
    # is missing and race to create it — observed for real with 6 concurrent runners, one dying on
    # `pg_type_typname_nsp_index`, the ledger unharmed). The entrypoint makes two runners starting
    # at once an ordinary occurrence, not a rare accident, so this has to hold under real
    # concurrency: a restart loop, or a second app container racing the first.
    with contextlib.ExitStack() as stack:
        # Opening the connection is the one call in this function that touches the DSN before
        # anything is known to have gone right — the seam where a rejected password or an
        # unreachable host surfaces, and therefore the seam whose exception must never be
        # printed as-is (see MigrationConnectionError). Everything past this point runs plain
        # SQL against an already-open connection, whose errors are safe to show in full.
        try:
            conn = stack.enter_context(connection(settings, autocommit=True))
        except Exception as error:
            raise MigrationConnectionError(
                f"could not open the configured database connection ({type(error).__name__}). "
                "Check that the host is reachable and that DATABASE_URL's user, password and "
                "database are correct."
            ) from None
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        conn.execute(_BOOTSTRAP)
        pending = _pending(conn, migrations)
        for migration in pending:
            _apply(conn, migration, applied_at=clock.now())
    log.info(
        "migrate.done",
        applied=[migration.path.name for migration in pending],
        total=len(migrations),
    )
    return pending


def _pending(conn: Connection, migrations: list[Migration]) -> list[Migration]:
    """Migrations not yet in `schema_migrations`, after checking the applied ones still match."""
    rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    applied = {str(version): str(checksum) for version, checksum in rows}
    for migration in migrations:
        recorded = applied.get(migration.version)
        if recorded is not None and recorded != migration.checksum:
            raise MigrationError(
                f"{migration.path.name} was applied with checksum {recorded[:12]}… but now "
                f"hashes to {migration.checksum[:12]}…: an applied migration was edited, so the "
                "database no longer matches this repo. Revert the file and add a new migration."
            )
    unknown = sorted(set(applied) - {migration.version for migration in migrations})
    if unknown:
        raise MigrationError(
            f"database has migrations this checkout does not: {unknown}. "
            "Running an older checkout against a newer database would corrupt it."
        )
    return [migration for migration in migrations if migration.version not in applied]


def _apply(conn: Connection, migration: Migration, *, applied_at: datetime) -> None:
    """Run one migration and record it, both in a single transaction."""
    log.info("migrate.applying", migration=migration.path.name, checksum=migration.checksum[:12])
    with conn.transaction():
        conn.execute(migration.sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
            "VALUES (%s, %s, %s, %s)",
            (migration.version, migration.name, migration.checksum, applied_at),
        )


def main() -> int:
    """`make migrate` — apply pending migrations, printing what happened."""
    configure_logging()
    try:
        applied = migrate()
    except MigrationConnectionError as error:
        log.error("migrate.failed", stage="connect", error=str(error))
        return 1
    except MigrationError as error:
        log.error("migrate.failed", stage="schema", error=str(error))
        return 1
    except Exception as error:
        # Last-resort net, not the expected path: MigrationConnectionError already covers the
        # one call site known to echo secrets. This exists so a future exception type nobody
        # anticipated still cannot put its own message — and whatever it embedded — on stdout.
        log.error("migrate.failed", stage="unexpected", error_type=type(error).__name__)
        return 1
    for migration in applied:
        print(f"applied {migration.path.name}")
    if not applied:
        print("no pending migrations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
