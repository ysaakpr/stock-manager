"""M5.3: the case lifecycle is a machine, a ratified policy is immutable, and fixtures buy nothing.

The three claims this task makes, and how each is shown here:

1. **Illegal lifecycle transitions raise; every transition is journaled.** The edge table is
   checked exhaustively — every one of the 64 ordered state pairs is either in §5.1's diagram and
   permitted, or absent from it and refused — and the happy path is then walked through the real
   `CaseService`, asserting one journal entry per move carrying `from`/`to`. A transition that
   raised leaves neither a state change nor an entry.
2. **A ratified policy set cannot be mutated; edits create a new version in `PROPOSAL`.** Three
   layers: the model refuses assignment (`frozen`), `revise()` produces version N+1 in `PROPOSAL`
   while version N keeps its content hash, and `analyst/cases/` is parsed to prove no statement
   in it updates or deletes a `policy_set` row — a behavioural test only shows that the paths it
   happened to call behaved.
3. **`FUNDED(real)` refuses a fixture ratification.** A case ratified under B9's `FIXTURE` kind
   funds in paper and is refused in real, staying `RATIFIED` with nothing journaled claiming
   otherwise; the same case ratified by a human funds in real.

The database is stood in for by `FakeDatabase`, an in-memory `case_`/`policy_set` that echoes
inserts back the way `INSERT ... RETURNING` does — so this file stays offline and fast (CLAUDE.md)
while the service really does round-trip its rows through SQL parameters and back through its own
row mapping. What that cannot prove is what Postgres does with them: the append-only trigger on
`policy_set` is `tests/integration/test_journal.py`'s style of test and belongs with M5's
integration pass.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from psycopg.types.json import Json
from pydantic import ValidationError

from analyst.cases import (
    CLOSED_STATES,
    LEGAL_TRANSITIONS,
    PENDING_POLICY_KEY,
    POLICY_FIELDS,
    PRE_FUNDING_STATES,
    TRADING_STATES,
    CapitalPlan,
    CaseNotFoundError,
    CaseService,
    CaseState,
    CashPolicy,
    DuplicateCaseError,
    ExitMenu,
    ExitStrategy,
    FundingMode,
    FundingRefusedError,
    HorizonAndBenchmarks,
    IllegalTransitionError,
    LifecycleError,
    MonitoringCadence,
    NoPolicyError,
    PolicySet,
    PolicyStatus,
    PolicyVersionError,
    Ratification,
    RatificationKind,
    RatificationMismatchError,
    RiskRails,
    RotationDial,
    T2Cadence,
    TriggerSensitivity,
    UnknownPolicyVersionError,
    check_funding,
    check_transition,
)
from analyst.journal import Actor, Decision, EvidenceStore, Journal
from dataplatform.clock import IST, FrozenClock
from dataplatform.store.db import Connection

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_PACKAGE = REPO_ROOT / "analyst" / "cases"
INIT_MIGRATION = REPO_ROOT / "dataplatform" / "store" / "migrations" / "0001_init.sql"

CASE_ID = "AI_ROBOTICS"
NOW = datetime(2026, 8, 7, 19, 30, tzinfo=IST)
TODAY = NOW.date()

#: A shape-valid placeholder for the liquid-ETF parking instrument. The real LIQUIDCASE/LIQUIDBEES
#: ISIN comes from the identity master (D2) when a proposal is built; nothing here depends on it
#: being a real listing, only on the cash policy naming an ISIN rather than a ticker (invariant #2).
PARKING_ISIN = "INF109AA1234"


# ── the §5.2 example case, as a policy set ───────────────────────────────────────────────────


def example_policies(**overrides: Any) -> dict[str, Any]:
    """§5.2's AI/Robotics example: 10k on the 1st, 5 yr, dial 30%, rails 15/35/8/-25%."""
    policies: dict[str, Any] = {
        "capital_plan": CapitalPlan(sip_amount_inr=Decimal("10000"), day_of_month=1),
        "horizon": HorizonAndBenchmarks(
            horizon_years=5,
            benchmark_primary="NIFTY-TRI",
            benchmark_secondary="NIFTY-IT/CPSE-blend",
        ),
        "rotation_dial": RotationDial(tactical_pct=Decimal("30")),
        "rails": RiskRails(
            max_position_pct=Decimal("15"),
            max_sector_pct=Decimal("35"),
            min_holdings=8,
            drawdown_review_pct=Decimal("25"),
            max_order_value_inr=Decimal("50000"),
            max_order_pct_of_case=Decimal("10"),
        ),
        "exit_menu": ExitMenu(
            allowed=(ExitStrategy.STAGED, ExitStrategy.IMMEDIATE),
            default=ExitStrategy.STAGED,
            immediate_allowed_on=("integrity",),
        ),
        "cash_policy": CashPolicy(
            parking_isin=PARKING_ISIN,
            parking_symbol="LIQUIDCASE",
            deploy_within_sessions=5,
            min_deployment_inr=Decimal("5000"),
        ),
        "monitoring": MonitoringCadence(
            t2_cadence=T2Cadence.MONTHLY, t1_sensitivity=TriggerSensitivity.STANDARD
        ),
    }
    policies.update(overrides)
    return policies


def proposal(case_id: str = CASE_ID, version: int = 1, **overrides: Any) -> PolicySet:
    """A version-1 §5.2 proposal for `case_id`."""
    return PolicySet(case_id=case_id, version=version, **example_policies(**overrides))


def ratification(
    policy: PolicySet, *, by: str = "vysh", kind: RatificationKind = RatificationKind.HUMAN
) -> Ratification:
    return Ratification(by=by, at=NOW, kind=kind, content_hash=policy.content_hash)


# ── the fake database ────────────────────────────────────────────────────────────────────────


class FakeCursor:
    """The two cursor methods the service and the journal use."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


#: `decision_journal` write columns, as `analyst/journal/writer.py` orders them. Duplicated here
#: rather than imported from the private constant so that a reordering there fails this file
#: loudly instead of silently relabelling the payload it asserts on.
_JOURNAL_COLUMNS = (
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


class FakeDatabase:
    """An in-memory `case_` + `policy_set` + `decision_journal`, faithful where it matters.

    Faithful in three ways: `INSERT ... RETURNING` hands back the row the server built, so echoing
    the parameters is what a real insert yields; `Json` wrappers are unwrapped exactly as Postgres
    unwraps them out of a `jsonb` column; and the `policy_set` table is append-only here too — an
    UPDATE or DELETE against it raises, as the trigger does.

    Cross-case exposure is scripted rather than computed: its statement is an aggregate over
    `order_`, and reimplementing SQL aggregation in a fake would only prove the fake works.
    """

    def __init__(self) -> None:
        self.cases: dict[str, tuple[Any, ...]] = {}
        self.policies: list[tuple[Any, ...]] = []
        self.journal: list[dict[str, Any]] = []
        self.exposure_rows: list[tuple[Any, ...]] = []
        self.statements: list[str] = []
        self._next_journal_id = 1

    # the psycopg surface the service uses
    def execute(self, sql: str, params: Any = None) -> FakeCursor:
        text = " ".join(sql.split())
        values = [value.obj if isinstance(value, Json) else value for value in (params or ())]
        self.statements.append(text)

        if "policy_set" in text and text.startswith(("UPDATE", "DELETE")):
            raise AssertionError(
                "policy_set is append-only (0001_init.sql reject_mutation trigger); "
                f"refusing: {text}"
            )
        if text.startswith("INSERT INTO decision_journal"):
            return self._insert_journal(values)
        if text.startswith("INSERT INTO case_"):
            return self._insert_case(values)
        if text.startswith("UPDATE case_"):
            return self._update_case(values)
        if text.startswith("DELETE FROM case_"):
            self.cases.pop(values[0], None)
            return FakeCursor([])
        if text.startswith("INSERT INTO policy_set"):
            return self._insert_policy(values)
        if "FROM policy_set" in text:
            return self._select_policies(text, values)
        if "FROM case_" in text:
            return self._select_cases(text, values)
        if "FROM order_" in text:
            return FakeCursor(list(self.exposure_rows))
        raise AssertionError(f"FakeDatabase does not know this statement: {text}")

    # ── writes ───────────────────────────────────────────────────────────────────────────────

    def _insert_journal(self, values: list[Any]) -> FakeCursor:
        entry_id = self._next_journal_id
        self._next_journal_id += 1
        self.journal.append({"id": entry_id, **dict(zip(_JOURNAL_COLUMNS, values, strict=True))})
        return FakeCursor([(entry_id, *values)])

    def _insert_case(self, values: list[Any]) -> FakeCursor:
        case_id = values[0]
        if case_id in self.cases:
            raise AssertionError(f"duplicate key value violates case__pkey: {case_id}")
        row = tuple(values)
        self.cases[case_id] = row
        return FakeCursor([row])

    def _update_case(self, values: list[Any]) -> FakeCursor:
        case_id = values[-1]
        if case_id not in self.cases:
            return FakeCursor([])
        row = (case_id, *values[:-1])
        self.cases[case_id] = row
        return FakeCursor([row])

    def _insert_policy(self, values: list[Any]) -> FakeCursor:
        key = (values[0], values[1])
        if any((row[0], row[1]) == key for row in self.policies):
            raise AssertionError(f"duplicate key violates policy_set_version_unique: {key}")
        row = tuple(values)
        self.policies.append(row)
        return FakeCursor([row])

    # ── reads ────────────────────────────────────────────────────────────────────────────────

    def _select_cases(self, text: str, values: list[Any]) -> FakeCursor:
        if "WHERE case_id = %s" in text:
            row = self.cases.get(values[0])
            return FakeCursor([] if row is None else [row])
        rows = [self.cases[key] for key in sorted(self.cases)]
        if "WHERE state = ANY(%s)" in text:
            rows = [row for row in rows if row[2] in values[0]]
        return FakeCursor(rows)

    def _select_policies(self, text: str, values: list[Any]) -> FakeCursor:
        rows = sorted(self.policies, key=lambda row: (row[0], row[1]))
        if "DISTINCT ON (case_id)" in text:
            latest: dict[str, tuple[Any, ...]] = {}
            for row in rows:
                latest[row[0]] = row
            return FakeCursor([latest[key] for key in sorted(latest)])
        rows = [row for row in rows if row[0] == values[0]]
        if "AND version = %s" in text:
            return FakeCursor([row for row in rows if row[1] == values[1]])
        if "ORDER BY version DESC LIMIT 1" in text:
            return FakeCursor(rows[-1:])
        return FakeCursor(rows)

    # ── assertions helpers ───────────────────────────────────────────────────────────────────

    def events(self, event: str | None = None) -> list[dict[str, Any]]:
        """Journal entries written, optionally filtered to one `payload["event"]`."""
        return [
            entry
            for entry in self.journal
            if event is None or entry["payload"].get("event") == event
        ]

    def transitions(self) -> list[tuple[str, str]]:
        """Every journaled `(from, to)` state pair, in the order it was written."""
        return [
            (entry["payload"]["from"], entry["payload"]["to"])
            for entry in self.events("CASE_TRANSITION")
        ]


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def db() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def service(db: FakeDatabase, clock: FrozenClock, tmp_path: Path) -> CaseService:
    connection = cast(Connection, db)
    journal = Journal(connection, clock=clock, evidence=EvidenceStore(tmp_path / "evidence"))
    return CaseService(connection, journal=journal, clock=clock)


def ratified_case(
    service: CaseService, *, kind: RatificationKind = RatificationKind.HUMAN
) -> PolicySet:
    """Walk a case from creation to `RATIFIED` and return the policy version in force."""
    service.create(CASE_ID, "AI & Robotics")
    service.begin_interview(CASE_ID)
    service.propose(CASE_ID, proposal())
    return service.ratify(CASE_ID, by="vysh" if kind is RatificationKind.HUMAN else "B9", kind=kind)


# ── acceptance 1: the §5.1 lifecycle ─────────────────────────────────────────────────────────


def test_the_state_machine_is_exactly_the_diagram_in_5_1() -> None:
    """Every arrow §5.1 draws, and no arrow it does not."""
    diagram = {
        CaseState.DRAFT: frozenset({CaseState.INTERVIEW}),
        CaseState.INTERVIEW: frozenset({CaseState.PROPOSAL}),
        CaseState.PROPOSAL: frozenset({CaseState.RATIFIED}),
        CaseState.RATIFIED: frozenset({CaseState.FUNDED}),
        CaseState.FUNDED: frozenset({CaseState.ACTIVE}),
        CaseState.ACTIVE: frozenset({CaseState.SUSPENDED, CaseState.CLOSED}),
        CaseState.SUSPENDED: frozenset({CaseState.ACTIVE, CaseState.CLOSED}),
        CaseState.CLOSED: frozenset(),
    }
    assert diagram == LEGAL_TRANSITIONS


@pytest.mark.parametrize("from_state", list(CaseState))
@pytest.mark.parametrize("to_state", list(CaseState))
def test_every_state_pair_is_either_drawn_or_refused(
    from_state: CaseState, to_state: CaseState
) -> None:
    """No pair is left to chance: 64 ordered pairs, each permitted or raising."""
    if to_state in LEGAL_TRANSITIONS[from_state]:
        check_transition(CASE_ID, from_state, to_state)
        return
    with pytest.raises(IllegalTransitionError) as raised:
        check_transition(CASE_ID, from_state, to_state)
    assert from_state.value in str(raised.value)
    assert to_state.value in str(raised.value)


def test_closed_is_terminal_and_says_so() -> None:
    """A closed case is history; reopening it would resurrect a policy nobody re-read."""
    with pytest.raises(IllegalTransitionError, match="terminal and nothing leaves it"):
        check_transition(CASE_ID, CaseState.CLOSED, CaseState.ACTIVE)


def test_the_state_sets_downstream_reads_partition_the_lifecycle() -> None:
    """A6, A7 and the daily loop filter cases by these; a state in none of them is invisible."""
    assert frozenset({CaseState.CLOSED}) == CLOSED_STATES
    assert frozenset({CaseState.ACTIVE}) == TRADING_STATES, "a SUSPENDED case places no orders"
    covered = PRE_FUNDING_STATES | TRADING_STATES | CLOSED_STATES
    assert set(CaseState) - covered == {CaseState.FUNDED, CaseState.SUSPENDED}
    assert not PRE_FUNDING_STATES & TRADING_STATES


def test_the_happy_path_journals_one_entry_per_transition(
    service: CaseService, db: FakeDatabase
) -> None:
    """Acceptance 1's second half: DRAFT to CLOSED, with the journal recording every move."""
    ratified_case(service)
    service.fund(CASE_ID, FundingMode.PAPER, by="vysh")
    service.activate(CASE_ID)
    service.suspend(CASE_ID, reason="drawdown review triggered")
    service.resume(CASE_ID, reason="review complete, thesis intact")
    service.close(CASE_ID, reason="horizon reached")

    assert db.transitions() == [
        ("DRAFT", "INTERVIEW"),
        ("INTERVIEW", "PROPOSAL"),
        ("PROPOSAL", "RATIFIED"),
        ("RATIFIED", "FUNDED"),
        ("FUNDED", "ACTIVE"),
        ("ACTIVE", "SUSPENDED"),
        ("SUSPENDED", "ACTIVE"),
        ("ACTIVE", "CLOSED"),
    ]
    assert service.require(CASE_ID).state is CaseState.CLOSED


def test_every_case_entry_is_a_governance_decision_with_its_event_named(
    service: CaseService, db: FakeDatabase
) -> None:
    """§5.7's vocabulary is closed, so the payload carries which governance event this was."""
    ratified_case(service)

    assert db.journal, "a case that was created, interviewed and ratified journaled nothing"
    for entry in db.journal:
        assert entry["decision"] == Decision.POLICY_PROPOSAL.value
        assert entry["actor"] == Actor.USER.value
        assert entry["case_id"] == CASE_ID
        assert entry["payload"]["event"]
        assert entry["rationale"]
        # Invariant #7's shape: a governance entry is about the session it was made in.
        assert entry["trading_date"] == TODAY


def test_journal_payloads_are_strings_only(service: CaseService, db: FakeDatabase) -> None:
    """A number read back out of `jsonb` is a float, and a float in this system is a bug."""
    ratified_case(service)
    for entry in db.journal:
        for key, value in entry["payload"].items():
            assert isinstance(value, str), f"payload[{key!r}] is {type(value).__name__}"


def test_an_illegal_transition_changes_nothing(service: CaseService, db: FakeDatabase) -> None:
    """Checked before written: a refused move leaves neither a state change nor a journal line."""
    service.create(CASE_ID, "AI & Robotics")
    entries_before = len(db.journal)

    with pytest.raises(IllegalTransitionError):
        service.activate(CASE_ID)

    assert service.require(CASE_ID).state is CaseState.DRAFT
    assert len(db.journal) == entries_before


def test_a_case_cannot_be_funded_without_a_ratified_policy(service: CaseService) -> None:
    """Rails, dial and exit menu all come from the policy set; funding without one is unrailed."""
    service.create(CASE_ID, "AI & Robotics")
    service.begin_interview(CASE_ID)
    service.propose(CASE_ID, proposal())
    with pytest.raises(NoPolicyError, match="no ratified policy set"):
        service.fund(CASE_ID, FundingMode.PAPER, by="vysh")


def test_suspension_and_closure_demand_a_reason(service: CaseService) -> None:
    """The journal's only account of a pause is the reason given for it."""
    ratified_case(service)
    service.fund(CASE_ID, FundingMode.PAPER, by="vysh")
    service.activate(CASE_ID)
    with pytest.raises(ValueError, match="reason"):
        service.suspend(CASE_ID, reason="   ")


# ── acceptance 1 (CRUD side) ─────────────────────────────────────────────────────────────────


def test_a_case_is_created_in_draft_on_paper(service: CaseService) -> None:
    """Decision #8: every case starts on paper, whatever it is eventually funded as."""
    record = service.create(CASE_ID, "AI & Robotics", theme="AI/Robotics value chain")
    assert (record.state, record.funding_mode) == (CaseState.DRAFT, FundingMode.PAPER)
    assert record.created_at == NOW


def test_case_ids_are_never_reused(service: CaseService) -> None:
    service.create(CASE_ID, "AI & Robotics")
    with pytest.raises(DuplicateCaseError):
        service.create(CASE_ID, "A different case with the same id")


def test_an_unknown_case_raises_rather_than_returning_none(service: CaseService) -> None:
    assert service.get("NOPE") is None
    with pytest.raises(CaseNotFoundError):
        service.require("NOPE")


def test_describe_edits_text_and_nothing_governed(service: CaseService) -> None:
    """The one in-place update: a title is not a policy and decides nothing."""
    service.create(CASE_ID, "AI & Robotics")
    updated = service.describe(CASE_ID, title="AI & Robotics (India)", theme="EMS capex")
    assert (updated.title, updated.theme) == ("AI & Robotics (India)", "EMS capex")
    assert updated.state is CaseState.DRAFT


def test_only_a_pristine_draft_can_be_deleted(service: CaseService) -> None:
    """Past DRAFT a case has journal entries; §5.1 ends at CLOSED, not at delete."""
    service.create(CASE_ID, "AI & Robotics")
    service.begin_interview(CASE_ID)
    with pytest.raises(LifecycleError, match="never deleted"):
        service.delete(CASE_ID)

    service.create("SCRATCH", "created by mistake")
    service.delete("SCRATCH")
    assert service.get("SCRATCH") is None


# ── acceptance 2: a ratified policy set is immutable ─────────────────────────────────────────


def test_the_policy_set_carries_all_seven_5_2_policies() -> None:
    """One document, one ratification (§5.1) — a missing policy is an ungoverned decision."""
    assert set(POLICY_FIELDS) == {
        "capital_plan",
        "horizon",
        "rotation_dial",
        "rails",
        "exit_menu",
        "cash_policy",
        "monitoring",
    }
    assert set(proposal().policies) == set(POLICY_FIELDS)


def test_a_ratified_policy_set_refuses_assignment() -> None:
    """Immutability is a `ValidationError`, not a convention."""
    policy = proposal()
    ratified = policy.ratified_with(ratification(policy))
    with pytest.raises(ValidationError):
        ratified.rotation_dial = RotationDial(tactical_pct=Decimal("60"))
    with pytest.raises(ValidationError):
        ratified.rails.max_position_pct = Decimal("90")


def test_an_edit_creates_the_next_version_in_proposal() -> None:
    """Acceptance 2: `revise()` versions the change and leaves the ratified document alone."""
    v1 = proposal().ratified_with(ratification(proposal()))
    original_hash = v1.content_hash

    v2 = v1.revise(rotation_dial=RotationDial(tactical_pct=Decimal("40")))

    assert (v2.version, v2.status, v2.supersedes_version) == (2, PolicyStatus.PROPOSAL, 1)
    assert v2.ratification is None
    assert v2.rotation_dial.tactical_pct == Decimal("40")
    assert v1.status is PolicyStatus.RATIFIED
    assert v1.rotation_dial.tactical_pct == Decimal("30")
    assert v1.content_hash == original_hash


def test_a_revision_that_changes_nothing_is_refused() -> None:
    """A second ratification for an identical document is work with no governance content."""
    v1 = proposal().ratified_with(ratification(proposal()))
    with pytest.raises(PolicyVersionError, match="nothing to re-ratify"):
        v1.revise(rotation_dial=RotationDial(tactical_pct=Decimal("30")))
    with pytest.raises(PolicyVersionError, match="no changes"):
        v1.revise()


def test_revise_edits_policies_not_bookkeeping() -> None:
    """Version numbers and status are this method's own; letting a caller set them is a forgery."""
    v1 = proposal().ratified_with(ratification(proposal()))
    with pytest.raises(PolicyVersionError, match="version"):
        v1.revise(version=7)


def test_a_ratified_version_cannot_be_ratified_again() -> None:
    policy = proposal()
    ratified = policy.ratified_with(ratification(policy))
    with pytest.raises(PolicyVersionError, match="already RATIFIED"):
        ratified.ratified_with(ratification(policy))


def test_a_ratification_cannot_be_moved_onto_different_content() -> None:
    """The governance artifact pins content — M5.8's "changed since it was displayed" rule."""
    displayed = proposal()
    edited = PolicySet(
        case_id=CASE_ID,
        version=1,
        **example_policies(rotation_dial=RotationDial(tactical_pct=Decimal("90"))),
    )
    with pytest.raises(RatificationMismatchError):
        edited.ratified_with(ratification(displayed))


def test_the_content_hash_covers_the_policies_and_only_the_policies() -> None:
    """Renumbering a proposal is not a policy change; changing a rail is."""
    v1 = proposal()
    same_policies_later_version = PolicySet(
        case_id=CASE_ID, version=2, supersedes_version=1, **example_policies()
    )
    assert same_policies_later_version.content_hash == v1.content_hash

    different = PolicySet(
        case_id=CASE_ID,
        version=1,
        **example_policies(
            rails=RiskRails(
                max_position_pct=Decimal("20"),
                max_sector_pct=Decimal("35"),
                min_holdings=8,
                drawdown_review_pct=Decimal("25"),
                max_order_value_inr=Decimal("50000"),
                max_order_pct_of_case=Decimal("10"),
            )
        ),
    )
    assert different.content_hash != v1.content_hash


def test_a_proposal_may_not_carry_a_ratification_and_vice_versa() -> None:
    """Proposing is never ratifying (§5.1), in both directions."""
    with pytest.raises(ValidationError, match="must not carry a ratification"):
        PolicySet(
            case_id=CASE_ID,
            version=1,
            ratification=ratification(proposal()),
            **example_policies(),
        )
    with pytest.raises(ValidationError, match="must carry the ratification"):
        PolicySet(case_id=CASE_ID, version=1, status=PolicyStatus.RATIFIED, **example_policies())


def test_a_later_version_must_name_what_it_supersedes() -> None:
    """Otherwise "which rails were in force when" stops being reconstructable."""
    with pytest.raises(ValidationError, match="must name the version it supersedes"):
        PolicySet(case_id=CASE_ID, version=2, **example_policies())


def test_the_service_versions_an_edit_and_leaves_the_ratified_row_alone(
    service: CaseService, db: FakeDatabase
) -> None:
    """Acceptance 2 end to end: v1 stays in force until v2 is ratified in its own right."""
    v1 = ratified_case(service)
    service.fund(CASE_ID, FundingMode.PAPER, by="vysh")
    service.activate(CASE_ID)

    v2 = service.revise_policy(CASE_ID, rotation_dial=RotationDial(tactical_pct=Decimal("40")))

    assert (v2.version, v2.status) == (2, PolicyStatus.PROPOSAL)
    assert service.current_policy(CASE_ID) == v1, "an unratified revision must not take effect"
    assert service.pending_proposal(CASE_ID) is not None
    assert len(db.policies) == 1, "a proposal is not a policy_set row"
    assert service.require(CASE_ID).state is CaseState.ACTIVE, "revising is not a lifecycle move"

    stored_v2 = service.ratify(CASE_ID, by="vysh")

    assert stored_v2.version == 2
    assert service.current_policy(CASE_ID) == stored_v2
    assert service.pending_proposal(CASE_ID) is None
    history = service.policy_history(CASE_ID)
    assert [(policy.version, policy.status) for policy in history] == [
        (1, PolicyStatus.SUPERSEDED),
        (2, PolicyStatus.RATIFIED),
    ]
    assert history[0].content_hash == v1.content_hash, "v1's record changed when v2 landed"
    assert history[0].rotation_dial.tactical_pct == Decimal("30")


def test_a_stored_policy_version_round_trips_through_the_row(service: CaseService) -> None:
    """The seven policies survive `jsonb`: exact Decimals, not floats."""
    stored = ratified_case(service)
    read_back = service.policy_version(CASE_ID, 1)
    assert read_back == stored
    assert read_back.capital_plan.sip_amount_inr == Decimal("10000")
    assert isinstance(read_back.rails.max_position_pct, Decimal)
    assert read_back.ratification is not None
    assert read_back.ratification.content_hash == stored.content_hash


def test_require_policy_is_the_guard_a_decision_path_calls(service: CaseService) -> None:
    """Downstream (A4, A6, A7) must not have to remember to check for None."""
    service.create(CASE_ID, "AI & Robotics")
    with pytest.raises(NoPolicyError, match="no decision may be made"):
        service.require_policy(CASE_ID)
    with pytest.raises(UnknownPolicyVersionError):
        service.policy_version(CASE_ID, 1)
    assert service.policy_history(CASE_ID) == ()


def test_a_version_number_is_never_reused(service: CaseService, db: FakeDatabase) -> None:
    """`policy_set` is append-only; the service refuses before the constraint has to.

    The stale proposal is planted directly on the row because the service offers no way to build
    one — which is the point. What is being tested is that a v1 proposal arriving from anywhere
    (a stale browser tab in M5.8, a replayed request) cannot overwrite the ratified v1.
    """
    ratified_case(service)
    stale = proposal(version=1)
    row = db.cases[CASE_ID]
    db.cases[CASE_ID] = (
        *row[:10],
        {PENDING_POLICY_KEY: stale.model_dump(mode="json")},
        *row[11:],
    )
    with pytest.raises(PolicyVersionError, match="never reused"):
        service.ratify(CASE_ID, by="vysh")


def test_no_statement_in_the_package_mutates_a_policy_version() -> None:
    """A behavioural test only proves the paths it called; this proves the package has no such path.

    `policy_set` rows are the answer to "which rails were in force when that order was placed"
    (decisions #4/#5/#9). The database refuses UPDATE and DELETE on them with a trigger; this is
    the client-side half of the same rule.
    """
    forbidden = re.compile(r"\b(UPDATE\s+policy_set|DELETE\s+FROM\s+policy_set|TRUNCATE)\b", re.I)
    for path in sorted(CASES_PACKAGE.glob("*.py")):
        offending = forbidden.findall(path.read_text(encoding="utf-8"))
        assert not offending, f"{path.name} would mutate a ratified policy version: {offending}"


def test_the_schema_and_the_models_agree_about_the_enums() -> None:
    """A value the model accepts and the database rejects surfaces mid-transition, not here."""
    sql = INIT_MIGRATION.read_text(encoding="utf-8")
    for table, column, enum in (
        ("case_", "state", CaseState),
        ("case_", "funding_mode", FundingMode),
        ("policy_set", "ratification_kind", RatificationKind),
    ):
        block = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", sql, re.S)
        assert block, f"no CREATE TABLE {table} in {INIT_MIGRATION.name}"
        check = re.search(rf"{column}\s+text\s+NOT NULL.*?IN \(([^)]*)\)", block.group(1), re.S)
        assert check, f"no CHECK constraint found for {table}.{column}"
        declared = set(re.findall(r"'([A-Z_]+)'", check.group(1)))
        assert declared == {member.value for member in enum}


# ── acceptance 3: FUNDED(real) refuses a fixture ratification ────────────────────────────────


def test_a_fixture_ratification_cannot_fund_real_money(
    service: CaseService, db: FakeDatabase
) -> None:
    """Acceptance 3, and B9's rule: the reference case is paper and tests only, forever."""
    ratified_case(service, kind=RatificationKind.FIXTURE)

    with pytest.raises(FundingRefusedError, match="HUMAN ratification"):
        service.fund(CASE_ID, FundingMode.REAL, by="vysh")

    record = service.require(CASE_ID)
    assert record.state is CaseState.RATIFIED, "the refused funding must not have moved the case"
    assert record.funding_mode is FundingMode.PAPER
    assert db.transitions()[-1] == ("PROPOSAL", "RATIFIED")


def test_a_fixture_ratification_still_funds_paper(service: CaseService) -> None:
    """B9 exists precisely so M5 can be built and self-tested without a live ratification."""
    ratified_case(service, kind=RatificationKind.FIXTURE)
    record = service.fund(CASE_ID, FundingMode.PAPER, by="agent")
    assert (record.state, record.funding_mode) == (CaseState.FUNDED, FundingMode.PAPER)


def test_a_human_ratification_funds_real_money(service: CaseService, db: FakeDatabase) -> None:
    """The path exists — it is the ratification kind that gates it, not the mode."""
    ratified_case(service, kind=RatificationKind.HUMAN)
    record = service.fund(CASE_ID, FundingMode.REAL, by="vysh")
    assert (record.state, record.funding_mode) == (CaseState.FUNDED, FundingMode.REAL)
    funded = db.events("CASE_TRANSITION")[-1]
    assert funded["payload"]["funding_mode"] == "REAL"
    assert funded["payload"]["ratification_kind"] == "HUMAN"


@pytest.mark.parametrize(
    ("mode", "kind", "allowed"),
    [
        (FundingMode.PAPER, RatificationKind.FIXTURE, True),
        (FundingMode.PAPER, RatificationKind.HUMAN, True),
        (FundingMode.REAL, RatificationKind.HUMAN, True),
        (FundingMode.REAL, RatificationKind.FIXTURE, False),
    ],
)
def test_the_funding_guard_is_a_table_of_four(
    mode: FundingMode, kind: RatificationKind, allowed: bool
) -> None:
    """All four combinations stated, so a future edit cannot quietly flip the one that matters."""
    if allowed:
        check_funding(CASE_ID, mode, kind)
        return
    with pytest.raises(FundingRefusedError):
        check_funding(CASE_ID, mode, kind)


def test_only_a_human_ratification_funds_real_money() -> None:
    """One place answers "is this good enough for real money", and it is this property."""
    assert RatificationKind.HUMAN.funds_real_money
    assert not RatificationKind.FIXTURE.funds_real_money


def test_there_is_no_bypass_parameter_in_the_package() -> None:
    """AGENTIC_CONTEXT §3.6 reserves graduation to the human; an override flag would return it."""
    pattern = re.compile(r"\b(force|override|bypass|skip_checks|allow_fixture)\s*[:=]")
    for path in sorted(CASES_PACKAGE.glob("*.py")):
        found = pattern.findall(path.read_text(encoding="utf-8"))
        assert not found, f"{path.name} exposes a bypass: {found}"


# ── the SIP scheduler ────────────────────────────────────────────────────────────────────────


def test_instalment_dates_walk_months_without_skipping_february() -> None:
    """`day_of_month` is capped at 28 so no month silently loses its instalment."""
    plan = CapitalPlan(sip_amount_inr=Decimal("10000"), day_of_month=28)
    assert list(plan.instalments(date(2027, 1, 1), date(2027, 4, 30))) == [
        date(2027, 1, 28),
        date(2027, 2, 28),
        date(2027, 3, 28),
        date(2027, 4, 28),
    ]


def test_the_next_instalment_rolls_into_the_next_year() -> None:
    plan = CapitalPlan(sip_amount_inr=Decimal("10000"), day_of_month=1)
    assert plan.next_instalment(date(2026, 12, 2)) == date(2027, 1, 1)
    assert plan.next_instalment(date(2026, 12, 1)) == date(2026, 12, 1)


def test_sips_are_due_only_for_funded_cases_with_a_ratified_plan(
    service: CaseService, db: FakeDatabase
) -> None:
    """Money does not arrive on a proposal, and a closed case owes nothing."""
    ratified_case(service)
    assert service.due_sips(date(2026, 9, 1)) == (), "a RATIFIED case is not yet funded"

    service.fund(CASE_ID, FundingMode.PAPER, by="vysh")
    service.activate(CASE_ID)

    due = service.due_sips(date(2026, 9, 1))
    assert [(item.case_id, item.amount_inr) for item in due] == [(CASE_ID, Decimal("10000"))]
    assert service.due_sips(date(2026, 9, 2)) == ()

    service.suspend(CASE_ID, reason="paused, but the instalment still lands")
    assert len(service.due_sips(date(2026, 9, 1))) == 1

    service.close(CASE_ID, reason="wound down")
    assert service.due_sips(date(2026, 9, 1)) == ()
    assert db.journal, "the walk above journaled its transitions"


# ── the multi-case view and cross-case concentration ─────────────────────────────────────────


def test_the_overview_shows_every_case_including_the_ungoverned_ones(
    service: CaseService,
) -> None:
    """A case stuck in INTERVIEW is exactly what the multi-case view exists to surface."""
    ratified_case(service)
    service.fund(CASE_ID, FundingMode.PAPER, by="vysh")
    service.create("HEALTHCARE", "Healthcare")
    service.begin_interview("HEALTHCARE")

    overview = {summary.case_id: summary for summary in service.overview(on=TODAY)}
    assert set(overview) == {CASE_ID, "HEALTHCARE"}

    governed = overview[CASE_ID]
    assert governed.is_governed
    assert governed.policy_version == 1
    assert governed.ratification_kind is RatificationKind.HUMAN
    assert governed.next_sip_date == date(2026, 9, 1)
    assert governed.sip_amount_inr == Decimal("10000")

    ungoverned = overview["HEALTHCARE"]
    assert not ungoverned.is_governed
    assert ungoverned.next_sip_date is None
    assert ungoverned.state is CaseState.INTERVIEW


def test_a_pending_proposal_is_visible_in_the_overview(service: CaseService) -> None:
    """ "Waiting on me" is the question the human asks this view."""
    ratified_case(service)
    service.revise_policy(CASE_ID, rotation_dial=RotationDial(tactical_pct=Decimal("45")))
    assert service.overview(on=TODAY)[0].has_pending_proposal


def test_cross_case_exposure_aggregates_one_isin_across_cases(
    service: CaseService, db: FakeDatabase
) -> None:
    """A8's input: two cases at 12% each are a 24% household position neither case can see."""
    db.exposure_rows = [
        ("INE009A01021", "AI_ROBOTICS", 40),
        ("INE009A01021", "SEMIS", 60),
        ("INE467B01029", "SEMIS", 25),
    ]
    exposure = {item.isin: item for item in service.cross_case_exposure()}

    assert exposure["INE009A01021"].quantity == 100
    assert exposure["INE009A01021"].case_count == 2
    assert exposure["INE009A01021"].by_case == {"AI_ROBOTICS": 40, "SEMIS": 60}
    assert exposure["INE467B01029"].case_count == 1


# ── the policy models' own rules ─────────────────────────────────────────────────────────────


def test_a_float_percentage_is_refused() -> None:
    """A rail that is off by a float epsilon is a rail that did not hold."""
    with pytest.raises(ValidationError, match="exact decimal"):
        RotationDial(tactical_pct=30.5)  # type: ignore[arg-type]


def test_a_float_rupee_amount_is_refused() -> None:
    with pytest.raises(ValidationError, match="exact decimal"):
        CapitalPlan(sip_amount_inr=10000.5, day_of_month=1)  # type: ignore[arg-type]


def test_rails_that_no_portfolio_can_satisfy_are_refused_at_proposal_time() -> None:
    """Eight holdings capped at 5% each cannot hold 100%: every order would be blocked."""
    with pytest.raises(ValidationError, match="cannot reach 100%"):
        RiskRails(
            max_position_pct=Decimal("5"),
            max_sector_pct=Decimal("35"),
            min_holdings=8,
            drawdown_review_pct=Decimal("25"),
            max_order_value_inr=Decimal("50000"),
            max_order_pct_of_case=Decimal("10"),
        )


def test_a_sector_cap_below_the_position_cap_is_refused() -> None:
    with pytest.raises(ValidationError, match="below max_position_pct"):
        RiskRails(
            max_position_pct=Decimal("40"),
            max_sector_pct=Decimal("35"),
            min_holdings=8,
            drawdown_review_pct=Decimal("25"),
            max_order_value_inr=Decimal("50000"),
            max_order_pct_of_case=Decimal("10"),
        )


def test_the_exit_default_must_be_on_the_ratified_menu() -> None:
    with pytest.raises(ValidationError, match="not on the ratified menu"):
        ExitMenu(allowed=(ExitStrategy.STAGED,), default=ExitStrategy.IMMEDIATE)


def test_an_unconditional_immediate_exit_is_refused() -> None:
    """§5.6 allows immediate exits *on integrity events*, not at the agent's discretion."""
    with pytest.raises(ValidationError, match="names no trigger"):
        ExitMenu(allowed=(ExitStrategy.STAGED, ExitStrategy.IMMEDIATE), default=ExitStrategy.STAGED)


def test_the_exit_menu_hashes_the_same_however_it_was_typed() -> None:
    """Order is not policy: two identically-permissive menus must not look like a change."""
    one = ExitMenu(
        allowed=(ExitStrategy.IMMEDIATE, ExitStrategy.STAGED, ExitStrategy.STAGED),
        default=ExitStrategy.STAGED,
        immediate_allowed_on=("integrity",),
    )
    other = ExitMenu(
        allowed=(ExitStrategy.STAGED, ExitStrategy.IMMEDIATE),
        default=ExitStrategy.STAGED,
        immediate_allowed_on=("integrity",),
    )
    assert one == other


def test_the_cash_policy_names_an_isin_not_a_ticker() -> None:
    """Invariant #2: the cash leg joins to prices the way everything else does."""
    with pytest.raises(ValidationError):
        CashPolicy(
            parking_isin="LIQUIDCASE",
            parking_symbol="LIQUIDCASE",
            deploy_within_sessions=5,
            min_deployment_inr=Decimal("5000"),
        )


def test_the_rotation_dial_derives_the_core_share() -> None:
    """Storing both halves would let them disagree."""
    assert RotationDial(tactical_pct=Decimal("30")).core_pct == Decimal("70")


def test_a_naive_ratification_timestamp_is_rejected() -> None:
    """B10: a ratification time that cannot be ordered is not a governance record."""
    with pytest.raises(ValidationError, match="tz-aware"):
        Ratification(
            by="vysh",
            at=datetime(2026, 8, 7, 19, 30),
            kind=RatificationKind.HUMAN,
            content_hash=proposal().content_hash,
        )
