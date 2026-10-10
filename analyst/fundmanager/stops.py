"""M17.7 — mechanical stops (Amendment 1 c): no model call between a stop and its sell.

A manager's BUY declares ``stop_pct`` (the schema bounds it at 1.5-3 x ATR and at most 15 %; that
check is the decision schema's). From then on the stop is the book's, not the model's: every
session close, `StopBook.on_close` compares each held name's close with its stop, and a close
**below** the stop emits a `StopExit` — a SELL of the whole holding for the next open, journaled
``STOP_EXIT`` when the book stages it (`StopExit.order`, ``BookOrder.event``). Nothing here reads
a model, a prompt or a decision; the daily job (M17.7) calls `on_close` after the marks and hands
the exits to `FundBook.decide` ahead of the manager's own orders (`with_stop_exits`). The exits
still clear every rail — a stop is not a bypass — and one too big for participation is worked
across sessions like any sell (`analyst.fundmanager.books.PendingExit`).

**Stops only tighten.** A stop starts at ``reference_price x (1 - stop_pct / 100)``. A manager may
raise it (`tighten`) or convert it to a trailing stop (`convert_to_trailing`): the level then
follows ``high_water x (1 - trail_pct / 100)``, where ``high_water`` is the highest close since the
conversion, and never falls. Any request that would lower a level — a lower `tighten`, a wider
trail, a fresh declaration below the standing stop on an add-on buy — raises `StopLoosenError`.
A split or bonus rescales the level with the shares (`rescale`), which is not a loosening: the same
fraction of the position's value is protected.

The interface M17.4 records stops through is `StopBook.declare` / `tighten` /
`convert_to_trailing`; `to_document` / `from_document` persist it with the book.

What this module never does: call a model, place an order, read a clock, or lower a stop.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final

from analyst.fundmanager.books import BookOrder
from dataplatform.logging import get_logger
from execution.broker import Side

__all__ = [
    "MAX_STOP_PCT",
    "STOP_EXIT_EVENT",
    "Stop",
    "StopBook",
    "StopExit",
    "StopKind",
    "StopLoosenError",
    "with_stop_exits",
]

_LOG = get_logger(__name__)

#: ``BookOrder.event`` (and so ``payload.event``) on a stop's sell.
STOP_EXIT_EVENT: Final = "STOP_EXIT"
#: Amendment 1 (c): a declared stop is at most 15 % below the reference price.
MAX_STOP_PCT: Final = Decimal(15)
_ZERO: Final = Decimal(0)
_HUNDRED: Final = Decimal(100)


class StopLoosenError(ValueError):
    """A request would lower a stop. Stops only tighten (Amendment 1 c)."""


class StopKind(StrEnum):
    FIXED = "FIXED"
    TRAILING = "TRAILING"


@dataclass(frozen=True, slots=True)
class Stop:
    """One held name's stop: its level, and for a trailing stop its trail and high-water close."""

    isin: str
    kind: StopKind
    level: Decimal
    set_on: date
    trail_pct: Decimal | None = None
    high_water: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.level, Decimal) or self.level <= _ZERO:
            raise ValueError(f"{self.isin}: a stop level is a positive Decimal, got {self.level!r}")
        trailing = self.kind is StopKind.TRAILING
        if trailing != (self.trail_pct is not None and self.high_water is not None):
            raise ValueError(f"{self.isin}: a trailing stop, and only one, has a trail and a high")

    def to_document(self) -> dict[str, str]:
        document = {
            "isin": self.isin,
            "kind": self.kind.value,
            "level": str(self.level),
            "set_on": self.set_on.isoformat(),
        }
        if self.trail_pct is not None and self.high_water is not None:
            document["trail_pct"] = str(self.trail_pct)
            document["high_water"] = str(self.high_water)
        return document

    @classmethod
    def from_document(cls, document: Mapping[str, str]) -> Stop:
        return cls(
            isin=document["isin"],
            kind=StopKind(document["kind"]),
            level=Decimal(document["level"]),
            set_on=date.fromisoformat(document["set_on"]),
            trail_pct=Decimal(document["trail_pct"]) if "trail_pct" in document else None,
            high_water=Decimal(document["high_water"]) if "high_water" in document else None,
        )


@dataclass(frozen=True, slots=True)
class StopExit:
    """A close below a stop: sell ``quantity`` of ``isin`` at the next open."""

    isin: str
    quantity: int
    level: Decimal
    close: Decimal
    session: date
    kind: StopKind

    def order(self) -> BookOrder:
        """The SELL the book stages for this exit, journaled ``STOP_EXIT``."""
        return BookOrder(
            self.isin,
            Side.SELL,
            self.quantity,
            (
                f"mechanical {self.kind.value.lower()} stop: {self.session.isoformat()} close "
                f"{self.close} is below the stop {self.level}; selling {self.quantity} at the next "
                "open, no model call"
            ),
            STOP_EXIT_EVENT,
        )


def _pct_below(price: Decimal, pct: Decimal) -> Decimal:
    return price * (_HUNDRED - pct) / _HUNDRED


def _check_pct(name: str, pct: Decimal) -> None:
    if not isinstance(pct, Decimal) or not _ZERO < pct <= MAX_STOP_PCT:
        raise ValueError(f"{name} must be a Decimal in (0, {MAX_STOP_PCT}], got {pct!r}")


class StopBook:
    """One book's stops, by ISIN. See the module docstring for the rules."""

    __slots__ = ("_stops",)

    def __init__(self, stops: Mapping[str, Stop] | None = None) -> None:
        self._stops: dict[str, Stop] = {} if stops is None else dict(stops)

    @property
    def stops(self) -> Mapping[str, Stop]:
        return dict(self._stops)

    def declare(
        self, isin: str, *, stop_pct: Decimal, reference_price: Decimal, session: date
    ) -> Stop:
        """A BUY's stop: ``stop_pct`` below ``reference_price``. On a name that already has a
        stop (an add-on buy) the new level must not be below the standing one."""
        _check_pct("stop_pct", stop_pct)
        if not isinstance(reference_price, Decimal) or reference_price <= _ZERO:
            raise ValueError(f"reference price must be a positive Decimal, got {reference_price!r}")
        level = _pct_below(reference_price, stop_pct)
        standing = self._stops.get(isin)
        if standing is not None:
            if level < standing.level:
                raise StopLoosenError(
                    f"{isin}: a new stop at {level} is below the standing {standing.level}"
                )
            stop = replace(standing, level=level, set_on=session)
        else:
            stop = Stop(isin=isin, kind=StopKind.FIXED, level=level, set_on=session)
        self._stops[isin] = stop
        return stop

    def tighten(self, isin: str, *, level: Decimal, session: date) -> Stop:
        """Raise ``isin``'s stop to ``level``. A lower level raises `StopLoosenError`."""
        standing = self._require(isin)
        if level < standing.level:
            raise StopLoosenError(f"{isin}: {level} would loosen the stop at {standing.level}")
        stop = replace(standing, level=level, set_on=session)
        self._stops[isin] = stop
        return stop

    def convert_to_trailing(
        self, isin: str, *, trail_pct: Decimal, close: Decimal, session: date
    ) -> Stop:
        """Make ``isin``'s stop trail ``trail_pct`` below the highest close from ``close`` on.

        The level is the higher of the standing level and ``close``'s trail, so converting never
        lowers it. On a stop already trailing, a wider trail raises `StopLoosenError`.
        """
        _check_pct("trail_pct", trail_pct)
        standing = self._require(isin)
        if standing.trail_pct is not None and trail_pct > standing.trail_pct:
            raise StopLoosenError(
                f"{isin}: a {trail_pct}% trail would loosen the {standing.trail_pct}% trail"
            )
        high = close if standing.high_water is None else max(close, standing.high_water)
        stop = Stop(
            isin=isin,
            kind=StopKind.TRAILING,
            level=max(standing.level, _pct_below(high, trail_pct)),
            set_on=session,
            trail_pct=trail_pct,
            high_water=high,
        )
        self._stops[isin] = stop
        return stop

    def rescale(self, isin: str, *, numerator: Decimal, denominator: Decimal) -> None:
        """Shares of ``isin`` were multiplied by ``numerator / denominator`` (a split or bonus):
        prices — the level and the high-water close — divide by the same ratio."""
        standing = self._stops.get(isin)
        if standing is None:
            return
        ratio = denominator / numerator
        self._stops[isin] = replace(
            standing,
            level=standing.level * ratio,
            high_water=None if standing.high_water is None else standing.high_water * ratio,
        )

    def drop(self, isin: str) -> None:
        """Forget ``isin``'s stop (the position is gone)."""
        self._stops.pop(isin, None)

    def on_close(
        self,
        session: date,
        closes: Mapping[str, Decimal],
        held: Mapping[str, int],
        *,
        exiting: Collection[str] = (),
    ) -> tuple[StopExit, ...]:
        """Every held name whose ``session`` close is below its stop, as a `StopExit`; then every
        trailing stop not hit ratchets up on the close.

        A name no longer held loses its stop. A held name with no close this session is not
        judged (no print, no trigger). ``exiting`` names a book already selling out of (an open
        parent exit), which is not emitted again.
        """
        exits: list[StopExit] = []
        for isin in sorted(self._stops):
            stop = self._stops[isin]
            quantity = held.get(isin, 0)
            if quantity <= 0:
                del self._stops[isin]
                continue
            close = closes.get(isin)
            if close is None:
                continue
            if close < stop.level:
                if isin not in exiting:
                    exits.append(StopExit(isin, quantity, stop.level, close, session, stop.kind))
                    _LOG.info(
                        "fm_stops.stop_exit",
                        isin=isin,
                        session=session.isoformat(),
                        close=str(close),
                        level=str(stop.level),
                        kind=stop.kind.value,
                    )
                continue
            if stop.trail_pct is not None and stop.high_water is not None:
                high = max(stop.high_water, close)
                self._stops[isin] = replace(
                    stop, high_water=high, level=max(stop.level, _pct_below(high, stop.trail_pct))
                )
        return tuple(exits)

    def to_document(self) -> list[dict[str, str]]:
        return [self._stops[isin].to_document() for isin in sorted(self._stops)]

    @classmethod
    def from_document(cls, documents: Sequence[Mapping[str, str]]) -> StopBook:
        stops = [Stop.from_document(document) for document in documents]
        return cls({stop.isin: stop for stop in stops})

    def _require(self, isin: str) -> Stop:
        standing = self._stops.get(isin)
        if standing is None:
            raise KeyError(f"{isin} has no stop to change")
        return standing


def with_stop_exits(orders: Sequence[BookOrder], exits: Sequence[StopExit]) -> list[BookOrder]:
    """The session's orders with the stop exits first; a manager order on a stopped name is
    dropped (the stop has already decided that name), every other order kept in its order."""
    stopped = {exit_.isin for exit_ in exits}
    return [exit_.order() for exit_ in exits] + [o for o in orders if o.isin not in stopped]
