"""M5.9: the rotation engine honours the two sleeves and the dial is a governed boundary.

Four claims, one per acceptance criterion, each shown rather than asserted in prose:

1. **A core sell without a BROKEN verdict is impossible.** `core_exit` raises on INTACT/WEAKENED
   verdicts, and `core_tilt` refuses a sell or a new-name buy — core membership changes on a broken
   thesis and on nothing weaker (§5.5 / decision #4).
2. **Tactical trades run freely inside the sleeve but never breach rails.** A within-rail tactical
   trade is placed and journaled; one that would breach a rail is blocked and A8 writes the
   `RAIL_BLOCK`; and a tactical trade past the *dial target* is still placed, because the dial is a
   target and only A8 is a cap.
3. **Changing the dial requires a new ratified policy version.** The engine refuses an unratified
   policy set, `resize_dial` returns a proposal that must be ratified before a new engine runs on
   it, and the ratified dial cannot be mutated in place.
4. **Every emitted order carries a CORE or TACTICAL tag.** The tag comes from the verb, onto both
   the order's `request.tag` and the journal line's `sleeve`.

The database is stood in for by a recording connection (the device `test_rails.py` and
`test_journal.py` use), so the file is offline and fast (CLAUDE.md). Postgres actually enforcing the
append-only journal is the integration suite's job.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from psycopg.types.json import Json
from pydantic import ValidationError

from analyst.cases import (
    CapitalPlan,
    CashPolicy,
    ExitMenu,
    ExitStrategy,
    HorizonAndBenchmarks,
    MonitoringCadence,
    PolicySet,
    PolicyStatus,
    Ratification,
    RatificationKind,
    RiskRails,
    RotationDial,
    T2Cadence,
    TriggerSensitivity,
)
from analyst.journal import (
    Actor,
    BreakConditionEvaluation,
    Decision,
    EvidenceStore,
    Journal,
    Sleeve,
    Verdict,
)
from analyst.journal.writer import _WRITE_COLUMNS
from analyst.rails import Lot, Portfolio, ProposedOrder, RailEngine
from analyst.rotation import (
    CoreMembershipError,
    DialResizeError,
    RotationEngine,
    SleeveAllocation,
    UnratifiedDialError,
    allocate,
    resize_dial,
    sleeve_targets,
)
from dataplatform.clock import IST, FrozenClock
from dataplatform.store.db import Connection
from execution.broker import Exchange, OrderRequest, OrderType, Side

CASE_ID = "AI_ROBOTICS"
TRADING_DATE = datetime(2026, 8, 7, 19, 30, tzinfo=IST).date()
DECIDED_AT = datetime(2026, 8, 7, 19, 30, tzinfo=IST)
NOW = DECIDED_AT

IT_A = "INE001A01001"
IT_B = "INE002A01009"
PARKING_ISIN = "INF109AA1234"


# ── the §5.2 example policy set ─────────────────────────────────────────────────────────────────


def example_policies(**overrides: Any) -> dict[str, Any]:
    """§5.2's AI/Robotics example, with rails generous enough that a normal trade clears them."""
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
    isin: str, side: Side, quantity: int, price: str, *, sector: str = "IT", tag: str | None = None
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


def book(*lots: Lot, cash: str = "1000000") -> Portfolio:
    return Portfolio(case_id=CASE_ID, lots=lots, cash=Decimal(cash))


def lot(isin: str, quantity: int, price: str, *, sector: str = "IT") -> Lot:
    return Lot(isin=isin, sector=sector, quantity=quantity, price=Decimal(price))


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
def engine(journal: Journal) -> RotationEngine:
    rails = RailEngine(journal, clock=FrozenClock(DECIDED_AT))
    return RotationEngine(ratified(), rails, journal, clock=FrozenClock(DECIDED_AT))


# ── criterion 1: a core sell without a BROKEN verdict is impossible ──────────────────────────────


@pytest.mark.parametrize("verdict", [Verdict.INTACT, Verdict.WEAKENED])
def test_core_exit_without_a_broken_verdict_raises(
    engine: RotationEngine, conn: _RecordingConnection, verdict: Verdict
) -> None:
    held = book(lot(IT_A, 100, "100"))
    with pytest.raises(CoreMembershipError, match="BROKEN"):
        engine.core_exit(
            order(IT_A, Side.SELL, 50, "100"),
            held,
            evaluations=[BreakConditionEvaluation(id="BC1", verdict=verdict)],
            trading_date=TRADING_DATE,
            rationale="cycle turned",
        )
    # A refused exit trades nothing and journals nothing — the refusal is a raise, not a record.
    assert conn.inserts == []


def test_core_exit_with_no_break_conditions_at_all_raises(engine: RotationEngine) -> None:
    with pytest.raises(CoreMembershipError, match="none"):
        engine.core_exit(
            order(IT_A, Side.SELL, 50, "100"),
            book(lot(IT_A, 100, "100")),
            evaluations=[],
            trading_date=TRADING_DATE,
            rationale="no reason",
        )


def test_core_exit_on_a_broken_verdict_sells_and_journals_the_break(
    engine: RotationEngine, conn: _RecordingConnection
) -> None:
    held = book(lot(IT_A, 100, "100"))
    decision = engine.core_exit(
        order(IT_A, Side.SELL, 50, "100"),
        held,
        evaluations=[
            BreakConditionEvaluation(id="BC1", verdict=Verdict.BROKEN, observed="driver gone"),
            BreakConditionEvaluation(id="BC2", verdict=Verdict.INTACT),
        ],
        trading_date=TRADING_DATE,
        rationale="thesis broken; staged exit",
    )
    assert decision.placed
    assert decision.sleeve is Sleeve.CORE
    assert len(conn.inserts) == 1
    params = conn.inserts[0]
    assert _insert_field(params, "decision") == Decision.SELL.value
    assert _insert_field(params, "sleeve") == Sleeve.CORE.value
    assert _insert_field(params, "isin") == IT_A
    # The broken condition rides along on the entry so the record shows *which* condition broke.
    conditions = _insert_field(params, "break_conditions_evaluated")
    assert [c["verdict"] for c in conditions] == [Verdict.BROKEN.value, Verdict.INTACT.value]


def test_core_tilt_refuses_a_sell(engine: RotationEngine) -> None:
    with pytest.raises(CoreMembershipError, match="membership change"):
        engine.core_tilt(
            order(IT_A, Side.SELL, 50, "100"),
            book(lot(IT_A, 100, "100")),
            trading_date=TRADING_DATE,
            rationale="trim",
        )


def test_core_tilt_refuses_adding_a_name_the_book_does_not_hold(engine: RotationEngine) -> None:
    # Steering new money is a tilt toward an *existing* core name; a new name is a membership
    # addition, which is A4's ratified-thesis path, not a rotation decision.
    with pytest.raises(CoreMembershipError, match="ratified-thesis"):
        engine.core_tilt(
            order(IT_B, Side.BUY, 50, "100"),
            book(lot(IT_A, 100, "100")),
            trading_date=TRADING_DATE,
            rationale="new core name",
        )


def test_core_tilt_grows_an_existing_holding_with_new_money(
    engine: RotationEngine, conn: _RecordingConnection
) -> None:
    decision = engine.core_tilt(
        order(IT_A, Side.BUY, 50, "100"),
        book(lot(IT_A, 100, "100")),
        trading_date=TRADING_DATE,
        rationale="SIP tilt toward the cycle-favored core name",
    )
    assert decision.placed
    assert decision.sleeve is Sleeve.CORE
    assert _insert_field(conn.inserts[0], "decision") == Decision.BUY.value
    assert _insert_field(conn.inserts[0], "sleeve") == Sleeve.CORE.value


# ── criterion 2: tactical trades run freely inside the sleeve, but rails still bind ──────────────


def test_tactical_trade_within_rails_is_placed_and_journaled(
    engine: RotationEngine, conn: _RecordingConnection
) -> None:
    decision = engine.tactical_trade(
        order(IT_A, Side.BUY, 100, "100"),
        book(cash="1000000"),
        trading_date=TRADING_DATE,
        rationale="cycle expression",
    )
    assert decision.placed
    assert decision.assessment.allowed
    assert len(conn.inserts) == 1
    assert _insert_field(conn.inserts[0], "decision") == Decision.BUY.value
    assert _insert_field(conn.inserts[0], "sleeve") == Sleeve.TACTICAL.value


def test_a_tactical_trade_that_breaches_a_rail_is_blocked_not_placed(
    engine: RotationEngine, conn: _RecordingConnection
) -> None:
    # 100 @ 1000 = 100_000 > the 50_000 per-order sanity cap: rails bind on the tactical sleeve too.
    decision = engine.tactical_trade(
        order(IT_A, Side.BUY, 100, "1000"),
        book(cash="1000000"),
        trading_date=TRADING_DATE,
        rationale="oversized rotation",
    )
    assert not decision.placed
    assert not decision.assessment.allowed
    # The only journal line is A8's RAIL_BLOCK; there is no BUY, because nothing was placed.
    assert len(conn.inserts) == 1
    params = conn.inserts[0]
    assert _insert_field(params, "decision") == Decision.RAIL_BLOCK.value
    assert _insert_field(params, "actor") == Actor.RAILS.value
    assert _insert_field(params, "sleeve") == Sleeve.TACTICAL.value
    assert decision.entry is None


def test_a_tactical_trade_past_the_dial_target_is_still_placed(journal: Journal) -> None:
    # The dial is a target, not a cap. With rails wide open, a tactical buy that pushes the sleeve
    # well past its 30% target is placed anyway — only A8 caps a trade, and it did not fire.
    wide = ratified(
        proposal(
            rails=RiskRails(
                max_position_pct=Decimal("100"),
                max_sector_pct=Decimal("100"),
                min_holdings=1,
                drawdown_review_pct=Decimal("25"),
                max_order_value_inr=Decimal("1000000000"),
                max_order_pct_of_case=Decimal("100"),
            )
        )
    )
    rails = RailEngine(journal, clock=FrozenClock(DECIDED_AT))
    engine = RotationEngine(wide, rails, journal, clock=FrozenClock(DECIDED_AT))

    empty = book(cash="100000")
    target = engine.targets(empty.total_value)
    assert target.tactical_target == Decimal("30000")  # 30% of 100_000

    decision = engine.tactical_trade(
        order(IT_A, Side.BUY, 500, "100"),  # 50_000 — well past the 30_000 target
        empty,
        trading_date=TRADING_DATE,
        rationale="deliberately over the dial target",
    )
    assert decision.placed


# ── criterion 3: changing the dial requires a new ratified policy version ────────────────────────


def test_rotation_engine_refuses_an_unratified_policy_set(journal: Journal) -> None:
    rails = RailEngine(journal, clock=FrozenClock(DECIDED_AT))
    with pytest.raises(UnratifiedDialError, match="RATIFIED"):
        RotationEngine(proposal(), rails, journal, clock=FrozenClock(DECIDED_AT))


def test_resize_dial_returns_an_unratified_proposal_that_cannot_yet_run(journal: Journal) -> None:
    live = ratified()  # v1, dial 30
    revised = resize_dial(live, Decimal("40"))
    assert revised.version == 2
    assert revised.status is PolicyStatus.PROPOSAL
    assert revised.rotation_dial.tactical_pct == Decimal("40")

    rails = RailEngine(journal, clock=FrozenClock(DECIDED_AT))
    # The new dial is proposed, not ratified: no engine may run on it yet (§5.5).
    with pytest.raises(UnratifiedDialError):
        RotationEngine(revised, rails, journal, clock=FrozenClock(DECIDED_AT))

    # And the mix in force is unchanged — the ratified v1 still dials 30.
    still = RotationEngine(live, rails, journal, clock=FrozenClock(DECIDED_AT))
    assert still.dial.tactical_pct == Decimal("30")


def test_a_resized_dial_runs_only_after_it_is_ratified(journal: Journal) -> None:
    revised = resize_dial(ratified(), Decimal("40"))
    now_ratified = revised.ratified_with(
        Ratification(
            by="vysh",
            at=NOW,
            kind=RatificationKind.HUMAN,
            content_hash=revised.content_hash,
        )
    )
    rails = RailEngine(journal, clock=FrozenClock(DECIDED_AT))
    engine = RotationEngine(now_ratified, rails, journal, clock=FrozenClock(DECIDED_AT))
    assert engine.dial.tactical_pct == Decimal("40")
    assert engine.targets(Decimal("100000")).tactical_target == Decimal("40000")


def test_resize_dial_refuses_an_unratified_base() -> None:
    with pytest.raises(DialResizeError, match="in force"):
        resize_dial(proposal(), Decimal("40"))


def test_the_ratified_dial_cannot_be_mutated_in_place(engine: RotationEngine) -> None:
    # Frozen by construction: the boundary moves through governance, never by assignment.
    with pytest.raises(ValidationError):
        engine.dial.tactical_pct = Decimal("55")


# ── criterion 4: every emitted order carries a CORE or TACTICAL tag ─────────────────────────────


def test_tactical_orders_are_tagged_tactical_on_the_order_and_the_journal(
    engine: RotationEngine, conn: _RecordingConnection
) -> None:
    decision = engine.tactical_trade(
        order(IT_A, Side.BUY, 100, "100", tag="whatever-the-caller-passed"),
        book(cash="1000000"),
        trading_date=TRADING_DATE,
        rationale="tactical",
    )
    # The tag is authoritative and comes from the verb — the caller's tag is overwritten.
    assert decision.order.request.tag == Sleeve.TACTICAL.value
    assert _insert_field(conn.inserts[0], "sleeve") == Sleeve.TACTICAL.value


def test_core_orders_are_tagged_core_on_the_order_and_the_journal(
    engine: RotationEngine, conn: _RecordingConnection
) -> None:
    tilt = engine.core_tilt(
        order(IT_A, Side.BUY, 10, "100"),
        book(lot(IT_A, 100, "100")),
        trading_date=TRADING_DATE,
        rationale="core tilt",
    )
    assert tilt.order.request.tag == Sleeve.CORE.value
    assert _insert_field(conn.inserts[0], "sleeve") == Sleeve.CORE.value


def test_a_blocked_order_is_still_tagged(engine: RotationEngine) -> None:
    decision = engine.tactical_trade(
        order(IT_A, Side.BUY, 100, "1000"),  # blocked on the per-order cap
        book(cash="1000000"),
        trading_date=TRADING_DATE,
        rationale="oversized",
    )
    assert not decision.placed
    assert decision.order.request.tag == Sleeve.TACTICAL.value


# ── the dial's arithmetic and the sleeve split (§5.5) ───────────────────────────────────────────


def test_sleeve_targets_split_case_value_by_the_dial() -> None:
    targets = sleeve_targets(RotationDial(tactical_pct=Decimal("30")), Decimal("100000"))
    assert targets.tactical_target == Decimal("30000")
    assert targets.core_target == Decimal("70000")
    # Core and tactical always sum to the case value — the two never drift.
    assert targets.tactical_target + targets.core_target == Decimal("100000")


def test_sleeve_targets_reject_a_float_case_value() -> None:
    with pytest.raises(TypeError, match="Decimal"):
        sleeve_targets(RotationDial(tactical_pct=Decimal("30")), 100000.0)  # type: ignore[arg-type]


def test_sleeve_targets_reject_a_negative_case_value() -> None:
    with pytest.raises(ValueError, match="negative"):
        sleeve_targets(RotationDial(tactical_pct=Decimal("30")), Decimal("-1"))


def test_allocate_splits_the_book_and_reports_drift() -> None:
    # Case value 100_000: 40_000 tactical (IT_B), 40_000 core (IT_A), 20_000 cash.
    portfolio = book(lot(IT_A, 400, "100"), lot(IT_B, 400, "100"), cash="20000")
    alloc = allocate(
        portfolio, RotationDial(tactical_pct=Decimal("30")), tactical_isins=frozenset({IT_B})
    )
    assert isinstance(alloc, SleeveAllocation)
    assert alloc.tactical_value == Decimal("40000")
    assert alloc.core_value == Decimal("40000")
    assert alloc.cash == Decimal("20000")
    # Tactical is over its 30_000 target by 10_000; core is under its 70_000 target by 30_000.
    assert alloc.tactical_drift == Decimal("10000")
    assert alloc.core_drift == Decimal("-30000")


def test_resize_dial_refuses_a_noop() -> None:
    from analyst.cases import PolicyVersionError

    with pytest.raises(PolicyVersionError):
        resize_dial(ratified(), Decimal("30"))  # identical dial — nothing to re-ratify
