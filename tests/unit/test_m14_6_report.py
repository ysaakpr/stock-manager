"""M14.6 report: the book-exposure rebuild from a run ledger, and the campaign manifest."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from backtest.m12_rerun import PER_STOCK_SET, RerunPlan, build_manifest
from backtest.m14_6_report import exposure
from backtest.sweep import D13_PAPER_BASELINE


def _ledger(**overrides: object) -> dict[str, object]:
    ledger: dict[str, object] = {
        "external_flows": [{"amount": "-1000", "date": "2024-01-01"}],
        "dividends": [],
        "interest": [],
        "corporate_events": [
            {
                "ex_date": "2024-01-04",
                "isin": "A",
                "kind": "split",
                "resulting_quantity": "20",
                "numerator": "2",
                "denominator": "1",
            },
            {"ex_date": "2024-01-05", "from_isin": "A", "isin": "A2", "kind": "reissue"},
        ],
        "trades": [
            {
                "isin": "A",
                "side": "BUY",
                "quantity": "10",
                "net_amount": "500",
                "trade_date": "2024-01-02",
            },
            {
                "isin": "B",
                "side": "BUY",
                "quantity": "5",
                "net_amount": "400",
                "trade_date": "2024-01-02",
            },
            {
                "isin": "B",
                "side": "SELL",
                "quantity": "5",
                "net_amount": "450",
                "trade_date": "2024-01-03",
            },
        ],
        "terminal_prices": {"A2": "30"},
        "terminal_nav": "1150",
    }
    ledger.update(overrides)
    return ledger


_NAV = {
    "points": [
        ["2024-01-01", "1000"],
        ["2024-01-02", "1000"],
        ["2024-01-03", "1050"],
        ["2024-01-04", "1100"],
        ["2024-01-05", "1150"],
    ]
}


def test_the_rebuild_counts_names_and_cash_through_fills_splits_and_reissues() -> None:
    e = exposure(_ledger(), _NAV, Decimal("1"))
    # Names at each close: 0, 2 (A, B), 1 (A), 1 (A split), 1 (A2 reissued).
    assert e.mean_names == Decimal("1.0")
    assert e.empty_share == Decimal("0.2")
    assert e.thin_share == Decimal("1")
    # Cash 1000, 100, 550, 550, 550 over NAV 1000, 1000, 1050, 1100, 1150.
    expected = (
        Decimal(1)
        + Decimal("0.1")
        + Decimal(550) / 1050
        + Decimal(550) / 1100
        + Decimal(550) / 1150
    ) / 5
    assert e.mean_cash_share == expected
    assert e.trading_sessions_per_year == Decimal("2.0")
    assert e.checked  # 550 cash + 20 x 30 = 1150, the terminal NAV


def test_a_rebuild_that_misses_the_terminal_nav_is_flagged_unchecked() -> None:
    assert not exposure(_ledger(terminal_nav="1300"), _NAV, Decimal("1")).checked


def test_the_manifest_records_a_panel_only_when_one_is_used(tmp_path: Path) -> None:
    plan = RerunPlan(out_dir=tmp_path, data_root=None, arms=PER_STOCK_SET)
    manifest = build_manifest(plan, commit="abc", last_session=date(2026, 1, 1))
    assert "signal_panel" not in manifest
    assert manifest["arms"][0] == D13_PAPER_BASELINE.label
