"""M13.1 — the ``paper_session`` scheduler job against the docker Postgres.

The unit suite proves the session's logic over in-memory stores; this proves the job as the
scheduler runs it — ``run_paper_session_job`` with a real ``JobContext``, the real status interlock
reading ``sync_state``, the real append-only ``decision_journal`` and the ``paper_session`` ledger
(migration 0012), one committed transaction per run — over the frozen fixture market
(``tests/paper_session_support.py``) so it never reads the lake:

* with nothing published the status gate is red: one ``SKIPPED_DATA_RED`` lands in the journal
  and the ledger, and a rerun adds nothing;
* once ``nse_bhavcopy`` is published for the date the same rerun decides it, and a further rerun
  is a no-op — the journal row count does not move;
* the next session rebuilds the book from the ledger's JSON (orders, digest) and decides again;
* left disabled (the default), the job writes nothing at all;
* the table refuses a red row that claims orders;
* (M15.3) every decided session ends on its reconciliation; a book that disagrees with its broker
  is recorded ``RECON_BREAK``, trips the book's kill switch under the lake root, and is refused
  until the switch is reset and the runbook's resolution row is inserted; and migration 0015 keeps
  a book saved before it — the next session seeds its accounting book and decides.

Needs the docker postgres (`make up`); skips loudly if it is unreachable. No network.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Json

from backtest.paper_session import (
    PAPER_BOOK_ID,
    PAPER_MODE,
    RECON_BREAK_EVENT,
    InMemoryPaperSessionStore,
    PaperSessionResult,
    PostgresPaperSessionStore,
    ReconStatus,
    RecordingJournal,
    RunVerdict,
    SessionOutcome,
    paper_kill_switch,
    ratified_paper_book,
    run_paper_session,
    run_paper_session_job,
)
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.scheduler import JobContext
from dataplatform.status.sync_state import SyncStateStore
from dataplatform.store.db import connect, connection, with_dbname
from dataplatform.store.migrate import MIGRATIONS_DIR, migrate
from execution.kill_switch import TripSource
from tests.paper_session_support import (
    OCT_FIRST,
    OCT_FOURTH,
    OCT_SECOND,
    OCT_THIRD,
    FixtureWorld,
    fresh_kill_switch,
)

pytestmark = pytest.mark.integration

#: Pid-suffixed so concurrent build agents do not drop each other's scratch DB (cf test_migrations).
SCRATCH_DB = f"trading_m13_1_paper_session_{os.getpid()}"
MIGRATED_AT = datetime(2026, 9, 30, 9, 0, tzinfo=IST)


def _settings_for(dbname: str, data_root: Path | None = None) -> Settings:
    base = Settings()
    settings = Settings(database_url=with_dbname(base.database_url, dbname))
    if data_root is not None:
        settings = settings.model_copy(update={"data_root": data_root})
    return settings


@pytest.fixture(scope="module")
def scratch(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Settings]:
    """An empty scratch database migrated through 0012, dropped at the end of the module."""
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
    settings = _settings_for(SCRATCH_DB, tmp_path_factory.mktemp("paper_lake"))
    migrate(settings, clock=FrozenClock(MIGRATED_AT))
    yield settings.model_copy(update={"paper_session_enabled": True})
    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


def _job(settings: Settings, day: date, world: FixtureWorld) -> PaperSessionResult | None:
    """One scheduler run of the job at 20:30 IST on ``day``."""
    context = JobContext(
        job_name="paper_session",
        run_id=uuid4(),
        clock=FrozenClock(datetime(day.year, day.month, day.day, 20, 30, tzinfo=IST)),
        settings=settings,
    )
    return run_paper_session_job(context, world=world)


def _run(settings: Settings, day: date, world: FixtureWorld) -> PaperSessionResult:
    result = _job(settings, day, world)
    assert result is not None, "the scratch settings enable the job"
    return result


def _publish(settings: Settings, day: date) -> None:
    with connection(settings) as conn:
        store = SyncStateStore(conn, clock=FrozenClock(datetime(2026, 10, 1, 18, 45, tzinfo=IST)))
        store.begin("nse_bhavcopy", day)
        store.mark_fetched("nse_bhavcopy", day, checksum="fixture", l0_path="L0/fixture.zip")
        store.mark_validated("nse_bhavcopy", day)
        store.mark_normalized("nse_bhavcopy", day)
        store.mark_published("nse_bhavcopy", day)
        conn.commit()


def _journal_rows(settings: Settings, day: date) -> list[tuple[str, str]]:
    with connection(settings) as conn:
        rows = conn.execute(
            "SELECT decision, payload->>'mode' FROM decision_journal "
            "WHERE trading_date = %s AND payload->>'paper_book' = %s ORDER BY id",
            (day, PAPER_BOOK_ID),
        ).fetchall()
    return [(row[0], row[1]) for row in rows]


def test_the_job_is_red_until_published_then_decides_once(scratch: Settings) -> None:
    world = FixtureWorld()

    red = _run(scratch, OCT_FIRST, world)
    assert red.verdict is RunVerdict.SKIPPED_DATA_RED
    assert _journal_rows(scratch, OCT_FIRST) == [("SKIPPED_DATA_RED", PAPER_MODE)]
    assert world.reads == [], "a red day reads no decision data (invariant #10)"

    again = _run(scratch, OCT_FIRST, world)
    assert again.verdict is RunVerdict.STILL_RED
    assert len(_journal_rows(scratch, OCT_FIRST)) == 1, "a still-red rerun is not re-journaled"

    _publish(scratch, OCT_FIRST)
    decided = _run(scratch, OCT_FIRST, world)
    assert decided.verdict is RunVerdict.DECIDED
    rows = _journal_rows(scratch, OCT_FIRST)
    assert rows[0] == ("SKIPPED_DATA_RED", PAPER_MODE), "the journal is append-only"
    assert rows[-1] == ("HEARTBEAT", PAPER_MODE), "the session's reconciliation (M15.3)"
    assert {decision for decision, _ in rows[1:-1]} <= {"BUY", "RAIL_BLOCK"}
    assert any(decision == "BUY" for decision, _ in rows[1:])
    assert all(mode == PAPER_MODE for _, mode in rows)

    noop = _run(scratch, OCT_FIRST, world)
    assert noop.verdict is RunVerdict.ALREADY_DECIDED
    assert _journal_rows(scratch, OCT_FIRST) == rows, "a rerun on a decided date writes nothing"

    with connection(scratch) as conn:
        stored = PostgresPaperSessionStore(conn).get(PAPER_BOOK_ID, OCT_FIRST)
    assert stored is not None and stored == decided.record
    assert stored.outcome is SessionOutcome.COMPLETED and stored.orders
    assert stored.book_state is not None and stored.book_digest is not None


def test_the_next_session_rebuilds_the_book_from_the_ledger(scratch: Settings) -> None:
    world = FixtureWorld()
    _publish(scratch, OCT_SECOND)
    second = _run(scratch, OCT_SECOND, world)

    assert second.verdict is RunVerdict.DECIDED
    assert second.book is not None and second.book.positions, "Thursday's orders filled Monday"
    # The day's heartbeat, then its reconciliation.
    assert [decision for decision, _ in _journal_rows(scratch, OCT_SECOND)] == [
        "HEARTBEAT",
        "HEARTBEAT",
    ]


def test_the_job_left_disabled_writes_nothing(scratch: Settings) -> None:
    disabled = scratch.model_copy(update={"paper_session_enabled": False})
    assert _job(disabled, date(2026, 10, 6), FixtureWorld()) is None
    with connection(scratch) as conn:
        row = conn.execute(
            "SELECT count(*) FROM paper_session WHERE trading_date = '2026-10-06'"
        ).fetchone()
    assert row is not None and row[0] == 0


def test_the_table_refuses_a_red_row_that_claims_orders(scratch: Settings) -> None:
    with connection(scratch) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO paper_session (book_id, trading_date, outcome, reason, rebalanced, "
            "orders, journal_digest, recorded_at) VALUES "
            "('paper_check_book', '2026-10-07', 'SKIPPED_DATA_RED', 'red', false, "
            "'[{\"isin\": \"INE001A01173\"}]'::jsonb, 'x', now())"
        )


_KEY = "DIVIDEND:INE001A01173:2026-10-07"
_TERMS = "0123456789abcdef"


def test_the_owner_resolution_table_is_what_the_store_reads(scratch: Settings) -> None:
    """Item 2's way out: the runbook's INSERT — one key, one set of terms — is what unblocks."""
    with connection(scratch) as conn:
        store = PostgresPaperSessionStore(conn)
        assert store.resolutions(PAPER_BOOK_ID) == frozenset()
        conn.execute(
            "INSERT INTO paper_session_resolution (book_id, action_key, terms, resolved_by, note, "
            "resolved_at) VALUES (%s, %s, %s, 'owner', 'correction accepted as booked', now())",
            (PAPER_BOOK_ID, _KEY, _TERMS),
        )
        assert store.resolutions(PAPER_BOOK_ID) == frozenset({(_KEY, _TERMS)})
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO paper_session_resolution (book_id, action_key, terms, resolved_by, "
                "note, resolved_at) VALUES (%s, 'x', %s, 'owner', '  ', now())",
                (PAPER_BOOK_ID, _TERMS),
            )
        conn.rollback()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE paper_session_resolution SET note = 'rewritten'",
        "DELETE FROM paper_session_resolution",
        "TRUNCATE paper_session_resolution",
    ],
)
def test_a_resolution_can_never_be_edited_or_withdrawn(scratch: Settings, statement: str) -> None:
    """Append-only, by 0001's trigger: a resolution is a record of a human decision (#12)."""
    with connection(scratch) as conn:
        conn.execute(
            "INSERT INTO paper_session_resolution (book_id, action_key, terms, resolved_by, note, "
            "resolved_at) VALUES (%s, %s, %s, 'owner', 'accepted', now())",
            (PAPER_BOOK_ID, _KEY, _TERMS),
        )
        with pytest.raises(psycopg.errors.FeatureNotSupported, match="append-only"):
            conn.execute(statement)
        conn.rollback()


def test_the_summaries_read_no_book_state_and_carry_the_traded_names(scratch: Settings) -> None:
    with connection(scratch) as conn:
        store = PostgresPaperSessionStore(conn)
        summaries = store.summaries(PAPER_BOOK_ID, before=date(2026, 12, 31))
        decided = store.get(PAPER_BOOK_ID, OCT_FIRST)
    assert decided is not None
    (first,) = [s for s in summaries if s.trading_date == OCT_FIRST]
    assert first.traded == frozenset(order.isin for order in decided.orders)
    assert not hasattr(first, "book_state")


# ── M15.3: reconciliation, the break, and a book saved before 0015 ───────────────────────────────


def test_the_table_refuses_a_recon_break_that_claims_orders(scratch: Settings) -> None:
    with connection(scratch) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO paper_session (book_id, trading_date, outcome, reason, rebalanced, "
            "orders, journal_digest, book_state, book_digest, recon, recorded_at) VALUES "
            "('paper_check_book', '2026-10-07', 'RECON_BREAK', 'break', false, "
            "'[{\"isin\": \"INE001A01173\"}]'::jsonb, 'x', '{}'::jsonb, 'd', "
            '\'{"status": "BREAK"}\'::jsonb, now())'
        )


@pytest.mark.parametrize(
    ("rebalanced", "recon"),
    [
        ("true", '{"status": "BREAK", "key": "RECON:2026-10-07", "terms": "0123456789abcdef"}'),
        ("false", '{"status": "BREAK", "terms": "0123456789abcdef"}'),
        ("false", '{"status": "BREAK", "key": "RECON:2026-10-07"}'),
    ],
    ids=["rebalanced", "no-key", "no-terms"],
)
def test_the_table_refuses_a_recon_break_row_it_could_not_resolve_or_that_rebalanced(
    scratch: Settings, rebalanced: str, recon: str
) -> None:
    with connection(scratch) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO paper_session (book_id, trading_date, outcome, reason, rebalanced, "
            "journal_digest, book_state, book_digest, recon, recorded_at) VALUES "
            f"('paper_check_book', '2026-10-07', 'RECON_BREAK', 'break', {rebalanced}, 'x', "
            f"'{{}}'::jsonb, 'd', '{recon}'::jsonb, now())"
        )


def test_a_recon_break_is_stored_halts_the_book_and_clears_only_by_reset_and_resolution(
    scratch: Settings,
) -> None:
    world = FixtureWorld()
    # A divergence the broker does not share: the persisted accounting book loses a rupee.
    with connection(scratch) as conn:
        conn.execute(
            "UPDATE paper_session SET expected_book = jsonb_set(expected_book, '{cash}', "
            "to_jsonb(((expected_book->>'cash')::numeric - 1)::text)) "
            "WHERE book_id = %s AND trading_date = %s",
            (PAPER_BOOK_ID, OCT_SECOND),
        )
        conn.commit()

    _publish(scratch, OCT_THIRD)
    broken = _run(scratch, OCT_THIRD, world)
    assert broken.verdict is RunVerdict.RECON_BREAK
    with connection(scratch) as conn:
        store = PostgresPaperSessionStore(conn)
        stored = store.get(PAPER_BOOK_ID, OCT_THIRD)
        (summary,) = [
            s
            for s in store.summaries(PAPER_BOOK_ID, before=OCT_FOURTH)
            if s.trading_date == OCT_THIRD
        ]
        latest = store.latest_completed(PAPER_BOOK_ID, before=OCT_FOURTH)
    assert stored is not None and stored == broken.record
    assert stored.outcome is SessionOutcome.RECON_BREAK and stored.orders == ()
    assert stored.recon is not None and stored.recon.status is ReconStatus.BREAK
    assert summary.recon_break == (stored.recon.key, stored.recon.terms)
    assert latest is not None and latest.trading_date == OCT_THIRD, "the break's book is restored"
    switch = paper_kill_switch(scratch.data_root, PAPER_BOOK_ID)
    assert switch.is_tripped and switch.state.source is TripSource.RECON
    assert _journal_rows(scratch, OCT_THIRD)[-1] == ("ESCALATE", PAPER_MODE)

    _publish(scratch, OCT_FOURTH)
    assert _run(scratch, OCT_FOURTH, world).verdict is RunVerdict.HALTED

    # The runbook: reset the switch, then record the resolution; the book trades again after both.
    switch.reset(note="integration: owner accepted the broker's book")
    oct8 = date(2026, 10, 8)
    _publish(scratch, oct8)
    blocked = _run(scratch, oct8, world)
    assert blocked.verdict is RunVerdict.SKIPPED_DATA_RED
    assert blocked.entries[0].payload["event"] == RECON_BREAK_EVENT
    with connection(scratch) as conn:
        conn.execute(
            "INSERT INTO paper_session_resolution (book_id, action_key, terms, resolved_by, note, "
            "resolved_at) VALUES (%s, %s, %s, 'owner', 'broker side accepted', now())",
            (PAPER_BOOK_ID, stored.recon.key, stored.recon.terms),
        )
        conn.commit()
    oct9 = date(2026, 10, 9)
    _publish(scratch, oct9)
    resumed = _run(scratch, oct9, world)
    assert resumed.verdict is RunVerdict.DECIDED
    assert resumed.record is not None and resumed.record.recon is not None
    assert resumed.record.recon.seeded and resumed.record.recon.status is ReconStatus.CLEAN


#: A second scratch database, for the backward-compatibility check: migrated to 0014 only.
LEGACY_DB = f"trading_m15_3_legacy_{os.getpid()}"

#: The M13.1 writer's INSERT, verbatim in shape — what the live paper book's rows were written with.
_LEGACY_INSERT = (
    "INSERT INTO paper_session (book_id, trading_date, outcome, reason, rebalanced, orders, "
    "pending, journal_digest, book_state, book_digest, actions, recorded_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)


def test_0015_keeps_a_book_saved_before_it_and_the_next_session_decides(
    tmp_path: Path,
) -> None:
    admin = _settings_for("postgres")
    try:
        conn = connect(admin, autocommit=True)
    except psycopg.OperationalError as error:  # pragma: no cover - environment, not logic
        pytest.skip(f"postgres is not reachable — run `make up` first: {error}")
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{LEGACY_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{LEGACY_DB}"')
    finally:
        conn.close()
    try:
        settings = _settings_for(LEGACY_DB, tmp_path / "lake")
        through_0014 = tmp_path / "migrations_0014"
        through_0014.mkdir()
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name < "0015":
                (through_0014 / path.name).write_text(path.read_text())
        migrate(settings, clock=FrozenClock(MIGRATED_AT), directory=through_0014)

        # The live book as M13.1 left it: one decided session, written with the 0012 columns.
        legacy = run_paper_session(
            trading_date=OCT_FIRST,
            spec=ratified_paper_book(),
            world=FixtureWorld(),
            store=InMemoryPaperSessionStore(),
            journal=RecordingJournal(),
            gate=lambda _day: _Verdict(),
            clock=FrozenClock(datetime(2026, 10, 1, 21, 45, tzinfo=IST)),
            kill_switch=fresh_kill_switch(),
        ).record
        assert legacy is not None and legacy.book_state is not None
        with connection(settings) as conn:
            conn.execute(
                _LEGACY_INSERT,
                (
                    legacy.book_id,
                    legacy.trading_date,
                    legacy.outcome.value,
                    legacy.reason,
                    legacy.rebalanced,
                    Json(legacy.orders_document()),
                    None,
                    legacy.journal_digest,
                    Json(dict(legacy.book_state)),
                    legacy.book_digest,
                    Json(legacy.actions_document()),
                    datetime(2026, 10, 1, 21, 46, tzinfo=IST),
                ),
            )
            conn.commit()

        # Exactly the migrations after 0014 apply, 0015 first; later ones (0018, M17.1) ride along.
        applied = [m.version for m in migrate(settings, clock=FrozenClock(MIGRATED_AT))]
        assert applied == sorted(
            p.name[:4] for p in MIGRATIONS_DIR.glob("*.sql") if p.name >= "0015"
        )
        assert applied[0] == "0015"
        with connection(settings) as conn:
            kept = PostgresPaperSessionStore(conn).get(PAPER_BOOK_ID, OCT_FIRST)
        assert kept is not None and kept.recon is None and kept.expected_book is None
        assert kept.book_digest == legacy.book_digest

        enabled = settings.model_copy(update={"paper_session_enabled": True})
        _publish(enabled, OCT_SECOND)
        result = _run(enabled, OCT_SECOND, FixtureWorld())
        assert result.verdict is RunVerdict.DECIDED
        assert result.record is not None and result.record.recon is not None
        assert result.record.recon.seeded and result.record.recon.status is ReconStatus.CLEAN
        assert result.record.recon.executed, "the legacy session's staged orders filled"
        # Seeded once: the next session restores the accounting book the previous one persisted.
        _publish(enabled, OCT_THIRD)
        third = _run(enabled, OCT_THIRD, FixtureWorld())
        assert third.verdict is RunVerdict.DECIDED
        assert third.record is not None and third.record.recon is not None
        assert not third.record.recon.seeded
        assert third.record.recon.status is ReconStatus.CLEAN
    finally:
        conn = connect(admin, autocommit=True)
        try:
            conn.execute(f'DROP DATABASE IF EXISTS "{LEGACY_DB}" WITH (FORCE)')
        finally:
            conn.close()


class _Verdict:
    reason = ""

    def __bool__(self) -> bool:
        return True
