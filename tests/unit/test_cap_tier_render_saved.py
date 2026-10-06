"""Render the cap-tier report from saved runs alone, however far the store has moved since.

The a4a4003 turnover-floor campaign (56 runs) could not be rendered on 2026-10-06:

1. **Six runs' cash could not be rebuilt.** The book credits a share swap's cash leg (Cairn India's
   four ₹10 Vedanta preference shares per share, 2017-04-27), but the saved ledger does not record
   it (``run_ledger._tax_events`` drops ``AppliedSchemeCash``). ``idle_cash`` rebuilt the cash
   from the ledger alone, came up short by that credit and refused the run as negative cash. The
   render now rebuilds the credit from the ledger's own swap (``unrecorded_scheme_cash``) and
   refuses any run whose rebuilt cash misses its saved terminal cash by a paisa.
2. **Merger flags came from today's store.** A merger row the store gained after the runs flagged
   a holding the runs' store did not. The flags are printed only at the runs' own store identity,
   and the header says when they were not evaluated.

Each test below builds a run on disk as the campaign persists it and renders it with the store
moved; the first fails on the code before the fix.
"""

from __future__ import annotations

import dataclasses
import json
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

import backtest.cap_tier_campaign as campaign
from backtest.book_actions import BookActionCalendar, CashDividend, UnmodelledAction
from backtest.cap_tier_campaign import CapTierCampaignError, CapTierPlan, cap_tier_plan
from backtest.nav import PRE_TAX, NavSeries, nav_file, write_nav
from backtest.rails import ratified_backtest_rail_policy
from backtest.run_ledger import (
    RunSummary,
    _actions_identity,
    ledger_path,
    summary_path,
    unrecorded_scheme_cash,
)
from backtest.sweep import CAP_TIER_ARMS, FOCUSED_SMALLCAP, HIGH_FLOOR, LOW_FLOOR
from backtest.tax import ReissueEvent, RunLedger, SplitEvent, TaxTrade
from backtest.tax_report import write_run_ledger
from backtest.windows import Window
from backtest.xirr import Cashflow
from dataplatform.corpactions import MergerTerms, SchemeCashLeg, ShareSwapTerm
from execution.broker import Side

D = Decimal
OLD: Final = "INE910H01017"  # Cairn India
NEW: Final = "INE205A01025"  # Vedanta
OTHER: Final = "INE000B01011"
DAY: Final = [date(2024, 1, 1) + timedelta(days=i) for i in range(6)]
ARM: Final = next(arm for arm in CAP_TIER_ARMS if arm.label == FOCUSED_SMALLCAP)
#: The store the runs were made at, and today's: it has since gained a merger row for the holding.
THEN: Final = BookActionCalendar([CashDividend("INE999Z01010", DAY[4], D("1"))])
TODAY: Final = BookActionCalendar(
    [CashDividend("INE999Z01010", DAY[4], D("1")), UnmodelledAction(NEW, DAY[4], "MERGER")]
)


def _terms(rupees_per_share: str = "40") -> MergerTerms:
    leg = SchemeCashLeg(
        instrument="Vedanta Ltd 7.5% redeemable preference share",
        units_received=D(4),
        units_held=D(1),
        value_inr=D(rupees_per_share) / 4,
        value_basis="face value",
        sources=(),
    )
    swap = ShareSwapTerm(
        old_isin=OLD,
        old_company="Cairn India",
        surviving_isin=NEW,
        surviving_company="Vedanta",
        shares_received=D(1),
        shares_held=D(1),
        record_date=DAY[2],
        knowable_date=DAY[0],
        sources=(),
        cash_legs=(leg,),
    )
    return MergerTerms(share_swaps=(swap,), cash_exits=(), unsourced=())


def _trade(when: date, isin: str, quantity: int, net: str) -> TaxTrade:
    return TaxTrade(
        isin=isin,
        trade_date=when,
        side=Side.BUY,
        quantity=quantity,
        net_amount=D(net),
        stt=D(0),
        stt_known=True,
    )


def _ledger() -> RunLedger:
    """₹10 L in; 100 Cairn bought; the swap pays ₹4,000 in preference shares; a buy spends it."""
    return RunLedger(
        source="test",
        trades=(_trade(DAY[1], OLD, 100, "100000"), _trade(DAY[3], OTHER, 904, "904000")),
        external_flows=(Cashflow(DAY[0], -D("1000000")),),
        terminal_date=DAY[-1],
        terminal_nav=D("1014000"),
        terminal_prices={NEW: D("1100"), OTHER: D("1000")},
        corporate_events=(SplitEvent(OLD, DAY[2], 1, 1, 100), ReissueEvent(NEW, DAY[2], OLD)),
    )


NAV: Final = tuple(
    zip(
        DAY,
        (D(v) for v in ("1000000", "1000000", "1004000", "1004000", "1010000", "1014000")),
        strict=True,
    )
)


def _persist(out: Path, digest: str) -> None:
    spec = {
        "book_actions": _actions_identity(THEN),
        "rail_policy": ratified_backtest_rail_policy().digest(),
        "runner": "swing_composite",
    }
    summary = RunSummary(
        digest=digest,
        spec=spec,
        policy="swing_composite",
        start=DAY[0],
        terminal=DAY[-1],
        sessions=len(NAV),
        xirr=D("0.5"),
        max_drawdown=D("0.01"),
        excess=D("0.1"),
        benchmark_xirr=D("0.4"),
        benchmark_name="NIFTY Smallcap 250 TRI",
        total_charges=D("125"),
        final_nav=D("1014000"),
        round_trips=0,
        median_hold_days=0,
        replay_digest="r",
        rail_blocks={},
    )
    ledger_path(out, digest).parent.mkdir(parents=True, exist_ok=True)
    write_run_ledger(_ledger(), ledger_path(out, digest))
    write_nav(NavSeries(digest, PRE_TAX, NAV), nav_file(out, digest))
    summary_path(out, digest).parent.mkdir(parents=True, exist_ok=True)
    summary_path(out, digest).write_text(json.dumps(summary.to_document()), encoding="utf-8")


class _Reader:
    """The lake's last prints: the survivor stopped printing before the window's end."""

    def __init__(self, **_: object) -> None:
        pass

    def last_prints(self, end: date) -> dict[str, date]:
        return {NEW: DAY[3], OTHER: end}

    def close(self) -> None:
        pass


class _NoFmv:
    def fmv_per_share(self, isin: str) -> Decimal:
        raise AssertionError("no lot predates 2018 here")


@pytest.fixture
def plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CapTierPlan:
    digests = {(ARM.label, "full", LOW_FLOOR): "a" * 64, (ARM.label, "full", HIGH_FLOOR): "b" * 64}
    for digest in digests.values():
        _persist(tmp_path, digest)
    monkeypatch.setattr(campaign, "_digests", lambda plan, actions: digests)
    monkeypatch.setattr(campaign, "_L1Reader", _Reader)
    monkeypatch.setattr(campaign, "l1_grandfathering", lambda root: (None, _NoFmv()))
    monkeypatch.setattr(campaign, "_benchmark_path", lambda *args: None)
    monkeypatch.setattr(campaign, "load_merger_terms", _terms, raising=False)
    return CapTierPlan(
        out_dir=tmp_path,
        arms=(ARM,),
        windows=(Window("full", DAY[0], DAY[-1]),),
        data_root=tmp_path,
        universe="turnover_floor",
    )


def _render(plan: CapTierPlan, monkeypatch: pytest.MonkeyPatch, store: BookActionCalendar) -> str:
    monkeypatch.setattr(campaign, "_actions", lambda plan: store)
    return campaign.render(plan, commit="abc")


def _lines(report: str, prefix: str) -> list[str]:
    return [line for line in report.splitlines() if line.startswith(prefix)]


def test_a_moved_store_reproduces_the_saved_runs_figures_exactly(
    plan: CapTierPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = _render(plan, monkeypatch, TODAY)
    then = _render(plan, monkeypatch, THEN)
    rows_today, rows_then = _lines(today, f"| {ARM.label} |"), _lines(then, f"| {ARM.label} |")
    assert len(rows_today) == 4  # per floor: the strategy table and the idle-cash table
    # Every figure but the merger flag is the saved run's, whichever store is in force today.
    assert [r.rsplit("|", 2)[0] for r in rows_today] == [r.rsplit("|", 2)[0] for r in rows_then]
    strategy = rows_today[0]
    assert "| 50.00% |" in strategy  # the saved XIRR
    assert "| 1.00% |" in strategy  # the saved max drawdown
    assert "| 2 |" in strategy  # the saved ledger's trades
    assert "| 0.00% |" in strategy  # end cash: the buy spent the scheme cash, as the run did
    assert strategy.endswith("| 1 (merger flags not evaluated) |")
    assert rows_then[0].endswith("| 1 (0 merger in store, 0 merger terms unsourced) |")
    # The scheme cash is the old name's profit, folded into the survivor: 110,000 + 4,000 - 100,000.
    assert f"{NEW} ₹0.14 L" in today
    assert "**Merger flags not evaluated**" in today
    assert "Merger flags not evaluated" not in then


def test_rebuilt_cash_that_misses_the_saved_terminal_cash_is_refused(
    plan: CapTierPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(campaign, "load_merger_terms", lambda: _terms("50"), raising=False)
    with pytest.raises(CapTierCampaignError, match="does not account for all of the run's cash"):
        _render(plan, monkeypatch, THEN)


def test_scheme_cash_is_rebuilt_from_the_ledgers_own_swap_and_holding() -> None:
    credits = unrecorded_scheme_cash(_ledger(), _terms())
    assert [(c.isin, c.surviving_isin, c.received, c.amount) for c in credits] == [
        (OLD, NEW, DAY[2], D("4000"))
    ]
    pure = MergerTerms(
        share_swaps=(
            ShareSwapTerm(OLD, "c", NEW, "v", D(1), D(1), DAY[2], DAY[0], sources=(), cash_legs=()),
        ),
        cash_exits=(),
        unsourced=(),
    )
    assert unrecorded_scheme_cash(_ledger(), pure) == ()
    # A reissue alone (no rescale on the same date) is a carry, not a swap: nothing is credited.
    carry = dataclasses.replace(_ledger(), corporate_events=(ReissueEvent(NEW, DAY[2], OLD),))
    assert unrecorded_scheme_cash(carry, _terms()) == ()


def test_the_header_names_every_store_identity_the_matched_runs_span(tmp_path: Path) -> None:
    plan = cap_tier_plan(tmp_path, data_root=tmp_path)
    text = "\n".join(
        campaign._header(
            plan,
            commit="a4a4003",
            stores=("calendar[1]:A", "calendar[2]:B"),
            rendered_at="def5678",
            recorded={
                "book_actions": Counter({"calendar[1]:A": 40, "calendar[2]:B": 16}),
                "signal_split_factors": Counter({"split[9]": 56}),
            },
            today_store="calendar[3]:C",
            flags_evaluated=False,
        )
    )
    assert "made at commit `a4a4003` (rendered at `def5678`)" in text
    assert "Saved runs matched by strategy specification with store fields set aside" in text
    assert (
        "span 2 `book_actions` identities: `calendar[1]:A` (40 runs); `calendar[2]:B` (16" in text
    )
    assert "`signal_split_factors` identities" not in text  # one identity: nothing to list
    assert "Today's store is `calendar[3]:C`, not the runs'." in text
    assert "**Merger flags not evaluated**" in text
