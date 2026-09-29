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


# ── the ISIN era, 2011-06-22 → 2016-09-01: one block, or several ────────────────────────────────

#: The first session of the ISIN era (single `N` block), the first multi-block file seen (`D` then
#: `N`), and the last session before this platform's first MTO-era price history already had one.
ISIN_ERA_START: Final = "MTO_22062011.DAT"
MULTI_BLOCK: Final = "MTO_01072011.DAT"
RANGE_END: Final = "MTO_01092016.DAT"

#: The N-block row for 20MICRONS on 2011-07-01, verbatim, used to doctor the D block.
_N_ROW: Final = "20,2,20MICRONS,EQ,12394,4683,37.78"
_D_ROW: Final = "20,1,1000E14,GC,10,10,100.00"


def _with_d_block_row(row: str) -> bytes:
    """The real 2011-07-01 file with `row` added to its `D` block, after the block's one row."""
    text = _mto(MULTI_BLOCK).decode()
    assert text.count(_D_ROW) == 1
    return text.replace(_D_ROW, f"{_D_ROW}\n{row}", 1).encode()


@pytest.mark.parametrize(
    ("name", "session", "rows", "shape"),
    [
        (ISIN_ERA_START, date(2011, 6, 22), 1473, "N"),
        (MULTI_BLOCK, date(2011, 7, 1), 1468, "D+N"),
        (RANGE_END, date(2016, 9, 1), 1636, "N"),
    ],
)
def test_each_isin_era_shape_parses_to_one_row_per_record(
    name: str, session: date, rows: int, shape: str
) -> None:
    """Every era the W3 range spans, against the bytes the archive served — the `10` record's own
    count is the check that no block was skipped and none was read twice."""
    payload = _mto(name)
    parsed = mto.parse(payload, filename=name, trade_date=session)
    assert len(parsed) == rows
    assert payload.decode().splitlines()[1].split(",")[4] == f"{rows:07d}"
    assert mto.block_shape(payload) == shape
    assert mto.stated_date(payload, filename=name) == session
    assert {r.trade_date for r in parsed} == {session}


def test_the_isin_era_start_reads_the_figures_the_file_states() -> None:
    rows = mto.parse(_mto(ISIN_ERA_START), filename=ISIN_ERA_START)
    reliance = next(r for r in rows if (r.symbol, r.series) == ("RELIANCE", "EQ"))
    assert reliance.deliv_qty == 2343085
    assert reliance.deliv_pct == Decimal("50.98")


def test_both_settlement_blocks_are_read() -> None:
    """The `D` block's row and the `N` block's rows all land; neither block shadows the other."""
    rows = {(r.symbol, r.series): r for r in mto.parse(_mto(MULTI_BLOCK), filename=MULTI_BLOCK)}
    assert rows[("1000E14", "GC")].deliv_qty == 10
    assert rows[("20MICRONS", "EQ")].deliv_qty == 4683
    assert rows[("ZYLOG", "EQ")].deliv_qty == 21498


def test_a_key_restated_in_a_second_block_is_not_double_counted() -> None:
    """The double-count test. The same figures in two blocks are one fact: one row, the stated
    quantity — not two rows (a join would fan out) and not their sum (9,366)."""
    doctored = _with_d_block_row(_N_ROW)
    rows = mto.parse(doctored, filename="doctored.DAT")
    micron = [r for r in rows if (r.symbol, r.series) == ("20MICRONS", "EQ")]
    assert len(micron) == 1
    assert micron[0].deliv_qty == 4683
    assert len(rows) == len(mto.parse(_mto(MULTI_BLOCK), filename=MULTI_BLOCK))


def test_a_key_with_different_figures_in_two_blocks_is_refused() -> None:
    """Two blocks disagreeing on one key: summing may double-count, picking one is arbitrary."""
    doctored = _with_d_block_row("20,2,20MICRONS,EQ,100,50,50.00")
    with pytest.raises(ParseError, match="settlement blocks D and N with different figures"):
        mto.parse(doctored, filename="doctored.DAT")


def test_a_repeat_inside_one_block_is_still_refused_in_a_multi_block_file() -> None:
    text = _mto(MULTI_BLOCK).decode()
    doctored = text.replace("20,3,3IINFOTECH,EQ", "20,3,20MICRONS,EQ", 1).encode()
    with pytest.raises(ParseError, match="appears twice"):
        mto.parse(doctored, filename="doctored.DAT")


def test_a_block_for_another_session_is_refused() -> None:
    """A block dated otherwise inside the file is another session's figures — a look-ahead leak."""
    text = _mto(MULTI_BLOCK).decode()
    stale = "Trade Date <30-JUN-2011>,Settlement Type <N>"
    doctored = text.replace("Trade Date <01-JUL-2011>,Settlement Type <N>", stale, 1).encode()
    with pytest.raises(ParseError, match="settlement block states trade date 2011-06-30"):
        mto.parse(doctored, filename="doctored.DAT")


def test_a_file_that_names_no_settlement_type_has_an_untyped_shape() -> None:
    assert mto.block_shape(b"10,MTO,02012003,1,0000001\n20,1,ABC,EQ,1,1,100.00\n") == "untyped"
