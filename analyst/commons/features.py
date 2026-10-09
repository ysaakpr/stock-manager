"""A10 · M17.9 — per-name price, volume and earnings features, one definition for every reader.

The screens (`analyst.commons.screens`), the dossier (`analyst.commons.dossier`) and the base-rate
table (`analyst.commons.base_rates`) all read a name through :func:`name_features` and
:func:`earnings_features`. So a screen evaluated live and the same screen evaluated over ten years
of history are one rule, not two implementations that can drift.

**The window.** Features are struck on a name's bars aligned to the NSE session calendar, ending
at the decision session: ``window[-1]`` is the session's bar, and ``window[-1 - k]`` is the bar
``k`` sessions back, or ``None`` when the name did not print in the series that session. A window
holds at most :data:`WINDOW_SESSIONS` bars. Prices and volumes are adjusted
(`analyst.commons.inputs.PriceBar`).

**Definitions** (study §2; every return is a plain fraction, 0.05 = 5 %):

- ``ret_6_1`` = close(t-21) / close(t-126) - 1 and ``ret_12_1`` = close(t-21) / close(t-252) - 1,
  on those exact sessions (the 12-1 of `analyst.commons.shortlist`, and 6-1 its half-year twin).
- ``vol_252``: the sample stdev of the 252 daily returns ending at t; it needs a print on all 253
  sessions. ``vol_20`` likewise over 20 returns.
- ``sma_n``: the mean of the last ``n`` closes, all present. ``sma200_prev`` is the SMA200 struck
  21 sessions earlier; the SMA200 is *rising* when ``sma200 > sma200_prev``.
- ``high_252``: the highest close of the last 252 sessions; ``near_high`` = close / high_252.
- True range ``max(high, prev close) - min(low, prev close)``; ``atr_n`` is its simple mean over
  ``n`` sessions, all present. ``atr10_prev`` and ``atr50_prev`` end at t-1: the contraction before
  the move.
- ``at_high_60``: the close is the highest close of the last 60 sessions, t included.
- Volume ratios divide by ``volume_median_50``, the median volume of the 50 sessions *before* t.
  ``volume_ratio_day0`` is t's volume over it; ``volume_ratio_5`` is the mean of the last 5.
- ``deliv_z60``: (delivered quantity at t - its mean over the 60 sessions before) / their sample
  stdev, with at least :data:`DELIVERY_MIN_SESSIONS` of the 60 present.
- The 20-session pullback: ``high_20`` is the highest close of the last 20 sessions (the latest
  session that set it), ``days_since_high_20`` how many sessions ago, ``pullback_pct`` =
  1 - close / high_20, and ``pullback_volume_ratio`` the mean volume of the sessions after the high
  over the median volume of the 20 sessions.
- Earnings (:func:`earnings_features`): the M16.3 SUE of the latest quarter, its day 0 (the first
  session on or after the quarter's first filing), the earnings-announcement return over
  [-1, +1] around day 0 minus NIFTY 500's over the same sessions, and day 0's volume over the
  median of the 50 sessions before it.

A feature whose inputs are incomplete is ``None``, never zero or a partial-window estimate.
Every value is quantised to 8 dp in a fixed decimal context, so two runs give the same bytes.

What it never does: read anything, look past ``window[-1]``, or treat a missing bar as flat.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from itertools import pairwise
from typing import Final

from pydantic import BaseModel, ConfigDict

from analyst.commons.inputs import PriceBar
from backtest.policies.earnings_surprise import EarningsSurprisePanel
from dataplatform.query import PitError

__all__ = [
    "DELIVERY_MIN_SESSIONS",
    "EAR_AFTER",
    "EAR_BEFORE",
    "FEATURE_FIELDS",
    "WINDOW_SESSIONS",
    "EarningsFeatures",
    "NameFeatures",
    "align",
    "earnings_features",
    "feature_values",
    "level_on_or_before",
    "name_features",
]

#: The longest look-back any feature needs: 12-1 and vol_252 need the close 252 sessions back,
#: and that is 253 bars. 261 matches the sheets' window (`ADJUSTED_LOOKBACK_SESSIONS`).
WINDOW_SESSIONS: Final = 261
DELIVERY_MIN_SESSIONS: Final = 40
#: The earnings-announcement return window around day 0, in sessions: [-1, +1].
EAR_BEFORE: Final = 1
EAR_AFTER: Final = 1

_CONTEXT: Final = Context(prec=28, rounding=ROUND_HALF_EVEN)
_Q: Final = Decimal("0.00000001")
_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NameFeatures(_Model):
    """One name's price and volume features on one session (module docstring)."""

    isin: str
    close: Decimal
    ret_5: Decimal | None
    ret_6_1: Decimal | None
    ret_12_1: Decimal | None
    vol_20: Decimal | None
    vol_252: Decimal | None
    sma20: Decimal | None
    sma50: Decimal | None
    sma200: Decimal | None
    sma200_prev: Decimal | None
    dist_sma20: Decimal | None
    dist_sma50: Decimal | None
    dist_sma200: Decimal | None
    high_252: Decimal | None
    near_high: Decimal | None
    from_high_252: Decimal | None
    atr14: Decimal | None
    atr14_pct: Decimal | None
    atr10_prev: Decimal | None
    atr50_prev: Decimal | None
    atr_contraction: Decimal | None
    at_high_60: bool | None
    volume_median_50: Decimal | None
    volume_ratio_day0: Decimal | None
    volume_ratio_5: Decimal | None
    deliv_pct: Decimal | None
    deliv_z60: Decimal | None
    high_20: Decimal | None
    days_since_high_20: int | None
    pullback_pct: Decimal | None
    pullback_volume_ratio: Decimal | None


class EarningsFeatures(_Model):
    """The latest results of one name, as of one session: SUE, its day 0, EAR and day-0 volume."""

    isin: str
    sue: Decimal
    filing_date: date
    period_end: date
    day0: date
    sessions_since_day0: int
    ear: Decimal | None
    day0_volume_ratio: Decimal | None


#: Every scalar feature name, in model order: what a dossier field id may name.
FEATURE_FIELDS: Final[tuple[str, ...]] = tuple(n for n in NameFeatures.model_fields if n != "isin")


def _q(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    out = value.quantize(_Q)
    return out.copy_abs() if out.is_zero() else out


def _ratio(num: Decimal | None, den: Decimal | None) -> Decimal | None:
    if num is None or den is None or den <= _ZERO:
        return None
    return num / den


def _rel(now: Decimal | None, then: Decimal | None) -> Decimal | None:
    r = _ratio(now, then)
    return None if r is None else r - _ONE


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, _ZERO) / Decimal(len(values))


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    n, mid = len(ordered), len(ordered) // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / Decimal(2)


def _stdev(values: Sequence[Decimal]) -> Decimal:
    mean = _mean(values)
    return (sum(((v - mean) ** 2 for v in values), _ZERO) / Decimal(len(values) - 1)).sqrt()


def align(bars: Sequence[PriceBar], calendar: Sequence[date]) -> dict[str, list[PriceBar | None]]:
    """Each ISIN's bars laid on ``calendar``: one slot per session, ``None`` where it printed none.

    Raises ``ValueError`` for two different bars of one name on one session, and for a bar dated
    off the calendar (a bar after the last session would be a look-ahead; one between sessions is
    not an NSE session at all).
    """
    position = {day: i for i, day in enumerate(calendar)}
    out: dict[str, list[PriceBar | None]] = {}
    for bar in bars:
        i = position.get(bar.trade_date)
        if i is None:
            raise ValueError(f"{bar.isin} bar on {bar.trade_date} is not a calendar session")
        slots = out.setdefault(bar.isin, [None] * len(calendar))
        seen = slots[i]
        if seen is not None and seen != bar:
            raise ValueError(f"{bar.isin} has two bars on {bar.trade_date.isoformat()}")
        slots[i] = bar
    return out


def _closes(window: Sequence[PriceBar | None]) -> list[Decimal | None]:
    return [None if b is None else b.close for b in window]


def _full(values: Sequence[Decimal | None]) -> list[Decimal] | None:
    out = [v for v in values if v is not None]
    return out if len(out) == len(values) and out else None


def _back(values: Sequence[Decimal | None], k: int) -> Decimal | None:
    return values[-1 - k] if len(values) > k else None


def _sma(closes: Sequence[Decimal | None], n: int, offset: int = 0) -> Decimal | None:
    end = len(closes) - offset
    if end < n:
        return None
    full = _full(closes[end - n : end])
    return None if full is None else _mean(full)


def _returns_stdev(closes: Sequence[Decimal | None], n: int) -> Decimal | None:
    if len(closes) < n + 1:
        return None
    full = _full(closes[-n - 1 :])
    if full is None or any(c <= _ZERO for c in full):
        return None
    return _stdev([b / a - _ONE for a, b in pairwise(full)])


def _atr(window: Sequence[PriceBar | None], n: int, offset: int = 0) -> Decimal | None:
    end = len(window) - offset
    if end < n + 1:
        return None
    span = window[end - n - 1 : end]
    if any(b is None for b in span):
        return None
    ranges = [
        max(cur.high, prev.close) - min(cur.low, prev.close)
        for prev, cur in pairwise(b for b in span if b is not None)
    ]
    return _mean(ranges)


def name_features(isin: str, window: Sequence[PriceBar | None]) -> NameFeatures | None:
    """``isin``'s features on the session ``window`` ends at, or ``None`` if it did not print.

    What it does: computes every field of :class:`NameFeatures` from ``window`` alone.
    What it assumes: ``window`` is the name's bars aligned to consecutive NSE sessions, the last
    being the decision session, at most :data:`WINDOW_SESSIONS` long.
    What it never does: read a bar it was not given, or fill a missing one.
    """
    today = window[-1] if window else None
    if today is None:
        return None
    if today.isin != isin:
        raise ValueError(f"window of {today.isin} handed to {isin}")
    with localcontext(_CONTEXT):
        closes = _closes(window)
        close = today.close
        sma20, sma50, sma200 = _sma(closes, 20), _sma(closes, 50), _sma(closes, 200)
        last252 = [c for c in closes[-252:] if c is not None] if len(closes) >= 252 else []
        high_252 = max(last252) if last252 else None
        last60 = _full(closes[-60:]) if len(closes) >= 60 else None
        volumes = [None if b is None else b.volume for b in window]
        before50 = _full(volumes[-51:-1]) if len(volumes) >= 51 else None
        volume_median = _median(before50) if before50 else None
        last5 = _full(volumes[-5:])
        atr14, atr10_prev, atr50_prev = _atr(window, 14), _atr(window, 10, 1), _atr(window, 50, 1)

        deliv_z: Decimal | None = None
        if today.deliv_qty is not None and len(window) >= 61:
            prior = [
                b.deliv_qty for b in window[-61:-1] if b is not None and b.deliv_qty is not None
            ]
            if len(prior) >= DELIVERY_MIN_SESSIONS:
                spread = _stdev(prior)
                if spread > _ZERO:
                    deliv_z = (today.deliv_qty - _mean(prior)) / spread

        high_20: Decimal | None = None
        days_since: int | None = None
        pullback_volume: Decimal | None = None
        last20 = _full(closes[-20:]) if len(closes) >= 20 else None
        vol20 = _full(volumes[-20:]) if len(volumes) >= 20 else None
        if last20 is not None:
            high_20 = max(last20)
            # The latest session that set the high: a double top dates the pullback from its
            # second peak.
            at = max(i for i, c in enumerate(last20) if c == high_20)
            days_since = len(last20) - 1 - at
            if vol20 is not None and days_since >= 1:
                pullback_volume = _ratio(_mean(vol20[at + 1 :]), _median(vol20))

        return NameFeatures(
            isin=isin,
            close=close,
            ret_5=_q(_rel(close, _back(closes, 5))),
            ret_6_1=_q(_rel(_back(closes, 21), _back(closes, 126))),
            ret_12_1=_q(_rel(_back(closes, 21), _back(closes, 252))),
            vol_20=_q(_returns_stdev(closes, 20)),
            vol_252=_q(_returns_stdev(closes, 252)),
            sma20=_q(sma20),
            sma50=_q(sma50),
            sma200=_q(sma200),
            sma200_prev=_q(_sma(closes, 200, 21)),
            dist_sma20=_q(_rel(close, sma20)),
            dist_sma50=_q(_rel(close, sma50)),
            dist_sma200=_q(_rel(close, sma200)),
            high_252=_q(high_252),
            near_high=_q(_ratio(close, high_252)),
            from_high_252=_q(_rel(close, high_252)),
            atr14=_q(atr14),
            atr14_pct=_q(_ratio(atr14, close)),
            atr10_prev=_q(atr10_prev),
            atr50_prev=_q(atr50_prev),
            atr_contraction=_q(_ratio(atr10_prev, atr50_prev)),
            at_high_60=None if last60 is None else close >= max(last60),
            volume_median_50=_q(volume_median),
            volume_ratio_day0=_q(_ratio(today.volume, volume_median)),
            volume_ratio_5=_q(_ratio(_mean(last5), volume_median) if last5 else None),
            deliv_pct=_q(today.deliv_pct),
            deliv_z60=_q(deliv_z),
            high_20=_q(high_20),
            days_since_high_20=days_since,
            pullback_pct=_q(_ONE - close / high_20 if high_20 else None),
            pullback_volume_ratio=_q(pullback_volume),
        )


def level_on_or_before(levels: Sequence[tuple[date, Decimal]], day: date) -> Decimal | None:
    """The last index level on or before ``day`` from date-sorted ``levels``, or ``None``."""
    i = bisect_right(levels, day, key=lambda lv: lv[0])
    return levels[i - 1][1] if i else None


def earnings_features(
    isin: str,
    window: Sequence[PriceBar | None],
    calendar: Sequence[date],
    *,
    panel: EarningsSurprisePanel,
    index_levels: Sequence[tuple[date, Decimal]],
) -> EarningsFeatures | None:
    """``isin``'s latest results as of ``calendar[-1]``, or ``None`` with no SUE or no day 0.

    ``window`` and ``calendar`` are aligned and end at the session. ``index_levels`` are NIFTY
    500's published closes, date-sorted, none after the session. The EAR is ``None`` until the
    session after day 0 has printed, and when either end of the window is missing.
    """
    session = calendar[-1]
    reading = panel.reading(isin, session)
    if reading is None:
        return None
    if reading.filing_date > session:
        raise PitError(f"{isin} results filed {reading.filing_date} reached {session}")
    if index_levels and index_levels[-1][0] > session:
        raise PitError(f"a NIFTY 500 level dated {index_levels[-1][0]} reached {session}")
    at = bisect_left(calendar, reading.filing_date)
    if at >= len(calendar):
        return None
    with localcontext(_CONTEXT):
        closes = _closes(window)
        offset = len(calendar) - len(window)
        ear: Decimal | None = None
        start_i, end_i = at - EAR_BEFORE - 1, at + EAR_AFTER
        if start_i - offset >= 0 and end_i < len(calendar):
            begin, end = closes[start_i - offset], closes[end_i - offset]
            stock = _rel(end, begin)
            index = _rel(
                level_on_or_before(index_levels, calendar[end_i]),
                level_on_or_before(index_levels, calendar[start_i]),
            )
            if stock is not None and index is not None:
                ear = stock - index
        day0_ratio: Decimal | None = None
        d0 = at - offset
        if d0 - 50 >= 0 and window[d0] is not None:
            before = _full([None if b is None else b.volume for b in window[d0 - 50 : d0]])
            today = window[d0]
            if before and today is not None:
                day0_ratio = _ratio(today.volume, _median(before))
        return EarningsFeatures(
            isin=isin,
            sue=reading.sue,
            filing_date=reading.filing_date,
            period_end=reading.latest_period_end,
            day0=calendar[at],
            sessions_since_day0=len(calendar) - 1 - at,
            ear=_q(ear),
            day0_volume_ratio=_q(day0_ratio),
        )


def feature_values(features: NameFeatures) -> Mapping[str, Decimal | int | bool | None]:
    """Every scalar feature of ``features`` by name (what the dossier exposes as field ids)."""
    return {name: getattr(features, name) for name in FEATURE_FIELDS}
