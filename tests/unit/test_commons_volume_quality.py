"""Amendment 4 (M17.16) definitions: results dates, delivery share, matched round trips."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from analyst.commons.inputs import BoardMeetingNotice, DealRecord, PriceBar
from analyst.commons.volume_quality import (
    deal_sessions,
    delivery_average,
    matched_round_trip_qty,
    raw_to_bar_quantity,
    results_dates,
    results_day_index,
    volume_ratio,
)

ISIN = "INE277A01016"
DAYS = [date(2026, 7, 1) + timedelta(days=k) for k in range(80)]


def _bar(day: date, volume: int, deliv: str | None = "40", raw: str = "100") -> PriceBar:
    return PriceBar(
        isin=ISIN,
        trade_date=day,
        high=Decimal(101),
        low=Decimal(99),
        close=Decimal(100),
        volume=Decimal(volume),
        raw_close=Decimal(raw),
        traded_value=Decimal(100 * volume),
        size=Decimal(100 * volume),
        deliv_qty=None,
        deliv_pct=None if deliv is None else Decimal(deliv),
    )


def _notice(meeting: date, knowable: date, *, results: bool = True) -> BoardMeetingNotice:
    return BoardMeetingNotice(ISIN, meeting, "Financial Results", None, results, knowable)


def _deal(day: date, side: str, qty: int, client: str = "BLITZQUANT RESEARCH LLP") -> DealRecord:
    return DealRecord(ISIN, "BULK", day, client, side, qty, Decimal("3828.18"))


def test_results_dates_split_on_the_session_and_ignore_other_meetings() -> None:
    session = date(2026, 10, 19)
    notices = [
        _notice(date(2026, 7, 24), date(2026, 7, 10)),
        _notice(date(2026, 10, 10), date(2026, 9, 30)),
        _notice(date(2026, 10, 29), date(2026, 10, 6)),
        _notice(date(2026, 11, 12), date(2026, 10, 15)),
        _notice(date(2026, 10, 21), date(2026, 10, 1), results=False),
    ]
    assert results_dates(notices, ISIN, session) == (date(2026, 10, 29), date(2026, 10, 10))
    # A meeting on the session itself is the last results date, not the next.
    assert results_dates(notices, ISIN, date(2026, 10, 29)) == (
        date(2026, 11, 12),
        date(2026, 10, 29),
    )
    assert results_dates(notices, "INE000000000", session) == (None, None)


def test_results_dates_refuse_an_intimation_knowable_after_the_session() -> None:
    with pytest.raises(ValueError, match="knowable"):
        results_dates([_notice(date(2026, 11, 1), date(2026, 10, 20))], ISIN, date(2026, 10, 19))


def test_delivery_average_is_the_mean_of_complete_windows_only() -> None:
    window: list[PriceBar | None] = [_bar(d, 1000, str(k)) for k, d in enumerate(DAYS[:20])]
    assert delivery_average(window, 5) == Decimal("17")  # mean of 15..19
    assert delivery_average(window, 20) == Decimal("9.5")
    window[-3] = None
    assert delivery_average(window, 5) is None
    window[-3] = _bar(DAYS[17], 1000, None)
    assert delivery_average(window, 5) is None
    assert delivery_average(window[:4], 5) is None


def test_the_results_day_is_the_heavier_of_the_meeting_session_and_the_next() -> None:
    window: list[PriceBar | None] = [_bar(d, 1000) for d in DAYS[:10]]
    window[4] = _bar(DAYS[4], 5000)  # the session after the meeting carries the reaction
    assert results_day_index(window, DAYS[:10], DAYS[3]) == 4
    window[3] = _bar(DAYS[3], 9000)  # results in market hours: the meeting day itself
    assert results_day_index(window, DAYS[:10], DAYS[3]) == 3
    # A meeting on a non-session (a Saturday) starts from the first session after it.
    sessions = [*DAYS[:3], *DAYS[5:12]]
    bars: list[PriceBar | None] = [_bar(d, 1000) for d in sessions]
    bars[4] = _bar(sessions[4], 3000)
    assert results_day_index(bars, sessions, DAYS[3]) == 4
    # The next session has not printed yet: unknown.
    assert results_day_index(window, DAYS[:10], DAYS[9]) is None
    assert results_day_index(window, DAYS[:10], None) is None


def test_a_matched_round_trip_counts_both_legs_and_only_the_matched_part() -> None:
    """Swaraj Engines, 2026-10-12: 70,956 bought and 70,956 sold by one client."""
    day = date(2026, 10, 12)
    deals = [_deal(day, "BUY", 70_956), _deal(day, "SELL", 70_956)]
    assert matched_round_trip_qty(deals, ISIN, day) == 141_912  # fails if one side is counted
    # Only the matched part, per client; spelling differences in the name do not split a client.
    deals = [
        _deal(day, "BUY", 100),
        _deal(day, "SELL", 40, client="blitzquant  research llp "),
        _deal(day, "BUY", 500, client="SOMEONE ELSE"),
        _deal(day - timedelta(days=1), "SELL", 100),
    ]
    assert matched_round_trip_qty(deals, ISIN, day) == 80
    assert deal_sessions(deals) == {day, day - timedelta(days=1)}


def test_volume_net_of_matched_trades_over_the_fifty_session_median() -> None:
    window: list[PriceBar | None] = [_bar(d, 6_585) for d in DAYS[:51]]
    window[50] = _bar(DAYS[50], 1_409_494)
    gross = volume_ratio(window, 50)
    net = volume_ratio(window, 50, Decimal(141_912))
    assert gross is not None and net is not None
    assert gross == (Decimal(1_409_494) / Decimal(6_585)).quantize(Decimal("0.00000001"))
    assert net == (Decimal(1_267_582) / Decimal(6_585)).quantize(Decimal("0.00000001"))
    assert net < gross
    window[10] = None
    assert volume_ratio(window, 50) is None  # a missing base session is unknown


def test_a_raw_quantity_takes_the_bars_adjustment() -> None:
    # A 1:5 split after the bar: adjusted close 100 on a raw close of 500, so volume x5.
    bar = _bar(DAYS[0], 5_000, raw="500")
    assert raw_to_bar_quantity(1_000, bar) == Decimal(5_000)
    assert raw_to_bar_quantity(1_000, _bar(DAYS[0], 5_000)) == Decimal(1_000)
