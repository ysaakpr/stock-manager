"""D4: Postgres connections — the one place that knows how to reach the database.

Every module that talks to Postgres opens its connection here, so the connection parameters are
read from `Settings` exactly once in the codebase and a future change (a pool, a read replica, a
statement timeout) is one file's problem rather than a grep.

Deliberately thin: no ORM, no schema knowledge, no query helpers. Postgres holds a dozen tables
and plain SQL is the boring choice (decision #13); an abstraction over it would earn nothing and
would hide the `EXPLAIN` an operator needs to read.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg

from dataplatform.config import Settings, get_settings

__all__ = ["Connection", "connect", "connection", "with_dbname"]

#: psycopg's default row factory yields plain tuples. Naming the alias keeps every signature in
#: the platform identical and spells out the generic parameter `mypy --strict` insists on.
Connection = psycopg.Connection[tuple[Any, ...]]


def connect(settings: Settings | None = None, *, autocommit: bool = False) -> Connection:
    """Open one connection to the configured Postgres.

    What it does: connects to the configured server and returns the live connection, which the
    caller owns and must close. `settings.database_url`, if set, is an explicit override and is
    handed to psycopg as-is; otherwise this connects with `settings.postgres_*` as discrete
    keyword arguments — never a DSN string it would have to assemble itself. A Postgres password
    can contain a space, `@`, `/`, `%`, `#`, `?` or `:`, every one of which is grammar in a URI;
    passed as a keyword argument instead, psycopg hands it to libpq verbatim, so no character
    needs escaping and none can be misparsed into a different password, database or host
    (invariant #13 — a DSN built by string concatenation is exactly the leak/misconnection risk
    that rule exists to prevent).
    What it assumes: the database exists and the schema has been migrated. It does not migrate,
    probe or retry — a caller that wants a health check should run one explicitly, and a caller
    that wants a closed connection afterwards should use `connection()`.
    What it never does: swallow a connection error, or let one carry a credential. An unreachable
    database is an operational fact that has to reach the status API (§4.4), not a `None` that
    surfaces three frames later — and psycopg's own error messages never echo a password back.
    """
    settings = get_settings() if settings is None else settings
    if settings.database_url is not None:
        return psycopg.connect(settings.database_url.get_secret_value(), autocommit=autocommit)
    return psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        dbname=settings.postgres_db,
        autocommit=autocommit,
    )


@contextmanager
def connection(
    settings: Settings | None = None, *, autocommit: bool = False
) -> Iterator[Connection]:
    """`connect()` as a context manager, closed on the way out — exception or not.

    Note the difference from psycopg's own `with psycopg.connect(...)`, which commits the open
    transaction on a clean exit: this only closes. Committing is the caller's decision, because a
    block that ends without an explicit commit has, as often as not, ended early.
    """
    conn = connect(settings, autocommit=autocommit)
    try:
        yield conn
    finally:
        conn.close()


def with_dbname(database_url: str, dbname: str) -> str:
    """The same DSN pointing at a different database on the same server.

    Used for the two jobs that cannot be done from inside the target database: connecting to
    `postgres` to CREATE or DROP one, and pointing the integration suite at a scratch database so
    that "applies cleanly to an empty DB" is tested against a genuinely empty one.

    Replaces only the path component, so credentials, port and query parameters (`sslmode`,
    `application_name`) survive — which string surgery on the URL reliably does not. `Settings`
    guarantees the URL form, so there is no keyword-value DSN to handle here.
    """
    parts = urlsplit(database_url)
    return urlunsplit(parts._replace(path="/" + quote(dbname)))
