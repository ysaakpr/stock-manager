"""M5.12 — order staging, daily reconciliation, and the kill switch, exercised end to end.

The three acceptance criteria of M5.12 are each a *behaviour under a fault*, not a shape a unit test
can assert offline, so this drives the real thing (§8.3.5, "reconciliation drills: inject
mismatches, assert freeze + alert"):

1. **A mismatch freezes and alerts.** Orders are staged and executed so the internal book and the
   broker agree, reconciliation confirms that, then a mismatch is *injected* and the next
   reconciliation trips the kill switch and raises exactly one alert carrying the break.
2. **The kill switch halts placement immediately and survives a restart.** Once tripped, the very
   next `stage` is refused; and a freshly constructed `KillSwitch` pointed at the same state file is
   still tripped — the "restart" a process boundary would be.
3. **Staged orders are journaled before execution and reconciled after.** The order row is written
   STAGED before the session that fills it runs, and is EXECUTED after, and it is the internal book
   built from those executions that reconciliation checks.

The order journal is the real `order_` table (0001_init.sql) via `PostgresOrderJournal`, so
"journaled" means durably written, which is why this is an integration test: it needs the docker
postgres (`make up`) and skips loudly if unreachable, as `test_journal.py`/`test_migrations.py` do.
The broker is `SimBroker` over an in-memory market, the clock is frozen (B10), and no network is
touched.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import psycopg
import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.migrate import migrate
from execution.broker import Exchange, OrderRequest, OrderStatus, Side
from execution.costs import CostModel, load_rate_card
from execution.kill_switch import KillSwitch, TradingHaltedError, TripSource
from execution.recon import BreakKind, Reconciler, RecordingAlerter
from execution.sim_broker import (
    FillPolicy,
    NoReferenceBarError,
    ReferenceBar,
    ReferencePrice,
    SimBroker,
)
from execution.staging import InternalBook, PostgresOrderJournal, StagingCoordinator

pytestmark = pytest.mark.integration

#: Pid-suffixed so concurrent build agents do not drop each other's scratch DB (cf test_migrations).
SCRATCH_DB: Final = f"trading_m5_12_recon_{os.getpid()}"

CASE_ID: Final = "AI_ROBOTICS"
INFY: Final = "INE009A01021"
TCS: Final = "INE467B01029"

DECISION_DAY: Final = date(2024, 1, 1)
S1: Final = date(2024, 1, 2)
S2: Final = date(2024, 1, 3)
S3: Final = date(2024, 1, 4)
DECIDED_AT: Final = datetime(2024, 1, 1, 19, 30, tzinfo=IST)

OPENING_CASH: Final = Decimal("10000000")


# ── the in-memory market (offline stand-in for M4.1, as in test_sim_broker) ─────────────────────


class InMemoryMarket:
    """A `SessionMarket` from a session list and a bar table."""

    def __init__(self, sessions: list[date], bars: dict[tuple[str, date], ReferenceBar]) -> None:
        self._sessions = sorted(sessions)
        self._bars = bars

    def next_session(self, after: date) -> date:
        for session in self._sessions:
            if session > after:
                return session
        raise NoReferenceBarError(f"no session after {after.isoformat()}")

    def reference_bar(self, isin: str, session: date) -> ReferenceBar:
        try:
            return self._bars[(isin, session)]
        except KeyError:
            raise NoReferenceBarError(f"no bar for {isin} on {session.isoformat()}") from None


def _bar(isin: str, session: date, *, open_: str) -> ReferenceBar:
    return ReferenceBar(
        isin=isin,
        session=session,
        exchange=Exchange.NSE,
        open=Decimal(open_),
        vwap=Decimal(open_),
        traded_value=Decimal("100000000"),
    )


# ── database fixtures (the scratch-DB pattern from test_journal.py) ──────────────────────────────


def _settings_for(dbname: str) -> Settings:
    return Settings(database_url=with_dbname(Settings().database_url, dbname))


@pytest.fixture(scope="session")
def recon_settings() -> Iterator[Settings]:
    """An empty scratch database with the schema applied, dropped at the end of the session."""
    admin = _settings_for("postgres")
    try:
        conn = connect(admin, autocommit=True)
    except psycopg.OperationalError as error:  # pragma: no cover - environment, not logic
        pytest.skip(f"postgres is not reachable — run `make up` first: {error}")
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{SCRATCH_DB}"')
    finally:
        conn.close()

    scratch = _settings_for(SCRATCH_DB)
    migrate(scratch, clock=FrozenClock(DECIDED_AT))
    yield scratch

    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture
def conn(recon_settings: Settings) -> Iterator[Connection]:
    """A connection whose transaction is rolled back after the test, with case + securities seeded.

    `order_` references `case_` and `security_master`, so those rows must exist before an order can
    be staged. Rolled back rather than cleaned up so each test starts from the same seeded state.
    """
    with connection(recon_settings) as live:
        live.execute(
            "INSERT INTO case_ (case_id, title, state, created_at, updated_at) "
            "VALUES (%s, 'M5.12 fixture', 'ACTIVE', %s, %s) ON CONFLICT DO NOTHING",
            (CASE_ID, DECIDED_AT, DECIDED_AT),
        )
        for isin, name in ((INFY, "Infosys"), (TCS, "TCS")):
            live.execute(
                "INSERT INTO security_master (isin, name, primary_exchange, status, "
                "first_seen_date, created_at, updated_at) "
                "VALUES (%s, %s, 'NSE', 'ACTIVE', %s, %s, %s) ON CONFLICT DO NOTHING",
                (isin, name, DECISION_DAY, DECIDED_AT, DECIDED_AT),
            )
        try:
            yield live
        finally:
            live.rollback()


# ── the assembled system under test ─────────────────────────────────────────────────────────────


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(DECIDED_AT)


@pytest.fixture
def market() -> InMemoryMarket:
    bars = {
        (INFY, S1): _bar(INFY, S1, open_="100"),
        (TCS, S1): _bar(TCS, S1, open_="200"),
        (INFY, S2): _bar(INFY, S2, open_="100"),
    }
    return InMemoryMarket([S1, S2, S3], bars)


@pytest.fixture
def broker(clock: FrozenClock, market: InMemoryMarket) -> SimBroker:
    return SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card()),
        market=market,
        opening_cash=OPENING_CASH,
        policy=FillPolicy(reference=ReferencePrice.OPEN),
    )


@pytest.fixture
def kill_switch(tmp_path: Path, clock: FrozenClock) -> KillSwitch:
    return KillSwitch(tmp_path / "killswitch.json", clock=clock)


@pytest.fixture
def book() -> InternalBook:
    return InternalBook(opening_cash=OPENING_CASH)


@pytest.fixture
def coordinator(
    broker: SimBroker,
    kill_switch: KillSwitch,
    conn: Connection,
    book: InternalBook,
    clock: FrozenClock,
) -> StagingCoordinator:
    journal = PostgresOrderJournal(conn, broker_tag="SIM", clock=clock)
    return StagingCoordinator(
        broker=broker, kill_switch=kill_switch, journal=journal, book=book, clock=clock
    )


@pytest.fixture
def alerter() -> RecordingAlerter:
    return RecordingAlerter()


@pytest.fixture
def reconciler(
    broker: SimBroker,
    book: InternalBook,
    kill_switch: KillSwitch,
    alerter: RecordingAlerter,
    clock: FrozenClock,
) -> Reconciler:
    return Reconciler(
        broker=broker, book=book, kill_switch=kill_switch, alerter=alerter, clock=clock
    )


# ── acceptance 3: staged orders journaled before execution, reconciled after ─────────────────────


def test_staged_order_is_journaled_before_execution_and_reconciled_after(
    coordinator: StagingCoordinator, conn: Connection, reconciler: Reconciler, clock: FrozenClock
) -> None:
    """A staged order is written STAGED before its session runs, EXECUTED after, then reconciles."""
    journal = coordinator.journal
    assert isinstance(journal, PostgresOrderJournal)

    staged = coordinator.stage(
        OrderRequest(isin=INFY, side=Side.BUY, quantity=10),
        case_id=CASE_ID,
        sleeve="TACTICAL",
    )

    # Journaled BEFORE the target session has been executed — the row is durably STAGED already.
    assert journal.state_of(staged.order_uid) == "STAGED"
    assert staged.staged_for_date == S1

    executed = coordinator.execute(S1)
    assert len(executed) == 1
    assert executed[0].state is OrderStatus.COMPLETE
    assert journal.state_of(staged.order_uid) == "EXECUTED"

    # Reconciled after: the book built from the fill agrees with the broker, so nothing freezes.
    result = reconciler.reconcile(S1)
    assert result.ok
    assert not result.froze
    assert not reconciler.kill_switch.is_tripped


# ── acceptance 1: an injected position mismatch freezes trading and alerts ────────────────────────


def test_injected_position_mismatch_freezes_and_alerts(
    coordinator: StagingCoordinator,
    reconciler: Reconciler,
    alerter: RecordingAlerter,
    book: InternalBook,
    kill_switch: KillSwitch,
) -> None:
    """The drill: after a clean session, inject a book/broker divergence; assert freeze + alert."""
    coordinator.stage(
        OrderRequest(isin=INFY, side=Side.BUY, quantity=10), case_id=CASE_ID, sleeve="TACTICAL"
    )
    coordinator.execute(S1)
    assert reconciler.reconcile(S1).ok  # book and broker agree first

    # Inject a mismatch: the platform's book now believes it holds 999 shares the broker never
    # filled — a corporate action missed, a phantom fill, a manual broker trade. Recon catches it.
    book.force_set_quantity(INFY, 999, exchange=Exchange.NSE)

    result = reconciler.reconcile(S1)

    assert not result.ok
    assert result.froze
    assert kill_switch.is_tripped
    assert kill_switch.state.source is TripSource.RECON

    # Exactly one alert, naming the position break on the diverged ISIN.
    assert len(alerter.alerts) == 1
    (alert,) = alerter.alerts
    position_breaks = [b for b in alert.breaks if b.kind is BreakKind.POSITION]
    assert [b.isin for b in position_breaks] == [INFY]
    assert position_breaks[0].expected == Decimal("999")
    assert position_breaks[0].actual == Decimal("10")


def test_injected_cash_mismatch_freezes_and_alerts(
    coordinator: StagingCoordinator,
    reconciler: Reconciler,
    alerter: RecordingAlerter,
    kill_switch: KillSwitch,
    broker: SimBroker,
) -> None:
    """A cash divergence is as much a break as a position one — recon compares the ledger too."""
    coordinator.stage(
        OrderRequest(isin=INFY, side=Side.BUY, quantity=10), case_id=CASE_ID, sleeve="TACTICAL"
    )
    coordinator.execute(S1)
    assert reconciler.reconcile(S1).ok

    # Place and fill an unrecorded order straight on the broker — the coordinator's book never sees
    # it, so the broker's cash drops below what the book believes: a cash break.
    broker.place(OrderRequest(isin=TCS, side=Side.BUY, quantity=5))
    broker.execute_session(S1)

    result = reconciler.reconcile(S1)
    assert result.froze
    assert kill_switch.is_tripped
    assert any(b.kind is BreakKind.CASH for b in result.breaks)
    assert len(alerter.alerts) == 1


# ── acceptance 2: the kill switch halts placement immediately and survives a restart ──────────────


def test_kill_switch_halts_placement_immediately(
    coordinator: StagingCoordinator, kill_switch: KillSwitch
) -> None:
    """Once tripped, the very next placement is refused before it can reach the broker."""
    kill_switch.trip(reason="drill: manual halt", source=TripSource.MANUAL)

    with pytest.raises(TradingHaltedError):
        coordinator.stage(
            OrderRequest(isin=INFY, side=Side.BUY, quantity=10),
            case_id=CASE_ID,
            sleeve="TACTICAL",
        )


def test_kill_switch_state_survives_restart(tmp_path: Path, clock: FrozenClock) -> None:
    """A fresh switch on the same state file is still tripped — the restart property."""
    path = tmp_path / "killswitch.json"
    first = KillSwitch(path, clock=clock)
    first.trip(reason="recon break on 2024-01-02", source=TripSource.RECON)

    # A new process would build a new object from the same file; it must read the trip back.
    restarted = KillSwitch(path, clock=clock)
    assert restarted.is_tripped
    assert restarted.state.source is TripSource.RECON
    assert restarted.state.reason == "recon break on 2024-01-02"
    assert restarted.state.tripped_at is not None

    # And an explicit reset re-arms it, again durably.
    restarted.reset(note="cause understood, book corrected")
    assert not KillSwitch(path, clock=clock).is_tripped


def test_recon_trip_blocks_the_coordinator(
    coordinator: StagingCoordinator,
    reconciler: Reconciler,
    book: InternalBook,
) -> None:
    """End to end: a recon break freezes, and the freeze is what stops the next day's staging."""
    coordinator.stage(
        OrderRequest(isin=INFY, side=Side.BUY, quantity=10), case_id=CASE_ID, sleeve="TACTICAL"
    )
    coordinator.execute(S1)
    book.force_set_quantity(INFY, 5, exchange=Exchange.NSE)

    assert reconciler.reconcile(S1).froze

    # The next session's decision cannot stage anything while the account is frozen.
    with pytest.raises(TradingHaltedError):
        coordinator.stage(
            OrderRequest(isin=TCS, side=Side.BUY, quantity=1), case_id=CASE_ID, sleeve="TACTICAL"
        )
