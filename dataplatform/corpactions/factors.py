"""D3 (M2.4): the adjustment factor chain and the series derived from it (§4.3).

This module turns the reconciled corporate actions of one ISIN into the multiplicative factors that
make its price history comparable across splits, bonuses and structural breaks, and derives the
three series a consumer actually reads: the price-adjusted close, the daily return, and the
total-return close. It never stores an adjusted price in L1 (invariant #3) — everything here is a
pure function of raw prices and factors, computed on read or materialized into L2 (M2.5).

──────────────────────────────────────────────────────────────────────────────────────────────
THE FACTOR CONVENTION — every downstream consumer depends on this, so it is spelled out in full.
──────────────────────────────────────────────────────────────────────────────────────────────

The adjusted series is **back-adjusted**: prices are expressed in the *current* (most recent) share
basis, and the newest segment — after the last corporate action — is left unscaled at factor 1.0.
Applying a corporate action therefore rewrites *history*, never the present.

For each ex-date event the chain stores two multiplicative factors:

* ``price_factor`` multiplies raw prices on every trading date **strictly before** this ex-date, to
  re-express them in the post-event basis. It is ``< 1`` for a split or a bonus (a pre-event share
  was worth more than a post-event one).
* ``qty_factor`` does the same for raw quantities (shares held, volume). It is the reciprocal of
  ``price_factor`` by construction, so ``price x quantity`` — market cap, turnover — is preserved
  across the event (``price_factor * qty_factor == 1``).

The **cumulative** factor at a date ``d`` is the product of ``price_factor`` over every event whose
ex-date is **after** ``d``. So the oldest prices are scaled by every event, prices in the newest
segment by nothing (factor 1.0), and a price *on* an ex-date is already in post-event terms and is
scaled only by the events that come after it. ``adjusted_close(d) = raw_close(d) x cum_price(d)``.

**What a 1:5 split does to a 2019 close.** A face-value split from ₹10 to ₹2 divides each share
into five, so ``price_factor = to / from = 2 / 10 = 0.2`` and ``qty_factor = from / to = 5``. A raw
2019 close of ₹1000, once a 2021 1:5 split has landed, appears in the price-adjusted series as
``1000 x 0.2 = ₹200`` — directly comparable to the post-split quotes. A holder's 100 shares in 2019
become ``100 x 5 = 500`` shares in current-basis terms.

**Bonus.** A ``new:held`` bonus of ``a:b`` turns ``b`` shares into ``a + b``:
``price_factor = b / (a + b)``, ``qty_factor = (a + b) / b``. A 1:1 bonus gives ``0.5`` and ``2``.

**Composition.** Factors compose by multiplication and the events commute, so a 1:1 bonus stacked
with a later 1:5 split scales a price before both by ``0.5 x 0.2 = 0.1``. This is acceptance 1.

──────────────────────────────────────────────────────────────────────────────────────────────
STRUCTURAL BREAKS — mergers and demergers (§4.3 rule 3).
──────────────────────────────────────────────────────────────────────────────────────────────

A merger, demerger, scheme of arrangement or DVR conversion is **not** a scaling of the ISIN's own
price: on ex-date the parent sheds the value of a business it no longer contains (a demerger) or the
security ceases to exist in its own right (a merger). The close-to-close move across that ex-date is
a *structural event, not a return*. So these events carry ``price_factor == qty_factor == 1`` and
``structural_break = True``; the price-adjusted level series shows the gap (it is a real change in
what the security is), but the **return series bridges it** — the crossing return is ``None``, never
a spurious -40%. Adjusting the parent for the spun-off entity's value would require that entity's
price, which is a different ISIN; we do not guess it (see ``ops/BACKLOG.md``). This is acceptance 3.

──────────────────────────────────────────────────────────────────────────────────────────────
DIVIDENDS — price-adjusted vs total-return (§4.3, acceptance 4).
──────────────────────────────────────────────────────────────────────────────────────────────

A cash dividend is **absent from the price-adjusted series**: it is a distribution, not a change in
the share basis, so it produces no ``adjustment_factors`` row and does not move ``cum_price``. It
appears **only in the total-return series**, which reinvests it: a dividend of ``D`` on an ex-date
whose prior close is ``P`` scales all earlier prices by ``(P - D) / P``, exactly as a price event's
factor does. The two series therefore diverge by the compounded dividend yield and agree in its
absence — which is the documented, tested distinction of acceptance 4.

``P`` is the **last close before the ex-date**, which is usually the previous session. When the
stock did not trade on the ex-date (a session in the trade-to-trade series, which L2's EQ view does
not carry, or a short suspension), the reinvestment base is still that last close, however many
sessions back it is, up to ``MAX_STALE_CLOSE_DAYS`` calendar days (owner decision, 2026-11-01).
Beyond that bound the close is too stale to price the distribution against, so the dividend raises
``FactorError`` rather than being reinvested at it.

Rights issues are not yet in the chain: their factor depends on the theoretical ex-rights price and
so on the market close, and none of the M2 golden cases is a rights issue. Recorded in
``ops/BACKLOG.md`` rather than half-implemented here.

Everything is ``Decimal`` (a float in a factor is a bug — invariant, CLAUDE.md). Nothing here reads
a clock, a database or the network: the retroactive-recompute seam that does live in
``dataplatform.corpactions.recompute``.
"""

from __future__ import annotations

import bisect
import itertools
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, Field

from dataplatform.corpactions.taxonomy import (
    ActionType,
    DividendTerms,
    FaceValueTerms,
    RatioTerms,
    Terms,
)
from dataplatform.ingest.models import ISIN_PATTERN, IngestError
from dataplatform.logging import get_logger

if TYPE_CHECKING:
    # Only used in annotations (stringized by `from __future__ import annotations`), so it stays out
    # of the runtime import graph: `ingest.corp_actions` imports this package's vocabulary
    # submodules, so importing it eagerly here would deepen a pre-existing cycle (ops/BACKLOG.md).
    from dataplatform.ingest.corp_actions import CorporateAction

__all__ = [
    "MAX_STALE_CLOSE_DAYS",
    "PRICE_EVENT_TYPES",
    "STRUCTURAL_BREAK_TYPES",
    "AdjustedPoint",
    "FactorChain",
    "FactorError",
    "FactorRow",
    "PricePoint",
    "ReturnPoint",
    "build_chain_for_isin",
    "build_factor_chain",
    "price_adjusted_series",
    "return_series",
    "total_return_series",
    "with_events",
    "with_price_events",
]

_LOG = get_logger(__name__)

_ONE: Final = Decimal(1)

#: The longest gap, in calendar days, from the last close before a no-trade-day dividend's ex-date
#: to the ex-date that the total-return series will still reinvest against (``total_return_series``;
#: an ex-date that has a bar always reinvests against the previous bar, as before). One quarter:
#: a close older than that predates at least one quarterly results cycle (SEBI LODR reg. 33 gives
#: 45 days after quarter end), so it no longer prices the business the dividend is paid out of.
#: It is a judgment, not a cliff in the data: on the 2026-11-01 lake, 492 no-trade-day dividends in
#: 357 partitions fell within it (median gap 31 days: trade-to-trade stints, short suspensions), and
#: 107 partitions hit it with gaps of 93 days to 12 years: suspended or relisted securities,
#: which must fail loud rather than reinvest at a stale price.
MAX_STALE_CLOSE_DAYS: Final = 92

#: The action types that scale the price/quantity basis and so produce a factor row. Everything the
#: back-adjusted convention above describes as a ratio is one of these; nothing else moves a price.
PRICE_EVENT_TYPES: Final[frozenset[ActionType]] = frozenset({ActionType.SPLIT, ActionType.BONUS})

#: The action types whose ex-date gap is a structural event, not a return (§4.3 rule 3). They carry
#: unit factors and a ``structural_break`` marker so the return series can bridge them.
STRUCTURAL_BREAK_TYPES: Final[frozenset[ActionType]] = frozenset(
    {
        ActionType.MERGER,
        ActionType.DEMERGER,
        ActionType.SCHEME_OF_ARRANGEMENT,
        ActionType.DVR_CONVERSION,
    }
)


class FactorError(IngestError):
    """A corporate action the factor chain cannot turn into a factor — a defect, not a data gap.

    Raised, never swallowed: a SPLIT whose terms were never quantified, or a dividend stated only as
    a percentage of a face value this layer does not hold, cannot yield a factor, and guessing one
    is exactly how an ISIN's adjusted history gets silently rewritten wrong (risk register row 1).
    The action reaches here only because it was reconciled, so an un-factorable one is a real
    inconsistency a human must see, not something to default past.
    """


class PricePoint(BaseModel):
    """One raw close for one trading date — the input to every derived series.

    Deliberately minimal and typed rather than a bare ``(date, Decimal)`` tuple: the series
    functions cross the module boundary and a mislabeled tuple field is precisely the kind of silent
    error the platform's "no bare dicts as interfaces" rule exists to stop. ``close`` is the raw
    traded close from L1 (invariant #3 — never an adjusted one).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    date: date
    close: Decimal = Field(gt=0, description="Raw (unadjusted) close, INR.")


class AdjustedPoint(BaseModel):
    """One date's back-adjusted close, in current share basis (see the module convention)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    date: date
    adj_close: Decimal = Field(gt=0)


class ReturnPoint(BaseModel):
    """One date's return relative to the previous point.

    ``ret`` is ``None`` exactly when the crossing is bridged — the previous point sits on the far
    side of a structural break (a merger/demerger ex-date), where the price gap is not a return
    (§4.3 rule 3). A consumer compounding a return index skips a ``None`` rather than folding a
    spurious -40% into it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    date: date
    ret: Decimal | None
    bridged: bool = False


class FactorRow(BaseModel):
    """One ex-date's factors for one ISIN — a single ``adjustment_factors`` row (§4.3).

    The grain is the **ex-date**, not the action, because that is the table's primary key
    ``(isin, ex_date)`` and §4.3's own wording ("factors computed per ISIN at each ex-date"): two
    actions sharing an ex-date (a split and a bonus on the same day) compose into one row whose
    ``price_factor`` is their product. ``price_factor``/``qty_factor`` are this ex-date's own
    multipliers; ``cum_price_factor``/``cum_qty_factor`` are the product of this ex-date's and every
    *later* ex-date's factors, i.e. the factor that applies to a raw price on the trading day
    immediately before this ex-date. A structural break carries unit factors and
    ``structural_break = True``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    isin: str = Field(pattern=ISIN_PATTERN)
    ex_date: date
    price_factor: Decimal = Field(gt=0)
    qty_factor: Decimal = Field(gt=0)
    cum_price_factor: Decimal = Field(gt=0)
    cum_qty_factor: Decimal = Field(gt=0)
    structural_break: bool = False


class FactorChain(BaseModel):
    """The full, ordered factor chain for one ISIN, and the lookups every series is built on.

    Rows are sorted by ex-date ascending and share the ISIN. The chain is the single authority on
    "what factor applies to a price on date ``d``" and "which ex-dates are structural breaks", so no
    consumer re-derives either from raw actions. Empty (no rows) is a valid chain: an ISIN with no
    price events or breaks has an adjusted series identical to its raw one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    isin: str = Field(pattern=ISIN_PATTERN)
    rows: tuple[FactorRow, ...] = ()

    def price_factor_asof(self, on: date, *, base: Decimal = _ONE) -> Decimal:
        """Cumulative price factor for a raw price on ``on`` — product of events *after* ``on``.

        A price on an ex-date is already in post-event terms, so an event exactly on ``on`` does not
        scale it (only strictly-later events do). Returns ``1`` for the newest segment.

        ``base`` is the running product to fold this chain's factors onto, oldest event first —
        the cumulative factor an older chain already carries for ``on`` when this chain holds only
        the events after it (the L2 overlay, ``dataplatform.store.l2_overlay``). The default ``1``
        is the plain lookup; the multiplication order is the same either way.
        """
        factor = base
        for row in self.rows:
            if row.ex_date > on:
                factor *= row.price_factor
        return factor

    def qty_factor_asof(self, on: date, *, base: Decimal = _ONE) -> Decimal:
        """Cumulative quantity factor for a raw quantity on ``on`` (reciprocal of price)."""
        factor = base
        for row in self.rows:
            if row.ex_date > on:
                factor *= row.qty_factor
        return factor

    def structural_break_dates(self) -> frozenset[date]:
        """The ex-dates whose crossing the return series must bridge (§4.3 rule 3)."""
        return frozenset(row.ex_date for row in self.rows if row.structural_break)


# ── building the chain (pure, terms-only) ──────────────────────────────────────────────────────


@dataclass(slots=True)
class _Accum:
    """A mutable per-ex-date accumulator used only while composing same-day actions into one row."""

    price_factor: Decimal = _ONE
    qty_factor: Decimal = _ONE
    structural_break: bool = False


def _event_factors(action: CorporateAction) -> tuple[Decimal, Decimal]:
    """The ``(price_factor, qty_factor)`` for a SPLIT or BONUS, from its terms alone.

    Both are the exact rational the terms describe, computed independently rather than one as
    ``1 / other`` — a reciprocal through ``Decimal`` division would lose exactness for a
    non-terminating ratio (a 2:1 bonus's ``1/3``). They are reciprocals mathematically, so
    ``price x qty`` is 1 wherever the ratio terminates.

    SPLIT (face value ``from → to``): ``price = to / from`` (a 10→2 split gives 0.2), ``qty =
    from / to`` (5). BONUS (``new:held``): ``price = held / (new + held)`` (a 1:1 bonus gives 0.5),
    ``qty = (new + held) / held`` (2).
    """
    terms: Terms = action.terms
    if action.action_type is ActionType.SPLIT:
        if not isinstance(terms, FaceValueTerms):
            raise FactorError(
                f"{action.isin} SPLIT on {action.ex_date.isoformat()} has no quantified face "
                f"values ({type(terms).__name__}); a reconciled split must state its terms"
            )
        return terms.to_value / terms.from_value, terms.from_value / terms.to_value
    if action.action_type is ActionType.BONUS:
        if not isinstance(terms, RatioTerms):
            raise FactorError(
                f"{action.isin} BONUS on {action.ex_date.isoformat()} has no quantified ratio "
                f"({type(terms).__name__}); a reconciled bonus must state its terms"
            )
        total = terms.new_shares + terms.held_shares
        return terms.held_shares / total, total / terms.held_shares
    raise FactorError(  # pragma: no cover - guarded by the caller's membership test
        f"{action.action_type} is not a price event"
    )


def build_factor_chain(actions: Iterable[CorporateAction]) -> FactorChain:
    """Build one ISIN's factor chain from its reconciled corporate actions.

    What it does: keeps the actions that move the price/quantity basis (``PRICE_EVENT_TYPES``) and
    the ones that are structural breaks (``STRUCTURAL_BREAK_TYPES``), computes each event's factor
    from its terms, sorts by ex-date, and folds in the cumulative product from the newest event
    backward so each row carries the factor that applies to prices just before its ex-date.

    What it assumes: every action is for the same ISIN and is already reconciled (M2.3) — the caller
    reads them through ``load_reconciled_actions``. Dividends, buybacks, name changes and the like
    are ignored here: they create no ``adjustment_factors`` row (a dividend's effect lives in the
    total-return series, not the price basis).

    What it never does: read a clock, a database or a price; pick a winner between disagreeing
    feeds; or invent a factor for an action whose terms were never quantified — that raises
    ``FactorError`` rather than defaulting.
    """
    relevant = [
        a
        for a in actions
        if a.action_type in PRICE_EVENT_TYPES or a.action_type in STRUCTURAL_BREAK_TYPES
    ]
    if not relevant:
        isins = {a.isin for a in actions}
        # An empty chain still needs an ISIN; take it from the input when there is one, else the
        # caller passed nothing factor-relevant for a known ISIN and must name it.
        if len(isins) > 1:
            raise FactorError(f"actions span more than one ISIN: {sorted(isins)}")
        # No relevant and no actions at all: caller should use build_factor_chain([...]) per ISIN.
        raise FactorError(
            "no price events or structural breaks to build a chain from; call per ISIN with its "
            "reconciled actions, and handle the empty case at the call site"
        )

    isins = {a.isin for a in relevant}
    if len(isins) > 1:
        raise FactorError(f"a factor chain is per ISIN, but actions span {sorted(isins)}")
    isin = next(iter(isins))

    # Compose actions sharing an ex-date into one row (the table's grain): factors multiply, a
    # break on the date marks the row. Sorted by ex-date so the cumulative pass runs newest-first.
    per_date: dict[date, _Accum] = {}
    for action in sorted(relevant, key=lambda a: (a.ex_date, a.action_type.value)):
        slot = per_date.setdefault(action.ex_date, _Accum())
        if action.action_type in STRUCTURAL_BREAK_TYPES:
            slot.structural_break = True
        else:
            price_factor, qty_factor = _event_factors(action)
            slot.price_factor *= price_factor
            slot.qty_factor *= qty_factor

    partial = [
        FactorRow(
            isin=isin,
            ex_date=ex_date,
            price_factor=slot.price_factor,
            qty_factor=slot.qty_factor,
            # cumulative filled in the backward pass below.
            cum_price_factor=slot.price_factor,
            cum_qty_factor=slot.qty_factor,
            structural_break=slot.structural_break,
        )
        for ex_date, slot in sorted(per_date.items())
    ]

    rows = _with_cumulative(partial)
    _LOG.info(
        "ca.factor_chain_built",
        isin=isin,
        rows=len(rows),
        structural_breaks=sum(1 for r in rows if r.structural_break),
    )
    return FactorChain(isin=isin, rows=tuple(rows))


def build_chain_for_isin(isin: str, actions: Iterable[CorporateAction]) -> FactorChain:
    """``build_factor_chain`` that tolerates the empty case, given the ISIN explicitly.

    The recompute seam knows the ISIN it is rebuilding and must produce a chain even when the ISIN
    has only dividends (no price events): that is an empty, valid chain, not an error. Kept separate
    from ``build_factor_chain`` so the pure builder can stay strict about being handed nothing.
    """
    relevant = [
        a
        for a in actions
        if a.action_type in PRICE_EVENT_TYPES or a.action_type in STRUCTURAL_BREAK_TYPES
    ]
    if not relevant:
        return FactorChain(isin=isin, rows=())
    stray = {a.isin for a in relevant if a.isin != isin}
    if stray:
        raise FactorError(f"actions for {sorted(stray)} passed to chain for {isin}")
    return build_factor_chain(relevant)


def with_price_events(chain: FactorChain, actions: Iterable[CorporateAction]) -> FactorChain:
    """``chain`` with further SPLIT/BONUS events composed in and its cumulative factors re-folded.

    What it does: multiplies each action's own factor into the row for its ex-date — creating the
    row when the chain has none there, exactly as ``build_factor_chain`` composes same-day actions —
    and recomputes every cumulative factor, since an added event re-scales every earlier row. The
    price-implied splits the L2 materializer reads off L1 reach the chain this way, so the
    arithmetic stays in this module.

    What it assumes: ``actions`` are price events for ``chain.isin``; anything else raises
    ``FactorError``. What it never does: drop or re-derive a row ``chain`` already had.
    """
    return _compose(chain, actions, PRICE_EVENT_TYPES)


def with_events(chain: FactorChain, actions: Iterable[CorporateAction]) -> FactorChain:
    """``with_price_events`` that also accepts structural breaks, marking their ex-date's row.

    The curated actions (``corpactions.manual_actions``) are both kinds: a split or bonus no feed
    carried, which scales history like any other, and a demerger or scheme, which carries a unit
    factor and the ``structural_break`` marker so ``return_series`` bridges it (§4.3 rule 3).
    """
    return _compose(chain, actions, PRICE_EVENT_TYPES | STRUCTURAL_BREAK_TYPES)


def _compose(
    chain: FactorChain, actions: Iterable[CorporateAction], allowed: frozenset[ActionType]
) -> FactorChain:
    extra = list(actions)
    if not extra:
        return chain
    per_date: dict[date, _Accum] = {
        row.ex_date: _Accum(row.price_factor, row.qty_factor, row.structural_break)
        for row in chain.rows
    }
    for action in sorted(extra, key=lambda a: (a.ex_date, a.action_type.value)):
        if action.isin != chain.isin:
            raise FactorError(f"action for {action.isin} passed to the chain for {chain.isin}")
        if action.action_type not in allowed:
            raise FactorError(f"{action.action_type} is not a price event")
        slot = per_date.setdefault(action.ex_date, _Accum())
        if action.action_type in STRUCTURAL_BREAK_TYPES:
            slot.structural_break = True
            continue
        price_factor, qty_factor = _event_factors(action)
        slot.price_factor *= price_factor
        slot.qty_factor *= qty_factor
    partial = [
        FactorRow(
            isin=chain.isin,
            ex_date=ex_date,
            price_factor=slot.price_factor,
            qty_factor=slot.qty_factor,
            cum_price_factor=slot.price_factor,
            cum_qty_factor=slot.qty_factor,
            structural_break=slot.structural_break,
        )
        for ex_date, slot in sorted(per_date.items())
    ]
    return FactorChain(isin=chain.isin, rows=tuple(_with_cumulative(partial)))


def _with_cumulative(partial: Sequence[FactorRow]) -> list[FactorRow]:
    """Fold the cumulative product in from the newest event backward.

    ``cum_*`` on a row is the product of that row's and every later row's own factor — the factor a
    raw price on the trading day just before this ex-date carries. The newest event's cumulative is
    its own factor; each earlier event multiplies the running product.
    """
    running_price = _ONE
    running_qty = _ONE
    out: list[FactorRow] = []
    for row in reversed(partial):
        running_price *= row.price_factor
        running_qty *= row.qty_factor
        out.append(
            row.model_copy(
                update={"cum_price_factor": running_price, "cum_qty_factor": running_qty}
            )
        )
    out.reverse()
    return out


# ── derived series (raw prices + chain) ────────────────────────────────────────────────────────


def _sorted_prices(prices: Iterable[PricePoint]) -> list[PricePoint]:
    ordered = sorted(prices, key=lambda p: p.date)
    seen: set[date] = set()
    for point in ordered:
        if point.date in seen:
            raise FactorError(f"duplicate price for {point.date.isoformat()}")
        seen.add(point.date)
    return ordered


def price_adjusted_series(
    chain: FactorChain, prices: Iterable[PricePoint]
) -> tuple[AdjustedPoint, ...]:
    """Back-adjusted closes: ``raw x cum_price_factor`` per date (splits/bonuses only).

    Dividends are deliberately absent (they are not a change in the share basis — acceptance 4), and
    structural breaks leave the level series showing the gap (it is a real change in the security);
    only the *return* series bridges those. The newest segment is unscaled.
    """
    return tuple(
        AdjustedPoint(date=p.date, adj_close=p.close * chain.price_factor_asof(p.date))
        for p in _sorted_prices(prices)
    )


def return_series(chain: FactorChain, prices: Iterable[PricePoint]) -> tuple[ReturnPoint, ...]:
    """Daily returns of the price-adjusted series, with structural-break crossings bridged.

    A normal day's return is ``adj(d) / adj(d-1) - 1``. When ``d`` is a structural-break ex-date the
    close-to-close move is a structural event, not a return (§4.3 rule 3), so its point carries
    ``ret = None`` and ``bridged = True`` — never the raw gap as a return. The first date has no
    predecessor and is omitted. This is acceptance 3.
    """
    adjusted = price_adjusted_series(chain, prices)
    breaks = chain.structural_break_dates()
    out: list[ReturnPoint] = []
    for prev, cur in itertools.pairwise(adjusted):
        if cur.date in breaks:
            out.append(ReturnPoint(date=cur.date, ret=None, bridged=True))
            continue
        out.append(ReturnPoint(date=cur.date, ret=cur.adj_close / prev.adj_close - _ONE))
    return tuple(out)


def _dividend_amount(action: CorporateAction) -> Decimal:
    """The cash dividend per share in rupees, or a ``FactorError`` when it cannot be known here.

    A percentage-of-face-value dividend needs a face value this layer does not hold (it lives in the
    identity master, D2), so converting it here would be the kind of cross-layer guess the platform
    forbids. The total-return builder's caller resolves such a dividend to an amount first, or this
    fails loud rather than dropping the distribution.
    """
    if not isinstance(action.terms, DividendTerms):  # pragma: no cover - guarded by caller
        raise FactorError(f"{action.isin} {action.action_type} is not a dividend")
    if action.terms.amount_inr is None:
        raise FactorError(
            f"{action.isin} dividend on {action.ex_date.isoformat()} is stated as a percentage of "
            "face value; resolve it to a rupee amount before building the total-return series"
        )
    return action.terms.amount_inr


def total_return_series(
    actions: Iterable[CorporateAction], prices: Iterable[PricePoint]
) -> tuple[AdjustedPoint, ...]:
    """Total-return closes: the price-adjusted series with cash dividends reinvested (acceptance 4).

    A dividend of ``D`` whose prior close is ``P`` scales every earlier price by ``(P - D) / P`` —
    the same back-adjustment shape a split's factor has — so the return across the ex-date includes
    the distribution. Split/bonus factors apply too; structural breaks do not scale (their gap is
    handled by the return series, not the level). It equals ``price_adjusted_series`` exactly
    when there are no dividends, which is the other half of acceptance 4.

    Takes the raw actions (not a pre-built chain) because a dividend factor needs the pre-ex close,
    which only exists alongside the price series. ``P`` is the last close strictly before the
    ex-date: the previous bar when the ex-date has one. When the stock had no bar on the ex-date (a
    no-trade day), that close is older; it is still the base, as long as it lies within
    ``MAX_STALE_CLOSE_DAYS`` calendar days of the ex-date (owner decision, 2026-11-01). Further
    back, the dividend raises ``FactorError``: a stale close is no reinvestment base, and the
    caller must see that rather than a total-return series priced against it. A dividend after the
    last bar follows the same rule. It scales every bar alike, so no return inside the series
    moves, and the series already holds the factor a later bar on or after the ex-date will give.

    A dividend on or before the first bar scales no bar — its factor applies only to dates before
    its ex-date — so it is skipped and logged, never a reason to give up: the factor chain reaches
    back to 2000 while a partition often starts later, and one such dividend used to make L2 fall
    back to ``tr_close = adj_close`` for the whole partition (backlog M13.8).
    """
    ordered = _sorted_prices(prices)
    if not ordered:
        return ()
    actions = list(actions)
    isins = {a.isin for a in actions}
    if len(isins) > 1:
        raise FactorError(f"total-return series is per ISIN, but actions span {sorted(isins)}")
    if not isins:
        # No actions at all: the total-return series is the raw series (nothing to reinvest or
        # rescale). The ISIN is unknowable here, and does not matter, so return raw closes.
        return tuple(AdjustedPoint(date=p.date, adj_close=p.close) for p in ordered)

    price_chain = build_chain_for_isin(next(iter(isins)), actions)
    dividends = sorted(
        (a for a in actions if a.action_type is ActionType.DIVIDEND),
        key=lambda a: a.ex_date,
    )
    bar_dates = [p.date for p in ordered]
    bar_index = frozenset(bar_dates)
    first_bar = bar_dates[0]
    pre_series = [div for div in dividends if div.ex_date <= first_bar]
    if pre_series:
        _LOG.info(
            "ca.total_return.pre_series_dividends_skipped",
            isin=pre_series[0].isin,
            skipped=len(pre_series),
            earliest_ex_date=pre_series[0].ex_date.isoformat(),
            first_bar=first_bar.isoformat(),
        )

    # Per-date cumulative dividend factor = product over dividends with ex_date > date of (P-D)/P.
    div_factors: list[tuple[date, Decimal]] = []
    no_trade_day: list[tuple[date, int]] = []
    for div in dividends:
        if div.ex_date <= first_bar:
            continue  # scales no bar in the series (see the docstring)
        # The last bar strictly before the ex-date. The skip above means one exists; were it ever
        # missing, index -1 would silently wrap to the newest bar, so it fails loud instead.
        before = bisect.bisect_left(bar_dates, div.ex_date)
        if before == 0:
            raise FactorError(
                f"{div.isin} dividend on {div.ex_date.isoformat()} has no prior close in the price "
                "series; the total-return factor needs the cum-dividend close to reinvest against"
            )
        last = ordered[before - 1]
        if div.ex_date not in bar_index:
            # A no-trade day: reinvest at the last close before it, unless that is too stale. An
            # ex-date that has a bar keeps the previous bar as its base however far back, as it
            # always has; the bound governs only the no-trade-day reinvestment.
            gap_days = (div.ex_date - last.date).days
            if gap_days > MAX_STALE_CLOSE_DAYS:
                raise FactorError(
                    f"{div.isin} dividend on {div.ex_date.isoformat()} falls on a no-trade day "
                    f"with no close within {MAX_STALE_CLOSE_DAYS} days before it; the last is "
                    f"{last.date.isoformat()}, {gap_days} days earlier, too stale to reinvest "
                    "the distribution against"
                )
            no_trade_day.append((div.ex_date, gap_days))
        prior_close = last.close
        amount = _dividend_amount(div)
        if amount >= prior_close:
            raise FactorError(
                f"{div.isin} dividend {amount} on {div.ex_date.isoformat()} is not below the prior "
                f"close {prior_close}; a total-return factor would be non-positive"
            )
        div_factors.append((div.ex_date, (prior_close - amount) / prior_close))
    if no_trade_day:
        _LOG.info(
            "ca.total_return.no_trade_day_dividends",
            isin=dividends[0].isin,
            reinvested=len(no_trade_day),
            ex_dates=[d.isoformat() for d, _ in no_trade_day],
            max_gap_days=max(g for _, g in no_trade_day),
        )

    out: list[AdjustedPoint] = []
    for point in ordered:
        cum = price_chain.price_factor_asof(point.date)
        for ex_date, factor in div_factors:
            if ex_date > point.date:
                cum *= factor
        out.append(AdjustedPoint(date=point.date, adj_close=point.close * cum))
    return tuple(out)
