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
  holds the predecessor. The split and the reissue are two events on two dates: the split is ex on
  the predecessor's *last* session (``P``'s own bar already prints the post-split price), and the
  successor first trades on the next one — the lineage edge's ``effective_date`` (HDFC Bank 2019:
  ex 19-09 on INE040A01026, INE040A01034 from 20-09). So the rescale lands on ``P`` on its ex-date,
  and an :class:`IsinReissue` carries the (already rescaled) holding ``P → S`` 1:1 on the edge's
  effective date — settled shares, every pending lot with its own trade date, and every staged
  order. When both fall on one date the carry runs first. A carry is made only when a SPLIT/BONUS of
  that chain is ex within :data:`_REISSUE_WINDOW_DAYS` on or before the effective date, which is
  what explains a 1:1 continuation; a reissue nothing explains is counted and logged when held
  (``book_actions.reissue_unexplained``) and left alone — carrying it would be a guess that the
  share count did not change. A dividend stored under ``S`` but dated before the reissue is paid on
  the ISIN that was live on its ex-date.

**Mergers and delisting exits, from curated terms.** ``MERGER`` rows in the store are all
``UnquantifiedTerms`` and none names the transferee, so the book reads the terms instead from the
reviewed, sourced table ``dataplatform.corpactions.merger_terms`` (every ratio quoted from an L0
document). A :class:`ShareSwap` converts the held old-ISIN shares into the survivor on the
scheme's record date — or on the survivor's first priced session when that is later (Indiabulls
Housing Finance first printed four months after its record date), so a holding is never parked on
an ISIN with no close: ``floor(held x received / held_ratio)`` shares, the fraction forfeited as a
split's is, the *whole* cost basis carried, and every lot keeping its own trade date (the tax
ledger rebuilds it as a rescale of the old lots and a carry — Sec 47(vii) makes the swap no
transfer and Sec 2(42A) counts the holding from the original purchase). Mechanically it is the
rescale and the carry this module already performs for a reissue, in that order, so unsettled
lots settle their converted count on their own T+N and staged orders follow the shares. A
:class:`CashExit` surrenders the holding at the sourced exit price on the delisting date — the
first day of the exit window — and books it as a sale. A swap whose scheme also pays a non-share
leg (Cairn India's four Vedanta redeemable preference shares per share) carries it as
``cash_per_share``: ``old shares x cash_per_share`` rupees credited on the swap date, at the
leg's sourced face value, with no basis apportioned to it (``AppliedSchemeCash``). The tax
ledger does not yet tax that leg — it carries the swap's share events only, so a run's tax
report understates by the gain on it; the applier logs every credit so a report can name it.
Both are keyed on dates, like every other
action here, never on ``knowable_date``, and neither reaches a signal. A store merger row the
table covers is replaced by it; one it does not is named at load (``book_actions.merger_skipped``),
and a scheme the table lists as unsourced is counted when held, never guessed.

**Curated and price-implied splits — the events L2 composes, applied to the count.** The feeds
miss some share-basis changes: a 2013 bonus before their history, an ETF unit split no equity feed
lists, a split filed under an ISIN a reissue retired. L2 composes two further sources into its
factor chain for them — the sourced rows of ``dataplatform.corpactions.manual_actions`` and the
splits ``dataplatform.corpactions.implied`` reads off L1 — so the *price* steps vanish; this module
takes the very same composition (``dataplatform.store.l2.compose_events``, through
``compose_lake_events``) so the *share count* steps with it. A position held across one gets the
shares the depository credited, not a phantom 99 % loss. Only their SPLIT/BONUS rows scale shares;
a curated DEMERGER or scheme is a structural break and becomes an :class:`UnmodelledAction`, and
an explained move is not an event at all and never reaches here. Three guards hold at load
(:func:`added_book_events`):

* **One fact, one rescale.** For every ISIN and ex-date L2 added an event on, the product of the
  book's feed ratios and added ratios must equal the composed chain's quantity factor there. The
  one way they can part is a persisted ``adjustment_factors`` chain that lags the reconciled rows:
  L2 then *implies* the feed's split back from L1 on its own ex-date, and applying both would
  double the count. When the feed ratios alone already equal L2's factor, the added events are
  dropped (``book_actions.added_event_superseded``). Any other disagreement raises — a book that
  does not match its prices is red data, not a run.
* **PIT.** A curated row's ``knowable_date`` and an implied split's (its own ex-date, the session
  whose bar reveals it) must be on or before the ex-date: :class:`ShareRescale` refuses one that is
  not, since applying it on the ex-date would act on a fact before anyone could know it. The book
  then applies it on the ex-date, exactly as a feed's split. That the implied scan reads L1 bars
  after the ex-date (its "level does not revert" test) changes nothing a policy can see: the
  event reaches the account on its ex-date, as the exchange's own split does.
* **Identity.** Their rows are counted apart in :meth:`BookActionCalendar.counts`
  (``SPLIT:implied``, ``BONUS:curated``) and hashed by :meth:`BookActionCalendar.added_identity`,
  so a run made with them never shares a digest with one made before them, nor with one whose
  curated ratio was later corrected.

**What is not, and why.** ``DEMERGER`` and ``SCHEME_OF_ARRANGEMENT`` rows in the store are all
``UnquantifiedTerms`` and none names the counterparty ISIN, so the book cannot apply them
without inventing terms (the lineage table is not a merger map either: one issuer, linear chains).
``RIGHTS`` is a subscription decision, and an un-exercised entitlement lapses. Each is counted and
logged when the book holds the name on its ex-date (``book_actions.unmodelled``), never silently
dropped. A dividend stated only as a percentage of face value is skipped for the same reason (none
exist today).

Fractional entitlements (a 3:2 bonus on an odd count) are floored and forfeited — the conservative
side of the cash-in-lieu the store does not carry. Money is ``Decimal`` throughout; nothing here
reads a clock.
"""

from __future__ import annotations

import argparse
import hashlib
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
    from dataplatform.corpactions import MergerTerms
    from dataplatform.ingest.corp_actions import CorporateAction
    from dataplatform.store.db import Connection
    from dataplatform.store.l2 import ComposedEvents

_ZERO = Decimal("0")
_ONE = Decimal("1")

_log = structlog.get_logger(__name__)

__all__ = [
    "AppliedBookAction",
    "AppliedCarry",
    "AppliedCashExit",
    "AppliedDividend",
    "AppliedMerger",
    "AppliedRescale",
    "AppliedSchemeCash",
    "BookActionApplier",
    "BookActionCalendar",
    "BookActionSource",
    "CashDividend",
    "CashExit",
    "IsinReissue",
    "RescaleKind",
    "RescaleSource",
    "ShareRescale",
    "ShareSwap",
    "UnmodelledAction",
    "add_book_actions_flag",
    "added_book_events",
    "book_corporate_actions",
    "corporate_actions_in_force",
    "current_book_actions",
    "current_signal_split_factors",
    "current_signal_split_factors_identity",
    "load_book_actions",
    "load_store_book_actions",
    "merger_term_actions",
    "signal_split_factors",
    "store_book_actions_unless",
]


# ── the actions, in the book's terms ─────────────────────────────────────────────────────────────


class RescaleSource(StrEnum):
    """Where a :class:`ShareRescale` came from: a feed's reconciled row, or what L2 added to it."""

    FEED = "feed"
    CURATED = "curated"
    IMPLIED = "implied"


class RescaleKind(StrEnum):
    """Which share-count action a :class:`ShareRescale` came from — for the log and the ledger."""

    SPLIT = "SPLIT"
    BONUS = "BONUS"


@dataclass(frozen=True, slots=True)
class ShareRescale:
    """Multiply the shares of ``isin`` by ``numerator / denominator`` on ``ex_date``; basis fixed.

    ``carried_from`` names a retired predecessor ISIN whose holding continues as ``isin`` from this
    ex-date (an NSE reissue); it is carried 1:1 *before* the rescale. ``source`` says whether a
    feed published it or L2 added it (curated, implied); an added one carries the
    ``knowable_date`` it was disseminated on, which must not be after the ex-date it applies on.
    """

    isin: str
    ex_date: date
    kind: RescaleKind
    numerator: Decimal
    denominator: Decimal
    carried_from: str | None = None
    source: RescaleSource = RescaleSource.FEED
    knowable_date: date | None = None

    def __post_init__(self) -> None:
        for name in ("numerator", "denominator"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be a Decimal")
            if value <= _ZERO:
                raise ValueError(f"{name} must be positive, got {value}")
        if self.knowable_date is not None and self.knowable_date > self.ex_date:
            raise ValueError(
                f"{self.isin} {self.kind} on {self.ex_date.isoformat()} is knowable only from "
                f"{self.knowable_date.isoformat()}; the book would apply it before it was known"
            )

    @property
    def tally_key(self) -> str:
        """``SPLIT``/``BONUS`` for a feed's row, ``SPLIT:implied`` etc. for one L2 added."""
        if self.source is RescaleSource.FEED:
            return self.kind.value
        return f"{self.kind.value}:{self.source.value}"


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


@dataclass(frozen=True, slots=True)
class IsinReissue:
    """On ``ex_date``, the successor's first session, the holding in ``from_isin`` becomes ``isin``.

    One lineage edge ``from_isin → isin``. ``explained`` says a SPLIT/BONUS of the chain is ex
    within :data:`_REISSUE_WINDOW_DAYS` on or before ``ex_date``; only then is the holding carried.
    """

    isin: str
    ex_date: date
    from_isin: str
    explained: bool


@dataclass(frozen=True, slots=True)
class ShareSwap:
    """On ``ex_date`` every share of ``isin`` becomes ``numerator / denominator`` of the survivor.

    An amalgamation from the curated terms: ``numerator`` shares of ``surviving_isin`` (the ISIN
    live on ``ex_date``) for every ``denominator`` of ``isin``, in the scheme's own old → new
    order. ``ex_date`` is the record date, or the survivor's first priced session if later;
    ``record_date`` and ``knowable_date`` are carried for the log and the run specification.
    ``cash_per_share`` is the scheme's non-share leg in rupees per old share (zero for a pure
    share swap), credited on ``ex_date`` alongside the conversion.
    """

    isin: str
    ex_date: date
    surviving_isin: str
    numerator: Decimal
    denominator: Decimal
    record_date: date
    knowable_date: date
    cash_per_share: Decimal = _ZERO

    def __post_init__(self) -> None:
        if self.isin == self.surviving_isin:
            raise ValueError(f"{self.isin}: a share swap's survivor must be another ISIN")
        for name in ("numerator", "denominator"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be a Decimal")
            if value <= _ZERO:
                raise ValueError(f"{name} must be positive, got {value}")
        if not isinstance(self.cash_per_share, Decimal):
            raise TypeError("cash_per_share must be a Decimal — money is never float (CLAUDE.md)")
        if self.cash_per_share < _ZERO:
            raise ValueError(f"cash_per_share must not be negative, got {self.cash_per_share}")


@dataclass(frozen=True, slots=True)
class CashExit:
    """On ``ex_date`` (the delisting) every share of ``isin`` is surrendered at ``price``."""

    isin: str
    ex_date: date
    price: Decimal
    knowable_date: date

    def __post_init__(self) -> None:
        if not isinstance(self.price, Decimal):
            raise TypeError("price must be a Decimal — money is never float (CLAUDE.md)")
        if self.price <= _ZERO:
            raise ValueError(f"exit price must be positive, got {self.price}")


BookAction = IsinReissue | ShareRescale | CashDividend | ShareSwap | CashExit | UnmodelledAction

#: How far before a lineage edge's effective date a SPLIT/BONUS may be ex and still explain the
#: reissue. Measured over the store's 593 edges: the explaining action is ex on the effective date
#: (198), or 1-7 calendar days before it — the predecessor's last session, across a weekend or a
#: holiday (291); 104 edges have no action within 10 days.
_REISSUE_WINDOW_DAYS = 10


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


@dataclass(frozen=True, slots=True)
class AppliedMerger:
    """A share swap applied on ``ex_date``: ``old_quantity`` of ``from_isin`` became
    ``new_quantity`` of ``isin`` (floored; the fraction forfeited) at ``numerator : denominator``.
    """

    from_isin: str
    isin: str
    ex_date: date
    numerator: Decimal
    denominator: Decimal
    old_quantity: int
    new_quantity: int


@dataclass(frozen=True, slots=True)
class AppliedCashExit:
    """``quantity`` shares of ``isin`` surrendered at ``price`` on ``ex_date`` for ``amount``."""

    isin: str
    ex_date: date
    quantity: int
    price: Decimal
    amount: Decimal


@dataclass(frozen=True, slots=True)
class AppliedSchemeCash:
    """The non-share leg of a swap: ``quantity`` old shares of ``isin`` (converted into
    ``surviving_isin``) paid ``per_share`` rupees each — ``amount`` — on ``ex_date``.
    """

    isin: str
    surviving_isin: str
    ex_date: date
    quantity: int
    per_share: Decimal
    amount: Decimal


AppliedBookAction = (
    AppliedDividend
    | AppliedCarry
    | AppliedRescale
    | AppliedMerger
    | AppliedCashExit
    | AppliedSchemeCash
)

#: Within one ex-date: reissue carries first (1:1, so no count changes, and a dividend or split
#: dated the survivor's first session then finds the holding under the survivor), then dividends
#: (paid on the pre-action count), then rescales, then share swaps (a survivor's own split that
#: day must not rescale shares that were not yet its own) and cash exits, then the unmodelled
#: notices (which then see nothing held for a name a curated term converted). Then ISIN.
_ORDER = {
    IsinReissue: 0,
    CashDividend: 1,
    ShareRescale: 2,
    ShareSwap: 3,
    CashExit: 4,
    UnmodelledAction: 5,
}


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
                tally[action.tally_key] += 1
            elif isinstance(action, IsinReissue):
                tally["REISSUE" if action.explained else "REISSUE:unexplained"] += 1
            elif isinstance(action, ShareSwap):
                tally["MERGER:share_swap"] += 1
            elif isinstance(action, CashExit):
                tally["MERGER:cash_exit"] += 1
            else:
                tally["DIVIDEND"] += 1
        return dict(sorted(tally.items()))

    def merger_terms_identity(self) -> str | None:
        """A content hash of the share swaps and cash exits — ``None`` when there are none.

        The run specification carries it beside :meth:`counts`: a corrected ratio changes a run's
        result without changing any count, so it must change the run's digest too.
        """
        rows = sorted(
            f"swap|{a.isin}|{a.ex_date}|{a.surviving_isin}|{a.numerator}|{a.denominator}"
            + (f"|cash={a.cash_per_share}" if a.cash_per_share else "")
            if isinstance(a, ShareSwap)
            else f"exit|{a.isin}|{a.ex_date}|{a.price}"
            for a in self._actions
            if isinstance(a, ShareSwap | CashExit)
        )
        if not rows:
            return None
        digest = hashlib.sha256("\n".join(rows).encode()).hexdigest()[:16]
        return f"merger_terms[{len(rows)}]:{digest}"

    def added_identity(self) -> str | None:
        """A content hash of the rescales L2 added (curated, implied); ``None`` if there are none.

        The run specification carries it beside :meth:`counts` for the reason
        :meth:`merger_terms_identity` exists: a corrected curated ratio changes a run's result
        without changing a count.
        """
        rows = sorted(
            f"{a.source.value}|{a.isin}|{a.ex_date}|{a.kind.value}|{a.numerator}|"
            f"{a.denominator}|{a.knowable_date}"
            for a in self._actions
            if isinstance(a, ShareRescale) and a.source is not RescaleSource.FEED
        )
        if not rows:
            return None
        digest = hashlib.sha256("\n".join(rows).encode()).hexdigest()[:16]
        return f"added_rescales[{len(rows)}]:{digest}"

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


def current_signal_split_factors_identity() -> str | None:
    """A stable name for the rescales in force — ``None`` when :func:`signal_split_factors` is off.

    The run specification carries it (``backtest.run_ledger.run_spec``): the factors change what a
    swing signal reads before the seam, so a run made with them and one made without are different
    runs and must never share a digest. Content-addressed — the count and a hash of every rescale
    — so a store whose split rows changed is a different specification too.
    """
    rescales = _SIGNAL.get()
    if rescales is None:
        return None
    canonical = "\n".join(
        sorted(
            f"{r.isin}|{r.ex_date.isoformat()}|{r.kind.value}|{r.numerator}|{r.denominator}|"
            f"{r.carried_from or ''}"
            for r in rescales
        )
    )
    return f"rescales[{len(rescales)}]:{hashlib.sha256(canonical.encode()).hexdigest()[:16]}"


@contextmanager
def corporate_actions_in_force(
    source: BookActionSource | None, *, apply_to_book: bool
) -> Iterator[None]:
    """Put ``source`` in force for the enclosed backtest(s): signal split factors, and the book.

    The one place a driver turns the store's corporate actions on, so the sweep CLI and the fold
    campaign cannot drift apart again (X2: the fold path once put the book in force without the
    signal factors, and the same run digest replayed to two different results). The signal's
    pre-seam factors follow ``source`` whatever ``apply_to_book`` says — the book switch is a
    before/after measurement of the *book*, which holds the signal fixed. ``None`` turns both off.
    """
    with (
        signal_split_factors(source),
        book_corporate_actions(source if apply_to_book else None),
    ):
        yield


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
            elif isinstance(action, IsinReissue):
                self._reissue(action, sim, book)
            elif isinstance(action, ShareSwap):
                self._swap(action, session, sim, book)
            elif isinstance(action, CashExit):
                self._exit(action, session, sim, book)
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

    def _carry(
        self, from_isin: str, isin: str, ex_date: date, sim: SimBroker, book: PortfolioBook
    ) -> None:
        if sim.held_quantity(from_isin) == 0:
            return
        sim.carry_over(from_isin, isin)
        book.apply_merger(from_isin, surviving_isin=isin, shares_received=_ONE, shares_held=_ONE)
        _check_agree(isin, sim, book)
        _check_agree(from_isin, sim, book)
        self.applied["REISSUE"] += 1
        self.log.append(AppliedCarry(from_isin, isin, ex_date))

    def _reissue(self, action: IsinReissue, sim: SimBroker, book: PortfolioBook) -> None:
        if action.explained:
            self._carry(action.from_isin, action.isin, action.ex_date, sim, book)
            return
        if sim.held_quantity(action.from_isin) == 0:
            return
        self.applied["unmodelled:REISSUE"] += 1
        _log.warning(
            "book_actions.reissue_unexplained",
            isin=action.from_isin,
            successor_isin=action.isin,
            ex_date=action.ex_date.isoformat(),
            detail="held across a reissue no split/bonus explains; not carried, book unchanged",
        )

    def _rescale(self, action: ShareRescale, sim: SimBroker, book: PortfolioBook) -> None:
        if action.carried_from is not None:
            self._carry(action.carried_from, action.isin, action.ex_date, sim, book)
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
        self.applied[action.tally_key] += 1
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

    def _swap(self, action: ShareSwap, session: date, sim: SimBroker, book: PortfolioBook) -> None:
        """Rescale the old ISIN by the swap ratio, then carry it 1:1 into the survivor.

        The two steps are the split and the reissue this applier already trusts, so pending lots
        keep their trade dates and T+N, staged orders follow the shares, and both books are checked
        share for share. Rescaling *before* the carry keeps the ratio off any survivor shares the
        book already held. A non-share leg is credited on the *pre-conversion* count — every old
        share is entitled to it, including the fraction a floored conversion forfeits.
        """
        if _entitled(action.isin, action.ex_date, sim) == 0:
            return
        old, new = sim.apply_share_rescale(
            action.isin,
            numerator=action.numerator,
            denominator=action.denominator,
            ex_date=action.ex_date,
        )
        sim.carry_over(action.isin, action.surviving_isin)
        converted = book.apply_merger(
            action.isin,
            surviving_isin=action.surviving_isin,
            shares_received=action.numerator,
            shares_held=action.denominator,
            forfeit_fraction=True,
        )
        if converted != new:
            raise BookError(
                f"share swap {action.isin} -> {action.surviving_isin}: SimBroker converted {new}, "
                f"PortfolioBook {converted}"
            )
        _check_agree(action.isin, sim, book)
        _check_agree(action.surviving_isin, sim, book)
        self.applied["MERGER:share_swap"] += 1
        if action.cash_per_share > _ZERO:
            amount = action.cash_per_share * old
            book.credit_scheme_cash(session, action.isin, amount)
            sim.credit_corporate_cash(
                session,
                action.isin,
                amount,
                f"SCHEME CASH {old} x {action.cash_per_share} ({action.isin} -> "
                f"{action.surviving_isin})",
            )
            self.applied["MERGER:scheme_cash"] += 1
            self.log.append(
                AppliedSchemeCash(
                    isin=action.isin,
                    surviving_isin=action.surviving_isin,
                    ex_date=action.ex_date,
                    quantity=old,
                    per_share=action.cash_per_share,
                    amount=amount,
                )
            )
        self.log.append(
            AppliedMerger(
                from_isin=action.isin,
                isin=action.surviving_isin,
                ex_date=action.ex_date,
                numerator=action.numerator,
                denominator=action.denominator,
                old_quantity=old,
                new_quantity=new,
            )
        )
        _log.info(
            "book_actions.share_swap",
            isin=action.isin,
            surviving_isin=action.surviving_isin,
            ex_date=action.ex_date.isoformat(),
            record_date=action.record_date.isoformat(),
            old_quantity=old,
            new_quantity=new,
        )

    def _exit(self, action: CashExit, session: date, sim: SimBroker, book: PortfolioBook) -> None:
        quantity = _entitled(action.isin, action.ex_date, sim)
        if quantity == 0:
            return
        surrendered = sim.surrender(action.isin, ex_date=action.ex_date)
        amount = book.apply_cash_exit(session, action.isin, price=action.price)
        if surrendered != quantity:
            raise BookError(f"cash exit {action.isin}: surrendered {surrendered} of {quantity}")
        sim.credit_corporate_cash(
            session, action.isin, amount, f"CASH EXIT {quantity} x {action.price}"
        )
        _check_agree(action.isin, sim, book)
        self.applied["MERGER:cash_exit"] += 1
        self.log.append(AppliedCashExit(action.isin, session, quantity, action.price, amount))

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


def load_book_actions(
    conn: Connection,
    *,
    merger_terms: MergerTerms | None = None,
    first_priced: _FirstPriced | None = None,
    composed: Iterable[ComposedEvents] = (),
) -> BookActionCalendar:
    """Every reconciled corporate action, in book terms, keyed by ``ex_date`` (never knowable_date).

    Reads through the corporate-action module's one door for trusted rows,
    ``dataplatform.corpactions.load_reconciled_actions`` (reconciled only, one row per event), and
    the ISIN lineage through ``dataplatform.identity.LineageStore``. See the module docstring for
    why ``knowable_date`` is deliberately not consulted here, and why that is confined to the book.

    ``merger_terms`` (default: the curated file) adds the sourced share swaps and cash exits and
    replaces the store's unquantified merger rows for the names it covers. ``first_priced(isin,
    on)`` — the first session on or after ``on`` with a close for ``isin`` — defers a swap whose
    survivor had not yet listed on its record date; ``None`` applies every swap on its record date.

    ``composed`` is what L2 composed on top of those rows (``store.l2.compose_lake_events``): the
    curated and price-implied events it adjusts prices by, which :func:`added_book_events` checks
    against the feed rows and turns into the same share-count changes. Empty: feed rows only.
    """
    from dataplatform.corpactions import load_merger_terms, load_reconciled_actions
    from dataplatform.identity import LineageStore

    recorded = load_reconciled_actions(conn)
    actions = (*recorded, *added_book_events(recorded, composed))
    resolver = LineageStore(conn).load()
    terms = load_merger_terms() if merger_terms is None else merger_terms
    curated = merger_term_actions(
        terms, resolver.chain_to, resolver.effective_date, first_priced=first_priced
    )
    calendar = BookActionCalendar(
        [
            *_to_book_actions(
                actions,
                resolver.chain_to,
                resolver.effective_date,
                reissues=resolver.edges(),
                covered=_covered(terms),
            ),
            *curated,
        ]
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
    *,
    reissues: Iterable[tuple[str, str, date]] = (),
    covered: frozenset[str] = frozenset(),
) -> list[BookAction]:
    """Translate store rows into book actions, resolving each onto the ISIN held on its ex-date.

    ``reissues`` are the lineage's one-hop edges ``(predecessor, successor, effective_date)``; each
    becomes an :class:`IsinReissue` on its effective date, explained when a SPLIT/BONUS resolved
    onto either end of the edge is ex within :data:`_REISSUE_WINDOW_DAYS` on or before it.
    ``covered`` names the ISINs the curated merger terms convert (:func:`merger_term_actions`):
    a store MERGER row on one of them is dropped in favour of the sourced term.
    """
    from dataplatform.corpactions import DividendTerms

    out: list[BookAction] = []
    skipped: Counter[str] = Counter()
    mergers = 0
    for row in rows:
        action_type = str(getattr(row.action_type, "value", row.action_type))
        chain = chain_to(row.isin)
        live = _live_isin(row.isin, row.ex_date, chain, effective_date)
        terms = row.terms
        ratio = _rescale_ratio(row)
        if ratio is not None:
            source = _rescale_source(row)
            out.append(
                ShareRescale(
                    isin=live,
                    ex_date=row.ex_date,
                    kind=RescaleKind(action_type),
                    numerator=ratio[0],
                    denominator=ratio[1],
                    source=source,
                    knowable_date=None
                    if source is RescaleSource.FEED
                    else getattr(row, "knowable_date", None),
                )
            )
        elif action_type == "DIVIDEND":
            if isinstance(terms, DividendTerms) and terms.amount_inr is not None:
                out.append(CashDividend(isin=live, ex_date=row.ex_date, per_share=terms.amount_inr))
            else:
                skipped["DIVIDEND:no_rupee_amount"] += 1
        elif action_type in ("MERGER", "DEMERGER", "SCHEME_OF_ARRANGEMENT", "RIGHTS"):
            if action_type == "MERGER" and (live in covered or row.isin in covered):
                skipped["MERGER:curated_terms"] += 1  # the sourced term converts the holding
                continue
            if action_type == "MERGER":
                # No stored merger names its surviving ISIN (and the lineage is not a merger map),
                # so even a stated ratio has nowhere to go: named here, never guessed.
                mergers += 1
                _log.warning(
                    "book_actions.merger_skipped",
                    isin=live,
                    ex_date=row.ex_date.isoformat(),
                    terms=type(terms).__name__,
                    detail="no surviving ISIN in the store; the book leaves the holding as it is",
                )
            out.append(UnmodelledAction(isin=live, ex_date=row.ex_date, action_type=action_type))
        else:
            skipped[action_type] += 1  # BUYBACK, DELISTING, NAME_CHANGE: no share/cash effect here
    rescaled = [(a.isin, a.ex_date) for a in out if isinstance(a, ShareRescale)]
    for predecessor, successor, effective in reissues:
        explained = any(
            isin in (predecessor, successor)
            and 0 <= (effective - ex_date).days <= _REISSUE_WINDOW_DAYS
            for isin, ex_date in rescaled
        )
        out.append(IsinReissue(successor, effective, predecessor, explained))
    if mergers:
        skipped["MERGER:no_surviving_isin"] = mergers
    if skipped:
        _log.info("book_actions.skipped", **dict(sorted(skipped.items())))
    return out


def _rescale_ratio(row: _ActionRow) -> tuple[Decimal, Decimal] | None:
    """A SPLIT's or BONUS's share multiple as ``(numerator, denominator)``; ``None`` for the rest.

    SPLIT ``from → to`` face value: shares x ``from / to`` (₹10 → ₹2 is x5). BONUS ``new:held``:
    shares x ``(new + held) / held`` (1:2 is x3/2). The reciprocal of the price factor
    ``dataplatform.corpactions.factors`` derives from the same terms, which
    :func:`added_book_events` re-checks against L2's chain wherever L2 added an event.
    """
    from dataplatform.corpactions import FaceValueTerms, RatioTerms

    action_type = str(getattr(row.action_type, "value", row.action_type))
    terms = row.terms
    if action_type == "SPLIT" and isinstance(terms, FaceValueTerms):
        return terms.from_value, terms.to_value
    if action_type == "BONUS" and isinstance(terms, RatioTerms):
        return terms.new_shares + terms.held_shares, terms.held_shares
    return None


def _rescale_source(row: _ActionRow) -> RescaleSource:
    from dataplatform.corpactions import MANUAL_SOURCE
    from dataplatform.corpactions.implied import IMPLIED_SOURCE

    source = getattr(row, "source", None)
    if source == MANUAL_SOURCE:
        return RescaleSource.CURATED
    if source == IMPLIED_SOURCE:
        return RescaleSource.IMPLIED
    return RescaleSource.FEED


#: How far apart two Decimal products of the same ratios may land and still be one factor: the
#: persisted chain's factors come back from a Postgres NUMERIC, a non-terminating ratio (a 1:3
#: bonus's 4/3) rounded at its scale rather than Python's.
_FACTOR_TOLERANCE = Decimal("1e-12")


def _same_factor(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= _FACTOR_TOLERANCE * max(_ONE, abs(b))


def added_book_events(
    recorded: Iterable[_ActionRow], composed: Iterable[ComposedEvents]
) -> tuple[CorporateAction, ...]:
    """The events L2 added to the feed rows (curated, implied) that the book must apply too.

    What it does: for each ISIN and each ex-date ``composed`` added a SPLIT/BONUS on, compares the
    share multiple the book would apply — the product of the feed rows' ratios and the added ones'
    — with the quantity factor of L2's composed chain on that date. Equal: the added events are
    returned. Not equal, but the feed rows alone equal L2's factor: the persisted chain lagged the
    feed, L2 implied the feed's own split back from L1, and the added events are dropped so the
    one split is applied once (``book_actions.added_event_superseded``). Anything else raises
    ``BookError``: the book would hold a count its prices contradict. A curated structural break
    (DEMERGER, scheme) is returned as it is; it scales nothing and the book counts it when held.

    What it assumes: ``recorded`` are the rows the book reads from the store
    (``load_reconciled_actions``), keyed by the same ISIN as ``composed`` (the store's, the L2
    partition's). What it never does: read a price, invent a ratio, or change a feed row.
    """
    feed: dict[tuple[str, date], Decimal] = {}
    for row in recorded:
        ratio = _rescale_ratio(row)
        if ratio is not None:
            key = (row.isin, row.ex_date)
            feed[key] = feed.get(key, _ONE) * ratio[0] / ratio[1]
    out: list[CorporateAction] = []
    for events in composed:
        l2_qty = {r.ex_date: r.qty_factor for r in events.chain.rows}
        by_date: dict[date, list[CorporateAction]] = {}
        for action in events.added:
            by_date.setdefault(action.ex_date, []).append(action)
        for ex_date, added in sorted(by_date.items()):
            scaling: list[CorporateAction] = []
            for action in added:
                if action.action_type.value in ("SPLIT", "BONUS"):
                    if _rescale_ratio(action) is None:
                        raise BookError(
                            f"{events.isin} {action.action_type.value} on {ex_date.isoformat()} "
                            f"from {action.source} has no quantified terms"
                        )
                    scaling.append(action)
                else:
                    out.append(action)  # a structural break: no share multiple
            if not scaling:
                continue
            from_feed = feed.get((events.isin, ex_date), _ONE)
            added_qty = _ONE
            for action in scaling:
                numerator, denominator = _rescale_ratio(action) or (_ONE, _ONE)
                added_qty = added_qty * numerator / denominator
            in_l2 = l2_qty.get(ex_date, _ONE)
            if _same_factor(from_feed * added_qty, in_l2):
                out.extend(scaling)
            elif from_feed != _ONE and _same_factor(from_feed, in_l2):
                _log.warning(
                    "book_actions.added_event_superseded",
                    isin=events.isin,
                    ex_date=ex_date.isoformat(),
                    sources=sorted({a.source for a in scaling}),
                    feed_multiple=str(from_feed),
                    l2_multiple=str(in_l2),
                    detail="the feed row already carries the split L2 composed; applied once",
                )
            else:
                raise BookError(
                    f"{events.isin} on {ex_date.isoformat()}: the book would multiply shares by "
                    f"{from_feed * added_qty} (feed x{from_feed}, added x{added_qty}) but L2 "
                    f"adjusts prices by x{in_l2}; recompute adjustment_factors before backtesting"
                )
    return tuple(out)


class _ChainTo(Protocol):
    def __call__(self, isin: str, /) -> tuple[str, ...]: ...


class _FirstPriced(Protocol):
    def __call__(self, isin: str, on: date, /) -> date | None: ...


def _covered(terms: MergerTerms) -> frozenset[str]:
    return frozenset(
        [t.old_isin for t in terms.share_swaps] + [t.old_isin for t in terms.cash_exits]
    )


def merger_term_actions(
    terms: MergerTerms,
    chain_to: _ChainTo,
    effective_date: _EffectiveDate,
    *,
    first_priced: _FirstPriced | None = None,
) -> list[BookAction]:
    """The curated merger terms as book actions: share swaps, cash exits, unsourced notices.

    A swap's survivor is resolved to the member of its lineage chain live on the day the swap
    applies — the record date, or the survivor's first priced session after it when
    ``first_priced`` says the survivor had not yet listed. When the chain member live on the record
    date never prints again, the named survivor's own first print is used instead. A survivor
    ``first_priced`` never sees again is not applied (``book_actions.merger_no_survivor_price``):
    converting into a name with no close would stall every NAV sample. An unsourced scheme with a
    record date becomes an :class:`UnmodelledAction` (``MERGER:unsourced``), so a holding in it is
    counted, not guessed.
    """
    out: list[BookAction] = []
    for swap in terms.share_swaps:
        applies = swap.record_date
        if first_priced is not None:
            survivor_on_record = _live_isin(
                swap.surviving_isin,
                swap.record_date,
                chain_to(swap.surviving_isin),
                effective_date,
            )
            priced = first_priced(survivor_on_record, swap.record_date)
            if priced is None and survivor_on_record != swap.surviving_isin:
                # The lineage named a predecessor live on the record date that never prints again
                # (Piramal Finance: a derived edge from the pre-listing ISIN). The scheme's shares
                # are the named survivor's, so its own first print is where the holding lands.
                priced = first_priced(swap.surviving_isin, swap.record_date)
            if priced is None:
                _log.warning(
                    "book_actions.merger_no_survivor_price",
                    isin=swap.old_isin,
                    surviving_isin=survivor_on_record,
                    record_date=swap.record_date.isoformat(),
                    detail="the survivor never prints after the record date; swap not applied",
                )
                continue
            applies = max(applies, priced)
        survivor = _live_isin(
            swap.surviving_isin, applies, chain_to(swap.surviving_isin), effective_date
        )
        out.append(
            ShareSwap(
                isin=swap.old_isin,
                ex_date=applies,
                surviving_isin=survivor,
                numerator=swap.shares_received,
                denominator=swap.shares_held,
                record_date=swap.record_date,
                knowable_date=swap.knowable_date,
                cash_per_share=swap.cash_per_share_held,
            )
        )
    for cash in terms.cash_exits:
        out.append(
            CashExit(
                isin=cash.old_isin,
                ex_date=cash.effective_date,
                price=cash.exit_price,
                knowable_date=cash.knowable_date,
            )
        )
    for unsourced in terms.unsourced:
        if unsourced.record_date is not None:
            out.append(
                UnmodelledAction(unsourced.old_isin, unsourced.record_date, "MERGER:unsourced")
            )
    return out


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


def load_store_book_actions() -> BookActionCalendar:
    """:func:`load_book_actions` over the configured Postgres (``dataplatform.store.db``).

    A swap's survivor is checked for a close against the configured lake's L1 listing windows, and
    the curated and implied events come from the composition the configured lake's L2 is built
    with (:func:`_l2_composed_events`).
    """
    from dataplatform.store.db import connect

    with connect() as conn:
        return load_book_actions(
            conn, first_priced=_l1_first_priced(), composed=_l2_composed_events(conn)
        )


def _l2_composed_events(conn: Connection) -> tuple[ComposedEvents, ...]:
    """``store.l2.compose_lake_events`` over the configured lake, with the D2 lineage chains."""
    from dataplatform.config import get_settings
    from dataplatform.identity import LineageStore
    from dataplatform.store.l2 import compose_lake_events

    resolver = LineageStore(conn).load()
    history = {
        isin: tuple(chain)
        for isin in resolver.survivors()
        if len(chain := resolver.chain_to(isin)) > 1
    }
    return compose_lake_events(
        conn,
        data_root=get_settings().data_root,
        history_for=history,
        survivor_of=resolver.survivor_of,
    )


def _l1_first_priced() -> _FirstPriced:
    """``first_priced`` over L1: the survivor's first print if it lists later, else the date."""
    from backtest.run import _L1Reader  # deferred: backtest.run imports this module

    reader = _L1Reader()
    try:
        windows = {w.isin: w for w in reader.listing_windows()}
    finally:
        reader.close()

    def first_priced(isin: str, on: date) -> date | None:
        window = windows.get(isin)
        if window is None:
            return None
        if window.delisted_on is not None and window.delisted_on <= on:
            return None
        return max(on, window.listed_from)

    return first_priced


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
    with corporate_actions_in_force(load_store_book_actions(), apply_to_book=apply_to_book):
        yield
