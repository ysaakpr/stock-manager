"""M19.4 · The review context: the owner's snapshot joined to the lake, by ISIN.

Per holding (keyed by the ISIN Kite carries, invariant #2): the latest lake close and its session;
1m/3m/6m/1y returns on the split/bonus-adjusted series; distance to the 50- and 200-session moving
averages and to the 52-week high and low; the 20-session median traded value; the thinly-traded
label (M13.8); the NSE industry; index membership and a size bucket from it; the latest PIT
fundamentals with their filing date; corporate actions announced and not yet ex.

Portfolio level: value and weight per holding (lake close x held quantity, Kite's own mark beside
it), unrealised P&L on Kite's average cost, cash and the liquid-ETF share, concentration (top-5
weight and HHI), sector, size-bucket and ETF/direct-equity weights, and every holding the lake
cannot price, named.

The lake reads are the ones M17's Research Commons already makes (`analyst.commons.sources.
LakeCommonsSource`: sessions, adjusted bars with the L2-lag overlay, the industry snapshot, PIT
filings, corporate-action broadcasts) plus the query layer's `index_membership_asof`,
`thin_trading_asof` and `compute_metrics`. They are called, never changed; nothing under
`analyst.fundmanager` is imported, and nothing M17 reads is touched.

A holding the lake cannot identify or price (an SME scrip, an unlisted bond, a name with no bar in
the window) is listed with Kite's own numbers and ``lake_context = unavailable(<reason>)`` — never
dropped. A lake piece that fails (Postgres down, an L0 snapshot missing) degrades only that piece,
and the review says which.

What it never does: write anything, read a wall clock (the session is the clock's today, injected),
join on a symbol, or log a symbol, quantity or value.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

import structlog

from analyst.owner_portfolio.snapshot import OwnerPortfolio
from dataplatform.query.fundamentals_metrics import FundamentalMetrics, MetricValue, Unavailable
from execution.kite_read import KiteHolding

if TYPE_CHECKING:  # pragma: no cover
    from analyst.commons.inputs import CorporateActionNotice, PriceBar
    from dataplatform.quality.thin_trading import ThinTradingLabel

__all__ = [
    "INDEX_SLUGS",
    "SESSIONS_NEEDED",
    "CorporateActionDue",
    "FundamentalsView",
    "HoldingReview",
    "InstrumentKind",
    "LakeContext",
    "LakeSource",
    "PortfolioReview",
    "SizeBucket",
    "ThinTradingView",
    "build_review",
]

_LOG = structlog.get_logger(__name__)

#: Return horizons in sessions: 1m, 3m, 6m, 1y.
RETURN_SESSIONS: Final[Mapping[str, int]] = {"1m": 21, "3m": 63, "6m": 126, "1y": 252}
#: The longest look-back any field needs (1y return needs the close 252 sessions before).
SESSIONS_NEEDED: Final[int] = 253
_MEDIAN_TV_SESSIONS: Final[int] = 20
#: Index membership read for each holding; the size bucket comes from the first three.
INDEX_SLUGS: Final[tuple[str, ...]] = (
    "nifty50",
    "nifty100",
    "niftymidcap150",
    "niftysmallcap250",
    "nifty500",
)
#: Corporate actions announced this far back are scanned for an ex date still ahead.
_CA_LOOKBACK_DAYS: Final[int] = 120
_RATIO_Q: Final[Decimal] = Decimal("0.0001")
_MONEY_Q: Final[Decimal] = Decimal("0.01")
_ZERO: Final[Decimal] = Decimal("0")
_ONE: Final[Decimal] = Decimal("1")


class SizeBucket(StrEnum):
    """From PIT index membership: NIFTY 100 → LARGE, Midcap 150 → MID, Smallcap 250 → SMALL."""

    LARGE = "LARGE"
    MID = "MID"
    SMALL = "SMALL"
    MICRO = "MICRO"  # in the lake, in none of the three
    ETF = "ETF"
    UNKNOWN = "UNKNOWN"  # the lake cannot say (no context, or membership unavailable)


class InstrumentKind(StrEnum):
    """``INF…`` ISINs are fund units: every NSE ETF. A liquid ETF is cash-like."""

    EQUITY = "EQUITY"
    ETF = "ETF"
    LIQUID_ETF = "LIQUID_ETF"


# ── the lake seam ────────────────────────────────────────────────────────────────────────────


class LakeSource(Protocol):
    """The per-stock lake reads the review makes. Production: `LakeContextSource`.

    Each method may raise; the builder records the failure against that piece only.
    """

    def latest_session(self, through: date) -> date:
        """The newest NSE session on or before ``through``."""

    def security_names(self) -> Mapping[str, str]:
        """ISIN → name for every security in the identity master."""

    def bars(
        self, isins: frozenset[str], through: date, sessions: int
    ) -> Mapping[str, Sequence[PriceBar]]:
        """Adjusted bars (raw close beside) for the last ``sessions`` sessions, ascending."""

    def sectors(self, through: date) -> Mapping[str, str]:
        """ISIN → NSE industry from the latest classification on or before ``through``."""

    def index_members(self, slugs: Sequence[str], on: date) -> Mapping[str, frozenset[str]]:
        """Index slug → member ISINs in force on ``on``."""

    def fundamentals(
        self, isins: frozenset[str], through: date, prices: Mapping[str, Decimal]
    ) -> Mapping[str, FundamentalMetrics]:
        """PIT metrics as of ``through`` from filings knowable then."""

    def thin_trading(self, isins: frozenset[str], through: date) -> Mapping[str, ThinTradingLabel]:
        """The M13.8 label from the newest labelled session on or before ``through``."""

    def corporate_actions(
        self, isins: frozenset[str], start: date, through: date
    ) -> Sequence[CorporateActionNotice]:
        """Corporate-action broadcasts knowable in ``[start, through]``."""


# ── result values ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ThinTradingView:
    as_of: date
    thin: bool
    traded_ratio: Decimal
    longest_gap_sessions: int
    current_gap_sessions: int
    trade_to_trade: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FundamentalsView:
    """Latest PIT metrics; a metric that cannot be computed carries its reason, never a zero."""

    filing_date: date
    latest_period_end: date
    revenue_ttm_growth: Decimal | str
    profit_ttm_growth: Decimal | str
    net_margin_ttm: Decimal | str
    roe: Decimal | str
    pe_ttm: Decimal | str
    pb: Decimal | str


@dataclass(frozen=True, slots=True)
class CorporateActionDue:
    purpose: str
    ex_date: date | None
    record_date: date | None
    announced: date


@dataclass(frozen=True, slots=True)
class LakeContext:
    """What the lake knows about one holding, as of ``session``. ``None`` = not computable."""

    isin: str
    name: str | None
    session: date
    close: Decimal
    close_session: date
    returns: Mapping[str, Decimal | None]
    dma50_distance: Decimal | None
    dma200_distance: Decimal | None
    high_52w: Decimal | None
    low_52w: Decimal | None
    from_52w_high: Decimal | None
    from_52w_low: Decimal | None
    median_traded_value_20: Decimal | None
    sector: str | None
    indices: tuple[str, ...]
    size_bucket: SizeBucket
    thin_trading: ThinTradingView | None
    fundamentals: FundamentalsView | None
    corporate_actions_due: tuple[CorporateActionDue, ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HoldingReview:
    """One held ISIN: Kite's row, the lake's context (or why there is none), and the arithmetic.

    ``value`` uses the lake close when the lake has one (``value_basis = "lake_close"``) and
    Kite's last price otherwise, so an unpriced holding still counts toward the total.
    """

    holding: KiteHolding
    kind: InstrumentKind
    lake: LakeContext | None
    lake_unavailable: str | None
    held_quantity: int
    value: Decimal
    value_basis: str
    value_kite: Decimal
    cost: Decimal
    unrealised_pnl: Decimal
    unrealised_pnl_pct: Decimal | None
    weight: Decimal

    @property
    def isin(self) -> str:
        return self.holding.isin

    @property
    def lake_context_label(self) -> str:
        return "available" if self.lake is not None else f"unavailable({self.lake_unavailable})"


@dataclass(frozen=True, slots=True)
class PortfolioReview:
    snapshot_id: str
    taken_at: datetime
    lake_session: date | None
    holdings: tuple[HoldingReview, ...]
    holdings_value: Decimal
    cost: Decimal
    unrealised_pnl: Decimal
    cash: Decimal
    total: Decimal
    liquid_etf_value: Decimal
    liquid_etf_share: Decimal | None
    cash_and_liquid_share: Decimal | None
    top5_weight: Decimal
    hhi: Decimal
    sector_weights: tuple[tuple[str, Decimal], ...]
    size_weights: tuple[tuple[str, Decimal], ...]
    kind_weights: tuple[tuple[str, Decimal], ...]
    unpriced: tuple[tuple[str, str, str], ...]
    mf_value: Decimal
    lake_notes: tuple[str, ...] = field(default=())

    def holding(self, isin: str) -> HoldingReview | None:
        return next((h for h in self.holdings if h.isin == isin), None)


# ── arithmetic (pure) ────────────────────────────────────────────────────────────────────────


def _ratio(value: Decimal) -> Decimal:
    return value.quantize(_RATIO_Q, rounding=ROUND_HALF_EVEN)


def _money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_Q, rounding=ROUND_HALF_EVEN)


def _change(now: Decimal, base: Decimal) -> Decimal | None:
    return None if base <= _ZERO else _ratio(now / base - _ONE)


def trailing_return(closes: Sequence[Decimal], sessions: int) -> Decimal | None:
    """``close[-1] / close[-1-sessions] - 1``, or None with too little history."""
    if len(closes) <= sessions:
        return None
    return _change(closes[-1], closes[-1 - sessions])


def sma_distance(closes: Sequence[Decimal], window: int) -> Decimal | None:
    """``close / SMA(window) - 1``, or None with fewer than ``window`` closes."""
    if len(closes) < window:
        return None
    mean = sum(closes[-window:], _ZERO) / window
    return _change(closes[-1], mean)


def hhi(weights: Iterable[Decimal]) -> Decimal:
    """Herfindahl index of the weights (fractions): 1/n for n equal weights, 1 for one name."""
    return _ratio(sum((w * w for w in weights), _ZERO))


def instrument_kind(isin: str, name: str | None, symbol: str) -> InstrumentKind:
    """``INF`` ISIN → a fund unit (ETF); "LIQUID" in its name or symbol → a liquid ETF."""
    if not isin.startswith("INF"):
        return InstrumentKind.EQUITY
    label = f"{name or ''} {symbol}".upper()
    return InstrumentKind.LIQUID_ETF if "LIQUID" in label else InstrumentKind.ETF


def size_bucket(
    isin: str, kind: InstrumentKind, members: Mapping[str, frozenset[str]]
) -> SizeBucket:
    if kind is not InstrumentKind.EQUITY:
        return SizeBucket.ETF
    if not members:
        return SizeBucket.UNKNOWN
    if isin in members.get("nifty100", frozenset()):
        return SizeBucket.LARGE
    if isin in members.get("niftymidcap150", frozenset()):
        return SizeBucket.MID
    if isin in members.get("niftysmallcap250", frozenset()):
        return SizeBucket.SMALL
    return SizeBucket.MICRO


def _metric(value: MetricValue) -> Decimal | str:
    return value.value if isinstance(value, Unavailable) else _ratio(value)


def fundamentals_view(metrics: FundamentalMetrics) -> FundamentalsView:
    return FundamentalsView(
        filing_date=metrics.knowable_date,
        latest_period_end=metrics.latest_period_end,
        revenue_ttm_growth=_metric(metrics.revenue_ttm_yoy),
        profit_ttm_growth=_metric(metrics.earnings_ttm_yoy),
        net_margin_ttm=_metric(metrics.net_margin_ttm),
        roe=_metric(metrics.roe),
        pe_ttm=_metric(metrics.pe_ttm),
        pb=_metric(metrics.pb),
    )


# ── the builder ──────────────────────────────────────────────────────────────────────────────


def _attempt[T](piece: str, notes: list[str], fn: Callable[[], T], default: T) -> T:
    """Run one lake read; on failure record which piece and why, and use ``default``.

    Not a silent swallow: the failure is logged (piece and error class only — no holdings) and
    reported in the review's ``lake_notes`` so the reader sees exactly what is missing.
    """
    try:
        return fn()
    except Exception as err:
        notes.append(f"{piece}: unavailable ({type(err).__name__}: {str(err)[:160]})")
        _LOG.warning(
            "owner_portfolio.lake_piece_unavailable", piece=piece, error=type(err).__name__
        )
        return default


def _lake_context(
    isin: str,
    *,
    session: date,
    bars: Sequence[PriceBar],
    name: str | None,
    kind: InstrumentKind,
    sectors: Mapping[str, str],
    members: Mapping[str, frozenset[str]],
    metrics: Mapping[str, FundamentalMetrics],
    thin: Mapping[str, ThinTradingLabel],
    actions: Sequence[CorporateActionNotice],
    piece_notes: Sequence[str],
) -> LakeContext:
    closes = [b.close for b in bars]
    last = bars[-1]
    year = bars[-252:]
    high = max(b.high for b in year)
    low = min(b.low for b in year)
    traded = [b.traded_value for b in bars[-_MEDIAN_TV_SESSIONS:]]
    notes = list(piece_notes)
    if last.trade_date < session:
        notes.append(f"last traded {last.trade_date.isoformat()}, before the lake session")
    label = thin.get(isin)
    due = tuple(
        CorporateActionDue(a.purpose, a.ex_date, a.record_date, a.knowable_date)
        for a in actions
        if a.isin == isin and (a.ex_date is None or a.ex_date > session)
    )
    return LakeContext(
        isin=isin,
        name=name,
        session=session,
        close=last.raw_close,
        close_session=last.trade_date,
        returns={k: trailing_return(closes, n) for k, n in RETURN_SESSIONS.items()},
        dma50_distance=sma_distance(closes, 50),
        dma200_distance=sma_distance(closes, 200),
        high_52w=high,
        low_52w=low,
        from_52w_high=_change(closes[-1], high),
        from_52w_low=_change(closes[-1], low),
        median_traded_value_20=_money(statistics.median(traded)) if traded else None,
        sector=sectors.get(isin),
        indices=tuple(slug for slug in INDEX_SLUGS if isin in members.get(slug, frozenset())),
        size_bucket=size_bucket(isin, kind, members),
        thin_trading=None
        if label is None
        else ThinTradingView(
            as_of=label.as_of,
            thin=label.thin,
            traded_ratio=label.traded_ratio,
            longest_gap_sessions=label.longest_gap_sessions,
            current_gap_sessions=label.current_gap_sessions,
            trade_to_trade=label.trade_to_trade,
            reasons=tuple(label.reasons),
        ),
        fundamentals=fundamentals_view(metrics[isin]) if isin in metrics else None,
        corporate_actions_due=due,
        notes=tuple(notes),
    )


def _weights(
    pairs: Iterable[tuple[str, Decimal]], total: Decimal
) -> tuple[tuple[str, Decimal], ...]:
    sums: dict[str, Decimal] = {}
    for key, value in pairs:
        sums[key] = sums.get(key, _ZERO) + value
    if total <= _ZERO:
        return ()
    return tuple(
        sorted(((k, _ratio(v / total)) for k, v in sums.items()), key=lambda kv: (-kv[1], kv[0]))
    )


def build_review(
    portfolio: OwnerPortfolio, lake: LakeSource | None, *, today: date
) -> PortfolioReview:
    """Join ``portfolio`` to the lake as of the newest lake session on or before ``today``.

    ``lake=None`` (or a lake that cannot name a session) yields a review of Kite's numbers alone,
    every holding marked ``unavailable``. ``today`` comes from the caller's injected clock.
    """
    held = portfolio.held
    isins = frozenset(h.isin for h in held)
    notes: list[str] = []
    session: date | None = None
    if lake is None:
        notes.append("lake: not wired")
    else:
        session = _attempt("lake session", notes, lambda: lake.latest_session(today), None)

    names: Mapping[str, str] = {}
    bars: Mapping[str, Sequence[PriceBar]] = {}
    sectors: Mapping[str, str] = {}
    members: Mapping[str, frozenset[str]] = {}
    metrics: Mapping[str, FundamentalMetrics] = {}
    thin: Mapping[str, ThinTradingLabel] = {}
    actions: Sequence[CorporateActionNotice] = ()
    if lake is not None and session is not None and isins:
        s = session
        names = _attempt("identity master", notes, lake.security_names, {})
        bars = _attempt("price bars", notes, lambda: lake.bars(isins, s, SESSIONS_NEEDED), {})
        sectors = _attempt("industry classification", notes, lambda: lake.sectors(s), {})
        members = _attempt(
            "index membership", notes, lambda: lake.index_members(INDEX_SLUGS, s), {}
        )
        prices = {i: b[-1].raw_close for i, b in bars.items() if b}
        metrics = _attempt(
            "PIT fundamentals", notes, lambda: lake.fundamentals(isins, s, prices), {}
        )
        thin = _attempt("thin-trading label", notes, lambda: lake.thin_trading(isins, s), {})
        actions = _attempt(
            "corporate actions",
            notes,
            lambda: lake.corporate_actions(isins, s - timedelta(days=_CA_LOOKBACK_DAYS), s),
            (),
        )

    rows: list[tuple[KiteHolding, InstrumentKind, LakeContext | None, str | None]] = []
    for h in held:
        name = names.get(h.isin)
        kind = instrument_kind(h.isin, name, h.symbol)
        series = bars.get(h.isin) or ()
        if session is None:
            rows.append((h, kind, None, "no lake session"))
        elif series:
            found = _lake_context(
                h.isin,
                session=session,
                bars=series,
                name=name,
                kind=kind,
                sectors=sectors,
                members=members,
                metrics=metrics,
                thin=thin,
                actions=actions,
                piece_notes=(),
            )
            rows.append((h, kind, found, None))
        elif names and h.isin not in names:
            rows.append((h, kind, None, "ISIN not in the lake's identity master"))
        else:
            rows.append(
                (h, kind, None, f"no NSE bar in the {SESSIONS_NEEDED} sessions to {session}")
            )

    valued: list[
        tuple[KiteHolding, InstrumentKind, LakeContext | None, str | None, Decimal, str]
    ] = []
    for h, kind, ctx, why in rows:
        if ctx is not None:
            valued.append((h, kind, ctx, why, ctx.close * h.held_quantity, "lake_close"))
        else:
            valued.append((h, kind, ctx, why, h.last_price * h.held_quantity, "kite_last_price"))
    holdings_value = sum((v for *_, v, _ in valued), _ZERO)

    reviews: list[HoldingReview] = []
    for h, kind, ctx, why, value, basis in valued:
        cost = h.average_price * h.held_quantity
        pnl = value - cost
        reviews.append(
            HoldingReview(
                holding=h,
                kind=kind,
                lake=ctx,
                lake_unavailable=why,
                held_quantity=h.held_quantity,
                value=_money(value),
                value_basis=basis,
                value_kite=_money(h.last_price * h.held_quantity),
                cost=_money(cost),
                unrealised_pnl=_money(pnl),
                unrealised_pnl_pct=None if cost <= _ZERO else _ratio(pnl / cost),
                weight=_ratio(value / holdings_value) if holdings_value > _ZERO else _ZERO,
            )
        )
    reviews.sort(key=lambda r: (-r.value, r.isin))

    cash = portfolio.funds.equity.net
    total = holdings_value + cash
    liquid = sum((r.value for r in reviews if r.kind is InstrumentKind.LIQUID_ETF), _ZERO)
    # Concentration from unrounded weights, so the top five of five names is exactly 1.
    weights = [v / holdings_value for *_, v, _ in valued] if holdings_value > _ZERO else []
    unpriced = tuple(
        (r.isin, r.holding.symbol, r.lake_unavailable or "") for r in reviews if r.lake is None
    )
    mf_value = sum((m.quantity * m.last_price for m in portfolio.mf_holdings), _ZERO)
    review = PortfolioReview(
        snapshot_id=portfolio.snapshot_id,
        taken_at=portfolio.taken_at,
        lake_session=session,
        holdings=tuple(reviews),
        holdings_value=_money(holdings_value),
        cost=_money(sum((r.cost for r in reviews), _ZERO)),
        unrealised_pnl=_money(sum((r.unrealised_pnl for r in reviews), _ZERO)),
        cash=_money(cash),
        total=_money(total),
        liquid_etf_value=_money(liquid),
        liquid_etf_share=_ratio(liquid / total) if total > _ZERO else None,
        cash_and_liquid_share=_ratio((cash + liquid) / total) if total > _ZERO else None,
        top5_weight=_ratio(sum(sorted(weights, reverse=True)[:5], _ZERO)),
        hhi=hhi(weights),
        sector_weights=_weights(
            (
                (
                    r.lake.sector
                    if r.lake is not None and r.lake.sector
                    else ("ETF" if r.kind is not InstrumentKind.EQUITY else "UNCLASSIFIED"),
                    r.value,
                )
                for r in reviews
            ),
            holdings_value,
        ),
        size_weights=_weights(
            (
                (
                    r.lake.size_bucket.value
                    if r.lake is not None
                    else (
                        SizeBucket.ETF.value
                        if r.kind is not InstrumentKind.EQUITY
                        else SizeBucket.UNKNOWN.value
                    ),
                    r.value,
                )
                for r in reviews
            ),
            holdings_value,
        ),
        kind_weights=_weights(((r.kind.value, r.value) for r in reviews), holdings_value),
        unpriced=unpriced,
        mf_value=_money(mf_value),
        lake_notes=tuple(notes),
    )
    _LOG.info(
        "owner_portfolio.review.built",
        snapshot_id=portfolio.snapshot_id,
        holdings=len(reviews),
        with_lake=sum(1 for r in reviews if r.lake is not None),
        lake_notes=len(notes),
    )
    return review


# ── production lake wiring ───────────────────────────────────────────────────────────────────

#: Series tried, in order, for a holding the EQ read did not find (trade-to-trade, SME).
_SERIES_FALLBACK: Final[tuple[str, ...]] = ("EQ", "BE", "BZ", "SM", "ST")


class LakeContextSource:
    """`LakeSource` over the local lake and the (read-only) Postgres masters.

    Each read is the existing one: `LakeCommonsSource` (sessions, adjusted bars with the L2-lag
    overlay, industry, filings, corporate actions), `index_membership_asof`, `thin_trading_asof`,
    `compute_metrics` behind the `PitContext` guard, and the identity master loaded with an
    in-memory reconciliation queue so a lookup can never write a conflict row to Postgres.
    What it never does: write, fetch, or modify anything M17 reads.
    """

    def __init__(self, *, clock: Any, data_root: Path | None, settings: Any) -> None:
        from analyst.commons.sources import LakeCommonsSource

        self._commons = LakeCommonsSource(clock=clock, data_root=data_root)
        self._data_root = data_root
        self._settings = settings

    def close(self) -> None:
        self._commons.close()

    def __enter__(self) -> LakeContextSource:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def latest_session(self, through: date) -> date:
        return self._commons.sessions(through, 1).records[-1]

    def security_names(self) -> Mapping[str, str]:
        from dataplatform.identity import IdentityStore, InMemoryReconciliationQueue
        from dataplatform.store.db import connection

        with connection(self._settings) as conn:
            master = IdentityStore(conn).load_master(queue=InMemoryReconciliationQueue())
            conn.rollback()
        return {isin: sec.name for isin, sec in master.securities.items()}

    def bars(
        self, isins: frozenset[str], through: date, sessions: int
    ) -> Mapping[str, Sequence[PriceBar]]:
        from dataplatform.query import PitContext

        guard = PitContext(as_of=through)
        window = list(guard.admit(self._commons.sessions(through, sessions)))
        found: dict[str, list[PriceBar]] = {}
        missing = set(isins)
        for series in _SERIES_FALLBACK:
            if not missing:
                break
            dataset = self._commons.price_bars(frozenset(missing), window, series)
            for bar in guard.admit(dataset):
                found.setdefault(bar.isin, []).append(bar)
            missing -= set(found)
        return {isin: sorted(b, key=lambda x: x.trade_date) for isin, b in found.items()}

    def sectors(self, through: date) -> Mapping[str, str]:
        from dataplatform.query import PitContext

        rows = PitContext(as_of=through).admit(self._commons.sectors(through))
        return {r.isin: r.sector for r in rows}

    def index_members(self, slugs: Sequence[str], on: date) -> Mapping[str, frozenset[str]]:
        from dataplatform.query import index_membership_asof

        return {s: index_membership_asof([s], on, data_root=self._data_root) for s in slugs}

    def fundamentals(
        self, isins: frozenset[str], through: date, prices: Mapping[str, Decimal]
    ) -> Mapping[str, FundamentalMetrics]:
        from dataplatform.query import PitContext
        from dataplatform.query.fundamentals_metrics import compute_metrics

        facts = [
            f
            for f in PitContext(as_of=through).admit(self._commons.filings(through))
            if f.isin in isins
        ]
        return compute_metrics(facts, as_of=through, prices=prices)

    def thin_trading(self, isins: frozenset[str], through: date) -> Mapping[str, ThinTradingLabel]:
        from dataplatform.query import thin_trading_asof
        from dataplatform.store.db import connection

        with connection(self._settings) as conn:
            labels = thin_trading_asof(conn, through, isins=isins)
            conn.rollback()
        return labels

    def corporate_actions(
        self, isins: frozenset[str], start: date, through: date
    ) -> Sequence[CorporateActionNotice]:
        from dataplatform.query import PitContext

        rows = PitContext(as_of=through).admit(self._commons.corporate_actions(start, through))
        return [r for r in rows if r.isin in isins]
