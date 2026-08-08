"""Alerting (§8.1 observability) — how a broken pipeline reaches a human.

The status API says what state the platform is in; this module is what wakes someone when that
state is bad: a FAILED ingestion streak, quality gone red, a reconciliation break, a 403 spike
that hard-stopped a source (§4.1, AGENTIC_CONTEXT §8).

Three properties shape the design.

*The default must need no credential.* No SMTP relay and no bot token exist yet (B4), so
`LogAlerter` — a structured log event at the alert's severity — is what ships and what the tests
exercise. `EmailAlerter` and `TelegramAlerter` implement the same protocol behind config; the day
a credential appears, `ALERT_PROVIDER` changes and no caller does.

*A selected-but-unconfigured channel fails loud.* Falling back to the log channel because the
token is missing would produce a system that believes it is alerting and is not — the exact
failure alerting exists to prevent. Constructing such a channel raises `AlertConfigurationError`,
naming the environment keys to set, at startup rather than at 3 a.m.

*Repeats are suppressed, not multiplied.* A source that has been failing for five days is one
piece of news. Every alerter dedups on the caller's `dedup_key` for a configurable window, in the
base class rather than per channel, so a channel added later cannot forget to.

Suppression state is in-process and is deliberately lost on restart: the scheduler is a long-lived
process (§8.1), and a restart is itself a reason to hear the alert again. Nothing here reads the
wall clock — the window is measured with an injected `Clock` (B10), so a test proves the five-day
case in microseconds.
"""

from __future__ import annotations

import smtplib
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from enum import StrEnum
from threading import Lock
from typing import ClassVar, Protocol, assert_never, runtime_checkable

import httpx
import structlog
from pydantic import SecretStr
from structlog.typing import FilteringBoundLogger

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import AlertProvider, Settings, get_settings
from dataplatform.logging import get_logger

#: Suppression window used when a caller constructs an alerter without consulting `Settings`.
DEFAULT_DEDUP_WINDOW = timedelta(hours=6)

#: Telegram rejects a `sendMessage` body over 4096 characters; a long traceback is truncated
#: rather than dropped, because a truncated alert still tells someone to go look.
TELEGRAM_MAX_CHARS = 4096

#: Telegram Bot API root. The bot token travels in the path, so this URL is never logged.
TELEGRAM_API_ROOT = "https://api.telegram.org"

#: The SMTP port that speaks TLS from the first byte; every other port gets STARTTLS.
SMTPS_PORT = 465

_SMTP_TIMEOUT_SECONDS = 30.0
_TELEGRAM_TIMEOUT_SECONDS = 15.0

__all__ = [
    "DEFAULT_DEDUP_WINDOW",
    "SMTPS_PORT",
    "TELEGRAM_API_ROOT",
    "TELEGRAM_MAX_CHARS",
    "AlertConfigurationError",
    "AlertOutcome",
    "Alerter",
    "BaseAlerter",
    "EmailAlerter",
    "EmailConfig",
    "LogAlerter",
    "Severity",
    "TelegramAlerter",
    "TelegramConfig",
    "build_alerter",
]


class Severity(StrEnum):
    """How loud this alert is. `CRITICAL` means the platform stopped doing its job."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertOutcome(StrEnum):
    """What `send` did — worth journaling, since "no alert" has two very different causes."""

    SENT = "sent"
    SUPPRESSED = "suppressed"


class AlertConfigurationError(RuntimeError):
    """A channel was selected but its credentials are absent (B4).

    Carries the channel and the exact environment keys that are missing, so the message is a
    repair instruction rather than a puzzle. Raised at construction, not at the first alert.
    """

    def __init__(self, channel: str, missing: Sequence[str]) -> None:
        self.channel = channel
        self.missing: tuple[str, ...] = tuple(missing)
        super().__init__(
            f"{channel} alerting is selected but not configured: "
            f"set {', '.join(self.missing)} in .env, "
            f"or set ALERT_PROVIDER=log to use the credential-free LogAlerter."
        )


@runtime_checkable
class Alerter(Protocol):
    """Everything the rest of the platform may know about alerting.

    One method, so a caller cannot depend on a channel's specifics, and `dedup_key` is required
    rather than defaulted — choosing the key is the caller's judgement about what counts as "the
    same news", and a default would silently make every alert unique.
    """

    def send(self, severity: Severity, title: str, body: str, dedup_key: str) -> AlertOutcome:
        """Deliver the alert unless an identical `dedup_key` is still inside the window."""


class BaseAlerter(ABC):
    """Deduplication and the audit trail, shared by every channel.

    What it does: implements `send` once — suppress-or-deliver against the `dedup_key` table,
    then record the send time — and delegates the channel-specific part to `_deliver`.
    What it assumes: `dedup_key` is stable across repeats of the same problem (`"ingest:nse_
    bhavcopy:FAILED"`, not one containing a timestamp), and that the injected clock moves forward.
    What it never does: swallow a delivery failure. `_deliver` raising propagates to the caller
    *and* leaves the dedup slot unclaimed, so the next attempt is not suppressed by a send that
    never happened.

    Subclassing rather than wrapping is deliberate: dedup is not a decoration a new channel can be
    constructed without.
    """

    #: Channel name, bound onto every log event this alerter emits.
    channel: ClassVar[str]

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        dedup_window: timedelta = DEFAULT_DEDUP_WINDOW,
    ) -> None:
        if dedup_window < timedelta(0):
            raise ValueError(f"dedup_window must not be negative, got {dedup_window!r}")
        self._clock: Clock = SystemClock() if clock is None else clock
        self._dedup_window = dedup_window
        self._sent_at: dict[str, datetime] = {}
        self._lock = Lock()
        self._log: FilteringBoundLogger = get_logger("alerts", channel=self.channel)

    def send(self, severity: Severity, title: str, body: str, dedup_key: str) -> AlertOutcome:
        """Deliver the alert unless an identical `dedup_key` is still inside the window.

        The lock is held across delivery so two scheduler threads reporting the same failure
        cannot both pass the check and both send; alerts are rare enough that serializing them
        costs nothing.
        """
        if not dedup_key.strip():
            raise ValueError(
                "dedup_key must be a non-empty key that is stable across repeats of the same "
                "problem — it is the only thing standing between a failure streak and 500 alerts"
            )

        with self._lock:
            now = self._clock.now()
            self._forget_expired(now)
            last_sent = self._sent_at.get(dedup_key)
            if last_sent is not None and now - last_sent < self._dedup_window:
                self._log.debug(
                    "alert_suppressed",
                    dedup_key=dedup_key,
                    severity=severity.value,
                    title=title,
                    sendable_after=(last_sent + self._dedup_window).isoformat(),
                )
                return AlertOutcome.SUPPRESSED

            self._deliver(severity, title, body, dedup_key)
            self._sent_at[dedup_key] = now
            return AlertOutcome.SENT

    @abstractmethod
    def _deliver(self, severity: Severity, title: str, body: str, dedup_key: str) -> None:
        """Put the alert on the wire. Raises on failure; never returns a failure quietly."""

    def _forget_expired(self, now: datetime) -> None:
        """Drop keys whose window has passed, so the table stays the size of the live problem."""
        cutoff = now - self._dedup_window
        self._sent_at = {key: at for key, at in self._sent_at.items() if at > cutoff}


_LOG_LEVEL: dict[Severity, int] = {
    Severity.INFO: structlog.processors.NAME_TO_LEVEL["info"],
    Severity.WARNING: structlog.processors.NAME_TO_LEVEL["warning"],
    Severity.CRITICAL: structlog.processors.NAME_TO_LEVEL["critical"],
}


class LogAlerter(BaseAlerter):
    """The default channel: the alert becomes a structured log event. No credentials (B4).

    What it does: emits one `alert` event at the level matching the severity, carrying the title,
    body and dedup key as fields the log store can query.
    What it assumes: something is watching the logs — in dev that is a terminal, in prod whatever
    ships the container's stderr.
    What it never does: fail. A channel that can break is a channel that cannot be the fallback
    for a system with no credentials.
    """

    channel = "log"

    def _deliver(self, severity: Severity, title: str, body: str, dedup_key: str) -> None:
        self._log.log(
            _LOG_LEVEL[severity],
            "alert",
            severity=severity.value,
            title=title,
            body=body,
            dedup_key=dedup_key,
        )


@dataclass(frozen=True, slots=True)
class EmailConfig:
    """A validated SMTP destination. Existing means "configured" — see `from_settings`."""

    host: str
    port: int
    sender: str
    recipients: tuple[str, ...]
    username: str | None = None
    password: SecretStr | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> EmailConfig:
        """Build from `Settings`, or raise `AlertConfigurationError` naming what is missing.

        Username and password are optional on purpose: an internal relay that accepts mail from
        the VM without authenticating is a legitimate configuration, and demanding a credential
        that the deployment does not have would push the operator toward inventing one.
        """
        host, sender = settings.alert_smtp_host, settings.alert_email_from
        recipients = settings.alert_email_recipients
        if host is None or sender is None or not recipients:
            raise AlertConfigurationError(
                "email",
                [
                    key
                    for key, present in (
                        ("ALERT_SMTP_HOST", host is not None),
                        ("ALERT_EMAIL_FROM", sender is not None),
                        ("ALERT_EMAIL_TO", bool(recipients)),
                    )
                    if not present
                ],
            )
        return cls(
            host=host,
            port=settings.alert_smtp_port,
            sender=sender,
            recipients=recipients,
            username=settings.alert_smtp_username,
            password=settings.alert_smtp_password,
        )


#: Hands a built message to a relay. Injected so tests exercise the real alerter offline (B8).
MailTransport = Callable[[EmailConfig, EmailMessage], None]


class EmailAlerter(BaseAlerter):
    """Email over SMTP, for the alerts someone reads rather than reacts to within the minute.

    What it does: renders the alert as a plain-text message with the severity in the subject and
    the dedup key in a header, and hands it to the relay in `EmailConfig`.
    What it assumes: the relay is reachable and accepts mail from this host; a rejection raises.
    What it never does: exist unconfigured — `from_settings` raises rather than returning an
    alerter that would discover at 3 a.m. that it has no host.
    """

    channel = "email"

    def __init__(
        self,
        config: EmailConfig,
        *,
        clock: Clock | None = None,
        dedup_window: timedelta = DEFAULT_DEDUP_WINDOW,
        transport: MailTransport | None = None,
    ) -> None:
        super().__init__(clock=clock, dedup_window=dedup_window)
        self._config = config
        self._transport: MailTransport = _smtp_transport if transport is None else transport

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        clock: Clock | None = None,
        transport: MailTransport | None = None,
    ) -> EmailAlerter:
        """Wire from configuration, raising `AlertConfigurationError` if it is incomplete."""
        settings = get_settings() if settings is None else settings
        return cls(
            EmailConfig.from_settings(settings),
            clock=clock,
            dedup_window=_dedup_window(settings),
            transport=transport,
        )

    def build_message(
        self, severity: Severity, title: str, body: str, dedup_key: str
    ) -> EmailMessage:
        """The message this alerter would send. Separate from delivery so it is testable offline.

        The dedup key rides along as a header so a mail client can thread a streak, and so a
        post-mortem can tell which alerts were the same news.
        """
        message = EmailMessage()
        message["Subject"] = f"[{severity.value.upper()}] {title}"
        message["From"] = self._config.sender
        message["To"] = ", ".join(self._config.recipients)
        message["X-Alert-Severity"] = severity.value
        message["X-Alert-Dedup-Key"] = dedup_key
        message.set_content(body)
        return message

    def _deliver(self, severity: Severity, title: str, body: str, dedup_key: str) -> None:
        self._transport(self._config, self.build_message(severity, title, body, dedup_key))
        self._log.info(
            "alert_email_sent",
            severity=severity.value,
            title=title,
            dedup_key=dedup_key,
            recipients=len(self._config.recipients),
        )


def _smtp_transport(config: EmailConfig, message: EmailMessage) -> None:
    """Hand `message` to the configured relay over TLS. The only socket on the email path.

    Assumes port 465 means implicit TLS and anything else means STARTTLS, which is the convention
    every relay follows; an unencrypted session is never attempted, because the alert body can
    name positions and failures.
    """
    if config.port == SMTPS_PORT:
        client: smtplib.SMTP = smtplib.SMTP_SSL(
            config.host, config.port, timeout=_SMTP_TIMEOUT_SECONDS
        )
    else:
        client = smtplib.SMTP(config.host, config.port, timeout=_SMTP_TIMEOUT_SECONDS)
    with client as smtp:
        if config.port != SMTPS_PORT:
            smtp.starttls()
        if config.username is not None and config.password is not None:
            smtp.login(config.username, config.password.get_secret_value())
        smtp.send_message(message)


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    """A validated Telegram bot destination. Existing means "configured"."""

    bot_token: SecretStr
    chat_id: str

    @classmethod
    def from_settings(cls, settings: Settings) -> TelegramConfig:
        """Build from `Settings`, or raise `AlertConfigurationError` naming what is missing."""
        token, chat_id = settings.alert_telegram_bot_token, settings.alert_telegram_chat_id
        if token is None or chat_id is None:
            raise AlertConfigurationError(
                "telegram",
                [
                    key
                    for key, present in (
                        ("ALERT_TELEGRAM_BOT_TOKEN", token is not None),
                        ("ALERT_TELEGRAM_CHAT_ID", chat_id is not None),
                    )
                    if not present
                ],
            )
        return cls(bot_token=token, chat_id=chat_id)

    def send_message_url(self) -> str:
        """The Bot API endpoint, token included. Never log this — it *is* the credential."""
        return f"{TELEGRAM_API_ROOT}/bot{self.bot_token.get_secret_value()}/sendMessage"


#: Posts a built payload to the Bot API. Injected so tests exercise the real alerter offline (B8).
TelegramTransport = Callable[[TelegramConfig, Mapping[str, str]], None]


class TelegramAlerter(BaseAlerter):
    """Telegram, for the alerts that should reach a phone.

    What it does: posts one plain-text `sendMessage` per alert to the configured chat.
    What it assumes: the bot is already a member of that chat — the API returns 403 otherwise,
    which surfaces as a raised `HTTPStatusError` rather than a silent no-op.
    What it never does: put the alert body in the URL, or log the URL: the token is in the path.
    """

    channel = "telegram"

    def __init__(
        self,
        config: TelegramConfig,
        *,
        clock: Clock | None = None,
        dedup_window: timedelta = DEFAULT_DEDUP_WINDOW,
        transport: TelegramTransport | None = None,
    ) -> None:
        super().__init__(clock=clock, dedup_window=dedup_window)
        self._config = config
        self._transport: TelegramTransport = _telegram_transport if transport is None else transport

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        clock: Clock | None = None,
        transport: TelegramTransport | None = None,
    ) -> TelegramAlerter:
        """Wire from configuration, raising `AlertConfigurationError` if it is incomplete."""
        settings = get_settings() if settings is None else settings
        return cls(
            TelegramConfig.from_settings(settings),
            clock=clock,
            dedup_window=_dedup_window(settings),
            transport=transport,
        )

    def build_payload(self, severity: Severity, title: str, body: str) -> dict[str, str]:
        """The `sendMessage` body this alerter would post, truncated to the API's limit.

        Plain text, no `parse_mode`: a traceback full of underscores and asterisks would otherwise
        be rejected as malformed Markdown, and an alert that fails to render is an alert lost.
        """
        text = f"[{severity.value.upper()}] {title}\n\n{body}"
        if len(text) > TELEGRAM_MAX_CHARS:
            marker = "\n… truncated"
            text = text[: TELEGRAM_MAX_CHARS - len(marker)] + marker
        return {"chat_id": self._config.chat_id, "text": text}

    def _deliver(self, severity: Severity, title: str, body: str, dedup_key: str) -> None:
        self._transport(self._config, self.build_payload(severity, title, body))
        self._log.info(
            "alert_telegram_sent",
            severity=severity.value,
            title=title,
            dedup_key=dedup_key,
            chat_id=self._config.chat_id,
        )


def _telegram_transport(config: TelegramConfig, payload: Mapping[str, str]) -> None:
    """POST `payload` to the Bot API. The only socket on the Telegram path.

    A non-2xx response raises: the Bot API answers a wrong chat id or a revoked token with a
    perfectly well-formed error body, and treating that as delivery is how alerting dies quietly.
    """
    response = httpx.post(
        config.send_message_url(), json=dict(payload), timeout=_TELEGRAM_TIMEOUT_SECONDS
    )
    response.raise_for_status()


def _dedup_window(settings: Settings) -> timedelta:
    """The configured suppression window; zero means every alert is delivered."""
    return timedelta(minutes=settings.alert_dedup_window_minutes)


def build_alerter(settings: Settings | None = None, *, clock: Clock | None = None) -> Alerter:
    """The alerter this configuration selects.

    What it does: reads `ALERT_PROVIDER` and returns the matching channel, already carrying the
    configured dedup window.
    What it assumes: the caller owns the clock — production passes `SystemClock`, replay and tests
    pass a `FrozenClock` so suppression is deterministic (B10).
    What it never does: fall back to `LogAlerter` when the selected channel is unconfigured. That
    would leave an operator believing their phone will ring; the `AlertConfigurationError` from
    here fails the process at startup instead.
    """
    settings = get_settings() if settings is None else settings
    match settings.alert_provider:
        case AlertProvider.LOG:
            return LogAlerter(clock=clock, dedup_window=_dedup_window(settings))
        case AlertProvider.EMAIL:
            return EmailAlerter.from_settings(settings, clock=clock)
        case AlertProvider.TELEGRAM:
            return TelegramAlerter.from_settings(settings, clock=clock)
        case _:  # pragma: no cover — exhaustive over AlertProvider
            assert_never(settings.alert_provider)
