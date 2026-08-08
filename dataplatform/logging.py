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
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(sort_keys=True),
        ]
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=_is_a_terminal(target)))

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
