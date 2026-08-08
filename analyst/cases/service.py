"""A1: the case service — CRUD, the §5.1 lifecycle, versioned policy sets, SIP and the case views.

Everything that changes a case goes through here, and everything that goes through here is
journaled. `lifecycle.py` says which moves are legal and `policies.py` says what a policy set is;
this module is those rules against `case_` and `policy_set`, plus the A9 write that makes each one
reviewable afterwards.

Three design points worth knowing before reading the code:

**Where a proposal lives.** `policy_set` has `ratified_by`, `ratified_at` and `ratification_kind`
as NOT NULL columns, so every row in it is, by construction, a ratified version — which is what
makes "which rails were in force when that order was placed" answerable from one table. A
*proposal* is therefore not a `policy_set` row: it sits on `case_.config` under `pending_policy`
until someone ratifies it, at which point it is inserted as the next version and cleared from the
case. A version number is never reused: the append-only trigger and `policy_set_version_unique`
make the rewrite impossible at the database, and `ratify()` refuses it before it gets there.

**Why every transition is a `POLICY_PROPOSAL` journal entry.** §5.7 fixes the journal's decision
vocabulary — `HOLD|BUY|SELL|ESCALATE|SKIP_DATA_RED|RAIL_BLOCK|POLICY_PROPOSAL` — and the
`decision_journal` CHECK constraint mirrors it exactly. Of those, `POLICY_PROPOSAL` is the
governance member, and §5.7 names `USER` as the actor for "a ratification, a graduation, a manual
instruction", so the plan clearly expects governance events to land under it. Every entry this
module writes therefore carries `payload["event"]` naming what actually happened
(`CASE_TRANSITION`, `POLICY_RATIFIED`, `POLICY_REVISED`, …) with the states in
`payload["from"]`/`payload["to"]`, so a reader is never left inferring it from prose. A dedicated
`CASE_LIFECYCLE` decision value would read better and is in `ops/BACKLOG.md`; it needs a migration
that alters a CHECK constraint on the journal, which is not this task's blast radius.

**Nothing here commits, and nothing here reads the wall clock.** The caller owns the transaction
(`dataplatform.store.db.connection`), so a transition and its journal entry land together or not
at all — a state change with no journal line would be exactly the hole invariant #9 exists to
close. Time comes from an injected `Clock` (B10).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from psycopg.types.json import Json

from analyst.cases.lifecycle import (
    PRE_FUNDING_STATES,
    CaseState,
    FundingMode,
    IllegalTransitionError,
    LifecycleError,
    check_funding,
    check_transition,
)
from analyst.cases.policies import (
    PolicyError,
    PolicySet,
    PolicyStatus,
    PolicyVersionError,
    Ratification,
    RatificationKind,
    policy_digest,
)
from analyst.journal import Actor, Decision, Journal, JournalEntry, RecordedEntry
from dataplatform.clock import IST, Clock, SystemClock
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection

__all__ = [
    "CaseError",
    "CaseNotFoundError",
    "CaseRecord",
    "CaseService",
    "CaseSummary",
    "CrossCaseExposure",
    "DuplicateCaseError",
    "NoPolicyError",
    "SipInstalment",
    "UnknownPolicyVersionError",
]

_LOG = get_logger(__name__)

#: Key under which a case carries its un-ratified policy set. One key rather than a second table:
#: a proposal is a draft of a `policy_set` row, and giving it storage of its own invites the
#: question of what a proposal that was never ratified means to a query over policy history.
PENDING_POLICY_KEY: Final = "pending_policy"

#: `case_` columns, in one place so every read, insert and update agrees about the row shape.
_CASE_COLUMNS: Final[tuple[str, ...]] = (
    "case_id",
    "title",
    "state",
    "funding_mode",
    "theme",
    "horizon_years",
    "benchmark_primary",
    "benchmark_secondary",
    "sip_amount_inr",
    "sip_day_of_month",
    "config",
    "created_at",
    "updated_at",
)

#: `policy_set` columns, minus the identity the database assigns.
_POLICY_COLUMNS: Final[tuple[str, ...]] = (
    "case_id",
    "version",
    "supersedes_version",
    "policy",
    "rotation_dial_pct",
    "max_position_pct",
    "max_sector_pct",
    "min_holdings",
    "drawdown_review_pct",
    "ratified_by",
    "ratified_at",
    "ratification_kind",
    "recorded_at",
)

_CASE_SELECT: Final = f"SELECT {', '.join(_CASE_COLUMNS)} FROM case_"
_CASE_INSERT: Final = (
    f"INSERT INTO case_ ({', '.join(_CASE_COLUMNS)}) "
    f"VALUES ({', '.join(['%s'] * len(_CASE_COLUMNS))}) "
    f"RETURNING {', '.join(_CASE_COLUMNS)}"
)
#: Writes the whole row rather than the fields that changed. A case row is small, and one update
#: statement means there is exactly one place where a new column would have to be remembered.
_CASE_UPDATE: Final = (
    "UPDATE case_ SET "
    + ", ".join(f"{column} = %s" for column in _CASE_COLUMNS[1:])
    + " WHERE case_id = %s "
    f"RETURNING {', '.join(_CASE_COLUMNS)}"
)

_POLICY_SELECT: Final = f"SELECT {', '.join(_POLICY_COLUMNS)} FROM policy_set"
_POLICY_INSERT: Final = (
    f"INSERT INTO policy_set ({', '.join(_POLICY_COLUMNS)}) "
    f"VALUES ({', '.join(['%s'] * len(_POLICY_COLUMNS))}) "
    f"RETURNING {', '.join(_POLICY_COLUMNS)}"
)

#: Net executed quantity per (case, ISIN) — the cross-case concentration input A8 needs (§5.2's
#: "cross-case concentration" rail). Quantities, not values: valuing a position needs a price, the
#: price layer is D4's, and a case service that reached into it would own two jobs. A8 multiplies
#: by the price it is already holding for the order it is checking.
_EXPOSURE_SQL: Final = """
    SELECT o.isin,
           o.case_id,
           SUM(CASE WHEN o.side = 'BUY' THEN o.filled_quantity ELSE -o.filled_quantity END)
    FROM order_ o
    JOIN case_ c ON c.case_id = o.case_id
    WHERE o.state = 'EXECUTED' AND c.state = ANY(%s)
    GROUP BY o.isin, o.case_id
    HAVING SUM(CASE WHEN o.side = 'BUY' THEN o.filled_quantity ELSE -o.filled_quantity END) > 0
    ORDER BY o.isin, o.case_id
"""

#: Case states whose holdings count toward cross-case concentration. A suspended case still holds
#: stock, so its positions still crowd another case's cap; a closed one does not.
_HOLDING_STATES: Final[tuple[str, ...]] = (
    CaseState.FUNDED.value,
    CaseState.ACTIVE.value,
    CaseState.SUSPENDED.value,
)


class CaseError(Exception):
    """Base for every case-service failure, so callers can catch the module."""


class CaseNotFoundError(CaseError):
    """No case exists with that id."""


class DuplicateCaseError(CaseError):
    """A case id that already exists was created again."""


class NoPolicyError(CaseError):
    """An operation needed a policy set the case does not have — ratified or pending."""


class UnknownPolicyVersionError(CaseError):
    """A policy version was requested that this case never had."""


@dataclass(frozen=True, slots=True)
class CaseRecord:
    """One `case_` row as a value.

    What it does: carries the case's identity, §5.1 state, funding mode, descriptive fields and
    its `config` blob (which is where a pending proposal lives).
    What it assumes: `created_at`/`updated_at` came from an injected clock.
    What it never does: mutate, or transition itself. State changes go through `CaseService` so
    that no path exists which changes a state without journaling it.
    """

    case_id: str
    title: str
    state: CaseState
    funding_mode: FundingMode
    theme: str | None
    horizon_years: int | None
    benchmark_primary: str | None
    benchmark_secondary: str | None
    sip_amount_inr: Decimal | None
    sip_day_of_month: int | None
    config: Mapping[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SipInstalment:
    """One capital instalment that falls due (§5.2's capital plan, scheduled).

    A value rather than a bare `(case_id, date)` because it crosses into A7: the cash manager
    parks the instalment and the deployment queue spends it, and both want the amount alongside
    the date without re-reading the policy set.
    """

    case_id: str
    due_date: date
    amount_inr: Decimal


@dataclass(frozen=True, slots=True)
class CrossCaseExposure:
    """One instrument's net position across every case that holds it.

    The input A8 needs for the cross-case concentration rail: two cases each holding 12% of
    themselves in one stock is a 24% household exposure that neither case's own rails can see.
    """

    isin: str
    quantity: int
    by_case: Mapping[str, int]

    @property
    def case_count(self) -> int:
        """How many cases hold it. One is ordinary; two or more is what the rail is watching."""
        return len(self.by_case)


@dataclass(frozen=True, slots=True)
class CaseSummary:
    """One line of the multi-case view: where a case stands and what governs it."""

    case_id: str
    title: str
    state: CaseState
    funding_mode: FundingMode
    theme: str | None
    policy_version: int | None
    ratification_kind: RatificationKind | None
    has_pending_proposal: bool
    next_sip_date: date | None
    sip_amount_inr: Decimal | None

    @property
    def is_governed(self) -> bool:
        """Whether a ratified policy set exists — decisions are impossible without one."""
        return self.policy_version is not None


class CaseService:
    """The A1 case service: CRUD, §5.1 transitions, §5.2 policy versions, SIP and the case views.

    What it does: owns every write to `case_` and `policy_set`, applies `lifecycle.py`'s rules
    before each one, and appends the A9 entry that records it.
    What it assumes: the schema is migrated, the caller owns the transaction, and the `Journal` it
    is given writes to the same connection — so a transition and its journal entry commit
    together.
    What it never does: mutate a ratified policy version, transition a case without journaling it,
    or fund real money on anything but a `HUMAN` ratification.
    """

    __slots__ = ("_clock", "_conn", "_journal")

    def __init__(self, conn: Connection, *, journal: Journal, clock: Clock | None = None) -> None:
        self._conn = conn
        self._journal = journal
        self._clock = SystemClock() if clock is None else clock

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    # ── CRUD ─────────────────────────────────────────────────────────────────────────────────

    def create(
        self, case_id: str, title: str, *, theme: str | None = None, by: str = "agent"
    ) -> CaseRecord:
        """Create a case in `DRAFT` and journal its existence.

        What it does: inserts the row with `funding_mode = PAPER` (decision #8: every case starts
        on paper) and writes the entry that opens this case's journal.
        What it assumes: `case_id` is the caller's stable identifier — it is the foreign key every
        order, thesis and journal entry will carry, so it is never regenerated.
        What it never does: accept a state. A case that could be created directly in `ACTIVE`
        would skip every gate §5.1 exists to impose.
        """
        if not case_id.strip() or not title.strip():
            raise ValueError("a case needs a non-blank case_id and title")
        if self.get(case_id) is not None:
            raise DuplicateCaseError(
                f"case {case_id} already exists; case ids are the foreign key every order and "
                "journal entry carries and are never reused"
            )
        now = self._clock.now()
        record = CaseRecord(
            case_id=case_id,
            title=title,
            state=CaseState.DRAFT,
            funding_mode=FundingMode.PAPER,
            theme=theme,
            horizon_years=None,
            benchmark_primary=None,
            benchmark_secondary=None,
            sip_amount_inr=None,
            sip_day_of_month=None,
            config={},
            created_at=now,
            updated_at=now,
        )
        row = self._conn.execute(_CASE_INSERT, _case_parameters(record)).fetchone()
        if row is None:  # pragma: no cover - RETURNING always yields a row on a successful insert
            raise CaseError(f"INSERT ... RETURNING produced no row for case {case_id}")
        created = _case_record(row)
        self._journal_event(
            created,
            "CASE_CREATED",
            rationale=f"case {case_id} created in DRAFT by {by}: {title}",
            actor=Actor.USER,
            extra={"to": CaseState.DRAFT.value, "title": title},
        )
        return created

    def get(self, case_id: str) -> CaseRecord | None:
        """The case, or None when there is no such id."""
        row = self._conn.execute(f"{_CASE_SELECT} WHERE case_id = %s", (case_id,)).fetchone()
        return None if row is None else _case_record(row)

    def require(self, case_id: str) -> CaseRecord:
        """The case, or raise `CaseNotFoundError`. For callers that know it must exist."""
        record = self.get(case_id)
        if record is None:
            raise CaseNotFoundError(f"no case with id {case_id!r}")
        return record

    def cases(self, *, states: Iterable[CaseState] | None = None) -> tuple[CaseRecord, ...]:
        """Every case, optionally filtered to a set of §5.1 states, ordered by id."""
        if states is None:
            rows = self._conn.execute(f"{_CASE_SELECT} ORDER BY case_id").fetchall()
        else:
            wanted = [state.value for state in states]
            if not wanted:
                return ()
            rows = self._conn.execute(
                f"{_CASE_SELECT} WHERE state = ANY(%s) ORDER BY case_id", (wanted,)
            ).fetchall()
        return tuple(_case_record(row) for row in rows)

    def describe(
        self, case_id: str, *, title: str | None = None, theme: str | None = None
    ) -> CaseRecord:
        """Edit a case's descriptive fields. The only in-place update the service offers.

        What it does: changes the title and/or the theme label.
        What it assumes: descriptive text is not policy — nothing decides anything from these.
        What it never does: touch state, funding mode or anything a policy set governs. Those move
        only through a transition or a new ratified version, which is why this method cannot be
        used as a back door into either.
        """
        record = self.require(case_id)
        if title is None and theme is None:
            raise ValueError("describe() needs a title or a theme to change")
        updated = _replace(
            record,
            title=record.title if title is None else title,
            theme=record.theme if theme is None else theme,
            updated_at=self._clock.now(),
        )
        return self._save(updated)

    def delete(self, case_id: str) -> None:
        """Delete a `DRAFT` case that never had a policy version.

        What it does: removes a case created by mistake, before it has any history.
        What it assumes: nothing references it — a case past `DRAFT` has journal entries, and
        possibly theses and orders, all of which carry `case_id` as a foreign key.
        What it never does: delete a case with history. §5.1 ends at `CLOSED`, and a closed case
        with its journal intact is the record of what was tried; deleting it would erase evidence
        the journal exists to keep (invariant #12's spirit).
        """
        record = self.require(case_id)
        if record.state is not CaseState.DRAFT:
            raise LifecycleError(
                f"case {case_id} is {record.state.value}, not DRAFT: a case with history is "
                "closed (§5.1), never deleted — its journal is the record of what was tried"
            )
        if self.policy_history(case_id):
            raise LifecycleError(
                f"case {case_id} has ratified policy versions; those are append-only "
                "(decisions #4/#5/#9) and the case that owns them cannot be removed"
            )
        self._conn.execute("DELETE FROM case_ WHERE case_id = %s", (case_id,))
        _LOG.info("case.deleted", case_id=case_id)

    # ── §5.1 lifecycle ───────────────────────────────────────────────────────────────────────

    def begin_interview(self, case_id: str, *, by: str = "agent") -> CaseRecord:
        """`DRAFT → INTERVIEW`: A2 starts eliciting the capital plan and the rest of §5.1."""
        return self._transition(
            case_id,
            CaseState.INTERVIEW,
            rationale=f"interview opened by {by}",
            actor=Actor.USER,
        )

    def propose(self, case_id: str, policy: PolicySet, *, by: str = "agent") -> CaseRecord:
        """Attach a proposed §5.2 policy set to the case, moving `INTERVIEW → PROPOSAL`.

        What it does: stores the proposal on the case and, from `INTERVIEW`, transitions to
        `PROPOSAL`. Re-proposing while already in `PROPOSAL` replaces the pending document — a
        proposal under review may be reworked freely, since nothing has been approved yet.
        What it assumes: the proposal is version 1. A change to an already-ratified policy is
        `revise_policy`, which numbers the new version and records what it supersedes.
        What it never does: ratify. Proposing is never ratifying (§5.1).
        """
        record = self.require(case_id)
        if record.state not in (CaseState.INTERVIEW, CaseState.PROPOSAL):
            raise IllegalTransitionError(
                case_id,
                record.state,
                CaseState.PROPOSAL,
                f"legal from {CaseState.INTERVIEW.value}"
                if record.state in PRE_FUNDING_STATES
                else "a funded case changes policy by revise_policy(), which versions the change",
            )
        self._check_proposal(record, policy, expected_version=1)
        staged = self._stage_proposal(record, policy)
        if record.state is CaseState.INTERVIEW:
            return self._transition(
                case_id,
                CaseState.PROPOSAL,
                rationale=f"policy set v{policy.version} proposed by {by} ({policy.content_hash})",
                actor=Actor.USER,
                record=staged,
                extra={"policy_version": str(policy.version), "content_hash": policy.content_hash},
            )
        self._journal_event(
            staged,
            "POLICY_PROPOSED",
            rationale=f"policy set v{policy.version} re-proposed by {by} ({policy.content_hash})",
            actor=Actor.USER,
            extra={"policy_version": str(policy.version), "content_hash": policy.content_hash},
        )
        return staged

    def revise_policy(self, case_id: str, *, by: str = "agent", **changes: Any) -> PolicySet:
        """Edit the ratified policy set: build version N+1 in `PROPOSAL`, leaving N untouched.

        What it does: takes the current ratified version, applies `changes` to the §5.2 policies,
        and stages the result as the case's pending proposal awaiting a fresh ratification.
        What it assumes: a ratified version exists — there is nothing to revise otherwise.
        What it never does: change version N, or let the case act on N+1. The case keeps running
        under the ratified version until a human ratifies the new one (decisions #4/#5/#9), which
        is the entire reason a policy change is a version rather than an edit.
        """
        record = self.require(case_id)
        current = self.current_policy(case_id)
        if current is None:
            raise NoPolicyError(
                f"case {case_id} has no ratified policy set to revise; propose() one first"
            )
        revised = current.revise(**changes)
        staged = self._stage_proposal(record, revised)
        self._journal_event(
            staged,
            "POLICY_REVISED",
            rationale=(
                f"policy set v{revised.version} proposed by {by}, superseding "
                f"v{current.version}; changed: {', '.join(sorted(changes))}. "
                f"v{current.version} remains in force until this is ratified."
            ),
            actor=Actor.USER,
            extra={
                "policy_version": str(revised.version),
                "supersedes_version": str(current.version),
                "content_hash": revised.content_hash,
                "changed": ",".join(sorted(changes)),
            },
        )
        return revised

    def ratify(
        self,
        case_id: str,
        *,
        by: str,
        kind: RatificationKind = RatificationKind.HUMAN,
        at: datetime | None = None,
    ) -> PolicySet:
        """Ratify the pending proposal: write it as the next `policy_set` version.

        What it does: pins the ratification to the proposal's content hash, inserts the version,
        clears the pending proposal, promotes the rail scalars onto the row A8 reads, and — when
        the case is in `PROPOSAL` — moves it to `RATIFIED`. A case already past funding stays
        where it is; ratifying a revision changes which policy governs it, not its lifecycle
        state.
        What it assumes: `kind` is the truth about who approved. `FIXTURE` is B9's path and is
        valid for paper and tests only.
        What it never does: overwrite a version. The version number must be exactly one past the
        current one, `policy_set` is append-only, and `policy_set_version_unique` is the backstop.
        """
        record = self.require(case_id)
        proposal = self.pending_proposal(case_id)
        if proposal is None:
            raise NoPolicyError(
                f"case {case_id} has no pending proposal to ratify; propose() or revise_policy() "
                "one first"
            )
        current = self.current_policy(case_id)
        expected = 1 if current is None else current.version + 1
        if proposal.version != expected:
            raise PolicyVersionError(
                f"case {case_id}: pending proposal is v{proposal.version} but the next version is "
                f"v{expected}; a version number is never reused (policy_set is append-only)"
            )
        ratified = proposal.ratified_with(
            Ratification(
                by=by,
                at=self._clock.now() if at is None else at,
                kind=kind,
                content_hash=proposal.content_hash,
            )
        )
        row = self._conn.execute(
            _POLICY_INSERT, _policy_parameters(ratified, self._clock.now())
        ).fetchone()
        if row is None:  # pragma: no cover - RETURNING always yields a row on a successful insert
            raise CaseError(f"INSERT ... RETURNING produced no row for {case_id} policy")
        stored = _policy_set(row, status=PolicyStatus.RATIFIED)

        promoted = _replace(
            _without_pending_policy(record),
            horizon_years=stored.horizon.horizon_years,
            benchmark_primary=stored.horizon.benchmark_primary,
            benchmark_secondary=stored.horizon.benchmark_secondary,
            sip_amount_inr=stored.capital_plan.sip_amount_inr,
            sip_day_of_month=stored.capital_plan.day_of_month,
            updated_at=self._clock.now(),
        )
        saved = self._save(promoted)
        rationale = (
            f"policy set v{stored.version} ratified by {by} as {kind.value} ({stored.content_hash})"
        )
        extra = {
            "policy_version": str(stored.version),
            "ratification_kind": kind.value,
            "ratified_by": by,
            "content_hash": stored.content_hash,
        }
        if saved.state is CaseState.PROPOSAL:
            self._transition(
                case_id,
                CaseState.RATIFIED,
                rationale=rationale,
                actor=Actor.USER,
                record=saved,
                extra=extra,
            )
        else:
            self._journal_event(saved, "POLICY_RATIFIED", rationale=rationale, extra=extra)
        return stored

    def fund(self, case_id: str, mode: FundingMode, *, by: str) -> CaseRecord:
        """`RATIFIED → FUNDED`, in paper or real mode.

        What it does: checks the *current ratified* policy set's ratification kind against the
        mode being asked for, then records the commitment of capital.
        What it assumes: `by` names whoever committed the capital; it lands in the journal.
        What it never does: fund real money on a fixture ratification. B9 says a fixture
        ratification is never valid for real money, so `FUNDED(real)` on one raises
        `FundingRefusedError` — there is no flag, no environment variable and no test hook that
        changes that answer.
        """
        record = self.require(case_id)
        policy = self.current_policy(case_id)
        if policy is None or policy.ratification is None:
            raise NoPolicyError(
                f"case {case_id} cannot be funded: no ratified policy set governs it, so there "
                "are no rails, no dial and no exit menu for its first order to obey"
            )
        check_funding(case_id, mode, policy.ratification.kind)
        funded = self._transition(
            case_id,
            CaseState.FUNDED,
            rationale=(
                f"funded in {mode.value} mode by {by} under policy v{policy.version} "
                f"({policy.ratification.kind.value} ratification by {policy.ratification.by})"
            ),
            actor=Actor.USER,
            record=_replace(record, funding_mode=mode),
            extra={
                "funding_mode": mode.value,
                "policy_version": str(policy.version),
                "ratification_kind": policy.ratification.kind.value,
            },
        )
        return funded

    def activate(self, case_id: str, *, by: str = "agent") -> CaseRecord:
        """`FUNDED → ACTIVE`: the daily loop may now decide for this case."""
        return self._transition(
            case_id,
            CaseState.ACTIVE,
            rationale=f"case activated by {by}; the daily loop now decides for it",
            actor=Actor.USER,
        )

    def suspend(self, case_id: str, *, reason: str, actor: Actor = Actor.SYSTEM) -> CaseRecord:
        """`ACTIVE → SUSPENDED`: monitoring continues, no new orders.

        `reason` is mandatory and lands in the journal: a suspension nobody can explain later is
        indistinguishable from an outage, and §5.7's decision review needs to tell them apart.
        """
        if not reason.strip():
            raise ValueError("a suspension needs a reason; it is the journal's only account of it")
        return self._transition(
            case_id,
            CaseState.SUSPENDED,
            rationale=f"case suspended: {reason}",
            actor=actor,
        )

    def resume(self, case_id: str, *, reason: str, actor: Actor = Actor.USER) -> CaseRecord:
        """`SUSPENDED → ACTIVE`: whatever caused the pause is resolved."""
        if not reason.strip():
            raise ValueError("resuming needs a reason: what changed since the suspension?")
        return self._transition(
            case_id, CaseState.ACTIVE, rationale=f"case resumed: {reason}", actor=actor
        )

    def close(self, case_id: str, *, reason: str, actor: Actor = Actor.USER) -> CaseRecord:
        """`ACTIVE|SUSPENDED → CLOSED`: terminal. The journal and policy history stay."""
        if not reason.strip():
            raise ValueError("closing a case needs a reason; it is the last entry in its journal")
        return self._transition(
            case_id, CaseState.CLOSED, rationale=f"case closed: {reason}", actor=actor
        )

    # ── policy versions ──────────────────────────────────────────────────────────────────────

    def current_policy(self, case_id: str) -> PolicySet | None:
        """The ratified version in force, or None when the case has never been ratified."""
        row = self._conn.execute(
            f"{_POLICY_SELECT} WHERE case_id = %s ORDER BY version DESC LIMIT 1", (case_id,)
        ).fetchone()
        return None if row is None else _policy_set(row, status=PolicyStatus.RATIFIED)

    def require_policy(self, case_id: str) -> PolicySet:
        """The ratified version in force, or raise. What every decision path should call."""
        policy = self.current_policy(case_id)
        if policy is None:
            raise NoPolicyError(
                f"case {case_id} has no ratified policy set; no decision may be made for it"
            )
        return policy

    def policy_version(self, case_id: str, version: int) -> PolicySet:
        """One historical version — "which rails were in force when that order was placed"."""
        row = self._conn.execute(
            f"{_POLICY_SELECT} WHERE case_id = %s AND version = %s", (case_id, version)
        ).fetchone()
        if row is None:
            raise UnknownPolicyVersionError(f"case {case_id} has no policy version {version}")
        current = self.current_policy(case_id)
        status = (
            PolicyStatus.RATIFIED
            if current is not None and current.version == version
            else PolicyStatus.SUPERSEDED
        )
        return _policy_set(row, status=status)

    def policy_history(self, case_id: str) -> tuple[PolicySet, ...]:
        """Every ratified version, oldest first; the newest is `RATIFIED`, the rest `SUPERSEDED`."""
        rows = self._conn.execute(
            f"{_POLICY_SELECT} WHERE case_id = %s ORDER BY version", (case_id,)
        ).fetchall()
        if not rows:
            return ()
        latest = len(rows) - 1
        return tuple(
            _policy_set(
                row,
                status=PolicyStatus.RATIFIED if index == latest else PolicyStatus.SUPERSEDED,
            )
            for index, row in enumerate(rows)
        )

    def pending_proposal(self, case_id: str) -> PolicySet | None:
        """The un-ratified policy set staged on this case, or None."""
        record = self.require(case_id)
        document = record.config.get(PENDING_POLICY_KEY)
        if document is None:
            return None
        return PolicySet.model_validate(document)

    # ── views ────────────────────────────────────────────────────────────────────────────────

    def overview(self, *, on: date | None = None) -> tuple[CaseSummary, ...]:
        """The multi-case view: one line per case, with what governs it and what it owes.

        One row per case regardless of whether it has a policy — a case stuck in `INTERVIEW` for a
        month is exactly what this view exists to make visible, and filtering it out would hide
        the thing worth seeing.
        """
        today = self._clock.today() if on is None else on
        current = self._current_policies()
        summaries: list[CaseSummary] = []
        for record in self.cases():
            policy = current.get(record.case_id)
            plan = None if policy is None else policy.capital_plan
            summaries.append(
                CaseSummary(
                    case_id=record.case_id,
                    title=record.title,
                    state=record.state,
                    funding_mode=record.funding_mode,
                    theme=record.theme,
                    policy_version=None if policy is None else policy.version,
                    ratification_kind=(
                        None
                        if policy is None or policy.ratification is None
                        else policy.ratification.kind
                    ),
                    has_pending_proposal=PENDING_POLICY_KEY in record.config,
                    next_sip_date=(
                        None
                        if plan is None or record.state is CaseState.CLOSED
                        else plan.next_instalment(today)
                    ),
                    sip_amount_inr=None if plan is None else plan.sip_amount_inr,
                )
            )
        return tuple(summaries)

    def due_sips(self, on: date) -> tuple[SipInstalment, ...]:
        """The SIP instalments falling due on `on`, across every funded case.

        What it does: matches the date against each funded case's ratified capital plan.
        What it assumes: these are *nominal* dates. If the 1st is a holiday, the instalment is
        still due on the 1st; deploying it into a session is A7's job, and deciding that here
        would make the schedule depend on a calendar this module has no reason to hold.
        What it never does: report an instalment for a case that is not funded, or for one whose
        policy was never ratified. Money does not arrive on a proposal.
        """
        current = self._current_policies()
        funded = {
            CaseState.FUNDED,
            CaseState.ACTIVE,
            CaseState.SUSPENDED,
        }
        due: list[SipInstalment] = []
        for record in self.cases(states=funded):
            policy = current.get(record.case_id)
            if policy is None or not policy.capital_plan.is_due(on):
                continue
            due.append(
                SipInstalment(
                    case_id=record.case_id,
                    due_date=on,
                    amount_inr=policy.capital_plan.sip_amount_inr,
                )
            )
        return tuple(due)

    def cross_case_exposure(self) -> tuple[CrossCaseExposure, ...]:
        """Net executed quantity per ISIN across every case holding it — A8's cross-case input.

        What it does: aggregates filled orders into one net position per (ISIN, case), keeping the
        per-case split so a rail breach can name which cases combined to cause it.
        What it assumes: `order_` is the record of what actually filled; `state = 'EXECUTED'` is
        the only state that has moved stock.
        What it never does: value the positions. That needs a price, prices are D4's, and A8 is
        already holding one for the order it is checking.
        """
        rows = self._conn.execute(_EXPOSURE_SQL, (list(_HOLDING_STATES),)).fetchall()
        by_isin: dict[str, dict[str, int]] = {}
        for isin, case_id, quantity in rows:
            by_isin.setdefault(isin, {})[case_id] = int(quantity)
        return tuple(
            CrossCaseExposure(
                isin=isin, quantity=sum(cases.values()), by_case=dict(sorted(cases.items()))
            )
            for isin, cases in sorted(by_isin.items())
        )

    # ── internals ────────────────────────────────────────────────────────────────────────────

    def _current_policies(self) -> Mapping[str, PolicySet]:
        """The in-force version for every case, in one query rather than one per case."""
        rows = self._conn.execute(
            f"SELECT DISTINCT ON (case_id) {', '.join(_POLICY_COLUMNS)} FROM policy_set "
            "ORDER BY case_id, version DESC"
        ).fetchall()
        policies = (_policy_set(row, status=PolicyStatus.RATIFIED) for row in rows)
        return {policy.case_id: policy for policy in policies}

    def _check_proposal(
        self, record: CaseRecord, policy: PolicySet, *, expected_version: int
    ) -> None:
        """Refuse a proposal that does not belong to this case or is not a proposal."""
        if policy.case_id != record.case_id:
            raise PolicyError(
                f"policy set names case {policy.case_id} but was proposed for {record.case_id}"
            )
        if policy.status is not PolicyStatus.PROPOSAL:
            raise PolicyVersionError(
                f"a {policy.status.value} policy set cannot be proposed; proposing is never "
                "ratifying (§5.1)"
            )
        if policy.version != expected_version:
            raise PolicyVersionError(
                f"case {record.case_id}: a first proposal is v{expected_version}, got "
                f"v{policy.version}; use revise_policy() to version a change to a ratified set"
            )

    def _stage_proposal(self, record: CaseRecord, policy: PolicySet) -> CaseRecord:
        """Write the pending proposal onto the case's `config` and return the saved record."""
        config = {**record.config, PENDING_POLICY_KEY: policy.model_dump(mode="json")}
        return self._save(_replace(record, config=config, updated_at=self._clock.now()))

    def _transition(
        self,
        case_id: str,
        to_state: CaseState,
        *,
        rationale: str,
        actor: Actor,
        record: CaseRecord | None = None,
        extra: Mapping[str, str] | None = None,
    ) -> CaseRecord:
        """Apply one §5.1 transition: check it, write it, journal it — in that order.

        The single write path for `case_.state`. Checking first means an illegal move leaves no
        trace of a state that never existed; journaling last means the entry describes a
        transition that actually landed. Both are in one transaction, so there is no window in
        which a case has moved and the journal does not say so (invariant #9).
        """
        current = self.require(case_id) if record is None else record
        check_transition(case_id, current.state, to_state)
        moved = self._save(_replace(current, state=to_state, updated_at=self._clock.now()))
        self._journal_event(
            moved,
            "CASE_TRANSITION",
            rationale=rationale,
            actor=actor,
            extra={"from": current.state.value, "to": to_state.value, **(extra or {})},
        )
        return moved

    def _journal_event(
        self,
        record: CaseRecord,
        event: str,
        *,
        rationale: str,
        actor: Actor = Actor.USER,
        extra: Mapping[str, str] | None = None,
    ) -> RecordedEntry:
        """Append the A9 entry for one governance event on a case.

        `POLICY_PROPOSAL` is the journal's governance decision (§5.7's vocabulary is closed);
        `payload["event"]` says which governance event it actually was, so a reader never has to
        parse the rationale to find out. Strings only in the payload — a number read back out of
        `jsonb` is a float, and a float in this system is a bug.
        """
        now = self._clock.now()
        entry = self._journal.append(
            JournalEntry(
                ts=now,
                trading_date=now.astimezone(IST).date(),
                case_id=record.case_id,
                actor=actor,
                decision=Decision.POLICY_PROPOSAL,
                rationale=rationale,
                payload={"event": event, "state": record.state.value, **(extra or {})},
            )
        )
        # `event` is structlog's own name for the message, so the key is `case_event` — passing
        # `event=` here shadows it and raises rather than logging.
        _LOG.info(
            "case.event",
            case_id=record.case_id,
            case_event=event,
            state=record.state.value,
            funding_mode=record.funding_mode.value,
            journal_id=entry.id,
        )
        return entry

    def _save(self, record: CaseRecord) -> CaseRecord:
        """Write the whole case row and return it as the database recorded it."""
        parameters = _case_parameters(record)
        row = self._conn.execute(_CASE_UPDATE, (*parameters[1:], record.case_id)).fetchone()
        if row is None:
            raise CaseNotFoundError(
                f"UPDATE matched no case with id {record.case_id!r}; it was deleted concurrently"
            )
        return _case_record(row)


# ── row ↔ value mapping ──────────────────────────────────────────────────────────────────────


def _replace(record: CaseRecord, **changes: Any) -> CaseRecord:
    """`dataclasses.replace` for a slotted frozen record, typed for this one class."""
    fields: dict[str, Any] = {
        name: getattr(record, name) for name in CaseRecord.__dataclass_fields__
    }
    fields.update(changes)
    return CaseRecord(**fields)


def _without_pending_policy(record: CaseRecord) -> CaseRecord:
    """The record with its staged proposal removed — called once a version is ratified."""
    config = {key: value for key, value in record.config.items() if key != PENDING_POLICY_KEY}
    return _replace(record, config=config)


def _case_parameters(record: CaseRecord) -> tuple[Any, ...]:
    """The parameter tuple for `_CASE_INSERT`, in `_CASE_COLUMNS` order."""
    return (
        record.case_id,
        record.title,
        record.state.value,
        record.funding_mode.value,
        record.theme,
        record.horizon_years,
        record.benchmark_primary,
        record.benchmark_secondary,
        record.sip_amount_inr,
        record.sip_day_of_month,
        Json(dict(record.config)),
        record.created_at,
        record.updated_at,
    )


def _case_record(row: Sequence[Any]) -> CaseRecord:
    """Build a `CaseRecord` from a row selected in `_CASE_COLUMNS` order."""
    return CaseRecord(
        case_id=row[0],
        title=row[1],
        state=CaseState(row[2]),
        funding_mode=FundingMode(row[3]),
        theme=row[4],
        horizon_years=None if row[5] is None else int(row[5]),
        benchmark_primary=row[6],
        benchmark_secondary=row[7],
        sip_amount_inr=None if row[8] is None else Decimal(row[8]),
        sip_day_of_month=None if row[9] is None else int(row[9]),
        config=dict(row[10] or {}),
        created_at=row[11],
        updated_at=row[12],
    )


def _policy_parameters(policy: PolicySet, recorded_at: datetime) -> tuple[Any, ...]:
    """The parameter tuple for `_POLICY_INSERT`, in `_POLICY_COLUMNS` order.

    The rail scalars are promoted out of the `policy` document into their own columns exactly as
    the schema intends, so A8 reads numbers rather than parsing JSON — and so a rail can be
    compared in SQL by anything auditing orders after the fact.
    """
    if policy.ratification is None:  # pragma: no cover - `ratified_with` guarantees it
        raise PolicyError(f"policy v{policy.version} is not ratified and cannot be stored")
    return (
        policy.case_id,
        policy.version,
        policy.supersedes_version,
        Json(dict(policy.policies)),
        policy.rotation_dial.tactical_pct,
        policy.rails.max_position_pct,
        policy.rails.max_sector_pct,
        policy.rails.min_holdings,
        policy.rails.drawdown_review_pct,
        policy.ratification.by,
        policy.ratification.at,
        policy.ratification.kind.value,
        recorded_at,
    )


def _policy_set(row: Sequence[Any], *, status: PolicyStatus) -> PolicySet:
    """Build a `PolicySet` from a `policy_set` row selected in `_POLICY_COLUMNS` order.

    The seven policies come back from the `policy` document and the ratification from its columns;
    the promoted rail scalars are deliberately *not* read, because `policy` is the ratified
    content and a disagreement between the two must surface as a hash mismatch rather than be
    papered over by preferring one source.
    """
    document = dict(row[3])
    return PolicySet.model_validate(
        {
            **document,
            "case_id": row[0],
            "version": int(row[1]),
            "supersedes_version": None if row[2] is None else int(row[2]),
            "status": status,
            "ratification": {
                "by": row[9],
                "at": row[10],
                "kind": RatificationKind(row[11]),
                "content_hash": _document_hash(document),
            },
        }
    )


def _document_hash(document: Mapping[str, Any]) -> str:
    """The content hash of a stored policy document, recomputed on read.

    Recomputed rather than stored: a hash column could agree with a document that had been edited
    around it, whereas a hash derived from the bytes just read cannot. `PolicySet`'s validator then
    checks the ratification against it, so a `policy_set` row whose JSON was tampered with fails to
    load at all instead of loading as a policy nobody ratified.
    """
    return policy_digest(document)
