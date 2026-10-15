"""M18.7: the console's Portfolio page — offline, over one hand-computed fixture served to the app.

The fixture is one session of FM-POS-FUND-10L (and its mirror, a control and the bench) as the
M17 job persists it: `BOOK_MARK` lines on two sessions, a fill reconciled on the second, staging
lines (a TRIM of a held name, a BUY of a name not yet held, and yesterday's filled BUY), a
manager decision line, a suspended holding, the desk's `paper_session.book_state` for the
session, the closes the book was marked at and a frozen clock. Every figure below was worked out
by hand from those numbers. The acceptance criteria, one block each:

1. the page shows exactly the held names plus the staged new name, BUY/SELL only on the two staged
   names and an empty action cell elsewhere (and yesterday's filled order is not "staged");
2. CMP, average price, day's change, current profit and every fund total are Decimal end to end
   and match the hand computation to the paisa;
3. NAV and holdings equal what `/status/managers` and the digest report for the same session;
4. with no live entries the page shows the dry stream labelled DRY RUN; with live entries, live;
5. every recorded book is selectable on its own; an unknown book is a 404;
6. the M18.1 shared rules hold: read-only, no external URL, no secret rendered, IST;
7. a strategy paper book (`paper_session`) is its own picker group with the same view: holdings,
   staged orders and totals to the paisa, NAV and cash from its latest record, opening capital
   from its first session, the staged marker, its rebalance dates, and "No holdings yet".

The real SQL (the desk record, the symbols, the dry stream's read-back) is exercised against
Postgres in `tests/integration/test_console_portfolio.py`.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, cast

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError

from analyst.fundmanager import load_roster
from analyst.fundmanager.digest import render_digest
from analyst.fundmanager.job import book_entry
from analyst.fundmanager.scoreboard import (
    BookMark,
    DecisionAction,
    ScoredDecision,
    build_scoreboard,
    inputs_from_journal,
    mark_entry,
)
from analyst.journal.models import Actor, Decision, JournalEntry, Sleeve
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.status.api import (
    app,
    clock_source,
    db_connection,
    m17_journal_source,
    m17_roster_source,
)
from dataplatform.status.api import settings_source as api_settings_source
from dataplatform.status.console import READ_METHODS, router
from dataplatform.status.console.portfolio import (
    m17_dry_journal_source,
    portfolio_prices_source,
)
from dataplatform.status.console.portfolio_render import paisa
from dataplatform.status.models_portfolio import HoldingRowOut, PortfolioStream, StagedAction
from dataplatform.status.queries import read_managers_status
from dataplatform.status.queries_portfolio import (
    DRY_DESK_PATTERN,
    LIVE_DESK_RECORD,
    LakeSessionPrices,
    choose_stream,
    read_portfolio,
)
from dataplatform.store.db import Connection
from tests.unit.test_fm_scoreboard import _decision_entry

ROSTER: Final = load_roster()
D0, D1, D2, D3 = date(2026, 10, 7), date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 13)
NOW: Final = datetime(2026, 10, 13, 7, 30, tzinfo=IST)

PRIMARY, MIRROR = "FM-POS-FUND-10L", "FM-POS-FUND-1CR"
CONTROL, BENCH = "CTRL-FM-POS-FUND-10L", "BENCH-N500"
UNMARKED: Final = "CTRL-FM-SWING-BRK-10L"
DRY_DESK: Final = "m17_dry2_fund_managers"

ISIN_A, ISIN_B, ISIN_C, ISIN_D = "INE002A01018", "INE009A01021", "INE467B01029", "INE040A01034"
SYMBOLS: Final = {ISIN_A: "RELIANCE", ISIN_B: "INFY", ISIN_C: "TCS", ISIN_D: "HDFCBANK"}

#: Every planted secret carries this marker, so "no secret leaked" is one substring check.
PLANT: Final = "PLANTEDpf"
_PW: Final = "=".join(("password", f"{PLANT}pw1"))


# ── the fixture: what the M17 job persisted ────────────────────────────────────────────────


def _clock(session: date) -> FrozenClock:
    return FrozenClock(datetime.combine(session, time(22, 0), tzinfo=IST))


def _mark(book: str, session: date, nav: str, cash: str, positions: int) -> JournalEntry:
    mark = BookMark(
        book_id=book, session=session, nav=Decimal(nav), cash=Decimal(cash), positions=positions,
        turnover=Decimal(0), costs=Decimal(0), interest=Decimal(0),
    )  # fmt: skip
    entry, _ = mark_entry(mark, clock=_clock(session))
    return entry


def _staging(
    book: str, session: date, side: Decision, isin: str, quantity: int, event: str, seq: int
) -> JournalEntry:
    """A line `FundBook._stage` writes: BUY/SELL with an order_uid (a planted secret in the why)."""
    return JournalEntry(
        ts=_clock(session).now(),
        trading_date=session,
        case_id=book,
        actor=Actor.EXEC,
        decision=side,
        isin=isin,
        sleeve=Sleeve.TACTICAL,
        rationale=f"staged by the fixture; {_PW}",
        orders_ref=f"m17_{book.lower().replace('-', '_')}:{session}:{seq:03d}",
        payload={
            "event": event,
            "book": book,
            "mode": "PAPER",
            "order_uid": f"m17_{book.lower().replace('-', '_')}:{session}:{seq:03d}",
            "quantity": str(quantity),
            "reference_price": "1",
            "notional": str(quantity),
        },
    )


def _job_line(
    book: str,
    session: date,
    event: str,
    payload: Mapping[str, str],
    *,
    isin: str | None = None,
) -> JournalEntry:
    entry, _ = book_entry(
        book_id=book,
        session=session,
        clock=_clock(session),
        decision=Decision.HEARTBEAT,
        event=event,
        rationale=f"{event} written by the fixture",
        payload=payload,
        actor=Actor.EXEC,
        isin=isin,
    )
    return entry


def _manager_buy_decision() -> JournalEntry:
    """The manager's own BUY decision on D — a decision, not a staging line (no order_uid)."""
    return _decision_entry(
        ScoredDecision(
            book_id=PRIMARY, decided_on=D2, isin=ISIN_D, action=DecisionAction.BUY,
            horizon_sessions=60, p_beat_bench=Decimal("0.55"), target_weight=Decimal("0.05"),
            expected_excess_pct=Decimal("2"), edge_type="EARNINGS", base_rate_p=Decimal("0.5"),
            regime="RISK_ON", cap_tier="LARGE", in_shortlist=True, stop_pct=Decimal("10"),
        ),
        clock=_clock(D2),
    )  # fmt: skip


def journal() -> list[JournalEntry]:
    """The books' entries over D1 and D2, in append order (as the books wrote them)."""
    return [
        _mark(PRIMARY, D1, "1001000.00", "963499.50", 1),
        _mark(MIRROR, D1, "10000000", "10000000", 0),
        _mark(CONTROL, D1, "1000000", "1000000", 0),
        _mark(BENCH, D1, "1000000", "0", 1),
        # D1: the primary staged a BUY of B for D2's open — it fills on D2 (below).
        _staging(PRIMARY, D1, Decision.BUY, ISIN_B, 7, "STAGED", 0),
        # D2: the fill is reconciled, every book is marked, C is suspended.
        _job_line(PRIMARY, D2, "RECONCILIATION", {"fills": "1", "positions": "3"}),
        _mark(PRIMARY, D2, "999266.26", "951234.56", 3),
        _mark(MIRROR, D2, "10000000", "10000000", 0),
        _mark(CONTROL, D2, "1000000", "1000000", 0),
        _mark(BENCH, D2, "997993.242581", "0", 1),
        _job_line(
            PRIMARY,
            D2,
            "SUSPENDED_HOLDING",
            {
                "last_trade_date": D0.isoformat(),
                "last_close": "2900.25",
                "quantity": "4",
                "sessions_suspended": "3",
            },
            isin=ISIN_C,
        ),
        _manager_buy_decision(),
        # D2's staging for D3: a BUY of D (new), a TRIM of B; the mirror follows the BUY.
        _staging(PRIMARY, D2, Decision.BUY, ISIN_D, 5, "STAGED", 0),
        _staging(PRIMARY, D2, Decision.SELL, ISIN_B, 3, "STAGED", 1),
        _staging(MIRROR, D2, Decision.BUY, ISIN_D, 50, "MIRROR_FOLLOW", 0),
    ]


def desk_state(positions: Sequence[Mapping[str, str]] | None = None) -> dict[str, Any]:
    """`paper_session.book_state` as `backtest.fm_job` records it after D2 (the parts read)."""
    held = [
        {"isin": ISIN_A, "quantity": "10", "cost_basis": "25010.50"},
        {"isin": ISIN_B, "quantity": "7", "cost_basis": "10535.3549"},
        {"isin": ISIN_C, "quantity": "4", "cost_basis": "12000.00"},
    ]
    account = {"expected_book": {"cash": "951234.56", "positions": list(positions or held)}}
    flat = {"account": {"expected_book": {"cash": "10000000", "positions": []}}}
    return {
        "version": "m17-desk/2",
        "books": {
            PRIMARY: {"account": account},
            MIRROR: flat,
            CONTROL: {"account": {"expected_book": {"cash": "1000000", "positions": []}}},
        },
        "bench": {"method": "published", "base_level": "35176.1500", "base_session": "2026-10-09"},
    }


#: The closes the books were marked at (NSE), on D2 and the session before it (D1).
CLOSES: Final = {
    D2: {ISIN_A: Decimal("2600.00"), ISIN_B: Decimal("1490.10"), ISIN_D: Decimal("1650.00")},
    D1: {ISIN_A: Decimal("2550.50"), ISIN_B: Decimal("1510.00"), ISIN_D: Decimal("1640.00")},
}


class FakePrices:
    """`SessionPrices` from the table above and a three-session calendar."""

    def __init__(self, closes: Mapping[date, Mapping[str, Decimal]] = CLOSES) -> None:
        self._closes = closes
        self.asked: list[tuple[date, frozenset[str]]] = []

    def closes(self, session: date, isins: Collection[str]) -> Mapping[str, Decimal]:
        self.asked.append((session, frozenset(isins)))
        day = self._closes.get(session, {})
        return {i: day[i] for i in isins if i in day}

    def previous_session(self, session: date) -> date | None:
        return {D2: D1, D3: D2}.get(session)

    def next_session(self, session: date) -> date | None:
        return {D1: D2, D2: D3}.get(session)


@dataclass(frozen=True)
class PaperRow:
    """One `paper_session` row of a strategy paper book (the columns the page reads)."""

    book_id: str
    on: date
    outcome: str = "COMPLETED"
    reason: str = "decided"
    rebalanced: bool = False
    state: Mapping[str, Any] | None = None
    expected: Mapping[str, Any] | None = None


class _Result:
    def __init__(self, rows: Sequence[tuple[Any, ...]]) -> None:
        self._rows = list(rows)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class StubConnection:
    """Answers the Portfolio page's queries from in-memory rows; anything else fails loudly."""

    def __init__(
        self,
        desks: Mapping[tuple[str, date], Mapping[str, Any]],
        paper: Sequence[PaperRow] = (),
    ) -> None:
        self.desks = dict(desks)
        self.paper = list(paper)
        self.queries: list[tuple[str, Sequence[Any]]] = []

    @property
    def db(self) -> Connection:
        return cast(Connection, self)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _Result:
        text = " ".join(sql.split())
        self.queries.append((text, params))
        if text.startswith("SELECT book_id, book_state FROM paper_session WHERE book_id LIKE"):
            pattern, session = params
            assert pattern == DRY_DESK_PATTERN
            dry = [(k, v) for (k, d), v in self.desks.items() if d == session and "dry" in k]
            return _Result([(k, v) for k, v in sorted(dry, reverse=True)][:1])
        if text.startswith("SELECT book_id, book_state FROM paper_session WHERE book_id = %s"):
            record, session = params
            state = self.desks.get((record, session))
            return _Result([] if state is None else [(record, state)])
        if "FROM symbol_history" in text:
            return _Result([(i, SYMBOLS[i]) for i in sorted(params[0]) if i in SYMBOLS])
        if text.startswith("SELECT book_id, trading_date, outcome, reason, rebalanced, book_state"):
            assert params == (LIVE_DESK_RECORD, DRY_DESK_PATTERN)  # the M17 desk records excluded
            return _Result(
                [
                    (r.book_id, r.on, r.outcome, r.reason, r.rebalanced, r.state is not None)
                    for r in sorted(self.paper, key=lambda r: (r.book_id, r.on))
                ]
            )
        if text.startswith("SELECT book_id, trading_date, book_state, expected_book"):
            books, days = params
            return _Result(
                [
                    (r.book_id, r.on, r.state, r.expected)
                    for r in self.paper
                    if r.book_id in books and r.on in days and r.state is not None
                ]
            )
        raise AssertionError(f"unexpected query against the stub connection: {text}")


def _settings(tmp_path: Path) -> Settings:
    return Settings(data_root=tmp_path / "lake", backup_root=tmp_path / "backups")


def _client(
    tmp_path: Path,
    conn: StubConnection,
    *,
    live: Sequence[JournalEntry] = (),
    dry: Sequence[JournalEntry] = (),
    prices: FakePrices | None = None,
) -> TestClient:
    settings = _settings(tmp_path)
    app.dependency_overrides[api_settings_source] = lambda: settings
    app.dependency_overrides[clock_source] = lambda: FrozenClock(NOW)
    app.dependency_overrides[db_connection] = lambda: conn
    app.dependency_overrides[m17_roster_source] = lambda: ROSTER
    app.dependency_overrides[m17_journal_source] = lambda: list(live)
    app.dependency_overrides[m17_dry_journal_source] = lambda: () if live else list(dry)
    app.dependency_overrides[portfolio_prices_source] = lambda: prices or FakePrices()
    return TestClient(app)


@pytest.fixture
def conn() -> StubConnection:
    return StubConnection({(DRY_DESK, D2): desk_state()})


@pytest.fixture
def client(tmp_path: Path, conn: StubConnection) -> Iterator[TestClient]:
    with _client(tmp_path, conn, dry=journal()) as live:
        yield live
    app.dependency_overrides.clear()


def _page(client: TestClient, book: str | None = None) -> str:
    response = client.get("/console/portfolio", params=None if book is None else {"book": book})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    return str(response.text)


def _rows(html: str) -> dict[str, dict[str, str]]:
    """Every holdings row's data attributes, by ISIN, in page order."""
    out: dict[str, dict[str, str]] = {}
    for tag in re.findall(r"<tr data-holding=\"[^\"]*\"[^>]*>", html):
        attrs = dict(re.findall(r'data-([a-z0-9-]+)="([^"]*)"', tag))
        out[attrs.pop("holding")] = attrs
    return out


def _totals(html: str) -> dict[str, str]:
    tag = re.search(r'<table id="totals"[^>]*>', html)
    assert tag is not None, "no fund totals on the page"
    return dict(re.findall(r'data-([a-z0-9-]+)="([^"]*)"', tag.group(0)))


def _model(stream: PortfolioStream = PortfolioStream.DRY, book: str | None = PRIMARY) -> Any:
    conn = StubConnection({(DRY_DESK, D2): desk_state(), (LIVE_DESK_RECORD, D2): desk_state()})
    return read_portfolio(
        conn.db, entries=journal(), stream=stream, roster=ROSTER, book_id=book,
        prices=FakePrices(), as_of=NOW,
    )  # fmt: skip


# ── 1. exactly those rows; BUY/SELL only on the two staged names ───────────────────────────


def _buying(html: str) -> dict[str, dict[str, str]]:
    """Every "buying next session" row's data attributes, by ISIN, in page order."""
    out: dict[str, dict[str, str]] = {}
    for tag in re.findall(r"<tr data-buying=\"[^\"]*\"[^>]*>", html):
        attrs = dict(re.findall(r'data-([a-z0-9-]+)="([^"]*)"', tag))
        out[attrs.pop("buying")] = attrs
    return out


def test_the_page_shows_the_holdings_and_the_staged_new_name_and_nothing_else(
    client: TestClient,
) -> None:
    html = _page(client, PRIMARY)
    rows = _rows(html)
    assert list(rows) == [ISIN_A, ISIN_B, ISIN_C]  # held, by ISIN; the new name is not a holding
    assert {isin: (r["action"], r["action-quantity"]) for isin, r in rows.items()} == {
        ISIN_A: ("", ""),
        ISIN_B: ("SELL", "3"),  # the TRIM shows as SELL with its quantity
        ISIN_C: ("", ""),
    }
    # Owner, 2026-10-13: a holdings row always has a quantity.
    assert all(r["held"] == "true" and r["quantity"] for r in rows.values())
    # The staged new name is in its own section: quantity to buy, CMP, day's change, est. cost.
    assert _buying(html) == {
        ISIN_D: {
            "quantity": "5",
            "cmp": "1650.00",
            "day-change-pct": "0.61",
            "estimated-cost": "8250.00",  # 5 x 1650.00, before charges
        }
    }
    assert "<h2>Buying next session (2026-10-13)</h2>" in html
    assert "₹8,250.00" in html and "an estimate before charges" in html


def _holdings_section(html: str) -> str:
    """The holdings panel's HTML: from its opening tag to the next section."""
    start = html.index('id="holdings"')
    end = html.find('id="buying"', start)
    return html[start:] if end < 0 else html[start:end]


def test_a_staged_new_buy_never_appears_in_the_holdings_table(client: TestClient) -> None:
    """Inverted check: a page listing the staged BUY among the holdings (with an empty Qty)
    fails here, for the primary and for its mirror."""
    for book in (PRIMARY, MIRROR):
        html = _page(client, book)
        holdings = _holdings_section(html)
        assert holdings and ISIN_D not in holdings
        assert ISIN_D in _buying(html)


def test_the_estimated_cost_is_quantity_times_cmp_to_the_paisa() -> None:
    closes = {D2: {**CLOSES[D2], ISIN_D: Decimal("1650.005")}, D1: CLOSES[D1]}
    conn = StubConnection({(DRY_DESK, D2): desk_state()})
    page = read_portfolio(
        conn.db, entries=journal(), stream=PortfolioStream.DRY, roster=ROSTER, book_id=MIRROR,
        prices=FakePrices(closes), as_of=NOW,
    )  # fmt: skip
    (row,) = page.rows
    assert row.estimated_cost == Decimal("82500.250") and isinstance(row.estimated_cost, Decimal)
    assert paisa(row.estimated_cost) == "82500.25"
    # A held name's SELL carries no estimate.
    assert all(r.estimated_cost is None for r in _model().rows if r.held)


def test_no_holdings_says_so_and_names_when_the_first_orders_fill(client: TestClient) -> None:
    mirror = _page(client, MIRROR)
    assert '<p id="no-holdings">No holdings yet — first orders fill on 2026-10-13.</p>' in mirror
    assert "<th>Avg price</th>" not in mirror  # no empty holdings table
    control = _page(client, CONTROL)
    assert '<p id="no-holdings">No holdings yet.</p>' in control
    assert 'id="buying"' not in control  # no staged new buy: the section is omitted


def test_yesterdays_filled_order_and_the_managers_decision_line_are_not_staged() -> None:
    """Inverted check: the D1 BUY of B filled at D2's open, and the manager's FM_DECISION BUY of
    D is a decision, not an order — counting either would show BUY 7 on B or BUY 10 on D."""
    page = _model()
    actions = {r.isin: (r.action, r.action_quantity) for r in page.rows}
    assert actions[ISIN_B] == (StagedAction.SELL, 3)
    assert actions[ISIN_D] == (StagedAction.BUY, 5)
    assert actions[ISIN_A] == (None, None) and actions[ISIN_C] == (None, None)


def test_the_symbol_is_display_only_and_the_isin_is_the_key(client: TestClient) -> None:
    html = _page(client, PRIMARY)
    for isin, symbol in SYMBOLS.items():
        assert f'data-holding="{isin}"' in html or f'data-buying="{isin}"' in html
        assert f"<td>{symbol}</td>" in html


def test_a_suspended_holding_shows_suspended_since_in_place_of_cmp_figures(
    client: TestClient,
) -> None:
    html = _page(client, PRIMARY)
    row = _rows(html)[ISIN_C]
    assert row["suspended-since"] == D0.isoformat()
    assert (row["cmp"], row["day-change"], row["profit"]) == ("—", "—", "—")
    assert row["position-day-change"] == "—"  # no day's change on a suspended name, as before
    assert row["value"] == "11601.00"  # 4 x its last close 2900.25 — what the book is marked at
    assert row["avg"] == "3000.00"
    assert f"suspended since {D0.isoformat()}" in html


# ── 2. Decimal end to end, to the paisa ────────────────────────────────────────────────────

#: Hand-computed: A 10 @ 25010.50 marked 2600.00 (prev 2550.50); B 7 @ 10535.3549 marked 1490.10
#: (prev 1510.00); C 4 @ 12000.00 suspended at 2900.25. Cash 951234.56; NAV 999266.26; the
#: previous NAV 1001000.00; opening capital 10,00,000.
EXPECTED_ROWS: Final = {
    ISIN_A: {"quantity": "10", "cmp": "2600.00", "avg": "2501.05", "day-change": "49.50",
             "position-day-change": "495.00",  # 49.50 x 10
             "day-change-pct": "1.94", "value": "26000.00", "profit": "989.50",
             "profit-pct": "3.96"},
    ISIN_B: {"quantity": "7", "cmp": "1490.10", "avg": "1505.05", "day-change": "-19.90",
             "position-day-change": "-139.30",  # -19.90 x 7: a down day
             "day-change-pct": "-1.32", "value": "10430.70", "profit": "-104.65",
             "profit-pct": "-0.99"},
}  # fmt: skip
EXPECTED_TOTALS: Final = {
    "session": "2026-10-12",
    "invested": "47545.85",  # 25010.50 + 10535.3549 + 12000.00
    "current-value": "48031.70",  # 26000.00 + 10430.70 + 11601.00
    "cash": "951234.56",
    "nav": "999266.26",
    "total-profit": "-733.74",
    "total-profit-pct": "-0.07",
    "daily-change": "-1733.74",
    "daily-change-pct": "-0.17",
    "reconciled": "true",
}


def test_every_figure_matches_the_hand_computation_to_the_paisa(client: TestClient) -> None:
    html = _page(client, PRIMARY)
    rows = _rows(html)
    for isin, expected in EXPECTED_ROWS.items():
        assert {k: rows[isin][k] for k in expected} == expected, isin
    assert _totals(html) == EXPECTED_TOTALS
    # And as the operator reads them: rupees with Indian grouping, signed changes.
    assert "₹9,99,266.26" in html and "-₹733.74 (-0.07 %)" in html
    assert "-₹1,733.74 (-0.17 %)" in html and "+₹989.50" in html


def test_the_days_change_is_shown_per_share_and_for_the_whole_position(
    client: TestClient,
) -> None:
    """Owner, 2026-10-13: both figures, the % once. Inverted check: the position figure is the
    per-share change times the quantity held — a page printing the per-share figure twice, or
    multiplying by the staged quantity, fails here."""
    html = _page(client, PRIMARY)
    assert "<th>Day's change / share</th><th>Day's change (position)</th>" in html
    assert _holdings_section(html).count("<th>Day's change %</th>") == 1
    rows = _rows(html)
    assert (rows[ISIN_A]["day-change"], rows[ISIN_A]["position-day-change"]) == ("49.50", "495.00")
    assert (rows[ISIN_B]["day-change"], rows[ISIN_B]["position-day-change"]) == (
        "-19.90",
        "-139.30",
    )
    assert "+₹495.00" in html and "-₹139.30" in html
    # The staged BUY of D (5 shares, not held) has no position change, not 5 x 10.00.
    new_row = re.search(rf'<tr data-buying="{ISIN_D}"[^>]*>(.*?)</tr>', html)
    assert new_row is not None
    assert "+0.61 %" in new_row.group(1) and "₹50.00" not in new_row.group(1)
    page = _model()
    exact = {r.isin: r.position_day_change for r in page.rows}
    assert exact[ISIN_A] == Decimal("495.00") and exact[ISIN_B] == Decimal("-139.30")
    assert exact[ISIN_C] is None and exact[ISIN_D] is None
    # The fund-level daily change stays NAV against the previous NAV, not the rows' sum.
    assert page.totals is not None and page.totals.daily_change == Decimal("-1733.74")


def test_the_model_carries_exact_decimals_never_floats() -> None:
    page = _model()
    money = [
        value
        for row in page.rows
        for name, value in row
        if name not in {"isin", "symbol", "held", "quantity", "action", "action_quantity",
                        "note", "cmp_session", "previous_session", "suspended_since"}
        and value is not None
    ]  # fmt: skip
    assert money and all(type(v) is Decimal for v in money)
    totals = page.totals
    assert totals is not None
    for name, value in totals:
        if name not in {"session", "previous_session", "positions", "reconciled"}:
            assert value is None or type(value) is Decimal, name
    # Exact, not pre-rounded: the cost basis keeps its four places until the page prints it.
    b = next(r for r in page.rows if r.isin == ISIN_B)
    assert b.cost == Decimal("10535.3549") and b.profit == Decimal("-104.6549")
    assert totals.invested == Decimal("47545.8549")


def test_a_float_is_refused_at_the_model() -> None:
    row = next(r for r in _model().rows if r.isin == ISIN_A)
    with pytest.raises(ValidationError):
        HoldingRowOut(**{**row.model_dump(), "cmp": 2600.0})


def test_a_float_close_from_the_lake_is_refused(tmp_path: Path) -> None:
    from dataplatform.status.queries import StatusQueryError
    from dataplatform.status.queries_portfolio import _decimal

    with pytest.raises(StatusQueryError, match="never a float"):
        _decimal(2600.0, "close")


def test_paisa_rounds_half_up() -> None:
    assert paisa(Decimal("-104.6549")) == "-104.65"
    assert paisa(Decimal("0.005")) == "0.01"
    assert paisa(None) == "—"


# ── 3. parity with /status/managers and the digest ─────────────────────────────────────────


def test_nav_and_holdings_equal_the_status_and_digest_reads_for_the_session() -> None:
    entries = journal()
    status = read_managers_status(entries, roster=ROSTER, as_of=NOW)
    board = build_scoreboard(ROSTER, inputs_from_journal(entries, ROSTER))
    digest = render_digest(board, D2, [])
    for book in (PRIMARY, MIRROR, CONTROL, BENCH):
        page = _model(book=book)
        line = next(b for b in status.books if b.book_id == book)
        totals = page.totals
        assert totals is not None
        assert (line.latest_session, line.nav, line.cash) == (page.session, totals.nav, totals.cash)
        assert line.positions == totals.positions
        assert f"| {book} | {line.kind} | {D2.isoformat()} | {totals.nav} |" in digest
        if book != BENCH:
            assert line.positions == sum(1 for r in page.rows if r.held)
    # The primary's rows add up to its mark: holdings + cash = NAV, to the rupee-fraction.
    primary = _model().totals
    assert primary is not None and primary.reconciled and primary.unreconciled is None
    assert primary.current_value is not None
    assert primary.current_value + primary.cash == primary.nav


def test_status_managers_and_the_page_agree_on_the_live_stream(
    tmp_path: Path, conn: StubConnection
) -> None:
    conn.desks[(LIVE_DESK_RECORD, D2)] = desk_state()
    with _client(tmp_path, conn, live=journal()) as live:
        payload = live.get("/status/managers").json()
        html = _page(live, PRIMARY)
    app.dependency_overrides.clear()
    line = next(b for b in payload["books"] if b["book_id"] == PRIMARY)
    totals = _totals(html)
    assert paisa(Decimal(line["nav"])) == totals["nav"] == "999266.26"
    assert paisa(Decimal(line["cash"])) == totals["cash"]
    assert line["positions"] == sum(r["held"] == "true" for r in _rows(html).values())


def test_rows_that_do_not_add_up_to_the_mark_are_flagged(tmp_path: Path) -> None:
    short = desk_state(positions=[{"isin": ISIN_A, "quantity": "10", "cost_basis": "25010.50"}])
    with _client(tmp_path, StubConnection({(DRY_DESK, D2): short}), dry=journal()) as live:
        html = _page(live, PRIMARY)
    app.dependency_overrides.clear()
    assert _totals(html)["reconciled"] == "false"
    assert _totals(html)["nav"] == "999266.26"  # the mark's NAV, never a recomputed one
    assert "do not add up to the book" in html


# ── 4. the stream: dry until S0, live once it has entries ──────────────────────────────────


def test_with_no_live_entries_the_page_shows_the_dry_stream_labelled_dry_run(
    client: TestClient, conn: StubConnection
) -> None:
    html = _page(client, PRIMARY)
    assert 'id="stream" data-stream="m17-dry"' in html
    assert "<strong>DRY RUN</strong>" in html and DRY_DESK in html
    assert any(params == (DRY_DESK_PATTERN, D2) for _, params in conn.queries)


def test_with_live_entries_the_page_shows_live(tmp_path: Path) -> None:
    conn = StubConnection({(LIVE_DESK_RECORD, D2): desk_state(), (DRY_DESK, D2): {"books": {}}})
    with _client(tmp_path, conn, live=journal(), dry=journal()) as live:
        html = _page(live, PRIMARY)
    app.dependency_overrides.clear()
    assert 'id="stream" data-stream="m17"' in html and "<strong>LIVE</strong>" in html
    assert "DRY RUN" not in html and LIVE_DESK_RECORD in html
    assert all(params != (DRY_DESK_PATTERN, D2) for _, params in conn.queries)
    assert _totals(html)["nav"] == "999266.26"


def test_the_stream_choice_and_the_dry_read_are_skipped_once_live() -> None:
    live, dry = journal(), journal()[:1]
    assert choose_stream(live, dry) == (PortfolioStream.LIVE, live)
    assert choose_stream([], dry) == (PortfolioStream.DRY, dry)
    sentinel = cast(Connection, object())  # touching it would raise: it must not be read
    assert m17_dry_journal_source(sentinel, cast(Settings, None), ROSTER, live) == ()


# ── 5. selection: every recorded book on its own; unknown is a 404 ─────────────────────────


def _picks(html: str) -> list[dict[str, str]]:
    """Every book picker link's attributes, in page order."""
    out = []
    for tag in re.findall(r"<a href=\"[^\"]*\"[^>]*class=\"pick[^\"]*\"[^>]*>", html):
        out.append(dict(re.findall(r'([a-z-]+)="([^"]*)"', tag)))
    return out


def test_every_recorded_book_is_one_link_grouped_by_kind(client: TestClient) -> None:
    html = _page(client)
    picks = _picks(html)
    assert [p["data-book"] for p in picks] == [book.id for book in ROSTER.books]
    assert len(picks) == 21
    for pick in picks:
        # One click: a plain GET link to the book's own URL, and htmx fetches the same URL.
        assert pick["href"] == f"/console/portfolio?book={pick['data-book']}"
        assert pick["hx-get"] == pick["href"]
    groups = re.findall(r'<div class="pick-group" data-kind="[^"]+"><h3>([^<]+)</h3>', html)
    assert groups == ["Manager", "Control", "Style", "Bench"]
    # No dropdown and no submit button: selection is the click.
    assert "<select" not in html and "<button" not in html and "<form" not in html
    picker = re.search(r'<nav id="book-picker"[^>]*>', html)
    assert picker is not None
    assert 'hx-target="#portfolio"' in picker.group(0)
    assert 'hx-push-url="true"' in picker.group(0)
    # The default is the first manager book, highlighted.
    current = [p["data-book"] for p in picks if "aria-current" in p]
    assert current == ["FM-SWING-BRK-10L"]
    assert 'data-book="FM-SWING-BRK-10L" data-kind="MANAGER"' in html


def test_a_managers_primary_and_mirror_share_one_line(client: TestClient) -> None:
    html = _page(client)
    line = re.search(
        r'<div class="pick-line"><span class="pick-stem mono">FM-POS-TREND</span>(.*?)</div>', html
    )
    assert line is not None
    labels = re.findall(r'<span class="mono">([^<]+)</span>', line.group(1))
    assert labels == ["10L", "1CR"]
    assert '<span class="pick-stem mono">BENCH-N500</span>' not in html


def test_the_selected_book_is_highlighted_with_aria_current(client: TestClient) -> None:
    picks = _picks(_page(client, MIRROR))
    current = [p for p in picks if p.get("aria-current") == "page"]
    assert [p["data-book"] for p in current] == [MIRROR]
    assert current[0]["class"] == "pick current"
    assert all(p["class"] == "pick" for p in picks if p["data-book"] != MIRROR)


def test_each_pick_shows_its_nav_change_and_a_staged_marker(client: TestClient) -> None:
    html = _page(client)
    picks = {p["data-book"]: p for p in _picks(html)}
    # The primary and its mirror each have an order staged on D2 for D3; nothing else does.
    assert {b for b, p in picks.items() if p["data-staged"] == "true"} == {PRIMARY, MIRROR}
    item = re.search(rf'data-book="{PRIMARY}".*?</a>', html)
    assert item is not None
    # NAV 999266.26 to the rupee; vs 1001000.00 the day before: -0.17 %.
    assert "₹9,99,266 · " in item.group(0) and "-0.17 %" in item.group(0)
    assert 'class="pick-staged"' in item.group(0)
    unmarked = re.search(rf'data-book="{UNMARKED}".*?</a>', html)
    assert unmarked is not None and "not marked" in unmarked.group(0)
    assert 'class="pick-staged"' not in unmarked.group(0)


def test_an_htmx_request_gets_the_region_and_a_normal_request_the_page(
    client: TestClient,
) -> None:
    full = client.get("/console/portfolio", params={"book": MIRROR})
    assert full.status_code == 200 and full.text.lstrip().startswith("<!doctype html>")
    assert '<div id="portfolio">' in full.text
    part = client.get("/console/portfolio", params={"book": MIRROR}, headers={"HX-Request": "true"})
    assert part.status_code == 200 and part.headers["content-type"].startswith("text/html")
    assert part.text.startswith('<div id="portfolio">')
    assert "<html" not in part.text and "<body" not in part.text and "<header" not in part.text
    # The region carries the picker, so the highlight swaps with the book.
    assert [p["data-book"] for p in _picks(part.text) if "aria-current" in p] == [MIRROR]
    assert _totals(part.text) == _totals(full.text)
    assert "HX-Request" in part.headers["vary"]
    # htmx's history restore swaps the whole body: it gets the full page.
    restore = client.get(
        "/console/portfolio",
        params={"book": MIRROR},
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
    )
    assert restore.text.lstrip().startswith("<!doctype html>")


def test_an_unknown_book_is_a_404_for_htmx_too(client: TestClient) -> None:
    response = client.get(
        "/console/portfolio", params={"book": "NOPE"}, headers={"HX-Request": "true"}
    )
    assert response.status_code == 404


def test_a_mirror_is_its_own_portfolio(client: TestClient) -> None:
    html = _page(client, MIRROR)
    assert 'data-book="FM-POS-FUND-1CR" data-kind="MANAGER"' in html
    assert _rows(html) == {}
    assert _buying(html)[ISIN_D]["quantity"] == "50"
    assert _totals(html)["nav"] == "10000000.00"


def test_the_bench_and_a_control_get_the_same_view(client: TestClient) -> None:
    bench = _page(client, BENCH)
    assert 'data-kind="BENCH"' in bench and "no instrument, no ISIN" in bench
    assert _totals(bench)["nav"] == "997993.24"
    assert _totals(bench)["daily-change"] == "-2006.76"
    control = _page(client, CONTROL)
    assert 'data-kind="CONTROL"' in control and "control for FM-POS-FUND-10L" in control
    assert "No holdings yet." in control


def test_a_book_with_no_mark_says_so(client: TestClient) -> None:
    html = _page(client, UNMARKED)
    assert f"No session has been marked yet for {UNMARKED} in the m17-dry stream." in html
    assert 'id="totals"' not in html


@pytest.mark.parametrize("book", ["NOPE", "m17-dry:FM-POS-FUND-10L", "fm-pos-fund-10l"])
def test_an_unknown_book_is_a_404(client: TestClient, book: str) -> None:
    response = client.get("/console/portfolio", params={"book": book})
    assert response.status_code == 404
    assert "no M17 book" in response.text


# ── 6. the M18.1 shared rules ──────────────────────────────────────────────────────────────


def test_the_route_is_on_the_console_router_and_read_only(client: TestClient) -> None:
    routes = [r for r in router.routes if isinstance(r, APIRoute) and r.path.endswith("portfolio")]
    assert [r.path for r in routes] == ["/console/portfolio"]
    assert set(routes[0].methods or ()) <= set(READ_METHODS)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert client.request(method, f"/console/portfolio?book={PRIMARY}").status_code == 405
    assert '<a href="/console/portfolio" class="current">Portfolio</a>' in _page(client)


def test_no_external_url_and_no_planted_secret(client: TestClient) -> None:
    for book in (None, PRIMARY, MIRROR, BENCH, "NOPE"):
        response = client.get("/console/portfolio", params=None if book is None else {"book": book})
        html = response.text
        assert PLANT not in html
        assert "http://" not in html and "https://" not in html
        for value in re.findall(r'\s(?:src|href|action)="([^"]*)"', html):
            assert value.startswith("/") and not value.startswith("//"), value


def test_a_journal_the_parse_refuses_is_reported_masked_never_with_figures(
    tmp_path: Path, conn: StubConnection
) -> None:
    broken = _mark(PRIMARY, D2, "1", "1", 0).model_copy(
        update={"payload": {"event": "BOOK_MARK", "book": PRIMARY, "record": f'{{"x": "{_PW}"}}'}}
    )
    with _client(tmp_path, conn, dry=[*journal(), broken]) as live:
        html = _page(live, PRIMARY)
    app.dependency_overrides.clear()
    assert 'id="portfolio-error"' in html and "malformed BOOK_MARK" in html
    assert PLANT not in html
    assert 'id="totals"' not in html


def test_every_timestamp_is_ist_and_the_instant_is_the_injected_clock(client: TestClient) -> None:
    html = _page(client, PRIMARY)
    stamps = re.findall(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?.{0,4}", html)
    assert stamps and all(s.endswith(" IST") for s in stamps)
    assert "as of 2026-10-13 07:30:00 IST" in html


# ── the lake reader ────────────────────────────────────────────────────────────────────────


def test_the_lake_closes_are_nse_eq_first_then_be_bz_never_bse(tmp_path: Path) -> None:
    import duckdb

    from dataplatform.store.paths import l1_partition_path

    path = l1_partition_path("prices_raw", D2, data_root=tmp_path)
    path.parent.mkdir(parents=True)
    rows = [
        (ISIN_A, "NSE", "EQ", "2600.00"), (ISIN_A, "BSE", "A", "2601.00"),
        (ISIN_B, "NSE", "BZ", "11.50"), (ISIN_B, "NSE", "BE", "12.25"),
        (ISIN_C, "BSE", "A", "99.00"), (ISIN_D, "NSE", "EQ", "0"),
    ]  # fmt: skip
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t (isin VARCHAR, exchange VARCHAR, series VARCHAR, close DECIMAL(18,4), "
        "trade_date DATE)"
    )
    con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?)", [(*r, D2) for r in rows])
    con.execute(f"COPY t TO '{path}' (FORMAT PARQUET)")
    con.close()

    prices = LakeSessionPrices(tmp_path)
    closes = prices.closes(D2, {ISIN_A, ISIN_B, ISIN_C, ISIN_D})
    assert closes == {ISIN_A: Decimal("2600.00"), ISIN_B: Decimal("12.25")}
    assert all(type(v) is Decimal for v in closes.values())
    assert prices.closes(D1, {ISIN_A}) == {}  # no partition: no close, not an error
    # The calendar skips the weekend: the session before Monday 12 Oct is Friday 9 Oct.
    assert prices.previous_session(D2) == D1
    assert prices.next_session(D2) == D3
    assert prices.next_session(D1) == D2  # Friday → Monday


# ── strategy paper books: every book is a portfolio (owner, 2026-10-14) ───────────────────────

PAPER_BOOK: Final = "momentum_v2_paper_2026_09_06"
#: The first session of November 2026 (Sunday 1 Nov): the next monthly rebalance after October's.
NOV_FIRST: Final = date(2026, 11, 2)


def _broker(cash: str, on: date, staged: Sequence[tuple[str, str, int]] = ()) -> dict[str, Any]:
    """`paper_session.book_state` as `backtest.paper_session` records it (the parts read)."""
    return {
        "broker": {
            "cash": cash, "released_ahead": "0", "current_session": on.isoformat(),
            "holdings": [], "pending": [], "receivables": [],
            "staged": [
                {"order_id": f"o{i}", "decision_date": on.isoformat(), "target_session": "x",
                 "isin": isin, "side": side, "quantity": str(quantity), "exchange": "NSE",
                 "order_type": "MARKET", "limit_price": None, "tag": "momentum"}
                for i, (isin, side, quantity) in enumerate(staged)
            ],
            "next_order_seq": str(len(staged)), "next_ledger_seq": "0",
        },
        "session_ledger": [],
    }  # fmt: skip


#: The book D1 and D2 hold: 10 A and 5 B bought on D1 with charges, out of ₹10 L opened on D0.
PAPER_HELD: Final = {
    "cash": "966938.99",
    "positions": [
        {"isin": ISIN_A, "quantity": "10", "cost_basis": "25510.75"},
        {"isin": ISIN_B, "quantity": "5", "cost_basis": "7550.26"},
    ],
}


def paper_rows(opening: str = "1000000") -> list[PaperRow]:
    """D0 rebalanced (orders staged, nothing filled), D1 filled them, D2 staged a TRIM of B and a
    BUY of D for D3. D0 recorded no accounting book (its broker cash is the opening capital)."""
    return [
        PaperRow(PAPER_BOOK, D0, reason="rebalance", rebalanced=True,
                 state=_broker(opening, D0, [(ISIN_A, "BUY", 10), (ISIN_B, "BUY", 5)])),
        PaperRow(PAPER_BOOK, D1, state=_broker("966938.99", D1), expected=PAPER_HELD),
        PaperRow(PAPER_BOOK, D2, expected=PAPER_HELD,
                 state=_broker("966938.99", D2, [(ISIN_B, "SELL", 2), (ISIN_D, "BUY", 3)])),
    ]  # fmt: skip


class PaperPrices(FakePrices):
    """The fixture's calendar, plus the turn of the month."""

    def next_session(self, session: date) -> date | None:
        return {date(2026, 10, 31): NOV_FIRST}.get(session) or super().next_session(session)


#: Worked by hand. NAV D2 = 966938.99 + 10 x 2600.00 + 5 x 1490.10 = 1000389.49; NAV D1 =
#: 966938.99 + 10 x 2550.50 + 5 x 1510.00 = 999993.99; daily change 395.50 (0.0395 %); profit
#: vs ₹10 L 389.49 (0.0389 %); invested 25510.75 + 7550.26 = 33061.01; value 26000 + 7450.50.
PAPER_TOTALS: Final = {
    "session": D2.isoformat(), "invested": "33061.01", "current-value": "33450.50",
    "cash": "966938.99", "nav": "1000389.49", "total-profit": "389.49",
    "total-profit-pct": "0.04", "daily-change": "395.50", "daily-change-pct": "0.04",
    "reconciled": "true",
}  # fmt: skip
PAPER_ROWS: Final = {
    # avg 25510.75 / 10 = 2551.075 → 2551.08; day 2600 - 2550.50 = 49.50 (1.94 %), x 10 = 495;
    # profit 26000 - 25510.75 = 489.25 (1.92 %).
    ISIN_A: {"quantity": "10", "cmp": "2600.00", "avg": "2551.08", "day-change": "49.50",
             "position-day-change": "495.00", "day-change-pct": "1.94", "value": "26000.00",
             "profit": "489.25", "profit-pct": "1.92", "action": "", "action-quantity": ""},
    # avg 1510.052 → 1510.05; day 1490.10 - 1510.00 = -19.90 (-1.32 %), x 5 = -99.50;
    # profit 7450.50 - 7550.26 = -99.76 (-1.32 %); a TRIM of 2 staged.
    ISIN_B: {"quantity": "5", "cmp": "1490.10", "avg": "1510.05", "day-change": "-19.90",
             "position-day-change": "-99.50", "day-change-pct": "-1.32", "value": "7450.50",
             "profit": "-99.76", "profit-pct": "-1.32", "action": "SELL", "action-quantity": "2"},
}  # fmt: skip


@pytest.fixture
def paper_client(tmp_path: Path) -> Iterator[TestClient]:
    conn = StubConnection({(DRY_DESK, D2): desk_state()}, paper=paper_rows())
    with _client(tmp_path, conn, dry=journal(), prices=PaperPrices()) as live:
        yield live
    app.dependency_overrides.clear()


def test_a_strategy_paper_book_is_its_own_group_and_selectable(paper_client: TestClient) -> None:
    html = _page(paper_client)
    groups = re.findall(r'<div class="pick-group" data-kind="[^"]+"><h3>([^<]+)</h3>', html)
    assert groups == ["Manager", "Control", "Style", "Bench", "Paper (strategy)"]
    group = re.search(r'<div class="pick-group" data-kind="PAPER">.*?</div></div>', html)
    assert group is not None
    assert [p["data-book"] for p in _picks(group.group(0))] == [PAPER_BOOK]
    picks = {p["data-book"]: p for p in _picks(html)}
    assert len(picks) == 22
    assert picks[PAPER_BOOK]["href"] == f"/console/portfolio?book={PAPER_BOOK}"
    assert picks[PAPER_BOOK]["hx-get"] == picks[PAPER_BOOK]["href"]

    page = _page(paper_client, PAPER_BOOK)
    current = [p["data-book"] for p in _picks(page) if p.get("aria-current") == "page"]
    assert current == [PAPER_BOOK]
    assert f'data-book="{PAPER_BOOK}" data-kind="PAPER"' in page
    assert "strategy paper book · momentum v2 (D13)" in page
    assert "opening capital ₹10,00,000.00" in page
    # The real paper stream: no DRY RUN banner, no LIVE banner — a small label.
    assert "DRY RUN" not in page and "<strong>LIVE</strong>" not in page
    assert 'data-stream="paper_session"' in page


def test_a_paper_book_renders_its_holdings_and_staged_orders_to_the_paisa(
    paper_client: TestClient,
) -> None:
    html = _page(paper_client, PAPER_BOOK)
    rows = _rows(html)
    assert list(rows) == [ISIN_A, ISIN_B]  # D is staged, not held: never in the holdings
    for isin, expected in PAPER_ROWS.items():
        assert {k: rows[isin][k] for k in expected} == expected, isin
    assert _buying(html) == {
        ISIN_D: {"quantity": "3", "cmp": "1650.00", "day-change-pct": "0.61",
                 "estimated-cost": "4950.00"},
    }  # fmt: skip
    assert f"Buying next session ({D3.isoformat()})" in html
    assert _totals(html) == PAPER_TOTALS
    assert "<td>RELIANCE</td>" in html and "<td>HDFCBANK</td>" in html


def test_a_paper_books_nav_and_cash_are_its_latest_paper_session_record() -> None:
    conn = StubConnection({}, paper=paper_rows())
    page = read_portfolio(
        conn.db, entries=journal(), stream=PortfolioStream.DRY, roster=ROSTER,
        book_id=PAPER_BOOK, prices=PaperPrices(), as_of=NOW,
    )  # fmt: skip
    totals = page.totals
    assert totals is not None and page.session == D2 and page.stream is PortfolioStream.PAPER
    assert totals.cash == Decimal("966938.99")  # D2's expected_book cash, exact
    assert totals.current_value is not None
    assert totals.nav == totals.cash + totals.current_value == Decimal("1000389.49")
    assert type(totals.nav) is Decimal and totals.positions == 2
    assert totals.previous_session == D1 and totals.previous_nav == Decimal("999993.99")
    assert page.paper is not None
    assert (page.paper.last_rebalance, page.paper.next_rebalance) == (D0, NOV_FIRST)
    assert page.desk_record == PAPER_BOOK and page.missing == []


def test_the_opening_capital_is_read_from_the_first_session_never_assumed() -> None:
    conn = StubConnection({}, paper=paper_rows(opening="2000000"))
    page = read_portfolio(
        conn.db, entries=journal(), stream=PortfolioStream.DRY, roster=ROSTER,
        book_id=PAPER_BOOK, prices=PaperPrices(), as_of=NOW,
    )  # fmt: skip
    assert page.book.opening_capital_inr == Decimal("2000000")
    assert page.totals is not None
    assert page.totals.total_profit == Decimal("1000389.49") - Decimal("2000000")


def test_the_picker_marks_a_paper_book_with_an_order_staged(tmp_path: Path) -> None:
    # Through D2 an order is staged for D3; through D1 nothing is (D0's orders filled on D1).
    # Each glance carries the book's NAV to the rupee and its change on the session before.
    cases = ((paper_rows(), "true", "₹10,00,389 · "), (paper_rows()[:2], "false", "₹9,99,994 · "))
    for rows, staged, nav in cases:
        conn = StubConnection({(DRY_DESK, D2): desk_state()}, paper=rows)
        with _client(tmp_path, conn, dry=journal(), prices=PaperPrices()) as live:
            html = _page(live, PRIMARY)
        app.dependency_overrides.clear()
        pick = next(p for p in _picks(html) if p["data-book"] == PAPER_BOOK)
        assert pick["data-staged"] == staged
        item = re.search(rf'data-book="{PAPER_BOOK}".*?</a>', html)
        assert item is not None
        assert ('class="pick-staged"' in item.group(0)) is (staged == "true")
        assert nav in item.group(0)
    first = re.search(rf'data-book="{PAPER_BOOK}".*?</a>', _paper_page_html(tmp_path))
    assert first is not None and "+0.04 %" in first.group(0)


def _paper_page_html(tmp_path: Path) -> str:
    conn = StubConnection({}, paper=paper_rows())
    with _client(tmp_path, conn, dry=journal(), prices=PaperPrices()) as live:
        html = _page(live, PAPER_BOOK)
    app.dependency_overrides.clear()
    return html


def test_a_paper_book_with_no_holdings_says_so_with_its_rebalance_dates(tmp_path: Path) -> None:
    """What the live book records today: ₹10 L cash, no position, nothing staged."""
    flat = {"cash": "1000000", "positions": []}
    rows = [
        PaperRow(PAPER_BOOK, D0, reason="rebalance", rebalanced=True,
                 state=_broker("1000000", D0)),
        PaperRow(PAPER_BOOK, D1, state=_broker("1000000", D1), expected=flat),
        PaperRow(PAPER_BOOK, D2, state=_broker("1000000", D2), expected=flat),
    ]  # fmt: skip
    conn = StubConnection({}, paper=rows)
    with _client(tmp_path, conn, dry=journal(), prices=PaperPrices()) as live:
        html = _page(live, PAPER_BOOK)
    app.dependency_overrides.clear()
    assert 'id="no-holdings">No holdings yet.</p>' in html
    assert _rows(html) == {} and _buying(html) == {}
    assert 'data-last-rebalance="2026-10-07"' in html
    assert (
        f'next monthly rebalance <span id="next-rebalance">{NOV_FIRST}</span> (scheduled)' in html
    )
    totals = _totals(html)
    assert (totals["nav"], totals["cash"], totals["total-profit"]) == ("1000000.00",) * 2 + (
        "0.00",
    )
    assert (totals["invested"], totals["daily-change"]) == ("0.00", "0.00")


def test_a_red_session_after_the_last_decided_one_is_named(tmp_path: Path) -> None:
    rows = [*paper_rows(), PaperRow(PAPER_BOOK, D3, outcome="SKIPPED_DATA_RED", reason="red")]
    conn = StubConnection({}, paper=rows)
    page = read_portfolio(
        conn.db, entries=journal(), stream=PortfolioStream.DRY, roster=ROSTER,
        book_id=PAPER_BOOK, prices=PaperPrices(), as_of=NOW,
    )  # fmt: skip
    assert page.session == D2 and page.paper is not None and page.paper.sessions == 4
    assert page.missing and "SKIPPED_DATA_RED" in page.missing[0]


def test_an_unknown_book_is_still_a_404_with_paper_books_recorded(tmp_path: Path) -> None:
    conn = StubConnection({}, paper=paper_rows())
    with _client(tmp_path, conn, dry=journal(), prices=PaperPrices()) as live:
        for book in ("momentum_v2_paper_2026_09_07", "m17_dry2_fund_managers", PAPER_BOOK.upper()):
            assert live.get("/console/portfolio", params={"book": book}).status_code == 404
    app.dependency_overrides.clear()


def test_a_recon_break_session_is_shown_as_left_and_named() -> None:
    last = paper_rows()[-1]
    rows = [*paper_rows()[:-1], PaperRow(**{**last.__dict__, "outcome": "RECON_BREAK",
                                            "reason": "recon_break"})]  # fmt: skip
    conn = StubConnection({}, paper=rows)
    page = read_portfolio(
        conn.db, entries=journal(), stream=PortfolioStream.DRY, roster=ROSTER,
        book_id=PAPER_BOOK, prices=PaperPrices(), as_of=NOW,
    )  # fmt: skip
    assert page.session == D2 and page.totals is not None
    assert page.missing and "RECON_BREAK" in page.missing[0]
