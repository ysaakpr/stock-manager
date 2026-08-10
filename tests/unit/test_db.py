"""D4: `dataplatform.store.db` picks the right connection path and never mangles a credential.

Offline by construction (B8): every test here replaces `psycopg.connect` with a recorder rather
than opening a socket, so a metacharacter-in-a-password case is provable without docker.
`tests/integration/test_*` prove the same thing against a real server; these prove it does not
regress even when nobody has `make up` running.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from dataplatform.config import Settings
from dataplatform.store.db import connect, with_dbname


class ConnectSpy:
    """Stands in for `psycopg.connect`: records how it was called and returns a sentinel."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append((args, kwargs))
        return "connection"

    @property
    def kwargs(self) -> dict[str, Any]:
        ((_, kwargs),) = self.calls
        return kwargs


@pytest.fixture
def connect_spy(monkeypatch: pytest.MonkeyPatch) -> ConnectSpy:
    spy = ConnectSpy()
    monkeypatch.setattr("dataplatform.store.db.psycopg.connect", spy)
    return spy


#: Every character invariant #13's audit named as something a Postgres password can legitimately
#: contain and a DSN URI would need to treat as grammar.
METACHARACTER_PASSWORDS = [
    "pw with space",
    "pw@with@at",
    "pw/with/slash",
    "pw%with%percent",
    "pw$with$dollar",
    "pw#with#hash",
    "pw?with?question",
    "pw:with:colon",
    "p@ss w/ord%25$#?:",  # every one of the above, together
]


@pytest.mark.parametrize("password", METACHARACTER_PASSWORDS)
def test_connect_hands_a_metacharacter_password_to_psycopg_unmodified(
    password: str, connect_spy: ConnectSpy
) -> None:
    """The whole point of the discrete-parameters path: nothing in between ever formats a URI.

    If anything on this path percent-encoded, percent-decoded, or otherwise reformatted the
    password, this would observe a different string than the one that went in — and psycopg
    would receive a password that fails to authenticate against the real server.
    """
    settings = Settings(postgres_password=SecretStr(password))

    connect(settings)

    assert connect_spy.kwargs["password"] == password


def test_connect_uses_discrete_keyword_arguments_not_a_dsn_string(
    connect_spy: ConnectSpy,
) -> None:
    """No URI is ever assembled for the default path — there is no string to get wrong."""
    settings = Settings(
        postgres_host="db.internal",
        postgres_port=6543,
        postgres_user="analyst",
        postgres_password=SecretStr("s3cret"),
        postgres_db="trading",
    )

    connect(settings)

    assert connect_spy.calls[0][0] == ()  # no positional DSN string
    assert connect_spy.kwargs == {
        "host": "db.internal",
        "port": 6543,
        "user": "analyst",
        "password": "s3cret",
        "dbname": "trading",
        "autocommit": False,
    }


def test_an_explicit_database_url_override_takes_the_dsn_string_path(
    connect_spy: ConnectSpy,
) -> None:
    """Whoever sets `DATABASE_URL` explicitly owns escaping it; this just proves it is honoured."""
    settings = Settings(
        database_url=SecretStr("postgresql://trading:trading@override-host:5432/trading")
    )

    connect(settings)

    assert connect_spy.calls[0][0] == ("postgresql://trading:trading@override-host:5432/trading",)
    assert connect_spy.kwargs == {"autocommit": False}


def test_with_dbname_replaces_only_the_path_component() -> None:
    """Credentials, host, port and query parameters must survive untouched."""
    dsn = "postgresql://trading:s3cret@localhost:5433/trading?sslmode=disable"

    assert with_dbname(dsn, "postgres") == (
        "postgresql://trading:s3cret@localhost:5433/postgres?sslmode=disable"
    )
