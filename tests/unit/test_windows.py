"""X2 — the campaign windows: configured, and every first decision has its full lookback.

* the full-span start is the first session on which the longest lookback any arm reads lies wholly
  in ISIN-joinable history — and the 260-print swing minimum is what binds it;
* sessions before the joinable start (the quarantined, ISIN-less 2006-2011 era) never count toward
  a lookback — a calendar that counted them would open the window a year early;
* a window opening before that session is refused, as is a full window opening after it;
* the walk-forward is the full span cut into two equal session counts, selection first;
* the checked-in configuration carries the dates the task fixed.

Offline: a synthetic weekday calendar stands in for the lake's.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from backtest.windows import (
    LOOKBACKS,
    CampaignWindows,
    Window,
    WindowError,
    first_full_lookback_session,
    load_windows,
    split_in_half,
    verify_windows,
)

_JOINABLE = date(2011, 6, 22)


def _weekdays(start: date, end: date) -> list[date]:
    """Weekdays less every 20th — about the thirteen exchange holidays an NSE year closes on.

    The holidays matter: without them 260 sessions fit inside 365 days and the calendar-day
    lookbacks would bind instead, which is not the lake's calendar.
    """
    days, day, weekday = [], start, 0
    while day <= end:
        if day.weekday() < 5:
            weekday += 1
            if weekday % 20:
                days.append(day)
        day += timedelta(days=1)
    return days


_SESSIONS = _weekdays(_JOINABLE, date(2026, 9, 1))


def test_the_swing_minimum_history_binds_the_full_span_start() -> None:
    first, binding = first_full_lookback_session(_SESSIONS, _JOINABLE)
    assert first == _SESSIONS[259]  # the 260th session: a name cannot have more prints than that
    assert "260" in binding.what
    # Every other lookback is satisfied on or before it — which is what "binds" means.
    for lookback in LOOKBACKS:
        at = lookback.first_session(_SESSIONS, _JOINABLE)
        assert at is not None and at <= first, lookback.what


def test_quarantined_sessions_never_count_toward_a_lookback() -> None:
    with_quarantine = _weekdays(date(2006, 1, 2), date(2011, 6, 21)) + _SESSIONS
    assert first_full_lookback_session(with_quarantine, _JOINABLE) == (
        first_full_lookback_session(_SESSIONS, _JOINABLE)
    )


def test_a_calendar_day_lookback_needs_its_first_day_in_joinable_history() -> None:
    (liquidity,) = [lb for lb in LOOKBACKS if "liquidity" in lb.what]
    at = liquidity.first_session(_SESSIONS, _JOINABLE)
    assert at is not None
    assert at - timedelta(days=365) >= _JOINABLE
    earlier = [s for s in _SESSIONS if s < at][-1]
    assert earlier - timedelta(days=365) < _JOINABLE


def _windows(full_start: date) -> CampaignWindows:
    full = Window("full", full_start, date(2026, 8, 31))
    selection, verification = split_in_half(full, _SESSIONS)
    return CampaignWindows(
        joinable_from=_JOINABLE,
        sweeps=(full, Window("decade", date(2016, 9, 2), date(2026, 8, 31))),
        selection=selection,
        verification=verification,
    )


def test_a_full_window_opening_before_its_lookback_has_filled_is_refused() -> None:
    first, _ = first_full_lookback_session(_SESSIONS, _JOINABLE)
    verify_windows(_windows(first), _SESSIONS)
    early = _SESSIONS[_SESSIONS.index(first) - 1]
    with pytest.raises(WindowError, match="first full-lookback session"):
        verify_windows(_windows(early), _SESSIONS)
    late = _SESSIONS[_SESSIONS.index(first) + 1]
    with pytest.raises(WindowError, match=r"set full\.start"):
        verify_windows(_windows(late), _SESSIONS)


def test_any_window_opening_before_the_lookback_is_refused() -> None:
    first, _ = first_full_lookback_session(_SESSIONS, _JOINABLE)
    base = _windows(first)
    bad = CampaignWindows(
        joinable_from=_JOINABLE,
        sweeps=(*base.sweeps, Window("too-early", date(2012, 1, 2), date(2026, 8, 31))),
        selection=base.selection,
        verification=base.verification,
    )
    with pytest.raises(WindowError, match="too-early"):
        verify_windows(bad, _SESSIONS)


def test_the_walk_forward_is_two_equal_session_halves_selection_first() -> None:
    full = Window("full", date(2012, 7, 4), date(2026, 8, 31))
    selection, verification = split_in_half(full, _SESSIONS)
    inside = [s for s in _SESSIONS if full.start <= s <= full.end]
    left = [s for s in inside if s <= selection.end]
    right = [s for s in inside if s >= verification.start]
    assert selection.start == inside[0] and verification.end == inside[-1]
    assert selection.end < verification.start
    assert len(left) + len(right) == len(inside)
    assert len(right) - len(left) in (0, 1)  # an odd count leaves the extra session in the second


def test_the_checked_in_configuration_carries_the_task_windows() -> None:
    windows = load_windows()
    assert windows.joinable_from == date(2011, 6, 22)
    assert (windows.named("full").start, windows.named("full").end) == (
        date(2012, 7, 4),
        date(2026, 8, 31),
    )
    assert (windows.named("decade").start, windows.named("decade").end) == (
        date(2016, 9, 2),
        date(2026, 8, 31),
    )
    assert (windows.named("six-year").start, windows.named("six-year").end) == (
        date(2019, 7, 1),
        date(2026, 8, 31),
    )
    assert windows.selection.start == windows.named("full").start
    assert windows.verification.end == windows.named("full").end
    assert windows.selection.end < windows.verification.start
