"""M5.15 — the broker-session (auth) interlock, offline.

§4.4's interlock refuses to trade on a day the data is not green; M5.15 adds its counterpart for a
day the *broker session* is not authenticated — the daily OAuth+2FA logout Indian brokers force
(NSE consolidated NNF circular INVG/73992 §8.3.2.1.8) can leave the loop holding a dead session at
market open. This suite proves the five acceptance criteria:

1. an invalid session journals `AUTH_REQUIRED` and places **zero** orders — the order path is a spy
   that raises if it is ever reached, so "no order was placed" is a fact about control flow, not a
   count that happened to be zero;
2. paper mode via a real `SimBroker` is never blocked — its session is always valid;
3. the day's staged decisions are `DEFERRED`, not dropped — one journal entry each, naming the
   instrument, and handed back to be carried to the next authenticated session;
4. the alert fires once per dead-session streak, not once per check;
5. the runbook tells the owner how to re-authenticate and what the loop does until they do.

The database is stood in for by the same recording connection the other analyst suites use — an
`INSERT ... RETURNING` echo — so the real `Journal` round-trips offline and its `AUTH_REQUIRED` /
`DEFERRED` validation rules are exercised for real. No network, no docker, no wall clock (B8, B10).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, cast

import pytest
from psycopg.types.json import Json

from analyst.journal import Actor, Decision, Journal, Sleeve
from analyst.journal.evidence import EvidenceStore
from analyst.journal.writer import _WRITE_COLUMNS
from analyst.monitor.interlock import AuthInterlock, PendingDecision
from dataplatform.clock import IST, FrozenClock
from dataplatform.store.db import Connection
from execution.broker import Broker, OrderRequest, Side
from execution.broker import SessionExpired as BrokerSessionExpired
from execution.costs import CostModel, load_rate_card
from execution.session import (
    REAUTH_INSTRUCTION,
    BrokerSessionGate,
    RecordingAuthAlerter,
    SessionStatus,
)
from execution.sim_broker import ReferenceBar, SimBroker

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

CASE_ID: Final[str] = "CASE-AUTH-1"
INFY: Final[str] = "INE009A01021"
TCS: Final[str] = "INE467B01029"

D1: Final[date] = date(2026, 8, 10)
D2: Final[date] = date(2026, 8, 11)
D3: Final[date] = date(2026, 8, 12)
D4: Final[date] = date(2026, 8, 13)
D5: Final[date] = date(2026, 8, 14)


# ── recording connection (INSERT ... RETURNING, offline) ─────────────────────────────────────────


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _RecordingConnection:
    """Echoes an insert's parameters back as the returned row, like `INSERT ... RETURNING`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[Any]]] = []
        self.next_id = 1

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> _FakeCursor:
        unwrapped = [p.obj if isinstance(p, Json) else p for p in (params or ())]
        self.calls.append((sql, unwrapped))
        if sql.lstrip().upper().startswith("INSERT"):
            row = (self.next_id, *unwrapped)
            self.next_id += 1
            return _FakeCursor([row])
        return _FakeCursor([])

    @property
    def inserts(self) -> list[list[Any]]:
        return [params for sql, params in self.calls if sql.lstrip().upper().startswith("INSERT")]


def _field(params: Sequence[Any], name: str) -> Any:
    return params[_WRITE_COLUMNS.index(name)]


def _decisions(conn: _RecordingConnection) -> list[str]:
    return [_field(p, "decision") for p in conn.inserts]


# ── gates and spies ──────────────────────────────────────────────────────────────────────────────


class _ScriptedGate:
    """An `AuthGate` that reports a dead session on a named set of days, live otherwise."""

    def __init__(self, dead_days: set[date]) -> None:
        self._dead = dead_days
        self.calls: list[date] = []

    def __call__(self, trading_date: date) -> SessionStatus:
        self.calls.append(trading_date)
        if trading_date in self._dead:
            return SessionStatus(valid=False, reason="broker session expired")
        return SessionStatus(valid=True)


class _StagingSpy:
    """Stands in for the staging coordinator's order path. Raises if reached when it must not be."""

    def __init__(self, *, explode: bool = False) -> None:
        self.explode = explode
        self.staged: list[tuple[str, str, Sleeve]] = []

    def stage(self, request: OrderRequest, *, case_id: str, sleeve: Sleeve) -> None:
        if self.explode:
            raise AssertionError("the order path ran on a dead-session day")
        self.staged.append((request.isin, case_id, sleeve))


class _FakeBroker:
    """A minimal broker for gate tests: `session_valid` returns or raises what it is told to."""

    def __init__(self, *, valid: bool = True, raises: BrokerSessionExpired | None = None) -> None:
        self._valid = valid
        self._raises = raises

    def session_valid(self) -> bool:
        if self._raises is not None:
            raise self._raises
        return self._valid


# ── builders ───────────────────────────────────────────────────────────────────────────────────


def make_journal(conn: _RecordingConnection, tmp_path: Path, when: datetime) -> Journal:
    return Journal(
        cast(Connection, conn),
        clock=FrozenClock(when),
        evidence=EvidenceStore(tmp_path / "evidence"),
    )


def make_interlock(
    gate: Any, journal: Journal, alerter: RecordingAuthAlerter, when: datetime
) -> AuthInterlock:
    return AuthInterlock(gate=gate, journal=journal, alerter=alerter, clock=FrozenClock(when))


def pending(*isins: str) -> tuple[PendingDecision, ...]:
    return tuple(
        PendingDecision(
            request=OrderRequest(isin=isin, side=Side.BUY, quantity=10),
            case_id=CASE_ID,
            sleeve=Sleeve.CORE,
        )
        for isin in isins
    )


def evening(day: date) -> datetime:
    """The EOD instant of a trading day, tz-aware — decisions are made after the close."""
    return datetime(day.year, day.month, day.day, 18, 30, tzinfo=IST)


def run_daily_loop(
    interlock: AuthInterlock,
    trading_date: date,
    decisions: Sequence[PendingDecision],
    staging: _StagingSpy,
) -> Any:
    """The daily loop's auth step: guard first; stage only if the session is live."""
    result = interlock.guard(trading_date, decisions)
    if result.session_valid:
        for decision in decisions:
            staging.stage(decision.request, case_id=decision.case_id, sleeve=decision.sleeve)
    return result


def _sim_broker() -> SimBroker:
    """A real `SimBroker` — the paper broker whose session is always valid."""

    class _Market:
        def next_session(self, after: date) -> date:
            return D2

        def reference_bar(self, isin: str, session: date) -> ReferenceBar:  # pragma: no cover
            raise AssertionError("the auth interlock never reaches a reference bar")

    return SimBroker(
        clock=FrozenClock(evening(D1)),
        cost_model=CostModel(load_rate_card()),
        market=_Market(),
        opening_cash=Decimal("1000000"),
    )


# ── acceptance 1: an invalid session journals AUTH_REQUIRED and places zero orders ──────────────


def test_invalid_session_journals_auth_required_and_no_order_path_runs(tmp_path: Path) -> None:
    conn = _RecordingConnection()
    journal = make_journal(conn, tmp_path, evening(D1))
    interlock = make_interlock(_ScriptedGate({D1}), journal, RecordingAuthAlerter(), evening(D1))
    staging = _StagingSpy(explode=True)  # any call is a test failure

    result = run_daily_loop(interlock, D1, pending(INFY), staging)

    assert result.session_valid is False
    assert staging.staged == []  # the order path was never entered
    assert Decision.AUTH_REQUIRED.value in _decisions(conn)
    # the AUTH_REQUIRED row is a SYSTEM, no-instrument, no-order entry — the SKIPPED_DATA_RED shape
    auth = conn.inserts[0]
    assert _field(auth, "actor") == Actor.SYSTEM.value
    assert _field(auth, "decision") == Decision.AUTH_REQUIRED.value
    assert _field(auth, "isin") is None
    assert _field(auth, "orders_ref") is None
    assert _field(auth, "case_id") is None


def test_a_clean_session_places_and_journals_no_auth_required(tmp_path: Path) -> None:
    conn = _RecordingConnection()
    journal = make_journal(conn, tmp_path, evening(D1))
    interlock = make_interlock(_ScriptedGate(set()), journal, RecordingAuthAlerter(), evening(D1))
    staging = _StagingSpy()

    result = run_daily_loop(interlock, D1, pending(INFY, TCS), staging)

    assert result.session_valid is True
    assert [isin for isin, _, _ in staging.staged] == [INFY, TCS]
    assert conn.inserts == []  # a live day journals nothing here


# ── acceptance 2: paper mode via SimBroker is never blocked ─────────────────────────────────────


def test_sim_broker_session_is_always_valid() -> None:
    assert _sim_broker().session_valid() is True


def test_paper_mode_via_simbroker_is_not_blocked(tmp_path: Path) -> None:
    conn = _RecordingConnection()
    journal = make_journal(conn, tmp_path, evening(D1))
    # The *same* interlock, but the gate reads a real SimBroker rather than a scripted verdict.
    gate = BrokerSessionGate(cast(Broker, _sim_broker()))
    interlock = make_interlock(gate, journal, RecordingAuthAlerter(), evening(D1))
    staging = _StagingSpy()

    result = run_daily_loop(interlock, D1, pending(INFY), staging)

    assert result.session_valid is True
    assert [isin for isin, _, _ in staging.staged] == [INFY]
    assert conn.inserts == []


# ── acceptance 3: staged decisions are deferred, not dropped ────────────────────────────────────


def test_staged_decisions_are_deferred_not_dropped(tmp_path: Path) -> None:
    conn = _RecordingConnection()
    journal = make_journal(conn, tmp_path, evening(D1))
    interlock = make_interlock(_ScriptedGate({D1}), journal, RecordingAuthAlerter(), evening(D1))

    result = interlock.guard(D1, pending(INFY, TCS))

    # nothing dropped: both decisions come back to be carried forward
    assert [d.request.isin for d in result.deferred] == [INFY, TCS]
    # one AUTH_REQUIRED umbrella entry, then one DEFERRED entry per decision, each naming its ISIN
    assert _decisions(conn) == [
        Decision.AUTH_REQUIRED.value,
        Decision.DEFERRED.value,
        Decision.DEFERRED.value,
    ]
    deferred_rows = conn.inserts[1:]
    assert [_field(r, "isin") for r in deferred_rows] == [INFY, TCS]
    for row in deferred_rows:
        assert _field(row, "actor") == Actor.EXEC.value
        assert _field(row, "sleeve") == Sleeve.CORE.value
        assert _field(row, "rationale")  # a deferral without a reason would be a journal lie


def test_a_deferred_decision_is_restaged_on_the_next_valid_session(tmp_path: Path) -> None:
    conn = _RecordingConnection()
    gate = _ScriptedGate({D1})  # D1 dead, D2 live
    alerter = RecordingAuthAlerter()
    staging = _StagingSpy()

    # Day 1: session dead — the decision is deferred, staged nowhere.
    j1 = make_journal(conn, tmp_path, evening(D1))
    interlock = AuthInterlock(
        gate=gate, journal=j1, alerter=alerter, clock=FrozenClock(evening(D1))
    )
    day1 = run_daily_loop(interlock, D1, pending(INFY), staging)
    assert day1.session_valid is False
    assert staging.staged == []

    # Day 2: session restored — the carried-forward decision is re-evaluated and staged.
    interlock.clock = FrozenClock(evening(D2))
    day2 = run_daily_loop(interlock, D2, day1.deferred, staging)
    assert day2.session_valid is True
    assert [isin for isin, _, _ in staging.staged] == [INFY]


def test_a_deferred_entry_needs_its_instrument() -> None:
    """A DEFERRED decision with no ISIN is rejected by the journal model — invariant #2."""
    from analyst.journal import JournalEntry

    with pytest.raises(ValueError, match="needs the isin"):
        JournalEntry(
            ts=evening(D1),
            trading_date=D1,
            case_id=CASE_ID,
            actor=Actor.EXEC,
            decision=Decision.DEFERRED,
            rationale="deferred",
            sleeve=Sleeve.CORE,
        )


# ── acceptance 4: the alert fires once per streak, not once per check ────────────────────────────


def test_alert_fires_once_per_streak_and_resets_when_valid(tmp_path: Path) -> None:
    conn = _RecordingConnection()
    gate = _ScriptedGate({D1, D2, D3, D5})  # D4 is the one live day
    alerter = RecordingAuthAlerter()
    journal = make_journal(conn, tmp_path, evening(D5))
    interlock = AuthInterlock(
        gate=gate, journal=journal, alerter=alerter, clock=FrozenClock(evening(D1))
    )

    fired: list[bool] = []
    for day in (D1, D2, D3, D4, D5):
        interlock.clock = FrozenClock(evening(day))
        fired.append(interlock.guard(day, pending(INFY)).alerted)

    # first dead day of each streak alerts; the rest of the streak does not; a live day resets it
    assert fired == [True, False, False, False, True]
    assert len(alerter.alerts) == 2
    assert [a.trading_date for a in alerter.alerts] == [D1, D5]
    assert [a.streak_started for a in alerter.alerts] == [D1, D5]


def test_every_dead_day_journals_even_when_it_does_not_alert(tmp_path: Path) -> None:
    conn = _RecordingConnection()
    gate = _ScriptedGate({D1, D2})
    journal = make_journal(conn, tmp_path, evening(D2))
    interlock = AuthInterlock(
        gate=gate, journal=journal, alerter=RecordingAuthAlerter(), clock=FrozenClock(evening(D1))
    )

    interlock.guard(D1, ())
    interlock.clock = FrozenClock(evening(D2))
    interlock.guard(D2, ())

    # both days journal AUTH_REQUIRED even though only the first alerts (silent != skipped)
    assert _decisions(conn) == [Decision.AUTH_REQUIRED.value, Decision.AUTH_REQUIRED.value]


def test_the_alert_and_entry_carry_the_reauth_instruction(tmp_path: Path) -> None:
    conn = _RecordingConnection()
    alerter = RecordingAuthAlerter()
    journal = make_journal(conn, tmp_path, evening(D1))
    interlock = make_interlock(_ScriptedGate({D1}), journal, alerter, evening(D1))

    interlock.guard(D1, pending(INFY))

    assert alerter.alerts[0].instruction == REAUTH_INSTRUCTION
    assert "re-auth" in REAUTH_INSTRUCTION.lower()
    assert _field(conn.inserts[0], "payload")["reauth"] == REAUTH_INSTRUCTION


# ── the read side: BrokerSessionGate maps every broker signal to a SessionStatus ────────────────


def test_gate_reports_valid_when_the_broker_session_is_live() -> None:
    status = BrokerSessionGate(cast(Broker, _FakeBroker(valid=True)))(D1)
    assert bool(status) is True


def test_gate_reports_invalid_on_a_false_session() -> None:
    status = BrokerSessionGate(cast(Broker, _FakeBroker(valid=False)))(D1)
    assert bool(status) is False
    assert status.reason


def test_gate_turns_session_expired_into_an_invalid_status_not_an_exception() -> None:
    broker = _FakeBroker(raises=BrokerSessionExpired("access token no longer valid"))
    status = BrokerSessionGate(cast(Broker, broker))(D1)
    assert bool(status) is False
    assert "access token no longer valid" in status.reason


def test_session_expired_is_a_broker_error() -> None:
    from execution.broker import BrokerError

    assert issubclass(BrokerSessionExpired, BrokerError)


# ── acceptance 5: the runbook documents the re-auth and the interlock's behaviour ───────────────


def test_runbook_tells_the_owner_how_to_reauth_and_what_the_loop_does() -> None:
    text = (REPO_ROOT / "ops" / "runbooks" / "broker-reauth.md").read_text(encoding="utf-8")
    lowered = text.lower()
    # how to re-authenticate
    assert "oauth" in lowered and "2fa" in lowered
    assert "session_valid" in text
    # what the loop does until they do
    assert "AUTH_REQUIRED" in text
    assert "defer" in lowered  # decisions deferred, not dropped
    assert "once" in lowered  # alert once per streak
