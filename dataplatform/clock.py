"""Injected time (B10, invariant #11) — the one file in the repo that reads the wall clock.

Replay determinism (EXECUTION_PLAN §8.3.3) and point-in-time correctness (§8.3.6) both break the
moment a module decides "now" for itself: a replayed run picks a different today, and a decision
silently sees data that did not exist yet on the date it claims to be reasoning about. So every
component that needs the time takes a `Clock`. Production wires `SystemClock`; tests, the replay
engine and the backtest wire `FrozenClock`.

Conventions this module fixes for the whole codebase: a trading date is a `date` in the exchange's
timezone (Asia/Kolkata), a timestamp is always tz-aware, and a naive datetime is an error rather
than an assumption about the host's locale.

`tests/unit/test_clock_guard.py` enforces the "one file" part — it fails if any other file in the
repo calls a wall-clock function directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

#: The exchange timezone. Every trading date in this system is a calendar date here, never in UTC.
IST = ZoneInfo("Asia/Kolkata")

__all__ = ["IST", "Clock", "FrozenClock", "SystemClock"]


@runtime_checkable
class Clock(Protocol):
    """Everything a module is allowed to know about the current time.

    Deliberately tiny: a component that needs elapsed duration should measure it with
    `time.monotonic()` (an interval, not a date); a component that needs *the* date or instant
    takes one of these. Anything wider would be a second way to learn the wall clock.
    """

    @property
    def timezone(self) -> ZoneInfo:
        """The zone `now()` is expressed in, and that `today()` is a calendar date in."""

    def now(self) -> datetime:
        """The current instant, tz-aware in `timezone`. Never returns a naive datetime."""

    def today(self) -> date:
        """The current calendar date in `timezone` — the trading date, not the UTC date."""


@dataclass(frozen=True, slots=True)
class SystemClock:
    """The real clock. The only wall-clock reader in the codebase.

    What it does: reads the host clock and presents it in `timezone`.
    What it assumes: the host clock is roughly correct — nothing here corrects for drift.
    What it never does: appear in a backtest or a replay. Those inject `FrozenClock`, because a
    run whose output depends on when it ran is not reproducible.
    """

    timezone: ZoneInfo = IST

    def now(self) -> datetime:
        """The current instant, tz-aware in `timezone`."""
        return datetime.now(tz=self.timezone)

    def today(self) -> date:
        """The current calendar date in `timezone`."""
        return self.now().date()


class FrozenClock:
    """A clock stopped at one instant, moved only by an explicit call.

    What it does: returns exactly the instant it was set to, every time, so a function that reads
    the clock becomes a pure function of its inputs in a test or a replay.
    What it assumes: the instant is unambiguous — either a tz-aware datetime, or a bare `date`,
    which means midnight in `timezone` (so `today()` is exactly that date).
    What it never does: advance on its own. Wall-clock time passing changes nothing here.
    """

    def __init__(self, moment: datetime | date, *, timezone: ZoneInfo = IST) -> None:
        self.timezone = timezone
        self._moment = _as_aware(moment, timezone)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._moment.isoformat()!r})"

    def now(self) -> datetime:
        """The frozen instant, tz-aware in `timezone`."""
        return self._moment

    def today(self) -> date:
        """The calendar date of the frozen instant, in `timezone`."""
        return self._moment.date()

    def advance(self, delta: timedelta) -> None:
        """Move the frozen instant by `delta`; a negative delta moves it back."""
        self._moment += delta

    def freeze_at(self, moment: datetime | date) -> None:
        """Jump to a new instant, under the same rules as construction."""
        self._moment = _as_aware(moment, self.timezone)


def _as_aware(moment: datetime | date, timezone: ZoneInfo) -> datetime:
    """Normalize a caller-supplied instant to a tz-aware datetime in `timezone`.

    A naive datetime is rejected rather than assumed to be local time: guessing there is how a
    replay ends up an hour — and sometimes a trading date — away from the run it must reproduce.
    An aware datetime keeps its instant and is re-expressed in `timezone`.
    """
    # datetime is a subclass of date, so it must be tested first.
    if isinstance(moment, datetime):
        if moment.tzinfo is None:
            raise ValueError(
                f"FrozenClock needs an unambiguous instant, got naive {moment.isoformat()!r}; "
                f"pass a tz-aware datetime, or a date meaning midnight in {timezone.key}."
            )
        return moment.astimezone(timezone)
    return datetime.combine(moment, time.min, tzinfo=timezone)
