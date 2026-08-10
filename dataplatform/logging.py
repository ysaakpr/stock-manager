"""Structured logging (§8.1 observability) — configured once, in one place.

JSON in prod so a failure streak is queryable; a console renderer in dev so it is readable. Every
event is key-value (CLAUDE.md): one event per meaningful step, with the source, date and state
bound to it, because the sync dashboard and the alerting rules read fields, not prose.

Timestamps come from an injected `Clock` (B10), so a replayed run's log is as reproducible as its
journal, and a test can assert on an exact timestamp.

This module is `dataplatform.logging`, not the stdlib `logging`: imports inside the package are
absolute, so `import logging` anywhere still resolves to the standard library.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TextIO

import structlog
from structlog.typing import EventDict, FilteringBoundLogger, Processor, WrappedLogger

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import LogFormat, Settings, get_settings

__all__ = ["bind_context", "clear_context", "configure_logging", "get_logger", "log_context"]

#: A traceback frame's locals are live, unwrapped values — `SecretStr.get_secret_value()` called
#: two lines above a failure puts a plaintext credential right back into the very frame that
#: shows up in the traceback. `show_locals=True` is the default on both formatters below; setting
#: it False here is the one thing standing between that credential and a production log line
#: (invariant #13). `dataplatform.logging.SHOW_LOCALS_IN_TRACEBACKS` names the guard in one place
#: so a test can pin it and a future edit cannot silently flip it back.
SHOW_LOCALS_IN_TRACEBACKS = False

#: The JSON-mode exception renderer, built explicitly rather than using `structlog.processors.
#: dict_tracebacks` — that convenience object hardcodes `show_locals=True`.
_JSON_EXCEPTION_RENDERER: Processor = structlog.processors.ExceptionRenderer(
    structlog.tracebacks.ExceptionDictTransformer(show_locals=SHOW_LOCALS_IN_TRACEBACKS)
)

#: The console-mode exception formatter. `structlog.dev.ConsoleRenderer`'s own default
#: (`RichTracebackFormatter`) also hardcodes `show_locals=True` — dev logging is not exempt from
#: invariant #13 just because it is not JSON.
_CONSOLE_EXCEPTION_FORMATTER = structlog.dev.RichTracebackFormatter(
    show_locals=SHOW_LOCALS_IN_TRACEBACKS
)

#: Matches the password segment of a DSN (`scheme://user:PASSWORD@host`) so it can be blanked out
#: wherever a connection string reaches a log field despite never being logged deliberately.
_DSN_PASSWORD_RE = re.compile(r"(://[^\s:/@]+:)([^\s@]+)(@)")

#: Matches a Telegram Bot API token sitting in a URL path (`/bot<token>/...`), per
#: `TelegramConfig.send_message_url` — the shape invariant #13 calls out by name.
_TELEGRAM_TOKEN_IN_URL_RE = re.compile(r"(/bot)\d{8,10}:[A-Za-z0-9_-]{35}(/)")


def _redact_known_secret_shapes(value: Any) -> Any:
    """Blank out a DSN password or a Telegram bot token wherever one appears in a string value.

    A last line of defence, not the fix: the real fix is that no caller ever puts a secret in a
    log field or an exception string in the first place. This exists for the case a future caller
    gets that wrong anyway — recursing into dicts and lists so it also catches a rendered
    traceback's `exc_value` / `exc_notes` strings, not just top-level event fields.
    """
    if isinstance(value, str):
        value = _DSN_PASSWORD_RE.sub(r"\1***REDACTED***\3", value)
        value = _TELEGRAM_TOKEN_IN_URL_RE.sub(r"\1***REDACTED***\2", value)
        return value
    if isinstance(value, dict):
        return {key: _redact_known_secret_shapes(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_redact_known_secret_shapes(item) for item in value]
    return value


def _redaction_processor(logger: WrappedLogger, name: str, event_dict: EventDict) -> EventDict:
    """The redaction pass, wired in after tracebacks are rendered so it also covers them."""
    return {key: _redact_known_secret_shapes(val) for key, val in event_dict.items()}


@dataclass(frozen=True, slots=True)
class _ClockTimeStamper:
    """Stamp every event from the injected clock rather than from the host wall clock."""

    clock: Clock
    key: str = "timestamp"

    def __call__(self, logger: WrappedLogger, name: str, event_dict: EventDict) -> EventDict:
        event_dict[self.key] = self.clock.now().isoformat(timespec="milliseconds")
        return event_dict


def configure_logging(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    stream: TextIO | None = None,
) -> None:
    """Install the process-wide structlog configuration.

    What it does: picks the renderer from `settings.effective_log_format()`, filters at
    `settings.log_level`, and writes one line per event to `stream` (stderr by default).
    What it assumes: it is called once, early, by whatever owns the process — an entrypoint, a
    CLI, or a test. Calling it again reconfigures cleanly.
    What it never does: touch the stdlib root logger. Library logs (httpx, uvicorn) still go
    wherever their own configuration sends them until an entrypoint routes them here.
    """
    settings = get_settings() if settings is None else settings
    clock = SystemClock(settings.tzinfo) if clock is None else clock
    target = sys.stderr if stream is None else stream

    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        _ClockTimeStamper(clock),
        structlog.processors.StackInfoRenderer(),
    ]
    if settings.effective_log_format() is LogFormat.JSON:
        processors += [
            _JSON_EXCEPTION_RENDERER,
            _redaction_processor,
            structlog.processors.JSONRenderer(sort_keys=True),
        ]
    else:
        processors += [
            _redaction_processor,
            structlog.dev.ConsoleRenderer(
                colors=_is_a_terminal(target), exception_formatter=_CONSOLE_EXCEPTION_FORMATTER
            ),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            structlog.processors.NAME_TO_LEVEL[settings.log_level.lower()]
        ),
        logger_factory=structlog.WriteLoggerFactory(file=target),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None, **initial: Any) -> FilteringBoundLogger:
    """A logger, optionally pre-bound with context that every event from it should carry.

    Callers use this rather than `structlog.get_logger` so the platform keeps one import surface
    for logging, and so a future change of logging backend is one file's problem.
    """
    logger: FilteringBoundLogger = structlog.get_logger(name, **initial)
    return logger


def bind_context(**values: Any) -> None:
    """Bind key-values onto every subsequent event in this task/thread until cleared.

    For the facts a whole unit of work shares — `source`, `logical_date`, `run_id` — so each step
    does not repeat them and none of them can be forgotten halfway through.
    """
    structlog.contextvars.bind_contextvars(**values)


def clear_context() -> None:
    """Drop everything `bind_context` bound. Call between units of work."""
    structlog.contextvars.clear_contextvars()


@contextmanager
def log_context(**values: Any) -> Iterator[None]:
    """Scoped `bind_context`: the values are unbound again on the way out, exception or not."""
    with structlog.contextvars.bound_contextvars(**values):
        yield


def _is_a_terminal(stream: TextIO) -> bool:
    """Colour codes belong on a terminal and nowhere else — not in a file or a captured buffer."""
    try:
        return stream.isatty()
    except (AttributeError, ValueError):  # detached or closed stream
        return False
