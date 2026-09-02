"""D4 query shape (c) — screen filters over cross-sections joined to flows/fundamentals (M4.2).

§4.5 names the third canonical shape: *screen-style filters over cross-sections joined to
fundamentals/flows*. This module is that shape. A screen is a composable predicate evaluated over a
`ScreenRow` — one ISIN's cross-section bar (shape (b), M4.1) enriched with the per-ISIN flow and
fundamental metrics joined onto it — and it returns the set of ISINs that pass.

Two design commitments run through it:

* **Filters compose.** `all_of`, `any_of` and `~` build a filter algebra, so "adj_close between 100
  and 500 AND delivery ≥ 40% AND (ROE ≥ 15% OR debt/equity ≤ 0.5)" is one `Filter` object, not a
  hand-rolled loop the caller has to get right each time. Every consumer that screens (A3's case
  discovery, A5, a backtest's candidate set) composes the same primitives, so a screen means the
  same thing everywhere.

* **The fundamentals join surface accepts only PIT-tagged sources — structurally.** Invariant #8:
  restated (Screener) fundamentals are monitoring-only and must be *unreachable* from a
  decision/backtest path. A screen only accepts a `PitFundamentals` view, and a `PitFundamentals`
  can be constructed *only* through `PitFundamentals.from_source`, which refuses any source whose
  `point_in_time` is false (`QuarantineError`). There is no other constructor that carries data, so
  a restated source cannot be turned into something the join will take — the quarantine is a
  property of the types, not a rule a caller must remember. M7 does not exist yet; the boundary
  does, exactly as the task requires ("respect the M7 quarantine boundary even before M7 exists").

Flows join in as plain per-ISIN metric maps (delivery %, deal turnover, FII/DII tilt — whatever the
caller has resolved for the session); they carry no PIT hazard of their own, so they are a mapping,
not a guarded view. A missing metric makes a comparison filter *not match* rather than raise: a
screen over a metric a name lacks excludes that name, which is the conservative reading.

Money is `Decimal` throughout — a `float` threshold in a screen would be a bug (CLAUDE.md). No I/O
here: `Screen.run` takes an already-fetched `CrossSection`; `QueryService.screen` is the wiring that
fetches it first.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol, runtime_checkable

from dataplatform.logging import get_logger
from dataplatform.query.errors import QueryError
from dataplatform.query.shapes import AdjustedPoint, CrossSection

__all__ = [
    "Compare",
    "Filter",
    "FundamentalDatum",
    "FundamentalsSource",
    "Op",
    "PitFundamentals",
    "QuarantineError",
    "ScreenRow",
    "Selector",
    "all_of",
    "any_of",
    "between",
    "eq",
    "fundamental",
    "ge",
    "gt",
    "le",
    "lt",
    "metric",
    "run_screen",
]

_LOG = get_logger(__name__)


# ── the row a filter sees ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ScreenRow:
    """One ISIN's cross-section bar plus the flow/fundamental metrics joined onto it.

    `point` is the shape-(b) adjusted, primary-deduped bar (M4.1). `flows` and `fundamentals` are
    per-ISIN metric maps the screen was given for this session — `flows` straight from resolved flow
    files, `fundamentals` only ever from a PIT-tagged view (see `PitFundamentals`). A metric absent
    from a map reads as "unknown" and makes a comparison over it fail closed.
    """

    isin: str
    point: AdjustedPoint
    flows: Mapping[str, Decimal] = field(default_factory=dict)
    fundamentals: Mapping[str, Decimal] = field(default_factory=dict)


#: A selector pulls one comparable number out of a row, or `None` when the row does not carry it.
Selector = Callable[[ScreenRow], Decimal | None]


# ── selectors: name the column a filter compares ───────────────────────────────────────────────

#: The adjusted-bar fields a screen may compare on, mapped to their `AdjustedPoint` attribute.
_POINT_FIELDS: Mapping[str, Callable[[AdjustedPoint], Decimal]] = {
    "adj_open": lambda p: p.adj_open,
    "adj_high": lambda p: p.adj_high,
    "adj_low": lambda p: p.adj_low,
    "adj_close": lambda p: p.adj_close,
    "adj_volume": lambda p: p.adj_volume,
    "tr_close": lambda p: p.tr_close,
}


def metric(name: str) -> Selector:
    """Select an adjusted-bar field (e.g. ``adj_close``) or a joined flow metric by name.

    Adjusted-bar fields (`_POINT_FIELDS`) resolve off the row's `point`; any other name is looked up
    in the row's `flows` map (delivery %, deal turnover, …). A name in neither yields `None`, so a
    comparison over an unknown metric fails closed rather than raising — a screen for "delivery ≥
    40%" simply does not match a name whose delivery was not resolved.
    """
    point_field = _POINT_FIELDS.get(name)
    if point_field is not None:
        return lambda row: point_field(row.point)
    return lambda row: row.flows.get(name)


def fundamental(name: str) -> Selector:
    """Select a fundamental metric from the row's PIT fundamentals map (`None` if absent).

    Kept distinct from `metric` on purpose: fundamentals only ever reach a row through a
    `PitFundamentals` view (invariant #8), so selecting one names the guarded namespace explicitly
    rather than sharing the flow namespace.
    """
    return lambda row: row.fundamentals.get(name)


# ── the filter algebra ─────────────────────────────────────────────────────────────────────────


class Op(ABC):
    """A comparison operator over two decimals — the leaf of the compare filter."""

    symbol: str

    @abstractmethod
    def compare(self, value: Decimal, threshold: Decimal) -> bool: ...


class _Lt(Op):
    symbol = "<"

    def compare(self, value: Decimal, threshold: Decimal) -> bool:
        return value < threshold


class _Le(Op):
    symbol = "<="

    def compare(self, value: Decimal, threshold: Decimal) -> bool:
        return value <= threshold


class _Gt(Op):
    symbol = ">"

    def compare(self, value: Decimal, threshold: Decimal) -> bool:
        return value > threshold


class _Ge(Op):
    symbol = ">="

    def compare(self, value: Decimal, threshold: Decimal) -> bool:
        return value >= threshold


class _Eq(Op):
    symbol = "=="

    def compare(self, value: Decimal, threshold: Decimal) -> bool:
        return value == threshold


class Filter(ABC):
    """A composable screen predicate over a `ScreenRow`.

    `matches` is the whole contract; `&`, `|` and `~` build the composites so a caller writes
    ``gt(metric("adj_close"), 100) & ge(fundamental("roe"), Decimal("0.15"))`` and gets one filter.
    Filters are pure and stateless — the same row always screens the same way, which is what lets a
    replay reproduce a screen byte-for-byte.
    """

    @abstractmethod
    def matches(self, row: ScreenRow) -> bool: ...

    def __and__(self, other: Filter) -> Filter:
        return _All((self, other))

    def __or__(self, other: Filter) -> Filter:
        return _Any((self, other))

    def __invert__(self) -> Filter:
        return _Not(self)


@dataclass(frozen=True, slots=True)
class Compare(Filter):
    """One comparison: `selector(row) <op> threshold`. Fails closed when the selector yields `None`.

    A row that does not carry the selected metric never matches — the conservative reading of a
    screen for "delivery ≥ 40%" over a name whose delivery is unknown is to exclude it, not guess.
    """

    selector: Selector
    op: Op
    threshold: Decimal

    def matches(self, row: ScreenRow) -> bool:
        value = self.selector(row)
        if value is None:
            return False
        return self.op.compare(value, self.threshold)


@dataclass(frozen=True, slots=True)
class _All(Filter):
    """Conjunction — matches when every child matches. Empty conjunction matches everything."""

    filters: tuple[Filter, ...]

    def matches(self, row: ScreenRow) -> bool:
        return all(child.matches(row) for child in self.filters)


@dataclass(frozen=True, slots=True)
class _Any(Filter):
    """Disjunction — matches when any child matches. Empty disjunction matches nothing."""

    filters: tuple[Filter, ...]

    def matches(self, row: ScreenRow) -> bool:
        return any(child.matches(row) for child in self.filters)


@dataclass(frozen=True, slots=True)
class _Not(Filter):
    """Negation of a child filter."""

    inner: Filter

    def matches(self, row: ScreenRow) -> bool:
        return not self.inner.matches(row)


def lt(selector: Selector, threshold: Decimal) -> Filter:
    """`selector < threshold`."""
    return Compare(selector, _Lt(), threshold)


def le(selector: Selector, threshold: Decimal) -> Filter:
    """`selector <= threshold`."""
    return Compare(selector, _Le(), threshold)


def gt(selector: Selector, threshold: Decimal) -> Filter:
    """`selector > threshold`."""
    return Compare(selector, _Gt(), threshold)


def ge(selector: Selector, threshold: Decimal) -> Filter:
    """`selector >= threshold`."""
    return Compare(selector, _Ge(), threshold)


def eq(selector: Selector, threshold: Decimal) -> Filter:
    """`selector == threshold`."""
    return Compare(selector, _Eq(), threshold)


def between(selector: Selector, low: Decimal, high: Decimal) -> Filter:
    """`low <= selector <= high` — the inclusive band, as one composed filter."""
    if low > high:
        raise ValueError(f"empty band: low {low} > high {high}")
    return ge(selector, low) & le(selector, high)


def all_of(*filters: Filter) -> Filter:
    """Conjunction of every filter (matches everything when none are given)."""
    return _All(tuple(filters))


def any_of(*filters: Filter) -> Filter:
    """Disjunction of every filter (matches nothing when none are given)."""
    return _Any(tuple(filters))


# ── the fundamentals join surface (invariant #8: PIT-tagged only) ──────────────────────────────


class QuarantineError(QueryError):
    """A restated (non-PIT) fundamentals source was offered to the screen join surface.

    Invariant #8 / §7: restated Screener data is monitoring-only and physically unreachable from a
    decision or backtest. Raised — not silently dropped — because a caller that reached here has a
    bug (it tried to screen on quarantined data) and must be told, not left to wonder why the source
    "did nothing". Subclasses `QueryError`: it is the query layer refusing the request.
    """


@dataclass(frozen=True, slots=True)
class FundamentalDatum:
    """One fundamental metric for one ISIN, tagged with the date it was first knowable.

    `filing_date` is the point-in-time tag: a true PIT fundamental (an NSE/BSE XBRL filing, M7) has
    exactly one — the filing timestamp is the first date the figure could have been used. A restated
    Screener figure has no single filing date, which is precisely why it cannot be represented here
    as PIT and is quarantined. Money is `Decimal`.
    """

    isin: str
    metric: str
    value: Decimal
    filing_date: date


@runtime_checkable
class FundamentalsSource(Protocol):
    """Any fundamentals provider — the quarantine discriminator is `point_in_time`.

    A PIT source (M7's XBRL filings store) sets `point_in_time` true and every datum carries its
    filing date. A restated source (the Screener export store) sets it false. The screen never takes
    a `FundamentalsSource` directly; it takes a `PitFundamentals`, which can only be built from a
    source whose `point_in_time` is true — so the discriminator is enforced at construction, not
    trusted at the call site.
    """

    @property
    def point_in_time(self) -> bool:
        """True only for a genuine point-in-time source (filing-date-tagged, §4.1)."""
        ...

    def data(self) -> Iterable[FundamentalDatum]:
        """Every fundamental datum this source offers."""
        ...


@dataclass(frozen=True, slots=True)
class PitFundamentals:
    """The only fundamentals view the screen join accepts — constructible solely from a PIT source.

    What it does: hold per-ISIN, per-metric values that are safe to screen on because they came from
    a point-in-time source and (optionally) were filtered to `filing_date <= as_of`.
    What it assumes: it was built by `from_source`. The `_by_isin` field is private and carries no
    public constructor of its own that a caller would reach for — a restated source cannot produce
    one, because `from_source` refuses it before any datum is copied in.
    What it never does: accept restated data. That refusal is the structural half of invariant #8.

    The `as_of` filter here is a within-join point-in-time guard (a filing not yet public on the
    decision date must not screen). M4.3 generalises this into the query-wide leak guard over every
    dataset's `knowable_date`; this narrower filter keeps the fundamentals join correct on its own
    in the meantime, and the two agree (a filing_date *is* a knowable_date).
    """

    _by_isin: Mapping[str, Mapping[str, Decimal]]

    @classmethod
    def from_source(
        cls, source: FundamentalsSource, *, as_of: date | None = None
    ) -> PitFundamentals:
        """Build a screenable view from a PIT source, refusing a restated one (`QuarantineError`).

        Raises `QuarantineError` when `source.point_in_time` is false — the structural quarantine of
        invariant #8: there is no path from a restated source to a `PitFundamentals`. When `as_of`
        is given, a datum whose `filing_date` is after it is dropped (it was not yet knowable — no
        look-ahead, invariant #7). Later data for the same `(isin, metric)` wins, so a caller that
        feeds an unfiltered history and an `as_of` gets the latest filing on or before the date.
        """
        if not source.point_in_time:
            raise QuarantineError(
                "refusing a restated (non-PIT) fundamentals source at the screen join surface: "
                "restated Screener data is monitoring-only and unreachable from a decision or "
                "backtest (invariant #8, §7). Only a point-in-time (filing-date-tagged) source is "
                "joinable — build the M7 PIT store, or use this data for monitoring, not screening."
            )
        by_isin: dict[str, dict[str, Decimal]] = {}
        for datum in source.data():
            if as_of is not None and datum.filing_date > as_of:
                continue
            by_isin.setdefault(datum.isin, {})[datum.metric] = datum.value
        return cls(_by_isin={isin: dict(metrics) for isin, metrics in by_isin.items()})

    def metrics_for(self, isin: str) -> Mapping[str, Decimal]:
        """The joinable metrics for one ISIN — an empty map when it has none."""
        return self._by_isin.get(isin, {})


# ── running a screen ───────────────────────────────────────────────────────────────────────────


def run_screen(
    cross_section: CrossSection,
    screen: Filter,
    *,
    flows: Mapping[str, Mapping[str, Decimal]] | None = None,
    fundamentals: PitFundamentals | None = None,
    universe: frozenset[str] | None = None,
) -> frozenset[str]:
    """Apply `screen` to a cross-section joined to flows/fundamentals; return the matching ISINs.

    What it does: for each ISIN in the cross-section (optionally first narrowed to `universe` — e.g.
    a PIT universe from shape (d)), builds a `ScreenRow` from its adjusted bar plus its joined flow
    metrics (`flows[isin]`) and PIT fundamentals (`fundamentals.metrics_for(isin)`), evaluates the
    composed filter, and keeps the ISINs that pass.
    What it assumes: `cross_section` was already fetched (this is pure, no I/O); `fundamentals`, if
    given, is a `PitFundamentals` — the type system does not let a restated source in here.
    What it never does: take a bare fundamentals source (only the guarded view) or a symbol (the key
    is the ISIN throughout, invariant #2).

    Filters compose, so this is the one place a screen is *run*: the composition happens in the
    `Filter` the caller passes, and the join + scoping happens here, once.
    """
    flow_maps = flows or {}
    matched: set[str] = set()
    for point in cross_section.rows:
        if universe is not None and point.isin not in universe:
            continue
        row = ScreenRow(
            isin=point.isin,
            point=point,
            flows=flow_maps.get(point.isin, {}),
            fundamentals=(fundamentals.metrics_for(point.isin) if fundamentals is not None else {}),
        )
        if screen.matches(row):
            matched.add(point.isin)
    result = frozenset(matched)
    _LOG.info(
        "query.screen",
        trade_date=cross_section.trade_date.isoformat(),
        candidates=len(cross_section.rows),
        scoped=universe is not None,
        matched=len(result),
    )
    return result
