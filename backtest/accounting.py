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

**Cash dividends** are credited to free cash on the ex-date, per share held at the close before it
(:meth:`PortfolioBook.credit_dividend`). They are portfolio income, not an external flow: they raise
NAV exactly as far as the ex-date price drop lowers it, and they never enter the XIRR stream as a
pay-out. The walk that feeds this book its actions is ``backtest.book_actions``.

**XIRR and benchmarks.** External cashflows — the investor's SIP instalments in, any withdrawals
out — plus the terminal mark-to-market value form the stream whose XIRR (``backtest/xirr.py``) is
the portfolio's money-weighted return. The same external stream, replayed into a benchmark total-
return series, gives that benchmark's XIRR on identical timing, so the comparison the plan asks for
(EXECUTION_PLAN §5.2, "NIFTY-TRI + theme proxy") is apples-to-apples: same money, same dates, two
places it could have gone.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal

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
    splits/bonuses/mergers/demergers to the holdings, mark to market, and report XIRR against a
    benchmark pair.

    What it assumes: fills, deposits and corporate actions arrive in chronological order — the book
    is a forward walk, and it does not re-sort history. Dates come from the events (a ``Fill`` knows
    its session), never from a clock the book reads (invariant #11).

    What it never does: let a corporate action change the total value of the book (the one stated
    exception is a forfeited fractional entitlement, see ``_rescale_quantity``), cover a buy the
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
        self._dividends: Decimal = _ZERO
        self._interest: Decimal = _ZERO
        self._ledger: list[LedgerEntry] = []
        #: External cashflows in XIRR sign convention: pay-in negative, pay-out positive.
        self._external: list[Cashflow] = []
        self._seq: int = 0

    @classmethod
    def seeded(cls, cash: Decimal, positions: Iterable[BookPosition]) -> PortfolioBook:
        """A book that starts holding ``positions`` with ``cash`` — a mirror restored from state.

        For a forward runner that persists its broker between processes (the daily paper session,
        M13.1) and needs the share-count mirror corporate actions are applied against. The mirror
        starts with no history: realized P&L, income and external flows count from here.
        """
        book = cls(cash)
        for position in positions:
            if position.quantity <= 0:
                raise ValueError(f"a seeded position must hold shares, got {position!r}")
            book._positions[position.isin] = position
        return book

    # -- reads -----------------------------------------------------------------------------------

    @property
    def cash(self) -> Decimal:
        """Free cash in the book, in rupees."""
        return self._cash

    @property
    def realized_pnl(self) -> Decimal:
        """Cumulative realized P&L booked on sells so far, net of sell-side charges."""
        return self._realized

    @property
    def dividend_income(self) -> Decimal:
        """Cumulative cash dividends credited so far (gross — no TDS is modelled here)."""
        return self._dividends

    @property
    def interest_income(self) -> Decimal:
        """Cumulative interest on idle cash credited so far (gross; ``backtest.cash_interest``)."""
        return self._interest

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

    @property
    def external_flows(self) -> tuple[Cashflow, ...]:
        """Every deposit (negative) and withdrawal (positive), in order — the XIRR stream."""
        return tuple(self._external)

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

    def apply_split(
        self,
        isin: str,
        *,
        from_face_value: Decimal,
        to_face_value: Decimal,
        forfeit_fraction: bool = False,
    ) -> None:
        """A stock split: face value ``from → to`` multiplies the share count, basis unchanged.

        The quantity multiple is ``from / to`` (a ₹10→₹2 split gives 5x), exactly the arithmetic
        the adjustment engine uses (``dataplatform.corpactions.factors``). Total cost basis is
        untouched, so the per-share basis divides by the same factor and the position's *value* does
        not move — the invariant a split most often breaks. Refuses a ratio that would leave a
        fractional holding, unless ``forfeit_fraction`` (see :meth:`_rescale_quantity`).
        """
        self._require_decimal("from_face_value", from_face_value)
        self._require_decimal("to_face_value", to_face_value)
        if from_face_value <= _ZERO or to_face_value <= _ZERO:
            raise CorporateActionError("split face values must be positive")
        self._rescale_quantity(
            isin,
            numerator=from_face_value,
            denominator=to_face_value,
            event="split",
            forfeit_fraction=forfeit_fraction,
        )

    def apply_bonus(
        self,
        isin: str,
        *,
        new_shares: Decimal,
        held_shares: Decimal,
        forfeit_fraction: bool = False,
    ) -> None:
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
        self._rescale_quantity(
            isin,
            numerator=new_shares + held_shares,
            denominator=held_shares,
            event="bonus",
            forfeit_fraction=forfeit_fraction,
        )

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

    def apply_merger(
        self,
        acquired_isin: str,
        *,
        surviving_isin: str,
        shares_received: Decimal,
        shares_held: Decimal,
        forfeit_fraction: bool = False,
    ) -> int:
        """A merger: the acquired entity's shares convert to the surviving entity's, basis carried.

        The holder of the *acquired* (amalgamating) company receives ``shares_received`` of the
        surviving entity for every ``shares_held`` held (``ExchangeRatioTerms``' replacement ratio —
        HDFC Ltd into HDFC Bank was 42:25). The acquired position ceases to exist and its **whole**
        cost basis moves to the surviving entity: a merger redistributes nothing, it re-labels a
        holding, so the sum of value across the two ISINs before and after is identical. If the book
        already holds the surviving entity, the converted shares and basis are added to it.

        This is the mirror of the surviving-entity view the golden CA suite verifies (unit price
        factor, bridged return): on the *survivor's* own price series a merger is a structural
        break, not a scaling, so nothing here touches a price — it moves a share count and its basis
        from the dead ISIN to the live one. Leaving the acquired holding parked on a dead line is
        how a naive book silently loses a position, which is exactly the failure this refuses.
        Refuses a ratio that would leave a fractional holding, unless ``forfeit_fraction`` — the
        walk's case, where the holding is whatever the strategy bought (see
        :meth:`_rescale_quantity`): the fraction is floored away, and a holding floored to nothing
        books its whole basis as a realized loss and leaves the survivor untouched. Returns the
        surviving-entity shares the conversion added.
        """
        if acquired_isin == surviving_isin:
            raise CorporateActionError("a merger's surviving ISIN must differ from the acquired")
        self._require_decimal("shares_received", shares_received)
        self._require_decimal("shares_held", shares_held)
        if shares_received <= _ZERO or shares_held <= _ZERO:
            raise CorporateActionError("merger exchange ratio terms must be positive")
        acquired = self._positions.get(acquired_isin)
        if acquired is None or acquired.quantity == 0:
            raise InsufficientSharesError(
                f"cannot apply merger: no position in acquired {acquired_isin}"
            )

        exact = acquired.quantity * shares_received / shares_held
        if forfeit_fraction:
            converted_quantity = int(exact.to_integral_value(rounding=ROUND_FLOOR))
        else:
            converted_quantity = self._whole_shares(exact, "merger")
        moved_basis = acquired.cost_basis
        if converted_quantity == 0:
            self._realized -= moved_basis
            self._positions[acquired_isin] = BookPosition(acquired_isin, 0, _ZERO)
            _log.info(
                "book.merger",
                acquired=acquired_isin,
                surviving=surviving_isin,
                converted_quantity=0,
                basis_forfeited=str(moved_basis),
            )
            return 0
        surviving = self._positions.get(surviving_isin)
        if surviving is None or surviving.quantity == 0:
            new_quantity = converted_quantity
            new_basis = moved_basis
        else:
            new_quantity = surviving.quantity + converted_quantity
            new_basis = surviving.cost_basis + moved_basis
        self._positions[acquired_isin] = BookPosition(acquired_isin, 0, _ZERO)
        self._positions[surviving_isin] = BookPosition(surviving_isin, new_quantity, new_basis)
        _log.info(
            "book.merger",
            acquired=acquired_isin,
            surviving=surviving_isin,
            converted_quantity=converted_quantity,
            basis_moved=str(moved_basis),
        )
        return converted_quantity

    def apply_cash_exit(self, when: date, isin: str, *, price: Decimal) -> Decimal:
        """A delisting exit: every share of ``isin`` is surrendered at ``price``; return the cash.

        The position closes like a sale with no charges — the proceeds are credited to cash and
        ``proceeds - basis`` is realized — because that is what the residual shareholder who
        tenders in the exit window receives. Refuses an exit on a name the book does not hold.
        """
        self._require_decimal("price", price)
        if price <= _ZERO:
            raise CorporateActionError(f"exit price must be positive, got {price}")
        held = self._positions.get(isin)
        if held is None or held.quantity == 0:
            raise InsufficientSharesError(f"cannot apply cash exit: no position in {isin}")
        proceeds = price * held.quantity
        self._realized += proceeds - held.cost_basis
        self._cash += proceeds
        self._positions[isin] = BookPosition(isin, 0, _ZERO)
        self._post_ledger(when, isin, "cash exit", debit=_ZERO, credit=proceeds)
        _log.info(
            "book.cash_exit",
            isin=isin,
            session=when.isoformat(),
            quantity=held.quantity,
            price=str(price),
            proceeds=str(proceeds),
        )
        return proceeds

    def credit_dividend(self, when: date, isin: str, *, per_share: Decimal) -> Decimal:
        """Credit a cash dividend of ``per_share`` on every share of ``isin`` held; return the cash.

        Called on the ex-date, before that session's fills: the shares held then are exactly the
        ones bought before the ex-date and not yet sold, which is the entitlement the exchange's
        record date fixes. Crediting on the ex-date rather than the payment date (up to 30 days
        later, and not in the store) is what keeps NAV continuous — the price drops by the dividend
        on the ex-date and the cash arrives the same day — at the cost of making the cash available
        to reinvest a few weeks early. Gross: dividend TDS is a tax, and taxes are post-processed.

        Income, not an external flow: it raises cash and :attr:`dividend_income`, never the XIRR
        stream. Refuses a dividend on a name the book does not hold — an entitlement with no
        holding is a wiring bug, not a zero.
        """
        self._require_decimal("per_share", per_share)
        if per_share <= _ZERO:
            raise CorporateActionError(f"dividend per share must be positive, got {per_share}")
        held = self._positions.get(isin)
        if held is None or held.quantity == 0:
            raise InsufficientSharesError(f"cannot credit dividend: no position in {isin}")
        amount = per_share * held.quantity
        self._cash += amount
        self._dividends += amount
        self._post_ledger(when, isin, "dividend", debit=_ZERO, credit=amount)
        _log.info(
            "book.dividend",
            isin=isin,
            session=when.isoformat(),
            quantity=held.quantity,
            per_share=str(per_share),
            amount=str(amount),
        )
        return amount

    def credit_interest(self, when: date, amount: Decimal) -> None:
        """Credit ``amount`` of interest on idle cash (``backtest.cash_interest``) to free cash.

        Income, not an external flow, exactly as a dividend is: it raises cash and
        :attr:`interest_income`, never the XIRR stream. Gross — its tax is post-processed.
        """
        amount = self._require_positive_money("interest", amount)
        self._cash += amount
        self._interest += amount
        self._post_ledger(when, "", "interest", debit=_ZERO, credit=amount)

    def credit_scheme_cash(self, when: date, isin: str, amount: Decimal) -> None:
        """Credit the non-share leg of an amalgamation (``backtest.book_actions``) to free cash.

        A scheme that pays part of its consideration in a security the book cannot hold (Cairn
        India's Vedanta redeemable preference shares) is carried as cash at that security's sourced
        face value. No basis is apportioned to it: the whole cost of the old holding moves to the
        survivor's shares (:meth:`apply_merger`), so the cash is realized in full. Total P&L is the
        same either way; only its split between realized and unrealized depends on the choice.
        ``isin`` is the *old* (amalgamated) ISIN, for the ledger row.
        """
        amount = self._require_positive_money("scheme cash", amount)
        self._cash += amount
        self._realized += amount
        self._post_ledger(when, isin, "scheme cash", debit=_ZERO, credit=amount)
        _log.info("book.scheme_cash", isin=isin, session=when.isoformat(), amount=str(amount))

    def _rescale_quantity(
        self,
        isin: str,
        *,
        numerator: Decimal,
        denominator: Decimal,
        event: str,
        forfeit_fraction: bool = False,
    ) -> None:
        """Scale a position's share count by ``numerator / denominator``, cost basis fixed.

        The ratio is applied as ``quantity * numerator / denominator`` — multiply first, divide
        last — so the arithmetic stays exact whenever the true result is a whole number. Dividing
        first would turn a 1:3 bonus into the repeating ``1.333…`` and 300 shares into
        ``399.999…``, which ``_whole_shares`` then rightly refuses: a valid corporate action
        rejected by a rounding artefact. The property suite (``tests/property/test_book_property``)
        is what caught that.

        ``forfeit_fraction`` is for the walk, where the holding is whatever the strategy bought
        and a 3:2 bonus on an odd count is a real event, not a wiring error. The fractional
        entitlement is floored away and its value forfeited: the exchange sells aggregated
        fractions and pays cash in lieu, a sum the store does not carry, so the book takes the
        conservative side — never more shares than the arithmetic gives. The basis stays whole on
        the shares that remain; a holding floored to zero books its whole basis as a realized loss
        rather than letting it vanish from the P&L.
        """
        held = self._positions.get(isin)
        if held is None or held.quantity == 0:
            raise InsufficientSharesError(f"cannot apply {event}: no position in {isin}")
        exact = held.quantity * numerator / denominator
        if forfeit_fraction:
            new_quantity = int(exact.to_integral_value(rounding=ROUND_FLOOR))
        else:
            new_quantity = self._whole_shares(exact, event)
        basis = held.cost_basis
        if new_quantity == 0:
            self._realized -= basis
            basis = _ZERO
        self._positions[isin] = BookPosition(isin, new_quantity, basis)
        _log.info(
            "book.corporate_action",
            ca_event=event,
            isin=isin,
            old_quantity=held.quantity,
            new_quantity=new_quantity,
            forfeited=str(exact - new_quantity),
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
