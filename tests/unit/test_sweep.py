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

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest

from backtest.run import _SwingFeatures
from backtest.sweep import (
    ARMS,
    HIGH_FLOOR,
    LOW_FLOOR,
    Arm,
    SweepResult,
    SweepRow,
    render_sweep_report,
)

_SESSION = date(2020, 1, 1)


def _stub_run(*, xirr: str, drawdown: str, excess: str = "0.02") -> Any:
    """A stand-in for ``BacktestResult`` carrying only what a row reads off it."""
    return SimpleNamespace(
        comparison=SimpleNamespace(
            portfolio_xirr=Decimal(xirr),
            benchmark_xirr=Decimal("0.0856"),
            excess_over_benchmark=Decimal(excess),
        ),
        max_drawdown=Decimal(drawdown),
        total_charges=Decimal("100000"),
        benchmark_index_name="NIFTY-TRI L1 proxy",
    )


def _row(label: str, *, xirr: str, drawdown: str, floor: Decimal = LOW_FLOOR) -> SweepRow:
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
        run=cast(Any, _stub_run(xirr=xirr, drawdown=drawdown)),
        round_trips=10,
        median_hold_days=30,
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
    result = SweepResult(rows=[broken, good])
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
    )
    report = render_sweep_report(result, floors=[LOW_FLOOR, HIGH_FLOOR])
    assert "₹1 crore/day" in report
    assert "₹10 crore/day" in report
    assert "What each arm changed" in report
    assert "cannot be asked to prove" in report


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
    )
    walk = WalkForward(selection=selection, verification=verification, selected="winner")
    report = render_verdict(
        walk,
        floors=[LOW_FLOOR],
        selection_window=(date(2016, 9, 1), date(2021, 8, 31)),
        verification_window=(date(2021, 9, 1), date(2026, 8, 31)),
    )
    assert report.index("**Chosen: winner**") < report.index("Selection rank against verification")
    # The decay is visible: chosen first on selection, second on verification.
    assert walk.rank_of(selection, "winner", LOW_FLOOR) == 1
    assert walk.rank_of(verification, "winner", LOW_FLOOR) == 2


def test_the_verdict_says_no_when_no_arm_cleared_the_bar() -> None:
    """A bar that was not reached is reported as not reached, never as the best number available."""
    from backtest.verdict import WalkForward, render_verdict

    thin = SweepResult(rows=[_row("modest", xirr="0.14", drawdown="0.20")])
    walk = WalkForward(selection=thin, verification=thin, selected="modest")
    report = render_verdict(
        walk,
        floors=[LOW_FLOOR],
        selection_window=(date(2016, 9, 1), date(2021, 8, 31)),
        verification_window=(date(2021, 9, 1), date(2026, 8, 31)),
    )
    assert "**Answer: no.**" in report


def test_the_verdict_attaches_window_floor_and_drawdown_when_the_bar_is_cleared() -> None:
    from backtest.verdict import WalkForward, render_verdict

    rich = SweepResult(rows=[_row("strong", xirr="0.31", drawdown="0.28")])
    walk = WalkForward(selection=rich, verification=rich, selected="strong")
    report = render_verdict(
        walk,
        floors=[LOW_FLOOR],
        selection_window=(date(2016, 9, 1), date(2021, 8, 31)),
        verification_window=(date(2021, 9, 1), date(2026, 8, 31)),
    )
    assert "**Answer: the bar was cleared**" in report
    assert "31.00%" in report and "28.00%" in report and "₹1 crore/day" in report
