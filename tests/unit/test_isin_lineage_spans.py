"""D2 ISIN lineage: the spans are read from the exchange that *printed* the ISIN.

`read_equity_spans` is the one impure step of the derivation, and the one place a second exchange
in L1 can corrupt it. An NSE bhavcopy carries the ISIN as of that session, so a reissue shows up
as one span ending and the next beginning. The BSE legacy bhavcopy (before the 2024-07 UDiFF
cutover) carries no ISIN at all; its rows reach L1 with the ISIN the scrip master holds *today*,
which for a reissued security is the successor — so on a lake that holds both exchanges the
successor appears to trade from the original listing, the two spans overlap, and the derivation
reads the reissue as "concurrent, not sequential" and drops it. Measured on the server on
2026-09-07: IRCTC's INE335Y01020 on BSE from 2019-10-14, two years before NSE issued it.

Offline: synthetic rows under `tmp_path`, no network, no postgres.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from dataplatform.identity.lineage import derive_edges, read_equity_spans
from dataplatform.identity.master import Exchange
from dataplatform.ingest.models import PriceRow
from dataplatform.store.l1 import write_prices_raw

_OLD = "INE335Y01012"
_NEW = "INE335Y01020"
_LAST_OLD = date(2021, 10, 28)
_FIRST_NEW = date(2021, 10, 29)


def _row(isin: str, day: date, *, series: str) -> PriceRow:
    price = Decimal("800.00")
    return PriceRow(
        isin=isin,
        symbol="IRCTC",
        series=series,
        trade_date=day,
        open=price,
        high=price,
        low=price,
        close=price,
        last=price,
        prev_close=price,
        total_traded_qty=1_000,
        total_traded_value=price * 1_000,
        total_trades=10,
    )


def _lake_with_both_exchanges(root: Path) -> None:
    # NSE prints the ISIN of the day: the old one to the 28th, the new one from the 29th.
    for day in (date(2021, 10, 27), _LAST_OLD):
        write_prices_raw([_row(_OLD, day, series="EQ")], exchange=Exchange.NSE, data_root=root)
    write_prices_raw([_row(_NEW, _FIRST_NEW, series="EQ")], exchange=Exchange.NSE, data_root=root)
    # BSE legacy rows were labelled through today's scrip master, so the *successor* ISIN sits on
    # every BSE session, including the two before it existed — and on a BSE-only session.
    for day in (date(2021, 10, 27), _LAST_OLD, _FIRST_NEW, date(2021, 10, 30)):
        write_prices_raw([_row(_NEW, day, series="A")], exchange=Exchange.BSE, data_root=root)


def test_spans_come_from_nse_rows_only(tmp_path: Path) -> None:
    _lake_with_both_exchanges(tmp_path)
    spans, sessions = read_equity_spans(data_root=tmp_path)
    by_isin = {s.isin: s for s in spans}
    assert by_isin[_OLD].last_date == _LAST_OLD
    assert by_isin[_NEW].first_date == _FIRST_NEW, "BSE's relabelled history moved the reissue"
    assert date(2021, 10, 30) not in sessions, "a BSE-only session is not an NSE session"


def test_a_relabelled_bse_history_does_not_hide_the_reissue(tmp_path: Path) -> None:
    _lake_with_both_exchanges(tmp_path)
    spans, sessions = read_equity_spans(data_root=tmp_path)
    edges = derive_edges(spans, sessions, {})
    assert [(e.predecessor_isin, e.successor_isin, e.effective_date) for e in edges] == [
        (_OLD, _NEW, _FIRST_NEW)
    ]
