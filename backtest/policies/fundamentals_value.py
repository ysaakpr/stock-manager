"""M10.6 — a fundamentals-signal backtest policy: value (earnings yield) and growth tilts (§7, X2).

The momentum family (naive, v2, sector rotation) ranks on price history. This policy ranks on the
M10.5 metrics — figures a filing *stated*, read point-in-time — so the question the M10 report can
finally ask is whether a fundamentals signal diversifies momentum's regime dependence, not whether
it beats it outright. Three a-priori signals, each a stated parameter, none tuned:

* ``VALUE`` — rank on **trailing earnings yield** (TTM earnings / market cap; the inverse of P/E,
  used because it orders loss-makers naturally to the bottom instead of giving them a negative P/E
  that sorts as "cheap"). Only names with positive trailing earnings qualify.
* ``GROWTH`` — rank on **TTM-on-TTM earnings growth** (the trailing year against the year before
  it), which needs eight knowable quarters and a positive base.
* ``QUALITY_VALUE`` — the classic two-factor blend: average of the name's *rank* on earnings yield
  and its rank on ROE, where ROE is stated (the balance sheet is filed by ~19% of filings, so this
  arm's universe is smaller and the report says so).
* ``MOMENTUM_VALUE`` — the other classic blend, and the one the M10 question is really about:
  average of the rank on earnings yield and the rank on 12-1 price momentum (the same raw signal the
  momentum v2 arm ranks on), over names with positive trailing earnings and a stated momentum. Value
  and momentum are the two factor premia with the most negative long-run correlation; if a
  fundamentals signal is going to diversify momentum's regime dependence, this is where it shows.

Mechanics are copied from the momentum v2 policy exactly — monthly rebalance on the first session,
top-``top_n`` equal weight, whole-share buys sized from free cash through the M4.7 allocator, an
optional hysteresis ``sell_band``, fills through the injected broker's one cost model (invariant
#4/#5) — so the only thing that differs between this policy and plain momentum on the same universe
is *what the ranking reads*. That is what makes the comparison in the report a comparison of
signals rather than of plumbing.

Point-in-time (invariant #7): every candidate record carries the ``knowable_date`` of the newest
filing that entered it (never after the session — `compute_metrics` refuses a later filing) and is
admitted through ``ctx.pit``. A name whose newest filing is older than ``max_staleness_days`` is
dropped: a company that has stopped filing has a TTM that is quietly rotting, and the policy should
not hold it on that basis.

What it never does: read a wall clock, key on a symbol, hold a cost model, read the restated
(Screener) store — it consumes :class:`FundamentalMetrics`, which are derived from the PIT store
alone (invariant #8) — or return a different decision for the same inputs.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import Actor, Decision, JournalEntry, Sleeve
from backtest.replay import SessionContext, SessionDecision
from backtest.sip import simulate_sip_instalment
from dataplatform.query.pit import Dataset
from execution.broker import Exchange, Holding, OrderRequest, Side

__all__ = [
    "FundamentalsRecord",
    "FundamentalsSignal",
    "FundamentalsSignalData",
    "FundamentalsValueParameters",
    "FundamentalsValuePolicy",
    "rank_candidates",
]

_ZERO = Decimal("0")
_ONE = Decimal("1")
_WEIGHT_QUANTUM = Decimal("0.00000001")


class FundamentalsSignal(StrEnum):
    """Which stated metric ranks the universe. Chosen once per run and echoed into the report."""

    VALUE = "VALUE"
    GROWTH = "GROWTH"
    QUALITY_VALUE = "QUALITY_VALUE"
    MOMENTUM_VALUE = "MOMENTUM_VALUE"


@dataclass(frozen=True, slots=True)
class FundamentalsRecord:
    """One candidate this session: its stated-metric signals, its price, and when it was knowable.

    ``earnings_yield`` is TTM earnings over market cap (may be negative for a loss-maker — the
    VALUE arm drops those); ``earnings_growth`` is TTM-on-TTM growth or ``None`` when fewer than
    eight quarters are knowable or the base is non-positive; ``roe`` is TTM earnings over the latest
    annual equity or ``None`` where no balance sheet is filed. ``price`` is the raw close the
    whole-share allocation uses; ``knowable_date`` is the newest filing date behind the figures.
    ``momentum_12_1`` is the trailing 12-1 price return (the momentum v2 signal) or ``None`` when
    the look-back is not available; only the MOMENTUM_VALUE arm reads it. All money and ratios are
    ``Decimal``; a ``float`` is refused (CLAUDE.md).
    """

    isin: str
    earnings_yield: Decimal
    earnings_growth: Decimal | None
    roe: Decimal | None
    price: Decimal
    knowable_date: date
    momentum_12_1: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("earnings_yield", "earnings_growth", "roe", "price", "momentum_12_1"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Decimal):
                raise TypeError(f"{name} must be a Decimal — money/signal is never float")
        if self.price <= _ZERO:
            raise ValueError(f"price must be positive, got {self.price}")


@dataclass(frozen=True, slots=True)
class FundamentalsValueParameters:
    """The a-priori knobs — stated once, echoed verbatim into the report, never tuned.

    * ``signal`` — VALUE, GROWTH, QUALITY_VALUE or MOMENTUM_VALUE (see the module docstring).
    * ``top_n`` — names held, equal-weighted. 20, the same as the momentum baseline.
    * ``sell_band`` — hysteresis: sell only once a holding leaves the top ``sell_band``; ``None``
      sells the moment it leaves the top ``top_n``.
    * ``max_staleness_days`` — drop a name whose newest filing is older than this on the session.
      200 days: two missed quarterly deadlines.
    * ``buy_budget_fraction`` / ``sleeve`` — the execution margin and journal sleeve, as in v2.
    """

    signal: FundamentalsSignal = FundamentalsSignal.VALUE
    top_n: int = 20
    sell_band: int | None = None
    max_staleness_days: int = 200
    buy_budget_fraction: Decimal = Decimal("0.98")
    sleeve: Sleeve = Sleeve.TACTICAL

    def __post_init__(self) -> None:
        if self.top_n <= 0:
            raise ValueError(f"top_n must be positive, got {self.top_n}")
        if self.sell_band is not None and self.sell_band < self.top_n:
            raise ValueError(
                f"sell_band must be >= top_n: got sell_band={self.sell_band}, top_n={self.top_n}"
            )
        if self.max_staleness_days <= 0:
            raise ValueError(f"max_staleness_days must be positive, got {self.max_staleness_days}")
        if not isinstance(self.buy_budget_fraction, Decimal):
            raise TypeError("buy_budget_fraction must be a Decimal")
        if not (_ZERO < self.buy_budget_fraction <= _ONE):
            raise ValueError(
                f"buy_budget_fraction must be in (0, 1], got {self.buy_budget_fraction}"
            )


@runtime_checkable
class FundamentalsSignalData(Protocol):
    """Where the policy reads its world — the injected seam the point-in-time context wraps.

    * ``is_rebalance(session)`` — is today a rebalance session?
    * ``signal(as_of)`` — the candidate set as a guardable :class:`Dataset` of
      :class:`FundamentalsRecord`, already narrowed to the investable PIT universe and to names
      with a price and a computable earnings yield.
    """

    def is_rebalance(self, session: date) -> bool:
        """Whether ``session`` is a rebalance session."""

    def signal(self, as_of: date) -> Dataset[FundamentalsRecord]:
        """The PIT candidate set as of ``as_of``, as a guardable dataset."""


def rank_candidates(
    records: tuple[FundamentalsRecord, ...],
    *,
    signal: FundamentalsSignal,
    session: date,
    max_staleness_days: int,
) -> list[FundamentalsRecord]:
    """Order the admitted candidates best-first under ``signal``, dropping the unrankable.

    VALUE: positive earnings yield, descending. GROWTH: stated growth, descending (a name without
    eight knowable quarters or with a non-positive base is unrankable). QUALITY_VALUE: the mean of
    the name's rank on earnings yield and on ROE, ascending (best first), over names that have both
    and a positive earnings yield. MOMENTUM_VALUE: the same blend with 12-1 momentum in place of
    ROE. Stale names (newest filing older than ``max_staleness_days``) are dropped first under
    every signal. Ties break on ISIN, so the order is deterministic.
    """
    fresh = [r for r in records if (session - r.knowable_date).days <= max_staleness_days]
    if signal is FundamentalsSignal.VALUE:
        eligible = [r for r in fresh if r.earnings_yield > _ZERO]
        return sorted(eligible, key=lambda r: (-r.earnings_yield, r.isin))
    if signal is FundamentalsSignal.GROWTH:
        eligible = [r for r in fresh if r.earnings_growth is not None]
        return sorted(eligible, key=lambda r: (-(r.earnings_growth or _ZERO), r.isin))
    if signal is FundamentalsSignal.MOMENTUM_VALUE:
        eligible = [r for r in fresh if r.momentum_12_1 is not None and r.earnings_yield > _ZERO]
        return _blend_ranks(eligible, lambda r: r.momentum_12_1 or _ZERO)
    eligible = [r for r in fresh if r.roe is not None and r.earnings_yield > _ZERO]
    return _blend_ranks(eligible, lambda r: r.roe or _ZERO)


def _blend_ranks(
    eligible: list[FundamentalsRecord], second: Callable[[FundamentalsRecord], Decimal]
) -> list[FundamentalsRecord]:
    """Best-first by the sum of the earnings-yield rank and the rank on ``second`` (both desc.)."""
    by_yield = sorted(eligible, key=lambda r: (-r.earnings_yield, r.isin))
    by_second = sorted(eligible, key=lambda r: (-second(r), r.isin))
    yield_rank = {r.isin: i for i, r in enumerate(by_yield)}
    second_rank = {r.isin: i for i, r in enumerate(by_second)}
    return sorted(eligible, key=lambda r: (yield_rank[r.isin] + second_rank[r.isin], r.isin))


class FundamentalsValuePolicy:
    """Top-N on a stated fundamentals signal, with the momentum v2 mechanics (M10.6).

    Satisfies :class:`backtest.replay.Policy`; reads data only through ``ctx.pit`` (invariant #7),
    the book only through ``ctx.broker`` (invariant #5), time only through ``ctx.clock`` (B10).
    """

    __slots__ = ("_data", "_params")

    def __init__(
        self, data: FundamentalsSignalData, params: FundamentalsValueParameters | None = None
    ) -> None:
        self._data = data
        self._params = params if params is not None else FundamentalsValueParameters()

    def decide(self, ctx: SessionContext) -> SessionDecision:
        """A heartbeat off a rebalance, a full rebalance on one."""
        if not self._data.is_rebalance(ctx.session):
            return self._heartbeat(ctx)
        return self._rebalance(ctx)

    def _heartbeat(self, ctx: SessionContext) -> SessionDecision:
        holdings = ctx.broker.holdings()
        evidence = EvidenceBundle(
            trading_date=ctx.session,
            actor=Actor.T0,
            items=(
                EvidenceItem(
                    kind=EvidenceKind.POSITION,
                    source="book",
                    label="held_names",
                    value=Decimal(len(holdings)),
                    text="no rebalance due this session",
                ),
            ),
        )
        return SessionDecision(evidence=evidence)

    def _rebalance(self, ctx: SessionContext) -> SessionDecision:
        candidates = ctx.pit.admit(self._data.signal(ctx.session))
        ranked = rank_candidates(
            candidates,
            signal=self._params.signal,
            session=ctx.session,
            max_staleness_days=self._params.max_staleness_days,
        )
        chosen = ranked[: self._params.top_n]
        band = self._params.sell_band if self._params.sell_band is not None else self._params.top_n
        keep = {r.isin for r in ranked[:band]}
        target = {r.isin: r for r in chosen}
        prices = {r.isin: r.price for r in chosen}

        held = {holding.isin: holding for holding in ctx.broker.holdings()}
        sells = self._sells(held, keep, band)
        buys, drift = self._buys(ctx, held, target, prices)
        orders = tuple(order for order, _ in (*sells, *buys))
        entries = tuple(self._entry(ctx, order, note) for order, note in (*sells, *buys))
        evidence = self._evidence(ctx.session, chosen, drift, universe=len(ranked))
        return SessionDecision(evidence=evidence, orders=orders, entries=entries)

    def _sells(
        self, held: Mapping[str, Holding], keep: set[str], band: int
    ) -> list[tuple[OrderRequest, str]]:
        sells: list[tuple[OrderRequest, str]] = []
        for isin in sorted(held):
            if isin in keep:
                continue
            quantity = held[isin].quantity
            sells.append(
                (
                    OrderRequest(isin=isin, side=Side.SELL, quantity=quantity, tag="FUNDAMENTALS"),
                    f"left the top-{band} {self._params.signal.value} band; "
                    f"liquidating {quantity} shares",
                )
            )
        return sells

    def _buys(
        self,
        ctx: SessionContext,
        held: Mapping[str, Holding],
        target: Mapping[str, FundamentalsRecord],
        prices: Mapping[str, Decimal],
    ) -> tuple[list[tuple[OrderRequest, str]], Decimal]:
        if not target:
            return [], _ZERO
        budget = ctx.broker.margins().available * self._params.buy_budget_fraction
        weights = _equal_weights(sorted(target))
        existing_value = {
            isin: Decimal(held[isin].quantity) * prices[isin] for isin in target if isin in held
        }
        allocation = simulate_sip_instalment(
            instalment=budget, targets=weights, prices=prices, existing_value=existing_value
        )
        buys = [
            (
                order.to_order_request(exchange=Exchange.NSE, tag="FUNDAMENTALS"),
                f"top-{self._params.top_n} {self._params.signal.value} "
                f"(earnings yield {target[order.isin].earnings_yield:+.4f}); "
                f"buy {order.quantity} @ {order.price}",
            )
            for order in allocation.orders
        ]
        return buys, allocation.tracking_drift

    def _entry(self, ctx: SessionContext, order: OrderRequest, rationale: str) -> JournalEntry:
        decision = Decision.BUY if order.side is Side.BUY else Decision.SELL
        return JournalEntry(
            ts=ctx.clock.now(),
            trading_date=ctx.session,
            actor=Actor.T0,
            decision=decision,
            isin=order.isin,
            sleeve=self._params.sleeve,
            rationale=rationale,
        )

    def _evidence(
        self,
        session: date,
        chosen: list[FundamentalsRecord],
        tracking_drift: Decimal,
        *,
        universe: int,
    ) -> EvidenceBundle:
        items = [
            EvidenceItem(
                kind=EvidenceKind.FUNDAMENTAL,
                source="pit_fundamentals",
                label=f"{self._params.signal.value.lower()}_rank",
                isin=record.isin,
                as_of=record.knowable_date,
                value=record.earnings_yield,
                detail={
                    "price": str(record.price),
                    "earnings_growth": str(record.earnings_growth),
                    "roe": str(record.roe),
                    "momentum_12_1": str(record.momentum_12_1),
                    "rank": str(rank),
                },
            )
            for rank, record in enumerate(chosen, start=1)
        ]
        items.append(
            EvidenceItem(
                kind=EvidenceKind.POSITION,
                source="book",
                label="tracking_drift",
                as_of=session,
                value=tracking_drift,
                text=f"equal-weight top-{self._params.top_n} {self._params.signal.value} "
                f"rebalance over {universe} rankable names",
            )
        )
        return EvidenceBundle(trading_date=session, actor=Actor.T0, items=tuple(items))


def _equal_weights(isins: list[str]) -> dict[str, Decimal]:
    """Equal weights that sum to exactly 1 — the last name absorbs the quantisation residue."""
    if not isins:
        raise ValueError("cannot weight an empty basket")
    n = len(isins)
    each = (_ONE / Decimal(n)).quantize(_WEIGHT_QUANTUM)
    weights = dict.fromkeys(isins[:-1], each)
    weights[isins[-1]] = _ONE - each * (n - 1)
    return weights
