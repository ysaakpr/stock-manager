"""X2 — a run's daily NAV is persisted beside its ledger, deterministically (``backtest.nav``).

* ``persist_run`` writes ``navs/<digest>.json`` when the runner sampled a path, before the
  summary, and a re-run writes byte-identical bytes;
* the after-tax NAV is the pre-tax NAV less the tax paid on or before each session, with tax due
  after the last session charged on the last point — and a run with no tax is unchanged;
* daily returns are consecutive-point simple returns, refused on a non-positive NAV.

Offline: no lake, no network, no wall clock.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from backtest.nav import (
    PRE_TAX,
    NavFormatError,
    NavSeries,
    after_tax_nav,
    after_tax_nav_file,
    daily_returns,
    nav_file,
    read_nav,
    write_nav,
)
from backtest.run_ledger import persist_run, persist_run_ledgers, run_digest, run_spec, summary_path
from backtest.tax import FyTax, InvestorProfile, PaymentTiming, RunLedger
from backtest.xirr import Cashflow

_ZERO = Decimal("0")
_CASH = Decimal("1000000")
_PROFILE = InvestorProfile(
    residency="resident_individual",
    slab_rate=Decimal("0.30"),
    cg_surcharge_rate=Decimal("0"),
    dividend_surcharge_rate=Decimal("0"),
    payment_timing=PaymentTiming.FY_END,
)
_POINTS = (
    (date(2024, 3, 28), Decimal("1000000")),
    (date(2024, 4, 1), Decimal("1010000.50")),
    (date(2024, 4, 2), Decimal("1005000.25")),
    (date(2025, 4, 1), Decimal("1100000")),
    (date(2025, 4, 2), Decimal("1120000")),
)


def _spec() -> dict[str, str]:
    return run_spec(
        "synthetic",
        start=_POINTS[0][0],
        end=_POINTS[-1][0],
        opening_cash=_CASH,
        book_actions=None,
        parameters="nav",
    )


def _ledger() -> RunLedger:
    return RunLedger(
        source="synthetic",
        trades=(),
        external_flows=(Cashflow(_POINTS[0][0], -_CASH),),
        terminal_date=_POINTS[-1][0],
        terminal_nav=_POINTS[-1][1],
        terminal_prices={},
    )


def _tax(fy: int, total: Decimal) -> FyTax:
    return FyTax(
        fy=fy,
        stcg_gross={},
        ltcg_gross={},
        st_loss=_ZERO,
        lt_loss=_ZERO,
        exempt_ltcg_net=_ZERO,
        brought_forward_used=_ZERO,
        exemption_used=_ZERO,
        stcg_taxable={},
        ltcg_taxable={},
        carried_forward_st=_ZERO,
        carried_forward_lt=_ZERO,
        expired=_ZERO,
        dividends=_ZERO,
        dividend_taxable=_ZERO,
        cg_tax=total,
        dividend_tax=_ZERO,
        surcharge=_ZERO,
        cess=_ZERO,
        payment_date=PaymentTiming.FY_END.payment_date(fy),
    )


def test_persist_run_writes_the_nav_beside_the_ledger(tmp_path: Path) -> None:
    with persist_run_ledgers(tmp_path):
        persist_run(_spec(), _ledger(), None, nav=_POINTS)
    digest = run_digest(_spec())
    series = read_nav(nav_file(tmp_path, digest), digest=digest)
    assert series == NavSeries(digest, PRE_TAX, _POINTS)
    assert not summary_path(tmp_path, digest).exists()  # no summary was asked for


def test_no_nav_file_when_the_runner_sampled_no_path(tmp_path: Path) -> None:
    with persist_run_ledgers(tmp_path):
        persist_run(_spec(), _ledger(), None)
    assert not nav_file(tmp_path, run_digest(_spec())).exists()


def test_a_rerun_writes_a_byte_identical_nav(tmp_path: Path) -> None:
    first, second = tmp_path / "one", tmp_path / "two"
    for out in (first, second):
        with persist_run_ledgers(out):
            persist_run(_spec(), _ledger(), None, nav=_POINTS)
    digest = run_digest(_spec())
    assert nav_file(first, digest).read_bytes() == nav_file(second, digest).read_bytes()
    assert not list(first.rglob("*.partial"))


def test_a_nav_file_naming_another_run_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    write_nav(NavSeries("a" * 64, PRE_TAX, _POINTS), path)
    with pytest.raises(NavFormatError, match="carries digest"):
        read_nav(path, digest="b" * 64)


def test_sessions_must_strictly_increase() -> None:
    with pytest.raises(NavFormatError, match="strictly increasing"):
        NavSeries("d", PRE_TAX, (_POINTS[1], _POINTS[0]))


def test_after_tax_nav_deducts_tax_from_its_payment_date() -> None:
    # FY 2023-24's tax is paid 2024-03-31: every point from 2024-04-01 carries it. FY 2024-25's is
    # paid 2025-03-31: the last two points carry both. FY 2025-26's falls due after the last point
    # (2026-03-31) and is charged on the last point.
    series = NavSeries("d", PRE_TAX, _POINTS)
    taxes = [_tax(2023, Decimal("1000")), _tax(2024, Decimal("2500")), _tax(2025, Decimal("7"))]
    out = after_tax_nav(series, taxes, _PROFILE)
    assert [nav for _, nav in out.points] == [
        Decimal("1000000"),
        Decimal("1009000.50"),
        Decimal("1004000.25"),
        Decimal("1096500"),
        Decimal("1116493"),
    ]
    assert [d for d, _ in out.points] == [d for d, _ in _POINTS]
    assert out.basis.startswith("after-tax slab0.30")


def test_after_tax_nav_without_tax_is_the_pre_tax_nav() -> None:
    out = after_tax_nav(NavSeries("d", PRE_TAX, _POINTS), [], _PROFILE)
    assert out.points == _POINTS


def test_after_tax_file_names_the_profile(tmp_path: Path) -> None:
    path = after_tax_nav_file(tmp_path, "d" * 64, _PROFILE)
    assert path.name == f"{'d' * 64}.after-tax.slab0.30-cgsur0-divsur0-fy_end-cessschedule.json"


def test_daily_returns_are_consecutive_simple_returns() -> None:
    points = (
        (date(2024, 1, 1), Decimal("100")),
        (date(2024, 1, 2), Decimal("110")),
        (date(2024, 1, 3), Decimal("99")),
    )
    assert daily_returns(points) == pytest.approx([0.10, -0.10])
    assert daily_returns(points[:1]) == []
    with pytest.raises(ValueError, match="non-positive"):
        daily_returns(((date(2024, 1, 1), _ZERO), (date(2024, 1, 2), Decimal("1"))))
