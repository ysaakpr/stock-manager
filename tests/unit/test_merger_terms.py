"""Curated merger terms (``dataplatform.corpactions.merger_terms``) applied to the backtest book.

Each book test walks the drivers' stack (``ReplayEngine`` → ``_AccountingBroker`` → ``SimBroker`` +
``PortfolioBook``) and fails if the conversion is skipped *or* inverted:

* NAV is continuous across a share swap, and the count is ``held x received / held_ratio`` — the
  inverted ratio (new → old) is a different count and a step in NAV;
* the tax ledger keeps the *original* acquisition date across the swap;
* a pending (unsettled) lot is converted and settles under the survivor;
* a cash exit credits ``qty x exit price`` and closes the position in both books and the ledger;
* an unsourced scheme stays stuck and is counted; a store MERGER row the table covers is dropped;
* two runs are byte-identical, and no decision input changes.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from backtest.book_actions import (
    AppliedCashExit,
    AppliedMerger,
    BookActionCalendar,
    CashExit,
    ShareSwap,
    UnmodelledAction,
    _to_book_actions,
    current_signal_split_factors,
    merger_term_actions,
    signal_split_factors,
)
from backtest.policies.naive_momentum import MomentumRecord
from backtest.run_ledger import _replayed_quantities
from backtest.tax import (
    MappingGrandfatheringPrices,
    ReissueEvent,
    SplitEvent,
    load_tax_schedule,
    match_lots,
)
from dataplatform.corpactions import (
    ActionType,
    CashExitTerm,
    MergerTerms,
    MergerTermsError,
    ShareSwapTerm,
    TermSource,
    UnquantifiedTerms,
    UnsourcedMerger,
    load_merger_terms,
)
from dataplatform.query.pit import Dataset
from execution.broker import Side
from tests.unit.test_book_actions import (
    _buy,
    _imports,
    _RecordingData,
    _RecordingPolicy,
    _Row,
    _Scripted,
    _StaleMarkWalk,
)

OLD = "INE572A01036"  # the transferor: last prints T2
NEW = "INE685A01028"  # the transferee
# T+2 era (2019): a buy staged T1 fills T2 and is still pending on T3.
T1, T2, T3, T4, T5 = (date(2019, 3, d) for d in (4, 5, 6, 7, 8))
SESSIONS = (T1, T2, T3, T4, T5)
RECEIVED, HELD = Decimal("51"), Decimal("100")  # 51 new for every 100 old (JB -> Torrent)


def _prices() -> dict[tuple[str, date], Decimal]:
    # 100 old x 51 = 5,100 = 51 new x 100: the swap is value-neutral at these closes.
    prices = {(OLD, T1): Decimal("51"), (OLD, T2): Decimal("51")}
    prices |= {(NEW, day): Decimal("100") for day in (T1, T2, T3, T4)}
    prices[(NEW, T5)] = Decimal("110")
    return prices


def _swap(ex: date = T3, numerator: Decimal = RECEIVED, denominator: Decimal = HELD) -> ShareSwap:
    return ShareSwap(
        isin=OLD,
        ex_date=ex,
        surviving_isin=NEW,
        numerator=numerator,
        denominator=denominator,
        record_date=ex,
        knowable_date=date(2019, 2, 20),
    )


def _walk(actions: list[object], orders: dict[date, tuple[object, ...]]) -> _StaleMarkWalk:
    walk = _StaleMarkWalk(
        _prices(),
        _Scripted(orders),  # type: ignore[arg-type]
        BookActionCalendar(actions),  # type: ignore[arg-type]
        sessions=SESSIONS,
    )
    walk.run()
    return walk


# ── share swap ─────────────────────────────────────────────────────────────────────────────────


def test_a_share_swap_converts_old_to_new_at_the_ratio_and_keeps_nav_continuous() -> None:
    walk = _walk([_swap()], {T1: (_buy(OLD, 100),)})
    assert walk.sim.held_quantity(OLD) == 0 and walk.book.position(OLD) is None
    survivor = walk.book.position(NEW)
    assert survivor is not None and survivor.quantity == 51  # 100 x 51 / 100, old -> new
    assert walk.nav[T3] == walk.nav[T2]  # 100 x 51 became 51 x 100
    assert walk.nav[T5] - walk.nav[T4] == 51 * Decimal("10")  # now it follows the survivor
    assert walk.broker.corporate_actions_applied == {"MERGER:share_swap": 1}


def test_an_inverted_ratio_is_caught() -> None:
    inverted = _walk([_swap(numerator=HELD, denominator=RECEIVED)], {T1: (_buy(OLD, 100),)})
    position = inverted.book.position(NEW)
    assert position is not None and position.quantity == 196  # 100 x 100 / 51: not 51
    assert inverted.nav[T3] != inverted.nav[T2]  # the fake 92 % jump


def test_without_the_term_the_holding_stays_stuck_at_its_last_print() -> None:
    walk = _walk([], {T1: (_buy(OLD, 100),)})
    assert walk.sim.held_quantity(OLD) == 100
    assert walk.nav[T5] == walk.nav[T2]  # the survivor's +₹10 never reaches the book


def test_the_whole_cost_basis_moves_to_the_survivor() -> None:
    walk = _walk([_swap()], {T1: (_buy(OLD, 100),)})
    survivor = walk.book.position(NEW)
    assert survivor is not None
    paid = walk.sim.ledger()[0].debit  # the buy's whole cash cost, charges included
    assert survivor.cost_basis == paid


def test_a_fractional_entitlement_is_floored_identically_in_both_books() -> None:
    walk = _walk([_swap()], {T1: (_buy(OLD, 99),)})  # 99 x 51 / 100 = 50.49
    assert walk.sim.held_quantity(NEW) == 50
    position = walk.book.position(NEW)
    assert position is not None and position.quantity == 50


def test_a_swap_adds_to_a_survivor_already_held() -> None:
    walk = _walk([_swap()], {T1: (_buy(OLD, 100), _buy(NEW, 10))})
    position = walk.book.position(NEW)
    assert position is not None and position.quantity == 61
    assert walk.sim.held_quantity(NEW) == 61


def test_a_pending_lot_is_converted_and_settles_under_the_survivor() -> None:
    walk = _walk([_swap()], {T1: (_buy(OLD, 100),)})
    assert walk.settled[T3] == {}
    assert walk.pending[T3] == ((NEW, T2, 51),)  # converted while still in settlement
    assert walk.settled[T4] == {NEW: 51}


def test_the_tax_ledger_keeps_the_original_acquisition_date() -> None:
    walk = _walk([_swap()], {T1: (_buy(OLD, 100),)})
    ledger = walk.broker.run_ledger(
        source="swap",
        terminal=T5,
        terminal_nav=walk.nav[T5],
        terminal_prices={NEW: Decimal("110")},
    )
    assert ledger.corporate_events == (SplitEvent(OLD, T3, 51, 100, 51), ReissueEvent(NEW, T3, OLD))
    realisations, open_lots = match_lots(
        ledger, load_tax_schedule(), MappingGrandfatheringPrices({})
    )
    assert realisations == ()  # a swap is not a transfer: nothing is realised
    (lot,) = open_lots[NEW]
    assert (lot.acquired, lot.quantity) == (T2, 51)  # the old buy's trade date, not the swap's


def test_the_applied_log_records_direction_and_counts() -> None:
    walk = _walk([_swap()], {T1: (_buy(OLD, 100),)})
    assert walk.broker.applied_actions == (
        AppliedMerger(OLD, NEW, T3, RECEIVED, HELD, old_quantity=100, new_quantity=51),
    )


def test_a_swap_on_a_name_not_held_changes_nothing() -> None:
    walk = _walk([_swap()], {T1: (_buy(NEW, 10),)})
    assert walk.broker.corporate_actions_applied == {}
    assert walk.sim.held_quantity(NEW) == 10


# ── cash exit ──────────────────────────────────────────────────────────────────────────────────


def test_a_cash_exit_credits_quantity_times_the_exit_price_and_closes_the_position() -> None:
    exit_ = CashExit(isin=OLD, ex_date=T4, price=Decimal("60"), knowable_date=T2)
    walk = _walk([exit_], {T1: (_buy(OLD, 100),)})
    assert walk.sim.held_quantity(OLD) == 0 and walk.book.position(OLD) is None
    assert walk.cash[T4] - walk.cash[T3] == Decimal("6000")
    assert walk.sim.cash == walk.book.cash
    assert walk.broker.applied_actions == (
        AppliedCashExit(OLD, T4, 100, Decimal("60"), Decimal("6000")),
    )
    ledger = walk.broker.run_ledger(
        source="exit", terminal=T5, terminal_nav=walk.nav[T5], terminal_prices={}
    )
    exit_trade = ledger.trades[-1]
    assert (exit_trade.side, exit_trade.quantity, exit_trade.net_amount) == (
        Side.SELL,
        100,
        Decimal("6000"),
    )
    assert _replayed_quantities(ledger) == {}


def test_a_float_exit_price_is_refused() -> None:
    with pytest.raises(TypeError):
        CashExit(isin=OLD, ex_date=T4, price=60.0, knowable_date=T2)  # type: ignore[arg-type]


# ── unsourced, and store rows the table covers ─────────────────────────────────────────────────


def _terms(**overrides: object) -> MergerTerms:
    swap = ShareSwapTerm(
        old_isin=OLD,
        old_company="Old Ltd",
        surviving_isin=NEW,
        surviving_company="New Ltd",
        shares_received=RECEIVED,
        shares_held=HELD,
        record_date=T3,
        knowable_date=date(2019, 2, 20),
        sources=(TermSource(l0_key="x/y.pdf", quote="51 for every 100"),),
    )
    fields: dict[str, object] = {"share_swaps": (swap,), "cash_exits": (), "unsourced": ()}
    fields.update(overrides)
    return MergerTerms(**fields)  # type: ignore[arg-type]


def _no_chain(isin: str) -> tuple[str, ...]:
    return (isin,)


def _no_edge(isin: str) -> date | None:
    return None


def test_an_unsourced_merger_stays_stuck_and_is_counted() -> None:
    terms = _terms(
        share_swaps=(),
        unsourced=(UnsourcedMerger(OLD, "Old Ltd", "share_swap", "ratio not stated", T3),),
    )
    actions = merger_term_actions(terms, _no_chain, _no_edge)
    assert actions == [UnmodelledAction(OLD, T3, "MERGER:unsourced")]
    walk = _walk(actions, {T1: (_buy(OLD, 100),)})  # type: ignore[arg-type]
    assert walk.sim.held_quantity(OLD) == 100
    assert walk.broker.corporate_actions_applied == {"unmodelled:MERGER:unsourced": 1}


def test_a_store_merger_row_the_table_covers_is_replaced_by_the_term() -> None:
    row = _Row(OLD, T3, ActionType.MERGER, UnquantifiedTerms())
    uncovered = _to_book_actions([row], _no_chain, _no_edge)
    assert uncovered == [UnmodelledAction(OLD, T3, "MERGER")]
    assert _to_book_actions([row], _no_chain, _no_edge, covered=frozenset({OLD})) == []


def test_a_swap_waits_for_the_survivors_first_priced_session() -> None:
    def first_priced(isin: str, on: date) -> date | None:
        return T5 if isin == NEW else on

    (swap,) = merger_term_actions(_terms(), _no_chain, _no_edge, first_priced=first_priced)
    assert isinstance(swap, ShareSwap) and swap.ex_date == T5 and swap.record_date == T3


def test_a_survivor_that_never_prints_again_is_not_converted() -> None:
    assert merger_term_actions(_terms(), _no_chain, _no_edge, first_priced=lambda i, o: None) == []


def test_the_survivor_resolves_to_the_isin_live_on_the_record_date() -> None:
    retired = "INE685A01010"

    def chain(isin: str) -> tuple[str, ...]:
        return (retired, NEW) if isin == NEW else (isin,)

    def edge(isin: str) -> date | None:
        return T5 if isin == retired else None  # the survivor's own reissue, after the swap

    (swap,) = merger_term_actions(_terms(), chain, edge)
    assert isinstance(swap, ShareSwap) and swap.surviving_isin == retired


# ── idempotence and PIT ────────────────────────────────────────────────────────────────────────


def test_two_runs_with_a_swap_and_an_exit_are_byte_identical() -> None:
    actions = [_swap(), CashExit(isin=NEW, ex_date=T5, price=Decimal("120"), knowable_date=T1)]
    first = _walk(actions, {T1: (_buy(OLD, 100),)})
    second = _walk(actions, {T1: (_buy(OLD, 100),)})
    assert first.broker.applied_actions == second.broker.applied_actions
    assert first.nav == second.nav and first.cash == second.cash


def test_the_curated_terms_load_identically_twice() -> None:
    assert load_merger_terms() == load_merger_terms()
    calendar = BookActionCalendar(merger_term_actions(load_merger_terms(), _no_chain, _no_edge))
    again = BookActionCalendar(merger_term_actions(load_merger_terms(), _no_chain, _no_edge))
    assert calendar.merger_terms_identity() == again.merger_terms_identity() is not None


class _SwapRecordingData(_RecordingData):
    """The decision-side view: the raw bar of whichever ISIN traded that session."""

    def signal(self, as_of: date) -> Dataset[MomentumRecord]:
        isin, price = (OLD, Decimal("51")) if as_of < T3 else (NEW, Decimal("100"))
        records = (
            MomentumRecord(isin=isin, momentum=Decimal("0.1"), price=price, knowable_date=as_of),
        )
        self.reads.append((as_of, records))
        return Dataset.declaring(
            f"m@{as_of.isoformat()}", records, knowable_date=lambda r: r.knowable_date
        )


def test_a_swap_changes_no_decision_input() -> None:
    """With and without the swap: every signal read, PIT scope and split factor identical."""
    runs = []
    swaps: tuple[list[ShareSwap], ...] = ([], [_swap(ex=T3)])
    for actions in swaps:
        data = _SwapRecordingData()
        policy = _RecordingPolicy(data)
        calendar = BookActionCalendar(actions)
        with signal_split_factors(calendar):
            factors = current_signal_split_factors()
        walk = _StaleMarkWalk(_prices(), policy, calendar, sessions=SESSIONS)
        walk.run()
        runs.append((repr(data.reads), policy.scopes, factors, walk))
    (reads_off, scopes_off, factors_off, off), (reads_on, scopes_on, factors_on, on) = runs
    assert reads_on == reads_off
    assert scopes_on == scopes_off == list(SESSIONS)
    assert factors_on == factors_off == ()  # a swap is never a signal's price factor
    # ...while the account did change: the conversion is live.
    assert on.sim.held_quantity(OLD) != off.sim.held_quantity(OLD)


def test_the_terms_are_physically_outside_the_decision_path() -> None:
    root = Path(__file__).resolve().parents[2]
    for pkg in ("analyst", "backtest/policies", "dataplatform/query"):
        for path in (root / pkg).rglob("*.py"):
            found = _imports(path)
            assert "dataplatform.corpactions.merger_terms" not in found, path
            assert "backtest.book_actions" not in found, path


# ── the curated file ───────────────────────────────────────────────────────────────────────────


def test_every_curated_term_is_sourced_and_directed_old_to_new() -> None:
    terms = load_merger_terms()
    assert terms.share_swaps and terms.cash_exits and terms.unsourced
    sourced: list[ShareSwapTerm | CashExitTerm] = [*terms.share_swaps, *terms.cash_exits]
    for term in sourced:
        assert term.sources and all(s.l0_key and s.quote for s in term.sources)
    by_old = {t.old_isin: t for t in terms.share_swaps}
    gsk = by_old["INE264A01014"]  # 4.39 HUL for every 1 GSK Consumer
    assert (gsk.surviving_isin, gsk.shares_received / gsk.shares_held) == (
        "INE030A01027",
        Decimal("4.39"),
    )
    jb = by_old["INE572A01036"]  # 51 Torrent for every 100 JB Chemicals
    assert (jb.surviving_isin, jb.shares_received, jb.shares_held) == (
        "INE685A01028",
        Decimal("51"),
        Decimal("100"),
    )


def test_every_l0_source_exists_in_the_lake_when_the_lake_is_here() -> None:
    lake = Path("/home/ubuntu/stock-manager/data/L0")
    if not lake.is_dir():
        pytest.skip("no lake on this host")
    terms = load_merger_terms()
    sourced: list[ShareSwapTerm | CashExitTerm] = [*terms.share_swaps, *terms.cash_exits]
    for term in sourced:
        for source in term.sources:
            year_month = source.l0_key.split("/")
            assert any(lake.glob(f"{year_month[0]}/*/*/{year_month[-1]}")), source.l0_key


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ("shares_received: 51", "shares_received: 51.0"),
        ("old_isin: INE572A01036", "old_isin: NOT-AN-ISIN"),
        ("    sources:\n      - l0_key: x", "    sources: []\n    unused:\n      - l0_key: x"),
        ("old_isin: INE264A01014", "old_isin: INE572A01036"),
    ],
)
def test_a_malformed_terms_file_is_refused(tmp_path: Path, patch: str, message: str) -> None:
    body = (
        "share_swaps:\n"
        "  - old_isin: INE572A01036\n    old_company: A\n    surviving_isin: INE685A01028\n"
        "    surviving_company: B\n    shares_received: 51\n    shares_held: 100\n"
        "    record_date: 2026-07-17\n    knowable_date: 2026-07-07\n"
        "    sources:\n      - l0_key: x\n        quote: q\n"
        "  - old_isin: INE264A01014\n    old_company: C\n    surviving_isin: INE030A01027\n"
        "    surviving_company: D\n    shares_received: 439\n    shares_held: 100\n"
        "    record_date: 2020-04-17\n    knowable_date: 2020-04-01\n"
        "    sources:\n      - l0_key: y\n        quote: q\n"
    )
    good = tmp_path / "good.yaml"
    good.write_text(body)
    assert len(load_merger_terms(good).share_swaps) == 2
    bad = tmp_path / "bad.yaml"
    assert body.count(patch) >= 1
    bad.write_text(body.replace(patch, message, 1))
    with pytest.raises(MergerTermsError):
        load_merger_terms(bad)
