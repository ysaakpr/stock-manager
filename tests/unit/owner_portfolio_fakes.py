"""Shared offline doubles for the M19 owner-portfolio tests: a fixture Kite and a fake lake.

Everything is synthetic (INFY/TCS-style values). The fake lake answers the `LakeSource` protocol
from in-memory tables, so `build_review` runs without DuckDB, Postgres or a lake on disk.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import httpx
from pydantic import SecretStr

from analyst.commons.inputs import CorporateActionNotice, PriceBar
from analyst.owner_portfolio.context import LakeSource
from analyst.owner_portfolio.snapshot import SnapshotStore
from analyst.owner_portfolio.tools import ReviewService
from dataplatform.clock import IST, FrozenClock
from dataplatform.ingest.xbrl.models import Nature
from dataplatform.quality.thin_trading import ThinTradingLabel
from dataplatform.query.fundamentals_metrics import FundamentalMetrics, Unavailable
from execution.kite_read import Endpoint, KiteReader, KiteReadTransport
from execution.kite_session import KiteSession, SessionStore, next_expiry

FIXTURES: Final[Path] = Path(__file__).resolve().parents[1] / "fixtures" / "kite_read"
NOW: Final[datetime] = datetime(2026, 11, 6, 15, 45, 0, tzinfo=IST)
SESSION: Final[date] = date(2026, 11, 6)

INFY: Final = "INE009A01021"
TCS: Final = "INE467B01029"
NIFTYBEES: Final = "INF204KB14I2"
LIQUIDBEES: Final = "INF732E01037"
SME: Final = "INE0ZZZ01011"
WIPRO: Final = "INE075A01022"

SYMBOLS: Final = ("INFY", "TCS", "NIFTYBEES", "LIQUIDBEES", "SMEXYZ-SM", "WIPRO")
ISINS: Final = (INFY, TCS, NIFTYBEES, LIQUIDBEES, SME, WIPRO)

#: (base close, final close) per ISIN the fake lake prices. SME is unknown to the lake.
PRICES: Final[Mapping[str, tuple[Decimal, Decimal]]] = {
    INFY: (Decimal("1500.00"), Decimal("1550.00")),
    TCS: (Decimal("3000.00"), Decimal("3300.00")),
    NIFTYBEES: (Decimal("250.00"), Decimal("262.00")),
    LIQUIDBEES: (Decimal("1000.00"), Decimal("1000.00")),
}
NAMES: Final[Mapping[str, str]] = {
    INFY: "Infosys Limited",
    TCS: "Tata Consultancy Services Limited",
    NIFTYBEES: "Nippon India ETF Nifty 50 BeES",
    LIQUIDBEES: "Nippon India ETF Nifty 1D Rate Liquid BeES",
    WIPRO: "Wipro Limited",
}


def fixture_kite() -> httpx.MockTransport:
    def serve(request: httpx.Request) -> httpx.Response:
        stem = Endpoint(request.url.path).stem
        return httpx.Response(200, content=(FIXTURES / f"{stem}.json").read_bytes())

    return httpx.MockTransport(serve)


def fixture_reader(transport: httpx.MockTransport | None = None) -> tuple[KiteReader, httpx.Client]:
    client = httpx.Client(transport=transport or fixture_kite())
    reader = KiteReader(
        KiteReadTransport(
            api_key=SecretStr("synthkey"),
            access_token=SecretStr("synthtoken0001"),
            client=client,
            sleep=lambda _: None,
        )
    )
    return reader, client


def _bars(isin: str, base: Decimal, final: Decimal, sessions: int = 253) -> list[PriceBar]:
    start = SESSION - timedelta(days=sessions - 1)
    out = []
    for i in range(sessions):
        close = final if i == sessions - 1 else base
        out.append(
            PriceBar(
                isin=isin,
                trade_date=start + timedelta(days=i),
                high=close + Decimal("10"),
                low=close - Decimal("10"),
                close=close,
                volume=Decimal(1000),
                raw_close=close,
                traded_value=close * 1000,
                size=close * 1000,
                deliv_qty=None,
                deliv_pct=None,
            )
        )
    return out


def _metrics(isin: str) -> FundamentalMetrics:
    return FundamentalMetrics(
        isin=isin,
        as_of=SESSION,
        nature=Nature.CONSOLIDATED,
        knowable_date=date(2026, 10, 16),
        latest_period_end=date(2026, 9, 30),
        quarters=12,
        revenue_ttm=Decimal("1000"),
        earnings_ttm=Decimal("200"),
        eps_ttm=Decimal("50"),
        revenue_yoy=Decimal("0.08"),
        earnings_yoy=Decimal("0.10"),
        earnings_ttm_yoy=Decimal("0.12"),
        revenue_ttm_yoy=Decimal("0.09"),
        net_margin_ttm=Decimal("0.2"),
        net_margin_trend=Decimal("0.01"),
        shares_outstanding=Decimal("4"),
        market_cap=Decimal("6200"),
        pe_ttm=Decimal("31"),
        earnings_yield=Decimal("0.032"),
        book_value=Unavailable.BALANCE_SHEET_ABSENT,
        pb=Unavailable.BALANCE_SHEET_ABSENT,
        roe=Unavailable.BALANCE_SHEET_ABSENT,
    )


def _label(isin: str, thin: bool) -> ThinTradingLabel:
    return ThinTradingLabel(
        as_of=SESSION,
        isin=isin,
        window_start=SESSION - timedelta(days=365),
        sessions_observed=250,
        sessions_traded=250 if not thin else 100,
        capture_holes=0,
        traded_ratio=Decimal("1") if not thin else Decimal("0.4"),
        nse_traded_ratio=None,
        bse_traded_ratio=None,
        longest_gap_sessions=0 if not thin else 30,
        longest_gap_days=0 if not thin else 45,
        longest_gap_from=None,
        longest_gap_to=None,
        current_gap_sessions=0,
        last_traded=SESSION,
        nse_series="EQ",
        bse_series=None,
        trade_to_trade=False,
        listing_status="ACTIVE",
        insufficient_history=False,
        identity_seam=False,
        thin=thin,
        reasons=() if not thin else ("traded_ratio",),
    )


class FakeLake:
    """`LakeSource` from in-memory tables. ``broken`` names pieces that raise."""

    def __init__(self, broken: frozenset[str] = frozenset()) -> None:
        self.broken = broken
        self.calls: list[str] = []

    def _check(self, piece: str) -> None:
        self.calls.append(piece)
        if piece in self.broken:
            raise RuntimeError(f"{piece} is down")

    def latest_session(self, through: date) -> date:
        self._check("session")
        return min(through, SESSION)

    def security_names(self) -> Mapping[str, str]:
        self._check("names")
        return dict(NAMES)

    def bars(
        self, isins: frozenset[str], through: date, sessions: int
    ) -> Mapping[str, Sequence[PriceBar]]:
        self._check("bars")
        return {i: _bars(i, *PRICES[i], sessions) for i in isins if i in PRICES}

    def sectors(self, through: date) -> Mapping[str, str]:
        self._check("sectors")
        return {INFY: "Information Technology", TCS: "Information Technology"}

    def index_members(self, slugs: Sequence[str], on: date) -> Mapping[str, frozenset[str]]:
        self._check("indices")
        table = {
            "nifty50": frozenset({INFY, TCS}),
            "nifty100": frozenset({INFY, TCS}),
            "niftymidcap150": frozenset(),
            "niftysmallcap250": frozenset(),
            "nifty500": frozenset({INFY, TCS}),
        }
        return {s: table.get(s, frozenset()) for s in slugs}

    def fundamentals(
        self, isins: frozenset[str], through: date, prices: Mapping[str, Decimal]
    ) -> Mapping[str, FundamentalMetrics]:
        self._check("fundamentals")
        return {i: _metrics(i) for i in isins if i in (INFY, TCS)}

    def thin_trading(self, isins: frozenset[str], through: date) -> Mapping[str, ThinTradingLabel]:
        self._check("thin")
        return {INFY: _label(INFY, False), TCS: _label(TCS, True)}

    def corporate_actions(
        self, isins: frozenset[str], start: date, through: date
    ) -> Sequence[CorporateActionNotice]:
        self._check("actions")
        return [
            CorporateActionNotice(
                isin=TCS,
                purpose="Interim Dividend - Rs 11 Per Share",
                ex_date=SESSION + timedelta(days=10),
                record_date=SESSION + timedelta(days=10),
                knowable_date=SESSION - timedelta(days=3),
            ),
            CorporateActionNotice(
                isin=INFY,
                purpose="Dividend - already ex",
                ex_date=SESSION - timedelta(days=20),
                record_date=None,
                knowable_date=SESSION - timedelta(days=40),
            ),
        ]


def _protocol_check(lake: FakeLake) -> LakeSource:
    return lake  # mypy proves FakeLake satisfies the protocol


def service(
    tmp_path: Path,
    *,
    clock: FrozenClock | None = None,
    lake: FakeLake | None = None,
    logged_in: bool = True,
    kite: httpx.MockTransport | None = None,
) -> ReviewService:
    clk = clock or FrozenClock(NOW)
    sessions = SessionStore(tmp_path / "cfg" / "kite-session.json", clock=clk)
    if logged_in:
        sessions.save(
            KiteSession(
                access_token=SecretStr("synthtoken0001"),
                user_id="AB1234",
                login_time=clk.now() - timedelta(hours=6),
                expires_at=next_expiry(clk.now() - timedelta(hours=6)),
            )
        )
    snapshots = SnapshotStore(tmp_path / "data" / "private" / "kite", clock=clk)
    fake_lake = lake if lake is not None else FakeLake()

    @contextmanager
    def reader() -> Iterator[KiteReader]:
        sessions.require_valid()
        r, client = fixture_reader(kite)
        with client:
            yield r

    @contextmanager
    def lake_cm() -> Iterator[LakeSource | None]:
        yield _protocol_check(fake_lake)

    return ReviewService(
        clock=clk,
        sessions=sessions,
        snapshots=snapshots,
        reader_factory=reader,
        lake_factory=lake_cm,
    )
