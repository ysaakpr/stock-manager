"""`Settings` parses, validates and fails loud — offline (M0.2 acceptance criterion 1).

Constructing configuration must never be the thing that touches a socket: unit tests build one
with no docker, no database and no network (B8), and a `Settings()` that quietly resolved a DSN or
pinged a host would break that for every test in the suite. The socket ban below is asserted, not
assumed.

Overrides are applied as environment variables rather than constructor arguments, because the
environment is the path production actually uses — including the dotenv parsing of awkward values
like a user-agent full of spaces and parentheses.
"""

from __future__ import annotations

import re
import socket
import typing
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from dataplatform.config import (
    REPO_ROOT,
    AppEnv,
    BrokerProvider,
    LlmProvider,
    LogFormat,
    LogLevel,
    Settings,
    SettingsValidationError,
    get_settings,
)
from tests.conftest import SettingsLoader

EXAMPLE_ENV = REPO_ROOT / ".env.example"

#: A documented key in `.env.example`, live (`KEY=value`) or commented out (`#KEY=`).
KEY_LINE = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=")


@pytest.fixture
def no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any network use during a test an immediate, obvious failure."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("configuration must not touch the network")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@pytest.mark.usefixtures("no_sockets")
def test_settings_load_from_the_example_env_offline(load_settings: SettingsLoader) -> None:
    settings = load_settings(EXAMPLE_ENV)

    assert settings.app_env is AppEnv.DEV
    assert settings.log_level is LogLevel.INFO
    assert settings.log_format is LogFormat.AUTO
    assert settings.timezone == "Asia/Kolkata"
    assert settings.tzinfo.key == "Asia/Kolkata"
    assert settings.database_url is None  # discrete POSTGRES_* below is the documented default
    assert settings.postgres_host == "localhost"
    assert settings.postgres_port == 5433
    assert settings.postgres_user == "trading"
    assert settings.postgres_password.get_secret_value() == "trading"
    assert settings.postgres_db == "trading"
    assert settings.data_root == REPO_ROOT / "data"
    assert settings.http_min_interval_seconds >= 2.0  # §4.1 crawl policy
    assert settings.http_user_agent.startswith("Mozilla/5.0")  # NSE rejects non-browser agents
    assert settings.llm_provider is LlmProvider.STUB
    assert settings.broker_provider is BrokerProvider.STUB
    assert settings.anthropic_api_key is None
    assert settings.kite_api_key is None
    assert settings.kite_api_secret is None


def test_example_env_documents_every_setting_and_nothing_else() -> None:
    """The template is the documentation; a setting added to only one of the two is a defect."""
    documented = {
        match.group(1)
        for line in EXAMPLE_ENV.read_text(encoding="utf-8").splitlines()
        if (match := KEY_LINE.match(line.strip()))
    }
    expected = {field.upper() for field in Settings.model_fields}

    assert documented - expected == set(), "documented in .env.example but not a Settings field"
    assert expected - documented == set(), "a Settings field nobody documented in .env.example"


@pytest.mark.usefixtures("no_sockets")
def test_providers_default_to_the_stub_that_needs_no_credential(
    load_settings: SettingsLoader,
) -> None:
    """B4: no Anthropic key and no Kite key exist, so nothing may default to needing one."""
    settings = load_settings(None)

    assert settings.llm_provider is LlmProvider.STUB
    assert settings.broker_provider is BrokerProvider.STUB


@pytest.mark.parametrize(
    ("app_env", "configured", "expected"),
    [
        ("prod", "auto", LogFormat.JSON),
        ("dev", "auto", LogFormat.CONSOLE),
        ("test", "auto", LogFormat.CONSOLE),
        ("prod", "console", LogFormat.CONSOLE),
        ("dev", "json", LogFormat.JSON),
    ],
)
def test_auto_log_format_resolves_by_deployment(
    app_env: str,
    configured: str,
    expected: LogFormat,
    load_settings: SettingsLoader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("LOG_FORMAT", configured)

    assert load_settings(None).effective_log_format() is expected


def test_log_level_is_case_insensitive(
    load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")

    assert load_settings(None).log_level is LogLevel.DEBUG


def test_relative_data_root_is_anchored_at_the_repo_root(
    load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`DATA_ROOT=data` must mean the same directory from a make target, a container and pytest."""
    monkeypatch.setenv("DATA_ROOT", "lake")
    assert load_settings(None).data_root == REPO_ROOT / "lake"

    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    assert load_settings(None).data_root == tmp_path


def test_a_blank_credential_is_absent_rather_than_an_empty_secret(
    load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`KITE_API_KEY=` in a .env must not read as a credential that happens to be empty."""
    monkeypatch.setenv("KITE_API_KEY", "   ")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    settings = load_settings(None)

    assert settings.kite_api_key is None
    assert settings.anthropic_api_key is None


#: Every field on `Settings` whose type is `SecretStr` or `SecretStr | None` — computed from the
#: model rather than hand-listed, so a credential added later (M6.8's Anthropic key already here,
#: M8.3's Kite secret already here) is covered by construction instead of by remembering to widen
#: this test (invariant #13).
_SECRET_STR_FIELDS = [
    name
    for name, field in Settings.model_fields.items()
    if field.annotation is SecretStr or SecretStr in typing.get_args(field.annotation)
]


def test_every_settings_field_that_can_authenticate_is_a_secret_str() -> None:
    """A bare `str` credential (a DSN included) is invariant #13's one-line regression."""
    assert "database_url" in _SECRET_STR_FIELDS
    assert "postgres_password" in _SECRET_STR_FIELDS
    assert "anthropic_api_key" in _SECRET_STR_FIELDS
    assert "kite_api_key" in _SECRET_STR_FIELDS
    assert "kite_api_secret" in _SECRET_STR_FIELDS
    assert "alert_smtp_password" in _SECRET_STR_FIELDS
    assert "alert_telegram_bot_token" in _SECRET_STR_FIELDS


@pytest.mark.parametrize("field", _SECRET_STR_FIELDS)
def test_a_configured_credential_is_masked_in_the_repr(
    field: str, load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settings get printed into logs and tracebacks; no secret-bearing field may leak there."""
    marker = f"s3cret-{field}-9f2c"
    value = (
        f"postgresql://trading:{marker}@localhost:5433/trading"
        if field == "database_url"
        else marker
    )
    monkeypatch.setenv(field.upper(), value)
    settings = load_settings(None)

    secret = getattr(settings, field)
    assert secret is not None
    assert marker in secret.get_secret_value()
    assert marker not in repr(settings)
    assert marker not in str(settings)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("TIMEZONE", "Asia/Kolkatta"),  # a typo must not silently become UTC
        ("DATABASE_URL", "mysql://localhost/trading"),
        ("LOG_LEVEL", "CHATTY"),
        ("APP_ENV", "staging"),
        ("BROKER_PROVIDER", "zerodha"),
        ("LLM_PROVIDER", "openai"),
        ("HTTP_MAX_ATTEMPTS", "0"),
        ("HTTP_MIN_INTERVAL_SECONDS", "-1"),
        ("HTTP_TIMEOUT_SECONDS", "0"),
    ],
)
def test_a_bad_value_fails_loud_at_construction(
    key: str, value: str, load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError):
        load_settings(None)


@pytest.mark.parametrize(
    "bad_dsn",
    [
        # A colon-free input — a bare password, or the libpq keyword form
        # (`host=... password=... dbname=...`), which `psycopg.conninfo.conninfo_to_dict`
        # parses happily and which store/db.py's own docstring points operators toward for a
        # managed Postgres that hands out one connection string.
        "host=localhost user=trading password=Sup3rS3cret! dbname=trading",
        "MyPlainPasswordOnly",
        "mysql://u:s3cret-mysql-pw@h/d",
    ],
)
def test_an_invalid_database_url_raises_a_validation_error_naming_no_scheme(
    bad_dsn: str, load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`value.split(":")[0]` used to return the *entire* input when it held no colon, putting a
    plaintext password straight into `_postgres_dsn`'s own hand-written message — fixed by not
    building one from the value at all.

    This test only proves the *type* of failure and the *validator's own message*; whether the
    value can still appear is a question about `pydantic.ValidationError` itself, not about this
    validator, and belongs to `test_get_settings_never_echoes_a_rejected_database_url` below —
    `pydantic.ValidationError.errors()` attaches the pre-coercion input to any field a validator
    rejects, and `str(ValidationError)` renders it, regardless of the validator's own message.
    Asserting non-leakage against the raw `ValidationError` here would be asserting something
    false: it *does* leak at this level, unavoidably, which is exactly why `get_settings()` exists
    to catch and re-wrap it before anything in this process ever sees the raw object.
    """
    monkeypatch.setenv("DATABASE_URL", bad_dsn)

    with pytest.raises(ValidationError, match="must be a Postgres DSN"):
        load_settings(None)


@pytest.mark.parametrize(
    "bad_dsn",
    [
        "host=localhost user=trading password=Sup3rS3cret! dbname=trading",
        "MyPlainPasswordOnly",
        "mysql://u:s3cret-mysql-pw@h/d",
    ],
)
def test_get_settings_never_echoes_a_rejected_database_url(
    bad_dsn: str, clean_settings_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real production path: nothing outside config.py and tests constructs `Settings()`
    directly (`status/api.py`, every other caller, goes through `get_settings()`). Proves the
    deeper leak `_postgres_dsn`'s own message fix does not touch: `pydantic.ValidationError`
    attaches the raw, pre-coercion `DATABASE_URL` string to its `errors()` output — and renders it
    in `str(ValidationError)` — for *any* field a custom validator rejects, independent of what
    that validator's message says. `SecretStr`'s masking only applies once a `SecretStr` instance
    exists; the string `BaseSettings` read from the environment is not one yet when this fires.
    """
    monkeypatch.setenv("DATABASE_URL", bad_dsn)
    get_settings.cache_clear()

    try:
        with pytest.raises(SettingsValidationError) as caught:
            get_settings()
        assert bad_dsn not in str(caught.value)
        assert "database_url" in str(caught.value)
    finally:
        get_settings.cache_clear()


def test_a_sqlalchemy_style_scheme_is_rejected_at_validation_not_at_connect(
    load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`postgresql+psycopg://` is SQLAlchemy's driver-suffix convention — this platform has no
    SQLAlchemy anywhere, and psycopg.conninfo.conninfo_to_dict rejects the `+driver` suffix
    unconditionally. Accepting it here would only move that same rejection past this message and
    into connect()'s MalformedDatabaseUrlError, which would then blame "a metacharacter in the
    password" for a DSN that never had one — the wrong diagnosis for the actual problem.
    """
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://trading:trading@localhost:5433/trading"
    )

    with pytest.raises(ValidationError, match="must be a Postgres DSN"):
        load_settings(None)
