"""Process configuration (§8.1) — one `Settings` object, read from the environment or `.env`.

Every knob the platform has lives here, so "what is this process actually configured to do" is a
single object an operator can print, and so no module invents its own `os.environ` lookup.
`.env.example` documents every key and is the file `tests/unit/test_config.py` loads, which keeps
the two from drifting apart.

Nothing in this module opens a socket or a database connection: constructing `Settings` is pure
parsing and validation, and it must stay that way so unit tests can build one offline.
"""

from __future__ import annotations

import re
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

#: An AWS region name (`us-east-1`, `ap-south-1`, `us-gov-west-1`): it is interpolated into the
#: Bedrock endpoint's host name, so anything else is refused at startup rather than at a DNS error.
_AWS_REGION = re.compile(r"[a-z]{2}(-[a-z]+)+-[0-9]+")

__all__ = [
    "REPO_ROOT",
    "AlertProvider",
    "AppEnv",
    "BrokerProvider",
    "LlmEffort",
    "LlmProvider",
    "LogFormat",
    "LogLevel",
    "Settings",
    "WebResearchPath",
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


class WebResearchPath(StrEnum):
    """How a Bedrock desk's Commons fetcher retrieves the web (pre-registration Amendment 5 (d)).

    `WEBSEARCH` is our own layer (`analyst.websearch`): a Bedrock tool loop over Brave, Tavily and
    Firecrawl, with the Claude CLI's web tools as the per-request backup when Bedrock is
    unavailable. `CLAUDE_CLI` keeps the Claude CLI's own web search and fetch for every request —
    the rollback that needs no code change. A desk on the Claude CLI always researches there.
    """

    WEBSEARCH = "websearch"
    CLAUDE_CLI = "claude_cli"


class LlmProvider(StrEnum):
    """Which LLM implementation the analyst wires. `STUB` needs no credential (B4).

    `CLAUDE_CLI` needs no credential *in this repo* either: it shells out to the Claude CLI, which
    holds its own login. That is the difference from `ANTHROPIC`, which wants `ANTHROPIC_API_KEY`
    and bills per token — the CLI route runs on a subscription and reports an estimated cost.
    `BEDROCK` is Claude on Amazon Bedrock (the Mantle endpoint), billed per token to the AWS
    account behind `AWS_BEARER_TOKEN_BEDROCK` in `BEDROCK_REGION`.
    """

    STUB = "stub"
    ANTHROPIC = "anthropic"
    CLAUDE_CLI = "claude_cli"
    BEDROCK = "bedrock"


class LlmEffort(StrEnum):
    """How hard a model thinks (`output_config.effort` on the API, `--effort` on the Claude CLI).

    Pinned per call type in `analyst/llm/effort.yaml` rather than left to a default, because the
    default is not one thing: the API's depends on the model, and the CLI's is whatever the
    logged-in user last chose with `/effort` — which is how the M17 desk ran at `xhigh` before
    anyone decided it.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


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
    database_url: str = Field(
        # Port 5433, not 5432: that is where ops/docker-compose.yml publishes the container DB on
        # the host, because this host already runs its own Postgres on 127.0.0.1:5432 and a
        # default of 5432 silently reaches that other server (ops/README.md). In-container the
        # app is handed postgres:5432 by compose and never sees this default.
        default="postgresql://trading:trading@localhost:5433/trading",
        description="Postgres DSN for masters, sync state and the journal",
    )
    data_root: Path = Field(
        default=Path("data"),
        description="root of the L0/L1/L2 lake; a relative path is anchored at the repo root",
    )
    snapshot_expect_lake_root: Path | None = Field(
        default=None,
        description=(
            "absolute L0 root the daily snapshotter asserts before its first request; unset "
            "disables the check"
        ),
    )

    # ── backups (M15.4) ───────────────────────────────────────────────────────────────────────
    backup_root: Path = Field(
        default=Path("~/backups"),
        description=(
            "where the nightly dumps (postgres/) and the L0 manifest (l0/) go; outside the repo, "
            "absolute or ~-prefixed"
        ),
    )
    backup_keep_daily: Annotated[int, Field(ge=1)] = Field(
        default=14, description="keep the newest dump of each of this many most recent days"
    )
    backup_keep_weekly: Annotated[int, Field(ge=0)] = Field(
        default=8, description="and the newest dump of each of this many most recent ISO weeks"
    )
    backup_pg_client_image: str = Field(
        default="postgres:16",
        description=(
            "image whose pg_dump/pg_restore run against the live server (the host has no libpq "
            "client); empty uses the tools on PATH"
        ),
    )
    backup_l0_mirror: str | None = Field(
        default=None,
        description=(
            "rsync destination L0 is copied to nightly (a path on a second disk, or host:path); "
            "unset means L0 has a manifest but no second copy"
        ),
    )
    restore_drill_database_url: SecretStr | None = Field(
        default=None,
        description=(
            "DSN of an empty throwaway database the restore drill restores into; unset starts a "
            "scratch container instead. Refused if it is the live database"
        ),
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

    # ── the daily paper session (M13.1) ────────────────────────────────────────────────────────
    paper_session_enabled: bool = Field(
        default=False,
        description=(
            "run the scheduler's paper_session job; off until the regime filter has a same-evening "
            "source for the session's published NIFTY 50 TRI (ops/runbooks/daily-eod.md)"
        ),
    )

    # ── provider selectors (B4: no credential exists, so both default to a stub) ──────────────
    llm_provider: LlmProvider = Field(
        default=LlmProvider.STUB, description="stub | anthropic | claude_cli | bedrock"
    )
    #: The backup `build_llm` asks per call when `llm_provider` could not serve it — the same rule
    #: as `m17_llm_fallback` (`analyst.llm.fallback`). Ignored while the provider is the stub.
    llm_fallback: LlmProvider | None = Field(
        default=LlmProvider.CLAUDE_CLI,
        description="the analyst's backup provider, per call: none | anthropic | claude_cli",
    )
    broker_provider: BrokerProvider = Field(default=BrokerProvider.STUB, description="stub | kite")
    #: The M17 desk's own provider (M17.12): its managers, its on-demand digests and its web
    #: fetcher follow this and never `llm_provider`, so switching the desk to the Claude CLI does
    #: not also switch the older analyst paper paths, and the global default stays the stub (B4).
    #: A persisted M17 run that resolves to `stub` is refused (`backtest.fm_world`); only a
    #: `--memory` rehearsal may decide with the stub.
    m17_llm_provider: LlmProvider = Field(
        default=LlmProvider.CLAUDE_CLI,
        description=(
            "the M17 desk's managers and digests: stub | anthropic | claude_cli | bedrock; on "
            "bedrock the web fetcher follows M17_WEB_RESEARCH, otherwise it is the Claude CLI's"
        ),
    )
    #: The provider a M17 call is re-asked on when `m17_llm_provider` could not serve it — not
    #: reachable, rate limited after retries, a 5xx, a rejected credential, or not configured
    #: (`analyst.llm.fallback`). Never on a bad request, a refusal or an answer that did not
    #: parse: those are the question's fault, and another provider would only hide them. `none`
    #: (or blank) disables it; one equal to the primary is ignored.
    m17_llm_fallback: LlmProvider | None = Field(
        default=LlmProvider.CLAUDE_CLI,
        description="the M17 desk's backup provider, per call: none | anthropic | claude_cli",
    )
    #: Read only while `m17_llm_provider` is bedrock: Bedrock has no web tools of its own. The
    #: research model is the roster's `models.research` (Amendment 5: `claude-sonnet-5-5`, served
    #: on bedrock-runtime through its Global inference profile, as Mantle does not serve it). The
    #: default stays `claude_cli` so that turning our own search layer on is an explicit deploy
    #: step (`M17_WEB_RESEARCH=websearch`), taken with `M17_LLM_PROVIDER=bedrock`.
    m17_web_research: WebResearchPath = Field(
        default=WebResearchPath.CLAUDE_CLI,
        description=(
            "a Bedrock desk's web research: websearch (our own vendors, CLI backup) | claude_cli"
        ),
    )
    bedrock_region: str | None = Field(
        default=None,
        description=(
            "AWS region of the Bedrock Mantle endpoint; required only when a provider is bedrock. "
            "Deliberately not AWS_REGION, which the S3 mirror's AWS tooling reads"
        ),
    )
    bedrock_max_tokens: Annotated[int, Field(ge=1_024, le=128_000)] = Field(
        default=64_000,
        description=(
            "output ceiling of a Bedrock call, thinking included; a caller asking for less gets "
            "this, so thinking at a high effort cannot truncate the answer"
        ),
    )

    # ── alerting (§8.1: FAILED streaks, red quality, reconciliation breaks) ───────────────────
    alert_provider: AlertProvider = Field(
        default=AlertProvider.LOG, description="log | email | telegram; log needs no credential"
    )
    alert_dedup_window_minutes: Annotated[int, Field(ge=0)] = Field(
        default=360,
        description="repeat alerts on one dedup_key are suppressed for this long; 0 disables",
    )
    alert_failure_streak_threshold: Annotated[int, Field(ge=1)] = Field(
        default=3,
        description="consecutive FAILED sessions of one source before failure_alerts pages",
    )
    alert_calendar_lead_days: Annotated[int, Field(ge=0)] = Field(
        default=60,
        description="failure_alerts pages once the holiday calendar's coverage ends this close",
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

    # ── M17 web research (analyst.websearch): vendors, routing, budgets, limits ───────────────
    #: The vendors that share search requests. Each request goes to ONE of them, picked by a
    #: rotation weighted by each one's remaining monthly budget; if it fails or finds nothing,
    #: exactly one other is tried, never a third. A vendor with no key, no budget left, or one
    #: that refused us (401/403/429, never retried) is out of the rotation.
    websearch_search_vendors: str = Field(
        default="brave,tavily,firecrawl",
        description="comma-separated search rotation: brave | tavily | firecrawl",
    )
    websearch_fetch_vendors: str = Field(
        default="firecrawl,tavily",
        description="comma-separated fetch rotation: firecrawl | tavily",
    )
    #: Our own HTTP fetch has no vendor quota. It is used only as a fetch's single fallback, and
    #: only when no fetch vendor has budget left.
    websearch_direct_fallback: bool = Field(
        default=True,
        description="allow the direct fetch as the one fallback when no fetch vendor has budget",
    )
    #: Monthly budgets, in each vendor's own billing unit (credits; one request is one credit
    #: except a Firecrawl search, which costs two). Taken from each vendor's published free
    #: allowance as read on 2026-10-30; the three sum to the owner's ~3,000 a month:
    #: - Brave: https://brave.com/search/api/ — Search plan, "$5 in free credits every month" at
    #:   "$5 per 1,000 requests", i.e. 1,000 requests a month.
    #: - Tavily: https://docs.tavily.com/documentation/api-credits — Researcher (free) plan,
    #:   1,000 credits a month; a basic search is 1 credit, a basic extract 1 credit per 5 URLs.
    #: - Firecrawl: https://www.firecrawl.dev/pricing — Free plan, 1,000 credits a month; a scrape
    #:   is 1 credit, a search 2 credits per 10 results.
    websearch_budget_brave: Annotated[int, Field(ge=0)] = Field(
        default=1000, description="Brave Search API requests per calendar month (IST)"
    )
    websearch_budget_tavily: Annotated[int, Field(ge=0)] = Field(
        default=1000, description="Tavily credits per calendar month (IST)"
    )
    websearch_budget_firecrawl: Annotated[int, Field(ge=0)] = Field(
        default=1000, description="Firecrawl credits per calendar month (IST)"
    )
    #: The owner's overall allowance: every vendor call, fallbacks included, counts one. The
    #: direct fetch is not a vendor call and does not count. At the cap a call is refused, loudly.
    websearch_monthly_request_cap: Annotated[int, Field(ge=0)] = Field(
        default=3000, description="vendor requests per calendar month (IST), all vendors together"
    )
    #: Per M17 session, across every manager: 4 managers x `rounds.max_queries` (8) in
    #: analyst/fundmanager/roster.yaml. A cache hit costs nothing and is not counted.
    websearch_session_max_searches: Annotated[int, Field(ge=0)] = Field(
        default=32, description="web searches one M17 session may send to vendors"
    )
    websearch_session_max_fetches: Annotated[int, Field(ge=0)] = Field(
        default=32, description="page fetches one M17 session may send to vendors"
    )
    websearch_timeout_seconds: Annotated[float, Field(gt=0, le=120)] = Field(
        default=30.0, description="per-request timeout of a vendor call or a direct fetch"
    )
    websearch_max_fetch_bytes: Annotated[int, Field(ge=1024)] = Field(
        default=2 * 1024 * 1024,
        description="most bytes read from one response body; the rest is never downloaded",
    )
    websearch_max_redirects: Annotated[int, Field(ge=0, le=10)] = Field(
        default=5, description="most redirects a direct fetch follows, each one re-checked"
    )
    websearch_max_result_chars: Annotated[int, Field(ge=500)] = Field(
        default=16_000, description="most characters of one tool result handed to the model"
    )
    #: Hosts the scheduler owns (their own leases and request budgets; BSE is paused, D29). The
    #: fetch tool refuses each one and every subdomain of it; search results may still name them.
    websearch_blocked_hosts: str = Field(
        default="nseindia.com,nsearchives.nseindia.com,bseindia.com,api.bseindia.com,"
        "niftyindices.com",
        description="comma-separated hosts the web fetch tool refuses, subdomains included",
    )

    # ── credentials (absent by default; never logged — SecretStr masks its repr) ──────────────
    brave_search_api_key: SecretStr | None = Field(
        default=None, description="Brave Search API subscription token; unset skips Brave"
    )
    tavily_api_key: SecretStr | None = Field(
        default=None, description="Tavily API key; unset skips Tavily"
    )
    firecrawl_api_key: SecretStr | None = Field(
        default=None, description="Firecrawl API key; unset skips Firecrawl"
    )
    anthropic_api_key: SecretStr | None = Field(
        default=None, description="required only when LLM_PROVIDER=anthropic"
    )
    aws_bearer_token_bedrock: SecretStr | None = Field(
        default=None,
        description=(
            "Amazon Bedrock API key; required only when a provider is bedrock. Handed to the SDK "
            "explicitly, never through the process environment"
        ),
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
        "aws_bearer_token_bedrock",
        "bedrock_region",
        "kite_api_key",
        "kite_api_secret",
        "alert_smtp_host",
        "alert_smtp_username",
        "alert_smtp_password",
        "alert_email_from",
        "alert_telegram_bot_token",
        "alert_telegram_chat_id",
        "backup_l0_mirror",
        "restore_drill_database_url",
        mode="before",
    )
    @classmethod
    def _blank_is_absent(cls, value: object) -> object:
        """`KEY=` in a .env means "not configured", not a credential that is the empty string."""
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("brave_search_api_key", "tavily_api_key", "firecrawl_api_key", mode="before")
    @classmethod
    def _blank_vendor_key_is_absent(cls, value: object) -> object:
        """`KEY=` means the vendor is not configured, and the router skips it."""
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("bedrock_region")
    @classmethod
    def _aws_region_name(cls, value: str | None) -> str | None:
        """The region becomes the endpoint's host name, so it must look like one (`us-east-1`)."""
        if value is not None and _AWS_REGION.fullmatch(value) is None:
            raise ValueError(
                f"bedrock_region must be an AWS region name like us-east-1, got {value!r}"
            )
        return value

    @field_validator("llm_fallback", "m17_llm_fallback", mode="before")
    @classmethod
    def _no_fallback(cls, value: object) -> object:
        """`LLM_FALLBACK=` or `=none` means "no backup", not a provider called "none"."""
        if isinstance(value, str) and value.strip().lower() in ("", "none"):
            return None
        return value

    @field_validator("llm_fallback", "m17_llm_fallback")
    @classmethod
    def _fallback_can_decide(cls, value: LlmProvider | None) -> LlmProvider | None:
        """A stub cannot decide, so it is never a backup: a fallback to it would journal a desk of
        manager errors that read as a model outage (`backtest.fm_world.m17_provider`)."""
        if value is LlmProvider.STUB:
            raise ValueError("a fallback cannot be stub: a stub cannot decide; use none")
        return value

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
    def _postgres_dsn(cls, value: str) -> str:
        """Postgres is the only supported store (§8.1); a DSN for anything else is a typo."""
        if not value.startswith(("postgres://", "postgresql://", "postgresql+")):
            raise ValueError(f"database_url must be a Postgres DSN, got {value.split(':')[0]!r}")
        return value

    @field_validator("data_root")
    @classmethod
    def _anchor_at_repo_root(cls, value: Path) -> Path:
        return value if value.is_absolute() else REPO_ROOT / value

    @field_validator("backup_root")
    @classmethod
    def _backup_root_outside_the_checkout(cls, value: Path) -> Path:
        """A relative backup root would land in whichever checkout ran the job — a worktree's
        copy is deleted with the worktree, and the repo itself is no place for a dump."""
        expanded = value.expanduser()
        if not expanded.is_absolute():
            raise ValueError(f"backup_root must be absolute or ~-prefixed, got {str(value)!r}")
        return expanded

    @property
    def tzinfo(self) -> ZoneInfo:
        """`timezone` as a tzinfo, for wiring a `Clock`."""
        return ZoneInfo(self.timezone)

    @property
    def alert_email_recipients(self) -> tuple[str, ...]:
        """`alert_email_to` as addresses. Empty when nothing is configured, never `('',)`."""
        return tuple(part.strip() for part in self.alert_email_to.split(",") if part.strip())

    def llm_wiring(self) -> dict[str, str | bool | int | None]:
        """Which LLM providers this process wires, for a startup log line — never a secret.

        The credential and the region are reported as set or not set only; the key's value never
        leaves its `SecretStr`.
        """
        return {
            "llm_provider": self.llm_provider.value,
            "llm_fallback": None if self.llm_fallback is None else self.llm_fallback.value,
            "m17_llm_provider": self.m17_llm_provider.value,
            "m17_llm_fallback": None
            if self.m17_llm_fallback is None
            else self.m17_llm_fallback.value,
            "bedrock_key_set": self.aws_bearer_token_bedrock is not None,
            "bedrock_region_set": self.bedrock_region is not None,
            "bedrock_max_tokens": self.bedrock_max_tokens,
        }

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
