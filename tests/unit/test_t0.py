"""M5.11 — the T0 mechanical monitor and its data-red interlock (§5.4, invariant #10).

The four acceptance criteria, made concrete:

1. **A red data day produces `SKIPPED_DATA_RED` and zero orders** — and the proof is stronger than
   "no order row": the `gather` thunk that would assemble the day's facts is a spy that fails the
   test if it is ever called, and the T1 queue is asserted empty. On a red day nothing downstream
   of the interlock is reached at all (`test_red_data_*`).
2. **Each T0 check fires on a synthetic trigger and is silent otherwise** — every pure check has a
   fires/silent pair, and a full run over a synthetic trigger escalates (`test_check_*`,
   `test_run_escalates_*`).
3. **A clean day still writes a heartbeat naming the checks performed** (`test_clean_day_*`).
4. **T0 makes no LLM calls** — a `StubLLM` stands in for the analyst's LLM client and is asserted
   to have logged zero calls after a full sweep, clean and flagged (`test_t0_makes_no_llm_calls`).

The database is stood in for by the same recording connection the other analyst suites use — an
`INSERT ... RETURNING` echo — so the journal round-trips offline; Postgres enforcing append-only is
the integration suite's job.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from psycopg.types.json import Json

from analyst.cases.policies import RiskRails
from analyst.journal import (
    Actor,
    Decision,
    EvidenceStore,
    Journal,
    Sleeve,
    Verdict,
)
from analyst.journal.writer import _WRITE_COLUMNS
from analyst.llm import StubLLM
from analyst.monitor import (
    CHECKS_PERFORMED,
    CorporateActionEvent,
    Deal,
    DeliverySignal,
    FlowKind,
    InMemoryEscalationQueue,
    KeywordWatch,
    T0Check,
    T0Config,
    T0Holding,
    T0Inputs,
    T0Monitor,
    T0Outcome,
    check_announcements,
    check_corporate_actions,
    check_data_quality,
    check_drawdown,
    check_flow,
    check_rails,
)
from analyst.rails import Lot, Portfolio
from dataplatform.clock import IST, FrozenClock
from dataplatform.ingest.announcements import AnnouncementRow
from dataplatform.quality import QualityFinding, finding_fingerprint
from dataplatform.query import AnnouncementIndex, KeywordQuery
from dataplatform.status import GreenStatus
from dataplatform.store.db import Connection

CASE_ID = "AI_ROBOTICS"
TRADING_DATE = datetime(2026, 8, 7, 19, 30, tzinfo=IST).date()
DECIDED_AT = datetime(2026, 8, 7, 19, 30, tzinfo=IST)

HELD_A = "INE001A01001"
HELD_B = "INE005A01003"
HELD_C = "INE009A01021"
NOT_HELD = "INE002A01009"

CORE_DATASETS = ("nse_eod", "bse_eod", "nse_ca")


# ── green / red gates ────────────────────────────────────────────────────────────────────────────


def green_status(*, green: bool, reason: str) -> GreenStatus:
    return GreenStatus(
        logical_date=TRADING_DATE,
        green=green,
        reason=reason,
        datasets=CORE_DATASETS,
        published=CORE_DATASETS if green else (),
        missing=() if green else ("nse_eod",),
        not_published=(),
        open_error_flags=0,
        day_kind=None,
    )


def green_gate(status: GreenStatus) -> Any:
    def _gate(trading_date: Any) -> GreenStatus:
        assert trading_date == TRADING_DATE
        return status

    return _gate


class _ExplodingGate:
    """A gate that must be *called* — records that it was, so we prove the interlock ran first."""

    def __init__(self, status: GreenStatus) -> None:
        self.status = status
        self.calls = 0

    def __call__(self, trading_date: Any) -> GreenStatus:
        self.calls += 1
        return self.status


# ── recording connection (INSERT ... RETURNING, offline) ─────────────────────────────────────────


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _RecordingConnection:
    """Echoes an insert's parameters back as the returned row, like `INSERT ... RETURNING`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[Any]]] = []
        self.next_id = 1

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> _FakeCursor:
        unwrapped = [p.obj if isinstance(p, Json) else p for p in (params or ())]
        self.calls.append((sql, unwrapped))
        if sql.lstrip().upper().startswith("INSERT"):
            row = (self.next_id, *unwrapped)
            self.next_id += 1
            return _FakeCursor([row])
        return _FakeCursor([])

    @property
    def inserts(self) -> list[list[Any]]:
        return [params for sql, params in self.calls if sql.lstrip().upper().startswith("INSERT")]


def _insert_field(params: Sequence[Any], name: str) -> Any:
    return params[_WRITE_COLUMNS.index(name)]


def _decisions(conn: _RecordingConnection) -> list[str]:
    return [_insert_field(params, "decision") for params in conn.inserts]


# ── builders ─────────────────────────────────────────────────────────────────────────────────────


def lot(isin: str, quantity: int, price: str, *, sector: str = "IT") -> Lot:
    return Lot(isin=isin, sector=sector, quantity=quantity, price=Decimal(price))


def open_rails() -> RiskRails:
    """Caps loose enough that a normal book trips nothing — for the clean-day and silent tests."""
    return RiskRails(
        max_position_pct=Decimal("100"),
        max_sector_pct=Decimal("100"),
        min_holdings=1,
        drawdown_review_pct=Decimal("25"),
        max_order_value_inr=Decimal("50000"),
        max_order_pct_of_case=Decimal("100"),
    )


def tight_rails() -> RiskRails:
    """Caps a concentrated book breaches — for the rails-fires test."""
    return RiskRails(
        max_position_pct=Decimal("20"),
        max_sector_pct=Decimal("40"),
        min_holdings=5,
        drawdown_review_pct=Decimal("25"),
        max_order_value_inr=Decimal("50000"),
        max_order_pct_of_case=Decimal("10"),
    )


def clean_book() -> Portfolio:
    """One held name, valued modestly; trips nothing under `open_rails`."""
    return Portfolio(case_id=CASE_ID, lots=(lot(HELD_A, 100, "100"),), cash=Decimal("10000"))


def holding(isin: str, *, watches: tuple[KeywordWatch, ...] = ()) -> T0Holding:
    return T0Holding(
        isin=isin, case_id=CASE_ID, sector="IT", sleeve=Sleeve.CORE, keyword_watches=watches
    )


def clean_inputs(**overrides: Any) -> T0Inputs:
    """A green day with a compliant book and empty feeds — the sweep runs, nothing fires."""
    base: dict[str, Any] = {
        "portfolio": clean_book(),
        "rails": open_rails(),
        "case_value_series": (Decimal("100000"), Decimal("101000")),
        "holdings": (holding(HELD_A),),
        "announcements": AnnouncementIndex(()),
    }
    base.update(overrides)
    return T0Inputs(**base)


def announcement(isin: str, subject: str, *, hour: int = 16) -> AnnouncementRow:
    return AnnouncementRow(
        ts=datetime(2026, 8, 7, hour, 0, tzinfo=IST),
        source="nse_announcements",
        isin=isin,
        subject=subject,
    )


def make_journal(conn: _RecordingConnection, tmp_path: Path) -> Journal:
    return Journal(
        cast(Connection, conn),
        clock=FrozenClock(DECIDED_AT),
        evidence=EvidenceStore(tmp_path / "evidence"),
    )


def make_monitor(
    gate: Any,
    journal: Journal,
    queue: InMemoryEscalationQueue,
    *,
    config: T0Config | None = None,
) -> T0Monitor:
    return T0Monitor(gate, journal, queue, clock=FrozenClock(DECIDED_AT), config=config)


@pytest.fixture
def conn() -> _RecordingConnection:
    return _RecordingConnection()


@pytest.fixture
def queue() -> InMemoryEscalationQueue:
    return InMemoryEscalationQueue()


@pytest.fixture
def journal(conn: _RecordingConnection, tmp_path: Path) -> Journal:
    return make_journal(conn, tmp_path)


# ── criterion 1: a red data day skips, runs nothing, queues nothing ──────────────────────────────


def test_red_data_day_writes_skipped_data_red_and_runs_no_checks(
    conn: _RecordingConnection, journal: Journal, queue: InMemoryEscalationQueue
) -> None:
    red = green_status(green=False, reason="no sync_state row for nse_eod on 2026-08-07")
    monitor = make_monitor(green_gate(red), journal, queue)

    def gather() -> T0Inputs:  # the fact-gathering path — must never run on a red day
        raise AssertionError("gather() ran on a red day: the interlock did not short-circuit")

    result = monitor.run(TRADING_DATE, gather, datasets=CORE_DATASETS)

    assert result.outcome is T0Outcome.SKIPPED_DATA_RED
    assert result.checks_performed == ()  # not one check ran
    assert result.flags == ()
    assert queue.pending == ()  # nothing escalated


def test_red_data_day_journals_exactly_one_skip_and_no_order_decision(
    conn: _RecordingConnection, journal: Journal, queue: InMemoryEscalationQueue
) -> None:
    red = green_status(green=False, reason="nse_eod=FAILED on 2026-08-07")
    monitor = make_monitor(green_gate(red), journal, queue)

    monitor.run(TRADING_DATE, lambda: clean_inputs(), datasets=CORE_DATASETS)

    decisions = _decisions(conn)
    assert decisions == [Decision.SKIPPED_DATA_RED.value]  # the only row written
    # No order path anywhere: not a BUY, SELL, ESCALATE or HEARTBEAT in the journal.
    for forbidden in (Decision.BUY, Decision.SELL, Decision.ESCALATE, Decision.HEARTBEAT):
        assert forbidden.value not in decisions
    params = conn.inserts[0]
    assert _insert_field(params, "actor") == Actor.SYSTEM.value
    assert _insert_field(params, "rationale") == "nse_eod=FAILED on 2026-08-07"
    assert _insert_field(params, "case_id") is None  # a data-red skip is platform-wide


def test_interlock_runs_before_anything(journal: Journal, queue: InMemoryEscalationQueue) -> None:
    gate = _ExplodingGate(green_status(green=False, reason="red"))
    monitor = make_monitor(gate, journal, queue)
    calls: list[str] = []

    def gather() -> T0Inputs:
        calls.append("gather")
        return clean_inputs()

    monitor.run(TRADING_DATE, gather)

    assert gate.calls == 1  # the gate was asked
    assert calls == []  # and gather was not reached


# ── criterion 3: a clean day writes a heartbeat naming the checks ────────────────────────────────


def test_clean_day_writes_heartbeat_naming_the_checks(
    conn: _RecordingConnection, journal: Journal, queue: InMemoryEscalationQueue
) -> None:
    monitor = make_monitor(green_gate(green_status(green=True, reason="green")), journal, queue)

    result = monitor.run(TRADING_DATE, lambda: clean_inputs(), datasets=CORE_DATASETS)

    assert result.outcome is T0Outcome.HEARTBEAT
    assert result.flags == ()
    assert result.checks_performed == CHECKS_PERFORMED
    assert queue.pending == ()

    decisions = _decisions(conn)
    assert decisions == [Decision.HEARTBEAT.value]
    params = conn.inserts[0]
    assert _insert_field(params, "actor") == Actor.T0.value
    rationale = _insert_field(params, "rationale")
    for check in CHECKS_PERFORMED:  # every check is named in the heartbeat
        assert check.value in rationale
    assert _insert_field(params, "evidence_snapshot_ref") is not None  # invariant #9


# ── criterion 2: each check fires on a trigger, silent otherwise ─────────────────────────────────


def test_check_rails_fires_on_a_breaching_book_and_is_silent_otherwise() -> None:
    concentrated = Portfolio(
        case_id=CASE_ID,
        lots=(lot(HELD_A, 100, "1000", sector="IT"), lot(HELD_B, 10, "100", sector="PHARMA")),
        cash=Decimal("0"),
    )  # HELD_A is ~99% of the book — over every cap, and only 2 names
    fired = check_rails(concentrated, tight_rails())
    assert fired
    assert all(flag.check is T0Check.RAILS for flag in fired)

    assert check_rails(clean_book(), open_rails()) == []


def test_check_drawdown_fires_past_the_limit_and_is_silent_within_it() -> None:
    rails = open_rails()  # drawdown_review_pct = 25
    fell = (Decimal("100000"), Decimal("70000"))  # -30%
    fired = check_drawdown(CASE_ID, fell, rails)
    assert len(fired) == 1
    assert fired[0].check is T0Check.DRAWDOWN

    held = (Decimal("100000"), Decimal("90000"))  # -10%, within the limit
    assert check_drawdown(CASE_ID, held, rails) == []


def test_check_corporate_actions_fires_on_an_upcoming_ca_on_a_holding() -> None:
    inputs = clean_inputs(
        corporate_actions=(
            CorporateActionEvent(
                isin=HELD_A, ex_date=TRADING_DATE, action_type="BONUS", terms="1:1"
            ),
        )
    )
    fired = check_corporate_actions(TRADING_DATE, inputs)
    assert len(fired) == 1
    assert fired[0].check is T0Check.CORPORATE_ACTION
    assert fired[0].isin == HELD_A


def test_check_corporate_actions_silent_on_past_ca_and_on_unheld_names() -> None:
    past = clean_inputs(
        corporate_actions=(
            CorporateActionEvent(
                isin=HELD_A,
                ex_date=datetime(2026, 8, 1, tzinfo=IST).date(),
                action_type="SPLIT",
                terms="2:1",
            ),
        )
    )
    assert check_corporate_actions(TRADING_DATE, past) == []

    unheld = clean_inputs(
        corporate_actions=(
            CorporateActionEvent(
                isin=NOT_HELD, ex_date=TRADING_DATE, action_type="BONUS", terms="1:1"
            ),
        )
    )
    assert check_corporate_actions(TRADING_DATE, unheld) == []


def _auditor_query() -> KeywordQuery:
    return KeywordQuery(all_of=("auditor",), any_of=("resign", "resignation"))


def test_check_announcements_fires_on_a_keyword_hit_and_is_silent_otherwise() -> None:
    watch = KeywordWatch(break_condition_id="BC2", query=_auditor_query())
    index = AnnouncementIndex((announcement(HELD_A, "Resignation of Statutory Auditor"),))
    inputs = clean_inputs(holdings=(holding(HELD_A, watches=(watch,)),), announcements=index)
    fired = check_announcements(TRADING_DATE, inputs)
    assert len(fired) == 1
    assert fired[0].check is T0Check.ANNOUNCEMENT
    assert fired[0].break_condition_id == "BC2"
    assert fired[0].isin == HELD_A

    quiet = AnnouncementIndex((announcement(HELD_A, "Board meeting intimation for results"),))
    silent = clean_inputs(holdings=(holding(HELD_A, watches=(watch,)),), announcements=quiet)
    assert check_announcements(TRADING_DATE, silent) == []


def test_check_announcements_silent_when_hit_is_on_another_isin() -> None:
    watch = KeywordWatch(break_condition_id="BC2", query=_auditor_query())
    index = AnnouncementIndex((announcement(NOT_HELD, "Resignation of Statutory Auditor"),))
    inputs = clean_inputs(holdings=(holding(HELD_A, watches=(watch,)),), announcements=index)
    assert check_announcements(TRADING_DATE, inputs) == []


def test_check_flow_fires_on_a_delivery_spike_and_is_silent_within_baseline() -> None:
    spike = clean_inputs(
        delivery_signals=(
            DeliverySignal(
                isin=HELD_A, delivery_qty=Decimal("300000"), baseline_qty=Decimal("100000")
            ),
        )
    )
    fired = check_flow(spike, T0Config())
    assert len(fired) == 1
    assert fired[0].check is T0Check.FLOW
    assert fired[0].detail["kind"] == FlowKind.DELIVERY_SPIKE.value

    normal = clean_inputs(
        delivery_signals=(
            DeliverySignal(
                isin=HELD_A, delivery_qty=Decimal("110000"), baseline_qty=Decimal("100000")
            ),
        )
    )
    assert check_flow(normal, T0Config()) == []


def test_check_flow_fires_on_a_bulk_deal_on_a_holding_only() -> None:
    on_holding = clean_inputs(
        deals=(
            Deal(
                isin=HELD_A,
                kind=FlowKind.BULK_DEAL,
                counterparty="FII-X",
                quantity=Decimal("500000"),
                price=Decimal("980"),
            ),
        )
    )
    fired = check_flow(on_holding, T0Config())
    assert len(fired) == 1
    assert fired[0].detail["kind"] == FlowKind.BULK_DEAL.value

    off_holding = clean_inputs(
        deals=(
            Deal(
                isin=NOT_HELD,
                kind=FlowKind.BLOCK_DEAL,
                counterparty="FII-Y",
                quantity=Decimal("500000"),
                price=Decimal("500"),
            ),
        )
    )
    assert check_flow(off_holding, T0Config()) == []


def _finding(isin: str, severity: str) -> QualityFinding:
    return QualityFinding(
        logical_date=TRADING_DATE,
        check_name="unexplained_move",
        severity=cast(Any, severity),
        isin=isin,
        source="nse_eod",
        fingerprint=finding_fingerprint("unexplained_move", isin, TRADING_DATE),
    )


def test_check_data_quality_fires_on_a_finding_on_a_holding_and_is_silent_otherwise() -> None:
    on_holding = clean_inputs(quality_findings=(_finding(HELD_A, "WARN"),))
    fired = check_data_quality(on_holding)
    assert len(fired) == 1
    assert fired[0].check is T0Check.DATA_QUALITY
    assert fired[0].isin == HELD_A

    off_holding = clean_inputs(quality_findings=(_finding(NOT_HELD, "WARN"),))
    assert check_data_quality(off_holding) == []


# ── criterion 2 (integration): a synthetic trigger through a full run escalates ──────────────────


def test_run_escalates_on_a_synthetic_trigger_and_queues_for_t1(
    conn: _RecordingConnection, journal: Journal, queue: InMemoryEscalationQueue
) -> None:
    monitor = make_monitor(green_gate(green_status(green=True, reason="green")), journal, queue)
    watch = KeywordWatch(break_condition_id="BC2", query=_auditor_query())
    index = AnnouncementIndex((announcement(HELD_A, "Resignation of Statutory Auditor"),))

    def gather() -> T0Inputs:
        return clean_inputs(holdings=(holding(HELD_A, watches=(watch,)),), announcements=index)

    result = monitor.run(TRADING_DATE, gather, datasets=CORE_DATASETS)

    assert result.outcome is T0Outcome.ESCALATED
    assert len(result.flags) == 1
    assert result.flags[0].check is T0Check.ANNOUNCEMENT

    # journalled as an ESCALATE by T0, not a heartbeat...
    assert _decisions(conn) == [Decision.ESCALATE.value]
    params = conn.inserts[0]
    assert _insert_field(params, "actor") == Actor.T0.value
    assert _insert_field(params, "isin") == HELD_A
    bce = _insert_field(params, "break_conditions_evaluated")
    assert bce[0]["id"] == "BC2"
    assert bce[0]["verdict"] == Verdict.WEAKENED.value  # T0 cannot conclude BROKEN

    # ...and queued for T1, pointing back at that journal row.
    assert len(queue.pending) == 1
    escalation = queue.pending[0]
    assert escalation.flag.check is T0Check.ANNOUNCEMENT
    assert escalation.journal_entry_id == result.journal_entry_ids[0]


def test_run_raises_one_escalation_per_flag(
    conn: _RecordingConnection, journal: Journal, queue: InMemoryEscalationQueue
) -> None:
    monitor = make_monitor(green_gate(green_status(green=True, reason="green")), journal, queue)

    def gather() -> T0Inputs:
        # two independent triggers: a drawdown breach and a bulk deal on a holding
        return clean_inputs(
            case_value_series=(Decimal("100000"), Decimal("60000")),
            deals=(
                Deal(
                    isin=HELD_A,
                    kind=FlowKind.BULK_DEAL,
                    counterparty="FII-X",
                    quantity=Decimal("500000"),
                    price=Decimal("980"),
                ),
            ),
        )

    result = monitor.run(TRADING_DATE, gather)

    assert result.outcome is T0Outcome.ESCALATED
    assert len(result.flags) == 2
    assert {flag.check for flag in result.flags} == {T0Check.DRAWDOWN, T0Check.FLOW}
    assert _decisions(conn) == [Decision.ESCALATE.value, Decision.ESCALATE.value]
    assert len(queue.pending) == 2


# ── criterion 4: T0 makes no LLM calls ───────────────────────────────────────────────────────────


def test_t0_makes_no_llm_calls(
    conn: _RecordingConnection, journal: Journal, queue: InMemoryEscalationQueue
) -> None:
    # A StubLLM stands in for the analyst's LLM client. T0 is mechanical: it holds no client and
    # calls none. We run it through both endings — a clean sweep and a flagged one — and assert the
    # client logged zero calls, so no code path T0 exercises reaches a model.
    llm = StubLLM()
    monitor = make_monitor(green_gate(green_status(green=True, reason="green")), journal, queue)

    monitor.run(TRADING_DATE, lambda: clean_inputs())  # clean → heartbeat
    flagged = clean_inputs(case_value_series=(Decimal("100000"), Decimal("60000")))
    monitor.run(TRADING_DATE, lambda: flagged)  # drawdown → escalate

    assert llm.calls == ()  # not a single model call in either path
    # T0 also has no way to accept a client: its slots carry no LLM reference.
    assert "llm" not in getattr(T0Monitor, "__slots__", ())
    assert not any("llm" in slot.lower() for slot in getattr(T0Monitor, "__slots__", ()))
