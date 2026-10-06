"""The pre-ISIN resolver admits a row only on the exchange's own chain evidence (l1-widen).

Every test here is built to fail if the logic is inverted: a reused symbol that maps to the new
holder's ISIN, a re-issued ISIN pushed back across its creation, a rename followed the wrong way,
or a row two chains claim being admitted to either.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from dataplatform.identity.pre_isin import (
    ActionEvidence,
    ChainRow,
    PreIsinReason,
    Rename,
    ResolverConfig,
    is_first_issue,
    issuer_of,
    resolve,
)
from dataplatform.ingest.nse import bhavcopy_legacy

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "nse_bhavcopy"

#: The first ISIN-era session of the synthetic tapes below.
CUT: Final = date(2011, 6, 22)

FIRST_ISSUE: Final = "INE002A01018"  # serial 01: the issuer's first equity ISIN
REISSUED: Final = "INE144J01027"  # serial 02: re-issued once (20MICRONS' face-value change)
OTHER: Final = "INE118H01017"  # another issuer's first issue


def _days(n: int, *, end: date = CUT) -> list[date]:
    """`n` consecutive weekdays ending the session before `end`, ascending."""
    out: list[date] = []
    day = end
    while len(out) < n:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            out.append(day)
    return sorted(out)


def _after(n: int) -> list[date]:
    """`n` weekdays from CUT inclusive."""
    out: list[date] = []
    day = CUT
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def _tape(
    symbol: str,
    closes: list[str],
    days: list[date],
    *,
    isin: str | None = None,
    first_prev: str | None = None,
    series: str = "EQ",
) -> list[ChainRow]:
    """A row per day whose PREVCLOSE is the previous row's CLOSE (an unbroken exchange chain)."""
    rows: list[ChainRow] = []
    prev = Decimal(first_prev if first_prev is not None else closes[0])
    for day, close in zip(days, closes, strict=True):
        rows.append(ChainRow(symbol, series, day, Decimal(close), prev, isin))
        prev = Decimal(close)
    return rows


def _isin_of(rows: list[ChainRow], **kw: object) -> dict[date, str | None]:
    report = resolve(rows, first_isin_session=CUT, **kw)  # type: ignore[arg-type]
    return {r.trade_date: r.isin for r in report.resolutions}


def _verdicts(rows: list[ChainRow], **kw: object) -> dict[tuple[str, date], PreIsinReason]:
    report = resolve(rows, first_isin_session=CUT, **kw)  # type: ignore[arg-type]
    return {(r.symbol, r.trade_date): r.reason for r in report.resolutions}


def test_the_isin_serial_says_whether_it_was_ever_reissued() -> None:
    assert is_first_issue("INE002A01018")
    assert not is_first_issue("INE144J01027")
    assert not is_first_issue("INF204KB14I2")  # fund units are never classified as first issue
    assert issuer_of("INE144J01019") == issuer_of("INE144J01027") == "INE144J"


def test_an_unbroken_chain_carries_the_anchor_isin_back() -> None:
    pre = _days(5)
    post = _after(2)
    closes = ["100", "101", "102", "103", "104"]
    rows = _tape("ABC", closes, pre) + _tape(
        "ABC", ["105", "106"], post, isin=FIRST_ISSUE, first_prev="104"
    )
    assert set(_isin_of(rows).values()) == {FIRST_ISSUE}


def test_a_reused_symbol_never_maps_to_the_new_holders_isin() -> None:
    """Company A traded as ABC and stopped; B listed as ABC later. A's rows must stay unresolved.

    Inverted logic (ignore the gap, or ignore PREVCLOSE) would hand A's 2010 rows to B's ISIN.
    """
    a_days = _days(40)[:10]
    b_days = _days(40)[-5:]  # 25 silent sessions in between
    filler = _tape("FILL", ["1"] * 40, _days(40))  # the market kept trading meanwhile
    rows = filler + (
        _tape("ABC", [str(50 + i) for i in range(10)], a_days)
        + _tape("ABC", [str(200 + i) for i in range(5)], b_days, first_prev="180")
        + _tape("ABC", ["205"], _after(1), isin=OTHER, first_prev="204")
    )
    got = _isin_of(rows)
    assert all(got[d] is None for d in a_days), "a reused symbol was mapped across the gap"
    assert all(got[d] == OTHER for d in b_days)
    reasons = _verdicts(rows)
    assert reasons[("ABC", a_days[-1])] is PreIsinReason.CHAIN_GAP


def test_a_reused_symbol_without_a_gap_is_still_refused_by_prevclose() -> None:
    """No silence at all, but B's first PREVCLOSE is not A's last CLOSE: a different security."""
    pre = _days(6)
    rows = (
        _tape("ABC", ["10", "11", "12"], pre[:3])
        + _tape("ABC", ["300", "301", "302"], pre[3:], first_prev="290")
        + _tape("ABC", ["303"], _after(1), isin=OTHER, first_prev="302")
    )
    got = _isin_of(rows)
    assert [got[d] for d in pre[:3]] == [None, None, None]
    assert [got[d] for d in pre[3:]] == [OTHER] * 3


def test_a_split_on_a_reissued_isin_stops_the_chain_and_the_settlement_margin() -> None:
    """GRUH 2012-07-24 shape: PREVCLOSE unadjusted, CLOSE a fifth of it, new ISIN later.

    Rows before the split belong to the predecessor ISIN, which no pre-ISIN row can name; rows in
    the first sessions after it may still have carried the old ISIN on the exchange's own file.
    """
    pre = _days(12)
    closes = ["750"] * 4 + ["150"] * 8  # split takes effect on pre[4]
    rows = _tape("GRUH", closes, pre) + _tape(
        "GRUH", ["151"], _after(1), isin=REISSUED, first_prev="150"
    )
    cfg = ResolverConfig(reissue_settlement=3)
    got = _isin_of(rows, config=cfg)
    assert all(got[d] is None for d in pre[:4]), "a re-issued ISIN was pushed across its creation"
    assert all(got[d] is None for d in pre[4:7]), "the settlement margin admitted a boundary row"
    assert all(got[d] == REISSUED for d in pre[7:])


def test_the_same_move_on_a_first_issue_isin_is_admitted() -> None:
    """A serial-01 ISIN was never re-issued, so a bonus-shaped move cannot be its creation."""
    pre = _days(8)
    closes = ["750"] * 4 + ["375"] * 4
    rows = _tape("RELI", closes, pre) + _tape(
        "RELI", ["376"], _after(1), isin=FIRST_ISSUE, first_prev="375"
    )
    assert set(_isin_of(rows).values()) == {FIRST_ISSUE}


def test_a_reissued_isin_crosses_a_break_only_with_a_stored_non_reissuing_action() -> None:
    pre = _days(6)
    # PREVCLOSE adjusted on pre[3] (bonus 1:1): 400 -> 200.
    rows = _tape("XYZ", ["400", "401", "400"], pre[:3]) + _tape(
        "XYZ", ["201", "202", "203"], pre[3:], first_prev="200"
    )
    rows += _tape("XYZ", ["204"], _after(1), isin=REISSUED, first_prev="203")
    bare = _isin_of(rows, config=ResolverConfig(reissue_settlement=0))
    assert [bare[d] for d in pre[:3]] == [None] * 3
    bonus = [ActionEvidence(issuer=issuer_of(REISSUED), ex_date=pre[3], action_type="BONUS")]
    corroborated = _isin_of(rows, actions=bonus, config=ResolverConfig(reissue_settlement=0))
    assert set(corroborated.values()) == {REISSUED}
    split = [
        *bonus,
        ActionEvidence(issuer=issuer_of(REISSUED), ex_date=pre[3], action_type="SPLIT"),
    ]
    contradicted = _isin_of(rows, actions=split, config=ResolverConfig(reissue_settlement=0))
    assert [contradicted[d] for d in pre[:3]] == [None] * 3


def test_a_rename_is_followed_to_the_old_symbol_and_never_the_other_way() -> None:
    pre = _days(6)
    change = pre[3]
    rows = (
        _tape("OLDCO", ["10", "11", "12"], pre[:3])
        + _tape("NEWCO", ["13", "14", "15"], pre[3:], first_prev="12")
        + _tape("NEWCO", ["16"], _after(1), isin=FIRST_ISSUE, first_prev="15")
        # someone else's NEWCO rows before the rename must not be claimed
        + _tape("NEWCO", ["900", "901", "902"], pre[:3])
    )
    renames = [Rename(effective=change, old="OLDCO", new="NEWCO")]
    report = resolve(rows, first_isin_session=CUT, renames=renames)
    got = {(r.symbol, r.trade_date): r.isin for r in report.resolutions}
    assert all(got[("OLDCO", d)] == FIRST_ISSUE for d in pre[:3])
    assert all(got[("NEWCO", d)] == FIRST_ISSUE for d in pre[3:])
    assert all(got[("NEWCO", d)] is None for d in pre[:3]), "a pre-rename holder was claimed"
    without = _isin_of(rows)  # no rename evidence: OLDCO is simply unreached
    assert without  # sanity
    plain = resolve(rows, first_isin_session=CUT)
    assert all(r.isin is None for r in plain.resolutions if r.symbol == "OLDCO"), (
        "a rename was inferred without NSE's record of it"
    )


def test_a_symbol_vacated_by_a_rename_is_not_walked_into() -> None:
    """VAC was renamed away to GONE on pre[3]; a later VAC holder must stop there."""
    pre = _days(6)
    rows = (
        _tape("VAC", ["10", "11", "12"], pre[:3])
        + _tape("VAC", ["12", "12", "12"], pre[3:], first_prev="12")  # coincident prices
        + _tape("VAC", ["12"], _after(1), isin=OTHER, first_prev="12")
    )
    renames = [Rename(effective=pre[3], old="VAC", new="GONE")]
    got = _isin_of(rows, renames=renames)
    assert [got[d] for d in pre[:3]] == [None] * 3
    assert _verdicts(rows, renames=renames)[("VAC", pre[2])] is PreIsinReason.CHAIN_SYMBOL_VACATED


def test_a_row_two_chains_claim_is_admitted_by_neither() -> None:
    """AAA renamed to both BBB and CCC on one day: neither successor may take AAA's history."""
    pre = _days(4)
    change = pre[2]
    rows = (
        _tape("AAA", ["10", "11"], pre[:2])
        + _tape("BBB", ["12", "13"], pre[2:], first_prev="11")
        + _tape("CCC", ["12", "13"], pre[2:], first_prev="11")
        + _tape("BBB", ["14"], _after(1), isin=FIRST_ISSUE, first_prev="13")
        + _tape("CCC", ["14"], _after(1), isin="INE040A01034", first_prev="13")
    )
    renames = [Rename(change, "AAA", "BBB"), Rename(change, "AAA", "CCC")]
    report = resolve(rows, first_isin_session=CUT, renames=renames)
    aaa = [r for r in report.resolutions if r.symbol == "AAA"]
    assert aaa and all(r.isin is None for r in aaa), "a doubly-claimed row was admitted"
    assert {r.reason for r in aaa} <= {
        PreIsinReason.CONFLICTING_CLAIMS,
        PreIsinReason.CHAIN_SYMBOL_VACATED,
    }


def test_non_equity_series_are_out_of_scope_and_aux_series_ride_the_chain_row() -> None:
    pre = _days(3)
    rows = _tape("ABC", ["10", "11", "12"], pre)
    rows += _tape("ABC", ["10.5"], pre[1:2], series="BL")
    rows += _tape("ABC", ["99"], pre[1:2], series="N1")
    rows += _tape("ABC", ["13"], _after(1), isin=FIRST_ISSUE, first_prev="12")
    report = resolve(rows, first_isin_session=CUT)
    by = {(r.series, r.trade_date): r for r in report.resolutions}
    assert by[("BL", pre[1])].isin == FIRST_ISSUE
    assert by[("N1", pre[1])].reason is PreIsinReason.SERIES_OUT_OF_SCOPE
    assert by[("N1", pre[1])].isin is None


def test_the_real_2011_boundary_links_on_prevclose() -> None:
    """The frozen 2011-06-21 (no ISIN) / 2011-06-22 (ISIN) pair, as NSE published it.

    Every admitted 06-21 row must carry exactly the ISIN its symbol stated on 06-22, and only a
    row whose 06-22 PREVCLOSE equals its 06-21 CLOSE (or a first-issue ISIN's adjusted one) links.
    """
    e1 = FIXTURES / "pre_isin" / "cm21JUN2011bhav.csv.zip"
    e2 = FIXTURES / "legacy" / "cm22JUN2011bhav.csv.zip"
    pre = bhavcopy_legacy.parse_pre_isin_prices(e1.read_bytes(), filename=e1.name)
    post = bhavcopy_legacy.parse(e2.read_bytes(), filename=e2.name)
    rows = [ChainRow(r.symbol, r.series, r.trade_date, r.close, r.prev_close) for r in pre] + [
        ChainRow(r.symbol, r.series, r.trade_date, r.close, r.prev_close, r.isin) for r in post
    ]
    report = resolve(rows, first_isin_session=CUT)
    stated = {(r.symbol, r.series): r for r in post}
    before = {(r.symbol, r.series): r for r in pre}
    admitted = [r for r in report.resolutions if r.isin is not None]
    assert len(admitted) > 1000
    for res in admitted:
        later = stated.get((res.symbol, "EQ")) or stated.get((res.symbol, res.series))
        assert later is not None and later.isin == res.isin
        if res.series == "EQ" and (res.symbol, "EQ") in stated:
            link = stated[(res.symbol, "EQ")].prev_close == before[(res.symbol, "EQ")].close
            assert link or is_first_issue(res.isin)
    assert {r.reason for r in report.resolutions} <= set(PreIsinReason)


def test_the_priced_pre_isin_parser_agrees_with_the_enumerator() -> None:
    path = FIXTURES / "pre_isin" / "cm02JAN2006bhav.csv.zip"
    priced = bhavcopy_legacy.parse_pre_isin_prices(path.read_bytes(), filename=path.name)
    enumerated = bhavcopy_legacy.parse_pre_isin(path.read_bytes(), filename=path.name)
    assert [(r.symbol, r.series, r.line) for r in priced] == [
        (r.symbol, r.series, r.line) for r in enumerated
    ]
    assert all(r.close >= 0 and r.prev_close >= 0 for r in priced)
    assert not hasattr(priced[0], "isin")


def test_the_priced_reader_refuses_an_isin_era_file() -> None:
    path = FIXTURES / "legacy" / "cm22JUN2011bhav.csv.zip"
    with pytest.raises(Exception, match="pre-2011-06-22"):
        bhavcopy_legacy.parse_pre_isin_prices(path.read_bytes(), filename=path.name)
