"""X2 — the round-2 fold campaign: baseline first and frozen, H-arms only against the freeze.

* ``baseline-folds`` runs exactly the two baseline arms, on the three test and three selection
  windows, at the ₹10 crore floor only, persisting summary + ledger + NAV per run; it resumes with
  zero replays and two directories get byte-identical NAV files;
* the frozen record names every run's digest and file hashes, is never rewritten, and is not
  written from a partial campaign;
* ``round2-signals`` refuses to start with no frozen record, a record from another lake, a baseline
  digest the current code no longer gives (a changed baseline arm), or a file changed since the
  freeze — and refuses a baseline arm, an unknown arm or a missing trial count as input;
* end to end, it renders §4's PASS/FAIL per criterion against the named baseline, naming the
  folds each criterion read and the Sharpe variance's source and n, and never writes into the
  frozen directory;
* ``trial-sharpes`` writes the round-1 arms' Sharpes on the folds, refused on other folds or
  another lake, and ``round2-signals``/``render-round2`` pool them into the variance;
* ``render-round2`` replays nothing: it reads runs already on disk and writes elsewhere;
* a fold run puts in force exactly what ``backtest.sweep`` does — the signal's pre-seam split
  factors as well as the book — and a run without them never shares a digest with one with them.

Offline: the lake and the replay are stubbed at the sweep's two seams (``open_swing_lake`` and
``_run_arm``), as in ``test_campaign``.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import backtest.book_actions as book_actions_module
import backtest.fold_campaign as fc
import backtest.sweep as sweep_module
from backtest.book_actions import (
    RescaleKind,
    ShareRescale,
    book_corporate_actions,
    current_book_actions,
    current_signal_split_factors,
    signal_split_factors,
    store_book_actions_unless,
)
from backtest.cash_interest import cash_interest_unless
from backtest.folds import load_folds
from backtest.nav import nav_file
from backtest.policies.swing_composite import SwingCompositeParameters
from backtest.run import UniverseParameters
from backtest.run_ledger import RunSummary, persist_run, run_digest
from backtest.sweep import HIGH_FLOOR, Arm, _arm_spec
from backtest.tax import MappingGrandfatheringPrices, RunLedger, TaxTrade
from backtest.xirr import Cashflow
from execution.broker import Side

_ISIN = "INE001A01036"
_CASH = Decimal("1000000")
_BUY_LEVY = Decimal(500)  # the stub buy's STT (a fixture value, not a rate)
_FOLDS = load_folds()
_PINNED = fc.Pinned(commit="a" * 40, data_root="/lake", lake_last_session=date(2026, 9, 25))
_FMV = MappingGrandfatheringPrices({})
_H_ARM = "Short composite"  # a stand-in H-arm: any non-baseline sweep arm drives the same path

#: Per-arm drift of the stub NAV, so the arms' Sharpe ratios differ.
_DRIFT = {
    "Swing composite (M10.7)": 1,
    "M10.7 + regime gate": 2,
    _H_ARM: 9,
    "Breakout": 4,
    "Trend: 1-month": 6,
}
_TRIAL_ARMS = ["Breakout", "Trend: 1-month"]


class _FakeLake:
    def __init__(self, start: date, end: date) -> None:
        self.sessions = (start, end)
        self.first_session, self.terminal = start, end
        self.features = SimpleNamespace(load=lambda dates: None)

    def close(self) -> None:
        pass


@dataclass
class _Counters:
    lakes: int = 0
    backtests: list[tuple[str, Decimal, date, date]] = field(default_factory=list)


def _nav(label: str, start: date) -> tuple[tuple[date, Decimal], ...]:
    drift = _DRIFT[label]
    return tuple(
        (start + timedelta(days=i), _CASH + Decimal(drift * 100 * i + (i % 3) * 700))
        for i in range(40)
    )


def _stub_xirr(start: date) -> Decimal:
    """A pre-tax XIRR that differs per window, so a chain and a mean of them differ too."""
    return Decimal(start.year - 2010) / Decimal(50)


def _ledger(label: str, start: date, end: date) -> RunLedger:
    return RunLedger(
        source="stub",
        trades=(
            TaxTrade(
                isin=_ISIN,
                trade_date=start,
                side=Side.BUY,
                quantity=1000,
                net_amount=Decimal("500000"),
                stt=_BUY_LEVY,
                stt_known=True,
            ),
        ),
        external_flows=(Cashflow(start, -_CASH),),
        terminal_date=end,
        terminal_nav=_CASH + Decimal(_DRIFT[label] * 20000),
        terminal_prices={_ISIN: Decimal("600")},
    )


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Counters]:
    counters = _Counters()

    def open_lake(**kwargs: Any) -> _FakeLake:
        counters.lakes += 1
        return _FakeLake(kwargs["start"], kwargs["end"])

    def run_arm(
        arm: Arm,
        *,
        start: date,
        end: date,
        universe: UniverseParameters,
        opening_cash: Decimal,
        data_root: Path | None,
        adjusted: bool,
        lake: object,
    ) -> Any:
        counters.backtests.append((arm.label, universe.median_turnover_floor, start, end))
        spec = _arm_spec(
            arm,
            start=start,
            end=end,
            universe=universe,
            opening_cash=opening_cash,
            adjusted=adjusted,
        )
        digest = run_digest(spec)
        ledger = _ledger(arm.label, start, end)
        persist_run(
            spec,
            ledger,
            RunSummary(
                digest=digest,
                spec=spec,
                policy="stub",
                start=start,
                terminal=end,
                sessions=40,
                xirr=_stub_xirr(start),
                max_drawdown=Decimal("0.2"),
                excess=Decimal("0"),
                benchmark_xirr=Decimal("0.1"),
                benchmark_name="Nifty 50",
                total_charges=Decimal("0"),
                final_nav=ledger.terminal_nav,
                round_trips=0,
                median_hold_days=0,
                replay_digest="0" * 64,
                benchmark_source="published_tri",
                rail_blocks={"MAX_POSITION": 2},
            ),
            nav=_nav(arm.label, start),
        )
        return SimpleNamespace(
            result=SimpleNamespace(journal=()),
            comparison=SimpleNamespace(
                portfolio_xirr=Decimal("0.1"),
                benchmark_xirr=Decimal("0.1"),
                excess_over_benchmark=Decimal("0"),
            ),
            max_drawdown=Decimal("0.2"),
            total_charges=Decimal("0"),
            benchmark_index_name="stub",
            digest=digest,
            ledger=ledger,
        )

    monkeypatch.setattr(sweep_module, "open_swing_lake", open_lake)
    monkeypatch.setattr(sweep_module, "_run_arm", run_arm)
    yield counters


def _baseline(out: Path) -> fc.FoldRunPlan:
    return fc.baseline_plan(out, _FOLDS, data_root=None, book_actions=False)


def _frozen(out: Path) -> fc.FoldRunPlan:
    plan = _baseline(out)
    fc.run_fold_units(plan, workers=1)
    fc.freeze_baseline(plan, _PINNED, None)
    return plan


# ── baseline-folds ─────────────────────────────────────────────────────────────────────────────


def test_baseline_folds_runs_only_the_two_baselines_on_every_fold_window(
    tmp_path: Path, stubbed: _Counters
) -> None:
    fc.run_fold_units(_baseline(tmp_path), workers=1)
    assert {label for label, *_ in stubbed.backtests} == set(fc.BASELINE_LABELS)
    assert {floor for _, floor, *_ in stubbed.backtests} == {HIGH_FLOOR}
    windows = {(start, end) for *_, start, end in stubbed.backtests}
    expected = {(f.test.start, f.test.end) for f in _FOLDS.folds}
    expected |= {(f.selection.start, f.selection.end) for f in _FOLDS.folds}
    expected |= {(_FOLDS.folds[0].test.start, _FOLDS.folds[-1].test.end)}  # the continuous run
    assert windows == expected
    assert len(stubbed.backtests) == 14
    assert len(list((tmp_path / "navs").glob("*.json"))) == 14


def test_baseline_folds_resumes_with_zero_replays(tmp_path: Path, stubbed: _Counters) -> None:
    fc.run_fold_units(_baseline(tmp_path), workers=1)
    stubbed.backtests.clear()
    stubbed.lakes = 0
    outcomes = fc.run_fold_units(_baseline(tmp_path), workers=1)
    assert stubbed.backtests == [] and stubbed.lakes == 0
    assert sum(o.resumed for o in outcomes) == 14


def test_two_directories_get_byte_identical_nav_files(tmp_path: Path, stubbed: _Counters) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    for out in (one, two):
        fc.run_fold_units(_baseline(out), workers=1)
    names = sorted(p.name for p in (one / "navs").glob("*.json"))
    assert names and all(
        (one / "navs" / n).read_bytes() == (two / "navs" / n).read_bytes() for n in names
    )


def test_the_frozen_record_names_every_run_and_its_hashes(
    tmp_path: Path, stubbed: _Counters
) -> None:
    _frozen(tmp_path)
    record = json.loads((tmp_path / fc.FROZEN_NAME).read_text(encoding="utf-8"))
    assert record["arms"] == list(fc.BASELINE_LABELS)
    assert record["floor"] == str(HIGH_FLOOR)
    assert record["commit"] == "a" * 40
    assert len(record["runs"]) == 14
    assert {(r["fold"], r["role"]) for r in record["runs"]} == {
        (f, role) for f in ("F1", "F2", "F3") for role in ("test", "selection")
    } | {("F1-F3", "continuous")}
    assert all(
        len(r[k]) == 64
        for r in record["runs"]
        for k in ("digest", "summary_sha256", "ledger_sha256", "nav_sha256")
    )


def test_a_partial_campaign_is_not_frozen(tmp_path: Path, stubbed: _Counters) -> None:
    plan = _baseline(tmp_path)
    fc.run_fold_units(plan, workers=1)
    next((tmp_path / "navs").glob("*.json")).unlink()
    with pytest.raises(fc.FoldCampaignError, match="incomplete"):
        fc.freeze_baseline(plan, _PINNED, None)
    assert not (tmp_path / fc.FROZEN_NAME).exists()


def test_a_frozen_record_is_never_rewritten(tmp_path: Path, stubbed: _Counters) -> None:
    plan = _frozen(tmp_path)
    fc.freeze_baseline(plan, _PINNED, None)  # the identical record: left as it is
    with pytest.raises(fc.FoldCampaignError, match="never rewritten"):
        fc.freeze_baseline(plan, replace(_PINNED, commit="b" * 40), None)


# ── round2-signals refuses a missing or stale freeze ───────────────────────────────────────────


def _verify(baseline_dir: Path, pinned: fc.Pinned = _PINNED) -> dict[str, Any]:
    return fc.verify_frozen(baseline_dir, pinned, _FOLDS, None, book_actions=False)


def test_round2_refuses_without_a_frozen_baseline(tmp_path: Path) -> None:
    with pytest.raises(fc.FoldCampaignError, match="no frozen baseline"):
        _verify(tmp_path)


def test_round2_accepts_a_matching_freeze_at_a_later_commit(
    tmp_path: Path, stubbed: _Counters
) -> None:
    _frozen(tmp_path)
    assert len(_verify(tmp_path, replace(_PINNED, commit="c" * 40))["runs"]) == 14


def test_round2_refuses_a_freeze_from_another_lake(tmp_path: Path, stubbed: _Counters) -> None:
    _frozen(tmp_path)
    with pytest.raises(fc.FoldCampaignError, match="lake_last_session"):
        _verify(tmp_path, replace(_PINNED, lake_last_session=date(2026, 10, 30)))


def test_round2_refuses_when_the_baseline_arm_changed_in_code(
    tmp_path: Path, stubbed: _Counters, monkeypatch: pytest.MonkeyPatch
) -> None:
    _frozen(tmp_path)
    changed = tuple(
        replace(arm, swing=SwingCompositeParameters(top_n=arm.swing.top_n + 1))
        if arm.label == fc.BASELINE_LABELS[0] and arm.swing is not None
        else arm
        for arm in sweep_module.ARMS
    )
    monkeypatch.setattr(fc, "ARMS", changed)
    with pytest.raises(fc.FoldCampaignError, match="does not match the current code"):
        _verify(tmp_path)


def test_round2_refuses_a_file_changed_since_the_freeze(tmp_path: Path, stubbed: _Counters) -> None:
    _frozen(tmp_path)
    victim = next((tmp_path / "navs").glob("*.json"))
    victim.write_text(victim.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(fc.FoldCampaignError, match="changed since the freeze"):
        _verify(tmp_path)


@pytest.mark.parametrize(
    ("labels", "match"),
    [
        ([fc.BASELINE_LABELS[1]], "frozen baseline arm"),
        (["No such arm"], "no sweep arm"),
        ([], "at least one"),
        ([_H_ARM, _H_ARM], "twice"),
    ],
)
def test_round2_refuses_bad_arm_lists(tmp_path: Path, labels: list[str], match: str) -> None:
    with pytest.raises(fc.FoldCampaignError, match=match):
        fc.round2_plan(tmp_path, _FOLDS, labels, data_root=None, book_actions=False)


def test_round2_runs_the_test_windows_and_the_continuous_headline_run(tmp_path: Path) -> None:
    plan = fc.round2_plan(tmp_path, _FOLDS, [_H_ARM], data_root=None, book_actions=False)
    assert [(w.fold, w.role) for w in plan.windows] == [
        ("F1", "test"),
        ("F2", "test"),
        ("F3", "test"),
        ("F1-F3", "continuous"),
    ]
    continuous = plan.windows[-1].window
    assert (continuous.start, continuous.end) == (date(2016, 9, 1), date(2026, 8, 31))


def test_trial_sharpes_run_the_test_windows_only(tmp_path: Path) -> None:
    plan = fc.round2_plan(
        tmp_path, _FOLDS, _TRIAL_ARMS, data_root=None, book_actions=False, continuous=False
    )
    assert {w.role for w in plan.windows} == {"test"}


def test_a_union_with_a_hole_is_not_one_continuous_window() -> None:
    f1, f2, f3 = _FOLDS.folds
    holed = replace(
        _FOLDS, folds=(f1, replace(f2, test=replace(f2.test, start=date(2019, 10, 1))), f3)
    )
    with pytest.raises(fc.FoldCampaignError, match="do not abut"):
        fc.continuous_window(holed)


def test_the_trial_count_is_a_required_flag() -> None:
    argv = ["round2-signals", "--baseline-dir", "b", "--out", "o", "--arms", _H_ARM]
    argv += ["--baseline", fc.BASELINE_LABELS[0], "--workers", "1"]
    with pytest.raises(SystemExit):
        fc._parse_args(argv)
    assert fc._parse_args([*argv, "--trials", "31"]).trials == 31


# ── end to end ─────────────────────────────────────────────────────────────────────────────────


def test_round2_renders_pass_or_fail_per_criterion_against_the_named_baseline(
    tmp_path: Path, stubbed: _Counters
) -> None:
    base_dir, h_dir = tmp_path / "base", tmp_path / "h"
    _frozen(base_dir)
    _verify(base_dir)
    plan = fc.round2_plan(h_dir, _FOLDS, [_H_ARM], data_root=None, book_actions=False)
    stubbed.backtests.clear()
    fc.run_fold_units(plan, workers=1)
    assert {label for label, *_ in stubbed.backtests} == {_H_ARM}
    text = fc.render_round2(
        plan, base_dir, _FOLDS, None, _FMV, baseline_label=fc.BASELINE_LABELS[0], trials=28
    )
    assert f"### {_H_ARM} vs {fc.BASELINE_LABELS[0]}:" in text
    assert text.count("**PASS**") + text.count("**FAIL**") + text.count("**INCONCLUSIVE**") == 4
    # Three arms evaluated is far short of the minimum: the variance and its n are printed, and
    # criterion 4 cannot PASS on it.
    assert "from 3 trial Sharpes (3 arms evaluated in this round)" in text
    assert "too few: criterion 4 cannot PASS" in text
    assert "| Folds used |" in text and "| F1, F2, F3 |" in text
    assert "Trial count for the deflation: **28**" in text
    assert "₹10 crore/day" in text
    # The after-tax NAV is written beside each H-arm run it was struck from.
    assert len(list((h_dir / "navs").glob("*.after-tax.*.json"))) == 3
    assert all(nav_file(h_dir, d).is_file() for d in fc._digests(plan, None).values())


def test_the_round2_report_heads_with_the_continuous_runs_and_shows_rail_blocks(
    tmp_path: Path, stubbed: _Counters
) -> None:
    base_dir, plan = _round2(tmp_path)
    text = fc.render_round2(
        plan, base_dir, _FOLDS, None, _FMV, baseline_label=fc.BASELINE_LABELS[0], trials=28
    )
    headline = _rows(_section(text, "## Headline — one continuous run, 2016-09-01 → 2026-08-31"))
    assert [row[0] for row in headline] == [*fc.BASELINE_LABELS, _H_ARM]
    assert all(row[6] == "published Nifty 50 TRI" for row in headline)
    # The headline and the stability evidence come before the rule's own figures and verdicts.
    assert text.index("## Headline") < text.index("## Per-fold test windows — stability")
    assert text.index("## Per-fold test windows") < text.index("## Test-window figures the rule")
    assert text.index("## Test-window figures the rule") < text.index("## Verdict per arm")
    assert "geometric: (Π(1 + r_i)^t_i)^(1/T) - 1" in text
    rule = _rows(_section(text, "## Test-window figures the rule reads (§4)"))
    assert len(rule) == 9 and all(row[-1] == "MAX_POSITION 2" for row in rule)
    # Criterion 1 is the pre-registered arithmetic mean, and says so.
    assert "arithmetic mean" in text


def test_round2_refuses_an_unnamed_baseline(tmp_path: Path, stubbed: _Counters) -> None:
    base_dir = tmp_path / "base"
    _frozen(base_dir)
    plan = fc.round2_plan(tmp_path / "h", _FOLDS, [_H_ARM], data_root=None, book_actions=False)
    with pytest.raises(fc.FoldCampaignError, match="frozen baseline arm"):
        fc.render_round2(plan, base_dir, _FOLDS, None, _FMV, baseline_label=_H_ARM, trials=28)


def test_the_baseline_report_renders_every_fold_and_role(
    tmp_path: Path, stubbed: _Counters
) -> None:
    plan = _frozen(tmp_path)
    text = fc.render_baseline_report(plan, _FOLDS, None, _FMV)
    every = _section(text, "## Every fold window, as frozen")
    assert len(_rows(every)) == 12
    assert all(row[-1] == "MAX_POSITION 2" for row in _rows(every))  # rail blocks per row


def _section(text: str, heading: str) -> str:
    """The markdown under ``heading`` up to the next heading of any level."""
    body = text.split(heading, 1)[1]
    return body.split("\n#", 1)[0]


def _rows(section: str) -> list[list[str]]:
    """The data rows of the first table in ``section``, as cells."""
    lines = section.splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith("| "))
    table: list[str] = []
    for line in lines[first:]:
        if not line.startswith("| "):
            break
        table.append(line)
    return [[c.strip() for c in line.strip("|").split("|")] for line in table[2:]]


def test_the_baseline_report_heads_with_one_continuous_run(
    tmp_path: Path, stubbed: _Counters
) -> None:
    """The headline is the continuous run over 2016-09-01 → 2026-08-31, not a fold mean."""
    plan = _frozen(tmp_path)
    text = fc.render_baseline_report(plan, _FOLDS, None, _FMV)
    headline = _section(text, "## Headline — one continuous run, 2016-09-01 → 2026-08-31")
    rows = _rows(headline)
    assert [row[0] for row in rows] == list(fc.BASELINE_LABELS)
    for row in rows:
        assert row[1] == "2016-09-01 → 2026-08-31"
        assert row[2] == f"{_stub_xirr(date(2016, 9, 1)):.2%}"  # pre-tax, the continuous run's
        assert row[3].endswith("%")  # after-tax realised
        assert row[4] == "20.00%"  # max drawdown
        assert row[6] == "published Nifty 50 TRI"  # read from the recorded source
        assert row[8] == "MAX_POSITION 2"
    assert text.index("## Headline") < text.index("## Per-fold test windows")
    assert "price-return L1 proxy" not in text


def test_the_per_fold_xirrs_are_shown_as_stability_evidence(
    tmp_path: Path, stubbed: _Counters
) -> None:
    plan = _frozen(tmp_path)
    text = fc.render_baseline_report(plan, _FOLDS, None, _FMV)
    rows = _rows(_section(text, "## Per-fold test windows — stability evidence"))
    assert [(row[0], row[1]) for row in rows] == [
        (label, fold) for label in fc.BASELINE_LABELS for fold in ("F1", "F2", "F3")
    ]
    assert [row[3] for row in rows[:3]] == [f"{_stub_xirr(f.test.start):.2%}" for f in _FOLDS.folds]
    assert all(row[-1] == "MAX_POSITION 2" for row in rows)


def test_the_chained_figure_is_geometric_never_an_arithmetic_mean(
    tmp_path: Path, stubbed: _Counters
) -> None:
    """Fails if the chain is computed as the arithmetic mean of the fold XIRRs (or weighted)."""
    plan = _frozen(tmp_path)
    text = fc.render_baseline_report(plan, _FOLDS, None, _FMV)
    chained = text.split("**Chained across F1, F2, F3 — geometric: (Π(1 + r_i)^t_i)^(1/T) - 1**")
    assert len(chained) == 2, "the chained figure must be labelled as geometric"
    swing = next(row for row in _rows(chained[1]) if row[0] == fc.BASELINE_LABELS[0])
    legs = [
        (_stub_xirr(f.test.start), Decimal((f.test.end - f.test.start).days) / Decimal(365))
        for f in _FOLDS.folds
    ]
    rates = [r for r, _ in legs]
    arithmetic = sum(rates, Decimal(0)) / len(rates)
    weighted = sum((r * t for r, t in legs), Decimal(0)) / sum((t for _, t in legs), Decimal(0))
    assert swing[1] == f"{fc.geometric_chain(legs):.2%}"
    assert swing[1] not in (f"{arithmetic:.2%}", f"{weighted:.2%}")


def test_geometric_chain_compounds_to_the_legs_terminal_wealth() -> None:
    legs = [(Decimal("0.50"), Decimal(1)), (Decimal("-0.30"), Decimal(1))]
    chained = fc.geometric_chain(legs)
    # 1.5 x 0.7 = 1.05 over two years: sqrt(1.05) - 1, not the arithmetic 10 %.
    assert abs(chained - (Decimal("1.05").sqrt() - 1)) < Decimal("1e-20")
    assert chained < Decimal("0.03")
    weighted = [(Decimal("0.10"), Decimal(3)), (Decimal("0.20"), Decimal(1))]
    expected = (Decimal("1.10") ** 3 * Decimal("1.20")) ** (Decimal(1) / 4) - 1
    assert abs(fc.geometric_chain(weighted) - expected) < Decimal("1e-20")
    with pytest.raises(ValueError):
        fc.geometric_chain([])
    with pytest.raises(ValueError):
        fc.geometric_chain([(Decimal("-1"), Decimal(1))])


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def _round2(tmp_path: Path) -> tuple[Path, fc.FoldRunPlan]:
    base_dir = tmp_path / "base"
    _frozen(base_dir)
    plan = fc.round2_plan(tmp_path / "h", _FOLDS, [_H_ARM], data_root=None, book_actions=False)
    fc.run_fold_units(plan, workers=1)
    return base_dir, plan


def test_round2_never_writes_into_the_frozen_directory(tmp_path: Path, stubbed: _Counters) -> None:
    base_dir, plan = _round2(tmp_path)
    before = _tree(base_dir)
    fc.render_round2(
        plan, base_dir, _FOLDS, None, _FMV, baseline_label=fc.BASELINE_LABELS[0], trials=28
    )
    assert _tree(base_dir) == before


def test_a_render_to_another_directory_writes_nothing_beside_the_runs(
    tmp_path: Path, stubbed: _Counters
) -> None:
    base_dir, plan = _round2(tmp_path)
    runs_before, base_before = _tree(plan.out_dir), _tree(base_dir)
    elsewhere = tmp_path / "render"
    stubbed.backtests.clear()
    fc.render_round2(
        plan,
        base_dir,
        _FOLDS,
        None,
        _FMV,
        baseline_label=fc.BASELINE_LABELS[0],
        trials=28,
        nav_dir=elsewhere,
    )
    assert stubbed.backtests == []
    assert _tree(plan.out_dir) == runs_before and _tree(base_dir) == base_before
    assert len(list((elsewhere / "navs").glob("*.after-tax.*.json"))) == 3


def test_absent_runs_are_named_for_a_render_that_replays_nothing(
    tmp_path: Path, stubbed: _Counters
) -> None:
    _, plan = _round2(tmp_path)
    assert fc._absent_runs(plan, None) == []
    digest = fc._digests(plan, None)[(_H_ARM, "F2", "test")]
    nav_file(plan.out_dir, digest).unlink()
    assert fc._absent_runs(plan, None) == [f"{_H_ARM} F2 test"]


# ── trial-sharpes: the round-1 arms on the same folds, as the variance source ───────────────────


def _trial_sharpes(tmp_path: Path) -> Path:
    plan = fc.round2_plan(
        tmp_path / "trials", _FOLDS, _TRIAL_ARMS, data_root=None, book_actions=False
    )
    fc.run_fold_units(plan, workers=1)
    return fc.write_trial_sharpes(plan, _PINNED, _FOLDS, None, _FMV)


def test_the_round_one_arms_are_every_non_baseline_non_hypothesis_arm() -> None:
    labels = fc.round1_labels()
    assert not set(labels) & set(fc.BASELINE_LABELS)
    assert all("(H" not in label for label in labels)
    assert len(labels) == len(sweep_module.ARMS) - len(fc.BASELINE_LABELS) - 3


def test_trial_sharpes_are_written_per_arm_on_every_fold(
    tmp_path: Path, stubbed: _Counters
) -> None:
    path = _trial_sharpes(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert [t["arm"] for t in document["trials"]] == _TRIAL_ARMS
    assert document["folds"] == ["F1", "F2", "F3"] and document["excluded"] == []
    assert all(set(t["digests"]) == {"F1", "F2", "F3"} for t in document["trials"])
    loaded = fc.load_trial_sharpes(path, _PINNED, _FOLDS, book_actions=False)
    assert [t.label for t in loaded.sharpes] == _TRIAL_ARMS
    assert loaded.source.startswith(fc.TRIAL_SHARPES_NAME)


def test_trial_sharpes_from_another_lake_or_other_folds_are_refused(
    tmp_path: Path, stubbed: _Counters
) -> None:
    path = _trial_sharpes(tmp_path)
    with pytest.raises(fc.FoldCampaignError, match="lake_last_session"):
        fc.load_trial_sharpes(
            path, replace(_PINNED, lake_last_session=date(2026, 10, 30)), _FOLDS, book_actions=False
        )
    two_folds = replace(_FOLDS, folds=_FOLDS.folds[:2])
    with pytest.raises(fc.FoldCampaignError, match="folds"):
        fc.load_trial_sharpes(path, _PINNED, two_folds, book_actions=False)


def test_supplied_trial_sharpes_are_pooled_and_reported(tmp_path: Path, stubbed: _Counters) -> None:
    base_dir, plan = _round2(tmp_path)
    supplied = fc.load_trial_sharpes(_trial_sharpes(tmp_path), _PINNED, _FOLDS, book_actions=False)
    text = fc.render_round2(
        plan,
        base_dir,
        _FOLDS,
        None,
        _FMV,
        baseline_label=fc.BASELINE_LABELS[0],
        trials=28,
        trial_sharpes=supplied,
    )
    assert "from 5 trial Sharpes (3 arms evaluated in this round + 2 supplied by " in text
    assert "Breakout" in text and "Trend: 1-month" in text


def test_the_render_and_trial_sharpes_commands_parse() -> None:
    argv = ["render-round2", "--baseline-dir", "b", "--runs-dir", "r", "--runs-from-commit"]
    argv += ["52ff874", "--out", "o", "--arms", _H_ARM, "--baseline", fc.BASELINE_LABELS[0]]
    render = fc._parse_args([*argv, "--trials", "28", "--trial-sharpes", "t.json"])
    assert render.runs_from_commit == "52ff874" and render.trial_sharpes == Path("t.json")
    assert not hasattr(render, "workers")
    trials = fc._parse_args(["trial-sharpes", "--out", "o", "--workers", "2"])
    assert trials.arms is None and trials.workers == 2


# ── the fold path puts in force what the sweep CLI does (X2: one digest, one replay) ──────────

#: A split ex before L2's 2016-09-02 seam: the factor a swing signal needs across it.
_PRE_SEAM_SPLIT = ShareRescale(
    isin=_ISIN,
    ex_date=date(2015, 6, 1),
    kind=RescaleKind.SPLIT,
    numerator=Decimal(10),
    denominator=Decimal(2),
)


class _StubActions:
    """A ``BookActionSource`` holding one pre-seam split (no calendar, no Postgres)."""

    def between(self, after: date | None, upto: date) -> tuple[ShareRescale, ...]:
        due = after is None or after < _PRE_SEAM_SPLIT.ex_date
        return (_PRE_SEAM_SPLIT,) if due and _PRE_SEAM_SPLIT.ex_date <= upto else ()


def test_the_fold_contexts_put_the_signal_split_factors_in_force(tmp_path: Path) -> None:
    """Before the fix ``_contexts`` set the book and cash interest only: this read ``None``."""
    plan = fc.baseline_plan(tmp_path, _FOLDS, data_root=None, book_actions=True)
    with ExitStack() as stack:
        fc._contexts(stack, plan, _StubActions())
        assert current_signal_split_factors() == (_PRE_SEAM_SPLIT,)
        assert current_book_actions() is not None
    assert current_signal_split_factors() is None


def test_the_fold_contexts_switched_off_turn_the_signal_factors_off_too(tmp_path: Path) -> None:
    plan = fc.baseline_plan(tmp_path, _FOLDS, data_root=None, book_actions=False)
    with ExitStack() as stack:
        fc._contexts(stack, plan, _StubActions())
        assert current_signal_split_factors() is None
        assert current_book_actions() is None


def _fold_digest(window: Any, *, signal: bool) -> str:
    arm = fc._resolve(fc.BASELINE_LABELS[:1])
    with book_corporate_actions(_StubActions()), ExitStack() as stack:
        if signal:
            stack.enter_context(signal_split_factors(_StubActions()))
        digests = sweep_module.run_digests(
            start=window.start, end=window.end, arms=arm, floors=(HIGH_FLOOR,)
        )
    return digests[(fc.BASELINE_LABELS[0], HIGH_FLOOR)]


def test_a_run_with_signal_split_factors_never_shares_a_digest_with_one_without() -> None:
    window = _FOLDS.folds[1].test
    assert _fold_digest(window, signal=True) != _fold_digest(window, signal=False)
    assert _fold_digest(window, signal=True) == _fold_digest(window, signal=True)


def test_the_fold_path_and_the_sweep_cli_derive_the_same_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same window, arm and floor: the fold unit and ``backtest.sweep`` name one run, not two."""
    monkeypatch.setattr(book_actions_module, "load_store_book_actions", _StubActions)
    plan = fc.baseline_plan(tmp_path, _FOLDS, data_root=None, book_actions=True)
    fold = fc._digests(plan, _StubActions())[(fc.BASELINE_LABELS[0], "F2", "test")]
    window = _FOLDS.folds[1].test
    args = argparse.Namespace(book_corporate_actions=True, cash_interest=True)
    with store_book_actions_unless(args), cash_interest_unless(args):
        cli = sweep_module.run_digests(
            start=window.start, end=window.end, arms=plan.arms, floors=(HIGH_FLOOR,)
        )[(fc.BASELINE_LABELS[0], HIGH_FLOOR)]
    assert fold == cli
