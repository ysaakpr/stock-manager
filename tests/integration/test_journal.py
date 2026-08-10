"""M5.1 acceptance 1, live: Postgres itself refuses to change or remove a journal entry.

`tests/unit/test_journal.py` proves the two halves that can be checked offline — the schema
declares the enforcement, and no code in `analyst/journal/` can attempt a mutation. Neither is the
claim. The claim is that a statement issued by *anything* — this package, a future module, an
operator with psql open — is rejected by the database, and only a database can demonstrate that.

So this file drives the real thing: a scratch database, the real migrations, a real `Journal`
writing real rows, and then the four statements invariant #12 forbids. It also closes the loop the
recording connection could only simulate — that a `Decimal` cost survives `NUMERIC`, that a
`jsonb` verdict list comes back as the same verdicts, and that a decision written today is
reconstructable byte-for-byte from its evidence reference.

Needs the docker postgres (`make up`). If it is unreachable the module skips with a loud reason,
matching `test_migrations.py`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from pydantic import SecretStr

from analyst.journal import (
    Actor,
    BreakConditionEvaluation,
    Decision,
    EvidenceBundle,
    EvidenceItem,
    EvidenceKind,
    EvidenceStore,
    Journal,
    JournalEntry,
    JournalFilter,
    Sleeve,
    TokenSpend,
    Verdict,
)
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

#: Created and dropped per session, pid-suffixed so concurrent build agents do not drop each
#: other's database mid-test (the reasoning is spelled out in test_migrations.py).
SCRATCH_DB = f"trading_m5_1_journal_{os.getpid()}"

CASE_ID = "AI_ROBOTICS"
ISIN = "INE009A01021"
TRADING_DATE = date(2026, 8, 7)
DECIDED_AT = datetime(2026, 8, 7, 19, 30, tzinfo=IST)
RECORDED_AT = datetime(2026, 8, 7, 19, 30, 2, tzinfo=IST)


def _settings_for(dbname: str) -> Settings:
    dsn = with_dbname(Settings().database_url.get_secret_value(), dbname)
    return Settings(database_url=SecretStr(dsn))


@pytest.fixture(scope="session")
def journal_settings() -> Iterator[Settings]:
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
    migrate(scratch, clock=FrozenClock(RECORDED_AT))
    yield scratch

    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture
def conn(journal_settings: Settings) -> Iterator[Connection]:
    """A connection whose transaction is rolled back at the end of the test.

    Rollback rather than cleanup, because there is no cleanup available: `decision_journal`
    rejects DELETE, which is the very thing under test here.
    """
    with connection(journal_settings) as live:
        live.execute(
            "INSERT INTO case_ (case_id, title, state, created_at, updated_at) "
            "VALUES (%s, 'M5.1 fixture', 'ACTIVE', %s, %s) ON CONFLICT DO NOTHING",
            (CASE_ID, RECORDED_AT, RECORDED_AT),
        )
        try:
            yield live
        finally:
            live.rollback()


@pytest.fixture
def journal(conn: Connection, tmp_path: Path) -> Journal:
    return Journal(conn, clock=FrozenClock(RECORDED_AT), evidence=EvidenceStore(tmp_path))


def bundle(actor: Actor = Actor.T0) -> EvidenceBundle:
    return EvidenceBundle(
        case_id=CASE_ID,
        trading_date=TRADING_DATE,
        actor=actor,
        rendered_prompt="Are any break conditions met for the holdings below?",
        items=(
            EvidenceItem(
                kind=EvidenceKind.PRICE,
                source="nse_bhavcopy",
                label="close",
                isin=ISIN,
                as_of=TRADING_DATE,
                knowable_at=datetime(2026, 8, 7, 18, 0, tzinfo=IST),
                value=Decimal("1234.55"),
                detail={"series": "EQ"},
            ),
            EvidenceItem(
                kind=EvidenceKind.STATUS,
                source="status_api",
                label="sync_green",
                as_of=TRADING_DATE,
                text="all core datasets PUBLISHED, no open ERROR flags",
            ),
        ),
    )


def a_sell(journal: Journal) -> int:
    """Append a fully-populated SELL and return its id."""
    return journal.append(
        JournalEntry(
            ts=DECIDED_AT,
            trading_date=TRADING_DATE,
            case_id=CASE_ID,
            actor=Actor.T1,
            decision=Decision.SELL,
            isin=ISIN,
            sleeve=Sleeve.CORE,
            break_conditions_evaluated=(
                BreakConditionEvaluation(
                    id="BC1", verdict=Verdict.BROKEN, observed="two down quarters"
                ),
            ),
            rationale="BC1 met: segment revenue declined for a second consecutive quarter",
            model="stub-strong-1",
            tokens=TokenSpend(tokens_in=1200, tokens_out=300, cost_inr=Decimal("4.125000")),
            orders_ref="ORD-2026-08-07-0001",
            payload={"trigger": "results_filing"},
        ),
        evidence=bundle(Actor.T1),
    ).id


# ── acceptance 1: the database rejects UPDATE, DELETE and TRUNCATE ────────────────────────────


def test_an_update_is_rejected_by_the_database(journal: Journal, conn: Connection) -> None:
    """Acceptance 1. A journal whose rationale can be edited afterwards is not evidence."""
    entry_id = a_sell(journal)

    with pytest.raises(psycopg.errors.FeatureNotSupported, match="append-only"), conn.transaction():
        conn.execute(
            "UPDATE decision_journal SET rationale = 'rewritten' WHERE id = %s", (entry_id,)
        )

    assert journal.get(entry_id).rationale.startswith("BC1 met")  # type: ignore[union-attr]


def test_a_delete_is_rejected_by_the_database(journal: Journal, conn: Connection) -> None:
    """Acceptance 1. Deleting the entry for a trade that happened is rewriting history."""
    entry_id = a_sell(journal)

    with pytest.raises(psycopg.errors.FeatureNotSupported, match="append-only"), conn.transaction():
        conn.execute("DELETE FROM decision_journal WHERE id = %s", (entry_id,))

    assert journal.get(entry_id).id == entry_id


def test_a_delete_matching_no_rows_is_rejected_too(journal: Journal, conn: Connection) -> None:
    """The trigger is statement-level for exactly this reason.

    A row-level trigger never fires on a statement that matches nothing, so
    `DELETE ... WHERE false` would appear to succeed — and a caller that reads "deletion works,
    my predicate was just too narrow" is one predicate away from deleting the journal.
    """
    a_sell(journal)

    with pytest.raises(psycopg.errors.FeatureNotSupported, match="append-only"), conn.transaction():
        conn.execute("DELETE FROM decision_journal WHERE false")


def test_a_truncate_is_rejected_by_the_database(journal: Journal, conn: Connection) -> None:
    """TRUNCATE is not a DELETE and needs its own trigger; without it the table empties in one line.

    The plain form is refused earlier and for a weaker reason — `order_` has a foreign key into
    the journal, and Postgres will not truncate a referenced table. That refusal is real but it is
    not the invariant: `CASCADE` is the documented way around it, and it is the trigger, not the
    foreign key, that has to be waiting there. Both forms are asserted so that a future schema
    change dropping the FK cannot quietly turn this test into a no-op.
    """
    a_sell(journal)

    with pytest.raises(psycopg.errors.Error) as plain, conn.transaction():
        conn.execute("TRUNCATE decision_journal")
    assert "append-only" in str(plain.value) or "foreign key" in str(plain.value)

    with pytest.raises(psycopg.errors.FeatureNotSupported, match="append-only"), conn.transaction():
        conn.execute("TRUNCATE decision_journal CASCADE")


def test_the_refusal_says_what_to_do_instead(journal: Journal, conn: Connection) -> None:
    """An operator who hits this at 2am needs the rule, not just the rejection."""
    a_sell(journal)

    with pytest.raises(psycopg.errors.FeatureNotSupported) as raised, conn.transaction():
        conn.execute("UPDATE decision_journal SET rationale = 'x'")

    assert "append a superseding row" in (raised.value.diag.message_hint or "")


# ── the round trip the offline suite can only simulate ────────────────────────────────────────


def test_an_appended_entry_comes_back_with_every_field_intact(journal: Journal) -> None:
    entry_id = a_sell(journal)

    found = journal.get(entry_id)

    assert (found.actor, found.decision, found.sleeve) == (Actor.T1, Decision.SELL, Sleeve.CORE)
    assert found.isin == ISIN and found.orders_ref == "ORD-2026-08-07-0001"
    assert found.ts == DECIDED_AT and found.recorded_at == RECORDED_AT
    assert found.break_conditions_evaluated[0].verdict is Verdict.BROKEN
    assert found.break_conditions_evaluated[0].observed == "two down quarters"
    assert found.payload == {"trigger": "results_filing"}


def test_a_rupee_cost_survives_the_numeric_column_exactly(journal: Journal) -> None:
    """Money is NUMERIC end to end; a float here would be a bug in the cost report (CLAUDE.md)."""
    found = journal.get(a_sell(journal))

    assert found.tokens is not None
    assert found.tokens.cost_inr == Decimal("4.125000")
    assert isinstance(found.tokens.cost_inr, Decimal)


def test_a_decision_is_reconstructable_from_its_stored_evidence(journal: Journal) -> None:
    """§8.3.3: the bundle comes back byte-identically, re-hashed on the way out."""
    entry_id = a_sell(journal)
    shown = bundle(Actor.T1)

    reconstruction = journal.reconstruct(entry_id)

    assert reconstruction.entry.id == entry_id
    assert reconstruction.evidence == shown
    assert journal.evidence.get(reconstruction.entry.evidence_snapshot_ref or "") == (
        shown.canonical_bytes()
    )


# ── acceptance 3, live: a heartbeat is a row like any other ───────────────────────────────────


def test_a_heartbeat_is_stored_and_queried_like_any_other_decision(journal: Journal) -> None:
    """Acceptance 3 and invariant #9, against the real CHECK constraint on `decision`."""
    recorded = journal.heartbeat(bundle())

    found = journal.entries(JournalFilter(case_id=CASE_ID, decision=Decision.HEARTBEAT))

    assert [item.id for item in found] == [recorded.id]
    assert found[0].evidence_snapshot_ref == bundle().ref().ref
    assert journal.reconstruct(recorded.id).evidence == bundle()


def test_the_query_surface_filters_by_case_date_actor_and_decision(journal: Journal) -> None:
    heartbeat = journal.heartbeat(bundle())
    sell = a_sell(journal)

    assert [item.id for item in journal.entries(JournalFilter(actor=Actor.T1))] == [sell]
    assert [item.id for item in journal.entries(JournalFilter(isin=ISIN))] == [sell]
    assert journal.count(JournalFilter(case_id=CASE_ID)) == 2
    assert journal.count(JournalFilter(start=TRADING_DATE, end=TRADING_DATE)) == 2
    assert journal.count(JournalFilter(start=date(2026, 8, 8))) == 0
    # Newest session first, ties broken by insertion order reversed.
    assert [item.id for item in journal.entries()] == [sell, heartbeat.id]


def test_a_platform_wide_entry_needs_no_case(journal: Journal) -> None:
    """A data-red skip is about the platform, not a case (invariant #10), and must still land."""
    recorded = journal.append(
        JournalEntry(
            ts=DECIDED_AT,
            trading_date=TRADING_DATE,
            actor=Actor.SYSTEM,
            decision=Decision.SKIPPED_DATA_RED,
            rationale="not PUBLISHED on 2026-08-07: nse_bhavcopy=FAILED",
        )
    )

    assert journal.get(recorded.id).case_id is None
