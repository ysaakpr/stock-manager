"""X2: corporate actions applied to the backtest's *book* on their ex-dates — accounting only.

A backtest fills and marks on raw bars (invariant #3). Raw bars step on an ex-date: a 2:1 split
halves the close, a ₹10 dividend takes ₹10 off it. The exchange and the depository make the holder
whole on the same day — twice the shares, or the cash — and a book that does not do the same shows
the step as a loss: a holding through a split "halves", a trailing stop fires on it, and a strategy
that collects dividends is compared against a total-return benchmark that counts them while it does
not. This module is the missing half of that walk: on each session it applies the actions whose
ex-date has arrived to both books the backtest keeps — the ``SimBroker`` the policy reads and
trades against, and the ``PortfolioBook`` the report is struck from — so share counts, basis and
cash move exactly as the account's would.

**Why this read ignores ``knowable_date`` — and why that is not a PIT leak.** Every
``corporate_actions`` row today carries ``knowable_date = 2026-09-07`` (an ingest-time defect:
the backfill stamped the day it ran). Read through the PIT layer, no action would ever reach a
backtest before 2026. But what happens *here* is not a decision. On the ex-date the holder's share
count doubles and the dividend is fixed whether or not the strategy knew it was coming; applying it
by ``ex_date`` is the mechanical accounting the exchange performs, the same fact the raw price
already reflects that morning. So this read is keyed on ``ex_date`` and deliberately bypasses
``knowable_date`` — and for exactly that reason it is kept *physically apart* from the decision
path:

* nothing here imports ``dataplatform.query`` or touches a ``PitContext``;
* the source is handed to the accounting broker (``backtest.run._AccountingBroker``), never to a
  policy or its data object — a policy sees an action only as its consequence on the account
  (holdings, cash), exactly as a live policy would read it off its broker the next morning;
* ``tests/unit/test_book_actions.py`` pins both: the module's import graph, and a replay in which a
  policy's every data read is byte-identical with and without the wiring.

A signal that wanted to *know* a split was coming would have to read it through the PIT layer, and
would (correctly) see nothing until the knowable-date defect is repaired. That is a separate fix.

**One further consumer, and why it is not a decision input either (X2).** L2's adjusted series
starts on 2016-09-02; before it the swing features read raw closes, so a lookback return that
straddled the seam divided an adjusted price by a raw one (HPCL: ₹1,210.40 raw → ₹182.47 adjusted
overnight). :func:`signal_split_factors` hands the same SPLIT/BONUS rescales to
``backtest.run._SwingFeatures``, which back-adjusts the pre-seam raw closes onto L2's basis. That
is what L2 itself is — a price series rebuilt from these very rows by ex-date — so it gives a
signal no fact L2 does not already carry, and a return struck inside a window only ever sees the
factors of actions ex-dated inside that window, which its raw prices already reflect. It is a
separate switch from the book's: ``--no-book-corporate-actions`` changes the book and nothing
else, so a before/after book measurement still holds the signal fixed.

**What is modelled.**

* ``SPLIT`` (``FaceValueTerms``): shares x ``from / to``; basis unchanged.
* ``BONUS`` (``RatioTerms``): shares x ``(new + held) / held``; basis unchanged.
* ``DIVIDEND`` with a rupee amount: ``shares x per_share`` credited to cash on the ex-date, on the
  shares held at the start of that session (bought before the ex-date, not yet sold) — the
  entitlement the record date fixes. Credited on the ex-date rather than the payment date (not in
  the store, typically within 30 days) so NAV is continuous across the price drop; the cost is cash
  available a few weeks early. Gross of TDS (taxes are post-processed).
* **ISIN reissue.** A face-value split usually retires the ISIN; the action is stored against the
  survivor (``filed_against_isin`` names the retired one) while the book, which bought off raw L1,
  holds the predecessor. When a SPLIT/BONUS for survivor ``S`` sits on the effective date of a
  lineage edge ``P → S``, the holding in ``P`` is carried to ``S`` 1:1 and then rescaled. A dividend
  stored under ``S`` but dated before the reissue is paid on the ISIN that was live on its ex-date.

**What is not, and why.** ``MERGER``, ``DEMERGER`` and ``SCHEME_OF_ARRANGEMENT`` rows in the store
are all ``UnquantifiedTerms`` and none names the counterparty ISIN, so the book cannot apply them
without inventing terms; ``RIGHTS`` is a subscription decision, and an un-exercised entitlement
lapses. Each is counted and logged when the book holds the name on its ex-date
(``book_actions.unmodelled``), never silently dropped. A reissue with no split/bonus on its date is
left alone and logged: carrying it 1:1 would be a guess that the shares did not change. A dividend
stated only as a percentage of face value is skipped for the same reason (none exist today).

Fractional entitlements (a 3:2 bonus on an odd count) are floored and forfeited — the conservative
side of the cash-in-lieu the store does not carry. Money is ``Decimal`` throughout; nothing here
reads a clock.
"""

from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

import structlog

from backtest.accounting import BookError, PortfolioBook
from execution.sim_broker import SimBroker

if TYPE_CHECKING:
    from dataplatform.store.db import Connection

_ZERO = Decimal("0")
_ONE = Decimal("1")

_log = structlog.get_logger(__name__)

__all__ = [
    "AppliedBookAction",
    "AppliedCarry",
    "AppliedDividend",
    "AppliedRescale",
    "BookActionApplier",
    "BookActionCalendar",
    "BookActionSource",
    "CashDividend",
    "RescaleKind",
    "ShareRescale",
    "UnmodelledAction",
    "add_book_actions_flag",
    "book_corporate_actions",
    "current_book_actions",
    "current_signal_split_factors",
    "load_book_actions",
    "load_store_book_actions",
    "signal_split_factors",
    "store_book_actions_unless",
]


# ── the actions, in the book's terms ─────────────────────────────────────────────────────────────


class RescaleKind(StrEnum):
    """Which share-count action a :class:`ShareRescale` came from — for the log and the ledger."""

    SPLIT = "SPLIT"
    BONUS = "BONUS"


@dataclass(frozen=True, slots=True)
class ShareRescale:
    """Multiply the shares of ``isin`` by ``numerator / denominator`` on ``ex_date``; basis fixed.

    ``carried_from`` names a retired predecessor ISIN whose holding continues as ``isin`` from this
    ex-date (an NSE reissue); it is carried 1:1 *before* the rescale.
    """

    isin: str
    ex_date: date
    kind: RescaleKind
    numerator: Decimal
    denominator: Decimal
    carried_from: str | None = None

    def __post_init__(self) -> None:
        for name in ("numerator", "denominator"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be a Decimal")
            if value <= _ZERO:
                raise ValueError(f"{name} must be positive, got {value}")


@dataclass(frozen=True, slots=True)
class CashDividend:
    """Credit ``per_share`` rupees on every share of ``isin`` held at the start of ``ex_date``."""

    isin: str
    ex_date: date
    per_share: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.per_share, Decimal):
            raise TypeError("per_share must be a Decimal — money is never float (CLAUDE.md)")
        if self.per_share <= _ZERO:
            raise ValueError(f"per_share must be positive, got {self.per_share}")


@dataclass(frozen=True, slots=True)
class UnmodelledAction:
    """An action the book cannot apply (see the module docstring) — kept so a hit is counted."""

    isin: str
    ex_date: date
    action_type: str


BookAction = ShareRescale | CashDividend | UnmodelledAction


# ── what an applier did — the record a run's tax ledger is built from ─────────────────────────


@dataclass(frozen=True, slots=True)
class AppliedDividend:
    """``amount`` rupees of dividend on ``isin`` credited to both books on ``session``."""

    isin: str
    session: date
    amount: Decimal


@dataclass(frozen=True, slots=True)
class AppliedCarry:
    """Every share of ``from_isin`` carried 1:1 to ``isin`` on ``ex_date`` (an ISIN reissue)."""

    from_isin: str
    isin: str
    ex_date: date


@dataclass(frozen=True, slots=True)
class AppliedRescale:
    """A split or bonus applied on ``ex_date``: ``old_quantity`` shares became ``new_quantity``.

    ``new_quantity`` is the floored count the books actually hold (a fraction is forfeited), which
    is what a tax-lot rebuild has to reach, not the unfloored ratio.
    """

    isin: str
    ex_date: date
    kind: RescaleKind
    numerator: Decimal
    denominator: Decimal
    old_quantity: int
    new_quantity: int


AppliedBookAction = AppliedDividend | AppliedCarry | AppliedRescale

#: Within one ex-date: dividends first (paid on the pre-action count), then reissue carries and
#: rescales, then the unmodelled notices. Then ISIN, for a stable order.
_ORDER = {CashDividend: 0, ShareRescale: 1, UnmodelledAction: 2}


def _sort_key(action: BookAction) -> tuple[date, int, str]:
    return (action.ex_date, _ORDER[type(action)], action.isin)


class BookActionSource(Protocol):
    """Where the walk gets the actions whose ex-date falls in a window of sessions."""

    def between(self, after: date | None, upto: date) -> tuple[BookAction, ...]:
        """Every action with ``after < ex_date <= upto`` (``after=None``: no lower bound)."""


class BookActionCalendar:
    """An in-memory, ex-date-ordered set of book actions — the production and the test source."""

    def __init__(self, actions: Iterable[BookAction] = ()) -> None:
        self._actions = tuple(sorted(actions, key=_sort_key))
        self._dates = [action.ex_date for action in self._actions]

    def between(self, after: date | None, upto: date) -> tuple[BookAction, ...]:
        lo = 0 if after is None else bisect_right(self._dates, after)
        hi = bisect_right(self._dates, upto)
        return self._actions[lo:hi]

    def __len__(self) -> int:
        return len(self._actions)

    def counts(self) -> dict[str, int]:
        """How many of each kind the calendar carries, for the run log."""
        tally: Counter[str] = Counter()
        for action in self._actions:
            if isinstance(action, UnmodelledAction):
                tally[f"unmodelled:{action.action_type}"] += 1
            elif isinstance(action, ShareRescale):
                tally[action.kind.value] += 1
            else:
                tally["DIVIDEND"] += 1
        return dict(sorted(tally.items()))

    def first_on_or_after(self, day: date) -> date | None:
        """The earliest ex-date on or after ``day`` (a debugging aid)."""
        index = bisect_left(self._dates, day)
        return self._dates[index] if index < len(self._dates) else None


# ── the process-wide switch the drivers read ─────────────────────────────────────────────────────

_CURRENT: ContextVar[BookActionSource | None] = ContextVar("book_actions", default=None)


@contextmanager
def book_corporate_actions(source: BookActionSource | None) -> Iterator[None]:
    """Run the enclosed backtest(s) with ``source`` applied to the book on every ex-date.

    The drivers build their accounting broker deep inside a dozen report functions; this is how a
    CLI hands every one of them the same calendar without threading a parameter through each. The
    same shape as ``backtest.run.require_published_benchmark``. ``None`` switches it off.
    """
    token = _CURRENT.set(source)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_book_actions() -> BookActionSource | None:
    """The source :func:`book_corporate_actions` put in force, or ``None``."""
    return _CURRENT.get()


_SIGNAL: ContextVar[tuple[ShareRescale, ...] | None] = ContextVar(
    "signal_split_factors", default=None
)


@contextmanager
def signal_split_factors(source: BookActionSource | None) -> Iterator[None]:
    """Give the swing features ``source``'s SPLIT/BONUS rescales for the pre-2016-09 seam (X2).

    Only the share rescales are carried: they are the price factors. Dividends and unmodelled
    actions are not, because L2's ``adj_close`` carries no dividend effect either. ``None`` turns
    it off, and the features then exclude a name's pre-seam window rather than mix bases.
    """
    rescales = (
        None
        if source is None
        else tuple(a for a in source.between(None, date.max) if isinstance(a, ShareRescale))
    )
    token = _SIGNAL.set(rescales)
    try:
        yield
    finally:
        _SIGNAL.reset(token)


def current_signal_split_factors() -> tuple[ShareRescale, ...] | None:
    """The rescales :func:`signal_split_factors` put in force, or ``None``."""
    return _SIGNAL.get()


# ── applying them ────────────────────────────────────────────────────────────────────────────────


class BookActionApplier:
    """Applies each session's due actions to a ``SimBroker`` and its mirror ``PortfolioBook``.

    Call :meth:`apply` once per session, **before** that session's ``execute_session``: the shares
    held at that point are exactly the ones bought before the ex-date, and the orders staged for the
    session fill on the post-action count at the raw ex-date price. An ex-date the walk did not
    visit (a gap in the calendar) is applied on the next session it does. The two books are checked
    share for share after every action; a disagreement raises rather than drifts.

    What it never does: read a price, a signal or a clock, or apply an action to a name neither
    book holds.
    """

    def __init__(self, source: BookActionSource) -> None:
        self._source = source
        self._last: date | None = None
        self.applied: Counter[str] = Counter()
        #: Every action that moved shares or cash, in the order it was applied — the run's tax
        #: ledger (``backtest.run_ledger``) is rebuilt from this and the fills, nothing else.
        self.log: list[AppliedBookAction] = []

    def apply(self, session: date, *, sim: SimBroker, book: PortfolioBook) -> None:
        """Apply every action with ex-date in ``(previous session, session]``."""
        if self._last is not None and session <= self._last:
            raise BookError(
                f"book actions run forward: {session.isoformat()} after {self._last.isoformat()}"
            )
        for action in self._source.between(self._last, session):
            if isinstance(action, CashDividend):
                self._dividend(action, session, sim, book)
            elif isinstance(action, ShareRescale):
                self._rescale(action, sim, book)
            else:
                self._unmodelled(action, sim)
        self._last = session

    def _dividend(
        self, action: CashDividend, session: date, sim: SimBroker, book: PortfolioBook
    ) -> None:
        quantity = _entitled(action.isin, action.ex_date, sim)
        if quantity == 0:
            return
        amount = book.credit_dividend(session, action.isin, per_share=action.per_share)
        sim.credit_corporate_cash(
            session, action.isin, amount, f"DIVIDEND {quantity} x {action.per_share}"
        )
        _check_agree(action.isin, sim, book)
        self.applied["DIVIDEND"] += 1
        self.log.append(AppliedDividend(action.isin, session, amount))

    def _rescale(self, action: ShareRescale, sim: SimBroker, book: PortfolioBook) -> None:
        if action.carried_from is not None and sim.held_quantity(action.carried_from) > 0:
            sim.carry_over(action.carried_from, action.isin)
            book.apply_merger(
                action.carried_from,
                surviving_isin=action.isin,
                shares_received=_ONE,
                shares_held=_ONE,
            )
            _check_agree(action.isin, sim, book)
            self.applied["REISSUE"] += 1
            self.log.append(AppliedCarry(action.carried_from, action.isin, action.ex_date))
        if _entitled(action.isin, action.ex_date, sim) == 0:
            return
        old, new = sim.apply_share_rescale(
            action.isin,
            numerator=action.numerator,
            denominator=action.denominator,
            ex_date=action.ex_date,
        )
        if action.kind is RescaleKind.SPLIT:
            book.apply_split(
                action.isin,
                from_face_value=action.numerator,
                to_face_value=action.denominator,
                forfeit_fraction=True,
            )
        else:
            # A bonus's (new + held) / held, handed back to the book in its own terms.
            book.apply_bonus(
                action.isin,
                new_shares=action.numerator - action.denominator,
                held_shares=action.denominator,
                forfeit_fraction=True,
            )
        _check_agree(action.isin, sim, book)
        self.applied[action.kind.value] += 1
        self.log.append(
            AppliedRescale(
                isin=action.isin,
                ex_date=action.ex_date,
                kind=action.kind,
                numerator=action.numerator,
                denominator=action.denominator,
                old_quantity=old,
                new_quantity=new,
            )
        )

    def _unmodelled(self, action: UnmodelledAction, sim: SimBroker) -> None:
        if sim.held_quantity(action.isin) == 0:
            return
        self.applied[f"unmodelled:{action.action_type}"] += 1
        _log.warning(
            "book_actions.unmodelled",
            isin=action.isin,
            ex_date=action.ex_date.isoformat(),
            action_type=action.action_type,
            detail="held on the ex-date; the store carries no applicable terms, book unchanged",
        )


def _entitled(isin: str, ex_date: date, sim: SimBroker) -> int:
    """Shares of ``isin`` entitled on ``ex_date`` — every lot traded before it, settled or pending.

    The exchange rule (``SimBroker``'s corporate-action section states it for both T+2 and T+1):
    entitlement follows the trade date, so a buy still pending settlement on the ex-date is
    entitled. The applier runs before the first fill on or after the ex-date, so every lot on the
    book traded before it; one that did not would be rescaled or paid in ``PortfolioBook`` (which
    keeps no trade dates) but not in the broker, so that is refused rather than let the books part.
    The check sees pending lots only — a settled holding carries no trade date — which is where
    such a lot would be: nothing traded on the ex-date can have settled before it.
    """
    entitled = sim.held_quantity(isin, bought_before=ex_date)
    on_book = sim.held_quantity(isin)
    if entitled != on_book:
        raise BookError(
            f"{isin} holds {on_book - entitled} shares traded on or after the ex-date "
            f"{ex_date.isoformat()}; corporate actions must be applied before that session's fills"
        )
    return entitled


def _check_agree(isin: str, sim: SimBroker, book: PortfolioBook) -> None:
    position = book.position(isin)
    in_book = 0 if position is None else position.quantity
    in_sim = sim.held_quantity(isin)
    if in_book != in_sim:
        raise BookError(
            f"after a corporate action the books disagree on {isin}: SimBroker {in_sim}, "
            f"PortfolioBook {in_book}"
        )


# ── reading them out of the store ────────────────────────────────────────────────────────────────


def load_book_actions(conn: Connection) -> BookActionCalendar:
    """Every reconciled corporate action, in book terms, keyed by ``ex_date`` (never knowable_date).

    Reads through the corporate-action module's one door for trusted rows,
    ``dataplatform.corpactions.load_reconciled_actions`` (reconciled only, one row per event), and
    the ISIN lineage through ``dataplatform.identity.LineageStore``. See the module docstring for
    why ``knowable_date`` is deliberately not consulted here, and why that is confined to the book.
    """
    from dataplatform.corpactions import load_reconciled_actions
    from dataplatform.identity import LineageStore

    actions = load_reconciled_actions(conn)
    resolver = LineageStore(conn).load()
    calendar = BookActionCalendar(
        _to_book_actions(actions, resolver.chain_to, resolver.effective_date)
    )
    _log.info("book_actions.loaded", total=len(calendar), **calendar.counts())
    return calendar


class _ActionRow(Protocol):
    """The fields of ``dataplatform.ingest.corp_actions.CorporateAction`` this module reads."""

    @property
    def isin(self) -> str: ...
    @property
    def ex_date(self) -> date: ...
    @property
    def action_type(self) -> object: ...
    @property
    def terms(self) -> object: ...


def _to_book_actions(
    rows: Iterable[_ActionRow],
    chain_to: _ChainTo,
    effective_date: _EffectiveDate,
) -> list[BookAction]:
    """Translate store rows into book actions, resolving each onto the ISIN held on its ex-date."""
    from dataplatform.corpactions import DividendTerms, FaceValueTerms, RatioTerms

    out: list[BookAction] = []
    skipped: Counter[str] = Counter()
    for row in rows:
        action_type = str(getattr(row.action_type, "value", row.action_type))
        chain = chain_to(row.isin)
        live = _live_isin(row.isin, row.ex_date, chain, effective_date)
        terms = row.terms
        if action_type == "SPLIT" and isinstance(terms, FaceValueTerms):
            out.append(
                ShareRescale(
                    isin=live,
                    ex_date=row.ex_date,
                    kind=RescaleKind.SPLIT,
                    numerator=terms.from_value,
                    denominator=terms.to_value,
                    carried_from=_reissued_on(live, row.ex_date, chain, effective_date),
                )
            )
        elif action_type == "BONUS" and isinstance(terms, RatioTerms):
            out.append(
                ShareRescale(
                    isin=live,
                    ex_date=row.ex_date,
                    kind=RescaleKind.BONUS,
                    numerator=terms.new_shares + terms.held_shares,
                    denominator=terms.held_shares,
                    carried_from=_reissued_on(live, row.ex_date, chain, effective_date),
                )
            )
        elif action_type == "DIVIDEND":
            if isinstance(terms, DividendTerms) and terms.amount_inr is not None:
                out.append(CashDividend(isin=live, ex_date=row.ex_date, per_share=terms.amount_inr))
            else:
                skipped["DIVIDEND:no_rupee_amount"] += 1
        elif action_type in ("MERGER", "DEMERGER", "SCHEME_OF_ARRANGEMENT", "RIGHTS"):
            out.append(UnmodelledAction(isin=live, ex_date=row.ex_date, action_type=action_type))
        else:
            skipped[action_type] += 1  # BUYBACK, DELISTING, NAME_CHANGE: no share/cash effect here
    if skipped:
        _log.info("book_actions.skipped", **dict(sorted(skipped.items())))
    return out


class _ChainTo(Protocol):
    def __call__(self, isin: str, /) -> tuple[str, ...]: ...


class _EffectiveDate(Protocol):
    def __call__(self, predecessor: str, /) -> date | None: ...


def _live_isin(
    survivor: str, on: date, chain: Sequence[str], effective_date: _EffectiveDate
) -> str:
    """The member of ``survivor``'s lineage chain that was trading on ``on``.

    A predecessor ``P`` trades until its successor's first session, ``effective_date(P)``; the
    survivor has no end. So the live ISIN is the chain member whose end is after ``on`` and earliest
    — in a linear chain an older predecessor always ends no later than a newer one.
    """
    best, best_end = survivor, date.max
    for member in chain:
        if member == survivor:
            continue
        end = effective_date(member)
        if end is not None and on < end < best_end:
            best, best_end = member, end
    return best


def _reissued_on(
    live: str, on: date, chain: Sequence[str], effective_date: _EffectiveDate
) -> str | None:
    """The predecessor whose successor first trades on ``on``: the holding carried into ``live``."""
    for member in chain:
        if member != live and effective_date(member) == on:
            return member
    return None


def load_store_book_actions() -> BookActionCalendar:
    """:func:`load_book_actions` over the configured Postgres (``dataplatform.store.db``)."""
    from dataplatform.store.db import connect

    with connect() as conn:
        return load_book_actions(conn)


# ── the CLI switch the three backtest entry points share ─────────────────────────────────────────


def add_book_actions_flag(parser: argparse.ArgumentParser) -> None:
    """Add ``--no-book-corporate-actions`` — the switch that restores the pre-wiring book."""
    parser.add_argument(
        "--no-book-corporate-actions",
        dest="book_corporate_actions",
        action="store_false",
        help="do not apply splits, bonuses and cash dividends to the book on their ex-dates (the "
        "pre-X2-fix accounting, kept for before/after measurement). The default reads every "
        "reconciled corporate action from Postgres and applies it to the book only — never to a "
        "signal",
    )
    parser.set_defaults(book_corporate_actions=True)


def store_book_actions_unless(args: argparse.Namespace) -> AbstractContextManager[None]:
    """The context a CLI runs its backtest in: the store's actions, unless the flag said no.

    The signal's pre-seam split factors (:func:`signal_split_factors`) are loaded either way — the
    flag switches the *book* wiring off for a before/after measurement, and holding the signal
    fixed is what keeps that measurement a measurement of the book.
    """
    return _store_actions(apply_to_book=getattr(args, "book_corporate_actions", True))


@contextmanager
def _store_actions(*, apply_to_book: bool) -> Iterator[None]:
    calendar = load_store_book_actions()
    with signal_split_factors(calendar):
        if apply_to_book:
            with book_corporate_actions(calendar):
                yield
        else:
            yield
