"""M14.9 — early re-entry triggers for a parked momentum-v2 book (research variants on D13).

D13 (``PAPER_RATIFIED_2026_09_06``) reads the regime — the published NIFTY 50 TRI against its
200-session mean — only on the monthly rebalance session, so a book parked on a risk-off rebalance
waits for the next month even when the index turns two sessions later. M14.5 tested re-entering on
a plain daily level crossing and lost: most crossings reverse. M14.9 asks whether a trigger that
reads the *turn* rather than the level does better. The four triggers are pre-registered in
``ops/studies/preregistration-m14-9-momentum-turn-entry-2026-11-05.md``; their constants are fixed
here, once, and never searched:

* **confirmed cross** (V1) — the TRI has closed at or above its MA200 on :data:`CONFIRM_SESSIONS`
  consecutive sessions;
* **momentum turn** (V2, the owner's idea) — TRI >= :data:`TURN_PROXIMITY` x MA200, the
  :data:`TURN_MA_DAYS`-session mean above its value :data:`TURN_MA_LAG` sessions earlier, and the
  :data:`TURN_RETURN_SESSIONS`-session return positive;
* **scale-in** (V3) — the first close at or above MA200 deploys :data:`SCALE_IN_FRACTION` of the
  basket; the next monthly rebalance completes it (risk-on) or parks again (risk-off);
* **breadth turn** (V4) — the share of the run's PIT universe closing above its own
  :data:`BREADTH_MA_DAYS`-close mean is >= :data:`BREADTH_THRESHOLD` and higher than
  :data:`BREADTH_LAG` sessions earlier.

The policy (:class:`backtest.policies.momentum_v2.MomentumV2Policy`) consults a trigger only while
the book is parked and only off a rebalance session; exits stay D13's. The trigger predicates are
module functions so the gate report replays exactly the rule the policy ran.

Point-in-time (invariant #7): both readings carry the session close they were computed on and the
policy admits them through ``ctx.pit``; a source computes them from points knowable by then only.

What this module never does: read a wall clock, take a float, or decide anything itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from dataplatform.query.pit import Dataset

__all__ = [
    "BREADTH_LAG",
    "BREADTH_MA_DAYS",
    "BREADTH_THRESHOLD",
    "CONFIRM_SESSIONS",
    "SCALE_IN_FRACTION",
    "TURN_MA_DAYS",
    "TURN_MA_LAG",
    "TURN_PROXIMITY",
    "TURN_RETURN_SESSIONS",
    "BreadthData",
    "BreadthReading",
    "EarlyEntry",
    "RegimeTrendData",
    "RegimeTrendReading",
    "breadth_turn",
    "confirmed_cross",
    "momentum_turn",
]

_ZERO = Decimal("0")
_ONE = Decimal("1")

#: V1: consecutive closes at or above MA200 before a parked book re-enters.
CONFIRM_SESSIONS: Final = 5
#: V2: the TRI may still be below MA200, but by no more than 2 %.
TURN_PROXIMITY: Final = Decimal("0.98")
#: V2: the short moving average whose slope is read, and how many sessions back it is compared.
TURN_MA_DAYS: Final = 50
TURN_MA_LAG: Final = 10
#: V2: the TRI's own return over this many sessions must be positive.
TURN_RETURN_SESSIONS: Final = 20
#: V3: the share of the basket deployed at the first close at or above MA200.
SCALE_IN_FRACTION: Final = Decimal("0.5")
#: V4: the breadth threshold, the look-back it must exceed, and each name's own moving average.
BREADTH_THRESHOLD: Final = Decimal("0.5")
BREADTH_LAG: Final = 20
BREADTH_MA_DAYS: Final = 200


class EarlyEntry(StrEnum):
    """Which pre-registered trigger re-enters a parked book (M14.9)."""

    CONFIRMED_CROSS = "confirmed_cross"
    MOMENTUM_TURN = "momentum_turn"
    SCALE_IN = "scale_in"
    BREADTH_TURN = "breadth_turn"


@dataclass(frozen=True, slots=True)
class RegimeTrendReading:
    """The regime index's trend as of a session — what V1 and V2 read.

    * ``index_level`` / ``moving_average`` — the session's close and its regime MA (MA200), the
      same pair :class:`~backtest.policies.momentum_v2.RegimeReading` carries;
    * ``sessions_at_or_above`` — consecutive closes, ending with this one, at or above their own
      MA200 (0 when this close is below it);
    * ``short_average`` / ``short_average_lagged`` — the :data:`TURN_MA_DAYS`-close mean today and
      :data:`TURN_MA_LAG` published sessions earlier;
    * ``level_lagged`` — the close :data:`TURN_RETURN_SESSIONS` published sessions earlier.

    ``knowable_date`` is the session close all of it was computed on. Never a float.
    """

    index_level: Decimal
    moving_average: Decimal
    sessions_at_or_above: int
    short_average: Decimal
    short_average_lagged: Decimal
    level_lagged: Decimal
    knowable_date: date

    def __post_init__(self) -> None:
        for name in (
            "index_level",
            "moving_average",
            "short_average",
            "short_average_lagged",
            "level_lagged",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be a Decimal — never float (CLAUDE.md)")
            if value <= _ZERO:
                raise ValueError(f"{name} must be positive, got {value}")
        if self.sessions_at_or_above < 0:
            raise ValueError("sessions_at_or_above cannot be negative")
        if (self.sessions_at_or_above > 0) != (self.index_level >= self.moving_average):
            raise ValueError("the streak counts this close only when it is at or above its MA")


@dataclass(frozen=True, slots=True)
class BreadthReading:
    """The share of the PIT universe closing above its own moving average, today and lagged (V4).

    ``share_above`` is names above ÷ names counted on the session; ``share_above_lagged`` the same
    :data:`BREADTH_LAG` sessions earlier, on that session's own universe. ``names_counted`` is the
    session's denominator (reported, never a trigger input). ``knowable_date`` is the session.
    """

    share_above: Decimal
    share_above_lagged: Decimal
    names_counted: int
    knowable_date: date

    def __post_init__(self) -> None:
        for name in ("share_above", "share_above_lagged"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be a Decimal — never float (CLAUDE.md)")
            if not (_ZERO <= value <= _ONE):
                raise ValueError(f"{name} must be a share in [0, 1], got {value}")
        if self.names_counted <= 0:
            raise ValueError("a breadth reading needs at least one name counted")


@runtime_checkable
class RegimeTrendData(Protocol):
    """The seam V1 and V2 read: the regime index's trend as a one-element guardable dataset."""

    def regime_trend(self, as_of: date) -> Dataset[RegimeTrendReading]:
        """The trend reading as of ``as_of``."""


@runtime_checkable
class BreadthData(Protocol):
    """The seam V4 reads: the universe's breadth as a one-element guardable dataset."""

    def breadth(self, as_of: date) -> Dataset[BreadthReading]:
        """The breadth reading as of ``as_of``."""


def confirmed_cross(reading: RegimeTrendReading) -> bool:
    """V1: the TRI has closed at or above MA200 on :data:`CONFIRM_SESSIONS` straight sessions."""
    return reading.sessions_at_or_above >= CONFIRM_SESSIONS


def momentum_turn(reading: RegimeTrendReading) -> bool:
    """V2: near MA200 (within 2 %), MA50 rising over 10 sessions, 20-session return positive."""
    return (
        reading.index_level >= TURN_PROXIMITY * reading.moving_average
        and reading.short_average > reading.short_average_lagged
        and reading.index_level > reading.level_lagged
    )


def breadth_turn(reading: BreadthReading) -> bool:
    """V4: at least half the universe above its own MA200, and more of it than 20 sessions ago."""
    return (
        reading.share_above >= BREADTH_THRESHOLD
        and reading.share_above > reading.share_above_lagged
    )
