"""M16.0 report: the A6 blend, the selection floor, Step 2's directions and the deflated Sharpe."""

from __future__ import annotations

import json
import random
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from backtest.m12_rerun import REGIME_DAILY_SET, RERUN_ARMS, WINDOWS
from backtest.m16_arms import A2_INDUSTRY_GATE, A6_BLEND, A7_ARM, A7_LOW_VOL, M10_7_BASELINE
from backtest.m16_report import (
    DILUTION_THRESHOLD,
    MARKER,
    PRIMARY_FLOOR,
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


# ── Step 1: the selection floor ──────────────────────────────────────────────────────────────────


def test_the_choice_is_made_at_ten_crore_not_one() -> None:
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
    return [*earlier, *trial_sharpes(facts)]


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


def test_trial_sharpes_are_one_per_configuration_campaign_first() -> None:
    cell = (FLOOR, VER, HIGH_FLOOR)
    old = replace(_facts(D13, cell, "0", "0", nav=_path(1, 0.0)), key="k")
    new = replace(_facts(D13, cell, "0", "0", nav=_path(2, 0.002)), key="k")
    other = _facts("x", (FLOOR, VER, LOW_FLOOR), "0", "0", nav=_path(3, 0.0))  # wrong cell
    got = trial_sharpes([new], [old, other])
    assert len(got) == 1
    assert got[0].sharpe == sharpe_stats(daily_returns(new.nav)).sharpe


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


def test_main_renders_tables_blend_scorecard_and_decision(tmp_path: Path) -> None:
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
    assert main([str(run_dir), "--out", str(out)]) == 0
    text = out.read_text()
    # A6 is built in each of the four cells where D13 and M10.7 both ran, and scored.
    assert text.count(f"| {A6_BLEND} |") == 4 + 1  # four tables plus its scorecard row
    assert "## Scorecard against D13 (generated)" in text
    assert "**Step 1 — walk-forward choice**" in text and "N = 75" in text
    assert text.split(MARKER, 1)[1].strip() == "## Analysis\n\nkept"


def test_render_keeps_the_hand_written_section() -> None:
    text = render([], manifests={}, sharpes=[], hand_written="\n## Analysis\n\nkept\n")
    assert text.split(MARKER, 1)[1].strip() == "## Analysis\n\nkept"


# ── Amendment 1: A2's dilution ───────────────────────────────────────────────────────────────────


def _coverage(share: str, cell: tuple[str, str, Decimal]) -> A2Coverage:
    return A2Coverage(
        share={cell: Decimal(share)},
        by_year={cell: {2016: Decimal("0.42"), 2021: Decimal(share)}},
        first_rankable={"niftyit": date(2006, 1, 2)},
    )


def test_a_diluted_a2_takes_no_part_in_the_choice() -> None:
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


def test_render_refuses_a2_rows_without_coverage_and_labels_them_with_it() -> None:
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
