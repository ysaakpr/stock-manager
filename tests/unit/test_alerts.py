"""Alerting behaves like a channel someone relies on (§8.1, C.3).

Three properties are load-bearing and each is asserted here rather than assumed: the default
channel needs no credential (B4), a `dedup_key` collapses a failure streak into a handful of
alerts instead of one per poll, and a channel that was selected but never configured says so
loudly instead of quietly not alerting.

Everything runs offline — the socket ban below is asserted, not assumed (B8) — and every window
is measured against a `FrozenClock`, so the five-day streak case takes microseconds and cannot
become flaky on a slow machine.
"""

from __future__ import annotations

import io
import json
import socket
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any

import httpx
import pytest
import structlog
from pydantic import SecretStr

from dataplatform.alerts import (
    DEFAULT_DEDUP_WINDOW,
    TELEGRAM_MAX_CHARS,
    AlertConfigurationError,
    Alerter,
    AlertOutcome,
    BaseAlerter,
    EmailAlerter,
    EmailConfig,
    LogAlerter,
    Severity,
    TelegramAlerter,
    TelegramConfig,
    TelegramDeliveryError,
    _telegram_transport,
    build_alerter,
)
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import REPO_ROOT, AlertProvider
from dataplatform.logging import configure_logging, get_logger
from tests.conftest import SettingsLoader

EXAMPLE_ENV = REPO_ROOT / ".env.example"
CLOSE = datetime(2026, 8, 7, 15, 30, tzinfo=IST)
HOUR = timedelta(hours=1)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any socket use in this module is a bug: the transports are injected in every test."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("alerting tests must not touch the network")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@pytest.fixture(autouse=True)
def reset_structlog() -> Iterator[None]:
    """`configure_logging` is process-wide state; no test may leak it into the next one."""
    yield
    structlog.contextvars.clear_contextvars()
    structlog.reset_defaults()


@pytest.fixture
def log_stream(load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """Route logging into a buffer as JSON, so an emitted alert can be read back as fields."""
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    stream = io.StringIO()
    configure_logging(load_settings(None), clock=FrozenClock(CLOSE), stream=stream)
    return stream


def emitted(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


class SpyAlerter(BaseAlerter):
    """A channel that records instead of sending, so the base class's dedup is observable."""

    channel = "spy"

    def __init__(
        self,
        *,
        clock: FrozenClock,
        dedup_window: timedelta = DEFAULT_DEDUP_WINDOW,
        fail_with: Exception | None = None,
    ) -> None:
        super().__init__(clock=clock, dedup_window=dedup_window)
        self.delivered: list[tuple[Severity, str, str, str]] = []
        self.fail_with = fail_with

    def _deliver(self, severity: Severity, title: str, body: str, dedup_key: str) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.delivered.append((severity, title, body, dedup_key))


class MailRecorder:
    """Stands in for the SMTP relay."""

    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    def __call__(self, config: EmailConfig, message: EmailMessage) -> None:
        self.messages.append(message)


class TelegramRecorder:
    """Stands in for the Bot API."""

    def __init__(self) -> None:
        self.payloads: list[Mapping[str, str]] = []

    def __call__(self, config: TelegramConfig, payload: Mapping[str, str]) -> None:
        self.payloads.append(payload)


def email_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALERT_PROVIDER", "email")
    monkeypatch.setenv("ALERT_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("ALERT_EMAIL_FROM", "platform@example.test")
    monkeypatch.setenv("ALERT_EMAIL_TO", "owner@example.test, oncall@example.test")


def telegram_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALERT_PROVIDER", "telegram")
    monkeypatch.setenv("ALERT_TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("ALERT_TELEGRAM_CHAT_ID", "-100999")


# ── acceptance 1: the log channel is the default and needs no credentials ─────────────────────


def test_the_shipped_configuration_selects_the_credential_free_log_channel(
    load_settings: SettingsLoader,
) -> None:
    """B4: no relay and no bot token exist, so nothing may default to needing one."""
    settings = load_settings(EXAMPLE_ENV)

    assert settings.alert_provider is AlertProvider.LOG
    assert settings.alert_smtp_host is None
    assert settings.alert_email_recipients == ()
    assert settings.alert_telegram_bot_token is None
    assert isinstance(build_alerter(settings), LogAlerter)


def test_the_log_alerter_constructs_and_sends_with_no_configuration_at_all() -> None:
    alerter = LogAlerter(clock=FrozenClock(CLOSE))

    assert alerter.send(Severity.WARNING, "gap", "no bhavcopy", "ingest:nse") is AlertOutcome.SENT


def test_every_alerter_satisfies_the_protocol_callers_depend_on() -> None:
    """A caller holds an `Alerter`; swapping the channel must never be a caller's problem."""
    clock = FrozenClock(CLOSE)
    email = EmailAlerter(
        EmailConfig(
            host="smtp.example.test", port=587, sender="a@example.test", recipients=("b@x.test",)
        ),
        clock=clock,
    )
    telegram = TelegramAlerter(TelegramConfig(bot_token=SecretStr("t"), chat_id="1"), clock=clock)

    for alerter in (LogAlerter(clock=clock), email, telegram):
        assert isinstance(alerter, Alerter)


@pytest.mark.parametrize(
    ("severity", "level"),
    [(Severity.INFO, "info"), (Severity.WARNING, "warning"), (Severity.CRITICAL, "critical")],
)
def test_the_log_alerter_emits_one_queryable_event_per_alert(
    severity: Severity, level: str, log_stream: io.StringIO
) -> None:
    """The alert has to be findable by field, not by reading prose out of a message string."""
    LogAlerter(clock=FrozenClock(CLOSE)).send(
        severity, "bhavcopy missing", "nse_bhavcopy FAILED 5 days", "ingest:nse_bhavcopy:FAILED"
    )

    (event,) = emitted(log_stream)
    assert event["event"] == "alert"
    assert event["level"] == level
    assert event["channel"] == "log"
    assert event["severity"] == severity.value
    assert event["title"] == "bhavcopy missing"
    assert event["body"] == "nse_bhavcopy FAILED 5 days"
    assert event["dedup_key"] == "ingest:nse_bhavcopy:FAILED"


# ── acceptance 2: dedup_key suppresses repeats within a configurable window ───────────────────


def test_a_repeat_inside_the_window_is_suppressed_and_the_first_one_is_not() -> None:
    clock = FrozenClock(CLOSE)
    alerter = SpyAlerter(clock=clock, dedup_window=timedelta(hours=6))

    assert alerter.send(Severity.CRITICAL, "red", "quality red", "quality:red") is AlertOutcome.SENT
    clock.advance(timedelta(hours=5, minutes=59))
    outcome = alerter.send(Severity.CRITICAL, "red", "quality red", "quality:red")

    assert outcome is AlertOutcome.SUPPRESSED
    assert len(alerter.delivered) == 1


def test_the_window_reopens_once_it_has_elapsed() -> None:
    clock = FrozenClock(CLOSE)
    alerter = SpyAlerter(clock=clock, dedup_window=timedelta(hours=6))

    alerter.send(Severity.WARNING, "stale", "no sync", "sync:stale")
    clock.advance(timedelta(hours=6))

    assert alerter.send(Severity.WARNING, "stale", "no sync", "sync:stale") is AlertOutcome.SENT
    assert len(alerter.delivered) == 2


def test_different_dedup_keys_do_not_shadow_each_other() -> None:
    """Two sources failing at once is two pieces of news, not one."""
    alerter = SpyAlerter(clock=FrozenClock(CLOSE), dedup_window=timedelta(days=1))

    alerter.send(Severity.CRITICAL, "down", "403 spike", "ingest:nse:403")
    alerter.send(Severity.CRITICAL, "down", "403 spike", "ingest:bse:403")

    assert [entry[3] for entry in alerter.delivered] == ["ingest:nse:403", "ingest:bse:403"]


def test_a_five_day_failure_streak_alerts_once_a_day_rather_than_once_an_hour() -> None:
    """The case the dedup window exists for: 120 hourly reports become 5 alerts, not 500."""
    clock = FrozenClock(CLOSE)
    alerter = SpyAlerter(clock=clock, dedup_window=timedelta(days=1))

    outcomes = []
    for _ in range(5 * 24):
        outcomes.append(
            alerter.send(Severity.CRITICAL, "bhavcopy FAILED", "5-day streak", "ingest:nse:FAILED")
        )
        clock.advance(HOUR)

    assert len(alerter.delivered) == 5
    assert outcomes.count(AlertOutcome.SENT) == 5
    assert outcomes.count(AlertOutcome.SUPPRESSED) == 115


def test_a_zero_window_delivers_every_alert() -> None:
    """The window is configurable, and configuring it to nothing must disable suppression."""
    alerter = SpyAlerter(clock=FrozenClock(CLOSE), dedup_window=timedelta(0))

    for _ in range(3):
        assert alerter.send(Severity.INFO, "tick", "body", "same:key") is AlertOutcome.SENT

    assert len(alerter.delivered) == 3


def test_the_window_comes_from_configuration(
    load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALERT_DEDUP_WINDOW_MINUTES", "30")
    clock = FrozenClock(CLOSE)
    alerter = build_alerter(load_settings(None), clock=clock)

    assert alerter.send(Severity.INFO, "t", "b", "k") is AlertOutcome.SENT
    clock.advance(timedelta(minutes=29))
    assert alerter.send(Severity.INFO, "t", "b", "k") is AlertOutcome.SUPPRESSED
    clock.advance(timedelta(minutes=1))
    assert alerter.send(Severity.INFO, "t", "b", "k") is AlertOutcome.SENT


def test_a_failed_delivery_does_not_consume_the_dedup_slot() -> None:
    """Suppressing a retry because of a send that never happened would lose the alert entirely."""
    clock = FrozenClock(CLOSE)
    alerter = SpyAlerter(
        clock=clock, dedup_window=timedelta(days=1), fail_with=OSError("relay refused")
    )

    with pytest.raises(OSError, match="relay refused"):
        alerter.send(Severity.CRITICAL, "recon break", "book != broker", "recon:break")

    alerter.fail_with = None
    assert alerter.send(Severity.CRITICAL, "recon break", "book != broker", "recon:break") is (
        AlertOutcome.SENT
    )


def test_expired_keys_are_forgotten_so_the_table_tracks_the_live_problem() -> None:
    clock = FrozenClock(CLOSE)
    alerter = SpyAlerter(clock=clock, dedup_window=HOUR)

    for index in range(50):
        alerter.send(Severity.INFO, "t", "b", f"source:{index}")
    clock.advance(timedelta(hours=2))
    alerter.send(Severity.INFO, "t", "b", "source:0")

    assert set(alerter._sent_at) == {"source:0"}


def test_an_empty_dedup_key_is_rejected_rather_than_treated_as_one_bucket() -> None:
    alerter = SpyAlerter(clock=FrozenClock(CLOSE))

    with pytest.raises(ValueError, match="dedup_key"):
        alerter.send(Severity.INFO, "t", "b", "  ")


def test_a_negative_window_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="dedup_window"):
        SpyAlerter(clock=FrozenClock(CLOSE), dedup_window=-HOUR)


# ── acceptance 3: an unconfigured email/telegram alerter raises a named, actionable error ─────


def test_an_unconfigured_email_alerter_names_every_missing_key(
    load_settings: SettingsLoader,
) -> None:
    with pytest.raises(AlertConfigurationError) as caught:
        EmailAlerter.from_settings(load_settings(None))

    assert caught.value.channel == "email"
    assert caught.value.missing == ("ALERT_SMTP_HOST", "ALERT_EMAIL_FROM", "ALERT_EMAIL_TO")
    assert "ALERT_PROVIDER=log" in str(caught.value)


def test_an_unconfigured_telegram_alerter_names_every_missing_key(
    load_settings: SettingsLoader,
) -> None:
    with pytest.raises(AlertConfigurationError) as caught:
        TelegramAlerter.from_settings(load_settings(None))

    assert caught.value.channel == "telegram"
    assert caught.value.missing == ("ALERT_TELEGRAM_BOT_TOKEN", "ALERT_TELEGRAM_CHAT_ID")
    assert "ALERT_PROVIDER=log" in str(caught.value)


def test_a_half_configured_channel_names_only_what_is_actually_missing(
    load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Not configured" is not a useful error when the operator set two of the three keys."""
    monkeypatch.setenv("ALERT_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("ALERT_EMAIL_FROM", "platform@example.test")

    with pytest.raises(AlertConfigurationError) as caught:
        EmailAlerter.from_settings(load_settings(None))

    assert caught.value.missing == ("ALERT_EMAIL_TO",)


def test_a_blank_credential_counts_as_absent(
    load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ALERT_TELEGRAM_BOT_TOKEN=` must not read as a token that happens to be empty."""
    monkeypatch.setenv("ALERT_TELEGRAM_BOT_TOKEN", "   ")
    monkeypatch.setenv("ALERT_TELEGRAM_CHAT_ID", "-100999")

    with pytest.raises(AlertConfigurationError) as caught:
        TelegramAlerter.from_settings(load_settings(None))

    assert caught.value.missing == ("ALERT_TELEGRAM_BOT_TOKEN",)


@pytest.mark.parametrize("provider", ["email", "telegram"])
def test_selecting_an_unconfigured_channel_fails_instead_of_falling_back_to_the_log(
    provider: str, load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silent fallback leaves an operator believing their phone will ring. It will not."""
    monkeypatch.setenv("ALERT_PROVIDER", provider)

    with pytest.raises(AlertConfigurationError):
        build_alerter(load_settings(None))


# ── the configured channels ──────────────────────────────────────────────────────────────────


def test_a_configured_email_alerter_sends_one_message_per_undeduplicated_alert(
    load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    email_settings(monkeypatch)
    monkeypatch.setenv("ALERT_DEDUP_WINDOW_MINUTES", "60")
    relay = MailRecorder()
    clock = FrozenClock(CLOSE)
    alerter = EmailAlerter.from_settings(load_settings(None), clock=clock, transport=relay)

    alerter.send(Severity.CRITICAL, "recon break", "book != broker", "recon:break")
    alerter.send(Severity.CRITICAL, "recon break", "book != broker", "recon:break")

    (message,) = relay.messages
    assert message["Subject"] == "[CRITICAL] recon break"
    assert message["From"] == "platform@example.test"
    assert message["To"] == "owner@example.test, oncall@example.test"
    assert message["X-Alert-Severity"] == "critical"
    assert message["X-Alert-Dedup-Key"] == "recon:break"
    assert message.get_content().strip() == "book != broker"


def test_the_recipient_list_is_split_on_commas(
    load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALERT_EMAIL_TO", " owner@example.test ,, oncall@example.test ")

    assert load_settings(None).alert_email_recipients == (
        "owner@example.test",
        "oncall@example.test",
    )


def test_a_configured_telegram_alerter_posts_a_plain_text_message(
    load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    telegram_settings(monkeypatch)
    bot = TelegramRecorder()
    alerter = TelegramAlerter.from_settings(
        load_settings(None), clock=FrozenClock(CLOSE), transport=bot
    )

    alerter.send(Severity.WARNING, "403 spike", "nse_bhavcopy hard-stopped", "ingest:nse:403")

    (payload,) = bot.payloads
    assert payload["chat_id"] == "-100999"
    assert payload["text"] == "[WARNING] 403 spike\n\nnse_bhavcopy hard-stopped"
    assert "test-token" not in payload["text"]


def test_the_telegram_token_lives_in_the_url_and_nowhere_else() -> None:
    config = TelegramConfig(bot_token=SecretStr("123456:test-token"), chat_id="-100999")

    assert config.send_message_url().endswith("/bot123456:test-token/sendMessage")
    assert "test-token" not in repr(config)


def test_a_rejected_telegram_call_raises_without_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`httpx.HTTPStatusError.__str__` embeds the full request URL — token in the path, per
    `TelegramConfig.send_message_url` — so it must never escape `_telegram_transport` (invariant
    #13): a wrong chat id or a bot token mid-rotation is exactly when this path is exercised."""
    config = TelegramConfig(bot_token=SecretStr("123456:a-real-looking-token"), chat_id="-100999")
    request = httpx.Request("POST", config.send_message_url())
    response = httpx.Response(401, request=request, text='{"ok": false}')
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: response)

    with pytest.raises(TelegramDeliveryError) as caught:
        _telegram_transport(config, {"chat_id": "-100999", "text": "hi"})

    assert "a-real-looking-token" not in str(caught.value)
    assert caught.value.__cause__ is None  # raised `from None`: no chained, token-bearing cause
    assert caught.value.status_code == 401
    assert caught.value.chat_id == "-100999"


def test_a_rejected_telegram_call_does_not_leak_the_token_into_the_log(
    monkeypatch: pytest.MonkeyPatch, log_stream: io.StringIO
) -> None:
    """The exact production path the audit named: a scheduler job catches the delivery failure
    and logs it with `exc_info=True` (scheduler/runner.py)."""
    config = TelegramConfig(bot_token=SecretStr("123456:a-real-looking-token"), chat_id="-100999")
    request = httpx.Request("POST", config.send_message_url())
    response = httpx.Response(401, request=request)
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: response)
    alerter = TelegramAlerter(config, clock=FrozenClock(CLOSE))

    try:
        alerter.send(Severity.CRITICAL, "test", "body", "dedup:key")
    except TelegramDeliveryError:
        get_logger().exception("job_failed")

    assert "a-real-looking-token" not in log_stream.getvalue()


def test_an_oversized_body_is_truncated_rather_than_rejected_by_the_api() -> None:
    """A 6 kB traceback must still produce an alert; the API would reject the whole message."""
    alerter = TelegramAlerter(
        TelegramConfig(bot_token=SecretStr("t"), chat_id="1"),
        clock=FrozenClock(CLOSE),
        transport=TelegramRecorder(),
    )

    payload = alerter.build_payload(Severity.CRITICAL, "traceback", "x" * 6000)

    assert len(payload["text"]) == TELEGRAM_MAX_CHARS
    assert payload["text"].endswith("… truncated")


def test_a_configured_telegram_alerter_is_what_the_factory_returns(
    load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    telegram_settings(monkeypatch)

    assert isinstance(build_alerter(load_settings(None)), TelegramAlerter)
