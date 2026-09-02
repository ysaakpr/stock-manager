"""M4.8 — the replay engine (X2) and its determinism harness (EXECUTION_PLAN §7, §8.3.3).

The three acceptance criteria, each a section below:

1. **Byte-identical.** Two replays of the same market, sessions and policy produce a journal and a
   final book that are equal *byte-for-byte*, not merely value-equal — the §8.3.3 determinism claim
   the reference case (M4.9) and CI both stand on. The policy here really trades: it reads a
   point-in-time price cross-section, allocates a SIP instalment into whole shares (M4.7), then
   stages the buys, so the run has a real journal and a non-empty book to be identical *about*.

2. **No strategy in the engine.** The engine takes a policy object and carries out its intent; it
   holds no decision of its own. Shown two ways: swapping the policy while every other input is held
   fixed changes the output (a real allocating policy and a do-nothing one diverge), and the
   do-nothing policy still yields one journalled heartbeat per session — the engine's own
   contribution is bookkeeping (invariant #9), never a trade.

3. **Automatic point-in-time scope.** Each session's context carries ``as_of`` equal to that
   session, set by the engine, and it is the only surface the policy reads data through. A policy
   that reaches for a not-yet-knowable figure does not get a quietly shortened answer — the guard
   raises (invariant #7), on the engine's per-session scope, without the policy needing to
   filter.

Nothing here needs Postgres: the journal the engine *produces* (the value in ``ReplayResult``) is
what determinism is asserted over, and it is the same with or without a database attached. A final,
skippable section drives the engine against a real scratch database to show the same entries land in
the append-only ``decision_journal`` (invariant #9), matching the pattern of ``test_journal.py``.

The market is in memory and the clock is frozen (B10), so the offline sections are deterministic by
construction — which is precisely the property under test.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import psycopg
import pytest

from analyst.journal import (
    Actor,
    Decision,
    EvidenceBundle,
    EvidenceItem,
    EvidenceKind,
    EvidenceStore,
    Journal,
    JournalEntry,
    JournalFilter,
    Sleeve,
)
from backtest.replay import (
    Policy,
    ReplayEngine,
    ReplayError,
    ReplayResult,
    SessionContext,
    SessionDecision,
)
from backtest.sip import simulate_sip_instalment
from dataplatform.clock import FrozenClock
from dataplatform.config import Settings
from dataplatform.query.pit import Dataset, PitError
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.migrate import migrate
from execution.broker import Exchange, OrderRequest, Side
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import NoReferenceBarError, ReferenceBar, SimBroker

# ── fixtures shared by every section: two names, a five-session window, a price path ──────────────

INFY: Final[str] = "INE009A01021"
TCS: Final[str] = "INE467B01029"

SESSIONS: Final[tuple[date, ...]] = (
    date(2024, 1, 2),
    date(2024, 1, 3),
    date(2024, 1, 4),
    date(2024, 1, 5),
    date(2024, 1, 8),
)

#: The trading calendar the market knows — one session past the replay window, so an order the
#: policy stages on the last replayed session has a next session to be staged for (it never fills
#: within the window; the book excludes it, deterministically). A real calendar outruns a backtest.
CALENDAR: Final[tuple[date, ...]] = (*SESSIONS, date(2024, 1, 9))

TARGETS: Final[dict[str, Decimal]] = {INFY: Decimal("0.6"), TCS: Decimal("0.4")}
INSTALMENT: Final[Decimal] = Decimal("500000")
OPENING_CASH: Final[Decimal] = Decimal("100000000")

#: A per-session close for each name — the point-in-time figure the policy allocates against, and
#: the reference the broker fills at. Deliberately moving, so the whole-share allocation shifts.
CLOSES: Final[dict[tuple[str, date], Decimal]] = {
    (INFY, SESSIONS[0]): Decimal("1500"),
    (TCS, SESSIONS[0]): Decimal("3600"),
    (INFY, SESSIONS[1]): Decimal("1520"),
    (TCS, SESSIONS[1]): Decimal("3550"),
    (INFY, SESSIONS[2]): Decimal("1490"),
    (TCS, SESSIONS[2]): Decimal("3620"),
    (INFY, SESSIONS[3]): Decimal("1535"),
    (TCS, SESSIONS[3]): Decimal("3580"),
    (INFY, SESSIONS[4]): Decimal("1550"),
    (TCS, SESSIONS[4]): Decimal("3500"),
}


@dataclass(frozen=True, slots=True)
class PriceRecord:
    """One name's close on one session, tagged with when it became knowable (invariant #7).

    ``knowable_date`` is the session itself: an EOD close for session S is knowable on S. The replay
    guard checks each record against the session's ``as_of``, so a record dated to a future session
    is a leak the guard must catch.
    """

    isin: str
    session: date
    close: Decimal
    knowable_date: date


class InMemoryPitStore:
    """A point-in-time price store the policy queries per session — the offline stand-in for D4.

    ``cross_section`` builds the dataset a correct policy asks for: only the rows knowable on or
    before ``as_of``, declared with their ``knowable_date`` so the guard can prove there is no leak.
    ``leaking_cross_section`` builds the dataset a *buggy* policy would — one reaching a
    session ahead — so the test can show the guard raising rather than silently trimming.
    """

    def __init__(self, closes: dict[tuple[str, date], Decimal]) -> None:
        self._records = tuple(
            PriceRecord(isin=isin, session=session, close=close, knowable_date=session)
            for (isin, session), close in closes.items()
        )

    def cross_section(self, as_of: date) -> Dataset[PriceRecord]:
        """The closes for session ``as_of`` — scoped to knowable data, as a PIT query should be."""
        rows = [record for record in self._records if record.session == as_of]
        return Dataset.declaring(
            f"closes@{as_of.isoformat()}", rows, knowable_date=lambda record: record.knowable_date
        )

    def leaking_cross_section(self, as_of: date) -> Dataset[PriceRecord]:
        """Every close from ``as_of`` on — including sessions not yet knowable. A bug on purpose."""
        rows = [record for record in self._records if record.session >= as_of]
        return Dataset.declaring(
            f"leak@{as_of.isoformat()}", rows, knowable_date=lambda record: record.knowable_date
        )


class InMemoryMarket:
    """A ``SessionMarket`` over the fixture calendar and closes — the offline stand-in for M4.1.

    The fill reference is the session's close for every name; liquidity is uniform and deep, so the
    slippage term is the same each fill and the run's determinism is not an artefact of a flat
    market.
    """

    def __init__(self, sessions: tuple[date, ...], closes: dict[tuple[str, date], Decimal]) -> None:
        self._sessions = sorted(sessions)
        self._closes = closes

    def next_session(self, after: date) -> date:
        for session in self._sessions:
            if session > after:
                return session
        raise NoReferenceBarError(f"no session after {after.isoformat()}")

    def reference_bar(self, isin: str, session: date) -> ReferenceBar:
        close = self._closes.get((isin, session))
        if close is None:
            raise NoReferenceBarError(f"no bar for {isin} on {session.isoformat()}")
        return ReferenceBar(
            isin=isin,
            session=session,
            exchange=Exchange.NSE,
            open=close,
            vwap=close,
            traded_value=Decimal("100000000"),
        )


# ── policies under replay ────────────────────────────────────────────────────────────────────────


class SipPolicy:
    """A deterministic SIP policy: read the closes, allocate an instalment, stage the buys.

    Reads only through the injected point-in-time context (so it cannot leak), allocates through the
    shared M4.7 allocator (so the whole-share arithmetic is the same code the SIP tests cover), and
    returns one ``BUY`` entry per name bought plus the price evidence it decided on. It reads no
    clock but the injected one, and keys only on ISIN — output is a pure function of the inputs.
    """

    def __init__(self, store: InMemoryPitStore, targets: dict[str, Decimal], instalment: Decimal):
        self._store = store
        self._targets = targets
        self._instalment = instalment

    def decide(self, ctx: SessionContext) -> SessionDecision:
        rows = ctx.pit.admit(self._store.cross_section(ctx.session))
        prices = {record.isin: record.close for record in rows}
        allocation = simulate_sip_instalment(
            instalment=self._instalment, targets=self._targets, prices=prices
        )
        orders = tuple(order.to_order_request(tag="SIP") for order in allocation.orders)
        entries = tuple(
            JournalEntry(
                ts=ctx.clock.now(),
                trading_date=ctx.session,
                actor=Actor.T0,
                decision=Decision.BUY,
                isin=order.isin,
                sleeve=Sleeve.TACTICAL,
                rationale=f"SIP: buy {order.quantity} at {order.price}",
            )
            for order in allocation.orders
        )
        evidence = _price_evidence(ctx.session, rows)
        return SessionDecision(evidence=evidence, orders=orders, entries=entries)


class NoOpPolicy:
    """A policy that looks at the same evidence and decides to do nothing.

    It returns no orders and no entries, so the engine supplies the one heartbeat per session
    invariant #9 requires. Used to prove the engine's own contribution is bookkeeping, not strategy.
    """

    def __init__(self, store: InMemoryPitStore) -> None:
        self._store = store

    def decide(self, ctx: SessionContext) -> SessionDecision:
        rows = ctx.pit.admit(self._store.cross_section(ctx.session))
        return SessionDecision(evidence=_price_evidence(ctx.session, rows))


class RecordingPolicy:
    """A no-op policy that records the ``as_of`` it was handed each session, to prove the scope."""

    def __init__(self, store: InMemoryPitStore) -> None:
        self._store = store
        self.seen: list[date] = []

    def decide(self, ctx: SessionContext) -> SessionDecision:
        self.seen.append(ctx.pit.as_of)
        rows = ctx.pit.admit(self._store.cross_section(ctx.session))
        return SessionDecision(evidence=_price_evidence(ctx.session, rows))


class LeakingPolicy:
    """A buggy policy that admits a dataset reaching past the session — the guard must stop it."""

    def __init__(self, store: InMemoryPitStore) -> None:
        self._store = store
        self.as_of_at_leak: date | None = None

    def decide(self, ctx: SessionContext) -> SessionDecision:
        self.as_of_at_leak = ctx.pit.as_of
        # Admitting a cross-section that includes later sessions is exactly the look-ahead the guard
        # exists to catch; it raises here rather than returning a filtered-but-silent answer.
        ctx.pit.admit(self._store.leaking_cross_section(ctx.session))
        raise AssertionError("unreachable: the PIT guard must have raised before this")


def _price_evidence(session: date, rows: tuple[PriceRecord, ...]) -> EvidenceBundle:
    """The price cross-section the policy saw, as an evidence bundle addressed by its content."""
    return EvidenceBundle(
        trading_date=session,
        actor=Actor.T0,
        items=tuple(
            EvidenceItem(
                kind=EvidenceKind.PRICE,
                source="L1",
                label="close",
                isin=record.isin,
                as_of=record.session,
                value=record.close,
            )
            for record in rows
        ),
    )


# ── the harness itself ───────────────────────────────────────────────────────────────────────────


def _run(policy: Policy, *, journal: Journal | None = None) -> ReplayResult:
    """One full replay of ``policy`` over the fixture window, with a fresh clock and broker.

    A fresh ``FrozenClock`` and ``SimBroker`` per call is the point: determinism means two
    independent runs — not one run read twice — produce the same bytes. The broker shares the
    engine's clock, as the engine requires.
    """
    clock = FrozenClock(SESSIONS[0])
    broker = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card()),
        market=InMemoryMarket(CALENDAR, CLOSES),
        opening_cash=OPENING_CASH,
    )
    engine = ReplayEngine(
        policy=policy, broker=broker, clock=clock, sessions=SESSIONS, journal=journal
    )
    return engine.run()


# ── acceptance #1: byte-identical journal and final book across two runs ──────────────────────────


def test_two_runs_are_byte_identical() -> None:
    """The §8.3.3 claim: same inputs → identical journal and book, byte-for-byte."""
    store = InMemoryPitStore(CLOSES)
    first = _run(SipPolicy(store, TARGETS, INSTALMENT))
    second = _run(SipPolicy(store, TARGETS, INSTALMENT))

    assert first.journal_bytes() == second.journal_bytes()
    assert first.book_bytes() == second.book_bytes()
    assert first.digest() == second.digest()


def test_the_replay_actually_traded() -> None:
    """Determinism over an empty run would be vacuous — assert the run has a journal and a book."""
    store = InMemoryPitStore(CLOSES)
    result = _run(SipPolicy(store, TARGETS, INSTALMENT))

    buys = [entry for entry in result.journal if entry.decision is Decision.BUY]
    assert buys, "the SIP policy should have staged buys"
    # Buys placed in earlier sessions have filled and settled into the book by the final session.
    assert result.book.holdings, "settled holdings should remain at the end of the replay"
    assert result.book.ledger, "fills should have posted cash-ledger lines"


def test_final_book_reflects_fills_not_just_orders() -> None:
    """The book carries filled cash movements, so its bytes truly depend on the fill model."""
    store = InMemoryPitStore(CLOSES)
    result = _run(SipPolicy(store, TARGETS, INSTALMENT))

    # Cash fell from opening: buys were filled and paid for (turnover + costs), not merely staged.
    assert result.book.cash < OPENING_CASH
    held_isins = {holding["isin"] for holding in result.book.holdings}
    assert held_isins <= {INFY, TCS}


# ── acceptance #2: the engine has no strategy of its own; the policy is injected ──────────────────


def test_swapping_the_policy_changes_the_output() -> None:
    """Same engine, same everything else — only the policy differs, and the run differs with it."""
    store = InMemoryPitStore(CLOSES)
    trading = _run(SipPolicy(store, TARGETS, INSTALMENT))
    idle = _run(NoOpPolicy(store))

    assert trading.digest() != idle.digest()
    assert trading.book.holdings  # the trading policy built a book
    assert not idle.book.holdings  # the idle policy did not — the engine placed nothing on its own


def test_a_do_nothing_policy_still_journals_one_heartbeat_per_session() -> None:
    """The engine's own contribution is invariant #9's heartbeat, never a trade."""
    store = InMemoryPitStore(CLOSES)
    result = _run(NoOpPolicy(store))

    assert len(result.journal) == len(SESSIONS)
    assert {entry.decision for entry in result.journal} == {Decision.HEARTBEAT}
    assert [entry.trading_date for entry in result.journal] == list(SESSIONS)
    # Every heartbeat names the evidence it was made on (invariant #9): no bare "process was alive".
    assert all(entry.evidence_snapshot_ref is not None for entry in result.journal)


def test_two_different_policies_diverge_through_the_same_engine() -> None:
    """A different instalment is a different strategy; the identical engine reproduces the gap."""
    store = InMemoryPitStore(CLOSES)
    small = _run(SipPolicy(store, TARGETS, Decimal("200000")))
    large = _run(SipPolicy(store, TARGETS, Decimal("900000")))

    assert small.journal_bytes() != large.journal_bytes()
    assert small.book_bytes() != large.book_bytes()


# ── acceptance #3: each session is point-in-time scoped to that session, automatically ────────────


def test_each_session_is_scoped_to_its_own_date() -> None:
    """The context's ``as_of`` is the session, set by the engine, for every session in order."""
    store = InMemoryPitStore(CLOSES)
    policy = RecordingPolicy(store)
    _run(policy)

    assert policy.seen == list(SESSIONS)


def test_a_policy_reaching_past_the_session_is_stopped_by_the_guard() -> None:
    """Look-ahead raises on the engine's per-session scope, with no filter for the policy."""
    store = InMemoryPitStore(CLOSES)
    policy = LeakingPolicy(store)

    with pytest.raises(PitError):
        _run(policy)

    # The guard fired on the first session's scope, which the engine had set to that session's date.
    assert policy.as_of_at_leak == SESSIONS[0]


def test_engine_rejects_a_decision_about_the_wrong_session() -> None:
    """A structural leak guard at the boundary: evidence must be about the session decided."""

    class MislabelledPolicy:
        def decide(self, ctx: SessionContext) -> SessionDecision:
            wrong = SESSIONS[1] if ctx.session == SESSIONS[0] else SESSIONS[0]
            return SessionDecision(
                evidence=EvidenceBundle(
                    trading_date=wrong,
                    actor=Actor.T0,
                    items=(
                        EvidenceItem(
                            kind=EvidenceKind.PRICE, source="L1", label="close", isin=INFY
                        ),
                    ),
                )
            )

    with pytest.raises(ReplayError):
        _run(MislabelledPolicy())


# ── the engine drives a sane order lifecycle ──────────────────────────────────────────────────────


def test_orders_are_staged_for_the_next_session() -> None:
    """A single-buy policy on the first session fills on the second (EOD → next-day execution)."""

    class OneShotPolicy:
        def __init__(self, store: InMemoryPitStore) -> None:
            self._store = store

        def decide(self, ctx: SessionContext) -> SessionDecision:
            rows = ctx.pit.admit(self._store.cross_section(ctx.session))
            orders: tuple[OrderRequest, ...] = ()
            entries: tuple[JournalEntry, ...] = ()
            if ctx.session == SESSIONS[0]:
                orders = (OrderRequest(isin=INFY, side=Side.BUY, quantity=10),)
                entries = (
                    JournalEntry(
                        ts=ctx.clock.now(),
                        trading_date=ctx.session,
                        actor=Actor.T0,
                        decision=Decision.BUY,
                        isin=INFY,
                        sleeve=Sleeve.TACTICAL,
                        rationale="one-shot buy",
                    ),
                )
            return SessionDecision(
                evidence=_price_evidence(ctx.session, rows), orders=orders, entries=entries
            )

    store = InMemoryPitStore(CLOSES)
    result = _run(OneShotPolicy(store))

    # Placed on session 0, filled on session 1 at that session's reference — settled by the end.
    (holding,) = result.book.holdings
    assert holding["isin"] == INFY
    assert holding["quantity"] == "10"
    (line,) = result.book.ledger
    assert line["session"] == SESSIONS[1].isoformat()
    assert line["isin"] == INFY


# ── full journaling: entries also land in the real append-only journal (invariant #9) ────────────

SCRATCH_DB = f"trading_m4_8_replay_{os.getpid()}"


def _settings_for(dbname: str) -> Settings:
    return Settings(database_url=with_dbname(Settings().database_url, dbname))


@pytest.fixture(scope="module")
def replay_settings() -> Iterator[Settings]:
    """A scratch database with the schema applied, dropped at the end — skipped without docker."""
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
    migrate(scratch, clock=FrozenClock(SESSIONS[0]))
    yield scratch

    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture
def conn(replay_settings: Settings) -> Iterator[Connection]:
    with connection(replay_settings) as live:
        try:
            yield live
        finally:
            live.rollback()


@pytest.mark.integration
def test_full_journaling_lands_in_the_decision_journal(conn: Connection, tmp_path: Path) -> None:
    """With a Journal injected, every produced entry is also appended to `decision_journal`.

    Case-free entries (case_id is None) so the run needs no `case_` row — the point here is that the
    engine's journal and the database's agree, not the foreign key.
    """
    store = InMemoryPitStore(CLOSES)
    journal = Journal(conn, clock=FrozenClock(SESSIONS[0]), evidence=EvidenceStore(tmp_path))

    result = _run(SipPolicy(store, TARGETS, INSTALMENT), journal=journal)

    persisted = journal.count(JournalFilter())
    assert persisted == len(result.journal)
    # The buys the engine produced are the buys the database now holds.
    produced_buys = sum(1 for entry in result.journal if entry.decision is Decision.BUY)
    persisted_buys = journal.count(JournalFilter(decision=Decision.BUY))
    assert persisted_buys == produced_buys
