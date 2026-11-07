"""M14.9 V4 — market breadth: the share of a PIT universe closing above its own moving average.

The breadth-turn trigger (:func:`backtest.policies.early_entry.breadth_turn`) reads, for a session
``s``, the share of the run's investable universe whose close on ``s`` is strictly above the mean
of its own last :data:`~backtest.policies.early_entry.BREADTH_MA_DAYS` printed closes (``s``
included), and the same share :data:`~backtest.policies.early_entry.BREADTH_LAG` sessions earlier.
The definition is the pre-registered one
(``ops/studies/preregistration-m14-9-momentum-turn-entry-2026-11-05.md`` §3):

* **universe** — the run's own screen (same index and liquidity floor), as of the first session of
  ``s``'s month: the latest rebalance on or before ``s``. It is a callable here, so this module
  never reaches into the universe screen's internals;
* **closes** — the session's close per ISIN from the same source the momentum signal reads (L2
  adjusted over raw). A ratio of a close to a mean of closes in one share basis is unchanged by a
  later back-adjustment, so the series is point-in-time though L2 is struck on today's factors;
* **counted** — a universe name that printed on ``s`` and has ``MA_DAYS`` closes in the pass;
* **above** — ``close * MA_DAYS > sum of the last MA_DAYS closes`` (exact Decimal, no division).

**One forward pass.** :class:`BreadthSeries` walks the calendar once, from
:data:`PASS_LEAD_SESSIONS` sessions before the first session it must answer for, keeping each
name's last ``MA_DAYS`` closes and their running sum. A name with fewer than ``MA_DAYS`` prints in
that lead is not counted until it has them: the lead is long enough that only a name trading on
fewer than half the sessions is affected, and the liquidity floor screens those out anyway.

What it never does: read a close dated after the session it answers for, take a float, write
anywhere, or answer for a session outside the span it was built for (that raises).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Final

from backtest.policies.early_entry import BREADTH_LAG, BREADTH_MA_DAYS, BreadthReading
from dataplatform.logging import get_logger

__all__ = ["PASS_LEAD_SESSIONS", "BreadthError", "BreadthSeries"]

_LOG = get_logger(__name__)
_ZERO = Decimal("0")

#: Sessions the pass opens before the first session answered for — twice the moving average.
PASS_LEAD_SESSIONS: Final = 2 * BREADTH_MA_DAYS


class BreadthError(RuntimeError):
    """A breadth reading was asked for a session the series does not cover, or has no names."""


class BreadthSeries:
    """The breadth share for every session of ``[first, last]`` on ``calendar``, built on demand.

    ``closes(session)`` answers the session's close per ISIN; ``universe(rebalance)`` the
    investable ISINs as of a month's first session. Both are called only for sessions on or before
    the one being computed, in calendar order. ``first`` and ``last`` must be calendar sessions;
    the lagged share needs :data:`BREADTH_LAG` sessions before ``first``, so the pass covers them.
    """

    def __init__(
        self,
        *,
        calendar: Sequence[date],
        first: date,
        last: date,
        closes: Callable[[date], Mapping[str, Decimal]],
        universe: Callable[[date], frozenset[str] | set[str]],
        ma_days: int = BREADTH_MA_DAYS,
        lag: int = BREADTH_LAG,
    ) -> None:
        if first not in calendar or last not in calendar or last < first:
            raise BreadthError(f"breadth span {first}..{last} is not a calendar span")
        self._calendar = list(calendar)
        self._first = first
        self._last = last
        self._closes = closes
        self._universe = universe
        self._ma_days = ma_days
        self._lag = lag
        #: session -> (names above, names counted); filled by :meth:`_build`.
        self._counts: dict[date, tuple[int, int]] | None = None

    def reading(self, as_of: date) -> BreadthReading:
        """The breadth reading for ``as_of``: today's share and the share ``lag`` sessions back."""
        counts = self._built()
        index = self._index(as_of)
        if as_of < self._first or as_of > self._last:
            raise BreadthError(f"breadth was built for {self._first}..{self._last}, not {as_of}")
        lagged = self._calendar[index - self._lag]
        above, counted = counts[as_of]
        lag_above, lag_counted = counts[lagged]
        if counted == 0 or lag_counted == 0:
            raise BreadthError(f"no universe name has {self._ma_days} closes by {as_of}")
        return BreadthReading(
            share_above=Decimal(above) / Decimal(counted),
            share_above_lagged=Decimal(lag_above) / Decimal(lag_counted),
            names_counted=counted,
            knowable_date=as_of,
        )

    def _index(self, session: date) -> int:
        try:
            return self._calendar.index(session)
        except ValueError:
            raise BreadthError(f"{session} is not a session of the calendar") from None

    def _built(self) -> dict[date, tuple[int, int]]:
        if self._counts is None:
            self._counts = self._build()
        return self._counts

    def _build(self) -> dict[date, tuple[int, int]]:
        first_index = self._index(self._first)
        answer_from = first_index - self._lag
        if answer_from < 0:
            raise BreadthError(f"the calendar has no {self._lag} sessions before {self._first}")
        start = max(0, answer_from - PASS_LEAD_SESSIONS)
        end = self._index(self._last)
        window: dict[str, deque[Decimal]] = {}
        sums: dict[str, Decimal] = {}
        counts: dict[date, tuple[int, int]] = {}
        month_universe: tuple[tuple[int, int], frozenset[str]] | None = None
        month_first: dict[tuple[int, int], date] = {}
        for session in self._calendar:
            month_first.setdefault((session.year, session.month), session)
        for index in range(start, end + 1):
            session = self._calendar[index]
            closes = self._closes(session)
            for isin, close in closes.items():
                kept = window.get(isin)
                if kept is None:
                    kept = window[isin] = deque()
                    sums[isin] = _ZERO
                kept.append(close)
                sums[isin] += close
                if len(kept) > self._ma_days:
                    sums[isin] -= kept.popleft()
            if index < answer_from:
                continue
            key = (session.year, session.month)
            if month_universe is None or month_universe[0] != key:
                month_universe = (key, frozenset(self._universe(month_first[key])))
            above = counted = 0
            for isin in month_universe[1]:
                today = closes.get(isin)
                kept = window.get(isin)
                if today is None or kept is None or len(kept) < self._ma_days:
                    continue
                counted += 1
                if today * self._ma_days > sums[isin]:
                    above += 1
            counts[session] = (above, counted)
        _LOG.info(
            "breadth.built",
            first=self._first.isoformat(),
            last=self._last.isoformat(),
            sessions=len(counts),
            pass_from=self._calendar[start].isoformat(),
        )
        return counts
