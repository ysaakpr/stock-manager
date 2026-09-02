"""M4.11 — the automated PIT-leak harness (EXECUTION_PLAN §8.3.6, invariant #7).

Invariant #7: *no datum with ``knowable_date > decision_date`` ever reaches a decision.* M4.3 made
that a per-call mechanism — ``PitContext.admit`` raises the instant it meets a record that is not
yet knowable. This module is the *replay-wide audit* the plan asks for on top of that guard
(§8.3.6): it **instruments the query layer during a full replay and audits every access against
the session's ``as_of``**, so a run comes with a certificate — "N point-in-time reads, 0 leaked" —
rather than a hope that every call site remembered to filter.

Why an audit *as well as* the guard — the two catch different failures:

* **The guard** (``PitContext.admit``) is the first line: an honest dataset whose rows reach past
  the session raises ``PitError`` at the query layer, before the record can reach a decision. The
  replay fails loud. ``test_the_guard_stops_an_honest_future_reaching_read`` shows this.

* **The audit** is defence-in-depth: it re-derives each admitted datum's *true* knowable date from
  the record itself and checks it against the session's ``as_of``, independently of what the
  dataset *declared*. That catches the one leak the per-record guard cannot — a dataset that
  **mis-declares** its ``knowable_date`` (a query built with the wrong knowability rule), which
  sails through the guard because the guard trusts the declaration.
  ``test_the_audit_catches_a_misdeclared_future_read`` is that case, and it is the deliberate leak
  the task requires the harness to catch.

Everything here is offline and deterministic (AGENTIC_CONTEXT B8): an in-memory point-in-time
store, an in-memory market, and a ``FrozenClock`` moved one session at a time by the engine (B10).
The file also asserts the audited run is byte-identical across two executions (§8.3.3) —
determinism and the leak audit are the two properties M4's gate stands on, and CI runs both
offline.

The auditor instruments the query layer by patching ``PitContext.admit`` for the duration of one
replay (``pytest``'s ``monkeypatch``, reverted automatically). It never weakens the guard: the real
``admit`` still runs first — the audit wraps it, it does not replace it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Final

import pytest

from analyst.journal import (
    Actor,
    Decision,
    EvidenceBundle,
    EvidenceItem,
    EvidenceKind,
    JournalEntry,
    Sleeve,
)
from backtest.replay import (
    Policy,
    ReplayEngine,
    ReplayResult,
    SessionContext,
    SessionDecision,
)
from dataplatform.clock import FrozenClock
from dataplatform.query.pit import Dataset, PitContext, PitError
from execution.broker import Exchange, OrderRequest, Side
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import NoReferenceBarError, ReferenceBar, SimBroker

# ── fixtures: two names, four sessions, a moving price path ─────────────────────────────────────

INFY: Final[str] = "INE009A01021"
TCS: Final[str] = "INE467B01029"

SESSIONS: Final[tuple[date, ...]] = (
    date(2024, 1, 2),
    date(2024, 1, 3),
    date(2024, 1, 4),
    date(2024, 1, 5),
)

#: One session past the replay window, so an order staged on the last replayed session has a next
#: session to fill on (it never fills inside the window; the book excludes it, deterministically).
CALENDAR: Final[tuple[date, ...]] = (*SESSIONS, date(2024, 1, 8))

OPENING_CASH: Final[Decimal] = Decimal("100000000")

#: A per-session close for each name — the point-in-time figure the policy reads, and the reference
#: the broker fills at. Deliberately moving, so the run is not a flat, trivially-determinate one.
CLOSES: Final[dict[tuple[str, date], Decimal]] = {
    (INFY, SESSIONS[0]): Decimal("1500"),
    (TCS, SESSIONS[0]): Decimal("3600"),
    (INFY, SESSIONS[1]): Decimal("1520"),
    (TCS, SESSIONS[1]): Decimal("3550"),
    (INFY, SESSIONS[2]): Decimal("1490"),
    (TCS, SESSIONS[2]): Decimal("3620"),
    (INFY, SESSIONS[3]): Decimal("1535"),
    (TCS, SESSIONS[3]): Decimal("3580"),
}


@dataclass(frozen=True, slots=True)
class PriceRecord:
    """One name's close on one session, carrying the date it *truly* became knowable (invariant #7).

    ``knowable_date`` is the session itself: an EOD close for session S is knowable on S. This field
    is the *ground truth* the audit reads — independent of whatever ``knowable_date`` extractor a
    ``Dataset`` chooses to *declare*. A dataset that lies about knowability (declares an earlier
    date than this field) is exactly the leak the audit exists to catch.
    """

    isin: str
    session: date
    close: Decimal
    knowable_date: date


def _true_knowable_date(record: object) -> date | None:
    """The audit's independent truth: a ``PriceRecord``'s real knowable date, read from the record.

    Deliberately *not* the dataset's declared extractor — the whole point of the audit is to check
    the datum that actually reached the decision against the invariant, not to re-trust the
    declaration the guard already trusted.
    """
    return record.knowable_date if isinstance(record, PriceRecord) else None


class InMemoryPitStore:
    """A point-in-time price store the policy queries per session — the offline stand-in for D4.

    ``cross_section`` is what a *correct* policy asks for: the rows knowable on or before ``as_of``,
    declared with their real ``knowable_date`` so the guard can prove there is no leak. The two
    ``*_future`` builders are deliberate bugs, one honest and one dishonest, that the harness must
    catch in different ways.
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
            f"closes@{as_of.isoformat()}",
            rows,
            knowable_date=lambda record: record.knowable_date,
        )

    def future_reaching_cross_section(self, as_of: date) -> Dataset[PriceRecord]:
        """Every close from ``as_of`` on, declared honestly — a bug the *guard* raises on.

        The rows genuinely carry future ``knowable_date`` values and the dataset declares them
        truthfully, so ``PitContext.admit`` meets a ``knowable_date > as_of`` and refuses.
        """
        rows = [record for record in self._records if record.session >= as_of]
        return Dataset.declaring(
            f"future@{as_of.isoformat()}",
            rows,
            knowable_date=lambda record: record.knowable_date,
        )

    def misdeclared_cross_section(self, as_of: date) -> Dataset[PriceRecord]:
        """Future rows, but declared as if knowable *today* — the leak the *audit* must catch.

        The rows are from ``as_of`` onward, yet the declared extractor claims every one became
        knowable on ``as_of``. The guard, trusting the declaration, admits them; only an audit that
        reads each record's *true* knowable date (``_true_knowable_date``) sees the leak. This is a
        query built with the wrong knowability rule — the realistic way future data slips past a
        per-record guard.
        """
        rows = [record for record in self._records if record.session >= as_of]
        return Dataset.declaring(
            f"misdeclared@{as_of.isoformat()}",
            rows,
            knowable_date=lambda _record: as_of,
        )


class InMemoryMarket:
    """A ``SessionMarket`` over the fixture calendar and closes — the offline stand-in for M4.1."""

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


# ── the instrumentation: audit every point-in-time access during a replay ────────────────────────


@dataclass(frozen=True, slots=True)
class Access:
    """One admitted point-in-time read, as the audit recorded it: what was asked, what came back.

    ``as_of`` is the session the read was scoped to; ``knowable_dates`` are the *true* knowable
    dates of the records that reached the policy through this call. A clean access has every one on
    or before ``as_of``.
    """

    dataset: str
    as_of: date
    knowable_dates: tuple[date, ...]


@dataclass(frozen=True, slots=True)
class Leak:
    """A datum that reached a decision though not yet knowable — a caught invariant-#7 breach."""

    dataset: str
    as_of: date
    knowable_date: date


class PitLeakError(AssertionError):
    """The audit found a datum with ``knowable_date > as_of`` that reached a decision.

    An ``AssertionError`` on purpose: a leak the audit catches is a test failure, the loud kind the
    harness exists to produce. It carries the offending ``Leak`` records for the report.
    """

    def __init__(self, leaks: tuple[Leak, ...]) -> None:
        self.leaks = leaks
        detail = "; ".join(
            f"{leak.dataset}: knowable {leak.knowable_date.isoformat()} > as_of "
            f"{leak.as_of.isoformat()}"
            for leak in leaks
        )
        super().__init__(f"point-in-time leak(s) reached a decision — {detail}")


@dataclass
class PitAuditor:
    """Instruments ``PitContext.admit`` for one replay, recording every access and auditing it.

    ``truth`` is the independent knowable-date reader (``_true_knowable_date`` for the price
    fixture): it is what makes the audit more than a re-run of the guard — it reads the datum that
    actually reached the decision, not the declaration the guard already trusted. ``install``
    patches the query layer for the duration of a test (reverted by ``monkeypatch``);
    ``assert_no_leak`` is the harness's verdict, and ``assert_audited`` guards against a vacuous
    pass (a run that read nothing proves nothing).
    """

    truth: Callable[[object], date | None]
    accesses: list[Access] = field(default_factory=list)
    leaks: list[Leak] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wrap ``PitContext.admit`` so every admitted read is audited against its ``as_of``.

        The real ``admit`` runs first and unchanged — the guard still raises on an honestly-declared
        leak. The audit runs on what ``admit`` returned, reading each record's *true* knowable date
        and recording any that is not yet knowable. So a dishonestly-declared dataset that the guard
        waves through is still caught here.
        """
        original = PitContext.admit

        def audited_admit(pit_self: PitContext, dataset: Dataset[Any]) -> tuple[Any, ...]:
            admitted = original(pit_self, dataset)
            knowable_dates: list[date] = []
            for record in admitted:
                knowable = self.truth(record)
                if knowable is None:
                    continue
                knowable_dates.append(knowable)
                if knowable > pit_self.as_of:
                    self.leaks.append(
                        Leak(dataset=dataset.name, as_of=pit_self.as_of, knowable_date=knowable)
                    )
            self.accesses.append(
                Access(
                    dataset=dataset.name,
                    as_of=pit_self.as_of,
                    knowable_dates=tuple(knowable_dates),
                )
            )
            return admitted

        monkeypatch.setattr(PitContext, "admit", audited_admit)

    def assert_audited(self) -> None:
        """Fail if the replay made no PIT reads — a clean audit over nothing proves nothing."""
        assert self.accesses, "the audit recorded no point-in-time accesses; the run was vacuous"

    def assert_no_leak(self) -> None:
        """The verdict: raise ``PitLeakError`` if any datum reached a decision unknowable."""
        if self.leaks:
            raise PitLeakError(tuple(self.leaks))


# ── policies: one correct, two buggy (one honest bug, one dishonest bug) ─────────────────────────


class HonestPolicy:
    """Reads the knowable cross-section each session and stages a fixed small buy — a clean run.

    Deterministic and PIT-correct: it reads only ``store.cross_section`` (scoped to ``as_of``) and
    keys on ISIN. It exists to give the audit real accesses to certify and a real book to be
    determinate about.
    """

    def __init__(self, store: InMemoryPitStore) -> None:
        self._store = store

    def decide(self, ctx: SessionContext) -> SessionDecision:
        rows = ctx.pit.admit(self._store.cross_section(ctx.session))
        prices = {record.isin: record.close for record in rows}
        orders = (OrderRequest(isin=INFY, side=Side.BUY, quantity=10, tag="AUDIT"),)
        entries = (
            JournalEntry(
                ts=ctx.clock.now(),
                trading_date=ctx.session,
                actor=Actor.T0,
                decision=Decision.BUY,
                isin=INFY,
                sleeve=Sleeve.TACTICAL,
                rationale=f"buy 10 INFY at {prices[INFY]}",
            ),
        )
        return SessionDecision(
            evidence=_price_evidence(ctx.session, rows), orders=orders, entries=entries
        )


class FutureReachingPolicy:
    """Reads an honestly-declared cross-section reaching past the session — the guard must raise."""

    def __init__(self, store: InMemoryPitStore) -> None:
        self._store = store

    def decide(self, ctx: SessionContext) -> SessionDecision:
        # Honestly-declared future rows: admit meets a knowable_date > as_of and raises.
        ctx.pit.admit(self._store.future_reaching_cross_section(ctx.session))
        raise AssertionError("unreachable: the PIT guard must have raised before this")


class MisdeclaringPolicy:
    """Reads a cross-section *mis-declaring* future rows as knowable today — the audit catches it.

    The declared knowable date is ``as_of`` for every row, so the guard admits rows that are really
    from later sessions. The read succeeds; the leak is invisible to the guard and visible only to
    the audit, which reads each record's true knowable date.
    """

    def __init__(self, store: InMemoryPitStore) -> None:
        self._store = store

    def decide(self, ctx: SessionContext) -> SessionDecision:
        rows = ctx.pit.admit(self._store.misdeclared_cross_section(ctx.session))
        return SessionDecision(evidence=_price_evidence(ctx.session, rows))


def _price_evidence(session: date, rows: tuple[PriceRecord, ...]) -> EvidenceBundle:
    """The price cross-section the policy saw, as an evidence bundle addressed by its content.

    Only the rows knowable on ``session`` become evidence — a datum a leak smuggled in for a
    *later* session is never presented as evidence *for this session*, which keeps the evidence
    bundle consistent even in the misdeclared run.
    """
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
            if record.session == session
        ),
    )


# ── the replay harness ───────────────────────────────────────────────────────────────────────────


def _run(policy: Policy) -> ReplayResult:
    """One full offline replay of ``policy`` over the fixture window, fresh clock and broker."""
    clock = FrozenClock(SESSIONS[0])
    broker = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card()),
        market=InMemoryMarket(CALENDAR, CLOSES),
        opening_cash=OPENING_CASH,
    )
    engine = ReplayEngine(policy=policy, broker=broker, clock=clock, sessions=SESSIONS)
    return engine.run()


def _audited_run(policy: Policy, monkeypatch: pytest.MonkeyPatch) -> PitAuditor:
    """Replay ``policy`` with the query layer instrumented, return the auditor for its verdict."""
    auditor = PitAuditor(truth=_true_knowable_date)
    auditor.install(monkeypatch)
    _run(policy)
    return auditor


# ── acceptance #1 (part a): the audit certifies a correct replay leaked nothing ──────────────────


def test_a_correct_replay_passes_the_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the harness in the good case: every access audited, zero leaks."""
    store = InMemoryPitStore(CLOSES)
    auditor = _audited_run(HonestPolicy(store), monkeypatch)

    auditor.assert_audited()  # the run actually read point-in-time data — not a vacuous pass
    auditor.assert_no_leak()  # and none of it reached a decision unknowable

    # The audit saw exactly one read per session, each scoped to that session's date.
    assert [access.as_of for access in auditor.accesses] == list(SESSIONS)
    # Every datum that reached a decision was knowable on or before the session it reached.
    for access in auditor.accesses:
        assert all(knowable <= access.as_of for knowable in access.knowable_dates)


# ── acceptance #1 (part b): the deliberate leaks — one caught by the guard, one by the audit ──────


def test_the_guard_stops_an_honest_future_reaching_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """First line of defence: an honestly-declared future read raises at the query layer, loud."""
    store = InMemoryPitStore(CLOSES)
    auditor = PitAuditor(truth=_true_knowable_date)
    auditor.install(monkeypatch)

    with pytest.raises(PitError):
        _run(FutureReachingPolicy(store))


def test_the_audit_catches_a_misdeclared_future_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deliberate leak: a mis-declared dataset the guard admits, and the audit catches.

    This is the harness earning its place. The dataset lies about knowability, so ``admit`` does not
    raise; the future datum reaches the decision. The audit — reading each record's *true* knowable
    date — flags it, and ``assert_no_leak`` fails loud.
    """
    store = InMemoryPitStore(CLOSES)
    auditor = _audited_run(MisdeclaringPolicy(store), monkeypatch)

    # The read succeeded (the guard was fooled), so the audit did record accesses...
    auditor.assert_audited()
    # ...and it caught the future data those accesses smuggled through.
    with pytest.raises(PitLeakError) as caught:
        auditor.assert_no_leak()

    leaks = caught.value.leaks
    assert leaks, "the audit must have recorded at least one leaked datum"
    # Every recorded leak is genuinely a future datum: its true knowable date is after the session.
    assert all(leak.knowable_date > leak.as_of for leak in leaks)
    # The very first session already leaked: it admitted rows from every later session.
    first_session_leaks = [leak for leak in leaks if leak.as_of == SESSIONS[0]]
    assert first_session_leaks


def test_the_audit_is_the_only_thing_that_catches_the_misdeclared_leak() -> None:
    """Proof the audit is not redundant: the guard alone admits the mis-declared rows.

    Without the audit the misdeclared read returns silently — no ``PitError`` — which is exactly why
    a replay-wide audit is needed on top of the per-record guard.
    """
    store = InMemoryPitStore(CLOSES)
    pit = PitContext(as_of=SESSIONS[0])

    # The guard admits the mis-declared future rows without complaint (it trusts the declaration)...
    admitted = pit.admit(store.misdeclared_cross_section(SESSIONS[0]))
    assert admitted, "the mis-declared dataset should be admitted by the guard"
    # ...yet some admitted rows are truly from later sessions — the leak the guard cannot see.
    assert any(record.session > SESSIONS[0] for record in admitted)


# ── acceptance #2: determinism of the audited run (byte-identical, offline) ───────────────────────


def test_the_audited_replay_is_byte_identical_across_runs() -> None:
    """§8.3.3 determinism beside the leak audit: same inputs → identical journal and book bytes."""
    store = InMemoryPitStore(CLOSES)
    first = _run(HonestPolicy(store))
    second = _run(HonestPolicy(store))

    assert first.journal_bytes() == second.journal_bytes()
    assert first.book_bytes() == second.book_bytes()
    assert first.digest() == second.digest()


def test_the_replay_actually_traded() -> None:
    """A determinism/audit claim over an empty run would be vacuous — assert there is a real run."""
    store = InMemoryPitStore(CLOSES)
    result = _run(HonestPolicy(store))

    buys = [entry for entry in result.journal if entry.decision is Decision.BUY]
    assert buys, "the honest policy should have staged buys"
    assert result.book.holdings, "settled holdings should remain at the end of the replay"
    assert result.book.ledger, "fills should have posted cash-ledger lines"
