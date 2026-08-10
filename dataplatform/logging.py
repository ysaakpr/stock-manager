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

import logging
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, TextIO

import structlog
from pydantic import BaseModel
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

#: The transformer `_JSON_EXCEPTION_RENDERER` wraps, kept as its own name (rather than inlined)
#: so a test can assert `.show_locals` on a concretely-typed object — `ExceptionRenderer` declares
#: its wrapped callable as the abstract `ExceptionTransformer`, which does not expose it.
_JSON_EXCEPTION_TRANSFORMER = structlog.tracebacks.ExceptionDictTransformer(
    show_locals=SHOW_LOCALS_IN_TRACEBACKS
)

#: The JSON-mode exception renderer, built explicitly rather than using `structlog.processors.
#: dict_tracebacks` — that convenience object hardcodes `show_locals=True`.
_JSON_EXCEPTION_RENDERER = structlog.processors.ExceptionRenderer(_JSON_EXCEPTION_TRANSFORMER)

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
#: `TelegramConfig.send_message_url` — the shape invariant #13 calls out by name. Bot ids already
#: reach 10 digits and a token body of 36+ chars is observed in the wild, so both ends are open
#: rather than the exact `{8,10}`/`{35}` this repo shipped with first, which a longer real token
#: or bot id would silently pass through.
_TELEGRAM_TOKEN_IN_URL_RE = re.compile(r"(/bot)\d{5,}:[\w-]{20,}")

#: Anthropic API keys (`sk-ant-api03-...`, `console.anthropic.com` → Settings → API Keys) — M6.8
#: has not landed yet, so nothing calls `get_secret_value()` on `anthropic_api_key` today, but the
#: day it does, this pattern is already in place rather than a gap discovered after the fact.
_ANTHROPIC_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")

#: An AWS access key ID (never the secret access key, which has no fixed shape at all — see
#: `_SENSITIVE_KEY_NAME_MARKERS` below for how that half is covered). Not used anywhere in this
#: platform's own credentials; kept because a future dependency (an S3-backed L0 target is an open
#: item in ops/BACKLOG.md) would otherwise add this exact shape with zero coverage.
_AWS_ACCESS_KEY_ID_RE = re.compile(r"AKIA[0-9A-Z]{16}")

#: Field-name substrings that make a value's entire content suspect regardless of its shape.
#: Exists because SMTP and Kite credentials — the two credential types this repo's own audit named
#: as having zero shape-based coverage — have no recognizable format at all: an SMTP password is
#: an arbitrary string, and a Kite api_key/api_secret is a plain alphanumeric string with no fixed
#: prefix (unlike Anthropic's `sk-ant-` or a Telegram token's `\d+:` shape). Nothing about the
#: VALUE can be pattern-matched for those two; only the NAME a value is logged under can be, which
#: is why this list exists as a second, complementary mechanism rather than more regexes.
_SENSITIVE_KEY_NAME_MARKERS = ("password", "secret", "token", "api_key", "apikey")


def _is_sensitive_key_name(key: object) -> bool:
    lowered = str(key).lower()
    return any(marker in lowered for marker in _SENSITIVE_KEY_NAME_MARKERS)


def _redact_known_secret_shapes(value: Any) -> Any:
    """Blank out a value whose shape or field name is a known credential, wherever it appears.

    A last line of defence, not the fix: the real fix is that no caller ever puts a secret in a
    log field or an exception string in the first place. This exists for the case a future caller
    gets that wrong anyway — recursing into dicts, lists, tuples and sets so it also catches a
    rendered traceback's `exc_value` / `exc_notes` strings and any of the container shapes a
    caller might collect several DSNs or tokens into, not just a single top-level string field.

    A dict's *values* are redacted by name where the key is sensitive (whole-value blanking,
    since a shapeless credential has nothing else to match on) and by shape everywhere else.

    A dataclass or a pydantic `BaseModel` is redacted by field name via its own fields, not its
    `repr()` text directly: `repr()` renders as `ClassName(field='value', ...)`, which the shape
    regexes above cannot parse as key-value pairs, so a bare `str` field with no recognisable
    shape (a plain settings-carrying object's password field — `SecretStr` masks its own `repr()`
    already, but a *bare* `str`/`int` field on the same object would not) would survive a naive
    `repr()`-then-regex pass untouched. Converting to a dict first (`dataclasses.asdict` /
    `model_dump(mode="json")`) reuses the dict branch's key-name check, then folds back into a
    single string — the one non-primitive shape worth the small behaviour change of turning the
    field into text. Every other object type (`Decimal`, `date`, `UUID`, an `Enum`...) is returned
    unchanged: those are exactly the value types this platform's own logging convention relies on
    structlog/`JSONRenderer` to serialise cleanly, and coercing all of them through `repr()` on the
    chance one might carry a credential would degrade every ordinary log line's field shapes to
    catch a case that, for this platform's own types, does not occur.
    """
    if isinstance(value, str):
        value = _DSN_PASSWORD_RE.sub(r"\1***REDACTED***\3", value)
        value = _TELEGRAM_TOKEN_IN_URL_RE.sub(r"\1***REDACTED***", value)
        value = _ANTHROPIC_KEY_RE.sub("sk-ant-***REDACTED***", value)
        value = _AWS_ACCESS_KEY_ID_RE.sub("AKIA***REDACTED***", value)
        return value
    if isinstance(value, dict):
        return {
            key: (
                "***REDACTED***"
                if _is_sensitive_key_name(key)
                else _redact_known_secret_shapes(val)
            )
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_redact_known_secret_shapes(item) for item in value]
    if isinstance(value, (tuple, set, frozenset)):
        return type(value)(_redact_known_secret_shapes(item) for item in value)
    if isinstance(value, BaseModel):
        fields = _redact_known_secret_shapes(value.model_dump(mode="json"))
        return f"{type(value).__name__}({fields})"
    if is_dataclass(value) and not isinstance(value, type):
        fields = _redact_known_secret_shapes(asdict(value))
        return f"{type(value).__name__}({fields})"
    return value


def _redaction_processor(logger: WrappedLogger, name: str, event_dict: EventDict) -> EventDict:
    """The redaction pass, wired in after tracebacks are rendered so it also covers them."""
    return {
        key: ("***REDACTED***" if _is_sensitive_key_name(key) else _redact_known_secret_shapes(val))
        for key, val in event_dict.items()
    }


class _HttpxUrlRedactor(logging.Filter):
    """httpx logs every request's URL at INFO — `HTTP Request: POST <url> "HTTP/1.1 ..."` — on
    its own stdlib `logging.getLogger("httpx")`, entirely outside structlog. `TelegramConfig.
    send_message_url` puts the bot token in that URL's path, and nothing about the redaction
    processors above reaches this: they run inside structlog's own pipeline, which this record
    never enters unless something later routes stdlib logging through it.

    Today that "something" does not exist (`configure_logging` never touches the stdlib root
    logger — ops/BACKLOG.md's M0.2 entry names the gap and routes the real fix to M0.5's
    entrypoint work), which is exactly why this cannot wait for M0.5: the httpx logger already
    has its own default level (`WARNING`) and no handler, so the token is silent only by
    accident — the first `logging.basicConfig(level=logging.INFO)` anywhere in the process (a
    test, a REPL, a future entrypoint) arms it. Verified with a real `httpx.post` against a
    fake-token URL: unfiltered, the token appears in the stdlib log line; filtered, it does not.

    A filter on the named logger, not a raised level: raising `httpx`'s level to hide this one
    URL shape would also hide its INFO line for every *other* source this platform fetches from,
    which is a real observability cost this rule does not need to pay.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = (
            _redact_known_secret_shapes(record.msg) if isinstance(record.msg, str) else record.msg
        )
        if isinstance(record.args, tuple):
            record.args = tuple(self._redact_arg(arg) for arg in record.args)
        elif record.args:
            record.args = self._redact_arg(record.args)
        return True

    @staticmethod
    def _redact_arg(arg: Any) -> Any:
        """Redact whatever `arg` stringifies to — httpx passes the request URL as its own
        `httpx.URL` object, not a `str`, and it is only converted to text when the record is
        finally formatted (by `%s % args`, well after any filter has run). Checking `isinstance
        (arg, str)` alone misses it entirely: the object survives the filter unredacted, and
        formatting stringifies the *original* URL. Only replaces `arg` when redaction actually
        changed something — a numeric arg (`%d` status code) must stay numeric, or formatting a
        record whose format string expects a number breaks.
        """
        original = str(arg)
        redacted = _redact_known_secret_shapes(original)
        return redacted if redacted != original else arg


def _install_httpx_url_redaction() -> None:
    """Attach `_HttpxUrlRedactor` to the `httpx` logger exactly once per process.

    Idempotent by class-identity check rather than a module-level flag: `configure_logging` is
    documented to be callable more than once (tests do it per case), and `Logger.addFilter`
    itself is not idempotent — calling it twice would redact twice, which is harmless for the
    regex but pointless and would multiply across every test in a suite.
    """
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(f, _HttpxUrlRedactor) for f in httpx_logger.filters):
        httpx_logger.addFilter(_HttpxUrlRedactor())


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
    What it never does: touch the stdlib root logger, or route library logs (httpx, uvicorn)
    through structlog — they still go wherever their own configuration sends them until an
    entrypoint does that (M0.5; ops/BACKLOG.md). The one exception is `_install_httpx_url_
    redaction`, a narrow filter on the named `httpx` logger that redacts a Telegram bot token
    from its own INFO line regardless of level or handler — a security containment, not routing.
    """
    settings = get_settings() if settings is None else settings
    clock = SystemClock(settings.tzinfo) if clock is None else clock
    target = sys.stderr if stream is None else stream
    _install_httpx_url_redaction()

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
