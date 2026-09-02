"""X2: the replay engine + determinism harness (M4.8) — EXECUTION_PLAN §7, §8.3.3.

The replay engine drives *a policy* over a stretch of history, session by session, through the
same ``SimBroker`` fill model that paper and (eventually) real money run behind (invariant #5). It
is a harness, not a strategy: it advances the clock, sets the point-in-time scope, calls the
injected policy, carries out the orders the policy returns, and journals every session — but it
contains no decision of its own. What to buy and why is the policy's job; *when* and *on what
information* is the engine's. That split is what lets the identical policy code run live and be
replayed here (§7).

Three properties the engine exists to guarantee:

* **The clock is frozen and moved one session at a time (B10, invariant #11).** Every component
  that reads the time — the broker's ``place`` (its decision date), the policy's journal timestamps
  — sees the session under replay, never the wall clock. The engine holds a ``FrozenClock`` and
  re-freezes it at each session before anything runs, so a replay of the same range is byte-for-byte
  the same run whenever it is executed (§8.3.3).

* **Each session is point-in-time scoped, automatically (invariant #7, §8.3.6).** The engine builds
  a fresh ``PitContext(as_of=session)`` for every session and hands it to the policy as the *only*
  way it reads data. A policy that reaches for a figure not yet knowable on the session date does
  not get a quietly shortened answer — the guard raises (``dataplatform.query.pit``). The scope is
  the engine's to set, not the policy's to remember.

* **Same inputs → byte-identical journal and book (§8.3.3).** ``ReplayResult`` serialises the
  journal (the ordered decisions, including the no-op heartbeats of invariant #9) and the final book
  (cash, holdings, positions, ledger) to canonical bytes. Two runs over the same market, sessions
  and policy produce identical bytes; that equality is the determinism test and, downstream, the
  reference-case gate (M4.9). The serialised journal is deliberately the *decision* form
  (``JournalEntry``), not the database-recorded form: a ``RecordedEntry`` carries the row id and
  landing time the database assigns, which are different every run by design
  (``analyst.journal.models.RecordedEntry``) and are not part of what the policy decided.

Persisting the journal to the real append-only ``decision_journal`` (invariants #9 and #12) is a
side effect the engine performs when a ``Journal`` is injected; it is optional so the determinism
harness runs offline in CI without Postgres, because the journal the engine *produces* — the value
in ``ReplayResult`` — is the same whether or not a database is attached. Every evidence reference on
an entry is content-addressed (``EvidenceBundle.ref``), so it is stable without a store behind it.

What the engine never does: read a wall clock, decide whether to trade, or hold a second cost model
— the broker it drives already imports the one shared ``execution.costs`` (invariant #4).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise
from typing import Any, Protocol, runtime_checkable

import structlog

from analyst.journal.evidence import EvidenceBundle, canonical_bytes, digest_of
from analyst.journal.models import Actor, Decision, JournalEntry
from analyst.journal.writer import Journal
from dataplatform.clock import Clock, FrozenClock
from dataplatform.query.pit import PitContext
from execution.broker import Broker, Order, OrderRequest

_log = structlog.get_logger(__name__)

__all__ = [
    "BookSnapshot",
    "Policy",
    "ReplayBroker",
    "ReplayEngine",
    "ReplayError",
    "ReplayResult",
    "SessionContext",
    "SessionDecision",
]


# ── the broker the engine drives ─────────────────────────────────────────────────────────────────


@runtime_checkable
class ReplayBroker(Broker, Protocol):
    """A ``Broker`` the engine can also advance session by session (``SimBroker`` satisfies it).

    The read/place surface of ``Broker`` is what the decision layer sees (invariant #5). Driving the
    fills forward — settling the previous session and executing the orders staged for the current
    one — is a backtest capability the live broker does not expose, so it lives here, not on the
    injectable ``Broker`` the policy is given. The engine holds this fuller surface; the policy, via
    ``SessionContext.broker``, holds only the read ``Broker`` and cannot advance time itself.
    """

    def execute_session(self, session: date) -> tuple[Order, ...]:
        """Settle the previous session and fill every order staged for ``session``."""


# ── errors ─────────────────────────────────────────────────────────────────────────────────────


class ReplayError(Exception):
    """A replay could not proceed. The engine fails loud (CLAUDE.md), never on a silent skip."""


# ── what a policy is given, and what it returns ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SessionContext:
    """Everything the engine hands a policy for one session — and nothing more.

    The surface is deliberately narrow. Data is read only through ``pit``, whose ``as_of`` is this
    session's date, so a policy physically cannot reach a figure not yet knowable without tripping
    the guard (invariant #7). Account state is read through ``broker`` (holdings, positions, cash,
    margins) — the ``Broker`` protocol, never a concrete broker (invariant #5). ``clock`` is the
    frozen clock, already set to this session, so a journal timestamp the policy stamps is the
    session's, not the wall clock's (B10).

    What it never does: expose a raw data store or a mutation handle. A policy reads its situation
    and returns its intent; the engine carries the intent out.
    """

    session: date
    pit: PitContext
    broker: Broker
    clock: Clock

    def __post_init__(self) -> None:
        if self.pit.as_of != self.session:
            raise ReplayError(
                f"session context is inconsistent: pit.as_of {self.pit.as_of.isoformat()} is not "
                f"the session {self.session.isoformat()}; the point-in-time scope must be the "
                "session under replay (invariant #7)"
            )


@dataclass(frozen=True, slots=True)
class SessionDecision:
    """What a policy decided for one session: orders to place, entries to journal, and the evidence.

    ``orders`` are staged by the engine for the next session, as a live EOD decision would be
    (``SimBroker.place``). ``entries`` are the journal records of the decision — a ``BUY`` with its
    rationale, a ``HOLD``, an ``ESCALATE``; they may be empty, in which case the engine writes a
    single ``HEARTBEAT`` so that "checked, nothing to do" is still a journalled decision (invariant
    #9). ``evidence`` is the bundle the policy actually looked at this session: it is required,
    because a decision — including a heartbeat — is only evidence if the facts behind it are
    recorded, and it is what every entry that does not already name a snapshot is stamped with.

    What it assumes: ``evidence.trading_date`` is the session, and any ``entry.ts`` was taken from
    the injected clock (``ctx.clock``), so the whole decision is a pure function of the session's
    inputs and replays identically.
    """

    evidence: EvidenceBundle
    orders: tuple[OrderRequest, ...] = ()
    entries: tuple[JournalEntry, ...] = ()


@runtime_checkable
class Policy(Protocol):
    """The one thing the engine does not implement: the strategy.

    A policy looks at a session's point-in-time situation and its book, and returns what to do
    (``SessionDecision``). The engine takes a policy *object* — the naive momentum backtest (M4.10),
    the reference case (M4.9), and ultimately the live agent (M5) all satisfy this same surface,
    which is what makes "the same policy code that runs live is what the backtest replays" (§7) a
    structural fact, not an aspiration. The engine calls ``decide`` once per session and never
    inspects what a strategy would: a price, a signal, a weight. Those live only behind this
    protocol.
    """

    def decide(self, ctx: SessionContext) -> SessionDecision:
        """Decide this session's orders and journal entries from its point-in-time context."""


# ── the final book, as a deterministic snapshot ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """The broker's book at the end of a replay, frozen into a canonically serialisable value.

    Holds exactly what the ``Broker`` protocol reports — free cash, settled holdings, unsettled
    positions and the cash ledger — captured through that protocol so the snapshot is
    broker-agnostic. ``canonical_bytes`` renders it the one way ``analyst.journal.evidence`` renders
    everything: UTF-8 JSON, keys sorted, money as exact ``Decimal`` strings, dates as ISO. Two runs
    that reach the same book produce the same bytes, which is half of the §8.3.3 determinism claim
    (the journal is the other half).
    """

    cash: Decimal
    holdings: tuple[dict[str, str], ...]
    positions: tuple[dict[str, str], ...]
    ledger: tuple[dict[str, str], ...]

    @classmethod
    def of(cls, broker: Broker) -> BookSnapshot:
        """Snapshot ``broker``'s current book through the ``Broker`` protocol alone."""
        holdings = tuple(
            {
                "isin": holding.isin,
                "exchange": holding.exchange.value,
                "quantity": str(holding.quantity),
                "average_price": str(holding.average_price),
            }
            for holding in broker.holdings()
        )
        positions = tuple(
            {
                "isin": position.isin,
                "exchange": position.exchange.value,
                "quantity": str(position.quantity),
                "average_price": str(position.average_price),
                "session": position.session.isoformat(),
            }
            for position in broker.positions()
        )
        ledger = tuple(
            {
                "seq": str(entry.seq),
                "session": entry.session.isoformat(),
                "isin": entry.isin,
                "description": entry.description,
                "debit": str(entry.debit),
                "credit": str(entry.credit),
                "balance": str(entry.balance),
            }
            for entry in broker.ledger()
        )
        return cls(
            cash=broker.margins().available,
            holdings=holdings,
            positions=positions,
            ledger=ledger,
        )

    def to_document(self) -> dict[str, Any]:
        """A JSON-safe document — the input ``canonical_bytes`` serialises."""
        return {
            "cash": str(self.cash),
            "holdings": [dict(holding) for holding in self.holdings],
            "positions": [dict(position) for position in self.positions],
            "ledger": [dict(entry) for entry in self.ledger],
        }

    def canonical_bytes(self) -> bytes:
        """The one canonical byte string this book is identified by (sorted keys, tight)."""
        return canonical_bytes(self.to_document())


# ── the result of a replay ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """The output of a replay: the journal it produced and the book it ended on.

    ``journal`` is every entry the engine wrote, in the order it wrote them — one or more per
    session, never fewer (invariant #9). It is the decision form (``JournalEntry``), not the
    recorded form: the row id and landing time the database assigns differ every run
    by design (``RecordedEntry``), so serialising them would make a reproducible run look
    irreproducible. ``book`` is the final ``BookSnapshot``.

    ``journal_bytes``, ``book_bytes`` and ``digest`` are the determinism surface: equal bytes mean
    equal runs, byte-for-byte (§8.3.3). The determinism test asserts exactly this across two runs;
    the reference case (M4.9) asserts these bytes against hand-computed literals.
    """

    journal: tuple[JournalEntry, ...]
    book: BookSnapshot

    def journal_document(self) -> list[dict[str, Any]]:
        """The journal as a JSON-safe list — each entry dumped in the same mode evidence uses."""
        return [entry.model_dump(mode="json") for entry in self.journal]

    def journal_bytes(self) -> bytes:
        """The canonical byte string of the whole journal, in write order."""
        return canonical_bytes(self.journal_document())

    def book_bytes(self) -> bytes:
        """The canonical byte string of the final book."""
        return self.book.canonical_bytes()

    def digest(self) -> str:
        """A single sha256 over journal-then-book — the one number that identifies this run.

        Two runs share a digest iff they share both the journal and the book byte-for-byte, so a
        determinism check can compare one value, and a regression perturbing either half moves
        it.
        """
        return digest_of(self.journal_bytes() + b"\x00" + self.book_bytes())


# ── the engine ───────────────────────────────────────────────────────────────────────────────────


class ReplayEngine:
    """Drive a policy over a session range through a broker, point-in-time and deterministically.

    Construct it with an injected ``Policy``, a ``Broker`` (the paper ``SimBroker`` for a backtest),
    the ``FrozenClock`` that broker shares, and the ordered ``sessions``. You may also inject
    a ``Journal`` to persist every entry to the real append-only ``decision_journal`` (invariants #9
    and #12); omit it to run offline — the journal is still produced in the ``ReplayResult`` either
    way.

    The broker **must** have been built with the same ``clock`` instance passed here: the engine
    re-freezes that clock at each session, and a broker holding a different clock would stage orders
    against a different decision date, breaking both correctness and determinism. This is the one
    wiring the caller owns; everything else the engine drives.

    ``run`` walks the sessions in order. For each: it re-freezes the clock, settles and fills the
    orders staged by the previous session (``Broker.execute_session``), builds the session's
    ``PitContext``, asks the policy to decide, places the returned orders (staged for the next
    session), and journals the decision. It returns a ``ReplayResult`` whose bytes are identical
    across runs of the same inputs (§8.3.3).

    What it never does: implement a strategy, read a wall clock, or run sessions out of order.
    """

    __slots__ = ("_broker", "_clock", "_journal", "_policy", "_sessions")

    def __init__(
        self,
        *,
        policy: Policy,
        broker: ReplayBroker,
        clock: FrozenClock,
        sessions: Sequence[date],
        journal: Journal | None = None,
    ) -> None:
        ordered = tuple(sessions)
        if not ordered:
            raise ReplayError("a replay needs at least one session")
        for earlier, later in pairwise(ordered):
            if later <= earlier:
                raise ReplayError(
                    f"sessions must be strictly increasing, got {earlier.isoformat()} then "
                    f"{later.isoformat()}; a replay runs each session once, forward only"
                )
        self._policy = policy
        self._broker = broker
        self._clock = clock
        self._sessions = ordered
        self._journal = journal

    def run(self) -> ReplayResult:
        """Replay every session in order and return the journal and final book (§8.3.3)."""
        produced: list[JournalEntry] = []
        for session in self._sessions:
            produced.extend(self._run_session(session))
        result = ReplayResult(journal=tuple(produced), book=BookSnapshot.of(self._broker))
        _log.info(
            "replay.complete",
            sessions=len(self._sessions),
            first=self._sessions[0].isoformat(),
            last=self._sessions[-1].isoformat(),
            entries=len(result.journal),
            digest=result.digest(),
        )
        return result

    def _run_session(self, session: date) -> list[JournalEntry]:
        """Replay one session: freeze, fill staged orders, decide, place, journal."""
        # 1. Move the clock to this session before anything reads it (B10). Everything downstream —
        #    the broker's decision date, the policy's timestamps — now sees exactly `session`.
        self._clock.freeze_at(session)

        # 2. Fill what the *previous* session staged. On the first session nothing is staged, so
        #    this only advances the settlement cursor. Fills change the book but are not decisions
        #    here — the decision was the prior session's, the fill its consequence, in the ledger
        #    the final book carries.
        self._broker.execute_session(session)

        # 3. Scope the session point-in-time and ask the policy to decide (invariant #7). Context
        #    is the only way the policy reads data, so the scope is enforced, not requested.
        ctx = SessionContext(
            session=session,
            pit=PitContext(as_of=session),
            broker=self._broker,
            clock=self._clock,
        )
        decision = self._policy.decide(ctx)
        _validate_decision(decision, session)

        # 4. Carry out the intent: stage each order for the next session (SimBroker.place reads the
        #    now-frozen clock for its decision date). The engine chooses no orders; it places the
        #    ones the policy returned, in the order it returned them.
        for request in decision.orders:
            self._broker.place(request)

        # 5. Journal the decision. Every session writes at least one entry (invariant #9).
        return self._journal_decision(decision, session)

    def _journal_decision(self, decision: SessionDecision, session: date) -> list[JournalEntry]:
        """Write the session's entries (or a heartbeat), persisting them if a Journal is attached.

        The evidence reference is content-addressed, so it is the same with or without a store
        behind it — the offline determinism run and the Postgres-backed run stamp entries
        identically. When a ``Journal`` is present the bundle is stored once and each entry appended
        (invariant #9); when absent the entries are still produced, carrying the same reference.
        """
        evidence_ref = decision.evidence.ref().ref
        if self._journal is not None:
            self._journal.snapshot(decision.evidence)

        entries = list(decision.entries)
        if not entries:
            # "Checked, nothing to do" is a decision with evidence behind it, not a missing row.
            entries = [
                JournalEntry(
                    ts=self._clock.now(),
                    trading_date=session,
                    case_id=decision.evidence.case_id,
                    actor=Actor.T0,
                    decision=Decision.HEARTBEAT,
                    evidence_snapshot_ref=evidence_ref,
                )
            ]

        written: list[JournalEntry] = []
        for entry in entries:
            # Stamp the session's evidence onto any entry that did not name its own, so every entry
            # is tied to what the policy actually saw. An entry that names a different snapshot is
            # left as-is — it is asserting it was decided on other evidence, the policy's to claim.
            stamped = (
                entry
                if entry.evidence_snapshot_ref is not None
                else entry.model_copy(update={"evidence_snapshot_ref": evidence_ref})
            )
            if self._journal is not None:
                self._journal.append(stamped)
            written.append(stamped)
        return written


def _validate_decision(decision: SessionDecision, session: date) -> None:
    """Reject a decision that could not have been made on ``session``'s information.

    Two cheap structural checks the engine owes the journal: the evidence must be about the session
    it was gathered for, and no entry may claim a session other than this one. Both are shapes of
    invariant #7 at the harness boundary — a mismatch means the policy assembled the wrong session's
    world, and catching it here is far cheaper than discovering a leaked run months later.
    """
    if decision.evidence.trading_date != session:
        raise ReplayError(
            f"policy returned evidence for {decision.evidence.trading_date.isoformat()} while "
            f"deciding session {session.isoformat()}: the evidence must be about the session under "
            "replay (invariant #7)"
        )
    for entry in decision.entries:
        if entry.trading_date != session:
            raise ReplayError(
                f"policy returned a journal entry for {entry.trading_date.isoformat()} while "
                f"deciding session {session.isoformat()}: a replayed decision is about exactly the "
                "session it was made on (invariant #7)"
            )
