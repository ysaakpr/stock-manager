"""A10 · M17.9 — the three-state market regime of study §4, computed, given to every manager.

The regime is information for the manager, never a rail. Its rule, on NIFTY 500 levels:

- **RISK-OFF** when the index is below its SMA200 **and** either its 24-month return is negative
  or its 126-session realised volatility is in the top 20 % of its own history;
- **RISK-ON** when the index is above a *rising* SMA200 and at least 50 % of the universe closes
  above its own SMA50;
- **NEUTRAL** otherwise.

RISK-OFF is tested first, so a market that is both below its mean and volatile is never
RISK-ON. Definitions, all on the index's own published sessions:

- SMA200 is the mean of the last 200 levels; it is *rising* when it is above the SMA200 struck
  21 levels earlier (the S1 rule's 21 sessions).
- The 24-month return runs from the last level on or before the same calendar date two years
  earlier to the latest level.
- Realised volatility is the sample stdev of the last 126 daily returns. Its percentile is its
  rank among every 126-session volatility the index has had up to and including now (an
  expanding history, so nothing after the session enters it). "Top 20 %" is a percentile of at
  least 0.8, and needs :data:`VOL_HISTORY_MIN` readings to be decided.

**The index.** Study §4 names a NIFTY 500 TRI proxy. The lake has no NIFTY 500 TRI
(`L1/benchmark_tri` carries NIFTY 50, the IT, CPSE, Midcap 150 and Smallcap 250 TRIs only), so the
regime reads the published NIFTY 500 price index, ``IN.NSE.NIFTY_500.CLOSE``, the series the
sheets and the shortlist already read. A trend, a mean and a volatility are the same on either
up to the dividend drift; the 24-month return is lower than the TRI's by about two years of
dividend yield (~2-3 pp). Stated here and in the M17.9 commit.

A missing or stale index (latest level more than :data:`STALE_DAYS` before the session), or a
breadth that is needed and missing, gives ``state=None`` with the reason; it is never guessed.

What it never does: read a clock, a level dated after the session, or act on the state.
"""

from __future__ import annotations

from bisect import bisect_right, insort
from collections.abc import Sequence
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

from dataplatform.query import PitError

__all__ = [
    "BREADTH_RISK_ON",
    "REGIME_INDEX_SERIES",
    "SMA_RISING_LAG",
    "STALE_DAYS",
    "VOL_HISTORY_MIN",
    "VOL_SESSIONS",
    "VOL_TOP_PERCENTILE",
    "RegimeIndex",
    "RegimeReading",
    "RegimeState",
]

REGIME_INDEX_SERIES: Final = "IN.NSE.NIFTY_500.CLOSE"
SMA_SESSIONS: Final = 200
SMA_RISING_LAG: Final = 21
VOL_SESSIONS: Final = 126
VOL_TOP_PERCENTILE: Final = Decimal("0.8")
VOL_HISTORY_MIN: Final = 252
BREADTH_RISK_ON: Final = Decimal("0.5")
RETURN_YEARS: Final = 2
STALE_DAYS: Final = 7

_CONTEXT: Final = Context(prec=28, rounding=ROUND_HALF_EVEN)
_Q: Final = Decimal("0.00000001")
_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)


class RegimeState(StrEnum):
    RISK_ON = "RISK_ON"
    NEUTRAL = "NEUTRAL"
    RISK_OFF = "RISK_OFF"


class RegimeReading(BaseModel):
    """The regime on one session, every input beside it, and why the state is what it is."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trading_date: date
    state: RegimeState | None
    reason: str
    index_series: str
    index_session: date | None
    index_close: Decimal | None
    sma200: Decimal | None
    sma200_prev: Decimal | None
    sma200_rising: bool | None
    return_24m: Decimal | None
    vol_126: Decimal | None
    vol_126_percentile: Decimal | None
    vol_top_20: bool | None
    breadth_above_sma50: Decimal | None
    breadth_above_sma200: Decimal | None
    india_vix: Decimal | None


def _q(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    out = value.quantize(_Q)
    return out.copy_abs() if out.is_zero() else out


def _two_years_before(day: date) -> date:
    try:
        return day.replace(year=day.year - RETURN_YEARS)
    except ValueError:  # 29 February
        return day.replace(year=day.year - RETURN_YEARS, day=28)


class RegimeIndex:
    """The regime rule over one index history, precomputed once and read per session.

    ``levels`` are the index's published ``(session, close)`` pairs. Every per-position statistic
    (SMA200, volatility, its expanding percentile) uses only levels up to that position, so a
    reading for a session never sees a later level. Built once, it answers any number of sessions,
    which is what the base-rate table's decade of sessions needs.
    """

    def __init__(self, levels: Sequence[tuple[date, Decimal]]) -> None:
        ordered = sorted(levels)
        if len({d for d, _ in ordered}) != len(ordered):
            raise ValueError("an index session appears twice")
        self.dates = [d for d, _ in ordered]
        self.closes = [c for _, c in ordered]
        n = len(ordered)
        with localcontext(_CONTEXT):
            self._sma: list[Decimal | None] = [None] * n
            running = _ZERO
            for i, close in enumerate(self.closes):
                running += close
                if i >= SMA_SESSIONS:
                    running -= self.closes[i - SMA_SESSIONS]
                if i >= SMA_SESSIONS - 1:
                    self._sma[i] = running / Decimal(SMA_SESSIONS)
            returns = [
                b / a - _ONE if a > _ZERO else None
                for a, b in zip(self.closes, self.closes[1:], strict=False)
            ]
            self._vol: list[Decimal | None] = [None] * n
            self._pct: list[Decimal | None] = [None] * n
            self._seen: list[int] = [0] * n
            history: list[Decimal] = []
            for i in range(VOL_SESSIONS, n):
                window = returns[i - VOL_SESSIONS : i]
                if any(r is None for r in window):
                    continue
                values = [r for r in window if r is not None]
                mean = sum(values, _ZERO) / Decimal(VOL_SESSIONS)
                var = sum(((r - mean) ** 2 for r in values), _ZERO) / Decimal(VOL_SESSIONS - 1)
                vol = var.sqrt().quantize(_Q)
                self._vol[i] = vol
                insort(history, vol)
                self._seen[i] = len(history)
                self._pct[i] = Decimal(bisect_right(history, vol)) / Decimal(len(history))

    def reading(
        self,
        session: date,
        *,
        breadth_above_sma50: Decimal | None,
        breadth_above_sma200: Decimal | None = None,
        india_vix: Decimal | None = None,
    ) -> RegimeReading:
        """The regime on ``session`` from levels on or before it and the universe's breadth."""
        i = bisect_right(self.dates, session) - 1
        b50, b200 = _q(breadth_above_sma50), _q(breadth_above_sma200)
        if i < 0 or (session - self.dates[i]).days > STALE_DAYS:
            return RegimeReading(
                state=None,
                reason=f"no {REGIME_INDEX_SERIES} level within {STALE_DAYS} days of the session",
                index_session=None,
                index_close=None,
                sma200=None,
                sma200_prev=None,
                sma200_rising=None,
                return_24m=None,
                vol_126=None,
                vol_126_percentile=None,
                vol_top_20=None,
                trading_date=session,
                index_series=REGIME_INDEX_SERIES,
                breadth_above_sma50=b50,
                breadth_above_sma200=b200,
                india_vix=india_vix,
            )
        if self.dates[i] > session:
            raise PitError(f"index level {self.dates[i]} reached {session}")
        with localcontext(_CONTEXT):
            close = self.closes[i]
            sma = self._sma[i]
            prev = self._sma[i - SMA_RISING_LAG] if i >= SMA_RISING_LAG else None
            rising = None if sma is None or prev is None else sma > prev
            base_i = bisect_right(self.dates, _two_years_before(self.dates[i])) - 1
            ret24 = (
                close / self.closes[base_i] - _ONE
                if base_i >= 0
                and self.closes[base_i] > _ZERO
                and (self.dates[i] - self.dates[base_i]).days <= 365 * RETURN_YEARS + STALE_DAYS
                else None
            )
            vol, pct = self._vol[i], self._pct[i]
            top = (
                None
                if pct is None or self._seen[i] < VOL_HISTORY_MIN
                else pct >= VOL_TOP_PERCENTILE
            )
            state, reason = _decide(close, sma, rising, ret24, top, breadth_above_sma50)
        return RegimeReading(
            state=state,
            reason=reason,
            index_session=self.dates[i],
            index_close=close,
            sma200=_q(sma),
            sma200_prev=_q(prev),
            sma200_rising=rising,
            return_24m=_q(ret24),
            vol_126=vol,
            vol_126_percentile=_q(pct),
            vol_top_20=top,
            trading_date=session,
            index_series=REGIME_INDEX_SERIES,
            breadth_above_sma50=b50,
            breadth_above_sma200=b200,
            india_vix=india_vix,
        )


def _decide(
    close: Decimal,
    sma: Decimal | None,
    rising: bool | None,
    ret24: Decimal | None,
    vol_top: bool | None,
    breadth: Decimal | None,
) -> tuple[RegimeState | None, str]:
    """Study §4's three states, RISK-OFF first. ``None`` when a deciding input is missing."""
    if sma is None:
        return None, "fewer than 200 index levels: no SMA200"
    below, above = close < sma, close > sma
    if below:
        if ret24 is not None and ret24 < _ZERO:
            return RegimeState.RISK_OFF, "index below SMA200 and its 24-month return is negative"
        if vol_top:
            return (
                RegimeState.RISK_OFF,
                "index below SMA200 and 126-session volatility in its top 20 %",
            )
        if ret24 is None or vol_top is None:
            return None, "index below SMA200; the 24-month return or the volatility rank is missing"
        return (
            RegimeState.NEUTRAL,
            "index below SMA200, 24-month return >= 0, volatility not in top 20 %",
        )
    if above and rising:
        if breadth is None:
            return None, "index above a rising SMA200; universe breadth is missing"
        if breadth >= BREADTH_RISK_ON:
            return (
                RegimeState.RISK_ON,
                "index above a rising SMA200 and >= 50 % of names above SMA50",
            )
        return RegimeState.NEUTRAL, "index above a rising SMA200 but < 50 % of names above SMA50"
    if above and rising is None:
        return None, "index above SMA200; fewer than 221 levels to tell whether it is rising"
    return RegimeState.NEUTRAL, "index above a falling SMA200, or on it"
