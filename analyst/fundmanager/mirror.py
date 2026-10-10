"""A10 · M17.14 — the mirror book: a manager's ₹1 cr book, driven toward its ₹10 L book's weights.

Pre-registration §8 Amendment 2 (b). Each manager decides on its **primary** book
(``<manager>-10L``) only. After the primary's decisions are cleared and staged, its **mirror**
(``<manager>-1CR``) is driven mechanically toward the primary's *post-decision target weights*
through the mirror's own rails: no model call, no judgment, and nothing the manager ever sees.
What the mirror cannot do — a buy the participation rail refuses, a sell it slices over several
sessions — is the capacity measurement, journaled per session on the mirror book only
(``payload.event = MIRROR_DIVERGENCE``).

**The primary's post-decision target weights** (`target_weights`), fixed a priori: for each name,
the shares the primary will hold once the orders it staged this session fill — what it holds
after this session's fills, less every staged sell (a sliced sell counts at its whole parent
quantity, and a parent exit still being worked at its floor), plus every staged buy — valued at the
session's close (`FundBook.valuation_close`) over the primary's value at that close. A refused
order of the primary changes nothing, so the mirror never trades what the primary did not.

**The mirror's orders** (`plan_mirror`), per name, against the mirror's own value at the close:

- target shares = ``floor(weight x mirror value / close)``;
- a name the primary holds none of is sold in full; a name the primary acted on this session (it
  staged an order on it) is moved exactly to target; any other name is moved only when it is more
  than `MIRROR_DRIFT_BAND` (10 %) of its target off it — so the mirror catches up on a move it
  could not make earlier without paying a round trip on a few rupees of price drift;
- a sell goes to the book whole: the participation rail may slice it into a parent exit worked
  one child a session (`books.PendingExit`), and an exit already leading to the target is left
  to work rather than restarted;
- a buy is offered only when it fits the book's spendable cash and a free position slot (it
  otherwise waits, ``CASH`` / ``SLOTS``) and is **never resized**: one the participation rail (or
  any rail) refuses is journaled ``RAIL_BLOCK`` on the mirror book and offered again the next
  session it is still off target;
- a name the mirror's own stop sold while the primary still holds it is not bought back
  (``STOPPED``) until the primary's weight in it returns to zero.

**Stops** are declared once, in percent, and apply to both books: the mirror keeps its own
`StopBook`, declaring the primary's ``stop_pct`` at its own staging close when it buys, and taking
the primary's tightened level when the primary tightens (the job wires this,
`backtest.fm_job`). `MirrorState` carries the declared percents and the stopped names between
sessions.

What this module never does: call a model, read or write the primary's state (it reads the
primary's book and decision report only), stage an order itself (`FundBook.decide` clears and
stages), or resize an order a rail refused.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from typing import Any, Final

from analyst.fundmanager.books import (
    PAPER_MODE,
    BookOrder,
    DecisionReport,
    FundBook,
)
from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import Actor, Decision, JournalEntry
from dataplatform.logging import get_logger
from execution.broker import Side

__all__ = [
    "MIRROR_DIVERGENCE_EVENT",
    "MIRROR_DRIFT_BAND",
    "MIRROR_FOLLOW_EVENT",
    "MIRROR_RULE_VERSION",
    "MirrorLine",
    "MirrorPlan",
    "MirrorState",
    "MirrorStatus",
    "TargetWeights",
    "divergence_entry",
    "mirror_rule_bytes",
    "plan_mirror",
    "post_decision_quantities",
    "settle_plan",
    "target_weights",
]

_LOG = get_logger(__name__)

#: A name the primary did not act on is moved only when this far (a fraction of its target) off it.
MIRROR_DRIFT_BAND: Final = Decimal("0.10")
#: ``payload.event`` on the mirror's per-session divergence line.
MIRROR_DIVERGENCE_EVENT: Final = "MIRROR_DIVERGENCE"
#: ``BookOrder.event`` (and so ``payload.event``) on a mirror order's staging line.
MIRROR_FOLLOW_EVENT: Final = "MIRROR_FOLLOW"
#: The mirror rule, versioned: part of every manager book's `mandate_hash`.
MIRROR_RULE_VERSION: Final = "m17-mirror/1"

_CONTEXT: Final = Context(prec=34, rounding=ROUND_HALF_EVEN)
_ZERO: Final = Decimal(0)
_HUNDRED: Final = Decimal(100)
_WEIGHT: Final = Decimal("0.00000001")
_PP: Final = Decimal("0.0001")


def mirror_rule_bytes() -> bytes:
    """The mirror rule's constants as canonical bytes (a manager book's `mandate_hash` input)."""
    return json.dumps(
        {
            "version": MIRROR_RULE_VERSION,
            "drift_band": str(MIRROR_DRIFT_BAND),
            "buys": "whole, never resized; offered only within spendable cash and free slots",
            "sells": "whole; the participation rail may slice them into a parent exit",
            "stops": "declared once in percent; each book's own StopBook",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


class MirrorStatus(StrEnum):
    """Where one name of the mirror stands against the primary's target after the session."""

    ON_TARGET = "ON_TARGET"
    """Already at target, or inside the drift band of a name the primary did not act on."""
    STAGED = "STAGED"
    """An order toward the target was staged whole."""
    SLICED = "SLICED"
    """A sell was staged as the first child of a parent exit the participation rail slices."""
    EXITING = "EXITING"
    """A parent exit already working toward the target is left to work (or a suspended name's
    sell is held over until it prints)."""
    REFUSED = "REFUSED"
    """A rail refused the order (``rails`` names them); offered again while still off target."""
    CASH = "CASH"
    """The buy does not fit the book's spendable cash yet (a sale has not filled and settled)."""
    SLOTS = "SLOTS"
    """The buy would open a position beyond the book's maximum."""
    UNPRICED = "UNPRICED"
    """No close to size the order at."""
    STOPPED = "STOPPED"
    """The mirror's own stop sold it; not bought back while the primary still holds it."""
    HALTED = "HALTED"
    """The kill switch is tripped; nothing is staged."""


@dataclass(slots=True)
class MirrorState:
    """What a mirror carries between sessions: the primary's declared stop percents, and the
    names its own stop sold that the primary still holds."""

    stop_pcts: dict[str, Decimal] = field(default_factory=dict)
    stopped: set[str] = field(default_factory=set)

    def to_document(self) -> dict[str, Any]:
        return {
            "stop_pcts": {isin: str(pct) for isin, pct in sorted(self.stop_pcts.items())},
            "stopped": sorted(self.stopped),
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> MirrorState:
        return cls(
            stop_pcts={isin: Decimal(pct) for isin, pct in document["stop_pcts"].items()},
            stopped=set(document["stopped"]),
        )


@dataclass(frozen=True, slots=True)
class TargetWeights:
    """The primary's post-decision target weights on one session (module docstring).

    ``weights`` are fractions of ``nav`` (the primary's value at the close), every name the
    primary will hold; ``acted`` the names it staged an order on this session.
    """

    book_id: str
    session: date
    nav: Decimal
    weights: Mapping[str, Decimal]
    acted: frozenset[str]


def post_decision_quantities(book: FundBook, report: DecisionReport) -> dict[str, int]:
    """The shares ``book`` will hold once the orders ``report`` staged fill (module docstring)."""
    quantities = {i: q for i, q in book.account.quantities().items() if q > 0}
    for order, _ in report.staged:
        signed = order.quantity if order.side is Side.BUY else -order.quantity
        quantities[order.isin] = quantities.get(order.isin, 0) + signed
    for isin, pending in book.pending_exits.items():
        if isin in quantities:
            quantities[isin] = min(quantities[isin], pending.floor)
    return {isin: q for isin, q in sorted(quantities.items()) if q > 0}


def _nav(book: FundBook, session: date) -> tuple[Decimal, dict[str, Decimal]]:
    """``book``'s value at ``session``'s close and the close each held name is valued at."""
    closes: dict[str, Decimal] = {}
    total = book.account.cash_value
    for isin, quantity in book.account.quantities().items():
        if quantity <= 0:
            continue
        price = book.valuation_close(isin, session)
        if price is None:
            raise ValueError(
                f"{book.book_id}: held {isin} has no close on {session.isoformat()}; the book "
                "cannot be valued"
            )
        closes[isin] = price
        total += price * quantity
    return total, closes


def target_weights(primary: FundBook, report: DecisionReport, session: date) -> TargetWeights:
    """The primary's post-decision target weights at ``session``'s close (module docstring)."""
    nav, closes = _nav(primary, session)
    weights: dict[str, Decimal] = {}
    if nav > _ZERO:
        for isin, quantity in post_decision_quantities(primary, report).items():
            price = closes.get(isin) or primary.valuation_close(isin, session)
            if price is None:
                continue
            with localcontext(_CONTEXT):
                weights[isin] = (price * quantity / nav).quantize(_WEIGHT)
    acted = frozenset(order.isin for order, _ in report.staged) | frozenset(
        order.isin for order, _ in report.refused
    )
    return TargetWeights(
        book_id=primary.book_id,
        session=session,
        nav=nav,
        weights=weights,
        acted=acted if not report.halted else frozenset(),
    )


@dataclass(frozen=True, slots=True)
class MirrorLine:
    """One name of the mirror on one session: target, holding, what was asked, where it stands."""

    isin: str
    target_weight: Decimal
    target_quantity: int | None
    held: int
    side: Side | None
    quantity: int
    status: MirrorStatus
    rails: tuple[str, ...] = ()

    def to_document(self) -> dict[str, str]:
        return {
            "isin": self.isin,
            "target_weight": str(self.target_weight),
            "target_quantity": "" if self.target_quantity is None else str(self.target_quantity),
            "held": str(self.held),
            "side": "" if self.side is None else self.side.value,
            "quantity": str(self.quantity),
            "status": self.status.value,
            "rails": ",".join(self.rails),
        }


@dataclass(frozen=True, slots=True)
class MirrorPlan:
    """The mirror's orders for one session and a line for every name it considered."""

    book_id: str
    session: date
    nav: Decimal
    orders: tuple[BookOrder, ...]
    lines: Mapping[str, MirrorLine]


def plan_mirror(
    mirror: FundBook,
    targets: TargetWeights,
    session: date,
    state: MirrorState,
    *,
    max_positions: int,
    stopped_today: Collection[str] = (),
) -> MirrorPlan:
    """The mirror's orders toward ``targets`` at ``session``'s close (module docstring).

    ``stopped_today`` names the mirror's own stop exits this session: the job stages those ahead
    of these orders (`stops.with_stop_exits`), so no order is planned for them here. Updates
    ``state.stopped``: a name the primary holds none of is released.
    """
    nav, _ = _nav(mirror, session)
    held = {i: q for i, q in mirror.account.quantities().items() if q > 0}
    for isin in list(state.stopped):
        if targets.weights.get(isin, _ZERO) <= _ZERO:
            state.stopped.discard(isin)
    state.stopped |= set(stopped_today)

    slots = max_positions - len(held)
    spendable = mirror.account.spendable_cash
    orders: list[BookOrder] = []
    lines: dict[str, MirrorLine] = {}
    names = sorted(set(held) | {i for i, w in targets.weights.items() if w > _ZERO})
    sells: list[BookOrder] = []
    buys: list[tuple[BookOrder, Decimal]] = []
    for isin in names:
        weight = targets.weights.get(isin, _ZERO)
        quantity = held.get(isin, 0)

        def line(
            status: MirrorStatus,
            target: int | None,
            side: Side | None = None,
            size: int = 0,
            *,
            isin: str = isin,
            weight: Decimal = weight,
            quantity: int = quantity,
        ) -> MirrorLine:
            return MirrorLine(isin, weight, target, quantity, side, size, status)

        if isin in stopped_today:
            lines[isin] = line(MirrorStatus.STOPPED, None)
            continue
        close = mirror.market.close(isin, session)
        if weight <= _ZERO:
            target: int | None = 0
        elif close is None or close <= _ZERO:
            lines[isin] = line(MirrorStatus.UNPRICED, None)
            continue
        else:
            with localcontext(_CONTEXT):
                target = int((weight * nav / close).to_integral_value(rounding=ROUND_FLOOR))
        assert target is not None
        if isin in state.stopped and quantity == 0:
            lines[isin] = line(MirrorStatus.STOPPED, target)
            continue
        delta = target - quantity
        pending = mirror.pending_exits.get(isin)
        if pending is not None and delta < 0 and _within(pending.floor, target):
            lines[isin] = line(MirrorStatus.EXITING, target)
            continue
        if delta == 0:
            lines[isin] = line(MirrorStatus.ON_TARGET, target)
            continue
        if (
            isin not in targets.acted
            and target > 0
            and quantity > 0
            and pending is None
            and _within(quantity, target)
        ):
            lines[isin] = line(MirrorStatus.ON_TARGET, target)
            continue
        why = (
            f"mirror of {targets.book_id}: toward its post-decision weight "
            f"{(weight * _HUNDRED).quantize(_PP)}% ({target} shares at the {session.isoformat()} "
            f"close, {quantity} held)"
        )
        if delta < 0:
            sells.append(BookOrder(isin, Side.SELL, -delta, why, MIRROR_FOLLOW_EVENT))
            lines[isin] = line(MirrorStatus.STAGED, target, Side.SELL, -delta)
            continue
        assert close is not None
        buys.append((BookOrder(isin, Side.BUY, delta, why, MIRROR_FOLLOW_EVENT), close))
        lines[isin] = line(MirrorStatus.STAGED, target, Side.BUY, delta)

    orders.extend(sells)
    for order, close in buys:
        new_name = order.isin not in held
        notional = close * order.quantity
        if new_name and slots <= 0:
            lines[order.isin] = _restatus(lines[order.isin], MirrorStatus.SLOTS)
            continue
        if notional > spendable:
            lines[order.isin] = _restatus(lines[order.isin], MirrorStatus.CASH)
            continue
        orders.append(order)
        spendable -= notional
        if new_name:
            slots -= 1
    return MirrorPlan(mirror.book_id, session, nav, tuple(orders), lines)


def _within(quantity: int, target: int) -> bool:
    """``quantity`` is inside the drift band of ``target`` (exactly equal when the target is 0)."""
    if target == 0:
        return quantity == 0
    with localcontext(_CONTEXT):
        return abs(Decimal(quantity - target)) <= MIRROR_DRIFT_BAND * Decimal(target)


def _restatus(line: MirrorLine, status: MirrorStatus, rails: Sequence[str] = ()) -> MirrorLine:
    return MirrorLine(
        line.isin,
        line.target_weight,
        line.target_quantity,
        line.held,
        line.side,
        line.quantity,
        status,
        tuple(rails),
    )


def settle_plan(
    plan: MirrorPlan, report: DecisionReport, mirror: FundBook
) -> tuple[MirrorLine, ...]:
    """Each planned line's status once the mirror's book has cleared the orders (``report``)."""
    lines = dict(plan.lines)
    if report.halted:
        return tuple(
            _restatus(line, MirrorStatus.HALTED) if line.side is not None else line
            for _, line in sorted(lines.items())
        )
    refused = {order.isin: verdict for order, verdict in report.refused}
    staged = {order.isin for order, _ in report.staged}
    unpriced = {order.isin for order in report.unpriced}
    for isin, line in lines.items():
        if line.side is None or line.status is not MirrorStatus.STAGED:
            continue
        if isin in refused:
            rails = tuple(r.value for r in refused[isin].breached_rails)
            lines[isin] = _restatus(line, MirrorStatus.REFUSED, rails)
        elif isin in staged:
            pending = mirror.pending_exits.get(isin)
            sliced = pending is not None and pending.decided == plan.session
            lines[isin] = _restatus(line, MirrorStatus.SLICED if sliced else MirrorStatus.STAGED)
        elif isin in unpriced or isin in mirror.pending_exits:
            held_over = isin in mirror.pending_exits
            lines[isin] = _restatus(
                line, MirrorStatus.EXITING if held_over else MirrorStatus.UNPRICED
            )
    return tuple(line for _, line in sorted(lines.items()))


def divergence_entry(
    mirror: FundBook,
    targets: TargetWeights,
    lines: Sequence[MirrorLine],
    report: DecisionReport,
    session: date,
) -> tuple[JournalEntry, EvidenceBundle]:
    """The mirror's ``MIRROR_DIVERGENCE`` line for ``session``: the primary's target weight, the
    mirror's weight once its staged orders fill, and why they differ, name by name.

    ``tracking_gap_pp`` is the sum over names of ``|target weight - mirror weight|`` in percentage
    points — 0 when the mirror holds exactly the primary's weights.
    """
    nav, closes = _nav(mirror, session)
    after = post_decision_quantities(mirror, report)
    rows: list[dict[str, str]] = []
    gap = _ZERO
    names = sorted({line.isin for line in lines} | set(after) | set(targets.weights))
    by_isin = {line.isin: line for line in lines}
    for isin in names:
        price = closes.get(isin) or mirror.market.close(isin, session)
        quantity = after.get(isin, 0)
        with localcontext(_CONTEXT):
            weight = (
                _ZERO
                if price is None or nav <= _ZERO or quantity == 0
                else (price * quantity / nav).quantize(_WEIGHT)
            )
            target = targets.weights.get(isin, _ZERO)
            gap += abs(target - weight) * _HUNDRED
        line = by_isin.get(isin)
        row = {
            "isin": isin,
            "target_weight": str(target),
            "mirror_weight": str(weight),
            "status": MirrorStatus.ON_TARGET.value if line is None else line.status.value,
        }
        if line is not None:
            row["rails"] = ",".join(line.rails)
            row["side"] = "" if line.side is None else line.side.value
            row["quantity"] = str(line.quantity)
        rows.append(row)
    off = [r for r in rows if r["status"] not in (MirrorStatus.ON_TARGET.value,)]
    gap_pp = gap.quantize(_PP)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    evidence = EvidenceBundle(
        case_id=mirror.book_id,
        trading_date=session,
        actor=Actor.EXEC,
        items=(
            EvidenceItem(
                kind=EvidenceKind.POSITION,
                source="m17_mirror",
                label="tracking_gap_pp",
                as_of=session,
                value=gap_pp,
            ),
            EvidenceItem(
                kind=EvidenceKind.POSITION,
                source="m17_mirror",
                label="primary_nav",
                as_of=session,
                value=targets.nav,
            ),
            EvidenceItem(
                kind=EvidenceKind.POSITION,
                source="m17_mirror",
                label="mirror_nav",
                as_of=session,
                value=nav,
            ),
        ),
    )
    entry = JournalEntry(
        ts=mirror.clock.now(),
        trading_date=session,
        case_id=mirror.book_id,
        actor=Actor.EXEC,
        decision=Decision.HEARTBEAT,
        evidence_snapshot_ref=evidence.ref().ref,
        rationale=(
            f"mirror of {targets.book_id}: {len(rows)} name(s), {len(off)} not simply on target; "
            f"tracking gap {gap_pp} pp of weight after this session's orders fill"
        ),
        payload={
            "event": MIRROR_DIVERGENCE_EVENT,
            "book": mirror.book_id,
            "mode": PAPER_MODE,
            "primary": targets.book_id,
            "tracking_gap_pp": str(gap_pp),
            "statuses": json.dumps(counts, sort_keys=True, separators=(",", ":")),
            "names": json.dumps(rows, sort_keys=True, separators=(",", ":")),
        },
    )
    _LOG.info(
        "fm_mirror.divergence",
        book=mirror.book_id,
        primary=targets.book_id,
        session=session.isoformat(),
        names=len(rows),
        tracking_gap_pp=str(gap_pp),
        statuses=counts,
    )
    return entry, evidence
