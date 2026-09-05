"""The MTO delivery parser, against the real archive files it exists to read.

Three captured sessions: the first session this platform holds prices for (2016-09-02), the last
session only MTO serves (2019-09-27), and one both sources serve (2024-06-20) — the last is what
makes the era join testable offline rather than asserted in a docstring.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse import delivery, mto

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures"
MTO_DIR: Final = FIXTURES / "nse_mto"
OVERLAP: Final = date(2026, 8, 7)


def _mto(name: str) -> bytes:
    return (MTO_DIR / name).read_bytes()


def test_the_oldest_session_this_platform_holds_prices_for_parses() -> None:
    """2016-09-02 is the first price session in the lake; MTO is the only delivery route to it."""
    rows = mto.parse(_mto("MTO_02092016.DAT"), filename="MTO_02092016.DAT")
    assert len(rows) == 1611
    assert all(row.trade_date == date(2016, 9, 2) for row in rows)
    first = next(row for row in rows if row.symbol == "20MICRONS")
    assert (first.series, first.deliv_qty, first.deliv_pct) == ("EQ", 56159, Decimal("63.39"))


def test_the_series_is_kept_apart_from_the_symbol() -> None:
    """The header names six columns and a data row spends seven fields; the split is symbol/series.

    Trusting the header count would shear the series off every row, and `(symbol, series)` is the
    file's key — the same ISIN can trade in more than one series on a date.
    """
    rows = mto.parse(_mto("MTO_27092019.DAT"), filename="MTO_27092019.DAT")
    assert {row.series for row in rows} >= {"EQ", "GS"}
    assert all(row.symbol and " " not in row.symbol for row in rows)
    # No row kept a comma-joined "SYMBOL,SERIES" in its symbol.
    assert not [row for row in rows if row.symbol.isdigit()]


def test_the_session_comes_from_the_file_not_the_filename() -> None:
    """A file is addressed by date in its URL; the date it is *about* comes from its contents."""
    rows = mto.parse(_mto("MTO_07082026.DAT"), filename="anything-at-all.DAT")
    assert {row.trade_date for row in rows} == {OVERLAP}


def test_a_file_for_the_wrong_session_is_refused() -> None:
    """Delivery figures filed under the wrong date are a look-ahead leak, not a cosmetic error."""
    with pytest.raises(ParseError, match="look-ahead leak"):
        mto.parse(
            _mto("MTO_07082026.DAT"), filename="MTO_07082026.DAT", trade_date=date(2026, 8, 6)
        )


def test_mto_and_sec_bhavdata_agree_exactly_where_both_exist() -> None:
    """The claim the era join rests on, pinned to two real files for the same session.

    If these ever disagreed, splicing the two eras would put a visible seam in the delivery history
    at 2019-09-30 — and a factor built on delivery would read that seam as a change in the market
    rather than a change in our sourcing.
    """
    mto_rows = mto.parse(_mto("MTO_07082026.DAT"), filename="MTO_07082026.DAT")
    sbd_name = "sec_bhavdata_full_07082026.csv"
    sbd_rows = delivery.parse(
        (FIXTURES / "nse_delivery" / sbd_name).read_bytes(), filename=sbd_name
    )

    by_mto = {(r.symbol, r.series): r.deliv_qty for r in mto_rows}
    by_sbd = {(r.symbol, r.series): r.deliv_qty for r in sbd_rows if r.deliv_qty is not None}
    common = by_mto.keys() & by_sbd.keys()
    assert len(common) > 2_000, "the two files barely overlap; the comparison proves nothing"
    assert [k for k in common if by_mto[k] != by_sbd[k]] == []
    # MTO is a superset on this session: it values keys the modern file leaves blank, and leaves
    # none blank that the modern file fills. So the old era is not a degraded one.
    assert by_sbd.keys() - by_mto.keys() == set()


def test_a_dash_is_absent_not_zero() -> None:
    """A security with no delivery reported is not a security that delivered nothing."""
    doctored = _mto("MTO_02092016.DAT").replace(
        b"20MICRONS,EQ,88586,56159,63.39", b"20MICRONS,EQ,88586,-,-"
    )
    rows = mto.parse(doctored, filename="doctored.DAT")
    row = next(r for r in rows if r.symbol == "20MICRONS")
    assert row.deliv_qty is None and row.deliv_pct is None


def test_a_repeated_security_is_refused() -> None:
    """Two delivery figures for one (symbol, series) cannot both be right."""
    text = _mto("MTO_02092016.DAT").decode()
    doctored = text.replace("20,2,3IINFOTECH,EQ", "20,2,20MICRONS,EQ", 1).encode()
    with pytest.raises(ParseError, match="appears twice"):
        mto.parse(doctored, filename="doctored.DAT")


def test_a_body_that_is_not_an_mto_file_fails_loudly() -> None:
    with pytest.raises(ParseError):
        mto.parse(b"<html>404</html>", filename="not-mto.DAT")


def test_a_header_without_any_security_records_is_a_failure() -> None:
    """A truncated file must not pass for a session on which nothing was delivered."""
    head = b"\n".join(_mto("MTO_02092016.DAT").splitlines()[:4])
    with pytest.raises(ParseError, match="no security records"):
        mto.parse(head, filename="truncated.DAT")
