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
6. the M18.1 shared rules hold: read-only, no external URL, no secret rendered, IST.

The real SQL (the desk record, the symbols, the dry stream's read-back) is exercised against
Postgres in `tests/integration/test_console_portfolio.py`.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterator, Mapping, Sequence
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


class _Result:
    def __init__(self, rows: Sequence[tuple[Any, ...]]) -> None:
        self._rows = list(rows)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class StubConnection:
    """Answers the Portfolio page's queries from in-memory rows; anything else fails loudly."""

    def __init__(self, desks: Mapping[tuple[str, date], Mapping[str, Any]]) -> None:
        self.desks = dict(desks)
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


def test_the_page_shows_the_holdings_and_the_staged_new_name_and_nothing_else(
    client: TestClient,
) -> None:
    rows = _rows(_page(client, PRIMARY))
    assert list(rows) == [ISIN_A, ISIN_B, ISIN_C, ISIN_D]  # held by ISIN, then the new name
    assert {isin: (r["action"], r["action-quantity"]) for isin, r in rows.items()} == {
        ISIN_A: ("", ""),
        ISIN_B: ("SELL", "3"),  # the TRIM shows as SELL with its quantity
        ISIN_C: ("", ""),
        ISIN_D: ("BUY", "5"),
    }
    # The staged new name has no holding figures: only CMP, day's change and the BUY.
    new = rows[ISIN_D]
    assert new["held"] == "false" and new["quantity"] == ""
    assert (new["avg"], new["value"], new["profit"], new["profit-pct"]) == ("—",) * 4
    assert (new["cmp"], new["day-change"], new["day-change-pct"]) == ("1650.00", "10.00", "0.61")
    assert new["position-day-change"] == ""  # no position yet: the per-share change only


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
        assert f'data-holding="{isin}"' in html
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
    assert html.count("<th>Day's change %</th>") == 1
    rows = _rows(html)
    assert (rows[ISIN_A]["day-change"], rows[ISIN_A]["position-day-change"]) == ("49.50", "495.00")
    assert (rows[ISIN_B]["day-change"], rows[ISIN_B]["position-day-change"]) == (
        "-19.90",
        "-139.30",
    )
    assert "+₹495.00" in html and "-₹139.30" in html
    # The staged BUY of D (5 shares, not held) has an empty position cell, not 5 x 10.00.
    new_row = re.search(rf'<tr data-holding="{ISIN_D}"[^>]*>(.*?)</tr>', html)
    assert new_row is not None
    assert "+₹10.00" in new_row.group(1) and "₹50.00" not in new_row.group(1)
    assert '<td class="num"></td>' in new_row.group(1)
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


def test_every_recorded_book_is_selectable_grouped_by_kind(client: TestClient) -> None:
    html = _page(client)
    options = re.findall(r'<option value="([^"]+)"', html)
    assert options == [book.id for book in ROSTER.books] and len(options) == 21
    groups = re.findall(r'<optgroup label="([^"]+)">', html)
    assert groups == ["Manager", "Control", "Style", "Bench"]
    assert '<form method="get" action="/console/portfolio"' in html
    # The default is the first manager book.
    assert re.search(r'<option value="FM-SWING-BRK-10L" selected>', html)
    assert 'data-book="FM-SWING-BRK-10L" data-kind="MANAGER"' in html


def test_a_mirror_is_its_own_portfolio(client: TestClient) -> None:
    html = _page(client, MIRROR)
    assert 'data-book="FM-POS-FUND-1CR" data-kind="MANAGER"' in html
    assert {k: (v["held"], v["action"], v["action-quantity"]) for k, v in _rows(html).items()} == {
        ISIN_D: ("false", "BUY", "50")
    }
    assert _totals(html)["nav"] == "10000000.00"


def test_the_bench_and_a_control_get_the_same_view(client: TestClient) -> None:
    bench = _page(client, BENCH)
    assert 'data-kind="BENCH"' in bench and "no instrument, no ISIN" in bench
    assert _totals(bench)["nav"] == "997993.24"
    assert _totals(bench)["daily-change"] == "-2006.76"
    control = _page(client, CONTROL)
    assert 'data-kind="CONTROL"' in control and "control for FM-POS-FUND-10L" in control
    assert f"{CONTROL} has no holdings on 2026-10-12." in control


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
