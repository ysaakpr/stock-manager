"""The two identity sources behind the delivery join, each pinned in both directions.

`IdentityMaster.isin_in_force` walks `isin_lineage` edges so a dated fact lands on the ISIN that
traded that day; `SessionIdentity` is the session's own bhavcopy statement of `(symbol, series) →
ISIN`. Synthetic and small on purpose — `test_delivery_identity_eras.py` runs the same rules over
the real files of every era; this file is where an inverted comparison shows up first.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Final

import pytest

from dataplatform.identity.master import Exchange, IdentityMaster, SymbolWindow
from dataplatform.identity.session import SessionIdentity
from dataplatform.ingest.models import PriceRow, is_isin_check_digit_valid, is_keyable_isin
from dataplatform.ingest.nse import delivery
from dataplatform.ingest.nse.delivery import DeliveryRow

#: A real chain: BAJFINANCE, reissued 2016-09-09 and again 2025-06-16 (`isin_lineage`).
A: Final = "INE296A01016"
B: Final = "INE296A01024"
C: Final = "INE296A01032"
FIRST: Final = date(2016, 9, 9)
SECOND: Final = date(2025, 6, 16)
CHAIN: Final = ((A, B, FIRST), (B, C, SECOND))


def _window(symbol: str, isin: str, valid_from: date = date(2003, 4, 1)) -> SymbolWindow:
    return SymbolWindow(
        exchange=Exchange.NSE, symbol=symbol, valid_from=valid_from, valid_to=None, isin=isin
    )


def _price(symbol: str, series: str, isin: str, day: date) -> PriceRow:
    one = Decimal("1")
    return PriceRow(
        isin=isin,
        symbol=symbol,
        series=series,
        trade_date=day,
        open=one,
        high=one,
        low=one,
        close=one,
        last=one,
        prev_close=one,
        total_traded_qty=10,
        total_traded_value=Decimal("10"),
        total_trades=1,
    )


def _deliv(symbol: str, series: str, day: date, qty: int = 5) -> DeliveryRow:
    return DeliveryRow(
        symbol=symbol, series=series, trade_date=day, deliv_qty=qty, deliv_pct=Decimal("50")
    )


# ── isin_in_force ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("on", "expected"),
    [
        (date(2012, 1, 2), A),
        (FIRST - (FIRST - date(2016, 9, 8)), A),  # the last session before the first reissue
        (FIRST, B),  # the successor's first session is the successor's
        (date(2020, 7, 13), B),
        (SECOND, C),
        (date(2026, 8, 7), C),
    ],
)
def test_the_isin_in_force_follows_the_chain_by_effective_date(on: date, expected: str) -> None:
    """From every ISIN of the chain, the answer for a date is the one the bhavcopy carried then.

    Starting from the survivor (what `EQUITY_L.csv` yields), the predecessor (a stale open window)
    and the middle: the walk goes backward and forward as needed. Swap `>` and `<=` in either loop
    and the boundary rows fail.
    """
    master = IdentityMaster((), reissues=CHAIN)
    for start in (A, B, C):
        assert master.isin_in_force(start, on) == expected, (start, on)


def test_try_resolve_in_force_moves_the_window_s_isin_and_leaves_an_unreissued_one() -> None:
    master = IdentityMaster(
        (_window("BAJFINANCE", C), _window("INFY", "INE009A01021")), reissues=CHAIN
    )

    assert master.try_resolve("BAJFINANCE", date(2015, 1, 1)) == C, "the window alone"
    assert master.try_resolve_in_force("BAJFINANCE", date(2015, 1, 1)) == A
    assert master.try_resolve_in_force("BAJFINANCE", date(2026, 1, 1)) == C
    assert master.try_resolve_in_force("INFY", date(2015, 1, 1)) == "INE009A01021"
    assert master.try_resolve_in_force("NOSUCH", date(2015, 1, 1)) is None
    assert master.reissue_count == 2


def test_with_no_edges_in_force_is_exactly_try_resolve() -> None:
    master = IdentityMaster((_window("BAJFINANCE", C),))
    assert master.try_resolve_in_force("BAJFINANCE", date(2015, 1, 1)) == C


def test_two_predecessors_after_the_date_is_a_fork_and_is_not_picked() -> None:
    """Lineage is one-to-one; a successor with two later-dated predecessors is not walked back."""
    master = IdentityMaster(
        (),
        reissues=(
            ("INE111A01011", "INE333A01013", date(2020, 1, 1)),
            ("INE222A01012", "INE333A01013", date(2021, 1, 1)),
        ),
    )
    assert master.isin_in_force("INE333A01013", date(2019, 1, 1)) == "INE333A01013"
    # After the first edge only one predecessor is still in the future: that one is unambiguous.
    assert master.isin_in_force("INE333A01013", date(2020, 6, 1)) == "INE222A01012"


def test_a_cyclic_lineage_terminates() -> None:
    master = IdentityMaster((), reissues=((A, B, FIRST), (B, A, SECOND)))
    assert master.isin_in_force(A, date(2030, 1, 1)) in {A, B}


# ── SessionIdentity ──────────────────────────────────────────────────────────────────────────

DAY: Final = date(2025, 3, 3)


def test_the_session_statement_answers_only_its_own_session_exchange_and_series() -> None:
    session = SessionIdentity.from_statements(
        [
            _price("BANKBEES", "EQ", "INF204KB15I9", DAY),
            _price("IFCI", "EQ", "INE039A01010", DAY),
            _price("IFCI", "NE", "INE039A07777", DAY),
        ],
        exchange=Exchange.NSE,
        trade_date=DAY,
    )

    assert session.try_resolve("BANKBEES", "EQ", DAY, exchange=Exchange.NSE) == "INF204KB15I9"
    assert session.try_resolve(" bankbees ", "eq", DAY, exchange=Exchange.NSE) == "INF204KB15I9"
    assert session.try_resolve("IFCI", "NE", DAY, exchange=Exchange.NSE) == "INE039A07777"
    assert session.try_resolve("IFCI", "EQ", DAY, exchange=Exchange.NSE) == "INE039A01010"
    assert session.try_resolve("BANKBEES", "BE", DAY, exchange=Exchange.NSE) is None
    assert session.try_resolve("BANKBEES", "EQ", date(2025, 3, 4), exchange=Exchange.NSE) is None
    assert session.try_resolve("BANKBEES", "EQ", DAY, exchange=Exchange.BSE) is None


def test_a_key_the_file_states_twice_is_ambiguous_and_never_picked() -> None:
    session = SessionIdentity.from_statements(
        [_price("X", "EQ", "INE002A01018", DAY), _price("X", "EQ", "INE009A01021", DAY)],
        exchange=Exchange.NSE,
        trade_date=DAY,
    )
    assert session.try_resolve("X", "EQ", DAY, exchange=Exchange.NSE) is None
    assert len(session) == 0


def test_a_statement_from_another_session_is_refused() -> None:
    with pytest.raises(ValueError, match="evidence for that session only"):
        SessionIdentity.from_statements(
            [_price("X", "EQ", "INE002A01018", date(2025, 3, 4))],
            exchange=Exchange.NSE,
            trade_date=DAY,
        )


# ── delivery.resolve: the order the two sources are asked in ─────────────────────────────────


def _session(*rows: PriceRow) -> SessionIdentity:
    return SessionIdentity.from_statements(rows, exchange=Exchange.NSE, trade_date=DAY)


def test_the_master_is_asked_first_and_moved_to_the_isin_in_force() -> None:
    master = IdentityMaster((_window("BAJFINANCE", C),), reissues=CHAIN)
    day = date(2020, 7, 13)
    out = delivery.resolve([_deliv("BAJFINANCE", "EQ", day)], master)

    assert [r.isin for r in out.resolved] == [B]
    assert (out.via_lineage, out.via_session, out.session_disagrees) == (1, 0, 0)


def test_a_symbol_the_master_never_saw_is_placed_by_the_session_statement() -> None:
    master = IdentityMaster(())
    rows = [_deliv("BANKBEES", "EQ", DAY), _deliv("GHOST", "EQ", DAY)]

    without = delivery.resolve(rows, master)
    assert [r.symbol for r in without.unresolved] == ["BANKBEES", "GHOST"]

    out = delivery.resolve(
        rows, master, session=_session(_price("BANKBEES", "EQ", "INF204KB15I9", DAY))
    )
    assert [(r.symbol, r.isin) for r in out.resolved] == [("BANKBEES", "INF204KB15I9")]
    assert [r.symbol for r in out.unresolved] == ["GHOST"], "no statement, no guess"
    assert out.via_session == 1


def test_where_master_and_session_disagree_the_session_statement_is_used_and_counted() -> None:
    """The master is series-blind: IFCI's debenture series resolves to IFCI's equity ISIN."""
    master = IdentityMaster((_window("IFCI", "INE039A01010"),))
    session = _session(
        _price("IFCI", "EQ", "INE039A01010", DAY), _price("IFCI", "NE", "INE039A07777", DAY)
    )

    out = delivery.resolve(
        [_deliv("IFCI", "EQ", DAY), _deliv("IFCI", "NE", DAY)], master, session=session
    )

    assert [(r.series, r.isin) for r in out.resolved] == [
        ("EQ", "INE039A01010"),
        ("NE", "INE039A07777"),
    ]
    assert out.session_disagrees == 1
    assert out.via_session == 0


# ── the ISIN check digit ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "isin", ["INE002A01018", "INE237A01028", "INE237A01036", "INF204KB15I9", "IN9155A01020"]
)
def test_real_isins_are_keyable(isin: str) -> None:
    assert is_isin_check_digit_valid(isin)
    assert is_keyable_isin(isin)


@pytest.mark.parametrize(
    "literal", ["INE", "DUMMY", "NA", "-", "", "IN9232101012", "INE002A01017", "ine002a01018"]
)
def test_junk_and_wrong_check_digits_are_not(literal: str) -> None:
    assert not is_keyable_isin(literal)
