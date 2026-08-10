"""Process configuration (§8.1) — one `Settings` object, read from the environment or `.env`.

Every knob the platform has lives here, so "what is this process actually configured to do" is a
single object, and so no module invents its own `os.environ` lookup. Every credential-bearing
field is a `SecretStr` (AGENTIC_CONTEXT invariant #13), so an operator inspecting the object at a
REPL sees it masked — but that masking is a last line of defence, not a licence to log it: no code
in this platform may log, print to the status API, or journal a whole `Settings` object.
`.env.example` documents every key and is the file `tests/unit/test_config.py` loads, which keeps
the two from drifting apart.

Nothing in this module opens a socket or a database connection: constructing `Settings` is pure
parsing and validation, and it must stay that way so unit tests can build one offline.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Repo root, derived from this file's location so the defaults work from any working directory
#: (a `make` target, a container whose WORKDIR is /app, or pytest run from a subdirectory).
REPO_ROOT = Path(__file__).resolve().parent.parent

__all__ = [
    "REPO_ROOT",
    "AlertProvider",
    "AppEnv",
    "BrokerProvider",
    "LlmProvider",
    "LogFormat",
    "LogLevel",
    "Settings",
    "get_settings",
]


class AppEnv(StrEnum):
    """Which deployment this process is. Only affects logging and defaults, never decisions."""

    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class LogLevel(StrEnum):
    """Standard severity names, upper-cased on input."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LogFormat(StrEnum):
    """`AUTO` resolves to JSON in prod, console elsewhere (`Settings.effective_log_format`)."""

    AUTO = "auto"
    JSON = "json"
    CONSOLE = "console"


class LlmProvider(StrEnum):
    """Which LLM implementation the analyst wires. `STUB` needs no credential (B4)."""

    STUB = "stub"
    ANTHROPIC = "anthropic"


class BrokerProvider(StrEnum):
    """Which `Broker` implementation execution wires.

    `STUB` is the simulated broker — the paper path, and the default, because no Kite credential
    exists (B4). Selecting `KITE` selects a real-money-capable broker; invariant #5 means that is
    the *only* difference between paper and real, so this key is deliberately not a `paper` flag.
    """

    STUB = "stub"
    KITE = "kite"


class AlertProvider(StrEnum):
    """Which channel `dataplatform.alerts.build_alerter` wires.

    `LOG` writes the alert as a structured log event and needs no credential, which is why it is
    the default (B4) — an alerting channel that cannot be configured is one that never fires.
    """

    LOG = "log"
    EMAIL = "email"
    TELEGRAM = "telegram"


class Settings(BaseSettings):
    """The whole configuration surface, validated at construction.

    What it does: reads `.env` and the process environment (environment wins), coerces and
    validates every value, and fails loud on an unknown timezone or a non-Postgres DB URL.
    What it assumes: unknown keys in `.env` belong to something else (docker-compose shares the
    file), so they are ignored rather than rejected.
    What it never does: reach the network, connect to the database, or read a credential from
    anywhere but the environment.
    """

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        case_sensitive=False,
    )

    # ── runtime ──────────────────────────────────────────────────────────────────────────────
    app_env: AppEnv = Field(default=AppEnv.DEV, description="dev | test | prod")
    log_level: LogLevel = Field(default=LogLevel.INFO, description="DEBUG..CRITICAL")
    log_format: LogFormat = Field(
        default=LogFormat.AUTO,
        description="auto (json in prod, console otherwise) | json | console",
    )
    timezone: str = Field(
        default="Asia/Kolkata",
        description="IANA zone the exchange trades in; every trading date is a date here",
    )

    # ── storage ──────────────────────────────────────────────────────────────────────────────
    # Discrete connection parameters, not a pre-built DSN string, are the default path: a
    # Postgres password can legitimately contain a space, `@`, `/`, `%`, `#`, `?` or `:`, and a
    # DSN string is a URI — those characters need percent-encoding to survive one, which nothing
    # upstream of this process can be trusted to have done (ops/docker-compose.yml's own
    # `${POSTGRES_PASSWORD}` interpolation does not). `dataplatform.store.db.connect` hands these
    # to psycopg as keyword arguments, never assembling a URL, so no character is ever ambiguous
    # (invariant #13: a wrong DSN parse silently reaching a different database, or a malformed one
    # quoting the password fragment back in a `ProgrammingError`, are both a leak/misconnection of
    # exactly the kind that rule exists to prevent).
    #
    # Port 5433, not 5432: that is where ops/docker-compose.yml publishes the container DB on the
    # host, because this host already runs its own Postgres on 127.0.0.1:5432 and a default of
    # 5432 silently reaches that other server (ops/README.md). In-container the app is expected to
    # be given POSTGRES_HOST=postgres and POSTGRES_PORT=5432 explicitly.
    postgres_host: str = Field(default="localhost", description="Postgres server host")
    postgres_port: Annotated[int, Field(ge=1, le=65535)] = Field(
        default=5433, description="Postgres server port"
    )
    postgres_user: str = Field(default="trading", description="Postgres role this process uses")
    postgres_password: SecretStr = Field(
        default=SecretStr("trading"),
        description="Postgres role's password — a SecretStr regardless of the default (#13)",
    )
    postgres_db: str = Field(
        default="trading", description="Postgres database for masters, sync state and the journal"
    )
    # An explicit escape hatch, not the default: a single DSN string is occasionally the only
    # option (a managed Postgres that hands out one connection string), and existing tooling
    # (ops/README.md's demo, a developer's own shell) may still export DATABASE_URL directly. Set,
    # it overrides every POSTGRES_* field above outright — whoever sets it owns getting any
    # metacharacter in its password percent-encoded, per `urllib.parse.quote`.
    database_url: SecretStr | None = Field(
        default=None,
        description="explicit full DSN override; unset means build safely from POSTGRES_* above",
    )
    data_root: Path = Field(
        default=Path("data"),
        description="root of the L0/L1/L2 lake; a relative path is anchored at the repo root",
    )

    # ── crawl policy (§4.1: 2-3 s spacing, backoff, hard stop on a 403 spike) ─────────────────
    http_user_agent: str = Field(
        default=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        description="NSE archives reject non-browser agents; identifies every outbound fetch",
    )
    http_min_interval_seconds: Annotated[float, Field(ge=0)] = Field(
        default=3.0, description="minimum spacing between two requests to the same source"
    )
    http_timeout_seconds: Annotated[float, Field(gt=0)] = Field(
        default=30.0, description="per-request timeout"
    )
    http_max_attempts: Annotated[int, Field(ge=1)] = Field(
        default=5, description="attempts per fetch, including the first"
    )
    http_backoff_base_seconds: Annotated[float, Field(gt=0)] = Field(
        default=2.0, description="exponential backoff base between attempts"
    )
    http_forbidden_streak_limit: Annotated[int, Field(ge=1)] = Field(
        default=3, description="consecutive 403s from one source before ingestion hard-stops"
    )

    # ── status API (§4.4) ─────────────────────────────────────────────────────────────────────
    scheduler_heartbeat_stale_after_seconds: Annotated[int, Field(ge=1)] = Field(
        default=300,
        description="/health turns 503 once the newest scheduler heartbeat is older than this",
    )
    status_quality_flag_limit: Annotated[int, Field(ge=1)] = Field(
        default=200,
        description="most open quality flags GET /status/quality returns in one response",
    )

    # ── provider selectors (B4: no credential exists, so both default to a stub) ──────────────
    llm_provider: LlmProvider = Field(default=LlmProvider.STUB, description="stub | anthropic")
    broker_provider: BrokerProvider = Field(default=BrokerProvider.STUB, description="stub | kite")

    # ── alerting (§8.1: FAILED streaks, red quality, reconciliation breaks) ───────────────────
    alert_provider: AlertProvider = Field(
        default=AlertProvider.LOG, description="log | email | telegram; log needs no credential"
    )
    alert_dedup_window_minutes: Annotated[int, Field(ge=0)] = Field(
        default=360,
        description="repeat alerts on one dedup_key are suppressed for this long; 0 disables",
    )
    alert_smtp_host: str | None = Field(
        default=None, description="required only when ALERT_PROVIDER=email"
    )
    alert_smtp_port: Annotated[int, Field(ge=1, le=65535)] = Field(
        default=587, description="587 uses STARTTLS; 465 uses implicit TLS"
    )
    alert_smtp_username: str | None = Field(
        default=None, description="omit for a relay that does not authenticate"
    )
    alert_smtp_password: SecretStr | None = Field(
        default=None, description="omit for a relay that does not authenticate"
    )
    alert_email_from: str | None = Field(
        default=None, description="envelope sender; required only when ALERT_PROVIDER=email"
    )
    alert_email_to: str = Field(
        default="", description="comma-separated recipients; see alert_email_recipients"
    )
    alert_telegram_bot_token: SecretStr | None = Field(
        default=None, description="required only when ALERT_PROVIDER=telegram"
    )
    alert_telegram_chat_id: str | None = Field(
        default=None, description="required only when ALERT_PROVIDER=telegram"
    )

    # ── credentials (absent by default; never logged — SecretStr masks its repr) ──────────────
    anthropic_api_key: SecretStr | None = Field(
        default=None, description="required only when LLM_PROVIDER=anthropic"
    )
    kite_api_key: SecretStr | None = Field(
        default=None, description="required only when BROKER_PROVIDER=kite"
    )
    kite_api_secret: SecretStr | None = Field(
        default=None, description="required only when BROKER_PROVIDER=kite"
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator(
        "anthropic_api_key",
        "kite_api_key",
        "kite_api_secret",
        "alert_smtp_host",
        "alert_smtp_username",
        "alert_smtp_password",
        "alert_email_from",
        "alert_telegram_bot_token",
        "alert_telegram_chat_id",
        "database_url",
        mode="before",
    )
    @classmethod
    def _blank_is_absent(cls, value: object) -> object:
        """`KEY=` in a .env means "not configured", not a credential that is the empty string."""
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone {value!r}") from exc
        return value

    @field_validator("database_url")
    @classmethod
    def _postgres_dsn(cls, value: SecretStr | None) -> SecretStr | None:
        """Postgres is the only supported store (§8.1); a DSN for anything else is a typo."""
        if value is None:
            return None
        dsn = value.get_secret_value()
        if not dsn.startswith(("postgres://", "postgresql://", "postgresql+")):
            raise ValueError(f"database_url must be a Postgres DSN, got {dsn.split(':')[0]!r}")
        return value

    @field_validator("data_root")
    @classmethod
    def _anchor_at_repo_root(cls, value: Path) -> Path:
        return value if value.is_absolute() else REPO_ROOT / value

    @property
    def tzinfo(self) -> ZoneInfo:
        """`timezone` as a tzinfo, for wiring a `Clock`."""
        return ZoneInfo(self.timezone)

    @property
    def alert_email_recipients(self) -> tuple[str, ...]:
        """`alert_email_to` as addresses. Empty when nothing is configured, never `('',)`."""
        return tuple(part.strip() for part in self.alert_email_to.split(",") if part.strip())

    def effective_log_format(self) -> LogFormat:
        """Resolve `AUTO`: JSON in prod so the log is queryable, console elsewhere so it reads."""
        if self.log_format is not LogFormat.AUTO:
            return self.log_format
        return LogFormat.JSON if self.app_env is AppEnv.PROD else LogFormat.CONSOLE


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings, parsed once.

    What it does: builds `Settings` from the environment on first call and caches it, so every
    module sees the same configuration for the life of the process.
    What it assumes: configuration does not change under a running process.
    What it never does: hide a construction error — a bad value raises here, at startup, rather
    than at the first ingestion. Tests that need a different configuration construct `Settings`
    directly and inject it, instead of mutating this cache.
    """
    return Settings()
