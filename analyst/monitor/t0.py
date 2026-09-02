"""A5 · T0 — the mechanical daily monitor, and the data-red interlock in front of it.

§5.4's first tier: every trading day, at ~₹0, T0 runs a fixed battery of *mechanical* checks over
a case's holdings — current book against the ratified rails, drawdown against the review limit,
corporate actions landing on holdings, announcement keyword hits against a thesis's T0 break
conditions, flow anomalies (delivery spikes, bulk/block deals), and data-quality flags — and does
exactly one of three things:

* **Nothing green to act on** → a `HEARTBEAT` naming the checks it performed (invariant #9: a day
  with nothing to do still records what it looked at).
* **A check fired** → an `ESCALATE` entry per flag and the same flag queued for T1, which reads
  the evidence and returns a verdict (T1 is M6; here escalation is *recorded and queued*, no more).
* **Data was not green** → a `SKIPPED_DATA_RED` entry and no evaluation at all (invariant #10).

The interlock runs **first, before anything else** (§4.4). `run()` asks the injected `GreenGate`
whether the trading date's core datasets are `PUBLISHED` and quality-green; only if they are does
it call `gather()` to assemble the day's facts and run the checks. On a red day nothing downstream
is even reached — no data is gathered, no check runs, no escalation is queued — which is what makes
"bad data never becomes decisions" a structural fact here rather than a hopeful ordering.

T0 is mechanical by definition. It holds no `LLM` and calls none: every check is a comparison a
human could re-run by hand, and the evidence pack's per-tier burn report (§5.7) shows ₹0 against
T0 because there is nothing here that could spend. It also holds no broker and no order path: T0
observes and escalates, it never trades.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

import structlog

from analyst.cases.policies import RiskRails
from analyst.journal import (
    Actor,
    BreakConditionEvaluation,
    Decision,
    EvidenceBundle,
    EvidenceItem,
    EvidenceKind,
    Journal,
    JournalEntry,
    Sleeve,
    Verdict,
)
from analyst.monitor.interlock import GreenGate, GreenLike
from analyst.rails import Portfolio, assess_drawdown
from dataplatform.clock import Clock, SystemClock
from dataplatform.quality import QualityFinding
from dataplatform.query import AnnouncementIndex, KeywordQuery

_LOG = structlog.get_logger(__name__)

_ZERO: Final = Decimal("0")


class T0Check(StrEnum):
    """The mechanical checks T0 runs, in the order it runs them (§5.4).

    A closed vocabulary because the heartbeat *names the checks it performed* and the evidence pack
    slices escalations by which check fired — a free-text check name would make "how often did the
    drawdown tripwire fire this quarter" unanswerable.
    """

    RAILS = "rails"
    """Current book against the ratified concentration caps — position, sector, holding count."""

    DRAWDOWN = "drawdown"
    """Peak-to-trough case-value fall against `drawdown_review_pct` (§5.2 policy 4)."""

    CORPORATE_ACTION = "corporate_action"
    """A CA effective on or after the trading date on a held ISIN — a splt/bonus/merger to watch."""

    ANNOUNCEMENT = "announcement"
    """An exchange announcement whose text satisfies a T0 break condition's keyword set (§5.3)."""

    FLOW = "flow"
    """Flow anomalies on a holding — a delivery spike, or a bulk/block deal."""

    DATA_QUALITY = "data_quality"
    """A D7 sentinel finding scoped to a held ISIN that did not itself make the day red."""


#: Every check T0 performs, in run order. The heartbeat records this list so "checked, nothing
#: happened" says *what* was checked — an empty absence and a full clean sweep are different facts.
CHECKS_PERFORMED: Final[tuple[T0Check, ...]] = (
    T0Check.RAILS,
    T0Check.DRAWDOWN,
    T0Check.CORPORATE_ACTION,
    T0Check.ANNOUNCEMENT,
    T0Check.FLOW,
    T0Check.DATA_QUALITY,
)

#: Which evidence kind an escalation's snapshot item takes, per check — so the evidence pack can
#: answer "which escalations were made on a filing" versus "on a price move" (§5.7).
_EVIDENCE_KIND: Final[Mapping[T0Check, EvidenceKind]] = {
    T0Check.RAILS: EvidenceKind.RAIL,
    T0Check.DRAWDOWN: EvidenceKind.PRICE,
    T0Check.CORPORATE_ACTION: EvidenceKind.CORPORATE_ACTION,
    T0Check.ANNOUNCEMENT: EvidenceKind.FILING,
    T0Check.FLOW: EvidenceKind.PRICE,
    T0Check.DATA_QUALITY: EvidenceKind.STATUS,
}


class T0Outcome(StrEnum):
    """What one T0 run concluded — the three legal endings of the daily mechanical sweep."""

    SKIPPED_DATA_RED = "SKIPPED_DATA_RED"
    """`/status/sync` was not green; no check ran and nothing was queued (invariant #10)."""

    HEARTBEAT = "HEARTBEAT"
    """The sweep ran and found nothing; the day is recorded with the checks it performed."""

    ESCALATED = "ESCALATED"
    """At least one check fired; each flag is journalled `ESCALATE` and queued for T1."""


class FlowKind(StrEnum):
    """The flow anomalies T0 notices on a holding (§5.4)."""

    DELIVERY_SPIKE = "delivery_spike"
    """Delivered quantity well above its own recent baseline — accumulation or distribution."""

    BULK_DEAL = "bulk_deal"
    """A bulk deal disclosed on the holding (>0.5% of shares, per SEBI)."""

    BLOCK_DEAL = "block_deal"
    """A block deal disclosed on the holding (the block window)."""


# ── inputs the checks read ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class KeywordWatch:
    """One T0 break condition, as its keyword set against the announcement feed (§5.3/§5.4).

    Ties a `KeywordQuery` back to the break-condition id it came from so an escalation can name the
    condition it fired on (`BreakConditionEvaluation.id`), which is how the journal records a
    verdict per condition rather than per holding.
    """

    break_condition_id: str
    query: KeywordQuery


@dataclass(frozen=True, slots=True)
class T0Holding:
    """A position T0 watches, with what it needs to watch it — the sweep's per-name unit.

    What it assumes: `isin` is the identity (invariant #2), `sleeve` is CORE or TACTICAL (a CASH
    park is not a thesis position T0 reviews), and `keyword_watches` are the T0-tier break
    conditions from the holding's ratified thesis — a T1-tier condition is judged by reading the
    filing, not by a keyword, so it does not belong here.
    """

    isin: str
    case_id: str
    sector: str
    sleeve: Sleeve
    keyword_watches: tuple[KeywordWatch, ...] = ()


@dataclass(frozen=True, slots=True)
class CorporateActionEvent:
    """A corporate action landing on a holding — the CA-events check's input (D3).

    `ex_date` is the point that matters to a monitor: a CA already in the past has been absorbed by
    the adjustment engine, while one effective on or after the trading date changes what the
    holding is about and wants a human eye.
    """

    isin: str
    ex_date: date
    action_type: str
    terms: str


@dataclass(frozen=True, slots=True)
class DeliverySignal:
    """A holding's delivered quantity for the session against its own recent baseline.

    Kept as the two raw numbers, not a precomputed ratio, so the spike threshold lives in one place
    (`T0Config.delivery_spike_multiple`) and the evidence records what was actually seen.
    """

    isin: str
    delivery_qty: Decimal
    baseline_qty: Decimal


@dataclass(frozen=True, slots=True)
class Deal:
    """A bulk or block deal disclosed on a holding — always notable when it is on a name we hold."""

    isin: str
    kind: FlowKind
    counterparty: str
    quantity: Decimal
    price: Decimal

    def __post_init__(self) -> None:
        if self.kind is FlowKind.DELIVERY_SPIKE:
            raise ValueError("a Deal is a bulk or block deal; a delivery spike is a DeliverySignal")


@dataclass(frozen=True, slots=True)
class T0Inputs:
    """Everything the T0 checks read for one case on one session, gathered once.

    Assembled by the caller (the daily loop, M5.13) *after* the interlock has passed — `run()` calls
    the `gather` thunk only on a green day, so building this is never on the path of a red one. An
    open container in spirit like `SentinelInput`: each check reads its own slice and ignores the
    rest.
    """

    portfolio: Portfolio
    rails: RiskRails
    case_value_series: tuple[Decimal, ...]
    holdings: tuple[T0Holding, ...]
    announcements: AnnouncementIndex
    corporate_actions: tuple[CorporateActionEvent, ...] = ()
    delivery_signals: tuple[DeliverySignal, ...] = ()
    deals: tuple[Deal, ...] = ()
    quality_findings: tuple[QualityFinding, ...] = ()


# ── outputs ────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class T0Flag:
    """One thing a check found — the mechanical trigger, before it becomes an escalation.

    `summary` is the one line the `ESCALATE` rationale carries; `detail` is strings only (a float
    read back out of jsonb is a bug, per the journal's payload rule). `break_condition_id` is set
    only for an announcement flag, where the escalation records a per-condition verdict.
    """

    check: T0Check
    isin: str | None
    case_id: str
    summary: str
    detail: Mapping[str, str] = field(default_factory=dict)
    break_condition_id: str | None = None


@dataclass(frozen=True, slots=True)
class T0Escalation:
    """A T0 flag as it is handed to T1 — the queued item and the journal's escalation, together.

    Carries the flag, the trading date, and the id of the `ESCALATE` journal entry that recorded
    it, so a T1 review (M6) can reconstruct exactly what T0 saw when it raised the hand.
    """

    trading_date: date
    flag: T0Flag
    journal_entry_id: int


@runtime_checkable
class EscalationQueue(Protocol):
    """Where T0 hands a flag for T1 to pick up (T1 itself is M6).

    Structural on purpose: the daily loop can back this with a table, a test with a list. T0 only
    ever `enqueue`s — it never reads the queue back, because acting on an escalation is T1's job.
    """

    def enqueue(self, escalation: T0Escalation) -> None:
        """Record one escalation for T1. Must not raise for a well-formed escalation."""


class InMemoryEscalationQueue:
    """A list-backed `EscalationQueue` — the default for the daily loop's in-process use and tests.

    Persistence is a later concern (T1 is M6); this keeps the queued escalations in append order so
    the loop that raised them can hand them straight on within the same session.
    """

    __slots__ = ("_items",)

    def __init__(self) -> None:
        self._items: list[T0Escalation] = []

    def __repr__(self) -> str:
        return f"{type(self).__name__}(pending={len(self._items)})"

    def enqueue(self, escalation: T0Escalation) -> None:
        self._items.append(escalation)

    @property
    def pending(self) -> tuple[T0Escalation, ...]:
        """The escalations queued so far, in the order T0 raised them."""
        return tuple(self._items)


@dataclass(frozen=True, slots=True)
class T0Result:
    """The outcome of one T0 run — what it concluded and what it recorded.

    `checks_performed` is populated on a green day (the sweep ran) and empty on a red one (the
    interlock short-circuited before any check), which is itself the evidence that the interlock
    ran first.
    """

    trading_date: date
    outcome: T0Outcome
    reason: str
    flags: tuple[T0Flag, ...]
    journal_entry_ids: tuple[int, ...]
    checks_performed: tuple[T0Check, ...]

    @property
    def escalated(self) -> bool:
        """Whether any flag was raised and queued for T1."""
        return self.outcome is T0Outcome.ESCALATED


# ── the monitor ──────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class T0Config:
    """The thresholds T0's mechanical checks compare against, all ratified elsewhere or fixed.

    Kept tiny and explicit: the drawdown limit and the concentration caps come from the case's
    `RiskRails` (already ratified), so the only knob here is the delivery-spike multiple, which is a
    monitoring sensitivity rather than a risk policy.
    """

    delivery_spike_multiple: Decimal = Decimal("2")

    def __post_init__(self) -> None:
        if not isinstance(self.delivery_spike_multiple, Decimal):
            raise TypeError("delivery_spike_multiple must be a Decimal, never a float")
        if self.delivery_spike_multiple <= _ZERO:
            raise ValueError(
                f"delivery_spike_multiple must be positive, got {self.delivery_spike_multiple}"
            )


class T0Monitor:
    """§5.4's T0 tier wired to the journal and the T1 queue, with the data-red interlock in front.

    What it does: on `run()`, ask the `GreenGate` first (invariant #10); on red, record
    `SKIPPED_DATA_RED` and stop; on green, gather the day's facts, run every mechanical check, and
    either escalate each flag (journal `ESCALATE` + queue) or write a `HEARTBEAT` naming the checks.
    What it assumes: the caller owns the transaction (the `Journal` never commits), `ts` comes from
    the injected `Clock` (B10), and `gather()` returns the facts for the case the run is about.
    What it never does: place an order (it has no broker), call a model (it has no `LLM` and spends
    ₹0), or run a single check before the interlock has said green — the whole point of the tier.
    """

    __slots__ = ("_clock", "_config", "_gate", "_journal", "_queue")

    def __init__(
        self,
        gate: GreenGate,
        journal: Journal,
        queue: EscalationQueue,
        *,
        clock: Clock | None = None,
        config: T0Config | None = None,
    ) -> None:
        self._gate = gate
        self._journal = journal
        self._queue = queue
        self._clock = SystemClock() if clock is None else clock
        self._config = T0Config() if config is None else config

    def __repr__(self) -> str:
        return f"{type(self).__name__}(queue={self._queue!r})"

    def run(
        self, trading_date: date, gather: Callable[[], T0Inputs], *, datasets: Sequence[str] = ()
    ) -> T0Result:
        """Run the T0 sweep for `trading_date`, interlock first.

        `gather` is a thunk, not a value, so it is *not evaluated* on a red day — the interlock
        runs before a single fact is read, which is invariant #10 as control flow rather than as a
        comment. `datasets` is recorded on the skip line for the human reading it later.
        """
        green = self._gate(trading_date)
        if not green:
            return self._skip_data_red(trading_date, green.reason, datasets)

        inputs = gather()
        flags = tuple(self._run_checks(trading_date, inputs))
        _LOG.info(
            "t0.swept",
            trading_date=trading_date.isoformat(),
            holdings=len(inputs.holdings),
            flags=len(flags),
        )
        if flags:
            return self._escalate(trading_date, green, inputs, flags)
        return self._heartbeat(trading_date, green, inputs)

    # ── the three endings ────────────────────────────────────────────────────────────────────────

    def _skip_data_red(self, trading_date: date, reason: str, datasets: Sequence[str]) -> T0Result:
        """Record the interlock skip and return — no checks ran, nothing was queued (#10)."""
        _LOG.warning("t0.data_red", trading_date=trading_date.isoformat(), reason=reason)
        entry = self._journal.append(
            JournalEntry(
                ts=self._clock.now(),
                trading_date=trading_date,
                case_id=None,
                actor=Actor.SYSTEM,
                decision=Decision.SKIPPED_DATA_RED,
                rationale=reason,
                payload={"datasets": ",".join(datasets)} if datasets else {},
            )
        )
        return T0Result(
            trading_date=trading_date,
            outcome=T0Outcome.SKIPPED_DATA_RED,
            reason=reason,
            flags=(),
            journal_entry_ids=(entry.id,),
            checks_performed=(),
        )

    def _heartbeat(self, trading_date: date, green: GreenLike, inputs: T0Inputs) -> T0Result:
        """A clean sweep: one `HEARTBEAT` naming every check performed (invariant #9)."""
        bundle = self._evidence(trading_date, green, inputs, flags=())
        performed = ", ".join(check.value for check in CHECKS_PERFORMED)
        entry = self._journal.heartbeat(
            bundle,
            case_id=inputs.portfolio.case_id,
            actor=Actor.T0,
            rationale=f"T0 checks performed, no flags: {performed}",
        )
        return T0Result(
            trading_date=trading_date,
            outcome=T0Outcome.HEARTBEAT,
            reason=f"clean: {performed}",
            flags=(),
            journal_entry_ids=(entry.id,),
            checks_performed=CHECKS_PERFORMED,
        )

    def _escalate(
        self,
        trading_date: date,
        green: GreenLike,
        inputs: T0Inputs,
        flags: tuple[T0Flag, ...],
    ) -> T0Result:
        """One `ESCALATE` entry per flag, each queued for T1 — the flag path (§5.4).

        The evidence bundle is snapshotted once and every escalation references it: a single sweep
        shows one bundle and raises N hands, and re-storing it per flag would be N verified no-ops
        (the store is content-addressed, so it would dedupe anyway — this just says so in code).
        """
        bundle = self._evidence(trading_date, green, inputs, flags=flags)
        ref = self._journal.snapshot(bundle)

        entry_ids: list[int] = []
        for flag in flags:
            evaluated: tuple[BreakConditionEvaluation, ...] = ()
            if flag.break_condition_id is not None:
                # T0 cannot conclude BROKEN — that needs T1 reading the filing. A keyword hit is
                # evidence moving against the thesis with the condition not yet confirmed met:
                # WEAKENED, escalated for T1 to judge.
                evaluated = (
                    BreakConditionEvaluation(
                        id=flag.break_condition_id,
                        verdict=Verdict.WEAKENED,
                        observed=flag.summary,
                    ),
                )
            entry = self._journal.append(
                JournalEntry(
                    ts=self._clock.now(),
                    trading_date=trading_date,
                    case_id=flag.case_id,
                    actor=Actor.T0,
                    decision=Decision.ESCALATE,
                    isin=flag.isin,
                    evidence_snapshot_ref=ref.ref,
                    break_conditions_evaluated=evaluated,
                    rationale=f"[{flag.check.value}] {flag.summary}",
                    payload=dict(flag.detail),
                )
            )
            entry_ids.append(entry.id)
            self._queue.enqueue(
                T0Escalation(trading_date=trading_date, flag=flag, journal_entry_id=entry.id)
            )
            _LOG.info(
                "t0.escalated",
                trading_date=trading_date.isoformat(),
                check=flag.check.value,
                isin=flag.isin,
                entry_id=entry.id,
            )
        return T0Result(
            trading_date=trading_date,
            outcome=T0Outcome.ESCALATED,
            reason=f"{len(flags)} flag(s) escalated to T1",
            flags=flags,
            journal_entry_ids=tuple(entry_ids),
            checks_performed=CHECKS_PERFORMED,
        )

    # ── the checks ───────────────────────────────────────────────────────────────────────────────

    def _run_checks(self, trading_date: date, inputs: T0Inputs) -> list[T0Flag]:
        """Every mechanical check, in `CHECKS_PERFORMED` order; the union of what they flagged."""
        flags: list[T0Flag] = []
        flags += check_rails(inputs.portfolio, inputs.rails)
        flags += check_drawdown(inputs.portfolio.case_id, inputs.case_value_series, inputs.rails)
        flags += check_corporate_actions(trading_date, inputs)
        flags += check_announcements(trading_date, inputs)
        flags += check_flow(inputs, self._config)
        flags += check_data_quality(inputs)
        return flags

    # ── evidence ─────────────────────────────────────────────────────────────────────────────────

    def _evidence(
        self,
        trading_date: date,
        green: GreenLike,
        inputs: T0Inputs,
        *,
        flags: tuple[T0Flag, ...],
    ) -> EvidenceBundle:
        """The bundle a heartbeat or escalation was decided on — the green check, the book, flags.

        Always carries at least the STATUS item (the interlock's own answer), so a bundle over a
        clean day with an empty book still satisfies `EvidenceBundle`'s min-one-item rule and still
        records that the sweep ran and what it saw.
        """
        items: list[EvidenceItem] = [
            EvidenceItem(
                kind=EvidenceKind.STATUS,
                source="status_api",
                label="green",
                as_of=trading_date,
                text=green.reason,
                detail={
                    "checks": ",".join(check.value for check in CHECKS_PERFORMED),
                    "holdings": str(len(inputs.holdings)),
                },
            ),
            EvidenceItem(
                kind=EvidenceKind.POSITION,
                source="book",
                label="case_value",
                as_of=trading_date,
                value=inputs.portfolio.total_value,
                detail={"holding_count": str(inputs.portfolio.holding_count)},
            ),
        ]
        for flag in flags:
            items.append(
                EvidenceItem(
                    kind=_EVIDENCE_KIND[flag.check],
                    source=f"t0:{flag.check.value}",
                    label=flag.check.value,
                    isin=flag.isin,
                    as_of=trading_date,
                    text=flag.summary,
                    detail=dict(flag.detail),
                )
            )
        return EvidenceBundle(
            case_id=inputs.portfolio.case_id,
            trading_date=trading_date,
            actor=Actor.T0,
            items=tuple(items),
        )


# ── the pure checks (each independently testable: fires on a trigger, silent otherwise) ───────────


def check_rails(portfolio: Portfolio, rails: RiskRails) -> list[T0Flag]:
    """Prices vs rails: the *current* book against the ratified concentration caps (§5.2 policy 4).

    Distinct from A8's `check_order`, which clears a *proposed* trade: this measures the book as it
    already stands, because a name can drift over its cap on price alone with no order in sight, and
    a T0 that only ever checked orders would never notice. Silent when nothing exceeds a cap.
    """
    flags: list[T0Flag] = []
    total = portfolio.total_value
    if total <= _ZERO:
        return flags

    for lot in portfolio.lots:
        position_pct = lot.value / total * 100
        if position_pct > rails.max_position_pct:
            flags.append(
                T0Flag(
                    check=T0Check.RAILS,
                    isin=lot.isin,
                    case_id=portfolio.case_id,
                    summary=(
                        f"{lot.isin} is {position_pct:.2f}% of case value, over the "
                        f"{rails.max_position_pct}% position cap"
                    ),
                    detail={
                        "position_pct": str(position_pct),
                        "cap_pct": str(rails.max_position_pct),
                    },
                )
            )

    seen_sectors: set[str] = set()
    for lot in portfolio.lots:
        if lot.sector in seen_sectors:
            continue
        seen_sectors.add(lot.sector)
        sector_pct = portfolio.sector_value(lot.sector) / total * 100
        if sector_pct > rails.max_sector_pct:
            flags.append(
                T0Flag(
                    check=T0Check.RAILS,
                    isin=None,
                    case_id=portfolio.case_id,
                    summary=(
                        f"sector {lot.sector!r} is {sector_pct:.2f}% of case value, over the "
                        f"{rails.max_sector_pct}% sector cap"
                    ),
                    detail={
                        "sector": lot.sector,
                        "sector_pct": str(sector_pct),
                        "cap_pct": str(rails.max_sector_pct),
                    },
                )
            )

    if portfolio.holding_count < rails.min_holdings:
        flags.append(
            T0Flag(
                check=T0Check.RAILS,
                isin=None,
                case_id=portfolio.case_id,
                summary=(
                    f"book holds {portfolio.holding_count} names, below the "
                    f"{rails.min_holdings}-name floor"
                ),
                detail={
                    "holding_count": str(portfolio.holding_count),
                    "min_holdings": str(rails.min_holdings),
                },
            )
        )
    return flags


def check_drawdown(
    case_id: str, case_value_series: Sequence[Decimal], rails: RiskRails
) -> list[T0Flag]:
    """Drawdown trigger: the worst peak-to-trough fall against `drawdown_review_pct` (§5.2).

    Reuses A8's `assess_drawdown` so "drawdown" means exactly one thing across the platform. Silent
    when the fall has not reached the ratified limit (including a series too short to fall).
    """
    status = assess_drawdown(tuple(case_value_series), rails)
    if not status.review_forced:
        return []
    return [
        T0Flag(
            check=T0Check.DRAWDOWN,
            isin=None,
            case_id=case_id,
            summary=(
                f"case fell {status.drawdown_pct:.2f}% peak-to-trough, at or over the "
                f"{status.limit_pct}% review limit"
            ),
            detail={
                "drawdown_pct": str(status.drawdown_pct),
                "limit_pct": str(status.limit_pct),
                "peak": str(status.peak),
                "trough": str(status.trough),
            },
        )
    ]


def check_corporate_actions(trading_date: date, inputs: T0Inputs) -> list[T0Flag]:
    """CA events on holdings: a corporate action effective on or after the trading date (§5.4).

    Scoped to held ISINs — a CA on a name we do not own is not this case's concern — and to CAs
    that have not already passed, since a past-dated one is the adjustment engine's job, not a
    thing for a human to review. Silent when no held name has an upcoming CA.
    """
    held = {holding.isin for holding in inputs.holdings}
    flags: list[T0Flag] = []
    for event in inputs.corporate_actions:
        if event.isin not in held or event.ex_date < trading_date:
            continue
        flags.append(
            T0Flag(
                check=T0Check.CORPORATE_ACTION,
                isin=event.isin,
                case_id=inputs.portfolio.case_id,
                summary=(
                    f"{event.action_type} on {event.isin} ex-{event.ex_date.isoformat()}: "
                    f"{event.terms}"
                ),
                detail={
                    "action_type": event.action_type,
                    "ex_date": event.ex_date.isoformat(),
                    "terms": event.terms,
                },
            )
        )
    return flags


def check_announcements(trading_date: date, inputs: T0Inputs) -> list[T0Flag]:
    """Announcement keyword hits against a holding's T0 break conditions (§5.3/§5.4).

    For each holding, each of its T0 keyword watches is matched against the day's announcements for
    that ISIN. A hit is escalated naming the break condition, so T1 reads the filing and returns a
    verdict. Silent when no watch fires — the keyword sets are `all_of`/`any_of` constrained at
    construction, so an empty match is a real absence, not a mis-specified query matching nothing.
    """
    flags: list[T0Flag] = []
    for holding in inputs.holdings:
        for watch in holding.keyword_watches:
            hits = inputs.announcements.search(
                isin=holding.isin, start=trading_date, end=trading_date, query=watch.query
            )
            if not hits:
                continue
            subjects = "; ".join(row.subject for row in hits)
            flags.append(
                T0Flag(
                    check=T0Check.ANNOUNCEMENT,
                    isin=holding.isin,
                    case_id=holding.case_id,
                    summary=(
                        f"break condition {watch.break_condition_id} keyword hit on "
                        f"{len(hits)} announcement(s): {subjects}"
                    ),
                    detail={
                        "break_condition_id": watch.break_condition_id,
                        "hits": str(len(hits)),
                        "subjects": subjects,
                    },
                    break_condition_id=watch.break_condition_id,
                )
            )
    return flags


def check_flow(inputs: T0Inputs, config: T0Config) -> list[T0Flag]:
    """Flow anomalies on holdings: delivery spikes and bulk/block deals (§5.4).

    A delivery spike is delivered quantity at or above `delivery_spike_multiple` times its own
    baseline; a bulk or block deal on a held name is always notable. Both are scoped to holdings —
    a deal on a name we do not own is market colour, not a position signal. Silent when neither.
    """
    held = {holding.isin for holding in inputs.holdings}
    flags: list[T0Flag] = []

    for signal in inputs.delivery_signals:
        if signal.isin not in held or signal.baseline_qty <= _ZERO:
            continue
        if signal.delivery_qty >= config.delivery_spike_multiple * signal.baseline_qty:
            ratio = signal.delivery_qty / signal.baseline_qty
            flags.append(
                T0Flag(
                    check=T0Check.FLOW,
                    isin=signal.isin,
                    case_id=inputs.portfolio.case_id,
                    summary=(
                        f"delivery on {signal.isin} was {ratio:.2f}x its baseline "
                        f"({signal.delivery_qty} vs {signal.baseline_qty})"
                    ),
                    detail={
                        "kind": FlowKind.DELIVERY_SPIKE.value,
                        "delivery_qty": str(signal.delivery_qty),
                        "baseline_qty": str(signal.baseline_qty),
                        "ratio": str(ratio),
                    },
                )
            )

    for deal in inputs.deals:
        if deal.isin not in held:
            continue
        flags.append(
            T0Flag(
                check=T0Check.FLOW,
                isin=deal.isin,
                case_id=inputs.portfolio.case_id,
                summary=(
                    f"{deal.kind.value} on {deal.isin}: {deal.counterparty} "
                    f"{deal.quantity} @ {deal.price}"
                ),
                detail={
                    "kind": deal.kind.value,
                    "counterparty": deal.counterparty,
                    "quantity": str(deal.quantity),
                    "price": str(deal.price),
                },
            )
        )
    return flags


def check_data_quality(inputs: T0Inputs) -> list[T0Flag]:
    """Data-quality flags on holdings: D7 sentinel findings scoped to a held ISIN (§5.4).

    An open ERROR flag on a *core dataset* has already made the day red and stopped this run at the
    interlock; what reaches here are findings scoped to a held name that did not halt trading — a
    WARN, or an ERROR on a non-core dataset — which still deserve a T1 look. Silent when none.
    """
    held = {holding.isin for holding in inputs.holdings}
    flags: list[T0Flag] = []
    for finding in inputs.quality_findings:
        if finding.isin is None or finding.isin not in held:
            continue
        flags.append(
            T0Flag(
                check=T0Check.DATA_QUALITY,
                isin=finding.isin,
                case_id=inputs.portfolio.case_id,
                summary=(
                    f"{finding.severity} data-quality flag {finding.check_name!r} on "
                    f"{finding.isin} for {finding.logical_date.isoformat()}"
                ),
                detail={
                    "check_name": finding.check_name,
                    "severity": finding.severity,
                    "logical_date": finding.logical_date.isoformat(),
                    "fingerprint": finding.fingerprint,
                },
            )
        )
    return flags


__all__ = [
    "CHECKS_PERFORMED",
    "CorporateActionEvent",
    "Deal",
    "DeliverySignal",
    "EscalationQueue",
    "FlowKind",
    "InMemoryEscalationQueue",
    "KeywordWatch",
    "T0Check",
    "T0Config",
    "T0Escalation",
    "T0Flag",
    "T0Holding",
    "T0Inputs",
    "T0Monitor",
    "T0Outcome",
    "T0Result",
    "check_announcements",
    "check_corporate_actions",
    "check_data_quality",
    "check_drawdown",
    "check_flow",
    "check_rails",
]
