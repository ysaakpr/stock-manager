"""The injected clock (B10, M0.2 acceptance criterion 2).

The point of `FrozenClock` is that a function which reads the time stops being a function of when
it ran. These tests assert that against literal expected values — something no wall-clock-reading
implementation could ever satisfy — and that the same function still moves when the clock does, so
the determinism is not just the function ignoring its clock.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from dataplatform.clock import IST, Clock, FrozenClock, SystemClock

CLOSE = datetime(2026, 8, 7, 15, 30, tzinfo=IST)  # a Friday, at the NSE close


def session_stamp(clock: Clock) -> str:
    """A deliberately time-dependent function, standing in for anything that reads the clock."""
    return f"{clock.today().isoformat()}@{clock.now().isoformat(timespec='seconds')}"


def test_frozen_clock_makes_a_time_dependent_function_deterministic() -> None:
    clock = FrozenClock(CLOSE)

    first = session_stamp(clock)
    second = session_stamp(clock)

    assert first == second == "2026-08-07@2026-08-07T15:30:00+05:30"


def test_the_same_function_still_moves_when_the_clock_does() -> None:
    """Determinism must come from the clock being frozen, not from the function ignoring it."""
    clock = FrozenClock(CLOSE)
    before = session_stamp(clock)

    clock.advance(timedelta(days=1, minutes=15))

    assert session_stamp(clock) == "2026-08-08@2026-08-08T15:45:00+05:30"
    assert session_stamp(clock) != before


def test_freeze_at_jumps_to_a_new_instant() -> None:
    clock = FrozenClock(CLOSE)
    clock.freeze_at(date(2021, 10, 21))

    assert clock.today() == date(2021, 10, 21)
    assert clock.now() == datetime(2021, 10, 21, 0, 0, tzinfo=IST)


def test_a_bare_date_freezes_at_midnight_in_the_clock_timezone() -> None:
    clock = FrozenClock(date(2026, 8, 7))

    assert clock.today() == date(2026, 8, 7)
    assert clock.now() == datetime(2026, 8, 7, 0, 0, tzinfo=IST)


def test_an_aware_instant_is_re_expressed_as_an_indian_trading_date() -> None:
    """20:00 UTC is already the next trading date in Kolkata — that is the date we must report."""
    clock = FrozenClock(datetime(2026, 8, 7, 20, 0, tzinfo=UTC))

    assert clock.now() == datetime(2026, 8, 8, 1, 30, tzinfo=IST)
    assert clock.today() == date(2026, 8, 8)


def test_a_naive_datetime_is_rejected_rather_than_assumed_local() -> None:
    with pytest.raises(ValueError, match="unambiguous instant"):
        # A naive datetime is exactly what this must refuse: guessing it is local time is how a
        # replay lands on the wrong trading date.
        FrozenClock(datetime(2026, 8, 7, 15, 30))


def test_system_clock_is_tz_aware_and_reports_the_indian_calendar_date() -> None:
    """Asserted without reading the wall clock here — the guard allows that in one file only."""
    clock = SystemClock()
    now = clock.now()

    assert clock.timezone is IST
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(hours=5, minutes=30)
    assert clock.today() == clock.now().date()


def test_both_clocks_satisfy_the_protocol() -> None:
    assert isinstance(SystemClock(), Clock)
    assert isinstance(FrozenClock(CLOSE), Clock)


def test_repr_names_the_frozen_instant() -> None:
    assert repr(FrozenClock(CLOSE)) == "FrozenClock('2026-08-07T15:30:00+05:30')"
