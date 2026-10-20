"""A10 · M17.16 — results dates, delivery share and volume net of matched deals, per name.

Pre-registration §8 Amendment 4. The dossier (`analyst.commons.dossier`) reads every field below
through these functions, so a definition lives in one place and its tests pin it.

**Results dates** (:func:`results_dates`), from NSE board-meeting intimations knowable by the
session (`dataplatform.ingest.nse.board_meetings`; an intimation disseminated at or after 20:00 IST
is knowable the next day). Only results meetings count (`is_results`):

- ``next_results_date``: the earliest meeting date after the session;
- ``last_results_date``: the latest meeting date on or before the session.

A company that moved a meeting keeps both intimations, so ``next_results_date`` is the earliest
date it has stated, which is the right one unless it postponed (Amendment 4 (a)).

**Delivery share** (:func:`delivery_average`): the arithmetic mean of the daily delivery
percentage (NSE `deliv_pct`, in percent) over the last :data:`DELIVERY_AVG_SESSIONS` sessions
(5 and 20), the session included; every one of those sessions must have printed with a delivery
figure, or the field is unknown.

**Results day** (:func:`results_day_index`): of the first session on or after the last results
meeting and the session after it, the one with the larger traded volume (the earlier on a tie).
Results land during or after a meeting, often after the close, so the reaction is on one of the
two; both must have printed by the session, or the field is unknown. Its delivery percentage is
``results_day_deliv_pct``.

**Matched round trips** (:func:`matched_round_trip_qty`): a bulk or block deal client that both
bought and sold the same name on the same session put ``2 x min(bought, sold)`` shares on the tape
without taking any home (the Swaraj Engines case of 2026-10-12: 70,956 bought and 70,956 sold by
one client). Clients are matched on their name, upper-cased and whitespace-collapsed. Deal
quantities are the exchange's raw shares; volumes in a dossier are adjusted, so a raw quantity is
put on the bar's basis by ``raw_close / close`` (1 on the session itself). Deals are known for a
session only when the deals read holds at least one deal of any name on it (:func:`deal_sessions`):
L1 ``deals`` starts on 2026-10-05, and a session before it is unknown, never zero.

- ``matched_deal_qty``: the session's matched quantity, raw shares;
- ``volume_ex_matched``: the session's volume less it;
- ``volume_ratio_day0_ex_matched``: that over ``volume_median_50`` (the 50 sessions before);
- ``results_day_volume_ratio`` and ``results_day_volume_ratio_ex_matched``: the results day's
  volume, gross and net of that day's matched quantity, over the median volume of the 50 sessions
  before it (all 50 present).

What it never does: read anything, look past the window's last bar, or treat a missing bar or an
uncaptured deals session as zero.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Final

from analyst.commons.inputs import BoardMeetingNotice, DealRecord, PriceBar

__all__ = [
    "DELIVERY_AVG_SESSIONS",
    "RESULTS_VOLUME_BASE_SESSIONS",
    "deal_sessions",
    "delivery_average",
    "matched_round_trip_qty",
    "raw_to_bar_quantity",
    "results_dates",
    "results_day_index",
    "volume_ratio",
]

#: The two delivery-share windows Amendment 4 pre-registers, in sessions.
DELIVERY_AVG_SESSIONS: Final[tuple[int, int]] = (5, 20)
#: The sessions before the results day whose median volume is the ratio's base.
RESULTS_VOLUME_BASE_SESSIONS: Final = 50

_CONTEXT: Final = Context(prec=28, rounding=ROUND_HALF_EVEN)
_Q: Final = Decimal("0.00000001")
_ZERO: Final = Decimal(0)
_SPACES: Final = re.compile(r"\s+")


def _q(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    out = value.quantize(_Q)
    return out.copy_abs() if out.is_zero() else out


def results_dates(
    notices: Iterable[BoardMeetingNotice], isin: str, session: date
) -> tuple[date | None, date | None]:
    """``(next_results_date, last_results_date)`` of ``isin`` as known on ``session``.

    Raises ``ValueError`` for a notice knowable after ``session``: the caller passes notices that
    crossed the PIT guard, and one that did not is a leak, not a value.
    """
    upcoming: list[date] = []
    held: list[date] = []
    for notice in notices:
        if notice.isin != isin or not notice.is_results:
            continue
        if notice.knowable_date > session:
            raise ValueError(
                f"{isin} intimation knowable {notice.knowable_date} reached session {session}"
            )
        (upcoming if notice.meeting_date > session else held).append(notice.meeting_date)
    return (min(upcoming) if upcoming else None, max(held) if held else None)


def delivery_average(window: Sequence[PriceBar | None], sessions: int) -> Decimal | None:
    """The mean daily delivery % over the window's last ``sessions`` bars, or ``None``."""
    if sessions <= 0 or len(window) < sessions:
        return None
    values = [b.deliv_pct for b in window[-sessions:] if b is not None]
    full = [v for v in values if v is not None]
    if len(full) != sessions:
        return None
    with localcontext(_CONTEXT):
        return _q(sum(full, _ZERO) / Decimal(sessions))


def results_day_index(
    window: Sequence[PriceBar | None], calendar: Sequence[date], meeting: date | None
) -> int | None:
    """The window index of the results day for a meeting on ``meeting`` (module docstring)."""
    if meeting is None or len(window) != len(calendar):
        return None
    first = next((i for i, day in enumerate(calendar) if day >= meeting), None)
    if first is None or first + 1 >= len(calendar):
        return None
    a, b = window[first], window[first + 1]
    if a is None or b is None:
        return None
    return first if a.volume >= b.volume else first + 1


def _client(name: str) -> str:
    return _SPACES.sub(" ", name.strip().upper())


def deal_sessions(deals: Iterable[DealRecord]) -> frozenset[date]:
    """The sessions the deals read covers: those with at least one deal of any name."""
    return frozenset(d.trade_date for d in deals)


def matched_round_trip_qty(deals: Iterable[DealRecord], isin: str, day: date) -> int:
    """Raw shares put on the tape by clients who both bought and sold ``isin`` on ``day``.

    Per client, ``2 x min(bought, sold)`` over that day's bulk and block deals; summed over
    clients. Only the matched part counts: a client that bought 100 and sold 40 contributes 80.
    """
    bought: dict[str, int] = defaultdict(int)
    sold: dict[str, int] = defaultdict(int)
    for deal in deals:
        if deal.isin != isin or deal.trade_date != day:
            continue
        side = deal.side.upper()
        if side == "BUY":
            bought[_client(deal.client_name)] += deal.quantity
        elif side == "SELL":
            sold[_client(deal.client_name)] += deal.quantity
    return sum(2 * min(qty, sold[client]) for client, qty in bought.items() if client in sold)


def raw_to_bar_quantity(raw_qty: int, bar: PriceBar) -> Decimal:
    """A raw share count on ``bar``'s (adjusted) volume basis: ``raw x raw_close / close``."""
    with localcontext(_CONTEXT):
        if bar.close <= _ZERO or bar.raw_close == bar.close:
            return Decimal(raw_qty)
        return Decimal(raw_qty) * bar.raw_close / bar.close


def volume_ratio(
    window: Sequence[PriceBar | None], index: int, less: Decimal = _ZERO
) -> Decimal | None:
    """``(volume[index] - less)`` over the median volume of the 50 bars before ``index``.

    ``None`` when any of those 50 bars is missing or the median is not positive. ``less`` is
    floored so the numerator is never negative.
    """
    bar = window[index] if 0 <= index < len(window) else None
    if bar is None or index < RESULTS_VOLUME_BASE_SESSIONS:
        return None
    before = window[index - RESULTS_VOLUME_BASE_SESSIONS : index]
    volumes = [b.volume for b in before if b is not None]
    if len(volumes) != RESULTS_VOLUME_BASE_SESSIONS:
        return None
    with localcontext(_CONTEXT):
        ordered = sorted(volumes)
        mid = len(ordered) // 2
        median = (ordered[mid - 1] + ordered[mid]) / Decimal(2)
        if median <= _ZERO:
            return None
        return _q(max(bar.volume - less, _ZERO) / median)
