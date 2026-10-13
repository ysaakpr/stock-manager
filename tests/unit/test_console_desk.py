"""M18.3: the console's Desk page — offline, over one in-memory fixture served to the real app.

The fixture is a stub connection holding `paper_session`, `order_`, `decision_journal` (the
broker-session interlock's AUTH_REQUIRED) and `token_usage` rows, kill-switch state files under a
tmp lake written by the real `execution.kill_switch.KillSwitch`, a scripted M17 run's journal
(`tests.unit.test_fm_scoreboard._Run`, the same run `/status/managers` is tested over) and a frozen
clock. `/status/paper`, `/status/managers` and `/console/desk` are asked about the *same* rows
through the *same* dependency overrides. The acceptance criteria, one block each:

1. the paper and managers panels match `/status/paper` and `/status/managers` (parity);
2. a kill switch ENGAGED and a broker AUTH_REQUIRED render as unmistakable red banners — and an
   armed switch and a clear session do not (the inverted check);
3. cost totals are Decimal end to end, and a float anywhere in the path fails;
4. the M18.1 shared rules hold on this page: read-only, no control, no external URL, IST
   labelled, no planted credential rendered (the M18.1 suite walks this route too).

The real SQL is exercised against Postgres in `tests/integration/test_console_desk.py`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

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
from dataplatform.status.console import desk, desk_render
from dataplatform.status.console.desk_render import render_desk
from dataplatform.status.models import (
    ManagerDecisionOut,
    ManagersOut,
    SuspendedHoldingOut,
)
from dataplatform.status.models_desk import (
    BrokerSessionState,
    ModelCostLineOut,
    ModelCostOut,
    OrderLineOut,
)
from dataplatform.status.queries import StatusQueryError
from dataplatform.status.queries_desk import (
    model_cost_since,
    read_broker_session,
    read_model_cost,
    read_orders,
    read_paper_positions,
)
from dataplatform.store.db import Connection
from execution.kill_switch import KillSwitch, TripSource, kill_switch_path
from tests.unit.test_fm_scoreboard import RATIONALE_MARKER, _persisted, _Run

NOW: Final = datetime(2026, 10, 12, 20, 0, tzinfo=IST)

#: Every planted secret carries this marker, so "no secret leaked" is one substring check.
PLANT: Final = "PLANTEDdk"
#: Test fakes assembled from fragments, so the secret scanner does not read them as credentials.
_PW: Final = "=".join(("password", f"{PLANT}pw1"))
_TOKEN: Final = ": ".join(("token", f"{PLANT}tok2"))
_USERINFO: Final = ":".join(("kite", f"{PLANT}pw3"))

HEALTHY, BROKEN = "momentum_paper", "value_paper"
ISIN_A, ISIN_B, ISIN_C = "INE002A01018", "INE009A01021", "INE467B01029"


# ── the fixture ────────────────────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, rows: Sequence[tuple[Any, ...]]) -> None:
        self._rows = list(rows)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


def _paper_rows() -> list[tuple[Any, ...]]:
    """`paper_session` rows: (book, date, outcome, reason, recon, expected_book, orders, at)."""
    recorded = NOW.replace(hour=18, minute=55)
    good_book = {
        "cash": "125000.50",
        "positions": [
            {"isin": ISIN_B, "quantity": "40", "cost_basis": "60123.456789"},
            {"isin": ISIN_A, "quantity": "12", "cost_basis": "35000.10"},
        ],
    }
    orders = [
        {"isin": ISIN_C, "side": "BUY", "quantity": "7", "exchange": "NSE",
         "order_type": "MARKET", "limit_price": None, "tag": "MOMENTUM_REBAL"},
        {"isin": ISIN_A, "side": "SELL", "quantity": "12", "exchange": "NSE",
         "order_type": "LIMIT", "limit_price": "2950.05", "tag": None},
    ]  # fmt: skip
    return [
        (HEALTHY, date(2026, 10, 12), "COMPLETED", "rebalanced", "CLEAN", good_book, orders,
         recorded),
        (HEALTHY, date(2026, 10, 9), "COMPLETED", "held", "CLEAN", good_book, [], recorded),
        (BROKEN, date(2026, 10, 12), "RECON_BREAK", f"broker ledger mismatch {_PW}", "BREAK",
         {"cash": "9.5", "positions": []}, [], recorded),
    ]  # fmt: skip


def _ledger_rows() -> list[tuple[Any, ...]]:
    """`order_` rows in `_ORDERS_SQL` column order, newest first."""
    staged = NOW.replace(hour=19, minute=10)
    return [
        ("ord-3", "case_core_1", ISIN_A, "BUY", "MARKET", 5, None, "RAIL_BLOCKED", "SIM", "CORE",
         staged, date(2026, 10, 13), None, 0, None, None, f"cap breached; {_TOKEN}"),
        ("ord-2", "case_core_1", ISIN_B, "SELL", "LIMIT", 3, Decimal("1500.250000"), "STAGED",
         "SIM", "TACTICAL", staged - timedelta(minutes=5), date(2026, 10, 13), None, 0, None,
         None, None),
        ("ord-1", "case_core_1", ISIN_C, "BUY", "MARKET", 10, None, "EXECUTED", "SIM", "CORE",
         staged - timedelta(days=1), date(2026, 10, 12), NOW.replace(hour=9, minute=16), 10,
         Decimal("812.400000"), Decimal("21.337000"), None),
    ]  # fmt: skip


#: `token_usage` aggregated: (case, provider, model, day, calls, in, out, inr, usd, usd_calls).
COST_ROWS: Final = [
    ("FM-SWING-BRK-10L", "anthropic", "claude-opus-5-5", date(2026, 10, 12), 3, Decimal(3000),
     Decimal(600), Decimal("0.1"), Decimal("0.001234"), 3),
    ("FM-SWING-BRK-10L", "anthropic", "claude-opus-5-5", date(2026, 10, 11), 2, Decimal(2000),
     Decimal(400), Decimal("0.2"), None, 0),
    ("FM-POS-TRD-10L", "stub", "claude-sonnet-5", date(2026, 10, 11), 1, Decimal(10),
     Decimal(5), Decimal("1234567.000001"), Decimal("0.000002"), 1),
]  # fmt: skip


class StubConnection:
    """Answers the Desk's queries from in-memory rows; any other query fails the test loudly."""

    def __init__(
        self,
        *,
        paper: Sequence[tuple[Any, ...]] = (),
        unresolved: Sequence[tuple[str, str]] = (),
        ledger: Sequence[tuple[Any, ...]] = (),
        newest_journaled: date | None = None,
        auth_required: tuple[Any, ...] | None = None,
        cost: Sequence[tuple[Any, ...]] = (),
    ) -> None:
        self.paper = list(paper)
        self.unresolved = list(unresolved)
        self.ledger = list(ledger)
        self.newest_journaled = newest_journaled
        self.auth_required = auth_required
        self.cost = list(cost)
        self.cost_params: list[Sequence[Any]] = []

    @property
    def db(self) -> Connection:
        """This stub where a query function takes a connection (it answers `execute` only)."""
        return cast(Connection, self)

    def _latest(self, rows: Sequence[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
        newest: dict[str, tuple[Any, ...]] = {}
        for row in rows:
            if row[0] not in newest or row[1] > newest[row[0]][1]:
                newest[row[0]] = row
        return [newest[book] for book in sorted(newest)]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _Result:
        text = " ".join(sql.split())
        if text.startswith("SELECT DISTINCT ON (book_id) book_id, trading_date, outcome"):
            return _Result([(r[0], r[1], r[2], r[3], r[4]) for r in self._latest(self.paper)])
        if text.startswith("WITH raised AS"):
            return _Result(sorted(self.unresolved))
        if "expected_book FROM paper_session WHERE expected_book IS NOT NULL" in text:
            with_book = [r for r in self.paper if r[5] is not None]
            return _Result([(r[0], r[1], r[5]) for r in self._latest(with_book)])
        if "WHERE jsonb_array_length(orders) > 0" in text:
            placed = [r for r in self.paper if r[6]]
            return _Result([(r[0], r[1], r[7], r[6]) for r in self._latest(placed)])
        if "FROM order_ ORDER BY staged_at DESC" in text:
            return _Result(self.ledger[: params[0]])
        if text == "SELECT max(trading_date) FROM decision_journal":
            return _Result([(self.newest_journaled,)])
        if "WHERE decision = 'AUTH_REQUIRED'" in text:
            return _Result([] if self.auth_required is None else [self.auth_required])
        if "FROM token_usage" in text:
            self.cost_params.append(params)
            return _Result(self.cost)
        raise AssertionError(f"unexpected query against the stub connection: {text}")


def _auth_required(on: date) -> tuple[Any, ...]:
    return (
        on,
        datetime(2026, 10, 12, 3, 46, tzinfo=UTC),  # 09:16 IST
        f"broker API session expired: postgresql://{_USERINFO}@kite/x rejected",
        "Re-authenticate via the broker login flow. See ops/runbooks/broker-reauth.md.",
        "2026-10-09",
        "4",
    )


def _trip(lake: Path, book: str, *, reason: str, source: TripSource) -> None:
    switch = KillSwitch(kill_switch_path(lake, book), clock=FrozenClock(NOW - timedelta(hours=1)))
    switch.trip(reason=reason, source=source)


@pytest.fixture(scope="module")
def m17_run(tmp_path_factory: pytest.TempPathFactory) -> _Run:
    return _Run(tmp_path_factory.mktemp("desk_m17"))


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    """A lake whose kill-switch directory holds BROKEN's switch, tripped by its recon break."""
    root = tmp_path / "lake"
    _trip(root, BROKEN, reason=f"RECON break on 2026-10-12; {_PW}", source=TripSource.RECON)
    return root


@pytest.fixture
def conn() -> StubConnection:
    return StubConnection(
        paper=_paper_rows(),
        unresolved=[(BROKEN, "RECON:2026-10-12@" + "ab" * 8)],
        ledger=_ledger_rows(),
        newest_journaled=date(2026, 10, 12),
        auth_required=_auth_required(date(2026, 10, 12)),
        cost=COST_ROWS,
    )


def _client(lake: Path, conn: StubConnection, run: _Run, now: datetime = NOW) -> TestClient:
    settings = Settings(data_root=lake, backup_root=lake / "backups")
    entries = _persisted(run.journal.entries)
    app.dependency_overrides[api_settings_source] = lambda: settings
    app.dependency_overrides[clock_source] = lambda: FrozenClock(now)
    app.dependency_overrides[db_connection] = lambda: conn
    app.dependency_overrides[m17_journal_source] = lambda: entries
    app.dependency_overrides[m17_roster_source] = lambda: run.roster
    return TestClient(app)


@pytest.fixture
def client(lake: Path, conn: StubConnection, m17_run: _Run) -> Iterator[TestClient]:
    with _client(lake, conn, m17_run) as live:
        yield live
    app.dependency_overrides.clear()


def _rows(html: str, key: str) -> dict[str, dict[str, str]]:
    """Every ``<tr data-<key>="id" ...>``'s data attributes, by id."""
    out: dict[str, dict[str, str]] = {}
    for tag in re.findall(rf"<tr data-{key}=\"[^\"]*\"[^>]*>", html):
        attrs = dict(re.findall(r'data-([a-z0-9-]+)="([^"]*)"', tag))
        out[attrs.pop(key)] = attrs
    return out


def _desk(client: TestClient) -> str:
    response = client.get("/console/desk")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    return str(response.text)


# ── 1. parity with /status/paper and /status/managers ──────────────────────────────────────


def test_the_paper_panel_matches_status_paper(client: TestClient) -> None:
    payload = client.get("/status/paper").json()
    html = _desk(client)
    page = _rows(html, "paper-book")
    assert set(page) == {book["book_id"] for book in payload["books"]} == {HEALTHY, BROKEN}
    for book in payload["books"]:
        row = page[book["book_id"]]
        assert row["healthy"] == ("true" if book["healthy"] else "false")
        switch = book["kill_switch"]["tripped"]
        assert row["switch"] == {True: "ENGAGED", False: "ARMED", None: "UNREADABLE"}[switch]
        assert row["outcome"] == book["latest_outcome"]
        assert row["recon"] == book["latest_recon"]
        assert int(row["unresolved"]) == len(book["unresolved"])
    assert f'id="paper" data-healthy="{str(payload["healthy"]).lower()}"' in html
    # The fixture is substantive: one healthy book, one blocked by a break and a tripped switch.
    assert page[HEALTHY] == {
        "healthy": "true", "switch": "ARMED", "outcome": "COMPLETED", "recon": "CLEAN",
        "unresolved": "0",
    }  # fmt: skip
    assert page[BROKEN]["switch"] == "ENGAGED" and page[BROKEN]["recon"] == "BREAK"
    assert "RECON:2026-10-12@abababababababab" in html  # what blocks the next session


def test_the_managers_panel_matches_status_managers(client: TestClient, m17_run: _Run) -> None:
    payload = client.get("/status/managers").json()
    html = _desk(client)

    books = _rows(html, "m17-book")
    assert list(books) == [b["book_id"] for b in payload["books"]]
    for book in payload["books"]:
        nav = book["nav"]
        assert books[book["book_id"]]["nav"] == ("—" if nav is None else str(Decimal(nav)))

    scores = _rows(html, "score")
    assert {k: v["verdict"] for k, v in scores.items()} == {
        m["book_id"]: m["verdict"] for m in payload["managers"]
    }
    assert scores, "the scripted run scores its manager books"

    results = _rows(html, "manager-result")
    assert {k: v["passed-on"] for k, v in results.items()} == {
        r["manager_id"]: r["passed_on"] for r in payload["manager_results"]
    }
    assert f'data-k-of-n="{payload["k_of_n"]}"' in html

    page_decisions = re.findall(
        r'<tr data-decision="([^"]+)" data-kind="([^"]+)" data-event="([^"]*)"', html
    )
    assert page_decisions == [
        (d["book_id"], d["decision"], d["event"] or "—") for d in payload["decisions"]
    ]
    assert payload["decisions"], "the latest session journaled decisions"
    assert f"session {payload['decisions_session']}" in html
    assert len(_rows(html, "suspended")) == len(payload["suspended_holdings"])

    # The mandates are the roster `/status/managers` scores against, every book of it.
    assert list(_rows(html, "mandate")) == [book.id for book in m17_run.roster.books]
    # No rationale and no prompt reaches the page, as none reaches the endpoint.
    assert RATIONALE_MARKER not in html
    assert "prompt" not in html.lower()


def test_refusals_and_suspended_holdings_are_shown_and_flagged(
    client: TestClient, m17_run: _Run
) -> None:
    """The renderer over a ManagersOut with a rail block, a contract refusal and a suspension."""
    base = ManagersOut.model_validate(client.get("/status/managers").json())
    session = date(2026, 10, 12)
    refusal = ManagerDecisionOut(
        book_id="FM-SWING-BRK-10L", trading_date=session, decision="RAIL_BLOCK", event=None,
        isin=ISIN_A, action="BUY", target_weight="0.08", p_beat_bench=None, horizon_sessions=None,
        rails="MAX_POSITION_PCT,PARTICIPATION",
    )  # fmt: skip
    contract = ManagerDecisionOut(
        book_id="FM-SWING-BRK-10L", trading_date=session, decision="HOLD",
        event="FM_DECISION_REFUSED", isin=ISIN_B, action=None, target_weight=None,
        p_beat_bench=None, horizon_sessions=None, rails=None, refused_for="HORIZON_OUT_OF_BAND",
    )  # fmt: skip
    suspended = SuspendedHoldingOut(
        book_id="FM-SWING-BRK-10L", trading_date=session, isin=ISIN_C,
        last_trade_date=date(2026, 10, 7), sessions_suspended=3,
    )  # fmt: skip
    managers = base.model_copy(
        update={
            "decisions": [*base.decisions, refusal, contract],
            "suspended_holdings": [suspended],
        }
    )
    html = render_desk(
        paper=_paper_model(client),
        positions=[],
        managers=managers,
        mandates=[],
        broker=read_broker_session(StubConnection().db),
        orders=read_orders(StubConnection().db, as_of=NOW),
        cost=read_model_cost(StubConnection().db, as_of=NOW),
        as_of=NOW,
    )
    refused = {
        (m[0], m[1]): m[2]
        for m in re.findall(
            r'<tr data-decision="([^"]+)" data-kind="([^"]+)" data-event="[^"]*" '
            r'data-refused="([^"]+)"',
            html,
        )
    }
    assert refused[("FM-SWING-BRK-10L", "RAIL_BLOCK")] == "true"
    assert refused[("FM-SWING-BRK-10L", "HOLD")] == "true"
    assert "MAX_POSITION_PCT,PARTICIPATION" in html and "HORIZON_OUT_OF_BAND" in html
    assert '<span class="state state-FAILED">RAIL_BLOCK</span>' in html
    assert f'data-suspended="FM-SWING-BRK-10L:{ISIN_C}"' in html
    assert "held, not trading since 2026-10-07" in html
    assert "2 refused" in html


def _paper_model(client: TestClient) -> Any:
    from dataplatform.status.models import PaperOut

    return PaperOut.model_validate(client.get("/status/paper").json())


def test_positions_are_the_newest_accounting_book_per_paper_book(client: TestClient) -> None:
    html = _desk(client)
    positions = _rows(html, "position")
    assert set(positions) == {f"{HEALTHY}:{ISIN_A}", f"{HEALTHY}:{ISIN_B}"}
    assert "cash ₹125,000.50" in html
    assert "60,123.456789" in html  # the cost basis exactly as recorded
    assert "flat: no position" in html  # BROKEN's book is empty, said so


# ── 2. red banners: kill switch ENGAGED, broker AUTH_REQUIRED ───────────────────────────────


def _banner(html: str, banner_id: str) -> re.Match[str]:
    match = re.search(rf'<div class="banner (\w+)" id="{banner_id}" data-state="(\w+)">', html)
    assert match is not None, f"no {banner_id} banner"
    return match


def test_an_engaged_kill_switch_is_a_red_banner_at_the_top(client: TestClient) -> None:
    html = _desk(client)
    banner = _banner(html, "kill-switch")
    assert banner.groups() == ("bad", "ENGAGED")
    assert "<strong>KILL SWITCH ENGAGED</strong>" in html
    assert f'data-switch-book="{BROKEN}"' in html and "source RECON" in html
    assert "tripped 2026-10-12 19:00:00 IST" in html
    # Above every panel: the first thing on the page after its title.
    assert html.index('id="kill-switch"') < html.index('id="paper"') < html.index('id="managers"')


def test_an_armed_switch_is_not_red(m17_run: _Run, tmp_path: Path) -> None:
    conn = StubConnection(paper=[_paper_rows()[0]])
    with _client(tmp_path / "lake", conn, m17_run) as live:
        html = _desk(live)
    app.dependency_overrides.clear()
    assert _banner(html, "kill-switch").groups() == ("ok", "ARMED")
    assert "KILL SWITCH ENGAGED" not in html


def test_an_unreadable_switch_is_red_never_armed(m17_run: _Run, tmp_path: Path) -> None:
    lake = tmp_path / "lake"
    path = kill_switch_path(lake, HEALTHY)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with _client(lake, StubConnection(paper=[_paper_rows()[0]]), m17_run) as live:
        html = _desk(live)
    app.dependency_overrides.clear()
    assert _banner(html, "kill-switch").groups() == ("bad", "UNREADABLE")
    assert "KILL SWITCH STATE UNREADABLE" in html


def test_broker_auth_required_is_a_red_banner(client: TestClient) -> None:
    html = _desk(client)
    assert _banner(html, "broker-session").groups() == ("bad", "AUTH_REQUIRED")
    assert "<strong>BROKER AUTH REQUIRED</strong>" in html
    assert "streak since 2026-10-09" in html and "4 decisions deferred" in html
    assert "at 2026-10-12 09:16:00 IST" in html
    assert "ops/runbooks/broker-reauth.md" in html
    assert html.index('id="broker-session"') < html.index('id="paper"')


@pytest.mark.parametrize(
    ("newest", "auth", "state"),
    [
        (date(2026, 10, 12), date(2026, 10, 12), BrokerSessionState.AUTH_REQUIRED),
        (date(2026, 10, 12), date(2026, 10, 9), BrokerSessionState.CLEAR),
        (date(2026, 10, 12), None, BrokerSessionState.NO_LAPSE_RECORDED),
    ],
)
def test_the_broker_session_state_follows_the_newest_journaled_session(
    newest: date, auth: date | None, state: BrokerSessionState
) -> None:
    conn = StubConnection(
        newest_journaled=newest, auth_required=None if auth is None else _auth_required(auth)
    )
    assert read_broker_session(conn.db).state is state


def test_a_clear_broker_session_is_not_red(lake: Path, m17_run: _Run) -> None:
    conn = StubConnection(
        newest_journaled=date(2026, 10, 12), auth_required=_auth_required(date(2026, 10, 9))
    )
    with _client(lake, conn, m17_run) as live:
        html = _desk(live)
    app.dependency_overrides.clear()
    assert _banner(html, "broker-session").groups() == ("ok", "CLEAR")
    assert "BROKER AUTH REQUIRED" not in html


def test_the_desk_offers_no_control_of_any_kind(client: TestClient) -> None:
    html = _desk(client).lower()
    for control in ("<form", "<button", "<input", "hx-post", "hx-put", "hx-delete", "method="):
        assert control not in html, control
    links = re.findall(r'href="([^"]+)"', html)
    assert all(link.startswith("/console/") for link in links), links
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert client.request(method, "/console/desk").status_code == 405


# ── 3. Decimal end to end ───────────────────────────────────────────────────────────────────


def test_cost_lines_and_totals_are_decimal_and_exact(conn: StubConnection) -> None:
    cost = read_model_cost(conn.db, as_of=NOW)
    for line in cost.lines:
        assert type(line.cost_inr) is Decimal
        assert line.cost_usd is None or type(line.cost_usd) is Decimal
    assert type(cost.total_inr) is Decimal and type(cost.total_usd) is Decimal
    # 0.1 + 0.2 + 1234567.000001 — exact in Decimal; a float on the way would not be.
    assert cost.total_inr == Decimal("1234567.300001")
    assert str(cost.total_inr) == "1234567.300001"
    assert cost.total_usd == Decimal("0.001236")
    assert cost.lines[1].cost_usd is None  # a day with no USD figure is None, never $0


def test_the_page_shows_the_exact_decimal_totals(client: TestClient) -> None:
    html = _desk(client)
    assert 'data-total-inr="1234567.300001"' in html and 'data-total-usd="0.001236"' in html
    assert "Total ₹1,234,567.300001" in html
    assert "0.30000000000000004" not in html
    assert len(_rows(html, "cost")) == 3


@pytest.mark.parametrize("column", [7, 8])
def test_a_float_cost_from_the_database_is_refused(column: int) -> None:
    row: list[Any] = list(COST_ROWS[0])
    row[column] = 0.1
    with pytest.raises(StatusQueryError, match="never a float"):
        read_model_cost(StubConnection(cost=[tuple(row)]).db, as_of=NOW)


def test_a_float_cost_fails_the_page_rather_than_rendering(lake: Path, m17_run: _Run) -> None:
    row: list[Any] = list(COST_ROWS[0])
    row[7] = 0.1
    with (
        _client(lake, StubConnection(cost=[tuple(row)]), m17_run) as live,
        pytest.raises(StatusQueryError, match="never a float"),
    ):
        live.get("/console/desk")
    app.dependency_overrides.clear()


def test_the_cost_models_refuse_a_float() -> None:
    line: dict[str, Any] = {
        "case_id": "FM-X-10L", "provider": "stub", "model": "m", "day": date(2026, 10, 12),
        "calls": 1, "tokens_in": 1, "tokens_out": 1, "cost_inr": Decimal("0.1"),
        "cost_usd": None, "usd_calls": 0,
    }  # fmt: skip
    ModelCostLineOut(**line)
    with pytest.raises(ValidationError):
        ModelCostLineOut(**{**line, "cost_inr": 0.1})
    with pytest.raises(ValidationError):
        ModelCostOut(
            as_of=NOW,
            days=30,
            since=NOW,
            lines=[],
            total_inr=0.0,  # type: ignore[arg-type]
            total_usd=None,
        )


def test_a_float_in_paper_money_is_refused() -> None:
    row = list(_paper_rows()[0])
    row[5] = {"cash": 125000.5, "positions": []}
    with pytest.raises(StatusQueryError, match="never a float"):
        read_paper_positions(StubConnection(paper=[tuple(row)]).db)
    row = list(_paper_rows()[0])
    row[6] = [{"isin": ISIN_C, "side": "BUY", "quantity": "7", "limit_price": 2950.05}]
    with pytest.raises(StatusQueryError, match="never a float"):
        read_orders(StubConnection(paper=[tuple(row)]).db, as_of=NOW)


def test_no_desk_module_converts_through_float() -> None:
    from dataplatform.status import models_desk, queries_desk

    for module in (desk, desk_render, models_desk, queries_desk):
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        assert "float(" not in source, module.__name__
        assert ": float" not in source and "-> float" not in source, module.__name__


def test_the_cost_window_is_thirty_ist_days_from_the_injected_clock(
    conn: StubConnection,
) -> None:
    read_model_cost(conn.db, as_of=NOW)
    (since,) = conn.cost_params[-1]
    assert since == datetime(2026, 9, 13, 0, 0, tzinfo=IST) == model_cost_since(NOW)
    # An instant just after IST midnight is already the new IST day, though it is UTC yesterday.
    early = datetime(2026, 10, 12, 18, 40, tzinfo=UTC)  # 2026-10-13 00:10 IST
    assert model_cost_since(early) == datetime(2026, 9, 14, 0, 0, tzinfo=IST)


# ── orders ──────────────────────────────────────────────────────────────────────────────────


def test_orders_show_staged_filled_refused_and_paper_with_their_tags(client: TestClient) -> None:
    html = _desk(client)
    orders = _rows(html, "order")
    assert orders[f"order_:case_core_1:{ISIN_A}"]["state"] == "RAIL_BLOCKED"
    assert orders[f"order_:case_core_1:{ISIN_B}"]["state"] == "STAGED"
    assert orders[f"order_:case_core_1:{ISIN_C}"]["state"] == "EXECUTED"
    assert orders[f"paper_session:{HEALTHY}:{ISIN_C}"]["state"] == "PLACED"
    assert orders[f"paper_session:{HEALTHY}:{ISIN_A}"]["state"] == "PLACED"
    assert '<span class="state state-FAILED">RAIL_BLOCKED</span>' in html
    for tag in ("SIM", "CORE", "TACTICAL", "MOMENTUM_REBAL"):
        assert f'<span class="mono">{tag}</span>' in html, tag
    assert "LIMIT 1,500.250000" in html and "LIMIT 2,950.05" in html
    assert "21.337000" in html  # the executed order's costs, as recorded


def test_the_ledger_is_capped_newest_first(conn: StubConnection) -> None:
    out = read_orders(conn.db, as_of=NOW, limit=2)
    assert [line.reference for line in out.ledger] == ["ord-3", "ord-2"]
    assert all(isinstance(line, OrderLineOut) for line in out.paper)


# ── 4. the M18.1 shared rules on this page ──────────────────────────────────────────────────


def test_a_planted_credential_never_reaches_the_desk(client: TestClient) -> None:
    html = _desk(client)
    assert PLANT not in html
    # And the masked text is still there to diagnose from.
    assert "broker ledger mismatch" in html and "cap breached" in html
    assert "postgresql://***@kite/x" in html


_STAMP: Final = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?(?P<tail>.{0,4})")


def test_every_timestamp_is_ist_and_the_page_is_at_the_injected_instant(
    client: TestClient,
) -> None:
    html = _desk(client)
    stamps = [m.group(0) for m in _STAMP.finditer(html)]
    assert len(stamps) > 5
    assert all(stamp.endswith(" IST") for stamp in stamps), stamps
    assert "as of 2026-10-12 20:00:00 IST" in html


def test_no_external_url_and_the_nav_marks_the_desk(client: TestClient) -> None:
    html = _desk(client)
    assert "http://" not in html and "https://" not in html and "url(" not in html
    assert '<a href="/console/desk" class="current">Desk</a>' in html
    assert "Not built yet" not in html
