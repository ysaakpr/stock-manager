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

**The M17 book rails** (pre-registration §4 step 5) are the same kind of pure check —
`check_book_order` — over the same `Portfolio` and `ProposedOrder`, against a `BookRails` built
from the fund-manager roster plus the per-order `BookOrderFacts` (liquidity, series, last buy fill,
spendable cash) the book knew at the decision session. `RailEngine.guard_book_order` journals a
refusal exactly as `guard_order` does: one `RAIL_BLOCK` line naming every rail it broke.

A8 decides; it does not act. `guard_order` returns the assessment and journals a block — it never
places the order, because placement is X1's job and giving the rail engine a broker would be a
second path an order could reach the market by. The caller places the order only if the assessment
allows it, and the fact that *every* caller must ask first is what makes the rail unbypassable.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Final, Protocol

from analyst.cases import RiskRails
from analyst.journal import Actor, Decision, JournalEntry, RecordedEntry, Sleeve
from analyst.rails.policies import (
    BookOrderFacts,
    BookRails,
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
    "BookExitClearance",
    "ExitClearance",
    "RailEngine",
    "RailJournal",
    "apply_order",
    "assess_drawdown",
    "check_book_order",
    "check_order",
    "max_child_quantity",
    "order_value_ceiling",
    "participation_child_quantity",
    "slice_book_exit",
    "slice_exit",
]

_LOG = get_logger(__name__)

_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)
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


def _breached(rail: RailId, observed: Decimal, limit: Decimal, *, floor: bool = False) -> bool:
    """Whether ``observed`` breaks ``limit`` for ``rail``: above a cap, or below a ``floor``.

    Every M17 book rail compares through this one function, so the comparison direction lives in
    exactly one place — and the property test can show that inverting it for any single rail lets
    a breaching order through (tests/unit/test_fm_rails_property.py).
    """
    del rail  # named at every call site so a breach and its comparison read together
    return observed < limit if floor else observed > limit


def check_book_order(
    order: ProposedOrder,
    portfolio: Portfolio,
    rails: BookRails,
    facts: BookOrderFacts,
) -> RailAssessment:
    """Assess one order of an M17 paper book against the rails of pre-registration §4 step 5.

    What it does: a SELL is checked for a short (more shares than ``portfolio`` holds), the
    minimum hold (sessions since the last buy fill, at the decision session) and participation;
    a BUY for F&O (a series outside ``rails.equity_series``), margin (notional above
    ``facts.spendable_cash``), participation, and — on the book the buy would produce, valued at
    the order's reference price — the position, sector and number-of-names caps. Every breach is
    returned, not just the first.
    What it assumes: ``portfolio`` is the book marked at the decision session's reference prices
    (the same price the order carries), with every buy already cleared this session applied to it,
    and its ``cash`` is the cash the book is worth (settled and in settlement). For a sell it holds
    what is still sellable after the sells already cleared this session. A sell never frees cash
    or a name slot for a buy of the same session: its fill is not certain, and its proceeds are
    not spendable before it settles — the caller clears buys against a book without it.
    What it never does: take an override, read a clock or a market, or construct a book with
    negative cash. A refused order is refused whole; this function does not resize it.
    """
    breaches: list[RailBreach] = []
    held = portfolio.lot(order.isin)
    if order.side is Side.SELL:
        held_quantity = 0 if held is None else held.quantity
        if _breached(RailId.NO_SHORT, Decimal(order.quantity), Decimal(held_quantity)):
            breaches.append(
                RailBreach(
                    rail=RailId.NO_SHORT,
                    limit=Decimal(held_quantity),
                    observed=Decimal(order.quantity),
                    detail=f"selling {order.quantity} of {order.isin} with {held_quantity} held",
                )
            )
        held_for = facts.sessions_since_buy_fill
        if held_for is None or _breached(
            RailId.MIN_HOLD, Decimal(held_for), Decimal(rails.min_hold_sessions), floor=True
        ):
            breaches.append(
                RailBreach(
                    rail=RailId.MIN_HOLD,
                    limit=Decimal(rails.min_hold_sessions),
                    observed=Decimal(-1 if held_for is None else held_for),
                    detail=(
                        f"{order.isin} has no buy fill on record"
                        if held_for is None
                        else f"{order.isin} last bought {held_for} session(s) before the decision"
                    ),
                )
            )
    else:
        # 1 when the instrument is outside every cash-equity series the book may buy, else 0.
        outside = _ONE if facts.series is None or facts.series not in rails.equity_series else _ZERO
        if _breached(RailId.NO_FNO, outside, _ZERO):
            breaches.append(
                RailBreach(
                    rail=RailId.NO_FNO,
                    limit=_ZERO,
                    observed=outside,
                    detail=(
                        f"{order.isin} is in series {facts.series!r} on "
                        f"{facts.decision_session.isoformat()}, not a cash-equity series of "
                        f"{sorted(rails.equity_series)}"
                    ),
                )
            )
        if _breached(RailId.NO_MARGIN, order.value, facts.spendable_cash):
            breaches.append(
                RailBreach(
                    rail=RailId.NO_MARGIN,
                    limit=facts.spendable_cash,
                    observed=order.value,
                    detail=f"buy of {order.value} for {order.isin} exceeds spendable cash",
                )
            )
    breaches.extend(_participation_breaches(order, rails, facts))
    if order.side is Side.BUY:
        breaches.extend(_book_cap_breaches(order, portfolio, rails))
    return RailAssessment(isin=order.isin, side=order.side, breaches=tuple(breaches))


def _participation_breaches(
    order: ProposedOrder, rails: BookRails, facts: BookOrderFacts
) -> list[RailBreach]:
    """The participation rail: notional vs ``participation_max_pct`` of the median traded value."""
    median = facts.median_traded_value
    complete = facts.median_sessions >= rails.participation_lookback_sessions
    if median is None or median <= _ZERO or not complete:
        return [
            RailBreach(
                rail=RailId.PARTICIPATION,
                limit=_ZERO,
                observed=order.value,
                detail=(
                    f"no {rails.participation_lookback_sessions}-session median traded value for "
                    f"{order.isin} at {facts.decision_session.isoformat()} "
                    f"({facts.median_sessions} session(s) knowable)"
                ),
            )
        ]
    ceiling = rails.participation_max_pct * median / _HUNDRED
    if _breached(RailId.PARTICIPATION, order.value, ceiling):
        return [
            RailBreach(
                rail=RailId.PARTICIPATION,
                limit=ceiling,
                observed=order.value,
                detail=(
                    f"order notional for {order.isin} exceeds {rails.participation_max_pct}% of "
                    f"its {rails.participation_lookback_sessions}-session median traded value "
                    f"{median}"
                ),
            )
        ]
    return []


def _book_cap_breaches(
    order: ProposedOrder, portfolio: Portfolio, rails: BookRails
) -> list[RailBreach]:
    """Position, sector and number-of-names caps on the book a buy would produce.

    Computed arithmetically rather than through ``apply_order`` so a buy the margin rail refuses
    still has its caps reported: the resulting book re-marks the traded lot at the order's price,
    exactly as ``apply_order`` does, and moves cash into it, which leaves every other value alone.
    """
    breaches: list[RailBreach] = []
    held = portfolio.lot(order.isin)
    held_quantity = 0 if held is None else held.quantity
    lot_after = order.price * (held_quantity + order.quantity)
    remark = _ZERO if held is None else order.price * held.quantity - held.value
    total_after = portfolio.total_value + remark
    position_pct = _pct_of(lot_after, total_after)
    if _breached(RailId.MAX_POSITION, position_pct, rails.max_position_pct):
        breaches.append(
            RailBreach(
                rail=RailId.MAX_POSITION,
                limit=rails.max_position_pct,
                observed=position_pct,
                detail=f"{order.isin} would be {position_pct}% of book value",
            )
        )
    others_in_sector = sum(
        (
            lot.value
            for lot in portfolio.lots
            if lot.sector == order.sector and lot.isin != order.isin
        ),
        _ZERO,
    )
    sector_pct = _pct_of(others_in_sector + lot_after, total_after)
    if _breached(RailId.MAX_SECTOR, sector_pct, rails.max_sector_pct):
        breaches.append(
            RailBreach(
                rail=RailId.MAX_SECTOR,
                limit=rails.max_sector_pct,
                observed=sector_pct,
                detail=f"sector {order.sector!r} would be {sector_pct}% of book value",
            )
        )
    names_after = portfolio.holding_count + (0 if held is not None else 1)
    if _breached(RailId.MAX_POSITIONS, Decimal(names_after), Decimal(rails.max_positions)):
        breaches.append(
            RailBreach(
                rail=RailId.MAX_POSITIONS,
                limit=Decimal(rails.max_positions),
                observed=Decimal(names_after),
                detail=f"buying {order.isin} would make {names_after} names",
            )
        )
    return breaches


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


def order_value_ceiling(rails: RiskRails, case_value: Decimal) -> Decimal:
    """The most rupees one order may carry under both per-order caps, for a case of ``case_value``.

    What it does: the smaller of ``max_order_value_inr`` and ``max_order_pct_of_case`` of the case's
    value — the same two numbers ``check_order`` refuses an order above. It is the one place the
    order-size ceiling is derived, so the exit slicer and a policy sizing its buys read it from A8
    rather than re-deriving a cap the rail might then disagree with.
    What it assumes: ``case_value`` is the case's value as the rail book would mark it.
    What it never does: decide. An order sized to the ceiling still goes through ``check_order``,
    and every other rail, like any other.
    """
    require_decimal("case value", case_value)
    ceiling = rails.max_order_value_inr
    if case_value > _ZERO:
        ceiling = min(ceiling, rails.max_order_pct_of_case * case_value / _HUNDRED)
    return ceiling


def max_child_quantity(order: ProposedOrder, portfolio: Portfolio, rails: RiskRails) -> int:
    """The most shares of ``order``'s instrument one order may carry under both per-order caps.

    What it does: the largest whole-share count whose value at the order's reference price is
    within ``order_value_ceiling`` for the book's value.
    What it assumes: ``portfolio`` is the book the order would be checked against. A sell moves
    value from a lot to cash at the same price, so the case's value — and with it the percentage
    cap in rupees — is the same for every child of one exit.
    What it never does: round up. Zero means one share is already above a cap.
    """
    cap = order_value_ceiling(rails, portfolio.total_value)
    shares = int((cap / order.price).to_integral_value(rounding=ROUND_FLOOR))
    # Decimal division is exact to the context's precision, not exactly; the caps are checked as
    # ``price * quantity``, so the slice is held to the same product the rail will compute.
    while shares > 0 and order.price * shares > cap:
        shares -= 1
    return max(shares, 0)


def slice_exit(
    order: ProposedOrder, portfolio: Portfolio, rails: RiskRails
) -> tuple[ProposedOrder, ...]:
    """Split a risk-reducing sell into child orders that each fit the per-order caps.

    What it does: return ``(order,)`` unchanged unless it is a SELL of a held long, no larger
    than the held quantity, whose value breaches a per-order cap; then return the fewest children
    (as equal as whole shares allow, larger ones first) that sum to the order's quantity, each
    within both ``max_order_value_inr`` and ``max_order_pct_of_case``. A real broker slices a
    large order the same way; the fat-finger cap exists to stop a typo opening exposure, not to
    trap a position the case has decided to leave.
    What it assumes: ``portfolio`` is the book the order is checked against.
    What it never does: slice a buy, a sell of something not held, or a sell larger than the
    held quantity — those reach ``check_order`` whole, and are refused there or by the caller's
    executability check. It never decides anything either: every child still goes through
    ``check_order`` one by one, against the book the children before it leave.
    """
    if order.side is not Side.SELL:
        return (order,)
    lot = portfolio.lot(order.isin)
    if lot is None or order.quantity > lot.quantity:
        return (order,)
    if not _order_sanity_breaches(order, portfolio, rails):
        return (order,)
    per_child = max_child_quantity(order, portfolio, rails)
    if per_child <= 0:
        # One share is above the cap: there is no slice that fits, so the rail refuses it whole.
        return (order,)
    count = -(-order.quantity // per_child)
    base, extra = divmod(order.quantity, count)
    quantities = [base + 1] * extra + [base] * (count - extra)
    return tuple(
        replace(order, request=replace(order.request, quantity=quantity)) for quantity in quantities
    )


def participation_child_quantity(
    order: ProposedOrder, rails: BookRails, facts: BookOrderFacts
) -> int:
    """The most shares of ``order``'s instrument one session's order may carry under participation.

    What it does: the largest whole-share count whose value at the order's reference price is
    within ``participation_max_pct`` of the decision session's median traded value.
    What it never does: round up, or guess a median. Zero means the median is not knowable (an
    incomplete lookback) or one share is already above the ceiling.
    """
    median = facts.median_traded_value
    if (
        median is None
        or median <= _ZERO
        or facts.median_sessions < rails.participation_lookback_sessions
    ):
        return 0
    ceiling = rails.participation_max_pct * median / _HUNDRED
    shares = int((ceiling / order.price).to_integral_value(rounding=ROUND_FLOOR))
    # Held to the same ``price * quantity`` product the rail computes, as in max_child_quantity.
    while shares > 0 and _breached(RailId.PARTICIPATION, order.price * shares, ceiling):
        shares -= 1
    return max(shares, 0)


def slice_book_exit(
    order: ProposedOrder, portfolio: Portfolio, rails: BookRails, facts: BookOrderFacts
) -> ProposedOrder:
    """This session's child of an M17 book's exit: the order whole, or the part participation lets.

    What it does: return ``order`` unchanged unless it is a SELL of a held long, no larger than
    the held quantity, that participation alone refuses (every other rail passes it whole) with a
    knowable median; then return a child of ``participation_child_quantity`` shares. The rest of
    the parent is worked on later sessions, a child per session, each cleared afresh against that
    session's median — participation is a per-session cap, so two children of one exit in one
    session would together breach it, and they are never staged together.
    What it never does: slice a buy, a short, an oversell, or a sell some other rail refuses: those
    reach ``check_book_order`` whole and are refused there. It never weakens a rail either — the
    child is cleared through every rail by the caller.
    """
    if order.side is not Side.SELL:
        return order
    lot = portfolio.lot(order.isin)
    if lot is None or order.quantity > lot.quantity:
        return order
    whole = check_book_order(order, portfolio, rails, facts)
    if whole.allowed or whole.breached_rails != (RailId.PARTICIPATION,):
        return order
    shares = participation_child_quantity(order, rails, facts)
    if shares <= 0:
        return order
    return replace(order, request=replace(order.request, quantity=shares))


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


class RailJournal(Protocol):
    """The one journal operation A8 performs: append an entry and get the recorded row back.

    `analyst.journal.Journal` satisfies it, and is what the daily loop passes. The seam exists so
    the replay engine (X2) can run the *same* `RailEngine` offline — collecting the `RAIL_BLOCK`
    lines into its own deterministic journal, and persisting them only when a database is attached
    — rather than re-implementing the verdict or the journal line beside it (invariant #5: one
    decision path for paper, real and replay). It narrows what the engine may do with a journal;
    it widens nothing about what a rail decides.
    """

    def append(self, entry: JournalEntry) -> RecordedEntry:
        """Append `entry` and return it as recorded."""
        ...


@dataclass(frozen=True, slots=True)
class ExitClearance:
    """A8's verdict on one exit, cleared as the children ``slice_exit`` cut it into.

    ``children`` is every child in order and ``assessments`` the verdict on each child that was
    checked — clearing stops at the first refused child, so the two are the same length only when
    every child passed. ``allowed`` is the children the caller may place, in order: all of them,
    or none. An exit is one decision; a rail that refuses any part of it refuses the exit, exactly
    as it would have refused the unsliced order, so slicing changes the outcome of the per-order
    caps and of nothing else.
    """

    parent: ProposedOrder
    children: tuple[ProposedOrder, ...]
    assessments: tuple[RailAssessment, ...]

    @property
    def sliced(self) -> bool:
        """True when the parent was cut into more than one child."""
        return len(self.children) > 1

    @property
    def allowed(self) -> tuple[ProposedOrder, ...]:
        """Every child when every rail allowed every child; otherwise nothing."""
        if self.blocked is not None or len(self.assessments) != len(self.children):
            return ()
        return self.children

    @property
    def blocked(self) -> RailAssessment | None:
        """The assessment of the child that stopped the exit, or None when every child passed."""
        for assessment in self.assessments:
            if not assessment.allowed:
                return assessment
        return None

    def payload(self) -> dict[str, str]:
        """The parent intent and each child, for the parent's journal line (strings only)."""
        return {
            "exit_parent_quantity": str(self.parent.quantity),
            "exit_children": ",".join(str(child.quantity) for child in self.children),
            "exit_children_allowed": str(len(self.allowed)),
            "exit_reference_price": str(self.parent.price),
        }


@dataclass(frozen=True, slots=True)
class BookExitClearance:
    """A8's verdict on one session's child of an M17 book's exit (``slice_book_exit``).

    ``child`` is the order whole when nothing needed slicing. ``allowed`` says whether the caller
    may stage ``child`` this session; ``remaining`` is what of ``parent`` is still to sell after it
    — the caller works that on the following sessions, one child each, through this method again.
    """

    parent: ProposedOrder
    child: ProposedOrder
    assessment: RailAssessment

    @property
    def allowed(self) -> bool:
        return self.assessment.allowed

    @property
    def sliced(self) -> bool:
        """True when the child is less than the parent (the rest waits for later sessions)."""
        return self.child.quantity < self.parent.quantity

    @property
    def remaining(self) -> int:
        """Shares of the parent left to sell after this session's child, if it is staged."""
        return self.parent.quantity - (self.child.quantity if self.allowed else 0)

    def payload(self) -> dict[str, str]:
        """The parent intent and this child, for the child's journal line (strings only)."""
        return {
            "exit_parent_quantity": str(self.parent.quantity),
            "exit_child_quantity": str(self.child.quantity),
            "exit_remaining": str(self.remaining),
            "exit_reference_price": str(self.parent.price),
        }


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

    def __init__(self, journal: RailJournal, *, clock: Clock | None = None) -> None:
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
        if not assessment.allowed:
            self._journal_block(
                order, portfolio, assessment, trading_date=trading_date, sleeve=sleeve
            )
        return assessment

    def guard_book_order(
        self,
        order: ProposedOrder,
        portfolio: Portfolio,
        rails: BookRails,
        facts: BookOrderFacts,
        *,
        trading_date: date,
        sleeve: Sleeve | None = None,
    ) -> RailAssessment:
        """Clear an M17 book's order, journalling a `RAIL_BLOCK` if any rail refuses it.

        What it does: call `check_book_order`; a refusal appends one `RAIL_BLOCK` line under the
        book's id (``portfolio.case_id``) whose ``payload.rails`` names every rail that refused it
        and whose rationale gives each one's numbers. An allowed order writes nothing here.
        What it never does: place the order, or pass a refused one.
        """
        assessment = check_book_order(order, portfolio, rails, facts)
        if not assessment.allowed:
            self._journal_block(
                order, portfolio, assessment, trading_date=trading_date, sleeve=sleeve
            )
        return assessment

    def guard_book_exit(
        self,
        order: ProposedOrder,
        portfolio: Portfolio,
        rails: BookRails,
        facts: BookOrderFacts,
        *,
        trading_date: date,
        sleeve: Sleeve | None = None,
    ) -> BookExitClearance:
        """Clear this session's child of an M17 book's sell, journalling a refusal.

        What it does: cut the order with ``slice_book_exit`` (a no-op for anything but a held
        long's sell that participation alone refuses), then ``check_book_order`` the child. A
        refused child is journalled as a ``RAIL_BLOCK`` naming its rails and, when sliced, the
        parent it belongs to. An allowed one writes nothing here; the caller stages it and
        journals the parent intent with ``BookExitClearance.payload``.
        What it assumes: the caller stages at most one child of a parent per session, and offers
        the remainder again on the next session against that session's facts.
        What it never does: weaken a rail. Every rail sees the child; the participation ceiling is
        the same number, applied to each session's child. A buy is cleared whole, exactly as
        ``guard_book_order`` clears it — a buy too big for participation is refused, not sliced.
        """
        child = slice_book_exit(order, portfolio, rails, facts)
        assessment = check_book_order(child, portfolio, rails, facts)
        if not assessment.allowed:
            context = (
                {"exit_parent_quantity": str(order.quantity)}
                if child.quantity < order.quantity
                else {}
            )
            self._journal_block(
                child,
                portfolio,
                assessment,
                trading_date=trading_date,
                sleeve=sleeve,
                context=context,
            )
        clearance = BookExitClearance(parent=order, child=child, assessment=assessment)
        if clearance.sliced:
            _LOG.info(
                "rails.book_exit_sliced",
                case_id=portfolio.case_id,
                isin=order.isin,
                parent_quantity=order.quantity,
                child_quantity=child.quantity,
                allowed=clearance.allowed,
            )
        return clearance

    def guard_exit(
        self,
        order: ProposedOrder,
        portfolio: Portfolio,
        rails: RiskRails,
        *,
        trading_date: date,
        sleeve: Sleeve | None = None,
    ) -> ExitClearance:
        """Clear a sell as ``slice_exit``'s children, each through every rail, in order.

        What it does: cut the order with ``slice_exit`` (a no-op for anything but an over-cap sell
        of a held long), then ``check_order`` each child against the book the children before it
        leave. A refused child is journalled as a ``RAIL_BLOCK`` naming its rails and its place in
        the exit, and refuses the exit: no child is placed, the ones before it included, so a rail
        other than the per-order caps decides a sliced exit exactly as it decided the whole one.
        What it assumes: the caller places exactly ``allowed``, in order, in one session, and
        journals the parent intent with ``ExitClearance.payload`` when the exit was sliced.
        What it never does: weaken a rail. The per-order caps are the same numbers, applied to
        every child; slicing only stops a whole-position exit from being refused for its size.
        A buy handed here is cleared whole, exactly as ``guard_order`` would clear it.
        """
        children = slice_exit(order, portfolio, rails)
        assessments: list[RailAssessment] = []
        book = portfolio
        for index, child in enumerate(children):
            assessment = check_order(child, book, rails)
            assessments.append(assessment)
            if not assessment.allowed:
                context = (
                    {
                        "exit_child": f"{index + 1}/{len(children)}",
                        "exit_parent_quantity": str(order.quantity),
                    }
                    if len(children) > 1
                    else {}
                )
                self._journal_block(
                    child,
                    book,
                    assessment,
                    trading_date=trading_date,
                    sleeve=sleeve,
                    context=context,
                )
                break
            book = apply_order(book, child)
        clearance = ExitClearance(parent=order, children=children, assessments=tuple(assessments))
        if clearance.sliced:
            _LOG.info(
                "rails.exit_sliced",
                case_id=portfolio.case_id,
                isin=order.isin,
                parent_quantity=order.quantity,
                children=len(children),
                allowed=len(clearance.allowed),
            )
        return clearance

    def _journal_block(
        self,
        order: ProposedOrder,
        portfolio: Portfolio,
        assessment: RailAssessment,
        *,
        trading_date: date,
        sleeve: Sleeve | None,
        context: Mapping[str, str] | None = None,
    ) -> None:
        """Append the ``RAIL_BLOCK`` line for a refused order, naming every breached rail."""
        payload = {
            f"breach_{index}": breach.message() for index, breach in enumerate(assessment.breaches)
        }
        payload["rails"] = ",".join(rail.value for rail in assessment.breached_rails)
        payload.update(context or {})
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
