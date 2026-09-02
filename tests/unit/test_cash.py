"""M5.10: the cash manager parks idle cash the same session and deploys it only when §5.6 permits.

Three claims, one per acceptance criterion, each shown rather than asserted in prose:

1. **Exit proceeds and SIP instalments are parked in the liquid ETF the same session.** `park`
   totals the queued cash, sizes the largest whole-share ETF buy that fits it (the India constraint
   applies to the ETF too), tags it CASH, dates it to the session, and journals it; the remainder
   too small for one share carries forward.
2. **Deployment happens only against a ratified-thesis core replacement or a tactical opportunity.**
   `deploy` demands a `BuyAuthorization`: a CORE one, which A4 mints only against a ratified thesis,
   or a TACTICAL one, which needs a journaled rationale. A CASH authorization, a mismatched
   instrument, a sell, or (upstream) an unratified thesis is refused; below the ratified minimum the
   cash waits.
3. **Parking and deployment produce journal entries with rationale.** Both write a `BUY` line whose
   rationale names what was done and why, and whose payload carries the machine-readable detail (the
   sources parked, the thesis or tactical rationale deployed against).

The deployment queue's own FIFO arithmetic is tested apart from the manager, the way A6 tests
`sleeves.py` apart from `engine.py`. The database is stood in for by a recording connection (the
device `test_rotation.py` and `test_rails.py` use), so the file is offline and fast (CLAUDE.md);
Postgres actually enforcing the append-only journal is the integration suite's job.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from psycopg.types.json import Json

from analyst.cases import (
    CapitalPlan,
    CashPolicy,
    ExitMenu,
    ExitStrategy,
    HorizonAndBenchmarks,
    MonitoringCadence,
    PolicySet,
    Ratification,
    RatificationKind,
    RiskRails,
    RotationDial,
    T2Cadence,
    TriggerSensitivity,
)
from analyst.cash import (
    BelowMinimumDeploymentError,
    CashError,
    CashManager,
    CashSource,
    DeploymentQueue,
    InsufficientQueuedCashError,
    QueueError,
    UndeployableError,
    UnratifiedCashPolicyError,
)
from analyst.journal import (
    Decision,
    EvidenceStore,
    Journal,
    Sleeve,
)
from analyst.journal.writer import _WRITE_COLUMNS
from analyst.rails import Lot, Portfolio, ProposedOrder, RailEngine
from analyst.thesis import (
    BreakCondition,
    BreakConditionType,
    CoreBuyError,
    EvaluationTier,
    Thesis,
    ThesisBook,
    ThesisKey,
    authorize_buy,
)
from dataplatform.clock import IST, FrozenClock
from dataplatform.store.db import Connection
from execution.broker import Exchange, OrderRequest, OrderType, Side

CASE_ID = "AI_ROBOTICS"
TRADING_DATE = datetime(2026, 8, 7, 19, 30, tzinfo=IST).date()
DECIDED_AT = datetime(2026, 8, 7, 19, 30, tzinfo=IST)
NOW = DECIDED_AT

PARKING_ISIN = "INF109AA1234"  # a liquid ETF (LIQUIDCASE), as §5.2's example cash policy names
TARGET_CORE = "INE009A01021"  # a core replacement to deploy into
TARGET_TACTICAL = "INE002A01009"  # a tactical opportunity
HELD_A = "INE001A01001"
HELD_B = "INE005A01003"


# ── the §5.2 example policy set ─────────────────────────────────────────────────────────────────


def example_policies(**overrides: Any) -> dict[str, Any]:
    """§5.2's AI/Robotics example, with rails generous enough that a normal cash order clears."""
    policies: dict[str, Any] = {
        "capital_plan": CapitalPlan(sip_amount_inr=Decimal("10000"), day_of_month=1),
        "horizon": HorizonAndBenchmarks(
            horizon_years=5, benchmark_primary="NIFTY-TRI", benchmark_secondary="NIFTY-IT"
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


def proposal(version: int = 1, **overrides: Any) -> PolicySet:
    return PolicySet(case_id=CASE_ID, version=version, **example_policies(**overrides))


def ratified(policy: PolicySet | None = None) -> PolicySet:
    """A ratified version of `policy` (a fresh v1 proposal by default), HUMAN-ratified."""
    policy = proposal() if policy is None else policy
    return policy.ratified_with(
        Ratification(
            by="vysh", at=NOW, kind=RatificationKind.HUMAN, content_hash=policy.content_hash
        )
    )


# ── the recording connection: INSERT ... RETURNING, offline ────────────────────────────────────


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


# ── builders ────────────────────────────────────────────────────────────────────────────────────


def order(
    isin: str, side: Side, quantity: int, price: str, *, sector: str = "EMS", tag: str | None = None
) -> ProposedOrder:
    return ProposedOrder(
        request=OrderRequest(
            isin=isin,
            side=side,
            quantity=quantity,
            exchange=Exchange.NSE,
            order_type=OrderType.MARKET,
            tag=tag,
        ),
        price=Decimal(price),
        sector=sector,
    )


def lot(isin: str, quantity: int, price: str, *, sector: str = "IT") -> Lot:
    return Lot(isin=isin, sector=sector, quantity=quantity, price=Decimal(price))


def book(*lots: Lot, cash: str = "500000") -> Portfolio:
    """A case book with two IT names by default, sized so a normal cash order clears the rails."""
    held = lots or (lot(HELD_A, 1000, "100"), lot(HELD_B, 1000, "400"))
    return Portfolio(case_id=CASE_ID, lots=held, cash=Decimal(cash))


def _break_conditions() -> tuple[BreakCondition, ...]:
    return (
        BreakCondition(
            id="BC1",
            type=BreakConditionType.FUNDAMENTAL,
            condition="segment revenue declines for two consecutive quarters",
            evaluation_tier=EvaluationTier.T1,
            evaluation="T1 on results filing",
        ),
        BreakCondition(
            id="BC2",
            type=BreakConditionType.INTEGRITY,
            condition="auditor resignation or promoter pledge above 50%",
            evaluation_tier=EvaluationTier.T0,
            evaluation="T0 -> immediate T1",
        ),
    )


def ratified_thesis_book(isin: str = TARGET_CORE) -> ThesisBook:
    """A thesis book with one HUMAN-ratified thesis for `isin` — a core replacement's licence."""
    book_ = ThesisBook()
    fresh = Thesis(
        case_id=CASE_ID,
        isin=isin,
        version=1,
        driver="EMS capex cycle x robotics component localization",
        theme_purity=Decimal("0.6"),
        expected_evidence=("order-book growth >20% YoY", "robotics revenue disclosure"),
        break_conditions=_break_conditions(),
    )
    proposed = book_.propose(fresh)
    book_.ratify(
        ThesisKey(CASE_ID, isin),
        Ratification(
            by="vysh", at=NOW, kind=RatificationKind.HUMAN, content_hash=proposed.content_hash
        ),
    )
    return book_


@pytest.fixture
def conn() -> _RecordingConnection:
    return _RecordingConnection()


@pytest.fixture
def journal(conn: _RecordingConnection, tmp_path: Path) -> Journal:
    return Journal(
        cast(Connection, conn),
        clock=FrozenClock(DECIDED_AT),
        evidence=EvidenceStore(tmp_path / "evidence"),
    )


@pytest.fixture
def manager(journal: Journal) -> CashManager:
    rails = RailEngine(journal, clock=FrozenClock(DECIDED_AT))
    return CashManager(ratified(), rails, journal, clock=FrozenClock(DECIDED_AT))


# ── construction: the manager runs only on a ratified policy set (§3 item 2) ─────────────────────


def test_manager_refuses_an_unratified_policy_set(journal: Journal) -> None:
    rails = RailEngine(journal, clock=FrozenClock(DECIDED_AT))
    with pytest.raises(UnratifiedCashPolicyError, match="RATIFIED"):
        CashManager(proposal(), rails, journal, clock=FrozenClock(DECIDED_AT))


# ── criterion 1: exit proceeds and SIP instalments parked in the ETF the same session ────────────


def test_park_exit_and_sip_buys_whole_etf_shares_same_session(
    manager: CashManager, conn: _RecordingConnection
) -> None:
    decision = manager.park(
        book(),
        sources={
            CashSource.EXIT_PROCEEDS: Decimal("30000"),
            CashSource.SIP_INSTALMENT: Decimal("10000"),
        },
        price=Decimal("1000"),
        trading_date=TRADING_DATE,
    )
    assert decision.parked
    assert decision.shares == 40  # floor(40000 / 1000)
    assert decision.residual_cash == Decimal("0")
    # The cash leg is a buy of the ratified parking ISIN, tagged CASH, dated to this session.
    assert decision.order is not None
    assert decision.order.isin == PARKING_ISIN
    assert decision.order.side is Side.BUY
    assert decision.order.quantity == 40
    assert decision.order.request.tag == Sleeve.CASH.value
    # ... and it is journaled as a CASH-sleeve BUY for the same trading date.
    assert len(conn.inserts) == 1
    params = conn.inserts[0]
    assert _insert_field(params, "decision") == Decision.BUY.value
    assert _insert_field(params, "sleeve") == Sleeve.CASH.value
    assert _insert_field(params, "isin") == PARKING_ISIN
    assert _insert_field(params, "trading_date") == TRADING_DATE


def test_park_honours_the_whole_share_constraint_and_carries_the_residual(
    manager: CashManager,
) -> None:
    # ₹40,000 at ₹1,001 buys 39 shares (₹39,039); ₹961 cannot buy a 40th and carries forward.
    decision = manager.park(
        book(),
        sources={CashSource.SIP_INSTALMENT: Decimal("40000")},
        price=Decimal("1001"),
        trading_date=TRADING_DATE,
    )
    assert decision.shares == 39
    assert decision.residual_cash == Decimal("961")


def test_park_below_one_etf_share_parks_nothing_and_journals_nothing(
    manager: CashManager, conn: _RecordingConnection
) -> None:
    # Idle cash below one ETF share: there is nothing to park this session, so nothing is journaled
    # (the daily loop's heartbeat records that the session was considered), and the cash carries on.
    decision = manager.park(
        book(),
        sources={CashSource.SIP_INSTALMENT: Decimal("500")},
        price=Decimal("1000"),
        trading_date=TRADING_DATE,
    )
    assert not decision.parked
    assert decision.order is None
    assert decision.shares == 0
    assert decision.residual_cash == Decimal("500")
    assert conn.inserts == []


def test_park_passes_through_a8_and_a_blocked_park_is_journaled_as_a_rail_block(
    journal: Journal, conn: _RecordingConnection
) -> None:
    # Rails tight enough that parking ₹40,000 breaches the per-order value cap (invariant #6: even
    # the cash leg passes A8). A8 writes the RAIL_BLOCK; the park is not itself journaled as a buy.
    tight = ratified(
        proposal(
            rails=example_policies()["rails"].model_copy(
                update={"max_order_value_inr": Decimal("1000")}
            )
        )
    )
    rails = RailEngine(journal, clock=FrozenClock(DECIDED_AT))
    manager = CashManager(tight, rails, journal, clock=FrozenClock(DECIDED_AT))

    decision = manager.park(
        book(),
        sources={CashSource.EXIT_PROCEEDS: Decimal("40000")},
        price=Decimal("1000"),
        trading_date=TRADING_DATE,
    )
    assert not decision.parked
    assert decision.assessment is not None and not decision.assessment.allowed
    assert decision.entry is None  # A7 wrote no BUY — the trade was blocked
    assert len(conn.inserts) == 1
    assert _insert_field(conn.inserts[0], "decision") == Decision.RAIL_BLOCK.value


def test_park_rejects_a_float_price(manager: CashManager) -> None:
    with pytest.raises(CashError, match="float"):
        manager.park(
            book(),
            sources={CashSource.SIP_INSTALMENT: Decimal("10000")},
            price=1000.0,  # type: ignore[arg-type]
            trading_date=TRADING_DATE,
        )


def test_park_needs_at_least_one_source(manager: CashManager) -> None:
    with pytest.raises(CashError, match="at least one cash source"):
        manager.park(book(), sources={}, price=Decimal("1000"), trading_date=TRADING_DATE)


# ── criterion 2: deployment only against a ratified-thesis replacement or tactical opportunity ────


def test_deploy_core_against_a_ratified_thesis_places_and_releases_from_the_queue(
    manager: CashManager, conn: _RecordingConnection
) -> None:
    auth = authorize_buy(
        case_id=CASE_ID, isin=TARGET_CORE, sleeve=Sleeve.CORE, book=ratified_thesis_book()
    )
    queue = DeploymentQueue().enqueue(CashSource.EXIT_PROCEEDS, Decimal("40000"), TRADING_DATE)

    decision = manager.deploy(
        order(TARGET_CORE, Side.BUY, 50, "200"),
        book(),
        authorization=auth,
        queue=queue,
        trading_date=TRADING_DATE,
        rationale="ratified-thesis core replacement after the BROKEN name was exited",
    )
    assert decision.placed
    assert decision.sleeve is Sleeve.CORE
    assert decision.deployed == Decimal("10000")  # 50 * 200
    assert decision.queue.total == Decimal("30000")  # released off the front of the queue
    # Journaled as a CORE BUY whose payload pins the ratified thesis it deployed against.
    params = conn.inserts[0]
    assert _insert_field(params, "decision") == Decision.BUY.value
    assert _insert_field(params, "sleeve") == Sleeve.CORE.value
    assert _insert_field(params, "isin") == TARGET_CORE
    payload = _insert_field(params, "payload")
    assert payload["thesis_version"] == "1"
    assert "thesis_content_hash" in payload
    assert decision.order.request.tag == Sleeve.CORE.value


def test_deploy_tactical_opportunity_places_with_a_journaled_rationale(
    manager: CashManager, conn: _RecordingConnection
) -> None:
    auth = authorize_buy(
        case_id=CASE_ID,
        isin=TARGET_TACTICAL,
        sleeve=Sleeve.TACTICAL,
        rationale="oversold cyclical, mean-reversion window",
    )
    queue = DeploymentQueue().enqueue(CashSource.SIP_INSTALMENT, Decimal("20000"), TRADING_DATE)

    decision = manager.deploy(
        order(TARGET_TACTICAL, Side.BUY, 40, "250", sector="METALS"),
        book(),
        authorization=auth,
        queue=queue,
        trading_date=TRADING_DATE,
        rationale="tactical sleeve opportunity",
    )
    assert decision.placed
    assert decision.sleeve is Sleeve.TACTICAL
    payload = _insert_field(conn.inserts[0], "payload")
    assert payload["tactical_rationale"] == "oversold cyclical, mean-reversion window"
    assert _insert_field(conn.inserts[0], "sleeve") == Sleeve.TACTICAL.value


def test_core_deployment_without_a_ratified_thesis_cannot_even_be_authorized() -> None:
    # The gate is A4's: a core buy with only a proposed (unratified) thesis yields no authorization,
    # so `deploy` — which demands one — can never be reached for it (§5.5 / §5.6).
    book_ = ThesisBook()
    book_.propose(
        Thesis(
            case_id=CASE_ID,
            isin=TARGET_CORE,
            version=1,
            driver="EMS capex cycle x robotics component localization",
            theme_purity=Decimal("0.6"),
            expected_evidence=("order-book growth >20% YoY",),
            break_conditions=_break_conditions(),
        )
    )
    with pytest.raises(CoreBuyError):
        authorize_buy(case_id=CASE_ID, isin=TARGET_CORE, sleeve=Sleeve.CORE, book=book_)


def test_deploy_refuses_a_cash_authorization(manager: CashManager) -> None:
    # A CASH authorization is the parking leg, not a deployment — use park() for that.
    auth = authorize_buy(case_id=CASE_ID, isin=PARKING_ISIN, sleeve=Sleeve.CASH)
    with pytest.raises(UndeployableError, match="CASH"):
        manager.deploy(
            order(PARKING_ISIN, Side.BUY, 10, "1000"),
            book(),
            authorization=auth,
            queue=DeploymentQueue().enqueue(
                CashSource.SIP_INSTALMENT, Decimal("20000"), TRADING_DATE
            ),
            trading_date=TRADING_DATE,
            rationale="should be refused",
        )


def test_deploy_refuses_an_authorization_for_a_different_instrument(manager: CashManager) -> None:
    auth = authorize_buy(
        case_id=CASE_ID, isin=TARGET_CORE, sleeve=Sleeve.CORE, book=ratified_thesis_book()
    )
    with pytest.raises(UndeployableError, match="authorization is for"):
        manager.deploy(
            order(TARGET_TACTICAL, Side.BUY, 40, "250"),  # a different ISIN than the auth
            book(),
            authorization=auth,
            queue=DeploymentQueue().enqueue(
                CashSource.SIP_INSTALMENT, Decimal("20000"), TRADING_DATE
            ),
            trading_date=TRADING_DATE,
            rationale="mismatched",
        )


def test_deploy_refuses_a_sell(manager: CashManager) -> None:
    auth = authorize_buy(
        case_id=CASE_ID, isin=TARGET_CORE, sleeve=Sleeve.CORE, book=ratified_thesis_book()
    )
    with pytest.raises(UndeployableError, match="buy"):
        manager.deploy(
            order(TARGET_CORE, Side.SELL, 50, "200"),
            book(lot(TARGET_CORE, 100, "200", sector="EMS")),
            authorization=auth,
            queue=DeploymentQueue().enqueue(
                CashSource.EXIT_PROCEEDS, Decimal("40000"), TRADING_DATE
            ),
            trading_date=TRADING_DATE,
            rationale="a sell is not a deployment",
        )


def test_deploy_below_the_ratified_minimum_tranche_makes_the_cash_wait(
    manager: CashManager,
) -> None:
    auth = authorize_buy(
        case_id=CASE_ID, isin=TARGET_CORE, sleeve=Sleeve.CORE, book=ratified_thesis_book()
    )
    with pytest.raises(BelowMinimumDeploymentError, match="minimum"):
        manager.deploy(
            order(TARGET_CORE, Side.BUY, 10, "200"),  # ₹2,000 < ₹5,000 min tranche
            book(),
            authorization=auth,
            queue=DeploymentQueue().enqueue(
                CashSource.EXIT_PROCEEDS, Decimal("40000"), TRADING_DATE
            ),
            trading_date=TRADING_DATE,
            rationale="sub-scale",
        )


def test_deploy_blocked_by_a_rail_leaves_the_queue_untouched(
    manager: CashManager, conn: _RecordingConnection
) -> None:
    auth = authorize_buy(
        case_id=CASE_ID, isin=TARGET_CORE, sleeve=Sleeve.CORE, book=ratified_thesis_book()
    )
    queue = DeploymentQueue().enqueue(CashSource.EXIT_PROCEEDS, Decimal("40000"), TRADING_DATE)
    # 300 * ₹200 = ₹60,000 breaches the ₹50,000 per-order value cap (invariant #6: A8 binds the
    # deployment leg like any other), so A8 blocks it and the queued cash stays put.
    decision = manager.deploy(
        order(TARGET_CORE, Side.BUY, 300, "200"),
        book(),
        authorization=auth,
        queue=queue,
        trading_date=TRADING_DATE,
        rationale="too large — should be blocked by A8",
    )
    assert not decision.placed
    assert decision.entry is None
    assert decision.deployed == Decimal("0")
    assert decision.queue.total == Decimal("40000")  # nothing released
    assert _insert_field(conn.inserts[0], "decision") == Decision.RAIL_BLOCK.value


# ── criterion 3: parking and deployment carry a rationale ────────────────────────────────────────


def test_park_journal_line_has_a_rationale(
    manager: CashManager, conn: _RecordingConnection
) -> None:
    manager.park(
        book(),
        sources={CashSource.EXIT_PROCEEDS: Decimal("40000")},
        price=Decimal("1000"),
        trading_date=TRADING_DATE,
    )
    rationale = _insert_field(conn.inserts[0], "rationale")
    assert rationale and "LIQUIDCASE" in rationale and "EXIT_PROCEEDS" in rationale


def test_deploy_journal_line_has_a_rationale(
    manager: CashManager, conn: _RecordingConnection
) -> None:
    auth = authorize_buy(
        case_id=CASE_ID, isin=TARGET_CORE, sleeve=Sleeve.CORE, book=ratified_thesis_book()
    )
    manager.deploy(
        order(TARGET_CORE, Side.BUY, 50, "200"),
        book(),
        authorization=auth,
        queue=DeploymentQueue().enqueue(CashSource.EXIT_PROCEEDS, Decimal("40000"), TRADING_DATE),
        trading_date=TRADING_DATE,
        rationale="ratified-thesis core replacement",
    )
    assert _insert_field(conn.inserts[0], "rationale") == "ratified-thesis core replacement"


# ── the deployment queue: FIFO arithmetic, tested apart from the manager ──────────────────────────


def test_queue_totals_and_per_source() -> None:
    queue = (
        DeploymentQueue()
        .enqueue(CashSource.EXIT_PROCEEDS, Decimal("30000"), TRADING_DATE)
        .enqueue(CashSource.SIP_INSTALMENT, Decimal("10000"), TRADING_DATE)
        .enqueue(CashSource.SIP_INSTALMENT, Decimal("10000"), TRADING_DATE)
    )
    assert queue.total == Decimal("50000")
    assert queue.amount_from(CashSource.SIP_INSTALMENT) == Decimal("20000")
    assert queue.amount_from(CashSource.EXIT_PROCEEDS) == Decimal("30000")


def test_queue_release_is_fifo_and_splits_the_straddling_tranche() -> None:
    first = date(2026, 8, 1)
    second = date(2026, 9, 1)
    queue = (
        DeploymentQueue()
        .enqueue(CashSource.EXIT_PROCEEDS, Decimal("30000"), first)
        .enqueue(CashSource.SIP_INSTALMENT, Decimal("10000"), second)
    )
    drained, released = queue.release(Decimal("35000"))
    assert released == Decimal("35000")
    assert drained.total == Decimal("5000")
    # The oldest tranche is consumed whole; the newer one is split, keeping its arrival date.
    remaining = drained.oldest
    assert remaining is not None
    assert remaining.source is CashSource.SIP_INSTALMENT
    assert remaining.amount == Decimal("5000")
    assert remaining.arrived == second


def test_queue_release_more_than_total_raises() -> None:
    queue = DeploymentQueue().enqueue(CashSource.SIP_INSTALMENT, Decimal("10000"), TRADING_DATE)
    with pytest.raises(InsufficientQueuedCashError, match="only 10000"):
        queue.release(Decimal("10001"))


def test_queue_rejects_a_float_amount() -> None:
    with pytest.raises(QueueError, match="float"):
        DeploymentQueue().enqueue(CashSource.SIP_INSTALMENT, 10000.0, TRADING_DATE)  # type: ignore[arg-type]


def test_queue_release_is_immutable() -> None:
    queue = DeploymentQueue().enqueue(CashSource.EXIT_PROCEEDS, Decimal("40000"), TRADING_DATE)
    drained, _ = queue.release(Decimal("10000"))
    assert queue.total == Decimal("40000")  # the original is untouched
    assert drained.total == Decimal("30000")


def test_empty_queue_has_no_oldest() -> None:
    assert DeploymentQueue().is_empty
    assert DeploymentQueue().oldest is None
