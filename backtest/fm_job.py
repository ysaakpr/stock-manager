"""A10 · M17.7 — the daily M17 job: interlock, Commons, managers, controls, staging, marks.

Pre-registration §4 run forward, once per trading session, after the EOD pipeline is green. This
module is the job's **composition root**: it builds the M17 paper accounts
(`backtest.fm_paper.M17PaperAccount`, the M15.3 staging/recon/kill-switch path), so it lives under
``backtest/`` beside the M15.3 paper session — `analyst/` never names a concrete broker
(invariant #5). The decision-side pieces it composes are `analyst.fundmanager.job`.

**One evening, in order** (`run_m17_session`; the session is an explicit date, never "today"):

1. *Trading day / idempotency.* A holiday does nothing; a session already recorded for the
   desk is a no-op (a rerun never trades twice).
2. *Interlock* (invariant #10). The status gate is read first. Red — or a gate that cannot be
   read — journals ``SKIPPED_DATA_RED`` in every book's stream (managers, controls, the bench),
   records the skip, and stages nothing. The single M17 kill switch is read next; tripped, every
   book's due orders lapse and every book journals the halt, and no model is called.
3. *The previous cycle's close.* Yesterday's staged orders fill at this session's open (corporate
   actions booked first, an upper-circuit buy left unfilled), idle cash accrues, each book is
   reconciled (`FundDesk.execute`); every book and the bench is marked (``BOOK_MARK``, every book
   every session); each decision whose horizon (or a BUY's exit) has come resolves
   (``DECISION_OUTCOME``). This is pre-registration §4 step 7 for the decisions staged last night.
   *Suspended holdings* (M17.13, owner decision 2026-10-10): the interlock having passed, a held
   name that is still listed but has no bar on a session whose L1 coverage is at its usual level
   (`M17World.suspended`, production `backtest.fm_world.LakeSuspendedNames`) is SUSPENDED, not a
   data fault — marked at its last traded close, journaled ``SUSPENDED_HOLDING`` in its book's
   stream, shown to the manager as suspended since that date, and its sells held over until it
   prints (`analyst.fundmanager.books`). A decision resolving while it is suspended is scored at
   that close with ``suspended`` set. A market-wide gap is red data and never gets this far.
4. *Mechanical stops* (Amendment 1 c). Each manager book's `StopBook` judges the close; a close
   below a stop becomes a ``STOP_EXIT`` sell for the next open, ahead of anything the manager
   decides and with no model call (a stopped name's own decision is dropped).
5. *Wait for data.* Same-evening index and VIX levels, the bench's NIFTY 500 TRI level and the L2
   refresh for the session are probed (`M17World.readiness`); while one is missing the job waits,
   bounded (`WaitPolicy`), journaling ``M17_DATA_WAIT``; past the bound it **runs with the gaps
   recorded** (``M17_DATA_GAPS``) — the Commons sheets name every lagging input as a gap, and a
   missing bench level leaves the bench's mark owed until it lands.
6. *Commons build* (`CommonsBuilder`): sheets, shortlist, screens and regime, then the filing
   digests scoped to the screens' names plus the shortlist, and the frozen base-rate table.
7. *Managers*, one at a time, each in isolation (`analyst.fundmanager.runtime.run_manager`): its
   own book view (`analyst.fundmanager.job.manager_book`: thesis, invalidations, stop, weight,
   forced reviews first), its own journal stream, the model behind a `DeadlineLLM` (08:30 IST on
   the next session; rate limits back off inside it). A manager past the deadline journals
   ``MISSED_SESSION`` and stages nothing of its own; one that raises is journaled
   ``MANAGER_CRASHED``; either way the others still run and its stop exits still stage. Accepted
   decisions become book orders through the M17 rails (`FundBook.decide`); a staged BUY's stop
   goes into the `StopBook` and its memo beside it.
8. *Control books* (`ControlBook.run`), on the same shortlist, no model.
9. *Persist, score, digest.* The desk's whole state is recorded in ``paper_session`` (one row a
   session under the desk id, the M15.3 ledger, no new schema); the scoreboard is built from the
   desk's own ledger and — when a journal reader is given — rebuilt from the journal and checked
   byte-for-byte against it; the owner's digest is written.

**Streams.** ``--dry-run`` files every entry under ``m17-dry:<book>`` (`StreamJournal`) and the
desk state under its own desk id, so a rehearsal never touches the live books. ``--start S0``
opens the books at S0's open (all cash: S0 itself never fills) and journals ``M17_S0`` with every
book's `mandate_hash` (`analyst.fundmanager.job.mandate_fingerprints`). The live stream refuses
to run before it has started.

What it never does: read a wall clock for a decision (journal entries are stamped on the
session's evening by a frozen clock; only the deadline and the data wait read the injected
real clock), trade on red data, call a model for a stop, build a broker other than ``SimBroker``,
or let one manager's failure stop another.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import time as walltime
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

from accounting import TokenPricer
from analyst.commons import CommonsScreens, CommonsSheets, Fetcher, Shortlist
from analyst.fundmanager.books import (
    BookJournal,
    BookMarket,
    BookOrder,
    CorporateActionStatus,
    DecisionReport,
    DelistedNames,
    ExecutionReport,
    FundBook,
    FundDesk,
    SuspendedNames,
    book_rails,
    m17_kill_switch,
    paper_account_id,
)
from analyst.fundmanager.controls import (
    BenchBook,
    BenchmarkLevels,
    BenchmarkUnavailableError,
    BenchState,
    ControlBook,
    ControlState,
    record_mark,
)
from analyst.fundmanager.digest import write_digest
from analyst.fundmanager.job import (
    COMMONS_UNAVAILABLE_EVENT,
    DATA_GAPS_EVENT,
    DATA_WAIT_EVENT,
    MANAGER_CRASHED_EVENT,
    MISSED_SESSION_EVENT,
    ORDERS_LAPSED_EVENT,
    SKIPPED_DATA_RED_EVENT,
    STREAM_DRY,
    STREAM_LIVE,
    DeadlineLLM,
    HoldingMemo,
    StreamJournal,
    book_entry,
    manager_book,
    manager_deadline,
    mandate_fingerprints,
    orders_from_verdicts,
    s0_entry,
)
from analyst.fundmanager.mandate import (
    BenchMandate,
    ControlMandate,
    ManagerMandate,
    Roster,
    load_roster,
)
from analyst.fundmanager.render import PromptTemplate
from analyst.fundmanager.runtime import (
    ManagerCommons,
    ManagerSessionResult,
    SessionStatus,
    run_manager,
)
from analyst.fundmanager.schemas import Action
from analyst.fundmanager.scoreboard import (
    DECISION_EVENT,
    BookMark,
    ControlBuy,
    DecisionOutcome,
    ModelCall,
    OutcomeError,
    RailRefusal,
    Scoreboard,
    ScoreboardError,
    ScoreboardInputs,
    ScoredDecision,
    build_scoreboard,
    inputs_from_journal,
    mark_book,
    outcome_entry,
    resolve_outcome,
    scored_decision_from_entry,
    suspended_holdings_on,
    todays_decisions,
)
from analyst.fundmanager.stops import STOP_EXIT_EVENT, StopBook, StopExit, with_stop_exits
from analyst.journal.evidence import canonical_bytes, digest_of
from analyst.journal.models import Actor, Decision, JournalEntry
from analyst.llm import LLM
from analyst.monitor.interlock import GreenGate
from backtest.book_actions import BookActionSource, ShareRescale
from backtest.cash_interest import RepoRateSchedule, load_repo_rate_schedule
from backtest.fm_circuit import CircuitMarket
from backtest.fm_paper import M17PaperAccount
from backtest.paper_session import (
    EOD_DUE_AT,
    PaperSessionRecord,
    PaperSessionStore,
    SessionOutcome,
)
from dataplatform.clock import IST, Clock, FrozenClock
from dataplatform.logging import get_logger
from execution.broker import Side
from execution.kill_switch import KillSwitch
from execution.recon import Alerter, LoggingAlerter
from execution.sim_broker import SessionMarket

if TYPE_CHECKING:
    from dataplatform.scheduler.registry import JobContext

__all__ = [
    "DESK_IDS",
    "DESK_STATE_VERSION",
    "JOURNAL_CLOCK_AT",
    "CommonsBuilder",
    "M17JobError",
    "M17SessionResult",
    "M17World",
    "ManagerOutcome",
    "ManagerRunner",
    "RunOutcome",
    "SessionCommons",
    "WaitPolicy",
    "desk_kill_switch",
    "main",
    "owed_m17_session",
    "run_m17_job",
    "run_m17_session",
]

_LOG = get_logger(__name__)

#: The ``paper_session.book_id`` the desk's state is recorded under, per stream.
DESK_IDS: Final[Mapping[str, str]] = {
    STREAM_LIVE: "m17_fund_managers",
    STREAM_DRY: "m17_dry_fund_managers",
}
DESK_STATE_VERSION: Final = "m17-desk/1"
#: Every journal entry of a session is stamped at this IST time on the session's date (a frozen
#: clock), so a session's journal is a pure function of the session, as in every replay.
JOURNAL_CLOCK_AT: Final = time(22, 0)
#: What the scoreboard ledger holds, by key.
_LEDGER_KEYS: Final = ("marks", "decisions", "outcomes", "refusals", "calls", "control_buys")


class M17JobError(RuntimeError):
    """The job cannot run the session as asked — always loud; the runner records it FAILED."""


# ── the seams ────────────────────────────────────────────────────────────────────────────────────


class M17World(Protocol):
    """Everything the desk reads about the market. Production is `backtest.fm_world.LakeM17World`;
    a test supplies an in-memory one."""

    def is_session(self, day: date) -> bool: ...

    def sessions(self, start: date, end: date) -> Sequence[date]:
        """The trading sessions in ``[start, end]``, ascending."""
        ...

    def previous_session(self, day: date) -> date: ...

    def next_session(self, day: date) -> date: ...

    def fill_market(self, held: Callable[[], Iterable[str]]) -> SessionMarket:
        """The market one paper account fills against (``held`` names its holdings)."""
        ...

    def book_market(self) -> BookMarket: ...

    def circuit(self) -> CircuitMarket: ...

    def corporate_actions(self) -> BookActionSource: ...

    def delisted(self) -> DelistedNames | None: ...

    def suspended(self) -> SuspendedNames | None:
        """Which held, still-listed names with no bar are suspended (M17.13), under the world's
        stated test for a session that printed normally."""
        ...

    def adjusted_close(self, isin: str, session: date) -> Decimal | None: ...

    def bench_levels(self, label: str, *, through: date, method: str | None) -> BenchmarkLevels:
        """The bench's levels through ``through`` (raises `BenchmarkUnavailableError`)."""
        ...

    def readiness(self, session: date) -> tuple[str, ...]:
        """What the session's Commons build is still waiting for (empty when everything landed)."""
        ...


@dataclass(frozen=True, slots=True)
class SessionCommons:
    """One session's Commons, as the job uses them. ``manager`` is what `run_manager` reads; the
    others feed the controls (``shortlist``), the cap tiers and sectors (``sheets``) and the
    forced reviews (``screens``)."""

    shortlist: Shortlist | None
    manager: ManagerCommons | None
    sheets: CommonsSheets | None = None
    screens: CommonsScreens | None = None
    gaps: tuple[str, ...] = ()
    timings: Mapping[str, float] = field(default_factory=dict)


class CommonsBuilder(Protocol):
    def build(self, session: date, *, clock: Clock) -> SessionCommons:
        """The session's Commons; raises when they cannot be built (the managers then sit out)."""
        ...


class ManagerRunner(Protocol):
    """`analyst.fundmanager.runtime.run_manager`'s signature (a test may script it)."""

    def __call__(
        self,
        mandate: ManagerMandate,
        session: date,
        commons: ManagerCommons,
        book: Any,
        llm: LLM,
        fetcher: Fetcher,
        *,
        journal: BookJournal,
        clock: Clock,
        pricer: TokenPricer | None = None,
        template: PromptTemplate | None = None,
    ) -> ManagerSessionResult: ...


@dataclass(frozen=True, slots=True)
class WaitPolicy:
    """How long the job waits for same-evening data before running with the gaps recorded."""

    max_wait: timedelta = timedelta(minutes=120)
    poll: timedelta = timedelta(minutes=10)


class RunOutcome(StrEnum):
    HOLIDAY = "HOLIDAY"
    ALREADY_DONE = "ALREADY_DONE"
    SKIPPED_DATA_RED = "SKIPPED_DATA_RED"
    HALTED = "HALTED"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True, slots=True)
class ManagerOutcome:
    """What one manager's session came to."""

    book_id: str
    status: str
    calls: int
    staged: int
    refused: int
    stop_exits: int
    seconds: float
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class M17SessionResult:
    """What one run of the job did."""

    session: date
    stream: str
    outcome: RunOutcome
    reason: str = ""
    managers: tuple[ManagerOutcome, ...] = ()
    timings: Mapping[str, float] = field(default_factory=dict)
    scoreboard: Scoreboard | None = None
    scoreboard_error: str | None = None
    rebuilt_digest: str | None = None
    digest_path: Path | None = None
    data_gaps: tuple[str, ...] = ()
    deferred_outcomes: tuple[str, ...] = ()


# ── the desk: every book's state between sessions ────────────────────────────────────────────────


@dataclass(slots=True)
class _ManagerSide:
    stops: StopBook
    memos: dict[str, HoldingMemo]


@dataclass(slots=True)
class _Desk:
    """Every book of one stream, restored or opened for one session."""

    roster: Roster
    books: dict[str, FundBook]
    accounts: dict[str, M17PaperAccount]
    managers: dict[str, _ManagerSide]
    controls: dict[str, ControlState]
    bench: BenchBook
    bench_owed: list[date]
    open_decisions: list[ScoredDecision]
    exits: dict[str, dict[str, date]]
    ledger: dict[str, list[Any]]
    s0: date | None
    opened_on: date
    last_session: date | None

    def to_document(self) -> dict[str, Any]:
        books: dict[str, Any] = {}
        for book_id, book in self.books.items():
            account = self.accounts[book_id]
            document: dict[str, Any] = {
                "account": account.to_document(),
                "account_digest": account.book_digest,
                "last_buy_fill": book.buy_fills_document(),
                "pending_exits": book.pending_exits_document(),
                "staged_sells": book.staged_sells_document(),
            }
            side = self.managers.get(book_id)
            if side is not None:
                document["stops"] = side.stops.to_document()
                document["memos"] = [side.memos[i].to_document() for i in sorted(side.memos)]
            if book_id in self.controls:
                document["control"] = self.controls[book_id].to_document()
            books[book_id] = document
        state = self.bench.state
        return {
            "version": DESK_STATE_VERSION,
            "opened_on": self.opened_on.isoformat(),
            "s0": None if self.s0 is None else self.s0.isoformat(),
            "books": books,
            "bench": None if state is None else state.to_document(),
            "bench_owed": [d.isoformat() for d in self.bench_owed],
            "open_decisions": [d.model_dump(mode="json") for d in self.open_decisions],
            "exits": {
                b: {i: d.isoformat() for i, d in sorted(e.items())}
                for b, e in sorted(self.exits.items())
            },
            "ledger": {k: list(self.ledger.get(k, [])) for k in _LEDGER_KEYS},
        }


def _ledger_inputs(desk: _Desk) -> ScoreboardInputs:
    ledger = desk.ledger
    return ScoreboardInputs(
        s0=desk.s0,
        marks=tuple(BookMark.model_validate(r) for r in ledger["marks"]),
        decisions=tuple(ScoredDecision.model_validate(r) for r in ledger["decisions"]),
        outcomes=tuple(DecisionOutcome.model_validate(r) for r in ledger["outcomes"]),
        refusals=tuple(RailRefusal.model_validate(r) for r in ledger["refusals"]),
        calls=tuple(ModelCall.model_validate(r) for r in ledger["calls"]),
        control_buys=tuple(ControlBuy.model_validate(r) for r in ledger["control_buys"]),
    )


@dataclass(frozen=True, slots=True)
class _Wiring:
    """What every account and book of the desk is built with."""

    world: M17World
    market: BookMarket
    journal: StreamJournal
    kill_switch: KillSwitch
    clock: Clock
    schedule: RepoRateSchedule
    actions: BookActionSource
    circuit: CircuitMarket
    delisted: DelistedNames | None
    suspended: SuspendedNames | None
    alerter: Alerter


def _held_of(cell: list[M17PaperAccount]) -> Callable[[], list[str]]:
    def held() -> list[str]:
        return [] if not cell else [i for i, q in cell[0].quantities().items() if q > 0]

    return held


def _account(
    book_id: str, wiring: _Wiring, *, capital: Decimal, document: Mapping[str, Any] | None
) -> M17PaperAccount:
    cell: list[M17PaperAccount] = []
    market = wiring.world.fill_market(_held_of(cell))
    if document is None:
        account = M17PaperAccount.open(
            account_id=paper_account_id(book_id),
            opening_cash=capital,
            market=market,
            kill_switch=wiring.kill_switch,
            clock=wiring.clock,
            schedule=wiring.schedule,
            corporate_actions=wiring.actions,
            circuit=wiring.circuit,
            alerter=wiring.alerter,
        )
    else:
        account = M17PaperAccount.restore(
            document["account"],
            account_id=paper_account_id(book_id),
            market=market,
            kill_switch=wiring.kill_switch,
            clock=wiring.clock,
            schedule=wiring.schedule,
            book_digest=document["account_digest"],
            corporate_actions=wiring.actions,
            circuit=wiring.circuit,
            alerter=wiring.alerter,
        )
    cell.append(account)
    return account


def _desk(
    roster: Roster, wiring: _Wiring, state: Mapping[str, Any] | None, *, session: date
) -> _Desk:
    """The desk ``state`` describes, or a fresh one opened at ``session``'s open (all cash)."""
    if state is not None and state.get("version") != DESK_STATE_VERSION:
        raise M17JobError(
            f"desk state version {state.get('version')!r} is not {DESK_STATE_VERSION}"
        )
    documents: Mapping[str, Any] = {} if state is None else state["books"]
    books: dict[str, FundBook] = {}
    accounts: dict[str, M17PaperAccount] = {}
    managers: dict[str, _ManagerSide] = {}
    controls: dict[str, ControlState] = {}
    for mandate in roster.books:
        if isinstance(mandate, BenchMandate):
            continue
        document = documents.get(mandate.id)
        if state is not None and document is None:
            raise M17JobError(f"the desk state has no book {mandate.id}; the roster changed")
        account = _account(
            mandate.id, wiring, capital=mandate.opening_capital_inr, document=document
        )
        accounts[mandate.id] = account
        books[mandate.id] = FundBook(
            book_id=mandate.id,
            rails=book_rails(mandate, roster.rails),
            account=account,
            market=wiring.market,
            journal=wiring.journal,
            kill_switch=wiring.kill_switch,
            clock=wiring.clock,
            last_buy_fill=FundBook.buy_fills_from(document["last_buy_fill"]) if document else {},
            pending_exits=FundBook.pending_exits_from(document["pending_exits"])
            if document
            else {},
            delisted=wiring.delisted,
            suspended=wiring.suspended,
            staged_sells=FundBook.staged_sells_from(document.get("staged_sells", {}))
            if document
            else {},
        )
        if isinstance(mandate, ManagerMandate):
            managers[mandate.id] = _ManagerSide(
                stops=StopBook.from_document(document["stops"]) if document else StopBook(),
                memos={
                    m["isin"]: HoldingMemo.from_document(m)
                    for m in (document["memos"] if document else [])
                },
            )
        elif isinstance(mandate, ControlMandate):
            controls[mandate.id] = (
                ControlState.from_document(document["control"]) if document else ControlState()
            )
    (bench_mandate,) = roster.benches
    bench_doc = None if state is None else state["bench"]
    bench = BenchBook(
        bench_mandate,
        journal=wiring.journal,
        clock=wiring.clock,
        state=None if bench_doc is None else BenchState.from_document(bench_doc),
    )
    ledger = {k: [] for k in _LEDGER_KEYS} if state is None else dict(state["ledger"])
    return _Desk(
        roster=roster,
        books=books,
        accounts=accounts,
        managers=managers,
        controls=controls,
        bench=bench,
        bench_owed=[] if state is None else [date.fromisoformat(d) for d in state["bench_owed"]],
        open_decisions=[]
        if state is None
        else [ScoredDecision.model_validate(d) for d in state["open_decisions"]],
        exits={}
        if state is None
        else {
            b: {i: date.fromisoformat(d) for i, d in e.items()} for b, e in state["exits"].items()
        },
        ledger={k: list(ledger.get(k, [])) for k in _LEDGER_KEYS},
        s0=None if state is None or state["s0"] is None else date.fromisoformat(state["s0"]),
        opened_on=session if state is None else date.fromisoformat(state["opened_on"]),
        last_session=None,
    )


# ── one session ──────────────────────────────────────────────────────────────────────────────────


class _Stages:
    """Wall-time per stage, for the run report only (never a decision input)."""

    def __init__(self) -> None:
        self.seconds: dict[str, float] = {}
        self._mark = walltime.perf_counter()

    def done(self, stage: str) -> None:
        now = walltime.perf_counter()
        self.seconds[stage] = round(self.seconds.get(stage, 0.0) + now - self._mark, 3)
        self._mark = now


def _write(journal: BookJournal, item: tuple[JournalEntry, Any]) -> None:
    entry, evidence = item
    journal.append(entry, evidence=evidence)


def _journal_digest(entries: Sequence[JournalEntry]) -> str:
    return digest_of(canonical_bytes([json.loads(e.model_dump_json()) for e in entries]))


def _state_digest(state: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def run_m17_session(
    session: date,
    *,
    world: M17World,
    commons: CommonsBuilder,
    store: PaperSessionStore,
    journal: StreamJournal,
    gate: GreenGate,
    llm: LLM,
    fetcher: Fetcher,
    clock: Clock,
    kill_switch: KillSwitch,
    roster: Roster | None = None,
    start: bool = False,
    sleep: Callable[[float], None] = walltime.sleep,
    wait: WaitPolicy | None = None,
    runner: ManagerRunner = run_manager,
    pricer: TokenPricer | None = None,
    template: PromptTemplate | None = None,
    digest_dir: Path | None = None,
    entries_reader: Callable[[], Sequence[JournalEntry]] | None = None,
    alerter: Alerter | None = None,
) -> M17SessionResult:
    """Run the M17 desk for ``session`` (module docstring, steps 1-9).

    What it assumes: ``session``'s EOD has run; ``clock`` is the real (injected) clock — the
    deadline and the data wait read it — while every journal entry is stamped by a clock frozen
    on the session's evening; ``store`` and ``journal`` commit together in the caller's
    transaction; ``kill_switch`` is the one M17 switch.
    What it never does: stage on a red day or a tripped switch, call a model for a stop, accept a
    decision made after the deadline, or let one manager's failure stop another.
    """
    roster = roster or load_roster()
    stages = _Stages()
    first = len(journal.written)  # this session's entries are journal.written[first:]
    stream = journal.stream
    desk_id = DESK_IDS[stream]
    if not world.is_session(session):
        _LOG.info("fm_job.holiday", session=session.isoformat(), stream=stream)
        return M17SessionResult(session, stream, RunOutcome.HOLIDAY, "not a trading session")
    existing = store.get(desk_id, session)
    if existing is not None and existing.outcome.moved_book:
        return M17SessionResult(
            session, stream, RunOutcome.ALREADY_DONE, f"{session} is already {existing.outcome}"
        )
    prior = store.latest_completed(desk_id, before=session)
    if start and prior is not None:
        raise M17JobError(
            f"{stream} already ran on {prior.trading_date.isoformat()}; S0 opens a stream once"
        )
    if not start and prior is None and stream == STREAM_LIVE:
        raise M17JobError(
            "the live M17 stream has not started: the first live run is `--start S0` on S0 "
            "(after the M17.8 dry run and the owner's go)"
        )
    book_clock = FrozenClock(datetime.combine(session, JOURNAL_CLOCK_AT, tzinfo=IST))

    # 2. the interlock
    try:
        verdict = gate(session)
        red = None if verdict else (verdict.reason or "data is not green")
    except Exception as exc:  # a status read that failed is red, never green (invariant #10)
        red = f"the status read failed: {type(exc).__name__}: {exc}"
    if red is not None:
        for book in roster.books:
            _write(
                journal,
                book_entry(
                    book_id=book.id,
                    session=session,
                    clock=book_clock,
                    decision=Decision.SKIPPED_DATA_RED,
                    event=SKIPPED_DATA_RED_EVENT,
                    rationale=f"data is red for {session.isoformat()} ({red}); nothing decided, "
                    "nothing staged",
                    payload={"reason": red},
                ),
            )
        store.record(
            PaperSessionRecord(
                book_id=desk_id,
                trading_date=session,
                outcome=SessionOutcome.SKIPPED_DATA_RED,
                reason=red[:500],
                rebalanced=False,
                journal_digest=_journal_digest(journal.written[first:]),
            ),
            recorded_at=clock.now(),
        )
        _LOG.warning("fm_job.data_red", session=session.isoformat(), stream=stream, reason=red)
        return M17SessionResult(session, stream, RunOutcome.SKIPPED_DATA_RED, red)
    stages.done("interlock")

    wiring = _Wiring(
        world=world,
        market=world.book_market(),
        journal=journal,
        kill_switch=kill_switch,
        clock=book_clock,
        schedule=load_repo_rate_schedule(),
        actions=world.corporate_actions(),
        circuit=world.circuit(),
        delisted=world.delisted(),
        suspended=world.suspended(),
        alerter=alerter or LoggingAlerter(),
    )
    desk = _desk(roster, wiring, None if prior is None else prior.book_state, session=session)
    desk.last_session = None if prior is None else prior.trading_date
    if prior is None:
        _open_bench(desk, world, session)
    if start:
        desk.s0 = session
        for fingerprint in mandate_fingerprints(roster, template=template).values():
            _write(journal, s0_entry(fingerprint, session=session, clock=book_clock))
    stages.done("restore")

    # 3. the previous cycle's close: fills, recon, marks, outcomes
    halted = kill_switch.is_tripped
    if halted:
        reports = {book_id: book.halt(session) for book_id, book in desk.books.items()}
    else:
        _lapse_stale(desk, session, book_clock)
        reports = FundDesk(list(desk.books.values()), kill_switch=kill_switch).execute(session)
    _after_fills(desk, world, reports, session)
    marks = _mark_all(desk, world, reports, session)
    for fund_book in desk.books.values():
        fund_book.journal_suspended_holdings(session)
    deferred = _resolve_outcomes(desk, world, session, book_clock)
    stages.done("fills_marks_outcomes")

    # a recon break or an unbookable corporate action may have tripped the switch just now
    halted = halted or kill_switch.is_tripped
    managers: list[ManagerOutcome] = []
    gaps: tuple[str, ...] = ()
    control_buys: list[ControlBuy] = []
    if halted:
        for fund_book in desk.books.values():
            fund_book.decide(session, ())
        stages.done("halted")
    else:
        # 4. mechanical stops
        exits = _stop_exits(desk, session)
        stages.done("stops")
        # 5. wait for the session's data
        deadline = manager_deadline(world.next_session(session))
        gaps = _wait_for_data(
            world, session, journal, clock, book_clock, sleep, wait or WaitPolicy(), deadline
        )
        stages.done("data_wait")
        # 6. the Commons
        built, commons_error = _build_commons(commons, session, book_clock)
        stages.done("commons")
        # 7. managers, one at a time
        for mandate in roster.managers:
            managers.append(
                _run_one_manager(
                    mandate,
                    desk,
                    session,
                    built,
                    commons_error,
                    exits.get(mandate.id, ()),
                    llm=llm,
                    fetcher=fetcher,
                    clock=clock,
                    book_clock=book_clock,
                    sleep=sleep,
                    deadline=deadline,
                    runner=runner,
                    pricer=pricer,
                    template=template,
                )
            )
            stages.done(f"manager:{mandate.id}")
        # 8. controls
        cap_tiers: dict[str, str | None] = (
            {}
            if built is None or built.sheets is None
            else {r.isin: r.cap_tier for r in built.sheets.universe}
        )
        for control in roster.controls:
            control_book = ControlBook(
                control, desk.books[control.id], roster.rails, state=desk.controls[control.id]
            )
            run = control_book.run(session, None if built is None else built.shortlist, cap_tiers)
            desk.controls[control.id] = run.state
            control_buys.extend(run.buys)
        stages.done("controls")

    # 9. persist, score, digest
    written = journal.written[first:]
    _extend_ledger(desk, written, marks, control_buys)
    state = desk.to_document()
    store.record(
        PaperSessionRecord(
            book_id=desk_id,
            trading_date=session,
            outcome=SessionOutcome.COMPLETED,
            reason="halted: the M17 kill switch is tripped" if halted else "decided",
            rebalanced=False,
            journal_digest=_journal_digest(written),
            book_state=state,
            book_digest=_state_digest(state),
        ),
        recorded_at=clock.now(),
    )
    stages.done("persist")
    result = _score_and_digest(
        desk, journal, written, session, entries_reader=entries_reader, digest_dir=digest_dir
    )
    stages.done("scoreboard_digest")
    outcome = RunOutcome.HALTED if halted else RunOutcome.COMPLETED
    _LOG.info(
        "fm_job.session_done",
        session=session.isoformat(),
        stream=stream,
        outcome=outcome.value,
        managers={m.book_id: m.status for m in managers},
        timings=stages.seconds,
    )
    return M17SessionResult(
        session=session,
        stream=stream,
        outcome=outcome,
        reason="halted" if halted else "decided",
        managers=tuple(managers),
        timings=dict(stages.seconds),
        scoreboard=result[0],
        scoreboard_error=result[1],
        rebuilt_digest=result[2],
        digest_path=result[3],
        data_gaps=gaps,
        deferred_outcomes=tuple(deferred),
    )


# -- step 3 ---------------------------------------------------------------------------------------


def _open_bench(desk: _Desk, world: M17World, session: date) -> None:
    """Buy the bench at the close before the desk's first session (the last level knowable at
    that session's open). Raises `BenchmarkUnavailableError` when the lake has no level there."""
    (bench,) = desk.roster.benches
    base = world.previous_session(session)
    levels = world.bench_levels(bench.benchmark, through=base, method=None)
    desk.bench.open(levels, base)


def _lapse_stale(desk: _Desk, session: date, clock: Clock) -> None:
    """Orders staged for a session the desk never executed (red, missed) lapse unfilled."""
    previous = session - timedelta(days=1)
    for book_id, account in desk.accounts.items():
        lapsed = account.lapse(previous)
        if lapsed:
            _write(
                desk.books[book_id].journal,
                book_entry(
                    book_id=book_id,
                    session=session,
                    clock=clock,
                    decision=Decision.HOLD,
                    event=ORDERS_LAPSED_EVENT,
                    actor=Actor.EXEC,
                    rationale=(
                        f"{len(lapsed)} order(s) staged for a session the desk did not execute "
                        "lapse unfilled; there is no catch-up trading on a stale view"
                    ),
                    payload={"lapsed": ",".join(lapsed)},
                ),
            )


def _after_fills(
    desk: _Desk, world: M17World, reports: Mapping[str, ExecutionReport], session: date
) -> None:
    """Rescale stops on a split or bonus; note when a held position was fully sold.

    A stop is a price, struck on the close of the session its BUY was staged, so on a split's
    ex-date it must move by the split's factor whether or not the account held shares at the
    open. A held name's rescale is the one the account booked. A name whose BUY fills *on* the
    ex-date holds no entitled shares, so the account books nothing — yet its stop was struck on
    the pre-split close and the first post-split close would trip it (M17.11). Its stop takes the
    store's own split/bonus ex this session, by that factor and nothing else, so the protected
    fraction of the position is exactly what was declared.
    """
    previous: date | None
    try:
        previous = world.previous_session(session)
    except M17JobError:
        previous = None
    due = [
        a
        for a in world.corporate_actions().between(previous, session)
        if isinstance(a, ShareRescale)
    ]
    for book_id, report in reports.items():
        side = desk.managers.get(book_id)
        if side is not None:
            rescaled: set[tuple[str, date]] = set()
            for action in report.corporate_actions:
                if action.status is CorporateActionStatus.BOOKED and action.rescale is not None:
                    side.stops.rescale(
                        action.isin, numerator=action.rescale[0], denominator=action.rescale[1]
                    )
                    rescaled.add((action.isin, action.ex_date))
            for rescale in due:
                if (rescale.isin, rescale.ex_date) in rescaled:
                    continue
                if rescale.isin in side.stops.stops:
                    side.stops.rescale(
                        rescale.isin, numerator=rescale.numerator, denominator=rescale.denominator
                    )
                    rescaled.add((rescale.isin, rescale.ex_date))
                    _LOG.info(
                        "fm_job.stop_rescaled_unheld",
                        book=book_id,
                        isin=rescale.isin,
                        ex_date=rescale.ex_date.isoformat(),
                        ratio=f"{rescale.numerator}:{rescale.denominator}",
                    )
        held = desk.accounts[book_id].quantities()
        for fill in report.fills:
            if fill.side is Side.SELL and held.get(fill.isin, 0) <= 0:
                desk.exits.setdefault(book_id, {})[fill.isin] = session
        if side is not None:
            # A memo is written when its BUY is staged; once that BUY's session has executed, a
            # name not held (sold, or the buy never filled) has no position for it to describe.
            for isin in [i for i in side.memos if held.get(i, 0) <= 0]:
                del side.memos[isin]


def _mark_all(
    desk: _Desk, world: M17World, reports: Mapping[str, ExecutionReport], session: date
) -> list[BookMark]:
    """Every book's mark, and the bench's (and any it still owes, oldest first)."""
    marks = [
        mark_book(book, session, execution=reports.get(book_id))
        for book_id, book in desk.books.items()
    ]
    for mark in marks:
        record_mark(mark, desk.books[mark.book_id])
    owed = sorted({*desk.bench_owed, session})
    still_owed: list[date] = []
    state = desk.bench.state
    for day in owed:
        if state is None or day <= state.base_session:
            continue
        try:
            levels = world.bench_levels(
                desk.roster.benches[0].benchmark, through=day, method=state.method
            )
            marks.append(desk.bench.mark(levels, day))
        except BenchmarkUnavailableError as exc:
            still_owed.append(day)
            _LOG.warning("fm_job.bench_mark_owed", session=day.isoformat(), detail=str(exc))
    desk.bench_owed = still_owed
    return marks


class _Prices:
    def __init__(self, world: M17World, levels: BenchmarkLevels) -> None:
        self._world = world
        self._levels = levels

    def adjusted_close(self, isin: str, session: date) -> Decimal | None:
        return self._world.adjusted_close(isin, session)

    def bench_level(self, session: date) -> Decimal | None:
        return self._levels.level(session)


def _resolve_outcomes(desk: _Desk, world: M17World, session: date, clock: Clock) -> list[str]:
    """Journal every open decision that has resolved by ``session``; the keys still deferred."""
    state = desk.bench.state
    if not desk.open_decisions or state is None:
        return []
    levels = world.bench_levels(
        desk.roster.benches[0].benchmark, through=session, method=state.method
    )
    prices = _Prices(world, levels)
    still: list[ScoredDecision] = []
    deferred: list[str] = []
    for decision in desk.open_decisions:
        calendar = list(world.sessions(decision.decided_on, session))
        exited = desk.exits.get(decision.book_id, {}).get(decision.isin)
        try:
            outcome = resolve_outcome(
                decision,
                calendar=calendar,
                as_of=session,
                prices=prices,
                exited_on=exited if exited is not None and exited > decision.decided_on else None,
                delisted=world.delisted(),
                suspended=world.suspended(),
            )
        except OutcomeError as exc:
            # A due close not in yet (an L2 refresh behind the session): asked again tomorrow;
            # the resolution session is fixed by the calendar, so a late answer is the same one.
            deferred.append(decision.key)
            still.append(decision)
            _LOG.warning("fm_job.outcome_deferred", decision=decision.key, detail=str(exc))
            continue
        if outcome is None:
            still.append(decision)
            continue
        entry, evidence = outcome_entry(outcome, clock=clock)
        desk.books[decision.book_id].journal.append(entry, evidence=evidence)
        desk.ledger["outcomes"].append(outcome.model_dump(mode="json"))
    desk.open_decisions = still
    return deferred


# -- step 4 ---------------------------------------------------------------------------------------


def _stop_exits(desk: _Desk, session: date) -> dict[str, tuple[StopExit, ...]]:
    out: dict[str, tuple[StopExit, ...]] = {}
    for book_id, side in desk.managers.items():
        book = desk.books[book_id]
        held = {i: q for i, q in book.account.quantities().items() if q > 0}
        closes: dict[str, Decimal] = {}
        for isin in held:
            close = book.market.close(isin, session)
            if close is not None:
                closes[isin] = close
        out[book_id] = side.stops.on_close(session, closes, held, exiting=tuple(book.pending_exits))
    return out


# -- step 5 ---------------------------------------------------------------------------------------


def _wait_for_data(
    world: M17World,
    session: date,
    journal: StreamJournal,
    clock: Clock,
    book_clock: Clock,
    sleep: Callable[[float], None],
    policy: WaitPolicy,
    deadline: datetime,
) -> tuple[str, ...]:
    missing = world.readiness(session)
    if not missing:
        return ()
    started = clock.now()
    _write(
        journal,
        book_entry(
            book_id=None,
            session=session,
            clock=book_clock,
            decision=Decision.HEARTBEAT,
            event=DATA_WAIT_EVENT,
            rationale=(
                f"waiting up to {int(policy.max_wait.total_seconds() // 60)} min for the "
                f"session's data before the Commons build: {', '.join(missing)}"
            ),
            payload={
                "phase": "START",
                "missing": ",".join(missing),
                "started_at": started.isoformat(),
            },
        ),
    )
    _LOG.warning("fm_job.data_wait", session=session.isoformat(), missing=list(missing))
    limit = min(started + policy.max_wait, deadline)
    while missing and clock.now() + policy.poll <= limit:
        sleep(policy.poll.total_seconds())
        missing = world.readiness(session)
    waited = clock.now() - started
    _write(
        journal,
        book_entry(
            book_id=None,
            session=session,
            clock=book_clock,
            decision=Decision.HEARTBEAT,
            event=DATA_GAPS_EVENT if missing else DATA_WAIT_EVENT,
            rationale=(
                f"still missing after {int(waited.total_seconds() // 60)} min: "
                f"{', '.join(missing)}; the Commons are built with these recorded as gaps"
                if missing
                else f"the session's data landed after {int(waited.total_seconds() // 60)} min"
            ),
            payload={
                "phase": "GAPS" if missing else "LANDED",
                "missing": ",".join(missing),
                "waited_seconds": str(int(waited.total_seconds())),
            },
        ),
    )
    return missing


# -- step 6 ---------------------------------------------------------------------------------------


def _build_commons(
    builder: CommonsBuilder, session: date, clock: Clock
) -> tuple[SessionCommons | None, str | None]:
    try:
        built = builder.build(session, clock=clock)
    except Exception as exc:  # the managers sit out; the job journals why for each of them
        _LOG.error(
            "fm_job.commons_failed",
            session=session.isoformat(),
            error=f"{type(exc).__name__}: {exc}",
        )
        return None, f"{type(exc).__name__}: {exc}"
    if built.manager is None:
        return built, "the Commons were built without a manager view"
    return built, None


# -- step 7 ---------------------------------------------------------------------------------------


def _forced_reviews(
    built: SessionCommons | None, held: Iterable[str], since: date | None
) -> dict[str, str]:
    """A held name with an integrity event knowable after the last session the desk ran."""
    if built is None or built.screens is None:
        return {}
    held_set = set(held)
    out: dict[str, str] = {}
    for exclusion in built.screens.exclusions.excluded:
        if exclusion.isin not in held_set:
            continue
        new = [e for e in exclusion.events if since is None or e.knowable_date > since]
        if new:
            event = new[-1]
            out[exclusion.isin] = (
                f"integrity event {', '.join(event.categories)} knowable "
                f"{event.knowable_date.isoformat()}: {event.subject}"
            )
    return out


def _sectors(
    built: SessionCommons | None, book: FundBook, held: Iterable[str]
) -> dict[str, str | None]:
    rows = (
        {}
        if built is None or built.sheets is None
        else {r.isin: r.sector for r in built.sheets.universe}
    )
    return {isin: rows.get(isin) or book.market.sector(isin) for isin in held}


def _journal_manager_line(
    book: FundBook,
    session: date,
    clock: Clock,
    *,
    event: str,
    rationale: str,
    payload: Mapping[str, str],
) -> None:
    _write(
        book.journal,
        book_entry(
            book_id=book.book_id,
            session=session,
            clock=clock,
            decision=Decision.ESCALATE,
            event=event,
            rationale=rationale,
            payload=payload,
        ),
    )


def _run_one_manager(
    mandate: ManagerMandate,
    desk: _Desk,
    session: date,
    built: SessionCommons | None,
    commons_error: str | None,
    exits: Sequence[StopExit],
    *,
    llm: LLM,
    fetcher: Fetcher,
    clock: Clock,
    book_clock: Clock,
    sleep: Callable[[float], None],
    deadline: datetime,
    runner: ManagerRunner,
    pricer: TokenPricer | None,
    template: PromptTemplate | None,
) -> ManagerOutcome:
    started = walltime.perf_counter()
    book = desk.books[mandate.id]
    side = desk.managers[mandate.id]
    status = "DECIDED"
    reason: str | None = None
    calls = 0
    orders_out: list[BookOrder] = []
    stops: Sequence[Any] = ()
    tightenings: Sequence[Any] = ()
    memos: Sequence[HoldingMemo] = ()

    if book.kill_switch.is_tripped:
        report = book.decide(session, ())
        return _manager_outcome(mandate.id, "HALTED", 0, report, 0, started, None)
    if commons_error is not None or built is None or built.manager is None:
        status, reason = COMMONS_UNAVAILABLE_EVENT, commons_error or "no Commons"
        _journal_manager_line(
            book,
            session,
            book_clock,
            event=COMMONS_UNAVAILABLE_EVENT,
            rationale=f"the Commons for {session.isoformat()} could not be built ({reason}); "
            "the manager is not asked, and only its mechanical stop exits stage",
            payload={"reason": reason[:500]},
        )
    elif clock.now() >= deadline:
        status, reason = MISSED_SESSION_EVENT, "the deadline passed before the manager started"
        _journal_missed(book, session, book_clock, deadline, clock, reason, None)
    else:
        held = {i: q for i, q in book.account.quantities().items() if q > 0}
        # a stop exit held over on a suspended name is still the stop's decision on that name
        stopped = {e.isin for e in exits} | {
            isin for isin, p in book.pending_exits.items() if p.event == STOP_EXIT_EVENT
        }
        notes: dict[str, list[str]] = {}
        for exit_ in exits:
            notes.setdefault(exit_.isin, []).append(
                f"STOP HIT: the {session.isoformat()} close {exit_.close} is below the stop "
                f"{exit_.level}; the book sells it at the next open mechanically and any "
                "decision on it today is dropped"
            )
        delisted = book.delisted
        for isin in held:
            if book.market.close(isin, session) is None and delisted is not None:
                last = delisted.last_traded(isin, session)
                if last is not None:
                    notes.setdefault(isin, []).append(
                        f"DELISTED: last traded {last.session.isoformat()} at {last.raw_close}; "
                        "valued there until a corporate action converts it"
                    )
        for isin in held:
            if isin in book.pending_exits and book.suspension(isin, session) is not None:
                notes.setdefault(isin, []).append(
                    "a sell of this suspended name is already held over; it is offered each "
                    "session until the name trades again"
                )
        view = manager_book(
            book,
            session,
            memos=side.memos,
            stops=side.stops,
            sectors=_sectors(built, book, held),
            notes=notes,
            forced=_forced_reviews(built, held, desk.last_session),
        )
        timed = DeadlineLLM(llm, deadline, clock, sleep)
        try:
            result = runner(
                mandate,
                session,
                built.manager,
                view,
                timed,
                fetcher,
                journal=book.journal,
                clock=book_clock,
                pricer=pricer,
                template=template,
            )
        except Exception as exc:  # isolation: one manager's defect never stops another
            status, reason = MANAGER_CRASHED_EVENT, f"{type(exc).__name__}: {exc}"
            _journal_manager_line(
                book,
                session,
                book_clock,
                event=MANAGER_CRASHED_EVENT,
                rationale=f"the manager's session raised ({reason}); nothing of its own is "
                "staged, its stop exits still are",
                payload={"error": reason[:1000]},
            )
            _LOG.error("fm_job.manager_crashed", book=mandate.id, error=reason)
        else:
            calls = result.calls + result.repairs
            late = result.status is SessionStatus.MANAGER_ERROR and timed.missed
            if late:
                status, reason = MISSED_SESSION_EVENT, result.reason
                _journal_missed(book, session, book_clock, deadline, clock, reason, timed)
            else:
                status, reason = result.status.value, result.reason
                if result.status is SessionStatus.DECIDED:
                    closes = {
                        isin: price
                        for isin in {v.decision.isin for v in result.accepted}
                        if (price := book.market.close(isin, session)) is not None
                    }
                    live = [v for v in result.accepted if v.decision.isin not in stopped]
                    # M17.13: a BUY of a name with no close today is never staged; it goes to the
                    # book whole, which journals it UNPRICED. A TRIM of a suspended holding is
                    # sized at its last traded close (the book's own valuation) and held over.
                    unpriced_buys = [
                        v
                        for v in live
                        if v.decision.action is Action.BUY
                        and v.decision.isin not in closes
                        and v.round_trip is not None
                    ]
                    for verdict in live:
                        isin = verdict.decision.isin
                        if isin not in closes and isin in held:
                            last = book.suspension(isin, session)
                            if last is not None:
                                closes[isin] = last.raw_close
                    planned = orders_from_verdicts(
                        [v for v in live if all(v is not u for u in unpriced_buys)],
                        book=view,
                        held=held,
                        closes=closes,
                        session=session,
                    )
                    orders_out = list(planned.orders) + [
                        BookOrder(
                            v.decision.isin,
                            Side.BUY,
                            v.round_trip.quantity,
                            v.decision.rationale,
                        )
                        for v in unpriced_buys
                        if v.round_trip is not None
                    ]
                    stops, tightenings, memos = planned.stops, planned.tightenings, planned.memos
    report = book.decide(session, with_stop_exits(orders_out, list(exits)))
    staged_buys = {o.isin for o, _ in report.staged if o.side is Side.BUY}
    for declaration in stops:
        if declaration.isin in staged_buys:
            side.stops.declare(
                declaration.isin,
                stop_pct=declaration.stop_pct,
                reference_price=declaration.reference_price,
                session=session,
            )
    for memo in memos:
        if memo.isin in staged_buys:
            side.memos[memo.isin] = memo
    for tightening in tightenings:
        if tightening.isin in side.stops.stops:
            side.stops.tighten(tightening.isin, level=tightening.level, session=session)
    return _manager_outcome(mandate.id, status, calls, report, len(exits), started, reason)


def _journal_missed(
    book: FundBook,
    session: date,
    book_clock: Clock,
    deadline: datetime,
    clock: Clock,
    reason: str | None,
    timed: DeadlineLLM | None,
) -> None:
    payload = {
        "deadline": deadline.isoformat(),
        "detected_at": clock.now().isoformat(),
        "reason": (reason or "")[:500],
    }
    if timed is not None:
        payload["rate_limit_retries"] = str(timed.retries)
        payload["rate_limit_waited_seconds"] = str(int(timed.waited.total_seconds()))
        payload["late_calls"] = str(len(timed.late))
        payload["late_tokens_in"] = str(sum(u.prompt_tokens for u in timed.late))
        payload["late_tokens_out"] = str(sum(u.output_tokens for u in timed.late))
    _journal_manager_line(
        book,
        session,
        book_clock,
        event=MISSED_SESSION_EVENT,
        rationale=(
            f"the manager had not finished by the {deadline.isoformat()} deadline; it stages "
            "nothing for this session (no catch-up on a stale view); its stop exits still stage"
        ),
        payload=payload,
    )
    _LOG.warning("fm_job.missed_session", book=book.book_id, session=session.isoformat())


def _manager_outcome(
    book_id: str,
    status: str,
    calls: int,
    report: DecisionReport,
    stop_exits: int,
    started: float,
    reason: str | None,
) -> ManagerOutcome:
    return ManagerOutcome(
        book_id=book_id,
        status=status,
        calls=calls,
        staged=len(report.staged),
        refused=len(report.refused),
        stop_exits=stop_exits,
        seconds=round(walltime.perf_counter() - started, 3),
        reason=reason,
    )


# -- step 9 ---------------------------------------------------------------------------------------


def _extend_ledger(
    desk: _Desk,
    written: Sequence[JournalEntry],
    marks: Sequence[BookMark],
    control_buys: Sequence[ControlBuy],
) -> None:
    """Add this session's scoreboard inputs to the desk's ledger, from the objects themselves
    where the job holds them (marks, outcomes — already added — control buys) and from the lines
    it journaled for the rest (decisions, refusals, model calls)."""
    managers = {m.id for m in desk.roster.managers}
    books = {b.id for b in desk.roster.books}
    ledger = desk.ledger
    ledger["marks"].extend(m.model_dump(mode="json") for m in marks)
    ledger["control_buys"].extend(b.model_dump(mode="json") for b in control_buys)
    for entry in written:
        if entry.case_id not in books:
            continue
        if entry.payload.get("event") == DECISION_EVENT:
            scored = scored_decision_from_entry(entry)
            if scored is not None:
                ledger["decisions"].append(scored.model_dump(mode="json"))
                desk.open_decisions.append(scored)
        if entry.decision is Decision.RAIL_BLOCK:
            rails = tuple(r for r in entry.payload.get("rails", "").split(",") if r)
            if rails:
                ledger["refusals"].append(
                    RailRefusal(
                        book_id=entry.case_id,
                        session=entry.trading_date,
                        isin=entry.isin,
                        rails=rails,
                    ).model_dump(mode="json")
                )
        if entry.tokens is not None and entry.model is not None and entry.case_id in managers:
            ledger["calls"].append(
                ModelCall(
                    book_id=entry.case_id,
                    session=entry.trading_date,
                    model=entry.model,
                    tokens_in=entry.tokens.tokens_in,
                    tokens_out=entry.tokens.tokens_out,
                    cost_inr=entry.tokens.cost_inr,
                ).model_dump(mode="json")
            )


def _score_and_digest(
    desk: _Desk,
    journal: StreamJournal,
    written: Sequence[JournalEntry],
    session: date,
    *,
    entries_reader: Callable[[], Sequence[JournalEntry]] | None,
    digest_dir: Path | None,
) -> tuple[Scoreboard | None, str | None, str | None, Path | None]:
    roster = desk.roster
    error: str | None = None
    scoreboard: Scoreboard | None = None
    try:
        scoreboard = build_scoreboard(roster, _ledger_inputs(desk))
    except ScoreboardError as exc:
        error = str(exc)
        _LOG.error("fm_job.scoreboard_refused", session=session.isoformat(), error=error)
    rebuilt: str | None = None
    if entries_reader is not None:
        entries = [journal.unprefixed(e) for e in entries_reader()]
        try:
            rebuilt = build_scoreboard(roster, inputs_from_journal(entries, roster)).digest()
        except ScoreboardError as exc:
            rebuilt = f"refused: {exc}"
        if scoreboard is not None and rebuilt != scoreboard.digest():
            error = (
                f"the journal rebuilds scoreboard {rebuilt[:16]}, not the job's "
                f"{scoreboard.digest()[:16]}: the journal and the desk ledger disagree"
            )
            _LOG.error("fm_job.scoreboard_mismatch", session=session.isoformat(), error=error)
    path: Path | None = None
    if digest_dir is not None:
        shown = scoreboard or build_scoreboard(roster, ScoreboardInputs(s0=desk.s0))
        _, lines = todays_decisions(written, roster, session=session)
        _, suspended = suspended_holdings_on(written, roster, session=session)
        path = write_digest(shown, session, lines, directory=digest_dir, suspended=suspended)
    return scoreboard, error, rebuilt, path


# ── the scheduler and the CLI ────────────────────────────────────────────────────────────────────


def owed_m17_session(world: M17World, now: datetime) -> date | None:
    """The session the job owes at ``now``: today's from `EOD_DUE_AT` IST, else the previous one
    (the paper session's rule, so a retry after midnight decides the session that failed)."""
    local = now.astimezone(IST)
    day = local.date() if local.time() >= EOD_DUE_AT else local.date() - timedelta(days=1)
    for _ in range(10):
        if world.is_session(day):
            return day
        day -= timedelta(days=1)
    return None


def run_m17_job(
    context: JobContext,
    *,
    dry_run: bool = True,
    start: date | None = None,
    session: date | None = None,
) -> M17SessionResult | None:
    """The ``m17_fund_managers`` scheduler job: the owed session of the M17 desk.

    What it does: opens one Postgres transaction and runs `run_m17_session` for the owed session
    (or ``session``) over the lake (`backtest.fm_world`), the real append-only journal, the
    ``paper_session`` ledger, the status interlock, the desk's LLM (``M17_LLM_PROVIDER``) and the
    Commons fetcher, then commits. ``dry_run`` (the registered default until M17.8's go) files
    everything under the ``m17-dry`` stream. ``start`` must equal the session it runs.
    What it never does: route to a real broker, or commit a half-run session — an exception
    rolls the whole session back and the runner records the run FAILED.
    """
    from backtest.fm_world import production_run

    return production_run(context, dry_run=dry_run, start=start, session=session)


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m backtest.fm_job``: one session of the desk from a shell.

    ``--memory`` runs against an in-memory journal, desk store and Commons stores (and a scratch
    kill switch and fetch cache), reading the lake and the status database only — the wiring
    rehearsal; nothing persists.
    """
    from backtest.fm_world import cli_run

    parser = argparse.ArgumentParser(prog="python -m backtest.fm_job")
    parser.add_argument("--session", type=date.fromisoformat, default=None)
    parser.add_argument("--dry-run", action="store_true", help="the m17-dry stream")
    parser.add_argument("--start", type=date.fromisoformat, default=None, metavar="S0")
    parser.add_argument(
        "--memory", action="store_true", help="in-memory journal and stores; nothing persists"
    )
    parser.add_argument(
        "--stub-llm",
        action="store_true",
        help="StubLLM for the managers and digests (with --memory only; a persisted run refuses)",
    )
    parser.add_argument("--no-wait", action="store_true", help="do not wait for late data")
    parser.add_argument("--scratch", type=Path, default=None)
    args = parser.parse_args(argv)
    result = cli_run(
        session=args.session,
        dry_run=args.dry_run,
        start=args.start,
        memory=args.memory,
        stub_llm=args.stub_llm,
        no_wait=args.no_wait,
        scratch=args.scratch or Path(tempfile.mkdtemp(prefix="m17-")),
    )
    print(
        json.dumps(
            {
                "session": result.session.isoformat(),
                "stream": result.stream,
                "outcome": result.outcome.value,
                "reason": result.reason,
                "timings_seconds": dict(result.timings),
                "managers": [
                    {
                        "book": m.book_id,
                        "status": m.status,
                        "calls": m.calls,
                        "staged": m.staged,
                        "refused": m.refused,
                        "stop_exits": m.stop_exits,
                        "seconds": m.seconds,
                    }
                    for m in result.managers
                ],
                "data_gaps": list(result.data_gaps),
                "scoreboard": None if result.scoreboard is None else result.scoreboard.k_of_n,
                "scoreboard_error": result.scoreboard_error,
                "digest": None if result.digest_path is None else str(result.digest_path),
            },
            indent=2,
        )
    )
    return 0


def desk_kill_switch(data_root: Path, *, clock: Clock) -> KillSwitch:
    """The one M17 kill switch (`analyst.fundmanager.books.m17_kill_switch`), dry run included."""
    return m17_kill_switch(data_root, clock=clock)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
