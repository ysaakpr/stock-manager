"""A8: the risk rails — deterministic pre-trade checks and the daily drawdown monitor.

Every order from A5/A6/A7 passes through here before it reaches the broker (invariant #6). The
checks are pure functions of the order, the book and the ratified `RiskRails` — no LLM is anywhere
near this, and there is no override: `check_order` takes what it needs to decide and nothing that
could tell it to decide differently. A grep test (`tests/unit/test_rails.py`) asserts that no
bypass parameter exists on any callable in this package.

The module is two layers, and the split is deliberate:

* **Pure logic** — `check_order`, `assess_drawdown`, `apply_order` — depends on nothing but its
  arguments. It is what the property tests drive: a generated stream of orders is cleared, the
  allowed ones applied, and the resulting book asserted never to breach a cap. That proof needs no
  database and no clock, so the pure layer takes neither.
* **Journalling** — `RailEngine.guard_order`, `RailEngine.review_drawdown` — wraps the pure checks
  and writes their outcome to the decision journal (§0, invariant #9). A blocked order writes a
  `RAIL_BLOCK` line naming every breached rail; a breached drawdown writes a forced-review
  `ESCALATE` line by the `RAILS` actor. This layer takes the `Journal` and an injected `Clock`.

A8 decides; it does not act. `guard_order` returns the assessment and journals a block — it never
places the order, because placement is X1's job and giving the rail engine a broker would be a
second path an order could reach the market by. The caller places the order only if the assessment
allows it, and the fact that *every* caller must ask first is what makes the rail unbypassable.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Final

from analyst.cases import RiskRails
from analyst.journal import Actor, Decision, Journal, JournalEntry, Sleeve
from analyst.rails.policies import (
    DrawdownStatus,
    HouseholdExposure,
    Lot,
    Portfolio,
    ProposedOrder,
    RailAssessment,
    RailBreach,
    RailId,
    drawdown_of,
    require_decimal,
)
from dataplatform.clock import Clock, SystemClock
from dataplatform.logging import get_logger
from execution.broker import Side

__all__ = [
    "FORCED_REVIEW_EVENT",
    "RailEngine",
    "apply_order",
    "assess_drawdown",
    "check_order",
]

_LOG = get_logger(__name__)

_ZERO: Final = Decimal(0)
_HUNDRED: Final = Decimal(100)

#: The payload marker on a forced-review journal line, so a reviewer can query the drawdown trigger
#: apart from other `RAILS` escalations without parsing a rationale sentence.
FORCED_REVIEW_EVENT: Final = "DRAWDOWN_FORCED_REVIEW"


# ── pure checks ────────────────────────────────────────────────────────────────────────────────


def apply_order(portfolio: Portfolio, order: ProposedOrder) -> Portfolio:
    """Return the book that results from executing `order` against `portfolio` — a pure function.

    What it does: move cash and shares. A buy debits `order.value` from cash and adds (or grows) a
    lot marked at the order's price; a sell credits `order.value` and shrinks (or removes) the lot.
    The traded lot is marked at the order's reference price, so the pre-trade check and the book it
    produces value that instrument identically.
    What it assumes: the order is executable against this book — a sell names a lot that exists and
    does not oversell it. Both are rail-independent facts the broker guarantees, so violating them
    is a programming error here, raised loudly rather than absorbed.
    What it never does: apply costs or slippage. Rails value orders at the reference price (a
    sanity check is not an accounting entry); the book's true cost basis is X2's problem.
    """
    existing = portfolio.lot(order.isin)
    if order.side is Side.BUY:
        merged = _merge_buy(existing, order)
        others = tuple(lot for lot in portfolio.lots if lot.isin != order.isin)
        return Portfolio(
            case_id=portfolio.case_id,
            lots=(*others, merged),
            cash=portfolio.cash - order.value,
        )
    # SELL
    if existing is None:
        raise ValueError(f"cannot sell {order.isin}: the case does not hold it")
    if order.quantity > existing.quantity:
        raise ValueError(
            f"cannot sell {order.quantity} of {order.isin}: only {existing.quantity} held"
        )
    remaining = existing.quantity - order.quantity
    others = tuple(lot for lot in portfolio.lots if lot.isin != order.isin)
    lots = others if remaining == 0 else (*others, _remark(existing, remaining, order.price))
    return Portfolio(case_id=portfolio.case_id, lots=lots, cash=portfolio.cash + order.value)


def _merge_buy(existing: Lot | None, order: ProposedOrder) -> Lot:
    """The lot after a buy: marked at the order's price, quantity grown by the order's quantity."""
    quantity = order.quantity if existing is None else existing.quantity + order.quantity
    return Lot(isin=order.isin, sector=order.sector, quantity=quantity, price=order.price)


def _remark(lot: Lot, quantity: int, price: Decimal) -> Lot:
    """The lot after a partial sell: same sector, new quantity, marked at the sell price."""
    return Lot(isin=lot.isin, sector=lot.sector, quantity=quantity, price=price)


def check_order(
    order: ProposedOrder,
    portfolio: Portfolio,
    rails: RiskRails,
    *,
    household: HouseholdExposure | None = None,
) -> RailAssessment:
    """Assess one order against the ratified rails — the whole of A8's pre-trade authority.

    What it does: compute the book the order would produce and test every rail against it — the
    per-order sanity caps on the order itself, and the position, sector, min-holdings and (when a
    household view is supplied) cross-case concentration caps on the resulting book. It returns
    *every* breach, so a caller fixing a proposal sees all of them at once.
    What it assumes: `rails` is the ratified `RiskRails` for this case (§5.2 policy 4) and `order`
    is priced at the reference price the book marks it at. `household` is the household exposure
    *after* this order — the daily loop assembles it; when it is None the cross-case rail is not
    evaluated (a single-case check).
    What it never does: take an override. There is no argument that makes it pass a breaching
    order, because a rail with a bypass is not a rail (invariant #6).
    """
    breaches: list[RailBreach] = []
    breaches.extend(_order_sanity_breaches(order, portfolio, rails))
    resulting = apply_order(portfolio, order)
    breaches.extend(_position_breaches(order, resulting, rails))
    breaches.extend(_sector_breaches(order, resulting, rails))
    min_holdings = _min_holdings_breach(portfolio, resulting, rails)
    if min_holdings is not None:
        breaches.append(min_holdings)
    if household is not None:
        cross = _cross_case_breach(household, rails)
        if cross is not None:
            breaches.append(cross)
    return RailAssessment(isin=order.isin, side=order.side, breaches=tuple(breaches))


def _pct_of(value: Decimal, total: Decimal) -> Decimal:
    """`value` as a percentage of `total`, or zero when there is no total to be a fraction of."""
    if total <= _ZERO:
        return _ZERO
    return value * _HUNDRED / total


def _order_sanity_breaches(
    order: ProposedOrder, portfolio: Portfolio, rails: RiskRails
) -> list[RailBreach]:
    """The per-order fat-finger caps: absolute rupee value and share of case value (§5.2)."""
    breaches: list[RailBreach] = []
    if order.value > rails.max_order_value_inr:
        breaches.append(
            RailBreach(
                rail=RailId.MAX_ORDER_VALUE,
                limit=rails.max_order_value_inr,
                observed=order.value,
                detail=f"order value {order.value} for {order.isin}",
            )
        )
    order_pct = _pct_of(order.value, portfolio.total_value)
    if order_pct > rails.max_order_pct_of_case:
        breaches.append(
            RailBreach(
                rail=RailId.MAX_ORDER_PCT,
                limit=rails.max_order_pct_of_case,
                observed=order_pct,
                detail=f"order is {order_pct}% of case value",
            )
        )
    return breaches


def _position_breaches(
    order: ProposedOrder, resulting: Portfolio, rails: RiskRails
) -> list[RailBreach]:
    """The position cap on the traded name in the resulting book.

    Only the traded instrument can newly breach its own cap — other lots' values did not change —
    so it is the only one checked, and a sell (which can only shrink a position) never can.
    """
    if order.side is Side.SELL:
        return []
    lot = resulting.lot(order.isin)
    if lot is None:
        return []
    position_pct = _pct_of(lot.value, resulting.total_value)
    if position_pct > rails.max_position_pct:
        return [
            RailBreach(
                rail=RailId.MAX_POSITION,
                limit=rails.max_position_pct,
                observed=position_pct,
                detail=f"{order.isin} would be {position_pct}% of case value",
            )
        ]
    return []


def _sector_breaches(
    order: ProposedOrder, resulting: Portfolio, rails: RiskRails
) -> list[RailBreach]:
    """The sector cap on the traded name's sector in the resulting book (buys only)."""
    if order.side is Side.SELL:
        return []
    sector_pct = _pct_of(resulting.sector_value(order.sector), resulting.total_value)
    if sector_pct > rails.max_sector_pct:
        return [
            RailBreach(
                rail=RailId.MAX_SECTOR,
                limit=rails.max_sector_pct,
                observed=sector_pct,
                detail=f"sector {order.sector!r} would be {sector_pct}% of case value",
            )
        ]
    return []


def _min_holdings_breach(
    before: Portfolio, after: Portfolio, rails: RiskRails
) -> RailBreach | None:
    """The minimum-holdings floor: a sell may not drop the book below it once it has reached it.

    A book that has not yet reached `min_holdings` is still being built — demanding eight names on
    the first buy would block every case at inception — so the rail bites only when a sell would
    take a book that *was* at or above the floor down below it. Buys only ever add names, so they
    can never breach it.
    """
    if after.holding_count >= before.holding_count:
        return None
    if before.holding_count >= rails.min_holdings > after.holding_count:
        return RailBreach(
            rail=RailId.MIN_HOLDINGS,
            limit=Decimal(rails.min_holdings),
            observed=Decimal(after.holding_count),
            detail=(
                f"selling out would leave {after.holding_count} holdings, "
                f"below the floor of {rails.min_holdings}"
            ),
        )
    return None


def _cross_case_breach(household: HouseholdExposure, rails: RiskRails) -> RailBreach | None:
    """The cross-case concentration rail: household exposure to one name vs the position cap.

    The household is bound by the same `max_position_pct` as a single case, applied to the
    household's total value — so two cases cannot each sit just under their own cap and together
    breach a concentration neither can see (§8.1, decision #4). There is no separate ratified
    number for it in §5.2; the position cap is the ceiling, at both scopes.
    """
    exposure_pct = _pct_of(household.household_value_in_isin, household.household_total_value)
    if exposure_pct > rails.max_position_pct:
        return RailBreach(
            rail=RailId.CROSS_CASE_CONCENTRATION,
            limit=rails.max_position_pct,
            observed=exposure_pct,
            detail=f"household holds {exposure_pct}% in {household.isin}",
        )
    return None


def assess_drawdown(values: Sequence[Decimal], rails: RiskRails) -> DrawdownStatus:
    """The daily monitor's verdict: the worst peak-to-trough fall in a case-value series.

    What it does: walk the series once, tracking the running peak and the deepest fall from any
    peak to a later value, and report that fall against the ratified `drawdown_review_pct`. The
    worst fall, not the last: a case that fell 30% and recovered 5% still triggered a review on the
    day it hit the trough, and the monitor must see that.
    What it assumes: `values` is the case's total value over the window being reviewed, in
    chronological order, each a `Decimal` (a value that arrived as a float would move the trigger).
    What it never does: read a clock or invent a value — an empty or single-point series has no
    drawdown, reported as zero rather than raising, because "not enough history to fall" is a real
    daily state, not an error.
    """
    peak = _ZERO
    worst_peak = _ZERO
    worst_trough = _ZERO
    worst_pct = _ZERO
    for raw in values:
        value = require_decimal("case value", raw)
        if value > peak:
            peak = value
        fall = drawdown_of(peak, value)
        if fall > worst_pct:
            worst_pct = fall
            worst_peak = peak
            worst_trough = value
    return DrawdownStatus(
        peak=worst_peak,
        trough=worst_trough,
        drawdown_pct=worst_pct,
        limit_pct=rails.drawdown_review_pct,
    )


# ── journalling ──────────────────────────────────────────────────────────────────────────────


class RailEngine:
    """A8 wired to the journal: it clears orders and monitors drawdown, and writes down what it did.

    What it does: run the pure checks and record their outcome. `guard_order` returns the
    assessment and, on a block, appends a `RAIL_BLOCK` line naming every breached rail;
    `review_drawdown` appends a forced-review `ESCALATE` line when the fall reaches the limit.
    What it assumes: the caller owns the transaction (the `Journal` never commits), so a rail block
    and the heartbeat around it land atomically; `ts` comes from the injected `Clock` (B10).
    What it never does: place, modify or cancel an order. A8 has no broker — it decides whether an
    order *may* be placed and journals that decision, and X1 does the placing. Giving the rail a
    broker would be a second path to the market, and the one thing invariant #6 forbids is a second
    path.
    """

    __slots__ = ("_clock", "_journal")

    def __init__(self, journal: Journal, *, clock: Clock | None = None) -> None:
        self._journal = journal
        self._clock = SystemClock() if clock is None else clock

    def __repr__(self) -> str:
        return f"{type(self).__name__}(journal={self._journal!r})"

    def guard_order(
        self,
        order: ProposedOrder,
        portfolio: Portfolio,
        rails: RiskRails,
        *,
        trading_date: date,
        household: HouseholdExposure | None = None,
        sleeve: Sleeve | None = None,
    ) -> RailAssessment:
        """Clear an order, journalling a `RAIL_BLOCK` if any rail refuses it.

        What it does: call `check_order`; if the order is allowed, return the (empty) assessment
        without writing — a passed rail is not itself a decision, the caller's subsequent order or
        heartbeat is. If it is blocked, append a `RAIL_BLOCK` entry whose rationale names every
        breached rail and whose payload carries each breach's numbers, then return the assessment.
        What it assumes: the caller places the order only when `assessment.allowed`. A8 cannot
        enforce that by acting itself, but every caller asking first is what makes the rail binding.
        What it never does: place the order, or pass a blocked one. There is no parameter that
        changes the verdict — `household` and `sleeve` add context, they do not grant exceptions.
        """
        assessment = check_order(order, portfolio, rails, household=household)
        if assessment.allowed:
            return assessment
        payload = {
            f"breach_{index}": breach.message() for index, breach in enumerate(assessment.breaches)
        }
        payload["rails"] = ",".join(rail.value for rail in assessment.breached_rails)
        entry = JournalEntry(
            ts=self._clock.now(),
            trading_date=trading_date,
            case_id=portfolio.case_id,
            actor=Actor.RAILS,
            decision=Decision.RAIL_BLOCK,
            isin=order.isin,
            sleeve=sleeve,
            rationale=assessment.rationale(),
            payload=payload,
        )
        recorded = self._journal.append(entry)
        _LOG.info(
            "rails.block",
            case_id=portfolio.case_id,
            isin=order.isin,
            side=order.side.value,
            rails=payload["rails"],
            entry_id=recorded.id,
        )
        return assessment

    def review_drawdown(
        self,
        values: Sequence[Decimal],
        rails: RiskRails,
        *,
        case_id: str,
        trading_date: date,
    ) -> DrawdownStatus:
        """Run the daily drawdown monitor, journalling a forced review when the limit is reached.

        What it does: call `assess_drawdown`; if the fall reached `drawdown_review_pct`, append an
        `ESCALATE` entry by the `RAILS` actor — the drawdown-triggered forced review of §6 — with
        the peak, trough and fall in the payload and `FORCED_REVIEW_EVENT` marking it. Return the
        status either way, so a caller can journal its own heartbeat when nothing fired.
        What it never does: decide *what* the review concludes. It forces the review; T1/T2 and the
        human do the reviewing. A forced review is an escalation, not a sell.
        """
        status = assess_drawdown(values, rails)
        if not status.review_forced:
            return status
        entry = JournalEntry(
            ts=self._clock.now(),
            trading_date=trading_date,
            case_id=case_id,
            actor=Actor.RAILS,
            decision=Decision.ESCALATE,
            rationale=(
                f"peak-to-trough drawdown {status.drawdown_pct}% reached the review limit of "
                f"{status.limit_pct}%; forcing a review"
            ),
            payload={
                "event": FORCED_REVIEW_EVENT,
                "drawdown_pct": str(status.drawdown_pct),
                "limit_pct": str(status.limit_pct),
                "peak": str(status.peak),
                "trough": str(status.trough),
            },
        )
        recorded = self._journal.append(entry)
        _LOG.info(
            "rails.drawdown_review",
            case_id=case_id,
            drawdown_pct=str(status.drawdown_pct),
            limit_pct=str(status.limit_pct),
            entry_id=recorded.id,
        )
        return status
