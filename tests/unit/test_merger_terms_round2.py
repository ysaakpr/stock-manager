"""Round 2 of the curated merger terms: every newly sourced scheme converts, and nothing else moves.

* Each scheme sourced in round 2 is in the file with the exact numbers its source states, and
  ``merger_term_actions`` turns it into a book action — a term dropped, skipped or inverted fails
  here by name;
* a swap's non-share leg (Cairn India's Vedanta preference shares) is credited on the *old*
  share count at its sourced face value, in both books, and changes no decision input;
* every quote taken from a document with a text layer is verbatim in that document.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pypdf import PdfReader

from backtest.book_actions import (
    AppliedMerger,
    AppliedSchemeCash,
    BookActionCalendar,
    CashExit,
    ShareSwap,
    current_signal_split_factors,
    merger_term_actions,
    signal_split_factors,
)
from backtest.run_ledger import _replayed_quantities
from dataplatform.corpactions import (
    CashExitTerm,
    MergerTermsError,
    ShareSwapTerm,
    TermSource,
    load_merger_terms,
)
from tests.unit.test_book_actions import _buy, _RecordingPolicy, _Scripted, _StaleMarkWalk
from tests.unit.test_merger_terms import (
    NEW,
    OLD,
    SESSIONS,
    T1,
    T2,
    T3,
    T4,
    T5,
    _no_chain,
    _no_edge,
    _prices,
    _SwapRecordingData,
)

_LAKE = Path("/home/ubuntu/stock-manager/data/L0")

#: old ISIN -> (survivor, shares received, shares held, rupees per old share), as the sources state.
ROUND_2_SWAPS: dict[str, tuple[str, str, str, str]] = {
    "INE850A01010": ("INE536H01010", "284", "100", "0"),  # Mahindra Ugine -> Mahindra CIE
    "INE605A01026": ("INE726V01018", "1", "1", "0"),  # Pricol -> Pricol (formerly Pricol Pune)
    "INE910H01017": ("INE205A01025", "1", "1", "40"),  # Cairn -> Vedanta + 4 RPS x Rs 10
    "INE824B01021": ("INE081A01020", "1", "15", "0"),  # Tata Steel BSL -> Tata Steel
    "INE988K01017": ("INE063P01018", "231", "100", "0"),  # Equitas Holdings -> Equitas SFB
    "INE312H01016": ("INE191H01014", "3", "10", "0"),  # INOX Leisure -> PVR
    "IN9155A01020": ("INE155A01022", "7", "10", "0"),  # Tata Motors 'A' (DVR) -> ordinary
    "INE763G01038": ("INE090A01021", "67", "100", "0"),  # ICICI Securities -> ICICI Bank
    "INE140A01024": ("INE202B01038", "1", "1", "67"),  # Piramal Ent -> Piramal Finance + NCRPS
    "INE274G01010": ("INE126M01010", "294", "100", "0"),  # Dhani (fully paid) -> Yaari
    "IN9274G01034": ("INE126M01010", "162", "100", "0"),  # Dhani (partly paid) -> Yaari
    "INE558B01017": ("INE088F01024", "187", "100", "0"),  # Mangalore Chemicals -> Paradeep
}
#: old ISIN -> (exit price, delisting date).
ROUND_2_EXITS: dict[str, tuple[str, date]] = {
    "INE763A01023": ("480", date(2018, 8, 1)),  # Polaris (Virtusa delisting)
}
#: Still unapplied, each for a stated reason: no survivor ISIN, several survivors, an unlisted leg.
STILL_UNSOURCED = {"INE479E01028", "INE018B01012", "INE594A01014", "INE974H01013", "INE797A01021"}


def _swap_actions() -> dict[str, ShareSwap]:
    actions = merger_term_actions(load_merger_terms(), _no_chain, _no_edge)
    return {a.isin: a for a in actions if isinstance(a, ShareSwap)}


# ── the file states what the sources state ─────────────────────────────────────────────────────


@pytest.mark.parametrize("old", sorted(ROUND_2_SWAPS))
def test_each_round_2_swap_is_in_the_file_as_sourced(old: str) -> None:
    survivor, received, held, cash = ROUND_2_SWAPS[old]
    by_old = {t.old_isin: t for t in load_merger_terms().share_swaps}
    term = by_old[old]
    assert (term.surviving_isin, term.shares_received, term.shares_held) == (
        survivor,
        Decimal(received),
        Decimal(held),
    )
    assert term.cash_per_share_held == Decimal(cash)
    assert term.knowable_date <= term.record_date, old  # the ratio was public before it applied


@pytest.mark.parametrize("old", sorted(ROUND_2_EXITS))
def test_each_round_2_exit_is_in_the_file_as_sourced(old: str) -> None:
    price, effective = ROUND_2_EXITS[old]
    by_old = {t.old_isin: t for t in load_merger_terms().cash_exits}
    assert (by_old[old].exit_price, by_old[old].effective_date) == (Decimal(price), effective)


def test_no_scheme_is_both_applied_and_unsourced_and_none_is_lost() -> None:
    terms = load_merger_terms()
    unsourced = {u.old_isin for u in terms.unsourced}
    assert unsourced == STILL_UNSOURCED
    applied = {t.old_isin for t in terms.share_swaps} | {t.old_isin for t in terms.cash_exits}
    assert set(ROUND_2_SWAPS) | set(ROUND_2_EXITS) <= applied
    actions = merger_term_actions(terms, _no_chain, _no_edge)
    converted = {a.isin for a in actions if isinstance(a, ShareSwap | CashExit)}
    assert not converted & STILL_UNSOURCED  # an unsourced scheme is never converted


# ── each new scheme converts a holding (fails if its conversion is skipped) ────────────────────


def _walk_through(action: ShareSwap, quantity: int) -> _StaleMarkWalk:
    """Hold ``quantity`` of the old ISIN into the swap, re-dated onto the fixture's sessions."""
    swap = replace(action, ex_date=T3, record_date=T3, knowable_date=T1)
    prices = {(swap.isin, T1): Decimal("100"), (swap.isin, T2): Decimal("100")}
    prices |= {(swap.surviving_isin, day): Decimal("100") for day in SESSIONS}
    walk = _StaleMarkWalk(
        prices,
        _Scripted({T1: (_buy(swap.isin, quantity),)}),
        BookActionCalendar([swap]),
        sessions=SESSIONS,
    )
    walk.run()
    return walk


@pytest.mark.parametrize("old", sorted(ROUND_2_SWAPS))
def test_each_round_2_swap_converts_a_holding_into_its_survivor(old: str) -> None:
    survivor, received, held, cash = ROUND_2_SWAPS[old]
    actions = _swap_actions()
    assert old in actions, f"{old}: the curated swap produced no book action"
    walk = _walk_through(actions[old], 100)
    expected = int(100 * Decimal(received) / Decimal(held))  # floored, old -> new
    assert walk.sim.held_quantity(old) == 0 and walk.book.position(old) is None
    position = walk.book.position(survivor)
    assert position is not None and position.quantity == expected
    assert walk.sim.held_quantity(survivor) == expected
    credited = walk.cash[T3] - walk.cash[T2]
    assert credited == 100 * Decimal(cash)
    assert walk.sim.cash == walk.book.cash


def test_the_round_2_exit_surrenders_at_the_exit_price() -> None:
    (exit_,) = (
        a
        for a in merger_term_actions(load_merger_terms(), _no_chain, _no_edge)
        if isinstance(a, CashExit) and a.isin in ROUND_2_EXITS
    )
    walk = _StaleMarkWalk(
        {(exit_.isin, T1): Decimal("470"), (exit_.isin, T2): Decimal("470")},
        _Scripted({T1: (_buy(exit_.isin, 10),)}),
        BookActionCalendar([replace(exit_, ex_date=T4, knowable_date=T1)]),
        sessions=SESSIONS,
    )
    walk.run()
    assert walk.sim.held_quantity(exit_.isin) == 0
    assert walk.cash[T4] - walk.cash[T3] == 10 * Decimal("480")


# ── the non-share leg ──────────────────────────────────────────────────────────────────────────


def _cash_swap(numerator: str = "1", denominator: str = "2", cash: str = "10") -> ShareSwap:
    return ShareSwap(
        isin=OLD,
        ex_date=T3,
        surviving_isin=NEW,
        numerator=Decimal(numerator),
        denominator=Decimal(denominator),
        record_date=T3,
        knowable_date=T1,
        cash_per_share=Decimal(cash),
    )


def _cash_walk(swap: ShareSwap, quantity: int = 100) -> _StaleMarkWalk:
    walk = _StaleMarkWalk(
        _prices(),
        _Scripted({T1: (_buy(OLD, quantity),)}),
        BookActionCalendar([swap]),
        sessions=SESSIONS,
    )
    walk.run()
    return walk


def test_the_cash_leg_is_paid_per_old_share_not_per_new() -> None:
    walk = _cash_walk(_cash_swap())  # 1 new per 2 old, Rs 10 per old share
    position = walk.book.position(NEW)
    assert position is not None and position.quantity == 50
    assert walk.cash[T3] - walk.cash[T2] == Decimal("1000")  # 100 old x 10; per new would be 500
    assert walk.sim.cash == walk.book.cash
    assert walk.broker.applied_actions == (
        AppliedSchemeCash(OLD, NEW, T3, 100, Decimal("10"), Decimal("1000")),
        AppliedMerger(OLD, NEW, T3, Decimal("1"), Decimal("2"), old_quantity=100, new_quantity=50),
    )
    assert walk.broker.corporate_actions_applied == {
        "MERGER:share_swap": 1,
        "MERGER:scheme_cash": 1,
    }


def test_the_cash_leg_counts_the_old_shares_a_floored_conversion_forfeits() -> None:
    walk = _cash_walk(_cash_swap(), quantity=99)  # 49.5 new, floored to 49
    position = walk.book.position(NEW)
    assert position is not None and position.quantity == 49
    assert walk.cash[T3] - walk.cash[T2] == Decimal("990")


def test_the_cash_leg_keeps_nav_continuous_when_the_old_close_prices_it_in() -> None:
    # 100 old at 51 = 5,100 = 1 new (100) per 2 old -> 50 x 100 = 5,000 plus 100 x Rs 1 cash.
    walk = _cash_walk(_cash_swap(cash="1"))
    assert walk.nav[T3] == walk.nav[T2]


def test_the_whole_basis_moves_to_the_survivor_and_the_cash_is_realized() -> None:
    walk = _cash_walk(_cash_swap())
    survivor = walk.book.position(NEW)
    assert survivor is not None
    assert survivor.cost_basis == walk.sim.ledger()[0].debit
    assert walk.book.realized_pnl == Decimal("1000")


def test_a_swap_without_a_cash_leg_credits_nothing() -> None:
    walk = _cash_walk(_cash_swap(cash="0"))
    assert walk.cash[T3] == walk.cash[T2]
    assert "MERGER:scheme_cash" not in walk.broker.corporate_actions_applied


def test_a_float_or_negative_cash_leg_is_refused() -> None:
    with pytest.raises(TypeError):
        replace(_cash_swap(), cash_per_share=10.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _cash_swap(cash="-1")


def test_a_float_cash_leg_value_in_the_file_is_refused(tmp_path: Path) -> None:
    body = (
        "share_swaps:\n"
        "  - old_isin: INE910H01017\n    old_company: A\n    surviving_isin: INE205A01025\n"
        "    surviving_company: B\n    shares_received: 1\n    shares_held: 1\n"
        "    record_date: 2017-04-27\n    knowable_date: 2016-07-22\n"
        "    sources:\n      - l0_key: x\n        quote: q\n"
        "    cash_legs:\n      - instrument: RPS\n        units_received: 4\n"
        "        units_held: 1\n        value_inr: VALUE\n        value_basis: face\n"
        "        sources:\n          - l0_key: y\n            quote: q\n"
    )
    good = tmp_path / "good.yaml"
    good.write_text(body.replace("VALUE", '"10"'))
    (term,) = load_merger_terms(good).share_swaps
    assert term.cash_per_share_held == Decimal("40")
    for bad_value in ("10.0", '"0"'):
        bad = tmp_path / "bad.yaml"
        bad.write_text(body.replace("VALUE", bad_value))
        with pytest.raises(MergerTermsError):
            load_merger_terms(bad)


def test_the_cash_leg_changes_the_merger_terms_identity() -> None:
    plain = BookActionCalendar([_cash_swap(cash="0")]).merger_terms_identity()
    with_cash = BookActionCalendar([_cash_swap()]).merger_terms_identity()
    assert plain is not None and with_cash is not None and plain != with_cash


def test_the_run_ledger_reconciles_across_a_cash_leg() -> None:
    walk = _cash_walk(_cash_swap())
    ledger = walk.broker.run_ledger(
        source="cash-leg",
        terminal=T5,
        terminal_nav=walk.nav[T5],
        terminal_prices={NEW: Decimal("110")},
    )
    assert _replayed_quantities(ledger) == {NEW: 50}
    assert len(ledger.trades) == 1  # the buy; the cash leg is not booked as a sale


# ── PIT: the round-2 terms and the cash leg reach no decision input ────────────────────────────


def test_a_swap_with_a_cash_leg_changes_no_decision_input() -> None:
    """With and without the swap (and its cash): every signal read, PIT scope and factor equal."""
    runs = []
    for actions in ([], [_cash_swap()]):
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
    assert factors_on == factors_off == ()
    assert on.broker.applied_actions != off.broker.applied_actions  # ...yet the swap was live


def test_the_whole_curated_calendar_contributes_no_signal_factor() -> None:
    calendar = BookActionCalendar(merger_term_actions(load_merger_terms(), _no_chain, _no_edge))
    with signal_split_factors(calendar):
        assert current_signal_split_factors() == ()


# ── provenance: quotes from text-layer documents are verbatim ──────────────────────────────────


def _document_sources() -> list[tuple[str, TermSource]]:
    terms = load_merger_terms()
    out: list[tuple[str, TermSource]] = []
    sourced: list[ShareSwapTerm | CashExitTerm] = [*terms.share_swaps, *terms.cash_exits]
    for term in sourced:
        out += [(term.old_isin, s) for s in term.sources]
        if isinstance(term, ShareSwapTerm):
            out += [(term.old_isin, s) for leg in term.cash_legs for s in leg.sources]
    return [(isin, s) for isin, s in out if s.member is None and not s.scanned]


def _squash(text: str) -> str:
    return "".join(text.split())


def _document_text(path: Path) -> str:
    readers: list[PdfReader] = []
    if path.suffix == ".zip":
        archive = zipfile.ZipFile(path)
        readers = [
            PdfReader(io.BytesIO(archive.read(name)))
            for name in archive.namelist()
            if name.lower().endswith(".pdf")
        ]
    else:
        readers = [PdfReader(path)]
    return "".join(_squash(page.extract_text() or "") for r in readers for page in r.pages)


@pytest.mark.parametrize(
    ("isin", "source"),
    _document_sources(),
    ids=[f"{isin}:{s.l0_key.rsplit('/', 1)[-1][:40]}" for isin, s in _document_sources()],
)
def test_every_text_layer_quote_is_verbatim_in_its_document(isin: str, source: TermSource) -> None:
    """Whitespace aside (PDF extraction re-flows it), every fragment of the quote is in the text."""
    path = _LAKE / source.l0_key
    if not path.is_file():
        pytest.skip(f"L0 object {source.l0_key} is not on this host (no lake, e.g. CI)")
    text = _document_text(path)
    assert text, f"{isin}: {source.l0_key} has no text layer; mark the source scanned"
    fragments = [f for f in source.quote.split("...") if f.strip()]
    missing = [f.strip() for f in fragments if _squash(f) not in text]
    assert not missing, f"{isin}: not in {source.l0_key}: {missing}"
