"""M16.0 report: the A6 blend, the selection floor, Step 2's directions and the deflated Sharpe."""

from __future__ import annotations

import json
import random
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import backtest.m16_report as report_module
from backtest.m12_rerun import REGIME_DAILY_SET, RERUN_ARMS, WINDOWS
from backtest.m16_arms import A2_INDUSTRY_GATE, A6_BLEND, A7_ARM, A7_LOW_VOL, M10_7_BASELINE
from backtest.m16_report import (
    DILUTION_THRESHOLD,
    MARKER,
    PRIMARY_FLOOR,
    TRIAL_DIRS,
    TRIALS,
    TRIALS_ON_RECORD,
    A2Coverage,
    RunFacts,
    TrialSharpe,
    blend,
    collect,
    config_key,
    criteria,
    decision,
    decisive,
    load_a2_coverage,
    main,
    merge_rows,
    render,
    select,
    trial_sharpes,
    with_blends,
)
from backtest.nav import daily_returns
from backtest.policies.swing_composite import SwingCompositeParameters
from backtest.run import UniverseParameters
from backtest.sharpe import deflated_sharpe_ratio, sharpe_stats, sharpe_variance
from backtest.sweep import (
    CAP_TIER_ARMS,
    D13_PAPER_BASELINE,
    HIGH_FLOOR,
    LOW_FLOOR,
    REDEPLOY_ARMS,
    Arm,
    _arm_spec,
)
from backtest.xirr import Cashflow, xirr

D13 = D13_PAPER_BASELINE.label
M107 = M10_7_BASELINE.label
FLOOR = "turnover_floor"
N500 = "nifty500"
VER = "wf-verification"
SEL = "wf-selection"
_START = date(2021, 9, 1)


def _path(
    seed: int, drift: float, vol: float = 0.01, n: int = 300
) -> tuple[tuple[date, Decimal], ...]:
    rng = random.Random(seed)
    nav = Decimal("1000000")
    points = [(_START, nav)]
    day = _START
    for _ in range(n):
        day += timedelta(days=1)
        nav = (nav * Decimal(repr(1 + rng.gauss(drift, vol)))).quantize(Decimal("0.01"))
        points.append((day, nav))
    return tuple(points)


def _facts(
    label: str,
    cell: tuple[str, str, Decimal],
    xirr_: str,
    dd: str,
    *,
    nav: tuple[tuple[date, Decimal], ...] = (),
    refusals: int = 0,
) -> RunFacts:
    return RunFacts(
        window=cell[1],
        universe=cell[0],
        floor=cell[2],
        label=label,
        key=label,
        digest=label,
        xirr=Decimal(xirr_),
        max_drawdown=Decimal(dd),
        excess=Decimal("0.01"),
        charges=Decimal("1000"),
        trades=10,
        final_nav=nav[-1][1] if nav else Decimal("2000000"),
        opening_cash=Decimal("1000000"),
        terminal=nav[-1][0] if nav else date(2026, 8, 31),
        floor_refusals=refusals,
        rail_blocks={"MIN_HOLDINGS": refusals} if refusals else {},
        nav=nav,
    )


# ── the trial count ──────────────────────────────────────────────────────────────────────────────


def test_trial_count_is_the_preregistered_75() -> None:
    assert len(TRIALS_ON_RECORD) == 68
    assert len(set(TRIALS_ON_RECORD)) == 68
    assert TRIALS == 75


def test_appendix_rows_from_saved_runs_carry_the_sweeps_own_labels() -> None:
    # Rows 1-47 are matched to saved runs by label, so each must be a real arm's label (or the
    # retired top-5 arm, which ran before it was removed from the sweep).
    known = {a.label for a in (*RERUN_ARMS, *REGIME_DAILY_SET, *CAP_TIER_ARMS, *REDEPLOY_ARMS)}
    assert set(TRIALS_ON_RECORD[:47]) - known == {"Short composite, top-5"}


def test_config_key_ignores_a_field_added_later_at_its_default() -> None:
    spec = _arm_spec(
        M10_7_BASELINE,
        start=WINDOWS[VER][0],
        end=WINDOWS[VER][1],
        universe=UniverseParameters.for_universe(FLOOR, median_turnover_floor=HIGH_FLOOR),
        opening_cash=Decimal("1000000"),
        adjusted=True,
    )
    older = dict(
        spec, parameters=spec["parameters"].replace("weight_volatility=Decimal('0'), ", "")
    )
    assert older["parameters"] != spec["parameters"]
    assert config_key(older) == config_key(spec)
    changed = dict(spec, parameters=repr(SwingCompositeParameters(top_n=10, sell_band=30)))
    assert config_key(changed) != config_key(spec)


# ── A6 ───────────────────────────────────────────────────────────────────────────────────────────


def test_blend_is_half_of_each_path_with_gaps_carried() -> None:
    cell = (FLOOR, VER, HIGH_FLOOR)
    d0, d1, d2 = date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)
    a = _facts(
        D13,
        cell,
        "0.2",
        "0.1",
        nav=((d0, Decimal("100")), (d1, Decimal("120")), (d2, Decimal("90"))),
    )
    b = _facts(M107, cell, "0.1", "0.1", nav=((d0, Decimal("100")), (d2, Decimal("130"))))
    a = replace(a, opening_cash=Decimal("100"), final_nav=Decimal("90"))
    b = replace(b, opening_cash=Decimal("100"), final_nav=Decimal("130"), terminal=a.terminal)
    mixed = blend(a, b)
    # b has no point on d1: its d0 value (100) is carried, so the blend is (120 + 100) / 2.
    assert mixed.nav == ((d0, Decimal("100")), (d1, Decimal("110")), (d2, Decimal("110")))
    assert mixed.max_drawdown == Decimal("0")  # 100 -> 110 -> 110 never falls
    assert mixed.final_nav == Decimal("110")
    expected = xirr([Cashflow(d0, Decimal("-100")), Cashflow(a.terminal, Decimal("110"))])
    assert mixed.xirr == expected
    assert mixed.label == A6_BLEND


def test_blend_drawdown_comes_from_the_blended_path_not_the_halves() -> None:
    cell = (FLOOR, VER, HIGH_FLOOR)
    days = [date(2024, 1, 1), date(2024, 7, 1), date(2025, 1, 1)]
    a = _facts(
        D13, cell, "0", "0.5", nav=tuple(zip(days, map(Decimal, ("100", "50", "110")), strict=True))
    )
    b = _facts(
        M107,
        cell,
        "0",
        "0.5",
        nav=tuple(zip(days, map(Decimal, ("100", "150", "110")), strict=True)),
    )
    assert blend(a, b).max_drawdown == Decimal("0")  # the halves' falls cancel exactly


def test_blend_of_a_run_with_itself_reproduces_the_run() -> None:
    nav = _path(1, 0.0008)
    run = _facts(D13, (FLOOR, VER, HIGH_FLOOR), "0", "0", nav=nav)
    single = xirr([Cashflow(nav[0][0], -run.opening_cash), Cashflow(run.terminal, run.final_nav)])
    twin = blend(run, run)
    assert twin.xirr == single
    assert twin.nav == nav
    assert twin.trades == 2 * run.trades and twin.charges == run.charges


def test_blend_refuses_runs_of_different_cells() -> None:
    a = _facts(D13, (FLOOR, VER, HIGH_FLOOR), "0", "0", nav=_path(1, 0))
    b = _facts(M107, (FLOOR, VER, LOW_FLOOR), "0", "0", nav=_path(2, 0))
    with pytest.raises(ValueError, match="cannot blend"):
        blend(a, b)


def test_a6_is_added_only_where_both_halves_ran() -> None:
    nav = _path(1, 0.001)
    facts = [
        _facts(D13, (FLOOR, VER, HIGH_FLOOR), "0.2", "0.1", nav=nav),
        _facts(M107, (FLOOR, VER, HIGH_FLOOR), "0.2", "0.1", nav=nav),
        _facts(D13, (FLOOR, VER, LOW_FLOOR), "0.2", "0.1", nav=nav),
    ]
    blends = [f for f in with_blends(facts) if f.label == A6_BLEND]
    assert [f.cell for f in blends] == [(FLOOR, VER, HIGH_FLOOR)]


def test_blend_drawdown_is_measured_from_the_peak() -> None:
    # The blend runs 100 -> 75 -> 100: a fall of 25 from a peak of 100 is 25%, not 25/75.
    cell = (FLOOR, VER, HIGH_FLOOR)
    days = [date(2024, 1, 1), date(2024, 7, 1), date(2025, 1, 1)]
    a = _facts(
        D13, cell, "0", "0.5", nav=tuple(zip(days, map(Decimal, ("100", "50", "110")), strict=True))
    )
    b = _facts(
        M107, cell, "0", "0", nav=tuple(zip(days, map(Decimal, ("100", "100", "110")), strict=True))
    )
    assert blend(a, b).max_drawdown == Decimal("0.25")


# ── Step 1: the selection floor ──────────────────────────────────────────────────────────────────


def _only(monkeypatch: pytest.MonkeyPatch, *labels: str) -> None:
    """Narrow Step 1's arm list for a test that does not build all eight selection arms."""
    monkeypatch.setattr(report_module, "SELECTION_LABELS", labels)


def test_the_choice_is_made_at_ten_crore_not_one(monkeypatch: pytest.MonkeyPatch) -> None:
    _only(monkeypatch, D13, A7_LOW_VOL)
    # A7 wins at ₹1 cr, D13 wins at ₹10 cr: the choice must be D13. Choosing on ₹1 cr (M12's
    # floor) — or ranking on the verification window — would pick A7 and fail here.
    assert PRIMARY_FLOOR == HIGH_FLOOR
    facts = [
        _facts(A7_LOW_VOL, (FLOOR, SEL, LOW_FLOOR), "0.30", "0.10"),
        _facts(D13, (FLOOR, SEL, LOW_FLOOR), "0.20", "0.20"),
        _facts(A7_LOW_VOL, (FLOOR, SEL, HIGH_FLOOR), "0.10", "0.20"),
        _facts(D13, (FLOOR, SEL, HIGH_FLOOR), "0.20", "0.20"),
        _facts(A7_LOW_VOL, (FLOOR, VER, HIGH_FLOOR), "0.90", "0.01"),
    ]
    assert select(facts)[0].label == D13
    assert select(facts, LOW_FLOOR)[0].label == A7_LOW_VOL
    text = "\n".join(decision(facts, []))
    assert "Choice: **Momentum v2, D13 paper config**" in text
    assert "The choice is D13 itself; nothing changes." in text
    assert "*informational only*" in text


def test_the_choice_ranks_on_return_per_drawdown_best_first() -> None:
    facts = [
        _facts("Swing composite (M10.7)", (FLOOR, SEL, HIGH_FLOOR), "0.30", "0.30"),  # 1.0
        _facts(D13, (FLOOR, SEL, HIGH_FLOOR), "0.20", "0.10"),  # 2.0
        _facts(A7_LOW_VOL, (FLOOR, SEL, HIGH_FLOOR), "0.15", "0.05"),  # 3.0
    ]
    assert [f.label for f in select(facts)] == [A7_LOW_VOL, D13, "Swing composite (M10.7)"]


def test_a_ratio_tie_goes_to_the_smaller_drawdown() -> None:
    facts = [
        _facts(D13, (FLOOR, SEL, HIGH_FLOOR), "0.40", "0.20"),  # 2.0, deeper
        _facts(A7_LOW_VOL, (FLOOR, SEL, HIGH_FLOOR), "0.20", "0.10"),  # 2.0, shallower
    ]
    assert [f.label for f in select(facts)] == [A7_LOW_VOL, D13]


def test_step_1_refuses_when_an_arm_lacks_its_selection_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _only(monkeypatch, D13, A7_LOW_VOL, M107)
    facts = [_facts(D13, (FLOOR, SEL, HIGH_FLOOR), "0.2", "0.2")]
    with pytest.raises(
        ValueError, match=r"Step 1 refuses.*Pure low volatility \(A7\), Swing composite"
    ):
        decision(facts, [])


def test_a_zero_drawdown_has_no_ratio() -> None:
    flat = _facts(D13, (FLOOR, SEL, HIGH_FLOOR), "0.1", "0")
    with pytest.raises(ValueError, match="undefined"):
        _ = flat.ratio
    with pytest.raises(ValueError, match="undefined"):
        select([flat])


def test_a4_and_a5_never_take_part_in_the_choice() -> None:
    from backtest.m16_arms import A4_PROFITABILITY

    facts = [
        _facts(A4_PROFITABILITY, (FLOOR, SEL, HIGH_FLOOR), "0.9", "0.01"),
        _facts(D13, (FLOOR, SEL, HIGH_FLOOR), "0.2", "0.2"),
    ]
    assert [f.label for f in select(facts)] == [D13]


# ── Step 2: directions ───────────────────────────────────────────────────────────────────────────


def _step2_facts(arm_ratio_ten: tuple[str, str], *, dd_gap: str = "0.00") -> list[RunFacts]:
    # Per-session Sharpes of about 0.35 (the arm) and 0.05 (D13) over 1,000 sessions.
    good = _path(7, 0.0035, 0.01, n=1000)
    base = _path(8, 0.0005, 0.01, n=1000)
    x, dd = arm_ratio_ten
    rows = [
        _facts(D13, (FLOOR, VER, LOW_FLOOR), "0.20", "0.20"),
        _facts(A7_LOW_VOL, (FLOOR, VER, LOW_FLOOR), "0.30", "0.20"),
        _facts(D13, (FLOOR, VER, HIGH_FLOOR), "0.20", "0.20", nav=base),
        _facts(A7_LOW_VOL, (FLOOR, VER, HIGH_FLOOR), x, dd, nav=good),
        _facts(D13, ("nifty500", VER, HIGH_FLOOR), "0.20", "0.20"),
        _facts(A7_LOW_VOL, ("nifty500", VER, HIGH_FLOOR), "0.30", "0.20"),
        _facts(D13, (FLOOR, "decade", HIGH_FLOOR), "0.20", "0.20"),
        _facts(
            A7_LOW_VOL,
            (FLOOR, "decade", HIGH_FLOOR),
            "0.20",
            str(Decimal("0.20") + Decimal(dd_gap)),
        ),
    ]
    return rows


def _trials(facts: list[RunFacts]) -> list[TrialSharpe]:
    # Eight earlier trials clustered where this lake's Sharpes sit, plus the campaign's own.
    earlier = [TrialSharpe(f"t{n}", f"t{n}", 0.04 + 0.01 * n) for n in range(8)]
    return [*earlier, *trial_sharpes(facts, commit_time=lambda _: 0)]


def _passed(facts: list[RunFacts]) -> dict[str, bool]:
    return {c.name.split(".")[0]: c.passed for c in criteria(facts, A7_LOW_VOL, _trials(facts))}


def test_a_strictly_better_arm_passes_every_criterion() -> None:
    facts = _step2_facts(("0.30", "0.15"))
    assert all(_passed(facts).values()), criteria(facts, A7_LOW_VOL, _trials(facts))


def test_a_worse_ratio_fails_and_a_tie_passes() -> None:
    worse = _passed(_step2_facts(("0.30", "0.40")))  # 0.75 < D13's 1.0
    assert worse["1b"] is False
    tie = _passed(_step2_facts(("0.30", "0.30")))  # 1.0 == 1.0
    assert tie["1b"] is True


def test_the_bar_is_strictly_above_25_percent() -> None:
    assert _passed(_step2_facts(("0.25", "0.10")))["3"] is False
    assert _passed(_step2_facts(("0.2501", "0.10")))["3"] is True


def test_a_drawdown_more_than_3pp_worse_anywhere_fails() -> None:
    assert _passed(_step2_facts(("0.30", "0.15"), dd_gap="0.035"))["4"] is False
    assert _passed(_step2_facts(("0.30", "0.15"), dd_gap="0.03"))["4"] is True
    # A *better* drawdown is never held against the arm.
    assert _passed(_step2_facts(("0.30", "0.15"), dd_gap="-0.10"))["4"] is True


def test_criterion_1a_reads_the_one_crore_cell() -> None:
    facts = _step2_facts(("0.30", "0.15"))
    low = (FLOOR, VER, LOW_FLOOR)
    worse_at_one = [
        replace(f, max_drawdown=Decimal("0.40")) if f.label == A7_LOW_VOL and f.cell == low else f
        for f in facts
    ]
    passed = _passed(worse_at_one)
    assert passed["1a"] is False and passed["1b"] is True


def test_a_missing_required_cell_fails_criterion_4() -> None:
    facts = [
        f
        for f in _step2_facts(("0.30", "0.15"))
        if not (f.label == A7_LOW_VOL and f.window == "decade")
    ]
    test = next(c for c in criteria(facts, A7_LOW_VOL, _trials(facts)) if c.name.startswith("4."))
    assert test.passed is False and "no floor-only decade" in test.detail


def test_a_diluted_required_cell_fails_every_criterion_that_needs_it() -> None:
    facts = [
        replace(f, label=A2_INDUSTRY_GATE) if f.label == A7_LOW_VOL else f
        for f in _step2_facts(("0.30", "0.15"))
    ]
    clean = A2Coverage(
        share={f.cell: Decimal("0.1") for f in facts},
        by_year={},
        first_rankable={},
    )
    assert all(c.passed for c in criteria(facts, A2_INDUSTRY_GATE, _trials(facts), coverage=clean))
    diluted_ten = A2Coverage(
        share={**clean.share, (FLOOR, VER, HIGH_FLOOR): Decimal("0.31")},
        by_year={},
        first_rankable={},
    )
    got = {
        c.name.split(".")[0]: c
        for c in criteria(facts, A2_INDUSTRY_GATE, _trials(facts), coverage=diluted_ten)
    }
    assert {k for k, c in got.items() if not c.passed} == {"1b", "3", "4", "5"}
    assert all("diluted" in got[k].detail for k in ("1b", "3", "4", "5"))


def test_a_missing_nifty500_row_fails_rather_than_passes() -> None:
    facts = [f for f in _step2_facts(("0.30", "0.15")) if f.universe != "nifty500"]
    assert _passed(facts)["2"] is False


def test_dsr_is_the_sharpe_module_measured_from_d13() -> None:
    facts = _step2_facts(("0.30", "0.15"))
    trials = _trials(facts)
    test = next(c for c in criteria(facts, A7_LOW_VOL, trials) if c.name.startswith("5."))
    arm = next(f for f in facts if f.label == A7_LOW_VOL and f.cell == (FLOOR, VER, HIGH_FLOOR))
    base = next(f for f in facts if f.label == D13 and f.cell == (FLOOR, VER, HIGH_FLOOR))
    expected = deflated_sharpe_ratio(
        sharpe_stats(daily_returns(arm.nav)),
        trials=75,
        sharpe_variance=sharpe_variance([t.sharpe for t in trials]),
        benchmark_sharpe=sharpe_stats(daily_returns(base.nav)).sharpe,
    )
    assert f"p = {expected:.4f}" in test.detail and "N = 75" in test.detail
    assert test.passed is (expected >= 0.95)


def test_dsr_fails_when_the_arm_is_the_weaker_series() -> None:
    # Swap the paths: now D13 has the strong series. Measuring from zero, or comparing the wrong
    # way round, would still pass the arm.
    facts = _step2_facts(("0.30", "0.15"))
    strong = next(f.nav for f in facts if f.label == A7_LOW_VOL and f.nav)
    weak = next(f.nav for f in facts if f.label == D13 and f.nav)
    swapped = [
        replace(f, nav=weak if f.label == A7_LOW_VOL else strong) if f.nav else f for f in facts
    ]
    assert _passed(swapped)["5"] is False


def test_trials_outside_v_are_listed_and_the_two_lists_make_n() -> None:
    from backtest.m16_report import M16_TRIAL_LABELS, _n_only

    sharpes = [TrialSharpe("k1", D13, 0.05), TrialSharpe("k2", A7_LOW_VOL, 0.06)]
    rest = _n_only(sharpes)
    assert D13 not in rest and A7_LOW_VOL not in rest
    assert len(rest) + len(sharpes) == TRIALS
    assert set(rest) | {D13, A7_LOW_VOL} == {*TRIALS_ON_RECORD, *M16_TRIAL_LABELS}


def test_v_uses_the_newest_commits_run_whatever_the_order() -> None:
    cell = (FLOOR, VER, HIGH_FLOOR)
    old = replace(
        _facts(D13, cell, "0", "0", nav=_path(1, 0.0)), key="k", commit="old", digest="d1"
    )
    new = replace(
        _facts(D13, cell, "0", "0", nav=_path(2, 0.002)), key="k", commit="new", digest="d2"
    )
    other = _facts("x", (FLOOR, VER, LOW_FLOOR), "0", "0", nav=_path(3, 0.0))  # wrong cell
    when = {"old": 1, "new": 2, "c": 0}.__getitem__
    expected = sharpe_stats(daily_returns(new.nav)).sharpe
    for facts, extra in (([], [old, new]), ([], [new, old]), ([old], [new]), ([new], [old, other])):
        got = trial_sharpes(facts, extra, commit_time=when)
        assert [t.sharpe for t in got] == [expected]


def test_v_refuses_two_different_runs_at_one_commit() -> None:
    cell = (FLOOR, VER, HIGH_FLOOR)
    a = replace(_facts(D13, cell, "0", "0", nav=_path(1, 0.0)), key="k", commit="c", digest="d1")
    b = replace(a, digest="d2")
    with pytest.raises(ValueError, match="two different runs"):
        trial_sharpes([a], [b], commit_time=lambda _: 0)
    assert len(trial_sharpes([a], [a], commit_time=lambda _: 0)) == 1


def test_trial_dirs_are_pinned_by_name() -> None:
    assert TRIAL_DIRS == ("m12-rerun-2bb2b08", "m14-5-regime-floor-cb1fcf9")


# ── loading and rendering ────────────────────────────────────────────────────────────────────────


def _write_run(run_dir: Path, arm: Arm, window: str, floor: Decimal, seed: int) -> None:
    start, end = WINDOWS[window]
    spec = _arm_spec(
        arm,
        start=start,
        end=end,
        universe=UniverseParameters.for_universe(FLOOR, median_turnover_floor=floor),
        opening_cash=Decimal("1000000"),
        adjusted=True,
    )
    digest = f"{seed:064d}"
    nav = _path(seed, 0.001)
    for sub in ("runs", "ledgers", "navs"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    (run_dir / "runs" / f"{digest}.json").write_text(
        json.dumps(
            {
                "digest": digest,
                "spec": spec,
                "xirr": "0.21",
                "max_drawdown": "0.2",
                "excess": "0.1",
                "total_charges": "5000",
                "final_nav": str(nav[-1][1]),
                "terminal": nav[-1][0].isoformat(),
                "rail_blocks": {"MIN_HOLDINGS": seed},
            }
        )
    )
    (run_dir / "ledgers" / f"{digest}.json").write_text(json.dumps({"trades": [{}] * 3}))
    (run_dir / "navs" / f"{digest}.json").write_text(
        json.dumps({"points": [[d.isoformat(), str(v)] for d, v in nav]})
    )


def test_collect_reads_m16_runs_and_skips_other_configurations(tmp_path: Path) -> None:
    _write_run(tmp_path, D13_PAPER_BASELINE, VER, HIGH_FLOOR, 1)
    _write_run(tmp_path, A7_ARM, VER, LOW_FLOOR, 2)
    _write_run(tmp_path, REGIME_DAILY_SET[1], VER, HIGH_FLOOR, 3)  # not an M16 arm
    facts = sorted(collect(tmp_path), key=lambda f: f.label)
    assert [(f.label, f.cell, f.floor_refusals, f.trades) for f in facts] == [
        (D13, (FLOOR, VER, HIGH_FLOOR), 1, 3),
        (A7_LOW_VOL, (FLOOR, VER, LOW_FLOOR), 2, 3),
    ]


def _trial_dirs(tmp_path: Path) -> list[str]:
    paths = []
    for name in TRIAL_DIRS:
        (tmp_path / name / "runs").mkdir(parents=True)
        paths.append(str(tmp_path / name))
    return paths


def _manifest(run_dir: Path) -> None:
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "universe": FLOOR,
                "commit": "0" * 40,
                "lake_last_session": "2026-10-07",
                "units": ["walk-forward"],
                "arms": [D13, M107, A7_LOW_VOL],
            }
        )
    )


def test_main_renders_tables_blend_scorecard_and_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _only(monkeypatch, D13, M107, A6_BLEND, A7_LOW_VOL)
    monkeypatch.setattr(report_module, "git_commit_time", lambda _: 0)
    run_dir = tmp_path / "m16-floor"
    seed = 1
    for window in (SEL, VER):
        for floor in (LOW_FLOOR, HIGH_FLOOR):
            for arm in (D13_PAPER_BASELINE, M10_7_BASELINE, A7_ARM):
                _write_run(run_dir, arm, window, floor, seed)
                seed += 1
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "universe": FLOOR,
                "commit": "0" * 40,
                "lake_last_session": "2026-10-07",
                "units": ["walk-forward"],
                "arms": [D13, M107, A7_LOW_VOL],
            }
        )
    )
    out = tmp_path / "report.md"
    out.write_text(f"old\n{MARKER}\n## Analysis\n\nkept\n")
    assert main([str(run_dir), "--trial-dirs", *_trial_dirs(tmp_path), "--out", str(out)]) == 0
    text = out.read_text()
    # A6 is built in each of the four cells where D13 and M10.7 both ran, and scored.
    assert text.count(f"| {A6_BLEND} |") == 4 + 1  # four tables plus its scorecard row
    assert "## Scorecard against D13 (generated)" in text
    assert "**Step 1 — walk-forward choice**" in text and "N = 75" in text
    assert text.split(MARKER, 1)[1].strip() == "## Analysis\n\nkept"


def test_two_overlapping_run_directories_give_one_row_per_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # m16 and m16-fundamentals both carry D13 and M10.7: the same runs, saved twice.
    _only(monkeypatch, D13, M107, A6_BLEND, A7_LOW_VOL)
    monkeypatch.setattr(report_module, "git_commit_time", lambda _: 0)
    first, second = tmp_path / "m16", tmp_path / "m16-fundamentals"
    seed = 1
    for window in (SEL, VER):
        for floor in (LOW_FLOOR, HIGH_FLOOR):
            for arm in (D13_PAPER_BASELINE, M10_7_BASELINE, A7_ARM):
                _write_run(first, arm, window, floor, seed)
                if arm is not A7_ARM and window == VER:
                    _write_run(second, arm, window, floor, seed)
                seed += 1
    _manifest(first)
    _manifest(second)
    assert len(collect(first)) + len(collect(second)) == 12 + 4
    assert len(merge_rows([*collect(first), *collect(second)])) == 12
    out = tmp_path / "report.md"
    args = [str(first), str(second), "--trial-dirs", *_trial_dirs(tmp_path), "--out", str(out)]
    assert main(args) == 0
    text = out.read_text()
    assert f"| **{D13}** | 4 |" in text  # the scorecard counts D13's four cells once each


def test_two_different_runs_of_one_cell_are_refused(tmp_path: Path) -> None:
    _write_run(tmp_path / "a", D13_PAPER_BASELINE, VER, HIGH_FLOOR, 1)
    _write_run(tmp_path / "b", D13_PAPER_BASELINE, VER, HIGH_FLOOR, 2)
    with pytest.raises(ValueError, match="two different runs"):
        merge_rows([*collect(tmp_path / "a"), *collect(tmp_path / "b")])


def test_unmerged_duplicates_are_refused_by_render_and_criteria() -> None:
    row = _facts(D13, (FLOOR, VER, HIGH_FLOOR), "0.2", "0.2")
    with pytest.raises(ValueError, match="more than one row"):
        render([row, row], manifests={}, sharpes=[])
    with pytest.raises(ValueError, match="more than one row"):
        criteria([row, row], D13, [])


def test_main_refuses_unpinned_trial_dirs(tmp_path: Path) -> None:
    run_dir = tmp_path / "m16"
    _write_run(run_dir, D13_PAPER_BASELINE, VER, HIGH_FLOOR, 1)
    _manifest(run_dir)
    (tmp_path / "elsewhere" / "runs").mkdir(parents=True)
    with pytest.raises(SystemExit, match="must be exactly the pinned"):
        main([str(run_dir), "--trial-dirs", str(tmp_path / "elsewhere")])


def test_render_keeps_the_hand_written_section(monkeypatch: pytest.MonkeyPatch) -> None:
    _only(monkeypatch)
    text = render([], manifests={}, sharpes=[], hand_written="\n## Analysis\n\nkept\n")
    assert text.split(MARKER, 1)[1].strip() == "## Analysis\n\nkept"


# ── Amendment 1: A2's dilution ───────────────────────────────────────────────────────────────────


def _coverage(share: str, cell: tuple[str, str, Decimal]) -> A2Coverage:
    return A2Coverage(
        share={cell: Decimal(share)},
        by_year={cell: {2016: Decimal("0.42"), 2021: Decimal(share)}},
        first_rankable={"niftyit": date(2006, 1, 2)},
    )


def test_a_diluted_a2_takes_no_part_in_the_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    _only(monkeypatch, D13, A2_INDUSTRY_GATE)
    cell = (FLOOR, SEL, HIGH_FLOOR)
    facts = [
        _facts(A2_INDUSTRY_GATE, cell, "0.40", "0.10"),  # best ratio on the window
        _facts(D13, cell, "0.20", "0.20"),
    ]
    over = decision(facts, [], coverage=_coverage("0.3001", cell))
    assert "Choice: **Momentum v2, D13 paper config**" in "\n".join(over)
    at = decision(facts, [], coverage=_coverage("0.30", cell))  # not over 30%: A2 decides
    assert f"Choice: **{A2_INDUSTRY_GATE}**" in "\n".join(at)
    # No coverage measured for the cell counts as diluted, never as clean.
    unmeasured = decision(facts, [], coverage=_coverage("0.10", (FLOOR, VER, HIGH_FLOOR)))
    assert "Choice: **Momentum v2, D13 paper config**" in "\n".join(unmeasured)
    # The exclusion is listed, never silent.
    assert f"Excluded as diluted (Amendment 1): {A2_INDUSTRY_GATE}." in "\n".join(over)


def test_decisive_keeps_every_other_arm_and_drops_only_diluted_a2() -> None:
    cell = (FLOOR, VER, HIGH_FLOOR)
    rows = [_facts(A2_INDUSTRY_GATE, cell, "0.3", "0.1"), _facts(A7_LOW_VOL, cell, "0.3", "0.1")]
    assert [f.label for f in decisive(rows, _coverage("0.5", cell))] == [A7_LOW_VOL]
    assert [f.label for f in decisive(rows, _coverage("0.2", cell))] == [
        A2_INDUSTRY_GATE,
        A7_LOW_VOL,
    ]
    assert [f.label for f in decisive(rows, None)] == [A7_LOW_VOL]
    assert Decimal("0.30") == DILUTION_THRESHOLD


def test_render_refuses_a2_rows_without_coverage_and_labels_them_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _only(monkeypatch)
    cell = (FLOOR, VER, HIGH_FLOOR)
    rows = [_facts(A2_INDUSTRY_GATE, cell, "0.3", "0.1"), _facts(D13, cell, "0.2", "0.2")]
    with pytest.raises(ValueError, match="A2 coverage"):
        render(rows, manifests={}, sharpes=[])
    text = render(rows, manifests={}, sharpes=[], a2_coverage=_coverage("0.45", cell))
    assert f"{A2_INDUSTRY_GATE} *(diluted: informational, decides nothing)*" in text
    assert (
        "| floor-only wf-verification ₹10 cr | 45.00% | diluted | 2016 42.00%, 2021 45.00% |"
        in text
    )
    assert "| niftyit | 2006-01-02 |" in text


def test_a2_coverage_file_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "a2.json"
    path.write_text(
        json.dumps(
            {
                "cells": [
                    {
                        "universe": FLOOR,
                        "window": VER,
                        "floor": "100000000",
                        "unclassified_share": "0.25",
                        "by_year": {"2022": "0.27"},
                    }
                ],
                "first_rankable": {"niftybank": "2005-01-03"},
            }
        )
    )
    coverage = load_a2_coverage(path)
    assert coverage.share == {(FLOOR, VER, HIGH_FLOOR): Decimal("0.25")}
    assert coverage.by_year[(FLOOR, VER, HIGH_FLOOR)] == {2022: Decimal("0.27")}
    assert coverage.first_rankable == {"niftybank": date(2005, 1, 3)}
    assert not coverage.diluted((FLOOR, VER, HIGH_FLOOR))
