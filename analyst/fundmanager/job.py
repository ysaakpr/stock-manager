"""A10 · M17.7 — the daily job's decision-side pieces: no broker, no lake, no wall clock.

The daily M17 job's composition root is `backtest.fm_job` (beside the M15.3 paper session),
because it builds the paper accounts and `analyst/` never names a concrete broker (invariant #5,
`tests/unit/test_sim_broker.py`). What the job needs that is about *managers* rather than about
accounts lives here, where it can be tested without either:

- the **journal events** the job writes beside the runtime's and the books' (`SKIPPED_DATA_RED`,
  ``MISSED_SESSION``, the ``M17_S0`` line with its mandate hash, the data wait), and the
  `StreamJournal` that files a dry run under its own ``m17-dry`` stream;
- the **mandate hashes** pre-registration §7 journals at S0 (`mandate_fingerprints`);
- the **deadline** (§4: a manager unfinished by 08:30 IST on the next session misses it) and the
  rate-limit back-off inside it (`DeadlineLLM`);
- the manager's **book view** (`manager_book`: weight, thesis, invalidations, stop, forced review —
  never a cost basis) and the step from accepted decisions to book orders, stops and holding memos
  (`orders_from_verdicts`).

What this module never does: place or stage an order, read a price it is not handed, read a wall
clock (every instant comes from an injected `Clock`), or show one manager anything of another's.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from typing import Any, Final

from analyst.commons.screens import SCREENS_RULE_HASH
from analyst.commons.shortlist import SHORTLIST_RULE_HASH
from analyst.fundmanager.books import BookError, BookJournal, BookOrder, FundBook
from analyst.fundmanager.bundle import Holding, InvalidationStatus, ManagerBook
from analyst.fundmanager.contract import (
    P_TOLERANCE,
    QUOTE_TOLERANCE,
    SCENARIO_SUM_TOLERANCE,
    STOP_ATR_MAX,
    STOP_ATR_MIN,
    STOP_MAX_PCT,
    ContractVerdict,
)
from analyst.fundmanager.controls import CONTROL_BUY_BUDGET_FRACTION, CONTROL_REBALANCE_BAND
from analyst.fundmanager.mandate import (
    BenchMandate,
    ControlMandate,
    ManagerMandate,
    Roster,
    canonical_json,
    mandate_hash,
)
from analyst.fundmanager.render import SYSTEM_PROMPT, PromptTemplate
from analyst.fundmanager.schemas import Action, schema_bytes
from analyst.fundmanager.scoreboard import S0_EVENT
from analyst.fundmanager.stops import StopBook
from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import Actor, Decision, JournalEntry
from analyst.llm import (
    DEFAULT_MAX_TOKENS,
    LLM,
    LLMError,
    LLMRateLimitError,
    LLMResponse,
    Message,
    ToolSpec,
    Usage,
)
from dataplatform.clock import IST, Clock
from dataplatform.logging import get_logger
from execution.broker import Side

__all__ = [
    "COMMONS_UNAVAILABLE_EVENT",
    "DATA_GAPS_EVENT",
    "DATA_WAIT_EVENT",
    "DEADLINE_IST",
    "MANAGER_CRASHED_EVENT",
    "MISSED_SESSION_EVENT",
    "ORDERS_LAPSED_EVENT",
    "SKIPPED_DATA_RED_EVENT",
    "STREAM_DRY",
    "STREAM_LIVE",
    "DeadlineExceededError",
    "DeadlineLLM",
    "HoldingMemo",
    "MandateFingerprint",
    "StopDeclaration",
    "StopTightening",
    "StreamJournal",
    "VerdictOrders",
    "book_entry",
    "manager_book",
    "manager_deadline",
    "mandate_fingerprints",
    "orders_from_verdicts",
    "s0_entry",
]

_LOG = get_logger(__name__)

#: The live stream: every entry under its book's own id, what `/status/managers` reads.
STREAM_LIVE: Final = "m17"
#: The dry-run stream (M17.8's rehearsal): every entry under ``m17-dry:<book id>``, so nothing a
#: dry run journals can reach the live streams, the status page or the live scoreboard.
STREAM_DRY: Final = "m17-dry"

#: ``payload.event`` values this module and `backtest.fm_job` write.
SKIPPED_DATA_RED_EVENT: Final = "SKIPPED_DATA_RED"
MISSED_SESSION_EVENT: Final = "MISSED_SESSION"
MANAGER_CRASHED_EVENT: Final = "MANAGER_CRASHED"
COMMONS_UNAVAILABLE_EVENT: Final = "COMMONS_UNAVAILABLE"
DATA_WAIT_EVENT: Final = "M17_DATA_WAIT"
DATA_GAPS_EVENT: Final = "M17_DATA_GAPS"
ORDERS_LAPSED_EVENT: Final = "ORDERS_LAPSED"

#: Pre-registration §4: a manager unfinished by 08:30 IST on the next session misses the session.
DEADLINE_IST: Final = time(8, 30)

_HUNDRED: Final = Decimal(100)
_ZERO: Final = Decimal(0)
_PCT: Final = Decimal("0.01")


# ── the journal streams ──────────────────────────────────────────────────────────────────────────


class StreamJournal:
    """A `BookJournal` that files every entry in one M17 stream, and remembers what it filed.

    The live stream (`STREAM_LIVE`) passes entries through untouched: a book's entries carry its
    own id as ``case_id``. The dry stream (`STREAM_DRY`) files them under ``m17-dry:<book id>``
    and tags ``payload.stream``, so the live streams never see a rehearsal; `unprefixed` reads one
    back as its book wrote it. ``written`` keeps every entry this object filed, as written by the
    book (unprefixed), for the job's own in-process scoreboard ledger.
    """

    __slots__ = ("_sink", "stream", "written")

    def __init__(self, sink: BookJournal, stream: str) -> None:
        if stream not in (STREAM_LIVE, STREAM_DRY):
            raise ValueError(f"unknown M17 stream {stream!r}")
        self._sink = sink
        self.stream = stream
        self.written: list[JournalEntry] = []

    @property
    def dry(self) -> bool:
        return self.stream == STREAM_DRY

    def case_id(self, book_id: str) -> str:
        """The ``case_id`` ``book_id``'s entries are filed under in this stream."""
        return f"{self.stream}:{book_id}" if self.dry else book_id

    def append(self, entry: JournalEntry, *, evidence: EvidenceBundle | None = None) -> object:
        self.written.append(entry)
        if not self.dry:
            return self._sink.append(entry, evidence=evidence)
        filed = entry.model_copy(
            update={
                "case_id": None if entry.case_id is None else self.case_id(entry.case_id),
                "payload": {**entry.payload, "stream": self.stream},
            }
        )
        return self._sink.append(filed, evidence=evidence)

    def unprefixed(self, entry: JournalEntry) -> JournalEntry:
        """``entry`` as its book wrote it (the inverse of `append`'s dry-run filing)."""
        if not self.dry or entry.case_id is None:
            return entry
        prefix = f"{self.stream}:"
        if not entry.case_id.startswith(prefix):
            return entry
        payload = {k: v for k, v in entry.payload.items() if k != "stream"}
        return entry.model_copy(
            update={"case_id": entry.case_id[len(prefix) :], "payload": payload}
        )


def book_entry(
    *,
    book_id: str | None,
    session: date,
    clock: Clock,
    decision: Decision,
    event: str,
    rationale: str,
    payload: Mapping[str, str] | None = None,
    actor: Actor = Actor.SYSTEM,
    isin: str | None = None,
    evidence: EvidenceBundle | None = None,
) -> tuple[JournalEntry, EvidenceBundle | None]:
    """One job-level journal line (and its evidence) in ``book_id``'s stream (None: the desk's)."""
    body = {"event": event, **(payload or {})}
    if book_id is not None:
        body = {**body, "book": book_id, "mode": "PAPER"}
    if decision is Decision.HEARTBEAT and evidence is None:
        evidence = EvidenceBundle(
            case_id=book_id,
            trading_date=session,
            actor=actor,
            items=(
                EvidenceItem(
                    kind=EvidenceKind.STATUS,
                    source="m17_job",
                    label=event,
                    as_of=session,
                    text=rationale,
                ),
            ),
        )
    entry = JournalEntry(
        ts=clock.now(),
        trading_date=session,
        case_id=book_id,
        actor=actor,
        decision=decision,
        isin=isin,
        rationale=rationale,
        evidence_snapshot_ref=None if evidence is None else evidence.ref().ref,
        payload=body,
    )
    return entry, evidence


# ── S0: the mandate hashes (pre-registration §7) ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MandateFingerprint:
    """One book's `mandate_hash` and the digests of what went into it."""

    book_id: str
    mandate_hash: str
    prompt_digest: str
    schema_digest: str
    rule_digest: str
    rails_digest: str


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _contract_bytes() -> bytes:
    """The decision contract's tolerances (M17.4's backlog: they are in neither the prompt nor
    the schemas, and a changed tolerance changes what a manager may decide)."""
    return json.dumps(
        {
            "p_tolerance": str(P_TOLERANCE),
            "quote_tolerance": str(QUOTE_TOLERANCE),
            "scenario_sum_tolerance": str(SCENARIO_SUM_TOLERANCE),
            "stop_atr_min": str(STOP_ATR_MIN),
            "stop_atr_max": str(STOP_ATR_MAX),
            "stop_max_pct": str(STOP_MAX_PCT),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _control_bytes() -> bytes:
    return json.dumps(
        {
            "buy_budget_fraction": str(CONTROL_BUY_BUDGET_FRACTION),
            "rebalance_band": str(CONTROL_REBALANCE_BAND),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def mandate_fingerprints(
    roster: Roster, *, template: PromptTemplate | None = None
) -> dict[str, MandateFingerprint]:
    """Every roster book's `mandate_hash`, as journaled at S0.

    A manager's hash covers its roster entry, the prompt template's bytes, both decision schemas
    with the system prompt (`schemas.schema_bytes`) and the contract's tolerances, the shortlist
    and screens rule hashes (the Commons it decides on), and the shared rails. A control's covers
    its entry, its rebalance constants, the shortlist rule and the rails; the bench's its entry and
    the rails. Any change is a new book id (§7).
    """
    template = template or PromptTemplate.load()
    rules = f"shortlist:{SHORTLIST_RULE_HASH}\nscreens:{SCREENS_RULE_HASH}".encode("ascii")
    rails = canonical_json(roster.rails)
    out: dict[str, MandateFingerprint] = {}
    for book in roster.books:
        if isinstance(book, ManagerMandate):
            prompt = template.raw_bytes
            schema = (
                schema_bytes(book.rounds, system_prompt=SYSTEM_PROMPT) + b"\n" + (_contract_bytes())
            )
            rule = rules
        elif isinstance(book, ControlMandate):
            prompt, schema = b"", _control_bytes()
            rule = f"shortlist:{SHORTLIST_RULE_HASH}".encode("ascii")
        else:
            assert isinstance(book, BenchMandate)
            prompt, schema, rule = b"", b"", b""
        out[book.id] = MandateFingerprint(
            book_id=book.id,
            mandate_hash=mandate_hash(book, prompt, schema, rule, roster.rails),
            prompt_digest=_digest(prompt),
            schema_digest=_digest(schema),
            rule_digest=_digest(rule),
            rails_digest=_digest(rails),
        )
    return out


def s0_entry(
    fingerprint: MandateFingerprint, *, session: date, clock: Clock
) -> tuple[JournalEntry, EvidenceBundle]:
    """The ``M17_S0`` line of one book: S0 is this session, and this is the policy that runs."""
    payload = {
        "mandate_hash": fingerprint.mandate_hash,
        "prompt_digest": fingerprint.prompt_digest,
        "schema_digest": fingerprint.schema_digest,
        "rule_digest": fingerprint.rule_digest,
        "rails_digest": fingerprint.rails_digest,
        "shortlist_rule_hash": SHORTLIST_RULE_HASH,
        "screens_rule_hash": SCREENS_RULE_HASH,
    }
    evidence = EvidenceBundle(
        case_id=fingerprint.book_id,
        trading_date=session,
        actor=Actor.SYSTEM,
        items=tuple(
            EvidenceItem(
                kind=EvidenceKind.POLICY,
                source="m17_s0",
                label=label,
                as_of=session,
                text=value,
            )
            for label, value in sorted(payload.items())
        ),
    )
    entry, _ = book_entry(
        book_id=fingerprint.book_id,
        session=session,
        clock=clock,
        decision=Decision.HEARTBEAT,
        event=S0_EVENT,
        rationale=(
            f"S0 is {session.isoformat()}: the scoring window opens at this session's open with "
            f"every book in cash; {fingerprint.book_id} runs mandate "
            f"{fingerprint.mandate_hash[:16]} (pre-registration §6, §7)"
        ),
        payload=payload,
        evidence=evidence,
    )
    return entry, evidence


# ── the deadline ─────────────────────────────────────────────────────────────────────────────────


def manager_deadline(next_session: date) -> datetime:
    """08:30 IST on ``next_session``: the instant a manager still deciding misses the session."""
    return datetime.combine(next_session, DEADLINE_IST, tzinfo=IST)


class DeadlineExceededError(LLMError):
    """A manager's call would start, or finished, past the session's deadline."""

    def __init__(self, message: str, *, late: Usage | None = None) -> None:
        super().__init__(message)
        self.late = late


@dataclass(slots=True)
class DeadlineLLM:
    """An `LLM` that refuses to start or to return past ``deadline``, and waits out rate limits.

    A call is refused before it starts once the clock is at the deadline. A rate-limited call
    (`LLMRateLimitError`) is retried after ``backoff`` (doubling, at most ``max_backoff``) for as
    long as the wait ends before the deadline; otherwise the limit error stands. A call that
    *returns* after the deadline is refused too (`DeadlineExceededError` with its usage), so a
    decision made late is never accepted, and so never staged or journaled as ``FM_DECISION``.
    ``sleep`` is injected, like the clock, so a test waits in no real time.
    """

    inner: LLM
    deadline: datetime
    clock: Clock
    sleep: Callable[[float], None]
    backoff: timedelta = timedelta(minutes=2)
    max_backoff: timedelta = timedelta(minutes=16)
    retries: int = 0
    waited: timedelta = field(default_factory=timedelta)
    late: list[Usage] = field(default_factory=list)
    #: A rate limit whose next wait would have ended past the deadline: the session cannot
    #: finish in time, which is a missed session, not a manager error.
    gave_up: bool = False

    @property
    def expired(self) -> bool:
        return self.clock.now() >= self.deadline

    @property
    def missed(self) -> bool:
        """Whether the deadline, not the manager, ended the session."""
        return self.expired or bool(self.late) or self.gave_up

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        system: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResponse:
        wait = self.backoff
        while True:
            if self.expired:
                raise DeadlineExceededError(
                    f"the {self.deadline.isoformat()} deadline has passed; no call is started"
                )
            try:
                response = self.inner.complete(
                    messages, model=model, tools=tools, system=system, max_tokens=max_tokens
                )
            except LLMRateLimitError as exc:
                if self.clock.now() + wait >= self.deadline:
                    self.gave_up = True
                    raise
                self.retries += 1
                self.waited += wait
                _LOG.warning(
                    "fm_job.rate_limited",
                    model=model,
                    wait_seconds=int(wait.total_seconds()),
                    retry=self.retries,
                    detail=str(exc)[:200],
                )
                self.sleep(wait.total_seconds())
                wait = min(wait * 2, self.max_backoff)
                continue
            if self.clock.now() > self.deadline:
                self.late.append(response.usage)
                raise DeadlineExceededError(
                    f"the call returned after the {self.deadline.isoformat()} deadline; its "
                    "answer is not used",
                    late=response.usage,
                )
            return response


# ── the manager's book view ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HoldingMemo:
    """What a position was opened on: the BUY decision's thesis and invalidations (its journal
    record), persisted with the book. No price, so no cost basis can be read back out of it."""

    isin: str
    decided_on: date
    thesis: str
    invalidation: tuple[str, ...]
    horizon_sessions: int

    def to_document(self) -> dict[str, Any]:
        return {
            "isin": self.isin,
            "decided_on": self.decided_on.isoformat(),
            "thesis": self.thesis,
            "invalidation": list(self.invalidation),
            "horizon_sessions": str(self.horizon_sessions),
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> HoldingMemo:
        return cls(
            isin=document["isin"],
            decided_on=date.fromisoformat(document["decided_on"]),
            thesis=document["thesis"],
            invalidation=tuple(document["invalidation"]),
            horizon_sessions=int(document["horizon_sessions"]),
        )


#: Shown beside each invalidation condition: the harness does not judge them, the manager does.
INVALIDATION_UNJUDGED: Final = "not judged by the harness; judge it on today's evidence"
_NO_MEMO: Final = (
    "no opening thesis on record (the position did not come from this manager's BUY: a "
    "corporate action or a carried holding); judge it on today's evidence"
)


def manager_book(
    book: FundBook,
    session: date,
    *,
    memos: Mapping[str, HoldingMemo],
    stops: StopBook,
    sectors: Mapping[str, str | None],
    notes: Mapping[str, Sequence[str]] | None = None,
    forced: Mapping[str, str] | None = None,
) -> ManagerBook:
    """``book`` as its manager sees it at ``session``'s close: weights, theses, stops, reviews.

    Each holding is valued at `FundBook.valuation_close` (a delisted or suspended name at its last
    traded close), weighted against the book's value, and carries its memo's thesis and
    invalidation conditions, its current stop level, ``notes`` as evidence since entry and
    ``forced`` as its forced review; a suspended holding (M17.13) carries ``suspended_since``.
    Forced reviews are listed first (Amendment 1 c). Sessions held count from the memo's decision
    session.
    What it never does: put a cost basis, an entry price or a P&L anywhere in the view.
    Raises `BookError` (from the valuation) for a held name with no close that is neither
    delisted nor suspended.
    """
    notes = notes or {}
    forced = forced or {}
    held = {i: q for i, q in sorted(book.account.quantities().items()) if q > 0}
    values: dict[str, Decimal] = {}
    for isin, quantity in held.items():
        price = book.valuation_close(isin, session)
        if price is None:
            raise BookError(
                f"{book.book_id}: held {isin} has no close on {session.isoformat()} and is neither "
                "delisted nor suspended; the manager's book cannot be valued"
            )
        values[isin] = price * quantity
    suspended = {h.isin: h.last.session for h in book.suspended_holdings(session)}
    cash = book.account.cash_value
    nav = cash + sum(values.values(), _ZERO)
    holdings: list[Holding] = []
    standing = stops.stops
    for isin, value in values.items():
        memo = memos.get(isin)
        stop = standing.get(isin)
        holdings.append(
            Holding(
                isin=isin,
                sector=sectors.get(isin),
                weight_pct=(value / nav * _HUNDRED).quantize(_PCT, rounding=ROUND_HALF_EVEN),
                sessions_held=(
                    0 if memo is None else book.market.sessions_between(memo.decided_on, session)
                ),
                opening_thesis=_NO_MEMO if memo is None else memo.thesis,
                invalidation=()
                if memo is None
                else tuple(InvalidationStatus(c, INVALIDATION_UNJUDGED) for c in memo.invalidation),
                stop_price=None if stop is None else stop.level,
                evidence_since_entry=tuple(notes.get(isin, ())),
                forced_review=forced.get(isin),
                suspended_since=suspended.get(isin),
            )
        )
    holdings.sort(key=lambda h: (h.forced_review is None, h.isin))
    return ManagerBook(
        book_id=book.book_id,
        nav=nav,
        cash_pct=(cash / nav * _HUNDRED).quantize(_PCT, rounding=ROUND_HALF_EVEN),
        holdings=tuple(holdings),
    )


# ── accepted decisions → orders, stops and memos ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StopDeclaration:
    """A BUY's stop, declared on the session close (`StopBook.declare`) once the BUY is staged."""

    isin: str
    stop_pct: Decimal
    reference_price: Decimal


@dataclass(frozen=True, slots=True)
class StopTightening:
    """A HOLD's ``new_stop_pct``, already at or above the standing stop (the contract checked)."""

    isin: str
    level: Decimal


@dataclass(frozen=True, slots=True)
class VerdictOrders:
    """What one session's accepted decisions ask of the book."""

    orders: tuple[BookOrder, ...] = ()
    stops: tuple[StopDeclaration, ...] = ()
    tightenings: tuple[StopTightening, ...] = ()
    memos: tuple[HoldingMemo, ...] = ()


def _floor_shares(value: Decimal, price: Decimal) -> int:
    return int((value / price).to_integral_value(rounding=ROUND_FLOOR))


def orders_from_verdicts(
    accepted: Iterable[ContractVerdict],
    *,
    book: ManagerBook,
    held: Mapping[str, int],
    closes: Mapping[str, Decimal],
    session: date,
) -> VerdictOrders:
    """The book orders, stops and memos of ``accepted`` (contract-accepted) decisions.

    - ``BUY``: the whole shares the contract sized (``round_trip.quantity``: ``target_weight`` of
      the book's value at the close), with the BUY's stop and its memo.
    - ``SELL``: every share held. ``TRIM``: down to ``target_weight`` of the book at the close.
    - ``HOLD`` with ``new_stop_pct``: the tightened stop. ``PASS``/``WATCH``: nothing.

    The rationale carried into the order is the decision's own (with what changed, for a sale).
    What it never does: resize what the rails will later refuse — the book clears every order.
    """
    orders: list[BookOrder] = []
    stops: list[StopDeclaration] = []
    tightenings: list[StopTightening] = []
    memos: list[HoldingMemo] = []
    for verdict in accepted:
        d = verdict.decision
        if d.action is Action.BUY:
            trip = verdict.round_trip
            close = closes.get(d.isin)
            if trip is None or close is None or d.stop_pct is None:
                raise ValueError(f"an accepted BUY of {d.isin} has no size, close or stop")
            orders.append(BookOrder(d.isin, Side.BUY, trip.quantity, d.rationale))
            stops.append(StopDeclaration(d.isin, d.stop_pct, close))
            memos.append(
                HoldingMemo(
                    isin=d.isin,
                    decided_on=session,
                    thesis=d.thesis or d.rationale,
                    invalidation=tuple(d.invalidation),
                    horizon_sessions=d.horizon_sessions,
                )
            )
        elif d.action in (Action.SELL, Action.TRIM):
            quantity = held.get(d.isin, 0)
            if d.action is Action.TRIM:
                close = closes.get(d.isin)
                if close is None or d.target_weight is None:
                    raise ValueError(f"an accepted TRIM of {d.isin} has no close or target")
                keep = _floor_shares(book.nav * d.target_weight / _HUNDRED, close)
                quantity -= keep
            if quantity <= 0:
                continue
            changed = (
                ""
                if d.what_changed is None
                else f" [what changed: {d.what_changed.kind.value}: {d.what_changed.text}]"
            )
            orders.append(BookOrder(d.isin, Side.SELL, quantity, d.rationale + changed))
        elif d.action is Action.HOLD and d.new_stop_pct is not None:
            if verdict.stop_price is None:
                raise ValueError(f"an accepted stop tightening on {d.isin} has no stop price")
            tightenings.append(StopTightening(d.isin, verdict.stop_price))
    return VerdictOrders(tuple(orders), tuple(stops), tuple(tightenings), tuple(memos))
