"""Shared fixtures and helpers for the integration suite (needs docker postgres, `make up`).

Every module in this package needs the same two things: a `Settings` pointed at a different
database on the same server, and a way to tell "postgres is not running" apart from "postgres is
running and rejected these credentials." Both used to be copy-pasted per module; both are now
here so a fix lands once rather than seven times.
"""

from __future__ import annotations

from typing import NoReturn

import psycopg
import pytest
from pydantic import SecretStr

from dataplatform.config import Settings
from dataplatform.store.db import with_dbname

__all__ = ["settings_for", "skip_or_fail_on_connect_error"]


def settings_for(dbname: str) -> Settings:
    """Settings for the configured server with a different database selected.

    Mirrors the branch `dataplatform.store.db.connect` itself takes. An explicit `DATABASE_URL`
    override is rewritten with `with_dbname` — URL surgery, safe because it only ever touches the
    path component. The default path — discrete `POSTGRES_*` fields, no DSN string involved at
    all — just swaps `postgres_db`; there is no string to get wrong, which is the whole point of
    that path existing (invariant #13).
    """
    settings = Settings()
    if settings.database_url is not None:
        dsn = with_dbname(settings.database_url.get_secret_value(), dbname)
        return settings.model_copy(update={"database_url": SecretStr(dsn)})
    return settings.model_copy(update={"postgres_db": dbname})


#: psycopg3's `OperationalError.sqlstate` is `None` for a connection-phase failure — no `PGresult`
#: was ever received to carry one, verified against a real server for both an auth failure and a
#: dead port. The only signal actually available is the message text, and Postgres's own
#: convention there is unambiguous: `FATAL:` means a server received the startup packet and
#: rejected it outright (wrong password, wrong role, too many connections); its absence means
#: nothing ever answered (server down, wrong port, docker not up). Neither libpq nor Postgres ever
#: echoes a submitted credential back in this text, in any of those cases.
_SERVER_RESPONDED_AND_REFUSED = "FATAL:"


def skip_or_fail_on_connect_error(error: psycopg.OperationalError) -> NoReturn:
    """Skip a genuinely unreachable postgres; fail loud on one that rejected the credentials.

    A wrong `POSTGRES_PASSWORD` must never read as "no database there" — that is how a third of
    this suite can go silently unrun behind a green exit code, which is the exact class of gate
    failure this build exists to catch, just moved from the platform's own data to its tests.

    `pytrace=False` on the `fail`, not incidental: this function runs inside an `except ... as
    error:` block, so Python chains `error` onto the `Failed` exception's `__context__`
    automatically, and pytest's *default* long traceback style — no `--showlocals`, nothing
    special, `uv run pytest` exactly as `make check` invokes it — prints every chained frame's
    call arguments. `psycopg.connect`'s own internal frames bind the plaintext password to a
    local (`conninfo`, `kwargs`) one level below this one, verified with a throwaway reproduction:
    without `pytrace=False` the password appears in the failure output; with it, it does not.
    Invariant #13 does not carve out an exception for a test's own stdout.
    """
    if _SERVER_RESPONDED_AND_REFUSED in str(error):
        pytest.fail(
            "postgres rejected the configured credentials outright rather than being "
            f"unreachable — this is a misconfiguration to fix (DATABASE_URL / POSTGRES_PASSWORD), "
            f"not a missing database to skip past: {error}",
            pytrace=False,
        )
    pytest.skip(f"postgres is not reachable — run `make up` first: {error}")
