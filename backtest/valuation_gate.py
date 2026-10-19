"""PE timing: the market P/E against its own trailing mean, point-in-time, for the valuation gate.

``ops/studies/preregistration-pe-timing-2026-10-19.md`` fixes the signal before any run: on session
``t`` the gate reads NSE's published NIFTY 500 P/E for ``t`` and the simple mean of the last ``L``
published P/Es knowable by ``t`` (``t``'s own included), and calls the market *cheap* when the P/E
is strictly below that mean. The figures are NSE's daily close-all snapshot, landed in
``L1/macro_series`` by :mod:`dataplatform.ingest.macro.index_valuation` with ``release_date`` the
session itself (knowable that evening) — the same convention as the index close D13's price regime
reads, so a decision on ``t`` may use the P/E through ``t`` and nothing later.

**The methodology break.** NSE moved index P/E from standalone to consolidated earnings with
effect from the 2021-03-31 snapshot. The NIFTY 500 P/E fell from 43.33 to 35.89 that session
(log change -0.188) while the index closed -0.6 %; the NIFTY 50 P/E fell by the same proportion the
same day. Read raw, every trailing mean that straddles the date is inflated by pre-break points and
the gate reads *cheap* for up to ``L`` sessions after it whatever the market did. The *spliced*
reading rescales, for a session on or after the break, each window point dated before it by the
implied-earnings ratio across the break,

    k = (close(b-1) / PE(b-1)) / (close(b) / PE(b)),

with ``b`` the break session and ``b-1`` the last P/E session before it. ``k`` uses only the two
sessions' published figures, both knowable on ``b``; a session before ``b`` is never rescaled. A
constant rescaling of a window wholly on one side of the break cannot change the comparison, so the
two readings differ only on sessions within ``L`` points after ``b``.

What it never does: fetch, write, read a wall clock, fill a missing P/E from a neighbour, or let a
point with a ``release_date`` after the session reach the session's reading (invariant #7).
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Final

from backtest.policies.momentum_v2 import ValuationReading
from dataplatform.logging import get_logger
from dataplatform.store.macro_series import series_history

__all__ = [
    "CLOSE_SERIES",
    "PE_METHODOLOGY_BREAK",
    "PE_SERIES",
    "PePoint",
    "ValuationSource",
    "ValuationSourceError",
    "load_pe_points",
    "splice_factor",
]

_LOG = get_logger(__name__)

#: The gate's series: NSE's published NIFTY 500 P/E (``IN.NSE.NIFTY_500.PE``, a pure multiple).
PE_SERIES: Final = "IN.NSE.NIFTY_500.PE"
#: The same index's close, read only for the splice's implied-earnings ratio.
CLOSE_SERIES: Final = "IN.NSE.NIFTY_500.CLOSE"
#: The first snapshot on consolidated earnings (standalone before it) — read off the data: the one
#: session where both NIFTY 50 and NIFTY 500 P/E fell ~17 % on a flat index.
PE_METHODOLOGY_BREAK: Final = date(2021, 3, 31)

_ZERO: Final = Decimal("0")


class ValuationSourceError(RuntimeError):
    """The P/E series cannot serve a reading as asked. Fails loud, never falls back."""


@dataclass(frozen=True, slots=True)
class PePoint:
    """One published figure: the session it describes, when it became knowable, and its value."""

    session: date
    knowable: date
    value: Decimal


@lru_cache(maxsize=8)
def _cached_points(series_id: str, through: date, data_root: Path | None) -> tuple[PePoint, ...]:
    facts = series_history(series_id, through, data_root=data_root)
    return tuple(PePoint(f.period_end, f.release_date, f.value) for f in facts)


def load_pe_points(
    series_id: str, *, through: date, data_root: Path | None = None
) -> tuple[PePoint, ...]:
    """``series_id`` as it stood on ``through``, oldest session first (each at its latest release).

    Cached per process: a sweep runs many arms over one window, and the store read is the slow part.
    Raises ``ValuationSourceError`` when the series has nothing by ``through``.
    """
    points = _cached_points(series_id, through, data_root)
    if not points:
        raise ValuationSourceError(
            f"no {series_id!r} in L1/macro_series through {through.isoformat()}: the valuation "
            "gate has no fallback (land it with dataplatform.ingest.macro.index_valuation)"
        )
    return points


def splice_factor(
    pe: Mapping[date, Decimal], close: Mapping[date, Decimal], *, break_session: date
) -> Decimal:
    """``k`` — the implied-earnings ratio across ``break_session`` (see the module docstring).

    Multiplying a pre-break P/E by ``k`` restates it on the post-break earnings basis. Raises
    ``ValuationSourceError`` when either session's P/E or close is missing.
    """
    before = [d for d in pe if d < break_session]
    if not before or break_session not in pe:
        raise ValuationSourceError(f"no P/E on both sides of {break_session.isoformat()}")
    prior = max(before)
    for day in (prior, break_session):
        if day not in close:
            raise ValuationSourceError(f"no index close on {day.isoformat()} for the splice")
    earnings_before = close[prior] / pe[prior]
    earnings_after = close[break_session] / pe[break_session]
    return earnings_before / earnings_after


class ValuationSource:
    """The valuation gate's reading per session: P/E against its trailing ``mean_days`` mean.

    ``points`` are the P/E series oldest first; ``factor`` is the splice ``k`` (``None``: raw).
    A reading needs a P/E dated the session itself and ``mean_days`` points knowable by it; either
    missing raises :class:`ValuationSourceError` — a stale figure is never carried.
    """

    def __init__(
        self,
        points: Sequence[PePoint],
        *,
        mean_days: int,
        factor: Decimal | None = None,
        break_session: date = PE_METHODOLOGY_BREAK,
    ) -> None:
        if mean_days <= 0:
            raise ValueError(f"mean_days must be positive, got {mean_days}")
        if factor is not None and factor <= _ZERO:
            raise ValueError(f"splice factor must be positive, got {factor}")
        ordered = sorted(points, key=lambda p: (p.knowable, p.session))
        self._mean_days = mean_days
        self._factor = factor
        self._break = break_session
        self._knowable = [p.knowable for p in ordered]
        self._points = ordered
        self._by_session = {p.session: p.value for p in ordered}

    @classmethod
    def published(
        cls,
        *,
        through: date,
        mean_days: int,
        splice: bool,
        data_root: Path | None = None,
    ) -> ValuationSource:
        """The NIFTY 500 P/E through ``through``, raw or spliced at :data:`PE_METHODOLOGY_BREAK`."""
        points = load_pe_points(PE_SERIES, through=through, data_root=data_root)
        factor: Decimal | None = None
        if splice and through >= PE_METHODOLOGY_BREAK:
            closes = load_pe_points(CLOSE_SERIES, through=through, data_root=data_root)
            factor = splice_factor(
                {p.session: p.value for p in points},
                {p.session: p.value for p in closes},
                break_session=PE_METHODOLOGY_BREAK,
            )
        _LOG.info(
            "backtest.valuation_source",
            series=PE_SERIES,
            points=len(points),
            first_point=points[0].session.isoformat(),
            last_point=points[-1].session.isoformat(),
            mean_days=mean_days,
            splice=splice,
            factor=None if factor is None else str(factor),
        )
        return cls(points, mean_days=mean_days, factor=factor)

    @property
    def spliced(self) -> bool:
        return self._factor is not None

    @property
    def first_full_session(self) -> date | None:
        """The first session with ``mean_days`` points knowable — the earliest possible reading."""
        if len(self._points) < self._mean_days:
            return None
        return self._knowable[self._mean_days - 1]

    def has(self, session: date) -> bool:
        """Whether a P/E is published for ``session``."""
        return session in self._by_session

    def reading(self, as_of: date) -> ValuationReading:
        """The P/E on ``as_of`` and its trailing mean over points knowable by ``as_of``."""
        current = self._by_session.get(as_of)
        if current is None:
            raise ValuationSourceError(
                f"{PE_SERIES} has no P/E for {as_of.isoformat()}; the valuation gate does not "
                "carry a stale figure"
            )
        cutoff = bisect_right(self._knowable, as_of)
        window = self._points[max(0, cutoff - self._mean_days) : cutoff]
        if len(window) < self._mean_days:
            raise ValuationSourceError(
                f"{PE_SERIES} has {len(window)} points knowable by {as_of.isoformat()}, short of "
                f"the {self._mean_days}-session mean"
            )
        factor = self._factor if as_of >= self._break else None
        values = [
            p.value * factor if factor is not None and p.session < self._break else p.value
            for p in window
        ]
        mean = sum(values, _ZERO) / Decimal(len(values))
        return ValuationReading(
            pe=current,
            trailing_mean=mean,
            mean_days=self._mean_days,
            knowable_date=as_of,
            spliced=factor is not None and window[0].session < self._break,
        )
