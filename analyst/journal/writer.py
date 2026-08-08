"""A9: the append-only journal itself — writing decisions down, and reading them back.

`Journal` is the only way `analyst/` touches `decision_journal`. It appends and it selects. There
is no update method, no delete method, and no flag that would add one: the table rejects both at
the database level (`reject_mutation()` triggers plus revoked grants, `0001_init.sql`), and this
module is the client-side half of the same rule (invariant #12). `tests/unit/test_journal.py`
parses this package and fails if a mutating statement ever appears in it, because a behavioural
test only proves that the paths it happened to call did not mutate anything.

Reads live here rather than in a separate module for one reason: the query surface and the insert
must agree, column for column, about what a journal row is. Two files would drift, and the shape
they would drift in — a column added to writes and forgotten in reads — is invisible until
someone tries to reconstruct a decision and finds the field empty.

Writing a decision is two steps and they happen in this order, always:

1. the evidence bundle is stored, content-addressed, in the `EvidenceStore`;
2. the entry, carrying that bundle's `sha256:<hex>`, is inserted.

An interrupted write therefore leaves an unreferenced bundle on disk — harmless, since it is
addressed by its own content and nothing points at it — rather than a journal entry pointing at
evidence that was never written, which would be a decision claiming to be reconstructable when it
is not.

Nothing here commits. The caller owns the transaction, exactly as `dataplatform.store.db` intends,
so the daily loop can journal several entries and one order in a single atomic unit. Time comes
from an injected `Clock` (B10): `ts` is the caller's (it is *when the decision was made*, which a
replay reproduces) and `recorded_at` is this module's (it is when the row landed).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from psycopg.types.json import Json

from analyst.journal.evidence import EvidenceBundle, EvidenceRef, EvidenceStore
from analyst.journal.models import (
    Actor,
    BreakConditionEvaluation,
    Decision,
    JournalEntry,
    RecordedEntry,
    Sleeve,
    TokenSpend,
)
from dataplatform.clock import Clock, SystemClock
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection

__all__ = ["Journal", "JournalError", "JournalFilter", "Reconstruction", "UnknownEntryError"]

_LOG = get_logger(__name__)

#: Columns written on every insert, in one place so the INSERT and its parameter tuple cannot
#: drift apart. `id` and `recorded_at` are excluded: the first is assigned by the database, the
#: second is appended by `_insert_parameters` after the caller-supplied fields.
_WRITE_COLUMNS: Final[tuple[str, ...]] = (
    "ts",
    "trading_date",
    "case_id",
    "actor",
    "decision",
    "isin",
    "sleeve",
    "evidence_snapshot_ref",
    "break_conditions_evaluated",
    "rationale",
    "model",
    "tokens_in",
    "tokens_out",
    "cost_inr",
    "orders_ref",
    "payload",
    "recorded_at",
)

#: Columns selected on every read: the row's identity first, then exactly what was written.
_READ_COLUMNS: Final[tuple[str, ...]] = ("id", *_WRITE_COLUMNS)

_INSERT: Final[str] = (
    f"INSERT INTO decision_journal ({', '.join(_WRITE_COLUMNS)}) "
    f"VALUES ({', '.join(['%s'] * len(_WRITE_COLUMNS))}) "
    f"RETURNING {', '.join(_READ_COLUMNS)}"
)

_SELECT: Final[str] = f"SELECT {', '.join(_READ_COLUMNS)} FROM decision_journal"


class JournalError(Exception):
    """Base for every journal failure, so callers can catch the module."""


class UnknownEntryError(JournalError):
    """No journal entry exists with the requested id."""


@dataclass(frozen=True, slots=True)
class JournalFilter:
    """The §5.7 query surface: by case, by date, by actor, by decision type.

    A value rather than a bag of keyword arguments because it crosses a module boundary
    (CLAUDE.md) — the monitor, the evidence pack and the replay engine all ask the journal
    questions, and a filter they can build, pass around and assert on is one they can share.

    Every field is a conjunction; `None` means "do not constrain". The date bounds are inclusive
    and are on `trading_date`, not `ts`: "what did the agent decide about the 7th" is the question
    a reviewer asks, and a decision made at 19:30 on the 7th and a correction appended on the 8th
    are both about the 7th.
    """

    case_id: str | None = None
    actor: Actor | None = None
    decision: Decision | None = None
    isin: str | None = None
    start: date | None = None
    end: date | None = None

    def __post_init__(self) -> None:
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError(
                f"empty journal window: start {self.start.isoformat()} is after end "
                f"{self.end.isoformat()}"
            )

    def where(self) -> tuple[str, list[Any]]:
        """The SQL `WHERE` clause and its parameters, or `('', [])` for an unconstrained filter.

        Built as parameters rather than interpolated text — the values reaching here include a
        `case_id` that ultimately came from a user, and a journal is a poor place to discover
        string formatting.
        """
        clauses: list[str] = []
        parameters: list[Any] = []
        for column, value in (
            ("case_id", self.case_id),
            ("actor", None if self.actor is None else self.actor.value),
            ("decision", None if self.decision is None else self.decision.value),
            ("isin", self.isin),
        ):
            if value is not None:
                clauses.append(f"{column} = %s")
                parameters.append(value)
        if self.start is not None:
            clauses.append("trading_date >= %s")
            parameters.append(self.start)
        if self.end is not None:
            clauses.append("trading_date <= %s")
            parameters.append(self.end)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", parameters


@dataclass(frozen=True, slots=True)
class Reconstruction:
    """A decision and the exact evidence it was made on — what §8.3.3 replays against."""

    entry: RecordedEntry
    evidence: EvidenceBundle | None
    """None only for entries written without a snapshot; a `HEARTBEAT`'s is never None."""


class Journal:
    """Append-only access to `decision_journal`, with its evidence store attached.

    What it does: appends entries (storing their evidence first), and answers the §5.7 query
    surface — by case, date, actor and decision type — plus the reconstruction of one decision.
    What it assumes: the schema is migrated and the caller owns the transaction. Nothing here
    commits, so a daily loop can write a heartbeat, a rail block and an order atomically.
    What it never does: update, delete or truncate a journal row — there is no method for it, the
    database refuses it, and a correction is an appended entry that supersedes the old one.
    """

    __slots__ = ("_clock", "_conn", "_evidence")

    def __init__(
        self, conn: Connection, *, clock: Clock | None = None, evidence: EvidenceStore | None = None
    ) -> None:
        self._conn = conn
        self._clock = SystemClock() if clock is None else clock
        self._evidence = EvidenceStore() if evidence is None else evidence

    def __repr__(self) -> str:
        return f"{type(self).__name__}(evidence={self._evidence!r})"

    @property
    def evidence(self) -> EvidenceStore:
        """The store this journal snapshots into and reconstructs from."""
        return self._evidence

    # ── writes ───────────────────────────────────────────────────────────────────────────────

    def snapshot(self, bundle: EvidenceBundle) -> EvidenceRef:
        """Store an evidence bundle and return the reference an entry can carry.

        Separate from `append` for the caller that assembles evidence once and writes several
        entries against it — a T0 sweep over twenty holdings shows one bundle and decides twenty
        times, and re-storing it per decision would be twenty verified no-ops.
        """
        return self._evidence.put(bundle)

    def append(
        self, entry: JournalEntry, *, evidence: EvidenceBundle | None = None
    ) -> RecordedEntry:
        """Append one entry, storing `evidence` first when it is supplied.

        What it does: snapshots the bundle (if any), inserts the row, and returns it as the
        database recorded it — with the `id` that `order_.decision_journal_id` will point at.
        What it assumes: `entry.ts` came from the caller's injected clock. `recorded_at` comes
        from this journal's own. A decision whose *schema* requires evidence — `HEARTBEAT` — must
        be built with a reference already on it, since the entry will not validate without one:
        call `snapshot()` first and pass the resulting `ref`. `heartbeat()` does exactly that.
        What it never does: overwrite anything. Passing a bundle whose reference contradicts the
        one already on the entry raises rather than picking a winner, because an entry that names
        evidence other than what it was given is not reconstructable.
        """
        if evidence is not None:
            ref = self.snapshot(evidence)
            if entry.evidence_snapshot_ref not in (None, ref.ref):
                raise JournalError(
                    f"entry names evidence {entry.evidence_snapshot_ref} but was passed a bundle "
                    f"addressing {ref.ref}; one of the two is not what this decision saw"
                )
            entry = entry.model_copy(update={"evidence_snapshot_ref": ref.ref})

        row = self._conn.execute(_INSERT, _insert_parameters(entry, self._clock.now())).fetchone()
        if row is None:  # pragma: no cover - RETURNING always yields a row on a successful insert
            raise JournalError("INSERT ... RETURNING produced no row; the journal write is lost")
        recorded = _recorded_entry(row)

        _LOG.info(
            "journal.append",
            entry_id=recorded.id,
            case_id=recorded.case_id,
            trading_date=recorded.trading_date.isoformat(),
            actor=recorded.actor.value,
            decision=recorded.decision.value,
            isin=recorded.isin,
            sleeve=None if recorded.sleeve is None else recorded.sleeve.value,
            evidence=recorded.evidence_snapshot_ref,
            orders_ref=recorded.orders_ref,
        )
        return recorded

    def heartbeat(
        self,
        evidence: EvidenceBundle,
        *,
        trading_date: date | None = None,
        case_id: str | None = None,
        actor: Actor = Actor.T0,
        rationale: str | None = None,
        break_conditions_evaluated: Sequence[BreakConditionEvaluation] = (),
    ) -> RecordedEntry:
        """Record that the agent checked and nothing happened (invariant #9).

        What it does: stores the evidence considered and appends a `HEARTBEAT` entry naming it.
        This is a first-class decision, not the absence of one: a day with no row and a day the
        agent looked at twenty holdings and found every thesis intact are indistinguishable
        afterwards, and §0 is the claim that they never will be here.
        What it assumes: `evidence` is what was actually examined. A heartbeat over an empty
        bundle is refused by `EvidenceBundle` itself.
        What it never does: default the trading date to today behind the caller's back — it takes
        it from the bundle, which is the session the evidence is about.
        """
        # Stored before the entry is built, not just before it is written: a HEARTBEAT does not
        # validate without its reference (invariant #9), so the bundle has to exist first.
        ref = self.snapshot(evidence)
        return self.append(
            JournalEntry(
                ts=self._clock.now(),
                trading_date=evidence.trading_date if trading_date is None else trading_date,
                case_id=evidence.case_id if case_id is None else case_id,
                actor=actor,
                decision=Decision.HEARTBEAT,
                evidence_snapshot_ref=ref.ref,
                rationale=rationale,
                break_conditions_evaluated=tuple(break_conditions_evaluated),
            )
        )

    # ── reads ────────────────────────────────────────────────────────────────────────────────

    def entries(
        self, journal_filter: JournalFilter | None = None, *, limit: int | None = None
    ) -> tuple[RecordedEntry, ...]:
        """Entries matching a filter, newest session first, ties broken by insertion order.

        The ordering is `(trading_date DESC, id DESC)` — the index the schema ships — so a review
        of the last N decisions is the natural read, and two entries about the same session come
        back in the order they were appended, reversed.
        """
        if limit is not None and limit <= 0:
            raise ValueError(f"limit must be positive, got {limit}")
        clause, parameters = (JournalFilter() if journal_filter is None else journal_filter).where()
        sql = f"{_SELECT}{clause} ORDER BY trading_date DESC, id DESC"
        if limit is not None:
            sql += " LIMIT %s"
            parameters = [*parameters, limit]
        rows = self._conn.execute(sql, parameters).fetchall()
        return tuple(_recorded_entry(row) for row in rows)

    def latest(self, journal_filter: JournalFilter | None = None) -> RecordedEntry | None:
        """The most recent entry matching a filter, or None when there is none."""
        found = self.entries(journal_filter, limit=1)
        return found[0] if found else None

    def count(self, journal_filter: JournalFilter | None = None) -> int:
        """How many entries match a filter. The evidence pack's counting question (§5.7)."""
        clause, parameters = (JournalFilter() if journal_filter is None else journal_filter).where()
        row = self._conn.execute(
            f"SELECT count(*) FROM decision_journal{clause}", parameters
        ).fetchone()
        return 0 if row is None else int(row[0])

    def get(self, entry_id: int) -> RecordedEntry:
        """One entry by id, or raise `UnknownEntryError`."""
        row = self._conn.execute(f"{_SELECT} WHERE id = %s", (entry_id,)).fetchone()
        if row is None:
            raise UnknownEntryError(f"no decision_journal entry with id {entry_id}")
        return _recorded_entry(row)

    def reconstruct(self, entry_id: int) -> Reconstruction:
        """One decision together with the exact evidence it was made on (§8.3.3).

        The evidence is re-hashed on the way out of the store, so a bundle that has been damaged
        since the decision raises rather than being presented as what the agent saw.
        """
        entry = self.get(entry_id)
        bundle = (
            None
            if entry.evidence_snapshot_ref is None
            else self._evidence.load(entry.evidence_snapshot_ref)
        )
        return Reconstruction(entry=entry, evidence=bundle)


def _insert_parameters(entry: JournalEntry, recorded_at: datetime) -> tuple[Any, ...]:
    """The parameter tuple for `_INSERT`, in `_WRITE_COLUMNS` order.

    `break_conditions_evaluated` and `payload` are wrapped in `Json` because psycopg will not
    adapt a Python list or dict to `jsonb` on its own — an unwrapped dict reaches the server as a
    text literal and the insert fails at the column type, which is a loud failure but a confusing
    one.
    """
    tokens: TokenSpend | None = entry.tokens
    return (
        entry.ts,
        entry.trading_date,
        entry.case_id,
        entry.actor.value,
        entry.decision.value,
        entry.isin,
        None if entry.sleeve is None else entry.sleeve.value,
        entry.evidence_snapshot_ref,
        Json([condition.model_dump(mode="json") for condition in entry.break_conditions_evaluated]),
        entry.rationale,
        entry.model,
        None if tokens is None else tokens.tokens_in,
        None if tokens is None else tokens.tokens_out,
        None if tokens is None else tokens.cost_inr,
        entry.orders_ref,
        Json(dict(entry.payload)),
        recorded_at,
    )


def _recorded_entry(row: Sequence[Any]) -> RecordedEntry:
    """Build a `RecordedEntry` from a row selected in `_READ_COLUMNS` order.

    Re-validating through the model rather than trusting the row is deliberate: the schema's CHECK
    constraints and this module's rules overlap but are not identical, and a row that predates a
    rule should fail here — visibly, on the entry that has it — rather than be handed to a caller
    as a well-formed entry.
    """
    tokens_in, tokens_out, cost_inr = row[12], row[13], row[14]
    tokens = (
        None
        if tokens_in is None and tokens_out is None and cost_inr is None
        else TokenSpend(
            tokens_in=int(tokens_in or 0),
            tokens_out=int(tokens_out or 0),
            # `Decimal(...)` rather than a fallback of `0`: the column is NUMERIC, so psycopg
            # hands back a Decimal, and a bare int here would put a non-Decimal into a money field.
            cost_inr=Decimal(0) if cost_inr is None else Decimal(cost_inr),
        )
    )
    return RecordedEntry(
        id=int(row[0]),
        ts=row[1],
        trading_date=row[2],
        case_id=row[3],
        actor=Actor(row[4]),
        decision=Decision(row[5]),
        isin=row[6],
        sleeve=None if row[7] is None else Sleeve(row[7]),
        evidence_snapshot_ref=row[8],
        break_conditions_evaluated=tuple(
            BreakConditionEvaluation.model_validate(condition) for condition in (row[9] or ())
        ),
        rationale=row[10],
        model=row[11],
        tokens=tokens,
        orders_ref=row[15],
        payload=dict(row[16] or {}),
        recorded_at=row[17],
    )
