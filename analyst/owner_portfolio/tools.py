"""M19.5 · The review tools — one set of functions behind both the MCP server and the CLI.

Every tool is read-only. Only `refresh` talks to Kite (GET-only, through `KiteReadTransport`); every
other tool reads a snapshot, so a review is reproducible and cannot hammer the API. Symbols are
accepted as an input convenience and resolved to the ISIN through the snapshot's own Kite row;
every output is keyed by ISIN with the symbol as a display label (invariant #2).

A tool never raises for an expected condition — no session, no snapshot, a refresh too soon, a
Kite error — it returns ``{"error": "<what happened and what to do>"}`` so the agent can relay it.
Money is `Decimal` throughout and serialised as a string, so no value passes through a float.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any, Final

import structlog

from analyst.owner_portfolio.context import LakeSource, PortfolioReview, build_review
from analyst.owner_portfolio.snapshot import (
    OwnerPortfolio,
    RefreshTooSoonError,
    SnapshotError,
    SnapshotStore,
)
from dataplatform.clock import IST, Clock
from execution.kite_read import KiteApiError, KiteReader, KiteReadTransport
from execution.kite_session import (
    KiteSessionFileError,
    SessionStore,
    mask_user_id,
    session_status,
)

if TYPE_CHECKING:  # pragma: no cover
    from dataplatform.config import Settings

__all__ = ["TOOL_NAMES", "ReviewService", "default_service", "quiet_settings", "to_jsonable"]

_LOG = structlog.get_logger(__name__)

#: The complete, pinned tool surface. A test fails if the MCP server registers anything else.
TOOL_NAMES: Final[tuple[str, ...]] = (
    "session_status",
    "refresh",
    "portfolio_overview",
    "holding_detail",
    "exposures",
    "changes",
    "orders_today",
    "trades_today",
    "mf_holdings",
    "gtt_triggers",
)
_ISIN: Final = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
_SNAPSHOT_ID: Final = re.compile(r"\d{4}-\d{2}-\d{2}/\d{6}")
_EXPECTED: Final = (KiteApiError, KiteSessionFileError, SnapshotError)


def to_jsonable(value: Any) -> Any:
    """Dataclasses, Decimals, dates and enums → JSON-safe values. Decimal becomes a string."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(IST).isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, frozenset, set)):
        return [to_jsonable(v) for v in value]
    return value


def _obj(value: Mapping[str, Any]) -> dict[str, Any]:
    out = to_jsonable(value)
    assert isinstance(out, dict)
    return out


ReaderFactory = Callable[[], AbstractContextManager[KiteReader]]
LakeFactory = Callable[[], AbstractContextManager[LakeSource | None]]


@dataclass
class ReviewService:
    """The tools' state: session store, snapshot store, how to reach Kite and the lake.

    ``reader_factory`` builds a `KiteReader` for the current session (it raises
    `LoginNeededError` without one); ``lake_factory`` yields a `LakeSource` or ``None``.
    Reviews are cached per snapshot id: a snapshot never changes, so neither does its review.
    """

    clock: Clock
    sessions: SessionStore
    snapshots: SnapshotStore
    reader_factory: ReaderFactory
    lake_factory: LakeFactory
    _reviews: dict[str, PortfolioReview] = field(default_factory=dict)

    # ── helpers ─────────────────────────────────────────────────────────────────────────────

    def _portfolio(self, snapshot: str | None) -> OwnerPortfolio:
        return self.snapshots.load(snapshot)

    def _review(self, portfolio: OwnerPortfolio) -> PortfolioReview:
        cached = self._reviews.get(portfolio.snapshot_id)
        if cached is not None:
            return cached
        with self.lake_factory() as lake:
            review = build_review(portfolio, lake, today=self.clock.today())
        self._reviews[portfolio.snapshot_id] = review
        return review

    def _run(self, tool: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            out = fn()
        except _EXPECTED as err:
            _LOG.info("owner_portfolio.tool.refused", tool=tool, error=type(err).__name__)
            return {"error": str(err)}
        _LOG.info("owner_portfolio.tool.ok", tool=tool)
        return out

    # ── tools ───────────────────────────────────────────────────────────────────────────────

    def session_status(self) -> dict[str, Any]:
        """Logged in?, expiry (IST), and the last snapshot's id, time and age."""

        def run() -> dict[str, Any]:
            out = session_status(self.sessions, self.clock)
            latest = self.snapshots.latest()
            if latest is None:
                out["last_snapshot"] = None
            else:
                age = self.clock.now() - latest.taken_at
                out["last_snapshot"] = {
                    "snapshot_id": latest.snapshot_id,
                    "taken_at_ist": to_jsonable(latest.taken_at),
                    "age_minutes": int(age.total_seconds() // 60),
                }
            return out

        return self._run("session_status", run)

    def refresh(self) -> dict[str, Any]:
        """Take a new snapshot (GET-only reads), or say why not (login needed, too soon)."""

        def run() -> dict[str, Any]:
            try:
                with self.reader_factory() as reader:
                    info = self.snapshots.refresh(reader)
            except RefreshTooSoonError as err:
                return {
                    "error": str(err),
                    "latest_snapshot_id": err.latest.snapshot_id,
                }
            return {"snapshot_id": info.snapshot_id, "taken_at_ist": to_jsonable(info.taken_at)}

        return self._run("refresh", run)

    def portfolio_overview(self, snapshot: str | None = None) -> dict[str, Any]:
        """Totals, cash, and one row per holding with a summary of its lake context."""

        def run() -> dict[str, Any]:
            portfolio = self._portfolio(snapshot)
            review = self._review(portfolio)
            rows = []
            for r in review.holdings:
                lake = r.lake
                rows.append(
                    {
                        "isin": r.isin,
                        "symbol": r.holding.symbol,
                        "kind": r.kind.value,
                        "held_quantity": r.held_quantity,
                        "t1_quantity": r.holding.t1_quantity,
                        "average_price": r.holding.average_price,
                        "value": r.value,
                        "value_basis": r.value_basis,
                        "kite_mark_value": r.value_kite,
                        "weight": r.weight,
                        "unrealised_pnl": r.unrealised_pnl,
                        "unrealised_pnl_pct": r.unrealised_pnl_pct,
                        "lake_context": r.lake_context_label,
                        "lake_close": None if lake is None else lake.close,
                        "lake_close_session": None if lake is None else lake.close_session,
                        "return_1m": None if lake is None else lake.returns.get("1m"),
                        "return_3m": None if lake is None else lake.returns.get("3m"),
                        "return_1y": None if lake is None else lake.returns.get("1y"),
                        "from_52w_high": None if lake is None else lake.from_52w_high,
                        "dma200_distance": None if lake is None else lake.dma200_distance,
                        "sector": None if lake is None else lake.sector,
                        "size_bucket": None if lake is None else lake.size_bucket.value,
                        "thin": None
                        if lake is None or lake.thin_trading is None
                        else lake.thin_trading.thin,
                    }
                )
            return _obj(
                {
                    "snapshot_id": review.snapshot_id,
                    "taken_at_ist": review.taken_at,
                    "lake_session": review.lake_session,
                    "user": mask_user_id(portfolio.profile.user_id),
                    "totals": {
                        "holdings_value": review.holdings_value,
                        "cost": review.cost,
                        "unrealised_pnl": review.unrealised_pnl,
                        "cash": review.cash,
                        "total": review.total,
                        "cash_and_liquid_share": review.cash_and_liquid_share,
                        "mutual_funds_value": review.mf_value,
                    },
                    "holdings": rows,
                    "unpriced_by_lake": [
                        {"isin": i, "symbol": s, "reason": why} for i, s, why in review.unpriced
                    ],
                    "lake_notes": review.lake_notes,
                    "unavailable_endpoints": portfolio.unavailable,
                }
            )

        return self._run("portfolio_overview", run)

    def _resolve(self, portfolio: OwnerPortfolio, isin_or_symbol: str) -> str:
        key = isin_or_symbol.strip().upper()
        if _ISIN.fullmatch(key) and portfolio.holding(key) is not None:
            return key
        # A display symbol is resolved through the snapshot's own Kite row, which carries both.
        match = [h.isin for h in portfolio.holdings if h.symbol.upper() == key]
        if len(match) == 1:
            return match[0]
        raise SnapshotError(
            f"{isin_or_symbol!r} is not a holding in snapshot {portfolio.snapshot_id}"
        )

    def holding_detail(self, isin_or_symbol: str, snapshot: str | None = None) -> dict[str, Any]:
        """The full Kite row and the full lake context of one holding."""

        def run() -> dict[str, Any]:
            portfolio = self._portfolio(snapshot)
            isin = self._resolve(portfolio, isin_or_symbol)
            review = self._review(portfolio)
            row = review.holding(isin)
            kite_row = portfolio.holding(isin)
            return _obj(
                {
                    "snapshot_id": portfolio.snapshot_id,
                    "isin": isin,
                    "kite": kite_row,
                    "held_quantity": None if kite_row is None else kite_row.held_quantity,
                    "review": None
                    if row is None
                    else {
                        "kind": row.kind,
                        "value": row.value,
                        "value_basis": row.value_basis,
                        "kite_mark_value": row.value_kite,
                        "cost": row.cost,
                        "unrealised_pnl": row.unrealised_pnl,
                        "unrealised_pnl_pct": row.unrealised_pnl_pct,
                        "weight": row.weight,
                        "lake_context": row.lake_context_label,
                    },
                    "lake": None if row is None else row.lake,
                    "lake_notes": review.lake_notes,
                }
            )

        return self._run("holding_detail", run)

    def exposures(self, snapshot: str | None = None) -> dict[str, Any]:
        """Sector, size-bucket and ETF/direct-equity weights; concentration; cash share."""

        def run() -> dict[str, Any]:
            review = self._review(self._portfolio(snapshot))
            return _obj(
                {
                    "snapshot_id": review.snapshot_id,
                    "lake_session": review.lake_session,
                    "weights_basis": "share of holdings value (lake close, else Kite mark)",
                    "sector": dict(review.sector_weights),
                    "size_bucket": dict(review.size_weights),
                    "instrument_kind": dict(review.kind_weights),
                    "concentration": {
                        "holdings": len(review.holdings),
                        "top5_weight": review.top5_weight,
                        "hhi": review.hhi,
                    },
                    "cash": review.cash,
                    "liquid_etf_value": review.liquid_etf_value,
                    "liquid_etf_share_of_total": review.liquid_etf_share,
                    "cash_and_liquid_share_of_total": review.cash_and_liquid_share,
                    "unpriced_by_lake": [i for i, _, _ in review.unpriced],
                }
            )

        return self._run("exposures", run)

    def _snapshot_at_or_before(self, ref: str) -> str:
        if _SNAPSHOT_ID.fullmatch(ref):
            return ref
        try:
            day = date.fromisoformat(ref)
        except ValueError:
            raise SnapshotError(
                f"{ref!r} is neither a snapshot id (YYYY-MM-DD/HHMMSS) nor a date"
            ) from None
        candidates = [
            s for s in self.snapshots.snapshots() if s.taken_at.astimezone(IST).date() <= day
        ]
        if not candidates:
            raise SnapshotError(f"no snapshot on or before {day.isoformat()}")
        return candidates[-1].snapshot_id

    def changes(self, since: str, until: str | None = None) -> dict[str, Any]:
        """Holdings diff between two snapshots: bought, sold, quantity changed (by ISIN)."""

        def run() -> dict[str, Any]:
            before = self._portfolio(self._snapshot_at_or_before(since))
            after = self._portfolio(None if until is None else self._snapshot_at_or_before(until))
            old = {h.isin: h for h in before.held}
            new = {h.isin: h for h in after.held}
            bought = [
                {"isin": i, "symbol": new[i].symbol, "quantity": new[i].held_quantity}
                for i in sorted(new.keys() - old.keys())
            ]
            sold = [
                {"isin": i, "symbol": old[i].symbol, "quantity": old[i].held_quantity}
                for i in sorted(old.keys() - new.keys())
            ]
            changed = [
                {
                    "isin": i,
                    "symbol": new[i].symbol,
                    "from": old[i].held_quantity,
                    "to": new[i].held_quantity,
                    "delta": new[i].held_quantity - old[i].held_quantity,
                }
                for i in sorted(old.keys() & new.keys())
                if old[i].held_quantity != new[i].held_quantity
            ]
            return _obj(
                {
                    "from_snapshot": before.snapshot_id,
                    "to_snapshot": after.snapshot_id,
                    "bought": bought,
                    "sold": sold,
                    "quantity_changed": changed,
                    "cash_from": before.funds.equity.net,
                    "cash_to": after.funds.equity.net,
                }
            )

        return self._run("changes", run)

    def orders_today(self, snapshot: str | None = None) -> dict[str, Any]:
        """The order book Kite returned in the snapshot (Kite serves today's only)."""
        return self._run(
            "orders_today",
            lambda: self._listing(snapshot, lambda p: p.orders, "orders"),
        )

    def trades_today(self, snapshot: str | None = None) -> dict[str, Any]:
        """The fills Kite returned in the snapshot (today's only)."""
        return self._run(
            "trades_today",
            lambda: self._listing(snapshot, lambda p: p.trades, "trades"),
        )

    def mf_holdings(self, snapshot: str | None = None) -> dict[str, Any]:
        """Mutual-fund folios and SIPs (keyed by scheme ISIN)."""

        def run() -> dict[str, Any]:
            portfolio = self._portfolio(snapshot)
            return _obj(
                {
                    "snapshot_id": portfolio.snapshot_id,
                    "mf_holdings": portfolio.mf_holdings,
                    "mf_sips": portfolio.mf_sips,
                    "mf_orders": portfolio.mf_orders,
                    "unavailable_endpoints": [
                        u for u in portfolio.unavailable if u.endpoint.startswith("mf_")
                    ],
                }
            )

        return self._run("mf_holdings", run)

    def gtt_triggers(self, snapshot: str | None = None) -> dict[str, Any]:
        """Standing GTT triggers."""
        return self._run(
            "gtt_triggers",
            lambda: self._listing(snapshot, lambda p: p.gtt_triggers, "gtt_triggers"),
        )

    def _listing(
        self, snapshot: str | None, pick: Callable[[OwnerPortfolio], Any], key: str
    ) -> dict[str, Any]:
        portfolio = self._portfolio(snapshot)
        return _obj(
            {
                "snapshot_id": portfolio.snapshot_id,
                "taken_at_ist": portfolio.taken_at,
                key: pick(portfolio),
                "unavailable_endpoints": [u for u in portfolio.unavailable if u.endpoint == key],
            }
        )


# ── production wiring ────────────────────────────────────────────────────────────────────────


def quiet_settings(settings: Settings) -> Settings:
    """``settings`` with logging at WARNING or above (unless already stricter).

    The lake reads the review reuses log the ISINs they touch at INFO; for the owner's private
    portfolio that would put the holdings in the log (spec §4 I2), so the review's entrypoints
    log warnings and errors only.
    """
    from dataplatform.config import LogLevel

    order = list(LogLevel)
    if order.index(settings.log_level) >= order.index(LogLevel.WARNING):
        return settings
    return settings.model_copy(update={"log_level": LogLevel.WARNING})


def default_service(settings: Settings, clock: Clock) -> ReviewService:
    """The service over the configured session file, ``<data_root>/private/kite`` and the lake."""
    from analyst.owner_portfolio.context import LakeContextSource
    from analyst.owner_portfolio.snapshot import default_snapshot_root
    from execution.kite_session import LOGIN_HINT, LoginNeededError

    sessions = SessionStore(settings.kite_session_file, clock=clock)
    snapshots = SnapshotStore(default_snapshot_root(settings.data_root), clock=clock)

    @contextmanager
    def reader() -> Iterator[KiteReader]:
        import httpx

        session = sessions.require_valid()
        if settings.kite_api_key is None:
            raise LoginNeededError(f"KITE_API_KEY is not set in .env; {LOGIN_HINT}")
        with httpx.Client(timeout=20.0) as client:
            yield KiteReader(
                KiteReadTransport(
                    api_key=settings.kite_api_key,
                    access_token=session.access_token,
                    client=client,
                )
            )

    @contextmanager
    def lake() -> Iterator[LakeSource | None]:
        source: LakeContextSource | None = None
        try:
            source = LakeContextSource(clock=clock, data_root=settings.data_root, settings=settings)
        except Exception as err:  # the lake is optional: a review of Kite's numbers still works
            _LOG.warning("owner_portfolio.lake_unavailable", error=type(err).__name__)
        if source is None:
            yield None
            return
        with source:
            yield source

    return ReviewService(
        clock=clock,
        sessions=sessions,
        snapshots=snapshots,
        reader_factory=reader,
        lake_factory=lake,
    )
