"""Research (MA-timing study, part 3): a stock-level 30-day moving-average system, as an article
described it, made into fixed rules.

The article's system, per stock: a 30-day SMA trend filter ("sustainably" above = bullish), a
pullback entry (in an uptrend, wait for a dip back to the SMA and buy the bounce off it), and a fast
EMA crossing the SMA as confirmation (EMA up through SMA = buy, down through = sell). Every vague
term is pinned to one stated rule here, chosen before any window was run and never tuned:

* **Uptrend** ``U(d)``: the close was above its SMA30 on each of the 5 sessions ending ``d``, and
  SMA30 on ``d`` is above SMA30 five sessions earlier (rising).
* **Pullback** ``P(d)``: the session's low came within 2% of SMA30 (``low <= SMA30 x 1.02``) and it
  did not close more than 1% below it (``close >= SMA30 x 0.99``).
* **Bounce** (the pullback entry, signal on ``t``): ``U(t-2)`` and ``P(t-1)``, and ``t`` closes
  above ``t-1``'s close and above SMA30. ``U`` is read at ``t-2`` because a pullback day may close
  just under the SMA, which by itself would break ``U``.
* **Golden cross** (signal on ``t``): EMA10 (alpha 2/11) crossed above SMA30 on one of the last 5
  sessions and is still above it on ``t``. **Death cross**: EMA10 below SMA30 on ``t``.
* **Protective exit** (P3c): the close below SMA30 by more than 3%.

All of it is struck by the data source on split/bonus-adjusted prices (the swing composite's
seam-consistent adjusted price), over backward-only windows; the policy only reads the flags.

Arms: **P3a** crossover entry, death-cross exit. **P3b** bounce entry, death-cross exit. **P3c**
P3b plus the protective exit. Each optionally behind a market regime gate (NIFTY 50 TRI against
its N-session SMA, as D13's — here suppressing *new buys only*; held names leave on their own
exits, as the swing composite's gate does).

Portfolio: at most ``max_names`` (20) names; each new name is bought at ``1 / max_names`` of the
account's value (equal weight), or, vol-scaled, at that times the session's median candidate
volatility over the name's own (63-session), capped at ``max_weight`` (10%); never more than the
settled cash and the A8 per-order ceiling allow. No top-ups or trims: entries and exits are events.
When more names signal than slots are free they are ranked by 12-1 momentum (ties by ISIN).
Signals are read every session at the close; orders fill at the next session's open (SimBroker).

What it never does: read a clock, key on a symbol, hold a cost model, or read data outside the
point-in-time context.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Protocol

from analyst.cases import RiskRails
from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import Actor, Decision, JournalEntry, Sleeve
from backtest.policies.momentum_v2 import RegimeReading
from backtest.policies.sizing import account_order_ceiling
from backtest.replay import SessionContext, SessionDecision
from backtest.sip import MIN_ORDER_VALUE_INR
from dataplatform.query.pit import Dataset
from execution.broker import Exchange, Holding, OrderRequest, Position, Side

__all__ = [
    "Entry",
    "Sma30Data",
    "Sma30Parameters",
    "Sma30Record",
    "Sma30StockPolicy",
]

_ZERO = Decimal("0")
_ONE = Decimal("1")


class Entry(StrEnum):
    """Which entry signal opens a position."""

    #: EMA10 crossed above SMA30 within the last 5 sessions and is still above it (P3a).
    CROSSOVER = "crossover"
    #: The uptrend-pullback-bounce sequence (P3b, P3c).
    PULLBACK = "pullback"


@dataclass(frozen=True, slots=True)
class Sma30Parameters:
    """One arm of the stock-level 30-DMA system. Every field is a stated, fixed rule."""

    entry: Entry
    #: Sell when the close is below SMA30 by more than this (P3c: 3%); ``None`` = death cross only.
    protective_exit: Decimal | None = None
    #: NIFTY 50 TRI regime gate on new buys: its SMA length (200 = D13's), or ``None`` for no gate.
    regime_ma_days: int | None = None
    vol_scaled: bool = False
    max_names: int = 20
    max_weight: Decimal = Decimal("0.10")
    buy_budget_fraction: Decimal = Decimal("0.98")
    sleeve: Sleeve = Sleeve.TACTICAL

    def __post_init__(self) -> None:
        if self.max_names <= 0:
            raise ValueError("max_names must be positive")
        if not (_ZERO < self.max_weight <= _ONE):
            raise ValueError("max_weight must be in (0, 1]")
        if self.protective_exit is not None and not (_ZERO < self.protective_exit < _ONE):
            raise ValueError("protective_exit must be in (0, 1)")
        if self.regime_ma_days is not None and self.regime_ma_days <= 0:
            raise ValueError("regime_ma_days must be positive")


@dataclass(frozen=True, slots=True)
class Sma30Record:
    """One name's 30-DMA facts on one session, struck on adjusted prices through that close.

    ``price`` is the raw close (sizing, invariant #3). ``close_to_sma`` and ``ema_to_sma`` are the
    adjusted close and EMA10 over SMA30 — dimensionless, so a split moves neither.
    """

    isin: str
    price: Decimal
    knowable_date: date
    close_to_sma: Decimal
    ema_to_sma: Decimal
    golden_cross: bool
    bounce: bool
    momentum_12_1: Decimal | None
    volatility: Decimal | None
    #: The session's cross-sectional median volatility (every name with features), for vol sizing.
    median_volatility: Decimal | None = None


class Sma30Data(Protocol):
    """What the policy reads, every read through the PIT guard.

    ``candidates(as_of)`` — records for the investable, liquid universe (entries).
    ``held(as_of)`` — records for every name with features that session, universe or not (exits:
    a name that left the screen still leaves on its own exit rule).
    ``regime(as_of)`` — the NIFTY 50 TRI reading at the arm's regime length.
    """

    def candidates(self, as_of: date) -> Dataset[Sma30Record]: ...

    def held(self, as_of: date) -> Dataset[Sma30Record]: ...

    def regime(self, as_of: date) -> Dataset[RegimeReading]: ...


class Sma30StockPolicy:
    """The stock-level 30-DMA system. Satisfies :class:`backtest.replay.Policy`."""

    __slots__ = ("_data", "_order_caps", "_params")

    def __init__(
        self, data: Sma30Data, params: Sma30Parameters, *, order_caps: RiskRails | None = None
    ) -> None:
        self._data = data
        self._params = params
        #: A8's rails, so no buy is sized past the per-order ceiling the gate would refuse.
        self._order_caps = order_caps

    def exits(self, record: Sma30Record) -> str | None:
        """Why a held name leaves on this record, or ``None``."""
        if record.ema_to_sma < _ONE:
            return f"death cross: EMA10/SMA30 {record.ema_to_sma} < 1"
        stop = self._params.protective_exit
        if stop is not None and record.close_to_sma < _ONE - stop:
            return f"protective exit: close/SMA30 {record.close_to_sma} below 1 - {stop}"
        return None

    def enters(self, record: Sma30Record) -> bool:
        """Whether the arm's entry signal fired on this record."""
        if self._params.entry is Entry.CROSSOVER:
            return record.golden_cross
        return record.bounce

    def decide(self, ctx: SessionContext) -> SessionDecision:
        """Exits on every held name with a signal; then entries into free slots, best 12-1 first."""
        held = {h.isin: h for h in ctx.broker.holdings()}
        pending = {p.isin for p in ctx.broker.positions()}
        facts = {r.isin: r for r in ctx.pit.admit(self._data.held(ctx.session))}
        sells: list[tuple[OrderRequest, str]] = []
        for isin in sorted(held):
            record = facts.get(isin)
            why = None if record is None else self.exits(record)
            if why is not None:
                order = OrderRequest(isin=isin, side=Side.SELL, quantity=held[isin].quantity)
                sells.append((order, f"{why}; selling {held[isin].quantity}"))
        selling = {o.isin for o, _ in sells}
        book = (set(held) | pending) - selling
        risk_on = True
        regime_note = "no regime gate"
        if self._params.regime_ma_days is not None:
            readings = tuple(ctx.pit.admit(self._data.regime(ctx.session)))
            risk_on = bool(readings) and readings[0].risk_on
            regime_note = f"regime {self._params.regime_ma_days}d: " + (
                "risk-on" if risk_on else "risk-off (no new buys)"
            )
        buys: list[tuple[OrderRequest, str]] = []
        signalled: list[Sma30Record] = []
        free = self._params.max_names - len(book)
        if risk_on and free > 0:
            candidates = tuple(ctx.pit.admit(self._data.candidates(ctx.session)))
            signalled = [
                r
                for r in candidates
                if r.isin not in book and r.isin not in selling and self.enters(r)
            ]
            signalled.sort(
                key=lambda r: (
                    -(r.momentum_12_1 if r.momentum_12_1 is not None else Decimal("-1e9")),
                    r.isin,
                )
            )
            buys = self._buys(ctx, signalled[:free], candidates, facts)
        orders = tuple(o for o, _ in (*sells, *buys))
        entries = tuple(self._entry(ctx, o, note) for o, note in (*sells, *buys))
        evidence = EvidenceBundle(
            trading_date=ctx.session,
            actor=Actor.T0,
            items=(
                EvidenceItem(
                    kind=EvidenceKind.POSITION,
                    source="book",
                    label="sma30_book",
                    as_of=ctx.session,
                    value=Decimal(len(book)),
                    detail={
                        "exits": str(len(sells)),
                        "signalled": str(len(signalled)),
                        "bought": str(len(buys)),
                        "free_slots": str(max(free, 0)),
                    },
                    text=f"{self._params.entry.value} entry; {regime_note}",
                ),
            ),
        )
        return SessionDecision(evidence=evidence, orders=orders, entries=entries)

    def _buys(
        self,
        ctx: SessionContext,
        chosen: Sequence[Sma30Record],
        candidates: Sequence[Sma30Record],
        facts: dict[str, Sma30Record],
    ) -> list[tuple[OrderRequest, str]]:
        if not chosen:
            return []
        margins = ctx.broker.margins()
        marks = {r.isin: r.price for r in candidates}
        marks.update({isin: r.price for isin, r in facts.items()})
        value = margins.cash_value
        lots: list[Holding | Position] = [*ctx.broker.holdings(), *ctx.broker.positions()]
        for lot in lots:
            value += marks.get(lot.isin, lot.average_price) * lot.quantity
        base = _ONE / Decimal(self._params.max_names)
        budget = margins.available * self._params.buy_budget_fraction
        ceiling = account_order_ceiling(self._order_caps, ctx.broker, marks)
        out: list[tuple[OrderRequest, str]] = []
        for r in chosen:
            weight = base
            if self._params.vol_scaled and r.median_volatility and r.volatility:
                weight = base * r.median_volatility / r.volatility
            weight = min(weight, self._params.max_weight)
            target = value * weight
            spend = min(target, budget)
            if ceiling is not None:
                spend = min(spend, ceiling)
            quantity = int((spend / r.price).to_integral_value(ROUND_DOWN))
            if quantity <= 0 or quantity * r.price < MIN_ORDER_VALUE_INR:
                continue
            budget -= quantity * r.price
            signal = "golden cross" if self._params.entry is Entry.CROSSOVER else "SMA30 bounce"
            out.append(
                (
                    OrderRequest(
                        isin=r.isin,
                        side=Side.BUY,
                        quantity=quantity,
                        exchange=Exchange.NSE,
                        tag="SMA30",
                    ),
                    f"{signal}; 12-1 {r.momentum_12_1}; weight {weight.quantize(Decimal('0.0001'))}"
                    f"; buy {quantity} @ {r.price}",
                )
            )
        return out

    def _entry(self, ctx: SessionContext, order: OrderRequest, rationale: str) -> JournalEntry:
        return JournalEntry(
            ts=ctx.clock.now(),
            trading_date=ctx.session,
            actor=Actor.T0,
            decision=Decision.BUY if order.side is Side.BUY else Decision.SELL,
            isin=order.isin,
            sleeve=self._params.sleeve,
            rationale=rationale,
        )
