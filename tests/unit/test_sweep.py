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

from collections.abc import Sequence
from dataclasses import fields
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest

from backtest.policies.swing_composite import SwingCompositeParameters
from backtest.run import BacktestError, _SwingFeatures
from backtest.sweep import (
    ARMS,
    DURATION_ARMS,
    HIGH_FLOOR,
    LOW_FLOOR,
    Arm,
    SweepResult,
    SweepRow,
    Window,
    WindowRole,
    _run_arm,
    independent_passes,
    render_sweep_report,
    run_multi_window_sweep,
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
        mean_universe=Decimal("908.9"),
        # The replay result a row reaches through for turnover and for the determinism digest.
        result=SimpleNamespace(journal=(), digest=lambda: "0" * 64),
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


# ── M12.3: the duration grid on M10.7 itself ─────────────────────────────────────────────────────
#
# The grid is a comparison, not a search, and that is a structural claim rather than a stylistic
# one: every arm must score on exactly M10.7's three legs (or the whole table stops being a
# duration measurement and becomes a signal measurement), and every arm must differ from a *named*
# reference by exactly the fields its note claims. Both are pinned below against a table declared
# here, so a silent edit to a weight or a knob fails a test rather than a reading of the report.

#: What each duration arm changes against its named reference: field -> (from, to). Declared here
#: rather than derived, so the test disagrees with the arm list when either one moves.
_EXPECTED_DURATION_CHANGES: dict[str, dict[str, tuple[object, object]]] = {
    "M10.7 @ fortnightly / 21-session hold": {"max_hold_sessions": (63, 21)},
    "M10.7 @ fortnightly / 42-session hold": {"max_hold_sessions": (63, 42)},
    "M10.7 @ weekly / 21-session hold": {"rebalance_interval_sessions": (10, 5)},
    "M10.7 @ weekly / 10-session hold": {"max_hold_sessions": (21, 10)},
    # The min-hold floor is *not* forced down by a 10-session re-underwrite — 5 < 10 is legal, so
    # moving it too would have priced two knobs in one row. It gets its own arm instead.
    "M10.7 @ weekly / 10-session hold, 2-session floor": {"min_hold_sessions": (5, 2)},
    "M10.7 @ monthly / 63-session hold": {"rebalance_interval_sessions": (10, 21)},
    "M10.7 @ monthly / 126-session hold": {"max_hold_sessions": (63, 126)},
    "M10.7 @ quarterly / 126-session hold": {"rebalance_interval_sessions": (21, 63)},
    "M10.7, band 1.5x top_n": {"sell_band": (60, 30)},
    "M10.7, band 5x top_n": {"sell_band": (60, 100)},
}


def _swing_diff(left: Any, right: Any) -> dict[str, tuple[object, object]]:
    """Every ``SwingCompositeParameters`` field on which two arms disagree."""
    return {
        field.name: (getattr(left, field.name), getattr(right, field.name))
        for field in fields(left)
        if getattr(left, field.name) != getattr(right, field.name)
    }


def test_the_duration_grid_covers_the_cadences_and_holds_the_owner_asked_for() -> None:
    """weekly/10, weekly/21, fortnightly/21, fortnightly/42, fortnightly/63, monthly/63,
    monthly/126, quarterly/126 — plus the band axis at the default cadence."""
    grid = {
        (arm.swing.rebalance_interval_sessions, arm.swing.max_hold_sessions)
        for arm in DURATION_ARMS
        if arm.swing is not None and arm.swing.sell_band == 60
    }
    assert grid == {(5, 10), (5, 21), (10, 21), (10, 42), (10, 63), (21, 63), (21, 126), (63, 126)}
    bands = {arm.swing.sell_band for arm in DURATION_ARMS if arm.swing is not None}
    assert bands == {30, 60, 100}  # 1.5x, 3x (the default) and 5x of a top-20 basket


def test_every_duration_arm_is_distinct() -> None:
    """A grid with two identical cells is a table that prices the same change twice."""
    labels = [arm.label for arm in DURATION_ARMS]
    assert len(labels) == len(set(labels))
    configs = [arm.swing for arm in DURATION_ARMS if arm.swing is not None]
    assert len(configs) == len({repr(config) for config in configs})


def test_every_duration_arm_differs_from_its_reference_by_exactly_the_stated_change() -> None:
    """The convention M12.2 set: a row is readable only as the price of one named change."""
    by_label = {arm.label: arm for arm in DURATION_ARMS}
    for arm in DURATION_ARMS:
        if arm.label not in _EXPECTED_DURATION_CHANGES:
            continue
        reference = by_label[arm.reference]
        assert reference.swing is not None and arm.swing is not None, arm.label
        diff = _swing_diff(reference.swing, arm.swing)
        assert diff == _EXPECTED_DURATION_CHANGES[arm.label], arm.label
        # "One stated change" means one, not "one plus whatever the note calls forced".
        assert len(diff) == 1, f"{arm.label} moves {sorted(diff)} — that is a confounded row"


def test_every_duration_change_is_holding_period_machinery_and_nothing_else() -> None:
    """A duration axis that quietly moved a weight would be measuring the signal instead."""
    machinery = {
        "rebalance_interval_sessions",
        "max_hold_sessions",
        "min_hold_sessions",
        "sell_band",
    }
    for changes in _EXPECTED_DURATION_CHANGES.values():
        assert set(changes) <= machinery


def test_every_duration_arm_scores_on_exactly_the_m10_7_legs() -> None:
    """The grid is on M10.7 *itself*: its three legs at weight 1, every M12.1 leg at zero."""
    for arm in DURATION_ARMS:
        if arm.swing is None:
            continue
        swing = arm.swing
        assert (swing.weight_high, swing.weight_delivery, swing.weight_momentum) == (
            Decimal("1"),
            Decimal("1"),
            Decimal("1"),
        ), arm.label
        for leg in (
            "weight_return_5",
            "weight_momentum_1m",
            "weight_delivery_trend",
            "weight_turnover_expansion",
            "weight_ma_proximity",
            "weight_volatility",
        ):
            assert getattr(swing, leg) == Decimal("0"), f"{arm.label}: {leg}"
        # Everything else that is not duration machinery stays at the M10.7 default.
        assert swing.top_n == 20 and swing.trailing_stop == Decimal("0.25"), arm.label
        assert swing.regime_filter is False, arm.label
        assert swing.exclude_vol_fraction == Decimal("0.10"), arm.label


def test_the_duration_grid_keeps_the_reference_and_both_baselines() -> None:
    """Every row is priced against something, and against the policies the repo already had."""
    labels = {arm.label for arm in DURATION_ARMS}
    assert "Swing composite (M10.7)" in labels
    assert {"Naive momentum (M4.10)", "Momentum v2, all on (M9.5)"} <= labels
    assert all(arm.reference == "—" or arm.reference in labels for arm in DURATION_ARMS)


def test_the_default_duration_arm_is_untouched_m10_7() -> None:
    """The reference row must still reproduce M10.7's measurement — nothing here perturbs it."""
    reference = next(arm for arm in DURATION_ARMS if arm.label == "Swing composite (M10.7)")
    assert reference.swing == SwingCompositeParameters()


def test_the_m12_2_arm_list_is_not_disturbed_by_the_duration_grid() -> None:
    """M12.2's sweep is a signal comparison with a campaign running against it; it does not move."""
    assert len(ARMS) == 23
    assert not any(arm.family == "duration" for arm in ARMS)


# ── M12.3: many windows, one lake pass each ──────────────────────────────────────────────────────
#
# The acceptance criterion M12.2 set for one window generalises to the criterion this task has to
# meet for several: a *second shared* pass over a window is the defect. The stubs below make that
# countable without a lake.
#
# **What these tests do not prove.** They monkeypatch `_run_arm`, so every read a real arm would
# make disappears behind the patch. In particular the two momentum baselines open their own
# `_L1Reader` and `QueryService` inside `run_naive_momentum` / `run_momentum_v2` and re-walk the
# window — a window costs one shared pass *plus* `independent_passes(arms, floors)` traversals, and
# no amount of counting `open_swing_lake` will see them. `test_the_baselines_do_not_share_the_lake`
# below counts that separately, and it is the reason the report says "one shared pass, plus each
# baseline's own" rather than "one pass".


class _CountingFeatures:
    """Records every ``load`` so a test can count the windowed passes rather than time them."""

    def __init__(self) -> None:
        self.loads: list[tuple[date, ...]] = []

    def load(self, dates: Sequence[date]) -> None:
        self.loads.append(tuple(dates))


class _CountingLake:
    """A ``SwingLake`` stand-in carrying only what ``run_sweep`` reads off one."""

    def __init__(self, sessions: Sequence[date]) -> None:
        self.sessions = tuple(sessions)
        self.features = _CountingFeatures()
        self.closed = False

    @property
    def first_session(self) -> date:
        return self.sessions[0]

    @property
    def terminal(self) -> date:
        return self.sessions[-1]

    def close(self) -> None:
        self.closed = True


def _install_stub_lake(
    monkeypatch: pytest.MonkeyPatch, *, failing: str = ""
) -> tuple[list[_CountingLake], list[tuple[date, date]]]:
    """Replace the lake and the replay with counters; return the lakes opened and the windows."""
    lakes: list[_CountingLake] = []
    opened: list[tuple[date, date]] = []

    def fake_open(**kwargs: Any) -> _CountingLake:
        start, end = kwargs["start"], kwargs["end"]
        opened.append((start, end))
        span = (end - start).days
        lake = _CountingLake([start + timedelta(days=offset) for offset in range(0, span, 7)])
        lakes.append(lake)
        return lake

    def fake_run_arm(arm: Arm, **kwargs: Any) -> Any:
        if failing and failing in arm.label:
            raise BacktestError(f"{arm.label} could not run")
        # The XIRR is a function of the window, so a report that averaged two windows would show a
        # figure neither window produced.
        start: date = kwargs["start"]
        return _stub_run(xirr=f"0.{start.year - 2000:02d}", drawdown="0.25")

    monkeypatch.setattr("backtest.sweep.open_swing_lake", fake_open)
    monkeypatch.setattr("backtest.sweep._run_arm", fake_run_arm)
    monkeypatch.setattr("backtest.sweep._holding_periods", lambda journal: (40, 30, 12))
    return lakes, opened


_WINDOWS = (
    Window(label="A", start=date(2018, 1, 1), end=date(2019, 12, 31)),
    Window(label="B", start=date(2020, 1, 1), end=date(2021, 12, 31)),
    Window(label="C", start=date(2022, 1, 1), end=date(2023, 12, 31), role=WindowRole.SELECTION),
    Window(label="D", start=date(2024, 1, 1), end=date(2025, 12, 31), role=WindowRole.VERIFICATION),
)


def test_each_window_gets_exactly_one_lake_pass_no_matter_how_many_arms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared-pass invariant: N windows cost N shared passes, whatever the arm count.

    This counts `open_swing_lake` only, which is the *shared* pass. It says nothing about the
    baselines' own traversals — see the module comment above and the baseline test below.
    """
    lakes, opened = _install_stub_lake(monkeypatch)
    sweep = run_multi_window_sweep(
        windows=_WINDOWS, arms=DURATION_ARMS, floors=(LOW_FLOOR, HIGH_FLOOR)
    )
    assert len(opened) == len(_WINDOWS)  # one open_swing_lake per window, not per arm-run
    assert len(lakes) == len(_WINDOWS)
    for lake in lakes:
        assert len(lake.features.loads) == 1, "a second windowed pass is the defect"
        assert lake.closed, "every window's lake is released before the next opens"
    # ...and the arm-runs really did all happen against those four passes.
    total = sum(len(entry.result.rows) for entry in sweep.windows)
    assert total == len(_WINDOWS) * len(DURATION_ARMS) * 2


def test_the_one_pass_loads_the_union_of_every_cadence(monkeypatch: pytest.MonkeyPatch) -> None:
    """A weekly arm and a quarterly arm share one query, not two.

    The expectation is built from the *cadences* rather than by re-running the code's own slicing
    expression, so this checks the union rather than detecting that the expression changed.
    """
    lakes, _ = _install_stub_lake(monkeypatch)
    # Three arms whose cadences are known here, not read back off the module: 5, 21 and 63.
    arms = tuple(
        arm
        for arm in DURATION_ARMS
        if arm.label
        in {
            "M10.7 @ weekly / 21-session hold",
            "M10.7 @ monthly / 63-session hold",
            "M10.7 @ quarterly / 126-session hold",
        }
    )
    assert {a.swing.rebalance_interval_sessions for a in arms if a.swing} == {5, 21, 63}
    run_multi_window_sweep(windows=_WINDOWS[:1], arms=arms, floors=(LOW_FLOOR,))
    (loaded,) = lakes[0].features.loads
    sessions = lakes[0].sessions

    # Stated as index arithmetic rather than by re-running the code's own `sessions[::step]`:
    # a session is a decision date iff its index is a multiple of some arm's cadence. 63 is a
    # multiple of 21, so the quarterly arm adds nothing the monthly one had not already asked for
    # — which is the redundancy that makes a union cheaper than three separate loads.
    expected = {
        session
        for index, session in enumerate(sessions)
        if index % 5 == 0 or index % 21 == 0 or index % 63 == 0
    }
    assert set(loaded) == expected
    assert set(sessions[::63]) <= set(sessions[::21])
    # A session no cadence lands on is not loaded: the union is not "the whole window".
    assert sessions[1] not in set(loaded)
    assert len(loaded) < len(sessions)
    assert list(loaded) == sorted(loaded)


def test_every_window_runs_every_arm_on_both_liquidity_floors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both floors, every arm, every window — the discovery number and the reachable one."""
    _install_stub_lake(monkeypatch)
    sweep = run_multi_window_sweep(
        windows=_WINDOWS, arms=DURATION_ARMS, floors=(LOW_FLOOR, HIGH_FLOOR)
    )
    for entry in sweep.windows:
        for floor in (LOW_FLOOR, HIGH_FLOOR):
            ranked = entry.result.ranked(floor)
            assert len(ranked) == len(DURATION_ARMS)
            assert {row.arm.label for row in ranked} == {arm.label for arm in DURATION_ARMS}


def test_a_failed_arm_keeps_its_row_on_every_window_and_every_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An arm that raises is reported as a failed arm — never dropped from a window's table."""
    _install_stub_lake(monkeypatch, failing="quarterly")
    sweep = run_multi_window_sweep(
        windows=_WINDOWS, arms=DURATION_ARMS, floors=(LOW_FLOOR, HIGH_FLOOR)
    )
    for entry in sweep.windows:
        for floor in (LOW_FLOOR, HIGH_FLOOR):
            ranked = entry.result.ranked(floor)
            assert len(ranked) == len(DURATION_ARMS), "the table did not shrink"
            broken = [row for row in ranked if not row.ok]
            assert len(broken) == 1
            assert "quarterly" in broken[0].arm.label
            assert broken[0].error is not None and "could not run" in broken[0].error


def test_no_figure_is_pooled_across_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each window keeps its own rows; there is no accessor that merges two into one ranking."""
    _install_stub_lake(monkeypatch)
    sweep = run_multi_window_sweep(windows=_WINDOWS, arms=DURATION_ARMS[:1], floors=(LOW_FLOOR,))
    xirrs = {entry.window.label: entry.result.ranked(LOW_FLOOR)[0].xirr for entry in sweep.windows}
    assert len(set(xirrs.values())) == len(_WINDOWS), "the stub made every window differ"
    # Every window's result is its own object, and none of them carries another's rows.
    for entry in sweep.windows:
        others = [e for e in sweep.windows if e is not entry]
        assert all(entry.result is not other.result for other in others)
        assert len(entry.result.rows) == 1

    # The rule stated positively: no accessor on the sweep returns a value derived from more than
    # one window. Anything that did — a pooled ranking, a mean XIRR — would have to read rows from
    # two results, so every public accessor is checked to return only its own window's figures.
    mean = sum(xirrs.values(), start=Decimal("0")) / Decimal(len(xirrs))
    for entry in sweep.windows:
        assert entry.result.ranked(LOW_FLOOR)[0].xirr == xirrs[entry.window.label]
        assert entry.result.ranked(LOW_FLOOR)[0].xirr != mean
        assert sweep.result_for(entry.window.label) is entry.result
    # And a label that names no window raises rather than quietly returning something blended.
    with pytest.raises(KeyError, match="no window called"):
        sweep.result_for("every window")


def test_the_selection_winner_is_frozen_before_verification_is_swept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The name on record is the one the selection window alone could produce."""
    _, opened = _install_stub_lake(monkeypatch)
    sweep = run_multi_window_sweep(windows=_WINDOWS, arms=DURATION_ARMS, floors=(LOW_FLOOR,))
    selection = sweep.with_role(WindowRole.SELECTION)
    assert selection is not None
    assert sweep.selected == selection.result.ranked(LOW_FLOOR)[0].arm.label
    # The selection window's lake was opened before the verification window's, so the choice could
    # not have been informed by figures that did not exist yet.
    assert opened.index((date(2022, 1, 1), date(2023, 12, 31))) < opened.index(
        (date(2024, 1, 1), date(2025, 12, 31))
    )


@pytest.mark.parametrize(
    ("windows", "message"),
    [
        ((), "at least one window"),
        (
            (
                Window(label="dup", start=date(2018, 1, 1), end=date(2019, 1, 1)),
                Window(label="dup", start=date(2020, 1, 1), end=date(2021, 1, 1)),
            ),
            "unique",
        ),
        (
            (
                Window(
                    label="sel",
                    start=date(2016, 9, 1),
                    end=date(2021, 12, 31),
                    role=WindowRole.SELECTION,
                ),
                Window(
                    label="ver",
                    start=date(2021, 9, 1),
                    end=date(2026, 8, 31),
                    role=WindowRole.VERIFICATION,
                ),
            ),
            "close before verification opens",
        ),
        (
            (
                Window(
                    label="ver",
                    start=date(2021, 9, 1),
                    end=date(2026, 8, 31),
                    role=WindowRole.VERIFICATION,
                ),
                Window(
                    label="sel",
                    start=date(2016, 9, 1),
                    end=date(2021, 8, 31),
                    role=WindowRole.SELECTION,
                ),
            ),
            "swept before the verification window",
        ),
        (
            (
                Window(
                    label="ver",
                    start=date(2021, 9, 1),
                    end=date(2026, 8, 31),
                    role=WindowRole.VERIFICATION,
                ),
            ),
            "verifies nothing",
        ),
    ],
)
def test_a_window_list_that_cannot_be_read_honestly_is_refused(
    windows: tuple[Window, ...], message: str
) -> None:
    """An overlap or a reordering leaks the answer into the choice, so it raises not warns."""
    with pytest.raises(ValueError, match=message):
        run_multi_window_sweep(windows=windows, arms=DURATION_ARMS[:1])


def test_a_window_states_its_own_span() -> None:
    with pytest.raises(ValueError, match="is before"):
        Window(label="backwards", start=date(2021, 1, 1), end=date(2020, 1, 1))
    with pytest.raises(ValueError, match="must be labelled"):
        Window(label="  ", start=date(2020, 1, 1), end=date(2021, 1, 1))


# ── M12.3: the shared pass covers the swing arms, and only the swing arms ────────────────────────
#
# The tests above monkeypatch `_run_arm`, so they can only ever count the *shared* pass. This one
# patches one level lower — the three policy entry points `_run_arm` dispatches to — so it sees
# which arm-runs are handed the window's lake and which go and build their own. That distinction is
# the difference between the report's old claim ("one lake pass per window") and the true one.


def test_the_baselines_do_not_share_the_lake(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swing arms take the shared lake; each momentum baseline opens its own and re-walks."""
    shared: list[str] = []
    independent: list[str] = []

    def fake_swing(**kwargs: Any) -> Any:
        assert kwargs["lake"] is not None, "a swing arm must be given the window's shared lake"
        shared.append(str(kwargs["parameters"].rebalance_interval_sessions))
        return _stub_run(xirr="0.18", drawdown="0.25")

    def fake_naive(**kwargs: Any) -> Any:
        # The signature takes no lake at all — that is the whole finding.
        assert "lake" not in kwargs
        independent.append("naive")
        return _stub_run(xirr="0.11", drawdown="0.25")

    def fake_v2(**kwargs: Any) -> Any:
        assert "lake" not in kwargs
        independent.append("v2")
        return _stub_run(xirr="0.14", drawdown="0.25")

    lakes, _ = _install_stub_lake(monkeypatch)
    monkeypatch.setattr("backtest.sweep._run_arm", _run_arm)  # restore the real dispatcher
    monkeypatch.setattr("backtest.sweep.run_swing_composite", fake_swing)
    monkeypatch.setattr("backtest.sweep.run_naive_momentum", fake_naive)
    monkeypatch.setattr("backtest.sweep.run_momentum_v2", fake_v2)

    floors = (LOW_FLOOR, HIGH_FLOOR)
    run_multi_window_sweep(windows=_WINDOWS[:1], arms=DURATION_ARMS, floors=floors)

    swing_arms = [arm for arm in DURATION_ARMS if arm.swing is not None]
    baselines = [arm for arm in DURATION_ARMS if arm.swing is None]
    assert len(shared) == len(swing_arms) * len(floors)
    assert len(independent) == len(baselines) * len(floors)
    # One shared pass for the window, and one traversal per baseline run on top of it.
    assert len(lakes[0].features.loads) == 1
    assert len(independent) == independent_passes(DURATION_ARMS, floors)
    assert independent_passes(DURATION_ARMS, floors) == 4  # 2 baselines x 2 floors


def test_independent_passes_counts_only_the_arms_that_build_their_own_lake() -> None:
    """The number the report prints, so it can never be a hardcoded claim again."""
    swing_only = tuple(arm for arm in DURATION_ARMS if arm.swing is not None)
    assert independent_passes(swing_only, (LOW_FLOOR, HIGH_FLOOR)) == 0
    assert independent_passes(DURATION_ARMS, (LOW_FLOOR,)) == 2
    assert independent_passes(DURATION_ARMS, (LOW_FLOOR, HIGH_FLOOR)) == 4
    assert independent_passes((), (LOW_FLOOR,)) == 0
