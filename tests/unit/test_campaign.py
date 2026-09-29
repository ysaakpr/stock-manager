"""X2 — the after-tax campaign: resumable, capped at two workers, and explicit about the investor.

* **Resume**: a second campaign invocation over finished runs performs zero backtests and never
  opens the lake — the sweeps load every run from its persisted summary and ledger;
* the reports rendered from those persisted runs carry the after-tax columns, struck from the
  ledgers, and state the investor assumptions in their header;
* **no silent investor defaults**: every after-tax CLI refuses to start without the investor flags,
  and a report asked to render after-tax columns nobody computed refuses too;
* the sweep's pre-run digest is the digest the runner itself persists under (else resume would
  silently replay everything);
* a directory started by a different commit or lake is refused; more than two workers is refused.

Offline: the lake and the replay are stubbed at the sweep's two seams (``open_swing_lake`` and
``_run_arm``); what is under test is the resume bookkeeping, not the replay.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import backtest.run as run_module
import backtest.sweep as sweep_module
from backtest.book_actions import BookActionCalendar
from backtest.campaign import (
    CampaignError,
    CampaignPlan,
    check_manifest,
    check_render_only,
    missing_runs,
    render_campaign,
    run_campaign,
)
from backtest.campaign import _parse_args as campaign_args
from backtest.run import (
    UniverseParameters,
    run_momentum_v2,
    run_naive_momentum,
    run_swing_composite,
)
from backtest.run_ledger import RunSummary, persist_run, run_digest
from backtest.sweep import (
    ARMS,
    HIGH_FLOOR,
    LOW_FLOOR,
    Arm,
    SweepResult,
    SweepRow,
    _arm_spec,
    render_sweep_report,
)
from backtest.sweep import _parse_args as sweep_args
from backtest.tax import (
    InvestorProfile,
    MappingGrandfatheringPrices,
    PaymentTiming,
    RunLedger,
    TaxTrade,
)
from backtest.verdict import _parse_args as verdict_args
from backtest.windows import CampaignWindows, Window
from backtest.xirr import Cashflow, xirr
from execution.broker import Side

_ISIN = "INE001A01036"
_CASH = Decimal("1000000")
_BUY_LEVY = Decimal(500)  # the stub buy's STT: 0.1 % of ₹5 lakh (a fixture value, not a rate)
_PROFILE = InvestorProfile(
    residency="resident_individual",
    slab_rate=Decimal("0.30"),
    cg_surcharge_rate=Decimal("0.15"),
    dividend_surcharge_rate=Decimal("0.15"),
    payment_timing=PaymentTiming.FY_END,
)
_INVESTOR = [
    "--slab-rate",
    "0.30",
    "--cg-surcharge",
    "0.15",
    "--dividend-surcharge",
    "0.15",
    "--payment",
    "fy_end",
]

_WINDOWS = CampaignWindows(
    joinable_from=date(2019, 1, 1),
    sweeps=(
        Window("full", date(2020, 1, 1), date(2021, 12, 31)),
        Window("decade", date(2020, 6, 1), date(2021, 12, 31)),
        # Not the verification half's dates: an identical spec would (rightly) be one shared run.
        Window("six-year", date(2021, 3, 1), date(2021, 12, 31)),
    ),
    selection=Window("wf-selection", date(2020, 1, 1), date(2020, 12, 31)),
    verification=Window("wf-verification", date(2021, 1, 1), date(2021, 12, 31)),
)
#: One swing arm and one momentum baseline: both spec paths, and the 2 x 2 x 5 = 20-run grid.
_ARMS: tuple[Arm, ...] = (ARMS[0], next(a for a in ARMS if a.naive is not None))


class _FakeLake:
    """``open_swing_lake`` stand-in: a window of sessions, a feature cache that loads nothing."""

    def __init__(self, start: date, end: date) -> None:
        self.sessions = (start, end)
        self.first_session, self.terminal = start, end
        self.features = SimpleNamespace(load=lambda dates: None)

    def close(self) -> None:
        pass


@dataclass
class _Counters:
    lakes: int = 0
    backtests: list[str] = field(default_factory=list)


def _stub_ledger(start: date, end: date) -> RunLedger:
    """A run that bought ₹5 lakh of one name on its first day and held it to a ₹6 lakh mark."""
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
        terminal_nav=Decimal("1100000"),
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
        # What a real runner does at its end (``_finish_backtest``): persist under its own spec.
        counters.backtests.append(f"{arm.label}@{universe.median_turnover_floor}:{start}..{end}")
        spec = _arm_spec(
            arm,
            start=start,
            end=end,
            universe=universe,
            opening_cash=opening_cash,
            adjusted=adjusted,
        )
        digest = run_digest(spec)
        ledger = _stub_ledger(start, end)
        xirr = Decimal("0.12") if arm.swing is not None else Decimal("0.09")
        persist_run(
            spec,
            ledger,
            RunSummary(
                digest=digest,
                spec=spec,
                policy="stub",
                start=start,
                terminal=end,
                sessions=2,
                xirr=xirr,
                max_drawdown=Decimal("0.2"),
                excess=Decimal("0.01"),
                benchmark_xirr=Decimal("0.11"),
                benchmark_name="stub index",
                total_charges=Decimal("1000"),
                final_nav=ledger.terminal_nav,
                round_trips=0,
                median_hold_days=0,
                replay_digest="0" * 64,
            ),
        )
        return SimpleNamespace(
            result=SimpleNamespace(journal=()),
            comparison=SimpleNamespace(
                portfolio_xirr=xirr,
                benchmark_xirr=Decimal("0.11"),
                excess_over_benchmark=Decimal("0.01"),
            ),
            max_drawdown=Decimal("0.2"),
            total_charges=Decimal("1000"),
            benchmark_index_name="stub index",
            digest=digest,
            ledger=ledger,
        )

    monkeypatch.setattr(sweep_module, "open_swing_lake", open_lake)
    monkeypatch.setattr(sweep_module, "_run_arm", run_arm)
    yield counters


def _plan(out: Path) -> CampaignPlan:
    return CampaignPlan(
        out_dir=out, windows=_WINDOWS, data_root=None, book_actions=False, arms=_ARMS
    )


# ── resume ─────────────────────────────────────────────────────────────────────────────────────


def test_a_second_invocation_over_finished_runs_performs_zero_backtests(
    tmp_path: Path, stubbed: _Counters
) -> None:
    first = run_campaign(_plan(tmp_path), workers=1)
    # 2 arms x 2 floors x (3 sweep windows + the 2 walk-forward halves).
    assert len(stubbed.backtests) == 20
    assert len(set(stubbed.backtests)) == 20
    assert sum(o.replayed for o in first) == 20 and sum(o.resumed for o in first) == 0
    assert len(list((tmp_path / "ledgers").glob("*.json"))) == 20

    stubbed.backtests.clear()
    stubbed.lakes = 0
    second = run_campaign(_plan(tmp_path), workers=1)
    assert stubbed.backtests == []
    assert stubbed.lakes == 0  # not even the lake is opened when every run is on disk
    assert sum(o.replayed for o in second) == 0 and sum(o.resumed for o in second) == 20


def test_a_partly_finished_campaign_replays_only_what_is_missing(
    tmp_path: Path, stubbed: _Counters
) -> None:
    run_campaign(_plan(tmp_path), workers=1)
    victim = sorted((tmp_path / "runs").glob("*.json"))[0]
    victim.unlink()  # as if the worker died after the ledger and before the summary
    stubbed.backtests.clear()
    outcomes = run_campaign(_plan(tmp_path), workers=1)
    assert len(stubbed.backtests) == 1
    assert sum(o.resumed for o in outcomes) == 19


def test_the_reports_come_from_the_persisted_ledgers_with_after_tax_columns(
    tmp_path: Path, stubbed: _Counters
) -> None:
    run_campaign(_plan(tmp_path), workers=1)
    stubbed.backtests.clear()
    reports = render_campaign(_plan(tmp_path), _PROFILE, fmv=MappingGrandfatheringPrices({}))
    assert stubbed.backtests == []
    assert set(reports) == {
        "sweep-full.md",
        "sweep-decade.md",
        "sweep-six-year.md",
        "verdict-walk-forward.md",
    }
    sweep = reports["sweep-full.md"]
    assert "After-tax XIRR (realised)" in sweep and "After-tax XIRR (liquidated)" in sweep
    assert "Slab rate on dividends** (from FY2020-21): 30.00%" in sweep
    assert "15.00% on 111A/112A tax" in sweep
    # The stub held its one lot to the end: nothing realised, so the realised after-tax XIRR is
    # the ledger's own pre-tax one; the deemed sale taxes the gain. By hand: 6,00,000 - (5,00,000
    # - 500 STT) = 1,00,500 of LTCG (held two years), 500 above the ₹1 lakh 112A exemption, at 10 %
    # = 50, surcharge 15 % = 7.50, cess 4 % = 2.30 -> ₹59.80, printed to the rupee.
    full = _WINDOWS.named("full")
    ledger_xirr = xirr([Cashflow(full.start, -_CASH), Cashflow(full.end, Decimal("1100000"))])
    row = next(
        line for line in sweep.splitlines() if line.startswith("| 1 | Swing composite (M10.7) |")
    )
    assert f"| 12.00% | {ledger_xirr:.2%} | " in row
    assert "| ₹0 / ₹60 |" in row
    assert "| **0.60** |" in row  # ranked on the persisted pre-tax figures, as a fresh run would be
    assert "verification" in reports["verdict-walk-forward.md"].lower()
    assert "Slab rate on dividends" in reports["verdict-walk-forward.md"]


def test_more_than_two_workers_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CampaignError, match=r"1\.\.2"):
        run_campaign(_plan(tmp_path), workers=3)


def test_a_directory_started_by_another_commit_is_refused(tmp_path: Path) -> None:
    check_manifest(tmp_path, {"version": 1, "commit": "aaa"})
    check_manifest(tmp_path, {"version": 1, "commit": "aaa"})  # the same campaign resumes
    with pytest.raises(CampaignError, match="commit"):
        check_manifest(tmp_path, {"version": 1, "commit": "bbb"})


# ── render-only at a later commit: honest only if nothing is replayed ──────────────────────────

_PINNED = "a" * 40
_LATER = "b" * 40


def _ancestry(monkeypatch: pytest.MonkeyPatch, *, is_ancestor: bool) -> None:
    def fake_run(cmd: list[str], **_: Any) -> SimpleNamespace:
        assert cmd[:3] == ["git", "merge-base", "--is-ancestor"]
        return SimpleNamespace(returncode=0 if is_ancestor else 1)

    monkeypatch.setattr("backtest.campaign.subprocess.run", fake_run)


def test_render_only_accepts_a_later_commit_that_descends_from_the_pinned_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ancestry(monkeypatch, is_ancestor=True)
    check_manifest(tmp_path, {"version": 1, "commit": _PINNED})
    check_manifest(tmp_path, {"version": 1, "commit": _LATER}, runs_from_commit=_PINNED[:7])
    # Without the flag the ordinary guard still refuses, and the pinned manifest is untouched.
    with pytest.raises(CampaignError, match="commit"):
        check_manifest(tmp_path, {"version": 1, "commit": _LATER})
    assert _PINNED in (tmp_path / "manifest.json").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("existing", "current", "named", "is_ancestor", "why"),
    [
        ({"commit": _PINNED, "lake": "x"}, {"commit": _LATER, "lake": "y"}, _PINNED, True, "lake"),
        ({"commit": _PINNED}, {"commit": _LATER}, "c" * 40, True, "does not name"),
        ({"commit": _PINNED}, {"commit": _LATER}, "aaa", True, "does not name"),  # too short
        ({"commit": _PINNED + "-dirty"}, {"commit": _LATER}, _PINNED, True, "clean commit"),
        ({"commit": _PINNED}, {"commit": _LATER + "-dirty"}, _PINNED, True, "dirty"),
        ({"commit": _PINNED}, {"commit": _LATER}, _PINNED, False, "not an ancestor"),
    ],
)
def test_render_only_refuses_anything_but_the_named_commit_differing(
    monkeypatch: pytest.MonkeyPatch,
    existing: dict[str, Any],
    current: dict[str, Any],
    named: str,
    is_ancestor: bool,
    why: str,
) -> None:
    _ancestry(monkeypatch, is_ancestor=is_ancestor)
    with pytest.raises(CampaignError, match=why):
        check_render_only(existing, current, named)


def test_missing_runs_names_every_run_a_render_would_have_to_replay(
    tmp_path: Path, stubbed: _Counters
) -> None:
    assert len(missing_runs(_plan(tmp_path), None)) == 20  # nothing on disk yet
    run_campaign(_plan(tmp_path), workers=1)
    assert missing_runs(_plan(tmp_path), None) == []
    sorted((tmp_path / "runs").glob("*.json"))[0].unlink()
    assert len(missing_runs(_plan(tmp_path), None)) == 1


def test_missing_runs_keys_on_the_corporate_action_source_it_is_given(
    tmp_path: Path, stubbed: _Counters
) -> None:
    # A run's digest covers the corporate-action source. Runs made without one are not the runs a
    # render under a calendar needs — the check must say so, not wave them through or miss them
    # because it derived digests outside the source the render will use.
    run_campaign(_plan(tmp_path), workers=1)
    calendar = BookActionCalendar(())
    assert len(missing_runs(_plan(tmp_path), calendar)) == 20
    assert missing_runs(_plan(tmp_path), None) == []


# ── the sweep's pre-run digest is the runner's own ─────────────────────────────────────────────


class _StopError(Exception):
    """Raised by the spy once the runner has computed its spec."""


@pytest.mark.parametrize("arm", [*_ARMS, next(a for a in ARMS if a.v2 is not None)])
def test_the_sweep_predicts_the_digest_the_runner_persists_under(
    arm: Arm, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, str]] = []
    real = run_module.backtest_spec

    def spy(*args: Any, **kwargs: Any) -> dict[str, str]:
        captured.append(real(*args, **kwargs))
        raise _StopError  # the spec is the runner's first act; nothing after it is under test

    monkeypatch.setattr(run_module, "backtest_spec", spy)
    universe = UniverseParameters(median_turnover_floor=HIGH_FLOOR)
    start, end = date(2020, 1, 1), date(2021, 1, 1)
    with pytest.raises(_StopError):
        if arm.swing is not None:
            run_swing_composite(
                start=start, end=end, parameters=arm.swing, opening_cash=_CASH, universe=universe
            )
        elif arm.naive is not None:
            run_naive_momentum(
                start=start, end=end, parameters=arm.naive, opening_cash=_CASH, universe=universe
            )
        else:
            assert arm.v2 is not None
            run_momentum_v2(
                start=start, end=end, v2_parameters=arm.v2, opening_cash=_CASH, universe=universe
            )
    monkeypatch.setattr(run_module, "backtest_spec", real)
    predicted = _arm_spec(
        arm, start=start, end=end, universe=universe, opening_cash=_CASH, adjusted=True
    )
    assert captured == [predicted]


# ── no silent investor defaults ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "missing", ["--slab-rate", "--cg-surcharge", "--dividend-surcharge", "--payment"]
)
def test_every_after_tax_cli_refuses_a_missing_investor_flag(missing: str) -> None:
    at = _INVESTOR.index(missing)
    partial = _INVESTOR[:at] + _INVESTOR[at + 2 :]
    for parse, base in (
        (sweep_args, ["--from", "2020-01-01", "--to", "2021-01-01"]),
        (verdict_args, []),
        (campaign_args, ["--out", "x", "--workers", "1"]),
    ):
        with pytest.raises(SystemExit) as raised:
            parse([*base, *partial])
        assert raised.value.code == 2
        parse([*base, *_INVESTOR])  # and with every flag it parses


def test_a_report_without_an_investor_profile_is_an_error_not_a_default() -> None:
    row = SweepRow(
        arm=ARMS[0],
        floor=LOW_FLOOR,
        summary=RunSummary.from_document(
            {
                "version": 1,
                "digest": "d",
                "spec": {},
                "policy": "stub",
                "start": "2020-01-01",
                "terminal": "2021-01-01",
                "sessions": 2,
                "xirr": "0.1",
                "max_drawdown": "0.2",
                "excess": "0",
                "benchmark_xirr": "0.1",
                "benchmark_name": "x",
                "total_charges": "1",
                "final_nav": "1",
                "round_trips": 0,
                "median_hold_days": 0,
                "replay_digest": "r",
            }
        ),
    )
    with pytest.raises(ValueError, match="investor profile"):
        render_sweep_report(SweepResult(rows=[row]), floors=[LOW_FLOOR])
    with pytest.raises(ValueError, match="no after-tax figures attached"):
        render_sweep_report(SweepResult(rows=[row], profile=_PROFILE), floors=[LOW_FLOOR])
