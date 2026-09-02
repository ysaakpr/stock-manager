"""M5.2: the risk rails block what they must, journal it, and offer no way around themselves.

Three claims, each shown rather than asserted in prose:

1. **Every cap is enforced, in both directions of the logic.** For each rail there is a case that
   passes at the boundary and one that breaks just past it, because a rail that is stuck open and a
   rail that is stuck shut both pass a test that only checks one side. The generated-stream proof
   that *no* sequence breaches a cap lives in `tests/property/test_rails_property.py`; this file
   pins the individual rails and the arithmetic behind them.
2. **A blocked order reaches the journal.** `guard_order` writes a `RAIL_BLOCK` line by the `RAILS`
   actor naming every breached rail, and a -25% drawdown writes a forced-review `ESCALATE` line —
   invariant #9 and acceptance criterion 2. The database is stood in for by a recording connection
   (the same device `test_journal.py` uses), so the file stays offline and fast (CLAUDE.md).
3. **There is no override.** No callable in `analyst/rails/` takes a bypass parameter, and nothing
   in the package imports an LLM — the rail is deterministic code the agent cannot talk out of it
   (invariant #6, acceptance criterion 3), proved by parsing the package, not by hoping.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from psycopg.types.json import Json

from analyst.cases import RiskRails
from analyst.journal import Actor, Decision, EvidenceStore, Journal, Sleeve
from analyst.rails import (
    FORCED_REVIEW_EVENT,
    HouseholdExposure,
    Lot,
    Portfolio,
    ProposedOrder,
    RailEngine,
    RailId,
    apply_order,
    assess_drawdown,
    check_order,
    drawdown_of,
)
from dataplatform.clock import IST, FrozenClock
from dataplatform.store.db import Connection
from execution.broker import Exchange, FractionalQuantityError, OrderRequest, Side

REPO_ROOT = Path(__file__).resolve().parents[2]
RAILS_PACKAGE = REPO_ROOT / "analyst" / "rails"

TRADING_DATE = date(2026, 8, 7)
DECIDED_AT = datetime(2026, 8, 7, 19, 30, tzinfo=IST)
CASE_ID = "AI_ROBOTICS"

IT_A = "INE001A01001"
IT_B = "INE002A01009"
PHARMA_A = "INE004A01005"
ENERGY_A = "INE007A01009"


# ── the ratified caps under test ───────────────────────────────────────────────────────────────


def make_rails(
    *,
    max_position_pct: str = "15",
    max_sector_pct: str = "35",
    min_holdings: int = 8,
    drawdown_review_pct: str = "25",
    max_order_value_inr: str = "1000000",
    max_order_pct_of_case: str = "20",
) -> RiskRails:
    return RiskRails(
        max_position_pct=Decimal(max_position_pct),
        max_sector_pct=Decimal(max_sector_pct),
        min_holdings=min_holdings,
        drawdown_review_pct=Decimal(drawdown_review_pct),
        max_order_value_inr=Decimal(max_order_value_inr),
        max_order_pct_of_case=Decimal(max_order_pct_of_case),
    )


def order(
    isin: str,
    side: Side,
    quantity: int,
    *,
    price: str = "100",
    sector: str = "IT",
) -> ProposedOrder:
    return ProposedOrder(
        request=OrderRequest(isin=isin, side=side, quantity=quantity, exchange=Exchange.NSE),
        price=Decimal(price),
        sector=sector,
    )


def lot(isin: str, sector: str, quantity: int, price: str) -> Lot:
    return Lot(isin=isin, sector=sector, quantity=quantity, price=Decimal(price))


# ── position cap ───────────────────────────────────────────────────────────────────────────────


def test_a_buy_that_stays_under_the_position_cap_passes() -> None:
    rails = make_rails(max_position_pct="15")
    book = Portfolio(case_id=CASE_ID, lots=(), cash=Decimal("1000000"))
    # 1000 x 100 = 100,000 = 10% of 1,000,000.
    assessment = check_order(order(IT_A, Side.BUY, 1000), book, rails)
    assert assessment.allowed


def test_a_buy_that_breaches_the_position_cap_is_blocked_and_names_the_rail() -> None:
    rails = make_rails(max_position_pct="15")
    book = Portfolio(case_id=CASE_ID, lots=(), cash=Decimal("1000000"))
    # 2000 x 100 = 200,000 = 20% of case value, past the 15% cap.
    assessment = check_order(order(IT_A, Side.BUY, 2000), book, rails)
    assert not assessment.allowed
    assert RailId.MAX_POSITION in assessment.breached_rails
    assert "MAX_POSITION" in assessment.rationale()


def test_the_position_cap_is_exact_at_the_boundary() -> None:
    rails = make_rails(max_position_pct="15")
    book = Portfolio(case_id=CASE_ID, lots=(), cash=Decimal("1000000"))
    # Exactly 15% is allowed (a max is inclusive); one share more is not.
    assert check_order(order(IT_A, Side.BUY, 1500), book, rails).allowed
    assert not check_order(order(IT_A, Side.BUY, 1501), book, rails).allowed


def test_a_second_buy_that_tips_an_existing_lot_over_is_blocked() -> None:
    rails = make_rails(max_position_pct="15")
    book = Portfolio(case_id=CASE_ID, lots=(lot(IT_A, "IT", 1400, "100"),), cash=Decimal("860000"))
    # Lot already 1400 x 100 = 140,000 of 1,000,000 = 14%. Adding 200 more → 16%.
    assessment = check_order(order(IT_A, Side.BUY, 200), book, rails)
    assert not assessment.allowed
    assert RailId.MAX_POSITION in assessment.breached_rails


def test_a_sell_never_breaches_the_position_cap() -> None:
    rails = make_rails(max_position_pct="15")
    book = Portfolio(case_id=CASE_ID, lots=(lot(IT_A, "IT", 5000, "100"),), cash=Decimal("500000"))
    # The lot is already 50% over its cap (a pre-existing state), but a sell can only reduce it.
    assessment = check_order(order(IT_A, Side.SELL, 1000), book, rails)
    assert RailId.MAX_POSITION not in assessment.breached_rails


# ── sector cap ─────────────────────────────────────────────────────────────────────────────────


def test_a_buy_that_breaches_the_sector_cap_is_blocked() -> None:
    rails = make_rails(max_position_pct="30", max_sector_pct="35")
    book = Portfolio(case_id=CASE_ID, lots=(lot(IT_A, "IT", 3000, "100"),), cash=Decimal("700000"))
    # IT already 300,000 = 30% (under its 30% position cap, on the boundary). A second IT name
    # adding 100,000 takes the *sector* to 40%, past 35%, though the new lot alone is only 10%.
    assessment = check_order(order(IT_B, Side.BUY, 1000, sector="IT"), book, rails)
    assert not assessment.allowed
    assert RailId.MAX_SECTOR in assessment.breached_rails
    assert RailId.MAX_POSITION not in assessment.breached_rails


def test_buys_in_different_sectors_do_not_pool() -> None:
    rails = make_rails(max_position_pct="30", max_sector_pct="35")
    book = Portfolio(case_id=CASE_ID, lots=(lot(IT_A, "IT", 3000, "100"),), cash=Decimal("700000"))
    # Same rupee add as the blocked case, but into PHARMA — its own sector is only 10%.
    assessment = check_order(order(PHARMA_A, Side.BUY, 1000, sector="PHARMA"), book, rails)
    assert assessment.allowed


# ── minimum-holdings floor ───────────────────────────────────────────────────────────────────


def test_a_full_exit_below_the_floor_is_blocked() -> None:
    rails = make_rails(min_holdings=3, max_position_pct="34")
    lots = (
        lot(IT_A, "IT", 1000, "100"),
        lot(PHARMA_A, "PHARMA", 1000, "100"),
        lot(ENERGY_A, "ENERGY", 1000, "100"),
    )
    book = Portfolio(case_id=CASE_ID, lots=lots, cash=Decimal("700000"))
    # Exactly at the floor of 3; selling one out entirely would leave 2.
    assessment = check_order(order(IT_A, Side.SELL, 1000), book, rails)
    assert not assessment.allowed
    assert RailId.MIN_HOLDINGS in assessment.breached_rails


def test_a_partial_sell_at_the_floor_is_allowed() -> None:
    rails = make_rails(min_holdings=3, max_position_pct="34")
    lots = (
        lot(IT_A, "IT", 1000, "100"),
        lot(PHARMA_A, "PHARMA", 1000, "100"),
        lot(ENERGY_A, "ENERGY", 1000, "100"),
    )
    book = Portfolio(case_id=CASE_ID, lots=lots, cash=Decimal("700000"))
    # A partial sell keeps the name, so the count does not fall.
    assessment = check_order(order(IT_A, Side.SELL, 400), book, rails)
    assert RailId.MIN_HOLDINGS not in assessment.breached_rails


def test_a_full_exit_above_the_floor_is_allowed() -> None:
    rails = make_rails(min_holdings=2, max_position_pct="50", max_sector_pct="100")
    lots = (
        lot(IT_A, "IT", 1000, "100"),
        lot(PHARMA_A, "PHARMA", 1000, "100"),
        lot(ENERGY_A, "ENERGY", 1000, "100"),
    )
    book = Portfolio(case_id=CASE_ID, lots=lots, cash=Decimal("700000"))
    # Three names, floor of two: exiting one leaves two, still at the floor.
    assessment = check_order(order(IT_A, Side.SELL, 1000), book, rails)
    assert RailId.MIN_HOLDINGS not in assessment.breached_rails


def test_building_below_the_floor_does_not_block_buys() -> None:
    rails = make_rails(min_holdings=8, max_position_pct="15")
    book = Portfolio(case_id=CASE_ID, lots=(), cash=Decimal("1000000"))
    # An empty book has 0 holdings, below the floor of 8; the rail must not block the first buy.
    assessment = check_order(order(IT_A, Side.BUY, 1000), book, rails)
    assert RailId.MIN_HOLDINGS not in assessment.breached_rails


# ── per-order sanity caps ─────────────────────────────────────────────────────────────────────


def test_an_order_over_the_rupee_cap_is_blocked() -> None:
    rails = make_rails(max_order_value_inr="100000", max_order_pct_of_case="100")
    book = Portfolio(case_id=CASE_ID, lots=(), cash=Decimal("100000000"))
    # 2000 x 100 = 200,000, past the 100,000 per-order rupee cap.
    assessment = check_order(order(IT_A, Side.BUY, 2000), book, rails)
    assert not assessment.allowed
    assert RailId.MAX_ORDER_VALUE in assessment.breached_rails


def test_an_order_over_the_pct_cap_is_blocked() -> None:
    rails = make_rails(max_order_pct_of_case="5", max_order_value_inr="100000000")
    book = Portfolio(case_id=CASE_ID, lots=(), cash=Decimal("1000000"))
    # 1000 x 100 = 100,000 = 10% of case value, past the 5% per-order cap.
    assessment = check_order(order(IT_A, Side.BUY, 1000), book, rails)
    assert not assessment.allowed
    assert RailId.MAX_ORDER_PCT in assessment.breached_rails


def test_the_sanity_caps_bind_a_sell_too() -> None:
    rails = make_rails(max_order_value_inr="100000", max_order_pct_of_case="100")
    book = Portfolio(case_id=CASE_ID, lots=(lot(IT_A, "IT", 5000, "100"),), cash=Decimal("500000"))
    # A fat-finger sell of 300,000 trips the rupee cap the same way a buy would.
    assessment = check_order(order(IT_A, Side.SELL, 3000), book, rails)
    assert not assessment.allowed
    assert RailId.MAX_ORDER_VALUE in assessment.breached_rails


# ── cross-case concentration ─────────────────────────────────────────────────────────────────


def test_cross_case_concentration_blocks_a_household_over_the_position_cap() -> None:
    rails = make_rails(max_position_pct="15")
    book = Portfolio(case_id=CASE_ID, lots=(), cash=Decimal("1000000"))
    # This case's own buy is a fine 10%, but the household already holds 18% of itself in the name.
    household = HouseholdExposure(
        isin=IT_A,
        household_value_in_isin=Decimal("360000"),
        household_total_value=Decimal("2000000"),
    )
    assessment = check_order(order(IT_A, Side.BUY, 1000), book, rails, household=household)
    assert not assessment.allowed
    assert RailId.CROSS_CASE_CONCENTRATION in assessment.breached_rails
    # The single-case check on the same order passes — the rail sees what the case alone cannot.
    assert check_order(order(IT_A, Side.BUY, 1000), book, rails).allowed


def test_cross_case_within_the_cap_passes() -> None:
    rails = make_rails(max_position_pct="15")
    book = Portfolio(case_id=CASE_ID, lots=(), cash=Decimal("1000000"))
    household = HouseholdExposure(
        isin=IT_A,
        household_value_in_isin=Decimal("200000"),
        household_total_value=Decimal("2000000"),
    )
    assessment = check_order(order(IT_A, Side.BUY, 1000), book, rails, household=household)
    assert assessment.allowed


def test_every_breach_is_reported_not_just_the_first() -> None:
    rails = make_rails(max_position_pct="5", max_order_pct_of_case="5", min_holdings=20)
    book = Portfolio(case_id=CASE_ID, lots=(), cash=Decimal("1000000"))
    # 1000 x 100 = 100,000 = 10% — over both the position cap and the per-order pct cap at once.
    assessment = check_order(order(IT_A, Side.BUY, 1000), book, rails)
    assert {RailId.MAX_POSITION, RailId.MAX_ORDER_PCT} <= set(assessment.breached_rails)


# ── apply_order (the pure book transition) ───────────────────────────────────────────────────


def test_apply_buy_moves_cash_into_a_new_lot() -> None:
    book = Portfolio(case_id=CASE_ID, lots=(), cash=Decimal("1000000"))
    after = apply_order(book, order(IT_A, Side.BUY, 1000))
    assert after.cash == Decimal("900000")
    assert after.lot(IT_A) is not None
    assert cast(Lot, after.lot(IT_A)).quantity == 1000
    assert after.total_value == book.total_value  # value is invariant under a trade


def test_apply_partial_sell_shrinks_the_lot() -> None:
    book = Portfolio(case_id=CASE_ID, lots=(lot(IT_A, "IT", 1000, "100"),), cash=Decimal("0"))
    after = apply_order(book, order(IT_A, Side.SELL, 400))
    assert cast(Lot, after.lot(IT_A)).quantity == 600
    assert after.cash == Decimal("40000")


def test_apply_full_sell_removes_the_lot() -> None:
    book = Portfolio(case_id=CASE_ID, lots=(lot(IT_A, "IT", 1000, "100"),), cash=Decimal("0"))
    after = apply_order(book, order(IT_A, Side.SELL, 1000))
    assert after.lot(IT_A) is None
    assert after.holding_count == 0


def test_apply_rejects_overselling() -> None:
    book = Portfolio(case_id=CASE_ID, lots=(lot(IT_A, "IT", 1000, "100"),), cash=Decimal("0"))
    with pytest.raises(ValueError, match="only 1000 held"):
        apply_order(book, order(IT_A, Side.SELL, 1001))


def test_apply_rejects_selling_what_is_not_held() -> None:
    book = Portfolio(case_id=CASE_ID, lots=(), cash=Decimal("0"))
    with pytest.raises(ValueError, match="does not hold"):
        apply_order(book, order(IT_A, Side.SELL, 1))


# ── drawdown monitor ─────────────────────────────────────────────────────────────────────────


def test_a_25pct_drawdown_forces_review() -> None:
    rails = make_rails(drawdown_review_pct="25")
    status = assess_drawdown([Decimal("100"), Decimal("120"), Decimal("90")], rails)
    assert status.review_forced
    assert status.drawdown_pct == Decimal("25")
    assert status.peak == Decimal("120")
    assert status.trough == Decimal("90")


def test_a_recovery_still_reports_the_worst_fall() -> None:
    rails = make_rails(drawdown_review_pct="25")
    # Fell 30% to 70, then recovered to 95 — the review was forced at the trough.
    status = assess_drawdown([Decimal("100"), Decimal("70"), Decimal("95")], rails)
    assert status.review_forced
    assert status.drawdown_pct == Decimal("30")


def test_a_shallow_fall_does_not_force_review() -> None:
    rails = make_rails(drawdown_review_pct="25")
    status = assess_drawdown([Decimal("100"), Decimal("80")], rails)
    assert not status.review_forced
    assert status.drawdown_pct == Decimal("20")


def test_a_monotonic_rise_has_no_drawdown() -> None:
    rails = make_rails(drawdown_review_pct="25")
    status = assess_drawdown([Decimal("100"), Decimal("110"), Decimal("130")], rails)
    assert status.drawdown_pct == Decimal("0")
    assert not status.review_forced


def test_an_empty_series_is_a_zero_drawdown_not_an_error() -> None:
    rails = make_rails(drawdown_review_pct="25")
    status = assess_drawdown([], rails)
    assert status.drawdown_pct == Decimal("0")
    assert not status.review_forced


def test_drawdown_rejects_a_float_value() -> None:
    rails = make_rails()
    with pytest.raises(TypeError, match="Decimal"):
        assess_drawdown([Decimal("100"), cast(Decimal, 90.0)], rails)


# ── value-object guards ──────────────────────────────────────────────────────────────────────


def test_a_lot_rejects_a_float_price() -> None:
    with pytest.raises(TypeError, match="Decimal"):
        Lot(isin=IT_A, sector="IT", quantity=1, price=cast(Decimal, 100.0))


def test_an_order_rejects_a_fractional_quantity() -> None:
    with pytest.raises(FractionalQuantityError):
        order(IT_A, Side.BUY, 0)


def test_a_portfolio_rejects_two_lots_for_one_isin() -> None:
    with pytest.raises(ValueError, match="one lot per ISIN"):
        Portfolio(
            case_id=CASE_ID,
            lots=(lot(IT_A, "IT", 1, "100"), lot(IT_A, "IT", 2, "100")),
            cash=Decimal("0"),
        )


def test_drawdown_of_is_never_negative() -> None:
    assert drawdown_of(Decimal("100"), Decimal("150")) == Decimal("0")
    assert drawdown_of(Decimal("0"), Decimal("0")) == Decimal("0")


# ── journalling (the recording-connection half) ──────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, rows: Sequence[tuple[Any, ...]]) -> None:
        self._rows = list(rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _RecordingConnection:
    """Echoes an insert's parameters back as the returned row, exactly as `INSERT ... RETURNING`.

    The same device `tests/unit/test_journal.py` uses, so the rail-block and forced-review writes
    are asserted on offline without a database (CLAUDE.md); Postgres actually enforcing the
    append-only table is the integration suite's job.
    """

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


@pytest.fixture
def conn() -> _RecordingConnection:
    return _RecordingConnection()


@pytest.fixture
def engine(conn: _RecordingConnection, tmp_path: Path) -> RailEngine:
    journal = Journal(
        cast(Connection, conn),
        clock=FrozenClock(DECIDED_AT),
        evidence=EvidenceStore(tmp_path / "evidence"),
    )
    return RailEngine(journal, clock=FrozenClock(DECIDED_AT))


def _insert_field(params: Sequence[Any], name: str) -> Any:
    """Read one written column out of an INSERT's parameter tuple by name."""
    from analyst.journal.writer import _WRITE_COLUMNS

    return params[_WRITE_COLUMNS.index(name)]


def test_guard_order_journals_a_rail_block_naming_the_rail(
    engine: RailEngine, conn: _RecordingConnection
) -> None:
    rails = make_rails(max_position_pct="15")
    book = Portfolio(case_id=CASE_ID, lots=(), cash=Decimal("1000000"))
    assessment = engine.guard_order(
        order(IT_A, Side.BUY, 2000), book, rails, trading_date=TRADING_DATE, sleeve=Sleeve.TACTICAL
    )
    assert not assessment.allowed
    assert len(conn.inserts) == 1
    params = conn.inserts[0]
    assert _insert_field(params, "decision") == Decision.RAIL_BLOCK.value
    assert _insert_field(params, "actor") == Actor.RAILS.value
    assert _insert_field(params, "isin") == IT_A
    assert _insert_field(params, "sleeve") == Sleeve.TACTICAL.value
    assert _insert_field(params, "case_id") == CASE_ID
    assert "MAX_POSITION" in _insert_field(params, "rationale")
    assert _insert_field(params, "payload")["rails"] == "MAX_POSITION"


def test_guard_order_does_not_journal_when_the_order_passes(
    engine: RailEngine, conn: _RecordingConnection
) -> None:
    rails = make_rails(max_position_pct="15")
    book = Portfolio(case_id=CASE_ID, lots=(), cash=Decimal("1000000"))
    assessment = engine.guard_order(
        order(IT_A, Side.BUY, 1000), book, rails, trading_date=TRADING_DATE
    )
    assert assessment.allowed
    # A passed rail is not itself a decision — the caller's next order or heartbeat is.
    assert conn.inserts == []


def test_review_drawdown_journals_a_forced_review(
    engine: RailEngine, conn: _RecordingConnection
) -> None:
    rails = make_rails(drawdown_review_pct="25")
    status = engine.review_drawdown(
        [Decimal("100"), Decimal("120"), Decimal("84")],
        rails,
        case_id=CASE_ID,
        trading_date=TRADING_DATE,
    )
    assert status.review_forced
    assert len(conn.inserts) == 1
    params = conn.inserts[0]
    assert _insert_field(params, "decision") == Decision.ESCALATE.value
    assert _insert_field(params, "actor") == Actor.RAILS.value
    assert _insert_field(params, "case_id") == CASE_ID
    payload = _insert_field(params, "payload")
    assert payload["event"] == FORCED_REVIEW_EVENT
    assert Decimal(payload["drawdown_pct"]) == Decimal("30")


def test_review_drawdown_is_silent_when_no_review_is_forced(
    engine: RailEngine, conn: _RecordingConnection
) -> None:
    rails = make_rails(drawdown_review_pct="25")
    status = engine.review_drawdown(
        [Decimal("100"), Decimal("90")], rails, case_id=CASE_ID, trading_date=TRADING_DATE
    )
    assert not status.review_forced
    assert conn.inserts == []


# ── no override, no LLM (acceptance criterion 3, invariant #6) ────────────────────────────────


_BANNED_PARAMETERS = frozenset(
    {
        "override",
        "overrides",
        "bypass",
        "force",
        "forced",
        "skip",
        "skip_rails",
        "ignore",
        "ignore_rails",
        "disable",
        "disabled",
        "unsafe",
        "allow_breach",
        "allow_override",
        "no_rails",
    }
)


def _package_sources() -> list[tuple[Path, ast.Module]]:
    trees: list[tuple[Path, ast.Module]] = []
    for path in sorted(RAILS_PACKAGE.glob("*.py")):
        trees.append((path, ast.parse(path.read_text(encoding="utf-8"))))
    assert trees, "expected python sources in analyst/rails/"
    return trees


def test_no_rail_callable_takes_a_bypass_parameter() -> None:
    """The rail has no override: parse every function and reject a bypass-shaped parameter.

    A behavioural test can only prove the paths it calls have no bypass; this proves *no* callable
    in the package declares one, which is what "the agent has no override" (invariant #6) means.
    """
    offenders: list[str] = []
    for path, tree in _package_sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            args = node.args
            names = [
                a.arg
                for a in (
                    *args.posonlyargs,
                    *args.args,
                    *args.kwonlyargs,
                    args.vararg,
                    args.kwarg,
                )
                if a is not None
            ]
            for name in names:
                if name in _BANNED_PARAMETERS:
                    offenders.append(f"{path.name}:{node.name}({name})")
    assert not offenders, f"a rail must not offer a bypass parameter: {offenders}"


def test_the_rails_package_imports_no_llm() -> None:
    """Rails are deterministic code, never an LLM judgment (invariant #6).

    No module in `analyst/rails/` may import an LLM client or a provider SDK — the check is a
    structural fact, not a promise, so a future edit that reaches for a model here fails the suite.
    """
    banned = ("analyst.llm", "anthropic")
    offenders: list[str] = []
    for path, tree in _package_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if any(alias.name.startswith(b) for b in banned):
                        offenders.append(f"{path.name}: import {alias.name}")
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module is not None
                and any(node.module.startswith(b) for b in banned)
            ):
                offenders.append(f"{path.name}: from {node.module}")
    assert not offenders, f"rails must not touch an LLM (invariant #6): {offenders}"
