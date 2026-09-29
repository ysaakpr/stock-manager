"""Test support: rail gates for replay tests whose subject is not the rails.

The replay engine takes a required ``RailGate`` (invariant #6). A test of the engine's
*mechanics* — determinism, the PIT guard, journal stamping — drives two- or three-name books that
the ratified 15% position cap would block on the first buy, which would make those tests about the
rails instead. They run under ``mechanics_rail_policy``: a real policy, checked by the real A8 on
every order, whose caps are set wide enough that a tiny test book does not meet them. It is
test-only by construction (it lives under ``tests/``) and named so no report could mistake it for
the ratified one. Tests of the rails themselves use
``backtest.rails.ratified_backtest_rail_policy``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal

from analyst.cases import RiskRails
from backtest.rails import BacktestRailPolicy, RailGate, SectorMap


def mechanics_rail_policy(sectors: Mapping[str, str] | None = None) -> BacktestRailPolicy:
    """A real rail policy whose caps a two-name test book never meets (see the module docstring)."""
    return BacktestRailPolicy(
        policy_id="test-engine-mechanics",
        version=1,
        rails=RiskRails(
            max_position_pct=Decimal("100"),
            max_sector_pct=Decimal("100"),
            min_holdings=1,
            drawdown_review_pct=Decimal("100"),
            max_order_value_inr=Decimal("1000000000000"),
            max_order_pct_of_case=Decimal("100"),
        ),
        sectors=SectorMap(source="test", sha256="test", by_isin=dict(sectors or {})),
        provenance="test-only: engine-mechanics tests, caps wide of a two-name book",
    )


def marks_from(closes: Mapping[tuple[str, date], Decimal]) -> Callable[[date], dict[str, Decimal]]:
    """A ``marks`` source over a ``(isin, session) -> close`` table."""

    def marks(session: date) -> dict[str, Decimal]:
        return {isin: close for (isin, on), close in closes.items() if on == session}

    return marks


def mechanics_gate(closes: Mapping[tuple[str, date], Decimal]) -> RailGate:
    """``mechanics_rail_policy`` over ``closes``."""
    return RailGate(mechanics_rail_policy(), marks_from(closes))
