"""`Pd` — index levels, NIFTY 50 flags and `CORP_IND` ex-markers, one frozen fixture per era.

Fixtures are reduced from the authoritative lake (`tests/fixtures/nse_pr_bundle/pd/<era>/`): the
`Pd` member and the readme, byte-for-byte; `manifest.json` records the member's sha256 and the
original bundle's full member list, and the first test re-hashes it so a fixture cannot drift.
Offline: nothing here opens a socket or reads a clock.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse.pr_bundle import MemberKind, PrBundle
from dataplatform.ingest.nse.pr_bundle.pd import PD_COLUMNS, parse_pd, parse_pd_bundle

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "nse_pr_bundle" / "pd"

#: era → (archive, session, index rows, security rows, banners).
ERAS: Final[dict[str, tuple[str, date, int, int, int]]] = {
    "unpadded": ("PR040110.zip", date(2010, 1, 4), 10, 1333, 6),
    "trailing_header_cell": ("PR140510.zip", date(2010, 5, 14), 10, 1374, 7),
    "padded": ("PR150615.zip", date(2015, 6, 15), 35, 1550, 7),
    "lowercase": ("PR040926.zip", date(2026, 9, 4), 139, 3655, 7),
}

HEADER: Final = ",".join(PD_COLUMNS)


def _bundle(era: str) -> PrBundle:
    archive = ERAS[era][0]
    return PrBundle((FIXTURES / era / archive).read_bytes(), filename=archive)


@pytest.mark.parametrize("era", sorted(ERAS))
def test_fixture_member_is_byte_identical_to_the_lake(era: str) -> None:
    manifest = json.loads((FIXTURES / era / "manifest.json").read_text())
    with _bundle(era) as bundle:
        body = bundle.read(MemberKind.PD)
    assert hashlib.sha256(body).hexdigest() == manifest["member_sha256"]
    assert len(body) == manifest["member_bytes"]


@pytest.mark.parametrize("era", sorted(ERAS))
def test_every_era_parses_into_index_and_security_rows(era: str) -> None:
    _, session, n_index, n_security, n_banners = ERAS[era]
    with _bundle(era) as bundle:
        parsed = parse_pd_bundle(bundle)
    assert parsed.publication_date == session
    assert (len(parsed.index_rows), len(parsed.security_rows), len(parsed.banners)) == (
        n_index,
        n_security,
        n_banners,
    )
    assert all(row.session == session for row in parsed.security_rows)
    # IND_SEC=Y on a security row is NIFTY 50 membership: 50 names in every era.
    members = [row for row in parsed.security_rows if row.nifty50_flag]
    assert len(members) == 50
    assert {row.section for row in members} == {parsed.banners[0]}
    assert parsed.banners[0].endswith("Sec")


def test_unpadded_era_reads_dotted_counts_and_the_blank_mkt_vix_row() -> None:
    with _bundle("unpadded") as bundle:
        parsed = parse_pd_bundle(bundle)
    nifty = parsed.index_rows[0]
    assert nifty.index_name == "S&P CNX Nifty"
    assert nifty.close == Decimal("5232.2")
    assert nifty.traded_qty == 903330835  # printed '903330835.00'
    vix = next(row for row in parsed.index_rows if row.index_name == "India Vix")
    assert vix.close == Decimal("23.64")
    # Blank is None, never zero; a published zero stays zero.
    assert vix.trades is None and vix.hi_52wk is None
    assert vix.traded_qty == 0


def test_corp_ind_marks_are_kept_verbatim() -> None:
    with _bundle("padded") as bundle:
        parsed = parse_pd_bundle(bundle)
    marks = {(row.symbol, row.series): row.corp_ind for row in parsed.security_rows if row.corp_ind}
    assert marks[("INFY", "EQ")] == "XDBO"
    assert marks[("ASSAMCO", "EQ")] == "XO"
    assert marks[("TORNTPHARM", "EQ")] == "XD"
    assert len(marks) == 3


def test_lowercase_era_carries_market_codes_and_index_names() -> None:
    with _bundle("lowercase") as bundle:
        parsed = parse_pd_bundle(bundle)
    assert parsed.index_rows[0].index_name == "Nifty 50"
    assert {"N", "G", "O"} <= {row.mkt for row in parsed.security_rows}
    assert "India VIX" in {row.index_name for row in parsed.index_rows}


# ── fail loud ────────────────────────────────────────────────────────────────────────────────

ROW: Final = "N,EQ,FOO,FOO LTD,10,10,11,9,10.5,1000,100,N, ,5,12,8"


def _parse(body: str) -> object:
    return parse_pd(body.encode(), filename="Pd010120.csv", publication_date=date(2020, 1, 1))


def test_an_unknown_header_cell_raises() -> None:
    with pytest.raises(ParseError, match="unexpected header cell 'SURPRISE'"):
        _parse(HEADER + ",SURPRISE\n" + ROW + ",x\n")


def test_a_missing_header_column_raises() -> None:
    with pytest.raises(ParseError, match="missing"):
        _parse(HEADER.replace(",CORP_IND", "") + "\n")


def test_a_value_beyond_the_header_raises() -> None:
    with pytest.raises(ParseError, match="beyond its 16-column header"):
        _parse(HEADER + "\n" + ROW + ",stray\n")


def test_a_short_row_raises() -> None:
    with pytest.raises(ParseError, match="expected 16 columns"):
        _parse(HEADER + "\nN,EQ,FOO\n")


def test_a_security_without_mkt_raises() -> None:
    with pytest.raises(ParseError, match="no SERIES or no MKT"):
        _parse(HEADER + "\n" + ROW.replace("N,EQ", " ,EQ", 1) + "\n")


def test_a_priced_row_with_a_series_and_no_symbol_raises() -> None:
    with pytest.raises(ParseError, match="no symbol"):
        _parse(HEADER + "\n" + ROW.replace("FOO,", " ,", 1) + "\n")


def test_an_unknown_corp_ind_raises() -> None:
    with pytest.raises(ParseError, match="CORP_IND"):
        _parse(HEADER + "\n" + ROW.replace("N, ,5", "N,EXDIV,5") + "\n")


def test_a_fractional_count_raises() -> None:
    with pytest.raises(ParseError, match="whole count"):
        _parse(HEADER + "\n" + ROW.replace("N, ,5,", "N, ,5.5,") + "\n")


def test_a_banner_names_the_section_of_the_rows_below_it() -> None:
    parsed = parse_pd(
        f"{HEADER}\n , , ,TRADE FOR TRADE STOCKS, , , , , , , ,N, , , , \n{ROW}\n".encode(),
        filename="Pd010120.csv",
        publication_date=date(2020, 1, 1),
    )
    assert parsed.banners == ("TRADE FOR TRADE STOCKS",)
    assert parsed.security_rows[0].section == "TRADE FOR TRADE STOCKS"
    assert parsed.security_rows[0].nifty50_flag is False
    assert parsed.security_rows[0].corp_ind is None
