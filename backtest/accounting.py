"""X2: the portfolio book — positions, cash ledger, P&L, corporate actions, XIRR, benchmarks.

This is the ledger a backtest (and, read the same way, a paper run) keeps as it walks a strategy
forward: what is held, how much cash is free, what has been made or lost, and — the part paper
portfolios quietly get wrong — what a split or a demerger does to the book. Every figure that is
money is a ``Decimal`` and every share count is an ``int`` (CLAUDE.md); the only join key is the
ISIN (invariant #2); nothing here reads a wall clock — dates arrive with the events.

**Cost basis and realized P&L.** A buy adds its full cash cost — turnover *plus* the shared cost
model's charges (invariant #4, carried on the ``Fill``) — to the position's cost basis. A sell
removes cost basis *proportionally* to the shares sold and books the difference against the net
proceeds (turnover minus sell-side charges) as realized P&L. Proportional removal, not a re-rounded
average price, is what keeps a partial sell from leaking a paisa of basis. Unrealized P&L is the
mark-to-market of what is still held against that basis.

**Corporate actions in the book (the part that silently breaks).** A split or bonus multiplies the
share count and divides the per-share basis by the *same* factor, so the position's total value and
total cost basis are unchanged — a split turns 100 shares worth ₹5,000 into 500 shares worth
₹5,000, never into 500 shares worth ₹25,000. A demerger *creates a new position* in the resulting
entity and moves a fraction of the parent's cost basis into it: value is redistributed across two
ISINs, never destroyed. Getting either wrong is how a paper portfolio shows a phantom gain or loses
a holding outright, so both are modelled explicitly and tested against exactly those failures.

**XIRR and benchmarks.** External cashflows — the investor's SIP instalments in, any withdrawals
out — plus the terminal mark-to-market value form the stream whose XIRR (``backtest/xirr.py``) is
the portfolio's money-weighted return. The same external stream, replayed into a benchmark total-
return series, gives that benchmark's XIRR on identical timing, so the comparison the plan asks for
(EXECUTION_PLAN §5.2, "NIFTY-TRI + theme proxy") is apples-to-apples: same money, same dates, two
places it could have gone.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import structlog

from backtest.xirr import Cashflow, xirr
from dataplatform.ingest.indices import TriSeries
from execution.broker import Fill, LedgerEntry, Side

_ZERO = Decimal("0")

_log = structlog.get_logger(__name__)

__all__ = [
    "BenchmarkComparison",
    "BookError",
    "BookPosition",
    "CorporateActionError",
    "InsufficientCashError",
    "InsufficientSharesError",
    "PortfolioBook",
    "PriceUnavailableError",
]


# ── errors ─────────────────────────────────────────────────────────────────────────────────────


class BookError(Exception):
    """Base for every refusal the book makes. The book fails loud (CLAUDE.md), never silently."""


class InsufficientCashError(BookError):
    """A buy or withdrawal asked for more cash than the book holds."""


class InsufficientSharesError(BookError):
    """A sell or corporate action referenced more shares than the position holds (or none)."""


class CorporateActionError(BookError):
    """A corporate action cannot be applied to the book as stated.

    Raised for terms that would not produce a whole share count (a split ratio that leaves a
    fractional holding) or a cost allocation outside ``[0, 1]`` — an event the book cannot honour
    without inventing or destroying value, which is the exact failure this module exists to catch.
    """


class PriceUnavailableError(BookError):
    """A valuation or benchmark lookup needed a price/level for a date or ISIN that was not given.

    Marking to market with a missing price is guessed value; the book refuses rather than treat an
    absent price as zero and silently write down a holding.
    """


# ── position ─────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BookPosition:
    """A holding in the book: how many shares, and the total cost basis behind them.

    ``cost_basis`` is the whole rupee cost of the shares still held, buy-side charges included; the
    average price is derived from it rather than stored, so a split (which changes the count but not
    the basis) needs to touch only two numbers and cannot leave the two inconsistent. Keyed by ISIN
    (invariant #2).
    """

    isin: str
    quantity: int
    cost_basis: Decimal

    @property
    def average_price(self) -> Decimal:
        """Cost basis per share, charges included. Zero-guarded: an empty position has none."""
        if self.quantity == 0:
            return _ZERO
        return self.cost_basis / self.quantity


# ── benchmark comparison result ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BenchmarkComparison:
    """The portfolio's money-weighted return beside the two benchmarks', on identical cashflows.

    All three XIRRs are computed from the *same* external cashflow schedule — the investor's actual
    instalments and withdrawals — so the excess figures are a like-for-like read of what the
    strategy added over simply buying the index or the theme proxy (EXECUTION_PLAN §5.2).
    """

    portfolio_xirr: Decimal
    benchmark_xirr: Decimal
    theme_xirr: Decimal

    @property
    def excess_over_benchmark(self) -> Decimal:
        """Portfolio XIRR minus the broad-market (NIFTY-TRI) benchmark XIRR."""
        return self.portfolio_xirr - self.benchmark_xirr

    @property
    def excess_over_theme(self) -> Decimal:
        """Portfolio XIRR minus the theme-proxy benchmark XIRR."""
        return self.portfolio_xirr - self.theme_xirr


# ── the book ───────────────────────────────────────────────────────────────────────────────────


class PortfolioBook:
    """A running portfolio: cash, positions, realized P&L, a ledger, and the external SIP stream.

    What it does: post fills (from the shared ``Broker``/``SimBroker`` fill model, so costs come
    from the one cost model — invariant #4), record the investor's deposits and withdrawals, apply
    splits/bonuses/demergers to the holdings, mark to market, and report XIRR against a benchmark
    pair.

    What it assumes: fills, deposits and corporate actions arrive in chronological order — the book
    is a forward walk, and it does not re-sort history. Dates come from the events (a ``Fill`` knows
    its session), never from a clock the book reads (invariant #11).

    What it never does: let a corporate action change the total value of the book, cover a buy the
    cash cannot pay for, or mark a holding at a price it was not given.
    """

    def __init__(self, opening_cash: Decimal = _ZERO) -> None:
        if not isinstance(opening_cash, Decimal):
            raise TypeError("opening_cash must be a Decimal — money is never float (CLAUDE.md)")
        if opening_cash < _ZERO:
            raise ValueError(f"opening cash must not be negative, got {opening_cash}")
        self._cash: Decimal = opening_cash
        self._positions: dict[str, BookPosition] = {}
        self._realized: Decimal = _ZERO
        self._ledger: list[LedgerEntry] = []
        #: External cashflows in XIRR sign convention: pay-in negative, pay-out positive.
        self._external: list[Cashflow] = []
        self._seq: int = 0

    # -- reads -----------------------------------------------------------------------------------

    @property
    def cash(self) -> Decimal:
        """Free cash in the book, in rupees."""
        return self._cash

    @property
    def realized_pnl(self) -> Decimal:
        """Cumulative realized P&L booked on sells so far, net of sell-side charges."""
        return self._realized

    def positions(self) -> tuple[BookPosition, ...]:
        """Open positions with a non-zero share count, ordered by ISIN for a stable read."""
        return tuple(
            self._positions[isin]
            for isin in sorted(self._positions)
            if self._positions[isin].quantity != 0
        )

    def position(self, isin: str) -> BookPosition | None:
        """The position in ``isin``, or ``None`` if the book holds none."""
        held = self._positions.get(isin)
        if held is None or held.quantity == 0:
            return None
        return held

    def ledger(self) -> tuple[LedgerEntry, ...]:
        """The cash ledger in posting order — append-only (invariant #12)."""
        return tuple(self._ledger)

    # -- external cashflows (the SIP stream) -----------------------------------------------------

    def deposit(self, when: date, amount: Decimal) -> None:
        """Record cash paid into the account (a SIP instalment). Increases free cash.

        In XIRR terms this is money the investor pays in, so it enters the return stream as a
        *negative* flow; in the cash ledger it is a credit to the account.
        """
        amount = self._require_positive_money("deposit", amount)
        self._cash += amount
        self._external.append(Cashflow(when, -amount))
        self._post_ledger(when, "", "deposit", debit=_ZERO, credit=amount)

    def withdraw(self, when: date, amount: Decimal) -> None:
        """Record cash paid out of the account. Decreases free cash; refuses to overdraw.

        Enters the return stream as a *positive* flow (money back to the investor) and debits the
        cash ledger.
        """
        amount = self._require_positive_money("withdraw", amount)
        if amount > self._cash:
            raise InsufficientCashError(f"withdraw {amount} exceeds free cash {self._cash}")
        self._cash -= amount
        self._external.append(Cashflow(when, amount))
        self._post_ledger(when, "", "withdraw", debit=amount, credit=_ZERO)

    # -- fills -----------------------------------------------------------------------------------

    def record_fill(self, fill: Fill) -> None:
        """Post a broker fill: move cash, update the position's basis, book any realized P&L.

        The fill already carries the shared cost model's full charge breakdown (invariant #4), so
        the book never recomputes costs — a buy debits turnover plus charges and adds that to the
        position's cost basis; a sell credits turnover minus charges and books the gain or loss
        against the proportional slice of basis it removes. Refuses a buy the cash cannot cover and
        a sell of more than is held, rather than going cash- or share-negative.
        """
        if fill.side is Side.BUY:
            self._record_buy(fill)
        else:
            self._record_sell(fill)

    def _record_buy(self, fill: Fill) -> None:
        cost = fill.cost.net_amount  # turnover + charges, positive
        if cost > self._cash:
            raise InsufficientCashError(
                f"buy of {fill.quantity} {fill.isin} costs {cost}, free cash is {self._cash}"
            )
        self._cash -= cost
        held = self._positions.get(fill.isin)
        if held is None:
            self._positions[fill.isin] = BookPosition(fill.isin, fill.quantity, cost)
        else:
            self._positions[fill.isin] = BookPosition(
                fill.isin, held.quantity + fill.quantity, held.cost_basis + cost
            )
        self._post_ledger(fill.session, fill.isin, "buy", debit=cost, credit=_ZERO)

    def _record_sell(self, fill: Fill) -> None:
        held = self._positions.get(fill.isin)
        if held is None or held.quantity < fill.quantity:
            have = 0 if held is None else held.quantity
            raise InsufficientSharesError(
                f"sell of {fill.quantity} {fill.isin} but only {have} held"
            )
        proceeds = fill.cost.net_amount  # turnover - charges, positive
        # Remove cost basis in proportion to the shares sold — not via a re-rounded average — so a
        # partial sell leaves exactly the right basis behind on the remaining shares.
        removed_basis = held.cost_basis * fill.quantity / held.quantity
        self._realized += proceeds - removed_basis
        self._cash += proceeds
        remaining_qty = held.quantity - fill.quantity
        remaining_basis = held.cost_basis - removed_basis
        self._positions[fill.isin] = BookPosition(fill.isin, remaining_qty, remaining_basis)
        self._post_ledger(fill.session, fill.isin, "sell", debit=_ZERO, credit=proceeds)

    # -- corporate actions in the book -----------------------------------------------------------

    def apply_split(self, isin: str, *, from_face_value: Decimal, to_face_value: Decimal) -> None:
        """A stock split: face value ``from → to`` multiplies the share count, basis unchanged.

        The quantity multiple is ``from / to`` (a ₹10→₹2 split gives 5x), exactly the arithmetic
        the adjustment engine uses (``dataplatform.corpactions.factors``). Total cost basis is
        untouched, so the per-share basis divides by the same factor and the position's *value* does
        not move — the invariant a split most often breaks. Refuses a ratio that would leave a
        fractional holding.
        """
        self._require_decimal("from_face_value", from_face_value)
        self._require_decimal("to_face_value", to_face_value)
        if from_face_value <= _ZERO or to_face_value <= _ZERO:
            raise CorporateActionError("split face values must be positive")
        self._rescale_quantity(isin, from_face_value / to_face_value, "split")

    def apply_bonus(self, isin: str, *, new_shares: Decimal, held_shares: Decimal) -> None:
        """A bonus issue ``new:held``: adds free shares, basis unchanged, value preserved.

        A ``1:1`` bonus leaves a holder of 1 share with 2, so the quantity multiple is
        ``(new + held) / held`` — the additive arithmetic, kept distinct from a split's replacement
        arithmetic exactly as the taxonomy keeps ``RatioTerms`` distinct from ``FaceValueTerms``.
        Cost basis does not change; the per-share basis dilutes across the larger count.
        """
        self._require_decimal("new_shares", new_shares)
        self._require_decimal("held_shares", held_shares)
        if new_shares <= _ZERO or held_shares <= _ZERO:
            raise CorporateActionError("bonus ratio terms must be positive")
        self._rescale_quantity(isin, (new_shares + held_shares) / held_shares, "bonus")

    def apply_demerger(
        self,
        parent_isin: str,
        *,
        resulting_isin: str,
        shares_received: Decimal,
        shares_held: Decimal,
        cost_fraction_to_resulting: Decimal,
    ) -> None:
        """A demerger: create a position in ``resulting_isin`` and split the parent's basis into it.

        The holder keeps the parent shares and receives ``shares_received`` of the resulting entity
        for every ``shares_held`` held (``ExchangeRatioTerms``' replacement-shaped ratio). A
        fraction of the parent's cost basis — ``cost_fraction_to_resulting``, established from the
        relative fair values on the demerger date — moves to the new position; the parent keeps the
        rest. The *sum* of the two cost bases equals the parent's basis before, so no value is
        created or lost: it is redistributed across two ISINs. This is the event that most often
        loses a holding in a naive book, so the whole-share and value checks are explicit.
        """
        if parent_isin == resulting_isin:
            raise CorporateActionError("a demerger's resulting ISIN must differ from the parent")
        if resulting_isin in self._positions and self._positions[resulting_isin].quantity != 0:
            raise CorporateActionError(
                f"cannot demerge into {resulting_isin}: the book already holds it"
            )
        self._require_decimal("shares_received", shares_received)
        self._require_decimal("shares_held", shares_held)
        self._require_decimal("cost_fraction_to_resulting", cost_fraction_to_resulting)
        if shares_received <= _ZERO or shares_held <= _ZERO:
            raise CorporateActionError("demerger exchange ratio terms must be positive")
        if not (_ZERO <= cost_fraction_to_resulting <= Decimal("1")):
            raise CorporateActionError(
                f"cost fraction to the resulting entity must be in [0, 1], got "
                f"{cost_fraction_to_resulting}"
            )
        parent = self._positions.get(parent_isin)
        if parent is None or parent.quantity == 0:
            raise InsufficientSharesError(
                f"cannot apply demerger: no position in parent {parent_isin}"
            )

        new_quantity = self._whole_shares(
            parent.quantity * shares_received / shares_held, "demerger"
        )
        resulting_basis = parent.cost_basis * cost_fraction_to_resulting
        parent_basis = parent.cost_basis - resulting_basis
        self._positions[parent_isin] = BookPosition(parent_isin, parent.quantity, parent_basis)
        self._positions[resulting_isin] = BookPosition(
            resulting_isin, new_quantity, resulting_basis
        )
        _log.info(
            "book.demerger",
            parent=parent_isin,
            resulting=resulting_isin,
            resulting_quantity=new_quantity,
            cost_moved=str(resulting_basis),
        )

    def _rescale_quantity(self, isin: str, multiple: Decimal, event: str) -> None:
        """Multiply a position's share count by ``multiple``, holding its cost basis fixed."""
        held = self._positions.get(isin)
        if held is None or held.quantity == 0:
            raise InsufficientSharesError(f"cannot apply {event}: no position in {isin}")
        new_quantity = self._whole_shares(held.quantity * multiple, event)
        self._positions[isin] = BookPosition(isin, new_quantity, held.cost_basis)
        _log.info(
            "book.corporate_action",
            ca_event=event,
            isin=isin,
            old_quantity=held.quantity,
            new_quantity=new_quantity,
        )

    # -- valuation --------------------------------------------------------------------------------

    def market_value(self, prices: Mapping[str, Decimal]) -> Decimal:
        """Mark-to-market value of the open positions at ``prices`` (ISIN → price).

        Refuses rather than guesses: a held ISIN missing from ``prices`` raises, because treating a
        missing price as zero would silently write the holding off.
        """
        total = _ZERO
        for pos in self.positions():
            price = prices.get(pos.isin)
            if price is None:
                raise PriceUnavailableError(f"no price for held ISIN {pos.isin}")
            self._require_decimal("price", price)
            total += price * pos.quantity
        return total

    def net_asset_value(self, prices: Mapping[str, Decimal]) -> Decimal:
        """Free cash plus the marked-to-market value of the holdings."""
        return self._cash + self.market_value(prices)

    def unrealized_pnl(self, prices: Mapping[str, Decimal]) -> Decimal:
        """Mark-to-market value of the open positions minus their cost basis."""
        basis = sum((pos.cost_basis for pos in self.positions()), _ZERO)
        return self.market_value(prices) - basis

    # -- returns ----------------------------------------------------------------------------------

    def xirr(self, as_of: date, prices: Mapping[str, Decimal]) -> Decimal:
        """The portfolio's money-weighted return: external cashflows plus the terminal NAV.

        The stream is every deposit (a pay-in, negative), every withdrawal (a pay-out, positive) and
        one final positive flow equal to the net asset value on ``as_of`` — the value the investor
        could realize if they stopped there. Its XIRR is the return the plan reports and benchmarks
        against (EXECUTION_PLAN §5.2). Raises ``XIRRError`` if the stream has no defined rate.
        """
        stream = [*self._external, Cashflow(as_of, self.net_asset_value(prices))]
        return xirr(stream)

    def compare_to_benchmarks(
        self,
        as_of: date,
        prices: Mapping[str, Decimal],
        *,
        benchmark: TriSeries,
        theme: TriSeries,
    ) -> BenchmarkComparison:
        """Portfolio XIRR beside the two benchmarks', all on the investor's actual cashflows.

        Each benchmark's return is what the *same* deposits and withdrawals would have earned put
        into that total-return index instead — units bought at the index level on each pay-in date,
        sold on each pay-out date, the remainder marked at the ``as_of`` level — so the comparison
        holds money and timing identical and varies only the destination (EXECUTION_PLAN §5.2).
        """
        return BenchmarkComparison(
            portfolio_xirr=self.xirr(as_of, prices),
            benchmark_xirr=self._benchmark_xirr(benchmark, as_of),
            theme_xirr=self._benchmark_xirr(theme, as_of),
        )

    def _benchmark_xirr(self, series: TriSeries, as_of: date) -> Decimal:
        """XIRR of the external cashflows replayed into one total-return series."""
        units = _ZERO
        for flow in self._external:
            level = _tri_level_asof(series, flow.when)
            # flow.amount < 0 is a pay-in (buy units); > 0 is a pay-out (sell units).
            units -= flow.amount / level
        terminal = units * _tri_level_asof(series, as_of)
        stream = [*self._external, Cashflow(as_of, terminal)]
        return xirr(stream)

    # -- internals --------------------------------------------------------------------------------

    def _post_ledger(
        self, when: date, isin: str, description: str, *, debit: Decimal, credit: Decimal
    ) -> None:
        self._seq += 1
        self._ledger.append(
            LedgerEntry(
                seq=self._seq,
                session=when,
                isin=isin,
                description=description,
                debit=debit,
                credit=credit,
                balance=self._cash,
            )
        )

    @staticmethod
    def _require_decimal(name: str, value: object) -> Decimal:
        if not isinstance(value, Decimal):
            raise TypeError(f"{name} must be a Decimal — money is never float (CLAUDE.md)")
        return value

    def _require_positive_money(self, name: str, amount: Decimal) -> Decimal:
        self._require_decimal(name, amount)
        if amount <= _ZERO:
            raise ValueError(f"{name} amount must be positive, got {amount}")
        return amount

    @staticmethod
    def _whole_shares(value: Decimal, event: str) -> int:
        """Return ``value`` as a whole share count or raise — a CA that leaves a fraction is a bug.

        A split/bonus/demerger over an integer holding should produce an integer holding; a
        fractional result means the terms and the holding do not line up, which the book refuses
        rather than silently rounding into a different position than the arithmetic gives.
        """
        rounded = value.to_integral_value()
        if value != rounded:
            raise CorporateActionError(
                f"{event} would leave a fractional holding ({value}); terms and holding disagree"
            )
        return int(rounded)


def _tri_level_asof(series: TriSeries, on: date) -> Decimal:
    """The total-return index level in force on ``on`` — the last point dated on or before it.

    A step function, PIT-style: the benchmark's value on a non-index day is its last published
    level, not an interpolation. Raises if ``on`` precedes the series' first point, since there is
    no level to value a cashflow that predates the benchmark's own history.
    """
    level: Decimal | None = None
    for point in series.points:  # points are ascending by construction (TriSeries validates)
        if point.as_of <= on:
            level = point.tri_value
        else:
            break
    if level is None:
        raise PriceUnavailableError(
            f"no benchmark level for {on.isoformat()}: it precedes the series' first point"
        )
    return level
