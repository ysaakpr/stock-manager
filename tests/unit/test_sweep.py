"""M12.2 — the strategy sweep, unit-tested offline (EXECUTION_PLAN §7, X2).

The sweep's own logic is three claims, and each is tested here rather than read off a report:

* **one windowed pass over the lake** — the feature cache is incremental, so a second ``load`` of
  dates already materialized issues no query at all. That is what makes twenty arms cost one pass;
  if it regresses, the sweep silently becomes twenty passes and only the wall clock notices.
* **the arms are a comparison, not a grid** — every label unique, every ``reference`` naming a real
  arm, every arm driving exactly one policy, and at least twenty of them.
* **the ranking is return per drawdown, and a failure is a row** — including the case that decides
  whether the ranking is honest: an arm with no measured drawdown is ranked last, not first.

Offline: no lake, no network, no wall clock. The rows are built from stub runs, because what is
under test is the ordering and the rendering, never the replay.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest

from analyst.rails import Portfolio, ProposedOrder, RailId, check_order
from backtest.rails import ratified_backtest_rail_policy
from backtest.run import _SwingFeatures
from backtest.sweep import (
    _DEFAULT_OPENING_CASH,
    ARMS,
    HIGH_FLOOR,
    LOW_FLOOR,
    RETIRED_ARMS,
    Arm,
    SweepResult,
    SweepRow,
    render_sweep_report,
    tax_cells,
)
from backtest.tax import InvestorProfile, PaymentTiming
from execution.broker import OrderRequest, Side

_SESSION = date(2020, 1, 1)


def _stub_run(
    *, xirr: str, drawdown: str, excess: str = "0.02", benchmark_source: str = "published_tri"
) -> Any:
    """A stand-in for ``BacktestResult`` carrying only what a row reads off it."""
    return SimpleNamespace(
        comparison=SimpleNamespace(
            portfolio_xirr=Decimal(xirr),
            benchmark_xirr=Decimal("0.0856"),
            excess_over_benchmark=Decimal(excess),
        ),
        max_drawdown=Decimal(drawdown),
        total_charges=Decimal("100000"),
        benchmark_index_name="Nifty 50",
        benchmark_source=benchmark_source,
    )


_PROFILE = InvestorProfile(
    residency="resident_individual",
    slab_rate=Decimal("0.30"),
    cg_surcharge_rate=Decimal("0.15"),
    dividend_surcharge_rate=Decimal("0.15"),
    payment_timing=PaymentTiming.FY_END,
)


def _stub_after_tax(xirr: str) -> Any:
    """A stand-in for ``AfterTaxResult``: 3 points of tax drag off the pre-tax XIRR."""
    taxed = Decimal(xirr) - Decimal("0.03")
    return SimpleNamespace(
        after_tax_xirr_realised=taxed,
        after_tax_xirr_liquidated=taxed - Decimal("0.01"),
        total_tax=Decimal("250000"),
        total_tax_liquidated=Decimal("400000"),
    )


def _row(
    label: str,
    *,
    xirr: str,
    drawdown: str,
    floor: Decimal = LOW_FLOOR,
    benchmark_source: str = "published_tri",
) -> SweepRow:
    arm = Arm(
        label=label,
        family="test",
        reference="—",
        note="a stub",
        swing=ARMS[0].swing,
    )
    return SweepRow(
        arm=arm,
        floor=floor,
        run=cast(Any, _stub_run(xirr=xirr, drawdown=drawdown, benchmark_source=benchmark_source)),
        round_trips=10,
        median_hold_days=30,
        after_tax=cast(Any, _stub_after_tax(xirr)),
    )


# ── the arm list is a comparison, not a grid ─────────────────────────────────────────────────────


def test_the_sweep_carries_at_least_twenty_arms() -> None:
    """The acceptance criterion, stated directly."""
    assert len(ARMS) >= 20


def test_every_arm_label_is_unique() -> None:
    """Two arms sharing a label would make the ranked table unreadable and the report wrong."""
    labels = [arm.label for arm in ARMS]
    assert len(labels) == len(set(labels))


def test_every_reference_names_a_real_arm() -> None:
    """A row reads as the price of one change only if the thing it changed is in the table."""
    labels = {arm.label for arm in ARMS}
    for arm in ARMS:
        assert arm.reference == "—" or arm.reference in labels, arm.label


def test_every_arm_states_a_change() -> None:
    for arm in ARMS:
        assert arm.note.strip(), arm.label
        assert arm.family.strip(), arm.label


def test_every_swing_arm_s_basket_is_one_the_ratified_rails_admit() -> None:
    """A top-N whose equal-weight entry the rails block never trades — that was the top-5 arm (X2).

    Checked against the rails themselves, not loosened to fit: an equal-weight buy of 1/N of the
    case must clear the position cap and the per-order % cap, one lot of the opening budget must
    clear the rupee order cap, and N must reach the minimum-holdings floor so the book it builds
    is one A8 lets it rotate. Re-add "top-5" (or any N below 9 at ₹10 lakh) and this fails.
    """
    rails = ratified_backtest_rail_policy().rails
    for arm in ARMS:
        if arm.swing is None:
            continue
        n = Decimal(arm.swing.top_n)
        assert Decimal(100) / n <= rails.max_position_pct, arm.label
        assert Decimal(100) / n <= rails.max_order_pct_of_case, arm.label
        lot = _DEFAULT_OPENING_CASH * arm.swing.buy_budget_fraction / n
        assert lot <= rails.max_order_value_inr, arm.label
        assert arm.swing.top_n >= rails.min_holdings, arm.label


def test_the_retired_top_5_arm_is_gone_with_its_reason_stated() -> None:
    labels = {arm.label for arm in ARMS}
    for label, reason in RETIRED_ARMS:
        assert label not in labels
        assert reason.strip(), label
    assert "Short composite, top-5" in {label for label, _ in RETIRED_ARMS}


def test_the_rails_block_a_top_5_entry_which_is_why_the_arm_never_traded() -> None:
    """The diagnosis, reproduced: the first equal-weight top-5 buy is 19.6 % of a fresh case."""
    rails = ratified_backtest_rail_policy().rails
    book = Portfolio(case_id="sweep", lots=(), cash=_DEFAULT_OPENING_CASH)
    lot_value = _DEFAULT_OPENING_CASH * Decimal("0.98") / 5
    order = ProposedOrder(
        request=OrderRequest(isin="INE002A01018", side=Side.BUY, quantity=int(lot_value / 100)),
        price=Decimal("100"),
        sector="UNKNOWN",
    )
    breached = {breach.rail for breach in check_order(order, book, rails).breaches}
    assert RailId.MAX_POSITION in breached
    assert RailId.MAX_ORDER_PCT in breached


def test_an_arm_must_drive_exactly_one_policy() -> None:
    with pytest.raises(ValueError, match="exactly one policy"):
        Arm(label="none", family="test", reference="—", note="drives nothing")


def test_the_sweep_covers_the_short_horizon_families() -> None:
    """The families asked for: reversal, a short trend, and holds under three months."""
    families = {arm.family for arm in ARMS}
    assert {
        "single leg",
        "composite",
        "holding period",
        "risk overlay",
        "concentration",
    } <= families
    assert any("reversal" in arm.label.lower() for arm in ARMS)
    assert any(arm.family == "baseline" for arm in ARMS)


def test_every_swing_arm_holds_for_no_more_than_a_quarter() -> None:
    """The brief is a 7-90 day band; an arm re-underwriting past 63 sessions is outside it."""
    for arm in ARMS:
        if arm.swing is not None:
            assert arm.swing.max_hold_sessions <= 63, arm.label
            assert arm.swing.min_hold_sessions >= 2, arm.label


# ── the ranking ──────────────────────────────────────────────────────────────────────────────────


def test_rows_are_ranked_on_return_per_drawdown_not_on_return() -> None:
    """The owner decision, pinned: the higher-return arm loses to the better ratio."""
    greedy = _row("greedy", xirr="0.30", drawdown="0.50")  # ratio 0.60
    steady = _row("steady", xirr="0.20", drawdown="0.20")  # ratio 1.00
    result = SweepResult(rows=[greedy, steady])
    assert [row.arm.label for row in result.ranked(LOW_FLOOR)] == ["steady", "greedy"]
    # The inversion: ranked on return alone the order would be the other way round.
    assert greedy.xirr > steady.xirr


def test_an_arm_with_no_measured_drawdown_ranks_last_not_first() -> None:
    """An unmeasurable denominator is not a perfect score."""
    unmeasured = _row("never fell", xirr="0.25", drawdown="0")
    ordinary = _row("ordinary", xirr="0.10", drawdown="0.40")
    result = SweepResult(rows=[unmeasured, ordinary])
    assert result.ranked(LOW_FLOOR)[0].arm.label == "ordinary"
    assert unmeasured.return_per_drawdown == Decimal("0")


def test_ranking_is_per_floor() -> None:
    """A floor's table holds only its own rows — the two are never pooled into one ranking."""
    low = _row("low only", xirr="0.30", drawdown="0.30", floor=LOW_FLOOR)
    high = _row("high only", xirr="0.10", drawdown="0.30", floor=HIGH_FLOOR)
    result = SweepResult(rows=[low, high])
    assert [r.arm.label for r in result.ranked(LOW_FLOOR)] == ["low only"]
    assert [r.arm.label for r in result.ranked(HIGH_FLOOR)] == ["high only"]


def test_a_failed_arm_keeps_its_row_and_is_ranked_last() -> None:
    """Dropping a failing arm is how a sweep reports a survivor bias it created itself."""
    good = _row("good", xirr="0.15", drawdown="0.30")
    broken = SweepRow(arm=good.arm, floor=LOW_FLOOR, error="no sessions in window")
    result = SweepResult(rows=[broken, good], profile=_PROFILE)
    ranked = result.ranked(LOW_FLOOR)
    assert len(ranked) == 2
    assert ranked[0].ok and not ranked[-1].ok
    assert "no sessions in window" in render_sweep_report(result, floors=[LOW_FLOOR])
    assert "failed" in render_sweep_report(result, floors=[LOW_FLOOR])


# ── the report ───────────────────────────────────────────────────────────────────────────────────


def test_the_report_states_both_floors_and_what_each_arm_changed() -> None:
    result = SweepResult(
        rows=[
            _row("a", xirr="0.20", drawdown="0.25", floor=LOW_FLOOR),
            _row("b", xirr="0.12", drawdown="0.25", floor=HIGH_FLOOR),
        ],
        start=_SESSION,
        terminal=date(2026, 8, 31),
        sessions=2470,
        profile=_PROFILE,
    )
    report = render_sweep_report(result, floors=[LOW_FLOOR, HIGH_FLOOR])
    assert "₹1 crore/day" in report
    assert "₹10 crore/day" in report
    assert "What each arm changed" in report
    assert "cannot be asked to prove" in report
    # A removed arm is named with its reason, so an older table's missing row is explained.
    assert "## Arms removed from the sweep" in report
    assert "| Short composite, top-5 |" in report


def _report_on(source: str) -> str:
    result = SweepResult(
        rows=[_row("a", xirr="0.20", drawdown="0.25", benchmark_source=source)],
        start=_SESSION,
        terminal=date(2026, 8, 31),
        sessions=2470,
        benchmark_name="Nifty 50",
        profile=_PROFILE,
    )
    return render_sweep_report(result, floors=[LOW_FLOOR])


def test_the_report_names_the_benchmark_from_the_rows_recorded_source() -> None:
    """The stale "price-return L1 proxy" caveat is read from the source, never hard-coded."""
    published = _report_on("published_tri")
    assert "price-return L1 proxy" not in published
    assert "Excess is against the exchange's published TRI" in published
    assert "published Nifty 50 TRI" in published
    proxy = _report_on("l1_proxy")
    assert "Excess is against a price-return L1 proxy" in proxy
    assert "published TRI" not in proxy


# ── one windowed pass over the lake ──────────────────────────────────────────────────────────────


class _RecordingConnection:
    """A DuckDB stand-in that records every statement it is asked to execute."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str, params: object = None) -> _RecordingConnection:
        self.statements.append(sql)
        return self

    def fetchall(self) -> list[tuple[object, ...]]:
        return []


def _features_with(connection: _RecordingConnection) -> _SwingFeatures:
    """A ``_SwingFeatures`` around a recording connection, without opening a lake."""
    features = _SwingFeatures.__new__(_SwingFeatures)
    features._con = cast(Any, connection)
    features._adjusted = True
    features._have_factors = False
    features._by_date = {}
    features._imputed = 0
    features._rows = 0
    return features


def test_loading_the_same_dates_twice_issues_one_query() -> None:
    """The claim the whole sweep rests on: many arms, one windowed pass."""
    connection = _RecordingConnection()
    features = _features_with(connection)
    dates = [date(2020, 1, 6), date(2020, 1, 20)]
    features.load(dates)
    assert len(connection.statements) == 1
    # The query returned no rows, so nothing is cached and a re-load would legitimately re-query.
    # Cache what the query would have cached, then ask again: the second load must be a no-op.
    features._by_date = dict.fromkeys(dates, ())
    features.load(dates)
    assert len(connection.statements) == 1


def test_a_second_arm_queries_only_the_dates_the_first_did_not_cover() -> None:
    """A fortnightly arm after a weekly one adds no dates; an uncovered date still queries."""
    connection = _RecordingConnection()
    features = _features_with(connection)
    weekly = [date(2020, 1, day) for day in (6, 13, 20, 27)]
    features._by_date = dict.fromkeys(weekly, ())
    features.load([date(2020, 1, 6), date(2020, 1, 20)])  # a fortnightly subset
    assert connection.statements == []
    features.load([date(2020, 2, 3)])  # a date nothing has covered
    assert len(connection.statements) == 1


def test_an_empty_load_touches_the_lake_at_all() -> None:
    connection = _RecordingConnection()
    _features_with(connection).load([])
    assert connection.statements == []


# ── M12.3: the walk-forward's one enforced rule ──────────────────────────────────────────────────


def test_a_walk_forward_refuses_an_overlapping_split() -> None:
    """An overlap leaks the answer into the choice, so it is refused rather than warned about."""
    from backtest.verdict import run_walk_forward

    with pytest.raises(ValueError, match="close before verification opens"):
        run_walk_forward(
            selection=(date(2016, 9, 1), date(2021, 12, 31)),
            verification=(date(2021, 9, 1), date(2026, 8, 31)),
        )


def test_the_verdict_names_its_choice_before_any_verification_figure() -> None:
    """The frozen name must appear in the report above the verification table, not after it."""
    from backtest.verdict import WalkForward, render_verdict

    selection = SweepResult(
        rows=[
            _row("winner", xirr="0.30", drawdown="0.20"),
            _row("other", xirr="0.10", drawdown="0.40"),
        ],
        start=date(2016, 9, 1),
        terminal=date(2021, 8, 31),
    )
    verification = SweepResult(
        rows=[
            _row("winner", xirr="0.05", drawdown="0.30"),
            _row("other", xirr="0.20", drawdown="0.25"),
        ],
        start=date(2021, 9, 1),
        terminal=date(2026, 8, 31),
        profile=_PROFILE,
    )
    walk = WalkForward(selection=selection, verification=verification, selected="winner")
    report = render_verdict(
        walk,
        floors=[LOW_FLOOR],
        selection_window=(date(2016, 9, 1), date(2021, 8, 31)),
        verification_window=(date(2021, 9, 1), date(2026, 8, 31)),
    )
    assert report.index("**Chosen: winner**") < report.index("Selection rank against verification")
    # The benchmark caveat is read from the rows' source: published TRI, not a stale proxy line.
    assert "price-return benchmark proxy" not in report
    assert "the benchmark series (published" in report
    # The decay is visible: chosen first on selection, second on verification.
    assert walk.rank_of(selection, "winner", LOW_FLOOR) == 1
    assert walk.rank_of(verification, "winner", LOW_FLOOR) == 2


def test_the_verdict_says_no_when_no_arm_cleared_the_bar() -> None:
    """A bar not reached is reported as not reached — and scoped to the windows actually swept.

    The first edition of this renderer said "no arm cleared 25 % on any window at any liquidity
    floor" while holding only the two walk-forward windows, which was false the moment a third
    window existed: the six-year sweep clears it. A verdict may not generalise past its own
    evidence, so the sentence now names the windows it measured.
    """
    from backtest.verdict import WalkForward, render_verdict

    thin = SweepResult(rows=[_row("modest", xirr="0.14", drawdown="0.20")], profile=_PROFILE)
    walk = WalkForward(selection=thin, verification=thin, selected="modest")
    report = render_verdict(
        walk,
        floors=[LOW_FLOOR],
        selection_window=(date(2016, 9, 1), date(2021, 8, 31)),
        verification_window=(date(2021, 9, 1), date(2026, 8, 31)),
    )
    assert "**Answer: not on the windows this run measured**" in report
    # And it must name the windows it actually held, rather than generalising past them.
    assert "selection" in report and "verification" in report


def test_the_verdict_attaches_window_floor_and_drawdown_when_the_bar_is_cleared() -> None:
    from backtest.verdict import WalkForward, render_verdict

    rich = SweepResult(rows=[_row("strong", xirr="0.31", drawdown="0.28")], profile=_PROFILE)
    walk = WalkForward(selection=rich, verification=rich, selected="strong")
    report = render_verdict(
        walk,
        floors=[LOW_FLOOR],
        selection_window=(date(2016, 9, 1), date(2021, 8, 31)),
        verification_window=(date(2021, 9, 1), date(2026, 8, 31)),
    )
    assert "**Answer: the bar was cleared**" in report
    assert "31.00%" in report and "28.00%" in report and "₹1 crore/day" in report


def test_a_withheld_after_tax_xirr_renders_as_n_a_with_its_reason() -> None:
    """No solvable after-tax rate reads as n/a and why — never a crash, never the pre-tax rate."""
    reason = "no after-tax XIRR (realised gains): XIRR did not converge"
    withheld = SimpleNamespace(
        after_tax_xirr_realised=None,
        realised_xirr_error=reason,
        after_tax_xirr_liquidated=None,
        liquidation_error="no after-tax XIRR (deemed liquidation): x",
        total_tax=Decimal("250000"),
    )
    row = replace(_row("Withheld", xirr="0.15", drawdown="0.30"), after_tax=cast(Any, withheld))
    realised, liquidated, tax = tax_cells(row)
    assert realised == f"n/a ({reason})"
    assert liquidated.startswith("n/a (no after-tax XIRR (deemed liquidation)")
    assert "15.00" not in realised
    assert tax.endswith("/ n/a")
