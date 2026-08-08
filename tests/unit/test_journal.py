"""M5.1: the journal is append-only, its evidence content-addressed, and a no-op an entry.

§0 calls the decision journal the product, so the three claims it makes have to be shown rather
than documented:

1. **Entries are immutable.** Proved in two halves, because neither alone is enough. Here, offline:
   the schema *declares* the enforcement (`reject_mutation()` triggers on UPDATE, DELETE and
   TRUNCATE, plus revoked grants) and no code in `analyst/journal/` can attempt a mutation — checked
   by parsing both, since a behavioural test only shows that the paths it happened to call behaved.
   In `tests/integration/test_journal.py`, live: Postgres actually refuses all four statements.
2. **Evidence is content-addressed and re-fetchable byte-identically.** Fully covered here — the
   store is filesystem-backed, so a real bundle really is written, re-read, re-hashed and compared
   byte for byte, including the case where the stored file is damaged behind the store's back.
3. **A heartbeat is a first-class entry.** Not the absence of a row: `Journal.heartbeat` writes a
   `HEARTBEAT` decision carrying the evidence considered, and it comes back from the same query
   surface as every other decision.

The database is stood in for by a recording connection that echoes an insert's parameters back as
the inserted row — which is what `INSERT ... RETURNING` does — so this file stays offline and fast
(CLAUDE.md) while still asserting on the exact SQL and parameters the journal emits. What that
cannot prove is what Postgres does with them; that is the integration half's job.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from psycopg.types.json import Json
from pydantic import ValidationError

from analyst.journal import (
    Actor,
    BreakConditionEvaluation,
    Decision,
    EvidenceBundle,
    EvidenceChecksumError,
    EvidenceItem,
    EvidenceKind,
    EvidenceNotFoundError,
    EvidenceStore,
    Journal,
    JournalEntry,
    JournalError,
    JournalFilter,
    Sleeve,
    TokenSpend,
    UnknownEntryError,
    Verdict,
    canonical_bytes,
    digest_of,
    parse_ref,
)
from dataplatform.clock import IST, FrozenClock
from dataplatform.store.db import Connection

REPO_ROOT = Path(__file__).resolve().parents[2]
JOURNAL_PACKAGE = REPO_ROOT / "analyst" / "journal"
INIT_MIGRATION = REPO_ROOT / "dataplatform" / "store" / "migrations" / "0001_init.sql"

TRADING_DATE = date(2026, 8, 7)
DECIDED_AT = datetime(2026, 8, 7, 19, 30, tzinfo=IST)
RECORDED_AT = datetime(2026, 8, 7, 19, 30, 2, tzinfo=IST)
CASE_ID = "AI_ROBOTICS"
ISIN = "INE009A01021"
OTHER_ISIN = "INE467B01029"


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(RECORDED_AT)


@pytest.fixture
def store(tmp_path: Path) -> EvidenceStore:
    return EvidenceStore(tmp_path / "evidence")


def price_item(isin: str = ISIN, close: str = "1234.55") -> EvidenceItem:
    return EvidenceItem(
        kind=EvidenceKind.PRICE,
        source="nse_bhavcopy",
        label="close",
        isin=isin,
        as_of=TRADING_DATE,
        knowable_at=datetime(2026, 8, 7, 18, 0, tzinfo=IST),
        value=Decimal(close),
        detail={"series": "EQ", "delivery_pct": "42.75"},
    )


def bundle(*items: EvidenceItem, case_id: str | None = CASE_ID) -> EvidenceBundle:
    return EvidenceBundle(
        case_id=case_id,
        trading_date=TRADING_DATE,
        actor=Actor.T0,
        items=items or (price_item(),),
    )


def entry(**overrides: Any) -> JournalEntry:
    """A minimal valid entry, with the fields a test cares about overridden."""
    fields: dict[str, Any] = {
        "ts": DECIDED_AT,
        "trading_date": TRADING_DATE,
        "case_id": CASE_ID,
        "actor": Actor.T0,
        "decision": Decision.HOLD,
    }
    fields.update(overrides)
    return JournalEntry(**fields)


class FakeCursor:
    """The two cursor methods the journal uses."""

    def __init__(self, rows: Sequence[tuple[Any, ...]]) -> None:
        self._rows = list(rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class RecordingConnection:
    """A connection that records every statement and echoes inserts back as rows.

    Faithful in the one way that matters here: `INSERT ... RETURNING` hands back the row the
    server built, so returning `(id, *parameters)` is what a real insert of those parameters
    yields. `Json` wrappers are unwrapped exactly as Postgres unwraps them on the way out of a
    `jsonb` column, which is why `payload` and `break_conditions_evaluated` round-trip.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[Any]]] = []
        self.rows: list[tuple[Any, ...]] = []
        self.next_id = 1

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> FakeCursor:
        unwrapped = [_unwrap(value) for value in (params or ())]
        self.calls.append((sql, unwrapped))
        if sql.lstrip().upper().startswith("INSERT"):
            row = (self.next_id, *unwrapped)
            self.next_id += 1
            return FakeCursor([row])
        return FakeCursor(self.rows)

    @property
    def last(self) -> tuple[str, list[Any]]:
        return self.calls[-1]


def _unwrap(value: Any) -> Any:
    return value.obj if isinstance(value, Json) else value


@pytest.fixture
def conn() -> RecordingConnection:
    return RecordingConnection()


@pytest.fixture
def journal(conn: RecordingConnection, clock: FrozenClock, store: EvidenceStore) -> Journal:
    return Journal(cast(Connection, conn), clock=clock, evidence=store)


def row_for(**overrides: Any) -> tuple[Any, ...]:
    """A `decision_journal` row in `_READ_COLUMNS` order, for the read paths."""
    columns: dict[str, Any] = {
        "id": 7,
        "ts": DECIDED_AT,
        "trading_date": TRADING_DATE,
        "case_id": CASE_ID,
        "actor": "T0",
        "decision": "HEARTBEAT",
        "isin": None,
        "sleeve": None,
        "evidence_snapshot_ref": f"sha256:{'a' * 64}",
        "break_conditions_evaluated": [],
        "rationale": None,
        "model": None,
        "tokens_in": None,
        "tokens_out": None,
        "cost_inr": None,
        "orders_ref": None,
        "payload": {},
        "recorded_at": RECORDED_AT,
    }
    columns.update(overrides)
    return tuple(columns.values())


# ── the §5.7 entry schema ────────────────────────────────────────────────────────────────────


def test_an_entry_carries_every_field_the_plan_names() -> None:
    """§5.7's shape, end to end — anything missing here is a decision nobody can review."""
    recorded = entry(
        decision=Decision.SELL,
        actor=Actor.T1,
        isin=ISIN,
        sleeve=Sleeve.CORE,
        evidence_snapshot_ref=f"sha256:{'0' * 64}",
        break_conditions_evaluated=(
            BreakConditionEvaluation(
                id="BC1", verdict=Verdict.BROKEN, observed="two down quarters"
            ),
            BreakConditionEvaluation(id="BC2", verdict=Verdict.INTACT),
        ),
        rationale="segment revenue fell for a second quarter; BC1 is met",
        model="stub-strong-1",
        tokens=TokenSpend(tokens_in=1200, tokens_out=300, cost_inr=Decimal("4.125000")),
        orders_ref="ORD-2026-08-07-0001",
        payload={"trigger": "results_filing"},
    )

    assert recorded.decision is Decision.SELL
    assert recorded.break_conditions_evaluated[0].verdict is Verdict.BROKEN
    assert recorded.tokens is not None and recorded.tokens.cost_inr == Decimal("4.125000")


def test_a_naive_timestamp_is_rejected() -> None:
    """B10: the instant a decision claims must be unambiguous, or a replay cannot reproduce it."""
    with pytest.raises(ValidationError, match="tz-aware"):
        entry(ts=datetime(2026, 8, 7, 19, 30))


def test_a_decision_about_a_future_session_is_rejected() -> None:
    """Invariant #7 at this layer: no entry may claim to reason about a session that has not run."""
    with pytest.raises(ValidationError, match="has not happened yet"):
        entry(trading_date=date(2026, 8, 8))


def test_a_decision_about_an_earlier_session_is_allowed() -> None:
    """A backfilled or replayed decision is about a past date and must still be writable."""
    assert entry(trading_date=date(2026, 8, 6)).trading_date == date(2026, 8, 6)


@pytest.mark.parametrize(
    "decision",
    [
        Decision.BUY,
        Decision.SELL,
        Decision.ESCALATE,
        Decision.SKIPPED_DATA_RED,
        Decision.RAIL_BLOCK,
        Decision.POLICY_PROPOSAL,
    ],
)
def test_a_decision_that_acted_needs_a_rationale(decision: Decision) -> None:
    """§0: 'reason acted' is a field, not a habit."""
    with pytest.raises(ValidationError, match="needs a rationale"):
        entry(decision=decision, isin=ISIN, sleeve=Sleeve.CORE)


def test_a_blank_rationale_is_not_a_rationale() -> None:
    with pytest.raises(ValidationError):
        entry(decision=Decision.ESCALATE, rationale="   ")


@pytest.mark.parametrize("decision", [Decision.BUY, Decision.SELL])
def test_a_trade_needs_its_instrument_and_its_sleeve(decision: Decision) -> None:
    """Invariant #2 and §5.5: an untagged trade belongs to no sleeve and joins to no instrument."""
    with pytest.raises(ValidationError, match="needs the isin"):
        entry(decision=decision, rationale="because", sleeve=Sleeve.CORE)
    with pytest.raises(ValidationError, match="needs its sleeve"):
        entry(decision=decision, rationale="because", isin=ISIN)


def test_a_symbol_cannot_be_passed_where_an_isin_belongs() -> None:
    """Invariant #2: the shape check is what catches a bhavcopy symbol pasted into this field."""
    with pytest.raises(ValidationError):
        entry(decision=Decision.BUY, rationale="because", isin="RELIANCE", sleeve=Sleeve.CORE)


def test_money_may_not_be_a_float() -> None:
    """CLAUDE.md: a float cost is a bug, and pydantic would otherwise coerce one silently."""
    with pytest.raises(ValidationError, match="float"):
        TokenSpend(tokens_in=1, tokens_out=1, cost_inr=4.125)  # type: ignore[arg-type]


def test_a_cost_without_a_model_is_rejected() -> None:
    """An unattributed spend cannot be reported per-tier in the evidence pack (§5.7)."""
    with pytest.raises(ValidationError, match="without a model"):
        entry(tokens=TokenSpend(tokens_in=1, tokens_out=1, cost_inr=Decimal("0.1")))


def test_an_evidence_reference_must_be_a_content_address() -> None:
    """A pointer that is not a hash is a pointer at something that could have changed."""
    with pytest.raises(ValidationError):
        entry(evidence_snapshot_ref="s3://bucket/latest.json")


# ── acceptance 2: evidence is content-addressed and re-fetchable byte-identically ─────────────


def test_a_bundle_is_addressed_by_the_sha256_of_its_canonical_bytes(store: EvidenceStore) -> None:
    original = bundle()
    ref = store.put(original)

    assert ref.sha256 == digest_of(original.canonical_bytes())
    assert ref.ref == f"sha256:{ref.sha256}"
    assert parse_ref(ref.ref) == ref.sha256
    assert ref.size_bytes == len(original.canonical_bytes())
    assert ref.item_count == 1


def test_a_stored_bundle_comes_back_byte_identically(store: EvidenceStore) -> None:
    """Acceptance 2. Not 'equivalent JSON' — the same bytes, so the hash still answers for them."""
    original = bundle(price_item(), price_item(OTHER_ISIN, "987.10"))
    ref = store.put(original)

    assert store.get(ref) == original.canonical_bytes()
    assert store.get(ref.ref) == original.canonical_bytes()
    assert store.load(ref) == original


def test_a_reloaded_bundle_keeps_its_prices_exact(store: EvidenceStore) -> None:
    """A price that came back as a float would make the reconstruction subtly untrue."""
    ref = store.put(bundle(price_item(close="1234.55")))
    value = store.load(ref).items[0].value

    assert value == Decimal("1234.55")
    assert isinstance(value, Decimal)


def test_the_same_evidence_always_gets_the_same_address(store: EvidenceStore) -> None:
    """Content addressing means storing it twice is a verified no-op, not a second copy."""
    first = store.put(bundle())
    second = store.put(bundle())

    assert first.sha256 == second.sha256
    assert [path.name for path in sorted(store.root.rglob("*.json"))] == [f"{first.sha256}.json"]


def test_different_evidence_gets_a_different_address(store: EvidenceStore) -> None:
    assert store.put(bundle()).sha256 != store.put(bundle(price_item(close="1.00"))).sha256


def test_the_address_does_not_depend_on_mapping_order() -> None:
    """Two callers who built the same facts in a different order must agree on the address."""
    forward = price_item()
    reversed_detail = forward.model_copy(
        update={"detail": dict(reversed(list(forward.detail.items())))}
    )

    assert bundle(forward).ref().sha256 == bundle(reversed_detail).ref().sha256


def test_the_bundle_carries_no_timestamp_of_its_own(store: EvidenceStore) -> None:
    """A clock in the content would re-address identical evidence on every run and break replay."""
    document = json.loads(store.get(store.put(bundle())))

    assert set(document) == {"case_id", "trading_date", "actor", "rendered_prompt", "items"}


def test_canonical_bytes_refuse_a_non_finite_number() -> None:
    """NaN is not JSON, and its presence means a float reached a place that keeps floats out."""
    with pytest.raises(ValueError, match="Out of range"):
        canonical_bytes({"value": float("nan")})


def test_a_damaged_bundle_is_refused_rather_than_returned(store: EvidenceStore) -> None:
    """The address verifies itself: bytes that changed cannot answer to their own name."""
    ref = store.put(bundle())
    path = store.path_of(ref)
    path.chmod(0o644)
    path.write_bytes(b'{"case_id": "TAMPERED"}')

    with pytest.raises(EvidenceChecksumError, match="cannot be used to reconstruct"):
        store.get(ref)


def test_a_stored_bundle_is_written_read_only(store: EvidenceStore) -> None:
    """Defence in depth behind O_EXCL — a careless rewrite has to escalate on the file first."""
    path = store.path_of(store.put(bundle()))

    assert path.stat().st_mode & 0o222 == 0


def test_an_unknown_reference_raises_rather_than_returning_nothing(store: EvidenceStore) -> None:
    with pytest.raises(EvidenceNotFoundError):
        store.get(f"sha256:{'b' * 64}")


def test_a_reference_that_is_not_a_content_address_is_refused() -> None:
    with pytest.raises(ValueError, match="not an evidence reference"):
        parse_ref("md5:deadbeef")
    with pytest.raises(ValueError, match="hex sha256"):
        parse_ref(f"sha256:{'A' * 64}")


def test_an_empty_bundle_is_not_evidence() -> None:
    """A snapshot with nothing in it would let a heartbeat claim evidence it never had."""
    with pytest.raises(ValidationError):
        EvidenceBundle(trading_date=TRADING_DATE, actor=Actor.T0, items=())


def test_an_evidence_value_may_not_be_a_float() -> None:
    with pytest.raises(ValidationError, match="float"):
        EvidenceItem(
            kind=EvidenceKind.PRICE,
            source="nse_bhavcopy",
            label="close",
            value=1234.55,  # type: ignore[arg-type]
        )


# ── acceptance 3: a heartbeat is a first-class entry ──────────────────────────────────────────


def test_a_heartbeat_writes_a_row_carrying_the_evidence_considered(
    journal: Journal, conn: RecordingConnection, store: EvidenceStore
) -> None:
    """Acceptance 3, and invariant #9: 'checked, nothing happened' is a decision with evidence."""
    considered = bundle(price_item(), price_item(OTHER_ISIN, "987.10"))

    recorded = journal.heartbeat(considered)

    sql, params = conn.last
    assert sql.startswith("INSERT INTO decision_journal")
    assert recorded.decision is Decision.HEARTBEAT
    assert recorded.actor is Actor.T0
    assert recorded.case_id == CASE_ID
    assert recorded.trading_date == TRADING_DATE
    assert recorded.ts == RECORDED_AT  # from the injected clock, never the wall clock
    assert recorded.evidence_snapshot_ref == considered.ref().ref
    assert considered.ref().ref in params
    assert store.exists(considered.ref())


def test_a_heartbeat_is_reconstructable_from_its_reference(
    journal: Journal, conn: RecordingConnection
) -> None:
    """The point of the entry: what the agent looked at on a day it did nothing is recoverable."""
    considered = bundle()
    recorded = journal.heartbeat(considered)
    conn.rows = [
        row_for(
            id=recorded.id,
            ts=recorded.ts,
            evidence_snapshot_ref=recorded.evidence_snapshot_ref,
        )
    ]

    reconstruction = journal.reconstruct(recorded.id)

    assert reconstruction.entry.decision is Decision.HEARTBEAT
    assert reconstruction.evidence == considered


def test_a_heartbeat_may_carry_break_condition_verdicts(
    journal: Journal, conn: RecordingConnection
) -> None:
    """T0 evaluates conditions mechanically; 'all intact' is exactly what a heartbeat records."""
    recorded = journal.heartbeat(
        bundle(),
        break_conditions_evaluated=[BreakConditionEvaluation(id="BC1", verdict=Verdict.INTACT)],
    )

    assert recorded.break_conditions_evaluated[0].verdict is Verdict.INTACT
    assert [{"id": "BC1", "verdict": "INTACT", "observed": None}] in conn.last[1]


def test_a_heartbeat_without_evidence_cannot_be_constructed() -> None:
    """Invariant #9 is 'a heartbeat *with the evidence considered*', not a liveness ping."""
    with pytest.raises(ValidationError, match="needs evidence_snapshot_ref"):
        entry(decision=Decision.HEARTBEAT)


def test_heartbeats_are_queryable_as_a_decision_type(
    journal: Journal, conn: RecordingConnection
) -> None:
    """First-class means it comes back from the same query surface as a BUY."""
    conn.rows = [row_for(id=9), row_for(id=8)]

    found = journal.entries(JournalFilter(case_id=CASE_ID, decision=Decision.HEARTBEAT))

    sql, params = conn.last
    assert "WHERE case_id = %s AND decision = %s" in sql
    assert params == [CASE_ID, "HEARTBEAT"]
    assert [item.id for item in found] == [9, 8]
    assert all(item.decision is Decision.HEARTBEAT for item in found)


# ── the write path ───────────────────────────────────────────────────────────────────────────


def test_append_stores_the_bundle_before_the_row(
    journal: Journal, conn: RecordingConnection, store: EvidenceStore
) -> None:
    """Order matters: an entry naming evidence that was never written is not reconstructable."""
    considered = bundle()

    recorded = journal.append(
        entry(decision=Decision.HOLD, rationale="thesis intact"), evidence=considered
    )

    assert store.exists(considered.ref())
    assert recorded.evidence_snapshot_ref == considered.ref().ref
    assert conn.calls[0][0].startswith("INSERT INTO decision_journal")


def test_append_refuses_a_bundle_that_contradicts_the_entry(journal: Journal) -> None:
    """One of the two is not what the decision saw, and guessing which would falsify the record."""
    with pytest.raises(JournalError, match="not what this decision saw"):
        journal.append(entry(evidence_snapshot_ref=f"sha256:{'c' * 64}"), evidence=bundle())


def test_append_writes_every_column_the_schema_holds(
    journal: Journal, conn: RecordingConnection
) -> None:
    """A column written but never read (or the reverse) is a field that silently stays empty."""
    recorded = journal.append(
        entry(
            decision=Decision.BUY,
            actor=Actor.T2,
            isin=ISIN,
            sleeve=Sleeve.TACTICAL,
            rationale="cycle expression within the ratified dial",
            model="stub-strong-1",
            tokens=TokenSpend(tokens_in=900, tokens_out=120, cost_inr=Decimal("2.500000")),
            orders_ref="ORD-1",
            payload={"dial_pct": "30"},
            evidence_snapshot_ref=f"sha256:{'d' * 64}",
        )
    )

    _, params = conn.last
    assert params[:8] == [
        DECIDED_AT,
        TRADING_DATE,
        CASE_ID,
        "T2",
        "BUY",
        ISIN,
        "TACTICAL",
        f"sha256:{'d' * 64}",
    ]
    assert params[-1] == RECORDED_AT  # recorded_at, from this journal's injected clock
    assert recorded.id == 1
    assert recorded.tokens is not None and recorded.tokens.cost_inr == Decimal("2.500000")
    assert recorded.payload == {"dial_pct": "30"}


def test_the_journal_never_commits(journal: Journal, conn: RecordingConnection) -> None:
    """The caller owns the transaction, so a loop can journal several entries atomically."""
    journal.append(entry())

    assert not hasattr(conn, "committed")
    assert all("COMMIT" not in sql.upper() for sql, _ in conn.calls)


# ── the query surface: by case, date, actor, decision type ────────────────────────────────────


def test_an_unconstrained_filter_adds_no_where_clause() -> None:
    assert JournalFilter().where() == ("", [])


def test_every_filter_dimension_becomes_a_bound_parameter() -> None:
    """Bound, not interpolated: a case id ultimately came from a user."""
    clause, params = JournalFilter(
        case_id=CASE_ID,
        actor=Actor.RAILS,
        decision=Decision.RAIL_BLOCK,
        isin=ISIN,
        start=date(2026, 8, 1),
        end=TRADING_DATE,
    ).where()

    assert clause == (
        " WHERE case_id = %s AND actor = %s AND decision = %s AND isin = %s "
        "AND trading_date >= %s AND trading_date <= %s"
    )
    assert params == [CASE_ID, "RAILS", "RAIL_BLOCK", ISIN, date(2026, 8, 1), TRADING_DATE]


def test_a_backwards_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="empty journal window"):
        JournalFilter(start=TRADING_DATE, end=date(2026, 8, 1))


def test_entries_come_back_newest_session_first(
    journal: Journal, conn: RecordingConnection
) -> None:
    journal.entries(JournalFilter(actor=Actor.T1), limit=5)

    sql, params = conn.last
    assert sql.endswith("ORDER BY trading_date DESC, id DESC LIMIT %s")
    assert params == ["T1", 5]


def test_a_non_positive_limit_is_a_caller_bug(journal: Journal) -> None:
    with pytest.raises(ValueError, match="limit must be positive"):
        journal.entries(limit=0)


def test_count_uses_the_same_filter(journal: Journal, conn: RecordingConnection) -> None:
    conn.rows = [(3,)]

    assert journal.count(JournalFilter(case_id=CASE_ID)) == 3
    assert conn.last[0] == "SELECT count(*) FROM decision_journal WHERE case_id = %s"


def test_latest_asks_for_one_row(journal: Journal, conn: RecordingConnection) -> None:
    conn.rows = [row_for(id=11)]
    found = journal.latest(JournalFilter(case_id=CASE_ID))

    assert found is not None and found.id == 11
    assert conn.last[1] == [CASE_ID, 1]


def test_an_unknown_entry_id_raises(journal: Journal) -> None:
    with pytest.raises(UnknownEntryError, match="id 404"):
        journal.get(404)


def test_a_row_round_trips_through_the_read_mapping(
    journal: Journal, conn: RecordingConnection
) -> None:
    """Every read column lands in the right field; an off-by-one here mis-reports a decision."""
    conn.rows = [
        row_for(
            id=42,
            decision="SELL",
            actor="T1",
            isin=ISIN,
            sleeve="CORE",
            break_conditions_evaluated=[
                {"id": "BC1", "verdict": "BROKEN", "observed": "segment revenue fell"}
            ],
            rationale="BC1 met",
            model="stub-strong-1",
            tokens_in=1200,
            tokens_out=300,
            cost_inr=Decimal("4.125000"),
            orders_ref="ORD-9",
            payload={"trigger": "results_filing"},
        )
    ]

    found = journal.get(42)

    assert (found.id, found.decision, found.actor) == (42, Decision.SELL, Actor.T1)
    assert (found.isin, found.sleeve) == (ISIN, Sleeve.CORE)
    assert found.break_conditions_evaluated[0].verdict is Verdict.BROKEN
    assert found.tokens is not None and found.tokens.cost_inr == Decimal("4.125000")
    assert found.orders_ref == "ORD-9"
    assert found.payload == {"trigger": "results_filing"}
    assert found.recorded_at == RECORDED_AT


# ── acceptance 1, offline half: nothing here can mutate a journal row ─────────────────────────
#
# The live proof that Postgres refuses UPDATE, DELETE and TRUNCATE is in
# tests/integration/test_journal.py. These two check the other two halves of the same claim: that
# the schema declares the enforcement, and that no code in this package can even attempt one.


def _table_block(sql: str, table: str) -> str:
    match = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", sql, re.DOTALL)
    assert match, f"no CREATE TABLE {table} in {INIT_MIGRATION.name}"
    return match.group(1)


def _check_values(block: str, column: str) -> set[str]:
    """The allowed values of a `column text CHECK (column IN (...))` constraint."""
    match = re.search(rf"CHECK \({column} IN \((.*?)\)\)", block, re.DOTALL)
    assert match, f"no CHECK (...) IN constraint on {column}"
    return set(re.findall(r"'([A-Z_0-9]+)'", match.group(1)))


@pytest.fixture(scope="module")
def init_migration() -> str:
    return INIT_MIGRATION.read_text(encoding="utf-8")


def test_the_schema_rejects_mutation_of_the_journal_at_the_database_level(
    init_migration: str,
) -> None:
    """Acceptance 1, declared. The live refusal is asserted in the integration suite.

    Statement-level triggers, not row-level: a `DELETE ... WHERE false` matches no rows and must
    still fail, or a caller could believe deletion is permitted and simply narrow its predicate.
    """
    assert re.search(
        r"CREATE TRIGGER decision_journal_append_only\s+BEFORE UPDATE OR DELETE ON "
        r"decision_journal\s+FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation\(\);",
        init_migration,
    )
    assert re.search(
        r"CREATE TRIGGER decision_journal_no_truncate\s+BEFORE TRUNCATE ON decision_journal\s+"
        r"FOR EACH STATEMENT EXECUTE FUNCTION reject_mutation\(\);",
        init_migration,
    )
    assert "REVOKE UPDATE, DELETE ON decision_journal FROM PUBLIC;" in init_migration


@pytest.mark.parametrize(
    ("column", "enum"),
    [("actor", Actor), ("decision", Decision), ("sleeve", Sleeve)],
)
def test_the_models_enums_match_the_schemas_check_constraints(
    init_migration: str, column: str, enum: type[Actor] | type[Decision] | type[Sleeve]
) -> None:
    """A value this model accepts and the database rejects fails mid-decision, in production."""
    block = _table_block(init_migration, "decision_journal")

    assert _check_values(block, column) == {member.value for member in enum}


def _string_constants(source: str) -> Iterator[str]:
    """Every string literal in a module except its docstrings.

    Docstrings are excluded because this file's subject is prose-heavy: `writer.py` explains at
    length that it never updates or deletes, and a scan that could not tell an explanation from a
    statement would either flag the explanation or force it to be written in code-safe euphemism.
    """
    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.value


#: SQL that would change or remove something already written (invariant #12). `UPDATE` and
#: `DELETE` need a following token so that the English words, which are common in this package's
#: error messages, are not mistaken for statements.
MUTATING_SQL = re.compile(
    r"\b(UPDATE\s+\w|DELETE\s+FROM|TRUNCATE|DROP\s+TABLE|ALTER\s+TABLE)", re.IGNORECASE
)

#: Filesystem calls that could edit or remove a stored evidence bundle.
DESTRUCTIVE_CALLS = frozenset(
    {"unlink", "remove", "rmdir", "rmtree", "rename", "replace", "truncate", "write_text"}
)


@pytest.mark.parametrize("module", sorted(path.name for path in JOURNAL_PACKAGE.glob("*.py")))
def test_no_module_in_the_package_can_mutate_what_it_wrote(module: str) -> None:
    """Acceptance 1, client side. Parsed rather than exercised: a behavioural test only shows
    that the paths it happened to call did not mutate anything, which is a weaker claim."""
    source = (JOURNAL_PACKAGE / module).read_text(encoding="utf-8")
    offenders = [literal for literal in _string_constants(source) if MUTATING_SQL.search(literal)]

    assert not offenders, f"{module} contains mutating SQL: {offenders}"

    called = {
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not called & DESTRUCTIVE_CALLS, f"{module} calls {sorted(called & DESTRUCTIVE_CALLS)}"


def test_the_guard_would_notice_a_mutating_statement() -> None:
    """The scan above passes trivially if it has quietly stopped matching anything."""
    source = 'def f():\n    """Never updates."""\n    return "UPDATE decision_journal SET x = 1"\n'
    literals = list(_string_constants(source))

    assert literals == ["UPDATE decision_journal SET x = 1"]
    assert MUTATING_SQL.search(literals[0])
