"""M14.4 — a risk-off rebalance says so in its own journal row (observability only).

The momentum v2 regime filter parks the book in cash when the index sits below its moving average.
With a basket to sell, the parking SELLs already name the regime. With nothing held there is nothing
to sell, and the engine used to fall through to its bare ``HEARTBEAT`` — the paper book's first
session (2026-10-07, NIFTY 50 TRI 34,345.16 < 200-day MA 36,721.94) was journaled exactly that way,
so *why* the book was in cash lived only behind the evidence link. Now the policy journals a
``HOLD`` of the parking sleeve carrying the reading, and the paper session's row reason says
``rebalance_risk_off``.

Pinned here, offline over the real ``ReplayEngine`` -> ``RailGate`` -> ``SimBroker`` stack:

* a risk-off rebalance on an empty book journals a ``HOLD`` with the reading, never a ``HEARTBEAT``
  (fails if the change is reverted);
* the change is journal-only: a run with the ``HOLD`` stripped back out — the old behaviour —
  places the same orders and ends on the same broker state, byte for byte, and its journal differs
  only by ``HOLD`` -> ``HEARTBEAT`` on the parked session;
* the paper session names the risk-off on its row, and a book whose earlier row was recorded by
  the old code (reason ``rebalance``, a ``HEARTBEAT`` journal digest) decides its next session.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from analyst.journal.evidence import canonical_bytes, digest_of
from analyst.journal.models import Decision, JournalEntry, Sleeve
from backtest.paper_session import (
    InMemoryPaperSessionStore,
    PaperSessionResult,
    RecordingJournal,
    RunVerdict,
    run_paper_session,
)
from backtest.policies.momentum_v2 import (
    MomentumV2Parameters,
    MomentumV2Policy,
    MomentumV2Record,
    RegimeReading,
    regime_parked,
)
from backtest.replay import Policy, ReplayEngine, ReplayResult, SessionContext, SessionDecision
from dataplatform.clock import IST, FrozenClock
from dataplatform.query.pit import Dataset, PitContext
from execution.broker import Exchange
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import NoReferenceBarError, ReferenceBar, SimBroker
from tests.paper_session_support import OCT_FIRST, OCT_SECOND, FixtureWorld, fixture_spec
from tests.rails_support import mechanics_gate

ISINS = ("INE001A01036", "INE002A01018", "INE003A01016", "INE004A01014")
_PRICE = Decimal("100")
_CASH = Decimal("1000000")


def _weekdays(start: date, n: int) -> tuple[date, ...]:
    out: list[date] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return tuple(out)


SESSIONS = _weekdays(date(2024, 2, 5), 20)
#: Rebalances: open risk-off on an empty book (the case M14.4 is about — the paper book's first
#: session), buy the basket (risk-on), park it (risk-off, held), buy it back (risk-on).
REBALANCES = {SESSIONS[0]: False, SESSIONS[5]: True, SESSIONS[10]: False, SESSIONS[15]: True}
EMPTY_PARK = SESSIONS[0]
HELD_PARK = SESSIONS[10]


class _Market:
    """Every name opens at ``_PRICE`` on every session."""

    def __init__(self) -> None:
        self._calendar = (*SESSIONS, *_weekdays(SESSIONS[-1] + timedelta(days=1), 10))

    def next_session(self, after: date) -> date:
        for session in self._calendar:
            if session > after:
                return session
        raise NoReferenceBarError(f"no session after {after}")

    def reference_bar(self, isin: str, session: date) -> ReferenceBar:
        if isin not in ISINS:
            raise NoReferenceBarError(f"{isin} {session}")
        return ReferenceBar(
            isin=isin,
            session=session,
            exchange=Exchange.NSE,
            open=_PRICE,
            vwap=_PRICE,
            traded_value=Decimal("10000000000"),
        )


class _Data:
    """Fixed candidates; the regime is risk-on or risk-off per ``REBALANCES``."""

    def is_rebalance(self, session: date) -> bool:
        return session in REBALANCES

    def signal(self, as_of: date) -> Dataset[MomentumV2Record]:
        records = tuple(
            MomentumV2Record(
                isin=isin,
                momentum_0_12=Decimal(len(ISINS) - rank),
                momentum_12_1=Decimal(len(ISINS) - rank),
                price=_PRICE,
                volatility=Decimal("0.2"),
                knowable_date=as_of,
            )
            for rank, isin in enumerate(ISINS)
        )
        return Dataset.declaring(f"m@{as_of}", records, knowable_date=lambda r: r.knowable_date)

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        level = Decimal("110") if REBALANCES.get(as_of, True) else Decimal("90")
        reading = RegimeReading(
            index_level=level, moving_average=Decimal("100"), knowable_date=as_of
        )
        return Dataset.declaring(f"r@{as_of}", (reading,), knowable_date=lambda r: r.knowable_date)


class _WithoutRegimeHold:
    """The policy as it was before M14.4: the risk-off HOLD is dropped, nothing else is touched."""

    def __init__(self, policy: Policy) -> None:
        self._policy = policy

    def decide(self, ctx: SessionContext) -> SessionDecision:
        decision = self._policy.decide(ctx)
        kept = tuple(e for e in decision.entries if e.decision is not Decision.HOLD)
        return dataclasses.replace(decision, entries=kept)


_PARAMS = MomentumV2Parameters(top_n=3, regime_filter=True)


def _replay(*, legacy: bool) -> tuple[ReplayResult, SimBroker]:
    clock = FrozenClock(SESSIONS[0])
    sim = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state="MH"),
        market=_Market(),
        opening_cash=_CASH,
    )
    policy: Policy = MomentumV2Policy(_Data(), _PARAMS)
    if legacy:
        policy = _WithoutRegimeHold(policy)
    closes = {(isin, s): _PRICE for isin in ISINS for s in SESSIONS}
    result = ReplayEngine(
        policy=policy, broker=sim, clock=clock, sessions=SESSIONS, rails=mechanics_gate(closes)
    ).run()
    return result, sim


def _on(result: ReplayResult, day: date) -> list[JournalEntry]:
    return [entry for entry in result.journal if entry.trading_date == day]


# ── the journal row names the risk-off ───────────────────────────────────────────────────────────


def test_a_risk_off_rebalance_on_an_empty_book_journals_a_hold_never_a_bare_heartbeat() -> None:
    result, _ = _replay(legacy=False)

    (entry,) = _on(result, EMPTY_PARK)
    assert entry.decision is Decision.HOLD, "a risk-off rebalance fell through to a bare HEARTBEAT"
    assert entry.sleeve is Sleeve.CASH
    assert entry.isin is None
    assert entry.rationale == "regime risk-off; holding cash"
    assert entry.payload == {
        "regime": "risk_off",
        "index_level": "90",
        "moving_average": "100",
        "ma_days": "200",
        "risk_on": "false",
    }
    assert entry.evidence_snapshot_ref is not None  # still tied to what the policy saw
    # The parking session with a basket to sell is unchanged: SELLs naming the regime, no HOLD.
    sells = [e for e in _on(result, HELD_PARK) if e.decision is Decision.SELL]
    assert sells and all("regime risk-off" in str(e.rationale) for e in sells)
    assert Decision.HOLD not in {e.decision for e in _on(result, HELD_PARK)}
    # Ordinary non-rebalance days stay heartbeats.
    assert [e.decision for e in _on(result, SESSIONS[1])] == [Decision.HEARTBEAT]


def test_the_change_is_journal_only_orders_and_book_are_byte_identical() -> None:
    """With and without the HOLD: same orders placed, same broker state, same final book."""
    new, new_sim = _replay(legacy=False)
    old, old_sim = _replay(legacy=True)

    assert new.book_bytes() == old.book_bytes()
    assert canonical_bytes(new_sim.export_state().to_document()) == canonical_bytes(
        old_sim.export_state().to_document()
    )
    assert new_sim.ledger() == old_sim.ledger()
    trades = {Decision.BUY, Decision.SELL, Decision.RAIL_BLOCK}
    assert [e for e in new.journal if e.decision in trades] == [
        e for e in old.journal if e.decision in trades
    ]
    assert any(e.decision is Decision.BUY for e in new.journal)  # the scenario does trade

    # The journals differ in exactly one row: the empty park, HOLD now and HEARTBEAT before.
    assert len(new.journal) == len(old.journal)
    diffs = [(a, b) for a, b in zip(new.journal, old.journal, strict=True) if a != b]
    assert [(a.trading_date, a.decision, b.decision) for a, b in diffs] == [
        (EMPTY_PARK, Decision.HOLD, Decision.HEARTBEAT)
    ]


def test_regime_parked_reads_the_evidence_not_the_absence_of_orders() -> None:
    policy = MomentumV2Policy(_Data(), _PARAMS)
    broker = SimBroker(
        clock=FrozenClock(EMPTY_PARK),
        cost_model=CostModel(load_rate_card(), account_state="MH"),
        market=_Market(),
        opening_cash=_CASH,
    )

    def evidence(day: date) -> bool:
        ctx = SessionContext(
            session=day, pit=PitContext(as_of=day), broker=broker, clock=FrozenClock(day)
        )
        return regime_parked(policy.decide(ctx).evidence)

    assert evidence(EMPTY_PARK) is True
    assert evidence(SESSIONS[5]) is False  # risk-on rebalance
    assert evidence(SESSIONS[1]) is False  # not a rebalance: the regime was not read


# ── the paper session's row ──────────────────────────────────────────────────────────────────────


RUN_AT = datetime(2026, 10, 1, 20, 30, tzinfo=IST)


@dataclass(frozen=True, slots=True)
class _Green:
    reason: str = ""

    def __bool__(self) -> bool:
        return True


def _run(
    day: date, store: InMemoryPaperSessionStore, journal: RecordingJournal, world: FixtureWorld
) -> PaperSessionResult:
    return run_paper_session(
        trading_date=day,
        spec=fixture_spec(),
        world=world,
        store=store,
        journal=journal,
        gate=lambda _day: _Green(),
        clock=FrozenClock(RUN_AT),
    )


def test_a_risk_off_paper_rebalance_names_it_on_the_row_and_in_the_journal() -> None:
    world = FixtureWorld(risk_off={OCT_FIRST})
    store, journal = InMemoryPaperSessionStore(), RecordingJournal()

    first = _run(OCT_FIRST, store, journal, world)

    assert first.verdict is RunVerdict.DECIDED
    assert first.record is not None and first.record.rebalanced
    assert first.record.reason == "rebalance_risk_off"
    assert first.record.orders == ()
    (entry,) = first.entries
    assert entry.decision is Decision.HOLD
    assert entry.payload["risk_on"] == "false" and entry.payload["mode"] == "PAPER"
    assert entry.payload["index_level"] == "90" and entry.payload["moving_average"] == "100"

    risk_on = _run(OCT_FIRST, InMemoryPaperSessionStore(), RecordingJournal(), FixtureWorld())
    assert risk_on.record is not None and risk_on.record.reason == "rebalance"


def test_a_row_recorded_before_m14_4_still_restores_and_the_next_session_decides() -> None:
    """The live 2026-10-07 row was written by the old code: reason ``rebalance`` and the digest of
    a bare HEARTBEAT. Only ``book_digest`` is verified on restore and the rebalance rule reads
    ``rebalanced``, so the next session decides as it would have — no rebalance, a heartbeat."""
    world = FixtureWorld(risk_off={OCT_FIRST})
    fresh = InMemoryPaperSessionStore()
    first = _run(OCT_FIRST, fresh, RecordingJournal(), world)
    assert first.record is not None
    (hold,) = first.entries
    heartbeat = hold.model_copy(
        update={
            "decision": Decision.HEARTBEAT,
            "rationale": None,
            "sleeve": None,
            "payload": {"paper_book": hold.payload["paper_book"], "mode": "PAPER"},
        }
    )
    legacy = dataclasses.replace(
        first.record,
        reason="rebalance",
        journal_digest=digest_of(canonical_bytes([heartbeat.model_dump(mode="json")])),
    )
    assert legacy.journal_digest != first.record.journal_digest
    store = InMemoryPaperSessionStore()
    store.record(legacy, recorded_at=RUN_AT)

    second = _run(OCT_SECOND, store, RecordingJournal(), world)

    assert second.verdict is RunVerdict.DECIDED
    assert second.record is not None and not second.record.rebalanced
    assert second.record.reason == "decided"
    assert [e.decision for e in second.entries] == [Decision.HEARTBEAT]
